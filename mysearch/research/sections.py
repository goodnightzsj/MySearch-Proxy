"""Research 报告章节组装：从候选池到 `sections` dict 的纯转换。

从 `mysearch/clients.py` 抽出的**章节装配层**。输入是已经取回的
`results` / `claims` / `clusters` 等中间结构，输出是 `render_research_report`
消费的 `sections` dict —— 不做任何 provider 调用、不读 config、不碰实例状态。

依赖方向单向：本模块依赖 `query_routing`（谓词）、`postprocess`（域名归一）、
`ranking`（重排键）以及同包的 `claims` / `comparison`；`clients` 在上层依赖本模块。

`MySearchClient` 保留同名方法作为一行委托，调用面不变。
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence, cast
from urllib.parse import urlparse

from mysearch import postprocess
from mysearch import query_routing
from mysearch import ranking
from mysearch.research import claims
from mysearch.research import comparison
from mysearch.types import SEARCH_MODES, SearchMode


def _align_research_claims_with_comparison_rows(
    *,
    claim_evidence: list[dict[str, Any]],
    comparison_rows: Sequence[Mapping[str, Any]],
    comparison_entities: Sequence[Sequence[str]],
) -> list[dict[str, Any]]:
    if not claim_evidence or not comparison_rows or not comparison_entities:
        return claim_evidence[:4]

    aligned: list[dict[str, Any]] = []
    seen_signatures: set[str] = set()

    def append_entry(entry: Mapping[str, Any]) -> None:
        if len(aligned) >= 4:
            return
        claim_text = str(entry.get("claim") or "").strip()
        signature = claims.research_claim_signature(claim_text)
        if not claim_text or not signature or signature in seen_signatures:
            return
        aligned.append(dict(entry))
        seen_signatures.add(signature)

    for entity_tokens in comparison_entities[:4]:
        synthetic_claim = _research_comparison_claim_from_row(
            comparison_rows=comparison_rows,
            entity_tokens=entity_tokens,
        )
        matching_claims = [
            item
            for item in claim_evidence
            if claims.research_claim_comparison_subject_match_count(
                claim=str(item.get("claim") or "").strip(),
                sources=[str(source) for source in (item.get("sources") or []) if source],
                entities=[entity_tokens],
            )
            > 0
        ]
        if synthetic_claim:
            matching_claims.append(synthetic_claim)
        if matching_claims:
            def claim_rank(entry: Mapping[str, Any]) -> tuple[int, int, int, int]:
                claim = str(entry.get("claim") or "").strip()
                clusters = [
                    str(cluster).strip()
                    for cluster in (entry.get("clusters") or [])
                    if str(cluster).strip()
                ]
                return (
                    int(entry.get("comparison_subject_match_count") or 0),
                    claims.research_claim_best_cluster_rank(
                        clusters=clusters,
                        authoritative_preferred=True,
                    ),
                    claims.research_claim_support_rank(str(entry.get("support_level") or "").strip()),
                    len(claim),
                )

            append_entry(max(matching_claims, key=claim_rank))

    for entry in claim_evidence:
        claim_text = str(entry.get("claim") or "").strip()
        match_count = claims.research_claim_comparison_subject_match_count(
            claim=claim_text,
            sources=[str(source) for source in (entry.get("sources") or []) if source],
            entities=comparison_entities,
        )
        if match_count <= 0 and not claims.research_claim_is_comparison_tail_relevant(claim_text):
            continue
        append_entry(entry)
        if len(aligned) >= 4:
            break
    return aligned[:4]


def _build_excerpt(
    content: str,
    limit: int = 600,
) -> str:
    compact = re.sub(r"\s+", " ", content).strip()
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "..."


def _build_research_claim_evidence(
    *,
    query: str,
    mode: str,
    ordered_results: list[dict[str, Any]],
    pages: list[dict[str, Any]],
    citations: list[dict[str, Any]],
    comparison_like: bool,
    include_domains: list[str] | None,
    authoritative_preferred: bool,
) -> list[dict[str, Any]]:
    if not ordered_results and citations:
        ordered_results = [
            {
                "provider": (
                    "canonical_research_docs"
                    if _research_is_canonical_vendor_doc(str(citation.get("url") or ""))
                    else "citation"
                ),
                "matched_providers": [
                    "canonical_research_docs"
                    if _research_is_canonical_vendor_doc(str(citation.get("url") or ""))
                    else "citation"
                ],
                "title": (citation.get("title") or "").strip(),
                "url": (citation.get("url") or "").strip(),
                "snippet": _research_canonical_doc_snippet_for_url(
                    str(citation.get("url") or "")
                ),
                "content": "",
            }
            for citation in citations
            if (citation.get("url") or "").strip()
        ]
    if not ordered_results:
        return []

    url_to_excerpt = {
        (page.get("url") or "").strip(): (
            page.get("excerpt")
            or _build_excerpt((page.get("content") or "").strip(), limit=180)
        )
        for page in pages
        if (page.get("url") or "").strip() and not page.get("error")
    }
    url_to_title = {
        (citation.get("url") or "").strip(): (citation.get("title") or "").strip()
        for citation in citations
        if (citation.get("url") or "").strip()
    }
    claims_by_key: dict[str, dict[str, Any]] = {}
    fallback_claims_by_key: dict[str, dict[str, Any]] = {}
    claim_order: list[str] = []
    fallback_claim_order: list[str] = []
    for item in ordered_results:
        url = (item.get("url") or "").strip()
        title = (item.get("title") or url_to_title.get(url) or "").strip()
        excerpt = _select_research_claim_excerpt(
            page_excerpt=(url_to_excerpt.get(url) or "").strip(),
            snippet=(item.get("snippet") or "").strip(),
            content=(item.get("content") or "").strip(),
        )
        canonical_doc_snippet = _research_canonical_doc_snippet_for_url(url)
        if canonical_doc_snippet and (
            not excerpt
            or claims.research_excerpt_looks_like_navigation_noise(excerpt)
            or (
                comparison_like
                and not claims.research_excerpt_has_substantive_claim(excerpt)
            )
        ):
            excerpt = canonical_doc_snippet
        claim = _research_claim_text(
            title=title,
            excerpt=excerpt,
            comparison_like=comparison_like,
        )
        if not claim:
            continue
        claim_key = claims.research_claim_signature(claim)
        if not claim_key:
            continue
        is_generic_claim = claims.research_claim_is_generic(claim)
        target_claims = fallback_claims_by_key if is_generic_claim else claims_by_key
        target_order = fallback_claim_order if is_generic_claim else claim_order
        if claim_key not in target_claims:
            target_claims[claim_key] = {
                "claim": claim,
                "sources": [],
                "urls": [],
                "providers": [],
                "clusters": [],
                "domains": [],
            }
            target_order.append(claim_key)
        entry = target_claims[claim_key]
        source_label = title or (query_routing._registered_domain(query_routing._result_hostname(item)) or url)
        if source_label and source_label not in entry["sources"]:
            entry["sources"].append(source_label)
        if url and url not in entry["urls"]:
            entry["urls"].append(url)
        domain = query_routing._registered_domain(query_routing._result_hostname(item))
        if domain and domain not in entry["domains"]:
            entry["domains"].append(domain)
        for provider in (
            item.get("matched_providers")
            or [item.get("provider", "")]
        ):
            if provider and provider not in entry["providers"]:
                entry["providers"].append(provider)
        cluster_label = _research_result_cluster_label(
            query=query,
            mode=cast(SearchMode, mode if mode in SEARCH_MODES else "web"),
            item=item,
            include_domains=include_domains,
            authoritative_preferred=authoritative_preferred,
        )
        if cluster_label and cluster_label not in entry["clusters"]:
            entry["clusters"].append(cluster_label)
        if len(claim_order) >= 8 and all(
            len(claims_by_key[key]["sources"]) >= 1 for key in claim_order[:8]
        ):
            continue
    if not claim_order:
        claims_by_key = fallback_claims_by_key
        claim_order = fallback_claim_order
    claim_list: list[dict[str, Any]] = []
    comparison_entities = (
        _research_comparison_entities(query)
        if comparison_like
        else []
    )
    for order_index, key in enumerate(claim_order):
        entry = claims_by_key[key]
        entry["source_count"] = len(entry["sources"])
        entry["provider_count"] = len(entry["providers"])
        entry["cluster_count"] = len(entry["clusters"])
        entry["comparison_subject_match_count"] = (
            claims.research_claim_comparison_subject_match_count(
                claim=str(entry.get("claim") or "").strip(),
                sources=[str(item) for item in (entry.get("sources") or []) if item],
                entities=comparison_entities,
            )
            if comparison_entities
            else 0
        )
        entry["support_level"] = claims.research_claim_support_level(
            source_count=int(entry["source_count"] or 0),
            provider_count=int(entry["provider_count"] or 0),
            cluster_count=int(entry["cluster_count"] or 0),
        )
        entry["support_basis"] = claims.research_claim_support_basis(entry)
        entry["_order_index"] = order_index
        claim_list.append(entry)
    if authoritative_preferred and not any(
        any(cluster in {"official", "supporting"} for cluster in (entry.get("clusters") or []))
        for entry in claim_list
    ):
        fallback_entry = _research_authoritative_claim_fallback(
            query=query,
            mode=mode,
            ordered_results=ordered_results,
            include_domains=include_domains,
            authoritative_preferred=authoritative_preferred,
        )
        if fallback_entry:
            claim_list.append(fallback_entry)
    claim_list.sort(
        key=lambda entry: (
            -claims.research_claim_best_cluster_rank(
                clusters=[str(item) for item in (entry.get("clusters") or []) if item],
                authoritative_preferred=authoritative_preferred,
            ),
            -claims.research_claim_support_rank(str(entry.get("support_level") or "")),
            -int(entry.get("comparison_subject_match_count") or 0),
            -int(entry.get("source_count") or 0),
            -int(entry.get("provider_count") or 0),
            int(entry.get("_order_index") or 0),
        )
    )
    for entry in claim_list:
        entry.pop("_order_index", None)
    claim_list = _trim_research_claims_for_visibility(
        claims=claim_list,
        authoritative_preferred=authoritative_preferred,
        comparison_like=comparison_like,
        limit=4,
    )
    claim_list = _dedupe_research_claims_by_source_topic(claim_list, limit=4)
    claim_list = _diversify_research_claims_by_domain(claim_list, limit=4)
    return claim_list


def _build_research_report_sections(
    *,
    query: str,
    web_search: dict[str, Any],
    ordered_results: list[dict[str, Any]],
    pages: list[dict[str, Any]],
    citations: list[dict[str, Any]],
    social: dict[str, Any] | None,
    evidence: dict[str, Any],
    executive_summary_override: str = '',
) -> dict[str, Any]:
    web_answer = (web_search.get("answer") or "").strip()
    social_answer = ""
    if social:
        social_answer = (social.get("answer") or "").strip()

    url_to_title: dict[str, str] = {}
    for citation in citations:
        url = (citation.get("url") or "").strip()
        title = (citation.get("title") or "").strip()
        if url and title and url not in url_to_title:
            url_to_title[url] = title

    highlights: list[str] = []
    for page in pages:
        if page.get("error"):
            continue
        excerpt = (page.get("excerpt") or page.get("content") or "").strip()
        if not excerpt:
            continue
        excerpt = _build_excerpt(excerpt, limit=180)
        title = url_to_title.get((page.get("url") or "").strip(), "").strip()
        if title:
            highlights.append(f"{title}: {excerpt}")
        else:
            highlights.append(excerpt)
        if len(highlights) >= 2:
            break

    citation_titles = [
        (citation.get("title") or "").strip()
        for citation in citations
        if (citation.get("title") or "").strip()
    ]
    citation_title_lines: list[str] = []
    for citation in citations:
        title = (citation.get("title") or "").strip()
        url = (citation.get("url") or "").strip()
        if not title:
            continue
        domain = query_routing._registered_domain(query_routing._result_hostname({"url": url}))
        line = f"{title} ({domain})" if domain and domain not in title.lower() else title
        if line not in citation_title_lines:
            citation_title_lines.append(line)
        if len(citation_title_lines) >= 4:
            break
    ordered_title_lines: list[str] = []
    for item in ordered_results:
        url = (item.get("url") or "").strip()
        title = (item.get("title") or url_to_title.get(url) or "").strip()
        if not title:
            continue
        domain = query_routing._registered_domain(query_routing._result_hostname(item))
        line = f"{title} ({domain})" if domain and domain not in title.lower() else title
        if line not in ordered_title_lines:
            ordered_title_lines.append(line)
        if len(ordered_title_lines) >= 4:
            break
    query_lower = query.lower()
    comparison_like = (
        web_search.get("intent") in {"comparison", "exploratory"}
        or query_routing._looks_like_comparison_query(query_lower)
        or query_routing._looks_like_exploratory_query(query_lower)
        or any(token in query_lower for token in ("best ", "top ", "compare ", "comparison "))
    )
    selected_cluster_counts = {
        str(key): int(value or 0)
        for key, value in dict(evidence.get("selected_candidate_cluster_counts") or {}).items()
    }
    explicit_selected_authoritative = int(evidence.get("selected_authoritative_source_count") or 0)
    explicit_selected_supporting = int(evidence.get("selected_supporting_source_count") or 0)
    explicit_selected_community = int(evidence.get("selected_community_source_count") or 0)
    if (
        explicit_selected_authoritative > 0
        or explicit_selected_supporting > 0
        or explicit_selected_community > 0
    ):
        authoritative_source_count = explicit_selected_authoritative
        supporting_source_count = explicit_selected_supporting
        community_source_count = explicit_selected_community
    elif selected_cluster_counts:
        authoritative_source_count = int(selected_cluster_counts.get("official") or 0)
        supporting_source_count = int(selected_cluster_counts.get("supporting") or 0)
        community_source_count = int(selected_cluster_counts.get("community") or 0)
    else:
        authoritative_source_count = int(evidence.get("authoritative_source_count") or 0)
        supporting_source_count = int(evidence.get("supporting_source_count") or 0)
        community_source_count = int(evidence.get("community_source_count") or 0)
    report_mode = str(
        (evidence.get("research_plan") or {}).get("web_mode")
        or web_search.get("intent")
        or "web"
    )
    authoritative_research = bool(evidence.get("authoritative_research"))
    preferred_title_lines = _build_research_report_source_lines(
        query=query,
        mode=report_mode,
        ordered_results=ordered_results,
        citations=citations,
        include_domains=None,
        authoritative_preferred=authoritative_research,
        comparison_like=comparison_like,
        max_items=4,
    ) or (ordered_title_lines or citation_title_lines)
    anchor_tokens = _research_report_anchor_tokens(
        query=query,
        mode=report_mode,
        ordered_results=ordered_results,
        authoritative_preferred=authoritative_research,
    )
    if not anchor_tokens:
        for domain in [str(item).strip() for item in (evidence.get("selected_candidate_domains") or [])]:
            if not domain:
                continue
            registered_domain = query_routing._registered_domain(domain)
            for token in re.split(r"[^a-z0-9]+", registered_domain.lower()):
                if token in {
                    "",
                    "ai",
                    "api",
                    "com",
                    "dev",
                    "developers",
                    "docs",
                    "guide",
                    "guides",
                    "io",
                    "net",
                    "org",
                    "platform",
                    "reference",
                    "www",
                }:
                    continue
                if token not in anchor_tokens:
                    anchor_tokens.append(token)
                if len(anchor_tokens) >= 6:
                    break
            if len(anchor_tokens) >= 6:
                break
    if (
        web_answer
        and comparison_like
        and (authoritative_source_count > 0 or supporting_source_count > 0)
    ):
        web_answer = ""
    elif (
        web_answer
        and authoritative_research
        and (authoritative_source_count > 0 or supporting_source_count > 0)
        and anchor_tokens
        and not _research_summary_mentions_anchor_tokens(web_answer, anchor_tokens)
    ):
        web_answer = ""
    primary_finding = executive_summary_override or web_answer or social_answer
    if not primary_finding and comparison_like:
        if authoritative_source_count > 0 and preferred_title_lines:
            primary_finding = (
                "Authoritative sources and corroborating analysis were found; "
                f"the strongest anchors include {', '.join(preferred_title_lines[:3])}."
            )
        elif supporting_source_count > 0 and preferred_title_lines:
            primary_finding = (
                "Supporting sources and corroborating analysis were found; "
                f"the strongest anchors include {', '.join(preferred_title_lines[:3])}."
            )
        elif preferred_title_lines:
            primary_finding = (
                "The strongest available evidence is comparative rather than authoritative; "
                f"recurring source clusters include {', '.join(preferred_title_lines[:3])}."
            )
        else:
            primary_finding = (
                "The strongest available evidence is comparative rather than authoritative."
            )
    if not primary_finding and citation_titles:
        primary_finding = citation_titles[0]
    if not primary_finding and highlights:
        primary_finding = highlights[0]
    if not primary_finding:
        return {}

    supporting = preferred_title_lines[:] if comparison_like and preferred_title_lines else highlights[:]
    if supporting and supporting[0] == primary_finding:
        supporting = supporting[1:]

    key_findings: list[str] = []
    if comparison_like and preferred_title_lines:
        key_findings.extend(preferred_title_lines[:3])
    else:
        for item in highlights[:3]:
            if item not in key_findings:
                key_findings.append(item)
        if not key_findings:
            for title in citation_title_lines[:3]:
                if title not in key_findings:
                    key_findings.append(title)

    evidence_highlights: list[str] = []
    for item in supporting[:3]:
        if item not in evidence_highlights:
            evidence_highlights.append(item)

    provider_roles: list[str] = []
    providers = [str(item) for item in (evidence.get("providers_consulted") or []) if item]
    if "tavily" in providers:
        provider_roles.append("Tavily handled broad discovery and initial ranking.")
    if evidence.get("page_count"):
        provider_roles.append(
            f"Firecrawl/extract captured full content for {int(evidence.get('page_count') or 0)} page(s)."
        )
    exa_unique = int(evidence.get("exa_unique_url_count") or 0)
    if exa_unique > 0:
        provider_roles.append(
            f"Exa expanded semantic coverage with {exa_unique} unique candidate URL(s)."
        )
    docs_rescue_count = int(evidence.get("docs_rescue_result_count") or 0)
    if docs_rescue_count > 0:
        provider_roles.append(
            f"Docs rescue surfaced {docs_rescue_count} product-native or supporting candidate result(s)."
        )
    if social_answer:
        provider_roles.append("xAI added social or synthesis context to the research pass.")
    arbitration_summary = str(evidence.get("xai_arbitration_summary") or "").strip()
    if arbitration_summary:
        provider_roles.append("xAI arbitrated conflicting evidence across providers.")

    coverage_bits: list[str] = []
    if providers:
        coverage_bits.append(f"providers={', '.join(providers)}")
    page_count = int(evidence.get("page_count") or 0)
    requested_pages = int((evidence.get("research_plan") or {}).get("scrape_top_n") or 0)
    if requested_pages > 0:
        coverage_bits.append(f"pages={page_count}/{requested_pages}")
    citation_count = int(evidence.get("citation_count") or 0)
    if citation_count > 0:
        coverage_bits.append(f"citations={citation_count}")
    if exa_unique > 0:
        coverage_bits.append(f"exa_unique_urls={exa_unique}")
    source_diversity = int(evidence.get("source_diversity") or 0)
    if source_diversity > 0:
        coverage_bits.append(f"source_domains={source_diversity}")
    if authoritative_source_count > 0:
        coverage_bits.append(f"authoritative_sources={authoritative_source_count}")
    if supporting_source_count > 0:
        coverage_bits.append(f"supporting_sources={supporting_source_count}")
    if community_source_count > 0:
        coverage_bits.append(f"community_sources={community_source_count}")
    confidence = str(evidence.get("confidence") or "").strip()
    if confidence:
        coverage_bits.append(f"confidence={confidence}")
    social_signal = social_answer if social_answer and social_answer != primary_finding else ""
    source_clusters = _build_research_source_clusters(
        query=query,
        mode=report_mode,
        ordered_results=ordered_results,
        include_domains=None,
        authoritative_preferred=authoritative_research,
    )
    claim_evidence = _build_research_claim_evidence(
        query=query,
        mode=report_mode,
        ordered_results=ordered_results,
        pages=pages,
        citations=citations,
        comparison_like=comparison_like,
        include_domains=None,
        authoritative_preferred=authoritative_research,
    )
    claim_by_url: dict[str, str] = {}
    for item in claim_evidence:
        claim_text = str(item.get("claim") or "").strip()
        if not claim_text:
            continue
        for claim_url in item.get("urls") or []:
            normalized_url = str(claim_url or "").strip()
            if normalized_url and normalized_url not in claim_by_url:
                claim_by_url[normalized_url] = claim_text
    significant_conflicts = [
        str(item)
        for item in (evidence.get("conflicts") or [])
        if item and item != "social-search-unavailable"
    ]
    top_sources = preferred_title_lines[:4]

    comparison_lens: list[str] = []
    if comparison_like:
        if "search" in query_lower:
            comparison_lens = [
                "search breadth and freshness",
                "integration fit for agent workflows",
                "deployment and operational simplicity",
            ]
        elif any(token in query_lower for token in ("code", "analysis", "repo", "repository")):
            comparison_lens = [
                "code intelligence depth",
                "IDE or workflow integration",
                "operational fit and maintenance burden",
            ]
        else:
            comparison_lens = [
                "relevance and source quality",
                "coverage breadth",
                "operational trade-offs",
            ]

    comparison_rows: list[dict[str, str]] = []
    decision_table: list[dict[str, str]] = []
    decision_criteria: list[str] = []
    comparison_matrix: list[dict[str, str]] = []
    operational_tradeoffs: list[str] = []
    decision_checklist: list[dict[str, str]] = []
    if comparison_like:
        comparison_entities = _research_comparison_entities(query)
        ordered_result_by_url = {
            (item.get("url") or "").strip(): item
            for item in ordered_results
            if (item.get("url") or "").strip()
        }
        url_to_excerpt = {
            (page.get("url") or "").strip(): _build_excerpt(
                (page.get("excerpt") or page.get("content") or "").strip(),
                limit=120,
            )
            for page in pages
            if (page.get("url") or "").strip() and not page.get("error")
        }
        seen_shortlist_urls: set[str] = set()
        shortlist_urls = [
            (citation.get("url") or "").strip()
            for citation in citations
            if (citation.get("url") or "").strip()
        ]
        shortlist_urls.extend(
            (page.get("url") or "").strip()
            for page in pages
            if (page.get("url") or "").strip() and not page.get("error")
        )
        if comparison_entities:
            prioritized_shortlist_urls: list[str] = []
            seen_prioritized_urls: set[str] = set()
            project_url = next(
                (
                    url
                    for url in shortlist_urls
                    if url
                    and _research_project_candidate_kind_rank(
                        ordered_result_by_url.get(url) or {
                            "url": url,
                            "title": url_to_title.get(url, ""),
                        }
                    )
                    == 0
                ),
                "",
            )
            if project_url:
                prioritized_shortlist_urls.append(project_url)
                seen_prioritized_urls.add(project_url)
            for entity_tokens in comparison_entities[:4]:
                for url in shortlist_urls:
                    if not url or url in seen_prioritized_urls:
                        continue
                    item = ordered_result_by_url.get(url) or {
                        "url": url,
                        "title": url_to_title.get(url, ""),
                    }
                    if not comparison.research_result_matches_comparison_subject(
                        item=item,
                        entity_tokens=entity_tokens,
                    ):
                        continue
                    prioritized_shortlist_urls.append(url)
                    seen_prioritized_urls.add(url)
                    break
            shortlist_urls = prioritized_shortlist_urls + [
                url for url in shortlist_urls if url and url not in seen_prioritized_urls
            ]
        for url in shortlist_urls:
            if not url or url in seen_shortlist_urls:
                continue
            seen_shortlist_urls.add(url)
            title = url_to_title.get(url, "").strip() or url
            candidate = title
            if "github.com/" in url:
                parsed = urlparse(url)
                parts = [part for part in parsed.path.strip("/").split("/") if part]
                if len(parts) >= 2:
                    candidate = f"{parts[0]}/{parts[1]}"
            else:
                candidate = re.split(r"\s[\-|:|]\s", title, maxsplit=1)[0].strip() or title
            matching_item = next(
                (
                    item
                    for item in ordered_results
                    if (item.get("url") or "").strip() == url
                ),
                {},
            )
            cluster_label = _research_result_cluster_label(
                query=query,
                mode=report_mode,
                item=matching_item or {"url": url, "title": title},
                include_domains=None,
                authoritative_preferred=authoritative_research,
            )
            providers = [
                provider
                for provider in (
                    matching_item.get("matched_providers")
                    or [matching_item.get("provider", "")]
                )
                if provider
            ]
            evidence_note = _select_research_claim_excerpt(
                page_excerpt=(url_to_excerpt.get(url) or "").strip(),
                snippet=(matching_item.get("snippet") or "").strip(),
                content=(matching_item.get("content") or "").strip(),
            )
            canonical_doc_snippet = _research_canonical_doc_snippet_for_url(url)
            if canonical_doc_snippet and (
                not evidence_note
                or claims.research_excerpt_looks_like_navigation_noise(evidence_note)
                or not claims.research_excerpt_has_substantive_claim(evidence_note)
            ):
                evidence_note = canonical_doc_snippet
            evidence_note_lower = evidence_note.lower()
            if any(
                marker in evidence_note_lower
                for marker in (
                    "marketing copy",
                    "skip to content",
                    "you signed in with another tab",
                    "method not allowed",
                    "\"error\"",
                    "jsonrpc",
                )
            ):
                evidence_note = ""
            normalized_note = _normalize_research_claim_text(
                evidence_note,
                comparison_like=comparison_like,
            ) if evidence_note else ""
            note_is_link_index = bool(evidence_note) and claims.research_excerpt_looks_like_link_index_noise(
                evidence_note
            )
            note_is_substantive = bool(normalized_note) and claims.research_excerpt_has_substantive_claim(
                normalized_note
            )
            claim_note = claim_by_url.get(url, "").strip()
            if (
                claim_note
                and comparison_like
                and cluster_label == "official"
                and not claims.research_claim_is_generic(claim_note)
            ):
                evidence_note = claim_note
                normalized_note = _normalize_research_claim_text(
                    evidence_note,
                    comparison_like=comparison_like,
                )
                note_is_link_index = False
                note_is_substantive = bool(normalized_note) and claims.research_excerpt_has_substantive_claim(
                    normalized_note
                )
            elif claim_note and (
                not evidence_note
                or claims.research_excerpt_looks_like_navigation_noise(evidence_note)
                or note_is_link_index
                or not note_is_substantive
            ):
                evidence_note = claim_note
                normalized_note = _normalize_research_claim_text(
                    evidence_note,
                    comparison_like=comparison_like,
                )
                note_is_link_index = False
                note_is_substantive = bool(normalized_note) and claims.research_excerpt_has_substantive_claim(
                    normalized_note
                )
            if not evidence_note or (
                cluster_label == "official" and (note_is_link_index or not note_is_substantive)
            ):
                title_note = _normalize_research_claim_text(
                    candidate or title,
                    comparison_like=comparison_like,
                )
                if title_note and (
                    cluster_label == "official"
                    or not claims.research_claim_is_generic(title_note)
                ):
                    evidence_note = title_note
            if not evidence_note:
                evidence_note = query_routing._registered_domain(query_routing._result_hostname({"url": url}))
            comparison_rows.append(
                {
                    "candidate": candidate[:80],
                    "url": url,
                    "source": query_routing._registered_domain(query_routing._result_hostname({"url": url})) or url,
                    "cluster": cluster_label,
                    "provider_support": " + ".join(providers[:3]) if providers else "unknown",
                    "note": evidence_note[:140],
                }
            )
            if len(comparison_rows) >= 4:
                break
        if comparison_entities and comparison_rows:
            comparison_top_sources: list[str] = []
            for row in comparison_rows[:4]:
                row_url = str(row.get("url") or "").strip()
                row_title = url_to_title.get(row_url, "").strip() or str(row.get("candidate") or "").strip()
                if not row_title:
                    continue
                domain = query_routing._registered_domain(query_routing._result_hostname({"url": row_url}))
                line = f"{row_title} ({domain})" if domain and domain not in row_title.lower() else row_title
                if line not in comparison_top_sources:
                    comparison_top_sources.append(line)
            if len(comparison_top_sources) >= 2:
                top_sources = comparison_top_sources[:4]
        for row in comparison_rows[:4]:
            cluster_label = str(row.get("cluster") or "").strip()
            cluster_detail = next(
                (
                    item
                    for item in source_clusters
                    if str(item.get("label") or "").strip() == cluster_label
                ),
                {},
            )
            decision_table.append(
                {
                    "candidate": str(row.get("candidate") or "").strip(),
                    "fit": comparison.research_cluster_fit_summary(cluster_label),
                    "strengths": comparison.research_decision_strengths(
                        cluster_label=cluster_label,
                        provider_support=str(row.get("provider_support") or "").strip(),
                        note=str(row.get("note") or "").strip(),
                        cluster_detail=cluster_detail,
                    ),
                    "cautions": comparison.research_decision_cautions(
                        cluster_label=cluster_label,
                        provider_support=str(row.get("provider_support") or "").strip(),
                    ),
                }
            )
        focus_rows = _research_select_comparison_focus_rows(
            comparison_rows=comparison_rows,
            comparison_entities=comparison_entities,
            selected_urls=list(ordered_result_by_url.keys()),
        )
        if focus_rows:
            comparison_rows = [dict(row) for row in focus_rows[:4]]
            visible_candidates = {
                str(row.get("candidate") or "").strip()
                for row in comparison_rows
                if str(row.get("candidate") or "").strip()
            }
            if visible_candidates and decision_table:
                filtered_decision_table: list[dict[str, str]] = []
                seen_decision_candidates: set[str] = set()
                for row in decision_table:
                    candidate = str(row.get("candidate") or "").strip()
                    if (
                        not candidate
                        or candidate in seen_decision_candidates
                        or candidate not in visible_candidates
                    ):
                        continue
                    filtered_decision_table.append(dict(row))
                    seen_decision_candidates.add(candidate)
                if filtered_decision_table:
                    decision_table = filtered_decision_table[:4]
        decision_criteria = _research_build_decision_criteria(
            focus_rows=focus_rows,
        )
        comparison_matrix = comparison.research_build_comparison_matrix(
            focus_rows=focus_rows,
        )
        operational_tradeoffs = comparison.research_build_operational_tradeoffs(
            focus_rows=focus_rows,
        )
        decision_checklist = comparison.research_build_decision_checklist(
            focus_rows=focus_rows,
        )
        if focus_rows:
            comparison_visible_sources: list[str] = []
            for row in focus_rows[:3]:
                row_url = str(row.get("url") or "").strip()
                row_title = url_to_title.get(row_url, "").strip() or str(row.get("candidate") or "").strip()
                if not row_title:
                    continue
                domain = query_routing._registered_domain(query_routing._result_hostname({"url": row_url}))
                line = f"{row_title} ({domain})" if domain and domain not in row_title.lower() else row_title
                if line not in comparison_visible_sources:
                    comparison_visible_sources.append(line)
            if comparison_visible_sources:
                top_sources = comparison_visible_sources[:4]
            if len(comparison_visible_sources) >= 2:
                top_sources = comparison_visible_sources[:4]
                key_findings = comparison_visible_sources[:3]
                if decision_criteria:
                    evidence_highlights = decision_criteria[:2]
        claim_evidence = _align_research_claims_with_comparison_rows(
            claim_evidence=claim_evidence,
            comparison_rows=focus_rows or comparison_rows,
            comparison_entities=comparison_entities,
        )
        if focus_rows and comparison_entities:
            focused_claims: list[dict[str, Any]] = []
            seen_claim_signatures: set[str] = set()
            for entity_tokens in comparison_entities[: len(focus_rows)]:
                focus_row = next(
                    (
                        row
                        for row in focus_rows
                        if claims.research_claim_comparison_subject_match_count(
                            claim=" ".join(
                                bit
                                for bit in (
                                    str(row.get("candidate") or "").strip(),
                                    str(row.get("note") or "").strip(),
                                )
                                if bit
                            ),
                            sources=[
                                str(row.get("candidate") or "").strip(),
                                str(row.get("source") or "").strip(),
                            ],
                            entities=[entity_tokens],
                        )
                        > 0
                    ),
                    None,
                )
                matching_claim = _research_claim_entry_from_focus_row(focus_row) if focus_row else None
                if not matching_claim:
                    matching_claim = next(
                        (
                            item
                            for item in claim_evidence
                            if claims.research_claim_comparison_subject_match_count(
                                claim=str(item.get("claim") or "").strip(),
                                sources=[
                                    str(source).strip()
                                    for source in (item.get("sources") or [])
                                    if str(source).strip()
                                ],
                                entities=[entity_tokens],
                            )
                            > 0
                        ),
                        None,
                    )
                if not matching_claim:
                    continue
                signature = claims.research_claim_signature(
                    str(matching_claim.get("claim") or "").strip()
                )
                if not signature or signature in seen_claim_signatures:
                    continue
                focused_claims.append(dict(matching_claim))
                seen_claim_signatures.add(signature)
            if focused_claims:
                claim_evidence = focused_claims[:4]

    top_claim = _select_research_primary_claim(claim_evidence)
    top_claim_text = str(top_claim.get("claim") or "").strip()
    if (
        top_claim_text
        and str(top_claim.get("support_level") or "") != "single-source"
        and not claims.research_claim_is_generic(top_claim_text)
    ):
        support_phrase = _research_claim_support_phrase(top_claim)
        if comparison_like and decision_table:
            primary_finding = (
                f"{decision_table[0]['candidate']} is the strongest current fit "
                f"for {decision_table[0]['fit']}."
            )
            if top_claim_text:
                primary_finding += f" {top_claim_text}."
        else:
            primary_finding = top_claim_text
        if support_phrase:
            primary_finding += f" {support_phrase.capitalize()}."

    comparison_subject_phrase = _research_comparison_subject_phrase(query)
    if comparison_like and decision_criteria:
        summary_bits: list[str] = []
        if len(comparison_rows) == 1 and decision_table:
            fit_sentence = (
                f"{decision_table[0]['candidate']} is the strongest current fit "
                f"for {decision_table[0]['fit']}."
            )
            summary_bits.append(fit_sentence)
        else:
            summary_bits.append(" ".join(item.strip() for item in decision_criteria[:2] if item.strip()))
        summary_claim_text = ""
        summary_claim_entry = next(
            (
                item
                for item in claim_evidence
                if (
                    str(item.get("claim") or "").strip()
                    and not claims.research_claim_is_generic(str(item.get("claim") or "").strip())
                    and claims.research_excerpt_has_substantive_claim(str(item.get("claim") or "").strip())
                    and str(item.get("claim") or "").strip()
                    != _normalize_research_claim_text(
                        str((item.get("sources") or [""])[0] or "").strip(),
                        comparison_like=True,
                    )
                )
            ),
            {},
        )
        if summary_claim_entry:
            summary_claim_text = str(summary_claim_entry.get("claim") or "").strip()
        top_claim_support = _research_claim_support_phrase(
            summary_claim_entry or top_claim
        )
        top_claim_sentence = ""
        if summary_claim_text:
            top_claim_sentence = summary_claim_text
            if not top_claim_sentence.endswith("."):
                top_claim_sentence += "."
            if top_claim_support:
                top_claim_sentence += f" {top_claim_support.capitalize()}."
        if top_claim_sentence:
            summary_bits.append(top_claim_sentence)
        elif len(comparison_rows) == 1 and top_claim_support:
            summary_bits.append(f"{top_claim_support.capitalize()}.")
        support_summary = comparison.research_comparison_support_summary(
            authoritative_source_count=authoritative_source_count,
            supporting_source_count=supporting_source_count,
        )
        if support_summary:
            summary_bits.append(support_summary)
        if comparison_subject_phrase:
            summary_bits.append(
                f"{comparison_subject_phrase} is the core comparison for this query."
            )
        visible_anchors = [str(item).strip() for item in top_sources[:2] if str(item).strip()]
        if len(visible_anchors) >= 2:
            summary_bits.append(
                f"The strongest anchors are {visible_anchors[0]}, {visible_anchors[1]}."
            )
        elif visible_anchors:
            summary_bits.append(f"The strongest anchor is {visible_anchors[0]}.")
        primary_finding = " ".join(bit for bit in summary_bits if bit).strip()
    elif comparison_like and comparison_subject_phrase:
        primary_finding_lower = primary_finding.lower()
        if comparison_subject_phrase.lower() not in primary_finding_lower:
            primary_finding = (
                f"{comparison_subject_phrase} is the core comparison for this query. "
                f"{primary_finding}"
            ).strip()

    consensus_snapshot: list[str] = []
    for item in claim_evidence[:3]:
        claim = str(item.get("claim") or "").strip()
        if not claim:
            continue
        support_phrase = _research_claim_support_phrase(item)
        if support_phrase:
            consensus_snapshot.append(f"{claim} ({support_phrase})")
        else:
            consensus_snapshot.append(claim)

    recommendation = ""
    if comparison_like:
        primary_claim = _select_research_primary_claim(claim_evidence)
        primary_support_phrase = _research_claim_support_phrase(primary_claim)
        runner_up = decision_table[1] if len(decision_table) > 1 else {}
        response_candidate = next(
            (
                str(row.get("candidate") or "").strip()
                for row in comparison_rows
                if "response" in str(row.get("candidate") or "").lower()
            ),
            "",
        )
        batch_candidate = next(
            (
                str(row.get("candidate") or "").strip()
                for row in comparison_rows
                if "batch" in str(row.get("candidate") or "").lower()
            ),
            "",
        )
        has_batch_faq_signal = any(
            "batch api faq" in str(item.get("title") or "").lower()
            or "9197833-batch-api-faq" in str(item.get("url") or "").lower()
            for item in citations
        )
        has_background_signal = any(
            "background mode" in str(item.get("title") or "").lower()
            or "/background/" in str(item.get("url") or "").lower()
            for item in citations
        )
        if response_candidate and batch_candidate:
            recommendation = (
                f"Choose {response_candidate} for interactive or tool-using flows that need iterative request/response control. "
                f"Choose {batch_candidate} for bulk asynchronous workloads"
            )
            if has_batch_faq_signal:
                recommendation += " when you can tolerate up to 24 hours of turnaround and want discounted throughput."
            else:
                recommendation += " when throughput matters more than immediate latency."
            if has_background_signal:
                recommendation += (
                    " Use Background mode when a single long-running workflow should continue asynchronously "
                    "without holding the client request open."
                )
        elif authoritative_source_count > 0 and decision_table:
            recommendation = (
                f"Start from {decision_table[0]['candidate']} as the primary anchor, "
                "then use the remaining shortlisted sources to validate trade-offs and edge cases."
            )
            if primary_support_phrase:
                recommendation += f" {primary_support_phrase.capitalize()}."
            if runner_up:
                recommendation += (
                    f" Use {runner_up['candidate']} as the leading counterpoint when checking "
                    f"{runner_up['fit']} trade-offs."
                )
        elif decision_table:
            recommendation = (
                f"Treat {decision_table[0]['candidate']} as the leading candidate for now, "
                "but keep the next shortlisted sources in scope because the evidence is still comparative."
            )
            if primary_support_phrase:
                recommendation += f" {primary_support_phrase.capitalize()}."
            if runner_up:
                recommendation += (
                    f" {runner_up['candidate']} remains the strongest alternate angle for "
                    f"{runner_up['fit']}."
                )
        if recommendation and comparison_subject_phrase:
            recommendation_lower = recommendation.lower()
            if comparison_subject_phrase.lower() not in recommendation_lower:
                recommendation = (
                    f"Keep {comparison_subject_phrase} as the explicit comparison frame. "
                    f"{recommendation}"
                )

    visible_source_domains = _research_report_source_domains(top_sources)
    supporting_context: list[str] = []
    if comparison_like:
        visible_urls = {
            str(row.get("url") or "").strip()
            for row in comparison_rows
            if str(row.get("url") or "").strip()
        }
        seen_supporting_context: set[str] = set()
        for item in ordered_results:
            url = str(item.get("url") or "").strip()
            if not url or url in visible_urls:
                continue
            cluster_label = _research_result_cluster_label(
                query=query,
                mode=report_mode,
                item=item,
                include_domains=None,
                authoritative_preferred=authoritative_research,
            )
            if cluster_label not in {"official", "supporting"}:
                continue
            title = str(item.get("title") or url_to_title.get(url) or "").strip()
            candidate = re.split(r"\s[\-|:|]\s", title, maxsplit=1)[0].strip() or title
            note = claim_by_url.get(url, "").strip()
            if not note:
                matching_page = next(
                    (
                        page
                        for page in pages
                        if (page.get("url") or "").strip() == url and not page.get("error")
                    ),
                    {},
                )
                note = _select_research_claim_excerpt(
                    page_excerpt=(matching_page.get("excerpt") or "").strip(),
                    snippet=(item.get("snippet") or "").strip(),
                    content=(matching_page.get("content") or item.get("content") or "").strip(),
                )
            canonical_doc_snippet = _research_canonical_doc_snippet_for_url(url)
            title_lower = title.lower()
            if canonical_doc_snippet and (
                str(item.get("provider") or "") != "canonical_research_docs"
                or any(
                    marker in title_lower
                    for marker in ("faq", "overview", "pricing", "rate limits", "background")
                )
            ):
                note = canonical_doc_snippet
            if canonical_doc_snippet and (
                not note
                or claims.research_excerpt_looks_like_navigation_noise(note)
                or not claims.research_excerpt_has_substantive_claim(note)
            ):
                note = canonical_doc_snippet
            note = _normalize_research_claim_text(
                note,
                comparison_like=comparison_like,
            ) if note else ""
            if (
                not note
                or claims.research_claim_is_generic(note)
                or not claims.research_excerpt_has_substantive_claim(note)
            ):
                continue
            prefix = candidate or title
            if prefix and prefix.lower() not in note.lower():
                if note[:1].islower():
                    context_line = f"{prefix} {note}"
                else:
                    context_line = f"{prefix}: {note}"
            else:
                context_line = note
            signature = claims.research_claim_signature(context_line)
            if not signature or signature in seen_supporting_context:
                continue
            seen_supporting_context.add(signature)
            supporting_context.append(context_line)
            if len(supporting_context) >= 3:
                break
        if len(supporting_context) < 3:
            for citation in citations:
                url = str(citation.get("url") or "").strip()
                if not url or url in visible_urls or not _research_is_canonical_vendor_doc(url):
                    continue
                pseudo_item = {
                    "provider": "canonical_research_docs",
                    "title": str(citation.get("title") or "").strip(),
                    "url": url,
                    "matched_providers": ["canonical_research_docs"],
                }
                cluster_label = _research_result_cluster_label(
                    query=query,
                    mode=report_mode,
                    item=pseudo_item,
                    include_domains=None,
                    authoritative_preferred=authoritative_research,
                )
                if cluster_label not in {"official", "supporting"}:
                    continue
                title = str(citation.get("title") or url_to_title.get(url) or "").strip()
                candidate = re.split(r"\s[\-|:|]\s", title, maxsplit=1)[0].strip() or title
                canonical_doc_snippet = _research_canonical_doc_snippet_for_url(url)
                note = canonical_doc_snippet or claim_by_url.get(url, "").strip()
                note = _normalize_research_claim_text(
                    note,
                    comparison_like=comparison_like,
                ) if note else ""
                if (
                    not note
                    or claims.research_claim_is_generic(note)
                    or not claims.research_excerpt_has_substantive_claim(note)
                ):
                    continue
                if candidate and candidate.lower() not in note.lower():
                    if note[:1].islower():
                        context_line = f"{candidate} {note}"
                    else:
                        context_line = f"{candidate}: {note}"
                else:
                    context_line = note
                signature = claims.research_claim_signature(context_line)
                if not signature or signature in seen_supporting_context:
                    continue
                seen_supporting_context.add(signature)
                supporting_context.append(context_line)
                if len(supporting_context) >= 3:
                    break
    return {
        "executive_summary": primary_finding,
        "key_findings": key_findings[:3],
        "evidence_highlights": evidence_highlights[:3],
        "supporting_context": supporting_context[:3],
        "consensus_snapshot": consensus_snapshot[:3],
        "provider_roles": provider_roles[:4],
        "coverage_bits": coverage_bits,
        "claim_evidence": claim_evidence[:4],
        "claim_level_evidence": claim_evidence[:4],
        "source_clusters": source_clusters[:5],
        "social_signal": social_signal,
        "caveats": significant_conflicts[:4],
        "top_sources": top_sources,
        "source_mix": [
            bit
            for bit in (
                f"authoritative={authoritative_source_count}" if authoritative_source_count > 0 else "",
                f"supporting={supporting_source_count}" if supporting_source_count > 0 else "",
                f"community={community_source_count}" if community_source_count > 0 else "",
                (
                    f"domains={', '.join(visible_source_domains)}"
                    if visible_source_domains
                    else (
                        f"domains={', '.join(evidence.get('selected_candidate_domains') or [])}"
                        if evidence.get("selected_candidate_domains")
                        else ""
                    )
                ),
            )
            if bit
        ],
        "comparison_lens": comparison_lens,
        "comparison_rows": comparison_rows,
        "decision_table": decision_table,
        "decision_criteria": decision_criteria,
        "comparison_matrix": comparison_matrix,
        "operational_tradeoffs": operational_tradeoffs,
        "decision_checklist": decision_checklist,
        "recommendation": recommendation,
    }


def _build_research_report_source_lines(
    *,
    query: str,
    mode: str,
    ordered_results: list[dict[str, Any]],
    citations: list[dict[str, Any]],
    include_domains: list[str] | None,
    authoritative_preferred: bool,
    comparison_like: bool,
    max_items: int = 4,
) -> list[str]:
    mode_name = cast(SearchMode, mode if mode in SEARCH_MODES else "web")
    source_entries: list[dict[str, str]] = []
    seen_lines: set[str] = set()

    def add_source_entry(item: dict[str, Any]) -> None:
        title = (item.get("title") or "").strip()
        if not title:
            return
        domain = query_routing._registered_domain(query_routing._result_hostname(item))
        dedupe_key = _research_source_topic_key(
            title=title,
            domain=domain or (item.get("url") or "").strip(),
        )
        if dedupe_key in seen_lines:
            return
        line = f"{title} ({domain})" if domain and domain not in title.lower() else title
        if not line:
            return
        seen_lines.add(dedupe_key)
        cluster_label = _research_result_cluster_label(
            query=query,
            mode=mode_name,
            item=item,
            include_domains=include_domains,
            authoritative_preferred=authoritative_preferred,
        )
        source_entries.append({"line": line, "cluster": cluster_label})

    for item in ordered_results:
        add_source_entry(item)
    if not source_entries:
        for citation in citations:
            add_source_entry(
                {
                    "title": (citation.get("title") or "").strip(),
                    "url": (citation.get("url") or "").strip(),
                    "snippet": "",
                }
            )
    if not source_entries:
        return []

    if authoritative_preferred:
        official_count = sum(
            1 for item in source_entries if item["cluster"] == "official"
        )
        supporting_count = sum(
            1 for item in source_entries if item["cluster"] == "supporting"
        )
        anchor_count = sum(
            1
            for item in source_entries
            if item["cluster"] in {"official", "supporting"}
        )
        if official_count >= 2 and supporting_count >= 1:
            cluster_caps = {"official": max_items, "supporting": 1}
        elif anchor_count >= 3:
            cluster_caps = {"official": max_items, "supporting": max_items}
        elif anchor_count >= 2:
            cluster_caps = {
                "official": max_items,
                "supporting": max_items,
                "general": 1,
            }
        else:
            cluster_caps = {
                "official": max_items,
                "supporting": max_items,
                "general": 2,
                "community": 1,
            }
    elif comparison_like:
        anchor_count = sum(
            1
            for item in source_entries
            if item["cluster"] in {"project", "supporting"}
        )
        if anchor_count >= 3:
            cluster_caps = {"project": max_items, "supporting": max_items}
        elif anchor_count >= 2:
            cluster_caps = {
                "project": max_items,
                "supporting": max_items,
                "curated": 1,
            }
        else:
            cluster_caps = {
                "project": max_items,
                "supporting": max_items,
                "curated": 2,
                "listicle": 1,
                "directory": 1,
                "community": 1,
            }
    else:
        cluster_caps = {}

    if not cluster_caps:
        return [item["line"] for item in source_entries[:max_items]]

    selected_lines: list[str] = []
    cluster_counts: dict[str, int] = {}
    for item in source_entries:
        cluster_label = item["cluster"]
        line = item["line"]
        cap = cluster_caps.get(cluster_label, 0)
        if cap and cluster_counts.get(cluster_label, 0) < cap and len(selected_lines) < max_items:
            selected_lines.append(line)
            cluster_counts[cluster_label] = cluster_counts.get(cluster_label, 0) + 1
    return selected_lines[:max_items]


def _build_research_source_clusters(
    *,
    query: str,
    mode: str,
    ordered_results: list[dict[str, Any]],
    include_domains: list[str] | None,
    authoritative_preferred: bool,
) -> list[dict[str, Any]]:
    if not ordered_results:
        return []

    cluster_buckets: dict[str, dict[str, Any]] = {}
    for item in ordered_results:
        label = _research_result_cluster_label(
            query=query,
            mode=cast(SearchMode, mode if mode in SEARCH_MODES else "web"),
            item=item,
            include_domains=include_domains,
            authoritative_preferred=authoritative_preferred,
        )
        bucket = cluster_buckets.setdefault(
            label,
            {
                "label": label,
                "count": 0,
                "domains": [],
                "providers": [],
                "cross_provider_count": 0,
            },
        )
        bucket["count"] += 1
        domain = query_routing._registered_domain(query_routing._result_hostname(item))
        if domain and domain not in bucket["domains"]:
            bucket["domains"].append(domain)
        matched_providers = [
            provider
            for provider in (
            item.get("matched_providers")
            or [item.get("provider", "")]
            )
            if provider
        ]
        if len(set(matched_providers)) > 1:
            bucket["cross_provider_count"] += 1
        for provider in matched_providers:
            if provider and provider not in bucket["providers"]:
                bucket["providers"].append(provider)

    for bucket in cluster_buckets.values():
        provider_support = len(bucket.get("providers") or [])
        cross_provider_count = int(bucket.get("cross_provider_count") or 0)
        label = str(bucket.get("label") or "")
        base_weight = _research_cluster_base_weight(
            label=label,
            authoritative_preferred=authoritative_preferred,
        )
        weight = round(
            base_weight
            + float(bucket.get("count") or 0) * 1.4
            + provider_support * 0.8
            + cross_provider_count * 1.2,
            2,
        )
        bucket["provider_support_count"] = provider_support
        bucket["weight"] = weight
        bucket["tier"] = _research_cluster_tier(weight=weight, label=label)

    preferred_order = (
        ["official", "supporting", "general", "community"]
        if authoritative_preferred
        else ["project", "supporting", "curated", "listicle", "directory", "community"]
    )
    return sorted(
        cluster_buckets.values(),
        key=lambda item: (
            -float(item.get("weight") or 0),
            preferred_order.index(item["label"])
            if item["label"] in preferred_order
            else len(preferred_order),
            -int(item.get("count") or 0),
        ),
    )


def _dedupe_research_claims_by_source_topic(
    claims: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if not claims:
        return []
    deduped: list[dict[str, Any]] = []
    seen_topics: set[str] = set()
    best_entries: dict[str, dict[str, Any]] = {}
    topic_order: list[str] = []
    for entry in claims:
        sources = [str(item).strip() for item in (entry.get("sources") or []) if str(item).strip()]
        domains = [str(item).strip() for item in (entry.get("domains") or []) if str(item).strip()]
        topic_key = _research_source_topic_key(
            title=sources[0] if sources else str(entry.get("claim") or "").strip(),
            domain=domains[0] if domains else "unknown",
        )
        if topic_key not in seen_topics:
            seen_topics.add(topic_key)
            topic_order.append(topic_key)
            best_entries[topic_key] = entry
            continue
        current_best = best_entries[topic_key]
        if _research_claim_source_kind_rank(entry) < _research_claim_source_kind_rank(current_best):
            best_entries[topic_key] = entry
    for topic_key in topic_order:
        deduped.append(best_entries[topic_key])
        if len(deduped) >= limit:
            break
    return deduped[:limit]


def _diversify_research_claims_by_domain(
    claims: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if len(claims) <= limit:
        return claims

    selected: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    used_domains: set[str] = set()
    for entry in claims:
        domains = [str(item) for item in (entry.get("domains") or []) if item]
        if domains and all(domain in used_domains for domain in domains):
            deferred.append(entry)
            continue
        selected.append(entry)
        used_domains.update(domains)
        if len(selected) >= limit:
            return selected
    for entry in deferred:
        if len(selected) >= limit:
            break
        selected.append(entry)
    return selected[:limit]


def _normalize_research_claim_text(
    text: str,
    *,
    comparison_like: bool,
) -> str:
    compact = re.sub(r"[\u200b\u200c\u200d\ufeff]", " ", text)
    compact = compact.replace("**", " ").replace("__", " ").replace("`", " ")
    compact = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", compact)
    compact = re.sub(r"\[[^\]]+\]\([^)]+\)", " ", compact)
    compact = re.sub(
        r"(?i)\b(copy\s*pagecopy|copy\s+markdown|open\s+in\s+chatgpt|view\s+as\s+markdown|copy\s+page|view\s+page)\b",
        " ",
        compact,
    )
    compact = re.sub(r"(?i)^\s*(?:copy\s+pagecopy|copy\s+page|copy)\s+", "", compact).strip()
    compact = re.sub(r"\s*#+\s*", " ", compact)
    compact = compact.replace("*", " ")
    compact = re.sub(
        r"(?i)\b(master this essential documentation concept|quick definition|table of contents)\b",
        " ",
        compact,
    )
    compact = re.sub(r"\s+", " ", compact).strip(" -|:;,.")
    if claims.research_excerpt_looks_like_json_shell(compact):
        return ""
    heading_match = re.match(
        r"^#\s*[A-Z][A-Za-z0-9'’&./() \-]{1,80}?\s+"
        r"((?:create|use|build|manage|run|stream|process|compare|choose|support|supports|allow|allows|enable|enables|let|lets|track|learn|handle)\b.+)$",
        compact,
        flags=re.IGNORECASE,
    )
    if heading_match:
        compact = heading_match.group(1).strip()
    compact = re.sub(
        r"(?i)^[A-Z][A-Za-z0-9'’&./() \-]{5,100}\s+"
        r"[A-Z][a-z]+\s+\d{1,2}(?:st|nd|rd|th),\s+\d{4}\s+by\s+"
        r"[A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){0,3}\s+(?:abstract\s+)?",
        "",
        compact,
    )
    compact = re.sub(r"(?i)^comparison\s+", "", compact).strip()
    compact = re.sub(r"(?i)^summary\s*:\s*", "", compact).strip()
    compact = re.sub(r"(?i)^tl\s*;?\s*dr\s*:\s*", "", compact).strip()
    compact = re.sub(r"(?i)^abstract\s+", "", compact).strip()
    compact = re.sub(
        r"(?i)^[A-Z][a-z]+\s+\d{1,2}(?:st|nd|rd|th),\s+\d{4}\s+by\s+"
        r"[A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){0,3}(?=\s+(?:abstract\b|$))\s*",
        "",
        compact,
    )
    compact = re.sub(r"(?i)^abstract\s+", "", compact).strip()
    compact = re.sub(r"^[#>*`\-\d\.\)\s]+", "", compact).strip()
    compact = re.sub(r"https?://\S+", "", compact).strip()
    compact = compact.replace("\\_", "_")
    if not compact:
        return ""
    if claims.research_excerpt_looks_like_json_shell(compact):
        return ""
    if comparison_like:
        compact = re.split(r"\s(?:[-:|–—])\s", compact, maxsplit=1)[0].strip() or compact
    compact = _build_excerpt(compact, limit=160)
    if claims.research_excerpt_looks_like_noise(compact):
        return ""
    return compact


def _research_authoritative_claim_fallback(
    *,
    query: str,
    mode: str,
    ordered_results: list[dict[str, Any]],
    include_domains: list[str] | None,
    authoritative_preferred: bool,
) -> dict[str, Any]:
    resolved_mode = cast(SearchMode, mode if mode in SEARCH_MODES else "web")
    for item in ordered_results:
        cluster_label = _research_result_cluster_label(
            query=query,
            mode=resolved_mode,
            item=item,
            include_domains=include_domains,
            authoritative_preferred=authoritative_preferred,
        )
        if cluster_label not in {"official", "supporting"}:
            continue
        title = (item.get("title") or "").strip()
        claim = _normalize_research_claim_text(title, comparison_like=True)
        if not claim:
            continue
        source_label = title or (
            query_routing._registered_domain(query_routing._result_hostname(item)) or (item.get("url") or "")
        )
        return {
            "claim": claim,
            "sources": [source_label] if source_label else [],
            "providers": [
                provider
                for provider in (item.get("matched_providers") or [item.get("provider", "")])
                if provider
            ],
            "clusters": [cluster_label],
            "source_count": 1 if source_label else 0,
            "provider_count": len(
                [
                    provider
                    for provider in (item.get("matched_providers") or [item.get("provider", "")])
                    if provider
                ]
            ),
            "cluster_count": 1,
            "support_level": "single-source",
            "_order_index": -1,
        }
    return {}


def _research_authoritative_query_tokens(
    query: str,
) -> list[str]:
    generic_tokens = {
        "agent",
        "agentic",
        "agents",
        "approach",
        "best",
        "build",
        "docs",
        "documentation",
        "guide",
        "official",
        "retrieval",
        "search",
        "workflow",
        "workflows",
    }
    return [
        token
        for token in query_routing._query_brand_tokens(query)
        if token not in generic_tokens
    ]


def _research_build_decision_criteria(
    *,
    focus_rows: Sequence[Mapping[str, Any]],
) -> list[str]:
    criteria: list[str] = []
    focus_profiles: list[tuple[str, dict[str, str]]] = []
    for row in focus_rows[:3]:
        candidate = str(row.get("candidate") or "").strip()
        if not candidate:
            continue
        profile = comparison.research_comparison_profile(
            candidate=candidate,
            note=str(row.get("note") or "").strip(),
            fit=comparison.research_cluster_fit_summary(str(row.get("cluster") or "").strip()),
            url=str(row.get("url") or "").strip(),
        )
        focus_profiles.append((candidate, profile))
        criteria.append(
            f"Use {candidate} when you need {profile['best_for']}."
        )
    responses_candidate = next(
        (
            candidate
            for candidate, _profile in focus_profiles
            if "responses" in candidate.lower() or "response" in candidate.lower()
        ),
        "",
    )
    batch_candidate = next(
        (
            candidate
            for candidate, _profile in focus_profiles
            if "batch" in candidate.lower()
        ),
        "",
    )
    background_candidate = next(
        (
            candidate
            for candidate, _profile in focus_profiles
            if "background" in candidate.lower()
        ),
        "",
    )
    if responses_candidate and batch_candidate:
        criteria.append(
            f"Prefer {batch_candidate} when discounted throughput matters more than immediate latency."
        )
        criteria.append(
            f"Prefer {responses_candidate} when iterative request/response control matters more than offline throughput."
        )
    if background_candidate:
        criteria.append(
            f"Reach for {background_candidate} when work should continue asynchronously without holding the client connection open."
        )
    return criteria[:4]


def _research_canonical_doc_catalog() -> dict[str, list[dict[str, Any]]]:
    return {
        "tavily": [
            {
                "provider": "canonical_research_docs",
                "title": "Search API - Tavily",
                "url": "https://docs.tavily.com/documentation/api-reference/search",
                "snippet": "Tavily exposes a search API for web retrieval, real-time discovery, and agent search workflows.",
            },
            {
                "provider": "canonical_research_docs",
                "title": "Extract API - Tavily",
                "url": "https://docs.tavily.com/documentation/api-reference/extract",
                "snippet": "Tavily exposes an extract API for content extraction and document retrieval workflows.",
            },
        ],
        "firecrawl": [
            {
                "provider": "canonical_research_docs",
                "title": "Scrape - Firecrawl Docs",
                "url": "https://docs.firecrawl.dev/api-reference/endpoint/scrape",
                "snippet": "Firecrawl provides a scrape API for markdown extraction, page retrieval, and dynamic site capture.",
            },
            {
                "provider": "canonical_research_docs",
                "title": "Extract - Firecrawl Docs",
                "url": "https://docs.firecrawl.dev/api-reference/endpoint/extract",
                "snippet": "Firecrawl provides an extract API for structured extraction across URLs, domains, and documents.",
            },
        ],
        "exa": [
            {
                "provider": "canonical_research_docs",
                "title": "Search - Exa Docs",
                "url": "https://docs.exa.ai/reference/search",
                "snippet": "Exa provides a search API for semantic web retrieval and content discovery.",
            },
            {
                "provider": "canonical_research_docs",
                "title": "Contents Retrieval - Exa",
                "url": "https://exa.ai/docs/reference/contents-retrieval",
                "snippet": "Exa supports contents retrieval for full text, summaries, highlights, and context extraction across fetched pages.",
            },
        ],
        "apify": [
            {
                "provider": "canonical_research_docs",
                "title": "Apify API documentation",
                "url": "https://docs.apify.com/api",
                "snippet": "Apify exposes a REST API for running actors, retrieving datasets, and automating large-scale web scraping workflows.",
            },
        ],
        "responses api": [
            {
                "provider": "canonical_research_docs",
                "title": "Responses Overview | OpenAI API Reference",
                "url": "https://developers.openai.com/api/reference/responses/overview/",
                "snippet": "OpenAI Responses is the primary interface for interactive and tool-using request flows with stateful model responses.",
            },
            {
                "provider": "canonical_research_docs",
                "title": "Migrate to the Responses API - OpenAI Developers",
                "url": "https://developers.openai.com/api/docs/guides/migrate-to-responses/",
                "snippet": "OpenAI recommends Responses for tool use, built-in tools, multimodal inputs, and modern interactive API workflows.",
            },
            {
                "provider": "canonical_research_docs",
                "title": "Migrate to Responses API - OpenAI Docs",
                "url": "https://platform.openai.com/docs/guides/responses-vs-chat-completions",
                "snippet": "OpenAI recommends Responses for tool use, built-in tools, multimodal inputs, and modern interactive API workflows.",
            },
            {
                "provider": "canonical_research_docs",
                "title": "Rate limits - OpenAI Developers",
                "url": "https://developers.openai.com/api/docs/guides/rate-limits/",
                "snippet": "Rate limits vary by model and tier and should be considered when interactive request flows need predictable throughput.",
            },
        ],
        "batch api": [
            {
                "provider": "canonical_research_docs",
                "title": "Batch API - OpenAI Developers",
                "url": "https://developers.openai.com/api/docs/guides/batch/",
                "snippet": "The Batch API is designed for bulk asynchronous workloads, file-backed execution, and discounted high-throughput processing.",
            },
            {
                "provider": "canonical_research_docs",
                "title": "Batch API FAQ - OpenAI Help Center",
                "url": "https://help.openai.com/en/articles/9197833-batch-api-faq",
                "snippet": "OpenAI documents that Batch API jobs can take up to 24 hours and are priced for discounted asynchronous throughput.",
            },
            {
                "provider": "canonical_research_docs",
                "title": "Pricing - OpenAI Developers",
                "url": "https://developers.openai.com/api/docs/pricing/",
                "snippet": "Batch API requests are billed at a discount compared with standard online requests, making them a better fit for high-volume asynchronous workloads.",
            },
        ],
        "background mode": [
            {
                "provider": "canonical_research_docs",
                "title": "Background mode guide - OpenAI API",
                "url": "https://developers.openai.com/api/docs/guides/background/",
                "snippet": "Background mode lets a long-running OpenAI workflow continue asynchronously without holding the client request open.",
            },
        ],
    }


def _research_canonical_doc_snippet_for_url(
    url: str,
) -> str:
    normalized = url.strip()
    if not normalized:
        return ""
    parsed = urlparse(normalized)
    normalized = parsed._replace(fragment="", query="").geturl().rstrip("/")
    for items in _research_canonical_doc_catalog().values():
        for item in items:
            candidate_url = str(item.get("url") or "").strip()
            if not candidate_url:
                continue
            candidate_parsed = urlparse(candidate_url)
            candidate_normalized = candidate_parsed._replace(
                fragment="",
                query="",
            ).geturl().rstrip("/")
            if candidate_normalized == normalized:
                return str(item.get("snippet") or "").strip()
    return ""


def _research_claim_entry_from_focus_row(
    row: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not row:
        return {}
    candidate = str(row.get("candidate") or "").strip()
    note = str(row.get("note") or "").strip()
    source = str(row.get("source") or "").strip()
    url = str(row.get("url") or "").strip()
    cluster = str(row.get("cluster") or "").strip()
    normalized_note = _normalize_research_claim_text(note, comparison_like=True)
    normalized_candidate = _normalize_research_claim_text(
        candidate,
        comparison_like=True,
    )
    claim_text = ""
    if normalized_note and claims.research_excerpt_has_substantive_claim(normalized_note):
        claim_text = normalized_note
    elif normalized_note and not claims.research_claim_is_generic(normalized_note):
        claim_text = normalized_note
    else:
        claim_text = normalized_candidate or normalized_note
    if not claim_text:
        return {}
    providers = [
        item.strip()
        for item in str(row.get("provider_support") or "").split("+")
        if item.strip()
    ]
    entry: dict[str, Any] = {
        "claim": claim_text,
        "sources": [candidate] if candidate else ([source] if source else []),
        "urls": [url] if url else [],
        "providers": providers,
        "clusters": [cluster] if cluster else [],
        "domains": [query_routing._registered_domain(query_routing._result_hostname({"url": url}))] if url else [],
        "source_count": 1 if (candidate or source) else 0,
        "provider_count": len(providers),
        "cluster_count": 1 if cluster else 0,
        "comparison_subject_match_count": 1,
    }
    entry["support_level"] = claims.research_claim_support_level(
        source_count=int(entry.get("source_count") or 0),
        provider_count=int(entry.get("provider_count") or 0),
        cluster_count=int(entry.get("cluster_count") or 0),
    )
    entry["support_basis"] = claims.research_claim_support_basis(entry)
    return entry


def _research_claim_primary_cluster(
    *,
    entry: dict[str, Any],
    authoritative_preferred: bool,
) -> str:
    preferred_order = (
        ["official", "supporting", "general", "community"]
        if authoritative_preferred
        else ["project", "supporting", "curated", "listicle", "directory", "community"]
    )
    clusters = [str(item) for item in (entry.get("clusters") or []) if item]
    for label in preferred_order:
        if label in clusters:
            return label
    return clusters[0] if clusters else ""


def _research_claim_source_kind_rank(
    entry: dict[str, Any],
) -> int:
    sources = [str(item).strip() for item in (entry.get("sources") or []) if str(item).strip()]
    urls = [str(item).strip() for item in (entry.get("urls") or []) if str(item).strip()]
    title = sources[0] if sources else str(entry.get("claim") or "").strip()
    url = urls[0] if urls else ""
    return _research_official_candidate_kind_rank({"title": title, "url": url})


def _research_claim_support_phrase(
    claim_entry: dict[str, Any],
) -> str:
    if not claim_entry:
        return ""
    support_level = str(claim_entry.get("support_level") or "").strip()
    support_basis = str(claim_entry.get("support_basis") or "").strip()
    if not support_basis:
        support_basis = claims.research_claim_support_basis(claim_entry)
    source_count = int(claim_entry.get("source_count") or 0)
    provider_count = int(claim_entry.get("provider_count") or 0)
    cluster_count = int(claim_entry.get("cluster_count") or 0)
    phrase = support_level.replace("-", " ").strip() if support_level else ""
    detail_bits: list[str] = []
    if provider_count > 0:
        detail_bits.append(f"{provider_count} provider{'s' if provider_count != 1 else ''}")
    if source_count > 0:
        detail_bits.append(f"{source_count} source{'s' if source_count != 1 else ''}")
    if cluster_count > 1:
        detail_bits.append(f"{cluster_count} source clusters")
    if support_basis:
        if support_level == "single-source":
            if detail_bits:
                return f"{phrase} support anchored by {support_basis} ({', '.join(detail_bits)})"
            if phrase:
                return f"{phrase} support anchored by {support_basis}"
        if phrase:
            if detail_bits:
                return f"{phrase} support across {support_basis} ({', '.join(detail_bits)})"
            return f"{phrase} support across {support_basis}"
        if detail_bits:
            return f"supported across {support_basis} ({', '.join(detail_bits)})"
        return f"supported across {support_basis}"
    if phrase and detail_bits:
        return f"{phrase} support across {', '.join(detail_bits)}"
    if phrase:
        return f"{phrase} support"
    if detail_bits:
        return f"supported by {', '.join(detail_bits)}"
    return ""


def _research_claim_text(
    *,
    title: str,
    excerpt: str,
    comparison_like: bool,
) -> str:
    cleaned_title = _normalize_research_claim_text(
        re.split(r"\s(?:[-:|–—])\s", title, maxsplit=1)[0].strip() or title,
        comparison_like=comparison_like,
    )
    excerpt = re.sub(r"\s+", " ", excerpt).strip()
    if excerpt:
        sentence_ready_excerpt = re.sub(r"\bvs\.\s+", "vs ", excerpt, flags=re.IGNORECASE)
        raw_sentences = re.split(
            r"(?<=[.!?。！？])\s+",
            sentence_ready_excerpt,
        )
        cleaned_sentences = [
            cleaned
            for sentence in raw_sentences[:3]
            if (cleaned := _normalize_research_claim_text(
                sentence.strip(),
                comparison_like=comparison_like,
            ))
        ]
        cleaned_excerpt = cleaned_sentences[0] if cleaned_sentences else ""
        if comparison_like and len(cleaned_sentences) > 1:
            preferred_excerpt = ""
            fallback_excerpt = cleaned_excerpt
            for candidate in cleaned_sentences:
                if not claims.research_excerpt_has_substantive_claim(candidate):
                    continue
                if not claims.research_claim_is_generic(candidate):
                    preferred_excerpt = candidate
                    break
                if not fallback_excerpt or not claims.research_excerpt_has_substantive_claim(
                    fallback_excerpt
                ):
                    fallback_excerpt = candidate
            cleaned_excerpt = preferred_excerpt or fallback_excerpt
        if cleaned_excerpt:
            if cleaned_title:
                title_prefix = f"{cleaned_title} "
                if cleaned_excerpt.lower().startswith(title_prefix.lower()):
                    trailing_excerpt = cleaned_excerpt[len(title_prefix):].strip(" -:;,.")
                    if trailing_excerpt.lower().startswith(title_prefix.lower()):
                        trailing_excerpt = trailing_excerpt[len(title_prefix):].strip(" -:;,.")
                    if trailing_excerpt:
                        leading_word = trailing_excerpt.split()[0].lower()
                        if leading_word in {
                            "is",
                            "are",
                            "can",
                            "lets",
                            "allows",
                            "enables",
                            "supports",
                            "provides",
                            "helps",
                            "uses",
                        }:
                            trailing_excerpt = f"{cleaned_title} {trailing_excerpt}"
                    if len(trailing_excerpt.split()) >= 3:
                        return trailing_excerpt
            if cleaned_title and claims.research_excerpt_looks_like_navigation_noise(cleaned_excerpt):
                if comparison_like and claims.research_claim_is_generic(cleaned_title):
                    return ""
                return cleaned_title
            if cleaned_title and claims.research_excerpt_looks_like_link_index_noise(cleaned_excerpt):
                if comparison_like and claims.research_claim_is_generic(cleaned_title):
                    return ""
                return cleaned_title
            if _research_excerpt_looks_like_schema_noise(cleaned_excerpt):
                if cleaned_title and not claims.research_claim_is_generic(cleaned_title):
                    return cleaned_title
                return ""
            if not claims.research_excerpt_looks_like_noise(cleaned_excerpt):
                if comparison_like and claims.research_excerpt_has_substantive_claim(cleaned_excerpt):
                    return cleaned_excerpt
                excerpt_tokens = set(re.findall(r"[a-z0-9]+", cleaned_excerpt.lower()))
                title_tokens = set(re.findall(r"[a-z0-9]+", cleaned_title.lower()))
                if cleaned_title and title_tokens and excerpt_tokens and (
                    len(title_tokens & excerpt_tokens) >= max(2, min(len(title_tokens), 3))
                ):
                    if (
                        comparison_like
                        and claims.research_claim_is_generic(cleaned_title)
                        and claims.research_excerpt_has_substantive_claim(cleaned_excerpt)
                    ):
                        return cleaned_excerpt
                    return cleaned_title
                return cleaned_excerpt
    if cleaned_title and comparison_like and claims.research_claim_is_generic(cleaned_title):
        return ""
    return cleaned_title


def _research_cluster_base_weight(
    *,
    label: str,
    authoritative_preferred: bool,
) -> float:
    if authoritative_preferred:
        return {
            "official": 4.0,
            "supporting": 3.0,
            "general": 2.0,
            "community": 1.0,
        }.get(label, 1.0)
    return {
        "project": 4.0,
        "curated": 3.0,
        "listicle": 2.0,
        "directory": 1.5,
        "community": 1.0,
    }.get(label, 1.0)


def _research_cluster_tier(
    *,
    weight: float,
    label: str,
) -> str:
    if label in {"official", "project"} or weight >= 6.0:
        return "primary"
    if weight >= 3.5:
        return "secondary"
    return "supplemental"


def _research_comparison_claim_from_row(
    *,
    comparison_rows: Sequence[Mapping[str, Any]],
    entity_tokens: Sequence[str],
) -> dict[str, Any]:
    for row in comparison_rows:
        candidate = str(row.get("candidate") or "").strip()
        note = str(row.get("note") or "").strip()
        source = str(row.get("source") or "").strip()
        url = str(row.get("url") or "").strip()
        cluster = str(row.get("cluster") or "").strip()
        claim_text = _normalize_research_claim_text(
            note or candidate,
            comparison_like=True,
        )
        if not claim_text:
            claim_text = _normalize_research_claim_text(
                candidate,
                comparison_like=True,
            )
        if not claim_text:
            continue
        if claims.research_claim_comparison_subject_match_count(
            claim=claim_text,
            sources=[candidate, source],
            entities=[entity_tokens],
        ) <= 0:
            continue
        providers = [
            item.strip()
            for item in str(row.get("provider_support") or "").split("+")
            if item.strip()
        ]
        entry: dict[str, Any] = {
            "claim": claim_text,
            "sources": [candidate] if candidate else ([source] if source else []),
            "urls": [url] if url else [],
            "providers": providers,
            "clusters": [cluster] if cluster else [],
            "domains": [query_routing._registered_domain(query_routing._result_hostname({"url": url}))] if url else [],
            "source_count": 1 if (candidate or source) else 0,
            "provider_count": len(providers),
            "cluster_count": 1 if cluster else 0,
            "comparison_subject_match_count": 1,
        }
        entry["support_level"] = claims.research_claim_support_level(
            source_count=int(entry.get("source_count") or 0),
            provider_count=int(entry.get("provider_count") or 0),
            cluster_count=int(entry.get("cluster_count") or 0),
        )
        entry["support_basis"] = claims.research_claim_support_basis(entry)
        return entry
    return {}


def _research_comparison_entities(
    query: str,
) -> list[tuple[str, ...]]:
    subjects = _research_parse_comparison_subjects(query)
    if not subjects:
        return []
    brand_prefix = ""
    first_words = subjects[0].split()
    if first_words:
        candidate = first_words[0].strip(" ,.;:")
        if candidate and candidate[0].isalpha() and candidate[0].isupper():
            brand_prefix = candidate
    generic_tokens = {
        "advice",
        "api",
        "community",
        "docs",
        "documentation",
        "guidance",
        "guide",
        "guides",
        "migration",
        "migrations",
        "official",
        "resource",
        "resources",
        "reference",
    }
    ambiguous_product_tokens = comparison.research_ambiguous_product_tokens()
    entities: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for part in subjects[:4]:
        if _research_subject_is_generic_comparison_dimension(part):
            continue
        candidate = part
        if _research_subject_should_inherit_brand_prefix(
            subject=candidate,
            brand_prefix=brand_prefix,
            generic_tokens=generic_tokens,
        ):
            candidate = f"{brand_prefix} {candidate}"
        tokens_list = [
            token
            for token in query_routing._query_precision_tokens(candidate)
            if token not in generic_tokens
        ]
        if brand_prefix and brand_prefix.lower() in candidate.lower():
            ambiguous_tokens = [
                token for token in tokens_list if token in ambiguous_product_tokens
            ]
            if ambiguous_tokens:
                tokens_list = [brand_prefix.lower(), *ambiguous_tokens]
        tokens = tuple(dict.fromkeys(tokens_list))
        if not tokens or tokens in seen:
            continue
        seen.add(tokens)
        entities.append(tokens)
    return entities


def _research_comparison_subject_phrase(
    query: str,
) -> str:
    subjects = _research_parse_comparison_subjects(query)
    if len(subjects) < 2:
        return ""
    trimmed = [item.strip(" ,.;:") for item in subjects[:2] if item.strip(" ,.;:")]
    if len(trimmed) < 2:
        return ""
    return f"{trimmed[0]} vs {trimmed[1]}"


def _research_excerpt_looks_like_schema_noise(
    text: str,
) -> bool:
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    if not normalized:
        return False
    if claims.research_excerpt_looks_like_json_shell(normalized):
        return True
    schema_markers = (
        "keys are strings",
        "maximum length",
        "minimum length",
        "defaults to",
        "id: string",
        "completion_window: string",
        "metadata: object",
        "must be one of",
        "object containing",
        "array of",
        "the id of the",
        "the type of the",
        "a list of",
    )
    return any(marker in normalized for marker in schema_markers)


def _research_is_canonical_vendor_doc(
    url: str,
) -> bool:
    normalized = url.strip()
    if not normalized:
        return False
    parsed = urlparse(normalized)
    normalized = parsed._replace(fragment="", query="").geturl().rstrip("/")
    for items in _research_canonical_doc_catalog().values():
        for item in items:
            candidate_url = str(item.get("url") or "").strip()
            if not candidate_url:
                continue
            candidate_parsed = urlparse(candidate_url)
            candidate_normalized = candidate_parsed._replace(
                fragment="",
                query="",
            ).geturl().rstrip("/")
            if candidate_normalized == normalized:
                return True
    return False


def _research_item_matches_brand_token(
    *,
    item: dict[str, Any],
    brand_token: str,
) -> bool:
    text = " ".join(
        [
            query_routing._result_hostname(item),
            str(item.get("url") or ""),
            str(item.get("title") or ""),
            str(item.get("snippet") or ""),
        ]
    ).lower()
    return brand_token in text


def _research_non_authoritative_query_tokens(
    query: str,
) -> list[str]:
    generic_tokens = {
        "agent",
        "agentic",
        "agents",
        "ai",
        "analysis",
        "approach",
        "best",
        "build",
        "code",
        "compare",
        "comparison",
        "docs",
        "documentation",
        "guide",
        "guides",
        "latest",
        "mcp",
        "official",
        "retrieval",
        "search",
        "server",
        "servers",
        "tool",
        "tools",
        "web",
        "workflow",
        "workflows",
    }
    return [
        token
        for token in query_routing._query_brand_tokens(query)
        if token not in generic_tokens
    ]


def _research_official_candidate_kind_rank(
    item: dict[str, Any],
) -> int:
    url = str(item.get("url") or "")
    title_text = str(item.get("title") or "").lower()
    path = urlparse(url).path.lower()
    if "migrate" in path or "migration" in title_text:
        return 0
    if any(marker in path for marker in ("/guide/", "/guides/", "/docs/guides/")) or "guide" in title_text:
        return 1
    if "/overview" in path or "overview" in title_text:
        return 2
    if (
        "/methods/" in path
        or title_text.startswith(("create ", "list ", "retrieve ", "delete ", "update "))
        or " method " in f" {title_text} "
    ):
        return 5
    if (
        "/api-reference/" in path
        or "/api/reference/" in path
        or "api reference" in title_text
        or "reference" in title_text
    ):
        return 3
    return 4


def _research_parse_comparison_subjects(
    query: str,
) -> list[str]:
    normalized = re.sub(r"\b20\d{2}\b", "", query).strip()
    query_lower = normalized.lower()
    if not query_routing._looks_like_comparison_query(query_lower):
        return []
    match = re.search(
        r"^\s*compare\s+(.+?)(?:\s+for\s+(.+))?$",
        normalized,
        re.IGNORECASE,
    )
    if not match:
        return []
    subjects = match.group(1).strip()
    return [
        item.strip(" ,.;:")
        for item in re.split(r"\s+(?:and|vs\.?|versus)\s+", subjects, flags=re.IGNORECASE)
        if item.strip(" ,.;:")
    ]


def _research_primary_vendor_brand(
    query: str,
) -> str:
    subjects = _research_parse_comparison_subjects(query)
    if len(subjects) < 2:
        return ""
    first_words = subjects[0].split()
    if not first_words:
        return ""
    brand_prefix = first_words[0].strip(" ,.;:")
    if not brand_prefix or not brand_prefix[0].isalpha() or not brand_prefix[0].isupper():
        return ""
    generic_tokens = {
        "api",
        "docs",
        "documentation",
        "guide",
        "guides",
        "official",
        "resource",
        "resources",
        "reference",
    }
    inherited_subject_found = False
    for subject in subjects[1:]:
        if brand_prefix.lower() in subject.lower():
            continue
        if _research_subject_should_inherit_brand_prefix(
            subject=subject,
            brand_prefix=brand_prefix,
            generic_tokens=generic_tokens,
        ):
            inherited_subject_found = True
            continue
        return ""
    return brand_prefix.lower() if inherited_subject_found else ""


def _research_project_candidate_kind_rank(
    item: dict[str, Any],
) -> int:
    url = str(item.get("url") or "")
    title_text = str(item.get("title") or "").lower()
    path = urlparse(url).path.lower()
    if any(marker in path for marker in ("/alternatives/", "/compare", "/comparison")):
        return 0
    if any(marker in f" {title_text} " for marker in (" vs ", " versus ", " comparison ", " compare ")):
        return 1
    if "/blog/" in path:
        return 3
    return 2


def _research_report_anchor_tokens(
    *,
    query: str,
    mode: str,
    ordered_results: list[dict[str, Any]],
    authoritative_preferred: bool,
) -> list[str]:
    ignored_tokens = {
        "ai",
        "api",
        "app",
        "com",
        "dev",
        "developers",
        "docs",
        "guide",
        "guides",
        "io",
        "net",
        "org",
        "platform",
        "reference",
        "www",
    }
    tokens: list[str] = []
    seen: set[str] = set()
    for item in ordered_results:
        cluster_label = _research_result_cluster_label(
            query=query,
            mode=mode,
            item=item,
            include_domains=None,
            authoritative_preferred=authoritative_preferred,
        )
        if cluster_label not in {"official", "supporting"}:
            continue
        registered_domain = query_routing._registered_domain(query_routing._result_hostname(item))
        if not registered_domain:
            continue
        for token in re.split(r"[^a-z0-9]+", registered_domain.lower()):
            if not token or token in ignored_tokens or token in seen:
                continue
            seen.add(token)
            tokens.append(token)
            if len(tokens) >= 6:
                return tokens
    return tokens


def _research_report_source_domains(
    source_lines: Sequence[str],
) -> list[str]:
    domains: list[str] = []
    seen: set[str] = set()
    for line in source_lines:
        text = str(line).strip()
        if not text:
            continue
        match = re.search(r"\(([^()]+)\)\s*$", text)
        if not match:
            continue
        domain = query_routing._registered_domain(match.group(1).strip())
        if not domain or domain in seen:
            continue
        seen.add(domain)
        domains.append(domain)
    return domains


def _research_result_cluster_label(
    *,
    query: str,
    mode: SearchMode,
    item: dict[str, Any],
    include_domains: list[str] | None,
    authoritative_preferred: bool,
) -> str:
    normalized = postprocess._canonicalize_result_item(item)
    hostname = query_routing._result_hostname(normalized)
    registered_domain = query_routing._registered_domain(hostname)
    path = urlparse(normalized.get("url", "")).path.lower()
    title_text = (normalized.get("title") or "").lower()
    snippet_text = (
        normalized.get("snippet")
        or normalized.get("content")
        or ""
    ).lower()
    community_domains = {
        "facebook.com",
        "linkedin.com",
        "news.ycombinator.com",
        "quora.com",
        "reddit.com",
        "twitter.com",
        "x.com",
        "youtube.com",
        "youtu.be",
    }
    directory_domains = {
        "capterra.com",
        "g2.com",
        "mcp-ai.org",
        "mcp.so",
        "mcpmarket.com",
        "mcpnow.io",
        "mcpserverfinder.com",
        "mcpservers.org",
        "pulsemcp.com",
        "sourceforge.net",
        "toolhunter.cc",
    }
    if authoritative_preferred:
        effective_mode: SearchMode = mode if mode in {"docs", "github", "pdf"} else "docs"
        query_tokens = _research_authoritative_query_tokens(query)
        primary_vendor_brand = _research_primary_vendor_brand(query)
        comparison_entities = _research_comparison_entities(query)
        flags = ranking._resource_result_flags(
            mode=effective_mode,
            item=normalized,
            query_tokens=query_tokens,
            include_domains=include_domains,
        )
        snippet_text = (normalized.get("snippet") or "").lower()
        brand_domain_match = (
            bool(flags["include_match"])
            or bool(flags["registered_domain_label_match"])
            or bool(flags["host_brand_match"])
            or (
                bool(flags["docs_shape_match"])
                and bool(flags["title_brand_match"])
            )
        )
        primary_vendor_match = (
            not primary_vendor_brand
            or _research_item_matches_brand_token(
                item=normalized,
                brand_token=primary_vendor_brand,
            )
        )
        comparison_entity_match = (
            not comparison_entities
            or any(
                comparison.research_result_matches_entity(
                    item=normalized,
                    entity_tokens=entity_tokens,
                )
                for entity_tokens in comparison_entities
            )
        )
        authoritative_target = query_routing._looks_like_authoritative_research_target(
            url=normalized.get("url", ""),
            hostname=hostname,
            title_text=title_text,
            mode=effective_mode,
        )
        if query_tokens:
            official_candidate = brand_domain_match and _result_matches_official_policy(
                item=normalized,
                mode=effective_mode,
                query_tokens=query_tokens,
                include_domains=include_domains,
                strict_official=False,
            )
        else:
            host_authoritative = query_routing._looks_like_authoritative_research_host(
                hostname=hostname,
                path=path,
            )
            official_candidate = bool(flags["non_third_party"]) and authoritative_target and not query_routing._looks_like_research_marketing_or_blog_result(
                hostname=hostname,
                path=path,
                title_text=title_text,
                snippet_text=snippet_text,
            ) and host_authoritative
        community_candidate = (
            not bool(flags["non_third_party"])
            or query_routing._is_obvious_official_community_result(hostname=hostname, path=path)
        )
        if primary_vendor_brand and not primary_vendor_match:
            official_candidate = False
        if comparison_entities and not comparison_entity_match:
            official_candidate = False
        supportive_candidate = bool(flags["non_third_party"]) and (
            query_routing._looks_like_supporting_research_target(
                url=normalized.get("url", ""),
                hostname=hostname,
                title_text=title_text,
                snippet_text=snippet_text,
                mode=effective_mode,
            )
            or (
                bool(query_tokens)
                and (
                    bool(flags["include_match"])
                    or bool(flags["registered_domain_label_match"])
                    or bool(flags["host_brand_match"])
                    or (
                        bool(flags["docs_shape_match"])
                        and bool(flags["title_brand_match"])
                    )
                )
            )
            or (
                not bool(query_tokens)
                and query_routing._looks_like_supporting_research_target(
                    url=normalized.get("url", ""),
                    hostname=hostname,
                    title_text=title_text,
                    snippet_text=snippet_text,
                    mode=effective_mode,
                )
                and query_routing._looks_like_authoritative_research_host(
                    hostname=hostname,
                    path=path,
                )
            )
        )
        if primary_vendor_brand and not primary_vendor_match:
            supportive_candidate = False
        if comparison_entities and not comparison_entity_match:
            supportive_candidate = False
        if community_candidate:
            return "community"
        if official_candidate and authoritative_target:
            return "official"
        if official_candidate or supportive_candidate:
            return "supporting"
        return "general"

    listicle_candidate = (
        any(marker in title_text for marker in ("best ", "top ", "roundup", "ranking"))
        or any(marker in path for marker in ("/best-", "/top-", "/list-", "/lists/"))
        or (
            any(marker in snippet_text for marker in ("top ", "best ", "ranked ", "roundup"))
            and "/blog/" in path
        )
    )
    if registered_domain in community_domains:
        return "community"
    if registered_domain == "github.com":
        return "project"
    comparison_like = query_routing._looks_like_comparison_query(query.lower())
    query_tokens = _research_non_authoritative_query_tokens(query)
    flags = ranking._resource_result_flags(
        mode=mode,
        item=normalized,
        query_tokens=query_tokens,
        include_domains=include_domains,
    )
    project_brand_match = (
        bool(flags["include_match"])
        or bool(flags["registered_domain_label_match"])
        or bool(flags["host_brand_match"])
    )
    comparison_marker = (
        " vs " in f" {title_text} "
        or " versus " in f" {title_text} "
        or " compare " in f" {title_text} "
        or " comparison " in f" {title_text} "
        or any(
            marker in path
            for marker in (
                "-vs-",
                "/compare",
                "/comparison",
                "/comparisons/",
                "/versus/",
            )
        )
    )
    branded_marketing_candidate = comparison_like and project_brand_match and any(
        marker in title_text or marker in path
        for marker in (
            "alternatives",
            "pricing",
            "best ",
            "top ",
            "/pricing",
            "/alternatives/",
        )
    )
    supporting_candidate = (
        comparison_like
        and bool(flags["non_third_party"])
        and project_brand_match
        and bool(flags["docs_shape_match"])
        and not comparison_marker
        and not branded_marketing_candidate
        and not query_routing._looks_like_research_marketing_or_blog_result(
            hostname=hostname,
            path=path,
            title_text=title_text,
            snippet_text=snippet_text,
        )
    )
    project_candidate = bool(flags["non_third_party"]) and project_brand_match and (
        not comparison_like
        or comparison_marker
    )
    if project_candidate:
        return "project"
    if supporting_candidate:
        return "supporting"
    if branded_marketing_candidate:
        return "listicle"
    if registered_domain in directory_domains or (
        "mcp" in hostname and any(marker in path for marker in ("/server/", "/servers/"))
    ):
        return "directory"
    if listicle_candidate:
        return "listicle"
    return "curated"


def _research_select_comparison_focus_rows(
    *,
    comparison_rows: Sequence[Mapping[str, Any]],
    comparison_entities: Sequence[Sequence[str]],
    selected_urls: Sequence[str] | None = None,
) -> list[dict[str, str]]:
    if not comparison_rows:
        return []

    focus_rows: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    selected_url_set = {
        str(url).strip()
        for url in (selected_urls or [])
        if str(url).strip()
    }

    def append_row(row: Mapping[str, Any]) -> None:
        if len(focus_rows) >= 4:
            return
        url = str(row.get("url") or "").strip()
        if url and url in seen_urls:
            return
        focus_rows.append(dict(row))
        if url:
            seen_urls.add(url)

    if not comparison_entities and selected_url_set:
        for row in comparison_rows:
            if str(row.get("url") or "").strip() in selected_url_set:
                append_row(row)
        if focus_rows:
            return focus_rows[:4]

    for entity_tokens in comparison_entities[:4]:
        matching_row = next(
            (
                row
                for row in comparison_rows
                if claims.research_claim_comparison_subject_match_count(
                    claim=" ".join(
                        part
                        for part in (
                            str(row.get("candidate") or "").strip(),
                            str(row.get("note") or "").strip(),
                        )
                        if part
                    ),
                    sources=[
                        str(row.get("candidate") or "").strip(),
                        str(row.get("source") or "").strip(),
                    ],
                    entities=[entity_tokens],
                )
                > 0
            ),
            None,
        )
        if matching_row:
            append_row(matching_row)

    required_subject_rows = min(2, len(comparison_entities[:4]))
    if required_subject_rows and len(focus_rows) >= required_subject_rows:
        return focus_rows[:4]

    for row in comparison_rows:
        append_row(row)
        if len(focus_rows) >= 4:
            break
    return focus_rows[:4]


def _research_source_topic_key(
    *,
    title: str,
    domain: str,
) -> str:
    head = re.split(r"\s[\-|:|]\s", title, maxsplit=1)[0].strip().lower() or title.lower()
    tokens = re.findall(r"[a-z0-9]+", head)
    stop_tokens = {
        "a",
        "an",
        "and",
        "api",
        "app",
        "apps",
        "developer",
        "developers",
        "doc",
        "docs",
        "documentation",
        "for",
        "guide",
        "guides",
        "in",
        "of",
        "on",
        "openai",
        "platform",
        "reference",
        "the",
        "to",
    }
    action_tokens = {
        "create",
        "delete",
        "get",
        "list",
        "migrate",
        "retrieve",
        "update",
        "use",
        "using",
    }
    normalized_tokens: list[str] = []
    for token in tokens:
        if token in stop_tokens or token in action_tokens:
            continue
        if token.endswith("ies") and len(token) > 4:
            token = f"{token[:-3]}y"
        elif token.endswith(("ches", "shes", "sses", "xes", "zes")) and len(token) > 5:
            token = token[:-2]
        elif token.endswith("s") and len(token) > 4:
            token = token[:-1]
        normalized_tokens.append(token)
    if not normalized_tokens:
        normalized_tokens = tokens[:2] or [head]
    return f"{domain}|{' '.join(normalized_tokens[:3])}"


def _research_subject_is_generic_comparison_dimension(
    subject: str,
) -> bool:
    tokens = query_routing._query_precision_tokens(subject)
    if not tokens:
        return True
    generic_dimension_tokens = {
        "advice",
        "approach",
        "approaches",
        "best",
        "community",
        "compare",
        "comparison",
        "docs",
        "documentation",
        "guidance",
        "guide",
        "guides",
        "migration",
        "migrations",
        "official",
        "opinion",
        "opinions",
        "strategy",
        "strategies",
        "tutorial",
        "tutorials",
        "usage",
        "workflow",
        "workflows",
    }
    return all(token in generic_dimension_tokens for token in tokens)


def _research_subject_should_inherit_brand_prefix(
    *,
    subject: str,
    brand_prefix: str,
    generic_tokens: set[str],
) -> bool:
    if not brand_prefix or brand_prefix.lower() in subject.lower():
        return False
    meaningful_tokens = [
        token
        for token in query_routing._query_precision_tokens(subject)
        if token not in generic_tokens
    ]
    if not meaningful_tokens:
        return True
    ambiguous_product_tokens = comparison.research_ambiguous_product_tokens()
    return all(token in ambiguous_product_tokens for token in meaningful_tokens)


def _research_summary_mentions_anchor_tokens(
    text: str,
    anchor_tokens: list[str],
) -> bool:
    normalized = f" {re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()} "
    return any(f" {token} " in normalized for token in anchor_tokens if token)


def _result_matches_official_policy(
    *,
    item: dict[str, Any],
    mode: SearchMode,
    query_tokens: list[str],
    include_domains: list[str] | None,
    strict_official: bool,
) -> bool:
    flags = ranking._resource_result_flags(
        mode=mode,
        item=item,
        query_tokens=query_tokens,
        include_domains=include_domains,
    )
    return query_routing._is_probably_official_resource_result(
        mode=mode,
        hostname=str(flags["hostname"]),
        include_match=bool(flags["include_match"]),
        registered_domain_label_match=bool(flags["registered_domain_label_match"]),
        host_brand_match=bool(flags["host_brand_match"]),
        title_brand_match=bool(flags["title_brand_match"]),
        docs_shape_match=bool(flags["docs_shape_match"]),
        non_third_party=bool(flags["non_third_party"]),
        official_query=strict_official,
    )


def _select_research_claim_excerpt(
    *,
    page_excerpt: str,
    snippet: str,
    content: str,
) -> str:
    for candidate in (page_excerpt, snippet, content):
        cleaned = re.sub(r"\s+", " ", candidate).strip()
        if not cleaned:
            continue
        if claims.research_excerpt_looks_like_navigation_noise(cleaned):
            continue
        if claims.research_excerpt_looks_like_link_index_noise(cleaned):
            continue
        if claims.research_excerpt_looks_like_noise(cleaned):
            continue
        return cleaned
    return ""


def _select_research_primary_claim(
    claim_evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    for item in claim_evidence:
        claim = str(item.get("claim") or "").strip()
        if not claim:
            continue
        if str(item.get("support_level") or "") == "single-source":
            continue
        if claims.research_claim_is_generic(claim):
            continue
        if not claims.research_excerpt_has_substantive_claim(claim):
            continue
        return item
    for item in claim_evidence:
        claim = str(item.get("claim") or "").strip()
        if claim and not claims.research_claim_is_generic(claim):
            return item
    return claim_evidence[0] if claim_evidence else {}


def _trim_research_claims_for_visibility(
    *,
    claims: list[dict[str, Any]],
    authoritative_preferred: bool,
    comparison_like: bool,
    limit: int,
) -> list[dict[str, Any]]:
    if not claims:
        return []

    if authoritative_preferred:
        primary_clusters = [
            _research_claim_primary_cluster(
                entry=entry,
                authoritative_preferred=True,
            )
            for entry in claims
        ]
        official_count = sum(1 for label in primary_clusters if label == "official")
        supporting_count = sum(1 for label in primary_clusters if label == "supporting")
        anchor_count = official_count + supporting_count
        if official_count >= 2 and supporting_count >= 1:
            cluster_caps = {"official": limit, "supporting": 1}
        elif anchor_count >= 3:
            cluster_caps = {"official": limit, "supporting": limit}
        elif anchor_count >= 2:
            cluster_caps = {"official": limit, "supporting": limit, "general": 1}
        else:
            cluster_caps = {
                "official": limit,
                "supporting": limit,
                "general": 2,
                "community": 1,
            }
    elif comparison_like:
        primary_clusters = [
            _research_claim_primary_cluster(
                entry=entry,
                authoritative_preferred=False,
            )
            for entry in claims
        ]
        anchor_count = sum(
            1 for label in primary_clusters if label in {"project", "supporting"}
        )
        if anchor_count >= 3:
            cluster_caps = {"project": limit, "supporting": limit}
        elif anchor_count >= 2:
            cluster_caps = {"project": limit, "supporting": limit, "curated": 1}
        else:
            cluster_caps = {
                "project": limit,
                "supporting": limit,
                "curated": 2,
                "listicle": 1,
                "directory": 1,
                "community": 1,
            }
    else:
        return claims[:limit]

    selected: list[dict[str, Any]] = []
    cluster_counts: dict[str, int] = {}
    for entry in claims:
        cluster_label = _research_claim_primary_cluster(
            entry=entry,
            authoritative_preferred=authoritative_preferred,
        )
        cap = cluster_caps.get(cluster_label, 0)
        if not cap or cluster_counts.get(cluster_label, 0) >= cap:
            continue
        selected.append(entry)
        cluster_counts[cluster_label] = cluster_counts.get(cluster_label, 0) + 1
        if len(selected) >= limit:
            break
    return selected[:limit]

