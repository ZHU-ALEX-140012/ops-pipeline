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

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / "data" / "_test_tmp"


class TestLocalConfigOverride(unittest.TestCase):
    """`config/config.local.toml` 覆盖主配置。

    ## 为什么要有这个机制

    主配置 `config.toml` 是**跟着仓库走的**，里面只能放通用示例。
    而每个人真正要盯的商品、自己的阈值、私人的地址都是私人内容 ——
    写进主配置，一开源就等于公开了（比如"我正在比价买什么、预算多少"）。

    所以：主配置放示例，`config.local.toml` 放自己的，后者覆盖前者。
    这个文件在 .gitignore 里，**不进版本库**。
    """

    def setUp(self):
        self.tmp = TMP / "localcfg"
        shutil.rmtree(self.tmp, ignore_errors=True)
        (self.tmp / "config").mkdir(parents=True, exist_ok=True)
        self.main = self.tmp / "config" / "config.toml"
        self.local = self.tmp / "config" / "config.local.toml"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, main: str, local: str | None = None) -> dict:
        self.main.write_text(main, encoding="utf-8")
        if local is not None:
            self.local.write_text(local, encoding="utf-8")
        return cfgmod.load_config(str(self.main))

    def test_local_scalar_overrides_main(self):
        cfg = self.write('[project]\nroot = ""\n[ai]\ntimeout_seconds = 100\n',
                         "[ai]\ntimeout_seconds = 200\n")
        self.assertEqual(cfg["ai"]["timeout_seconds"], 200)

    def test_keys_not_mentioned_locally_are_kept(self):
        cfg = self.write('[project]\nroot = ""\n[ai]\ntimeout_seconds = 100\nmodel = "x"\n',
                         "[ai]\ntimeout_seconds = 200\n")
        self.assertEqual(cfg["ai"]["model"], "x", "本地没提的键不能被抹掉")

    def test_lists_are_replaced_not_appended(self):
        """⚠️ 这条是刻意的：清单必须能被**换掉**，而不是只能往上加。

        `[[price_watch.items]]` 是"要盯的东西的清单"。如果本地清单和公共示例
        拼在一起，用户就没法用本地文件替换掉仓库里的示例 ——
        而"示例不要出现在我的运行里"恰恰是最常见的需求。
        """
        cfg = self.write(
            '[project]\nroot = ""\n[[price_watch.items]]\nname = "demo"\n',
            '[[price_watch.items]]\nname = "mine"\n',
        )
        names = [i["name"] for i in cfg["price_watch"]["items"]]
        self.assertEqual(names, ["mine"], "本地清单应整体替换，而不是拼在后面")

    def test_nested_tables_merge_key_by_key(self):
        cfg = self.write(
            '[project]\nroot = ""\n[alert.email]\nenabled = true\nsmtp_port = 465\n',
            "[alert.email]\nsmtp_port = 587\n",
        )
        self.assertEqual(cfg["alert"]["email"]["smtp_port"], 587)
        self.assertTrue(cfg["alert"]["email"]["enabled"], "同层的其它键要保留")

    def test_no_local_file_behaves_exactly_as_before(self):
        cfg = self.write('[project]\nroot = ""\n[ai]\ntimeout_seconds = 100\n')
        self.assertEqual(cfg["ai"]["timeout_seconds"], 100)
        self.assertNotIn("_local_config", cfg)

    def test_local_file_is_recorded_for_diagnostics(self):
        cfg = self.write('[project]\nroot = ""\n', "[ai]\ntimeout_seconds = 1\n")
        self.assertIn("config.local.toml", cfg["_local_config"])

    def test_broken_local_file_raises_a_clear_error(self):
        """本地文件写坏了要**指名道姓**地报，而不是含糊的解析错误。"""
        with self.assertRaises(cfgmod.ConfigError) as ctx:
            self.write('[project]\nroot = ""\n', "这不是 TOML\n")
        self.assertIn("config.local.toml", str(ctx.exception))
        self.assertIn("本地覆盖配置", str(ctx.exception))

    def test_deep_merge_does_not_mutate_inputs(self):
        base = {"a": {"b": 1}, "list": [1]}
        override = {"a": {"b": 2}, "list": [9]}
        merged = cfgmod._deep_merge(base, override)
        self.assertEqual(base, {"a": {"b": 1}, "list": [1]}, "原字典不该被改动")
        self.assertEqual(merged["a"]["b"], 2)
        self.assertEqual(merged["list"], [9])


