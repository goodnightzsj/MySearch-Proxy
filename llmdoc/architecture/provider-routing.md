# Provider 路由关系

这份文档只回答一件事：**不同任务在默认情况下走哪个 provider，何时回退，哪些健康状态会影响选路。** 主实现看 `mysearch/clients.py` 的 `_route_search`、`_route_policy_for_request`、`_MODE_PROVIDER_POLICY`。

## 角色分工

- **Tavily**：普通网页发现、新闻、快速答案的默认入口。`search_depth` 会随 `strategy` 自动切换（`verify`/`deep` 用 `advanced`）。news/status 查询自动传 `days` 做时间限制。blended 中即使做 secondary 也保留 `include_answer=True`，确保 answer 不丢失。
- **Firecrawl**：文档、GitHub、PDF、pricing、changelog 与正文抓取的主入口。news category 已补全。`data.news` / `data.web` 分区会按 intent 优先排序。tutorial intent 发现阶段自动带 `include_content`。blended secondary 在 `verify`/`deep` 策略下均带正文，支持正文级交叉验证。
- **Exa**：语义搜索与多样化发现。`type` 会自适应 `neural`（语义）/ `keyword`（精确标识符）。支持 `category`（github/news/research paper）、`highlights`（语义高亮 snippet）、`startPublishedDate`/`endPublishedDate`。**`exploratory`/`comparison` intent 且 Exa 可用时提升为 primary provider**（fallback `Tavily -> Firecrawl`）。在 `verify`/`deep` 策略下作为第三方参与 blended 交叉验证，在 `confidence=low` 时自动补搜，在 `extract_url` Firecrawl+Tavily 双失败时做第三道 fallback。research `deep` 策略下作为第三路并行发现源。
- **xAI / compatible social gateway**：X / Social 搜索与舆情路径。official 模式下 `strategy=fast` 的 hybrid 请求会用 xAI 单次 web+x 联合搜索。`comparison`/`status` intent 和 `verify`/`deep` 策略无 answer 时自动补充 xAI answer。research `deep` 策略生成 `research_summary`。

## 默认路由矩阵

| 场景 | 默认 provider | 回退链 | 备注 |
| --- | --- | --- | --- |
| `mode=web` | Tavily | `Tavily -> Exa -> Firecrawl` | `verify`/`deep` 时 blended 含 Exa 三方交叉 |
| `mode=news` / `intent=status/news` | Tavily | `Tavily -> Firecrawl -> Exa` | 自动 `days` 限时；`verify`/`deep` 时允许 Tavily+Firecrawl blended |
| `mode=docs` | Firecrawl | `Firecrawl -> Tavily -> Exa` | 结果走官方优先重排 |
| `mode=github` | Firecrawl | `Firecrawl -> Exa -> Tavily` | GitHub 按资源类严格模式 |
| `mode=pdf` | Firecrawl | `Firecrawl -> Tavily -> Exa` | PDF 按资源类严格模式 |
| `intent=resource/tutorial` | Firecrawl | `Firecrawl -> Tavily -> Exa` | tutorial 自动带 `include_content` |
| `intent=exploratory/comparison` | **Exa** | `Exa -> Tavily -> Firecrawl` | Exa 可用时提权；neural search 语义多样性优势 |
| `include_content=true` | Firecrawl | `Firecrawl -> Tavily -> Exa` | — |
| `mode=social` / X handle | xAI | 不走 Tavily/Firecrawl | — |
| `web + x` hybrid | 并行 | `strategy=fast` + xAI official → 单次联合请求；否则并行 Tavily+xAI | — |
| `research` | Tavily 发现 + Firecrawl 抓取 | docs 模式 Firecrawl 发现阶段带正文；social URL 纳入 scrape；`deep` 策略 Exa 并行第三发现源 | `deep` 策略生成 xAI `research_summary` |
| `extract_url(auto)` | Firecrawl scrape | → Tavily extract → Exa `text=true` | 三级 fallback |

## Provider 协作机制

### strategy 驱动的协作

| strategy | Tavily | Firecrawl | Exa | xAI |
|----------|--------|-----------|-----|-----|
| `fast` | 单 provider；`search_depth=basic` | 单 provider | 仅 rescue | hybrid 用 xAI 单次联合 |
| `balanced` | `search_depth=basic`；blended primary/secondary | 参与 blended | 仅 rescue | — |
| `verify` | `search_depth=advanced`；blended | 参与 blended（**带正文**） | **三方交叉验证** | 补 answer（含 status intent） |
| `deep` | `search_depth=advanced`；blended | 参与 blended（**带正文**） | **三方交叉验证** + research 并行发现 | 补 answer + research summary |

### news blending

`verify`/`deep` 策略下，news 和 status 场景也允许 Tavily+Firecrawl blended，利用两者 news 来源覆盖互补。`fast`/`balanced` 策略下仍走单 provider。

### 结果质量闭环

1. **`confidence=low` 自动补搜**：非 fast 策略下，evidence 评估为 low confidence 时自动触发 Exa 补搜并重新评估。见 `_postprocess_search`。
2. **`low-source-diversity` 反馈**：conflicts 检测到低来源多样性时，在 evidence 里注入 `retry_hint` 建议 `strategy=verify`。
3. **`cross_provider_boost`**：被多个 provider 同时发现的结果在 rerank 中获得更高排序权重。见 `_web_result_rank`。
4. **blended secondary 失败降级**：secondary provider 失败时，verification 标记为 `single-provider-secondary-failed`，不再误报 `cross-provider`。

### extract_url 三级 fallback

1. Firecrawl scrape → 质量检查
2. Tavily extract → 质量检查
3. Exa `text=true` 语义抓取 → 质量检查

### xAI answer 补充

- `comparison`/`status` intent 无 answer → xAI `web_search` 生成摘要
- `verify`/`deep` 策略无 answer → xAI `web_search` 补充
- evidence 标记 `answer_source: xai`

## Exa 能力利用

| Exa 参数 | 使用方式 |
|----------|---------|
| `type: neural` | 默认语义查询；exploratory/comparison intent 下作为 primary |
| `type: keyword` | 精确标识符（类名、API 路径、版本号）自动切换 |
| `category` | `github` / `news` / `research paper`，映射自 mode/intent |
| `highlights` | 默认启用，优先作为 snippet 来源 |
| `startPublishedDate` / `endPublishedDate` | 透传 `from_date` / `to_date` |
| `text: true` | `include_content` 和 extract fallback |

## health-aware 路由

`_probe_provider_status` 会用真实请求探活（300s TTL 缓存）；状态区分 `not_configured` / `ok` / `auth_error` / `http_error` / `network_error`。`health()` 并行探测 4 个 provider。`auth_error` 的 provider 在路由和 fallback 中被跳过。

## research 工作流

1. 并行：web 发现（docs 模式 Firecrawl 带正文预取） + social 搜索 + Exa 并行发现（`deep` 策略）
2. 选 URL（web 发现 + Exa 发现补位 + social 里的非 x.com 文章 URL 补位）
3. 已预取的跳过 scrape，其余并行 `extract_url`
4. 证据汇总 + 可选 xAI research summary（deep 策略）
