"""从浏览器"复制为 cURL"里导入登录 Cookie —— 两条命令搞定，不用手工拼字符串。

    python tools/import_cookie.py                      # 从剪贴板读
    python tools/import_cookie.py --name OPS_JD_COOKIE
    python tools/import_cookie.py --file curl.txt      # 从文件读
    type curl.txt | python tools/import_cookie.py -    # 从管道读

## 为什么做成这个工具（真实踩坑换来的）

有的站点把登录凭证设成 **HttpOnly** —— 浏览器**故意不给网页脚本看**，
所以控制台里 `copy(document.cookie)` **拿不到它**。
实测：京东的 `pt_key` / `pt_pin` 就是 HttpOnly，
`document.cookie` 只能给出一堆无关的跟踪 Cookie（`__jdv`、`shshshfpa`…），
拿它去请求，页面照样要求登录。

而"复制为 cURL"是**浏览器自己导出这个请求**，**包含它实际发出去的全部请求头**
（包括 HttpOnly Cookie）。所以这条路能一次拿到完整、可用的 Cookie。

难点本来是：那条 cURL 很长，人手工从里面挑出 Cookie 很容易挑错、挑漏。
这个工具就是把那一步自动化 —— 你只负责复制，它负责提取和存储。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ops.core import config as cfgmod  # noqa: E402
from ops.core import make_console_safe  # noqa: E402

# cURL 里 Cookie 的几种写法（Chrome / Edge / Firefox 导出的格式不同）
COOKIE_PATTERNS = [
    # -H 'cookie: xxx'   /  --header "cookie: xxx"
    re.compile(r"""-H\s+(['"])cookie:\s*(.*?)\1""", re.I | re.S),
    re.compile(r"""--header\s+(['"])cookie:\s*(.*?)\1""", re.I | re.S),
    # curl -b 'xxx'  /  --cookie 'xxx'
    re.compile(r"""-b\s+(['"])(.*?)\1""", re.S),
    re.compile(r"""--cookie\s+(['"])(.*?)\1""", re.I | re.S),
    # 直接粘了一段原始请求头
    re.compile(r"""^\s*cookie:\s*(.+)$""", re.I | re.M),
]


def looks_like_cookie(text: str) -> bool:
    """粗略判断"这像不像一串 Cookie"：至少有一个 name=value 且不含引号/换行。"""
    if not text or len(text) < 10:
        return False
    if any(ch in text for ch in "\"'\\\n"):
        return False
    return bool(re.search(r"[A-Za-z0-9_.\-]+=[^;=]+", text))


def extract_cookie(text: str) -> str | None:
    """从 cURL 文本里找出 Cookie。多个写法都试。"""
    for pattern in COOKIE_PATTERNS:
        for match in pattern.finditer(text):
            candidate = (match.group(2) if match.lastindex and match.lastindex >= 2
                         else match.group(1)).strip()
            if looks_like_cookie(candidate):
                return candidate
    # 兜底：整段文本本身可能就是 Cookie（用户直接粘了值）
    stripped = text.strip()
    return stripped if looks_like_cookie(stripped) else None


def read_clipboard() -> str | None:
    """用 PowerShell 的 Get-Clipboard 取剪贴板（仅 Windows）。"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 and proc.stdout else None


def summarize(cookie: str) -> str:
    names = [piece.split("=", 1)[0].strip()
             for piece in cookie.split(";") if "=" in piece]
    return f"{len(cookie)} 字符，{len(names)} 个 Cookie：{', '.join(names[:8])}" + \
           ("…" if len(names) > 8 else "")


def main(argv: list[str] | None = None) -> int:
    make_console_safe()
    parser = argparse.ArgumentParser(
        description="从浏览器『复制为 cURL』里导入登录 Cookie，写进密钥文件")
    parser.add_argument("--config", default=str(ROOT / "config" / "config.toml"))
    parser.add_argument("--name", default="OPS_JD_COOKIE",
                        help="写进密钥文件的键名（要和 config.toml 里 cookie_env 一致）")
    parser.add_argument("--file", help="从文件读；不填则读剪贴板")
    parser.add_argument("--stdin", action="store_true", help="从标准输入读")
    parser.add_argument("--show", action="store_true",
                        help="打印要写入的内容（**含凭据，别截图别外发**）")
    parser.add_argument("--dry-run", action="store_true", help="只解析，不写入")
    args = parser.parse_args(argv)

    # ---------------------------------------------------------- 取输入
    if args.file:
        source = f"文件 {args.file}"
        raw = Path(args.file).read_text(encoding="utf-8", errors="replace")
    elif args.stdin:
        source = "标准输入"
        raw = sys.stdin.read()
    else:
        source = "剪贴板"
        raw = read_clipboard()
        if raw is None:
            print("[FAIL] 读不到剪贴板。可以改成：")
            print("         python tools/import_cookie.py --file curl.txt")
            print("       或者把内容存成文件后用 --file 传进来。")
            return 1
        if not raw.strip():
            print("[FAIL] 剪贴板是空的。先在浏览器里：")
            print("         右键要请求 → 复制 → 以 cURL 格式复制（bash）")
            return 1

    print(f"输入来源：{source}（{len(raw)} 字符）")

    # ---------------------------------------------------------- 提取
    cookie = extract_cookie(raw)
    if not cookie:
        print("[FAIL] 在这段内容里没找到 Cookie。请确认复制的是：")
        print("         开发者工具 → 网络 → 右键某个请求 → 复制 → 以 cURL 格式复制")
        print("       并把整段内容原样复制过来（不要只复制一部分）。")
        return 1

    print(f"找到 Cookie：{summarize(cookie)}")
    if args.show:
        print()
        print("--- 即将写入的内容（含凭据，别外发）---")
        print(cookie)
        print("--- 结束 ---")

    # 常见的"白忙一场"情形：拿到的其实是无关的跟踪 Cookie
    names = {piece.split("=", 1)[0].strip().lower()
             for piece in cookie.split(";") if "=" in piece}
    if "jd" in (cookie[:200] + cookie[-200:]).lower() and "pt_key" not in names:
        print()
        print("⚠️ 这串里**没有 pt_key** —— 京东的登录凭证是 HttpOnly，")
        print("   `document.cookie` 拿不到它。请改用『网络 → 右键请求 → 复制为 cURL』。")

    if args.dry_run:
        print("（--dry-run：没有写入任何东西）")
        return 0

    # ---------------------------------------------------------- 写入
    cfg = cfgmod.load_config(args.config)
    path = cfgmod.write_secret(cfg, args.name, cookie)
    masked = cookie[:8] + "…" + cookie[-4:] if len(cookie) > 14 else "（已写入）"
    print()
    print(f"已写入 {path}")
    print(f"  {args.name:<22} {masked}")
    print()
    print("下一步：")
    print("  python -m ops --task price_watch --once --no-ai --dry-run")
    print("  不再出现『页面要求登录』就说明成了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
