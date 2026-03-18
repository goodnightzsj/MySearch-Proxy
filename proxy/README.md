# MySearch Proxy Console

[English Guide](./README_EN.md) · [返回仓库](../README.md)

`proxy/` 是 MySearch 的控制台与代理层。

它不是单纯的 key 面板，而是整套 `proxy-first` 架构的中间层：

- 上游连 Tavily / Firecrawl / Exa / 可选 Social
- 下游给 MySearch MCP、OpenClaw skill 和其他 Agent 发统一 token
- 页面里同时看 key 池、token 池、调用统计和额度信息

![MySearch Console Hero](../docs/images/mysearch-console-hero.jpg)

## 它解决什么问题

如果没有 Proxy，常见问题会很散：

- 每台客户端都要单独填 provider key
- OpenClaw 和本地 Codex 的配置容易分叉
- token、额度、调用统计没有统一入口
- 上游 provider 一旦换地址，所有客户端都要跟着改
- Social / X 这条链很难跟 Web / Docs 搜索放在一个控制平面里

`MySearch Proxy` 的目标就是把这些收回来。

## 支持的能力

### Tavily

代理入口：

- `POST /api/search`
- `POST /api/extract`

控制台能力：

- key 池
- token 池
- 使用量同步
- 调用统计

### Firecrawl

代理入口：

- `POST /firecrawl/v2/search`
- `POST /firecrawl/v2/scrape`

控制台能力：

- key 池
- token 池
- credits 同步
- 调用统计

### Exa

代理入口：

- `POST /exa/search`

控制台能力：

- key 池
- token 池
- 调用统计

说明：

- Exa 当前在控制台里支持接入和分发
- 实时官方额度暂时无法查询，所以页面会明确标注这一点

### MySearch 通用 token

控制台能力：

- 创建 `mysp-` 开头的 MySearch token
- 一次接通 Tavily / Firecrawl / Exa
- 给 `mysearch/.env` 和 OpenClaw skill 直接复用
- 记录这类 token 的调用统计

### Social / X

代理入口：

- `GET /social/health`
- `POST /social/search`

控制台能力：

- 上游 base URL 管理
- gateway token 管理
- 兼容 admin API 对接
- token 状态展示

## 当前推荐用法

推荐你把它当成统一入口，而不是单独使用某一个 provider 工作台。

标准链路：

```text
上游 provider
  -> MySearch Proxy
     -> 生成 mysp- token
        -> MySearch MCP / OpenClaw skill / 其他 Agent
```

客户端只需要：

```env
MYSEARCH_PROXY_BASE_URL=https://your-mysearch-proxy.example.com
MYSEARCH_PROXY_API_KEY=mysp-...
```

## 部署

### 方式 A：直接跑 Docker Hub 镜像

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

访问：

```text
http://localhost:9874
```

### 方式 B：docker compose

```bash
cd proxy
docker compose up -d
```

### 方式 C：本地源码运行

```bash
cd proxy
pip install -r requirements.txt
ADMIN_PASSWORD=change-me uvicorn server:app --host 0.0.0.0 --port 9874
```

## 首次初始化建议

第一次打开页面后，按这个顺序做最稳：

1. 用 `ADMIN_PASSWORD` 登录控制台
2. 添加 Tavily / Firecrawl / Exa 的上游 key
3. 如果你要 Social / X，再补它的 upstream 配置
4. 执行一轮 usage sync
5. 创建 MySearch 通用 token
6. 把 `MYSEARCH_PROXY_BASE_URL` 和 `MYSEARCH_PROXY_API_KEY` 填给客户端

当前控制台已经带密码登录，不再适合匿名裸放在公网。

## 下游怎么接

### 给 `mysearch/` MCP

```env
MYSEARCH_PROXY_BASE_URL=https://your-mysearch-proxy.example.com
MYSEARCH_PROXY_API_KEY=mysp-...
```

### 给 OpenClaw skill

```json
{
  "skills": {
    "entries": {
      "mysearch": {
        "enabled": true,
        "env": {
          "MYSEARCH_PROXY_BASE_URL": "https://your-mysearch-proxy.example.com",
          "MYSEARCH_PROXY_API_KEY": "mysp-..."
        }
      }
    }
  }
}
```

## 页面与数据

控制台页面会按服务拆成独立区域：

- Tavily
- Exa
- Firecrawl
- Social / X
- MySearch 通用 token

这样做的目的是：

- 各服务额度不会混在一起
- token 不会串用
- 调用统计更清楚
- 下游接线一眼能看懂

界面预览：

![MySearch Console Workspaces](../docs/images/mysearch-console-workspaces.jpg)

默认数据目录：

- Docker compose
  - `./data`
- `docker run` 示例
  - `$(pwd)/mysearch-proxy-data`

## 认证与安全

关键环境变量：

```env
ADMIN_PASSWORD=change-me
ADMIN_SESSION_COOKIE=mysearch_proxy_session
ADMIN_SESSION_MAX_AGE=2592000
```

建议：

- 第一时间改掉默认管理员密码
- 放公网时务必配 HTTPS 反代
- 不要把生产上游 key 暴露到前端代码仓库
- 只把 `mysp-` token 发给下游客户端

## 支持的 API

管理和面板相关：

- `GET /`
- `GET /api/session`
- `POST /api/session/login`
- `POST /api/session/logout`
- `GET /api/stats`
- `GET /api/settings`
- `PUT /api/settings/social`
- `GET /api/keys`
- `POST /api/keys`
- `GET /api/tokens`
- `POST /api/tokens`
- `POST /api/usage/sync`

搜索代理相关：

- `POST /api/search`
- `POST /api/extract`
- `POST /firecrawl/v2/search`
- `POST /firecrawl/v2/scrape`
- `POST /exa/search`
- `GET /social/health`
- `POST /social/search`

## 什么时候看别的文档

- 你要安装 MCP：
  看 [../mysearch/README.md](../mysearch/README.md)
- 你要给 AI 安装 skill：
  看 [../skill/README.md](../skill/README.md)
- 你要装 OpenClaw bundle：
  看 [../openclaw/README.md](../openclaw/README.md)
