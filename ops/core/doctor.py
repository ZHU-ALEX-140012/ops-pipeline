"""自检：一条命令回答"这套东西现在能不能正常工作"。

## 为什么值得单独做一个

这个项目里**真正花时间的从来不是写代码，而是排查"为什么它没动静"**：
SMTP 端口通不通、授权码对不对、判断层能不能写自己的 home、
预算能不能落盘、数据库迁移有没有跑上、计划任务到底注册没注册。

这些排查我这次做了一遍又一遍。把它们固化成命令，下次（或换台机器）三十秒就能定位。

## 原则

* **只读 + 探测**：除了往状态目录写一个探针文件，不改动任何东西；
* **不花 AI 调用**：`--doctor` 绝不触发判断层；
* **每一项都给"怎么修"**：报错必须告诉人该改什么（这是全项目的原则）。
"""

from __future__ import annotations

import importlib.util
import socket
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import config as cfgmod
from . import store

OK = "ok"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"

STATUS_LABEL = {OK: "[OK]  ", WARN: "[WARN]", FAIL: "[FAIL]", SKIP: "[SKIP]"}


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""


def _tcp(host: str, port: int, timeout: float = 5.0) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "可连接"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _dir_writable(path: Path) -> tuple[bool, str]:
    probe = path / ".doctor-probe"
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True, str(path)
    except OSError as exc:
        return False, f"{path}（{type(exc).__name__}: {exc}）"


# ---------------------------------------------------------------- 各项检查

def check_config(cfg: dict[str, Any]) -> list[Check]:
    out: list[Check] = []
    # 有独立配置段的任务
    required = ("api_watch", "regression", "log_scan", "daily_report", "feedback")
    missing = [name for name in required if name not in cfg]
    out.append(Check(
        "配置段完整", OK if not missing else FAIL,
        "全部存在" if not missing else f"缺少：{missing}",
        "" if not missing else "对照 config/config.toml 补上缺失的段",
    ))

    # heartbeat 没有自己的段，配置在 [schedule.heartbeat_expect] 里 ——
    # 第一版把它也列进上面那个清单，于是自检报了一个不存在的"缺失"
    expect = (cfg.get("schedule") or {}).get("heartbeat_expect") or {}
    out.append(Check(
        "心跳预期间隔", OK if expect else FAIL,
        f"监视 {sorted(expect)}" if expect else "为空 —— 心跳会认为没有任务需要检查",
        "" if expect else "在 [schedule.heartbeat_expect] 里列出实际注册了计划任务的项",
    ))

    alert = cfg.get("alert") or {}
    primary = str(alert.get("primary", ""))
    out.append(Check(
        "告警主通道", OK if primary else FAIL, f"primary = {primary or '（未设置）'}",
        "" if primary else "在 [alert] 里设置 primary",
    ))
    return out


def check_channels(cfg: dict[str, Any], *, offline: bool = False) -> list[Check]:
    """检查告警通道的**凭据与连通性**，但不发信。"""
    out: list[Check] = []
    alert = cfg.get("alert") or {}

    email = alert.get("email") or {}
    if email.get("enabled"):
        user = cfgmod.resolve_secret(cfg, email.get("user_env"))
        pwd = cfgmod.resolve_secret(cfg, email.get("pass_env"))
        missing = [n for n, v in (("user", user), ("pass", pwd)) if not v]
        out.append(Check(
            "邮件凭据", OK if not missing else FAIL,
            "已配置" if not missing else f"缺少 {missing}",
            "" if not missing else "scripts\\set-secret.cmd -Name OPS_MAIL_USER / OPS_MAIL_PASS",
        ))
        # 授权码长度：QQ 是 16 位，短了基本就能确定抄漏了
        if pwd and len(pwd) != 16:
            out.append(Check(
                "授权码长度", WARN, f"{len(pwd)} 位（QQ 邮箱授权码通常是 16 位）",
                "核对授权码是否抄全 —— 少一位会报 535 认证失败",
            ))
        host = str(email.get("smtp_host", "smtp.qq.com"))
        port = int(email.get("smtp_port", 465))
        if offline:
            out.append(Check("SMTP 连通", SKIP, "offline 模式跳过"))
        else:
            reachable, detail = _tcp(host, port)
            out.append(Check(
                f"SMTP 连通 {host}:{port}", OK if reachable else FAIL, detail,
                "" if reachable else "端口被挡就换 587（STARTTLS）或换服务商",
            ))
            if reachable:
                out.append(_smtp_mechanism(host, port))

    ntfy = alert.get("ntfy") or {}
    if alert.get("primary") == "ntfy" or ntfy:
        url = cfgmod.resolve_secret(cfg, ntfy.get("url_env", "OPS_NTFY_URL"))
        out.append(Check(
            "ntfy 地址", OK if url else WARN,
            "已配置" if url else "未配置（该通道会退化为 dry-run）",
            "" if url else "scripts\\set-secret.cmd -Name OPS_NTFY_URL",
        ))
    return out


