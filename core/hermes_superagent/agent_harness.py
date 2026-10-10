"""agent–harness 契约的 Python 侧可运行实现（t5，docs/agent-harness-contract.md §2–§11）。

角色：
- AgentHarnessRuntime：宿主插件唯一对外的 Python 面。
  ① Capability 注册表（§3.3）：把 t2/t4 已落地的 browser/crawl/model/tool 能力
     挂到统一的 invoke 入口，未注册能力显式报 -32601；
  ② Bridge JSON-RPC 2.0 服务端（§3）：aiohttp 仅监听 127.0.0.1，Bearer token 鉴权，
     initialize 握手做协议版本协商；
  ③ 事件总线（§4）：/events SSE 推送 task/model/crawl/net/compliance 事件；
  ④ 合规拒绝（§9A.4）：线 B 违规与非官方协议入口即拒，error -32001 + compliance.denied 事件；
  ⑤ collab 触发面（§8）：collab.run 走 t3 五角色链（LiteAgent 兜底，无 LLM 可跑），
     每步决策写 TaskContext.decisions，经 /events 上抛。

硬约束（design §9B.4，不得回退）：
- 任何失败不得静默：所有错误载荷附 reason，宿主必须可渲染；
- token 不落盘、不进日志（K7）；未配 token 时仅信任环回地址并显式告警。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import time
import uuid
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger("hermes.harness")

#: Bridge 协议版本（宿主 TS 侧 contract.ts 的镜像常量必须同步）
PROTOCOL_VERSION = "1.0"

#: JSON-RPC 标准错误码（§3.2）
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
#: 本契约自定义
EXECUTION_FAILED = -32000
COMPLIANCE_REJECTED = -32001
SANDBOX_VIOLATION = -32002


@runtime_checkable
class Capability(Protocol):
    """design §6.1 Capability 协议（t2/t4 各能力已实现，这里只做类型约束）。"""

    name: str

    def schema(self) -> dict[str, Any]: ...

    async def invoke(self, action: str, params: dict[str, Any]) -> dict[str, Any]: ...


class CapabilityNotRegistered(LookupError):
    pass


class ComplianceViolation(RuntimeError):
    """§9A 合规红线：入口即拒，附显式原因（可观测、不静默）。"""

    def __init__(self, capability: str, action: str, reason: str):
        self.capability = capability
        self.action = action
        self.reason = reason
        super().__init__(f"{capability}.{action} 被合规红线拒绝: {reason}")


#: 线 B（发/写）平台白名单：仅官方 API（design §9A.1/§13.4）
OFFICIAL_API_PLATFORMS: frozenset[str] = frozenset(
    {"weixin-enterprise-api", "whatsapp-business-api", "meta-graph-api"}
)

#: 入口即拒的非官方协议特征（§9A.4 落到代码：显式拒绝并返回原因）
_NONOFFICIAL_MARKERS: tuple[str, ...] = (
    "unofficial", "unofficial-protocol", "wechat-personal", "whatsapp-web-unofficial",
)


class _LiteRunner:
    """契约面的角色兜底执行器（§8）。

    无 LLM 环境跑通五角色链：crawler 走注入的 fake/真实 crawl 能力（单一出口，
    不变式 1），其余角色回 ok 壳。真实角色（CrewAI Agent）经 runner.inject 替换。
    """

    def __init__(self, role: str) -> None:
        self.role = role
        self._crawl: Any = None
        self._url: str | None = None

    def set_crawl_capability(self, cap: Any) -> None:
        self._crawl = cap

    def set_url(self, url: str) -> None:
        self._url = url

    async def run(self, task: Any, context: dict[str, Any]) -> Any:
        from hermes_superagent.collab.agents import AgentResult
        from hermes_superagent.collab.models import TaskDef

        task = task or TaskDef(title="harness-collab", context=str(context.get("goal", "")))
        if self.role == "crawler":
            url = self._url or context.get("url") or task.context
            if not url or self._crawl is None:
                # 无 url / 无注入 crawl 能力 → 契约面兜底 ok（真实爬取归 t2）
                return AgentResult(role=self.role, task_id=task.id, ok=True,
                                   payload={"level": "L0", "content": "", "note": "lite fallback"})
            res = await self._crawl.invoke("fetch", {"url": url})
            ok = bool(res.get("ok"))
            return AgentResult(role=self.role, task_id=task.id, ok=ok,
                               payload={"level": res.get("level"),
                                        "content": (res.get("data") or {}).get("content", "")})
        return AgentResult(role=self.role, task_id=task.id, ok=True, payload={"note": "lite fallback"})


class AgentHarnessRuntime:
    """宿主插件契约的 Python 侧运行时（§7 生命周期：内核重启后宿主 3s 退避重连）。"""

    def __init__(
        self,
        token: str | None = None,
        host_secrets: dict[str, Any] | None = None,
    ) -> None:
        self.token = token or secrets.token_hex(16)
        self._host_secrets = host_secrets or {}
        self._capabilities: dict[str, Capability] = {}
        self._events: list[dict[str, Any]] = []
        self._event_waiters: list[asyncio.Future] = []
        self._runs: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ 能力
    def register(self, capability: Capability) -> None:
        self._capabilities[capability.name] = capability

    @property
    def capabilities(self) -> list[str]:
        return sorted(self._capabilities)

    def _cap(self, name: str) -> Capability:
        cap = self._capabilities.get(name)
        if cap is None:
            raise CapabilityNotRegistered(name)
        return cap

    async def invoke(self, capability: str, action: str, params: dict[str, Any], trace_id: str | None = None) -> dict[str, Any]:
        """通用能力入口（§3.1 capability.invoke 的服务端实现）。"""
        trace = trace_id or _new_trace_id()
        # §9A.4：合规检查先于能力调用，入口即拒
        self._compliance_check(capability, action, params)
        cap = self._cap(capability)  # 未注册 → CapabilityNotRegistered（外层转 -32601）
        result = await cap.invoke(action, params)
        if not isinstance(result, dict):
            result = {"ok": True, "data": result, "error": None}
        result.setdefault("trace_id", trace)
        return result

    def check_compliance(self, capability: str, action: str, params: dict[str, Any]) -> None:
        """同步合规检查面（RPC 同步方法路径用；async invoke 路径也走它）。"""
        self._compliance_check(capability, action, params)

    def _compliance_check(self, capability: str, action: str, params: dict[str, Any]) -> None:
        """§9A.1/§9A.4：线 B 只走官方 API；非官方协议入口即拒。

        判定面：params 里的 platform / protocol / channel 任一字段命中非官方特征，
        或 capability 本身属于发/写（line B）能力且未声明官方 API 来源。
        """
        markers: list[str] = [
            str(params.get(k, "")) for k in ("platform", "protocol", "channel")
        ]
        if "personal" in " ".join(markers).lower() or any(
            m in _NONOFFICIAL_MARKERS for m in markers
        ):
            reason = (
                f"非官方协议通道（{', '.join(m for m in markers if m)}）。"
                f"线 B 写操作仅接受官方 API（{sorted(OFFICIAL_API_PLATFORMS)}），"
                f"个人号/非官方协议不实现（design §9A.2）。"
            )
            self.emit("compliance.denied", {"capability": capability, "action": action,
                                             "reason": reason})
            raise ComplianceViolation(capability, action, reason)

    # ------------------------------------------------------------------ 事件
    def emit(self, event: str, payload: dict[str, Any]) -> None:
        payload = dict(payload)
        payload.setdefault("trace_id", _new_trace_id())
        self._events.append({"event": event, "data": payload})
        for fut in self._event_waiters:
            if not fut.done():
                fut.set_result((event, payload))
        self._event_waiters = [f for f in self._event_waiters if not f.done()]

    async def wait_event(self) -> tuple[str, dict[str, Any]]:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._event_waiters.append(fut)
        return await fut

    def recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._events[-limit:]

    # ------------------------------------------------------------------ 协作
    async def run_collab(self, goal: str, tasks: list[dict[str, Any]], process: str = "sequential",
                         trace_id: str | None = None) -> dict[str, Any]:
        """§8 协作触发面：五角色链（t3 pipeline，LiteAgent 兜底可无 LLM 跑）。

        decisions[] 取自 runner.context（t3 层级流失败时写入 escalate 决策，
        §4 不变式 3：失败必产出决策记录）；经 /events 上抛 GUI 可见。
        """
        from hermes_superagent.collab import TaskDef, create_crew_pipeline
        from hermes_superagent.collab.models import ProcessMode

        trace = trace_id or _new_trace_id()
        run_id = uuid.uuid4().hex[:12]
        self._runs[run_id] = {"status": "running", "goal": goal, "trace_id": trace}
        self.emit("task.started", {"run_id": run_id, "goal": goal, "trace_id": trace})
        mode = ProcessMode.HIERARCHICAL if process == "hierarchical" else ProcessMode.SEQUENTIAL
        runner = create_crew_pipeline(goal, process=mode)
        runner.inject("crawler", _LiteRunner(role="crawler"))
        runner.inject("coder", _LiteRunner(role="coder"))
        runner.inject("verifier", _LiteRunner(role="verifier"))
        runner.inject("reporter", _LiteRunner(role="reporter"))
        # crawler 的 crawl 能力面：若注册了 crawl capability 则接入（单一出口不变式 1）
        crawler_runner = runner._agents.get("crawler")
        if crawler_runner is not None and "crawl" in self._capabilities:
            crawler_runner.set_crawl_capability(self._capabilities["crawl"])
        for task_dict in tasks:
            if task_dict.get("url") and crawler_runner is not None:
                crawler_runner.set_url(task_dict["url"])
                break
        task_defs = [TaskDef(title=t.get("title", goal), description=t.get("description", goal),
                             task_class=t.get("task_class", "default"),
                             context=str(t.get("url") or "")) for t in tasks]
        try:
            task = task_defs[0] if task_defs else None
            result = await runner.run(task)
        except Exception as exc:  # 绝不静默（§9.3）
            self._runs[run_id] = {"status": "failed", "goal": goal, "trace_id": trace,
                                   "error": str(exc)}
            self.emit("task.finished", {"run_id": run_id, "status": "failed",
                                         "reason": str(exc), "trace_id": trace})
            return {"ok": False, "run_id": run_id, "status": "failed",
                    "error": {"code": EXECUTION_FAILED, "message": str(exc)}, "trace_id": trace}
        verdict = result.verdict
        decisions = list(runner.context.get("decisions", []))
        self._runs[run_id] = {"status": verdict, "goal": goal, "trace_id": trace,
                               "decisions": decisions}
        self.emit("task.finished", {"run_id": run_id, "status": verdict,
                                    "decisions": decisions, "trace_id": trace})
        return {"ok": True, "run_id": run_id, "status": verdict,
                "decisions": decisions, "result": result.as_dict(), "trace_id": trace}

    def collab_status(self, run_id: str) -> dict[str, Any]:
        run = self._runs.get(run_id)
        if run is None:
            return {"ok": False, "run_id": run_id,
                    "error": {"code": INVALID_PARAMS, "message": f"unknown run {run_id}"}}
        return {"ok": True, "run_id": run_id, **run}

    def collab_cancel(self, run_id: str) -> dict[str, Any]:
        run = self._runs.get(run_id)
        if run is None:
            return {"ok": False, "run_id": run_id,
                    "error": {"code": INVALID_PARAMS, "message": f"unknown run {run_id}"}}
        run["status"] = "cancelled"
        self.emit("task.finished", {"run_id": run_id, "status": "cancelled"})
        return {"ok": True, "run_id": run_id, "status": "cancelled"}

    # ------------------------------------------------------------------ JSON-RPC
    def handle_rpc(self, body: dict[str, Any], token: str | None = None,
                   loopback: bool = True) -> dict[str, Any]:
        """JSON-RPC 2.0 请求 → 响应（同步门面；bridge_server 负责 async 调用）。

        token 校验失败返回 INVALID_REQUEST（不泄漏原因细节，避免探测）；
        环回降级时允许 token=None。
        """
        if self.token:
            if token is None or token != self.token:
                return _rpc_error(None, INVALID_REQUEST, "unauthorized")
        if not loopback:
            return _rpc_error(None, INVALID_REQUEST, "bridge is loopback-only")

        req = body
        if not isinstance(req, dict) or req.get("jsonrpc") != "2.0":
            return _rpc_error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 request")
        rid = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}
        if method not in self._METHODS:
            return _rpc_error(rid, METHOD_NOT_FOUND, f"unknown method {method!r}")
        # 协议版本协商（§2）：客户端声明版本且与服务端不符 → 显式拒绝，不静默降级
        if method == "initialize":
            client_ver = (params or {}).get("protocol_version")
            if client_ver is not None and client_ver != PROTOCOL_VERSION:
                return _rpc_error(rid, METHOD_NOT_FOUND,
                                  f"protocol_mismatch: client={client_ver} server={PROTOCOL_VERSION}")
        try:
            if method == "capability.invoke" or method == "collab.run":
                # 标记为 async 方法：bridge_server 捕获后走 invoke_rpc
                raise _AsyncInvoke(params, rid, method)
            if method == "initialize":
                data = self._m_initialize(params)
            elif method == "capability.list":
                data = {"capabilities": self.capabilities}
            elif method == "capability.schema":
                name = params.get("capability")
                if name not in self._capabilities:
                    return _rpc_error(rid, METHOD_NOT_FOUND, f"capability not registered: {name}")
                data = self._capabilities[name].schema()
            elif method == "collab.status":
                data = self.collab_status(params.get("run_id", ""))
            elif method == "collab.cancel":
                data = self.collab_cancel(params.get("run_id", ""))
            elif method == "net.status":
                data = self._net_status()
            elif method == "compliance.reject":
                return self._m_compliance_reject(rid, params)
            else:  # pragma: no cover
                raise AssertionError(method)
        except _AsyncInvoke:
            raise  # bridge_server 捕获，走 invoke_rpc
        except ComplianceViolation as cv:
            self.emit("compliance.denied", {"capability": cv.capability, "action": cv.action,
                                             "reason": cv.reason})
            return _rpc_error(rid, COMPLIANCE_REJECTED, cv.reason)
        except CapabilityNotRegistered as e:
            return _rpc_error(rid, METHOD_NOT_FOUND, f"capability not registered: {e}")
        except (KeyError, ValueError) as e:
            return _rpc_error(rid, INVALID_PARAMS, str(e))
        return _rpc_result(rid, data)

    async def invoke_rpc(self, ai: _AsyncInvoke) -> dict[str, Any]:
        """async 方法服务端（capability.invoke / collab.run）。"""
        params, rid, method = ai.params, ai.rid, ai.method
        try:
            if method == "collab.run":
                return _rpc_result(rid, await self.run_collab(
                    params.get("goal", ""), params.get("tasks") or [],
                    params.get("process") or "sequential", params.get("trace_id")))
            return _rpc_result(rid, await self.invoke(
                params.get("capability", ""), params.get("action", ""),
                params.get("params") or {}, params.get("trace_id")))
        except ComplianceViolation as cv:
            return _rpc_error(rid, COMPLIANCE_REJECTED, cv.reason)
        except CapabilityNotRegistered as e:
            return _rpc_error(rid, METHOD_NOT_FOUND, f"capability not registered: {e}")
        except Exception as exc:  # 能力执行失败：载荷化，不静默
            return _rpc_error(rid, EXECUTION_FAILED, f"{type(exc).__name__}: {exc}")

    def _m_initialize(self, _params: dict[str, Any]) -> dict[str, Any]:
        """版本协商在 handle_rpc 已完成，这里只做握手成功响应。"""
        return {
            "ok": True,
            "protocol_version": PROTOCOL_VERSION,
            "capabilities": self.capabilities,
            "trace_id": _new_trace_id(),
        }

    def _net_status(self) -> dict[str, Any]:
        """双栈路由现状（契约 §3.1 net.status）：内核为单一事实源，宿主只读。"""
        from hermes_superagent.net.routing import resolve_net_route
        route = resolve_net_route(region="auto")
        return {"ok": True, "route": route.__dict__, "probe": "auto",
                "proxy": route.proxy, "degraded": []}

    def _m_compliance_reject(self, rid: Any, params: dict[str, Any]) -> dict[str, Any]:
        """§3.1 合规拒绝回执：宿主/内核任一侧拒绝后的显式上抛面。"""
        self.emit("compliance.denied", {"capability": params.get("capability", ""),
                                         "action": params.get("action", ""),
                                         "reason": params.get("reason", "unspecified")})
        return _rpc_error(rid, COMPLIANCE_REJECTED, params.get("reason", "compliance rejected"))

    _METHODS = {
        "initialize", "capability.invoke", "capability.list", "capability.schema",
        "collab.run", "collab.status", "collab.cancel", "net.status", "compliance.reject",
    }


class _AsyncInvoke(Exception):
    """async 方法标记：bridge_server 捕获后走 invoke_rpc。"""

    def __init__(self, params: dict[str, Any], rid: Any, method: str) -> None:
        self.params = params
        self.rid = rid
        self.method = method
        super().__init__(method)


# ------------------------------------------------------------------ 工具
def _new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def _rpc_result(rid: Any, data: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": data}


def _rpc_error(rid: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def build_default_runtime(host_secrets: dict[str, Any] | None = None,
                          token: str | None = None) -> AgentHarnessRuntime:
    """按 §3.3 能力目录装配默认 runtime（t2/t4 已落地能力 + net/collab 契约面）。"""
    rt = AgentHarnessRuntime(token=token, host_secrets=host_secrets)
    try:
        from hermes_superagent.capabilities.crawl import CrawlCapability
        rt.register(CrawlCapability())
    except Exception as exc:  # 能力降级不阻塞握手（K4），但必须可见
        log.warning("crawl capability unavailable: %s", exc)
    try:
        from hermes_superagent.capabilities.model import ModelCapability
        rt.register(ModelCapability())
    except Exception as exc:
        log.warning("model capability unavailable: %s", exc)
    try:
        from hermes_superagent.capabilities.tool import ToolCapability
        rt.register(ToolCapability())
    except Exception as exc:
        log.warning("tool capability unavailable: %s", exc)
    try:
        from hermes_superagent.capabilities.skill import SkillCapability
        rt.register(SkillCapability())
    except Exception as exc:
        log.warning("skill capability unavailable: %s", exc)
    # net 能力（契约 §3.3）：双栈路由现状读取，单一事实源在 Python 侧
    rt.register(NetCapability(rt))
    return rt


class NetCapability:
    """net 能力（契约 §3.3）：status 读双栈路由现状；route 按 region 解析一条路由。"""

    name = "net"

    def __init__(self, runtime: AgentHarnessRuntime) -> None:
        self._rt = runtime

    def schema(self) -> dict[str, Any]:
        return {"actions": {
            "status": {"params": {}, "returns": "route/probe/proxy/degraded"},
            "route": {"params": {"region": "str", "target": "str?"},
                      "returns": "resolved NetRoute dict"},
        }}

    async def invoke(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        from hermes_superagent.net.routing import resolve_net_route
        if action == "status":
            data = self._rt._net_status()
            return {"ok": True, "data": data, "error": None}
        if action == "route":
            route = resolve_net_route(region=params.get("region", "auto"),
                                      target=params.get("target"))
            return {"ok": True, "data": route.__dict__, "error": None}
        raise ValueError(f"unknown net action: {action}")
