"""查询理解谓词：这是一类查询吗？

从 `mysearch/clients.py` 抽出的**无状态谓词层**。这些函数只根据 query 文本和
结果项的形状做判断（"像不像 pricing 查询"、"是不是官方文档查询"、"这条结果像不像
聚合站"），不访问 `config` / `keyring` / cache，因此可以独立成模块。

`MySearchClient` 保留同名方法作为一行委托，调用面不变。

依赖方向是单向的：本模块依赖 `postprocess`（hostname/domain 规范化）与 `types`
（Literal 别名），`clients` 在上层依赖本模块。三者之间无环。
"""

from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import urlparse

from mysearch import postprocess
from mysearch.errors import MySearchHTTPError
from mysearch.postprocess import _HCAPTCHA_LANGUAGES
from mysearch.types import ResolvedSearchIntent, SearchMode, SearchStrategy

#: 官方颁奖机构域名：出现在结果里时说明命中官方颁奖页。
_OFFICIAL_AWARD_DOMAINS = frozenset(
    {
        "grammy.com",
        "grammys.com",
        "oscars.org",
        "theacademy.com",
    }
)

def _looks_like_technical_research_query(query_lower: str) -> bool:
    technical_markers = (
        " api",
        "api ",
        "sdk",
        "reference",
        "background mode",
        "batch api",
        "responses api",
        "webhook",
        "webhooks",
        "technical report",
        "research paper",
        "pdf",
    )
    return _looks_like_docs_query(query_lower) or any(
        marker in query_lower for marker in technical_markers
    )


def _looks_like_authoritative_research_target(*,
    url: str,
    hostname: str,
    title_text: str,
    mode: SearchMode,
) -> bool:
    path = urlparse(url).path.lower()
    if mode == "github":
        return hostname in {"github.com", "raw.githubusercontent.com"} and any(
            marker in path
            for marker in (
                "/blob/",
                "/discussions/",
                "/issues/",
                "/pull/",
                "/releases",
                "/tree/",
            )
        )
    if mode == "pdf":
        return _looks_like_pdf_url(url) or (
            hostname == "arxiv.org" and path.startswith("/abs/")
        )
    authoritative_path_markers = (
        "/api/docs",
        "/api/reference",
        "/changelog",
        "/docs",
        "/documentation",
        "/guide/",
        "/guides/",
        "/manual",
        "/pricing",
        "/readme",
        "/reference/",
        "/references/",
    )
    authoritative_title_markers = (
        "api reference",
        "changelog",
        "documentation",
        "guide",
        "manual",
        "pricing",
        "readme",
        "reference",
    )
    return any(marker in path for marker in authoritative_path_markers) or any(
        marker in title_text for marker in authoritative_title_markers
    )


def _looks_like_authoritative_research_host(*,
    hostname: str,
    path: str,
) -> bool:
    host = hostname.lower()
    normalized_path = path.lower()
    if any(
        host.startswith(prefix)
        for prefix in (
            "api.",
            "developer.",
            "developers.",
            "docs.",
        )
    ):
        return True
    if any(marker in host for marker in (".docs.", ".developer.", ".developers.")):
        return True
    return any(
        marker in normalized_path
        for marker in (
            "/api/docs",
            "/api/reference",
            "/api-reference",
            "/docs/",
            "/documentation/",
            "/guide/",
            "/guides/",
            "/manual",
            "/reference/",
            "/references/",
        )
    )


def _looks_like_research_marketing_or_blog_result(*,
    hostname: str,
    path: str,
    title_text: str,
    snippet_text: str,
) -> bool:
    normalized_path = path.rstrip("/")
    marketing_path_markers = (
        "/article/",
        "/articles/",
        "/blog/",
        "/blogs/",
        "/insights/",
        "/learn/",
        "/news/",
        "/post/",
        "/posts/",
        "/resources/",
    )
    if any(marker in normalized_path for marker in marketing_path_markers):
        return True
    title_tokens = f" {title_text} "
    snippet_tokens = f" {snippet_text} "
    marketing_title_markers = (
        " alternatives ",
        " best ",
        " comparison ",
        " complete guide ",
        " landscape ",
        " seo ",
        " top ",
        " vs ",
    )
    if any(marker in title_tokens for marker in marketing_title_markers):
        return True
    if hostname.startswith("www.") and any(
        marker in snippet_tokens
        for marker in (" ai search ", " ai seo ", " better pricing ", " complete guide ")
    ):
        return True
    return False


def _looks_like_supporting_research_target(*,
    url: str,
    hostname: str,
    title_text: str,
    snippet_text: str,
    mode: SearchMode,
) -> bool:
    path = urlparse(url).path.lower()
    if _looks_like_research_marketing_or_blog_result(
        hostname=hostname,
        path=path,
        title_text=title_text,
        snippet_text=snippet_text,
    ):
        return False
    if mode == "pdf":
        return _looks_like_pdf_url(url) or (
            hostname == "arxiv.org" and path.startswith("/abs/")
        )
    hostname_labels = [item for item in hostname.split(".") if item]
    docs_host = any(
        label in {"api", "developer", "developers", "docs", "help", "platform", "reference", "support"}
        for label in hostname_labels
    )
    docs_path_markers = (
        "/api",
        "/documentation",
        "/docs",
        "/guide",
        "/guides",
        "/manual",
        "/reference",
        "/references",
    )
    docs_title_markers = (
        "api reference",
        "developer documentation",
        "documentation",
        "docs",
        "manual",
        "reference",
    )
    return (
        docs_host
        or any(marker in path for marker in docs_path_markers)
        or any(marker in title_text for marker in docs_title_markers)
    )


def _resolve_official_result_mode(*,
    query: str,
    mode: SearchMode,
    intent: ResolvedSearchIntent,
    include_domains: list[str] | None,
) -> str:
    if mode == "social":
        return "off"
    if _should_use_strict_resource_policy(
        query=query,
        mode=mode,
        intent=intent,
        include_domains=include_domains,
    ):
        return "strict"
    if _should_rerank_resource_results(mode=mode, intent=intent):
        return "standard"
    return "off"


def _should_use_strict_resource_policy(*,
    query: str,
    mode: SearchMode,
    intent: ResolvedSearchIntent,
    include_domains: list[str] | None,
) -> bool:
    if mode == "social":
        return False
    query_lower = query.lower()
    explicit_resource_mode = mode in {"docs", "github", "pdf"}
    if _looks_like_github_release_query(query_lower):
        return False
    if include_domains:
        return True
    if intent == "tutorial" and not explicit_resource_mode:
        return False
    if explicit_resource_mode:
        return True
    if _looks_like_official_query(query):
        return True
    if _looks_like_pricing_query(query_lower):
        return True
    if _looks_like_changelog_query(query_lower):
        return True
    if intent in {"resource", "tutorial"} and _looks_like_docs_query(query_lower):
        return True
    return False


def _looks_like_official_query(query: str) -> bool:
    query_lower = query.lower()
    if re.search(r"\bofficial\b", query_lower):
        return True
    official_markers = (
        "官网",
        "官方",
        "原文",
        "定价官方",
        "官方定价",
        "官方价格",
        "官方文档",
    )
    return any(marker in query for marker in official_markers)


def _looks_like_changelog_query(query_lower: str) -> bool:
    keywords = [
        "changelog",
        "latest release",
        "latest releases",
        "release notes",
        "what's new",
        "whats new",
        "更新日志",
        "发布说明",
        "变更日志",
        "版本更新",
    ]
    return any(keyword in query_lower for keyword in keywords)


def _looks_like_github_release_query(query_lower: str) -> bool:
    if "github" not in query_lower:
        return False
    return any(
        marker in query_lower
        for marker in ("latest release", "latest releases", "release", "releases", "changelog")
    )


def _extract_explicit_github_repo_slug(query: str) -> tuple[str, str] | None:
    match = re.search(
        r"\b([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\b",
        query,
    )
    if not match:
        return None
    return match.group(1), match.group(2)


def _preferred_react_docs_locale(query: str) -> str | None:
    query_lower = query.lower()
    if "中文" in query or "简体" in query or "zh-hans" in query_lower:
        return "zh-hans"
    if "繁體" in query or "繁体" in query or "zh-hant" in query_lower:
        return "zh-hant"
    return None


def _query_prefers_versioned_react_docs(query: str, *, version: str) -> bool:
    query_lower = query.lower()
    return bool(
        re.search(rf"\breact\s*v?{re.escape(version)}\b", query_lower)
        or re.search(rf"\breact{re.escape(version)}\b", query_lower)
    )


def _looks_like_noncanonical_react_docs_hostname(hostname: str, *, query: str = "") -> bool:
    normalized = _clean_hostname(hostname)
    if normalized in {"beta.reactjs.org", "legacy.reactjs.org"}:
        return True
    match = re.fullmatch(r"(?P<version>\d+)\.react\.dev", normalized)
    if not match:
        return False
    if query and _query_prefers_versioned_react_docs(query, version=match.group("version")):
        return False
    return True


