"""数据模型（设计方案 §4.4 / §7.3）。"""
from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ProcessMode(str, Enum):
    SEQUENTIAL = "sequential"
    HIERARCHICAL = "hierarchical"


@dataclass
class RoleDef:
    name: str
    prompt_template: str
    goal: str
    tools: list[str]
    llm: str
    backup_llm: str
    max_iter: int
    process: ProcessMode

    def prompt(self, **kwargs: Any) -> str:
        tmpl = self.prompt_template
        for k, v in kwargs.items():
            tmpl = tmpl.replace("{{" + k + "}}", str(v))
        return tmpl


@dataclass
class TaskDef:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    title: str = ""
    description: str = ""
    task_class: str = "default"
    timeout_seconds: int = 1800
    max_retries: int = 3
    context: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        return f"[{self.task_class}] {self.title}: {self.description[:100]}"


@dataclass
class CostRecord:
    role: str
    model: str
    tokens_input: int = 0
    tokens_output: int = 0
    latency_ms: int = 0
    success: bool = True
    error: str | None = None
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts_ms: int = field(default_factory=lambda: int(__import__("time").time() * 1000))

    @property
    def total_tokens(self) -> int:
        return self.tokens_input + self.tokens_output


@dataclass
class TaskRoute:
    task: TaskDef
    target_role: str
    llm_key: str
    backup_llm_key: str | None = None
    process_mode: ProcessMode = ProcessMode.SEQUENTIAL
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return f"{self.task.id} -> {self.target_role} ({self.llm_key})"
