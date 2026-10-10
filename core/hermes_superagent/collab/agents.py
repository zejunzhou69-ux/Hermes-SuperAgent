"""五角色实体（设计方案 §4.4 / 协作骨架 §3）。

- 优先用 CrewAI 的 `Agent` 组装（若已安装，可选依赖组 [collab]）；
- 缺 CrewAI / LLM key 时用纯 Python `LiteAgent` 兜底，保证"路由+拓扑"可被无 LLM 验证。
- 角色一律经 Supervisor 路由（协作骨架 §4 不变式）：任何"取数据"必经 Crawl Capability，
  任何产物必经 Verifier 才能到 Reporter。

角色名：supervisor（主管）/ crawler（爬虫手）/ coder（编码手）/ verifier（验证手）/ reporter（汇报手）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .models import RoleDef, TaskDef


class RoleBackend(Protocol):
    """角色执行后端：给定任务上下文，产出一个结果。"""

    name: str

    async def run(self, task: TaskDef, context: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class AgentResult:
    """一次角色执行的标准化结果（供 Supervisor 裁决 + Reporter 汇总）。"""
    role: str
    task_id: str
    ok: bool
    payload: dict[str, Any] = field(default_factory=dict)
    trace_id: str = ""
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "task_id": self.task_id,
            "ok": self.ok,
            "payload": self.payload,
            "trace_id": self.trace_id,
            "error": self.error,
        }


class LiteAgent:
    """纯 Python 规则角色（无 LLM / 无 CrewAI 时的兜底实现）。

    每个子类实现 `decide()` 返回 payload；`run()` 负责套上 AgentResult 外壳。
    这样拓扑与路由不变式在任何环境都可被测试覆盖。
    """

    name: str = "lite"

    def __init__(self, role_def: RoleDef | None = None) -> None:
        self.role_def = role_def
        # 数据获取唯一出口：Crawl Capability（协作骨架 §4 不变式 1）
        self._crawl: Any | None = None

    # -- 工具注入（测试 / 宿主可替换真实 CrawlCapability）--
    def set_crawl_capability(self, cap: Any) -> None:
        self._crawl = cap

    async def _crawl_fetch(self, url: str, **params: Any) -> dict[str, Any]:
        """角色不得自带裸 httpx，统一走 Crawl Capability（不变式 1 可测点）。"""
        if self._crawl is None:
            return {"ok": False, "error": "crawl capability not injected"}
        return await self._crawl.invoke("fetch", {"url": url, **params})

    # -- 规则钩子（子类覆盖）--
    async def decide(self, task: TaskDef, context: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "note": "lite default", "echo": task.task_class}

    async def run(self, task: TaskDef, context: dict[str, Any]) -> AgentResult:
        import uuid

        try:
            payload = await self.decide(task, context)
            ok = bool(payload.get("ok", True))
            return AgentResult(
                role=self.name,
                task_id=task.id,
                ok=ok,
                payload=payload,
                trace_id=uuid.uuid4().hex[:12],
            )
        except Exception as exc:  # noqa: BLE001 - 失败必须可见，绝不静默
            return AgentResult(
                role=self.name,
                task_id=task.id,
                ok=False,
                payload={},
                trace_id=uuid.uuid4().hex[:12],
                error=f"{type(exc).__name__}: {exc}",
            )


# ----------------------------------------------------------------------
# 五个具体角色
# ----------------------------------------------------------------------
class Supervisor(LiteAgent):
    """主管：拆解 + 分派 + 裁决。

    规则兜底：把用户目标拆成 crawl→code→verify→report 的标准链，
    并给出每个子任务的目标角色。层级模式下可动态调整（由 router 决定）。
    """

    name = "supervisor"

    async def decide(self, task: TaskDef, context: dict[str, Any]) -> dict[str, Any]:
        subtasks = [
            {"task_class": "crawl", "role": "crawler", "desc": "采集目标数据"},
            {"task_class": "code", "role": "coder", "desc": "落地解析/魔改"},
            {"task_class": "verify", "role": "verifier", "desc": "抽样复现校验"},
            {"task_class": "report", "role": "reporter", "desc": "汇总交付"},
        ]
        return {
            "ok": True,
            "subtasks": subtasks,
            "process": "hierarchical" if context.get("process") == "hierarchical" else "sequential",
            "decisions": ["拆解为标准四段链"],
        }


class Crawler(LiteAgent):
    """爬虫手：L1→L4 分级抓取，唯一数据出口 = Crawl Capability。"""

    name = "crawler"

    async def decide(self, task: TaskDef, context: dict[str, Any]) -> dict[str, Any]:
        url = context.get("url") or task.context
        if not url:
            return {"ok": False, "error": "missing url"}
        res = await self._crawl_fetch(url)
        ok = res.get("ok", False)
        payload: dict[str, Any] = {
            "ok": ok,
            "level": res.get("level"),
            "content": (res.get("data") or {}).get("content", "") if ok else "",
            "trace_id": res.get("trace_id", ""),
        }
        if not ok:
            payload["error"] = res.get("error", "crawl failed")
        return payload


class Coder(LiteAgent):
    """编码手：把 Crawler 产物落地为可运行代码（规则兜底=生成伪代码模板）。"""

    name = "coder"

    async def decide(self, task: TaskDef, context: dict[str, Any]) -> dict[str, Any]:
        src = context.get("crawler_content") or task.context
        code = (
            "# 由编码手生成的解析骨架\n"
            f"# 输入: {src[:80]}\n"
            "def parse(html: str) -> dict:\n"
            "    raise NotImplementedError('由编码手按站点定制')\n"
        )
        return {"ok": True, "code": code, "note": "skeleton generated (rule fallback)"}


class Verifier(LiteAgent):
    """验证手：对编码手产物做"存在性+可解析"校验；FAIL 必须回传证据。"""

    name = "verifier"

    async def decide(self, task: TaskDef, context: dict[str, Any]) -> dict[str, Any]:
        code = context.get("coder_code") or ""
        passed = bool(code) and "def parse" in code
        return {
            "ok": passed,
            "verdict": "pass" if passed else "fail",
            "evidence": ["code contains 'def parse'" if passed else "code missing parse()"],
        }


class Reporter(LiteAgent):
    """汇报手：汇总全链 + 成本/降级留证，产出人类可读报告。"""

    name = "reporter"

    async def decide(self, task: TaskDef, context: dict[str, Any]) -> dict[str, Any]:
        lines = [
            "# 任务报告",
            f"- 目标: {task.title}",
            f"- 爬取级别: {context.get('crawler_level', '?')}",
            f"- 校验结论: {context.get('verifier_verdict', '?')}",
        ]
        # 汇总成本/降级（若注入了中间件）
        mw = context.get("middleware_report")
        if isinstance(mw, dict):
            lines.append(f"- 总成本(USD): {mw.get('total_cost_usd', 0)}")
            lines.append(f"- 降级次数: {len(mw.get('degradation_history', []))}")
        return {
            "ok": True,
            "report": "\n".join(lines),
            "skills": ["crawl-parse-skeleton"],  # 可固化技能（自进化闭环占位）
        }


# ----------------------------------------------------------------------
# 工厂
# ----------------------------------------------------------------------
def build_agents(
    roles: dict[str, RoleDef] | None = None,
) -> dict[str, LiteAgent]:
    """按角色名实例化五个角色（CrewAI 可用时可换为真实 Agent 封装，当前为规则兜底）。

    roles 缺省从 roles.yaml 加载；返回 name → LiteAgent 映射。
    """
    from .router import load_roles_yaml

    if roles is None:
        roles = load_roles_yaml()
    return {
        "supervisor": Supervisor(roles.get("supervisor")),
        "crawler": Crawler(roles.get("crawler")),
        "coder": Coder(roles.get("coder")),
        "verifier": Verifier(roles.get("verifier")),
        "reporter": Reporter(roles.get("reporter")),
    }
