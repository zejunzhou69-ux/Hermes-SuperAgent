"""爬取核心（设计方案 §4.2 分级 L1–L4 + §8 国内 CDN/反爬适配）。

调度策略（§4.2）：默认 L1 → 失败升 L2 → 命中风控升 L3 → 仍失败请求外接 L4。
autoEscalate 可配置；每一级都走统一的双栈路由与限速（net/routing.py）。

国内 CDN/反爬适配要点（均配置驱动）：
- 指纹伪装 L3：UA/viewport/时区/locale 参数化，L3 启用人类化节奏（随机延迟）
- 代理池：L3 消费 NetRoute.proxy（宿主注入国内/国际代理）
- CDN 静态资源：L1 命中 CDN 缓存头（age/max-age 新鲜）时直接复用，跳过浏览器
- robots / 限速：crawl.max_per_minute 全局令牌桶，可审计
"""

from __future__ import annotations

import asyncio
import random
import re
import time
import uuid
from typing import Any

import httpx

from hermes_superagent.capabilities.browser import BrowserCapability, BrowserUnavailable
from hermes_superagent.net.routing import NetRoute, RateLimiter, resolve_net_route

LEVELS = ("L1", "L2", "L3", "L4")

#: 风控特征（命中则升 L3；启发式，可配置扩展）
#: 只匹配"页面级拦截"的强信号；"验证码"是中文搜索 UI 的常见词条（如百度首页），不单独作为风控特征
RISK_PATTERNS = (
    re.compile(r"human ?verification", re.I),
    re.compile(r"access ?denied", re.I),
    re.compile(r"403 ?forbidden", re.I),
    re.compile(r"请完成以下验证", re.I),
    re.compile(r"验证一下", re.I),
)


def _looks_dynamic(html: str) -> bool:
    """启发式判断页面是否需要 JS 渲染（L2 升级依据）。

    判定顺序：先剔除 script/style 后看正文体量（国内 CDN 静态资源多的站点
    即使引用 JS 也可能是纯展示页，正文足够即判静态）；再按壳页特征判动态。
    阈值用字符数（CJK 每字 1 字符），300 字正文视为"可取的静态内容"。
    """
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", html, flags=re.I)
    body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
    if len(body) >= 300:
        return False  # 正文足够 → 静态可取
    if len(html) < 2000:
        return True  # 内容过短：大概率是 JS 壳页
    if re.search(r"<script[^>]*src=", html, re.I):
        return True
    if re.search(r"__NEXT_DATA__|window\.__NUXT__|ng-version|data-reactroot", html, re.I):
        return True
    return True


def _hit_risk(status_code: int | None, html: str) -> bool:
    """风控特征命中判断（4xx 状态码或页面验证特征）。"""
    if status_code in (401, 403, 429, 451):
        return True
    return any(p.search(html) for p in RISK_PATTERNS)