def _smtp_mechanism(host: str, port: int) -> Check:
    """探一下服务端宣告哪些认证方式。

    这一条是踩坑换来的：QQ 对 `AUTH PLAIN` **直接掐断连接**，
    于是真正的 535 被吞成 `SMTPServerDisconnected`，排查方向被彻底带偏。
    项目里的 `smtp_login()` 因此强制走 `AUTH LOGIN`。

    ⚠️ 必须**显式**调一次 `ehlo()`：`SMTP_SSL(...)` 构造函数里的隐式 EHLO
    实测有时不会把能力表填上（`esmtp_features` 是空的），
    那样这里就会误报"服务端不支持 LOGIN"。同一个坑在 `smtp_login` 里已经踩过一次。
    """
    import smtplib
    import ssl

    try:
        with smtplib.SMTP_SSL(host, port, timeout=8,
                              context=ssl.create_default_context()) as server:
            if not server.esmtp_features:
                server.ehlo()
            mechanisms = str(server.esmtp_features.get("auth", "")).upper().split()
    except Exception as exc:  # noqa: BLE001
        return Check("SMTP 可用性", WARN, f"{type(exc).__name__}: {exc}", "")
    if not mechanisms:
        return Check("SMTP 认证方式", WARN, "服务端没宣告任何认证方式（EHLO 返回里没有 AUTH）")
    if "LOGIN" in mechanisms:
        return Check("SMTP 认证方式", OK, f"支持 {' '.join(mechanisms)}（项目固定用 AUTH LOGIN）")
    return Check(
        "SMTP 认证方式", WARN, f"未宣告 LOGIN：{mechanisms}",
        "服务端不支持 AUTH LOGIN 时会退回 smtplib 默认方式，可能触发难懂的报错",
    )


def check_storage(cfg: dict[str, Any]) -> list[Check]:
    out: list[Check] = []
    for name in ("runs", "reports", "state", "logs"):
        writable, detail = _dir_writable(cfgmod.data_dir(cfg, name))
        out.append(Check(f"目录可写 data/{name}", OK if writable else FAIL, detail,
                         "" if writable else "检查目录权限"))

    db_path = cfgmod.data_dir(cfg, "ops.db")
    if not Path(db_path).is_file():
        out.append(Check("历史库", WARN, f"还不存在：{db_path}（跑过第一次就有了）"))
        return out
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        have = {row[1] for row in conn.execute("PRAGMA table_info(run)")}
        conn.close()
    except sqlite3.Error as exc:
        out.append(Check("历史库", FAIL, f"打不开：{exc}", "删掉重建，或检查文件权限"))
        return out

    out.append(Check("历史库", OK, f"WAL={mode}"))
    # 迁移列：老库打开时自动补列，缺了就说明迁移没跑上
    need = {"cache_read_tokens", "cache_write_tokens"}
    missing = sorted(need - have)
    out.append(Check(
        "列迁移已应用", OK if not missing else FAIL,
        "已应用" if not missing else f"run 表缺少：{missing}",
        "" if not missing else "打开一次数据库即会自动迁移；失败则看 data/ 的写权限",
    ))
    return out


def check_ai(cfg: dict[str, Any]) -> list[Check]:
    out: list[Check] = []
    ai = cfg.get("ai") or {}
    cmd = Path(str(ai.get("cmd", "")))
    out.append(Check(
        "判断层命令", OK if cmd.is_file() else FAIL,
        str(cmd) if cmd.is_file() else f"找不到：{cmd}",
        "" if cmd.is_file() else "把 ai.cmd 指向 deepseek harness 的 dsh.cmd",
    ))
    patch = cfgmod.project_root(cfg) / str(ai.get("patch", ""))
    out.append(Check(
        "凭据 patch", OK if patch.is_file() else WARN,
        str(patch) if patch.is_file() else f"找不到：{patch}（将走默认 provider）",
        "" if patch.is_file() else "确认 config/dsh-headless.patch.yml 存在",
    ))
    home = cfgmod.project_root(cfg) / str(ai.get("dsh_home", "data/dsh-home"))
    writable, detail = _dir_writable(home)
    out.append(Check(
        "判断层 home 可写", OK if writable else FAIL, detail,
        "" if writable else "home 必须在工作区内，否则沙箱会拦下每次启动（要人工提权）",
    ))
    return out


