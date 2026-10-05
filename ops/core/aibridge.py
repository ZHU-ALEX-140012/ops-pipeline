"""AI 判断层桥接：调用 `dsh --profile <p>` 完成一次无交互任务。

契约（R1 权限收敛的落地方式）：
* 我们把 run.json 作为**事实**写进提示词；
* 要求 agent 只写两个文件：verdict.json 与 report.md，都在项目目录内；
* agent **不需要访问网络或数据库**，因此无人值守时不会卡在批准提示上。

沙箱隔离（R1 的另一半）：
* 子进程的 `DSH_HOME` 被重定向到项目内的 `data/dsh-home`，继承来的 `DSH_*` 一律剔除；
* 于是 headless profile 的每次重写、会话与存储的落盘都发生在工作区内，**不需要任何提权**，
  也不会把编排层的 profile / 会话 id 串进判断层；
* 账号凭据不跟着搬：`config/dsh-headless.patch.yml` 把 credentials 插件的家钉回真实
  `~/.dsh` 读取（只读，工作区外的读取不受 workspace-write 限制）。

健壮性：
* 超时/非零退出/verdict 缺失或非法 → 一律返回 ok=False，由任务层降级；
* 提示词、原始输出、事件流全部留痕，便于事后复盘。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config as cfgmod
from . import routing

VERDICT_SCHEMA = "ops/verdict@1"
SEVERITIES = {"info", "notice", "alert", "critical"}
SESSION_RE = re.compile(r"session-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass
class RunUsage:
    """一次判断层调用的 token 用量。

    ⚠️ 关于事件流里的数字（实测 schema）：
        {"type":"thinking",...,"usage":{"inputTokens":2294,"outputTokens":966,
         "cacheReadTokens":5760,"cacheWriteTokens":0,"totalTokens":9020}}
    `inputTokens`/`outputTokens` 是**单次调用**的量，而 `totalTokens` 是**会话累计**。
    所以这里按单次字段求和，绝不累加 `totalTokens`（那会把总量重复计算）。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    session_total_tokens: int = 0  # 最后一个 usage 事件里的累计值，仅作参考

    @property
    def billable_in(self) -> int:
        """计费意义上的输入：普通输入 + cache 读 + cache 写。

        ⚠️ cache 读的单价远低于普通输入，所以这个数字用于**观察趋势**，
        不能直接乘单价当钱算。
        """
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "session_total_tokens": self.session_total_tokens,
        }


def parse_events(text: str) -> tuple[str | None, RunUsage]:
    """从 `dsh --json` 的 NDJSON 事件流里取出 sessionId 与 token 用量。

    为什么不用正则从 stdio 里捞 sessionId：事件流里有结构化的 `session` 事件，
    解析它比正则可靠（正则可能匹到日志里别处的 id）。
    """
    session_id: str | None = None
    usage = RunUsage()
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "session" and event.get("sessionId"):
            session_id = str(event["sessionId"])
        payload = event.get("usage")
        if isinstance(payload, dict):
            def pick(key: str) -> int:
                value = payload.get(key)
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return 0

            usage.input_tokens += pick("inputTokens")
            usage.output_tokens += pick("outputTokens")
            usage.cache_read_tokens += pick("cacheReadTokens")
            usage.cache_write_tokens += pick("cacheWriteTokens")
            usage.session_total_tokens = pick("totalTokens")
    return session_id, usage


def parse_tool_calls(text: str, needle: str = "tools/investigate.py") -> dict[str, Any]:
    """从事件流里统计判断层调了哪些工具。

    **这是宿主侧的权威计数，不需要任何写入** —— 所以它在"子进程沙箱只能读、
    状态文件写不进去"的环境里照样准确。实测就是这种环境：CLI 自己的计数器
    落不了盘（而且第一版还把写入异常静默吞了），但工具**确实被调用了 7 次**。

    于是预算的两个角色被分开：
      * **强制**：CLI 在状态目录可写时硬拦（超了返回退出码 2）；
      * **检测**：宿主侧从事件流数 —— 即使拦不住，也一定看得见超了。
    把"拦得住"和"看得见"混为一谈，是这类护栏最常见的自欺。
    """
    calls: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "tool_call":
            continue
        payload = event.get("input")
        command = str(payload.get("command") or "") if isinstance(payload, dict) else ""
        calls.append({
            "tool": str(event.get("tool") or ""),
            "command": command,
            "is_investigate": needle in command,
        })
    investigate_calls = [c for c in calls if c["is_investigate"]]
    return {
        "total": len(calls),
        "investigate": len(investigate_calls),
        "commands": [c["command"][:120] for c in investigate_calls],
    }


