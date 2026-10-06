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


class TestBuySignal(unittest.TestCase):
    """⚠️ **这一组是「哪个值得买」这个功能的核心。**

    "到价"和"值得下手"不是一回事，单独看哪个都不够：
      * 到价但无货 → 买不到，白高兴
      * 有货但没到价 → 还能再等
    只有**两个同时成立**，"可以买了"这句话才站得住。

    而且它和"到价"一样必须是**跨越**而不是**状态** ——
    "到价且有货"会持续好几天，每轮都报就变成唠叨，用户会把通知关掉。
    """

    def test_buy_signal_when_target_hit_and_in_stock(self):
        result = analyze([point(value=38000.0, stock_state=33, stock_label="现货",
                                stock_buyable=True, stock_remaining=5)],
                         {"gpu": history(last_value=40000.0, stock_buyable=True)},
                         prices.PriceRules(target_price=39000))
        self.assertIn("buy_signal", kinds(result))
        detail = next(a.detail for a in result.anomalies if a.kind == "buy_signal")
        self.assertIn("值得下手", detail)
        self.assertIn("现货", detail)

    def test_no_buy_signal_when_out_of_stock_even_if_target_hit(self):
        """到价但无货 → **不是**可以买。说"可以买了"会让收件人白跑一趟。"""
        result = analyze([point(value=38000.0, stock_state=34, stock_label="无货",
                                stock_buyable=False)],
                         {"gpu": history(last_value=40000.0, stock_buyable=False)},
                         prices.PriceRules(target_price=39000))
        self.assertNotIn("buy_signal", kinds(result))
        self.assertIn("price_below_target", kinds(result))
        detail = next(a.detail for a in result.anomalies if a.kind == "price_below_target")
        self.assertIn("无货", detail, "到价但买不到，必须在文案里说清")

    def test_no_buy_signal_when_in_stock_but_above_target(self):
        result = analyze([point(value=40000.0, stock_state=33, stock_label="现货",
                                stock_buyable=True)],
                         {"gpu": history(last_value=40000.0, stock_buyable=True)},
                         prices.PriceRules(target_price=39000))
        self.assertNotIn("buy_signal", kinds(result))

    def test_buy_signal_does_not_repeat(self):
        """已经"到价且有货"了就不再每轮喊 —— 同一条理由。"""
        result = analyze([point(value=38000.0, stock_state=33, stock_label="现货",
                                stock_buyable=True)],
                         {"gpu": history(last_value=38000.0, stock_buyable=True)},
                         prices.PriceRules(target_price=39000))
        self.assertNotIn("buy_signal", kinds(result))

    def test_buy_signal_fires_when_it_just_restocked(self):
        """上一轮还没货（所以买不成），这一轮补货了 —— 这是**新的一次机会**。"""
        result = analyze([point(value=38000.0, stock_state=33, stock_label="现货",
                                stock_buyable=True)],
                         {"gpu": history(last_value=38000.0, stock_buyable=False)},
                         prices.PriceRules(target_price=39000))
        self.assertIn("buy_signal", kinds(result))
        detail = next(a.detail for a in result.anomalies if a.kind == "buy_signal")
        self.assertIn("上一轮还没货", detail)

    def test_buy_signal_reason_mentions_the_previous_price(self):
        result = analyze([point(value=38000.0, stock_state=33, stock_label="现货",
                                stock_buyable=True)],
                         {"gpu": history(last_value=41000.0, stock_buyable=True)},
                         prices.PriceRules(target_price=39000))
        detail = next(a.detail for a in result.anomalies if a.kind == "buy_signal")
        self.assertIn("¥41,000", detail, "要说明上一轮多少钱，用户才知道是不是真降了")


