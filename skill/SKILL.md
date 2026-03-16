---
name: mysearch
description: >-
  Unified web research skill for MySearch MCP. Aggregates Tavily, Firecrawl,
  and X search (via xAI) behind one workflow. Use when the user wants current
  web search, social/X search, document-oriented search, URL content extraction,
  or a small multi-source research pack with citations.
allowed-tools: mcp__mysearch__search, mcp__mysearch__extract_url, mcp__mysearch__research, mcp__mysearch__mysearch_health
---

# MySearch

MySearch 是一层聚合搜索技能，不假设你只用单一 provider：

- Tavily：适合普通网页发现、新闻检索、快速答案
- Firecrawl：适合文档、GitHub、pricing、changelog、正文抓取
- X 搜索：适合“大家在 X 上怎么说”、实时舆情、开发者讨论

## 默认工作流

1. 先用 `mysearch_health` 确认当前哪些 provider 已配置可用
2. 默认从 `search` 开始，让路由层自动选 provider
3. 如果问题明显是对比、趋势、原因分析，优先显式传 `intent`
4. 如果要交叉验证或更稳妥的结果，显式传 `strategy="verify"` 或 `strategy="deep"`
5. 只有在需要正文时才用 `extract_url`
6. 需要“先搜再抓再整理”时用 `research`

## 决策流程

1. 先判断是否真的需要外部搜索
2. 需要实时信息、新闻、产品状态时优先搜索，不用内部记忆硬答
3. 需要单页正文时，不要反复搜索，直接 `extract_url`
4. 需要多个来源交叉验证时，用 `research`
5. 输出时保留来源链接，并区分事实、引文和推断

## Intent 与 Strategy

- `intent="factual"`：普通事实检索
- `intent="status"` / `intent="news"`：最新动态、版本、发布、事故
- `intent="comparison"`：选型、对比、优缺点
- `intent="tutorial"`：教程、guide、how-to
- `intent="exploratory"`：原因、影响、趋势、分析
- `intent="resource"`：docs、GitHub、pricing、changelog、PDF

- `strategy="fast"`：单 provider 快速返回
- `strategy="balanced"`：主 provider + 次 provider 补充
- `strategy="verify"`：Tavily + Firecrawl 交叉验证网页结果
- `strategy="deep"`：更偏 research 的双 provider 路径

默认自动行为：

- `comparison` / `exploratory` 会自动倾向 `verify`
- `resource` / `tutorial` / `include_content=true` 会自动倾向 `balanced`
- `research` 会自动倾向 `deep`

## 自动路由规则

- 普通网页检索：默认 Tavily
- 新闻 / 最新动态：默认 Tavily news
- 文档 / GitHub / PDF / changelog / pricing：默认 Firecrawl
- X / Twitter / 社交舆情：默认 xAI X search
- 同时要网页和社交：走 hybrid，网页用 Tavily，社交用 xAI

## X provider 模式

- `official`：适合官方 xAI，或真正支持 `x_search` / `web_search` 的兼容后端
- `compatible`：适合 `grok2api` 这类只提供 `/responses` 的兼容网关
- `compatible` 模式下，真正的 X 结果要来自 `mysearch.social_gateway` 这类 social search gateway
- 如果 social gateway 前面还有一层 proxy，可以优先用“grok2api admin 自动继承”模式，避免重复维护 `SOCIAL_GATEWAY_UPSTREAM_API_KEY` / `SOCIAL_GATEWAY_TOKEN`
- `MYSEARCH_XAI_SOCIAL_BASE_URL` 用来单独指定 social gateway 根地址；`MySearch` 默认会自动追加 `/social/search`

## 什么时候强制指定 provider

- 你明确知道要对正文友好的结果：`provider="firecrawl"`
- 你明确要 X 搜索：`provider="xai"` 或 `mode="social"`
- 你只想走 Tavily：`provider="tavily"`

## 使用准则

- 默认 `max_results` 控制在 5 以内
- 普通问答不要默认 `include_content=true`
- 输出时保留 URL，并区分事实与推断
- 需要更稳妥的网页结论时，优先用 `strategy="verify"`
- 输出里如果有 `evidence`，要把它当成“证据密度提示”一起解读
- 需要同时看网页和 X 时，传 `sources=["web","x"]`
- X 搜索依赖单独的 xAI key；没配时应该显式说明 social 部分不可用
- 单个页面阅读优先 `extract_url`
- 多来源整理优先 `research`

## 证据标准

- 涉及时效性、版本、发布信息时，优先相信搜索结果，不靠旧记忆
- 关键结论尽量给至少两个独立来源
- 单一来源结论要显式说明限制
- 来源冲突时，要把冲突本身讲清楚，而不是强行给一个确定答案

## 常见模式

### 普通网页搜索

- `search(query="best search MCP server", mode="web")`

### 对比 + 交叉验证

- `search(query="Tavily vs Firecrawl for docs search", intent="comparison", strategy="verify")`

### 最新新闻

- `search(query="OpenAI latest announcements", mode="news")`

### X 舆情

- `search(query="what are people saying about MCP", mode="social")`

### 网页 + X 聚合

- `search(query="latest MCP search server feedback", sources=["web", "x"])`

### 文档 / GitHub / changelog

- `search(query="Firecrawl pricing changes", mode="docs", include_content=true)`

### 抓正文

- `extract_url(url="https://example.com/post")`

### 小型研究

- `research(query="best search MCP server 2026", intent="exploratory", include_social=true)`

## 需要强制指定 provider 的场景

- 网页搜索结果太泛，需要文档站 / GitHub / changelog：`provider="firecrawl"`
- 你明确只想看 X 上的讨论：`provider="xai"` 或 `mode="social"`
- 你只需要 Tavily 的快速网页发现和 answer：`provider="tavily"`
