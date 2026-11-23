# MySearch

[English Guide](./README_EN.md) · [返回仓库](../README.md)

`mysearch/` 是这个仓库里真正可安装的 MCP 服务。

它负责把 Tavily、Firecrawl 和可选 X / Social 收成一个统一搜索入口，
并暴露给 Codex、Claude Code 或其他支持 MCP 的客户端。

Exa 默认停用，保留数据和代码以便显式恢复。`MYSEARCH_EXA_ENABLED=true` 必须在 runtime / Proxy 两端一致配置，并先确认 Key 权限与余额；停用时不会读取 Exa Key 文件、探测或发起补搜，显式 Exa 搜索返回 `provider_disabled`。

如果你只想先把搜索能力装到本机 AI 助手里，优先看这一份文档。

## 提供的工具

### `search`

统一搜索入口。

直接调用 `search` 即可读取完整结果。答案、引用和各候选的正文均可按原字段内联读取。`content[0].text` 是单行 JSON，解析后与 `structuredContent` 一致。通过 Streamable HTTP 调用时，支持 gzip 的客户端可以压缩传输 4 KiB 及以上的 JSON 响应。

`include_content=false` 在所有模式、策略和嵌套分支中都不返回正文；设为 `true` 时保留完整正文。摘要只读取已检索的证据，不隐式补抓发布页。多来源命中表示检索覆盖，不能证明答案或问题前提成立；查询改写只附加提示，保留原始主体及限制条件。

带明确域名的 API 文档查找会优先匹配命名 API 的正式页面，保留版本、语言和调试查询的既有排序。奖项补充取页在同次搜索内复用已读页面，并共享最多 15 秒的时限，预留当时剩余总预算的一半。时限内未补足答案时，`evidence.result_event_page_budget_exhausted` 明确标记限制，诊断为 `degraded`。

普通 Firecrawl 正文搜索先发现 URL，再按解码后的文件路径排除 `llms*.txt` 聚合文档，最后并行抓取全部合格页面。负 `inurl` 仅为搜索提示，不能代替 URL 资格检查；`route_debug.excluded_urls` 记录排除原因与 `not-attempted`，抓取失败在 `content_fetch` 中明确保留。明确搜索聚合文档或直接 `extract_url` 时仍返回全文；辅助分支的正文仍完整内联。

