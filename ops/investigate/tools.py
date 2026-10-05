"""判断层的只读调查工具。

## 为什么是"白名单 + 只读"而不是"给它文件系统权限"

判断层本来就有 `fs` / `fs-search` / `pwsh` / `web` 这些工具（见 `dsh --dump-config`），
我提示词里写的"不要读日志文件"**只是建议，不是安全边界**。

所以这里不做"再给它加权限"，而是做相反的事：**给它一组收窄到最小的调查动作**，
每个动作只能碰配置里已经声明过的对象：

| 工具 | 能碰的东西 | 为什么这样收窄 |
|---|---|---|
| `read_log` | 只能按 `[[log_scan.sources]]` 里的**名字**取 | 传名字而不是路径，路径穿越就无从谈起 |
| `query_history` | 只以**只读**方式打开数据库 | 判断层不该有写历史的能力 |
| `probe_target` | 只能探测 `[[api_watch.targets]]` 里的**名字** | 否则它能拿这台机器当跳板扫内网 |
| `read_report` | 只能读 `data/reports` 下的文件 | 同上，且校验 run_id 形状 |

顺带一个安全好处：**这些工具是纯读的**，所以判断层即使被提示注入操纵，
也改不了任何东西 —— 而它自带的 `pwsh` 可以。
（真正要收窄自带工具面得靠 harness 的插件机制，那是另一件事，见 README。）
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..core import config as cfgmod
from ..core import http

# ---------------------------------------------------------------- 规格

RUN_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,120}$")
TASK_RE = re.compile(r"^[a-z_]{1,32}$")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

TOOL_SPECS: dict[str, dict[str, Any]] = {
    "read_log": {
        "description": (
            "读取一个已配置日志源的尾部若干行（只能按 configured_log_sources 里的名字取，"
            "不接受路径）。可用 pattern 做正则过滤，便于定位某类错误。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "日志源名称，见 configured_log_sources"},
                "tail_lines": {"type": "integer", "description": "取最后多少行，1-2000，默认 300"},
                "pattern": {"type": "string", "description": "可选的正则，只返回匹配的行"},
            },
            "required": ["source"],
            "additionalProperties": False,
        },
    },
    "query_history": {
        "description": (
            "查历史运行/告警/样本，用来判断'这个问题以前出现过吗、是从什么时候开始的'。"
            "只读，不会修改任何数据。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "任务名，例如 api_watch"},
                "target": {"type": "string", "description": "可选，限定某个目标"},
                "days": {"type": "integer", "description": "回看天数，1-30，默认 7"},
            },
            "required": ["task"],
            "additionalProperties": False,
        },
    },
    "probe_target": {
        "description": (
            "立刻重新探测一个已配置的接口目标（只能按 configured_targets 里的名字取），"
            "用来确认'现在还是失败吗'。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "目标名，见 configured_targets"},
            },
            "required": ["target"],
            "additionalProperties": False,
        },
    },
    "read_report": {
        "description": "读某一次运行的报告正文（Markdown），用来对照历史上同类问题的结论。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "形如 20261005T180459Z-api_watch-abc123"},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
}


@dataclass
class ToolResult:
    ok: bool
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- 配置辅助

def investigate_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg.get("investigate") or {}


def max_output_bytes(cfg: dict[str, Any]) -> int:
    return int(investigate_cfg(cfg).get("max_output_bytes", 6000))


def log_sources(cfg: dict[str, Any]) -> dict[str, str]:
    """名字 → 路径。只认识配置文件里声明过的源。"""
    out: dict[str, str] = {}
    for source in (cfg.get("log_scan") or {}).get("sources") or []:
        name = source.get("name")
        path = source.get("path")
        if name and path:
            out[str(name)] = str(path)
    return out


def api_targets(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for target in (cfg.get("api_watch") or {}).get("targets") or []:
        name = target.get("name")
        if name:
            out[str(name)] = target
    return out


def truncate(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[: max(0, limit - 40)].decode("utf-8", "ignore") + "\n…（输出已截断）"


def _safe_under(root: Path, candidate: Path) -> bool:
    """确认 candidate 落在 root 之内 —— 防路径穿越。"""
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def open_db_readonly(db_path: Path) -> sqlite3.Connection:
    """以只读方式打开数据库。

    先用 SQLite 的 `mode=ro`（最硬的保证：连接层面就写不进去）。
    实测在**沙箱里缺 `-wal`/`-shm` 附属文件**时这一步会报
    `unable to open database file` —— 库是 WAL 模式，只读连接需要能创建共享内存文件。
    判断层第一次真实跑工具时正好撞上：`catalog` 能用，`history` 直接报错。

    所以退一步用普通连接 + `PRAGMA query_only=ON`：**写入同样被 SQLite 拒绝**，
    只是连接本身有创建 `-shm` 的副作用（文件系统痕迹，不是数据改动）。
    两条路都不允许改数据，区别只在失败面。
    """
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)
        conn.execute("SELECT 1 FROM run LIMIT 1")
        return conn
    except sqlite3.Error:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA query_only=ON")
        return conn


# ---------------------------------------------------------------- 工具实现

def read_tail(path: Path, count: int, max_bytes: int = 4_000_000) -> tuple[list[str], int, bool]:
    """只读文件尾部若干行，返回 (行列表, 文件总行数估算, 是否被字节上限截断)。

    **为什么不能 `read_text()` 了再切尾部**：生产日志动辄几百 MB，
    整读一次既吃内存又可能卡住判断层（工具没有硬超时）。
    这里从文件末尾往回读一块有上界的字节，再按行切 —— 内存占用与文件大小无关。
    实测 13MB 日志 0.018 秒返回。

    代价是"总行数"只能给出估算：没读到的部分数不出准确行数。
    所以发生字节截断时，行数返回 -1（表示"未知"），不假装精确。

    注意函数名不叫 `tail_lines` —— 那样会和 `read_log` 的同名参数互相遮蔽，
    实测直接 `TypeError: 'int' object is not callable`。
    """
    size = path.stat().st_size
    span = min(size, max_bytes)
    with path.open("rb") as handle:
        handle.seek(size - span)
        chunk = handle.read(span)

    truncated = span < size
    text = chunk.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if truncated and lines:
        # 切点很可能落在一行中间，丢掉那个残行，避免给出半行
        lines = lines[1:]
    return lines[-count:], (-1 if truncated else len(lines)), truncated


def read_log(cfg: dict[str, Any], *, source: str, tail_lines: int = 300,
             pattern: str | None = None) -> ToolResult:
    if not isinstance(source, str) or not source:
        return ToolResult(False, "source 必须是非空字符串")
    known = log_sources(cfg)
    if source not in known:
        return ToolResult(
            False,
            f"未知日志源 {source!r}。可用：{sorted(known) or '（配置里没有声明任何日志源）'}",
        )

    root = Path(cfgmod.project_root(cfg))
    path = Path(known[source])
    if not path.is_absolute():
        path = root / path
    if not _safe_under(root, path):
        return ToolResult(False, f"日志源 {source!r} 的路径落在项目目录之外，已拒绝")
    if not path.is_file():
        return ToolResult(False, f"日志源 {source!r} 的文件不存在：{path}")

    try:
        count = max(1, min(int(tail_lines), 2000))
    except (TypeError, ValueError):
        return ToolResult(False, "tail_lines 必须是整数")

    try:
        # 先取一个"足够宽"的窗口，再切到 count 行：这样带 pattern 时过滤完
        # 仍然有东西可看（否则只读到 count 行，过滤后可能一行不剩）。
        window = max(count * 20, 2000)
        selected, total, truncated = read_tail(path, window)
    except OSError as exc:
        return ToolResult(False, f"读取日志失败：{type(exc).__name__}: {exc}")

    if pattern:
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return ToolResult(False, f"pattern 不是合法正则：{exc}")
        selected = [line for line in selected if rx.search(line)]
    selected = selected[-count:]

    header = f"# {source}  文件 {path.stat().st_size} 字节"
    header += "，只读了尾部（总行数未知）" if truncated else f"，共 {total} 行"
    header += f"，返回最后 {len(selected)} 行"
    if pattern:
        header += f"（已按 {pattern!r} 过滤）"
    body = "\n".join(selected) if selected else "（没有匹配的行）"
    return ToolResult(True, f"{header}\n{body}",
                      {"returned": len(selected), "truncated": truncated})


def query_history(cfg: dict[str, Any], *, task: str, target: str | None = None,
                  days: int = 7) -> ToolResult:
    if not isinstance(task, str) or not TASK_RE.match(task):
        return ToolResult(False, f"task 不合法。可用任务：{known_tasks(cfg) or '（历史库为空）'}")

    try:
        window = max(1, min(int(days), 30))
    except (TypeError, ValueError):
        return ToolResult(False, "days 必须是整数")

    db_path = cfgmod.data_dir(cfg, "ops.db")
    if not Path(db_path).is_file():
        return ToolResult(False, f"历史库还不存在：{db_path}")

    # 只读打开：判断层不该有写历史的能力
    conn = open_db_readonly(Path(db_path))
    conn.row_factory = sqlite3.Row
    try:
        since = f"datetime('now','-{window} days')"
        runs = conn.execute(
            f"SELECT id, status, started_at, items_ok, items_failed, ai_status, summary "
            f"FROM run WHERE task=? AND started_at >= {since} "
            f"ORDER BY started_at DESC LIMIT 30",
            (task,),
        ).fetchall()

        params: list[Any] = [task]
        target_clause = ""
        if target:
            target_clause = " AND target=?"
            params.append(target)
        alerts = conn.execute(
            f"SELECT severity, send_status, target, substr(sent_at,1,19) AS sent_at, "
            f"substr(COALESCE(detail,''),1,120) AS detail, resolved_at "
            f"FROM alert WHERE task=?{target_clause} AND sent_at >= {since} "
            f"ORDER BY sent_at DESC LIMIT 30",
            params,
        ).fetchall()

        lines = [f"# {task} 最近 {window} 天（只读）", "",
                 f"## 运行记录（{len(runs)} 条）",
                 "| run_id | 状态 | 开始 | ok/failed | AI | 摘要 |",
                 "|---|---|---|---|---|---|"]
        for row in runs:
            lines.append(
                f"| {row['id']} | {row['status']} | {row['started_at']} | "
                f"{row['items_ok']}/{row['items_failed']} | {row['ai_status']} | "
                f"{str(row['summary'] or '')[:60]} |"
            )
        if not runs:
            lines.append("| （窗口内没有运行记录） | | | | | |")

        lines += ["", f"## 告警记录（{len(alerts)} 条）",
                  "| 时间 | 级别 | 目标 | 投递 | 已恢复 | 详情 |",
                  "|---|---|---|---|---|---|"]
        for row in alerts:
            lines.append(
                f"| {row['sent_at']} | {row['severity']} | {row['target']} | "
                f"{row['send_status']} | {'是' if row['resolved_at'] else '否'} | {row['detail']} |"
            )
        if not alerts:
            lines.append("| （窗口内没有告警） | | | | | |")

        return ToolResult(True, "\n".join(lines),
                          {"runs": len(runs), "alerts": len(alerts)})
    finally:
        conn.close()


def probe_target(cfg: dict[str, Any], *, target: str) -> ToolResult:
    if not isinstance(target, str) or not NAME_RE.match(target):
        return ToolResult(False, "target 名不合法")
    known = api_targets(cfg)
    if target not in known:
        return ToolResult(
            False,
            f"未知目标 {target!r}。可用：{sorted(known) or '（配置里没有声明任何目标）'}",
        )

    probe_cfg = (cfg.get("api_watch") or {}).get("probe") or {}
    pcfg = http.ProbeConfig(
        timeout_ms=int(probe_cfg.get("timeout_ms", 8000)),
        retries=int(investigate_cfg(cfg).get("probe_retries", 0)),
        rate_limit_ms=0,
    )
    result = http.probe(known[target], pcfg)
    text = (
        f"# 目标 {target} 的即时探测结果\n"
        f"- 结果：{'成功' if result.ok else '失败'}\n"
        f"- 状态码：{result.status_code}\n"
        f"- 耗时：{result.latency_ms} ms\n"
        f"- 错误：{result.error or '（无）'}\n"
        f"- 响应体长度：{result.body_len}\n"
        f"- 内容指纹：{result.body_sha256[:16] if result.body_sha256 else None}\n"
        f"- 时间：{result.ts}"
    )
    return ToolResult(True, text, {"ok": result.ok, "latency_ms": result.latency_ms})


def read_report(cfg: dict[str, Any], *, run_id: str) -> ToolResult:
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        return ToolResult(False, "run_id 形状不合法（只允许字母、数字、点、下划线、连字符）")

    reports_root = cfgmod.data_dir(cfg, "reports")
    if not Path(reports_root).is_dir():
        return ToolResult(False, f"报告目录还不存在：{reports_root}")

    matches = sorted(Path(reports_root).rglob(f"*{run_id}*.md"))
    if not matches:
        return ToolResult(False, f"没有找到与 {run_id!r} 匹配的报告")
    path = matches[-1]
    if not _safe_under(Path(reports_root), path):
        return ToolResult(False, "报告路径越界，已拒绝")

    text = path.read_text(encoding="utf-8", errors="replace")
    return ToolResult(True, f"# 报告 {path.name}\n\n{text}", {"path": str(path)})


HANDLERS: dict[str, Callable[..., ToolResult]] = {
    "read_log": read_log,
    "query_history": query_history,
    "probe_target": probe_target,
    "read_report": read_report,
}


# ---------------------------------------------------------------- 目录清单（写进提示词）

def known_tasks(cfg: dict[str, Any]) -> list[str]:
    """历史库里真正出现过的任务。

    不能用 `heartbeat_expect` 来回答"有哪些任务" —— 它只覆盖被心跳监视的那些
    （log_scan 就没在里面），会让模型以为 `history --task log_scan` 不可用。
    """
    db_path = cfgmod.data_dir(cfg, "ops.db")
    if not Path(db_path).is_file():
        return []
    conn = open_db_readonly(Path(db_path))
    try:
        rows = conn.execute("SELECT DISTINCT task FROM run ORDER BY task").fetchall()
        return [str(r[0]) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def catalog(cfg: dict[str, Any]) -> dict[str, Any]:
    """把"允许碰哪些对象"告诉模型，免得它去猜、去试。"""
    return {
        "configured_log_sources": sorted(log_sources(cfg)),
        "configured_targets": sorted(api_targets(cfg)),
        "available_tasks": known_tasks(cfg),
        "recent_run_ids": recent_report_ids(cfg),
    }


def tool_context(cfg: dict[str, Any], run_id: str | None = None) -> dict[str, Any]:
    """把"能用什么工具、能碰哪些对象、还剩几次预算"塞进喂给模型的输入。

    **为什么不让它用一次调用去问**：这些是静态配置信息（白名单、预算、输出上限），
    每次运行都一样。让模型花掉 1/6 的预算去 `catalog` 一次纯属浪费；
    更糟的是 —— 它不查就不知道预算还剩多少，也就无法规划。

    预算的**可写性**也一起报：真实踩到过"预算写不进去、每轮都显示已用 0"，
    那时硬上限形同虚设。让模型看见这件事，它至少知道自己没有护栏。
    """
    from . import budget as budget_mod

    info = catalog(cfg)
    verdict = budget_mod.check(cfg, run_id)
    writable, detail = budget_mod.probe_state_dir(cfg)
    return {
        "how_to_call": "python tools/investigate.py <子命令>（用 shell 工具执行）",
        "commands": {
            "read_log": "read-log --source <日志源名> [--tail N] [--pattern <正则>]",
            "query_history": "history --task <任务名> [--target <目标名>] [--days N]",
            "probe_target": "probe --target <目标名>",
            "read_report": "report --run-id <run_id>",
        },
        "allowed_log_sources": info["configured_log_sources"],
        "allowed_targets": info["configured_targets"],
        "allowed_tasks": info["available_tasks"],
        "recent_run_ids": info["recent_run_ids"],
        "budget": {
            "remaining_calls": max(0, verdict.limit - verdict.used) if verdict.allowed else 0,
            "limit": verdict.limit,
            "used": verdict.used,
            "counter_writable": writable,
            "warning": None if writable else f"预算计数不可写（{detail}）—— 次数限制未生效",
        },
        "max_output_bytes": max_output_bytes(cfg),
        "note": "工具是只读的。不要用 shell 读写其他文件或访问网络。",
    }


def dispatch(cfg: dict[str, Any], name: str, args: dict[str, Any]) -> ToolResult:
    handler = HANDLERS.get(name)
    if handler is None:
        return ToolResult(False, f"未知工具 {name!r}。可用：{sorted(HANDLERS)}")
    if not isinstance(args, dict):
        return ToolResult(False, "参数必须是对象")
    try:
        return handler(cfg, **args)
    except TypeError as exc:
        return ToolResult(False, f"参数不对：{exc}")
    except Exception as exc:  # noqa: BLE001 - 工具出错不能把服务打挂
        return ToolResult(False, f"{type(exc).__name__}: {exc}")


def recent_report_ids(cfg: dict[str, Any], limit: int = 5) -> list[str]:
    """给提示词用：告诉模型最近有哪些 run 可以查。"""
    db_path = cfgmod.data_dir(cfg, "ops.db")
    if not Path(db_path).is_file():
        return []
    conn = open_db_readonly(Path(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id FROM run ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [str(r["id"]) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()
