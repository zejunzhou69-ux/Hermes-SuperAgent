"""协作层 CLI：python -m hermes_superagent.collab demo "<目标>" [--url ...] [--hierarchical]

无 LLM 也能跑：默认用 LiteAgent 规则后端，把五角色链跑通并打印每步结果。
配好 Crawl Capability 后（inject crawler 的 _crawl），crawler 走真实 L1 抓取。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from hermes_superagent.collab import create_crew_pipeline
from hermes_superagent.collab.models import ProcessMode, TaskDef


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes_superagent.collab")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_demo = sub.add_parser("demo", help="跑通五角色协作链（无 LLM 规则兜底）")
    p_demo.add_argument("goal")
    p_demo.add_argument("--url", default="", help="可选：给爬虫手一个真实 URL（走 L1 抓取）")
    p_demo.add_argument("--hierarchical", action="store_true", help="层级流（Supervisor 在环）")
    p_demo.add_argument("--json", action="store_true", help="以 JSON 输出（默认人类可读）")
    p_demo.set_defaults(func=_demo)

    args = parser.parse_args(argv)
    return args.func(args)


def _demo(args: argparse.Namespace) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    process = ProcessMode.HIERARCHICAL if args.hierarchical else ProcessMode.SEQUENTIAL

    # 若给了 URL，注入真实 CrawlCapability（可选，失败不影响拓扑演示）
    context: dict = {"process": process.value}
    if args.url:
        context["url"] = args.url
        try:
            from hermes_superagent.capabilities.crawl import CrawlCapability

            cap = CrawlCapability()
            runner = create_crew_pipeline(args.goal, context=context, process=process)
            # 注入到 crawler 角色
            from hermes_superagent.collab.agents import Crawler

            crawler = Crawler()
            crawler.set_crawl_capability(cap)
            runner.inject("crawler", crawler)
        except Exception as exc:  # noqa: BLE001
            context["_crawl_warning"] = f"crawl capability unavailable: {exc}"
    else:
        runner = create_crew_pipeline(args.goal, context=context, process=process)

    async def run() -> int:
        result = await runner.run(TaskDef(title=args.goal, description=args.goal))
        if args.json:
            print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, default=str))
        else:
            print(f"目标: {args.goal}")
            print(f"流程: {result.process}   结论: {result.verdict}")
            for r in result.results:
                status = "OK " if r.ok else "FAIL"
                extra = r.payload.get("level") or r.payload.get("verdict") or ""
                print(f"  [{status}] {r.role:<10} trace={r.trace_id} {extra}")
                if r.error:
                    print(f"         error: {r.error}")
        return 0 if result.verdict == "pass" else 1

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
