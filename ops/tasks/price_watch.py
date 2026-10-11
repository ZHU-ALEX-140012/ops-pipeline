"""显卡价格监控 —— 把骨架复用到"盯价格"这个场景。

和 `api_watch` 的差别只在**两块**：

* **采集**换成"按配置抓 JSON 里的价格字段"（`core/prices.py` 的 `collect_item`）；
* **判据**换成价格语义（到价 / 单次降幅 / 窗口新低 / 数据源静默）。

其余全部复用：落库、基线、AI 归因、去重冷却、邮件、报告、只读调查工具、小模型分流。
收尾走同一个 `pipeline.finalize` —— 这也是当初把收尾逻辑只写一份的回报：
新流水线不用再实现一遍"发不发、发过没有、发失败了怎么办"。

⚠️ **为什么采集是配置驱动、而不是写死爬某电商**：
那类页面有反爬、有 ToS 限制、改版就失效。把系统绑在一个随时会坏的抓取上，
是这类项目最常见的死法。所以这里只提供"给定 JSON 地址 + 取值路径就能用"的能力，
具体接哪个源由配置决定 —— 接官方 API、接比价服务、接你自己的中间层都行。
"""

from __future__ import annotations

import uuid
from typing import Any

from ..core import config as cfgmod, http, pipeline, prices, report, store

TASK = "price_watch"


def _run_id(started_at: str) -> str:
    return f"{started_at.replace('-', '').replace(':', '')}-{TASK}-{uuid.uuid4().hex[:6]}"


def build_history(conn: Any, items: list[str], *, days: int = 30,
                  exclude_run_id: str | None = None) -> dict[str, prices.PriceHistory]:
    """从库里读价格历史（判定层保持无 I/O，所以这里做 I/O）。

    `exclude_run_id` **必须传本轮 run_id**：本轮的样本在统计之前已经落库，
    不排除的话"上一次价格"就是本轮自己，`price_drop` 恒为 0、永远不会触发。
    """
    out: dict[str, prices.PriceHistory] = {}
    for item in items:
        raw = store.price_history(conn, item, days=days, exclude_run_id=exclude_run_id)
        out[item] = prices.PriceHistory(
            item=item,
            samples=int(raw["samples"]),
            failures=int(raw["failures"]),
            median=raw["median"],
            lowest=raw["lowest"],
            lowest_at=raw["lowest_at"],
            last_value=raw["last_value"],
            last_at=raw["last_at"],
            last_changed_at=raw["last_changed_at"],
            minutes_since_seen=raw["minutes_since_seen"],
            minutes_since_change=raw["minutes_since_change"],
            stock_buyable=raw["stock_buyable"],
            stock_label=raw["stock_label"],
            stock_remaining=raw["stock_remaining"],
            target_price=raw["target_price"],
        )
    return out


