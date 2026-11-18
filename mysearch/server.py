"""MySearch MCP Server."""

from __future__ import annotations

import atexit
import copy
from dataclasses import replace
from functools import partial
from threading import Event
from typing import Annotated, Literal

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
from pydantic_core import to_json
from starlette.applications import Starlette
from starlette.types import ASGIApp, Receive, Scope, Send

from mysearch.clients import MySearchClient
from mysearch.config import MCPTransport, MySearchConfig
from mysearch.diagnostics import remaining_seconds, request_context
from mysearch.mcp_http import gzip_json_responses
from mysearch.mcp_results import CrawlResult, ExtractResult, HealthResult, MapResult, ResearchResult, SearchResult


OUTPUT_MODELS = {
    "search": SearchResult,
    "extract_url": ExtractResult,
    "research": ResearchResult,
    "map_site": MapResult,
    "crawl_site": CrawlResult,
    "health": HealthResult,
}


async def _close_client(client: MySearchClient) -> None:
    with anyio.CancelScope(shield=True):
        await anyio.to_thread.run_sync(client.close, limiter=anyio.CapacityLimiter(1))


class _ClientLifetimeMiddleware:
    """Own shared resources at ASGI application scope, not MCP session scope."""

    def __init__(self, app: ASGIApp, client: MySearchClient) -> None:
        self.app, self.client = app, client

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "lifespan":
            await self.app(scope, receive, send)
            return
        closed = False

        async def close():
            nonlocal closed
            if not closed:
                closed = True
                await _close_client(self.client)

        async def send_lifespan(message):
            if message["type"] in {"lifespan.shutdown.complete", "lifespan.shutdown.failed", "lifespan.startup.failed"}:
                # The host may exit as soon as it receives this acknowledgement.
                await close()
            await send(message)

        try:
            await self.app(scope, receive, send_lifespan)
        finally:
            await close()


class _MySearchMCP(FastMCP):
    def __init__(self, *args, client: MySearchClient, **kwargs):
        super().__init__(*args, **kwargs)
        self._client = client

    def sse_app(self, mount_path: str | None = None) -> Starlette:
        app = super().sse_app(mount_path)
        app.add_middleware(_ClientLifetimeMiddleware, client=self._client)
        return app

    def streamable_http_app(self) -> Starlette:
        app = super().streamable_http_app()
        app.add_middleware(gzip_json_responses)
        app.add_middleware(_ClientLifetimeMiddleware, client=self._client)
        return app

    async def run_stdio_async(self) -> None:
        try:
            await super().run_stdio_async()
        finally:
            await _close_client(self._client)


async def _offload(client, tool, ctx=None, **kwargs):
    """Include queue time in the budget and cancel future work on disconnect."""
    state = client.new_request(tool, **kwargs)
    with state.lock:
        state.claimed = True  # MCP owns finalization through output validation.
    finished = anyio.Event()
    worker_started = Event()
    result = error = None
    progress_enabled = False
    if ctx is not None:
        try:
            progress_enabled = bool(ctx.request_context.meta and ctx.request_context.meta.progressToken is not None)
        except ValueError:
            pass  # Direct SDK calls have no request context.

    def invoke():
        worker_started.set()
        worker_result = worker_error = None
        try:
            remaining_seconds()
            worker_result = getattr(client, tool)(**kwargs)
            OUTPUT_MODELS[tool].model_validate(worker_result).model_dump(mode="json")
        except BaseException as exc:
            worker_error = exc
            raise
        finally:
            # Native Task.cancel may abandon run_sync; retain ownership until I/O really ends.
            client.finish_request(state, result=worker_result, error=worker_error)
        return {**worker_result, "diagnostics": copy.deepcopy(state.final["diagnostics"])}

    async def observe():
        reported = 0
        try:
            if progress_enabled:
                await ctx.report_progress(0, message="queued")
            while True:
                phases = state.progress_snapshot()
                if progress_enabled:
                    for index in range(reported, len(phases)):
                        await ctx.report_progress(index + 1, message=phases[index])
                reported = len(phases)
                if finished.is_set():
                    break
                with anyio.move_on_after(0.1):
                    await finished.wait()
            if progress_enabled:
                await ctx.report_progress(reported + 1, message="failed" if error else "completed")
        finally:
            if not finished.is_set():
                state.cancel()

    try:
        async with anyio.create_task_group() as group:
            group.start_soon(observe)
            try:
                with request_context(state):
                    result = await anyio.to_thread.run_sync(invoke)
            except BaseException as exc:
                error = exc
                if isinstance(exc, anyio.get_cancelled_exc_class()):
                    state.cancel()
            finally:
                finished.set()
        if error is not None:
            raise error
        return result
    finally:
        if not worker_started.is_set():
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(partial(client.finish_request, state, result=result, error=error))


