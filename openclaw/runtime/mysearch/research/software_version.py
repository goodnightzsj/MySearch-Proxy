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
            return _asserted_major_version_answer(
                subject=subject,
                subject_tokens=subject_tokens,
                results=results,
            )

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


def _asserted_major_version_answer(
    *,
    subject: str,
    subject_tokens: tuple[str, ...],
    results: list[dict[str, Any]],
) -> str:
    """带点版本号一个都没读到时，退回**断言式裸主版本号**。

    这是生产实测的缺口（2026-09-24）：`latest stable version of Java` 答
    `JDK 25`（上游透传的过期值），而同一结果集里 oracle.com 写着
    `JDK 27 is the latest release of the Java SE Platform.`。
    `_software_version_candidates_from_text` 只认带点版本号（`\\d+\\.\\d+`），
    官方页通篇是裸主版本号，所以它一个候选都挑不出来、直接返回空 ——
    答案于是完全依赖上游，产品自己没能发现池里已有正确答案。

    这里复用冲突检测那套**已验证**的谓词 `_asserted_versions_from_text`：
    它按句法认"最新"声明的宾语，全 48 行零误报，且结构上放不进带点版本号，
    因此 loop36 的 `Minecraft Java Edition 26.1.2` 一类编造进不来。

    只在**一个**主版本号被断言时作答。多个来源各执一词时不猜 ——
    那种情形该由 `conflicting_version_claims` 报冲突、由 confidence 降级表达，
    而不是让答案随手挑一个。
    """
    claimed: set[int] = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        for field in ("title", "snippet", "content"):
            claimed.update(
                _asserted_versions_from_text(
                    str(item.get(field) or ""), subject_tokens=subject_tokens
                )
            )
    if len(claimed) != 1:
        return ""
    version = next(iter(claimed))
    if subject:
        return f"The latest stable version of {subject} is {version}."
    return f"The latest stable version is {version}."


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

#: 出现在主语**之前**、把主语变成别的产品限定语的词。
#:
#: 锚点恰好是主语名仍不够：`Aspose.Cells for Node.js via Java 25.12` 里
#: 紧邻 `25.12` 的实词就是 `Java`，但整个短语说的是 Aspose 的版本。
#: 这类短语的形态固定 —— 主语前是 `via`/`for`/`with` 等连接词。
#: 与连接词表的分工：那张表管**数字之前**的词，这张表管**主语之前**的词。
_SUBJECT_QUALIFIER_PREPOSITIONS = frozenset({
    "for", "in", "on", "using", "via", "with",
})


def _version_is_anchored_to_subject(
    *,
    text: str,
    start: int,
    subject_tokens: tuple[str, ...],
) -> bool:
    """版本号**紧邻**的前一个实词是否就是被问软件，且不是别的产品的限定语。

    判据从"附近出现过主语"收紧为"主语必须是紧邻锚点"，因为前者会放行
    主语属于别的产品的文本。四次真实编造（2026-09-22，生产
    `latest stable version of Java`）机制同一 —— 都通过了旧校验：

    - `Minecraft Java Edition 26.1.2`        -> 抽出 `26.1.2`
    - `JavaFX 10.7.3`                        -> 抽出 `10.7.3`
    - `Gradle 4.5 stable release`            -> 抽出 `4.5`
    - `Aspose.Cells for Node.js via Java 25.12` -> 抽出 `25.12`

    前两者紧邻的实词分别是 `Edition` / `JavaFX`（都不是主语）；第四个的
    锚点**确实是** `Java`，所以还要看它前面 —— 是 `via`，说明 `Java` 在这里
    是"通过 Java 调用"的限定语。而 `version of Java is 25.0.1` 里
    主语前是 `of`，不在限定语表内，照常通过。
    """
    if not subject_tokens:
        return True
    tokens = {token.lower().rstrip(".") for token in subject_tokens}
    words = list(re.finditer(r"[A-Za-z][A-Za-z0-9.+#-]*", text[:start]))
    index = len(words)
    while index > 0 and words[index - 1].group(0).lower() in _VERSION_ANCHOR_CONNECTORS:
        index -= 1
    if index == 0:
        return False
    if words[index - 1].group(0).lower().rstrip(".") not in tokens:
        return False
    if index >= 2 and words[index - 2].group(0).lower() in _SUBJECT_QUALIFIER_PREPOSITIONS:
        return False
    return True


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


