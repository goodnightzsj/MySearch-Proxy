"""软件版本答案：从文档片段里抽取版本号并据此重排结果。

从 `mysearch/clients.py` 抽出的**版本事实层**。输入是已经取回的结果项与
文档正文，输出是版本号、候选排序与答案文本——不做 provider 调用、
不读 config、不碰实例状态。

依赖方向单向：本模块依赖 `postprocess`（结果规范化）与 `query_routing`
（版本类查询谓词）；`clients` 在上层依赖本模块。

`MySearchClient` 保留同名方法作为一行委托，调用面不变。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from mysearch import postprocess
from mysearch import query_routing
from mysearch.research import sections

#: 一个候选要算作对当前版本的**断言**而非单纯提及，所需的每版本最低信号分。
#: `_software_version_candidates_from_text` 给泛化正向标记（"version"/"release"/
#: "supported"）2 分，给真正的 "latest stable release" 措辞 4 分；版本索引页的
#: 表格行永远只到 2 分。见 `_software_version_item_is_version_index`。
MIN_VERSION_ASSERTION_SCORE = 4
def _apply_software_version_answer_override(
    *,
    query: str,
    mode: SearchMode,
    intent: ResolvedSearchIntent,
    result: dict[str, Any],
) -> dict[str, Any]:
        query_lower = query.lower()
        if not query_routing._looks_like_software_version_query(query_lower):
            return result
        if mode == "news" or intent in {"news", "status", "social"}:
            return result

        result_items = list(result.get("results") or [])
        if not result_items:
            return result

        version_evidence_items = list(result_items)
        for branch_name in ("primary_search", "secondary_search"):
            branch = result.get(branch_name)
            if isinstance(branch, dict):
                version_evidence_items.extend(list(branch.get("results") or []))

        extracted_answer = _extract_software_version_answer(
            query=query,
            results=version_evidence_items,
        )
        if not extracted_answer:
            return result

        current_answer = str(result.get("answer") or "").strip()
        current_version = _extract_semantic_version(current_answer)
        extracted_version = _extract_semantic_version(extracted_answer)
        should_override = not current_answer
        if extracted_version is not None:
            if current_version is None or extracted_version > current_version or current_answer != extracted_answer:
                should_override = True
        elif current_answer != extracted_answer:
            should_override = True
        if not should_override:
            return result

        updated = dict(result)
        updated["answer"] = extracted_answer
        updated["evidence"] = dict(updated.get("evidence") or {})
        updated["evidence"]["answer_source"] = "software-version-extraction"
        return updated


def _extract_software_version_answer(
    *,
    query: str,
    results: list[dict[str, Any]],
) -> str:
        subject = _software_version_subject(query)
        candidates: list[tuple[int, tuple[int, int, int], int, str]] = []
        for index, item in enumerate(results):
            item_score = _software_version_result_score(query=query, item=item)
            if item_score <= 0:
                continue
            item_candidates: list[tuple[tuple[int, int, int], int, str]] = []
            seen_versions: dict[tuple[int, int, int], int] = {}
            text_chunks = [
                str(item.get("title") or ""),
                str(item.get("snippet") or ""),
                str(item.get("content") or ""),
            ]
            for text in text_chunks:
                for version_text, version_tuple, signal_score in _software_version_candidates_from_text(text):
                    prior = seen_versions.get(version_tuple)
                    if prior is not None and prior >= signal_score:
                        continue
                    seen_versions[version_tuple] = signal_score
                    item_candidates.append((version_tuple, signal_score, version_text))
            if _software_version_item_is_version_index(
                versions=[candidate[0] for candidate in item_candidates],
                peak_signal=max((candidate[1] for candidate in item_candidates), default=0),
            ):
                continue
            for version_tuple, signal_score, version_text in item_candidates:
                candidates.append((item_score + signal_score, version_tuple, -index, version_text))

        if not candidates:
            return ""

        best_score = max(item[0] for item in candidates)
        shortlist = [item for item in candidates if item[0] >= best_score - 1]
        _, _, _, version_text = max(shortlist, key=lambda item: (item[1], item[0], item[2]))
        if subject:
            return f"The latest stable version of {subject} is {version_text}."
        return f"The latest stable version is {version_text}."


def _software_version_item_is_version_index(
    *,
    versions: list[tuple[int, int, int]],
    peak_signal: int,
) -> bool:
        """True when an item enumerates versions instead of asserting one.

        Pages such as devguide.python.org/versions/ or an end-of-life table
        list every supported branch. Their entries score only the generic
        positive marker, so the page can still win on host authority while
        saying nothing about which release is current -- exactly how a
        "future Python 3.16" table row displaced the real answer.
        """
        if len(set(versions)) < 3:
            return False
        return peak_signal < MIN_VERSION_ASSERTION_SCORE


def _software_version_candidates_from_text(
    text: str,
) -> list[tuple[str, tuple[int, int, int], int]]:
        if not text:
            return []
        normalized = re.sub(r"\s+", " ", text).strip()
        lowered = normalized.lower()
        if not normalized:
            return []
        positive_markers = (
            "current stable",
            "latest stable",
            "stable version",
            "stable release",
            "latest version",
            "latest release",
            "released",
            "release",
            "supported",
            "version",
        )
        negative_markers = (
            "alpha",
            "beta",
            "development branch",
            "development version",
            "future",
            "main branch",
            "planned",
            "pre-release",
            "prerelease",
            "preview",
            "rc",
            "release candidate",
            "scheduled",
            "upcoming",
        )
        candidates: list[tuple[str, tuple[int, int, int], int]] = []
        for match in re.finditer(r"\b\d+\.\d+(?:\.\d+)?\b", normalized):
            version_text = match.group(0)
            version_tuple = _extract_semantic_version(version_text)
            if version_tuple is None:
                continue
            start = max(0, match.start() - 80)
            end = min(len(lowered), match.end() + 80)
            context = lowered[start:end]
            sentence_start = start
            sentence_end = end
            left_context = lowered[start:match.start()]
            left_boundaries = list(re.finditer(r"[.!?;]\s+", left_context))
            if left_boundaries:
                sentence_start += left_boundaries[-1].end()
            right_context = lowered[match.end():end]
            right_boundary = re.search(r"[.!?;]\s+", right_context)
            if right_boundary:
                sentence_end = match.end() + right_boundary.start()
            sentence_context = lowered[sentence_start:sentence_end]
            if any(marker in sentence_context for marker in negative_markers):
                continue
            score = 0
            if any(marker in context for marker in positive_markers):
                score += 2
            if "latest" in context and "stable" in context:
                score += 2
            if "as of" in context or "maintenance release" in context:
                score += 1
            # Prefer an exact patch release over a major-only status-page mention
            # when both are otherwise plausible stable-version evidence.
            if version_text.count(".") >= 2:
                score += 1
            if score <= 0:
                continue
            candidates.append((version_text, version_tuple, score))
        return candidates


def _software_version_result_score(
    *,
    query: str,
    item: Mapping[str, Any],
) -> int:
        url = str(item.get("url") or "")
        hostname = postprocess._result_hostname(item)
        registered_domain = postprocess._registered_domain(hostname)
        path = urlparse(url).path.lower()
        title_text = str(item.get("title") or "").lower()
        snippet_text = str(item.get("snippet") or "").lower()
        query_tokens = query_routing._query_brand_tokens(query)

        score = 0
        if query_routing._looks_like_canonical_software_version_result(
            hostname=hostname,
            path=path,
            title_text=title_text,
        ):
            score += 4
        elif query_routing._looks_like_software_version_reference_result(
            url=url,
            hostname=hostname,
            title_text=title_text,
            snippet_text=snippet_text,
        ):
            score += 2
        if query_routing._registered_domain_label_matches(
            registered_domain=registered_domain,
            query_tokens=query_tokens,
        ):
            score += 2
        if sections._result_matches_official_policy(
            item=item,
            mode="web",
            query_tokens=query_tokens,
            include_domains=None,
            strict_official=False,
        ):
            score += 2
        if not query_routing._is_obvious_web_aggregator(registered_domain):
            score += 1
        else:
            score -= 2
        return score


def _software_version_subject(
    query: str,
) -> str:
        subject = ""
        match = re.search(r"(?:version|release)\s+of\s+([^?]+)", query, flags=re.IGNORECASE)
        if match:
            subject = match.group(1).strip(" .?!")
        if not subject:
            skip_tokens = {
                "current",
                "latest",
                "newest",
                "release",
                "stable",
                "version",
            }
            candidates = [
                token for token in query_routing._query_brand_tokens(query)
                if token not in skip_tokens
            ]
            if candidates:
                subject = candidates[-1]
        if not subject:
            return ""
        normalized = re.sub(r"\s+", " ", subject).strip()
        subject_map = {
            "go": "Go",
            "javascript": "JavaScript",
            "kubernetes": "Kubernetes",
            "next.js": "Next.js",
            "node": "Node.js",
            "node.js": "Node.js",
            "openai": "OpenAI",
            "postgres": "Postgres",
            "postgresql": "PostgreSQL",
            "python": "Python",
            "react": "React",
            "rust": "Rust",
            "typescript": "TypeScript",
        }
        return subject_map.get(normalized.lower(), normalized if any(ch.isupper() for ch in normalized) else normalized.title())


def _extract_semantic_version(
    text: str,
) -> tuple[int, int, int] | None:
        if not text:
            return None
        match = re.search(r"\b(\d+)\.(\d+)(?:\.(\d+))?\b", text)
        if not match:
            return None
        major = int(match.group(1))
        minor = int(match.group(2))
        patch = int(match.group(3) or 0)
        return (major, minor, patch)

