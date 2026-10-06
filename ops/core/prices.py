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

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
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
            # 库存这一列只在真的解析到库存时才出现（见下面的 stock_columns）
        ]

    def stock_columns() -> list[tuple[str, Any]]:
        def clean(text: Any) -> str:
            # 库里存的是 "34 无货"（带取值码，便于排查），
            # 但表里只需要给人看的那个词 —— 把开头的数字码去掉。
            return re.sub(r"^\d+\s*", "", str(text or "")).strip() or "—"

        def remaining(info: Mapping[str, Any]) -> str:
            count = info.get("stock_remaining")
            # 无货时不显示"0 件" —— 那是京东的默认值，读起来像"马上补货"
            if not count or not info.get("stock_now") or "无货" in str(info.get("stock_now")):
                return "—"
            return str(count)

        return [
            ("当前库存", lambda info: clean(info.get("stock_now"))),
            ("上一轮库存", lambda info: clean(info.get("stock_prev"))),
            ("剩余件数", remaining),
        ]

    return {"value_format": value_cell, "baseline_columns": columns(),
            "stock_columns": stock_columns()}


# 页面里出现这些，说明"不是没爬到价格，是**站点要求登录**"。
#
# 实测：京东移动版商品页（item.m.jd.com/product/<sku>.html）**可达**，
# 标题也能拿到，但价格字段是 `"priceLoginText":"登录查看价格"`。
# 把这种情形和"页面改版了""正则写错了"混成一个"没匹配到"，
# 会让人往完全错误的方向排查 —— 所以单独识别、单独报。
LOGIN_WALL_MARKERS = (
    "登录查看价格",
    "priceLoginText",
    "urgeLogin",
    "请先登录",
    "passport.jd.com",
    "login.jd.com",
)


def detect_login_wall(text: str) -> str | None:
    """页面看起来要求登录才能看价格吗？返回命中的标记（用于报错信息）。"""
    for marker in LOGIN_WALL_MARKERS:
        if marker in text:
            return marker
    return None


def resolve_headers(cfg: dict[str, Any], item_cfg: Mapping[str, Any]) -> dict[str, str]:
    """把配置里的凭据解析成真正的 Cookie 头。

    **凭据只从环境变量 / `config/secrets.local.toml` 读，绝不写进 config.toml** ——
    和项目里其它凭据（企业微信 secret、邮箱授权码）走同一套机制。

    两种写法，`cookie_parts` 优先：

    | 写法 | 配置 | 适用 |
    |---|---|---|
    | 整串 | `cookie_env = "OPS_X"` | 你能一次拿到完整 Cookie 串 |
    | 分片 | `cookie_parts = [["pt_key","OPS_A"], ["pt_pin","OPS_B"]]` | 只能逐个 Cookie 值复制 |

    ## ⚠️ 为什么必须有"分片"这个写法（实测换来的）

    我一开始让用户用浏览器控制台 `copy(document.cookie)` 一次拿到整串，
    结果**拿不到登录凭证**：京东的 `pt_key` / `pt_pin` 是 **HttpOnly**，
    浏览器**故意不给网页脚本看**，所以 `document.cookie` 里根本没有它们。

    能拿到 HttpOnly Cookie 的地方只有一处：**开发者工具的
    「应用程序 → 存储 → Cookie」表格**（或「网络 → 请求标头」里的 cookie 行）。
    但那里的表格是"一个名字一个值"，逐个复制之后还要人手工拼成
    `pt_key=xxx; pt_pin=yyy` —— 那是最容易出错的一步。

    所以让配置声明"哪个名字对应哪个环境变量"，由程序去拼：
    用户只需要往每个环境变量里粘一个值，不用记格式、不用拼字符串。
    """
    from . import config as cfgmod

    headers = dict(item_cfg.get("headers") or {})

    # ① 分片（优先）：每个元素是 [cookie 名, 环境变量名]
    parts = item_cfg.get("cookie_parts") or []
    pieces: list[str] = []
    for entry in parts:
        try:
            name, env = entry[0], entry[1]
        except (TypeError, IndexError):
            continue
        value = cfgmod.resolve_secret(cfg, str(env))
        if value and str(value).strip():
            pieces.append(f"{name}={str(value).strip()}")
    if pieces:
        headers["Cookie"] = "; ".join(pieces)

    # ② 整串（分片没配成时才用）
    if "Cookie" not in headers:
        cookie_env = item_cfg.get("cookie_env")
        if cookie_env:
            cookie = cfgmod.resolve_secret(cfg, str(cookie_env))
            if cookie:
                headers["Cookie"] = cookie.strip()

    return headers


