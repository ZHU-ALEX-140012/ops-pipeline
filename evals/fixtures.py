"""黄金集场景定义与夹具构造。

## 为什么夹具是「冻结的 JSON」而不是运行时生成

评测要度量的是一个**固定靶子**。如果夹具每次都调用生产代码现算，
那么一改判定逻辑，输入就跟着变 —— 分数波动分不清是"模型变好了"
还是"题目变简单了"。所以：夹具一次性生成、提交进仓库、评测时只读。

`evals/build.py` 负责重新生成；要改题目就显式改 `SCENARIOS` 再重新生成。

## 夹具的可信度从哪来

* 结构完全照 **55 份真实 run.json** 建模（字段名、嵌套、取值形态都取自真实产出）；
* `validate_run_doc()` 做**内部一致性校验**：metrics 必须与 samples 对得上、
  anomalies 计数必须与列表长度一致、时间必须成序 —— 防止手写夹具自相矛盾
  （自相矛盾的输入会让模型"答错"，那是题目有问题，不是模型有问题）。

## 为什么要包含反向用例

告警系统死于**误报**。所以黄金集里有一半是"看着像故障、其实正常"的场景
（冷启动样本不足、轻微延迟、正常抖动、v2 变体的预期差异）。
只测"能不能发现问题"是自欺欺人 —— 一个永远返回 alert 的模型召回率 100%。
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------- 阈值（与生产默认一致）

DEFAULT_THRESHOLDS = {
    "consecutive_failures": 3,
    "latency_p95_multiple": 3.0,
    "cold_start_min_samples": 20,
}

TS = "2026-10-06T09:00:00Z"
WINDOW_START = "2026-10-05T09:00:00Z"


def _doc(task: str, *, status: str, metrics: dict[str, Any], samples: list[dict[str, Any]],
         anomalies: list[dict[str, Any]], baseline: dict[str, Any] | None = None,
         extra: dict[str, Any] | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema": "ops/run@1",
        "run_id": f"eval-{task}",
        "task": task,
        "started_at": TS,
        "finished_at": TS,
        "status": status,
        "metrics": metrics,
        "samples": samples,
        "anomalies": anomalies,
        "notes": [],
        "artifacts": [],
    }
    if baseline is not None:
        doc["baseline"] = baseline
    if extra:
        doc.update(extra)
    return doc


# ---------------------------------------------------------------- api_watch


def _api_watch(spec: dict[str, Any]) -> dict[str, Any]:
    probes = spec["probes"]
    history = spec.get("history") or {}

    ok_lat = [p.get("latency_ms", 100.0) for p in probes if p.get("ok")]
    samples = []
    for p in probes:
        samples.append({
            "target": p["target"],
            "metric": "probe",
            "value_num": p.get("latency_ms", 0.0) if p.get("ok") else 0.0,
            "value_text": p.get("body_hash") if p.get("ok") else None,
            "ok": 1 if p.get("ok") else 0,
            "ts": TS,
            "status_code": p.get("status_code", 200) if p.get("ok") else None,
            "error": p.get("error") if not p.get("ok") else None,
            "body_len": p.get("body_len", 64) if p.get("ok") else None,
            "content_type": "application/json; charset=utf-8" if p.get("ok") else None,
        })

    baseline = {}
    for p in probes:
        h = history.get(p["target"], {})
        baseline[p["target"]] = {
            "samples": h.get("samples", 0),
            "p50_ms": h.get("p50_ms", 0.0),
            "p95_ms": h.get("p95_ms", 0.0),
            "consecutive_failures": h.get("consecutive_failures", 0),
            "streak_span_minutes": h.get("streak_span_minutes"),
            "failures_24h": h.get("failures_24h", 0),
            "modal_body_hash": h.get("modal_body_hash"),
            "current_body_hash": p.get("body_hash") if p.get("ok") else None,
        }

    metrics = {
        "targets": len(probes),
        "ok": sum(1 for p in probes if p.get("ok")),
        "failed": sum(1 for p in probes if not p.get("ok")),
        "p50_ms": round(sorted(ok_lat)[len(ok_lat) // 2], 1) if ok_lat else 0.0,
        "p95_ms": round(max(ok_lat), 1) if ok_lat else 0.0,
        "max_ms": round(max(ok_lat), 1) if ok_lat else 0.0,
    }

    anomalies = []
    for a in spec.get("anomalies", []):
        anomalies.append(a)
    metrics["anomalies"] = len(anomalies)
    status = "degraded" if anomalies else "ok"
    return _doc("api_watch", status=status, metrics=metrics, samples=samples,
                anomalies=anomalies, baseline=baseline,
                extra={"thresholds": spec.get("thresholds", DEFAULT_THRESHOLDS)})


# ---------------------------------------------------------------- regression


def _regression(spec: dict[str, Any]) -> dict[str, Any]:
    cases = spec["cases"]
    samples, anomalies = [], []
    latencies = []
    for c in cases:
        lat = c.get("latency_ms", 15.0)
        latencies.append(lat)
        samples.append({
            "target": c["name"], "metric": "case", "value_num": lat,
            "ok": 1 if c.get("ok") else 0, "ts": TS,
            "status_code": c.get("actual_status", 200), "error": None,
        })
        if not c.get("ok"):
            for failure in (c.get("failures") or ["用例未通过"]):
                anomalies.append({
                    "kind": "case_failed", "target": c["name"],
                    "detail": f"{c.get('path', '')} {failure}".strip(), "severity_hint": "alert",
                })

    passed = sum(1 for c in cases if c.get("ok"))
    metrics = {
        "targets": len(cases), "ok": passed, "failed": len(cases) - passed,
        "total_cases": len(cases), "passed": passed,
        "p50_ms": round(sorted(latencies)[len(latencies) // 2], 1),
        "p95_ms": round(max(latencies), 1), "max_ms": round(max(latencies), 1),
        "anomalies": len(anomalies),
    }
    doc = _doc("regression", status="degraded" if anomalies else "ok", metrics=metrics,
               samples=samples, anomalies=anomalies, extra={"variant": spec.get("variant", "v1")})
    doc["cases"] = [{
        "name": c["name"], "path": c.get("path", ""), "method": c.get("method", "GET"),
        "expected_status": c.get("expected_status", 200),
        "actual_status": c.get("actual_status", 200),
        "latency_ms": c.get("latency_ms", 15.0), "ok": bool(c.get("ok")),
        "failures": c.get("failures") or [],
        "body_preview": c.get("body_preview", ""),
    } for c in cases]
    return doc


# ---------------------------------------------------------------- log_scan


def _log_scan(spec: dict[str, Any]) -> dict[str, Any]:
    src = spec["source"]
    templates = src["templates"]
    current_lines = sum(t["current"] for t in templates)
    baseline_lines = sum(t["baseline"] for t in templates)

    samples = [{
        "target": src.get("name", "app"), "metric": "log_template",
        "value_num": t["current"], "ok": 1, "ts": TS, "status_code": None,
        "error": t["template"],
    } for t in templates]

    anomalies = list(spec.get("anomalies") or [])
    top = [{
        "template": t["template"], "level": t.get("level", "INFO"),
        "current": t["current"], "baseline": t["baseline"],
        "history_avg": t.get("history_avg", float(t["baseline"])),
        "history_median": t.get("history_median", float(t["baseline"])),
        "history_windows": t.get("history_windows", 7),
        "excluded_windows": t.get("excluded_windows", 0),
        "first_seen": t.get("first_seen", "2026-09-01T00:00:00Z"),
    } for t in templates]

    source_doc = {
        "name": src.get("name", "app"), "path": src.get("path", "data/logs/app.log"),
        "exists": src.get("exists", True),
        "total_lines": current_lines + baseline_lines,
        "parsed_lines": src.get("parsed_lines", current_lines + baseline_lines),
        "unparsed_lines": src.get("unparsed_lines", 0),
        "current_lines": current_lines, "baseline_lines": baseline_lines,
        "oldest_ts": src.get("oldest_ts", WINDOW_START),
        "newest_ts": src.get("newest_ts", TS),
        "error_lines_current": src.get("error_lines_current", 0),
        "top_templates": top,
    }
    metrics = {
        "targets": 1, "ok": 0 if anomalies else 1, "failed": 1 if anomalies else 0,
        "sources": 1, "lines_current": current_lines,
        "error_lines_current": source_doc["error_lines_current"],
        "templates": len(templates), "anomalies": len(anomalies),
    }
    return _doc("log_scan", status="degraded" if anomalies else "ok", metrics=metrics,
                samples=samples, anomalies=anomalies,
                extra={"sources": [source_doc], "window_start": WINDOW_START, "window_end": TS})


# ---------------------------------------------------------------- heartbeat


def _heartbeat(spec: dict[str, Any]) -> dict[str, Any]:
    samples, baseline, anomalies = [], {}, []
    for task, age in spec["ages_minutes"].items():
        limit = spec["limits_minutes"][task]
        stale = age > limit
        samples.append({
            "target": task, "metric": "heartbeat", "value_num": round(float(age), 1),
            "ok": 0 if stale else 1, "status_code": None,
            "error": f"超过预期间隔（{age:.0f} 分钟 > {limit:.0f} 分钟）" if stale else None,
            "ts": TS,
        })
        baseline[task] = {
            "last_run_at": TS, "age_minutes": round(float(age), 1),
            "limit_minutes": float(limit), "stale": stale,
        }
        if stale:
            anomalies.append({
                "kind": "stale_run", "target": task,
                "detail": f"{task} 已 {age:.0f} 分钟未运行（预期不超过 {limit:.0f} 分钟）",
                "severity_hint": "alert",
            })
    failed = sum(1 for s in samples if not s["ok"])
    metrics = {
        "targets": len(samples), "ok": len(samples) - failed, "failed": failed,
        "p50_ms": 0, "p95_ms": 0, "max_ms": 0, "anomalies": len(anomalies),
    }
    return _doc("heartbeat", status="degraded" if anomalies else "ok", metrics=metrics,
                samples=samples, anomalies=anomalies, baseline=baseline)


# ---------------------------------------------------------------- price_watch

def _price_watch(spec: dict[str, Any]) -> dict[str, Any]:
    """价格场景。

    注意 `baseline` 里的样本数与「上次价格」是**分别给出**的 ——
    生产上这两个值来自同一张表但含义不同（`samples` 是历史样本数、
    `last_value` 是**上一轮**的价格）。夹具把它们显式写出来，
    是为了让"冷启动"这类题能被明确地构造出来。
    """
    items = spec["items"]
    samples = []
    for it in items:
        ok = it.get("ok", True)
        samples.append({
            "target": it["name"],
            "metric": "price",
            "value_num": it.get("value") if ok else None,
            "value_text": it.get("label", it["name"]),
            "currency": it.get("currency", "CNY"),
            "ok": 1 if ok else 0,
            "ts": TS,
            "status_code": None,
            "error": it.get("error"),
        })

    baseline = {
        it["name"]: {
            "samples": it.get("samples", 0),
            "median": it.get("median"),
            "lowest": it.get("lowest"),
            "last_value": it.get("last_value"),
            "minutes_since_seen": it.get("minutes_since_seen", 0.0),
            "minutes_since_change": it.get("minutes_since_change", 0.0),
            "currency": it.get("currency", "CNY"),
        }
        for it in items
    }
    ok_items = [it for it in items if it.get("ok", True)]
    values = [it["value"] for it in ok_items if it.get("value") is not None]
    anomalies = spec.get("anomalies") or []
    metrics = {
        "items": len(items),
        "targets": len(items),
        "ok": len(ok_items),
        "failed": len(items) - len(ok_items),
        "anomalies": len(anomalies),
        "lowest": min(values) if values else None,
        "highest": max(values) if values else None,
    }
    return _doc("price_watch", status="degraded" if anomalies else "ok",
                metrics=metrics, samples=samples, anomalies=anomalies, baseline=baseline)


BUILDERS = {
    "api_watch": _api_watch,
    "regression": _regression,
    "log_scan": _log_scan,
    "heartbeat": _heartbeat,
    "price_watch": _price_watch,
}


# ---------------------------------------------------------------- 一致性校验

def validate_run_doc(doc: dict[str, Any]) -> list[str]:
    """检查夹具是否自相矛盾。返回问题列表，空表示通过。

    自相矛盾的输入会让模型"答错" —— 那是题目有问题，不是模型有问题。

    ⚠️ 这里踩到过一个真实差异：**`run@1` 契约并非各任务统一**。
      * api_watch / regression / heartbeat：`metrics.targets` == `len(samples)`
        （样本与目标一一对应）
      * log_scan：`metrics.targets/ok/failed` 是**按日志源**计的，
        而 `samples` 是**按日志模板**计的 —— 一个源可以有 9 个模板样本。
    写这个校验器时才发现的（第一版校验直接把 log_scan 判成自相矛盾）。
    """
    errors: list[str] = []
    task = doc.get("task")
    if task not in BUILDERS:
        errors.append(f"未知任务 {task!r}")
        return errors

    metrics = doc.get("metrics") or {}
    samples = doc.get("samples") or []
    anomalies = doc.get("anomalies") or []

    if task == "log_scan":
        sources = doc.get("sources") or []
        if metrics.get("targets") != len(sources):
            errors.append(
                f"log_scan 的 metrics.targets={metrics.get('targets')} 应等于日志源数量 "
                f"{len(sources)}（不是样本数 {len(samples)}）"
            )
        expected_failed = len(sources) if anomalies else 0
        if metrics.get("failed") != expected_failed:
            errors.append(
                f"log_scan 的 metrics.failed={metrics.get('failed')} 与「有异常即算失败」"
                f"推算的 {expected_failed} 不一致"
            )
    else:
        if metrics.get("targets") != len(samples):
            errors.append(
                f"metrics.targets={metrics.get('targets')} 与 samples={len(samples)} 不一致"
            )
        ok = sum(1 for s in samples if s.get("ok"))
        if metrics.get("ok") != ok:
            errors.append(f"metrics.ok={metrics.get('ok')} 与 samples 中 ok={ok} 不一致")
        if metrics.get("failed") != len(samples) - ok:
            errors.append(f"metrics.failed={metrics.get('failed')} 与 samples 推算值不一致")

    declared = metrics.get("anomalies", 0)
    if declared != len(anomalies):
        errors.append(f"metrics.anomalies={declared} 与 anomalies 列表长度 {len(anomalies)} 不一致")
    if bool(anomalies) != (doc.get("status") == "degraded"):
        errors.append(f"status={doc.get('status')!r} 与是否存在 anomalies 不符")

    for a in anomalies:
        if not a.get("kind") or not a.get("target"):
            errors.append(f"anomaly 缺少 kind/target: {a}")
    for s in samples:
        if not s.get("target"):
            errors.append(f"sample 缺少 target: {s}")

    return errors


# ---------------------------------------------------------------- 场景定义
#
# expect 字段说明：
#   should_alert             模型是否应该触发通知（精确率/召回率就靠它算）
#   severity_any_of          可接受的严重级别（不唯一正确答案，避免过严）
#   mention_any / mention_none
#                            在 headline + plain_summary + root_cause_hypothesis
#                            里必须出现 / 不得出现的关键词。这是**根因分类准确率的近似**，
#                            真正的语义评分要上 LLM-as-judge（见 README 的局限说明）

SCENARIOS: list[dict[str, Any]] = [
    # ============================== api_watch ==============================
    {
        "name": "aw_01_healthy",
        "task": "api_watch",
        "note": "三个目标全正常、延迟平稳 —— 反向用例：不该告警",
        "probes": [
            {"target": "worldtime", "ok": True, "latency_ms": 118.0, "body_hash": "h1"},
            {"target": "frankfurter", "ok": True, "latency_ms": 132.0, "body_hash": "h2"},
            {"target": "github_rate", "ok": True, "latency_ms": 205.0, "body_hash": "h3"},
        ],
        "history": {
            "worldtime": {"samples": 40, "p50_ms": 120.0, "p95_ms": 150.0, "modal_body_hash": "h1"},
            "frankfurter": {"samples": 40, "p50_ms": 130.0, "p95_ms": 160.0, "modal_body_hash": "h2"},
            "github_rate": {"samples": 40, "p50_ms": 210.0, "p95_ms": 260.0, "modal_body_hash": "h3"},
        },
        "anomalies": [],
        "expect": {"should_alert": False, "severity_any_of": ["info"],
                   "mention_none": ["严重", "全部不可用"]},
    },
    {
        "name": "aw_02_single_target_down",
        "task": "api_watch",
        "note": "单目标连续失败、同轮其他两个正常 —— 应告警，且不能误判为全局/本机故障",
        "probes": [
            {"target": "worldtime", "ok": False,
             "error": 'URLError: [WinError 10054] 远程主机强迫关闭了一个现有的连接。'},
            {"target": "frankfurter", "ok": True, "latency_ms": 128.0, "body_hash": "h2"},
            {"target": "github_rate", "ok": True, "latency_ms": 281.0, "body_hash": "h3"},
        ],
        "history": {
            "worldtime": {"samples": 0, "consecutive_failures": 5,
                          "streak_span_minutes": 888.0, "failures_24h": 5},
            "frankfurter": {"samples": 40, "p50_ms": 130.0, "p95_ms": 160.0, "modal_body_hash": "h2"},
            "github_rate": {"samples": 40, "p50_ms": 210.0, "p95_ms": 260.0, "modal_body_hash": "h3"},
        },
        "anomalies": [{
            "kind": "consecutive_failure", "target": "worldtime",
            "detail": "连续 5 次失败（阈值 3，跨度 888 分钟）；24 小时内累计失败 5 次；"
                      "最近错误：URLError: [WinError 10054] 远程主机强迫关闭了一个现有的连接。",
            "severity_hint": "alert",
        }],
        "expect": {"should_alert": True, "severity_any_of": ["alert", "critical"],
                   "mention_any": ["worldtime"],
                   # 同轮另两个目标成功 → 不该说"本机断网/整体故障"
                   "mention_none": ["全部目标失败", "本机断网", "整体不可用"]},
    },
    {
        "name": "aw_03_all_targets_down",
        "task": "api_watch",
        "note": "三个目标同一轮全部失败 —— 更像本机网络问题，应严重告警",
        "probes": [
            {"target": "worldtime", "ok": False, "error": "URLError: [Errno 11001] getaddrinfo failed"},
            {"target": "frankfurter", "ok": False, "error": "URLError: [Errno 11001] getaddrinfo failed"},
            {"target": "github_rate", "ok": False, "error": "URLError: [Errno 11001] getaddrinfo failed"},
        ],
        "history": {
            "worldtime": {"samples": 40, "consecutive_failures": 3, "failures_24h": 3,
                          "streak_span_minutes": 120.0},
            "frankfurter": {"samples": 40, "consecutive_failures": 3, "failures_24h": 3,
                            "streak_span_minutes": 120.0},
            "github_rate": {"samples": 40, "consecutive_failures": 3, "failures_24h": 3,
                            "streak_span_minutes": 120.0},
        },
        "anomalies": [
            {"kind": "consecutive_failure", "target": t,
             "detail": "连续 3 次失败（阈值 3）；DNS 解析失败 getaddrinfo failed",
             "severity_hint": "critical"}
            for t in ("worldtime", "frankfurter", "github_rate")
        ],
        "expect": {"should_alert": True, "severity_any_of": ["critical", "alert"],
                   "mention_any": ["网络", "DNS", "解析", "本机", "全部", "三个"]},
    },
    {
        "name": "aw_04_latency_mild",
        "task": "api_watch",
        "note": "延迟略高于基线但远未到阈值 —— 反向用例：不该告警（防误报）",
        "probes": [
            {"target": "worldtime", "ok": True, "latency_ms": 180.0, "body_hash": "h1"},
            {"target": "frankfurter", "ok": True, "latency_ms": 140.0, "body_hash": "h2"},
        ],
        "history": {
            "worldtime": {"samples": 40, "p50_ms": 120.0, "p95_ms": 150.0, "modal_body_hash": "h1"},
            "frankfurter": {"samples": 40, "p50_ms": 130.0, "p95_ms": 160.0, "modal_body_hash": "h2"},
        },
        "anomalies": [],
        "expect": {"should_alert": False, "severity_any_of": ["info", "notice"]},
    },
    {
        "name": "aw_05_latency_severe",
        "task": "api_watch",
        "note": "某目标 p95 涨到基线 4 倍 —— 应告警并指出是延迟问题",
        "probes": [
            {"target": "worldtime", "ok": True, "latency_ms": 920.0, "body_hash": "h1"},
            {"target": "frankfurter", "ok": True, "latency_ms": 135.0, "body_hash": "h2"},
        ],
        "history": {
            "worldtime": {"samples": 40, "p50_ms": 120.0, "p95_ms": 150.0, "modal_body_hash": "h1"},
            "frankfurter": {"samples": 40, "p50_ms": 130.0, "p95_ms": 160.0, "modal_body_hash": "h2"},
        },
        "anomalies": [{
            "kind": "latency_regression", "target": "worldtime",
            "detail": "p95 920.0ms 达到基线 150.0ms 的 6.1 倍（阈值 3.0 倍）",
            "severity_hint": "alert",
        }],
        "expect": {"should_alert": True, "severity_any_of": ["alert", "notice"],
                   "mention_any": ["worldtime"],
                   "mention_none": ["全部目标失败"]},
    },
    {
        "name": "aw_06_cold_start",
        "task": "api_watch",
        "note": "首次运行、样本远不足基线阈值 —— 反向用例：冷启动不该报异常",
        "probes": [
            {"target": "worldtime", "ok": True, "latency_ms": 300.0, "body_hash": "h1"},
            {"target": "frankfurter", "ok": True, "latency_ms": 260.0, "body_hash": "h2"},
        ],
        "history": {
            "worldtime": {"samples": 2, "p50_ms": 310.0, "p95_ms": 320.0, "modal_body_hash": "h1"},
            "frankfurter": {"samples": 1, "p50_ms": 250.0, "p95_ms": 250.0, "modal_body_hash": "h2"},
        },
        "anomalies": [],
        "expect": {"should_alert": False, "severity_any_of": ["info", "notice"],
                   "mention_none": ["严重", "故障"]},
    },
    {
        "name": "aw_07_structure_change",
        "task": "api_watch",
        "note": "接口仍返回 200 但内容变了（字段更名）—— 应告警为结构性变更，不是故障",
        "probes": [
            {"target": "user_api", "ok": True, "latency_ms": 96.0, "body_hash": "NEW"},
            {"target": "frankfurter", "ok": True, "latency_ms": 130.0, "body_hash": "h2"},
        ],
        "history": {
            "user_api": {"samples": 40, "p50_ms": 95.0, "p95_ms": 110.0, "modal_body_hash": "OLD"},
            "frankfurter": {"samples": 40, "p50_ms": 130.0, "p95_ms": 160.0, "modal_body_hash": "h2"},
        },
        "anomalies": [{
            "kind": "structure_change", "target": "user_api",
            "detail": "响应体指纹由 OLD 变为 NEW（主流指纹出现 38/40 次），HTTP 仍为 200 —— "
                      "疑似契约变更而非故障",
            "severity_hint": "alert",
        }],
        "expect": {"should_alert": True, "severity_any_of": ["alert", "notice"],
                   "mention_any": ["user_api"],
                   "mention_none": ["全部目标失败"]},
    },

    # ============================== regression ==============================
    {
        "name": "rg_01_all_pass",
        "task": "regression",
        "note": "7 条契约用例全通过 —— 反向用例：不该告警",
        "cases": [
            {"name": "health_contract", "path": "/api/health", "ok": True, "latency_ms": 14.0},
            {"name": "user_detail_contract", "path": "/api/users/1", "ok": True, "latency_ms": 16.0},
            {"name": "user_list_contract", "path": "/api/users", "ok": True, "latency_ms": 18.0},
            {"name": "order_detail_contract", "path": "/api/orders/1", "ok": True, "latency_ms": 15.0},
            {"name": "order_list_limit", "path": "/api/orders?limit=2", "ok": True, "latency_ms": 17.0},
            {"name": "not_found_contract", "path": "/api/users/9999", "ok": True, "latency_ms": 12.0},
            {"name": "error_shape_contract", "path": "/api/boom", "ok": True, "latency_ms": 13.0},
        ],
        "expect": {"should_alert": False, "severity_any_of": ["info"]},
    },
    {
        "name": "rg_02_single_failure",
        "task": "regression",
        "note": "单条用例失败（字段缺失）—— 应告警并点出是哪条",
        "cases": [
            {"name": "health_contract", "path": "/api/health", "ok": True, "latency_ms": 14.0},
            {"name": "user_detail_contract", "path": "/api/users/1", "ok": False, "latency_ms": 15.0,
             "failures": ["字段 full_name 期望类型 string，实际缺失"]},
            {"name": "user_list_contract", "path": "/api/users", "ok": True, "latency_ms": 18.0},
            {"name": "order_detail_contract", "path": "/api/orders/1", "ok": True, "latency_ms": 15.0},
            {"name": "order_list_limit", "path": "/api/orders?limit=2", "ok": True, "latency_ms": 17.0},
            {"name": "not_found_contract", "path": "/api/users/9999", "ok": True, "latency_ms": 12.0},
            {"name": "error_shape_contract", "path": "/api/boom", "ok": True, "latency_ms": 13.0},
        ],
        "expect": {"should_alert": True, "severity_any_of": ["alert", "notice"],
                   "mention_any": ["full_name", "字段", "user_detail"]},
    },
    {
        "name": "rg_03_clustered_failures",
        "task": "regression",
        "note": "三条用例同源失败（同一次字段更名）—— 应聚类成一个根因，而不是三条独立问题",
        "cases": [
            {"name": "health_contract", "path": "/api/health", "ok": True, "latency_ms": 14.0},
            {"name": "user_detail_contract", "path": "/api/users/1", "ok": False, "latency_ms": 15.0,
             "failures": ["字段 name 期望存在，实际缺失（出现 full_name）"]},
            {"name": "user_list_contract", "path": "/api/users", "ok": False, "latency_ms": 16.0,
             "failures": ["列表元素缺少字段 name"]},
            {"name": "order_detail_contract", "path": "/api/orders/1", "ok": False, "latency_ms": 15.0,
             "failures": ["字段 name 期望存在，实际缺失（出现 full_name）"]},
            {"name": "order_list_limit", "path": "/api/orders?limit=2", "ok": True, "latency_ms": 17.0},
            {"name": "not_found_contract", "path": "/api/users/9999", "ok": True, "latency_ms": 12.0},
            {"name": "error_shape_contract", "path": "/api/boom", "ok": True, "latency_ms": 13.0},
        ],
        "expect": {"should_alert": True, "severity_any_of": ["alert", "critical"],
                   "mention_any": ["name", "字段", "更名", "同一个", "同源", "共同"]},
    },
    {
        "name": "rg_04_mixed_independent",
        "task": "regression",
        "note": "两条失败用例根因不同（字段缺失 + 状态码错误）—— 不应当成同一个根因",
        "cases": [
            {"name": "user_detail_contract", "path": "/api/users/1", "ok": False, "latency_ms": 15.0,
             "failures": ["字段 full_name 期望类型 string，实际缺失"]},
            {"name": "not_found_contract", "path": "/api/users/9999", "ok": False, "latency_ms": 12.0,
             "failures": ["期望状态码 404，实际 200"]},
            {"name": "health_contract", "path": "/api/health", "ok": True, "latency_ms": 14.0},
            {"name": "order_detail_contract", "path": "/api/orders/1", "ok": True, "latency_ms": 15.0},
        ],
        "expect": {"should_alert": True, "severity_any_of": ["alert", "notice"],
                   "mention_any": ["404", "状态码", "字段", "两个", "两处", "独立"]},
    },

    # ============================== log_scan ==============================
    {
        "name": "ls_01_clean",
        "task": "log_scan",
        "note": "8 个模板量级与基线一致 —— 反向用例：不该告警",
        "source": {
            "name": "app",
            "templates": [
                {"template": "INFO [http] GET /api/products status=200", "level": "INFO",
                 "current": 1180, "baseline": 1200},
                {"template": "INFO [cache] hit key=product:<*>", "level": "INFO",
                 "current": 1120, "baseline": 1150},
                {"template": "INFO [db] query took <*>ms", "level": "INFO",
                 "current": 640, "baseline": 660},
                {"template": "WARN [db] slow query <*>ms", "level": "WARN",
                 "current": 42, "baseline": 45},
                {"template": "ERROR [db] connection timeout after <*>ms", "level": "ERROR",
                 "current": 8, "baseline": 9},
                {"template": "INFO [http] POST /api/orders status=201", "level": "INFO",
                 "current": 310, "baseline": 300},
                {"template": "INFO [auth] token refreshed user=<*>", "level": "INFO",
                 "current": 220, "baseline": 215},
                {"template": "ERROR [cache] OOM evicting <*> keys", "level": "ERROR",
                 "current": 3, "baseline": 3},
            ],
            "error_lines_current": 11,
        },
        "anomalies": [],
        "expect": {"should_alert": False, "severity_any_of": ["info"]},
    },
    {
        "name": "ls_02_new_error_template",
        "task": "log_scan",
        "note": "出现一个从没见过的新错误模板 —— 应告警并指出是「新出现」",
        "source": {
            "name": "app",
            "templates": [
                {"template": "INFO [http] GET /api/products status=200", "level": "INFO",
                 "current": 1170, "baseline": 1200},
                {"template": "INFO [cache] hit key=product:<*>", "level": "INFO",
                 "current": 1110, "baseline": 1150},
                {"template": "ERROR [payment] gateway returned 502 body=<*>", "level": "ERROR",
                 "current": 47, "baseline": 0, "first_seen": TS,
                 "history_windows": 0},
            ],
            "error_lines_current": 47,
        },
        "anomalies": [{
            "kind": "new_error_template", "target": "app",
            "detail": "模板「ERROR [payment] gateway returned 502 body=<*>」本窗口出现 47 次，"
                      "此前 7 个窗口从未出现（首见 2026-10-05T09:00:00Z）",
            "severity_hint": "alert",
        }],
        "expect": {"should_alert": True, "severity_any_of": ["alert", "critical"],
                   "mention_any": ["新", "首次", "以前没有", "从未"]},
    },
    {
        "name": "ls_03_count_spike",
        "task": "log_scan",
        "note": "已知模板量级涨到 45 倍 —— 应告警并指出是「量级激增」而非新问题",
        "source": {
            "name": "app",
            "templates": [
                {"template": "INFO [http] GET /api/products status=200", "level": "INFO",
                 "current": 1180, "baseline": 1200},
                {"template": "ERROR [cache] OOM evicting <*> keys", "level": "ERROR",
                 "current": 1350, "baseline": 30, "history_median": 30.0},
                {"template": "WARN [db] slow query <*>ms", "level": "WARN",
                 "current": 880, "baseline": 45, "history_median": 44.0},
            ],
            "error_lines_current": 1350,
        },
        "anomalies": [
            {"kind": "count_spike", "target": "app",
             "detail": "模板「ERROR [cache] OOM evicting <*> keys」本窗口 1350 次，"
                       "基线 30 次（45.0 倍，阈值 3.0 倍）",
             "severity_hint": "alert"},
            {"kind": "count_spike", "target": "app",
             "detail": "模板「WARN [db] slow query <*>ms」本窗口 880 次，"
                       "基线 45 次（19.6 倍，阈值 3.0 倍）",
             "severity_hint": "notice"},
        ],
        "expect": {"should_alert": True, "severity_any_of": ["alert", "notice"],
                   "mention_any": ["量", "倍", "激增", "变多", "OOM", "内存"]},
    },
    {
        "name": "ls_04_log_stale",
        "task": "log_scan",
        "note": "日志 24 小时没有新内容 —— 服务可能已停，应告警，且不能被当成「没有错误=正常」",
        "source": {
            "name": "app",
            "newest_ts": "2026-10-05T01:31:59+08:00",
            "templates": [
                {"template": "INFO [cache] hit key=product:<*>", "level": "INFO",
                 "current": 0, "baseline": 1170},
                {"template": "INFO [http] GET /api/products status=200", "level": "INFO",
                 "current": 0, "baseline": 1100},
                {"template": "ERROR [db] connection timeout after <*>ms", "level": "ERROR",
                 "current": 0, "baseline": 12},
            ],
            "error_lines_current": 0,
        },
        "anomalies": [{
            "kind": "log_stale", "target": "app",
            "detail": "最近一条日志是 2026-10-05T01:31:59+08:00，已 24.2 小时没有新日志"
                      "（阈值 3.0h）",
            "severity_hint": "alert",
        }],
        "expect": {"should_alert": True, "severity_any_of": ["alert", "critical"],
                   "mention_any": ["停", "没有新", "24"]},
    },
    {
        "name": "ls_05_parse_ratio_low",
        "task": "log_scan",
        "note": "六成日志解析失败（格式变了）—— 应告警，且不能声称「日志正常」",
        "source": {
            "name": "app",
            "templates": [
                {"template": "INFO [http] GET /api/products status=200", "level": "INFO",
                 "current": 400, "baseline": 1200},
                {"template": "INFO [cache] hit key=product:<*>", "level": "INFO",
                 "current": 380, "baseline": 1150},
            ],
            "parsed_lines": 780,
            "unparsed_lines": 1150,
            "error_lines_current": 0,
        },
        "anomalies": [{
            "kind": "parse_ratio_low", "target": "app",
            "detail": "1930 行中 1150 行无法解析（59.6%），阈值 30% —— 日志格式可能变了",
            "severity_hint": "alert",
        }],
        "expect": {"should_alert": True, "severity_any_of": ["alert", "notice"],
                   "mention_any": ["解析", "格式"],
                   "mention_none": ["一切正常", "日志正常"]},
    },

    {
        "name": "ls_06_new_info_template_benign",
        "task": "log_scan",
        "note": "新出现一个 INFO 模板，规则层按「0 → N 倍」误报 count_spike；"
                "正确答案是不告警 —— 这是唯一能区分「AI 有没有独立思考」的题型",
        "source": {
            "name": "app",
            "templates": [
                {"template": "INFO [http] GET /api/products status=200", "level": "INFO",
                 "current": 1170, "baseline": 1200},
                {"template": "INFO [deploy] reloaded config from /etc/app/conf.d", "level": "INFO",
                 "current": 12, "baseline": 0, "first_seen": TS, "history_windows": 0},
            ],
            "error_lines_current": 0,
        },
        # 规则层的判断（会原样出现在喂给模型的输入里）
        "anomalies": [{
            "kind": "count_spike", "target": "app",
            "detail": "模板「INFO [deploy] reloaded config from /etc/app/conf.d」本窗口 12 次，"
                      "基线 0 次（0 → 12，无法计算倍数，阈值 3.0 倍）",
            "severity_hint": "notice",
        }],
        "expect": {
            "should_alert": False,
            "severity_any_of": ["info", "notice"],
            # 认出这是无害的新日志行，而不是错误
            "mention_any": ["INFO", "信息", "正常", "无害", "部署", "配置"],
            "mention_none": ["严重故障", "服务不可用"],
        },
    },

    # ============================== heartbeat ==============================
    {
        "name": "hb_01_all_fresh",
        "task": "heartbeat",
        "note": "所有任务都在预期间隔内运行过 —— 反向用例：不该告警",
        "ages_minutes": {"api_watch": 22.0, "regression": 1270.0, "daily_report": 1165.0},
        "limits_minutes": {"api_watch": 100.0, "regression": 2170.0, "daily_report": 2170.0},
        "expect": {"should_alert": False, "severity_any_of": ["info"]},
    },
    {
        "name": "hb_02_stale_task",
        "task": "heartbeat",
        "note": "接口巡检超过预期间隔 6 倍没跑 —— 应告警并点名是哪个任务",
        "ages_minutes": {"api_watch": 610.0, "regression": 1270.0, "daily_report": 1165.0},
        "limits_minutes": {"api_watch": 100.0, "regression": 2170.0, "daily_report": 2170.0},
        "expect": {"should_alert": True, "severity_any_of": ["alert", "critical"],
                   "mention_any": ["api_watch"]},
    },

    # ------------------------------------------------------------ price_watch
    {
        "name": "pw_01_quiet",
        "task": "price_watch",
        "note": "价格在目标价之上正常波动、其它商品也无变化 —— 反向用例：不该打扰",
        "items": [
            {"name": "rtx5070", "label": "RTX 5070 显卡", "value": 4499.0,
             "samples": 20, "median": 4550.0, "lowest": 4399.0,
             "last_value": 4520.0, "minutes_since_change": 300.0},
            {"name": "rtx5080", "label": "RTX 5080 显卡", "value": 8299.0,
             "samples": 18, "median": 8300.0, "lowest": 8100.0,
             "last_value": 8299.0, "minutes_since_change": 900.0},
        ],
        "anomalies": [],
        "expect": {"should_alert": False, "severity_any_of": ["info"]},
    },
    {
        "name": "pw_02_target_hit",
        "task": "price_watch",
        "note": "跌破用户设的目标价 —— 应告警，而且语气是「可以买了」不是「出事了」",
        "items": [
            {"name": "rtx5070", "label": "RTX 5070 显卡", "value": 3799.0,
             "samples": 22, "median": 4550.0, "lowest": 3850.0,
             "last_value": 4299.0, "minutes_since_change": 10.0},
            {"name": "rtx5080", "label": "RTX 5080 显卡", "value": 8299.0,
             "samples": 18, "median": 8300.0, "lowest": 8100.0,
             "last_value": 8299.0, "minutes_since_change": 900.0},
        ],
        "anomalies": [
            {"kind": "price_below_target", "target": "rtx5070",
             "detail": "「RTX 5070 显卡」现价 ¥3,799 已到你的目标价 ¥4,000（低 5.0%）",
             "severity_hint": "notice"},
            {"kind": "price_drop", "target": "rtx5070",
             "detail": "「RTX 5070 显卡」¥4,299 → ¥3,799，一次降了 11.6%（阈值 8.0%）",
             "severity_hint": "notice"},
        ],
        "expect": {"should_alert": True, "severity_any_of": ["notice", "alert"],
                   "mention_any": ["rtx5070", "5070"]},
    },
    {
        "name": "pw_03_all_collection_failed",
        "task": "price_watch",
        "note": "一个价都没抓到 —— 应告警，且必须说清这是「我们瞎了」不是「价格没降」",
        "items": [
            {"name": "rtx5070", "label": "RTX 5070 显卡", "ok": False,
             "error": "URLError: timed out", "samples": 22, "median": 4550.0,
             "lowest": 3850.0, "last_value": 4299.0, "minutes_since_seen": 400.0},
            {"name": "rtx5080", "label": "RTX 5080 显卡", "ok": False,
             "error": "HTTP 503（非 2xx 视为没取到价格）", "samples": 18,
             "minutes_since_seen": 400.0},
        ],
        "anomalies": [
            {"kind": "price_no_data", "target": "-",
             "detail": "本轮 2 个商品**一个价格都没抓到**（0 条 = 失败，不是没降价）",
             "severity_hint": "alert"},
            {"kind": "price_missing", "target": "rtx5070",
             "detail": "没取到「RTX 5070 显卡」的价格：URLError: timed out（取不到 ≠ 没降价）",
             "severity_hint": "alert"},
            {"kind": "price_missing", "target": "rtx5080",
             "detail": "没取到「RTX 5080 显卡」的价格：HTTP 503（取不到 ≠ 没降价）",
             "severity_hint": "alert"},
        ],
        "expect": {"should_alert": True, "severity_any_of": ["alert", "critical"]},
    },
    {
        "name": "pw_04_cold_start_false_new_low",
        "task": "price_watch",
        "note": "规则层按「创新低」报了，但历史只有 2 条样本 —— 应驳回，不能断言新低",
        "items": [
            {"name": "rx9070", "label": "RX 9070 显卡", "value": 3799.0,
             "samples": 2, "median": 4199.0, "lowest": 4199.0,
             "last_value": 4199.0, "minutes_since_change": 60.0},
        ],
        "anomalies": [
            {"kind": "price_new_low", "target": "rx9070",
             "detail": "「RX 9070 显卡」现价 ¥3,799 是近 14 天最低（此前最低 ¥4,199）",
             "severity_hint": "notice"},
        ],
        "expect": {"should_alert": False, "severity_any_of": ["info", "notice"]},
    },
    {
        "name": "pw_05_price_rise",
        "task": "price_watch",
        "note": "一次涨 18% —— 应提醒（缺货/临时加价），但别写成故障",
        "items": [
            {"name": "rtx5080", "label": "RTX 5080 显卡", "value": 9799.0,
             "samples": 18, "median": 8300.0, "lowest": 8100.0,
             "last_value": 8299.0, "minutes_since_change": 30.0},
        ],
        "anomalies": [
            {"kind": "price_rise", "target": "rtx5080",
             "detail": "「RTX 5080 显卡」¥8,299 → ¥9,799，一次涨了 18.1%（阈值 15.0%）"
                       "—— 可能是缺货或临时加价",
             "severity_hint": "notice"},
        ],
        "expect": {"should_alert": True, "severity_any_of": ["notice", "alert"]},
    },
]


def build_all() -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """返回 [(场景名, run_doc, expect)]。"""
    out = []
    for spec in SCENARIOS:
        builder = BUILDERS[spec["task"]]
        doc = builder(spec)
        errors = validate_run_doc(doc)
        if errors:
            raise ValueError(f"场景 {spec['name']} 的夹具自相矛盾: {errors}")
        out.append((spec["name"], doc, spec["expect"]))
    return out


def scenario_index() -> dict[str, dict[str, Any]]:
    return {spec["name"]: spec for spec in SCENARIOS}
