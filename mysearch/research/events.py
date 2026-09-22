"""结果事件抽取：奖项 / 票房类查询的答案与实体提取。

从 `mysearch/clients.py` 抽出的**事实抽取层**。输入是已经取回的页面文本与
结果集，输出是结构化的答案与实体名——不做 provider 调用、不读 config、
不碰实例状态。

依赖方向单向：本模块依赖 `query_routing`（查询与结果谓词）与
`postprocess`（文本清洗）；`clients` 在上层依赖本模块。

`MySearchClient` 保留同名方法作为一行委托，调用面不变。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from mysearch import query_routing
def _result_event_candidates(
    *,
    query: str,
    results: list[dict[str, Any]],
    limit: int = 5,
) -> list[dict[str, Any]]:
        if query_routing._looks_like_result_event_query(query.lower()):
            candidate_window = max(limit, 10)
            return sorted(
                results[:candidate_window],
                key=lambda item: query_routing._result_event_page_priority(query=query, item=item),
                reverse=True,
            )[:limit]
        return results[: min(limit, 3)]


def _extract_result_event_answer(
    *,
    query: str,
    results: list[dict[str, Any]],
) -> str:
        if not results:
            return ""

        query_lower = query.lower()
        award_query = query_routing._looks_like_award_result_query(query_lower)
        signal_texts: list[str] = []
        strict_texts: list[str] = []
        for item in _result_event_candidates(query=query, results=results, limit=5):
            title_text = str(item.get("title") or "").strip()
            snippet_text = str(item.get("snippet") or "").strip()
            content_text = str(item.get("content") or "").strip()
            url = str(item.get("url") or "")
            path = urlparse(url).path.lower()
            if award_query and query_routing._looks_like_query_year_mismatch(
                query=query_lower,
                text=f"{title_text} {snippet_text} {content_text} {url}",
            ):
                continue
            if (
                award_query
                and query_routing._looks_like_award_category_conflict(
                    query_lower=query_lower,
                    title_text=title_text,
                    snippet_text=snippet_text,
                    content_text=content_text,
                )
            ):
                continue
            candidate_texts = [text for text in (content_text, snippet_text) if text]
            title_only_allowed = (
                not award_query
                or query_routing._looks_like_award_winner_result(
                    title_text=title_text.lower(),
                    snippet_text="",
                    path=path,
                )
                or "winner" in title_text.lower()
                or "winners" in title_text.lower()
            )
            if title_only_allowed and title_text:
                candidate_texts.append(title_text)
            # 第一遍的材料先攒起来，**不要**在这里就返回：标题式/名单式的宽松
            # 命中会把"提名名单首项"当成获奖者，而正确表述可能排在后面的候选里
            # （实测 entertainment-03：LA Times 的提名名单抢在 ABC News 的
            #  `record of the year winner "luther"` 之前被抽走）。
            strict_texts.extend(t for t in candidate_texts if t)
            combined_item_text = "\n".join(
                value for value in (snippet_text, content_text, title_text) if value
            )
            if combined_item_text:
                signal_texts.append(combined_item_text)

        # 第一遍：严格模式，要求显式的获奖措辞。
        for text in strict_texts:
            answer = _strict_award_answer(query_lower=query_lower, text=text)
            if answer:
                return answer
        if signal_texts:
            answer = _strict_award_answer(
                query_lower=query_lower, text="\n".join(signal_texts)
            )
            if answer:
                return answer

        # 第二遍：宽松模式（原行为），只在严格模式全无命中时兜底。
        for text in strict_texts:
            answer = _extract_result_event_answer_from_text(
                query_lower=query_lower,
                text=text,
            )
            if answer:
                return answer
        if not signal_texts:
            return ""

        combined_text = "\n".join(signal_texts)
        return _extract_result_event_answer_from_text(
            query_lower=query_lower,
            text=combined_text,
        )


#: 每类奖项的**严格**答案模式：必须出现 won / wins / winner 这类"获奖"动词。
#:
#: 为什么需要第二遍扫描（实测 entertainment-03）：抽取器原本逐条尝试候选文本、
#: 第一条抽到就返回。LA Times 的页面在 `## Record of the year` 标题下给出的是
#: **提名名单**（“DtMF” — Bad Bunny 排在首位），宽松模式 `record of the year\s*[–—:]\s*`
#: 直接把它当成了获奖者；而同一批候选里 ABC News 写的是
#: `record of the year winner "luther," Kendrick Lamar With SZA`（**正确**），
#: 却因为 LA Times 排在前面而从未被尝试。
#:
#: 严格模式要求显式的获奖措辞，因此不会命中"标题 + 名单首项"。两遍扫描的次序
#: 是关键：**先**用严格模式扫完所有候选，找不到才退回宽松模式。
#: 类别 -> 输出标签。**不要**用 `str.title()`：它会把 "record of the year"
#: 变成 "Record Of The Year"，与既有输出格式（`Record of the Year winner: …`）
#: 不一致，而下游与测试按该格式断言。
_AWARD_LABELS = {
    "best picture": "Best Picture",
    "best actor": "Best Actor",
    "album of the year": "Album of the Year",
    "record of the year": "Record of the Year",
}

_STRICT_AWARD_PATTERNS: dict[str, tuple[list[str], list[str]]] = {
    "best picture": (
        [
            # 引号标题 + "is the …winner"：新闻报道最常见。
            r"[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]\s+is\s+the\s+(?:20\d{2}\s+)?best picture",
            # 引号 + won + 奖项品牌
            r"[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]\s+won\s+(?:the\s+)?(?:20\d{2}\s+)?(?:oscar|academy award)[^\n]{0,40}\bbest picture\b",
            # `… winner "X"`：紧跟在 winner 后的**引号**实体（ABC News 的措辞）。
            r"best picture\s+winner[\s:–—-]*(?:is\s+)?[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]",
            # `… winner: X`：**必须**带分隔符。曾写成可选，于是
            # `Best Picture winner at the Academy Awards` 把 "at the Academy Awards"
            # 当成了实体 —— 由 test_..._from_headline_style_result 抓到。
            r"best picture\s+winner\s*[:\-–—]\s*(?:is\s+)?([^\n.;\"”’']{2,100})",
            r"best picture[^\n]{0,30}\b(?:goes to|went to|awarded to)\b\s*[\"“'‘]?([^\n.;\"”’']{2,100})",
        ],
        ["presented annually", "recognizes", "is an award"],
    ),
    "best actor": (
        [
            # 引号标题 + "is the …winner"：新闻报道最常见。
            r"[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]\s+is\s+the\s+(?:20\d{2}\s+)?best actor",
            # 引号 + won + 奖项品牌
            r"[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]\s+won\s+(?:the\s+)?(?:20\d{2}\s+)?(?:oscar|academy award)[^\n]{0,40}\bbest actor\b",
            # `… winner "X"`：紧跟在 winner 后的**引号**实体（ABC News 的措辞）。
            r"best actor\s+winner[\s:–—-]*(?:is\s+)?[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]",
            # `… winner: X`：**必须**带分隔符。曾写成可选，于是
            # `Best Picture winner at the Academy Awards` 把 "at the Academy Awards"
            # 当成了实体 —— 由 test_..._from_headline_style_result 抓到。
            r"best actor\s+winner\s*[:\-–—]\s*(?:is\s+)?([^\n.;\"”’']{2,100})",
            r"best actor[^\n]{0,30}\b(?:goes to|went to|awarded to)\b\s*[\"“'‘]?([^\n.;\"”’']{2,100})",
        ],
        ["actress", "supporting", "nominee", "nominees", "presented annually"],
    ),
    "album of the year": (
        [
            # 引号标题 + "is the …winner"：新闻报道最常见。
            r"[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]\s+is\s+the\s+(?:20\d{2}\s+)?album of the year",
            # 引号 + won + 奖项品牌
            r"[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]\s+won\s+(?:the\s+)?(?:20\d{2}\s+)?(?:grammy)[^\n]{0,40}\balbum of the year\b",
            # `… winner "X"`：紧跟在 winner 后的**引号**实体（ABC News 的措辞）。
            r"album of the year\s+winner[\s:–—-]*(?:is\s+)?[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]",
            # `… winner: X`：**必须**带分隔符。曾写成可选，于是
            # `Best Picture winner at the Academy Awards` 把 "at the Academy Awards"
            # 当成了实体 —— 由 test_..._from_headline_style_result 抓到。
            r"album of the year\s+winner\s*[:\-–—]\s*(?:is\s+)?([^\n.;\"”’']{2,100})",
            r"album of the year[^\n]{0,30}\b(?:goes to|went to|awarded to)\b\s*[\"“'‘]?([^\n.;\"”’']{2,100})",
        ],
        ["nominee", "nominees", "presented annually"],
    ),
    "record of the year": (
        [
            # 引号标题 + "is the …winner"：新闻报道最常见。
            r"[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]\s+is\s+the\s+(?:20\d{2}\s+)?record of the year",
            # 引号 + won + 奖项品牌
            r"[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]\s+won\s+(?:the\s+)?(?:20\d{2}\s+)?(?:grammy)[^\n]{0,40}\brecord of the year\b",
            # `… winner "X"`：紧跟在 winner 后的**引号**实体（ABC News 的措辞）。
            r"record of the year\s+winner[\s:–—-]*(?:is\s+)?[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]",
            # `… winner: X`：**必须**带分隔符。曾写成可选，于是
            # `Best Picture winner at the Academy Awards` 把 "at the Academy Awards"
            # 当成了实体 —— 由 test_..._from_headline_style_result 抓到。
            r"record of the year\s+winner\s*[:\-–—]\s*(?:is\s+)?([^\n.;\"”’']{2,100})",
            r"record of the year[^\n]{0,30}\b(?:goes to|went to|awarded to)\b\s*[\"“'‘]?([^\n.;\"”’']{2,100})",
        ],
        ["nominee", "nominees", "presented annually"],
    ),
}


def _strict_award_answer(*, query_lower: str, text: str) -> str:
    """只在文本**明确写出获奖者**时返回答案，否则返回空串。

    与 `_extract_result_event_answer_from_text` 的区别是后者接受
    "`<奖项>:` + 首项"这种名单式表述，会把提名当成获奖。本函数的模式都要求
    `winner` / `won` / `goes to` 之类的显式获奖措辞。
    """
    if not text:
        return ""
    text = re.sub(
        r"\[([^\]\n]+)\]\(https?://[^)\n]+\)",
        r"\1",
        text,
        flags=re.IGNORECASE,
    )
    for category, (patterns, reject) in _STRICT_AWARD_PATTERNS.items():
        if category not in query_lower:
            continue
        entity = _extract_named_fact_entity(text, patterns=patterns, reject_substrings=reject)
        if entity:
            return f"{_AWARD_LABELS[category]} winner: {entity}"
    return ""


def _extract_result_event_answer_from_text(
    *,
    query_lower: str,
    text: str,
) -> str:
        if not text:
            return ""

        text = re.sub(
            r"\[([^\]\n]+)\]\(https?://[^)\n]+\)",
            r"\1",
            text,
            flags=re.IGNORECASE,
        )

        if "best picture" in query_lower or "最佳影片" in query_lower:
            entity = _extract_named_fact_entity(
                text,
                patterns=[
                    r"[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]\s+won[^\n]{0,80}\bbest picture\b",
                    r"[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]\s+is\s+the\s+(?:20\d{2}\s+)?best picture winner",
                    r"best picture\s+winner\s+([^\n.;]{2,100})",
                    r"best picture\s*\.\s*winner\s*[\.\-–—: ]+\s*([^\n.;]{2,100})",
                    r"best picture\s*[–—:]\s*[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]",
                    r"best picture(?:\s+winner)?(?:\s*[–—:]|\s+was|\s+is|\s+goes to|\s+went to)\s+[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]",
                    r"best picture\s*[–—:]\s*([^\n.;]{2,100})",
                    r"best picture(?:\s+winner)?(?:\s*[–—:]|\s+was|\s+is|\s+goes to|\s+went to)\s+([^\n.;]{2,100})",
                ],
            )
            if entity:
                return f"Best Picture winner: {entity}"

        if "best actor" in query_lower or "最佳男主角" in query_lower:
            entity = _extract_named_fact_entity(
                text,
                patterns=[
                    r"(?:^|[.!?]\s+)([A-Z][A-Za-z0-9'’&.\- ]{2,80}?)\s+and\s+[A-Z][A-Za-z0-9'’&.\- ]{2,80}\s+won\s+best actor\s+and\s+best actress\b",
                    r"[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]\s+won[^\n]{0,80}\bbest actor\b",
                    r"([A-Z][A-Za-z0-9'’&.\- ]{2,100})\s+wins\s+best actor",
                    r"([A-Z][A-Za-z0-9'’&.\- ]{2,100})\s+is\s+the\s+(?:20\d{2}\s+)?best actor winner",
                    r"best actor\s+winner\s+([^\n.;]{2,100})",
                    r"best actor\s*[–—:]\s*([^\n.;]{2,100})",
                    r"best actor(?:\s+winner)?(?:\s+was|\s+is|\s+goes to|\s+went to)?\s+([^\n.;]{2,100})",
                    r"([A-Z][A-Za-z0-9'’&.\- ]{2,100})\s+won\s+best actor",
                ],
                reject_substrings=[
                    "actress",
                    "award",
                    "nominee",
                    "nominees",
                    "supporting",
                    "winner",
                ],
            )
            if entity:
                return f"Best Actor winner: {entity}"

        if any(token in query_lower for token in ("album of the year", "aoty", "最佳专辑")):
            entity = _extract_album_of_the_year_entity(text)
            if entity:
                return f"Album of the Year winner: {entity}"
            entity = _extract_named_fact_entity(
                text,
                patterns=[
                    r"[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]\s+(?:won|wins)[^\n]{0,80}\balbum of the year\b",
                    r"[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]\s+[–—:]\s*album of the year",
                    r"album of the year\s+winner\s+([^\n.;]{2,100})",
                    r"album of the year\s*\.\s*winner\s*[\.\-–—: ]+\s*([^\n.;]{2,100})",
                    r"album of the year\s*[·•]\s*([^\n.;]{2,100})",
                    r"album of the year\s*[–—:]\s*([^\n.;]{2,100})",
                    r"album of the year(?:\s+winner)?(?:\s+was|\s+is|\s+goes to|\s+went to)\s+([^\n.;]{2,100})",
                    r"([^\n.;]{2,100})\s+won\s+album of the year",
                    r"([^\n.;]{2,100})\s+(?:won|wins)[^\n]{0,40}\balbum of the year\b",
                ],
                reject_substrings=[
                    "award",
                    "winner",
                    "nominee",
                    "nominees",
                    "best new artist",
                    "his album",
                    "her album",
                    "their album",
                    "its album",
                    "the album",
                    "record of the year",
                    "song of the year",
                    "won ",
                ],
            )
            if entity:
                return f"Album of the Year winner: {entity}"

        if "record of the year" in query_lower or "最佳歌曲" in query_lower:
            entity = _extract_named_fact_entity(
                text,
                patterns=[
                    r"[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]\s+(?:won|wins)[^\n]{0,80}\brecord of the year\b",
                    r"[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]\s+[–—:]\s*record of the year",
                    r"record of the year\s+winner\s+([^\n.;]{2,100})",
                    r"record of the year\s*[–—:]\s*([^\n.;]{2,100})",
                    r"record of the year(?:\s+winner)?(?:\s+was|\s+is|\s+goes to|\s+went to)?\s+([^\n.;]{2,100})",
                    r"([^\n.;]{2,100})\s+wins\s+record of the year",
                    r"([^\n.;]{2,100})\s+won\s+record of the year",
                ],
                reject_substrings=[
                    "album of the year",
                    "award",
                    "nominee",
                    "nominees",
                    "song of the year",
                    "winner",
                ],
            )
            if entity:
                return f"Record of the Year winner: {entity}"

        if query_routing._looks_like_box_office_query(query_lower):
            entity = _extract_named_fact_entity(
                text,
                patterns=[
                    r"[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]\s+(?:becomes|become|became|scores|scored|tops|topped)[^\n]{0,80}(?:highest-grossing|biggest opening|opening weekend|box office)",
                    r"([A-Z][A-Za-z0-9:,'’&\- ]{2,100})\s+(?:becomes|become|became|scores|scored|tops|topped)[^\n]{0,80}(?:highest-grossing|biggest opening|opening weekend|box office)",
                ],
            )
            if entity:
                return f"Top opening-weekend title: {entity}"

        return ""


def _extract_album_of_the_year_entity(
    text: str,
) -> str:
        duo_patterns = [
            r"([A-Z][A-Za-z0-9&'’.\- ]{1,80}) won album of the year for (?:his|her|their|its) album[_*\s]+([^_\n.;]{2,120})",
            r"([A-Z][A-Za-z0-9&'’.\- ]{1,80}) won album of the year for the album[_*\s]+([^_\n.;]{2,120})",
            r"([A-Z][A-Za-z0-9&'’.\- ]{1,80}) won album of the year for[_*\s]+([^_\n.;]{2,120})",
            r"([A-Z][A-Za-z0-9&'’.\- ]{1,80}) wins album of the year for[_*\s]+([^_\n.;]{2,120})",
        ]
        for pattern in duo_patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            artist = _clean_extracted_fact_entity(
                match.group(1),
                reject_substrings=[
                    "award",
                    "winner",
                    "nominee",
                    "nominees",
                    "best new artist",
                    "his album",
                    "her album",
                    "their album",
                    "its album",
                    "the album",
                    "collaboration",
                    "his ",
                    "her ",
                    "their ",
                    "its ",
                ],
            )
            album = _clean_extracted_fact_entity(
                match.group(2),
                reject_substrings=[
                    "award",
                    "winner",
                    "nominee",
                    "nominees",
                    "best new artist",
                    "his album",
                    "her album",
                    "their album",
                    "its album",
                    "the album",
                ],
            )
            if album and artist:
                return f"{album} by {artist}"
            if album:
                return album
        album_only_patterns = [
            r"won album of the year for (?:his|her|their|its) album[_*\s]+([^_\n.;]{2,120})",
            r"won album of the year for the album[_*\s]+([^_\n.;]{2,120})",
        ]
        for pattern in album_only_patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            album = _clean_extracted_fact_entity(
                match.group(1),
                reject_substrings=[
                    "award",
                    "winner",
                    "nominee",
                    "nominees",
                    "best new artist",
                    "his album",
                    "her album",
                    "their album",
                    "its album",
                    "the album",
                ],
            )
            if album:
                return album
        artist_only_patterns = [
            r"([A-Z][A-Za-z0-9&'’.\- ]{1,80})['’]s win for album of the year",
            r"([A-Z][A-Za-z0-9&'’.\- ]{1,80})['’]s album of the year win",
            r"([A-Z][A-Za-z0-9&'’.\- ]{1,80})\s+wins\s+the\s+grammy\s+for\s+album\s+of\s+the\s+year",
            r"([A-Z][A-Za-z0-9&'’.\- ]{1,80})\s+won\s+the\s+grammy\s+for\s+album\s+of\s+the\s+year",
        ]
        for pattern in artist_only_patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            artist = _clean_extracted_fact_entity(
                match.group(1),
                reject_substrings=[
                    "award",
                    "winner",
                    "nominee",
                    "nominees",
                    "best new artist",
                    "his album",
                    "her album",
                    "their album",
                    "its album",
                    "the album",
                ],
            )
            if artist:
                return artist
        return ""


def _extract_named_fact_entity(
    text: str,
    *,
    patterns: list[str],
    reject_substrings: list[str] | None = None,
) -> str:
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            entity = _clean_extracted_fact_entity(
                match.group(1),
                reject_substrings=reject_substrings,
            )
            if entity:
                return entity
        return ""


def _clean_extracted_fact_entity(
    value: str,
    *,
    reject_substrings: list[str] | None = None,
) -> str:
        entity = re.sub(r"\s+", " ", value).strip(" \t\r\n-:;,.\"'“”‘’")
        entity = re.sub(r"^[#>*`]+\s*", "", entity)
        entity = re.sub(r"^[·•]+\s*", "", entity)
        entity = re.sub(r"^(?:winner|winners)\s*[:\-]\s*", "", entity, flags=re.IGNORECASE)
        entity = re.sub(r"['’]s\s+win\b.*$", "", entity, flags=re.IGNORECASE)
        entity = re.split(r"\s+(?:with|which|that|during|for)\s+", entity, maxsplit=1)[0]
        entity = re.split(r"\s*\|\s*", entity, maxsplit=1)[0]
        entity = re.split(r",\s*(?:[\"“]|[A-Z][A-Za-z])", entity, maxsplit=1)[0]
        entity = re.split(r",\s*(?:marking|while|as|where|when)\b", entity, maxsplit=1, flags=re.IGNORECASE)[0]
        entity = re.split(r"\s{2,}", entity, maxsplit=1)[0]
        entity = re.sub(r"\s+\((?:winner|winners)\)$", "", entity, flags=re.IGNORECASE).strip()
        entity = entity.strip(" \t\r\n-:;,.\"'“”‘’·•")
        if len(entity) < 2:
            return ""
        if query_routing._looks_like_publisher_fragment(entity):
            return ""
        if reject_substrings:
            entity_lower = entity.lower()
            if any(token in entity_lower for token in reject_substrings):
                return ""
        return entity

