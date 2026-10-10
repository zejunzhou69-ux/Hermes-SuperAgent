"""五角色协作链（pipeline）——CrewAI 主干 + LiteAgent 规则兜底。

与 `crew.py`（HermesCrew，单任务路由执行器）互补：
- `crew.py` 负责"一个任务 → 路由到单个角色 → 跑"。
- 本模块负责"五角色拓扑"：supervisor 拆解 + 顺序链 crawler→coder→verifier→reporter，
  是 design.md §4.4 与协作骨架 §3 的落地，M2 出口标准（一个任务走完五角色）。

CrewAI / LangGraph 为可选依赖；缺省时用 `LiteAgent` 规则后端直接跑链。
长任务（>30min / 需人工介入）经 `long_task.make_langgraph_fallback` 切 LangGraph。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from .agents import AgentResult, build_agents, LiteAgent
from .models import ProcessMode, RoleDef, TaskDef


#: 顺序流的角色执行链（四个"手"；supervisor 在层级流下先做一次拆解）
SEQUENTIAL_CHAIN = ["crawler", "coder", "verifier", "reporter"]


@dataclass
class CrewResult:
    """一次五角色链运行的汇总（供 GUI 卡片 / 测试断言）。"""

    goal: str
    task_id: str
    process: str
    results: list[AgentResult] = field(default_factory=list)
    verdict: str = "unknown"  # pass / fail / escalated
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "task_id": self.task_id,
            "process": self.process,
            "results": [r.as_dict() for r in self.results],
            "verdict": self.verdict,
            "notes": self.notes,
        }


def create_crew_pipeline(
    goal: str,
    *,
    context: dict[str, Any] | None = None,
    process: ProcessMode = ProcessMode.SEQUENTIAL,
    use_langgraph_fallback: bool = False,
    long_task_minutes: int = 30,
) -> "CrewRunner":
    """组装一个可执行的五角色协作链（见 create_crew_pipeline docstring 同名参数）。"""
    ctx = dict(context or {})
    ctx.setdefault("process", process.value)
    return CrewRunner(
        goal=goal,
        context=ctx,
        process=process,
        use_langgraph_fallback=use_langgraph_fallback,
        long_task_minutes=long_task_minutes,
    )


class CrewRunner:
    """五角色链执行器。"""

    def __init__(
        self,
        *,
        goal: str,
        context: dict[str, Any],
        process: ProcessMode,
        use_langgraph_fallback: bool,
        long_task_minutes: int,
    ) -> None:
        self.goal = goal
        self.context = context
        self.process = process
        self.use_langgraph_fallback = use_langgraph_fallback
        self.long_task_minutes = long_task_minutes
        self._agents: dict[str, LiteAgent] = {}
        self._task_id = uuid.uuid4().hex[:8]

    def inject(self, role_name: str, agent: LiteAgent) -> None:
        """把某角色替换为自定义实现（默认用 LiteAgent 规则兜底）。"""
        self._agents[role_name] = agent

    def _ensure_agents(self) -> None:
        if not self._agents:
            self._agents = build_agents()

    async def run(self, task: TaskDef | None = None) -> CrewResult:
        self._ensure_agents()
        task = task or TaskDef(title=self.goal, description=self.goal, task_class="default")

        # 长任务逃生舱（协作骨架 §5）
        if self.use_langgraph_fallback:
            from .long_task import make_langgraph_fallback

            fallback = make_langgraph_fallback(
                task, context=self.context, estimated_minutes=self.long_task_minutes + 1
            )
            if fallback is not None:
                result = await fallback()
                return CrewResult(
                    goal=self.goal,
                    task_id=self._task_id,
                    process=f"langgraph-fallback({self.process.value})",
                    results=result.get("results", []),
                    verdict=result.get("verdict", "escalated"),
                    notes=result.get("notes", []),
                )

        results: list[AgentResult] = []
        chain = (
            SEQUENTIAL_CHAIN
            if self.process == ProcessMode.SEQUENTIAL
            else self._hierarchical_chain()
        )

        for role_name in chain:
            agent = self._agents.get(role_name) or _default_agent(role_name)
            # 上游产物写进共享上下文（crawler→coder→verifier→reporter）
            if role_name == "coder":
                prev = _last(results, "crawler")
                if prev:
                    self.context.setdefault("crawler_content", prev.payload.get("content", ""))
            elif role_name == "verifier":
                prev = _last(results, "coder")
                if prev:
                    self.context.setdefault("coder_code", prev.payload.get("code", ""))
            elif role_name == "reporter":
                for r in results:
                    if "verdict" in r.payload:
                        self.context.setdefault(f"{r.role}_verdict", r.payload.get("verdict"))
                self.context.setdefault("crawler_level", _first_level(results))

            res = await agent.run(task, self.context)
            results.append(res)
            if not res.ok:
                # 失败路由（§4）：层级模式 → Supervisor 裁决（记录 escalate，绝不静默吞错）
                if self.process == ProcessMode.HIERARCHICAL:
                    self.context.setdefault("decisions", []).append({
                        "role": role_name,
                        "action": "escalate_to_supervisor",
                        "error": res.error or str(res.payload.get("error", "")),
                    })
                    continue
                return CrewResult(
                    goal=self.goal,
                    task_id=self._task_id,
                    process=self.process.value,
                    results=results,
                    verdict="fail",
                    notes=[f"{role_name} failed: {res.error or res.payload.get('error')}"],
                )

        verdict = "pass" if all(r.ok for r in results) else "fail"
        return CrewResult(
            goal=self.goal,
            task_id=self._task_id,
            process=self.process.value,
            results=results,
            verdict=verdict,
        )

    def _hierarchical_chain(self) -> list[str]:
        """层级流：Supervisor 先拆解，再按标准四段链执行。"""
        return ["supervisor"] + SEQUENTIAL_CHAIN


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------
def _default_agent(role_name: str) -> LiteAgent:
    agents = build_agents()
    return agents.get(role_name) or LiteAgent()


def _last(results: list[AgentResult], role: str) -> AgentResult | None:
    for r in reversed(results):
        if r.role == role:
            return r
    return None


def _first_level(results: list[AgentResult]) -> str:
    r = _last(results, "crawler")
    return str((r.payload.get("level") if r else None) or "?")
