"""故障靶子（fault drill）—— 只用标准库。

存在的理由：**你必须先证明"监控能抓到已知故障"，才敢相信它在真实故障面前不撒谎。**
这个服务可以人为制造两类故障，用来验证告警链路端到端可用：

    python tools/fault_drill.py --port 8788 --fail-rate 0.0
    curl http://127.0.0.1:8788/fault/on        # 开始返回 500
    curl http://127.0.0.1:8788/fault/off       # 恢复
    curl "http://127.0.0.1:8788/fault/latency?ms=6000"   # 制造延迟回归
    curl http://127.0.0.1:8788/fault/status

注意：它是**本地自建**服务，不涉及任何第三方，可以随便打。
"""

from __future__ import annotations

import argparse
import json
import random
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

STATE = {"fail_rate": 0.0, "latency_ms": 0, "hits": 0, "failures": 0}


class Handler(BaseHTTPRequestHandler):
    server_version = "fault-drill/0.1"

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path == "/fault/on":
            STATE["fail_rate"] = float(query.get("rate", ["1.0"])[0])
            self._json(200, {"ok": True, "fail_rate": STATE["fail_rate"]})
            return
        if path == "/fault/off":
            STATE["fail_rate"] = 0.0
            self._json(200, {"ok": True, "fail_rate": 0.0})
            return
        if path == "/fault/latency":
            STATE["latency_ms"] = int(query.get("ms", ["0"])[0])
            self._json(200, {"ok": True, "latency_ms": STATE["latency_ms"]})
            return
        if path == "/fault/status" or path == "/":
            self._json(200, {"ok": True, **STATE})
            return
        if path == "/health":
            STATE["hits"] += 1
            if STATE["latency_ms"]:
                time.sleep(STATE["latency_ms"] / 1000)
            if random.random() < STATE["fail_rate"]:
                STATE["failures"] += 1
                self._json(500, {"ok": False, "error": "drill: injected failure"})
                return
            self._json(200, {"ok": True, "hits": STATE["hits"]})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def log_message(self, fmt: str, *args) -> None:  # 静音，避免刷屏
        return


def main() -> int:
    # 中文 Windows 控制台是 GBK；不先安全化，打印中文/符号时会抛 UnicodeEncodeError，
    # 把要显示的结果变成看起来像崩溃。
    from ops.core import make_console_safe

    make_console_safe()

    parser = argparse.ArgumentParser(description="本地故障靶子")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--fail-rate", type=float, default=0.0, help="0.0~1.0，/health 返回 500 的概率")
    parser.add_argument("--latency-ms", type=int, default=0, help="/health 固定延迟")
    args = parser.parse_args()

    STATE["fail_rate"] = args.fail_rate
    STATE["latency_ms"] = args.latency_ms
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"fault drill 已启动： http://127.0.0.1:{args.port}/health  (fail_rate={args.fail_rate})")
    print("  /fault/on  /fault/off  /fault/latency?ms=6000  /fault/status")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
