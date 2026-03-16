# MySearch Architecture

`MySearch` 的目标不是把所有搜索都塞进一个“万能接口”，而是把不同能力层明确分开：

```text
┌──────────────────────────────────────────────────────┐
│ Layer 1: Skill / Decision Layer                     │
│ 决定何时搜索、用哪种模式、证据标准、输出方式         │
├──────────────────────────────────────────────────────┤
│ Layer 2: MCP / Orchestration Layer                  │
│ search / extract_url / research / route / normalize │
├──────────────────────────────────────────────────────┤
│ Layer 3: Provider Layer                             │
│ Tavily | Firecrawl | official xAI | custom social   │
└──────────────────────────────────────────────────────┘
```

## 设计原则

- `search` 是统一入口，不让调用方直接感知 provider 差异
- `intent` 和 `strategy` 分开表达“搜什么”和“怎么搜”
- `extract_url` 单独负责正文抓取，不把抓取逻辑塞进搜索结果本身
- `research` 负责“小型研究工作流”，而不是把每次搜索都升级成重型 pipeline
- `official xAI` 和 `grok2api-compatible` 明确区分，避免把模型网关误当成真实搜索后端

## 四条能力线

### 1. Web discovery

- 默认 provider：Tavily
- 适合：普通网页、新闻、快速答案
- 输出重点：`answer`、基础 `results`、`citations`
- `verify` / `deep` 时：会把 Tavily 和 Firecrawl 做轻量交叉检索与去重

### 2. Docs / content extraction

- 默认 provider：Firecrawl
- 适合：文档站、GitHub、pricing、changelog、PDF、正文抓取
- 输出重点：Markdown 正文、结构化 metadata

### 3. Official X / social search

- 默认 provider：official xAI Responses API
- 条件：后端真正支持 `x_search` / `web_search` server-side tools
- 输出重点：social `results`、`citations`、`tool_usage`

### 4. Custom social search

- 适合：`grok2api` 兼容网关 + 你自己的聚合搜索 API
- 模式：`MYSEARCH_XAI_SEARCH_MODE=compatible`
- 推荐：只配置 `MYSEARCH_XAI_SOCIAL_BASE_URL`，路径默认追加 `/social/search`
- 原则：模型网关与搜索后端分离
- 内置参考实现：`mysearch.social_gateway`

## 为什么这样拆

这套结构综合参考了几个公开项目，但没有直接照搬：

- `blessonism/openclaw-search-skills`
  - 借的是“搜索层”和“内容提取层”分离
  - 没照搬它的脚本式多源评分和 thread pulling
- `ckckck/UltimateSearchSkill`
  - 借的是 skill-first 的决策流和证据标准
  - 没照搬它的 Shell + Docker 基础设施设计
- `skernelx/xai-mcp-server`
  - 借的是 MCP server / API client / tool handler 三层分离
  - 没把官方 xAI 的 server-side search 假设硬套到所有兼容网关上

## 现在已经吸收的能力

- intent-aware routing
  - `comparison` / `exploratory` / `resource` / `tutorial` 不再和普通网页问答走完全同一条路
- strategy-aware execution
  - `fast` / `balanced` / `verify` / `deep` 让调用方能显式控制证据密度
- lightweight cross-provider verification
  - 网页侧在需要时会同时调用 Tavily + Firecrawl，并返回 `evidence`
- research evidence
  - `research` 会回传 provider 覆盖、citation 数量、验证状态等元数据

## 当前边界

当前 `MySearch` 已经明确支持：

- Tavily 官方 / 兼容接口
- Firecrawl 官方 / 兼容接口
- xAI 官方搜索模式
- `grok2api-compatible + custom social gateway`

当前还没有直接内建：

- 多 query bundle 自动扩展
- 重型的跨来源统一评分排序
- thread pulling / 引用追踪
- `web_map`

这些能力后续如果要加，应该放在 `research` 或新的高级工具中，而不是把基础 `search` 变成不可控的大杂烩。
