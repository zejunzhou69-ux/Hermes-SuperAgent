# Hermes-SuperAgent

> **一个能自己上网、自己长出能力、自己能组队干活的开源智能体运行时。**

[![Python](https://img.shields.io/badge/python-%3E%3D3.10-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)]()
[![多模态](https://img.shields.io/badge/multimodal-识图%20%2B%20生图%20%2B%20生视频-purple)]()
[![可交互](https://img.shields.io/badge/browser-画面滚轮/点击-可交互-teal)]()
[![部署](https://img.shields.io/badge/部署-一键脚本-绿)](一键部署.bat)

Hermes-SuperAgent 把三个开源项目魔改融合，构建出一个**自带嵌入式浏览器、强爬取、可深度魔改、国内外双栈接入、支持团队多智能体协作**的超级智能体。

**多模态版**：三个模型各就各位 —— `agnes-3.0-flash`（文本·视觉）识图、`agnes-image-2.5-flash` 生图、`agnes-video-2.5-flash` 生视频；密钥/模型可在面板设置栏**随时更换**，画面支持**滚轮滚动 / 鼠标点击**直接操作内嵌浏览器。

---

## 目录

- [它是什么](#它是什么)
- [核心能力](#核心能力)
- [架构](#架构)
- [快速开始](#快速开始)
- [项目结构](#项目结构)
- [魔改点清单](#魔改点清单)
- [合规红线与安全边界](#合规红线与安全边界)
- [测试与验证](#测试与验证)
- [文档](#文档)
- [许可证](#许可证)

---

## 它是什么

大多数开源智能体只能做一件事：要么是**编码助手**（Codex CLI），要么是**通用对话**（原版 hermes），要么是**低代码工作流**（dify）。它们的能力上限被各自的定位锁死了。

Hermes-SuperAgent 要的是**把上限顶开**：

| 目标 | 做法 |
|---|---|
| **长程任务**（分钟～小时级） | CrewAI 五角色协作 + LangGraph 长任务逃生舱 |
| **工具上限不被封死** | 一切皆插件：Capability 热插拔 + MCP 双栈挂载 |
| **自主性** | 主管自主拆解 / 分派 / 裁决 / 重派，人工只在关键节点介入 |
| **鲁棒性** | 429 / 断网 / 单角色失败都不中断任务：自动降级、切路由、换角色续跑 |

同时它**自带一个浏览器**——不是一个只读的画面，而是你能用鼠标键盘直接操作的**真浏览器**，agent 也能用它干活。

### 能力上限目标（验收锚点）

| # | 指标 | 含义 |
|---|---|---|
| U1 | 长程任务 | 稳定跑完跨多子任务、分钟到小时级任务，中途不丢上下文 |
| U2 | 工具上限 | 工具可深度魔改、热插拔，扩展上限不被架构封死 |
| U3 | 自主性上限 | 多角色自主拆解/分派/裁决/重派 |
| U4 | 鲁棒性上限 | 模型限流 / 断网 / 单角色失败时任务不中断 |

---

## 核心能力

### 1. 嵌入浏览器 + 强爬取

**双模式浏览器**：

| 模式 | 机制 | 适用 |
|---|---|---|
| 内建模式 | Playwright 驱动内置 Chromium，全托管 | 无人值守爬取、批量渲染 |
| 外接模式 | CDP 接管已开的 Chrome（`--remote-debugging-port`） | 需登录态、需过风控、需人工介入 |

**爬取分级**（自动升级，可配置）：

| 级别 | 手段 | 反爬对抗 |
|---|---|---|
| L1 | `httpx` + 解析（静态页，CDN 感知） | 弱 |
| L2 | Headless Chromium（动态页） | 中 |
| L3 | 指纹伪装 + 代理 + 人类化节奏 | 强 |
| L4 | 外接真实浏览器 + 人工过验证 | 最强 |

调度：默认 L1 → 失败升 L2 → 命中风控升 L3 → 仍失败请求外接 L4。

### 2. 拇指浏览器（Thumb Browser）

宿主 GUI 内的**可交互内嵌浏览器**：

- **你本人**可用鼠标 / 键盘自由操控：点链接、打字、搜索、滚动、手动登录
- **agent** 用同一个浏览器干活，画面实时推到面板（默认 360×640 竖屏 @ 4fps 帧流）
- 运行在**沙箱内**，独立 `chrome-profile/`，是沙箱的可视窗口

> 「能用」由拇指浏览器保证，「用了不炸」由沙箱保证。

```bash
python -m hermes_superagent panel     # 终端启用即弹出桌面面板
```

![拇指浏览器桌面面板：真 Chromium 渲染 python.org](docs/images/panel.png)

*上图由 e2e 实测截取：面板经 `/thumb/goto` 导航到 python.org，真页面帧流式渲染进 canvas（360×640 竖屏）。*

### 3. 多智能体团队协作

```
              ┌──────────────┐
              │  Supervisor  │  拆解 · 分派 · 裁决 · 重派
              └──────┬───────┘
      ┌──────────────┼──────────────┬──────────────┐
      ▼              ▼              ▼              ▼
┌───────────┐ ┌───────────┐ ┌───────────┐ ┌───────────┐
│  Crawler  │ │   Coder   │ │ Verifier  │ │ Reporter  │
│ 采集/清洗  │ │ 写码/魔改  │ │ 校验/复现  │ │ 汇总/交付  │
└───────────┘ └───────────┘ └───────────┘ └───────────┘
```

- 默认 **CrewAI** 主干（`sequential` / `hierarchical` 两种流程）
- 超 30 分钟的长任务切 **LangGraph** 逃生舱（checkpointer + 人工介入）
- **路由不变式**：① 取数据必经 Crawl Capability ② 产物必经 Verifier ③ 失败必须产出决策记录，绝不静默吞错

### 4. 模型 / 工具双栈接入

**五路模型适配**（统一 `BaseProvider` 接口）：

| 路由 | 提供商 |
|---|---|
| 国际-前沿 | OpenAI / Anthropic |
| 国际-备用 | agnes-flash 系列 |
| 国内-主力 | DeepSeek |
| 本地 | Ollama（离线兜底，永远可达） |

`ModelRouter` 按 `task_class`（reasoning / coding / crawl-summarize / cheap）决策，**429 / 超时自动沿 fallbacks 降级**，全部降级事件可观测。

**工具接入**：MCP 协议统一暴露，内置 `browser` / `crawl` / `search` / `fs` / `shell` / `code` 六个命名空间；第三方（飞书 / 钉钉 / 语雀 / Gitee / GitHub / Slack / Notion）走适配器，**强制声明官方 API 合规来源**。

### 5. 国内 / 国际双栈

| 维度 | 国际 | 国内 |
|---|---|---|
| 模型 API | OpenAI / Anthropic | DeepSeek / 通义 / 智谱 |
| 包源 | PyPI / npm | 清华 TUNA / 阿里云 / npmmirror |
| 搜索 | Google / Brave | Bing CN / 百度 / 搜狗 |
| 代码托管 | GitHub | Gitee / 自建 Git |
| 浏览器下载 | Playwright CDN | 国内镜像 |

`region: auto` 启动探测（连通性 + 延迟），结果缓存、GUI 可覆盖；任一探测失败不阻塞启动，降级并显式告警。所有镜像 / 代理**配置化**，代码中无硬编码域名。

### 6. 自魔改能力（技能层）

三层扩展点，**越往上越轻**：

| 层 | 扩展方式 | 热加载 | 需重启 |
|---|---|---|---|
| **技能层** | 丢一个 `skills/<name>/SKILL.md` | ✅ | ❌ |
| 工具层 | 实现 MCP server 或 Tool 接口 | ✅ | ❌ |
| 宿主插件层 | DSH 插件（TS） | ✅（client） | 视类型 |

技能就是一个 Markdown 文件，YAML front matter 声明元数据，正文是给 agent 看的步骤：

```markdown
---
name: web-digest
description: 抓取网页并产出结构化摘要
triggers: [抓取, 摘要, digest]
tools: [crawl, model, fs]
---
# 步骤
1. crawl.fetch …
```

```bash
python -m hermes_superagent skill                        # 列出
python -m hermes_superagent skill --match "帮我抓取网页"   # 检索（触发词/名称/描述打分）
python -m hermes_superagent skill --scaffold my-fix --description "固化一次有效解法"
```

**自进化闭环**：任务跑顺后把当时的有效解法用 `skill.scaffold` 固化成新技能，下次同类任务直接命中复用。热加载按 mtime 增量重读，**不重启进程**。

**不静默**：技能定义缺字段不阻断加载，但会产出可见告警；坏掉的技能只记入 `errors` 不拖垮整层。

---

## 架构

```
┌──────────────────────────────────────────────────────────────────────┐
│                        DeepSeek Harness (TS 宿主)                     │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────────────┐  │
│  │ 插件管理器      │  │ 配置中心        │  │ Web GUI / 拇指浏览器面板 │  │
│  └────────────────┘  └────────────────┘  └────────────────────────┘  │
│                              │                                        │
│                    Local Capability Bridge (JSON-RPC + SSE)           │
└──────────────────────────────┼────────────────────────────────────────┘
                               │
┌──────────────────────────────▼────────────────────────────────────────┐
│                     hermes-superagent Core (Python)                    │
│  ┌──────────────────────────────────────────────────────────────────┐ │
│  │  协作层 (CrewAI)：Supervisor · Crawler · Coder · Verifier · Reporter│
│  └───────────────┬──────────────────────────────────────────────────┘ │
│  ┌───────────────▼────────────┐   ┌───────────────────────────────┐   │
│  │  能力层                     │   │  记忆 / 技能                   │   │
│  │  Browser / Crawl / Model    │   │  Memory · Skill · Growth      │   │
│  │  Tool Registry (MCP)        │   │  (hermes 内核)                 │   │
│  └───────────────┬────────────┘   └───────────────────────────────┘   │
│  ┌───────────────▼──────────────────────────────────────────────────┐ │
│  │  执行层：Playwright/Chromium 池 │ CDP 外接 │ HTTP 抓取 │ 渲染队列  │ │
│  └──────────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────────┘
```

### 三根支柱

| 层 | 项目 | 许可证 | 职责 |
|---|---|---|---|
| 智能体内核 | [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) | MIT | 自主循环、持久记忆、技能成长 |
| 运行时 / 插件宿主 | [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) | MIT | 「一切皆插件」宿主、配置、GUI、能力路由 |
| 多智能体协作 | [CrewAI](https://github.com/crewAIInc/crewAI) | MIT | 角色化团队编排、任务路由 |

#### 为什么用 CrewAI 替代 autogen

| 维度 | autogen（不采用） | CrewAI（采用） |
|---|---|---|
| 活跃度 | 主仓 push 停在 2026-04 | 持续活跃 |
| 许可证 | CC-BY-4.0（**非软件许可证**，商用风险） | MIT |
| 抽象贴合度 | 偏「对话群聊」研究范式 | 「角色 + 任务 + 流程」，直接命中 |
| 落地成本 | 需自行设计编排与终止条件 | 开箱即用 |

LangGraph 保留为**长任务状态机补充**（断点续跑 / 人工审批 / 确定性分支），不作默认协作层。

---

## 快速开始

### 环境要求

- Python >= 3.10
- Node.js `^22.19.0 || >=24.0.0`（仅宿主插件需要）

### 安装

```bash
# 1) 获取代码（替换为实际仓库地址）
git clone <your-repo-url> hermes-superagent
cd hermes-superagent/core

# 核心依赖（爬取 + 拇指浏览器 + 桥接）
pip install -e .

# 可选：真实浏览器（Playwright）
pip install -e ".[browser]"
playwright install chromium

# 可选：多智能体协作
pip install -e ".[collab]"

# 开发依赖（跑测试）
pip install -e ".[dev]"
```

### 跑起来

```bash
# 1) 抓取一个 URL（分级调度）
python -m hermes_superagent crawl https://example.com --level L1
python -m hermes_superagent crawl https://example.com --level L2 --headful

# 2) 启动拇指浏览器帧流服务（默认 8766）
python -m hermes_superagent thumb --port 8766
#    浏览器打开 http://127.0.0.1:8766/panel.html

# 3) 以 stdio 运行 MCP server
python -m hermes_superagent mcp

# 4) 启动 Bridge（JSON-RPC + SSE，供 DSH 宿主调用，默认 8765）
python -m hermes_superagent bridge --port 8765

# 5) 查看分层沙箱策略 + 安装通道体检（M4）
python -m hermes_superagent sandbox
python -m hermes_superagent sandbox --json

# 6) 启动并弹出桌面面板（拇指浏览器，对标 DSH 面板体感）
python -m hermes_superagent panel
python -m hermes_superagent panel --native   # 原生窗口（需 pip install pywebview），失败回退浏览器
python -m hermes_superagent panel --no-open  # 只起服务，手动开 URL

# 7) 技能层（自魔改能力）：列出 / 检索 / 固化新技能
python -m hermes_superagent skill
python -m hermes_superagent skill --match "帮我抓取网页"
python -m hermes_superagent skill --scaffold my-fix --description "固化一次有效解法" --trigger 修复
```

### 实操演示（真浏览器 + 真抓取 + 截图存证）

```bash
cd hermes-superagent/core
python demo_live.py                        # 自动挑可达目标
python demo_live.py https://www.python.org # 指定目标
```

6 步能力链逐步记录结果，产出 `demo_output/`：整页截图 PNG、拇指帧 JPEG、`report.json`。
退出码 0=全通过、1=有失败，可直接接 CI。

| 实测目标 | 抓取 | 截图 | 拇指帧 |
|---|---|---|---|
| example.com | 577 字 | 39 KB | 360×202 |
| python.org | 52543 字（正文 7124） | 90 KB | 360×202 |

### 宿主插件（TS）

```bash
cd hermes-superagent/harness-plugin

# 注意：若环境变量 NODE_ENV=production，npm 会跳过 devDependencies，
# 必须显式加 --include=dev，否则 tsdown / typescript 装不上，构建会失败。
npm install --include=dev

npm run typecheck  # tsc --noEmit
npm run build      # tsdown 双构建：lib/index.js(+d.ts) host 半边、lib/client.js client 半边
npm test           # 结构契约断言（8 组）
```

构建产物：

| 产物 | 形态 | 说明 |
|---|---|---|
| `lib/index.js` | ESM | host 半边；`zod`、`@deepseek-ai/cordis` 外部化 |
| `lib/index.d.ts` | d.ts | host 类型声明 |
| `lib/client.js` | CJS 闭包工厂 | client 半边；被包进 `window.__ModuleLoader__.load({ factory: (require) => … })`，`react` 由宿主模块表注入 |

### 五角色协作 demo

```bash
cd hermes-superagent/core
python -m hermes_superagent.collab demo "抓取 example.com 并写解析脚本"
```

---

## 项目结构

```
hermes-superagent/
├── README.md                        # 本文件（智能体介绍材料）
├── LICENSE                          # MIT
├── docs/
│   ├── images/panel.png             # 拇指浏览器桌面面板实测截图
│   ├── design.md                    # 总体设计方案（架构/接口/魔改点/风险/里程碑）
│   ├── collab-framework.md          # CrewAI vs LangGraph 选型与五角色拓扑
│   └── agent-harness-contract.md    # agent–harness 契约（RPC/事件/帧流/验收口径）
├── core/                            # Python 内核
│   ├── pyproject.toml
│   ├── hermes_superagent/
│   │   ├── __main__.py              # CLI：crawl / mcp / thumb / panel / bridge / sandbox
│   │   ├── agent_harness.py         # Capability 注册表 + JSON-RPC + 事件总线
│   │   ├── bridge_server.py         # aiohttp 127.0.0.1 服务端（/rpc /events /frames）
│   │   ├── panel_launcher.py        # 桌面面板弹出（原生窗口/浏览器/仅打印 URL）
│   │   ├── capabilities/            # 能力层
│   │   │   ├── browser.py           #   内建池 + CDP 外接 + 帧流
│   │   │   ├── browser_mcp.py       #   MCP 封装
│   │   │   ├── crawl.py             #   L1–L4 分级调度 + 令牌桶限速
│   │   │   ├── secrets.py           #   三层密钥管理
│   │   │   ├── hermes_adapter.py    #   hermes 内核接入 ModelRouter
│   │   │   ├── model/               #   providers / router / capability
│   │   │   └── tool/                #   registry / mcp / capability
│   │   ├── collab/                  # CrewAI 协作层
│   │   │   ├── agents.py            #   五角色 + LiteAgent 兜底
│   │   │   ├── crew.py              #   HermesCrew
│   │   │   ├── pipeline.py          #   CrewRunner / SEQUENTIAL_CHAIN
│   │   │   ├── router.py            #   六类路由 + 不变式
│   │   │   ├── middleware.py        #   成本追踪 + 429 降级
│   │   │   ├── long_task.py         #   LangGraph 逃生舱
│   │   │   └── roles.yaml           #   角色定义（YAML 标准化）
│   │   ├── net/routing.py           # 双栈路由 + 限速
│   │   ├── skills/                  # 技能层（自魔改能力，热加载）
│   │   │   ├── loader.py            #   发现 + front matter 解析 + 校验
│   │   │   ├── registry.py          #   注册表：热加载 / 检索 / scaffold 固化
│   │   │   └── models.py            #   Skill 数据模型
│   │   ├── sandbox/                 # 分层沙箱（M4）
│   │   │   ├── policy.py            #   四层策略 + 硬约束
│   │   │   ├── fs_guard.py          #   文件/Shell 守卫
│   │   │   ├── install_diagnostics.py #  安装体检 + 失败归因
│   │   │   └── browser_sandbox.py   #   隔离 profile + CDP opt-in 留痕
│   │   └── thumb/                   # 拇指浏览器（panel / server）
│   ├── tests/                       # 134 个单测
│   ├── e2e_test.py                  # 端到端冒烟（含面板真渲染验证）
│   └── demo_live.py                 # 实操演示（真浏览器抓取+截图）
├── skills/                          # 技能目录（丢进来即生效）
│   └── web-digest/SKILL.md          #   示例技能：网页摘要
└── harness-plugin/                  # TS 宿主插件
    ├── package.json                 # dsh manifest（host/client 双出口）
    ├── cordis.patch.yml             # 插件注册
    ├── src/host/                    # index / config / bridge
    ├── src/client/                  # index / thumbBrowser.tsx
    ├── src/shared/contract.ts       # 契约常量（两侧一致性锁定）
    └── tests/structure.test.mjs     # 结构断言
```

---

## 魔改点清单

> 原则：优先插件与适配层，尽量不 fork 上游内部实现。

### hermes-agent

| # | 魔改点 | 方式 |
|---|---|---|
| H1 | 工具注册表接入 Capability 协议 | 适配层包装 |
| H2 | 记忆后端可插拔 | 实现后端接口 |
| H3 | 技能目录指向本项目 `skills/` | 配置 + 加载器扩展 |
| H4 | 成长闭环增加「固化建议」产出 | 轻度扩展 |
| H5 | 模型调用改走 ModelRouter | 适配层替换 |

### DeepSeek Harness

| # | 魔改点 | 方式 |
|---|---|---|
| D1 | 新增 `hermesSuperagent` 宿主插件 | 标准插件 |
| D2 | client 插件：拇指浏览器面板 | 挂载 Slot |
| D3 | Capability 路由注册 Python 侧能力 | 插件内声明 |
| D4 | 配置中心增加本项目配置段 | Schema 扩展 |
| D5 | 构建产物纳入发布流程 | tsdown 双构建 |

### autogen → CrewAI

| # | 魔改点 | 方式 |
|---|---|---|
| C1 | 不引入 autogen，直接用 CrewAI | 依赖决策 |
| C2 | 角色/任务定义标准化为 YAML | `roles.yaml` |
| C3 | 成本与限流中间件（429 降级） | `middleware.py` |
| C4 | 与 hermes 记忆打通 | 适配层 |

### 明确不改

- 不 fork Chromium
- 不改 DSH 内核（只加插件）
- 不重写 hermes 的自主循环

---

## 合规红线与安全边界

### 两条线原则

**线 A：查 / 读（低风险）—— 放心做**

公开网页抓取、信息检索、数据整理。走内建爬虫（遵守 robots + 限速）+ 拇指浏览器可视 + 沙箱隔离。

**线 B：发 / 写（高风险）—— 只走官方 API，个人号不碰**

| 平台 | 合规做法 | 禁止做法 |
|---|---|---|
| 微信 | **企业微信** 官方 API | 个人号外挂 / 非官方协议 |
| WhatsApp | **WhatsApp Business Platform** 官方 API | 非官方协议库 / 模拟真人高频 |
| Facebook | **Page / 群组** 走 Meta 官方 Graph API | 个人账号自动化 |

> 本项目**不实现**「伪装成真人绕过平台检测」。原因：技术上对微信/WhatsApp 这个量级的平台做不到；且「伪装真人骗风控」恰恰是封号概率最高的路线。正确做法是走官方 API 承诺的「不被封」。

红线为**代码级强制**：非官方通道工具在 Capability 层**入口即拒**，返回明确原因，并发出 `compliance.denied` 事件。

### 沙箱策略（分层）

| 层 | 默认 | 说明 |
|---|---|---|
| 内核（LLM / 记忆 / 路由） | 无沙箱 | 本就隔离进程 |
| 文件 / Shell | 可写沙箱 | 工作目录可写，系统路径只读 |
| 浏览器 / 爬取 | 完整沙箱 | Chromium 隔离，独立 `chrome-profile/` |
| 外接 CDP 接管真实浏览器 | 默认禁 + 显式 opt-in | 开启后动作留痕 |

**沙箱不卡正常使用**（硬约束）：

1. **搜索不被拦**——沙箱只管「跑在哪个隔离环境」，不管「你搜什么」。人机验证**不自动绕、不硬闯**：agent 卡住即把画面推上拇指浏览器，**由你本人手动过一次**。
2. **装东西不被卡**——「宽进严管」：包安装与双栈镜像源默认开放，工作区可写可装；只读区只圈系统关键路径。装不上必须**显式告警并指明卡点**，禁止静默卡死。

**实现与自检**（`core/hermes_superagent/sandbox/`）：

```bash
python -m hermes_superagent sandbox          # 打印生效策略 + 安装通道体检
python -m hermes_superagent sandbox --json   # 机器可读
```

| 模块 | 职责 |
|---|---|
| `policy.py` | 四层策略 + 硬约束；`guard_search()` 恒放行、`captcha_policy()` 交人工 |
| `fs_guard.py` | 工作区可写 / 系统只读 / 越界拒绝（必带 `blocked_at`）；Shell 不做命令白名单 |
| `install_diagnostics.py` | pip/npm 体检 + 失败归因（磁盘/权限/DNS/网络/TLS/包源/版本），未归因也不静默 |
| `browser_sandbox.py` | 隔离 `chrome-profile`；CDP 需 `supervisor` + `reason` 双要素并留痕 |

所有拒绝都返回 `Decision(allow=False, reason=..., blocked_at=...)` —— **拦截必须说明卡在哪**。

### 密钥管理

三层优先级：环境变量 > 密钥文件（0600）> 宿主 DSH 配置。所有密钥状态脱敏（`set` / `missing`）供 GUI 与 trace 使用，**不落盘明文日志**。

---

## 测试与验证

```bash
cd hermes-superagent/core
python -m pytest tests -q          # 109 passed
python e2e_test.py                 # 端到端冒烟
```

### 当前状态

| 项目 | 结果 |
|---|---|
| Python 单元测试 | **134 passed, 0 failed**（3.8s） |
| Python e2e 冒烟 | **5/5 通过**（含面板真渲染验证） |
| 实操演示 | **6/6 通过**（真 Chromium 抓取 + 截图 + 出帧） |
| TS 类型检查 | **`tsc --noEmit` 通过** |
| TS 构建 | **`tsdown` 双半构建通过**（lib/index.js + lib/client.js） |
| TS 结构契约 | **8/8 通过** |
| Bridge 联调 | ✅ 真实 aiohttp 服务上握手/鉴权/方法表/合规/帧流全通过 |
| 合规红线核验 | ✅ 非官方通道入口即拒 |
| 能力上限 U1–U4 | ✅ 逐项核验通过 |
| 沙箱硬约束（§9B.4） | ✅ 搜索不被拦 · 依赖可装 · 拦截必指明卡点 |

CI 配置见 [`.github/workflows/ci.yml`](.github/workflows/ci.yml)：Python 侧跑测试 + 冒烟，TS 侧跑类型检查 + 构建 + 结构契约 + 产物完整性。

### e2e 覆盖

- **MCP stdio**：`ping` → `pong`、`tools/list`（9 工具）、`tools/call` 合规失败返回 `isError=True`（不崩溃）
- **L2 浏览器**（真实 Playwright/Chromium）：`open` → `goto`（status 200）→ `screenshot`（约 82 KB JPEG）→ `frame`（360px）→ `detach`
- **L1 爬取**：example.com `status=200`
- **拇指浏览器 SSE**：`/thumb/status`、`/thumb/goto`、`/panel.html`（canvas）

### Bridge 联调覆盖

在进程内起**真实 aiohttp 服务**（非 mock），用真实 HTTP 客户端走完整契约：

- **握手**：`initialize` 返回协议版本 1.0 + 能力目录 + trace_id
- **版本协商**：客户端声明不匹配版本 → 显式拒绝（`protocol_mismatch`），不静默降级
- **鉴权**：无 / 错 Bearer token → 401
- **方法表**：`capability.list` / `capability.schema` / `net.status`
- **合规**：`compliance.reject` → 错误码 `-32001` + `compliance.denied` 事件
- **帧流**：帧源不可用 → **503 + 明确说明卡在哪**（而不是干瘪失败）
- **契约一致性**：Python `PROTOCOL_VERSION` 与 TS `contract.ts` 镜像常量比对

### 已知边界

- **真机 DSH 宿主联调**：Bridge 侧已按契约在真实 HTTP 服务上验证通过；接真实 DSH 宿主进程仍需在宿主侧环境跑一次（`python -m hermes_superagent bridge` + 宿主 `initialize`）。
- L3/L4 爬取需要代理池与真实浏览器会话，仓库内为可配置骨架。

---

## 文档

| 文档 | 内容 |
|---|---|
| [docs/design.md](docs/design.md) | 总体设计方案：目标与能力上限、技术选型、架构、模块设计、数据流、接口、魔改点、双栈适配、团队协作、合规红线、沙箱策略、风险、里程碑 |
| [docs/collab-framework.md](docs/collab-framework.md) | CrewAI vs LangGraph 逐项比较、五角色拓扑、六类路由、三条不变式、LangGraph 触发边界 |
| [docs/agent-harness-contract.md](docs/agent-harness-contract.md) | agent–harness 契约：RPC 方法表、错误码、SSE 事件、帧流契约、验收口径 A1–A8 |

---

## 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| M0 | 骨架 + Bridge 握手 | ✅ |
| M1 | 爬取核心（L1/L2 + 拇指浏览器） | ✅ |
| M2 | 协作层（CrewAI 五角色） | ✅ |
| M3 | 双栈模型/工具 + 429 降级 | ✅ |
| M4 | Harness 插件化收口 + 沙箱落地 | ✅ |
| M5 | 端到端验证 | ✅ |

---

## 许可证

[MIT](LICENSE)

上游依赖许可证：hermes-agent（MIT）、DeepSeek Harness（MIT）、CrewAI（MIT）。本项目**不使用** autogen（CC-BY-4.0，非软件许可证）。

---

<p align="center">
  <sub>Built with hermes-agent · DeepSeek Harness · CrewAI</sub>
</p>
