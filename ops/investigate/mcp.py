"""MCP 服务端：把只读调查工具按 Model Context Protocol 暴露出去。

## 为什么手写协议而不是装 SDK

项目的一条底线是**零第三方依赖**（全部标准库）。MCP 的 stdio 传输就是
"一行一个 JSON-RPC 2.0 消息"，自己实现一遍既符合这条底线，
也顺带证明**读过协议**而不是"装了个包会调 API"。

## 为什么现在还没接进流水线

`dsh headless` 的 profile 里**没有 MCP 客户端的配置面** —— 我用
`dsh --profile headless --dump-config` 逐项查过，只有 `dsh-mcp-resources`
（MCP *资源*，不是服务器连接）。MCP 服务器是通过 ACP 的 `session/new`
由**客户端**传 `mcpServers` 挂上的（见 asar 里的 `SessionBuilder.withMcpServer`），
而 `dsh headless` 自己就是那个客户端，命令行没有暴露这个入口。

所以现状是：

* **这个 MCP 服务端**：协议完整、可被任何 MCP 客户端使用（自带测试客户端验证），
  Claude Desktop / Cursor / DSH 的 web 与 tui 都能挂；
* **流水线实际走的是同源的 CLI**（`tools/investigate.py`）——
  判断层用它已经有的 `pwsh` 工具调用，边界控制完全一致。

两者共用 `ops/investigate/tools.py` 里同一份实现，所以不存在"两套逻辑各自演化"。
"""

from __future__ import annotations

import json
import sys
from typing import Any, Iterable

from ..core import config as cfgmod
from . import budget, tools as toolmod

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "ops-investigate"
SERVER_VERSION = "1.0.0"

# 标准的 JSON-RPC 错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _ok(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle_message(cfg: dict[str, Any], message: dict[str, Any]) -> dict[str, Any] | None:
    """处理一条 JSON-RPC 消息。返回 None 表示这是通知，不该有响应。"""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _err(message.get("id") if isinstance(message, dict) else None,
                    INVALID_REQUEST, "不是合法的 JSON-RPC 2.0 消息")

    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    # 通知（没有 id）：不需要响应
    if msg_id is None and isinstance(method, str) and method.startswith("notifications/"):
        return None

    if method == "initialize":
        return _ok(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method == "ping":
        return _ok(msg_id, {})

    if method == "tools/list":
        return _ok(msg_id, {"tools": [
            {"name": name, **spec} for name, spec in sorted(toolmod.TOOL_SPECS.items())
        ]})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str):
            return _err(msg_id, INVALID_PARAMS, "tools/call 需要 name")
        if not isinstance(arguments, dict):
            return _err(msg_id, INVALID_PARAMS, "arguments 必须是对象")

        verdict = budget.check(cfg)
        if not verdict.allowed:
            return _ok(msg_id, {
                "content": [{"type": "text", "text": verdict.reason}],
                "isError": True,
            })

        result = toolmod.dispatch(cfg, name, arguments)
        record = budget.record(cfg, name, arguments, result)
        text = toolmod.truncate(result.text, toolmod.max_output_bytes(cfg))
        if not record.written:
            # 预算失效必须让模型和调用方都看见，不能悄悄继续
            text += f"\n\n⚠️ {record.detail} —— 本轮工具预算未生效。"
        return _ok(msg_id, {
            "content": [{"type": "text", "text": text}],
            "isError": not result.ok,
        })

    if msg_id is None:
        return None
    return _err(msg_id, METHOD_NOT_FOUND, f"未实现的方法：{method}")


def serve(cfg: dict[str, Any], lines: Iterable[str], out: Any = None) -> int:
    """stdio 传输：一行一条 JSON-RPC。返回处理过的请求数（供测试断言）。"""
    stream = out if out is not None else sys.stdout
    handled = 0
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            stream.write(json.dumps(_err(None, PARSE_ERROR, "JSON 解析失败")) + "\n")
            stream.flush()
            continue
        response = handle_message(cfg, message)
        if response is None:
            continue
        handled += 1
        stream.write(json.dumps(response, ensure_ascii=False) + "\n")
        stream.flush()
    return handled


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cfg_path = argv[0] if argv else "config/config.toml"
    cfg = cfgmod.load_config(cfg_path)
    serve(cfg, sys.stdin)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
