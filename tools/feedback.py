"""人工反馈的命令行入口。

    python tools/feedback.py record --run-id <id> --vote up [--note "确实挂了"]
    python tools/feedback.py list [--limit 20]
    python tools/feedback.py stats
    python tools/feedback.py scan            # 去邮箱收回复（IMAP）

## 为什么要有人工反馈

离线黄金集能回答"模型在 19 个场景上多准"，回答不了"**它在你这儿多准**"。
真实环境里的误报往往长得跟黄金集里的不一样 —— 只有收到邮件的人知道。

代价是样本**天生有偏**：你更可能去反馈判错的那几条。
所以 `stats` 打印的是"人工标注准确率"，只能看趋势、找反例，
**不能当成模型在全部告警上的准确率** —— 输出里会明说这一点。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ops.core import config as cfgmod  # noqa: E402
from ops.core import feedback as fb  # noqa: E402
from ops.core import make_console_safe, store  # noqa: E402


def _connect(cfg: dict):
    return store.connect(cfgmod.data_dir(cfg, "ops.db"))


def cmd_record(cfg: dict, args) -> int:
    conn = _connect(cfg)
    try:
        row = conn.execute("SELECT id FROM run WHERE id=?", (args.run_id,)).fetchone()
        if not row:
            print(f"[FAIL] 找不到 run_id：{args.run_id}")
            return 1
        store.record_feedback(conn, run_id=args.run_id, vote=args.vote,
                              source="cli", note=args.note)
        print(f"[OK] 已记录：{args.run_id} → {args.vote}")
        stats = store.feedback_stats(conn)
        print(f"     累计：对 {stats['up']} / 错 {stats['down']}")
        return 0
    finally:
        conn.close()


def cmd_list(cfg: dict, args) -> int:
    conn = _connect(cfg)
    try:
        stats = store.feedback_stats(conn)
        if not stats["recent"]:
            print("还没有任何反馈。收到告警邮件后直接回复「对」或「错」即可。")
            return 0
        print(f"{'时间':<21} {'判定':<5} {'来源':<7} run_id")
        for item in stats["recent"][: args.limit]:
            print(f"{item['created_at'][:19]:<21} {item['vote']:<5} "
                  f"{item['source']:<7} {item['run_id']}")
            if item.get("note"):
                print(f"    └─ {item['note']}")
        return 0
    finally:
        conn.close()


def cmd_stats(cfg: dict, args) -> int:
    conn = _connect(cfg)
    try:
        stats = store.feedback_stats(conn)
        print(f"人工反馈：对 {stats['up']} / 错 {stats['down']}（共 {stats['total']} 条）")
        if stats["accuracy"] is not None:
            print(f"人工标注准确率：{stats['accuracy'] * 100:.1f}%")
        print()
        print("⚠️ 这个数字**天生有偏**：你更可能去反馈判错的那几条。")
        print("   它只用来看趋势、找反例，不能当成模型在全部告警上的准确率。")
        return 0
    finally:
        conn.close()


def cmd_scan(cfg: dict, args) -> int:
    from ops.tasks import feedback_scan

    return feedback_scan.run(cfg, dry_run=args.dry_run)


def main(argv: list[str] | None = None) -> int:
    make_console_safe()
    parser = argparse.ArgumentParser(description="人工反馈：记录 / 查看 / 从邮箱收回信")
    parser.add_argument("--config", default=str(ROOT / "config" / "config.toml"))
    sub = parser.add_subparsers(dest="command", required=True)

    p_rec = sub.add_parser("record", help="手动记录一条反馈")
    p_rec.add_argument("--run-id", required=True, dest="run_id")
    p_rec.add_argument("--vote", required=True, choices=("up", "down"))
    p_rec.add_argument("--note", default=None)

    p_list = sub.add_parser("list", help="列出最近的反馈")
    p_list.add_argument("--limit", type=int, default=20)

    sub.add_parser("stats", help="汇总人工标注准确率")

    p_scan = sub.add_parser("scan", help="连邮箱收回复（IMAP）")
    p_scan.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    cfg = cfgmod.load_config(args.config)

    if args.command == "record":
        return cmd_record(cfg, args)
    if args.command == "list":
        return cmd_list(cfg, args)
    if args.command == "stats":
        return cmd_stats(cfg, args)
    return cmd_scan(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
