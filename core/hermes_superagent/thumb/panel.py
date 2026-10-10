r"""Hermes-SuperAgent 双端工作台面板（panel.html）。

布局对标 DSH 桌面端初界面：
  左侧（约 38%）：
    ① 对话区——用户输入任务 → 完整智能体管道（路由→五角色→浏览器动作），
       消息流里显示角色交接、LLM 路由、耗时、成本
    ② 设置栏——密钥配置（agnes/openai/deepseek 等）、端点、模型路由、通用选项，
       保存写 secrets.json
  右侧（约 62%）：
    大尺寸内嵌浏览器实时画面（帧流 SSE → canvas），可跳转/暂停/截图/点选

设计原则（§9B.4）：绝不静默失败——任何通道故障都显式告警。
零依赖纯 HTML/CSS/JS，深色专业桌面应用风格。
"""

PANEL_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes-SuperAgent · 双端工作台</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0d0f13; --bg2: #131519; --bg3: #171a20;
    --surface: #1a1e26; --surface2: #20242d;
    --border: #272c35; --border2: #343b47;
    --text: #d8dce4; --text2: #9aa3b2; --dim: #5b6472;
    --accent: #4c8dff; --accent2: #35c47a;
    --ok: #35c47a; --warn: #ffb020; --err: #e5534b;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 13px/1.55 "Segoe UI", system-ui, -apple-system, sans-serif;
    display: flex; flex-direction: column; overflow: hidden;
  }

  /* ===== 顶栏 ===== */
  header {
    display: flex; align-items: center; gap: 10px;
    padding: 0 16px; height: 46px; flex: none;
    background: var(--bg2); border-bottom: 1px solid var(--border);
  }
  .logo { width: 10px; height: 10px; border-radius: 3px;
          background: var(--accent);
          box-shadow: 0 0 14px rgba(76,141,255,.4); flex: none; }
  .title { font-weight: 600; font-size: 14px; letter-spacing: .3px; }
  .ver { color: var(--dim); font-size: 11px; margin-left: 2px; }
  .spacer { flex: 1; }
  .pill {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 12px; border-radius: 999px; font-size: 12px;
    background: var(--surface); border: 1px solid var(--border);
  }
  .pill .dot { width: 7px; height: 7px; border-radius: 50%;
               background: var(--dim); flex: none; }
  .pill.on .dot { background: var(--ok); box-shadow: 0 0 8px var(--ok); }
  .pill.off .dot { background: var(--err); }
  .pill.wait .dot { background: var(--warn); box-shadow: 0 0 8px var(--warn); }

  /* ===== 主体（左右结构 + 可拖拽中线） ===== */
  main { flex: 1; display: flex; min-height: 0; }

  /* ===== 左端：对话 + 设置（宽度由中线拖拽控制） ===== */
  .left {
    width: 38%; min-width: 320px; max-width: 70%;
    flex: none;
    display: flex; flex-direction: column;
    background: var(--bg);
  }

  /* ===== 中线（可拖拽分割，auto 拉大拉小） ===== */
  .divider {
    width: 5px; flex: none; cursor: col-resize;
    background: var(--border);
    position: relative; z-index: 5;
    transition: background .15s;
  }
  .divider:hover, .divider.dragging { background: var(--accent); }
  /* 中线两侧热区扩大（视觉 5px，命中 12px） */
  .divider::after {
    content: ''; position: absolute; top: 0; bottom: 0;
    left: -4px; right: -4px;
  }

  /* ===== 右端：大浏览器（吃满剩余全部空间，全屏展示） ===== */
  .right {
    flex: 1; min-width: 0; display: flex; flex-direction: column;
    background: var(--bg);
  }

  /* -- 对话区（上，加大占比） -- */
  .chat-zone { flex: 1.6; display: flex; flex-direction: column; min-height: 0; }
  .chat-header {
    padding: 12px 16px 10px; flex: none;
    display: flex; align-items: center; gap: 8px;
    border-bottom: 1px solid var(--border);
    font-size: 12px; color: var(--text2);
    letter-spacing: .5px; text-transform: uppercase; font-weight: 600;
  }
  .chat-header .badge {
    background: var(--surface); border: 1px solid var(--border);
    padding: 2px 9px; border-radius: 999px; font-size: 11px;
    color: var(--dim); text-transform: none; letter-spacing: 0;
  }
  .chat-log {
    flex: 1; overflow-y: auto; padding: 16px;
    display: flex; flex-direction: column; gap: 12px;
  }
  .msg {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 11px 15px; max-width: 94%;
    font-size: 14px; line-height: 1.65;
  }
  .msg.user { align-self: flex-end; border-color: rgba(76,141,255,.35);
              background: rgba(76,141,255,.09); }
  .msg.agent { align-self: flex-start; }
  .msg.sys { align-self: center; background: var(--bg3); border-color: var(--border);
             color: var(--text2); font-size: 11px; padding: 5px 12px; border-radius: 999px; }
  .msg.err { border-color: rgba(229,83,75,.45); }
  .msg .meta { font-size: 11px; color: var(--dim); margin-top: 6px;
               font-variant-numeric: tabular-nums; }
  .msg.err .meta { color: var(--err); }
  .msg .role-tag {
    display: inline-block; font-size: 11px; padding: 1px 8px;
    border-radius: 4px; background: var(--surface2);
    color: var(--accent2); margin-right: 6px; font-weight: 600;
  }
  .msg pre {
    margin: 8px 0 0; padding: 10px 12px;
    background: #0a0c10; border: 1px solid var(--border);
    border-radius: 8px; font-size: 12px; color: var(--text2);
    max-height: 220px; overflow: auto; white-space: pre-wrap;
    font-family: "Cascadia Code", Consolas, monospace;
  }

  .quick-actions {
    display: flex; gap: 7px; padding: 10px 16px 0; flex-wrap: wrap; flex: none;
  }
  .quick-actions button {
    background: var(--surface); color: var(--text2);
    border: 1px solid var(--border); border-radius: 999px;
    padding: 5px 13px; font-size: 12px; cursor: pointer;
    transition: border-color .12s, color .12s;
  }
  .quick-actions button:hover { border-color: var(--accent); color: var(--accent); }

  /* 对话输入区（加大：多行文本框 + 大按钮） */
  .chat-input {
    padding: 12px 16px 14px; flex: none;
    display: flex; flex-direction: column; gap: 8px;
    border-top: 1px solid var(--border);
    background: var(--bg3);
  }
  .chat-input textarea {
    width: 100%; min-height: 64px; max-height: 160px;
    resize: vertical;
    background: #0a0c10; color: var(--text);
    border: 1px solid var(--border); border-radius: 10px;
    padding: 11px 14px; font-size: 14px; line-height: 1.55;
    outline: none; font-family: inherit;
    transition: border-color .15s;
  }
  .chat-input textarea:focus { border-color: var(--accent); }
  .chat-input .input-row { display: flex; gap: 8px; align-items: center; }
  .chat-input select {
    background: var(--surface); color: var(--text2);
    border: 1px solid var(--border); border-radius: 9px;
    padding: 0 10px; font-size: 13px; outline: none; flex: none; cursor: pointer;
  }
  .chat-input button {
    background: var(--accent); color: #fff;
    border: none; border-radius: 10px; padding: 10px 26px;
    font-size: 14px; font-weight: 600; cursor: pointer;
    transition: background .15s; flex: none;
  }
  .chat-input button:hover { background: #3a7ae8; }
  .chat-input button:disabled { opacity: .4; cursor: not-allowed; }
  .chat-input .hint { font-size: 11px; color: var(--dim); margin-top: 2px; }

  /* -- 多模态工具行（识图/生图/生视频） -- */
  .mm-tools {
    display: flex; flex-direction: column; gap: 6px;
    padding: 0 16px 8px; flex: none;
  }
  .mm-tools button {
    background: var(--surface); color: var(--text2);
    border: 1px solid var(--border); border-radius: 999px;
    padding: 6px 14px; font-size: 12px; cursor: pointer;
    transition: border-color .12s, color .12s; flex: none;
  }
  .mm-tools button:hover { border-color: var(--accent); color: var(--accent); }
  .mm-tools button:disabled { opacity: .4; cursor: not-allowed; }
  .mm-tools button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
  .mm-tools button.primary:hover { background: #3a7ae8; }
  .mm-tools input {
    background: #0a0c10; color: var(--text);
    border: 1px solid var(--border); border-radius: 8px;
    padding: 7px 11px; font-size: 12px; outline: none; font-family: inherit;
  }
  .mm-tools input:focus { border-color: var(--accent); }
  .mm-output {
    /* 右端画面下方的结果抽屉：不挤左侧对话，高度可滚动、可折叠关闭 */
    flex: none; margin: 0; max-height: 40%;
    background: var(--surface); border-top: 1px solid var(--border2);
    overflow: hidden; display: flex; flex-direction: column;
  }
  .mm-out-head {
    display: flex; align-items: center; gap: 8px;
    padding: 6px 12px; background: var(--bg3); border-bottom: 1px solid var(--border);
    font-size: 11px; color: var(--text2);
  }
  .mm-out-head button {
    background: var(--surface); color: var(--accent2);
    border: 1px solid var(--border); border-radius: 6px;
    padding: 2px 10px; font-size: 11px; cursor: pointer; margin-left: auto;
  }
  .mm-out-head button + button { margin-left: 6px; }
  .mm-out-head button:last-child { color: var(--dim); margin-left: 0; }
  .mm-out-body {
    flex: 1; overflow: auto; padding: 10px 12px;
    font-size: 13px; line-height: 1.6; word-break: break-word;
    display: flex; align-items: center; justify-content: center;
  }
  .mm-out-body img, .mm-out-body video { max-width: 100%; max-height: 240px; border-radius: 8px; }
  .mm-out-body p { margin: 0; text-align: left; }

  /* -- 设置栏（下，可折叠） -- */
  .settings-zone {
    flex: 1; border-top: 1px solid var(--border);
    display: flex; flex-direction: column; min-height: 0;
    background: var(--bg2);
  }
  .settings-toggle {
    display: flex; align-items: center; gap: 8px;
    padding: 10px 14px; flex: none; cursor: pointer; user-select: none;
    font-size: 11px; color: var(--text2);
    letter-spacing: .5px; text-transform: uppercase; font-weight: 600;
    border-bottom: 1px solid var(--border);
  }
  .settings-toggle:hover { color: var(--text); }
  .settings-toggle .chev { margin-left: auto; transition: transform .2s; }
  .settings-zone.collapsed .chev { transform: rotate(-90deg); }
  .settings-zone.collapsed .settings-body { display: none; }
  .settings-zone.collapsed { flex: none; }
  .settings-body { flex: 1; overflow-y: auto; padding: 12px 14px; }

  .set-group { margin-bottom: 14px; }
  .set-group h4 {
    margin: 0 0 8px; font-size: 10px; font-weight: 600;
    color: var(--dim); text-transform: uppercase; letter-spacing: .8px;
  }
  .set-row {
    display: flex; align-items: center; gap: 8px; margin-bottom: 7px;
  }
  .set-row label {
    width: 96px; flex: none; font-size: 12px; color: var(--text2);
    text-align: right;
  }
  .set-row input[type=text], .set-row input[type=password], .set-row select {
    flex: 1; min-width: 0; background: #0a0c10; color: var(--text);
    border: 1px solid var(--border); border-radius: 7px;
    padding: 7px 10px; font-size: 12px; outline: none;
    transition: border-color .15s;
  }
  .set-row input:focus, .set-row select:focus { border-color: var(--accent); }
  .set-row .key-state {
    font-size: 10px; width: 52px; flex: none; text-align: center;
    padding: 2px 0; border-radius: 4px;
  }
  .key-state.set { background: rgba(53,196,122,.15); color: var(--ok); }
  .key-state.missing { background: rgba(229,83,75,.12); color: var(--err); }
  .set-note {
    font-size: 11px; color: var(--dim); margin: 4px 0 8px 96px;
    display: block;
  }
  .set-actions { display: flex; gap: 8px; margin-top: 12px; }
  .btn {
    background: var(--surface); color: var(--text);
    border: 1px solid var(--border); border-radius: 8px;
    padding: 8px 16px; font-size: 12px; cursor: pointer;
    transition: border-color .12s, color .12s;
  }
  .btn:hover { border-color: var(--accent); color: var(--accent); }
  .btn.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
  .btn.primary:hover { background: #3a7ae8; }
  .btn.saved { background: rgba(53,196,122,.15); color: var(--ok); border-color: var(--ok); }

  /* ===== 右端：大浏览器（全屏展示模式，画面撑满整个区域） ===== */
  .browser-bar {
    display: flex; align-items: center; gap: 8px;
    padding: 10px 16px; background: var(--bg3);
    border-bottom: 1px solid var(--border); flex: none;
  }
  .browser-bar input {
    flex: 1; background: #0a0c10; color: var(--text);
    border: 1px solid var(--border); border-radius: 8px;
    padding: 9px 14px; font-size: 14px; outline: none;
    transition: border-color .15s;
  }
  .browser-bar input:focus { border-color: var(--accent); }
  .browser-bar button {
    background: var(--surface); color: var(--text);
    border: 1px solid var(--border); border-radius: 8px;
    padding: 8px 15px; font-size: 13px; cursor: pointer;
    transition: border-color .12s, color .12s; flex: none;
  }
  .browser-bar button:hover { border-color: var(--accent); color: var(--accent); }
  .browser-bar button.active { background: rgba(76,141,255,.15);
                                border-color: var(--accent); color: var(--accent); }

  /* 浏览器展示区撑满右侧全部空间；
     画面尽量铺满整个区域（最大化显示），像聊天窗口一样内容是主体、清晰可读——
     canvas 渲染分辨率随舞台自适应（JS 同步），画面按原比例 contain 完整不裁切，
     竖屏画面在宽舞台上横向居中放大到最大，字尽量大 */
  .browser-stage {
    flex: 1; position: relative; background: #080a0d; overflow: hidden;
  }
  .browser-stage canvas {
    position: absolute; top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    /* 铺满整个舞台（最大化），JS 同步渲染分辨率保证清晰 */
    width: calc(100% - 8px);
    height: calc(100% - 8px);
    object-fit: contain;        /* 画面按原比例完整显示、不裁切、不变形 */
    background: #080a0d;
  }
  /* 画面加载中提示（顶部小条，不挡画面） */
  .browser-stage .loading-bar {
    position: absolute; top: 8px; left: 50%;
    transform: translateX(-50%);
    background: rgba(10,12,16,.85); border: 1px solid var(--border);
    border-radius: 999px; padding: 4px 14px;
    font-size: 11px; color: var(--text2);
    z-index: 3; display: none; backdrop-filter: blur(4px);
  }
  .browser-stage .loading-bar.show { display: block; }
  .browser-stage .placeholder {
    position: absolute; top: 50%; left: 50%;
    transform: translate(-50%,-50%);
    color: var(--dim); font-size: 14px; text-align: center;
    pointer-events: none; user-select: none; line-height: 2.2;
  }
  .browser-stage .placeholder .hint { font-size: 12px; color: var(--dim); }

  /* ===== 底部状态栏 ===== */
  footer {
    display: flex; align-items: center; gap: 16px;
    padding: 0 16px; height: 28px; flex: none;
    background: var(--bg2); border-top: 1px solid var(--border);
    font-size: 11px; color: var(--dim);
  }
  footer .kv { color: var(--text2); }
  footer .kv b { color: var(--text); font-weight: 600; font-variant-numeric: tabular-nums; }
  footer .spacer { flex: 1; }
  footer .url { color: var(--accent2); max-width: 360px;
                overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  /* 滚动条 */
  ::-webkit-scrollbar { width: 8px; height: 8px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--border2); }

  /* 响应式 */
  @media (max-width: 1100px) {
    .left { width: 46%; min-width: 340px; }
  }
</style>
</head>
<body>

<!-- ===== 顶栏 ===== -->
<header>
  <div class="logo"></div>
  <span class="title">Hermes-SuperAgent</span>
  <span class="ver">v0.3 · 双端工作台</span>
  <div class="spacer"></div>
  <div class="pill off" id="sBrowser"><span class="dot"></span>浏览器</div>
  <div class="pill off" id="sAgent"><span class="dot"></span>智能体</div>
</header>

<main>
  <!-- ===== 左端：对话 + 设置 ===== -->
  <section class="left">

    <!-- 对话区（上） -->
    <div class="chat-zone">
      <div class="chat-header">Agent 对话 <span class="badge" id="chatCount">0</span></div>
      <div class="chat-log" id="chatLog">
        <div class="msg agent"><span class="role-tag">系统</span>智能体已就绪。输入任务，右侧内嵌浏览器实时执行并显示画面。<br>任务会走完整管道：路由 → 角色 → 浏览器动作 → 结果/成本。</div>
      </div>
      <div class="quick-actions" id="quickActions">
        <button data-q="抓取 https://example.com 并总结页面内容" data-class="crawl">抓取示例页</button>
        <button data-q="生成一个 Python 脚本：监控文件夹新增文件并记录日志" data-class="code">生成监控脚本</button>
        <button data-q="校验以下代码可运行：print('hello')，输出预期 hello" data-class="verify">校验代码</button>
        <button data-q="汇总本轮所有任务结果成一份报告" data-class="report">生成报告</button>
      </div>
      <!-- 快捷识图（生图/生视频走对话意图，按钮已移除） -->
      <div class="mm-tools">
        <button id="mmVision" title="把右侧画面截图交给文本模型识别">👁 识图（文本模型看右侧画面）</button>
        <span style="font-size:11px;color:var(--dim)">生图/生视频：左侧对话里说「画一张…」「做个…视频」即可</span>
      </div>
      <div class="chat-input">
        <textarea id="chatInput" rows="2" placeholder="和智能体说话：写代码、查资料、或输入网址让它帮你打开（如：打开 example.com）" autofocus></textarea>
        <div class="input-row">
          <select id="taskClass" title="任务类（智能体会自动判断，也可手动指定）">
            <option value="auto">智能判断（推荐）</option>
            <option value="crawl">抓取</option>
            <option value="code">代码</option>
            <option value="verify">校验</option>
            <option value="report">报告</option>
            <option value="research">研究</option>
          </select>
          <button id="chatSend">发送</button>
        </div>
        <div class="hint">💬 直接说「打开 xxx.com」→ 智能体驱动右侧内置浏览器；说「写个 xxx」→ 调度五角色团队</div>
      </div>
    </div>

    <!-- 设置栏（下，可折叠） -->
    <div class="settings-zone" id="settingsZone">
      <div class="settings-toggle" id="settingsToggle">
        ⚙ 设置 <span style="text-transform:none;letter-spacing:0;font-weight:400;color:var(--dim)">密钥 · 模型 · 通用</span>
        <span class="chev">▾</span>
      </div>
      <div class="settings-body" id="settingsBody">

        <div class="set-group">
          <h4>密钥配置（secrets.json · 可自由更换）</h4>
          <div class="set-row">
            <label>agnes key</label>
            <input type="password" id="kAgnes" placeholder="sk-...">
            <span class="key-state" id="ksAgnes">…</span>
          </div>
          <div class="set-row">
            <label>agnes 端点</label>
            <input type="text" id="kAgnesUrl" placeholder="https://apihub.agnes-ai.cn/v1">
          </div>
          <div class="set-row">
            <label>openai key</label>
            <input type="password" id="kOpenai" placeholder="sk-...">
          </div>
          <div class="set-row">
            <label>deepseek key</label>
            <input type="password" id="kDeepseek" placeholder="sk-...">
          </div>
          <div class="set-note">⚠ 更换密钥/端点直接改上面输入框 → 点「保存设置」即可生效（写 secrets.json，重启后保留）。</div>
        </div>

        <div class="set-group">
          <h4>多模态模型（可更换）</h4>
          <div class="set-row">
            <label>生图模型</label>
            <input type="text" id="mImage" placeholder="agnes-image-2.5-flash">
          </div>
          <div class="set-row">
            <label>生视频模型</label>
            <input type="text" id="mVideo" placeholder="agnes-video-2.5-flash">
          </div>
          <div class="set-note">识图走文本模型视觉能力（当前 agnes-3.0-flash）；生图/生视频走上面两个模型。留空 = 用内置默认。</div>
        </div>

        <div class="set-group">
          <h4>模型路由</h4>
          <div class="set-row">
            <label>主模型</label>
            <select id="mPrimary">
              <option value="agnes-3.0-flash">agnes-3.0-flash（OpenAI 兼容）</option>
              <option value="agnes-2.5-flash">agnes-2.5-flash</option>
              <option value="deepseek-chat">deepseek-chat</option>
              <option value="gpt-4o">gpt-4o（OpenAI）</option>
              <option value="anthropic/claude">claude（Anthropic）</option>
            </select>
          </div>
          <div class="set-row">
            <label>备用模型</label>
            <select id="mBackup">
              <option value="agnes-3.0-flash">agnes-3.0-flash</option>
              <option value="agnes-2.5-flash">agnes-2.5-flash</option>
              <option value="deepseek-chat">deepseek-chat</option>
              <option value="gpt-4o">gpt-4o</option>
            </select>
          </div>
          <div class="set-note">429 限流时自动降级到备用路由（K1 设计意图）</div>
        </div>

        <div class="set-group">
          <h4>通用</h4>
          <div class="set-row">
            <label>浏览器模式</label>
            <select id="gBrowserMode">
              <option value="builtin">内置 headless Chromium</option>
              <option value="cdp">外接 CDP（需授权）</option>
            </select>
          </div>
          <div class="set-row">
            <label>帧率</label>
            <select id="gFps">
              <option value="4">4 fps（默认）</option>
              <option value="8">8 fps</option>
              <option value="15">15 fps（上限）</option>
            </select>
          </div>
          <div class="set-row">
            <label>视口</label>
            <select id="gViewport">
              <option value="1440x900" selected>1440×900 大屏桌面（默认）</option>
              <option value="1920x1080">1920×1080 全高清</option>
              <option value="1280x800">1280×800 桌面</option>
              <option value="360x640">360×640 竖屏（拇指，窄屏设备）</option>
            </select>
          </div>
          <div class="set-row">
            <label>清晰度</label>
            <select id="gQuality">
              <option value="75">标准（75，省流量）</option>
              <option value="90">清晰（90）</option>
              <option value="95" selected>高清（95，默认）</option>
              <option value="100">无损（100，最锐利）</option>
            </select>
          </div>
          <div class="set-note">改清晰度/帧率后点「暂停」再「恢复」会重连帧流生效；或刷新页面。</div>
        </div>

        <div class="set-actions">
          <button class="btn primary" id="btnSaveSettings">保存设置</button>
          <button class="btn" id="btnReloadSettings">重新加载</button>
        </div>
      </div>
    </div>

  </section>

  <!-- ===== 可拖拽中线（拉大拉小两栏） ===== -->
  <div class="divider" id="divider" title="拖拽调整左右宽度"></div>

  <!-- ===== 右端：大浏览器（全屏展示） ===== -->
  <section class="right">
    <div class="browser-bar">
      <input id="urlInput" placeholder="https://example.com" value="about:blank">
      <button id="btnGoto">跳转</button>
      <button id="btnPause">暂停</button>
      <button id="btnShot">截图</button>
      <button id="btnPick">点选</button>
    </div>
    <div class="browser-stage">
      <div class="loading-bar" id="loadBar">页面跳转中…</div>
      <canvas id="cv" width="1440" height="900"></canvas>
      <div class="placeholder" id="ph">等待浏览器实例启动…<br><span class="hint">实例懒启动，首帧到达后自动显示画面</span></div>
    </div>
    <!-- 多模态结果展示区（识图/生图/生视频 的输出，放画面下方不挤左侧对话） -->
    <div class="mm-output" id="mmOutput" style="display:none">
      <div class="mm-out-head">
        <span id="mmOutLabel">结果</span>
        <button id="mmOutDownload" style="display:none">下载</button>
        <button id="mmOutClose" style="margin-left:6px;color:var(--dim)">✕</button>
      </div>
      <div class="mm-out-body" id="mmOutBody"></div>
    </div>
  </section>
</main>

<!-- ===== 底部状态栏 ===== -->
<footer>
  <span class="kv">fps <b id="fFps">4</b></span>
  <span class="kv">帧 <b id="fFrames">0</b></span>
  <span class="kv">实例 <b id="fInst">-</b></span>
  <span class="kv">模型 <b id="fModel">agnes-3.0-flash</b></span>
  <div class="spacer"></div>
  <span class="url" id="fUrl">about:blank</span>
</footer>

<script>
'use strict';
const $ = (id) => document.getElementById(id);
const cv = $('cv');
const ctx2d = cv.getContext('2d');
let frameCount = 0, lastSeq = 0, pickMode = false, paused = false;

/* ===== 可拖拽中线（两栏宽度 auto 拉大拉小） ===== */
(function initDivider() {
  const divider = $('divider');
  const left = document.querySelector('.left');
  const main = document.querySelector('main');
  if (!divider || !left || !main) return;

  let dragging = false;
  divider.addEventListener('mousedown', (e) => {
    e.preventDefault();
    dragging = true;
    divider.classList.add('dragging');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  });
  document.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const rect = main.getBoundingClientRect();
    // 左栏宽度 = 鼠标相对 main 左边缘的偏移（设上下限）
    let w = e.clientX - rect.left;
    const minW = Math.round(rect.width * 0.22);
    const maxW = Math.round(rect.width * 0.72);
    w = Math.max(minW, Math.min(maxW, w));
    left.style.width = w + 'px';
  });
  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    divider.classList.remove('dragging');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
  });
})();

