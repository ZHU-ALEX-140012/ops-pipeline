"""core: 与具体任务无关的公共能力（配置、HTTP、存储、判定、告警、报告、AI 桥）。"""

from __future__ import annotations

import sys


def make_console_safe(*streams: object) -> None:
    """让控制台输出永不因为编码问题崩溃。

    中文 Windows 的控制台默认是 GBK 代码页，遇到 emoji（⚠️、✓）这类字符会抛
    UnicodeEncodeError —— 而那通常发生在**正要打印错误信息**的时候，
    结果就是"排查问题的工具自己先把错误吞了"。

    与其逐个字符串避免特殊字符（治标，且改一处漏一处），
    不如在进程入口统一把不可编码字符替换成 '?'。

    可显式传入要处理的流（便于测试），默认处理 stdout / stderr。
    """
    targets = streams or (sys.stdout, sys.stderr)
    for stream in targets:
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass  # 不是可重配的文本流（例如被重定向到管道）就跳过
