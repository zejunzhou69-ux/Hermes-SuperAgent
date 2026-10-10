"""MCP 化的 browser / crawl 工具（设计方案 §4.3 工具接入：统一 MCP 协议）。

协议：Model Context Protocol（JSON-RPC 2.0 over stdio）。
  - 本骨架自实现最小 MCP 服务端（initialize / tools/list / tools/call），
    不依赖外部 mcp 包 → 可在受限环境运行与单测；
  - 上游（DSH 宿主 / hermes 内核 / 任意 MCP client）通过 stdio 挂载，
    同时暴露内置 browser.* / crawl.* 工具。

工具清单（§6.2）：
  browser.open / goto / click / type / screenshot / frame / attach_cdp / detach
  crawl.fetch / render / extract / sitemap / batch
"""

from __future__ import annotations

import json
import sys
from typing import Any

from hermes_superagent.capabilities.browser import BrowserCapability
from hermes_superagent.capabilities.crawl import CrawlCapability

SERVER_INFO = {"name": "hermes-superagent", "version": "0.1.0"}
PROTOCOL_VERSION = "2024-11-05"

# 工具名 → (capability, action)
_TOOLS: list[dict[str, Any]] = [
    {
        "name": "browser.open",
        "description": "打开 URL（内建 Chromium 或 CDP 外接实例池）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "headless": {"type": "boolean"},
                "cdp_endpoint": {"type": "string", "description": "CDP 外接端点（可选）"},
            },
        },
        "cap": "browser",
        "action": "open",
    },
    {
        "name": "browser.goto",
        "description": "在指定实例中跳转 URL",
        "inputSchema": {
            "type": "object",
            "properties": {"instance_id": {"type": "string"}, "url": {"type": "string"}},
            "required": ["instance_id", "url"],
        },
        "cap": "browser",
        "action": "goto",
    },
    {
        "name": "browser.screenshot",
        "description": "截图存证（jpeg/png/webp，base64 返回）",
        "inputSchema": {
            "type": "object",
            "properties": {"instance_id": {"type": "string"}, "format": {"type": "string"}},
            "required": ["instance_id"],
        },
        "cap": "browser",
        "action": "screenshot",
    },
    {
        "name": "browser.frame",
        "description": "拇指浏览器帧：当前画面缩放为 jpeg/webp（360px 宽）",
        "inputSchema": {
            "type": "object",
            "properties": {"instance_id": {"type": "string"}, "format": {"type": "string"}},
            "required": ["instance_id"],
        },
        "cap": "browser",
        "action": "frame",
    },
    {
        "name": "browser.attach_cdp",
        "description": "接管用户已开的 Chrome（--remote-debugging-port）",
        "inputSchema": {
            "type": "object",
            "properties": {"endpoint": {"type": "string"}},
            "required": ["endpoint"],
        },
        "cap": "browser",
        "action": "attach_cdp",
    },
    {
        "name": "crawl.fetch",
        "description": "分级抓取 URL（L1 静态 → L2 渲染 → L3 伪装；自动升级可配置）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "level": {"type": "string", "enum": ["L1", "L2", "L3", "L4"]},
                "headless": {"type": "boolean"},
                "region": {"type": "string", "enum": ["auto", "cn", "intl"]},
            },
            "required": ["url"],
        },
        "cap": "crawl",
        "action": "fetch",
    },
    {
        "name": "crawl.render",
        "description": "无头渲染并等待指定时间后取 HTML",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}, "wait_ms": {"type": "number"}, "selector": {"type": "string"}},
            "required": ["url"],
        },
        "cap": "crawl",
        "action": "render",
    },
    {
        "name": "crawl.extract",
        "description": "按 CSS 选择器提取元素文本/属性列表",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}, "selector": {"type": "string"}, "attr": {"type": "string"}},
            "required": ["url", "selector"],
        },
        "cap": "crawl",
        "action": "extract",
    },
    {
        "name": "crawl.batch",
        "description": "批量抓取（上限 20 个 URL，受全局限速约束）",
        "inputSchema": {
            "type": "object",
            "properties": {"urls": {"type": "array", "items": {"type": "string"}}},
            "required": ["urls"],
        },
        "cap": "crawl",
        "action": "batch",
    },
]


class MCPServer:
    """最小 MCP stdio 服务端（JSON-RPC 2.0）。"""

    def __init__(self) -> None:
        self._browser = BrowserCapability()
        self._crawl = CrawlCapability(browser=self._browser)
        self._caps = {"browser": self._browser, "crawl": self._crawl}
        self._index = {t["name"]: t for t in _TOOLS}

    # -- JSON-RPC handlers --
    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method")
        mid = msg.get("id")
        if method is None:  # notification
            return None
        params = msg.get("params") or {}
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "serverInfo": SERVER_INFO,
                    "capabilities": {"tools": {}},
                },
            }
        if method == "tools/list":
            tools = [
                {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
                for t in _TOOLS
            ]
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}}
        if method == "tools/call":
            return self._tools_call(mid, params)
        if method == "ping":
            return {"jsonrpc": "2.0", "id": mid, "result": "pong"}
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method: {method}"}}

    def _tools_call(self, mid: Any, params: dict[str, Any]) -> dict[str, Any]:
        tool = self._index.get(params.get("name", ""))
        if tool is None:
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "error": {"code": -32602, "message": f"unknown tool: {params.get('name')}"},
            }
        result = _sync_invoke(self._caps[tool["cap"]], tool["action"], params.get("arguments") or {})
        if result.get("ok"):
            content = [{"type": "text", "text": json.dumps(result["data"], ensure_ascii=False)}]
        else:
            content = [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]
            result = {"content": content, "isError": True}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    async def aclose(self) -> None:
        await self._crawl.aclose()


def _sync_invoke(cap: Any, action: str, params: dict[str, Any]) -> dict[str, Any]:
    """在同步 MCP 事件循环内驱动 async Capability.invoke。

    每次调用新建 loop 并立即关闭，保证池内异步任务不跨 loop 泄漏。
    """
    import asyncio

    async def _run() -> dict[str, Any]:
        return await cap.invoke(action, params)

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run())
    finally:
        loop.close()


def run_mcp_server() -> None:
    """stdio MCP 主循环：每行一个 JSON-RPC 消息。"""
    server = MCPServer()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            out = server.handle(msg)
        if out is not None:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()