服务端固定 MCP SDK 1.30.0 维护版。2026-11-21 隔离验证 Python SDK 2.3.0 客户端可自动回退到 `2025-11-25`，有状态与无状态 HTTP 均能调用六工具并完整读取 200 万字符 gzip JSON；原生 `2026-07-28` 尚不支持。无状态 HTTP 的取消通知不会立即终止服务端工作，后续调用仍可继续。维护版默认空闲会话30分钟过期（下次请求404，需重新初始化），最多10000会话；4MiB上限针对请求体，不限制全文响应。保持GET流的会话不按空闲回收。见 [1.30.0 官方说明](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v1.30.0) 与 [v2 迁移文档](https://py.sdk.modelcontextprotocol.io/migration/)。

进程内缓存每命名空间保守计量最多64MiB的 Python 载荷内存、最多256条，TTL不变。超大结果仍完整返回，只不驻留缓存；健康信息含 `payload_bytes` / `max_payload_bytes` / `oversized_skips`。这不是进程RSS上限。

支持的模式：

- `auto`
- `web`
- `news`
- `social`
- `docs`
- `research`
- `github`
- `pdf`

### `extract_url`

抓取单个页面正文。

默认行为：

- 优先 Firecrawl
- Firecrawl 失败或正文为空时回退 Tavily extract
- 两者都无法提供正文时明确保留失败；仅显式启用 Exa 后才使用 Contents，逐 URL 失败不会被 HTTP 200 掩盖

`auto` 的 Markdown 提取先复用现有缓存，再对已核实提供原生导出的 `docs.tavily.com`、`docs.firecrawl.dev` 请求对应 `.md`。只有 `text/markdown` 且正文质量通过时才采用，最多等待3秒、不跟随重定向；失败继续既有提取路径并保留诊断。其他来源及显式指定 provider 的请求不增加此探测。此能力按来源的公开导出合同启用，不依赖查询或评分题目。

Firecrawl 的目标页面状态独立于 API/Key 状态。错误页不会进入正常正文缓存或搜索正文，显式提取保留原响应并附 `warning`，自动提取继续回退；不会因为目标网站404/403而停用Key。

需要显式时效时，设置 `provider="firecrawl", max_age_seconds=0` 请求实时抓取，正整数表示允许的缓存年龄（秒）。此选项绕过本地 extract 缓存，并按毫秒传给 Firecrawl；不设置时保留现有缓存行为。

Tavily 搜索会请求发布日期；显式 `from_date` / `to_date` 同时要求上游剔除未知日期和窗口外来源。价格与状态查询使用 `max_age_hours=0` 获取新内容，不自动用发布日期限制常年更新的页面；容量不足返回真实失败。一次工具请求内相同 Firecrawl 抓取会共享完整结果，独立的实时提取仍重新请求。诊断读取 Firecrawl 抓取元数据中的 credits，xAI 明确上报的帖子/用户数量分别按原单位记录；这些数量不等于费用，缺失仍为未知。

普通 pricing/status 页面的正文搜索请求实时 Firecrawl 抓取（`maxAge=0`），而不是复用参考文档缓存。其他页面的 `auto` 提取可复用参数一致的 Firecrawl 完整正文缓存，但不延长该正文原有 TTL；formats、only_main_content 不同的投影不会混用。这不保证其他 provider 的搜索索引或上游计费信息是实时的。

MCP 调用可用 `offset` 和 `max_chars` 按字符读取正文片段，返回 `content_window` 中的总长度、下一偏移量和截断状态；省略时仍返回全文。分页复用正常提取缓存，不保证缓存过期后的页面快照不变。

### `research`

小型研究工作流。

默认流程：

- 先做网页发现
- 再抓取前几条正文
- 可选补充 X / Social 讨论

`research` 的发现、正文抓取、重试和补搜共享 `MYSEARCH_RESEARCH_TIMEOUT_SECONDS` 总预算（默认 90 秒）；过期会显式失败。普通 search/extract/map 默认使用 `MYSEARCH_TIMEOUT_SECONDS`（45 秒），Social 使用独立预算，crawl 保留独立请求预算与 pinned-key 冷却余量。MCP 排队时间也计入预算；已发出的同步 I/O 不能强杀，取消或过期后不再启动新请求，关闭客户端会等待在途工作释放资源。

`deep` 的 docs 补搜先检查本轮已有来源，仅在正文数量或比较对象覆盖不足时执行，最多3个既有查询变体；达到目标或预算耗尽即停止，域名和日期过滤继续生效。`evidence.supplemental_search` 记录查询、停止原因和错误。

`search` / `research` 的 `evidence.sources` 按 URL 区分无来源文本、检索文本与抽取正文，并记录抓取失败／空正文／预取、多 provider 命中。内置目录描述不算检索正文；`claim_verification=not-assessed` 与 `verification_scope=source-agreement-not-claim-verification` 明确表示这些信号不证明结论真实。`research` 另返回本次 `request_budget` 耗时。

hybrid 的失败占位和同provider双分支不算多provider；同一官方域名经多个provider返回也不因此获得高置信度。控制台“可调度”仅表示未隔离资格，历史成功和最近调用不代表实时额度或端点健康。限流沿用 `Retry-After`，没有可信team归属时不猜测全池分组。

### `mysearch_health`

返回当前 provider 配置、base URL、search mode、auth mode 和 key 可用性。

`available_keys` 只统计本地可调度凭证；`credential_scope=gateway` 时它是网关令牌数，不是上游健康 Key 数。外部兼容 X 端点标为 `endpoint`，不推断其后端账号数。`probe_evidence` 区分 `operation`、`service_health` 和 `public_status`；`operation_ready` 为 true 仅表示最近一次真实操作探测成功，false 表示已知不可用，null 表示未证实。结合 `probe_operation` 和 `last_checked_at` 判断适用操作与时效；公共状态页正常不证明账号有效或有余额。回退复用现有探测缓存跳过已知空池/额度错误，未知或瞬态故障仍可尝试，失败链保留各 provider 原因。单个操作不可用不会停用其他操作。

### `crawl_site`

Firecrawl 作业完成后，若入口 URL 缺失或其正文为空，会在原请求预算内精确补抓入口一次（瞬时错误沿用有界重试）。成功时入口优先，其余页面保持顺序且总数不超过 `limit`；`metadata.entry_page` 记录补抓结果及省略页数，`total/completed` 仍表示上游 crawl 作业计数。失败保留已有页面并标记 `degraded`；取消或预算耗尽继续报错。未声明完成的同步响应不触发补抓。

## MCP 合同与诊断

六个工具提供 `outputSchema` 与 annotations，保留既有文本结果及扩展字段；`crawl_site` 会创建上游作业，因此不声明只读或幂等。携带 progress token 的调用可收到排队、已完成阶段和终态通知。

使用显式 `MYSEARCH_PROXY_BASE_URL` 与 `MYSEARCH_PROXY_API_KEY` 时，每次工具调用向 Proxy 上报一条终态记录，关联 runtime 调用和 Proxy 实际上游尝试。缓存命中也有新的请求 ID；用量缺失记为未知，不跨 provider 或单位相加。诊断不记录 query、URL、正文或凭证。上报最多等待 2 秒，不重试；失败只记 warning，不改工具结果。诊断耗时不包含该上报等待，用户端实际延迟仍包含它。

Firecrawl crawl 状态中的 `creditsUsed` 是作业累计值，轮询返回值不进入逐请求用量求和；缺少独立请求用量时显示未知。诊断不是账单，旧版本已保存的累计值不会自动改写。

快速检索仅在兼容输入上使用 Tavily `fast` 和 Exa `lite`；日期、类别、排除过滤或正文需求会保留对应的既有检索深度。Tavily 请求同时收集上游 `usage`，不把 credits 当作美元费用。

Social 备用 provider 的窗口包含内部域名重试、换 key 和嵌套 fallback，不能逐调用重新获得整段预算；窗口到期后仍有父预算时继续下一 provider。版本答案只采用明确的当前版本声明或紧邻下载标题的版本，不把历史条目、支持列表或预发布尾缀当作最新版证据。

`verify` 的主检索使用根请求预算，可选验证分支使用独立窗口（包含排队、内部重试与补抓）。辅助分支到期不撤销主检索，结果保留验证失败原因；请求取消或根预算耗尽仍阻止后续发送。

## 当前推荐接法

最推荐的是 `proxy-first`：

```env
MYSEARCH_PROXY_BASE_URL=https://your-mysearch-proxy.example.com
MYSEARCH_PROXY_API_KEY=mysp-...
```

好处：

- Tavily / Firecrawl 默认一起走统一 proxy；Exa 仅显式启用时加入
- 如果 proxy 同时接通了 Social / X，同一个 token 还能继续复用
- 客户端配置最少
- 更适合团队共享和公开部署

如果你还没有 Proxy，也可以直接连 provider。

现在 Tavily 也支持显式两种接法：

- `MYSEARCH_TAVILY_MODE=official`
  - 自己导入和轮询 Tavily 官方 key
- `MYSEARCH_TAVILY_MODE=gateway`
  - 用上游 gateway token 访问兼容网关，例如 `tavily-hikari`

## 直连 provider 的最小配置

最小直连通常至少需要：

```env
MYSEARCH_TAVILY_API_KEY=tvly-...
MYSEARCH_FIRECRAWL_API_KEY=fc-...
```

如果你要让 Tavily 走上游 gateway：

```env
MYSEARCH_TAVILY_MODE=gateway
MYSEARCH_TAVILY_GATEWAY_BASE_URL=http://127.0.0.1:8787/api/tavily
MYSEARCH_TAVILY_GATEWAY_TOKEN=th-xxxx-xxxxxxxxxxxx
MYSEARCH_FIRECRAWL_API_KEY=fc-...
```

如果你明确不走上游 gateway，就保持：

```env
MYSEARCH_TAVILY_MODE=official
MYSEARCH_TAVILY_API_KEYS=tvly-a,tvly-b
MYSEARCH_TAVILY_KEYS_FILE=accounts.txt
```

如果确认 Exa 权限和余额已恢复，需要显式启用（Proxy 端也须设置）：

```env
MYSEARCH_EXA_API_KEY=exa-...
MYSEARCH_EXA_ENABLED=true
```

如果你也要接 X / Social：

```env
MYSEARCH_XAI_API_KEY=xai-...
```

没有 X / Social 时，下面这些仍然可用：

- `web`
- `news`
- `docs`
- `github`
- `pdf`
- `extract_url`
- `research`

## 安装到 Codex / Claude Code

在仓库根目录执行：

```bash
python3 -m venv venv
```

优先把配置放进宿主 config，而不是先复制 `.env`：

- `Codex`：`~/.codex/config.toml` 的 `mcp_servers.mysearch.env`
- `Claude Code`：注册 MCP 时直接把 `MYSEARCH_*` 注入 env
- `mysearch/.env`：只建议本地单仓调试时使用

填好配置后安装：

```bash
./install.sh
```

`install.sh` 会做两件事：

- 安装 `mysearch/requirements.txt`
- 如果本机有 `codex` 或 `claude` 命令，就自动注册 `mysearch` MCP
- 如果宿主已有 `mysearch` config，会直接复用其中的 `MYSEARCH_*`

## 作为 Docker MCP 服务运行

如果你已经把仓库根目录的一套 compose 跑起来：

```bash
cd /path/to/MySearch-Proxy
docker compose up -d
```

这时 `mysearch` 会通过 `MYSEARCH_PROXY_BOOTSTRAP_TOKEN` 自动从 `proxy` 申请或复用自己的 `mysp-` token，不再要求你手动先创建 MySearch 通用 token 才能拉起远程 MCP。

默认远程 MCP 地址：

- `streamableHTTP`
  - `http://127.0.0.1:8000/mcp`
- `SSE`
  - `http://127.0.0.1:8000/sse`

如果你部署的是单容器 `mysearch-stack`，容器会同时对外提供 `9874` 控制台和 `8000/mcp`；`mysearch` 自己仍然通过容器内 `127.0.0.1:9874` 回连 Proxy。

部署完成后，如果你要让 `Codex` 直接使用这个远程 MCP，最小 `~/.codex/config.toml` 配置是：

```toml
[mcp_servers.mysearch]
type = "http"
url = "http://127.0.0.1:8000/mcp"
```

如果你部署在远程主机：

```toml
[mcp_servers.mysearch]
type = "http"
url = "https://mysearch.example.com/mcp"
```

如果你的远程入口额外套了 Bearer 鉴权，可以继续写成：

```toml
[mcp_servers.mysearch]
type = "http"
url = "https://mysearch.example.com/mcp"
headers = { Authorization = "Bearer YOUR_MCP_TOKEN" }
```

加完配置后重启 `Codex`，再验收：

```bash
codex mcp get mysearch
python3 skill/scripts/check_mysearch.py --health-only
```

如果你只想单独构建 `mysearch` 镜像，也可以：

```bash
docker build -t mysearch-mcp ./mysearch
docker run --rm -p 8000:8000 \
  -e MYSEARCH_PROXY_BASE_URL=http://<your-proxy-host>:9874 \
  -e MYSEARCH_PROXY_API_KEY=mysp-... \
  mysearch-mcp
```

如果你更看重“部署最简单”，还可以直接跑单容器镜像：

```bash
export ADMIN_PASSWORD="$(openssl rand -base64 24)"
export MYSEARCH_PROXY_BOOTSTRAP_TOKEN="$(openssl rand -base64 32)"

docker run -d \
  --name mysearch-stack \
  --restart unless-stopped \
  -p 9874:9874 \
  -p 8000:8000 \
  -e ADMIN_PASSWORD="$ADMIN_PASSWORD" \
  -e MYSEARCH_PROXY_BOOTSTRAP_TOKEN="$MYSEARCH_PROXY_BOOTSTRAP_TOKEN" \
  -v $(pwd)/mysearch-proxy-data:/data \
  skernelx/mysearch-stack:latest
```

这个镜像会在同一容器里同时启动 `proxy` 和 `mysearch`，并通过内部 bootstrap 接口自动创建或复用 `mysearch` 专用 token。

## 推荐验收

### 1. 看 MCP 是否注册成功

```bash
codex mcp get mysearch
```

或者：

```bash
claude mcp list
```

### 2. 跑健康检查

```bash
python3 skill/scripts/check_mysearch.py --health-only
```

### 3. 跑一轮 smoke test

```bash
python3 skill/scripts/check_mysearch.py --web-query "OpenAI latest announcements"
python3 skill/scripts/check_mysearch.py --docs-query "OpenAI Responses API docs"
```

如果你配置了 Social / X，再补：

```bash
python3 skill/scripts/check_mysearch.py --social-query "Model Context Protocol"
```

如果你要测正文抓取：

```bash
python3 skill/scripts/check_mysearch.py \
  --extract-url "https://www.anthropic.com/news/model-context-protocol"
```

## 作为 HTTP MCP 单独启动

默认注册给 Codex / Claude Code 时，`MySearch` 走的是 `stdio`。

如果你想把它作为远程 MCP 暴露出来，可以直接启动：

```bash
python -m mysearch --transport streamable-http --host 127.0.0.1 --port 8000
```

默认 endpoint：

- `streamableHTTP`
  - `http://127.0.0.1:8000/mcp`
- `SSE`
  - `http://127.0.0.1:8000/sse`

如果你已经有远程 URL，也可以直接注册到 Codex：

```bash
codex mcp add mysearch --url https://your-mysearch.example.com/mcp
codex mcp get mysearch
```

需要 Bearer Token 时：

```bash
export MYSEARCH_MCP_BEARER_TOKEN=your-token
codex mcp add mysearch \
  --url https://your-mysearch.example.com/mcp \
  --bearer-token-env-var MYSEARCH_MCP_BEARER_TOKEN
```

## 路由逻辑怎么理解

MySearch 不是单一 provider 的壳。

默认可以这样理解：

- `web / news`
  - 优先 Tavily
- `docs / github / pdf`
  - 优先 Firecrawl
- `pricing / changelog / 官方文档`
  - 仍按 Firecrawl / 官方结果优先处理，不为凑数默认混入第三方页面
- 补充网页发现 / 长尾资料
  - 默认在 Tavily / Firecrawl 间按需补位；Exa 默认停用
- `social`
  - 走 xAI 或 compatible `/social/search`
- `extract_url`
  - Firecrawl 优先，Tavily 回退
- `research`
  - 一轮小 research：搜索发现 + 正文抓取 + 可选 social 补充
  - 当前会尽量输出结构化 short report，显式包含 `claim-level evidence`、`source clusters`、ranked shortlist 与 decision table，而不只是结果拼接

补充约束：

- `web` 与 `news` 使用不同排序口径：
  - `web` 更看官方性、页面相关性
  - `news` 更看时效、媒体质量与事件一致性
- `official / 官方 / 官网`、`docs / pricing / changelog` 一类查询会进入更严格的官方结果模式；如果官方域结果不足，会明确说明，而不是默认拿第三方结果补齐
- Exa 只有显式启用后才可在结果不足、长尾语义查询或 fallback 场景下介入
- Firecrawl github 类别已移除；公共 `mode=github` 保留，内部使用 developer 与 GitHub 域名过滤。docs 使用 developer，research 保持 academic research；发布说明和普通资源不限制学术站点。依据：[官方搜索文档](https://docs.firecrawl.dev/features/search)。

## Intent 和 Strategy

`search` 与 `research` 同时支持：

- `intent`
  - `auto`
  - `factual`
  - `status`
  - `comparison`
  - `tutorial`
  - `exploratory`
  - `news`
  - `resource`
- `strategy`
  - `auto`
  - `fast`
  - `balanced`
  - `verify`
  - `deep`

适合记忆的简单规则：

- 想快一点：
  - `fast`：单 provider，最小候选池
- 想稳一点：
  - `balanced`：先取主 provider，数量不足、低置信度、弱匹配或缺少所需正文时才补搜第二家
- 想多做交叉验证：
  - `verify`：扩大候选池，Tavily + Firecrawl 交叉验证；不默认启用 Exa
- 想做小研究：
  - `deep`：更偏 `research` 的较大候选池与更多正文抓取

## 关键环境变量

优先关注这几组：

### 通用

```env
MYSEARCH_NAME=MySearch
MYSEARCH_TIMEOUT_SECONDS=45
```

### Proxy-first

```env
MYSEARCH_PROXY_BASE_URL=
MYSEARCH_PROXY_API_KEY=
```

### 运行时优化参数（v0.1.5+）

```env
MYSEARCH_MAX_PARALLEL_WORKERS=4
MYSEARCH_SEARCH_CACHE_TTL_SECONDS=30
MYSEARCH_EXTRACT_CACHE_TTL_SECONDS=300
```

含义：

- `MYSEARCH_MAX_PARALLEL_WORKERS`
  - 控制混合查询与 `research` 中并行请求的工作线程数。
- `MYSEARCH_SEARCH_CACHE_TTL_SECONDS`
  - 控制 `search` 缓存生存时间。
- `MYSEARCH_EXTRACT_CACHE_TTL_SECONDS`
  - 控制 `extract_url` 缓存生存时间。

返回结构的新增字段：

- `route_debug`
  - 展示路由决策、解析后的 sources、cache 命中状态等调试信息。
- `cache`
  - 展示当前请求是否命中缓存与 TTL。

`mysearch_health` 新增：

- `runtime`
- `routing_defaults`
- `cache`

### MCP 传输配置

```env
MYSEARCH_MCP_HOST=127.0.0.1
MYSEARCH_MCP_PORT=8000
MYSEARCH_MCP_MOUNT_PATH=/
MYSEARCH_MCP_SSE_PATH=/sse
MYSEARCH_MCP_STREAMABLE_HTTP_PATH=/mcp
MYSEARCH_MCP_STATELESS_HTTP=false
```

### 直连 provider

可分别配置：

- `MYSEARCH_TAVILY_*`
- `MYSEARCH_FIRECRAWL_*`
- `MYSEARCH_EXA_*`
- `MYSEARCH_XAI_*`

完整示例见：
[.env.example](./.env.example)

## 什么时候该看别的文档

- 你要部署控制台和 token 管理：
  看 [../proxy/README.md](../proxy/README.md)
- 你要让 AI 自动理解怎么安装：
  看 [../skill/README.md](../skill/README.md)
- 你要装到 OpenClaw：
  看 [../openclaw/README.md](../openclaw/README.md)
