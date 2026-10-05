"""2.4 日志异常自动分析 —— 每日一次。

思路：把"日志行"变成"错误簇"，再判断哪个簇是新的、哪个簇量级炸了。

* 窗口设计：**同一个文件里比两个窗口**（最近 W 小时 vs 之前 W 小时），
  所以第一轮就有基线，不存在冷启动空转；DB 里累积的历史窗口作为补充基线。
* 严重性靠**日志级别**判断，不靠模板里的数字（见 core/logparse.py 的取舍说明）。
* 静默失败防护：日志文件不存在 / 读不到 / 一行都没解析出来，都算**失败**，
  而不是"今天没有异常" —— 这是这类系统最容易骗人的地方。
"""

from __future__ import annotations

import glob
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..core import config as cfgmod, http, judge, logparse, pipeline, report, store

TASK = "log_scan"
_ERROR_LEVELS = {"ERROR", "FATAL"}


def _run_id(started_at: str) -> str:
    return f"{started_at.replace('-', '').replace(':', '')}-{TASK}-{uuid.uuid4().hex[:6]}"


@dataclass
class TemplateStat:
    template: str
    level: str
    current: int = 0
    baseline: int = 0
    sample: str = ""
    template_id: int | None = None
    history_windows: int = 0
    history_avg: float = 0.0
    history_median: float = 0.0
    first_seen: str | None = None
    excluded_windows: int = 0

    @property
    def is_error(self) -> bool:
        return self.level in _ERROR_LEVELS


@dataclass
class SourceScan:
    name: str
    path: str
    exists: bool = False
    total_lines: int = 0
    parsed_lines: int = 0
    unparsed_lines: int = 0
    current_lines: int = 0
    baseline_lines: int = 0
    oldest_ts: datetime | None = None
    newest_ts: datetime | None = None
    stats: dict[str, TemplateStat] = field(default_factory=dict)
    error_lines_current: int = 0

    @property
    def parse_ratio(self) -> float:
        return self.parsed_lines / self.total_lines if self.total_lines else 0.0


# ------------------------------------------------------------------ 读取与解析

def read_lines(path: Path, max_lines: int) -> list[str]:
    """读日志。超长文件**保留尾部**（最近的部分才重要）。"""
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    if max_lines and len(lines) > max_lines:
        return lines[-max_lines:]
    return lines


def parse_lines(
    lines: list[str], assume_tz=None, placeholder: str = logparse.PLACEHOLDER
) -> tuple[list[logparse.ParsedLine], int]:
    parsed: list[logparse.ParsedLine] = []
    unparsed = 0
    for raw in lines:
        if not raw.strip():
            continue
        item = logparse.parse_line(raw, assume_tz, placeholder)
        if not item.timestamp_matched:
            unparsed += 1
            continue
        parsed.append(item)
    return parsed, unparsed


# ------------------------------------------------------------------ 窗口与计数

def split_windows(
    parsed: list[logparse.ParsedLine], now: datetime, window_hours: float
) -> tuple[list[logparse.ParsedLine], list[logparse.ParsedLine]]:
    """切成 (当前窗口, 基线窗口)。两个窗口长度相同，紧邻排列。"""
    current_start = now - timedelta(hours=window_hours)
    baseline_start = now - timedelta(hours=window_hours * 2)
    current = [p for p in parsed if p.ts and p.ts >= current_start]
    baseline = [p for p in parsed if p.ts and baseline_start <= p.ts < current_start]
    return current, baseline


def build_stats(
    current: list[logparse.ParsedLine], baseline: list[logparse.ParsedLine]
) -> dict[str, TemplateStat]:
    """把两个窗口的行各自聚成模板计数。

    两个窗口共用同一个解析结果（模板在解析阶段已经定好），所以同名模板
    在两个窗口里一定是同一个字符串，可以直接相减。
    """
    stats: dict[str, TemplateStat] = {}
    for item in current:
        stat = stats.setdefault(
            item.template, TemplateStat(template=item.template, level=item.level, sample=item.raw[:200])
        )
        stat.current += 1
    for item in baseline:
        stat = stats.setdefault(
            item.template, TemplateStat(template=item.template, level=item.level, sample=item.raw[:200])
        )
        stat.baseline += 1
    return stats


# ------------------------------------------------------------------ 异常判定