def _should_apply_canonical_resource_rescue(*,
    query: str,
    mode: SearchMode,
    intent: ResolvedSearchIntent,
    official_candidates: list[dict[str, Any]],
    rescue_candidate: dict[str, Any],
) -> bool:
    query_lower = query.lower()
    if not official_candidates:
        return True
    rescue_url = str(rescue_candidate.get("url") or "")
    if rescue_url and any(str(item.get("url") or "") == rescue_url for item in official_candidates):
        return False
    top_candidate = official_candidates[0]
    top_url = str(top_candidate.get("url") or "")
    top_path = urlparse(top_url).path.lower()
    top_title = str(top_candidate.get("title") or "").lower()
    if intent == "status" or _looks_like_status_query(query_lower):
        if _looks_like_status_result(
            url=top_url,
            hostname=_result_hostname(top_candidate),
            title_text=top_title,
        ):
            return False
        return True
    if "openai" in query_lower and (
        "webhook" in query_lower
        or "background mode" in query_lower
        or "batch api" in query_lower
        or (
            "api" in query_lower
            and _looks_like_pricing_query(query_lower)
            and not any(marker in query for marker in ("中文", "简体", "繁體", "繁体"))
        )
    ):
        return True
    if "playwright" in query_lower and "test.step" in query_lower:
        return True
    if (
        ("next.js" in query_lower or "nextjs" in query_lower)
        and "generatemetadata" in re.sub(r"[^a-z0-9]+", "", query_lower)
    ):
        return True
    if _looks_like_github_release_query(query_lower):
        repo_slug = _extract_explicit_github_repo_slug(query)
        if repo_slug is not None:
            owner, repo = repo_slug
            normalized_path = top_path.rstrip("/")
            repo_prefix = f"/{owner.lower()}/{repo.lower()}"
            if not normalized_path.startswith(f"{repo_prefix}/releases"):
                return True
            if "/releases/tag/" in normalized_path:
                return True
    if _preferred_react_docs_locale(query):
        return False
    if (
        mode == "docs"
        or intent in {"resource", "tutorial"}
    ) and _looks_like_noncanonical_react_docs_hostname(
        _result_hostname(top_candidate),
        query=query,
    ):
        return True
    if (
        mode == "docs"
        or intent in {"resource", "tutorial"}
    ) and (
        _looks_like_locale_prefixed_path(top_path)
        or _looks_like_locale_prefixed_hostname(_result_hostname(top_candidate))
    ):
        return True
    if _looks_like_language_specific_sdk_reference_result(
        hostname=_result_hostname(top_candidate),
        path=top_path,
        title_text=top_title,
    ) and not _query_mentions_programming_language(query_lower):
        return True
    if _looks_like_generic_official_landing_result(
        hostname=_result_hostname(top_candidate),
        path=top_path,
        title_text=top_title,
    ):
        return True
    if (
        mode == "docs"
        or intent in {"resource", "tutorial"}
    ) and (
        _looks_like_api_docs_topic_query(query_lower)
        or _looks_like_debugging_query(query_lower)
    ):
        topic_markers = [token for token in _query_precision_tokens(query) if len(token) >= 4]
        top_path_hits, top_total_hits = _query_precision_hit_counts_with_body(
            hostname=_result_hostname(top_candidate),
            path=top_path,
            title_text=top_title,
            body_text=f"{top_candidate.get('snippet', '')} {top_candidate.get('content', '')}",
            query_tokens=topic_markers,
        )
        rescue_path = urlparse(rescue_url).path.lower()
        rescue_path_hits, rescue_total_hits = _query_precision_hit_counts_with_body(
            hostname=_result_hostname(rescue_candidate),
            path=rescue_path,
            title_text=str(rescue_candidate.get("title") or "").lower(),
            body_text=f"{rescue_candidate.get('snippet', '')} {rescue_candidate.get('content', '')}",
            query_tokens=topic_markers,
        )
        if (
            (
                _looks_like_language_specific_sdk_reference_result(
                    hostname=_result_hostname(top_candidate),
                    path=top_path,
                    title_text=top_title,
                )
                or _looks_like_language_specific_docs_result(
                    path=top_path,
                    title_text=top_title,
                )
            )
            and not _query_mentions_programming_language(query_lower)
            and rescue_total_hits >= top_total_hits
        ):
            return True
        if (
            _looks_like_generic_official_docs_result(
                hostname=_result_hostname(top_candidate),
                path=top_path,
                title_text=top_title,
            )
            and not _query_mentions_programming_language(query_lower)
            and rescue_total_hits >= top_total_hits
        ):
            return True
        if rescue_path_hits > top_path_hits or rescue_total_hits > top_total_hits:
            return True
    return False


def _should_request_search_answer(*,
    requested: bool,
    mode: SearchMode,
    intent: ResolvedSearchIntent,
    strategy: SearchStrategy,
    include_content: bool,
    include_domains: list[str] | None,
) -> bool:
    if not requested:
        return False
    if include_content:
        return False
    if include_domains:
        return False
    if mode in {"docs", "github", "pdf"}:
        return False
    if intent == "resource":
        return False
    if strategy in {"verify", "deep"}:
        return False
    return True


def _refined_award_result_query(query: str) -> str:
    query_lower = query.lower()
    if not _looks_like_award_result_query(query_lower):
        return query
    year_match = re.search(r"\b(20\d{2})\b", query)
    year = year_match.group(1) if year_match else ""
    award_name = ""
    if "grammy" in query_lower:
        award_name = "Grammy"
    elif "oscar" in query_lower or "academy awards" in query_lower:
        award_name = "Oscars"
    elif "golden globe" in query_lower:
        award_name = "Golden Globes"
    elif "bafta" in query_lower:
        award_name = "BAFTA"
    category = ""
    category_markers = _award_query_category_markers(query_lower)
    if category_markers:
        category = category_markers[0]

    refined_parts = [part for part in [year, award_name, "winners list", category, "full results"] if part]
    refined_query = " ".join(refined_parts).strip()
    if refined_query:
        return refined_query

    extra_terms = []
    if "winner" not in query_lower and "winners" not in query_lower:
        extra_terms.append("winners")
    if "full results" not in query_lower:
        extra_terms.append("full results")
    if "winners list" not in query_lower:
        extra_terms.append("winners list")
    if not extra_terms:
        return query
    return f"{query} {' '.join(extra_terms)}".strip()


def _should_skip_exa_rescue_for_result_event(*,
    query: str,
    mode: SearchMode,
    intent: ResolvedSearchIntent,
    result: dict[str, Any],
) -> bool:
    query_lower = query.lower()
    if not (
        _looks_like_result_event_query(query_lower)
        and (mode == "news" or intent in {"news", "status"})
    ):
        return False
    results = list(result.get("results") or [])
    if _looks_like_award_result_query(query_lower):
        return _has_strong_award_result(query=query, results=results)
    evidence = result.get("evidence") or {}
    if bool(str(result.get("answer") or "").strip()) and (
        str(evidence.get("answer_source") or "") == "result-event-extraction"
    ):
        return True
    return False


def _has_strong_award_result(*,
    query: str,
    results: list[dict[str, Any]],
) -> bool:
    query_lower = query.lower()
    trusted_result_domains = {
        "abcnews.com",
        "apnews.com",
        "bbc.com",
        "cnn.com",
        "grammy.com",
        "latimes.com",
        "npr.org",
        "nytimes.com",
        "oscars.org",
        "pbs.org",
        "reuters.com",
        "theacademy.com",
        "washingtonpost.com",
    }
    official_award_domains = _OFFICIAL_AWARD_DOMAINS
    for item in results[:5]:
        hostname = _result_hostname(item)
        registered_domain = _registered_domain(hostname)
        path = urlparse(item.get("url", "")).path.lower()
        title_text = (item.get("title") or "").lower()
        snippet_text = (item.get("snippet") or "").lower()
        content_text = (item.get("content") or "").lower()
        winner_page = _looks_like_award_winner_result(
            title_text=title_text,
            snippet_text=snippet_text,
            path=path,
        )
        weak_official_feature = (
            registered_domain in official_award_domains
            and _looks_like_weak_official_award_feature_result(
                title_text=title_text,
                snippet_text=snippet_text,
                path=path,
            )
        )
        if _looks_like_award_prediction_result(
            title_text=title_text,
            snippet_text=snippet_text,
            path=path,
        ):
            continue
        if weak_official_feature:
            continue
        if (
            not winner_page
            and _looks_like_award_recap_or_gallery_result(
                title_text=title_text,
                snippet_text=snippet_text,
                path=path,
            )
        ):
            continue
        if _looks_like_query_year_mismatch(
            query=query_lower,
            text=f"{title_text} {snippet_text} {content_text} {path}",
        ):
            continue
        if _looks_like_award_category_conflict(
            query_lower=query_lower,
            title_text=title_text,
            snippet_text=snippet_text,
            content_text=content_text,
        ):
            continue
        if _looks_like_award_brand_conflict(
            query_lower=query_lower,
            title_text=title_text,
            snippet_text=snippet_text,
            content_text=content_text,
            path=path,
        ):
            continue
        if _looks_like_generic_award_archive_result(
            title_text=title_text,
            path=path,
        ):
            continue
        if registered_domain in {"facebook.com", "instagram.com", "tiktok.com", "youtube.com"}:
            continue
        category_match = _looks_like_award_category_match(
            query_lower=query_lower,
            title_text=title_text,
            snippet_text=snippet_text,
            content_text=content_text,
            path=path,
        )
        fact_match = _looks_like_award_fact_match(
            query_lower=query_lower,
            title_text=title_text,
            snippet_text=snippet_text,
            content_text=content_text,
            path=path,
        )
        award_coverage_page = _looks_like_award_coverage_page(
            query_lower=query_lower,
            title_text=title_text,
            path=path,
        )
        prioritized_winner_page = winner_page and (
            category_match
            or _result_event_page_priority(query=query, item=item) >= 8
        )
        trusted_full_results_page = (
            category_match
            and award_coverage_page
            and registered_domain in trusted_result_domains
            and _result_event_page_priority(query=query, item=item) >= 8
        )
        official_award_page = (
            registered_domain in official_award_domains
            and not _looks_like_award_nomination_result(
                title_text=title_text,
                snippet_text=snippet_text,
                path=path,
            )
            and not _looks_like_query_year_mismatch(
                query=query_lower,
                text=f"{title_text} {snippet_text} {content_text} {path}",
            )
            and _result_event_page_priority(query=query, item=item) >= 8
        )
        if (
            prioritized_winner_page
            or (fact_match and award_coverage_page)
            or trusted_full_results_page
            or official_award_page
        ):
            return True
    return False


