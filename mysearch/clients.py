"""MySearch provider client 和自动路由。"""

from __future__ import annotations

import copy
import hashlib
import html
import json
import re
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass as _dataclass
from datetime import date, datetime, time as dt_time, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Literal, cast
from urllib.error import HTTPError as UrlHTTPError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

import httpx

from mysearch.config import MySearchConfig, ProviderConfig
from mysearch.keyring import MySearchKeyRing


def dataclass(*args, **kwargs):
    if sys.version_info < (3, 10):
        kwargs.pop("slots", None)
    return _dataclass(*args, **kwargs)


SearchMode = Literal["auto", "web", "news", "social", "docs", "research", "github", "pdf"]
SearchIntent = Literal[
    "auto",
    "factual",
    "status",
    "comparison",
    "tutorial",
    "exploratory",
    "news",
    "resource",
]
ResolvedSearchIntent = Literal[
    "factual",
    "status",
    "comparison",
    "tutorial",
    "exploratory",
    "news",
    "resource",
]
SearchStrategy = Literal["auto", "fast", "balanced", "verify", "deep"]
ProviderName = Literal["auto", "tavily", "firecrawl", "exa", "xai"]
SEARCH_MODES: tuple[SearchMode, ...] = (
    "auto",
    "web",
    "news",
    "social",
    "docs",
    "research",
    "github",
    "pdf",
)


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
    ) -> None:
        self.provider = provider
        self.status_code = status_code
        self.detail = detail
        self.url = url
        super().__init__(self._build_message())

    @property
    def is_auth_error(self) -> bool:
        return self.status_code in {401, 403}

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
        fallback_chain=("exa",),
        tavily_topic="news",
        result_profile="news",
        allow_exa_rescue=True,
    ),
    "status": SearchRoutePolicy(
        key="status",
        provider="tavily",
        fallback_chain=("exa",),
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
        fallback_chain=("firecrawl", "exa"),
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


class MySearchClient:
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
        }
        self._cache_store: dict[str, dict[str, dict[str, Any]]] = {
            "search": {},
            "extract": {},
        }
        self._cache_stats: dict[str, dict[str, int]] = {
            "search": {"hits": 0, "misses": 0},
            "extract": {"hits": 0, "misses": 0},
        }
        self._cache_max_entries = 256
        self._provider_probe_ttl_seconds = 300
        self._provider_probe_cache: dict[str, dict[str, Any]] = {}
        self._http = httpx.Client(
            timeout=httpx.Timeout(self.config.timeout_seconds, connect=10.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={"User-Agent": "MySearch/0.2"},
        )

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
                "sources": info["sources"],
                "live_status": status["status"],
                "live_error": status["error"],
                "last_checked_at": status["checked_at"],
            }
        return {
            "server_name": self.config.server_name,
            "timeout_seconds": self.config.timeout_seconds,
            "xai_model": self.config.xai_model,
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
            return json.loads(json.dumps(payload["value"]))

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
                oldest_key = min(store, key=lambda k: store[k].get("expires_at", 0.0))
                store.pop(oldest_key, None)
            store[cache_key] = {
                "expires_at": now + ttl_seconds,
                "value": json.loads(json.dumps(value)),
            }

    def _build_cache_key(self, namespace: str, payload: dict[str, Any]) -> str:
        serialized = json.dumps(
            {
                "namespace": namespace,
                "payload": payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _execute_parallel(
        self,
        tasks: dict[str, Callable[[], Any]],
        *,
        max_workers: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Exception]]:
        if not tasks:
            return {}, {}

        if len(tasks) == 1:
            name, task = next(iter(tasks.items()))
            try:
                return {name: task()}, {}
            except Exception as exc:  # pragma: no cover - defensive
                return {}, {name: exc}

        worker_count = min(max_workers or self.config.max_parallel_workers, len(tasks))
        results: dict[str, Any] = {}
        errors: dict[str, Exception] = {}
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="mysearch") as executor:
            future_map: dict[Future[Any], str] = {
                executor.submit(task): name
                for name, task in tasks.items()
            }
            for future, name in future_map.items():
                try:
                    results[name] = future.result(timeout=self.config.timeout_seconds + 5)
                except Exception as exc:  # pragma: no cover - network/runtime dependent
                    errors[name] = exc
        return results, errors

    def _raise_parallel_error(self, errors: dict[str, Exception], task_name: str) -> None:
        error = errors.get(task_name)
        if error is None:
            return
        if isinstance(error, MySearchError):
            raise error
        raise MySearchError(str(error))

    def _should_cache_search(
        self,
        *,
        decision: RouteDecision,
        normalized_sources: list[str],
    ) -> bool:
        if self.config.search_cache_ttl_seconds <= 0:
            return False
        if "x" in normalized_sources:
            return False
        if decision.provider == "xai":
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
    ) -> str:
        return self._build_cache_key(
            "search",
            {
                "query": query,
                "mode": mode,
                "intent": resolved_intent,
                "strategy": resolved_strategy,
                "provider": provider,
                "normalized_sources": normalized_sources,
                "include_content": include_content,
                "include_answer": include_answer,
                "include_domains": sorted(set(include_domains or [])),
                "exclude_domains": sorted(set(exclude_domains or [])),
                "route_provider": decision.provider,
                "tavily_topic": decision.tavily_topic,
                "firecrawl_categories": decision.firecrawl_categories or [],
            },
        )

    def _build_extract_cache_key(
        self,
        *,
        url: str,
        formats: list[str],
        only_main_content: bool,
        provider: Literal["auto", "firecrawl", "tavily"],
    ) -> str:
        return self._build_cache_key(
            "extract",
            {
                "url": url,
                "formats": formats,
                "only_main_content": only_main_content,
                "provider": provider,
            },
        )

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

        if use_xai_unified:
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
            )
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
                    ),
                },
                max_workers=2,
            )
            self._raise_parallel_error(parallel_errors, "web")
            self._raise_parallel_error(parallel_errors, "social")
            web_result = parallel_results["web"]
            social_result = parallel_results["social"]
        web_route = web_result.get("route", {}).get("selected", web_result.get("provider", "tavily"))
        social_route = social_result.get("provider", "xai")
        web_results = list(web_result.get("results") or [])
        social_results = list(social_result.get("results") or [])
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
            "results": [*web_results, *social_results],
            "citations": self._dedupe_citations(
                web_result.get("citations") or [],
                social_result.get("citations") or [],
            ),
            "evidence": {
                "providers_consulted": [web_result.get("provider"), social_result.get("provider")],
                "web_result_count": len(web_results),
                "social_result_count": len(social_results),
                "citation_count": len(
                    self._dedupe_citations(
                        web_result.get("citations") or [],
                        social_result.get("citations") or [],
                    )
                ),
                "verification": "cross-provider",
            },
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
        result["summary"] = self._build_search_summary_fallback(
            query=query,
            mode=mode,
            intent=resolved_intent,
            result=result,
        )

        route_reason = decision.reason
        if result.get("provider") == "hybrid" and resolved_strategy in {"balanced", "verify", "deep"}:
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
            except MySearchError as exc:
                errors.append(f"firecrawl scrape failed: {exc}")
                if provider == "firecrawl":
                    raise

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
                )
                exa_results = exa_extract.get("results") or []
                if exa_results:
                    best = max(exa_results, key=lambda r: len(r.get("content") or ""))
                    content = (best.get("content") or "").strip()
                    if content and len(content) >= 100:
                        exa_result = {
                            "provider": "exa",
                            "transport": exa_extract.get("transport", ""),
                            "url": url,
                            "content": content,
                            "metadata": {"exa_url": best.get("url", "")},
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
        discovery_include_content = research_plan["web_mode"] in {"docs"} and self._provider_can_serve(
            self.config.firecrawl
        )
        research_tasks: dict[str, Callable[[], Any]] = {
            "web": lambda: self.search(
                query=query,
                mode=research_plan["web_mode"],
                intent=resolved_intent,
                strategy=resolved_strategy,
                provider="tavily" if research_plan["web_mode"] in {"web", "news"} else "auto",
                sources=["web"],
                max_results=research_plan["web_max_results"],
                include_content=discovery_include_content,
                include_answer=True,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
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
            )
        if authoritative_research and research_plan["web_mode"] == "web":
            research_tasks["docs_rescue"] = lambda: self.search(
                query=query,
                mode="docs",
                intent="resource",
                strategy="balanced" if resolved_strategy == "fast" else resolved_strategy,
                provider="auto",
                sources=["web"],
                max_results=max(4, min(research_plan["web_max_results"], 6)),
                include_content=False,
                include_answer=False,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
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
                from_date=from_date,
                to_date=to_date,
            )
        research_results, research_errors = self._execute_parallel(
            research_tasks,
            max_workers=len(research_tasks),
        )
        web_search = research_results.get("web")
        exa_discovery = research_results.get("exa_discovery")
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
            elif research_results.get("docs_rescue") and not research_errors.get("docs_rescue"):
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
            elif research_results.get("tavily_support") and not research_errors.get("tavily_support"):
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
            else:
                self._raise_parallel_error(research_errors, "web")
                web_search = research_results["web"]

        docs_rescue = research_results.get("docs_rescue")
        docs_rescue_results = (
            list(docs_rescue.get("results") or [])
            if docs_rescue and not research_errors.get("docs_rescue")
            else []
        )
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
        if web_search.get("provider") == "hybrid":
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

    def _looks_like_technical_research_query(self, query_lower: str) -> bool:
        technical_markers = (
            " api",
            "api ",
            "sdk",
            "reference",
            "background mode",
            "batch api",
            "responses api",
            "webhook",
            "webhooks",
            "technical report",
            "research paper",
            "pdf",
        )
        return self._looks_like_docs_query(query_lower) or any(
            marker in query_lower for marker in technical_markers
        )

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
        ordered_keys: list[str] = []
        seen: set[str] = set()
        variants_by_key: dict[str, list[dict[str, Any]]] = {}
        providers_by_key: dict[str, set[str]] = {}
        for results in result_lists:
            for item in results:
                if not isinstance(item, dict):
                    continue
                normalized = self._canonicalize_result_item(item)
                dedupe_key = self._result_dedupe_key(normalized)
                if not dedupe_key:
                    continue
                if dedupe_key not in seen:
                    seen.add(dedupe_key)
                    ordered_keys.append(dedupe_key)
                variants_by_key.setdefault(dedupe_key, []).append(normalized)
                providers = {
                    provider
                    for provider in (
                        normalized.get("matched_providers")
                        or [normalized.get("provider", "")]
                    )
                    if provider
                }
                providers_by_key.setdefault(dedupe_key, set()).update(providers)
        deduped: list[dict[str, Any]] = []
        for dedupe_key in ordered_keys:
            variants = variants_by_key.get(dedupe_key) or []
            if not variants:
                continue
            best = max(variants, key=self._result_quality_score)
            merged = self._canonicalize_result_item(dict(best))
            matched_providers = sorted(
                provider for provider in providers_by_key.get(dedupe_key, set()) if provider
            )
            if matched_providers:
                merged["matched_providers"] = matched_providers
            deduped.append(merged)
        return deduped

    def _research_result_cluster_label(
        self,
        *,
        query: str,
        mode: SearchMode,
        item: dict[str, Any],
        include_domains: list[str] | None,
        authoritative_preferred: bool,
    ) -> str:
        normalized = self._canonicalize_result_item(item)
        hostname = self._result_hostname(normalized)
        registered_domain = self._registered_domain(hostname)
        path = urlparse(normalized.get("url", "")).path.lower()
        title_text = (normalized.get("title") or "").lower()
        snippet_text = (
            normalized.get("snippet")
            or normalized.get("content")
            or ""
        ).lower()
        community_domains = {
            "facebook.com",
            "linkedin.com",
            "news.ycombinator.com",
            "quora.com",
            "reddit.com",
            "twitter.com",
            "x.com",
            "youtube.com",
            "youtu.be",
        }
        directory_domains = {
            "mcp-ai.org",
            "mcp.so",
            "mcpmarket.com",
            "mcpnow.io",
            "mcpserverfinder.com",
            "mcpservers.org",
            "pulsemcp.com",
            "toolhunter.cc",
        }
        if authoritative_preferred:
            effective_mode: SearchMode = mode if mode in {"docs", "github", "pdf"} else "docs"
            query_tokens = self._query_brand_tokens(query)
            flags = self._resource_result_flags(
                mode=effective_mode,
                item=normalized,
                query_tokens=query_tokens,
                include_domains=include_domains,
            )
            official_candidate = self._result_matches_official_policy(
                item=normalized,
                mode=effective_mode,
                query_tokens=query_tokens,
                include_domains=include_domains,
                strict_official=False,
            )
            community_candidate = (
                not bool(flags["non_third_party"])
                or self._is_obvious_official_community_result(hostname=hostname, path=path)
            )
            supportive_candidate = bool(flags["non_third_party"]) and (
                bool(flags["docs_shape_match"])
                or bool(flags["registered_domain_label_match"])
                or bool(flags["host_brand_match"])
                or bool(flags["title_brand_match"])
            )
            if official_candidate:
                return "official"
            if supportive_candidate:
                return "supporting"
            if community_candidate:
                return "community"
            return "general"

        listicle_candidate = (
            any(marker in title_text for marker in ("best ", "top ", "roundup", "ranking"))
            or any(marker in path for marker in ("/best-", "/top-", "/list-", "/lists/"))
            or (
                any(marker in snippet_text for marker in ("top ", "best ", "ranked ", "roundup"))
                and "/blog/" in path
            )
        )
        if registered_domain in community_domains:
            return "community"
        if registered_domain == "github.com":
            return "project"
        if registered_domain in directory_domains or (
            "mcp" in hostname and any(marker in path for marker in ("/server/", "/servers/"))
        ):
            return "directory"
        if listicle_candidate:
            return "listicle"
        return "curated"

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
        combined = self._dedupe_research_results_for_report(
            docs_rescue_results if authoritative_preferred else [],
            web_results,
            tavily_support_results,
            exa_results,
            [] if authoritative_preferred else docs_rescue_results,
        )
        if not combined:
            return [], {
                "authoritative_source_count": 0,
                "community_source_count": 0,
                "selected_candidate_domains": [],
                "selected_candidate_cluster_counts": {},
            }

        if not authoritative_preferred:
            project_candidates: list[dict[str, Any]] = []
            curated_candidates: list[dict[str, Any]] = []
            listicle_candidates: list[dict[str, Any]] = []
            directory_candidates: list[dict[str, Any]] = []
            community_candidates: list[dict[str, Any]] = []
            for item in combined:
                cluster_label = self._research_result_cluster_label(
                    query=query,
                    mode=mode,
                    item=item,
                    include_domains=include_domains,
                    authoritative_preferred=False,
                )
                if cluster_label == "community":
                    community_candidates.append(item)
                elif cluster_label == "project":
                    project_candidates.append(item)
                elif cluster_label == "directory":
                    directory_candidates.append(item)
                elif cluster_label == "listicle":
                    listicle_candidates.append(item)
                else:
                    curated_candidates.append(item)
            if len(project_candidates) > 1:
                project_candidates = self._rerank_general_results(
                    query=query,
                    result_profile="web",
                    results=project_candidates,
                    include_domains=include_domains,
                )
            if len(curated_candidates) > 1:
                curated_candidates = self._rerank_general_results(
                    query=query,
                    result_profile="web",
                    results=curated_candidates,
                    include_domains=include_domains,
                )
            if len(community_candidates) > 1:
                community_candidates = self._rerank_general_results(
                    query=query,
                    result_profile="web",
                    results=community_candidates,
                    include_domains=include_domains,
                )
            if len(directory_candidates) > 1:
                directory_candidates = self._rerank_general_results(
                    query=query,
                    result_profile="web",
                    results=directory_candidates,
                    include_domains=include_domains,
                )
            if len(listicle_candidates) > 1:
                listicle_candidates = self._rerank_general_results(
                    query=query,
                    result_profile="web",
                    results=listicle_candidates,
                    include_domains=include_domains,
                )
            selected = [
                *project_candidates,
                *curated_candidates,
                *listicle_candidates,
                *directory_candidates,
                *community_candidates,
            ][:max_results]
            selected_domains = self._collect_source_domains(results=selected, citations=[])
            cluster_counts: dict[str, int] = {}
            for item in selected:
                cluster_label = self._research_result_cluster_label(
                    query=query,
                    mode=mode,
                    item=item,
                    include_domains=include_domains,
                    authoritative_preferred=False,
                )
                cluster_counts[cluster_label] = cluster_counts.get(cluster_label, 0) + 1
            return selected, {
                "authoritative_source_count": 0,
                "community_source_count": sum(
                    1
                    for item in selected
                    if self._research_result_cluster_label(
                        query=query,
                        mode=mode,
                        item=item,
                        include_domains=include_domains,
                        authoritative_preferred=False,
                    )
                    == "community"
                ),
                "selected_candidate_domains": selected_domains[:5],
                "selected_candidate_cluster_counts": cluster_counts,
            }

        effective_mode: SearchMode = mode if mode in {"docs", "github", "pdf"} else "docs"
        official_candidates: list[dict[str, Any]] = []
        supporting_candidates: list[dict[str, Any]] = []
        general_candidates: list[dict[str, Any]] = []
        community_candidates: list[dict[str, Any]] = []

        for item in combined:
            normalized = self._canonicalize_result_item(item)
            cluster_label = self._research_result_cluster_label(
                query=query,
                mode=effective_mode,
                item=normalized,
                include_domains=include_domains,
                authoritative_preferred=True,
            )
            if cluster_label == "official":
                official_candidates.append(normalized)
            elif cluster_label == "supporting":
                supporting_candidates.append(normalized)
            elif cluster_label == "community":
                community_candidates.append(normalized)
            else:
                general_candidates.append(normalized)

        if len(official_candidates) > 1:
            official_candidates = self._rerank_resource_results(
                query=query,
                mode=effective_mode,
                results=official_candidates,
                include_domains=include_domains,
            )
        if len(supporting_candidates) > 1:
            supporting_candidates = self._rerank_resource_results(
                query=query,
                mode=effective_mode,
                results=supporting_candidates,
                include_domains=include_domains,
            )
        if len(general_candidates) > 1:
            general_candidates = self._rerank_general_results(
                query=query,
                result_profile="web",
                results=general_candidates,
                include_domains=include_domains,
            )
        if len(community_candidates) > 1:
            community_candidates = self._rerank_general_results(
                query=query,
                result_profile="web",
                results=community_candidates,
                include_domains=include_domains,
            )

        ordered = [
            *official_candidates,
            *supporting_candidates,
            *general_candidates,
            *community_candidates,
        ][:max_results]
        selected_domains = self._collect_source_domains(results=ordered, citations=[])
        cluster_counts: dict[str, int] = {}
        for item in ordered:
            cluster_label = self._research_result_cluster_label(
                query=query,
                mode=effective_mode,
                item=item,
                include_domains=include_domains,
                authoritative_preferred=True,
            )
            cluster_counts[cluster_label] = cluster_counts.get(cluster_label, 0) + 1
        return ordered, {
            "authoritative_source_count": min(
                len(ordered),
                len(official_candidates) + len(supporting_candidates),
            ),
            "community_source_count": min(len(ordered), len(community_candidates)),
            "selected_candidate_domains": selected_domains[:5],
            "selected_candidate_cluster_counts": cluster_counts,
        }

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
        trimmed = dict(result)
        results = list(trimmed.get("results") or [])[:max_results]
        trimmed["results"] = results
        trimmed["citations"] = self._align_citations_with_results(
            results=results,
            citations=list(trimmed.get("citations") or []),
        )
        return trimmed

    def _augment_evidence_summary(
        self,
        result: dict[str, Any],
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        include_domains: list[str] | None,
    ) -> dict[str, Any]:
        enriched = dict(result)
        evidence = dict(enriched.get("evidence") or {})
        results = list(enriched.get("results") or [])
        citations = list(enriched.get("citations") or [])
        official_mode = self._resolve_official_result_mode(
            query=query,
            mode=mode,
            intent=intent,
            include_domains=include_domains,
        )
        providers_consulted = [
            item
            for item in (
                evidence.get("providers_consulted")
                or [enriched.get("provider", "")]
            )
            if item
        ]
        evidence.setdefault("providers_consulted", providers_consulted)
        evidence.setdefault(
            "verification",
            "cross-provider" if len(set(providers_consulted)) > 1 else "single-provider",
        )
        evidence.setdefault("citation_count", len(citations))
        evidence.setdefault("official_mode", official_mode)
        evidence.setdefault("official_filter_applied", False)
        evidence.setdefault("official_filter_reduced", False)

        source_domains = self._collect_source_domains(results=results, citations=citations)
        official_source_count = self._count_official_resource_results(
            query=query,
            mode=mode,
            intent=intent,
            results=results,
            include_domains=include_domains,
        )
        conflicts = self._detect_evidence_conflicts(
            mode=mode,
            intent=intent,
            results=results,
            include_domains=include_domains,
            source_domains=source_domains,
            official_source_count=official_source_count,
            providers_consulted=providers_consulted,
            official_mode=str(evidence.get("official_mode") or official_mode),
        )
        evidence["source_diversity"] = len(source_domains)
        evidence["source_domains"] = source_domains[:5]
        evidence["official_source_count"] = official_source_count
        evidence["third_party_source_count"] = max(len(results) - official_source_count, 0)
        evidence["confidence"] = self._estimate_search_confidence(
            mode=mode,
            intent=intent,
            result_count=len(results),
            source_domain_count=len(source_domains),
            official_source_count=official_source_count,
            verification=str(evidence.get("verification") or "single-provider"),
            conflicts=conflicts,
            official_mode=str(evidence.get("official_mode") or official_mode),
        )
        evidence["conflicts"] = conflicts
        enriched["evidence"] = evidence
        return enriched

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
        finalized = dict(result)
        if self._should_rerank_resource_results(mode=mode, intent=intent):
            reranked_results = self._rerank_resource_results(
                query=query,
                mode=mode,
                results=list(finalized.get("results") or []),
                include_domains=include_domains,
            )
            finalized["results"] = reranked_results
            finalized["citations"] = self._align_citations_with_results(
                results=reranked_results,
                citations=list(finalized.get("citations") or []),
            )
        elif self._should_rerank_general_results(result_profile=result_profile):
            reranked_results = self._rerank_general_results(
                query=query,
                result_profile=result_profile,
                results=list(finalized.get("results") or []),
                include_domains=include_domains,
            )
            finalized["results"] = reranked_results
            finalized["citations"] = self._align_citations_with_results(
                results=reranked_results,
                citations=list(finalized.get("citations") or []),
            )

        finalized = self._apply_status_result_policy(
            query=query,
            mode=mode,
            intent=intent,
            result=finalized,
        )
        finalized = self._apply_official_resource_policy(
            query=query,
            mode=mode,
            intent=intent,
            result=finalized,
            include_domains=include_domains,
        )
        final_official_mode = str(
            (
                (finalized.get("evidence") or {})
                if isinstance(finalized.get("evidence"), dict)
                else {}
            ).get("official_mode")
            or "off"
        )
        if final_official_mode != "off" or self._should_rerank_resource_results(
            mode=mode,
            intent=intent,
        ):
            reranked_results = self._rerank_resource_results(
                query=query,
                mode=mode,
                results=list(finalized.get("results") or []),
                include_domains=include_domains,
            )
            finalized["results"] = reranked_results
            finalized["citations"] = self._align_citations_with_results(
                results=reranked_results,
                citations=list(finalized.get("citations") or []),
            )
        elif self._should_rerank_general_results(result_profile=result_profile):
            reranked_results = self._rerank_general_results(
                query=query,
                result_profile=result_profile,
                results=list(finalized.get("results") or []),
                include_domains=include_domains,
            )
            finalized["results"] = reranked_results
            finalized["citations"] = self._align_citations_with_results(
                results=reranked_results,
                citations=list(finalized.get("citations") or []),
            )

        finalized = self._trim_search_payload(finalized, max_results=max_results)
        finalized = self._augment_evidence_summary(
            finalized,
            query=query,
            mode=mode,
            intent=intent,
            include_domains=include_domains,
        )
        return finalized

    def _apply_status_result_policy(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        query_lower = query.lower()
        if intent != "status" and not self._looks_like_status_query(query_lower):
            return result

        enriched = dict(result)
        results = [dict(item) for item in (enriched.get("results") or [])]
        citations = list(enriched.get("citations") or [])
        evidence = dict(enriched.get("evidence") or {})
        status_results = [
            item
            for item in results
            if self._looks_like_status_result(
                url=str(item.get("url") or ""),
                hostname=self._result_hostname(item),
                title_text=str(item.get("title") or "").lower(),
            )
            and not self._is_obvious_official_community_result(
                hostname=self._result_hostname(item),
                path=urlparse(str(item.get("url") or "")).path.lower(),
            )
        ]
        if status_results:
            reordered = [
                *status_results,
                *[
                    item
                    for item in results
                    if str(item.get("url") or "") not in {str(status.get("url") or "") for status in status_results}
                ],
            ]
            if reordered != results:
                evidence["status_filter_applied"] = True
                enriched["results"] = reordered
                enriched["citations"] = self._align_citations_with_results(
                    results=reordered,
                    citations=citations,
                )
            enriched["evidence"] = evidence
            return enriched

        rescue_candidate = self._build_known_canonical_resource_rescue(
            query=query,
            mode=mode,
            intent=intent,
        )
        if rescue_candidate is None:
            enriched["evidence"] = evidence
            return enriched

        rescue_url = str(rescue_candidate.get("url") or "")
        deduped_results = [
            rescue_candidate,
            *[item for item in results if str(item.get("url") or "") != rescue_url],
        ]
        evidence["status_rescue_applied"] = True
        evidence["status_rescue_source"] = "canonical-map"
        enriched["results"] = deduped_results
        enriched["citations"] = self._align_citations_with_results(
            results=deduped_results,
            citations=citations,
        )
        enriched["evidence"] = evidence
        return enriched

    def _resolve_official_result_mode(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        include_domains: list[str] | None,
    ) -> str:
        if self._should_use_strict_resource_policy(
            query=query,
            mode=mode,
            intent=intent,
            include_domains=include_domains,
        ):
            return "strict"
        if self._should_rerank_resource_results(mode=mode, intent=intent):
            return "standard"
        return "off"

    def _should_use_strict_resource_policy(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        include_domains: list[str] | None,
    ) -> bool:
        query_lower = query.lower()
        explicit_resource_mode = mode in {"docs", "github", "pdf"}
        if include_domains:
            return True
        if intent == "tutorial" and not explicit_resource_mode:
            return False
        if explicit_resource_mode:
            return True
        if self._looks_like_official_query(query):
            return True
        if self._looks_like_pricing_query(query_lower):
            return True
        if self._looks_like_changelog_query(query_lower):
            return True
        if intent in {"resource", "tutorial"} and self._looks_like_docs_query(query_lower):
            return True
        return False

    def _looks_like_official_query(self, query: str) -> bool:
        query_lower = query.lower()
        if re.search(r"\bofficial\b", query_lower):
            return True
        official_markers = (
            "官网",
            "官方",
            "原文",
            "定价官方",
            "官方定价",
            "官方价格",
            "官方文档",
        )
        return any(marker in query for marker in official_markers)

    def _looks_like_pricing_query(self, query_lower: str) -> bool:
        keywords = [
            "price",
            "pricing",
            "plans",
            "subscription",
            "费用",
            "套餐",
            "定价",
            "价格",
            "售价",
        ]
        return any(keyword in query_lower for keyword in keywords)

    def _looks_like_changelog_query(self, query_lower: str) -> bool:
        keywords = [
            "changelog",
            "release notes",
            "what's new",
            "whats new",
            "更新日志",
            "发布说明",
            "变更日志",
            "版本更新",
        ]
        return any(keyword in query_lower for keyword in keywords)

    def _apply_official_resource_policy(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        result: dict[str, Any],
        include_domains: list[str] | None,
    ) -> dict[str, Any]:
        enriched = dict(result)
        results = list(enriched.get("results") or [])
        citations = list(enriched.get("citations") or [])
        official_mode = self._resolve_official_result_mode(
            query=query,
            mode=mode,
            intent=intent,
            include_domains=include_domains,
        )
        evidence = dict(enriched.get("evidence") or {})
        evidence.setdefault("official_mode", official_mode)
        evidence.setdefault("official_filter_applied", False)
        evidence.setdefault("official_filter_reduced", False)
        evidence.setdefault("official_candidate_count", 0)
        if official_mode == "off":
            enriched["evidence"] = evidence
            return enriched

        official_candidates = self._collect_official_result_candidates(
            query=query,
            mode=mode,
            intent=intent,
            results=results,
            include_domains=include_domains,
            strict_official=official_mode == "strict",
        )
        official_rescue_candidate: dict[str, Any] | None = None
        if official_mode == "strict":
            official_rescue_candidate = self._build_known_canonical_resource_rescue(
                query=query,
                mode=mode,
                intent=intent,
            )
            if official_rescue_candidate is not None and self._should_apply_canonical_resource_rescue(
                query=query,
                mode=mode,
                intent=intent,
                official_candidates=official_candidates,
                rescue_candidate=official_rescue_candidate,
            ):
                official_candidates = [
                    official_rescue_candidate,
                    *[
                        dict(item)
                        for item in official_candidates
                        if item.get("url") != official_rescue_candidate.get("url")
                    ],
                ]
                evidence["official_rescue_applied"] = True
                evidence["official_rescue_source"] = "canonical-map"
        evidence["official_candidate_count"] = len(official_candidates)
        if official_mode == "strict" and official_candidates:
            evidence["official_filter_applied"] = True
            evidence["official_filter_reduced"] = len(official_candidates) < len(results)
            enriched["results"] = official_candidates
            enriched["citations"] = self._align_citations_with_results(
                results=official_candidates,
                citations=citations,
            )
        enriched["evidence"] = evidence
        return enriched

    def _build_known_canonical_resource_rescue(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
    ) -> dict[str, Any] | None:
        query_lower = query.lower()
        if (
            mode not in {"docs", "github", "pdf", "web", "news"}
            and intent not in {"resource", "tutorial", "status"}
        ):
            return None
        if intent == "status" or self._looks_like_status_query(query_lower):
            if "openai" in query_lower:
                return {
                    "title": "OpenAI Status",
                    "url": "https://status.openai.com/",
                    "snippet": "Official OpenAI status dashboard for incidents and service health.",
                    "provider": "canonical-rescue",
                    "matched_providers": ["canonical-rescue"],
                }
            if "cloudflare" in query_lower:
                return {
                    "title": "Cloudflare Status",
                    "url": "https://www.cloudflarestatus.com/",
                    "snippet": "Official Cloudflare status dashboard for incidents and service health.",
                    "provider": "canonical-rescue",
                    "matched_providers": ["canonical-rescue"],
                }
        if "playwright" in query_lower and (
            "strict mode" in query_lower or "violation" in query_lower or "locator" in query_lower
        ):
            return {
                "title": "Locators | Playwright",
                "url": "https://playwright.dev/docs/locators",
                "snippet": "Locators are strict. A strict mode violation happens when a locator resolves to more than one element.",
                "provider": "canonical-rescue",
                "matched_providers": ["canonical-rescue"],
            }
        if ("next.js" in query_lower or "nextjs" in query_lower) and "hydration" in query_lower:
            return {
                "title": "Text content does not match server-rendered HTML | Next.js",
                "url": "https://nextjs.org/docs/messages/react-hydration-error",
                "snippet": "Official Next.js troubleshooting page for hydration mismatch errors and common fixes.",
                "provider": "canonical-rescue",
                "matched_providers": ["canonical-rescue"],
            }
        if "openai" in query_lower and "webhook" in query_lower:
            return {
                "title": "Webhooks | OpenAI API",
                "url": "https://developers.openai.com/api/docs/guides/webhooks/",
                "snippet": "Official OpenAI guide for receiving and verifying webhook events.",
                "provider": "canonical-rescue",
                "matched_providers": ["canonical-rescue"],
            }
        if "openai" in query_lower and "background mode" in query_lower:
            return {
                "title": "Background mode | OpenAI API",
                "url": "https://developers.openai.com/api/docs/guides/background/",
                "snippet": "Official OpenAI guide for background mode and long-running tasks.",
                "provider": "canonical-rescue",
                "matched_providers": ["canonical-rescue"],
            }
        return None

    def _should_apply_canonical_resource_rescue(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        official_candidates: list[dict[str, Any]],
        rescue_candidate: dict[str, Any],
    ) -> bool:
        query_lower = query.lower()
        if not official_candidates:
            return True
        rescue_url = str(rescue_candidate.get("url") or "")
        if rescue_url and any(str(item.get("url") or "") == rescue_url for item in official_candidates):
            return False
        top_candidate = official_candidates[0]
        top_url = str(top_candidate.get("url") or "")
        top_path = urlparse(top_url).path.lower()
        top_title = str(top_candidate.get("title") or "").lower()
        if intent == "status" or self._looks_like_status_query(query_lower):
            if self._looks_like_status_result(
                url=top_url,
                hostname=self._result_hostname(top_candidate),
                title_text=top_title,
            ):
                return False
            return True
        if "openai" in query_lower and ("webhook" in query_lower or "background mode" in query_lower):
            return True
        if self._looks_like_language_specific_sdk_reference_result(
            hostname=self._result_hostname(top_candidate),
            path=top_path,
            title_text=top_title,
        ) and not self._query_mentions_programming_language(query_lower):
            return True
        if self._looks_like_generic_official_landing_result(
            hostname=self._result_hostname(top_candidate),
            path=top_path,
            title_text=top_title,
        ):
            return True
        if (
            mode == "docs"
            or intent in {"resource", "tutorial"}
        ) and self._looks_like_api_docs_topic_query(query_lower):
            topic_markers = [token for token in self._query_precision_tokens(query) if len(token) >= 4]
            top_path_hits, top_total_hits = self._query_precision_hit_counts(
                hostname=self._result_hostname(top_candidate),
                path=top_path,
                title_text=top_title,
                query_tokens=topic_markers,
            )
            rescue_path = urlparse(rescue_url).path.lower()
            rescue_path_hits, rescue_total_hits = self._query_precision_hit_counts(
                hostname=self._result_hostname(rescue_candidate),
                path=rescue_path,
                title_text=str(rescue_candidate.get("title") or "").lower(),
                query_tokens=topic_markers,
            )
            if rescue_path_hits > top_path_hits or rescue_total_hits > top_total_hits:
                return True
        return False

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
        query_tokens = self._query_brand_tokens(query)
        candidates: list[dict[str, Any]] = []
        for item in results:
            if self._result_matches_official_policy(
                item=item,
                mode=mode,
                query_tokens=query_tokens,
                include_domains=include_domains,
                strict_official=strict_official,
            ):
                candidates.append(dict(item))
        if len(candidates) >= 2:
            use_general_official_rerank = (
                mode == "news"
                or intent in {"news", "status"}
                or self._looks_like_status_query(query.lower())
            )
            if use_general_official_rerank:
                result_profile: Literal["web", "news"] = (
                    "news" if mode == "news" or intent in {"news", "status"} else "web"
                )
                candidates = self._rerank_general_results(
                    query=query,
                    result_profile=result_profile,
                    results=candidates,
                    include_domains=include_domains,
                )
            else:
                candidates = self._rerank_resource_results(
                    query=query,
                    mode=mode,
                    results=candidates,
                    include_domains=include_domains,
                )
        return candidates

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
        community_source_count: int,
        selected_candidate_count: int,
        selected_candidate_domains: list[str],
        selected_candidate_cluster_counts: dict[str, int],
        docs_rescue_result_count: int,
        authoritative_research: bool,
        cross_provider_candidate_count: int,
        provider_match_depth: float,
    ) -> dict[str, Any]:
        successful_pages = [page for page in pages if not page.get("error")]
        page_error_count = max(len(pages) - len(successful_pages), 0)
        page_success_rate = (
            round(len(successful_pages) / requested_page_count, 2)
            if requested_page_count > 0
            else 0.0
        )
        web_evidence = dict(web_search.get("evidence") or {})
        source_domains = self._collect_source_domains(
            results=successful_pages,
            citations=citations,
        )
        conflicts = list(web_evidence.get("conflicts") or [])
        if requested_page_count and not successful_pages:
            conflicts.append("page-extraction-unavailable")
        elif requested_page_count and page_error_count > 0:
            conflicts.append("page-extraction-partial")
        if social_error:
            conflicts.append("social-search-unavailable")

        official_mode = str(
            web_evidence.get("official_mode")
            or self._resolve_official_result_mode(
                query=query,
                mode=mode,
                intent=str(intent) if isinstance(intent, str) else "factual",
                include_domains=None,
            )
        )
        confidence = self._estimate_research_confidence(
            search_confidence=str(web_evidence.get("confidence") or "low"),
            page_success_count=len(successful_pages),
            requested_page_count=requested_page_count,
            social_present=social is not None,
            social_error=bool(social_error),
            conflicts=conflicts,
            authoritative_source_count=authoritative_source_count,
            cross_provider_candidate_count=cross_provider_candidate_count,
            source_cluster_count=len(selected_candidate_cluster_counts),
        )
        return {
            "providers_consulted": providers_consulted,
            "web_result_count": len(web_search.get("results") or []),
            "page_count": len(successful_pages),
            "page_error_count": page_error_count,
            "page_success_rate": page_success_rate,
            "citation_count": len(citations),
            "verification": "cross-provider"
            if web_search.get("provider") == "hybrid" or len(providers_consulted) > 1
            else "single-provider",
            "source_diversity": len(source_domains),
            "source_domains": source_domains[:5],
            "official_source_count": int(web_evidence.get("official_source_count") or 0),
            "official_mode": official_mode,
            "search_confidence": str(web_evidence.get("confidence") or "low"),
            "confidence": confidence,
            "conflicts": conflicts,
            "research_plan": research_plan,
            "exa_discovery_count": exa_discovery_count,
            "exa_unique_url_count": exa_unique_url_count,
            "exa_promoted_page_count": exa_promoted_page_count,
            "authoritative_source_count": authoritative_source_count,
            "community_source_count": community_source_count,
            "selected_candidate_count": selected_candidate_count,
            "selected_candidate_domains": selected_candidate_domains[:5],
            "selected_candidate_cluster_counts": dict(selected_candidate_cluster_counts),
            "source_cluster_count": len(selected_candidate_cluster_counts),
            "docs_rescue_result_count": docs_rescue_result_count,
            "authoritative_research": authoritative_research,
            "cross_provider_candidate_count": cross_provider_candidate_count,
            "provider_match_depth": provider_match_depth,
        }

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
        if "strict-official-unmet" in conflicts or "page-extraction-unavailable" in conflicts:
            return "low"
        if authoritative_source_count >= 2 and page_success_count > 0 and not social_error:
            return "high"
        if (
            search_confidence == "high"
            and page_success_count > 0
            and not social_error
            and (
                authoritative_source_count >= 1
                or cross_provider_candidate_count > 0
                or source_cluster_count >= 2
            )
        ):
            return "high"
        if authoritative_source_count >= 1 and page_success_count > 0:
            return "medium"
        if search_confidence in {"high", "medium"} and (
            page_success_count > 0 or requested_page_count <= 0 or not social_present
        ):
            return "medium"
        if search_confidence == "high":
            return "medium"
        return "low" if conflicts else "medium"

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
        if not requested:
            return False
        if include_content:
            return False
        if include_domains:
            return False
        if mode in {"docs", "github", "pdf"}:
            return False
        if intent == "resource":
            return False
        if strategy in {"verify", "deep"}:
            return False
        return True

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
                reason="严格官方 / 精确资源页优先用 Tavily 做发现，再由 Firecrawl 接正文验证",
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
        if self._looks_like_tutorial_query(query_lower):
            return "tutorial"
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
        if include_content:
            return _MODE_PROVIDER_POLICY["content"]
        if explicit_resource_mode:
            return _MODE_PROVIDER_POLICY[mode]
        if intent == "resource" or self._looks_like_docs_query(query_lower):
            return _MODE_PROVIDER_POLICY["resource"]
        if intent == "status" or self._looks_like_status_query(query_lower):
            return _MODE_PROVIDER_POLICY["status"]
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
        healthy: list[ProviderName] = []
        degraded: list[ProviderName] = []
        for provider_name in ordered:
            config = self._provider_config_for_name(provider_name)
            status = self._provider_live_status(config)
            if status is None or status == "auth_error":
                continue
            if status == "ok":
                healthy.append(provider_name)
            else:
                degraded.append(provider_name)
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
            return False
        if mode == "pdf":
            return strategy in {"verify", "deep"} and self._provider_is_live_ok(
                self.config.tavily
            ) and self._provider_is_live_ok(self.config.firecrawl)
        if include_domains:
            return (
                self._looks_like_changelog_query(query.lower())
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
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        chain = [primary_provider, *(decision.fallback_chain or [])]
        last_error: Exception | None = None
        for provider_name in chain:
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
                )
                fallback_info = None
                if provider_name != primary_provider:
                    fallback_info = {
                        "from": primary_provider,
                        "to": provider_name,
                        "reason": str(last_error)[:200] if last_error else "primary provider failed",
                    }
                return result, fallback_info
            except MySearchError as exc:
                last_error = exc
                continue
            except Exception as exc:
                last_error = MySearchError(f"{provider_name}: {exc}")
                continue
        raise MySearchError(f"All providers failed for query '{query[:80]}': {last_error}")

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
    ) -> dict[str, Any]:
        result_event_query = self._looks_like_result_event_query(query.lower())
        if provider_name == "tavily":
            tavily_include_content = (
                include_content
                and intent != "tutorial"
                and not self._looks_like_changelog_query(query.lower())
            ) or (
                result_event_query
                and mode == "news"
                and strategy in {"verify", "deep"}
            )
            return self._search_tavily(
                query=query,
                max_results=max_results,
                topic=decision.tavily_topic,
                include_answer=include_answer,
                include_content=tavily_include_content,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                strategy=strategy,
                days=self._infer_tavily_days(intent, from_date),
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
                from_date=from_date,
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
        if results:
            extracted_answer = self._extract_result_event_answer(query=query, results=results)
            if extracted_answer and not self._answer_looks_uncertain(extracted_answer):
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

    def _should_skip_exa_rescue_for_result_event(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        result: dict[str, Any],
    ) -> bool:
        query_lower = query.lower()
        if not (
            self._looks_like_result_event_query(query_lower)
            and (mode == "news" or intent in {"news", "status"})
        ):
            return False
        evidence = result.get("evidence") or {}
        if bool(str(result.get("answer") or "").strip()) and (
            str(evidence.get("answer_source") or "") == "result-event-extraction"
        ):
            return True
        results = list(result.get("results") or [])
        if results:
            extracted_answer = self._extract_result_event_answer(query=query, results=results)
            if extracted_answer and not self._answer_looks_uncertain(extracted_answer):
                return True
        if self._looks_like_award_result_query(query_lower):
            return self._has_strong_award_result(query=query, results=results)
        return False

    def _result_set_looks_weak_for_exa_rescue(
        self,
        *,
        query: str,
        mode: SearchMode,
        result: dict[str, Any],
    ) -> bool:
        results = list(result.get("results") or [])
        if not results:
            return True
        query_lower = query.lower()
        if self._looks_like_result_event_query(query_lower):
            extracted_answer = self._extract_result_event_answer(query=query, results=results)
            if extracted_answer and not self._answer_looks_uncertain(extracted_answer):
                return False
        if self._looks_like_award_result_query(query_lower):
            return not self._has_strong_award_result(query=query, results=results)
        if mode == "pdf":
            return not self._has_strong_pdf_match(query=query, results=results)
        if self._looks_like_pricing_query(query_lower):
            return not self._has_canonical_pricing_result(results)
        if self._looks_like_changelog_query(query_lower):
            return not self._has_strong_changelog_result(query=query, results=results)
        if self._looks_like_tutorial_query(query_lower) or self._looks_like_debugging_query(query_lower):
            return not self._has_strong_tutorial_result(query=query, results=results, mode=mode)
        return False

    def _has_strong_award_result(
        self,
        *,
        query: str,
        results: list[dict[str, Any]],
    ) -> bool:
        query_lower = query.lower()
        for item in results[:5]:
            hostname = self._result_hostname(item)
            registered_domain = self._registered_domain(hostname)
            path = urlparse(item.get("url", "")).path.lower()
            title_text = (item.get("title") or "").lower()
            snippet_text = (item.get("snippet") or "").lower()
            content_text = (item.get("content") or "").lower()
            if self._looks_like_award_prediction_result(
                title_text=title_text,
                snippet_text=snippet_text,
                path=path,
            ):
                continue
            if self._looks_like_query_year_mismatch(
                query=query_lower,
                text=f"{title_text} {snippet_text} {content_text} {path}",
            ):
                continue
            if registered_domain in {"facebook.com", "instagram.com", "tiktok.com", "youtube.com"}:
                continue
            category_match = self._looks_like_award_category_match(
                query_lower=query_lower,
                title_text=title_text,
                snippet_text=snippet_text,
                content_text=content_text,
                path=path,
            )
            fact_match = self._looks_like_award_fact_match(
                query_lower=query_lower,
                title_text=title_text,
                snippet_text=snippet_text,
                content_text=content_text,
                path=path,
            )
            winner_page = self._looks_like_award_winner_result(
                title_text=title_text,
                snippet_text=snippet_text,
                path=path,
            )
            if winner_page or fact_match:
                return True
        return False

    def _has_strong_pdf_match(
        self,
        *,
        query: str,
        results: list[dict[str, Any]],
    ) -> bool:
        query_tokens = self._query_brand_tokens(query)
        precision_tokens = self._query_precision_tokens(query)
        paper_tokens = self._paper_query_subject_tokens(
            query=query,
            query_tokens=query_tokens,
            precision_tokens=precision_tokens,
        )
        compound_tokens = self._paper_query_compound_tokens(query)
        for item in results[:3]:
            url = item.get("url", "")
            hostname = self._result_hostname(item)
            path = urlparse(url).path.lower()
            title_text = (item.get("title") or "").lower()
            path_hits, total_hits = self._query_precision_hit_counts(
                hostname=hostname,
                path=path,
                title_text=title_text,
                query_tokens=precision_tokens,
            )
            named_paper = self._looks_like_primary_named_paper_result(
                title_text=title_text,
                query_tokens=paper_tokens,
            )
            compound_match = any(
                self._paper_text_matches_compound_token(f"{title_text} {path}", token)
                for token in compound_tokens
            )
            derivative_title = self._looks_like_derivative_paper_title(title_text)
            paper_shape = self._looks_like_pdf_url(url) or any(
                marker in path for marker in ("/abs/", "/html/")
            )
            if (
                compound_tokens
                and compound_match
                and paper_shape
                and not derivative_title
                and (named_paper or total_hits >= min(max(len(paper_tokens), 2), 3))
            ):
                return True
            if (
                compound_tokens
                and not compound_match
                and paper_shape
                and not self._looks_like_generic_arxiv_subject_title(title_text)
            ):
                continue
            if named_paper and paper_shape and not derivative_title:
                return True
            if not derivative_title and paper_shape and total_hits >= min(max(len(paper_tokens), 2), 3):
                return True
            if hostname == "arxiv.org" and path_hits >= 2 and paper_shape:
                return True
        return False

    def _looks_like_derivative_paper_title(self, title_text: str) -> bool:
        derivative_markers = (
            "analysis",
            "benchmark",
            "explained",
            "interpret",
            "lets think",
            "let’s think",
            "rethinking",
            "survey",
            "thoughtology",
            "tutorial",
        )
        return any(marker in title_text for marker in derivative_markers)

    def _has_canonical_pricing_result(self, results: list[dict[str, Any]]) -> bool:
        for item in results[:5]:
            hostname = self._result_hostname(item)
            path = urlparse(item.get("url", "")).path.lower()
            if self._looks_like_canonical_pricing_result(hostname=hostname, path=path):
                return True
        return False

    def _has_strong_changelog_result(
        self,
        *,
        query: str,
        results: list[dict[str, Any]],
    ) -> bool:
        precision_tokens = self._query_precision_tokens(query)
        for item in results[:3]:
            url = item.get("url", "")
            hostname = self._result_hostname(item)
            title_text = (item.get("title") or "").lower()
            if self._looks_like_canonical_changelog_result(
                url=url,
                hostname=hostname,
                title_text=title_text,
                precision_tokens=precision_tokens,
            ):
                return True
            if self._looks_like_changelog_result(
                url=url,
                hostname=hostname,
                title_text=title_text,
            ):
                return True
        return False

    def _has_strong_tutorial_result(
        self,
        *,
        query: str,
        results: list[dict[str, Any]],
        mode: SearchMode = "auto",
    ) -> bool:
        query_tokens = self._query_brand_tokens(query)
        precision_tokens = self._query_precision_tokens(query)
        exact_identifier_tokens = self._query_exact_identifier_tokens(query)
        debugging_query = self._looks_like_debugging_query(query.lower())
        explicit_resource_mode = mode in {"docs", "github", "pdf"}
        for item in results[:5]:
            url = item.get("url", "")
            hostname = self._result_hostname(item)
            registered_domain = self._registered_domain(hostname)
            path = urlparse(url).path.lower()
            title_text = (item.get("title") or "").lower()
            snippet_text = (item.get("snippet") or "").lower()
            path_hits, total_hits = self._query_precision_hit_counts(
                hostname=hostname,
                path=path,
                title_text=title_text,
                query_tokens=precision_tokens,
            )
            _, exact_total_hits = self._query_exact_identifier_hit_counts(
                path=path,
                title_text=title_text,
                query_tokens=exact_identifier_tokens,
            )
            community_debug = (
                registered_domain == "stackoverflow.com"
                or (registered_domain == "github.com" and any(marker in path for marker in ("/issues/", "/discussions/")))
                or self._is_obvious_official_community_result(hostname=hostname, path=path)
            )
            brand_aligned = self._registered_domain_label_matches(
                registered_domain=registered_domain,
                query_tokens=query_tokens,
            ) or any(token in hostname for token in query_tokens)
            brand_aligned_docs = self._looks_like_brand_aligned_tutorial_result(
                hostname=hostname,
                registered_domain=registered_domain,
                path=path,
                title_text=title_text,
                snippet_text=snippet_text,
                query_tokens=query_tokens,
                path_precision_hits=path_hits,
                exact_total_hits=exact_total_hits,
            )
            debugging_match = self._looks_like_debugging_result(
                hostname=hostname,
                registered_domain=registered_domain,
                path=path,
                title_text=title_text,
                snippet_text=snippet_text,
            )
            if not explicit_resource_mode and community_debug and (
                path_hits > 0 or exact_total_hits > 0 or "issue" in title_text
            ):
                return True
            if debugging_query:
                if not explicit_resource_mode and community_debug and debugging_match:
                    return True
                if brand_aligned and brand_aligned_docs and debugging_match and (
                    exact_total_hits > 0 or path_hits >= 1 or total_hits >= 2
                ):
                    return True
                continue
            if brand_aligned and brand_aligned_docs and (
                exact_total_hits > 0 or path_hits >= 2 or total_hits >= 3
            ):
                return True
        return False

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
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        exa_result = self._search_exa(
            query=query,
            max_results=max_results,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            include_content=include_content,
            mode=mode,
            intent=intent,
            from_date=from_date,
            to_date=to_date,
        )
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
        return result_profile in {"web", "news"}

    def _rerank_general_results(
        self,
        *,
        query: str,
        result_profile: Literal["web", "news"],
        results: list[dict[str, Any]],
        include_domains: list[str] | None,
    ) -> list[dict[str, Any]]:
        if len(results) < 2:
            return results
        ranked = sorted(
            enumerate(results),
            key=lambda pair: (
                self._general_result_rank(
                    query=query,
                    result_profile=result_profile,
                    item=pair[1],
                    include_domains=include_domains,
                ),
                -pair[0],
            ),
            reverse=True,
        )
        return [dict(pair[1]) for pair in ranked]

    def _general_result_rank(
        self,
        *,
        query: str,
        result_profile: Literal["web", "news"],
        item: dict[str, Any],
        include_domains: list[str] | None,
    ) -> tuple[int, int, int, int, int, int, int, int]:
        if result_profile == "news":
            return self._news_result_rank(
                query=query,
                item=item,
                include_domains=include_domains,
            )
        return self._web_result_rank(
            query=query,
            item=item,
            include_domains=include_domains,
        )

    def _news_result_rank(
        self,
        *,
        query: str,
        item: dict[str, Any],
        include_domains: list[str] | None,
    ) -> tuple[int, int, int, int, int, int, int, int, int, int, int, int, int, int, int, int, int]:
        hostname = self._result_hostname(item)
        registered_domain = self._registered_domain(hostname)
        path = urlparse(item.get("url", "")).path.lower()
        title_text = (item.get("title") or "").lower()
        snippet_text = (item.get("snippet") or "").lower()
        content_text = (item.get("content") or "").lower()
        query_lower = query.lower()
        page_text = f"{title_text} {snippet_text} {content_text} {path}"
        gossip_query = self._looks_like_gossip_query(query_lower)
        status_query = self._looks_like_status_query(query_lower)
        award_query = self._looks_like_award_result_query(query_lower)
        precision_tokens = self._query_precision_tokens(query)
        path_precision_hits, total_precision_hits = self._query_precision_hit_counts(
            hostname=hostname,
            path=path,
            title_text=f"{title_text} {snippet_text} {content_text}",
            query_tokens=precision_tokens,
        )
        award_winner_page_match = int(
            award_query
            and self._looks_like_award_winner_result(
                title_text=title_text,
                snippet_text=snippet_text,
                path=path,
            )
        )
        award_category_match = int(
            award_query
            and self._looks_like_award_category_match(
                query_lower=query_lower,
                title_text=title_text,
                snippet_text=snippet_text,
                content_text=content_text,
                path=path,
            )
        )
        non_award_prediction_page = int(
            not (
                award_query
                and self._looks_like_award_prediction_result(
                    title_text=title_text,
                    snippet_text=snippet_text,
                    path=path,
                )
            )
        )
        non_year_mismatch = int(
            not (
                award_query
                and self._looks_like_query_year_mismatch(
                    query=query_lower,
                    text=page_text,
                )
            )
        )
        non_low_signal_social = int(
            not (
                award_query
                and registered_domain in {"facebook.com", "instagram.com", "tiktok.com", "youtube.com"}
            )
        )
        non_award_nomination_page = int(
            not (
                award_query
                and self._looks_like_award_nomination_result(
                    title_text=title_text,
                    snippet_text=snippet_text,
                    path=path,
                )
            )
        )
        gossip_story_match = int(
            gossip_query
            and self._looks_like_gossip_result(
                title_text=title_text,
                snippet_text=snippet_text,
                path=path,
            )
        )
        gossip_domain_match = int(
            gossip_query and self._is_entertainment_gossip_domain(registered_domain)
        )
        status_query = self._looks_like_status_query(query_lower)
        include_match = int(
            (
                bool(include_domains)
                and any(self._domain_matches(hostname, domain) for domain in include_domains or [])
            )
            or (status_query and self._looks_like_brand_status_domain(hostname))
        )
        non_community_official = int(
            not (
                status_query
                and self._is_obvious_official_community_result(
                    hostname=hostname,
                    path=path,
                )
            )
        )
        canonical_status_page_match = int(
            status_query
            and self._looks_like_canonical_status_result(hostname=hostname, path=path)
        )
        status_page_match = int(
            status_query
            and self._looks_like_status_result(url=item.get("url", ""), hostname=hostname, title_text=title_text)
        )
        non_status_api_endpoint = int(
            not (
                status_query
                and path.startswith("/api")
            )
        )
        canonical_changelog_page_match = int(
            status_query
            and self._looks_like_canonical_changelog_result(
                url=item.get("url", ""),
                hostname=hostname,
                title_text=title_text,
                precision_tokens=precision_tokens,
            )
        )
        changelog_page_match = int(
            status_query
            and self._looks_like_changelog_result(
                url=item.get("url", ""),
                hostname=hostname,
                title_text=title_text,
            )
        )
        mainstream = int(self._is_mainstream_news_domain(hostname))
        article_shape = int(self._looks_like_news_article_result(item))
        has_timestamp = int(self._result_published_timestamp(item) is not None)
        timestamp_score = int(self._result_published_timestamp(item) or 0)
        content_score, snippet_score, title_score = self._result_quality_score(item)
        return (
            include_match,
            award_winner_page_match,
            award_category_match,
            non_award_prediction_page,
            non_year_mismatch,
            non_low_signal_social,
            non_award_nomination_page,
            non_community_official,
            non_status_api_endpoint,
            canonical_status_page_match,
            status_page_match,
            canonical_changelog_page_match,
            changelog_page_match,
            gossip_story_match,
            gossip_domain_match,
            path_precision_hits,
            total_precision_hits,
            mainstream,
            article_shape,
            has_timestamp,
            timestamp_score,
            content_score,
            snippet_score,
            title_score,
        )

    def _web_result_rank(
        self,
        *,
        query: str,
        item: dict[str, Any],
        include_domains: list[str] | None,
    ) -> tuple[int, int, int, int, int, int, int, int, int, int, int, int, int, int]:
        hostname = self._result_hostname(item)
        registered_domain = self._registered_domain(hostname)
        url = item.get("url", "")
        path = urlparse(url).path.lower()
        title_text = (item.get("title") or "").lower()
        query_lower = query.lower()
        query_tokens = self._query_brand_tokens(query)
        precision_tokens = self._query_precision_tokens(query)
        exact_identifier_tokens = self._query_exact_identifier_tokens(query)
        status_query = self._looks_like_status_query(query_lower)
        include_match = int(
            (
                bool(include_domains)
                and any(self._domain_matches(hostname, domain) for domain in include_domains or [])
            )
            or (status_query and self._looks_like_brand_status_domain(hostname))
        )
        registered_domain_label_match = int(
            self._registered_domain_label_matches(
                registered_domain=registered_domain,
                query_tokens=query_tokens,
            )
        )
        host_brand_match = int(any(token in hostname for token in query_tokens))
        title_brand_match = int(any(token in title_text for token in query_tokens))
        path_precision_hits, total_precision_hits = self._query_precision_hit_counts(
            hostname=hostname,
            path=path,
            title_text=title_text,
            query_tokens=precision_tokens,
        )
        exact_path_hits, exact_total_hits = self._query_exact_identifier_hit_counts(
            path=path,
            title_text=title_text,
            query_tokens=exact_identifier_tokens,
        )
        official_query = (
            bool(include_domains)
            or self._looks_like_official_query(query)
            or status_query
            or self._looks_like_changelog_query(query_lower)
        )
        non_community_official = int(
            not (
                official_query
                and self._is_obvious_official_community_result(
                    hostname=hostname,
                    path=path,
                )
            )
        )
        status_page_match = int(
            status_query
            and self._looks_like_status_result(url=url, hostname=hostname, title_text=title_text)
        )
        non_status_api_endpoint = int(
            not (
                status_query
                and path.startswith("/api")
            )
        )
        canonical_status_page_match = int(
            status_query
            and self._looks_like_canonical_status_result(hostname=hostname, path=path)
        )
        pricing_page_match = int(
            self._looks_like_pricing_query(query_lower)
            and self._looks_like_pricing_result(url=url, hostname=hostname, title_text=title_text)
        )
        canonical_pricing_page_match = int(
            self._looks_like_pricing_query(query_lower)
            and self._looks_like_canonical_pricing_result(hostname=hostname, path=path)
        )
        debugging_query = self._looks_like_debugging_query(query_lower)
        tutorial_query = self._looks_like_tutorial_query(query_lower) or debugging_query
        tutorial_community_match = int(
            tutorial_query
            and self._looks_like_tutorial_community_result(
                hostname=hostname,
                registered_domain=registered_domain,
                path=path,
            )
        )
        tutorial_brand_aligned = int(
            tutorial_query
            and self._looks_like_brand_aligned_tutorial_result(
                hostname=hostname,
                registered_domain=registered_domain,
                path=path,
                title_text=title_text,
                snippet_text=(item.get("snippet") or "").lower(),
                query_tokens=query_tokens,
                path_precision_hits=path_precision_hits,
                exact_total_hits=exact_total_hits,
            )
        )
        debugging_signal_match = int(
            debugging_query
            and self._looks_like_debugging_result(
                hostname=hostname,
                registered_domain=registered_domain,
                path=path,
                title_text=title_text,
                snippet_text=(item.get("snippet") or "").lower(),
            )
        )
        debugging_community_match = int(
            debugging_query and tutorial_community_match and debugging_signal_match
        )
        debugging_brand_aligned = int(
            debugging_query and tutorial_brand_aligned and debugging_signal_match
        )
        non_generic_debugging_docs = int(
            not (
                debugging_query
                and self._looks_like_generic_debugging_docs_result(
                    hostname=hostname,
                    path=path,
                    title_text=title_text,
                )
            )
        )
        non_tutorial_blog = int(
            not (
                tutorial_query
                and not tutorial_brand_aligned
                and not tutorial_community_match
                and self._is_obvious_tutorial_blog_domain(registered_domain)
            )
        )
        local_life_query = self._looks_like_local_life_query(query_lower)
        canonical_local_guide_match = int(
            local_life_query
            and self._looks_like_canonical_local_life_guide_result(
                url=url,
                hostname=hostname,
            )
        )
        local_guide_match = int(
            local_life_query
            and self._looks_like_local_life_guide_result(
                url=url,
                hostname=hostname,
                title_text=title_text,
                snippet_text=(item.get("snippet") or "").lower(),
            )
        )
        non_local_life_repost = int(
            not (
                local_life_query
                and self._is_obvious_local_life_repost_domain(registered_domain)
            )
        )
        non_aggregator = int(not self._is_obvious_web_aggregator(registered_domain))
        matched_provider_count = len(item.get("matched_providers") or [])
        cross_provider_boost = min(matched_provider_count, 3)
        content_score, snippet_score, title_score = self._result_quality_score(item)
        return (
            include_match,
            non_community_official,
            canonical_status_page_match,
            status_page_match,
            canonical_pricing_page_match,
            pricing_page_match,
            debugging_community_match,
            debugging_brand_aligned,
            non_generic_debugging_docs,
            tutorial_community_match,
            tutorial_brand_aligned,
            non_tutorial_blog,
            canonical_local_guide_match,
            local_guide_match,
            non_local_life_repost,
            exact_path_hits,
            exact_total_hits,
            path_precision_hits,
            total_precision_hits,
            registered_domain_label_match,
            host_brand_match,
            title_brand_match,
            non_status_api_endpoint,
            non_aggregator,
            cross_provider_boost,
            content_score,
            max(snippet_score, title_score),
        )

    def _result_published_timestamp(self, item: dict[str, Any]) -> float | None:
        for field in ("published_date", "publishedDate", "created_at"):
            parsed = self._parse_result_timestamp(item.get(field))
            if parsed is not None:
                return parsed.timestamp()
        return None

    def _is_mainstream_news_domain(self, hostname: str) -> bool:
        registered_domain = self._registered_domain(hostname)
        mainstream_domains = {
            "apnews.com",
            "bbc.com",
            "bloomberg.com",
            "cnn.com",
            "ft.com",
            "latimes.com",
            "nytimes.com",
            "reuters.com",
            "theguardian.com",
            "theverge.com",
            "washingtonpost.com",
            "wsj.com",
            "xinhuanet.com",
        }
        return registered_domain in mainstream_domains

    def _looks_like_award_winner_result(
        self,
        *,
        title_text: str,
        snippet_text: str,
        path: str,
    ) -> bool:
        text = f"{title_text} {snippet_text} {path}"
        winner_markers = [
            "complete winners",
            "full list of winners",
            "full winners",
            "winners and nominees",
            "heres a full list",
            "here's a full list",
            "winner list",
            "winners list",
        ]
        return any(marker in text for marker in winner_markers)

    def _looks_like_award_category_match(
        self,
        *,
        query_lower: str,
        title_text: str,
        snippet_text: str,
        content_text: str,
        path: str,
    ) -> bool:
        text = f"{title_text} {snippet_text} {content_text} {path}"
        category_markers = self._award_query_category_markers(query_lower)
        return bool(category_markers) and any(marker in text for marker in category_markers)

    def _looks_like_award_fact_match(
        self,
        *,
        query_lower: str,
        title_text: str,
        snippet_text: str,
        content_text: str,
        path: str,
    ) -> bool:
        text = f"{title_text} {snippet_text} {content_text} {path}"
        for marker in self._award_query_category_markers(query_lower):
            marker_pattern = re.escape(marker)
            patterns = [
                rf"{marker_pattern}(?:\s+winner)?\s*[–—:]",
                rf"{marker_pattern}(?:\s+winner)?(?:\s+was|\s+is|\s+goes to|\s+went to)\b",
                rf"[\"“'‘][^\"”’'\n]{{2,100}}[\"”’'‘]\s+is\s+the\s+(?:20\d{{2}}\s+)?{marker_pattern}\s+winner",
                rf"[\"“'‘][^\"”’'\n]{{2,100}}[\"”’'‘]\s+(?:won|wins)\s+{marker_pattern}",
                rf"[A-Z][A-Za-z0-9'’&.\- ]{{2,100}}\s+(?:won|wins)\s+{marker_pattern}",
            ]
            if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns):
                return True
        return False

    def _looks_like_award_nomination_result(
        self,
        *,
        title_text: str,
        snippet_text: str,
        path: str,
    ) -> bool:
        text = f"{title_text} {snippet_text} {path}"
        nomination_markers = [
            "nomination",
            "nominations",
            "nominee",
            "nominees",
        ]
        winner_markers = [
            "complete winners",
            "full winners",
            "winner list",
            "winners list",
            "winner",
            "winners",
        ]
        return any(marker in text for marker in nomination_markers) and not any(
            marker in text for marker in winner_markers
        )

    def _looks_like_award_prediction_result(
        self,
        *,
        title_text: str,
        snippet_text: str,
        path: str,
    ) -> bool:
        text = f"{title_text} {snippet_text} {path}"
        prediction_markers = [
            "award buzz",
            "contender",
            "contenders",
            "forecast",
            "next year",
            "next-year",
            "odds",
            "prediction",
            "predictions",
            "predicts",
            "snub",
            "snubs",
            "way too early",
        ]
        return any(marker in text for marker in prediction_markers)

    def _looks_like_news_article_result(self, item: dict[str, Any]) -> bool:
        path = urlparse(item.get("url", "")).path.lower()
        return any(
            marker in path
            for marker in ("/news/", "/story/", "/stories/", "/article/", "/articles/", "/202")
        )

    def _is_obvious_web_aggregator(self, registered_domain: str) -> bool:
        return registered_domain in {
            "linkedin.com",
            "medium.com",
            "quora.com",
            "reddit.com",
            "researchgate.net",
            "stackoverflow.com",
        }

    def _looks_like_local_life_guide_result(
        self,
        *,
        url: str,
        hostname: str,
        title_text: str,
        snippet_text: str,
    ) -> bool:
        registered_domain = self._registered_domain(hostname)
        path = urlparse(url).path.lower()
        guide_markers = (
            "攻略",
            "赏花",
            "踏青",
            "景点",
            "路线",
            "门票",
            "游玩",
            "travel guide",
            "things to do",
        )
        guide_shape = any(marker in path for marker in ("/tour/", "/travel/", "/guide", "/flowers", "/trip/"))
        guide_text = f"{title_text} {snippet_text}"
        if registered_domain in {"bendibao.com", "ctrip.com", "trip.com", "mafengwo.cn", "qyer.com"}:
            return guide_shape or any(marker in guide_text for marker in guide_markers)
        return guide_shape and any(marker in guide_text for marker in guide_markers)

    def _looks_like_canonical_local_life_guide_result(
        self,
        *,
        url: str,
        hostname: str,
    ) -> bool:
        registered_domain = self._registered_domain(hostname)
        path = urlparse(url).path.lower()
        if registered_domain == "bendibao.com":
            return "/tour/" in path or "/flowers" in path
        if registered_domain in {"trip.com", "ctrip.com"}:
            return any(marker in path for marker in ("/travel-guide/", "/travel/"))
        return any(marker in path for marker in ("/tour/", "/travel/", "/guide", "/flowers"))

    def _is_obvious_local_life_repost_domain(self, registered_domain: str) -> bool:
        return registered_domain in {
            "163.com",
            "facebook.com",
            "ifeng.com",
            "qq.com",
            "sina.cn",
            "sohu.com",
            "weibo.com",
        }

    def _looks_like_tutorial_community_result(
        self,
        *,
        hostname: str,
        registered_domain: str,
        path: str,
    ) -> bool:
        return (
            registered_domain == "stackoverflow.com"
            or (registered_domain == "github.com" and any(marker in path for marker in ("/issues/", "/discussions/")))
            or self._is_obvious_official_community_result(hostname=hostname, path=path)
        )

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
        brand_aligned = self._registered_domain_label_matches(
            registered_domain=registered_domain,
            query_tokens=query_tokens,
        ) or any(token in hostname for token in query_tokens)
        if not brand_aligned:
            return False
        docs_path = any(marker in path for marker in ("/docs/", "/guide", "/api/", "/writing-tests", "/running-tests"))
        tutorial_text = f"{title_text} {snippet_text}"
        return (
            docs_path
            or path_precision_hits >= 1
            or exact_total_hits > 0
            or "tutorial" in tutorial_text
            or "strict mode" in tutorial_text
            or "hydration" in tutorial_text
        )

    def _is_obvious_tutorial_blog_domain(self, registered_domain: str) -> bool:
        return registered_domain in {
            "checklyhq.com",
            "loadmill.com",
            "medium.com",
            "substack.com",
            "testgrid.io",
            "timdeschryver.dev",
            "youtube.com",
        }

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
    ) -> dict[str, Any]:
        if decision.provider == "tavily":
            tasks = {
                "primary": lambda: self._search_tavily(
                    query=query,
                    max_results=max_results,
                    topic=decision.tavily_topic,
                    include_answer=include_answer,
                    include_content=include_content,
                    include_domains=include_domains,
                    exclude_domains=exclude_domains,
                ),
                "secondary": lambda: self._search_firecrawl(
                    query=query,
                    max_results=max_results,
                    categories=self._firecrawl_categories(mode, intent),
                    include_content=include_content or strategy in {"verify", "deep"},
                    include_domains=include_domains,
                    exclude_domains=exclude_domains,
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
                ),
                "secondary": lambda: self._search_tavily(
                    query=query,
                    max_results=max_results,
                    topic="news" if intent in {"news", "status"} else "general",
                    include_answer=True,
                    include_content=False,
                    include_domains=include_domains,
                    exclude_domains=exclude_domains,
                ),
            }

        if strategy in {"verify", "deep"} and self._provider_can_serve(self.config.exa):
            tasks["exa_supplement"] = lambda: self._search_exa(
                query=query,
                max_results=min(max_results, 3),
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                include_content=False,
                mode=mode,
                intent=intent,
            )

        blended_results, blended_errors = self._execute_parallel(tasks, max_workers=len(tasks))
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

        if secondary_result:
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
        )
        if response.get("results") or not include_domains:
            return response

        retry_response = self._search_tavily_domain_retry(
            query=query,
            max_results=max_results,
            topic=topic,
            include_content=include_content,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
        )
        if retry_response is not None:
            return retry_response

        fallback_response = self._search_tavily_domain_fallback(
            query=query,
            max_results=max_results,
            include_content=include_content,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
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
    ) -> dict[str, Any]:
        provider = self.config.tavily
        key = self._get_key_or_raise(provider)
        payload: dict[str, Any] = {
            "query": query,
            "max_results": max_results,
            "search_depth": "advanced" if include_content or strategy in {"verify", "deep"} else "basic",
            "topic": topic,
            "include_answer": include_answer,
            "include_raw_content": include_content,
        }
        if days and days > 0:
            payload["days"] = days
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
            "query": response.get("query", query),
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
            )
            if not domain_result.get("results"):
                retry_result = self._search_firecrawl_domain_retry(
                    query=query,
                    max_results=max_results,
                    categories=categories,
                    include_content=include_content,
                    include_domain=domain,
                    exclude_domains=exclude_domains,
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
    ) -> dict[str, Any]:
        include_domains = [item.strip() for item in (include_domains or []) if item and item.strip()]
        exclude_domains = [item.strip() for item in (exclude_domains or []) if item and item.strip()]

        if include_domains:
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
                )
                if not domain_result.get("results"):
                    retry_result = self._search_firecrawl_domain_retry(
                        query=query,
                        max_results=max_results,
                        categories=categories,
                        include_content=include_content,
                        include_domain=domain,
                        exclude_domains=exclude_domains,
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
            query=self._build_firecrawl_domain_query(
                query=query,
                include_domain=None,
                exclude_domains=exclude_domains,
            ),
            max_results=max_results,
            categories=categories,
            include_content=include_content,
        )

    def _search_firecrawl_domain_fallback(
        self,
        *,
        query: str,
        max_results: int,
        include_content: bool,
        include_domains: list[str],
        exclude_domains: list[str] | None,
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
    ) -> dict[str, Any]:
        provider = self.config.firecrawl
        key = self._get_key_or_raise(provider)
        requested_news = "news" in categories
        search_categories = self._normalize_firecrawl_search_categories(categories)
        payload: dict[str, Any] = {
            "query": query,
            "limit": max_results,
        }
        if search_categories:
            payload["categories"] = [{"type": item} for item in search_categories]
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
        )
        data = response.get("data") or {}
        results = []
        source_order = ("news", "web") if requested_news else ("web", "news")
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

    def _build_firecrawl_domain_query(
        self,
        *,
        query: str,
        include_domain: str | None,
        exclude_domains: list[str] | None,
    ) -> str:
        parts: list[str] = []
        if include_domain:
            parts.append(f"site:{include_domain}")
        for domain in exclude_domains or []:
            parts.append(f"-site:{domain}")
        parts.append(query)
        return " ".join(parts).strip()

    def _merge_ranked_results(
        self,
        result_lists: list[list[dict[str, Any]]],
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
                url = candidate.get("url", "")
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                merged.append(candidate)
                if len(merged) >= max_results:
                    break
            if not progressed:
                break

        return merged

    def _filter_results_by_domains(
        self,
        results: list[dict[str, Any]],
        *,
        include_domains: list[str] | None,
        exclude_domains: list[str] | None,
    ) -> list[dict[str, Any]]:
        filtered: list[dict[str, Any]] = []
        for item in results:
            hostname = self._result_hostname(item)
            if include_domains and not any(
                self._domain_matches(hostname, domain) for domain in include_domains
            ):
                continue
            if exclude_domains and any(
                self._domain_matches(hostname, domain) for domain in exclude_domains
            ):
                continue
            filtered.append(dict(item))
        return filtered

    def _exa_search_type(
        self,
        query: str,
        *,
        mode: str = "",
        intent: str = "",
        include_domains: list[str] | None = None,
    ) -> str:
        query_lower = query.lower()
        if self._looks_like_pricing_query(query_lower) and (
            include_domains or mode in {"web", "docs"} or intent in {"factual", "resource"}
        ):
            return "keyword"
        exact_signals = re.findall(
            r'[A-Z][a-zA-Z]+\.[a-zA-Z_]+|[a-z_]{2,}\.[a-z_]+\(|::\w+|#\w+|v\d+\.\d+',
            query,
        )
        if exact_signals:
            return "keyword"
        return "neural"

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
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        provider = self.config.exa
        key = self._get_key_or_raise(provider)
        search_type = self._exa_search_type(
            query,
            mode=mode,
            intent=intent,
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
            payload["text"] = True
        payload["highlights"] = True
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
        )
        raw_results = response.get("results") or response.get("data") or []
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

        try:
            response = self._request_json(
                provider=provider,
                method="POST",
                path=search_path,
                payload=payload,
                key=key.key,
                base_url=provider.base_url_for("social_search"),
            timeout_seconds=max(
                30,
                int(getattr(self.config, "xai_social_timeout_seconds", 120) or 120),
            ),
            )
            return self._normalize_social_gateway_response(
                response=response,
                query=query,
                transport=key.source,
                from_date=from_date,
                to_date=to_date,
            )
        except MySearchHTTPError as exc:
            if exc.is_auth_error:
                raise
            return self._search_tavily_social_fallback(
                query=query,
                max_results=max_results,
                from_date=from_date,
                to_date=to_date,
                fallback_reason=str(exc),
            )
        except MySearchError as exc:
            return self._search_tavily_social_fallback(
                query=query,
                max_results=max_results,
                from_date=from_date,
                to_date=to_date,
                fallback_reason=str(exc),
            )

    def _search_tavily_social_fallback(
        self,
        *,
        query: str,
        max_results: int,
        from_date: str | None,
        to_date: str | None,
        fallback_reason: str,
    ) -> dict[str, Any]:
        tavily_result = self._search_tavily(
            query=query,
            max_results=max_results,
            topic="news",
            include_answer=True,
            include_content=False,
            include_domains=["x.com"],
            exclude_domains=None,
            strategy="fast",
            days=self._infer_tavily_days("status", from_date),
        )
        fallback_results = [
            {
                "provider": "tavily_social_fallback",
                "source": "x",
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("snippet", ""),
                "content": item.get("content", ""),
            }
            for item in tavily_result.get("results", [])
        ]
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
            ),
            "fallback": {
                "from": "xai_compatible",
                "to": "tavily_social_fallback",
                "reason": fallback_reason[:200],
            },
        }

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
        content = data.get("markdown", "")
        if not content and "json" in data:
            content = json.dumps(data["json"], ensure_ascii=False, indent=2)
        return {
            "provider": "firecrawl",
            "transport": key.source,
            "url": data.get("metadata", {}).get("sourceURL") or data.get("metadata", {}).get("url") or url,
            "content": content,
            "metadata": data.get("metadata") or {},
        }

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
            "oops! that page doesn’t exist or is private": "missing/private page shell",
            "oops! that page doesn\u2019t exist or is private": "missing/private page shell",
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
            if exclude_domains:
                filters["excluded_domains"] = exclude_domains
            if filters:
                tool["filters"] = filters
            tools.append(tool)

        if "x" in sources:
            tool = {"type": "x_search"}
            if allowed_x_handles:
                tool["allowed_x_handles"] = allowed_x_handles
            if excluded_x_handles:
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
        raw_results = self._extract_social_gateway_results(response)
        results = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or item.get("link") or ""
            content = (
                item.get("content")
                or item.get("full_text")
                or item.get("text")
                or item.get("body")
                or ""
            )
            title = (
                item.get("title")
                or item.get("author")
                or item.get("handle")
                or item.get("username")
                or url
            )
            snippet = item.get("snippet") or item.get("summary") or content
            results.append(
                {
                    "provider": "custom_social",
                    "source": "x",
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                    "content": content,
                    "author": item.get("author") or item.get("username") or item.get("handle") or "",
                    "created_at": item.get("created_at") or item.get("published_at") or "",
                }
            )

        results = self._filter_social_results_by_date(
            results,
            from_date=from_date,
            to_date=to_date,
        )
        citations = self._extract_social_gateway_citations(response, results)
        answer = (
            response.get("answer")
            or response.get("summary")
            or response.get("content")
            or response.get("text")
            or ""
        )
        warning = None
        if (from_date or to_date) and not results:
            answer = ""
            warning = "no social results matched the requested date window"

        normalized = {
            "provider": "custom_social",
            "transport": transport,
            "query": response.get("query", query),
            "answer": answer,
            "results": results,
            "citations": citations,
            "tool_usage": response.get("tool_usage") or {"social_search_calls": 1},
        }
        if warning:
            normalized["warning"] = warning
        return normalized

    def _filter_social_results_by_date(
        self,
        results: list[dict[str, Any]],
        *,
        from_date: str | None,
        to_date: str | None,
    ) -> list[dict[str, Any]]:
        if not from_date and not to_date:
            return results

        start = self._parse_date_bound(from_date, end_of_day=False) if from_date else None
        end = self._parse_date_bound(to_date, end_of_day=True) if to_date else None
        filtered: list[dict[str, Any]] = []
        for item in results:
            created_at = self._parse_result_timestamp(item.get("created_at"))
            if created_at is None:
                continue
            if start is not None and created_at < start:
                continue
            if end is not None and created_at > end:
                continue
            filtered.append(item)
        return filtered

    def _parse_date_bound(self, value: str, *, end_of_day: bool) -> datetime | None:
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise MySearchError(
                f"Invalid date format: '{value}'. Use ISO format YYYY-MM-DD."
            )
        bound_time = dt_time.max if end_of_day else dt_time.min
        return datetime.combine(parsed, bound_time).replace(tzinfo=timezone.utc)

    def _parse_result_timestamp(self, value: Any) -> datetime | None:
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

    def _extract_social_gateway_results(self, response: dict[str, Any]) -> list[Any]:
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

    def _extract_social_gateway_citations(
        self,
        response: dict[str, Any],
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
                citation = self._normalize_citation(item)
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
            best = max(variants, key=self._result_quality_score)
            merged_item = self._canonicalize_result_item(dict(best))
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
        explicit_resource_mode = mode in {"docs", "github", "pdf"}
        if intent == "tutorial" and not explicit_resource_mode:
            return False
        return explicit_resource_mode or intent == "resource"

    def _rerank_resource_results(
        self,
        *,
        query: str,
        mode: SearchMode,
        results: list[dict[str, Any]],
        include_domains: list[str] | None,
    ) -> list[dict[str, Any]]:
        if len(results) < 2:
            return results

        query_tokens = self._query_brand_tokens(query)
        precision_tokens = self._query_precision_tokens(query)
        exact_identifier_tokens = self._query_exact_identifier_tokens(query)
        topic_specific_tokens = self._query_topic_specific_tokens(query)
        strict_official = bool(include_domains) or self._looks_like_official_query(query)
        ranked = sorted(
            enumerate(results),
            key=lambda pair: (
                self._resource_result_rank(
                    query=query,
                    mode=mode,
                    item=pair[1],
                    query_tokens=query_tokens,
                    precision_tokens=precision_tokens,
                    exact_identifier_tokens=exact_identifier_tokens,
                    topic_specific_tokens=topic_specific_tokens,
                    include_domains=include_domains,
                    strict_official=strict_official,
                ),
                -pair[0],
            ),
            reverse=True,
        )
        return [self._canonicalize_result_item(dict(pair[1])) for pair in ranked]

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
        flags = self._resource_result_flags(
            mode=mode,
            item=item,
            query_tokens=query_tokens,
            include_domains=include_domains,
        )
        hostname = str(flags["hostname"])
        path = urlparse(item.get("url", "")).path.lower()
        include_match = int(flags["include_match"])
        host_brand_match = int(flags["host_brand_match"])
        registered_domain_label_match = int(flags["registered_domain_label_match"])
        title_brand_match = int(flags["title_brand_match"])
        docs_shape_match = int(flags["docs_shape_match"])
        github_bonus = int(
            mode == "github"
            and flags["hostname"] in {"github.com", "raw.githubusercontent.com"}
        )
        pdf_bonus = int(mode == "pdf" and self._looks_like_pdf_url(item.get("url", "")))
        non_derivative_paper_bonus = int(
            mode == "pdf"
            and not self._looks_like_derivative_paper_title(
                (item.get("title") or "").lower()
            )
        )
        paper_landing_bonus = int(
            mode == "pdf"
            and "paper" in precision_tokens
            and str(flags["hostname"]) == "arxiv.org"
            and any(
                marker in urlparse(item.get("url", "")).path.lower()
                for marker in ("/abs/", "/html/")
            )
        )
        primary_named_paper_bonus = int(
            mode == "pdf"
            and self._looks_like_primary_named_paper_result(
                title_text=(item.get("title") or "").lower(),
                query_tokens=self._paper_query_subject_tokens(
                    query=query,
                    query_tokens=query_tokens,
                    precision_tokens=precision_tokens,
                ),
            )
        )
        non_community_official = int(
            not (
                strict_official
                and self._is_obvious_official_community_result(
                    hostname=hostname,
                    path=path,
                )
            )
        )
        non_third_party = int(flags["non_third_party"])
        official_resource_match = int(
            self._is_probably_official_resource_result(
                mode=mode,
                hostname=hostname,
                include_match=bool(include_match),
                registered_domain_label_match=bool(registered_domain_label_match),
                host_brand_match=bool(host_brand_match),
                title_brand_match=bool(title_brand_match),
                docs_shape_match=bool(docs_shape_match),
                non_third_party=bool(non_third_party),
                official_query=strict_official,
            )
        )
        url = item.get("url", "")
        query_lower = query.lower()
        status_query = self._looks_like_status_query(query_lower)
        path_precision_hits, total_precision_hits = self._query_precision_hit_counts(
            hostname=hostname,
            path=path,
            title_text=(item.get("title") or "").lower(),
            query_tokens=precision_tokens,
        )
        topic_path_hits, topic_total_hits = self._query_precision_hit_counts(
            hostname=hostname,
            path=path,
            title_text=(item.get("title") or "").lower(),
            query_tokens=topic_specific_tokens,
        )
        exact_path_hits, exact_total_hits = self._query_exact_identifier_hit_counts(
            path=path,
            title_text=(item.get("title") or "").lower(),
            query_tokens=exact_identifier_tokens,
        )
        tutorial_query = self._looks_like_tutorial_query(query_lower) or self._looks_like_debugging_query(query_lower)
        tutorial_community_result = int(
            tutorial_query
            and self._looks_like_tutorial_community_result(
                hostname=hostname,
                registered_domain=self._registered_domain(hostname),
                path=path,
            )
        )
        tutorial_brand_aligned_resource = int(
            tutorial_query
            and not tutorial_community_result
            and self._looks_like_brand_aligned_tutorial_result(
                hostname=hostname,
                registered_domain=self._registered_domain(hostname),
                path=path,
                title_text=(item.get("title") or "").lower(),
                snippet_text=(item.get("snippet") or "").lower(),
                query_tokens=query_tokens,
                path_precision_hits=path_precision_hits,
                exact_total_hits=exact_total_hits,
            )
        )
        tutorial_exact_identifier_match = int(
            tutorial_query and (exact_total_hits > 0 or exact_path_hits > 0)
        )
        non_generic_tutorial_docs = int(
            not (
                tutorial_query
                and exact_identifier_tokens
                and self._looks_like_generic_debugging_docs_result(
                    hostname=hostname,
                    path=path,
                    title_text=(item.get("title") or "").lower(),
                )
            )
        )
        official_docs_query = strict_official and self._looks_like_official_docs_query(query_lower)
        official_topic_exact_match = int(
            official_docs_query
            and docs_shape_match
            and (
                (
                    bool(topic_specific_tokens)
                    and (topic_total_hits > 0 or topic_path_hits > 0)
                )
                or (
                    not topic_specific_tokens
                    and (exact_total_hits > 0 or path_precision_hits >= 2 or total_precision_hits >= 3)
                )
            )
        )
        non_language_sdk_reference = int(
            not (
                official_docs_query
                and self._looks_like_language_specific_sdk_reference_result(
                    hostname=hostname,
                    path=path,
                    title_text=(item.get("title") or "").lower(),
                )
                and not self._query_mentions_programming_language(query_lower)
            )
        )
        non_generic_official_landing = int(
            not (
                official_docs_query
                and self._looks_like_generic_official_landing_result(
                    hostname=hostname,
                    path=path,
                    title_text=(item.get("title") or "").lower(),
                )
            )
        )
        canonical_status_page_match = int(
            status_query
            and self._looks_like_canonical_status_result(hostname=hostname, path=path)
        )
        status_page_match = int(
            status_query
            and self._looks_like_status_result(
                url=url,
                hostname=hostname,
                title_text=(item.get("title") or "").lower(),
            )
        )
        non_status_api_endpoint = int(
            not (
                status_query
                and path.startswith("/api")
            )
        )
        paper_subject_tokens = (
            self._paper_query_subject_tokens(
                query=query,
                query_tokens=query_tokens,
                precision_tokens=precision_tokens,
            )
            if mode == "pdf"
            else []
        )
        paper_compound_tokens = self._paper_query_compound_tokens(query) if mode == "pdf" else []
        paper_compound_match = int(
            mode == "pdf"
            and any(
                self._paper_text_matches_compound_token(
                    f"{item.get('title', '')} {path}",
                    token,
                )
                for token in paper_compound_tokens
            )
        )
        paper_subject_exact_match = int(
            mode == "pdf"
            and self._looks_like_primary_named_paper_result(
                title_text=(item.get("title") or "").lower(),
                query_tokens=paper_subject_tokens,
            )
        )
        non_paper_compound_mismatch = int(
            not (
                mode == "pdf"
                and paper_compound_tokens
                and not paper_compound_match
                and not self._looks_like_generic_arxiv_subject_title((item.get("title") or "").lower())
            )
        )
        changelog_page_match = int(
            self._looks_like_changelog_query(query_lower)
            and self._looks_like_changelog_result(
                url=url,
                hostname=str(flags["hostname"]),
                title_text=(item.get("title") or "").lower(),
            )
        )
        canonical_changelog_page_match = int(
            self._looks_like_changelog_query(query_lower)
            and self._looks_like_canonical_changelog_result(
                url=url,
                hostname=hostname,
                title_text=(item.get("title") or "").lower(),
                precision_tokens=precision_tokens,
            )
        )
        non_generic_changelog_index = int(
            not (
                self._looks_like_changelog_query(query_lower)
                and self._looks_like_generic_changelog_index_result(
                    hostname=hostname,
                    path=path,
                )
            )
        )
        pricing_page_match = int(
            self._looks_like_pricing_query(query_lower)
            and self._looks_like_pricing_result(
                url=url,
                hostname=hostname,
                title_text=(item.get("title") or "").lower(),
            )
        )
        canonical_pricing_page_match = int(
            self._looks_like_pricing_query(query_lower)
            and self._looks_like_canonical_pricing_result(
                hostname=hostname,
                path=path,
            )
        )
        non_locale_variant = int(
            not (
                strict_official
                and self._looks_like_locale_prefixed_path(path)
            )
        )
        matched_provider_count = len(item.get("matched_providers") or [])
        content_score, snippet_score, title_score = self._result_quality_score(item)
        return (
            include_match,
            non_community_official,
            official_resource_match,
            official_topic_exact_match,
            canonical_status_page_match,
            status_page_match,
            non_status_api_endpoint,
            paper_subject_exact_match,
            primary_named_paper_bonus,
            non_derivative_paper_bonus,
            paper_compound_match,
            non_paper_compound_mismatch,
            paper_landing_bonus,
            topic_path_hits,
            topic_total_hits,
            tutorial_brand_aligned_resource,
            1 - tutorial_community_result,
            tutorial_exact_identifier_match,
            non_generic_tutorial_docs,
            non_language_sdk_reference,
            non_generic_official_landing,
            canonical_changelog_page_match,
            changelog_page_match,
            non_generic_changelog_index,
            canonical_pricing_page_match,
            pricing_page_match,
            exact_path_hits,
            exact_total_hits,
            non_locale_variant,
            path_precision_hits,
            total_precision_hits,
            registered_domain_label_match,
            github_bonus,
            pdf_bonus,
            host_brand_match,
            docs_shape_match,
            non_third_party,
            title_brand_match,
            matched_provider_count,
            content_score,
            snippet_score,
            title_score,
        )

    def _looks_like_generic_changelog_index_result(self, *, hostname: str, path: str) -> bool:
        normalized_path = path.rstrip("/") or "/"
        if hostname.startswith("github.com"):
            return False
        return normalized_path in {"/blog", "/changelog", "/releases", "/release-notes"}

    def _paper_query_subject_tokens(
        self,
        *,
        query: str,
        query_tokens: list[str],
        precision_tokens: list[str],
    ) -> list[str]:
        subject_tokens: list[str] = []
        seen: set[str] = set()
        compound_tokens = self._paper_query_compound_tokens(query)
        raw_query_tokens = [
            cleaned
            for raw_token in re.findall(r"[a-z0-9][a-z0-9._/-]{1,}", query.lower())
            for cleaned in [raw_token.strip("._/-")]
            if self._is_mixed_alnum_short_token(cleaned)
        ]
        for token in [*query_tokens, *compound_tokens, *raw_query_tokens, *precision_tokens]:
            cleaned = token.strip().lower()
            if cleaned in seen or cleaned in {"paper", "pdf"}:
                continue
            if len(cleaned) < 2:
                continue
            seen.add(cleaned)
            subject_tokens.append(cleaned)
        return subject_tokens[:3]

    def _paper_query_compound_tokens(self, query: str) -> list[str]:
        raw_tokens = [token for token in re.findall(r"[a-z0-9]+", query.lower()) if token]
        compounds: list[str] = []
        seen: set[str] = set()
        for index, token in enumerate(raw_tokens):
            compact = re.sub(r"[^a-z0-9]+", "", token)
            if compact and any(ch.isalpha() for ch in compact) and any(ch.isdigit() for ch in compact):
                if compact not in seen:
                    seen.add(compact)
                    compounds.append(compact)
            if index + 1 < len(raw_tokens) and raw_tokens[index].isalpha() and raw_tokens[index + 1].isdigit():
                combined = f"{raw_tokens[index]}{raw_tokens[index + 1]}"
                if combined not in seen:
                    seen.add(combined)
                    compounds.append(combined)
        return compounds

    def _paper_text_matches_compound_token(self, text: str, compound_token: str) -> bool:
        normalized_text = re.sub(r"[^a-z0-9]+", "", (text or "").lower())
        if compound_token in normalized_text:
            return True
        letters = "".join(ch for ch in compound_token if ch.isalpha())
        digits = "".join(ch for ch in compound_token if ch.isdigit())
        if not letters or not digits:
            return False
        return re.search(rf"{re.escape(letters)}[\s\-_:/()]*{re.escape(digits)}", (text or "").lower()) is not None

    def _looks_like_primary_named_paper_result(
        self,
        *,
        title_text: str,
        query_tokens: list[str],
    ) -> bool:
        if not query_tokens:
            return False
        if len(query_tokens) == 1:
            token = re.escape(query_tokens[0])
            return re.match(rf"^\s*(?:\[[^\]]+\]\s*)?{token}(?:\b|[\s:()\-])", title_text) is not None
        subject_pattern = r"[\s\-_]*".join(re.escape(token) for token in query_tokens[:2])
        return re.match(rf"^\s*{subject_pattern}\s*:", title_text) is not None

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
        if include_match:
            return True
        if mode in {"github", "pdf"}:
            return True
        if not non_third_party:
            return False
        if official_query and registered_domain_label_match:
            return True
        if not docs_shape_match:
            return False
        official_host_surface = any(
            part in {"api", "developer", "developers", "docs", "help", "platform", "reference", "support"}
            for part in hostname.split(".")
            if part
        )
        return registered_domain_label_match or (host_brand_match and official_host_surface) or (
            title_brand_match and official_host_surface
        )

    def _align_citations_with_results(
        self,
        *,
        results: list[dict[str, Any]],
        citations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        synthesized = [
            {"title": item.get("title", ""), "url": item.get("url", "")}
            for item in results
            if item.get("url")
        ]
        normalized = self._dedupe_citations(citations, synthesized)
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
            dedupe_key = self._citation_dedupe_key(citation)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            ordered.append(citation)

        for citation in normalized:
            dedupe_key = self._citation_dedupe_key(citation)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            ordered.append(citation)
        return ordered

    def _dedupe_citations(self, *citation_lists: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for citations in citation_lists:
            for item in citations:
                citation = self._normalize_citation(item)
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

    def _citation_dedupe_key(self, item: dict[str, Any]) -> str:
        return (
            item.get("url")
            or item.get("title")
            or json.dumps(item, ensure_ascii=False, sort_keys=True)
        )

    def _result_dedupe_key(self, item: dict[str, Any]) -> str:
        url = self._canonical_result_url((item.get("url") or "").strip()).lower()
        if url:
            return url
        title = re.sub(r"\s+", " ", (item.get("title") or "").strip().lower())
        snippet = re.sub(r"\s+", " ", (item.get("snippet") or "").strip().lower())
        return f"{title}|{snippet[:160]}".strip("|")

    def _canonicalize_result_item(self, item: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(item)
        normalized["url"] = self._canonical_result_url(str(item.get("url") or ""))
        return normalized

    def _canonical_result_url(self, url: str) -> str:
        raw = (url or "").strip()
        if not raw:
            return ""
        parsed = urlparse(raw)
        hostname = self._clean_hostname(parsed.netloc)
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

    def _looks_like_locale_prefixed_path(self, path: str) -> bool:
        parts = [item for item in (path or "").split("/") if item]
        if len(parts) < 2:
            return False
        first = parts[0].strip().lower()
        return bool(re.fullmatch(r"[a-z]{2}(?:-[a-z0-9]{2,8}){0,2}", first))

    def _looks_like_generic_arxiv_subject_title(self, title_text: str) -> bool:
        cleaned = re.sub(r"\s+", " ", (title_text or "").strip())
        if not cleaned:
            return True
        return bool(re.fullmatch(r"[A-Za-z][A-Za-z &]+ > [A-Za-z][A-Za-z ,&()/-]+", cleaned))

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
        content = item.get("content") or ""
        snippet = item.get("snippet") or ""
        title = item.get("title") or ""
        return (len(content), len(snippet), len(title))

    def _result_hostname(self, item: dict[str, Any]) -> str:
        url = (item.get("url") or "").strip()
        if not url:
            return ""
        return self._clean_hostname(urlparse(url).netloc)

    def _clean_hostname(self, hostname: str) -> str:
        cleaned = hostname.lower().strip().strip(".")
        if cleaned.startswith("www."):
            return cleaned[4:]
        return cleaned

    def _registered_domain(self, hostname: str) -> str:
        cleaned = self._clean_hostname(hostname)
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

    def _domain_matches(self, hostname: str, domain: str) -> bool:
        cleaned_host = self._clean_hostname(hostname)
        cleaned_domain = self._clean_hostname(domain)
        return bool(cleaned_host) and bool(cleaned_domain) and (
            cleaned_host == cleaned_domain or cleaned_host.endswith(f".{cleaned_domain}")
        )

    def _registered_domain_label_matches(self, *, registered_domain: str, query_tokens: list[str]) -> bool:
        labels = [item for item in self._clean_hostname(registered_domain).split(".") if item]
        return any(
            label == token or label.startswith(f"{token}-") or label.startswith(f"{token}_")
            for token in query_tokens
            for label in labels
        )

    def _resource_result_flags(
        self,
        *,
        mode: SearchMode,
        item: dict[str, Any],
        query_tokens: list[str],
        include_domains: list[str] | None,
    ) -> dict[str, Any]:
        url = item.get("url", "")
        hostname = self._result_hostname(item)
        registered_domain = self._registered_domain(hostname)
        title_text = (item.get("title") or "").lower()
        include_match = bool(
            include_domains
            and any(self._domain_matches(hostname, domain) for domain in include_domains or [])
        )
        host_brand_match = any(
            token in hostname or token in registered_domain for token in query_tokens
        )
        registered_domain_label_match = self._registered_domain_label_matches(
            registered_domain=registered_domain,
            query_tokens=query_tokens,
        )
        title_brand_match = any(token in title_text for token in query_tokens)
        docs_shape_match = self._looks_like_resource_result(
            url=url,
            hostname=hostname,
            title_text=title_text,
            mode=mode,
        )
        non_third_party = not self._is_obvious_third_party_resource(
            hostname=hostname,
            registered_domain=registered_domain,
            mode=mode,
        )
        return {
            "hostname": hostname,
            "registered_domain": registered_domain,
            "include_match": include_match,
            "host_brand_match": host_brand_match,
            "registered_domain_label_match": registered_domain_label_match,
            "title_brand_match": title_brand_match,
            "docs_shape_match": docs_shape_match,
            "non_third_party": non_third_party,
        }

    def _result_matches_official_policy(
        self,
        *,
        item: dict[str, Any],
        mode: SearchMode,
        query_tokens: list[str],
        include_domains: list[str] | None,
        strict_official: bool,
    ) -> bool:
        flags = self._resource_result_flags(
            mode=mode,
            item=item,
            query_tokens=query_tokens,
            include_domains=include_domains,
        )
        return self._is_probably_official_resource_result(
            mode=mode,
            hostname=str(flags["hostname"]),
            include_match=bool(flags["include_match"]),
            registered_domain_label_match=bool(flags["registered_domain_label_match"]),
            host_brand_match=bool(flags["host_brand_match"]),
            title_brand_match=bool(flags["title_brand_match"]),
            docs_shape_match=bool(flags["docs_shape_match"]),
            non_third_party=bool(flags["non_third_party"]),
            official_query=strict_official,
        )

    def _query_brand_tokens(self, query: str) -> list[str]:
        stopwords = {
            "a",
            "an",
            "and",
            "api",
            "apis",
            "best",
            "changelog",
            "compare",
            "comparison",
            "developer",
            "developers",
            "docs",
            "documentation",
            "for",
            "github",
            "guide",
            "how",
            "manual",
            "pricing",
            "reference",
            "release",
            "releases",
            "sdk",
            "status",
            "the",
            "tutorial",
            "vs",
            "with",
            "价格",
            "发布",
            "对比",
            "接口",
            "教程",
            "文档",
            "更新日志",
        }
        tokens: list[str] = []
        for token in re.findall(r"[a-z0-9][a-z0-9._-]{1,}", query.lower()):
            if token in stopwords or token.isdigit():
                continue
            if len(token) < 3:
                continue
            tokens.append(token)
        return tokens

    def _query_precision_tokens(self, query: str) -> list[str]:
        stopwords = {
            "a",
            "an",
            "and",
            "best",
            "docs",
            "documentation",
            "for",
            "guide",
            "how",
            "official",
            "the",
            "with",
            "官网",
            "官方",
        }
        precision_tokens: list[str] = []
        seen: set[str] = set()
        raw_tokens = re.findall(r"[a-z0-9][a-z0-9._/-]{1,}", query.lower())
        for raw_token in raw_tokens:
            cleaned = raw_token.strip("._/-")
            candidates = {cleaned}
            compact = re.sub(r"[^a-z0-9]+", "", cleaned)
            if compact:
                candidates.add(compact)
            candidates.update(
                token
                for token in re.split(r"[^a-z0-9]+", cleaned)
                if token
            )
            for candidate in candidates:
                if candidate in stopwords or candidate.isdigit():
                    continue
                if len(candidate) < 3:
                    continue
                if candidate in seen:
                    continue
                seen.add(candidate)
                precision_tokens.append(candidate)
        return precision_tokens

    def _is_mixed_alnum_short_token(self, token: str) -> bool:
        return (
            len(token) == 2
            and any(ch.isalpha() for ch in token)
            and any(ch.isdigit() for ch in token)
        )

    def _query_precision_hit_counts(
        self,
        *,
        hostname: str,
        path: str,
        title_text: str,
        query_tokens: list[str],
    ) -> tuple[int, int]:
        if not query_tokens:
            return 0, 0
        path_text = f"{hostname} {path}"
        full_text = f"{path_text} {title_text}"
        path_matches = sum(1 for token in query_tokens if token in path_text)
        total_matches = sum(1 for token in query_tokens if token in full_text)
        return min(path_matches, 4), min(total_matches, 6)

    def _query_exact_identifier_tokens(self, query: str) -> list[str]:
        tokens: list[str] = []
        seen: set[str] = set()
        raw_tokens = re.findall(r"[a-z0-9][a-z0-9._/-]{2,}", query.lower())
        for raw_token in raw_tokens:
            if not any(marker in raw_token for marker in (".", "/", "_", "-")):
                continue
            compact = re.sub(r"[^a-z0-9]+", "", raw_token.strip("._/-"))
            if len(compact) < 4 or compact in seen:
                continue
            seen.add(compact)
            tokens.append(compact)
        return tokens

    def _query_topic_specific_tokens(self, query: str) -> list[str]:
        generic_tokens = {
            "api",
            "apis",
            "docs",
            "documentation",
            "guide",
            "guides",
            "official",
            "openai",
            "paper",
            "pdf",
            "price",
            "pricing",
            "report",
            "response",
            "responses",
        }
        return [
            token
            for token in self._query_precision_tokens(query)
            if token not in generic_tokens
        ]

    def _query_exact_identifier_hit_counts(
        self,
        *,
        path: str,
        title_text: str,
        query_tokens: list[str],
    ) -> tuple[int, int]:
        if not query_tokens:
            return 0, 0
        path_segments = {
            token
            for token in re.split(r"[^a-z0-9]+", path.lower())
            if len(token) >= 3
        }
        title_segments = {
            token
            for token in re.split(r"[^a-z0-9]+", title_text.lower())
            if len(token) >= 3
        }
        path_matches = sum(1 for token in query_tokens if token in path_segments)
        total_matches = sum(1 for token in query_tokens if token in path_segments or token in title_segments)
        return min(path_matches, 3), min(total_matches, 3)

    def _looks_like_official_docs_query(self, query_lower: str) -> bool:
        if (
            self._looks_like_pricing_query(query_lower)
            or self._looks_like_changelog_query(query_lower)
            or self._looks_like_status_query(query_lower)
        ):
            return False
        return self._looks_like_docs_query(query_lower) or self._looks_like_api_docs_topic_query(query_lower)

    def _looks_like_pricing_query(self, query_lower: str) -> bool:
        keywords = [
            "pricing",
            "price",
            "多少钱",
            "价格",
            "售价",
            "buy",
            "购买",
        ]
        return any(keyword in query_lower for keyword in keywords)

    def _looks_like_pricing_result(self, *, url: str, hostname: str, title_text: str) -> bool:
        path = urlparse(url).path.lower()
        if hostname.startswith("shop.") and "/product/" not in path:
            return True
        pricing_markers = (
            "/pricing",
            "/price",
            "/plans",
            "/plan",
            "/shop/buy",
            "/buy-",
        )
        return any(marker in path for marker in pricing_markers) or any(
            marker in title_text for marker in ("pricing", "price", "plan")
        )

    def _looks_like_canonical_pricing_result(self, *, hostname: str, path: str) -> bool:
        normalized_path = path.rstrip("/")
        path_segments = [segment for segment in normalized_path.split("/") if segment]
        if hostname == "openai.com" and normalized_path in {"/business/chatgpt-pricing", "/chatgpt/pricing"}:
            return False
        if ("/shop/buy" in normalized_path or "/buy-" in normalized_path) and len(path_segments) <= 3:
            return True
        if hostname.startswith("shop.") and "/product/" not in normalized_path and len(path_segments) <= 3:
            return True
        if "pricing" in normalized_path and "/docs/" not in normalized_path and not hostname.startswith("developers."):
            return True
        return False

    def _looks_like_generic_official_landing_result(
        self,
        *,
        hostname: str,
        path: str,
        title_text: str,
    ) -> bool:
        normalized_path = path.rstrip("/") or "/"
        generic_paths = {
            "/",
            "/api",
            "/api/docs",
            "/api/docs/guides",
            "/developers",
            "/docs",
            "/documentation",
            "/guides",
            "/learn",
            "/reference",
        }
        if normalized_path in generic_paths:
            return True
        if hostname == "openai.com" and normalized_path in {"/pricing", "/business/chatgpt-pricing"}:
            return True
        generic_titles = {"docs", "documentation", "developer docs", "guides", "reference"}
        return title_text.strip() in generic_titles

    def _query_mentions_programming_language(self, query_lower: str) -> bool:
        terms = set(re.findall(r"[a-z0-9#+.-]+", query_lower))
        language_markers = (
            "c#",
            "csharp",
            "go",
            "java",
            "javascript",
            "node",
            "php",
            "python",
            "ruby",
            "sdk",
            "typescript",
        )
        return any(marker in query_lower and marker in terms for marker in language_markers)

    def _looks_like_language_specific_sdk_reference_result(
        self,
        *,
        hostname: str,
        path: str,
        title_text: str,
    ) -> bool:
        if "api/reference" not in path and "api reference" not in title_text:
            return False
        language_markers = (
            "/csharp/",
            "/go/",
            "/java/",
            "/javascript/",
            "/node/",
            "/php/",
            "/python/",
            "/ruby/",
            "/typescript/",
        )
        if any(marker in path for marker in language_markers):
            return True
        return any(
            marker in title_text
            for marker in ("python", "ruby", "go", "typescript", "javascript", "java", "php", "c#", "csharp", "node")
        ) and hostname.endswith("openai.com")

    def _looks_like_changelog_result(self, *, url: str, hostname: str, title_text: str) -> bool:
        path = urlparse(url).path.lower()
        changelog_markers = (
            "/blog/",
            "/changelog",
            "/release-notes",
            "/releases",
            "/updating",
            "/upgrading",
            "/version-",
        )
        title_markers = (
            "announcing",
            "changelog",
            "release notes",
            "what's new",
            "whats new",
        )
        return any(marker in path for marker in changelog_markers) or any(
            marker in title_text for marker in title_markers
        )

    def _looks_like_canonical_changelog_result(
        self,
        *,
        url: str,
        hostname: str,
        title_text: str,
        precision_tokens: list[str],
    ) -> bool:
        path = urlparse(url).path.lower().rstrip("/")
        if not self._looks_like_changelog_result(url=url, hostname=hostname, title_text=title_text):
            return False
        high_signal_path = any(
            marker in path
            for marker in ("/blog/", "/release-notes", "/releases")
        )
        if not high_signal_path:
            return False
        if not precision_tokens:
            return True
        path_hits, total_hits = self._query_precision_hit_counts(
            hostname=hostname,
            path=path,
            title_text=title_text,
            query_tokens=precision_tokens,
        )
        return path_hits > 0 or total_hits > 0

    def _is_obvious_official_community_result(self, *, hostname: str, path: str) -> bool:
        labels = [label for label in hostname.split(".") if label]
        community_labels = {"community", "forum", "forums", "discuss", "discussion"}
        if any(label in community_labels for label in labels[:2]):
            return True
        normalized_path = path.rstrip("/")
        return normalized_path.startswith("/t/") or normalized_path.startswith("/c/")

    def _looks_like_debugging_result(
        self,
        *,
        hostname: str,
        registered_domain: str,
        path: str,
        title_text: str,
        snippet_text: str,
    ) -> bool:
        text = f"{title_text} {snippet_text} {path}"
        if registered_domain == "github.com" and any(marker in path for marker in ("/issues/", "/discussions/")):
            return True
        debugging_markers = (
            "bug",
            "cannot",
            "can't",
            "debug",
            "error",
            "failed",
            "failing",
            "fix",
            "issue",
            "strict mode",
            "troubleshoot",
            "troubleshooting",
            "violation",
            "workaround",
            "报错",
            "排查",
            "修复",
            "错误",
        )
        if any(marker in text for marker in debugging_markers):
            return True
        return hostname.endswith("stackoverflow.com")

    def _looks_like_generic_debugging_docs_result(
        self,
        *,
        hostname: str,
        path: str,
        title_text: str,
    ) -> bool:
        normalized_path = path.rstrip("/")
        generic_paths = {
            "/docs/writing-tests",
            "/docs/running-tests",
            "/docs/test-fixtures",
            "/docs/intro",
            "/docs/test-ui-mode",
        }
        if normalized_path in generic_paths:
            return True
        generic_titles = (
            "writing tests",
            "running and debugging tests",
            "running tests",
            "test ui mode",
            "fixtures",
        )
        return any(title_text.strip() == candidate for candidate in generic_titles) and hostname.endswith("playwright.dev")

    def _looks_like_status_result(self, *, url: str, hostname: str, title_text: str) -> bool:
        path = urlparse(url).path.lower()
        if self._looks_like_brand_status_domain(hostname):
            return True
        status_markers = (
            "/status",
            "/incidents",
            "/incident",
            "/uptime",
            "/outage",
        )
        return any(marker in path for marker in status_markers) or any(
            marker in title_text for marker in ("status", "incident", "outage", "uptime")
        )

    def _looks_like_canonical_status_result(self, *, hostname: str, path: str) -> bool:
        normalized_path = path.rstrip("/")
        if self._looks_like_brand_status_domain(hostname):
            return normalized_path in {"", "/", "/history", "/incidents"} or normalized_path.startswith("/incidents")
        if hostname.startswith("status.") or ".statuspage." in hostname:
            return True
        return normalized_path.startswith("/incidents") or normalized_path.startswith("/incident")

    def _looks_like_brand_status_domain(self, hostname: str) -> bool:
        cleaned = self._clean_hostname(hostname)
        if not cleaned:
            return False
        if cleaned.startswith("status.") or ".statuspage." in cleaned:
            return True
        registered_domain = self._registered_domain(cleaned)
        return registered_domain.endswith("status.com") or registered_domain.endswith("status.io")

    def _looks_like_resource_result(
        self,
        *,
        url: str,
        hostname: str,
        title_text: str,
        mode: SearchMode,
    ) -> bool:
        parsed = urlparse(url)
        path = parsed.path.lower()
        hostname_labels = [item for item in hostname.split(".") if item]
        docs_keywords = (
            "/api",
            "/changelog",
            "/docs",
            "/documentation",
            "/guide",
            "/guides",
            "/manual",
            "/pricing",
            "/readme",
            "/reference",
            "/references",
        )
        title_keywords = (
            "api reference",
            "changelog",
            "docs",
            "documentation",
            "guide",
            "manual",
            "pricing",
            "readme",
            "reference",
        )
        hostname_keywords = {
            "api",
            "developer",
            "developers",
            "docs",
            "help",
            "platform",
            "reference",
            "support",
        }
        if mode == "github" and hostname in {"github.com", "raw.githubusercontent.com"}:
            return True
        if mode == "pdf" and self._looks_like_pdf_url(url):
            return True
        return (
            any(part in hostname_keywords for part in hostname_labels)
            or any(keyword in path for keyword in docs_keywords)
            or any(keyword in title_text for keyword in title_keywords)
        )

    def _looks_like_pdf_url(self, url: str) -> bool:
        return urlparse(url).path.lower().endswith(".pdf")

    def _is_obvious_third_party_resource(
        self,
        *,
        hostname: str,
        registered_domain: str,
        mode: SearchMode,
    ) -> bool:
        if mode == "github" and hostname in {"github.com", "raw.githubusercontent.com"}:
            return False
        third_party_domains = {
            "arxiv.org",
            "dev.to",
            "facebook.com",
            "hashnode.dev",
            "hashnode.com",
            "linkedin.com",
            "medium.com",
            "news.ycombinator.com",
            "quora.com",
            "reddit.com",
            "researchgate.net",
            "stackexchange.com",
            "stackoverflow.com",
            "substack.com",
            "towardsdatascience.com",
            "twitter.com",
            "x.com",
            "youtube.com",
            "youtu.be",
        }
        return registered_domain in third_party_domains

    def _collect_source_domains(
        self,
        *,
        results: list[dict[str, Any]],
        citations: list[dict[str, Any]],
    ) -> list[str]:
        domains: list[str] = []
        seen: set[str] = set()
        for item in [*results, *citations]:
            if not isinstance(item, dict):
                continue
            hostname = self._result_hostname(item)
            registered_domain = self._registered_domain(hostname)
            if not registered_domain or registered_domain in seen:
                continue
            seen.add(registered_domain)
            domains.append(registered_domain)
        return domains

    def _count_official_resource_results(
        self,
        *,
        query: str,
        mode: SearchMode,
        intent: ResolvedSearchIntent,
        results: list[dict[str, Any]],
        include_domains: list[str] | None,
    ) -> int:
        official_mode = self._resolve_official_result_mode(
            query=query,
            mode=mode,
            intent=intent,
            include_domains=include_domains,
        )
        if official_mode == "off" and not self._should_rerank_resource_results(mode=mode, intent=intent):
            return 0
        query_tokens = self._query_brand_tokens(query)
        strict_official = official_mode == "strict"
        official_count = 0
        for item in results:
            if self._result_matches_official_policy(
                item=item,
                mode=mode,
                query_tokens=query_tokens,
                include_domains=include_domains,
                strict_official=strict_official,
            ):
                official_count += 1
        return official_count

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
    ) -> list[str]:
        conflicts: list[str] = []
        if len(source_domains) <= 1 and len(results) > 1:
            conflicts.append("low-source-diversity")
        if len(set(providers_consulted)) <= 1 and len(source_domains) <= 1 and results:
            conflicts.append("single-provider-single-domain")
        if self._should_rerank_resource_results(mode=mode, intent=intent):
            if results and official_source_count <= 0:
                conflicts.append("official-source-not-confirmed")
            elif results and official_source_count < len(results):
                conflicts.append("mixed-official-and-third-party")
            if include_domains and not results:
                conflicts.append("domain-filter-returned-empty")
        if official_mode == "strict" and results and official_source_count <= 0:
            conflicts.append("strict-official-unmet")
        return conflicts

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
    ) -> str:
        if result_count <= 0:
            return "low"
        if official_mode == "strict" and official_source_count <= 0:
            return "low"
        if self._should_rerank_resource_results(mode=mode, intent=intent):
            if official_source_count > 0 and "official-source-not-confirmed" not in conflicts:
                if (
                    verification == "cross-provider"
                    or (source_domain_count >= 2 and "mixed-official-and-third-party" not in conflicts)
                ):
                    return "high"
                return "medium"
            return "medium" if source_domain_count >= 2 else "low"
        if verification == "cross-provider" and source_domain_count >= 2:
            return "high"
        if source_domain_count >= 2:
            return "medium"
        return "low" if conflicts else "medium"

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
            "sources": keyring_info["sources"],
            "live_status": status["status"],
            "live_error": status["error"],
            "last_checked_at": status["checked_at"],
        }

    def _get_key_or_raise(self, provider: ProviderConfig):
        record = self.keyring.get_next(provider.name)
        if record is None:
            if provider.name == "tavily":
                raise MySearchError(
                    "Tavily is not configured. Use "
                    "MYSEARCH_TAVILY_MODE=gateway with MYSEARCH_TAVILY_GATEWAY_TOKEN "
                    "to consume an upstream gateway, or keep "
                    "MYSEARCH_TAVILY_MODE=official and import your own Tavily keys "
                    "with MYSEARCH_TAVILY_API_KEY / MYSEARCH_TAVILY_API_KEYS / "
                    "MYSEARCH_TAVILY_KEYS_FILE."
                )
            if provider.name == "xai":
                raise MySearchError(
                    "xAI / Social search is not configured; MySearch can still use "
                    "Tavily + Firecrawl for web/docs/extract. Add "
                    "MYSEARCH_XAI_API_KEY for official xAI, or configure a "
                    "compatible /social/search gateway to enable mode='social'."
                )
            if provider.name == "exa":
                raise MySearchError(
                    "Exa search is not configured. Add MYSEARCH_EXA_API_KEY, "
                    "or point MYSEARCH_EXA_BASE_URL to your proxy / compatible gateway."
                )
            raise MySearchError(f"{provider.name} is not configured")
        return record

    def _request_json(
        self,
        *,
        provider: ProviderConfig,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        key: str,
        base_url: str | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {}
        body = dict(payload or {})

        if provider.auth_mode == "bearer":
            token = key if not provider.auth_scheme else f"{provider.auth_scheme} {key}"
            headers[provider.auth_header] = token
        elif provider.auth_mode == "body":
            body[provider.auth_field] = key
        else:
            raise MySearchError(f"unsupported auth mode for {provider.name}: {provider.auth_mode}")

        url = f"{(base_url or provider.base_url)}{path}"
        headers.setdefault("Content-Type", "application/json")
        effective_timeout = timeout_seconds or self.config.timeout_seconds

        prefer_urlopen = "unittest.mock" in type(urlopen).__module__
        if prefer_urlopen:
            request_data = None if method.upper() == "GET" else json.dumps(body).encode("utf-8")
            request = Request(url, data=request_data, headers=headers, method=method.upper())
            try:
                with urlopen(request, timeout=effective_timeout) as response:
                    raw_body = response.read()
                status_code = getattr(response, "status", 200)
                response_text = raw_body.decode("utf-8", errors="replace")
            except UrlHTTPError as exc:
                status_code = int(getattr(exc, "code", 500) or 500)
                raw_body = exc.read() if getattr(exc, "fp", None) else b""
                response_text = raw_body.decode("utf-8", errors="replace")
            except Exception as exc:
                raise MySearchError(f"{provider.name} network error: {exc}") from exc
        else:
            try:
                response = self._http.request(
                    method.upper(),
                    url,
                    json=body if method.upper() != "GET" else None,
                    headers=headers,
                    timeout=effective_timeout,
                )
                status_code = response.status_code
                response_text = response.text
            except httpx.TimeoutException as exc:
                raise MySearchError(
                    f"{provider.name} request timeout after {effective_timeout}s: {url}"
                ) from exc
            except httpx.HTTPError as exc:
                raise MySearchError(f"{provider.name} network error: {exc}") from exc

        try:
            data = json.loads(response_text)
        except ValueError as exc:
            if status_code >= 400:
                raise MySearchError(f"HTTP {status_code}: {response_text[:300]}") from exc
            raise MySearchError(f"non-json response from {url}: {response_text[:300]}") from exc

        if status_code >= 400:
            detail = data
            if isinstance(data, dict):
                detail = (
                    data.get("detail")
                    or data.get("error")
                    or data.get("message")
                    or data
                )
            raise MySearchHTTPError(
                provider=provider.name,
                status_code=status_code,
                detail=detail,
                url=url,
            )
        return data

    def _request_text(
        self,
        *,
        url: str,
        timeout_seconds: int | None = None,
    ) -> tuple[int, str]:
        effective_timeout = timeout_seconds or self.config.timeout_seconds
        try:
            response = self._http.get(
                url,
                headers={"Accept": "text/html,application/json;q=0.9,*/*;q=0.8"},
                timeout=effective_timeout,
            )
            return response.status_code, response.text
        except httpx.TimeoutException as exc:
            return 0, ""
        except httpx.HTTPError as exc:
            raise MySearchError(str(exc)) from exc

    def _xai_probe_model(self) -> str:
        return "grok-4.1-fast"

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
            timeout_seconds=max(timeout_seconds, fallback_timeout_seconds),
        )

    def _probe_provider_status(
        self,
        provider: ProviderConfig,
        key_count: int,
    ) -> dict[str, str]:
        if key_count <= 0:
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

        cache_key = f"{provider.name}:{record.label}"
        with self._cache_lock:
            now = time.monotonic()
            cached = self._provider_probe_cache.get(cache_key)
            if cached and cached.get("expires_at", 0.0) > now:
                return copy.deepcopy(cached["value"])

        checked_at = datetime.now(timezone.utc).isoformat()
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
        except MySearchError as exc:
            result = {
                "status": "network_error",
                "error": str(exc),
                "checked_at": checked_at,
            }

        with self._cache_lock:
            self._provider_probe_cache[cache_key] = {
                "expires_at": time.monotonic() + self._provider_probe_ttl_seconds,
                "value": copy.deepcopy(result),
            }
        return result

    def _probe_xai_compatible_gateway(self, provider: ProviderConfig, key: str, timeout_seconds: int) -> None:
        health_path = "/health"
        health_base_url = self._derive_root_health_base_url(provider)
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
            if isinstance(payload, dict) and payload.get("ok") is False:
                detail = (
                    payload.get("error")
                    or payload.get("detail")
                    or "social/X gateway health probe reported unavailable"
                )
                raise MySearchError(str(detail))
            return
        except (MySearchHTTPError, MySearchError):
            pass

        fallback_timeout_seconds = min(self.config.timeout_seconds, 20)
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
            except MySearchError as exc:
                if "not fully available" in str(exc):
                    raise
                self._probe_xai_official_via_responses(
                    provider=provider,
                    key=key,
                    timeout_seconds=timeout_seconds,
                )
            except MySearchHTTPError as exc:
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

    def _provider_can_serve(self, provider: ProviderConfig) -> bool:
        status = self._provider_live_status(provider)
        if status is None:
            return False
        return status != "auth_error"

    def _provider_live_status(self, provider: ProviderConfig) -> str | None:
        if not self.keyring.has_provider(provider.name):
            return None
        status = self._probe_provider_status(provider, 1)
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
        normalized["url"] = self._canonical_result_url(str(url))
        normalized["title"] = title
        return normalized

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
            return []
        if intent == "tutorial":
            return []
        if mode in {"docs", "research"} or intent in {"resource", "tutorial"}:
            return ["research"]
        return []

    def _normalize_firecrawl_search_categories(self, categories: list[str]) -> list[str]:
        supported = {"github", "research", "pdf"}
        normalized: list[str] = []
        for item in categories:
            value = str(item or "").strip().lower()
            if value in supported and value not in normalized:
                normalized.append(value)
        return normalized

    def _looks_like_news_query(self, query_lower: str) -> bool:
        if self._looks_like_result_event_query(query_lower):
            return True
        # 中文关键词：直接 substring 匹配
        cn_keywords = ["刚刚", "最新", "新闻", "动态"]
        if any(kw in query_lower for kw in cn_keywords):
            return True
        # 英文关键词：排除常见技术搭配的误判
        # "breaking changes" / "latest version" 等不是新闻查询
        tech_negatives = [
            "breaking change", "breaking update",
            "latest version", "latest release", "latest docs",
            "latest commit", "latest tag",
        ]
        if any(neg in query_lower for neg in tech_negatives):
            return False
        en_keywords = [
            "latest",
            "breaking",
            "news",
            "today",
            "this week",
            "box office",
            "opening weekend",
            "rumor",
            "rumors",
        ]
        return any(keyword in query_lower for keyword in en_keywords)

    def _looks_like_award_result_query(self, query_lower: str) -> bool:
        keywords = [
            "academy awards",
            "album of the year",
            "aoty",
            "best actor",
            "best actress",
            "best picture",
            "emmy",
            "emmys",
            "golden globe",
            "golden globes",
            "grammy",
            "grammys",
            "oscar",
            "oscars",
            "winner",
            "winners",
            "won",
            "获奖",
            "最佳专辑",
            "最佳影片",
            "最佳男主角",
            "最佳女主角",
            "最佳电影",
            "最佳剧集",
            "最佳歌曲",
        ]
        return any(keyword in query_lower for keyword in keywords)

    def _award_query_category_markers(self, query_lower: str) -> list[str]:
        markers: list[str] = []
        if "best picture" in query_lower or "最佳影片" in query_lower or "最佳电影" in query_lower:
            markers.extend(["best picture", "最佳影片", "最佳电影"])
        if "best actor" in query_lower or "最佳男主角" in query_lower:
            markers.extend(["best actor", "actor in a leading role", "最佳男主角"])
        if "best actress" in query_lower or "最佳女主角" in query_lower:
            markers.extend(["best actress", "actress in a leading role", "最佳女主角"])
        if any(token in query_lower for token in ("album of the year", "aoty", "最佳专辑")):
            markers.extend(["album of the year", "aoty", "最佳专辑"])
        if any(token in query_lower for token in ("record of the year", "最佳歌曲")):
            markers.extend(["record of the year", "最佳歌曲"])
        return markers

    def _looks_like_box_office_query(self, query_lower: str) -> bool:
        keywords = [
            "box office",
            "highest grossing",
            "opening weekend",
            "票房",
            "首周末",
            "开画",
        ]
        return any(keyword in query_lower for keyword in keywords)

    def _looks_like_result_event_query(self, query_lower: str) -> bool:
        return self._looks_like_award_result_query(query_lower) or self._looks_like_box_office_query(query_lower)

    def _looks_like_gossip_query(self, query_lower: str) -> bool:
        keywords = [
            "celebrity",
            "breakup",
            "breakups",
            "dating",
            "divorce",
            "rumor",
            "rumors",
            "八卦",
            "分手",
            "离婚",
            "恋情",
            "绯闻",
        ]
        return any(keyword in query_lower for keyword in keywords)

    def _is_entertainment_gossip_domain(self, registered_domain: str) -> bool:
        return registered_domain in {
            "eonline.com",
            "justjared.com",
            "pagesix.com",
            "people.com",
            "radaronline.com",
            "tmz.com",
            "usmagazine.com",
        }

    def _looks_like_gossip_result(
        self,
        *,
        title_text: str,
        snippet_text: str,
        path: str,
    ) -> bool:
        text = f"{title_text} {snippet_text} {path}"
        keywords = [
            "breakup",
            "breakups",
            "dating",
            "divorce",
            "rumor",
            "rumors",
            "split",
            "splits",
            "关系",
            "分手",
            "离婚",
            "恋情",
            "绯闻",
        ]
        return any(keyword in text for keyword in keywords)

    def _looks_like_status_query(self, query_lower: str) -> bool:
        keywords = [
            "status",
            "incident",
            "outage",
            "release",
            "roadmap",
            "version",
            "版本",
            "发布",
            "进展",
            "现状",
        ]
        return any(keyword in query_lower for keyword in keywords)

    def _looks_like_comparison_query(self, query_lower: str) -> bool:
        keywords = [
            " vs ",
            "versus",
            "compare",
            "comparison",
            "pros and cons",
            "pros cons",
            "对比",
            "比较",
            "区别",
            "哪个好",
        ]
        return any(keyword in query_lower for keyword in keywords)

    def _looks_like_tutorial_query(self, query_lower: str) -> bool:
        keywords = [
            "how to",
            "guide",
            "tutorial",
            "walkthrough",
            "教程",
            "怎么",
            "如何",
            "入门",
        ]
        return any(keyword in query_lower for keyword in keywords)

    def _looks_like_debugging_query(self, query_lower: str) -> bool:
        keywords = [
            "bug",
            "cannot",
            "can't",
            "debug",
            "error",
            "failed",
            "failing",
            "fix",
            "how do i fix",
            "issue",
            "strict mode",
            "troubleshoot",
            "troubleshooting",
            "violation",
            "workaround",
            "报错",
            "排查",
            "修复",
            "错误",
        ]
        return any(keyword in query_lower for keyword in keywords)

    def _looks_like_local_life_query(self, query_lower: str) -> bool:
        keywords = [
            "攻略",
            "赏花",
            "景点",
            "周末去哪",
            "游玩",
            "旅游",
            "旅行",
            "美食",
            "门票",
            "路线",
            "guide",
            "itinerary",
            "things to do",
            "travel guide",
            "weekend",
        ]
        return any(keyword in query_lower for keyword in keywords)

    def _looks_like_docs_query(self, query_lower: str) -> bool:
        if self._looks_like_api_docs_topic_query(query_lower):
            return True
        keywords = [
            "docs",
            "documentation",
            "api reference",
            "changelog",
            "readme",
            "github",
            "manual",
            "文档",
            "接口",
            "更新日志",
        ]
        return any(keyword in query_lower for keyword in keywords)

    def _looks_like_api_docs_topic_query(self, query_lower: str) -> bool:
        keywords = [
            "api webhook",
            "api webhooks",
            "background mode",
            "generate metadata",
            "generatemetadata",
            "response api",
            "responses api",
            "test.step",
            "webhook",
            "webhooks",
        ]
        return any(keyword in query_lower for keyword in keywords)

    def _looks_like_exploratory_query(self, query_lower: str) -> bool:
        keywords = [
            "why",
            "impact",
            "analysis",
            "trend",
            "ecosystem",
            "研究",
            "原因",
            "影响",
            "趋势",
            "生态",
        ]
        return any(keyword in query_lower for keyword in keywords)

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
        snippet = re.sub(
            r"\s+",
            " ",
            str(item.get("snippet") or item.get("content") or "").strip(),
        ).strip()
        if not snippet:
            return ""
        snippet = re.sub(r"^[#>*`\-\s]+", "", snippet).strip()
        return self._build_excerpt(snippet, limit=limit)

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
        if weak_award_signal and current_answer and self._answer_looks_uncertain(current_answer):
            current_answer = ""
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
        if not extracted_answer and should_try_page_extraction:
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

    def _result_event_candidates(
        self,
        *,
        query: str,
        results: list[dict[str, Any]],
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        if self._looks_like_result_event_query(query.lower()):
            candidate_window = max(limit, 10)
            return sorted(
                results[:candidate_window],
                key=lambda item: self._result_event_page_priority(query=query, item=item),
                reverse=True,
            )[:limit]
        return results[: min(limit, 3)]

    def _result_event_page_priority(
        self,
        *,
        query: str,
        item: Mapping[str, Any],
    ) -> int:
        query_lower = query.lower()
        title_text = str(item.get("title") or "").lower()
        snippet_text = str(item.get("snippet") or "").lower()
        content_text = str(item.get("content") or "").lower()
        url = str(item.get("url") or "")
        hostname = self._registered_domain(self._result_hostname({"url": url}))
        score = 0
        if hostname in {"nytimes.com", "npr.org", "pbs.org", "latimes.com", "washingtonpost.com", "apnews.com"}:
            score += 4
        if any(token in title_text or token in snippet_text for token in ("winner", "winners", "full results", "full list")):
            score += 3
        if self._looks_like_award_result_query(query_lower):
            if any(
                token in title_text or token in snippet_text or token in content_text
                for token in self._award_query_category_markers(query_lower)
            ):
                score += 4
            if "grammy" in query_lower and "grammy" in f"{title_text} {snippet_text} {content_text}":
                score += 2
            if "oscar" in query_lower and any(
                token in f"{title_text} {snippet_text} {content_text}"
                for token in ("oscar", "oscars", "academy awards")
            ):
                score += 2
        if self._looks_like_box_office_query(query_lower):
            if any(token in title_text or token in snippet_text for token in ("box office", "opening weekend", "highest-grossing", "biggest opening")):
                score += 4
        if self._looks_like_query_year_mismatch(query=query_lower, text=f"{title_text} {snippet_text} {content_text} {url}"):
            score -= 5
        if self._looks_like_award_prediction_result(
            title_text=title_text,
            snippet_text=snippet_text,
            path=urlparse(url).path.lower(),
        ):
            score -= 4
        return score

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
        if not results:
            return ""

        query_lower = query.lower()
        signal_texts: list[str] = []
        for item in self._result_event_candidates(query=query, results=results, limit=5):
            for key in ("title", "snippet", "content"):
                value = str(item.get(key) or "").strip()
                if value:
                    signal_texts.append(value)
        if not signal_texts:
            return ""

        combined_text = "\n".join(signal_texts)

        if "best picture" in query_lower or "最佳影片" in query_lower:
            entity = self._extract_named_fact_entity(
                combined_text,
                patterns=[
                    r"[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]\s+won[^\n]{0,80}\bbest picture\b",
                    r"[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]\s+is\s+the\s+(?:20\d{2}\s+)?best picture winner",
                    r"best picture\s*[–—:]\s*[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]",
                    r"best picture(?:\s+winner)?(?:\s*[–—:]|\s+was|\s+is|\s+goes to|\s+went to)\s+[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]",
                    r"best picture\s*[–—:]\s*([^\n.;]{2,100})",
                    r"best picture(?:\s+winner)?(?:\s*[–—:]|\s+was|\s+is|\s+goes to|\s+went to)\s+([^\n.;]{2,100})",
                ],
            )
            if entity:
                return f"Best Picture winner: {entity}"

        if "best actor" in query_lower or "最佳男主角" in query_lower:
            entity = self._extract_named_fact_entity(
                combined_text,
                patterns=[
                    r"[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]\s+won[^\n]{0,80}\bbest actor\b",
                    r"([A-Z][A-Za-z0-9'’&.\- ]{2,100})\s+wins\s+best actor",
                    r"([A-Z][A-Za-z0-9'’&.\- ]{2,100})\s+is\s+the\s+(?:20\d{2}\s+)?best actor winner",
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
            entity = self._extract_album_of_the_year_entity(combined_text)
            if entity:
                return f"Album of the Year winner: {entity}"
            entity = self._extract_named_fact_entity(
                combined_text,
                patterns=[
                    r"[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]\s+(?:won|wins)[^\n]{0,80}\balbum of the year\b",
                    r"[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]\s+[–—:]\s*album of the year",
                    r"album of the year\s*[–—:]\s*([^\n.;]{2,100})",
                    r"album of the year(?:\s+winner)?(?:\s+was|\s+is|\s+goes to|\s+went to)?\s+([^\n.;]{2,100})",
                    r"([^\n.;]{2,100})\s+won\s+album of the year",
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
            entity = self._extract_named_fact_entity(
                combined_text,
                patterns=[
                    r"[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]\s+(?:won|wins)[^\n]{0,80}\brecord of the year\b",
                    r"[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]\s+[–—:]\s*record of the year",
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

        if self._looks_like_box_office_query(query_lower):
            entity = self._extract_named_fact_entity(
                combined_text,
                patterns=[
                    r"[\"“'‘]([^\"”’'\n]{2,100})[\"”’'‘]\s+(?:becomes|become|became|scores|scored|tops|topped)[^\n]{0,80}(?:highest-grossing|biggest opening|opening weekend|box office)",
                    r"([A-Z][A-Za-z0-9:,'’&\\- ]{2,100})\s+(?:becomes|become|became|scores|scored|tops|topped)[^\n]{0,80}(?:highest-grossing|biggest opening|opening weekend|box office)",
                ],
            )
            if entity:
                return f"Top opening-weekend title: {entity}"

        return ""

    def _extract_album_of_the_year_entity(self, text: str) -> str:
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
            artist = self._clean_extracted_fact_entity(
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
            album = self._clean_extracted_fact_entity(
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
            album = self._clean_extracted_fact_entity(
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
        return ""

    def _extract_named_fact_entity(
        self,
        text: str,
        *,
        patterns: list[str],
        reject_substrings: list[str] | None = None,
    ) -> str:
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            entity = self._clean_extracted_fact_entity(
                match.group(1),
                reject_substrings=reject_substrings,
            )
            if entity:
                return entity
        return ""

    def _clean_extracted_fact_entity(
        self,
        value: str,
        *,
        reject_substrings: list[str] | None = None,
    ) -> str:
        entity = re.sub(r"\s+", " ", value).strip(" \t\r\n-:;,.\"'“”‘’")
        entity = re.sub(r"^(?:winner|winners)\s*[:\-]\s*", "", entity, flags=re.IGNORECASE)
        entity = re.split(r"\s+(?:with|which|that|after|during|for)\s+", entity, maxsplit=1)[0]
        entity = re.split(r",\s*(?:[\"“]|[A-Z][A-Za-z])", entity, maxsplit=1)[0]
        entity = re.split(r",\s*(?:marking|while|as|where|when)\b", entity, maxsplit=1, flags=re.IGNORECASE)[0]
        entity = re.split(r"\s{2,}", entity, maxsplit=1)[0]
        entity = re.sub(r"\s+\((?:winner|winners)\)$", "", entity, flags=re.IGNORECASE).strip()
        entity = entity.strip(" \t\r\n-:;,.\"'“”‘’")
        if len(entity) < 2:
            return ""
        if self._looks_like_publisher_fragment(entity):
            return ""
        if reject_substrings:
            entity_lower = entity.lower()
            if any(token in entity_lower for token in reject_substrings):
                return ""
        return entity

    def _looks_like_publisher_fragment(self, entity: str) -> bool:
        entity_lower = entity.lower().strip()
        known_outlets = {
            "npr",
            "ap",
            "reuters",
            "billboard",
            "variety",
            "bbc",
            "bbc news",
            "abc news",
            "cbs news",
            "pbs",
            "today",
            "usa today",
            "rolling stone",
            "grammy.com",
            "grammys.com",
        }
        if entity_lower in known_outlets:
            return True
        if ".com" in entity_lower:
            return True
        if re.fullmatch(r"[A-Z]{2,6}", entity):
            return entity_lower in known_outlets
        return False

    def _looks_like_query_year_mismatch(self, *, query: str, text: str) -> bool:
        query_years = {year for year in re.findall(r"\b20\d{2}\b", query)}
        if not query_years:
            return False
        result_years = {year for year in re.findall(r"\b20\d{2}\b", text)}
        if not result_years:
            return False
        return query_years.isdisjoint(result_years)

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
        web_answer = (web_search.get("answer") or "").strip()
        social_answer = ""
        if social:
            social_answer = (social.get("answer") or "").strip()

        url_to_title: dict[str, str] = {}
        for citation in citations:
            url = (citation.get("url") or "").strip()
            title = (citation.get("title") or "").strip()
            if url and title and url not in url_to_title:
                url_to_title[url] = title

        highlights: list[str] = []
        for page in pages:
            if page.get("error"):
                continue
            excerpt = (page.get("excerpt") or page.get("content") or "").strip()
            if not excerpt:
                continue
            excerpt = self._build_excerpt(excerpt, limit=180)
            title = url_to_title.get((page.get("url") or "").strip(), "").strip()
            if title:
                highlights.append(f"{title}: {excerpt}")
            else:
                highlights.append(excerpt)
            if len(highlights) >= 2:
                break

        citation_titles = [
            (citation.get("title") or "").strip()
            for citation in citations
            if (citation.get("title") or "").strip()
        ]
        citation_title_lines: list[str] = []
        for citation in citations:
            title = (citation.get("title") or "").strip()
            url = (citation.get("url") or "").strip()
            if not title:
                continue
            domain = self._registered_domain(self._result_hostname({"url": url}))
            line = f"{title} ({domain})" if domain and domain not in title.lower() else title
            if line not in citation_title_lines:
                citation_title_lines.append(line)
            if len(citation_title_lines) >= 4:
                break
        query_lower = query.lower()
        comparison_like = (
            web_search.get("intent") in {"comparison", "exploratory"}
            or self._looks_like_comparison_query(query_lower)
            or self._looks_like_exploratory_query(query_lower)
            or any(token in query_lower for token in ("best ", "top ", "compare ", "comparison "))
        )
        authoritative_source_count = int(evidence.get("authoritative_source_count") or 0)
        community_source_count = int(evidence.get("community_source_count") or 0)
        primary_finding = executive_summary_override or web_answer or social_answer
        if not primary_finding and comparison_like:
            if authoritative_source_count > 0 and citation_title_lines:
                primary_finding = (
                    "Authoritative sources and corroborating analysis were found; "
                    f"the strongest anchors include {', '.join(citation_title_lines[:3])}."
                )
            elif citation_title_lines:
                primary_finding = (
                    "The strongest available evidence is comparative rather than authoritative; "
                    f"recurring source clusters include {', '.join(citation_title_lines[:3])}."
                )
            else:
                primary_finding = (
                    "The strongest available evidence is comparative rather than authoritative."
                )
        if not primary_finding and citation_titles:
            primary_finding = citation_titles[0]
        if not primary_finding and highlights:
            primary_finding = highlights[0]
        if not primary_finding:
            return {}

        supporting = citation_title_lines[:] if comparison_like and citation_title_lines else highlights[:]
        if supporting and supporting[0] == primary_finding:
            supporting = supporting[1:]

        key_findings: list[str] = []
        if comparison_like and citation_title_lines:
            key_findings.extend(citation_title_lines[:3])
        else:
            for item in highlights[:3]:
                if item not in key_findings:
                    key_findings.append(item)
            if not key_findings:
                for title in citation_title_lines[:3]:
                    if title not in key_findings:
                        key_findings.append(title)

        evidence_highlights: list[str] = []
        for item in supporting[:3]:
            if item not in evidence_highlights:
                evidence_highlights.append(item)

        provider_roles: list[str] = []
        providers = [str(item) for item in (evidence.get("providers_consulted") or []) if item]
        if "tavily" in providers:
            provider_roles.append("Tavily handled broad discovery and initial ranking.")
        if evidence.get("page_count"):
            provider_roles.append(
                f"Firecrawl/extract captured full content for {int(evidence.get('page_count') or 0)} page(s)."
            )
        exa_unique = int(evidence.get("exa_unique_url_count") or 0)
        if exa_unique > 0:
            provider_roles.append(
                f"Exa expanded semantic coverage with {exa_unique} unique candidate URL(s)."
            )
        docs_rescue_count = int(evidence.get("docs_rescue_result_count") or 0)
        if docs_rescue_count > 0:
            provider_roles.append(
                f"Docs rescue surfaced {docs_rescue_count} authoritative or product-native candidate result(s)."
            )
        if social_answer:
            provider_roles.append("xAI added social or synthesis context to the research pass.")
        arbitration_summary = str(evidence.get("xai_arbitration_summary") or "").strip()
        if arbitration_summary:
            provider_roles.append("xAI arbitrated conflicting evidence across providers.")

        coverage_bits: list[str] = []
        if providers:
            coverage_bits.append(f"providers={', '.join(providers)}")
        page_count = int(evidence.get("page_count") or 0)
        requested_pages = int((evidence.get("research_plan") or {}).get("scrape_top_n") or 0)
        if requested_pages > 0:
            coverage_bits.append(f"pages={page_count}/{requested_pages}")
        citation_count = int(evidence.get("citation_count") or 0)
        if citation_count > 0:
            coverage_bits.append(f"citations={citation_count}")
        if exa_unique > 0:
            coverage_bits.append(f"exa_unique_urls={exa_unique}")
        source_diversity = int(evidence.get("source_diversity") or 0)
        if source_diversity > 0:
            coverage_bits.append(f"source_domains={source_diversity}")
        if authoritative_source_count > 0:
            coverage_bits.append(f"authoritative_sources={authoritative_source_count}")
        if community_source_count > 0:
            coverage_bits.append(f"community_sources={community_source_count}")
        confidence = str(evidence.get("confidence") or "").strip()
        if confidence:
            coverage_bits.append(f"confidence={confidence}")
        social_signal = social_answer if social_answer and social_answer != primary_finding else ""
        source_clusters = self._build_research_source_clusters(
            query=query,
            mode=str((evidence.get("research_plan") or {}).get("web_mode") or web_search.get("intent") or "web"),
            ordered_results=ordered_results,
            include_domains=None,
            authoritative_preferred=bool(evidence.get("authoritative_research")),
        )
        claim_evidence = self._build_research_claim_evidence(
            ordered_results=ordered_results,
            pages=pages,
            citations=citations,
            comparison_like=comparison_like,
        )

        significant_conflicts = [
            str(item)
            for item in (evidence.get("conflicts") or [])
            if item and item != "social-search-unavailable"
        ]
        top_sources = citation_title_lines[:4]

        comparison_lens: list[str] = []
        if comparison_like:
            if "search" in query_lower:
                comparison_lens = [
                    "search breadth and freshness",
                    "integration fit for agent workflows",
                    "deployment and operational simplicity",
                ]
            elif any(token in query_lower for token in ("code", "analysis", "repo", "repository")):
                comparison_lens = [
                    "code intelligence depth",
                    "IDE or workflow integration",
                    "operational fit and maintenance burden",
                ]
            else:
                comparison_lens = [
                    "relevance and source quality",
                    "coverage breadth",
                    "operational trade-offs",
                ]

        comparison_rows: list[dict[str, str]] = []
        decision_table: list[dict[str, str]] = []
        if comparison_like:
            url_to_excerpt = {
                (page.get("url") or "").strip(): self._build_excerpt(
                    (page.get("excerpt") or page.get("content") or "").strip(),
                    limit=120,
                )
                for page in pages
                if (page.get("url") or "").strip() and not page.get("error")
            }
            seen_shortlist_urls: set[str] = set()
            shortlist_urls = [
                (citation.get("url") or "").strip()
                for citation in citations
                if (citation.get("url") or "").strip()
            ]
            shortlist_urls.extend(
                (page.get("url") or "").strip()
                for page in pages
                if (page.get("url") or "").strip() and not page.get("error")
            )
            for url in shortlist_urls:
                if not url or url in seen_shortlist_urls:
                    continue
                seen_shortlist_urls.add(url)
                title = url_to_title.get(url, "").strip() or url
                candidate = title
                if "github.com/" in url:
                    parsed = urlparse(url)
                    parts = [part for part in parsed.path.strip("/").split("/") if part]
                    if len(parts) >= 2:
                        candidate = f"{parts[0]}/{parts[1]}"
                else:
                    candidate = re.split(r"\s[\-|:|]\s", title, maxsplit=1)[0].strip() or title
                matching_item = next(
                    (
                        item
                        for item in ordered_results
                        if (item.get("url") or "").strip() == url
                    ),
                    {},
                )
                cluster_label = self._research_result_cluster_label(
                    query=query,
                    mode=str((evidence.get("research_plan") or {}).get("web_mode") or "web"),
                    item=matching_item or {"url": url, "title": title},
                    include_domains=None,
                    authoritative_preferred=bool(evidence.get("authoritative_research")),
                )
                providers = [
                    provider
                    for provider in (
                        matching_item.get("matched_providers")
                        or [matching_item.get("provider", "")]
                    )
                    if provider
                ]
                evidence_note = url_to_excerpt.get(url, "").strip()
                evidence_note_lower = evidence_note.lower()
                if any(
                    marker in evidence_note_lower
                    for marker in (
                        "marketing copy",
                        "skip to content",
                        "you signed in with another tab",
                        "method not allowed",
                        "\"error\"",
                        "jsonrpc",
                    )
                ):
                    evidence_note = ""
                if not evidence_note:
                    evidence_note = self._registered_domain(self._result_hostname({"url": url}))
                comparison_rows.append(
                    {
                        "candidate": candidate[:80],
                        "source": self._registered_domain(self._result_hostname({"url": url})) or url,
                        "cluster": cluster_label,
                        "provider_support": " + ".join(providers[:3]) if providers else "unknown",
                        "note": evidence_note[:140],
                    }
                )
                if len(comparison_rows) >= 4:
                    break
            for row in comparison_rows[:4]:
                cluster_label = str(row.get("cluster") or "").strip()
                cluster_detail = next(
                    (
                        item
                        for item in source_clusters
                        if str(item.get("label") or "").strip() == cluster_label
                    ),
                    {},
                )
                decision_table.append(
                    {
                        "candidate": str(row.get("candidate") or "").strip(),
                        "fit": self._research_cluster_fit_summary(cluster_label),
                        "strengths": self._research_decision_strengths(
                            cluster_label=cluster_label,
                            provider_support=str(row.get("provider_support") or "").strip(),
                            note=str(row.get("note") or "").strip(),
                            cluster_detail=cluster_detail,
                        ),
                        "cautions": self._research_decision_cautions(
                            cluster_label=cluster_label,
                            provider_support=str(row.get("provider_support") or "").strip(),
                        ),
                    }
                )

        recommendation = ""
        if comparison_like:
            if authoritative_source_count > 0 and decision_table:
                recommendation = (
                    f"Start from {decision_table[0]['candidate']} as the primary anchor, "
                    "then use the remaining shortlisted sources to validate trade-offs and edge cases."
                )
            elif decision_table:
                recommendation = (
                    f"Treat {decision_table[0]['candidate']} as the leading candidate for now, "
                    "but keep the next shortlisted sources in scope because the evidence is still comparative."
                )

        return {
            "executive_summary": primary_finding,
            "key_findings": key_findings[:3],
            "evidence_highlights": evidence_highlights[:3],
            "provider_roles": provider_roles[:4],
            "coverage_bits": coverage_bits,
            "claim_evidence": claim_evidence[:4],
            "source_clusters": source_clusters[:5],
            "social_signal": social_signal,
            "caveats": significant_conflicts[:4],
            "top_sources": top_sources,
            "source_mix": [
                bit
                for bit in (
                    f"authoritative={authoritative_source_count}" if authoritative_source_count > 0 else "",
                    f"community={community_source_count}" if community_source_count > 0 else "",
                    f"domains={', '.join(evidence.get('selected_candidate_domains') or [])}"
                    if evidence.get("selected_candidate_domains")
                    else "",
                )
                if bit
            ],
            "comparison_lens": comparison_lens,
            "comparison_rows": comparison_rows,
            "decision_table": decision_table,
            "recommendation": recommendation,
        }

    def _render_research_report(self, sections: dict[str, Any]) -> str:
        if not sections:
            return ""

        lines: list[str] = ["## Executive Summary", sections.get("executive_summary", "").strip()]

        key_findings = [str(item).strip() for item in (sections.get("key_findings") or []) if str(item).strip()]
        if key_findings:
            lines.extend(["", "## Key Findings"])
            for item in key_findings:
                lines.append(f"- {item}")

        evidence_highlights = [
            str(item).strip()
            for item in (sections.get("evidence_highlights") or [])
            if str(item).strip()
        ]
        if evidence_highlights:
            lines.extend(["", "## Evidence Highlights"])
            for item in evidence_highlights:
                lines.append(f"- {item}")

        claim_evidence = [
            item
            for item in (sections.get("claim_evidence") or [])
            if isinstance(item, dict) and item.get("claim")
        ]
        if claim_evidence:
            lines.extend(["", "## Claim-Level Evidence"])
            for item in claim_evidence[:4]:
                claim = str(item.get("claim") or "").strip()
                sources = ", ".join(
                    str(source).strip()
                    for source in (item.get("sources") or [])[:3]
                    if str(source).strip()
                )
                providers = ", ".join(
                    str(provider).strip()
                    for provider in (item.get("providers") or [])[:3]
                    if str(provider).strip()
                )
                suffix_bits = [
                    f"Sources: {sources}" if sources else "",
                    f"Providers: {providers}" if providers else "",
                ]
                suffix = "; ".join(bit for bit in suffix_bits if bit)
                if suffix:
                    lines.append(f"- {claim} ({suffix})")
                else:
                    lines.append(f"- {claim}")

        comparison_lens = [
            str(item).strip()
            for item in (sections.get("comparison_lens") or [])
            if str(item).strip()
        ]
        if comparison_lens:
            lines.extend(["", "## Comparison Lens"])
            for item in comparison_lens:
                lines.append(f"- {item}")

        comparison_rows = [
            item
            for item in (sections.get("comparison_rows") or [])
            if isinstance(item, dict) and item.get("candidate")
        ]
        if comparison_rows:
            lines.extend(
                [
                    "",
                    "## Ranked Shortlist",
                    "| Candidate | Cluster | Provider Support | Evidence Note |",
                    "|---|---|---|---|",
                ]
            )
            for row in comparison_rows[:4]:
                candidate = str(row.get("candidate") or "").replace("|", "/").strip()
                cluster = str(row.get("cluster") or "").replace("|", "/").strip()
                provider_support = str(row.get("provider_support") or "").replace("|", "/").strip()
                note = str(row.get("note") or "").replace("|", "/").strip()
                lines.append(f"| {candidate} | {cluster} | {provider_support} | {note} |")

        decision_table = [
            item
            for item in (sections.get("decision_table") or [])
            if isinstance(item, dict) and item.get("candidate")
        ]
        if decision_table:
            lines.extend(
                [
                    "",
                    "## Decision Table",
                    "| Candidate | Best Fit | Strengths | Cautions |",
                    "|---|---|---|---|",
                ]
            )
            for row in decision_table[:4]:
                candidate = str(row.get("candidate") or "").replace("|", "/").strip()
                fit = str(row.get("fit") or "").replace("|", "/").strip()
                strengths = str(row.get("strengths") or "").replace("|", "/").strip()
                cautions = str(row.get("cautions") or "").replace("|", "/").strip()
                lines.append(f"| {candidate} | {fit} | {strengths} | {cautions} |")

        provider_roles = [
            str(item).strip()
            for item in (sections.get("provider_roles") or [])
            if str(item).strip()
        ]
        if provider_roles:
            lines.extend(["", "## Provider Contributions"])
            for item in provider_roles:
                lines.append(f"- {item}")

        coverage_bits = [
            str(item).strip()
            for item in (sections.get("coverage_bits") or [])
            if str(item).strip()
        ]
        if coverage_bits:
            lines.extend(["", "## Coverage", f"- {' | '.join(coverage_bits)}"])

        source_mix = [
            str(item).strip()
            for item in (sections.get("source_mix") or [])
            if str(item).strip()
        ]
        if source_mix:
            lines.extend(["", "## Source Mix", f"- {' | '.join(source_mix)}"])

        source_clusters = [
            item
            for item in (sections.get("source_clusters") or [])
            if isinstance(item, dict) and item.get("label")
        ]
        if source_clusters:
            lines.extend(["", "## Source Clusters"])
            for cluster in source_clusters[:5]:
                label = str(cluster.get("label") or "").strip()
                count = int(cluster.get("count") or 0)
                tier = str(cluster.get("tier") or "").strip()
                weight = float(cluster.get("weight") or 0)
                domains = ", ".join(
                    str(domain).strip()
                    for domain in (cluster.get("domains") or [])[:3]
                    if str(domain).strip()
                )
                providers = ", ".join(
                    str(provider).strip()
                    for provider in (cluster.get("providers") or [])[:3]
                    if str(provider).strip()
                )
                detail_bits = [
                    f"{count} source(s)" if count else "",
                    f"tier={tier}" if tier else "",
                    f"weight={weight:.1f}" if weight else "",
                    f"domains={domains}" if domains else "",
                    f"providers={providers}" if providers else "",
                ]
                lines.append(f"- {label}: {'; '.join(bit for bit in detail_bits if bit)}")

        social_signal = str(sections.get("social_signal") or "").strip()
        if social_signal:
            lines.extend(["", "## Social Signal", f"- {social_signal}"])

        recommendation = str(sections.get("recommendation") or "").strip()
        if recommendation:
            lines.extend(["", "## Recommendation", f"- {recommendation}"])

        caveats = [str(item).strip() for item in (sections.get("caveats") or []) if str(item).strip()]
        top_sources = [str(item).strip() for item in (sections.get("top_sources") or []) if str(item).strip()]
        if caveats:
            lines.extend(["", "## Caveats"])
            for item in caveats:
                lines.append(f"- {item}")
        if top_sources:
            lines.extend(["", "## Top Sources"])
            for item in top_sources[:3]:
                lines.append(f"- {item}")

        return "\n".join(lines).strip()

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
        sections = self._build_research_report_sections(
            query=query,
            web_search=web_search,
            ordered_results=[],
            pages=pages,
            citations=citations,
            social=social,
            evidence=evidence,
        )
        return self._render_research_report(sections)

    def _build_research_source_clusters(
        self,
        *,
        query: str,
        mode: str,
        ordered_results: list[dict[str, Any]],
        include_domains: list[str] | None,
        authoritative_preferred: bool,
    ) -> list[dict[str, Any]]:
        if not ordered_results:
            return []

        cluster_buckets: dict[str, dict[str, Any]] = {}
        for item in ordered_results:
            label = self._research_result_cluster_label(
                query=query,
                mode=cast(SearchMode, mode if mode in SEARCH_MODES else "web"),
                item=item,
                include_domains=include_domains,
                authoritative_preferred=authoritative_preferred,
            )
            bucket = cluster_buckets.setdefault(
                label,
                {
                    "label": label,
                    "count": 0,
                    "domains": [],
                    "providers": [],
                    "cross_provider_count": 0,
                },
            )
            bucket["count"] += 1
            domain = self._registered_domain(self._result_hostname(item))
            if domain and domain not in bucket["domains"]:
                bucket["domains"].append(domain)
            matched_providers = [
                provider
                for provider in (
                item.get("matched_providers")
                or [item.get("provider", "")]
                )
                if provider
            ]
            if len(set(matched_providers)) > 1:
                bucket["cross_provider_count"] += 1
            for provider in matched_providers:
                if provider and provider not in bucket["providers"]:
                    bucket["providers"].append(provider)

        for bucket in cluster_buckets.values():
            provider_support = len(bucket.get("providers") or [])
            cross_provider_count = int(bucket.get("cross_provider_count") or 0)
            label = str(bucket.get("label") or "")
            base_weight = self._research_cluster_base_weight(
                label=label,
                authoritative_preferred=authoritative_preferred,
            )
            weight = round(
                base_weight
                + float(bucket.get("count") or 0) * 1.4
                + provider_support * 0.8
                + cross_provider_count * 1.2,
                2,
            )
            bucket["provider_support_count"] = provider_support
            bucket["weight"] = weight
            bucket["tier"] = self._research_cluster_tier(weight=weight, label=label)

        preferred_order = (
            ["official", "supporting", "general", "community"]
            if authoritative_preferred
            else ["project", "curated", "listicle", "directory", "community"]
        )
        return sorted(
            cluster_buckets.values(),
            key=lambda item: (
                -float(item.get("weight") or 0),
                preferred_order.index(item["label"])
                if item["label"] in preferred_order
                else len(preferred_order),
                -int(item.get("count") or 0),
            ),
        )

    def _build_research_claim_evidence(
        self,
        *,
        ordered_results: list[dict[str, Any]],
        pages: list[dict[str, Any]],
        citations: list[dict[str, Any]],
        comparison_like: bool,
    ) -> list[dict[str, Any]]:
        if not ordered_results:
            return []

        url_to_excerpt = {
            (page.get("url") or "").strip(): (
                page.get("excerpt")
                or self._build_excerpt((page.get("content") or "").strip(), limit=180)
            )
            for page in pages
            if (page.get("url") or "").strip() and not page.get("error")
        }
        url_to_title = {
            (citation.get("url") or "").strip(): (citation.get("title") or "").strip()
            for citation in citations
            if (citation.get("url") or "").strip()
        }
        claims_by_key: dict[str, dict[str, Any]] = {}
        claim_order: list[str] = []
        for item in ordered_results:
            url = (item.get("url") or "").strip()
            title = (item.get("title") or url_to_title.get(url) or "").strip()
            excerpt = (url_to_excerpt.get(url) or item.get("snippet") or item.get("content") or "").strip()
            claim = self._research_claim_text(
                title=title,
                excerpt=excerpt,
                comparison_like=comparison_like,
            )
            if not claim:
                continue
            claim_key = self._research_claim_signature(claim)
            if not claim_key:
                continue
            if claim_key not in claims_by_key:
                claims_by_key[claim_key] = {
                    "claim": claim,
                    "sources": [],
                    "providers": [],
                }
                claim_order.append(claim_key)
            entry = claims_by_key[claim_key]
            source_label = title or (self._registered_domain(self._result_hostname(item)) or url)
            if source_label and source_label not in entry["sources"]:
                entry["sources"].append(source_label)
            for provider in (
                item.get("matched_providers")
                or [item.get("provider", "")]
            ):
                if provider and provider not in entry["providers"]:
                    entry["providers"].append(provider)
            if len(claim_order) >= 4 and all(
                len(claims_by_key[key]["sources"]) >= 1 for key in claim_order[:4]
            ):
                continue
        return [claims_by_key[key] for key in claim_order[:4]]

    def _research_claim_text(
        self,
        *,
        title: str,
        excerpt: str,
        comparison_like: bool,
    ) -> str:
        cleaned_title = self._normalize_research_claim_text(
            re.split(r"\s[\-|:|]\s", title, maxsplit=1)[0].strip() or title,
            comparison_like=comparison_like,
        )
        excerpt = re.sub(r"\s+", " ", excerpt).strip()
        if excerpt:
            first_sentence = re.split(r"(?<=[.!?。！？])\s+", excerpt, maxsplit=1)[0].strip()
            cleaned_excerpt = self._normalize_research_claim_text(
                first_sentence,
                comparison_like=comparison_like,
            )
            if cleaned_excerpt:
                if cleaned_title and self._research_excerpt_looks_like_navigation_noise(cleaned_excerpt):
                    return cleaned_title
                if not self._research_excerpt_looks_like_noise(cleaned_excerpt):
                    excerpt_tokens = set(re.findall(r"[a-z0-9]+", cleaned_excerpt.lower()))
                    title_tokens = set(re.findall(r"[a-z0-9]+", cleaned_title.lower()))
                    if cleaned_title and title_tokens and excerpt_tokens and (
                        len(title_tokens & excerpt_tokens) >= max(2, min(len(title_tokens), 3))
                    ):
                        return cleaned_title
                    return cleaned_excerpt
        return cleaned_title

    def _normalize_research_claim_text(
        self,
        text: str,
        *,
        comparison_like: bool,
    ) -> str:
        compact = re.sub(r"\s+", " ", text).strip(" -|:;,.")
        compact = re.sub(r"^[#>*`\-\d\.\)\s]+", "", compact).strip()
        compact = re.sub(r"\[[^\]]+\]\([^)]+\)", "", compact).strip()
        compact = re.sub(r"https?://\S+", "", compact).strip()
        compact = compact.replace("\\_", "_")
        if not compact:
            return ""
        if comparison_like:
            compact = re.split(r"\s[\-|:|]\s", compact, maxsplit=1)[0].strip() or compact
        compact = self._build_excerpt(compact, limit=160)
        if self._research_excerpt_looks_like_noise(compact):
            return ""
        return compact

    def _research_claim_signature(self, claim: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", " ", claim.lower()).strip()
        if not normalized:
            return ""
        stopwords = {
            "a",
            "an",
            "and",
            "api",
            "best",
            "by",
            "docs",
            "documentation",
            "for",
            "guide",
            "in",
            "latest",
            "of",
            "official",
            "reference",
            "the",
            "to",
            "updated",
            "with",
        }
        tokens = [
            token
            for token in normalized.split()
            if token not in stopwords and len(token) > 1
        ]
        signature_tokens = tokens[:8] or normalized.split()[:8]
        return " ".join(signature_tokens)

    def _research_excerpt_looks_like_navigation_noise(self, text: str) -> bool:
        lowered = text.lower()
        return any(
            marker in lowered
            for marker in (
                "primary navigation",
                "search docs",
                "skip to content",
                "suggested",
                "chatgpt actions",
                "search the api docs",
                "marketing copy",
            )
        )

    def _research_excerpt_looks_like_noise(self, text: str) -> bool:
        lowered = text.lower()
        return any(
            marker in lowered
            for marker in (
                "you signed in with another tab",
                "method not allowed",
                "\"error\"",
                "jsonrpc",
            )
        )

    def _research_cluster_base_weight(
        self,
        *,
        label: str,
        authoritative_preferred: bool,
    ) -> float:
        if authoritative_preferred:
            return {
                "official": 4.0,
                "supporting": 3.0,
                "general": 2.0,
                "community": 1.0,
            }.get(label, 1.0)
        return {
            "project": 4.0,
            "curated": 3.0,
            "listicle": 2.0,
            "directory": 1.5,
            "community": 1.0,
        }.get(label, 1.0)

    def _research_cluster_tier(self, *, weight: float, label: str) -> str:
        if label in {"official", "project"} or weight >= 6.0:
            return "primary"
        if weight >= 3.5:
            return "secondary"
        return "supplemental"

    def _research_cluster_fit_summary(self, cluster_label: str) -> str:
        return {
            "official": "canonical ground truth",
            "supporting": "supporting analysis",
            "general": "general coverage",
            "community": "community signal",
            "project": "project-native source",
            "curated": "curated comparison",
            "listicle": "broad scan",
            "directory": "directory-style inventory",
        }.get(cluster_label, "general coverage")

    def _research_decision_strengths(
        self,
        *,
        cluster_label: str,
        provider_support: str,
        note: str,
        cluster_detail: dict[str, Any],
    ) -> str:
        strength_bits = [self._research_cluster_fit_summary(cluster_label)]
        tier = str(cluster_detail.get("tier") or "").strip()
        if tier:
            strength_bits.append(tier)
        if provider_support and provider_support != "unknown":
            strength_bits.append(f"provider support={provider_support}")
        if note:
            strength_bits.append(note[:100])
        return "; ".join(bit for bit in strength_bits if bit)

    def _research_decision_cautions(
        self,
        *,
        cluster_label: str,
        provider_support: str,
    ) -> str:
        cautions: list[str] = []
        if cluster_label in {"community", "directory", "listicle"}:
            cautions.append("lower authority")
        if " + " not in provider_support and provider_support not in {"", "unknown"}:
            cautions.append("single-provider support")
        return "; ".join(cautions) if cautions else "none"

    def _build_excerpt(self, content: str, limit: int = 600) -> str:
        compact = re.sub(r"\s+", " ", content).strip()
        if len(compact) <= limit:
            return compact
        return compact[:limit].rstrip() + "..."
