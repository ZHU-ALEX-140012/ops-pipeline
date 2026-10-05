"""每日日报 —— 设计方案 §12 的「报告统一入口」。

它和另外三条流水线的**语义不同**，所以没有走 pipeline.finalize：

* `api_watch` / `regression` / `log_scan` 是**异常告警**：没异常就不出声；
* `daily_report` 是**定期汇总**：不管有没有异常都发一份，让"今天一切正常"也是明确信息。

复用的部分：`aibridge`（判断层）、`notify`（投递）、`report`（渲染）、`store`（存储）。
判断层使用 **D2 决策的固定会话**（有记忆），所以它能指出跨天的趋势。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..core import aibridge, config as cfgmod, http, notify, pipeline, report, store

TASK = "daily_report"


def _run_id(started_at: str) -> str:
    return f"{started_at.replace('-', '').replace(':', '')}-{TASK}-{uuid.uuid4().hex[:6]}"


def build_digest(conn: Any, *, hours: float, now: datetime) -> dict[str, Any]:
    """把窗口内的运行/告警/token 汇总成结构化日报素材。"""
    since_dt = now - timedelta(hours=hours)
    since = since_dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    tasks = store.run_stats_since(conn, since)
    raw_tokens = store.token_stats_since(conn, since)
    tokens = {
        "input": raw_tokens["tokens_in"],
        "output": raw_tokens["tokens_out"],
        "cache_read": raw_tokens["cache_read"],
        "cache_write": raw_tokens["cache_write"],
    }
    by_status = {row["send_status"]: int(row["n"]) for row in store.alert_stats_since(conn, since)}
    unresolved = store.unresolved_alerts_all(conn)
    recent = store.alerts_since(conn, hours=int(hours) if hours >= 1 else 1)

    totals = {
        "runs": sum(int(t.get("runs") or 0) for t in tasks),
        "ok_runs": sum(int(t.get("ok_runs") or 0) for t in tasks),
        "degraded_runs": sum(int(t.get("degraded_runs") or 0) for t in tasks),
        "failed_runs": sum(int(t.get("failed_runs") or 0) for t in tasks),
        "ai_calls": sum(int(t.get("ai_calls") or 0) for t in tasks),
        "ai_failures": sum(int(t.get("ai_failures") or 0) for t in tasks),
        "alerts_sent": by_status.get("sent", 0),
        "alerts_suppressed": by_status.get("suppressed", 0),
        "alerts_failed": by_status.get("failed", 0),
        "alerts_dry_run": by_status.get("dry-run", 0),
        "unresolved": len(unresolved),
    }

    notes: list[str] = []
    if totals["runs"] == 0:
        notes.append(
            "窗口内**没有任何运行记录** —— 可能是采集断了、计划任务没触发，"
            "或统计窗口设得太短。这种情况日报也要发出来，不能静默。"
        )
    if totals["alerts_failed"]:
        notes.append(
            f"有 {totals['alerts_failed']} 条告警**发送失败** —— 通知渠道本身可能有问题，"
            "这比业务异常更该优先处理。"
        )
    if totals["alerts_dry_run"]:
        notes.append(
            f"有 {totals['alerts_dry_run']} 条告警因为**通道未配置**没有真正投递出去"
            "（只落了本地日志）。这类告警不计入「未恢复」——没通知过就谈不上恢复，"
            "但也意味着你当时收不到通知。"
        )
    if totals["ai_failures"]:
        notes.append(f"有 {totals['ai_failures']} 次 AI 判断失败（已降级为规则判定）。")
    for task in tasks:
        if task["task"] == TASK:
            continue
        if task["failed_runs"]:
            notes.append(f"{task['task']} 有 {task['failed_runs']} 次运行失败。")

    return {
        "schema": "ops/run@1",
        "task": TASK,
        "window": {
            "start": since,
            "end": now.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "hours": hours,
        },
        "totals": totals,
        "tasks": tasks,
        "tokens": tokens,
        "unresolved": unresolved,
        "recent_alerts": recent,
        "notes": notes,
    }


def run(cfg: dict[str, Any], *, dry_run: bool = False, no_ai: bool = False) -> int:
    task_cfg = cfg.get(TASK) or {}
    ai_cfg = cfg.get("ai") or {}
    alert_cfg = cfg.get("alert") or {}
    hours = float(task_cfg.get("window_hours", 24))

    started_at = http.utc_now()
    run_id = _run_id(started_at)
    conn = store.connect(cfgmod.data_dir(cfg, "ops.db"))
    store.start_run(conn, run_id, TASK, started_at)

    log_lines: list[str] = []
    try:
        now = datetime.now().astimezone()
        digest = build_digest(conn, hours=hours, now=now)
        digest["run_id"] = run_id
        run_doc = dict(digest, started_at=started_at, finished_at=http.utc_now(),
                       status="ok" if not digest["totals"]["unresolved"] else "degraded")
        report.write_run_doc(cfg, run_id, run_doc)

        # ---------------------------------------------------------- 判断层（固定会话）
        ai_notes: list[str] = []
        verdict: dict[str, Any] | None = None
        ai_status = "skipped"
        ai_called = False
        ai_error: str | None = None
        usage = aibridge.RunUsage()
        body: str | None = None

        policy = (ai_cfg.get("session_policy") or {}).get(TASK, "persistent")
        if not no_ai and bool(ai_cfg.get("enabled", True)):
            bridge = aibridge.AIBridge(cfg)
            result = bridge.call(
                task=TASK,
                run_id=run_id,
                template_path=Path(__file__).resolve().parents[1] / "prompts" / "daily_report.md",
                run_doc=run_doc,
                use_session=(policy == "persistent"),
            )
            ai_called = True
            usage = result.usage
            if result.ok and result.verdict:
                verdict = result.verdict
                ai_status = "ok"
                if result.extra.get("note"):
                    ai_notes.append(str(result.extra["note"]))
                ai_notes.append(
                    f"AI 日报完成（{result.duration_s}s，"
                    f"会话 {'沿用固定会话' if policy == 'persistent' else '新会话'}"
                    f" {result.session_id or '-'}）"
                )
                # 邮件正文优先用 AI 写的那份（它就是给人读的）
                if result.ai_report_path and result.ai_report_path.is_file():
                    body = result.ai_report_path.read_text(encoding="utf-8")
                    ai_notes.append(f"日报正文取自 {result.ai_report_path.name}")
            else:
                ai_status = "failed"
                ai_error = result.error or "未知原因"
                ai_notes.append(f"AI 日报失败，改用确定性渲染：{ai_error}")

        if verdict is None:
            verdict = {
                "schema": "ops/verdict@1",
                "run_id": run_id,
                "task": TASK,
                "severity": "notice" if digest["totals"]["unresolved"] else "info",
                "should_alert": digest["totals"]["runs"] > 0,
                "headline": (
                    f"日报：{digest['totals']['runs']} 次运行，"
                    f"{digest['totals']['unresolved']} 条未恢复告警"
                ),
                "root_cause_hypothesis": "（规则层兜底：未经 AI 分析）",
                "evidence": [f"{n}" for n in digest["notes"][:5]],
                "confidence": 1.0,
                "cooldown_minutes": None,
                "ai_degraded": True,
            }
        verdict["run_id"] = run_id
        verdict["task"] = TASK
        store.record_verdict(conn, verdict, model=ai_cfg.get("profile"))

        if body is None:
            body = report.render_daily_markdown(digest)

        # ---------------------------------------------------------- 投递（每天最多一次）
        local_day = now.strftime("%Y-%m-%d")
        dedup_key = f"{TASK}:-:digest:{local_day}"
        already = store.last_alert_at(conn, dedup_key)
        severity = str(verdict.get("severity", "info"))

        if already:
            send_status, detail = "suppressed", f"今天（{local_day}）的日报已经发过，不重复发送"
            log_lines.append(detail)
        elif not bool(verdict.get("should_alert", True)):
            send_status, detail = "suppressed", "判断层认为不该发送（例如窗口内没有运行记录）"
            log_lines.append(detail)
        else:
            notifier = notify.Notifier(cfg, dry_run=dry_run)
            result = notifier.send(
                subject=f"[ops-pipeline 日报] {local_day} · {verdict.get('headline', '')}",
                markdown=body,
            )
            send_status, detail = result.status, result.detail
            if result.status in {"dry-run", "failed"}:
                notifier.write_local_log(f"[日报 {local_day}] {verdict.get('headline')}\n{body}")
            log_lines.append(f"日报投递：{result.status}（{result.detail}）")

        store.record_alert(
            conn, run_id=run_id, task=TASK, target="digest", severity=severity,
            channel=str(alert_cfg.get("primary", "email")), dedup_key=dedup_key,
            send_status=send_status, detail=detail,
            suppressed_reason=None if send_status in {"sent", "dry-run"} else detail,
            # 日报本身不是"故障"，别让它挂在"未恢复告警"里撑大数字
            resolved_at=http.utc_now(),
        )

        report_file = report.write_run_report(
            cfg, task=TASK, run_id=run_id, analysis={"metrics": digest["totals"], "samples": [],
                                                     "baseline": {}, "anomalies": [], "notes": digest["notes"]},
            verdict=verdict, item_label="任务", value_label="运行次数", value_suffix="",
            extra_lines=ai_notes + log_lines,
        )
        digest_body_path = cfgmod.ensure_dir(cfgmod.data_dir(cfg, "reports")) / f"digest-{local_day}.md"
        digest_body_path.write_text(body, encoding="utf-8")

        store.finish_run(
            conn, run_id, finished_at=http.utc_now(),
            status="ok" if not digest["totals"]["unresolved"] else "degraded",
            items_ok=digest["totals"]["runs"] - digest["totals"]["failed_runs"],
            items_failed=digest["totals"]["failed_runs"],
            ai_called=1 if ai_called else 0, ai_status=ai_status, exit_code=0,
            tokens_in=usage.input_tokens or None, tokens_out=usage.output_tokens or None,
            cache_read_tokens=usage.cache_read_tokens or None,
            cache_write_tokens=usage.cache_write_tokens or None,
            summary=str(verdict.get("headline", ""))[:500],
        )

        ai_part = f"ai={ai_status}"
        if ai_error:
            ai_part += f"（{ai_error[:120]}）"
        print(
            f"[{TASK}] {run_id} window={hours}h runs={digest['totals']['runs']} "
            f"unresolved={digest['totals']['unresolved']} {ai_part} "
            f"alert={send_status} report={report_file}"
        )
        return 0

    except Exception as exc:  # noqa: BLE001
        pipeline.report_failure(conn, run_id, exc)
        print(f"[{TASK}] {run_id} FAILED: {type(exc).__name__}: {exc}")
        raise
    finally:
        conn.close()
