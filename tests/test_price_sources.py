"""人工录入价格 + 从页面正则抠价 的测试。

这两条能力是为了应对一个实测出来的现实：

> **有价格的站点全都抓不到。** NVIDIA 官方店从这台机器超时，
> B&H / Micro Center 返回 403，京东 / 什么值得买 / 慢慢买返回反爬页。
> 商品页本身又大多不带价格（营销页）或由 JS 渲染。

所以"抓不到"不是例外而是常态，系统必须能体面地处理它：

* `price_regex` —— 你能打开的页面，给条正则就能盯（仍是配置驱动，不是写死爬虫）；
* `manual` —— 你自己看一眼、敲一条命令，**其余的（历史/判据/告警/报告）全自动**。
  对"一个月看一次"的贵东西（比如几万块的设备），这才是最实用的形态。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from ops.core import http, prices, store  # noqa: E402
from ops.tasks import price_watch  # noqa: E402

TMP = ROOT / "data" / "_test_tmp" / "price_sources"

PAGE = """<!doctype html><html><head><title>DGX Spark</title></head><body>
<h1>NVIDIA DGX Spark 128GB</h1>
<div class="product-price">价格：¥28,999.00 元</div>
<script>window.__DATA__ = {"sku":"dgx-spark-128g","msrp":31999};</script>
</body></html>"""


class PageHandler(BaseHTTPRequestHandler):
    """一个假商品页：价格既在 HTML 文本里，也在 JS 里。"""

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/product"):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif self.path.startswith("/json"):
            body = json.dumps({"data": {"price": 28999, "currency": "CNY"}}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
        elif self.path.startswith("/gbk"):
            body = "价格：￥29,499 元".encode("gbk")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=GBK")
        else:
            body = b"<html><body>not found</body></html>"
            self.send_response(404)
            self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: A003
        return


def cfg_for_tmp() -> dict:
    return {"project": {"root": str(TMP)}}


class TestPageRegexCollection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), PageHandler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def test_regex_pulls_price_out_of_html(self):
        point = prices.collect_item(
            {"name": "dgx", "url": self.url("/product"),
             "price_regex": r"价格：¥([\d,]+(?:\.\d+)?)"},
            http.ProbeConfig(timeout_ms=5000))
        self.assertTrue(point.ok, point.error)
        self.assertEqual(point.value, 28999.0)

    def test_regex_without_capture_group_uses_whole_match(self):
        point = prices.collect_item(
            {"name": "dgx", "url": self.url("/product"), "price_regex": r"28,999"},
            http.ProbeConfig(timeout_ms=5000))
        self.assertTrue(point.ok, point.error)
        self.assertEqual(point.value, 28999.0)

    def test_regex_can_pull_from_embedded_js(self):
        """价格常常只在 JS 里 —— 正则照样能抠出来，这是它比 CSS 选择器值钱的地方。"""
        point = prices.collect_item(
            {"name": "dgx", "url": self.url("/product"),
             "price_regex": r'"msrp"\s*:\s*(\d+)'},
            http.ProbeConfig(timeout_ms=5000))
        self.assertTrue(point.ok, point.error)
        self.assertEqual(point.value, 31999.0)

    def test_gbk_page_is_decoded(self):
        """国内不少站点还是 GBK。编码解错，"元"就成了乱码、正则也就匹配不上。"""
        point = prices.collect_item(
            {"name": "dgx", "url": self.url("/gbk"),
             "price_regex": r"￥([\d,]+)"},
            http.ProbeConfig(timeout_ms=5000))
        self.assertTrue(point.ok, point.error)
        self.assertEqual(point.value, 29499.0)

    def test_no_match_is_reported_with_page_size(self):
        """页面改版/JS 渲染时会匹配不到 —— 要说清"读了多少字节"，而不是只说没匹配到。"""
        point = prices.collect_item(
            {"name": "dgx", "url": self.url("/product"), "price_regex": r"根本不存在的模式(\d+)"},
            http.ProbeConfig(timeout_ms=5000))
        self.assertFalse(point.ok)
        self.assertIn("没匹配到", point.error or "")
        self.assertIn("字节", point.error or "")

    def test_bad_regex_is_reported_not_crashed(self):
        point = prices.collect_item(
            {"name": "dgx", "url": self.url("/product"), "price_regex": "(未闭合"},
            http.ProbeConfig(timeout_ms=5000))
        self.assertFalse(point.ok)
        self.assertIn("不是合法正则", point.error or "")

    def test_404_is_not_treated_as_a_price_source(self):
        """`TextResponse.ok` 要求 2xx —— 与 `JsonResponse.ok` 刻意不同。"""
        point = prices.collect_item(
            {"name": "dgx", "url": self.url("/nope"), "price_regex": r"(\d+)"},
            http.ProbeConfig(timeout_ms=5000))
        self.assertFalse(point.ok)
        self.assertIn("404", point.error or "")

    def test_html_source_without_regex_is_reported(self):
        point = prices.collect_item(
            {"name": "dgx", "source": "html", "url": self.url("/product")},
            http.ProbeConfig(timeout_ms=5000))
        self.assertFalse(point.ok)
        self.assertIn("price_regex", point.error or "")

    def test_json_source_still_works(self):
        point = prices.collect_item(
            {"name": "dgx", "url": self.url("/json"), "price_path": "data.price"},
            http.ProbeConfig(timeout_ms=5000))
        self.assertTrue(point.ok, point.error)
        self.assertEqual(point.value, 28999.0)

    def test_source_is_guessed_from_config(self):
        """配了 price_regex 就自动走 html，不用同时写 source。"""
        point = prices.collect_item(
            {"name": "dgx", "url": self.url("/product"), "price_regex": r"([\d,]{5,})"},
            http.ProbeConfig(timeout_ms=5000))
        self.assertEqual(point.source, "html")


class TestManualPrices(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        (TMP / "data" / "state").mkdir(parents=True, exist_ok=True)
        self.cfg = cfg_for_tmp()

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)

    def test_manual_item_without_a_recorded_price_says_how_to_record(self):
        """**这条最重要**：没录过不能静默变成"没降价"，要直接告诉用户敲哪条命令。"""
        point = prices.collect_item({"name": "dgx", "source": "manual"}, http.ProbeConfig())
        self.assertFalse(point.ok)
        self.assertIn("price_set.py", point.error or "")

    def test_save_then_collect_round_trip(self):
        prices.save_manual_price(self.cfg, "dgx", 28999, note="京东自营")
        point = prices.collect_item({"name": "dgx", "source": "manual",
                                     "label": "DGX Spark 128GB"},
                                    http.ProbeConfig(), prices.load_manual_prices(self.cfg))
        self.assertTrue(point.ok, point.error)
        self.assertEqual(point.value, 28999.0)
        self.assertEqual(point.label, "DGX Spark 128GB")
        self.assertEqual(point.note, "京东自营")
        self.assertTrue(point.recorded_at, "必须记下'什么时候看到的' —— 那本身就是证据")

    def test_manual_accepts_messy_string_values(self):
        prices.save_manual_price(self.cfg, "dgx", "¥28,999.00")
        point = prices.collect_item({"name": "dgx", "source": "manual"},
                                    http.ProbeConfig(), prices.load_manual_prices(self.cfg))
        self.assertEqual(point.value, 28999.0)

    def test_manual_rejects_unparseable_values(self):
        prices.save_manual_price(self.cfg, "dgx", "等有货了再说")
        point = prices.collect_item({"name": "dgx", "source": "manual"},
                                    http.ProbeConfig(), prices.load_manual_prices(self.cfg))
        self.assertFalse(point.ok)
        self.assertIn("没法当价格解析", point.error or "")

    def test_recording_again_overwrites_and_keeps_others(self):
        prices.save_manual_price(self.cfg, "dgx", 31999)
        prices.save_manual_price(self.cfg, "other", 100)
        prices.save_manual_price(self.cfg, "dgx", 28999)
        data = prices.load_manual_prices(self.cfg)
        self.assertEqual(data["dgx"]["price"], 28999)
        self.assertEqual(data["other"]["price"], 100)

    def test_broken_state_file_does_not_crash(self):
        """状态文件坏了就当作"没录过"，绝不能因此炸掉整轮巡检。"""
        path = prices.manual_prices_path(self.cfg)
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        self.assertEqual(prices.load_manual_prices(self.cfg), {})

    def test_missing_state_file_is_empty(self):
        self.assertEqual(prices.load_manual_prices(self.cfg), {})

    def test_manual_price_flows_into_judgment(self):
        """人工价格必须能一路走到判据 —— 不然"手工记一条"就没有意义。"""
        prices.save_manual_price(self.cfg, "dgx", 28999)
        manual = prices.load_manual_prices(self.cfg)
        point = prices.collect_item({"name": "dgx", "source": "manual"}, http.ProbeConfig(), manual)
        result = prices.analyze_prices(
            [point],
            {"dgx": prices.PriceHistory(item="dgx", samples=5, last_value=31999.0,
                                        lowest=30999.0)},
            {"dgx": prices.PriceRules(target_price=30000)},
        )
        kinds = sorted(a.kind for a in result.anomalies)
        self.assertIn("price_below_target", kinds)
        self.assertIn("price_drop", kinds)


class TestTaskPassesManualPricesThrough(unittest.TestCase):
    def test_run_loads_and_passes_manual_prices(self):
        """防"接线漏了"：任务层必须真的加载并传下去。"""
        import inspect

        source = inspect.getsource(price_watch.run)
        self.assertIn("load_manual_prices", source)
        self.assertIn("manual", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