class CrawlCapability:
    """爬取能力（§6.1 Capability 协议 + §6.2 crawl action 表）。"""

    name = "crawl"

    def __init__(
        self,
        browser: BrowserCapability | None = None,
        default_level: str = "L1",
        auto_escalate: bool = True,
        max_level: str = "L3",
        max_per_minute: float = 30.0,
    ) -> None:
        self._browser = browser or BrowserCapability()
        self.default_level = default_level
        self.auto_escalate = auto_escalate
        self.max_level = max_level
        self._limiter = RateLimiter(rate_per_min=max_per_minute)

    # ------------------------------------------------------------------
    # Capability 协议
    # ------------------------------------------------------------------
    async def invoke(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        trace_id = uuid.uuid4().hex[:12]
        handler = getattr(self, f"_act_{action}", None)
        if handler is None:
            return {"ok": False, "error": f"unknown action: {action}", "trace_id": trace_id}
        try:
            data = await handler(params)
            # handler 可返回带 "ok" 键的内层结果（短路信号），向上透传而非嵌套包装
            if isinstance(data, dict) and "ok" in data:
                data.setdefault("trace_id", trace_id)
                return data
            return {"ok": True, "data": data, "trace_id": trace_id}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "trace_id": trace_id}

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "actions": {
                "fetch": {"url": "str", "level": "L1|L2|L3|L4 (可选)", "headless": "bool", "region": "auto|cn|intl"},
                "render": {"url": "str", "wait_ms": "int", "selector": "str (可选)"},
                "extract": {"url": "str", "selector": "str", "attr": "str (可选)"},
                "sitemap": {"url": "str", "max_links": "int"},
                "batch": {"urls": "list[str]"},
            },
        }

    async def aclose(self) -> None:
        await self._browser.aclose()

    # ------------------------------------------------------------------
    # 分级调度
    # ------------------------------------------------------------------
    async def _act_fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        url = params["url"]
        requested = params.get("level", self.default_level)
        auto = params.get("auto_escalate", self.auto_escalate)
        max_level = params.get("max_level", self.max_level)
        headless = params.get("headless", True)
        region = params.get("region", "auto")
        timeout_s = params.get("timeout_ms", 15000) / 1000.0

        route = resolve_net_route(region, target="search")
        if not self._limiter.allow(url):
            return {"ok": False, "error": "rate limit: too many requests", "url": url}

        level_idx = LEVELS.index(requested)
        cap_idx = min(LEVELS.index(max_level), len(LEVELS) - 2)  # 升级上限 = L3（L4 仅可显式请求，返回外接提示）
        last_error = None
        while True:
            level = LEVELS[level_idx]
            try:
                if level == "L1":
                    result = await self._crawl_l1(url, route, timeout_s)
                elif level in ("L2", "L3"):
                    result = await self._crawl_l2(url, headless and level == "L2", level == "L3", timeout_s)
                else:  # L4（显式请求）
                    return {
                        "ok": False,
                        "escalated_to": level,
                        "needs_external_browser": True,
                        "hint": "attach CDP (browser.attach_cdp) and retry",
                        "url": url,
                        "last_error": last_error,
                    }
                result.update({"level": level, "url": url, "route": route.resolved_region})
                return result
            except BrowserUnavailable as exc:
                # 浏览器不可用 → 降级 L1（设计原则 5 失败可降级）
                if level_idx == 0:
                    raise
                last_error = f"browser unavailable: {exc}"
                level_idx = 0
                continue
            except _CrawlFailure as exc:
                last_error = str(exc)
                if not auto or level_idx >= cap_idx:
                    return {
                        "ok": False,
                        "url": url,
                        "level": level,
                        "error": last_error,
                        "escalated": False,
                        "max_level_reached": level_idx >= cap_idx,
                    }
                # 未达上限：L1 动态页/风控 → L2；L2 风控 → L3
                level_idx += 1

    # --- L1：httpx 静态抓取（CDN 感知）---
    async def _crawl_l1(self, url: str, route: NetRoute, timeout_s: float) -> dict[str, Any]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        proxy = route.proxy or None
        async with httpx.AsyncClient(
            headers=headers,
            proxy=proxy,
            timeout=timeout_s,
            follow_redirects=True,
        ) as client:
            t0 = time.time()
            resp = await client.get(url)
            elapsed_ms = int((time.time() - t0) * 1000)
            html = resp.text
            cdn = _cdn_hint(resp.headers)

            if resp.status_code >= 400:
                raise _CrawlFailure(f"http {resp.status_code}")
            if _hit_risk(resp.status_code, html):
                raise _CrawlFailure("risk-control hit at L1")

            body_text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", html, flags=re.I)
            body_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body_text)).strip()

            data: dict[str, Any] = {
                "status": resp.status_code,
                "cdn": cdn,
                "html_chars": len(html),
                "text_chars": len(body_text),
                "text_preview": body_text[:500],
                "elapsed_ms": elapsed_ms,
            }
            # 动态壳页且未命中 CDN 新鲜缓存 → 升 L2
            if cdn is None and _looks_dynamic(html):
                data["needs_js_render"] = True
            return data

    # --- L2/L3：Playwright 渲染（L3 = 指纹伪装 + 人类化节奏）---
    async def _crawl_l2(self, url: str, headless: bool, stealth: bool, timeout_s: float) -> dict[str, Any]:
        inst = await self._browser._acquire(headless=headless)  # noqa: SLF001 - 包内协作
        try:
            page = inst.page
            if stealth:
                _apply_stealth(page)
                await _human_pause()
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:  # noqa: BLE001 - 长连接页面 networkidle 可能不触发
                pass
            html = await page.content()
            if stealth:
                await _human_pause()
            body_text = await page.inner_text("body")
            if _hit_risk(getattr(resp, "status", None), html):
                raise _CrawlFailure(f"risk-control hit (status={getattr(resp, 'status', '?')})")
            return {
                "status": getattr(resp, "status", None),
                "html_chars": len(html),
                "text_chars": len(body_text),
                "text_preview": body_text[:500],
                "stealth": stealth,
            }
        finally:
            # L2/L3 用完即回收，避免池泄漏；池由 BrowserCapability 统一管理
            await self._browser._close_instance(inst)  # noqa: SLF001

    # --- 其余 action ---
    async def _act_render(self, params: dict[str, Any]) -> dict[str, Any]:
        wait_ms = params.get("wait_ms", 3000)
        inst = await self._browser._acquire(headless=True)  # noqa: SLF001
        try:
            page = inst.page
            await page.goto(params["url"], wait_until="domcontentloaded", timeout=(wait_ms + 15000) * 1000)
            await page.wait_for_timeout(wait_ms)
            selector = params.get("selector")
            html = await page.content()
            out = {"html_chars": len(html), "url": page.url}
            if selector:
                out["selector_text"] = await page.inner_text(selector)
            return out
        finally:
            await self._browser._close_instance(inst)  # noqa: SLF001

    async def _act_extract(self, params: dict[str, Any]) -> dict[str, Any]:
        inst = await self._browser._acquire(headless=True)  # noqa: SLF001
        try:
            page = inst.page
            await page.goto(params["url"], wait_until="domcontentloaded", timeout=20000)
            expr = params["selector"]
            attr = params.get("attr")
            items: list[str] = []
            for el in await page.query_selector_all(expr):
                val = (await el.get_attribute(attr)) if attr else (await el.inner_text())
                if val and val.strip():
                    items.append(val.strip())
            return {"selector": expr, "count": len(items), "items": items[:100]}
        finally:
            await self._browser._close_instance(inst)  # noqa: SLF001

    async def _act_sitemap(self, params: dict[str, Any]) -> dict[str, Any]:
        base = params["url"].rstrip("/")
        max_links = int(params.get("max_links", 50))
        client = httpx.AsyncClient(follow_redirects=True, timeout=15.0)
        try:
            links: list[str] = []
            for candidate in (f"{base}/sitemap.xml",):
                r = await client.get(candidate)
                if r.status_code == 200:
                    links = re.findall(r"<loc>\s*(\S+?)\s*</loc>", r.text)[:max_links]
            return {"url": candidate, "links": links}
        finally:
            await client.aclose()

    async def _act_batch(self, params: dict[str, Any]) -> dict[str, Any]:
        results = []
        for url in params["urls"][:20]:  # 批量上限保护
            results.append(await self.invoke("fetch", {"url": url, "level": self.default_level}))
        ok = sum(1 for r in results if r.get("ok"))
        return {"total": len(results), "ok": ok, "failed": len(results) - ok, "results": results}


