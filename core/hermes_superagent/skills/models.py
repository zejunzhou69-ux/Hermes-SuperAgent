"""技能层数据模型（设计方案 §4.5 自魔改能力）。

一个技能 = 一个目录 `skills/<name>/SKILL.md`：
- YAML front matter 声明元数据（name/description/triggers/tools/version）
- 正文是给 agent 看的可执行步骤（Markdown）

可选同目录放脚本（run.py / run.sh），由技能自行约定，本层只负责发现与描述，
不执行任意代码——执行一律走 Capability（单一出口原则）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

#: 技能描述文件的标准文件名
SKILL_FILENAME = "SKILL.md"

#: front matter 必填字段
REQUIRED_FIELDS = ("name", "description")


class SkillError(ValueError):
    """技能定义非法（缺必填字段 / front matter 损坏）。"""


@dataclass
class Skill:
    """一个已加载的技能。"""

    name: str
    description: str
    path: Path
    triggers: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    version: str = "1"
    body: str = ""
    #: 源文件 mtime，用于热加载判定
    mtime: float = 0.0
    #: 校验告警（不阻断加载，但必须可见——不静默）
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "path": str(self.path),
            "triggers": list(self.triggers),
            "tools": list(self.tools),
            "version": self.version,
            "mtime": self.mtime,
            "warnings": list(self.warnings),
            "body_chars": len(self.body),
        }

    def summary(self) -> str:
        t = f" triggers={','.join(self.triggers)}" if self.triggers else ""
        return f"{self.name} (v{self.version}) — {self.description}{t}"
