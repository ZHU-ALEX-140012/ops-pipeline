"""规则判定层（纯函数，无 I/O —— 所以好测）。

关键边界：这里产出的是**线索（anomaly）**，不是**结论**。
"严不严重、根因是什么、值不值得打扰你"交给判断层（AI）。
但当 AI 不可用时，rule_verdict() 提供一份能独立工作的兜底结论 —— 这是降级能力。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

SEVERITY_ORDER = {"info": 0, "notice": 1, "alert": 2, "critical": 3}


# ------------------------------------------------------------------ 基础统计

def percentile(values: Sequence[float], p: float) -> float:
    """线性插值分位数。p 取 0~100。空序列返回 0.0。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (p / 100.0) * (len(ordered) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return float(ordered[low])
    weight = rank - low
    return float(ordered[low] * (1 - weight) + ordered[high] * weight)


def modal(values: Sequence[str]) -> tuple[str | None, int]:
    """出现次数最多的值及其次数。"""
    if not values:
        return None, 0
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    best = max(counts.items(), key=lambda kv: kv[1])
    return best[0], best[1]


# ------------------------------------------------------------------ 数据结构

@dataclass
class TargetHistory:
    """任务层从 store 读出来的历史，喂给判定层（保持判定层无 I/O）。"""

    target: str
    samples: int = 0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    consecutive_failures: int = 0
    # 这段连续失败的时间跨度（分钟）。只说"连续 5 次"是不够的：
    # 5 次可能跨 5 分钟，也可能跨 5 天，判断价值完全不同。
    streak_span_minutes: float | None = None
    failures_24h: int = 0
    modal_body_hash: str | None = None
    body_hash_samples: int = 0


@dataclass
class Anomaly:
    kind: str
    target: str
    detail: str
    severity_hint: str = "notice"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "detail": self.detail,
            "severity_hint": self.severity_hint,
        }


@dataclass
class Analysis:
    metrics: dict[str, Any] = field(default_factory=dict)
    samples: list[dict[str, Any]] = field(default_factory=list)
    baseline: dict[str, Any] = field(default_factory=dict)
    anomalies: list[Anomaly] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metrics": self.metrics,
            "samples": self.samples,
            "baseline": self.baseline,
            "anomalies": [a.to_dict() for a in self.anomalies],
            "notes": self.notes,
        }


# ------------------------------------------------------------------ 采集结果分析

