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
        return (
            _classify_key_failure(self.status_code, self.classification_detail)
            == "auth_rejected"
        )

    @property
    def is_plan_limit_error(self) -> bool:
        return self.status_code in {402, 432}

    @property
    def key_failure_kind(self) -> str:
        return _classify_key_failure(self.status_code, self.classification_detail)

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


def _classify_key_failure(status_code: int, detail: Any) -> str:
    normalized = " ".join(_stringify_error_detail(detail).lower().split())
    has_quota_marker = any(
        marker in normalized for marker in _QUOTA_FAILURE_MARKERS
    )
    if status_code in {402, 432} or (
        status_code in {403, 429} and has_quota_marker
    ):
        return "quota_exhausted"
    if status_code == 429:
        return "rate_limited"
    if status_code == 401 or (
        status_code == 403
        and any(marker in normalized for marker in _AUTH_FAILURE_MARKERS)
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
