# 项目总览

## 项目定位

这个仓库不是单一 provider 的 wrapper，也不是单独的 skill 说明书，而是一套给 AI 助手使用的统一搜索栈。它把 MCP、安装说明、OpenClaw bundle、以及团队可共享的 Proxy Console 放在同一仓库中。见 `README.md:5`、`README.md:7`、`README.md:25`、`README.md:53`。

## 仓库组成

### 1. `mysearch/`

真正可安装的 MySearch MCP 服务，对外暴露 4 个工具：`search`、`extract_url`、`research`、`mysearch_health`。入口与工具注册在 `mysearch/server.py:34`、`mysearch/server.py:47`、`mysearch/server.py:97`、`mysearch/server.py:112`、`mysearch/server.py:156`；主要行为实现位于 `mysearch/clients.py:385`、`mysearch/clients.py:677`、`mysearch/clients.py:802`；配置对象在 `mysearch/config.py:221`。README 见 `mysearch/README.md:5`、`mysearch/README.md:12`。

### 2. `proxy/`

控制台与代理层，负责上游 key 池、下游 token、额度/用量同步、Social/X 网关兼容接口，以及管理页面。上游代理端点在 `proxy/server.py:1649`、`proxy/server.py:1680`、`proxy/server.py:1719`、`proxy/server.py:1783`；控制台与管理 API 在 `proxy/server.py:1877`、`proxy/server.py:1891`、`proxy/server.py:1936`、`proxy/server.py:2003`、`proxy/server.py:2075`。持久化与服务边界在 `proxy/database.py:11`、`proxy/database.py:61`、`proxy/database.py:77`、`proxy/database.py:102`，key 轮询在 `proxy/key_pool.py:9`、`proxy/key_pool.py:25`。README 见 `proxy/README.md:5`、`proxy/README.md:102`。

### 3. `skill/`

给 `Codex / Claude Code` 的安装与使用说明，不是 MCP 运行时代码。它告诉 AI 先如何安装 skill，再如何安装/验证 MySearch MCP。说明书入口在 `skill/README.md:5`、`skill/README.md:40`；调用策略在 `skill/SKILL.md:14`。安装脚本会把 skill 放到本地目录，见 `skill/scripts/install_codex_skill.sh:22`。

### 4. `openclaw/`

给 `OpenClaw / ClawHub` 的独立 skill bundle。与 `skill/` 的区别是：它自带 bundled runtime，而不是只提供说明。bundle 入口说明在 `openclaw/README.md:5`、`openclaw/README.md:14`；wrapper CLI 在 `openclaw/scripts/mysearch_openclaw.py:303`；运行时副本位于 `openclaw/runtime/mysearch/config.py:221` 等文件。README 明确它会优先走 skill env 注入，见 `openclaw/README.md:63`、`openclaw/README.md:91`。

### 5. `docs/`、`tests/`、`scripts/`

- `docs/mysearch-architecture.md` 解释层次化设计边界，见 `docs/mysearch-architecture.md:3`、`docs/mysearch-architecture.md:18`、`docs/mysearch-architecture.md:26`。
- `tests/` 覆盖配置继承、路由健康保护、Social/X 归一化与 fallback，见 `tests/test_config_bootstrap.py:39`、`tests/test_clients.py:488`、`tests/test_social_normalization.py:76`、`tests/test_social_normalization.py:171`。
- `scripts/` 目前主要放 OpenClaw 发布脚本，属于发布辅助层，不是主运行时入口。

## 推荐架构

默认推荐 `proxy-first`：上游 provider 先接到 `MySearch Proxy`，由 Proxy 统一签发 `mysp-` token，再给 `mysearch/`、`openclaw/` 或其他 Agent 复用。这样下游只需要一组 `MYSEARCH_PROXY_*`，不必在每个客户端散落 Tavily / Firecrawl / Exa / Social 配置。见 `README.md:66`、`proxy/README.md:102`、`proxy/README.md:181`、`mysearch/README.md:52`、`openclaw/README.md:28`。

直连 provider 仍然支持，但更适合本地单仓调试或尚未部署 Proxy 的场景。见 `README.md:84`、`mysearch/README.md:68`、`openclaw/README.md:44`。

## 技术栈

- `mysearch`：`mcp[cli]`、FastMCP、FastAPI、uvicorn、httpx，见 `mysearch/requirements.txt:1`。
- `proxy`：FastAPI、uvicorn、httpx、jinja2，状态落 SQLite，见 `proxy/requirements.txt:1`、`proxy/database.py:53`。
- `OpenClaw bundle`：Python wrapper + bundled `mysearch` runtime，不依赖运行时再下载远端代码，见 `openclaw/README.md:14`、`openclaw/scripts/mysearch_openclaw.py:74`。

## 对后续协作最重要的稳定事实

1. **搜索编排核心在 `mysearch/clients.py`**
   - 不是在 README，也不是在 skill 文档。改搜索行为时优先读 `mysearch/clients.py` 的 `search`、`_postprocess_search`、`_search_hybrid`、`_route_search`。
   - `search()` 已拆分为 resolve → cache → execute → `_postprocess_search`（exa rescue / rerank / official policy / evidence / xAI answer 补充 / cache write）。
2. **配置继承是产品约束，不是偶然实现**
   - 宿主配置优先、`.env` 兜底有回归测试保护，见 `mysearch/config.py:85`、`mysearch/config.py:104`、`tests/test_config_bootstrap.py:39`。
   - `MYSEARCH_TAVILY_MODE` 现在会校验合法值（`official` / `gateway`）。
3. **Proxy 是控制平面，不只是转发层**
   - 它同时负责 key 轮换、token 鉴权、用量记录、控制台与 Social/X 管理。
   - `proxy/database.py` 使用线程本地连接复用，批量导入用 `executemany`。
4. **OpenClaw 运行时是带内副本**
   - 不是直接 import 根目录 `mysearch/`。改共享行为时要判断是否同步 bundle。
5. **HTTP 层使用 `httpx.Client` 连接池**
   - `mysearch/clients.py` 已从 `urllib` 迁移到 `httpx`，支持 keep-alive 和连接复用。
