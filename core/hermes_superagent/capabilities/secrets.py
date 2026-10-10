"""密钥管理 (design §8 K7 敏感数据外泄 / §6.3 token 不落盘明文日志).

三层密钥来源，优先级从高到低：
1. 环境变量（OPENAI_API_KEY 等，由 provider 直接读取）
2. 本地密钥文件 `~/.hermes-superagent/secrets.json`（权限 0600，git 忽略）
3. 宿主 DSH 配置 `hermesSuperagent.models.*`（经 Bridge 下发，不落盘）

设计原则：
- 进程内只保存引用（env 名 / 文件键名），不落盘明文日志
- `resolve(key)` 只返回密钥值给 caller，不打印、不写 trace
- 日志/trace 中一律脱敏为 `<set>` / `<missing>`
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("hermes.secrets")

# 默认密钥文件路径（用户可覆盖）
_DEFAULT_SECRETS_FILE = Path.home() / ".hermes-superagent" / "secrets.json"


@dataclass
class SecretBinding:
    """一个逻辑密钥名 → 多个来源。resolve 按优先级取第一个存在的。"""
    name: str
    env_var: str
    file_key: str | None = None  # secrets.json 中的键
    host_key: str | None = None  # 宿主 DSH 配置下发的键


# 内置密钥绑定（四路模型 + agnes + 本地无需密钥）
DEFAULT_BINDINGS: tuple[SecretBinding, ...] = (
    SecretBinding("openai", "OPENAI_API_KEY", "openai.api_key", "models.openai.key"),
    SecretBinding("anthropic", "ANTHROPIC_API_KEY", "anthropic.api_key", "models.anthropic.key"),
    SecretBinding("deepseek", "DEEPSEEK_API_KEY", "deepseek.api_key", "models.deepseek.key"),
    SecretBinding("agnes", "AGNES_API_KEY", "agnes.api_key", "models.agnes.key"),
)

#: 端点（base_url）绑定：非密钥，但 provider 依赖环境变量取端点；
#: 同样支持 环境变量 > 密钥文件 > 宿主配置 三层来源，export_env 一并灌给 provider。
ENDPOINT_BINDINGS: tuple[SecretBinding, ...] = (
    SecretBinding("agnes_base_url", "AGNES_BASE_URL", "agnes.base_url", "models.agnes.base_url"),
    SecretBinding("openai_base_url", "OPENAI_BASE_URL", "openai.base_url", "models.openai.base_url"),
    SecretBinding("deepseek_base_url", "DEEPSEEK_BASE_URL", "deepseek.base_url", "models.deepseek.base_url"),
)

#: 多模态模型名绑定（非密钥）：生图/生视频模型 id 走 secrets.json 可改，
#: provider 经 MODEL_ENDPOINT_ENV 读 env 覆盖默认模型名（agnes-image / agnes-video）。
MODEL_ENDPOINT_BINDINGS: tuple[SecretBinding, ...] = (
    SecretBinding("agnes_image_model", "AGNES_IMAGE_MODEL", "agnes.image_model", "models.agnes.image_model"),
    SecretBinding("agnes_video_model", "AGNES_VIDEO_MODEL", "agnes.video_model", "models.agnes.video_model"),
)


class SecretsManager:
    def __init__(self, secrets_file: Path | None = None, host_secrets: dict[str, Any] | None = None):
        self.secrets_file = secrets_file or _DEFAULT_SECRETS_FILE
        self._host_secrets = host_secrets or {}
        self._file_cache: dict[str, str] | None = None

    # -- 来源读取 ------------------------------------------------------
    def _load_file(self) -> dict[str, str]:
        if self._file_cache is not None:
            return self._file_cache
        if not self.secrets_file.exists():
            self._file_cache = {}
            return self._file_cache
        try:
            # utf-8-sig 兼容 PowerShell `Set-Content -Encoding utf8` 写出的 BOM 文件
            data = json.loads(self.secrets_file.read_text(encoding="utf-8-sig"))
            self._file_cache = {k: str(v) for k, v in data.items() if isinstance(v, (str, int))}
        except Exception as exc:
            log.warning("secrets file unreadable (%s); falling back to env only", exc)
            self._file_cache = {}
        return self._file_cache

    def _get_from_host(self, binding: SecretBinding) -> str | None:
        if not binding.host_key:
            return None
        node: Any = self._host_secrets
        for part in binding.host_key.split("."):
            if not isinstance(node, dict):
                return None
            node = node.get(part)
        return node if isinstance(node, str) and node else None

    def _get_from_file(self, binding: SecretBinding) -> str | None:
        if not binding.file_key:
            return None
        return self._load_file().get(binding.file_key)

    # -- 对外接口 ------------------------------------------------------
    def resolve(self, name: str) -> str | None:
        """按 环境变量 > 密钥文件 > 宿主配置 返回密钥/端点；取不到返回 None。"""
        binding = self._binding_for(name)
        if binding is None:
            return None
        val = os.environ.get(binding.env_var)
        if val:
            return val
        val = self._get_from_file(binding)
        if val:
            return val
        return self._get_from_host(binding)

    def _binding_for(self, name: str) -> SecretBinding | None:
        for b in (*DEFAULT_BINDINGS, *ENDPOINT_BINDINGS, *MODEL_ENDPOINT_BINDINGS):
            if b.name == name:
                return b
        return None

    def status(self) -> dict[str, dict[str, Any]]:
        """脱敏状态（密钥 + 端点 + 模型名），供 GUI/trace 展示：每个绑定在哪层可用。"""
        out: dict[str, dict[str, Any]] = {}
        for b in (*DEFAULT_BINDINGS, *ENDPOINT_BINDINGS, *MODEL_ENDPOINT_BINDINGS):
            out[b.name] = {
                "env": "set" if os.environ.get(b.env_var) else "missing",
                "file": "set" if self._get_from_file(b) else "missing",
                "host": "set" if self._get_from_host(b) else "missing",
                "resolved": "set" if self.resolve(b.name) else "missing",
            }
        return out

    def read_file(self) -> dict[str, str]:
        """读密钥文件全文（供 GUI 设置面板回填表单）。"""
        if not self.secrets_file.exists():
            return {}
        try:
            data = json.loads(self.secrets_file.read_text(encoding="utf-8-sig"))
            return {k: str(v) for k, v in data.items() if isinstance(v, (str, int))}
        except Exception:
            return {}

    def save_file(self, values: dict[str, str]) -> None:
        """把 GUI 设置面板的值写入密钥文件（0600 权限，git 忽略）。"""
        self.secrets_file.parent.mkdir(parents=True, exist_ok=True)
        merged = self.read_file()
        merged.update({k: str(v) for k, v in values.items()})
        self.secrets_file.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # Windows 下 chmod 无效，仅 POSIX 生效；尝试不报错
        try:
            os.chmod(self.secrets_file, 0o600)
        except (OSError, NotImplementedError):
            pass
        # 失效缓存，下次 resolve 重读
        self._file_cache = None

    def export_env(self) -> dict[str, str]:
        """把已解析密钥 + 端点 + 模型名导出为环境变量字典（provider 直接读 env）。"""
        env: dict[str, str] = {}
        for b in (*DEFAULT_BINDINGS, *ENDPOINT_BINDINGS, *MODEL_ENDPOINT_BINDINGS):
            val = self.resolve(b.name)
            if val:
                env[b.env_var] = val
        return env

    def export_env_to_process(self) -> dict[str, str]:
        """把 export_env 的结果直接写入当前进程 os.environ（只补缺，不覆盖已有 env）。"""
        injected: dict[str, str] = {}
        for k, v in self.export_env().items():
            if not os.environ.get(k):
                os.environ[k] = v
                injected[k] = v
        return injected
