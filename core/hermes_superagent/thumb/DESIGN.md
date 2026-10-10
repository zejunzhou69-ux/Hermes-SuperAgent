# 拇指浏览器（Thumb Browser）UI 设计

> 对应设计方案 §4.2 / 需求 R4 / 魔改点 D2（client 插件：拇指浏览器面板）。
> 本文件是**设计说明**；可运行骨架见 `panel.html`（纯静态、无依赖，
> 直接打开或经帧流服务注入即可预览交互）。

## 1. 定位

宿主 GUI（DSH Web GUI）内的一个**小尺寸内嵌面板**，实时显示内建
Chromium 的画面，让"机器在爬什么"对**人和 Supervisor 都可见**
（设计原则 4 可观测；§9.3 失败时附截图留证）。

- 默认比例：**360×640**（"拇指"比例，右侧停靠）
- 帧流：**JPEG/WebP**，默认 4 fps（`thumbBrowser.fps` 可配）
- 不是全量截图轮询：帧仅在当前页变化或被请求时产出（省 CPU，K6）

## 2. 布局

```
┌────────────────────────────┐
│ 地址栏 [url]        [⏸/▶] │  ← 顶栏 32px
├────────────────────────────┤
│                            │
│      <canvas 360×640>      │  ← 帧流渲染区（自适应缩放）
│      （实时画面）           │
│                            │
├────────────────────────────┤
│ [截图存证] [元素点选] [固化为技能] │  ← 底栏 40px
└────────────────────────────┘
```

- 左侧停靠/右侧停靠可配；`width/height` 可配（配置中心 `hermesSuperagent.thumbBrowser`）
- 缩放：面板尺寸变化时按容器重新计算 canvas CSS 尺寸，帧流不变

## 3. 交互能力（§4.2 支持清单）

| 能力 | 实现 |
|---|---|
| 地址栏 | `browser.goto`（MCP tool） |
| 前进/后退 | 后续迭代：`browser.back/forward`（骨架预留） |
| 暂停/继续自动化 | 顶栏 ⏸：暂停 Supervisor 下发的自动化，画面继续推流 |
| 点选元素 | 点击 canvas 上的坐标 → 换算 viewport 坐标 → `browser.click` |
| 截图存证 | 底栏按钮 → `browser.screenshot`，base64 入会话 + 失败证据链 |
| 固化为技能 | §4.5 自进化闭环：把本次有效操作序列写为 `skills/<name>/SKILL.md` |

## 4. 数据通路（§5 数据流）

```
Playwright Page.screenshot(stream)  [Python]
   → BrowserCapability.frame()  缩放 JPEG ≤360px
   → SSE: GET /thumb/frames?instance=xxx  （帧流服务，127.0.0.1）
   → 面板 <canvas> 逐帧 putImage（EventSource，断线自动重连）
   人工/Supervisor 操作
   → MCP tools (browser.*) / 面板按钮 → 同一实例
```

- 渲染**不阻塞**抓取主链（§5.2：帧流与自动化并发）
- 地址栏与 URL 同步：SSE 每帧附 `url` 字段，面板显示
- 无 token 的本地回环服务仅监听 127.0.0.1（§6.3 鉴权模型一致）

## 5. 与 DSH 宿主集成（魔改点 D2）

- client 插件 `hermes-superagent-thumb` 声明一个 `Slot`（右侧停靠区）
- Slot 内容为 `<iframe src="http://127.0.0.1:8766/panel.html">`
  （帧流服务同时托管本文件）；后续可替换为 React 组件直接渲染
- 宿主重启时面板自动重连（EventSource 内建重连 + 实例 id 协商）

## 6. 失败态

- 实例不存在 / 浏览器不可用：面板显示占位图 + 一行告警（不静默，§9.3）
- 风控命中（L1/L2 升级事件）：顶栏红色徽标 `⚠ 风控 → L{n+1}`
- L4 外接请求：弹"请接管本地 Chrome（CDP 端口 9222）"按钮
