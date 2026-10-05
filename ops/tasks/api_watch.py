"""4.2 接口可用性巡检 —— 每小时一次。

执行层（确定性）：探测 → 落库 → 基线比对 → 规则判定 → 写 run.json
判断层（AI）：归因 → 分级 → 写 verdict.json
裁决层（共享 pipeline）：去重/冷却/静默 → 发企业微信 → 落库 → 出报告

注意执行顺序上的一个细节：**先写入本轮样本、再统计连续失败次数**，
否则"第 3 次连续失败"会被算成第 2 次，告警永远晚一轮。
"""

from __future__ import annotations

import uuid
from typing import Any

from ..core import config as cfgmod, http, judge, pipeline, report, store

TASK = "api_watch"


def _run_id(started_at: str) -> str:
    # 秒级时间戳 + 短随机后缀：同一秒内被触发两次（手动补跑 + 计划任务）也不会撞 ID。
    # 撞 ID 会让两轮运行共用一个 run_id，样本互相污染、run 记录被覆盖。
    return f"{started_at.replace('-', '').replace(':', '')}-{TASK}-{uuid.uuid4().hex[:6]}"


def build_history(
    conn: Any, names: list[str], *, streak_max_age_hours: float = 24.0
) -> dict[str, judge.TargetHistory]:
    """从库里读历史，喂给判定层（判定层保持无 I/O，所以这里做 I/O）。"""
    history: dict[str, judge.TargetHistory] = {}
    for name in names:
        values = store.baseline_values(conn, name, days=30)
        hashes = store.body_hash_history(conn, name, days=30)
        modal_hash, modal_count = judge.modal(hashes)
        streak = store.failure_streak(conn, name, max_age_hours=streak_max_age_hours)
        history[name] = judge.TargetHistory(
            target=name,
            samples=len(values),
            p50_ms=judge.percentile(values, 50),
            p95_ms=judge.percentile(values, 95),
            consecutive_failures=int(streak["count"]),
            streak_span_minutes=streak["span_minutes"],
            failures_24h=store.failures_since(conn, name, 24),
            modal_body_hash=modal_hash,
            body_hash_samples=modal_count,
        )
    return history


def run(cfg: dict[str, Any], *, dry_run: bool = False, no_ai: bool = False) -> int:
    task_cfg = cfg.get(TASK) or {}
    thresholds = task_cfg.get("thresholds") or {}

    started_at = http.utc_now()
    run_id = _run_id(started_at)
    conn = store.connect(cfgmod.data_dir(cfg, "ops.db"))
    store.start_run(conn, run_id, TASK, started_at)

    log_lines: list[str] = []
    try:
        # ---------------------------------------------------------- 1. 采集
        pcfg = http.ProbeConfig(
            timeout_ms=int(task_cfg.get("timeout_ms", 8000)),
            retries=int(task_cfg.get("retries", 2)),
            rate_limit_ms=int(task_cfg.get("rate_limit_ms", 1200)),
        )
        targets = [t for t in (task_cfg.get("targets") or []) if t.get("enabled", True)]
        results = http.probe_all(targets, pcfg)
        store.insert_samples(conn, run_id, [r.to_sample() for r in results])
        log_lines.append(f"探测 {len(results)} 个目标，失败 {sum(1 for r in results if not r.ok)} 个")

        # ---------------------------------------------------------- 2. 规则判定
        history = build_history(
            conn,
            [str(t.get("name")) for t in targets],
            streak_max_age_hours=float(thresholds.get("streak_max_age_hours", 24)),
        )
        analysis = judge.analyze_probes(results, history, thresholds)
        run_doc = {
            "schema": "ops/run@1",
            "run_id": run_id,
            "task": TASK,
            "started_at": started_at,
            "finished_at": http.utc_now(),
            "status": "degraded" if analysis.anomalies else "ok",
            "metrics": analysis.metrics,
            "samples": analysis.samples,
            "baseline": analysis.baseline,
            "anomalies": [a.to_dict() for a in analysis.anomalies],
            "notes": analysis.notes,
        }
        report.write_run_doc(cfg, run_id, run_doc)

        # ---------------------------------------------------------- 3. 收尾
        result = pipeline.finalize(
            cfg,
            conn,
            task=TASK,
            run_id=run_id,
            analysis=analysis,
            run_doc=run_doc,
            prompt_name="api_watch",
            session_policy=((cfg.get("ai") or {}).get("session_policy") or {}).get(TASK, "fresh"),
            item_label="目标",
            always_ai=bool(task_cfg.get("always_ai", False)),
            dry_run=dry_run,
            no_ai=no_ai,
            log_lines=log_lines,
        )
        print(
            f"[{TASK}] {run_id} status={result.status} severity={result.severity} "
            f"{pipeline.ai_note(result)} alert={result.alert_status} report={result.report_file}"
        )
        return result.exit_code

    except Exception as exc:  # noqa: BLE001 - 任何异常都要落库，绝不静默
        pipeline.report_failure(conn, run_id, exc)
        print(f"[{TASK}] {run_id} FAILED: {type(exc).__name__}: {exc}")
        raise
    finally:
        conn.close()
