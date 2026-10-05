"""小模型分流的测试。

分流是**成本优化**，代价是潜在准确率。所以这里的重点不是"它选了哪个模型"
而是三条更重要的性质：

1. **拿不准就升级**：多股证据、解释型异常一律走强模型 —— 宁可多花钱；
2. **显式配置优先**：`ai.model` 写死时必须绕过分流（评测要靠它分别跑两臂）；
3. **失败不放大**：patch 文件写不进去时返回 None，退化为"用 profile 默认模型"，
   **绝不能**让判断层整体失败。
"""

from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ops.core import judge, routing  # noqa: E402

TMP = ROOT / "data" / "_test_tmp" / "routing"


def base_cfg(**routing_overrides) -> dict:
    conf = {"enabled": True, "cheap_model": "cheap-x", "strong_model": "strong-y",
            "escalate_min_anomalies": 2, "escalate_min_failed": 2,
            "escalate_kinds": list(routing.INTERPRETIVE_KINDS), "always_strong_tasks": []}
    conf.update(routing_overrides)
    return {
        "project": {"root": str(TMP)},
        "ai": {"model": "auto", "provider": "deepseek-account", "routing": conf},
    }


def analysis(*kinds, failed=0, targets=3):
    anomalies = [judge.Anomaly(k, f"t{i}", "detail", "alert") for i, k in enumerate(kinds)]
    return judge.Analysis(metrics={"targets": targets, "ok": targets - failed, "failed": failed},
                          samples=[], baseline={}, anomalies=anomalies, notes=[])


class TestDecideModel(unittest.TestCase):
    def test_no_anomalies_uses_cheap(self):
        choice = routing.decide_model(base_cfg(), analysis())
        self.assertEqual(choice.model, "cheap-x")
        self.assertEqual(choice.tier, routing.CHEAP)

    def test_single_mechanical_anomaly_uses_cheap(self):
        """机械型异常：事实即结论，不需要权衡。"""
        choice = routing.decide_model(base_cfg(), analysis("log_stale"))
        self.assertEqual(choice.model, "cheap-x")

    def test_interpretive_kind_escalates(self):
        """解释型异常一律升级 —— 这是分流最关键的判断。"""
        for kind in ("structure_change", "latency_regression", "new_error_template",
                     "count_spike", "parse_ratio_low"):
            with self.subTest(kind=kind):
                choice = routing.decide_model(base_cfg(), analysis(kind))
                self.assertEqual(choice.model, "strong-y", kind)
                self.assertEqual(choice.tier, routing.STRONG)

    def test_multiple_anomalies_escalate(self):
        choice = routing.decide_model(base_cfg(), analysis("log_stale", "stale_run"))
        self.assertEqual(choice.model, "strong-y")

    def test_many_failed_targets_escalate(self):
        """多个目标失败可能同源也可能各自独立，需要合并判断。"""
        choice = routing.decide_model(base_cfg(), analysis("consecutive_failure", failed=2))
        self.assertEqual(choice.model, "strong-y")

    def test_always_strong_task(self):
        cfg = base_cfg(always_strong_tasks=["daily_report"])
        self.assertEqual(routing.decide_model(cfg, analysis(), task="daily_report").model,
                         "strong-y")
        self.assertEqual(routing.decide_model(cfg, analysis(), task="api_watch").model,
                         "cheap-x")

    def test_pinned_model_beats_routing(self):
        """评测要分别跑"全便宜"和"全强"两臂，所以必须有一个绕过分流的开关。"""
        cfg = base_cfg()
        cfg["ai"]["model"] = "pinned-model"
        choice = routing.decide_model(cfg, analysis("structure_change"))
        self.assertEqual(choice.model, "pinned-model")
        self.assertEqual(choice.tier, "pinned")

    def test_auto_means_routing(self):
        cfg = base_cfg()
        cfg["ai"]["model"] = "auto"
        self.assertEqual(routing.decide_model(cfg, analysis()).tier, routing.CHEAP)

    def test_disabled_routing_always_cheap(self):
        choice = routing.decide_model(base_cfg(enabled=False), analysis("structure_change"))
        self.assertEqual(choice.model, "cheap-x")
        self.assertIn("已关闭", choice.reason)

    def test_reason_is_always_present(self):
        """每条决策都要能回答"为什么用这个模型" —— 落库、事后可审计。"""
        for ana in (analysis(), analysis("structure_change"), analysis("a", "b"),
                    analysis("consecutive_failure", failed=3)):
            with self.subTest(anomalies=len(ana.anomalies)):
                self.assertTrue(routing.decide_model(base_cfg(), ana).reason)

    def test_escalation_is_configurable(self):
        """先验是可调的 —— 配置项的存在就是为了让人拿评测去改它。"""
        cfg = base_cfg(escalate_min_anomalies=5)
        self.assertEqual(routing.decide_model(cfg, analysis("a", "b")).model, "cheap-x")

    def test_missing_metrics_does_not_crash(self):
        ana = judge.Analysis(metrics={}, samples=[], baseline={}, anomalies=[], notes=[])
        self.assertTrue(routing.decide_model(base_cfg(), ana).model)


class TestModelPatch(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        (TMP / "data" / "state").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)

    def test_patch_contains_model_and_provider(self):
        path = routing.ensure_model_patch(base_cfg(), "strong-y")
        self.assertIsNotNone(path)
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("model: strong-y", text)
        self.assertIn("provider: deepseek-account", text)
        self.assertIn("agent-default-model", text)

    def test_patch_is_reused_not_rewritten(self):
        """计划任务每小时跑一次，每次都改文件没有意义。"""
        first = routing.ensure_model_patch(base_cfg(), "strong-y")
        stamp = Path(first).stat().st_mtime_ns
        second = routing.ensure_model_patch(base_cfg(), "strong-y")
        self.assertEqual(first, second)
        self.assertEqual(Path(second).stat().st_mtime_ns, stamp)

    def test_only_valid_yaml_characters(self):
        path = routing.ensure_model_patch(base_cfg(), "weird/model:name")
        self.assertIsNotNone(path)
        self.assertNotIn(":", Path(path).name.rsplit(".", 1)[0].split("model-")[1])

    def test_write_failure_returns_none_and_does_not_raise(self):
        """写不进 patch 就退化为"用 profile 默认模型" —— 分流失败绝不能让判断层失败。"""
        state = TMP / "data" / "state"
        shutil.rmtree(state, ignore_errors=True)
        state.write_text("not a directory", encoding="utf-8")
        self.assertIsNone(routing.ensure_model_patch(base_cfg(), "strong-y"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
