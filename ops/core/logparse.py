"""日志模板提取（Drain 算法，纯 Python 实现）。

要解决的问题：日志一行一个样，`user 12345 failed` 和 `user 67890 failed`
是同一类问题，但直接按原文计数会得到成千上万个"各自只出现一次"的键，
既看不出量级也看不出趋势。模板提取把变量位置归一成 `<*>`，
于是"这一类错误出现了 412 次"才成为可统计、可比较的对象。

算法（Drain）：
1. 取出日志级别与时间戳（时间戳另有用途，不参与模板）；
2. 把每行切成 token，数字/十六进制/UUID/IP 归一为 `<*>`；
3. 用前 depth 个 token 作前缀分桶，桶内与已有模板按"相同位置占比"算相似度；
   达到阈值就并入该模板（并把差异位置泛化成 `<*>`），否则新建模板。

**已知取舍**：模板提取的目的是聚类，不是逐字段区分。所以 `status=200` 与
`status=500` 会被归进同一个模板（数字被泛化）。严重性靠**日志级别**判断，
而不是靠模板里的状态码 —— 这一点是刻意的，不是缺陷。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

PLACEHOLDER = "<*>"

# 常见级别；顺序即严重度（越大越严重）
LEVELS: tuple[str, ...] = ("TRACE", "DEBUG", "INFO", "NOTICE", "WARN", "WARNING", "ERROR", "FATAL", "CRITICAL")
LEVEL_ALIASES = {"WARNING": "WARN", "CRITICAL": "FATAL", "ERR": "ERROR"}
LEVEL_SEVERITY = {name: index for index, name in enumerate(LEVELS)}
_ERROR_LEVELS = {"ERROR", "FATAL"}

# 多种常见时间戳形态。按"越长越具体"排序，先匹配到的胜出。
_TIMESTAMP_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^\[?(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})\.(\d{1,6})(Z|[+-]\d{2}:?\d{2})?\]?"), "frac"),
    (re.compile(r"^\[?(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(Z|[+-]\d{2}:?\d{2})?\]?"), "sec"),
    (re.compile(r"^\[?(\d{4}/\d{2}/\d{2})[ T](\d{2}:\d{2}:\d{2})\]?"), "slash"),
)

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_IP_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
_HEX_RE = re.compile(r"(?:0x)?[0-9a-fA-F]{8,}")
_NUM_RE = re.compile(r"\d+")
_LEVEL_RE = re.compile(r"\b(" + "|".join(LEVELS) + r")\b", re.IGNORECASE)


@dataclass
class ParsedLine:
    """一行的解析结果。`raw` 保留原文，方便出问题时回看。"""

    raw: str
    ts: datetime | None
    level: str
    message: str
    tokens: list[str] = field(default_factory=list)
    template: str = ""
    timestamp_matched: bool = False

    @property
    def is_error(self) -> bool:
        return self.level in _ERROR_LEVELS


def _find_level(text: str) -> tuple[str, int, int] | None:
    """定位级别词，返回 (规范级别, 起始, 结束)。找不到返回 None。"""
    match = _LEVEL_RE.search(text)
    if not match:
        return None
    name = match.group(1).upper()
    return LEVEL_ALIASES.get(name, name), match.start(), match.end()


def extract_level(text: str) -> str:
    """从行里取日志级别。取不到就 UNKNOWN，绝不猜。"""
    found = _find_level(text)
    return found[0] if found else "UNKNOWN"


def parse_timestamp(text: str, assume_tz=None) -> tuple[datetime | None, str]:
    """解析行首时间戳，返回 (时间, 去掉时间戳后的剩余文本)。

    `assume_tz` 用于无时区信息的日志（绝大多数本地日志都是这种），
    默认按**本地时区**解释 —— 因为写日志的机器就是本机。
    """
    for pattern, kind in _TIMESTAMP_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        groups = match.groups()
        try:
            if kind == "frac":
                date_s, time_s, frac, tz_s = groups
                stamp = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M:%S")
                stamp = stamp.replace(microsecond=int(frac.ljust(6, "0")[:6]))
            elif kind == "sec":
                date_s, time_s, tz_s = groups
                stamp = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M:%S")
            else:
                date_s, time_s = groups
                stamp = datetime.strptime(f"{date_s} {time_s}", "%Y/%m/%d %H:%M:%S")
        except (ValueError, TypeError):
            continue

        tz_s = groups[-1] if kind in ("frac", "sec") else None
        if tz_s:
            offset = tz_s.replace(":", "")
            if offset in ("Z", "z"):
                stamp = stamp.replace(tzinfo=timezone.utc)
            else:
                sign = 1 if offset[0] == "+" else -1
                hours, minutes = int(offset[1:3]), int(offset[3:5])
                from datetime import timedelta

                stamp = stamp.replace(tzinfo=timezone(sign * timedelta(hours=hours, minutes=minutes)))
        else:
            stamp = stamp.replace(tzinfo=assume_tz or datetime.now().astimezone().tzinfo)
        return stamp, text[match.end():].lstrip()
    return None, text


def normalize_token(token: str, placeholder: str = PLACEHOLDER) -> str:
    """把 token 里的"变量部分"归一化，让同类日志能聚到一起。"""
    if not token:
        return token
    if _UUID_RE.fullmatch(token) or _IP_RE.fullmatch(token):
        return placeholder
    if _HEX_RE.fullmatch(token) and len(token) >= 8:
        return placeholder
    if "=" in token:
        key, _, value = token.partition("=")
        return f"{key}={_mask_digits(value, placeholder)}"
    return _mask_digits(token, placeholder)


def _mask_digits(text: str, placeholder: str) -> str:
    if not text or not _NUM_RE.search(text):
        return text
    return _NUM_RE.sub(placeholder, text)


def tokenize(message: str, level: str, placeholder: str = PLACEHOLDER) -> list[str]:
    """切词并把级别放在最前面。

    把级别放进 token 序列是有意的：否则 `INFO ...` 与 `ERROR ...` 可能因为
    正文相似而被并成同一个模板，错误就被埋掉了。
    """
    tokens = [normalize_token(part, placeholder) for part in message.split()]
    return [level, *tokens]


def parse_line(raw: str, assume_tz=None, placeholder: str = PLACEHOLDER) -> ParsedLine:
    line = raw.rstrip("\r\n")
    stamp, rest = parse_timestamp(line.lstrip(), assume_tz)
    # 空行（或只剩时间戳的行）不该产生模板：否则会凭空多出一个 "UNKNOWN" 模板，
    # 污染模板统计。调用方虽然会过滤空行，但这一层自己也要站得住。
    if not rest.strip():
        return ParsedLine(
            raw=line, ts=stamp, level="UNKNOWN", message="",
            tokens=[], timestamp_matched=stamp is not None,
        )
    found = _find_level(rest)
    if found:
        level, start, end = found
        # 把级别词从正文里摘掉：模板首 token 已经是级别，
        # 不摘就会得到 "ERROR ERROR [db] ..." 这种重复，也会多占一个 token 位。
        body = (rest[:start] + rest[end:]).strip()
    else:
        level, body = "UNKNOWN", rest
    tokens = tokenize(body, level, placeholder)
    return ParsedLine(
        raw=line, ts=stamp, level=level, message=body,
        tokens=tokens, timestamp_matched=stamp is not None,
    )


@dataclass
class _Cluster:
    tokens: list[str]
    count: int = 0

    def similarity(self, tokens: list[str]) -> float:
        if len(tokens) != len(self.tokens):
            return 0.0
        same = sum(
            1 for mine, theirs in zip(self.tokens, tokens)
            if mine == theirs or mine == PLACEHOLDER
        )
        return same / len(self.tokens)

    def merge(self, tokens: list[str]) -> None:
        for index, theirs in enumerate(tokens):
            if self.tokens[index] != theirs:
                self.tokens[index] = PLACEHOLDER


class TemplateMiner:
    """Drain 模板提取器。

    用法：
        miner = TemplateMiner()
        parsed = parse_line(raw)
        template = miner.add(parsed)
    """

    def __init__(
        self,
        depth: int = 4,
        sim_threshold: float = 0.5,
        placeholder: str = PLACEHOLDER,
        max_templates: int = 20000,
    ) -> None:
        self.depth = max(1, depth)
        self.sim_threshold = sim_threshold
        self.placeholder = placeholder
        self.max_templates = max_templates
        # 键里带上 token 数：长度不同的行不该互相比较
        self._clusters: dict[tuple[tuple[str, ...], int], list[_Cluster]] = {}
        self.template_count = 0
        self.merged_count = 0
        self.overflow_count = 0

    def add(self, parsed: ParsedLine, placeholder: str | None = None) -> str:
        tokens = parsed.tokens
        if not tokens:
            parsed.template = ""
            return ""

        key = (tuple(tokens[: self.depth]), len(tokens))
        candidates = self._clusters.get(key)
        if candidates is None:
            candidates = []
            self._clusters[key] = candidates

        best: _Cluster | None = None
        best_score = 0.0
        for cluster in candidates:
            score = cluster.similarity(tokens)
            if score > best_score:
                best, best_score = cluster, score
        if best is not None and best_score >= self.sim_threshold:
            best.merge(tokens)
            best.count += 1
            self.merged_count += 1
            parsed.template = " ".join(best.tokens)
            return parsed.template

        if self.template_count >= self.max_templates:
            # 模板爆炸时不再新建，避免内存被打爆。计数留给上层报警。
            self.overflow_count += 1
            parsed.template = " ".join(tokens)
            return parsed.template

        cluster = _Cluster(tokens=list(tokens), count=1)
        candidates.append(cluster)
        self.template_count += 1
        parsed.template = " ".join(cluster.tokens)
        return parsed.template

    def stats(self) -> dict[str, int]:
        return {
            "templates": self.template_count,
            "merged": self.merged_count,
            "overflow": self.overflow_count,
        }
