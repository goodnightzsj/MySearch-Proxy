"""Social 搜索结果的规范化与展示层 —— 两侧共用的单一权威实现。

`mysearch/social_gateway.py`（独立部署的 social gateway）与
`proxy/server.py`（proxy 内置的 social 通道）此前各存一份逐字相同的拷贝。
实测该重复**没有买到独立性**：proxy 早已 `from mysearch.config import ...`，
CI 只构建 stack 镜像，两者还同容器同 entrypoint 启动；而拷贝之间**已经真的
分叉了** —— `build_social_route_metadata` 在 gateway 侧多写
`failure_kind` / `retry_after_seconds`，server 侧少一个 `_persist_...`，
且两侧的 `_available_social_upstream_keys` 各自修了不同的缺陷。

因此把**纯函数**收敛到本模块。这里只放无状态、无上下游协调的规范化逻辑：
不碰键调度、不碰网络、不碰 DB。有状态的调度与尝试执行仍各自留在原模块
（那部分双方本就有意不同）。

**边界**：本模块不得 import `mysearch` 包内其他模块 —— 它必须能被
proxy 与 gateway 双方廉价导入，且不触发 `mysearch.config` 的
`_bootstrap_runtime_env` 副作用。
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException

#: 权威清单。两侧通过 `from .social_normalization import *` 取用——
#: `import *` 默认跳过下划线名，所以这里必须显式列出。
__all__ = [
    "SOCIAL_HOST_ALIASES",
    "_parse_retry_after_header",
    "_safe_get",
    "_social_key_fingerprint",
    "build_empty_social_stats",
    "build_social_result",
    "choose_preferred_social_attempt",
    "count_social_citations",
    "count_social_results",
    "effective_social_fallback_threshold",
    "extract_social_upstream_error",
    "has_social_fallback",
    "is_supported_social_result_url",
    "looks_synthetic_social_status_id",
    "mask_secret",
    "normalize_citation",
    "normalize_result_item",
    "normalize_social_match_url",
    "redact_secret_text",
    "should_retry_social_with_fallback",
    "social_attempt_http_exception",
    "unwrap_social_tokens_payload",
]

SOCIAL_HOST_ALIASES = {
    "x.com",
    "www.x.com",
    "twitter.com",
    "www.twitter.com",
    "mobile.x.com",
    "mobile.twitter.com",
}


def build_empty_social_stats() -> dict[str, Any]:
    return {
        "token_total": 0,
        "token_normal": 0,
        "token_limited": 0,
        "token_invalid": 0,
        "chat_remaining": 0,
        "image_remaining": 0,
        "video_remaining": None,
        "total_calls": 0,
        "nsfw_enabled": 0,
        "nsfw_disabled": 0,
        "pool_count": 0,
        "pools": [],
    }


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    if len(value) <= 8:
        return f"{value[:2]}***{value[-2:]}"
    if len(value) <= 12:
        return f"{value[:3]}***{value[-3:]}"
    return f"{value[:6]}***{value[-4:]}"


def unwrap_social_tokens_payload(tokens_payload: Any) -> Any:
    if isinstance(tokens_payload, dict):
        for key_name in ("tokens", "data", "items", "result", "pools"):
            candidate = tokens_payload.get(key_name)
            if isinstance(candidate, dict):
                return candidate
            if isinstance(candidate, list):
                return {"default": candidate}
        return tokens_payload
    if isinstance(tokens_payload, list):
        return {"default": tokens_payload}
    return {}


def _safe_get(d: Any, key: str) -> Any:
    if isinstance(d, dict):
        return d.get(key)
    return None


def normalize_citation(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    url = item.get("url") or item.get("target_url") or item.get("link") or item.get("source_url") or ""
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
    normalized["url"] = url
    normalized["title"] = title
    return normalized


def normalize_result_item(item: Any) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None
    url = (item.get("url") or item.get("link") or "").strip()
    title = (item.get("title") or item.get("author") or item.get("handle") or url).strip()
    text = (
        item.get("text")
        or item.get("content")
        or item.get("body")
        or item.get("snippet")
        or item.get("summary")
        or ""
    ).strip()
    result = {
        "title": title,
        "url": url,
        "text": text,
        "content": (item.get("content") or text).strip(),
        "snippet": (item.get("snippet") or item.get("summary") or text).strip(),
        "author": (item.get("author") or item.get("username") or item.get("handle") or "").strip(),
        "handle": (item.get("handle") or item.get("username") or "").strip().lstrip("@"),
        "created_at": (item.get("created_at") or item.get("published_at") or "").strip(),
        "why_relevant": (item.get("why_relevant") or item.get("reason") or "").strip(),
    }
    if not result["url"] and not result["title"] and not result["text"]:
        return None
    return result


def looks_synthetic_social_status_id(status_id: str) -> bool:
    digits = (status_id or "").strip()
    if len(digits) < 12 or not digits.isdigit():
        return False

    repeated_sequences = [
        "0123456789" * 4,
        "1234567890" * 4,
        "9876543210" * 4,
        "0987654321" * 4,
        "".join(f"{i}{i}" for i in range(10)) * 3,
        "".join(f"{i}{i}" for i in range(9, -1, -1)) * 3,
    ]
    for sequence in repeated_sequences:
        if digits in sequence or digits[:-1] in sequence:
            return True

    for size in range(1, 5):
        pattern = digits[:size]
        if pattern and (pattern * ((len(digits) // size) + 1))[: len(digits)] == digits:
            return True
    return False


def normalize_social_match_url(url: str) -> str:
    """把 X/Twitter 的 status URL 归一成**按 status ID** 的 join key。

    这个 key 只用于在 `trusted_citations`（上游 annotations）与模型正文的
    `results[]` 之间做匹配，**从不进入响应输出**（输出用 `citation["url"]` 原值）。
    因此它可以自由选择最稳定的形态。

    历史实现按 `handle` 归一（`x.com/{handle}/status/{id}`）。那在真实上游返回
    下**永远匹配不上**：annotations 给的 handle 是匿名的 `i`
    （`https://x.com/i/status/<id>`），而模型正文写的是真实 handle
    （`https://x.com/QCodecc/status/<id>`）。结果是 `matched_results` 恒为空，
    `build_social_result` 拿不到 `matched`，模型已给出的 title/author/text
    全部丢失，下游只看到一串裸 URL。

    status ID 本身就是 post 的唯一标识，handle 只是路径上的装饰——同一 post
    经不同 handle 写法（含 `i`）指向的都是同一个对象，所以 key 只保留 ID。
    `looks_synthetic_social_status_id` 的过滤保留在 key 生成处，防编造的语义不变：
    ID 仍必须出现在 `trusted_citations` 里才算匹配成功。

    与 `proxy/server.py` 的同名函数保持逐字一致（两份是刻意的独立实现）。
    """
    raw_url = (url or "").strip()
    if not raw_url:
        return ""
    try:
        parsed = urlparse(raw_url)
    except Exception:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""

    host = parsed.netloc.lower()
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/")
    if not path:
        path = "/"

    if host not in SOCIAL_HOST_ALIASES:
        return ""

    parts = [part for part in path.split("/") if part]
    if len(parts) < 3 or parts[1].lower() != "status" or not parts[2].isdigit():
        return ""
    if looks_synthetic_social_status_id(parts[2]):
        return ""
    return f"https://x.com/i/status/{parts[2]}"


def is_supported_social_result_url(url: str) -> bool:
    return bool(normalize_social_match_url(url))


def build_social_result(
    citation: dict[str, str] | None = None,
    matched: dict[str, str] | None = None,
) -> dict[str, str]:
    citation = citation or {}
    matched = matched or {}
    url = (citation.get("url") or matched.get("url") or "").strip()
    title = (
        citation.get("title")
        or matched.get("title")
        or matched.get("author")
        or matched.get("handle")
        or url
    ).strip()
    text = (matched.get("text") or "").strip()
    content = (matched.get("content") or text).strip()
    snippet = (matched.get("snippet") or text).strip()
    author = (matched.get("author") or "").strip()
    handle = (matched.get("handle") or "").strip().lstrip("@")
    created_at = (matched.get("created_at") or "").strip()
    why_relevant = (matched.get("why_relevant") or "").strip()
    return {
        "title": title,
        "url": url,
        "text": text,
        "content": content,
        "snippet": snippet,
        "author": author,
        "handle": handle,
        "created_at": created_at,
        "why_relevant": why_relevant,
    }


def count_social_results(payload: dict[str, Any] | None) -> int:
    return len((payload or {}).get("results") or [])


def count_social_citations(payload: dict[str, Any] | None) -> int:
    return len((payload or {}).get("citations") or [])


def redact_secret_text(value: Any, *secrets: Any) -> str:
    text = str(value or "")
    for secret in secrets:
        normalized = str(secret or "")
        if normalized:
            text = text.replace(normalized, "<redacted>")
    return text


def _social_key_fingerprint(key: str) -> str:
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()


def _parse_retry_after_header(headers: Any) -> int | None:
    if headers is None or not hasattr(headers, "get"):
        return None
    raw_retry_after = str(headers.get("retry-after") or "").strip()
    if not raw_retry_after:
        return None
    try:
        return max(1, min(86400, math.ceil(float(raw_retry_after))))
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        retry_at = parsedate_to_datetime(raw_retry_after)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(
            1,
            min(86400, math.ceil((retry_at - datetime.now(timezone.utc)).total_seconds())),
        )
    except (TypeError, ValueError, OverflowError):
        return None


def has_social_fallback(primary_model: str, fallback_model: str) -> bool:
    primary = (primary_model or "").strip()
    fallback = (fallback_model or "").strip()
    return bool(primary and fallback and fallback != primary)


def effective_social_fallback_threshold(min_results: int, max_results: int) -> int:
    try:
        configured = max(1, int(min_results or 1))
    except (TypeError, ValueError):
        configured = 1
    try:
        requested = max(1, int(max_results or 1))
    except (TypeError, ValueError):
        requested = 1
    return min(configured, requested)


def should_retry_social_with_fallback(
    primary_model: str,
    fallback_model: str,
    response: dict[str, Any] | None,
    min_results: int,
    max_results: int,
) -> tuple[bool, str]:
    if not has_social_fallback(primary_model, fallback_model):
        return False, ""
    threshold = effective_social_fallback_threshold(min_results, max_results)
    if count_social_results(response) >= threshold:
        return False, ""
    return True, "result_count_below_threshold"


def choose_preferred_social_attempt(
    primary_attempt: dict[str, Any] | None,
    fallback_attempt: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not fallback_attempt or not fallback_attempt.get("ok"):
        return primary_attempt
    if not primary_attempt or not primary_attempt.get("ok"):
        return fallback_attempt

    primary_count = int(primary_attempt.get("result_count") or 0)
    fallback_count = int(fallback_attempt.get("result_count") or 0)
    if fallback_count > primary_count:
        return fallback_attempt

    primary_citations = int(primary_attempt.get("citation_count") or 0)
    fallback_citations = int(fallback_attempt.get("citation_count") or 0)
    if fallback_count == primary_count and fallback_citations > primary_citations:
        return fallback_attempt

    return primary_attempt


def extract_social_upstream_error(
    upstream_body: dict[str, Any] | Any,
    fallback_detail: str = "Social search failed",
    *secrets: Any,
) -> str:
    detail = ""
    if isinstance(upstream_body, dict):
        error = upstream_body.get("error") or {}
        if isinstance(error, dict):
            detail = error.get("message") or ""
        if not detail:
            detail = upstream_body.get("detail") or ""
    if not detail:
        detail = fallback_detail
    return redact_secret_text(detail, *secrets)[:300]


def social_attempt_http_exception(attempt: dict[str, Any]) -> HTTPException:
    status_code = max(400, int(attempt.get("status_code") or 502))
    headers = None
    retry_after_seconds = attempt.get("retry_after_seconds")
    if status_code == 429 and retry_after_seconds is not None:
        headers = {"Retry-After": str(max(1, int(retry_after_seconds)))}
    return HTTPException(
        status_code=status_code,
        detail=attempt.get("error") or "Social search failed",
        headers=headers,
    )
