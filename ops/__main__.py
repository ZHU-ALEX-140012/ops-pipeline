"""ops-pipeline 入口。

用法（幂等：同一条命令重复执行是安全的，去重与冷却由存储层和判定层负责）：

    python -m ops --task api_watch --once
    python -m ops --task api_watch --once --dry-run      # 只落本地日志，不真发告警
    python -m ops --task api_watch --once --no-ai        # 强制走规则层兜底
    python -m ops --list

计划任务调用的就是 `--once` 形式：无状态、可重入、可手动补跑。
"""

from __future__ import annotations

import argparse
import sys
import traceback
from typing import Any

from .core import config as cfgmod
from .core import make_console_safe
from .tasks import api_watch, daily_report, feedback_scan, heartbeat, log_scan, regression

TASKS: dict[str, Any] = {
    "api_watch": api_watch.run,
    "regression": regression.run,
    "log_scan": log_scan.run,
    "daily_report": daily_report.run,
    "feedback_scan": feedback_scan.run,
    "heartbeat": heartbeat.run,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ops",
        description="个人运维流水线：确定性采集 + AI 判断 + 去重告警",
    )
    parser.add_argument("--task", choices=sorted(TASKS), help="要执行的任务")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config/config.toml）")
    parser.add_argument(
        "--once",
        action="store_true",
        help="跑一轮就退出（计划任务用；也是唯一的运行模式，保留此参数是为了语义明确）",
    )
    parser.add_argument("--dry-run", action="store_true", help="不发真实告警，改为落本地日志")
    parser.add_argument("--no-ai", action="store_true", help="跳过 AI 判断层，强制规则层兜底")
    parser.add_argument(
        "--override",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="临时覆盖配置项，可重复。例：--override regression.variant=v1",
    )
    parser.add_argument("--list", action="store_true", help="列出可用任务")
    parser.add_argument("--doctor", action="store_true",
                        help="自检：配置 / 凭据 / 连通性 / 存储 / 判断层 / 计划任务")
    parser.add_argument("--doctor-offline", action="store_true",
                        help="自检时跳过网络探测（快速模式）")
    return parser


def main(argv: list[str] | None = None) -> int:
    # 必须在任何打印之前：避免 emoji 之类打不出来时抛 UnicodeEncodeError，
    # 那会把"要报告的失败"变成"看起来像崩溃"
    make_console_safe()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.doctor:
        from .core import doctor

        try:
            cfg = cfgmod.load_config(args.config)
        except cfgmod.ConfigError as exc:
            print(f"配置错误：{exc}", file=sys.stderr)
            return 2
        checks = doctor.run_doctor(cfg, offline=args.doctor_offline)
        print(doctor.render(checks))
        # 有 FAIL 就返回非零 —— 自检要能被脚本/计划任务消费
        return 1 if doctor.summarize(checks)[doctor.FAIL] else 0

    if args.list or not args.task:
        print("可用任务：")
        for name in sorted(TASKS):
            print(f"  - {name}")
        return 0 if args.list else 2

    try:
        cfg = cfgmod.load_config(args.config)
        cfgmod.apply_overrides(cfg, args.override)
    except cfgmod.ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    task = TASKS[args.task]
    try:
        return int(
            task(cfg, dry_run=args.dry_run, no_ai=args.no_ai)
        )
    except Exception:  # noqa: BLE001 - 入口兜底：把栈打出来，返回非零让计划任务能发现
        traceback.print_exc()
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
