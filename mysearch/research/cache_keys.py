"""缓存键与请求参数构造：纯字符串/字典拼装。

从 `mysearch/clients.py` 抽出的**键构造层**。四个缓存键函数把请求参数折叠成
稳定的字符串键，另有 Firecrawl 请求参数与搜索类别归一化。全部是纯拼装，
不做 provider 调用、不读 config、不碰实例状态。

依赖方向单向：本模块依赖 `postprocess` 与 `query_routing`；`clients` 在上层
依赖本模块。

`MySearchClient` 保留同名方法作为一行委托，调用面不变。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from mysearch import postprocess
from mysearch import query_routing
from mysearch.research import render
from mysearch.research import sections
def _build_cache_key(
    namespace: str,
    payload: dict[str, Any],
) -> str:
        serialized = json.dumps(
            {
                "namespace": namespace,
                "payload": payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _build_search_cache_key(
    *,
    query: str,
    mode: SearchMode,
    resolved_intent: ResolvedSearchIntent,
    resolved_strategy: SearchStrategy,
    provider: ProviderName,
    normalized_sources: list[str],
    include_content: bool,
    include_answer: bool,
    include_domains: list[str] | None,
    exclude_domains: list[str] | None,
    decision: RouteDecision,
    allowed_x_handles: list[str] | None = None,
    excluded_x_handles: list[str] | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    include_x_images: bool = False,
    include_x_videos: bool = False,
    max_results: int = 5,
) -> str:
        # `search()` 接受日期窗口和 X handle 过滤参数，且这些会改变上游请求的实际
        # query / 结果集；不把它们放进 cache key 会导致两次"同 query 但不同日期范围"
        # 的请求误命中同一条缓存（perf-r4 P0 正确性 bug）。
        return _build_cache_key(
            "search",
            {
                "query": query,
                "mode": mode,
                "intent": resolved_intent,
                "strategy": resolved_strategy,
                "provider": provider,
                "normalized_sources": normalized_sources,
                "include_content": include_content,
                "include_answer": include_answer,
                "include_domains": sorted(set(include_domains or [])),
                "exclude_domains": sorted(set(exclude_domains or [])),
                "route_provider": decision.provider,
                "tavily_topic": decision.tavily_topic,
                "firecrawl_categories": decision.firecrawl_categories or [],
                "allowed_x_handles": sorted(set(allowed_x_handles or [])),
                "excluded_x_handles": sorted(set(excluded_x_handles or [])),
                "from_date": from_date or "",
                "to_date": to_date or "",
                "include_x_images": include_x_images,
                "include_x_videos": include_x_videos,
                "requested_max_results": max_results,
            },
        )


def _build_extract_cache_key(
    *,
    url: str,
    formats: list[str],
    only_main_content: bool,
    provider: Literal['auto', 'firecrawl', 'tavily'],
) -> str:
        return _build_cache_key(
            "extract",
            {
                "url": url,
                "formats": formats,
                "only_main_content": only_main_content,
                "provider": provider,
            },
        )


def _build_social_cache_key(
    *,
    query: str,
    max_results: int,
    allowed_x_handles: list[str] | None,
    excluded_x_handles: list[str] | None,
    from_date: str | None,
    to_date: str | None,
    include_x_images: bool,
    include_x_videos: bool,
) -> str:
        return _build_cache_key(
            "social",
            {
                "query": query,
                "max_results": max_results,
                "allowed_x_handles": sorted(set(allowed_x_handles or [])),
                "excluded_x_handles": sorted(set(excluded_x_handles or [])),
                "from_date": from_date or "",
                "to_date": to_date or "",
                "include_x_images": include_x_images,
                "include_x_videos": include_x_videos,
            },
        )


def _build_social_gateway_cache_key(
    *,
    base_url: str,
    path: str,
) -> str:
        return _build_cache_key(
            "social_gateway",
            {
                "base_url": (base_url or "").rstrip("/"),
                "path": path,
            },
        )


def _build_firecrawl_tbs(
    from_date: str | None,
    to_date: str | None,
) -> str:
        if not from_date and not to_date:
            return ""
        if from_date and to_date:
            return f"cdr:1,cd_min:{from_date},cd_max:{to_date}"
        if from_date:
            return f"cdr:1,cd_min:{from_date}"
        return f"cdr:1,cd_max:{to_date}"


def _build_firecrawl_domain_query(
    *,
    query: str,
    include_domain: str | None,
    exclude_domains: list[str] | None,
) -> str:
        parts: list[str] = []
        if include_domain:
            parts.append(f"site:{include_domain}")
        for domain in exclude_domains or []:
            parts.append(f"-site:{domain}")
        parts.append(query)
        return " ".join(parts).strip()


def _normalize_firecrawl_search_categories(
    categories: list[str],
) -> list[str]:
        supported = {"github", "research", "pdf"}
        normalized: list[str] = []
        for item in categories:
            value = str(item or "").strip().lower()
            if value in supported and value not in normalized:
                normalized.append(value)
        return normalized


def _build_research_summary_fallback(
    *,
    query: str,
    web_search: dict[str, Any],
    pages: list[dict[str, Any]],
    citations: list[dict[str, Any]],
    social: dict[str, Any] | None,
    evidence: dict[str, Any],
) -> str:
        report_sections = sections._build_research_report_sections(
            query=query,
            web_search=web_search,
            ordered_results=[],
            pages=pages,
            citations=citations,
            social=social,
            evidence=evidence,
        )
        return render.render_research_report(report_sections)

