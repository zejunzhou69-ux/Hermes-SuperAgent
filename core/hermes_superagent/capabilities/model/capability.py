"""`model` Capability (design §6.1/§6.2): complete / stream / embed / route_info.

包装 ModelRouter，使其可以挂进 Capability Protocol，
供 Bridge 与 CrewAI 成本中间件统一调用（C3）。
"""
from __future__ import annotations

import asyncio
from typing import Any

from .router import ModelRouter
from .providers import ModelMessage

__all__ = ["ModelCapability"]


class ModelCapability:
    name = "model"

    def __init__(self, router: ModelRouter | None = None):
        self.router = router or ModelRouter()

    # -- Capability Protocol (design §6.1) ---------------------------
    def schema(self) -> dict[str, Any]:
        return {
            "actions": {
                "complete": {
                    "params": {"messages": "list[{role, content}]", "task_class": "str",
                                "model": "optional str"},
                    "returns": {"content": "str", "model": "str", "usage": "dict"},
                },
                "route_info": {"params": {"task_class": "str"},
                               "returns": {"primary": "dict", "fallbacks": "list", "stats": "dict"}},
            }
        }

    async def invoke(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action == "complete":
            msgs = [ModelMessage(m["role"], m["content"]) for m in params["messages"]]
            # provider.complete 是同步 httpx；丢线程池避免阻塞事件循环
            resp = await asyncio.to_thread(
                self.router.complete,
                msgs,
                task_class=params.get("task_class", "reasoning"),
                **({"model": params["model"]} if "model" in params else {}),
            )
            return {"ok": True, "data": resp.__dict__}
        if action == "route_info":
            return {"ok": True, "data": self.router.route_info(params.get("task_class", "reasoning"))}
        raise ValueError(f"unknown model action: {action}")
