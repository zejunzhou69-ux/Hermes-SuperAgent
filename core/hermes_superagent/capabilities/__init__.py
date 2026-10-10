"""hermes-superagent core capabilities package.

布局 (design §4 / §7):
- capabilities/model : 四路模型适配层 + ModelRouter + model Capability
- capabilities/tool  : 工具注册表 + MCP 接入 + tool Capability
- capabilities/secrets: 密钥管理
"""
from .model import ModelCapability, ModelRouter
from .tool import ToolCapability, ToolRegistry, build_default_registry
from .secrets import SecretsManager

__all__ = [
    "ModelCapability",
    "ModelRouter",
    "SecretsManager",
    "ToolCapability",
    "ToolRegistry",
    "build_default_registry",
]