class TestTargetChangeIsHandled(unittest.TestCase):
    """⚠️ **这组是实测踩到 bug 之后补的。**

    判"值得下手"的跨越时，如果拿**本轮**的目标价去算**上一轮**的状态，
    用户一改心理价就会出错：比如把目标价从 24,000 调到 26,000，
    系统会认为"上一轮 25,699 就已经满足 26,000 了" -> **不报**，
    而实际上这正是"刚刚才满足"的那一刻 —— **漏掉的恰好是最该报的信号。**

    修法：每轮把**当时的心理价**也存一行样本，判跨越时用上一轮记下的那个。
    """

    def test_changing_the_target_can_trigger_the_signal(self):
        result = analyze(
            [point(value=25699.0, stock_state=33, stock_label="现货", stock_buyable=True)],
            # 上一轮记下的心理价是 24000（当时 25699 还没到），本轮的目标价是 26000
            {"gpu": history(last_value=25699.0, stock_buyable=True, target_price=24000.0)},
            prices.PriceRules(target_price=26000),
        )
        self.assertIn("buy_signal", kinds(result),
                      "改高目标价后，'刚刚才满足'必须报出来")

    def test_explanation_uses_the_previous_target_not_the_current_one(self):
        """解释和判断必须用**同一个**目标价，否则会说成"首次观测"这种自相矛盾的话。"""
        result = analyze(
            [point(value=25699.0, stock_state=33, stock_label="现货", stock_buyable=True)],
            {"gpu": history(last_value=25699.0, stock_buyable=True, target_price=24000.0)},
            prices.PriceRules(target_price=26000),
        )
        detail = next(a.detail for a in result.anomalies if a.kind == "buy_signal")
        self.assertIn("¥24,000", detail, "要说明当时的目标价是多少")
        self.assertNotIn("首次观测", detail, "有历史就别说成首次观测")

    def test_no_signal_when_already_satisfied_under_the_previous_target(self):
        """上一轮在**当时的目标价**下就已经满足了 -> 不重复报。"""
        result = analyze(
            [point(value=25699.0, stock_state=33, stock_label="现货", stock_buyable=True)],
            {"gpu": history(last_value=25699.0, stock_buyable=True, target_price=26000.0)},
            prices.PriceRules(target_price=26000),
        )
        self.assertNotIn("buy_signal", kinds(result))

    def test_falls_back_to_current_target_when_no_history(self):
        """还没记过心理价（例如刚加上这个功能）-> 退回用本轮的，不能崩。"""
        result = analyze(
            [point(value=38000.0, stock_state=33, stock_label="现货", stock_buyable=True)],
            {"gpu": history(last_value=40000.0, stock_buyable=True, target_price=None)},
            prices.PriceRules(target_price=39000),
        )
        self.assertIn("buy_signal", kinds(result))

    def test_target_price_is_stored_as_a_sample(self):
        """每轮都要把当时的心理价存下来 —— 不存的话上面那条修复就无从谈起。"""
        import inspect

        from ops.tasks import price_watch

        source = inspect.getsource(price_watch.run)
        self.assertIn('"metric": "target"', source)


class TestCrossCandidateFields(unittest.TestCase):
    """判断层要回答"买哪个"，就必须知道**哪些能比、各自的心理价是多少**。

    比价必须同 `spec` 比：1TB 版比 4TB 版便宜是应该的，
    把它说成"更划算"是误导。所以这两个标签会写进 run_doc 交给判断层。
    """

    def test_baseline_carries_target_platform_spec(self):
        result = prices.analyze_prices(
            [point(value=38000.0)],
            {"gpu": history(last_value=40000.0)},
            {"gpu": prices.PriceRules(target_price=39000, platform="dgx_spark",
                                      spec="128G+4TB")},
        )
        info = result.baseline["gpu"]
        self.assertEqual(info["target_price"], 39000)
        self.assertEqual(info["platform"], "dgx_spark")
        self.assertEqual(info["spec"], "128G+4TB")

    def test_unset_platform_and_spec_are_none_not_empty(self):
        result = analyze([point()], {"gpu": history()})
        self.assertIsNone(result.baseline["gpu"]["platform"])
        self.assertIsNone(result.baseline["gpu"]["spec"])

    def test_prompt_requires_same_spec_comparison(self):
        """提示词里必须写明这条规则 —— 否则模型会拿 1TB 的价格说"更划算"。"""
        text = (ROOT / "ops" / "prompts" / "price_watch.md").read_text(encoding="utf-8")
        self.assertIn("比价必须同", text)
        self.assertIn("到价 ≠ 可以买", text)
        self.assertIn("buy_signal", text)

    def test_prompt_asks_for_a_clear_recommendation(self):
        text = (ROOT / "ops" / "prompts" / "price_watch.md").read_text(encoding="utf-8")
        for phrase in ("再等等", "现在就可以下单", "现在都不值得买"):
            self.assertIn(phrase, text)


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