def _ensure_list(value: str | list | None) -> list | None:
    """兼容单值字符串输入，避免模型把单元素数组写成标量时直接校验失败。"""
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return value


def build_mcp(config: MySearchConfig) -> tuple[MySearchClient, FastMCP]:
    client = MySearchClient(config)
    atexit.register(client.close)

    mcp = _MySearchMCP(
        config.server_name,
        client=client,
        json_response=True,
        host=config.mcp_host,
        port=config.mcp_port,
        mount_path=config.mcp_mount_path,
        sse_path=config.mcp_sse_path,
        streamable_http_path=config.mcp_streamable_http_path,
        stateless_http=config.mcp_stateless_http,
    )

    read_annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

    @mcp.tool(annotations=read_annotations)
    async def search(
        query: str,
        mode: Literal["auto", "web", "news", "social", "docs", "research", "github", "pdf"] = "auto",
        intent: Literal[
            "auto",
            "factual",
            "status",
            "comparison",
            "tutorial",
            "exploratory",
            "news",
            "resource",
        ] = "auto",
        strategy: Literal["auto", "fast", "balanced", "verify", "deep"] = "auto",
        provider: Literal["auto", "tavily", "firecrawl", "exa", "xai"] = "auto",
        sources: Literal["web", "x"] | list[Literal["web", "x"]] | None = None,
        max_results: int = 5,
        include_content: bool = False,
        include_answer: bool = True,
        include_domains: str | list[str] | None = None,
        exclude_domains: str | list[str] | None = None,
        allowed_x_handles: str | list[str] | None = None,
        excluded_x_handles: str | list[str] | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        include_x_images: bool = False,
        include_x_videos: bool = False,
        ctx: Context = None,
    ) -> Annotated[CallToolResult, SearchResult]:
        """按任务自动选择 Tavily / Firecrawl / Exa / xAI。"""
        result = await _offload(
            client, "search", ctx,
            query=query,
            mode=mode,
            intent=intent,
            strategy=strategy,
            provider=provider,
            sources=_ensure_list(sources),
            max_results=max_results,
            include_content=include_content,
            include_answer=include_answer,
            include_domains=_ensure_list(include_domains),
            exclude_domains=_ensure_list(exclude_domains),
            allowed_x_handles=_ensure_list(allowed_x_handles),
            excluded_x_handles=_ensure_list(excluded_x_handles),
            from_date=from_date,
            to_date=to_date,
            include_x_images=include_x_images,
            include_x_videos=include_x_videos,
        )
        payload = SearchResult.model_validate(result).model_dump(mode="json", by_alias=True)
        return CallToolResult(content=[TextContent(type="text", text=to_json(payload).decode())], structuredContent=payload)

    @mcp.tool(annotations=read_annotations)
    async def extract_url(
        url: str,
        formats: str | list[str] | None = None,
        only_main_content: bool = True,
        provider: Literal["auto", "firecrawl", "tavily"] = "auto",
        max_age_seconds: Annotated[int, Field(ge=0, strict=True)] | None = None,
        offset: Annotated[int, Field(ge=0, strict=True)] = 0,
        max_chars: Annotated[int, Field(ge=1, strict=True)] | None = None,
        ctx: Context = None,
    ) -> ExtractResult:
        """抓取正文，默认 Firecrawl 后回退 Tavily。max_age_seconds 仅用于显式 Firecrawl，0 请求实时抓取；offset/max_chars 可分页读取，省略时返回全文。"""
        result = await _offload(
            client, "extract_url", ctx,
            url=url,
            formats=_ensure_list(formats),
            only_main_content=only_main_content,
            provider=provider,
            **({"max_age_seconds": max_age_seconds} if max_age_seconds is not None else {}),
        )
        if offset or max_chars is not None:
            content = result["content"]
            end = len(content) if max_chars is None else min(len(content), offset + max_chars)
            result = {**result, "content": content[offset:end], "content_window": {
                "offset": offset, "returned_chars": len(content[offset:end]), "total_chars": len(content),
                "next_offset": end if end < len(content) else None,
                "truncated": offset > 0 or end < len(content),
            }}
        return result

    @mcp.tool(annotations=read_annotations)
    async def research(
        query: str,
        web_max_results: int = 5,
        social_max_results: int = 5,
        scrape_top_n: int = 3,
        include_social: bool = True,
        mode: Literal["auto", "web", "news", "social", "docs", "research", "github", "pdf"] = "auto",
        intent: Literal[
            "auto",
            "factual",
            "status",
            "comparison",
            "tutorial",
            "exploratory",
            "news",
            "resource",
        ] = "auto",
        strategy: Literal["auto", "fast", "balanced", "verify", "deep"] = "auto",
        include_domains: str | list[str] | None = None,
        exclude_domains: str | list[str] | None = None,
        allowed_x_handles: str | list[str] | None = None,
        excluded_x_handles: str | list[str] | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        ctx: Context = None,
    ) -> ResearchResult:
        """小型研究工作流：网页发现 + 正文抓取 + 可选 X 舆情补充。"""
        return await _offload(
            client, "research", ctx,
            query=query,
            web_max_results=web_max_results,
            social_max_results=social_max_results,
            scrape_top_n=scrape_top_n,
            include_social=include_social,
            mode=mode,
            intent=intent,
            strategy=strategy,
            include_domains=_ensure_list(include_domains),
            exclude_domains=_ensure_list(exclude_domains),
            allowed_x_handles=_ensure_list(allowed_x_handles),
            excluded_x_handles=_ensure_list(excluded_x_handles),
            from_date=from_date,
            to_date=to_date,
        )

    @mcp.tool(annotations=read_annotations)
    async def map_site(
        url: str,
        limit: int = 50,
        search: str | None = None,
        ctx: Context = None,
    ) -> MapResult:
        """获取站点地图：返回某站点下可发现的 URL 列表（基于 Firecrawl map）。"""
        return await _offload(client, "map_site", ctx, url=url, limit=limit, search=search)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    async def crawl_site(
        url: str,
        limit: int = 20,
        max_depth: int | None = None,
        crawl_entire_domain: bool = True,
        ctx: Context = None,
    ) -> CrawlResult:
        """爬取整站：抓取某站点多页正文并清洗（基于 Firecrawl crawl，正文复用 extract 清洗）。"""
        return await _offload(
            client, "crawl_site", ctx,
            url=url,
            limit=limit,
            max_depth=max_depth,
            crawl_entire_domain=crawl_entire_domain,
        )

    @mcp.tool(annotations=read_annotations)
    async def mysearch_health(ctx: Context = None) -> HealthResult:
        """查看 MySearch 当前 provider 配置、search mode、auth 模式、base URL 和 key 可用性。"""
        return await _offload(client, "health", ctx)

    return client, mcp


def main(
    *,
    transport: MCPTransport = "stdio",
    host: str | None = None,
    port: int | None = None,
    mount_path: str | None = None,
    sse_path: str | None = None,
    streamable_http_path: str | None = None,
    stateless_http: bool | None = None,
) -> None:
    base_config = MySearchConfig.from_env()
    config = replace(
        base_config,
        mcp_host=host if host is not None else base_config.mcp_host,
        mcp_port=port if port is not None else base_config.mcp_port,
        mcp_mount_path=mount_path if mount_path is not None else base_config.mcp_mount_path,
        mcp_sse_path=sse_path if sse_path is not None else base_config.mcp_sse_path,
        mcp_streamable_http_path=streamable_http_path if streamable_http_path is not None else base_config.mcp_streamable_http_path,
        mcp_stateless_http=base_config.mcp_stateless_http if stateless_http is None else stateless_http,
    )
    client, server = build_mcp(config)
    try:
        server.run(transport=transport, mount_path=config.mcp_mount_path)
    finally:
        client.close()


if __name__ == "__main__":
    main()
