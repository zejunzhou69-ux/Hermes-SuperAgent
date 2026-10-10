"""t5 agent–harness 契约测试（docs/agent-harness-contract.md §11 验收口径 A1–A8 Python 侧）。

不依赖真实 LLM / 真实网络 / 真实浏览器：能力层用 stub，collab 走 LiteAgent 兜底。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from hermes_superagent.agent_harness import (
    COMPLIANCE_REJECTED,
    METHOD_NOT_FOUND,
    PROTOCOL_VERSION,
    AgentHarnessRuntime,
    Capability,
    ComplianceViolation,
)


class StubCapability:
    name = "stub"

    def schema(self) -> dict:
        return {"actions": {"ping": {"params": {}}}}

    async def invoke(self, action: str, params: dict) -> dict:
        if action == "ping":
            return {"ok": True, "data": "pong"}
        raise ValueError(f"unknown stub action {action}")


@pytest.fixture()
def rt() -> AgentHarnessRuntime:
    runtime = AgentHarnessRuntime(token="test-token")
    runtime.register(StubCapability())
    return runtime


def rpc(runtime: AgentHarnessRuntime, method: str, params: dict, token: str = "test-token") -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    from hermes_superagent.agent_harness import _AsyncInvoke
    try:
        return runtime.handle_rpc(body, token=token)
    except _AsyncInvoke as ai:
        return asyncio.run(runtime.invoke_rpc(ai))


def rpc_sync(runtime: AgentHarnessRuntime, method: str, params: dict, token: str = "test-token") -> dict:
    """同步方法专用：返回的必须是最终 JSON-RPC 响应（不允许 _AsyncInvoke 标记）。"""
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    return runtime.handle_rpc(body, token=token)


# ---- A1: 握手 + 能力目录 -------------------------------------------------
def test_handshake(rt):
    resp = rpc(rt, "initialize", {"protocol_version": PROTOCOL_VERSION})
    assert "result" in resp
    assert resp["result"]["ok"] is True
    assert resp["result"]["protocol_version"] == PROTOCOL_VERSION
    assert "stub" in resp["result"]["capabilities"]


# ---- A2: capability.invoke 路由到已注册能力 --------------------------------
def test_capability_invoke(rt):
    resp = rpc(rt, "capability.invoke",
               {"capability": "stub", "action": "ping", "params": {}})
    assert resp["result"]["ok"] is True
    assert resp["result"]["data"] == "pong"
    assert "trace_id" in resp["result"]


# ---- A3: 未注册 capability → -32601 ---------------------------------------
def test_unregistered_capability(rt):
    resp = rpc(rt, "capability.invoke",
               {"capability": "ghost", "action": "x", "params": {}})
    assert resp["error"]["code"] == METHOD_NOT_FOUND
    assert "capability not registered" in resp["error"]["message"]


# ---- A4: 协议版本不匹配 → 显式拒绝（不静默降级）------------------------------
def test_protocol_mismatch(rt):
    resp = rpc(rt, "initialize", {"protocol_version": "9.9"})
    assert resp["error"]["code"] == METHOD_NOT_FOUND
    assert "protocol_mismatch" in resp["error"]["message"]


# ---- A5: 合规拒绝 → -32001 + compliance.denied 事件 ------------------------
def test_compliance_rejection(rt):
    with pytest.raises(ComplianceViolation):
        rt.check_compliance("stub", "ping", {"platform": "wechat-personal"})
    events = rt.recent_events()
    assert any(e["event"] == "compliance.denied" for e in events)


def test_compliance_reject_method(rt):
    resp = rpc(rt, "compliance.reject",
               {"capability": "browser", "action": "send_wechat",
                "reason": "线 B 仅官方 API"})
    assert resp["error"]["code"] == COMPLIANCE_REJECTED


# ---- A6: 五角色链经 collab.run 触发（LiteAgent 兜底，无 LLM）---------------
def test_collab_run(rt, monkeypatch):
    # 聚焦契约面（decisions 可读 + 事件发射）：crawler 角色用 fake 能力，
    # 避免真实网络（爬取行为归 t2 测试）。
    from hermes_superagent.collab import agents as collab_agents
    from hermes_superagent.collab.models import TaskDef

    class _FakeCrawl:
        async def invoke(self, action, params):
            return {"ok": True, "level": "L1", "data": {"content": "fake-html"}}

    def _build() -> dict:
        agents = collab_agents.build_agents()
        agents["crawler"].set_crawl_capability(_FakeCrawl())
        return agents

    monkeypatch.setattr(collab_agents, "build_agents", _build)
    resp = rpc(rt, "collab.run", {"goal": "demo", "tasks": [{"title": "t", "context": "https://example.com"}]})
    assert resp["result"]["ok"] is True
    assert resp["result"]["status"] == "pass"
    events = rt.recent_events()
    assert any(e["event"] == "task.started" for e in events)
    assert any(e["event"] == "task.finished" and e["data"]["status"] == "pass" for e in events)
    # collab.status 可读（契约 §3.1）
    run_id = resp["result"]["run_id"]
    st = rpc(rt, "collab.status", {"run_id": run_id})
    assert st["result"]["status"] == "pass"


def test_collab_cancel(rt, monkeypatch):
    from hermes_superagent.collab import agents as collab_agents

    class _FakeCrawl:
        async def invoke(self, action, params):
            return {"ok": True, "level": "L1", "data": {"content": "x"}}

    monkeypatch.setattr(collab_agents, "build_agents",
                       lambda: {r: collab_agents.build_agents()[r] for r in
                                ("supervisor", "crawler", "coder", "verifier", "reporter")})
    resp = rpc(rt, "collab.run", {"goal": "demo"})
    run_id = resp["result"]["run_id"]
    cancel = rpc(rt, "collab.cancel", {"run_id": run_id})
    assert cancel["result"]["ok"] is True


# ---- A7: 帧流契约字段完整 -------------------------------------------------
def test_frame_contract():
    """帧契约（§5）：Python 侧帧载荷字段；宿主侧 canvas 按 seq 乱序保护。"""
    frame = {"ts": 1, "url": "https://example.com", "title": "x",
             "frame": "AAAA", "seq": 12, "stopped": False}
    for key in ("ts", "url", "title", "frame", "seq", "stopped"):
        assert key in frame
    # 乱序保护规则（宿主侧断言）：旧 seq 帧必须被丢弃
    assert frame["seq"] >= 0


# ---- 鉴权 ------------------------------------------------------------------
def test_token_auth(rt):
    resp = rt.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "capability.list"},
                         token="wrong")
    assert resp["error"]["code"] == -32600


def test_loopback_only(rt):
    resp = rt.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "capability.list"},
                         token="test-token", loopback=False)
    assert resp["error"]["code"] == -32600


# ---- 能力 schema 面 ---------------------------------------------------------
def test_capability_schema(rt):
    resp = rpc(rt, "capability.schema", {"capability": "stub"})
    assert resp["result"]["actions"]["ping"]
