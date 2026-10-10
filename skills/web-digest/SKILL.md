---
name: web-digest
description: 抓取一个或多个网页，提取正文并产出结构化要点摘要
triggers: [抓取, 摘要, 网页, digest, summarize, crawl]
tools: [crawl, model, fs]
version: 1
---

# 网页摘要（web-digest）

把一个 URL 变成「可用要点」。**查/读属低风险线（§9A 线 A），放心做**。

## 步骤

1. **取内容（走单一出口）**
   - 调 `crawl.fetch`（L1，httpx）。命中动态壳页或风控特征时自动升 L2/L3。
   - 命中人机验证 → **停下交人工**：把画面推上拇指浏览器，由用户手动过一次；
     不自动绕、不硬闯（§9B.4-1）。
2. **清洗**
   - 去掉 script/style/nav/footer，保留正文；CJK 与英文都保留原语言。
3. **摘要（模型路由）**
   - 调 `model.complete`，`task_class="crawl-summarize"`。
   - 默认顺序：agnes-flash 优先 → 本地兜底；429 自动降级，**不中断任务**。
4. **产出**
   - 写 `digest/<host>.md`：标题、URL、抓取时间、3–7 条要点、原文行数。
   - 正文超过 20k 字时先分块再合并，避免超上下文。

## 约束

- 只处理**公开、无需登录**的内容；命中登录墙即停止升级。
- 遵守 robots 与限速（`crawl.max_per_minute`，默认 30）。
- 不写入工作区之外（沙箱 §9B 只读区保护）。

## 固化

这套步骤跑顺之后，用 `skill.scaffold` 把当时的具体参数（站点、字段、模板）
固化成新技能，下次同类任务直接复用——这就是自进化闭环的落点。
