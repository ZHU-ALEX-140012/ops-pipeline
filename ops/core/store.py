"""SQLite 存储（WAL）。

设计约定：
* 时间一律 UTC ISO8601 字符串；
* 原始 run.json 同时落盘（data/runs/），DB 坏了也不丢证据；
* alert 表的唯一索引是 (dedup_key, run_id) —— **同一轮运行内幂等**，
  跨轮的重复抑制交给 judge 的冷却逻辑（否则一条告警永远发不出第二次）。
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS run (
  id            TEXT PRIMARY KEY,
  task          TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  status        TEXT NOT NULL DEFAULT 'ok',
  items_ok      INTEGER DEFAULT 0,
  items_failed  INTEGER DEFAULT 0,
  ai_called     INTEGER DEFAULT 0,
  ai_status     TEXT,
  ai_degraded   INTEGER DEFAULT 0,
  tokens_in     INTEGER,
  tokens_out    INTEGER,
  cache_read_tokens  INTEGER,
  cache_write_tokens INTEGER,
  exit_code     INTEGER,
  log_path      TEXT,
  summary       TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_task_time ON run(task, started_at DESC);

CREATE TABLE IF NOT EXISTS sample (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id     TEXT NOT NULL REFERENCES run(id),
  target     TEXT NOT NULL,
  metric     TEXT NOT NULL,
  value_num  REAL,
  value_text TEXT,
  ok         INTEGER,
  ts         TEXT NOT NULL,
  status_code INTEGER,
  error      TEXT,
  body_len   INTEGER,
  content_type TEXT
);
CREATE INDEX IF NOT EXISTS idx_sample_target ON sample(target, metric, ts DESC);

CREATE TABLE IF NOT EXISTS verdict (
  run_id        TEXT PRIMARY KEY,
  severity      TEXT,
  should_alert  INTEGER,
  headline      TEXT,
  root_cause    TEXT,
  evidence_json TEXT,
  confidence    REAL,
  model         TEXT,
  created_at    TEXT
);

CREATE TABLE IF NOT EXISTS alert (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id      TEXT NOT NULL,
  task        TEXT,
  target      TEXT,
  severity    TEXT,
  channel     TEXT,
  dedup_key   TEXT NOT NULL,
  send_status TEXT NOT NULL,
  detail      TEXT,
  sent_at     TEXT NOT NULL,
  -- 故障恢复的时间。为空 = 这条告警还没恢复，恢复通知就是靠它判断的。
  resolved_at TEXT,
  suppressed_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_once ON alert(dedup_key, run_id);
CREATE INDEX IF NOT EXISTS idx_alert_dedup ON alert(dedup_key, sent_at DESC);

-- 日志模板字典：一个模板一行，长期累积
CREATE TABLE IF NOT EXISTS log_template (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  source      TEXT NOT NULL,
  template    TEXT NOT NULL,
  severity    TEXT,
  first_seen  TEXT,
  last_seen   TEXT,
  total_count INTEGER DEFAULT 0,
  UNIQUE(source, template)
);

-- 每次扫描的逐模板计数：多个窗口累积起来就构成"量级基线"
CREATE TABLE IF NOT EXISTS log_window (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id       TEXT NOT NULL,
  source       TEXT NOT NULL,
  template_id  INTEGER NOT NULL,
  window_start TEXT NOT NULL,
  window_end   TEXT NOT NULL,
  count        INTEGER NOT NULL DEFAULT 0,
  error_count  INTEGER NOT NULL DEFAULT 0,
  -- 这个窗口里该模板被判定为异常吗？基线**只能由正常窗口构成**，
  -- 否则故障期会被算进基线，异常逐渐变成"新常态"。
  anomalous    INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_log_window ON log_window(source, window_start DESC, template_id);

-- 人工反馈：这条告警判得对吗？
-- 离线评测能给"模型在黄金集上多准"，只有人能给"它在你这儿准不准"。
CREATE TABLE IF NOT EXISTS feedback (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id     TEXT NOT NULL,
  task       TEXT,
  -- up = 判得对；down = 判错了（误报）
  vote       TEXT NOT NULL,
  source     TEXT NOT NULL DEFAULT 'cli',   -- cli | email | web
  note       TEXT,
  created_at TEXT NOT NULL,
  -- 同一个人对同一轮重复反馈时以最后一次为准，所以按 (run_id, source) 去重
  UNIQUE(run_id, source)
);
CREATE INDEX IF NOT EXISTS idx_feedback_run ON feedback(run_id);
"""