def _looks_like_derivative_paper_title(title_text: str) -> bool:
    derivative_markers = (
        "analysis",
        "benchmark",
        "explained",
        "interpret",
        "lets think",
        "let’s think",
        "rethinking",
        "survey",
        "thoughtology",
        "tutorial",
    )
    return any(marker in title_text for marker in derivative_markers)


def _looks_like_exact_base_paper_query(query: str) -> bool:
    query_lower = (query or "").lower()
    return "technical report" in query_lower and bool(_base_report_subject_token(query))


def _base_report_subject_token(query: str) -> str:
    compound_tokens = _paper_query_compound_tokens(query)
    if len(compound_tokens) == 1:
        return compound_tokens[0]
    for token in _query_brand_tokens(query):
        if token not in {"technical", "report"}:
            return token
    return ""


def _looks_like_variant_base_paper_result(*, query: str, title_text: str) -> bool:
    if not _looks_like_exact_base_paper_query(query):
        return False
    base_token = _base_report_subject_token(query)
    if not base_token:
        return False
    base = re.escape(base_token)
    normalized_title = re.sub(r"\s+", " ", (title_text or "").strip().lower())
    if re.match(rf"^\s*(?:\[[^\]]+\]\s*)?{base}\s+technical report\b", normalized_title):
        return False
    return re.match(
        rf"^\s*(?:\[[^\]]+\]\s*)?{base}(?:[-\s][a-z0-9]+)+\s+technical report\b",
        normalized_title,
    ) is not None


def _is_obvious_pdf_mirror_or_aggregator_result(*,
    hostname: str,
    registered_domain: str,
    path: str,
) -> bool:
    mirror_domains = {
        "docdroid.net",
        "issuu.com",
        "jsdelivr.net",
        "researchgate.net",
        "scribd.com",
        "slideshare.net",
    }
    if registered_domain in mirror_domains:
        return True
    if hostname == "cdn.jsdelivr.net":
        return True
    normalized_path = (path or "").lower()
    return "/npm/" in normalized_path and ".pdf" in normalized_path


def _should_rerank_general_results(*,
    result_profile: str,
) -> bool:
    return result_profile in {"web", "news"}


def _query_prefers_web_social_sources(query_lower: str) -> bool:
    social_markers = (
        "community",
        "facebook",
        "forum",
        "forums",
        "instagram",
        "reddit",
        "reactions",
        "rumor",
        "rumors",
        "social",
        "threads",
        "tiktok",
        "twitter",
        "weibo",
        "x.com",
        "youtube",
    )
    return any(marker in query_lower for marker in social_markers)


def _is_mainstream_news_domain(hostname: str) -> bool:
    registered_domain = _registered_domain(hostname)
    mainstream_domains = {
        "apnews.com",
        "bbc.com",
        "bloomberg.com",
        "cnn.com",
        "ft.com",
        "latimes.com",
        "nytimes.com",
        "reuters.com",
        "theguardian.com",
        "theverge.com",
        "washingtonpost.com",
        "wsj.com",
        "xinhuanet.com",
    }
    return registered_domain in mainstream_domains


def _looks_like_award_winner_result(*,
    title_text: str,
    snippet_text: str,
    path: str,
) -> bool:
    text = f"{title_text} {snippet_text} {path}"
    winner_markers = [
        "complete winners",
        "full list of winners",
        "full winners",
        "winners and nominees",
        "heres a full list",
        "here's a full list",
        "winner list",
        "winners list",
    ]
    return any(marker in text for marker in winner_markers)


def _looks_like_award_category_match(*,
    query_lower: str,
    title_text: str,
    snippet_text: str,
    content_text: str,
    path: str,
) -> bool:
    text = f"{title_text} {snippet_text} {content_text} {path}"
    category_markers = _award_query_category_markers(query_lower)
    return bool(category_markers) and any(marker in text for marker in category_markers)


def _looks_like_award_coverage_page(*,
    query_lower: str,
    title_text: str,
    path: str,
) -> bool:
    title_path = f"{title_text} {path}"
    brand_markers = _award_query_brand_markers(query_lower)
    brand_match = bool(brand_markers) and any(marker in title_path for marker in brand_markers)
    winner_markers = [
        "complete winners",
        "full list",
        "full results",
        "winner list",
        "winners list",
    ]
    if any(marker in title_path for marker in winner_markers):
        return True
    category_markers = _award_query_category_markers(query_lower)
    if bool(category_markers) and any(marker in title_path for marker in category_markers):
        return True
    official_context_markers = [
        "academy awards",
        "grammy awards",
        "golden globes",
        "/award/",
        "/awards/",
        "/ceremonies/",
        "/ceremony/",
    ]
    return brand_match and any(marker in title_path for marker in official_context_markers)


def _looks_like_generic_award_archive_result(*,
    title_text: str,
    path: str,
) -> bool:
    text = f"{title_text} {path}"
    archive_markers = [
        "academy awards search",
        "awards database",
        "awards search",
        "/awardsdatabase",
    ]
    return any(marker in text for marker in archive_markers)


def _looks_like_weak_official_award_feature_result(*,
    title_text: str,
    snippet_text: str,
    path: str,
) -> bool:
    text = f"{title_text} {snippet_text} {path}"
    weak_markers = [
        "flashback",
        "1-minute roundup",
        "minute roundup",
        "roundup",
        "throughout the decades",
        "must-watch moments",
        "artist |",
        "/artists/",
    ]
    return any(marker in text for marker in weak_markers)


def _looks_like_award_fact_match(*,
    query_lower: str,
    title_text: str,
    snippet_text: str,
    content_text: str,
    path: str,
) -> bool:
    text = f"{title_text} {snippet_text} {content_text} {path}"
    for marker in _award_query_category_markers(query_lower):
        marker_pattern = re.escape(marker)
        patterns = [
            rf"{marker_pattern}(?:\s+winner)?\s*[–—:]",
            rf"{marker_pattern}\s*\.\s*winner\s*[\.\-–—: ]",
            rf"{marker_pattern}(?:\s+winner)?(?:\s+was|\s+is|\s+goes to|\s+went to)\b",
            rf"[\"“'‘][^\"”’'\n]{{2,100}}[\"”’'‘]\s+is\s+the\s+(?:20\d{{2}}\s+)?{marker_pattern}\s+winner",
            rf"[\"“'‘][^\"”’'\n]{{2,100}}[\"”’'‘]\s+(?:won|wins)\s+{marker_pattern}",
            rf"[A-Z][A-Za-z0-9'’&.\- ]{{2,100}}\s+(?:won|wins)\s+{marker_pattern}",
            rf"[A-Z][A-Za-z0-9'’&.\- ]{{2,100}}\s+(?:won|wins)[^\n]{{0,40}}\b{marker_pattern}\b",
        ]
        if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns):
            return True
    return False


def _looks_like_award_nomination_result(*,
    title_text: str,
    snippet_text: str,
    path: str,
) -> bool:
    text = f"{title_text} {snippet_text} {path}"
    nomination_markers = [
        "nomination",
        "nominations",
        "nominee",
        "nominees",
    ]
    winner_markers = [
        "complete winners",
        "full winners",
        "winner list",
        "winners list",
        "winner",
        "winners",
    ]
    return any(marker in text for marker in nomination_markers) and not any(
        marker in text for marker in winner_markers
    )


def _looks_like_award_prediction_result(*,
    title_text: str,
    snippet_text: str,
    path: str,
) -> bool:
    text = f"{title_text} {snippet_text} {path}"
    prediction_markers = [
        "award buzz",
        "contender",
        "contenders",
        "forecast",
        "next year",
        "next-year",
        "odds",
        "prediction",
        "predictions",
        "predicts",
        "snub",
        "snubs",
        "way too early",
    ]
    return any(marker in text for marker in prediction_markers)


