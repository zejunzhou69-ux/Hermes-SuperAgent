"""`skill` Capability（设计方案 §4.5 / §6.1）。

把技能层挂进 Capability 协议，从而：
- 经 MCP / Bridge 对宿主与 agent 可见（单一出口）
- 与其它能力一样受合规红线与沙箱约束

actions：
  list     列出现有技能
  get      取单个技能（含正文步骤）
  match    按查询匹配技能（Supervisor 拆解时的检索入口）
  reload   热加载（有变更才重读）
  scaffold 从一次有效解法固化出新技能（自进化落点）
"""

from __future__ import annotations

from typing import Any

from hermes_superagent.skills import SkillRegistry


class SkillCapability:
    name = "skill"

    def __init__(self, registry: SkillRegistry | None = None) -> None:
        self.registry = registry or SkillRegistry.create()

    # ------------------------------------------------------------------
    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "actions": {
                "list": {"params": {}},
                "get": {"params": {"name": "str"}},
                "match": {"params": {"query": "str", "limit": "int?"}},
                "reload": {"params": {"force": "bool?"}},
                "scaffold": {
                    "params": {
                        "name": "str",
                        "description": "str",
                        "triggers": "list[str]?",
                        "tools": "list[str]?",
                        "body": "str?",
                    }
                },
            },
        }

    async def invoke(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action == "list":
            return {"ok": True, "data": {
                "root": str(self.registry.root),
                "count": len(self.registry.skills),
                "skills": [s.to_dict() for s in self.registry.list()],
                "errors": self.registry.errors,
            }}
        if action == "get":
            skill = self.registry.get(params["name"])
            if skill is None:
                return {"ok": False, "error": f"技能不存在: {params['name']}"}
            data = skill.to_dict()
            data["body"] = skill.body
            return {"ok": True, "data": data}
        if action == "match":
            hits = self.registry.match(params.get("query", ""), int(params.get("limit", 5)))
            return {"ok": True, "data": {"query": params.get("query", ""),
                                          "hits": [s.to_dict() for s in hits]}}
        if action == "reload":
            return {"ok": True, "data": self.registry.reload(force=bool(params.get("force")))}
        if action == "scaffold":
            res = self.registry.scaffold(
                params["name"],
                params["description"],
                triggers=tuple(params.get("triggers") or ()),
                tools=tuple(params.get("tools") or ()),
                body=params.get("body") or "",
            )
            return res if res.get("ok") else {"ok": False, "error": res.get("error"),
                                              "blocked_at": res.get("blocked_at")}
        raise ValueError(f"unknown skill action: {action}")
