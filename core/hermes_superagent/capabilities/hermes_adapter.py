"""魔改点 H5 的落地：hermes-agent 内核模型调用改走 ModelRouter。

用法（hermes 侧只需替换模型调用处）：

    from hermes_superagent.capabilities.hermes_adapter import HermesModelAdapter
    from hermes_superagent.capabilities.model import ModelRouter

    router = ModelRouter()
    adapter = HermesModelAdapter(router)

    # 原内核调用：llm.complete(messages=msgs, model="deepseek-chat")
    # 改为       adapter.complete(msgs, task_class="reasoning")
    # task_class 由内核任务类型决定：
    #   reasoning → 复杂推理 / coding → 编码 / crawl-summarize → 抓取摘要 / cheap → 轻量

适配层不 fork hermes 内核，只在调用点包一层（design §7.1 H5「适配层替换」）。
"""
from __future__ import annotations

from typing import Any, Iterable

from .model.router import ModelRouter
from .model.providers import ModelMessage, ModelResponse


class HermesModelAdapter:
    """hermes 内核模型调用的适配层（H5）。"""

    def __init__(self, router: ModelRouter):
        self.router = router

    def complete(
        self,
        messages: Iterable[ModelMessage],
        *,
        task_class: str = "reasoning",
        **kw: Any,
    ) -> ModelResponse:
        return self.router.complete(messages, task_class=task_class, **kw)

    # 供内核以 dict 形式传消息（与 hermes 现有调用形态一致）
    def complete_dicts(
        self,
        messages: list[dict[str, str]],
        *,
        task_class: str = "reasoning",
        **kw: Any,
    ) -> ModelResponse:
        msgs = [ModelMessage(m["role"], m["content"]) for m in messages]
        return self.complete(msgs, task_class=task_class, **kw)
