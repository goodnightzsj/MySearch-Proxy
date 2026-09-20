"""研究候选筛选与奖励页过滤：从原始结果池到可排序候选集。

从 `mysearch/clients.py` 抽出的**候选装配层**。三簇函数：

- `_select_research_candidate_results` 及其子步骤：把 provider 结果池拆成
  authoritative / supporting / project / community / curated / listicle /
  directory 等候选簇，去重、按域名多样化、裁剪到上限。
- `_rerank_*` / `_general_result_rank`：候选的重排键。
- `_filter_strong_award_results`：颁奖季查询的强结果过滤。

不做 provider 调用、不读 config、不碰实例状态。依赖方向单向：本模块依赖
`query_routing`（谓词与常量）、`postprocess`、`ranking` 以及同包的
`claims` / `comparison` / `sections`；`clients` 在上层依赖本模块。

`MySearchClient` 保留同名方法作为一行委托，调用面不变。
"""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlparse

from mysearch import postprocess
from mysearch import query_routing
from mysearch import ranking
from mysearch.research import claims
from mysearch.research import comparison
from mysearch.research import sections
from mysearch.types import ResolvedSearchIntent, SearchMode


def _research_prefers_canonical_vendor_docs(
    query: str,
) -> bool:
        query_lower = query.lower()
        if not any(
            marker in query_lower
            for marker in (
                "official docs",
                "docs retrieval",
                "documentation retrieval",
                "agentic search",
                "web retrieval",
            )
        ):
            return False
        return not bool(sections._research_authoritative_query_tokens(query))


def _dedupe_research_results_for_report(
    *result_lists: list[dict[str, Any]],
) -> list[dict[str, Any]]:
        ordered_keys: list[str] = []
        seen: set[str] = set()
        variants_by_key: dict[str, list[dict[str, Any]]] = {}
        providers_by_key: dict[str, set[str]] = {}
        for results in result_lists:
            for item in results:
                if not isinstance(item, dict):
                    continue
                normalized = postprocess._canonicalize_result_item(item)
                dedupe_key = postprocess._result_dedupe_key(normalized)
                if not dedupe_key:
                    continue
                if dedupe_key not in seen:
                    seen.add(dedupe_key)
                    ordered_keys.append(dedupe_key)
                variants_by_key.setdefault(dedupe_key, []).append(normalized)
                providers = {
                    provider
                    for provider in (
                        normalized.get("matched_providers")
                        or [normalized.get("provider", "")]
                    )
                    if provider
                }
                providers_by_key.setdefault(dedupe_key, set()).update(providers)
        deduped: list[dict[str, Any]] = []
        for dedupe_key in ordered_keys:
            variants = variants_by_key.get(dedupe_key) or []
            if not variants:
                continue
            best = max(variants, key=postprocess._result_quality_score)
            merged = postprocess._canonicalize_result_item(dict(best))
            canonical_variant = next(
                (
                    variant for variant in variants
                    if str(variant.get("provider") or "") == "canonical_research_docs"
                ),
                None,
            )
            if canonical_variant:
                canonical_snippet = str(canonical_variant.get("snippet") or "").strip()
                merged_snippet = str(merged.get("snippet") or "").strip()
                if canonical_snippet and (
                    not merged_snippet
                    or len(merged_snippet.split()) < 8
                    or not claims.research_excerpt_has_substantive_claim(merged_snippet)
                ):
                    merged["snippet"] = canonical_snippet
            matched_providers = sorted(
                provider for provider in providers_by_key.get(dedupe_key, set()) if provider
            )
            if matched_providers:
                merged["matched_providers"] = matched_providers
            deduped.append(merged)
        return deduped


