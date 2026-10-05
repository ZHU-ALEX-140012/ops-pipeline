"""评测运行器：用黄金集给判断层打分。

    python tools/eval_suite.py --rule-only          # 只跑规则层基线（免费、秒级）
    python tools/eval_suite.py --limit 5            # 只跑前 5 个场景（控成本）
    python tools/eval_suite.py --only aw_02         # 只跑名字含 aw_02 的场景
    python tools/eval_suite.py                      # 跑全部（真实调用判断层，收费）
    python tools/eval_suite.py --compare            # 与上一次同模式的结果对比

## 为什么要同时跑「规则层」这一臂

规则层是**免费且确定**的下限。只报 AI 的绝对分数说明不了什么 ——
"AI 准确率 85%" 是好是坏？只有跟规则层并排看才知道：
如果规则层也有 84%，那这层 AI 几乎没带来价值。

## 指标口径

* `alert` 一组混淆矩阵：`should_alert` 的精确率/召回率/F1
  —— **精确率（不误报）比召回率更重要**：告警系统死于误报。
* `severity_acc`：严重级别落在可接受集合里的比例
* `root_acc`：根因关键词命中的比例。这是**根因质量的近似**
  （真正的语义评分要上 LLM-as-judge，见 README 的局限说明），
  但它是确定性的、免费的、可解释的 —— 适合做回归检测。
* token / 耗时：升级必须看代价，否则"更准"没有意义。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ops.core import aibridge, config as cfgmod, judge  # noqa: E402

GOLDEN_DIR = ROOT / "evals" / "golden"
EVAL_DIR = ROOT / "data" / "evals"
PROMPT_DIR = ROOT / "ops" / "prompts"

# usage 字典里的键名 → 累加表里的短名。
# 这个映射曾经写错过（表里用 "in"，usage 里是 "input_tokens"），
# 结果 17 次真实调用统计出 0 成本 —— 有单测锁住它。
USAGE_KEYS = {
    "input_tokens": "in",
    "output_tokens": "out",
    "cache_read_tokens": "cache_read",
    "cache_write_tokens": "cache_write",
}


def collect_usage(total: dict[str, int], usage: dict[str, Any] | None) -> dict[str, int]:
    """把一次调用的 usage 累加进总量。未知键忽略（不猜、不编）。"""
    for source, target in USAGE_KEYS.items():
        try:
            total[target] += int((usage or {}).get(source) or 0)
        except (TypeError, ValueError):
            continue
    return total


# ---------------------------------------------------------------- 夹具加载

def load_fixtures(only: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    items = []
    for path in sorted(GOLDEN_DIR.glob("*.json")):
        if path.name.startswith("_"):
            continue
        fixture = json.loads(path.read_text(encoding="utf-8"))
        # 同时匹配场景名与任务名：`--only heartbeat` 应该能筛出 hb_* 那些场景
        if only and only not in fixture["name"] and only != fixture["task"]:
            continue
        items.append(fixture)
    return items[:limit] if limit else items


def analysis_from_doc(doc: dict[str, Any]) -> judge.Analysis:
    """从 run_doc 复原 Analysis，好让规则层独立跑一遍（不依赖任务的 I/O）。"""
    anomalies = [
        judge.Anomaly(kind=a["kind"], target=a["target"], detail=a.get("detail", ""),
                      severity_hint=a.get("severity_hint", "notice"))
        for a in (doc.get("anomalies") or [])
    ]
    return judge.Analysis(
        metrics=dict(doc.get("metrics") or {}),
        samples=list(doc.get("samples") or []),
        baseline=dict(doc.get("baseline") or {}),
        anomalies=anomalies,
        notes=list(doc.get("notes") or []),
    )


# ---------------------------------------------------------------- 打分

def verdict_text(verdict: dict[str, Any]) -> str:
    """把模型所有"说人话"的字段拼起来做关键词判定。

    只看 root_cause_hypothesis 是不公平的 —— 模型完全可能把根因写在
    headline 或 plain_summary 里，而那同样算答对了。
    """
    return " ".join(
        str(verdict.get(key) or "")
        for key in ("headline", "plain_summary", "impact", "root_cause_hypothesis")
    )


def score(verdict: dict[str, Any] | None, expect: dict[str, Any], error: str | None = None) -> dict[str, Any]:
    if verdict is None:
        return {"ok": False, "error": error or "没有产出 verdict",
                "alert_ok": False, "severity_ok": False, "root_ok": False,
                "predicted": None, "severity": None}

    text = verdict_text(verdict).lower()
    predicted = bool(verdict.get("should_alert"))
    expected = bool(expect.get("should_alert"))

    severity_ok: bool | None = None
    allowed = expect.get("severity_any_of")
    if allowed:
        severity_ok = str(verdict.get("severity")) in allowed

    root_ok: bool | None = None
    any_kw = expect.get("mention_any") or []
    none_kw = expect.get("mention_none") or []
    if any_kw or none_kw:
        root_ok = True
        if any_kw and not any(kw.lower() in text for kw in any_kw):
            root_ok = False
        if none_kw and any(kw.lower() in text for kw in none_kw):
            root_ok = False

    return {
        "ok": True, "error": None,
        "predicted": predicted, "expected": expected,
        "alert_ok": predicted == expected,
        "severity": verdict.get("severity"),
        "severity_ok": severity_ok,
        "root_ok": root_ok,
        "headline": verdict.get("headline"),
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tp = sum(1 for r in rows if r["predicted"] and r["expected"])
    fp = sum(1 for r in rows if r["predicted"] and not r["expected"])
    fn = sum(1 for r in rows if not r["predicted"] and r["expected"])
    tn = sum(1 for r in rows if not r["predicted"] and not r["expected"])

    def ratio(num: int, den: int) -> float | None:
        # 分母为 0 时返回 None 而不是 1.0：没有正预测时"精确率"是未定义的，
        # 报成 100% 会骗人。
        return round(num / den, 4) if den else None

    precision, recall = ratio(tp, tp + fp), ratio(tp, tp + fn)
    f1 = None
    if precision and recall:
        f1 = round(2 * precision * recall / (precision + recall), 4)

    n = len(rows)
    sev = [r["severity_ok"] for r in rows if r["severity_ok"] is not None]
    root = [r["root_ok"] for r in rows if r["root_ok"] is not None]

    return {
        "n": n,
        "alert": {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
                  "precision": precision, "recall": recall, "f1": f1,
                  "accuracy": ratio(sum(1 for r in rows if r["alert_ok"]), n)},
        "severity_accuracy": ratio(sum(1 for v in sev if v), len(sev)),
        "severity_n": len(sev),
        "root_accuracy": ratio(sum(1 for v in root if v), len(root)),
        "root_n": len(root),
        "errors": sum(1 for r in rows if not r["ok"]),
    }


# ---------------------------------------------------------------- 两臂

def run_rule_arm(fixture: dict[str, Any]) -> dict[str, Any]:
    doc = fixture["run_doc"]
    analysis = analysis_from_doc(doc)
    started = time.perf_counter()
    verdict = judge.rule_verdict(doc["run_id"], doc["task"], analysis)
    return {
        "verdict": verdict,
        "duration_s": round(time.perf_counter() - started, 4),
        "usage": aibridge.RunUsage().to_dict(),
        "error": None,
    }


def prompt_for(task: str) -> Path | None:
    """该任务在生产上是否使用判断层。

    `heartbeat` 的判据是确定的"距上次运行多久"，生产上**根本不用 AI**
    （见 README 的能力表）。所以评测也不能给它调用判断层 ——
    第一版无脑对每个任务都调，直接因为缺 `heartbeat.md` 崩了。
    """
    path = PROMPT_DIR / f"{task}.md"
    return path if path.is_file() else None


def run_ai_arm(fixture: dict[str, Any], cfg: dict[str, Any], bridge: aibridge.AIBridge) -> dict[str, Any]:
    doc = dict(fixture["run_doc"])
    template = prompt_for(doc["task"])
    if template is None:
        return {
            "verdict": None, "duration_s": 0.0,
            "usage": aibridge.RunUsage().to_dict(), "error": None,
            "skipped": True,
            "reason": f"{doc['task']} 在生产上不使用判断层（无提示词 {doc['task']}.md）",
        }

    # run_id 必须与喂给模型的输入一致，否则 verdict 校验会判它跑题
    run_id = f"eval-{fixture['name'][:28]}-{uuid.uuid4().hex[:6]}"
    doc["run_id"] = run_id

    result = bridge.call(
        task=doc["task"],
        run_id=run_id,
        template_path=template,
        run_doc=doc,
        # 评测绝不能用生产会话：否则一次评测就把日报的"记忆"污染了
        use_session=False,
    )
    return {
        "verdict": result.verdict,
        "duration_s": result.duration_s,
        "usage": result.usage.to_dict(),
        "error": result.error,
        "run_id": run_id,
        "skipped": False,
    }


# ---------------------------------------------------------------- 输出

def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value * 100:.1f}%"
    return str(value)


def render_console(mode: str, metrics: dict[str, Any]) -> str:
    """控制台只输出 ASCII —— 中文 Windows 控制台是 GBK，中文表格会乱码，
    而这份输出经常被重定向到日志里。完整中文报告写进 .md 文件。"""
    alert = metrics["alert"]
    lines = [
        f"=== eval [{mode}] n={metrics['n']} ===",
        f"  should_alert : precision={_fmt(alert['precision'])} "
        f"recall={_fmt(alert['recall'])} f1={_fmt(alert['f1'])} "
        f"acc={_fmt(alert['accuracy'])}",
        f"  confusion    : tp={alert['tp']} fp={alert['fp']} fn={alert['fn']} tn={alert['tn']}",
        f"  severity_acc : {_fmt(metrics['severity_accuracy'])} (n={metrics['severity_n']})",
        f"  root_acc     : {_fmt(metrics['root_accuracy'])} (n={metrics['root_n']})",
        f"  errors       : {metrics['errors']}",
    ]
    tokens = metrics.get("tokens") or {}
    if tokens.get("in") or tokens.get("out"):
        lines.append(
            f"  tokens       : in={tokens['in']} out={tokens['out']} "
            f"cache_read={tokens['cache_read']} cache_write={tokens['cache_write']}"
        )
    if metrics.get("avg_duration_s"):
        lines.append(f"  avg duration : {metrics['avg_duration_s']}s")
    stability = metrics.get("stability")
    if stability and stability["repeat"] > 1:
        lines.append(
            f"  stability    : {stability['scenarios'] - stability['unstable_count']}"
            f"/{stability['scenarios']} scenarios consistent over "
            f"{stability['repeat']} repeats"
        )
    return "\n".join(lines)


def render_markdown(run: dict[str, Any]) -> str:
    ai = run["metrics"].get("ai")
    # 对照必须用**同一子集**的规则层分数：判断层不跑的场景（如 heartbeat）
    # 不该出现在差值里，否则两边 n 不同，差值就是假的。
    rule = run["metrics"].get("rule_subset") or run["metrics"]["rule"]
    all_rule = run["metrics"]["rule"]
    skipped = [r for r in run["scenarios"] if r.get("ai") and r["ai"].get("skipped")]

    lines = [
        "# 判断层评测报告",
        "",
        f"- 时间：{run['started_at']}",
        f"- 模式：{'AI（真实调用判断层）' if run['mode'] == 'ai' else '规则层（免费基线）'}",
        f"- 黄金集：{run['suite']}，共 {all_rule['n']} 个场景",
    ]
    if ai:
        lines.append(
            f"- 判断层实际覆盖：**{rule['n']} / {all_rule['n']}** 个场景"
            + (f"（跳过 {len(skipped)} 个：判断层在这些任务上不使用）" if skipped else "")
        )
    lines += [
        "",
        "## 总分",
        "",
        f"| 指标 | 规则层（同一子集 n={rule['n']}） | AI 判断层 | 差值 |",
        "|---|---|---|---|",
    ]

    def delta(key_a: str) -> str:
        if not ai:
            return "—"
        a = ai["alert"].get(key_a)
        b = rule["alert"].get(key_a)
        if a is None or b is None:
            return "n/a"
        return f"{'+' if a - b >= 0 else ''}{(a - b) * 100:.1f} pp"

    rows = [
        ("should_alert 精确率（不误报）", "precision", None),
        ("should_alert 召回率（不漏报）", "recall", None),
        ("should_alert F1", "f1", None),
        ("should_alert 准确率", "accuracy", None),
        ("严重级别准确率", "severity_accuracy", "top"),
        ("根因关键词命中率", "root_accuracy", "top"),
    ]
    for label, key, scope in rows:
        if scope == "top":
            rv = _fmt(rule.get(key))
            av = _fmt(ai.get(key)) if ai else "—"
            d = "—"
            if ai and ai.get(key) is not None and rule.get(key) is not None:
                diff = (ai[key] - rule[key]) * 100
                d = f"{'+' if diff >= 0 else ''}{diff:.1f} pp"
        else:
            rv = _fmt(rule["alert"].get(key))
            av = _fmt(ai["alert"].get(key)) if ai else "—"
            d = delta(key)
        lines.append(f"| {label} | {rv} | {av} | {d} |")

    lines += [
        "",
        f"规则层在**全部 {all_rule['n']} 个**场景上的精确率 "
        f"{_fmt(all_rule['alert']['precision'])} / 召回率 {_fmt(all_rule['alert']['recall'])}"
        f"（含判断层不覆盖的任务）。",
    ]

    if ai and ai.get("tokens"):
        tokens = ai["tokens"]
        lines += [
            "",
            f"AI 成本：输入 {tokens['in']}（cache 读 {tokens['cache_read']} / 写 "
            f"{tokens['cache_write']}），输出 {tokens['out']}；"
            f"平均 {ai.get('avg_duration_s')}s/次。",
            "",
            "> ⚠️ cache 读的单价远低于普通输入，这里只报用量，不折算成钱。",
        ]

    stability = (ai or {}).get("stability")
    if stability and stability["repeat"] > 1:
        lines += [
            "",
            "## 稳定性（同一批输入重复跑）",
            "",
            f"重复次数 **{stability['repeat']}**，场景数 {stability['scenarios']}，"
            f"其中 **{stability['unstable_count']}** 个场景在重复之间给出了**不一致**的结论。",
            "",
        ]
        if stability["unstable"]:
            lines += ["| 场景 | 答对次数 | 重复次数 |", "|---|---|---|"]
            for item in stability["unstable"]:
                lines.append(
                    f"| {item['name']} | {item['pass_count']} | {item['repeat']} |"
                )
            lines += [
                "",
                "> ⚠️ **这说明单次跑分本身不可靠。** 同一批输入、同一个模型，"
                "两次全量评测的总分可以差 8pp 以上 —— 差异全部来自个别场景的翻转。"
                "所以：看趋势要看多次重复的均值，不要拿一次结果下结论。",
            ]
        else:
            lines.append("所有场景在重复之间结论一致。")

    lines += ["", "## 逐场景", "", "| 场景 | 任务 | 期望 | 规则层 | AI | 说明 |", "|---|---|---|---|---|---|"]
    for row in run["scenarios"]:
        rule_cell = "—"
        if row["rule"]["score"]["predicted"] is not None:
            rule_cell = "告警" if row["rule"]["score"]["predicted"] else "静默"
            if not row["rule"]["score"]["alert_ok"]:
                rule_cell += " ✗"
        ai_cell = "未跑"
        if row.get("ai"):
            if row["ai"].get("skipped"):
                ai_cell = "不适用"
            else:
                s = row["ai"]["score"]
                if s["predicted"] is None:
                    ai_cell = "失败 ✗"
                else:
                    ai_cell = "告警" if s["predicted"] else "静默"
                    ai_cell += " ✓" if s["alert_ok"] else " ✗"
        expect = "告警" if row["expect"]["should_alert"] else "静默"
        lines.append(
            f"| {row['name']} | {row['task']} | {expect} | {rule_cell} | {ai_cell} | {row['note']} |"
        )

    if skipped:
        lines += ["", "### 判断层不适用的场景", ""]
        for row in skipped:
            lines.append(f"- `{row['name']}`（{row['task']}）：{row['ai']['reason']}")

    lines += ["", "## 已知局限", "",
              "- `root_accuracy` 是**关键词命中**的近似，不是语义评分。它便宜、确定、可解释，"
              "适合做回归检测；真正的语义质量需要 LLM-as-judge 或人工标注。",
              f"- 黄金集只有 {all_rule['n']} 个场景，单个场景翻转就会让百分比跳动约 "
              f"{100 / max(rule['n'], 1):.0f}pp，不要拿小数点后的差异当结论。",
              "- 「规则层误报、正确答案不告警」的题型目前只有 1 个。"
              "这类题最能体现判断层的独立价值，应该继续补。",
              "- 对照臂只在判断层覆盖的子集上计算 —— 否则两边 n 不同，差值是假的。",
              ""]
    return "\n".join(lines)


def compare(current: dict[str, Any], previous: dict[str, Any]) -> str:
    """与上一次同模式的结果对比 —— 这是「回归检测」的落点。"""
    lines = [
        f"# 与上次对比（{previous['started_at']} → {current['started_at']}）", "",
        "| 指标 | 上次 | 本次 | 变化 |", "|---|---|---|---|",
    ]
    for arm in ("rule", "ai"):
        cur_arm, prev_arm = current["metrics"].get(arm), previous["metrics"].get(arm)
        if not cur_arm or not prev_arm:
            continue
        for label, getter in (
            ("精确率", lambda m: m["alert"]["precision"]),
            ("召回率", lambda m: m["alert"]["recall"]),
            ("严重级别准确率", lambda m: m["severity_accuracy"]),
            ("根因命中率", lambda m: m["root_accuracy"]),
        ):
            a, b = getter(cur_arm), getter(prev_arm)
            if a is None or b is None:
                delta = "n/a"
            else:
                diff = (a - b) * 100
                delta = f"{'+' if diff >= 0 else ''}{diff:.1f} pp"
            lines.append(f"| {arm} · {label} | {_fmt(b)} | {_fmt(a)} | {delta} |")

    # 逐个场景看翻转 —— 总分可能不变但内部有对有错互相抵消
    prev_by_name = {s["name"]: s for s in previous["scenarios"]}
    flipped = []
    for row in current["scenarios"]:
        old = prev_by_name.get(row["name"])
        if not old or not row.get("ai") or not old.get("ai"):
            continue
        now_ok = row["ai"]["score"]["alert_ok"]
        was_ok = old["ai"]["score"]["alert_ok"]
        if now_ok != was_ok:
            flipped.append(f"- **{row['name']}**：{'答对→答错' if was_ok else '答错→答对'}")
    lines += ["", "## 结果翻转的场景", ""]
    lines += flipped or ["无。"]
    lines += [""]
    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程

def main() -> int:
    parser = argparse.ArgumentParser(description="用黄金集评测判断层")
    parser.add_argument("--rule-only", action="store_true", help="只跑规则层基线（免费）")
    parser.add_argument("--only", help="只跑名字包含该子串的场景")
    parser.add_argument("--limit", type=int, help="最多跑几个场景（控成本）")
    parser.add_argument("--compare", action="store_true", help="与上一次同模式结果对比")
    parser.add_argument("--repeat", type=int, default=1,
                        help="每个场景重复跑几次（用于量化模型的不稳定性；默认 1）")
    parser.add_argument("--timeout", type=int, default=240, help="单次判断超时（秒）")
    parser.add_argument("--list", action="store_true", help="只列出场景，不跑")
    args = parser.parse_args()

    fixtures = load_fixtures(args.only, args.limit)
    if not fixtures:
        print("[FAIL] 没有匹配的场景")
        return 1
    if args.list:
        for item in fixtures:
            print(f"  {item['name']:<34} {item['task']:<12} "
                  f"{'ALERT' if item['expect']['should_alert'] else 'quiet'}  {item['note'][:52]}")
        return 0

    cfg = cfgmod.load_config(str(ROOT / "config" / "config.toml"))
    cfgmod.apply_overrides(cfg, [f"ai.timeout_seconds={args.timeout}"])

    mode = "rule" if args.rule_only else "ai"
    started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    bridge = None
    if mode == "ai":
        if not (cfg.get("ai") or {}).get("enabled", True):
            print("[FAIL] 配置里 ai.enabled = false，无法跑 AI 评测")
            return 1
        bridge = aibridge.AIBridge(cfg)

    scenarios: list[dict[str, Any]] = []
    total_tokens = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
    durations: list[float] = []

    for index, fixture in enumerate(fixtures, 1):
        rule_result = run_rule_arm(fixture)
        row: dict[str, Any] = {
            "name": fixture["name"], "task": fixture["task"], "note": fixture["note"],
            "expect": fixture["expect"],
            "rule": {**rule_result, "score": score(rule_result["verdict"], fixture["expect"])},
            "ai": None,
        }
        if bridge is not None:
            arms: list[dict[str, Any]] = []
            for attempt in range(max(1, args.repeat)):
                ai_result = run_ai_arm(fixture, cfg, bridge)
                if ai_result.get("skipped"):
                    arms = [ai_result]
                    break
                ai_result["score"] = score(ai_result["verdict"], fixture["expect"],
                                           ai_result.get("error"))
                arms.append(ai_result)
                durations.append(ai_result["duration_s"])
                collect_usage(total_tokens, ai_result["usage"])

            if arms and arms[0].get("skipped"):
                row["ai"] = {**arms[0], "score": score(None, fixture["expect"]), "runs": []}
                print(f"[{index:>2}/{len(fixtures)}] SKIP {fixture['name']:<34} "
                      f"{arms[0]['reason']}")
            else:
                first = arms[0]
                row["ai"] = {**first, "runs": [a["score"] for a in arms]}
                # 稳定性：重复 N 次里"判断对错"是否一致。
                # 同一批输入给出不同结果，说明单次分数本身没有意义 —— 这比分数高低更重要。
                verdicts = {a["score"]["alert_ok"] for a in arms}
                row["ai"]["stable"] = len(verdicts) == 1
                row["ai"]["pass_count"] = sum(1 for a in arms if a["score"]["alert_ok"])
                row["ai"]["repeat"] = len(arms)
                marks = "".join("v" if a["score"]["alert_ok"] else "X" for a in arms)
                print(f"[{index:>2}/{len(fixtures)}] {marks:<8} {fixture['name']:<34} "
                      f"{first['duration_s']:>6.1f}s")
        scenarios.append(row)

    rule_metrics = aggregate([r["rule"]["score"] for r in scenarios])
    ai_metrics = None
    rule_subset_metrics = None
    if bridge is not None:
        # 只在**判断层真正跑过的**场景上算 AI 的指标
        ran = [r for r in scenarios if r["ai"] and not r["ai"].get("skipped")]
        if ran:
            repeat = max(1, args.repeat)
            # 重复跑时，把每一次都当作一个独立样本 —— 这正对应运维上的真实问题：
            # "这一轮告警里有多大比例是误报"，而不是"每个场景平均答得多好"。
            ai_scores = [s for r in ran for s in (r["ai"].get("runs") or [r["ai"]["score"]])]
            ai_metrics = aggregate(ai_scores)
            ai_metrics["tokens"] = total_tokens
            ai_metrics["avg_duration_s"] = (
                round(sum(durations) / len(durations), 1) if durations else 0.0
            )
            # 稳定性：同一批输入重复跑，结论是否一致。
            # 这比分数高低更重要 —— 不一致意味着单次分数本身不可信。
            unstable = [r for r in ran if not r["ai"].get("stable", True)]
            ai_metrics["stability"] = {
                "repeat": repeat,
                "scenarios": len(ran),
                "unstable_count": len(unstable),
                "unstable": [
                    {"name": r["name"], "pass_count": r["ai"]["pass_count"],
                     "repeat": r["ai"]["repeat"]}
                    for r in unstable
                ],
            }
            # 对照组限制在同一子集、同样重复次数：规则层是确定性的（有单测保证），
            # 所以直接复制 N 份即等价于"跑 N 次都得到同一结果"。
            rule_subset_metrics = aggregate([r["rule"]["score"] for r in ran] * repeat)

    run = {
        "schema": "ops/eval@1",
        "started_at": started_at,
        "mode": mode,
        "suite": "golden",
        "repeat": max(1, args.repeat),
        "scenarios": scenarios,
        "metrics": {"rule": rule_metrics, "rule_subset": rule_subset_metrics, "ai": ai_metrics},
    }

    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    stamp = started_at.replace(":", "").replace("-", "")
    json_path = EVAL_DIR / f"{stamp}-{mode}.json"
    md_path = EVAL_DIR / f"{stamp}-{mode}.md"
    json_path.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")

    report = render_markdown(run)
    previous = None
    if args.compare:
        candidates = sorted(EVAL_DIR.glob(f"*-{mode}.json"))
        candidates = [p for p in candidates if p != json_path]
        if candidates:
            previous = json.loads(candidates[-1].read_text(encoding="utf-8"))
            report += "\n" + compare(run, previous)
    md_path.write_text(report, encoding="utf-8")

    print()
    print(render_console("rule(all)", rule_metrics))
    if rule_subset_metrics:
        print()
        print(render_console("rule(same subset as ai)", rule_subset_metrics))
    if ai_metrics:
        print()
        print(render_console("ai", ai_metrics))
    print()
    print(f"报告: {md_path}")
    print(f"数据: {json_path}")
    if mode == "ai":
        print(f"评测产物: data/runs/eval-*.json（可安全删除："
              f"Get-ChildItem data\\runs -Filter 'eval-*' | Remove-Item）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
