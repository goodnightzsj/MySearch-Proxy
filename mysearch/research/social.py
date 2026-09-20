"""社交搜索响应归一化：把 xAI / Exa 的社交回包整理成统一结构。

从 `mysearch/clients.py` 抽出的**社交结果归一化层**。输入是 provider 的原始
响应 dict 与查询参数，输出是统一的结果载荷与"不可用"占位结果——不做
provider 调用、不读 config、不碰实例状态。

依赖方向单向：本模块依赖 `postprocess`（结果清洗与去重）；`clients` 在上层
依赖本模块。

`MySearchClient` 保留同名方法作为一行委托，调用面不变。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from mysearch import postprocess
from mysearch import query_routing
def _normalize_exa_social_fallback_results(
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in results:
            if not query_routing._is_exa_social_candidate(item):
                continue
            url = str(item.get("url") or "")
            normalized.append(
                {
                    "provider": "exa_social_fallback",
                    "source": "x",
                    "title": item.get("title", ""),
                    "url": url,
                    "snippet": item.get("snippet", ""),
                    "content": item.get("content", ""),
                    "author": postprocess._social_result_identity(item),
                }
            )
        return normalized


def _build_social_unavailable_result(
    *,
    query: str,
    fallback_reason: str,
) -> dict[str, Any]:
        reason = fallback_reason[:200]
        return {
            "provider": "social_unavailable",
            "transport": "",
            "query": query,
            "answer": "",
            "results": [],
            "citations": [],
            "fallback": {
                "from": "xai_compatible",
                "to": "social_unavailable",
                "reason": reason,
            },
            "summary": f"Social/X search unavailable: {reason}",
        }


def _build_social_gateway_unavailable_result(
    *,
    base_url: str,
    fallback_reason: str,
) -> dict[str, Any]:
        reason = fallback_reason[:200]
        return {
            "provider": "social_gateway_unavailable",
            "transport": "",
            "base_url": base_url,
            "results": [],
            "citations": [],
            "fallback": {
                "from": "xai_compatible",
                "to": "social_gateway_unavailable",
                "reason": reason,
            },
            "summary": f"Social/X gateway unavailable: {reason}",
        }


def _normalize_social_gateway_response(
    *,
    response: dict[str, Any],
    query: str,
    transport: str,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
        raw_results = postprocess._extract_social_gateway_results(response)
        results = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or item.get("link") or ""
            hostname = postprocess._clean_hostname(urlparse(url).netloc)
            if hostname and not hostname.endswith(("x.com", "twitter.com")):
                continue
            content = (
                item.get("content")
                or item.get("full_text")
                or item.get("text")
                or item.get("body")
                or ""
            )
            title = (
                item.get("title")
                or item.get("author")
                or item.get("handle")
                or item.get("username")
                or url
            )
            snippet = item.get("snippet") or item.get("summary") or content
            results.append(
                {
                    "provider": "custom_social",
                    "source": "x",
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                    "content": content,
                    "author": item.get("author") or item.get("username") or item.get("handle") or "",
                    "created_at": item.get("created_at") or item.get("published_at") or "",
                }
            )

        results = postprocess._filter_social_results_by_date(
            results,
            from_date=from_date,
            to_date=to_date,
        )
        results = postprocess._diversify_social_results(
            results,
            max_results=10,
            max_per_identity=1,
        )
        citations = postprocess._extract_social_gateway_citations(response, results)
        answer = (
            response.get("answer")
            or response.get("summary")
            or response.get("content")
            or response.get("text")
            or ""
        )
        warning = None
        if (from_date or to_date) and not results:
            answer = ""
            warning = "no social results matched the requested date window"

        normalized = {
            "provider": "custom_social",
            "transport": transport,
            "query": response.get("query", query),
            "answer": answer,
            "results": results,
            "citations": citations,
            "tool_usage": response.get("tool_usage") or {"social_search_calls": 1},
        }
        if warning:
            normalized["warning"] = warning
        return normalized

