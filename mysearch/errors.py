"""MySearch 的错误类型与 provider 失败分类。

从 `mysearch/clients.py` 抽出，使传输层（`mysearch/providers/base.py`）能
在不与 `clients` 形成循环导入的前提下复用错误类型。`clients` 继续 re-export
这些名字，对外导入路径不变。
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

class MySearchError(RuntimeError):
    """MySearch 调用失败。"""


class MySearchHTTPError(MySearchError):
    """携带 provider 与状态码的 HTTP 错误。"""

    def __init__(
        self,
        *,
        provider: str,
        status_code: int,
        detail: Any,
        url: str,
        retry_after_seconds: int | None = None,
        classification_detail: Any | None = None,
    ) -> None:
        self.provider = provider
        self.status_code = status_code
        self.detail = detail
        self.url = url
        self.retry_after_seconds = retry_after_seconds
        self.classification_detail = (
            detail if classification_detail is None else classification_detail
        )
        super().__init__(self._build_message())

    @property
    def is_auth_error(self) -> bool:
        return self.key_failure_kind == "auth_rejected"

    @property
    def is_plan_limit_error(self) -> bool:
        return self.status_code in {402, 432} or (
            self.provider == "tavily" and self.status_code == 433
        )

    @property
    def key_failure_kind(self) -> str:
        return classify_upstream_key_failure(
            self.status_code, self.classification_detail, service=self.provider,
        )

    def _build_message(self) -> str:
        detail_text = _stringify_error_detail(self.detail)
        if self.is_auth_error:
            return (
                f"{self.provider} is configured but the API key was rejected "
                f"(HTTP {self.status_code}): {detail_text or 'authentication failed'}"
            )
        return (
            f"{self.provider} request failed "
            f"(HTTP {self.status_code}): {detail_text or 'unknown error'}"
        )


def _stringify_error_detail(detail: Any) -> str:
    if isinstance(detail, str):
        return detail.strip()
    if detail is None:
        return ""
    if isinstance(detail, (dict, list)):
        return json.dumps(detail, ensure_ascii=False)
    return str(detail).strip()


def _redact_provider_secret(detail: Any, secret: str) -> str:
    text = _stringify_error_detail(detail)
    return text.replace(secret, "<redacted>") if secret else text


_QUOTA_FAILURE_MARKERS = (
    "quota_exhausted",
    "quota exhausted",
    "insufficient_quota",
    "insufficient quota",
    "credits exhausted",
    "credit exhausted",
    "credits limit",
    "credit limit",
    "exceeded your credits",
    "no credits remaining",
    "billing limit",
    "usage limit",
    "plan limit",
    "resource_exhausted",
)
_AUTH_FAILURE_MARKERS = (
    "invalid api key",
    "invalid_api_key",
    "api key is invalid",
    "api key has expired",
    "expired api key",
    "revoked api key",
    "invalid token",
    "token is invalid",
    "token has expired",
    "expired token",
    "revoked token",
    "bad credentials",
    "authentication failed",
)


# Firecrawl 9ae2451: routes/shared.ts + controllers/auth.ts. These are
# account-wide failures, unlike endpoint/format/IP and target-site restrictions.
_FIRECRAWL_ACCOUNT_FAILURES = (
    (403, "sponsor_verification_expired", ("sponsor_verification_expired", "sponsor verification has expired")),
    (402, "unverified_credit_limit_reached", ("unverified_credit_limit_reached", "this agent key has used its 50 unverified credits")),
    (403, "account_holder_blocked", ("this api key has been blocked by the account holder",)),
    (403, "account_banned", ("this account has been banned",)),
)
# Public request/job ErrorCodes in apps/api/src/lib/error.ts are not
# credential failures, even when a target site's message mentions auth/quota.
_FIRECRAWL_REQUEST_CODES = frozenset("""
    THIRD_PARTY_DATA_TERMS_REQUIRED THIRD_PARTY_DATA_NOT_FOUND
    THIRD_PARTY_DATA_NOT_ENABLED THIRD_PARTY_DATA_ENRICHMENT_NOT_ENABLED
    SCRAPE_TIMEOUT MAP_TIMEOUT UNKNOWN_ERROR SCRAPE_ALL_ENGINES_FAILED
    SCRAPE_SSL_ERROR SCRAPE_SITE_ERROR SCRAPE_PROXY_SELECTION_ERROR
    SCRAPE_PDF_PREFETCH_FAILED SCRAPE_DOCUMENT_PREFETCH_FAILED
    SCRAPE_JOB_CANCELLED SCRAPE_RETRY_LIMIT SCRAPE_ZDR_VIOLATION_ERROR
    SCRAPE_DNS_RESOLUTION_ERROR SCRAPE_PDF_INSUFFICIENT_TIME_ERROR
    SCRAPE_PDF_ANTIBOT_ERROR SCRAPE_PDF_FETCH_PROXY_ERROR SCRAPE_PDF_OCR_REQUIRED
    SCRAPE_DOCUMENT_ANTIBOT_ERROR SCRAPE_DOCUMENT_FETCH_PROXY_ERROR
    SCRAPE_UNSUPPORTED_FILE_ERROR SCRAPE_ACTION_ERROR SCRAPE_RACED_REDIRECT_ERROR
    SCRAPE_NO_CACHED_DATA SCRAPE_LOCKDOWN_CACHE_MISS SCRAPE_SITEMAP_ERROR
    SCRAPE_ACTIONS_NOT_SUPPORTED SCRAPE_BRANDING_NOT_SUPPORTED AGENT_INDEX_ONLY
    SCRAPE_AUDIO_UNSUPPORTED_URL SCRAPE_VIDEO_UNSUPPORTED_URL SCRAPE_MEDIA_ACCESS_DENIED
    SCRAPE_PROMPT_INJECTION_DETECTED SCRAPE_JSON_CONTENT_TOO_LARGE
    SCRAPE_X_TWITTER_CONFIGURATION_ERROR PARSE_UNSUPPORTED_OPTIONS CRAWL_DENIAL
    UNSUPPORTED_SITE MAP_FAILED BAD_REQUEST_INVALID_JSON BAD_REQUEST
    CONCURRENCY_QUEUE_TIMEOUT SAFE_MODE_BLOCKED SCRAPE_SITE_RESTRICTION_BLOCKED
    unsafe_domain_blocked thread_not_found thread_busy thread_expired
    threads_disabled exchange_not_enabled
