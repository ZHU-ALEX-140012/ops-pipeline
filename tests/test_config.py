"""配置加载与 --override 的单元测试。

`--override` 是运维手动重跑的入口（"临时换个日志文件/换个 variant 跑一次"），
它自己出错会让人以为是被测系统出问题，所以这里覆盖得细一点。
"""

from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops.core import config as cfgmod  # noqa: E402

TMP = Path(__file__).resolve().parents[1] / "data" / "_test_tmp"


class TestOverrides(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "project": {"root": ""},
            "log_scan": {
                "thresholds": {"window_hours": 24},
                "sources": [
                    {"name": "app", "path": "data/logs/app.log"},
                    {"name": "web", "path": "data/logs/web.log"},
                ],
            },
        }

    def test_scalar_coercion(self):
        cfgmod.apply_overrides(self.cfg, ["log_scan.thresholds.window_hours=6"])
        self.assertEqual(self.cfg["log_scan"]["thresholds"]["window_hours"], 6)
        self.assertIsInstance(self.cfg["log_scan"]["thresholds"]["window_hours"], int)

    def test_float_and_bool_and_string(self):
        cfgmod.apply_overrides(self.cfg, [
            "log_scan.thresholds.spike_multiple=2.5",
            "log_scan.always_ai=true",
            "log_scan.sources.0.name=renamed",
        ])
        self.assertEqual(self.cfg["log_scan"]["thresholds"]["spike_multiple"], 2.5)
        self.assertIs(self.cfg["log_scan"]["always_ai"], True)
        self.assertEqual(self.cfg["log_scan"]["sources"][0]["name"], "renamed")

    def test_list_index(self):
        """列表型配置必须能按下标覆盖，否则"换个日志文件重跑"做不到。"""
        cfgmod.apply_overrides(self.cfg, ["log_scan.sources.0.path=data/logs/clean.log"])
        self.assertEqual(self.cfg["log_scan"]["sources"][0]["path"], "data/logs/clean.log")
        # 另一个源不受影响
        self.assertEqual(self.cfg["log_scan"]["sources"][1]["path"], "data/logs/web.log")

    def test_negative_value_is_not_a_flag(self):
        cfgmod.apply_overrides(self.cfg, ["log_scan.sources.0.enabled=false"])
        self.assertIs(self.cfg["log_scan"]["sources"][0]["enabled"], False)

    def test_empty_string_value(self):
        cfgmod.apply_overrides(self.cfg, ["alert.quiet_hours="])
        self.assertEqual(self.cfg["alert"]["quiet_hours"], "")

    def test_missing_equals_raises(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.apply_overrides(self.cfg, ["log_scan.thresholds.window_hours"])

    def test_bad_list_index_raises(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.apply_overrides(self.cfg, ["log_scan.sources.5.path=x"])

    def test_index_on_scalar_layer_raises(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.apply_overrides(self.cfg, ["log_scan.thresholds.0=1"])


class TestRootDerivation(unittest.TestCase):
    """root 留空时按配置文件位置推导 —— 项目整体搬移后不用改配置。"""

    def setUp(self):
        self.work = TMP / "root_derive"
        if self.work.exists():
            shutil.rmtree(self.work, ignore_errors=True)
        (self.work / "config").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def _write(self, root_line: str, name: str = "config.toml") -> Path:
        path = self.work / "config" / name
        path.write_text(f'[project]\n{root_line}\ntimezone_display = "Asia/Shanghai"\n', encoding="utf-8")
        return path

    def test_empty_root_derives_from_config_location(self):
        path = self._write('root = ""')
        cfg = cfgmod.load_config(path)
        self.assertEqual(Path(cfg["project"]["root"]), self.work)

    def test_explicit_root_is_respected(self):
        path = self._write('root = "D:/somewhere/else"')
        cfg = cfgmod.load_config(path)
        self.assertEqual(cfg["project"]["root"], "D:/somewhere/else")

    def test_missing_config_raises(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.load_config(self.work / "config" / "nope.toml")


if __name__ == "__main__":
    unittest.main(verbosity=2)
