"""判定层单元测试（只用标准库 unittest，零依赖）。

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops.core import judge  # noqa: E402


class FakeProbe:
    def __init__(self, target, ok=True, latency_ms=100.0, status=200, error=None, body="h1"):
        self.target = target
        self.url = f"http://example.invalid/{target}"
        self.ts = "2026-10-04T12:00:00Z"
        self.ok = ok
        self.status_code = status
        self.latency_ms = latency_ms
        self.error = error
        self.body_sha256 = body
        self.body_len = 10
        self.content_type = "application/json"

    def to_sample(self):
        return {
            "target": self.target, "metric": "probe", "value_num": self.latency_ms,
            "value_text": self.body_sha256, "ok": 1 if self.ok else 0, "ts": self.ts,
            "status_code": self.status_code, "error": self.error, "body_len": self.body_len,
            "content_type": self.content_type,
        }


class TestPercentile(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(judge.percentile([], 95), 0.0)

    def test_single(self):
        self.assertEqual(judge.percentile([42.0], 95), 42.0)

    def test_known_values(self):
        values = [float(n) for n in range(1, 101)]  # 1..100
        self.assertAlmostEqual(judge.percentile(values, 50), 50.5, places=3)
        self.assertAlmostEqual(judge.percentile(values, 95), 95.05, places=3)

    def test_bounds(self):
        self.assertEqual(judge.percentile([1.0, 2.0, 3.0], 0), 1.0)
        self.assertEqual(judge.percentile([1.0, 2.0, 3.0], 100), 3.0)


class TestModal(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(judge.modal([]), (None, 0))

    def test_picks_most_common(self):
        self.assertEqual(judge.modal(["a", "b", "a", "a", "b"]), ("a", 3))


class TestAnalyzeProbes(unittest.TestCase):
    thresholds = {"consecutive_failures": 3, "latency_p95_multiple": 3.0,
                  "cold_start_min_samples": 20}

    def _history(self, target, **kwargs):
        base = dict(target=target, samples=100, p50_ms=100.0, p95_ms=200.0,
                    consecutive_failures=0, failures_24h=0,
                    modal_body_hash="h1", body_hash_samples=50)
        base.update(kwargs)
        return judge.TargetHistory(**base)

    def test_all_healthy(self):
        results = [FakeProbe("a"), FakeProbe("b")]
        analysis = judge.analyze_probes(results, {"a": self._history("a"), "b": self._history("b")}, self.thresholds)
        self.assertEqual(analysis.metrics["ok"], 2)
        self.assertEqual(analysis.anomalies, [])
        self.assertEqual(analysis.metrics["p95_ms"], 100.0)

    def test_consecutive_failure_triggers_at_threshold(self):
        results = [FakeProbe("a", ok=False, status=500, error="HTTP 500")]
        history = {"a": self._history("a", consecutive_failures=3, streak_span_minutes=45.0)}
        analysis = judge.analyze_probes(results, history, self.thresholds)
        kinds = [a.kind for a in analysis.anomalies]
        self.assertIn("consecutive_failure", kinds)

    def test_streak_detail_reports_span_and_24h_count(self):
        """异常描述必须说清"跨了多久、24 小时内失败几次"，否则结论误导人。"""
        results = [FakeProbe("a", ok=False, status=500, error="HTTP 500")]
        history = {"a": self._history("a", consecutive_failures=5, streak_span_minutes=240.0,
                                      failures_24h=5)}
        analysis = judge.analyze_probes(results, history, self.thresholds)
        detail = analysis.anomalies[0].detail
        self.assertIn("跨度 240 分钟", detail)
        self.assertIn("24 小时内累计失败 5 次", detail)

    def test_baseline_exposes_streak_span(self):
        results = [FakeProbe("a")]
        history = {"a": self._history("a", streak_span_minutes=90.0)}
        analysis = judge.analyze_probes(results, history, self.thresholds)
        self.assertEqual(analysis.baseline["a"]["streak_span_minutes"], 90.0)

    def test_below_threshold_is_only_a_note(self):
        results = [FakeProbe("a", ok=False, status=500, error="HTTP 500")]
        history = {"a": self._history("a", consecutive_failures=1)}
        analysis = judge.analyze_probes(results, history, self.thresholds)
        self.assertEqual([a.kind for a in analysis.anomalies], [])
        self.assertTrue(any("未达阈值" in n for n in analysis.notes))

    def test_latency_regression(self):
        results = [FakeProbe("a", latency_ms=900.0)]
        analysis = judge.analyze_probes(results, {"a": self._history("a")}, self.thresholds)
        self.assertIn("latency_regression", [a.kind for a in analysis.anomalies])

    def test_structure_change(self):
        results = [FakeProbe("a", body="h2")]
        analysis = judge.analyze_probes(results, {"a": self._history("a")}, self.thresholds)
        self.assertIn("structure_change", [a.kind for a in analysis.anomalies])

    def test_empty_round_is_a_failure_not_ok(self):
        analysis = judge.analyze_probes([], {}, self.thresholds)
        self.assertEqual([a.kind for a in analysis.anomalies], ["empty_round"])
        self.assertEqual(analysis.metrics["targets"], 0)

    def test_cold_start_uses_absolute_only(self):
        results = [FakeProbe("a", latency_ms=900.0)]
        history = {"a": self._history("a", samples=2, p95_ms=200.0)}
        analysis = judge.analyze_probes(results, history, self.thresholds)
        # 冷启动期不做基线比较，只记备注
        self.assertEqual(analysis.anomalies, [])
        self.assertTrue(any("冷启动" in n for n in analysis.notes))


class TestQuietHours(unittest.TestCase):
    def test_non_wrapping(self):
        spec = "01:00-06:00"
        self.assertTrue(judge.in_quiet_hours(datetime(2026, 10, 4, 3, 0), spec))
        self.assertFalse(judge.in_quiet_hours(datetime(2026, 10, 4, 7, 0), spec))

    def test_wrapping_midnight(self):
        spec = "23:30-07:30"
        self.assertTrue(judge.in_quiet_hours(datetime(2026, 10, 4, 23, 45), spec))
        self.assertTrue(judge.in_quiet_hours(datetime(2026, 10, 4, 2, 0), spec))
        self.assertFalse(judge.in_quiet_hours(datetime(2026, 10, 4, 12, 0), spec))

    def test_empty_spec_disables(self):
        self.assertFalse(judge.in_quiet_hours(datetime(2026, 10, 4, 3, 0), ""))


class TestDecide(unittest.TestCase):
    now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)

    def _decide(self, **kwargs):
        base = dict(severity="alert", should_alert=True, confidence=0.9,
                    now_local=self.now, last_sent_at=None, cooldown_minutes=60,
                    quiet_hours="")
        base.update(kwargs)
        return judge.decide(**base)

    def test_sends_by_default(self):
        self.assertTrue(self._decide().send)

    def test_suppressed_by_verdict(self):
        decision = self._decide(should_alert=False)
        self.assertFalse(decision.send)
        self.assertEqual(decision.reason, "verdict_suppressed")

    def test_low_confidence_suppressed(self):
        decision = self._decide(confidence=0.3)
        self.assertFalse(decision.send)
        self.assertIn("low_confidence", decision.reason)

    def test_cooldown_suppressed(self):
        decision = self._decide(last_sent_at=self.now - timedelta(minutes=10))
        self.assertFalse(decision.send)
        self.assertIn("cooldown", decision.reason)

    def test_cooldown_expired_sends(self):
        decision = self._decide(last_sent_at=self.now - timedelta(minutes=90))
        self.assertTrue(decision.send)

    def test_quiet_hours_suppressed_for_alert(self):
        decision = self._decide(quiet_hours="23:30-07:30", now_local=datetime(2026, 10, 4, 2, 0, tzinfo=timezone.utc))
        self.assertFalse(decision.send)
        self.assertEqual(decision.reason, "quiet_hours")

    def test_critical_breaks_through_quiet_hours(self):
        decision = self._decide(severity="critical", quiet_hours="23:30-07:30",
                                now_local=datetime(2026, 10, 4, 2, 0, tzinfo=timezone.utc))
        self.assertTrue(decision.send)

    def test_info_never_sends(self):
        self.assertFalse(self._decide(severity="info").send)


class TestRuleVerdict(unittest.TestCase):
    def test_clean_run_is_info(self):
        analysis = judge.Analysis(metrics={"ok": 4, "targets": 4, "p95_ms": 120.0})
        verdict = judge.rule_verdict("r1", "api_watch", analysis)
        self.assertEqual(verdict["severity"], "info")
        self.assertFalse(verdict["should_alert"])
        self.assertTrue(verdict["ai_degraded"])
        self.assertIn("全部正常", verdict["headline"])

    def test_partial_failure_must_not_claim_all_healthy(self):
        """结论不能和明细表自相矛盾：有目标失败就不能说"全部正常"。"""
        analysis = judge.Analysis(metrics={"ok": 2, "targets": 3, "p95_ms": 120.0})
        verdict = judge.rule_verdict("r1", "api_watch", analysis)
        self.assertNotIn("全部正常", verdict["headline"])
        self.assertIn("1 个目标失败", verdict["headline"])
        self.assertEqual(verdict["severity"], "notice")
        # 未达阈值就不该打扰人
        self.assertFalse(verdict["should_alert"])

    def test_anomaly_run_alerts(self):
        analysis = judge.Analysis(
            metrics={"ok": 1, "targets": 2, "p95_ms": 120.0},
            anomalies=[judge.Anomaly("consecutive_failure", "a", "连续 3 次失败", "alert")],
        )
        verdict = judge.rule_verdict("r1", "api_watch", analysis)
        self.assertEqual(verdict["severity"], "alert")
        self.assertTrue(verdict["should_alert"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
