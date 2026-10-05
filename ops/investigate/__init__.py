"""判断层的只读调查能力。

* `tools.py`   四个工具的**唯一实现**（白名单 + 只读 + 截断）
* `budget.py`  每次运行的调用预算 + 审计留痕
* `mcp.py`     MCP 服务端（stdio JSON-RPC 2.0，纯标准库）

MCP 服务端与 CLI（`tools/investigate.py`）**共用 `tools.py` 这一份实现** ——
所以不存在"两条通道各自演化、行为不一致"的问题。
"""
