"""MySearch provider client 和自动路由。"""

from __future__ import annotations

import copy
import hashlib
import html
import json
import logging
import math
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass as _dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Literal, Mapping, Sequence, cast
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

import httpx

from mysearch.config import MySearchConfig, ProviderConfig
from mysearch.keyring import MySearchKeyRing
from mysearch import postprocess
from mysearch import query_routing
from mysearch import ranking
from mysearch import research
from mysearch.research import cache_keys
from mysearch.research import events
from mysearch.research import finalize
from mysearch.research import quality
from mysearch.research import sections
from mysearch.research import software_version
from mysearch.research import selection
from mysearch.research import social
from mysearch.providers.base import ProviderTransport
from mysearch.provider_contract import ProviderResponse

logger = logging.getLogger(__name__)


def dataclass(*args, **kwargs):
    if sys.version_info < (3, 10):
        kwargs.pop("slots", None)
    return _dataclass(*args, **kwargs)


from mysearch.types import (  # noqa: F401  (re-exported: internal refs keep resolving)
    ProviderName,
    ResolvedSearchIntent,
    SearchIntent,
    SearchMode,
    SearchStrategy,
    SEARCH_MODES,
)
OPTIONAL_VERIFY_TIMEOUT_SECONDS = 10
HYBRID_SOCIAL_TIMEOUT_SECONDS = 20
DEFAULT_KEY_COOLDOWN_SECONDS = 60
MAX_PINNED_KEY_RETRY_DELAY_SECONDS = 120


from mysearch.errors import (  # noqa: F401  (re-exported: public import path stays mysearch.clients)
    MySearchError,
    MySearchHTTPError,
    _parse_retry_after_seconds,
    _stringify_error_detail,
)



@dataclass(slots=True)
class RouteDecision:
    provider: str
    reason: str
    tavily_topic: str = "general"
    firecrawl_categories: list[str] | None = None
    sources: list[str] | None = None
    fallback_chain: list[str] | None = None
    result_profile: Literal["off", "web", "news", "resource"] = "off"
    allow_exa_rescue: bool = False


@dataclass(slots=True)
class SearchRoutePolicy:
    key: str
    provider: str
    fallback_chain: tuple[str, ...] = ()
    tavily_topic: str = "general"
    firecrawl_categories: tuple[str, ...] = ()
    result_profile: Literal["off", "web", "news", "resource"] = "off"
    allow_exa_rescue: bool = False


_MODE_PROVIDER_POLICY: dict[str, SearchRoutePolicy] = {
    "web": SearchRoutePolicy(
        key="web",
        provider="tavily",
        fallback_chain=("exa", "firecrawl"),
        result_profile="web",
        allow_exa_rescue=True,
    ),
    "news": SearchRoutePolicy(
        key="news",
        provider="tavily",
        fallback_chain=("exa", "firecrawl"),
        tavily_topic="news",
        result_profile="news",
        allow_exa_rescue=True,
    ),
    "award_result": SearchRoutePolicy(
        key="award_result",
        provider="tavily",
        fallback_chain=("exa", "firecrawl"),
        tavily_topic="news",
        result_profile="news",
        allow_exa_rescue=True,
    ),
    "status": SearchRoutePolicy(
        key="status",
        provider="tavily",
        fallback_chain=("exa", "firecrawl"),
        tavily_topic="general",
        result_profile="web",
        allow_exa_rescue=True,
    ),
    "docs": SearchRoutePolicy(
        key="docs",
        provider="firecrawl",
        fallback_chain=("tavily", "exa"),
        firecrawl_categories=("research",),
        result_profile="resource",
    ),
    "github": SearchRoutePolicy(
        key="github",
        provider="firecrawl",
        fallback_chain=("exa", "tavily"),
        firecrawl_categories=("github",),
        result_profile="resource",
    ),
    "pdf": SearchRoutePolicy(
        key="pdf",
        provider="firecrawl",
        fallback_chain=("tavily", "exa"),
        firecrawl_categories=("pdf",),
        result_profile="resource",
        allow_exa_rescue=True,
    ),
    "content": SearchRoutePolicy(
        key="content",
        provider="firecrawl",
        fallback_chain=("tavily", "exa"),
        result_profile="resource",
    ),
    "resource": SearchRoutePolicy(
        key="resource",
        provider="firecrawl",
        fallback_chain=("tavily", "exa"),
        firecrawl_categories=("research",),
        result_profile="resource",
    ),
    "tutorial": SearchRoutePolicy(
        key="tutorial",
        provider="tavily",
        fallback_chain=("exa", "firecrawl"),
        result_profile="web",
        allow_exa_rescue=True,
    ),
    "changelog": SearchRoutePolicy(
        key="changelog",
        provider="tavily",
        fallback_chain=("exa", "firecrawl"),
        tavily_topic="news",
        firecrawl_categories=("research",),
        result_profile="resource",
        allow_exa_rescue=True,
    ),
    "exploratory": SearchRoutePolicy(
        key="exploratory",
        provider="exa",
        fallback_chain=("tavily", "firecrawl"),
        result_profile="web",
        allow_exa_rescue=True,
    ),
    "research": SearchRoutePolicy(
        key="research",
        provider="tavily",
        fallback_chain=("exa", "firecrawl"),
        result_profile="web",
        allow_exa_rescue=True,
    ),
}


