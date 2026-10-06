"""库存监控的测试。

⚠️ 这一组全部来自**实测的真实数据**，不是想象的字段。

京东商品页里库存就在 `_itemInfo.stock.StockState`，而且**页面自己的 JS 写明了取值表**：

    if (stockState === 33)                          stockName = '现货'
    else if (stockState === 39 || stockState === 40) stockName = '有货'
    else if (stockState === 36)                      stockName = '预定'
    else                                             stockName = '无货'

实测三个商品的取值与浏览器里看到的完全一致：
    自营 34（页面：暂无存货）/ 赞奇 34（页面：暂时售完）/ 华硕 33（页面：仅剩2件）

而 `rn` 是**剩余件数**（华硕那个 rn=2，页面正显示"仅剩2件"）。

**最值得记住的一条**：别用页面上的**文案**判断库存 ——
"抱歉，此商品在所选区域暂无存货" 这句在**有货和无货的页面里都出现**（静态模板）。
后面有一条测试专门锁住这件事。
"""

from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ops.core import http, prices, store  # noqa: E402

TMP = ROOT / "data" / "_test_tmp" / "price_stock"

# 京东页面里真实的片段（取自实测抓下来的页面）
PAGE_OUT_OF_STOCK = '"stock": {"skuId":"100254961843",...,"StockState":34,"rn":0,"code":1}'
PAGE_IN_STOCK = '"stock": {"skuId":"10181652415616",...,"StockState":33,"rn":2,"code":1}'
PAGE_PREORDER = '"stock": {"skuId":"x",...,"StockState":36,"rn":0,"code":1}'
PAGE_39 = '"stock": {"skuId":"x",...,"StockState":39,"rn":0,"code":1}'

# ⚠️ 这段静态模板文案在**有货和无货**的页面里都会出现
STATIC_TIP = '"tips":{"text":"抱歉，此商品在所选区域暂无存货","backgroupColor":"#fbf9da;"}'


def point(**kw):
    base = {"item": "gpu", "label": "显卡", "value": 40000.0, "ok": True,
            "ts": http.utc_now(), "currency": "CNY"}
    base.update(kw)
    return prices.PricePoint(**base)


def history(**kw):
    base = {"item": "gpu"}
    base.update(kw)
    return prices.PriceHistory(**base)


def analyze(points, hist=None, rule=None):
    return prices.analyze_prices(
        points, hist or {}, {"gpu": rule or prices.PriceRules(target_price=None)})


def kinds(analysis):
    return sorted(a.kind for a in analysis.anomalies)


class TestParseStock(unittest.TestCase):
    def test_the_three_real_values(self):
        """实测的三个真实取值，必须与浏览器里看到的一致。"""
        cases = [
            (PAGE_OUT_OF_STOCK, 34, "无货", False),
            (PAGE_IN_STOCK, 33, "现货", True),
            (PAGE_PREORDER, 36, "预定", False),
            (PAGE_39, 39, "有货", True),
        ]
        for text, state, label, buyable in cases:
            with self.subTest(state=state):
                info = prices.parse_stock(text, {})
                self.assertEqual(info["state"], state)
                self.assertEqual(info["label"], label)
                self.assertEqual(info["buyable"], buyable)

    def test_reads_the_remaining_count(self):
        """`rn` 是剩余件数 —— 华硕那个 rn=2 正对应页面上的"仅剩2件"。"""
        self.assertEqual(prices.parse_stock(PAGE_IN_STOCK, {})["remaining"], 2)

    def test_unknown_code_is_out_of_stock(self):
        """取值表里没写的码一律当"无货" —— 宁可漏报补货，也不要误报有货。"""
        info = prices.parse_stock('"StockState":99,"rn":0', {})
        self.assertEqual(info["state"], 99)
        self.assertEqual(info["label"], "无货")
        self.assertFalse(info["buyable"])

    def test_missing_field_is_unknown_not_out_of_stock(self):
        """读不到 ≠ 无货。返回 None，让上层去决定怎么说。"""
        info = prices.parse_stock("<html>没有库存字段</html>", {})
        self.assertIsNone(info["state"])
        self.assertIsNone(info["buyable"])
        self.assertEqual(info["label"], "未知")

    def test_static_tip_text_must_not_be_used_as_the_judge(self):
        """⚠️ **这条最值得记住**：那段"暂无存货"文案是静态模板。

        实测发现它在**有货和无货的页面里都出现** —— 拿文案当判据会把所有商品
        都判成无货（包括明明显示"仅剩2件"的那个）。
        所以判据必须是 `StockState` 字段，文案只能当参考。
        """
        # 无库存字段、只有那句文案 → 必须判"未知"，绝不能判"无货"
        info = prices.parse_stock(STATIC_TIP, {})
        self.assertIsNone(info["state"])
        self.assertIsNone(info["buyable"], "文案不能当判据")

        # 而真实字段在场时，一切以字段为准
        info = prices.parse_stock(PAGE_IN_STOCK + STATIC_TIP, {})
        self.assertTrue(info["buyable"], "字段说有货，文案说什么都不算")

    def test_regex_and_values_are_configurable(self):
        """换站点只改配置，不改代码。"""
        info = prices.parse_stock('{"available": true, "left": 7}',
                                  {"stock_regex": r'"available"\s*:\s*(true|false)',
                                   "stock_in_values": ["true"],
                                   "stock_remaining_regex": r'"left"\s*:\s*(\d+)'})
        self.assertTrue(info["buyable"])
        self.assertEqual(info["remaining"], 7)

    def test_empty_text_is_safe(self):
        self.assertIsNone(prices.parse_stock("", {})["buyable"])