@dataclass
class AIResult:
    ok: bool
    verdict: dict[str, Any] | None = None
    session_id: str | None = None
    duration_s: float = 0.0
    error: str | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    prompt_path: Path | None = None
    log_path: Path | None = None
    events_path: Path | None = None
    ai_report_path: Path | None = None
    usage: RunUsage = field(default_factory=RunUsage)
    tool_calls: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


def validate_verdict(payload: Any, expected_run_id: str) -> list[str]:
    """返回错误列表；空列表表示合法。"""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["verdict 不是 JSON 对象"]
    for key in ("schema", "severity", "should_alert", "headline"):
        if key not in payload:
            errors.append(f"缺少字段 {key}")
    if payload.get("schema") not in (None, VERDICT_SCHEMA):
        errors.append(f"schema 不是 {VERDICT_SCHEMA}")
    if payload.get("severity") not in SEVERITIES:
        errors.append(f"severity 非法: {payload.get('severity')!r}")
    if "should_alert" in payload and not isinstance(payload["should_alert"], bool):
        errors.append("should_alert 必须是布尔值")
    confidence = payload.get("confidence")
    if confidence is not None and not isinstance(confidence, (int, float)):
        errors.append("confidence 必须是数字")
    if payload.get("run_id") and payload["run_id"] != expected_run_id:
        errors.append(f"run_id 不匹配（期望 {expected_run_id}）")
    return errors


def build_prompt(template: str, *, run_json: str, verdict_path: str, report_path: str) -> str:
    return (
        template.replace("{{RUN_JSON}}", run_json)
        .replace("{{VERDICT_PATH}}", verdict_path)
        .replace("{{REPORT_PATH}}", report_path)
    )