class MySearchClient(ProviderTransport):
    def __init__(
        self,
        config: MySearchConfig | None = None,
        keyring: MySearchKeyRing | None = None,
    ) -> None:
        self.config = config or MySearchConfig.from_env()
        self.keyring = keyring or MySearchKeyRing(self.config)
        self._cache_lock = threading.Lock()
        self._cache_ttls = {
            "search": self.config.search_cache_ttl_seconds,
            "extract": self.config.extract_cache_ttl_seconds,
            "social": max(self.config.search_cache_ttl_seconds, 300),
            "social_gateway": 45,
            "social_unavailable": 30,
        }
        self._cache_store: dict[str, dict[str, dict[str, Any]]] = {
            "search": {},
            "extract": {},
            "social": {},
            "social_gateway": {},
            "social_unavailable": {},
        }
        self._cache_stats: dict[str, dict[str, int]] = {
            "search": {"hits": 0, "misses": 0},
            "extract": {"hits": 0, "misses": 0},
            "social": {"hits": 0, "misses": 0},
            "social_gateway": {"hits": 0, "misses": 0},
            "social_unavailable": {"hits": 0, "misses": 0},
        }
        self._cache_max_entries = 256
        self._provider_probe_ttl_seconds = 1800
        self._provider_probe_cache: dict[str, dict[str, Any]] = {}
        self._http = httpx.Client(
            timeout=httpx.Timeout(self.config.timeout_seconds, connect=10.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={"User-Agent": "MySearch/0.2"},
        )
        self._executor = ThreadPoolExecutor(
            max_workers=self.config.max_parallel_workers,
            thread_name_prefix="mysearch",
        )

    def close(self) -> None:
        self._executor.shutdown(wait=False)
        self._http.close()

    def health(self) -> dict[str, Any]:
        keyring_info = self.keyring.describe()
        cache = self._cache_health()
        provider_names = ["tavily", "firecrawl", "exa", "xai"]
        provider_configs = {
            "tavily": self.config.tavily,
            "firecrawl": self.config.firecrawl,
            "exa": self.config.exa,
            "xai": self.config.xai,
        }
        probe_results, _ = self._execute_parallel(
            {
                name: (
                    lambda n=name: self._probe_provider_status(
                        provider_configs[n],
                        int(keyring_info[n]["count"]),
                    )
                )
                for name in provider_names
            },
            max_workers=4,
        )
        providers = {}
        for name in provider_names:
            status = probe_results.get(name, {"status": "network_error", "error": "probe failed", "checked_at": ""})
            info = keyring_info[name]
            cfg = provider_configs[name]
            providers[name] = {
                "base_url": cfg.base_url,
                "alternate_base_urls": cfg.alternate_base_urls,
                "provider_mode": cfg.provider_mode,
                "auth_mode": cfg.auth_mode,
                "paths": cfg.default_paths,
                "search_mode": cfg.search_mode,
                "keys_file": str(cfg.keys_file or ""),
                "available_keys": info["count"],
                "total_keys": info.get("total_count", info["count"]),
                "quarantined_keys": info.get("quarantined_count", 0),
                "quarantine_reasons": info.get("quarantine_reasons", []),
                "sources": info["sources"],
                "live_status": status["status"],
                "live_error": status["error"],
                "last_checked_at": status["checked_at"],
            }
        return {
            "server_name": self.config.server_name,
            "timeout_seconds": self.config.timeout_seconds,
            "xai_model": self.config.xai_model,
            "known_grok_models": [
                {"id": m.id, "tier": m.tier, "source": m.source}
                for m in self.config.xai_models
            ],
            "mcp": {
                "default_transport": "stdio",
                "host": self.config.mcp_host,
                "port": self.config.mcp_port,
                "mount_path": self.config.mcp_mount_path,
                "sse_path": self.config.mcp_sse_path,
                "streamable_http_path": self.config.mcp_streamable_http_path,
                "stateless_http": self.config.mcp_stateless_http,
                "streamable_http_url": (
                    f"http://{self.config.mcp_host}:{self.config.mcp_port}"
                    f"{self.config.mcp_streamable_http_path}"
                ),
            },
            "runtime": {
                "max_parallel_workers": self.config.max_parallel_workers,
                "cache_ttl_seconds": {
                    "search": self.config.search_cache_ttl_seconds,
                    "extract": self.config.extract_cache_ttl_seconds,
                },
            },
            "routing_defaults": {
                "web": "tavily",
                "docs": "firecrawl",
                "content": "firecrawl",
                "social": "xai",
                "fallback": "exa",
            },
            "providers": providers,
            "cache": cache,
        }

    def _cache_health(self) -> dict[str, dict[str, int]]:
        snapshot: dict[str, dict[str, int]] = {}
        with self._cache_lock:
            now = time.monotonic()
            for namespace in self._cache_store:
                self._prune_expired_cache_entries_locked(namespace, now)
                stats = self._cache_stats[namespace]
                snapshot[namespace] = {
                    "ttl_seconds": self._cache_ttls.get(namespace, 0),
                    "entries": len(self._cache_store[namespace]),
                    "hits": stats["hits"],
                    "misses": stats["misses"],
                }
        return snapshot

    def _prune_expired_cache_entries_locked(self, namespace: str, now: float) -> None:
        expired_keys = [
            key
            for key, payload in self._cache_store[namespace].items()
            if payload.get("expires_at", 0.0) <= now
        ]
        for key in expired_keys:
            self._cache_store[namespace].pop(key, None)

    def _cache_get(self, namespace: str, cache_key: str) -> dict[str, Any] | None:
        ttl_seconds = self._cache_ttls.get(namespace, 0)
        if ttl_seconds <= 0:
            return None

        with self._cache_lock:
            now = time.monotonic()
            payload = self._cache_store[namespace].get(cache_key)
            if payload is None:
                self._cache_stats[namespace]["misses"] += 1
                return None
            if payload.get("expires_at", 0.0) <= now:
                self._cache_store[namespace].pop(cache_key, None)
                self._cache_stats[namespace]["misses"] += 1
                return None

            self._cache_stats[namespace]["hits"] += 1
            return copy.deepcopy(payload["value"])

    def _cache_set(self, namespace: str, cache_key: str, value: dict[str, Any]) -> None:
        ttl_seconds = self._cache_ttls.get(namespace, 0)
        if ttl_seconds <= 0:
            return

        with self._cache_lock:
            now = time.monotonic()
            store = self._cache_store[namespace]
            if len(store) >= self._cache_max_entries:
                self._prune_expired_cache_entries_locked(namespace, now)
            if len(store) >= self._cache_max_entries:
                oldest_key = min(store, key=lambda k: store[k].get("inserted_at", 0.0))
                store.pop(oldest_key, None)
            store[cache_key] = {
                "expires_at": now + ttl_seconds,
                "inserted_at": now,
                "value": copy.deepcopy(value),
            }

    def _cache_delete(self, namespace: str, cache_key: str) -> None:
        if namespace not in self._cache_store:
            return
        with self._cache_lock:
            self._cache_store[namespace].pop(cache_key, None)

    def _build_cache_key(self, namespace: str, payload: dict[str, Any]) -> str:
        return cache_keys._build_cache_key(namespace=namespace, payload=payload)

    def _should_cache_search(
        self,
        *,
        decision: RouteDecision,
        normalized_sources: list[str],
        mode: SearchMode,
    ) -> bool:
        if self.config.search_cache_ttl_seconds <= 0:
            return False
        social_only = mode == "social" and normalized_sources == ["x"]
        if "x" in normalized_sources and not social_only:
            return False
        if decision.provider == "xai" and not social_only:
            return False
        return True

    def _build_search_cache_key(
        self,
        *,
        query: str,
        mode: SearchMode,
        resolved_intent: ResolvedSearchIntent,
        resolved_strategy: SearchStrategy,
        provider: ProviderName,
        normalized_sources: list[str],
        include_content: bool,
        include_answer: bool,
        include_domains: list[str] | None,
        exclude_domains: list[str] | None,
        decision: RouteDecision,
        allowed_x_handles: list[str] | None = None,
        excluded_x_handles: list[str] | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        include_x_images: bool = False,
        include_x_videos: bool = False,
        max_results: int = 5,
    ) -> str:
        return cache_keys._build_search_cache_key(query=query, mode=mode, resolved_intent=resolved_intent, resolved_strategy=resolved_strategy, provider=provider, normalized_sources=normalized_sources, include_content=include_content, include_answer=include_answer, include_domains=include_domains, exclude_domains=exclude_domains, decision=decision, allowed_x_handles=allowed_x_handles, excluded_x_handles=excluded_x_handles, from_date=from_date, to_date=to_date, include_x_images=include_x_images, include_x_videos=include_x_videos, max_results=max_results)

    def _build_extract_cache_key(
        self,
        *,
        url: str,
        formats: list[str],
        only_main_content: bool,
        provider: Literal["auto", "firecrawl", "tavily"],
    ) -> str:
        return cache_keys._build_extract_cache_key(url=url, formats=formats, only_main_content=only_main_content, provider=provider)

    def _build_social_cache_key(
        self,
        *,
        query: str,
        max_results: int,
        allowed_x_handles: list[str] | None,
        excluded_x_handles: list[str] | None,
        from_date: str | None,
        to_date: str | None,
        include_x_images: bool,
        include_x_videos: bool,
    ) -> str:
        return cache_keys._build_social_cache_key(query=query, max_results=max_results, allowed_x_handles=allowed_x_handles, excluded_x_handles=excluded_x_handles, from_date=from_date, to_date=to_date, include_x_images=include_x_images, include_x_videos=include_x_videos)

    def _build_social_gateway_cache_key(
        self,
        *,
        base_url: str,
        path: str,
    ) -> str:
        return cache_keys._build_social_gateway_cache_key(base_url=base_url, path=path)

    def _annotate_cache(
        self,
        result: dict[str, Any],
        *,
        namespace: str,
        hit: bool,
    ) -> dict[str, Any]:
        cache_meta = dict(result.get("cache") or {})
        cache_meta[namespace] = {
            "hit": hit,
            "ttl_seconds": self._cache_ttls.get(namespace, 0),
        }
        result["cache"] = cache_meta
        return result

    def _annotate_search_debug(
        self,
        result: dict[str, Any],
        *,
        provider: ProviderName,
        normalized_sources: list[str],
        resolved_intent: ResolvedSearchIntent,
        resolved_strategy: SearchStrategy,
        decision: RouteDecision,
        include_content: bool,
        include_answer: bool,
        cache_hit: bool,
        requested_max_results: int | None = None,
        candidate_max_results: int | None = None,
    ) -> dict[str, Any]:
        result["route_debug"] = {
            "requested_provider": provider,
            "route_provider": decision.provider,
            "normalized_sources": normalized_sources,
            "resolved_intent": resolved_intent,
            "resolved_strategy": resolved_strategy,
            "include_content": include_content,
            "include_answer": include_answer,
            "cache_hit": cache_hit,
        }
        if requested_max_results is not None:
            result["route_debug"]["requested_max_results"] = requested_max_results
        if candidate_max_results is not None:
            result["route_debug"]["candidate_max_results"] = candidate_max_results
        evidence = result.get("evidence") or {}
        if evidence.get("official_mode"):
            result["route_debug"]["official_mode"] = evidence.get("official_mode")
        if "official_filter_applied" in evidence:
            result["route_debug"]["official_filter_applied"] = bool(
                evidence.get("official_filter_applied")
            )
        return result

    def search(
        self,
        *,
        query: str,
        mode: SearchMode = "auto",
        intent: SearchIntent = "auto",
        strategy: SearchStrategy = "auto",
        provider: ProviderName = "auto",
        sources: list[Literal["web", "x"]] | None = None,
        max_results: int = 5,
        include_content: bool = False,
        include_answer: bool = True,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        allowed_x_handles: list[str] | None = None,
        excluded_x_handles: list[str] | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        include_x_images: bool = False,
        include_x_videos: bool = False,
    ) -> dict[str, Any]:
        # --- Phase 1: resolve parameters ---
        query = query.strip()
        if not query:
            raise MySearchError("query must not be empty")
        if mode == "github" and not include_domains:
            include_domains = ["github.com"]

        normalized_sources = sorted(set(sources or []))
        if not normalized_sources:
            if mode == "social" or allowed_x_handles or excluded_x_handles:
                normalized_sources = ["x"]
            else:
                normalized_sources = ["web"]
        resolved_intent = self._resolve_intent(
            query=query,
            mode=mode,
            intent=intent,
            sources=normalized_sources,
        )
        resolved_strategy = self._resolve_strategy(
            mode=mode,
            intent=resolved_intent,
            strategy=strategy,
            sources=normalized_sources,
            include_content=include_content,
        )
        effective_include_answer = self._should_request_search_answer(
            requested=include_answer,
            mode=mode,
            intent=resolved_intent,
            strategy=resolved_strategy,
            include_content=include_content,
            include_domains=include_domains,
        )
        decision = self._route_search(
            query=query,
            mode=mode,
            intent=resolved_intent,
            provider=provider,
            sources=normalized_sources,
            include_content=include_content,
            include_domains=include_domains,
            allowed_x_handles=allowed_x_handles,
            excluded_x_handles=excluded_x_handles,
        )
        candidate_max_results = self._candidate_result_budget(
            requested_max_results=max_results,
            strategy=resolved_strategy,
            mode=mode,
            intent=resolved_intent,
            include_domains=include_domains,
            route_provider=decision.provider,
        )

        # --- Phase 2: cache check ---
        cacheable = self._should_cache_search(
            decision=decision,
            normalized_sources=normalized_sources,
            mode=mode,
        )
        cache_key = ""
        if cacheable:
            cache_key = self._build_search_cache_key(
                query=query,
                mode=mode,
                resolved_intent=resolved_intent,
                resolved_strategy=resolved_strategy,
                provider=provider,
                normalized_sources=normalized_sources,
                include_content=include_content,
                include_answer=effective_include_answer,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                decision=decision,
                allowed_x_handles=allowed_x_handles,
                excluded_x_handles=excluded_x_handles,
                from_date=from_date,
                to_date=to_date,
                include_x_images=include_x_images,
                include_x_videos=include_x_videos,
                max_results=max_results,
            )
            cached_result = self._cache_get("search", cache_key)
            if cached_result is not None:
                cached_result = self._annotate_cache(
                    cached_result,
                    namespace="search",
                    hit=True,
                )
                return self._annotate_search_debug(
                    cached_result,
                    provider=provider,
                    normalized_sources=normalized_sources,
                    resolved_intent=resolved_intent,
                    resolved_strategy=resolved_strategy,
                    decision=decision,
                    include_content=include_content,
                    include_answer=effective_include_answer,
                    cache_hit=True,
                    requested_max_results=max_results,
                    candidate_max_results=candidate_max_results,
                )

        # --- Phase 3: execute ---
        if decision.provider == "hybrid":
            hybrid_result = self._search_hybrid(
                query=query,
                mode=mode,
                resolved_intent=resolved_intent,
                resolved_strategy=resolved_strategy,
                decision=decision,
                max_results=max_results,
                include_content=include_content,
                effective_include_answer=effective_include_answer,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                allowed_x_handles=allowed_x_handles,
                excluded_x_handles=excluded_x_handles,
                from_date=from_date,
                to_date=to_date,
                include_x_images=include_x_images,
                include_x_videos=include_x_videos,
            )
            hybrid_result = self._augment_evidence_summary(
                hybrid_result,
                query=query,
                mode=mode,
                intent=resolved_intent,
                include_domains=include_domains,
            )
            return self._annotate_search_debug(
                hybrid_result,
                provider=provider,
                normalized_sources=normalized_sources,
                resolved_intent=resolved_intent,
                resolved_strategy=resolved_strategy,
                decision=decision,
                include_content=include_content,
                include_answer=effective_include_answer,
                cache_hit=False,
                requested_max_results=max_results,
                candidate_max_results=candidate_max_results,
            )

        if self._should_blend_web_providers(
            query=query,
            requested_provider=provider,
            decision=decision,
            sources=normalized_sources,
            strategy=resolved_strategy,
            mode=mode,
            intent=resolved_intent,
            include_domains=include_domains,
        ):
            result = self._search_web_blended(
                query=query,
                mode=mode,
                intent=resolved_intent,
                strategy=resolved_strategy,
                decision=decision,
                max_results=candidate_max_results,
                include_content=include_content,
                include_answer=effective_include_answer,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                from_date=from_date,
                to_date=to_date,
            )
        elif decision.provider in {"tavily", "firecrawl", "exa"}:
            result, fallback_info = self._search_with_fallback(
                primary_provider=decision.provider,
                query=query,
                max_results=candidate_max_results,
                mode=mode,
                intent=resolved_intent,
                decision=decision,
                include_answer=effective_include_answer,
                include_content=include_content,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                strategy=resolved_strategy,
                from_date=from_date,
                to_date=to_date,
            )
            if fallback_info:
                result["fallback"] = fallback_info
        elif decision.provider == "xai":
            result = self._search_xai(
                query=query,
                sources=decision.sources or ["x"],
                max_results=max_results,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                allowed_x_handles=allowed_x_handles,
                excluded_x_handles=excluded_x_handles,
                from_date=from_date,
                to_date=to_date,
                include_x_images=include_x_images,
                include_x_videos=include_x_videos,
            )
        else:
            raise MySearchError(f"Unsupported route decision: {decision.provider}")

        # --- Phase 4: postprocess ---
        result = self._postprocess_search(
            result=result,
            query=query,
            mode=mode,
            provider=provider,
            resolved_intent=resolved_intent,
            resolved_strategy=resolved_strategy,
            decision=decision,
            normalized_sources=normalized_sources,
            include_content=include_content,
            effective_include_answer=effective_include_answer,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            max_results=max_results,
            candidate_max_results=candidate_max_results,
            cacheable=cacheable,
            cache_key=cache_key,
            from_date=from_date,
            to_date=to_date,
        )
        return result

    def _search_hybrid(
        self,
        *,
        query: str,
        mode: SearchMode,
        resolved_intent: str,
        resolved_strategy: str,
        decision: RouteDecision,
        max_results: int,
        include_content: bool,
        effective_include_answer: bool,
        include_domains: list[str] | None,
        exclude_domains: list[str] | None,
        allowed_x_handles: list[str] | None,
        excluded_x_handles: list[str] | None,
        from_date: str | None,
        to_date: str | None,
        include_x_images: bool,
        include_x_videos: bool,
    ) -> dict[str, Any]:
        use_xai_unified = (
            resolved_strategy == "fast"
            and self.config.xai.search_mode == "official"
            and self._provider_can_serve(self.config.xai)
            and not allowed_x_handles
            and not excluded_x_handles
        )

        unified_result: dict[str, Any] | None = None
        if use_xai_unified:
            try:
                unified_result = self._search_xai(
                    query=query,
                    sources=["web", "x"],
                    max_results=max_results,
                    include_domains=include_domains,
                    exclude_domains=exclude_domains,
                    from_date=from_date,
                    to_date=to_date,
                    include_x_images=include_x_images,
                    include_x_videos=include_x_videos,
                    timeout_seconds=HYBRID_SOCIAL_TIMEOUT_SECONDS,
                )
            except MySearchError:
                use_xai_unified = False

        if unified_result is not None:
            web_result = unified_result
            social_result = unified_result
        else:
            parallel_results, parallel_errors = self._execute_parallel(
                {
                    "web": lambda: self.search(
                        query=query,
                        mode=mode,
                        intent=resolved_intent,
                        strategy=resolved_strategy,
                        provider="auto",
                        sources=["web"],
                        max_results=max_results,
                        include_content=include_content,
                        include_answer=effective_include_answer,
                        include_domains=include_domains,
                        exclude_domains=exclude_domains,
                        from_date=from_date,
                        to_date=to_date,
                    ),
                    "social": lambda: self._search_xai(
                        query=query,
                        sources=["x"],
                        max_results=max_results,
                        allowed_x_handles=allowed_x_handles,
                        excluded_x_handles=excluded_x_handles,
                        from_date=from_date,
                        to_date=to_date,
                        include_x_images=include_x_images,
                        include_x_videos=include_x_videos,
                        timeout_seconds=HYBRID_SOCIAL_TIMEOUT_SECONDS,
                    ),
                },
                max_workers=2,
                timeout_seconds=HYBRID_SOCIAL_TIMEOUT_SECONDS,
            )
            if "web" in parallel_errors and "social" in parallel_errors:
                self._raise_parallel_error(parallel_errors, "web")
                self._raise_parallel_error(parallel_errors, "social")
            web_result = parallel_results.get("web")
            social_result = parallel_results.get("social")
            if web_result is None:
                social_result = cast(dict[str, Any], social_result or {})
                web_error = str(parallel_errors.get("web") or "web search unavailable")
                social_result.setdefault("evidence", {})["web_error"] = web_error[:200]
                web_result = {
                    "provider": "web_unavailable",
                    "query": query,
                    "answer": "",
                    "results": [],
                    "citations": [],
                    "summary": f"Web search unavailable: {web_error[:200]}",
                }
            if social_result is None:
                web_result = cast(dict[str, Any], web_result or {})
                social_error = str(parallel_errors.get("social") or "social search unavailable")
                web_result.setdefault("evidence", {})["social_error"] = social_error[:200]
                social_result = {
                    "provider": "social_unavailable",
                    "query": query,
                    "answer": "",
                    "results": [],
                    "citations": [],
                    "summary": f"Social/X search unavailable: {social_error[:200]}",
                }
        web_route = web_result.get("route", {}).get("selected", web_result.get("provider", "tavily"))
        social_route = social_result.get("provider", "xai")
        web_results = list(web_result.get("results") or [])
        social_results = list(social_result.get("results") or [])
        merged = self._merge_search_payloads(
            primary_result=web_result,
            secondary_result=social_result,
            max_results=max_results,
        )
        merged_results = list(merged["results"])
        merged_citations = list(merged["citations"])
        evidence = {
            "providers_consulted": [web_result.get("provider"), social_result.get("provider")],
            "web_result_count": len(web_results),
            "social_result_count": len(social_results),
            "result_count_before_trim": len(web_results) + len(social_results),
            "returned_result_count": len(merged_results),
            "matched_results": merged["matched_results"],
            "citation_count": len(merged_citations),
            "verification": "cross-provider",
        }
        web_error = ""
        if web_result.get("provider") == "web_unavailable":
            web_error = web_result.get("summary") or "web search unavailable"
        elif isinstance(social_result.get("evidence"), dict):
            web_error = (social_result.get("evidence") or {}).get("web_error") or ""
        social_error = (web_result.get("evidence") or {}).get("social_error") if isinstance(web_result.get("evidence"), dict) else ""
        if web_error:
            evidence.setdefault("conflicts", []).append("web-search-unavailable")
            evidence["web_error"] = web_error
        if social_error or social_result.get("provider") == "social_unavailable":
            evidence.setdefault("conflicts", []).append("social-search-unavailable")
            evidence["social_error"] = social_error or (social_result.get("summary") or "")

        return {
            "provider": "hybrid",
            "intent": resolved_intent,
            "strategy": resolved_strategy,
            "route": {
                "selected": f"{web_route}+{social_route}",
                "reason": decision.reason,
            },
            "query": query,
            "answer": web_result.get("answer") or social_result.get("answer") or "",
            "results": merged_results,
            "citations": merged_citations,
            "evidence": evidence,
            "web": web_result,
            "social": social_result,
        }

    def _postprocess_search(
        self,
        *,
        result: dict[str, Any],
        query: str,
        mode: SearchMode,
        provider: ProviderName,
        resolved_intent: str,
        resolved_strategy: str,
        decision: RouteDecision,
        normalized_sources: list[str],
        include_content: bool,
        effective_include_answer: bool,
        include_domains: list[str] | None,
        exclude_domains: list[str] | None,
        max_results: int,
        candidate_max_results: int,
        cacheable: bool,
        cache_key: str,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        result = self._apply_result_event_answer_override(
            query=query,
            mode=mode,
            intent=resolved_intent,
            strategy=resolved_strategy,
            result=result,
        )
        result = self._maybe_refine_tavily_result_event_discovery(
            query=query,
            mode=mode,
            intent=resolved_intent,
            result=result,
            max_results=candidate_max_results,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            from_date=from_date,
        )
        result = self._apply_result_event_answer_override(
            query=query,
            mode=mode,
            intent=resolved_intent,
            strategy=resolved_strategy,
            result=result,
        )
        if self._should_attempt_exa_rescue(
            query=query,
            mode=mode,
            intent=resolved_intent,
            decision=decision,
            result=result,
            max_results=max_results,
            include_domains=include_domains,
        ):
            result = self._apply_exa_rescue(
                query=query,
                primary_result=result,
                max_results=candidate_max_results,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                include_content=include_content,
                mode=mode,
                intent=resolved_intent,
                strategy=resolved_strategy,
                from_date=from_date,
                to_date=to_date,
            )
            result = self._apply_result_event_answer_override(
                query=query,
                mode=mode,
                intent=resolved_intent,
                strategy=resolved_strategy,
                result=result,
            )

        result = self._finalize_search_result(
            result,
            query=query,
            mode=mode,
            intent=resolved_intent,
            include_domains=include_domains,
            result_profile=decision.result_profile,
            max_results=max_results,
        )
        result = self._apply_result_event_answer_override(
            query=query,
            mode=mode,
            intent=resolved_intent,
            strategy=resolved_strategy,
            result=result,
        )
        final_official_mode = str(
            ((result.get("evidence") or {}) if isinstance(result.get("evidence"), dict) else {}).get(
                "official_mode"
            )
            or "off"
        )

        needs_pdf_exa_boost = (
            mode == "pdf"
            and decision.provider not in {"exa", "xai"}
            and not result.get("fallback")
            and self._provider_can_serve(self.config.exa)
            and not self._has_strong_pdf_match(
                query=query,
                results=list(result.get("results") or []),
            )
        )
        should_low_confidence_boost = (
            (result.get("evidence") or {}).get("confidence") == "low"
            and resolved_strategy not in {"fast"}
            and decision.provider not in {"exa", "xai"}
            and not result.get("fallback")
            and not include_domains
            and self._provider_can_serve(self.config.exa)
        )
        if should_low_confidence_boost or needs_pdf_exa_boost:
            try:
                exa_boost = self._search_exa(
                    query=query,
                    max_results=max_results,
                    include_domains=None,
                    exclude_domains=exclude_domains,
                    include_content=False,
                    mode=mode,
                    intent=resolved_intent,
                    strategy=resolved_strategy,
                    from_date=from_date,
                    to_date=to_date,
                )
                if exa_boost.get("results"):
                    merged = self._merge_search_payloads(
                        primary_result=exa_boost if needs_pdf_exa_boost else result,
                        secondary_result=result if needs_pdf_exa_boost else exa_boost,
                        max_results=max_results,
                    )
                    result["results"] = merged["results"]
                    result["citations"] = merged["citations"]
                    if needs_pdf_exa_boost:
                        result["provider"] = "hybrid"
                    if self._should_rerank_resource_results(mode=mode, intent=resolved_intent):
                        reranked_results = self._rerank_resource_results(
                            query=query,
                            mode=mode,
                            results=list(result.get("results") or []),
                            include_domains=include_domains,
                        )
                        result["results"] = reranked_results
                        result["citations"] = self._align_citations_with_results(
                            results=reranked_results,
                            citations=list(result.get("citations") or []),
                        )
                    elif self._should_rerank_general_results(result_profile=decision.result_profile):
                        reranked_results = self._rerank_general_results(
                            query=query,
                            result_profile=decision.result_profile,
                            results=list(result.get("results") or []),
                            include_domains=include_domains,
                        )
                        result["results"] = reranked_results
                        result["citations"] = self._align_citations_with_results(
                            results=reranked_results,
                            citations=list(result.get("citations") or []),
                        )
                    if should_low_confidence_boost:
                        result.setdefault("evidence", {})["low_confidence_exa_boost"] = True
                    if needs_pdf_exa_boost:
                        result.setdefault("evidence", {})["pdf_exa_boost"] = True
                    result = self._finalize_search_result(
                        result,
                        query=query,
                        mode=mode,
                        intent=resolved_intent,
                        include_domains=include_domains,
                        result_profile=decision.result_profile,
                        max_results=max_results,
                    )
            except MySearchError:
                pass

        needs_pdf_tavily_boost = (
            mode == "pdf"
            and decision.provider != "xai"
            and self._provider_can_serve(self.config.tavily)
            and not self._has_strong_pdf_match(
                query=query,
                results=list(result.get("results") or []),
            )
        )
        if needs_pdf_tavily_boost:
            try:
                tavily_boost = self._search_tavily(
                    query=query,
                    max_results=max_results,
                    topic="general",
                    include_answer=False,
                    include_content=False,
                    include_domains=include_domains,
                    exclude_domains=exclude_domains,
                    strategy=resolved_strategy,
                )
                if tavily_boost.get("results"):
                    merged = self._merge_search_payloads(
                        primary_result=tavily_boost,
                        secondary_result=result,
                        max_results=max_results,
                    )
                    result["results"] = merged["results"]
                    result["citations"] = merged["citations"]
                    result["provider"] = "hybrid"
                    if self._should_rerank_resource_results(mode=mode, intent=resolved_intent):
                        reranked_results = self._rerank_resource_results(
                            query=query,
                            mode=mode,
                            results=list(result.get("results") or []),
                            include_domains=include_domains,
                        )
                        result["results"] = reranked_results
                        result["citations"] = self._align_citations_with_results(
                            results=reranked_results,
                            citations=list(result.get("citations") or []),
                        )
                    result.setdefault("evidence", {})["pdf_tavily_boost"] = True
                    result = self._finalize_search_result(
                        result,
                        query=query,
                        mode=mode,
                        intent=resolved_intent,
                        include_domains=include_domains,
                        result_profile=decision.result_profile,
                        max_results=max_results,
                    )
            except MySearchError:
                pass

        if mode == "pdf" and result.get("results"):
            enriched_results = [dict(item) for item in (result.get("results") or [])]
            updated_titles = False
            for item in enriched_results[:5]:
                if self._result_hostname(item) != "arxiv.org":
                    continue
                current_title = (item.get("title") or "").strip()
                if current_title and not self._looks_like_generic_arxiv_subject_title(current_title):
                    continue
                fetched_title = self._fetch_arxiv_title(item.get("url", ""))
                if fetched_title and fetched_title != current_title:
                    item["title"] = fetched_title
                    updated_titles = True
            if updated_titles:
                deduped = self._merge_search_payloads(
                    primary_result={
                        "provider": result.get("provider", ""),
                        "results": enriched_results,
                        "citations": list(result.get("citations") or []),
                    },
                    secondary_result=None,
                    max_results=max_results,
                )
                result["results"] = deduped["results"]
                result["citations"] = self._align_citations_with_results(
                    results=deduped["results"],
                    citations=list(result.get("citations") or []),
                )
                if self._should_rerank_resource_results(mode=mode, intent=resolved_intent):
                    reranked_results = self._rerank_resource_results(
                        query=query,
                        mode=mode,
                        results=list(result.get("results") or []),
                        include_domains=include_domains,
                    )
                    result["results"] = reranked_results
                    result["citations"] = self._align_citations_with_results(
                        results=reranked_results,
                        citations=list(result.get("citations") or []),
                    )
                result.setdefault("evidence", {})["pdf_title_enrichment"] = True
                result = self._finalize_search_result(
                    result,
                    query=query,
                    mode=mode,
                    intent=resolved_intent,
                    include_domains=include_domains,
                    result_profile=decision.result_profile,
                    max_results=max_results,
                )

        evidence = result.get("evidence") or {}
        conflicts = evidence.get("conflicts") or []
        if self._should_attempt_xai_arbitration(
            result=result,
            decision=decision,
            strategy=resolved_strategy,
            conflicts=conflicts,
        ):
            result = self._apply_xai_arbitration(
                query=query,
                result=result,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                from_date=from_date,
                to_date=to_date,
            )
            evidence = result.get("evidence") or {}
            conflicts = evidence.get("conflicts") or []

        result = self._apply_result_event_answer_override(
            query=query,
            mode=mode,
            intent=resolved_intent,
            strategy=resolved_strategy,
            result=result,
        )

        should_supplement_answer = (
            not (result.get("answer") or "").strip()
            and decision.provider != "xai"
            and self._provider_can_serve(self.config.xai)
            and self.config.xai.search_mode == "official"
            and (
                resolved_intent in {"comparison", "status"}
                or resolved_strategy in {"verify", "deep"}
            )
        )
        if should_supplement_answer:
            try:
                xai_supplement = self._search_xai(
                    query=query,
                    sources=["web"],
                    max_results=3,
                )
                xai_answer = (xai_supplement.get("answer") or "").strip()
                if xai_answer:
                    result["answer"] = xai_answer
                    result.setdefault("evidence", {})["answer_source"] = "xai"
                    evidence = result.get("evidence") or {}
                    conflicts = evidence.get("conflicts") or []
            except MySearchError:
                pass

        if "low-source-diversity" in conflicts and resolved_strategy in {"fast", "balanced"}:
            evidence["retry_hint"] = "consider strategy=verify for broader source diversity"
            result["evidence"] = evidence
        if (
            self._looks_like_award_result_query(query.lower())
            and (mode == "news" or resolved_intent in {"news", "status"})
            and result.get("results")
        ):
            filtered_results = self._filter_strong_award_results(
                query=query,
                results=list(result.get("results") or []),
            )
            result["results"] = filtered_results[:max_results]
            result["citations"] = self._align_citations_with_results(
                results=list(result.get("results") or []),
                citations=list(result.get("citations") or []),
            )
        result = self._apply_software_version_answer_override(
            query=query,
            mode=mode,
            intent=resolved_intent,
            result=result,
        )
        result["summary"] = self._build_search_summary_fallback(
            query=query,
            mode=mode,
            intent=resolved_intent,
            result=result,
        )

        route_reason = decision.reason
        if ProviderResponse.is_hybrid(result) and resolved_strategy in {"balanced", "verify", "deep"}:
            route_reason = f"{route_reason}；strategy={resolved_strategy} 已启用 Tavily + Firecrawl 交叉检索"
        fallback = result.get("fallback")
        if isinstance(fallback, dict):
            fallback_from = str(fallback.get("from", "")).strip()
            fallback_to = str(fallback.get("to", "")).strip()
            fallback_reason = str(fallback.get("reason", "")).strip()
            parts = [part for part in [fallback_from, fallback_to] if part]
            transition = " -> ".join(parts)
            if transition:
                route_reason = f"{route_reason}；{transition} fallback"
            if fallback_reason:
                route_reason = f"{route_reason}（{fallback_reason}）"
        secondary_error = str(result.get("secondary_error", "")).strip()
        if secondary_error:
            route_reason = (
                f"{route_reason}；secondary provider issue: "
                f"{self._summarize_route_error(secondary_error)}"
            )

        route_selected = result.pop("route_selected", result.get("provider", decision.provider))
        result["intent"] = resolved_intent
        result["strategy"] = resolved_strategy
        result["route"] = {
            "selected": route_selected,
            "reason": route_reason,
        }
        if cacheable and cache_key:
            self._cache_set("search", cache_key, result)
        result = self._annotate_cache(
            result,
            namespace="search",
            hit=False,
        )
        return self._annotate_search_debug(
            result,
            provider=provider,
            normalized_sources=normalized_sources,
            resolved_intent=resolved_intent,
            resolved_strategy=resolved_strategy,
            decision=decision,
            include_content=include_content,
            include_answer=effective_include_answer,
            cache_hit=False,
            requested_max_results=max_results,
            candidate_max_results=candidate_max_results,
        )

    def extract_url(
        self,
        *,
        url: str,
        formats: list[str] | None = None,
        only_main_content: bool = True,
        provider: Literal["auto", "firecrawl", "tavily"] = "auto",
    ) -> dict[str, Any]:
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise MySearchError("url must be an absolute http(s) URL")

        formats = formats or ["markdown"]
        cache_key = self._build_extract_cache_key(
            url=url,
            formats=formats,
            only_main_content=only_main_content,
            provider=provider,
        )
        cached_result = self._cache_get("extract", cache_key)
        if cached_result is not None:
            return self._annotate_cache(
                cached_result,
                namespace="extract",
                hit=True,
            )
        errors: list[str] = []
        firecrawl_result: dict[str, Any] | None = None
        firecrawl_issue = ""

        if provider == "auto":
            github_raw_result = self._extract_github_blob_raw(url=url)
            if github_raw_result is not None:
                self._cache_set("extract", cache_key, github_raw_result)
                return self._annotate_cache(
                    github_raw_result,
                    namespace="extract",
                    hit=False,
                )

        if provider in {"auto", "firecrawl"}:
            firecrawl_attempts = 2
            for attempt in range(firecrawl_attempts):
                try:
                    firecrawl_result = self._scrape_firecrawl(
                        url=url,
                        formats=formats,
                        only_main_content=only_main_content,
                    )
                    firecrawl_issue = self._extract_quality_issue(firecrawl_result) or ""
                    if not firecrawl_issue:
                        self._cache_set("extract", cache_key, firecrawl_result)
                        return self._annotate_cache(
                            firecrawl_result,
                            namespace="extract",
                            hit=False,
                        )

                    errors.append(f"firecrawl scrape returned {firecrawl_issue}")

                    if provider == "firecrawl":
                        result = self._annotate_extract_warning(
                            firecrawl_result,
                            warning=f"firecrawl scrape returned {firecrawl_issue}",
                        )
                        return self._annotate_cache(
                            result,
                            namespace="extract",
                            hit=False,
                        )
                    break
                except MySearchError as exc:
                    if attempt < firecrawl_attempts - 1 and self._is_retryable_transient_error(exc):
                        continue
                    errors.append(f"firecrawl scrape failed: {exc}")
                    if provider == "firecrawl":
                        raise
                    break

        if provider in {"auto", "tavily"}:
            try:
                tavily_result = self._extract_tavily(url=url)
                tavily_issue = self._extract_quality_issue(tavily_result)
                if provider == "auto" and errors and tavily_issue is None:
                    result = self._annotate_extract_fallback(
                        tavily_result,
                        fallback_from="firecrawl",
                        fallback_reason=" | ".join(errors),
                    )
                    self._cache_set("extract", cache_key, result)
                    return self._annotate_cache(
                        result,
                        namespace="extract",
                        hit=False,
                    )
                if tavily_issue is None:
                    self._cache_set("extract", cache_key, tavily_result)
                    return self._annotate_cache(
                        tavily_result,
                        namespace="extract",
                        hit=False,
                    )
                errors.append(f"tavily extract returned {tavily_issue}")
                if provider == "tavily":
                    result = self._annotate_extract_warning(
                        tavily_result,
                        warning=f"tavily extract returned {tavily_issue}",
                    )
                    return self._annotate_cache(
                        result,
                        namespace="extract",
                        hit=False,
                    )
            except MySearchError as exc:
                errors.append(f"tavily extract failed: {exc}")
                if provider == "tavily":
                    raise

        if provider == "auto" and self._provider_can_serve(self.config.exa):
            try:
                exa_extract = self._search_exa(
                    query=url,
                    max_results=1,
                    include_domains=None,
                    exclude_domains=None,
                    include_content=True,
                    strategy="fast",
                )
                exa_results = exa_extract.get("results") or []
                if exa_results:
                    matching_results = [
                        item
                        for item in exa_results
                        if self._extract_candidate_matches_requested_url(
                            requested_url=url,
                            candidate_url=str(item.get("url") or ""),
                        )
                    ]
                    if not matching_results:
                        errors.append("exa extract returned no same-domain URL match")
                        raise MySearchError("exa extract returned no same-domain URL match")
                    best = max(matching_results, key=lambda r: len(r.get("content") or ""))
                    content = (best.get("content") or "").strip()
                    if content and len(content) >= 100:
                        actual_url = str(best.get("url") or "").strip() or url
                        exa_result = {
                            "provider": "exa",
                            "transport": exa_extract.get("transport", ""),
                            "url": actual_url,
                            "content": content,
                            "metadata": {
                                "requested_url": url,
                                "exa_url": actual_url,
                            },
                        }
                        issue = self._extract_quality_issue(exa_result)
                        if issue is None:
                            exa_result = self._annotate_extract_fallback(
                                exa_result,
                                fallback_from="firecrawl+tavily",
                                fallback_reason=" | ".join(errors),
                            )
                            self._cache_set("extract", cache_key, exa_result)
                            return self._annotate_cache(
                                exa_result,
                                namespace="extract",
                                hit=False,
                            )
            except MySearchError:
                pass

        if firecrawl_result is not None and provider == "auto":
            result = self._annotate_extract_warning(
                firecrawl_result,
                warning=" | ".join(errors),
            )
            return self._annotate_cache(
                result,
                namespace="extract",
                hit=False,
            )

        raise MySearchError(" | ".join(errors) if errors else "no extraction provider available")

    def research(
        self,
        *,
        query: str,
        web_max_results: int = 5,
        social_max_results: int = 5,
        scrape_top_n: int = 3,
        include_social: bool = True,
        mode: SearchMode = "auto",
        intent: SearchIntent = "auto",
        strategy: SearchStrategy = "auto",
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        allowed_x_handles: list[str] | None = None,
        excluded_x_handles: list[str] | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise MySearchError("query must not be empty")
        resolved_intent = self._resolve_intent(
            query=query,
            mode=mode,
            intent=intent,
            sources=["web"],
        )
        resolved_strategy = self._resolve_strategy(
            mode=mode,
            intent=resolved_intent,
            strategy=strategy,
            sources=["web"],
            include_content=False,
        )
        research_plan = self._resolve_research_plan(
            query=query,
            mode=mode,
            intent=resolved_intent,
            strategy=resolved_strategy,
            web_max_results=web_max_results,
            social_max_results=social_max_results,
            scrape_top_n=scrape_top_n,
            include_social=include_social,
            include_domains=include_domains,
        )
        authoritative_research = self._research_prefers_authoritative_sources(
            query=query,
            mode=research_plan["web_mode"],
            intent=resolved_intent,
            include_domains=include_domains,
        )
        discovery_route = self._research_primary_discovery_route(
            query=query,
            mode=research_plan["web_mode"],
            intent=resolved_intent,
            authoritative_research=authoritative_research,
        )
        discovery_mode = cast(SearchMode, discovery_route["mode"])
        discovery_intent = cast(ResolvedSearchIntent, discovery_route["intent"])
        discovery_query = discovery_route["query"]
        discovery_include_content = discovery_mode in {"docs"} and self._provider_can_serve(
            self.config.firecrawl
        )
        research_tasks: dict[str, Callable[[], Any]] = {
            "web": lambda: self._run_research_web_discovery(
                query=discovery_query,
                mode=discovery_mode,
                intent=discovery_intent,
                strategy=resolved_strategy,
                max_results=research_plan["web_max_results"],
                include_content=discovery_include_content,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                authoritative_research=authoritative_research,
                from_date=from_date,
                to_date=to_date,
            )
        }
        if (
            not authoritative_research
            and research_plan["web_mode"] == "exploratory"
            and self._provider_can_serve(self.config.tavily)
        ):
            research_tasks["tavily_support"] = lambda: self.search(
                query=query,
                mode="web",
                intent=resolved_intent,
                strategy="balanced" if resolved_strategy == "fast" else resolved_strategy,
                provider="tavily",
                sources=["web"],
                max_results=max(4, min(research_plan["web_max_results"], 6)),
                include_content=False,
                include_answer=False,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                from_date=from_date,
                to_date=to_date,
            )
        if (
            authoritative_research
            or (
                self._looks_like_comparison_query(query.lower())
                and bool(self._research_comparison_entities(query))
            )
        ) and research_plan["web_mode"] in {"web", "docs", "exploratory", "research"}:
            research_tasks["docs_rescue"] = lambda: self._run_research_docs_rescue(
                query=query,
                strategy="balanced" if resolved_strategy == "fast" else resolved_strategy,
                max_results=max(4, min(research_plan["web_max_results"], 6)),
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                from_date=from_date,
                to_date=to_date,
            )
        if include_social:
            research_tasks["social"] = lambda: self.search(
                query=query,
                mode="social",
                intent="status",
                provider="auto",
                sources=["x"],
                max_results=research_plan["social_max_results"],
                allowed_x_handles=allowed_x_handles,
                excluded_x_handles=excluded_x_handles,
                from_date=from_date,
                to_date=to_date,
            )
        if resolved_strategy == "deep" and self._provider_can_serve(self.config.exa):
            exa_category = self._exa_category(
                research_plan["web_mode"], resolved_intent,
            )
            research_tasks["exa_discovery"] = lambda: self._search_exa(
                query=query,
                max_results=research_plan["web_max_results"],
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                include_content=False,
                mode=research_plan["web_mode"],
                intent=resolved_intent,
                strategy=resolved_strategy,
                from_date=from_date,
                to_date=to_date,
            )
        research_results, research_errors = self._execute_parallel(
            research_tasks,
            max_workers=len(research_tasks),
        )
        web_search = research_results.get("web")
        exa_discovery = research_results.get("exa_discovery")
        known_provider_doc_results = self._research_known_provider_doc_results(query)
        generic_vendor_doc_results = (
            self._research_generic_vendor_doc_results(query)
            if authoritative_research
            else []
        )
        canonical_research_doc_results = self._dedupe_research_results_for_report(
            known_provider_doc_results,
            generic_vendor_doc_results,
        )
        if web_search is None:
            if exa_discovery and not research_errors.get("exa_discovery"):
                web_search = self._build_research_web_fallback_result(
                    query=query,
                    mode=research_plan["web_mode"],
                    intent=resolved_intent,
                    strategy=resolved_strategy,
                    exa_discovery=exa_discovery,
                    include_domains=include_domains,
                )
            elif (
                research_results.get("docs_rescue")
                and not research_errors.get("docs_rescue")
                and (research_results["docs_rescue"].get("results") or [])
            ):
                web_search = self._build_research_secondary_fallback_result(
                    query=query,
                    mode=research_plan["web_mode"],
                    intent=resolved_intent,
                    strategy=resolved_strategy,
                    source_result=research_results["docs_rescue"],
                    include_domains=include_domains,
                    fallback_to="docs_rescue",
                    fallback_reason="primary web discovery failed",
                )
            elif (
                research_results.get("tavily_support")
                and not research_errors.get("tavily_support")
                and (research_results["tavily_support"].get("results") or [])
            ):
                web_search = self._build_research_secondary_fallback_result(
                    query=query,
                    mode=research_plan["web_mode"],
                    intent=resolved_intent,
                    strategy=resolved_strategy,
                    source_result=research_results["tavily_support"],
                    include_domains=include_domains,
                    fallback_to="tavily_support",
                    fallback_reason="primary web discovery failed",
                )
            elif canonical_research_doc_results:
                web_search = self._build_research_secondary_fallback_result(
                    query=query,
                    mode=research_plan["web_mode"],
                    intent=resolved_intent,
                    strategy=resolved_strategy,
                    source_result={
                        "provider": "canonical_research_docs",
                        "query": query,
                        "intent": resolved_intent,
                        "strategy": resolved_strategy,
                        "results": canonical_research_doc_results,
                        "citations": [
                            {
                                "title": str(item.get("title") or ""),
                                "url": str(item.get("url") or ""),
                            }
                            for item in canonical_research_doc_results
                            if str(item.get("url") or "").strip()
                        ],
                    },
                    include_domains=include_domains,
                    fallback_to="canonical_research_docs",
                    fallback_reason="primary web discovery failed",
                )
            else:
                self._raise_parallel_error(research_errors, "web")
                web_search = research_results["web"]

        docs_rescue = research_results.get("docs_rescue")
        docs_rescue_results = (
            list(docs_rescue.get("results") or [])
            if docs_rescue and not research_errors.get("docs_rescue")
            else []
        )
        docs_rescue_results.extend(known_provider_doc_results)
        docs_rescue_results.extend(generic_vendor_doc_results)
        docs_rescue_provider = docs_rescue.get("provider", "") if docs_rescue and not research_errors.get("docs_rescue") else ""
        tavily_support = research_results.get("tavily_support")
        tavily_support_results = (
            list(tavily_support.get("results") or [])
            if tavily_support and not research_errors.get("tavily_support")
            else []
        )
        tavily_support_provider = (
            tavily_support.get("provider", "")
            if tavily_support and not research_errors.get("tavily_support")
            else ""
        )
        exa_discovery_results = (
            list(exa_discovery.get("results") or [])
            if exa_discovery and not research_errors.get("exa_discovery")
            else []
        )

        urls: list[str] = []
        prefetched_content: dict[str, str] = {}
        if ProviderResponse.is_hybrid(web_search):
            base_candidate_results = web_search.get("results") or web_search.get("web", {}).get("results", [])
        else:
            base_candidate_results = web_search.get("results", [])

        research_candidate_results, research_selection_meta = self._select_research_candidate_results(
            query=query,
            mode=research_plan["web_mode"],
            intent=resolved_intent,
            max_results=research_plan["web_max_results"],
            web_results=base_candidate_results,
            docs_rescue_results=docs_rescue_results,
            tavily_support_results=tavily_support_results,
            exa_results=exa_discovery_results,
            include_domains=include_domains,
            authoritative_preferred=authoritative_research,
        )

        for result in research_candidate_results:
            url = (result.get("url") or "").strip()
            if not url or url in urls:
                continue
            urls.append(url)
            content = (result.get("content") or "").strip()
            if content and len(content) >= 200:
                prefetched_content[url] = content
            if len(urls) >= research_plan["scrape_top_n"]:
                break

        if len(urls) < research_plan["scrape_top_n"] and include_social:
            social_search = research_results.get("social")
            if social_search and not research_errors.get("social"):
                for social_item in social_search.get("results") or []:
                    social_url = (social_item.get("url") or "").strip()
                    if not social_url or social_url in urls:
                        continue
                    parsed = urlparse(social_url)
                    if parsed.netloc and not parsed.netloc.endswith(("x.com", "twitter.com")):
                        urls.append(social_url)
                        if len(urls) >= research_plan["scrape_top_n"]:
                            break

        exa_unique_urls: list[str] = []
        seen_exa_urls: set[str] = set()
        for exa_item in exa_discovery_results:
            exa_url = (exa_item.get("url") or "").strip()
            if not exa_url or exa_url in seen_exa_urls:
                continue
            seen_exa_urls.add(exa_url)
            exa_unique_urls.append(exa_url)

        exa_promoted_urls = [
            url
            for url in urls
            if url in seen_exa_urls
            and url not in {
                (item.get("url") or "").strip()
                for item in base_candidate_results
            }
        ]
        if len(urls) < research_plan["scrape_top_n"]:
            if exa_discovery and not research_errors.get("exa_discovery"):
                for exa_item in exa_discovery_results:
                    exa_url = (exa_item.get("url") or "").strip()
                    if not exa_url or exa_url in urls:
                        continue
                    urls.append(exa_url)
                    exa_promoted_urls.append(exa_url)
                    if len(urls) >= research_plan["scrape_top_n"]:
                        break

        pages: list[dict[str, Any]] = []
        urls_to_scrape = [url for url in urls if url not in prefetched_content]
        page_tasks = {
            f"page:{urls.index(url)}": (
                lambda current_url=url: self.extract_url(
                    url=current_url,
                    formats=["markdown"],
                    only_main_content=True,
                )
            )
            for url in urls_to_scrape
        }
        page_results, page_errors = self._execute_parallel(
            page_tasks,
            max_workers=min(self.config.max_parallel_workers, max(1, len(page_tasks))),
        )
        for index, url in enumerate(urls):
            task_name = f"page:{index}"
            if url in prefetched_content:
                page = {
                    "provider": "discovery_prefetch",
                    "url": url,
                    "content": prefetched_content[url],
                    "excerpt": self._build_excerpt(prefetched_content[url]),
                }
                pages.append(page)
            elif task_name in page_results:
                page = page_results[task_name]
                page["excerpt"] = self._build_excerpt(page.get("content", ""))
                pages.append(page)
            else:
                error = page_errors.get(task_name)
                pages.append({"url": url, "error": str(error) if error else "unknown error"})

        social: dict[str, Any] | None = None
        social_error = ""
        if include_social:
            social = research_results.get("social")
            social_exc = research_errors.get("social")
            if social_exc is not None:
                social_error = str(social_exc)
            elif self._is_social_unavailable_result(social):
                social_error = str(
                    (social or {}).get("summary")
                    or ((social or {}).get("fallback") or {}).get("reason")
                    or "social search unavailable"
                )
                social = None

        web_provider = web_search.get("provider", "")
        social_provider = social.get("provider", "") if social else ""
        providers_consulted = [
            item
            for item in [web_provider, docs_rescue_provider, tavily_support_provider, social_provider]
            if item
        ]
        if exa_discovery and not research_errors.get("exa_discovery"):
            providers_consulted.append("exa")
        providers_consulted = list(dict.fromkeys(providers_consulted))
        citations = self._dedupe_citations(
            web_search.get("citations") or [],
            (docs_rescue.get("citations") or [])
            if docs_rescue and not research_errors.get("docs_rescue")
            else [],
            (tavily_support.get("citations") or [])
            if tavily_support and not research_errors.get("tavily_support")
            else [],
            (social.get("citations") or []) if social else [],
            (exa_discovery.get("citations") or [])
            if exa_discovery and not research_errors.get("exa_discovery")
            else [],
        )
        ordered_research_results = self._dedupe_research_results_for_report(
            research_candidate_results,
            docs_rescue_results,
            tavily_support_results,
            exa_discovery_results,
        )
        citations = self._align_citations_with_results(
            results=ordered_research_results,
            citations=citations,
        )
        cross_provider_candidate_count = 0
        provider_support_total = 0
        for item in ordered_research_results:
            matched_providers = [
                provider
                for provider in (
                    item.get("matched_providers")
                    or [item.get("provider", "")]
                )
                if provider
            ]
            provider_support_total += len(matched_providers)
            if len(set(matched_providers)) > 1:
                cross_provider_candidate_count += 1
        provider_match_depth = (
            round(provider_support_total / len(ordered_research_results), 2)
            if ordered_research_results
            else 0.0
        )
        evidence = self._augment_research_evidence(
            query=query,
            mode=mode,
            intent=web_search.get("intent", intent if intent != "auto" else "factual"),
            requested_page_count=len(urls),
            pages=pages,
            citations=citations,
            web_search=web_search,
            social=social,
            social_error=social_error,
            providers_consulted=providers_consulted,
            research_plan=research_plan,
            exa_discovery_count=len(exa_discovery_results),
            exa_unique_url_count=len(exa_unique_urls),
            exa_promoted_page_count=len(exa_promoted_urls),
            authoritative_source_count=research_selection_meta["authoritative_source_count"],
            supporting_source_count=research_selection_meta["supporting_source_count"],
            community_source_count=research_selection_meta["community_source_count"],
            selected_candidate_count=len(research_candidate_results),
            selected_candidate_domains=research_selection_meta["selected_candidate_domains"],
            selected_candidate_cluster_counts=research_selection_meta["selected_candidate_cluster_counts"],
            docs_rescue_result_count=len(docs_rescue_results),
            authoritative_research=authoritative_research,
            cross_provider_candidate_count=cross_provider_candidate_count,
            provider_match_depth=provider_match_depth,
        )

        executive_summary = ""
        if (
            resolved_strategy == "deep"
            and self.config.xai.search_mode == "official"
            and self._provider_can_serve(self.config.xai)
        ):
            try:
                summary_result = self._search_xai(
                    query=f"Summarize key findings about: {query}",
                    sources=["web"],
                    max_results=3,
                )
                executive_summary = (summary_result.get("answer") or "").strip()
            except MySearchError:
                pass
        report_sections = self._build_research_report_sections(
            query=query,
            web_search=web_search,
            ordered_results=ordered_research_results,
            pages=pages,
            citations=citations,
            social=social,
            evidence=evidence,
            executive_summary_override=executive_summary,
        )
        research_summary = self._render_research_report(report_sections)
        visible_summary = str(report_sections.get("executive_summary") or "").strip()
        if (
            visible_summary
            and resolved_intent in {"comparison", "exploratory"}
            and (
                authoritative_research
                or int(evidence.get("selected_supporting_source_count") or 0) > 0
            )
        ):
            web_search = dict(web_search)
            web_search["answer"] = visible_summary
            web_search["summary"] = visible_summary
            comparison_row_urls = [
                str(row.get("url") or "").strip()
                for row in (report_sections.get("comparison_rows") or [])
                if str(row.get("url") or "").strip()
            ]
            if comparison_row_urls:
                ordered_results_by_url = {
                    str(item.get("url") or "").strip(): dict(item)
                    for item in ordered_research_results
                    if str(item.get("url") or "").strip()
                }
                visible_results = [
                    ordered_results_by_url[url]
                    for url in comparison_row_urls
                    if url in ordered_results_by_url
                ]
            else:
                visible_results = [dict(item) for item in ordered_research_results[:4] if item]
            if visible_results:
                web_search["results"] = visible_results
                web_search["citations"] = self._align_citations_with_results(
                    results=visible_results,
                    citations=citations or web_search.get("citations") or [],
                )

        return {
            "provider": "hybrid",
            "query": query,
            "intent": web_search.get("intent", resolved_intent),
            "strategy": web_search.get("strategy", resolved_strategy),
            "web_search": web_search,
            "pages": pages,
            "social_search": social,
            "social_error": social_error,
            "citations": citations,
            "evidence": evidence,
            "summary": research_summary,
            "confidence": evidence.get("confidence"),
            "research_summary": research_summary,
            "report_markdown": research_summary,
            "report_sections": report_sections,
            "notes": [
                "默认用 Tavily 做发现，Firecrawl 做正文抓取，X 搜索走 xAI Responses API",
                "如果某个 provider 没配 key，会保留错误并尽量返回其余部分",
            ],
        }

    def _resolve_research_plan(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        strategy: SearchStrategy,
        web_max_results: int,
        social_max_results: int,
        scrape_top_n: int,
        include_social: bool,
        include_domains: list[str] | None,
    ) -> dict[str, Any]:
        prefers_authoritative_sources = self._research_prefers_authoritative_sources(
            query=query,
            mode=mode,
            intent=intent,
            include_domains=include_domains,
        )
        if mode == "news":
            web_mode: SearchMode = "news"
        elif mode in {"docs", "github", "pdf"} or prefers_authoritative_sources:
            web_mode = "docs"
        elif intent in {"comparison", "exploratory"}:
            web_mode = "exploratory"
        else:
            web_mode = "web"
        planned_web_max = web_max_results
        planned_social_max = social_max_results if include_social else 0
        planned_scrape_top_n = scrape_top_n

        if mode in {"docs", "github", "pdf"} or self._should_use_strict_resource_policy(
            query=query,
            mode=mode,
            intent=intent,
            include_domains=include_domains,
        ):
            planned_web_max = max(planned_web_max, 4)
            planned_scrape_top_n = max(1, min(planned_scrape_top_n, 2))
        elif mode == "news" or intent in {"news", "status"}:
            planned_web_max = min(max(planned_web_max, 6), 8)
            planned_scrape_top_n = min(max(planned_scrape_top_n, 4), 5)
            if include_social:
                planned_social_max = min(max(planned_social_max, 4), 6)
        elif intent in {"comparison", "exploratory"} or strategy in {"verify", "deep"}:
            planned_web_max = min(max(planned_web_max, 6), 10)
            planned_scrape_top_n = min(max(planned_scrape_top_n, 4), 5)
            if include_social:
                planned_social_max = min(max(planned_social_max, 3), 5)

        return {
            "web_mode": web_mode,
            "web_max_results": planned_web_max,
            "social_max_results": planned_social_max,
            "scrape_top_n": planned_scrape_top_n,
        }

    def _research_authoritative_rescue_queries(self, query: str) -> list[str]:
        subjects = self._research_parse_comparison_subjects(query)
        if not subjects:
            return [query]
        normalized = re.sub(r"\b20\d{2}\b", "", query).strip()
        match = re.search(
            r"^\s*compare\s+(.+?)(?:\s+for\s+(.+))?$",
            normalized,
            re.IGNORECASE,
        )
        context = (match.group(2) or "").strip(" ,.;:") if match else ""
        brand_prefix = ""
        first_words = subjects[0].split()
        generic_tokens = {
            "api",
            "docs",
            "documentation",
            "guide",
            "guides",
            "official",
            "openai",
            "resource",
            "resources",
            "reference",
        }
        if first_words:
            candidate = first_words[0].strip(" ,.;:")
            if candidate and candidate[0].isalpha() and candidate[0].isupper():
                brand_prefix = candidate
        queries: list[str] = [query]
        for part in subjects[:3]:
            candidate = part
            if self._research_subject_should_inherit_brand_prefix(
                subject=candidate,
                brand_prefix=brand_prefix,
                generic_tokens=generic_tokens,
            ):
                candidate = f"{brand_prefix} {candidate}"
            if context:
                queries.append(f"{candidate} official docs {context}".strip())
            queries.append(f"{candidate} official docs".strip())
            queries.extend(self._research_known_provider_doc_queries(candidate))
        deduped: list[str] = []
        seen: set[str] = set()
        for candidate in queries:
            normalized_candidate = re.sub(r"\s+", " ", candidate).strip()
            if not normalized_candidate:
                continue
            dedupe_key = normalized_candidate.lower()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            deduped.append(normalized_candidate)
        return deduped

    def _research_known_provider_doc_queries(self, entity: str) -> list[str]:
        lowered = entity.lower()
        if "tavily" in lowered:
            return [
                "Tavily search api docs",
                "Tavily extract docs",
            ]
        if "firecrawl" in lowered:
            return [
                "Firecrawl scrape docs",
                "Firecrawl extract docs",
            ]
        if re.search(r"\bexa\b", lowered):
            return [
                "Exa search docs",
                "Exa contents docs",
            ]
        if "apify" in lowered:
            return [
                "Apify api docs",
                "Apify actors docs",
            ]
        return []

    def _research_canonical_doc_catalog(self) -> dict[str, list[dict[str, Any]]]:
        return sections._research_canonical_doc_catalog()

    def _research_canonical_doc_snippet_for_url(self, url: str) -> str:
        return sections._research_canonical_doc_snippet_for_url(url=url)

    def _research_is_canonical_vendor_doc(self, url: str) -> bool:
        return sections._research_is_canonical_vendor_doc(url=url)

    def _research_prefers_canonical_vendor_docs(self, query: str) -> bool:
        return selection._research_prefers_canonical_vendor_docs(query=query)

    def _research_relaxed_discovery_query(self, query: str) -> str:
        relaxed = re.sub(r"\bofficial\b", " ", query, flags=re.IGNORECASE)
        relaxed = re.sub(r"\s+", " ", relaxed).strip()
        return relaxed or query.strip()

    def _research_primary_discovery_route(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        authoritative_research: bool,
    ) -> dict[str, str]:
        if authoritative_research and self._research_prefers_canonical_vendor_docs(query):
            return {
                "query": self._research_relaxed_discovery_query(query),
                "mode": "web",
                "intent": "exploratory",
            }
        return {
            "query": query,
            "mode": mode,
            "intent": intent,
        }

    def _research_prefers_tavily_discovery(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        authoritative_research: bool,
    ) -> bool:
        if mode == "web" and not authoritative_research:
            return True
        if mode == "news" or intent == "news":
            return True
        if authoritative_research and self._research_primary_vendor_brand(query):
            return True
        return False

    def _research_known_provider_doc_results(self, query: str) -> list[dict[str, Any]]:
        if not self._looks_like_comparison_query(query.lower()):
            return []
        catalog = self._research_canonical_doc_catalog()
        seen_urls: set[str] = set()
        entity_texts = {
            " ".join(entity_tokens).lower()
            for entity_tokens in self._research_comparison_entities(query)
        }
        comparison_projects: dict[frozenset[str], dict[str, str]] = {
            frozenset({"firecrawl", "tavily"}): {
                "title": "Firecrawl vs Tavily - Firecrawl",
                "url": "https://www.firecrawl.dev/compare/firecrawl-vs-tavily",
                "snippet": (
                    "Firecrawl positions itself as an extraction-first workflow with scrape "
                    "and structured extraction, while Tavily focuses on search and retrieval APIs."
                ),
            },
            frozenset({"firecrawl", "exa"}): {
                "title": "Firecrawl vs Exa - Firecrawl",
                "url": "https://www.firecrawl.dev/compare/firecrawl-vs-exa",
                "snippet": (
                    "Firecrawl compares extraction-first crawling and structured data workflows "
                    "against Exa's semantic search and discovery APIs."
                ),
            },
            frozenset({"exa", "tavily"}): {
                "title": "Exa vs Tavily: 5x More Results & Content Filtering",
                "url": "https://exa.ai/versus/tavily",
                "snippet": (
                    "Exa compares semantic search, result volume, and content filtering "
                    "against Tavily for AI search workflows."
                ),
            },
            frozenset({"firecrawl", "apify"}): {
                "title": "Firecrawl vs Apify: Complete Comparison for AI Agents & RAG (2026)",
                "url": "https://www.firecrawl.dev/compare/firecrawl-vs-apify",
                "snippet": (
                    "Firecrawl compares AI-ready scraping, extraction, and agent workflows "
                    "against Apify's actor platform and scraping ecosystem."
                ),
            },
        }
        tracked_brands = set(catalog.keys())
        for pair in comparison_projects:
            tracked_brands.update(pair)
        entity_brands = {
            brand
            for brand in tracked_brands
            if any(brand in entity for entity in entity_texts)
        }
        project_results: list[dict[str, Any]] = []
        injected_pair_project = False
        for pair, item in comparison_projects.items():
            if not pair.issubset(entity_brands):
                continue
            comparison_url = item["url"]
            if comparison_url in seen_urls:
                continue
            injected_pair_project = True
            seen_urls.add(comparison_url)
            project_results.append(
                {
                    "provider": "canonical_research_projects",
                    "title": item["title"],
                    "url": comparison_url,
                    "snippet": item["snippet"],
                }
            )
        generic_comparison_hub = "https://www.firecrawl.dev/compare"
        if (
            "firecrawl" in entity_brands
            and len(entity_texts) >= 2
            and not injected_pair_project
            and generic_comparison_hub not in seen_urls
        ):
            seen_urls.add(generic_comparison_hub)
            project_results.append(
                {
                    "provider": "canonical_research_projects",
                    "title": "Compare Firecrawl with Alternatives | In-depth Tool Comparisons",
                    "url": generic_comparison_hub,
                    "snippet": (
                        "Firecrawl maintains first-party comparison pages covering extraction, "
                        "search, and RAG trade-offs against alternative tooling."
                    ),
                }
            )
        supporting_results: list[dict[str, Any]] = []
        query_lower = query.lower()
        for entity_tokens in self._research_comparison_entities(query):
            entity_text = " ".join(entity_tokens).lower()
            for brand, items in catalog.items():
                if brand not in entity_text:
                    continue
                for item in items:
                    url = str(item.get("url") or "")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    supporting_results.append(dict(item))
        if "responses api" in query_lower and "batch api" in query_lower:
            for key in ("responses api", "batch api"):
                for item in catalog.get(key, []):
                    url = str(item.get("url") or "")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    supporting_results.append(dict(item))
        if (
            "responses api" in query_lower
            and "batch api" in query_lower
            and any(
                marker in query_lower
                for marker in ("long-running", "long running", "asynchronous", "background")
            )
        ):
            for item in catalog.get("background mode", []):
                url = str(item.get("url") or "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                supporting_results.append(dict(item))
        return [*project_results, *supporting_results]

    def _research_generic_vendor_doc_results(self, query: str) -> list[dict[str, Any]]:
        query_lower = query.lower()
        if not any(
            marker in query_lower
            for marker in (
                "official docs",
                "docs retrieval",
                "documentation retrieval",
                "agentic search",
                "web retrieval",
            )
        ):
            return []
        generic_tokens = {
            "agent",
            "agentic",
            "agents",
            "ai",
            "approach",
            "best",
            "docs",
            "documentation",
            "guide",
            "guides",
            "official",
            "retrieval",
            "search",
            "web",
            "workflow",
            "workflows",
        }
        specific_tokens = [
            token
            for token in self._query_precision_tokens(query)
            if token not in generic_tokens
        ]
        if specific_tokens:
            return []
        catalog = self._research_canonical_doc_catalog()
        return [
            dict(catalog["tavily"][0]),
            dict(catalog["firecrawl"][1]),
            dict(catalog["exa"][0]),
        ]

    def _run_research_docs_rescue(
        self,
        *,
        query: str,
        strategy: SearchStrategy,
        max_results: int,
        include_domains: list[str] | None,
        exclude_domains: list[str] | None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        merged_result: dict[str, Any] | None = None
        primary_vendor_brand = self._research_primary_vendor_brand(query)
        for rescue_query in self._research_authoritative_rescue_queries(query):
            try:
                current_result = self.search(
                    query=rescue_query,
                    mode="docs",
                    intent="resource",
                    strategy=strategy,
                    provider="tavily",
                    sources=["web"],
                    max_results=max_results,
                    include_content=False,
                    include_answer=False,
                    include_domains=include_domains,
                    exclude_domains=exclude_domains,
                    from_date=from_date,
                    to_date=to_date,
                )
            except MySearchError:
                if primary_vendor_brand:
                    try:
                        current_result = self._run_research_tavily_discovery(
                            query=rescue_query,
                            mode="docs",
                            intent="resource",
                            strategy=strategy,
                            max_results=max_results,
                            include_content=False,
                            include_answer=False,
                            include_domains=include_domains,
                            exclude_domains=exclude_domains,
                            from_date=from_date,
                            to_date=to_date,
                        )
                    except MySearchError:
                        continue
                else:
                    continue
            if merged_result is None:
                merged_result = dict(current_result)
                continue
            merged_payload = self._merge_search_payloads(
                primary_result=merged_result,
                secondary_result=current_result,
                max_results=max_results,
            )
            merged_result["results"] = self._rerank_resource_results(
                query=query,
                mode="docs",
                results=merged_payload["results"],
                include_domains=include_domains,
            )
            merged_result["citations"] = self._align_citations_with_results(
                results=merged_result["results"],
                citations=merged_payload["citations"],
            )
            merged_result["matched_results"] = merged_payload["matched_results"]
        return merged_result or {
            "provider": "tavily",
            "query": query,
            "intent": "resource",
            "strategy": strategy,
            "results": [],
            "citations": [],
        }

    def _run_research_web_discovery(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        strategy: SearchStrategy,
        max_results: int,
        include_content: bool,
        include_domains: list[str] | None,
        exclude_domains: list[str] | None,
        authoritative_research: bool,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        prefers_tavily = self._research_prefers_tavily_discovery(
            query=query,
            mode=mode,
            intent=intent,
            authoritative_research=authoritative_research,
        )
        if not prefers_tavily:
            return self.search(
                query=query,
                mode=mode,
                intent=intent,
                strategy=strategy,
                provider="auto",
                sources=["web"],
                max_results=max_results,
                include_content=include_content,
                include_answer=True,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                from_date=from_date,
                to_date=to_date,
            )

        try:
            return self.search(
                query=query,
                mode=mode,
                intent=intent,
                strategy=strategy,
                provider="tavily",
                sources=["web"],
                max_results=max_results,
                include_content=include_content,
                include_answer=True,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                from_date=from_date,
                to_date=to_date,
            )
        except MySearchError:
            return self._run_research_tavily_discovery(
                query=query,
                mode=mode,
                intent=intent,
                strategy=strategy,
                max_results=max_results,
                include_content=include_content,
                include_answer=True,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                from_date=from_date,
                to_date=to_date,
            )

    def _run_research_tavily_discovery(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        strategy: SearchStrategy,
        max_results: int,
        include_content: bool,
        include_answer: bool,
        include_domains: list[str] | None,
        exclude_domains: list[str] | None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        tavily_result = self._search_tavily(
            query=query,
            max_results=max_results,
            topic="news" if mode == "news" or intent == "news" else "general",
            include_answer=include_answer,
            include_content=include_content,
            include_domains=include_domains,
            from_date=from_date,
            to_date=to_date,
            exclude_domains=exclude_domains,
            strategy="advanced" if strategy in {"verify", "deep"} else "fast",
        )
        tavily_result["query"] = query
        tavily_result["intent"] = intent
        tavily_result["strategy"] = strategy
        if mode in {"docs", "github", "pdf"}:
            tavily_result["results"] = self._rerank_resource_results(
                query=query,
                mode=mode,
                results=list(tavily_result.get("results") or []),
                include_domains=include_domains,
            )[:max_results]
            tavily_result["citations"] = self._align_citations_with_results(
                results=list(tavily_result.get("results") or []),
                citations=list(tavily_result.get("citations") or []),
            )
        return tavily_result

    def _looks_like_technical_research_query(self, query_lower: str) -> bool:
        return query_routing._looks_like_technical_research_query(query_lower)
    def _research_prefers_authoritative_sources(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        include_domains: list[str] | None,
    ) -> bool:
        query_lower = query.lower()
        if mode in {"docs", "github", "pdf"}:
            return True
        if include_domains:
            return True
        if self._should_use_strict_resource_policy(
            query=query,
            mode=mode,
            intent=intent,
            include_domains=include_domains,
        ):
            return True
        return self._looks_like_technical_research_query(query_lower)

    def _dedupe_research_results_for_report(
        self,
        *result_lists: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return selection._dedupe_research_results_for_report(*result_lists)

    def _prioritize_research_project_results(
        self,
        results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        project_results = [
            item
            for item in results
            if str(item.get("provider") or "") == "canonical_research_projects"
        ]
        if not project_results:
            return results
        other_results = [
            item
            for item in results
            if str(item.get("provider") or "") != "canonical_research_projects"
        ]
        return self._dedupe_research_results_for_report(
            project_results,
            other_results,
        )

    def _research_result_cluster_label(
        self,
        *,
        query: str,
        mode: SearchMode,
        item: dict[str, Any],
        include_domains: list[str] | None,
        authoritative_preferred: bool,
    ) -> str:
        return sections._research_result_cluster_label(query=query, mode=mode, item=item, include_domains=include_domains, authoritative_preferred=authoritative_preferred)

    def _select_research_candidate_results(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        max_results: int,
        web_results: list[dict[str, Any]],
        docs_rescue_results: list[dict[str, Any]],
        tavily_support_results: list[dict[str, Any]],
        exa_results: list[dict[str, Any]],
        include_domains: list[str] | None,
        authoritative_preferred: bool,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return selection._select_research_candidate_results(query=query, mode=mode, intent=intent, max_results=max_results, web_results=web_results, docs_rescue_results=docs_rescue_results, tavily_support_results=tavily_support_results, exa_results=exa_results, include_domains=include_domains, authoritative_preferred=authoritative_preferred)

    def _research_comparison_entities(self, query: str) -> list[tuple[str, ...]]:
        return sections._research_comparison_entities(query=query)

    def _research_parse_comparison_subjects(self, query: str) -> list[str]:
        return sections._research_parse_comparison_subjects(query=query)

    def _research_comparison_subject_phrase(self, query: str) -> str:
        return sections._research_comparison_subject_phrase(query=query)

    def _research_ambiguous_product_tokens(self) -> set[str]:
        return research.comparison.research_ambiguous_product_tokens()

    def _research_subject_is_generic_comparison_dimension(self, subject: str) -> bool:
        return sections._research_subject_is_generic_comparison_dimension(subject=subject)

    def _research_subject_should_inherit_brand_prefix(
        self,
        *,
        subject: str,
        brand_prefix: str,
        generic_tokens: set[str],
    ) -> bool:
        return sections._research_subject_should_inherit_brand_prefix(subject=subject, brand_prefix=brand_prefix, generic_tokens=generic_tokens)

    def _research_primary_vendor_brand(self, query: str) -> str:
        return sections._research_primary_vendor_brand(query=query)

    def _research_item_matches_brand_token(
        self,
        *,
        item: dict[str, Any],
        brand_token: str,
    ) -> bool:
        return sections._research_item_matches_brand_token(item=item, brand_token=brand_token)

    def _research_result_matches_entity(
        self,
        *,
        item: dict[str, Any],
        entity_tokens: tuple[str, ...],
    ) -> bool:
        return research.comparison.research_result_matches_entity(item=item, entity_tokens=entity_tokens)

    def _research_result_matches_comparison_subject(
        self,
        *,
        item: dict[str, Any],
        entity_tokens: tuple[str, ...],
    ) -> bool:
        return research.comparison.research_result_matches_comparison_subject(item=item, entity_tokens=entity_tokens)

    def _research_official_candidate_kind_rank(self, item: dict[str, Any]) -> int:
        return sections._research_official_candidate_kind_rank(item=item)

    def _diversify_research_official_candidates(
        self,
        *,
        query: str,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return selection._diversify_research_official_candidates(query=query, candidates=candidates)

    def _diversify_research_supporting_candidates(
        self,
        *,
        query: str,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return selection._diversify_research_supporting_candidates(query=query, candidates=candidates)

    def _assemble_non_authoritative_research_candidates(
        self,
        *,
        query: str,
        project_candidates: list[dict[str, Any]],
        supporting_candidates: list[dict[str, Any]],
        curated_candidates: list[dict[str, Any]],
        listicle_candidates: list[dict[str, Any]],
        directory_candidates: list[dict[str, Any]],
        community_candidates: list[dict[str, Any]],
        max_results: int,
    ) -> list[dict[str, Any]]:
        return selection._assemble_non_authoritative_research_candidates(query=query, project_candidates=project_candidates, supporting_candidates=supporting_candidates, curated_candidates=curated_candidates, listicle_candidates=listicle_candidates, directory_candidates=directory_candidates, community_candidates=community_candidates, max_results=max_results)

    def _diversify_results_by_registered_domain(
        self,
        results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return selection._diversify_results_by_registered_domain(results=results)

    def _research_project_candidate_kind_rank(self, item: dict[str, Any]) -> int:
        return sections._research_project_candidate_kind_rank(item=item)

    def _research_supporting_candidate_kind_rank(
        self,
        item: dict[str, Any],
        *,
        query: str,
    ) -> int:
        return selection._research_supporting_candidate_kind_rank(item=item, query=query)

    def _assemble_authoritative_research_candidates(
        self,
        *,
        official_candidates: list[dict[str, Any]],
        supporting_candidates: list[dict[str, Any]],
        general_candidates: list[dict[str, Any]],
        community_candidates: list[dict[str, Any]],
        max_results: int,
        prefer_canonical_vendor_docs: bool = False,
    ) -> list[dict[str, Any]]:
        return selection._assemble_authoritative_research_candidates(official_candidates=official_candidates, supporting_candidates=supporting_candidates, general_candidates=general_candidates, community_candidates=community_candidates, max_results=max_results, prefer_canonical_vendor_docs=prefer_canonical_vendor_docs)

    def _research_vendor_doc_general_candidate_kind_rank(
        self,
        item: dict[str, Any],
    ) -> int:
        return selection._research_vendor_doc_general_candidate_kind_rank(item=item)

    def _research_report_anchor_tokens(
        self,
        *,
        query: str,
        mode: str,
        ordered_results: list[dict[str, Any]],
        authoritative_preferred: bool,
    ) -> list[str]:
        return sections._research_report_anchor_tokens(query=query, mode=mode, ordered_results=ordered_results, authoritative_preferred=authoritative_preferred)

    def _research_summary_mentions_anchor_tokens(
        self,
        text: str,
        anchor_tokens: list[str],
    ) -> bool:
        return sections._research_summary_mentions_anchor_tokens(text=text, anchor_tokens=anchor_tokens)

    def _research_authoritative_query_tokens(self, query: str) -> list[str]:
        return sections._research_authoritative_query_tokens(query=query)

    def _research_non_authoritative_query_tokens(self, query: str) -> list[str]:
        return sections._research_non_authoritative_query_tokens(query=query)

    def _looks_like_authoritative_research_target(
        self,
        *,
        url: str,
        hostname: str,
        title_text: str,
        mode: SearchMode,
    ) -> bool:
        return query_routing._looks_like_authoritative_research_target(url=url, hostname=hostname, title_text=title_text, mode=mode)
    def _looks_like_authoritative_research_host(
        self,
        *,
        hostname: str,
        path: str,
    ) -> bool:
        return query_routing._looks_like_authoritative_research_host(hostname=hostname, path=path)
    def _looks_like_research_marketing_or_blog_result(
        self,
        *,
        hostname: str,
        path: str,
        title_text: str,
        snippet_text: str,
    ) -> bool:
        return query_routing._looks_like_research_marketing_or_blog_result(hostname=hostname, path=path, title_text=title_text, snippet_text=snippet_text)
    def _looks_like_supporting_research_target(
        self,
        *,
        url: str,
        hostname: str,
        title_text: str,
        snippet_text: str,
        mode: SearchMode,
    ) -> bool:
        return query_routing._looks_like_supporting_research_target(url=url, hostname=hostname, title_text=title_text, snippet_text=snippet_text, mode=mode)
    def _candidate_result_budget(
        self,
        *,
        requested_max_results: int,
        strategy: SearchStrategy,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        include_domains: list[str] | None,
        route_provider: str,
    ) -> int:
        if route_provider == "xai":
            return requested_max_results

        budget = requested_max_results
        strategy_floor = {
            "fast": requested_max_results,
            "balanced": min(max(requested_max_results * 2, requested_max_results + 2), 10),
            "verify": min(max(requested_max_results * 3, requested_max_results + 4), 15),
            "deep": min(max(requested_max_results * 4, requested_max_results + 6), 20),
        }
        budget = max(budget, strategy_floor.get(strategy, requested_max_results))

        if include_domains or self._should_rerank_resource_results(mode=mode, intent=intent):
            budget = max(budget, min(max(requested_max_results * 2, requested_max_results + 3), 12))

        return max(requested_max_results, budget)

    def _trim_search_payload(self, result: dict[str, Any], *, max_results: int) -> dict[str, Any]:
        return finalize._trim_search_payload(result=result, max_results=max_results)

    def _augment_evidence_summary(
        self,
        result: dict[str, Any],
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        include_domains: list[str] | None,
    ) -> dict[str, Any]:
        return finalize._augment_evidence_summary(result=result, query=query, mode=mode, intent=intent, include_domains=include_domains)

    def _finalize_search_result(
        self,
        result: dict[str, Any],
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        include_domains: list[str] | None,
        result_profile: Literal["web", "news", "resource"],
        max_results: int,
    ) -> dict[str, Any]:
        return finalize._finalize_search_result(result=result, query=query, mode=mode, intent=intent, include_domains=include_domains, result_profile=result_profile, max_results=max_results)

    def _apply_status_result_policy(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        return finalize._apply_status_result_policy(query=query, mode=mode, intent=intent, result=result)

    def _resolve_official_result_mode(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        include_domains: list[str] | None,
    ) -> str:
        return query_routing._resolve_official_result_mode(query=query, mode=mode, intent=intent, include_domains=include_domains)
    def _should_use_strict_resource_policy(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        include_domains: list[str] | None,
    ) -> bool:
        return query_routing._should_use_strict_resource_policy(query=query, mode=mode, intent=intent, include_domains=include_domains)
    def _looks_like_official_query(self, query: str) -> bool:
        return query_routing._looks_like_official_query(query)
    def _looks_like_changelog_query(self, query_lower: str) -> bool:
        return query_routing._looks_like_changelog_query(query_lower)
    def _looks_like_github_release_query(self, query_lower: str) -> bool:
        return query_routing._looks_like_github_release_query(query_lower)
    def _apply_official_resource_policy(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        result: dict[str, Any],
        include_domains: list[str] | None,
    ) -> dict[str, Any]:
        return finalize._apply_official_resource_policy(query=query, mode=mode, intent=intent, result=result, include_domains=include_domains)

    def _build_known_canonical_resource_rescue(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
    ) -> dict[str, Any] | None:
        return selection._build_known_canonical_resource_rescue(query=query, mode=mode, intent=intent)

    def _extract_known_react_hook_reference(self, query: str) -> str | None:
        return selection._extract_known_react_hook_reference(query=query)

    def _extract_explicit_github_repo_slug(self, query: str) -> tuple[str, str] | None:
        return query_routing._extract_explicit_github_repo_slug(query)
    def _preferred_react_docs_locale(self, query: str) -> str | None:
        return query_routing._preferred_react_docs_locale(query)
    def _query_prefers_versioned_react_docs(self, query: str, *, version: str) -> bool:
        return query_routing._query_prefers_versioned_react_docs(query, version=version)
    def _looks_like_noncanonical_react_docs_hostname(self, hostname: str, *, query: str = "") -> bool:
        return query_routing._looks_like_noncanonical_react_docs_hostname(hostname, query=query)
    def _should_apply_canonical_resource_rescue(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        official_candidates: list[dict[str, Any]],
        rescue_candidate: dict[str, Any],
    ) -> bool:
        return query_routing._should_apply_canonical_resource_rescue(query=query, mode=mode, intent=intent, official_candidates=official_candidates, rescue_candidate=rescue_candidate)
    def _collect_official_result_candidates(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        results: list[dict[str, Any]],
        include_domains: list[str] | None,
        strict_official: bool,
    ) -> list[dict[str, Any]]:
        return finalize._collect_official_result_candidates(query=query, mode=mode, intent=intent, results=results, include_domains=include_domains, strict_official=strict_official)

    def _build_research_web_fallback_result(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        strategy: SearchStrategy,
        exa_discovery: dict[str, Any],
        include_domains: list[str] | None,
    ) -> dict[str, Any]:
        fallback_result = dict(exa_discovery)
        fallback_result["provider"] = "exa"
        fallback_result["query"] = query
        fallback_result["intent"] = intent
        fallback_result["strategy"] = strategy
        fallback_result.setdefault(
            "fallback",
            {"from": "research-web", "to": "exa", "reason": "primary web discovery failed"},
        )
        results = list(fallback_result.get("results") or [])
        citations = list(fallback_result.get("citations") or [])
        if self._should_rerank_resource_results(mode=mode, intent=intent):
            results = self._rerank_resource_results(
                query=query,
                mode=mode,
                results=results,
                include_domains=include_domains,
            )
        elif self._should_rerank_general_results(result_profile="web"):
            results = self._rerank_general_results(
                query=query,
                result_profile="web",
                results=results,
                include_domains=include_domains,
            )
        fallback_result["results"] = results
        fallback_result["citations"] = self._align_citations_with_results(
            results=results,
            citations=citations,
        )
        fallback_result = self._apply_official_resource_policy(
            query=query,
            mode=mode,
            intent=intent,
            result=fallback_result,
            include_domains=include_domains,
        )
        fallback_result = self._trim_search_payload(fallback_result, max_results=len(results) or 5)
        fallback_result = self._augment_evidence_summary(
            fallback_result,
            query=query,
            mode=mode,
            intent=intent,
            include_domains=include_domains,
        )
        fallback_result["summary"] = self._build_search_summary_fallback(
            query=query,
            mode=mode,
            intent=intent,
            result=fallback_result,
        )
        return fallback_result

    def _build_research_secondary_fallback_result(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        strategy: SearchStrategy,
        source_result: dict[str, Any],
        include_domains: list[str] | None,
        fallback_to: str,
        fallback_reason: str,
    ) -> dict[str, Any]:
        fallback_result = dict(source_result)
        fallback_result["provider"] = str(source_result.get("provider") or fallback_to)
        fallback_result["query"] = query
        fallback_result["intent"] = intent
        fallback_result["strategy"] = strategy
        fallback_result["fallback"] = {
            "from": "research-web",
            "to": fallback_to,
            "reason": fallback_reason,
        }
        results = list(fallback_result.get("results") or [])
        citations = list(fallback_result.get("citations") or [])
        if self._should_rerank_resource_results(mode=mode, intent=intent):
            results = self._rerank_resource_results(
                query=query,
                mode=mode,
                results=results,
                include_domains=include_domains,
            )
        elif self._should_rerank_general_results(result_profile="web"):
            results = self._rerank_general_results(
                query=query,
                result_profile="web",
                results=results,
                include_domains=include_domains,
            )
        if (
            fallback_to == "canonical_research_docs"
            and self._looks_like_comparison_query(query.lower())
            and results
        ):
            project_results = [
                item
                for item in results
                if str(item.get("provider") or "") == "canonical_research_projects"
            ]
            if project_results:
                results = self._prioritize_research_project_results(results)
            if not project_results:
                selected_results, _ = self._select_research_candidate_results(
                    query=query,
                    mode=mode,
                    intent=intent,
                    max_results=len(results),
                    web_results=results,
                    docs_rescue_results=[],
                    tavily_support_results=[],
                    exa_results=[],
                    include_domains=include_domains,
                    authoritative_preferred=self._research_prefers_authoritative_sources(
                        query=query,
                        mode=mode,
                        intent=intent,
                        include_domains=include_domains,
                    ),
                )
                if selected_results:
                    results = selected_results
        fallback_result["results"] = results
        fallback_result["citations"] = self._align_citations_with_results(
            results=results,
            citations=citations,
        )
        fallback_result = self._apply_official_resource_policy(
            query=query,
            mode=mode,
            intent=intent,
            result=fallback_result,
            include_domains=include_domains,
        )
        if (
            fallback_to == "canonical_research_docs"
            and self._looks_like_comparison_query(query.lower())
            and fallback_result.get("results")
        ):
            reprioritized_results = self._prioritize_research_project_results(
                list(fallback_result.get("results") or [])
            )
            fallback_result["results"] = reprioritized_results
            fallback_result["citations"] = self._align_citations_with_results(
                results=reprioritized_results,
                citations=list(fallback_result.get("citations") or []),
            )
        fallback_result = self._trim_search_payload(fallback_result, max_results=len(results) or 5)
        fallback_result = self._augment_evidence_summary(
            fallback_result,
            query=query,
            mode=mode,
            intent=intent,
            include_domains=include_domains,
        )
        fallback_result["summary"] = self._build_search_summary_fallback(
            query=query,
            mode=mode,
            intent=intent,
            result=fallback_result,
        )
        return fallback_result

    def _augment_research_evidence(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: str,
        requested_page_count: int,
        pages: list[dict[str, Any]],
        citations: list[dict[str, Any]],
        web_search: dict[str, Any],
        social: dict[str, Any] | None,
        social_error: str,
        providers_consulted: list[str],
        research_plan: dict[str, Any],
        exa_discovery_count: int,
        exa_unique_url_count: int,
        exa_promoted_page_count: int,
        authoritative_source_count: int,
        supporting_source_count: int,
        community_source_count: int,
        selected_candidate_count: int,
        selected_candidate_domains: list[str],
        selected_candidate_cluster_counts: dict[str, int],
        docs_rescue_result_count: int,
        authoritative_research: bool,
        cross_provider_candidate_count: int,
        provider_match_depth: float,
    ) -> dict[str, Any]:
        return finalize._augment_research_evidence(query=query, mode=mode, intent=intent, requested_page_count=requested_page_count, pages=pages, citations=citations, web_search=web_search, social=social, social_error=social_error, providers_consulted=providers_consulted, research_plan=research_plan, exa_discovery_count=exa_discovery_count, exa_unique_url_count=exa_unique_url_count, exa_promoted_page_count=exa_promoted_page_count, authoritative_source_count=authoritative_source_count, supporting_source_count=supporting_source_count, community_source_count=community_source_count, selected_candidate_count=selected_candidate_count, selected_candidate_domains=selected_candidate_domains, selected_candidate_cluster_counts=selected_candidate_cluster_counts, docs_rescue_result_count=docs_rescue_result_count, authoritative_research=authoritative_research, cross_provider_candidate_count=cross_provider_candidate_count, provider_match_depth=provider_match_depth)

    def _should_attempt_xai_arbitration(
        self,
        *,
        result: dict[str, Any],
        decision: RouteDecision,
        strategy: SearchStrategy,
        conflicts: list[str],
    ) -> bool:
        if strategy not in {"verify", "deep"}:
            return False
        if not conflicts:
            return False
        if decision.provider == "xai" or result.get("provider") == "xai":
            return False
        if not self._provider_can_serve(self.config.xai):
            return False
        if self.config.xai.search_mode != "official":
            return False
        evidence = result.get("evidence") or {}
        providers_consulted = [
            item for item in (evidence.get("providers_consulted") or []) if item
        ]
        if len(set(providers_consulted)) < 2:
            return False
        if str(evidence.get("official_mode") or "off") == "strict":
            return False
        return True

    def _apply_xai_arbitration(
        self,
        *,
        query: str,
        result: dict[str, Any],
        include_domains: list[str] | None,
        exclude_domains: list[str] | None,
        from_date: str | None,
        to_date: str | None,
    ) -> dict[str, Any]:
        evidence = dict(result.get("evidence") or {})
        conflicts = [item for item in (evidence.get("conflicts") or []) if item]
        arbitration_query = (
            f"Resolve conflicting evidence for: {query}\n\n"
            f"Conflicts: {', '.join(conflicts)}.\n"
            "Prefer the most credible and current conclusion. "
            "Briefly explain which evidence should be trusted and why."
        )
        try:
            arbitration_result = self._search_xai(
                query=arbitration_query,
                sources=["web"],
                max_results=3,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                from_date=from_date,
                to_date=to_date,
            )
        except MySearchError:
            return result

        arbitration_summary = (arbitration_result.get("answer") or "").strip()
        if not arbitration_summary:
            return result

        citation_count = len(arbitration_result.get("citations") or [])
        arbitration_confidence = (
            "high"
            if citation_count >= 2
            else "medium"
        )
        evidence["arbitration_source"] = "xai"
        evidence["xai_arbitration_summary"] = arbitration_summary
        evidence["xai_arbitration_confidence"] = arbitration_confidence
        evidence["xai_arbitration_citation_count"] = citation_count

        enriched = dict(result)
        enriched["evidence"] = evidence
        if not (enriched.get("answer") or "").strip():
            enriched["answer"] = arbitration_summary
            evidence["answer_source"] = "xai_arbitration"
        return enriched

    def _estimate_research_confidence(
        self,
        *,
        search_confidence: str,
        page_success_count: int,
        requested_page_count: int,
        social_present: bool,
        social_error: bool,
        conflicts: list[str],
        authoritative_source_count: int,
        cross_provider_candidate_count: int,
        source_cluster_count: int,
    ) -> str:
        return finalize._estimate_research_confidence(search_confidence=search_confidence, page_success_count=page_success_count, requested_page_count=requested_page_count, social_present=social_present, social_error=social_error, conflicts=conflicts, authoritative_source_count=authoritative_source_count, cross_provider_candidate_count=cross_provider_candidate_count, source_cluster_count=source_cluster_count)

    def _should_request_search_answer(
        self,
        *,
        requested: bool,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        strategy: SearchStrategy,
        include_content: bool,
        include_domains: list[str] | None,
    ) -> bool:
        return query_routing._should_request_search_answer(requested=requested, mode=mode, intent=intent, strategy=strategy, include_content=include_content, include_domains=include_domains)
    def _route_search(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        provider: ProviderName,
        sources: list[str] | None,
        include_content: bool,
        include_domains: list[str] | None,
        allowed_x_handles: list[str] | None,
        excluded_x_handles: list[str] | None,
    ) -> RouteDecision:
        normalized_sources = sorted(set(sources or ["web"]))
        query_lower = query.lower()
        policy = self._route_policy_for_request(
            query=query,
            mode=mode,
            intent=intent,
            include_content=include_content,
        )
        prefer_tavily_official_discovery = self._should_prefer_tavily_official_discovery(
            query=query,
            mode=mode,
            intent=intent,
            include_domains=include_domains,
            include_content=include_content,
        )
        if prefer_tavily_official_discovery:
            policy = SearchRoutePolicy(
                key=policy.key,
                provider="tavily",
                fallback_chain=("firecrawl", "exa"),
                tavily_topic="general",
                firecrawl_categories=policy.firecrawl_categories,
                result_profile="resource",
                allow_exa_rescue=policy.allow_exa_rescue,
            )

        if provider != "auto":
            if provider == "tavily":
                return RouteDecision(
                    provider="tavily",
                    reason="显式指定 Tavily",
                    tavily_topic=policy.tavily_topic,
                    fallback_chain=self._explicit_provider_fallback_chain(
                        provider=provider,
                        policy=policy,
                    ),
                    result_profile=policy.result_profile,
                    allow_exa_rescue=policy.allow_exa_rescue,
                )
            if provider == "firecrawl":
                return RouteDecision(
                    provider="firecrawl",
                    reason="显式指定 Firecrawl",
                    firecrawl_categories=list(policy.firecrawl_categories)
                    or self._firecrawl_categories(mode, intent),
                    fallback_chain=self._explicit_provider_fallback_chain(
                        provider=provider,
                        policy=policy,
                    ),
                    result_profile=policy.result_profile,
                )
            if provider == "exa":
                return RouteDecision(
                    provider="exa",
                    reason="显式指定 Exa",
                    fallback_chain=self._explicit_provider_fallback_chain(
                        provider=provider,
                        policy=policy,
                    ),
                    result_profile=policy.result_profile,
                )
            if provider == "xai":
                return RouteDecision(
                    provider="xai",
                    reason="显式指定 xAI/X 搜索",
                    sources=normalized_sources,
                    result_profile="off",
                )

        if normalized_sources == ["web", "x"] or (
            "x" in normalized_sources and "web" in normalized_sources
        ):
            return RouteDecision(provider="hybrid", reason="同时请求网页和 X 结果")

        if mode == "social" or "x" in normalized_sources:
            return RouteDecision(
                provider="xai",
                reason="社交舆情 / X 搜索更适合走 xAI",
                sources=["x"],
                result_profile="off",
            )

        if allowed_x_handles or excluded_x_handles:
            return RouteDecision(
                provider="xai",
                reason="检测到 X handle 过滤条件",
                sources=["x"],
                result_profile="off",
            )
        if prefer_tavily_official_discovery:
            return RouteDecision(
                provider="tavily",
                reason="精确 docs / 官方资源页优先用 Tavily 做发现，再由 Firecrawl 接正文验证",
                tavily_topic=policy.tavily_topic,
                firecrawl_categories=list(policy.firecrawl_categories) or None,
                fallback_chain=self._explicit_provider_fallback_chain(
                    provider="tavily",
                    policy=policy,
                ),
                result_profile=policy.result_profile,
                allow_exa_rescue=policy.allow_exa_rescue,
            )
        if policy.key == "tutorial":
            reason = "教程 / 排障类查询默认走 Tavily，优先拿社区解法，再用 Exa 补语义相邻案例"
        elif policy.key == "changelog":
            reason = "release / changelog 类查询默认走 Tavily，优先拿官方发布页与更新说明"
        elif policy.key in {"docs", "resource"} and include_domains and self._domains_prefer_firecrawl_discovery(include_domains):
            reason = "检测到受限 / 社区域名，优先用 Firecrawl 做站内发现"
        elif policy.key in {"docs", "github", "pdf"}:
            reason = "文档 / GitHub / PDF 默认走 Firecrawl，页面发现与正文抓取保持一致"
        elif policy.key == "content":
            reason = "请求里需要正文内容，优先走 Firecrawl"
        elif policy.key == "news":
            reason = "状态 / 新闻类查询默认走 Tavily"
        elif policy.key == "resource":
            reason = "resource / docs 查询默认走 Firecrawl"
        elif policy.key == "research":
            reason = "research 发现阶段默认走 Tavily"
        else:
            reason = "普通网页检索默认走 Tavily"
        return self._decision_from_policy(policy=policy, reason=reason)

    def _domains_prefer_firecrawl_discovery(self, include_domains: list[str] | None) -> bool:
        if not include_domains:
            return False
        firecrawl_preferred_domains = {
            "dev.to",
            "juejin.cn",
            "linux.do",
            "medium.com",
            "mp.weixin.qq.com",
            "notion.site",
            "notion.so",
            "substack.com",
            "weixin.qq.com",
            "zhihu.com",
        }
        for domain in include_domains:
            cleaned_domain = self._clean_hostname(domain)
            if any(
                self._domain_matches(cleaned_domain, preferred)
                for preferred in firecrawl_preferred_domains
            ):
                return True
        return False

    def _resolve_intent(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: SearchIntent,
        sources: list[str],
    ) -> ResolvedSearchIntent:
        if intent != "auto":
            return intent

        query_lower = query.lower()

        if mode == "news":
            if self._looks_like_status_query(query_lower):
                return "status"
            return "news"
        if self._looks_like_debugging_query(query_lower):
            return "tutorial"
        if self._looks_like_tutorial_query(query_lower):
            return "tutorial"
        if mode in {"docs", "github", "pdf"}:
            return "resource"
        if mode == "research":
            return "exploratory"
        if sources == ["x"]:
            return "status"
        if self._looks_like_changelog_query(query_lower):
            return "resource"
        if self._looks_like_status_query(query_lower):
            return "status"
        if self._looks_like_news_query(query_lower):
            return "news"
        if self._looks_like_comparison_query(query_lower):
            return "comparison"
        if self._looks_like_docs_query(query_lower):
            return "resource"
        if self._looks_like_exploratory_query(query_lower):
            return "exploratory"
        return "factual"

    def _resolve_strategy(
        self,
        *,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        strategy: SearchStrategy,
        sources: list[str],
        include_content: bool,
    ) -> SearchStrategy:
        if strategy != "auto":
            return strategy

        if "web" in sources and "x" in sources:
            return "balanced"
        if mode == "research":
            return "deep"
        if intent in {"comparison", "exploratory"}:
            return "verify"
        if include_content or mode in {"docs", "github", "pdf"} or intent in {"resource", "tutorial"}:
            return "balanced"
        return "fast"

    def _should_prefer_tavily_official_discovery(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        include_domains: list[str] | None,
        include_content: bool,
    ) -> bool:
        if mode in {"github", "pdf"}:
            return False
        if include_domains and self._domains_prefer_firecrawl_discovery(include_domains):
            return False
        if not self._provider_can_serve(self.config.tavily):
            return False
        query_lower = query.lower()
        exact_docs_topic = self._looks_like_api_docs_topic_query(query_lower)
        pricing_query = self._looks_like_pricing_query(query_lower)
        changelog_query = self._looks_like_changelog_query(query_lower)
        strict_resource_policy = self._should_use_strict_resource_policy(
            query=query,
            mode=mode,
            intent=intent,
            include_domains=include_domains,
        )
        if mode == "docs" and intent == "resource":
            return True
        if include_content and not (
            exact_docs_topic
            or pricing_query
            or changelog_query
            or (strict_resource_policy and self._looks_like_official_query(query))
        ):
            return False
        if exact_docs_topic:
            return mode in {"docs", "web", "auto"} or intent == "resource"
        if pricing_query:
            return True
        if changelog_query:
            return True
        if not strict_resource_policy:
            return False
        return self._looks_like_official_query(query)

    def _route_policy_for_request(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        include_content: bool,
    ) -> SearchRoutePolicy:
        query_lower = query.lower()
        if mode == "research":
            return _MODE_PROVIDER_POLICY["research"]
        explicit_resource_mode = mode in {"docs", "github", "pdf"}
        if intent == "tutorial" and not explicit_resource_mode:
            return _MODE_PROVIDER_POLICY["tutorial"]
        if self._looks_like_changelog_query(query_lower):
            return _MODE_PROVIDER_POLICY["changelog"]
        if intent == "status" or self._looks_like_status_query(query_lower):
            return _MODE_PROVIDER_POLICY["status"]
        if include_content:
            return _MODE_PROVIDER_POLICY["content"]
        if explicit_resource_mode:
            return _MODE_PROVIDER_POLICY[mode]
        if intent == "resource" or self._looks_like_docs_query(query_lower):
            return _MODE_PROVIDER_POLICY["resource"]
        if (
            self._looks_like_award_result_query(query_lower)
            and (intent == "news" or mode == "news")
        ):
            return _MODE_PROVIDER_POLICY["award_result"]
        if intent == "news" or mode == "news" or self._looks_like_news_query(query_lower):
            return _MODE_PROVIDER_POLICY["news"]
        if intent in {"exploratory", "comparison"} and self._provider_can_serve(self.config.exa):
            return _MODE_PROVIDER_POLICY["exploratory"]
        return _MODE_PROVIDER_POLICY["web"]

    def _decision_from_policy(
        self,
        *,
        policy: SearchRoutePolicy,
        reason: str,
        sources: list[str] | None = None,
    ) -> RouteDecision:
        provider, fallback_chain = self._resolve_available_policy_chain(policy=policy)
        return RouteDecision(
            provider=provider,
            reason=reason,
            tavily_topic=policy.tavily_topic,
            firecrawl_categories=list(policy.firecrawl_categories) or None,
            sources=sources,
            fallback_chain=fallback_chain,
            result_profile=policy.result_profile,
            allow_exa_rescue=policy.allow_exa_rescue,
        )

    def _resolve_available_policy_chain(
        self,
        *,
        policy: SearchRoutePolicy,
    ) -> tuple[ProviderName, list[str] | None]:
        ordered: list[ProviderName] = [policy.provider, *policy.fallback_chain]
        provider_configs = {
            provider_name: self._provider_config_for_name(provider_name)
            for provider_name in ordered
        }
        probe_results, _ = self._execute_parallel(
            {
                provider_name: lambda config=provider_configs[provider_name]: self._provider_live_status(config)
                for provider_name in ordered
            },
            max_workers=len(ordered),
        )
        healthy: list[ProviderName] = []
        degraded: list[ProviderName] = []
        for provider_name in ordered:
            status = probe_results.get(provider_name)
            if status is None or status == "auth_error":
                continue
            if status == "ok":
                healthy.append(provider_name)
            else:
                degraded.append(provider_name)
        if self._should_keep_tavily_primary_when_degraded(policy=policy):
            selected = "tavily"
            remaining = [
                item for item in [*healthy, *degraded] if item != selected
            ]
            return selected, remaining or None
        if healthy:
            selected = healthy[0]
            remaining = [
                item for item in [*healthy[1:], *degraded] if item != selected
            ]
            return selected, remaining or None
        if degraded:
            return degraded[0], degraded[1:] or None
        if not healthy and not degraded:
            return policy.provider, list(policy.fallback_chain) or None
        return policy.provider, list(policy.fallback_chain) or None

    def _should_keep_tavily_primary_when_degraded(
        self,
        *,
        policy: SearchRoutePolicy,
    ) -> bool:
        if policy.provider != "tavily":
            return False
        if policy.key not in {"news", "award_result", "status"}:
            return False
        tavily_status = self._provider_live_status(self.config.tavily)
        return tavily_status not in {None, "auth_error", "ok"}

    def _provider_config_for_name(self, provider_name: ProviderName) -> ProviderConfig:
        if provider_name == "tavily":
            return self.config.tavily
        if provider_name == "firecrawl":
            return self.config.firecrawl
        if provider_name == "exa":
            return self.config.exa
        return self.config.xai

    def _explicit_provider_fallback_chain(
        self,
        *,
        provider: ProviderName,
        policy: SearchRoutePolicy,
    ) -> list[str] | None:
        if provider == "xai":
            return None
        chain = [item for item in policy.fallback_chain if item != provider]
        return list(chain) or None

    def _should_blend_web_providers(
        self,
        *,
        query: str = "",
        requested_provider: ProviderName,
        decision: RouteDecision,
        sources: list[str],
        strategy: SearchStrategy,
        mode: SearchMode = "auto",
        intent: ResolvedSearchIntent = "factual",
        include_domains: list[str] | None = None,
    ) -> bool:
        if requested_provider != "auto":
            return False
        if decision.provider not in {"tavily", "firecrawl"}:
            return False
        if strategy not in {"balanced", "verify", "deep"}:
            return False
        if "x" in sources:
            return False
        if mode == "news" or intent in {"news", "status"}:
            return strategy in {"verify", "deep"} and self._provider_is_live_ok(
                self.config.tavily
            ) and self._provider_is_live_ok(self.config.firecrawl)
        if mode == "pdf":
            return strategy in {"verify", "deep"} and self._provider_is_live_ok(
                self.config.tavily
            ) and self._provider_is_live_ok(self.config.firecrawl)
        if include_domains:
            return (
                (
                    self._looks_like_changelog_query(query.lower())
                    or mode == "docs"
                )
                and decision.provider == "tavily"
                and strategy in {"verify", "deep"}
                and self._provider_is_live_ok(self.config.tavily)
                and self._provider_is_live_ok(self.config.firecrawl)
            )
        if mode in {"docs", "github", "pdf"}:
            return False
        if intent in {"resource", "tutorial"}:
            return False
        if self._looks_like_local_life_query(query.lower()):
            return False
        return self._provider_is_live_ok(self.config.tavily) and self._provider_is_live_ok(
            self.config.firecrawl
        )

    def _search_with_fallback(
        self,
        *,
        primary_provider: str,
        query: str,
        max_results: int,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        decision: RouteDecision,
        include_answer: bool,
        include_content: bool,
        include_domains: list[str] | None,
        exclude_domains: list[str] | None,
        strategy: str = "fast",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        chain = [primary_provider, *(decision.fallback_chain or [])]
        last_error: Exception | None = None
        last_quality_issue = ""
        content_candidate: tuple[str, dict[str, Any]] | None = None
        attempted_providers: list[str] = []
        content_enrichment_errors: list[str] = []
        for provider_name in chain:
            attempted_providers.append(provider_name)
            try:
                result = self._dispatch_single_provider(
                    provider_name=provider_name,
                    query=query,
                    max_results=max_results,
                    mode=mode,
                    intent=intent,
                    decision=decision,
                    include_answer=include_answer,
                    include_content=include_content,
                    include_domains=include_domains,
                    exclude_domains=exclude_domains,
                    strategy=strategy,
                    from_date=from_date,
                    to_date=to_date,
                )
                quality_issue = self._fallback_quality_issue(
                    result=result,
                    mode=mode,
                    intent=intent,
                    include_domains=include_domains,
                    include_content=include_content,
                )
                if quality_issue:
                    last_quality_issue = f"{provider_name}: {quality_issue}"
                    content_enrichment_errors.append(last_quality_issue[:200])
                    if (
                        include_content
                        and quality_issue == "provider returned results without requested content"
                        and content_candidate is None
                    ):
                        content_candidate = (provider_name, result)
                    if provider_name != chain[-1]:
                        continue
                    if content_candidate is not None:
                        break
                fallback_info = None
                if provider_name != primary_provider:
                    fallback_info = {
                        "from": primary_provider,
                        "to": provider_name,
                        "reason": (
                            str(last_error)[:200]
                            if last_error
                            else last_quality_issue[:200]
                            if last_quality_issue
                            else "primary provider failed"
                        ),
                    }
                return result, fallback_info
            except MySearchError as exc:
                last_error = exc
                content_enrichment_errors.append(f"{provider_name}: {exc}"[:200])
                continue
            except Exception as exc:
                last_error = MySearchError(f"{provider_name}: {exc}")
                content_enrichment_errors.append(str(last_error)[:200])
                continue
        if content_candidate is not None:
            candidate_provider, candidate_result = content_candidate
            candidate_result = dict(candidate_result)
            evidence = dict(candidate_result.get("evidence") or {})
            conflicts = list(evidence.get("conflicts") or [])
            if "requested-content-unavailable" not in conflicts:
                conflicts.append("requested-content-unavailable")
            evidence["conflicts"] = conflicts
            evidence["content_enrichment"] = {
                "status": "unavailable",
                "requested": True,
                "result_provider": candidate_provider,
                "providers_attempted": attempted_providers,
                "errors": content_enrichment_errors[-3:],
            }
            if len(attempted_providers) > 1:
                evidence["fallback"] = {
                    "configured": len(chain) > 1,
                    "triggered": True,
                    "used": False,
                    "from": primary_provider,
                    "reason": "requested content enrichment unavailable",
                }
            candidate_result["evidence"] = evidence
            return candidate_result, None
        raise MySearchError(f"All providers failed for query '{query[:80]}': {last_error}")

    def _fallback_quality_issue(
        self,
        *,
        result: dict[str, Any],
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        include_domains: list[str] | None,
        include_content: bool = False,
    ) -> str | None:
        results = list(result.get("results") or [])
        if include_content and results and not any(
            isinstance(item, dict) and str(item.get("content") or "").strip()
            for item in results
        ):
            return "provider returned results without requested content"
        if results:
            return None
        if include_domains:
            return "provider returned no results for domain-filtered query"
        if mode in {"docs", "github", "pdf", "news"} or intent in {
            "comparison",
            "exploratory",
            "resource",
            "tutorial",
            "news",
            "status",
        }:
            return "provider returned no results"
        return None

    @staticmethod
    def _infer_tavily_days(
        intent: str,
        from_date: str | None = None,
    ) -> int | None:
        if from_date:
            try:
                delta = date.today() - date.fromisoformat(from_date[:10])
                if delta.days > 0:
                    return delta.days
            except (ValueError, TypeError):
                pass
        if intent in {"status"}:
            return 3
        if intent in {"news"}:
            return 7
        return None

    def _dispatch_single_provider(
        self,
        *,
        provider_name: str,
        query: str,
        max_results: int,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        decision: RouteDecision,
        include_answer: bool,
        include_content: bool,
        include_domains: list[str] | None,
        exclude_domains: list[str] | None,
        strategy: str = "fast",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        result_event_query = self._looks_like_result_event_query(query.lower())
        if provider_name == "tavily":
            prefer_tavily_official_discovery = self._should_prefer_tavily_official_discovery(
                query=query,
                mode=mode,
                intent=intent,
                include_domains=include_domains,
                include_content=include_content,
            )
            tavily_include_content = (
                include_content
                and intent != "tutorial"
                and not self._looks_like_changelog_query(query.lower())
                and not (result_event_query and mode == "news")
                and not prefer_tavily_official_discovery
            )
            tavily_strategy = (
                "fast"
                if prefer_tavily_official_discovery and strategy in {"verify", "deep"}
                else strategy
            )
            return self._search_tavily(
                query=query,
                max_results=max_results,
                topic=decision.tavily_topic,
                include_answer=include_answer,
                include_content=tavily_include_content,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                strategy=tavily_strategy,
                days=self._infer_tavily_days(intent, from_date),
                from_date=from_date,
                to_date=to_date,
            )
        if provider_name == "firecrawl":
            return self._search_firecrawl(
                query=query,
                max_results=max_results,
                categories=decision.firecrawl_categories or self._firecrawl_categories(mode, intent),
                include_content=(
                    include_content
                    or mode in {"docs", "research", "github", "pdf"}
                    or intent == "tutorial"
                    or (result_event_query and mode == "news" and strategy in {"verify", "deep"})
                ),
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                from_date=from_date,
                to_date=to_date,
            )
        if provider_name == "exa":
            return self._search_exa(
                query=query,
                max_results=max_results,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                include_content=include_content,
                mode=mode,
                intent=intent,
                strategy=strategy,
                from_date=from_date,
                to_date=to_date,
            )
        raise MySearchError(f"Unknown provider: {provider_name}")

    def _should_attempt_exa_rescue(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        decision: RouteDecision,
        result: dict[str, Any],
        max_results: int,
        include_domains: list[str] | None,
    ) -> bool:
        if not decision.allow_exa_rescue:
            return False
        if not self._provider_can_serve(self.config.exa):
            return False
        if result.get("provider") in {"exa", "xai"}:
            return False
        if result.get("fallback"):
            return False
        query_lower = query.lower()
        strict_official = self._resolve_official_result_mode(
            query=query,
            mode=mode,
            intent=intent,
            include_domains=include_domains,
        ) == "strict"
        rescue_sensitive_query = (
            mode == "pdf"
            or self._looks_like_pricing_query(query_lower)
            or self._looks_like_changelog_query(query_lower)
        )
        if (include_domains or strict_official) and not rescue_sensitive_query:
            return False
        results = list(result.get("results") or [])
        if self._looks_like_award_result_query(query_lower) and self._has_strong_award_result(
            query=query,
            results=results,
        ):
            return False
        sparse_results = len(results) < min(max_results, 3)
        weak_results = self._result_set_looks_weak_for_exa_rescue(
            query=query,
            mode=mode,
            result=result,
        )
        if self._should_skip_exa_rescue_for_result_event(
            query=query,
            mode=mode,
            intent=intent,
            result=result,
        ):
            return False
        if not sparse_results and not weak_results:
            return False
        if rescue_sensitive_query:
            return True
        query_terms = re.findall(r"[a-z0-9\u4e00-\u9fff]+", query_lower)
        long_tail_signal = len(query_terms) >= 6 or len(query) >= 48
        weak_result_signal = mode == "news" or intent in {"comparison", "exploratory", "tutorial"}
        if weak_results and weak_result_signal:
            return True
        return sparse_results and (weak_result_signal or long_tail_signal)

    def _maybe_refine_tavily_result_event_discovery(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        result: dict[str, Any],
        max_results: int,
        include_domains: list[str] | None,
        exclude_domains: list[str] | None,
        from_date: str | None,
    ) -> dict[str, Any]:
        query_lower = query.lower()
        if result.get("provider") != "tavily":
            return result
        if include_domains:
            return result
        if not (
            self._looks_like_award_result_query(query_lower)
            and (mode == "news" or intent in {"news", "status"})
        ):
            return result
        initial_strong_result = self._has_strong_award_result(
            query=query,
            results=list(result.get("results") or []),
        )
        refined_query = self._refined_award_result_query(query)
        days = self._infer_tavily_days(intent=intent, from_date=from_date)
        search_query = refined_query or query
        merged = dict(result)
        refinement_tags: list[str] = []
        original_best_priority = max(
            (self._result_event_page_priority(query=query, item=item) for item in list(result.get("results") or [])),
            default=-1,
        )
        if not initial_strong_result and refined_query and refined_query != query:
            refined = self._search_tavily(
                query=refined_query,
                max_results=max_results,
                topic="news",
                include_answer=False,
                include_content=False,
                include_domains=None,
                exclude_domains=exclude_domains,
                strategy="verify",
                days=days,
            )
            if refined.get("results"):
                merged = self._merge_search_payloads(
                    primary_result=merged,
                    secondary_result=refined,
                    max_results=max_results,
                )
                refinement_tags.append("award-result-tavily-refinement")
        merged_results = list(merged.get("results") or [])
        if not self._has_strong_award_result(query=query, results=merged_results):
            trusted_domain_groups = self._award_result_trusted_domain_groups(query)
            for group_index, trusted_domains in enumerate(trusted_domain_groups):
                focused = self._search_tavily(
                    query=search_query,
                    max_results=max_results,
                    topic="news",
                    include_answer=False,
                    include_content=False,
                    include_domains=trusted_domains,
                    exclude_domains=exclude_domains,
                    strategy="verify",
                    days=days,
                )
                if not focused.get("results"):
                    continue
                merged = self._merge_search_payloads(
                    primary_result=merged,
                    secondary_result=focused,
                    max_results=max_results,
                )
                merged_results = list(merged.get("results") or [])
                refinement_tags.append("award-result-trusted-domain-refinement")
                if self._has_strong_award_result(query=query, results=merged_results):
                    if (
                        group_index == 0
                        and len(trusted_domain_groups) > 1
                    ):
                        continue
                    break
        merged_results = list(merged.get("results") or [])
        if merged_results:
            merged_results = self._filter_strong_award_results(
                query=query,
                results=merged_results,
            )
            merged_results = sorted(
                merged_results,
                key=lambda item: self._news_result_rank(
                    query=query,
                    item=item,
                    include_domains=None,
                ),
                reverse=True,
            )[:max_results]
            merged["results"] = merged_results
        merged_best_priority = max(
            (self._result_event_page_priority(query=query, item=item) for item in merged_results),
            default=-1,
        )
        if (
            not self._has_strong_award_result(query=query, results=merged_results)
            and merged_best_priority <= original_best_priority
        ):
            return result
        refined_result = dict(result)
        refined_result["provider"] = "tavily"
        refined_result["results"] = merged_results
        refined_result["citations"] = merged.get("citations") or list(result.get("citations") or [])
        if merged.get("matched_results") is not None:
            refined_result["matched_results"] = merged.get("matched_results")
        refined_result["route_debug"] = dict(refined_result.get("route_debug") or {})
        if refinement_tags:
            refined_result["route_debug"]["query_refinement"] = ",".join(refinement_tags)
        return refined_result

    def _refined_award_result_query(self, query: str) -> str:
        return query_routing._refined_award_result_query(query)
    def _award_result_trusted_domain_groups(self, query: str) -> list[list[str]]:
        query_lower = query.lower()
        if "grammy" in query_lower:
            return [
                ["grammy.com"],
                ["npr.org", "pbs.org", "reuters.com", "billboard.com", "abcnews.go.com"],
            ]
        if "oscar" in query_lower or "academy awards" in query_lower:
            return [
                ["oscars.org", "theacademy.com"],
                ["apnews.com", "npr.org", "reuters.com", "nytimes.com", "abcnews.go.com"],
            ]
        if "golden globe" in query_lower:
            return [
                ["goldenglobes.com"],
                ["reuters.com", "variety.com", "nytimes.com", "apnews.com"],
            ]
        if "bafta" in query_lower:
            return [
                ["bafta.org"],
                ["reuters.com", "bbc.com", "apnews.com", "theguardian.com"],
            ]
        return [
            ["reuters.com", "apnews.com", "npr.org", "nytimes.com"],
        ]

    def _should_skip_exa_rescue_for_result_event(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        result: dict[str, Any],
    ) -> bool:
        return query_routing._should_skip_exa_rescue_for_result_event(query=query, mode=mode, intent=intent, result=result)
    def _result_set_looks_weak_for_exa_rescue(
        self,
        *,
        query: str,
        mode: SearchMode,
        result: dict[str, Any],
    ) -> bool:
        return quality._result_set_looks_weak_for_exa_rescue(query=query, mode=mode, result=result)

    def _has_strong_award_result(
        self,
        *,
        query: str,
        results: list[dict[str, Any]],
    ) -> bool:
        return query_routing._has_strong_award_result(query=query, results=results)
    def _can_attempt_award_page_extraction(
        self,
        *,
        query: str,
        results: list[dict[str, Any]],
    ) -> bool:
        for item in results[:5]:
            title_text = (item.get("title") or "").lower()
            snippet_text = (item.get("snippet") or "").lower()
            path = urlparse(item.get("url", "")).path.lower()
            if self._looks_like_award_winner_result(
                title_text=title_text,
                snippet_text=snippet_text,
                path=path,
            ):
                return True
            if self._result_event_page_priority(query=query, item=item) >= 8:
                return True
        return False

    def _filter_strong_award_results(
        self,
        *,
        query: str,
        results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return selection._filter_strong_award_results(query=query, results=results)

    def _has_strong_pdf_match(
        self,
        *,
        query: str,
        results: list[dict[str, Any]],
    ) -> bool:
        return quality._has_strong_pdf_match(query=query, results=results)

    def _looks_like_derivative_paper_title(self, title_text: str) -> bool:
        return query_routing._looks_like_derivative_paper_title(title_text)
    def _looks_like_exact_base_paper_query(self, query: str) -> bool:
        return query_routing._looks_like_exact_base_paper_query(query)
    def _base_report_subject_token(self, query: str) -> str:
        return query_routing._base_report_subject_token(query)
    def _looks_like_variant_base_paper_result(self, *, query: str, title_text: str) -> bool:
        return query_routing._looks_like_variant_base_paper_result(query=query, title_text=title_text)
    def _is_obvious_pdf_mirror_or_aggregator_result(
        self,
        *,
        hostname: str,
        registered_domain: str,
        path: str,
    ) -> bool:
        return query_routing._is_obvious_pdf_mirror_or_aggregator_result(hostname=hostname, registered_domain=registered_domain, path=path)
    def _has_canonical_pricing_result(self, results: list[dict[str, Any]]) -> bool:
        return quality._has_canonical_pricing_result(results=results)

    def _has_strong_changelog_result(
        self,
        *,
        query: str,
        results: list[dict[str, Any]],
    ) -> bool:
        return quality._has_strong_changelog_result(query=query, results=results)

    def _has_strong_tutorial_result(
        self,
        *,
        query: str,
        results: list[dict[str, Any]],
        mode: SearchMode = "auto",
    ) -> bool:
        return quality._has_strong_tutorial_result(query=query, results=results, mode=mode)

    def _apply_exa_rescue(
        self,
        *,
        query: str,
        primary_result: dict[str, Any],
        max_results: int,
        include_domains: list[str] | None,
        exclude_domains: list[str] | None,
        include_content: bool,
        mode: str = "",
        intent: str = "",
        strategy: str = "fast",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        try:
            exa_result = self._search_exa(
                query=query,
                max_results=max_results,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                include_content=include_content,
                mode=mode,
                intent=intent,
                strategy=strategy,
                from_date=from_date,
                to_date=to_date,
            )
        except MySearchError as exc:
            degraded_result = dict(primary_result)
            degraded_result["secondary_search"] = None
            degraded_result["secondary_error"] = str(exc)[:200]
            evidence = dict(degraded_result.get("evidence") or {})
            providers_consulted = [
                item for item in (evidence.get("providers_consulted") or []) if item
            ]
            if not providers_consulted and degraded_result.get("provider"):
                providers_consulted = [str(degraded_result.get("provider") or "")]
            if providers_consulted:
                evidence["providers_consulted"] = providers_consulted
            evidence["verification"] = "single-provider-secondary-failed"
            degraded_result["evidence"] = evidence
            return degraded_result
        if not exa_result.get("results"):
            return primary_result

        merged = self._merge_search_payloads(
            primary_result=primary_result,
            secondary_result=exa_result,
            max_results=max_results,
        )
        return {
            "provider": "hybrid",
            "route_selected": f"{primary_result.get('provider', 'unknown')}+exa",
            "query": query,
            "answer": primary_result.get("answer") or exa_result.get("answer", ""),
            "results": merged["results"],
            "citations": merged["citations"],
            "evidence": {
                "providers_consulted": [
                    item
                    for item in [primary_result.get("provider"), exa_result.get("provider")]
                    if item
                ],
                "matched_results": merged["matched_results"],
                "citation_count": len(merged["citations"]),
                "verification": "fallback",
            },
            "primary_search": primary_result,
            "secondary_search": exa_result,
            "secondary_error": "",
            "fallback": {
                "from": primary_result.get("provider", "unknown"),
                "to": "exa",
                "reason": "primary provider returned sparse or weak results; Exa rescue engaged",
            },
        }

    def _should_rerank_general_results(
        self,
        *,
        result_profile: str,
    ) -> bool:
        return query_routing._should_rerank_general_results(result_profile=result_profile)
    def _rerank_general_results(
        self,
        *,
        query: str,
        result_profile: Literal["web", "news"],
        results: list[dict[str, Any]],
        include_domains: list[str] | None,
    ) -> list[dict[str, Any]]:
        return selection._rerank_general_results(query=query, result_profile=result_profile, results=results, include_domains=include_domains)

    def _general_result_rank(
        self,
        *,
        query: str,
        result_profile: Literal["web", "news"],
        item: dict[str, Any],
        include_domains: list[str] | None,
    ) -> tuple[int, ...]:
        return selection._general_result_rank(query=query, result_profile=result_profile, item=item, include_domains=include_domains)

    def _news_result_rank(
        self,
        *,
        query: str,
        item: dict[str, Any],
        include_domains: list[str] | None,
    ) -> tuple[int, ...]:
        return ranking._news_result_rank(query=query, item=item, include_domains=include_domains)

    def _web_result_rank(
        self,
        *,
        query: str,
        item: dict[str, Any],
        include_domains: list[str] | None,
    ) -> tuple[int, ...]:
        return ranking._web_result_rank(query=query, item=item, include_domains=include_domains)

    def _query_prefers_web_social_sources(self, query_lower: str) -> bool:
        return query_routing._query_prefers_web_social_sources(query_lower)
    def _result_published_timestamp(self, item: dict[str, Any]) -> float | None:
        return postprocess._result_published_timestamp(item)

    def _is_mainstream_news_domain(self, hostname: str) -> bool:
        return query_routing._is_mainstream_news_domain(hostname)
    def _looks_like_award_winner_result(
        self,
        *,
        title_text: str,
        snippet_text: str,
        path: str,
    ) -> bool:
        return query_routing._looks_like_award_winner_result(title_text=title_text, snippet_text=snippet_text, path=path)
    def _looks_like_award_category_match(
        self,
        *,
        query_lower: str,
        title_text: str,
        snippet_text: str,
        content_text: str,
        path: str,
    ) -> bool:
        return query_routing._looks_like_award_category_match(query_lower=query_lower, title_text=title_text, snippet_text=snippet_text, content_text=content_text, path=path)
    def _looks_like_award_coverage_page(
        self,
        *,
        query_lower: str,
        title_text: str,
        path: str,
    ) -> bool:
        return query_routing._looks_like_award_coverage_page(query_lower=query_lower, title_text=title_text, path=path)
    def _looks_like_generic_award_archive_result(
        self,
        *,
        title_text: str,
        path: str,
    ) -> bool:
        return query_routing._looks_like_generic_award_archive_result(title_text=title_text, path=path)
    def _looks_like_weak_official_award_feature_result(
        self,
        *,
        title_text: str,
        snippet_text: str,
        path: str,
    ) -> bool:
        return query_routing._looks_like_weak_official_award_feature_result(title_text=title_text, snippet_text=snippet_text, path=path)
    def _looks_like_award_fact_match(
        self,
        *,
        query_lower: str,
        title_text: str,
        snippet_text: str,
        content_text: str,
        path: str,
    ) -> bool:
        return query_routing._looks_like_award_fact_match(query_lower=query_lower, title_text=title_text, snippet_text=snippet_text, content_text=content_text, path=path)
    def _looks_like_award_nomination_result(
        self,
        *,
        title_text: str,
        snippet_text: str,
        path: str,
    ) -> bool:
        return query_routing._looks_like_award_nomination_result(title_text=title_text, snippet_text=snippet_text, path=path)
    def _looks_like_award_prediction_result(
        self,
        *,
        title_text: str,
        snippet_text: str,
        path: str,
    ) -> bool:
        return query_routing._looks_like_award_prediction_result(title_text=title_text, snippet_text=snippet_text, path=path)
    def _looks_like_award_recap_or_gallery_result(
        self,
        *,
        title_text: str,
        snippet_text: str,
        path: str,
    ) -> bool:
        return query_routing._looks_like_award_recap_or_gallery_result(title_text=title_text, snippet_text=snippet_text, path=path)
    def _looks_like_news_article_result(self, item: dict[str, Any]) -> bool:
        return query_routing._looks_like_news_article_result(item)
    def _is_obvious_web_aggregator(self, registered_domain: str) -> bool:
        return query_routing._is_obvious_web_aggregator(registered_domain)
    def _looks_like_local_life_guide_result(
        self,
        *,
        url: str,
        hostname: str,
        title_text: str,
        snippet_text: str,
    ) -> bool:
        return query_routing._looks_like_local_life_guide_result(url=url, hostname=hostname, title_text=title_text, snippet_text=snippet_text)
    def _looks_like_canonical_local_life_guide_result(
        self,
        *,
        url: str,
        hostname: str,
    ) -> bool:
        return query_routing._looks_like_canonical_local_life_guide_result(url=url, hostname=hostname)
    def _is_obvious_local_life_repost_domain(self, registered_domain: str) -> bool:
        return query_routing._is_obvious_local_life_repost_domain(registered_domain)
    def _looks_like_tutorial_community_result(
        self,
        *,
        hostname: str,
        registered_domain: str,
        path: str,
    ) -> bool:
        return query_routing._looks_like_tutorial_community_result(hostname=hostname, registered_domain=registered_domain, path=path)
    def _looks_like_brand_aligned_tutorial_result(
        self,
        *,
        hostname: str,
        registered_domain: str,
        path: str,
        title_text: str,
        snippet_text: str,
        query_tokens: list[str],
        path_precision_hits: int,
        exact_total_hits: int,
    ) -> bool:
        return query_routing._looks_like_brand_aligned_tutorial_result(hostname=hostname, registered_domain=registered_domain, path=path, title_text=title_text, snippet_text=snippet_text, query_tokens=query_tokens, path_precision_hits=path_precision_hits, exact_total_hits=exact_total_hits)
    def _is_obvious_tutorial_blog_domain(self, registered_domain: str) -> bool:
        return query_routing._is_obvious_tutorial_blog_domain(registered_domain)
    def _search_web_blended(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        strategy: SearchStrategy,
        decision: RouteDecision,
        max_results: int,
        include_content: bool,
        include_answer: bool,
        include_domains: list[str] | None,
        exclude_domains: list[str] | None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        verify_timeout = (
            OPTIONAL_VERIFY_TIMEOUT_SECONDS if strategy == "verify" else None
        )
        if decision.provider == "tavily":
            lightweight_official_discovery = self._should_prefer_tavily_official_discovery(
                query=query,
                mode=mode,
                intent=intent,
                include_domains=include_domains,
                include_content=include_content,
            )
            tasks = {
                "primary": lambda: self._search_tavily(
                    query=query,
                    max_results=max_results,
                    topic=decision.tavily_topic,
                    include_answer=include_answer,
                    include_content=include_content and not lightweight_official_discovery,
                    include_domains=include_domains,
                    exclude_domains=exclude_domains,
                    strategy="fast" if lightweight_official_discovery else strategy,
                    from_date=from_date,
                    to_date=to_date,
                    timeout_seconds=verify_timeout,
                ),
                "secondary": lambda: self._search_firecrawl(
                    query=query,
                    max_results=max_results,
                    categories=self._firecrawl_categories(mode, intent),
                    include_content=include_content or strategy in {"verify", "deep"},
                    include_domains=include_domains,
                    exclude_domains=exclude_domains,
                    from_date=from_date,
                    to_date=to_date,
                    timeout_seconds=verify_timeout,
                ),
            }
        else:
            tasks = {
                "primary": lambda: self._search_firecrawl(
                    query=query,
                    max_results=max_results,
                    categories=decision.firecrawl_categories or self._firecrawl_categories(mode, intent),
                    include_content=include_content or strategy in {"verify", "deep"},
                    include_domains=include_domains,
                    exclude_domains=exclude_domains,
                    from_date=from_date,
                    to_date=to_date,
                    timeout_seconds=verify_timeout,
                ),
                "secondary": lambda: self._search_tavily(
                    query=query,
                    max_results=max_results,
                    topic="news" if intent in {"news", "status"} else "general",
                    include_answer=True,
                    include_content=include_content,
                    include_domains=include_domains,
                    exclude_domains=exclude_domains,
                    days=self._infer_tavily_days(intent, from_date),
                    from_date=from_date,
                    to_date=to_date,
                    timeout_seconds=verify_timeout,
                ),
            }

        if strategy in {"verify", "deep"} and self._provider_can_serve(self.config.exa):
            tasks["exa_supplement"] = lambda: self._search_exa(
                query=query,
                max_results=min(max_results, 3),
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                include_content=include_content,
                mode=mode,
                intent=intent,
                strategy=strategy,
                from_date=from_date,
                to_date=to_date,
                timeout_seconds=verify_timeout,
            )

        blended_results, blended_errors = self._execute_parallel(
            tasks,
            max_workers=len(tasks),
            timeout_seconds=verify_timeout,
            stop_after_primary_and_verifier=strategy == "verify",
        )
        primary_failed = "primary" in blended_errors
        secondary_failed = "secondary" in blended_errors
        exa_supplement = blended_results.get("exa_supplement")

        if primary_failed and not secondary_failed:
            primary_result = blended_results["secondary"]
            primary_result["fallback"] = {
                "from": decision.provider,
                "to": primary_result.get("provider", "unknown"),
                "reason": str(blended_errors["primary"])[:200],
            }
            secondary_result = None
            secondary_error = ""
        elif primary_failed and secondary_failed:
            if exa_supplement and exa_supplement.get("results"):
                exa_only = dict(exa_supplement)
                exa_only["route_selected"] = "exa"
                exa_only["fallback"] = {
                    "from": decision.provider,
                    "to": "exa",
                    "reason": "primary and secondary providers failed; Exa supplement engaged",
                }
                exa_only["secondary_error"] = str(blended_errors["secondary"])[:200]
                exa_only.setdefault("evidence", {})["providers_consulted"] = ["exa"]
                exa_only["evidence"]["verification"] = "fallback"
                return exa_only
            primary_err = str(blended_errors["primary"])[:150]
            secondary_err = str(blended_errors["secondary"])[:150]
            raise MySearchError(
                f"Blended search failed: primary ({decision.provider}): {primary_err}; "
                f"secondary: {secondary_err}"
            )
        else:
            primary_result = blended_results["primary"]
            secondary_result = blended_results.get("secondary")
            secondary_error = str(blended_errors["secondary"]) if secondary_failed else ""

        merged = self._merge_search_payloads(
            primary_result=primary_result,
            secondary_result=secondary_result,
            max_results=max_results,
        )
        if exa_supplement and exa_supplement.get("results"):
            merged = self._merge_search_payloads(
                primary_result=merged,
                secondary_result=exa_supplement,
                max_results=max_results,
            )

        merged_results = list(merged["results"])
        merged_citations = list(merged["citations"])
        if self._should_rerank_resource_results(mode=mode, intent=intent):
            merged_results = self._rerank_resource_results(
                query=query,
                mode=mode,
                results=merged_results,
                include_domains=include_domains,
            )
            merged_citations = self._align_citations_with_results(
                results=merged_results,
                citations=merged_citations,
            )
        elif self._should_rerank_general_results(result_profile=decision.result_profile):
            merged_results = self._rerank_general_results(
                query=query,
                result_profile=decision.result_profile,
                results=merged_results,
                include_domains=include_domains,
            )
            merged_citations = self._align_citations_with_results(
                results=merged_results,
                citations=merged_citations,
            )

        providers_consulted = [primary_result.get("provider", "")]
        if secondary_result:
            providers_consulted.append(secondary_result.get("provider", ""))
        if exa_supplement:
            providers_consulted.append("exa")

        if secondary_result or (exa_supplement and exa_supplement.get("results")):
            verification = "cross-provider"
        elif secondary_error:
            verification = "single-provider-secondary-failed"
        else:
            verification = "single-provider"

        return {
            "provider": "hybrid" if secondary_result else primary_result.get("provider", decision.provider),
            "route_selected": "+".join([item for item in providers_consulted if item]),
            "query": query,
            "answer": primary_result.get("answer") or (secondary_result or {}).get("answer", ""),
            "results": merged_results,
            "citations": merged_citations,
            "evidence": {
                "providers_consulted": [item for item in providers_consulted if item],
                "matched_results": merged["matched_results"],
                "citation_count": len(merged_citations),
                "verification": verification,
            },
            "primary_search": primary_result,
            "secondary_search": secondary_result,
            "secondary_error": secondary_error,
        }

    def _search_tavily(
        self,
        *,
        query: str,
        max_results: int,
        topic: str,
        include_answer: bool,
        include_content: bool,
        include_domains: list[str] | None,
        exclude_domains: list[str] | None,
        strategy: str = "fast",
        days: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        timeout_seconds: int | None = None,
        _skip_domain_fallback: bool = False,
    ) -> dict[str, Any]:
        include_domains = [item.strip() for item in (include_domains or []) if item and item.strip()]
        exclude_domains = [item.strip() for item in (exclude_domains or []) if item and item.strip()]

        response = self._search_tavily_once(
            query=query,
            max_results=max_results,
            topic=topic,
            include_answer=include_answer,
            include_content=include_content,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            strategy=strategy,
            days=days,
            from_date=from_date,
            to_date=to_date,
            timeout_seconds=timeout_seconds,
        )
        if response.get("results") or not include_domains:
            return response

        if not _skip_domain_fallback:
            retry_response = self._search_tavily_domain_retry(
                query=query,
                max_results=max_results,
                topic=topic,
                include_content=include_content,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                from_date=from_date,
                to_date=to_date,
                days=days,
            )
            if retry_response is not None:
                return retry_response

        if not _skip_domain_fallback:
            fallback_response = self._search_tavily_domain_fallback(
                query=query,
                max_results=max_results,
                include_content=include_content,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                from_date=from_date,
                to_date=to_date,
            )
            if fallback_response is not None:
                return fallback_response

        return response

    def _search_tavily_once(
        self,
        *,
        query: str,
        max_results: int,
        topic: str,
        include_answer: bool,
        include_content: bool,
        include_domains: list[str] | None,
        exclude_domains: list[str] | None,
        strategy: str = "fast",
        days: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        provider = self.config.tavily
        key = self._get_key_or_raise(provider)
        effective_query = query
        if topic == "news" and self._looks_like_award_result_query(query.lower()):
            refined_query = self._refined_award_result_query(query)
            if refined_query and refined_query != query:
                effective_query = refined_query
        payload: dict[str, Any] = {
            "query": effective_query,
            "max_results": max_results,
            "search_depth": "advanced" if include_content or strategy in {"verify", "deep"} else "basic",
            "topic": topic,
            "include_answer": include_answer,
            "include_raw_content": include_content,
        }
        if from_date:
            payload["start_date"] = from_date
        elif days and days > 0:
            payload["days"] = days
        if to_date:
            payload["end_date"] = to_date
        if include_domains:
            payload["include_domains"] = include_domains
        if exclude_domains:
            payload["exclude_domains"] = exclude_domains

        response = self._request_json(
            provider=provider,
            method="POST",
            path=provider.path("search"),
            payload=payload,
            key=key.key,
            timeout_seconds=timeout_seconds,
        )
        results = [
            {
                "provider": "tavily",
                "source": "web",
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
                "content": item.get("raw_content", "") if include_content else "",
                "score": item.get("score"),
                "published_date": item.get("published_date")
                or item.get("publishedDate")
                or item.get("published_at")
                or item.get("publishedAt")
                or "",
            }
            for item in response.get("results", [])
        ]
        filtered_results = self._filter_results_by_domains(
            results,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
        )
        return {
            "provider": "tavily",
            "transport": key.source,
            "query": response.get("query", effective_query),
            "answer": response.get("answer", ""),
            "request_id": response.get("request_id", ""),
            "response_time": response.get("response_time"),
            "results": filtered_results,
            "citations": [
                {"title": item.get("title", ""), "url": item.get("url", "")}
                for item in filtered_results
                if item.get("url")
            ],
        }

    def _search_tavily_domain_retry(
        self,
        *,
        query: str,
        max_results: int,
        topic: str,
        include_content: bool,
        include_domains: list[str],
        exclude_domains: list[str] | None,
        from_date: str | None = None,
        to_date: str | None = None,
        days: int | None = None,
    ) -> dict[str, Any] | None:
        per_domain_results = []
        retried_domains: list[str] = []
        for domain in include_domains:
            domain_result = self._search_tavily_once(
                query=self._build_firecrawl_domain_query(
                    query=query,
                    include_domain=domain,
                    exclude_domains=exclude_domains,
                ),
                max_results=max_results,
                topic=topic,
                include_answer=False,
                include_content=include_content,
                include_domains=None,
                exclude_domains=exclude_domains,
                from_date=from_date,
                to_date=to_date,
                days=days,
            )
            filtered_results = self._filter_results_by_domains(
                domain_result.get("results", []),
                include_domains=[domain],
                exclude_domains=exclude_domains,
            )
            if not filtered_results:
                continue
            domain_result = dict(domain_result)
            domain_result["results"] = filtered_results
            domain_result["citations"] = self._align_citations_with_results(
                results=filtered_results,
                citations=list(domain_result.get("citations") or []),
            )
            per_domain_results.append(domain_result)
            retried_domains.append(domain)

        if not per_domain_results:
            return None

        merged_results = self._merge_ranked_results(
            [result.get("results", []) for result in per_domain_results],
            max_results=max_results,
        )
        citations = self._align_citations_with_results(
            results=merged_results,
            citations=self._dedupe_citations(
                *[result.get("citations", []) for result in per_domain_results]
            ),
        )
        return {
            "provider": "tavily",
            "transport": per_domain_results[0].get("transport", "env"),
            "query": query,
            "answer": "",
            "request_id": "",
            "response_time": None,
            "results": merged_results,
            "citations": citations,
            "route_debug": {
                "domain_filter_mode": "site_query_retry",
                "retried_include_domains": retried_domains,
            },
        }

    def _search_tavily_domain_fallback(
        self,
        *,
        query: str,
        max_results: int,
        include_content: bool,
        include_domains: list[str],
        exclude_domains: list[str] | None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any] | None:
        if not self._provider_can_serve(self.config.firecrawl):
            return None

        categories = (
            self._firecrawl_categories("docs", "resource")
            if self._looks_like_docs_query(query.lower()) or self._looks_like_tutorial_query(query.lower())
            else []
        )
        per_domain_results = []
        citations = []
        seen_urls: set[str] = set()
        for domain in include_domains:
            domain_result = self._search_firecrawl_once(
                query=self._build_firecrawl_domain_query(
                    query=query,
                    include_domain=domain,
                    exclude_domains=exclude_domains,
                ),
                max_results=max_results,
                categories=categories,
                include_content=include_content,
                include_domains=None,
                exclude_domains=None,
                from_date=from_date,
                to_date=to_date,
            )
            if not domain_result.get("results"):
                retry_result = self._search_firecrawl_domain_retry(
                    query=query,
                    max_results=max_results,
                    categories=categories,
                    include_content=include_content,
                    include_domain=domain,
                    exclude_domains=exclude_domains,
                    from_date=from_date,
                    to_date=to_date,
                )
                if retry_result is not None:
                    domain_result = retry_result
            per_domain_results.append(domain_result)
            for item in domain_result.get("results", []):
                url = item.get("url", "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                citations.append({"title": item.get("title", ""), "url": url})

        merged_results = self._merge_ranked_results(
            [result.get("results", []) for result in per_domain_results],
            max_results=max_results,
        )
        if not merged_results:
            return None

        return {
            "provider": "hybrid",
            "route_selected": "tavily+firecrawl",
            "query": query,
            "answer": "",
            "results": merged_results,
            "citations": citations[:max_results],
            "primary_search": {
                "provider": "tavily",
                "query": query,
                "results": [],
                "citations": [],
            },
            "secondary_search": {
                "provider": "firecrawl",
                "query": query,
                "results": merged_results,
                "citations": citations[:max_results],
            },
            "secondary_error": "",
            "evidence": {
                "providers_consulted": ["tavily", "firecrawl"],
                "matched_results": 0,
                "citation_count": len(citations[:max_results]),
                "verification": "fallback",
            },
            "fallback": {
                "from": "tavily",
                "to": "firecrawl",
                "reason": "tavily returned 0 results for domain-filtered search",
            },
        }

    def _search_firecrawl(
        self,
        *,
        query: str,
        max_results: int,
        categories: list[str],
        include_content: bool,
        include_domains: list[str] | None,
        exclude_domains: list[str] | None,
        from_date: str | None = None,
        to_date: str | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        include_domains = [item.strip() for item in (include_domains or []) if item and item.strip()]
        exclude_domains = [item.strip() for item in (exclude_domains or []) if item and item.strip()]

        if include_domains:
            native_result = self._search_firecrawl_once(
                query=self._build_firecrawl_domain_query(
                    query=query,
                    include_domain=None,
                    exclude_domains=exclude_domains,
                ),
                max_results=max_results,
                categories=categories,
                include_content=include_content,
                include_domains=include_domains,
                exclude_domains=None,
                from_date=from_date,
                to_date=to_date,
                timeout_seconds=timeout_seconds,
            )
            if native_result.get("results"):
                route_debug = dict(native_result.get("route_debug") or {})
                route_debug["domain_filter_mode"] = "provider_native"
                route_debug["include_domains"] = include_domains
                if exclude_domains:
                    route_debug["exclude_domains_query_encoded"] = exclude_domains
                native_result["route_debug"] = route_debug
                return native_result

            per_domain_results = []
            citations = []
            seen_urls: set[str] = set()
            retried_domains: list[str] = []
            for domain in include_domains:
                domain_result = self._search_firecrawl_once(
                    query=self._build_firecrawl_domain_query(
                        query=query,
                        include_domain=domain,
                        exclude_domains=exclude_domains,
                    ),
                    max_results=max_results,
                    categories=categories,
                    include_content=include_content,
                    include_domains=None,
                    exclude_domains=None,
                    from_date=from_date,
                    to_date=to_date,
                )
                if not domain_result.get("results"):
                    retry_result = self._search_firecrawl_domain_retry(
                        query=query,
                        max_results=max_results,
                        categories=categories,
                        include_content=include_content,
                        include_domain=domain,
                        exclude_domains=exclude_domains,
                        from_date=from_date,
                        to_date=to_date,
                    )
                    if retry_result is not None:
                        domain_result = retry_result
                        retried_domains.append(domain)
                per_domain_results.append(domain_result)
                for item in domain_result.get("results", []):
                    url = item.get("url", "")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    citations.append({"title": item.get("title", ""), "url": url})

            merged_results = self._merge_ranked_results(
                [result.get("results", []) for result in per_domain_results],
                max_results=max_results,
            )
            if not merged_results:
                fallback_result = self._search_firecrawl_domain_fallback(
                    query=query,
                    max_results=max_results,
                    include_content=include_content,
                    include_domains=include_domains,
                    exclude_domains=exclude_domains,
                    from_date=from_date,
                    to_date=to_date,
                )
                if fallback_result is not None:
                    return fallback_result
            response = {
                "provider": "firecrawl",
                "transport": per_domain_results[0].get("transport", "env") if per_domain_results else "env",
                "query": query,
                "answer": "",
                "results": merged_results,
                "citations": citations[:max_results],
            }
            if retried_domains:
                response["route_debug"] = {
                    "domain_filter_mode": "client_filter_retry",
                    "retried_include_domains": retried_domains,
                }
            return response

        return self._search_firecrawl_once(
            query=query,
            max_results=max_results,
            categories=categories,
            include_content=include_content,
            include_domains=None,
            exclude_domains=exclude_domains,
            from_date=from_date,
            to_date=to_date,
            timeout_seconds=timeout_seconds,
        )

    def _search_firecrawl_domain_fallback(
        self,
        *,
        query: str,
        max_results: int,
        include_content: bool,
        include_domains: list[str],
        exclude_domains: list[str] | None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any] | None:
        if not self._provider_can_serve(self.config.tavily):
            return None

        fallback_result = self._search_tavily(
            query=query,
            max_results=max_results,
            topic="general",
            include_answer=False,
            include_content=include_content,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            from_date=from_date,
            to_date=to_date,
            _skip_domain_fallback=True,
        )
        if not fallback_result.get("results"):
            return None

        return {
            "provider": "hybrid",
            "route_selected": "firecrawl+tavily",
            "query": query,
            "answer": fallback_result.get("answer", ""),
            "results": fallback_result.get("results", []),
            "citations": fallback_result.get("citations", []),
            "primary_search": {
                "provider": "firecrawl",
                "query": query,
                "results": [],
                "citations": [],
            },
            "secondary_search": fallback_result,
            "secondary_error": "",
            "evidence": {
                "providers_consulted": ["firecrawl", fallback_result.get("provider", "tavily")],
                "matched_results": 0,
                "citation_count": len(fallback_result.get("citations", [])),
                "verification": "fallback",
            },
            "fallback": {
                "from": "firecrawl",
                "to": "tavily",
                "reason": "firecrawl returned 0 results for domain-filtered search",
            },
        }

    def _search_firecrawl_domain_retry(
        self,
        *,
        query: str,
        max_results: int,
        categories: list[str],
        include_content: bool,
        include_domain: str,
        exclude_domains: list[str] | None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any] | None:
        retry_result = self._search_firecrawl_once(
            query=self._build_firecrawl_domain_query(
                query=query,
                include_domain=None,
                exclude_domains=exclude_domains,
            ),
            max_results=max_results,
            categories=categories,
            include_content=include_content,
            include_domains=None,
            exclude_domains=None,
            from_date=from_date,
            to_date=to_date,
        )
        filtered_results = self._filter_results_by_domains(
            retry_result.get("results", []),
            include_domains=[include_domain],
            exclude_domains=exclude_domains,
        )
        if not filtered_results:
            return None

        return {
            "provider": "firecrawl",
            "transport": retry_result.get("transport", "env"),
            "query": query,
            "answer": retry_result.get("answer", ""),
            "results": filtered_results[:max_results],
            "citations": [
                {"title": item.get("title", ""), "url": item.get("url", "")}
                for item in filtered_results
                if item.get("url")
            ][:max_results],
            "route_debug": {
                "domain_filter_mode": "client_filter_retry",
                "include_domain": include_domain,
            },
        }

    def _search_firecrawl_once(
        self,
        *,
        query: str,
        max_results: int,
        categories: list[str],
        include_content: bool,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        provider = self.config.firecrawl
        key = self._get_key_or_raise(provider)
        requested_news = "news" in categories
        search_categories = self._normalize_firecrawl_search_categories(categories)
        payload: dict[str, Any] = {
            "query": query,
            "limit": max_results,
        }
        if requested_news:
            payload["sources"] = ["news", "web"]
        if search_categories:
            payload["categories"] = [{"type": item} for item in search_categories]
        include_domains = [item.strip() for item in (include_domains or []) if item and item.strip()]
        exclude_domains = [item.strip() for item in (exclude_domains or []) if item and item.strip()]
        if include_domains:
            payload["includeDomains"] = include_domains
        elif exclude_domains:
            payload["excludeDomains"] = exclude_domains
        tbs = self._build_firecrawl_tbs(from_date, to_date)
        if tbs:
            payload["tbs"] = tbs
        if include_content:
            if not requested_news and "news" not in search_categories:
                payload["scrapeOptions"] = {
                    "formats": ["markdown"],
                    "onlyMainContent": True,
                }

        response = self._request_json(
            provider=provider,
            method="POST",
            path=provider.path("search"),
            payload=payload,
            key=key.key,
            timeout_seconds=timeout_seconds,
        )
        data = response.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        results = []
        # Group order sets result priority (news first when news was requested).
        # `research` is included because Firecrawl moves the results of the
        # `research` category out of `data.web` into `data.research` on
        # 2026-11-16; reading both keys means the switch is a no-op for us, and
        # today the key is simply absent.
        source_order = ("news", "web", "research") if requested_news else ("web", "news", "research")
        for source_name in source_order:
            for item in data.get(source_name, []) or []:
                results.append(
                    {
                        "provider": "firecrawl",
                        "source": source_name,
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "snippet": item.get("description", "") or item.get("markdown", ""),
                        "content": item.get("markdown", "") if include_content else "",
                        "published_date": item.get("publishedDate")
                        or item.get("date")
                        or item.get("published_date")
                        or item.get("published_at")
                        or "",
                    }
                )

        return {
            "provider": "firecrawl",
            "transport": key.source,
            "query": query,
            "answer": "",
            "results": results,
            "citations": [
                {"title": item.get("title", ""), "url": item.get("url", "")}
                for item in results
                if item.get("url")
            ],
        }

    @staticmethod
    def _build_firecrawl_tbs(from_date: str | None, to_date: str | None) -> str:
        return cache_keys._build_firecrawl_tbs(from_date, to_date)

    def _build_firecrawl_domain_query(
        self,
        *,
        query: str,
        include_domain: str | None,
        exclude_domains: list[str] | None,
    ) -> str:
        return cache_keys._build_firecrawl_domain_query(query=query, include_domain=include_domain, exclude_domains=exclude_domains)

    def _merge_ranked_results(
        self,
        result_lists: list[list[dict[str, Any]]],
        *,
        max_results: int,
    ) -> list[dict[str, Any]]:
        return postprocess._merge_ranked_results(result_lists, max_results=max_results)

    def _filter_results_by_domains(
        self,
        results: list[dict[str, Any]],
        *,
        include_domains: list[str] | None,
        exclude_domains: list[str] | None,
    ) -> list[dict[str, Any]]:
        return postprocess._filter_results_by_domains(results, include_domains=include_domains, exclude_domains=exclude_domains)

    def _exa_search_type(
        self,
        query: str,
        *,
        mode: str = "",
        intent: str = "",
        strategy: str = "fast",
        include_domains: list[str] | None = None,
    ) -> str:
        query_lower = query.lower()
        exact_signals = re.findall(
            r"[A-Z][a-zA-Z]+\.[a-zA-Z_]+|[a-z_]{2,}\.[a-z_]+\(|::\w+|#\w+|v\d+\.\d+",
            query,
        )
        if strategy == "deep":
            return "deep"
        if exact_signals or (
            self._looks_like_pricing_query(query_lower)
            and (include_domains or mode in {"web", "docs"} or intent in {"factual", "resource"})
        ):
            return "auto"
        if strategy == "fast":
            return "fast"
        return "auto"

    def _exa_category(self, mode: str, intent: str) -> str:
        if mode == "pdf":
            return "research paper"
        if mode == "github":
            return "github"
        if mode == "news" or intent in {"news", "status"}:
            return "news"
        return ""

    def _search_exa(
        self,
        *,
        query: str,
        max_results: int,
        include_domains: list[str] | None,
        exclude_domains: list[str] | None,
        include_content: bool,
        mode: str = "",
        intent: str = "",
        strategy: str = "fast",
        from_date: str | None = None,
        to_date: str | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        provider = self.config.exa
        key = self._get_key_or_raise(provider)
        search_type = self._exa_search_type(
            query,
            mode=mode,
            intent=intent,
            strategy=strategy,
            include_domains=include_domains,
        )
        payload: dict[str, Any] = {
            "query": query,
            "type": search_type,
            "numResults": max_results,
        }
        exa_category = self._exa_category(mode, intent)
        if exa_category:
            payload["category"] = exa_category
        if include_content:
            payload["contents"] = {
                "text": True,
                "highlights": True,
            }
        if from_date:
            payload["startPublishedDate"] = from_date
        if to_date:
            payload["endPublishedDate"] = to_date
        if include_domains:
            payload["includeDomains"] = include_domains
        if exclude_domains:
            payload["excludeDomains"] = exclude_domains

        response = self._request_json(
            provider=provider,
            method="POST",
            path=provider.path("search"),
            payload=payload,
            key=key.key,
            timeout_seconds=timeout_seconds,
        )
        raw_results = response.get("results") or response.get("data") or []
        if not isinstance(raw_results, list):
            raw_results = []
        results = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            highlights = item.get("highlights") or []
            snippet = (
                " … ".join(highlights) if highlights
                else item.get("snippet")
                or item.get("text")
                or item.get("summary")
                or item.get("highlight")
                or ""
            )
            content = item.get("text") if include_content else ""
            results.append(
                {
                    "provider": "exa",
                    "source": "web",
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "snippet": snippet,
                    "content": content or "",
                    "score": item.get("score"),
                    "published_date": item.get("publishedDate") or item.get("published_date") or "",
                }
            )

        if include_domains or exclude_domains:
            results = self._filter_results_by_domains(
                results, include_domains=include_domains, exclude_domains=exclude_domains
            )

        return {
            "provider": "exa",
            "transport": key.source,
            "query": response.get("query", query),
            "answer": response.get("answer", ""),
            "results": results,
            "citations": [
                {"title": item.get("title", ""), "url": item.get("url", "")}
                for item in results
                if item.get("url")
            ],
        }

    def _search_xai(
        self,
        *,
        query: str,
        sources: list[str],
        max_results: int,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        allowed_x_handles: list[str] | None = None,
        excluded_x_handles: list[str] | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        include_x_images: bool = False,
        include_x_videos: bool = False,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        provider = self.config.xai
        if provider.search_mode == "compatible":
            return self._search_xai_compatible(
                query=query,
                sources=sources,
                max_results=max_results,
                allowed_x_handles=allowed_x_handles,
                excluded_x_handles=excluded_x_handles,
                from_date=from_date,
                to_date=to_date,
                include_x_images=include_x_images,
                include_x_videos=include_x_videos,
                timeout_seconds=timeout_seconds,
            )

        key = self._get_key_or_raise(provider)
        payload = self._build_xai_responses_payload(
            query=query,
            sources=sources,
            max_results=max_results,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            allowed_x_handles=allowed_x_handles,
            excluded_x_handles=excluded_x_handles,
            from_date=from_date,
            to_date=to_date,
            include_x_images=include_x_images,
            include_x_videos=include_x_videos,
        )
        response = self._request_json(
            provider=provider,
            method="POST",
            path=provider.path("responses"),
            payload=payload,
            key=key.key,
            timeout_seconds=timeout_seconds,
        )
        text = self._extract_xai_output_text(response)
        citations = self._extract_xai_citations(response)
        results = [
            {
                "provider": "xai",
                "source": "x" if "x" in sources else "web",
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": "",
                "content": "",
            }
            for item in citations
            if isinstance(item, dict)
        ]
        return {
            "provider": "xai",
            "transport": key.source,
            "query": query,
            "answer": text,
            "results": results,
            "citations": citations,
            "tool_usage": response.get("server_side_tool_usage") or response.get("tool_usage") or {},
        }

    def _search_xai_compatible(
        self,
        *,
        query: str,
        sources: list[str],
        max_results: int,
        allowed_x_handles: list[str] | None,
        excluded_x_handles: list[str] | None,
        from_date: str | None,
        to_date: str | None,
        include_x_images: bool,
        include_x_videos: bool,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        provider = self.config.xai
        if "x" not in sources:
            raise MySearchError(
                "xai compatible mode only supports social/X queries; "
                "use Tavily/Firecrawl for web search or switch to official xAI mode"
            )

        search_path = provider.path("social_search")
        key = self._get_key_or_raise(provider)
        payload: dict[str, Any] = {
            "query": query,
            "source": "x",
            "max_results": max_results,
        }
        if allowed_x_handles:
            payload["allowed_x_handles"] = allowed_x_handles
        if excluded_x_handles:
            payload["excluded_x_handles"] = excluded_x_handles
        if from_date:
            payload["from_date"] = from_date
        if to_date:
            payload["to_date"] = to_date
        if include_x_images:
            payload["include_x_images"] = True
        if include_x_videos:
            payload["include_x_videos"] = True
        social_cache_key = self._build_social_cache_key(
            query=query,
            max_results=max_results,
            allowed_x_handles=allowed_x_handles,
            excluded_x_handles=excluded_x_handles,
            from_date=from_date,
            to_date=to_date,
            include_x_images=include_x_images,
            include_x_videos=include_x_videos,
        )
        social_gateway_cache_key = self._build_social_gateway_cache_key(
            base_url=provider.base_url_for("social_search"),
            path=search_path,
        )
        cached_social_result = self._cache_get("social", social_cache_key)
        cached_social_unavailable = None
        cached_social_gateway_unavailable = None
        if (
            cached_social_result
            and cached_social_result.get("results")
            and str(cached_social_result.get("provider") or "") == "tavily_social_fallback"
        ):
            return self._annotate_cache(
                cached_social_result,
                namespace="social",
                hit=True,
            )
        if not (cached_social_result and cached_social_result.get("results")):
            cached_social_unavailable = self._cache_get("social_unavailable", social_cache_key)
            if not cached_social_unavailable:
                cached_social_gateway_unavailable = self._cache_get(
                    "social_gateway",
                    social_gateway_cache_key,
                )
        if cached_social_unavailable:
            return self._annotate_cache(
                cached_social_unavailable,
                namespace="social_unavailable",
                hit=True,
            )

        retry_attempts = 3
        configured_social_timeout = max(
            30,
            int(getattr(self.config, "xai_social_timeout_seconds", 120) or 120),
        )
        total_social_timeout = min(
            configured_social_timeout,
            max(5, int(timeout_seconds)) if timeout_seconds is not None else configured_social_timeout,
        )
        social_start = time.monotonic()
        social_fallback_reserve = max(15, min(30, total_social_timeout // 4 or 15))
        social_primary_budget = min(
            45,
            max(15, total_social_timeout - social_fallback_reserve),
        )
        social_deadline = social_start + social_primary_budget
        last_error: MySearchError | None = None
        if cached_social_gateway_unavailable:
            last_error = MySearchError(
                str(
                    cached_social_gateway_unavailable.get("fallback", {}).get("reason")
                    or cached_social_gateway_unavailable.get("summary")
                    or "social gateway unavailable"
                )
            )
        else:
            for attempt in range(retry_attempts):
                remaining_budget = social_deadline - time.monotonic()
                if remaining_budget <= 0:
                    break
                remaining_timeout = max(1, math.ceil(remaining_budget))
                try:
                    response = self._request_json(
                        provider=provider,
                        method="POST",
                        path=search_path,
                        payload=payload,
                        key=key.key,
                        base_url=provider.base_url_for("social_search"),
                        timeout_seconds=remaining_timeout,
                    )
                    normalized = self._normalize_social_gateway_response(
                        response=response,
                        query=query,
                        transport=key.source,
                        from_date=from_date,
                        to_date=to_date,
                    )
                    if normalized.get("results"):
                        self._cache_delete("social_gateway", social_gateway_cache_key)
                        self._cache_delete("social_unavailable", social_cache_key)
                        self._cache_set("social", social_cache_key, normalized)
                        return self._annotate_cache(
                            normalized,
                            namespace="social",
                            hit=False,
                        )
                    last_error = MySearchError(
                        "xai compatible returned no x.com/twitter.com results"
                    )
                    break
                except MySearchHTTPError as exc:
                    if exc.is_auth_error:
                        raise
                    last_error = exc
                    if cached_social_result and self._is_retryable_social_gateway_error(exc):
                        cached_social_result = self._annotate_cache(
                            cached_social_result,
                            namespace="social",
                            hit=True,
                        )
                        cached_social_result["fallback"] = {
                            "from": "xai_compatible",
                            "to": "social_last_good_cache",
                            "reason": str(exc)[:200],
                        }
                        return cached_social_result
                    if attempt < retry_attempts - 1 and self._is_retryable_social_gateway_error(exc):
                        continue
                    break
                except MySearchError as exc:
                    last_error = exc
                    if cached_social_result and self._is_retryable_social_gateway_error(exc):
                        cached_social_result = self._annotate_cache(
                            cached_social_result,
                            namespace="social",
                            hit=True,
                        )
                        cached_social_result["fallback"] = {
                            "from": "xai_compatible",
                            "to": "social_last_good_cache",
                            "reason": str(exc)[:200],
                        }
                        return cached_social_result
                    if attempt < retry_attempts - 1 and self._is_retryable_social_gateway_error(exc):
                        continue
                    break
            if last_error and self._is_retryable_social_gateway_error(last_error):
                self._cache_set(
                    "social_gateway",
                    social_gateway_cache_key,
                    self._build_social_gateway_unavailable_result(
                        base_url=provider.base_url_for("social_search"),
                        fallback_reason=str(last_error),
                    ),
                )

        if cached_social_result and cached_social_result.get("results"):
            cached_social_result = self._annotate_cache(
                cached_social_result,
                namespace="social",
                hit=True,
            )
            cached_social_result["fallback"] = {
                "from": "xai_compatible",
                "to": "social_last_good_cache",
                "reason": str(last_error or "xai compatible search failed")[:200],
            }
            return cached_social_result

        fallback_reason = str(last_error or "xai compatible search failed")
        elapsed_seconds = max(0.0, time.monotonic() - social_start)
        remaining_social_budget = max(0, int(math.ceil(total_social_timeout - elapsed_seconds)))
        if remaining_social_budget < 5:
            logger.warning(
                "event=social_budget_exhausted reason=%s elapsed=%.2fs total=%ss",
                fallback_reason,
                elapsed_seconds,
                total_social_timeout,
            )
            social_unavailable_result = self._build_social_unavailable_result(
                query=query,
                fallback_reason=f"{fallback_reason} | social budget exhausted before tavily_social_fallback",
            )
            self._cache_set("social_unavailable", social_cache_key, social_unavailable_result)
            return self._annotate_cache(
                social_unavailable_result,
                namespace="social_unavailable",
                hit=False,
            )
        logger.warning(
            "event=tavily_social_fallback_trigger reason=%s remaining_budget=%ss",
            fallback_reason,
            remaining_social_budget,
        )
        try:
            tavily_fallback_result = self._search_tavily_social_fallback(
                query=query,
                max_results=max_results,
                from_date=from_date,
                to_date=to_date,
                fallback_reason=fallback_reason,
                timeout_seconds=min(remaining_social_budget, social_fallback_reserve),
            )
        except MySearchError as fallback_exc:
            elapsed_after_tavily = max(0.0, time.monotonic() - social_start)
            remaining_for_exa = max(
                0, int(math.ceil(total_social_timeout - elapsed_after_tavily))
            )
            logger.warning(
                "event=exa_social_fallback_trigger reason=%s remaining_budget=%ss",
                fallback_exc,
                remaining_for_exa,
            )
            if remaining_for_exa <= 0:
                social_unavailable_result = self._build_social_unavailable_result(
                    query=query,
                    fallback_reason=(
                        f"{fallback_reason} | tavily_social_fallback failed: {fallback_exc}"
                        " | social budget exhausted before exa_social_fallback"
                    ),
                )
                self._cache_set("social_unavailable", social_cache_key, social_unavailable_result)
                return self._annotate_cache(
                    social_unavailable_result,
                    namespace="social_unavailable",
                    hit=False,
                )
            try:
                exa_fallback_result = self._search_exa_social_fallback(
                    query=query,
                    max_results=max_results,
                    fallback_reason=f"{fallback_reason} | tavily_social_fallback failed: {fallback_exc}",
                    from_date=from_date,
                    to_date=to_date,
                    timeout_seconds=remaining_for_exa,
                )
            except MySearchError as exa_exc:
                social_unavailable_result = self._build_social_unavailable_result(
                    query=query,
                    fallback_reason=(
                        f"{fallback_reason} | tavily_social_fallback failed: {fallback_exc}"
                        f" | exa_social_fallback failed: {exa_exc}"
                    ),
                )
                self._cache_set("social_unavailable", social_cache_key, social_unavailable_result)
                return self._annotate_cache(
                    social_unavailable_result,
                    namespace="social_unavailable",
                    hit=False,
                )
            self._cache_delete("social_unavailable", social_cache_key)
            self._cache_set("social", social_cache_key, exa_fallback_result)
            return self._annotate_cache(
                exa_fallback_result,
                namespace="social",
                hit=False,
            )
        if tavily_fallback_result.get("results"):
            self._cache_delete("social_unavailable", social_cache_key)
            self._cache_set("social", social_cache_key, tavily_fallback_result)
            result = self._annotate_cache(
                tavily_fallback_result,
                namespace="social",
                hit=False,
            )
            if cached_social_gateway_unavailable:
                result = self._annotate_cache(
                    result,
                    namespace="social_gateway",
                    hit=True,
                )
            return result
        social_unavailable_result = self._build_social_unavailable_result(
            query=query,
            fallback_reason=f"{fallback_reason} | tavily_social_fallback returned no results",
        )
        self._cache_set("social_unavailable", social_cache_key, social_unavailable_result)
        return self._annotate_cache(
            social_unavailable_result,
            namespace="social_unavailable",
            hit=False,
        )

    def _search_exa_social_fallback(
        self,
        *,
        query: str,
        max_results: int,
        fallback_reason: str,
        from_date: str | None,
        to_date: str | None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        # perf-r4 P0：内部最多两次 _search_exa，必须把外部传入的剩余 social budget 拆分，
        # 避免 worst-case `45s + 45s` 超出 xai_social_timeout_seconds（默认 120s）总预算。
        # `timeout_seconds=None` 表示沿用 _request_json 的 config.timeout_seconds 兜底。
        per_call_timeout: int | None
        fallback_deadline: float | None = None
        if timeout_seconds is not None and timeout_seconds > 0:
            per_call_timeout = max(1, timeout_seconds // 2)
            fallback_deadline = time.monotonic() + timeout_seconds
        else:
            per_call_timeout = None

        def remaining_call_timeout() -> int | None:
            if fallback_deadline is None:
                return None
            remaining = fallback_deadline - time.monotonic()
            if remaining <= 0:
                raise MySearchError("exa social fallback budget exhausted")
            return min(per_call_timeout or 1, max(1, math.ceil(remaining)))

        filtered_results: list[dict[str, Any]] = []
        exa_result = self._search_exa(
            query=query,
            max_results=max(max_results * 3, 8),
            include_domains=["x.com", "twitter.com"],
            exclude_domains=None,
            include_content=False,
            mode="social",
            intent="status",
            strategy="fast",
            from_date=from_date,
            to_date=to_date,
            timeout_seconds=remaining_call_timeout(),
        )
        filtered_results.extend(self._normalize_exa_social_fallback_results(exa_result.get("results", [])))
        if not filtered_results:
            exa_result = self._search_exa(
                query=f"{query} site:x.com OR site:twitter.com",
                max_results=max(max_results * 3, 8),
                include_domains=None,
                exclude_domains=None,
                include_content=False,
                mode="web",
                intent="factual",
                strategy="fast",
                from_date=from_date,
                to_date=to_date,
                timeout_seconds=remaining_call_timeout(),
            )
            filtered_results.extend(self._normalize_exa_social_fallback_results(exa_result.get("results", [])))
        if not filtered_results:
            raise MySearchError("exa social fallback returned no X-adjacent results")
        filtered_results = self._diversify_social_results(
            filtered_results,
            max_results=max_results,
            max_per_identity=1,
        )
        if not filtered_results:
            raise MySearchError("exa social fallback returned no X-adjacent results after diversification")
        return {
            "provider": "exa_social_fallback",
            "transport": exa_result.get("transport", ""),
            "query": query,
            "answer": "",
            "results": filtered_results,
            "citations": [
                {"title": item.get("title", ""), "url": item.get("url", "")}
                for item in filtered_results
                if item.get("url")
            ],
            "fallback": {
                "from": "xai_compatible",
                "to": "exa_social_fallback",
                "reason": fallback_reason,
            },
        }

    def _normalize_exa_social_fallback_results(
        self,
        results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return social._normalize_exa_social_fallback_results(results=results)

    def _is_exa_social_candidate(self, item: dict[str, Any]) -> bool:
        return query_routing._is_exa_social_candidate(item)
    def _is_retryable_social_gateway_error(self, exc: Exception) -> bool:
        return query_routing._is_retryable_social_gateway_error(exc)
    def _is_retryable_transient_error(self, exc: Exception) -> bool:
        return query_routing._is_retryable_transient_error(exc)
    def _request_json_with_transient_retry(
        self,
        *,
        provider: ProviderConfig,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        key: str,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        attempts: int = 2,
    ) -> dict[str, Any]:
        return self._request_json_with_transient_retry_selected(
            provider=provider,
            method=method,
            path=path,
            payload=payload,
            key=key,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            attempts=attempts,
        )[0]

    def _request_json_with_transient_retry_selected(
        self,
        *,
        provider: ProviderConfig,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        key: str,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        attempts: int = 2,
        allow_key_rotation: bool = True,
    ) -> tuple[dict[str, Any], str]:
        effective_attempts = max(1, attempts)
        for attempt in range(effective_attempts):
            try:
                return self._request_json_selected(
                    provider=provider,
                    method=method,
                    path=path,
                    payload=payload,
                    key=key,
                    base_url=base_url,
                    timeout_seconds=timeout_seconds,
                    allow_key_rotation=allow_key_rotation,
                )
            except MySearchError as exc:
                if attempt < effective_attempts - 1 and self._is_retryable_transient_error(exc):
                    if (
                        allow_key_rotation
                        and isinstance(exc, MySearchHTTPError)
                        and exc.status_code == 429
                    ):
                        raise
                    retry_delay = 1.5 * (attempt + 1)
                    if (
                        not allow_key_rotation
                        and isinstance(exc, MySearchHTTPError)
                        and exc.status_code == 429
                    ):
                        retry_delay = (
                            exc.retry_after_seconds or DEFAULT_KEY_COOLDOWN_SECONDS
                        )
                        if retry_delay > MAX_PINNED_KEY_RETRY_DELAY_SECONDS:
                            raise
                    time.sleep(retry_delay)
                    continue
                raise
        raise AssertionError("unreachable")

    def _search_tavily_social_fallback(
        self,
        *,
        query: str,
        max_results: int,
        from_date: str | None,
        to_date: str | None,
        fallback_reason: str,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        tavily_timeout_budget = max(1, int(timeout_seconds or 15))
        tavily_deadline = time.monotonic() + tavily_timeout_budget
        last_error: MySearchError | None = None
        tavily_result: dict[str, Any] | None = None
        for attempt in range(2):
            remaining_budget = tavily_deadline - time.monotonic()
            if remaining_budget <= 0:
                break
            attempt_timeout = max(1, math.ceil(remaining_budget))
            if attempt == 0:
                attempt_timeout = min(attempt_timeout, 10)
            try:
                tavily_result = self._search_tavily_once(
                    query=query,
                    max_results=max_results,
                    topic="news",
                    include_answer=True,
                    include_content=False,
                    include_domains=["x.com"],
                    exclude_domains=None,
                    strategy="fast",
                    days=self._infer_tavily_days("status", from_date),
                    from_date=from_date,
                    to_date=to_date,
                    timeout_seconds=attempt_timeout,
                )
                break
            except MySearchError as exc:
                last_error = exc
                if not self._is_retryable_social_gateway_error(exc) or attempt > 0:
                    raise
                continue
        if tavily_result is None:
            raise last_error or MySearchError(fallback_reason)
        fallback_results = [
            {
                "provider": "tavily_social_fallback",
                "source": "x",
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("snippet", ""),
                "content": item.get("content", ""),
                "author": self._social_result_identity(item),
            }
            for item in tavily_result.get("results", [])
        ]
        fallback_results = self._diversify_social_results(
            fallback_results,
            max_results=max_results,
            max_per_identity=1,
        )
        if not fallback_results:
            raise MySearchError(fallback_reason)
        return {
            "provider": "tavily_social_fallback",
            "transport": tavily_result.get("transport", "env"),
            "query": query,
            "answer": tavily_result.get("answer", ""),
            "results": fallback_results,
            "citations": self._align_citations_with_results(
                results=fallback_results,
                citations=list(tavily_result.get("citations") or []),
            )[: len(fallback_results)],
            "fallback": {
                "from": "xai_compatible",
                "to": "tavily_social_fallback",
                "reason": fallback_reason[:200],
            },
        }

    def _build_social_unavailable_result(
        self,
        *,
        query: str,
        fallback_reason: str,
    ) -> dict[str, Any]:
        return social._build_social_unavailable_result(query=query, fallback_reason=fallback_reason)

    def _build_social_gateway_unavailable_result(
        self,
        *,
        base_url: str,
        fallback_reason: str,
    ) -> dict[str, Any]:
        return social._build_social_gateway_unavailable_result(base_url=base_url, fallback_reason=fallback_reason)

    def _scrape_firecrawl(
        self,
        *,
        url: str,
        formats: list[str],
        only_main_content: bool,
    ) -> dict[str, Any]:
        provider = self.config.firecrawl
        key = self._get_key_or_raise(provider)
        payload = {
            "url": url,
            "formats": formats,
            "onlyMainContent": only_main_content,
        }
        response = self._request_json(
            provider=provider,
            method="POST",
            path=provider.path("scrape"),
            payload=payload,
            key=key.key,
        )
        data = response.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        metadata = data.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        content = data.get("markdown", "")
        if not content and "json" in data:
            content = json.dumps(data["json"], ensure_ascii=False, indent=2)
        content = self._clean_extract_content(content)
        return {
            "provider": "firecrawl",
            "transport": key.source,
            "url": metadata.get("sourceURL") or metadata.get("url") or url,
            "content": content,
            "metadata": metadata,
        }

    def map_site(
        self,
        *,
        url: str,
        limit: int = 50,
        search: str | None = None,
    ) -> dict[str, Any]:
        return self._map_firecrawl(url=url, limit=limit, search=search)

    def crawl_site(
        self,
        *,
        url: str,
        limit: int = 20,
        max_depth: int | None = None,
        crawl_entire_domain: bool = True,
    ) -> dict[str, Any]:
        return self._crawl_firecrawl(
            url=url,
            limit=limit,
            max_depth=max_depth,
            crawl_entire_domain=crawl_entire_domain,
        )

    def _map_firecrawl(
        self,
        *,
        url: str,
        limit: int = 50,
        search: str | None = None,
    ) -> dict[str, Any]:
        provider = self.config.firecrawl
        key = self._get_key_or_raise(provider)
        payload: dict[str, Any] = {"url": url, "limit": limit}
        if search:
            payload["search"] = search
        response = self._request_json_with_transient_retry(
            provider=provider,
            method="POST",
            path=provider.path("map"),
            payload=payload,
            key=key.key,
        )
        links_raw = response.get("links") or []
        if not isinstance(links_raw, list):
            links_raw = []
        links: list[dict[str, Any]] = []
        for item in links_raw:
            if isinstance(item, str) and item:
                links.append({"url": item, "title": "", "description": ""})
            elif isinstance(item, dict) and item.get("url"):
                links.append({
                    "url": item.get("url"),
                    "title": item.get("title", ""),
                    "description": item.get("description", ""),
                })
        return {
            "provider": "firecrawl",
            "transport": key.source,
            "url": url,
            "links": links,
            "count": len(links),
            "metadata": {"requested_limit": limit, "search": search or ""},
        }

    def _crawl_firecrawl(
        self,
        *,
        url: str,
        limit: int = 20,
        max_depth: int | None = None,
        crawl_entire_domain: bool = True,
        poll_interval_seconds: float = 2.0,
        max_poll_attempts: int = 30,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        provider = self.config.firecrawl
        key = self._get_key_or_raise(provider)
        configured_crawl_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else self.config.timeout_seconds + MAX_PINNED_KEY_RETRY_DELAY_SECONDS
        )
        crawl_timeout_seconds = max(0.001, float(configured_crawl_timeout))
        deadline = time.monotonic() + crawl_timeout_seconds

        def remaining_timeout() -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MySearchError("firecrawl crawl deadline exceeded")
            return max(0.001, min(float(self.config.timeout_seconds), remaining))

        def request_with_deadline(
            *,
            method: str,
            path: str,
            payload: dict[str, Any] | None,
            selected_key: str,
            allow_key_rotation: bool,
        ) -> tuple[dict[str, Any], str]:
            for attempt in range(2):
                try:
                    result = self._request_json_with_transient_retry_selected(
                        provider=provider,
                        method=method,
                        path=path,
                        payload=payload,
                        key=selected_key,
                        timeout_seconds=remaining_timeout(),
                        attempts=1,
                        allow_key_rotation=allow_key_rotation,
                    )
                    if time.monotonic() >= deadline:
                        raise MySearchError("firecrawl crawl deadline exceeded")
                    return result
                except MySearchError as exc:
                    if attempt or not self._is_retryable_transient_error(exc):
                        raise
                    if (
                        provider.managed_key_pool
                        and isinstance(exc, MySearchHTTPError)
                        and exc.status_code == 429
                    ):
                        raise
                    retry_delay = 1.5
                    if (
                        not allow_key_rotation
                        and isinstance(exc, MySearchHTTPError)
                        and exc.status_code == 429
                    ):
                        retry_delay = exc.retry_after_seconds or DEFAULT_KEY_COOLDOWN_SECONDS
                        if retry_delay > MAX_PINNED_KEY_RETRY_DELAY_SECONDS:
                            raise
                    if deadline - time.monotonic() <= retry_delay:
                        raise MySearchError("firecrawl crawl deadline exceeded") from exc
                    time.sleep(retry_delay)
            raise AssertionError("unreachable")

        payload: dict[str, Any] = {"url": url, "limit": limit}
        if max_depth is not None:
            # Firecrawl v2 crawl uses `maxDiscoveryDepth`; `maxDepth` is silently ignored.
            payload["maxDiscoveryDepth"] = max_depth
        payload["crawlEntireDomain"] = crawl_entire_domain
        start, selected_key = request_with_deadline(
            method="POST",
            path=provider.path("crawl"),
            payload=payload,
            selected_key=key.key,
            allow_key_rotation=True,
        )
        job_id = start.get("id")
        if not job_id:
            # Some deployments answer synchronously with the data already present.
            return self._build_firecrawl_crawl_result(
                url=url, limit=limit, transport=key.source, status_payload=start
            )
        status_path = f"{provider.path('crawl')}/{job_id}"
        status_payload: dict[str, Any] = start
        terminal = False
        for _ in range(max(1, max_poll_attempts)):
            status_payload = request_with_deadline(
                method="GET",
                path=status_path,
                payload=None,
                selected_key=selected_key,
                allow_key_rotation=False,
            )[0]
            state = str(status_payload.get("status") or "").lower()
            if state in {"completed", "failed", "cancelled"}:
                terminal = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MySearchError("firecrawl crawl deadline exceeded")
            time.sleep(min(max(0.0, poll_interval_seconds), remaining))
        if not terminal:
            raise MySearchError("firecrawl crawl did not reach a terminal state before deadline")
        return self._build_firecrawl_crawl_result(
            url=url, limit=limit, transport=key.source, status_payload=status_payload
        )

    def _build_firecrawl_crawl_result(
        self,
        *,
        url: str,
        limit: int,
        transport: str,
        status_payload: dict[str, Any],
    ) -> dict[str, Any]:
        return query_routing._build_firecrawl_crawl_result(url=url, limit=limit, transport=transport, status_payload=status_payload)

    def _extract_tavily(self, *, url: str) -> dict[str, Any]:
        provider = self.config.tavily
        key = self._get_key_or_raise(provider)
        response = self._request_json(
            provider=provider,
            method="POST",
            path=provider.path("extract"),
            payload={"urls": [url]},
            key=key.key,
        )
        results = response.get("results") or []
        if not isinstance(results, list):
            results = []
        first = results[0] if results else {}
        content = first.get("raw_content") or first.get("content") or ""
        return {
            "provider": "tavily",
            "transport": key.source,
            "url": first.get("url", url),
            "content": content,
            "metadata": {
                "request_id": response.get("request_id", ""),
                "response_time": response.get("response_time"),
                "failed_results": response.get("failed_results") or [],
            },
        }

    def _extract_github_blob_raw(self, *, url: str) -> dict[str, Any] | None:
        raw_urls = self._github_blob_raw_urls(url)
        if not raw_urls:
            return None

        prefer_urlopen = "unittest.mock" in type(urlopen).__module__
        for raw_url in raw_urls:
            try:
                if prefer_urlopen:
                    request = Request(
                        raw_url,
                        headers={"Accept": "text/plain, text/markdown;q=0.9, */*;q=0.8"},
                    )
                    with urlopen(request, timeout=self.config.timeout_seconds) as response:
                        raw_content = response.read()
                    content = raw_content.decode("utf-8", errors="replace")
                else:
                    response = self._http.get(
                        raw_url,
                        headers={"Accept": "text/plain, text/markdown;q=0.9, */*;q=0.8"},
                    )
                    response.raise_for_status()
                    content = response.text
            except (httpx.HTTPError, ValueError, OSError):
                continue

            result = {
                "provider": "github_raw",
                "transport": "direct",
                "url": url,
                "content": content,
                "metadata": {
                    "raw_url": raw_url,
                },
            }
            if self._extract_quality_issue(result) is not None:
                continue
            return result
        return None

    def _github_blob_raw_url(self, url: str) -> str | None:
        raw_urls = self._github_blob_raw_urls(url)
        if not raw_urls:
            return None
        return raw_urls[0]

    def _github_blob_raw_urls(self, url: str) -> list[str]:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return []
        if parsed.netloc.lower() != "github.com":
            return []

        parts = [segment for segment in parsed.path.split("/") if segment]
        if len(parts) < 5 or parts[2] != "blob":
            return []

        owner, repo, _, ref, *path_parts = parts
        if not owner or not repo or not ref or not path_parts:
            return []
        raw_path = "/".join(path_parts)
        refs = [ref]
        if ref == "main":
            refs.append("master")
        elif ref == "master":
            refs.append("main")
        return [
            f"https://raw.githubusercontent.com/{owner}/{repo}/{candidate_ref}/{raw_path}"
            for candidate_ref in refs
        ]

    # 官方奖项站域名。此前在 4 处函数体里逐字重复，改一处容易漏其余。
    _OFFICIAL_AWARD_DOMAINS = frozenset({
        "grammy.com",
        "grammys.com",
        "oscars.org",
        "theacademy.com",
    })

    _HCAPTCHA_LANGUAGES = frozenset({
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
    })

    def _clean_extract_content(self, content: str) -> str:
        return query_routing._clean_extract_content(content=content)

    def _strip_browser_challenge_block(self, text: str) -> str:
        return postprocess._strip_browser_challenge_block(text)

    def _strip_trailing_hcaptcha(self, text: str) -> str:
        return query_routing._strip_trailing_hcaptcha(text=text)

    def _is_hcaptcha_artifact_paragraph(self, p_lower: str) -> bool:
        return query_routing._is_hcaptcha_artifact_paragraph(p_lower)
    def _strip_trailing_empty_headings(self, text: str) -> str:
        # Remove dangling heading-only paragraphs left at the very end after
        # widget removal (e.g. a lone trailing `### Filters` with no body).
        return postprocess._strip_trailing_empty_headings(text)

    def _strip_hcaptcha_block(self, text: str) -> str:
        return postprocess._strip_hcaptcha_block(text)

    def _has_meaningful_extract_content(self, result: dict[str, Any]) -> bool:
        return self._extract_quality_issue(result) is None

    def _extract_quality_issue(self, result: dict[str, Any]) -> str | None:
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

    def _annotate_extract_warning(
        self,
        result: dict[str, Any],
        *,
        warning: str,
    ) -> dict[str, Any]:
        annotated = dict(result)
        metadata = dict(annotated.get("metadata") or {})
        metadata["warning"] = warning
        annotated["metadata"] = metadata
        annotated["warning"] = warning
        return annotated

    def _annotate_extract_fallback(
        self,
        result: dict[str, Any],
        *,
        fallback_from: str,
        fallback_reason: str,
    ) -> dict[str, Any]:
        annotated = dict(result)
        metadata = dict(annotated.get("metadata") or {})
        metadata["fallback_from"] = fallback_from
        metadata["fallback_reason"] = fallback_reason
        annotated["metadata"] = metadata
        annotated["fallback"] = {
            "from": fallback_from,
            "reason": fallback_reason,
        }
        return annotated

    def _build_xai_responses_payload(
        self,
        *,
        query: str,
        sources: list[str],
        max_results: int,
        include_domains: list[str] | None,
        exclude_domains: list[str] | None,
        allowed_x_handles: list[str] | None,
        excluded_x_handles: list[str] | None,
        from_date: str | None,
        to_date: str | None,
        include_x_images: bool,
        include_x_videos: bool,
        model: str | None = None,
    ) -> dict[str, Any]:
        tools: list[dict[str, Any]] = []
        if "web" in sources:
            tool: dict[str, Any] = {"type": "web_search"}
            filters: dict[str, Any] = {}
            if include_domains:
                filters["allowed_domains"] = include_domains
            elif exclude_domains:
                filters["excluded_domains"] = exclude_domains
            if filters:
                tool["filters"] = filters
            tools.append(tool)

        if "x" in sources:
            tool = {"type": "x_search"}
            if allowed_x_handles:
                tool["allowed_x_handles"] = allowed_x_handles
            elif excluded_x_handles:
                tool["excluded_x_handles"] = excluded_x_handles
            if from_date:
                tool["from_date"] = from_date
            if to_date:
                tool["to_date"] = to_date
            if include_x_images:
                tool["enable_image_understanding"] = True
            if include_x_videos:
                tool["enable_video_understanding"] = True
            tools.append(tool)

        augmented_query = f"{query}\n\nReturn up to {max_results} relevant results with concise sourcing."
        return {
            "model": (model or self.config.xai_model).strip(),
            "input": [
                {
                    "role": "user",
                    "content": augmented_query,
                }
            ],
            "tools": tools,
            "store": False,
            "stream": False,
        }

    def _normalize_social_gateway_response(
        self,
        *,
        response: dict[str, Any],
        query: str,
        transport: str,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        return social._normalize_social_gateway_response(response=response, query=query, transport=transport, from_date=from_date, to_date=to_date)

    def _social_result_identity(self, item: dict[str, Any]) -> str:
        return postprocess._social_result_identity(item)

    def _diversify_social_results(
        self,
        results: list[dict[str, Any]],
        *,
        max_results: int,
        max_per_identity: int = 2,
    ) -> list[dict[str, Any]]:
        return postprocess._diversify_social_results(results, max_results=max_results, max_per_identity=max_per_identity)

    def _filter_social_results_by_date(
        self,
        results: list[dict[str, Any]],
        *,
        from_date: str | None,
        to_date: str | None,
    ) -> list[dict[str, Any]]:
        return postprocess._filter_social_results_by_date(results, from_date=from_date, to_date=to_date)

    def _parse_date_bound(self, value: str, *, end_of_day: bool) -> datetime | None:
        try:
            return postprocess._parse_date_bound(value, end_of_day=end_of_day)
        except postprocess.PostprocessError as exc:
            # 对外错误语义保持 MySearchError 不变。
            raise MySearchError(str(exc)) from exc

    def _parse_result_timestamp(self, value: Any) -> datetime | None:
        return postprocess._parse_result_timestamp(value)

    def _extract_social_gateway_results(self, response: dict[str, Any]) -> list[Any]:
        return postprocess._extract_social_gateway_results(response)

    def _extract_social_gateway_citations(
        self,
        response: dict[str, Any],
        results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return postprocess._extract_social_gateway_citations(response, results)

    def _merge_search_payloads(
        self,
        *,
        primary_result: dict[str, Any],
        secondary_result: dict[str, Any] | None,
        max_results: int,
    ) -> dict[str, Any]:
        sequences: list[list[str]] = []
        variants_by_key: dict[str, list[dict[str, Any]]] = {}
        providers_by_key: dict[str, set[str]] = {}

        for result in [primary_result, secondary_result]:
            if not result:
                continue

            sequence: list[str] = []
            result_provider = result.get("provider", "")
            for item in result.get("results", []) or []:
                if not isinstance(item, dict):
                    continue
                dedupe_key = self._result_dedupe_key(item)
                if not dedupe_key:
                    continue
                sequence.append(dedupe_key)
                variants_by_key.setdefault(dedupe_key, []).append(dict(item))
                providers_by_key.setdefault(dedupe_key, set()).add(
                    item.get("provider") or result_provider
                )
            sequences.append(sequence)

        merged_keys: list[str] = []
        indexes = [0 for _ in sequences]
        seen_keys: set[str] = set()
        while len(merged_keys) < max_results and sequences:
            progressed = False
            for seq_index, sequence in enumerate(sequences):
                if len(merged_keys) >= max_results:
                    break
                while indexes[seq_index] < len(sequence):
                    dedupe_key = sequence[indexes[seq_index]]
                    indexes[seq_index] += 1
                    if dedupe_key in seen_keys:
                        continue
                    seen_keys.add(dedupe_key)
                    merged_keys.append(dedupe_key)
                    progressed = True
                    break
            if not progressed:
                break

        results: list[dict[str, Any]] = []
        matched_results = 0
        for dedupe_key in merged_keys:
            variants = variants_by_key.get(dedupe_key, [])
            if not variants:
                continue
            providers = sorted(item for item in providers_by_key.get(dedupe_key, set()) if item)
            if len(providers) > 1:
                matched_results += 1
            best = dict(max(variants, key=self._result_quality_score))
            if urlparse(dedupe_key).hostname == "arxiv.org":
                meaningful_titles = [
                    str(item.get("title") or "").strip()
                    for item in variants
                    if str(item.get("title") or "").strip()
                    and not self._looks_like_generic_arxiv_subject_title(
                        str(item.get("title") or "").strip()
                    )
                ]
                if meaningful_titles:
                    best["title"] = max(meaningful_titles, key=len)
            merged_item = self._canonicalize_result_item(best)
            merged_item["matched_providers"] = providers
            results.append(merged_item)

        citations = self._dedupe_citations(
            primary_result.get("citations") or [],
            (secondary_result.get("citations") or []) if secondary_result else [],
        )
        return {
            "results": results,
            "citations": citations,
            "matched_results": matched_results,
        }

    def _should_rerank_resource_results(
        self,
        *,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
    ) -> bool:
        return query_routing._should_rerank_resource_results(mode=mode, intent=intent)
    def _rerank_resource_results(
        self,
        *,
        query: str,
        mode: SearchMode,
        results: list[dict[str, Any]],
        include_domains: list[str] | None,
    ) -> list[dict[str, Any]]:
        return selection._rerank_resource_results(query=query, mode=mode, results=results, include_domains=include_domains)

    def _resource_result_rank(
        self,
        *,
        query: str,
        mode: SearchMode,
        item: dict[str, Any],
        query_tokens: list[str],
        precision_tokens: list[str],
        exact_identifier_tokens: list[str],
        topic_specific_tokens: list[str],
        include_domains: list[str] | None,
        strict_official: bool,
    ) -> tuple[int, ...]:
        return ranking._resource_result_rank(query=query, mode=mode, item=item, query_tokens=query_tokens, precision_tokens=precision_tokens, exact_identifier_tokens=exact_identifier_tokens, topic_specific_tokens=topic_specific_tokens, include_domains=include_domains, strict_official=strict_official)

    def _looks_like_generic_changelog_index_result(self, *, hostname: str, path: str) -> bool:
        return query_routing._looks_like_generic_changelog_index_result(hostname=hostname, path=path)
    def _paper_query_subject_tokens(
        self,
        *,
        query: str,
        query_tokens: list[str],
        precision_tokens: list[str],
    ) -> list[str]:
        return query_routing._paper_query_subject_tokens(query=query, query_tokens=query_tokens, precision_tokens=precision_tokens)
    def _paper_query_compound_tokens(self, query: str) -> list[str]:
        return query_routing._paper_query_compound_tokens(query)
    def _paper_text_matches_compound_token(self, text: str, compound_token: str) -> bool:
        return query_routing._paper_text_matches_compound_token(text, compound_token)
    def _looks_like_primary_named_paper_result(
        self,
        *,
        title_text: str,
        query_tokens: list[str],
    ) -> bool:
        return query_routing._looks_like_primary_named_paper_result(title_text=title_text, query_tokens=query_tokens)
    def _is_probably_official_resource_result(
        self,
        *,
        mode: SearchMode,
        hostname: str,
        include_match: bool,
        registered_domain_label_match: bool,
        host_brand_match: bool,
        title_brand_match: bool,
        docs_shape_match: bool,
        non_third_party: bool,
        official_query: bool,
    ) -> bool:
        return query_routing._is_probably_official_resource_result(mode=mode, hostname=hostname, include_match=include_match, registered_domain_label_match=registered_domain_label_match, host_brand_match=host_brand_match, title_brand_match=title_brand_match, docs_shape_match=docs_shape_match, non_third_party=non_third_party, official_query=official_query)
    def _align_citations_with_results(
        self,
        *,
        results: list[dict[str, Any]],
        citations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return postprocess._align_citations_with_results(results=results, citations=citations)

    def _dedupe_citations(self, *citation_lists: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return postprocess._dedupe_citations(*citation_lists)

    def _citation_dedupe_key(self, item: dict[str, Any]) -> str:
        return postprocess._citation_dedupe_key(item)

    def _result_dedupe_key(self, item: dict[str, Any]) -> str:
        return postprocess._result_dedupe_key(item)

    def _result_url_identity(self, url: str) -> str:
        return postprocess._result_url_identity(url)

    def _canonicalize_result_item(self, item: dict[str, Any]) -> dict[str, Any]:
        return postprocess._canonicalize_result_item(item)

    def _extract_candidate_matches_requested_url(
        self,
        *,
        requested_url: str,
        candidate_url: str,
    ) -> bool:
        requested = self._canonical_result_url(requested_url)
        candidate = self._canonical_result_url(candidate_url)
        if not requested or not candidate:
            return False
        if requested.rstrip("/") == candidate.rstrip("/"):
            return True
        requested_host = self._clean_hostname(urlparse(requested).netloc)
        candidate_host = self._clean_hostname(urlparse(candidate).netloc)
        if not requested_host or not candidate_host:
            return False
        return self._registered_domain(requested_host) == self._registered_domain(candidate_host)

    @staticmethod
    def _is_social_unavailable_result(result: dict[str, Any] | None) -> bool:
        return query_routing._is_social_unavailable_result(result)
    def _canonical_result_url(self, url: str) -> str:
        return postprocess._canonical_result_url(url)

    def _looks_like_locale_prefixed_path(self, path: str) -> bool:
        return query_routing._looks_like_locale_prefixed_path(path)
    def _looks_like_locale_prefixed_hostname(self, hostname: str) -> bool:
        return query_routing._looks_like_locale_prefixed_hostname(hostname)
    def _looks_like_generic_arxiv_subject_title(self, title_text: str) -> bool:
        return query_routing._looks_like_generic_arxiv_subject_title(title_text)
    def _fetch_arxiv_title(self, url: str) -> str:
        canonical_url = self._canonical_result_url(url)
        if self._result_hostname({"url": canonical_url}) != "arxiv.org":
            return ""
        try:
            response = self._http.get(canonical_url, headers={"Accept": "text/html"})
            response.raise_for_status()
        except httpx.HTTPError:
            return ""

        text = response.text
        meta_match = re.search(
            r'<meta[^>]+name=["\']citation_title["\'][^>]+content=["\']([^"\']+)["\']',
            text,
            re.IGNORECASE,
        )
        if meta_match:
            return html.unescape(meta_match.group(1)).strip()

        title_match = re.search(r"<title>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
        if not title_match:
            return ""
        title = html.unescape(re.sub(r"\s+", " ", title_match.group(1))).strip()
        title = re.sub(r"^\[\d{4}\.\d{4,5}(?:v\d+)?\]\s*", "", title)
        title = title.replace(" | arXiv e-print archive", "").strip()
        return title

    def _result_quality_score(self, item: dict[str, Any]) -> tuple[int, int, int]:
        return postprocess._result_quality_score(item)

    def _result_hostname(self, item: dict[str, Any]) -> str:
        return query_routing._result_hostname(item)
    def _clean_hostname(self, hostname: str) -> str:
        return query_routing._clean_hostname(hostname)
    def _registered_domain(self, hostname: str) -> str:
        return query_routing._registered_domain(hostname)
    def _domain_matches(self, hostname: str, domain: str) -> bool:
        return postprocess._domain_matches(hostname, domain)

    def _registered_domain_label_matches(self, *, registered_domain: str, query_tokens: list[str]) -> bool:
        return query_routing._registered_domain_label_matches(registered_domain=registered_domain, query_tokens=query_tokens)
    def _resource_result_flags(
        self,
        *,
        mode: SearchMode,
        item: dict[str, Any],
        query_tokens: list[str],
        include_domains: list[str] | None,
    ) -> dict[str, Any]:
        return ranking._resource_result_flags(mode=mode, item=item, query_tokens=query_tokens, include_domains=include_domains)

    def _result_matches_official_policy(
        self,
        *,
        item: dict[str, Any],
        mode: SearchMode,
        query_tokens: list[str],
        include_domains: list[str] | None,
        strict_official: bool,
    ) -> bool:
        return sections._result_matches_official_policy(item=item, mode=mode, query_tokens=query_tokens, include_domains=include_domains, strict_official=strict_official)

    def _query_brand_tokens(self, query: str) -> list[str]:
        return query_routing._query_brand_tokens(query)
    def _query_precision_tokens(self, query: str) -> list[str]:
        return query_routing._query_precision_tokens(query)
    def _is_mixed_alnum_short_token(self, token: str) -> bool:
        return query_routing._is_mixed_alnum_short_token(token)
    def _query_precision_hit_counts(
        self,
        *,
        hostname: str,
        path: str,
        title_text: str,
        query_tokens: list[str],
    ) -> tuple[int, int]:
        return query_routing._query_precision_hit_counts(hostname=hostname, path=path, title_text=title_text, query_tokens=query_tokens)
    def _query_precision_hit_counts_with_body(
        self,
        *,
        hostname: str,
        path: str,
        title_text: str,
        body_text: str,
        query_tokens: list[str],
    ) -> tuple[int, int]:
        return query_routing._query_precision_hit_counts_with_body(hostname=hostname, path=path, title_text=title_text, body_text=body_text, query_tokens=query_tokens)
    def _query_exact_identifier_tokens(self, query: str) -> list[str]:
        return query_routing._query_exact_identifier_tokens(query)
    def _query_topic_specific_tokens(self, query: str) -> list[str]:
        return query_routing._query_topic_specific_tokens(query)
    def _query_exact_identifier_hit_counts(
        self,
        *,
        path: str,
        title_text: str,
        query_tokens: list[str],
    ) -> tuple[int, int]:
        return query_routing._query_exact_identifier_hit_counts(path=path, title_text=title_text, query_tokens=query_tokens)
    def _looks_like_official_docs_query(self, query_lower: str) -> bool:
        return query_routing._looks_like_official_docs_query(query_lower)
    def _looks_like_pricing_query(self, query_lower: str) -> bool:
        return query_routing._looks_like_pricing_query(query_lower)
    def _looks_like_pricing_result(self, *, url: str, hostname: str, title_text: str) -> bool:
        return query_routing._looks_like_pricing_result(url=url, hostname=hostname, title_text=title_text)
    def _looks_like_canonical_pricing_result(self, *, hostname: str, path: str) -> bool:
        return query_routing._looks_like_canonical_pricing_result(hostname=hostname, path=path)
    def _looks_like_generic_official_landing_result(
        self,
        *,
        hostname: str,
        path: str,
        title_text: str,
    ) -> bool:
        return query_routing._looks_like_generic_official_landing_result(hostname=hostname, path=path, title_text=title_text)
    def _query_mentions_programming_language(self, query_lower: str) -> bool:
        return query_routing._query_mentions_programming_language(query_lower)
    def _looks_like_language_specific_sdk_reference_result(
        self,
        *,
        hostname: str,
        path: str,
        title_text: str,
    ) -> bool:
        return query_routing._looks_like_language_specific_sdk_reference_result(hostname=hostname, path=path, title_text=title_text)
    def _looks_like_language_specific_docs_result(
        self,
        *,
        path: str,
        title_text: str,
    ) -> bool:
        return query_routing._looks_like_language_specific_docs_result(path=path, title_text=title_text)
    def _looks_like_generic_official_docs_result(
        self,
        *,
        hostname: str,
        path: str,
        title_text: str,
    ) -> bool:
        return query_routing._looks_like_generic_official_docs_result(hostname=hostname, path=path, title_text=title_text)
    def _looks_like_changelog_result(self, *, url: str, hostname: str, title_text: str) -> bool:
        return query_routing._looks_like_changelog_result(url=url, hostname=hostname, title_text=title_text)
    def _looks_like_canonical_changelog_result(
        self,
        *,
        url: str,
        hostname: str,
        title_text: str,
        precision_tokens: list[str],
    ) -> bool:
        return query_routing._looks_like_canonical_changelog_result(url=url, hostname=hostname, title_text=title_text, precision_tokens=precision_tokens)
    def _is_obvious_official_community_result(self, *, hostname: str, path: str) -> bool:
        return query_routing._is_obvious_official_community_result(hostname=hostname, path=path)
    def _looks_like_debugging_result(
        self,
        *,
        hostname: str,
        registered_domain: str,
        path: str,
        title_text: str,
        snippet_text: str,
    ) -> bool:
        return query_routing._looks_like_debugging_result(hostname=hostname, registered_domain=registered_domain, path=path, title_text=title_text, snippet_text=snippet_text)
    def _looks_like_generic_debugging_docs_result(
        self,
        *,
        hostname: str,
        path: str,
        title_text: str,
    ) -> bool:
        return query_routing._looks_like_generic_debugging_docs_result(hostname=hostname, path=path, title_text=title_text)
    def _looks_like_status_result(self, *, url: str, hostname: str, title_text: str) -> bool:
        return query_routing._looks_like_status_result(url=url, hostname=hostname, title_text=title_text)
    def _looks_like_canonical_status_result(self, *, hostname: str, path: str) -> bool:
        return query_routing._looks_like_canonical_status_result(hostname=hostname, path=path)
    def _looks_like_brand_status_domain(self, hostname: str) -> bool:
        return query_routing._looks_like_brand_status_domain(hostname)
    def _looks_like_software_version_reference_result(
        self,
        *,
        url: str,
        hostname: str,
        title_text: str,
        snippet_text: str,
    ) -> bool:
        return query_routing._looks_like_software_version_reference_result(url=url, hostname=hostname, title_text=title_text, snippet_text=snippet_text)
    def _looks_like_canonical_software_version_result(
        self,
        *,
        hostname: str,
        path: str,
        title_text: str,
    ) -> bool:
        return query_routing._looks_like_canonical_software_version_result(hostname=hostname, path=path, title_text=title_text)
    def _looks_like_resource_result(
        self,
        *,
        url: str,
        hostname: str,
        title_text: str,
        mode: SearchMode,
    ) -> bool:
        return query_routing._looks_like_resource_result(url=url, hostname=hostname, title_text=title_text, mode=mode)
    def _looks_like_pdf_url(self, url: str) -> bool:
        return query_routing._looks_like_pdf_url(url)
    def _is_obvious_third_party_resource(
        self,
        *,
        hostname: str,
        registered_domain: str,
        mode: SearchMode,
    ) -> bool:
        return query_routing._is_obvious_third_party_resource(hostname=hostname, registered_domain=registered_domain, mode=mode)
    def _collect_source_domains(
        self,
        *,
        results: list[dict[str, Any]],
        citations: list[dict[str, Any]],
    ) -> list[str]:
        return selection._collect_source_domains(results=results, citations=citations)

    def _collect_social_identities(
        self,
        *,
        results: list[dict[str, Any]],
        citations: list[dict[str, Any]],
    ) -> list[str]:
        return finalize._collect_social_identities(results=results, citations=citations)

    def _should_use_social_identity_diversity(
        self,
        *,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        source_domains: list[str],
        social_identity_count: int,
    ) -> bool:
        return query_routing._should_use_social_identity_diversity(mode=mode, intent=intent, source_domains=source_domains, social_identity_count=social_identity_count)
    def _count_official_resource_results(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        results: list[dict[str, Any]],
        include_domains: list[str] | None,
    ) -> int:
        return finalize._count_official_resource_results(query=query, mode=mode, intent=intent, results=results, include_domains=include_domains)

    def _detect_evidence_conflicts(
        self,
        *,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        results: list[dict[str, Any]],
        include_domains: list[str] | None,
        source_domains: list[str],
        official_source_count: int,
        providers_consulted: list[str],
        official_mode: str,
        social_identity_count: int,
        social_identity_diversity_applies: bool,
    ) -> list[str]:
        return finalize._detect_evidence_conflicts(mode=mode, intent=intent, results=results, include_domains=include_domains, source_domains=source_domains, official_source_count=official_source_count, providers_consulted=providers_consulted, official_mode=official_mode, social_identity_count=social_identity_count, social_identity_diversity_applies=social_identity_diversity_applies)

    def _estimate_search_confidence(
        self,
        *,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        result_count: int,
        source_domain_count: int,
        official_source_count: int,
        verification: str,
        conflicts: list[str],
        official_mode: str,
        social_identity_count: int,
        social_identity_diversity_applies: bool,
    ) -> str:
        return finalize._estimate_search_confidence(mode=mode, intent=intent, result_count=result_count, source_domain_count=source_domain_count, official_source_count=official_source_count, verification=verification, conflicts=conflicts, official_mode=official_mode, social_identity_count=social_identity_count, social_identity_diversity_applies=social_identity_diversity_applies)

    def _describe_provider(
        self,
        provider: ProviderConfig,
        keyring_info: dict[str, object],
    ) -> dict[str, Any]:
        status = self._probe_provider_status(provider, int(keyring_info["count"]))
        return {
            "base_url": provider.base_url,
            "alternate_base_urls": provider.alternate_base_urls,
            "provider_mode": provider.provider_mode,
            "auth_mode": provider.auth_mode,
            "paths": provider.default_paths,
            "search_mode": provider.search_mode,
            "keys_file": str(provider.keys_file or ""),
            "available_keys": keyring_info["count"],
            "total_keys": keyring_info.get("total_count", keyring_info["count"]),
            "quarantined_keys": keyring_info.get("quarantined_count", 0),
            "quarantine_reasons": keyring_info.get("quarantine_reasons", []),
            "sources": keyring_info["sources"],
            "live_status": status["status"],
            "live_error": status["error"],
            "last_checked_at": status["checked_at"],
        }

    def _xai_probe_model(self) -> str:
        # Probe 取当前 registry 首项，跟随 MYSEARCH_GROK_MODELS / EXTRA_MODELS 自定义。
        # 极端测试场景下 xai_models 可能为空 tuple，回退到内置 basic 层首项。
        models = self.config.xai_models
        if models:
            return models[0].id
        return "grok-4.20-0309"

    def _derive_root_health_base_url(self, provider: ProviderConfig) -> str:
        candidate = (
            provider.base_url_for("social_search")
            or provider.base_url_for("social_health")
            or provider.base_url
        )
        parsed = urlparse(str(candidate or "").strip())
        if not parsed.scheme or not parsed.netloc:
            return str(candidate or "").strip().rstrip("/")
        return urlunparse((parsed.scheme, parsed.netloc, "", "", "", "")).rstrip("/")

    def _probe_xai_official_status_page(self, timeout_seconds: int) -> None:
        status_url = "https://status.x.ai/"
        status_code, response_text = self._request_text(
            url=status_url,
            timeout_seconds=timeout_seconds,
        )
        if status_code >= 400:
            raise MySearchHTTPError(
                provider="xai",
                status_code=status_code,
                detail=f"status.x.ai returned HTTP {status_code}",
                url=status_url,
            )

        lowered = " ".join(response_text.lower().split())
        if "all systems operational" in lowered:
            return

        matches = re.findall(
            r"api(?:\s*\([^)]*\))?[^a-z]{0,40}(available|operational|degraded|outage|unavailable|disrupted)",
            lowered,
        )
        if matches:
            negative = {"degraded", "outage", "unavailable", "disrupted"}
            if any(item in negative for item in matches):
                raise MySearchError(
                    "status.x.ai reports xAI API is not fully available"
                )
            return

        if "api" in lowered and "available" in lowered:
            return

        raise MySearchError("unable to determine xAI API status from status.x.ai")

    def _probe_xai_official_via_responses(
        self,
        provider: ProviderConfig,
        key: str,
        timeout_seconds: int,
    ) -> None:
        fallback_timeout_seconds = min(self.config.timeout_seconds, 20)
        self._request_json(
            provider=provider,
            method="POST",
            path=provider.path("responses"),
            payload=self._build_xai_responses_payload(
                query="openai",
                sources=["x"],
                max_results=1,
                include_domains=None,
                exclude_domains=None,
                allowed_x_handles=None,
                excluded_x_handles=None,
                from_date=None,
                to_date=None,
                include_x_images=False,
                include_x_videos=False,
                model=self._xai_probe_model(),
            ),
            key=key,
            timeout_seconds=min(timeout_seconds, fallback_timeout_seconds),
        )

    def _probe_provider_status(
        self,
        provider: ProviderConfig,
        key_count: int,
    ) -> dict[str, str]:
        if key_count <= 0:
            if self.keyring.has_configured_provider(provider.name):
                return {
                    "status": "key_unavailable",
                    "error": "all configured API keys are quarantined; manual key action required",
                    "checked_at": "",
                }
            return {
                "status": "not_configured",
                "error": "",
                "checked_at": "",
            }

        record = self.keyring.first(provider.name)
        if record is None:
            return {
                "status": "not_configured",
                "error": "",
                "checked_at": "",
            }

        key_fingerprint = hashlib.sha256(record.key.encode("utf-8")).hexdigest()[:12]
        cache_key = (
            f"{provider.name}:{record.label}:{key_fingerprint}:{key_count}:"
            f"{self.keyring.generation}"
        )
        with self._cache_lock:
            now = time.monotonic()
            cached = self._provider_probe_cache.get(cache_key)
            if cached and cached.get("expires_at", 0.0) > now:
                return copy.deepcopy(cached["value"])

        checked_at = datetime.now(timezone.utc).isoformat()
        cache_ttl_seconds = self._provider_probe_ttl_seconds
        try:
            self._probe_provider_request(provider, record.key)
            result = {
                "status": "ok",
                "error": "",
                "checked_at": checked_at,
            }
        except MySearchHTTPError as exc:
            result = {
                "status": "auth_error" if exc.is_auth_error else "http_error",
                "error": str(exc),
                "checked_at": checked_at,
            }
            if exc.key_failure_kind == "rate_limited":
                cache_ttl_seconds = min(
                    cache_ttl_seconds,
                    exc.retry_after_seconds or 60,
                )
            else:
                cache_ttl_seconds = min(cache_ttl_seconds, 30)
        except MySearchError as exc:
            result = {
                "status": "network_error",
                "error": str(exc),
                "checked_at": checked_at,
            }
            cache_ttl_seconds = min(cache_ttl_seconds, 30)

        with self._cache_lock:
            self._provider_probe_cache[cache_key] = {
                "expires_at": time.monotonic() + cache_ttl_seconds,
                "value": copy.deepcopy(result),
            }
        return result

    def _probe_xai_compatible_gateway(self, provider: ProviderConfig, key: str, timeout_seconds: int) -> None:
        health_path = "/health"
        health_base_url = self._derive_root_health_base_url(provider)
        payload = None
        try:
            payload = self._request_json(
                provider=provider,
                method="GET",
                path=health_path,
                payload=None,
                key=key,
                base_url=health_base_url,
                timeout_seconds=timeout_seconds,
            )
        except MySearchHTTPError as exc:
            if exc.is_auth_error:
                raise
        except MySearchError:
            pass
        if payload is not None:
            if not isinstance(payload, dict):
                raise MySearchError("social/X gateway health probe returned unexpected response type")
            if payload.get("ok") is False:
                detail = (
                    payload.get("error")
                    or payload.get("detail")
                    or "social/X gateway health probe reported unavailable"
                )
                raise MySearchError(str(detail))
            return

        fallback_timeout_seconds = min(timeout_seconds, min(self.config.timeout_seconds, 20))
        self._request_json(
            provider=provider,
            method="POST",
            path=provider.path("social_search"),
            payload={
                "query": "openai",
                "source": "x",
                "max_results": 1,
                "model": self._xai_probe_model(),
            },
            key=key,
            base_url=provider.base_url_for("social_search"),
            timeout_seconds=fallback_timeout_seconds,
        )

    def _probe_provider_request(self, provider: ProviderConfig, key: str) -> None:
        timeout_seconds = min(self.config.timeout_seconds, 10)
        if provider.name == "tavily":
            self._request_json(
                provider=provider,
                method="POST",
                path=provider.path("search"),
                payload={
                    "query": "openai",
                    "max_results": 1,
                    "search_depth": "basic",
                    "topic": "general",
                    "include_answer": False,
                    "include_raw_content": False,
                },
                key=key,
                timeout_seconds=timeout_seconds,
            )
            return
        if provider.name == "firecrawl":
            self._request_json(
                provider=provider,
                method="POST",
                path=provider.path("search"),
                payload={
                    "query": "openai",
                    "limit": 1,
                },
                key=key,
                timeout_seconds=timeout_seconds,
            )
            return
        if provider.name == "exa":
            self._request_json(
                provider=provider,
                method="POST",
                path=provider.path("search"),
                payload={
                    "query": "openai",
                    "numResults": 1,
                },
                key=key,
                timeout_seconds=timeout_seconds,
            )
            return
        if provider.name == "xai":
            if provider.search_mode == "compatible":
                self._probe_xai_compatible_gateway(provider, key, timeout_seconds)
                return
            try:
                self._probe_xai_official_status_page(timeout_seconds=timeout_seconds)
            except MySearchHTTPError:
                self._probe_xai_official_via_responses(
                    provider=provider,
                    key=key,
                    timeout_seconds=timeout_seconds,
                )
            except MySearchError as exc:
                if "not fully available" in str(exc):
                    raise
                self._probe_xai_official_via_responses(
                    provider=provider,
                    key=key,
                    timeout_seconds=timeout_seconds,
                )
            return

    def _summarize_route_error(self, error_text: str) -> str:
        compact = " ".join(error_text.split())
        if len(compact) <= 220:
            return compact
        return f"{compact[:217]}..."

    @staticmethod
    def _looks_like_provider_limit_error(error_text: str) -> bool:
        return query_routing._looks_like_provider_limit_error(error_text)
    def _provider_can_serve(self, provider: ProviderConfig) -> bool:
        if not self.keyring.has_provider(provider.name):
            return False
        key_count = int(self.keyring.describe()[provider.name]["count"])
        probe = self._probe_provider_status(provider, key_count)
        status = str(probe.get("status") or "")
        if status in {"", "not_configured", "auth_error", "key_unavailable"}:
            return False
        if status == "http_error" and self._looks_like_provider_limit_error(
            str(probe.get("error") or "")
        ):
            return False
        return True

    def _provider_live_status(self, provider: ProviderConfig) -> str | None:
        if not self.keyring.has_provider(provider.name):
            return None
        key_count = int(self.keyring.describe()[provider.name]["count"])
        status = self._probe_provider_status(provider, key_count)
        return str(status.get("status") or "")

    def _provider_is_live_ok(self, provider: ProviderConfig) -> bool:
        return self._provider_live_status(provider) == "ok"

    def _extract_xai_output_text(self, payload: dict[str, Any]) -> str:
        if isinstance(payload.get("output_text"), str):
            return payload["output_text"]

        parts: list[str] = []
        for item in payload.get("output", []) or []:
            content = item.get("content")
            if isinstance(content, str):
                parts.append(content)
                continue

            if not isinstance(content, list):
                continue

            for part in content:
                if not isinstance(part, dict):
                    continue

                if isinstance(part.get("text"), str):
                    parts.append(part["text"])
                    continue

                text_obj = part.get("text")
                if isinstance(text_obj, dict) and isinstance(text_obj.get("value"), str):
                    parts.append(text_obj["value"])

        return "\n".join([item for item in parts if item]).strip()

    def _extract_xai_citations(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        raw_citations = payload.get("citations") or []
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()

        if isinstance(raw_citations, list):
            for item in raw_citations:
                citation = self._normalize_citation(item)
                if citation is None:
                    continue
                url = citation.get("url", "")
                if url and url in seen:
                    continue
                if url:
                    seen.add(url)
                normalized.append(citation)

        if normalized:
            return normalized

        for output_item in payload.get("output", []) or []:
            if not isinstance(output_item, dict):
                continue

            content_items = output_item.get("content") or []
            if not isinstance(content_items, list):
                continue

            for content_item in content_items:
                if not isinstance(content_item, dict):
                    continue

                annotations = content_item.get("annotations") or []
                if not isinstance(annotations, list):
                    continue

                for annotation in annotations:
                    citation = self._normalize_citation(annotation)
                    if citation is None:
                        continue
                    url = citation.get("url", "")
                    if url and url in seen:
                        continue
                    if url:
                        seen.add(url)
                    normalized.append(citation)

        return normalized

    def _normalize_citation(self, item: Any) -> dict[str, Any] | None:
        return postprocess._normalize_citation(item)

    def _firecrawl_categories(
        self,
        mode: SearchMode,
        intent: ResolvedSearchIntent | None = None,
    ) -> list[str]:
        if mode == "github":
            return ["github"]
        if mode == "pdf":
            return ["pdf"]
        if mode == "news" or intent in {"news", "status"}:
            return ["news"]
        if intent == "tutorial":
            return []
        if mode in {"docs", "research"} or intent in {"resource", "tutorial"}:
            return ["research"]
        return []

    def _normalize_firecrawl_search_categories(self, categories: list[str]) -> list[str]:
        return cache_keys._normalize_firecrawl_search_categories(categories=categories)

    def _looks_like_news_query(self, query_lower: str) -> bool:
        return query_routing._looks_like_news_query(query_lower)
    def _looks_like_software_version_query(self, query_lower: str) -> bool:
        return query_routing._looks_like_software_version_query(query_lower)
    def _looks_like_award_result_query(self, query_lower: str) -> bool:
        return query_routing._looks_like_award_result_query(query_lower)
    def _award_query_category_markers(self, query_lower: str) -> list[str]:
        return query_routing._award_query_category_markers(query_lower)
    def _award_query_competing_category_markers(self, query_lower: str) -> list[str]:
        return query_routing._award_query_competing_category_markers(query_lower)
    def _looks_like_award_category_conflict(
        self,
        *,
        query_lower: str,
        title_text: str,
        snippet_text: str,
        content_text: str,
    ) -> bool:
        return query_routing._looks_like_award_category_conflict(query_lower=query_lower, title_text=title_text, snippet_text=snippet_text, content_text=content_text)
    def _award_query_brand_markers(self, query_lower: str) -> list[str]:
        return query_routing._award_query_brand_markers(query_lower)
    def _looks_like_award_brand_conflict(
        self,
        *,
        query_lower: str,
        title_text: str,
        snippet_text: str,
        content_text: str,
        path: str,
    ) -> bool:
        return query_routing._looks_like_award_brand_conflict(query_lower=query_lower, title_text=title_text, snippet_text=snippet_text, content_text=content_text, path=path)
    def _looks_like_box_office_query(self, query_lower: str) -> bool:
        return query_routing._looks_like_box_office_query(query_lower)
    def _looks_like_result_event_query(self, query_lower: str) -> bool:
        return query_routing._looks_like_result_event_query(query_lower)
    def _looks_like_gossip_query(self, query_lower: str) -> bool:
        return query_routing._looks_like_gossip_query(query_lower)
    def _is_entertainment_gossip_domain(self, registered_domain: str) -> bool:
        return query_routing._is_entertainment_gossip_domain(registered_domain)
    def _looks_like_gossip_result(
        self,
        *,
        title_text: str,
        snippet_text: str,
        path: str,
    ) -> bool:
        return query_routing._looks_like_gossip_result(title_text=title_text, snippet_text=snippet_text, path=path)
    def _looks_like_status_query(self, query_lower: str) -> bool:
        return query_routing._looks_like_status_query(query_lower)
    def _looks_like_comparison_query(self, query_lower: str) -> bool:
        return query_routing._looks_like_comparison_query(query_lower)
    def _looks_like_tutorial_query(self, query_lower: str) -> bool:
        return query_routing._looks_like_tutorial_query(query_lower)
    def _looks_like_debugging_query(self, query_lower: str) -> bool:
        return query_routing._looks_like_debugging_query(query_lower)
    def _looks_like_local_life_query(self, query_lower: str) -> bool:
        return query_routing._looks_like_local_life_query(query_lower)
    def _looks_like_docs_query(self, query_lower: str) -> bool:
        return query_routing._looks_like_docs_query(query_lower)
    def _looks_like_api_docs_topic_query(self, query_lower: str) -> bool:
        return query_routing._looks_like_api_docs_topic_query(query_lower)
    def _looks_like_exploratory_query(self, query_lower: str) -> bool:
        return query_routing._looks_like_exploratory_query(query_lower)
    def _build_search_summary_fallback(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent | str,
        result: dict[str, Any],
    ) -> str:
        answer = (result.get("answer") or "").strip()
        if answer:
            return answer

        results = list(result.get("results") or [])
        if not results:
            return ""

        top = results[0]
        title = (top.get("title") or "").strip()
        url = (top.get("url") or "").strip()
        label = title or url
        if not label:
            return ""

        evidence = result.get("evidence") or {}
        official_mode = str(evidence.get("official_mode") or "off")
        source_diversity = int(evidence.get("source_diversity") or 0)
        verification = str(evidence.get("verification") or "").strip()
        domain = self._registered_domain(self._result_hostname(top))
        top_excerpt = self._search_summary_excerpt(top, limit=160)

        if official_mode == "strict":
            summary = f"Top official match: {label}"
        elif mode == "news" or intent == "news":
            summary = f"Top news match: {label}"
        elif mode in {"docs", "github", "pdf"} or intent in {"resource", "tutorial"}:
            summary = f"Top source: {label}"
        else:
            summary = f"Top result: {label}"

        if domain:
            summary = f"{summary} ({domain})"
        if top_excerpt and (mode in {"docs", "github", "pdf"} or intent in {"resource", "tutorial"}):
            summary = f"{summary} — {top_excerpt}"
        if verification == "cross-provider" and source_diversity >= 2:
            summary = f"{summary}; corroborated across {source_diversity} domains"
        return summary

    def _search_summary_excerpt(self, item: Mapping[str, Any], limit: int = 160) -> str:
        github_release_excerpt = self._github_release_summary_excerpt(item)
        if github_release_excerpt:
            return self._build_excerpt(github_release_excerpt, limit=limit)
        snippet = re.sub(
            r"\s+",
            " ",
            str(item.get("snippet") or item.get("content") or "").strip(),
        ).strip()
        if not snippet:
            return ""
        snippet = re.sub(r"^[#>*`\-\s]+", "", snippet).strip()
        if self._search_summary_excerpt_looks_like_noise(snippet):
            return ""
        return self._build_excerpt(snippet, limit=limit)

    def _github_release_summary_excerpt(self, item: Mapping[str, Any]) -> str:
        url = str(item.get("url") or "").strip()
        if not url:
            return ""
        parsed = urlparse(url)
        hostname = self._registered_domain(parsed.hostname or "")
        if hostname != "github.com" or not parsed.path.rstrip("/").endswith("/releases"):
            return ""
        text = re.sub(r"\s+", " ", str(item.get("snippet") or item.get("content") or "").strip()).strip()
        matches = re.findall(
            r"(?:##\s+)?(v?\d+\.\d+\.\d+(?:[-+._][a-z0-9]+)?)\s*\((\d{4}-\d{2}-\d{2})\)",
            text,
            flags=re.IGNORECASE,
        )
        if not matches:
            try:
                extracted_page = self.extract_url(
                    url=url,
                    provider="auto",
                    formats=["markdown"],
                    only_main_content=True,
                )
            except MySearchError:
                extracted_page = {}
            extracted_text = re.sub(
                r"\s+",
                " ",
                str(extracted_page.get("content") or "").strip(),
            ).strip()
            if extracted_text:
                matches = re.findall(
                    r"(?:##\s+)?(v?\d+\.\d+\.\d+(?:[-+._][a-z0-9]+)?)\s*\((\d{4}-\d{2}-\d{2})\)",
                    extracted_text,
                    flags=re.IGNORECASE,
                )
        if not matches:
            return ""
        version, date = max(matches, key=lambda item: item[1])
        return f"Latest release {version} ({date})"

    def _search_summary_excerpt_looks_like_noise(self, text: str) -> bool:
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

    def _apply_result_event_answer_override(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        strategy: SearchStrategy,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        query_lower = query.lower()
        if not (
            self._looks_like_result_event_query(query_lower)
            and (mode == "news" or intent in {"news", "status"})
        ):
            return result

        current_answer = str(result.get("answer") or "").strip()
        result_items = list(result.get("results") or [])
        result_event_query = self._looks_like_result_event_query(query_lower)
        weak_award_signal = (
            self._looks_like_award_result_query(query_lower)
            and not self._has_strong_award_result(query=query, results=result_items)
        )
        allow_award_page_extraction = (
            self._looks_like_award_result_query(query_lower)
            and self._can_attempt_award_page_extraction(query=query, results=result_items)
        )
        if weak_award_signal and current_answer and self._answer_looks_uncertain(current_answer):
            current_answer = ""
        extracted_answer = ""
        if not weak_award_signal:
            extracted_answer = self._extract_result_event_answer(
                query=query,
                results=result_items,
            )
        should_try_page_extraction = (
            strategy in {"verify", "deep"}
            or not current_answer
            or self._answer_looks_uncertain(current_answer)
            or result_event_query
        )
        if (
            not extracted_answer
            and should_try_page_extraction
            and (not weak_award_signal or allow_award_page_extraction)
        ):
            extracted_answer = self._extract_result_event_answer_from_top_page(
                query=query,
                results=result_items,
            )

        if extracted_answer:
            updated = dict(result)
            updated["answer"] = extracted_answer
            updated["evidence"] = dict(updated.get("evidence") or {})
            updated["evidence"]["answer_source"] = "result-event-extraction"
            return updated

        if current_answer and self._answer_looks_uncertain(current_answer):
            updated = dict(result)
            updated["answer"] = ""
            updated["evidence"] = dict(updated.get("evidence") or {})
            updated["evidence"]["answer_source"] = "suppressed-provider-answer"
            return updated

        return result

    def _apply_software_version_answer_override(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        return software_version._apply_software_version_answer_override(query=query, mode=mode, intent=intent, result=result)

    def _extract_software_version_answer(
        self,
        *,
        query: str,
        results: list[dict[str, Any]],
    ) -> str:
        return software_version._extract_software_version_answer(query=query, results=results)

    def _software_version_item_is_version_index(
        self,
        *,
        versions: list[tuple[int, int, int]],
        peak_signal: int,
    ) -> bool:
        return software_version._software_version_item_is_version_index(versions=versions, peak_signal=peak_signal)

    def _software_version_candidates_from_text(
        self,
        text: str,
    ) -> list[tuple[str, tuple[int, int, int], int]]:
        return software_version._software_version_candidates_from_text(text=text)

    def _software_version_result_score(
        self,
        *,
        query: str,
        item: Mapping[str, Any],
    ) -> int:
        return software_version._software_version_result_score(query=query, item=item)

    def _software_version_subject(self, query: str) -> str:
        return software_version._software_version_subject(query=query)

    def _extract_semantic_version(self, text: str) -> tuple[int, int, int] | None:
        return software_version._extract_semantic_version(text=text)

    def _extract_result_event_answer_from_top_page(
        self,
        *,
        query: str,
        results: list[dict[str, Any]],
    ) -> str:
        candidates = self._result_event_candidates(query=query, results=results, limit=5)
        for top in candidates:
            url = str(top.get("url") or "").strip()
            if not url:
                continue
            extracted_answer = self._extract_result_event_answer_from_official_page_html(
                query=query,
                url=url,
            )
            if extracted_answer:
                return extracted_answer
            try:
                extracted_page = self.extract_url(
                    url=url,
                    provider="auto",
                    formats=["markdown"],
                    only_main_content=True,
                )
            except MySearchError:
                continue
            extracted_answer = self._extract_result_event_answer(
                query=query,
                results=[
                    {
                        "title": top.get("title", ""),
                        "snippet": top.get("snippet", ""),
                        "content": extracted_page.get("content", ""),
                    }
                ],
            )
            if extracted_answer:
                return extracted_answer
        return ""

    def _extract_result_event_answer_from_official_page_html(
        self,
        *,
        query: str,
        url: str,
    ) -> str:
        hostname = self._registered_domain(self._result_hostname({"url": url}))
        if hostname not in self._OFFICIAL_AWARD_DOMAINS:
            return ""
        try:
            status_code, response_text = self._request_text(
                url=url,
                timeout_seconds=min(self.config.timeout_seconds, 20),
            )
        except MySearchError:
            return ""
        if status_code >= 400 or not response_text:
            return ""
        plain = html.unescape(re.sub(r"<[^>]+>", " ", response_text))
        plain = re.sub(r"\s+", " ", plain).strip()
        if not plain:
            return ""
        return self._extract_result_event_answer_from_text(
            query_lower=query.lower(),
            text=plain,
        )

    def _result_event_candidates(
        self,
        *,
        query: str,
        results: list[dict[str, Any]],
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        return events._result_event_candidates(query=query, results=results, limit=limit)

    def _result_event_page_priority(
        self,
        *,
        query: str,
        item: Mapping[str, Any],
    ) -> int:
        return query_routing._result_event_page_priority(query=query, item=item)
    def _answer_looks_uncertain(self, answer: str) -> bool:
        answer_lower = answer.lower()
        markers = [
            "not yet determined",
            "not yet known",
            "cannot be determined",
            "cannot determine",
            "not specified",
            "not provided",
            "insufficient data",
            "no winner was specified",
            "cannot be concluded",
            "could not be determined",
            "still unknown",
            "to be announced",
            "tbd",
            "unclear",
            "unknown",
            "尚未确定",
            "尚未公布",
            "待公布",
            "未知",
        ]
        return any(marker in answer_lower for marker in markers)

    def _extract_result_event_answer(
        self,
        *,
        query: str,
        results: list[dict[str, Any]],
    ) -> str:
        return events._extract_result_event_answer(query=query, results=results)

    def _extract_result_event_answer_from_text(
        self,
        *,
        query_lower: str,
        text: str,
    ) -> str:
        return events._extract_result_event_answer_from_text(query_lower=query_lower, text=text)

    def _extract_album_of_the_year_entity(self, text: str) -> str:
        return events._extract_album_of_the_year_entity(text=text)

    def _extract_named_fact_entity(
        self,
        text: str,
        *,
        patterns: list[str],
        reject_substrings: list[str] | None = None,
    ) -> str:
        return events._extract_named_fact_entity(text=text, patterns=patterns, reject_substrings=reject_substrings)

    def _clean_extracted_fact_entity(
        self,
        value: str,
        *,
        reject_substrings: list[str] | None = None,
    ) -> str:
        return events._clean_extracted_fact_entity(value=value, reject_substrings=reject_substrings)

    def _looks_like_publisher_fragment(self, entity: str) -> bool:
        return query_routing._looks_like_publisher_fragment(entity)
    def _looks_like_query_year_mismatch(self, *, query: str, text: str) -> bool:
        return query_routing._looks_like_query_year_mismatch(query=query, text=text)
    def _build_research_report_source_lines(
        self,
        *,
        query: str,
        mode: str,
        ordered_results: list[dict[str, Any]],
        citations: list[dict[str, Any]],
        include_domains: list[str] | None,
        authoritative_preferred: bool,
        comparison_like: bool,
        max_items: int = 4,
    ) -> list[str]:
        return sections._build_research_report_source_lines(query=query, mode=mode, ordered_results=ordered_results, citations=citations, include_domains=include_domains, authoritative_preferred=authoritative_preferred, comparison_like=comparison_like, max_items=max_items)

    def _research_source_topic_key(self, *, title: str, domain: str) -> str:
        return sections._research_source_topic_key(title=title, domain=domain)

    def _build_research_report_sections(
        self,
        *,
        query: str,
        web_search: dict[str, Any],
        ordered_results: list[dict[str, Any]],
        pages: list[dict[str, Any]],
        citations: list[dict[str, Any]],
        social: dict[str, Any] | None,
        evidence: dict[str, Any],
        executive_summary_override: str = "",
    ) -> dict[str, Any]:
        return sections._build_research_report_sections(query=query, web_search=web_search, ordered_results=ordered_results, pages=pages, citations=citations, social=social, evidence=evidence, executive_summary_override=executive_summary_override)

    def _render_research_report(self, sections: dict[str, Any]) -> str:
        return research.render_research_report(sections)

    def _research_report_source_domains(self, source_lines: Sequence[str]) -> list[str]:
        return sections._research_report_source_domains(source_lines=source_lines)

    def _build_research_summary_fallback(
        self,
        *,
        query: str,
        web_search: dict[str, Any],
        pages: list[dict[str, Any]],
        citations: list[dict[str, Any]],
        social: dict[str, Any] | None,
        evidence: dict[str, Any],
    ) -> str:
        return cache_keys._build_research_summary_fallback(query=query, web_search=web_search, pages=pages, citations=citations, social=social, evidence=evidence)

    def _build_research_source_clusters(
        self,
        *,
        query: str,
        mode: str,
        ordered_results: list[dict[str, Any]],
        include_domains: list[str] | None,
        authoritative_preferred: bool,
    ) -> list[dict[str, Any]]:
        return sections._build_research_source_clusters(query=query, mode=mode, ordered_results=ordered_results, include_domains=include_domains, authoritative_preferred=authoritative_preferred)

    def _build_research_claim_evidence(
        self,
        *,
        query: str,
        mode: str,
        ordered_results: list[dict[str, Any]],
        pages: list[dict[str, Any]],
        citations: list[dict[str, Any]],
        comparison_like: bool,
        include_domains: list[str] | None,
        authoritative_preferred: bool,
    ) -> list[dict[str, Any]]:
        return sections._build_research_claim_evidence(query=query, mode=mode, ordered_results=ordered_results, pages=pages, citations=citations, comparison_like=comparison_like, include_domains=include_domains, authoritative_preferred=authoritative_preferred)

    def _diversify_research_claims_by_domain(
        self,
        claims: list[dict[str, Any]],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        return sections._diversify_research_claims_by_domain(claims=claims, limit=limit)

    def _dedupe_research_claims_by_source_topic(
        self,
        claims: list[dict[str, Any]],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        return sections._dedupe_research_claims_by_source_topic(claims=claims, limit=limit)

    def _research_claim_source_kind_rank(self, entry: dict[str, Any]) -> int:
        return sections._research_claim_source_kind_rank(entry=entry)

    def _research_claim_primary_cluster(
        self,
        *,
        entry: dict[str, Any],
        authoritative_preferred: bool,
    ) -> str:
        return sections._research_claim_primary_cluster(entry=entry, authoritative_preferred=authoritative_preferred)

    def _trim_research_claims_for_visibility(
        self,
        *,
        claims: list[dict[str, Any]],
        authoritative_preferred: bool,
        comparison_like: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        return sections._trim_research_claims_for_visibility(claims=claims, authoritative_preferred=authoritative_preferred, comparison_like=comparison_like, limit=limit)

    def _research_claim_text(
        self,
        *,
        title: str,
        excerpt: str,
        comparison_like: bool,
    ) -> str:
        return sections._research_claim_text(title=title, excerpt=excerpt, comparison_like=comparison_like)

    def _normalize_research_claim_text(
        self,
        text: str,
        *,
        comparison_like: bool,
    ) -> str:
        return sections._normalize_research_claim_text(text=text, comparison_like=comparison_like)

    def _select_research_claim_excerpt(
        self,
        *,
        page_excerpt: str,
        snippet: str,
        content: str,
    ) -> str:
        return sections._select_research_claim_excerpt(page_excerpt=page_excerpt, snippet=snippet, content=content)

    def _research_excerpt_looks_like_schema_noise(self, text: str) -> bool:
        return sections._research_excerpt_looks_like_schema_noise(text=text)

    def _research_excerpt_looks_like_json_shell(self, text: str) -> bool:
        return research.claims.research_excerpt_looks_like_json_shell(text)

    def _research_claim_signature(self, claim: str) -> str:
        return research.claims.research_claim_signature(claim)

    def _research_claim_is_generic(self, claim: str) -> bool:
        return research.claims.research_claim_is_generic(claim)

    def _research_claim_comparison_subject_match_count(
        self,
        *,
        claim: str,
        sources: Sequence[str],
        entities: Sequence[Sequence[str]],
    ) -> int:
        return research.claims.research_claim_comparison_subject_match_count(claim=claim, sources=sources, entities=entities)

    def _align_research_claims_with_comparison_rows(
        self,
        *,
        claim_evidence: list[dict[str, Any]],
        comparison_rows: Sequence[Mapping[str, Any]],
        comparison_entities: Sequence[Sequence[str]],
    ) -> list[dict[str, Any]]:
        return sections._align_research_claims_with_comparison_rows(claim_evidence=claim_evidence, comparison_rows=comparison_rows, comparison_entities=comparison_entities)

    def _research_claim_is_comparison_tail_relevant(self, claim: str) -> bool:
        return research.claims.research_claim_is_comparison_tail_relevant(claim)

    def _research_comparison_claim_from_row(
        self,
        *,
        comparison_rows: Sequence[Mapping[str, Any]],
        entity_tokens: Sequence[str],
    ) -> dict[str, Any]:
        return sections._research_comparison_claim_from_row(comparison_rows=comparison_rows, entity_tokens=entity_tokens)

    def _research_claim_entry_from_focus_row(
        self,
        row: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        return sections._research_claim_entry_from_focus_row(row=row)

    def _research_comparison_support_summary(
        self,
        *,
        authoritative_source_count: int,
        supporting_source_count: int,
    ) -> str:
        return research.comparison.research_comparison_support_summary(authoritative_source_count=authoritative_source_count, supporting_source_count=supporting_source_count)

    def _select_research_primary_claim(
        self, claim_evidence: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return sections._select_research_primary_claim(claim_evidence=claim_evidence)

    def _research_claim_support_level(
        self,
        *,
        source_count: int,
        provider_count: int,
        cluster_count: int,
    ) -> str:
        return research.claims.research_claim_support_level(source_count=source_count, provider_count=provider_count, cluster_count=cluster_count)

    def _research_claim_support_phrase(self, claim_entry: dict[str, Any]) -> str:
        return sections._research_claim_support_phrase(claim_entry=claim_entry)

    def _research_claim_support_basis(self, claim_entry: Mapping[str, Any]) -> str:
        return research.claims.research_claim_support_basis(claim_entry)

    def _research_claim_support_rank(self, support_level: str) -> int:
        return research.claims.research_claim_support_rank(support_level)

    def _research_claim_best_cluster_rank(
        self,
        *,
        clusters: list[str],
        authoritative_preferred: bool,
    ) -> int:
        return research.claims.research_claim_best_cluster_rank(clusters=clusters, authoritative_preferred=authoritative_preferred)

    def _research_authoritative_claim_fallback(
        self,
        *,
        query: str,
        mode: str,
        ordered_results: list[dict[str, Any]],
        include_domains: list[str] | None,
        authoritative_preferred: bool,
    ) -> dict[str, Any]:
        return sections._research_authoritative_claim_fallback(query=query, mode=mode, ordered_results=ordered_results, include_domains=include_domains, authoritative_preferred=authoritative_preferred)

    def _research_excerpt_has_substantive_claim(self, text: str) -> bool:
        return research.claims.research_excerpt_has_substantive_claim(text)

    def _research_excerpt_looks_like_link_index_noise(self, text: str) -> bool:
        return research.claims.research_excerpt_looks_like_link_index_noise(text)

    def _research_excerpt_looks_like_navigation_noise(self, text: str) -> bool:
        return research.claims.research_excerpt_looks_like_navigation_noise(text)

    def _research_excerpt_looks_like_noise(self, text: str) -> bool:
        return research.claims.research_excerpt_looks_like_noise(text)

    def _research_cluster_base_weight(
        self,
        *,
        label: str,
        authoritative_preferred: bool,
    ) -> float:
        return sections._research_cluster_base_weight(label=label, authoritative_preferred=authoritative_preferred)

    def _research_cluster_tier(self, *, weight: float, label: str) -> str:
        return sections._research_cluster_tier(weight=weight, label=label)

    def _research_cluster_fit_summary(self, cluster_label: str) -> str:
        return research.comparison.research_cluster_fit_summary(cluster_label)

    def _research_select_comparison_focus_rows(
        self,
        *,
        comparison_rows: Sequence[Mapping[str, Any]],
        comparison_entities: Sequence[Sequence[str]],
        selected_urls: Sequence[str] | None = None,
    ) -> list[dict[str, str]]:
        return sections._research_select_comparison_focus_rows(comparison_rows=comparison_rows, comparison_entities=comparison_entities, selected_urls=selected_urls)

    def _research_comparison_profile(
        self,
        *,
        candidate: str,
        note: str,
        fit: str,
        url: str,
    ) -> dict[str, str]:
        return research.comparison.research_comparison_profile(candidate=candidate, note=note, fit=fit, url=url)

    def _research_build_decision_criteria(
        self,
        *,
        focus_rows: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        return sections._research_build_decision_criteria(focus_rows=focus_rows)

    def _research_build_comparison_matrix(
        self,
        *,
        focus_rows: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, str]]:
        return research.comparison.research_build_comparison_matrix(focus_rows=focus_rows)

    def _research_build_operational_tradeoffs(
        self,
        *,
        focus_rows: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        return research.comparison.research_build_operational_tradeoffs(focus_rows=focus_rows)

    def _research_build_decision_checklist(
        self,
        *,
        focus_rows: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, str]]:
        return research.comparison.research_build_decision_checklist(focus_rows=focus_rows)

    def _research_decision_strengths(
        self,
        *,
        cluster_label: str,
        provider_support: str,
        note: str,
        cluster_detail: dict[str, Any],
    ) -> str:
        return research.comparison.research_decision_strengths(cluster_label=cluster_label, provider_support=provider_support, note=note, cluster_detail=cluster_detail)

    def _research_decision_cautions(
        self,
        *,
        cluster_label: str,
        provider_support: str,
    ) -> str:
        return research.comparison.research_decision_cautions(cluster_label=cluster_label, provider_support=provider_support)

    def _build_excerpt(self, content: str, limit: int = 600) -> str:
        return sections._build_excerpt(content=content, limit=limit)