class TestStockJudgment(unittest.TestCase):
    def test_restock_is_reported(self):
        """**"补货了"是这个商品最值得知道的一条。**"""
        result = analyze([point(stock_state=33, stock_label="现货", stock_buyable=True,
                                stock_remaining=5)],
                         {"gpu": history(stock_buyable=False, stock_label="无货")})
        self.assertIn("stock_available", kinds(result))
        detail = next(a.detail for a in result.anomalies if a.kind == "stock_available")
        self.assertIn("补货", detail)

    def test_selling_out_is_reported(self):
        result = analyze([point(stock_state=34, stock_label="无货", stock_buyable=False)],
                         {"gpu": history(stock_buyable=True, stock_label="现货")})
        self.assertIn("stock_out", kinds(result))

    def test_unchanged_stock_does_not_repeat(self):
        """⚠️ 库存是**状态**不是事件：没变就不该每轮报一遍。

        这和"到价"是同一个毛病 —— 每轮都报，两天后用户就把通知关了，
        而"补货了"恰恰是最值得知道的那一条。
        """
        result = analyze([point(stock_state=34, stock_label="无货", stock_buyable=False)],
                         {"gpu": history(stock_buyable=False, stock_label="无货")})
        self.assertEqual(kinds(result), [])
        self.assertTrue(any("库存状态未变" in note for note in result.notes))

    def test_first_observation_of_out_of_stock_is_a_note_not_an_alert(self):
        """首次观测到无货：说一句就好，别开局就打扰。"""
        result = analyze([point(stock_state=34, stock_label="无货", stock_buyable=False)], {})
        self.assertEqual(kinds(result), [])
        self.assertTrue(any("首次观测" in note for note in result.notes))

    def test_low_stock_is_reported_when_newly_low(self):
        result = analyze([point(stock_state=33, stock_label="现货", stock_buyable=True,
                                stock_remaining=2)],
                         {"gpu": history(stock_buyable=True, stock_label="现货",
                                         stock_remaining=20)},
                         prices.PriceRules(stock_low_threshold=3))
        self.assertIn("stock_low", kinds(result))

    def test_low_stock_does_not_repeat(self):
        """已经"仅剩 2 件"了就不再每轮重复喊 —— 同一条理由。"""
        result = analyze([point(stock_state=33, stock_label="现货", stock_buyable=True,
                                stock_remaining=2)],
                         {"gpu": history(stock_buyable=True, stock_label="现货",
                                         stock_remaining=2)},
                         prices.PriceRules(stock_low_threshold=3))
        self.assertNotIn("stock_low", kinds(result))
        # "库存状态未变：现货，仅剩 2 件" 已经把事实说清了，不需要再补一句
        self.assertEqual(len([n for n in result.notes if "剩" in n]), 1)

    def test_plenty_in_stock_is_quiet(self):
        result = analyze([point(stock_state=33, stock_label="现货", stock_buyable=True,
                                stock_remaining=50)],
                         {"gpu": history(stock_buyable=True, stock_label="现货",
                                         stock_remaining=50)},
                         prices.PriceRules(stock_low_threshold=3))
        self.assertNotIn("stock_low", kinds(result))

    def test_out_of_stock_does_not_claim_remaining_zero(self):
        """无货时不写"剩 0 件" —— 那是京东的默认值，读起来像"马上补货"。"""
        result = analyze([point(stock_state=34, stock_label="无货", stock_buyable=False,
                                stock_remaining=0)], {})
        for note in result.notes:
            self.assertNotIn("剩 0 件", note)

    def test_no_stock_info_means_no_stock_anomalies(self):
        """没开库存监控的商品不该冒出库存异常。"""
        result = analyze([point()], {"gpu": history()})
        self.assertEqual([k for k in kinds(result) if k.startswith("stock")], [])

    def test_baseline_records_stock(self):
        result = analyze([point(stock_state=33, stock_label="现货", stock_buyable=True,
                                stock_remaining=2)],
                         {"gpu": history(stock_buyable=False, stock_label="无货")})
        info = result.baseline["gpu"]
        self.assertEqual(info["stock_now"], "现货")
        self.assertEqual(info["stock_prev"], "无货")
        self.assertEqual(info["stock_remaining"], 2)


