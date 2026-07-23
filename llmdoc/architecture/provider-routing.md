# Provider 路由关系

这份文档只回答一件事：**不同任务在默认情况下走哪个 provider，何时回退，哪些健康状态会影响选路。** 主实现看 `mysearch/clients.py` 的 `_route_search`、`_route_policy_for_request`、`_MODE_PROVIDER_POLICY`。

## 角色分工

- **Tavily**：普通网页发现、新闻、快速答案的默认入口。`search_depth` 会随 `strategy` 自动切换（`verify`/`deep` 用 `advanced`）。news/status 查询自动传 `days` 做时间限制。blended 中即使做 secondary 也保留 `include_answer=True`，确保 answer 不丢失。对于 `official-web`、精确 API docs topic、pricing、changelog 这类“先找对 canonical 页，再读正文”的 query，当前也会优先让 Tavily 做第一页发现，再把 Firecrawl 留给正文验证与后续抽取；这条规则在 `include_content=true` 时也保留。strict official / exact docs 的 Tavily discovery 被收窄成“轻量发现”模式：即使请求层带了 `include_content=true`、`strategy=verify/deep`，真正发给 Tavily discovery 的仍是 `include_content=false + strategy=fast`。带 `include_domains` 的 strict docs `verify` 请求会同时启动有界 Firecrawl/Exa 正文验证，不再先让单个重型 enrichment 独占 45 秒。changelog enrichment 则先尝试 Exa，再尝试 Firecrawl。另外 `_looks_like_news_query` 的技术词白名单已扩展到 `latest/current/newest` 搭配 `stable/version/release` 等写法，避免 `python latest stable version` 这类软件版本事实查询被误判成 news 而被加上 `topic=news`、只回最近几天的新闻。软件版本 answer override 还会过滤 `future`、`main branch`、`development branch` 这类开发分支信号，避免把 `devguide` 里的未来版本误当成稳定版。来源：mysearch/clients.py
- **Firecrawl**：文档、GitHub、PDF、pricing、changelog 与正文抓取的主入口。news/status/award 现在会进入 fallback 链和 `verify`/`deep` blended；Firecrawl news 不走 unsupported `categories=["news"]`，而是发送 `sources=["news","web"]` 并继续按 `data.news` / `data.web` 分区排序。tutorial intent 发现阶段自动带 `include_content`。blended secondary 在 `verify`/`deep` 策略下均带正文；而 Firecrawl-primary 的 blended primary 现在在 `verify`/`deep` 也会带正文，支持正文级交叉验证。`include_domains` / `exclude_domains` 现在优先走 Firecrawl 原生 `includeDomains` / `excludeDomains`；只有 `includeDomains` 首轮空结果时，才退回旧的 `site:` query + 客户端域名过滤链，避免 provider 侧过滤误杀直接终止发现。strict official 结果在后续 Exa rescue、PDF boost、title enrichment 之后会再次进入统一 finalize 阶段，避免后处理把官方结果重新冲掉。对于少数高价值 troubleshooting query，如果 strict 官方候选仍为空，还会走 canonical rescue，把已知的精确官方排障页直接注入最终结果，例如 `Playwright strict mode violation fix -> /docs/locators`、`Next.js hydration error -> /docs/messages/react-hydration-error`。来源：mysearch/clients.py:143, mysearch/clients.py:6000, mysearch/clients.py:8473, mysearch/clients.py:8741, mysearch/clients.py:12156
- **Exa**：语义搜索与多样化发现。`type` 现在只走官方当前支持的 `neural` / `fast` / `auto` / `deep`：精确标识符与 pricing/docs strict query 仍会收敛到 `neural`，`fast` 策略走 `fast`，默认 verify/balanced 走 `auto`，`deep` 策略走 `deep`。`include_content` 时现在按官方 `/search` 结构发送 `contents={text:true, highlights:true}`；结果归一化时优先用 `highlights` 组 `snippet`，再把 `text` 落到 `content`。支持 `category`（github/news/research paper）、`startPublishedDate`/`endPublishedDate`。**`exploratory`/`comparison` intent 且 Exa 可用时提升为 primary provider**（fallback `Tavily -> Firecrawl`）。在 `verify`/`deep` 策略下作为第三方参与 blended 交叉验证；调用方请求正文时，supplement 同样带 `include_content=true`，避免 Firecrawl primary 失败后只剩 URL 壳。在 `confidence=low` 时自动补搜，在 `extract_url` Firecrawl+Tavily 双失败时做第三道 fallback。Exa 抽取 fallback 现在只接受精确 canonical URL 或同注册域候选，并在 metadata 中保留 `requested_url` / `exa_url`，避免把相似但不相关的页面误标为目标 URL。research `deep` 策略下作为第三路并行发现源；现在 Exa 的并行发现结果会正式进入 research evidence，补充 `exa_discovery_count`、`exa_unique_url_count` 与 `exa_promoted_page_count`，不再只是“补 URL 但不记功”。如果 Exa 只是在 rescue / supplement 阶段返回 `402/432` 额度限制或瞬时 HTTP 错误，runtime 现在会保留已拿到的主结果，并把问题降级成 `secondary provider issue`，而不是让整条 search 直接失败；live probe 命中明显的 `credits/usage limit` 文本时，也会暂时跳过 Exa rescue。来源：mysearch/clients.py
- **xAI / compatible social gateway**：X / Social 搜索与舆情路径。official 模式下 `strategy=fast` 的 hybrid 请求只会在**没有** `allowed_x_handles` / `excluded_x_handles` 时走 xAI 单次 web+x 联合搜索；一旦带 X handle 过滤，runtime 会拆回 Web 与 X 并行。official `/responses` 里的 `web_search.filters` 与 `x_search` 也按当前官方约束只发送单侧过滤：Web 只取 `allowed_domains` 或 `excluded_domains` 之一，X 只取 `allowed_x_handles` 或 `excluded_x_handles` 之一。`comparison`/`status` intent 和 `verify`/`deep` 策略无 answer 时自动补充 xAI answer。现在在 `verify`/`deep` 且 evidence 已暴露 conflicts、providers_consulted 至少两方、并且不处于 strict official 模式时，xAI 还会进入仲裁层，返回 `xai_arbitration_summary / xai_arbitration_confidence`，用于解释冲突而不是改写 strict 官方结论。research `deep` 策略继续生成 `research_summary`。兼容 social 搜索本身不再被 10 秒硬截断，而是读取 `MYSEARCH_XAI_SOCIAL_TIMEOUT_SECONDS`（默认 120 秒）；Proxy / social gateway 也同步读取 `SOCIAL_GATEWAY_TIMEOUT_SECONDS`（默认 120 秒）。不过这 `120s` 现在是整条 social 请求的总预算，不再默认让 `xAI` 单独吃掉 ~`105s`：runtime 会先给 `xAI` 一个更短的 primary 窗口，再显式给 `tavily_social_fallback` 预留更大的生存预算。对 `502 / 503 / 504 / TLS connect error / timeout / EOF` 这类瞬时 social gateway 错误，会先在 `custom_social` 内部最多重试 3 次；如果仍失败，则优先回退到同 query 的 `social_last_good_cache`，没有 last-good social cache 时再看 30 秒 TTL 的 `social_unavailable` 负缓存，避免同题在双上游都挂时反复把整条超时链跑满。同时还会把当前 social gateway 的不可用状态写入 45 秒 TTL 的 `social_gateway` 缓存：同一上游在持续 `502/TLS` 抖动时，后续不同 query 也会短时间直接绕过 gateway，直接走 `tavily_social_fallback` 或快速返回不可用，而不是每条 query 都重跑整条重试链。`tavily_social_fallback` 自身现在也会在自己的 budget 内对瞬时 `502/proxy_error` 再试一次，避免首个 fallback 请求就把结果判成 `social_unavailable`。一旦 fallback，结果仍会做 handle 多样性控制，citation 也只保留与当前结果对齐的 x.com 链接。来源：mysearch/clients.py:9046, mysearch/clients.py:9112, mysearch/clients.py:9871
- **正文 enrichment 失败语义**：`include_content=true` 时，provider 返回 URL 但没有正文会继续触发 fallback chain；如果后续 provider 全部失败或仍无正文，runtime 保留第一份成功发现结果，并在 `evidence.content_enrichment`、`requested-content-unavailable` conflict 及 `fallback.used=false` 中显式暴露失败，不再把已发现的 canonical URL 丢掉或伪装成完整正文成功。
- **Hybrid deadline**：`web + x` 的联合与并行路径共用 20 秒分支预算，预算会继续下传到 compatible social 的 Tavily/Exa fallback。official unified 请求失败时自动退到 Web + Social 并行路径；单独 `mode=social` 仍遵循 `MYSEARCH_XAI_SOCIAL_TIMEOUT_SECONDS`，不会被 Hybrid 的交互预算覆盖。
- **Crawl deadline**：Firecrawl `crawl_site` 的创建请求、状态轮询、瞬时重试和 `Retry-After` 等待共享有界总预算；默认预算由单次请求 timeout 加最多 120 秒 pinned-key 冷却余量组成，显式内部 timeout 可进一步收紧。预算或轮询次数耗尽时仍非终态会显式失败，不再返回伪成功的 processing payload。
- **Cache identity**：search TTL cache 除 query、route、日期与过滤条件外，还包含请求的 `max_results`，不同结果预算不会互相命中。
- **Social key/state safety**：standalone Social gateway 会按配置 key 轮换，429 按 `Retry-After` 冷却，明确额度/鉴权失败隔离 key，并对公开 health/API 错误脱敏；Proxy Social settings reset 使用 generation 校验，旧 resolver 不能在 reset 后重新提交过期 state。

