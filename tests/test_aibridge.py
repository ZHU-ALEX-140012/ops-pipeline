"""AI 桥接层的解析逻辑测试。

这里锁住的是**从事件流里取 sessionId 与 token 用量**这段：
它的输入是外部格式（`dsh --json` 的 NDJSON），一旦对方改了字段名，
我们必须能察觉，而不是静默地记一堆 0。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops.core import aibridge  # noqa: E402


def event(**payload) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


class TestParseEvents(unittest.TestCase):
    def test_extracts_session_id(self):
        text = "\n".join([
            event(type="session", sessionId="session-ff7e036a-3bd3-45e9-986d-7a3d73e1fefb",
                  cwd="D:\\proj"),
            event(type="final", text="done"),
        ])
        session_id, _ = aibridge.parse_events(text)
        self.assertEqual(session_id, "session-ff7e036a-3bd3-45e9-986d-7a3d73e1fefb")

    def test_sums_per_call_tokens(self):
        """input/output 是**单次调用**的量，要累加。"""
        text = "\n".join([
            event(type="thinking", usage={"inputTokens": 100, "outputTokens": 10,
                                          "cacheReadTokens": 5, "cacheWriteTokens": 1,
                                          "totalTokens": 120}),
            event(type="thinking", usage={"inputTokens": 200, "outputTokens": 20,
                                          "cacheReadTokens": 7, "cacheWriteTokens": 2,
                                          "totalTokens": 349}),
        ])
        _, usage = aibridge.parse_events(text)
        self.assertEqual(usage.input_tokens, 300)
        self.assertEqual(usage.output_tokens, 30)
        self.assertEqual(usage.cache_read_tokens, 12)
        self.assertEqual(usage.cache_write_tokens, 3)

    def test_does_not_sum_cumulative_total(self):
        """`totalTokens` 是会话累计值 —— 把它加起来会重复计算。"""
        text = "\n".join([
            event(type="thinking", usage={"inputTokens": 1, "totalTokens": 1000}),
            event(type="thinking", usage={"inputTokens": 1, "totalTokens": 2000}),
            event(type="thinking", usage={"inputTokens": 1, "totalTokens": 3000}),
        ])
        _, usage = aibridge.parse_events(text)
        self.assertEqual(usage.session_total_tokens, 3000, "应取最后一个累计值")
        self.assertNotEqual(usage.input_tokens, 6000)

    def test_billable_in_includes_cache(self):
        usage = aibridge.RunUsage(input_tokens=10, cache_read_tokens=90, cache_write_tokens=0)
        self.assertEqual(usage.billable_in, 100)

    def test_ignores_malformed_lines(self):
        text = "\n".join([
            "不是 JSON 的一行",
            "{坏掉的 json",
            event(type="session", sessionId="session-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
            "",
            "[]",  # 合法 JSON 但不是对象
        ])
        session_id, usage = aibridge.parse_events(text)
        self.assertTrue(session_id.startswith("session-"))
        self.assertEqual(usage.input_tokens, 0)

    def test_missing_usage_is_zero_not_error(self):
        session_id, usage = aibridge.parse_events(event(type="final", text="ok"))
        self.assertIsNone(session_id)
        self.assertEqual(usage.to_dict()["input_tokens"], 0)

    def test_non_numeric_usage_values_are_ignored(self):
        text = event(type="thinking", usage={"inputTokens": "一百", "outputTokens": None})
        _, usage = aibridge.parse_events(text)
        self.assertEqual(usage.input_tokens, 0)
        self.assertEqual(usage.output_tokens, 0)


class TestValidateVerdict(unittest.TestCase):
    def _valid(self, **overrides) -> dict:
        payload = {
            "schema": "ops/verdict@1", "run_id": "r1", "severity": "alert",
            "should_alert": True, "headline": "有问题", "confidence": 0.8,
        }
        payload.update(overrides)
        return payload

    def test_valid_passes(self):
        self.assertEqual(aibridge.validate_verdict(self._valid(), "r1"), [])

    def test_missing_field(self):
        payload = self._valid()
        del payload["headline"]
        self.assertTrue(aibridge.validate_verdict(payload, "r1"))

    def test_bad_severity(self):
        errors = aibridge.validate_verdict(self._valid(severity="warning"), "r1")
        self.assertTrue(any("severity" in e for e in errors))

    def test_should_alert_must_be_bool(self):
        errors = aibridge.validate_verdict(self._valid(should_alert="yes"), "r1")
        self.assertTrue(any("布尔" in e for e in errors))

    def test_run_id_mismatch_flagged(self):
        errors = aibridge.validate_verdict(self._valid(run_id="other"), "r1")
        self.assertTrue(any("run_id" in e for e in errors))

    def test_non_dict(self):
        self.assertTrue(aibridge.validate_verdict(["nope"], "r1"))


class TestChildEnv(unittest.TestCase):
    """子进程环境：既要隔离，也要把"这一轮"的标识传下去。"""

    def _env(self, run_id=None):
        bridge = aibridge.AIBridge.__new__(aibridge.AIBridge)
        bridge.cfg = {"project": {"root": "."}, "ai": {"dsh_home": "data/dsh-home"}}
        bridge.dsh_home = Path("data/dsh-home")
        return bridge._child_env(run_id)

    def test_strips_inherited_dsh_vars(self):
        import os

        os.environ["DSH_SOMETHING"] = "leak"
        try:
            env = self._env()
        finally:
            os.environ.pop("DSH_SOMETHING", None)
        self.assertNotIn("DSH_SOMETHING", env)
        self.assertIn("DSH_HOME", env)

    def test_injects_run_id_for_tool_budget(self):
        """没有这个变量，判断层的工具预算就变成全机器共享 —— 等于没有预算。"""
        self.assertEqual(self._env("run-abc")["OPS_INVESTIGATE_RUN_ID"], "run-abc")

    def test_no_run_id_leaves_variable_absent(self):
        self.assertNotIn("OPS_INVESTIGATE_RUN_ID", self._env())


class TestParseToolCalls(unittest.TestCase):
    """宿主侧的工具调用计数 —— 唯一在任何环境下都准的口径。

    实测环境里判断层的沙箱 shell 只能读、状态文件写不进去，
    于是 CLI 自己的计数器落不了盘（第一版还把异常静默吞了），
    但事件流里明明白白记着 7 次调用。**拦不住 ≠ 看不见。**
    """

    def _events(self, *commands):
        import json

        lines = []
        for cmd in commands:
            lines.append(json.dumps({"type": "tool_call", "tool": "pwsh",
                                     "input": {"command": cmd}}))
            lines.append(json.dumps({"type": "tool_result", "status": "completed",
                                     "result": "ok"}))
        return "\n".join(lines)

    def test_counts_only_investigate_commands(self):
        text = self._events(
            "python tools/investigate.py catalog",
            "echo hello",
            "python tools/investigate.py probe --target worldtime",
        )
        result = aibridge.parse_tool_calls(text)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["investigate"], 2)
        self.assertEqual(len(result["commands"]), 2)

    def test_no_tool_calls_is_zero(self):
        result = aibridge.parse_tool_calls('{"type":"final","text":"done"}')
        self.assertEqual(result["investigate"], 0)
        self.assertEqual(result["commands"], [])

    def test_malformed_lines_ignored(self):
        text = "不是 JSON\n" + self._events("python tools/investigate.py catalog")
        self.assertEqual(aibridge.parse_tool_calls(text)["investigate"], 1)

    def test_real_event_shape_without_command(self):
        """有些工具（比如 glob）没有 command 字段，不能因此崩掉。"""
        import json

        text = json.dumps({"type": "tool_call", "tool": "glob",
                           "input": {"pattern": "data/runs/*"}})
        self.assertEqual(aibridge.parse_tool_calls(text)["total"], 1)


class TestDiagnose(unittest.TestCase):
    def test_missing_credential_hint(self):
        hint = aibridge.diagnose("MISSING_CREDENTIAL: llm-deepseek: no API key")
        self.assertIsNotNone(hint)
        self.assertIn("凭据", hint)

    def test_timeout_hint(self):
        hint = aibridge.diagnose("TIMEOUT after 60s", timeout_s=60)
        self.assertIn("超时", hint)

    def test_unknown_error_returns_none(self):
        self.assertIsNone(aibridge.diagnose("some totally unrelated failure"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
