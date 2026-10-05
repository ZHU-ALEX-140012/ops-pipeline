"""4.1 接口回归测试流水线 —— 每天 08:30（也可手动）。

设计要点：
* **自包含**：任务自己拉起靶子服务、跑完、关掉。计划任务不依赖"你得先手动启动某个服务"。
* **用例是纯数据**（targets/cases.py），断言 DSL 在 ops/core/checks.py，
  失败明细天然结构化，AI 归因拿到的就是"期望 vs 实际"，不需要解析文本报告。
* **确定性归因提示**先做一遍聚类（失败是否集中在同一接口），再交给 AI 精修 ——
  把机器的强项（数数、分组）和 AI 的强项（判断、措辞）分开。
* 边界：**PR 门禁交给 CI（GitHub Actions/GitLab CI）**，这里只做"每日全量回归 + 归因 + 晨报"。
"""

from __future__ import annotations

import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..core import checks as checks_mod
from ..core import config as cfgmod, http, judge, pipeline, report, store

TASK = "regression"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _run_id(started_at: str) -> str:
    return f"{started_at.replace('-', '').replace(':', '')}-{TASK}-{uuid.uuid4().hex[:6]}"


def _family(path: str) -> str:
    """把 /api/users/1 归到 /api/users，用于判断"失败是否集中在同一接口"。"""
    parts = [p for p in urlparse(path).path.split("/") if p]
    return "/" + "/".join(parts[:2]) if len(parts) >= 2 else "/" + "/".join(parts)


