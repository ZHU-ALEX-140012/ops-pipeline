"""自检的测试。

自检工具最没用的失败方式是**它自己崩了、什么都没告诉我**。
所以这里重点是：单项出错不影响整体、检查项本身别报假警。

有两个真实踩到的坑被锁在这里：
1. `heartbeat` 的配置在 `[schedule.heartbeat_expect]` 下，不在 `[heartbeat]` ——
   第一版把它列进"必须有独立配置段"的清单，于是自检报了一个不存在的缺失；
2. `run_doctor` 统一带着 cfg 调各检查项，而 `check_golden_set()` 不接参数 ——
   自检把自己报成了 FAIL。
"""

from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ops.core import doctor  # noqa: E402

TMP = ROOT / "data" / "_test_tmp" / "doctor"


def minimal_cfg(root: Path | None = None) -> dict:
    return {
        "project": {"root": str(root or TMP)},
        "ai": {"cmd": "no-such-dsh.cmd", "patch": "no-such-patch.yml", "dsh_home": "data/dsh-home"},
        "alert": {"primary": "email", "email": {"enabled": False}},
        "api_watch": {}, "regression": {}, "log_scan": {}, "daily_report": {}, "feedback": {},
        "schedule": {"heartbeat_expect": {"api_watch": 60}},
    }


class DoctorTestBase(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        (TMP / "data").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)


class TestIndividualChecks(DoctorTestBase):
    def test_config_ok(self):
        checks = doctor.check_config(minimal_cfg())
        self.assertEqual([c.status for c in checks if c.name == "配置段完整"], [doctor.OK])

    def test_config_flags_missing_section(self):
        cfg = minimal_cfg()
        del cfg["regression"]
        checks = doctor.check_config(cfg)
        item = next(c for c in checks if c.name == "配置段完整")
        self.assertEqual(item.status, doctor.FAIL)
        self.assertIn("regression", item.detail)

    def test_heartbeat_is_not_a_top_level_section(self):
        """真实踩到的坑：heartbeat 的配置在 [schedule.heartbeat_expect] 里。"""
        checks = doctor.check_config(minimal_cfg())
        self.assertEqual(next(c for c in checks if c.name == "配置段完整").status, doctor.OK)
        expect = next(c for c in checks if c.name == "心跳预期间隔")
        self.assertEqual(expect.status, doctor.OK)
        self.assertIn("api_watch", expect.detail)

    def test_missing_heartbeat_expect_is_flagged(self):
        cfg = minimal_cfg()
        cfg["schedule"] = {}
        item = next(c for c in doctor.check_config(cfg) if c.name == "心跳预期间隔")
        self.assertEqual(item.status, doctor.FAIL)

    def test_storage_creates_and_verifies_dirs(self):
        checks = doctor.check_storage(minimal_cfg())
        writable = [c for c in checks if c.name.startswith("目录可写")]
        self.assertTrue(writable)
        self.assertTrue(all(c.status == doctor.OK for c in writable), writable)

    def test_storage_missing_db_is_warn_not_fail(self):
        """还没跑过第一次就没有库，那是正常的，不该报 FAIL。"""
        item = next(c for c in doctor.check_storage(minimal_cfg()) if c.name == "历史库")
        self.assertEqual(item.status, doctor.WARN)

    def test_ai_check_flags_missing_command(self):
        item = next(c for c in doctor.check_ai(minimal_cfg()) if c.name == "判断层命令")
        self.assertEqual(item.status, doctor.FAIL)
        self.assertTrue(item.fix, "失败项必须告诉人怎么修")

    def test_investigate_reports_budget_enforceability(self):
        checks = doctor.check_investigate(minimal_cfg())
        self.assertTrue(checks[0].name in ("工具预算可强制", "只读调查工具"))

    def test_golden_set_accepts_a_config_argument(self):
        """真实踩到的坑：run_doctor 带 cfg 调用，而这个函数不接参数。"""
        checks = doctor.check_golden_set(minimal_cfg())
        self.assertEqual(checks[0].status, doctor.OK)


class TestRunDoctor(DoctorTestBase):
    def test_runs_all_checks_without_raising(self):
        checks = doctor.run_doctor(minimal_cfg(), offline=True)
        self.assertGreater(len(checks), 10)
        self.assertTrue(all(c.status in (doctor.OK, doctor.WARN, doctor.FAIL, doctor.SKIP)
                            for c in checks))

    def test_one_broken_check_does_not_kill_the_rest(self):
        """单项炸了必须只影响那一项 —— 否则自检最没用。"""
        original = doctor.CHECKS
        try:
            def boom(cfg, offline=False):
                raise RuntimeError("故意炸")

            doctor.CHECKS = (boom,) + original[1:]
            checks = doctor.run_doctor(minimal_cfg(), offline=True)
        finally:
            doctor.CHECKS = original
        failed = [c for c in checks if c.status == doctor.FAIL and "boom" in c.name]
        self.assertEqual(len(failed), 1)
        self.assertIn("故意炸", failed[0].detail)
        self.assertGreater(len(checks), 10, "其余检查项应该照常跑完")

    def test_offline_skips_network_checks(self):
        checks = doctor.run_doctor(minimal_cfg(), offline=True)
        self.assertTrue(any(c.status == doctor.SKIP for c in checks))


class TestSummaryAndRender(DoctorTestBase):
    def test_summarize_counts(self):
        checks = [doctor.Check("a", doctor.OK, "x"), doctor.Check("b", doctor.WARN, "y"),
                  doctor.Check("c", doctor.FAIL, "z"), doctor.Check("d", doctor.OK, "w")]
        counts = doctor.summarize(checks)
        self.assertEqual((counts[doctor.OK], counts[doctor.WARN], counts[doctor.FAIL]), (2, 1, 1))

    def test_render_includes_fix_hint(self):
        text = doctor.render([doctor.Check("出问题了", doctor.FAIL, "细节", "照这样做")])
        self.assertIn("出问题了", text)
        self.assertIn("照这样做", text)
        self.assertIn("汇总", text)

    def test_output_is_console_safe(self):
        """自检输出会进日志，不能因为编码问题炸掉。"""
        text = doctor.render([doctor.Check("中文", doctor.OK, "中文细节")])
        self.assertIsInstance(text, str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