# 简单的列迁移：CREATE TABLE IF NOT EXISTS 不会给已存在的表加列，
# 所以新增列时要显式补上（老库需要平滑升级）。
COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("log_window", "anomalous", "ALTER TABLE log_window ADD COLUMN anomalous INTEGER NOT NULL DEFAULT 0"),
    ("run", "cache_read_tokens", "ALTER TABLE run ADD COLUMN cache_read_tokens INTEGER"),
    ("run", "cache_write_tokens", "ALTER TABLE run ADD COLUMN cache_write_tokens INTEGER"),
    ("alert", "resolved_at", "ALTER TABLE alert ADD COLUMN resolved_at TEXT"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, statement in COLUMN_MIGRATIONS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(statement)
    conn.commit()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


# ------------------------------------------------------------------ run

def start_run(conn: sqlite3.Connection, run_id: str, task: str, started_at: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO run(id, task, started_at, status) VALUES (?,?,?,?)",
        (run_id, task, started_at, "running"),
    )
    conn.commit()


def finish_run(conn: sqlite3.Connection, run_id: str, **fields: Any) -> None:
    allowed = {
        "finished_at", "status", "items_ok", "items_failed", "ai_called",
        "ai_status", "ai_degraded", "tokens_in", "tokens_out",
        "cache_read_tokens", "cache_write_tokens", "exit_code",
        "log_path", "summary",
    }
    sets, values = [], []
    for key, value in fields.items():
        if key in allowed:
            sets.append(f"{key}=?")
            values.append(value)
    if not sets:
        return
    values.append(run_id)
    conn.execute(f"UPDATE run SET {', '.join(sets)} WHERE id=?", values)
    conn.commit()


def insert_samples(conn: sqlite3.Connection, run_id: str, samples: Iterable[dict[str, Any]]) -> int:
    rows = [
        (
            run_id, s["target"], s.get("metric", "probe"), s.get("value_num"),
            s.get("value_text"), s.get("ok"), s.get("ts") or utc_now(),
            s.get("status_code"), s.get("error"), s.get("body_len"), s.get("content_type"),
        )
        for s in samples
    ]
    conn.executemany(
        "INSERT INTO sample(run_id,target,metric,value_num,value_text,ok,ts,"
        "status_code,error,body_len,content_type) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return len(rows)


def recent_runs(conn: sqlite3.Connection, task: str, limit: int = 10) -> list[dict[str, Any]]:
    cur = conn.execute(
        "SELECT id, task, started_at, finished_at, status, items_ok, items_failed, "
        "ai_status, ai_degraded, summary FROM run WHERE task=? ORDER BY started_at DESC LIMIT ?",
        (task, limit),
    )
    return [dict(row) for row in cur.fetchall()]


def last_run_at(conn: sqlite3.Connection, task: str) -> str | None:
    cur = conn.execute("SELECT MAX(started_at) AS ts FROM run WHERE task=?", (task,))
    row = cur.fetchone()
    return row["ts"] if row and row["ts"] else None


def ai_calls_today(conn: sqlite3.Connection) -> int:
    midnight = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat().replace("+00:00", "Z")
    cur = conn.execute(
        "SELECT COUNT(*) AS n FROM run WHERE ai_called=1 AND started_at>=?", (midnight,)
    )
    return int(cur.fetchone()["n"])


# ------------------------------------------------------------------ baseline

def baseline_values(
    conn: sqlite3.Connection, target: str, metric: str = "probe", days: int = 30, ok_only: bool = True
) -> list[float]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    sql = "SELECT value_num FROM sample WHERE target=? AND metric=? AND ts>=? AND value_num IS NOT NULL"
    if ok_only:
        sql += " AND ok=1"
    cur = conn.execute(sql, (target, metric, since))
    return [float(row["value_num"]) for row in cur.fetchall()]


def body_hash_history(
    conn: sqlite3.Connection, target: str, days: int = 30
) -> list[str]:
    """目标返回体指纹历史（用于识别"返回结构变了"）。"""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    cur = conn.execute(
        "SELECT value_text FROM sample WHERE target=? AND ok=1 AND ts>=? "
        "AND value_text IS NOT NULL AND value_text<>''",
        (target, since),
    )
    return [row["value_text"] for row in cur.fetchall()]


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def failure_streak(
    conn: sqlite3.Connection,
    target: str,
    max_age_hours: float = 24.0,
    metric: str = "probe",
    limit: int = 100,
) -> dict[str, Any]:
    """从最近一次往前数，连续失败了多少次 —— **并且限定时间窗口**。

    为什么要限时：如果不限，昨天甚至上周的失败会和今天的连成一段"连续失败"，
    于是 `consecutive_failures=5` 与 `failures_24h=1` 同时出现，两个指标自相矛盾。
    这会让判断层（包括 AI）得出错误结论 —— 第一次真实 AI 运行就抓到了这一点。

    返回 count / oldest / newest / span_minutes，让结论能说清"跨了多久"，
    而不是只说"连续 5 次"（5 次可能是 5 分钟，也可能是 5 天）。
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat().replace("+00:00", "Z")
    cur = conn.execute(
        "SELECT ok, ts FROM sample WHERE target=? AND metric=? ORDER BY ts DESC, id DESC LIMIT ?",
        (target, metric, limit),
    )
    count = 0
    newest: str | None = None
    oldest: str | None = None
    for row in cur.fetchall():
        if row["ok"] != 0 or row["ts"] < cutoff:
            break
        count += 1
        if newest is None:
            newest = row["ts"]
        oldest = row["ts"]

    span_minutes: float | None = None
    if count >= 2 and newest and oldest:
        start, end = _parse_ts(oldest), _parse_ts(newest)
        if start and end:
            span_minutes = round((end - start).total_seconds() / 60, 1)

    return {"count": count, "oldest": oldest, "newest": newest, "span_minutes": span_minutes}


def consecutive_failures(
    conn: sqlite3.Connection, target: str, limit: int = 20, max_age_hours: float = 24.0
) -> int:
    """兼容用的简单封装，内部走 failure_streak（含时间窗口）。"""
    return int(failure_streak(conn, target, max_age_hours=max_age_hours, limit=limit)["count"])


def failures_since(conn: sqlite3.Connection, target: str, hours: int = 24) -> int:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
    cur = conn.execute(
        "SELECT COUNT(*) AS n FROM sample WHERE target=? AND ok=0 AND ts>=?", (target, since)
    )
    return int(cur.fetchone()["n"])


# ------------------------------------------------------------------ verdict / alert

def record_verdict(conn: sqlite3.Connection, verdict: dict[str, Any], model: str | None = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO verdict(run_id,severity,should_alert,headline,root_cause,"
        "evidence_json,confidence,model,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            verdict.get("run_id"),
            verdict.get("severity"),
            1 if verdict.get("should_alert") else 0,
            verdict.get("headline"),
            verdict.get("root_cause_hypothesis") or verdict.get("root_cause"),
            json.dumps(verdict.get("evidence") or [], ensure_ascii=False),
            verdict.get("confidence"),
            model,
            utc_now(),
        ),
    )
    conn.commit()


def record_alert(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    task: str,
    target: str | None,
    severity: str,
    channel: str,
    dedup_key: str,
    send_status: str,
    detail: str = "",
    suppressed_reason: str | None = None,
    sent_at: str | None = None,
    resolved_at: str | None = None,
) -> bool:
    """写入告警记录。同 (dedup_key, run_id) 重复写入会被忽略 → 单轮幂等。

    返回 True 表示这次真的新写入（即这张告警之前没在同一轮发过）。

    `resolved_at` 用于"本来就不是故障"的记录（例如日报本身）：
    直接标成已恢复，免得它一直挂在"未恢复告警"里撑大数字。
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO alert(run_id,task,target,severity,channel,dedup_key,"
        "send_status,detail,sent_at,resolved_at,suppressed_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id, task, target, severity, channel, dedup_key, send_status,
            detail, sent_at or utc_now(), resolved_at, suppressed_reason,
        ),
    )
    conn.commit()
    return cur.rowcount > 0


def last_alert_at(conn: sqlite3.Connection, dedup_key: str) -> str | None:
    """上一次"有效投递"的时间。

    计入 'sent' 与 'dry-run'：dry-run 必须忠实模拟真实发送，否则演练时看不出冷却效果。
    **不计入 'failed'**：发送失败就应该下一轮重试，而不是被冷却压住。
    """
    cur = conn.execute(
        "SELECT MAX(sent_at) AS ts FROM alert WHERE dedup_key=? AND send_status IN ('sent','dry-run')",
        (dedup_key,),
    )
    row = cur.fetchone()
    return row["ts"] if row and row["ts"] else None


def alerts_since(conn: sqlite3.Connection, hours: int = 24) -> list[dict[str, Any]]:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
    cur = conn.execute(
        "SELECT * FROM alert WHERE sent_at>=? ORDER BY sent_at DESC", (since,)
    )
    return [dict(row) for row in cur.fetchall()]


def unresolved_alerts(
    conn: sqlite3.Connection, task: str, limit: int = 20
) -> list[dict[str, Any]]:
    """还没恢复的告警（按时间倒序）。

    只看 `sent`：**只有真正通知过你的告警才谈得上"恢复"**。
    dry-run 意味着当时压根没配通道、没通知任何人，给它发"已恢复"没有意义，
    也会把日报里的"未恢复"数字撑成一片噪声。
    """
    cur = conn.execute(
        "SELECT * FROM alert WHERE task=? AND resolved_at IS NULL "
        "AND send_status='sent' ORDER BY sent_at DESC LIMIT ?",
        (task, limit),
    )
    return [dict(row) for row in cur.fetchall()]


def unresolved_alerts_all(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    """所有任务里尚未恢复的告警（日报要列出来）。同样只看真正投递过的。"""
    cur = conn.execute(
        "SELECT * FROM alert WHERE resolved_at IS NULL AND send_status='sent' "
        "ORDER BY sent_at DESC LIMIT ?",
        (limit,),
    )
    return [dict(row) for row in cur.fetchall()]


def mark_alerts_resolved(conn: sqlite3.Connection, alert_ids: list[int], ts: str | None = None) -> int:
    """标记这些告警已恢复。返回实际更新的行数。"""
    if not alert_ids:
        return 0
    moment = ts or utc_now()
    placeholders = ",".join("?" for _ in alert_ids)
    cur = conn.execute(
        f"UPDATE alert SET resolved_at=? WHERE id IN ({placeholders}) AND resolved_at IS NULL",
        (moment, *alert_ids),
    )
    conn.commit()
    return cur.rowcount


# ------------------------------------------------------------------ 日报用的汇总

def run_stats_since(conn: sqlite3.Connection, since: str) -> list[dict[str, Any]]:
    """按任务汇总指定时间之后的运行情况。"""
    cur = conn.execute(
        "SELECT task, COUNT(*) AS runs, "
        "  SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_runs, "
        "  SUM(CASE WHEN status='degraded' THEN 1 ELSE 0 END) AS degraded_runs, "
        "  SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_runs, "
        "  SUM(items_ok) AS items_ok, SUM(items_failed) AS items_failed, "
        "  SUM(ai_called) AS ai_calls, "
        "  SUM(CASE WHEN ai_status='failed' THEN 1 ELSE 0 END) AS ai_failures, "
        "  SUM(CASE WHEN ai_status='budget_exceeded' THEN 1 ELSE 0 END) AS ai_budget_hits, "
        "  MIN(started_at) AS first_run, MAX(started_at) AS last_run "
        "FROM run WHERE started_at>=? GROUP BY task ORDER BY task",
        (since,),
    )
    return [dict(row) for row in cur.fetchall()]


def token_stats_since(conn: sqlite3.Connection, since: str) -> dict[str, int]:
    """指定时间之后的 token 用量汇总。

    ⚠️ cache_read 的单价远低于普通输入，所以这里分列展示、不做加权，
    避免给出一个看着精确其实误导的"钱数"。
    """
    cur = conn.execute(
        "SELECT COALESCE(SUM(tokens_in),0) AS tokens_in, "
        "  COALESCE(SUM(tokens_out),0) AS tokens_out, "
        "  COALESCE(SUM(cache_read_tokens),0) AS cache_read, "
        "  COALESCE(SUM(cache_write_tokens),0) AS cache_write "
        "FROM run WHERE started_at>=?",
        (since,),
    )
    row = cur.fetchone()
    return {
        "tokens_in": int(row["tokens_in"]),
        "tokens_out": int(row["tokens_out"]),
        "cache_read": int(row["cache_read"]),
        "cache_write": int(row["cache_write"]),
    }


def alert_stats_since(conn: sqlite3.Connection, since: str) -> list[dict[str, Any]]:
    cur = conn.execute(
        "SELECT send_status, COUNT(*) AS n FROM alert WHERE sent_at>=? GROUP BY send_status",
        (since,),
    )
    return [dict(row) for row in cur.fetchall()]


# ------------------------------------------------------------------ 人工反馈

def record_feedback(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    vote: str,
    task: str | None = None,
    source: str = "cli",
    note: str | None = None,
    created_at: str | None = None,
) -> bool:
    """记一条人工反馈。

    同一个 run_id + source 重复反馈时**以最后一次为准**（人工改主意是正常的），
    所以用 UPSERT 而不是 INSERT OR IGNORE。
    """
    if vote not in ("up", "down"):
        raise ValueError(f"vote 只能是 up/down，收到 {vote!r}")
    conn.execute(
        "INSERT INTO feedback(run_id, task, vote, source, note, created_at) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(run_id, source) DO UPDATE SET "
        "  vote=excluded.vote, note=excluded.note, created_at=excluded.created_at",
        (run_id, task, vote, source, note, created_at or utc_now()),
    )
    conn.commit()
    return True


def feedback_for_run(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    cur = conn.execute("SELECT * FROM feedback WHERE run_id=? ORDER BY created_at DESC", (run_id,))
    return [dict(row) for row in cur.fetchall()]


def feedback_stats(conn: sqlite3.Connection, *, since: str | None = None) -> dict[str, Any]:
    """反馈汇总。

    ⚠️ 这里算的是**人工标注的准确率**，样本来自"你愿意回一条的那些告警"——
    天然有偏（判错的更可能被反馈）。所以它只用来看趋势与找反例，
    **不能当成模型在全部告警上的准确率**。报告里必须这么写。
    """
    clause = "WHERE created_at>=?" if since else ""
    params: tuple[Any, ...] = (since,) if since else ()
    rows = conn.execute(
        f"SELECT vote, COUNT(*) AS n FROM feedback {clause} GROUP BY vote", params
    ).fetchall()
    counts = {str(r["vote"]): int(r["n"]) for r in rows}
    up, down = counts.get("up", 0), counts.get("down", 0)
    total = up + down
    recent = conn.execute(
        f"SELECT run_id, task, vote, source, substr(COALESCE(note,''),1,120) AS note, created_at "
        f"FROM feedback {clause} ORDER BY created_at DESC LIMIT 20",
        params,
    ).fetchall()
    return {
        "up": up,
        "down": down,
        "total": total,
        "accuracy": round(up / total, 4) if total else None,
        "recent": [dict(row) for row in recent],
    }


# ------------------------------------------------------------------ 日志模板与窗口

def upsert_log_template(
    conn: sqlite3.Connection,
    *,
    source: str,
    template: str,
    severity: str,
    ts: str,
    count: int = 0,
) -> int:
    """登记模板并返回 id。

    用 INSERT ... ON CONFLICT 而不是先查后插：并发/重复运行都不会产生重复行，
    也不会因为竞态插入两条同样的模板。
    """
    conn.execute(
        "INSERT INTO log_template(source, template, severity, first_seen, last_seen, total_count) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(source, template) DO UPDATE SET "
        "  last_seen=excluded.last_seen, "
        "  total_count=log_template.total_count+excluded.total_count, "
        "  severity=excluded.severity",
        (source, template, severity, ts, ts, count),
    )
    conn.commit()
    cur = conn.execute(
        "SELECT id FROM log_template WHERE source=? AND template=?", (source, template)
    )
    return int(cur.fetchone()["id"])


def record_log_windows(
    conn: sqlite3.Connection, *, run_id: str, source: str, entries: list[dict[str, Any]]
) -> int:
    """记录一次扫描的逐模板计数。

    entries: [{template_id, window_start, window_end, count, error_count, anomalous}]
    `anomalous=1` 的窗口不会进入后续的基线统计 —— 故障期不能当参照物。
    """
    now = utc_now()
    conn.executemany(
        "INSERT INTO log_window(run_id, source, template_id, window_start, window_end, "
        "count, error_count, anomalous, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                run_id, source, int(e["template_id"]), e["window_start"], e["window_end"],
                int(e["count"]), int(e.get("error_count", 0)),
                1 if e.get("anomalous") else 0, now,
            )
            for e in entries
        ],
    )
    conn.commit()
    return len(entries)


def log_template_history(
    conn: sqlite3.Connection, *, source: str, before: str, windows: int = 7,
    include_anomalous: bool = False,
) -> dict[int, dict[str, Any]]:
    """最近 N 个窗口的逐模板量级（**默认排除被判定为异常的窗口**）。

    返回 {template_id: {counts, windows, avg, median, first_seen, excluded_windows}}。

    两条关键设计：
    * **排除异常窗口**：否则故障期的量级会被写进基线，异常逐渐变成"新常态"，
      系统会对已经报警过的严重问题失去反应。
    * 给 **median** 而不只给均值：均值容易被单个异常窗口拉高，中位数不敏感。
    """
    result: dict[int, dict[str, Any]] = {}

    # 所有模板都带上 first_seen：判断"是不是新出现的模板"要靠它，
    # 而不是靠"历史窗口里有没有记录"（后者会在第二轮就把新问题变成老问题）。
    meta = {
        int(row["id"]): row["first_seen"]
        for row in conn.execute(
            "SELECT id, first_seen FROM log_template WHERE source=?", (source,)
        ).fetchall()
    }
    for template_id, first_seen in meta.items():
        result[template_id] = {
            "counts": [], "windows": 0, "avg": 0.0, "median": 0.0,
            "first_seen": first_seen, "excluded_windows": 0,
        }

    cur = conn.execute(
        "SELECT DISTINCT window_start FROM log_window WHERE source=? AND window_start<? "
        "ORDER BY window_start DESC LIMIT ?",
        (source, before, max(1, windows)),
    )
    stamps = [row["window_start"] for row in cur.fetchall()]
    if not stamps:
        return result

    placeholders = ",".join("?" for _ in stamps)
    filter_sql = "" if include_anomalous else " AND anomalous=0"
    cur = conn.execute(
        f"SELECT template_id, count, anomalous FROM log_window "
        f"WHERE source=? AND window_start IN ({placeholders}){filter_sql}",
        (source, *stamps),
    )
    buckets: dict[int, list[int]] = {}
    for row in cur.fetchall():
        buckets.setdefault(int(row["template_id"]), []).append(int(row["count"]))

    # 被排除掉的窗口数也要能说清楚，否则用的人会以为历史是空的
    cur = conn.execute(
        f"SELECT template_id, COUNT(*) AS n FROM log_window "
        f"WHERE source=? AND window_start IN ({placeholders}) AND anomalous=1 "
        f"GROUP BY template_id",
        (source, *stamps),
    )
    excluded = {int(row["template_id"]): int(row["n"]) for row in cur.fetchall()}

    for template_id, counts in buckets.items():
        entry = result.setdefault(
            template_id,
            {"counts": [], "windows": 0, "avg": 0.0, "median": 0.0,
             "first_seen": None, "excluded_windows": 0},
        )
        entry["counts"] = counts
        entry["windows"] = len(counts)
        entry["avg"] = sum(counts) / len(counts)
        entry["median"] = float(statistics.median(counts))
    for template_id, count in excluded.items():
        if template_id in result:
            result[template_id]["excluded_windows"] = count
    return result