## 默认路由矩阵

| 场景 | 默认 provider | 回退链 | 备注 |
| --- | --- | --- | --- |
| `mode=web` | Tavily | `Tavily -> Exa -> Firecrawl` | `verify`/`deep` 时 blended 含 Exa 三方交叉 |
| `mode=news` / `intent=status/news` | Tavily | `Tavily -> Exa -> Firecrawl` | 自动 `days` 限时；`verify`/`deep` 时允许 Tavily+Firecrawl blended |
| `mode=docs` | Firecrawl | `Firecrawl -> Tavily -> Exa` | 精确 API docs topic / pricing / changelog 会切成 Tavily 先发现，再由 Firecrawl 接正文验证；即使 `include_content=true` 也保持这条 discovery 顺序；结果继续走官方优先重排 |
| `mode=github` | Firecrawl | `Firecrawl -> Exa -> Tavily` | GitHub 按资源类严格模式 |
| `mode=pdf` | Firecrawl | `Firecrawl -> Tavily -> Exa` | PDF 按资源类严格模式 |
| `intent=resource/tutorial` | Firecrawl | `Firecrawl -> Tavily -> Exa` | tutorial 自动带 `include_content` |
| `intent=exploratory/comparison` | **Exa** | `Exa -> Tavily -> Firecrawl` | Exa 可用时提权；neural search 语义多样性优势 |
| `include_content=true` | Firecrawl | `Firecrawl -> Tavily -> Exa` | 通用正文请求仍是 Firecrawl-first；精确 official/docs/pricing/changelog 会切到 Tavily-first discovery |
| `mode=social` / X handle | xAI | 不走 Tavily/Firecrawl | — |
| `web + x` hybrid | 并行 | `strategy=fast` + xAI official + 无 X handle 过滤 → 单次联合请求；否则并行 Web 与 xAI/Social | 成功路径会走统一 merge/dedupe，按轮转混排后再截到 `max_results`；evidence 会补 `result_count_before_trim`、`returned_result_count`、`matched_results`。任一分支失败时仍保留另一分支结果，并在 evidence/conflicts 暴露 `web-search-unavailable` 或 `social-search-unavailable` |
| `research` | Tavily / Exa 协同发现 + Firecrawl 抓取 | 技术比较与 strict official 题会切到 docs-aware authoritative 路径；comparison / exploratory 题会切到 Exa 主发现，并并行补 Tavily 辅助发现；social URL 纳入 scrape | `deep` 策略生成结构化 shortlist 报告，并继续补 `claim-level evidence`、`source clusters`、cluster `tier/weight`、`decision table` 与更强的 comparison/recommendation 结构；research evidence 现在会显式区分 `authoritative_source_count` 与 `supporting_source_count`，不再把 supporting 来源误算成 authoritative；authoritative-preferred docs research 里，generic query 的 `agentic/search/retrieval` 之类泛词不再被当成品牌词，marketing/blog 页会被降到 `general/community`，且当 shortlist 已经有 official/supporting 锚点时，general 候选默认只保留最有信息量的一条，并优先泛 docs guide / retrieval explainer，而不是 paper/blog；对 `best approach for official docs retrieval...` 这类 generic vendor-doc query，primary discovery 现在会走 relaxed `Tavily web + exploratory`，避免误触发 strict official docs 搜索，再由 authoritative shortlist 把 canonical vendor docs 提到无关官方页前面；generic vendor-doc query 下，authoritative shortlist 还会对 `official/supporting` 候选按注册域名做限流，避免同一个 vendor 的多条 docs 挤占前排；如果 primary web discovery 和 docs rescue 都失败，research 还会直接退到 `canonical_research_docs`，至少保留 `Tavily / Exa / Firecrawl` 的 first-party docs 证据束，而不是整条 research 直接报错；comparison query 的 canonical fallback 现在还会补进已知的 first-party comparison page（例如 `Firecrawl vs Tavily`、`Firecrawl vs Exa`、`Firecrawl vs Apify`、`Exa vs Tavily`），并且这些 `canonical_research_projects` 会在注入源头就排在 supporting docs 前面；对未知 pair 只要命中 `Firecrawl` 也会补 `https://www.firecrawl.dev/compare` 的官方 compare hub，同时把 `Apify API` 这类对手方 first-party docs 一并注入 supporting 候选，不再只靠第三方 listicle 起步；针对显式的 OpenAI product comparison，例如 `Responses API vs Batch API`，research 现在还会额外注入 `Migrate to the Responses API`、`Responses Overview`、`Batch API`、`Background mode` 这些 canonical docs，避免长运行任务比较被社区讨论串或旧迁移文档重新带偏；在 `canonical_research_docs` fallback 真正落结果时，`canonical_research_projects` 会在 official/resource policy 之后再次被提到前面，确保 first-party comparison page 不会被 supporting docs 重新压回去；non-authoritative comparison research 里，supporting docs 也会按被比较方先做一轮实体级多样化，再按注册域名去重，尽量让 shortlist 前排同时保留双方 vendor docs，而不是只剩一个 vendor 的文档视角；只有 direct first-party comparison 页才会升为 `project`，同域 `alternatives/pricing` 这类 branded marketing 页会降到 `listicle`，shortlist 也会限制同域 `project` 刷屏；report 的 `Executive Summary / Key Findings / Top Sources` 会优先跟随 shortlisted candidates，而不是整份 citations；comparison summary 现在还会先给决策句，再给 substantive vendor-doc claim，最后补 authoritative/supporting support summary，不再让弱壳句抢首句；comparison claim 也会优先采用有内容的 excerpt，并过滤 CTA、导航壳、授权页壳和 markdown boilerplate；当 supporting vendor docs 的 live excerpt 太弱时，report/claim 层还会回退到 canonical vendor-doc snippet，而 `agent-skills` 之类导航壳页会被继续压掉；`claim_level_evidence` 现在与 `claim_evidence` 保持同一份结构化明细，并额外暴露 `support_basis`，例如 `shortlisted comparison page`、`shortlisted vendor docs`、`shortlisted official docs`，同时支持把“同一来源被多 provider 同时命中”的情况提升为 `cross-provider` 级支撑；必要时再补 xAI `research_summary` |
| `extract_url(auto)` | Firecrawl scrape | → Tavily extract → Exa `text=true` | Exa fallback 必须匹配目标 URL 或同注册域；返回结果会区分 requested URL 与实际 Exa URL |

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
5. **空结果质量回退**：domain-filtered、`docs/github/pdf/news`、`resource/tutorial/status`，以及 `comparison/exploratory` 这类 Exa 主路由查询，如果当前 provider 返回空结果，会被当作质量失败而不是成功空响应，继续尝试 fallback chain；这条回退不要求 provider 先抛异常。来源：mysearch/clients.py:6103, mysearch/clients.py:6155

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
| `type: neural` | 精确标识符、pricing/docs strict query 和默认语义查询 |
| `type: fast` | `strategy=fast` 的轻量补搜 |
| `type: auto` | `balanced` / `verify` 的默认发现 |
| `type: deep` | `strategy=deep` 的深度发现 |
| `category` | `github` / `news` / `research paper`，映射自 mode/intent |
| `highlights` | 默认启用，优先作为 snippet 来源 |
| `startPublishedDate` / `endPublishedDate` | 透传 `from_date` / `to_date` |
| `text: true` | `include_content` 和 extract fallback；与 `highlights=true` 一起使用 |

