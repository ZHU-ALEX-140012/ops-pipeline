"""多视角诊断的测试。

这里的重点是三条**防止自欺**的性质：

1. **不该分镜头时就不分**：机械型异常（日志源不见了、心跳发现任务没跑）事实即结论，
   派三个子代理去确认同一件事纯属烧钱；
2. **块里必须写明降级路径**：派不出子代理时要照常给结论并注明，
   **不能让没发生过的编排出现在报告里**；
3. **提示词必须真的描述了这套契约**：块是数据、提示词是规则，两边缺一不可。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ops.core import judge, perspectives  # noqa: E402

PROMPT_DIR = ROOT / "ops" / "prompts"


def cfg_with(**overrides) -> dict:
    conf = {"enabled": True, "max_subagents": 3, "trigger_min_anomalies": 2,
            "trigger_kinds": list(perspectives.LENSES and
                                  ("structure_change", "latency_regression",
                                   "new_error_template", "count_spike", "parse_ratio_low"))}
    conf.update(overrides)
    return {"ai": {"multi_perspective": conf}}


def analysis(*kinds, failed=0):
    anomalies = [judge.Anomaly(k, f"t{i}", "detail", "alert") for i, k in enumerate(kinds)]
    return judge.Analysis(metrics={"targets": 3, "ok": 3 - failed, "failed": failed},
                          samples=[], baseline={}, anomalies=anomalies, notes=[])


class TestShouldDispatch(unittest.TestCase):
    def test_disabled_never_dispatches(self):
        yes, reason = perspectives.should_dispatch(cfg_with(enabled=False),
                                                   analysis("structure_change"))
        self.assertFalse(yes)
        self.assertIn("未启用", reason)

    def test_no_anomalies_never_dispatches(self):
        self.assertFalse(perspectives.should_dispatch(cfg_with(), analysis())[0])

    def test_mechanical_anomaly_does_not_dispatch(self):
        """事实即结论的情形不该分镜头 —— 那是纯烧钱。"""
        for kind in ("log_stale", "empty_round", "source_missing", "stale_run"):
            with self.subTest(kind=kind):
                yes, reason = perspectives.should_dispatch(cfg_with(), analysis(kind))
                self.assertFalse(yes, kind)
                self.assertIn("事实即结论", reason)

    def test_interpretive_kinds_dispatch(self):
        for kind in ("structure_change", "latency_regression", "new_error_template",
                     "count_spike", "parse_ratio_low"):
            with self.subTest(kind=kind):
                self.assertTrue(perspectives.should_dispatch(cfg_with(), analysis(kind))[0], kind)

    def test_multiple_anomalies_dispatch(self):
        self.assertTrue(perspectives.should_dispatch(
            cfg_with(), analysis("log_stale", "stale_run"))[0])

    def test_reason_is_always_given(self):
        for ana in (analysis(), analysis("log_stale"), analysis("structure_change")):
            yes, reason = perspectives.should_dispatch(cfg_with(), ana)
            self.assertTrue(reason)


class TestContextBlock(unittest.TestCase):
    def test_block_shape(self):
        block = perspectives.context_block(cfg_with(), analysis("structure_change"))
        self.assertTrue(block["enabled"])
        self.assertEqual(len(block["lenses"]), 3)
        keys = {lens["key"] for lens in block["lenses"]}
        self.assertEqual(keys, {"contract", "logs", "history"})
        for lens in block["lenses"]:
            self.assertTrue(lens["question"])
            self.assertTrue(lens["evidence_hint"])

    def test_returns_none_when_not_needed(self):
        self.assertIsNone(perspectives.context_block(cfg_with(), analysis()))
        self.assertIsNone(perspectives.context_block(cfg_with(enabled=False),
                                                     analysis("structure_change")))

    def test_max_subagents_is_respected(self):
        block = perspectives.context_block(cfg_with(max_subagents=2), analysis("structure_change"))
        self.assertEqual(len(block["lenses"]), 2)
        self.assertEqual(block["max_subagents"], 2)

    def test_instructions_require_parallel_dispatch(self):
        """串行派发会让每个子代理都看不见别人的发现，也慢得多。"""
        text = " ".join(perspectives.context_block(cfg_with(),
                                                   analysis("structure_change"))["instructions"])
        self.assertIn("并行", text)

    def test_instructions_require_attribution(self):
        """汇总时必须标明每条结论来自哪个镜头，否则读的人无法判断可信度。"""
        text = " ".join(perspectives.context_block(cfg_with(),
                                                   analysis("structure_change"))["instructions"])
        self.assertIn("来自哪个镜头", text)

    def test_block_states_the_degradation_path(self):
        """**最重要的一条**：派不出子代理要照常给结论并注明，
        绝不能让没发生过的编排出现在报告里。"""
        block = perspectives.context_block(cfg_with(), analysis("structure_change"))
        note = block["degrade_note"]
        self.assertIn("单次判断", note)
        self.assertIn("未生效", note)

    def test_lens_questions_are_distinct(self):
        questions = [lens.question for lens in perspectives.LENSES]
        self.assertEqual(len(questions), len(set(questions)))


class TestPromptsDescribeTheContract(unittest.TestCase):
    """块是数据、提示词是规则 —— 只在数据里加字段而提示词不提，模型不会知道要做什么。"""

    def _text(self, name: str) -> str:
        return (PROMPT_DIR / name).read_text(encoding="utf-8")

    def test_prompts_mention_the_block(self):
        for name in ("api_watch.md", "log_scan.md"):
            with self.subTest(prompt=name):
                self.assertIn("multi_perspective", self._text(name))

    def test_prompts_state_the_degradation_rule(self):
        for name in ("api_watch.md", "log_scan.md"):
            with self.subTest(prompt=name):
                text = self._text(name)
                self.assertIn("多视角编排未生效", text)
                self.assertIn("绝不要把没发生过的编排写进报告", text)

    def test_prompts_require_attribution(self):
        for name in ("api_watch.md", "log_scan.md"):
            with self.subTest(prompt=name):
                self.assertIn("来自哪个镜头", self._text(name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