def credential_note(cfg: dict[str, Any], item_cfg: Mapping[str, Any]) -> str | None:
    """凭据是否齐全。缺哪个就说哪个 —— 少一个 `pt_pin` 就可能整串认证失败。

    单独一个函数（而不是塞进 headers 里当假头部）是为了让任务层能把它
    写进日志与报告，而采集器不必知道"配置与密钥"这回事。
    """
    from . import config as cfgmod

    parts = item_cfg.get("cookie_parts") or []
    if not parts:
        return None
    missing: list[str] = []
    for entry in parts:
        try:
            name, env = entry[0], entry[1]
        except (TypeError, IndexError):
            continue
        value = cfgmod.resolve_secret(cfg, str(env))
        if not (value and str(value).strip()):
            missing.append(f"{name}（环境变量 {env}）")
    if not missing:
        return None
    if len(missing) == len(parts):
        return f"还没录入登录凭据：缺少 {'、'.join(missing)}"
    return f"登录凭据不完整，缺少 {'、'.join(missing)}"


# ---------------------------------------------------------------- 库存（京东）

# 京东商品页里库存就在 `_itemInfo.stock.StockState`，而且**页面自己的 JS 写明了取值表**：
#
#     if (stockState === 33)                              stockName = '现货'
#     else if (stockState === 39 || stockState === 40)     stockName = '有货'
#     else if (stockState === 36)                          stockName = '预定'
#     else                                                 stockName = '无货'
#
# 实测三个商品与浏览器里看到的完全一致：自营 34（无货）、赞奇 34（无货）、
# 华硕 33（现货，页面显示"仅剩2件"）。
#
# ⚠️ 别用页面上的**文案**判断："抱歉，此商品在所选区域暂无存货" 这句
# 在两个**有货和无货的页面里都出现**（它是静态模板），拿它当判据会全判成无货。
STOCK_LABELS = {33: "现货", 39: "有货", 40: "有货", 36: "预定"}
STOCK_IN_VALUES = (33, 39, 40)
STOCK_PREORDER_VALUES = (36,)
DEFAULT_STOCK_REGEX = r'"StockState"\s*:\s*"?(\d+)"?'
# `rn` 是**剩余件数**（华硕那个 rn=2，页面正显示"仅剩2件"）
DEFAULT_STOCK_REMAINING_REGEX = r'"rn"\s*:\s*(\d+)'


def stock_label(value: int | None) -> str:
    if value is None:
        return "未知"
    return STOCK_LABELS.get(int(value), "无货")


