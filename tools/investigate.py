"""调查工具的命令行入口 —— 判断层通过它已经有的 `pwsh` 工具来调用。

    python tools/investigate.py catalog
    python tools/investigate.py read-log  --source app --tail 300 [--pattern "ERROR"]
    python tools/investigate.py history   --task api_watch [--target worldtime] [--days 7]
    python tools/investigate.py probe     --target worldtime
    python tools/investigate.py report    --run-id 20261005T180459Z-api_watch-abc123

## 为什么要有这个 CLI（而不是只用 MCP）

`dsh headless` 的 profile 里没有 MCP 客户端的配置面（用 `--dump-config` 逐项查过），
所以 MCP 服务端今天挂不上去。但判断层**本来就有** `pwsh` 工具，
所以同源的实现以 CLI 形式立刻可用 —— 边界控制完全一样（白名单、只读、截断、预算），
因为它们共用 `ops/investigate/tools.py`。

## 退出码

* `0`：工具执行成功
* `1`：工具失败（参数错、未知目标、文件不存在……）—— 让模型能区分"没查到"和"查到了"
* `2`：预算用尽
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ops.core import config as cfgmod  # noqa: E402
from ops.core import make_console_safe  # noqa: E402
from ops.investigate import budget, tools as toolmod  # noqa: E402


def _emit(text: str, ok: bool, cfg: dict) -> int:
    """统一输出：正文 + 预算状态。

    预算状态要给模型看，否则它不知道还能查几次。
    **写盘失败必须显式喊出来** —— 不然"已用 0/6"会让人以为预算在工作，
    实际上每一次调用都没被记上（这是真实踩到的坑：判断层连调 5 次工具，
    预算行始终显示已用 0，而硬上限形同虚设）。
    """
    print(toolmod.truncate(text, toolmod.max_output_bytes(cfg)))
    verdict = budget.check(cfg)
    print()
    print(f"--- 工具预算：{verdict.reason}", file=sys.stderr)
    return 0 if ok else 1


def _report_record(record) -> None:
    if not record.written:
        print(f"--- ⚠️ {record.detail} —— 本轮工具预算未生效，调用次数无法限制",
              file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    make_console_safe()
    parser = argparse.ArgumentParser(description="判断层的只读调查工具")
    parser.add_argument("--config", default=str(ROOT / "config" / "config.toml"))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("catalog", help="列出允许访问的日志源 / 目标 / 任务，以及预算")

    p_log = sub.add_parser("read-log", help="读一个已配置日志源的尾部")
    p_log.add_argument("--source", required=True)
    p_log.add_argument("--tail", type=int, default=300, dest="tail_lines")
    p_log.add_argument("--pattern", default=None)

    p_hist = sub.add_parser("history", help="查历史运行与告警（只读）")
    p_hist.add_argument("--task", required=True)
    p_hist.add_argument("--target", default=None)
    p_hist.add_argument("--days", type=int, default=7)

    p_probe = sub.add_parser("probe", help="立刻重新探测一个已配置目标")
    p_probe.add_argument("--target", required=True)

    p_price = sub.add_parser("check-price", help="立刻重新抓一个已配置商品的当前价格")
    p_price.add_argument("--item", required=True)

    p_rep = sub.add_parser("report", help="读某次运行的报告")
    p_rep.add_argument("--run-id", required=True, dest="run_id")

    args = parser.parse_args(argv)
    cfg = cfgmod.load_config(args.config)

    if args.command == "catalog":
        info = toolmod.catalog(cfg)
        verdict = budget.check(cfg)
        print("# 允许访问的对象（只能按名字取，不接受路径）")
        print(f"- 日志源（read-log --source）：{info['configured_log_sources'] or '（无）'}")
        print(f"- 探测目标（probe --target）：{info['configured_targets'] or '（无）'}")
        print(f"- 商品（check-price --item）：{info['configured_price_items'] or '（无）'}")
        print(f"- 任务（history --task）：{info['available_tasks'] or '（无）'}")
        print(f"- 最近可查的 run_id（report --run-id）：{info['recent_run_ids'] or '（无）'}")
        print(f"- 工具预算：{verdict.reason}")
        print(f"- 输出上限：{toolmod.max_output_bytes(cfg)} 字节/次")
        # 自检预算到底能不能落盘 —— 判断层第一次真实调用工具时，
        # 预算静默失效就是因为没人检查过这件事
        writable, detail = budget.probe_state_dir(cfg)
        flag = "可写" if writable else "**不可写**（预算将无法累加！）"
        print(f"- 预算计数落盘：{flag} — {detail}")
        print(f"- 当前运行标识：{budget.current_run_id(cfg)}")
        return 0

    # 预算是所有工具共同的闸门
    verdict = budget.check(cfg)
    if not verdict.allowed:
        print(verdict.reason)
        print("--- 预算用尽：请基于已有信息给出结论", file=sys.stderr)
        return 2

    if args.command == "read-log":
        kwargs = {"source": args.source, "tail_lines": args.tail_lines}
        if args.pattern:
            kwargs["pattern"] = args.pattern
        result = toolmod.dispatch(cfg, "read_log", kwargs)
    elif args.command == "history":
        kwargs = {"task": args.task, "days": args.days}
        if args.target:
            kwargs["target"] = args.target
        result = toolmod.dispatch(cfg, "query_history", kwargs)
    elif args.command == "probe":
        result = toolmod.dispatch(cfg, "probe_target", {"target": args.target})
    elif args.command == "check-price":
        result = toolmod.dispatch(cfg, "check_price", {"item": args.item})
    else:
        result = toolmod.dispatch(cfg, "read_report", {"run_id": args.run_id})

    record = budget.record(cfg, args.command.replace("-", "_"), vars(args), result)
    exit_code = _emit(result.text, result.ok, cfg)
    _report_record(record)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
