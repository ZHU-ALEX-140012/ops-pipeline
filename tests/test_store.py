"""存储层测试：重点验证幂等、冷却、以及连续失败的**时间窗口**这三处最容易写错的地方。

注意：所有样本时间都用"相对现在"生成，不用硬编码时间戳。
硬编码的测试会随着真实时间流逝悄悄失效 —— 那是比没有测试更坏的情况。
"""

from __future__ import annotations

import shutil
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops.core import store  # noqa: E402

# 测试临时目录放在工作区内：一是避开沙箱对外部临时目录的写入限制，
# 二是 Windows 上 SQLite 的 -wal/-shm 文件在外部目录清理时容易遇到权限问题。
TEST_TMP_ROOT = Path(__file__).resolve().parents[1] / "data" / "_test_tmp"


def _utc_iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ago(**kwargs) -> str:
    return _utc_iso(datetime.now(timezone.utc) - timedelta(**kwargs))


def now() -> str:
    return _utc_iso(datetime.now(timezone.utc))


def probe(value: float, ok: bool, ts: str, text: str | None = None) -> dict:
    return {
        "target": "a", "metric": "probe", "value_num": value,
        "ok": 1 if ok else 0, "ts": ts, "value_text": text,
    }


class StoreTestBase(unittest.TestCase):
    def setUp(self):
        self.workdir = TEST_TMP_ROOT / self._testMethodName
        if self.workdir.exists():
            shutil.rmtree(self.workdir, ignore_errors=True)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.conn = store.connect(self.workdir / "test.db")
        self.run_id = "20261004T120000Z-api_watch"
        store.start_run(self.conn, self.run_id, "api_watch", ago(minutes=5))

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.workdir, ignore_errors=True)


class TestSamples(StoreTestBase):
    def test_insert_and_count(self):
        rows = [probe(100.0, True, ago(minutes=2)), probe(0.0, False, ago(minutes=1))]
        self.assertEqual(store.insert_samples(self.conn, self.run_id, rows), 2)
        self.assertEqual(store.failures_since(self.conn, "a", hours=24), 1)

    def test_consecutive_failures_counts_backwards(self):
        rows = [
            probe(1.0, False, ago(minutes=1)),
            probe(1.0, False, ago(minutes=2)),
            probe(1.0, True, ago(minutes=3)),   # 这一条成功，应该断开计数
            probe(1.0, False, ago(minutes=4)),
        ]
        store.insert_samples(self.conn, self.run_id, rows)
        # 最近两条连续失败，再往前是成功 → 应为 2，不能被最早那条失败"穿透"
        self.assertEqual(store.consecutive_failures(self.conn, "a"), 2)

    def test_streak_is_time_bounded(self):
        """时间窗口外的失败不能算进"连续失败"，否则会和 failures_24h 自相矛盾。

        这正是第一次真实 AI 运行报出来的问题：
        consecutive_failures=5 与 failures_24h=1 同时出现，结论无法自洽。
        """
        rows = [
            probe(1.0, False, ago(hours=1)),
            probe(1.0, False, ago(hours=30)),   # 窗口外
            probe(1.0, False, ago(hours=31)),   # 窗口外
        ]
        store.insert_samples(self.conn, self.run_id, rows)
        streak = store.failure_streak(self.conn, "a", max_age_hours=24)
        self.assertEqual(streak["count"], 1)
        # 两个指标必须一致，否则判断层会被误导
        self.assertEqual(store.failures_since(self.conn, "a", hours=24), 1)

    def test_streak_reports_time_span(self):
        """只说"连续 3 次"不够：3 次可能跨 3 分钟，也可能跨 3 小时。"""
        rows = [
            probe(1.0, False, now()),
            probe(1.0, False, ago(minutes=60)),
            probe(1.0, False, ago(minutes=120)),
        ]
        store.insert_samples(self.conn, self.run_id, rows)
        streak = store.failure_streak(self.conn, "a")
        self.assertEqual(streak["count"], 3)
        self.assertIsNotNone(streak["span_minutes"])
        self.assertAlmostEqual(streak["span_minutes"], 120, delta=5)

    def test_streak_ignores_other_metrics(self):
        rows = [
            probe(1.0, False, now()),
            {"target": "a", "metric": "heartbeat", "value_num": 1.0, "ok": 0, "ts": now()},
        ]
        store.insert_samples(self.conn, self.run_id, rows)
        self.assertEqual(store.failure_streak(self.conn, "a")["count"], 1)

    def test_baseline_only_counts_ok(self):
        rows = [probe(100.0, True, now()), probe(9999.0, False, now())]
        store.insert_samples(self.conn, self.run_id, rows)
        self.assertEqual(store.baseline_values(self.conn, "a", days=30), [100.0])

    def test_body_hash_history_excludes_failed(self):
        rows = [
            probe(1.0, True, now(), "h1"),
            probe(1.0, True, now(), "h1"),
            probe(1.0, False, now(), "h2"),
        ]
        store.insert_samples(self.conn, self.run_id, rows)
        self.assertEqual(store.body_hash_history(self.conn, "a"), ["h1", "h1"])


