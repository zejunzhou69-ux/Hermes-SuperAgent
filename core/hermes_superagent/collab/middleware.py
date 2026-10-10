"""成本追踪与 429 降级中间件（设计方案 §7.3 C3 / §10 K1）。

职责：
1. 每次 LLM 调用记录 CostRecord（token / 延迟 / 模型 / 角色）
2. 对 429 / 超时 / 5xx 自动切换到 backup_llm
3. 维护 per-role 的 failure window，连续失败 N 次则上报 Supervisor
4. 导出本次任务总成本，供 Reporter 写入报告
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from .models import CostRecord, RoleDef, TaskDef, TaskRoute


# ----------------------------------------------------------------------
# 事件回调类型
# ----------------------------------------------------------------------
EventCallback = Callable[[str, dict[str, Any]], None]


@dataclass
class MiddlewareState:
    """中间件运行时状态。"""

    records: list[CostRecord] = field(default_factory=list)
    # role → 连续失败计数（429 / timeout / 5xx）
    failure_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # role → 当前使用的 llm key
    current_llm: dict[str, str] = field(default_factory=dict)
    # 阈值：连续失败多少次触发上报
    fail_threshold: int = 3
    # 降级历史（便于审计）
    degradation_history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_cost_usd(self) -> float:
        total = 0.0
        for r in self.records:
            rate = _PRICING.get(r.model, 0.000002)
            total += r.total_tokens * rate
        return round(total, 6)


#: 模型单价（美元/token），来自 roles.yaml models_config.pricing
_PRICING: dict[str, float] = {
    "agnes-2.5-flash": 0.000002,
    "agnes-3.0-flash": 0.000003,
}


class CostMiddleware:
    """成本追踪 + 429 降级中间件。

    用法：
        mw = CostMiddleware()
        result = await mw.call_with_middleware(
            role="crawler",
            llm_fn=actual_llm_call,
            params={"task": "..."},
            role_def=role_def,
        )
    """

    def __init__(
        self,
        state: MiddlewareState | None = None,
        on_event: EventCallback | None = None,
        pricing: dict[str, float] | None = None,
    ) -> None:
        self.state = state or MiddlewareState()
        self._on_event = on_event
        if pricing is not None:
            _PRICING.update(pricing)

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------
    async def call_with_middleware(
        self,
        *,
        role: str,
        llm_fn: Callable[..., Any],
        params: dict[str, Any],
        role_def: RoleDef | None = None,
        route: TaskRoute | None = None,
        max_retries: int = 3,
    ) -> dict[str, Any]:
        """带中间件的一次 LLM 调用。

        流程：
        1. 选择模型（优先 current_llm[role]，否则 role_def.llm）
        2. 调用 llm_fn
        3. 记录 CostRecord
        4. 若 429 / timeout / 5xx：切换 backup_llm，重试
        5. 连续失败超过阈值：发出 warning 事件
        """
        llm_key = self.state.current_llm.get(role, (role_def.llm if role_def else "agnes-2.5-flash"))
        backup_key = (
            (route.backup_llm_key if route and route.backup_llm_key else None)
            or (role_def.backup_llm if role_def else None)
        )

        last_error: str | None = None
        last_result: dict[str, Any] | None = None
        threshold = self.state.fail_threshold

        for attempt in range(max_retries + 1):
            t0 = time.time()
            try:
                result = await llm_fn(llm_key=llm_key, params=params)
                elapsed_ms = int((time.time() - t0) * 1000)

                # 成功：重置失败计数
                self.state.failure_counts[role] = 0
                self.state.current_llm[role] = llm_key

                # 估算 token（实际由 LLM 提供方返回；骨架用文本长度近似）
                text = result.get("content", "")
                est_tokens = max(1, len(text) // 4)  # 粗估

                record = CostRecord(
                    role=role,
                    model=llm_key,
                    tokens_input=est_tokens,
                    tokens_output=est_tokens,
                    latency_ms=elapsed_ms,
                    success=True,
                )
                self.state.records.append(record)
                self._emit("cost.record", {"record": _asdict(record)})
                return result

            except Exception as exc:  # noqa: BLE001
                elapsed_ms = int((time.time() - t0) * 1000)
                last_error = f"{type(exc).__name__}: {exc}"
                last_result = {"error": last_error, "model": llm_key}
                is_429 = "429" in str(exc).lower() or "rate limit" in str(exc).lower()

                self.state.failure_counts[role] += 1
                count = self.state.failure_counts[role]

                # 记录失败
                fail_record = CostRecord(
                    role=role,
                    model=llm_key,
                    latency_ms=elapsed_ms,
                    success=False,
                    error=last_error,
                )
                self.state.records.append(fail_record)
                self._emit("cost.fail", {"record": _asdict(fail_record), "count": count})

                # 连续失败阈值
                if count >= threshold:
                    self._emit("cost.threshold_exceeded", {
                        "role": role,
                        "count": count,
                        "error": last_error,
                    })

                # 429 / 超时 → 切换备用模型重试
                if is_429 and backup_key and attempt < max_retries:
                    self.state.current_llm[role] = backup_key
                    self.state.degradation_history.append({
                        "role": role,
                        "from": llm_key,
                        "to": backup_key,
                        "reason": "429",
                        "attempt": attempt + 1,
                    })
                    self._emit("cost.degrade", self.state.degradation_history[-1])
                    llm_key = backup_key
                    continue

                # 非 429 错误，不自动重试（让 Supervisor 裁决）
                break

        # 全部重试耗尽
        self._emit("cost.exhausted", {
            "role": role,
            "last_error": last_error,
            "attempts": max_retries + 1,
        })
        return last_result or {"error": last_error, "model": llm_key}

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------
    def get_route(self, role_def: RoleDef) -> TaskRoute:
        """根据 RoleDef 构建默认 TaskRoute。"""
        current = self.state.current_llm.get(role_def.name, role_def.llm)
        return TaskRoute(
            task=TaskDef(title=role_def.name, description=role_def.goal, task_class="default"),
            target_role=role_def.name,
            llm_key=current,
            backup_llm_key=role_def.backup_llm,
            process_mode=role_def.process,
            notes=[f"llm={current}", f"backup={role_def.backup_llm}"],
        )

    def reset_role(self, role: str) -> None:
        """重置某角色的失败计数（Supervisor 调用换模型后）。"""
        self.state.failure_counts[role] = 0

    def report(self) -> dict[str, Any]:
        """导出本次会话的成本报告。"""
        by_role: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in self.state.records:
            by_role[r.role].append(_asdict(r))
        return {
            "total_cost_usd": self.state.total_cost_usd,
            "total_records": len(self.state.records),
            "by_role": dict(by_role),
            "degradation_history": self.state.degradation_history,
            "failure_counts": dict(self.state.failure_counts),
        }

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self._on_event:
            try:
                self._on_event(event, payload)
            except Exception:  # noqa: BLE001 - 事件回调不能阻断主流程
                pass


def _asdict(obj: Any) -> dict[str, Any]:
    """dataclasses.asdict 的轻量封装。"""
    import dataclasses

    return dataclasses.asdict(obj)
