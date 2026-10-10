"""文件/Shell 沙箱守卫（设计方案 §9B.1 FILE_SHELL 层 + §9B.4 硬约束）。

规则（宽进严管）：
- 工作区内：**可读可写可装**
- 系统关键路径：**可读不可写**
- 工作区与系统路径之外：**拒绝**，且必须指明卡在哪（§9B.4-3）

不做的事（明确）：
- 不拦你的搜索
- 不阻断包安装（pip/npm 缓存与安装目标视为「宽进」的一部分）
- 不做静默失败：任何拒绝都返回 `blocked_at`
"""

from __future__ import annotations

import os
from pathlib import Path

from .policy import Decision, SandboxLayer, SandboxPolicy


class FsGuard:
    """基于分层策略的文件访问守卫。"""

    def __init__(self, policy: SandboxPolicy) -> None:
        self.policy = policy
        self._wl = policy.layer(SandboxLayer.FILE_SHELL)

    # ------------------------------------------------------------------
    # 路径判定
    # ------------------------------------------------------------------
    def check(self, path: str | os.PathLike[str], write: bool = False) -> Decision:
        """判定一次文件访问。

        write=False → 读；write=True → 写/删除/改权限。
        """
        try:
            target = Path(path).expanduser()
            resolved = target.resolve(strict=False)
        except (OSError, RuntimeError) as exc:  # 路径本身不可解析
            return Decision(
                allow=False,
                reason=f"路径无法解析：{exc}",
                blocked_at=f"路径解析：{path}",
            )

        for root in self._wl.writable_roots:
            if _is_within(resolved, root):
                return Decision(allow=True, reason=f"工作区内（可写）：{root}")

        for root in self._wl.readonly_roots:
            if _is_within(resolved, root):
                if write:
                    return Decision(
                        allow=False,
                        reason="系统关键路径只读（防止破坏系统）",
                        blocked_at=f"只读区写入被拒：{resolved}",
                    )
                return Decision(allow=True, reason=f"系统路径只读：{root}")

        # 沙箱外：一律拒绝，并指明卡点
        return Decision(
            allow=False,
            reason="路径在沙箱之外（既不在工作区，也不在只读系统区）",
            blocked_at=(
                f"沙箱边界：{resolved} 不在工作区 {self._wl.writable_roots} "
                f"也不在只读区 {[str(r) for r in self._wl.readonly_roots]}"
            ),
        )

    def check_write(self, path: str | os.PathLike[str]) -> Decision:
        return self.check(path, write=True)

    def check_read(self, path: str | os.PathLike[str]) -> Decision:
        return self.check(path, write=False)

    # ------------------------------------------------------------------
    # Shell 命令判定
    # ------------------------------------------------------------------
    def check_shell(self, command: str) -> Decision:
        """Shell 命令判定（保守：只拦「明显越界写系统路径」的模式）。

        刻意**不做**命令白名单——那会把「装不上东西」的锅甩给沙箱（§9B.4-2）。
        只拦明确指向只读区的写操作。
        """
        lowered = command.lower()
        write_markers = (">", "del ", "rm ", "rmdir ", "remove-item", "mv ", "move ",
                         "chmod", "chown", "takeown", "icacls")
        if not any(m in lowered for m in write_markers):
            return Decision(allow=True, reason="未检测到越界写操作")

        for root in self._wl.readonly_roots:
            root_str = str(root).lower()
            if root_str in lowered:
                return Decision(
                    allow=False,
                    reason="命令试图写入系统只读区",
                    blocked_at=f"命令写入只读区被拒：{root}",
                )
        return Decision(allow=True, reason="未指向系统只读区")

    # ------------------------------------------------------------------
    # 安装路径（宽进：显式放行）
    # ------------------------------------------------------------------
    def install_targets(self) -> list[Path]:
        """包安装相关路径（宽进严管的「宽」侧，供诊断与放行使用）。"""
        import site
        import sys

        targets = [self.policy.workspace_root]
        try:
            targets.append(Path(site.getusersitepackages()))
        except Exception:  # noqa: BLE001
            pass
        try:
            for p in site.getsitepackages():
                targets.append(Path(p))
        except Exception:  # noqa: BLE001
            pass
        targets.append(Path(sys.prefix))
        return targets

    def check_install_path(self, path: str | os.PathLike[str]) -> Decision:
        """安装目标放行（§9B.4-2：依赖可装，不被沙箱卡）。"""
        resolved = Path(path).expanduser().resolve(strict=False)
        for t in self.install_targets():
            try:
                if _is_within(resolved, t):
                    return Decision(
                        allow=True,
                        reason=f"包安装路径放行（§9B.4-2 宽进严管）：{t}",
                    )
            except OSError:
                continue
        return Decision(
            allow=False,
            reason="非包安装路径，走常规写入判定",
            blocked_at=f"非安装目标：{resolved}",
        )


def _is_within(path: Path, root: Path) -> bool:
    """path 是否在 root 之下（含自身）。大小写不敏感（Windows）。"""
    try:
        p = Path(path)
        r = Path(root).resolve(strict=False)
        try:
            p_res = p.resolve(strict=False)
        except OSError:
            return False
        if os.name == "nt":
            return str(p_res).lower().startswith(str(r).lower())
        return p_res == r or r in p_res.parents
    except (OSError, ValueError):
        return False
