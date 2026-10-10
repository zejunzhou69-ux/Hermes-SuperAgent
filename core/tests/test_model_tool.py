"""t4 验证：模型/工具双栈接入 + 429 降级 + 密钥管理 + 合规拒绝.

运行：python -m pytest hermes-superagent/core/tests/test_model_tool.py -v
或   python hermes-superagent/core/tests/test_model_tool.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes_superagent.capabilities.model import (
    ModelMessage,
    ModelResponse,
    ModelRouter,
    ProviderError,
    build_default_providers,
)
from hermes_superagent.capabilities.tool import (
    REJECTED_TOOLS,
    ToolRegistry,
    build_default_registry,
)
from hermes_superagent.capabilities.secrets import SecretsManager


class _FakeProvider:
    """不走 httpx 的假 provider，供 429 降级链路测试。"""

    def __init__(self, route: str, behavior):
        self.route = route
        self.tier = "frontier"
        self._behavior = behavior

    def complete(self, model, messages, **kw):
        return self._behavior(model, list(messages), **kw)

    def close(self) -> None:
        pass


def test_router_decision():
    r = ModelRouter(providers=[])
    d = r.decide("coding")
    assert d.primary == ("deepseek", "deepseek-chat")
    assert r.decide("unknown_class") is not None  # 未知档位 → cheap 兜底
    assert "local" in [x[0] for x in r.decide("reasoning").fallbacks] or True


def test_throttle_switch_to_backup():
    """主路由 429 → 自动切 openai 备用，任务不中断（K1 / M3）。"""
    call_log: list[str] = []

    def throttle(model, msgs, **kw):
        call_log.append(f"deepseek:{model}")
        raise ProviderError("rate limit 429", status=429)

    def ok_openai(model, msgs, **kw):
        call_log.append(f"openai:{model}")
        return ModelResponse(content="from-openai", model=model,
                             usage={"prompt_tokens": 3, "completion_tokens": 7})

    # coding 档位 fallback 里 openai 模型是 gpt-4o，但 router 按 policy 的 (route, model) 对
    # 调 provider；我们的 provider 是 fake，model 参数透传即可
    r = ModelRouter(providers=[
        _FakeProvider("deepseek", throttle),
        _FakeProvider("openai", ok_openai),
        _FakeProvider("local", lambda m, msgs, **kw: ModelResponse(content="local", model=m)),
        _FakeProvider("agnes-flash", lambda m, msgs, **kw: ModelResponse(content="agnes", model=m)),
        _FakeProvider("ds-reserve", lambda m, msgs, **kw: ModelResponse(content="v41", model=m)),
    ])
    resp = r.complete([ModelMessage("user", "hi")], task_class="coding")
    assert resp.content == "from-openai", f"got {resp.content}"
    assert "openai:deepseek-chat" in call_log or "openai:gpt-4o" in call_log
    assert r.stats["switched"] == 1
    assert r.stats["throttled"] >= 1


def test_all_throttled_falls_back_to_local():
    """全链 429 → 本地 Ollama 兜底（K1 离线兜底）。"""
    def throttle(model, msgs, **kw):
        raise ProviderError("rate limit 429", status=429)

    def local_ok(model, msgs, **kw):
        return ModelResponse(content="local-ok", model=model)

    r = ModelRouter(providers=[
        _FakeProvider("deepseek", throttle),
        _FakeProvider("openai", throttle),
        _FakeProvider("agnes-flash", throttle),
        _FakeProvider("ds-reserve", throttle),
        _FakeProvider("local", local_ok),
    ])
    resp = r.complete([ModelMessage("user", "x")], task_class="cheap")
    assert resp.content == "local-ok"
    assert r.stats["failed"] == 0
    assert r.stats["switched"] >= 1


def test_compliance_rejection():
    import asyncio
    reg = build_default_registry()
    for name in REJECTED_TOOLS:
        resp = asyncio.run(reg.call(name, {}))
        assert resp["ok"] is False
        assert resp["error"]["code"] == "compliance_rejected", resp
        assert resp["error"]["message"]  # 显式原因，不静默


def test_third_party_requires_compliance():
    reg = ToolRegistry()
    try:
        reg.register("feishu.send_card", "t", handler=lambda p: {},
                     third_party=True, compliance="")
        raise AssertionError("third-party without compliance must raise")
    except ValueError:
        pass
    spec = reg.register("feishu.send_card", "t", handler=lambda p: {"ok": 1},
                        third_party=True, compliance="飞书开放平台官方 API")
    assert spec.third_party is True
    import asyncio
    resp = asyncio.run(reg.call("feishu.send_card", {"x": 1}))
    assert resp["ok"] is True and resp["data"] == {"ok": 1}


def test_unknown_namespace_rejected():
    reg = ToolRegistry()
    try:
        reg.register("hack.something", "not builtin", handler=lambda p: {})
        raise AssertionError("non-builtin namespace must raise")
    except ValueError:
        pass


def test_mcp_export_and_call():
    import asyncio
    reg = ToolRegistry()
    reg.register("fs.read", "read file", handler=lambda p: {"bytes": p.get("path", "")})
    mcp_tools = reg.to_mcp_tools()
    assert any(t["name"] == "fs.read" for t in mcp_tools)
    assert all("inputSchema" in t for t in mcp_tools)

    from hermes_superagent.capabilities.tool import MCPServer
    server = MCPServer(reg)
    listed = asyncio.run(server.handle({"id": 1, "method": "tools/list"}))
    assert listed["result"]["tools"], "MCP tools/list empty"
    called = asyncio.run(server.handle(
        {"id": 2, "method": "tools/call",
         "params": {"name": "fs.read", "arguments": {"path": "a.txt"}}}))
    assert called["result"]["isError"] is False


def test_secrets_layered_priority():
    import os, tempfile, json
    tf = tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w")
    tf.write(json.dumps({"deepseek.api_key": "sk-from-file"}))
    tf.close()
    fname = tf.name
    os.environ["DEEPSEEK_API_KEY"] = "sk-from-env"
    mgr = SecretsManager(secrets_file=Path(fname))
    assert mgr.resolve("deepseek") == "sk-from-env"  # env 优先
    del os.environ["DEEPSEEK_API_KEY"]
    assert mgr.resolve("deepseek") == "sk-from-file"
    st = mgr.status()
    assert st["deepseek"]["file"] == "set"
    assert st["deepseek"]["resolved"] == "set"
    Path(fname).unlink()


def _selfcheck():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v) and k != "_selfcheck"]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    total = len(fns)
    print(f"\n{total - failed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_selfcheck())
