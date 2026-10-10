"""CrewAI 五角色 Crew 组装与执行（设计方案 §4.4 / §7.3 C1-C4）。"""
from __future__ import annotations
import asyncio
import json
import time
import uuid
from dataclasses import asdict
from typing import Any, Callable
from .models import ProcessMode, RoleDef, TaskDef, TaskRoute
from .middleware import CostMiddleware
from .router import load_roles_yaml, route_task
try:
    from crewai import Agent, Crew, Process, Task as CrewTask
    _CREWAI_AVAILABLE = True
except ImportError:
    _CREWAI_AVAILABLE = False
    Agent = None
    Crew = None
    Process = None
    CrewTask = None
EventCallback = Callable[[str, dict[str, Any]], None]

class HermesCrew:
    def __init__(self, task, roles=None, middleware=None, on_event=None, agent_params=None, model_router=None):
        self.task = task
        self._roles = roles or load_roles_yaml()
        self._middleware = middleware or CostMiddleware(on_event=on_event)
        self._on_event = on_event
        self._agent_params = agent_params or {}
        self._model_router = model_router  # t4: 可选注入 ModelRouter，429 降级走双栈路由
        self._route = None
        self._result = None

    @property
    def route(self):
        return self._route

    @property
    def result(self):
        return self._result

    async def run(self):
        self._route = route_task(self.task, self._roles)
        self._emit("crew.route", asdict(self._route))
        if _CREWAI_AVAILABLE:
            return await self._run_with_crewai()
        return await self._run_fallback()

    async def _run_with_crewai(self):
        target = self._route.target_role
        role_def = self._roles.get(target)
        if role_def is None:
            return {"error": f"role not found: {target}", "task_id": self.task.id}
        # 429 降级（K1 设计意图）：无可用 LLM 后端（如无 API key）时 CrewAI 会失败，
        # 此时走无 LLM 的 stub 路径，保证离线 / CI 环境任务不中断、且可观测。
        if self._make_llm(role_def.llm) is None:
            return await self._run_fallback()
        agent = Agent(role=target, goal=role_def.goal,
            backstory=role_def.prompt(task=self.task.description, context=self.task.context,
                history=json.dumps(self.task.history, ensure_ascii=False)),
            tools=self._resolve_tools(role_def.tools), llm=self._make_llm(role_def.llm),
            max_iter=role_def.max_iter, **self._agent_params)
        crew_task = CrewTask(description=self.task.description,
            expected_output="JSON result", agent=agent)
        proc = self._route.process_mode if self._route else ProcessMode.SEQUENTIAL
        crew_process = Process.sequential if proc == ProcessMode.SEQUENTIAL else Process.hierarchical
        crew = Crew(agents=[agent], tasks=[crew_task], process=crew_process)
        t0 = time.time()
        try:
            result = await asyncio.to_thread(crew.kickoff)
            elapsed_ms = int((time.time() - t0) * 1000)
        except Exception as exc:
            elapsed_ms = int((time.time() - t0) * 1000)
            error_str = f"{type(exc).__name__}: {exc}"
            final = await self._middleware.call_with_middleware(role=target,
                llm_fn=lambda **kw: (_ for _ in ()).throw(exc),
                params={"task": self.task.description}, role_def=role_def,
                route=self._route, max_retries=self.task.max_retries)
            return {"task_id": self.task.id, "ok": False, "error": error_str,
                "elapsed_ms": elapsed_ms, "fallback_result": final,
                "cost": self._middleware.state.total_cost_usd}
        self._result = {"task_id": self.task.id, "ok": True,
            "output": str(result) if result else "", "role": target,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "cost_usd": self._middleware.state.total_cost_usd,
            "trace_id": uuid.uuid4().hex[:12]}
        self._emit("crew.complete", self._result)
        return self._result

    async def _run_fallback(self):
        target = self._route.target_role
        role_def = self._roles.get(target)
        if role_def is None:
            return {"error": f"role not found: {target}", "task_id": self.task.id}
        t0 = time.time()
        prompt = role_def.prompt(task=self.task.description, context=self.task.context,
            history=json.dumps(self.task.history, ensure_ascii=False))
        simulated = {"role": target, "prompt_preview": prompt[:200], "simulated": True,
            "note": "crewai not installed; stub for validation", "trace_id": uuid.uuid4().hex[:12]}
        await self._middleware.call_with_middleware(role=target,
            llm_fn=lambda **kw: simulated, params={"task": self.task.description},
            role_def=role_def, route=self._route)
        self._result = {"task_id": self.task.id, "ok": True,
            "output": json.dumps(simulated, ensure_ascii=False), "role": target,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "cost_usd": self._middleware.state.total_cost_usd, "cost": self._middleware.state.total_cost_usd,
            "mode": "fallback",
            "trace_id": simulated["trace_id"]}
        self._emit("crew.complete", self._result)
        return self._result

    def _resolve_tools(self, tool_names):
        if not _CREWAI_AVAILABLE:
            return []
        return []

    def _make_llm(self, model_key):
        if not _CREWAI_AVAILABLE:
            return None
        # 429 降级（K1）：若注入了 ModelRouter，把 task_class→route 决策作为 LLM 配置提示，
        # 让 crewai LLM 在限流时可切备用路由；否则回退静态 model_key。
        if self._model_router is not None:
            try:
                info = self._model_router.route_info(self.task.task_class if hasattr(self.task, "task_class") else "reasoning")
                self._emit("crew.model_route", info)
            except Exception:
                pass
        try:
            from crewai import LLM
            import os
            # 凭证解析：OpenAI 系（crewai 默认 OpenAI 协议）+ agnes-flash（OpenAI 兼容端点）
            openai_key = os.environ.get("OPENAI_API_KEY") or self._resolve_secret("openai")
            agnes_key = os.environ.get("AGNES_API_KEY") or self._resolve_secret("agnes")
            agnes_base = os.environ.get("AGNES_BASE_URL") or self._resolve_secret("agnes_base_url")
            # agnes 模型走 agnes 端点（OpenAI 兼容）；其余走 OpenAI 默认
            if model_key and model_key.startswith("agnes"):
                if not (agnes_key and agnes_base):
                    return None
                return LLM(model=model_key, api_key=agnes_key, base_url=agnes_base)
            if not openai_key:
                return None  # 无可用凭证：交给 fallback（可观测、不静默）
            return LLM(model=model_key, api_key=openai_key)
        except Exception:
            return None

    def _resolve_secret(self, name: str):
        """从 SecretsManager（env > secrets.json > 宿主）取一个密钥/端点，失败返回 None。"""
        try:
            from hermes_superagent.capabilities.secrets import SecretsManager
            return SecretsManager().resolve(name)
        except Exception:
            return None

    def _emit(self, event, payload):
        if self._on_event:
            try:
                self._on_event(event, payload)
            except Exception:
                pass
        if self._middleware._on_event:
            try:
                self._middleware._on_event(event, payload)
            except Exception:
                pass

def create_crew(task, roles=None, on_event=None, **kwargs):
    return HermesCrew(task=task, roles=roles, on_event=on_event, **kwargs)