def _looks_like_award_recap_or_gallery_result(*,
    title_text: str,
    snippet_text: str,
    path: str,
) -> bool:
    text = f"{title_text} {snippet_text} {path}"
    weak_page_markers = [
        "gallery",
        "live blog",
        "live updates",
        "live-news",
        "live news",
        "liveblog",
        "photos",
        "recap",
        "red carpet",
        "week in",
        "winning streak",
    ]
    return any(marker in text for marker in weak_page_markers)


def _looks_like_news_article_result(item: dict[str, Any]) -> bool:
    path = urlparse(item.get("url", "")).path.lower()
    return any(
        marker in path
        for marker in ("/news/", "/story/", "/stories/", "/article/", "/articles/", "/202")
    )


def _is_obvious_web_aggregator(registered_domain: str) -> bool:
    return registered_domain in {
        "linkedin.com",
        "medium.com",
        "quora.com",
        "reddit.com",
        "researchgate.net",
        "stackoverflow.com",
    }


def _looks_like_local_life_guide_result(*,
    url: str,
    hostname: str,
    title_text: str,
    snippet_text: str,
) -> bool:
    registered_domain = _registered_domain(hostname)
    path = urlparse(url).path.lower()
    guide_markers = (
        "攻略",
        "赏花",
        "踏青",
        "景点",
        "路线",
        "门票",
        "游玩",
        "travel guide",
        "things to do",
    )
    guide_shape = any(marker in path for marker in ("/tour/", "/travel/", "/guide", "/flowers", "/trip/"))
    guide_text = f"{title_text} {snippet_text}"
    if registered_domain in {"bendibao.com", "ctrip.com", "trip.com", "mafengwo.cn", "qyer.com"}:
        return guide_shape or any(marker in guide_text for marker in guide_markers)
    return guide_shape and any(marker in guide_text for marker in guide_markers)


def _looks_like_canonical_local_life_guide_result(*,
    url: str,
    hostname: str,
) -> bool:
    registered_domain = _registered_domain(hostname)
    path = urlparse(url).path.lower()
    if registered_domain == "bendibao.com":
        return "/tour/" in path or "/flowers" in path
    if registered_domain in {"trip.com", "ctrip.com"}:
        return any(marker in path for marker in ("/travel-guide/", "/travel/"))
    return any(marker in path for marker in ("/tour/", "/travel/", "/guide", "/flowers"))


def _is_obvious_local_life_repost_domain(registered_domain: str) -> bool:
    return registered_domain in {
        "163.com",
        "facebook.com",
        "ifeng.com",
        "qq.com",
        "sina.cn",
        "sohu.com",
        "weibo.com",
    }


def _looks_like_tutorial_community_result(*,
    hostname: str,
    registered_domain: str,
    path: str,
) -> bool:
    return (
        registered_domain == "stackoverflow.com"
        or (registered_domain == "github.com" and any(marker in path for marker in ("/issues/", "/discussions/")))
        or _is_obvious_official_community_result(hostname=hostname, path=path)
    )


def _looks_like_brand_aligned_tutorial_result(*,
    hostname: str,
    registered_domain: str,
    path: str,
    title_text: str,
    snippet_text: str,
    query_tokens: list[str],
    path_precision_hits: int,
    exact_total_hits: int,
) -> bool:
    brand_aligned = _registered_domain_label_matches(
        registered_domain=registered_domain,
        query_tokens=query_tokens,
    ) or any(token in hostname for token in query_tokens)
    if not brand_aligned:
        return False
    docs_path = any(marker in path for marker in ("/docs/", "/guide", "/api/", "/writing-tests", "/running-tests"))
    tutorial_text = f"{title_text} {snippet_text}"
    return (
        docs_path
        or path_precision_hits >= 1
        or exact_total_hits > 0
        or "tutorial" in tutorial_text
        or "strict mode" in tutorial_text
        or "hydration" in tutorial_text
    )


def _is_obvious_tutorial_blog_domain(registered_domain: str) -> bool:
    return registered_domain in {
        "checklyhq.com",
        "loadmill.com",
        "medium.com",
        "substack.com",
        "testgrid.io",
        "timdeschryver.dev",
        "youtube.com",
    }


def _is_exa_social_candidate(item: dict[str, Any]) -> bool:
    hostname = _result_hostname(item)
    if hostname in {"x.com", "twitter.com"}:
        return True
    if hostname in {
        "xcancel.com",
        "threadreaderapp.com",
        "nitter.net",
        "fxtwitter.com",
        "fixupx.com",
        "vxtwitter.com",
        "techtwitter.com",
        "getdaytrends.com",
    }:
        return True
    combined = " ".join(
        [
            str(item.get("title") or ""),
            str(item.get("snippet") or ""),
            str(item.get("content") or ""),
            str(item.get("url") or ""),
        ]
    ).lower()
    return "twitter.com/" in combined or "x.com/" in combined


def _is_retryable_social_gateway_error(exc: Exception) -> bool:
    if _is_retryable_transient_error(exc):
        return True
    detail_text = str(exc).lower()
    return any(
        marker in detail_text
        for marker in (
            "timed out",
            "timeout",
            "tls connect error",
            "curl: (35)",
            "connection reset",
            "temporarily unavailable",
            "bad gateway",
            "eof",
        )
    )


def _is_retryable_transient_error(exc: Exception) -> bool:
    return isinstance(exc, MySearchHTTPError) and exc.status_code in {429, 502, 503, 504}


def _is_hcaptcha_artifact_paragraph(p_lower: str) -> bool:
    if not p_lower:
        return True
    if p_lower in _HCAPTCHA_LANGUAGES:
        return True
    if p_lower in {"hcaptcha", "ask ai", "en", "verify", "i am human"}:
        return True
    if (
        "select in order to trigger" in p_lower
        or "accessibility cookie" in p_lower
        or "hcaptcha" in p_lower
        or p_lower.startswith("please try again")
    ):
        return True
    if p_lower.startswith("### filters") or p_lower.startswith("#### tags"):
        return True
    # Short standalone tokens are widget chrome, not prose.
    return len(p_lower) <= 24


def _should_rerank_resource_results(*,
    mode: SearchMode,
    intent: ResolvedSearchIntent,
) -> bool:
    explicit_resource_mode = mode in {"docs", "github", "pdf"}
    if intent == "tutorial" and not explicit_resource_mode:
        return False
    return explicit_resource_mode or intent == "resource"


def _looks_like_generic_changelog_index_result(*, hostname: str, path: str) -> bool:
    normalized_path = path.rstrip("/") or "/"
    if hostname.startswith("github.com"):
        return False
    return normalized_path in {"/blog", "/changelog", "/releases", "/release-notes"}


def _paper_query_subject_tokens(*,
    query: str,
    query_tokens: list[str],
    precision_tokens: list[str],
) -> list[str]:
    subject_tokens: list[str] = []
    seen: set[str] = set()
    compound_tokens = _paper_query_compound_tokens(query)
    raw_query_tokens = [
        cleaned
        for raw_token in re.findall(r"[a-z0-9][a-z0-9._/-]{1,}", query.lower())
        for cleaned in [raw_token.strip("._/-")]
        if _is_mixed_alnum_short_token(cleaned)
    ]
    for token in [*query_tokens, *compound_tokens, *raw_query_tokens, *precision_tokens]:
        cleaned = token.strip().lower()
        if cleaned in seen or cleaned in {"paper", "pdf"}:
            continue
        if len(cleaned) < 2:
            continue
        seen.add(cleaned)
        subject_tokens.append(cleaned)
    return subject_tokens[:3]


def _paper_query_compound_tokens(query: str) -> list[str]:
    raw_tokens = [token for token in re.findall(r"[a-z0-9]+", query.lower()) if token]
    compounds: list[str] = []
    seen: set[str] = set()
    for index, token in enumerate(raw_tokens):
        compact = re.sub(r"[^a-z0-9]+", "", token)
        if compact and any(ch.isalpha() for ch in compact) and any(ch.isdigit() for ch in compact):
            if compact not in seen:
                seen.add(compact)
                compounds.append(compact)
        if index + 1 < len(raw_tokens) and raw_tokens[index].isalpha() and raw_tokens[index + 1].isdigit():
            combined = f"{raw_tokens[index]}{raw_tokens[index + 1]}"
            if combined not in seen:
                seen.add(combined)
                compounds.append(combined)
    return compounds


def _paper_text_matches_compound_token(text: str, compound_token: str) -> bool:
    normalized_text = re.sub(r"[^a-z0-9]+", "", (text or "").lower())
    if compound_token in normalized_text:
        return True
    letters = "".join(ch for ch in compound_token if ch.isalpha())
    digits = "".join(ch for ch in compound_token if ch.isdigit())
    if not letters or not digits:
        return False
    return re.search(rf"{re.escape(letters)}[\s\-_:/()]*{re.escape(digits)}", (text or "").lower()) is not None


def _looks_like_primary_named_paper_result(*,
    title_text: str,
    query_tokens: list[str],
) -> bool:
    if not query_tokens:
        return False
    if len(query_tokens) == 1:
        token = re.escape(query_tokens[0])
        return re.match(rf"^\s*(?:\[[^\]]+\]\s*)?{token}(?:\b|[\s:()\-])", title_text) is not None
    subject_pattern = r"[\s\-_]*".join(re.escape(token) for token in query_tokens[:2])
    return re.match(rf"^\s*{subject_pattern}\s*:", title_text) is not None


