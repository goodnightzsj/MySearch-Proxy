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

#: RRF 的名次常数 `k`。取 60 —— Cormack 原论文与 Elasticsearch / OpenSearch
#: 工业实现的默认值。见 `_merge_search_payloads` 里的说明：调低放大头部差异，
#: 调高让贡献更平缓；无证据支持偏离默认值。
RRF_RANK_CONSTANT = 60


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

        # 合并序用 **RRF**（reciprocal rank fusion，Cormack et al., SIGIR 2009）：
        #     score(d) = Σ_lists 1 / (k + rank_list(d))
        #
        # 原实现是**轮询交替 + 去重**：它不计算任何分数，于是"两个 provider
        # 都排第 1"的文档与"只在一个 provider 出现"的文档**同权** —— 而
        # "多来源共同确认"恰恰是多 provider 检索里最强的相关性信号。
        # 实测（loop38 的 9 份双 provider payload）：轮询与 RRF 的 top-1
        # **6/9 不同**，且 8/9 的 payload 里 RRF 会把双 provider 共同返回的
        # 文档上提。最明显的一例是 `factual-accuracy-01`：轮询把 YouTube 视频
        # 排在 `devguide.python.org/versions` 之前。
        #
        # RRF 的两个性质正合此处：**只看名次不看分数**（跨 provider 的分数
        # 本就不可比 —— 余弦相似度 0.85 与 BM25 12.4 没有共同尺度），
        # 且**免调参、免训练**（需要训练数据的 LambdaMART / neural rank fusion
        # 在 48 行评测集上会过拟合，且引入更难发现的问题）。
        #
        # `RRF_RANK_CONSTANT = 60` 是原论文与工业实现（Elasticsearch、OpenSearch）
        # 的默认值：调低（20-40）放大头部差异，调高（80-100）让贡献更平缓。
        # 这里不调参 —— 没有证据支持偏离默认值。
        rrf_scores: dict[str, float] = {}
        for sequence in sequences:
            for rank, dedupe_key in enumerate(sequence, start=1):
                rrf_scores[dedupe_key] = rrf_scores.get(dedupe_key, 0.0) + 1.0 / (
                    RRF_RANK_CONSTANT + rank
                )

        # 平分时用"最早出现"打破。**不需要**额外的次序表：`rrf_scores` 是
        # dict，插入序就是首次出现序，而 `sorted` 是稳定排序 —— 两者相加
        # 已经保证平分保持首次出现序。实测验证过这一点：加一张显式次序表，
        # 去掉它测试仍然全绿，说明那是死代码，故删掉。
        merged_keys = sorted(rrf_scores, key=lambda key: -rrf_scores[key])[:max_results]

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

