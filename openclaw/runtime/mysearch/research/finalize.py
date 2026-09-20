"""搜索结果收尾：官方资源策略、状态页策略与证据汇总。

从 `mysearch/clients.py` 抽出的**结果收尾层**。输入是已经合并好的结果
载荷，输出是施加了策略、去噪并附上证据摘要的载荷——不做 provider 调用、
不读 config、不碰实例状态。

依赖方向单向：本模块依赖 `postprocess`、`query_routing`、
`provider_contract` 与同包的 `selection`；`clients` 在上层依赖本模块。

`MySearchClient` 保留同名方法作为一行委托，调用面不变。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from mysearch import postprocess
from mysearch import query_routing
from mysearch.provider_contract import ProviderResponse
from mysearch.research import sections
from mysearch.research import selection
def _trim_search_payload(
    result: dict[str, Any],
    *,
    max_results: int,
) -> dict[str, Any]:
        trimmed = dict(result)
        results = list(trimmed.get("results") or [])[:max_results]
        trimmed["results"] = results
        trimmed["citations"] = postprocess._align_citations_with_results(
            results=results,
            citations=list(trimmed.get("citations") or []),
        )
        return trimmed


def _augment_evidence_summary(
    result: dict[str, Any],
    *,
    query: str,
    mode: SearchMode,
    intent: ResolvedSearchIntent,
    include_domains: list[str] | None,
) -> dict[str, Any]:
        enriched = dict(result)
        evidence = dict(enriched.get("evidence") or {})
        results = list(enriched.get("results") or [])
        citations = list(enriched.get("citations") or [])
        official_mode = query_routing._resolve_official_result_mode(
            query=query,
            mode=mode,
            intent=intent,
            include_domains=include_domains,
        )
        providers_consulted = [
            item
            for item in (
                evidence.get("providers_consulted")
                or [enriched.get("provider", "")]
            )
            if item
        ]
        evidence.setdefault("providers_consulted", providers_consulted)
        evidence.setdefault(
            "verification",
            "cross-provider" if len(set(providers_consulted)) > 1 else "single-provider",
        )
        evidence.setdefault("citation_count", len(citations))
        evidence.setdefault("official_mode", official_mode)
        evidence.setdefault("official_filter_applied", False)
        evidence.setdefault("official_filter_reduced", False)

        source_domains = selection._collect_source_domains(results=results, citations=citations)
        social_identities = _collect_social_identities(results=results, citations=citations)
        social_identity_diversity = len(social_identities)
        social_identity_diversity_applies = query_routing._should_use_social_identity_diversity(
            mode=mode,
            intent=intent,
            source_domains=source_domains,
            social_identity_count=social_identity_diversity,
        )
        official_source_count = _count_official_resource_results(
            query=query,
            mode=mode,
            intent=intent,
            results=results,
            include_domains=include_domains,
        )
        conflicts = _detect_evidence_conflicts(
            mode=mode,
            intent=intent,
            results=results,
            include_domains=include_domains,
            source_domains=source_domains,
            official_source_count=official_source_count,
            providers_consulted=providers_consulted,
            official_mode=str(evidence.get("official_mode") or official_mode),
            social_identity_count=social_identity_diversity,
            social_identity_diversity_applies=social_identity_diversity_applies,
        )
        evidence["source_diversity"] = len(source_domains)
        evidence["source_domains"] = source_domains[:5]
        if social_identities or mode == "social" or intent == "social":
            evidence["social_identity_diversity"] = social_identity_diversity
            evidence["social_handles"] = social_identities[:5]
            evidence["diversity_basis"] = (
                "social_handles" if social_identity_diversity_applies else "domains"
            )
        evidence["official_source_count"] = official_source_count
        evidence["third_party_source_count"] = max(len(results) - official_source_count, 0)
        evidence["confidence"] = _estimate_search_confidence(
            mode=mode,
            intent=intent,
            result_count=len(results),
            source_domain_count=len(source_domains),
            official_source_count=official_source_count,
            verification=str(evidence.get("verification") or "single-provider"),
            conflicts=conflicts,
            official_mode=str(evidence.get("official_mode") or official_mode),
            social_identity_count=social_identity_diversity,
            social_identity_diversity_applies=social_identity_diversity_applies,
        )
        evidence["conflicts"] = conflicts
        enriched["evidence"] = evidence
        return enriched


def _finalize_search_result(
    result: dict[str, Any],
    *,
    query: str,
    mode: SearchMode,
    intent: ResolvedSearchIntent,
    include_domains: list[str] | None,
    result_profile: Literal['web', 'news', 'resource'],
    max_results: int,
) -> dict[str, Any]:
        finalized = dict(result)

        finalized = _apply_status_result_policy(
            query=query,
            mode=mode,
            intent=intent,
            result=finalized,
        )
        finalized = _apply_official_resource_policy(
            query=query,
            mode=mode,
            intent=intent,
            result=finalized,
            include_domains=include_domains,
        )

        final_official_mode = str(
            (
                (finalized.get("evidence") or {})
                if isinstance(finalized.get("evidence"), dict)
                else {}
            ).get("official_mode")
            or "off"
        )
        if final_official_mode != "off" or query_routing._should_rerank_resource_results(
            mode=mode,
            intent=intent,
        ):
            reranked_results = selection._rerank_resource_results(
                query=query,
                mode=mode,
                results=list(finalized.get("results") or []),
                include_domains=include_domains,
            )
            finalized["results"] = reranked_results
            finalized["citations"] = postprocess._align_citations_with_results(
                results=reranked_results,
                citations=list(finalized.get("citations") or []),
            )
        elif query_routing._should_rerank_general_results(result_profile=result_profile):
            reranked_results = selection._rerank_general_results(
                query=query,
                result_profile=result_profile,
                results=list(finalized.get("results") or []),
                include_domains=include_domains,
            )
            finalized["results"] = reranked_results
            finalized["citations"] = postprocess._align_citations_with_results(
                results=reranked_results,
                citations=list(finalized.get("citations") or []),
            )

        finalized = _trim_search_payload(finalized, max_results=max_results)
        finalized = _augment_evidence_summary(
            finalized,
            query=query,
            mode=mode,
            intent=intent,
            include_domains=include_domains,
        )
        return finalized


def _apply_status_result_policy(
    *,
    query: str,
    mode: SearchMode,
    intent: ResolvedSearchIntent,
    result: dict[str, Any],
) -> dict[str, Any]:
        query_lower = query.lower()
        if intent != "status" and not query_routing._looks_like_status_query(query_lower):
            return result

        enriched = dict(result)
        results = [dict(item) for item in (enriched.get("results") or [])]
        citations = list(enriched.get("citations") or [])
        evidence = dict(enriched.get("evidence") or {})
        status_results = [
            item
            for item in results
            if query_routing._looks_like_status_result(
                url=str(item.get("url") or ""),
                hostname=postprocess._result_hostname(item),
                title_text=str(item.get("title") or "").lower(),
            )
            and not query_routing._is_obvious_official_community_result(
                hostname=postprocess._result_hostname(item),
                path=urlparse(str(item.get("url") or "")).path.lower(),
            )
        ]
        if status_results:
            reordered = [
                *status_results,
                *[
                    item
                    for item in results
                    if str(item.get("url") or "") not in {str(status.get("url") or "") for status in status_results}
                ],
            ]
            if reordered != results:
                evidence["status_filter_applied"] = True
                enriched["results"] = reordered
                enriched["citations"] = postprocess._align_citations_with_results(
                    results=reordered,
                    citations=citations,
                )
            enriched["evidence"] = evidence
            return enriched

        rescue_candidate = selection._build_known_canonical_resource_rescue(
            query=query,
            mode=mode,
            intent=intent,
        )
        if rescue_candidate is None:
            enriched["evidence"] = evidence
            return enriched

        rescue_url = str(rescue_candidate.get("url") or "")
        deduped_results = [
            rescue_candidate,
            *[item for item in results if str(item.get("url") or "") != rescue_url],
        ]
        evidence["status_rescue_applied"] = True
        evidence["status_rescue_source"] = "canonical-map"
        enriched["results"] = deduped_results
        enriched["citations"] = postprocess._align_citations_with_results(
            results=deduped_results,
            citations=citations,
        )
        enriched["evidence"] = evidence
        return enriched


def _apply_official_resource_policy(
    *,
    query: str,
    mode: SearchMode,
    intent: ResolvedSearchIntent,
    result: dict[str, Any],
    include_domains: list[str] | None,
) -> dict[str, Any]:
        enriched = dict(result)
        results = list(enriched.get("results") or [])
        citations = list(enriched.get("citations") or [])
        official_mode = query_routing._resolve_official_result_mode(
            query=query,
            mode=mode,
            intent=intent,
            include_domains=include_domains,
        )
        evidence = dict(enriched.get("evidence") or {})
        evidence.setdefault("official_mode", official_mode)
        evidence.setdefault("official_filter_applied", False)
        evidence.setdefault("official_filter_reduced", False)
        evidence.setdefault("official_candidate_count", 0)
        if official_mode == "off":
            enriched["evidence"] = evidence
            return enriched

        official_candidates = _collect_official_result_candidates(
            query=query,
            mode=mode,
            intent=intent,
            results=results,
            include_domains=include_domains,
            strict_official=official_mode == "strict",
        )
        official_rescue_candidate: dict[str, Any] | None = None
        if official_mode in {"strict", "standard"}:
            official_rescue_candidate = selection._build_known_canonical_resource_rescue(
                query=query,
                mode=mode,
                intent=intent,
            )
            if official_rescue_candidate is not None and query_routing._should_apply_canonical_resource_rescue(
                query=query,
                mode=mode,
                intent=intent,
                official_candidates=official_candidates,
                rescue_candidate=official_rescue_candidate,
            ):
                official_candidates = [
                    official_rescue_candidate,
                    *[
                        dict(item)
                        for item in official_candidates
                        if postprocess._result_url_identity(str(item.get("url") or ""))
                        != postprocess._result_url_identity(
                            str(official_rescue_candidate.get("url") or "")
                        )
                    ],
                ]
                evidence["official_rescue_applied"] = True
                evidence["official_rescue_source"] = "canonical-map"
                if official_mode == "standard" and query_routing._looks_like_github_release_query(query.lower()):
                    promoted_results = [
                        official_rescue_candidate,
                        *[
                            dict(item)
                            for item in results
                            if postprocess._result_url_identity(str(item.get("url") or ""))
                            != postprocess._result_url_identity(
                                str(official_rescue_candidate.get("url") or "")
                            )
                        ],
                    ]
                    enriched["results"] = selection._rerank_resource_results(
                        query=query,
                        mode=mode,
                        results=promoted_results,
                        include_domains=include_domains,
                    )
                    enriched["citations"] = postprocess._align_citations_with_results(
                        results=enriched["results"],
                        citations=[*citations, official_rescue_candidate],
                    )
        evidence["official_candidate_count"] = len(official_candidates)
        if official_mode == "strict" and official_candidates:
            evidence["official_filter_applied"] = True
            evidence["official_filter_reduced"] = len(official_candidates) < len(results)
            enriched["results"] = official_candidates
            enriched["citations"] = postprocess._align_citations_with_results(
                results=official_candidates,
                citations=citations,
            )
        enriched["evidence"] = evidence
        return enriched


def _collect_official_result_candidates(
    *,
    query: str,
    mode: SearchMode,
    intent: ResolvedSearchIntent,
    results: list[dict[str, Any]],
    include_domains: list[str] | None,
    strict_official: bool,
) -> list[dict[str, Any]]:
        query_tokens = query_routing._query_brand_tokens(query)
        candidates: list[dict[str, Any]] = []
        for item in results:
            if sections._result_matches_official_policy(
                item=item,
                mode=mode,
                query_tokens=query_tokens,
                include_domains=include_domains,
                strict_official=strict_official,
            ):
                candidates.append(dict(item))
        if len(candidates) >= 2:
            use_general_official_rerank = (
                mode == "news"
                or intent in {"news", "status"}
                or query_routing._looks_like_status_query(query.lower())
            )
            if use_general_official_rerank:
                result_profile: Literal["web", "news"] = (
                    "news" if mode == "news" or intent in {"news", "status"} else "web"
                )
                candidates = selection._rerank_general_results(
                    query=query,
                    result_profile=result_profile,
                    results=candidates,
                    include_domains=include_domains,
                )
            else:
                candidates = selection._rerank_resource_results(
                    query=query,
                    mode=mode,
                    results=candidates,
                    include_domains=include_domains,
                )
        return candidates


def _augment_research_evidence(
    *,
    query: str,
    mode: SearchMode,
    intent: str,
    requested_page_count: int,
    pages: list[dict[str, Any]],
    citations: list[dict[str, Any]],
    web_search: dict[str, Any],
    social: dict[str, Any] | None,
    social_error: str,
    providers_consulted: list[str],
    research_plan: dict[str, Any],
    exa_discovery_count: int,
    exa_unique_url_count: int,
    exa_promoted_page_count: int,
    authoritative_source_count: int,
    supporting_source_count: int,
    community_source_count: int,
    selected_candidate_count: int,
    selected_candidate_domains: list[str],
    selected_candidate_cluster_counts: dict[str, int],
    docs_rescue_result_count: int,
    authoritative_research: bool,
    cross_provider_candidate_count: int,
    provider_match_depth: float,
) -> dict[str, Any]:
        successful_pages = [page for page in pages if not page.get("error")]
        page_error_count = max(len(pages) - len(successful_pages), 0)
        page_success_rate = (
            round(len(successful_pages) / requested_page_count, 2)
            if requested_page_count > 0
            else 0.0
        )
        web_evidence = dict(web_search.get("evidence") or {})
        source_domains = selection._collect_source_domains(
            results=successful_pages,
            citations=citations,
        )
        conflicts = list(web_evidence.get("conflicts") or [])
        selected_authoritative_source_count = max(authoritative_source_count, 0)
        selected_supporting_source_count = max(supporting_source_count, 0)
        selected_community_source_count = max(community_source_count, 0)
        search_authoritative_source_count = int(web_evidence.get("official_source_count") or 0)
        effective_authoritative_source_count = max(
            selected_authoritative_source_count,
            search_authoritative_source_count,
        )
        if requested_page_count and not successful_pages:
            conflicts.append("page-extraction-unavailable")
        elif requested_page_count and page_error_count > 0:
            conflicts.append("page-extraction-partial")
        if social_error:
            conflicts.append("social-search-unavailable")

        official_mode = str(
            web_evidence.get("official_mode")
            or query_routing._resolve_official_result_mode(
                query=query,
                mode=mode,
                intent=str(intent) if isinstance(intent, str) else "factual",
                include_domains=None,
            )
        )
        confidence = _estimate_research_confidence(
            search_confidence=str(web_evidence.get("confidence") or "low"),
            page_success_count=len(successful_pages),
            requested_page_count=requested_page_count,
            social_present=social is not None,
            social_error=bool(social_error),
            conflicts=conflicts,
            authoritative_source_count=effective_authoritative_source_count,
            cross_provider_candidate_count=cross_provider_candidate_count,
            source_cluster_count=len(selected_candidate_cluster_counts),
        )
        return {
            "providers_consulted": providers_consulted,
            "web_result_count": len(web_search.get("results") or []),
            "page_count": len(successful_pages),
            "page_error_count": page_error_count,
            "page_success_rate": page_success_rate,
            "citation_count": len(citations),
            "verification": "cross-provider"
            if ProviderResponse.is_hybrid(web_search) or len(providers_consulted) > 1
            else "single-provider",
            "source_diversity": len(source_domains),
            "source_domains": source_domains[:5],
            "official_source_count": search_authoritative_source_count,
            "official_mode": official_mode,
            "search_confidence": str(web_evidence.get("confidence") or "low"),
            "confidence": confidence,
            "conflicts": conflicts,
            "research_plan": research_plan,
            "exa_discovery_count": exa_discovery_count,
            "exa_unique_url_count": exa_unique_url_count,
            "exa_promoted_page_count": exa_promoted_page_count,
            "authoritative_source_count": effective_authoritative_source_count,
            "search_authoritative_source_count": search_authoritative_source_count,
            "selected_authoritative_source_count": selected_authoritative_source_count,
            "supporting_source_count": selected_supporting_source_count,
            "selected_supporting_source_count": selected_supporting_source_count,
            "community_source_count": selected_community_source_count,
            "selected_community_source_count": selected_community_source_count,
            "selected_candidate_count": selected_candidate_count,
            "selected_candidate_domains": selected_candidate_domains[:5],
            "selected_candidate_cluster_counts": dict(selected_candidate_cluster_counts),
            "source_cluster_count": len(selected_candidate_cluster_counts),
            "docs_rescue_result_count": docs_rescue_result_count,
            "authoritative_research": authoritative_research,
            "cross_provider_candidate_count": cross_provider_candidate_count,
            "provider_match_depth": provider_match_depth,
        }


def _estimate_research_confidence(
    *,
    search_confidence: str,
    page_success_count: int,
    requested_page_count: int,
    social_present: bool,
    social_error: bool,
    conflicts: list[str],
    authoritative_source_count: int,
    cross_provider_candidate_count: int,
    source_cluster_count: int,
) -> str:
        if "strict-official-unmet" in conflicts or "page-extraction-unavailable" in conflicts:
            return "low"
        if authoritative_source_count >= 2 and page_success_count > 0 and not social_error:
            return "high"
        if (
            search_confidence == "high"
            and page_success_count > 0
            and not social_error
            and (
                authoritative_source_count >= 1
                or cross_provider_candidate_count > 0
                or source_cluster_count >= 2
            )
        ):
            return "high"
        if authoritative_source_count >= 1 and page_success_count > 0:
            return "medium"
        if search_confidence in {"high", "medium"} and (
            page_success_count > 0 or requested_page_count <= 0 or not social_present
        ):
            return "medium"
        if search_confidence == "high":
            return "medium"
        return "low" if conflicts else "medium"


def _collect_social_identities(
    *,
    results: list[dict[str, Any]],
    citations: list[dict[str, Any]],
) -> list[str]:
        identities: list[str] = []
        seen: set[str] = set()
        for item in [*results, *citations]:
            if not isinstance(item, dict):
                continue
            hostname = postprocess._result_hostname(item)
            if hostname and not hostname.endswith(("x.com", "twitter.com")):
                continue
            identity = postprocess._social_result_identity(item)
            if not identity or identity in seen:
                continue
            seen.add(identity)
            identities.append(identity)
        return identities


def _count_official_resource_results(
    *,
    query: str,
    mode: SearchMode,
    intent: ResolvedSearchIntent,
    results: list[dict[str, Any]],
    include_domains: list[str] | None,
) -> int:
        official_mode = query_routing._resolve_official_result_mode(
            query=query,
            mode=mode,
            intent=intent,
            include_domains=include_domains,
        )
        if official_mode == "off" and not query_routing._should_rerank_resource_results(mode=mode, intent=intent):
            return 0
        query_tokens = query_routing._query_brand_tokens(query)
        strict_official = official_mode == "strict"
        official_count = 0
        for item in results:
            if sections._result_matches_official_policy(
                item=item,
                mode=mode,
                query_tokens=query_tokens,
                include_domains=include_domains,
                strict_official=strict_official,
            ):
                official_count += 1
        return official_count


def _detect_evidence_conflicts(
    *,
    mode: SearchMode,
    intent: ResolvedSearchIntent,
    results: list[dict[str, Any]],
    include_domains: list[str] | None,
    source_domains: list[str],
    official_source_count: int,
    providers_consulted: list[str],
    official_mode: str,
    social_identity_count: int,
    social_identity_diversity_applies: bool,
) -> list[str]:
        conflicts: list[str] = []
        effective_diversity = (
            social_identity_count if social_identity_diversity_applies else len(source_domains)
        )
        if effective_diversity <= 1 and len(results) > 1:
            conflicts.append("low-source-diversity")
        if len(set(providers_consulted)) <= 1 and effective_diversity <= 1 and results:
            conflicts.append("single-provider-single-domain")
        if query_routing._should_rerank_resource_results(mode=mode, intent=intent):
            if results and official_source_count <= 0:
                conflicts.append("official-source-not-confirmed")
            elif results and official_source_count < len(results):
                conflicts.append("mixed-official-and-third-party")
            if include_domains and not results:
                conflicts.append("domain-filter-returned-empty")
        if official_mode == "strict" and results and official_source_count <= 0:
            conflicts.append("strict-official-unmet")
        return conflicts


def _estimate_search_confidence(
    *,
    mode: SearchMode,
    intent: ResolvedSearchIntent,
    result_count: int,
    source_domain_count: int,
    official_source_count: int,
    verification: str,
    conflicts: list[str],
    official_mode: str,
    social_identity_count: int,
    social_identity_diversity_applies: bool,
) -> str:
        effective_diversity = (
            social_identity_count if social_identity_diversity_applies else source_domain_count
        )
        if result_count <= 0:
            return "low"
        if official_mode == "strict" and official_source_count <= 0:
            return "low"
        if query_routing._should_rerank_resource_results(mode=mode, intent=intent):
            if official_source_count > 0 and "official-source-not-confirmed" not in conflicts:
                if (
                    verification == "cross-provider"
                    or (effective_diversity >= 2 and "mixed-official-and-third-party" not in conflicts)
                ):
                    return "high"
                return "medium"
            return "medium" if effective_diversity >= 2 else "low"
        if verification == "cross-provider" and effective_diversity >= 2:
            return "high"
        if effective_diversity >= 2:
            return "medium"
        return "low" if conflicts else "medium"


def _fallback_quality_issue(
    *,
    result: dict[str, Any],
    mode: SearchMode,
    intent: ResolvedSearchIntent,
    include_domains: list[str] | None,
    include_content: bool = False,
) -> str | None:
        results = list(result.get("results") or [])
        if include_content and results and not any(
            isinstance(item, dict) and str(item.get("content") or "").strip()
            for item in results
        ):
            return "provider returned results without requested content"
        if results:
            return None
        if include_domains:
            return "provider returned no results for domain-filtered query"
        if mode in {"docs", "github", "pdf", "news"} or intent in {
            "comparison",
            "exploratory",
            "resource",
            "tutorial",
            "news",
            "status",
        }:
            return "provider returned no results"
        return None


def _can_attempt_award_page_extraction(
    *,
    query: str,
    results: list[dict[str, Any]],
) -> bool:
        for item in results[:5]:
            title_text = (item.get("title") or "").lower()
            snippet_text = (item.get("snippet") or "").lower()
            path = urlparse(item.get("url", "")).path.lower()
            if query_routing._looks_like_award_winner_result(
                title_text=title_text,
                snippet_text=snippet_text,
                path=path,
            ):
                return True
            if query_routing._result_event_page_priority(query=query, item=item) >= 8:
                return True
        return False


def _answer_looks_uncertain(
    answer: str,
) -> bool:
        answer_lower = answer.lower()
        markers = [
            "not yet determined",
            "not yet known",
            "cannot be determined",
            "cannot determine",
            "not specified",
            "not provided",
            "insufficient data",
            "no winner was specified",
            "cannot be concluded",
            "could not be determined",
            "still unknown",
            "to be announced",
            "tbd",
            "unclear",
            "unknown",
            "尚未确定",
            "尚未公布",
            "待公布",
            "未知",
        ]
        return any(marker in answer_lower for marker in markers)