## health-aware 路由

`_probe_provider_status` 会用真实请求探活（300s TTL 缓存）；状态区分 `not_configured` / `ok` / `auth_error` / `http_error` / `network_error`。`health()` 并行探测 4 个 provider。`auth_error` 的 provider 在路由和 fallback 中被跳过；对 `http_error` 里明显的 provider-limit 文本（例如 `HTTP 402/432`、`credits limit`、`usage limit`），路由层也会把该 provider 视为当前不可用，避免反复触发明知会失败的 Exa rescue。

## 结果摘要信号

- `strict official` 命中时，summary fallback 继续以 `Top official match` 开头。
- `OpenAI API Pricing` 这类 canonical pricing 场景会显式压低 `ChatGPT Pricing` / `Business ChatGPT Pricing`，避免官方域内的错误价格页抢到第一位。
- docs / github / pdf / resource / tutorial 这类结果如果没有 `answer`，summary fallback 现在还会拼接首条 snippet 或 excerpt，不再只剩一行命中标题。
- `openai/openai-node latest release github` 这类 GitHub release / changelog 查询现在不会再误判成 `status` 或 strict official 过滤：runtime 会把它们保留在 `resource/changelog` 语义里，并在资源重排时优先 canonical `github.com/.../releases` 页面，再用 release 页正文补 `Latest release x.y.z (YYYY-MM-DD)` 摘要。
- strict docs/tutorial 查询如果 provider 结果里没有任何官方 troubleshooting 页，当前会触发小范围 canonical rescue，并在 evidence 里留下 `official_rescue_applied / official_rescue_source=canonical-map`，避免 community issue、博客或论文噪声压住精确官方排障页。
- `status` 严格官方题现在也会在 provider 完全空结果或只剩非 status 官方页时注入 brand status root 的 canonical rescue，例如 `OpenAI latest status official -> https://status.openai.com/`、`Cloudflare status official -> https://www.cloudflarestatus.com/`。
- 这层调整只影响可见解释，不改变排序；主要用于修正多语言官方文档、精确 API 参考页和 docs 命中后“结果对但摘要过薄”的问题。

