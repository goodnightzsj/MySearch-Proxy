"""Research claim analysis: pure text-to-bool/str/int transforms.

No network calls, no provider dependencies, no self state.
Extracted from MySearchClient methods in clients.py.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

def research_excerpt_looks_like_json_shell(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return False
    lowered = normalized.lower()
    if not (
        normalized.startswith("{")
        or normalized.startswith("{{")
        or '"id"' in lowered
        or '"completion_window"' in lowered
        or '"created_at"' in lowered
    ):
        return False
    json_field_count = len(
        re.findall(
            r'"[a-z0-9_]{2,40}"\s*:\s*(?:"[^"]*"|\d+|true|false|null|\{|\[)',
            lowered,
        )
    )
    if json_field_count >= 2:
        return True
    return any(
        marker in lowered
        for marker in (
            '"id":',
            '"completion_window":',
            '"created_at":',
            '"request_counts":',
            '"input_file_id":',
            '"output_file_id":',
            '"error_file_id":',
            '"status":',
            '"endpoint":',
        )
    )



def research_claim_signature(claim: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", claim.lower()).strip()
    if not normalized:
        return ""
    stopwords = {
        "a",
        "an",
        "and",
        "api",
        "best",
        "by",
        "docs",
        "documentation",
        "for",
        "guide",
        "in",
        "latest",
        "of",
        "official",
        "reference",
        "the",
        "to",
        "updated",
        "with",
    }
    tokens = [
        token
        for token in normalized.split()
        if token not in stopwords and len(token) > 1
    ]
    signature_tokens = tokens[:8] or normalized.split()[:8]
    return " ".join(signature_tokens)



def research_claim_is_generic(claim: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", claim.lower()).strip()
    if not normalized:
        return True
    if normalized.startswith(("best ", "top ", "compare ", "comparison ")):
        return True
    if normalized.endswith((" for", " guide", " reference", " docs")):
        return True
    if any(
        marker in normalized
        for marker in (
            "agent builder",
            "agent skills",
            "authoritative content for",
            "api guide",
            "api reference",
            "best mcp servers",
            "best practices",
            "compare models",
            "complete comparison",
            "definition examples",
            "integration docs",
            "integrations",
            "ultimate guide",
        )
    ):
        return True
    if "alternatives" in normalized:
        return True
    tokens = normalized.split()
    generic_tokens = {
        "api",
        "authoritative",
        "batch",
        "batches",
        "compare",
        "comparison",
        "content",
        "docs",
        "documentation",
        "guide",
        "guides",
        "model",
        "mcp",
        "models",
        "openai",
        "reference",
        "search",
        "server",
        "servers",
        "tool",
        "tools",
    }
    if ("vs" in tokens or "versus" in tokens) and any(
        marker in normalized
        for marker in ("comparison", "compare", "guide", "alternatives")
    ):
        return True
    meaningful_tokens = [token for token in tokens if token not in generic_tokens]
    if not meaningful_tokens and len(tokens) <= 4:
        return True
    return len(tokens) <= 3 and len(meaningful_tokens) <= 1



def research_claim_comparison_subject_match_count(
    
    *,
    claim: str,
    sources: Sequence[str],
    entities: Sequence[Sequence[str]],
) -> int:
    if not claim or not entities:
        return 0
    haystack = " ".join([claim, *sources]).lower()
    match_count = 0
    for entity_tokens in entities[:4]:
        tokens = [str(token).strip().lower() for token in entity_tokens if str(token).strip()]
        if tokens and all(token in haystack for token in tokens):
            match_count += 1
    return match_count



def research_claim_is_comparison_tail_relevant(claim: str) -> bool:
    normalized = f" {claim.lower().strip()} "
    if not normalized.strip():
        return False
    return any(
        marker in normalized
        for marker in (
            " background ",
            " async ",
            " asynchronous ",
            " latency ",
            " streaming ",
            " long running ",
            " long-running ",
            " bulk ",
            " batch api ",
            " responses api ",
            " tool-using ",
            " interactive ",
        )
    )



def research_claim_support_level(
    
    *,
    source_count: int,
    provider_count: int,
    cluster_count: int,
) -> str:
    if provider_count >= 2 and source_count >= 1:
        return "cross-provider"
    if source_count >= 3 or cluster_count >= 2:
        return "multi-source"
    if source_count >= 2:
        return "corroborated"
    return "single-source"



def research_claim_support_basis(claim_entry: Mapping[str, Any]) -> str:
    support_level = str(claim_entry.get("support_level") or "").strip()
    providers = {
        str(item).strip()
        for item in (claim_entry.get("providers") or [])
        if str(item).strip()
    }
    clusters = {
        str(item).strip()
        for item in (claim_entry.get("clusters") or [])
        if str(item).strip()
    }
    has_project = "project" in clusters
    has_vendor_docs = "canonical_research_docs" in providers and (
        "official" in clusters or "supporting" in clusters
    )
    has_official = "official" in clusters

    if has_project and has_vendor_docs:
        return "shortlisted comparison page and vendor docs"
    if has_project:
        return "shortlisted comparison page"
    if has_vendor_docs:
        return (
            "shortlisted vendor docs"
            if support_level in {"cross-provider", "multi-source", "corroborated"}
            else "shortlisted vendor doc"
        )
    if has_official:
        return (
            "shortlisted official docs"
            if support_level in {"cross-provider", "multi-source", "corroborated"}
            else "shortlisted official doc"
        )
    return ""



def research_claim_support_rank(support_level: str) -> int:
    return {
        "cross-provider": 4,
        "multi-source": 3,
        "corroborated": 2,
        "single-source": 1,
    }.get(support_level, 0)



def research_claim_best_cluster_rank(
    
    *,
    clusters: list[str],
    authoritative_preferred: bool,
) -> int:
    if not clusters:
        return 0
    if authoritative_preferred:
        weights = {
            "official": 5,
            "supporting": 4,
            "general": 3,
            "project": 3,
            "curated": 2,
            "directory": 1,
            "listicle": 1,
            "community": 0,
        }
    else:
        weights = {
            "project": 5,
            "supporting": 4,
            "curated": 3,
            "general": 3,
            "official": 3,
            "listicle": 2,
            "directory": 1,
            "community": 0,
        }
    return max(weights.get(cluster, 0) for cluster in clusters)



def research_excerpt_has_substantive_claim(text: str) -> bool:
    normalized = " " + re.sub(r"\s+", " ", text.lower()).strip() + " "
    word_count = len(normalized.split())
    if word_count < 5:
        return False
    markers = (
        " is ",
        " are ",
        " can ",
        " use ",
        " vary by ",
        " process ",
        " processes ",
        " handles ",
        " supports ",
        " allows ",
        " enables ",
        " helps ",
        " uses ",
        " provides ",
        " delivers ",
        " exposes ",
        " integrates ",
        " explores ",
        " built for ",
        " designed to ",
        " suited to ",
        " better suited ",
        " should be considered ",
        " unlike ",
        " compared with ",
        " compared to ",
    )
    if word_count < 7:
        short_sentence_markers = (
            " process ",
            " processes ",
            " use ",
            " uses ",
            " build ",
            " builds ",
            " run ",
            " runs ",
            " manage ",
            " manages ",
            " migrate ",
            " migrates ",
            " compare ",
            " compares ",
        )
        return any(marker in normalized for marker in short_sentence_markers)
    return any(
        marker in normalized
        for marker in markers
    )



def research_excerpt_looks_like_link_index_noise(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return False
    lowered = normalized.lower()
    markdown_link_count = normalized.count("](")
    if markdown_link_count >= 2:
        return True
    if "![image" in lowered or "[![image" in lowered:
        return True
    if normalized.startswith(("* [", "- [")) and markdown_link_count >= 1:
        return True
    if normalized.startswith("# ") and (
        markdown_link_count >= 1
        or "openai developers" in lowered
        or "api reference" in lowered
    ):
        return True
    return False



def research_excerpt_looks_like_navigation_noise(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "primary navigation",
            "copy markdown",
            "open in chatgpt",
            "search docs",
            "skip to content",
            "skip to main content",
            "suggested",
            "chatgpt actions",
            "search the api docs",
            "marketing copy",
            "copy markdown",
            "view as markdown",
            "access to this page requires authorization",
            "exit editor mode",
            "focus mode note",
            "ask learn",
            "get api key",
            "available skills",
            "sign up at tavily.com",
            "why use these skills",
        )
    )



def research_excerpt_looks_like_noise(text: str) -> bool:
    if research_excerpt_looks_like_json_shell(text):
        return True
    lowered = text.lower()
    code_like_markers = (
        "api_key=",
        "schema = {",
        "\"type\": \"object\"",
        "\"properties\": {",
        "\"required\": [",
        "firecrawl = firecrawl(",
        "from firecrawl import firecrawl",
        "your-api-key",
        "const exa = new exa(",
        "await exa.getcontents(",
        "highlights: {",
        "maxcharacters:",
    )
    if sum(1 for marker in code_like_markers if marker in lowered) >= 2:
        return True
    return any(
        marker in lowered
        for marker in (
            "step 1: curl",
            "curl -fssl",
            "curl --request",
            "authorization: bearer",
            "content-type: application/json",
            "x-api-key",
            "generated using ai and may contain mistakes",
            "api_key=",
            "schema = {",
            "your-api-key",
            "const exa = new exa(",
            "await exa.getcontents(",
            "highlights: {",
            "maxcharacters:",
            "start getting web data for free",
            "no credit card needed",
            "scale seamlessly as your project expands",
            "reasoning tokens pricing per 1m tokens",
            "context window",
            "knowledge cutoff",
            "cached input",
            "output tokens",
            "endpoints v1/chat",
            "ready to build",
            "table of contents",
            "back to all posts",
            "you signed in with another tab",
            "method not allowed",
            "\"error\"",
            "jsonrpc",
        )
    )