""".lower().split())


def _provider_error_fields(detail):
    if not isinstance(detail, dict):
        return [str(detail or "")]
    fields = []
    for name in ("code", "tag", "type", "error", "message", "detail"):
        value = detail.get(name)
        if isinstance(value, str):
            fields.append(value)
        elif isinstance(value, dict):
            fields.extend(
                item for field in ("code", "tag", "type", "error", "message", "detail")
                if isinstance(item := value.get(field), str)
            )
    return fields


# https://exa.ai/docs/admin/error-codes (checked 2026-10-07).
# Match HTTP status AND tag; payment/feature errors do not invalidate API keys.
_EXA_TAG_FAILURES = {
    (401, "invalid_api_key"): "auth_rejected",
    (402, "no_more_credits"): "quota_exhausted",
    (402, "api_key_budget_exceeded"): "api_key_budget_exceeded",
    (402, "team_budget_exceeded"): "team_budget_exceeded",
    (429, "rate_limit_exceeded"): "rate_limited",
    (403, "feature_disabled"): "",
    (403, "prohibited_content"): "",
    (403, "content_filter_error"): "",
    (400, "invalid_request_body"): "",
    (400, "invalid_request"): "",
    (400, "invalid_num_results"): "",
    (400, "num_results_exceeded"): "",
    (400, "invalid_json_schema"): "",
    (400, "subpages_limit_exceeded"): "",
    (402, "x402_payment_required"): "",
    (400, "x402_invalid_signature"): "",
    (402, "x402_verification_failed"): "",
    (402, "mpp_verification_failed"): "",
    (429, "x402_too_many_unpaid"): "",
    (429, "x402_wallet_rate_limited"): "",
    (500, "x402_internal_error"): "",
    (503, "service_overloaded"): "",
}


def classify_upstream_key_failure(status_code: int, detail: Any = "", *, service: str = "") -> str:
    """Classify failures that make one credential unschedulable."""
    if status_code < 400:
        return ""
    fields = [" ".join(field.lower().split()) for field in _provider_error_fields(detail)]
    normalized = " ".join(fields)
    if service == "firecrawl":
        if _FIRECRAWL_REQUEST_CODES.intersection(fields):
            return ""
        for status, reason, markers in _FIRECRAWL_ACCOUNT_FAILURES:
            if status_code == status and any(marker in normalized for marker in markers):
                return reason
    elif service == "tavily" and status_code == 433:
        # Tavily's documented pay-as-you-go cap, not a transient rate limit.
        return "pay_as_you_go_limit"
    elif service == "exa":
        for field in fields:
            if (status_code, field) in _EXA_TAG_FAILURES:
                return _EXA_TAG_FAILURES[(status_code, field)]
    if service not in {"tavily", "firecrawl", "exa"}:
        normalized = " ".join(_stringify_error_detail(detail).lower().split())
    has_quota_marker = any(marker in normalized for marker in _QUOTA_FAILURE_MARKERS)
    if status_code in {402, 432} or (
        status_code in {403, 429} and has_quota_marker
    ):
        return "quota_exhausted"
    if status_code == 429:
        return "rate_limited"
    if status_code == 401 or (
        status_code == 403 and any(marker in normalized for marker in _AUTH_FAILURE_MARKERS)
    ):
        return "auth_rejected"
    return ""


def _parse_retry_after_seconds(headers: Any) -> int | None:
    if headers is None or not hasattr(headers, "get"):
        return None
    raw_value = str(headers.get("retry-after") or headers.get("Retry-After") or "").strip()
    if not raw_value:
        return None
    try:
        return max(1, min(86400, math.ceil(float(raw_value))))
    except (ValueError, OverflowError):
        pass
    try:
        retry_at = parsedate_to_datetime(raw_value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = math.ceil((retry_at - datetime.now(timezone.utc)).total_seconds())
        return max(1, min(86400, seconds))
    except (TypeError, ValueError, OverflowError):
        return None
