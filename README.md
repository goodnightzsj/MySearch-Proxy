# MySearch Proxy

[English Guide](./README_EN.md)

`MySearch Proxy` 是一套给 AI 助手准备的统一搜索栈。

它把原本分散的 4 件事收成了同一个仓库：

- `mysearch/`
  - 真正可安装的 MySearch MCP
- `skill/`
  - 给 Codex / Claude Code 的 skill 与安装说明
- `openclaw/`
  - 给 OpenClaw / ClawHub 的独立 skill bundle
- `proxy/`
  - 给团队或公开部署使用的控制台与代理层

支持的搜索能力：

- Tavily
- Firecrawl
- Exa
- 可选 X / Social

目标很简单：

- 让本地 AI 助手先用起来
- 让 OpenClaw 直接装上去
- 让团队共享一套统一搜索后端
- 让调用方尽量少关心底层 provider 差异

项目入口：

- GitHub：
  [skernelx/MySearch-Proxy](https://github.com/skernelx/MySearch-Proxy)
- Docker Hub：
  [skernelx/mysearch-proxy](https://hub.docker.com/r/skernelx/mysearch-proxy)
- ClawHub：
  [clawhub.ai/skernelx/mysearch](https://clawhub.ai/skernelx/mysearch)

![MySearch Console Hero](./docs/images/mysearch-console-hero.jpg)

## 为什么做这个项目

很多搜索类项目只解决其中一小段：

- 只给一个 `web_search`
- 只会搜，不会抓正文
- 只会调官方 API，不方便接自建网关
- 只给 prompt，不给真正能安装的运行时
- 只做 key 面板，不解决 AI 如何调用

`MySearch Proxy` 选择直接把整条链补齐：

```text
上游 provider / 聚合网关
  -> Tavily / Firecrawl / Exa / X / Social

MySearch Proxy
  -> 控制台、Token、额度同步、兼容代理接口

MySearch MCP / Codex Skill / OpenClaw Skill
  -> 给 Codex、Claude Code、OpenClaw、其他 Agent 直接使用
```

## 推荐架构

当前最推荐的是 `proxy-first`：

```text
上游 provider
  -> MySearch Proxy
     -> 生成 MySearch 通用 token
        -> MySearch MCP / OpenClaw skill / 其他 Agent
```

这条路的好处很直接：

- 客户端只需要一组 `MYSEARCH_PROXY_*`
- Tavily / Firecrawl / Exa 不再散落到每台机器
- 可以统一管理 token、调用统计和额度同步
- OpenClaw、本地 Codex、团队代理都能复用同一套配置

如果你暂时还没有 Proxy，也可以让 `mysearch/` 或 `openclaw/` 直接连官方
provider。

## 最新优化（v0.1.6）

这次版本重点是继续收紧运行时稳定性，同时把 Social / X 的模型 fallback 正式做进运行时。

- 路由与参数稳定性：
  - MCP 入口现在兼容单字符串形式的 `sources`、`include_domains`、`formats` 等参数。
  - 显式指定 `provider` 时，不会再被 `balanced` 策略偷偷混成 `hybrid`。
- 抓取质量修复：
  - `extract_url(auto)` 现在会识别更多假正文，并按需回退。
  - 已覆盖 `linux.do` anti-bot 提示、验证码挑战页、GitHub blob 页面壳等场景。
  - GitHub 公开仓库 `blob` 页面在 `auto` 模式下会优先改写到 raw 地址直接抓正文。
- 社交结果一致性：
  - 日期过滤后如果没有命中，`results / citations / answer` 会保持一致，不再残留旧内容。
- Social / X fallback：
  - `social/search` 现在支持主模型结果过少或上游报错时自动 fallback。
  - 返回里新增 `route` 元信息，能直接看到实际选中的模型、fallback 是否触发，以及每轮尝试结果数。
  - 推荐线上配置改为：主模型 `grok-3-mini`，fallback `grok-4.1-fast`，阈值 `3`。

- 并行执行优化：
  - `search` 的混合分支和 `research` 工作流支持并行请求，减少长尾等待。
- 内存缓存：
  - 为 `search` 和 `extract` 增加 TTL 缓存，重复查询会显著更快。
- 调试可见性：
  - `search` 返回新增 `route_debug`，明确路由决策和是否命中缓存。
  - `search` / `extract` 返回新增 `cache` 字段，直接看到 `hit` 与 `ttl_seconds`。
- 健康检查增强：
  - `mysearch_health` / `health` 现在会返回 `runtime`、`routing_defaults`、`cache`。
- OpenClaw 同步：
  - `openclaw` bundle 将随本次发布同步到 `mysearch@0.1.6`。

新增运行时参数：

```env
MYSEARCH_MAX_PARALLEL_WORKERS=4
MYSEARCH_SEARCH_CACHE_TTL_SECONDS=30
MYSEARCH_EXTRACT_CACHE_TTL_SECONDS=300
```

说明：

- 终端里单次 CLI 调用通常是新进程，内存缓存不会跨进程复用。
- 常驻服务模式下缓存才会持续生效。

## 从哪里开始

按你的使用场景直接走：

- 只想让本机 Codex / Claude Code 先用起来：
  看 [mysearch/README.md](./mysearch/README.md)
- 想让 AI 自动理解怎么安装和使用：
  看 [skill/README.md](./skill/README.md)
- 想给 OpenClaw / ClawHub 安装独立搜索 skill：
  看 [openclaw/README.md](./openclaw/README.md)
- 想部署控制台、管理 key / token / 额度：
  看 [proxy/README.md](./proxy/README.md)

## 5 分钟快速开始

### 路线 A：本机直接安装 MySearch MCP

```bash
cd /path/to/MySearch-Proxy
python3 -m venv venv
cp mysearch/.env.example mysearch/.env
```

推荐填法：

```env
MYSEARCH_PROXY_BASE_URL=https://your-mysearch-proxy.example.com
MYSEARCH_PROXY_API_KEY=mysp-...
```

安装：

```bash
./install.sh
```

验收：

```bash
python3 skill/scripts/check_mysearch.py --health-only
python3 skill/scripts/check_mysearch.py --web-query "OpenAI latest announcements"
```

### 路线 B：先部署 Proxy，再让所有客户端复用

```bash
mkdir -p mysearch-proxy-data

docker run -d \
  --name mysearch-proxy \
  --restart unless-stopped \
  -p 9874:9874 \
  -e ADMIN_PASSWORD=change-me \
  -v $(pwd)/mysearch-proxy-data:/app/data \
  skernelx/mysearch-proxy:latest
```

部署后：

1. 登录控制台
2. 添加 Tavily / Firecrawl / Exa / Social 上游配置
3. 创建 MySearch 通用 token
4. 把这个 token 填给 `mysearch/.env` 或 OpenClaw skill env

## 目录说明

### `mysearch/`

真正可运行的 MCP 服务。

提供 4 个工具：

- `search`
- `extract_url`
- `research`
- `mysearch_health`

支持：

- `stdio`
- `streamableHTTP`
- `sse`

详细说明见：
[mysearch/README.md](./mysearch/README.md)

### `skill/`

这层不是 MCP 实现，而是给 AI 助手看的安装与使用说明。

适合：

- Codex 自动安装
- Claude Code 按 README + SKILL 完成接线

详细说明见：
[skill/README.md](./skill/README.md)

### `openclaw/`

这是单独打包的 OpenClaw skill bundle。

特点：

- 自带 runtime
- 可本地安装
- 可发布到 ClawHub
- 推荐通过 skill env 注入 `MYSEARCH_PROXY_*`

详细说明见：
[openclaw/README.md](./openclaw/README.md)

### `proxy/`

这是整套系统的控制台与代理层。

负责：

- Provider key 池
- MySearch token 池
- 调用统计
- 官方额度同步
- `/social/search` 兼容入口

详细说明见：
[proxy/README.md](./proxy/README.md)

## 路由策略

MySearch 默认不是“所有问题都塞给一个 provider”。

当前推荐理解方式：

- `web / news`
  - 优先 Tavily
- `docs / github / pdf / pricing / changelog`
  - 优先 Firecrawl
- 普通网页补充发现
  - 可回退 Exa
- `social`
  - 走 xAI 或兼容 `/social/search`
- `extract_url`
  - 优先 Firecrawl，失败或正文为空时回退 Tavily extract
- `research`
  - 先搜索，再抓取正文，再可选补 Social / X

## 适合哪些场景

- 本地开发助手的默认搜索入口
- OpenClaw 的默认搜索 skill
- 多个 Agent 共用的一套统一搜索后端
- 你已经有 Tavily / Firecrawl / xAI 上游，想统一收口

## 文档地图

- 总体架构：
  [docs/mysearch-architecture.md](./docs/mysearch-architecture.md)
- MCP：
  [mysearch/README.md](./mysearch/README.md)
- OpenClaw：
  [openclaw/README.md](./openclaw/README.md)
- Proxy：
  [proxy/README.md](./proxy/README.md)
- Codex / Claude Code skill：
  [skill/README.md](./skill/README.md)

## 当前公开页面

- Docker Hub：
  [skernelx/mysearch-proxy](https://hub.docker.com/r/skernelx/mysearch-proxy)
- ClawHub：
  [clawhub.ai/skernelx/mysearch](https://clawhub.ai/skernelx/mysearch)

下图是公开页面的历史截图，实时状态请以线上页面为准：

![MySearch Skill Security Scan](./docs/images/mysearch-skill-security-scan.jpg)
