"""给一个网址，帮你找出能抠出价格的正则。

    python tools/price_probe.py --url "https://某个商品页/"
    python tools/price_probe.py --url "..." --keyword DGX --context 120
    python tools/price_probe.py --url "..." --test "价格：¥([\\d,]+)"

## 它解决什么问题

`price_watch` 支持 `price_regex`（从页面文本抠价格），但"正则该怎么写"
不该让人靠猜。这个工具把页面抓下来（有界读），把**像价格的东西**连同
上下文一起列出来，并直接给出可以粘进 `config.toml` 的配置片段。

**它也是这套东西面对现实的方式**：官方店封机器人、电商返回反爬页、
价格由 JS 渲染 —— 这些都会在这里一眼看出来，而不是等到
"监控跑了三天什么都没报"才发现。

## 它不做的事

* 不绕过任何反爬（403 / 验证码 / 反爬页会如实报给你）
* 不猜价格（它只列出**页面里真实存在**的候选，由你确认哪个是对的）
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ops.core import http, make_console_safe  # noqa: E402

# 常见价格形态。按"精度从高到低"排：结构化数据 > 带货币符号 > 纯数字。
CANDIDATE_PATTERNS: list[tuple[str, str, str]] = [
    ("结构化数据（最稳）", r'"price"\s*:\s*"?([0-9][0-9,]*\.?[0-9]*)"?',
     r'"price"\s*:\s*"?([0-9][0-9,]*\.?[0-9]*)"?'),
    ("结构化数据 amount", r'"amount"\s*:\s*"?([0-9][0-9,]*\.?[0-9]*)"?',
     r'"amount"\s*:\s*"?([0-9][0-9,]*\.?[0-9]*)"?'),
    ("美元金额", r"\$\s?([0-9][0-9,]{2,}(?:\.\d{2})?)", r"\$\s?([0-9][0-9,]{2,}(?:\.\d{2})?)"),
    ("人民币金额", r"[¥￥]\s?([0-9][0-9,]{2,}(?:\.\d{2})?)",
     r"[¥￥]\s?([0-9][0-9,]{2,}(?:\.\d{2})?)"),
    ("N元", r"([0-9][0-9,]{3,})\s*元", r"([0-9][0-9,]{3,})\s*元"),
]

# 反爬/拦截的典型特征 —— 早说比晚说好
BLOCK_HINTS = [
    ("验证", "可能需要验证码/登录"),
    ("captcha", "可能需要验证码/登录"),
    ("robot", "可能被识别为机器人"),
    ("访问频繁", "可能被限流"),
    ("forbidden", "被拒绝访问"),
    ("access denied", "被拒绝访问"),
    ("Just a moment", "Cloudflare 挑战页"),
]


def snippet(text: str, start: int, end: int, width: int = 100) -> str:
    left = max(0, start - width // 2)
    right = min(len(text), end + width // 2)
    return re.sub(r"\s+", " ", text[left:right]).strip()


def main(argv: list[str] | None = None) -> int:
    make_console_safe()
    parser = argparse.ArgumentParser(description="帮你找出能从页面抠出价格的正则")
    parser.add_argument("--url", required=True)
    parser.add_argument("--keyword", default=None,
                        help="只关心含这个词的候选（例如 DGX）")
    parser.add_argument("--max-bytes", type=int, default=400_000)
    parser.add_argument("--timeout", type=int, default=25000, help="毫秒")
    parser.add_argument("--test", default=None,
                        help="直接测一条正则，看它能抠出什么")
    parser.add_argument("--limit", type=int, default=8, help="每类最多列几个候选")
    args = parser.parse_args(argv)

    print(f"抓取：{args.url}")
    response = http.fetch_text("GET", args.url, timeout_ms=args.timeout,
                               max_bytes=args.max_bytes)
    if response.error:
        print(f"[FAIL] 抓不到：{response.error}")
        return 1
    print(f"  状态 {response.status}  读取 {response.bytes_read} 字节"
          f"{'（**被截断**，价格可能在没读到的部分）' if response.truncated else ''}")
    print(f"  类型 {response.content_type or '（未给）'}")
    if not response.ok:
        print(f"[FAIL] 非 2xx —— 这个地址拿不到页面内容，换一个源")
        return 1

    text = response.text
    lowered = text.lower()
    print(f"  文本 {len(text)} 字符")

    # ---------------------------------------------------------- 反爬提示
    #
    # ⚠️ 这里踩过两次误报，都值得记下来：
    #   1) 一个 392KB 的正常官方页面，因为 JS 里恰好含 "robot"/"验证" 字样被判成反爬页；
    #   2) 一个 70 字节的 JSON API（完全正常）因为"页面太短"被判成反爬页。
    # 所以判据要**同时**满足"是 HTML"且"很短" —— 短 JSON 是最正常不过的东西。
    hints = {msg for token, msg in BLOCK_HINTS if token.lower() in lowered}
    is_html = "html" in (response.content_type or "").lower()
    short_html = is_html and len(text) < 3000
    if short_html:
        print()
        print(f"  ⚠️ 这个页面**不是真的商品页**"
              f"（HTML 只有 {len(text)} 字符，正常商品页不会这么短）：")
        for hint in sorted(hints):
            print(f"      - {hint}")
        if not hints:
            print("      - 内容太短，大概率是反爬页 / 跳转页 / 需要登录")
        print("      对策：换一个有权限的源，或用 `tools/price_set.py` 人工记价格。")
    elif hints and is_html:
        print()
        print(f"  注：页面里出现了 {sorted(hints)} 这类字眼，"
              f"但页面有 {len(text)} 字符、看起来是正常内容 —— 大概率只是代码，不用管。")

    # ---------------------------------------------------------- 关键词检查
    if args.keyword and args.keyword.lower() not in lowered:
        print()
        print(f"  ⚠️ 页面里没有 {args.keyword!r} 字样 —— 它可能不是你要的那个商品页。")

    # ---------------------------------------------------------- 直接测正则
    if args.test:
        print()
        print(f"测试正则：{args.test}")
        try:
            compiled = re.compile(args.test, re.S)
        except re.error as exc:
            print(f"[FAIL] 不是合法正则：{exc}")
            return 1
        matches = compiled.findall(text)
        if not matches:
            print("  没有匹配。页面改版、或价格由 JS 渲染时会发生这种情况。")
            return 1
        for match in matches[:args.limit]:
            print(f"  匹配：{match!r}")
        print()
        print("  配置写法：")
        print(f'    price_regex = \'{args.test.replace(chr(92), chr(92) * 2)}\'')
        return 0

    # ---------------------------------------------------------- 列候选
    print()
    print("=== 页面里像价格的东西 ===")
    any_found = False
    for label, search_pattern, config_pattern in CANDIDATE_PATTERNS:
        try:
            compiled = re.compile(search_pattern, re.S)
        except re.error:
            continue
        found = []
        for match in compiled.finditer(text):
            found.append((match.group(1), snippet(text, match.start(), match.end())))
            if len(found) >= args.limit:
                break
        if not found:
            continue
        any_found = True
        print(f"\n-- {label}  ({len(found)} 个候选)")
        for value, around in found:
            print(f"   {value:<12} …{around}…")
        print("   可用配置：")
        print(f'     price_regex = \'{config_pattern}\'')

    if not any_found:
        print("  一个都没有。")
        print()
        print("  可能的原因：")
        print("    1) 价格由 JS 渲染（抓到的 HTML 里没有）—— 需要找接口或用人工录入")
        print("    2) 页面是反爬/跳转页 —— 换源")
        print("    3) 价格用了不常见写法 —— 用 --test 自己试一条正则")
        print("    4) 被读取上限截断了（上面会提示）—— 加大 --max-bytes")
        return 1

    print()
    print("=== 下一步 ===")
    print("  把选中的 price_regex 填进 config/config.toml 的 [[price_watch.items]]，")
    print("  然后：python -m ops --task price_watch --once --dry-run --no-ai")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