def _select_research_candidate_results(
    *,
    query: str,
    mode: SearchMode,
    intent: ResolvedSearchIntent,
    max_results: int,
    web_results: list[dict[str, Any]],
    docs_rescue_results: list[dict[str, Any]],
    tavily_support_results: list[dict[str, Any]],
    exa_results: list[dict[str, Any]],
    include_domains: list[str] | None,
    authoritative_preferred: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        combined = _dedupe_research_results_for_report(
            docs_rescue_results if authoritative_preferred else [],
            web_results,
            tavily_support_results,
            exa_results,
            [] if authoritative_preferred else docs_rescue_results,
        )
        if not combined:
            return [], {
                "authoritative_source_count": 0,
                "supporting_source_count": 0,
                "community_source_count": 0,
                "selected_candidate_domains": [],
                "selected_candidate_cluster_counts": {},
            }

        if not authoritative_preferred:
            project_candidates: list[dict[str, Any]] = []
            supporting_candidates: list[dict[str, Any]] = []
            curated_candidates: list[dict[str, Any]] = []
            listicle_candidates: list[dict[str, Any]] = []
            directory_candidates: list[dict[str, Any]] = []
            community_candidates: list[dict[str, Any]] = []
            for item in combined:
                cluster_label = sections._research_result_cluster_label(
                    query=query,
                    mode=mode,
                    item=item,
                    include_domains=include_domains,
                    authoritative_preferred=False,
                )
                if cluster_label == "community":
                    community_candidates.append(item)
                elif cluster_label == "project":
                    project_candidates.append(item)
                elif cluster_label == "supporting":
                    supporting_candidates.append(item)
                elif cluster_label == "directory":
                    directory_candidates.append(item)
                elif cluster_label == "listicle":
                    listicle_candidates.append(item)
                else:
                    curated_candidates.append(item)
            if len(project_candidates) > 1:
                project_candidates = _rerank_general_results(
                    query=query,
                    result_profile="web",
                    results=project_candidates,
                    include_domains=include_domains,
                )
            if project_candidates:
                indexed_project_candidates = list(enumerate(project_candidates))
                project_candidates = [
                    item
                    for _, item in sorted(
                        indexed_project_candidates,
                        key=lambda pair: (
                            sections._research_project_candidate_kind_rank(pair[1]),
                            pair[0],
                        ),
                    )
                ]
            if len(curated_candidates) > 1:
                curated_candidates = _rerank_general_results(
                    query=query,
                    result_profile="web",
                    results=curated_candidates,
                    include_domains=include_domains,
                )
            if len(supporting_candidates) > 1:
                supporting_candidates = _rerank_resource_results(
                    query=query,
                    mode="docs",
                    results=supporting_candidates,
                    include_domains=include_domains,
                )
                supporting_candidates = _diversify_research_supporting_candidates(
                    query=query,
                    candidates=supporting_candidates,
                )
            if supporting_candidates:
                indexed_supporting_candidates = list(enumerate(supporting_candidates))
                supporting_candidates = [
                    item
                    for _, item in sorted(
                        indexed_supporting_candidates,
                        key=lambda pair: (
                            _research_supporting_candidate_kind_rank(
                                pair[1],
                                query=query,
                            ),
                            pair[0],
                        ),
                    )
                ]
                supporting_candidates = _diversify_results_by_registered_domain(
                    supporting_candidates
                )
            if len(community_candidates) > 1:
                community_candidates = _rerank_general_results(
                    query=query,
                    result_profile="web",
                    results=community_candidates,
                    include_domains=include_domains,
                )
            if len(directory_candidates) > 1:
                directory_candidates = _rerank_general_results(
                    query=query,
                    result_profile="web",
                    results=directory_candidates,
                    include_domains=include_domains,
                )
            if len(listicle_candidates) > 1:
                listicle_candidates = _rerank_general_results(
                    query=query,
                    result_profile="web",
                    results=listicle_candidates,
                    include_domains=include_domains,
                )
            selected = _assemble_non_authoritative_research_candidates(
                query=query,
                project_candidates=project_candidates,
                supporting_candidates=supporting_candidates,
                curated_candidates=curated_candidates,
                listicle_candidates=listicle_candidates,
                directory_candidates=directory_candidates,
                community_candidates=community_candidates,
                max_results=max_results,
            )
            selected_domains = _collect_source_domains(results=selected, citations=[])
            cluster_counts: dict[str, int] = {}
            for item in selected:
                cluster_label = sections._research_result_cluster_label(
                    query=query,
                    mode=mode,
                    item=item,
                    include_domains=include_domains,
                    authoritative_preferred=False,
                )
                cluster_counts[cluster_label] = cluster_counts.get(cluster_label, 0) + 1
            return selected, {
                "authoritative_source_count": 0,
                "supporting_source_count": int(cluster_counts.get("supporting") or 0),
                "community_source_count": sum(
                    1
                    for item in selected
                    if sections._research_result_cluster_label(
                        query=query,
                        mode=mode,
                        item=item,
                        include_domains=include_domains,
                        authoritative_preferred=False,
                    )
                    == "community"
                ),
                "selected_candidate_domains": selected_domains[:5],
                "selected_candidate_cluster_counts": cluster_counts,
            }

        effective_mode: SearchMode = mode if mode in {"docs", "github", "pdf"} else "docs"
        official_candidates: list[dict[str, Any]] = []
        supporting_candidates: list[dict[str, Any]] = []
        general_candidates: list[dict[str, Any]] = []
        community_candidates: list[dict[str, Any]] = []

        for item in combined:
            normalized = postprocess._canonicalize_result_item(item)
            cluster_label = sections._research_result_cluster_label(
                query=query,
                mode=effective_mode,
                item=normalized,
                include_domains=include_domains,
                authoritative_preferred=True,
            )
            if cluster_label == "official":
                official_candidates.append(normalized)
            elif cluster_label == "supporting":
                supporting_candidates.append(normalized)
            elif cluster_label == "community":
                community_candidates.append(normalized)
            else:
                general_candidates.append(normalized)

        if len(official_candidates) > 1:
            official_candidates = _rerank_resource_results(
                query=query,
                mode=effective_mode,
                results=official_candidates,
                include_domains=include_domains,
            )
            official_candidates = _diversify_research_official_candidates(
                query=query,
                candidates=official_candidates,
            )
            if _research_prefers_canonical_vendor_docs(query):
                official_candidates = _diversify_results_by_registered_domain(
                    official_candidates
                )
        if len(supporting_candidates) > 1:
            supporting_candidates = _rerank_resource_results(
                query=query,
                mode=effective_mode,
                results=supporting_candidates,
                include_domains=include_domains,
            )
            if _research_prefers_canonical_vendor_docs(query):
                supporting_candidates = _diversify_results_by_registered_domain(
                    supporting_candidates
                )
        if len(general_candidates) > 1:
            general_candidates = _rerank_general_results(
                query=query,
                result_profile="web",
                results=general_candidates,
                include_domains=include_domains,
            )
        if len(community_candidates) > 1:
            community_candidates = _rerank_general_results(
                query=query,
                result_profile="web",
                results=community_candidates,
                include_domains=include_domains,
            )

        ordered = _assemble_authoritative_research_candidates(
            official_candidates=official_candidates,
            supporting_candidates=supporting_candidates,
            general_candidates=general_candidates,
            community_candidates=community_candidates,
            max_results=max_results,
            prefer_canonical_vendor_docs=_research_prefers_canonical_vendor_docs(
                query
            ),
        )
        selected_domains = _collect_source_domains(results=ordered, citations=[])
        cluster_counts: dict[str, int] = {}
        for item in ordered:
            cluster_label = sections._research_result_cluster_label(
                query=query,
                mode=effective_mode,
                item=item,
                include_domains=include_domains,
                authoritative_preferred=True,
            )
            cluster_counts[cluster_label] = cluster_counts.get(cluster_label, 0) + 1
        return ordered, {
            "authoritative_source_count": int(cluster_counts.get("official") or 0),
            "supporting_source_count": int(cluster_counts.get("supporting") or 0),
            "community_source_count": int(cluster_counts.get("community") or 0),
            "selected_candidate_domains": selected_domains[:5],
            "selected_candidate_cluster_counts": cluster_counts,
        }


def _diversify_research_official_candidates(
    *,
    query: str,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
        entities = sections._research_comparison_entities(query)
        if len(entities) < 2 or len(candidates) < 2:
            return candidates
        indexed_remaining = list(enumerate(candidates))
        selected: list[dict[str, Any]] = []
        for entity_tokens in entities:
            matches = [
                (index, item)
                for index, item in indexed_remaining
                if comparison.research_result_matches_comparison_subject(
                    item=item,
                    entity_tokens=entity_tokens,
                )
            ]
            if not matches:
                continue
            best_index, best_item = min(
                matches,
                key=lambda pair: (
                    sections._research_official_candidate_kind_rank(pair[1]),
                    pair[0],
                ),
            )
            selected.append(best_item)
            indexed_remaining = [
                pair for pair in indexed_remaining if pair[0] != best_index
            ]
        remaining = [
            item
            for _, item in sorted(
                indexed_remaining,
                key=lambda pair: (
                    sections._research_official_candidate_kind_rank(pair[1]),
                    pair[0],
                ),
            )
        ]
        return [*selected, *remaining]


def _diversify_research_supporting_candidates(
    *,
    query: str,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
        entities = sections._research_comparison_entities(query)
        if len(entities) < 2 or len(candidates) < 2:
            return candidates
        indexed_remaining = list(enumerate(candidates))
        selected: list[dict[str, Any]] = []
        for entity_tokens in entities:
            matches = [
                (index, item)
                for index, item in indexed_remaining
                if comparison.research_result_matches_comparison_subject(
                    item=item,
                    entity_tokens=entity_tokens,
                )
            ]
            if not matches:
                continue
            best_index, best_item = min(
                matches,
                key=lambda pair: (
                    _research_supporting_candidate_kind_rank(
                        pair[1],
                        query=query,
                    ),
                    pair[0],
                ),
            )
            selected.append(best_item)
            indexed_remaining = [
                pair for pair in indexed_remaining if pair[0] != best_index
            ]
        remaining = [
            item
            for _, item in sorted(
                indexed_remaining,
                key=lambda pair: (
                    _research_supporting_candidate_kind_rank(
                        pair[1],
                        query=query,
                    ),
                    pair[0],
                ),
            )
        ]
        return [*selected, *remaining]


def _assemble_non_authoritative_research_candidates(
    *,
    query: str,
    project_candidates: list[dict[str, Any]],
    supporting_candidates: list[dict[str, Any]],
    curated_candidates: list[dict[str, Any]],
    listicle_candidates: list[dict[str, Any]],
    directory_candidates: list[dict[str, Any]],
    community_candidates: list[dict[str, Any]],
    max_results: int,
) -> list[dict[str, Any]]:
        if max_results <= 0:
            return []
        if not query_routing._looks_like_comparison_query(query.lower()):
            return [
                *project_candidates,
                *curated_candidates,
                *listicle_candidates,
                *directory_candidates,
                *community_candidates,
            ][:max_results]

        diversified_projects: list[dict[str, Any]] = []
        seen_project_domains: set[str] = set()
        for item in project_candidates:
            registered_domain = postprocess._registered_domain(
                postprocess._result_hostname(item)
            )
            domain_key = registered_domain or str(item.get("url") or "")
            if domain_key in seen_project_domains:
                continue
            seen_project_domains.add(domain_key)
            diversified_projects.append(item)

        ordered = diversified_projects[:2]
        remaining = max_results - len(ordered)
        if remaining <= 0:
            return ordered

        ordered.extend(supporting_candidates[: min(2, remaining)])
        remaining = max_results - len(ordered)
        if remaining <= 0:
            return ordered

        ordered.extend(curated_candidates[:remaining])
        remaining = max_results - len(ordered)
        if remaining <= 0:
            return ordered

        ordered.extend(listicle_candidates[:remaining])
        remaining = max_results - len(ordered)
        if remaining <= 0:
            return ordered

        ordered.extend(directory_candidates[:remaining])
        remaining = max_results - len(ordered)
        if remaining <= 0:
            return ordered

        ordered.extend(community_candidates[:remaining])
        return ordered[:max_results]


def _diversify_results_by_registered_domain(
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
        diversified: list[dict[str, Any]] = []
        seen_domains: set[str] = set()
        for item in results:
            registered_domain = postprocess._registered_domain(
                postprocess._result_hostname(item)
            )
            domain_key = registered_domain or str(item.get("url") or "")
            if domain_key in seen_domains:
                continue
            seen_domains.add(domain_key)
            diversified.append(item)
        return diversified


def _research_supporting_candidate_kind_rank(
    item: dict[str, Any],
    *,
    query: str,
) -> int:
        url = str(item.get("url") or "")
        title_text = str(item.get("title") or "").lower()
        path = urlparse(url).path.lower()
        comparison_like = query_routing._looks_like_comparison_query(query.lower())
        capability_markers = (
            "search",
            "extract",
            "scrape",
            "crawl",
            "retrieval",
            "api reference",
        )
        if any(marker in path for marker in ("/api-reference/", "/api-reference", "/reference/")) and any(
            marker in title_text or marker in path for marker in capability_markers
        ):
            return 0
        if any(marker in path for marker in ("/endpoint/scrape", "/endpoint/extract", "/features/scrape")):
            return 0
        if any(marker in title_text for marker in capability_markers):
            return 1
        if comparison_like and any(
            marker in path or marker in title_text
            for marker in ("agent-builder", "agent skills", "integrations")
        ):
            return 4
        if "/blog/" in path:
            return 5
        return 2


def _assemble_authoritative_research_candidates(
    *,
    official_candidates: list[dict[str, Any]],
    supporting_candidates: list[dict[str, Any]],
    general_candidates: list[dict[str, Any]],
    community_candidates: list[dict[str, Any]],
    max_results: int,
    prefer_canonical_vendor_docs: bool = False,
) -> list[dict[str, Any]]:
        if prefer_canonical_vendor_docs:
            vendor_official_candidates = [
                item
                for item in official_candidates
                if sections._research_is_canonical_vendor_doc(str(item.get("url") or ""))
            ]
            vendor_supporting_candidates = [
                item
                for item in supporting_candidates
                if sections._research_is_canonical_vendor_doc(str(item.get("url") or ""))
            ]
            other_official_candidates = [
                item
                for item in official_candidates
                if item not in vendor_official_candidates
            ]
            other_supporting_candidates = [
                item
                for item in supporting_candidates
                if item not in vendor_supporting_candidates
            ]
            anchor_candidates = [
                *vendor_official_candidates,
                *vendor_supporting_candidates,
                *other_official_candidates,
                *other_supporting_candidates,
            ]
        else:
            anchor_candidates = [*official_candidates, *supporting_candidates]
        if prefer_canonical_vendor_docs and len(general_candidates) > 1:
            indexed_general_candidates = list(enumerate(general_candidates))
            general_candidates = [
                item
                for _, item in sorted(
                    indexed_general_candidates,
                    key=lambda pair: (
                        _research_vendor_doc_general_candidate_kind_rank(pair[1]),
                        pair[0],
                    ),
                )
            ]
        if max_results <= 0:
            return []
        if not anchor_candidates:
            return [*general_candidates, *community_candidates][:max_results]

        ordered = anchor_candidates[:max_results]
        remaining = max_results - len(ordered)
        if remaining <= 0:
            return ordered

        anchor_count = len(ordered)
        if prefer_canonical_vendor_docs and anchor_count >= 2:
            general_limit = min(1, remaining)
        else:
            general_limit = min(2 if anchor_count >= 2 else 3, remaining)
        ordered.extend(general_candidates[:general_limit])
        remaining = max_results - len(ordered)
        if remaining > 0:
            community_limit = min(1, remaining)
            ordered.extend(community_candidates[:community_limit])
            remaining = max_results - len(ordered)
        if remaining > 0:
            ordered.extend(general_candidates[general_limit : general_limit + remaining])
            remaining = max_results - len(ordered)
        if remaining > 0:
            ordered.extend(community_candidates[1 : 1 + remaining])
        return ordered[:max_results]


def _research_vendor_doc_general_candidate_kind_rank(
    item: dict[str, Any],
) -> int:
        url = str(item.get("url") or "")
        hostname = postprocess._result_hostname(item)
        title_text = str(item.get("title") or "").lower()
        snippet_text = str(item.get("snippet") or "").lower()
        path = urlparse(url).path.lower()
        docs_markers = (
            "docs",
            "documentation",
            "guide",
            "guides",
            "retrieval",
            "search api",
            "api reference",
            "reference",
        )
        if any(marker in title_text or marker in snippet_text for marker in docs_markers):
            return 0
        if any(marker in path for marker in ("/docs", "/documentation", "/guide", "/guides")):
            return 0
        if hostname == "arxiv.org" or "/abs/" in path or path.endswith(".pdf") or "/pdf/" in path:
            return 4
        if "/blog/" in path or any(
            domain in hostname for domain in ("medium.com", "substack.com", "towardsai.net")
        ):
            return 3
        return 2


def _build_known_canonical_resource_rescue(
    *,
    query: str,
    mode: SearchMode,
    intent: ResolvedSearchIntent,
) -> dict[str, Any] | None:
        if mode == "social":
            return None
        query_lower = query.lower()
        if (
            mode not in {"docs", "github", "pdf", "web", "news"}
            and intent not in {"resource", "tutorial", "status"}
        ):
            return None
        if intent == "status" or query_routing._looks_like_status_query(query_lower):
            if "openai" in query_lower:
                return {
                    "title": "OpenAI Status",
                    "url": "https://status.openai.com/",
                    "snippet": "Official OpenAI status dashboard for incidents and service health.",
                    "provider": "canonical-rescue",
                    "matched_providers": ["canonical-rescue"],
                }
            if "cloudflare" in query_lower:
                return {
                    "title": "Cloudflare Status",
                    "url": "https://www.cloudflarestatus.com/",
                    "snippet": "Official Cloudflare status dashboard for incidents and service health.",
                    "provider": "canonical-rescue",
                    "matched_providers": ["canonical-rescue"],
                }
        if "playwright" in query_lower and (
            "strict mode" in query_lower or "violation" in query_lower or "locator" in query_lower
        ):
            return {
                "title": "Locators | Playwright",
                "url": "https://playwright.dev/docs/locators",
                "snippet": "Locators are strict. A strict mode violation happens when a locator resolves to more than one element.",
                "provider": "canonical-rescue",
                "matched_providers": ["canonical-rescue"],
            }
        if "playwright" in query_lower and "test.step" in query_lower:
            return {
                "title": "test.step | Playwright",
                "url": "https://playwright.dev/docs/api/class-test#test-step",
                "snippet": "Official Playwright API reference for test.step.",
                "provider": "canonical-rescue",
                "matched_providers": ["canonical-rescue"],
            }
        if ("next.js" in query_lower or "nextjs" in query_lower) and "hydration" in query_lower:
            return {
                "title": "Text content does not match server-rendered HTML | Next.js",
                "url": "https://nextjs.org/docs/messages/react-hydration-error",
                "snippet": "Official Next.js troubleshooting page for hydration mismatch errors and common fixes.",
                "provider": "canonical-rescue",
                "matched_providers": ["canonical-rescue"],
            }
        if query_routing._looks_like_github_release_query(query_lower):
            repo_slug = query_routing._extract_explicit_github_repo_slug(query)
            if repo_slug is not None:
                owner, repo = repo_slug
                return {
                    "title": f"Releases · {owner}/{repo} - GitHub",
                    "url": f"https://github.com/{owner}/{repo}/releases",
                    "snippet": f"Official GitHub releases page for {owner}/{repo}.",
                    "provider": "canonical-rescue",
                    "matched_providers": ["canonical-rescue"],
                }
        if "openai" in query_lower and "webhook" in query_lower:
            return {
                "title": "Webhooks | OpenAI API",
                "url": "https://developers.openai.com/api/docs/guides/webhooks/",
                "snippet": "Official OpenAI guide for receiving and verifying webhook events.",
                "provider": "canonical-rescue",
                "matched_providers": ["canonical-rescue"],
            }
        if "openai" in query_lower and "background mode" in query_lower:
            return {
                "title": "Background mode | OpenAI API",
                "url": "https://developers.openai.com/api/docs/guides/background/",
                "snippet": "Official OpenAI guide for background mode and long-running tasks.",
                "provider": "canonical-rescue",
                "matched_providers": ["canonical-rescue"],
            }
        if (
            "openai" in query_lower
            and "batch api" in query_lower
            and not query_routing._query_mentions_programming_language(query_lower)
        ):
            return {
                "title": "Batch API | OpenAI API",
                "url": "https://developers.openai.com/api/docs/guides/batch/",
                "snippet": "Official OpenAI guide for asynchronous Batch API workloads.",
                "provider": "canonical-rescue",
                "matched_providers": ["canonical-rescue"],
            }
        if (
            "openai" in query_lower
            and "api" in query_lower
            and query_routing._looks_like_pricing_query(query_lower)
            and not any(marker in query for marker in ("中文", "简体", "繁體", "繁体"))
        ):
            return {
                "title": "Pricing | OpenAI API",
                "url": "https://developers.openai.com/api/docs/pricing/",
                "snippet": "Official OpenAI API pricing reference.",
                "provider": "canonical-rescue",
                "matched_providers": ["canonical-rescue"],
            }
        if (
            ("next.js" in query_lower or "nextjs" in query_lower)
            and "generatemetadata" in re.sub(r"[^a-z0-9]+", "", query_lower)
        ):
            return {
                "title": "generateMetadata | Next.js",
                "url": "https://nextjs.org/docs/app/api-reference/functions/generate-metadata",
                "snippet": "Official Next.js API reference for generateMetadata.",
                "provider": "canonical-rescue",
                "matched_providers": ["canonical-rescue"],
            }
        react_hook = _extract_known_react_hook_reference(query)
        if react_hook is not None:
            react_locale = query_routing._preferred_react_docs_locale(query)
            locale_prefix = f"{react_locale}." if react_locale else ""
            react_title = (
                f"{react_hook} – React 中文文档"
                if react_locale == "zh-hans"
                else f"{react_hook} - React"
            )
            return {
                "title": react_title,
                "url": f"https://{locale_prefix}react.dev/reference/react/{react_hook}",
                "snippet": f"Official React API reference for {react_hook}.",
                "provider": "canonical-rescue",
                "matched_providers": ["canonical-rescue"],
            }
        return None


def _extract_known_react_hook_reference(
    query: str,
) -> str | None:
        query_lower = query.lower()
        if "react" not in query_lower:
            return None
        known_hooks = {
            "useactionstate": "useActionState",
            "useeffectevent": "useEffectEvent",
            "useoptimistic": "useOptimistic",
            "usetransition": "useTransition",
            "usedeferredvalue": "useDeferredValue",
        }
        for raw_token in re.findall(r"\buse[A-Za-z0-9]+\b", query):
            normalized = raw_token.lower()
            if normalized in known_hooks:
                return known_hooks[normalized]
            if len(raw_token) > 3:
                return raw_token
        for token in query_routing._query_precision_tokens(query):
            if token in known_hooks:
                return known_hooks[token]
        return None


def _filter_strong_award_results(
    *,
    query: str,
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
        if not results or not query_routing._has_strong_award_result(query=query, results=results):
            return results
        query_lower = query.lower()
        trusted_result_domains = {
            "abcnews.com",
            "apnews.com",
            "bbc.com",
            "cnn.com",
            "grammy.com",
            "grammys.com",
            "latimes.com",
            "npr.org",
            "nytimes.com",
            "oscars.org",
            "pbs.org",
            "reuters.com",
            "theacademy.com",
            "washingtonpost.com",
        }
        filtered_results: list[dict[str, Any]] = []
        for item in results:
            url = str(item.get("url") or "")
            registered_domain = postprocess._registered_domain(postprocess._result_hostname(item))
            path = urlparse(url).path.lower()
            title_text = str(item.get("title") or "").lower()
            snippet_text = str(item.get("snippet") or "").lower()
            content_text = str(item.get("content") or "").lower()
            if query_routing._looks_like_query_year_mismatch(
                query=query_lower,
                text=f"{title_text} {snippet_text} {content_text} {path}",
            ):
                continue
            if query_routing._looks_like_award_category_conflict(
                query_lower=query_lower,
                title_text=title_text,
                snippet_text=snippet_text,
                content_text=content_text,
            ):
                continue
            if query_routing._looks_like_award_brand_conflict(
                query_lower=query_lower,
                title_text=title_text,
                snippet_text=snippet_text,
                content_text=content_text,
                path=path,
            ):
                continue
            if query_routing._looks_like_generic_award_archive_result(
                title_text=title_text,
                path=path,
            ):
                continue
            winner_page = query_routing._looks_like_award_winner_result(
                title_text=title_text,
                snippet_text=snippet_text,
                path=path,
            )
            weak_official_feature = (
                registered_domain in query_routing._OFFICIAL_AWARD_DOMAINS
                and query_routing._looks_like_weak_official_award_feature_result(
                    title_text=title_text,
                    snippet_text=snippet_text,
                    path=path,
                )
            )
            if weak_official_feature:
                continue
            if (
                not winner_page
                and query_routing._looks_like_award_recap_or_gallery_result(
                    title_text=title_text,
                    snippet_text=snippet_text,
                    path=path,
                )
            ):
                continue
            category_match = query_routing._looks_like_award_category_match(
                query_lower=query_lower,
                title_text=title_text,
                snippet_text=snippet_text,
                content_text=content_text,
                path=path,
            )
            fact_match = query_routing._looks_like_award_fact_match(
                query_lower=query_lower,
                title_text=title_text,
                snippet_text=snippet_text,
                content_text=content_text,
                path=path,
            )
            award_coverage_page = query_routing._looks_like_award_coverage_page(
                query_lower=query_lower,
                title_text=title_text,
                path=path,
            )
            official_award_page = registered_domain in query_routing._OFFICIAL_AWARD_DOMAINS
            prioritized_winner_page = winner_page and (
                category_match
                or award_coverage_page
                or registered_domain in trusted_result_domains
                or official_award_page
            )
            trusted_media_page = (
                registered_domain in trusted_result_domains
                and award_coverage_page
                and category_match
            )
            if not (
                official_award_page
                or prioritized_winner_page
                or (fact_match and award_coverage_page)
                or trusted_media_page
            ):
                continue
            filtered_results.append(item)
        return filtered_results or results


def _rerank_general_results(
    *,
    query: str,
    result_profile: Literal['web', 'news'],
    results: list[dict[str, Any]],
    include_domains: list[str] | None,
) -> list[dict[str, Any]]:
        if len(results) < 2:
            return results
        ranked = sorted(
            enumerate(results),
            key=lambda pair: (
                _general_result_rank(
                    query=query,
                    result_profile=result_profile,
                    item=pair[1],
                    include_domains=include_domains,
                ),
                -pair[0],
            ),
            reverse=True,
        )
        return [dict(pair[1]) for pair in ranked]


def _general_result_rank(
    *,
    query: str,
    result_profile: Literal['web', 'news'],
    item: dict[str, Any],
    include_domains: list[str] | None,
) -> tuple[int, ...]:
        if result_profile == "news":
            return ranking._news_result_rank(
                query=query,
                item=item,
                include_domains=include_domains,
            )
        return ranking._web_result_rank(
            query=query,
            item=item,
            include_domains=include_domains,
        )


def _rerank_resource_results(
    *,
    query: str,
    mode: SearchMode,
    results: list[dict[str, Any]],
    include_domains: list[str] | None,
) -> list[dict[str, Any]]:
        if len(results) < 2:
            return results

        query_tokens = query_routing._query_brand_tokens(query)
        precision_tokens = query_routing._query_precision_tokens(query)
        exact_identifier_tokens = query_routing._query_exact_identifier_tokens(query)
        topic_specific_tokens = query_routing._query_topic_specific_tokens(query)
        strict_official = bool(include_domains) or (
            query_routing._resolve_official_result_mode(
                query=query,
                mode=mode,
                intent="resource",
                include_domains=include_domains,
            )
            == "strict"
        )
        ranked = sorted(
            enumerate(results),
            key=lambda pair: (
                ranking._resource_result_rank(
                    query=query,
                    mode=mode,
                    item=pair[1],
                    query_tokens=query_tokens,
                    precision_tokens=precision_tokens,
                    exact_identifier_tokens=exact_identifier_tokens,
                    topic_specific_tokens=topic_specific_tokens,
                    include_domains=include_domains,
                    strict_official=strict_official,
                ),
                -pair[0],
            ),
            reverse=True,
        )
        return [postprocess._canonicalize_result_item(dict(pair[1])) for pair in ranked]


def _collect_source_domains(
    *,
    results: list[dict[str, Any]],
    citations: list[dict[str, Any]],
) -> list[str]:
        domains: list[str] = []
        seen: set[str] = set()
        for item in [*results, *citations]:
            if not isinstance(item, dict):
                continue
            hostname = postprocess._result_hostname(item)
            registered_domain = postprocess._registered_domain(hostname)
            if not registered_domain or registered_domain in seen:
                continue
            seen.add(registered_domain)
            domains.append(registered_domain)
        return domains

