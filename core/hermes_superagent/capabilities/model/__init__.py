"""`model` capability package: 四路模型适配层 + ModelRouter + Capability 包装。

模块布局 (design §4.3):
- providers.py : OpenAI / Anthropic / DeepSeek / agnes-flash 备用 / Ollama 本地
- router.py    : task_class → route 映射 + 429/超时自动降级 (K1)
- capability.py: `model` Capability Protocol 实现 (complete / route_info)
"""
from .capability import ModelCapability
from .providers import (
    AnthropicProvider,
    BaseProvider,
    DeepSeekProvider,
    ModelMessage,
    ModelResponse,
    OllamaProvider,
    OpenAIProvider,
    ProviderError,
    ReserveFlashProvider,
    build_default_providers,
)
from .router import (
    DEFAULT_POLICY,
    IMAGE_MODEL_DEFAULT,
    ModelRouter,
    RouteDecision,
    VIDEO_DONE,
    VIDEO_FAILED,
    VIDEO_MODEL_DEFAULT,
)

__all__ = [
    "AnthropicProvider",
    "BaseProvider",
    "DEFAULT_POLICY",
    "DeepSeekProvider",
    "IMAGE_MODEL_DEFAULT",
    "ModelCapability",
    "ModelMessage",
    "ModelResponse",
    "ModelRouter",
    "OllamaProvider",
    "OpenAIProvider",
    "ProviderError",
    "ReserveFlashProvider",
    "RouteDecision",
    "VIDEO_DONE",
    "VIDEO_FAILED",
    "VIDEO_MODEL_DEFAULT",
    "build_default_providers",
]