class TestRepoIsSafeToPublish(unittest.TestCase):
    """⚠️ 这几条是给**开源**兜底的。

    一旦私人内容进了版本库，推到公开仓库就等于公开了。所以把它变成硬约束，
    而不是靠提交前"记得检查一下"。
    """

    DEMO_ITEM_NAMES = {"rtx5070", "rtx5080", "rx9070"}

    def test_main_config_contains_only_demo_items(self):
        """主配置只许有指向演示桩的示例项，不许混进私人清单。"""
        import tomllib

        base = tomllib.loads((ROOT / "config" / "config.toml").read_text(encoding="utf-8"))
        names = {str(i.get("name")) for i in base["price_watch"]["items"]}
        extra = names - self.DEMO_ITEM_NAMES
        self.assertEqual(
            extra, set(),
            f"主配置里混进了非示例商品 {sorted(extra)} —— 私人清单请放 config.local.toml",
        )

    def test_local_config_is_gitignored(self):
        """私人清单所在的文件必须被忽略，否则一提交就公开了。"""
        import subprocess

        proc = subprocess.run(["git", "check-ignore", "config/config.local.toml"],
                              cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0,
                         "config/config.local.toml 必须在 .gitignore 里")

    def test_no_secrets_file_is_tracked(self):
        """凭据文件永不被跟踪 —— 这是推公开仓库前的底线。"""
        import subprocess

        tracked = subprocess.run(["git", "ls-files"], cwd=str(ROOT),
                                 capture_output=True, text=True,
                                 encoding="utf-8", errors="replace").stdout.splitlines()
        offenders = [f for f in tracked
                     if "secrets.local" in f or f.startswith("data/") or f.endswith(".db")]
        self.assertEqual(offenders, [], f"这些敏感文件被跟踪了：{offenders}")

    def test_no_generated_machine_specific_launcher_is_tracked(self):
        """`run_once.cmd` 是现场生成的，里面有本机绝对路径与用户名，不该入库。"""
        import subprocess

        tracked = subprocess.run(["git", "ls-files", "scripts/run_once.cmd"],
                                 cwd=str(ROOT), capture_output=True, text=True,
                                 encoding="utf-8", errors="replace").stdout.strip()
        self.assertEqual(tracked, "", "scripts/run_once.cmd 不该被跟踪（它由 register_tasks 生成）")

    def test_commit_authors_use_a_private_email(self):
        """⚠️ 提交里的邮箱必须是**不公开**的那种。

        这是实测想到的一条：原来作者信息是"真名 + 个人 QQ 邮箱"，
        而它会被写进**每一个提交**、推到公开仓库后谁都能看到 ——
        等于公开招揽垃圾邮件。真名对简历是好事，邮箱不是。

        所以这里把它变成硬约束：任何一次提交如果用了私人邮箱，测试就红。
        """
        import subprocess

        log = subprocess.run(["git", "log", "--format=%ae%n%ce"], cwd=str(ROOT),
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace").stdout
        emails = {e.strip() for e in log.splitlines() if e.strip()}
        if not emails:
            self.skipTest("还不是 git 仓库或没有提交")
        offenders = [e for e in emails if not e.endswith("users.noreply.github.com")]
        self.assertEqual(
            offenders, [],
            f"提交里出现了非私密邮箱：{offenders}。"
            "公开仓库请用 <用户名>@users.noreply.github.com",
        )

    def test_commit_author_names_are_consistent(self):
        """姓名可以是真的（简历需要），但必须是**同一个** —— 混用会显得历史很乱。"""
        import subprocess

        log = subprocess.run(["git", "log", "--format=%an"], cwd=str(ROOT),
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace").stdout
        names = {n.strip() for n in log.splitlines() if n.strip()}
        if not names:
            self.skipTest("还不是 git 仓库或没有提交")
        self.assertEqual(len(names), 1, f"提交里混了多个作者名：{sorted(names)}")


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
