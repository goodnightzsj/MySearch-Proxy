"""Stable MCP output fields; provider-specific additions remain intact."""

from typing import Any

from pydantic import BaseModel, ConfigDict


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)


class SearchResult(ToolResult):
    provider: str
    results: list[dict[str, Any]]
    citations: list[dict[str, Any]]


class ExtractResult(ToolResult):
    provider: str
    url: str
    content: str
    metadata: dict[str, Any]


class ResearchResult(ToolResult):
    provider: str
    query: str
    web_search: dict[str, Any]
    pages: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    summary: str


class MapResult(ToolResult):
    provider: str
    url: str
    links: list[dict[str, Any]]
    count: int
    metadata: dict[str, Any]


class CrawlResult(ToolResult):
    provider: str
    url: str
    status: str
    pages: list[dict[str, Any]]
    count: int
    metadata: dict[str, Any]


class HealthResult(ToolResult):
    server_name: str
    providers: dict[str, Any]
    timeout_seconds: int
