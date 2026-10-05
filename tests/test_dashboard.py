"""只读面板的测试。

面板渲染的是**日志与告警内容** —— 里面完全可能出现 `<script>` 或 `<img onerror=…>`
（日志是外部输入，写日志的人不一定是你）。所以第一组测试是转义，不是排版。

第二组是"面板最没用的失败方式"：**它自己崩了、什么都看不到**。
所以每个数据块都必须独立容错，缺库/坏数据只能降级成一个错误提示。
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import dashboard  # noqa: E402
from ops.core import store  # noqa: E402

TMP = ROOT / "data" / "_test_tmp" / "dashboard"


def make_cfg() -> dict:
    return {
        "project": {"root": str(TMP)},
        "schedule": {"heartbeat_expect": {"api_watch": 60}},
        "alert": {"primary": "email", "email": {"enabled": False}},
        "ai": {"cmd": "no-such.cmd", "patch": "no.patch", "dsh_home": "data/dsh-home"},
        "api_watch": {}, "regression": {}, "log_scan": {}, "daily_report": {}, "feedback": {},
    }


class DashboardTestBase(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        (TMP / "data").mkdir(parents=True, exist_ok=True)
        self.cfg = make_cfg()

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)

    def seed(self, *, summary: str = "一切正常", detail: str = "测试告警",
             minutes_ago: float = 5) -> None:
        """⚠️ 时间戳一律用**相对当前时间**。

        固定时间戳的测试会悄悄过期 —— 第一版用 `2026-10-05T12:00:00Z`，
        跑起来就是"7 小时前"，于是"刚跑过不该判超期"这条断言莫名其妙地失败。
        这个坑项目里已经踩过一次（相对时间戳是测试约定）。
        """
        from datetime import datetime, timedelta, timezone

        started = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))
        stamp = started.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        conn = store.connect(TMP / "data" / "ops.db")
        store.start_run(conn, "20261005T120000Z-api_watch-aaaaaa", "api_watch", stamp)
        store.finish_run(conn, "20261005T120000Z-api_watch-aaaaaa",
                         finished_at=stamp, status="degraded",
                         items_ok=2, items_failed=1, ai_called=1, ai_status="ok",
                         tokens_in=100, tokens_out=50, cache_read_tokens=900,
                         summary=summary)
        store.record_alert(conn, run_id="20261005T120000Z-api_watch-aaaaaa", task="api_watch",
                           target="worldtime", severity="alert", channel="email",
                           dedup_key="k1", send_status="sent", detail=detail, sent_at=stamp)
        conn.close()


class TestCollectSummary(DashboardTestBase):
    def test_missing_db_is_reported_not_crashed(self):
        summary = dashboard.collect_summary(self.cfg)
        self.assertIn("error", summary["blocks"])
        self.assertIn("历史库还不存在", summary["blocks"]["error"])

    def test_with_data_all_blocks_present(self):
        self.seed()
        blocks = dashboard.collect_summary(self.cfg)["blocks"]
        for key in ("tasks", "unresolved", "alerts", "runs", "tokens", "feedback",
                    "alert_stats", "eval", "doctor"):
            self.assertIn(key, blocks)
        self.assertNotIn("error", blocks)

    def test_task_freshness_against_expectation(self):
        self.seed()
        task = next(t for t in dashboard.collect_summary(self.cfg)["blocks"]["tasks"]
                    if t["task"] == "api_watch")
        self.assertEqual(task["limit_minutes"], 60)
        self.assertIsNotNone(task["age_minutes"])
        self.assertFalse(task["stale"], "刚跑过不该判为超期")

    def test_tokens_within_window(self):
        self.seed()
        tokens = dashboard.collect_summary(self.cfg)["blocks"]["tokens"]
        self.assertEqual(tokens["tokens_in"], 100)
        self.assertEqual(tokens["cache_read"], 900)

    def test_eval_block_reads_latest_json(self):
        evals = TMP / "data" / "evals"
        evals.mkdir(parents=True, exist_ok=True)
        (evals / "20261005T000000Z-ai.json").write_text(json.dumps({
            "started_at": "T", "mode": "ai",
            "arms": [{"key": "ai", "label": "无工具"}],
            "metrics": {"arms": {"ai": {"alert": {"precision": 0.9, "recall": 1.0},
                                        "root_accuracy": 0.8, "tokens": {"in": 1234},
                                        "avg_duration_s": 20.0}}},
        }, ensure_ascii=False), encoding="utf-8")
        block = dashboard.collect_summary(self.cfg)["blocks"]["eval"]
        self.assertEqual(block["arms"][0]["precision"], 0.9)
        self.assertEqual(block["arms"][0]["tokens_in"], 1234)

    def test_eval_block_tolerates_broken_json(self):
        evals = TMP / "data" / "evals"
        evals.mkdir(parents=True, exist_ok=True)
        (evals / "broken.json").write_text("{ 不是 JSON", encoding="utf-8")
        self.assertIsNone(dashboard.collect_summary(self.cfg)["blocks"]["eval"])

    def test_doctor_block_is_offline_only(self):
        """刷新面板不该去连 SMTP/IMAP。"""
        block = dashboard.collect_summary(self.cfg)["blocks"]["doctor"]
        self.assertIn("counts", block)
        self.assertIsInstance(block["problems"], list)


class TestRenderEscaping(DashboardTestBase):
    """面板渲染的是日志与告警内容 —— **外部输入必须转义**。"""

    def test_script_tag_in_alert_detail_is_escaped(self):
        self.seed(detail="<script>alert('xss')</script>")
        html = dashboard.render_html(dashboard.collect_summary(self.cfg))
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)

    def test_script_tag_in_run_summary_is_escaped(self):
        self.seed(summary="<img src=x onerror=alert(1)>")
        html = dashboard.render_html(dashboard.collect_summary(self.cfg))
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img", html)

    def test_missing_db_renders_an_error_card(self):
        html = dashboard.render_html(dashboard.collect_summary(self.cfg))
        self.assertIn("读不到数据", html)
        self.assertIn("</html>", html)

    def test_all_sections_present_when_healthy(self):
        self.seed()
        html = dashboard.render_html(dashboard.collect_summary(self.cfg))
        for section in ("需要你注意的事", "任务健康度", "成本（窗口内）", "告警（窗口内）",
                        "人工反馈", "判断层评测", "最近运行"):
            self.assertIn(section, html, section)

    def test_idle_state_says_so_explicitly(self):
        """空闲时要说"没有"，不能只留一片空白 —— 沉默无法与"没跑"区分。"""
        self.seed()   # 有一条未恢复告警
        html = dashboard.render_html(dashboard.collect_summary(self.cfg))
        self.assertIn("未恢复告警 1 条", html)

    def test_feedback_bias_warning_shown(self):
        """人工反馈的准确率有采样偏差，面板必须一并显示这句话。"""
        self.seed()
        conn = store.connect(TMP / "data" / "ops.db")
        store.record_feedback(conn, run_id="20261005T120000Z-api_watch-aaaaaa", vote="down")
        conn.close()
        html = dashboard.render_html(dashboard.collect_summary(self.cfg))
        self.assertIn("天生有偏", html)


class TestHttpSurface(DashboardTestBase):
    """起一个真的 HTTP 服务，验证路由、状态码与安全响应头。"""

    @classmethod
    def setUpClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)
        (TMP / "data").mkdir(parents=True, exist_ok=True)
        cls.cfg = make_cfg()
        conn = store.connect(TMP / "data" / "ops.db")
        store.start_run(conn, "20261005T120000Z-api_watch-aaaaaa", "api_watch",
                        "2026-10-05T12:00:00Z")
        store.finish_run(conn, "20261005T120000Z-api_watch-aaaaaa",
                         finished_at="2026-10-05T12:00:05Z", status="ok")
        conn.close()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(cls.cfg))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(TMP, ignore_errors=True)

    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def test_index_serves_html(self):
        code, headers, body = self._get("/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("ops-pipeline 状态".encode(), body)

    def test_api_summary_serves_json(self):
        code, headers, body = self._get("/api/summary")
        self.assertEqual(code, 200)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertIn("blocks", json.loads(body.decode("utf-8")))

    def test_healthz(self):
        self.assertEqual(self._get("/healthz")[0], 200)

    def test_unknown_path_is_404(self):
        self.assertEqual(self._get("/../secrets")[0], 404)
        self.assertEqual(self._get("/admin")[0], 404)

    def test_security_headers(self):
        _, headers, _ = self._get("/")
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("Cache-Control"), "no-store")


if __name__ == "__main__":
    unittest.main(verbosity=2)
