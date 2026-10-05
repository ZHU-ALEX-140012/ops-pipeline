"""日志异常判定的单元测试（纯函数，不需要真实日志文件）。

重点覆盖两类最容易出错的地方：
1. **静默失败防护**：文件不在、文件为空、格式解析不出来 —— 都不能被当成"今天没异常"；
2. **冷启动**：基线窗口样本太少时，量级判定必须闭嘴，但"从未见过的新错误"仍要报。
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops.core import logparse  # noqa: E402
from ops.tasks import log_scan  # noqa: E402


def make_scan(**kwargs) -> log_scan.SourceScan:
    base = dict(
        name="app", path="data/logs/app.log", exists=True,
        total_lines=1000, parsed_lines=1000, unparsed_lines=0,
        current_lines=500, baseline_lines=500,
        oldest_ts=datetime.now().astimezone() - timedelta(hours=47),
        newest_ts=datetime.now().astimezone() - timedelta(minutes=1),
    )
    base.update(kwargs)
    return log_scan.SourceScan(**base)


def make_stat(template: str, level: str, current: int, baseline: int = 0,
              history_windows: int = 0, history_avg: float = 0.0,
              history_median: float = 0.0, first_seen: str | None = None) -> log_scan.TemplateStat:
    return log_scan.TemplateStat(
        template=template, level=level, current=current, baseline=baseline,
        history_windows=history_windows, history_avg=history_avg,
        history_median=history_median, first_seen=first_seen, sample="sample raw line",
    )


def hours_ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat()


def days_ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat()


TH = log_scan.LogThresholds()
NOW = datetime.now().astimezone()

DB_ERROR = "ERROR [db] connection failed host=<*> port=<*> retry=<*>"
CACHE_ERROR = "ERROR [cache] eviction failed shard=<*> reason=oom"
HTTP_INFO = "INFO [http] request completed path=<*> status=<*> ms=<*>"


class TestWindowSplitting(unittest.TestCase):
    def _line(self, hours_ago: float) -> logparse.ParsedLine:
        return logparse.parse_line(
            f"{NOW - timedelta(hours=hours_ago):%Y-%m-%d %H:%M:%S} INFO x y z"
        )

    def test_adjacent_windows(self):
        parsed = [self._line(1), self._line(23), self._line(25), self._line(30), self._line(50)]
        current, baseline = log_scan.split_windows(parsed, NOW, 24)
        self.assertEqual(len(current), 2)   # 1h、23h 落在当前窗口
        self.assertEqual(len(baseline), 2)  # 25h、30h 落在基线窗口
        # 50h 太老，两个窗口都不算

    def test_no_overlap(self):
        parsed = [self._line(h) for h in (1, 5, 25, 40)]
        current, baseline = log_scan.split_windows(parsed, NOW, 24)
        current_ids = {id(p) for p in current}
        self.assertFalse(current_ids & {id(p) for p in baseline})


class TestBuildStats(unittest.TestCase):
    def test_counts_per_window(self):
        def line(hours_ago: float) -> logparse.ParsedLine:
            return logparse.parse_line(
                f"{NOW - timedelta(hours=hours_ago):%Y-%m-%d %H:%M:%S} {DB_ERROR}"
            )

        current = [line(1), line(2)]
        baseline = [line(25)]
        stats = log_scan.build_stats(current, baseline)
        self.assertEqual(len(stats), 1)
        stat = next(iter(stats.values()))
        self.assertEqual(stat.current, 2)
        self.assertEqual(stat.baseline, 1)
        self.assertEqual(stat.level, "ERROR")


class TestSilentFailureGuards(unittest.TestCase):
    """这些是整条流水线最该守住的地方：没数据 ≠ 没异常。"""

    def test_missing_source_alerts(self):
        scan = make_scan(exists=False)
        anomalies, _, _ = log_scan.detect_anomalies(scan, NOW, TH)
        self.assertEqual([a.kind for a in anomalies], ["source_missing"])

    def test_empty_source_alerts(self):
        scan = make_scan(total_lines=0, parsed_lines=0, current_lines=0, baseline_lines=0)
        anomalies, _, _ = log_scan.detect_anomalies(scan, NOW, TH)
        self.assertEqual([a.kind for a in anomalies], ["source_empty"])

    def test_zero_lines_is_not_healthy(self):
        """0 条 = 失败，不是"没事"。"""
        scan = make_scan(total_lines=0, parsed_lines=0)
        anomalies, _, _ = log_scan.detect_anomalies(scan, NOW, TH)
        self.assertTrue(anomalies)

    def test_stale_log_alerts(self):
        scan = make_scan(newest_ts=NOW - timedelta(hours=10))
        anomalies, _, _ = log_scan.detect_anomalies(scan, NOW, TH)
        self.assertIn("log_stale", [a.kind for a in anomalies])

    def test_fresh_log_not_stale(self):
        scan = make_scan(newest_ts=NOW - timedelta(minutes=5))
        anomalies, _, _ = log_scan.detect_anomalies(scan, NOW, TH)
        self.assertNotIn("log_stale", [a.kind for a in anomalies])

    def test_low_parse_ratio_notices(self):
        scan = make_scan(total_lines=1000, parsed_lines=200, unparsed_lines=800)
        anomalies, _, _ = log_scan.detect_anomalies(scan, NOW, TH)
        self.assertIn("parse_ratio_low", [a.kind for a in anomalies])


class TestNewTemplateDetection(unittest.TestCase):
    def test_never_seen_error_alerts(self):
        scan = make_scan()
        scan.stats = {CACHE_ERROR: make_stat(CACHE_ERROR, "ERROR", 50)}
        anomalies, _, flagged = log_scan.detect_anomalies(scan, NOW, TH)
        kinds = [a.kind for a in anomalies]
        self.assertIn("new_error_template", kinds)
        self.assertIn(CACHE_ERROR, flagged)

    def test_below_min_count_is_silent(self):
        scan = make_scan()
        scan.stats = {CACHE_ERROR: make_stat(CACHE_ERROR, "ERROR", 1)}
        anomalies, _, _ = log_scan.detect_anomalies(scan, NOW, TH)
        self.assertNotIn("new_error_template", [a.kind for a in anomalies])

    def test_seen_in_history_is_not_new(self):
        """首次出现已超出宽限期才算"已知模板"，这能挡掉"每周出现一次"的噪声。"""
        scan = make_scan()
        scan.stats = {CACHE_ERROR: make_stat(CACHE_ERROR, "ERROR", 30, history_windows=3,
                                             first_seen=days_ago(10))}
        anomalies, notes, _ = log_scan.detect_anomalies(scan, NOW, TH)
        self.assertNotIn("new_error_template", [a.kind for a in anomalies])
        self.assertTrue(any("超出宽限期" in n for n in notes))

    def test_recently_first_seen_stays_new(self):
        """刚出现的模板在宽限期内每一轮都算"新" —— 否则首轮告警被抑制后就永远静默。"""
        scan = make_scan()
        scan.stats = {CACHE_ERROR: make_stat(CACHE_ERROR, "ERROR", 30, history_windows=3,
                                             first_seen=hours_ago(25))}
        anomalies, _, _ = log_scan.detect_anomalies(scan, NOW, TH)
        self.assertIn("new_error_template", [a.kind for a in anomalies])

    def test_sustained_spike_uses_previous_window_not_polluted_history(self):
        """量级参照值不能被"已经被抬高的历史"污染，否则持续故障会变成新的正常。

        这是实测踩到的坑：上一轮的突增量级被写进历史，第二轮 reference 被抬到
        与当前值相同，于是同样严重的故障被判为"正常"。
        """
        scan = make_scan()
        scan.stats = {
            DB_ERROR: make_stat(
                DB_ERROR, "ERROR", 8679, baseline=193,
                history_windows=2, history_avg=8680.0, history_median=8680.0,
            )
        }
        anomalies, _, _ = log_scan.detect_anomalies(scan, NOW, TH)
        spikes = [a for a in anomalies if a.kind == "count_spike"]
        self.assertEqual(len(spikes), 1, "被污染的历史不该压掉真实突增")
        self.assertIn("参照值 193", spikes[0].detail)

    def test_info_template_is_not_new_error(self):
        scan = make_scan()
        scan.stats = {HTTP_INFO: make_stat(HTTP_INFO, "INFO", 5000)}
        anomalies, _, _ = log_scan.detect_anomalies(scan, NOW, TH)
        self.assertNotIn("new_error_template", [a.kind for a in anomalies])


class TestSpikeDetection(unittest.TestCase):
    def test_error_spike_alerts(self):
        scan = make_scan()
        scan.stats = {DB_ERROR: make_stat(DB_ERROR, "ERROR", 500, baseline=20, history_windows=5,
                                          history_avg=18.0)}
        anomalies, _, _ = log_scan.detect_anomalies(scan, NOW, TH)
        spikes = [a for a in anomalies if a.kind == "count_spike"]
        self.assertEqual(len(spikes), 1)
        self.assertEqual(spikes[0].severity_hint, "alert")

    def test_info_spike_is_only_notice(self):
        """业务量上涨不是故障，所以只到 notice。"""
        scan = make_scan()
        scan.stats = {HTTP_INFO: make_stat(HTTP_INFO, "INFO", 5000, baseline=100,
                                           history_windows=5, history_avg=100.0)}
        anomalies, _, _ = log_scan.detect_anomalies(scan, NOW, TH)
        spikes = [a for a in anomalies if a.kind == "count_spike"]
        self.assertEqual(len(spikes), 1)
        self.assertEqual(spikes[0].severity_hint, "notice")

    def test_below_multiple_is_silent(self):
        scan = make_scan()
        scan.stats = {DB_ERROR: make_stat(DB_ERROR, "ERROR", 40, baseline=20, history_windows=5,
                                          history_avg=20.0)}
        anomalies, _, _ = log_scan.detect_anomalies(scan, NOW, TH)
        self.assertNotIn("count_spike", [a.kind for a in anomalies])

    def test_below_absolute_floor_is_silent(self):
        """基线 1 次、本轮 8 次是 8 倍，但绝对量太小，不该打扰人。"""
        scan = make_scan()
        scan.stats = {DB_ERROR: make_stat(DB_ERROR, "ERROR", 8, baseline=1, history_windows=5,
                                          history_avg=1.0)}
        anomalies, _, _ = log_scan.detect_anomalies(scan, NOW, TH)
        self.assertNotIn("count_spike", [a.kind for a in anomalies])


class TestColdStart(unittest.TestCase):
    def test_cold_start_suppresses_spike_but_not_new_error(self):
        scan = make_scan(baseline_lines=10, current_lines=500)
        scan.stats = {
            CACHE_ERROR: make_stat(CACHE_ERROR, "ERROR", 40),
            DB_ERROR: make_stat(DB_ERROR, "ERROR", 400, baseline=5),
        }
        anomalies, notes, _ = log_scan.detect_anomalies(scan, NOW, TH)
        kinds = [a.kind for a in anomalies]
        self.assertIn("new_error_template", kinds, "冷启动期也必须报新错误")
        self.assertNotIn("count_spike", kinds, "冷启动期不该做量级判定")
        self.assertTrue(any("冷启动" in n for n in notes))


class TestCleanScan(unittest.TestCase):
    def test_clean_scan_has_no_anomalies(self):
        scan = make_scan()
        scan.stats = {HTTP_INFO: make_stat(HTTP_INFO, "INFO", 500, baseline=480)}
        anomalies, notes, flagged = log_scan.detect_anomalies(scan, NOW, TH)
        self.assertEqual(anomalies, [])
        self.assertEqual(flagged, set())
        self.assertTrue(any("未发现异常" in n for n in notes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
