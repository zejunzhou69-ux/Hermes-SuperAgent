"""技能加载器（设计方案 §4.5：丢一个 skills/<name>/SKILL.md 即可扩展）。

职责：
- 发现 `skills/*/SKILL.md`
- 解析 YAML front matter + Markdown 正文
- 校验必填字段；非法**不静默**——记 warnings 并保留可见性
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from .models import REQUIRED_FIELDS, SKILL_FILENAME, Skill, SkillError

#: 技能根目录候选（按优先级）
_ENV_DIR = "HERMES_SUPERAGENT_SKILLS_DIR"


def default_skills_root(start: Path | None = None) -> Path:
    """解析技能根目录。

    优先级：环境变量 > 从 start 向上找 `skills/` > 当前目录下 `skills/`。
    """
    env = os.environ.get(_ENV_DIR)
    if env:
        return Path(env).expanduser().resolve(strict=False)

    base = (start or Path.cwd()).resolve(strict=False)
    for parent in [base, *base.parents]:
        cand = parent / "skills"
        if cand.is_dir():
            return cand
    return base / "skills"


def split_front_matter(text: str) -> tuple[dict, str]:
    """拆分 YAML front matter 与正文。

    约定：文件以 `---` 开头，到下一条 `---` 之间为 YAML。
    没有 front matter 时返回 ({}, 原文)。
    """
    stripped = text.lstrip("\ufeff")
    if not stripped.startswith("---"):
        return {}, text
    end = stripped.find("\n---", 3)
    if end == -1:
        return {}, text
    raw = stripped[3:end]
    body = stripped[end + 4:]
    try:
        meta = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"front matter YAML 解析失败: {exc}") from exc
    if not isinstance(meta, dict):
        raise SkillError("front matter 必须是键值映射")
    return meta, body.lstrip("\n")


def _as_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(x.strip() for x in value.split(",") if x.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(x).strip() for x in value if str(x).strip())
    return (str(value),)


def load_skill(skill_dir: Path) -> Skill:
    """从单个技能目录加载。目录名作为 name 兜底。"""
    path = skill_dir / SKILL_FILENAME
    text = path.read_text(encoding="utf-8")
    meta, body = split_front_matter(text)

    warnings: list[str] = []
    name = str(meta.get("name") or skill_dir.name)
    description = str(meta.get("description") or "")

    for field_name in REQUIRED_FIELDS:
        if not meta.get(field_name):
            warnings.append(f"front matter 缺少 `{field_name}`，已回退（name=目录名 / description 为空）")

    for key in ("triggers", "tools"):
        if key in meta and not isinstance(meta[key], (str, list, tuple)):
            warnings.append(f"`{key}` 类型可疑（{type(meta[key]).__name__}），已强制转字符串列表")

    return Skill(
        name=name,
        description=description,
        path=path,
        triggers=_as_tuple(meta.get("triggers")),
        tools=_as_tuple(meta.get("tools")),
        version=str(meta.get("version", "1")),
        body=body,
        mtime=path.stat().st_mtime,
        warnings=warnings,
    )


def discover(root: Path) -> tuple[list[Skill], list[dict]]:
    """扫描根目录下所有技能。

    返回 (技能列表, 错误列表)。**不抛异常**——单个技能坏掉不该拖垮整层，
    但错误必须被上抛给调用方记录（不静默）。
    """
    skills: list[Skill] = []
    errors: list[dict] = []

    if not root.is_dir():
        return skills, errors

    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if not (entry / SKILL_FILENAME).is_file():
            continue
        try:
            skills.append(load_skill(entry))
        except (SkillError, OSError, UnicodeDecodeError) as exc:
            errors.append({"dir": str(entry), "error": f"{type(exc).__name__}: {exc}"})

    return skills, errors
