"""HTTP 探测：只用标准库 urllib。

职责边界：这一层**只负责"把请求发出去、把结果如实记下来"**，
不做"严不严重""要不要报警"的判断——那是 judge 的事。
"""

from __future__ import annotations

import hashlib
import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 ops-pipeline/0.1"
)


def utc_now() -> str:
    """统一 UTC ISO8601（秒精度，带 Z）。全链路只存 UTC。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class ProbeResult:
    target: str
    url: str
    ts: str
    ok: bool
    status_code: int | None = None
    latency_ms: float = 0.0
    error: str | None = None
    body_sha256: str | None = None
    body_len: int | None = None
    content_type: str | None = None
    attempts: int = 1

    def to_sample(self) -> dict[str, Any]:
        """转成 store.sample 表的一行。"""
        return {
            "target": self.target,
            "metric": "probe",
            "value_num": self.latency_ms,
            "value_text": self.body_sha256,
            "ok": 1 if self.ok else 0,
            "ts": self.ts,
            "status_code": self.status_code,
            "error": self.error,
            "body_len": self.body_len,
            "content_type": self.content_type,
        }


@dataclass
class ProbeConfig:
    timeout_ms: int = 8000
    retries: int = 2
    rate_limit_ms: int = 1200
    user_agent: str = DEFAULT_UA
    _last_request_at: float = field(default=0.0, repr=False)

    def wait_turn(self) -> None:
        """礼貌调用：与上一次请求保持最小间隔。"""
        if self.rate_limit_ms <= 0:
            return
        elapsed_ms = (time.monotonic() - self._last_request_at) * 1000
        remaining_ms = self.rate_limit_ms - elapsed_ms
        if self._last_request_at and remaining_ms > 0:
            time.sleep(remaining_ms / 1000)
        self._last_request_at = time.monotonic()


def _should_retry(status: int | None, error: str | None) -> bool:
    """5xx / 429 / 网络错误才重试；4xx（除 429）是确定性失败，重试没意义。"""
    if error is not None:
        return True
    if status is None:
        return True
    return status >= 500 or status == 429


def probe(target_cfg: dict[str, Any], pcfg: ProbeConfig) -> ProbeResult:
    """探测单个目标。任何异常都转成结果对象，绝不向上抛。"""
    name = str(target_cfg.get("name") or target_cfg.get("url"))
    url = str(target_cfg["url"])
    expect = int(target_cfg.get("expect", 200))
    timeout_s = pcfg.timeout_ms / 1000

    last: ProbeResult | None = None
    for attempt in range(1, pcfg.retries + 2):  # retries=2 → 最多 3 次
        pcfg.wait_turn()
        started = time.perf_counter()
        status: int | None = None
        error: str | None = None
        body: bytes = b""
        content_type: str | None = None

        req = urllib.request.Request(
            url,
            headers={"User-Agent": pcfg.user_agent, "Accept": "*/*"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                status = resp.status
                body = resp.read(65536)
                content_type = resp.headers.get("Content-Type")
        except urllib.error.HTTPError as exc:  # 4xx/5xx 也是"有响应"
            status = exc.code
            try:
                body = exc.read(65536)
            except Exception:  # noqa: BLE001 - 读不到 body 不影响状态码判定
                body = b""
            content_type = exc.headers.get("Content-Type") if exc.headers else None
            error = f"HTTP {exc.code}"
        except urllib.error.URLError as exc:
            error = f"URLError: {exc.reason}"
        except (socket.timeout, TimeoutError):
            error = f"timeout after {pcfg.timeout_ms}ms"
        except Exception as exc:  # noqa: BLE001 - 兜底：绝不让探测异常打断整轮
            error = f"{type(exc).__name__}: {exc}"

        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        ok = (error is None) and (status == expect)
        if not ok and error is None:
            error = f"unexpected status {status}, expected {expect}"

        last = ProbeResult(
            target=name,
            url=url,
            ts=utc_now(),
            ok=ok,
            status_code=status,
            latency_ms=latency_ms,
            error=None if ok else error,
            body_sha256=hashlib.sha256(body).hexdigest()[:16] if body else None,
            body_len=len(body) or None,
            content_type=content_type,
            attempts=attempt,
        )
        if ok or not _should_retry(status, error):
            return last
        time.sleep(min(2 ** (attempt - 1), 5))  # 指数退避，封顶 5s

    assert last is not None
    return last


def probe_all(
    targets: list[dict[str, Any]], pcfg: ProbeConfig
) -> list[ProbeResult]:
    """顺序探测全部目标（并发留到需要时再加，个人规模没必要）。"""
    return [probe(t, pcfg) for t in targets]


def post_json(
    url: str, payload: dict[str, Any], timeout_s: float = 10
) -> tuple[bool, str, Any]:
    """POST 一个 JSON，返回 (HTTP 层成功?, 说明, 解析后的响应体)。

    **注意：HTTP 200 不代表业务成功。** 企业微信群机器人即使 key 无效也返回 200，
    真正的结果在响应体的 `errcode` 里 —— 调用方必须自己检查，否则会出现
    "webhook 配错了却显示发送成功"这种最阴险的静默失败。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            text = resp.read(8192).decode("utf-8", "replace")
            body: Any = None
            try:
                body = json.loads(text)
            except json.JSONDecodeError:
                body = None
            return 200 <= resp.status < 300, f"{resp.status} {text[:200]}", body
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}", None
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}", None


