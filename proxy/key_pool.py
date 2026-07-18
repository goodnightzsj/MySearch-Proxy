"""
按服务维度管理 API Key 轮询池
"""
import math
import threading
import time

from database import (
    SUPPORTED_SERVICES,
    get_active_keys,
    get_next_key_schedule_delay,
    normalize_retry_after_seconds,
    normalize_service,
    update_key_usage,
)

_QUOTA_MARKERS = (
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
_AUTH_MARKERS = (
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


def classify_upstream_key_failure(status_code, detail=""):
    """Classify failures that make one credential unschedulable."""
    normalized = " ".join(str(detail or "").lower().split())
    has_quota_marker = any(marker in normalized for marker in _QUOTA_MARKERS)
    if status_code in {402, 432} or (
        status_code in {403, 429} and has_quota_marker
    ):
        return "quota_exhausted"
    if status_code == 429:
        return "rate_limited"
    if status_code == 401 or (
        status_code == 403 and any(marker in normalized for marker in _AUTH_MARKERS)
    ):
        return "auth_rejected"
    return ""


class ServiceKeyPool:
    def __init__(self):
        self._lock = threading.Lock()
        self._keys = {service: [] for service in SUPPORTED_SERVICES}
        self._indexes = {service: 0 for service in SUPPORTED_SERVICES}
        self._reload_after = {service: None for service in SUPPORTED_SERVICES}
        self._generations = {service: 0 for service in SUPPORTED_SERVICES}
        self._initialized = set()

    def reload(self, service=None):
        services = [normalize_service(service)] if service else list(SUPPORTED_SERVICES)
        with self._lock:
            for item in services:
                self._reload_service_locked(item)

    def get_next_key(self, service="tavily", *, exclude_ids=None):
        """Round-robin 返回某个服务下一个可用 key。"""
        service = normalize_service(service)
        excluded = set(exclude_ids or ())
        with self._lock:
            reload_after = self._reload_after[service]
            cooldown_expired = reload_after is not None and time.monotonic() >= reload_after
            if service not in self._initialized or cooldown_expired:
                self._reload_service_locked(service)
            eligible_keys = [
                key for key in self._keys[service] if key.get("id") not in excluded
            ]
            if not eligible_keys and not self._keys[service]:
                self._reload_service_locked(service)
                eligible_keys = [
                    key for key in self._keys[service] if key.get("id") not in excluded
                ]
            if not eligible_keys:
                return None
            index = self._indexes.get(service, 0) % len(eligible_keys)
            key = eligible_keys[index]
            self._indexes[service] = (index + 1) % len(eligible_keys)
            selected = dict(key)
            selected["_pool_generation"] = self._generations[service]
            return selected

    def invalidate(self, service):
        service = normalize_service(service)
        with self._lock:
            self._generations[service] += 1

    def get_retry_after(self, service):
        service = normalize_service(service)
        with self._lock:
            reload_after = self._reload_after[service]
            if reload_after is not None:
                remaining = reload_after - time.monotonic()
                if remaining > 0:
                    return max(1, min(86400, math.ceil(remaining)))
            delay = get_next_key_schedule_delay(service)
            if delay is None or delay <= 0:
                return None
            self._reload_after[service] = time.monotonic() + delay
            return max(1, min(86400, math.ceil(delay)))

    def _reload_service_locked(self, service):
        self._generations[service] += 1
        self._keys[service] = [dict(row) for row in get_active_keys(service)]
        if self._indexes[service] >= len(self._keys[service]):
            self._indexes[service] = 0
        next_delay = get_next_key_schedule_delay(service)
        self._reload_after[service] = (
            time.monotonic() + max(0.001, next_delay)
            if next_delay is not None
            else None
        )
        self._initialized.add(service)

    def report_result(
        self,
        service,
        key_id,
        success,
        *,
        failure_kind="",
        failure_detail="",
        retry_after_seconds=None,
        generation=None,
    ):
        """Record usage and remove terminal credential failures immediately."""
        service = normalize_service(service)
        with self._lock:
            if generation is not None and generation != self._generations[service]:
                return False
            update_key_usage(
                key_id,
                success,
                failure_kind=failure_kind,
                failure_detail=failure_detail,
                retry_after_seconds=retry_after_seconds,
            )
            if failure_kind:
                self._keys[service] = [
                    key for key in self._keys[service] if key.get("id") != key_id
                ]
                if failure_kind == "rate_limited":
                    reload_after = time.monotonic() + normalize_retry_after_seconds(
                        retry_after_seconds
                    )
                    current_reload_after = self._reload_after[service]
                    if current_reload_after is None or reload_after < current_reload_after:
                        self._reload_after[service] = reload_after
                if self._keys[service]:
                    self._indexes[service] %= len(self._keys[service])
                else:
                    self._indexes[service] = 0
                return True
        return True


pool = ServiceKeyPool()
