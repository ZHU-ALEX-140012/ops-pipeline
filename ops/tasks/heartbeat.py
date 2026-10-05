"""心跳 —— 用**独立机制**检查"预期该跑的 run 有没有跑"。

这是整套系统里最重要的一道防线：脚本崩溃、计划任务没触发、机器重启，
表面上都是"今天没有异常"，实际是**静默失败**。心跳的存在就是为了把"没消息"
主动变成"坏消息"。

刻意不使用 AI：心跳判据是确定的（时间差），交给 AI 只会增加新的失败点。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from ..core import config as cfgmod, http, judge, notify, store

TASK = "heartbeat"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def run(
    cfg: dict[str, Any],
    *,
    dry_run: bool = False,
    no_ai: bool = False,  # 心跳不用 AI，参数保留是为了统一入口签名
) -> int:
    started_at = http.utc_now()
    run_id = f"{started_at.replace('-', '').replace(':', '')}-{TASK}-{uuid.uuid4().hex[:6]}"
    conn = store.connect(cfgmod.data_dir(cfg, "ops.db"))
    store.start_run(conn, run_id, TASK, started_at)

    try:
        expect: dict[str, int] = (cfg.get("schedule") or {}).get("heartbeat_expect") or {}
        alert_cfg = cfg.get("alert") or {}
        now = datetime.now(timezone.utc)

        missing: list[str] = []
        checked: dict[str, Any] = {}
        for task_name, interval_minutes in expect.items():
            if task_name == TASK:
                continue  # 不检查自己（否则一次误报会自我放大）
            last = store.last_run_at(conn, task_name)
            last_dt = _parse_ts(last)
            age_minutes = None if last_dt is None else (now - last_dt).total_seconds() / 60
            # 宽限 1.5 倍间隔 + 10 分钟，避免临界抖动导致误报
            limit = float(interval_minutes) * 1.5 + 10
            stale = age_minutes is None or age_minutes > limit
            checked[task_name] = {
                "last_run_at": last,
                "age_minutes": None if age_minutes is None else round(age_minutes, 1),
                "limit_minutes": round(limit, 1),
                "stale": stale,
            }
            if stale:
                missing.append(
                    f"{task_name}: 最近一次运行于 {last or '从未运行'}，"
                    f"已过 {age_minutes:.0f} 分钟" if age_minutes is not None
                    else f"{task_name}: **从未运行过**"
                )

        analysis = judge.Analysis(
            metrics={"targets": len(checked), "ok": len(checked) - len(missing),
                     "failed": len(missing), "p50_ms": 0, "p95_ms": 0, "max_ms": 0},
            samples=[], baseline=checked,
            anomalies=[
                judge.Anomaly("stale_run", item.split(":")[0], item, "alert") for item in missing
            ],
        )
        analysis.metrics["anomalies"] = len(analysis.anomalies)
        analysis.samples = [
            {"target": name, "metric": "heartbeat", "value_num": info["age_minutes"],
             "ok": 0 if info["stale"] else 1, "status_code": None,
             "error": None if not info["stale"] else "stale", "ts": http.utc_now()}
            for name, info in checked.items()
        ]
        store.insert_samples(conn, run_id, [
            {"target": name, "metric": "heartbeat",
             "value_num": info["age_minutes"], "ok": 0 if info["stale"] else 1,
             "ts": http.utc_now(), "error": "stale" if info["stale"] else None}
            for name, info in checked.items()
        ])

        verdict = judge.rule_verdict(run_id, TASK, analysis)
        verdict["run_id"] = run_id
        store.record_verdict(conn, verdict, model=None)

        run_doc = {
            "schema": "ops/run@1", "run_id": run_id, "task": TASK,
            "started_at": started_at, "finished_at": http.utc_now(),
            "status": "degraded" if missing else "ok",
            "metrics": analysis.metrics, "samples": analysis.samples,
            "baseline": analysis.baseline,
            "anomalies": [a.to_dict() for a in analysis.anomalies],
        }
        from ..core import report  # 局部导入，避免与 report 模块名冲突的困惑

        report.write_run_doc(cfg, run_id, run_doc)
        report_file = report.write_run_report(
            cfg, task=TASK, run_id=run_id, analysis=analysis.to_dict(), verdict=verdict
        )

        severity = str(verdict.get("severity"))
        key = judge.dedup_key(TASK, "schedule", severity)
        decision = judge.decide(
            severity=severity,
            should_alert=bool(verdict.get("should_alert")),
            confidence=1.0,
            now_local=datetime.now(timezone.utc).astimezone(),
            last_sent_at=_parse_ts(store.last_alert_at(conn, key)),
            cooldown_minutes=int(alert_cfg.get("default_cooldown_minutes", 60)),
            quiet_hours=str(alert_cfg.get("quiet_hours", "")),
        )

        notifier = notify.Notifier(cfg, dry_run=dry_run)
        send_status = "suppressed"
        detail = decision.reason
        if decision.send:
            markdown = notify.render_markdown(
                headline=str(verdict.get("headline")),
                severity=severity,
                evidence=list(verdict.get("evidence") or []),
                metrics=analysis.metrics,
                report_path=str(report_file),
            )
            result = notifier.send(subject=f"[{severity}] {verdict.get('headline')}", markdown=markdown)
            send_status, detail = result.status, result.detail
            if result.status in {"dry-run", "failed"}:
                notifier.write_local_log(f"[{severity}] {verdict.get('headline')}\n{markdown}")

        store.record_alert(
            conn, run_id=run_id, task=TASK, target="schedule", severity=severity,
            channel=notifier.primary, dedup_key=key, send_status=send_status,
            detail=detail, suppressed_reason=None if decision.send else decision.reason,
        )
        store.finish_run(
            conn, run_id, finished_at=http.utc_now(),
            status="degraded" if missing else "ok",
            items_ok=len(checked) - len(missing), items_failed=len(missing),
            ai_called=0, ai_status="not_used", exit_code=0,
            summary=str(verdict.get("headline"))[:500],
        )
        print(f"[{TASK}] {run_id} checked={len(checked)} missing={len(missing)} alert={send_status}")
        return 1 if missing else 0

    finally:
        conn.close()
