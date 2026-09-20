"""provider 响应解析与结果标注：xAI 回包提取、结果 debug 标注。

从 `mysearch/clients.py` 抽出的**响应解析层**。输入是 provider 的原始响应
dict 与路由决策，输出是规范化引用、输出文本，或附加了 debug 字段的结果——
不做 provider 调用、不读 config、不碰实例状态。

依赖方向单向：本模块依赖 `postprocess`（结果规范化与域名归一）与
`query_routing`；`clients` 在上层依赖本模块。

`MySearchClient` 保留同名方法作为一行委托，调用面不变。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from mysearch import postprocess
from mysearch import query_routing
from mysearch.provider_contract import ProviderResponse
from mysearch.types import (
    ProviderName,
    ResolvedSearchIntent,
    RouteDecision,
    SearchStrategy,
)


def _annotate_search_debug(
    result: dict[str, Any],
    *,
    provider: ProviderName,
    normalized_sources: list[str],
    resolved_intent: ResolvedSearchIntent,
    resolved_strategy: SearchStrategy,
    decision: RouteDecision,
    include_content: bool,
    include_answer: bool,
    cache_hit: bool,
    requested_max_results: int | None = None,
    candidate_max_results: int | None = None,
) -> dict[str, Any]:
        result["route_debug"] = {
            "requested_provider": provider,
            "route_provider": decision.provider,
            "normalized_sources": normalized_sources,
            "resolved_intent": resolved_intent,
            "resolved_strategy": resolved_strategy,
            "include_content": include_content,
            "include_answer": include_answer,
            "cache_hit": cache_hit,
        }
        if requested_max_results is not None:
            result["route_debug"]["requested_max_results"] = requested_max_results
        if candidate_max_results is not None:
            result["route_debug"]["candidate_max_results"] = candidate_max_results
        evidence = result.get("evidence") or {}
        if evidence.get("official_mode"):
            result["route_debug"]["official_mode"] = evidence.get("official_mode")
        if "official_filter_applied" in evidence:
            result["route_debug"]["official_filter_applied"] = bool(
                evidence.get("official_filter_applied")
            )
        return result


def _annotate_extract_warning(
    result: dict[str, Any],
    *,
    warning: str,
) -> dict[str, Any]:
        annotated = dict(result)
        metadata = dict(annotated.get("metadata") or {})
        metadata["warning"] = warning
        annotated["metadata"] = metadata
        annotated["warning"] = warning
        return annotated


def _annotate_extract_fallback(
    result: dict[str, Any],
    *,
    fallback_from: str,
    fallback_reason: str,
) -> dict[str, Any]:
        annotated = dict(result)
        metadata = dict(annotated.get("metadata") or {})
        metadata["fallback_from"] = fallback_from
        metadata["fallback_reason"] = fallback_reason
        annotated["metadata"] = metadata
        annotated["fallback"] = {
            "from": fallback_from,
            "reason": fallback_reason,
        }
        return annotated


def _extract_candidate_matches_requested_url(
    *,
    requested_url: str,
    candidate_url: str,
) -> bool:
        requested = postprocess._canonical_result_url(requested_url)
        candidate = postprocess._canonical_result_url(candidate_url)
        if not requested or not candidate:
            return False
        if requested.rstrip("/") == candidate.rstrip("/"):
            return True
        requested_host = postprocess._clean_hostname(urlparse(requested).netloc)
        candidate_host = postprocess._clean_hostname(urlparse(candidate).netloc)
        if not requested_host or not candidate_host:
            return False
        return postprocess._registered_domain(requested_host) == postprocess._registered_domain(candidate_host)


def _extract_xai_output_text(
    payload: dict[str, Any],
) -> str:
        if isinstance(payload.get("output_text"), str):
            return payload["output_text"]

        parts: list[str] = []
        for item in payload.get("output", []) or []:
            content = item.get("content")
            if isinstance(content, str):
                parts.append(content)
                continue

            if not isinstance(content, list):
                continue

            for part in content:
                if not isinstance(part, dict):
                    continue

                if isinstance(part.get("text"), str):
                    parts.append(part["text"])
                    continue

                text_obj = part.get("text")
                if isinstance(text_obj, dict) and isinstance(text_obj.get("value"), str):
                    parts.append(text_obj["value"])

        return "\n".join([item for item in parts if item]).strip()


def _extract_xai_citations(
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
        raw_citations = payload.get("citations") or []
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()

        if isinstance(raw_citations, list):
            for item in raw_citations:
                citation = postprocess._normalize_citation(item)
                if citation is None:
                    continue
                url = citation.get("url", "")
                if url and url in seen:
                    continue
                if url:
                    seen.add(url)
                normalized.append(citation)

        if normalized:
            return normalized

        for output_item in payload.get("output", []) or []:
            if not isinstance(output_item, dict):
                continue

            content_items = output_item.get("content") or []
            if not isinstance(content_items, list):
                continue

            for content_item in content_items:
                if not isinstance(content_item, dict):
                    continue

                annotations = content_item.get("annotations") or []
                if not isinstance(annotations, list):
                    continue

                for annotation in annotations:
                    citation = postprocess._normalize_citation(annotation)
                    if citation is None:
                        continue
                    url = citation.get("url", "")
                    if url and url in seen:
                        continue
                    if url:
                        seen.add(url)
                    normalized.append(citation)

        return normalized

