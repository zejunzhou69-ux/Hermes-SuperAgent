"""浏览器沙箱（设计方案 §9B.1 BROWSER 层 + §9B.1 EXTERNAL_CDP 层）。

要点：
- 内建 Chromium 跑在隔离 profile（默认 `<workspace>/.sandbox/chrome-profile`），
  **默认不碰用户日常浏览器**（§9B.2）。
- 外接 CDP 接管真实浏览器：**默认禁**，必须显式 opt-in，且开启后动作留痕（§9B.1）。
- 拇指浏览器为**可交互**内嵌浏览器（§9A.3 / §13.2），住沙箱内，是沙箱可视窗口；
  本模块只负责它的沙箱归属，不限制用户本人的鼠标/键盘操控。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .policy import Decision, SandboxLayer, SandboxPolicy


@dataclass
class BrowserSandboxConfig:
    """一次浏览器启动的沙箱配置。"""

    mode: str  # "builtin" | "cdp"
    profile_dir: Path
    headless: bool = True
    #: 拇指浏览器默认尺寸（360×640 为「拇指」比例，§13.2）
    thumb_width: int = 360
    thumb_height: int = 640
    thumb_fps: float = 4.0
    cdp_endpoint: str | None = None
    #: CDP 模式的授权依据（谁授权、为什么）
    authorized_by: str | None = None
    authorization_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "profile_dir": str(self.profile_dir),
            "headless": self.headless,
            "thumb": {
                "width": self.thumb_width,
                "height": self.thumb_height,
                "fps": self.thumb_fps,
            },
            "cdp_endpoint": self.cdp_endpoint,
            "authorized_by": self.authorized_by,
            "authorization_reason": self.authorization_reason,
        }


class BrowserSandbox:
    """浏览器沙箱裁决 + 启动参数生成 + CDP 留痕。"""

    def __init__(self, policy: SandboxPolicy) -> None:
        self.policy = policy
        self._evidence: list[dict] = []
        #: 会话级 CDP 授权（默认未授权）
        self._cdp_authorized = False

    # ------------------------------------------------------------------
    # 内建模式（默认）
    # ------------------------------------------------------------------
    def profile_dir(self) -> Path:
        """隔离 profile 目录（独立于用户日常浏览器）。"""
        layer = self.policy.layer(SandboxLayer.BROWSER)
        if layer.writable_roots:
            return layer.writable_roots[0]
        return self.policy.workspace_root / ".sandbox" / "chrome-profile"

    def builtin_config(self, headless: bool = True) -> BrowserSandboxConfig:
        return BrowserSandboxConfig(
            mode="builtin",
            profile_dir=self.profile_dir(),
            headless=headless,
        )

    # ------------------------------------------------------------------
    # 外接 CDP（默认禁，显式 opt-in）
    # ------------------------------------------------------------------
    @property
    def cdp_authorized(self) -> bool:
        return self._cdp_authorized

    def enable_cdp(self, endpoint: str, *, supervisor: str, reason: str) -> Decision:
        """显式开启 CDP 接管（需「有监督」：谁授权 + 为什么）。

        缺任一要素即拒绝，并指明卡点——不允许「悄悄接管用户真实浏览器」。
        """
        if not supervisor or not supervisor.strip():
            return Decision(
                allow=False,
                reason="CDP 接管需「有监督」：必须指明授权人",
                blocked_at="缺 supervisor（谁授权）",
            )
        if not reason or not reason.strip():
            return Decision(
                allow=False,
                reason="CDP 接管需「有监督」：必须说明授权原因",
                blocked_at="缺 reason（为什么接管真实浏览器）",
            )
        if not endpoint or not endpoint.strip():
            return Decision(
                allow=False,
                reason="CDP 接管缺少 endpoint",
                blocked_at="缺 endpoint（如 http://127.0.0.1:9222）",
            )

        self._cdp_authorized = True
        record = {
            "event": "cdp.enabled",
            "endpoint": endpoint,
            "supervisor": supervisor,
            "reason": reason,
            "ts": int(time.time() * 1000),
            "note": "§9B.1 外接 CDP 为默认禁 + 显式 opt-in；开启后动作留痕",
        }
        self._evidence.append(record)
        return Decision(allow=True, reason="CDP 已按显式 opt-in 开启（有监督 + 留痕）")

    def disable_cdp(self, *, operator: str = "system") -> None:
        self._cdp_authorized = False
        self._evidence.append(
            {
                "event": "cdp.disabled",
                "operator": operator,
                "ts": int(time.time() * 1000),
            }
        )

    def check_cdp(self, endpoint: str | None = None) -> Decision:
        """CDP 使用前裁决：未 opt-in 一律拒绝。"""
        if not self._cdp_authorized:
            return Decision(
                allow=False,
                reason="外接 CDP 默认禁用（§9B.1）；需显式 opt-in 后才能接管真实浏览器",
                blocked_at=(
                    "CDP 未授权：调用 enable_cdp(endpoint, supervisor=..., reason=...) 显式开启"
                ),
            )
        return Decision(allow=True, reason="CDP 已授权（有监督模式）")

    def cdp_config(self) -> BrowserSandboxConfig:
        auth = self._evidence[-1] if self._evidence else {}
        return BrowserSandboxConfig(
            mode="cdp",
            profile_dir=self.profile_dir(),
            headless=False,
            cdp_endpoint=auth.get("endpoint"),
            authorized_by=auth.get("supervisor"),
            authorization_reason=auth.get("reason"),
        )

    # ------------------------------------------------------------------
    # 启动参数
    # ------------------------------------------------------------------
    def launch_args(self, cfg: BrowserSandboxConfig) -> list[str]:
        """生成 chromium 启动参数（隔离 profile + 沙箱友好）。

        注意：这里用的是 **隔离 profile**，绝不指向用户日常 profile。
        """
        args = [
            f"--user-data-dir={cfg.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if cfg.headless:
            args.append("--headless=new")
        return args

    # ------------------------------------------------------------------
    # 留痕 / 报告
    # ------------------------------------------------------------------
    def evidence_log(self) -> list[dict]:
        return list(self._evidence)

    def report(self) -> dict:
        layer = self.policy.layer(SandboxLayer.BROWSER)
        cdp_layer = self.policy.layer(SandboxLayer.EXTERNAL_CDP)
        return {
            "browser_layer": layer.to_dict(),
            "external_cdp_layer": cdp_layer.to_dict(),
            "profile_dir": str(self.profile_dir()),
            "thumb_browser": {
                "interactive": True,
                "runs_in_sandbox": True,
                "note": "拇指浏览器为可交互内嵌浏览器，用户可自由用鼠标/键盘操控（§9A.3）",
                "default_size": {"width": 360, "height": 640, "fps": 4.0},
            },
            "cdp_authorized": self._cdp_authorized,
            "evidence": self.evidence_log(),
        }
