"""合成日志生成器。

存在的理由和 fault_drill / targets/service.py 一样：
**你必须先证明"分析器能抓到已知异常"，才敢相信它在真实日志里不撒谎。**

它生成一段时间跨度的正常流量，并可按需在**最近一个窗口**注入已知异常：

    new-error   出现一个历史上从未出现过的错误模板（最难发现、也最该报）
    spike       某个既有错误模板的量级暴涨（量级问题，不是新问题）

用法：
    python tools/loggen.py --out data/logs/app.log --hours 48 --inject new-error --inject spike
    python tools/loggen.py --out data/logs/clean.log --hours 48          # 干净基线
"""

from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta
from pathlib import Path

# 正常流量池：(级别, 模板, 相对权重)
NORMAL_POOL: list[tuple[str, str, int]] = [
    ("INFO", "INFO [http] request completed method=GET path=/api/users/{id} status=200 ms={ms}", 30),
    ("INFO", "INFO [http] request completed method=POST path=/api/orders status=201 ms={ms}", 12),
    ("INFO", "INFO [http] request completed method=GET path=/api/products/{id} status=200 ms={ms}", 18),
    ("INFO", "INFO [db] query executed table={table} rows={rows} ms={ms}", 20),
    ("INFO", "INFO [cache] hit key=product:{id}", 14),
    ("INFO", "INFO [cache] miss key=product:{id}", 6),
    ("INFO", "INFO [auth] token refreshed user={id}", 5),
    ("WARN", "WARN [db] slow query table={table} ms={ms} threshold=500", 3),
    ("ERROR", "ERROR [db] connection failed host=10.0.0.{ip} port=5432 retry={r}", 2),
    ("WARN", "WARN [auth] token about to expire user={id} ttl=60", 1),
]

TABLES = ("users", "orders", "products", "sessions")
PATHS = ("/api/users/1", "/api/products/2", "/api/orders")

# 注入用：历史上从未出现过的错误模板
NEW_ERROR_TEMPLATE = "ERROR [cache] eviction failed shard={shard} reason=oom"
# 注入用：被放大的既有错误模板
SPIKE_TEMPLATE = "ERROR [db] connection failed host=10.0.0.{ip} port=5432 retry={r}"


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S.") + f"{moment.microsecond // 1000:03d}"


def _render(template: str, rng: random.Random) -> str:
    return template.format(
        id=rng.randint(1000, 99999),
        ms=rng.randint(3, 900),
        table=rng.choice(TABLES),
        rows=rng.randint(1, 500),
        ip=rng.randint(1, 254),
        r=rng.randint(1, 3),
        shard=rng.randint(0, 15),
    )


def generate(
    *,
    hours: float,
    window_hours: float,
    injections: list[str],
    seed: int,
    rate_per_minute: float,
) -> tuple[list[tuple[datetime, str]], dict[str, int]]:
    rng = random.Random(seed)
    now = datetime.now().replace(microsecond=0)
    start = now - timedelta(hours=hours)
    window_start = now - timedelta(hours=window_hours)

    weights = [w for _, _, w in NORMAL_POOL]
    lines: list[tuple[datetime, str]] = []
    counts = {"normal": 0, "new_error": 0, "spike": 0}

    minute = start
    while minute <= now:
        in_window = minute >= window_start
        # 正常流量：按分钟随机条数
        for _ in range(max(0, int(rng.gauss(rate_per_minute, rate_per_minute / 3)))):
            level, template, _ = rng.choices(NORMAL_POOL, weights=weights, k=1)[0]
            lines.append((minute.replace(second=rng.randint(0, 59)), _render(template, rng)))
            counts["normal"] += 1

        if in_window and "spike" in injections:
            # 既有错误模板量级暴涨：每分钟额外若干条
            for _ in range(rng.randint(3, 9)):
                lines.append((minute.replace(second=rng.randint(0, 59)), _render(SPIKE_TEMPLATE, rng)))
                counts["spike"] += 1

        if in_window and "new-error" in injections:
            # 新错误：低频但持续，模拟"以前从没见过的故障"
            if rng.random() < 0.25:
                lines.append((minute.replace(second=rng.randint(0, 59)), _render(NEW_ERROR_TEMPLATE, rng)))
                counts["new_error"] += 1

        minute += timedelta(minutes=1)

    lines.sort(key=lambda item: item[0])
    return lines, counts


def main() -> int:
    # 中文 Windows 控制台是 GBK；不先安全化，打印中文/符号时会抛 UnicodeEncodeError，
    # 把要显示的结果变成看起来像崩溃。
    from ops.core import make_console_safe

    make_console_safe()

    parser = argparse.ArgumentParser(description="合成日志生成器（可注入已知异常）")
    parser.add_argument("--out", default="data/logs/app.log", help="输出文件路径")
    parser.add_argument("--hours", type=float, default=48, help="总共生成多少小时")
    parser.add_argument("--window-hours", type=float, default=24, help="最近多少小时算作当前窗口")
    parser.add_argument(
        "--inject", action="append", default=[], choices=["new-error", "spike"],
        help="注入已知异常，可重复",
    )
    parser.add_argument("--seed", type=int, default=20261005, help="随机种子（默认固定，保证可复现）")
    parser.add_argument("--rate-per-minute", type=float, default=7.0, help="正常流量每分钟条数")
    args = parser.parse_args()

    lines, counts = generate(
        hours=args.hours,
        window_hours=args.window_hours,
        injections=args.inject,
        seed=args.seed,
        rate_per_minute=args.rate_per_minute,
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as fh:
        for moment, text in lines:
            fh.write(f"{_stamp(moment)} {text}\n")

    print(f"已写入 {out}：{len(lines)} 行，跨度 {args.hours} 小时")
    print(f"  正常流量      : {counts['normal']} 行")
    print(f"  注入 新错误模板: {counts['new_error']} 行  （仅出现在最近 {args.window_hours} 小时）")
    print(f"  注入 量级暴涨  : {counts['spike']} 行  （仅出现在最近 {args.window_hours} 小时）")
    if not args.inject:
        print("  未注入异常 —— 这是干净基线，分析器不应对它告警")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
