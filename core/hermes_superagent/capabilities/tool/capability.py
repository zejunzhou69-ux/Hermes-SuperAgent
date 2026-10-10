"""`tool` Capability (design §6.2: list / call) — 包装 ToolRegistry + MCP。"""
from __future__ import annotations

from typing import Any

from .registry import ToolRegistry, build_default_registry
from .mcp import MCPServer


class ToolCapability:
    name = "tool"

    def __init__(self, registry: ToolRegistry | None = None):
        self.registry = registry or build_default_registry()
        self.mcp = MCPServer(self.registry)

    def schema(self) -> dict[str, Any]:
        return {
            "actions": {
                "list": {"params": {}, "returns": "list[tool spec]"},
                "call": {"params": {"name": "str", "args": "dict"},
                         "returns": {"ok": "bool", "data": "any", "error": "dict|None"}},
            }
        }

    async def invoke(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action == "list":
            return {"ok": True, "data": self.registry.list(), "error": None}
        if action == "call":
            return await self.registry.call(params["name"], params.get("args") or {})
        raise ValueError(f"unknown tool action: {action}")

    # MCP 侧
    async def mcp(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self.mcp.handle(request)
