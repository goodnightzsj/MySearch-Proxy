"""结果强度判定：当前结果集是否已经足够强，还是该触发救援路径。

从 `mysearch/clients.py` 抽出的**结果强度判据层**。这些函数回答"手上这批
结果够不够好"——PDF/论文类匹配是否够强、教程类是否有品牌对齐的正规文档、
changelog 与定价页是否命中，以及整体结果集是否弱到需要 Exa 救援。

输入是已经取回的结果项与查询文本，输出是布尔判据——不做 provider 调用、
不读 config、不碰实例状态。

依赖方向单向：本模块依赖 `query_routing`（结果与查询谓词）、`postprocess`
（域名归一）与同包的 `sections`；`clients` 在上层依赖本模块。

`MySearchClient` 保留同名方法作为一行委托，调用面不变。
"""

from __future__ import annotations

import re
from typing import Any, Sequence
from urllib.parse import urlparse

from mysearch import postprocess
from mysearch import query_routing
from mysearch.research import sections
def _result_set_looks_weak_for_exa_rescue(
    *,
    query: str,
    mode: SearchMode,
    result: dict[str, Any],
) -> bool:
        results = list(result.get("results") or [])
        if not results:
            return True
        query_lower = query.lower()
        if query_routing._looks_like_award_result_query(query_lower):
            return not query_routing._has_strong_award_result(query=query, results=results)
        if mode == "pdf":
            return not _has_strong_pdf_match(query=query, results=results)
        if query_routing._looks_like_pricing_query(query_lower):
            return not _has_canonical_pricing_result(results)
        if query_routing._looks_like_changelog_query(query_lower):
            return not _has_strong_changelog_result(query=query, results=results)
        if query_routing._looks_like_tutorial_query(query_lower) or query_routing._looks_like_debugging_query(query_lower):
            return not _has_strong_tutorial_result(query=query, results=results, mode=mode)
        return False


def _has_strong_pdf_match(
    *,
    query: str,
    results: list[dict[str, Any]],
) -> bool:
        query_tokens = query_routing._query_brand_tokens(query)
        precision_tokens = query_routing._query_precision_tokens(query)
        paper_tokens = query_routing._paper_query_subject_tokens(
            query=query,
            query_tokens=query_tokens,
            precision_tokens=precision_tokens,
        )
        compound_tokens = query_routing._paper_query_compound_tokens(query)
        exact_base_report_query = query_routing._looks_like_exact_base_paper_query(query)
        for item in results[:3]:
            url = item.get("url", "")
            hostname = postprocess._result_hostname(item)
            registered_domain = postprocess._registered_domain(hostname)
            path = urlparse(url).path.lower()
            title_text = (item.get("title") or "").lower()
            path_hits, total_hits = query_routing._query_precision_hit_counts(
                hostname=hostname,
                path=path,
                title_text=title_text,
                query_tokens=precision_tokens,
            )
            named_paper = query_routing._looks_like_primary_named_paper_result(
                title_text=title_text,
                query_tokens=paper_tokens,
            )
            compound_match = any(
                query_routing._paper_text_matches_compound_token(f"{title_text} {path}", token)
                for token in compound_tokens
            )
            derivative_title = query_routing._looks_like_derivative_paper_title(title_text)
            paper_shape = query_routing._looks_like_pdf_url(url) or any(
                marker in path for marker in ("/abs/", "/html/")
            )
            mirror_or_aggregator = query_routing._is_obvious_pdf_mirror_or_aggregator_result(
                hostname=hostname,
                registered_domain=registered_domain,
                path=path,
            )
            if mirror_or_aggregator:
                continue
            if exact_base_report_query and query_routing._looks_like_variant_base_paper_result(
                query=query,
                title_text=title_text,
            ):
                continue
            if (
                compound_tokens
                and compound_match
                and paper_shape
                and not derivative_title
                and (named_paper or total_hits >= min(max(len(paper_tokens), 2), 3))
            ):
                return True
            if (
                compound_tokens
                and not compound_match
                and paper_shape
                and not query_routing._looks_like_generic_arxiv_subject_title(title_text)
            ):
                continue
            if named_paper and paper_shape and not derivative_title:
                return True
            if not derivative_title and paper_shape and total_hits >= min(max(len(paper_tokens), 2), 3):
                return True
            if hostname == "arxiv.org" and path_hits >= 2 and paper_shape:
                return True
        return False


