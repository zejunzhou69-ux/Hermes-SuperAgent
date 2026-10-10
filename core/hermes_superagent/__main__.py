"""CLI 入口：python -m hermes_superagent ...

用法：
  python -m hermes_superagent crawl <url> [--level L1|L2|L3|L4] [--headful] [--region auto|cn|intl]
  python -m hermes_superagent mcp                 # 以 stdio MCP server 运行
  python -m hermes_superagent thumb --port 8766   # 启动拇指浏览器帧流服务
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import warnings


def _silence_asyncio_cleanup_noise() -> None:
    """Windows 下浏览器/网络句柄在进程退出时的 'Exception ignored' 属已知噪音，
    不影响结果；静默以保持 CLI 输出干净（结果 JSON 为唯一有效载荷）。"""
    warnings.filterwarnings("ignore", message="unclosed transport")
    warnings.filterwarnings("ignore", category=ResourceWarning)


def _crawl_cmd(args: argparse.Namespace) -> int:
    from hermes_superagent.capabilities.crawl import CrawlCapability

    async def run() -> int:
        cap = CrawlCapability()
        try:
            result = await cap.invoke(
                "fetch",
                {
                    "url": args.url,
                    "level": args.level,
                    "headless": not args.headful,
                    "region": args.region,
                    "timeout_ms": args.timeout_ms,
                },
            )
        finally:
            await cap.aclose()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    # Windows GBK 控制台兜底：强制 UTF-8 输出，避免含中文的 JSON 打印崩溃
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    return asyncio.run(run())


def _mcp_cmd(_args: argparse.Namespace) -> int:
    from hermes_superagent.capabilities.browser_mcp import run_mcp_server

    run_mcp_server()
    return 0


def _thumb_cmd(args: argparse.Namespace) -> int:
    from hermes_superagent.thumb.server import run_thumb_server

    asyncio.run(run_thumb_server(port=args.port))
    return 0


def _bridge_cmd(args: argparse.Namespace) -> int:
    """t5：启动 Bridge JSON-RPC + SSE 服务端（契约 §2/§3）。"""
    import os

    from hermes_superagent.agent_harness import build_default_runtime
    from hermes_superagent.bridge_server import BridgeServer
    from hermes_superagent.capabilities.secrets import SecretsManager

    # 把密钥文件 / 宿主下发的密钥与端点导出到环境变量（provider 直接读 env）
    SecretsManager().export_env_to_process()

    token = os.environ.get("HERMES_SUPERAGENT_TOKEN") or args.token
    runtime = build_default_runtime(token=token)
    server = BridgeServer(runtime, port=args.port)

    async def run() -> int:
        await server.start()
        print(json.dumps({"ok": True, "endpoint": f"http://{args.host}:{args.port}",
                         "capabilities": runtime.capabilities,
                         "note": "token is NOT logged (K7)"}, ensure_ascii=False))
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            await server.stop()
        return 0

    return asyncio.run(run())


def _sandbox_cmd(args: argparse.Namespace) -> int:
    """M4：打印分层沙箱策略 + 安装通道体检（design §9B）。"""
    from pathlib import Path

    from hermes_superagent.sandbox import build_sandbox

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    ws = Path(args.workspace).expanduser().resolve() if args.workspace else Path.cwd()
    sb = build_sandbox(ws)

    if args.json:
        print(json.dumps(sb.to_dict(), ensure_ascii=False, indent=2))
        return 0

    print(sb.policy.summary())
    print()
    print(sb.install.summary())
    print()
    print("外接 CDP：", "已授权（有监督）" if sb.browser.cdp_authorized else "默认禁用（需显式 opt-in）")
    print("浏览器 profile：", sb.browser.profile_dir())
    return 0


def _panel_cmd(args: argparse.Namespace) -> int:
    """桌面面板：启动服务并弹出面板（对标 DSH 面板体感）。"""
    from hermes_superagent.panel_launcher import run_panel

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    try:
        asyncio.run(
            run_panel(
                port=args.port,
                do_open=not args.no_open,
                prefer_native=not getattr(args, "browser", False),
            )
        )
    except KeyboardInterrupt:
        print("\n面板已停止")
    return 0


def _skill_cmd(args: argparse.Namespace) -> int:
    """技能层：列出 / 检索 / 固化新技能（设计 §4.5 自魔改能力）。"""
    from hermes_superagent.skills import SkillRegistry

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    reg = SkillRegistry.create()

    if args.scaffold:
        res = reg.scaffold(
            args.scaffold,
            args.description or "",
            triggers=tuple(args.trigger or ()),
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    if args.match:
        hits = reg.match(args.match)
        print(f"匹配 “{args.match}”：{len(hits)} 个")
        for s in hits:
            print(f"  - {s.summary()}")
        if not hits:
            print("  （无匹配技能）")
        return 0

    print(f"技能根目录: {reg.root}")
    print(f"共 {len(reg.skills)} 个技能：")
    for s in reg.list():
        print(f"  - {s.summary()}")
        for w in s.warnings:
            print(f"      ⚠ {w}")
    for e in reg.errors:
        print(f"  ! 加载失败 {e['dir']}: {e['error']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    _silence_asyncio_cleanup_noise()
    # Windows GBK 控制台兜底：尽早重配 stdout，保证 --help 的中文也不乱码
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(prog="hermes_superagent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_crawl = sub.add_parser("crawl", help="抓取一个 URL（L1–L4 分级调度）")
    p_crawl.add_argument("url")
    p_crawl.add_argument("--level", default="L1", choices=["L1", "L2", "L3", "L4"])
    p_crawl.add_argument("--headful", action="store_true", help="有头模式（人可见）")
    p_crawl.add_argument("--region", default="auto", choices=["auto", "cn", "intl"])
    p_crawl.add_argument("--timeout-ms", type=int, default=15000)
    p_crawl.set_defaults(func=_crawl_cmd)

    p_mcp = sub.add_parser("mcp", help="以 stdio 运行 MCP server")
    p_mcp.set_defaults(func=_mcp_cmd)

    p_thumb = sub.add_parser("thumb", help="启动拇指浏览器帧流服务")
    p_thumb.add_argument("--port", type=int, default=8766)
    p_thumb.set_defaults(func=_thumb_cmd)

    p_panel = sub.add_parser("panel", help="启动并弹出双端工作台（侧栏+对话+内嵌浏览器，对标 DSH 桌面端）")
    p_panel.add_argument("--port", type=int, default=8766)
    p_panel.add_argument("--no-open", action="store_true", help="只起服务，不自动弹面板")
    p_panel.add_argument("--browser", action="store_true",
                         help="强制用浏览器标签页（默认优先 pywebview 原生窗口，失败自动回落浏览器）")
    p_panel.add_argument("--native", action="store_true",
                         help="强制用原生桌面窗口（需 pip install pywebview）")
    p_panel.set_defaults(func=_panel_cmd)

    p_bridge = sub.add_parser("bridge", help="启动 Bridge JSON-RPC + SSE 服务端（t5 契约）")
    p_bridge.add_argument("--port", type=int, default=8765)
    p_bridge.add_argument("--host", default="127.0.0.1")
    p_bridge.add_argument("--token", default=None, help="缺省读 HERMES_SUPERAGENT_TOKEN")
    p_bridge.set_defaults(func=_bridge_cmd)

    p_sandbox = sub.add_parser("sandbox", help="打印分层沙箱策略 + 安装通道体检（M4）")
    p_sandbox.add_argument("--workspace", default=None, help="工作区根（缺省为当前目录）")
    p_sandbox.add_argument("--json", action="store_true", help="以 JSON 输出完整策略")
    p_sandbox.set_defaults(func=_sandbox_cmd)

    p_skill = sub.add_parser("skill", help="技能层：列出 / 检索 / 固化新技能（自魔改能力）")
    p_skill.add_argument("--match", default=None, help="按查询匹配技能")
    p_skill.add_argument("--scaffold", default=None, metavar="NAME", help="固化出新技能骨架")
    p_skill.add_argument("--description", default=None, help="配合 --scaffold 的技能描述")
    p_skill.add_argument("--trigger", action="append", help="配合 --scaffold 的触发词（可多次）")
    p_skill.set_defaults(func=_skill_cmd)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
