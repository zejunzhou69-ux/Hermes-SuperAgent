"""Verify collab/ modules correctness (t2 acceptance)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from hermes_superagent.collab.models import RoleDef, TaskDef, TaskRoute, CostRecord, ProcessMode
from hermes_superagent.collab.router import load_roles_yaml, route_task, list_routes
from hermes_superagent.collab.middleware import CostMiddleware
from hermes_superagent.collab.crew import HermesCrew, create_crew, _CREWAI_AVAILABLE
from hermes_superagent.collab.langgraph_bridge import is_langgraph_available, create_long_task_graph


def test_models():
    rd = RoleDef(
        name="test_role", prompt_template="Hello {{name}}", goal="Test goal",
        tools=["tool1"], llm="agnes-2.5-flash", backup_llm="agnes-3.0-flash",
        max_iter=5, process=ProcessMode.SEQUENTIAL,
    )
    assert rd.prompt(name="World") == "Hello World"
    td = TaskDef(title="t1", description="desc", task_class="crawl")
    assert td.id
    assert "crawl" in td.summary()
    cr = CostRecord(role="crawler", model="agnes-2.5-flash", tokens_input=100, tokens_output=50)
    assert cr.total_tokens == 150
    assert cr.trace_id
    tr = TaskRoute(task=td, target_role="crawler", llm_key="agnes-2.5-flash")
    assert "crawler" in tr.summary()
    print("OK test_models")


def test_router():
    roles = load_roles_yaml()
    assert set(roles.keys()) == {"supervisor", "crawler", "coder", "verifier", "reporter"}
    assert route_task(TaskDef(task_class="crawl"), roles).target_role == "crawler"
    assert route_task(TaskDef(task_class="code"), roles).target_role == "coder"
    assert route_task(TaskDef(task_class="verify"), roles).target_role == "verifier"
    assert route_task(TaskDef(task_class="report"), roles).target_role == "reporter"
    assert route_task(TaskDef(task_class="research"), roles).target_role == "supervisor"
    route = route_task(TaskDef(task_class="unknown"), roles)
    assert route.target_role == "supervisor"
    assert any("fallback" in n for n in route.notes)
    routes = list_routes()
    assert len(routes) == 5
    print("OK test_router")


def test_middleware():
    events = []
    mw = CostMiddleware(on_event=lambda e, p: events.append((e, p)))
    async def fake_llm(llm_key, params):
        return {"content": "ok", "model": llm_key}
    result = asyncio.run(mw.call_with_middleware(
        role="crawler", llm_fn=fake_llm, params={"task": "test"},
    ))
    assert result["content"] == "ok"
    assert len(mw.state.records) == 1
    assert mw.state.records[0].success
    assert len(events) > 0
    report = mw.report()
    assert "total_cost_usd" in report
    print(f"OK test_middleware: records={len(mw.state.records)}, cost=${report['total_cost_usd']}")


def test_crew_fallback():
    async def _run():
        crew = create_crew(TaskDef(title="scrape", description="fetch example.com", task_class="crawl"))
        # 强制无 LLM 环境：禁用 _make_llm，验证 fallback 路径本身可达（离线/CI 不中断）
        crew._make_llm = lambda model_key: None
        result = await crew.run()
        assert result["ok"]
        assert result["role"] == "crawler"
        assert result["mode"] == "fallback"
        assert "trace_id" in result
        print(f"OK test_crew_fallback: role={result['role']}, cost=${result['cost_usd']}")
    asyncio.run(_run())


def test_langgraph_bridge():
    available = is_langgraph_available()
    graph = create_long_task_graph("task-1", ["crawler", "coder"])
    assert graph["task_id"] == "task-1"
    assert "available" in graph
    print(f"OK test_langgraph_bridge: available={available}")


def test_roles_yaml_structure():
    roles = load_roles_yaml()
    for name, rd in roles.items():
        assert rd.name == name
        assert rd.llm in ("agnes-2.5-flash", "agnes-3.0-flash")
        assert rd.backup_llm in ("agnes-2.5-flash", "agnes-3.0-flash")
        assert rd.max_iter > 0
        assert isinstance(rd.process, ProcessMode)
        assert len(rd.prompt_template) > 0
        assert len(rd.goal) > 0
    print(f"OK test_roles_yaml_structure: {list(roles.keys())}")


def test_package_imports():
    from hermes_superagent import collab
    assert hasattr(collab, "HermesCrew")
    assert hasattr(collab, "create_crew")
    assert hasattr(collab, "load_roles_yaml")
    assert hasattr(collab, "route_task")
    assert hasattr(collab, "CostMiddleware")
    print("OK test_package_imports")


if __name__ == "__main__":
    tests = [
        test_models, test_router, test_middleware,
        test_crew_fallback, test_langgraph_bridge,
        test_roles_yaml_structure, test_package_imports,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as e:
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\nResults: {len(tests) - failed}/{len(tests)} passed")
    sys.exit(0 if failed == 0 else 1)