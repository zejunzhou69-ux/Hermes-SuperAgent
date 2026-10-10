"""任务路由（设计方案 §4.4 任务路由 + §9.3 失败处理）。

路由策略：
- 按 task_class 映射到目标角色
- 未知 task_class 默认走 Supervisor 拆解
- 路由决策附带 cost 预估（基于角色定义）

任务类映射表（可扩展）：
  crawl      → crawler
  code       → coder
  verify     → verifier
  report     → reporter
  research   → supervisor（拆解后分派）
  default    → supervisor（先拆解）
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import yaml

from .models import ProcessMode, RoleDef, TaskDef, TaskRoute

#: 路由映射表（task_class → 目标角色名）
_ROUTE_MAP: dict[str, str] = {
    "crawl": "crawler",
    "code": "coder",
    "verify": "verifier",
    "report": "reporter",
    "research": "supervisor",
}

#: YAML 文件路径（相对于本包）
_ROLES_YAML = Path(__file__).parent / "roles.yaml"


def _load_yaml() -> dict[str, Any]:
    """加载 roles.yaml；失败时返回空结构。"""
    try:
        with _ROLES_YAML.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001
        return {}


def load_roles_yaml() -> dict[str, RoleDef]:
    """从 YAML 加载全部角色定义，返回 name → RoleDef 映射。

    对应魔改点 C2：角色/任务定义标准化为 YAML。
    """
    data = _load_yaml()
    roles: dict[str, RoleDef] = {}
    for raw in data.get("roles", []):
        process_str = raw.get("process", "sequential")
        roles[raw["name"]] = RoleDef(
            name=raw["name"],
            prompt_template=raw.get("prompt_template", ""),
            goal=raw.get("goal", ""),
            tools=raw.get("tools", []),
            llm=raw.get("llm", "agnes-2.5-flash"),
            backup_llm=raw.get("backup_llm", "agnes-3.0-flash"),
            max_iter=raw.get("max_iter", 5),
            process=ProcessMode(process_str),
        )
    return roles


def route_task(task: TaskDef, roles: dict[str, RoleDef] | None = None) -> TaskRoute:
    """根据 task_class 路由到目标角色。

    未命中路由表时，默认路由到 supervisor（由 Supervisor 拆解）。
    """
    if roles is None:
        roles = load_roles_yaml()

    target = _ROUTE_MAP.get(task.task_class, "supervisor")
    role_def = roles.get(target)

    if role_def is None:
        # 回退到 supervisor
        role_def = roles.get("supervisor")
        target = "supervisor"

    notes = [f"task_class={task.task_class}", f"target={target}"]
    if task.task_class not in _ROUTE_MAP:
        notes.append("fallback to supervisor (unknown class)")

    return TaskRoute(
        task=task,
        target_role=target,
        llm_key=role_def.llm if role_def else "agnes-2.5-flash",
        backup_llm_key=role_def.backup_llm if role_def else "agnes-3.0-flash",
        process_mode=role_def.process if role_def else ProcessMode.SEQUENTIAL,
        notes=notes,
    )


def list_routes() -> list[dict[str, Any]]:
    """返回所有已知路由（用于调试/GUI 展示）。"""
    roles = load_roles_yaml()
    result = []
    for cls_name, role_name in _ROUTE_MAP.items():
        role_def = roles.get(role_name)
        result.append({
            "task_class": cls_name,
            "target_role": role_name,
            "llm": role_def.llm if role_def else "?",
            "backup_llm": role_def.backup_llm if role_def else "?",
        })
    return result
