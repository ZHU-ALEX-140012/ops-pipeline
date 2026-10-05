"""多视角诊断：让判断层派子代理从不同镜头分别调查，再汇总。

## 为什么值得这么做

单次判断里，模型要在**同一次思考**中同时回答三个性质不同的问题：

1. 目标自身的行为变了吗？（接口/契约镜头）
2. 日志里有对应证据吗？（日志证据镜头）
3. 这是新问题还是反复出现的？（历史趋势镜头）

它们互相干扰：模型往往抓住最显眼的那条线索就下结论，
而**最显眼的不一定是最相关的** —— 比如一个延迟飙升的错误簇，
真实原因可能在三天前的历史里。

分成独立子代理之后，每个镜头拿到的是**自己需要的那部分事实**，
不会被其它线索带偏；汇总时再要求它**指出哪条结论来自哪个镜头**。

## 代价

**子代理会把 token 消耗放大数倍。** 所以：

* 只在**真的需要多镜头**时才启用（解析型异常、多股证据）；
* 镜头上限硬编码在提示词与配置里（默认 3）；
* 子代理失败/不可用时要**降级为单次判断**，并在 `evidence` 里说明 ——
  不能让"编排没成功"伪装成"我看过了"。

## 与分流的配合

分流的升级条件与这里的启用条件**故意接近**（都看"是否需要权衡证据"）：
需要多镜头的情形，通常也正是需要强模型的情形。
但两者是独立的开关 —— 可以"强模型 + 单镜头"，也可以"便宜模型 + 多镜头"，
用评测分别量它们各自的性价比。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Lens:
    key: str
    title: str
    question: str
    evidence: str


LENSES: tuple[Lens, ...] = (
    Lens(
        key="contract",
        title="接口/契约镜头",
        question="目标自身的行为**本轮是否发生变化**？具体是状态码、响应体指纹、还是延迟？",
        evidence="本轮 samples 与 baseline 里的 p50/p95、modal_body_hash、consecutive_failures",
    ),
    Lens(
        key="logs",
        title="日志证据镜头",
        question="日志里有没有**能对上这次异常的证据**？如果有，量级是多少、是新错误还是老错误？",
        evidence="read-log 取到的原文（可用 --pattern 过滤）；没有对应证据时也要明确说没有",
    ),
    Lens(
        key="history",
        title="历史趋势镜头",
        question="这个现象**是新出现的还是反复出现的**？上次出现时结论是什么？",
        evidence="history 查到的运行/告警记录；必要时用 report 读历史报告",
    ),
)


def perspectives_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return (cfg.get("ai") or {}).get("multi_perspective") or {}


def should_dispatch(cfg: dict[str, Any], analysis: Any, *, task: str = "") -> tuple[bool, str]:
    """这一轮值不值得派子代理。返回 (是否派, 理由)。

    判据与分流的升级条件故意接近：都需要"在几股证据之间权衡"。
    机械型异常（日志源不见了、心跳发现任务没跑）**不启用** ——
    那种情形事实即结论，派三个子代理去确认同一件事纯属烧钱。
    """
    conf = perspectives_cfg(cfg)
    if not conf.get("enabled", False):
        return False, "多视角诊断未启用（ai.multi_perspective.enabled = false）"

    anomalies = list(getattr(analysis, "anomalies", []) or [])
    if not anomalies:
        return False, "没有异常线索，单次判断已足够"

    from . import routing

    kinds = {str(getattr(a, "kind", "")) for a in anomalies}
    interpretive = sorted(kinds & set(conf.get("trigger_kinds", routing.INTERPRETIVE_KINDS)))
    if interpretive:
        return True, f"含解析型异常 {interpretive}，需要多镜头交叉验证"

    min_anomalies = int(conf.get("trigger_min_anomalies", 2))
    if len(anomalies) >= min_anomalies:
        return True, f"异常项 {len(anomalies)} 个（阈值 {min_anomalies}），多股线索需要分别查证"

    return False, f"只有 {len(anomalies)} 个机械型异常（{sorted(kinds) or '-'}），事实即结论"


def context_block(cfg: dict[str, Any], analysis: Any, *, task: str = "") -> dict[str, Any] | None:
    """生成塞进 run_doc 的 `multi_perspective` 块；不该启用时返回 None。

    **为什么放在数据里而不是提示词里**：这样提示词保持静态（同一份提示词
    永远描述同一套契约），而"这次要不要分镜头"由数据决定 ——
    评测也就能靠注入/不注入这个块来跑对照臂。
    """
    enabled, reason = should_dispatch(cfg, analysis, task=task)
    if not enabled:
        return None
    conf = perspectives_cfg(cfg)
    max_sub = int(conf.get("max_subagents", 3))
    chosen = LENSES[:max_sub]
    return {
        "enabled": True,
        "reason": reason,
        "max_subagents": max_sub,
        "lenses": [
            {"key": lens.key, "title": lens.title,
             "question": lens.question, "evidence_hint": lens.evidence}
            for lens in chosen
        ],
        "instructions": [
            f"为下面 {len(chosen)} 个镜头各派一个子代理，一次派完（并行），"
            "每个子代理只回答自己镜头的那个问题",
            "给子代理的提示里要写清：它只需要看哪部分事实、以及它可以用哪些只读命令自证",
            "**不要**让子代理直接给最终结论 —— 它们只回各自的发现与证据",
            "拿到全部发现后由你汇总：先并列三家的结论，再判断它们是否指向同一个根因",
            # ⚠️ 这一条是实测换来的，见 DESIGN 附录 L2：
            # 三个镜头都如实回报"查不到线索"时，模型把置信度从 0.82 降到 0.70
            # 并**撤回了本该发的告警** —— 而多镜头回答的是"为什么"，
            # 不是"是不是问题"。别让"原因不明"吃掉"异常成立"。
            "⚠️ 汇总时必须区分两类不确定：**「原因不明」**和**「是否异常不明」**。"
            "镜头查不到原因，**不改变异常本身是否成立** —— "
            "例如返回结构变了就是变了，查不出谁改的不该让这件事降级。"
            "`severity`/`should_alert` 依据的是**异常本身**，不是你能不能解释它",
            "在 evidence 里标明**每条结论来自哪个镜头**（例如 `[历史镜头] …`）",
            "如果某个子代理失败或没有可用工具，就自己补上那个镜头并注明降级",
        ],
        "degrade_note": (
            "若无法派发子代理（工具不可用/编排失败），**照常按单次判断给结论**，"
            "并在 evidence 里写明「多视角编排未生效，本结论来自单次判断」。"
            "不要把没发生过的编排写进报告。"
        ),
    }
