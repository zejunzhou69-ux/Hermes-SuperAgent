"""分层沙箱（设计方案 §9B / §9B.4 / §13.3）。

对外接口：

    from hermes_superagent.sandbox import build_sandbox

    sb = build_sandbox(workspace_root)
    sb.policy.summary()                 # 人类可读策略
    sb.fs.check_write("C:/Windows/x")   # → Decision(allow=False, blocked_at=...)
    sb.policy.guard_search("任意词")     # → 永远 allow（硬约束 1）
    sb.browser.enable_cdp(...)          # 显式 opt-in（默认禁）
    sb.install.report()                 # 安装通道体检（硬约束 2/3）
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .browser_sandbox import BrowserSandbox, BrowserSandboxConfig
from .fs_guard import FsGuard
from .install_diagnostics import InstallCheck, InstallDiagnostics
from .policy import (
    Decision,
    LayerPolicy,
    SandboxLayer,
    SandboxPolicy,
    default_policy,
    system_readonly_roots,
)


@dataclass
class Sandbox:
    """一个 workspace 对应的一整套沙箱组件。"""

    policy: SandboxPolicy
    fs: FsGuard
    browser: BrowserSandbox
    install: InstallDiagnostics

    def to_dict(self) -> dict:
        return {
            "policy": self.policy.to_dict(),
            "browser": self.browser.report(),
            "install": self.install.report(),
        }


def build_sandbox(workspace_root: str | Path) -> Sandbox:
    """按默认分层策略构建沙箱（§9B.1）。"""
    ws = Path(workspace_root).expanduser().resolve(strict=False)
    policy = default_policy(ws)
    return Sandbox(
        policy=policy,
        fs=FsGuard(policy),
        browser=BrowserSandbox(policy),
        install=InstallDiagnostics(workspace_root=ws),
    )


__all__ = [
    "BrowserSandbox",
    "BrowserSandboxConfig",
    "Decision",
    "FsGuard",
    "InstallCheck",
    "InstallDiagnostics",
    "LayerPolicy",
    "Sandbox",
    "SandboxLayer",
    "SandboxPolicy",
    "build_sandbox",
    "default_policy",
    "system_readonly_roots",
]