@dataclass
class LogThresholds:
    window_hours: float = 24.0
    new_error_min_count: int = 3
    spike_multiple: float = 5.0
    spike_min_count: int = 20
    min_baseline_lines: int = 100
    stale_after_hours: float = 3.0
    parse_ratio_floor: float = 0.5
    top_templates: int = 15
    # 一个模板"首次出现"后多少天内仍按"新模板"对待。
    # 不设这个宽限期的话，第二轮它就被当成老模板了 —— 于是如果第一轮的告警
    # 恰好被静默时段/冷却抑制掉，用户就**永远收不到通知**。
    new_template_grace_days: float = 3.0


def _first_seen_within(value: str | None, now: datetime, days: float) -> bool:
    """首次出现时间是否落在宽限期内。解析不出来就按"是"处理（宁可多报一次）。"""
    if not value:
        return True
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=now.tzinfo or timezone.utc)
    return (now - moment) <= timedelta(days=days)


def detect_anomalies(
    scan: SourceScan, now: datetime, th: LogThresholds
) -> tuple[list[judge.Anomaly], list[str], set[str]]:
    """产出线索（不是结论）。

    返回 (线索, 备注, 被标记的模板集合)。第三个返回值让上层能精确标注
    "哪些模板被判为异常"，而不是事后从描述文本里反解。
    """
    anomalies: list[judge.Anomaly] = []
    notes: list[str] = []
    flagged: set[str] = set()

    # --- 静默失败一：文件不在 / 读不到
    if not scan.exists:
        anomalies.append(
            judge.Anomaly("source_missing", scan.name, f"日志文件不存在或不可读：{scan.path}", "alert")
        )
        return anomalies, notes, flagged

    # --- 静默失败二：一行都没有（0 条 = 失败，不是"没异常"）
    if scan.total_lines == 0:
        anomalies.append(
            judge.Anomaly("source_empty", scan.name, f"日志文件是空的：{scan.path}", "alert")
        )
        return anomalies, notes, flagged

    # --- 静默失败三：几乎没能解析出时间戳（很可能是格式变了）
    if scan.parse_ratio < th.parse_ratio_floor:
        anomalies.append(
            judge.Anomaly(
                "parse_ratio_low",
                scan.name,
                f"仅 {scan.parse_ratio:.0%} 的行能解析出时间戳"
                f"（{scan.unparsed_lines}/{scan.total_lines} 行无法解析）"
                "—— 日志格式可能变了，也可能是采样到了非法内容",
                "notice",
            )
        )

    # --- 日志停止写入（比"没有错误"重要得多）
    if scan.newest_ts is not None:
        idle_hours = (now - scan.newest_ts).total_seconds() / 3600
        if idle_hours > th.stale_after_hours:
            anomalies.append(
                judge.Anomaly(
                    "log_stale",
                    scan.name,
                    f"最新一条日志是 {scan.newest_ts.isoformat()}，"
                    f"已 {idle_hours:.1f} 小时没有新日志（阈值 {th.stale_after_hours}h）"
                    "—— 可能是服务停了、日志轮转出错，或采集路径不对",
                    "alert",
                )
            )

    # --- 冷启动：基线窗口样本太少，只报告不告警
    cold_start = scan.baseline_lines < th.min_baseline_lines
    if cold_start:
        notes.append(
            f"{scan.name}: 基线窗口只有 {scan.baseline_lines} 行"
            f"（阈值 {th.min_baseline_lines}），处于冷启动期 —— 本轮不做量级判定，"
            "新增模板也会用历史库二次确认"
        )

    for stat in sorted(scan.stats.values(), key=lambda s: s.current, reverse=True):
        too_short = len(stat.template.split()) < 3

        # 新出现的错误模板：最难发现、也最该报的一类。
        # 判据是"首次出现时间还在宽限期内"，**不是**"历史窗口里没记录" ——
        # 后者会在第二轮就把新问题当成老问题，一旦首轮告警被抑制就永远静默。
        if (
            stat.is_error
            and stat.current >= th.new_error_min_count
            and stat.baseline == 0
            and _first_seen_within(stat.first_seen, now, th.new_template_grace_days)
            and not too_short
        ):
            anomalies.append(
                judge.Anomaly(
                    "new_error_template",
                    scan.name,
                    f"出现新的错误模板（本轮 {stat.current} 次，基线窗口 0 次，"
                    f"首次出现 {stat.first_seen or '未知'}）：{stat.template}｜样例：{stat.sample}",
                    "alert",
                )
            )
            flagged.add(stat.template)
            continue
        if stat.is_error and stat.baseline == 0 and stat.current > 0 and not too_short:
            notes.append(
                f"{scan.name}: 错误模板本轮基线窗口为 0（{stat.current} 次），"
                f"但首次出现于 {stat.first_seen}，已超出宽限期，按已知模板处理：{stat.template}"
            )

        if cold_start or too_short:
            continue

        # 量级突增。
        # 参照值优先用**同文件里的前一个窗口**（它是固定不变的"之前"，不会被历次检测污染），
        # 只有前一个窗口没有该模板时才退到历史中位数；**不用均值、也不取两者最大值** ——
        # 那会让历史上被抬高过的量级变成新的正常，故障静悄悄地"合法化"。
        reference = stat.baseline if stat.baseline >= 1 else stat.history_median
        if (
            reference >= 1
            and stat.current >= th.spike_min_count
            and stat.current > reference * th.spike_multiple
        ):
            severity = "alert" if stat.is_error else "notice"
            if stat.baseline >= 1:
                basis = "取自同文件的前一窗口"
            else:
                basis = "取自历史中位数（前一窗口没有该模板）"
            extra = (
                f"，另有 {stat.excluded_windows} 个故障期窗口未计入基线"
                if stat.excluded_windows
                else ""
            )
            anomalies.append(
                judge.Anomaly(
                    "count_spike",
                    scan.name,
                    f"量级突增 {stat.current / reference:.1f} 倍：本轮 {stat.current} 次，"
                    f"参照值 {reference:.0f}（{basis}；前一窗口 {stat.baseline} 次，"
                    f"历史中位数 {stat.history_median:.0f}{extra}）—— {stat.template}",
                    severity,
                )
            )
            flagged.add(stat.template)
        elif stat.excluded_windows and stat.baseline == 0 and stat.current > 0:
            notes.append(
                f"{scan.name}: {stat.template} 的历史窗口全部因被判为异常而排除，"
                f"本轮没有可用参照值（{stat.current} 次）"
            )

    if not anomalies:
        notes.append(f"{scan.name}: 未发现异常（本轮 {scan.current_lines} 行，"
                     f"模板 {len(scan.stats)} 个，错误行 {scan.error_lines_current}）")
    return anomalies, notes, flagged


