"""本地价格源 —— 用来验证和演示 price_watch，不碰任何真实站点。

    python tools/price_stub.py --port 8790

## 为什么要有它

`price_watch` 的采集是**配置驱动**的（给个 JSON 地址 + 取值路径就行），
所以验证它不需要真去抓电商页面 —— 那既有反爬也有 ToS 问题，
而且一旦对方改版，测试就跟着坏。

这个桩服务提供三个能力，正好覆盖价格监控的三种真实情形：

| 情形 | 怎么触发 |
|---|---|
| 正常取到价格 | `GET /price/<item>` |
| 价格突变（降价/涨价） | `GET /set?item=rtx5070&price=3599` |
| 采集不稳（超时/500/坏数据） | `--fail-rate 0.3` |

## 接口

    GET /                     列出当前所有价格
    GET /price/<item>         单个商品（price_watch 用的就是它）
    GET /set?item=X&price=Y   改价（演示"突然降价"）
    GET /reset                恢复初始价
    GET /health               存活探针（配合 api_watch 用）

    # 演示"取到的值没法解析"这条路径
    GET /set?item=rtx5070&price=暂时没货
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 一些示例商品。价格刻意设成"离目标价还有一段距离"，
# 这样 /set 一改就能立刻演示"到价提醒"。
DEFAULT_PRICES: dict[str, dict[str, Any]] = {
    "rtx5070": {"label": "RTX 5070 显卡", "price": 4599, "currency": "CNY"},
    "rtx5080": {"label": "RTX 5080 显卡", "price": 8299, "currency": "CNY"},
    "rx9070": {"label": "RX 9070 显卡", "price": 4199, "currency": "CNY"},
}


class PriceStore:
    def __init__(self, prices: dict[str, dict[str, Any]], fail_rate: float = 0.0):
        self.initial = json.loads(json.dumps(prices))
        self.prices = json.loads(json.dumps(prices))
        self.fail_rate = fail_rate

    def reset(self) -> None:
        self.prices = json.loads(json.dumps(self.initial))

    def set_price(self, item: str, raw: str) -> bool:
        if item not in self.prices:
            return False
        try:
            self.prices[item]["price"] = float(raw)
        except ValueError:
            # 故意允许写入非数字：用来验证"取到的值没法解析"这条路径
            self.prices[item]["price"] = raw
        return True


def make_handler(store: PriceStore):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ops-price-stub"

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 的约定
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)

            if store.fail_rate and random.random() < store.fail_rate:
                self._json(500, {"error": "模拟的采集失败（--fail-rate）"})
                return

            if path == "/health":
                self._json(200, {"status": "ok", "items": len(store.prices)})
            elif path == "/":
                self._json(200, {"prices": store.prices})
            elif path.startswith("/price/"):
                item = path[len("/price/"):]
                entry = store.prices.get(item)
                if entry is None:
                    self._json(404, {"error": f"没有这个商品：{item}"})
                    return
                self._json(200, {
                    "item": item, "label": entry["label"], "currency": entry["currency"],
                    # 价格裹在一个嵌套结构里，顺便验证 price_path 的取值能力
                    "data": {"price": entry["price"], "stock": entry.get("stock", 1)},
                })
            elif path == "/set":
                item = (query.get("item") or [""])[0]
                price = (query.get("price") or [""])[0]
                if store.set_price(item, price):
                    self._json(200, {"ok": True, "item": item, "price": store.prices[item]["price"]})
                else:
                    self._json(404, {"error": f"没有这个商品：{item}"})
            elif path == "/reset":
                store.reset()
                self._json(200, {"ok": True, "prices": store.prices})
            else:
                self._json(404, {"error": "not found"})

        def _json(self, code: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # noqa: A003 - 静音
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    from ops.core import make_console_safe

    make_console_safe()
    parser = argparse.ArgumentParser(description="本地价格源（演示/验证用）")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--fail-rate", type=float, default=0.0,
                        help="0.0~1.0，返回 500 的概率（演示采集不稳）")
    args = parser.parse_args(argv)

    store = PriceStore(DEFAULT_PRICES, fail_rate=args.fail_rate)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(store))
    print(f"本地价格源已启动：http://127.0.0.1:{args.port}/")
    print(f"  商品：{sorted(store.prices)}")
    print(f"  改价：http://127.0.0.1:{args.port}/set?item=rtx5070&price=3599")
    print("  Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
