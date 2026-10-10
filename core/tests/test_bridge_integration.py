"""Bridge 宿主联调测试（契约 docs/agent-harness-contract.md §2–§5）。

在进程内起**真实** aiohttp 服务（不是 mock），用真实 HTTP 客户端走一遍：
- 握手与版本协商（§2）
- Bearer 鉴权 401（§2）
- RPC 方法表：capability.list / schema / net.status（§3.1）
- 合规拒绝回执 -32001 + compliance.denied 事件（§3.2 + §9A.4）
- 未知方法 -32601（§3.2）
- /frames 帧源不可用时 503 且**显式说明卡在哪**（§5 + §9B.4-3）

这把「真实 Bridge 握手」从「未验证集成项」变成「按契约验证通过」。

约定：与本仓库其它测试一致——同步测试函数，内部用 asyncio.run()，
不引入 pytest-asyncio 依赖。
"""

from __future__ import annotations

import asyncio

import pytest

from hermes_superagent.agent_harness import (
    COMPLIANCE_REJECTED,
    METHOD_NOT_FOUND,
    PROTOCOL_VERSION,
    build_default_runtime,
)
from hermes_superagent.bridge_server import BridgeServer

pytest.importorskip("aiohttp")

TOKEN = "test-token-abc123"


@pytest.fixture()
def runtime():
    return build_default_runtime(token=TOKEN)


@pytest.fixture()
def server(runtime):
    return BridgeServer(runtime, host="127.0.0.1", port=0)


# ----------------------------------------------------------------------
# 测试骨架：起真服务 → 跑断言 → 关服务
# ----------------------------------------------------------------------
def _with_client(server, body):
    """在真实 aiohttp 服务上执行 body(client)。"""

    async def runner():
        from aiohttp.test_utils import TestClient, TestServer

        client = TestClient(TestServer(server.make_app()))
        await client.start_server()
        try:
            return await body(client)
        finally:
            await client.close()

    return asyncio.run(runner())


