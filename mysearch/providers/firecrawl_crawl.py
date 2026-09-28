"""Firecrawl 的 map / crawl 子系统：作业提交、轮询、结果整形。

从 `mysearch/clients.py` 抽出的**唯一一个边界干净的内聚子系统**：外部没有任何
调用方，只通过 `crawl_site` / `map_site` 两个入口被 MCP 层调用；除了
`query_routing` 里已有的两个纯函数外，它对外部世界的全部需要就是

    transport.config
    transport._get_key_or_raise(provider)
    transport._request_json_selected(...)

也就是 `ProviderTransport` 已经提供的三样东西。因此这些函数不放在类里，
而是**把 transport 作为第一个参数注入**——这是本项目里 provider 层接口注入的
第一个真实用例，而不是预先搭的架子。

依赖方向单向：本模块依赖 `query_routing`（瞬时错误判定与结果整形）与
`providers.base` 的协议；`clients` 在上层依赖本模块。无环。

`MySearchClient` 保留同名方法作为一行委托，调用面不变。

`DEFAULT_KEY_COOLDOWN_SECONDS` / `MAX_PINNED_KEY_RETRY_DELAY_SECONDS` 跟着
搬过来：改动前它们在 `clients.py` 的读者全部集中在这几个函数内。
"""

from __future__ import annotations

import time
from typing import Any

from mysearch import query_routing
from mysearch.config import ProviderConfig
from mysearch.errors import MySearchError, MySearchHTTPError

#: 固定密钥（非托管池）在 429 后的默认冷却秒数。
DEFAULT_KEY_COOLDOWN_SECONDS = 60
#: 固定密钥重试延迟的上限；超过就直接失败而不是继续等。
MAX_PINNED_KEY_RETRY_DELAY_SECONDS = 120

#: 轮询 crawl 状态的起步间隔。上游 crawl 作业实测**固定要 13–15s**（与 `limit`
#: 和 `crawl_entire_domain` 无关 —— 返回 1 页也要 15.18s），所以这个间隔只是
#: **检测滞后**的粒度，不是吞吐旋钮。
CRAWL_POLL_INTERVAL_SECONDS = 2.0
#: 轮询退避倍率。2.0 → 4.0 → 8.0 …（上限见 `CRAWL_POLL_MAX_INTERVAL_SECONDS`）。
CRAWL_POLL_BACKOFF_FACTOR = 2.0
#: 退避上限。上游 crawl 实测固定 13–15s，所以超过这个间隔就纯粹是在增加
#: 检测滞后、却几乎不再省请求。
CRAWL_POLL_MAX_INTERVAL_SECONDS = 10.0


def request_json_with_transient_retry(
    transport: Any,
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
    return request_json_with_transient_retry_selected(
        transport,
        provider=provider,
        method=method,
        path=path,
        payload=payload,
        key=key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        attempts=attempts,
    )[0]


def request_json_with_transient_retry_selected(
    transport: Any,
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
            return transport._request_json_selected(
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
            if attempt < effective_attempts - 1 and query_routing._is_retryable_transient_error(exc):
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


def map_firecrawl(
    transport: Any,
    *,
    url: str,
    limit: int = 50,
    search: str | None = None,
) -> dict[str, Any]:
    provider = transport.config.firecrawl
    key = transport._get_key_or_raise(provider)
    payload: dict[str, Any] = {"url": url, "limit": limit}
    if search:
        payload["search"] = search
    response = request_json_with_transient_retry(
        transport,
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


def crawl_firecrawl(
    transport: Any,
    *,
    url: str,
    limit: int = 20,
    max_depth: int | None = None,
    crawl_entire_domain: bool = True,
    poll_interval_seconds: float = CRAWL_POLL_INTERVAL_SECONDS,
    max_poll_attempts: int = 30,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    provider = transport.config.firecrawl
    key = transport._get_key_or_raise(provider)
    configured_crawl_timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else transport.config.timeout_seconds + MAX_PINNED_KEY_RETRY_DELAY_SECONDS
    )
    crawl_timeout_seconds = max(0.001, float(configured_crawl_timeout))
    deadline = time.monotonic() + crawl_timeout_seconds

    def remaining_timeout() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MySearchError("firecrawl crawl deadline exceeded")
        return max(0.001, min(float(transport.config.timeout_seconds), remaining))

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
                result = request_json_with_transient_retry_selected(
                    transport,
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
                if attempt or not query_routing._is_retryable_transient_error(exc):
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
        return query_routing._build_firecrawl_crawl_result(
            url=url, limit=limit, transport=key.source, status_payload=start
        )
    status_path = f"{provider.path('crawl')}/{job_id}"
    status_payload: dict[str, Any] = start
    terminal = False
    # 退避轮询。一次 crawl 实测要在上游跑 13–15s（与 `limit` / `crawl_entire_domain`
    # 无关：返回 1 页也要 15.18s），而单 key 的配额可能是 **3 请求/分钟**
    # （实测上游报文 `Consumed (req/min): 3, Remaining: 0`）。固定 2s 间隔会发
    # 5–7 个请求，光是跑完一次 crawl 就打满整个窗口，于是下一次调用立刻 429
    # （实测：连续第 3 次必然失败。这也是 loop41-44 两行连续失败的根因）。
    #
    # **先睡再问**：作业刚提交时必然还是 processing，立刻问一次纯属浪费配额。
    # 实测把一次 crawl 的请求数从 5–7 降到 3–4（四次连续调用零 429），
    # 墙钟 13.7s → ~19s，全部是检测滞后。
    # ponytail: 固定倍率退避，没有按上游 `duration` 自适应；后者要等实测出
    # 各站点 duration 的分布才值得做。
    poll_interval = float(poll_interval_seconds)
    for _ in range(max(1, max_poll_attempts)):
        time.sleep(min(max(0.0, poll_interval), max(0.0, deadline - time.monotonic())))
        poll_interval = min(
            poll_interval * CRAWL_POLL_BACKOFF_FACTOR,
            CRAWL_POLL_MAX_INTERVAL_SECONDS,
        )
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
        if deadline - time.monotonic() <= 0:
            raise MySearchError("firecrawl crawl deadline exceeded")
    if not terminal:
        raise MySearchError("firecrawl crawl did not reach a terminal state before deadline")
    return query_routing._build_firecrawl_crawl_result(
        url=url, limit=limit, transport=key.source, status_payload=status_payload
    )


def crawl_site(
    transport: Any,
    *,
    url: str,
    limit: int = 20,
    max_depth: int | None = None,
    crawl_entire_domain: bool = True,
) -> dict[str, Any]:
    return crawl_firecrawl(
        transport,
        url=url,
        limit=limit,
        max_depth=max_depth,
        crawl_entire_domain=crawl_entire_domain,
    )


def map_site(
    transport: Any,
    *,
    url: str,
    limit: int = 50,
    search: str | None = None,
) -> dict[str, Any]:
    return map_firecrawl(transport, url=url, limit=limit, search=search)
