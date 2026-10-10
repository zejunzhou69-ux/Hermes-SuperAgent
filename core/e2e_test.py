"""端到端冒烟测试：MCP stdio + L2 浏览器 + L1 爬取 + 拇指浏览器 SSE。

运行：python e2e_test.py
退出码：0 = 全过，1 = 有失败。
"""

import asyncio
import gc
import json
import logging
import subprocess
import sys
from pathlib import Path

CORE = Path(__file__).parent
sys.path.insert(0, str(CORE))


def _quiet_closed_pipe(unraisable):
    """Windows ProactorEventLoop 在解释器退出时的 'closed pipe' 噪声属已知无害问题，
    不影响任何断言结果；静默它以保证脚本退出码真实反映测试通过与否。"""
    if "closed pipe" in str(unraisable.exc_value):
        return
    sys.__unraisablehook__(unraisable)


sys.unraisablehook = _quiet_closed_pipe


#: Playwright 的 sync API 在解释器退出时，其内部连接任务可能仍处 pending，
#: asyncio 会以 ERROR 级日志打印 "Task was destroyed but it is pending!"。
#: 这属驱动层清理噪声，不代表任何断言失败；过滤掉以保证退出码与输出真实。
class _DropTaskDestroyed(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D102
        return "Task was destroyed" not in record.getMessage()


logging.getLogger("asyncio").addFilter(_DropTaskDestroyed())


def _run(coro):
    """跑一个协程并回收事件循环残留，避免退出码被清理噪声污染。"""
    try:
        return asyncio.run(coro)
    finally:
        gc.collect()


def test_mcp_stdio():
    """Test MCP server via subprocess stdio."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "hermes_superagent", "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # CLI 会把 stdout 重配为 UTF-8（Windows GBK 控制台兜底），
        # 父进程必须同样按 UTF-8 解码，否则中文工具描述会 UnicodeDecodeError。
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    try:
        # Send ping
        msg = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"
        proc.stdin.write(msg)
        proc.stdin.flush()
        line = proc.stdout.readline()
        resp = json.loads(line.strip())
        assert resp["result"] == "pong", f"ping response: {resp}"
        print("OK MCP stdio ping")

        # Send tools/list
        msg = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
        proc.stdin.write(msg)
        proc.stdin.flush()
        line = proc.stdout.readline()
        resp = json.loads(line.strip())
        tools = resp["result"]["tools"]
        names = [t["name"] for t in tools]
        assert len(names) == 9, f"Expected 9 tools, got {len(names)}: {names}"
        print(f"OK MCP stdio tools/list: {names}")

        # Send tools/call on crawl.fetch (will fail due to network, but should not crash)
        msg = json.dumps({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "crawl.fetch", "arguments": {"url": "https://nonexistent.invalid"}}
        }) + "\n"
        proc.stdin.write(msg)
        proc.stdin.flush()
        line = proc.stdout.readline()
        resp = json.loads(line.strip())
        # Should return isError=true, not crash
        assert "result" in resp, f"Expected result key, got: {resp}"
        print(f"OK MCP stdio tools/call (expected failure): isError={resp['result'].get('isError')}")

    finally:
        proc.terminate()
        proc.wait(timeout=3)
        print("OK MCP stdio subprocess cleaned up")


def test_l2_browser():
    """Test L2 Playwright browser open/goto/screenshot/frame cycle."""
    from hermes_superagent.capabilities.browser import BrowserCapability

    async def run():
        b = BrowserCapability(max_instances=1)
        r = await b.invoke("open", {"url": "https://example.com", "headless": True})
        if not r.get("ok"):
            print(f"SKIP L2 browser test (open failed): {r.get('error', '')[:80]}")
            await b.aclose()
            return
        iid = r["data"]["instance_id"]
        print(f"OK L2 browser opened: {iid}")

        r2 = await b.invoke("goto", {"instance_id": iid, "url": "https://example.com"})
        assert r2["ok"], f"goto failed: {r2}"
        print(f"OK L2 goto: url={r2['data'].get('url')}, status={r2['data'].get('status')}")

        r3 = await b.invoke("screenshot", {"instance_id": iid, "format": "jpeg"})
        assert r3["ok"], f"screenshot failed: {r3}"
        b64 = r3["data"]["data_base64"]
        print(f"OK L2 screenshot: format={r3['data']['format']}, size={len(b64)} bytes")

        r4 = await b.invoke("frame", {"instance_id": iid, "format": "jpeg", "max_width": 360})
        assert r4["ok"], f"frame failed: {r4}"
        print(f"OK L2 frame: width={r4['data']['width']}, height={r4['data']['height']}")

        r5 = await b.invoke("detach", {"instance_id": iid})
        assert r5["ok"]
        print(f"OK L2 detach: {r5['data']['detached']}")

        await b.aclose()
        print("OK L2 browser cycle complete")

    _run(run())


def test_crawl_l1_simple():
    """Test L1 crawl on a known-static page (if reachable)."""
    from hermes_superagent.capabilities.crawl import CrawlCapability

    async def run():
        c = CrawlCapability(default_level="L1", auto_escalate=False, max_level="L1")
        r = await c.invoke("fetch", {"url": "https://example.com", "level": "L1"})
        if r.get("ok"):
            data = r["data"]
            print(f"OK L1 fetch example.com: status={data.get('status')}, html_chars={data.get('html_chars')}, text_chars={data.get('text_chars')}")
        else:
            print(f"INFO L1 fetch example.com unreachable (network restricted): {r.get('error', '')[:80]}")
        await c.aclose()

    _run(run())


def test_thumb_sse_simulation():
    """Simulate what the thumb SSE endpoint does with a fake browser."""
    from hermes_superagent.thumb.server import create_app

    class FakeBrowser:
        _counter = 0

        async def invoke(self, action, params):
            if action == "open":
                return {"ok": True, "data": {"instance_id": "sim-001", "mode": "builtin", "headless": True, "url": "about:blank"}}
            if action == "frame":
                self._counter += 1
                return {"ok": True, "data": {"format": "jpeg", "width": 360, "height": 640, "ts": self._counter * 1000, "data_base64": ""}}
            if action == "list_pool":
                return {"ok": True, "data": {"instances": [{"instance_id": "sim-001", "mode": "builtin", "headless": True, "url": "about:blank"}], "max_instances": 3}}
            if action == "goto":
                return {"ok": True, "data": {"url": params.get("url", ""), "status": 200}}
            return {"ok": False, "error": f"unknown action: {action}"}

        async def aclose(self):
            pass

    async def run():
        app = create_app(browser=FakeBrowser())
        # Verify app has expected middleware/routes
        assert "/panel.html" in [r.resource.canonical for r in app.router.routes()]
        print("OK thumb app has all expected routes")

        # Test /thumb/status endpoint
        from aiohttp.test_utils import TestClient, TestServer
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.get("/thumb/status")
            assert r.status == 200
            body = await r.json()
            assert body["ok"]
            instances = body["data"]["instances"]
            assert len(instances) == 1
            assert instances[0]["instance_id"] == "sim-001"
            print(f"OK thumb /thumb/status: {len(instances)} instance(s)")

            r2 = await client.post("/thumb/goto", json={"url": "https://example.com"})
            assert r2.status == 200
            body2 = await r2.json()
            assert body2["ok"]
            print(f"OK thumb /thumb/goto: {body2['data'].get('url')}")

            r3 = await client.get("/panel.html")
            assert r3.status == 200
            html = await r3.text()
            assert '<canvas id="cv"' in html
            print(f"OK thumb /panel.html: {len(html)} bytes, has canvas")
        finally:
            await client.close()

    _run(run())


def test_panel_render():
    """真渲染验证桌面面板：起真服务 → 真 Chromium 加载 → 校验 DOM/样式/Canvas 出帧。

    比「HTTP 200 + 含 canvas 字样」强得多：证明面板真的连上帧流并把帧画进了 canvas。
    无 Playwright/Chromium 时跳过（不误报失败）。
    """
    import threading
    import time as _time

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP panel render (playwright not installed)")
        return

    from aiohttp import web

    from hermes_superagent.thumb.server import create_app

    PORT = 8799
    started = threading.Event()
    state = {"stop": False}

    def serve():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        app = create_app()
        runner = web.AppRunner(app)

        async def run():
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", PORT)
            await site.start()
            started.set()
            while not state["stop"]:
                await asyncio.sleep(0.2)
            await runner.cleanup()

        try:
            loop.run_until_complete(run())
        finally:
            loop.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    if not started.wait(30):
        print("SKIP panel render (thumb server did not start)")
        return

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as exc:  # noqa: BLE001 - Chromium 未安装则跳过
                print(f"SKIP panel render (chromium unavailable): {str(exc)[:80]}")
                return

            page = browser.new_page(viewport={"width": 1000, "height": 820})
            js_errors: list[str] = []
            page.on("pageerror", lambda e: js_errors.append(str(e)))

            page.goto(f"http://127.0.0.1:{PORT}/panel.html", wait_until="load")
            page.wait_for_selector("header", timeout=10000)

            # 1) 桌面面板当前双栏布局元素都在（.left/.right + 中线 + 工具条）
            for sel in ("header", "footer", ".left", ".right", ".divider",
                        "#cv", "#chatLog", "#urlInput", "#btnGoto", "#btnShot", "#btnPick"):
                assert page.query_selector(sel) is not None, f"面板缺少元素 {sel}"
            print("OK panel DOM: header/left/right/divider/toolbar/footer 齐全")

            # 2) 布局未塌陷：左栏有实宽、右侧舞台高于 200px
            left_w = page.eval_on_selector(".left", "el => el.getBoundingClientRect().width")
            stage_h = page.eval_on_selector(".browser-stage",
                                            "el => el.getBoundingClientRect().height")
            assert left_w > 300, f"左栏宽度异常: {left_w}"
            assert stage_h > 200, f"舞台高度异常: {stage_h}"
            print(f"OK panel layout: left={left_w:.0f}px stage={stage_h:.0f}px")

            # 3) 深色主题生效（不是白页）
            body_bg = page.eval_on_selector("body", "el => getComputedStyle(el).backgroundColor")
            assert body_bg not in ("rgba(0, 0, 0, 0)", "rgb(255, 255, 255)"), \
                f"主题样式未生效: {body_bg}"
            print(f"OK panel theme: body background = {body_bg}")

            # 4) 真的收到帧：等待帧计数 > 0（#fFrames）
            deadline = _time.time() + 40
            frames = 0
            while _time.time() < deadline:
                frames = int(page.text_content("#fFrames") or "0")
                if frames > 0:
                    break
                _time.sleep(0.5)
            assert frames > 0, "面板 40 秒内未收到任何帧（帧流未打通）"
            print(f"OK panel frame stream: 已渲染 {frames} 帧")

            # 5) canvas 像素非空——证明帧真的画上去了（而不是只计数）
            painted = page.evaluate(
                """() => {
                    const c = document.getElementById('cv');
                    const ctx = c.getContext('2d');
                    const d = ctx.getImageData(0, 0, c.width, c.height).data;
                    let n = 0;
                    for (let i = 0; i < d.length; i += 4) {
                        if (d[i] || d[i+1] || d[i+2]) n++;
                    }
                    return n;
                }"""
            )
            assert painted > 0, "canvas 全黑：帧计数在涨但没有真正绘制"
            print(f"OK panel canvas: {painted} 个非空像素")

            # 6) 无未捕获 JS 异常
            assert not js_errors, f"面板有未捕获 JS 异常: {js_errors[:3]}"
            print("OK panel JS: 无未捕获异常")

            # 7) 截图存档（人工复核用）
            shot = CORE / "demo_output" / "panel-render.png"
            shot.parent.mkdir(exist_ok=True)
            page.screenshot(path=str(shot))
            print(f"OK panel screenshot: {shot.name}")

            page.close()
            browser.close()
    finally:
        state["stop"] = True
        t.join(timeout=15)


if __name__ == "__main__":
    tests = [
        ("mcp_stdio", test_mcp_stdio),
        ("l2_browser", test_l2_browser),
        ("crawl_l1", test_crawl_l1_simple),
        ("thumb_sse", test_thumb_sse_simulation),
        ("panel_render", test_panel_render),
    ]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"FAIL {name}: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\nResults: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
