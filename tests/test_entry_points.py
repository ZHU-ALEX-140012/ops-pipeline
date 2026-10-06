"""每个命令行入口都必须先做控制台安全化。

## 真实踩到的坑

中文 Windows 控制台是 GBK。任何入口只要可能打印 emoji 或特殊符号
（`⚠️`、`✓`、`→`…），不先调 `make_console_safe()` 就会抛
`UnicodeEncodeError: 'gbk' codec can't encode character ...`。

它把**"要显示的状态"变成"看起来像崩溃"** —— 这个项目一开始就为此做过根因修复，
但当时的测试只覆盖了 `python -m ops`，没覆盖 `tools/` 下的脚本。
于是后来新写的 `tools/dashboard.py` 又踩了同一个坑（HTML 里的 `⚠️` 打崩了终端）。

**所以这条测试是系统性的**：扫所有入口脚本，检查它们是否在打印之前调用了它。
靠人记住"新脚本要加一行"是靠不住的。
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def console_entry_points() -> list[Path]:
    """会往终端打印东西的入口脚本。"""
    out = []
    for path in sorted((ROOT / "tools").glob("*.py")):
        out.append(path)
    out.append(ROOT / "ops" / "__main__.py")
    return out


def calls_make_console_safe(path: Path) -> bool:
    """静态检查：这个文件里有没有调用 make_console_safe。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "make_console_safe":
                return True
            if isinstance(func, ast.Attribute) and func.attr == "make_console_safe":
                return True
    return False


class TestEveryEntryPointIsConsoleSafe(unittest.TestCase):
    def test_all_entry_points_call_it(self):
        missing = [p.name for p in console_entry_points() if not calls_make_console_safe(p)]
        self.assertEqual(
            missing, [],
            f"这些入口没有先做控制台安全化，打印 emoji 时会崩：{missing}",
        )

    def test_detector_actually_detects(self):
        """扫描器本身要能区分"有"和"没有" —— 否则这条测试是空转的。"""
        tmp = ROOT / "data" / "_test_tmp" / "console_probe.py"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            tmp.write_text("print('hi')\n", encoding="utf-8")
            self.assertFalse(calls_make_console_safe(tmp))
            tmp.write_text("from ops.core import make_console_safe\nmake_console_safe()\n",
                           encoding="utf-8")
            self.assertTrue(calls_make_console_safe(tmp))
        finally:
            tmp.unlink(missing_ok=True)


class TestBatchFilesAreAsciiOnly(unittest.TestCase):
    """`.cmd` 文件必须是纯 ASCII。

    ## 真实踩到的坑

    我给面板写了个双击启动的 `scripts/dashboard.cmd`，里面写了中文注释和
    中文提示，还在开头加了 `chcp 65001`（想着这样中文就不乱码了）。
    结果双击后一堆：

        '解释器优先复用' is not recognized as an internal or external command
        File "...\\run_once.cmd", line 1  @echo off  SyntaxError

    也就是 **cmd.exe 把中文注释当成命令去执行了**，最后甚至让 Python 去跑一个 .cmd。

    两个原因叠在一起：

    1. **`.cmd` 里的多字节字符会打乱 cmd.exe 的解析**；
    2. **在 `.cmd` 里 `chcp` 改代码页会让 cmd.exe 自己的读取位置错乱** ——
       它按字节偏移读文件，中途换代码页就对不上了。

    项目里**本来所有 `.cmd` 都是 0 非 ASCII**（register_tasks.cmd / set-secret.cmd …），
    是既有约定，我破坏了它。所以这条测试把它变成硬约束：
    **`.cmd` 只放 ASCII，给用户看的中文一律从 Python 那边打印**
    （Python 侧有 `make_console_safe()` 处理编码）。
    """

    def cmd_files(self) -> list[Path]:
        return sorted((ROOT / "scripts").glob("*.cmd"))

    def test_there_are_some(self):
        self.assertTrue(self.cmd_files(), "scripts/ 下应该至少有一个 .cmd")

    def test_all_cmd_files_are_ascii_only(self):
        offenders: list[str] = []
        for path in self.cmd_files():
            bad = sum(1 for byte in path.read_bytes() if byte > 127)
            if bad:
                offenders.append(f"{path.name}（{bad} 个非 ASCII 字节）")
        self.assertEqual(
            offenders, [],
            "这些 .cmd 里有非 ASCII 字符，cmd.exe 可能把注释当命令执行："
            f"{offenders}。给用户看的中文请交给 Python 打印。",
        )

    def test_no_cmd_changes_the_code_page(self):
        """`chcp` 写在 .cmd 里会让 cmd.exe 的读取位置错乱（实测过），一律不许有。"""
        for path in self.cmd_files():
            text = path.read_text(encoding="ascii", errors="replace")
            body = "\n".join(line for line in text.splitlines()
                             if not line.strip().lower().startswith("rem"))
            self.assertNotIn("chcp", body.lower(), f"{path.name} 里不该出现 chcp")


class TestMakeConsoleSafeBehaviour(unittest.TestCase):
    def test_replaces_unencodable_characters(self):
        """把它换成一个非 UTF-8 的编码，确认打印 emoji 不再抛异常。"""
        import io

        from ops.core import make_console_safe

        buffer = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")
        original = sys.stdout
        try:
            sys.stdout = buffer
            make_console_safe()
            print("⚠️ ✓ → 中文")   # 不抛异常即通过
            buffer.flush()
        finally:
            sys.stdout = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
