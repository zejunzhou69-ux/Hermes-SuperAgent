"""MCP 接入层 (design §4.3 工具接入).

把 ToolRegistry 暴露为 MCP 协议端点，DSH 侧与 Python 侧都能挂载：
- 进程内 MCP server（stdio / 同进程 handler）供 Python 内核使用
- 外部 MCP server（第三方工具）通过同一 adapter 目录挂载，不写死核心

保持骨架轻量：不依赖 mcp 官方 SDK（避免锁死版本，K4 上游破坏性变更风险），
而是定义与 MCP 对齐的最小 JSON 契约（tools / call），宿主 DSH 侧已有
等价 JSON-RPC 约定（design §6.3），两侧语义一致。
"""
from __future__ import annotations

import json
from typing import Any

from .registry import ToolRegistry


class MCPServer:
    """最小 MCP 服务面：initialize / tools/list / tools/call。

    消息形态与 MCP 的 JSON-RPC 2.0 请求体对齐：
    {"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}
    """

    SERVER_INFO = {"name": "hermes-superagent-mcp", "version": "0.1.0"}

    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    # -- capability: `tool` Capability 的 MCP 侧实现 (design §6.2) ----
    async def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        method = request.get("method", "")
        rid = request.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"serverInfo": self.SERVER_INFO,
                               "capabilities": {"tools": {}}}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"tools": self.registry.to_mcp_tools()}}
        if method == "tools/call":
            params = request.get("params") or {}
            resp = await self.registry.call(params.get("name", ""), params.get("arguments") or {})
            # MCP 约定：工具结果放 content[]
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps(resp, ensure_ascii=False)}],
                "isError": not resp["ok"],
            }}
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": f"unknown method {method!r}"}}

    # -- 便捷包装：Python 内核内进程直调，无需 JSON 编解码 -----------
    def list_tools(self) -> list[dict[str, Any]]:
        return self.registry.to_mcp_tools()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.registry.call(name, arguments)


class MCPClient:
    """挂载外部 MCP server（第三方工具）。

    骨架阶段：`endpoint` 支持两个形态——
    - 进程内: 传入一个 `MCPServer` 实例，直接 handle()
    - 远端:   (未来) HTTP/stdio 到外部 server；契约不变
    外部 server 暴露的工具合入主 registry，标记 third_party=True。
    """

    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    async def attach_process(self, server: MCPServer) -> int:
        result = await server.handle({"id": 0, "method": "tools/list"})
        added = 0
        for tool in result["result"]["tools"]:
            name = tool["name"]
            if self.registry._tools.get(name) is not None:
                continue  # 不覆盖已注册工具
            self.registry.register(
                name,
                description=tool.get("description", ""),
                schema=tool.get("inputSchema", {}),
                third_party=True,
                compliance=tool.get("_meta", {}).get("compliance", "external MCP server"),
            )
            added += 1
        return added


def new_mcp_server() -> MCPServer:
    from .registry import build_default_registry
    return MCPServer(build_default_registry())