class TestAlertIdempotency(StoreTestBase):
    def _record(self, **kwargs):
        base = dict(
            run_id=self.run_id, task="api_watch", target="a", severity="alert",
            channel="wecom", dedup_key="api_watch:a:alert", send_status="sent",
        )
        base.update(kwargs)
        return store.record_alert(self.conn, **base)

    def test_same_run_same_key_is_written_once(self):
        self.assertTrue(self._record())
        self.assertFalse(self._record())  # 单轮幂等：同一轮重复写被忽略

    def test_different_run_same_key_is_allowed(self):
        self.assertTrue(self._record())
        other = "20261004T130000Z-api_watch"
        store.start_run(self.conn, other, "api_watch", ago(minutes=1))
        # 跨轮必须允许写入，否则冷却到期后永远发不出第二次告警
        self.assertTrue(self._record(run_id=other))

    def test_last_alert_at_only_counts_delivered(self):
        self._record(send_status="suppressed")
        self.assertIsNone(store.last_alert_at(self.conn, "api_watch:a:alert"))
        # 同一轮里 (dedup_key, run_id) 唯一，所以"发送成功"必须发生在另一轮
        other = "20261004T130000Z-api_watch"
        store.start_run(self.conn, other, "api_watch", ago(minutes=1))
        self._record(run_id=other, send_status="sent", sent_at=ago(minutes=30))
        self.assertEqual(store.last_alert_at(self.conn, "api_watch:a:alert"), ago(minutes=30))

    def test_dedup_key_isolated_per_target_and_severity(self):
        self.assertTrue(self._record(target="a", dedup_key="api_watch:a:alert"))
        self.assertTrue(self._record(target="b", dedup_key="api_watch:b:alert"))
        self.assertTrue(self._record(target="a", dedup_key="api_watch:a:critical"))

    def test_dry_run_counts_for_cooldown(self):
        """dry-run 必须像真发一样参与冷却，否则演练结果会误导人。"""
        self._record(send_status="dry-run", sent_at=ago(minutes=10))
        self.assertIsNotNone(store.last_alert_at(self.conn, "api_watch:a:alert"))

    def test_failed_send_does_not_count_for_cooldown(self):
        """发送失败要能在下一轮重试，不能被冷却挡住。"""
        self._record(send_status="failed", sent_at=ago(minutes=10))
        self.assertIsNone(store.last_alert_at(self.conn, "api_watch:a:alert"))


class TestRuns(StoreTestBase):
    def test_finish_run_updates_only_allowed_fields(self):
        store.finish_run(
            self.conn, self.run_id, status="degraded", items_ok=3, items_failed=1,
            ai_status="ok", summary="ok", bogus_field="ignored",
        )
        runs = store.recent_runs(self.conn, "api_watch")
        self.assertEqual(runs[0]["status"], "degraded")
        self.assertEqual(runs[0]["items_ok"], 3)

    def test_last_run_at(self):
        self.assertEqual(store.last_run_at(self.conn, "api_watch"), ago(minutes=5))
        self.assertIsNone(store.last_run_at(self.conn, "nope"))

    def test_ai_calls_today(self):
        self.assertEqual(store.ai_calls_today(self.conn), 0)
        store.finish_run(self.conn, self.run_id, ai_called=1)
        self.assertEqual(store.ai_calls_today(self.conn), 1)

    def test_ai_calls_today_excludes_yesterday(self):
        old_run = "20261001T120000Z-api_watch"
        store.start_run(self.conn, old_run, "api_watch", ago(days=2))
        store.finish_run(self.conn, old_run, ai_called=1)
        self.assertEqual(store.ai_calls_today(self.conn), 0)

    def test_verdict_roundtrip(self):
        store.record_verdict(self.conn, {
            "run_id": self.run_id, "severity": "alert", "should_alert": True,
            "headline": "有问题", "root_cause_hypothesis": "上游故障",
            "evidence": ["e1"], "confidence": 0.8,
        }, model="headless")
        row = self.conn.execute("SELECT * FROM verdict WHERE run_id=?", (self.run_id,)).fetchone()
        self.assertEqual(row["severity"], "alert")
        self.assertEqual(row["should_alert"], 1)
        self.assertEqual(row["model"], "headless")


if __name__ == "__main__":
    unittest.main(verbosity=2)