/* ===== 对话区 ===== */
function addMsg(cls, text, meta, html) {
  const el = document.createElement('div');
  el.className = 'msg ' + cls;
  if (html) el.innerHTML = text;
  else el.innerHTML = '<div>' + text.replace(/</g,'&lt;').replace(/>/g,'&gt;') + '</div>';
  if (meta) {
    const m = document.createElement('div');
    m.className = 'meta'; m.textContent = meta;
    el.appendChild(m);
  }
  $('chatLog').appendChild(el);
  $('chatLog').scrollTop = $('chatLog').scrollHeight;
  $('chatCount').textContent = String($('chatLog').children.length - 1);
  return el;
}

function escapeHtml(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

/* ===== 左侧输入框 → 内置浏览器 联动 =====
   智能体对话（主脑）+ 自动识别：纯网址 / "打开 xxx" 直接驱动右侧浏览器；
   其余走 LLM 自主判断意图（browse/task/none）。 */
function normalizeTaskClass(v) {
  // 老版 default 统一映射到 auto（智能判断）
  return (v === 'default') ? 'auto' : v;
}

async function agentChat(message) {
  addMsg('user', message, '');
  $('sAgent').className = 'pill wait';
  $('chatSend').disabled = true;

  try {
    const r = await fetch('/chat', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({message})
    });
    const d = await r.json();
    if (d.ok && d.data) {
      const res = d.data;
      addMsg('agent', res.reply, 'model=' + (res.model||'') + ' · ' + (res.latency_ms!=null?res.latency_ms+'ms':'') + ' · 记忆 ' + (res.history_len||0) + ' 轮');

      // 智能体自主意图 → 驱动手脚（浏览器 / 生图 / 生视频 / 识图）
      if (res.intent === 'browse' && res.browse_url) {
        addMsg('sys', '🔍 智能体驱动内置浏览器 → ' + res.browse_url, '意图=browse，自动同步右侧画面');
        goto(res.browse_url);
      } else if (res.intent === 'task' && res.task_class) {
        addMsg('sys', '⚙ 智能体调度团队任务 → ' + res.task_class, '意图=task，交给五角色管道');
        runAgentTask(message, res.task_class);
      } else if (res.intent === 'image' && res.image_prompt) {
        // 智能体决定生图 → 调生图模型，结果展示在右侧画面下方抽屉
        addMsg('sys', '🎨 智能体决定生图 → "' + res.image_prompt + '"', '意图=image，调生图模型');
        runImageGen(res.image_prompt);
      } else if (res.intent === 'video' && res.video_prompt) {
        addMsg('sys', '🎬 智能体决定生视频 → "' + res.video_prompt + '"', '意图=video，调生视频模型（异步）');
        runVideoGen(res.video_prompt);
      } else if (res.intent === 'vision' && res.vision_question) {
        addMsg('sys', '👁 智能体决定识图 → "' + res.vision_question + '"', '意图=vision，调文本模型视觉');
        runVision(res.vision_question);
      }
      // intent === 'none'：纯对话，无动作
      $('sAgent').className = 'pill on';
    } else {
      addMsg('err','智能体故障：' + escapeHtml(d.error || JSON.stringify(d)), '');
      $('sAgent').className = 'pill off';
    }
  } catch(e) {
    addMsg('err','对话通道不可达：' + e.message, '/chat 故障');
    $('sAgent').className = 'pill off';
  } finally {
    $('chatSend').disabled = false;
  }
}

