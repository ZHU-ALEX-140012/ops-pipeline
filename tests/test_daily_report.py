"""日报与恢复通知的测试。

这两块是设计方案里"让沉默变成明确信息"的部分：
* 日报：不管有没有异常都要有输出，否则"今天没事"永远只能靠猜；
* 恢复通知：故障好了也要说一声，否则你只知道它挂过。
"""

from __future__ import annotations

import shutil
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops.core import report, store  # noqa: E402
from ops.tasks import daily_report  # noqa: E402

TMP = Path(__file__).resolve().parents[1] / "data" / "_test_tmp" / "daily"


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


class DigestTestBase(unittest.TestCase):
    def setUp(self):
        if TMP.exists():
            shutil.rmtree(TMP, ignore_errors=True)
        TMP.mkdir(parents=True, exist_ok=True)
        self.conn = store.connect(TMP / "test.db")
        self.now = datetime.now().astimezone()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(TMP, ignore_errors=True)

    def add_run(self, task: str, *, status: str = "ok", hours_ago: float = 0,
                ai_called: int = 0, ai_status: str | None = None,
                tokens_in: int | None = None, tokens_out: int | None = None,
                cache_read: int | None = None) -> str:
        started = _iso(datetime.now(timezone.utc) - timedelta(hours=hours_ago))
        run_id = f"{task}-{started}-{len(store.recent_runs(self.conn, task, 100))}"
        store.start_run(self.conn, run_id, task, started)
        store.finish_run(
            self.conn, run_id, finished_at=started, status=status, items_ok=1, items_failed=0,
            ai_called=ai_called, ai_status=ai_status, tokens_in=tokens_in,
            tokens_out=tokens_out, cache_read_tokens=cache_read,
        )
        return run_id

    def add_alert(self, task: str, *, status: str = "sent", hours_ago: float = 0,
                  severity: str = "alert", target: str = "app", resolved: bool = False) -> None:
        sent = _iso(datetime.now(timezone.utc) - timedelta(hours=hours_ago))
        # dedup_key 必须逐条唯一：alert 表上有 UNIQUE(dedup_key, run_id)，
        # 用它来保证单轮幂等 —— 用例里要是撞了键，后面的记录会被静默忽略。
        self._alert_seq = getattr(self, "_alert_seq", 0) + 1
        unique = f"{task}:{target}:{severity}:{status}:{sent}:{self._alert_seq}"
        store.record_alert(
            self.conn, run_id=f"{task}-run-{self._alert_seq}", task=task, target=target,
            severity=severity, channel="email", dedup_key=unique,
            send_status=status, detail="测试", sent_at=sent,
            resolved_at=sent if resolved else None,
        )


class TestBuildDigest(DigestTestBase):
    def test_aggregates_runs_and_tokens(self):
        self.add_run("api_watch", status="ok", ai_called=1, ai_status="ok",
                     tokens_in=100, tokens_out=20, cache_read=500)
        self.add_run("api_watch", status="degraded", ai_called=1, ai_status="failed")
        self.add_run("log_scan", status="failed")

        digest = daily_report.build_digest(self.conn, hours=24, now=self.now)
        totals = digest["totals"]
        self.assertEqual(totals["runs"], 3)
        self.assertEqual(totals["ok_runs"], 1)
        self.assertEqual(totals["degraded_runs"], 1)
        self.assertEqual(totals["failed_runs"], 1)
        self.assertEqual(totals["ai_calls"], 2)
        self.assertEqual(totals["ai_failures"], 1)
        self.assertEqual(digest["tokens"]["input"], 100)
        self.assertEqual(digest["tokens"]["cache_read"], 500)

    def test_excludes_runs_outside_window(self):
        self.add_run("api_watch", hours_ago=1)
        self.add_run("api_watch", hours_ago=30)   # 窗口外
        digest = daily_report.build_digest(self.conn, hours=24, now=self.now)
        self.assertEqual(digest["totals"]["runs"], 1)

    def test_counts_alerts_by_status(self):
        self.add_alert("api_watch", status="sent")
        self.add_alert("api_watch", status="suppressed")
        self.add_alert("api_watch", status="dry-run")
        self.add_alert("api_watch", status="failed")
        digest = daily_report.build_digest(self.conn, hours=24, now=self.now)
        totals = digest["totals"]
        self.assertEqual(totals["alerts_sent"], 1)
        self.assertEqual(totals["alerts_suppressed"], 1)
        self.assertEqual(totals["alerts_dry_run"], 1)
        self.assertEqual(totals["alerts_failed"], 1)

    def test_no_runs_produces_explicit_note(self):
        """窗口内没有运行记录时，日报必须**明说**，而不是给一份空白。"""
        digest = daily_report.build_digest(self.conn, hours=24, now=self.now)
        self.assertEqual(digest["totals"]["runs"], 0)
        self.assertTrue(any("没有任何运行记录" in n for n in digest["notes"]))

    def test_failed_alerts_are_highlighted(self):
        self.add_alert("api_watch", status="failed")
        digest = daily_report.build_digest(self.conn, hours=24, now=self.now)
        self.assertTrue(any("发送失败" in n for n in digest["notes"]))

    def test_dry_run_alerts_are_explained_not_counted_as_unresolved(self):
        self.add_alert("api_watch", status="dry-run", hours_ago=1)
        digest = daily_report.build_digest(self.conn, hours=24, now=self.now)
        self.assertEqual(digest["totals"]["unresolved"], 0)
        self.assertTrue(any("通道未配置" in n for n in digest["notes"]))