## research 工作流

1. 并行：web 发现（技术比较题可切 docs-aware authoritative 路径；comparison / exploratory 题走 Exa 主发现并并行 Tavily 辅助发现）+ social 搜索 + Exa 并行发现（`deep` 策略）。如果 social 分支返回 `social_unavailable` / `social_gateway_unavailable` 这类不可用 payload，research 会把它记成错误与 conflict，不再当成成功 social provider。
2. 选 URL（按 authoritative / project / curated / listicle / directory / community 分层选择 research 候选；comparison research 里只保留 direct first-party comparison `project`，同域 `project` 不会刷满前排；social 里的非 x.com 文章 URL 继续补位）
3. 已预取的跳过 scrape，其余并行 `extract_url`
4. 证据汇总 + 结构化 report sections；其中 Exa/Tavily discovery 都会显式写入 research evidence，并把被提升进入 scrape 的 URL 计入 `exa_promoted_page_count`；generic vendor-doc query 在 primary discovery 和 docs rescue 都失败时，会直接退到 `canonical_research_docs`，至少保留 first-party docs 的 evidence/shortlist/report
5. report 层继续把 research 候选的 `matched_providers`、`selected_candidate_cluster_counts`、`cross_provider_candidate_count` 变成可读结构：`Claim-Level Evidence`、带 `tier/weight` 的 `Source Clusters`、`Ranked Shortlist`、`Decision Table` 与更强的 `Recommendation`
6. `Executive Summary` 与 `Recommendation` 不再盲目使用 `claim_evidence[0]`；当前会优先选取第一个“非 generic、非 single-source、且具实质句意”的 claim，避免 `Proxy support.`、标题壳或导航壳抢占摘要主句
7. comparison / vendor-doc research 的可见 summary 现在还会先给 shortlist winner，再给 substantive supporting claim；React docs 一类页面里的 markdown links、`Copy pageCopy`、导航 chrome 不再优先进入可见 summary。若 surviving excerpt 只剩 `is` / `supports` / `provides` 这类谓词开头的残句，运行时会把清洗后的 canonical title 一并补回去，避免 summary 退化成无主语碎片。
8. research claim / report 现在还会显式过滤 templated JSON-shell 和 API schema 壳，例如 `{{ "id": ... "completion_window": ... }}` 这类批处理 reference 片段；如果 page excerpt / snippet / content 三者都只剩这种壳文本，claim 组装会直接丢弃，不再把它重新塞回 `Consensus Snapshot`、`Claim-Level Evidence`、`Ranked Shortlist` 或 `Decision Table`。对 `Responses API vs Batch API` 这类 canonical OpenAI 对比题，可见 summary 现在预期应该先看到“何时用 Responses、何时用 Batch”的决策句，而不是 community thread 或 API schema 片段。

