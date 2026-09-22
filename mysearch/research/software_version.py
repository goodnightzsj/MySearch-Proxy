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
from typing import Any, Mapping
from urllib.parse import urlparse

from mysearch import postprocess
from mysearch import query_routing
from mysearch.research import sections
from mysearch.types import ResolvedSearchIntent, SearchMode

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
        subject_tokens = _software_version_subject_tokens(query, subject)
        candidates: list[tuple[int, tuple[int, int, int], int, str, int]] = []
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
                for version_text, version_tuple, signal_score in _software_version_candidates_from_text(
                    text, subject_tokens=subject_tokens
                ):
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
                candidates.append(
                    (item_score + signal_score, version_tuple, -index, version_text, signal_score)
                )

        if not candidates:
            return ""

        best_score = max(item[0] for item in candidates)
        shortlist = [item for item in candidates if item[0] >= best_score - 1]
        # 先按**证据质量**取，版本号大小只作为同级兜底。
        #
        # 原实现是 `max(shortlist, key=lambda item: (item[1], item[0], item[2]))`，
        # 以版本元组为**第一**排序键 —— 等价于"取版本号数值最大者"。实测后果：
        # 页面上任何更大的无关数字都会赢，于是抽出了源文本里根本不存在的
        # "The latest stable version of Java is 4.5." / "…is 10.7.3."（同一查询两次
        # 不同结果、皆无出处）。已验证 `10.7.3` 在上游语境里是 JavaFX 的版本，
        # 被主语共现校验挡下后，这里再保证"证据最强"优先于"数字最大"。
        # 主语校验通过后，候选**都属于被问的软件**，此时"版本号最大者即最新版"
        # 才是成立的启发式 —— 恢复原有的按版本元组取最大。
        _, _, _, version_text, _ = max(
            shortlist, key=lambda item: (item[1], item[0], item[2])
        )
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


#: 版本号与主语之间允许出现的连接词（系词与虚词）。
#:
#: 刻意**不**收录 `version` / `release` / `edition`：它们出现在主语与数字之间时，
#: 恰恰说明主语名属于别的产品 —— `Minecraft Java Edition 26.1.2`、
#: `Minecraft Java version is 26.1.2`。把 `Java 25 LTS` 这类正确写法
#: 误伤的风险，远小于放行 Minecraft 版本号冒充 Java 的代价。
_VERSION_ANCHOR_CONNECTORS = frozenset({
    "a", "an", "are", "as", "at", "be", "been", "for", "in", "is", "of", "on",
    "or", "the", "to", "was", "were", "with",
})


def _version_is_anchored_to_subject(
    *,
    text: str,
    start: int,
    subject_tokens: tuple[str, ...],
) -> bool:
    """版本号**紧邻**的前一个实词是否就是被问软件。

    判据从"附近出现过主语"收紧为"主语必须是紧邻锚点"，因为前者会放行
    主语属于别的产品的文本。三次真实编造（2026-09-22，生产
    `latest stable version of Java`）机制同一 —— 都通过了旧校验：

    - `Minecraft Java Edition 26.1.2` -> 抽出 `26.1.2`
    - `JavaFX 10.7.3`                -> 抽出 `10.7.3`
    - `Gradle 4.5 stable release`    -> 抽出 `4.5`

    锚定后：前两者紧邻的实词分别是 `Edition` / `JavaFX`（都不是主语），
    自然被挡；而 `version of Java is 25.0.1` 跳过虚词后锚点正是 `Java`。
    """
    if not subject_tokens:
        return True
    tokens = {token.lower() for token in subject_tokens}
    words = list(re.finditer(r"[A-Za-z][A-Za-z0-9.+#-]*", text[:start]))
    index = len(words)
    while index > 0 and words[index - 1].group(0).lower() in _VERSION_ANCHOR_CONNECTORS:
        index -= 1
    if index == 0:
        return False
    return words[index - 1].group(0).lower().rstrip(".") in {
        token.rstrip(".") for token in tokens
    }


def _software_version_candidates_from_text(
    text: str,
    *,
    subject_tokens: tuple[str, ...] = (),
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
            # 紧邻锚点不是主语就丢弃：版本号在页面上存在，不代表它属于被问的软件。
            # `Minecraft Java Edition 26.1.2` 的锚点是 `Edition`，被这一步挡下。
            if not _version_is_anchored_to_subject(
                text=normalized,
                start=match.start(),
                subject_tokens=subject_tokens,
            ):
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


def _software_version_subject_tokens(query: str, subject: str) -> tuple[str, ...]:
    """返回用于"版本号归属"校验的软件名别名。

    同时给出原始 query token 与 `_software_version_subject` 归一化后的名字，
    两者取并集：查询里可能写 `node.js` 而归一化成 `Node.js`，任一形态出现
    都应算数。
    """
    tokens: list[str] = []
    # 句式词不能作锚点：`Java version is 26.1.2` 里紧邻数字的实词是 `version`，
    # 若它进了锚点集合，Minecraft 的版本号就会被判成"属于 Java"。
    sentence_tokens = {
        "current", "latest", "newest", "release", "stable", "version",
    }

    def _add(value: str) -> None:
        cleaned = value.strip().strip(".,;:!?()\"'")
        if len(cleaned) >= 2 and cleaned.lower() not in sentence_tokens and cleaned not in tokens:
            tokens.append(cleaned)

    if subject:
        _add(subject)
    for token in query_routing._query_brand_tokens(query):
        _add(token)
    return tuple(tokens)


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

