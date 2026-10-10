"""依赖安装诊断（设计方案 §9B.4-2/-3）。

两条硬约束：
- **装东西不被卡**：pip/npm 与双栈镜像源默认开放（宽进严管）。
- **装不上必须显式告警并指明卡在哪**，禁止静默卡死。

本模块只做「诊断 + 显式告警」，不改包管理器行为——把锅甩给沙箱是最糟的设计。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field

from .policy import Decision


#: 常见安装失败特征 → (卡点, 提示)。顺序敏感：越具体的排越前。
_FAILURE_SIGNATURES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (
        ("no space left", "disk full", "enospc"),
        "磁盘空间不足",
        "清理磁盘或换安装目录（HERMES_SUPERAGENT_INSTALL_DIR）",
    ),
    (
        ("permission denied", "access is denied", "eacces", "errno 13", "eperm"),
        "文件权限被拒",
        "安装目标不可写；检查是否需提权，或改用用户级安装（pip install --user / npm --prefix）",
    ),
    (
        ("temporary failure in name resolution", "name or service not known",
         "getaddrinfo", "nodename nor servname"),
        "DNS 解析失败",
        "换双栈镜像源（HERMES_SUPERAGENT_MIRRORS_PIP_INDEX_URL / NPM_REGISTRY）或检查 DNS",
    ),
    (
        ("certificate", "certifi", "self signed", "unable to get local issuer",
         "ssl error", "sslerror", "tlsv1", "certificate verify failed"),
        "TLS 证书校验失败",
        "企业代理自签证书场景：配置 CA（REQUESTS_CA_BUNDLE / NODE_EXTRA_CA_CERTS），勿直接关校验",
    ),
    (
        ("connection refused", "connection reset", "timed out", "timeout",
         "econnrefused", "etimedout", "read timed out"),
        "网络出口不可达",
        "检查代理（HERMES_SUPERAGENT_NET_PROXY）与镜像源；国内/国际双栈路由见 design §8",
    ),
    (
        ("could not find a version", "no matching distribution",
         "not found", "404", "package not found"),
        "包源里找不到该包",
        "源里无此包或包名拼错；换镜像源重试，或确认包名与目标版本",
    ),
    (
        ("requires-python", "unsupported engine", "node version", "python version"),
        "版本不满足",
        "当前运行时版本与包要求不符；升级运行时或选兼容版本",
    ),
)


@dataclass
class InstallCheck:
    """一种包管理器的环境体检结果。"""

    tool: str  # "pip" | "npm"
    available: bool
    index_url: str | None = None
    blocked_at: str | None = None
    hint: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "available": self.available,
            "index_url": self.index_url,
            "blocked_at": self.blocked_at,
            "hint": self.hint,
            "notes": list(self.notes),
        }


class InstallDiagnostics:
    """安装环境体检 + 失败归因。"""

    def __init__(self, workspace_root=None) -> None:
        self.workspace_root = workspace_root

    # ------------------------------------------------------------------
    # 体检
    # ------------------------------------------------------------------
    def check(self) -> list[InstallCheck]:
        return [self._check_pip(), self._check_npm()]

    def _check_pip(self) -> InstallCheck:
        exe = shutil.which("pip") or shutil.which("pip3")
        index = os.environ.get("HERMES_SUPERAGENT_MIRRORS_PIP_INDEX_URL") or os.environ.get(
            "PIP_INDEX_URL"
        )
        notes = ["安装默认开放（§9B.4-2 宽进严管）"]
        if not exe:
            return InstallCheck(
                tool="pip",
                available=False,
                index_url=index,
                blocked_at="pip 不在 PATH 中",
                hint="确认 Python 环境已激活；或用 python -m pip",
                notes=notes,
            )
        if index:
            notes.append(f"镜像源已配置：{index}")
        else:
            notes.append("未配置镜像源，将走默认源（国内建议配 HERMES_SUPERAGENT_MIRRORS_PIP_INDEX_URL）")
        return InstallCheck(tool="pip", available=True, index_url=index, notes=notes)

    def _check_npm(self) -> InstallCheck:
        exe = shutil.which("npm")
        registry = os.environ.get("HERMES_SUPERAGENT_MIRRORS_NPM_REGISTRY") or os.environ.get(
            "NPM_CONFIG_REGISTRY"
        )
        notes = ["安装默认开放（§9B.4-2 宽进严管）"]
        if not exe:
            return InstallCheck(
                tool="npm",
                available=False,
                index_url=registry,
                blocked_at="npm 不在 PATH 中",
                hint="仅宿主插件（harness-plugin）需要 Node 环境；Python 内核不受影响",
                notes=notes,
            )
        if registry:
            notes.append(f"镜像源已配置：{registry}")
        else:
            notes.append("未配置镜像源，将走默认源")
        return InstallCheck(tool="npm", available=True, index_url=registry, notes=notes)

    # ------------------------------------------------------------------
    # 失败归因（核心：绝不静默）
    # ------------------------------------------------------------------
    def diagnose_failure(self, tool: str, output: str = "", exit_code: int | None = None) -> Decision:
        """把一次安装失败翻译成「卡在哪 + 怎么办」。

        永远返回 allow=False 且**必定带 blocked_at**——禁止静默卡死。
        """
        text = (output or "").lower()

        for needles, blocked_at, hint in _FAILURE_SIGNATURES:
            if any(n in text for n in needles):
                return Decision(
                    allow=False,
                    reason=f"{tool} 安装失败，卡点：{blocked_at}",
                    blocked_at=f"{blocked_at} → {hint}",
                )

        if exit_code not in (None, 0):
            return Decision(
                allow=False,
                reason=f"{tool} 安装失败（exit={exit_code}），未匹配已知特征",
                blocked_at=(
                    f"未归因的失败（exit={exit_code}）；请把原始输出交给 agent 继续分析，"
                    f"不要重试同一条命令"
                ),
            )

        return Decision(
            allow=False,
            reason=f"{tool} 安装失败",
            blocked_at="未捕获到输出，无法归因；请保留 stderr 再诊断（禁止静默）",
        )

    # ------------------------------------------------------------------
    # 报告
    # ------------------------------------------------------------------
    def report(self) -> dict:
        checks = self.check()
        blocked = [c for c in checks if c.blocked_at]
        return {
            "checks": [c.to_dict() for c in checks],
            "blocked": [c.to_dict() for c in blocked],
            "all_available": not blocked,
            "policy": "§9B.4-2：包安装与镜像源默认开放；装不上必定显式指明卡点",
        }

    def summary(self) -> str:
        checks = self.check()
        lines = ["依赖安装环境体检（§9B.4）", ""]
        for c in checks:
            state = "可用" if c.available else "不可用"
            lines.append(f"  {c.tool:<4} {state}")
            if c.index_url:
                lines.append(f"       index: {c.index_url}")
            if c.blocked_at:
                lines.append(f"       卡点 : {c.blocked_at}")
                lines.append(f"       建议 : {c.hint}")
            for n in c.notes:
                lines.append(f"       - {n}")
        if all(c.available for c in checks):
            lines.append("")
            lines.append("  结论：安装通道正常，且默认开放（宽进严管）")
        return "\n".join(lines)