def _has_canonical_pricing_result(
    results: list[dict[str, Any]],
) -> bool:
        for item in results[:5]:
            hostname = postprocess._result_hostname(item)
            path = urlparse(item.get("url", "")).path.lower()
            if query_routing._looks_like_canonical_pricing_result(hostname=hostname, path=path):
                return True
        return False


def _has_strong_changelog_result(
    *,
    query: str,
    results: list[dict[str, Any]],
) -> bool:
        precision_tokens = query_routing._query_precision_tokens(query)
        for item in results[:3]:
            url = item.get("url", "")
            hostname = postprocess._result_hostname(item)
            title_text = (item.get("title") or "").lower()
            if query_routing._looks_like_canonical_changelog_result(
                url=url,
                hostname=hostname,
                title_text=title_text,
                precision_tokens=precision_tokens,
            ):
                return True
            if query_routing._looks_like_changelog_result(
                url=url,
                hostname=hostname,
                title_text=title_text,
            ):
                return True
        return False


def _has_strong_tutorial_result(
    *,
    query: str,
    results: list[dict[str, Any]],
    mode: SearchMode = 'auto',
) -> bool:
        query_tokens = query_routing._query_brand_tokens(query)
        precision_tokens = query_routing._query_precision_tokens(query)
        exact_identifier_tokens = query_routing._query_exact_identifier_tokens(query)
        debugging_query = query_routing._looks_like_debugging_query(query.lower())
        explicit_resource_mode = mode in {"docs", "github", "pdf"}
        for item in results[:5]:
            url = item.get("url", "")
            hostname = postprocess._result_hostname(item)
            registered_domain = postprocess._registered_domain(hostname)
            path = urlparse(url).path.lower()
            title_text = (item.get("title") or "").lower()
            snippet_text = (item.get("snippet") or "").lower()
            path_hits, total_hits = query_routing._query_precision_hit_counts(
                hostname=hostname,
                path=path,
                title_text=title_text,
                query_tokens=precision_tokens,
            )
            _, exact_total_hits = query_routing._query_exact_identifier_hit_counts(
                path=path,
                title_text=title_text,
                query_tokens=exact_identifier_tokens,
            )
            community_debug = (
                registered_domain == "stackoverflow.com"
                or (registered_domain == "github.com" and any(marker in path for marker in ("/issues/", "/discussions/")))
                or query_routing._is_obvious_official_community_result(hostname=hostname, path=path)
            )
            brand_aligned = query_routing._registered_domain_label_matches(
                registered_domain=registered_domain,
                query_tokens=query_tokens,
            ) or any(token in hostname for token in query_tokens)
            brand_aligned_docs = query_routing._looks_like_brand_aligned_tutorial_result(
                hostname=hostname,
                registered_domain=registered_domain,
                path=path,
                title_text=title_text,
                snippet_text=snippet_text,
                query_tokens=query_tokens,
                path_precision_hits=path_hits,
                exact_total_hits=exact_total_hits,
            )
            debugging_match = query_routing._looks_like_debugging_result(
                hostname=hostname,
                registered_domain=registered_domain,
                path=path,
                title_text=title_text,
                snippet_text=snippet_text,
            )
            if not explicit_resource_mode and community_debug and (
                path_hits > 0 or exact_total_hits > 0 or "issue" in title_text
            ):
                return True
            if debugging_query:
                if not explicit_resource_mode and community_debug and debugging_match:
                    return True
                if brand_aligned and brand_aligned_docs and debugging_match and (
                    exact_total_hits > 0 or path_hits >= 1 or total_hits >= 2
                ):
                    return True
                continue
            if brand_aligned and brand_aligned_docs and (
                exact_total_hits > 0 or path_hits >= 2 or total_hits >= 3
            ):
                return True
        return False