/* 左侧输入框发送：
   ① 纯网址（含 http/https 或以 www 开头）→ 直接驱动内置浏览器跳转
   ② 「打开/访问/看 xxx」开头 + 能抽出 url → 直接跳转
   ③ 其余 → 交给 LLM 智能判断（taskClass=auto 时） */
function extractUrl(text) {
  const m = text.match(/https?:\/\/[^\s"']+/i) ||
            text.match(/^(?:open|打开|访问|看|查看|抓取)\s*(?:https?:\/\/)?((?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:\/[^\s]*)?)/i);
  if (!m) return null;
  const raw = m[0].startsWith('http') ? m[0] : (m[1] || m[0]);
  let url = raw.trim().replace(/[.,;。！!？?]+$/, '');
  if (!/^https?:\/\//i.test(url)) url = 'https://' + url;
  return url;
}

$('chatSend').onclick = () => {
  const v = $('chatInput').value.trim();
  if (!v) return;
  $('chatInput').value = '';
  const tc = normalizeTaskClass($('taskClass').value);

  // 规则 ①/②：纯网址或「打开 xxx」→ 直接驱动内置浏览器（即时、零 LLM 延迟）
  if (tc !== 'auto') {
    // 手动指定了任务类 → 走对应管道
    runAgentTask(v, tc);
    return;
  }
  const bareUrl = extractUrl(v);
  const looksLikeOpen = /^(open|打开|访问|看|查看|抓取|goto)\s/i.test(v) || /^https?:\/\//i.test(v) || /^www\./.test(v);
  if (bareUrl && looksLikeOpen) {
    addMsg('sys', '🔗 识别为网址，驱动内置浏览器 → ' + bareUrl, '左侧输入框 联动 右侧画面');
    $('urlInput').value = bareUrl;
    goto(bareUrl);
    return;
  }
  // 规则 ③：交给 LLM 智能判断
  agentChat(v);
};
$('chatInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('chatSend').click(); }
});
document.querySelectorAll('#quickActions button').forEach(b => {
  b.addEventListener('click', () => {
    $('taskClass').value = b.dataset.class;
    runAgentTask(b.dataset.q, b.dataset.class);
  });
});

/* ===== 结构化任务管道（手脚的升级动作：/collab，五角色 CrewAI）===== */
async function runAgentTask(goal, taskClass) {
  const ROUTE = {crawl:'crawler', code:'coder', verify:'verifier',
                  report:'reporter', research:'supervisor', default:'supervisor'};
  const target = ROUTE[taskClass] || 'supervisor';
  addMsg('sys', '调度 → ' + target + ' 角色（任务类=' + taskClass + '）','走完整 CrewAI 管道');

  try {
    const r = await fetch('/collab', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({goal, task_class: taskClass})
    });
    const d = await r.json();
    if (d.ok && d.data) {
      const res = d.data;
      const elapsed = res.elapsed_ms != null ? (res.elapsed_ms/1000).toFixed(2)+'s' : '-';
      if (res.ok === false) {
        addMsg('err','任务失败：' + escapeHtml(res.error || '未知'), 'role=' + (res.role||target) + ' · ' + elapsed);
      } else {
        addMsg('agent', '✅ ' + (res.role||target) + ' 执行完成',
          elapsed + (res.cost_usd!=null?' · $'+res.cost_usd.toFixed(4):'') + ' · ' + (res.trace_id||''));
        const pre = document.createElement('pre');
        pre.textContent = (res.output||JSON.stringify(res)).slice(0,1500);
        $('chatLog').lastChild.appendChild(pre);
        if (res.browser_navigated) goto(res.browser_navigated);
      }
    } else {
      addMsg('err','管道错误：' + escapeHtml(d.error||''), '');
    }
  } catch(e) {
    addMsg('err','管道不可达：' + e.message, '');
  }
}

/* ===== 内嵌浏览器 ===== */
function setPill(id, cls) { $(id).className = 'pill ' + cls; }

/* 画面像素 → 内嵌视口坐标换算（画面在 canvas 里 contain 等比居中显示，
   需还原出真实 CSS 渲染区域再映射到 1440×900 视口）。
   返回 {x,y} 视口坐标；滚轮用画面中心 y 作锚点。 */
const VIEW_W = 1440, VIEW_H = 900;  // 与默认视口一致
function frameToViewport(px, py) {
  // px,py 是 CSS 像素坐标（鼠标事件给的是 CSS 坐标）；canvas 渲染分辨率是物理像素（×dpr），
  // 但映射到"帧坐标"只看 CSS 显示尺寸（画面在 canvas 里 contain 居中，CSS 尺寸即视觉尺寸）
  const cw = cv.clientWidth || cv.width;
  const ch = cv.clientHeight || cv.height;
  // 帧的原始尺寸（最近一帧的 width/height）
  const fw = lastFrameW || VIEW_W, fh = lastFrameH || VIEW_H;
  // contain 等比缩放后的渲染尺寸（画面在 canvas 内居中）
  const s = Math.min(cw / fw, ch / fh);
  const dw = fw * s, dh = fh * s;
  const ox = (cw - dw) / 2, oy = (ch - dh) / 2;  // 画面左上角在 canvas 里的偏移
  // px,py 是 canvas 元素坐标（已减去 canvas 左上）
  const fx = (px - ox) / s;  // → 帧坐标
  const fy = (py - oy) / s;
  // 帧坐标 → 视口坐标（帧 = 视口 1:1，帧宽=视口宽）
  return {
    x: Math.max(0, Math.min(VIEW_W - 1, fx * VIEW_W / fw)),
    y: Math.max(0, Math.min(VIEW_H - 1, fy * VIEW_H / fh)),
  };
}
let lastFrameW = VIEW_W, lastFrameH = VIEW_H;  // 最近帧尺寸（startStream 里更新）

async function mouseOp(op, x, y, deltaY) {
  try {
    const s = await (await fetch('/thumb/status')).json();
    if (s.booting) return;
    const inst = (s.data && s.data.instances && s.data.instances[0]) || {};
    if (!inst.instance_id) return;
    await fetch('/thumb/mouse', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({instance: inst.instance_id, op, x, y, delta_y: deltaY})
    });
  } catch(e) { /* 交互桥不可达不阻断画面 */ }
}

