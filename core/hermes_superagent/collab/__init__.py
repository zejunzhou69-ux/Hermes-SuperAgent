"""collab 包：CrewAI 五角色协作框架（设计方案 §4.4 / §7.3）。

CrewAI 为主干（顺序/层级团队路由），LangGraph 仅作长任务逃生舱。
CrewAI / LangGraph 为可选依赖（pip install ".[collab]"），缺省时用
纯 Python `LiteAgent` 规则后端兜底，保证拓扑与路由可被无 LLM 验证。

包含：
- models.py：数据模型（RoleDef, TaskDef, CostRecord, TaskRoute）
- agents.py：五角色实体（Supervisor/Crawler/Coder/Verifier/Reporter + LiteAgent 规则兜底）
- middleware.py：成本追踪与 429 降级中间件
- crew.py：单任务路由执行器（HermesCrew / create_crew）
- pipeline.py：五角色拓扑链（CrewRunner / create_crew_pipeline）—— M2 出口
- router.py：任务路由（按 task_class 分派到合适角色）
- long_task.py：LangGraph 长任务逃生舱（checkpointer / 人工介入）
- langgraph_bridge.py：LangGraph 存在性检查与长任务图 stub
- roles.yaml：YAML 角色定义（C2 魔改点）
"""

from __future__ import annotations

from .models import ProcessMode, CostRecord, RoleDef, TaskDef, TaskRoute
from .middleware import CostMiddleware, MiddlewareState
from .router import load_roles_yaml, route_task, list_routes
from .agents import (
    AgentResult,
    Coder,
    Crawler,
    LiteAgent,
    Reporter,
    Supervisor,
    Verifier,
    build_agents,
)
from .crew import HermesCrew, create_crew, _CREWAI_AVAILABLE
from .pipeline import CrewResult, CrewRunner, create_crew_pipeline, SEQUENTIAL_CHAIN
from .long_task import LONG_TASK_THRESHOLD_MIN, make_langgraph_fallback
from .langgraph_bridge import is_langgraph_available, create_long_task_graph

__all__ = [
    "ProcessMode",
    "RoleDef",
    "TaskDef",
    "CostRecord",
    "TaskRoute",
    "load_roles_yaml",
    "route_task",
    "list_routes",
    "CostMiddleware",
    "MiddlewareState",
    "AgentResult",
    "Coder",
    "Crawler",
    "LiteAgent",
    "Reporter",
    "Supervisor",
    "Verifier",
    "build_agents",
    "HermesCrew",
    "create_crew",
    "_CREWAI_AVAILABLE",
    "CrewResult",
    "CrewRunner",
    "create_crew_pipeline",
    "SEQUENTIAL_CHAIN",
    "LONG_TASK_THRESHOLD_MIN",
    "make_langgraph_fallback",
    "is_langgraph_available",
    "create_long_task_graph",
]
