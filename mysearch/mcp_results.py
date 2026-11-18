"""Stable MCP output fields; provider-specific additions remain intact."""

from typing import Any

from pydantic import BaseModel, ConfigDict


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)


class SearchResult(ToolResult):
    provider: str
    results: list[dict[str, Any]]
    citations: list[dict[str, Any]]


def compact_search_result(result: dict[str, Any]) -> dict[str, Any]:
    """Keep final rows inline; share only identical long bodies in candidate branches."""
    branches = ("primary_search", "secondary_search")
    if not any(isinstance(result.get(name), dict) for name in branches):
        return result
    bodies: dict[str, str] = {}

    def project(branch: dict, path: str) -> dict:
        output = branch.copy()
        rows = branch.get("results")
        if isinstance(rows, list):
            projected = list(rows) if path else rows
            for index, row in enumerate(rows):
                body = row.get("content") if isinstance(row, dict) else None
                if isinstance(body, str) and len(body) >= 256:
                    target = bodies.get(body)
                    if path and target is not None and len(target) + 16 < len(body):
                        projected[index] = {**row, "content": {"$ref": target}}
                    elif target is None:
                        bodies[body] = f"{path}/results/{index}/content"
                elif path and isinstance(body, dict) and body.keys() in ({"$ref"}, {"$literal"}):
                    # Preserve an extension value that would look like an encoding marker.
                    projected[index] = {**row, "content": {"$literal": body}}
            output["results"] = projected
        for name in branches:
            child = branch.get(name)
            if isinstance(child, dict):
                output[name] = project(child, f"{path}/{name}")
        return output

    return project(result, "")


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