def check_investigate(cfg: dict[str, Any]) -> list[Check]:
    """工具预算能不能**强制**，取决于状态目录可不可写。

    这一条是实测换来的：判断层的沙箱 shell 只能读，预算计数落不了盘，
    于是硬上限形同虚设（而且第一版还把写入异常吞了）。
    自检直接把这个事实摆出来 —— 拦得住和看得见是两件事。
    """
    conf = cfg.get("investigate") or {}
    if not conf.get("enabled", True):
        return [Check("只读调查工具", SKIP, "已关闭（investigate.enabled = false）")]
    from ..investigate import budget

    writable, detail = budget.probe_state_dir(cfg)
    return [Check(
        "工具预算可强制", OK if writable else WARN,
        f"状态目录可写：{detail}" if writable
        else f"状态目录不可写：{detail} —— 次数限制只能靠宿主侧检测",
        "" if writable else "判断层的沙箱 shell 可能写不进状态文件；"
                            "宿主侧仍会从事件流统计调用次数（看得见，但拦不住）",
    )]


def check_schedule(*, offline: bool = False) -> list[Check]:
    if offline:
        return [Check("计划任务", SKIP, "offline 模式跳过")]
    import subprocess

    try:
        proc = subprocess.run(["schtasks", "/query", "/fo", "LIST"],
                              capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return [Check("计划任务", SKIP, f"无法查询（{type(exc).__name__}）",
                      "沙箱下查不了计划任务是正常的；用 register_tasks.cmd -DryRun 预览")]
    if proc.returncode != 0:
        return [Check("计划任务", SKIP, "schtasks 返回非零（可能被沙箱限制）")]
    text = proc.stdout.decode("utf-8", "replace")
    found = [name for name in ("OpsPipeline-ApiWatch", "OpsPipeline-Regression",
                               "OpsPipeline-DailyReport", "OpsPipeline-Heartbeat",
                               "OpsPipeline-LogScan", "OpsPipeline-FeedbackScan")
             if name in text]
    return [Check(
        "计划任务", OK if found else WARN,
        f"已注册 {len(found)} 个：{found}" if found else "一个都没注册",
        "" if found else "scripts\\register_tasks.cmd",
    )]


def check_golden_set(cfg: dict[str, Any] | None = None) -> list[Check]:
    """黄金集是否自洽 —— 夹具自相矛盾会让模型"答错"，那是题目有问题。

    接受（并忽略）cfg：`run_doctor` 统一带着 cfg 调用各检查项，
    第一版这个函数不接参数，于是自检把自己报成了 FAIL。
    """
    try:
        from evals import fixtures
    except ImportError as exc:
        return [Check("评测黄金集", SKIP, f"无法导入：{exc}")]
    problems: list[str] = []
    for spec in fixtures.SCENARIOS:
        doc = fixtures.BUILDERS[spec["task"]](spec)
        errors = fixtures.validate_run_doc(doc)
        if errors:
            problems.append(f"{spec['name']}: {errors}")
    return [Check(
        "评测黄金集", OK if not problems else FAIL,
        f"{len(fixtures.SCENARIOS)} 个场景全部自洽" if not problems
        else f"{len(problems)} 个场景有问题",
        "" if not problems else "python -m evals.build 重新生成夹具",
    )]


# ---------------------------------------------------------------- 入口

CHECKS: tuple[Callable[..., list[Check]], ...] = (
    check_config,
    check_channels,
    check_storage,
    check_ai,
    check_investigate,
    check_golden_set,
)


def run_doctor(cfg: dict[str, Any], *, offline: bool = False) -> list[Check]:
    """跑所有检查。

    每一项都用 try 包住：**单项炸了不该让整份自检失败** ——
    自检最没用的失败方式就是"它自己崩了，什么都没告诉我"。
    """
    results: list[Check] = []
    for func in CHECKS:
        try:
            results += func(cfg, offline=offline)
        except TypeError:
            try:
                results += func(cfg)
            except Exception as exc:  # noqa: BLE001
                results.append(Check(func.__name__, FAIL, f"{type(exc).__name__}: {exc}"))
        except Exception as exc:  # noqa: BLE001
            results.append(Check(func.__name__, FAIL, f"{type(exc).__name__}: {exc}"))
    results += check_schedule(offline=offline)
    return results


def summarize(checks: list[Check]) -> dict[str, int]:
    counts = {OK: 0, WARN: 0, FAIL: 0, SKIP: 0}
    for item in checks:
        counts[item.status] = counts.get(item.status, 0) + 1
    return counts


def has_module(name: str) -> bool:
    """依赖自检：项目坚持零第三方依赖，缺了标准库模块要能立刻发现。"""
    return importlib.util.find_spec(name) is not None


def render(checks: list[Check]) -> str:
    lines: list[str] = []
    for item in checks:
        lines.append(f"{STATUS_LABEL.get(item.status, item.status)} {item.name}：{item.detail}")
        if item.fix:
            lines.append(f"        └─ 怎么办：{item.fix}")
    counts = summarize(checks)
    lines += [
        "",
        f"汇总：{counts[OK]} 项通过 / {counts[WARN]} 项警告 / "
        f"{counts[FAIL]} 项失败 / {counts[SKIP]} 项跳过",
    ]
    return "\n".join(lines)