def analyze_probes(
    results: Iterable[Any],
    history: Mapping[str, TargetHistory],
    thresholds: Mapping[str, Any],
) -> Analysis:
    """把一轮探测结果转成 metrics + anomalies。

    results: 具备 to_sample() 的探测结果（http.ProbeResult）
    """
    results = list(results)
    samples = [r.to_sample() for r in results]
    ok_count = sum(1 for r in results if r.ok)
    latencies = [r.latency_ms for r in results if r.ok]

    metrics = {
        "targets": len(results),
        "ok": ok_count,
        "failed": len(results) - ok_count,
        "p50_ms": round(percentile(latencies, 50), 1),
        "p95_ms": round(percentile(latencies, 95), 1),
        "max_ms": round(max(latencies), 1) if latencies else 0.0,
    }

    fail_threshold = int(thresholds.get("consecutive_failures", 3))
    latency_multiple = float(thresholds.get("latency_p95_multiple", 3.0))
    cold_start_min = int(thresholds.get("cold_start_min_samples", 20))

    anomalies: list[Anomaly] = []
    notes: list[str] = []
    baseline_out: dict[str, Any] = {}

    # 0 条 = 失败，不是"没事"（静默失败防护的第一道闸）
    if not results:
        anomalies.append(
            Anomaly("empty_round", "-", "本轮没有采到任何样本（0 条 = 失败，不是正常）", "alert")
        )

    for result in results:
        hist = history.get(result.target) or TargetHistory(target=result.target)
        current_hash = result.body_sha256

        baseline_out[result.target] = {
            "samples": hist.samples,
            "p50_ms": round(hist.p50_ms, 1),
            "p95_ms": round(hist.p95_ms, 1),
            "consecutive_failures": hist.consecutive_failures,
            "streak_span_minutes": hist.streak_span_minutes,
            "failures_24h": hist.failures_24h,
            "modal_body_hash": hist.modal_body_hash,
            "current_body_hash": current_hash,
        }

        # 冷启动期：样本不够，只能用绝对判定，并显式记一笔
        if hist.samples < cold_start_min:
            notes.append(
                f"{result.target}: 冷启动期（基线样本 {hist.samples}/{cold_start_min}），本轮仅做绝对判定"
            )

        if not result.ok:
            streak = hist.consecutive_failures
            if streak >= fail_threshold:
                span = (
                    f"，跨度 {hist.streak_span_minutes:.0f} 分钟"
                    if hist.streak_span_minutes is not None
                    else ""
                )
                anomalies.append(
                    Anomaly(
                        "consecutive_failure",
                        result.target,
                        f"连续 {streak} 次失败（阈值 {fail_threshold}{span}）；"
                        f"24 小时内累计失败 {hist.failures_24h} 次；"
                        f"最近错误：{result.error}",
                        "alert",
                    )
                )
            elif streak > 0:
                notes.append(
                    f"{result.target}: 连续失败 {streak} 次，未达阈值 {fail_threshold}，仅记录"
                )
            continue

        # 延迟回归
        if hist.samples >= cold_start_min and hist.p95_ms > 0:
            if result.latency_ms > hist.p95_ms * latency_multiple:
                anomalies.append(
                    Anomaly(
                        "latency_regression",
                        result.target,
                        f"本次 {result.latency_ms:.0f}ms，超过基线 p95 {hist.p95_ms:.0f}ms "
                        f"的 {latency_multiple} 倍",
                        "notice",
                    )
                )

        # 返回体结构变化（R5：必须和"服务故障"分开报，否则根因会判错）
        if hist.modal_body_hash and current_hash and hist.body_hash_samples >= 3:
            if current_hash != hist.modal_body_hash:
                anomalies.append(
                    Anomaly(
                        "structure_change",
                        result.target,
                        f"返回体指纹由 {hist.modal_body_hash} 变为 {current_hash}"
                        "（可能是上游改了返回结构，不是宕机）",
                        "notice",
                    )
                )

    metrics["anomalies"] = len(anomalies)
    return Analysis(metrics=metrics, samples=samples, baseline=baseline_out, anomalies=anomalies, notes=notes)


# ------------------------------------------------------------------ 兜底结论（AI 不可用时）

def rule_verdict(run_id: str, task: str, analysis: Analysis) -> dict[str, Any]:
    """规则层自己出的结论。标注 ai_degraded，绝不假装是 AI 分析过的。"""
    metrics = analysis.metrics
    total = int(metrics.get("targets") or 0)
    ok = int(metrics.get("ok") or 0)
    p95 = metrics.get("p95_ms")

    if not analysis.anomalies:
        # 关键：结论不能和明细表自相矛盾。
        # "有目标失败但没到告警阈值" ≠ "全部正常"，前者必须如实说出来，
        # 否则报告在骗人，整套系统的可信度就此归零。
        if ok >= total:
            severity = "info"
            headline = f"{task}: 全部正常（{ok}/{total}，p95 {p95}ms）"
        else:
            severity = "notice"
            headline = (
                f"{task}: {total - ok} 个目标失败但未达告警阈值"
                f"（{ok}/{total} 正常，见明细）"
            )
    else:
        worst = max(analysis.anomalies, key=lambda a: SEVERITY_ORDER.get(a.severity_hint, 0))
        severity = worst.severity_hint
        kinds = "、".join(sorted({a.kind for a in analysis.anomalies}))
        headline = f"{task}: 检出 {len(analysis.anomalies)} 项异常（{kinds}）"

    happened, impact, action = plain_text_for(analysis)
    return {
        "schema": "ops/verdict@1",
        "run_id": run_id,
        "severity": severity,
        "should_alert": bool(analysis.anomalies),
        "headline": headline,
        # 降级≠难读：判断层不可用时，邮件也必须是三句人话
        "plain_summary": happened,
        "impact": impact,
        "suggested_action": action,
        "root_cause_hypothesis": "（规则层兜底：未经 AI 分析）",
        "evidence": [f"{a.target}: {a.detail}" for a in analysis.anomalies],
        "confidence": 1.0,
        "cooldown_minutes": None,
        "ai_degraded": True,
    }


