"""工具调用预算闸门。

## 为什么必须有这个

给判断层工具之后，它的行为从"读一段固定输入然后回答"变成了**一个循环**：
调用工具 → 看结果 → 再调用 → ……。循环的代价是**无上界**：

* 一个路径写错的工具可以被反复重试，每轮都烧 token；
* 一个"再多查一点"的模型能在一次巡检里跑掉几十次调用；
* CPU 上的巡检是**每小时一次**的，失控会持续累积。

所以每次运行有一个硬预算：超过就直接把"预算用尽"作为工具结果返回给模型，
让它用已有信息作结论 —— **降级，不是失败**。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core import config as cfgmod

ENV_RUN_ID = "OPS_INVESTIGATE_RUN_ID"


def _budget_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg.get("investigate") or {}


def max_calls(cfg: dict[str, Any]) -> int:
    return int(_budget_cfg(cfg).get("max_calls_per_run", 6))


def audit_path(cfg: dict[str, Any], run_id: str) -> Path:
    """审计留痕文件路径（**纯计算**，不建目录）。

    建目录只发生在真正要写的那一刻 —— 否则"读一下计数"这种只读动作
    也会因为目录建不出来而抛异常，把整个工具调用打断。
    """
    return cfgmod.data_dir(cfg, "runs") / f"{run_id}.tools.ndjson"


def _counter_path(cfg: dict[str, Any], run_id: str) -> Path:
    """预算计数文件路径（同样是纯计算）。"""
    return cfgmod.data_dir(cfg, "state") / f"tool-calls-{run_id}.json"


def current_run_id(cfg: dict[str, Any]) -> str:
    """运行标识。

    由流水线在子进程环境里注入 —— 这样"这一次判断"用了多少次工具是可追溯的，
    而不是全机器共享一个计数器。
    """
    import os

    run_id = os.environ.get(ENV_RUN_ID) or _budget_cfg(cfg).get("run_id")
    return str(run_id) if run_id else "unknown-run"


def used(cfg: dict[str, Any], run_id: str | None = None) -> int:
    path = _counter_path(cfg, run_id or current_run_id(cfg))
    if not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data.get("calls", 0))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return 0


@dataclass
class Verdict:
    allowed: bool
    reason: str
    used: int = 0
    limit: int = 0


def check(cfg: dict[str, Any], run_id: str | None = None) -> Verdict:
    """这一轮还能不能再调工具。"""
    if not _budget_cfg(cfg).get("enabled", True):
        return Verdict(False, "调查工具未启用（config: investigate.enabled = false）")
    run_id = run_id or current_run_id(cfg)
    limit = max_calls(cfg)
    count = used(cfg, run_id)
    if count >= limit:
        return Verdict(
            False,
            f"工具调用预算已用尽（{count}/{limit}）。"
            f"请基于已经拿到的信息给出结论，不要再调用工具。",
            used=count, limit=limit,
        )
    return Verdict(True, f"还可以调用 {limit - count} 次（已用 {count}/{limit}）",
                   used=count, limit=limit)


@dataclass
class RecordResult:
    """一次调用的记账结果。

    `written=False` 是**必须被看见**的状态：预算计数写不进去，意味着硬上限失效。
    """

    count: int
    written: bool
    detail: str = ""


def record(cfg: dict[str, Any], name: str, args: dict[str, Any], result: Any,
           run_id: str | None = None) -> int:
    """记一次调用：既累加预算，也留审计留痕。

    留痕很重要 —— 事后要能回答"它查了什么、看了什么、为什么这么判断"。
    没有留痕，工具化就变成了"结论不知道从哪来的"。

    ⚠️ 这里的写入失败**必须被看见**。第一版把异常 `except OSError: pass` 吞掉了，
    结果判断层连着调了 5 次工具，每次预算行都显示"已用 0/6" —— 硬上限形同虚设，
    而日志里没有任何提示。静默失效的预算比没有预算更危险：你会以为有保护。
    """
    from ..core import http

    run_id = run_id or current_run_id(cfg)
    count = used(cfg, run_id) + 1
    written = True
    detail = ""
    try:
        # ensure_dir 也要包进来：目录建不出来（比如 state 位置被一个同名文件占了）
        # 同样是"预算无法累加"，不能让它抛异常把整个工具调用打断。
        path = _counter_path(cfg, run_id)
        cfgmod.ensure_dir(path.parent)
        path.write_text(
            json.dumps({"calls": count, "run_id": run_id}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        written = False
        detail = f"预算计数写入失败（{type(exc).__name__}: {exc}）"

    try:
        entry = {
            "ts": http.utc_now(),
            "tool": name,
            # 参数里可能包含很长的模式串，截断后再留痕
            "args": {k: (str(v)[:200]) for k, v in (args or {}).items()},
            "ok": bool(getattr(result, "ok", False)),
            "bytes": len(str(getattr(result, "text", "")).encode("utf-8")),
        }
        trail = audit_path(cfg, run_id)
        cfgmod.ensure_dir(trail.parent)
        with trail.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        if written:
            written, detail = False, f"审计留痕写入失败（{type(exc).__name__}: {exc}）"
    return RecordResult(count=count, written=written, detail=detail)


def probe_state_dir(cfg: dict[str, Any]) -> tuple[bool, str]:
    """自检：预算计数到底能不能落盘。

    `catalog` 会打印这个 —— 判断层第一次真实调用工具时预算静默失效，
    根子就是没人检查过这件事。目录建不出来也算不可用（不抛异常）。
    """
    try:
        target = cfgmod.ensure_dir(cfgmod.data_dir(cfg, "state"))
    except OSError as exc:
        return False, f"{cfgmod.data_dir(cfg, 'state')}（{type(exc).__name__}: {exc}）"

    probe = target / ".budget-write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True, str(target)
    except OSError as exc:
        return False, f"{target}（{type(exc).__name__}: {exc}）"
