"""`tool` capability package: 工具注册表 + MCP 接入 (design §4.3)."""
from .capability import ToolCapability
from .mcp import MCPServer, MCPClient
from .registry import (
    BUILTIN_NAMESPACES,
    REJECTED_TOOLS,
    THIRD_PARTY_ADAPTERS,
    ToolRegistry,
    ToolSpec,
    build_default_registry,
)

__all__ = [
    "BUILTIN_NAMESPACES",
    "MCPServer",
    "MCPClient",
    "REJECTED_TOOLS",
    "THIRD_PARTY_ADAPTERS",
    "ToolCapability",
    "ToolRegistry",
    "ToolSpec",
    "build_default_registry",
]