def _is_probably_official_resource_result(*,
    mode: SearchMode,
    hostname: str,
    include_match: bool,
    registered_domain_label_match: bool,
    host_brand_match: bool,
    title_brand_match: bool,
    docs_shape_match: bool,
    non_third_party: bool,
    official_query: bool,
) -> bool:
    if include_match:
        return True
    if mode in {"github", "pdf"}:
        return True
    if not non_third_party:
        return False
    if official_query and registered_domain_label_match:
        return True
    if not docs_shape_match:
        return False
    official_host_surface = any(
        part in {"api", "developer", "developers", "docs", "help", "platform", "reference", "support"}
        for part in hostname.split(".")
        if part
    )
    # 子域名品牌匹配：如 fastapi.tiangolo.com，品牌在子域名中
    brand_in_subdomain = host_brand_match and not registered_domain_label_match
    return registered_domain_label_match or (host_brand_match and official_host_surface) or (
        title_brand_match and official_host_surface
    ) or (brand_in_subdomain and title_brand_match)


def _is_social_unavailable_result(result: dict[str, Any] | None) -> bool:
    if not result:
        return False
    provider = str(result.get("provider") or "")
    if provider in {"social_unavailable", "social_gateway_unavailable"}:
        return True
    fallback = result.get("fallback") or {}
    if isinstance(fallback, dict) and str(fallback.get("to") or "") in {
        "social_unavailable",
        "social_gateway_unavailable",
    }:
        return True
    return False


def _looks_like_locale_prefixed_path(path: str) -> bool:
    parts = [item for item in (path or "").split("/") if item]
    if len(parts) < 2:
        return False
    first = parts[0].strip().lower()
    return bool(re.fullmatch(r"[a-z]{2}(?:-[a-z0-9]{2,8}){0,2}", first))


def _looks_like_locale_prefixed_hostname(hostname: str) -> bool:
    labels = [item for item in (hostname or "").split(".") if item]
    if len(labels) < 3:
        return False
    first = labels[0].strip().lower()
    return bool(re.fullmatch(r"[a-z]{2}(?:-[a-z0-9]{2,8}){0,2}", first))


def _looks_like_generic_arxiv_subject_title(title_text: str) -> bool:
    cleaned = re.sub(r"\s+", " ", (title_text or "").strip())
    if not cleaned:
        return True
    if re.fullmatch(r"[A-Za-z][A-Za-z &]+ > [A-Za-z][A-Za-z ,&()/-]+", cleaned):
        return True
    lowered = cleaned.lower()
    return bool(
        re.fullmatch(
            r"arxiv:\d{4}\.\d{4,5}(?:v\d+)?(?: \[[a-z.\-]+\])?(?: \d{1,2} [a-z]{3} \d{4})?",
            lowered,
        )
    )


def _result_hostname(item: dict[str, Any]) -> str:
    return postprocess._result_hostname(item)


def _clean_hostname(hostname: str) -> str:
    return postprocess._clean_hostname(hostname)


def _registered_domain(hostname: str) -> str:
    return postprocess._registered_domain(hostname)


def _registered_domain_label_matches(*, registered_domain: str, query_tokens: list[str]) -> bool:
    labels = [item for item in _clean_hostname(registered_domain).split(".") if item]
    return any(
        label == token or label.startswith(f"{token}-") or label.startswith(f"{token}_")
        for token in query_tokens
        for label in labels
    )


def _query_brand_tokens(query: str) -> list[str]:
    stopwords = {
        "a",
        "an",
        "and",
        "api",
        "apis",
        "best",
        "changelog",
        "compare",
        "comparison",
        "developer",
        "developers",
        "docs",
        "documentation",
        "for",
        "github",
        "guide",
        "how",
        "manual",
        "pricing",
        "reference",
        "release",
        "releases",
        "sdk",
        "status",
        "the",
        "tutorial",
        "vs",
        "with",
        "价格",
        "发布",
        "对比",
        "接口",
        "教程",
        "文档",
        "更新日志",
    }
    tokens: list[str] = []
    for token in re.findall(r"[a-z0-9][a-z0-9._-]{1,}", query.lower()):
        if token in stopwords or token.isdigit():
            continue
        if len(token) < 3:
            continue
        tokens.append(token)
    return tokens


def _query_precision_tokens(query: str) -> list[str]:
    stopwords = {
        "a",
        "an",
        "and",
        "best",
        "docs",
        "documentation",
        "for",
        "guide",
        "how",
        "official",
        "the",
        "with",
        "官网",
        "官方",
    }
    precision_tokens: list[str] = []
    seen: set[str] = set()
    raw_tokens = re.findall(r"[a-z0-9][a-z0-9._/-]{1,}", query.lower())
    for raw_token in raw_tokens:
        cleaned = raw_token.strip("._/-")
        candidates = {cleaned}
        compact = re.sub(r"[^a-z0-9]+", "", cleaned)
        if compact:
            candidates.add(compact)
        candidates.update(
            token
            for token in re.split(r"[^a-z0-9]+", cleaned)
            if token
        )
        for candidate in candidates:
            if candidate in stopwords or candidate.isdigit():
                continue
            if len(candidate) < 3:
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            precision_tokens.append(candidate)
    return precision_tokens


def _is_mixed_alnum_short_token(token: str) -> bool:
    return (
        len(token) == 2
        and any(ch.isalpha() for ch in token)
        and any(ch.isdigit() for ch in token)
    )


def _query_precision_hit_counts(*,
    hostname: str,
    path: str,
    title_text: str,
    query_tokens: list[str],
) -> tuple[int, int]:
    if not query_tokens:
        return 0, 0
    path_text = f"{hostname} {path}"
    full_text = f"{path_text} {title_text}"
    path_matches = sum(1 for token in query_tokens if token in path_text)
    total_matches = sum(1 for token in query_tokens if token in full_text)
    return min(path_matches, 4), min(total_matches, 6)


def _query_precision_hit_counts_with_body(*,
    hostname: str,
    path: str,
    title_text: str,
    body_text: str,
    query_tokens: list[str],
) -> tuple[int, int]:
    if not query_tokens:
        return 0, 0
    path_text = f"{hostname} {path}".lower()
    full_text = f"{path_text} {title_text} {body_text}".lower()
    path_matches = sum(1 for token in query_tokens if token in path_text)
    total_matches = sum(1 for token in query_tokens if token in full_text)
    return min(path_matches, 4), min(total_matches, 8)


