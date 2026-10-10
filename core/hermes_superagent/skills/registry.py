"""技能注册表（设计方案 §4.5：热加载 + 自进化沉淀）。

能力：
- `list()` / `get(name)` / `match(query)`：发现与检索
- `reload()`：热加载，只重读 mtime 变化的技能（丢文件进 skills/ 即生效）
- `scaffold(name, description)`：生成新技能骨架——「一键固化为技能」的落点
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .loader import SKILL_FILENAME, default_skills_root, discover
from .models import Skill


@dataclass
class SkillRegistry:
    """技能注册表（进程内，可热加载）。"""

    root: Path
    skills: dict[str, Skill] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)
    #: 最近一次 reload 的统计
    last_reload: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    @classmethod
    def create(cls, root: Path | None = None) -> "SkillRegistry":
        reg = cls(root=root or default_skills_root())
        reg.reload(force=True)
        return reg

    # ------------------------------------------------------------------
    # 热加载
    # ------------------------------------------------------------------
    def reload(self, force: bool = False) -> dict:
        """重新扫描技能目录。

        非 force 时：只在有新目录或已有技能 mtime 变化时替换，避免无谓重读。
        """
        found, errors = discover(self.root)
        found_by_name = {s.name: s for s in found}

        added: list[str] = []
        updated: list[str] = []
        removed: list[str] = []

        for name, skill in found_by_name.items():
            old = self.skills.get(name)
            if old is None:
                added.append(name)
            elif force or old.mtime != skill.mtime:
                updated.append(name)

        for name in list(self.skills):
            if name not in found_by_name:
                removed.append(name)

        changed = bool(added or updated or removed) or force
        if changed:
            self.skills = found_by_name
            self.errors = errors

        self.last_reload = {
            "root": str(self.root),
            "total": len(self.skills),
            "added": added,
            "updated": updated,
            "removed": removed,
            "errors": errors,
            "changed": changed,
        }
        return self.last_reload

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    def list(self) -> list[Skill]:
        return sorted(self.skills.values(), key=lambda s: s.name)

    def get(self, name: str) -> Skill | None:
        return self.skills.get(name)

    def match(self, query: str, limit: int = 5) -> list[Skill]:
        """按触发词 / 名称 / 描述做朴素匹配（打分排序，够用且可解释）。

        打分：触发词完全命中 +3，触发词包含 +2，名称命中 +2，描述命中 +1。
        """
        q = (query or "").strip().lower()
        if not q:
            return []

        scored: list[tuple[int, Skill]] = []
        for skill in self.skills.values():
            score = 0
            for trig in skill.triggers:
                tl = trig.lower()
                if not tl:
                    continue
                if tl == q:
                    score += 3
                elif tl in q or q in tl:
                    score += 2
            if skill.name.lower() in q or q in skill.name.lower():
                score += 2
            if q in skill.description.lower():
                score += 1
            if score > 0:
                scored.append((score, skill))

        scored.sort(key=lambda x: (-x[0], x[1].name))
        return [s for _, s in scored[:limit]]

    # ------------------------------------------------------------------
    # 自进化：生成技能骨架
    # ------------------------------------------------------------------
    def scaffold(self, name: str, description: str, triggers: tuple[str, ...] = (),
                 tools: tuple[str, ...] = (), body: str = "") -> dict:
        """在 skills/<name>/SKILL.md 生成新技能骨架（已存在则拒绝，不覆盖）。"""
        safe = re.sub(r"-{2,}", "-", "".join(
            c if (c.isalnum() or c in "-_") else "-" for c in name
        )).strip("-")
        if not safe:
            return {"ok": False, "error": "技能名非法（净化后为空）", "blocked_at": f"name={name!r}"}

        skill_dir = self.root / safe
        # 防御性检查：净化后仍必须落在技能根目录之内（防路径逃逸）
        try:
            root_res = self.root.resolve(strict=False)
            dir_res = skill_dir.resolve(strict=False)
            if root_res != dir_res and root_res not in dir_res.parents:
                return {
                    "ok": False,
                    "error": "技能目录越出技能根目录",
                    "blocked_at": f"{dir_res} 不在 {root_res} 之内",
                }
        except OSError as exc:
            return {"ok": False, "error": f"路径解析失败：{exc}", "blocked_at": str(skill_dir)}

        target = skill_dir / SKILL_FILENAME
        if target.exists():
            return {
                "ok": False,
                "error": "技能已存在，拒绝覆盖",
                "blocked_at": f"{target} 已存在；请改名或先删除",
            }

        trig = ", ".join(triggers)
        tool_list = ", ".join(tools)
        content = (
            "---\n"
            f"name: {safe}\n"
            f"description: {description}\n"
            f"triggers: [{trig}]\n"
            f"tools: [{tool_list}]\n"
            "version: 1\n"
            "---\n\n"
            + (body or "# 步骤\n\n1. 在这里写这个技能的可执行步骤。\n")
        )

        try:
            skill_dir.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": f"写入失败：{exc}", "blocked_at": str(target)}

        self.reload(force=True)
        return {"ok": True, "path": str(target), "name": safe,
                "note": "技能已生成并热加载；丢进 skills/ 的文件即生效"}

    # ------------------------------------------------------------------
    def report(self) -> dict:
        return {
            "root": str(self.root),
            "count": len(self.skills),
            "skills": [s.to_dict() for s in self.list()],
            "errors": self.errors,
            "last_reload": self.last_reload,
        }
