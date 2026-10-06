"""人工记一条价格 —— 给"抓不到源"的商品用。

    python tools/price_set.py --item dgx_spark --price 28999
    python tools/price_set.py --item dgx_spark --price "¥28,999" --note "京东自营，有货"
    python tools/price_set.py --list
    python tools/price_set.py --item dgx_spark --no-run      # 只记录，不立刻判断

## 为什么需要它

实测结论：**有价格的站点全都抓不到**（官方店超时、B&H 403、京东/什么值得买返回反爬页），
而商品页本身大多不带价格或由 JS 渲染。

这时候最诚实的做法不是硬爬，而是**人看一眼、敲一条命令** ——
剩下的（历史、判据、到价提醒、涨跌提醒、告警邮件、报告、趋势）全部自动。

对"一个月看一次"的贵东西（几万块的设备），这就是最实用的形态：
你本来就要去看价格，顺手记一条的成本是 5 秒。

记录完默认**立刻跑一轮判断**，这样"记了价格"和"收到提醒"是同一件事，
不用你再想"我还要不要再跑一次"。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ops.core import config as cfgmod  # noqa: E402
from ops.core import make_console_safe, prices  # noqa: E402


def _items(cfg: dict) -> list[dict]:
    return [i for i in (cfg.get("price_watch") or {}).get("items") or []]


def cmd_list(cfg: dict) -> int:
    recorded = prices.load_manual_prices(cfg)
    items = _items(cfg)
    print(f"配置里的商品（{len(items)} 个）：")
    for item in items:
        name = str(item.get("name"))
        source = item.get("source") or ("html" if item.get("price_regex") else "json")
        enabled = "启用" if item.get("enabled", True) else "**未启用**"
        print(f"  {name:<16} 来源={source:<7} {enabled}   {item.get('label') or ''}")
        if source == "manual":
            entry = recorded.get(name)
            if entry:
                print(f"      已录入：{prices.format_price(prices.parse_price(entry.get('price')), entry.get('currency') or 'CNY')}"
                      f"  （{entry.get('recorded_at')}）"
                      + (f"  备注：{entry.get('note')}" if entry.get("note") else ""))
            else:
                print("      **还没录入过** —— 用 --item "
                      f"{name} --price <价格> 记一条")
    return 0


def main(argv: list[str] | None = None) -> int:
    make_console_safe()
    parser = argparse.ArgumentParser(description="人工记录一条价格（给抓不到源的商�品用）")
    parser.add_argument("--config", default=str(ROOT / "config" / "config.toml"))
    parser.add_argument("--item", help="商品名，要和 config 里 [[price_watch.items]] 的 name 一致")
    parser.add_argument("--price", help="价格，可以带货币符号和千分位（¥28,999.00）")
    parser.add_argument("--currency", default=None, help="货币代码，默认用配置里的")
    parser.add_argument("--note", default="", help="备注（在哪看到的、有没有货）")
    parser.add_argument("--list", action="store_true", help="列出商品与已录入的价格")
    parser.add_argument("--no-run", action="store_true", help="只记录，不立刻跑判断")
    parser.add_argument("--dry-run", action="store_true", help="跑判断但不真发告警")
    parser.add_argument("--no-ai", action="store_true", help="跑判断但跳过 AI（省钱）")
    args = parser.parse_args(argv)

    cfg = cfgmod.load_config(args.config)

    if args.list or not args.item:
        return cmd_list(cfg)

    if not args.price:
        print("[FAIL] 要记价格就得给 --price（或者用 --list 看现状）", file=sys.stderr)
        return 1

    known = {str(i.get("name")): i for i in _items(cfg)}
    if args.item not in known:
        print(f"[FAIL] 配置里没有商品 {args.item!r}。可用：{sorted(known) or '（无）'}", file=sys.stderr)
        return 1

    value = prices.parse_price(args.price)
    if value is None:
        print(f"[FAIL] {args.price!r} 没法当价格解析 —— 例如 '等有货了再说' 就不是价格",
              file=sys.stderr)
        return 1

    currency = args.currency or str(known[args.item].get("currency") or "CNY")
    path = prices.save_manual_price(cfg, args.item, value, currency, args.note)
    print(f"已记录：{args.item} = {prices.format_price(value, currency)}"
          + (f"（{args.note}）" if args.note else ""))
    print(f"  落在：{path}")

    # 顺手检查一下这个商品到底走哪条源 —— 别人为"记了却没用上"
    source = known[args.item].get("source") or (
        "html" if known[args.item].get("price_regex") else "json")
    if source != "manual" and not known[args.item].get("url"):
        print(f"  ⚠️ 「{args.item}」的 source={source} 但没有 url，"
              f"建议改成 source = \"manual\"")
    elif source != "manual":
        print(f"  提示：「{args.item}」当前来源是 {source}（不是 manual），"
              f"这条记录不会被采集器读到。")
        print("        想让 price_watch 用人工价格，把它改成 source = \"manual\"。")

    if args.no_run:
        print("（--no-run：没有立刻跑判断）")
        return 0

    from ops.tasks import price_watch

    print()
    print("=" * 60)
    return price_watch.run(cfg, dry_run=args.dry_run, no_ai=args.no_ai)


if __name__ == "__main__":
    raise SystemExit(main())
