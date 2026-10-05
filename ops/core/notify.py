"""告警发送。三个通道：企业微信群机器人 / 企业微信自建应用 / 邮件。

铁律：**AI 不直接发告警**。这里只接收脚本裁决过的结论。
所有失败都转成返回值，绝不向上抛异常打断流水线。

三个通道的取舍：
* `wecom`（群机器人）最简单，但需要**普通内部群**；全员群 / 外部群 / 单人企业拿不到入口。
* `wecom_app`（自建应用）单人企业也能用，代价是要在后台拿 CorpID/Secret/AgentId，
  且要维护 access_token（两小时过期，这里做了本地缓存）。
* `email` 最通用，代码量最小，但手机上的提醒不如聊天消息显眼。

状态语义（很重要，别让"没配"和"发失败"混在一起）：
* `sent`    —— 接口确认投递成功（errcode == 0）
* `dry-run` —— **没配置**这个通道，根本没尝试（消息会落本地日志）
* `failed`  —— 尝试了但失败（网络、凭据无效、接口拒绝）
"""

from __future__ import annotations

import json
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import config as cfgmod
from .http import post_json, post_text, request_json, utc_now

WECOM_LIMIT = 4000        # 群机器人 markdown 上限 4096 字节，留余量
WECOM_APP_LIMIT = 1800    # 应用消息 markdown 容量更小，取保守值
NTFY_LIMIT = 3500         # ntfy 单条消息上限约 4KB
PUSHPLUS_LIMIT = 20000    # PushPlus 正文上限较宽松，留个保守值
WECOM_API = "https://qyapi.weixin.qq.com/cgi-bin"
TOKEN_REFRESH_MARGIN_S = 300  # 提前 5 分钟续期，别卡在过期瞬间

# 常见错误码 → 人话 + 怎么办。
# 静默失败的反面不是"报错"，而是"报错但看不出该怎么办"。
# 这张表都是从实际踩过的坑里长出来的。
WECOM_ERRCODE_HINTS: dict[int, str] = {
    40001: "Secret 不对或已被重置 —— 用 scripts\\set-wecom-app.cmd 重新填写",
    40013: "CorpID 不对 —— 后台「我的企业 → 团队信息」里的「团队ID」才是 CorpID",
    40014: "access_token 无效（已自动清缓存，下一轮会重新获取）",
    41001: "缺少 access_token 参数（本工具内部错误）",
    45009: "接口调用超过频率限制",
    60020: "当前公网 IP 不在「企业可信IP」白名单里 —— "
           "到应用详情页 →「开发者接口」→「企业可信IP」把本机公网 IP 加上。"
           "注意：家用宽带的 IP 会变，建议同时配好邮件兜底，避免 IP 一变就静默丢告警",
    82001: "应用的可见范围为空 —— 到应用详情页把你自己的部门/成员加进「可见范围」",
    93000: "群机器人 webhook 无效 —— key 抄错，或机器人已被移出群",
}

# PushPlus（国内服务，推微信）
# 只收录**实测过**的错误码：猜错码的含义比不给提示更糟（会把人引到错误方向）。
# 未收录的码不用慌，PushPlus 自己会在 msg 里给中文说明，调用方会原样带出来。
PUSHPLUS_CODE_HINTS: dict[int, str] = {
    903: "token 不正确 —— 到 https://www.pushplus.plus/ 用微信重新扫码拿新 token",
}

# ntfy 的错误码（HTTP 4xx，详情在响应体里）
NTFY_CODE_HINTS: dict[int, str] = {
    40301: "该话题是保留话题，换一个随机话题名",
    40302: "该话题名不被允许，换一个随机话题名",
    40401: "话题名为空 —— 地址要形如 https://ntfy.sh/你的话题",
    42901: "触发限流，稍后重试",
    42907: "当天消息数已达上限",
}


def _hint_for(errcode: Any, channel: str) -> str:
    try:
        code = int(errcode)
    except (TypeError, ValueError):
        return ""
    hint = WECOM_ERRCODE_HINTS.get(code)
    if hint:
        return f"｜建议：{hint}"
    return f"｜未收录的错误码，可到 https://open.work.weixin.qq.com/devtool/query?e={code} 查询"


@dataclass
class SendResult:
    channel: str
    status: str  # sent | failed | dry-run
    detail: str = ""