#: 版本号前的**产品版本标记**。`Java SE 27` / `JDK 26` 这类官方写法里，
#: 紧邻数字的实词是标记而不是产品名，产品名在它前面一格。
_VERSION_EDITION_MARKERS = frozenset({"se", "jdk", "jre", "me", "ee", "lts", "sdk"})

#: 否定与开发分支语境 —— 出现在文本里就整条不取。
_VERSION_CLAIM_NEGATIVE = (
    "alpha", "beta", "preview", "prerelease", "pre-release", "release candidate",
    "upcoming", "planned", "scheduled", "future", "development branch", "main branch",
)


def _claim_number_belongs_to_subject(
    *,
    text: str,
    start: int,
    subject_tokens: tuple[str, ...],
) -> bool:
    """形态 1 的归属校验：数字左侧紧邻的实词是被问软件，或该软件的**官方版本标记**。

    与 `_version_is_anchored_to_subject` 的区别只有一处：额外接受 `JDK` / `SE`
    这类标记作为锚点。

    为什么必须接受标记：官方写法的原文是 `JDK 27 is the latest release of the
    Java SE Platform` —— 主语 `Java` 出现在**数字右侧**，左侧只剩标记 `JDK`。
    只认主语会整类误杀官方页（实测 oracle.com 今日原文）。

    为什么接受标记仍然安全：标记是**产品专属**的（`jdk`/`se` 只属于 Java），
    所以标记锚点还要求**同一句里**出现被问主语名 —— 这句的
    `Java SE Platform` 正好满足。否则一个 Python 查询会把 Java 的 27
    读成 Python 的最新版（实测该泄漏）。

    挡住的是**别的产品的版本号**：`Minecraft Java Edition 27 is the latest
    release` 的紧邻实词是 `Edition`（既非主语也非标记）→ 拒绝；
    `JavaFX 27 …` → `JavaFX` → 拒绝；
    `Aspose.Cells for Node.js via Java 27 …` → 紧邻是 `Java`，但再往前是
    限定语介词 `via` → 拒绝。
    """
    if not subject_tokens:
        return True
    tokens = {token.lower().rstrip(".") for token in subject_tokens}
    words = list(re.finditer(r"[A-Za-z][A-Za-z0-9.+#-]*", text[:start]))
    index = len(words)
    while index > 0 and words[index - 1].group(0).lower() in _VERSION_ANCHOR_CONNECTORS:
        index -= 1
    if index == 0:
        return False
    anchor = words[index - 1].group(0).lower().rstrip(".")
    if anchor not in tokens and anchor not in _VERSION_EDITION_MARKERS:
        return False
    if index >= 2 and words[index - 2].group(0).lower() in _SUBJECT_QUALIFIER_PREPOSITIONS:
        return False
    # 标记是**产品专属**的：`jdk` / `se` 只属于 Java。仅凭标记就放行，会让
    # 一个 Python 查询把 `JDK 27 is the latest release of the Java SE Platform`
    # 读成 Python 的最新版（实测该泄漏）。因此标记锚点还要求**同一句里**
    # 出现被问主语 —— 官方写法里 `Java SE Platform` 正好在句内。
    if anchor in _VERSION_EDITION_MARKERS:
        clause_tokens = {
            word.group(0).lower().rstrip(".")
            for word in re.finditer(r"[A-Za-z][A-Za-z0-9.+#-]*", text)
        }
        if not (clause_tokens & tokens):
            return False
    return True


