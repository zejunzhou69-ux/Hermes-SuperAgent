"""技能层（设计方案 §4.5 自魔改能力 / 需求 R2）。

扩展方式（三层扩展点里的最轻一层，热加载、不重启）：

    skills/
      web-digest/
        SKILL.md        # YAML front matter + Markdown 步骤

丢进目录即生效（`reload()` 按 mtime 增量重读）。
自进化闭环的落点：`registry.scaffold(...)` 把一次有效解法固化成新技能。
"""

from __future__ import annotations

from .loader import SKILL_FILENAME, default_skills_root, discover, load_skill, split_front_matter
from .models import REQUIRED_FIELDS, Skill, SkillError
from .registry import SkillRegistry

__all__ = [
    "REQUIRED_FIELDS",
    "SKILL_FILENAME",
    "Skill",
    "SkillError",
    "SkillRegistry",
    "default_skills_root",
    "discover",
    "load_skill",
    "split_front_matter",
]
