"""收人工反馈：连邮箱读回复，落到 feedback 表。

## 为什么走 IMAP 而不是"点邮件里的链接"

链接要有一个**从手机可达的地址**。家宽没有公网 IP、没有域名，点下去只会打到手机上 ——
这和当初企业微信回调 URL 撞的是同一堵墙。

回复邮件不需要任何可达地址：邮件出去、回复回来，中间不需要你的机器能被访问。

## 边界

* **只读**收件箱里**未读**的邮件；识别成功才标记已读（`mark_seen` 可关）。
* 认领靠主题里的 `#<token>`，与 run_id 后缀对应 —— 伪造一封邮件也只能改**已有**记录的反馈，
  不能凭空造 run。
* 每轮最多处理 `max_messages` 封，且**单轮只发一次**告警（没有发现就静默）。
"""

from __future__ import annotations

import imaplib
import uuid
from datetime import datetime, timezone
from typing import Any

from ..core import config as cfgmod
from ..core import feedback as fb
from ..core import http, pipeline, store

TASK = "feedback_scan"


def _run_id(started_at: str) -> str:
    return f"{started_at.replace('-', '').replace(':', '')}-{TASK}-{uuid.uuid4().hex[:6]}"


def feedback_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg.get("feedback") or {}


def scan_once(cfg: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    """拉一次未读回复，返回处理统计。抛出异常由调用方收尾。"""
    conf = feedback_cfg(cfg)
    host = str(conf.get("imap_host", "imap.qq.com"))
    port = int(conf.get("imap_port", 993))
    mailbox = str(conf.get("mailbox", "INBOX"))
    limit = int(conf.get("max_messages", 20))
    mark_seen = bool(conf.get("mark_seen", True))

    user = cfgmod.resolve_secret(cfg, conf.get("user_env", "OPS_MAIL_USER"))
    password = cfgmod.resolve_secret(cfg, conf.get("pass_env", "OPS_MAIL_PASS"))
    if not user or not password:
        return {"skipped": True,
                "reason": "未配置邮箱凭据（OPS_MAIL_USER / OPS_MAIL_PASS）"}

    conn = store.connect(cfgmod.data_dir(cfg, "ops.db"))
    applied: list[dict[str, Any]] = []
    unclaimed = 0
    unparsed = 0
    seen = 0
    # 只记录**成功认领**的邮件 id。⚠️ 第一版是把整个未读列表都标记已读 ——
    # 那会把用户收件箱里所有未读邮件（包括私人邮件）一起标成已读，
    # 是真正会造成破坏的副作用。只动自己认领的那几封。
    claimed_ids: list[bytes] = []

    client = imaplib.IMAP4_SSL(host, port)
    try:
        client.login(user, password)
        client.select(mailbox, readonly=not mark_seen)
        # 只看未读：已读的不再重复处理，避免同一封回信反复计数
        status, data = client.search(None, "UNSEEN")
        if status != "OK":
            raise RuntimeError(f"IMAP 搜索失败：{status}")
        ids = (data[0] or b"").split()
        for msg_id in ids[-limit:]:
            status, payload = client.fetch(msg_id, "(RFC822)")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                continue
            seen += 1
            raw = payload[0][1]
            subject, body = fb.extract_subject_and_body(raw)
            reply = fb.parse_reply(subject, body)
            if reply is None:
                unparsed += 1
                continue
            if dry_run:
                applied.append({"token": reply.token, "vote": reply.vote, "run_id": None})
                continue
            run_id = fb.apply_reply(store, conn, reply, source="email")
            if run_id is None:
                unclaimed += 1
                continue
            applied.append({"token": reply.token, "vote": reply.vote, "run_id": run_id})
            claimed_ids.append(msg_id)

        if mark_seen and not dry_run and claimed_ids:
            for msg_id in claimed_ids:
                client.store(msg_id, "+FLAGS", "\\Seen")
    finally:
        try:
            client.logout()
        except Exception:  # noqa: BLE001 - 登出失败不该淹没真正的结果
            pass
        conn.close()

    return {
        "skipped": False,
        "seen": seen,
        "applied": applied,
        "unclaimed": unclaimed,
        "unparsed": unparsed,
        "marked_seen": len(claimed_ids),
    }


def run(cfg: dict[str, Any], *, dry_run: bool = False, no_ai: bool = False) -> int:
    conf = feedback_cfg(cfg)
    if not conf.get("enabled", True):
        print(f"[{TASK}] 未启用（config: feedback.enabled = false）")
        return 0

    started_at = http.utc_now()
    run_id = _run_id(started_at)
    conn = store.connect(cfgmod.data_dir(cfg, "ops.db"))
    store.start_run(conn, run_id, TASK, started_at)

    try:
        result = scan_once(cfg, dry_run=dry_run)
        if result.get("skipped"):
            store.finish_run(conn, run_id, finished_at=http.utc_now(), status="ok",
                             ai_called=0, ai_status="skipped", exit_code=0,
                             summary=result["reason"])
            print(f"[{TASK}] {run_id} 跳过：{result['reason']}")
            return 0

        applied = result["applied"]
        up = sum(1 for a in applied if a["vote"] == "up")
        down = sum(1 for a in applied if a["vote"] == "down")
        summary = (f"读 {result['seen']} 封，认领 {len(applied)} 条"
                   f"（对 {up} / 错 {down}），无法认领 {result['unclaimed']}，"
                   f"解析不出 {result['unparsed']}，标记已读 {result.get('marked_seen', 0)}")
        store.finish_run(conn, run_id, finished_at=http.utc_now(), status="ok",
                         items_ok=len(applied), items_failed=0,
                         ai_called=0, ai_status="skipped", exit_code=0, summary=summary[:500])
        print(f"[{TASK}] {run_id} {summary}")
        return 0
    except Exception as exc:  # noqa: BLE001
        # 收信失败是**正常情况**（密码改了、网络断了），不能把流水线打挂；
        # 但必须落库，这样"反馈一直收不到"这件事是可见的。
        store.finish_run(conn, run_id, finished_at=http.utc_now(), status="failed",
                         exit_code=3, summary=f"{type(exc).__name__}: {exc}"[:500])
        print(f"[{TASK}] {run_id} 失败：{type(exc).__name__}: {exc}")
        return 0
    finally:
        conn.close()
