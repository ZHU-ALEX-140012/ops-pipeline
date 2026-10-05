"""判断层环境隔离测试：DSH_HOME 必须进项目，继承来的 DSH_* 必须被剔除。

这两条断言对应一个真实踩过的坑：`headless` profile **每次启动都会重写**
`<DSH_HOME>/profiles/<name>/cordis.yml`。若沿用编排层的 `DSH_HOME`（真实的 `~/.dsh`），
这个写入就落在工作区之外，在任何被沙箱约束的运行环境里每跑一次都要人工提权，
无人值守时更会直接 fail-closed。

测试目录同样放在 `data/_test_tmp` 下：避开沙箱对外部临时目录的写入限制。
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops.core import aibridge  # noqa: E402

TEST_TMP_ROOT = Path(__file__).resolve().parents[1] / "data" / "_test_tmp"


def _cfg(root: Path, **ai_overrides: object) -> dict:
    ai_cfg: dict = {"enabled": True, "profile": "headless"}
    ai_cfg.update(ai_overrides)
    return {"project": {"root": str(root)}, "ai": ai_cfg}


class DshHomeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TEST_TMP_ROOT / "aibridge_env"
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_default_dsh_home_is_inside_project(self) -> None:
        bridge = aibridge.AIBridge(_cfg(self.root))
        self.assertEqual(bridge.dsh_home, self.root / "data" / "dsh-home")
        self.assertTrue(bridge.dsh_home.is_dir(), "home 目录应当被预先创建")

    def test_relative_dsh_home_resolves_against_project_root(self) -> None:
        bridge = aibridge.AIBridge(_cfg(self.root, dsh_home="runtime/dsh"))
        self.assertEqual(bridge.dsh_home, self.root / "runtime" / "dsh")

    def test_blank_dsh_home_falls_back_to_default(self) -> None:
        # 配置里写空白不该把 home 解析成驱动器的当前目录（resolveDshHome 也遵循同一约定）
        bridge = aibridge.AIBridge(_cfg(self.root, dsh_home="   "))
        self.assertEqual(bridge.dsh_home, self.root / "data" / "dsh-home")

    def test_absolute_dsh_home_is_honored(self) -> None:
        target = self.root / "abs" / "dsh"
        bridge = aibridge.AIBridge(_cfg(self.root, dsh_home=str(target)))
        self.assertEqual(bridge.dsh_home, target)

    def test_child_env_keeps_only_our_dsh_home(self) -> None:
        bridge = aibridge.AIBridge(_cfg(self.root))
        leaked = {
            "DSH_HOME": r"C:\Users\someone\.dsh",
            "DSH_PROFILE": "desktop",
            "DSH_PROFILE_DIR": r"C:\Users\someone\.dsh\profiles\desktop",
            "DSH_SESSION_ID": "session-00000000-0000-0000-0000-000000000000",
            "DSH_SHELL": "1",
            "DSH_WEB_URL": "http://127.0.0.1:19387",
        }
        with mock.patch.dict(os.environ, leaked, clear=False):
            env = bridge._child_env()

        dsh_keys = {k for k in env if k.upper().startswith("DSH_")}
        self.assertEqual(
            dsh_keys,
            {"DSH_HOME"},
            f"子进程环境里只应保留我们指定的 DSH_HOME，实际: {sorted(dsh_keys)}",
        )
        self.assertEqual(env["DSH_HOME"], str(bridge.dsh_home))

    def test_child_env_preserves_unrelated_variables(self) -> None:
        bridge = aibridge.AIBridge(_cfg(self.root))
        with mock.patch.dict(os.environ, {"OPS_WECOM_WEBHOOK": "https://example.invalid/hook"}, clear=False):
            env = bridge._child_env()
        self.assertEqual(env.get("OPS_WECOM_WEBHOOK"), "https://example.invalid/hook")


if __name__ == "__main__":
    unittest.main()
