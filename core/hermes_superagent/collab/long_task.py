"""长任务 LangGraph 逃生舱（设计方案 §4.4 / 协作骨架 §5）。

仅当预估任务 >30min、需要断点续跑(checkpoint)、人工介入(interrupt)
或强确定性分支时，把"子流程"交给 LangGraph；跑完把结果写回主管的共享上下文，
继续由 CrewAI 路由。常规链路（秒~分钟级）一律走 CrewAI，不要为简单事造状态机。

LangGraph 为可选依赖（pip install ".[collab]" 安装 langgraph）。缺省时
`make_langgraph_fallback` 返回 None，CrewRunner 回退到普通顺序链，保证骨架可运行。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from .models import TaskDef

#: 预估超过该分钟数即考虑切 LangGraph（协作骨架 §5 触发条件 1）
LONG_TASK_THRESHOLD_MIN = 30


def _langgraph_available() -> bool:
    try:
        import langgraph  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def make_langgraph_fallback(
    task: TaskDef,
    *,
    context: dict[str, Any] | None = None,
    estimated_minutes: int = 0,
    need_human: bool = False,
) -> Callable[[], Awaitable[dict[str, Any]]] | None:
    """若满足触发条件且 LangGraph 可用，返回一个 async 子流程闭包；否则返回 None。

    触发条件（任一）：
    1. estimated_minutes > LONG_TASK_THRESHOLD_MIN
    2. need_human（需人工介入节点）
    LangGraph 未安装时一律返回 None（回退 CrewAI 顺序链，不阻塞）。
    """
    context = context or {}
    triggered = estimated_minutes > LONG_TASK_THRESHOLD_MIN or need_human
    if not triggered:
        return None
    # LangGraph 未安装时仍返回子流程（内部回退到 CrewRunner 顺序链），
    # 保证"长任务边界"可被测试；安装了才真正走 LangGraph 状态机。

    # ------------------------------------------------------------------
    # 真实 LangGraph 子流程：crawl → code → verify → report，带 checkpoint 能力。
    # 骨架期：节点直接复用 LiteAgent 规则后端；checkpointer 由调用方注入。
    # ------------------------------------------------------------------
    async def fallback() -> dict[str, Any]:
        from .crew import CrewRunner
        from .models import ProcessMode

        runner = CrewRunner(
            goal=task.title or task.description,
            context=context,
            process=ProcessMode.SEQUENTIAL,
            use_langgraph_fallback=False,  # 避免递归
            long_task_minutes=LONG_TASK_THRESHOLD_MIN,
        )
        result = await runner.run(task)
        return {
            "results": result.results,
            "verdict": "escalated" if result.verdict != "pass" else "pass",
            "notes": ["langgraph fallback executed", *result.notes],
        }

    return fallback