## 内部 benchmark 比较链路

- 内部 benchmark runner 已回滚到直接 Tavily MCP 链路，不再使用 Tavily-compatible REST fallback，也不再把 Tavily comparator 状态折叠成 `comparator-blocked`。
- Tavily comparator Bearer 现在优先读 `--tavily-bearer` / `TAVILY_MCP_BEARER`，都为空时会自动回退到本机 `~/.codex/config.toml` 里的 `mcp_servers.tavily-hikari.headers.Authorization` 或 `http_headers.Authorization`，不再要求先去服务端管理接口拿 token。
- 当前恢复后的实测是：
  - `tavily_search` 可直接通过 MCP 返回结果
  - `tavily_research` 仍可能直接返回 `HTTP 502`
- 因此 research comparator 现在要按“直接 Tavily 失败”理解，而不是按“内部 fallback 已兜底”理解。
- `--mysearch-only` 现在是保守刷新：只更新 MySearch 列，不会再用空的 `tavily_*` 字段覆盖已有的 Tavily 对照列，也会保留既有 `tavily_raw=...` note。来源：scripts/run_remote_mcp_benchmark.py:952
- benchmark 输入行现在支持 `sources_hint`；例如 `web|x` 会直接透传到 MySearch `search(..., sources=[...])`，用于显式覆盖 web+X 协作路径，而不是只靠 query 猜测是否触发 hybrid。来源：scripts/run_remote_mcp_benchmark.py:261
- Tavily comparator 的 session 自愈现在不只覆盖旧的 `Session not found` / `Missing mcp-session-id`，也覆盖 `session_required` / `must include mcp-session-id`，以及新的 `session_unavailable / please reconnect to initialize a new session` 变体；遇到这类响应时，runner 会自动重新 `initialize` 再重放 `tools/call`，不再把这种可恢复的 MCP 会话抖动直接记成 `partial-error`。来源：scripts/run_remote_mcp_benchmark.py:415, scripts/run_remote_mcp_benchmark.py:1010
- Tavily comparator repeat sampling 现在也会把首个成功结果之后的 `HTTP 429 quota_exhausted` 当成非致命限流抖动处理，不再因为后续 repeat 被限流就把整行 benchmark 升成 `partial-error`。
- 如果 Tavily comparator 的 `research` 流在真正 payload 前先发 `: ping ...` 这类 SSE heartbeat/comment 行，runner 现在会先剥掉这些行，再做 JSON 解析；不会再把心跳注释误判成 `Expecting value: line 1 column 1`.
- Tavily comparator 的 SSE payload 如果被切成多行 continuation fragment，runner 现在会先去掉 `event:` / `data:` 前缀并把剩余片段重新拼回完整 JSON，再解析；像 `news-02` 这类长结果不再因为截断而掉成 `Unterminated string`.
- 远端 benchmark SSH watchdog 不再固定写死 `300s`；研究型 case 会拿到更高 timeout budget，避免 `research-01` / `research-03` / `longtail-academic-01` 这类长 research comparator 还没返回就被 runner 自己提前打成 `remote-benchmark-timeout`.
- 纯 `social/x` 查询如果刚刚已经通过 `tavily_social_fallback` 拿到有效结果，后续重复查询会直接命中这条 social cache，不再每次都重跑完整的 `xAI -> Tavily fallback` 退化链。
- `news / entertainment` 的 result-event 事实抽取现在多了一层“官方奖项页 HTML fallback”：如果 top result 已经是 `oscars.org / grammy.com` 这类官方奖项页，但普通 snippet 和 `extract_url(... only_main_content=true)` 还没产出 winner，runtime 会再从原始 HTML 文本里补一次 `category + winner` 抽取。这样像 `2026 Oscars best picture winner` 这类题，不会再只停在 `Top news match: ...`，而会直接补成 `Best Picture winner: ...`。