async function refreshStatus() {
  try {
    const s = await (await fetch('/thumb/status')).json();
    if (s.booting) { setPill('sBrowser','wait'); $('ph').innerHTML = '浏览器实例启动中…<br><span class="hint">懒启动，稍候首帧</span>'; return; }
    const inst = (s.data && s.data.instances && s.data.instances[0]) || {};
    $('fInst').textContent = inst.instance_id || '-';
  } catch(e) { setPill('sBrowser','off'); }
}

async function goto(url) {
  // 跳转中：顶部小条提示（不挡画面），地址栏先更新目标 URL
  $('urlInput').value = url;
  const loadBar = $('loadBar');
  loadBar.textContent = '跳转 → ' + url;
  loadBar.classList.add('show');
  setPill('sBrowser','wait');
  try {
    const s = await (await fetch('/thumb/status')).json();
    if (s.booting) { addMsg('err','浏览器实例还没就绪，稍候再试',''); return; }
    const inst = (s.data && s.data.instances && s.data.instances[0]) || {};
    const iid = inst.instance_id;
    if (!iid) { addMsg('err','无可用浏览器实例', s.error||''); return; }
    const g = await (await fetch('/thumb/goto?instance_id=' + encodeURIComponent(iid), {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({url})
    })).json();
    if (g.ok) {
      const finalUrl = g.data?.url || url;
      $('urlInput').value = finalUrl;       // 地址栏同步最终 URL
      $('fUrl').textContent = finalUrl;      // 底部状态栏同步
      loadBar.textContent = '✓ ' + finalUrl;
      addMsg('sys','浏览器已跳转 → ' + finalUrl, 'instance=' + iid);
      setPill('sBrowser','on');
      setTimeout(() => loadBar.classList.remove('show'), 1800);
    } else {
      addMsg('err','跳转失败：' + (g.error||''), g.trace_id||'');
      loadBar.classList.remove('show');
    }
  } catch(e) {
    addMsg('err','浏览器通道：' + e.message, '');
    loadBar.classList.remove('show');
    setPill('sBrowser','off');
  }
}

