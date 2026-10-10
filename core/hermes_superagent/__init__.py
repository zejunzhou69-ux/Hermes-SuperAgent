"""hermes-superagent 内核（Python 侧）。

本包是设计方案 §4 中 core/ 模块的可运行骨架。

已实现范围：
- capabilities/browser.py：内建 Chromium 池 / CDP 外接 双模式浏览器能力（t2）
- capabilities/crawl.py：L1–L4 分级爬取调度（t2）
- capabilities/browser_mcp.py：MCP 化的 browser/crawl 工具（t2）
- capabilities/model/：四路模型适配层（OpenAI/Anthropic/DeepSeek/agnes-flash/Ollama）
  + ModelRouter（task_class 路由 + 429 降级）+ model Capability（t4）
- capabilities/tool/：工具注册表 + MCP 接入 + tool Capability（t4）
- capabilities/secrets.py：密钥管理（env > 密钥文件 > 宿主配置）（t4）
- capabilities/hermes_adapter.py：hermes 内核模型调用适配层，魔改点 H5（t4）
- thumb/：拇指浏览器 UI 设计（t2）
- collab/：CrewAI 五角色协作框架（t3）
- agent_harness.py + bridge_server.py：agent–harness 契约 Python 侧
  （Capability 注册表 + JSON-RPC 服务端 + 事件总线 + 合规拒绝，t5）
"""

__version__ = "0.1.0"