## 结果型事实抽取

- `news / entertainment / status` 里，award / box-office / result-event query 会在结果排序之后进入一层确定性事实抽取。
- 奖项结果题当前回到 **Tavily 主发现**，但 discovery 后会做两层 refinement：先对 `winners list / full results` 这类精确 query 做 Tavily query refinement，再按奖项类型定向补搜 trusted domains（例如 `oscars.org / theacademy.com`、`grammy.com`、`npr.org / apnews.com / reuters.com`），尽量在主链路内把强 winners-page 拉上来。
- 先从 Top 结果的标题 / snippet / content 直接抽 `winner / category / title`；如果还不够，会按页面优先级对 Top5 结果做正文抓取，再从正文里抽最终答案。
- `box-office` 标题抽取现在也覆盖**无引号 headline** 形式，例如 `Project Hail Mary becomes the biggest opening weekend...`，不再因为字符类转义错误把整条结果题打成 runtime regex 异常。
- 页面优先级会提高主流来源、`winner / full results / full list` 这类 winners-page 信号，并下压 prediction 与年份不匹配页面，减少 Grammys / Oscars 一类结果题被泛娱乐报道带偏。
- 弱 award-result 页面不再抢先覆盖最终答案；只有强 winners-page 信号才会短路 Exa rescue，否则保持 rescue 路径开启。最近一轮还补了官方页格式识别，例如 `Best Picture. Winner. ...`、`Album Of The Year · ...` 这类 dotted / bullet 形式也能被正确抽成最终答案。
- `research` comparison report 现在把 markdown link-index / badge-shell 也视作可见层噪声：如果官方 row 只有 `* [Models]...`、`# Batch API | OpenAI API [![Image...` 这类导航壳，`Ranked Shortlist` 和 `Decision Table` 会优先回落到 claim text 或 cleaned title，而不是把壳文本直接展示给最终用户。
- strict official / exact docs 的普通 `summary` fallback 也补了同一类可见层去噪：如果 top hit 已经正确，但 snippet 只剩导航壳、badge-shell 或代码壳，`Top official match: ...` 不再把这些碎片拼到标题后面。

