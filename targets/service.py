"""被回归测试的靶子服务（只用标准库）。

存在理由：你无法在真实项目里故意弄坏线上接口来验证"回归测试真的抓得到"。
这里用 `--variant v2` 注入三类**真实世界最常见的接口回归**：

| # | 回归类型 | 具体表现 | 为什么常见 |
|---|---|---|---|
| 1 | 字段改名 | `/api/users/1` 的 `name` 变成 `full_name` | 后端重构 DTO、数据库列改名 |
| 2 | 类型变化 | `/api/orders[].amount` 由数字变成字符串 | 加了序列化/格式化（金额转字符串） |
| 3 | 状态码变化 | 查不存在的商品由 404 变成 200 + null | "友好化"错误处理，客户端判断全废 |

用法：
    python targets/service.py --variant v1 --port 8799     # 正常
    python targets/service.py --variant v2 --port 8799     # 注入回归
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

VARIANTS = ("v1", "v2")

USERS = {
    1: {"id": 1, "name": "alice", "email": "alice@example.com", "age": 30},
    2: {"id": 2, "name": "bob", "email": "bob@example.com", "age": 41},
}

ORDERS = [
    {"id": 1001, "amount": 19.9, "currency": "CNY"},
    {"id": 1002, "amount": 5, "currency": "CNY"},
]

PRODUCTS = {
    1: {"id": 1, "title": "widget", "price": 9.9, "stock": 12},
    2: {"id": 2, "title": "gadget", "price": 199.0, "stock": 0},
}

VARIANT = "v1"


class Handler(BaseHTTPRequestHandler):
    server_version = "regression-target/0.1"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ helpers

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> tuple[bool, object]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return False, None
        if length <= 0:
            return True, None
        raw = self.rfile.read(length)
        try:
            return True, json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False, None

    # ------------------------------------------------------------ routes

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        path = urlparse(self.path).path.rstrip("/") or "/"
        parts = [p for p in path.split("/") if p]  # ['api','users','1']

        if path == "/api/health":
            self._send(200, {"status": "ok", "version": VARIANT})
            return

        if len(parts) == 3 and parts[:2] == ["api", "users"]:
            self._user(parts[2])
            return

        if path == "/api/orders":
            self._orders()
            return

        if len(parts) == 3 and parts[:2] == ["api", "products"]:
            self._product(parts[2])
            return

        self._send(404, {"error": "no such route", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/api/echo":
            ok, payload = self._read_json()
            if not ok:
                self._send(400, {"error": "invalid json body"})
                return
            result = payload if isinstance(payload, dict) else {"value": payload}
            self._send(200, {**result, "echoed": True})
            return
        self._send(404, {"error": "no such route", "path": path})

    # ------------------------------------------------------------ handlers

    def _user(self, raw_id: str) -> None:
        try:
            user_id = int(raw_id)
        except ValueError:
            self._send(400, {"error": "invalid user id"})
            return
        user = USERS.get(user_id)
        if user is None:
            self._send(404, {"error": "user not found", "id": user_id})
            return
        # 回归 1：字段改名
        if VARIANT == "v2":
            user = {**{k: v for k, v in user.items() if k != "name"}, "full_name": user["name"]}
        self._send(200, user)

    def _orders(self) -> None:
        orders = ORDERS
        # 回归 2：金额由数字变成字符串
        if VARIANT == "v2":
            orders = [{**o, "amount": f"{o['amount']:.2f}"} for o in ORDERS]
        self._send(200, {"orders": orders})

    def _product(self, raw_id: str) -> None:
        try:
            product_id = int(raw_id)
        except ValueError:
            self._send(400, {"error": "invalid product id"})
            return
        product = PRODUCTS.get(product_id)
        if product is None:
            # 回归 3：不存在的商品由 404 变成 200 + null
            if VARIANT == "v2":
                self._send(200, {"product": None})
            else:
                self._send(404, {"error": "product not found", "id": product_id})
            return
        self._send(200, product)

    def log_message(self, fmt: str, *args) -> None:  # 静音
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="接口回归靶子服务")
    parser.add_argument("--variant", choices=VARIANTS, default="v1")
    parser.add_argument("--port", type=int, default=8799)
    args = parser.parse_args()

    global VARIANT
    VARIANT = args.variant

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"regression target 已启动： http://127.0.0.1:{args.port}  variant={VARIANT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
