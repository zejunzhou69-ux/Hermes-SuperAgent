"""LangGraph 长任务可选补充（设计方案 §4.4 状态机补充）。

当任务预计超过 30 分钟或需要人工介入时，使用 LangGraph 承载子流程：
- checkpoint 断点续跑
- 人工批准节点
- 确定性分支

本模块仅为可选入口，默认不启用；由 Supervisor 在判断任务超长时调用。
"""

from __future__ import annotations

from typing import Any


# ----------------------------------------------------------------------
# 可选 LangGraph 导入（graceful fallback）
# ----------------------------------------------------------------------
try:
    import langgraph  # type: ignore  # noqa: F401 - 仅作存在性检查
    _LANGGRAPH_AVAILABLE = True
except ImportError:  # pragma: no cover
    _LANGGRAPH_AVAILABLE = False


def is_langgraph_available() -> bool:
    """检查 LangGraph 是否已安装。"""
    return _LANGGRAPH_AVAILABLE


def create_long_task_graph(
    task_id: str,
    roles: list[str],
    checkpoints_dir: str | None = None,
) -> dict[str, Any]:
    """创建一个 LangGraph 状态机图（骨架 stub）。

    参数：
        task_id: 任务 ID
        roles: 参与的角色列表
        checkpoints_dir: checkpoint 存储目录（可选）

    返回：
        图配置字典（实际图构建需在 LangGraph 可用时实现）
    """
    if not _LANGGRAPH_AVAILABLE:
        return {
            "task_id": task_id,
            "available": False,
            "note": "langgraph not installed; falling back to sequential execution",
            "roles": roles,
        }

    # TODO (t2 follow-up): 接入 langgraph.graph.StateGraph
    # 当前仅做存在性检查 + 文档占位
    return {
        "task_id": task_id,
        "available": True,
        "roles": roles,
        "checkpoints_dir": checkpoints_dir,
        "note": "LangGraph graph construction placeholder",
    }
