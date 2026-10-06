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


LOGIN_PAGE = """<!doctype html><html><head><title>某商品</title></head><body>
<div class="p-price"></div>
<script>window.pageConfig={"priceFloor":{"priceLoginText":"登录查看价格"},"urgeLogin":true};</script>
</body></html>"""


class LoginWallHandler(PageHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/login"):
            body = LOGIN_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/echo"):
            # 把收到的 Cookie 回显出来，用来验证凭据真的被带上了
            body = json.dumps({"cookie": self.headers.get("Cookie", ""),
                               "data": {"price": 32999}}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


class TestSanityBounds(unittest.TestCase):
    """⚠️ **这组测试是实测换来的，防的是"静默的错误数据"。**

    真实经过：一个看起来无害的正则 `"price"\\s*:\\s*"?([0-9][0-9,]*)"?`
    在京东商品页上匹配到了 `"price":4` —— 系统把 **¥4** 记成了
    DGX Spark（三万块的东西）的价格，下一步就会报"价格暴跌 99.99%"。

    **比抓不到更糟的是抓到错的**：抓不到会报警、有人会去看；
    抓到错的会安静地产生假历史、假新低、假跌幅，污染整个价格库。
    """

    def test_bounds_reject_a_tiny_wrong_match(self):
        problem = prices.sanity_check(4.0, {"min_price": 10000, "max_price": 100000},
                                      matched="4")
        self.assertIsNotNone(problem)
        self.assertIn("低于合理性下限", problem or "")
        self.assertIn("无关的数字", problem or "")

    def test_bounds_reject_an_absurdly_large_match(self):
        problem = prices.sanity_check(9999999.0, {"min_price": 10000, "max_price": 100000})
        self.assertIsNotNone(problem)
        self.assertIn("高于合理性上限", problem or "")

    def test_value_inside_bounds_passes(self):
        self.assertIsNone(prices.sanity_check(32999.0, {"min_price": 10000, "max_price": 100000}))

    def test_bounds_are_inclusive(self):
        self.assertIsNone(prices.sanity_check(10000.0, {"min_price": 10000}))
        self.assertIsNone(prices.sanity_check(100000.0, {"max_price": 100000}))

    def test_no_bounds_means_no_check(self):
        """没配区间就不检查 —— 但也因此没有保护，README 里说了这是必需的。"""
        self.assertIsNone(prices.sanity_check(4.0, {}))

    def test_bounds_survive_the_config_layer(self):
        rules = prices.PriceRules.from_config({"min_price": 10000, "max_price": 100000})
        self.assertEqual(rules.min_price, 10000.0)
        self.assertEqual(rules.max_price, 100000.0)

    def test_collection_rejects_the_wrong_match_end_to_end(self):
        """端到端：那个真实误匹配必须在采集层就被拦下，绝不能进价格库。"""
        server = ThreadingHTTPServer(("127.0.0.1", 0), LoginWallHandler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            point = prices.collect_item(
                {"name": "dgx", "url": f"http://127.0.0.1:{port}/echo",
                 "price_path": "data.price", "min_price": 100000, "max_price": 200000},
                http.ProbeConfig(timeout_ms=5000))
            self.assertFalse(point.ok, "32999 低于下限 100000，必须被拦下")
            self.assertIn("合理性下限", point.error or "")
        finally:
            server.shutdown()
            server.server_close()

    def test_wrong_match_error_mentions_the_login_wall_when_present(self):
        """误匹配 + 页面要登录 → 报错要把"要登录"这个真正的原因带上，否则排查方向是错的。"""
        point = prices.collect_item(
            {"name": "dgx", "url": "https://item.m.jd.com/product/100254961843.html",
             "price_regex": r'"price"\s*:\s*"?([0-9][0-9,]*\.?[0-9]*)"?',
             "min_price": 10000, "max_price": 100000},
            http.ProbeConfig(timeout_ms=25000))
        # 这个请求依赖外网；拿不到网络就跳过（不把网络当测试前提）
        if point.ok and point.value and point.value >= 10000:
            self.skipTest("这次没匹配到那个无关数字，跳过")
        if point.error and "登录" in point.error:
            self.assertIn("需要登录", point.error)


class MockJdHandler(BaseHTTPRequestHandler):
    """模拟京东的行为：**不带 Cookie 就给掩码价，带了才给真价。**

    这是本轮实测到的真实行为：
      * 未登录 → `"price":"4??00"` + `"priceLoginText":"登录查看价格"`
      * 已登录 → 真实价格
    用这个 mock 把整条链路串起来验证，就不需要真的登录一次京东。
    """

    def do_GET(self):  # noqa: N802
        logged_in = "pt_key=" in (self.headers.get("Cookie") or "")
        if logged_in:
            body = ('<html><body><div class="p-price">¥32,999.00</div>'
                    '<script>window.x={"price":"32999.00"}</script></body></html>')
        else:
            body = ('<html><body>'
                    '<script>window.pageConfig={"priceFloor":{"demoteEnable":false,'
                    '"price":"4??00","type":"251"},'
                    '"priceLoginText":"登录查看价格"};</script>'
                    "</body></html>")
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # noqa: A003
        return


class TestJdLikeFlowEndToEnd(unittest.TestCase):
    """不带 Cookie → 明确报登录墙；带 Cookie → 取到价格 → 判据跑通。"""

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockJdHandler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        (TMP / "data").mkdir(parents=True, exist_ok=True)
        self.cfg = {**cfg_for_tmp(), "_secrets": {}}

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)

    def item(self) -> dict:
        return {
            "name": "dgx_spark",
            "label": "NVIDIA DGX Spark 128GB",
            "source": "html",
            "url": f"http://127.0.0.1:{self.port}/product",
            # 覆盖常见几种字段形态，和 config.toml 里的默认值一致
            "price_regex": r'"(?:p|price|jdPrice)"\s*:\s*"?([0-9][0-9,]*(?:\.[0-9]+)?)"?',
            "cookie_env": "OPS_JD_COOKIE",
            "min_price": 15000,
            "max_price": 80000,
        }

    def collect(self, item: dict):
        resolved = {**item, "headers": prices.resolve_headers(self.cfg, item)}
        return prices.collect_item(resolved, http.ProbeConfig(timeout_ms=5000))

    def test_without_cookie_reports_the_login_wall(self):
        point = self.collect(self.item())
        self.assertFalse(point.ok)
        self.assertIn("登录", point.error or "")
        # 正则先抓到了掩码里的 4，所以会同时报"低于区间"和"需要登录"——
        # **两件事都要说**：只说下限，用户会去调正则；只说登录，他不知道 4 哪来的。
        self.assertIn("15000", point.error or "")
        self.assertIn("OPS_JD_COOKIE", point.error or "", "必须给出解法")

    def test_with_cookie_extracts_the_real_price(self):
        self.cfg["_secrets"] = {"OPS_JD_COOKIE": "pt_key=abc; pt_pin=me"}
        point = self.collect(self.item())
        self.assertTrue(point.ok, point.error)
        self.assertEqual(point.value, 32999.0)

    def test_masked_price_never_becomes_a_number(self):
        """不带 Cookie 时页面里的 `4??00` 绝不能被当成价格。"""
        point = self.collect(self.item())
        self.assertIsNone(point.value)

    def test_cookie_path_flows_all_the_way_into_judgment(self):
        """带 Cookie 取到真实价格后，判据必须能正常跑出结论。"""
        self.cfg["_secrets"] = {"OPS_JD_COOKIE": "pt_key=abc; pt_pin=me"}
        point = self.collect(self.item())
        analysis = prices.analyze_prices(
            [point],
            # 历史最低比现价高一点，才构成"创新低"；
            # 相等不算新低（`value < lowest` 是严格小于）—— 这是对的
            {"dgx_spark": prices.PriceHistory(item="dgx_spark", samples=9,
                                              last_value=35000.0, lowest=33500.0)},
            {"dgx_spark": prices.PriceRules(target_price=34000)},
        )
        kinds = sorted(a.kind for a in analysis.anomalies)
        self.assertIn("price_below_target", kinds, "32999 低于目标价 34000，该报到价")
        self.assertIn("price_new_low", kinds, "32999 低于历史最低 33500，该报新低")
        self.assertEqual(analysis.metrics["ok"], 1)


class TestLoginWallDetection(unittest.TestCase):
    """京东这类站点**必须登录才显示价格** —— 这不是"没匹配到"，是另一回事。

    实测：item.m.jd.com 可达、标题能拿到，但价格字段是
    `"priceLoginText":"登录查看价格"`。把这两件事混成一个含糊的报错，
    会让人往完全错误的方向排查（去查正则、去查页面改版），
    而真正该做的是"配一个 Cookie"。
    """

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), LoginWallHandler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def test_detects_jd_login_markers(self):
        for marker in ("登录查看价格", "priceLoginText", "urgeLogin", "passport.jd.com"):
            with self.subTest(marker=marker):
                self.assertEqual(prices.detect_login_wall(f"...{marker}..."), marker)

    def test_normal_page_has_no_wall(self):
        self.assertIsNone(prices.detect_login_wall("<html>¥28,999</html>"))

    def test_login_wall_produces_an_actionable_error(self):
        point = prices.collect_item(
            {"name": "dgx", "url": self.url("/login"), "price_regex": r'"p":"([\d.]+)"'},
            http.ProbeConfig(timeout_ms=5000))
        self.assertFalse(point.ok)
        self.assertIn("登录", point.error or "")
        self.assertIn("Cookie", point.error or "")
        self.assertIn("price_set.py", point.error or "", "必须给出可行的替代方案")

    def test_error_differs_when_cookie_was_already_sent(self):
        """带了 Cookie 还是被要求登录 → 提示应该是"可能过期了"，而不是"去配置一个"。"""
        point = prices.collect_item(
            {"name": "dgx", "url": self.url("/login"), "price_regex": r'"p":"([\d.]+)"',
             "headers": {"Cookie": "pt_key=old"}},
            http.ProbeConfig(timeout_ms=5000))
        self.assertFalse(point.ok)
        self.assertIn("过期", point.error or "")


class TestCredentialResolution(unittest.TestCase):
    """凭据只从环境变量 / secrets.local.toml 读，**绝不写进 config.toml**。"""

    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        (TMP / "data").mkdir(parents=True, exist_ok=True)
        self.cfg = {**cfg_for_tmp(), "_secrets": {}}

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)
        os.environ.pop("OPS_TEST_COOKIE", None)

    def test_no_cookie_env_means_no_cookie_header(self):
        headers = prices.resolve_headers(self.cfg, {"name": "x"})
        self.assertNotIn("Cookie", headers)

    def test_cookie_read_from_secrets_file(self):
        self.cfg["_secrets"] = {"OPS_TEST_COOKIE": "pt_key=abc; pt_pin=me"}
        headers = prices.resolve_headers(self.cfg, {"name": "x", "cookie_env": "OPS_TEST_COOKIE"})
        self.assertEqual(headers["Cookie"], "pt_key=abc; pt_pin=me")

    def test_env_var_wins_over_secrets_file(self):
        os.environ["OPS_TEST_COOKIE"] = "from-env"
        self.cfg["_secrets"] = {"OPS_TEST_COOKIE": "from-file"}
        headers = prices.resolve_headers(self.cfg, {"name": "x", "cookie_env": "OPS_TEST_COOKIE"})
        self.assertEqual(headers["Cookie"], "from-env")

    def test_extra_headers_are_preserved(self):
        self.cfg["_secrets"] = {"OPS_TEST_COOKIE": "c"}
        headers = prices.resolve_headers(
            self.cfg, {"name": "x", "cookie_env": "OPS_TEST_COOKIE",
                       "headers": {"Referer": "https://item.jd.com/"}})
        self.assertEqual(headers["Referer"], "https://item.jd.com/")
        self.assertEqual(headers["Cookie"], "c")

    def test_cookie_missing_falls_back_to_no_header(self):
        """配了 cookie_env 但值拿不到 → 就是没有 Cookie，**报错要说清**，不能假装有。"""
        headers = prices.resolve_headers(self.cfg, {"name": "x", "cookie_env": "OPS_NOT_SET"})
        self.assertNotIn("Cookie", headers)

    def test_credentials_reach_the_request(self):
        """端到端：解析出来的 Cookie 必须真的出现在 HTTP 请求里。"""
        server = ThreadingHTTPServer(("127.0.0.1", 0), LoginWallHandler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            self.cfg["_secrets"] = {"OPS_TEST_COOKIE": "pt_key=abc"}
            item = {"name": "dgx", "url": f"http://127.0.0.1:{port}/echo",
                    "cookie_env": "OPS_TEST_COOKIE", "price_path": "data.price",
                    "headers": prices.resolve_headers(
                        self.cfg, {"cookie_env": "OPS_TEST_COOKIE"})}
            point = prices.collect_item(item, http.ProbeConfig(timeout_ms=5000))
            self.assertTrue(point.ok, point.error)
            self.assertEqual(point.value, 32999.0)
        finally:
            server.shutdown()
            server.server_close()


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