def _start_service(task_cfg: dict[str, Any], port: int) -> subprocess.Popen:
    script = PROJECT_ROOT / str(task_cfg.get("service", "targets/service.py"))
    variant = str(task_cfg.get("variant", "v2"))
    if not script.is_file():
        raise FileNotFoundError(f"找不到靶子服务脚本: {script}")
    proc = subprocess.Popen(  # noqa: S603 - 命令来自本项目配置
        [sys.executable, str(script), "--variant", variant, "--port", str(port)],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return proc


def _wait_ready(base_url: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = http.request_json("GET", f"{base_url}/api/health", timeout_ms=1500)
        if resp.status == 200:
            return True
        time.sleep(0.3)
    return False


def _stop_service(proc: subprocess.Popen | None) -> None:
    """无论成功失败都要关掉靶子服务，否则端口会被占住、下一轮直接失败。"""
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def execute_cases(
    cases: list[dict[str, Any]], base_url: str, timeout_ms: int
) -> list[dict[str, Any]]:
    """跑完所有用例。**任何异常都转成结果**，绝不让单条用例打断整轮。"""
    results: list[dict[str, Any]] = []
    for case in cases:
        name = str(case.get("name"))
        path = str(case.get("path"))
        method = str(case.get("method", "GET")).upper()
        expected = int(case.get("expect_status", 200))

        resp = http.request_json(
            method, f"{base_url}{path}",
            json_body=case.get("json_body"),
            timeout_ms=timeout_ms,
        )

        failures: list[dict[str, Any]] = []
        if resp.error:
            failures.append({
                "check": ["http_request"], "ok": False,
                "message": f"请求失败：{resp.error}",
            })
        else:
            if resp.status != expected:
                failures.append({
                    "check": ["http_status"], "ok": False,
                    "message": f"状态码期望 {expected}，实际 {resp.status}",
                })
            if resp.json_error:
                failures.append({
                    "check": ["json_body"], "ok": False,
                    "message": f"响应不是合法 JSON：{resp.json_error}；原文 {resp.text[:200]}",
                })
            else:
                failures.extend(
                    item for item in checks_mod.run_checks(resp.body, case.get("checks"))
                    if not item["ok"]
                )

        results.append({
            "name": name,
            "path": path,
            "method": method,
            "expected_status": expected,
            "actual_status": resp.status,
            "latency_ms": resp.latency_ms,
            "ok": not failures,
            "failures": failures,
            "body_preview": (resp.text or "")[:400],
        })
    return results


def _build_analysis(case_results: list[dict[str, Any]]) -> judge.Analysis:
    passed = sum(1 for r in case_results if r["ok"])
    failed = len(case_results) - passed
    latencies = [r["latency_ms"] for r in case_results if r["latency_ms"]]
    failures = [r for r in case_results if not r["ok"]]

    # 确定性归因提示：机器负责数数与分组，AI 负责判断与措辞
    notes: list[str] = []
    groups: dict[str, list[str]] = {}
    for item in failures:
        groups.setdefault(_family(item["path"]), []).append(item["name"])
    if failures:
        if len(groups) == 1:
            only = next(iter(groups))
            notes.append(
                f"全部 {len(failures)} 个失败用例都集中在 {only}，"
                "更像该接口整体不可用（单一根因）"
            )
        else:
            detail = "；".join(f"{ep}（{len(names)} 个）" for ep, names in sorted(groups.items()))
            notes.append(
                f"失败分散在 {len(groups)} 个接口：{detail} —— "
                "更可能是多个独立回归，而不是单点故障"
            )
    else:
        notes.append(f"全部 {len(case_results)} 个用例通过")

    anomalies: list[judge.Anomaly] = []
    for item in failures:
        messages = "；".join(f["message"] for f in item["failures"])
        anomalies.append(
            judge.Anomaly(
                "case_failed",
                item["name"],
                f"{item['method']} {item['path']} 未通过：{messages}"[:400],
                "alert",
            )
        )

    samples = [
        {
            "target": r["name"],
            "metric": "case",
            "value_num": r["latency_ms"],
            "ok": 1 if r["ok"] else 0,
            "ts": http.utc_now(),
            "status_code": r["actual_status"],
            "error": None if r["ok"] else "；".join(f["message"] for f in r["failures"])[:300],
        }
        for r in case_results
    ]

    metrics = {
        "targets": len(case_results),
        "ok": passed,
        "failed": failed,
        "total_cases": len(case_results),
        "passed": passed,
        "p50_ms": round(judge.percentile(latencies, 50), 1),
        "p95_ms": round(judge.percentile(latencies, 95), 1),
        "max_ms": round(max(latencies), 1) if latencies else 0.0,
        "anomalies": len(anomalies),
    }
    return judge.Analysis(metrics=metrics, samples=samples, baseline={}, anomalies=anomalies, notes=notes)


def run(cfg: dict[str, Any], *, dry_run: bool = False, no_ai: bool = False) -> int:
    task_cfg = cfg.get(TASK) or {}
    port = int(task_cfg.get("port", 8799))
    # base_url 非空 → 测你自己跑着的服务，不再启动内置靶子
    external_url = str(task_cfg.get("base_url") or "").strip().rstrip("/")
    base_url = external_url or f"http://127.0.0.1:{port}"
    timeout_ms = int(task_cfg.get("timeout_ms", 5000))

    started_at = http.utc_now()
    run_id = _run_id(started_at)
    conn = store.connect(cfgmod.data_dir(cfg, "ops.db"))
    store.start_run(conn, run_id, TASK, started_at)

    log_lines: list[str] = []
    proc: subprocess.Popen | None = None
    try:
        # ---------------------------------------------------------- 1. 准备被测目标
        variant = str(task_cfg.get("variant", "v1"))
        if external_url:
            log_lines.append(f"使用外部目标：{base_url}（未启动内置靶子）")
            if not _wait_ready(base_url, float(task_cfg.get("startup_timeout_s", 20))):
                raise RuntimeError(f"外部目标 {base_url}/api/health 在超时内没有返回 200")
        else:
            proc = _start_service(task_cfg, port)
            if not _wait_ready(base_url, float(task_cfg.get("startup_timeout_s", 20))):
                raise RuntimeError(
                    f"靶子服务在 {task_cfg.get('startup_timeout_s', 20)}s 内没有就绪"
                    f"（端口 {port} 可能被占用，或 service.py 启动失败）"
                )
            log_lines.append(f"靶子服务已就绪：{base_url}（variant={variant}）")

        # ---------------------------------------------------------- 2. 跑用例
        from importlib import import_module

        cases_module = import_module(str(task_cfg.get("cases_module", "targets.cases")))
        case_results = execute_cases(list(cases_module.CASES), base_url, timeout_ms)
        passed = sum(1 for r in case_results if r["ok"])
        log_lines.append(f"用例 {passed}/{len(case_results)} 通过")
        _stop_service(proc)
        proc = None

        analysis = _build_analysis(case_results)
        store.insert_samples(conn, run_id, analysis.samples)

        run_doc = {
            "schema": "ops/run@1",
            "run_id": run_id,
            "task": TASK,
            "started_at": started_at,
            "finished_at": http.utc_now(),
            "status": "degraded" if analysis.anomalies else "ok",
            "variant": variant,
            "metrics": analysis.metrics,
            "cases": [
                {
                    "name": r["name"], "path": r["path"], "method": r["method"],
                    "expected_status": r["expected_status"], "actual_status": r["actual_status"],
                    "latency_ms": r["latency_ms"], "ok": r["ok"],
                    "failures": r["failures"], "body_preview": r["body_preview"],
                }
                for r in case_results
            ],
            "samples": analysis.samples,
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
            prompt_name="regression",
            session_policy=((cfg.get("ai") or {}).get("session_policy") or {}).get(TASK, "fresh"),
            item_label="用例",
            always_ai=bool(task_cfg.get("always_ai", False)),
            dry_run=dry_run,
            no_ai=no_ai,
            log_lines=log_lines,
        )
        print(
            f"[{TASK}] {run_id} variant={variant} cases={passed}/{len(case_results)} "
            f"status={result.status} severity={result.severity} {pipeline.ai_note(result)} "
            f"alert={result.alert_status} report={result.report_file}"
        )
        return 0 if analysis.metrics["failed"] == 0 else 1

    except Exception as exc:  # noqa: BLE001
        pipeline.report_failure(conn, run_id, exc)
        print(f"[{TASK}] {run_id} FAILED: {type(exc).__name__}: {exc}")
        raise
    finally:
        _stop_service(proc)
        conn.close()
