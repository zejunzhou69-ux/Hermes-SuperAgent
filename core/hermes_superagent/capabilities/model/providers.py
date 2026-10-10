"""Four-route model adapter layer.

Design ref: docs/design.md §4.3 / §8 / §10(K1) / §13.5
- 国际-前沿: OpenAI / Anthropic
- 国际-备用: agnes-2.5-Flash / agnes-3.0-Flash (额度策略优先), ds-v4.1-flash (仅兜底)
- 国内-主力: DeepSeek
- 本地: Ollama / vLLM

All providers implement `BaseProvider` (sync httpx) so the router, tests
and hermes-agent's ModelRouter hook (魔改点 H5) share one interface.
"""
from __future__ import annotations

import abc
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx


@dataclass
class ModelMessage:
    """一条对话消息。

    content 两种形态：
    - str：纯文本（OpenAI 兼容接口直接透传）
    - list[dict]：OpenAI 多模态 content 块，例如视觉消息：
        [{"type":"text","text":"..."}, {"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}]
    """
    role: str  # "system" | "user" | "assistant"
    content: "str | list[dict[str, Any]]"


@dataclass
class ModelResponse:
    content: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)  # prompt_tokens / completion_tokens
    latency_ms: int = 0


class ProviderError(Exception):
    """Base error for provider failures (carries http status when known)."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def is_throttled(self) -> bool:
        return self.status in (408, 429, 503)


class BaseProvider(abc.ABC):
    """One model route. Implementations are stateless beyond an httpx client."""

    route: str
    tier: str  # "frontier" | "domestic" | "local"

    def __init__(self, route: str, tier: str, timeout_s: float = 120.0):
        self.route = route
        self.tier = tier
        self._timeout_s = timeout_s
        #: httpx.Client 懒建（见 `client`）——构造 httpx.Client 在 Windows 上约 0.75s，
        #: 五个 provider 会在装配 runtime 时白花 ~3.7s。首次真正发请求时才建。
        self._client: httpx.Client | None = None
        self._lock = threading.Lock()

    @property
    def client(self) -> httpx.Client:
        """懒加载的 httpx 客户端（首次使用时创建，线程安全）。"""
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = httpx.Client(timeout=self._timeout_s)
        return self._client

    # -- interface used by ModelRouter -------------------------------
    @abc.abstractmethod
    def complete(self, model: str, messages: Iterable[ModelMessage], **kw: Any) -> ModelResponse:
        """Non-streaming completion. Raises ProviderError on transport/throttle."""

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None


# ---------------------------------------------------------------------------
# OpenAI (国际-前沿)
# ---------------------------------------------------------------------------
@dataclass
class OpenAIConfig:
    api_key_env: str = "OPENAI_API_KEY"
    base_url: str = "https://api.openai.com/v1"
    default_model: str = "gpt-4o"
    models: tuple[str, ...] = ("gpt-4o", "gpt-4o-mini")


class OpenAIProvider(BaseProvider):
    route = "openai"
    tier = "frontier"

    def __init__(self, cfg: OpenAIConfig | None = None):
        super().__init__(self.route, self.tier)
        self.cfg = cfg or OpenAIConfig()

    def _headers(self) -> dict[str, str]:
        key = os.environ.get(self.cfg.api_key_env, "")
        if not key:
            raise ProviderError(f"missing credential: env {self.cfg.api_key_env} not set")
        return {"Authorization": f"Bearer {key}"}

    def complete(self, model: str, messages: Iterable[ModelMessage], **kw: Any) -> ModelResponse:
        payload = {"model": model, "messages": [m.__dict__ for m in messages], **kw}
        r = self.client.post(
            f"{self.cfg.base_url}/chat/completions",
            headers=self._headers(),
            json=payload,
        )
        if r.status_code >= 400:
            raise ProviderError(f"openai {r.status_code}: {r.text[:300]}", status=r.status_code)
        data = r.json()
        return ModelResponse(
            content=data["choices"][0]["message"]["content"],
            model=data.get("model", model),
            usage=data.get("usage") or {},
        )


# ---------------------------------------------------------------------------
# Anthropic (国际-前沿)
# ---------------------------------------------------------------------------
@dataclass
class AnthropicConfig:
    api_key_env: str = "ANTHROPIC_API_KEY"
    base_url: str = "https://api.anthropic.com"
    default_model: str = "claude-sonnet-4-5"
    api_version: str = "2023-06-01"
    models: tuple[str, ...] = ("claude-sonnet-4-5", "claude-haiku-4-5")


class AnthropicProvider(BaseProvider):
    route = "anthropic"
    tier = "frontier"

    def __init__(self, cfg: AnthropicConfig | None = None):
        super().__init__(self.route, self.tier)
        self.cfg = cfg or AnthropicConfig()

    def _headers(self) -> dict[str, str]:
        key = os.environ.get(self.cfg.api_key_env, "")
        if not key:
            raise ProviderError(f"missing credential: env {self.cfg.api_key_env} not set")
        return {
            "x-api-key": key,
            "anthropic-version": self.cfg.api_version,
            "content-type": "application/json",
        }

    def complete(self, model: str, messages: Iterable[ModelMessage], **kw: Any) -> ModelResponse:
        msgs = list(messages)
        system = "\n".join(m.content for m in msgs if m.role == "system")
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": kw.pop("max_tokens", 4096),
            "messages": [m.__dict__ for m in msgs if m.role in ("user", "assistant")],
        }
        if system:
            body["system"] = system
        body.update(kw)
        r = self.client.post(f"{self.cfg.base_url}/v1/messages", headers=self._headers(), json=body)
        if r.status_code >= 400:
            raise ProviderError(f"anthropic {r.status_code}: {r.text[:300]}", status=r.status_code)
        data = r.json()
        return ModelResponse(
            content="".join(b.get("text", "") for b in data.get("content", [])),
            model=data.get("model", model),
            usage=data.get("usage") or {},
        )


# ---------------------------------------------------------------------------
# DeepSeek (国内-主力)
# ---------------------------------------------------------------------------
@dataclass
class DeepSeekConfig:
    api_key_env: str = "DEEPSEEK_API_KEY"
    base_url: str = "https://api.deepseek.com/v1"
    default_model: str = "deepseek-chat"
    models: tuple[str, ...] = ("deepseek-chat", "deepseek-reasoner")


class DeepSeekProvider(BaseProvider):
    """DeepSeek 使用 OpenAI 兼容协议，直接复用 OpenAI 客户端语义。"""

    route = "deepseek"
    tier = "domestic"

    def __init__(self, cfg: DeepSeekConfig | None = None):
        super().__init__(self.route, self.tier)
        self.cfg = cfg or DeepSeekConfig()

    def _headers(self) -> dict[str, str]:
        key = os.environ.get(self.cfg.api_key_env, "")
        if not key:
            raise ProviderError(f"missing credential: env {self.cfg.api_key_env} not set")
        return {"Authorization": f"Bearer {key}"}

    def complete(self, model: str, messages: Iterable[ModelMessage], **kw: Any) -> ModelResponse:
        payload = {"model": model, "messages": [m.__dict__ for m in messages], **kw}
        r = self.client.post(
            f"{self.cfg.base_url}/chat/completions",
            headers=self._headers(),
            json=payload,
        )
        if r.status_code >= 400:
            raise ProviderError(f"deepseek {r.status_code}: {r.text[:300]}", status=r.status_code)
        data = r.json()
        return ModelResponse(
            content=data["choices"][0]["message"]["content"],
            model=data.get("model", model),
            usage=data.get("usage") or {},
        )


# ---------------------------------------------------------------------------
# 国际-备用路由 (agnes / 兜底 v4.1)
# ---------------------------------------------------------------------------
@dataclass
class ReserveConfig:
    """agnes-2.5-Flash / agnes-3.0-Flash / ds-v4.1-flash。

    额度策略（design §13.5）：v4.1 只作兜底，router 按顺序尝试 agnes 优先。
    端点/密钥走环境变量，避免硬编码域名。
    """

    base_url_env: str = "AGNES_BASE_URL"
    api_key_env: str = "AGNES_API_KEY"
    default_model: str = "agnes-2.5-Flash"
    models: tuple[str, ...] = ("agnes-2.5-Flash", "agnes-3.0-Flash")


class ReserveFlashProvider(BaseProvider):
    """OpenAI 兼容的备用 flash 路由。"""

    route = "agnes-flash"
    tier = "frontier"  # 国际-备用，与前沿同档以便 429 无缝切换

    def __init__(self, cfg: ReserveConfig | None = None):
        super().__init__(self.route, self.tier)
        self.cfg = cfg or ReserveConfig()

    def _base_url(self) -> str:
        url = os.environ.get(self.cfg.base_url_env, "")
        if not url:
            raise ProviderError(f"missing credential: env {self.cfg.base_url_env} not set")
        return url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        key = os.environ.get(self.cfg.api_key_env, "")
        if not key:
            raise ProviderError(f"missing credential: env {self.cfg.api_key_env} not set")
        return {"Authorization": f"Bearer {key}"}

    def complete(self, model: str, messages: Iterable[ModelMessage], **kw: Any) -> ModelResponse:
        """OpenAI 兼容 chat/completions。messages 支持多模态 content（视觉识图，design H5+）。

        响应里 message.content 可能是 str（纯文本）或 list（多模态输出）；统一拍平成文本。
        """
        payload = {"model": model, "messages": [m.__dict__ for m in messages], **kw}
        r = self.client.post(f"{self._base_url()}/chat/completions", headers=self._headers(), json=payload)
        if r.status_code >= 400:
            raise ProviderError(f"agnes-flash {r.status_code}: {r.text[:300]}", status=r.status_code)
        data = r.json()
        content = data["choices"][0]["message"].get("content")
        if isinstance(content, list):  # 多模态 content 块
            content = "".join(
                b.get("text", "") for b in content if isinstance(b, dict)
            )
        return ModelResponse(
            content=content or "",
            model=data.get("model", model),
            usage=data.get("usage") or {},
        )

    # -- 多模态生成（agnes-image / agnes-video，OpenAI 兼容 images/generations + 异步视频任务）--
    def generate_image(self, prompt: str, model: str = "agnes-image-2.5-flash",
                      size: str = "512x512", **kw: Any) -> dict[str, Any]:
        """POST {base}/images/generations → 返回 {url, b64_json, revised_prompt}。"""
        payload = {"model": model, "prompt": prompt, "n": 1, "size": size, **kw}
        r = self.client.post(f"{self._base_url()}/images/generations", headers=self._headers(), json=payload)
        if r.status_code >= 400:
            raise ProviderError(f"agnes image {r.status_code}: {r.text[:300]}", status=r.status_code)
        data = r.json()
        item = (data.get("data") or [{}])[0]
        return {
            "url": item.get("url", ""),
            "b64": item.get("b64_json", ""),
            "revised_prompt": item.get("revised_prompt", ""),
            "usage": data.get("usage") or {},
        }

    def create_video_task(self, prompt: str, model: str = "agnes-video-2.5-flash",
                          mode: str = "text2video", **kw: Any) -> str:
        """POST {base}/video/generations → 返回异步 task_id（agnes 视频为任务制）。

        mode 必填（端点校验，文生视频）。实测 429 免费限流下 text2v/t2v/1/2/3 通过校验，
        正常额度若报 "invalid mode" 可经 kw 换别名。429 是免费额度限流，非代码问题。
        """
        payload = {"model": model, "prompt": prompt, "mode": mode, **kw}
        r = self.client.post(f"{self._base_url()}/video/generations", headers=self._headers(), json=payload)
        if r.status_code >= 400:
            raise ProviderError(f"agnes video {r.status_code}: {r.text[:300]}", status=r.status_code)
        data = r.json()
        task_id = data.get("task_id") or (data.get("data") or {}).get("task_id") or data.get("id")
        if not task_id:
            raise ProviderError(f"agnes video: no task_id in response: {str(data)[:300]}")
        return str(task_id)

    def get_video_task(self, task_id: str) -> dict[str, Any]:
        """GET {base}/video/generations/{task_id} → 任务状态 + 完成后的视频 url。

        返回 {status, url, raw}；status ∈ queued/processing/completed/failed/...。
        """
        r = self.client.get(f"{self._base_url()}/video/generations/{task_id}", headers=self._headers())
        if r.status_code >= 400:
            raise ProviderError(f"agnes video task {r.status_code}: {r.text[:300]}", status=r.status_code)
        data = r.json()
        node = data.get("data") if isinstance(data.get("data"), dict) else data
        status = str(node.get("status") or node.get("state") or "").lower()
        url = node.get("output") or node.get("url") or node.get("video_url") or ""
        if not url and isinstance(node.get("output"), dict):
            url = node["output"].get("url", "")
        return {"status": status or "unknown", "url": url, "raw": data}


# ---------------------------------------------------------------------------
# 本地 (Ollama / vLLM，离线兜底)
# ---------------------------------------------------------------------------
@dataclass
class OllamaConfig:
    base_url_env: str = "OLLAMA_BASE_URL"
    default_base_url: str = "http://127.0.0.1:11434"
    default_model: str = "qwen2.5:7b-instruct"


class OllamaProvider(BaseProvider):
    """Ollama (OpenAI 兼容 /api/chat 与 /v1/chat/completions) 与 vLLM 双形态。

    本地无密钥；base_url 可配（vLLM 常用 :8000/v1，Ollama 默认 :11434）。
    """

    route = "local"
    tier = "local"

    def __init__(self, cfg: OllamaConfig | None = None):
        super().__init__(self.route, self.tier)
        self.cfg = cfg or OllamaConfig()

    def _base_url(self) -> str:
        return os.environ.get(self.cfg.base_url_env, self.cfg.default_base_url).rstrip("/")

    def complete(self, model: str, messages: Iterable[ModelMessage], **kw: Any) -> ModelResponse:
        payload = {"model": model, "messages": [m.__dict__ for m in messages], **kw}
        r = self.client.post(f"{self._base_url()}/api/chat", json=payload)
        if r.status_code >= 400:
            raise ProviderError(f"ollama {r.status_code}: {r.text[:300]}", status=r.status_code)
        data = r.json()
        return ModelResponse(
            content=data.get("message", {}).get("content", ""),
            model=model,
            usage={"prompt_tokens": data.get("prompt_eval_count", 0),
                   "completion_tokens": data.get("eval_count", 0)},
        )


def build_default_providers() -> list[BaseProvider]:
    """工厂：按 design §4.3 四路装配。密钥缺失时仍装配（调用时才报错），
    以便 router 可达性探测与 429 切换不依赖全部密钥齐备。"""
    return [
        OpenAIProvider(),
        AnthropicProvider(),
        DeepSeekProvider(),
        ReserveFlashProvider(),
        OllamaProvider(),
    ]
