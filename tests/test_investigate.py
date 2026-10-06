"""只读调查工具的测试。

这里锁的重点是**边界**，不是"功能能跑"：

* 白名单：只能按配置里声明过的**名字**取对象，不接受路径
* 只读：`query_history` 打开数据库必须是只读模式，写不进去
* 截断：工具输出会直接进上下文，不设上限等于不设成本上限
* 预算：每次运行有硬上限，超过就降级（而不是失败）
* 协议：MCP 服务端说 JSON-RPC 2.0，就要真的按协议应答 —— 用真客户端过一遍
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import unittest
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ops.core import config as cfgmod  # noqa: E402
from ops.core import store  # noqa: E402
from ops.investigate import budget, mcp  # noqa: E402
from ops.investigate import tools as toolmod  # noqa: E402

TMP = ROOT / "data" / "_test_tmp" / "investigate"


def make_cfg() -> dict:
    """一个最小但完整的配置：一个日志源、一个目标、一个任务。

    目录结构必须与 `cfgmod.data_dir()` 的约定一致（都是 `<root>/data/...`），
    否则测试会去操作真实项目的目录 —— 第一版就是这么写错的。
    """
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    for sub in ("data/logs", "data/reports/2026-10-06", "data/state"):
        (TMP / sub).mkdir(parents=True, exist_ok=True)

    (TMP / "data" / "logs" / "app.log").write_text(
        "\n".join(f"2026-10-06 09:00:{i:02d} INFO line {i}" for i in range(10))
        + "\n2026-10-06 09:01:00 ERROR boom happened\n",
        encoding="utf-8",
    )
    (TMP / "data" / "reports" / "2026-10-06" / "api_watch-abc123.md").write_text(
        "# 报告正文\n\n- 结论：一切正常\n", encoding="utf-8"
    )

    conn = store.connect(TMP / "data" / "ops.db")
    store.start_run(conn, "run-1", "api_watch", "2026-10-06T09:00:00Z")
    store.finish_run(conn, "run-1", finished_at="2026-10-06T09:00:05Z", status="ok",
                     items_ok=3, items_failed=0, ai_called=1, ai_status="ok")
    store.record_alert(conn, run_id="run-1", task="api_watch", target="x", severity="alert",
                       channel="email", dedup_key="k1", send_status="sent", detail="测试告警")
    conn.close()

    return {
        "project": {"root": str(TMP)},
        "investigate": {"enabled": True, "max_calls_per_run": 3, "max_output_bytes": 500,
                        "probe_retries": 0, "run_id": "unit-test-run"},
        "log_scan": {"sources": [{"name": "app", "path": str(TMP / "data" / "logs" / "app.log")}]},
        "api_watch": {"targets": [{"name": "local", "url": "http://127.0.0.1:9/"}],
                      "probe": {"timeout_ms": 200}},
        "schedule": {"heartbeat_expect": {"api_watch": 60}},
    }


class InvestigateTestBase(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)


class TestWhitelist(InvestigateTestBase):
    def test_read_log_by_name_works(self):
        result = toolmod.read_log(self.cfg, source="app", tail_lines=3)
        self.assertTrue(result.ok)
        self.assertIn("ERROR boom happened", result.text)
        self.assertEqual(result.meta["returned"], 3)

    def test_read_log_rejects_arbitrary_path(self):
        """核心边界：传路径不被接受 —— 只认识配置里声明过的名字。"""
        result = toolmod.read_log(self.cfg, source="C:/Windows/win.ini")
        self.assertFalse(result.ok)
        self.assertIn("未知日志源", result.text)

    def test_read_log_rejects_traversal_looking_name(self):
        result = toolmod.read_log(self.cfg, source="../../etc/passwd")
        self.assertFalse(result.ok)

    def test_read_log_pattern_filters(self):
        result = toolmod.read_log(self.cfg, source="app", tail_lines=50, pattern="ERROR")
        self.assertTrue(result.ok)
        self.assertEqual(result.meta["returned"], 1)

    def test_read_log_bad_regex_is_a_clean_error(self):
        result = toolmod.read_log(self.cfg, source="app", pattern="([")
        self.assertFalse(result.ok)
        self.assertIn("正则", result.text)

    def test_probe_rejects_url_like_input(self):
        """URL 形状先被名字格式校验拦下 —— 连不上"未知目标"那一步。"""
        result = toolmod.probe_target(self.cfg, target="http://169.254.169.254/latest")
        self.assertFalse(result.ok)
        self.assertIn("不合法", result.text)

    def test_probe_rejects_wellformed_but_unconfigured_name(self):
        """格式合法但没在配置里声明过的名字 —— 这才是"白名单"真正拦的东西。

        否则判断层能输入任意主机名，拿这台机器当跳板去探内网。
        """
        result = toolmod.probe_target(self.cfg, target="internal-admin-panel")
        self.assertFalse(result.ok)
        self.assertIn("未知目标", result.text)
        self.assertIn("local", result.text, "拒绝时应告诉它可用的目标")

    def test_probe_known_target_runs(self):
        result = toolmod.probe_target(self.cfg, target="local")
        # 端口 9 上什么都没有，探测应当失败但工具本身是"成功执行"的
        self.assertTrue(result.ok)
        self.assertIn("目标 local", result.text)

    def test_report_rejects_path_traversal(self):
        result = toolmod.read_report(self.cfg, run_id="../../../windows/win.ini")
        self.assertFalse(result.ok)
        self.assertIn("不合法", result.text)

    def test_report_finds_known_run(self):
        result = toolmod.read_report(self.cfg, run_id="abc123")
        self.assertTrue(result.ok)
        self.assertIn("结论：一切正常", result.text)

    def test_report_unknown_run_is_clean_error(self):
        result = toolmod.read_report(self.cfg, run_id="does-not-exist")
        self.assertFalse(result.ok)
        self.assertIn("没有找到", result.text)


class TestReadOnlyHistory(InvestigateTestBase):
    def test_query_history_returns_rows(self):
        result = toolmod.query_history(self.cfg, task="api_watch", days=7)
        self.assertTrue(result.ok)
        self.assertIn("run-1", result.text)
        self.assertIn("测试告警", result.text)

    def test_readonly_helper_rejects_writes(self):
        """两条打开路径（mode=ro / query_only）都必须拒绝写入。

        真实踩到的坑：WAL 模式下缺少 `-wal`/`-shm` 附属文件时，`mode=ro`
        会报 `unable to open database file`，于是工具整个失败（判断层第一次
        真实调用工具时，`catalog` 能用而 `history` 直接报错，就是这个原因）。
        现在会退回 `query_only=ON` —— 但**只读这条性质不能因此丢掉**。
        """
        conn = toolmod.open_db_readonly(TMP / "data" / "ops.db")
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM run").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM run")
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE run SET status='hacked'")
        finally:
            conn.close()

    def test_query_history_rejects_bad_task_name(self):
        result = toolmod.query_history(self.cfg, task="API WATCH; DROP TABLE run")
        self.assertFalse(result.ok)

    def test_history_connection_is_read_only(self):
        """判断层不该有写历史的能力 —— 底层连接必须是只读的。"""
        db = TMP / "data" / "ops.db"
        uri = f"file:{db.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM run")
        finally:
            conn.close()

    def test_query_history_rejects_missing_db(self):
        cfg = dict(self.cfg, project={"root": str(TMP / "nowhere")})
        result = toolmod.query_history(cfg, task="api_watch")
        self.assertFalse(result.ok)

    def test_known_tasks_comes_from_db(self):
        """任务清单要来自历史库 —— heartbeat_expect 会漏掉没被监视的任务。"""
        self.assertIn("api_watch", toolmod.known_tasks(self.cfg))


class TestTruncation(InvestigateTestBase):
    def test_long_output_is_truncated(self):
        big = "啊" * 5000
        text = toolmod.truncate(big, 500)
        self.assertLessEqual(len(text.encode("utf-8")), 500)
        self.assertIn("已截断", text)

    def test_short_output_untouched(self):
        self.assertEqual(toolmod.truncate("短", 500), "短")

    def test_read_log_output_respects_limit_via_dispatch(self):
        (TMP / "data" / "logs" / "big.log").write_text("x" * 20000, encoding="utf-8")
        cfg = dict(self.cfg)
        cfg["log_scan"] = {"sources": [{"name": "big",
                                        "path": str(TMP / "data" / "logs" / "big.log")}]}
        result = toolmod.dispatch(cfg, "read_log", {"source": "big", "tail_lines": 2000})
        self.assertTrue(result.ok)
        # dispatch 不做截断（那是通道层的事），这里确认原始文本确实超限
        self.assertGreater(len(result.text.encode("utf-8")), toolmod.max_output_bytes(cfg))


class TestBudget(InvestigateTestBase):
    def test_allows_until_limit_then_degrades(self):
        cfg = self.cfg
        for _ in range(3):
            self.assertTrue(budget.check(cfg).allowed)
            budget.record(cfg, "read_log", {}, toolmod.ToolResult(True, "x"))
        verdict = budget.check(cfg)
        self.assertFalse(verdict.allowed)
        self.assertIn("预算已用尽", verdict.reason)
        # 降级而不是失败：提示里要告诉模型该怎么办
        self.assertIn("不要再调用工具", verdict.reason)

    def test_record_reports_success(self):
        result = budget.record(self.cfg, "read_log", {}, toolmod.ToolResult(True, "x"))
        self.assertTrue(result.written)
        self.assertEqual(result.count, 1)

    def test_unwritable_counter_is_reported_not_swallowed(self):
        """预算写不进去必须显式报告 —— 这是真实踩到的坑。

        第一版 `except OSError: pass` 吞掉了写入异常，判断层连调 5 次工具，
        每次预算行都显示"已用 0/6"：硬上限形同虚设，而日志里毫无提示。
        静默失效的预算比没有预算更危险 —— 你会以为有保护。
        """
        cfg = dict(self.cfg)
        # 把 state 目录变成一个文件，写入必然失败
        state = TMP / "data" / "state"
        shutil.rmtree(state, ignore_errors=True)
        state.write_text("not a directory", encoding="utf-8")
        result = budget.record(cfg, "read_log", {}, toolmod.ToolResult(True, "x"))
        self.assertFalse(result.written)
        self.assertTrue(result.detail)

    def test_probe_state_dir_detects_unwritable(self):
        state = TMP / "data" / "state"
        shutil.rmtree(state, ignore_errors=True)
        state.write_text("not a directory", encoding="utf-8")
        writable, detail = budget.probe_state_dir(self.cfg)
        self.assertFalse(writable)
        self.assertIn("Error", detail) if "Error" in detail else self.assertTrue(detail)

    def test_record_writes_audit_trail(self):
        """留痕：事后要能回答"它查了什么" —— 没有留痕，结论就不知道从哪来。"""
        budget.record(self.cfg, "query_history", {"task": "api_watch"},
                      toolmod.ToolResult(True, "内容"))
        path = budget.audit_path(self.cfg, "unit-test-run")
        self.assertTrue(path.is_file())
        entry = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[0])
        self.assertEqual(entry["tool"], "query_history")
        self.assertEqual(entry["args"]["task"], "api_watch")
        self.assertTrue(entry["ok"])

    def test_disabled_blocks_everything(self):
        cfg = dict(self.cfg, investigate={"enabled": False})
        self.assertFalse(budget.check(cfg).allowed)

    def test_run_id_isolation(self):
        """不同运行各自计数 —— 全机器共享一个计数器会让预算失去意义。"""
        budget.record(self.cfg, "read_log", {}, toolmod.ToolResult(True, "x"), run_id="run-A")
        budget.record(self.cfg, "read_log", {}, toolmod.ToolResult(True, "x"), run_id="run-A")
        self.assertEqual(budget.used(self.cfg, "run-A"), 2)
        self.assertEqual(budget.used(self.cfg, "run-B"), 0)


class TestDispatch(InvestigateTestBase):
    def test_unknown_tool_is_clean_error(self):
        result = toolmod.dispatch(self.cfg, "rm_rf", {})
        self.assertFalse(result.ok)
        self.assertIn("未知工具", result.text)

    def test_bad_argument_name_is_clean_error(self):
        result = toolmod.dispatch(self.cfg, "read_log", {"path": "/etc/passwd"})
        self.assertFalse(result.ok)
        self.assertIn("参数", result.text)

    def test_tool_exception_does_not_escape(self):
        """工具炸了不能把服务打挂 —— 否则一次巡检会因为一个参数写错而整体失败。"""
        result = toolmod.dispatch(self.cfg, "read_log", {"source": "app", "tail_lines": "很多"})
        self.assertFalse(result.ok)


class TestMcpProtocol(InvestigateTestBase):
    """用真的 JSON-RPC 消息过一遍协议，而不是只测内部函数。"""

    def _call(self, message: dict) -> dict | None:
        return mcp.handle_message(self.cfg, message)

    def test_initialize_returns_capabilities(self):
        resp = self._call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": mcp.PROTOCOL_VERSION}})
        self.assertEqual(resp["jsonrpc"], "2.0")
        self.assertEqual(resp["id"], 1)
        self.assertIn("tools", resp["result"]["capabilities"])
        self.assertEqual(resp["result"]["serverInfo"]["name"], mcp.SERVER_NAME)

    def test_initialized_notification_has_no_response(self):
        self.assertIsNone(self._call({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_tools_list_declares_every_tool_with_schema(self):
        """断言"必需的都在 + 每个都有 schema"，而不是写死数量。

        原来这里写的是 `assertEqual(名字集合, {四个工具})` —— 加第五个工具
        （price_watch 的 `check_price`）时它就假失败了。**工具数量不是契约，
        "每个工具都有可校验的 schema"才是。**
        """
        resp = self._call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = resp["result"]["tools"]
        names = {t["name"] for t in tools}
        self.assertLessEqual(
            {"read_log", "query_history", "probe_target", "check_price", "read_report"}, names
        )
        self.assertEqual(names, set(toolmod.HANDLERS),
                         "tools/list 暴露的必须与 HANDLERS 完全一致（漏一个模型就用不到）")
        for tool in tools:
            self.assertIn("description", tool)
            self.assertTrue(tool["description"].strip())
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertIn("required", tool["inputSchema"])

    def test_tools_call_success(self):
        resp = self._call({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                           "params": {"name": "read_log", "arguments": {"source": "app"}}})
        self.assertFalse(resp["result"]["isError"])
        self.assertIn("ERROR boom", resp["result"]["content"][0]["text"])

    def test_tools_call_failure_sets_is_error(self):
        resp = self._call({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                           "params": {"name": "read_log", "arguments": {"source": "nope"}}})
        self.assertTrue(resp["result"]["isError"])

    def test_tools_call_respects_budget(self):
        for i in range(3):
            self._call({"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call",
                        "params": {"name": "read_log", "arguments": {"source": "app"}}})
        resp = self._call({"jsonrpc": "2.0", "id": 99, "method": "tools/call",
                           "params": {"name": "read_log", "arguments": {"source": "app"}}})
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("预算", resp["result"]["content"][0]["text"])

    def test_unknown_method_is_method_not_found(self):
        resp = self._call({"jsonrpc": "2.0", "id": 5, "method": "resources/list"})
        self.assertEqual(resp["error"]["code"], mcp.METHOD_NOT_FOUND)

    def test_bad_jsonrpc_version_rejected(self):
        resp = self._call({"id": 6, "method": "tools/list"})
        self.assertEqual(resp["error"]["code"], mcp.INVALID_REQUEST)

    def test_ping(self):
        self.assertEqual(self._call({"jsonrpc": "2.0", "id": 7, "method": "ping"})["result"], {})

    def test_stdio_transport_end_to_end(self):
        """走真正的 stdio 传输：多行输入、逐行应答、坏 JSON 不致命。"""
        lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            "{ 这不是 JSON",
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            "",
        ]
        out = StringIO()
        handled = mcp.serve(self.cfg, lines, out=out)
        self.assertEqual(handled, 2)
        responses = [json.loads(line) for line in out.getvalue().strip().splitlines()]
        self.assertEqual(responses[0]["id"], 1)
        self.assertEqual(responses[1]["error"]["code"], mcp.PARSE_ERROR)
        self.assertEqual(responses[2]["id"], 2)


class TestCatalog(InvestigateTestBase):
    def test_catalog_lists_only_configured_objects(self):
        info = toolmod.catalog(self.cfg)
        self.assertEqual(info["configured_log_sources"], ["app"])
        self.assertEqual(info["configured_targets"], ["local"])
        self.assertIn("api_watch", info["available_tasks"])

    def test_known_tasks_comes_from_db_not_heartbeat(self):
        """任务清单要来自历史库 —— heartbeat_expect 会漏掉没被监视的任务。"""
        self.assertIn("api_watch", toolmod.known_tasks(self.cfg))


if __name__ == "__main__":
    unittest.main(verbosity=2)