def _truncate(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[: limit - 20].decode("utf-8", "ignore") + "\n…（已截断）"


SEVERITY_LABEL = {
    "critical": "🔴 严重",
    "alert": "🟠 需要注意",
    "notice": "🟡 提醒",
    "info": "🟢 正常",
}


def _shorten(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_markdown(
    *,
    headline: str,
    severity: str,
    plain_summary: str | None = None,
    impact: str | None = None,
    action: str | None = None,
    evidence: list[str] | None = None,
    metrics: dict[str, Any] | None = None,
    report_path: str | None = None,
    ai_degraded: bool = False,
    item_label: str = "目标",
) -> str:
    """渲染告警消息。

    **设计原则：邮件是"提醒"，不是"分析报告"。**

    之前这里把判断层的 `evidence` 原样贴进邮件，结果是满屏
    `samples:` / `baseline.worldtime:` / `modal_body_hash` 这类机器字段 ——
    写代码的人勉强能读，收邮件的人看到的是一堵墙。
    现在只放三块人话：**发生了什么 → 这意味着什么 → 你该做什么**；
    技术细节留在 `report_path` 指向的报告文件里。
    """
    lines: list[str] = [f"{SEVERITY_LABEL.get(severity, severity)}　**{headline}**"]
    if ai_degraded:
        lines.append("> ⓘ 判断层不可用，以下是规则层的初步判断")

    if plain_summary:
        lines += ["", plain_summary.strip()]
    if impact:
        lines += ["", f"**这意味着**：{impact.strip()}"]
    if action:
        lines += ["", f"**建议**：{action.strip()}"]

    # 没有大白话解释时（例如判断层只给了 evidence），退回展示证据 ——
    # 至少不能发一封空邮件出去。
    if not plain_summary and evidence:
        lines += ["", "**初步证据**"]
        lines += [f"- {_shorten(item)}" for item in evidence[:5]]

    footer: list[str] = []
    if metrics and metrics.get("targets"):
        failed = metrics.get("failed") or 0
        footer.append(
            f"{item_label} {metrics.get('ok')}/{metrics.get('targets')} 正常"
            + (f"，失败 {failed}" if failed else "")
        )
    if report_path:
        footer.append(f"完整报告：{report_path}")
    if footer:
        lines += ["", "———", *footer]

    return _truncate("\n".join(lines), WECOM_LIMIT)


def smtp_login(server: Any, user: str, password: str) -> None:
    """登录 SMTP，**优先用 AUTH LOGIN**。

    为什么不用 smtplib 现成的 login()：Python 会优先选 AUTH PLAIN，而 QQ 邮箱的
    反滥用逻辑对 PLAIN 的处理是**直接掐断连接**，于是真正的 535 错误被吞掉，
    调用方只看到 `SMTPServerDisconnected: Connection unexpectedly closed`
    —— 那会把人引向"网络问题/端口被封"，而真实原因是授权码不对。

    实测对比（smtp.qq.com:465，同一个错误授权码）：
        AUTH PLAIN → SMTPServerDisconnected（看不出原因）
        AUTH LOGIN → 535 Login fail. Account is abnormal, ...（原因一目了然）

    另一个实测细节：某些情况下构造函数里的**隐式 EHLO 没有把能力表填上**
    （`esmtp_features` 是空的），这会让"服务端支不支持 LOGIN"判断错误、
    又退回 PLAIN。所以这里发现能力表为空时显式补一次 EHLO。
    """
    import base64

    mechanisms = str(server.esmtp_features.get("auth", "")).upper().split()
    if not mechanisms:
        try:
            server.ehlo()
        except Exception:  # noqa: BLE001 - 补 EHLO 失败就交给下面的兜底分支
            pass
        mechanisms = str(server.esmtp_features.get("auth", "")).upper().split()

    if "LOGIN" not in mechanisms:
        server.login(user, password)  # 服务端确实不支持 LOGIN 才交回 smtplib
        return

    server.docmd("AUTH", "LOGIN")
    server.docmd(base64.b64encode(user.encode("utf-8")).decode("ascii"))
    code, response = server.docmd(base64.b64encode(password.encode("utf-8")).decode("ascii"))
    if code != 235:
        text = response.decode("utf-8", "replace") if isinstance(response, bytes) else str(response)
        raise smtplib.SMTPAuthenticationError(code, text.encode("utf-8", "replace"))


class Notifier:
    def __init__(self, cfg: dict[str, Any], *, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        alert = cfg.get("alert") or {}
        self.primary = str(alert.get("primary", "wecom"))
        self.fallback = [str(item) for item in (alert.get("fallback") or [])]
        self.wecom_cfg = alert.get("wecom") or {}
        self.wecom_app_cfg = alert.get("wecom_app") or {}
        self.ntfy_cfg = alert.get("ntfy") or {}
        self.pushplus_cfg = alert.get("pushplus") or {}
        self.email_cfg = alert.get("email") or {}
        self.log_path = cfgmod.ensure_dir(cfgmod.data_dir(cfg, "logs")) / "notify.log"
        self.token_cache = cfgmod.ensure_dir(cfgmod.data_dir(cfg, "state")) / "wecom-token.json"

    # -------------------------------------------------------------- 企业微信群机器人

    def _send_wecom(self, markdown: str) -> SendResult:
        env_name = self.wecom_cfg.get("webhook_env", "OPS_WECOM_WEBHOOK")
        webhook = cfgmod.resolve_secret(self.cfg, env_name)
        if not webhook:
            # 未配置凭据按 dry-run 处理，而不是 failed：
            # 首次部署的人应该看到"链路通了、只是还没配通道"，而不是"发送失败"。
            # 细节字符串里写明原因和配置途径，避免静默掩盖配置缺失。
            return SendResult(
                "wecom", "dry-run",
                f"未配置群机器人 webhook（{env_name} 既不在环境变量里，也不在 "
                f"{cfgmod.SECRETS_FILENAME} 里）—— 未真实发送，已落本地日志",
            )
        ok, detail, body = post_json(
            webhook, {"msgtype": "markdown", "markdown": {"content": _truncate(markdown, WECOM_LIMIT)}}
        )
        return self._wecom_result("wecom", ok, detail, body)

    # -------------------------------------------------------------- 企业微信自建应用

    def _wecom_app_credentials(self) -> tuple[str | None, str | None, str | None, list[str]]:
        cfg = self.wecom_app_cfg
        corpid = cfgmod.resolve_secret(self.cfg, cfg.get("corpid_env"))
        secret = cfgmod.resolve_secret(self.cfg, cfg.get("secret_env"))
        agentid = cfgmod.resolve_secret(self.cfg, cfg.get("agentid_env"))
        missing = [
            str(name)
            for name, value in (
                (cfg.get("corpid_env"), corpid),
                (cfg.get("secret_env"), secret),
                (cfg.get("agentid_env"), agentid),
            )
            if not value
        ]
        return corpid, secret, agentid, missing

    def _access_token(self, corpid: str, secret: str) -> tuple[str | None, str | None]:
        """取 access_token，带本地缓存（官方有效期 7200 秒）。

        为什么要缓存：token 有配额限制，而且每小时跑一次的巡检没必要每次都换新的。
        缓存文件在 data/state 下（已 gitignore），**内含密钥**，不要把 data/ 分享出去。
        """
        now = time.time()
        if self.token_cache.is_file():
            try:
                cached = json.loads(self.token_cache.read_text(encoding="utf-8"))
                fresh = float(cached.get("expires_at", 0)) > now + TOKEN_REFRESH_MARGIN_S
                if cached.get("corpid") == corpid and fresh and cached.get("access_token"):
                    return str(cached["access_token"]), None
            except (OSError, ValueError, TypeError):
                pass  # 缓存坏了就当没有，重新取

        url = f"{WECOM_API}/gettoken?corpid={quote(corpid)}&corpsecret={quote(secret)}"
        resp = request_json("GET", url, timeout_ms=10000)
        if resp.error:
            return None, f"取 access_token 失败：{resp.error}"
        body = resp.body if isinstance(resp.body, dict) else None
        if body is None:
            return None, f"取 access_token 返回的不是 JSON：{resp.text[:200]}"
        if body.get("errcode") != 0:
            return None, (
                f"取 access_token 被拒：errcode={body.get('errcode')} "
                f"errmsg={body.get('errmsg')!r}（常见原因：CorpID/Secret 抄错）"
            )
        token = body.get("access_token")
        if not token:
            return None, "取 access_token 成功但响应里没有 access_token"
        expires_in = float(body.get("expires_in") or 7200)
        try:
            self.token_cache.write_text(
                json.dumps({"corpid": corpid, "access_token": token,
                            "expires_at": now + expires_in}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass  # 缓存写不了不影响本次发送
        return str(token), None

    def _send_wecom_app(self, markdown: str) -> SendResult:
        corpid, secret, agentid, missing = self._wecom_app_credentials()
        if missing:
            return SendResult(
                "wecom_app", "dry-run",
                f"未配置企业微信自建应用凭据（缺 {', '.join(missing)}）—— 未真实发送，已落本地日志",
            )
        assert corpid and secret and agentid

        token, error = self._access_token(corpid, secret)
        if not token:
            return SendResult("wecom_app", "failed", error or "取 access_token 失败")

        payload = {
            "touser": str(self.wecom_app_cfg.get("touser", "@all")),
            "msgtype": "markdown",
            "agentid": int(agentid),
            "markdown": {"content": _truncate(markdown, WECOM_APP_LIMIT)},
        }
        ok, detail, body = post_json(f"{WECOM_API}/message/send?access_token={token}", payload)
        result = self._wecom_result("wecom_app", ok, detail, body)
        if result.status == "failed" and "errcode=40014" in result.detail:
            # token 失效：删掉缓存，下一轮会重新获取
            self.token_cache.unlink(missing_ok=True)
        return result

    # -------------------------------------------------------------- 结果判定（两个企业微信通道共用）

    @staticmethod
    def _wecom_result(channel: str, ok: bool, detail: str, body: Any) -> SendResult:
        if not ok:
            return SendResult(channel, "failed", detail)
        # 企业微信**即使 key 无效也返回 HTTP 200**，真正的结果在 errcode 里。
        # 不检查它就会把"凭据配错了"当成"发送成功" —— 那是最阴险的静默失败。
        if not isinstance(body, dict):
            return SendResult(channel, "failed", f"响应不是 JSON，无法确认投递结果：{detail}")
        errcode = body.get("errcode")
        if errcode != 0:
            return SendResult(
                channel, "failed",
                f"企业微信拒绝投递：errcode={errcode} errmsg={body.get('errmsg')!r}"
                f"{_hint_for(errcode, channel)}（{detail}）",
            )
        invalid = body.get("invaliduser") or body.get("invalidparty")
        if invalid:
            return SendResult(
                channel, "failed",
                f"errcode=0 但有无效接收人：invalid={invalid}"
                f"｜建议：检查应用详情页的「可见范围」是否包含你自己（{detail}）",
            )
        return SendResult(channel, "sent", f"errcode=0（{detail}）")

    # -------------------------------------------------------------- PushPlus

    def _send_pushplus(self, subject: str, markdown: str) -> SendResult:
        """PushPlus：微信扫码登录拿 token，消息直接推到微信，不用装 App。

        对"国内安卓手机 + 应用商店锁了 ntfy"的场景，这是最省事的一条路：
        免费额度足够个人告警用，且服务在国内（实测 0.1s）。
        """
        env_name = self.pushplus_cfg.get("token_env", "OPS_PUSHPLUS_TOKEN")
        token = cfgmod.resolve_secret(self.cfg, env_name)
        if not token:
            return SendResult(
                "pushplus", "dry-run",
                f"未配置 PushPlus token（{env_name} 不在环境变量也不在 {cfgmod.SECRETS_FILENAME} 里）"
                "—— 未真实发送，已落本地日志",
            )
        payload = {
            "token": token,
            "title": subject[:100],
            "content": _truncate(markdown, PUSHPLUS_LIMIT),
            "template": str(self.pushplus_cfg.get("template", "markdown")),
        }
        url = str(self.pushplus_cfg.get("api", "https://www.pushplus.plus/send"))
        ok, detail, body = post_json(url, payload)
        if not ok:
            return SendResult("pushplus", "failed", detail)
        if not isinstance(body, dict):
            return SendResult("pushplus", "failed", f"响应不是 JSON，无法确认投递结果：{detail}")
        code = body.get("code")
        if code != 200:
            hint = ""
            try:
                hint = PUSHPLUS_CODE_HINTS.get(int(code), "")
            except (TypeError, ValueError):
                hint = ""
            return SendResult(
                "pushplus", "failed",
                f"PushPlus 拒绝投递：code={code} msg={body.get('msg')!r}"
                f"{('｜建议：' + hint) if hint else ''}（{detail}）",
            )
        return SendResult("pushplus", "sent", f"code=200 msg={body.get('msg')!r}（{detail}）")

    # -------------------------------------------------------------- ntfy

    def _send_ntfy(self, markdown: str) -> SendResult:
        """ntfy：手机真推送，无需注册、无域名、无 IP 白名单。

        适合本项目这种"一个人、家用宽带、没有公网服务器"的场景。
        代价是消息经过第三方服务器，所以别用易猜的话题名（话题名就是唯一的凭据）。
        """
        env_name = self.ntfy_cfg.get("url_env", "OPS_NTFY_URL")
        url = cfgmod.resolve_secret(self.cfg, env_name)
        if not url:
            return SendResult(
                "ntfy", "dry-run",
                f"未配置推送地址（{env_name} 不在环境变量也不在 {cfgmod.SECRETS_FILENAME} 里）"
                "—— 未真实发送，已落本地日志",
            )
        headers = {
            # Title 故意用 ASCII：HTTP 头带中文容易被中间的代理/网关搞坏
            "Title": str(self.ntfy_cfg.get("title", "ops-pipeline")),
            "Priority": str(self.ntfy_cfg.get("priority", "default")),
            "Tags": str(self.ntfy_cfg.get("tags", "warning")),
        }
        ok, detail, body = post_text(url, _truncate(markdown, NTFY_LIMIT), headers=headers)
        if ok:
            return SendResult("ntfy", "sent", detail)
        code = body.get("code") if isinstance(body, dict) else None
        hint = ""
        try:
            hint = NTFY_CODE_HINTS.get(int(code), "") if code is not None else ""
        except (TypeError, ValueError):
            hint = ""
        return SendResult("ntfy", "failed", f"{detail}{('｜建议：' + hint) if hint else ''}")

    # -------------------------------------------------------------- 邮件

    def _send_email(self, subject: str, body: str) -> SendResult:
        if not self.email_cfg.get("enabled"):
            return SendResult("email", "dry-run", "email 通道未启用（alert.email.enabled = false）")
        user = cfgmod.resolve_secret(self.cfg, self.email_cfg.get("user_env"))
        password = cfgmod.resolve_secret(self.cfg, self.email_cfg.get("pass_env"))
        to_addr = cfgmod.resolve_secret(self.cfg, self.email_cfg.get("to_env")) or user
        if not (user and password and to_addr):
            return SendResult("email", "dry-run", "邮件凭据未配置（环境变量或本地密钥文件）")
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = to_addr
        msg.set_content(body)
        try:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                self.email_cfg.get("smtp_host", "smtp.qq.com"),
                int(self.email_cfg.get("smtp_port", 465)),
                context=context,
                timeout=15,
            ) as server:
                smtp_login(server, user, password)
                server.send_message(msg)
            return SendResult("email", "sent", f"→ {to_addr}")
        except smtplib.SMTPAuthenticationError as exc:
            # 认证失败是最常见的情况，单独给出可执行的指引
            detail = exc.smtp_error.decode("utf-8", "replace") if isinstance(
                exc.smtp_error, bytes
            ) else str(exc.smtp_error)
            return SendResult(
                "email", "failed",
                f"SMTP 认证被拒：{exc.smtp_code} {detail.strip()}"
                f"｜建议：确认用的是**授权码**而不是登录密码，且长度与后台一致"
                f"（QQ 邮箱为 16 位小写字母）；核对 scripts\\set-secret.cmd -List 里的 OPS_MAIL_PASS",
            )
        except Exception as exc:  # noqa: BLE001 - 发送失败不能打断流水线
            return SendResult("email", "failed", f"{type(exc).__name__}: {exc}")

    # -------------------------------------------------------------- 对外

    def _dispatch(self, channel: str, subject: str, markdown: str) -> SendResult:
        if channel == "wecom":
            return self._send_wecom(markdown)
        if channel == "wecom_app":
            return self._send_wecom_app(markdown)
        if channel == "ntfy":
            return self._send_ntfy(markdown)
        if channel == "pushplus":
            return self._send_pushplus(subject, markdown)
        if channel == "email":
            return self._send_email(subject, markdown)
        return SendResult(channel, "failed", f"未知通道 {channel!r}")

    def send(self, *, subject: str, markdown: str) -> SendResult:
        """按主通道发送，失败再依次尝试 fallback。

        语义：`dry-run` = 这个通道没配（不算失败）；`failed` = 配了但发不出去。
        只要有一个通道成功就算成功；全都没成功时，把每个通道的结果汇总报出来，
        免得只看到最后一条而误判原因。
        """
        if self.dry_run:
            return SendResult(self.primary, "dry-run", "命令行指定了 --dry-run，未真实发送")

        order: list[str] = []
        for channel in [self.primary, *self.fallback]:
            if channel and channel not in order:
                order.append(channel)

        results = [self._dispatch(channel, subject, markdown) for channel in order]

        for result in results:
            if result.status == "sent":
                if len(results) > 1 and result.channel != self.primary:
                    result.detail = f"{result.detail}（主通道未成功，经备用通道）"
                return result

        failed = [r for r in results if r.status == "failed"]
        if failed:
            detail = "; ".join(f"{r.channel}: {r.detail}" for r in results)
            return SendResult(self.primary, "failed", detail)
        return results[0] if results else SendResult("none", "dry-run", "没有配置任何告警通道")

    def write_local_log(self, line: str) -> Path:
        """dry-run / 兜底时把告警落到本地日志（保证"至少有人能看到"）。"""
        record = {"ts": utc_now(), "line": line}
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return self.log_path