class TestUnresolvedTracking(DigestTestBase):
    def test_only_sent_alerts_are_tracked(self):
        """只有真正通知过你的告警才谈得上"恢复"。"""
        self.add_alert("api_watch", status="sent", hours_ago=1)
        self.add_alert("api_watch", status="dry-run", hours_ago=2)
        self.add_alert("api_watch", status="failed", hours_ago=3)
        pending = store.unresolved_alerts(self.conn, "api_watch")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["send_status"], "sent")

    def test_resolved_alerts_drop_out(self):
        self.add_alert("api_watch", status="sent", hours_ago=1)
        self.add_alert("api_watch", status="sent", resolved=True, hours_ago=2)
        self.assertEqual(len(store.unresolved_alerts(self.conn, "api_watch")), 1)

    def test_mark_resolved_is_idempotent(self):
        self.add_alert("api_watch", status="sent", hours_ago=1)
        pending = store.unresolved_alerts(self.conn, "api_watch")
        ids = [int(a["id"]) for a in pending]
        self.assertEqual(store.mark_alerts_resolved(self.conn, ids), 1)
        self.assertEqual(store.mark_alerts_resolved(self.conn, ids), 0, "重复标记应无效果")
        self.assertEqual(store.unresolved_alerts(self.conn, "api_watch"), [])

    def test_unresolved_alerts_all_spans_tasks(self):
        self.add_alert("api_watch", status="sent", hours_ago=1)
        self.add_alert("log_scan", status="sent", hours_ago=2)
        self.assertEqual(len(store.unresolved_alerts_all(self.conn)), 2)


class TestRenderDigest(DigestTestBase):
    def test_renders_key_sections(self):
        self.add_run("api_watch", ai_called=1, ai_status="ok", tokens_in=10, tokens_out=5)
        self.add_alert("api_watch", status="sent", hours_ago=1)
        digest = daily_report.build_digest(self.conn, hours=24, now=self.now)
        markdown = report.render_daily_markdown(digest)
        for section in ("# ops-pipeline 日报", "## 各任务运行情况", "## 未恢复告警", "## 窗口内的告警记录"):
            self.assertIn(section, markdown)
        self.assertIn("api_watch", markdown)

    def test_all_clear_is_stated_explicitly(self):
        """一切正常也要写出来 —— 沉默无法被区分于"没跑"。"""
        self.add_run("api_watch", status="ok")
        digest = daily_report.build_digest(self.conn, hours=24, now=self.now)
        markdown = report.render_daily_markdown(digest)
        self.assertIn("所有告警都已恢复", markdown)

    def test_empty_window_does_not_crash(self):
        digest = daily_report.build_digest(self.conn, hours=24, now=self.now)
        markdown = report.render_daily_markdown(digest)
        self.assertIn("窗口内没有运行记录", markdown)


if __name__ == "__main__":
    unittest.main(verbosity=2)