def parse_stock(text: str, item_cfg: Mapping[str, Any]) -> dict[str, Any]:
    """从页面文本里解析库存。返回 {state, label, remaining, buyable}。

    取法可配（`stock_regex` / `stock_remaining_regex`），默认就是京东那套；
    换站点只改配置，不改代码。
    """
    out: dict[str, Any] = {"state": None, "label": "未知", "remaining": None, "buyable": None}
    if not text:
        return out

    pattern = str(item_cfg.get("stock_regex") or DEFAULT_STOCK_REGEX)
    try:
        match = re.search(pattern, text, re.S)
    except re.error:
        return out
    if not match:
        return out

    raw = str(match.group(1) if match.groups() else match.group(0)).strip()
    in_values = [str(v) for v in (item_cfg.get("stock_in_values") or STOCK_IN_VALUES)]

    # 两种形态都支持：京东给数字码（33/34/36/39…），别的站点可能给 "true"/"in_stock"。
    # 数字走取值表翻译成中文；非数字就直接用原文当标签。
    try:
        state: int | None = int(raw)
    except (TypeError, ValueError):
        state = None

    if state is not None:
        out["state"] = state
        out["label"] = stock_label(state)
        out["buyable"] = str(state) in in_values
    else:
        out["state"] = None
        out["label"] = raw or "未知"
        out["buyable"] = raw.lower() in {v.lower() for v in in_values}

    remaining_pattern = item_cfg.get("stock_remaining_regex")
    if remaining_pattern is None:
        remaining_pattern = DEFAULT_STOCK_REMAINING_REGEX
    if remaining_pattern:
        try:
            found = re.search(str(remaining_pattern), text, re.S)
        except re.error:
            found = None
        if found:
            try:
                out["remaining"] = int(found.group(1) if found.groups() else found.group(0))
            except (TypeError, ValueError):
                pass
    return out


# ---------------------------------------------------------------- 人工录入的价格
#
# 有些商品**没有可自动抓的源**：官方店封机器人、电商返回反爬页、
# 商品页价格是 JS 渲染的。这时候最诚实的做法不是硬爬，而是
# **让人看一眼、敲一条命令**，剩下的（历史、判据、告警、报告）全自动。
#
# 对"一个月看一次"的东西（比如 DGX Spark 这种几万块的设备）这就是最实用的形态。

MANUAL_FILE = "manual-prices.json"


def manual_prices_path(cfg: dict[str, Any]) -> Path:
    from . import config as cfgmod

    return Path(cfgmod.data_dir(cfg, "state")) / MANUAL_FILE


def load_manual_prices(cfg: dict[str, Any]) -> dict[str, Any]:
    """读人工录入的价格。文件不存在/坏了都返回空 dict —— 绝不因此炸掉整轮。"""
    path = manual_prices_path(cfg)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_manual_price(cfg: dict[str, Any], item: str, price: float | str,
                      currency: str = "CNY", note: str = "") -> Path:
    """记一条人工价格。**带时间戳**，因为"什么时候看到的"本身就是证据。"""
    from . import config as cfgmod

    data = load_manual_prices(cfg)
    data[item] = {
        "price": price,
        "currency": currency,
        "note": note,
        "recorded_at": http.utc_now(),
    }
    path = manual_prices_path(cfg)
    cfgmod.ensure_dir(path.parent)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


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


# 掩码字符：出现它们说明这个"价格"是占位符，不是数字。
#
# 实测：京东对**未登录**请求返回的是 `"price":"4??00"`（真的问号字符，0x3F）——
# 那是掩码占位价，用来防爬。而 `parse_price` 会把 `?` 当噪声剥掉，
# 于是 **"4??00" 被凭空解析成 400**：一个完全由噪声捏造出来的数字。
# 这比"解析失败"危险得多，所以见到掩码字符一律判为**解析失败**。
MASK_CHARS = "?*＊？×"