# 异常类型 → 大白话。判断层不可用时，兜底结论也得是**人能读的**，
# 否则"降级"就意味着收件人收到一封满屏字段名的邮件。
ANOMALY_PLAIN: dict[str, str] = {
    "consecutive_failure": "有个目标连续失败，一直连不上",
    "latency_regression": "有个目标变慢了，比平时慢很多",
    "structure_change": "有个目标返回的内容和以前不一样了（可能对方改了接口）",
    "empty_round": "这一轮什么都没采集到（可能是网络或采集本身出了问题）",
    "case_failed": "有接口用例没通过",
    "new_error_template": "日志里出现了以前从没见过的错误",
    "count_spike": "某类日志的量突然明显变多了",
    "log_stale": "日志停止写入了（服务可能停了）",
    "source_missing": "找不到要分析的日志文件",
    "source_empty": "日志文件是空的",
    "parse_ratio_low": "大量日志解析不出来（格式可能变了）",
    "stale_run": "有任务没有按预期运行（可能没触发或中途挂了）",
}


def plain_text_for(analysis: Analysis) -> tuple[str, str, str]:
    """把异常线索翻译成 (发生了什么, 意味着什么, 建议做什么)。"""
    if not analysis.anomalies:
        total = analysis.metrics.get("targets") or 0
        return (
            f"这一轮一切正常，{total} 个检查项都通过了。",
            "没有需要你处理的事情。",
            "无需处理。",
        )

    kinds: list[str] = []
    targets: list[str] = []
    for item in analysis.anomalies:
        phrase = ANOMALY_PLAIN.get(item.kind, f"发现异常（{item.kind}）")
        if phrase not in kinds:
            kinds.append(phrase)
        if item.target and item.target != "-" and item.target not in targets:
            targets.append(item.target)

    happened = "；".join(kinds[:3])
    if targets:
        happened += f"。涉及：{'、'.join(targets[:4])}"

    severe = any(
        SEVERITY_ORDER.get(a.severity_hint, 0) >= SEVERITY_ORDER["alert"]
        for a in analysis.anomalies
    )
    impact = (
        "这件事可能会影响你正在用的东西，建议尽快看一眼。"
        if severe
        else "暂时看起来不紧急，可能只是一次抖动或对方的小改动。"
    )
    return happened + "。", impact, "打开报告看明细；确认是持续性问题再决定怎么处理。"


# ------------------------------------------------------------------ 决策（发不发）

def dedup_key(task: str, target: str | None, severity: str) -> str:
    return f"{task}:{target or '-'}:{severity}"


def in_quiet_hours(now_local: datetime, spec: str) -> bool:
    """判断是否处于静默时段。支持跨午夜，如 '23:30-07:30'。"""
    if not spec or "-" not in spec:
        return False
    try:
        start_s, end_s = spec.split("-", 1)
        sh, sm = (int(x) for x in start_s.strip().split(":"))
        eh, em = (int(x) for x in end_s.strip().split(":"))
    except (ValueError, TypeError):
        return False
    start = now_local.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now_local.replace(hour=eh, minute=em, second=0, microsecond=0)
    if start <= end:
        return start <= now_local < end
    return now_local >= start or now_local < end


@dataclass
class Decision:
    send: bool
    reason: str
    severity: str


def decide(
    *,
    severity: str,
    should_alert: bool,
    confidence: float,
    now_local: datetime,
    last_sent_at: datetime | None,
    cooldown_minutes: int,
    quiet_hours: str = "",
    min_confidence: float = 0.5,
) -> Decision:
    """决定这张告警发不发。所有抑制理由都显式返回，便于落库审计。"""
    if not should_alert:
        return Decision(False, "verdict_suppressed", severity)
    if severity == "info":
        return Decision(False, "severity_info", severity)
    if confidence < min_confidence:
        return Decision(False, f"low_confidence({confidence:.2f}<{min_confidence})", severity)
    if last_sent_at is not None:
        elapsed = now_local - last_sent_at
        if elapsed < timedelta(minutes=cooldown_minutes):
            return Decision(
                False,
                f"cooldown({elapsed.total_seconds() / 60:.0f}m<{cooldown_minutes}m)",
                severity,
            )
    if severity != "critical" and in_quiet_hours(now_local, quiet_hours):
        return Decision(False, "quiet_hours", severity)
    return Decision(True, "ok", severity)