def _query_exact_identifier_tokens(query: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    raw_tokens = re.findall(r"[a-z0-9][a-z0-9._/-]{2,}", query.lower())
    for raw_token in raw_tokens:
        if not any(marker in raw_token for marker in (".", "/", "_", "-")):
            continue
        compact = re.sub(r"[^a-z0-9]+", "", raw_token.strip("._/-"))
        if len(compact) < 4 or compact in seen:
            continue
        seen.add(compact)
        tokens.append(compact)
    return tokens


def _query_topic_specific_tokens(query: str) -> list[str]:
    generic_tokens = {
        "api",
        "apis",
        "docs",
        "documentation",
        "guide",
        "guides",
        "official",
        "openai",
        "paper",
        "pdf",
        "price",
        "pricing",
        "report",
        "response",
        "responses",
    }
    return [
        token
        for token in _query_precision_tokens(query)
        if token not in generic_tokens
    ]


def _query_exact_identifier_hit_counts(*,
    path: str,
    title_text: str,
    query_tokens: list[str],
) -> tuple[int, int]:
    if not query_tokens:
        return 0, 0
    path_segments = {
        token
        for token in re.split(r"[^a-z0-9]+", path.lower())
        if len(token) >= 3
    }
    title_segments = {
        token
        for token in re.split(r"[^a-z0-9]+", title_text.lower())
        if len(token) >= 3
    }
    path_matches = sum(1 for token in query_tokens if token in path_segments)
    total_matches = sum(1 for token in query_tokens if token in path_segments or token in title_segments)
    return min(path_matches, 3), min(total_matches, 3)


def _looks_like_official_docs_query(query_lower: str) -> bool:
    if (
        _looks_like_pricing_query(query_lower)
        or _looks_like_changelog_query(query_lower)
        or _looks_like_status_query(query_lower)
    ):
        return False
    return _looks_like_docs_query(query_lower) or _looks_like_api_docs_topic_query(query_lower)


def _looks_like_pricing_query(query_lower: str) -> bool:
    keywords = [
        "pricing",
        "price",
        "多少钱",
        "价格",
        "售价",
        "buy",
        "购买",
    ]
    return any(keyword in query_lower for keyword in keywords)


def _looks_like_pricing_result(*, url: str, hostname: str, title_text: str) -> bool:
    path = urlparse(url).path.lower()
    if hostname.startswith("shop.") and "/product/" not in path:
        return True
    pricing_markers = (
        "/pricing",
        "/price",
        "/plans",
        "/plan",
        "/shop/buy",
        "/buy-",
    )
    return any(marker in path for marker in pricing_markers) or any(
        marker in title_text for marker in ("pricing", "price", "plan")
    )


def _looks_like_canonical_pricing_result(*, hostname: str, path: str) -> bool:
    normalized_path = path.rstrip("/")
    path_segments = [segment for segment in normalized_path.split("/") if segment]
    if any(segment in {"career", "careers", "job", "jobs"} for segment in path_segments):
        return False
    if hostname == "openai.com" and normalized_path in {"/business/chatgpt-pricing", "/chatgpt/pricing"}:
        return False
    if ("/shop/buy" in normalized_path or "/buy-" in normalized_path) and len(path_segments) <= 3:
        return True
    if hostname.startswith("shop.") and "/product/" not in normalized_path and len(path_segments) <= 3:
        return True
    if "pricing" in normalized_path and "/docs/" not in normalized_path and not hostname.startswith("developers."):
        return True
    return False


def _looks_like_generic_official_landing_result(*,
    hostname: str,
    path: str,
    title_text: str,
) -> bool:
    normalized_path = path.rstrip("/") or "/"
    generic_paths = {
        "/",
        "/api",
        "/api/docs",
        "/api/docs/guides",
        "/developers",
        "/docs",
        "/documentation",
        "/guides",
        "/learn",
        "/reference",
    }
    if normalized_path in generic_paths:
        return True
    if hostname == "openai.com" and normalized_path in {"/pricing", "/business/chatgpt-pricing"}:
        return True
    generic_titles = {"docs", "documentation", "developer docs", "guides", "reference"}
    return title_text.strip() in generic_titles


def _query_mentions_programming_language(query_lower: str) -> bool:
    terms = set(re.findall(r"[a-z0-9#+.-]+", query_lower))
    language_markers = (
        "c#",
        "csharp",
        "go",
        "java",
        "javascript",
        "node",
        "php",
        "python",
        "ruby",
        "sdk",
        "typescript",
    )
    return any(marker in query_lower and marker in terms for marker in language_markers)


def _looks_like_language_specific_sdk_reference_result(*,
    hostname: str,
    path: str,
    title_text: str,
) -> bool:
    if "api/reference" not in path and "api reference" not in title_text:
        return False
    language_markers = (
        "/csharp/",
        "/go/",
        "/java/",
        "/javascript/",
        "/node/",
        "/php/",
        "/python/",
        "/ruby/",
        "/typescript/",
    )
    if any(marker in path for marker in language_markers):
        return True
    return any(
        marker in title_text
        for marker in ("python", "ruby", "go", "typescript", "javascript", "java", "php", "c#", "csharp", "node")
    ) and hostname.endswith("openai.com")


def _looks_like_language_specific_docs_result(*,
    path: str,
    title_text: str,
) -> bool:
    language_markers = (
        "c#",
        "csharp",
        "go",
        "java",
        "javascript",
        "node",
        "php",
        "python",
        "ruby",
        "typescript",
    )
    normalized_path = path.lower()
    if any(f"/{marker}/" in normalized_path for marker in language_markers if marker not in {"c#"}):
        return True
    return any(marker in title_text for marker in language_markers)


def _looks_like_generic_official_docs_result(*,
    hostname: str,
    path: str,
    title_text: str,
) -> bool:
    normalized_path = path.rstrip("/")
    generic_doc_paths = {
        "/docs/intro",
        "/docs/locators",
        "/docs/other-locators",
        "/docs/running-tests",
        "/docs/writing-tests",
    }
    if hostname == "playwright.dev" and normalized_path in generic_doc_paths:
        return True
    return False


def _looks_like_changelog_result(*, url: str, hostname: str, title_text: str) -> bool:
    path = urlparse(url).path.lower()
    changelog_markers = (
        "/blog/",
        "/changelog",
        "/release-notes",
        "/releases",
        "/updating",
        "/upgrading",
        "/version-",
    )
    title_markers = (
        "announcing",
        "changelog",
        "release notes",
        "what's new",
        "whats new",
    )
    return any(marker in path for marker in changelog_markers) or any(
        marker in title_text for marker in title_markers
    )


def _looks_like_canonical_changelog_result(*,
    url: str,
    hostname: str,
    title_text: str,
    precision_tokens: list[str],
) -> bool:
    path = urlparse(url).path.lower().rstrip("/")
    if not _looks_like_changelog_result(url=url, hostname=hostname, title_text=title_text):
        return False
    high_signal_path = any(
        marker in path
        for marker in ("/blog/", "/release-notes", "/releases")
    )
    if not high_signal_path:
        return False
    if not precision_tokens:
        return True
    path_hits, total_hits = _query_precision_hit_counts(
        hostname=hostname,
        path=path,
        title_text=title_text,
        query_tokens=precision_tokens,
    )
    return path_hits > 0 or total_hits > 0


def _is_obvious_official_community_result(*, hostname: str, path: str) -> bool:
    labels = [label for label in hostname.split(".") if label]
    community_labels = {"community", "forum", "forums", "discuss", "discussion"}
    if any(label in community_labels for label in labels[:2]):
        return True
    normalized_path = path.rstrip("/")
    return normalized_path.startswith("/t/") or normalized_path.startswith("/c/")


def _looks_like_debugging_result(*,
    hostname: str,
    registered_domain: str,
    path: str,
    title_text: str,
    snippet_text: str,
) -> bool:
    text = f"{title_text} {snippet_text} {path}"
    if registered_domain == "github.com" and any(marker in path for marker in ("/issues/", "/discussions/")):
        return True
    debugging_markers = (
        "bug",
        "cannot",
        "can't",
        "debug",
        "error",
        "failed",
        "failing",
        "fix",
        "issue",
        "strict mode",
        "troubleshoot",
        "troubleshooting",
        "violation",
        "workaround",
        "报错",
        "排查",
        "修复",
        "错误",
    )
    if any(marker in text for marker in debugging_markers):
        return True
    return hostname.endswith("stackoverflow.com")


def _looks_like_generic_debugging_docs_result(*,
    hostname: str,
    path: str,
    title_text: str,
) -> bool:
    normalized_path = path.rstrip("/")
    generic_paths = {
        "/docs/writing-tests",
        "/docs/running-tests",
        "/docs/test-fixtures",
        "/docs/intro",
        "/docs/test-ui-mode",
    }
    if normalized_path in generic_paths:
        return True
    generic_titles = (
        "writing tests",
        "running and debugging tests",
        "running tests",
        "test ui mode",
        "fixtures",
    )
    return any(title_text.strip() == candidate for candidate in generic_titles) and hostname.endswith("playwright.dev")


def _looks_like_status_result(*, url: str, hostname: str, title_text: str) -> bool:
    path = urlparse(url).path.lower()
    if _looks_like_brand_status_domain(hostname):
        return True
    status_markers = (
        "/status",
        "/incidents",
        "/incident",
        "/uptime",
        "/outage",
    )
    return any(marker in path for marker in status_markers) or any(
        marker in title_text for marker in ("status", "incident", "outage", "uptime")
    )


def _looks_like_canonical_status_result(*, hostname: str, path: str) -> bool:
    normalized_path = path.rstrip("/")
    if _looks_like_brand_status_domain(hostname):
        return normalized_path in {"", "/", "/history", "/incidents"} or normalized_path.startswith("/incidents")
    if hostname.startswith("status.") or ".statuspage." in hostname:
        return True
    return normalized_path.startswith("/incidents") or normalized_path.startswith("/incident")


def _looks_like_brand_status_domain(hostname: str) -> bool:
    cleaned = _clean_hostname(hostname)
    if not cleaned:
        return False
    if cleaned.startswith("status.") or ".statuspage." in cleaned:
        return True
    registered_domain = _registered_domain(cleaned)
    return registered_domain.endswith("status.com") or registered_domain.endswith("status.io")


def _looks_like_software_version_reference_result(*,
    url: str,
    hostname: str,
    title_text: str,
    snippet_text: str,
) -> bool:
    path = urlparse(url).path.lower()
    text = f"{title_text} {snippet_text} {path}"
    version_markers = (
        "latest version",
        "latest release",
        "latest stable",
        "stable version",
        "stable release",
        "supported versions",
        "version support",
        "current stable",
    )
    path_markers = (
        "/download",
        "/downloads",
        "/release",
        "/releases",
        "/release-notes",
        "/versions",
        "/whats-new",
        "/what-s-new",
    )
    title_markers = (
        "download",
        "release notes",
        "stable",
        "support",
        "version",
        "what's new",
        "whats new",
    )
    return (
        any(marker in text for marker in version_markers)
        or any(marker in path for marker in path_markers)
        or any(marker in title_text for marker in title_markers)
        or _looks_like_changelog_result(url=url, hostname=hostname, title_text=title_text)
    )


def _looks_like_canonical_software_version_result(*,
    hostname: str,
    path: str,
    title_text: str,
) -> bool:
    normalized_path = path.rstrip("/")
    if hostname in {"python.org", "www.python.org"} and normalized_path.startswith("/downloads"):
        return True
    if hostname == "devguide.python.org" and normalized_path.startswith("/versions"):
        return True
    canonical_path_markers = (
        "/download",
        "/downloads",
        "/release-notes",
        "/releases",
        "/versions",
        "/whats-new",
        "/what-s-new",
    )
    if any(marker in normalized_path for marker in canonical_path_markers):
        return True
    return any(
        marker in title_text
        for marker in (
            "latest version",
            "release notes",
            "supported versions",
            "version support",
            "what's new",
            "whats new",
        )
    )


def _looks_like_resource_result(*,
    url: str,
    hostname: str,
    title_text: str,
    mode: SearchMode,
) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    hostname_labels = [item for item in hostname.split(".") if item]
    docs_keywords = (
        "/advanced/",
        "/api",
        "/changelog",
        "/docs",
        "/documentation",
        "/getting-started/",
        "/guide",
        "/guides",
        "/learn",
        "/manual",
        "/pricing",
        "/readme",
        "/reference",
        "/references",
        "/tutorial",
    )
    title_keywords = (
        "api reference",
        "changelog",
        "docs",
        "documentation",
        "guide",
        "manual",
        "pricing",
        "readme",
        "reference",
    )
    hostname_keywords = {
        "api",
        "developer",
        "developers",
        "docs",
        "help",
        "platform",
        "reference",
        "support",
    }
    if mode == "github" and hostname in {"github.com", "raw.githubusercontent.com"}:
        return True
    if mode == "pdf" and _looks_like_pdf_url(url):
        return True
    return (
        any(part in hostname_keywords for part in hostname_labels)
        or any(keyword in path for keyword in docs_keywords)
        or any(keyword in title_text for keyword in title_keywords)
    )


def _looks_like_pdf_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".pdf")


