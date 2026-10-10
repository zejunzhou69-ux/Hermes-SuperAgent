"""桌面面板：终端启动后弹出面板（对标 DSH 的桌面面板体感）。

组成：
- `open_panel()`  跨平台弹出：原生窗口（可选 pywebview）→ 默认浏览器 → 仅打印 URL。
- `run_panel()`   启动拇指浏览器服务 + 弹面板 + 常驻，供 CLI `panel` 子命令调用。

原则（与 §9B.4-3 一致）：**绝不静默失败**。打不开就明确说打不开、URL 是多少、为什么。
"""

from __future__ import annotations

import asyncio
import os
import sys
import webbrowser
from dataclasses import dataclass

#: 默认面板路由
PANEL_PATH = "/panel.html"


@dataclass
class PanelLaunch:
    """一次面板弹出尝试的结果。"""

    mode: str  # "native" | "browser" | "printed"
    url: str
    detail: str

    @property
    def opened(self) -> bool:
        return self.mode in ("native", "browser")

    def to_dict(self) -> dict:
        return {"mode": self.mode, "url": self.url, "detail": self.detail,
                "opened": self.opened}


def panel_url(host: str = "127.0.0.1", port: int = 8766) -> str:
    return f"http://{host}:{port}{PANEL_PATH}"


def _is_headless() -> bool:
    """当前是否无 GUI（Linux 无 DISPLAY；CI 环境）。"""
    if os.environ.get("CI"):
        return True
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY") \
            and not os.environ.get("WAYLAND_DISPLAY"):
        return True
    return False


def open_panel(
    url: str | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8766,
    prefer_native: bool = True,
    native: bool | None = None,  # None = 自动（原生优先，回落浏览器）
) -> PanelLaunch:
    """弹出桌面面板。

    默认优先原生窗口（pywebview，独立桌面 App 观感，最接近 DSH 桌面端），
    打不开自动回落到默认浏览器标签页。
    """
    target = url or panel_url(host, port)

    if _is_headless():
        return PanelLaunch(
            mode="printed",
            url=target,
            detail="无 GUI 环境（headless/CI）：已打印 URL，请在浏览器手动打开",
        )

    want_native = prefer_native if native is None else native
    if want_native:
        detail = _try_native_window(target)
        if detail:
            return PanelLaunch(mode="native", url=target, detail=detail)

    if _try_browser(target):
        fallback = ("（pywebview 不可用，已回落到浏览器）" if want_native else "")
        return PanelLaunch(
            mode="browser",
            url=target,
            detail=f"已在默认浏览器打开面板{fallback}",
        )

    return PanelLaunch(
        mode="printed", url=target,
        detail="webbrowser 未能打开：请手动访问上面的 URL",
    )


def _try_native_window(url: str) -> str | None:
    """用 pywebview 开原生窗口；不可用返回 None（静默回退浏览器）。"""
    try:
        import webview  # type: ignore
    except ImportError:
        return None
    title = "Hermes-SuperAgent"
    try:
        webview.create_window(title, url, width=1000, height=820)
        webview.start()
        return f"原生窗口已关闭（{title} 1000×820）"
    except Exception:  # noqa: BLE001 - 原生窗口不可用则回退浏览器
        return None


def _try_browser(url: str) -> bool:
    """默认浏览器打开（macOS 优先 Chrome app 模式）。"""
    if sys.platform == "darwin":
        try:
            import subprocess

            subprocess.Popen(["open", "-a", "Google Chrome", "--args", f"--app={url}"])
            return True
        except Exception:  # noqa: BLE001
            pass
    try:
        return bool(webbrowser.open(url, new=1, autoraise=True))
    except Exception:  # noqa: BLE001
        return False


async def run_panel(
    port: int = 8766,
    *,
    do_open: bool = True,
    prefer_native: bool = False,
) -> None:
    """启动拇指浏览器服务并弹出面板，常驻直到 Ctrl+C。

    面板弹出放在后台线程：原生窗口（pywebview）会阻塞到窗口关闭，
    不能让它卡住 aiohttp 事件循环。
    """
    from aiohttp import web

    from hermes_superagent.thumb.server import create_app

    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    url = panel_url(port=port)
    print("Hermes-SuperAgent 桌面面板")
    print(f"  面板    : {url}")
    print(f"  帧流    : http://127.0.0.1:{port}/thumb/frames")
    print(f"  实例状态: http://127.0.0.1:{port}/thumb/status")
    print("  停止    : Ctrl+C")

    if do_open:
        # 默认原生窗口优先（pywebview），打不开回落浏览器
        launch = await asyncio.to_thread(
            open_panel, url, port=port,
            native=(True if prefer_native else False),
        )
        print(f"  弹出    : [{launch.mode}] {launch.detail}")
        if not launch.opened:
            print(f"  ⚠ 未能自动弹出，请手动打开：{url}")

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()


__all__ = ["PanelLaunch", "open_panel", "panel_url", "run_panel", "PANEL_PATH"]