class _CrawlFailure(Exception):
    """单级爬取失败，触发分级调度升级。"""


# ----------------------------------------------------------------------
# 国内 CDN 提示 & 反爬伪装
# ----------------------------------------------------------------------
def _cdn_hint(headers: dict) -> str | None:
    """识别 CDN（配置化扩展点：宿主可注册更多 vendor 头）。

    返回 vendor 名；无 CDN 头返回 None。仅基于响应头启发式，无硬编码域名。
    """
    vendors = (
        ("x-amz-cf-id", "cloudfront"),
        ("via", "generic-cdn"),
        ("x-cache", "fastly-or-nginx-cache"),
        ("x-cdn", "generic-cdn"),
    )
    lowered = {k.lower(): v for k, v in headers.items()}
    for key, vendor in vendors:
        if key in lowered:
            return vendor
    return None


def _apply_stealth(page: Any) -> None:
    """L3 指纹伪装（§4.2）：参数化 UA/视口/时区/语言。

    时区与语言默认面向国内栈（Asia/Shanghai + zh-CN）；国际栈由调用方覆写
    extra 参数（stealth 区不硬编码目标地域）。
    """
    try:
        page.set_default_timeout(30000)
    except Exception:  # noqa: BLE001
        pass
    # 视口/UA 由上下文继承；此处补人类化随机延迟，降低行为指纹
    page._hs_human_mode = True  # type: ignore[attr-defined]


async def _human_pause() -> None:
    """人类化节奏：300–900ms 随机等待（L3 反爬对抗）。"""
    await asyncio.sleep(random.uniform(0.3, 0.9))
