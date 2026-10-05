"""控制台输出的健壮性测试。

中文 Windows 的控制台是 GBK 代码页。如果报错信息里带 emoji（服务端原样返回的
消息经常带），打印时就会抛 UnicodeEncodeError —— 于是"要告诉用户哪里出错了"
变成了一个 traceback。这个坑本项目踩过两次，所以在这里锁死。
"""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops.core import make_console_safe  # noqa: E402


def gbk_stream() -> io.TextIOWrapper:
    """模拟一个 GBK、严格模式的控制台流。"""
    return io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict", newline="")


class TestConsoleSafety(unittest.TestCase):
    def test_emoji_crashes_strict_gbk(self):
        """先证明问题真实存在，否则这个测试就是摆设。"""
        stream = gbk_stream()
        with self.assertRaises(UnicodeEncodeError):
            stream.write("拒绝投递 ⚠️ 请注意")
            stream.flush()

    def test_make_console_safe_prevents_crash(self):
        stream = gbk_stream()
        make_console_safe(stream)
        stream.write("拒绝投递 ⚠️ 请注意")
        stream.flush()  # 不该抛异常

    def test_chinese_still_renders(self):
        """替换的只是打不出来的字符，中文本身必须原样保留。"""
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="gbk", errors="strict", newline="")
        make_console_safe(stream)
        stream.write("企业微信拒绝投递：errcode=60020")
        stream.flush()
        self.assertIn("企业微信拒绝投递", raw.getvalue().decode("gbk"))

    def test_tolerates_non_reconfigurable_stream(self):
        """被重定向到管道或替换成奇怪对象时不能反过来把程序搞崩。"""
        make_console_safe(object())
        make_console_safe(None)
        make_console_safe(io.StringIO())


if __name__ == "__main__":
    unittest.main(verbosity=2)
