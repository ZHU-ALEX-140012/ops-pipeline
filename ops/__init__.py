"""ops-pipeline: 个人质量保障与运维流水线。

设计要点见 DESIGN.md。三条铁律：
1. 确定性的活给脚本，判断性的活给 AI；
2. AI 只能读写文件契约（run.json / verdict.json），不能直接发告警；
3. 执行层不依赖 AI 就能跑通（AI 挂掉时降级为纯规则巡检）。
"""

__version__ = "0.1.0"
