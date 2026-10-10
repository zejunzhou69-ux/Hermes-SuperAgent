"""双栈网络路由（设计方案 §8 国内网络适配矩阵）。

国内/国际是**同构的两条路由**，全部配置驱动，无硬编码域名。
region="auto" 时做轻量探测；探测失败不阻塞，降级并告警。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class NetRoute:
    """一条网络出口路由（代理/镜像/搜索/浏览器下载源）。"""

    region: str  # "cn" | "intl" | "auto"
    resolved_region: str = "intl"
    proxy: str | None = None
    notes: list[str] = field(default_factory=list)
    # 各下游消费这些键：pypi/npm 镜像、搜索端点、浏览器下载源（均配置化）
    extra: dict[str, Any] = field(default_factory=dict)


def _env_cfg(prefix: str, key: str, default: str | None = None) -> str | None:
    return os.environ.get(f"HERMES_SUPERAGENT_{prefix}_{key}", default)


def resolve_net_route(region: str = "auto", target: str | None = None) -> NetRoute:
    """解析 region：显式值直接用；auto 时按探测结果或默认 intl。

    target（可选）：指明用途（pypi/npm/search/browser），仅用于附加配置提示。
    探测：当前实现读环境变量 HERMES_SUPERAGENT_CN_REACHABLE（由宿主层启动探测写入），
    保持本模块零 I/O、可单测。
    """
    if region in ("cn", "intl"):
        resolved = region
    else:
        reachable = _env_cfg("NET", "CN_REACHABLE", "0")
        resolved = "cn" if reachable in ("1", "true") else "intl"

    route = NetRoute(region=region, resolved_region=resolved)
    proxy = _env_cfg("NET", "PROXY")
    if proxy:
        route.proxy = proxy
        route.notes.append(f"proxy={proxy}")

    _apply_target_extra(route, target)
    return route


def _apply_target_extra(route: NetRoute, target: str | None) -> None:
    """把 env 配置注入 route.extra，供 httpx/Playwright/安装脚本消费。

    镜像/代理全部来自环境变量（HERMES_SUPERAGENT_MIRRORS_*），
    宿主层负责按探测结果写入；本层不硬编码任何域名。
    """
    if target is None:
        return
    mirrors_key = {
        "pypi": "PIP_INDEX_URL",
        "npm": "NPM_REGISTRY",
        "browser": "PLAYWRIGHT_DOWNLOAD_HOST",
        "search": "SEARCH_ENDPOINT",
    }.get(target)
    if mirrors_key:
        val = _env_cfg("MIRRORS", mirrors_key)
        if val:
            route.extra[target] = val
            route.notes.append(f"{target} mirror from config")


class RateLimiter:
    """极简令牌桶（每目标域），保证爬取限速（K8 合规：限速 + 可审计）。"""

    def __init__(self, rate_per_min: float = 30.0, burst: int = 5) -> None:
        import time as _t

        self.rate = rate_per_min
        self.burst = burst
        self._t = _t
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        now = self._t.time()
        tokens, last = self._buckets.get(key, (float(self.burst), now))
        tokens = min(float(self.burst), tokens + (now - last) * self.rate / 60.0)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1.0, now)
        return True