def _auth_headers(token: str | None = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _rpc(client, method, params=None, rid=1, token=TOKEN):
    payload = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        payload["params"] = params
    return await client.post("/rpc", json=payload, headers=_auth_headers(token))


# ----------------------------------------------------------------------
# 握手与鉴权（§2）
# ----------------------------------------------------------------------
def test_initialize_handshake_succeeds(server):
    async def body(client):
        r = await _rpc(client, "initialize", {"protocol_version": PROTOCOL_VERSION})
        assert r.status == 200
        payload = await r.json()
        assert "result" in payload, payload
        result = payload["result"]
        assert result["ok"] is True
        assert result["protocol_version"] == PROTOCOL_VERSION
        assert result["capabilities"], "握手必须返回能力目录"
        assert result["trace_id"]

    _with_client(server, body)


def test_initialize_rejects_protocol_mismatch(server):
    """§2：版本不匹配必须拒绝并提示，不许静默降级。"""

    async def body(client):
        r = await _rpc(client, "initialize", {"protocol_version": "9.9"})
        payload = await r.json()
        assert "error" in payload
        assert "protocol_mismatch" in payload["error"]["message"]
        assert PROTOCOL_VERSION in payload["error"]["message"]

    _with_client(server, body)


def test_missing_token_is_unauthorized(server):
    """§2 鉴权：无 Bearer token → 401。"""

    async def body(client):
        r = await client.post(
            "/rpc",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert r.status == 401
        payload = await r.json()
        assert payload["error"]["code"] == -32600

    _with_client(server, body)


def test_wrong_token_is_unauthorized(server):
    async def body(client):
        r = await _rpc(client, "initialize", {}, token="wrong-token")
        assert r.status == 401

    _with_client(server, body)


# ----------------------------------------------------------------------
# RPC 方法表（§3.1）
# ----------------------------------------------------------------------
def test_capability_list(server):
    async def body(client):
        r = await _rpc(client, "capability.list")
        payload = await r.json()
        assert "result" in payload, payload
        caps = payload["result"]["capabilities"]
        assert isinstance(caps, list) and caps

    _with_client(server, body)


def test_capability_schema_for_known_capability(server):
    async def body(client):
        r = await _rpc(client, "capability.list")
        caps = (await r.json())["result"]["capabilities"]
        name = caps[0]

        r2 = await _rpc(client, "capability.schema", {"capability": name}, rid=2)
        payload = await r2.json()
        assert "result" in payload, payload
        # capability.schema 直接返回该能力的 schema 本体（不再包一层 "schema"）
        assert payload["result"]["name"] == name

    _with_client(server, body)


def test_net_status_reports_route(server):
    """§3.1 net.status：内核为单一事实源，宿主只读。"""

    async def body(client):
        r = await _rpc(client, "net.status")
        payload = await r.json()
        assert "result" in payload, payload
        result = payload["result"]
        assert result["ok"] is True
        assert "route" in result
        assert result["route"]["region"] == "auto"

    _with_client(server, body)


def test_unknown_method_returns_method_not_found(server):
    async def body(client):
        r = await _rpc(client, "definitely.not.a.method")
        payload = await r.json()
        assert payload["error"]["code"] == METHOD_NOT_FOUND

    _with_client(server, body)


# ----------------------------------------------------------------------
# 合规红线（§9A.4）
# ----------------------------------------------------------------------
def test_compliance_reject_returns_32001_and_emits_event(server, runtime):
    async def body(client):
        r = await _rpc(
            client,
            "compliance.reject",
            {
                "capability": "tool",
                "action": "wechat_personal_send",
                "reason": "线 B 个人号自动化被禁（§9A.1）",
            },
        )
        payload = await r.json()
        assert "error" in payload, payload
        assert payload["error"]["code"] == COMPLIANCE_REJECTED

        events = [e["event"] for e in runtime.recent_events(limit=50)]
        assert "compliance.denied" in events, events

    _with_client(server, body)


# ----------------------------------------------------------------------
# 帧流（§5 + §9B.4-3）：不可用时必须显式说明卡点
# ----------------------------------------------------------------------
def test_frames_reports_explicit_reason_when_source_down(server):
    """帧源不可用 → 503 + 指明卡在哪；可用 → 200。两者都不许静默。"""

    async def body(client):
        r = await client.get("/frames", headers=_auth_headers())
        assert r.status in (200, 503)
        payload = await r.json()
        if r.status == 503:
            assert payload["ok"] is False
            assert "frame source unavailable" in payload["error"]
            # 必须给出排查线索，而不是干瘪的失败
            assert "thumb" in payload["error"]
        else:
            assert payload["ok"] is True

    _with_client(server, body)


def test_frames_requires_auth(server):
    async def body(client):
        r = await client.get("/frames")
        assert r.status == 401

    _with_client(server, body)


# ----------------------------------------------------------------------
# 契约常量两侧一致性
# ----------------------------------------------------------------------
def test_protocol_version_matches_ts_contract_constant():
    """Python 侧 PROTOCOL_VERSION 必须与 TS 侧 contract.ts 镜像常量一致。"""
    from pathlib import Path

    contract = (
        Path(__file__).resolve().parents[2]
        / "harness-plugin"
        / "src"
        / "shared"
        / "contract.ts"
    )
    if not contract.exists():
        pytest.skip("TS 契约文件不存在（仅 Python 侧可用）")
    text = contract.read_text(encoding="utf-8")
    assert f'"{PROTOCOL_VERSION}"' in text or f"'{PROTOCOL_VERSION}'" in text, (
        "TS contract.ts 与 Python PROTOCOL_VERSION 不一致，会导致握手失败"
    )


# ----------------------------------------------------------------------
# 装配性能：httpx.Client 必须懒建（回归护栏）
# ----------------------------------------------------------------------
def test_runtime_assembly_is_fast_and_clients_are_lazy():
    """装配 runtime 不应创建 httpx.Client。

    背景：`httpx.Client()` 在 Windows 上约 0.75s，五个 provider 会白花 ~3.7s。
    改为懒建后装配应为毫秒级。本测试锁住这个行为，防止回归。
    """
    import time

    from hermes_superagent.capabilities.model.providers import build_default_providers

    t0 = time.perf_counter()
    rt = build_default_runtime(token="perf")
    elapsed = time.perf_counter() - t0
    assert rt.capabilities, "装配后必须有能力"
    assert elapsed < 1.5, f"runtime 装配过慢（{elapsed:.2f}s）：httpx.Client 可能被提前创建了"

    providers = build_default_providers()
    assert providers
    assert all(p._client is None for p in providers), "provider 不应在构造时创建 httpx.Client"


def test_provider_client_is_created_on_first_use_and_closed():
    from hermes_superagent.capabilities.model.providers import build_default_providers

    provider = build_default_providers()[0]
    assert provider._client is None
    client = provider.client
    assert provider._client is not None
    assert client is provider.client, "重复访问应复用同一 client"
    provider.close()
    assert provider._client is None
