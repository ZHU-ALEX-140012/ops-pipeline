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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ops.core import aibridge, config as cfgmod, judge, perspectives, routing  # noqa: E402
from ops.investigate import tools as investigate  # noqa: E402

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


def run_ai_arm(fixture: dict[str, Any], cfg: dict[str, Any], bridge: aibridge.AIBridge,
               *, with_tools: bool = False, with_perspectives: bool = False,
               model: str | None = None) -> dict[str, Any]:
    """跑一次判断层。

    `with_tools=True` 时把**活的**工具面（白名单、预算、可查的 run_id）注入输入 ——
    和 `pipeline.finalize` 在生产里做的事完全一致。
    `model="route"` 表示"按分流规则自动选模型"，其余值直接钉死模型。

    ⚠️ **带工具那一臂不是完全 hermetic 的**：工具面来自当时的配置与数据库，
    工具**结果**（日志内容、历史告警）也都是活的。这是刻意的 ——
    要衡量"生产里加上工具会怎样"，就不能把环境冻住。
    代价是这一臂的分数不能像无工具臂那样逐位复现。
    报告里必须写明这一点，否则对比就成了误导。
    """
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
    if with_tools:
        doc["investigation"] = investigate.tool_context(cfg, run_id)
    if with_perspectives:
        # 与生产一致：只有该启用的场景才注入这个块（否则就是给每道题都加提示）
        analysis = analysis_from_doc(doc)
        block = perspectives.context_block(cfg, analysis, task=doc["task"])
        if block:
            doc["multi_perspective"] = block

    chosen = model
    tier = ""
    if model == "route":
        # 与生产一致：按规则层信号强度选模型。直接喂冻结夹具里的事实。
        analysis = analysis_from_doc(doc)
        choice = routing.decide_model(cfg, analysis, task=doc["task"])
        chosen, tier = choice.model, choice.tier

    result = bridge.call(
        task=doc["task"],
        run_id=run_id,
        template_path=template,
        run_doc=doc,
        # 评测绝不能用生产会话：否则一次评测就把日报的"记忆"污染了
        use_session=False,
        model=chosen,
    )
    calls = result.tool_calls or {}
    return {
        "verdict": result.verdict,
        "duration_s": result.duration_s,
        "usage": result.usage.to_dict(),
        "error": result.error,
        "run_id": run_id,
        "skipped": False,
        "with_tools": with_tools,
        "with_perspectives": with_perspectives,
        "model": chosen or "（profile 默认）",
        "tier": tier,
        # 宿主侧计数：不依赖子进程写状态文件，所以任何环境都准
        "tool_calls": calls.get("investigate", 0),
        "tool_commands": calls.get("commands", []),
    }


@dataclass
class ArmSpec:
    """一臂的配置。**配对比较的前提是各臂除被考察的那个变量以外完全一致。**"""

    key: str          # 结果 JSON 里的键，也是指标表的列键
    label: str        # 给人看的短标签
    model: str | None  # None = profile 默认；"route" = 自动分流；其余为具体模型
    with_tools: bool = False
    with_perspectives: bool = False


def _slug(model: str | None) -> str:
    if not model:
        return "ai"
    if model == "route":
        return "route"
    return model.replace("deepseek-", "").replace(".", "")


def _arm_label(model: str | None) -> str:
    return {"route": "自动分流", None: "profile 默认"}.get(model, model or "")


def _arm_key(model: str | None, *, tools: bool, perspectives: bool) -> str:
    key = _slug(model)
    if tools:
        key += "_tools"
    if perspectives:
        key += "_persp"
    return key