# ------------------------------------------------------------------ 扫描一个源

def scan_source(
    source_cfg: dict[str, Any],
    *,
    now: datetime,
    th: LogThresholds,
    miner: logparse.TemplateMiner,
    root: Path,
    assume_tz=None,
) -> SourceScan:
    name = str(source_cfg.get("name") or source_cfg.get("path") or "unknown")
    pattern = str(source_cfg.get("path") or "")
    scan = SourceScan(name=name, path=pattern)

    target = root / pattern if pattern and not Path(pattern).is_absolute() else Path(pattern)
    files: list[Path] = []
    if target.is_file():
        files = [target]
    elif any(ch in pattern for ch in "*?["):
        files = [Path(p) for p in sorted(glob.glob(str(root / pattern)))]
    if not files:
        if target.exists():
            files = [target]
        else:
            return scan

    scan.exists = True
    max_lines = int(source_cfg.get("max_lines", 200000))
    raw: list[str] = []
    for file in files:
        try:
            raw.extend(read_lines(file, max_lines))
        except OSError:
            continue
    if not raw:
        return scan

    scan.total_lines = len([line for line in raw if line.strip()])
    parsed, unparsed = parse_lines(raw, assume_tz)
    scan.unparsed_lines = unparsed
    scan.parsed_lines = len(parsed)
    if parsed:
        scan.oldest_ts = min(p.ts for p in parsed if p.ts)
        scan.newest_ts = max(p.ts for p in parsed if p.ts)

    # 关键：模板必须在**两个窗口一起**解析，否则窗口间的模板字符串对不上
    for item in sorted(parsed, key=lambda p: p.ts or now):
        miner.add(item)

    current, baseline = split_windows(parsed, now, th.window_hours)
    scan.current_lines = len(current)
    scan.baseline_lines = len(baseline)
    scan.stats = build_stats(current, baseline)
    scan.error_lines_current = sum(s.current for s in scan.stats.values() if s.is_error)
    return scan