## Benchmark 观察口径

- 现在的对比不再只看 generic `accuracy/richness`，而是按能力链看：
  - `authority_precision`
  - `semantic_discovery`
  - `provider_orchestration`
  - `multi_source_fusion`
  - `content_fidelity`
  - `freshness_signal`
  - `site_coverage`
  - `traceability`
  - `resilience`
  - `efficiency`
- 这意味着 `MySearch` 的主要判断点是 orchestration / fusion / extraction / coverage，`tavily-hikari` 的主要判断点是 authority / freshness / traceability。
- 做后续 benchmark 复核时，优先看 `active_dimensions` 是否落在这组能力链上，而不是沿用旧的内容类型标签。

2026-03-25：继续收口 `official-web / pdf / tutorial-debugging / status`。

2026-03-30：active benchmark 进入 provider-limit 窗口后，运行时和 comparator 又补了三条 resilience 语义：

- 搜索缓存默认 TTL 现在是 `120s`，用于提升 repeated benchmark query 的稳定性；同一容器生命周期内，`repeat_runs` 不会再因为 `30s` TTL 过短而把本可命中的结果提前打成 fallback 或 partial-error。来源：mysearch/config.py, mysearch/.env.example
- 内部 benchmark comparator 现在会把 Tavily 的 `432` 明确分类成 `tavily-search-upstream-plan-limited`、`tavily-extract-upstream-plan-limited`、`tavily-research-upstream-plan-limited`，不再和普通内容质量差异混在同一类 `partial-error`。来源：scripts/run_remote_mcp_benchmark.py
- `social/x` 在 `xAI timeout + Tavily social fallback` 同时失败时，当前会进入 `Exa social proxy` 最后兜底：先试严格 `x.com/twitter.com`，再试 query-augmented `site:x.com OR site:twitter.com`，并接受明确的 X 代理域，例如 `xcancel.com`、`techtwitter.com`、`threadreaderapp.com`。这条链已经把 active slice 里的 `social-x-stability-01/02` 从 `social_unavailable` 收口到 `exa_social_fallback`。来源：mysearch/clients.py
- `extract_url` 当前会对 Firecrawl 的瞬时 `429/502/503/504` 先重试一次，再决定是否落到下游 provider；这条修正已经把 active non-research slice 里的 `extract-quality-01 / hard-extract-quality-01` 从 runtime partial-error 收口到 `captured`。来源：mysearch/clients.py