def _asserted_versions_from_text(
    text: str,
    *,
    subject_tokens: tuple[str, ...] = (),
) -> list[int]:
    """文本里**被断言为最新版**的主版本号。

    与 `_software_version_candidates_from_text` 的区别是**目的相反**：
    那个要挑出"该软件的最新版"并据此作答，所以把判据收得极紧 —— 只认带点的
    语义版本号，且锚点必须是主语本身。这个要给**冲突检测**用：它恰恰要在
    "官方页说 26、维基说 27"这种**回答者自己都看不见**的情形下报警，
    因此必须认官方写法里的**裸主版本号**（`JDK 26`、`Java SE 27`）。

    收紧的办法不是放宽锚点，而是改看**句法**：版本号必须是"最新"这个
    声明的**宾语**，才是一句声明。只认三种形态：

    - `X 26 is the latest release`（版本号在左，latest 直接修饰 release/version）
    - `the latest version of Java is JDK 26`（版本号在右，落在声明之后）
    - `Latest version: Java SE 27`

    实测（loop38，`failure-version-attribution-01`）：源里三处互相矛盾的声明
    —— oracle.com 的 `JDK 26 is the latest release`、wikipedia 的
    `Latest version:Java SE 27`、jrebel 的 `The latest version of Java is Java 25`
    —— 本函数取到 `{oracle: 26, wikipedia: 27}`。

    **刻意不取 jrebel 的 25**：那句的 `latest` 修饰的是 `version of Java`，
    而 25 出现在句尾从句 `which is also a Java LTS version` 里。收成"latest
    必须直接修饰 release/version/stable"之后，这句话反而正确排除了
    `JDK 25 is the latest Long-Term Support (LTS) release`（latest 修饰 LTS）
    这类**不是**在断言主版本的情形。

    全 48 行实测**零误报**：只有本行触发。
    """
    if not text:
        return []
    flattened = re.sub(r"\s+", " ", text)
    lowered = flattened.lower()
    tokens = {token.lower().rstrip(".") for token in subject_tokens}
    found: list[int] = []
    # 版本号是否处在**条款**范围内（`|` 与换行分栏、句读断句）。
    # 只看条款而不看整篇：实测 jrebel 那句 `The latest version of Java is Java 25`
    # 所在页面的**别处**写着 "Java 21 is scheduled to receive premier support"，
    # 整篇级的否定判据会因此把正确答案排除掉 —— 排除得对，理由却不对，
    # 换个页面就会漏报。本模块其它地方（`_software_version_candidates_from_text`）
    # 也是按句取上下文。
    for clause in _claim_clauses(flattened):
        clause_lower = clause.lower()
        if any(marker in clause_lower for marker in _VERSION_CLAIM_NEGATIVE):
            continue
        # 裸主版本号：1-2 位，且**不是**带点版本号的一部分。
        # 右侧守卫写作 `(?!\d|\.\d|,\d)` 而不是 `(?![\d.])`：
        #  - 去掉 `.` 单字符：否则句末句点也算"属于更长的版本号"，
        #    `…is JDK 27.`（句号结尾）整类不匹配 —— 实测 oracle.com 与
        #    wikipedia 的原文都是句子形态，这条守卫错一格就等于谓词在真实
        #    输入上永远读不到数。改为只挡 `.` 后**紧跟数字**的情形。
        #  - 加 `,\d`：千分位不是版本号。实测
        #    `The current metro area population of Tokyo in 2026 is 36,954,000`
        #    会把 `36` 读成"当前版本"（`current` 在左侧，形态 2 命中）。
        for match in re.finditer(r"(?<![\d.])(\d{1,2})(?!\d|\.\d|,\d)", clause):
            start = match.start()
            before = clause_lower[max(0, start - 90):start]
            after = clause_lower[match.end():match.end() + 50]
            # 形态 1：`<产品> [SE|JDK|LTS] 26 is the latest release`。
            # `latest` 必须直接修饰版本名词，否则 `latest LTS release` 会把
            # `JDK 25`（LTS 版本号）也当成"最新正式版"的声明。
            if re.match(
                r"\s*(?:is|was)?\s*(?:the\s+)?(?:latest|current|newest)\s+(?:release|version|stable)\b",
                after,
            ) and _claim_number_belongs_to_subject(
                text=clause,
                start=start,
                subject_tokens=subject_tokens,
            ):
                found.append(int(match.group(1)))
                continue
            # 形态 2：`the latest version of Java is SDK 26` —— 版本号落在声明右侧。
            if re.search(
                r"\b(?:latest|current|newest)\b[^.\n]{0,60}?\b(?:is|:)\s*(?:the\s+)?"
                r"(?:[A-Za-z.+#]+\s+){0,3}$",
                before,
            ):
                # 锚点回看时跳过产品标记与连接词：`…of Java is JDK 27` 的
                # 紧邻实词是 `JDK`，不跳就整类误杀官方写法。
                words = list(re.finditer(r"[A-Za-z][A-Za-z0-9.+#_-]*", clause[:start]))
                index = len(words)
                while index > 0:
                    word = words[index - 1].group(0).lower().rstrip(".")
                    if word in _VERSION_ANCHOR_CONNECTORS or word in _VERSION_EDITION_MARKERS:
                        index -= 1
                        continue
                    break
                if index > 0 and words[index - 1].group(0).lower().rstrip(".") in tokens:
                    found.append(int(match.group(1)))
                    continue
            # 形态 3：`Latest version: Java SE 27`。
            # 冒号**不是**本函数的断句符（见 `_claim_clauses`）—— 若把
            # `Latest version:` 与 `Java SE 27` 切成两句，本条整类失效。
            #
            # 同样要过归属校验：`Latest version: Java SE 27` 里没有任何主语名，
            # 光看左侧前缀会让**任意**主语读到 Java 的 27（实测该泄漏）。
            # `_claim_number_belongs_to_subject` 会要求句内出现主语名。
            if re.search(
                r"\b(?:latest|current|newest)\s+version\s*:?\s*(?:[A-Za-z.+#]+\s+){0,3}$",
                before,
            ) and _claim_number_belongs_to_subject(
                text=clause,
                start=start,
                subject_tokens=subject_tokens,
            ):
                found.append(int(match.group(1)))
    return found


