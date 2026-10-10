"""嵌入浏览器能力（设计方案 §4.2 / §6.2，魔改点 H1 的适配层落点）。

双模式：
- 内建模式：Playwright 驱动的内置 Chromium 池（默认 headless，可 headful）
- 外接模式：通过 CDP（--remote-debugging-port）接管用户已开的 Chrome，
  用于登录态 / 风控 / 人工介入场景

实例池约束（对应风险 K6）：
- 池上限 max_instances，空闲超时自动回收
- 每次操作返回 trace_id，便于全链路可观测（设计原则 4）

注意：本骨架对 playwright 使用延迟导入 + 优雅降级——
Chromium 未安装（未执行 playwright install）时，L1 抓取仍可工作，
浏览器能力显式报 unavailable 而非崩溃。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from hermes_superagent.net.routing import NetRoute, resolve_net_route

#: 浏览器实例池默认上限（K6 资源约束）
MAX_INSTANCES = 3
#: 空闲回收阈值（秒）
IDLE_REAP_SECONDS = 300
#: 默认视口：桌面尺寸，利于常规抓取渲染
DEFAULT_VIEWPORT = (1280, 720)
#: 拇指浏览器视口：360×640 竖屏（设计方案 §13.2，窄屏设备兼容用）
THUMB_VIEWPORT = (360, 640)
#: 大屏桌面视口：16:9（面板/双端工作台默认，替换原 360×640 手机竖屏）
DESKTOP_VIEWPORT = (1440, 900)


class BrowserUnavailable(RuntimeError):
    """内置 Chromium 不可用（未安装或启动失败）。调用方应降级 L1。"""


@dataclass
class BrowserInstance:
    """一个内建浏览器实例（pool 管理）。"""

    instance_id: str
    mode: str  # "builtin" | "cdp"
    # 延迟赋值：playwright 句柄
    pw: Any = None
    browser: Any = None
    context: Any = None
    page: Any = None
    headless: bool = True
    last_used: float = field(default_factory=time.time)
    cdp_endpoint: str | None = None

    def touch(self) -> None:
        self.last_used = time.time()

    def is_idle(self, threshold: float = IDLE_REAP_SECONDS) -> bool:
        return (time.time() - self.last_used) > threshold


class BrowserCapability:
    """设计方案 §6.1 的 Capability 协议实现（browser 能力）。"""

    name = "browser"

    def __init__(self, max_instances: int = MAX_INSTANCES,
                 viewport: tuple[int, int] = DEFAULT_VIEWPORT) -> None:
        self._pool: list[BrowserInstance] = []
        self._lock = asyncio.Lock()
        self._max_instances = max_instances
        #: 内建实例的视口。默认桌面尺寸（利于抓取）；拇指浏览器用 360×640 竖屏
        self._viewport = viewport

    # ------------------------------------------------------------------
    # Capability 协议（§6.1）
    # ------------------------------------------------------------------
    async def invoke(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        trace_id = uuid.uuid4().hex[:12]
        handler = getattr(self, f"_act_{action}", None)
        if handler is None:
            return {"ok": False, "error": f"unknown action: {action}", "trace_id": trace_id}
        try:
            data = await handler(params)
            return {"ok": True, "data": data, "trace_id": trace_id}
        except BrowserUnavailable as exc:
            return {"ok": False, "error": f"browser unavailable: {exc}", "trace_id": trace_id}
        except Exception as exc:  # noqa: BLE001 - 能力层兜底，向上报告可观测错误
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "trace_id": trace_id}

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "actions": {
                "open": {"url": "str (可选)", "headless": "bool", "instance_id": "str (可选，复用池实例)"},
                "goto": {"instance_id": "str", "url": "str"},
                "click": {"instance_id": "str", "selector": "str"},
                "type": {"instance_id": "str", "selector": "str", "text": "str"},
                "screenshot": {"instance_id": "str", "format": "jpeg|png|webp"},
                "mouse": {"instance_id": "str", "op": "wheel|click|dblclick|rightclick",
                          "x": "float (视口坐标，0..viewport_w)", "y": "float",
                          "delta_y": "float (wheel 滚轮量，+下 -上)"},
                "extract_text": {"instance_id": "str", "selector": "str (可选)"},
                "attach_cdp": {"endpoint": "str (如 http://127.0.0.1:9222)"},
                "detach": {"instance_id": "str"},
                "frame": {"instance_id": "str", "format": "jpeg|webp", "max_width": "int"},
                "list_pool": {},
            },
        }

    # ------------------------------------------------------------------
    # 实例池
    # ------------------------------------------------------------------
    async def _acquire(self, headless: bool, cdp_endpoint: str | None = None) -> BrowserInstance:
        async with self._lock:
            self._reap_idle_locked()
            # 复用空闲实例
            for inst in self._pool:
                if inst.mode == "builtin" and cdp_endpoint is None:
                    return inst
            if cdp_endpoint:
                inst = await self._launch_cdp(cdp_endpoint)
            else:
                if len(self._pool) >= self._max_instances:
                    raise BrowserUnavailable(
                        f"pool full ({self._max_instances}); raise browser.max_instances in config"
                    )
                inst = await self._launch_builtin(headless)
            self._pool.append(inst)
            return inst

    def _reap_idle_locked(self) -> None:
        keep: list[BrowserInstance] = []
        for inst in self._pool:
            if inst.is_idle() and inst.page is not None:
                asyncio.get_running_loop().create_task(self._close_instance(inst))
                continue
            keep.append(inst)
        self._pool = keep

    async def _close_instance(self, inst: BrowserInstance) -> None:
        try:
            if inst.page is not None:
                await inst.page.close()
            if inst.context is not None and inst.mode == "builtin":
                await inst.context.close()
            if inst.browser is not None and inst.mode == "builtin":
                await inst.browser.close()
        except Exception:  # noqa: BLE001 - 回收兜底
            pass
        finally:
            self._pool = [p for p in self._pool if p is not inst]

    async def _launch_builtin(self, headless: bool) -> BrowserInstance:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover
            raise BrowserUnavailable(f"playwright not installed: {exc}") from exc

        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.launch(headless=headless)
        except Exception as exc:  # Chromium 未安装 / 启动失败
            await pw.stop()
            raise BrowserUnavailable(f"cannot launch chromium: {exc}") from exc

        context = await browser.new_context(
            viewport={"width": self._viewport[0], "height": self._viewport[1]}
        )
        page = await context.new_page()
        return BrowserInstance(
            instance_id=uuid.uuid4().hex[:8],
            mode="builtin",
            pw=pw,
            browser=browser,
            context=context,
            page=page,
            headless=headless,
        )

    async def _launch_cdp(self, endpoint: str) -> BrowserInstance:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover
            raise BrowserUnavailable(f"playwright not installed: {exc}") from exc

        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.connect_over_cdp(endpoint)
        except Exception as exc:
            await pw.stop()
            raise BrowserUnavailable(f"CDP connect failed ({endpoint}): {exc}") from exc

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()
        return BrowserInstance(
            instance_id=uuid.uuid4().hex[:8],
            mode="cdp",
            pw=pw,
            browser=browser,
            context=context,
            page=page,
            cdp_endpoint=endpoint,
        )

    def _get(self, instance_id: str) -> BrowserInstance:
        for inst in self._pool:
            if inst.instance_id == instance_id:
                inst.touch()
                return inst
        raise BrowserUnavailable(f"unknown instance_id: {instance_id}")

    async def aclose(self) -> None:
        """关闭池内全部实例（任务结束 / 进程退出时调用）。"""
        for inst in list(self._pool):
            await self._close_instance(inst)
        self._pool = []

    # ------------------------------------------------------------------
    # actions（§6.2 browser action 表）
    # ------------------------------------------------------------------
    async def _act_open(self, params: dict[str, Any]) -> dict[str, Any]:
        headless = params.get("headless", True)
        inst = await self._acquire(headless=headless, cdp_endpoint=params.get("cdp_endpoint"))
        url = params.get("url")
        if url:
            await inst.page.goto(url, wait_until="domcontentloaded", timeout=20000)
        return {
            "instance_id": inst.instance_id,
            "mode": inst.mode,
            "headless": inst.headless,
            "url": inst.page.url,
        }

    async def _act_goto(self, params: dict[str, Any]) -> dict[str, Any]:
        inst = self._get(params["instance_id"])
        resp = await inst.page.goto(params["url"], wait_until="domcontentloaded")
        return {"url": inst.page.url, "status": getattr(resp, "status", None)}

    async def _act_click(self, params: dict[str, Any]) -> dict[str, Any]:
        inst = self._get(params["instance_id"])
        await inst.page.click(params["selector"])
        return {"clicked": params["selector"]}

    async def _act_type(self, params: dict[str, Any]) -> dict[str, Any]:
        inst = self._get(params["instance_id"])
        await inst.page.fill(params["selector"], params["text"])
        return {"typed_into": params["selector"]}

    async def _act_screenshot(self, params: dict[str, Any]) -> dict[str, Any]:
        inst = self._get(params["instance_id"])
        fmt = params.get("format", "jpeg")
        b64 = await inst.page.screenshot(type=fmt, full_page=params.get("full_page", False))
        import base64

        return {"format": fmt, "data_base64": base64.b64encode(b64).decode("ascii")}

    async def _act_mouse(self, params: dict[str, Any]) -> dict[str, Any]:
        """画面直接交互：滚轮滚动 / 鼠标点击桥接到内嵌 Chromium（设计 §4.2 帧流升级）。

        坐标由面板 canvas 画面像素换算成视口坐标后传入（前端负责映射，
        因为画面是 contain 等比缩放且居中，换算比在后端做更准）。
        - op=wheel    : page.mouse.wheel(0, delta_y)（页面滚动）
        - op=click    : page.mouse.click(x, y)
        - op=dblclick : page.mouse.dblclick(x, y)
        - op=rightclick: page.mouse.click(x, y, button="right")
        """
        inst = self._get(params["instance_id"])
        op = params.get("op", "wheel")
        x = float(params.get("x", 0))
        y = float(params.get("y", 0))
        # 坐标夹到视口内（画面 contain 居中，超出部分点到空白）
        x = max(0.0, min(x, self._viewport[0] - 1))
        y = max(0.0, min(y, self._viewport[1] - 1))
        if op == "wheel":
            delta_y = float(params.get("delta_y", 0))
            if delta_y == 0:
                delta_y = 100.0  # 未传量时默认一档
            # 滚轮以画面中心附近滚动最自然；用当前鼠标位置或中心
            await inst.page.mouse.wheel(0, delta_y, steps=max(1, int(abs(delta_y) / 50)))
            return {"op": "wheel", "delta_y": delta_y}
        if op == "click":
            await inst.page.mouse.click(x, y)
        elif op == "dblclick":
            await inst.page.mouse.dblclick(x, y)
        elif op == "rightclick":
            await inst.page.mouse.click(x, y, button="right")
        else:
            return {"op": op, "note": "unknown op, no action"}
        return {"op": op, "x": x, "y": y}

    async def _act_extract_text(self, params: dict[str, Any]) -> dict[str, Any]:
        inst = self._get(params["instance_id"])
        selector = params.get("selector")
        text = await inst.page.inner_text(selector or "body")
        return {"text": text}

    async def _act_attach_cdp(self, params: dict[str, Any]) -> dict[str, Any]:
        inst = await self._acquire(headless=False, cdp_endpoint=params["endpoint"])
        return {"instance_id": inst.instance_id, "mode": "cdp", "endpoint": inst.cdp_endpoint}

    async def _act_detach(self, params: dict[str, Any]) -> dict[str, Any]:
        inst = self._get(params["instance_id"])
        await self._close_instance(inst)
        return {"detached": inst.instance_id}

    async def _act_frame(self, params: dict[str, Any]) -> dict[str, Any]:
        """大屏帧：截当前画面为 JPEG/WebP 并缩放到 max_width（设计 §4.2 帧流）。

        清晰度优化（§13.2 视口升级后）：
        - JPEG 质量默认 90（Pillow 默认 75 发糊），可经 quality 参数 1–100 调
        - LANCZOS 缩放（原默认 BILINEAR 缩小文字发糊）
        - max_width 默认随视口宽；调用方可传更大值（如 1920）换取更清晰
        """
        import base64

        inst = self._get(params["instance_id"])
        fmt = params.get("format", "jpeg")
        # 默认帧宽随实例视口宽（大屏 1440）；调用方可用 max_width 显式调大/收窄。
        # 旧版写死 360 是"手机感"根因之一。
        max_width = int(params.get("max_width") or self._viewport[0])
        # 质量：默认 90（清晰可读）；允许前端按视口 DPR 提到 95+
        quality = int(params.get("quality", 90))
        quality = max(1, min(100, quality))
        raw = await inst.page.screenshot(type="png")
        img = _decode_image(raw)
        img = _resize(img, max_width)
        out = _encode_image(img, fmt, quality=quality)
        return {
            "format": fmt,
            "width": img.width,
            "height": img.height,
            "quality": quality,
            "ts": int(time.time() * 1000),
            "data_base64": base64.b64encode(out).decode("ascii"),
        }

    async def _act_list_pool(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {
            "instances": [
                {
                    "instance_id": i.instance_id,
                    "mode": i.mode,
                    "headless": i.headless,
                    "url": i.page.url if i.page else None,
                }
                for i in self._pool
            ],
            "max_instances": self._max_instances,
        }

    # ------------------------------------------------------------------
    # 帧流（拇指浏览器 §4.2）
    # ------------------------------------------------------------------
    def frame_stream(
        self, instance_id: str, fps: float = 4.0, format: str = "jpeg", max_width: int = 360
    ) -> "asyncio.AsyncIterator[bytes]":
        """按 fps 产出 JPEG/WebP 帧（供 SSE/WS 推流）。"""
        return self._frame_generator(instance_id, fps, format, max_width)

    async def _frame_generator(self, instance_id, fps, format, max_width):
        period = 1.0 / max(fps, 0.1)
        while True:
            res = await self.invoke(
                "frame", {"instance_id": instance_id, "format": format, "max_width": max_width}
            )
            if res.get("ok"):
                import base64

                yield base64.b64decode(res["data"]["data_base64"])
            else:
                return
            await asyncio.sleep(period)


# ----------------------------------------------------------------------
# 图像缩放辅助（Pillow 可选；缺省退化为原图，不阻断帧流）
# ----------------------------------------------------------------------
def _decode_image(raw: bytes):
    try:
        from io import BytesIO

        from PIL import Image

        return Image.open(BytesIO(raw))
    except ImportError:  # pragma: no cover
        raise BrowserUnavailable("Pillow required for frame resizing") from None


def _resize(img, max_width: int):
    """LANCZOS 高质量缩放（Pillow 默认 BILINEAR 在缩小文字/图标时发糊，
    是大屏帧流"不够清晰"的主因之一；LANCZOS 边缘更锐利）。"""
    try:
        if img.width > max_width:
            from PIL import Image as _PILImage

            ratio = max_width / img.width
            new_size = (max_width, max(1, int(img.height * ratio)))
            filter = getattr(_PILImage, "LANCZOS", getattr(_PILImage, "ANTIALIAS", None))
            img = img.resize(new_size, filter) if filter else img.resize(new_size)
    except Exception:  # noqa: BLE001
        pass
    return img


def _encode_image(img, fmt: str, quality: int = 90) -> bytes:
    """JPEG/WebP 编码。默认 quality=90（Pillow JPEG 默认 75 偏软，是画面发糊的主因）；
    高分辨率大屏帧需要更高质量才清晰可读。"""
    from io import BytesIO

    buf = BytesIO()
    save_fmt = "JPEG" if fmt in ("jpeg", "jpg") else ("WEBP" if fmt == "webp" else "PNG")
    if save_fmt in ("JPEG", "WEBP") and img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    if save_fmt == "JPEG":
        img.save(buf, format=save_fmt, quality=quality, optimize=True)
    elif save_fmt == "WEBP":
        img.save(buf, format=save_fmt, quality=quality)
    else:
        img.save(buf, format=save_fmt)
    return buf.getvalue()


def resolve_route_for_browser(region: str = "auto") -> NetRoute:
    """浏览器网络出口选择（§8：region 配置，不写死域名）。"""
    return resolve_net_route(region)
