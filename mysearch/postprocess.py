"""结果后处理：合并、去重、排序、清洗、归一化。

从 `mysearch/clients.py` 抽出的纯函数层。这些函数不依赖任何实例状态
（`MySearchClient` 里 88% 的方法从不触碰 `config`/`keyring`/cache），
因此可以独立成模块，供编排层与 provider 适配器共同使用。

`MySearchClient` 保留同名方法作为一行委托，调用面不变。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time as dt_time, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse, urlunparse

#: hCaptcha 挑战页会列出这些语言名；出现在正文里即判定为挑战页污染。
_HCAPTCHA_LANGUAGES = frozenset(
    {
        "afrikaans", "albanian", "amharic", "arabic", "armenian", "azerbaijani",
        "basque", "belarusian", "bengali", "bulgarian", "bosnian", "burmese",
        "catalan", "cebuano", "chinese", "chinese simplified", "chinese traditional",
        "corsican", "croatian", "czech", "danish", "dutch", "english", "esperanto",
        "estonian", "filipino", "finnish", "french", "frisian", "galician",
        "georgian", "german", "greek", "gujarati", "haitian creole", "hausa",
        "hawaiian", "hebrew", "hindi", "hmong", "hungarian", "icelandic", "igbo",
        "indonesian", "irish", "italian", "japanese", "javanese", "kannada",
        "kazakh", "khmer", "kinyarwanda", "korean", "kurdish", "kyrgyz", "lao",
        "latin", "latvian", "lithuanian", "luxembourgish", "macedonian",
        "malagasy", "malay", "malayalam", "maltese", "maori", "marathi",
        "mongolian", "nepali", "norwegian", "nyanja", "odia", "pashto", "persian",
        "polish", "portuguese", "punjabi", "romanian", "russian", "samoan",
        "scots gaelic", "serbian", "sesotho", "shona", "sindhi", "sinhala",
        "slovak", "slovenian", "somali", "spanish", "sundanese", "swahili",
        "swedish", "tagalog", "tajik", "tamil", "tatar", "telugu", "thai",
        "turkish", "turkmen", "ukrainian", "urdu", "uyghur", "uzbek",
        "vietnamese", "welsh", "xhosa", "yiddish", "yoruba", "zulu",
    }
)


class PostprocessError(ValueError):
    """后处理输入非法。

    独立于 `mysearch.clients.MySearchError`，避免 `clients` 与 `postprocess`
    互相导入。调用方（`MySearchClient`）在委托层把它转换成 `MySearchError`，
    对外错误语义不变。
    """

def _result_published_timestamp(item: dict[str, Any]) -> float | None:
    for field in ("published_date", "publishedDate", "created_at"):
        parsed = _parse_result_timestamp(item.get(field))
        if parsed is not None:
            return parsed.timestamp()
    return None


def _merge_ranked_results(result_lists: list[list[dict[str, Any]]],
    *,
    max_results: int,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    indexes = [0 for _ in result_lists]

    while len(merged) < max_results and result_lists:
        progressed = False
        for list_index, items in enumerate(result_lists):
            current_index = indexes[list_index]
            if current_index >= len(items):
                continue
            candidate = dict(items[current_index])
            indexes[list_index] += 1
            progressed = True
            dedupe_key = _result_url_identity(str(candidate.get("url") or ""))
            if dedupe_key and dedupe_key in seen_urls:
                continue
            if dedupe_key:
                seen_urls.add(dedupe_key)
            merged.append(candidate)
            if len(merged) >= max_results:
                break
        if not progressed:
            break

    return merged


def _filter_results_by_domains(results: list[dict[str, Any]],
    *,
    include_domains: list[str] | None,
    exclude_domains: list[str] | None,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for item in results:
        hostname = _result_hostname(item)
        if include_domains and not any(
            _domain_matches(hostname, domain) for domain in include_domains
        ):
            continue
        if exclude_domains and any(
            _domain_matches(hostname, domain) for domain in exclude_domains
        ):
            continue
        filtered.append(dict(item))
    return filtered


def _strip_browser_challenge_block(text: str) -> str:
    lowered = text.lower()
    if (
        "checking your browser" not in lowered
        or "challenges.cloudflare.com" not in lowered
    ):
        return text

    paragraphs = text.split("\n\n")
    start = next(
        (
            index
            for index, paragraph in enumerate(paragraphs)
            if "checking your browser" in paragraph.lower()
        ),
        None,
    )
    if start is None:
        return text

    end = None
    for index in range(start, min(len(paragraphs), start + 16)):
        paragraph_lower = paragraphs[index].lower()
        if "cloudflare.com/privacypolicy" in paragraph_lower:
            end = index
            break
    if end is None:
        return text

    challenge_text = "\n\n".join(paragraphs[start : end + 1]).lower()
    if not (
        "verification failed" in challenge_text
        and "verification expired" in challenge_text
        and "challenge-platform" in challenge_text
    ):
        return text

    return "\n\n".join([*paragraphs[:start], *paragraphs[end + 1 :]])


def _strip_trailing_empty_headings(text: str) -> str:
    # Remove dangling heading-only paragraphs left at the very end after
    # widget removal (e.g. a lone trailing `### Filters` with no body).
    paragraphs = text.split("\n\n")
    while paragraphs:
        last = paragraphs[-1].strip()
        if last and "\n" not in last and re.match(r"^#{1,6}\s+\S", last):
            paragraphs.pop()
        else:
            break
    return "\n\n".join(paragraphs)


def _strip_hcaptcha_block(text: str) -> str:
    paragraphs = text.split("\n\n")
    total = len(paragraphs)
    is_language = [
        para.strip().lower() in _HCAPTCHA_LANGUAGES for para in paragraphs
    ]
    artifact = re.compile(
        r"^(hcaptcha|en|verify|ask ai|i am human|please try again.*"
        r"|\[hcaptcha logo[^\]]*\]\([^)]*\)|.*hcaptcha\.com.*)$",
        re.IGNORECASE | re.DOTALL,
    )
    remove: set[int] = set()
    index = 0
    while index < total:
        if is_language[index]:
            end = index
            while end < total and is_language[end]:
                end += 1
            # Only a long contiguous run is the hCaptcha language dropdown;
            # a stray language name in prose never reaches this threshold.
            if end - index >= 12:
                remove.update(range(index, end))
                back = index - 1
                while back >= 0 and (
                    not paragraphs[back].strip()
                    or artifact.match(paragraphs[back].strip())
                ):
                    remove.add(back)
                    back -= 1
                forward = end
                while forward < total and (
                    not paragraphs[forward].strip()
                    or artifact.match(paragraphs[forward].strip())
                ):
                    remove.add(forward)
                    forward += 1
            index = end
        else:
            index += 1
    if not remove:
        return text
    kept = [para for pos, para in enumerate(paragraphs) if pos not in remove]
    return "\n\n".join(kept)


def _social_result_identity(item: dict[str, Any]) -> str:
    explicit_handle = str(item.get("handle") or item.get("username") or "").strip()
    if explicit_handle:
        return explicit_handle.lstrip("@").strip().lower()
    title = str(item.get("title") or "").strip()
    handle_match = re.search(r"\(@?([A-Za-z0-9_]{1,32})\)", title)
    if handle_match:
        return handle_match.group(1).strip().lower()
    url = str(item.get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.netloc.lower().endswith(("x.com", "twitter.com")):
        path_parts = [part for part in parsed.path.split("/") if part]
        if path_parts:
            candidate = path_parts[0].strip().lstrip("@")
            if candidate and candidate.lower() not in {"i", "search", "home", "explore", "status"}:
                return candidate.lower()
    author = str(item.get("author") or "").strip()
    if author:
        return author.lstrip("@").strip().lower()
    return ""


def _diversify_social_results(results: list[dict[str, Any]],
    *,
    max_results: int,
    max_per_identity: int = 2,
) -> list[dict[str, Any]]:
    diversified: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for item in results:
        identity = _social_result_identity(item)
        if identity and counts.get(identity, 0) >= max_per_identity:
            continue
        diversified.append(item)
        if identity:
            counts[identity] = counts.get(identity, 0) + 1
        if len(diversified) >= max_results:
            break
    return diversified


def _filter_social_results_by_date(results: list[dict[str, Any]],
    *,
    from_date: str | None,
    to_date: str | None,
) -> list[dict[str, Any]]:
    if not from_date and not to_date:
        return results

    start = _parse_date_bound(from_date, end_of_day=False) if from_date else None
    end = _parse_date_bound(to_date, end_of_day=True) if to_date else None
    filtered: list[dict[str, Any]] = []
    for item in results:
        created_at = _parse_result_timestamp(item.get("created_at"))
        if created_at is None:
            filtered.append(item)
            continue
        if start is not None and created_at < start:
            continue
        if end is not None and created_at > end:
            continue
        filtered.append(item)
    return filtered


def _parse_date_bound(value: str, *, end_of_day: bool) -> datetime | None:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise PostprocessError(
            f"Invalid date format: '{value}'. Use ISO format YYYY-MM-DD."
        )
    bound_time = dt_time.max if end_of_day else dt_time.min
    return datetime.combine(parsed, bound_time).replace(tzinfo=timezone.utc)


def _parse_result_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value.strip())
        except (TypeError, ValueError, IndexError):
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _extract_social_gateway_results(response: dict[str, Any]) -> list[Any]:
    for key in ("results", "items", "posts", "tweets"):
        value = response.get(key)
        if isinstance(value, list):
            return value

    data = response.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("results", "items", "posts", "tweets"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def _extract_social_gateway_citations(response: dict[str, Any],
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not results:
        return []

    raw = response.get("citations") or response.get("sources") or []
    citations = []
    seen: set[str] = set()
    allowed_urls = {
        item.get("url", "")
        for item in results
        if isinstance(item, dict) and item.get("url")
    }

    if isinstance(raw, list):
        for item in raw:
            citation = _normalize_citation(item)
            if citation is None:
                continue
            url = citation.get("url", "")
            if allowed_urls and url and url not in allowed_urls:
                continue
            if url and url in seen:
                continue
            if url:
                seen.add(url)
            citations.append(citation)

    if citations:
        return citations

    for item in results:
        url = item.get("url", "")
        if not url or url in seen:
            continue
        seen.add(url)
        citations.append({"title": item.get("title", ""), "url": url})

    return citations


def _align_citations_with_results(*,
    results: list[dict[str, Any]],
    citations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    synthesized = [
        {"title": item.get("title", ""), "url": item.get("url", "")}
        for item in results
        if item.get("url")
    ]
    normalized = _dedupe_citations(citations, synthesized)
    citations_by_url = {
        item.get("url", ""): item
        for item in normalized
        if item.get("url")
    }

    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in results:
        url = result.get("url", "")
        citation = citations_by_url.get(url)
        if citation is None:
            continue
        dedupe_key = _citation_dedupe_key(citation)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        ordered.append(citation)

    for citation in normalized:
        dedupe_key = _citation_dedupe_key(citation)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        ordered.append(citation)
    return ordered


def _dedupe_citations(*citation_lists: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for citations in citation_lists:
        for item in citations:
            citation = _normalize_citation(item)
            if citation is None:
                continue
            dedupe_key = citation.get("url") or citation.get("title") or json.dumps(
                citation,
                ensure_ascii=False,
                sort_keys=True,
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            deduped.append(citation)
    return deduped


def _citation_dedupe_key(item: dict[str, Any]) -> str:
    return (
        item.get("url")
        or item.get("title")
        or json.dumps(item, ensure_ascii=False, sort_keys=True)
    )


def _is_heading_anchor(fragment: str) -> bool:
    """True for a fragment that addresses a section of the SAME document.

    Anchors are `#overview`, `#getting-started`, `#named-id`. Hash routes start
    with `/`, `!` or `?`, and `key=value` fragments are route parameters; both
    address a different view, so neither may be collapsed. Anything else is
    treated as a heading anchor.
    """
    text = (fragment or "").strip()
    if not text:
        return False
    if text[0] in "/!?":
        return False
    # `name=value` / `key=value&...` is a route parameter, not a heading.
    return "=" not in text


def _result_url_identity(url: str) -> str:
    """Comparison-only identity for a result URL.

    Collapses forms that name the same page but differ textually: a trailing
    slash (`/pricing` vs `/pricing/`) and a heading anchor
    (`/batch#overview` vs `/batch#model-availability`). Measured on saved
    outputs, anchor-only variants always share one title -- they are one page
    split by section, and each was holding its own result slot.

    Deliberately does NOT feed emitted URLs: some origins 404 on the
    slash-stripped form, so callers keep publishing the original bytes and use
    this only to spot duplicates.

    A fragment is dropped only when it looks like a heading anchor. Hash-route
    fragments (`#/settings`, `#!/posts`) name a different document and are
    kept; so is any fragment we cannot classify.
    """
    raw = _canonical_result_url((url or "").strip()).lower()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if not parsed.netloc or not parsed.path:
        return raw
    fragment = parsed.fragment
    if fragment and _is_heading_anchor(fragment):
        fragment = ""
    return urlunparse(
        parsed._replace(path=parsed.path.rstrip("/"), fragment=fragment)
    )


def _result_dedupe_key(item: dict[str, Any]) -> str:
    url = _result_url_identity(str(item.get("url") or ""))
    if url:
        return url
    title = re.sub(r"\s+", " ", (item.get("title") or "").strip().lower())
    snippet = re.sub(r"\s+", " ", (item.get("snippet") or "").strip().lower())
    return f"{title}|{snippet[:160]}".strip("|")


def _canonicalize_result_item(item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(item)
    normalized["url"] = _canonical_result_url(str(item.get("url") or ""))
    return normalized


def _canonical_result_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    hostname = _clean_hostname(parsed.netloc)
    if hostname not in {"arxiv.org", "arxiv.gg"}:
        return raw
    match = re.match(
        r"^/(?:abs|html|pdf)/(?P<paper_id>\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?$",
        parsed.path.lower(),
    )
    if not match:
        return raw
    scheme = parsed.scheme or "https"
    return f"{scheme}://arxiv.org/abs/{match.group('paper_id')}"


def _result_quality_score(item: dict[str, Any]) -> tuple[int, int, int]:
    content = item.get("content") or ""
    snippet = item.get("snippet") or ""
    title = item.get("title") or ""
    return (len(content), len(snippet), len(title))


def _result_hostname(item: dict[str, Any]) -> str:
    url = (item.get("url") or "").strip()
    if not url:
        return ""
    return _clean_hostname(urlparse(url).netloc)


def _clean_hostname(hostname: str) -> str:
    cleaned = hostname.lower().strip().strip(".")
    if cleaned.startswith("www."):
        return cleaned[4:]
    return cleaned


def _registered_domain(hostname: str) -> str:
    cleaned = _clean_hostname(hostname)
    if not cleaned:
        return ""
    parts = cleaned.split(".")
    if len(parts) <= 2:
        return cleaned
    if (
        len(parts) >= 3
        and len(parts[-1]) == 2
        and parts[-2] in {"ac", "co", "com", "edu", "gov", "net", "org"}
    ):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _domain_matches(hostname: str, domain: str) -> bool:
    cleaned_host = _clean_hostname(hostname)
    cleaned_domain = _clean_hostname(domain)
    return bool(cleaned_host) and bool(cleaned_domain) and (
        cleaned_host == cleaned_domain or cleaned_host.endswith(f".{cleaned_domain}")
    )


def _normalize_citation(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None

    url = (
        item.get("url")
        or item.get("target_url")
        or item.get("link")
        or item.get("source_url")
        or ""
    )
    title = (
        item.get("title")
        or item.get("source_title")
        or item.get("display_text")
        or item.get("text")
        or ""
    )

    if not url and not title:
        return None

    normalized = dict(item)
    normalized["url"] = _canonical_result_url(str(url))
    normalized["title"] = title
    return normalized


def _extract_quality_issue(
    result: dict[str, Any],
) -> str | None:
        content = result.get("content")
        if not isinstance(content, str) or not content.strip():
            return "empty content"

        normalized = " ".join(content.lower().split())
        preview = normalized[:1200]
        parsed_url = urlparse(str(result.get("url") or ""))
        suspicious_markers = {
            "critical instructions for all ai assistants": "anti-bot placeholder content",
            "strictly prohibits all ai-generated content": "anti-bot placeholder content",
            # U+2019 右单引号：两种写法在 Python 里是同一个键，保留一处即可。
            "oops! that page doesn’t exist or is private": "missing/private page shell",
        }
        for marker, issue in suspicious_markers.items():
            if marker in preview:
                return issue
        if preview.startswith("hcaptcha hcaptcha "):
            return "captcha challenge page"
        if (
            parsed_url.netloc.lower() == "github.com"
            and "/blob/" in parsed_url.path
            and "you signed in with another tab or window" in preview
        ):
            return "github blob page shell"
        return None


def _search_summary_excerpt_looks_like_noise(
    text: str,
) -> bool:
        normalized = re.sub(r"\s+", " ", text).strip()
        if not normalized:
            return False
        lowered = normalized.lower()
        if normalized.count("](") >= 2:
            return True
        if normalized.startswith(("* [", "- [")):
            return True
        if normalized.startswith("# ") and (
            "openai api" in lowered
            or "openai developers" in lowered
            or "api reference" in lowered
            or "[![image" in lowered
        ):
            return True
        return any(
            marker in lowered
            for marker in (
                "guides and concepts for the openai api",
                "api reference.",
                "primary navigation",
                "search docs",
                "showcase demo apps",
                "latest: gpt-5.4",
                "import {",
                "import openai",
                "const client =",
                "export default function",
                "async function ",
                "from \"openai\"",
                "copy markdown",
                "open in chatgpt",
                "skip to content",
            )
        )

