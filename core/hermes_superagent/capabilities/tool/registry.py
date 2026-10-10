"""工具注册表 (design §4.3 工具接入 / 魔改点 H1 / §9A.4 合规拒绝).

- 统一 MCP 协议暴露工具：DSH 侧与 Python 侧都能挂载
- 内置工具命名空间：browser.* / crawl.* / search.* / fs.* / shell.* / code.*
- 第三方工具（飞书/钉钉/语雀/Gitee/GitHub/Slack/Notion）走适配器目录，不写死核心
- 合规红线 (§9A.4)：涉及「绕检测/伪装真人」的工具入口在 Capability 层显式拒绝并返回原因
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

# Tool handler: 同步或异步皆可，registry 统一 await
ToolHandler = Callable[[dict[str, Any]], Any]

# 内置工具命名空间白名单（design §4.3）
BUILTIN_NAMESPACES: tuple[str, ...] = ("browser", "crawl", "search", "fs", "shell", "code")

# 第三方适配器目录：name → 是否可用（国内/国际双栈）。
# 真正的实现在 adapters/ 下各自目录，这里只登记元数据，核心不依赖。
THIRD_PARTY_ADAPTERS: dict[str, dict[str, str]] = {
    # 国内
    "feishu": {"region": "cn", "tools": "feishu.send_card,feishu.read_doc", "compliance": "官方开放平台 API"},
    "dingtalk": {"region": "cn", "tools": "dingtalk.send_robot_msg", "compliance": "官方 API"},
    "yuque": {"region": "cn", "tools": "yuque.read_doc,yuque.search", "compliance": "官方 OpenAPI"},
    "gitee": {"region": "cn", "tools": "gitee.repo_clone,gitee.list", "compliance": "官方 + 镜像加速"},
    # 国际
    "github": {"region": "intl", "tools": "github.pr_list,github.issue_create", "compliance": "官方 GraphQL/REST"},
    "slack": {"region": "intl", "tools": "slack.post_msg", "compliance": "官方 Web API"},
    "notion": {"region": "intl", "tools": "notion.page_read,notion.page_create", "compliance": "官方 API"},
}

# 合规红线 (§9A.4)：这些能力入口一律拒绝，显式返回原因，可观测、不静默。
REJECTED_TOOLS: dict[str, str] = {
    "wechat.personal_send": "微信个人号外挂违反红线 §9A，仅支持企业微信官方 API（wechat.enterprise.*）",
    "whatsapp.personal_send": "WhatsApp 个人号非官方协议违反红线 §9A，仅支持 WhatsApp Business Platform",
    "facebook.personal_auto": "Facebook 个人账号自动化违反红线 §9A，仅支持 Page/群组 Meta Graph API",
    "browser.fake_human": "「伪装真人绕检测」本项目明确不实现（§9A.2），请走官方 API",
}


@dataclass
class ToolSpec:
    name: str  # 带命名空间前缀，如 "crawl.fetch"
    namespace: str
    description: str
    handler: ToolHandler | None = None
    schema: dict[str, Any] = field(default_factory=dict)
    third_party: bool = False
    compliance: str = ""  # 线 B 连接器必须声明的官方 API 来源（§9A.4）


class ToolRegistry:
    """H1 的落地：工具注册表接入 Capability 协议。

    - `register` 支持热插拔（新工具不重启核心）
    - `list` / `call` 即 `tool` Capability 的两个 action（design §6.2）
    - `to_mcp_tools` 导出 MCP 协议形态，供 DSH 侧挂载
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    # -- registration -------------------------------------------------
    def register(
        self,
        name: str,
        description: str = "",
        handler: ToolHandler | None = None,
        schema: dict[str, Any] | None = None,
        *,
        third_party: bool = False,
        compliance: str = "",
    ) -> ToolSpec:
        namespace = name.split(".", 1)[0]
        if not third_party and namespace not in BUILTIN_NAMESPACES:
            raise ValueError(
                f"namespace {namespace!r} not in builtin {BUILTIN_NAMESPACES}; "
                "第三方工具必须 third_party=True 并声明 compliance 来源"
            )
        if third_party and not compliance:
            raise ValueError(f"third-party tool {name!r} 必须声明官方 API 来源 (compliance)")
        spec = ToolSpec(name, namespace, description, handler, schema or {}, third_party, compliance)
        self._tools[name] = spec
        return spec

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    # -- Capability: list / call --------------------------------------
    def list(self) -> list[dict[str, Any]]:
        out = []
        for t in self._tools.values():
            out.append({
                "name": t.name,
                "namespace": t.namespace,
                "description": t.description,
                "third_party": t.third_party,
                "compliance": t.compliance,
            })
        return sorted(out, key=lambda d: d["name"])

    async def call(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        # 合规红线优先：拒绝的工具在 Capability 层显式打回（§9A.4）
        if name in REJECTED_TOOLS:
            return {"ok": False, "data": None,
                    "error": {"code": "compliance_rejected", "message": REJECTED_TOOLS[name]}}
        spec = self._tools.get(name)
        if spec is None:
            return {"ok": False, "data": None, "error": {"code": "unknown_tool", "message": name}}
        if spec.handler is None:
            return {"ok": False, "data": None,
                    "error": {"code": "not_implemented", "message": f"{name} 未绑定 handler（骨架占位）"}}
        try:
            result = spec.handler(params)
            if isinstance(result, Awaitable):
                result = await result
            return {"ok": True, "data": result, "error": None}
        except Exception as exc:  # 工具异常也结构化返回，不静默吞错
            return {"ok": False, "data": None, "error": {"code": "tool_error", "message": str(exc)}}

    # -- MCP 导出 (design §4.3: 统一 MCP 协议暴露工具) ----------------
    def to_mcp_tools(self) -> list[dict[str, Any]]:
        """MCP `tools` 列表形态，DSH 宿主与 Python 内核两侧均可挂载。"""
        return [
            {
                "name": t.name,
                "description": t.description or t.name,
                "inputSchema": t.schema or {"type": "object", "properties": {}},
                "_meta": {"third_party": t.third_party, "compliance": t.compliance,
                          "namespace": t.namespace},
            }
            for t in self._tools.values()
        ]

    def load_builtin_skeleton(self) -> None:
        """按 design §4.3 内置工具命名空间登记占位 spec（handler 由 t2 等任务填充）。"""
        for ns in BUILTIN_NAMESPACES:
            self.register(f"{ns}.*", f"内置 {ns} 命名空间占位（handler 由对应任务实现）")


def build_default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.load_builtin_skeleton()
    return reg


def registry_to_json_string(reg: ToolRegistry) -> str:
    return json.dumps(reg.list(), ensure_ascii=False)
