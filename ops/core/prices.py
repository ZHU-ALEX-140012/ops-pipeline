"""价格采集与判据 —— 把骨架复用到"盯价格"这个场景上。

## 一个关键差别：价格**没有"正常值"**

`api_watch` 判的是"这个接口还活着吗"，所以"偏离基线 3 倍"是有意义的判据。
**价格不一样** —— 显卡卖 4000 还是 4300 都叫正常，没有"应该的值"。

所以这里只用三个**有明确含义**的判据：

| 判据 | 依据 | 为什么它有意义 |
|---|---|---|
| 到价 | 用户自己设的目标价 | 这是唯一真正确定的"我想要的价格" |
| 单次变化幅度 | 相对上一次 | 一夜降 8% 是事件，长期在 4000±100 波动不是 |
| 窗口极值 | 30 天内的最低/最高 | "创 N 天新低"是人真正会行动的信号 |

**刻意不用"偏离中位数 N 倍"** —— 那需要先假设价格有一个基准，而它没有。
把它当成 `api_watch` 来调参，结果只会在促销季疯狂误报。

## 另一个关键差别：跌破目标价是**好消息**

故障告警说"出事了"，价格告警说"可以买了"。所以这些异常走 `notice` 级别
（邮件显示"🟡 提醒"），并且文案要写成机会而不是故障。

## 复用了之前踩出来的三条原则

1. **静默失败防护**：取不到价格是**失败**，不是"没降价"。整轮 0 条数据要单独报出来 ——
   这正是当初"0 条 = 失败，不是没事"那条规则。
2. **区分"没变"和"抓不到"**：`last_at` 旧了说明数据源挂了（要报）；
   `last_changed_at` 旧了只说明价格稳定（**别报**）。
3. **去重按商品隔离**：去重键是"任务 + 商品 + 级别"，
   否则一个商品的告警会把另一个压掉（`target_of_worst` 已经做到了）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from . import http, judge

# 货币符号。取不到就用代码本身（`1234 CNY`），不猜。
CURRENCY_SYMBOLS = {"CNY": "¥", "RMB": "¥", "USD": "$", "EUR": "€", "JPY": "¥", "GBP": "£"}

# 价格里常见的噪声：货币符号、千分位、空格、全角字符
_PRICE_NOISE = re.compile(r"[^\d.\-]")


def format_price(value: float | None, currency: str = "CNY") -> str:
    if value is None:
        return "—"
    symbol = CURRENCY_SYMBOLS.get((currency or "").upper())
    text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{symbol}{text}" if symbol else f"{text} {currency}".strip()


def _age(minutes: float | None) -> str:
    if minutes is None:
        return "—"
    if minutes < 90:
        return f"{minutes:.0f} 分钟前"
    if minutes < 60 * 48:
        return f"{minutes / 60:.1f} 小时前"
    return f"{minutes / 1440:.1f} 天前"


def report_style() -> dict[str, Any]:
    """给共用报告渲染器的"价格版式"。

    **为什么放在这里而不是 `report.py`**：渲染器是通用的，它不该知道"价格"这个概念
    （否则每加一个场景都要去改它）。"价格怎么显示"是价格模块自己的事 ——
    这里把它作为两个回调交出去，渲染器只负责调用。

    对比一下默认（探测版式）：那里显示 `3599.0ms`、基线列是 p50/p95/连续失败；
    价格这里显示 `¥3,599`、基线列是 中位价/最低价/上次价格/距上次采集。
    """

    def value_cell(sample: Mapping[str, Any]) -> str:
        value = sample.get("value_num")
        if value is None:
            return ""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value)
        return format_price(number, str(sample.get("currency") or "CNY"))

    def columns() -> list[tuple[str, Any]]:
        return [
            ("中位价", lambda info: format_price(info.get("median"), info.get("currency") or "CNY")),
            ("最低价", lambda info: format_price(info.get("lowest"), info.get("currency") or "CNY")),
            ("上次价格", lambda info: format_price(info.get("last_value"),
                                                  info.get("currency") or "CNY")),
            ("距上次采集", lambda info: _age(info.get("minutes_since_seen"))),
        ]

    return {"value_format": value_cell, "baseline_columns": columns()}


# ---------------------------------------------------------------- 纯函数：解析

def extract_path(payload: Any, path: str | None) -> Any:
    """按 `a.b.0.c` 这种路径从 JSON 里取值。

    支持嵌套对象与数组下标。路径为空时直接返回整个 payload
    （有些接口就返回一个裸数字或字符串）。
    """
    if not path:
        return payload
    node = payload
    for segment in str(path).split("."):
        if segment == "":
            continue
        if isinstance(node, Mapping):
            if segment not in node:
                raise KeyError(f"路径 {path!r} 在 {segment!r} 处断了")
            node = node[segment]
        elif isinstance(node, (list, tuple)):
            try:
                node = node[int(segment)]
            except (ValueError, IndexError) as exc:
                raise KeyError(f"路径 {path!r} 在 {segment!r} 处不是合法下标") from exc
        else:
            raise KeyError(f"路径 {path!r} 在 {segment!r} 处无法继续（当前是 {type(node).__name__}）")
    return node


def parse_price(raw: Any) -> float | None:
    """把各种形态的价格转成数字。

    实测会遇到：`3999`、`"3999"`、`"¥3,999.00"`、`"3999元"`、`"￥ 3 999"`。
    解析不出来就返回 **None**（而不是 0 或猜一个值）——
    0 会被下游当成"价格暴跌到 0"，那是最坏的一种错误。
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value if value > 0 else None
    text = _PRICE_NOISE.sub("", str(raw).replace("，", ",").replace("　", " "))
    if not text or text in {"-", ".", "-."}:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value > 0 else None