def fetch_text(
    method: str,
    url: str,
    *,
    timeout_ms: int = 8000,
    max_bytes: int = 400_000,
    extra_headers: dict[str, str] | None = None,
    json_body: Any = None,
) -> "TextResponse":
    """取一段**有界**的文本（HTML 或任意文本），用于"从页面里抠一个值"。

    与 `request_json` 的区别：那个只留 2000 字节（够报错，不够解析页面），
    这个按 `max_bytes` 留一段可解析的正文。**仍然是有界读** ——
    上限由调用方给，不会把整个页面/无限流读进内存。

    编码按 UTF-8 → GBK 依次尝试：国内不少站点还是 GBK，
    解错了会让"元"变成乱码，正则也就跟着匹配不上。
    """
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "User-Agent": DEFAULT_UA,
    }
    headers.update(extra_headers or {})
    payload = None
    if json_body is not None:
        payload = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    started = time.perf_counter()
    status: int | None = None
    raw = b""
    error: str | None = None
    ctype = ""
    try:
        req = urllib.request.Request(url, data=payload, headers=headers, method=method.upper())
        with urllib.request.urlopen(req, timeout=timeout_ms / 1000) as resp:
            status = resp.status
            ctype = resp.headers.get("Content-Type", "") or ""
            raw = resp.read(max_bytes)
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 也是"有响应"，但页面内容基本没用 —— 仍然把状态码带回去
        status = exc.code
        ctype = exc.headers.get("Content-Type", "") if exc.headers else ""
        try:
            raw = exc.read(8192)
        except Exception:  # noqa: BLE001
            raw = b""
    except urllib.error.URLError as exc:
        error = f"URLError: {exc.reason}"
    except (socket.timeout, TimeoutError):
        error = f"timeout after {timeout_ms}ms"
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"

    lowered = ctype.lower()
    if "gbk" in lowered or "gb2312" in lowered or "gb18030" in lowered:
        encodings = ("gb18030", "utf-8")
    else:
        encodings = ("utf-8", "gb18030")
    text = ""
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not text and raw:
        text = raw.decode("utf-8", "replace")

    return TextResponse(
        status=status, latency_ms=round((time.perf_counter() - started) * 1000, 1),
        text=text, bytes_read=len(raw), truncated=len(raw) >= max_bytes,
        content_type=ctype, error=error,
    )


@dataclass
class TextResponse:
    """一次文本抓取的结果（不含任何解析结论）。"""

    status: int | None
    latency_ms: float
    text: str = ""
    bytes_read: int = 0
    truncated: bool = False
    content_type: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        """对"从页面抠值"来说，ok 必须是 **2xx**，不是"有响应"。

        这一点和 `JsonResponse.ok` 刻意不同：那个给契约断言层用
        （4xx/5xx 也算有响应、也要能看到状态码）。抠值拿一个 404 页面
        继续走只会得到一个莫名其妙的"没匹配到"。
        """
        return self.error is None and self.status is not None and 200 <= self.status < 300


def post_text(
    url: str, text: str, *, headers: dict[str, str] | None = None, timeout_s: float = 10
) -> tuple[bool, str, Any]:
    """POST 一段纯文本（ntfy 这类服务要的就是纯文本 body，不是 JSON）。"""
    base_headers = {"Content-Type": "text/plain; charset=utf-8", "User-Agent": DEFAULT_UA}
    base_headers.update(headers or {})
    req = urllib.request.Request(
        url, data=text.encode("utf-8"), headers=base_headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body_text = resp.read(8192).decode("utf-8", "replace")
            body: Any = None
            try:
                body = json.loads(body_text)
            except json.JSONDecodeError:
                body = None
            return 200 <= resp.status < 300, f"{resp.status} {body_text[:200]}", body
    except urllib.error.HTTPError as exc:
        # 错误详情通常在响应体里（ntfy 用 {"code":..,"error":..}）
        body: Any = None
        try:
            body = json.loads(exc.read(4096).decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            body = None
        return False, f"HTTP {exc.code}", body
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}", None


@dataclass
class JsonResponse:
    """一次接口调用的原始结果（不含任何断言结论）。"""

    status: int | None
    latency_ms: float
    body: Any = None
    text: str = ""
    json_error: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None


def request_json(
    method: str,
    url: str,
    *,
    json_body: Any = None,
    timeout_ms: int = 5000,
    user_agent: str = DEFAULT_UA,
    extra_headers: dict[str, str] | None = None,
) -> JsonResponse:
    """通用 JSON 请求。任何异常都转成结果对象，绝不向上抛。

    与 probe() 的区别：probe() 面向"服务活着吗"（只看状态码与延迟），
    request_json() 面向"接口契约对不对"（要拿到响应体交给断言层）。
    """
    payload = None
    headers = {"Accept": "application/json", "User-Agent": user_agent}
    if json_body is not None:
        payload = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    headers.update(extra_headers or {})

    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    started = time.perf_counter()
    status: int | None = None
    text = ""
    error: str | None = None
    try:
        with urllib.request.urlopen(req, timeout=timeout_ms / 1000) as resp:
            status = resp.status
            text = resp.read(1 << 20).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也是"有响应"，断言层要看到状态码
        status = exc.code
        try:
            text = exc.read(1 << 20).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            text = ""
    except urllib.error.URLError as exc:
        error = f"URLError: {exc.reason}"
    except (socket.timeout, TimeoutError):
        error = f"timeout after {timeout_ms}ms"
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"

    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    body: Any = None
    json_error: str | None = None
    if text:
        try:
            body = json.loads(text)
        except json.JSONDecodeError as exc:
            json_error = str(exc)

    return JsonResponse(
        status=status, latency_ms=latency_ms, body=body, text=text[:2000],
        json_error=json_error, error=error,
    )
