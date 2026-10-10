"""ModelRouter: task_class → route mapping + 429/超时自动降级 (design §4.3, §10 K1, §13.5).

魔改点 H5 的落地物：hermes-agent 内核模型调用改走本路由器。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .providers import (
    BaseProvider,
    ModelMessage,
    ModelResponse,
    ProviderError,
    build_default_providers,
)

#: 视频任务状态（get_video_task 返回）：这些值视为终态。
VIDEO_DONE: str = "completed"
VIDEO_FAILED: str = "failed"

log = logging.getLogger("hermes.modelrouter")


@dataclass
class RouteDecision:
    task_class: str
    primary: tuple[str, str]          # (route, model)
    fallbacks: tuple[tuple[str, str], ...] = field(default_factory=tuple)


# task_class 定义 (design §4.3): reasoning / coding / crawl-summarize / cheap
# 模型名以端点实际支持为准（agnes-ai.cn 端点小写：agnes-3.0-flash / agnes-2.5-flash）
DEFAULT_POLICY: dict[str, RouteDecision] = {
    "reasoning": RouteDecision(
        "reasoning",
        ("deepseek", "deepseek-reasoner"),
        fallbacks=(
            ("openai", "gpt-4o"),
            ("agnes-flash", "agnes-3.0-flash"),     # 额度策略：agnes 优先
            ("agnes-flash", "agnes-2.5-flash"),
            ("ds-reserve", "ds-v4.1-flash"),         # 仅兜底
            ("local", "qwen2.5:7b-instruct"),
        ),
    ),
    "coding": RouteDecision(
        "coding",
        ("deepseek", "deepseek-chat"),
        fallbacks=(
            ("openai", "gpt-4o"),
            ("agnes-flash", "agnes-3.0-flash"),
            ("agnes-flash", "agnes-2.5-flash"),
            ("ds-reserve", "ds-v4.1-flash"),
            ("local", "qwen2.5:7b-instruct"),
        ),
    ),
    "crawl-summarize": RouteDecision(
        "crawl-summarize",
        ("deepseek", "deepseek-chat"),
        fallbacks=(
            ("agnes-flash", "agnes-3.0-flash"),
            ("agnes-flash", "agnes-2.5-flash"),
            ("ds-reserve", "ds-v4.1-flash"),
            ("local", "qwen2.5:7b-instruct"),
        ),
    ),
    "cheap": RouteDecision(
        "cheap",
        ("agnes-flash", "agnes-3.0-flash"),
        fallbacks=(
            ("agnes-flash", "agnes-2.5-flash"),
            ("deepseek", "deepseek-chat"),
            ("local", "qwen2.5:7b-instruct"),
        ),
    ),
    # 视觉识图：多模态消息（image_url 块）必须走支持视觉的模型。
    # agnes-3.0-flash 端点实测支持 vision（usage.image_tokens），故 agnes 视觉主力，
    # 降级到其它能接 OpenAI 多模态 content 的路由。
    "vision": RouteDecision(
        "vision",
        ("agnes-flash", "agnes-3.0-flash"),
        fallbacks=(
            ("openai", "gpt-4o"),
            ("deepseek", "deepseek-chat"),
            ("agnes-flash", "agnes-2.5-flash"),
        ),
    ),
}

# 多模态生成模型名（可被 secrets.json 覆盖，见 MODEL_ENDPOINT_BINDINGS）：
# 生图 / 生视频走 agnes 的 images/generations 与异步 video/generations，
# 不走 chat/completions，故单独成档、不挂在 429 降级链上。
IMAGE_MODEL_DEFAULT: str = "agnes-image-2.5-flash"
VIDEO_MODEL_DEFAULT: str = "agnes-video-2.5-flash"

DEFAULT_ROUTE: str = "deepseek"  # 国内-主力，与宿主插件配置 models.default 对齐

# 未识别 task_class 的兜底决策：走 cheap 档（本地/Ollama 永远可达，K1）。
_FALLBACK_DECISION = RouteDecision(
    "cheap",
    ("agnes-flash", "agnes-3.0-flash"),
    fallbacks=(
        ("agnes-flash", "agnes-2.5-flash"),
        ("deepseek", "deepseek-chat"),
        ("local", "qwen2.5:7b-instruct"),
    ),
)


class ModelRouter:
    """按 task_class 选择路由；throttle/超时时沿 fallbacks 自动切换（K1）。

    降级事件必须可观测（design §9.3：绝不静默吞错）：所有切换写日志并计入
    `stats`，供 GUI 与 CrewAI 成本中间件读取。
    """

    def __init__(
        self,
        providers: Sequence[BaseProvider] | None = None,
        policy: dict[str, RouteDecision] | None = None,
        max_retries: int = 2,
        image_model: str | None = None,
        video_model: str | None = None,
    ):
        self.providers: dict[str, BaseProvider] = {}
        for p in providers if providers is not None else build_default_providers():
            self.providers[p.route] = p
        self.policy = policy or DEFAULT_POLICY
        self.max_retries = max_retries
        # K1 的"本地兜底"：local 路由永远排最后，任何 task_class 都可达。
        self.stats: dict[str, Any] = {"calls": 0, "throttled": 0, "switched": 0, "failed": 0}
        # 多模态生成模型名（可经 secrets.json 覆盖，见 MODEL_ENDPOINT_BINDINGS）
        self.image_model = image_model or IMAGE_MODEL_DEFAULT
        self.video_model = video_model or VIDEO_MODEL_DEFAULT
        # 多模态 429 退避：免费额度限流常见，指数退避 + 重试而非立即失败。
        self.mm_max_retries = 4          # 多模态专属重试次数（比文本档更多，因为生成慢）
        self.mm_backoff_base_s = 3.0     # 退避基数：3s → 6s → 12s → 24s

    # ------------------------------------------------------------------
    def _retry_on_throttle(self, fn, *args: Any, what: str = "op", **kw: Any) -> Any:
        """对同步 fn 做 429 指数退避重试（K1 延伸：多模态同样不撞墙即停）。"""
        import time as _t
        last: Exception | None = None
        for attempt in range(self.mm_max_retries):
            try:
                return fn(*args, **kw)
            except ProviderError as exc:
                last = exc
                if not exc.is_throttled:
                    raise  # 非限流错误立即抛（参数/鉴权等，退避无用）
                if attempt < self.mm_max_retries - 1:
                    wait = self.mm_backoff_base_s * (2 ** attempt)
                    log.warning("%s throttled (429); backoff %.0fs before retry %d/%d",
                                what, wait, attempt + 1, self.mm_max_retries)
                    _t.sleep(wait)
                    continue
            except Exception as exc:  # noqa: BLE001
                last = exc
                if isinstance(exc, ProviderError):
                    raise
                # 网络等瞬时错误也退避一次
                if attempt < self.mm_max_retries - 1:
                    _t.sleep(self.mm_backoff_base_s * (2 ** attempt))
                    continue
        self.stats["throttled"] += 1
        raise ProviderError(f"{what} still throttled after {self.mm_max_retries} retries: {last}")

    # ------------------------------------------------------------------
    def _agnes_provider(self) -> "ReserveFlashProvider":
        """取 agnes 多模态 provider（生图/生视频端点挂在它上面）。"""
        from .providers import ReserveFlashProvider

        p = self.providers.get("agnes-flash")
        if not isinstance(p, ReserveFlashProvider):
            raise ProviderError("agnes route not registered; 无法生图/生视频")
        return p

    def generate_image(self, prompt: str, size: str = "512x512", **kw: Any) -> dict[str, Any]:
        """文本模型 → 生图模型：prompt 交给 agnes-image，返回 {url, b64, ...}。

        429 自动指数退避重试（3/6/12/24s），不撞墙即停。
        """
        self.stats["calls"] += 1
        try:
            return self._retry_on_throttle(
                self._agnes_provider().generate_image,
                prompt, what="image-gen", model=self.image_model, size=size, **kw,
            )
        except Exception as exc:  # noqa: BLE001 - 失败统计，不中断
            self.stats["failed"] += 1
            raise exc

    def create_video(self, prompt: str, **kw: Any) -> str:
        """生视频：提交异步任务，返回 task_id（agnes 视频为任务制，不阻塞）。

        429 自动指数退避重试（3/6/12/24s），不撞墙即停。
        """
        self.stats["calls"] += 1
        try:
            return self._retry_on_throttle(
                self._agnes_provider().create_video_task,
                prompt, what="video-task", model=self.video_model, **kw,
            )
        except Exception as exc:  # noqa: BLE001
            self.stats["failed"] += 1
            raise exc

    def get_video(self, task_id: str) -> dict[str, Any]:
        """查异步视频任务状态 → {status, url, raw}。"""
        return self._agnes_provider().get_video_task(task_id)

    # ------------------------------------------------------------------
    def decide(self, task_class: str) -> RouteDecision:
        return self.policy.get(task_class, _FALLBACK_DECISION)

    # ------------------------------------------------------------------
    def complete(
        self,
        messages: Iterable[ModelMessage],
        task_class: str = "reasoning",
        **kw: Any,
    ) -> ModelResponse:
        decision = self.decide(task_class)
        chain = [decision.primary, *decision.fallbacks]
        last_error: Exception | None = None
        self.stats["calls"] += 1
        for i, (route, model) in enumerate(chain):
            provider = self.providers.get(route)
            if provider is None:
                log.warning("route %s not registered, skipping", route)
                continue
            attempts = 1 + self.max_retries if route == decision.primary[0] else 1
            for attempt in range(1, attempts + 1):
                started = time.monotonic()
                try:
                    resp = provider.complete(model, list(messages), **kw)
                    resp.latency_ms = int((time.monotonic() - started) * 1000)
                    if route != decision.primary[0]:
                        self.stats["switched"] += 1
                        log.warning(
                            "throttled/failed on primary %s; switched to %s/%s for task_class=%s",
                            decision.primary, route, model, task_class,
                        )
                    return resp
                except ProviderError as exc:
                    last_error = exc
                    if exc.is_throttled and route == decision.primary[0] and attempt < attempts:
                        log.warning("retry %s/%s on %s after throttle", attempt, attempts, route)
                        continue
                    self.stats["throttled"] += 1
                    break  # → 切下一个 fallback
                except Exception as exc:  # 网络/配置错误同样降级，不中断任务
                    last_error = exc
                    log.warning("route %s error: %s", route, exc)
                    self.stats["throttled"] += 1
                    break
        self.stats["failed"] += 1
        raise ProviderError(f"all routes failed for task_class={task_class}: {last_error}")

    # ------------------------------------------------------------------
    def route_info(self, task_class: str) -> dict[str, Any]:
        """`model.route_info` capability action (design §6.2)。"""
        d = self.decide(task_class)
        return {
            "task_class": task_class,
            "primary": {"route": d.primary[0], "model": d.primary[1]},
            "fallbacks": [{"route": r, "model": m} for r, m in d.fallbacks],
            "registered_routes": sorted(self.providers),
            "stats": self.stats,
        }
