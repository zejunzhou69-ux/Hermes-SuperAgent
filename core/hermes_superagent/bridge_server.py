"""Bridge JSON-RPC + SSE 服务端（t5，契约 §2/§3/§4/§5）。

传输：aiohttp，仅监听 127.0.0.1。
路由：
- POST /rpc      JSON-RPC 2.0（契约 §3 方法表）
- GET  /events   SSE（契约 §4 事件表）
- GET  /frames   SSE（契约 §5 拇指浏览器帧流；由 t2 thumb 服务供给，此处转发/占位）

鉴权：Authorization: Bearer <token>；token 不落盘日志（K7）。
内核不可用能力时（如 thumb 未装 playwright）帧流端点显式返回 503 + reason，
不静默（§9B.4.2 必须显式告警并指明卡在哪）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .agent_harness import AgentHarnessRuntime, _AsyncInvoke

log = logging.getLogger("hermes.bridge")


class BridgeServer:
    def __init__(self, runtime: AgentHarnessRuntime, host: str = "127.0.0.1",
                 port: int = 8765, aiohttp_module: Any = None) -> None:
        self.runtime = runtime
        self.host = host
        self.port = port
        self._aiohttp = aiohttp_module  # 延迟注入（aiohttp 为运行时依赖，测试可 mock）
        self._server = None

    # ---------------------------------------------------------------- 启动
    def make_app(self) -> Any:
        """构建 aiohttp 应用（与 start() 同源，供进程内集成测试复用）。"""
        import aiohttp

        if self._aiohttp is None:
            self._aiohttp = aiohttp
        app = aiohttp.web.Application()
        app.router.add_post("/rpc", self._rpc_endpoint)
        app.router.add_get("/events", self._events_endpoint)
        app.router.add_get("/frames", self._frames_endpoint)
        return app

    async def start(self) -> None:
        import aiohttp

        if self._aiohttp is None:
            self._aiohttp = aiohttp
        app = self.make_app()
        self._server = aiohttp.web.AppRunner(app)
        await self._server.setup()
        site = aiohttp.web.TCPSite(self._server, self.host, self.port)
        await site.start()
        if not self.runtime.token or self.runtime.token == "":
            log.warning("bridge: no token configured; loopback-only trust is in effect (§2)")

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.cleanup()
            self._server = None

    # ---------------------------------------------------------------- 鉴权
    def _auth_ok(self, request: Any) -> bool:
        if not self.runtime.token:
            return True  # 环回降级态（构造时未配 token）
        header = request.headers.get("Authorization", "")
        return header == f"Bearer {self.runtime.token}"

    # ---------------------------------------------------------------- /rpc
    async def _rpc_endpoint(self, request: Any) -> Any:
        aiohttp = self._aiohttp
        if not self._auth_ok(request):
            body = json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32600, "message": "unauthorized"}}).encode()
            return aiohttp.web.Response(status=401, body=body, content_type="application/json")
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            body = json.dumps({"jsonrpc": "2.0", "id": None,
                               "error": {"code": -32700, "message": "parse error"}}).encode()
            return aiohttp.web.Response(status=400, body=body, content_type="application/json")
        loopback = request.remote in ("127.0.0.1", "::1")
        try:
            resp = self.runtime.handle_rpc(payload, token=self._resolve_token(request),
                                           loopback=loopback)
        except _AsyncInvoke as ai:
            resp = await self.runtime.invoke_rpc(ai)
        return aiohttp.web.json_response(resp)

    def _resolve_token(self, request: Any) -> str | None:
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return header[len("Bearer "):]
        return None

    # ---------------------------------------------------------------- /events
    async def _events_endpoint(self, request: Any) -> Any:
        aiohttp = self._aiohttp
        if not self._auth_ok(request):
            return aiohttp.web.Response(status=401)
        resp = aiohttp.web.StreamResponse(
            status=200, headers={"Content-Type": "text/event-stream",
                                 "Cache-Control": "no-cache"})
        await resp.prepare(request)
        # 先发历史事件（宿主重连后可补全视图），再挂实时订阅
        for ev in self.runtime.recent_events(limit=50):
            await self._write_sse(resp, ev["event"], ev["data"])
        try:
            while True:
                event, payload = await self.runtime.wait_event()
                await self._write_sse(resp, event, payload)
        except asyncio.CancelledError:
            pass
        return resp

    async def _write_sse(self, resp: Any, event: str, data: dict[str, Any]) -> None:
        text = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        await resp.write(text.encode("utf-8"))

    # ---------------------------------------------------------------- /frames
    async def _frames_endpoint(self, request: Any) -> Any:
        """拇指浏览器帧流（契约 §5）。

        帧源：t2 thumb 服务（`python -m hermes_superagent thumb`，端口 8766）
        产出的 `/{run}/frame` 端点；本端点做 SSE 封装 + seq 乱序保护标记。
        thumb 服务不可达 → 503 + reason（不静默，§9B.4.2）。
        """
        aiohttp = self._aiohttp
        if not self._auth_ok(request):
            return aiohttp.web.Response(status=401)
        thumb_url = request.app.get("thumb_url") or "http://127.0.0.1:8766/frame"
        import urllib.request
        import urllib.error

        def _fetch_frame() -> dict[str, Any] | None:
            try:
                with urllib.request.urlopen(thumb_url, timeout=2) as r:
                    return json.loads(r.read().decode("utf-8"))
            except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
                return None

        # 注意：urlopen 是同步阻塞调用，必须丢线程池，否则会卡住 aiohttp 事件循环
        last = await asyncio.to_thread(_fetch_frame)
        if last is None:
            return aiohttp.web.json_response(
                {"ok": False, "error": "frame source unavailable: " + thumb_url +
                 " (is `python -m hermes_superagent thumb` running?)"},
                status=503)
        seq = last.get("seq", 0)
        return aiohttp.web.json_response(
            {"ok": True, "protocol_version": "1.0",
             "frame_source": thumb_url, "last_seq": seq,
             "note": "frame stream endpoint is a skeleton; production forwards via SSE loop"})