class AIBridge:
    def __init__(self, cfg: dict[str, Any], *, enabled: bool | None = None) -> None:
        self.cfg = cfg
        ai = cfg.get("ai") or {}
        self.ai_cfg = ai
        self.enabled = bool(ai.get("enabled", True)) if enabled is None else enabled
        self.profile = str(ai.get("profile", "headless"))
        self.cmd = Path(str(ai.get("cmd", ""))) if ai.get("cmd") else None
        # --patch 覆盖层：让 headless profile 复用桌面账号登录（见 config/dsh-headless.patch.yml）
        patch_cfg = ai.get("patch")
        if patch_cfg:
            candidate = Path(str(patch_cfg))
            if not candidate.is_absolute():
                candidate = cfgmod.project_root(cfg) / candidate
            self.patch = candidate if candidate.is_file() else None
            self.patch_missing = None if candidate.is_file() else str(candidate)
        else:
            self.patch = None
            self.patch_missing = None
        self.json_events = bool(ai.get("json_events", True))
        self.timeout_s = int(ai.get("timeout_seconds", 240))
        self.state_dir = cfgmod.ensure_dir(cfgmod.data_dir(cfg, "state"))
        self.runs_dir = cfgmod.ensure_dir(cfgmod.data_dir(cfg, "runs"))
        self.logs_dir = cfgmod.ensure_dir(cfgmod.data_dir(cfg, "logs"))
        # 子进程的 DSH home：默认 data/dsh-home，相对路径按项目根解析。
        dsh_home_cfg = str(ai.get("dsh_home", "") or "").strip()
        dsh_home = Path(dsh_home_cfg) if dsh_home_cfg else Path("data") / "dsh-home"
        if not dsh_home.is_absolute():
            dsh_home = cfgmod.project_root(cfg) / dsh_home
        self.dsh_home = cfgmod.ensure_dir(dsh_home)

    # -------------------------------------------------------------- 子进程环境

    def _child_env(self, run_id: str | None = None) -> dict[str, str]:
        """给判断层子进程一套干净的环境变量。

        继承调用方的 `DSH_*` 会把编排层自己的 home / profile / 会话 id 带进判断层：
        实测 `DSH_HOME` 指向真实的 `~/.dsh`，于是 headless 每次启动重写
        `<DSH_HOME>/profiles/headless/cordis.yml` 时就写到了工作区之外，被沙箱拦下。

        这些 `DSH_*` 本来就是 DSH 注入给模型 shell 工具的"事实"
        （见 `@deepseek-ai/dsh-shell-env`：ambient 值会被 executor 丢弃后重新注入），
        不是 boot 的输入，所以这里整体剔除，只留下我们指定的 `DSH_HOME`。

        另外注入 `OPS_INVESTIGATE_RUN_ID`：判断层调调查工具时要靠它把"这一轮"
        的调用次数分开计。没有它，预算就变成全机器共享，等于没有预算。
        """
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("DSH_")}
        env["DSH_HOME"] = str(self.dsh_home)
        if run_id:
            env["OPS_INVESTIGATE_RUN_ID"] = run_id
        return env

    # -------------------------------------------------------------- session

    def _session_file(self, task: str) -> Path:
        return self.state_dir / f"ai-session-{task}.txt"

    def load_session(self, task: str) -> str | None:
        path = self._session_file(task)
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            return value or None
        return None

    def store_session(self, task: str, session_id: str | None) -> None:
        path = self._session_file(task)
        if session_id:
            path.write_text(session_id, encoding="utf-8")
        elif path.is_file():
            path.unlink()  # 会话失效就清掉，下次开新的

    # -------------------------------------------------------------- 调用

    def _argv(self, session_id: str | None, model_patch: Path | None = None) -> list[str]:
        base = [str(self.cmd)] if self.cmd else ["dsh"]
        argv = list(base)
        argv += ["--profile", self.profile]
        if self.patch:
            argv += ["--patch", str(self.patch)]
        # 模型 patch 必须排在 ai.patch 之后：patch 层是"后来者覆盖"，
        # 而 ai.patch 负责 provider 与凭据，模型要压在它上面。
        if model_patch:
            argv += ["--patch", str(model_patch)]
        if self.json_events:
            argv.append("--json")
        if session_id:
            argv += ["--session-id", session_id]
        argv.append("-")  # 从 stdin 读任务，避免命令行长度与转义问题
        return argv

    def call(
        self,
        *,
        task: str,
        run_id: str,
        template_path: Path,
        run_doc: dict[str, Any],
        use_session: bool = False,
        model: str | None = None,
    ) -> AIResult:
        if not self.enabled:
            return AIResult(ok=False, error="ai_disabled")
        if self.patch_missing:
            return AIResult(
                ok=False,
                error=(
                    f"配置了 ai.patch 但文件不存在：{self.patch_missing}。"
                    "这个 patch 负责把 headless profile 切到账号登录，缺了它会报缺少 API Key。"
                ),
            )

        verdict_path = self.runs_dir / f"{run_id}.verdict.json"
        report_out = self.runs_dir / f"{run_id}.ai-report.md"
        if verdict_path.exists():
            verdict_path.unlink()  # 清掉旧文件，否则无法判断这次到底写没写

        template = template_path.read_text(encoding="utf-8")
        prompt = build_prompt(
            template,
            run_json=json.dumps(run_doc, ensure_ascii=False, indent=2),
            verdict_path=str(verdict_path),
            report_path=str(report_out),
        )
        prompt_path = self.runs_dir / f"{run_id}.prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")

        # 模型由分流层决定（ai.model 写死时则用它）。写不进 patch 文件时返回 None，
        # 那就是"不做分流、用 profile 默认模型" —— 这一步失败绝不能让判断层整体失败。
        model_patch = routing.ensure_model_patch(self.cfg, model) if model else None

        session_id = self.load_session(task) if use_session else None
        started = time.perf_counter()
        result = self._run_once(prompt, session_id, run_id, model_patch)
        duration = time.perf_counter() - started

        # 会话可能已失效：清掉并用新会话重试一次（只重试一次，避免打转）
        if session_id and result["returncode"] != 0 and _looks_like_bad_session(result["stderr"]):
            self.store_session(task, None)
            started = time.perf_counter()
            result = self._run_once(prompt, None, run_id, model_patch)
            duration = time.perf_counter() - started
            result["note"] = "会话失效，已回退到新会话"

        log_path = self.logs_dir / f"{run_id}.ai.log"
        log_path.write_text(
            f"# task={task} profile={self.profile} session={session_id} dsh_home={self.dsh_home}\n"
            f"# returncode={result['returncode']} duration={duration:.1f}s\n"
            f"# argv={' '.join(result['argv'])}\n\n"
            f"===== STDOUT =====\n{result['stdout']}\n\n===== STDERR =====\n{result['stderr']}\n",
            encoding="utf-8",
        )

        events_path = None
        if self.json_events:
            events_path = self.runs_dir / f"{run_id}.events.ndjson"
            events_path.write_text(result["stdout"], encoding="utf-8")

        # 优先从结构化事件流里取 sessionId 与 token 用量；
        # 拿不到（例如没开 --json）才退回正则。
        usage = RunUsage()
        new_session = session_id
        tool_calls: dict[str, Any] = {}
        if events_path is not None:
            parsed_session, usage = parse_events(result["stdout"])
            tool_calls = parse_tool_calls(result["stdout"])
            if parsed_session:
                new_session = parsed_session
        if new_session is None:
            detected = SESSION_RE.search(result["stdout"] + result["stderr"])
            new_session = detected.group(0) if detected else None
        if use_session and new_session:
            self.store_session(task, new_session)

        payload: dict[str, Any] | None = None
        error: str | None = None
        if not verdict_path.is_file():
            hint = diagnose(result["stderr"], result["stdout"], self.timeout_s)
            error = f"判断层没有写出 verdict（returncode={result['returncode']}）"
            if hint:
                error = f"{error}。{hint}"
        else:
            try:
                payload = json.loads(verdict_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                error = f"verdict 不是合法 JSON: {exc}"
            else:
                errors = validate_verdict(payload, run_id)
                if errors:
                    error = "verdict 校验失败: " + "; ".join(errors)
                    payload = None

        return AIResult(
            ok=payload is not None,
            verdict=payload,
            session_id=new_session,
            duration_s=round(duration, 1),
            error=error,
            stdout_tail=result["stdout"][-2000:],
            stderr_tail=result["stderr"][-2000:],
            prompt_path=prompt_path,
            log_path=log_path,
            events_path=events_path,
            ai_report_path=report_out if report_out.is_file() else None,
            usage=usage,
            tool_calls=tool_calls,
            extra={"returncode": result["returncode"], "note": result.get("note")},
        )

    def _run_once(self, prompt: str, session_id: str | None,
                  run_id: str | None = None,
                  model_patch: Path | None = None) -> dict[str, Any]:
        argv = self._argv(session_id, model_patch)
        # Windows 上 .cmd 必须通过 cmd.exe 执行，否则 CreateProcess 会失败
        if argv[0].lower().endswith((".cmd", ".bat")):
            argv = ["cmd.exe", "/c"] + argv
        try:
            proc = subprocess.run(  # noqa: S603 - 命令来自本项目的配置文件
                argv,
                input=prompt.encode("utf-8"),
                capture_output=True,
                timeout=self.timeout_s,
                cwd=str(cfgmod.project_root(self.cfg)),
                env=self._child_env(run_id),
            )
            return {
                "returncode": proc.returncode,
                "stdout": proc.stdout.decode("utf-8", "replace"),
                "stderr": proc.stderr.decode("utf-8", "replace"),
                "argv": argv,
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "returncode": -1,
                "stdout": (exc.stdout or b"").decode("utf-8", "replace") if exc.stdout else "",
                "stderr": f"TIMEOUT after {self.timeout_s}s",
                "argv": argv,
            }
        except FileNotFoundError as exc:
            return {"returncode": -2, "stdout": "", "stderr": f"找不到命令: {exc}", "argv": argv}
        except Exception as exc:  # noqa: BLE001 - 桥接层绝不向上抛
            return {
                "returncode": -3,
                "stdout": "",
                "stderr": f"{type(exc).__name__}: {exc}",
                "argv": argv,
            }


def _looks_like_bad_session(stderr: str) -> bool:
    lowered = stderr.lower()
    return "session" in lowered and any(
        token in lowered for token in ("unknown", "not found", "invalid", "不存在")
    )


def diagnose(stderr: str, stdout: str = "", timeout_s: int = 0) -> str | None:
    """把 dsh 的原始报错翻译成「人话 + 怎么办」。

    静默失败的反面不是"报错"，而是"报错但看不出该怎么办"。
    """
    text = f"{stderr}\n{stdout}"
    lowered = text.lower()
    if not text.strip():
        return None
    if "missing_credential" in lowered or "no api key" in lowered:
        return (
            "AI 判断层缺少模型凭据：headless profile 默认走 deepseek-official（需要 API Key）。"
            "解决办法：确认 config/dsh-headless.patch.yml 存在、且 config.toml 里 ai.patch 指向它，"
            "从而切到 deepseek-account 复用桌面登录；或设置环境变量 DEEPSEEK_API_KEY。"
        )
    if "eperm" in lowered and "profiles" in lowered:
        return (
            "无法写入 DSH profile 目录。判断层默认把子进程的 DSH_HOME 重定向到项目内的 "
            "data/dsh-home（见 config.toml 的 ai.dsh_home），正常不该再出现这个错；"
            "若仍出现，说明 ai.dsh_home 被指到了工作区之外，或该目录不可写。"
        )
    if "enotfound" in lowered or "econnrefused" in lowered or "getaddrinfo" in lowered:
        return "无法连接模型服务，请检查网络或代理设置。"
    if "timeout" in lowered or "timed out" in lowered:
        return f"判断层超时（上限 {timeout_s}s）。可调大 ai.timeout_seconds，或减少每次送入的事实量。"
    if "cannot find" in lowered or "不是内部或外部命令" in text or "commandnotfound" in lowered:
        return "找不到 dsh 命令，请检查 config.toml 里 ai.cmd 指向的路径。"
    if "patch" in lowered and ("invalid" in lowered or "parse" in lowered):
        return "patch 文件格式错误，请检查 config/dsh-headless.patch.yml 的 YAML 语法。"
    return None
