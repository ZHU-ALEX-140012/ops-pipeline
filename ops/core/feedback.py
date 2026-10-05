"""人工反馈：把"这条判得对吗"变成数据。

## 为什么需要人工反馈

离线黄金集能回答"模型在 19 个场景上多准"，回答不了"**它在你这儿多准**"。
真实环境里的误报往往长得跟黄金集里的不一样 —— 只有收到邮件的人知道。

## 为什么用"回复邮件"而不是"点链接"

最自然的交互是邮件里放两个按钮，点一下就打上分。但那需要一个**从手机可达的地址** ——
家宽没有公网 IP、没有域名，链接点下去只会打到手机上。这和当初企业微信回调 URL
撞的是同一堵墙。

**回复邮件不需要任何可达地址**：邮件出去、回复回来，中间不需要你的机器能被访问。
代价是需要轮询邮箱（`feedback_scan` 任务），但那是本地就能做的事。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

# 主题里的标记：#<token>。用 run_id 的最后一段（uuid4 的 6 位十六进制），
# 足够短、便于人眼确认，又足够唯一。
TOKEN_RE = re.compile(r"#([0-9a-f]{4,12})\b", re.IGNORECASE)

# 投票词。**顺序很重要**：必须先判负向 —— "不对" 里含 "对"，
# 先判正向会把"不对"当成"对"，这是这类关键词匹配最经典的坑。
DOWN_WORDS = ("👎", "错", "不对", "误报", "有问题", "误判", "no", "bad", "false", "wrong")
UP_WORDS = ("👍", "对", "正确", "判对", "没问题", "准确", "yes", "good", "ok", "true", "right")

# 引用的原文不该参与判断：多数客户端回复时会把原邮件整段带上
QUOTE_MARKERS = ("写道：", "写道:", "wrote:", "-----原始邮件-----", "发件人:", "from:")


@dataclass
class ParsedReply:
    token: str
    vote: str
    note: str


def token_for(run_id: str) -> str:
    """run_id 的最后一段就是标记。"""
    return run_id.rsplit("-", 1)[-1]


def mark_subject(subject: str, run_id: str) -> str:
    """给主题加上可回信的标记（已带就不重复加）。"""
    token = token_for(run_id)
    if TOKEN_RE.search(subject):
        return subject
    return f"{subject} [#{token}]"


def find_run_by_token(conn: sqlite3.Connection, token: str) -> str | None:
    """按标记找回 run_id。

    只匹配"以 -<token> 结尾"的 run_id，避免误配到中间片段。
    同时排除评测产生的 eval-* 记录 —— 它们不是真实告警。
    """
    if not re.fullmatch(r"[0-9a-fA-F]{4,12}", token or ""):
        return None
    row = conn.execute(
        "SELECT id FROM run WHERE id LIKE ? AND id NOT LIKE 'eval-%' "
        "ORDER BY started_at DESC LIMIT 1",
        (f"%-{token}",),
    ).fetchone()
    return str(row[0]) if row else None


def first_meaningful_line(body: str) -> str:
    """取正文里第一条"人写的话"，跳过引用与空行。

    回复时大多数人只在最上面写一两个字，下面全是引用原文 ——
    不跳过引用就会把原邮件里的"alert"之类当成反馈内容。
    """
    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(">"):
            continue
        lowered = line.lower()
        if any(marker in lowered for marker in QUOTE_MARKERS):
            break
        # 去掉常见的列表/引用前缀
        line = re.sub(r"^[-*•\s]+", "", line).strip()
        if line:
            return line
    return ""


def classify(text: str) -> str | None:
    """把一句话判成 up / down / None。**先判负向。**"""
    lowered = (text or "").strip().lower()
    if not lowered:
        return None
    if any(word in lowered for word in DOWN_WORDS):
        return "down"
    if any(word in lowered for word in UP_WORDS):
        return "up"
    return None


def parse_reply(subject: str, body: str) -> ParsedReply | None:
    """从一封回信里解析出 (token, vote, note)。解析不出来就返回 None。

    解析不出来是**正常情况**（自动回复、签名、无关邮件），
    调用方应该静默跳过，不要把它当成错误。
    """
    match = TOKEN_RE.search(subject or "")
    if not match:
        return None
    token = match.group(1).lower()
    line = first_meaningful_line(body)
    vote = classify(line)
    if vote is None:
        return None
    note = line[:200]
    return ParsedReply(token=token, vote=vote, note=note)


def apply_reply(store_mod: Any, conn: sqlite3.Connection, reply: ParsedReply,
                source: str = "email") -> str | None:
    """把解析出的回信写进库。返回被反馈的 run_id；找不到则返回 None。"""
    run_id = find_run_by_token(conn, reply.token)
    if not run_id:
        return None
    row = conn.execute("SELECT task FROM run WHERE id=?", (run_id,)).fetchone()
    store_mod.record_feedback(
        conn, run_id=run_id, vote=reply.vote, source=source,
        task=str(row[0]) if row else None, note=reply.note,
    )
    return run_id


def feedback_hint(run_id: str) -> str:
    """邮件里那行"怎么反馈"。

    刻意用回复邮件而不是链接：链接要有个从手机可达的地址，
    而家宽两者都没有（见模块开头）。
    """
    return (
        f"**这条判得对吗？** 直接回复本邮件：正文写「对」或「错」即可"
        f"（回信请保留主题里的 `#{token_for(run_id)}`，系统靠它认领是哪一条）。"
    )


# ---------------------------------------------------------------- 邮件正文提取

def decode_header_value(raw: Any) -> str:
    """解码邮件头（RFC 2047）。

    中文主题在原始邮件里是 `=?utf-8?B?...?=` 形式，不解码就找不到标记。
    """
    from email.header import decode_header

    if raw is None:
        return ""
    parts = decode_header(str(raw))
    out = []
    for text, charset in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                out.append(text.decode("utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def extract_subject_and_body(raw: bytes) -> tuple[str, str]:
    """从一封原始邮件里取出解码后的主题与纯文本正文。

    优先 `text/plain`；只有 HTML 时做一次粗糙的去标签（够用来判一个"对/错"）。
    """
    from email import message_from_bytes
    from email.message import Message

    message: Message = message_from_bytes(raw)
    subject = decode_header_value(message.get("Subject"))

    body = ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                payload = part.get_payload(decode=True) or b""
                body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                break
        if not body:
            for part in message.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True) or b""
                    html = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    body = re.sub(r"<[^>]+>", " ", html)
                    break
    else:
        payload = message.get_payload(decode=True) or b""
        body = payload.decode(message.get_content_charset() or "utf-8", errors="replace")

    return subject, body