def parse_price(raw: Any) -> float | None:
    """把各种形态的价格转成数字。

    实测会遇到：`3999`、`"3999"`、`"¥3,999.00"`、`"3999元"`、`"￥ 3 999"`。
    解析不出来就返回 **None**（而不是 0 或猜一个值）——
    0 会被下游当成"价格暴跌到 0"，那是最坏的一种错误。

    ⚠️ 带掩码字符（`4??00`）的一律返回 None，见 `MASK_CHARS` 的说明。
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value if value > 0 else None
    text = str(raw)
    if any(char in text for char in MASK_CHARS):
        return None
    text = _PRICE_NOISE.sub("", text.replace("，", ",").replace("　", " "))
    if not text or text in {"-", ".", "-."}:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value > 0 else None


def extract_regex(text: str, pattern: str) -> Any:
    """用正则从页面文本里抠一个值。**有捕获组就用第 1 组，否则用整个匹配。**

    为什么需要它：不少商品页的价格既不在 JSON 里、也没有稳定的结构化数据，
    只在 HTML 文本里。给一条正则就能用，仍然是"配置驱动"，
    **不是给某个站点写死的爬虫** —— 换站点只改配置，不改代码。
    """
    try:
        compiled = re.compile(pattern, re.S)
    except re.error as exc:
        raise ValueError(f"price_regex 不是合法正则：{exc}") from exc
    match = compiled.search(text)
    if not match:
        return None
    return match.group(1) if match.groups() else match.group(0)


# ---------------------------------------------------------------- 数据结构

@dataclass
class PricePoint:
    """本轮某个商品的一次抓取结果（价格 + 库存）。"""

    item: str
    label: str = ""
    value: float | None = None
    currency: str = "CNY"
    ok: bool = False
    error: str | None = None
    ts: str = ""
    url: str = ""
    source: str = "json"
    # 人工录入时才有：你是什么时候看到的、以及备注
    recorded_at: str = ""
    note: str = ""
    # 库存（可选；没开库存监控或页面里读不到时为 None）
    stock_state: int | None = None
    stock_label: str = ""
    stock_remaining: int | None = None
    stock_buyable: bool | None = None

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
            "source": self.source,
            "ok": 1 if self.ok else 0,
            "ts": self.ts,
            "status_code": None,
            "error": self.error,
        }

    def to_stock_sample(self) -> dict[str, Any] | None:
        """库存单独存一行样本（metric='stock'），这样价格与库存的历史互不干扰。

        `value_num` 存 **1/0（能不能买）** 而不是京东那个数字码 ——
        历史查询与判断层只关心"能不能买"，数字码留在 `value_text` 里备查。
        """
        if self.stock_buyable is None:
            return None
        return {
            "target": self.item,
            "metric": "stock",
            "value_num": 1.0 if self.stock_buyable else 0.0,
            # 文案与报告里保持一致；无货时不写"剩 0 件"（那是京东的默认值，读起来会误导）
            "value_text": (f"{self.stock_state} {self.stock_label}"
                           if self.stock_state is not None else self.stock_label)
                          + (f"，仅剩 {self.stock_remaining} 件"
                             if self.stock_buyable and self.stock_remaining is not None
                             else ""),
            "currency": self.currency,
            "source": self.source,
            "ok": 1,
            "ts": self.ts,
            "status_code": None,
            "error": None,
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
    # 库存：**上一轮**（不含本轮）能不能买。用来判"刚补货 / 刚卖完"这种**跨越**。
    stock_buyable: bool | None = None
    stock_label: str = ""
    stock_remaining: int | None = None
    # **上一轮**的心理价。判"值得下手"这种跨越时必须用它，而不是本轮的目标价 ——
    # 否则用户改一次心理价，系统就会拿新目标去算旧状态，
    # 把"刚刚才满足"误判成"早就满足了"，于是**漏掉那条最该报的信号**。
    target_price: float | None = None


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
    # ⚠️ 合理性区间。**这不是可选项，是必需的守卫**（见 collect_item 里的说明）
    min_price: float | None = None
    max_price: float | None = None
    # 库存：开着就盯"补货/卖完"，并在"仅剩 N 件"时提一句
    watch_stock: bool = False
    stock_low_threshold: int | None = 3
    # 跨候选比较用的两个标签（会写进 run_doc，让判断层能做同类比较）
    #
    # `platform`：同一平台 = 同一类东西，可以直接比价
    #             （例如同为 "dgx_spark" 的三个渠道）
    # `spec`    ：配置（例如 "128G+4TB"）。**比价必须同 spec 比** ——
    #             1TB 版比 4TB 版便宜是应该的，那不是"更划算"。
    platform: str = ""
    spec: str = ""

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
            min_price=number("min_price", None),
            max_price=number("max_price", None),
            watch_stock=bool(raw.get("watch_stock", False)),
            stock_low_threshold=whole("stock_low_threshold", 3),
            platform=str(raw.get("platform") or ""),
            spec=str(raw.get("spec") or ""),
        )


def sanity_check(value: float, item_cfg: Mapping[str, Any],
                 matched: Any = None) -> str | None:
    """合理性区间守卫。**返回问题描述，或在区间内返回 None。**

    ## 为什么这**不是**可选项

    实测：一个看起来无害的正则 `"price"\\s*:\\s*"?([0-9][0-9,]*)"?` 在京东商品页上
    匹配到了 `"price":4` —— 于是系统把 **¥4** 记成了 DGX Spark（三万块的东西）的价格，
    下一步就会报"价格暴跌 99.99%"。

    **比抓不到更糟的是抓到错的**：抓不到会报警、会有人去看；
    抓到错的会安静地产生假历史、假新低、假跌幅，把整个价格库污染掉。

    所以商品必须声明"它大概多少钱"：
        min_price = 10000
        max_price = 100000
    区间外的值一律当**解析失败**处理，而不是当价格。
    """
    low = item_cfg.get("min_price")
    high = item_cfg.get("max_price")
    if low is None and high is None:
        return None
    try:
        low_v = float(low) if low is not None and low != "" else None
        high_v = float(high) if high is not None and high != "" else None
    except (TypeError, ValueError):
        return None
    if low_v is not None and value < low_v:
        return (f"匹配到 {matched!r} → {value}，低于合理性下限 {low_v} —— "
                f"大概率是匹配到了页面上无关的数字（不是这个商品的价格）")
    if high_v is not None and value > high_v:
        return (f"匹配到 {matched!r} → {value}，高于合理性上限 {high_v} —— "
                f"同上，先检查正则是不是匹配错了地方")
    return None


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
            # 库存：`stock_now` 是本轮、`stock_prev` 是上一轮（判定"跨越"靠它）
            "stock_now": point.stock_label or None,
            "stock_remaining": point.stock_remaining,
            "stock_prev": hist.stock_label or None,
            # 跨候选比较需要的东西：判断层靠它们回答"哪个值得买"
            #   `target_price` 是这个商品的心理价位；`platform`/`spec` 决定能不能直接比
            "target_price": rule.target_price,
            "platform": rule.platform or None,
            "spec": rule.spec or None,
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
        #
        # ⚠️ **只在"刚跌破"时提醒，不在"一直在下面"时每轮提醒。**
        #
        # 这是价格监控能不能用的关键。价格一旦低于目标价，如果每轮都报，
        # 你会一天收到两次一模一样的消息，然后就会把通知关掉 ——
        # 那正是"告警系统死于误报"的标准死法。
        #
        # 判据是**跨越**（上一次在目标价之上、这次不在），而不是**状态**（现在在不在下面）。
        # 没有历史时的第一次观测也算跨越 —— 那时你确实需要知道"它已经在目标价下面了"。
        if rule.target_price is not None and value <= rule.target_price:
            crossed = hist.last_value is None or hist.last_value > rule.target_price
            gap = (rule.target_price - value) / rule.target_price * 100
            if crossed:
                # ⚠️ 到价**不等于**可以买。如果当前无货，"价格到了"是没用的信息 ——
                #    必须在文案里说清楚，否则收件人会白跑一趟。
                stock_suffix = ""
                if point.stock_buyable is False:
                    stock_suffix = "（⚠️ 但当前**无货**，暂时买不到）"
                elif point.stock_buyable is True and point.stock_remaining is not None:
                    stock_suffix = f"（现货，仅剩 {point.stock_remaining} 件）"
                anomalies.append(judge.Anomaly(
                    "price_below_target", point.item,
                    f"「{name}」现价 {format_price(value, currency)} 已到你的目标价 "
                    f"{format_price(rule.target_price, currency)}（低 {gap:.1f}%）{stock_suffix}",
                    rule.target_severity,
                ))
            else:
                # 持续在目标价下方：记一笔说明，但不打扰
                notes.append(
                    f"「{name}」现价 {format_price(value, currency)} "
                    f"仍在目标价 {format_price(rule.target_price, currency)} 之下"
                    f"（低 {gap:.1f}%）—— 这是**持续状态**，不重复提醒；"
                    f"只有再次跌破或明显变化时才报"
                )

        # ③ 「值得下手」：**到价 + 现货**，两个条件同时成立才算
        #
        # ⚠️ 这一条是整套价格监控里最该有的东西，因为它回答的是
        #    **"我现在该不该掏钱"** —— 而"到价"和"有货"单独看都不够：
        #      * 到价但无货 → 买不到，白高兴
        #      * 有货但没到价 → 还能再等
        #    只有两个同时成立，"可以买了"这句话才站得住。
        #
        # 判据同样是**跨越**：上一轮还不满足（没到价，或没货），这一轮满足了。
        # 因为"到价且有货"是个状态，不是事件 —— 每轮都报会变成唠叨。
        if (rule.target_price is not None and value <= rule.target_price
                and point.stock_buyable is True):
            # ⚠️ 用**上一轮记下来的心理价**，不是本轮的 ——
            #    用户改过目标价时，拿新目标去算旧状态会把"刚刚满足"误判成"早就满足"，
            #    于是漏掉这条最该报的信号。（实测踩到过。）
            previous_target = (hist.target_price if hist.target_price is not None
                               else rule.target_price)
            previous_ok = (
                hist.last_value is not None
                and hist.last_value <= previous_target
                and hist.stock_buyable is True
            )
            if not previous_ok:
                remaining = (f"，仅剩 {point.stock_remaining} 件"
                             if point.stock_remaining is not None else "")
                # 解释"为什么现在才报"，必须和上面的判断用**同一个** previous_target ——
                # 否则会出现"判断说刚满足、解释却说首次观测"这种自相矛盾。
                was = []
                if hist.last_value is None:
                    pass
                elif hist.last_value > previous_target:
                    was.append(f"上一轮还 {format_price(hist.last_value, currency)}"
                               f"，高于当时的目标价 "
                               f"{format_price(previous_target, currency)}")
                elif hist.stock_buyable is False:
                    was.append("上一轮还没货")
                if hist.stock_buyable is False and not was:
                    was.append("上一轮还没货")
                reason = ("（" + "、".join(was) + "）") if was else "（首次观测）"
                anomalies.append(judge.Anomaly(
                    "buy_signal", point.item,
                    f"「{name}」**值得下手了**：{format_price(value, currency)} "
                    f"已到目标价 {format_price(rule.target_price, currency)}，"
                    f"且**现货**{remaining} {reason}",
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

        # ⑥ 库存：只在**状态刚变化**时提醒，不每轮重复
        #
        # 和"到价"同一个道理（见上面 ② 的说明）：库存是一个**状态**，
        # 不是事件。如果"有货"每轮都报，你会天天收到同一条消息然后关掉通知 ——
        # 而对这个商品来说，"补货了"恰恰是最值得知道的那一条。
        #
        # 所以判据也是**跨越**：上一轮买不到、这一轮能买 → 报；
        # 上一轮能买、这一轮买不到 → 报；状态没变 → 只记一条说明。
        if point.stock_buyable is not None:
            previous = hist.stock_buyable
            # "剩 0 件"是京东在无货时给的 rn 值 —— 读起来别扭，所以只在**有货**时提件数。
            # 无货时说"剩 0 件"会让人以为"马上要补货"，而它只是无货。
            if point.stock_buyable and point.stock_remaining is not None:
                stock_note = f"{point.stock_label}，仅剩 {point.stock_remaining} 件"
            else:
                stock_note = point.stock_label
            if previous is None:
                # 第一次观测到库存：只在"现在买不到"时说一句，免得开局就打扰
                if not point.stock_buyable:
                    notes.append(f"「{name}」目前{stock_note}（首次观测，之后会盯补货）")
            elif point.stock_buyable and not previous:
                anomalies.append(judge.Anomaly(
                    "stock_available", point.item,
                    f"「{name}」**补货了** —— 现在{stock_note}（上一轮还买不到）",
                    rule.other_severity,
                ))
            elif not point.stock_buyable and previous:
                anomalies.append(judge.Anomaly(
                    "stock_out", point.item,
                    f"「{name}」**卖完了** —— 现在{stock_note}（上一轮还能买）",
                    rule.other_severity,
                ))
            else:
                notes.append(f"「{name}」库存状态未变：{stock_note}")

            # 有货但快没了 —— 这不是"跨越"，但对你决定要不要立刻下单有用。
            #
            # ⚠️ 同样只在**刚变少**时提醒：如果"仅剩 2 件"每轮都报，你会一天收到
            #    两次一模一样的消息然后关掉通知 —— 和"到价"是同一个毛病。
            #    判据：上一轮的件数还不低于阈值（或没有历史），这一轮低了。
            if (point.stock_buyable and point.stock_remaining is not None
                    and rule.stock_low_threshold
                    and point.stock_remaining <= rule.stock_low_threshold):
                previous_remaining = hist.stock_remaining
                newly_low = (previous_remaining is None
                             or previous_remaining > rule.stock_low_threshold)
                if newly_low:
                    anomalies.append(judge.Anomaly(
                        "stock_low", point.item,
                        f"「{name}」{stock_note} —— 快没了（阈值 {rule.stock_low_threshold} 件）",
                        rule.other_severity,
                    ))
                # 已经偏少且状态没变 → 不再多说一句：
                # 上面的"库存状态未变：现货，仅剩 2 件"已经把事实说清了

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

def _attach_stock(point: PricePoint, text: str, item_cfg: Mapping[str, Any]) -> None:
    """把页面里的库存信息挂到采集结果上。

    只在这几个来源上做：JSON 接口通常没有库存字段，硬解析只会得到 None。
    """
    if not (item_cfg.get("watch_stock") or item_cfg.get("stock_regex")):
        return
    info = parse_stock(text, item_cfg)
    point.stock_state = info["state"]
    point.stock_label = info["label"]
    point.stock_remaining = info["remaining"]
    point.stock_buyable = info["buyable"]


def collect_item(item_cfg: Mapping[str, Any], pcfg: http.ProbeConfig,
                 manual: Mapping[str, Any] | None = None) -> PricePoint:
    """取一个商品的价格（顺带解析库存）。三种来源，按配置选：

    | `source` | 怎么取 | 需要什么 |
    |---|---|---|
    | `json`（默认） | 请求 → JSON → 按路径取值 | `url` + `price_path` |
    | `html` | 请求 → 文本 → 按正则抠值 | `url` + `price_regex` |
    | `manual` | 读人工录入的状态文件 | `tools/price_set.py` 记过一条 |

    **采集是配置驱动的通用能力**：给它地址和取值方式，它就能用。
    刻意不去爬特定电商 —— 那类站点有反爬、有 ToS 限制、改版就失效，
    而且实测里它们连请求都不给你（见 README 的探测结论）。
    "抓不到"是常态，所以 `manual` 是**一等公民**而不是补丁。
    """
    name = str(item_cfg.get("name") or "")
    label = str(item_cfg.get("label") or name)
    currency = str(item_cfg.get("currency") or "CNY")
    source = str(item_cfg.get("source") or ("html" if item_cfg.get("price_regex") else "json"))
    ts = http.utc_now()

    point = PricePoint(item=name, label=label, currency=currency, ts=ts,
                       url=str(item_cfg.get("url") or ""), source=source)

    # ---------------------------------------------------------- 人工录入
    if source == "manual":
        entry = (manual or {}).get(name)
        if not isinstance(entry, Mapping):
            point.error = (f"还没有「{name}」的人工价格。"
                           f"用 tools/price_set.py --item {name} --price <价格> 记一条")
            return point
        value = parse_price(entry.get("price"))
        if value is None:
            point.error = f"人工录入的值没法当价格解析：{entry.get('price')!r}"
            return point
        point.value = value
        point.currency = str(entry.get("currency") or currency)
        point.ok = True
        point.recorded_at = str(entry.get("recorded_at") or "")
        point.note = str(entry.get("note") or "")
        return point

    url = str(item_cfg.get("url") or "")
    if not url:
        point.error = "配置里没有 url"
        return point

    # ---------------------------------------------------------- 页面文本 + 正则
    if source == "html" or item_cfg.get("price_regex"):
        pattern = item_cfg.get("price_regex")
        if not pattern:
            point.error = "source=html 但没有配 price_regex"
            return point
        cookie_env = item_cfg.get("cookie_env")
        headers = dict(item_cfg.get("headers") or {})
        # 凭据已经由任务层解析好并合进 headers（见 resolve_headers 与 price_watch.run）——
        # `collect_item` 不碰配置与密钥，保持可纯测试。
        using_cookie = "Cookie" in headers
        response = http.fetch_text(
            str(item_cfg.get("method") or "GET").upper(), url,
            timeout_ms=int(item_cfg.get("timeout_ms") or pcfg.timeout_ms),
            max_bytes=int(item_cfg.get("max_bytes") or 400_000),
            extra_headers=headers,
        )
        if not response.ok:
            point.error = response.error or f"HTTP {response.status}"
            return point
        if response.truncated:
            # 截断意味着"价格可能在没读到的那部分里"，必须说出来，不能装作读全了
            point.note = f"页面被截断在 {response.bytes_read} 字节，未匹配到不代表页面上没有"
        try:
            raw = extract_regex(response.text, str(pattern))
        except ValueError as exc:
            point.error = str(exc)
            return point
        if raw is None:
            # 先分辨"要登录"和"真的没匹配到" —— 这两件事的排查方向完全相反
            wall = detect_login_wall(response.text)
            note = str(item_cfg.get("_credential_note") or "")
            if wall:
                if using_cookie:
                    hint = "，且**已带 Cookie**（可能已过期，请重新取一份）"
                elif note:
                    hint = f"。{note}；录入后重试，或用 tools/price_set.py 人工记价格"
                else:
                    hint = ("。用 scripts\\set-secret.cmd 录入登录凭据，"
                            "或用 tools/price_set.py 人工记价格")
                point.error = (f"页面要求**登录**才能看到价格（命中标记 {wall!r}）{hint}")
                return point
            point.error = (f"正则在页面里没匹配到价格（读了 {response.bytes_read} 字节）。"
                           f"页面改版或价格由 JS 渲染时会发生这种情况")
            return point
        value = parse_price(raw)
        if value is None:
            point.error = f"正则匹配到的值没法当价格解析：{raw!r}"
            return point
        problem = sanity_check(value, item_cfg, matched=raw)
        if problem:
            # 误匹配 + 页面要登录时，**把现象和解法一起说**。
            # 只说"低于下限"，用户会去调正则；只说"需要登录"，他不知道 4 哪来的。
            wall = detect_login_wall(response.text)
            if wall:
                hint = ("，且**已带 Cookie**（可能已过期，请重新取一份）" if using_cookie
                        else f"；要自动抓就把 {cookie_env or 'Cookie'} 配好"
                             f"（或用 tools/price_set.py 人工记价格）")
                point.error = (f"{problem}。页面同时提示**需要登录**"
                               f"（命中标记 {wall!r}）{hint}")
            else:
                point.error = problem
            return point
        point.value = value
        point.ok = True
        _attach_stock(point, response.text, item_cfg)
        return point
        point.value = value
        point.ok = True
        return point

    # ---------------------------------------------------------- JSON + 路径
    method = str(item_cfg.get("method") or "GET").upper()
    body = item_cfg.get("payload") if method == "POST" else None
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
        raw = extract_path(response.body, item_cfg.get("price_path"))
    except KeyError as exc:
        point.error = str(exc)
        return point

    value = parse_price(raw)
    if value is None:
        point.error = f"取到的值没法当价格解析：{raw!r}"
        return point

    problem = sanity_check(value, item_cfg, matched=raw)
    if problem:
        point.error = problem
        return point

    point.value = value
    point.ok = True
    return point
