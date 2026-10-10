"""协作框架（t3）无 LLM 单测：五角色拓扑 + 路由不变式 + 长任务逃生舱边界。

覆盖 docs/collab-framework.md §4 的三条不变式：
1. 任何"取数据"必经 Crawl Capability（爬虫手不自带裸 httpx）。
2. 任何产物必经 Verifier，才能进入 Reporter。
3. 失败必须产出"决策记录"（层级流 escalate），不允许静默吞错。
以及 §5 LangGraph 逃生舱触发边界。
"""

from __future__ import annotations

import asyncio

from hermes_superagent.collab import (
    create_crew_pipeline,
    route_task,
    load_roles_yaml,
    LONG_TASK_THRESHOLD_MIN,
    make_langgraph_fallback,
    SEQUENTIAL_CHAIN,
)
from hermes_superagent.collab.agents import Crawler
from hermes_superagent.collab.models import TaskDef, ProcessMode


class _FakeCrawl:
    """模拟 Crawl Capability：返回固定抓取结果，便于断言数据出口。"""

    def __init__(self, ok=True, content="hello-crawled"):
        self._ok = ok
        self._content = content
        self.calls = 0

    async def invoke(self, action, params):
        self.calls += 1
        if action == "fetch":
            return {
                "ok": self._ok,
                "level": "L1",
                "data": {"content": self._content if self._ok else ""},
                "trace_id": "t1",
            }
        return {"ok": False, "error": "unknown action"}


# ----------------------------------------------------------------------
# 拓扑：五角色齐备且顺序链正确
# ----------------------------------------------------------------------
def test_five_roles_exist():
    roles = load_roles_yaml()
    assert set(roles) == {"supervisor", "crawler", "coder", "verifier", "reporter"}


def test_sequential_chain_order():
    runner = create_crew_pipeline("goal", context={"url": "https://example.com"})
    chain = runner._hierarchical_chain()  # 层级链 = supervisor + 顺序链
    assert chain[0] == "supervisor"
    assert chain[1:] == SEQUENTIAL_CHAIN == ["crawler", "coder", "verifier", "reporter"]


def test_route_task_maps_class_to_role():
    assert route_task(TaskDef(task_class="crawl")).target_role == "crawler"
    assert route_task(TaskDef(task_class="code")).target_role == "coder"
    assert route_task(TaskDef(task_class="verify")).target_role == "verifier"
    assert route_task(TaskDef(task_class="report")).target_role == "reporter"
    assert route_task(TaskDef(task_class="default")).target_role == "supervisor"


# ----------------------------------------------------------------------
# 不变式 1：爬虫手数据出口 = Crawl Capability（不得裸 httpx）
# ----------------------------------------------------------------------
def test_crawler_uses_injected_capability():
    cap = _FakeCrawl()
    crawler = Crawler()
    crawler.set_crawl_capability(cap)

    async def go():
        return await crawler.run(TaskDef(task_class="crawl", context="https://example.com"), {})

    res = asyncio.run(go())
    assert cap.calls == 1, "爬虫手必须经 Crawl Capability 取数据"
    assert res.payload.get("content") == "hello-crawled"


def test_crawler_without_capability_fails_loudly():
    crawler = Crawler()  # 未注入能力
    res = asyncio.run(crawler.run(TaskDef(task_class="crawl", context="https://x"), {}))
    assert res.ok is False
    assert "crawl capability not injected" in res.payload.get("error", "")


# ----------------------------------------------------------------------
# 不变式 2：产物必经 Verifier → Reporter；不变式 3：失败产出决策记录
# ----------------------------------------------------------------------
def test_full_chain_verifier_gates_reporter():
    runner = create_crew_pipeline("goal", context={"url": "https://example.com"})
    runner.inject("crawler", _make_crawler())

    async def go():
        return await runner.run(TaskDef(title="goal", description="goal", task_class="default"))

    result = asyncio.run(go())
    roles_in_order = [r.role for r in result.results]
    assert roles_in_order == ["crawler", "coder", "verifier", "reporter"]
    assert roles_in_order.index("verifier") < roles_in_order.index("reporter")
    assert result.verdict == "pass"


def test_failure_produces_decision_record_not_silent():
    runner = create_crew_pipeline(
        "goal",
        context={"url": "https://example.com"},
        process=ProcessMode.HIERARCHICAL,
    )
    runner.inject("crawler", _make_crawler(ok=False))

    async def go():
        return await runner.run(TaskDef(title="goal", description="goal", task_class="default"))

    result = asyncio.run(go())
    decisions = runner.context.get("decisions", [])
    assert any(d.get("action") == "escalate_to_supervisor" for d in decisions)
    failed = [r for r in result.results if not r.ok]
    assert failed, "失败必须被显式记录"


# ----------------------------------------------------------------------
# 长任务逃生舱边界（§5）
# ----------------------------------------------------------------------
def test_langgraph_fallback_triggers_only_when_long_or_human():
    task = TaskDef(title="long task")
    assert make_langgraph_fallback(task, estimated_minutes=LONG_TASK_THRESHOLD_MIN + 1) is not None
    assert make_langgraph_fallback(task, need_human=True) is not None
    assert make_langgraph_fallback(task, estimated_minutes=5, need_human=False) is None


def _make_crawler(ok=True):
    crawler = Crawler()
    crawler.set_crawl_capability(_FakeCrawl(ok=ok, content="data" if ok else ""))
    return crawler
