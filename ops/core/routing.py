"""小模型分流：按"这一轮需要多少判断"选模型。

## 为什么可以分流

判断层面对的情形不是同一种难度：

* **机械型**：日志源文件不见了、心跳发现某任务没跑、整轮采到 0 条 ——
  这些事实本身就构成结论，不需要在互相矛盾的证据之间权衡。
* **解释型**：接口返回 200 但内容指纹变了（改了字段还是临时故障？）、
  延迟涨到 6 倍但只有单样本（趋势还是抖动？）、
  一个新错误模板（真的是新问题还是已知噪声换了个形状？）——
  这些要在几股证据之间做取舍。

**便宜模型不是缩水版，它在明确的情形下够用；**
强模型的价值集中在"证据互相矛盾、需要权衡"的时候。

## 这个模块只做决策，不做度量

分流好不好用**必须靠评测回答**（`tools/eval_suite.py --model` 分别跑强弱模型，
再用自动分流跑一遍对比）。这里的启发式是**可配置的先验**，不是结论 ——
配置项存在的意义就是让人能拿评测去调它。

## 一条重要的诚实声明

分流的收益是**成本**，代价是**潜在准确率**。所以：
* 升级条件宁可保守（拿不准就升级到强模型）；
* 每一轮的模型选择都记进 `run` 表（`ai_model`），事后能回答"这次用了哪个、为什么"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 需要"解释"而不只是"报事实"的异常类型。
# 这些一旦出现就升级到强模型：它们要求模型在几股证据之间做取舍。
#   structure_change   —— 200 但内容变了：契约变更还是临时故障？
#   latency_regression —— 变慢了：趋势还是抖动？样本够不够？
#   new_error_template —— 新错误：真新问题，还是已知噪声换了形状？
#   count_spike        —— 量级激增：真实放大，还是对比窗口偏低造成的假象？
#   parse_ratio_low    —— 解析率掉：格式变了还是文件被截断？
INTERPRETIVE_KINDS = (
    "structure_change",
    "latency_regression",
    "new_error_template",
    "count_spike",
    "parse_ratio_low",
)

# 机械型异常：事实即结论。出现这些**不足以**升级。
MECHANICAL_KINDS = (
    "empty_round",
    "source_missing",
    "source_empty",
    "log_stale",
    "stale_run",
    "consecutive_failure",
)

CHEAP, STRONG = "cheap", "strong"


@dataclass
class ModelChoice:
    model: str
    tier: str          # cheap | strong | pinned
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"model": self.model, "tier": self.tier, "reason": self.reason}


def routing_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return (cfg.get("ai") or {}).get("routing") or {}


def decide_model(cfg: dict[str, Any], analysis: Any, *, task: str = "") -> ModelChoice:
    """选模型。`analysis` 是 judge.Analysis（有 .anomalies 与 .metrics）。

    `ai.model` 显式设成某个模型名时**优先于分流** —— 那是给评测和调试用的开关
    （评测要能分别跑"全是便宜模型"和"全是强模型"）。
    """
    ai = cfg.get("ai") or {}
    pinned = str(ai.get("model") or "").strip()
    if pinned and pinned != "auto":
        return ModelChoice(pinned, "pinned", f"配置里写死了 ai.model = {pinned}")

    conf = routing_cfg(cfg)
    cheap = str(conf.get("cheap_model", "deepseek-flash"))
    strong = str(conf.get("strong_model", "deepseek-v4-pro"))

    if not conf.get("enabled", True):
        return ModelChoice(cheap, CHEAP, "分流已关闭（ai.routing.enabled = false）")

    anomalies = list(getattr(analysis, "anomalies", []) or [])
    metrics = getattr(analysis, "metrics", {}) or {}

    # ---------------------------------------------------------- 升级条件
    kinds = {str(getattr(a, "kind", "")) for a in anomalies}
    interpretive = sorted(kinds & set(conf.get("escalate_kinds", INTERPRETIVE_KINDS)))
    if interpretive:
        return ModelChoice(
            strong, STRONG,
            f"含需要解释的异常类型 {interpretive}，证据要权衡，用强模型",
        )

    min_anomalies = int(conf.get("escalate_min_anomalies", 2))
    if len(anomalies) >= min_anomalies:
        return ModelChoice(
            strong, STRONG,
            f"异常项 {len(anomalies)} 个（阈值 {min_anomalies}），多股证据要合并，用强模型",
        )

    min_failed = int(conf.get("escalate_min_failed", 2))
    failed = int(metrics.get("failed") or 0)
    if failed >= min_failed:
        return ModelChoice(
            strong, STRONG,
            f"失败目标 {failed} 个（阈值 {min_failed}），可能同源也可能各自独立，用强模型",
        )

    # 每轮都要分析的综合类任务（例如日报）：句子里没有"异常"，但负担在汇总上
    always_strong = set(conf.get("always_strong_tasks") or [])
    if task and task in always_strong:
        return ModelChoice(strong, STRONG, f"{task} 属于 always_strong_tasks，用强模型")

    # ---------------------------------------------------------- 否则走便宜模型
    if not anomalies:
        why = "没有任何异常线索，规则层已足够"
    else:
        mechanical = sorted(kinds) or ["-"]
        why = f"只有 {len(anomalies)} 个机械型异常 {mechanical}，事实即结论"
    return ModelChoice(cheap, CHEAP, why)


# ---------------------------------------------------------------- patch 生成

MODEL_PATCH_TEMPLATE = """\
# 由 ops-pipeline 自动生成：把判断层的模型钉到 {model}。
#
# 为什么用 patch 而不是命令行开关：`dsh headless` 没有 --model，
# 而 patch 层是可组合的 —— 它在 `ai.patch`（负责 provider 与凭据）之后再覆盖模型，
# 两者互不干扰。
- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"
  config:
    provider: {provider}
    model: {model}
"""


def model_patch_path(cfg: dict[str, Any], model: str):
    """每个模型一个固定的 patch 文件，写在状态目录里（不进版本库）。

    **纯路径计算，不建目录** —— 建目录放在 `ensure_model_patch` 的 try 里。
    否则"状态目录建不出来"会在这里抛异常，把整次判断打断，
    而正确答案是"退化为不做分流"。
    """
    from . import config as cfgmod

    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in model)
    return cfgmod.data_dir(cfg, "state") / f"model-{safe}.yml"


def ensure_model_patch(cfg: dict[str, Any], model: str):
    """写好（或复用）该模型的 patch 文件，返回路径；写不进去返回 None。

    内容一致的已存在文件不重写 —— 计划任务每小时跑一次，
    每次都改文件没有意义，还会让"谁动过这个文件"变得难以回答。
    """
    from . import config as cfgmod

    ai = cfg.get("ai") or {}
    provider = str(ai.get("provider") or "deepseek-account")
    text = MODEL_PATCH_TEMPLATE.format(provider=provider, model=model)
    try:
        path = model_patch_path(cfg, model)
        cfgmod.ensure_dir(path.parent)
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
    except OSError:
        # 写不进去就退化为"不做分流"：没有 patch 就用 profile 默认模型，
        # 这一步失败**绝不能**让判断层整体失败。
        return None
    return path