# ---------------------------------------------------------------- 数据结构

@dataclass
class PricePoint:
    """本轮某个商品的一次价格抓取结果。"""

    item: str
    label: str = ""
    value: float | None = None
    currency: str = "CNY"
    ok: bool = False
    error: str | None = None
    ts: str = ""
    url: str = ""

    def to_sample(self) -> dict[str, Any]:
        # 复用 sample 表：metric 固定 price，value_num 存价格、value_text 存标签。
        # currency 只进 run_doc（库里没这一列，insert_samples 会忽略多余的键），
        # 报告渲染靠它把 3599 显示成 ¥3,599。
        return {
            "target": self.item,
            "metric": "price",
            "value_num": self.value if self.ok else None,
            "value_text": self.label or self.item,
            "currency": self.currency,
            "ok": 1 if self.ok else 0,
            "ts": self.ts,
            "status_code": None,
            "error": self.error,
        }


@dataclass
class PriceHistory:
    """从库里读出来的历史（判定层保持无 I/O，所以由任务层填）。"""

    item: str
    samples: int = 0
    failures: int = 0
    median: float | None = None
    lowest: float | None = None
    lowest_at: str | None = None
    last_value: float | None = None
    last_at: str | None = None
    last_changed_at: str | None = None
    minutes_since_seen: float | None = None
    minutes_since_change: float | None = None


@dataclass
class PriceRules:
    """一个商品的判据。全部可选 —— 只设目标价也能用。"""

    target_price: float | None = None
    drop_percent: float | None = 8.0
    rise_percent: float | None = 15.0
    new_low_days: int | None = 14
    stale_hours: float = 6.0
    # 到价是"我要的东西到了"，可以调得更响一点；默认与其它一致
    target_severity: str = "notice"
    other_severity: str = "notice"

    @classmethod
    def from_config(cls, raw: Mapping[str, Any] | None) -> PriceRules:
        raw = raw or {}

        def number(key: str, default: float | None) -> float | None:
            value = raw.get(key, default)
            if value is None or value == "":
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        def whole(key: str, default: int | None) -> int | None:
            value = number(key, default)
            return None if value is None else int(value)

        return cls(
            target_price=number("target_price", None),
            drop_percent=number("drop_percent", 8.0),
            rise_percent=number("rise_percent", 15.0),
            new_low_days=whole("new_low_days", 14),
            stale_hours=float(number("stale_hours", 6.0) or 6.0),
            target_severity=str(raw.get("target_severity", "notice")),
            other_severity=str(raw.get("other_severity", "notice")),
        )