def _is_obvious_third_party_resource(*,
    hostname: str,
    registered_domain: str,
    mode: SearchMode,
) -> bool:
    if mode == "github" and hostname in {"github.com", "raw.githubusercontent.com"}:
        return False
    third_party_domains = {
        "arxiv.org",
        "dev.to",
        "facebook.com",
        "hashnode.dev",
        "hashnode.com",
        "inference.net",
        "linkedin.com",
        "medium.com",
        "news.ycombinator.com",
        "quora.com",
        "reddit.com",
        "researchgate.net",
        "stackexchange.com",
        "stackoverflow.com",
        "substack.com",
        "towardsdatascience.com",
        "twitter.com",
        "x.com",
        "youtube.com",
        "youtu.be",
    }
    return registered_domain in third_party_domains


def _should_use_social_identity_diversity(*,
    mode: SearchMode,
    intent: ResolvedSearchIntent,
    source_domains: list[str],
    social_identity_count: int,
) -> bool:
    if social_identity_count < 2:
        return False
    if mode != "social" and intent != "social":
        return False
    if not source_domains:
        return True
    return all(domain in {"x.com", "twitter.com"} for domain in source_domains)


def _looks_like_provider_limit_error(error_text: str) -> bool:
    lowered = " ".join(error_text.lower().split())
    return any(
        token in lowered
        for token in (
            "http 429",
            "http 402",
            "http 432",
            "rate limit",
            "too many requests",
            "quota_exhausted",
            "quota exhausted",
            "insufficient_quota",
            "insufficient quota",
            "credits limit",
            "credits exhausted",
            "credit limit",
            "exceeded your credits",
            "usage limit",
            "plan's set usage limit",
            "plan limit",
        )
    )


def _looks_like_news_query(query_lower: str) -> bool:
    if _looks_like_result_event_query(query_lower):
        return True
    # 中文关键词：直接 substring 匹配
    cn_keywords = ["刚刚", "最新", "新闻", "动态"]
    if any(kw in query_lower for kw in cn_keywords):
        return True
    # 英文关键词：排除常见技术搭配的误判
    # "breaking changes" / "latest version" 等不是新闻查询
    tech_negatives = [
        "breaking change", "breaking update",
        "latest version", "latest release", "latest docs",
        "latest commit", "latest tag",
        "latest stable", "stable version", "stable release",
        "current version", "current stable",
        "newest version", "newest release",
    ]
    # An explicit hard-news marker overrides the software-version whitelist, so
    # entertainment/event queries like "Taylor Swift newest release news today"
    # are not misclassified as non-news. Software-version queries such as
    # "latest stable version of Python" carry no such marker, so the Loop 4 fix
    # is preserved.
    explicit_news = bool(re.search(r"\bnews\b", query_lower)) or "breaking news" in query_lower
    if not explicit_news and any(neg in query_lower for neg in tech_negatives):
        return False
    en_keywords = [
        "latest",
        "breaking",
        "news",
        "today",
        "this week",
        "box office",
        "opening weekend",
        "rumor",
        "rumors",
    ]
    return any(keyword in query_lower for keyword in en_keywords)


def _looks_like_software_version_query(query_lower: str) -> bool:
    version_markers = [
        "latest version",
        "latest release",
        "latest stable",
        "stable version",
        "stable release",
        "current version",
        "current stable",
        "newest version",
        "newest release",
    ]
    if not any(marker in query_lower for marker in version_markers):
        return False
    if _query_mentions_programming_language(query_lower):
        return True
    software_terms = [
        "cli",
        "database",
        "framework",
        "library",
        "package",
        "runtime",
        "sdk",
    ]
    return any(term in query_lower for term in software_terms)


def _looks_like_award_result_query(query_lower: str) -> bool:
    keywords = [
        "academy awards",
        "album of the year",
        "aoty",
        "best actor",
        "best actress",
        "best picture",
        "emmy",
        "emmys",
        "golden globe",
        "golden globes",
        "grammy",
        "grammys",
        "oscar",
        "oscars",
        "winner",
        "winners",
        "won",
        "获奖",
        "最佳专辑",
        "最佳影片",
        "最佳男主角",
        "最佳女主角",
        "最佳电影",
        "最佳剧集",
        "最佳歌曲",
    ]
    return any(keyword in query_lower for keyword in keywords)


def _award_query_category_markers(query_lower: str) -> list[str]:
    markers: list[str] = []
    if "best picture" in query_lower or "最佳影片" in query_lower or "最佳电影" in query_lower:
        markers.extend(["best picture", "最佳影片", "最佳电影"])
    if "best actor" in query_lower or "最佳男主角" in query_lower:
        markers.extend(["best actor", "actor in a leading role", "最佳男主角"])
    if "best actress" in query_lower or "最佳女主角" in query_lower:
        markers.extend(["best actress", "actress in a leading role", "最佳女主角"])
    if any(token in query_lower for token in ("album of the year", "aoty", "最佳专辑")):
        markers.extend(["album of the year", "aoty", "最佳专辑"])
    if any(token in query_lower for token in ("record of the year", "最佳歌曲")):
        markers.extend(["record of the year", "最佳歌曲"])
    return markers


def _award_query_competing_category_markers(query_lower: str) -> list[str]:
    markers: list[str] = []
    if any(token in query_lower for token in ("album of the year", "aoty", "最佳专辑")):
        markers.extend(["record of the year", "song of the year", "best new artist"])
    if any(token in query_lower for token in ("record of the year", "最佳歌曲")):
        markers.extend(["album of the year", "song of the year", "best new artist"])
    if "best picture" in query_lower or "最佳影片" in query_lower or "最佳电影" in query_lower:
        markers.extend(["best actor", "best actress", "supporting actor", "supporting actress"])
    return markers


def _looks_like_award_category_conflict(*,
    query_lower: str,
    title_text: str,
    snippet_text: str,
    content_text: str,
) -> bool:
    category_markers = _award_query_category_markers(query_lower)
    if not category_markers:
        return False
    body_text = f"{snippet_text} {content_text}".lower()
    if any(marker in body_text for marker in category_markers):
        return False
    if not any(marker in title_text.lower() for marker in category_markers):
        return False
    competing_markers = _award_query_competing_category_markers(query_lower)
    return any(marker in body_text for marker in competing_markers)


def _award_query_brand_markers(query_lower: str) -> list[str]:
    if "grammy" in query_lower:
        return ["grammy", "grammys", "grammy awards"]
    if "oscar" in query_lower or "academy awards" in query_lower:
        return ["oscar", "oscars", "academy awards", "academy award"]
    if "golden globe" in query_lower:
        return ["golden globe", "golden globes"]
    if "bafta" in query_lower:
        return ["bafta"]
    return []


def _looks_like_award_brand_conflict(*,
    query_lower: str,
    title_text: str,
    snippet_text: str,
    content_text: str,
    path: str,
) -> bool:
    brand_markers = _award_query_brand_markers(query_lower)
    if not brand_markers:
        return False
    text = f"{title_text} {snippet_text} {content_text} {path}"
    if any(marker in text for marker in brand_markers):
        return False
    if "grammy" in query_lower:
        competing_markers = [
            "academy awards",
            "american music awards",
            "bafta",
            "billboard music awards",
            "brit awards",
            "emmy",
            "glaad",
            "golden globe",
            "golden globes",
            "hall of fame gala",
            "iheartradio",
            "juno",
            "mtv",
            "oscars",
            "platino",
            "sxsw",
            "vma",
        ]
    elif "oscar" in query_lower or "academy awards" in query_lower:
        competing_markers = [
            "bafta",
            "emmy",
            "glaad",
            "golden globe",
            "golden globes",
            "grammy",
            "grammys",
            "iheartradio",
            "mtv",
            "platino",
            "sxsw",
            "vma",
        ]
    else:
        competing_markers = [
            "academy awards",
            "bafta",
            "emmy",
            "glaad",
            "golden globe",
            "golden globes",
            "grammy",
            "grammys",
            "iheartradio",
            "mtv",
            "oscars",
            "platino",
            "sxsw",
            "vma",
        ]
    return any(marker in text for marker in competing_markers)


