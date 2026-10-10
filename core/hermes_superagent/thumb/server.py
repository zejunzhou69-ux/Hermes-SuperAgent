"""双端工作台服务（设计方案 §4.2 / §5 / §9B.4 读写分离）。

单文件 aiohttp 服务（仅 127.0.0.1），职责：
  GET /panel.html           双端工作台面板（左对话+设置 / 右大浏览器）
  GET /thumb/frames?...    SSE 帧流（每帧 JSON：b64 图 + url + ts）
  GET /thumb/status        实例池状态（懒启动未就绪返回 200 + booting）
  POST /thumb/goto         触发地址栏跳转（面板按钮 → 同一实例）
  GET /secrets             读密钥/端点状态 + 密钥文件内容（设置栏）
  POST /secrets            写密钥/端点到密钥文件（设置栏保存）
  POST /collab            跑完整智能体管道（路由→五角色→浏览器动作→结果）

用法：
  python -m hermes_superagent panel --port 8766
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sys
import time
from typing import Any

from hermes_superagent.capabilities.browser import DEFAULT_VIEWPORT, BrowserCapability
from hermes_superagent.thumb.panel import PANEL_HTML

#: 默认面板端口（与 §6.4 pythonEndpoint 8765 区分）
DEFAULT_PORT = 8766

#: 大屏桌面视口（16:9，升级自原 360×640 手机竖屏）。原先拇指浏览器写死
#: THUMB_VIEWPORT 导致右侧画面永远是一小块"手机"；现默认用 16:9 桌面视口，
#: 帧宽随视口，画布按 contain 撑满右侧舞台。§13.2 视口升级。
DESKTOP_VIEWPORT = (1440, 900)
#: 帧流最大宽度：与视口同宽，保证大屏不压缩（旧版写死 360 是"手机感"根因）。
DEFAULT_FRAME_MAX_WIDTH = 1440


def create_app(
    browser: BrowserCapability | None = None,
    viewport: tuple[int, int] | None = None,
    frame_max_width: int | None = None,
):
    """构建 aiohttp App（便于测试注入 BrowserCapability 替身）。

    viewport：浏览器实例渲染视口，默认大屏 1440×900（不再是 360×640 手机竖屏）。
    frame_max_width：帧流缩放上限，默认随视口宽；None=自动取视口宽。
    """
    from aiohttp import web

    viewport = viewport or DESKTOP_VIEWPORT
    cap = browser or BrowserCapability(viewport=viewport)
    frame_max_width = frame_max_width or viewport[0]

    async def on_startup(app: web.Application) -> None:
        # 灌密钥：把 secrets.json 的 agnes 凭证导出到进程 env，
        # 让 agnes-flash provider（cheap 档主力）真实可达（对齐 crew._make_llm 的解析逻辑）。
        try:
            from hermes_superagent.capabilities.secrets import SecretsManager
            injected = SecretsManager().export_env_to_process()
            if injected:
                log_injected = ", ".join(sorted(injected))
                print(f"[panel] injected secrets env: {log_injected}")
        except Exception as exc:  # noqa: BLE001 - 密钥灌入失败不阻塞服务，仅告警
            print(f"[panel] secrets inject failed: {exc}")

        # 默认实例懒启动（后台 task），不阻塞事件循环——
        # 硬启动 Chromium 在受限环境（后台 job / CI）会拖死事件循环导致所有路由 503。
        app["default_instance"] = None
        app["current_url"] = "about:blank"
        app["startup_error"] = None

        async def _boot() -> None:
            # 启动异常必须捕获并记入 startup_error（§9B.4 显式告警不静默）——
            # 旧版未捕获时 default_instance 永远 None，503 空 body 无从查因。
            # 加 45s 超时守卫：受限环境（DSH 沙箱/CI）Chromium 子进程 EPERM 时，
            # 不拖死事件循环，API 路由（对话/生图/识图/视频）照常可用。
            try:
                res = await asyncio.wait_for(
                    cap.invoke("open", {"headless": True, "url": "about:blank"}),
                    timeout=45.0,
                )
                app["default_instance"] = res.get("data", {}).get("instance_id")
                app["current_url"] = res.get("data", {}).get("url") or "about:blank"
                if not res.get("ok"):
                    app["startup_error"] = res.get("error")
            except asyncio.TimeoutError:
                app["startup_error"] = "browser boot timeout (>45s, API routes still usable)"
                print(f"[panel] {app['startup_error']}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                app["startup_error"] = f"browser boot failed: {type(exc).__name__}: {exc}"
                print(f"[panel] {app['startup_error']}", file=sys.stderr)

        # 必须持有 task 引用（放入 app 状态），否则 ensure_future 的任务
        # 可能被 GC 掉导致 _boot 静默消失 → default_instance 永远 None（§9B.4）
        app["_boot_task"] = asyncio.ensure_future(_boot())
        # task 加 done-callback 兜底：异常被显式吞掉，绝不冒泡到事件循环
        app["_boot_task"].add_done_callback(lambda t: (
            t.exception() and print(f"[panel] boot task exception: {t.exception()}", file=sys.stderr)
        ) if not t.cancelled() else None)

    async def on_cleanup(app: web.Application) -> None:
        await cap.aclose()

    async def panel(_request: web.Request) -> web.Response:
        return web.Response(text=PANEL_HTML, content_type="text/html")

    async def _wait_instance(app: web.Application, timeout_s: float = 30.0) -> str | None:
        """等待默认实例就绪（懒启动的 _boot 完成），返回 instance_id。"""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            iid = app.get("default_instance")
            if iid:
                return iid
            if app.get("startup_error"):
                return None
            await asyncio.sleep(0.2)
        return app.get("default_instance")

    async def frames(request: web.Request) -> web.StreamResponse:
        fps = min(float(request.query.get("fps", "4")), 15)
        fmt = request.query.get("format", "jpeg")
        # 帧宽允许前端按视口覆盖；缺省用大屏上限（不再写死 360）
        req_max_width = request.query.get("max_width")
        max_width = int(req_max_width) if req_max_width else frame_max_width
        # JPEG 质量（清晰度）：前端可传 quality=95 提清晰度，默认 90（Pillow 默认 75 发糊）
        req_quality = request.query.get("quality")
        quality = int(req_quality) if req_quality else 90
        quality = max(1, min(100, quality))
        # 等默认实例懒启动就绪（首次请求时 _boot 可能还没跑完）
        instance = request.query.get("instance") or await _wait_instance(request.app)
        if not instance:
            return web.json_response(
                {"ok": False, "error": request.app.get("startup_error") or "browser instance unavailable"},
                status=503,
            )

        resp = web.StreamResponse(status=200, reason="Streaming")
        resp.content_type = "text/event-stream"
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["Connection"] = "keep-alive"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        await resp.prepare(request)

        def sse(payload: dict[str, Any]) -> bytes:
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

        try:
            await resp.write(sse({"type": "hello", "instance": instance, "fps": fps,
                                   "max_width": max_width, "quality": quality}))
            period = 1.0 / fps
            seq = 0
            while True:
                res = await cap.invoke(
                    "frame", {"instance_id": instance, "format": fmt,
                               "max_width": max_width, "quality": quality}
                )
                if res.get("ok"):
                    data = res["data"]
                    seq += 1
                    page_url = request.app.get("current_url") or "about:blank"
                    await resp.write(
                        sse(
                            {
                                "type": "frame",
                                "format": data["format"],
                                "ts": data["ts"],
                                "seq": seq,
                                "url": page_url,
                                "title": page_url,
                                "width": data["width"],
                                "height": data["height"],
                                "quality": data.get("quality", quality),
                                "stopped": bool(request.app.get("paused")),
                                "image_base64": data["data_base64"],
                            }
                        )
                    )
                else:
                    await resp.write(sse({"type": "error", "error": res.get("error")}))
                    break
                await asyncio.sleep(period)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    async def status(_request: web.Request) -> web.Response:
        """实例池状态。实例懒启动未就绪时返回 200 + booting（不 503），
        只有真故障（startup_error 非空）才 503 + reason（§9B.4 显式告警不静默）。"""
        if not _request.app.get("default_instance") and not _request.app.get("startup_error"):
            return web.json_response(
                {"ok": True, "booting": True,
                 "data": {"instances": [], "max_instances": cap._max_instances,
                          "note": "browser instance starting…"},
                 "trace_id": ""}
            )
        res = await cap.invoke("list_pool", {})
        return web.json_response(res)

    async def mouse(request: web.Request) -> web.Response:
        """画面交互：滚轮/点击 → 内嵌 Chromium（前端把画面坐标换算成视口坐标）。"""
        body = await _read_json(request)
        instance = body.get("instance") or await _wait_instance(request.app, 30.0)
        if not instance:
            return web.json_response(
                {"ok": False, "error": request.app.get("startup_error") or "browser instance unavailable"},
                status=503,
            )
        res = await cap.invoke("mouse", {
            "instance_id": instance,
            "op": body.get("op", "wheel"),
            "x": body.get("x", 0),
            "y": body.get("y", 0),
            "delta_y": body.get("delta_y", 0),
        })
        return web.json_response(res)

    async def _read_json(request: web.Request) -> dict:
        """容错读 JSON body（空 body / 损坏 → {} 或 400，不 500 空 body）。
        Windows 上 curl -d 可能带 BOM/编码问题，去 BOM 再解析。"""
        raw = await request.text()
        if not raw or not raw.strip():
            return {}
        raw = raw.lstrip("\ufeff")
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {"_raw": data}
        except json.JSONDecodeError:
            return {}

    async def goto(request: web.Request) -> web.Response:
        body = await _read_json(request)
        instance = body.get("instance") or await _wait_instance(request.app, 30.0)
        if not instance:
            return web.json_response(
                {"ok": False, "error": request.app.get("startup_error") or "browser instance unavailable"},
                status=503,
            )
        res = await cap.invoke("goto", {"instance_id": instance, "url": body.get("url", "")})
        if res.get("ok"):
            request.app["current_url"] = res.get("data", {}).get("url") or body.get("url", "")
        return web.json_response(res)

    # ---- 设置栏：密钥 / 端点 读写（§8 敏感数据外泄，secrets.json 0600）----
    from hermes_superagent.capabilities.secrets import SecretsManager

    sm = SecretsManager()

    async def secrets_get(_request: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            "data": {
                "status": sm.status(),
                "file": {k: v for k, v in sm.read_file().items()},
                "secrets_file": str(sm.secrets_file),
            },
        })

    async def secrets_post(request: web.Request) -> web.Response:
        body = await _read_json(request)
        values = {k: v for k, v in body.items() if v}  # 空值忽略，保留旧值
        if not values:
            return web.json_response({"ok": False, "error": "empty"}, status=400)
        sm.save_file(values)
        return web.json_response({
            "ok": True,
            "data": {"saved": list(values.keys()), "status": sm.status()},
        })

    # ---- 智能体管道：路由 → 五角色 → 浏览器动作 → 结果（§4.4 / §7.3）----
    async def collab(request: web.Request) -> web.Response:
        """完整智能体管道。异常显式捕获转 200 + ok:false（带 error 信息），
        绝不让 503 空 body 吞掉原因（§9B.4 绝不静默失败）。"""
        try:
            body = await request.json()
            goal = body.get("goal", "").strip()
            task_class = body.get("task_class", "default")
            if not goal:
                return web.json_response({"ok": False, "error": "empty goal"}, status=400)

            from hermes_superagent.collab.crew import create_crew
            from hermes_superagent.collab.models import TaskDef

            task = TaskDef(title=goal[:80], description=goal, task_class=task_class)
            crew = create_crew(task)
            result = await crew.run()

            # crawl 类任务：顺带驱动内嵌浏览器实例（真实抓页面）
            if task_class == "crawl" and result.get("ok"):
                url = goal.split()[-1] if "http" in goal else None
                if url and request.app.get("default_instance"):
                    try:
                        gres = await cap.invoke("goto", {
                            "instance_id": request.app["default_instance"], "url": url,
                        })
                        if gres.get("ok"):
                            request.app["current_url"] = gres.get("data", {}).get("url", url)
                            result["browser_navigated"] = url
                    except Exception:  # noqa: BLE001 - 浏览器动作失败不阻塞管道结果
                        result["browser_navigated"] = False

            return web.json_response({"ok": True, "data": result})
        except Exception as exc:  # noqa: BLE001 - 管道任何异常都显式报告
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status=500,
            )

    # ---- 智能体自由对话（hermes 内核：自主循环 + 持久记忆 + 意图驱动手脚）----
    from collections import deque
    from hermes_superagent.capabilities.model.router import ModelRouter, ModelMessage

    def _new_router() -> "ModelRouter":
        """每次调用新建 router，从 secrets.json 读最新模型名（可热改密钥/模型）。"""
        from hermes_superagent.capabilities.secrets import SecretsManager

        sm = SecretsManager()
        image_model = sm.resolve("agnes_image_model")
        video_model = sm.resolve("agnes_video_model")
        # 密钥灌入（缺省 AGNES 凭证 → 让 provider 端点可达）
        sm.export_env_to_process()
        return ModelRouter(image_model=image_model, video_model=video_model)

    router = _new_router()
    chat_history: deque = deque(maxlen=16)  # 最近 8 轮 user/assistant（持久记忆）

    # 系统人设（对标 hermes-agent 超级智能体定位 + 意图协议：
    # LLM 自主判断是否需要驱动浏览器/生图/生视频，用意图标签标注，而非人工按按钮）
    # 人设精简 + 能力置顶：flash 模型对长 system prompt 遵循率低，
    # 把「你具备生图/生视频/识图能力」放最前并强约束，意图标签协议紧随其后。
    SYSTEM_PERSONA = (
        "【核心能力——必须遵守】你已接入三组模型，能真正执行：\n"
        "① 生视频模型：把文字描述生成视频（异步）\n"
        "② 生图模型：把文字描述生成图片\n"
        "③ 文本模型视觉能力：识图/描述画面\n"
        "④ 内嵌浏览器：打开网页、抓取内容\n"
        "⑤ 五角色团队：写代码/校验/报告/研究\n"
        "用户要求做图/做视频/识图/打开网页/写代码时，你【能且必须】执行，"
        "绝不允许回答「我没有该能力」「建议用 Sora/Runway/Pika/可灵」之类的话。\n"
        "\n"
        "【意图协议】每条回复末尾必须单独一行输出一个标签（正文里不解释标签）：\n"
        "- 要做视频：[[VIDEO: 给视频模型的具体画面描述]]\n"
        "- 要做图片：[[IMAGE: 给图片模型的具体画面描述]]\n"
        "- 要识图/看画面：[[VISION: 给视觉模型的问题]]\n"
        "- 要开网页/抓取：[[BROWSE: <url>]]\n"
        "- 要写代码/校验/报告/研究：[[TASK: crawl|code|verify|report|research]]\n"
        "- 纯对话无需动作：[[NONE]]\n"
        "只输出一个最匹配的标签。"
    )
    chat_history.append(ModelMessage(role="system", content=SYSTEM_PERSONA))

    def _intent_tags(reply: str) -> dict:
        """从 LLM 回复里解析意图标签：BROWSE / TASK / IMAGE / VIDEO / VISION / NONE。"""
        import re
        out = {"browse_url": None, "task_class": None,
               "image_prompt": None, "video_prompt": None, "vision_question": None}
        m = re.search(r"\[\[BROWSE:\s*([^\]]+)\]\]", reply)
        if m:
            out["browse_url"] = m.group(1).strip()
        m = re.search(r"\[\[TASK:\s*([^\]]+)\]\]", reply)
        if m:
            out["task_class"] = m.group(1).strip()
        m = re.search(r"\[\[IMAGE:\s*([^\]]+)\]\]", reply)
        if m:
            out["image_prompt"] = m.group(1).strip()
        m = re.search(r"\[\[VIDEO:\s*([^\]]+)\]\]", reply)
        if m:
            out["video_prompt"] = m.group(1).strip()
        m = re.search(r"\[\[VISION:\s*([^\]]+)\]\]", reply)
        if m:
            out["vision_question"] = m.group(1).strip()
        return out

    async def chat(request: web.Request) -> web.Response:
        """智能体自由对话：人设 + 多轮记忆 + 真实 LLM（cheap 档 agnes-flash 主力）+ 意图驱动。"""
        body = await _read_json(request)
        user_msg = (body.get("message") or "").strip()
        if not user_msg:
            return web.json_response({"ok": False, "error": "empty message"}, status=400)

        try:
            import asyncio as _asyncio

            # 消息序列：系统人设 + 历史（最近 7 轮）+ 本轮用户消息
            msgs: list[ModelMessage] = []
            hist = list(chat_history)  # 最新在前
            hist.reverse()              # 变旧→新
            msgs.extend(hist[-14:])
            msgs.append(ModelMessage(role="user", content=user_msg))

            # 模型调用是同步 httpx，丢线程池避免阻塞事件循环
            resp = await _asyncio.to_thread(router.complete, msgs, task_class="cheap")

            # 意图解析 → 记忆追加（标签不进历史，避免污染上下文）
            # LLM 有时把标签写进正文中间而非行尾，统一剥除所有位置的标签
            tags = _intent_tags(resp.content)
            clean_reply = re.sub(r"\[\[(BROWSE|TASK|IMAGE|VIDEO|VISION|NONE):\s*[^\]]*\]\]",
                                 "", resp.content).strip()
            chat_history.appendleft(ModelMessage(role="user", content=user_msg))
            chat_history.appendleft(ModelMessage(role="assistant", content=clean_reply))
            while len(chat_history) > 16:
                chat_history.pop()

            data = {
                "reply": clean_reply,
                "model": resp.model,
                "usage": resp.usage,
                "latency_ms": resp.latency_ms,
                "history_len": len(chat_history) - 1,  # 不含 system
            }
            # 意图 → 智能体自主驱动手脚（浏览器 / 生图 / 生视频 / 识图）
            if tags["browse_url"]:
                data["intent"] = "browse"
                data["browse_url"] = tags["browse_url"]
            elif tags["task_class"]:
                data["intent"] = "task"
                data["task_class"] = tags["task_class"]
            elif tags["image_prompt"]:
                data["intent"] = "image"
                data["image_prompt"] = tags["image_prompt"]
            elif tags["video_prompt"]:
                data["intent"] = "video"
                data["video_prompt"] = tags["video_prompt"]
            elif tags["vision_question"]:
                data["intent"] = "vision"
                data["vision_question"] = tags["vision_question"]
            else:
                data["intent"] = "none"

            return web.json_response({"ok": True, "data": data})
        except Exception as exc:  # noqa: BLE001 - 对话故障显式报告，不静默
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status=500,
            )

    # ---- 多模态：识图（vision）/ 生图（image）/ 生视频（video）----
    # 文本模型识图：把图片以 OpenAI 多模态 content 块交给视觉模型（task_class=vision）
    # 生图 / 生视频：走 agnes images/generations + 异步 video/generations（任务制）

    async def vision(request: web.Request) -> web.Response:
        """识图：POST /vision {image_b64, image_url, question} → 文本模型看图的回答。"""
        body = await _read_json(request)
        question = (body.get("question") or "描述这张图片。").strip()
        b64 = (body.get("image_b64") or "").strip()
        image_url = (body.get("image_url") or "").strip()
        if not b64 and not image_url:
            return web.json_response({"ok": False, "error": "image_b64 or image_url required"}, status=400)
        try:
            if b64:
                # 允许纯 base64 或 data URL
                url = b64 if b64.startswith("data:") else f"data:image/png;base64,{b64}"
            else:
                url = image_url
            content: list[dict[str, Any]] = [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": url}},
            ]
            import asyncio as _asyncio

            router_now = _new_router()
            resp = await _asyncio.to_thread(
                router_now.complete,
                [ModelMessage(role="user", content=content)],
                task_class="vision",
            )
            return web.json_response({
                "ok": True,
                "data": {"answer": resp.content, "model": resp.model,
                         "usage": resp.usage, "latency_ms": resp.latency_ms},
            })
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)

    async def image(request: web.Request) -> web.Response:
        """生图：POST /image {prompt, size, model} → 生图模型出图（url + b64）。"""
        body = await _read_json(request)
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return web.json_response({"ok": False, "error": "prompt required"}, status=400)
        size = body.get("size", "512x512")
        try:
            import asyncio as _asyncio

            router_now = _new_router()
            result = await _asyncio.to_thread(router_now.generate_image, prompt, size)
            return web.json_response({"ok": True, "data": result})
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)

    async def video_create(request: web.Request) -> web.Response:
        """生视频：POST /video {prompt} → 提交异步任务，返回 task_id。"""
        body = await _read_json(request)
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return web.json_response({"ok": False, "error": "prompt required"}, status=400)
        try:
            import asyncio as _asyncio

            router_now = _new_router()
            task_id = await _asyncio.to_thread(router_now.create_video, prompt)
            return web.json_response({
                "ok": True, "data": {"task_id": task_id,
                                     "hint": "轮询 /video/status?task_id=... 取结果"}})
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)

    async def video_status(request: web.Request) -> web.Response:
        """查视频任务：GET /video/status?task_id=... → {status, url}。"""
        task_id = (request.query.get("task_id") or "").strip()
        if not task_id:
            return web.json_response({"ok": False, "error": "task_id required"}, status=400)
        try:
            import asyncio as _asyncio

            router_now = _new_router()
            result = await _asyncio.to_thread(router_now.get_video, task_id)
            return web.json_response({"ok": True, "data": result})
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)

    # 识图请求会带 base64 大图（动辄 1–5MB），aiohttp 默认 1MB 上限会 413；
    # 提到 15MB（仍远低于视频等重型负载，仅文本+图片 base64 场景）。
    app = web.Application(client_max_size=15 * 1024 * 1024)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/panel.html", panel)
    app.router.add_get("/thumb/frames", frames)
    app.router.add_get("/thumb/status", status)
    app.router.add_post("/thumb/goto", goto)
    app.router.add_post("/thumb/mouse", mouse)
    app.router.add_get("/secrets", secrets_get)
    app.router.add_post("/secrets", secrets_post)
    app.router.add_post("/collab", collab)
    app.router.add_post("/chat", chat)
    # 多模态（文本模型识图 + 生图模型生图 + 生视频）
    app.router.add_post("/vision", vision)
    app.router.add_post("/image", image)
    app.router.add_post("/video", video_create)
    app.router.add_get("/video/status", video_status)
    return app


async def run_thumb_server(port: int = DEFAULT_PORT) -> None:
    from aiohttp import web

    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    print(f"thumb browser panel: http://127.0.0.1:{port}/panel.html")
    print(f"frame stream:        http://127.0.0.1:{port}/thumb/frames")
    await asyncio.Event().wait()


__all__ = ["create_app", "run_thumb_server", "DEFAULT_PORT", "base64"]