# ---------------------------------------------------------------- 判据

def analyze_prices(
    points: list[PricePoint],
    history: Mapping[str, PriceHistory],
    rules: Mapping[str, PriceRules],
    *,
    default_rules: PriceRules | None = None,
) -> judge.Analysis:
    """把一轮价格抓取转成 metrics + anomalies。纯函数，无 I/O。"""
    default_rules = default_rules or PriceRules()
    samples: list[dict[str, Any]] = []
    anomalies: list[judge.Anomaly] = []
    notes: list[str] = []
    baseline_out: dict[str, Any] = {}

    ok_points = [p for p in points if p.ok and p.value is not None]

    # 0 条 = 失败，不是"没降价"（静默失败防护的第一道闸）
    if not points:
        anomalies.append(
            judge.Anomaly("empty_round", "-", "本轮没有配置任何商品（0 条 = 失败，不是没事）", "alert")
        )
    elif not ok_points:
        anomalies.append(
            judge.Anomaly("price_no_data", "-",
                          f"本轮 {len(points)} 个商品**一个价格都没抓到**（0 条 = 失败，不是没降价）",
                          "alert")
        )

    for point in points:
        samples.append(point.to_sample())
        rule = rules.get(point.item, default_rules)
        hist = history.get(point.item) or PriceHistory(item=point.item)
        baseline_out[point.item] = {
            "samples": hist.samples,
            "median": hist.median,
            "lowest": hist.lowest,
            "last_value": hist.last_value,
            "minutes_since_seen": hist.minutes_since_seen,
            "minutes_since_change": hist.minutes_since_change,
            "currency": point.currency,
        }

        if not point.ok or point.value is None:
            anomalies.append(judge.Anomaly(
                "price_missing", point.item,
                f"没取到「{point.label or point.item}」的价格：{point.error or '未知原因'}"
                f"（取不到 ≠ 没降价）",
                "alert",
            ))
            continue

        value = point.value
        currency = point.currency
        name = point.label or point.item

        # ① 数据源是否还活着：用"最近一次**成功抓到**"而不是"最近一次变化"
        if hist.minutes_since_seen is not None and rule.stale_hours:
            if hist.minutes_since_seen > rule.stale_hours * 60:
                anomalies.append(judge.Anomaly(
                    "price_source_stale", point.item,
                    f"「{name}」已 {hist.minutes_since_seen / 60:.1f} 小时没抓到价格"
                    f"（阈值 {rule.stale_hours} 小时）—— 数据源可能挂了",
                    "alert",
                ))

        # ② 到价：唯一真正确定的判据
        if rule.target_price is not None and value <= rule.target_price:
            gap = (rule.target_price - value) / rule.target_price * 100
            anomalies.append(judge.Anomaly(
                "price_below_target", point.item,
                f"「{name}」现价 {format_price(value, currency)} 已到你的目标价 "
                f"{format_price(rule.target_price, currency)}（低 {gap:.1f}%）",
                rule.target_severity,
            ))

        # ③ 单次变化幅度：相对**上一次**，而不是相对中位数
        if hist.last_value:
            change = (value - hist.last_value) / hist.last_value * 100
            if rule.drop_percent is not None and change <= -abs(rule.drop_percent):
                anomalies.append(judge.Anomaly(
                    "price_drop", point.item,
                    f"「{name}」{format_price(hist.last_value, currency)} → "
                    f"{format_price(value, currency)}，一次降了 {abs(change):.1f}%"
                    f"（阈值 {rule.drop_percent}%）",
                    rule.other_severity,
                ))
            elif rule.rise_percent is not None and change >= abs(rule.rise_percent):
                anomalies.append(judge.Anomaly(
                    "price_rise", point.item,
                    f"「{name}」{format_price(hist.last_value, currency)} → "
                    f"{format_price(value, currency)}，一次涨了 {change:.1f}%"
                    f"（阈值 {rule.rise_percent}%）—— 可能是缺货或临时加价",
                    rule.other_severity,
                ))

        # ④ 窗口极值：人真正会行动的信号
        if rule.new_low_days and hist.lowest is not None and hist.samples >= 3:
            if value < hist.lowest:
                anomalies.append(judge.Anomaly(
                    "price_new_low", point.item,
                    f"「{name}」现价 {format_price(value, currency)} 是近 "
                    f"{rule.new_low_days} 天最低（此前最低 "
                    f"{format_price(hist.lowest, currency)}）",
                    rule.other_severity,
                ))
        elif rule.new_low_days and hist.samples < 3:
            notes.append(f"「{name}」历史样本只有 {hist.samples} 条，"
                         f"暂不判断是否创新低（冷启动）")

        # 价格稳定不是异常，但值得记一笔 —— 免得看报告的人以为采集坏了
        if hist.minutes_since_change is not None and hist.minutes_since_change > 24 * 60:
            notes.append(f"「{name}」价格已 {hist.minutes_since_change / 60:.0f} 小时未变"
                         f"（采集正常，这是稳定不是故障）")

    metrics = {
        "items": len(points),
        "targets": len(points),      # 与其它任务保持同一套字段名，报告渲染直接复用
        "ok": len(ok_points),
        "failed": len(points) - len(ok_points),
        "anomalies": len(anomalies),
        "lowest": min((p.value for p in ok_points), default=None),
        "highest": max((p.value for p in ok_points), default=None),
    }
    return judge.Analysis(
        metrics=metrics, samples=samples, baseline=baseline_out,
        anomalies=anomalies, notes=notes,
    )