def _looks_like_box_office_query(query_lower: str) -> bool:
    keywords = [
        "box office",
        "highest grossing",
        "opening weekend",
        "票房",
        "首周末",
        "开画",
    ]
    return any(keyword in query_lower for keyword in keywords)


def _looks_like_result_event_query(query_lower: str) -> bool:
    return _looks_like_award_result_query(query_lower) or _looks_like_box_office_query(query_lower)


def _looks_like_gossip_query(query_lower: str) -> bool:
    keywords = [
        "celebrity",
        "breakup",
        "breakups",
        "dating",
        "divorce",
        "rumor",
        "rumors",
        "八卦",
        "分手",
        "离婚",
        "恋情",
        "绯闻",
    ]
    return any(keyword in query_lower for keyword in keywords)


def _is_entertainment_gossip_domain(registered_domain: str) -> bool:
    return registered_domain in {
        "eonline.com",
        "justjared.com",
        "pagesix.com",
        "people.com",
        "radaronline.com",
        "tmz.com",
        "usmagazine.com",
    }


def _looks_like_gossip_result(*,
    title_text: str,
    snippet_text: str,
    path: str,
) -> bool:
    text = f"{title_text} {snippet_text} {path}"
    keywords = [
        "breakup",
        "breakups",
        "dating",
        "divorce",
        "rumor",
        "rumors",
        "split",
        "splits",
        "关系",
        "分手",
        "离婚",
        "恋情",
        "绯闻",
    ]
    return any(keyword in text for keyword in keywords)


def _looks_like_status_query(query_lower: str) -> bool:
    if _looks_like_changelog_query(query_lower):
        return False
    keywords = [
        "status",
        "incident",
        "outage",
        "roadmap",
        "版本",
        "进展",
        "现状",
    ]
    return any(keyword in query_lower for keyword in keywords)


def _looks_like_comparison_query(query_lower: str) -> bool:
    keywords = [
        " vs ",
        "versus",
        "compare",
        "comparison",
        "pros and cons",
        "pros cons",
        "对比",
        "比较",
        "区别",
        "哪个好",
    ]
    return any(keyword in query_lower for keyword in keywords)


def _looks_like_tutorial_query(query_lower: str) -> bool:
    keywords = [
        "how to",
        "guide",
        "tutorial",
        "walkthrough",
        "教程",
        "怎么",
        "如何",
        "入门",
    ]
    return any(keyword in query_lower for keyword in keywords)


def _looks_like_debugging_query(query_lower: str) -> bool:
    keywords = [
        "bug",
        "cannot",
        "can't",
        "debug",
        "error",
        "failed",
        "failing",
        "fix",
        "how do i fix",
        "issue",
        "strict mode",
        "troubleshoot",
        "troubleshooting",
        "violation",
        "workaround",
        "报错",
        "排查",
        "修复",
        "错误",
    ]
    return any(keyword in query_lower for keyword in keywords)


def _looks_like_local_life_query(query_lower: str) -> bool:
    keywords = [
        "攻略",
        "赏花",
        "景点",
        "周末去哪",
        "游玩",
        "旅游",
        "旅行",
        "美食",
        "门票",
        "路线",
        "guide",
        "itinerary",
        "things to do",
        "travel guide",
        "weekend",
    ]
    return any(keyword in query_lower for keyword in keywords)


def _looks_like_docs_query(query_lower: str) -> bool:
    if _looks_like_api_docs_topic_query(query_lower):
        return True
    keywords = [
        "docs",
        "documentation",
        "api reference",
        "changelog",
        "readme",
        "github",
        "manual",
        "文档",
        "接口",
        "更新日志",
    ]
    return any(keyword in query_lower for keyword in keywords)


def _looks_like_api_docs_topic_query(query_lower: str) -> bool:
    keywords = [
        "api webhook",
        "api webhooks",
        "background mode",
        "generate metadata",
        "generatemetadata",
        "response api",
        "responses api",
        "test.step",
        "webhook",
        "webhooks",
    ]
    return any(keyword in query_lower for keyword in keywords)


def _looks_like_exploratory_query(query_lower: str) -> bool:
    keywords = [
        "why",
        "impact",
        "analysis",
        "trend",
        "ecosystem",
        "研究",
        "原因",
        "影响",
        "趋势",
        "生态",
    ]
    return any(keyword in query_lower for keyword in keywords)


def _result_event_page_priority(*,
    query: str,
    item: Mapping[str, Any],
) -> int:
    query_lower = query.lower()
    title_text = str(item.get("title") or "").lower()
    snippet_text = str(item.get("snippet") or "").lower()
    content_text = str(item.get("content") or "").lower()
    url = str(item.get("url") or "")
    hostname = _registered_domain(_result_hostname({"url": url}))
    score = 0
    if hostname in {"nytimes.com", "npr.org", "pbs.org", "latimes.com", "washingtonpost.com", "apnews.com"}:
        score += 4
    if (
        hostname in _OFFICIAL_AWARD_DOMAINS
        and not _looks_like_award_nomination_result(
            title_text=title_text,
            snippet_text=snippet_text,
            path=urlparse(url).path.lower(),
        )
        and not _looks_like_query_year_mismatch(
            query=query_lower,
            text=f"{title_text} {snippet_text} {content_text} {url}",
        )
    ):
        score += 6
    if any(token in title_text or token in snippet_text for token in ("winner", "winners", "full results", "full list")):
        score += 3
    if _looks_like_award_result_query(query_lower):
        award_coverage_page = _looks_like_award_coverage_page(
            query_lower=query_lower,
            title_text=title_text,
            path=urlparse(url).path.lower(),
        )
        if any(
            token in title_text or token in snippet_text or token in content_text
            for token in _award_query_category_markers(query_lower)
        ):
            score += 4
        if "grammy" in query_lower and "grammy" in f"{title_text} {snippet_text} {content_text}":
            score += 2
        if "oscar" in query_lower and any(
            token in f"{title_text} {snippet_text} {content_text}"
            for token in ("oscar", "oscars", "academy awards")
        ):
            score += 2
        if award_coverage_page:
            score += 2
        elif not _looks_like_award_winner_result(
            title_text=title_text,
            snippet_text=snippet_text,
            path=urlparse(url).path.lower(),
        ):
            score -= 4
    if _looks_like_box_office_query(query_lower):
        if any(token in title_text or token in snippet_text for token in ("box office", "opening weekend", "highest-grossing", "biggest opening")):
            score += 4
    if _looks_like_query_year_mismatch(query=query_lower, text=f"{title_text} {snippet_text} {content_text} {url}"):
        score -= 5
    if (
        _looks_like_award_result_query(query_lower)
        and _looks_like_award_category_conflict(
            query_lower=query_lower,
            title_text=title_text,
            snippet_text=snippet_text,
            content_text=content_text,
        )
    ):
        score -= 10
    if (
        _looks_like_award_result_query(query_lower)
        and _looks_like_award_brand_conflict(
            query_lower=query_lower,
            title_text=title_text,
            snippet_text=snippet_text,
            content_text=content_text,
            path=urlparse(url).path.lower(),
        )
    ):
        score -= 12
    if (
        _looks_like_award_result_query(query_lower)
        and _looks_like_weak_official_award_feature_result(
            title_text=title_text,
            snippet_text=snippet_text,
            path=urlparse(url).path.lower(),
        )
    ):
        score -= 6
    if (
        _looks_like_award_result_query(query_lower)
        and _looks_like_generic_award_archive_result(
            title_text=title_text,
            path=urlparse(url).path.lower(),
        )
    ):
        score -= 6
    if _looks_like_award_prediction_result(
        title_text=title_text,
        snippet_text=snippet_text,
        path=urlparse(url).path.lower(),
    ):
        score -= 4
    if (
        _looks_like_award_result_query(query_lower)
        and _looks_like_award_recap_or_gallery_result(
            title_text=title_text,
            snippet_text=snippet_text,
            path=urlparse(url).path.lower(),
        )
        and not _looks_like_award_winner_result(
            title_text=title_text,
            snippet_text=snippet_text,
            path=urlparse(url).path.lower(),
        )
    ):
        score -= 5
    return score


def _looks_like_publisher_fragment(entity: str) -> bool:
    entity_lower = entity.lower().strip()
    known_outlets = {
        "npr",
        "ap",
        "reuters",
        "billboard",
        "variety",
        "bbc",
        "bbc news",
        "abc news",
        "cbs news",
        "pbs",
        "today",
        "usa today",
        "rolling stone",
        "grammy",
        "grammy.com",
        "grammys",
        "grammys.com",
    }
    if entity_lower in known_outlets:
        return True
    if ".com" in entity_lower:
        return True
    if re.fullmatch(r"[A-Z]{2,6}", entity):
        return entity_lower in known_outlets
    return False


def _looks_like_query_year_mismatch(*, query: str, text: str) -> bool:
    query_years = {year for year in re.findall(r"\b(?:19|20)\d{2}\b", query)}
    if not query_years:
        return False
    result_years = {year for year in re.findall(r"\b(?:19|20)\d{2}\b", text)}
    if not result_years:
        return False
    return query_years.isdisjoint(result_years)