function startStream() {
  setPill('sBrowser','wait');
  const fps = $('gFps').value || '4';
  // 清晰度：随面板「清晰度」选择（q 1-100）+ 按设备像素比提高帧宽
  const quality = $('gQuality').value || '95';
  const dpr = window.devicePixelRatio || 1;
  // 大屏帧宽：随视口 1440（不再写死 360）；高 DPI 屏幕按 dpr 提宽至 1920 上限，
  // 与 canvas 物理分辨率（×dpr）匹配，画面锐利接近原网页渲染
  const frameW = Math.min(1920, Math.round(1440 * Math.min(dpr, 1.5)));
  lastSeq = 0;
  const es = new EventSource('/thumb/frames?fps=' + fps + '&max_width=' + frameW + '&quality=' + quality);
  es.onmessage = (ev) => {
    let m;
    try { m = JSON.parse(ev.data); } catch(e) { return; }
    if (m.type === 'hello') {
      $('fInst').textContent = m.instance || '-';
      setPill('sBrowser','on');
      return;
    }
    if (m.type === 'error') {
      addMsg('err','帧流中断：' + m.error, m.instance||'');
      setPill('sBrowser','off');
      es.close();
      setTimeout(startStream, 2500);
      return;
    }
    if (m.type !== 'frame' || paused) return;
    // 乱序保护：只丢弃比当前旧一帧的；新连接首帧 seq=1 必收
    if (lastSeq && m.seq < lastSeq) return;
    lastSeq = m.seq;
    frameCount++;
    // 帧尺寸（画面 contain 缩放 + 画面坐标→视口坐标映射用）
    if (m.width && m.height) { lastFrameW = m.width; lastFrameH = m.height; }
    // 首次出帧提示画面可交互（滚轮/点击已桥接）
    if (!window.__frameHintShown) { window.__frameHintShown = true;
      addMsg('sys','💡 右侧画面可直接交互：滚轮滚动页面 / 点击元素','鼠标事件已桥接到内嵌浏览器');
    }
    $('fFrames').textContent = String(frameCount);
    if (m.url && m.url !== 'about:blank') {
      $('fUrl').textContent = m.url;
      // 画面 URL 变化时同步地址栏（不覆盖用户正在输入的跳转目标）
      if (document.activeElement !== $('urlInput')) $('urlInput').value = m.url;
    }
    // 首帧到达才隐藏占位提示（之前 hello 就隐藏，但首帧失败会一直空白）
    $('ph').style.display = 'none';
    const img = new Image();
    img.onload = () => {
      // 同步 canvas 渲染分辨率到【物理像素】= CSS 尺寸 × devicePixelRatio（高 DPI 屏发糊根因）。
      // 只在尺寸真正变化时改 width/height（避免每帧清空画布）。
      // dpr=2 时 canvas 物理分辨率翻倍，画面锐利如原网页渲染；dpr=1 时行为不变。
      const dpr = window.devicePixelRatio || 1;
      const rw = cv.clientWidth, rh = cv.clientHeight;
      const bw = Math.round(rw * dpr), bh = Math.round(rh * dpr);  // 物理像素
      if (rw && rh && (cv.width !== bw || cv.height !== bh)) {
        cv.width = bw; cv.height = bh;
      }
      // 绘制坐标系用物理像素（ctx 已随 canvas.width 自动按物理像素建）
      ctx2d.clearRect(0, 0, cv.width, cv.height);
      // 按帧原比例 contain 绘制到画布（画面完整、不裁切、不变形，宽舞台横向居中放大）
      const iw = img.width, ih = img.height;
      if (!iw || !ih) return;
      const s = Math.min(cv.width / iw, cv.height / ih);
      const dw = Math.round(iw * s), dh = Math.round(ih * s);
      const ox = (cv.width - dw) >> 1, oy = (cv.height - dh) >> 1;
      ctx2d.drawImage(img, ox, oy, dw, dh);
    };
    img.onerror = () => {
      // base64 解码失败（帧过大/损坏）显式告警，不静默
      addMsg('err','帧解码失败（第 ' + frameCount + ' 帧）','检查帧流完整性');
    };
    img.src = 'data:image/' + (m.format||'jpeg') + ';base64,' + m.image_base64;
  };
  es.onerror = () => {
    setPill('sBrowser','off');
    es.close();
    setTimeout(startStream, 2500);
  };
}

