# MySearch Proxy

`MySearch Proxy` 是一个面向 `Codex`、`Claude Code`、`OpenClaw` 和自建
Agent 工作流的通用搜索基础设施项目。

它不是“再包一层 Tavily API”的单点工具，而是一整套围绕搜索落地的完整方案：

- `mysearch/`
  - 真正可安装的搜索 MCP
  - 统一聚合 `Tavily + Firecrawl + X / Social`
- `proxy/`
  - 可视化控制台与统一代理层
  - Key 池、Token 池、额度同步、`/social/search`
- `skill/`
  - 给 `Codex` / `Claude Code` 用的 MySearch Skill
- `openclaw/`
  - 给 `OpenClaw Hub` 用的独立 skill
  - 已做成 bundled runtime 形式，便于公开分发和审计

项目入口：

- GitHub: [skernelx/MySearch-Proxy](https://github.com/skernelx/MySearch-Proxy)
- OpenClaw Hub Skill: [clawhub.ai/skernelx/mysearch](https://clawhub.ai/skernelx/mysearch)
- Tavily / Firecrawl provider 基础设施推荐搭配：
  [skernelx/tavily-key-generator](https://github.com/skernelx/tavily-key-generator)

默认推荐组合不是“手动东拼西凑几个 key”，而是：

- `MySearch Proxy` 负责 MCP、Skill、OpenClaw Skill、Proxy Console
- `tavily-key-generator` 负责 Tavily / Firecrawl provider 来源、key 管理和聚合 API

## 这个项目到底在做什么

`MySearch Proxy` 的目标，不是把所有搜索源硬塞进一个“万能接口”，而是把
真正的搜索工作流拆成四层：

1. 决策层
   - 什么时候搜索
   - 用哪种模式
   - 结果怎么组织
2. MCP / Skill 层
   - `search`
   - `extract_url`
   - `research`
3. Provider 层
   - Tavily
   - Firecrawl
   - official xAI
   - compatible social gateway
4. Proxy / Console 层
   - 给团队统一接线
   - 做 key、token、额度和服务状态管理

简单说：

- 如果你只想给本地 Agent 一个更强的搜索入口，用 `mysearch/`
- 如果你要给 AI 一个“会自动选搜索方式”的 skill，用 `skill/` 或 `openclaw/`
- 如果你要团队共用一套搜索网关和控制台，用 `proxy/`

推荐的完整落地方式：

```text
tavily-key-generator
  -> 提供 Tavily / Firecrawl key、聚合 API、provider source

MySearch Proxy
  -> 负责 MCP、Skill、OpenClaw Skill、Proxy Console、Social / X 接线
```

也就是说，`MySearch Proxy` 不是要替代 `tavily-key-generator`，而是默认把它
当成 Tavily / Firecrawl 这一层的最佳搭档。

## 和同类项目相比，它的优势在哪里

和常见的几类同类工具相比，`MySearch Proxy` 的优势不是“某一个 provider
更强”，而是整体链路更完整。

### 对比单一搜索源 MCP

常见问题：

- 只会搜网页，不会抓正文
- 只适合新闻，不适合 docs / GitHub / PDF
- X / Social 要靠额外脚本拼接

`MySearch` 的优势：

- 网页发现和新闻优先 Tavily
- docs / GitHub / PDF / pricing / changelog 优先 Firecrawl
- X / Social 可以走 official xAI，也可以走 compatible `/social/search`
- `extract_url` 和 `research` 是一等能力，不只是“搜完给几个链接”

### 对比只做 Skill 的项目

常见问题：

- skill 会告诉 AI“怎么搜”，但没有真正可复用的 MCP
- 一旦换运行环境，就要重写提示词和脚本

`MySearch Proxy` 的优势：

- 同一个仓库里同时提供 `MCP + Skill + OpenClaw Skill`
- 本地 Agent、OpenClaw、团队网关可以共用同一套搜索逻辑
- skill 不是孤立文档，而是和真实运行时绑定的

### 对比只做 Proxy / Key 面板的项目

常见问题：

- 只管 key，不管 AI 怎么调用
- 只适合人工维护，不适合直接接到 Agent

`MySearch Proxy` 的优势：

- Proxy 不是孤立控制台，而是服务于 MySearch MCP / Skill
- 同一套仓库里可以同时解决：
  - provider 接入
  - AI 调用
  - skill 安装
  - OpenClaw 发布

### 对比 X / Social 专用封装

常见问题：

- 只解决“怎么搜 X”，但不解决文档、正文、网页证据
- 一旦没有 X key，整套工具价值大幅下降

`MySearch Proxy` 的优势：

- X / Social 是可选增强项，不是安装门槛
- 没有 X 时，`web / docs / extract / research` 仍然可正常工作
- 只有明确的 `social` 路由会受影响，不会把整个系统一起拖挂

## 可以用在哪里

这套项目适合下面几类场景：

### 1. 本地 AI 助手的默认搜索入口

适合：

- `Codex`
- `Claude Code`
- 其他能挂 MCP 的开发助手

用途：

- 最新网页搜索
- 技术文档 / GitHub / 价格页 / changelog 检索
- 单页正文抓取
- 小型研究包
- X / Social 舆情补充

### 2. OpenClaw 的默认搜索 skill

适合：

- 想替换旧的 Tavily-only skill
- 想让 OpenClaw 拥有更完整的 web + docs + social 搜索能力
- 想通过 ClawHub 分发 skill

### 3. 团队共享搜索网关

适合：

- 多个下游程序共用一套 Tavily / Firecrawl / Social 接口
- 希望分离上游 key 和下游 token
- 需要代理统计、额度同步、控制台管理

### 4. 自己的聚合 API / compatible 网关接线

适合：

- 你有自己的 Tavily / Firecrawl 聚合 API
- 你有 `grok2api` 或其他 xAI-compatible 服务
- 你想让 MySearch 统一接这些后端，而不是把调用逻辑散在多个脚本里

## Provider 支持与缺失后的行为

推荐最小组合是：

- `Tavily + Firecrawl`

`X / Social` 是可选第三个 provider。

| Provider | 主要负责什么 | 默认推荐怎么接 | 缺失时会怎样 |
| --- | --- | --- | --- |
| Tavily | 普通网页、新闻、快速 answer、默认 research 发现 | 官方 API，或通过 `tavily-key-generator` 提供聚合 API / provider source | `web` / `news` / 默认 `research` 会受影响；`docs/github/pdf` 仍可走 Firecrawl |
| Firecrawl | docs、GitHub、PDF、pricing、changelog、正文抓取 | 官方 API，或通过 `tavily-key-generator` 提供聚合 API / provider source | `docs/github/pdf/resource` 能力变弱；`extract_url` 会回退 Tavily extract；普通网页仍可走 Tavily |
| xAI / Social | X / Social 搜索、舆情、开发者讨论 | 官方 xAI，或 compatible `/social/search` | `mode=\"social\"` 不可用；`research(include_social=true)` 仍会返回网页结果并附带 `social_error` |

额外说明：

- 只有 `xAI` 并不能替代整套 MySearch 的 web / docs 能力
- 只有 `Firecrawl` 时，`docs / extract` 仍可做，但普通 `web/news` 不完整
- 只有 `Tavily` 时，网页搜索仍可做，但 docs / GitHub / PDF / 正文抓取体验会下降
- 如果你缺 `Tavily` 或 `Firecrawl` 官方 key，默认推荐不是“先放弃”，而是先接
  [tavily-key-generator](https://github.com/skernelx/tavily-key-generator)

## 当前项目状态

这个仓库已经完整覆盖：

- `MCP server`
- `Proxy Console`
- `Codex / Claude Code skill`
- `OpenClaw Hub skill`

截至 `2026-03-17`，公开可验证的 OpenClaw Hub skill 状态是：

- `mysearch@0.1.1`
- `Security Scan -> OpenClaw: Benign`
- `clawhub inspect` 中 `security.status = clean`
- `clawhub inspect` 中 `scanners.llm.verdict = benign`

说明：

- README 里引用的安全结论，以 ClawHub skill 页面和 `clawhub inspect` 的真实输出为准
- 当前公开页面展示的是 `Security Scan`
- 它不一定和社区里流传的“多维打分 / 平均分”评测卡是同一种页面样式

安全检查截图：

![MySearch Skill Security Scan](./docs/images/mysearch-skill-security-scan.jpg)

## 界面预览

### 首屏

![MySearch Console Hero](./docs/images/mysearch-console-hero.jpg)

### 工作台

![MySearch Console Workspaces](./docs/images/mysearch-console-workspaces.jpg)

## 运行前你需要准备什么

按使用方式不同，所需支持不一样。

### A. 安装 MCP 到 Codex / Claude Code

需要：

- `python3`
- 可访问 Tavily / Firecrawl / xAI 或 compatible 网关的网络环境
- `codex` 或 `claude` CLI（至少有一个）

### B. 跑 Proxy 控制台

需要：

- `Docker` / `docker compose`
  或
- Python + `uvicorn`

### C. 安装 OpenClaw skill

需要：

- OpenClaw 环境
- 如果走 Hub：`clawhub` CLI
- 如果走仓库本地安装：`python3`

## 安装方式

你可以按自己的目标选一条安装路径，不需要所有部分都装。

### 1. 给 Codex / Claude Code 安装 MySearch MCP

推荐流程：

```bash
python3 -m venv venv
cp mysearch/.env.example mysearch/.env
```

先填最小配置：

```env
MYSEARCH_TAVILY_API_KEY=tvly-...
MYSEARCH_FIRECRAWL_API_KEY=fc-...
```

然后执行：

```bash
./install.sh
```

这个安装脚本会：

1. 安装 `mysearch/requirements.txt`
2. 检测本机是否有 `Claude Code`
3. 检测本机是否有 `Codex`
4. 把 `mysearch/.env` 里的 `MYSEARCH_*` 自动注入到 MCP 注册项

验收：

```bash
claude mcp list
codex mcp list
```

### 2. 给 Codex 安装配套 skill

如果你不只是要 MCP，还要让 AI 自动理解 MySearch 的使用规则，再装
`skill/`：

```bash
bash skill/scripts/install_codex_skill.sh
```

如果目标目录已存在：

```bash
bash skill/scripts/install_codex_skill.sh --force
```

### 3. 给 OpenClaw 安装 MySearch skill

#### 方式 A：从 ClawHub 安装

```bash
clawhub install mysearch
```

如果已经装过：

```bash
clawhub update mysearch
```

说明：

- 安装目录由你的 OpenClaw / ClawHub 环境决定
- 安装后请把 skill 的 `.env` 配到对应目录里
- 如果你已有旧目录，建议保留配置文件再覆盖更新

#### 方式 B：从仓库本地安装

```bash
cp openclaw/.env.example openclaw/.env
# 编辑 openclaw/.env

bash openclaw/scripts/install_openclaw_skill.sh \
  --install-to ~/.openclaw/skills/mysearch \
  --copy-env openclaw/.env
```

验收：

```bash
python3 ~/.openclaw/skills/mysearch/scripts/mysearch_openclaw.py health
```

### 4. 部署 Proxy 控制台

最简单方式：

```bash
cd proxy
docker compose up -d
```

或者直接跑镜像：

```bash
docker run -d \
  --name mysearch-proxy \
  --restart unless-stopped \
  -p 9874:9874 \
  -e ADMIN_PASSWORD=your-admin-password \
  -v $(pwd)/mysearch-proxy-data:/app/data \
  your-registry/mysearch-proxy:latest
```

启动后访问：

```text
http://localhost:9874
```

## 配置方式

### 最小配置

这是最推荐的起步方式：

```env
MYSEARCH_TAVILY_API_KEY=tvly-...
MYSEARCH_FIRECRAWL_API_KEY=fc-...
```

如果你不想直接填官方 key，或者你本来就准备走聚合 API，默认推荐先部署：

- [skernelx/tavily-key-generator](https://github.com/skernelx/tavily-key-generator)

然后把它暴露出来的 Tavily / Firecrawl 入口接到 MySearch：

```env
MYSEARCH_TAVILY_BASE_URL=https://your-search-gateway.example.com
MYSEARCH_TAVILY_SEARCH_PATH=/api/search
MYSEARCH_TAVILY_EXTRACT_PATH=/api/extract
MYSEARCH_TAVILY_AUTH_MODE=bearer
MYSEARCH_TAVILY_API_KEY=your-token

MYSEARCH_FIRECRAWL_BASE_URL=https://your-search-gateway.example.com
MYSEARCH_FIRECRAWL_SEARCH_PATH=/firecrawl/v2/search
MYSEARCH_FIRECRAWL_SCRAPE_PATH=/firecrawl/v2/scrape
MYSEARCH_FIRECRAWL_AUTH_MODE=bearer
MYSEARCH_FIRECRAWL_API_KEY=your-token
```

这时你就已经可以正常使用：

- `search(mode="web")`
- `search(mode="news")`
- `search(mode="docs")`
- `extract_url(...)`
- `research(...)`

### 官方 xAI 模式

如果你有官方 xAI key：

```env
MYSEARCH_XAI_BASE_URL=https://api.x.ai/v1
MYSEARCH_XAI_RESPONSES_PATH=/responses
MYSEARCH_XAI_SEARCH_MODE=official
MYSEARCH_XAI_API_KEY=xai-...
```

### compatible / 自定义 social gateway 模式

如果你要把 X / Social 路由接到自己的 compatible 网关：

```env
MYSEARCH_XAI_BASE_URL=https://media.example.com/v1
MYSEARCH_XAI_SOCIAL_BASE_URL=https://your-social-gateway.example.com
MYSEARCH_XAI_SEARCH_MODE=compatible
MYSEARCH_XAI_API_KEY=your-gateway-token
```

说明：

- `MYSEARCH_XAI_BASE_URL` 指向模型 / `/responses` 网关
- `MYSEARCH_XAI_SOCIAL_BASE_URL` 指向 social gateway 根地址
- MySearch 默认自动追加 `/social/search`

### 使用自己的 Tavily / Firecrawl 聚合 API

如果你不想直连官方 API，也可以覆盖 `BASE_URL / PATH / AUTH_*`：

```env
MYSEARCH_TAVILY_BASE_URL=https://your-search-gateway.example.com
MYSEARCH_TAVILY_SEARCH_PATH=/api/search
MYSEARCH_TAVILY_EXTRACT_PATH=/api/extract
MYSEARCH_TAVILY_AUTH_MODE=bearer
MYSEARCH_TAVILY_API_KEY=your-token

MYSEARCH_FIRECRAWL_BASE_URL=https://your-search-gateway.example.com
MYSEARCH_FIRECRAWL_SEARCH_PATH=/firecrawl/v2/search
MYSEARCH_FIRECRAWL_SCRAPE_PATH=/firecrawl/v2/scrape
MYSEARCH_FIRECRAWL_AUTH_MODE=bearer
MYSEARCH_FIRECRAWL_API_KEY=your-token
```

## 常见问题

### 没有 X / Social API，项目还能用吗

能。

这时你仍然可以正常使用：

- 网页搜索
- 新闻搜索
- docs / GitHub / PDF
- 正文抓取
- research

只有：

- `search(mode="social")`
- 明确依赖 X 结果的工作流

会不可用。

### 缺 Firecrawl 会怎样

影响最大的是：

- `docs`
- `github`
- `pdf`
- `pricing`
- `changelog`
- 正文抓取质量

但普通网页搜索和新闻仍然可以由 Tavily 承担。

如果你缺的不是能力，而是官方 key，默认推荐先接
[tavily-key-generator](https://github.com/skernelx/tavily-key-generator)，
把 Firecrawl provider 从它那一层补进来，而不是直接把 Firecrawl 整块拿掉。

### 缺 Tavily 会怎样

影响最大的是：

- 普通 `web`
- `news`
- 默认 `research` 的发现阶段

但 docs / GitHub / PDF / extract 仍可以由 Firecrawl 承担一部分。

同样地，如果你缺的是 Tavily 官方 key，默认推荐先接
[tavily-key-generator](https://github.com/skernelx/tavily-key-generator)，
把 Tavily provider 层补上，而不是直接退回单一 Firecrawl 方案。

### 只想把它当 X 搜索工具用可以吗

可以，但不推荐把它缩成单一 X 工具。

`MySearch` 的价值在于：

- web
- docs
- extract
- social

四条能力线一起协同，而不是只保留一个 provider。

## 维护与发布

如果你改了 `mysearch/` 的主代码，推荐先同步 runtime 到 OpenClaw skill：

```bash
bash scripts/release_openclaw_skill.sh --sync-only
```

这个脚本会自动：

- 同步 `mysearch/*.py` 到 `openclaw/runtime/mysearch/`
- 清理 OpenClaw skill 里的缓存
- 跑一轮 `py_compile + health` smoke test

真正发布新的 OpenClaw Hub 版本时：

```bash
bash scripts/release_openclaw_skill.sh \
  --version 0.1.2 \
  --changelog "Bundle refreshed runtime and docs"
```

发布后脚本会自动再跑一次 `clawhub inspect`，把 `security.status` 和
`llm.verdict` 打出来，方便你确认公开安全状态。

## 仓库结构

```text
MySearch-Proxy/
├── docs/
│   └── mysearch-architecture.md
├── mysearch/
├── openclaw/
├── proxy/
├── scripts/
├── skill/
└── install.sh
```

## 相关文档

- [mysearch/README.md](./mysearch/README.md)
- [mysearch/README_EN.md](./mysearch/README_EN.md)
- [openclaw/SKILL.md](./openclaw/SKILL.md)
- [proxy/README.md](./proxy/README.md)
- [docs/mysearch-architecture.md](./docs/mysearch-architecture.md)
- [skill/SKILL.md](./skill/SKILL.md)
