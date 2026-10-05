"""流水线收尾：判断层 → 报告 → 裁决 → 告警 → 落库。

**为什么必须共享**：冷却、降级、幂等这些规则一旦复制到第二条流水线，
两边就会各自演化，最后出现"这条流水线的告警被抑制、那条没有"的不一致。
所有任务都走这里，规则只有一份。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import aibridge, config as cfgmod, http, judge, notify, report, store
from ..investigate import tools as investigate

PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts"


@dataclass
class FinalizeResult:
    exit_code: int
    status: str
    severity: str
    ai_status: str
    ai_error: str | None
    ai_degraded: bool
    alert_status: str
    alert_detail: str
    report_file: Path
    verdict: dict[str, Any]
    usage: aibridge.RunUsage = field(default_factory=aibridge.RunUsage)
    recovery_note: str | None = None


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def target_of_worst(analysis: judge.Analysis) -> str | None:
    """取最严重那条线索的目标，作为告警去重键的一部分。

    去重按"任务 + 目标 + 级别"隔离，否则 A 接口的告警会把 B 接口的告警压掉。
    """
    if not analysis.anomalies:
        return None
    worst = max(analysis.anomalies, key=lambda a: judge.SEVERITY_ORDER.get(a.severity_hint, 0))
    return None if worst.target == "-" else worst.target


def finalize(
    cfg: dict[str, Any],
    conn: Any,
    *,
    task: str,
    run_id: str,
    analysis: judge.Analysis,
    run_doc: dict[str, Any],
    prompt_name: str,
    session_policy: str = "fresh",
    item_label: str = "目标",
    value_label: str = "延迟",
    value_suffix: str = "ms",
    always_ai: bool = False,
    dry_run: bool = False,
    no_ai: bool = False,
    log_lines: list[str] | None = None,
) -> FinalizeResult:
    ai_cfg = cfg.get("ai") or {}
    alert_cfg = cfg.get("alert") or {}
    log_lines = list(log_lines or [])
    ai_notes: list[str] = []

    verdict: dict[str, Any] | None = None
    ai_status = "skipped"
    ai_called = False
    ai_error: str | None = None
    usage = aibridge.RunUsage()
    recovery_note: str | None = None

    # "判断层本来该干活吗"：有异常，或任务要求每轮都分析（例如日报）
    needed = bool(analysis.anomalies) or always_ai
    want_ai = (not no_ai) and bool(ai_cfg.get("enabled", True)) and needed

    if want_ai:
        max_calls = int(ai_cfg.get("max_calls_per_day", 30))
        used_today = store.ai_calls_today(conn)
        if used_today >= max_calls:
            ai_status = "budget_exceeded"
            ai_notes.append(f"今日 AI 调用已达上限 {max_calls}，本轮降级为规则判定")
        else:
            bridge = aibridge.AIBridge(cfg)
            # 把"能用什么只读工具、能碰哪些对象、还剩几次预算"附在输入里。
            # 放在这里（生产路径）而不是桥接层：评测用的是冻结夹具，
            # 注入活的配置/数据库内容会让评测不再可复现。
            run_doc = {**run_doc, "investigation": investigate.tool_context(cfg, run_id)}
            result = bridge.call(
                task=task,
                run_id=run_id,
                template_path=PROMPT_DIR / f"{prompt_name}.md",
                run_doc=run_doc,
                use_session=(session_policy == "persistent"),
            )
            ai_called = True
            usage = result.usage
            if result.ok and result.verdict:
                verdict = result.verdict
                ai_status = "ok"
                ai_notes.append(
                    f"AI 判断完成（{result.duration_s}s，会话 {result.session_id or '新建'}）"
                )
                if result.events_path:
                    ai_notes.append(f"事件流：{result.events_path}")
                if usage.session_total_tokens:
                    ai_notes.append(
                        f"token：输入 {usage.input_tokens}"
                        f"（cache 读 {usage.cache_read_tokens}"
                        f" / 写 {usage.cache_write_tokens}）"
                        f"，输出 {usage.output_tokens}，会话累计 {usage.session_total_tokens}"
                    )
                # 宿主侧的工具调用计数：不依赖子进程写状态文件，所以任何环境下都准
                used_tools = int((result.tool_calls or {}).get("investigate") or 0)
                if used_tools:
                    limit = int((cfg.get("investigate") or {}).get("max_calls_per_run", 6))
                    note = f"判断层调用了 {used_tools} 次只读调查工具"
                    if used_tools > limit:
                        note += (
                            f" —— **超过预算 {limit}**：说明子进程沙箱写不进状态文件，"
                            f"CLI 的硬拦未生效；此数来自宿主侧事件流统计"
                        )
                    ai_notes.append(note)
            else:
                ai_status = "failed"
                ai_error = result.error or "未知原因"
                ai_notes.append(f"AI 判断失败，已降级：{ai_error}")
                if result.stderr_tail:
                    log_lines.append(f"AI stderr 尾部：{result.stderr_tail[-300:]}")

    ai_degraded = False
    if verdict is None:
        verdict = judge.rule_verdict(run_id, task, analysis)
        verdict["ai_degraded"] = needed
        ai_degraded = needed
        if not needed and ai_status == "skipped":
            ai_status = "not_needed"

    # 契约加固：run_id/task 以本地为准，不信任模型回填的值
    verdict["run_id"] = run_id
    verdict["task"] = task
    store.record_verdict(conn, verdict, model=ai_cfg.get("profile"))

    # 先算出报告路径（不写文件）：告警里要带这个路径，但报告正文得等
    # 恢复通知也算完之后再落盘 —— 否则恢复记录不会出现在报告里。
    report_file = report.report_path(cfg, run_id, task)
    run_doc["artifacts"] = [str(report_file)]

    # ---------------------------------------------------------------- 裁决
    severity = str(verdict.get("severity", "info"))
    confidence = float(verdict.get("confidence") or 0.0)
    target = target_of_worst(analysis)
    key = judge.dedup_key(task, target, severity)
    cooldown = int(verdict.get("cooldown_minutes") or alert_cfg.get("default_cooldown_minutes", 60))
    cooldown = max(cooldown, int(alert_cfg.get("min_cooldown_minutes", 15)))

    decision = judge.decide(
        severity=severity,
        should_alert=bool(verdict.get("should_alert")),
        confidence=confidence,
        now_local=datetime.now(timezone.utc).astimezone(),
        last_sent_at=parse_ts(store.last_alert_at(conn, key)),
        cooldown_minutes=cooldown,
        quiet_hours=str(alert_cfg.get("quiet_hours", "")),
    )

    notifier = notify.Notifier(cfg, dry_run=dry_run)
    send_status = "suppressed"
    detail = decision.reason
    if decision.send:
        markdown = notify.render_markdown(
            headline=str(verdict.get("headline", "")),
            severity=severity,
            # 邮件只放人话三件套：发生了什么 / 意味着什么 / 该做什么。
            # evidence 是给报告文件用的技术细节，不再往邮件里倒 ——
            # 满屏 samples:/baseline.xxx 对收邮件的人是灾难。
            plain_summary=verdict.get("plain_summary"),
            impact=verdict.get("impact"),
            action=verdict.get("suggested_action") or verdict.get("action"),
            evidence=list(verdict.get("evidence") or []),
            metrics=analysis.metrics,
            item_label=item_label,
            report_path=str(report_file),
            ai_degraded=ai_degraded,
        )
        send_result = notifier.send(
            subject=f"{notify.SEVERITY_LABEL.get(severity, severity)} {verdict.get('headline')}",
            markdown=markdown,
        )
        send_status, detail = send_result.status, send_result.detail
        if send_result.status in {"dry-run", "failed"}:
            notifier.write_local_log(f"[{severity}] {verdict.get('headline')}\n{markdown}")
        log_lines.append(f"告警发送：{send_result.status}（{send_result.detail}）")
    else:
        log_lines.append(f"告警被抑制：{decision.reason}")

    store.record_alert(
        conn, run_id=run_id, task=task, target=target, severity=severity,
        channel=notifier.primary, dedup_key=key, send_status=send_status,
        detail=detail, suppressed_reason=None if decision.send else decision.reason,
    )

    # ---------------------------------------------------------------- 恢复通知
    # 设计稿 §8：故障消失时发一条"已恢复"，避免"只知道挂了、不知道好了"。
    # 判据：本轮没有任何异常线索，且判断层给的是 info。
    healthy = not analysis.anomalies and severity == "info"
    if healthy and not dry_run:
        pending = store.unresolved_alerts(conn, task)
        if pending:
            quiet = judge.in_quiet_hours(
                datetime.now(timezone.utc).astimezone(), str(alert_cfg.get("quiet_hours", ""))
            )
            if quiet:
                # 好消息也不该半夜吵人；保持未恢复状态，等出了静默时段再通知
                recovery_note = f"{len(pending)} 条告警已恢复，但处于静默时段，暂不通知"
                log_lines.append(recovery_note)
            else:
                oldest = pending[-1]
                lines = [
                    f"✅ **已恢复** {task}",
                    f"> 恢复时间：{http.utc_now()}",
                    f"> 本轮：{item_label} {analysis.metrics.get('ok')}/"
                    f"{analysis.metrics.get('targets')} 正常，无异常线索",
                    f"> 之前告警（{len(pending)} 条未恢复中最早一条）："
                    f"[{oldest.get('severity')}] {oldest.get('target') or '-'}",
                ]
                if oldest.get("detail"):
                    lines.append(f"> 原始详情：{str(oldest['detail'])[:160]}")
                markdown = "\n".join(lines)
                send_result = notifier.send(subject=f"[已恢复] {task}", markdown=markdown)
                if send_result.status in {"sent", "dry-run"}:
                    store.mark_alerts_resolved(conn, [int(a["id"]) for a in pending])
                    recovery_note = (
                        f"已发出恢复通知（{len(pending)} 条告警标记为已恢复，"
                        f"通道 {send_result.channel} 状态 {send_result.status}）"
                    )
                    log_lines.append(recovery_note)
                else:
                    recovery_note = f"恢复通知发送失败，保持未恢复状态：{send_result.detail}"
                    log_lines.append(recovery_note)

    # ---------------------------------------------------------------- 落盘
    # 放在恢复通知之后：这样报告里的附注包含本轮的**全部**动作（含"已恢复"）。
    report.write_run_report(
        cfg, task=task, run_id=run_id, analysis=analysis.to_dict(), verdict=verdict,
        item_label=item_label, value_label=value_label, value_suffix=value_suffix,
        extra_lines=ai_notes + log_lines,
    )
    report.write_run_doc(cfg, run_id, run_doc)

    status = "degraded" if (analysis.anomalies or ai_degraded) else "ok"
    store.finish_run(
        conn, run_id, finished_at=http.utc_now(), status=status,
        items_ok=int(analysis.metrics.get("ok", 0)),
        items_failed=int(analysis.metrics.get("failed", 0)),
        ai_called=1 if ai_called else 0, ai_status=ai_status,
        ai_degraded=1 if ai_degraded else 0, exit_code=0,
        # token 分列存：cache 读的单价远低于普通输入，混在一起就算不出真实成本
        tokens_in=usage.input_tokens or None,
        tokens_out=usage.output_tokens or None,
        cache_read_tokens=usage.cache_read_tokens or None,
        cache_write_tokens=usage.cache_write_tokens or None,
        summary=str(verdict.get("headline", ""))[:500],
    )

    return FinalizeResult(
        exit_code=0, status=status, severity=severity, ai_status=ai_status,
        ai_error=ai_error, ai_degraded=ai_degraded, alert_status=send_status,
        alert_detail=detail, report_file=report_file, verdict=verdict,
        usage=usage, recovery_note=recovery_note,
    )


def report_failure(conn: Any, run_id: str, exc: BaseException) -> None:
    """任务抛异常时的统一收尾：**必须落库**，绝不静默。"""
    store.finish_run(
        conn, run_id, finished_at=http.utc_now(), status="failed", exit_code=3,
        summary=f"{type(exc).__name__}: {exc}"[:500],
    )


def ai_note(result: FinalizeResult) -> str:
    """stdout 用的一行 AI 状态。

    无人值守时运维第一眼看的是 stdout（落进 launcher.log），
    所以 AI 失败的原因必须打在这里，不能只躺在报告附注里。
    """
    note = f"ai={result.ai_status}"
    if result.ai_error:
        note += f"（{result.ai_error[:150]}）"
    return note
