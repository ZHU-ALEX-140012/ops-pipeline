"""评测脚手架的测试。

评测工具本身也是代码，也会错。最要命的一类错误是**悄悄算错分** ——
分数错了比没分数更糟，因为它会让人做出错误的判断。
所以这里重点锁三件事：

1. **夹具不能自相矛盾**（自相矛盾的输入会让模型"答错"，那是题目有问题）
2. **打分口径正确**（混淆矩阵、分母为 0 的处理、关键词判定的方向）
3. **黄金集不能被悄悄改小**（少了场景 = 分数变好看）
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals import fixtures  # noqa: E402
from tools import eval_suite  # noqa: E402


class TestFixturesAreSelfConsistent(unittest.TestCase):
    def test_every_scenario_builds_and_validates(self):
        for spec in fixtures.SCENARIOS:
            with self.subTest(scenario=spec["name"]):
                doc = fixtures.BUILDERS[spec["task"]](spec)
                self.assertEqual(fixtures.validate_run_doc(doc), [])

    def test_validator_actually_catches_inconsistency(self):
        """校验器必须真的会报错，而不是永远返回空列表。"""
        spec = fixtures.SCENARIOS[0]
        doc = fixtures.BUILDERS[spec["task"]](spec)
        doc["metrics"]["targets"] = 999
        self.assertTrue(fixtures.validate_run_doc(doc))

    def test_validator_catches_anomaly_count_mismatch(self):
        spec = next(s for s in fixtures.SCENARIOS if s["name"] == "aw_02_single_target_down")
        doc = fixtures.BUILDERS[spec["task"]](spec)
        doc["metrics"]["anomalies"] = 0
        self.assertTrue(fixtures.validate_run_doc(doc))

    def test_log_scan_targets_counts_sources_not_templates(self):
        """run@1 契约在 log_scan 上不一致：targets 按日志源计、samples 按模板计。

        这是写校验器时发现的真实差异。锁住它，免得以后有人"顺手统一"而弄坏夹具。
        """
        spec = next(s for s in fixtures.SCENARIOS if s["name"] == "ls_01_clean")
        doc = fixtures.BUILDERS["log_scan"](spec)
        self.assertEqual(doc["metrics"]["targets"], 1)
        self.assertEqual(len(doc["samples"]), 8)


class TestGoldenSetHygiene(unittest.TestCase):
    """黄金集的"题目卫生" —— 题目退化了，分数就失去意义。"""

    def setUp(self):
        self.fixtures = eval_suite.load_fixtures()

    def test_frozen_files_match_scenarios(self):
        """冻结夹具必须与 SCENARIOS 一致（改了题目就要重新生成）。"""
        self.assertEqual(__import__("evals.build", fromlist=["build"]).build(write=False), [])

    def test_scenario_count_does_not_shrink(self):
        """场景数只能增不能减 —— 悄悄删掉难题目是最容易发生的作弊。"""
        self.assertGreaterEqual(len(self.fixtures), 19)

    def test_has_both_positive_and_negative_cases(self):
        """必须同时有"该告警"和"不该告警"的题目。

        只有正向题的黄金集，一个永远返回 alert 的模型能拿满分。
        """
        positives = [f for f in self.fixtures if f["expect"]["should_alert"]]
        negatives = [f for f in self.fixtures if not f["expect"]["should_alert"]]
        self.assertGreaterEqual(len(positives), 8)
        self.assertGreaterEqual(len(negatives), 5)

    def test_every_scenario_has_a_note(self):
        for fixture in self.fixtures:
            with self.subTest(scenario=fixture["name"]):
                self.assertTrue(fixture.get("note"), "场景必须说明它在考什么")

    def test_expectations_are_wellformed(self):
        for fixture in self.fixtures:
            expect = fixture["expect"]
            with self.subTest(scenario=fixture["name"]):
                self.assertIn("should_alert", expect)
                self.assertIsInstance(expect["should_alert"], bool)
                self.assertTrue(expect.get("severity_any_of"), "必须给出可接受的级别范围")
                for key in ("mention_any", "mention_none"):
                    if key in expect:
                        self.assertIsInstance(expect[key], list)

    def test_has_a_rule_layer_false_positive_case(self):
        """必须保留至少一个「规则层误报、正确答案是不告警」的题型。

        这是唯一能区分"AI 有没有独立思考"的题型 —— 只会复述规则层的模型
        在这题上必错。删掉它，评测就退化成"规则层的镜像"。
        """
        discriminating = [
            f for f in self.fixtures
            if not f["expect"]["should_alert"] and (f["run_doc"].get("anomalies") or [])
        ]
        self.assertGreaterEqual(len(discriminating), 1,
                                "缺少「规则层误报」题型，评测将无法体现判断层的独立价值")


class TestUsageAggregation(unittest.TestCase):
    """token 统计必须真的累加得上。

    这里踩过一个真实 bug：usage 字典的键是 `input_tokens`，而累加表用的是短名 `in`，
    映射写错之后 17 次真实调用统计出 **0 成本** —— 报告上看着"零消耗"，
    实际上是统计失效。成本数据出错比没有成本数据更糟。
    """

    def test_accumulates_real_usage_shape(self):
        total = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
        eval_suite.collect_usage(total, {
            "input_tokens": 2294, "output_tokens": 966,
            "cache_read_tokens": 5760, "cache_write_tokens": 0,
            "session_total_tokens": 9020,
        })
        self.assertEqual(total["in"], 2294)
        self.assertEqual(total["out"], 966)
        self.assertEqual(total["cache_read"], 5760)

    def test_accumulates_across_calls(self):
        total = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
        for _ in range(3):
            eval_suite.collect_usage(total, {"input_tokens": 100, "output_tokens": 10})
        self.assertEqual(total["in"], 300)
        self.assertEqual(total["out"], 30)

    def test_ignores_unknown_and_bad_values(self):
        total = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
        eval_suite.collect_usage(total, {"input_tokens": "一百", "some_new_field": 5})
        eval_suite.collect_usage(total, None)
        self.assertEqual(total["in"], 0)

    def test_cumulative_total_is_not_added(self):
        """`session_total_tokens` 是会话累计值，累加它会把成本算爆。"""
        total = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
        eval_suite.collect_usage(total, {"session_total_tokens": 99999})
        self.assertEqual(sum(total.values()), 0)


class TestScoring(unittest.TestCase):
    def _verdict(self, **overrides):
        base = {
            "severity": "alert", "should_alert": True, "headline": "有问题",
            "plain_summary": "连续失败", "root_cause_hypothesis": "上游挂了",
        }
        base.update(overrides)
        return base

    def test_correct_alert(self):
        result = eval_suite.score(self._verdict(), {"should_alert": True, "severity_any_of": ["alert"]})
        self.assertTrue(result["alert_ok"])
        self.assertTrue(result["severity_ok"])

    def test_false_positive_detected(self):
        result = eval_suite.score(self._verdict(), {"should_alert": False})
        self.assertFalse(result["alert_ok"])

    def test_missing_verdict_is_a_failure_not_a_pass(self):
        """判断层挂了必须算错，不能悄悄算过。"""
        result = eval_suite.score(None, {"should_alert": True})
        self.assertFalse(result["ok"])
        self.assertFalse(result["alert_ok"])
        self.assertIn("verdict", result["error"])

    def test_keywords_checked_across_all_plain_fields(self):
        """根因写在 headline 里也该算对 —— 只看 root_cause_hypothesis 不公平。"""
        verdict = self._verdict(root_cause_hypothesis="不确定", headline="worldtime 连不上")
        result = eval_suite.score(verdict, {"should_alert": True, "mention_any": ["worldtime"]})
        self.assertTrue(result["root_ok"])

    def test_forbidden_keyword_fails_the_case(self):
        verdict = self._verdict(plain_summary="本机断网，全部目标失败")
        result = eval_suite.score(
            verdict, {"should_alert": True, "mention_none": ["本机断网"]}
        )
        self.assertFalse(result["root_ok"])

    def test_keyword_match_is_case_insensitive(self):
        verdict = self._verdict(root_cause_hypothesis="OOM evicting keys")
        result = eval_suite.score(verdict, {"should_alert": True, "mention_any": ["oom"]})
        self.assertTrue(result["root_ok"])


class TestAggregate(unittest.TestCase):
    def _row(self, predicted: bool, expected: bool, **extra):
        row = {"predicted": predicted, "expected": expected, "ok": True,
               "alert_ok": predicted == expected, "severity_ok": None, "root_ok": None}
        row.update(extra)
        return row

    def test_confusion_matrix(self):
        rows = [self._row(True, True), self._row(True, False),
                self._row(False, True), self._row(False, False)]
        metrics = eval_suite.aggregate(rows)
        self.assertEqual(
            (metrics["alert"]["tp"], metrics["alert"]["fp"],
             metrics["alert"]["fn"], metrics["alert"]["tn"]), (1, 1, 1, 1)
        )

    def test_zero_denominator_is_none_not_perfect(self):
        """没有正预测时精确率是**未定义**的，报成 100% 会骗人。"""
        metrics = eval_suite.aggregate([self._row(False, False)])
        self.assertIsNone(metrics["alert"]["precision"])
        self.assertIsNone(metrics["alert"]["recall"])

    def test_perfect_run(self):
        metrics = eval_suite.aggregate([self._row(True, True), self._row(False, False)])
        self.assertEqual(metrics["alert"]["precision"], 1.0)
        self.assertEqual(metrics["alert"]["recall"], 1.0)
        self.assertEqual(metrics["alert"]["f1"], 1.0)

    def test_errors_counted(self):
        rows = [self._row(True, True), {"predicted": None, "expected": True, "ok": False,
                                        "alert_ok": False, "severity_ok": None, "root_ok": None}]
        self.assertEqual(eval_suite.aggregate(rows)["errors"], 1)


class TestRuleArm(unittest.TestCase):
    def test_rule_arm_produces_a_verdict(self):
        fixture = eval_suite.load_fixtures(only="aw_02")[0]
        result = eval_suite.run_rule_arm(fixture)
        self.assertTrue(result["verdict"]["should_alert"])
        self.assertEqual(result["usage"]["input_tokens"], 0, "规则层不该有 token 消耗")

    def test_rule_arm_is_deterministic(self):
        fixture = eval_suite.load_fixtures(only="ls_01")[0]
        first = eval_suite.run_rule_arm(fixture)["verdict"]
        second = eval_suite.run_rule_arm(fixture)["verdict"]
        self.assertEqual(first, second)


class TestCompare(unittest.TestCase):
    """回归检测：改完提示词，分数是涨了还是跌了、哪些场景翻了。"""

    def _run(self, name: str, alert_ok: bool, precision: float) -> dict:
        return {
            "started_at": name,
            "metrics": {
                "rule": {"alert": {"precision": precision, "recall": 1.0},
                         "severity_accuracy": 1.0, "root_accuracy": 0.5},
                "ai": {"alert": {"precision": precision, "recall": 1.0},
                       "severity_accuracy": 1.0, "root_accuracy": 0.5},
            },
            "scenarios": [{
                "name": "s1", "expect": {"should_alert": True},
                "ai": {"score": {"alert_ok": alert_ok}},
            }],
        }

    def test_reports_flipped_scenarios(self):
        text = eval_suite.compare(self._run("after", True, 1.0), self._run("before", False, 0.9))
        self.assertIn("答错→答对", text)

    def test_reports_no_flips(self):
        text = eval_suite.compare(self._run("after", True, 1.0), self._run("before", True, 1.0))
        self.assertIn("无。", text)

    def test_delta_rendered(self):
        text = eval_suite.compare(self._run("after", True, 1.0), self._run("before", True, 0.9))
        self.assertIn("+10.0 pp", text)


class TestPairedArms(unittest.TestCase):
    """配对比较：同一批夹具、同一次运行里跑「无工具」和「有工具」。

    这里锁的是**对比的公平性** —— 两边子集不同、重复次数不同，
    差值就是假的，而假的差值比没有差值更糟。
    """

    def _scenario(self, name, *, predicted, expected, skipped=False, tool_calls=0):
        def arm(ok):
            return {"skipped": False, "tool_calls": tool_calls, "stable": True, "pass_count": 1,
                    "repeat": 1,
                    "runs": [{"predicted": predicted, "expected": expected, "ok": True,
                              "alert_ok": ok, "severity_ok": True, "root_ok": True}],
                    "score": {"predicted": predicted, "expected": expected, "ok": True,
                              "alert_ok": ok, "severity_ok": True, "root_ok": True}}

        return {
            "name": name, "task": "api_watch", "note": "n",
            "expect": {"should_alert": expected},
            "rule": {"score": {"predicted": expected, "expected": expected, "ok": True,
                               "alert_ok": True, "severity_ok": True, "root_ok": True}},
            "ai": ({"skipped": True, "runs": [], "tool_calls": 0,
                    "score": {"predicted": None, "expected": expected, "ok": False,
                              "alert_ok": False, "severity_ok": None, "root_ok": None}}
                   if skipped else arm(predicted == expected)),
            "ai_tools": None if skipped else arm(predicted == expected),
        }

    def test_skipped_scenarios_excluded_from_arm(self):
        scenarios = [self._scenario("a", predicted=True, expected=True),
                     self._scenario("b", predicted=False, expected=True, skipped=True)]
        metrics = eval_suite.arm_metrics(
            scenarios, "ai", 1, {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}, [])
        self.assertEqual(metrics["n"], 1)

    def test_rule_subset_matches_ai_subset(self):
        """对照臂必须落在同一子集上 —— 否则两边 n 不同，差值没有意义。"""
        scenarios = [self._scenario("a", predicted=True, expected=True),
                     self._scenario("b", predicted=False, expected=True, skipped=True)]
        self.assertEqual(eval_suite.rule_subset(scenarios, "ai", 1)["n"], 1)

    def test_rule_subset_replicates_for_repeat(self):
        """规则层是确定性的，重复 N 次等价于复制 N 份 —— 这样两边 n 才相等。"""
        scenarios = [self._scenario("a", predicted=True, expected=True)]
        self.assertEqual(eval_suite.rule_subset(scenarios, "ai", 3)["n"], 3)

    def test_tool_calls_aggregated(self):
        scenarios = [self._scenario("a", predicted=True, expected=True, tool_calls=4),
                     self._scenario("b", predicted=True, expected=True, tool_calls=0)]
        metrics = eval_suite.arm_metrics(
            scenarios, "ai_tools", 1, {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}, [])
        self.assertEqual(metrics["tool_calls"], 4)
        self.assertEqual(metrics["scenarios_with_tools"], 1)



class TestMultiArmReport(unittest.TestCase):
    """报告要能表达任意多臂。臂结构从 `ai`/`ai_tools` 泛化成 `arms` 列表，
    因为"哪个模型 × 有没有工具"是二维的，写死的两列装不下。"""

    def _arm(self, ok: bool = True, tools: int = 0, duration: float = 12.3):
        return {"skipped": False, "tool_calls": tools, "stable": True, "pass_count": 1,
                "repeat": 1,
                "tokens": {"in": 10, "out": 5, "cache_read": 0, "cache_write": 0},
                "avg_duration_s": duration,
                "score": {"predicted": True, "expected": True, "ok": True,
                          "alert_ok": ok, "severity_ok": True, "root_ok": True}}

    def _metrics(self, n: int, precision: float, tokens_in: int = 10):
        return {"n": n, "alert": {"precision": precision, "recall": 1.0, "f1": 0.8,
                                  "accuracy": 0.8, "tp": 1, "fp": 1, "fn": 0, "tn": 0},
                "severity_accuracy": 1.0, "severity_n": n,
                "root_accuracy": 0.5, "root_n": n, "errors": 0,
                "tokens": {"in": tokens_in, "out": 5, "cache_read": 0, "cache_write": 0},
                "avg_duration_s": 12.3, "tool_calls": 0, "scenarios_with_tools": 0,
                "stability": {"repeat": 1, "scenarios": 1, "unstable_count": 0, "unstable": []}}

    def _run(self, specs: list[tuple[str, str, int]], *, with_tools: bool = False) -> dict:
        """specs: [(key, label, tokens_in), ...]"""
        arms = [{"key": k, "label": lab, "model": k, "with_tools": with_tools}
                for k, lab, _ in specs]
        metrics = {
            "rule": self._metrics(1, 0.5),
            "rule_subset": self._metrics(1, 0.5),
            "arms": {k: self._metrics(1, 1.0, t) for k, _, t in specs},
            "rule_subsets": {k: self._metrics(1, 0.5) for k, _, _ in specs},
        }
        row = {"name": "s1", "task": "api_watch", "note": "note",
               "expect": {"should_alert": True},
               "rule": {"score": {"predicted": True, "expected": True, "ok": True,
                                  "alert_ok": True, "severity_ok": True, "root_ok": True}}}
        for k, _, _ in specs:
            row[k] = self._arm(tools=3 if with_tools else 0)
        return {"started_at": "T", "mode": "+".join(a["key"] for a in arms), "suite": "golden",
                "repeat": 1, "arms": arms, "scenarios": [row], "metrics": metrics}

    def test_single_arm_has_no_diff_section(self):
        text = eval_suite.render_markdown(self._run([("ai", "无工具", 10)]))
        self.assertIn("无工具", text)
        self.assertNotIn("相对基准臂的差值", text)

    def test_three_arms_all_appear(self):
        text = eval_suite.render_markdown(self._run(
            [("flash", "flash", 100), ("v4pro", "v4-pro", 200), ("route", "自动分流", 130)]))
        for label in ("flash", "v4-pro", "自动分流"):
            self.assertIn(label, text)
        self.assertIn("相对基准臂的差值", text)

    def test_routing_arm_gets_an_explanation(self):
        """分流臂是成本优化，报告必须说清楚该看什么 —— 否则读者会拿它比准确率。"""
        text = eval_suite.render_markdown(self._run(
            [("flash", "flash", 100), ("route", "自动分流", 130)]))
        self.assertIn("自动分流", text)
        self.assertIn("成本优化", text)

    def test_cost_table_lists_every_arm(self):
        text = eval_suite.render_markdown(self._run(
            [("flash", "flash", 100), ("v4pro", "v4-pro", 200)]))
        self.assertIn("## 成本", text)
        self.assertIn("200", text)

    def test_tools_arm_gets_the_fairness_caveat(self):
        """带工具臂不是 hermetic 的 —— 报告必须自己说出来，否则对比是误导。"""
        text = eval_suite.render_markdown(self._run([("ai", "无工具", 10)], with_tools=True))
        self.assertIn("不是完全 hermetic", text)
        self.assertIn("investigation", text)

    def test_skipped_scenarios_reported(self):
        run = self._run([("ai", "无工具", 10)])
        run["scenarios"][0]["ai"] = {"skipped": True, "reason": "heartbeat 不用判断层", "runs": []}
        run["metrics"]["arms"]["ai"] = None
        text = eval_suite.render_markdown(run)
        self.assertIn("判断层不适用的场景", text)

    def test_report_without_arms_does_not_crash(self):
        """`--rule-only` 时没有任何臂，报告仍要能出。"""
        run = self._run([("ai", "x", 10)])
        run["arms"] = []
        run["metrics"]["arms"] = {}
        run["metrics"]["rule_subsets"] = {}
        self.assertIn("判断层评测报告", eval_suite.render_markdown(run))


class TestReportRendering(unittest.TestCase):
    def test_console_output_is_ascii_only(self):
        """控制台输出必须纯 ASCII —— 中文 Windows 控制台是 GBK，会乱码，
        而这份输出经常被重定向进日志。"""
        metrics = eval_suite.aggregate([
            {"predicted": True, "expected": True, "ok": True, "alert_ok": True,
             "severity_ok": True, "root_ok": True},
        ])
        text = eval_suite.render_console("ai", metrics)
        text.encode("ascii")  # 抛异常即失败


if __name__ == "__main__":
    unittest.main(verbosity=2)