# ------------------------------------------------------------------ 入口

def run(cfg: dict[str, Any], *, dry_run: bool = False, no_ai: bool = False) -> int:
    task_cfg = cfg.get(TASK) or {}
    thresholds_cfg = task_cfg.get("thresholds") or {}
    th = LogThresholds(
        window_hours=float(thresholds_cfg.get("window_hours", 24)),
        new_error_min_count=int(thresholds_cfg.get("new_error_min_count", 3)),
        spike_multiple=float(thresholds_cfg.get("spike_multiple", 5.0)),
        spike_min_count=int(thresholds_cfg.get("spike_min_count", 20)),
        min_baseline_lines=int(thresholds_cfg.get("min_baseline_lines", 100)),
        stale_after_hours=float(thresholds_cfg.get("stale_after_hours", 3)),
        parse_ratio_floor=float(thresholds_cfg.get("parse_ratio_floor", 0.5)),
        top_templates=int(thresholds_cfg.get("top_templates", 15)),
        new_template_grace_days=float(thresholds_cfg.get("new_template_grace_days", 3)),
    )
    root = cfgmod.project_root(cfg)

    started_at = http.utc_now()
    run_id = _run_id(started_at)
    conn = store.connect(cfgmod.data_dir(cfg, "ops.db"))
    store.start_run(conn, run_id, TASK, started_at)

    log_lines: list[str] = []
    try:
        now = datetime.now().astimezone()
        sources = list(task_cfg.get("sources") or [])
        if not sources:
            raise RuntimeError("配置里没有任何 [log_scan.sources] 日志源")

        miner = logparse.TemplateMiner(
            depth=int(task_cfg.get("drain_depth", 4)),
            sim_threshold=float(task_cfg.get("drain_similarity", 0.5)),
        )
        scans: list[SourceScan] = []
        for source in sources:
            if not source.get("enabled", True):
                continue
            scans.append(scan_source(source, now=now, th=th, miner=miner, root=root))

        window_start = (now - timedelta(hours=th.window_hours)).astimezone(timezone.utc)
        window_end = now.astimezone(timezone.utc)
        window_start_iso = window_start.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        window_end_iso = window_end.replace(microsecond=0).isoformat().replace("+00:00", "Z")

        anomalies: list[judge.Anomaly] = []
        notes: list[str] = [f"模板提取：{miner.stats()}"]
        flagged_by_source: dict[str, set[str]] = {}

        for scan in scans:
            # 登记模板 + 读历史，供"新模板"与"量级突增"二次确认
            for stat in scan.stats.values():
                stat.template_id = store.upsert_log_template(
                    conn, source=scan.name, template=stat.template,
                    severity=stat.level, ts=window_end_iso, count=stat.current,
                )
            history = store.log_template_history(
                conn, source=scan.name, before=window_start_iso,
                windows=int(thresholds_cfg.get("history_windows", 7)),
            )
            for stat in scan.stats.values():
                info = history.get(int(stat.template_id or -1))
                if info:
                    stat.history_windows = int(info["windows"])
                    stat.history_avg = float(info["avg"])
                    stat.history_median = float(info["median"])
                    stat.first_seen = info.get("first_seen")
                    stat.excluded_windows = int(info.get("excluded_windows") or 0)

            source_anomalies, source_notes, flagged = detect_anomalies(scan, now, th)
            anomalies.extend(source_anomalies)
            notes.extend(source_notes)
            flagged_by_source[scan.name] = flagged
            log_lines.append(
                f"{scan.name}: {scan.current_lines} 行（基线 {scan.baseline_lines} 行），"
                f"模板 {len(scan.stats)} 个，异常 {len(source_anomalies)} 项"
            )

        # 记录本窗口的逐模板计数，供后续运行做量级基线。
        # 被判为异常的模板要打 anomalous 标记：**基线只能由正常窗口构成**，
        # 否则故障期会被算成基线，异常慢慢变成"新常态"。
        for scan in scans:
            flagged = flagged_by_source.get(scan.name, set())
            entries = [
                {
                    "template_id": stat.template_id,
                    "window_start": window_start_iso,
                    "window_end": window_end_iso,
                    "count": stat.current,
                    "error_count": stat.current if stat.is_error else 0,
                    "anomalous": stat.template in flagged,
                }
                for stat in scan.stats.values()
                if stat.template_id is not None
            ]
            store.record_log_windows(conn, run_id=run_id, source=scan.name, entries=entries)

        # ---------------- 组装 Analysis ----------------
        samples: list[dict[str, Any]] = []
        for scan in scans:
            flagged = flagged_by_source.get(scan.name, set())
            top = sorted(scan.stats.values(), key=lambda s: s.current, reverse=True)[: th.top_templates]
            for stat in top:
                is_flagged = stat.template in flagged
                samples.append({
                    "target": scan.name,
                    "metric": "log_template",
                    "value_num": stat.current,
                    "ok": 0 if is_flagged else 1,
                    "ts": window_end_iso,
                    "status_code": None,
                    # 说明列始终给出模板本身，否则"正常"的模板看起来是一行空白
                    "error": stat.template[:200],
                })

        error_lines_total = sum(s.error_lines_current for s in scans)
        lines_total = sum(s.current_lines for s in scans)
        failed_sources = len({a.target for a in anomalies})
        metrics = {
            "targets": len(scans),
            "ok": len(scans) - failed_sources,
            "failed": failed_sources,
            "sources": len(scans),
            "lines_current": lines_total,
            "error_lines_current": error_lines_total,
            "templates": miner.template_count,
            "anomalies": len(anomalies),
        }

        run_doc = {
            "schema": "ops/run@1",
            "run_id": run_id,
            "task": TASK,
            "started_at": started_at,
            "finished_at": http.utc_now(),
            "status": "degraded" if anomalies else "ok",
            "window_start": window_start_iso,
            "window_end": window_end_iso,
            "metrics": metrics,
            "sources": [
                {
                    "name": s.name,
                    "path": s.path,
                    "exists": s.exists,
                    "total_lines": s.total_lines,
                    "parsed_lines": s.parsed_lines,
                    "unparsed_lines": s.unparsed_lines,
                    "current_lines": s.current_lines,
                    "baseline_lines": s.baseline_lines,
                    "oldest_ts": s.oldest_ts.isoformat() if s.oldest_ts else None,
                    "newest_ts": s.newest_ts.isoformat() if s.newest_ts else None,
                    "error_lines_current": s.error_lines_current,
                    "top_templates": [
                        {
                            "template": t.template, "level": t.level,
                            "current": t.current, "baseline": t.baseline,
                            "history_avg": round(t.history_avg, 2),
                            "history_median": round(t.history_median, 2),
                            "history_windows": t.history_windows,
                            "excluded_windows": t.excluded_windows,
                            "first_seen": t.first_seen,
                        }
                        for t in sorted(s.stats.values(), key=lambda x: x.current, reverse=True)[
                            : th.top_templates
                        ]
                    ],
                }
                for s in scans
            ],
            "samples": samples,
            "anomalies": [a.to_dict() for a in anomalies],
            "notes": notes,
        }
        report.write_run_doc(cfg, run_id, run_doc)

        analysis = judge.Analysis(metrics=metrics, samples=samples, baseline={},
                                  anomalies=anomalies, notes=notes)
        result = pipeline.finalize(
            cfg, conn, task=TASK, run_id=run_id, analysis=analysis, run_doc=run_doc,
            prompt_name="log_scan",
            session_policy=((cfg.get("ai") or {}).get("session_policy") or {}).get(TASK, "fresh"),
            item_label="日志源",
            value_label="本轮次数",
            value_suffix="",
            always_ai=bool(task_cfg.get("always_ai", False)),
            dry_run=dry_run, no_ai=no_ai, log_lines=log_lines,
        )
        print(
            f"[{TASK}] {run_id} sources={len(scans)} lines={lines_total} "
            f"errors={error_lines_total} templates={miner.template_count} "
            f"status={result.status} severity={result.severity} {pipeline.ai_note(result)} "
            f"alert={result.alert_status} report={result.report_file}"
        )
        return 0 if not anomalies else 1

    except Exception as exc:  # noqa: BLE001
        pipeline.report_failure(conn, run_id, exc)
        print(f"[{TASK}] {run_id} FAILED: {type(exc).__name__}: {exc}")
        raise
    finally:
        conn.close()
