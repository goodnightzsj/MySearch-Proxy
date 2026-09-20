"""TTL 缓存：命名空间隔离、过期裁剪、容量淘汰与命中统计。

从 `mysearch/clients.py` 抽出的**缓存所有者**。

命名空间（`search` / `extract` / `social` / `social_gateway` /
`social_unavailable`）的 TTL 由 `MySearchConfig` 决定，容量上限 256，
超出时先裁剪过期项、再淘汰最旧项。

内部自己读时钟（`time.monotonic`）而不是由调用方传入 `now`：有 30 处调用
点，逐一传参会让每个调用点都要先取一次时钟。需要控制时钟的测试应 patch
`mysearch.cache.time.monotonic`。

`MySearchClient` 保留 `_cache_*` / `_annotate_cache` 同名方法作为一行委托，
并转发 `_cache_stats` / `_cache_max_entries` 两个属性，调用面不变。
"""

from __future__ import annotations

import copy
import threading
import time
from typing import Any

#: 单个命名空间的条目上限，超出后按 `inserted_at` 淘汰最旧项。
DEFAULT_MAX_ENTRIES = 256


class CacheStore:
    """进程内 TTL 缓存，线程安全。

    `ttls` 的键集同时也是命名空间集合：只认识构造时声明的命名空间，
    TTL <= 0 的命名空间既不存也不读（用于关闭某一类缓存）。
    """

    def __init__(self, *, ttls: dict[str, int], max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self._lock = threading.Lock()
        self._ttls = dict(ttls)
        self._store: dict[str, dict[str, dict[str, Any]]] = {
            namespace: {} for namespace in self._ttls
        }
        self._stats: dict[str, dict[str, int]] = {
            namespace: {"hits": 0, "misses": 0} for namespace in self._ttls
        }
        self.max_entries = max_entries

    @property
    def stats(self) -> dict[str, dict[str, int]]:
        """命中/未命中计数，按命名空间分组。仅供健康检查读取。"""
        return self._stats

    def ttl_seconds(self, namespace: str) -> int:
        return self._ttls.get(namespace, 0)

    def health(self) -> dict[str, dict[str, int]]:
        snapshot: dict[str, dict[str, int]] = {}
        with self._lock:
            now = time.monotonic()
            for namespace in self._store:
                self._prune_expired_locked(namespace, now)
                stats = self._stats[namespace]
                snapshot[namespace] = {
                    "ttl_seconds": self._ttls.get(namespace, 0),
                    "entries": len(self._store[namespace]),
                    "hits": stats["hits"],
                    "misses": stats["misses"],
                }
        return snapshot

    def _prune_expired_locked(self, namespace: str, now: float) -> None:
        expired_keys = [
            key
            for key, payload in self._store[namespace].items()
            if payload.get("expires_at", 0.0) <= now
        ]
        for key in expired_keys:
            self._store[namespace].pop(key, None)

    def get(self, namespace: str, cache_key: str) -> dict[str, Any] | None:
        ttl_seconds = self._ttls.get(namespace, 0)
        if ttl_seconds <= 0:
            return None

        with self._lock:
            now = time.monotonic()
            payload = self._store[namespace].get(cache_key)
            if payload is None:
                self._stats[namespace]["misses"] += 1
                return None
            if payload.get("expires_at", 0.0) <= now:
                self._store[namespace].pop(cache_key, None)
                self._stats[namespace]["misses"] += 1
                return None

            self._stats[namespace]["hits"] += 1
            return copy.deepcopy(payload["value"])

    def set(self, namespace: str, cache_key: str, value: dict[str, Any]) -> None:
        ttl_seconds = self._ttls.get(namespace, 0)
        if ttl_seconds <= 0:
            return

        with self._lock:
            now = time.monotonic()
            store = self._store[namespace]
            if len(store) >= self.max_entries:
                self._prune_expired_locked(namespace, now)
            if len(store) >= self.max_entries:
                oldest_key = min(store, key=lambda k: store[k].get("inserted_at", 0.0))
                store.pop(oldest_key, None)
            store[cache_key] = {
                "expires_at": now + ttl_seconds,
                "inserted_at": now,
                "value": copy.deepcopy(value),
            }

    def delete(self, namespace: str, cache_key: str) -> None:
        if namespace not in self._store:
            return
        with self._lock:
            self._store[namespace].pop(cache_key, None)

    def annotate(
        self,
        result: dict[str, Any],
        *,
        namespace: str,
        hit: bool,
    ) -> dict[str, Any]:
        cache_meta = dict(result.get("cache") or {})
        cache_meta[namespace] = {
            "hit": hit,
            "ttl_seconds": self._ttls.get(namespace, 0),
        }
        result["cache"] = cache_meta
        return result
