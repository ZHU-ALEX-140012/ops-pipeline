"""报告渲染（确定性，永不依赖 AI）。

即使判断层完全不可用，也必须有一份人能读懂的报告 —— 这是降级能力的一部分。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config as cfgmod


def render_run_markdown(
    *,
    task: str,
    run_id: str,
    analysis: dict[str, Any],
    verdict: dict[str, Any] | None,
    extra_lines: list[str] | None = None,
    item_label: str = "目标",
    value_label: str = "延迟",
    value_suffix: str = "ms",
) -> str:
    metrics = analysis.get("metrics") or {}
    lines: list[str] = [
        f"# {task} 巡检报告",
        "",
        f"- 运行 ID：`{run_id}`",
        f"- {item_label}：{metrics.get('ok')}/{metrics.get('targets')} 正常，"
        f"失败 {metrics.get('failed', 0)}",
    ]
    # 延迟对日志分析没有意义，所以这一行按数据是否存在决定打不打
    if metrics.get("p50_ms") is not None:
        lines.append(
            f"- 延迟：p50 {metrics.get('p50_ms')}ms / p95 {metrics.get('p95_ms')}ms / "
            f"max {metrics.get('max_ms')}ms"
        )
    if metrics.get("lines_current") is not None:
        lines.append(
            f"- 日志：本轮 {metrics.get('lines_current')} 行，"
            f"错误行 {metrics.get('error_lines_current')} 行，"
            f"模板 {metrics.get('templates')} 个"
        )

    if verdict:
        lines += [
            "",
            "## 结论",
            "",
            f"- 级别：**{verdict.get('severity')}**"
            + ("（规则层兜底，未经 AI 分析）" if verdict.get("ai_degraded") else ""),
            f"- 摘要：{verdict.get('headline')}",
        ]
        root_cause = verdict.get("root_cause_hypothesis") or verdict.get("root_cause")
        if root_cause:
            lines.append(f"- 根因假设：{root_cause}")
        if verdict.get("confidence") is not None:
            lines.append(f"- 置信度：{verdict.get('confidence')}")

    anomalies = analysis.get("anomalies") or []
    lines += ["", f"## 异常线索（{len(anomalies)}）", ""]
    if anomalies:
        lines += ["| 类型 | 目标 | 说明 |", "|---|---|---|"]
        for item in anomalies:
            lines.append(
                f"| {item.get('kind')} | {item.get('target')} | {item.get('detail')} |"
            )
    else:
        lines.append("无。")

    evidence = (verdict or {}).get("evidence") or []
    if evidence:
        lines += ["", "## 证据", ""]
        lines += [f"- {item}" for item in evidence]

    lines += ["", f"## 逐{item_label}明细", "",
              f"| {item_label} | 状态 | {value_label} | 状态码 | 说明 |", "|---|---|---|---|---|"]
    for sample in analysis.get("samples") or []:
        ok = "OK" if sample.get("ok") else "FAIL"
        value = sample.get("value_num")
        shown = "" if value is None else f"{value}{value_suffix}"
        lines.append(
            f"| {sample.get('target')} | {ok} | {shown} | "
            f"{sample.get('status_code') if sample.get('status_code') is not None else ''} | "
            f"{sample.get('error') or ''} |"
        )

    baseline = analysis.get("baseline") or {}
    if baseline:
        lines += ["", "## 基线对照", "", f"| {item_label} | 基线样本 | p50 | p95 | 连续失败 | 24h 失败 |", "|---|---|---|---|---|---|"]
        for target, info in baseline.items():
            lines.append(
                f"| {target} | {info.get('samples')} | {info.get('p50_ms')}ms | "
                f"{info.get('p95_ms')}ms | {info.get('consecutive_failures')} | "
                f"{info.get('failures_24h')} |"
            )

    notes = analysis.get("notes") or []
    if notes:
        lines += ["", "## 备注", ""]
        lines += [f"- {note}" for note in notes]

    if extra_lines:
        lines += ["", "## 附注", ""] + [f"- {line}" for line in extra_lines]

    lines += ["", "---", "", "_由 ops-pipeline 自动生成（确定性渲染，不依赖 AI）_", ""]
    return "\n".join(lines)


def _local_day(run_id: str) -> str:
    """把 run_id 里的 UTC 时间戳换算成本地日期，用于报告分目录。

    run_id 形如 20261004T120500Z-api_watch。存储一律 UTC，但人是按本地日期翻报告的，
    所以这里显式转换 —— 且转换是确定性的（同一个 run_id 永远落在同一个目录）。
    """
    stamp = run_id.split("-", 1)[0]
    try:
        moment = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return moment.astimezone().strftime("%Y-%m-%d")
    except ValueError:
        return "unknown"


def report_path(cfg: dict[str, Any], run_id: str, task: str, suffix: str = "md") -> Path:
    directory = cfgmod.ensure_dir(cfgmod.data_dir(cfg, "reports", _local_day(run_id)))
    return directory / f"{task}-{run_id}.{suffix}"


def write_run_report(
    cfg: dict[str, Any],
    *,
    task: str,
    run_id: str,
    analysis: dict[str, Any],
    verdict: dict[str, Any] | None,
    extra_lines: list[str] | None = None,
    item_label: str = "目标",
    value_label: str = "延迟",
    value_suffix: str = "ms",
) -> Path:
    path = report_path(cfg, run_id, task)
    path.write_text(
        render_run_markdown(
            task=task, run_id=run_id, analysis=analysis, verdict=verdict,
            extra_lines=extra_lines, item_label=item_label,
            value_label=value_label, value_suffix=value_suffix,
        ),
        encoding="utf-8",
    )
    return path


def write_run_doc(cfg: dict[str, Any], run_id: str, doc: dict[str, Any]) -> Path:
    """把 run.json 原始留痕落盘（DB 坏了也不丢证据）。"""
    path = cfgmod.ensure_dir(cfgmod.data_dir(cfg, "runs")) / f"{run_id}.run.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def render_daily_markdown(digest: dict[str, Any]) -> str:
    """确定性日报渲染 —— AI 不可用时也要有一份能看的日报（降级能力）。"""
    window = digest.get("window") or {}
    totals = digest.get("totals") or {}
    lines: list[str] = [
        "# ops-pipeline 日报",
        "",
        f"- 统计窗口：{window.get('start')} → {window.get('end')}"
        f"（{window.get('hours')} 小时）",
        f"- 运行总次数：**{totals.get('runs', 0)}**"
        f"（ok {totals.get('ok_runs', 0)} / degraded {totals.get('degraded_runs', 0)}"
        f" / failed {totals.get('failed_runs', 0)}）",
        f"- 告警：已投递 {totals.get('alerts_sent', 0)}，抑制 {totals.get('alerts_suppressed', 0)}，"
        f"发送失败 {totals.get('alerts_failed', 0)}",
        f"- 未恢复告警：**{totals.get('unresolved', 0)}**",
    ]
    tokens = digest.get("tokens") or {}
    if tokens.get("input") or tokens.get("output"):
        lines.append(
            f"- AI token：输入 {tokens.get('input', 0)}"
            f"（cache 读 {tokens.get('cache_read', 0)} / 写 {tokens.get('cache_write', 0)}）"
            f"，输出 {tokens.get('output', 0)}"
        )

    tasks = digest.get("tasks") or []
    lines += ["", "## 各任务运行情况", "",
              "| 任务 | 运行 | ok | degraded | failed | AI 调用 | AI 失败 | 最近一次 |",
              "|---|---|---|---|---|---|---|---|"]
    if tasks:
        for task in tasks:
            lines.append(
                f"| {task.get('task')} | {task.get('runs')} | {task.get('ok_runs')} | "
                f"{task.get('degraded_runs')} | {task.get('failed_runs')} | "
                f"{task.get('ai_calls')} | {task.get('ai_failures')} | {task.get('last_run')} |"
            )
    else:
        lines.append("| （窗口内没有运行记录） | | | | | | | |")

    unresolved = digest.get("unresolved") or []
    lines += ["", f"## 未恢复告警（{len(unresolved)}）", ""]
    if unresolved:
        for item in unresolved[:20]:
            lines.append(
                f"- **{item.get('task')}** / {item.get('target') or '-'}"
                f"｜{item.get('severity')}｜{str(item.get('detail') or '')[:120]}"
                f"｜{item.get('sent_at')}"
            )
    else:
        lines.append("无 —— 所有告警都已恢复。")

    events = digest.get("recent_alerts") or []
    lines += ["", f"## 窗口内的告警记录（{len(events)}）", "",
              "| 时间 | 任务 | 级别 | 状态 | 说明 |", "|---|---|---|---|---|"]
    if events:
        for item in events[:30]:
            lines.append(
                f"| {item.get('sent_at')} | {item.get('task')} | {item.get('severity')} | "
                f"{item.get('send_status')} | {str(item.get('detail') or '')[:80]} |"
            )
    else:
        lines.append("| （无） | | | | |")

    notes = digest.get("notes") or []
    if notes:
        lines += ["", "## 备注", ""] + [f"- {note}" for note in notes]

    lines += ["", "---", "", "_由 ops-pipeline 确定性渲染（AI 不可用时的降级版本）_", ""]
    return "\n".join(lines)
