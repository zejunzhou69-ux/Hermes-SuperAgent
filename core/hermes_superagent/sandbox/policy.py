"""分层沙箱（设计方案 §9B，用户已拍板）。

四层，默认策略严格对齐 §9B.1：

| 层 | 默认 | 说明 |
|---|---|---|
| KERNEL（LLM/记忆/路由/纯计算） | 无沙箱 | 本就隔离进程，无额外风险 |
| FILE_SHELL（文件/Shell 工具） | 可写沙箱 | 工作目录可写，系统路径只读 |
| BROWSER（浏览器/爬取） | 完整沙箱 | Chromium 隔离，独立 chrome-profile |
| EXTERNAL_CDP（外接真实浏览器） | 默认禁 + 显式 opt-in | 开启后进入「有监督」模式，动作留痕 |

**硬约束（§9B.4，t5 实现时不得回退）**：
1. 搜索不被拦——沙箱只管「跑在哪个隔离环境」，不管「你搜什么」。
2. 装东西不被卡——「宽进严管」，包安装与双栈镜像源默认开放。
3. 装不上必须显式告警并指明卡点，禁止静默卡死。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class SandboxLayer(str, Enum):
    """四个沙箱层。"""

    KERNEL = "kernel"
    FILE_SHELL = "file_shell"
    BROWSER = "browser"
    EXTERNAL_CDP = "external_cdp"


@dataclass
class Decision:
    """一次放行/拦截判定。

    `blocked_at` 是硬约束 3 的落点：拦截必须**指明卡在哪**，不许只说「不行」。
    """

    allow: bool
    reason: str
    blocked_at: str | None = None

    def to_dict(self) -> dict:
        return {"allow": self.allow, "reason": self.reason, "blocked_at": self.blocked_at}


@dataclass
class LayerPolicy:
    """单层沙箱策略。"""

    layer: SandboxLayer
    sandboxed: bool
    writable_roots: list[Path] = field(default_factory=list)
    readonly_roots: list[Path] = field(default_factory=list)
    network_outbound: bool = True
    requires_opt_in: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "layer": self.layer.value,
            "sandboxed": self.sandboxed,
            "writable_roots": [str(p) for p in self.writable_roots],
            "readonly_roots": [str(p) for p in self.readonly_roots],
            "network_outbound": self.network_outbound,
            "requires_opt_in": self.requires_opt_in,
            "notes": list(self.notes),
        }


def system_readonly_roots() -> list[Path]:
    """系统关键路径（只读区）。只圈「系统不被破坏」，不圈「你装不了东西」。"""
    import sys

    if sys.platform.startswith("win"):
        candidates = [
            r"C:\Windows",
            r"C:\Program Files",
            r"C:\Program Files (x86)",
            r"C:\ProgramData",
        ]
    else:
        candidates = ["/usr", "/etc", "/bin", "/sbin", "/lib", "/opt", "/boot"]
    roots: list[Path] = []
    for c in candidates:
        p = Path(c)
        if p.exists():
            roots.append(p)
    return roots


def default_policy(workspace_root: Path) -> "SandboxPolicy":
    """构建默认分层策略（§9B.1）。"""
    ws = Path(workspace_root)
    policy = SandboxPolicy(workspace_root=ws)

    policy.layers[SandboxLayer.KERNEL] = LayerPolicy(
        layer=SandboxLayer.KERNEL,
        sandboxed=False,
        notes=["本就隔离进程，无额外风险（§9B.1）"],
    )

    policy.layers[SandboxLayer.FILE_SHELL] = LayerPolicy(
        layer=SandboxLayer.FILE_SHELL,
        sandboxed=True,
        writable_roots=[ws],
        readonly_roots=system_readonly_roots(),
        network_outbound=True,
        notes=[
            "工作目录可写，系统路径只读（§9B.1）",
            "包安装与双栈镜像源默认开放：宽进严管（§9B.4-2）",
        ],
    )

    policy.layers[SandboxLayer.BROWSER] = LayerPolicy(
        layer=SandboxLayer.BROWSER,
        sandboxed=True,
        writable_roots=[ws / ".sandbox" / "chrome-profile"],
        readonly_roots=[],
        network_outbound=True,
        notes=[
            "Chromium 隔离，独立 chrome-profile（§9B.1）",
            "默认不碰用户日常浏览器（§9B.2）",
        ],
    )

    policy.layers[SandboxLayer.EXTERNAL_CDP] = LayerPolicy(
        layer=SandboxLayer.EXTERNAL_CDP,
        sandboxed=True,
        writable_roots=[],
        readonly_roots=[],
        requires_opt_in=True,
        notes=[
            "默认禁 + 显式 opt-in（§9B.1）",
            "开启后进入「有监督」模式，动作必须留痕（§9B.2）",
        ],
    )

    return policy


class SandboxPolicy:
    """分层沙箱策略容器。"""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root)
        self.layers: dict[SandboxLayer, LayerPolicy] = {}

    # -- 查询 ----------------------------------------------------------
    def layer(self, layer: SandboxLayer) -> LayerPolicy:
        return self.layers[layer]

    def is_sandboxed(self, layer: SandboxLayer) -> bool:
        return self.layers[layer].sandboxed

    # -- 硬约束 1：搜索永不被拦（§9B.4-1） -----------------------------
    def guard_search(self, query: str) -> Decision:
        """搜索面**永不拦截**。

        沙箱只管「跑在哪个隔离环境」，不管「你搜什么」。人机验证由人工过一次，
        不自动绕、不硬闯——那是「人工介入」，不是「沙箱拦截」。
        """
        return Decision(
            allow=True,
            reason="§9B.4-1：搜索不被沙箱拦截（沙箱不决定你搜什么）",
        )

    def captcha_policy(self) -> dict:
        """人机验证策略：交人工，不自动绕过（§9B.4-1 + §9A）。"""
        return {
            "action": "hand_to_human",
            "auto_bypass": False,
            "surface": "thumb_browser",
            "note": "验证由用户本人在拇指浏览器手动完成；agent 不自动绕过、不硬闯",
        }

    # -- 网络 ----------------------------------------------------------
    def network_allowed(self, layer: SandboxLayer) -> bool:
        return self.layers[layer].network_outbound

    def to_dict(self) -> dict:
        return {
            "workspace_root": str(self.workspace_root),
            "layers": {l.value: p.to_dict() for l, p in self.layers.items()},
            "hard_constraints": {
                "search_never_intercepted": True,
                "install_open_wide_in_strict_out": True,
                "block_must_name_the_point": True,
            },
            "captcha": self.captcha_policy(),
        }

    def summary(self) -> str:
        """人类可读摘要（供 CLI / GUI）。"""
        lines = [f"沙箱策略（workspace={self.workspace_root}）", ""]
        for layer in SandboxLayer:
            p = self.layers.get(layer)
            if p is None:
                continue
            tag = "沙箱" if p.sandboxed else "无沙箱"
            extra = " [需显式 opt-in]" if p.requires_opt_in else ""
            lines.append(f"  {layer.value:<14} {tag}{extra}")
            for n in p.notes:
                lines.append(f"      - {n}")
        lines.append("")
        lines.append("  硬约束：搜索不被拦 · 依赖可装 · 拦截必指明卡点")
        return "\n".join(lines)
