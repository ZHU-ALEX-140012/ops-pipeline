"""价格监控的测试。

这里的重点和 `api_watch` 不同：**价格没有"正常值"**，所以判据必须只用
三个有明确含义的东西（目标价、相对上一次的变化、窗口极值）。
测试要锁住这一点 —— 尤其是"不要拿中位数当基准"。

第二组测试锁住**静默失败防护**：取不到价格是"我们瞎了"，
绝不能被当成"没降价"。
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import price_stub  # noqa: E402
from ops.core import http, prices, store  # noqa: E402
from ops.tasks import price_watch  # noqa: E402

TMP = ROOT / "data" / "_test_tmp" / "price"


def point(item="gpu", value=4000.0, *, ok=True, error=None, label="显卡", currency="CNY"):
    return prices.PricePoint(item=item, label=label, value=value, currency=currency,
                             ok=ok, error=error, ts=http.utc_now())


def history(item="gpu", **kw):
    base = {"item": item}
    base.update(kw)
    return prices.PriceHistory(**base)


def analyze(points, hist=None, rule=None):
    return prices.analyze_prices(
        points, hist or {}, {"gpu": rule or prices.PriceRules()})


def kinds(analysis):
    return sorted(a.kind for a in analysis.anomalies)


# ---------------------------------------------------------------- 纯函数：解析

class TestParsePrice(unittest.TestCase):
    def test_parses_the_shapes_you_actually_meet(self):
        "真实会遇到的价格写法都要能解析出来。"
        for raw in (3999, 3999.0, "3999", "¥3,999.00", "￥3,999", "3999元",
                    "  3999 ", "¥ 3,999.5"):
            with self.subTest(raw=raw):
                self.assertEqual(prices.parse_price(raw), 3999 if "5" not in str(raw) else 3999.5)

    def test_unparseable_returns_none_not_zero(self):
        """**这条最重要**：返回 0 会被下游当成"价格暴跌到 0"，那是最坏的错误。"""
        for raw in (None, "", "暂时没货", "N/A", "--", True, False, 0, -5, "0"):
            with self.subTest(raw=raw):
                self.assertIsNone(prices.parse_price(raw))

    def test_masked_placeholder_price_is_rejected(self):
        """⚠️ **这条是实测换来的，防的是"凭噪声捏造一个价格"。**

        京东对未登录请求返回 `"price":"4??00"` —— 真的问号字符，掩码占位价。
        而剥掉非数字字符会把 `4??00` 变成 **400**：一个完全由噪声捏造出来的数字。
        比"解析失败"危险得多，所以见到掩码字符一律判失败。
        """
        for raw in ("4??00", "3??99", "??", "12*34", "1？234"):
            with self.subTest(raw=raw):
                self.assertIsNone(prices.parse_price(raw), f"{raw!r} 是掩码，不是价格")

    def test_extract_path_nested_and_indexed(self):
        payload = {"data": {"price": 3999, "offers": [{"p": 1}, {"p": 2}]}}
        self.assertEqual(prices.extract_path(payload, "data.price"), 3999)
        self.assertEqual(prices.extract_path(payload, "data.offers.1.p"), 2)
        self.assertEqual(prices.extract_path(payload, None), payload)

    def test_extract_path_raises_on_broken_path(self):
        """路径断了要显式报错，不能返回 None 混过去 —— 那会变成"没有价格"。"""
        with self.assertRaises(KeyError):
            prices.extract_path({"a": 1}, "a.b")
        with self.assertRaises(KeyError):
            prices.extract_path({"a": [1]}, "a.9")

    def test_format_price(self):
        self.assertEqual(prices.format_price(3999, "CNY"), "¥3,999")
        self.assertEqual(prices.format_price(3899.5, "CNY"), "¥3,899.5")
        self.assertEqual(prices.format_price(None, "CNY"), "—")
        self.assertEqual(prices.format_price(10, "XYZ"), "10 XYZ")


# ---------------------------------------------------------------- 判据

class TestPriceJudgment(unittest.TestCase):
    def test_quiet_when_nothing_happens(self):
        """价格在区间里正常波动，不该报警。"""
        result = analyze([point(value=4599.0)],
                         {"gpu": history(last_value=4600.0, lowest=4400.0, samples=20)},
                         prices.PriceRules(target_price=4000))
        self.assertEqual(kinds(result), [])

    def test_target_price_hit_is_reported(self):
        result = analyze([point(value=3899.0)],
                         {"gpu": history(last_value=4200.0, samples=20)},
                         prices.PriceRules(target_price=4000))
        self.assertIn("price_below_target", kinds(result))
        detail = next(a.detail for a in result.anomalies if a.kind == "price_below_target")
        self.assertIn("¥3,899", detail)
        self.assertIn("目标价", detail)

    def test_target_is_inclusive(self):
        """正好等于目标价也算到 —— 差一分钱不提醒是说不通的。"""
        result = analyze([point(value=4000.0)], {}, prices.PriceRules(target_price=4000))
        self.assertIn("price_below_target", kinds(result))

    def test_already_below_target_does_not_repeat(self):
        """⚠️ **这条决定了这个价格监控能不能用。**

        价格一旦低于目标价，如果每轮都报，用户一天会收到两次一模一样的消息，
        然后就会把通知关掉 —— 那是"告警系统死于误报"的标准死法。
        所以要判**跨越**（上次在上面、这次不在），不是判**状态**（现在在不在下面）。
        """
        result = analyze([point(value=27500.0)],
                         {"gpu": history(last_value=27999.0, lowest=27500.0, samples=5)},
                         prices.PriceRules(target_price=28000, drop_percent=None))
        self.assertNotIn("price_below_target", kinds(result))
        self.assertTrue(any("持续状态" in note for note in result.notes),
                        "不提醒也要说明'它还在下面'，否则看报告的人以为没检测到")

    def test_crossing_below_target_alerts(self):
        """上次在目标价之上、这次跌破 —— 这才是该提醒的那一刻。"""
        result = analyze([point(value=27500.0)],
                         {"gpu": history(last_value=30000.0, lowest=27500.0, samples=5)},
                         prices.PriceRules(target_price=28000, drop_percent=None))
        self.assertIn("price_below_target", kinds(result))

    def test_first_observation_below_target_alerts(self):
        """没有历史时的第一次观测也算跨越 —— 你确实需要知道'它已经在目标价下面了'。"""
        result = analyze([point(value=27500.0)], {},
                         prices.PriceRules(target_price=28000, drop_percent=None))
        self.assertIn("price_below_target", kinds(result))

    def test_recrossing_alerts_again(self):
        """先涨回目标价之上、再跌破 —— 应该再提醒一次（这是新的一次机会）。"""
        result = analyze([point(value=27500.0)],
                         {"gpu": history(last_value=28500.0, lowest=27000.0, samples=9)},
                         prices.PriceRules(target_price=28000, drop_percent=None))
        self.assertIn("price_below_target", kinds(result))

    def test_big_drop_is_reported_relative_to_last_not_median(self):
        """单次降幅比的是**上一次**。

        这条测试锁住一个容易写错的地方：如果拿中位数当基准，
        一个"长期在 4000 上下、这次突然 3500"的商品反而会被算成"正常"。
        """
        result = analyze([point(value=3500.0)],
                         {"gpu": history(last_value=4300.0, median=4000.0, samples=30)},
                         prices.PriceRules(target_price=None))
        self.assertIn("price_drop", kinds(result))
        detail = next(a.detail for a in result.anomalies if a.kind == "price_drop")
        self.assertIn("¥4,300", detail)
        self.assertIn("¥3,500", detail)

    def test_small_change_is_not_reported(self):
        result = analyze([point(value=4250.0)],
                         {"gpu": history(last_value=4300.0, samples=30)},
                         prices.PriceRules(drop_percent=8))
        self.assertEqual(kinds(result), [])

    def test_new_low_is_reported(self):
        result = analyze([point(value=3700.0)],
                         {"gpu": history(last_value=4300.0, lowest=3800.0, samples=25)},
                         prices.PriceRules(target_price=None, drop_percent=None))
        self.assertIn("price_new_low", kinds(result))

    def test_new_low_needs_enough_history(self):
        """冷启动时不能判"新低" —— 没有历史不等于历史最低。

        这是本项目一贯的冷启动保护：样本不够时，把判断让给"我不知道"。
        """
        result = analyze([point(value=3700.0)],
                         {"gpu": history(last_value=4300.0, lowest=3800.0, samples=2)},
                         prices.PriceRules(target_price=None, drop_percent=None))
        self.assertNotIn("price_new_low", kinds(result))
        self.assertTrue(any("冷启动" in note for note in result.notes))

    def test_rise_is_reported(self):
        result = analyze([point(value=5200.0)],
                         {"gpu": history(last_value=4300.0, samples=20)},
                         prices.PriceRules(target_price=None, rise_percent=15))
        self.assertIn("price_rise", kinds(result))

    def test_unstable_price_note_is_not_an_anomaly(self):
        """"价格很久没变"是稳定，不是故障 —— 它只该是一条说明。"""
        result = analyze([point(value=4300.0)],
                         {"gpu": history(last_value=4300.0, samples=20,
                                         minutes_since_change=72 * 60,
                                         minutes_since_seen=30)},
                         prices.PriceRules(target_price=4000))
        self.assertEqual(kinds(result), [])
        self.assertTrue(any("未变" in note for note in result.notes))


class TestSilentFailureProtection(unittest.TestCase):
    """取不到价格是"我们瞎了"，不是"没降价"。"""

    def test_missing_price_is_an_anomaly(self):
        result = analyze([point(ok=False, value=None, error="URLError: timed out")])
        self.assertIn("price_missing", kinds(result))
        detail = next(a.detail for a in result.anomalies if a.kind == "price_missing")
        self.assertIn("取不到 ≠ 没降价", detail)

    def test_zero_items_is_a_failure_not_ok(self):
        """0 条 = 失败。这条规则在别处已经救过一次，这里同样适用。"""
        result = analyze([])
        self.assertIn("empty_round", kinds(result))

    def test_all_items_failed_is_reported_separately(self):
        result = analyze([point("a", ok=False, error="boom"), point("b", ok=False, error="boom")])
        self.assertIn("price_no_data", kinds(result))
        detail = next(a.detail for a in result.anomalies if a.kind == "price_no_data")
        self.assertIn("一个价格都没抓到", detail)

    def test_stale_collection_is_reported_using_seen_not_changed(self):
        """数据源静默用 `minutes_since_seen`（多久没抓到），不是 `since_change`。"""
        result = analyze([point(value=4300.0)],
                         {"gpu": history(samples=10, minutes_since_seen=9 * 60,
                                         minutes_since_change=9 * 60, last_value=4300.0)},
                         prices.PriceRules(target_price=4000, stale_hours=6))
        self.assertIn("price_source_stale", kinds(result))

    def test_stable_price_is_not_stale_collection(self):
        """反过来：价格三小时没变但一直在正常采集 —— 不许当成数据源故障。"""
        result = analyze([point(value=4300.0)],
                         {"gpu": history(samples=10, minutes_since_seen=5,
                                         minutes_since_change=180, last_value=4300.0)},
                         prices.PriceRules(target_price=4000, stale_hours=6))
        self.assertEqual(kinds(result), [])


class TestMetrics(unittest.TestCase):
    def test_metrics_shape_matches_other_tasks(self):
        """字段名要和其它任务一致 —— 报告渲染与 finalize 直接复用它们。"""
        result = analyze([point("a", 100.0), point("b", ok=False, error="x")])
        for key in ("items", "targets", "ok", "failed", "anomalies"):
            self.assertIn(key, result.metrics)
        self.assertEqual(result.metrics["ok"], 1)
        self.assertEqual(result.metrics["failed"], 1)

    def test_baseline_is_recorded_for_every_item(self):
        result = analyze([point("a", 100.0)],
                         {"a": history("a", samples=7, median=110.0, lowest=95.0)})
        self.assertEqual(result.baseline["a"]["samples"], 7)
        self.assertEqual(result.baseline["a"]["lowest"], 95.0)


# ---------------------------------------------------------------- 采集（真起桩服务）

class TestCollection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = price_stub.PriceStore(price_stub.DEFAULT_PRICES)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                        price_stub.make_handler(cls.store))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def item(self, **kw):
        cfg = {"name": "rtx5070", "label": "RTX 5070",
               "url": f"http://127.0.0.1:{self.port}/price/rtx5070",
               "price_path": "data.price", "currency": "CNY"}
        cfg.update(kw)
        return cfg

    def test_collects_price_through_nested_path(self):
        point = prices.collect_item(self.item(), http.ProbeConfig(timeout_ms=5000))
        self.assertTrue(point.ok, point.error)
        self.assertEqual(point.value, 4599.0)
        self.assertEqual(point.currency, "CNY")

    def test_missing_item_gives_404_not_crash(self):
        point = prices.collect_item(self.item(url=f"http://127.0.0.1:{self.port}/price/nope"),
                                    http.ProbeConfig(timeout_ms=5000))
        self.assertFalse(point.ok)
        self.assertIn("404", point.error or "")

    def test_broken_path_is_reported(self):
        point = prices.collect_item(self.item(price_path="data.nothing.here"),
                                    http.ProbeConfig(timeout_ms=5000))
        self.assertFalse(point.ok)
        self.assertIn("路径", point.error or "")

    def test_unparseable_value_is_reported(self):
        self.store.set_price("rtx5070", "暂时没货")
        try:
            point = prices.collect_item(self.item(), http.ProbeConfig(timeout_ms=5000))
            self.assertFalse(point.ok)
            self.assertIn("没法当价格解析", point.error or "")
        finally:
            self.store.reset()

    def test_network_failure_is_reported(self):
        """采集失败必须变成一条**数据**，而不是抛异常炸掉整轮。"""
        point = prices.collect_item(
            self.item(url="http://127.0.0.1:1/price/rtx5070"),
            http.ProbeConfig(timeout_ms=1500))
        self.assertFalse(point.ok)
        self.assertTrue(point.error)

    def test_missing_url_is_reported(self):
        point = prices.collect_item({"name": "x"}, http.ProbeConfig())
        self.assertFalse(point.ok)
        self.assertIn("url", point.error or "")


# ---------------------------------------------------------------- 落库与历史

class TestHistoryRoundTrip(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        (TMP / "data").mkdir(parents=True, exist_ok=True)
        self.conn = store.connect(TMP / "data" / "ops.db")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(TMP, ignore_errors=True)

    def _record(self, values, *, run_prefix="r"):
        for index, (value, offset_minutes) in enumerate(values):
            from datetime import datetime, timedelta, timezone

            ts = (datetime.now(timezone.utc) - timedelta(minutes=offset_minutes))
            store.start_run(self.conn, f"{run_prefix}{index}", "price_watch",
                            ts.replace(microsecond=0).isoformat().replace("+00:00", "Z"))
            store.insert_samples(self.conn, f"{run_prefix}{index}", [{
                "target": "gpu", "metric": "price", "value_num": value,
                "value_text": "显卡", "ok": 1,
                "ts": ts.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            }])

    def test_history_reports_lowest_and_change_times(self):
        self._record([(4300, 120), (4300, 90), (3900, 60), (3900, 5)])
        raw = store.price_history(self.conn, "gpu", days=30)
        self.assertEqual(raw["samples"], 4)
        self.assertEqual(raw["median"], 4100.0)
        self.assertEqual(raw["lowest"], 3900.0)
        self.assertEqual(raw["last_value"], 3900.0)
        self.assertEqual(raw["changes"], 1)
        # 最近一次变化是 60 分钟前，而最近一次采集是 5 分钟前 —— 两者必须分开
        self.assertLess(raw["minutes_since_seen"], 15)
        self.assertGreater(raw["minutes_since_change"], 45)

    def test_failed_collection_shows_up_as_failure_not_as_price(self):
        store.start_run(self.conn, "bad", "price_watch", http.utc_now())
        store.insert_samples(self.conn, "bad", [{
            "target": "gpu", "metric": "price", "value_num": None,
            "value_text": "显卡", "ok": 0, "ts": http.utc_now(), "error": "boom",
        }])
        raw = store.price_history(self.conn, "gpu", days=30)
        self.assertEqual(raw["samples"], 0)
        self.assertEqual(raw["failures"], 1)
        self.assertIsNone(raw["last_value"])

    def test_empty_history_is_safe(self):
        raw = store.price_history(self.conn, "never-seen", days=30)
        self.assertEqual(raw["samples"], 0)
        self.assertIsNone(raw["last_value"])
        self.assertIsNone(raw["minutes_since_seen"])


class TestBuildHistory(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        (TMP / "data").mkdir(parents=True, exist_ok=True)
        self.conn = store.connect(TMP / "data" / "ops.db")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(TMP, ignore_errors=True)

    def test_build_history_shapes_into_dataclass(self):
        store.start_run(self.conn, "r1", "price_watch", http.utc_now())
        store.insert_samples(self.conn, "r1", [{
            "target": "gpu", "metric": "price", "value_num": 3999.0,
            "value_text": "显卡", "ok": 1, "ts": http.utc_now(),
        }])
        out = price_watch.build_history(self.conn, ["gpu"], days=30)
        self.assertEqual(out["gpu"].samples, 1)
        self.assertEqual(out["gpu"].last_value, 3999.0)

    def test_unknown_item_gets_empty_history(self):
        out = price_watch.build_history(self.conn, ["nope"], days=30)
        self.assertEqual(out["nope"].samples, 0)


class TestCurrentRoundIsExcludedFromBaseline(unittest.TestCase):
    """⚠️ **这条测试是真实 bug 换来的。**

    本轮的样本在统计之前就已经落库（和 `api_watch` 一样）。但价格判据里
    "单次降幅"要比的是**上一次**，所以历史里**必须排除本轮** ——
    否则 `last_value` 就是本轮自己，`(现价 - 上次价)` 恒等于 0，
    `price_drop` **永远不会触发**，而且不报错、不变慢，只是安静地什么都不做。

    它当初"看起来能用"，是因为两次运行落在同一秒、`ts` 精度不够，
    SQLite 恰好返回了另一行 —— **是运气，不是设计**。
    """

    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        (TMP / "data").mkdir(parents=True, exist_ok=True)
        self.conn = store.connect(TMP / "data" / "ops.db")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(TMP, ignore_errors=True)

    def _record(self, run_id: str, value: float, ts: str) -> None:
        store.start_run(self.conn, run_id, "price_watch", ts)
        store.insert_samples(self.conn, run_id, [{
            "target": "gpu", "metric": "price", "value_num": value,
            "value_text": "显卡", "ok": 1, "ts": ts,
        }])

    def test_excluding_current_run_makes_drop_detectable(self):
        """同一个 ts（最苛刻的情形）：排除本轮后必须能看出降幅。"""
        same_second = "2026-10-06T17:09:47Z"
        self._record("r1", 100.0, same_second)
        self._record("r2", 80.0, same_second)   # 本轮

        # 不排除本轮 —— "上次价格"变成本轮自己，降幅为 0
        naive = store.price_history(self.conn, "gpu", days=30)
        self.assertEqual(naive["last_value"], 80.0)
        self.assertEqual(
            prices.analyze_prices(
                [point(value=80.0)],
                {"gpu": prices.PriceHistory(**{k: naive[k] for k in (
                    "item", "samples", "failures", "median", "lowest", "lowest_at",
                    "last_value", "last_at", "last_changed_at",
                    "minutes_since_seen", "minutes_since_change")})},
                {"gpu": prices.PriceRules(target_price=None)},
            ).anomalies, [], "不排除本轮时降幅恒为 0 —— 这正是那个 bug 的样子",
        )

        # 排除本轮 —— 正确
        fixed = store.price_history(self.conn, "gpu", days=30, exclude_run_id="r2")
        self.assertEqual(fixed["samples"], 1)
        self.assertEqual(fixed["last_value"], 100.0)

    def test_exclude_run_id_also_hides_its_failures(self):
        ts = http.utc_now()
        self._record("r1", 100.0, ts)
        store.start_run(self.conn, "r2", "price_watch", ts)
        store.insert_samples(self.conn, "r2", [{
            "target": "gpu", "metric": "price", "value_num": None,
            "value_text": "显卡", "ok": 0, "ts": ts, "error": "boom",
        }])
        self.assertEqual(store.price_history(self.conn, "gpu", days=30)["failures"], 1)
        self.assertEqual(
            store.price_history(self.conn, "gpu", days=30, exclude_run_id="r2")["failures"], 0)

    def test_build_history_passes_the_exclusion_through(self):
        ts = http.utc_now()
        self._record("r1", 100.0, ts)
        self._record("r2", 80.0, ts)
        out = price_watch.build_history(self.conn, ["gpu"], days=30, exclude_run_id="r2")
        self.assertEqual(out["gpu"].last_value, 100.0)

    def test_task_passes_its_own_run_id(self):
        """任务层必须真的把它自己的 run_id 传下去 —— 传漏了就等于没修。"""
        import inspect

        source = inspect.getsource(price_watch.run)
        self.assertIn("exclude_run_id=run_id", source,
                      "price_watch.run 必须把本轮 run_id 传给 build_history")


class TestStubService(unittest.TestCase):
    """桩服务本身也要测 —— 它是验证其它东西的前提。"""

    @classmethod
    def setUpClass(cls):
        cls.store = price_stub.PriceStore(price_stub.DEFAULT_PRICES)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                        price_stub.make_handler(cls.store))
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.store.reset()

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_price_and_set_and_reset(self):
        self.assertEqual(self.get("/price/rtx5070")["data"]["price"], 4599)
        self.get("/set?item=rtx5070&price=3599")
        self.assertEqual(self.get("/price/rtx5070")["data"]["price"], 3599)
        self.get("/reset")
        self.assertEqual(self.get("/price/rtx5070")["data"]["price"], 4599)

    def test_health(self):
        self.assertEqual(self.get("/health")["status"], "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