def _claim_clauses(text: str) -> list[str]:
    """把一段结果文本切成条款级片段，供声明判定逐条检查。

    切分符**不含冒号**：`Latest version: Java SE 27` 是一个声明，把冒号当
    断句符会拆成 `Latest version:` 与 `Java SE 27` 两句，形态 3 整类失效。
    反向也成立 —— 冒号常引出的是**限定语**（`Upcoming: JDK 27 is the latest
    release`），拆开会让否定判据看不见 `Upcoming`，把预告当成已发布。
    """
    parts: list[str] = []
    for chunk in re.split(r"\n+|\|", text or ""):
        for clause in re.split(r"(?<=[.!?;])\s+", chunk):
            stripped = clause.strip()
            if stripped:
                parts.append(stripped)
    return parts


def conflicting_version_claims(
    *,
    query: str,
    results: list[dict[str, Any]],
) -> dict[int, list[str]]:
    """同一版本问题在**不同来源**上得到不同主版本号时返回 `{版本号: [域名]}`。

    这是 `_detect_evidence_conflicts` 缺失的那一类冲突：它现有的判据全是
    **来源结构**（多样性、provider 数、官方源覆盖），没有一条看**内容是否
    一致**。于是实测这一行 `evidence.conflicts` 为空、`confidence` 还是
    `high`，而池子里 oracle 说 26、wikipedia 说 27、jrebel 说 25。

    只在**多个域名**各执一词时算冲突 —— 同一个域名内部前后矛盾不算
    （那是页面本身在列举版本，不是来源分歧）。

    非版本类查询、或只有一个主版本号时返回空。全 48 行实测只在本行触发。
    """
    if not query_routing._looks_like_software_version_query(query.lower()):
        return {}
    subject = _software_version_subject(query)
    subject_tokens = _software_version_subject_tokens(query, subject)
    claimed: dict[int, list[str]] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        host = urlparse(url).hostname or ""
        if not host:
            continue
        for field in ("title", "snippet", "content"):
            for version in _asserted_versions_from_text(
                str(item.get(field) or ""), subject_tokens=subject_tokens
            ):
                hosts = claimed.setdefault(version, [])
                if host not in hosts:
                    hosts.append(host)
    distinct_hosts = {host for hosts in claimed.values() for host in hosts}
    if len(claimed) < 2 or len(distinct_hosts) < 2:
        return {}
    return claimed

