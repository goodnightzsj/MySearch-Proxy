"""research 模式的发现路径规划：文档查询候选、权威救援与路由选择。

从 `mysearch/clients.py` 抽出的**研究发现规划层**。输入是查询文本与意图，
输出是候选查询列表、已知 provider 文档结果，以及"该走哪条发现路径"的决策——
不做 provider 调用、不读 config、不碰实例状态。

依赖方向单向：本模块依赖 `query_routing`（查询与结果谓词）、`postprocess`
（结果规范化）；`clients` 在上层依赖本模块。

`MySearchClient` 保留同名方法作为一行委托，调用面不变。
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from mysearch import postprocess
from mysearch import query_routing
from mysearch.research import sections
from mysearch.research import selection
from mysearch.types import ResolvedSearchIntent, SearchMode
def _research_authoritative_rescue_queries(
    query: str,
) -> list[str]:
        subjects = sections._research_parse_comparison_subjects(query)
        if not subjects:
            return [query]
        normalized = re.sub(r"\b20\d{2}\b", "", query).strip()
        match = re.search(
            r"^\s*compare\s+(.+?)(?:\s+for\s+(.+))?$",
            normalized,
            re.IGNORECASE,
        )
        context = (match.group(2) or "").strip(" ,.;:") if match else ""
        brand_prefix = ""
        first_words = subjects[0].split()
        generic_tokens = {
            "api",
            "docs",
            "documentation",
            "guide",
            "guides",
            "official",
            "openai",
            "resource",
            "resources",
            "reference",
        }
        if first_words:
            candidate = first_words[0].strip(" ,.;:")
            if candidate and candidate[0].isalpha() and candidate[0].isupper():
                brand_prefix = candidate
        queries: list[str] = [query]
        for part in subjects[:3]:
            candidate = part
            if sections._research_subject_should_inherit_brand_prefix(
                subject=candidate,
                brand_prefix=brand_prefix,
                generic_tokens=generic_tokens,
            ):
                candidate = f"{brand_prefix} {candidate}"
            if context:
                queries.append(f"{candidate} official docs {context}".strip())
            queries.append(f"{candidate} official docs".strip())
            queries.extend(_research_known_provider_doc_queries(candidate))
        deduped: list[str] = []
        seen: set[str] = set()
        for candidate in queries:
            normalized_candidate = re.sub(r"\s+", " ", candidate).strip()
            if not normalized_candidate:
                continue
            dedupe_key = normalized_candidate.lower()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            deduped.append(normalized_candidate)
        return deduped


def _research_known_provider_doc_queries(
    entity: str,
) -> list[str]:
        lowered = entity.lower()
        if "tavily" in lowered:
            return [
                "Tavily search api docs",
                "Tavily extract docs",
            ]
        if "firecrawl" in lowered:
            return [
                "Firecrawl scrape docs",
                "Firecrawl extract docs",
            ]
        if re.search(r"\bexa\b", lowered):
            return [
                "Exa search docs",
                "Exa contents docs",
            ]
        if "apify" in lowered:
            return [
                "Apify api docs",
                "Apify actors docs",
            ]
        return []


def _research_relaxed_discovery_query(
    query: str,
) -> str:
        relaxed = re.sub(r"\bofficial\b", " ", query, flags=re.IGNORECASE)
        relaxed = re.sub(r"\s+", " ", relaxed).strip()
        return relaxed or query.strip()


def _research_primary_discovery_route(
    *,
    query: str,
    mode: SearchMode,
    intent: ResolvedSearchIntent,
    authoritative_research: bool,
) -> dict[str, str]:
        if authoritative_research and selection._research_prefers_canonical_vendor_docs(query):
            return {
                "query": _research_relaxed_discovery_query(query),
                "mode": "web",
                "intent": "exploratory",
            }
        return {
            "query": query,
            "mode": mode,
            "intent": intent,
        }


def _research_prefers_tavily_discovery(
    *,
    query: str,
    mode: SearchMode,
    intent: ResolvedSearchIntent,
    authoritative_research: bool,
) -> bool:
        if mode == "web" and not authoritative_research:
            return True
        if mode == "news" or intent == "news":
            return True
        if authoritative_research and sections._research_primary_vendor_brand(query):
            return True
        return False


def _research_known_provider_doc_results(
    query: str,
) -> list[dict[str, Any]]:
        if not query_routing._looks_like_comparison_query(query.lower()):
            return []
        catalog = sections._research_canonical_doc_catalog()
        seen_urls: set[str] = set()
        entity_texts = {
            " ".join(entity_tokens).lower()
            for entity_tokens in sections._research_comparison_entities(query)
        }
        comparison_projects: dict[frozenset[str], dict[str, str]] = {
            frozenset({"firecrawl", "tavily"}): {
                "title": "Firecrawl vs Tavily - Firecrawl",
                "url": "https://www.firecrawl.dev/compare/firecrawl-vs-tavily",
                "snippet": (
                    "Firecrawl positions itself as an extraction-first workflow with scrape "
                    "and structured extraction, while Tavily focuses on search and retrieval APIs."
                ),
            },
            frozenset({"firecrawl", "exa"}): {
                "title": "Firecrawl vs Exa - Firecrawl",
                "url": "https://www.firecrawl.dev/compare/firecrawl-vs-exa",
                "snippet": (
                    "Firecrawl compares extraction-first crawling and structured data workflows "
                    "against Exa's semantic search and discovery APIs."
                ),
            },
            frozenset({"exa", "tavily"}): {
                "title": "Exa vs Tavily: 5x More Results & Content Filtering",
                "url": "https://exa.ai/versus/tavily",
                "snippet": (
                    "Exa compares semantic search, result volume, and content filtering "
                    "against Tavily for AI search workflows."
                ),
            },
            frozenset({"firecrawl", "apify"}): {
                "title": "Firecrawl vs Apify: Complete Comparison for AI Agents & RAG (2026)",
                "url": "https://www.firecrawl.dev/compare/firecrawl-vs-apify",
                "snippet": (
                    "Firecrawl compares AI-ready scraping, extraction, and agent workflows "
                    "against Apify's actor platform and scraping ecosystem."
                ),
            },
        }
        tracked_brands = set(catalog.keys())
        for pair in comparison_projects:
            tracked_brands.update(pair)
        entity_brands = {
            brand
            for brand in tracked_brands
            if any(brand in entity for entity in entity_texts)
        }
        project_results: list[dict[str, Any]] = []
        injected_pair_project = False
        for pair, item in comparison_projects.items():
            if not pair.issubset(entity_brands):
                continue
            comparison_url = item["url"]
            if comparison_url in seen_urls:
                continue
            injected_pair_project = True
            seen_urls.add(comparison_url)
            project_results.append(
                {
                    "provider": "canonical_research_projects",
                    "title": item["title"],
                    "url": comparison_url,
                    "snippet": item["snippet"],
                }
            )
        generic_comparison_hub = "https://www.firecrawl.dev/compare"
        if (
            "firecrawl" in entity_brands
            and len(entity_texts) >= 2
            and not injected_pair_project
            and generic_comparison_hub not in seen_urls
        ):
            seen_urls.add(generic_comparison_hub)
            project_results.append(
                {
                    "provider": "canonical_research_projects",
                    "title": "Compare Firecrawl with Alternatives | In-depth Tool Comparisons",
                    "url": generic_comparison_hub,
                    "snippet": (
                        "Firecrawl maintains first-party comparison pages covering extraction, "
                        "search, and RAG trade-offs against alternative tooling."
                    ),
                }
            )
        supporting_results: list[dict[str, Any]] = []
        query_lower = query.lower()
        for entity_tokens in sections._research_comparison_entities(query):
            entity_text = " ".join(entity_tokens).lower()
            for brand, items in catalog.items():
                if brand not in entity_text:
                    continue
                for item in items:
                    url = str(item.get("url") or "")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    supporting_results.append(dict(item))
        if "responses api" in query_lower and "batch api" in query_lower:
            for key in ("responses api", "batch api"):
                for item in catalog.get(key, []):
                    url = str(item.get("url") or "")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    supporting_results.append(dict(item))
        if (
            "responses api" in query_lower
            and "batch api" in query_lower
            and any(
                marker in query_lower
                for marker in ("long-running", "long running", "asynchronous", "background")
            )
        ):
            for item in catalog.get("background mode", []):
                url = str(item.get("url") or "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                supporting_results.append(dict(item))
        return [*project_results, *supporting_results]


def _research_generic_vendor_doc_results(
    query: str,
) -> list[dict[str, Any]]:
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
            return []
        generic_tokens = {
            "agent",
            "agentic",
            "agents",
            "ai",
            "approach",
            "best",
            "docs",
            "documentation",
            "guide",
            "guides",
            "official",
            "retrieval",
            "search",
            "web",
            "workflow",
            "workflows",
        }
        specific_tokens = [
            token
            for token in query_routing._query_precision_tokens(query)
            if token not in generic_tokens
        ]
        if specific_tokens:
            return []
        catalog = sections._research_canonical_doc_catalog()
        return [
            dict(catalog["tavily"][0]),
            dict(catalog["firecrawl"][1]),
            dict(catalog["exa"][0]),
        ]


def _research_prefers_authoritative_sources(
    *,
    query: str,
    mode: SearchMode,
    intent: ResolvedSearchIntent,
    include_domains: list[str] | None,
) -> bool:
        query_lower = query.lower()
        if mode in {"docs", "github", "pdf"}:
            return True
        if include_domains:
            return True
        if query_routing._should_use_strict_resource_policy(
            query=query,
            mode=mode,
            intent=intent,
            include_domains=include_domains,
        ):
            return True
        return query_routing._looks_like_technical_research_query(query_lower)

