"""结果载荷合并与整形：去重、优先级、URL 归一。

从 `mysearch/clients.py` 抽出的**载荷整形层**。输入是多个 provider 的结果
载荷，输出是合并去重后的单一载荷——不做 provider 调用、不读 config、
不碰实例状态。

单独成模块而不是并入 `postprocess`：`_prioritize_research_project_results`
需要 `research.selection` 的去重逻辑，而 `postprocess` 必须保持对
`query_routing` / `research` 的零依赖（`query_routing` 依赖 `postprocess`，
反向引用会成环）。

依赖方向单向：本模块依赖 `postprocess` 与同包的 `selection`；`clients` 在上层
依赖本模块。

`MySearchClient` 保留同名方法作为一行委托，调用面不变。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from mysearch import postprocess
from mysearch import query_routing
from mysearch.research import selection


def _prioritize_research_project_results(
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
        project_results = [
            item
            for item in results
            if str(item.get("provider") or "") == "canonical_research_projects"
        ]
        if not project_results:
            return results
        other_results = [
            item
            for item in results
            if str(item.get("provider") or "") != "canonical_research_projects"
        ]
        return selection._dedupe_research_results_for_report(
            project_results,
            other_results,
        )


def _github_blob_raw_url(
    url: str,
) -> str | None:
        raw_urls = _github_blob_raw_urls(url)
        if not raw_urls:
            return None
        return raw_urls[0]


def _github_blob_raw_urls(
    url: str,
) -> list[str]:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return []
        if parsed.netloc.lower() != "github.com":
            return []

        parts = [segment for segment in parsed.path.split("/") if segment]
        if len(parts) < 5 or parts[2] != "blob":
            return []

        owner, repo, _, ref, *path_parts = parts
        if not owner or not repo or not ref or not path_parts:
            return []
        raw_path = "/".join(path_parts)
        refs = [ref]
        if ref == "main":
            refs.append("master")
        elif ref == "master":
            refs.append("main")
        return [
            f"https://raw.githubusercontent.com/{owner}/{repo}/{candidate_ref}/{raw_path}"
            for candidate_ref in refs
        ]


def _has_meaningful_extract_content(
    result: dict[str, Any],
) -> bool:
        return postprocess._extract_quality_issue(result) is None


def _merge_search_payloads(
    *,
    primary_result: dict[str, Any],
    secondary_result: dict[str, Any] | None,
    max_results: int,
) -> dict[str, Any]:
        sequences: list[list[str]] = []
        variants_by_key: dict[str, list[dict[str, Any]]] = {}
        providers_by_key: dict[str, set[str]] = {}

        for result in [primary_result, secondary_result]:
            if not result:
                continue

            sequence: list[str] = []
            result_provider = result.get("provider", "")
            for item in result.get("results", []) or []:
                if not isinstance(item, dict):
                    continue
                dedupe_key = postprocess._result_dedupe_key(item)
                if not dedupe_key:
                    continue
                sequence.append(dedupe_key)
                variants_by_key.setdefault(dedupe_key, []).append(dict(item))
                providers_by_key.setdefault(dedupe_key, set()).add(
                    item.get("provider") or result_provider
                )
            sequences.append(sequence)

        merged_keys: list[str] = []
        indexes = [0 for _ in sequences]
        seen_keys: set[str] = set()
        while len(merged_keys) < max_results and sequences:
            progressed = False
            for seq_index, sequence in enumerate(sequences):
                if len(merged_keys) >= max_results:
                    break
                while indexes[seq_index] < len(sequence):
                    dedupe_key = sequence[indexes[seq_index]]
                    indexes[seq_index] += 1
                    if dedupe_key in seen_keys:
                        continue
                    seen_keys.add(dedupe_key)
                    merged_keys.append(dedupe_key)
                    progressed = True
                    break
            if not progressed:
                break

        results: list[dict[str, Any]] = []
        matched_results = 0
        for dedupe_key in merged_keys:
            variants = variants_by_key.get(dedupe_key, [])
            if not variants:
                continue
            providers = sorted(item for item in providers_by_key.get(dedupe_key, set()) if item)
            if len(providers) > 1:
                matched_results += 1
            best = dict(max(variants, key=postprocess._result_quality_score))
            if urlparse(dedupe_key).hostname == "arxiv.org":
                meaningful_titles = [
                    str(item.get("title") or "").strip()
                    for item in variants
                    if str(item.get("title") or "").strip()
                    and not query_routing._looks_like_generic_arxiv_subject_title(
                        str(item.get("title") or "").strip()
                    )
                ]
                if meaningful_titles:
                    best["title"] = max(meaningful_titles, key=len)
            merged_item = postprocess._canonicalize_result_item(best)
            merged_item["matched_providers"] = providers
            results.append(merged_item)

        citations = postprocess._dedupe_citations(
            primary_result.get("citations") or [],
            (secondary_result.get("citations") or []) if secondary_result else [],
        )
        return {
            "results": results,
            "citations": citations,
            "matched_results": matched_results,
        }

