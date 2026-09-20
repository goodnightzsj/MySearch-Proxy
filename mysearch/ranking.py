"""结果重排打分:把搜索结果项折叠成一个可比较大小的元组。

从 `mysearch/clients.py` 抽出的**顺序键层**。三个 rank 函数把一条结果映射成
`tuple[int, ...]`(`sorted(key=...)` 的排序键),`_resource_result_flags` 是
resource 模式前置的布尔特征折叠。

依赖方向单向:本模块依赖 `query_routing`(谓词)与 `postprocess`(域名归一),
`clients` 在上层依赖本模块。三者之间无环。

`MySearchClient` 保留同名方法作为一行委托,调用面不变。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from mysearch import postprocess
from mysearch import query_routing
from mysearch.types import SearchMode


def _news_result_rank(
    *,
    query: str,
    item: dict[str, Any],
    include_domains: list[str] | None,
) -> tuple[int, ...]:
    hostname = query_routing._result_hostname(item)
    registered_domain = query_routing._registered_domain(hostname)
    path = urlparse(item.get("url", "")).path.lower()
    title_text = (item.get("title") or "").lower()
    snippet_text = (item.get("snippet") or "").lower()
    content_text = (item.get("content") or "").lower()
    query_lower = query.lower()
    page_text = f"{title_text} {snippet_text} {content_text} {path}"
    gossip_query = query_routing._looks_like_gossip_query(query_lower)
    status_query = query_routing._looks_like_status_query(query_lower)
    award_query = query_routing._looks_like_award_result_query(query_lower)
    result_event_query = query_routing._looks_like_result_event_query(query_lower)
    precision_tokens = query_routing._query_precision_tokens(query)
    path_precision_hits, total_precision_hits = query_routing._query_precision_hit_counts(
        hostname=hostname,
        path=path,
        title_text=f"{title_text} {snippet_text} {content_text}",
        query_tokens=precision_tokens,
    )
    award_winner_page_match = int(
        award_query
        and query_routing._looks_like_award_winner_result(
            title_text=title_text,
            snippet_text=snippet_text,
            path=path,
        )
    )
    award_category_match = int(
        award_query
        and query_routing._looks_like_award_category_match(
            query_lower=query_lower,
            title_text=title_text,
            snippet_text=snippet_text,
            content_text=content_text,
            path=path,
        )
    )
    non_award_prediction_page = int(
        not (
            award_query
            and query_routing._looks_like_award_prediction_result(
                title_text=title_text,
                snippet_text=snippet_text,
                path=path,
            )
        )
    )
    non_year_mismatch = int(
        not (
            award_query
            and query_routing._looks_like_query_year_mismatch(
                query=query_lower,
                text=page_text,
            )
        )
    )
    non_low_signal_social = int(
        not (
            award_query
            and registered_domain in {"facebook.com", "instagram.com", "tiktok.com", "youtube.com"}
        )
    )
    non_award_nomination_page = int(
        not (
            award_query
            and query_routing._looks_like_award_nomination_result(
                title_text=title_text,
                snippet_text=snippet_text,
                path=path,
            )
        )
    )
    gossip_story_match = int(
        gossip_query
        and query_routing._looks_like_gossip_result(
            title_text=title_text,
            snippet_text=snippet_text,
            path=path,
        )
    )
    gossip_domain_match = int(
        gossip_query and query_routing._is_entertainment_gossip_domain(registered_domain)
    )
    include_match = int(
        (
            bool(include_domains)
            and any(postprocess._domain_matches(hostname, domain) for domain in include_domains or [])
        )
        or (status_query and query_routing._looks_like_brand_status_domain(hostname))
    )
    result_event_priority = (
        query_routing._result_event_page_priority(query=query, item=item)
        if result_event_query
        else 0
    )
    non_community_official = int(
        not (
            status_query
            and query_routing._is_obvious_official_community_result(
                hostname=hostname,
                path=path,
            )
        )
    )
    canonical_status_page_match = int(
        status_query
        and query_routing._looks_like_canonical_status_result(hostname=hostname, path=path)
    )
    status_page_match = int(
        status_query
        and query_routing._looks_like_status_result(url=item.get("url", ""), hostname=hostname, title_text=title_text)
    )
    non_status_api_endpoint = int(
        not (
            status_query
            and path.startswith("/api")
        )
    )
    canonical_changelog_page_match = int(
        status_query
        and query_routing._looks_like_canonical_changelog_result(
            url=item.get("url", ""),
            hostname=hostname,
            title_text=title_text,
            precision_tokens=precision_tokens,
        )
    )
    changelog_page_match = int(
        status_query
        and query_routing._looks_like_changelog_result(
            url=item.get("url", ""),
            hostname=hostname,
            title_text=title_text,
        )
    )
    mainstream = int(query_routing._is_mainstream_news_domain(hostname))
    article_shape = int(query_routing._looks_like_news_article_result(item))
    has_timestamp = int(postprocess._result_published_timestamp(item) is not None)
    timestamp_score = int(postprocess._result_published_timestamp(item) or 0)
    content_score, snippet_score, title_score = postprocess._result_quality_score(item)
    return (
        include_match,
        result_event_priority,
        award_winner_page_match,
        award_category_match,
        non_award_prediction_page,
        non_year_mismatch,
        non_low_signal_social,
        non_award_nomination_page,
        non_community_official,
        non_status_api_endpoint,
        canonical_status_page_match,
        status_page_match,
        canonical_changelog_page_match,
        changelog_page_match,
        gossip_story_match,
        gossip_domain_match,
        path_precision_hits,
        total_precision_hits,
        mainstream,
        article_shape,
        has_timestamp,
        timestamp_score,
        content_score,
        snippet_score,
        title_score,
    )


def _web_result_rank(
    *,
    query: str,
    item: dict[str, Any],
    include_domains: list[str] | None,
) -> tuple[int, ...]:
    hostname = query_routing._result_hostname(item)
    registered_domain = query_routing._registered_domain(hostname)
    url = item.get("url", "")
    path = urlparse(url).path.lower()
    title_text = (item.get("title") or "").lower()
    query_lower = query.lower()
    query_tokens = query_routing._query_brand_tokens(query)
    precision_tokens = query_routing._query_precision_tokens(query)
    exact_identifier_tokens = query_routing._query_exact_identifier_tokens(query)
    status_query = query_routing._looks_like_status_query(query_lower)
    include_match = int(
        (
            bool(include_domains)
            and any(postprocess._domain_matches(hostname, domain) for domain in include_domains or [])
        )
        or (status_query and query_routing._looks_like_brand_status_domain(hostname))
    )
    registered_domain_label_match = int(
        query_routing._registered_domain_label_matches(
            registered_domain=registered_domain,
            query_tokens=query_tokens,
        )
    )
    host_brand_match = int(any(token in hostname for token in query_tokens))
    title_brand_match = int(any(token in title_text for token in query_tokens))
    path_precision_hits, total_precision_hits = query_routing._query_precision_hit_counts(
        hostname=hostname,
        path=path,
        title_text=title_text,
        query_tokens=precision_tokens,
    )
    exact_path_hits, exact_total_hits = query_routing._query_exact_identifier_hit_counts(
        path=path,
        title_text=title_text,
        query_tokens=exact_identifier_tokens,
    )
    official_query = (
        bool(include_domains)
        or query_routing._looks_like_official_query(query)
        or status_query
        or query_routing._looks_like_changelog_query(query_lower)
    )
    non_community_official = int(
        not (
            official_query
            and query_routing._is_obvious_official_community_result(
                hostname=hostname,
                path=path,
            )
        )
    )
    status_page_match = int(
        status_query
        and query_routing._looks_like_status_result(url=url, hostname=hostname, title_text=title_text)
    )
    non_status_api_endpoint = int(
        not (
            status_query
            and path.startswith("/api")
        )
    )
    canonical_status_page_match = int(
        status_query
        and query_routing._looks_like_canonical_status_result(hostname=hostname, path=path)
    )
    pricing_page_match = int(
        query_routing._looks_like_pricing_query(query_lower)
        and query_routing._looks_like_pricing_result(url=url, hostname=hostname, title_text=title_text)
    )
    canonical_pricing_page_match = int(
        query_routing._looks_like_pricing_query(query_lower)
        and query_routing._looks_like_canonical_pricing_result(hostname=hostname, path=path)
    )
    debugging_query = query_routing._looks_like_debugging_query(query_lower)
    tutorial_query = query_routing._looks_like_tutorial_query(query_lower) or debugging_query
    tutorial_community_match = int(
        tutorial_query
        and query_routing._looks_like_tutorial_community_result(
            hostname=hostname,
            registered_domain=registered_domain,
            path=path,
        )
    )
    tutorial_brand_aligned = int(
        tutorial_query
        and query_routing._looks_like_brand_aligned_tutorial_result(
            hostname=hostname,
            registered_domain=registered_domain,
            path=path,
            title_text=title_text,
            snippet_text=(item.get("snippet") or "").lower(),
            query_tokens=query_tokens,
            path_precision_hits=path_precision_hits,
            exact_total_hits=exact_total_hits,
        )
    )
    debugging_signal_match = int(
        debugging_query
        and query_routing._looks_like_debugging_result(
            hostname=hostname,
            registered_domain=registered_domain,
            path=path,
            title_text=title_text,
            snippet_text=(item.get("snippet") or "").lower(),
        )
    )
    debugging_community_match = int(
        debugging_query and tutorial_community_match and debugging_signal_match
    )
    debugging_brand_aligned = int(
        debugging_query and tutorial_brand_aligned and debugging_signal_match
    )
    non_generic_debugging_docs = int(
        not (
            debugging_query
            and query_routing._looks_like_generic_debugging_docs_result(
                hostname=hostname,
                path=path,
                title_text=title_text,
            )
        )
    )
    non_tutorial_blog = int(
        not (
            tutorial_query
            and not tutorial_brand_aligned
            and not tutorial_community_match
            and query_routing._is_obvious_tutorial_blog_domain(registered_domain)
        )
    )
    local_life_query = query_routing._looks_like_local_life_query(query_lower)
    software_version_query = query_routing._looks_like_software_version_query(query_lower)
    non_social_query = not query_routing._query_prefers_web_social_sources(query_lower)
    canonical_local_guide_match = int(
        local_life_query
        and query_routing._looks_like_canonical_local_life_guide_result(
            url=url,
            hostname=hostname,
        )
    )
    local_guide_match = int(
        local_life_query
        and query_routing._looks_like_local_life_guide_result(
            url=url,
            hostname=hostname,
            title_text=title_text,
            snippet_text=(item.get("snippet") or "").lower(),
        )
    )
    non_local_life_repost = int(
        not (
            local_life_query
            and query_routing._is_obvious_local_life_repost_domain(registered_domain)
        )
    )
    non_low_signal_social_repost = int(
        not (
            non_social_query
            and registered_domain in {
                "facebook.com",
                "instagram.com",
                "threads.com",
                "tiktok.com",
                "twitter.com",
                "weibo.com",
                "x.com",
                "youtube.com",
                "youtu.be",
            }
        )
    )
    canonical_version_reference_match = int(
        software_version_query
        and query_routing._looks_like_canonical_software_version_result(
            hostname=hostname,
            path=path,
            title_text=title_text,
        )
    )
    version_reference_match = int(
        software_version_query
        and query_routing._looks_like_software_version_reference_result(
            url=url,
            hostname=hostname,
            title_text=title_text,
            snippet_text=(item.get("snippet") or "").lower(),
        )
    )
    non_software_version_aggregator = int(
        not (
            software_version_query
            and query_routing._is_obvious_web_aggregator(registered_domain)
        )
    )
    non_aggregator = int(not query_routing._is_obvious_web_aggregator(registered_domain))
    matched_provider_count = len(item.get("matched_providers") or [])
    cross_provider_boost = min(matched_provider_count, 3)
    content_score, snippet_score, title_score = postprocess._result_quality_score(item)
    return (
        include_match,
        non_community_official,
        canonical_status_page_match,
        status_page_match,
        canonical_pricing_page_match,
        pricing_page_match,
        canonical_version_reference_match,
        version_reference_match,
        debugging_community_match,
        debugging_brand_aligned,
        non_generic_debugging_docs,
        tutorial_community_match,
        tutorial_brand_aligned,
        non_tutorial_blog,
        canonical_local_guide_match,
        local_guide_match,
        non_local_life_repost,
        non_low_signal_social_repost,
        non_software_version_aggregator,
        exact_path_hits,
        exact_total_hits,
        path_precision_hits,
        total_precision_hits,
        registered_domain_label_match,
        host_brand_match,
        title_brand_match,
        non_status_api_endpoint,
        non_aggregator,
        cross_provider_boost,
        content_score,
        max(snippet_score, title_score),
    )


def _resource_result_rank(
    *,
    query: str,
    mode: SearchMode,
    item: dict[str, Any],
    query_tokens: list[str],
    precision_tokens: list[str],
    exact_identifier_tokens: list[str],
    topic_specific_tokens: list[str],
    include_domains: list[str] | None,
    strict_official: bool,
) -> tuple[int, ...]:
    flags = _resource_result_flags(
        mode=mode,
        item=item,
        query_tokens=query_tokens,
        include_domains=include_domains,
    )
    hostname = str(flags["hostname"])
    path = urlparse(item.get("url", "")).path.lower()
    include_match = int(flags["include_match"])
    host_brand_match = int(flags["host_brand_match"])
    registered_domain_label_match = int(flags["registered_domain_label_match"])
    title_brand_match = int(flags["title_brand_match"])
    docs_shape_match = int(flags["docs_shape_match"])
    github_bonus = int(
        mode == "github"
        and flags["hostname"] in {"github.com", "raw.githubusercontent.com"}
    )
    github_release_query = query_routing._looks_like_github_release_query(query.lower())
    canonical_github_release_page_match = int(
        github_release_query
        and hostname == "github.com"
        and path.rstrip("/").endswith("/releases")
    )
    pdf_bonus = int(mode == "pdf" and query_routing._looks_like_pdf_url(item.get("url", "")))
    non_derivative_paper_bonus = int(
        mode == "pdf"
        and not query_routing._looks_like_derivative_paper_title(
            (item.get("title") or "").lower()
        )
    )
    paper_landing_bonus = int(
        mode == "pdf"
        and "paper" in precision_tokens
        and str(flags["hostname"]) == "arxiv.org"
        and any(
            marker in urlparse(item.get("url", "")).path.lower()
            for marker in ("/abs/", "/html/")
        )
    )
    primary_named_paper_bonus = int(
        mode == "pdf"
        and query_routing._looks_like_primary_named_paper_result(
            title_text=(item.get("title") or "").lower(),
            query_tokens=query_routing._paper_query_subject_tokens(
                query=query,
                query_tokens=query_tokens,
                precision_tokens=precision_tokens,
            ),
        )
    )
    non_community_official = int(
        not (
            strict_official
            and query_routing._is_obvious_official_community_result(
                hostname=hostname,
                path=path,
            )
        )
    )
    non_third_party = int(flags["non_third_party"])
    official_resource_match = int(
        query_routing._is_probably_official_resource_result(
            mode=mode,
            hostname=hostname,
            include_match=bool(include_match),
            registered_domain_label_match=bool(registered_domain_label_match),
            host_brand_match=bool(host_brand_match),
            title_brand_match=bool(title_brand_match),
            docs_shape_match=bool(docs_shape_match),
            non_third_party=bool(non_third_party),
            official_query=strict_official,
        )
    )
    url = item.get("url", "")
    query_lower = query.lower()
    status_query = query_routing._looks_like_status_query(query_lower)
    path_precision_hits, total_precision_hits = query_routing._query_precision_hit_counts(
        hostname=hostname,
        path=path,
        title_text=(item.get("title") or "").lower(),
        query_tokens=precision_tokens,
    )
    topic_path_hits, topic_total_hits = query_routing._query_precision_hit_counts(
        hostname=hostname,
        path=path,
        title_text=(item.get("title") or "").lower(),
        query_tokens=topic_specific_tokens,
    )
    exact_path_hits, exact_total_hits = query_routing._query_exact_identifier_hit_counts(
        path=path,
        title_text=(item.get("title") or "").lower(),
        query_tokens=exact_identifier_tokens,
    )
    tutorial_query = query_routing._looks_like_tutorial_query(query_lower) or query_routing._looks_like_debugging_query(query_lower)
    tutorial_community_result = int(
        tutorial_query
        and query_routing._looks_like_tutorial_community_result(
            hostname=hostname,
            registered_domain=query_routing._registered_domain(hostname),
            path=path,
        )
    )
    tutorial_brand_aligned_resource = int(
        tutorial_query
        and not tutorial_community_result
        and query_routing._looks_like_brand_aligned_tutorial_result(
            hostname=hostname,
            registered_domain=query_routing._registered_domain(hostname),
            path=path,
            title_text=(item.get("title") or "").lower(),
            snippet_text=(item.get("snippet") or "").lower(),
            query_tokens=query_tokens,
            path_precision_hits=path_precision_hits,
            exact_total_hits=exact_total_hits,
        )
    )
    tutorial_exact_identifier_match = int(
        tutorial_query and (exact_total_hits > 0 or exact_path_hits > 0)
    )
    non_generic_tutorial_docs = int(
        not (
            tutorial_query
            and exact_identifier_tokens
            and query_routing._looks_like_generic_debugging_docs_result(
                hostname=hostname,
                path=path,
                title_text=(item.get("title") or "").lower(),
            )
        )
    )
    official_docs_query = strict_official and query_routing._looks_like_official_docs_query(query_lower)
    official_topic_exact_match = int(
        official_docs_query
        and docs_shape_match
        and (
            (
                bool(topic_specific_tokens)
                and (topic_total_hits > 0 or topic_path_hits > 0)
            )
            or (
                not topic_specific_tokens
                and (exact_total_hits > 0 or path_precision_hits >= 2 or total_precision_hits >= 3)
            )
        )
    )
    non_language_sdk_reference = int(
        not (
            official_docs_query
            and query_routing._looks_like_language_specific_sdk_reference_result(
                hostname=hostname,
                path=path,
                title_text=(item.get("title") or "").lower(),
            )
            and not query_routing._query_mentions_programming_language(query_lower)
        )
    )
    non_generic_official_landing = int(
        not (
            official_docs_query
            and query_routing._looks_like_generic_official_landing_result(
                hostname=hostname,
                path=path,
                title_text=(item.get("title") or "").lower(),
            )
        )
    )
    canonical_status_page_match = int(
        status_query
        and query_routing._looks_like_canonical_status_result(hostname=hostname, path=path)
    )
    status_page_match = int(
        status_query
        and query_routing._looks_like_status_result(
            url=url,
            hostname=hostname,
            title_text=(item.get("title") or "").lower(),
        )
    )
    non_status_api_endpoint = int(
        not (
            status_query
            and path.startswith("/api")
        )
    )
    paper_subject_tokens = (
        query_routing._paper_query_subject_tokens(
            query=query,
            query_tokens=query_tokens,
            precision_tokens=precision_tokens,
        )
        if mode == "pdf"
        else []
    )
    paper_compound_tokens = query_routing._paper_query_compound_tokens(query) if mode == "pdf" else []
    paper_compound_match = int(
        mode == "pdf"
        and any(
            query_routing._paper_text_matches_compound_token(
                f"{item.get('title', '')} {path}",
                token,
            )
            for token in paper_compound_tokens
        )
    )
    paper_subject_exact_match = int(
        mode == "pdf"
        and query_routing._looks_like_primary_named_paper_result(
            title_text=(item.get("title") or "").lower(),
            query_tokens=paper_subject_tokens,
        )
    )
    exact_base_report_title_match = int(
        mode == "pdf"
        and query_routing._looks_like_exact_base_paper_query(query)
        and not query_routing._looks_like_variant_base_paper_result(
            query=query,
            title_text=(item.get("title") or "").lower(),
        )
        and bool(
            re.match(
                rf"^\s*(?:\[[^\]]+\]\s*)?{re.escape(query_routing._base_report_subject_token(query))}\s+technical report\b",
                (item.get("title") or "").lower(),
            )
        )
    )
    canonical_paper_source = int(
        mode == "pdf"
        and (
            (
                hostname == "arxiv.org"
                and any(marker in path for marker in ("/abs/", "/html/", "/pdf/"))
            )
            or (
                hostname == "openreview.net"
                and "/forum" in path
            )
        )
    )
    non_pdf_mirror_aggregator = int(
        not (
            mode == "pdf"
            and query_routing._is_obvious_pdf_mirror_or_aggregator_result(
                hostname=hostname,
                registered_domain=query_routing._registered_domain(hostname),
                path=path,
            )
        )
    )
    non_paper_compound_mismatch = int(
        not (
            mode == "pdf"
            and paper_compound_tokens
            and not paper_compound_match
            and not query_routing._looks_like_generic_arxiv_subject_title((item.get("title") or "").lower())
        )
    )
    changelog_page_match = int(
        query_routing._looks_like_changelog_query(query_lower)
        and query_routing._looks_like_changelog_result(
            url=url,
            hostname=str(flags["hostname"]),
            title_text=(item.get("title") or "").lower(),
        )
    )
    canonical_changelog_page_match = int(
        query_routing._looks_like_changelog_query(query_lower)
        and query_routing._looks_like_canonical_changelog_result(
            url=url,
            hostname=hostname,
            title_text=(item.get("title") or "").lower(),
            precision_tokens=precision_tokens,
        )
    )
    non_generic_changelog_index = int(
        not (
            query_routing._looks_like_changelog_query(query_lower)
            and query_routing._looks_like_generic_changelog_index_result(
                hostname=hostname,
                path=path,
            )
        )
    )
    pricing_page_match = int(
        query_routing._looks_like_pricing_query(query_lower)
        and query_routing._looks_like_pricing_result(
            url=url,
            hostname=hostname,
            title_text=(item.get("title") or "").lower(),
        )
    )
    canonical_pricing_page_match = int(
        query_routing._looks_like_pricing_query(query_lower)
        and query_routing._looks_like_canonical_pricing_result(
            hostname=hostname,
            path=path,
        )
    )
    non_locale_variant = int(
        not (
            strict_official
            and (
                query_routing._looks_like_locale_prefixed_path(path)
                or query_routing._looks_like_locale_prefixed_hostname(hostname)
            )
        )
    )
    non_preview_react_variant = int(
        not (
            strict_official
            and query_routing._looks_like_noncanonical_react_docs_hostname(hostname, query=query)
        )
    )
    matched_provider_count = len(item.get("matched_providers") or [])
    content_score, snippet_score, title_score = postprocess._result_quality_score(item)
    return (
        include_match,
        canonical_github_release_page_match,
        non_community_official,
        official_resource_match,
        official_topic_exact_match,
        canonical_status_page_match,
        status_page_match,
        non_status_api_endpoint,
        exact_base_report_title_match,
        canonical_paper_source,
        non_pdf_mirror_aggregator,
        paper_subject_exact_match,
        primary_named_paper_bonus,
        non_derivative_paper_bonus,
        paper_compound_match,
        non_paper_compound_mismatch,
        paper_landing_bonus,
        topic_path_hits,
        topic_total_hits,
        tutorial_brand_aligned_resource,
        1 - tutorial_community_result,
        tutorial_exact_identifier_match,
        non_generic_tutorial_docs,
        non_language_sdk_reference,
        non_generic_official_landing,
        canonical_changelog_page_match,
        changelog_page_match,
        non_generic_changelog_index,
        canonical_pricing_page_match,
        pricing_page_match,
        exact_path_hits,
        exact_total_hits,
        non_preview_react_variant,
        non_locale_variant,
        path_precision_hits,
        total_precision_hits,
        registered_domain_label_match,
        github_bonus,
        pdf_bonus,
        host_brand_match,
        docs_shape_match,
        non_third_party,
        title_brand_match,
        matched_provider_count,
        content_score,
        snippet_score,
        title_score,
    )


def _resource_result_flags(
    *,
    mode: SearchMode,
    item: dict[str, Any],
    query_tokens: list[str],
    include_domains: list[str] | None,
) -> dict[str, Any]:
    url = item.get("url", "")
    hostname = query_routing._result_hostname(item)
    registered_domain = query_routing._registered_domain(hostname)
    title_text = (item.get("title") or "").lower()
    include_match = bool(
        include_domains
        and any(postprocess._domain_matches(hostname, domain) for domain in include_domains or [])
    )
    host_brand_match = any(
        token in hostname or token in registered_domain for token in query_tokens
    )
    registered_domain_label_match = query_routing._registered_domain_label_matches(
        registered_domain=registered_domain,
        query_tokens=query_tokens,
    )
    title_brand_match = any(token in title_text for token in query_tokens)
    docs_shape_match = query_routing._looks_like_resource_result(
        url=url,
        hostname=hostname,
        title_text=title_text,
        mode=mode,
    )
    non_third_party = not query_routing._is_obvious_third_party_resource(
        hostname=hostname,
        registered_domain=registered_domain,
        mode=mode,
    )
    return {
        "hostname": hostname,
        "registered_domain": registered_domain,
        "include_match": include_match,
        "host_brand_match": host_brand_match,
        "registered_domain_label_match": registered_domain_label_match,
        "title_brand_match": title_brand_match,
        "docs_shape_match": docs_shape_match,
        "non_third_party": non_third_party,
    }

