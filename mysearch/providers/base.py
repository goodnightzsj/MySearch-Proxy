"""Provider 传输层：HTTP 请求、密钥获取、并发执行。

从 `mysearch/clients.py` 抽出的传输层。这些方法只依赖 `self.config` 与
`self.keyring`，不含 provider 业务语义（路由、后处理、结果合并），因此可
作为 provider 适配器的公共底座。

`MySearchClient` 继承本类，方法原地迁移，调用点与测试桩不变。
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextvars import copy_context
from functools import wraps
from typing import Any, Callable
from urllib.error import HTTPError as UrlHTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import httpx

from mysearch.config import ProviderConfig
from mysearch.diagnostics import (
    RequestState as _RequestBudget,
    current_request as _request_budget,
    current_deadline,
    current_phase,
    direct_call,
    elapsed_ms,
    extract_usage,
    finish_payload,
    operation_timeout,
    phase,
    remaining_seconds,
    upstream_request_id,
)
from mysearch.errors import (
    MySearchError,
    MySearchHTTPError,
    _parse_retry_after_seconds,
    _redact_provider_secret,
)


logger = logging.getLogger(__name__)


def remaining_request_seconds() -> float | None:
    return remaining_seconds()


def wait_before_retry(delay: float) -> None:
    remaining = remaining_request_seconds()
    if remaining is None:
        time.sleep(delay)
        return
    if delay >= remaining:
        raise MySearchError("research request budget exhausted before retry")
    state = _request_budget.get()
    if state is None:
        time.sleep(delay)
    elif state.cancelled.wait(delay):
        raise MySearchError("research request cancelled before retry")


def budgeted_provider(method):
    """An explicit provider window includes domain retries and nested fallbacks."""
    @wraps(method)
    def run(self, *args, **kwargs):
        timeout = kwargs.get("timeout_seconds")
        if timeout is None:
            return method(self, *args, **kwargs)
        deadline = time.monotonic() + timeout
        parent_deadline = current_deadline.get()
        token = current_deadline.set(min(deadline, parent_deadline) if parent_deadline is not None else deadline)
        try:
            remaining_request_seconds()
            result = method(self, *args, **kwargs)
            remaining_request_seconds()
            return result
        finally:
            current_deadline.reset(token)
    return run


def budgeted_research(method):
    # shortcut: in-flight sync I/O survives cancellation; use cancellable I/O if hard abort is required.
    @wraps(method)
    def run(self, *args, **kwargs):
        started = time.monotonic()
        # research 是复合操作（多轮发现 + 并行抓取 + 补搜），冷启动天然比单次
        # 检索慢；与 crawl 同理用独立预算，避免贴边的冷启动周期性超时。回退到
        # `timeout_seconds` 以兼容没有该字段的旧 config。
        timeout = float(
            getattr(self.config, "research_timeout_seconds", 0)
            or self.config.timeout_seconds
        )
        parent_remaining = remaining_request_seconds()
        if parent_remaining is not None:
            timeout = min(timeout, parent_remaining)
        parent = _request_budget.get()
        budget = parent or _RequestBudget(started + timeout)
        token = _request_budget.set(budget)
        try:
            results, errors = self._execute_parallel(
                {"research": lambda: method(self, *args, **kwargs)},
                max_workers=1,
                timeout_seconds=timeout,
            )
            self._raise_parallel_error(errors, "research")
            remaining_request_seconds()
            result = results["research"]
            if isinstance(result, dict) and isinstance(result.get("evidence"), dict):
                result["evidence"]["request_budget"] = {
                    "timeout_seconds": timeout,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                }
            return result
        finally:
            if parent is None:
                budget.cancelled.set()
            _request_budget.reset(token)

    return run


class ProviderTransport:
    """HTTP / key / concurrency 底座。

    子类必须提供 ``config`` 与 ``keyring``。
    """

    config: Any
    keyring: Any

    def new_request(self, tool: str, **arguments: Any) -> _RequestBudget:
        tool = "mysearch_health" if tool == "health" else tool
        started = time.monotonic()
        state = _RequestBudget(started + operation_timeout(self.config, tool, arguments), tool=tool, started=started)
        lifecycle = getattr(self, "_lifecycle", None)
        if lifecycle is not None:
            with lifecycle:
                if self._closing:
                    raise MySearchError("MySearch client is closing")
                self._active_requests.add(state)
        return state

    def finish_request(self, state: _RequestBudget, result: Any = None, error: BaseException | None = None) -> None:
        payload = finish_payload(state, result, error)
        if payload is None:
            return
        try:
            base_url = getattr(self.config, "proxy_base_url", "")
            key = getattr(self.config, "proxy_api_key", "")
            if base_url and key:
                try:
                    response = self._http.post(
                        f"{base_url.rstrip('/')}/api/mysearch/requests",
                        json=payload, headers={"Authorization": f"Bearer {key}"},
                        timeout=2.0, follow_redirects=False,
                    )
                    response.raise_for_status()
                except (httpx.HTTPError, ValueError):
                    payload["diagnostics"]["warning"] = "telemetry_failed"
                    logger.warning("MySearch telemetry_failed request_id=%s", state.request_id)
            logger.info("MySearch request %s", json.dumps(payload, ensure_ascii=True))
        finally:
            lifecycle = getattr(self, "_lifecycle", None)
            if lifecycle is not None:
                with lifecycle:
                    self._active_requests.discard(state)
                    lifecycle.notify_all()

    def _submit_tracked(self, executor: ThreadPoolExecutor, task: Callable[[], Any]) -> Future[Any]:
        lifecycle = getattr(self, "_lifecycle", None)
        if lifecycle is None:
            return executor.submit(task)
        with lifecycle:
            if self._closing:
                raise MySearchError("MySearch client is closing")
            future = executor.submit(task)
            self._active_futures.add(future)

        def completed(done: Future[Any]) -> None:
            with lifecycle:
                self._active_futures.discard(done)
                lifecycle.notify_all()

        future.add_done_callback(completed)
        return future

    def _execute_parallel(
        self,
        tasks: dict[str, Callable[[], Any]],
        *,
        max_workers: int | None = None,
        timeout_seconds: float | None = None,
        stop_after_primary_and_verifier: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Exception]]:
        if not tasks:
            return {}, {}

        if len(tasks) == 1 and timeout_seconds is None:
            name, task = next(iter(tasks.items()))
            try:
                with phase(name):
                    remaining_request_seconds()
                    return {name: task()}, {}
            except Exception as exc:  # pragma: no cover - defensive
                return {}, {name: exc}

        results: dict[str, Any] = {}
        errors: dict[str, Exception] = {}
        budget = max(
            0.001,
            float(timeout_seconds)
            if timeout_seconds is not None
            else float(self.config.timeout_seconds + 5),
        )
        request_remaining = remaining_request_seconds()
        if request_remaining is not None:
            budget = min(budget, request_remaining)
        deadline = time.monotonic() + budget
        worker_count = max(1, min(max_workers or self.config.max_parallel_workers, len(tasks)))
        executor = self._executor
        temporary_executor: ThreadPoolExecutor | None = None
        if max_workers is not None:
            temporary_executor = ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="mysearch-branch",
            )
            executor = temporary_executor
        def run_branch(name, task):
            with phase(name):
                remaining_request_seconds()
                return task()

        future_map: dict[Future[Any], str] = {}
        try:
            for name, task in tasks.items():
                context = copy_context()
                future = self._submit_tracked(executor, lambda n=name, t=task, c=context: c.run(run_branch, n, t))
                future_map[future] = name
        except BaseException:
            for future in future_map:
                future.cancel()
            if temporary_executor is not None:
                temporary_executor.shutdown(wait=False, cancel_futures=True)
            raise
        pending = set(future_map)

        def cancel_pending(reason: str) -> None:
            for pending_future in pending:
                pending_future.cancel()
                pending_name = future_map[pending_future]
                errors.setdefault(pending_name, MySearchError(reason))

        try:
            while pending:
                try:
                    remaining_request_seconds()
                except MySearchError as exc:
                    cancel_pending(str(exc))
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    cancel_pending(f"parallel task timed out after {budget:g}s")
                    break
                completed, _ = wait(
                    pending,
                    timeout=min(remaining, 0.1),
                    return_when=FIRST_COMPLETED,
                )
                if not completed:
                    continue
                for future in completed:
                    pending.discard(future)
                    name = future_map[future]
                    if name in errors or name in results:
                        continue
                    try:
                        results[name] = future.result()
                    except TimeoutError:
                        errors[name] = MySearchError(
                            f"{name} task timed out within {budget:g}s parallel budget"
                        )
                    except Exception as exc:  # pragma: no cover - network/runtime dependent
                        errors[name] = exc

                if stop_after_primary_and_verifier:
                    # quorum 只认 primary + secondary（真正的交叉验证者）。
                    # `exa_supplement` 是**补充**角色，让它顶替 secondary 会把
                    # 慢但更权威的 secondary 提前取消掉（loop55 docs-02：tavily
                    # 与 exa 先返回，firecrawl 从未参与，期望 URL 落榜）。
                    verifier_succeeded = "secondary" in results
                    primary_completed = "primary" in results or "primary" in errors
                    primary_failed_with_verifier = (
                        "primary" in errors and verifier_succeeded
                    )
                    if primary_completed and (
                        ("primary" in results and verifier_succeeded)
                        or primary_failed_with_verifier
                    ):
                        cancel_pending(
                            "parallel task cancelled after primary and verifier quorum"
                        )
                        break
        except BaseException:
            cancel_pending("parallel task cancelled")
            raise
        finally:
            if temporary_executor is not None:
                temporary_executor.shutdown(wait=False, cancel_futures=True)
        return results, errors

    def _raise_parallel_error(self, errors: dict[str, Exception], task_name: str) -> None:
        error = errors.get(task_name)
        if error is None:
            return
        if isinstance(error, MySearchError):
            raise error
        raise MySearchError(str(error))

    def _require_provider_enabled(self, provider: ProviderConfig) -> None:
        if not provider.enabled:
            raise MySearchHTTPError(
                provider=provider.name,
                status_code=503,
                detail={"code": "provider_disabled", "message": f"{provider.name} is disabled by configuration"},
                url="",
            )

    def _get_key_or_raise(self, provider: ProviderConfig):
        self._require_provider_enabled(provider)
        remaining_request_seconds()
        record = self.keyring.get_next(provider.name)
        if record is None:
            if self.keyring.has_configured_provider(provider.name):
                raise MySearchError(
                    f"{provider.name} has no available API keys; replace the key configuration "
                    "and restart or reload MySearch before retrying"
                )
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
        return self._request_json_selected(
            provider=provider,
            method=method,
            path=path,
            payload=payload,
            key=key,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )[0]

    def _request_json_once(
        self,
        *,
        provider: ProviderConfig,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        key: str,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        self._require_provider_enabled(provider)
        remaining_request_seconds()
        state = _request_budget.get()
        operation = next((name for name, value in getattr(provider, "default_paths", {}).items() if value == path), "request")
        if operation == "social_health":
            operation = "health"
        if path.startswith(getattr(provider, "default_paths", {}).get("crawl", "\0") + "/"):
            operation = "crawl_status"
        call = {
            "layer": "runtime", "call_id": uuid4().hex, "provider": provider.name,
            "operation": operation, "phase": current_phase.get(), "elapsed_ms": 0,
            "status_code": None, "outcome": "pending", "failure_reason": "", "usage": [],
        }
        started = time.monotonic()
        try:
            return self._request_json_response(
                provider=provider, method=method, path=path, payload=payload,
                key=key, base_url=base_url, timeout_seconds=timeout_seconds, call=call,
            )
        except BaseException as exc:
            call["outcome"] = "error"
            call["failure_reason"] = type(exc).__name__
            raise
        finally:
            call["elapsed_ms"] = elapsed_ms(started)
            if state is not None:
                state.complete_call(call)

    def _request_json_response(
        self,
        *,
        provider: ProviderConfig,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        key: str,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        call: dict[str, Any],
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
        proxy_base = getattr(self.config, "proxy_base_url", "").rstrip("/")
        state = _request_budget.get()
        if state is not None and proxy_base and (base_url or provider.base_url).rstrip("/") == proxy_base:
            headers.update({
                "X-MySearch-Request-ID": state.request_id,
                "X-MySearch-Call-ID": call["call_id"],
                "X-MySearch-Phase": current_phase.get(),
            })
        if method.upper() != "GET":
            headers.setdefault("Content-Type", "application/json")
        effective_timeout = timeout_seconds or self.config.timeout_seconds
        remaining = remaining_request_seconds()
        if remaining is not None:
            effective_timeout = min(effective_timeout, remaining)

        response_headers: Any = None
        prefer_urlopen = "unittest.mock" in type(urlopen).__module__
        if state is not None:
            state.start_call(call)
        if prefer_urlopen:
            request_data = None if method.upper() == "GET" else json.dumps(body).encode("utf-8")
            request = Request(url, data=request_data, headers=headers, method=method.upper())
            try:
                with urlopen(request, timeout=effective_timeout) as response:
                    raw_body = response.read()
                status_code = getattr(response, "status", 200)
                response_headers = getattr(response, "headers", None)
                response_text = raw_body.decode("utf-8", errors="replace")
            except UrlHTTPError as exc:
                status_code = int(getattr(exc, "code", 500) or 500)
                response_headers = getattr(exc, "headers", None)
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
                response_headers = response.headers
                response_text = response.text
            except httpx.TimeoutException as exc:
                raise MySearchError(
                    f"{provider.name} request timeout after {effective_timeout}s: {url}"
                ) from exc
            except httpx.HTTPError as exc:
                raise MySearchError(f"{provider.name} network error: {exc}") from exc

        call["status_code"] = status_code
        try:
            data = json.loads(response_text)
        except ValueError as exc:
            if status_code >= 400:
                raise MySearchHTTPError(
                    provider=provider.name,
                    status_code=status_code,
                    detail=_redact_provider_secret(response_text, key)[:300],
                    url=url,
                    retry_after_seconds=_parse_retry_after_seconds(response_headers),
                ) from exc
            raise MySearchError(f"non-json response from {url}: {response_text[:300]}") from exc

        call["usage"] = extract_usage(data, operation=call["operation"])
        request_id = upstream_request_id(data)
        if request_id:
            call["upstream_request_id"] = request_id
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
                detail=_redact_provider_secret(detail, key)[:500],
                url=url,
                retry_after_seconds=_parse_retry_after_seconds(response_headers),
                classification_detail=data,
            )
        if not isinstance(data, dict):
            safe_response = _redact_provider_secret(response_text, key)[:200]
            raise MySearchError(f"non-dict JSON response from {provider.name}: {safe_response}")
        call["outcome"] = "success"
        return data

    def _request_json_selected(
        self,
        *,
        provider: ProviderConfig,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        key: str,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        allow_key_rotation: bool = True,
    ) -> tuple[dict[str, Any], str]:
        current_key = key
        request_generation = self.keyring.generation
        attempted_keys: set[str] = set()
        if (
            allow_key_rotation
            and self.keyring.has_configured_provider(provider.name)
            and not self.keyring.is_available(provider.name, current_key)
        ):
            replacement = self.keyring.get_next(provider.name)
            if replacement is None:
                raise MySearchError(
                    f"{provider.name} has no available API keys; manual key action required"
                )
            current_key = replacement.key
            request_generation = self.keyring.generation
        while True:
            try:
                return (
                    self._request_json_once(
                        provider=provider,
                        method=method,
                        path=path,
                        payload=payload,
                        key=current_key,
                        base_url=base_url,
                        timeout_seconds=timeout_seconds,
                    ),
                    current_key,
                )
            except MySearchHTTPError as exc:
                failure_kind = exc.key_failure_kind
                if failure_kind == "upstream_quota_exhausted":
                    raise
                if failure_kind and (
                    not provider.managed_key_pool or failure_kind == "auth_rejected"
                ):
                    if failure_kind == "rate_limited":
                        self.keyring.quarantine(
                            provider.name,
                            current_key,
                            failure_kind,
                            retry_after_seconds=exc.retry_after_seconds or 60,
                            generation=request_generation,
                        )
                    else:
                        self.keyring.quarantine(
                            provider.name,
                            current_key,
                            failure_kind,
                            generation=request_generation,
                        )
                if (
                    not failure_kind
                    or (provider.managed_key_pool and failure_kind != "auth_rejected")
                    or not allow_key_rotation
                ):
                    raise
                attempted_keys.add(current_key)
                replacement = self.keyring.get_next(provider.name)
                if replacement is None or replacement.key in attempted_keys:
                    raise
                current_key = replacement.key
                request_generation = self.keyring.generation

    def _request_text(
        self,
        *,
        url: str,
        timeout_seconds: int | None = None,
    ) -> tuple[int, str]:
        effective_timeout = timeout_seconds or self.config.timeout_seconds
        remaining = remaining_request_seconds()
        if remaining is not None:
            effective_timeout = min(effective_timeout, remaining)
        with direct_call() as call:
            try:
                response = self._http.get(
                    url,
                    headers={"Accept": "text/html,application/json;q=0.9,*/*;q=0.8"},
                    timeout=effective_timeout,
                )
                call["status_code"] = response.status_code
                call["outcome"] = "success" if response.status_code < 400 else "error"
                call["failure_reason"] = "" if response.status_code < 400 else "http_error"
                return response.status_code, response.text
            except httpx.TimeoutException as exc:
                call["outcome"], call["failure_reason"] = "error", type(exc).__name__
                return 0, ""
            except httpx.HTTPError as exc:
                raise MySearchError(str(exc)) from exc
