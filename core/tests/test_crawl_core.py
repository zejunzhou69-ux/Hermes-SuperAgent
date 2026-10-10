"""hermes_superagent 单元测试（不依赖网络 / 不启动浏览器）。

运行：python -m pytest hermes-superagent/core/tests -q
（或 PYTHONPATH=hermes-superagent/core python -m pytest tests -q）
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest

from hermes_superagent.capabilities.crawl import (
    CrawlCapability,
    _cdn_hint,
    _hit_risk,
    _looks_dynamic,
)
from hermes_superagent.net.routing import RateLimiter, resolve_net_route


# ----------------------------------------------------------------------
# 网络路由（§8：配置化、无硬编码域名）
# ----------------------------------------------------------------------
def test_route_explicit_region():
    assert resolve_net_route("cn").resolved_region == "cn"
    assert resolve_net_route("intl").resolved_region == "intl"


def test_route_auto_defaults_intl(monkeypatch):
    monkeypatch.delenv("HERMES_SUPERAGENT_NET_CN_REACHABLE", raising=False)
    assert resolve_net_route("auto").resolved_region == "intl"


def test_route_auto_respects_probe(monkeypatch):
    monkeypatch.setenv("HERMES_SUPERAGENT_NET_CN_REACHABLE", "1")
    assert resolve_net_route("auto").resolved_region == "cn"


def test_route_proxy_injected(monkeypatch):
    monkeypatch.setenv("HERMES_SUPERAGENT_NET_PROXY", "http://127.0.0.1:7890")
    r = resolve_net_route("cn")
    assert r.proxy == "http://127.0.0.1:7890"


def test_rate_limiter():
    rl = RateLimiter(rate_per_min=2, burst=2)
    assert rl.allow("a") and rl.allow("a")
    assert not rl.allow("a")  # 桶空


# ----------------------------------------------------------------------
# 启发式（§4.2 分级判断 / 国内反爬）
# ----------------------------------------------------------------------
def test_looks_dynamic_small_shell():
    assert _looks_dynamic("<html><script src='/app.js'></script></html>") is True


def test_not_dynamic_content_page():
    html = "<html><body>" + "<p>" + "正文内容 " * 100 + "</p></body></html>"
    assert _looks_dynamic(html) is False


def test_hit_risk_status_and_captcha():
    assert _hit_risk(403, "")
    assert _hit_risk(200, "请完成以下验证 (captcha)")


def test_cdn_hint_vendors():
    assert _cdn_hint({"x-amz-cf-id": "abc"}) == "cloudfront"
    assert _cdn_hint({"x-cache": "HIT"}) == "fastly-or-nginx-cache"
    assert _cdn_hint({"content-type": "text/html"}) is None


# ----------------------------------------------------------------------
# L1 爬取（mock httpx，验证 CDN/限速/风控升级路径）
# ----------------------------------------------------------------------
def _fake_client_factory(handler):
    """构造替换 httpx.AsyncClient 的假客户端工厂。

    FakeClient 必须与传入的 kwargs（headers/proxy/timeout/follow_redirects）
    兼容，且不产生任何真实 I/O。
    """

    class FakeClient:
        def __init__(self, *a, **k):
            self._handler = handler

        def __aiter__(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            return self._handler(url)

        async def aclose(self):
            pass

    return FakeClient


def _resp(status, html, headers=None):
    class R:
        def __init__(self):
            self.status_code = status
            self.text = html
            self.headers = headers or {}

    return R()


def test_l1_success_and_cdn(monkeypatch):
    import hermes_superagent.capabilities.crawl as crawl_mod

    def handler(url):
        return _resp(200, "<html><body>" + "内容" * 200 + "</body></html>", {"x-cache": "HIT"})

    monkeypatch.setattr(crawl_mod.httpx, "AsyncClient", _fake_client_factory(handler))
    cap = CrawlCapability()

    async def run():
        r = await cap.invoke("fetch", {"url": "https://ex.cn/page", "level": "L1"})
        assert r["ok"], r
        assert r["data"]["level"] == "L1"
        assert r["data"]["cdn"] == "fastly-or-nginx-cache"
        assert r["data"]["status"] == 200
        # CDN 命中 → 即使短文本也不应升 L2
        assert r["data"].get("needs_js_render") is None

    asyncio.run(run())


def test_l1_risk_escalates_to_l2(monkeypatch):
    import hermes_superagent.capabilities.crawl as crawl_mod

    calls = {"n": 0}

    async def fake_crawl_l2(self, url, headless, stealth, timeout_s):
        calls["n"] += 1
        return {"status": 200, "html_chars": 10, "text_chars": 5, "text_preview": "ok", "stealth": stealth}

    def handler(url):
        return _resp(200, "access denied, human verification")

    monkeypatch.setattr(crawl_mod.httpx, "AsyncClient", _fake_client_factory(handler))
    monkeypatch.setattr(CrawlCapability, "_crawl_l2", fake_crawl_l2)
    cap = CrawlCapability()

    async def run():
        r = await cap.invoke("fetch", {"url": "https://ex.cn/x", "level": "L1"})
        assert r["ok"], r
        assert r["data"]["level"] == "L2"  # L1 命中风控 → 自动升级
        assert calls["n"] == 1

    asyncio.run(run())


def test_l1_failure_no_auto_escalate(monkeypatch):
    import hermes_superagent.capabilities.crawl as crawl_mod

    def handler(url):
        return _resp(500, "<html>internal error</html>")

    monkeypatch.setattr(crawl_mod.httpx, "AsyncClient", _fake_client_factory(handler))
    cap = CrawlCapability(auto_escalate=False)

    async def run():
        r = await cap.invoke("fetch", {"url": "https://ex.cn/500", "level": "L1"})
        assert not r["ok"], r
        assert r["level"] == "L1"
        assert "http 500" in r["error"]

    asyncio.run(run())


def test_rate_limit_blocks_fetch(monkeypatch):
    import hermes_superagent.capabilities.crawl as crawl_mod

    cap = CrawlCapability(max_per_minute=1)
    key = "https://ex.cn/a"
    # 强制空桶：0 令牌且时间戳为"刚刚"，allow() 必然拒绝（不依赖真实计时）
    cap._limiter._buckets[key] = (0.0, cap._limiter._t.time())
    monkeypatch.setattr(crawl_mod.httpx, "AsyncClient", _fake_client_factory(lambda url: _resp(200, "<p>x" * 400)))

    async def run():
        r = await cap.invoke("fetch", {"url": key, "level": "L1"})
        assert not r["ok"], r
        assert "rate limit" in r["error"]

    asyncio.run(run())


# ----------------------------------------------------------------------
# MCP server（JSON-RPC 契约，纯同步、不起浏览器）
# ----------------------------------------------------------------------
def test_mcp_initialize_and_tools_list():
    from hermes_superagent.capabilities.browser_mcp import MCPServer

    srv = MCPServer()
    r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert r["result"]["serverInfo"]["name"] == "hermes-superagent"
    r2 = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    names = {t["name"] for t in r2["result"]["tools"]}
    assert {"crawl.fetch", "browser.open", "browser.frame", "crawl.batch"} <= names


def test_mcp_tools_call_unknown_tool():
    from hermes_superagent.capabilities.browser_mcp import MCPServer

    srv = MCPServer()
    r = srv.handle(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nope"}}
    )
    assert r["error"]["code"] == -32602


def test_mcp_ping():
    from hermes_superagent.capabilities.browser_mcp import MCPServer

    srv = MCPServer()
    r = srv.handle({"jsonrpc": "2.0", "id": 4, "method": "ping"})
    assert r["result"] == "pong"


# ----------------------------------------------------------------------
# 拇指浏览器帧缩放（Pillow 路径，不起浏览器）
# ----------------------------------------------------------------------
def test_frame_resize_helpers():
    from hermes_superagent.capabilities.browser import _encode_image, _resize

    raw = "fake"  # 这里只测 resize 不炸，图像编码用真图测
    from io import BytesIO

    from PIL import Image

    img = Image.new("RGB", (720, 1280), "white")
    img = _resize(img, 360)
    assert img.width == 360 and img.height == 640
    out = _encode_image(img, "jpeg")
    assert out[:2] == b"\xff\xd8"


def test_panel_html_serves():
    from hermes_superagent.thumb.panel import PANEL_HTML

    assert "EventSource" in PANEL_HTML and "360" in PANEL_HTML


def test_thumb_app_routes():
    from hermes_superagent.thumb.server import create_app

    app = create_app()
    routes = [r for r in app.router.routes() if getattr(r, "method", "") in ("GET", "POST")]
    paths = {getattr(r, "resource", None) and r.resource.canonical for r in routes}
    assert "/panel.html" in paths
    assert "/thumb/frames" in paths
    assert "/thumb/status" in paths
    assert "/thumb/goto" in paths


# ----------------------------------------------------------------------
# 视口：拇指浏览器必须出竖屏帧
# ----------------------------------------------------------------------
def test_thumb_viewport_is_portrait():
    """拇指浏览器必须用 360×640 竖屏视口，否则出的是横屏帧（§13.2）。"""
    from hermes_superagent.capabilities.browser import DEFAULT_VIEWPORT, THUMB_VIEWPORT

    assert THUMB_VIEWPORT == (360, 640)
    assert DEFAULT_VIEWPORT == (1280, 720)
    assert THUMB_VIEWPORT[1] > THUMB_VIEWPORT[0], "拇指视口应为竖屏（高 > 宽）"


def test_browser_capability_honours_viewport():
    from hermes_superagent.capabilities.browser import BrowserCapability

    assert BrowserCapability(viewport=(360, 640))._viewport == (360, 640)
    assert BrowserCapability()._viewport == (1280, 720)


def test_thumb_server_passes_desktop_viewport():
    """create_app 默认用大屏桌面视口（1440×900），不再写死 360×640 手机竖屏；
    帧宽随视口（DEFAULT_FRAME_MAX_WIDTH），前端可覆盖。"""
    from hermes_superagent.thumb import server as thumb_server

    assert thumb_server.DESKTOP_VIEWPORT == (1440, 900)
    assert thumb_server.DEFAULT_FRAME_MAX_WIDTH == 1440