$('btnGoto').onclick = () => {
  const u = $('urlInput').value.trim();
  if (u) goto(u);
};
$('urlInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') $('btnGoto').click();
});

$('btnPause').onclick = (ev) => {
  paused = !paused;
  ev.currentTarget.classList.toggle('active', paused);
  ev.currentTarget.textContent = paused ? '恢复' : '暂停';
  // 恢复时重连帧流 → 重新读取「清晰度/帧率/视口」设置（改动即时生效）
  if (!paused) startStream();
};
/* 清晰度选择变更：直接重连帧流生效（不必点暂停/恢复） */
if (typeof $('gQuality') !== 'undefined') {
  $('gQuality').onchange = () => startStream();
}

$('btnShot').onclick = async () => {
  try {
    const s = await (await fetch('/thumb/status')).json();
    if (s.booting) { addMsg('err','实例未就绪',''); return; }
    const inst = (s.data && s.data.instances && s.data.instances[0]) || {};
    const iid = inst.instance_id;
    if (!iid) { addMsg('err','无实例',''); return; }
    const r = await (await fetch('/thumb/frame?instance_id=' + encodeURIComponent(iid))).json();
    if (r.ok && r.data && r.data.data_base64) {
      const a = document.createElement('a');
      a.href = 'data:image/' + (r.data.format||'jpeg') + ';base64,' + r.data.data_base64;
      a.download = 'hermes-shot.jpg'; a.click();
      addMsg('sys','截图已保存存证','');
    } else addMsg('err','截图失败：'+(r.error||''), '');
  } catch(e) { addMsg('err','截图失败：'+e.message,''); }
};