class TestStockSample(unittest.TestCase):
    def test_sample_shape(self):
        sample = point(stock_state=33, stock_label="现货", stock_buyable=True,
                       stock_remaining=2).to_stock_sample()
        self.assertEqual(sample["metric"], "stock")
        self.assertEqual(sample["value_num"], 1.0)
        self.assertIn("仅剩 2 件", sample["value_text"])

    def test_out_of_stock_sample_has_no_count(self):
        sample = point(stock_state=34, stock_label="无货", stock_buyable=False,
                       stock_remaining=0).to_stock_sample()
        self.assertEqual(sample["value_num"], 0.0)
        self.assertNotIn("剩", sample["value_text"])

    def test_no_stock_info_gives_none(self):
        self.assertIsNone(point().to_stock_sample())


class TestStockHistoryRoundTrip(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        (TMP / "data").mkdir(parents=True, exist_ok=True)
        self.conn = store.connect(TMP / "data" / "ops.db")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(TMP, ignore_errors=True)

    def _record(self, run_id: str, ts: str, *, price: float | None, stock: dict | None):
        store.start_run(self.conn, run_id, "price_watch", ts)
        rows = []
        if price is not None:
            rows.append({"target": "gpu", "metric": "price", "value_num": price,
                         "value_text": "显卡", "ok": 1, "ts": ts})
        if stock is not None:
            rows.append({"target": "gpu", "metric": "stock", **stock, "ts": ts})
        store.insert_samples(self.conn, run_id, rows)

    def test_reads_previous_stock_state(self):
        self._record("r1", "2026-10-06T10:00:00Z", price=40000.0,
                     stock={"value_num": 0.0, "value_text": "34 无货", "ok": 1})
        self._record("r2", "2026-10-06T11:00:00Z", price=40000.0,
                     stock={"value_num": 1.0, "value_text": "33 现货，仅剩 2 件", "ok": 1})
        # 排除本轮 → 看到的是上一轮（无货）
        raw = store.price_history(self.conn, "gpu", days=30, exclude_run_id="r2")
        self.assertFalse(raw["stock_buyable"])
        self.assertEqual(raw["stock_label"], "34 无货")

    def test_remaining_is_parsed_from_the_label(self):
        self._record("r1", "2026-10-06T10:00:00Z", price=40000.0,
                     stock={"value_num": 1.0, "value_text": "33 现货，仅剩 2 件", "ok": 1})
        raw = store.price_history(self.conn, "gpu", days=30)
        self.assertTrue(raw["stock_buyable"])
        self.assertEqual(raw["stock_remaining"], 2)

    def test_no_stock_samples_means_unknown(self):
        self._record("r1", "2026-10-06T10:00:00Z", price=40000.0, stock=None)
        raw = store.price_history(self.conn, "gpu", days=30)
        self.assertIsNone(raw["stock_buyable"])

    def test_stock_and_price_histories_do_not_mix(self):
        """两种样本用不同 metric，互不干扰 —— 价格统计不该被库存值污染。"""
        self._record("r1", "2026-10-06T10:00:00Z", price=40000.0,
                     stock={"value_num": 0.0, "value_text": "34 无货", "ok": 1})
        raw = store.price_history(self.conn, "gpu", days=30)
        self.assertEqual(raw["samples"], 1)
        self.assertEqual(raw["last_value"], 40000.0, "价格历史里不该混进 0")
        self.assertEqual(raw["lowest"], 40000.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