def run(cfg: dict[str, Any], *, dry_run: bool = False, no_ai: bool = False) -> int:
    task_cfg = cfg.get(TASK) or {}
    started_at = http.utc_now()
    run_id = _run_id(started_at)
    conn = store.connect(cfgmod.data_dir(cfg, "ops.db"))
    store.start_run(conn, run_id, TASK, started_at)

    log_lines: list[str] = []
    try:
        # ---------------------------------------------------------- 1. 采集
        pcfg = http.ProbeConfig(
            timeout_ms=int(task_cfg.get("timeout_ms", 8000)),
            retries=0,                      # 价格不该重试：重试拿到的是同一个值，白白拖慢
            rate_limit_ms=int(task_cfg.get("rate_limit_ms", 800)),
        )
        items = [i for i in (task_cfg.get("items") or []) if i.get("enabled", True)]
        # 凭据在这一层解析（它才拿得到 cfg 与密钥），合进 headers 后再交给采集器。
        # 采集器因此完全不碰配置与密钥，保持可纯测试。
        prepared: list[dict[str, Any]] = []
        for raw_item in items:
            item = {**raw_item, "headers": prices.resolve_headers(cfg, raw_item)}
            note = prices.credential_note(cfg, raw_item)
            if note:
                item["_credential_note"] = note
                log_lines.append(f"「{raw_item.get('name')}」{note}")
            prepared.append(item)
        items = prepared
        # 人工录入的价格当作一种"源"喂给同一个采集器 ——
        # 这样下游（判据、告警、报告、历史）完全不需要知道它是怎么来的。
        manual = prices.load_manual_prices(cfg)
        points = [prices.collect_item(item, pcfg, manual) for item in items]

        # 每个商品"当前的心理价"也各存一行样本。
        #
        # **为什么要存**：判"值得下手"这种跨越时需要知道**上一轮**的心理价。
        # 不存的话只能拿本轮的目标价去算旧状态 —— 用户一改目标价，
        # 系统就会把"刚刚才满足"误判成"早就满足了"，于是漏掉那条最该报的信号。
        rules = {
            str(i.get("name")): prices.PriceRules.from_config(i.get("rules") or i)
            for i in items
        }
        for name, rule_cfg in (task_cfg.get("rules") or {}).items():
            rules[str(name)] = prices.PriceRules.from_config(rule_cfg)

        # 价格、库存、心理价各存一行样本（metric 不同），三者的历史互不干扰
        sample_rows: list[dict[str, Any]] = []
        for point in points:
            sample_rows.append(point.to_sample())
            stock_row = point.to_stock_sample()
            if stock_row:
                sample_rows.append(stock_row)
            target = rules.get(point.item).target_price if rules.get(point.item) else None
            if target is not None:
                sample_rows.append({
                    "target": point.item, "metric": "target", "value_num": float(target),
                    "value_text": "心理价", "ok": 1, "ts": point.ts,
                })
        store.insert_samples(conn, run_id, sample_rows)
        got = sum(1 for p in points if p.ok)
        stocked = sum(1 for p in points if p.stock_buyable)
        log_lines.append(f"采集 {len(points)} 个商品，取到价格 {got} 个"
                         + (f"，其中有货 {stocked} 个" if any(
                             p.stock_buyable is not None for p in points) else ""))
        if points and all(not p.ok for p in points) and manual:
            log_lines.append("提示：本轮一个价都没取到，先检查 url / price_path / price_regex 配置")

        # ---------------------------------------------------------- 2. 判据
        history = build_history(conn, [str(i.get("name")) for i in items],
                                days=int(task_cfg.get("history_days", 30)),
                                exclude_run_id=run_id)

        analysis = prices.analyze_prices(
            points, history, rules,
            default_rules=prices.PriceRules.from_config(task_cfg.get("default_rules")),
        )
        run_doc = {
            "schema": "ops/run@1",
            "run_id": run_id,
            "task": TASK,
            "started_at": started_at,
            "finished_at": http.utc_now(),
            "status": "degraded" if analysis.anomalies else "ok",
            "metrics": analysis.metrics,
            "samples": analysis.samples,
            "baseline": analysis.baseline,
            "anomalies": [a.to_dict() for a in analysis.anomalies],
            "notes": analysis.notes,
        }
        report.write_run_doc(cfg, run_id, run_doc)

        # ---------------------------------------------------------- 3. 收尾（与其它流水线完全共用）
        result = pipeline.finalize(
            cfg,
            conn,
            task=TASK,
            run_id=run_id,
            analysis=analysis,
            run_doc=run_doc,
            prompt_name="price_watch",
            session_policy=((cfg.get("ai") or {}).get("session_policy") or {}).get(TASK, "fresh"),
            item_label="商品",
            value_label="价格",
            # 价格版式：`¥3,599` 而不是 `3599.0ms`，基线列换成中位价/最低价/上次价格
            report_style=prices.report_style(),
            always_ai=bool(task_cfg.get("always_ai", False)),
            dry_run=dry_run,
            no_ai=no_ai,
            log_lines=log_lines,
        )
        print(
            f"[{TASK}] {run_id} got={got}/{len(points)} status={result.status} "
            f"severity={result.severity} {pipeline.ai_note(result)} "
            f"alert={result.alert_status} report={result.report_file}"
        )
        return result.exit_code

    except Exception as exc:  # noqa: BLE001 - 任何异常都要落库，绝不静默
        pipeline.report_failure(conn, run_id, exc)
        print(f"[{TASK}] {run_id} FAILED: {type(exc).__name__}: {exc}")
        raise
    finally:
        conn.close()