$('btnPick').onclick = (ev) => {
  pickMode = !pickMode;
  ev.currentTarget.classList.toggle('active', pickMode);
  addMsg('sys', pickMode ? '点选模式开：点击画面读坐标' : '点选模式关','');
};

/* ===== 画面直接交互：滚轮滚动 / 鼠标点击 → 内嵌 Chromium =====
   在 canvas 上滚轮 → 桥接 page.mouse.wheel（页面滚动）；
   在 canvas 上点击 → 桥接 page.mouse.click（点元素）。
   坐标经 frameToViewport 换算成视口坐标。 */
cv.addEventListener('wheel', (e) => {
  e.preventDefault();
  // 滚轮在画面中心附近滚动最自然：用鼠标相对 canvas 位置
  const r = cv.getBoundingClientRect();
  const cx = e.clientX - r.left, cy = e.clientY - r.top;
  const v = frameToViewport(cx, cy);
  mouseOp('wheel', v.x, v.y, e.deltaY > 0 ? 120 : -120);
}, { passive: false });

cv.addEventListener('click', (e) => {
  if (pickMode) {
    // 旧点选模式：读坐标
    const r = cv.getBoundingClientRect();
    const x = Math.round((e.clientX - r.left) / r.width * cv.width);
    const y = Math.round((e.clientY - r.top) / r.height * cv.height);
    addMsg('sys', '点选坐标 (' + x + ', ' + y + ') · 写操作经内核 capability 上行','');
    return;
  }
  // 直接点击：桥接到内嵌浏览器（点元素 / 触发链接）
  const r = cv.getBoundingClientRect();
  const cx = e.clientX - r.left, cy = e.clientY - r.top;
  const v = frameToViewport(cx, cy);
  mouseOp('click', v.x, v.y, 0);
});

cv.addEventListener('dblclick', (e) => {
  const r = cv.getBoundingClientRect();
  const cx = e.clientX - r.left, cy = e.clientY - r.top;
  const v = frameToViewport(cx, cy);
  mouseOp('dblclick', v.x, v.y, 0);
});

cv.addEventListener('contextmenu', (e) => {
  e.preventDefault();
  const r = cv.getBoundingClientRect();
  const cx = e.clientX - r.left, cy = e.clientY - r.top;
  const v = frameToViewport(cx, cy);
  mouseOp('rightclick', v.x, v.y, 0);
});

/* ===== 多模态工具：识图 / 生图 / 生视频 ===== */
let lastShotB64 = '';  // 最近一帧的 base64（供识图取图）

function showMM(label, html, downloadHref) {
  const out = $('mmOutput');
  out.style.display = 'flex';
  $('mmOutLabel').textContent = label;
  const body = $('mmOutBody');
  body.innerHTML = html;
  const dl = $('mmOutDownload');
  if (downloadHref) { dl.href = downloadHref; dl.style.display = 'inline-block'; }
  else dl.style.display = 'none';
}

// 帧流里顺手留底最近一帧（识图用）
const _origImgOnload = null;
function grabFrameB64() {
  // 从最近渲染的 canvas 取当前画面（更直接：对 canvas 做 toDataURL）
  try {
    return cv.toDataURL('image/jpeg', 0.85).split(',')[1];
  } catch (e) { return ''; }
}

/* 识图：把右侧画面截图交给文本模型视觉能力（按钮 + 对话意图共用） */
async function runVision(question) {
  const b64 = grabFrameB64();
  if (!b64) { addMsg('err','右侧画面还没出帧，无法识图','先跳转一个页面再试'); return; }
  lastShotB64 = b64;
  if ($('mmVision')) $('mmVision').disabled = true;
  const q = question || '画面里有什么?用两三句中文描述。';
  showMM('👁 文本模型识别中…', '<p style="color:var(--dim)">正在把画面交给视觉模型（' + ($('mPrimary').value || 'agnes-3.0-flash') + '）…</p>');
  try {
    const r = await (await fetch('/vision', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({image_b64: b64, question: q})
    })).json();
    if (r.ok) {
      showMM('👁 识图结果（' + (r.data.model||'') + ' · ' + (r.data.latency_ms!=null?r.data.latency_ms+'ms':'') + ')',
             '<p>' + escapeHtml(r.data.answer) + '</p>');
      addMsg('agent', '👁 ' + r.data.answer, 'vision · ' + (r.data.model||''));
    } else {
      showMM('识图失败', '<p style="color:var(--err)">' + escapeHtml(r.error||'') + '</p>');
      addMsg('err','识图失败：'+escapeHtml(r.error||''), '');
    }
  } catch(e) {
    showMM('识图通道不可达', '<p style="color:var(--err)">'+e.message+'</p>');
    addMsg('err','识图通道：'+e.message,'');
  } finally { if ($('mmVision')) $('mmVision').disabled = false; }
}
$('mmVision').onclick = () => runVision();