def build_arms(models: list[str | None], *, with_tools: bool = False,
               with_perspectives: bool = False) -> list[ArmSpec]:
    """把命令行给的开关展开成臂。

    `--model flash --model route --tools` → 4 臂（各自再加一个带工具的版本）。
    `--model` 缺省时是 `[None]`（profile 默认），此时臂键沿用历史的 `ai` / `ai_tools`，
    这样早期的评测结果仍能直接对比。

    带工具与多视角是**两个独立的开关**，各自给每个模型臂加一个变体 ——
    它们衡量的是不同的问题（"能不能自证"vs"要不要分镜头"），不该互相混同。
    """
    arms: list[ArmSpec] = []
    variants = [(False, False)]
    if with_tools:
        variants.append((True, False))
    if with_perspectives:
        variants.append((False, True))
    for model in models:
        for tools, persp in variants:
            label = _arm_label(model)
            if tools:
                label += " + 工具"
            if persp:
                label += " + 多视角"
            arms.append(ArmSpec(key=_arm_key(model, tools=tools, perspectives=persp),
                                label=label, model=model, with_tools=tools,
                                with_perspectives=persp))
    return arms


def run_arm_batch(fixture: dict[str, Any], cfg: dict[str, Any], bridge: aibridge.AIBridge,
                  *, arm: ArmSpec, repeat: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """跑一臂（可重复 N 次），返回 (汇总后的臂数据, 各次原始结果)。"""
    expect = fixture["expect"]
    runs: list[dict[str, Any]] = []
    for _ in range(max(1, repeat)):
        result = run_ai_arm(fixture, cfg, bridge, with_tools=arm.with_tools,
                            with_perspectives=arm.with_perspectives, model=arm.model)
        if result.get("skipped"):
            return {"skipped": True, "reason": result["reason"],
                    "score": score(None, expect), "runs": []}, []
        result["score"] = score(result["verdict"], expect, result.get("error"))
        runs.append(result)

    first = runs[0]
    verdicts = {a["score"]["alert_ok"] for a in runs}
    return {
        **first,
        "runs": [a["score"] for a in runs],
        # 稳定性：重复 N 次里"判断对错"是否一致。不一致意味着单次分数本身不可信。
        "stable": len(verdicts) == 1,
        "pass_count": sum(1 for a in runs if a["score"]["alert_ok"]),
        "repeat": len(runs),
    }, runs


def arm_metrics(scenarios: list[dict[str, Any]], arm: str, repeat: int,
                tokens: dict[str, int], durations: list[float]) -> dict[str, Any] | None:
    """算某个 AI 臂的指标。**对照臂必须限制在同一子集**，否则两边 n 不同，差值是假的。"""
    ran = [r for r in scenarios if r.get(arm) and not r[arm].get("skipped")]
    if not ran:
        return None
    scores = [s for r in ran for s in (r[arm].get("runs") or [r[arm]["score"]])]
    metrics = aggregate(scores)
    metrics["tokens"] = tokens
    metrics["avg_duration_s"] = round(sum(durations) / len(durations), 1) if durations else 0.0
    metrics["tool_calls"] = sum(int(r[arm].get("tool_calls") or 0) for r in ran)
    metrics["scenarios_with_tools"] = sum(1 for r in ran if int(r[arm].get("tool_calls") or 0))
    metrics["repeat"] = repeat
    unstable = [r for r in ran if not r[arm].get("stable", True)]
    metrics["stability"] = {
        "repeat": repeat,
        "scenarios": len(ran),
        "unstable_count": len(unstable),
        "unstable": [{"name": r["name"], "pass_count": r[arm]["pass_count"],
                      "repeat": r[arm]["repeat"]} for r in unstable],
    }
    return metrics


def rule_subset(scenarios: list[dict[str, Any]], arm: str, repeat: int) -> dict[str, Any] | None:
    """与某个 AI 臂**同子集、同重复次数**的规则层分数。

    规则层是确定性的（有单测保证），所以直接复制 N 份等价于"跑 N 次都得到同一结果"。
    """
    ran = [r for r in scenarios if r.get(arm) and not r[arm].get("skipped")]
    if not ran:
        return None
    return aggregate([r["rule"]["score"] for r in ran] * max(1, repeat))


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
    metrics = run["metrics"]
    all_rule = metrics["rule"]
    arms = run.get("arms") or []
    arm_metrics_map = metrics.get("arms") or {}
    subset_map = metrics.get("rule_subsets") or {}

    active = [a for a in arms if arm_metrics_map.get(a["key"])]
    # 基准臂：第一臂。其余臂的差值都是"相对它"。
    base = active[0] if active else None
    base_subset = subset_map.get(base["key"]) if base else all_rule
    # 用"臂存在"而不是"臂有指标"来判断跳过：**全部场景都被跳过**时
    # （例如只跑 --only heartbeat）指标是空的，但"为什么跳过"必须照旧报出来。
    probe_key = active[0]["key"] if active else (arms[0]["key"] if arms else None)
    skipped = [r for r in run["scenarios"]
               if probe_key and r.get(probe_key) and r[probe_key].get("skipped")]
    has_tools_arm = any(a.get("with_tools") for a in active) or any(
        a.get("with_tools") for a in arms)

    lines = [
        "# 判断层评测报告",
        "",
        f"- 时间：{run['started_at']}",
        f"- 黄金集：{run['suite']}，共 {all_rule['n']} 个场景",
        f"- 重复次数：{run.get('repeat', 1)}",
    ]
    if active:
        lines.append("- 本次的臂：" + "；".join(
            f"`{a['key']}` = {a['label']}" + ("（带工具）" if a.get("with_tools") else "")
            for a in active))
        n = (base_subset or all_rule)["n"]
        lines.append(
            f"- 判断层实际覆盖：**{n} / {all_rule['n']}** 个场景"
            + (f"（跳过 {len(skipped)} 个：判断层在这些任务上不使用）" if skipped else "")
        )

    # ---------------------------------------------------------------- 总分
    rule_col = f"规则层（n={(base_subset or all_rule)['n']}）"
    head = f"| 指标 | {rule_col} | " + " | ".join(a["label"] for a in active) + " |"
    lines += ["", "## 总分", "", head, "|" + "---|" * (2 + len(active))]

    def value(arm: dict[str, Any] | None, key: str, scope: str | None) -> Any:
        if not arm:
            return None
        return (arm if scope == "top" else arm["alert"]).get(key)

    rows = [
        ("should_alert 精确率（不误报）", "precision", None),
        ("should_alert 召回率（不漏报）", "recall", None),
        ("should_alert F1", "f1", None),
        ("should_alert 准确率", "accuracy", None),
        ("严重级别准确率", "severity_accuracy", "top"),
        ("根因关键词命中率", "root_accuracy", "top"),
    ]
    for label, key, scope in rows:
        cells = [label, _fmt(value(base_subset, key, scope))]
        for spec in active:
            cells.append(_fmt(value(arm_metrics_map.get(spec["key"]), key, scope)))
        lines.append("| " + " | ".join(cells) + " |")

    lines += [
        "",
        f"规则层在**全部 {all_rule['n']} 个**场景上的精确率 "
        f"{_fmt(all_rule['alert']['precision'])} / 召回率 {_fmt(all_rule['alert']['recall'])}"
        f"（含判断层不覆盖的任务）。",
    ]

    # ---------------------------------------------------------------- 相对基准臂的差值
    if len(active) > 1:
        lines += ["", "### 相对基准臂的差值", "",
                  f"基准是 `{base['key']}`（{base['label']}）。"
                  f"**每臂的对照都限制在它自己的子集上**，否则 n 不同，差值是假的。", "",
                  "| 指标 | " + " | ".join(f"{a['label']} − 基准" for a in active[1:]) + " |",
                  "|" + "---|" * len(active)]
        for label, key, scope in rows:
            cells = [label]
            for spec in active[1:]:
                a = value(arm_metrics_map.get(spec["key"]), key, scope)
                b = value(arm_metrics_map.get(base["key"]), key, scope)
                if a is None or b is None:
                    cells.append("n/a")
                else:
                    delta = (a - b) * 100
                    cells.append(f"{'+' if delta >= 0 else ''}{delta:.1f} pp")
            lines.append("| " + " | ".join(cells) + " |")

    # ---------------------------------------------------------------- 成本
    cost_rows = []
    for spec in active:
        m = arm_metrics_map.get(spec["key"]) or {}
        t = m.get("tokens") or {}
        if not (t.get("in") or t.get("out")):
            continue
        cost_rows.append(
            f"| {spec['label']} | {t.get('in', 0)} | {t.get('out', 0)} | "
            f"{t.get('cache_read', 0)} | {m.get('avg_duration_s', 0)}s | "
            f"{m.get('tool_calls', 0)} |"
        )
    if cost_rows:
        lines += ["", "## 成本", "",
                  "| 臂 | 输入 tokens | 输出 tokens | cache 读 | 平均耗时 | 工具调用 |",
                  "|---|---|---|---|---|---|", *cost_rows, "",
                  "> ⚠️ cache 读的单价远低于普通输入，这里只报用量，不折算成钱。"]

    if has_tools_arm:
        lines += [
            "",
            "## ⚠️ 关于带工具臂的公平性",
            "",
            "不带工具的臂用的是**冻结夹具**，逐位可复现；**带工具臂不是完全 hermetic 的** ——",
            "它的工具面来自当时的配置与数据库，工具**返回的内容**（日志原文、历史告警）",
            "也是活的。这是刻意的：要衡量「生产里加上工具会怎样」就不能把环境冻住。",
            "代价是这一臂的分数不能像无工具臂那样逐位复现，"
            "**跨运行比较它时要按「有噪声」对待**。",
            "",
            "另一处刻意的不对称：带工具臂的输入里多了一个 `investigation` 字段。",
            "这不是作弊 —— 生产路径（`pipeline.finalize`）就是这么注入的。",
        ]

    if any(a.get("model") == "route" for a in active):
        lines += [
            "",
            "## 关于「自动分流」臂",
            "",
            "它按 `[ai.routing]` 的规则逐场景选模型：机械型异常走便宜模型，",
            "需要权衡证据的升级到强模型。**分流是成本优化，不是准确率优化** ——",
            "所以关键不是它比强模型准，而是**它比强模型便宜多少、又比便宜模型准多少**。",
        ]

    stability = (arm_metrics_map.get(base["key"]) if base else None) or {}
    stability = stability.get("stability")
    if stability and stability["repeat"] > 1:
        lines += [
            "", "## 稳定性（同一批输入重复跑）", "",
            f"重复次数 **{stability['repeat']}**，场景数 {stability['scenarios']}，"
            f"其中 **{stability['unstable_count']}** 个场景在重复之间给出了**不一致**的结论。",
            "",
        ]
        if stability["unstable"]:
            lines += ["| 场景 | 答对次数 | 重复次数 |", "|---|---|---|"]
            for item in stability["unstable"]:
                lines.append(f"| {item['name']} | {item['pass_count']} | {item['repeat']} |")
            lines += ["", "> ⚠️ **单次跑分本身不可靠**，要看多次重复的均值。"]
        else:
            lines.append("所有场景在重复之间结论一致。")

    # ---------------------------------------------------------------- 逐场景
    header = "| 场景 | 任务 | 期望 | 规则层 | " + " | ".join(a["label"] for a in active)
    if has_tools_arm:
        header += " | 工具调用"
    header += " | 说明 |"
    lines += ["", "## 逐场景", "", header, "|" + "---|" * (5 + len(active) + (1 if has_tools_arm else 0))]

    def arm_cell(arm: dict[str, Any] | None) -> str:
        if not arm:
            return "未跑"
        if arm.get("skipped"):
            return "不适用"
        s = arm["score"]
        if s["predicted"] is None:
            return "失败 ✗"
        return ("告警" if s["predicted"] else "静默") + (" ✓" if s["alert_ok"] else " ✗")

    for row in run["scenarios"]:
        rule_cell = "—"
        if row["rule"]["score"]["predicted"] is not None:
            rule_cell = "告警" if row["rule"]["score"]["predicted"] else "静默"
            if not row["rule"]["score"]["alert_ok"]:
                rule_cell += " ✗"
        cells = [row["name"], row["task"],
                 "告警" if row["expect"]["should_alert"] else "静默", rule_cell]
        cells += [arm_cell(row.get(a["key"])) for a in active]
        if has_tools_arm:
            used = sum(int((row.get(a["key"]) or {}).get("tool_calls") or 0)
                       for a in active if a.get("with_tools"))
            cells.append(str(used))
        cells.append(row["note"])
        lines.append("| " + " | ".join(cells) + " |")

    if skipped and probe_key:
        lines += ["", "### 判断层不适用的场景", ""]
        for row in skipped:
            lines.append(f"- `{row['name']}`（{row['task']}）：{row[probe_key]['reason']}")

    n_arm = (base_subset or all_rule)["n"]
    lines += ["", "## 已知局限", "",
              "- `root_accuracy` 是**关键词命中**的近似，不是语义评分。它便宜、确定、可解释，"
              "适合做回归检测；真正的语义质量需要 LLM-as-judge 或人工标注。",
              f"- 黄金集只有 {all_rule['n']} 个场景，单个场景翻转就会让百分比跳动约 "
              f"{100 / max(n_arm, 1):.0f}pp，不要拿小数点后的差异当结论。",
              "- 「规则层误报、正确答案不告警」的题型目前只有 1 个。"
              "这类题最能体现判断层的独立价值，应该继续补。",
              "- 对照臂只在各臂自己的子集上计算 —— 否则两边 n 不同，差值是假的。",
              "- 多个臂在**同一次运行**里跑（配对），所以臂间差异不叠加上跨运行的波动；"
              "但每臂仍是单次采样，重复跑才能谈稳定性。",
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
    # 必须在任何打印之前：臂标签与报告里有中文，
    # 中文 Windows 控制台是 GBK，不先安全化就可能抛 UnicodeEncodeError。
    from ops.core import make_console_safe

    make_console_safe()

    parser = argparse.ArgumentParser(description="用黄金集评测判断层")
    parser.add_argument("--rule-only", action="store_true", help="只跑规则层基线（免费）")
    parser.add_argument("--only", help="只跑名字包含该子串的场景")
    parser.add_argument("--limit", type=int, help="最多跑几个场景（控成本）")
    parser.add_argument("--compare", action="store_true", help="与上一次同模式结果对比")
    parser.add_argument("--tools", action="store_true",
                        help="配对模式：每个模型臂再加一个「带只读工具」的版本")
    parser.add_argument("--perspectives", action="store_true",
                        help="配对模式：每个模型臂再加一个「多视角诊断」的版本")
    parser.add_argument("--model", action="append", default=None, metavar="NAME",
                        help="钉死模型（可重复以并列多个臂）；特殊值 route = 按分流规则自动选。"
                             "缺省则用 profile 默认模型")
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
    if args.perspectives:
        # ⚠️ 必须**强制打开**：配置里默认是 false（它把 token 消耗放大数倍，
        # 不该默认开）。不打开的话 `--perspectives` 臂根本不会注入那个块 ——
        # 于是这一臂是空转的，对比毫无意义（第一版就是这样，白跑了一轮）。
        cfg.setdefault("ai", {}).setdefault("multi_perspective", {})["enabled"] = True

    arms_spec = build_arms(args.model if args.model else [None],
                           with_tools=args.tools, with_perspectives=args.perspectives)
    if args.rule_only:
        arms_spec = []
    # 模式名带上臂配置：`--compare` 只会和**配置相同**的上一次结果比，
    # 否则"flash 比 v4-pro"会被误当成"同一配置的两次运行"。
    mode = "rule" if args.rule_only else ("+".join(a.key for a in arms_spec) or "ai")
    started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    bridge = None
    if not args.rule_only:
        if not (cfg.get("ai") or {}).get("enabled", True):
            print("[FAIL] 配置里 ai.enabled = false，无法跑 AI 评测")
            return 1
        bridge = aibridge.AIBridge(cfg)

    scenarios: list[dict[str, Any]] = []
    tokens: dict[str, dict[str, int]] = {}
    durations: dict[str, list[float]] = {}
    for spec in arms_spec:
        tokens[spec.key] = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
        durations[spec.key] = []
    repeat = max(1, args.repeat)

    if arms_spec:
        print("本次运行的臂：" + "；".join(
            f"{a.key}={a.label}" + ("（带工具）" if a.with_tools else "") for a in arms_spec))
        print()

    for index, fixture in enumerate(fixtures, 1):
        rule_result = run_rule_arm(fixture)
        row: dict[str, Any] = {
            "name": fixture["name"], "task": fixture["task"], "note": fixture["note"],
            "expect": fixture["expect"],
            "rule": {**rule_result, "score": score(rule_result["verdict"], fixture["expect"])},
        }
        for spec in arms_spec:
            row[spec.key] = None

        if bridge is not None:
            for spec in arms_spec:
                arm, runs = run_arm_batch(fixture, cfg, bridge, arm=spec, repeat=repeat)
                row[spec.key] = arm
                for item in runs:
                    durations[spec.key].append(item["duration_s"])
                    collect_usage(tokens[spec.key], item["usage"])
                if arm.get("skipped"):
                    print(f"[{index:>2}/{len(fixtures)}] SKIP {fixture['name']:<34} {arm['reason']}")
                    continue
                marks = "".join("v" if a["score"]["alert_ok"] else "X" for a in runs)
                extra = ""
                if arm.get("model"):
                    extra = f"  模型 {arm['model']}"
                    if arm.get("tier"):
                        extra += f"（{arm['tier']}）"
                if arm.get("tool_calls"):
                    extra += f"  工具 {arm['tool_calls']} 次"
                print(f"[{index:>2}/{len(fixtures)}] {marks:<6} {spec.label:<14} "
                      f"{fixture['name']:<34} {arm['duration_s']:>6.1f}s{extra}")
        scenarios.append(row)

    rule_metrics = aggregate([r["rule"]["score"] for r in scenarios])
    # 每个臂各自算指标；对照臂（规则层）限制在**该臂的同一子集与同样重复次数**上，
    # 否则两边 n 不同，差值就是假的。
    arm_metrics_map: dict[str, dict[str, Any] | None] = {}
    subset_metrics_map: dict[str, dict[str, Any] | None] = {}
    for spec in arms_spec:
        arm_metrics_map[spec.key] = arm_metrics(scenarios, spec.key, repeat,
                                                tokens[spec.key], durations[spec.key])
        subset_metrics_map[spec.key] = rule_subset(scenarios, spec.key, repeat)

    run = {
        "schema": "ops/eval@1",
        "started_at": started_at,
        "mode": mode,
        "suite": "golden",
        "repeat": repeat,
        "arms": [{"key": a.key, "label": a.label, "model": a.model,
                  "with_tools": a.with_tools} for a in arms_spec],
        "scenarios": scenarios,
        "metrics": {
            "rule": rule_metrics,
            "rule_subset": subset_metrics_map.get(arms_spec[0].key) if arms_spec else None,
            "arms": arm_metrics_map,
            "rule_subsets": subset_metrics_map,
        },
    }

    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    stamp = started_at.replace(":", "").replace("-", "")
    safe_mode = mode.replace("+", "_")
    json_path = EVAL_DIR / f"{stamp}-{safe_mode}.json"
    md_path = EVAL_DIR / f"{stamp}-{safe_mode}.md"
    json_path.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")

    report = render_markdown(run)
    previous = None
    if args.compare:
        candidates = sorted(EVAL_DIR.glob(f"*-{safe_mode}.json"))
        candidates = [p for p in candidates if p != json_path]
        if candidates:
            previous = json.loads(candidates[-1].read_text(encoding="utf-8"))
            report += "\n" + compare(run, previous)
    md_path.write_text(report, encoding="utf-8")

    print()
    print(render_console("rule(all)", rule_metrics))
    for spec in arms_spec:
        metrics = arm_metrics_map.get(spec.key)
        if metrics:
            print()
            print(render_console(spec.label, metrics))
    print()
    print(f"报告: {md_path}")
    print(f"数据: {json_path}")
    if mode != "rule":
        print("评测产物: data/runs/eval-*.json（可安全删除："
              "Get-ChildItem data\\runs -Filter 'eval-*' | Remove-Item）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