# ---------------------------------------------------------------- 采集（唯一有 I/O 的地方）

def collect_item(item_cfg: Mapping[str, Any], pcfg: http.ProbeConfig) -> PricePoint:
    """抓一个商品的价格。

    **采集是配置驱动的通用能力**：给它一个返回 JSON 的地址和一个取值路径，
    它就能用。刻意不去爬电商页面 —— 那类站点有反爬、有 ToS 限制、改版就失效，
    把系统绑死在一个会随时坏掉的抓取上，是这类项目最常见的死法。
    """
    name = str(item_cfg.get("name") or "")
    label = str(item_cfg.get("label") or name)
    url = str(item_cfg.get("url") or "")
    currency = str(item_cfg.get("currency") or "CNY")
    path = item_cfg.get("price_path")
    method = str(item_cfg.get("method") or "GET").upper()
    body = item_cfg.get("payload") if method == "POST" else None
    ts = http.utc_now()

    point = PricePoint(item=name, label=label, currency=currency, ts=ts, url=url)
    if not url:
        point.error = "配置里没有 url"
        return point

    response = http.request_json(
        method, url,
        json_body=body,
        timeout_ms=int(item_cfg.get("timeout_ms") or pcfg.timeout_ms),
        extra_headers=item_cfg.get("headers"),
    )
    # ⚠️ 这里**不能**用 response.ok：它的含义是"**有响应**"
    # （给契约断言层用的，4xx/5xx 也算有响应、也要能看到状态码）。
    # 而价格采集要的是"**成功取到**" —— 一个 404 或 500 的响应体里没有价格，
    # 拿它往下走只会得到一个莫名其妙的"路径断了"。
    if response.error is not None:
        point.error = response.error
        return point
    if response.status is None or not 200 <= response.status < 300:
        point.error = f"HTTP {response.status}（非 2xx 视为没取到价格）"
        return point

    try:
        raw = extract_path(response.body, path)
    except KeyError as exc:
        point.error = str(exc)
        return point

    value = parse_price(raw)
    if value is None:
        point.error = f"取到的值没法当价格解析：{raw!r}"
        return point

    point.value = value
    point.ok = True
    return point