/* 生图：生图模型出图（由对话意图调用，结果展示在右侧画面下方抽屉） */
async function runImageGen(prompt) {
  if (!prompt) { addMsg('err','生图需要先有描述',''); return; }
  showMM('🎨 生图中…', '<p style="color:var(--dim)">交 ' + ($('mImage').value||'agnes-image-2.5-flash') + ' 出图（约 10–30s）…</p>');
  try {
    const r = await (await fetch('/image', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({prompt})
    })).json();
    if (r.ok && r.data && r.data.url) {
      showMM('🎨 生成完成（' + (r.data.model||'') + '）',
             '<img src="' + r.data.url + '" alt="generated">', r.data.url);
      addMsg('agent','🎨 图片已生成（' + prompt + '）','url=' + r.data.url);
    } else {
      showMM('生图失败', '<p style="color:var(--err)">'+escapeHtml((r.error||'')||'未知')+'</p>');
      addMsg('err','生图失败：'+escapeHtml((r.error||'')||''), '');
    }
  } catch(e) {
    showMM('生图通道不可达', '<p style="color:var(--err)">'+e.message+'</p>');
    addMsg('err','生图通道：'+e.message,'');
  }
}

/* 生视频：提交异步任务 + 轮询（由对话意图调用，结果展示在右侧抽屉） */
async function runVideoGen(prompt) {
  if (!prompt) { addMsg('err','生视频需要先有描述',''); return; }
  showMM('🎬 视频任务提交中…', '<p style="color:var(--dim)">交 ' + ($('mVideo').value||'agnes-video-2.5-flash') + ' 排队生成…</p>');
  try {
    const r = await (await fetch('/video', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({prompt})
    })).json();
    if (!r.ok || !r.data || !r.data.task_id) {
      showMM('生视频失败', '<p style="color:var(--err)">'+escapeHtml((r.error||'')||'')+'</p>');
      addMsg('err','生视频提交失败：'+escapeHtml((r.error||'')||''),'');
      return;
    }
    const taskId = r.data.task_id;
    addMsg('sys','🎬 视频任务已提交 → '+taskId,'轮询状态中');
    let tries = 0;
    const tick = async () => {
      if (tries++ > 30) {
        addMsg('err','视频生成超时（>3min）','任务号 '+taskId+' 可稍后手动查 /video/status'); return; }
      try {
        const s = await (await fetch('/video/status?task_id='+encodeURIComponent(taskId))).json();
        if (s.ok && s.data) {
          const st = (s.data.status||'').toLowerCase();
          if (st === 'completed' || st === 'succeeded' || st === 'done') {
            const url = s.data.url || '';
            showMM('🎬 视频生成完成', '<video controls src="'+url+'"></video>', url);
            addMsg('agent','🎬 视频已生成（' + prompt + '）','url=' + url);
            return;
          }
          if (st === 'failed' || st === 'error') {
            showMM('🎬 视频生成失败', '<p style="color:var(--err)">'+escapeHtml(JSON.stringify(s.data.raw||s.data))+'</p>');
            addMsg('err','视频生成失败','task '+taskId);
            return;
          }
          showMM('🎬 视频生成中（'+s.data.status+'）…',
                 '<p style="color:var(--dim)">task '+taskId+'，第 '+tries+' 次轮询…</p>');
        }
      } catch(e) { /* 单次轮询失败不中断 */ }
      setTimeout(tick, 6000);
    };
    setTimeout(tick, 4000);
  } catch(e) {
    showMM('生视频通道不可达', '<p style="color:var(--err)">'+e.message+'</p>');
    addMsg('err','生视频通道：'+e.message,'');
  }
}

/* 结果抽屉关闭按钮 */
if ($('mmOutClose')) {
  $('mmOutClose').onclick = () => { $('mmOutput').style.display = 'none'; };
}

/* ===== 设置栏 ===== */
$('settingsToggle').onclick = () => $('settingsZone').classList.toggle('collapsed');

async function loadSettings() {
  try {
    const d = await (await fetch('/secrets')).json();
    if (!d.ok) return;
    const st = d.data.status || {};
    const file = d.data.file || {};

    // 回填输入框
    $('kAgnes').value = file['agnes.api_key'] || '';
    $('kAgnesUrl').value = file['agnes.base_url'] || '';
    $('kOpenai').value = file['openai.api_key'] || '';
    $('kDeepseek').value = file['deepseek.api_key'] || '';
    $('mImage').value = file['agnes.image_model'] || '';
    $('mVideo').value = file['agnes.video_model'] || '';

    // 密钥状态
    const setKs = (id, name) => {
      const el = $(id);
      const s = st[name];
      if (!s) { el.className='key-state'; el.textContent='未知'; return; }
      el.className = 'key-state ' + (s.resolved === 'set' ? 'set' : 'missing');
      el.textContent = s.resolved === 'set' ? '已配置' : '缺失';
    };
    setKs('ksAgnes','agnes');

    addMsg('sys','设置已加载（密钥文件：' + (d.data.secrets_file||'未找到') + '）','');
  } catch(e) { addMsg('err','设置加载失败：'+e.message,''); }
}

$('btnReloadSettings').onclick = loadSettings;

$('btnSaveSettings').onclick = async () => {
  const values = {};
  if ($('kAgnes').value.trim()) values['agnes.api_key'] = $('kAgnes').value.trim();
  if ($('kAgnesUrl').value.trim()) values['agnes.base_url'] = $('kAgnesUrl').value.trim();
  if ($('kOpenai').value.trim()) values['openai.api_key'] = $('kOpenai').value.trim();
  if ($('kDeepseek').value.trim()) values['deepseek.api_key'] = $('kDeepseek').value.trim();
  if ($('mImage').value.trim()) values['agnes.image_model'] = $('mImage').value.trim();
  if ($('mVideo').value.trim()) values['agnes.video_model'] = $('mVideo').value.trim();

  if (Object.keys(values).length === 0) {
    addMsg('err','没有要保存的密钥（输入框全空）','');
    return;
  }

  const btn = $('btnSaveSettings');
  btn.disabled = true;
  try {
    const r = await (await fetch('/secrets', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(values)
    })).json();
    if (r.ok) {
      btn.className = 'btn primary saved';
      btn.textContent = '已保存 ✓';
      addMsg('sys','设置已保存：' + (r.data.saved||[]).join(', '), '');
      setTimeout(() => { btn.className='btn primary'; btn.textContent='保存设置'; btn.disabled=false; }, 1500);
      loadSettings();
    } else {
      btn.disabled = false;
      addMsg('err','保存失败：' + (r.error||''), '');
    }
  } catch(e) {
    btn.disabled = false;
    addMsg('err','保存失败：'+e.message,'');
  }
};

/* 模型路由变更 → 更新状态栏 */
$('mPrimary').onchange = () => {
  $('fModel').textContent = $('mPrimary').value;
  addMsg('sys','主模型切换 → ' + $('mPrimary').value,'429 时自动降级到 ' + $('mBackup').value);
};

/* 初始化 */
setPill('sBrowser','wait');
setPill('sAgent','off');
startStream();
loadSettings();
refreshStatus();
setInterval(refreshStatus, 4000);
</script>
</body>
</html>
"""

__all__ = ["PANEL_HTML"]
