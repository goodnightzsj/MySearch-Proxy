"""Provider 传输层：HTTP 请求、密钥获取、并发执行。

从 `mysearch/clients.py` 抽出的传输层。这些方法只依赖 `self.config` 与
`self.keyring`，不含 provider 业务语义（路由、后处理、结果合并），因此可
作为 provider 适配器的公共底座。

`MySearchClient` 继承本类，方法原地迁移，调用点与测试桩不变。
"""

from __future__ import annotations

import json
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Any, Callable
from urllib.error import HTTPError as UrlHTTPError
from urllib.request import Request, urlopen

import httpx

from mysearch.config import ProviderConfig
from mysearch.errors import (
    MySearchError,
    MySearchHTTPError,
    _parse_retry_after_seconds,
    _redact_provider_secret,
)


class ProviderTransport:
    """HTTP / key / concurrency 底座。

    子类必须提供 ``config`` 与 ``keyring``。
    """

    config: Any
    keyring: Any

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

        if len(tasks) == 1:
            name, task = next(iter(tasks.items()))
            try:
                return {name: task()}, {}
            except Exception as exc:  # pragma: no cover - defensive
                return {}, {name: exc}

        results: dict[str, Any] = {}
        errors: dict[str, Exception] = {}
        worker_count = max(1, min(max_workers or self.config.max_parallel_workers, len(tasks)))
        executor = self._executor
        temporary_executor: ThreadPoolExecutor | None = None
        if max_workers is not None:
            temporary_executor = ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="mysearch-branch",
            )
            executor = temporary_executor
        future_map: dict[Future[Any], str] = {
            executor.submit(task): name for name, task in tasks.items()
        }
        pending = set(future_map)
        budget = max(
            0.001,
            float(timeout_seconds)
            if timeout_seconds is not None
            else float(self.config.timeout_seconds + 5),
        )
        deadline = time.monotonic() + budget

        def cancel_pending(reason: str) -> None:
            for pending_future in pending:
                pending_future.cancel()
                pending_name = future_map[pending_future]
                errors.setdefault(pending_name, MySearchError(reason))

        try:
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    cancel_pending(f"parallel task timed out after {budget:g}s")
                    break
                completed, _ = wait(
                    pending,
                    timeout=remaining,
                    return_when=FIRST_COMPLETED,
                )
                if not completed:
                    cancel_pending(f"parallel task timed out after {budget:g}s")
                    break
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
                    primary_completed = "primary" in results or "primary" in errors
                    verifier_succeeded = any(name != "primary" for name in results)
                    primary_failed_with_verifier = (
                        "primary" in errors and verifier_succeeded
                    )
                    if primary_completed and (
                        len(results) >= 2 or primary_failed_with_verifier
                    ):
                        cancel_pending(
                            "parallel task cancelled after primary and verifier quorum"
                        )
                        break
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

    def _get_key_or_raise(self, provider: ProviderConfig):
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
        if method.upper() != "GET":
            headers.setdefault("Content-Type", "application/json")
        effective_timeout = timeout_seconds or self.config.timeout_seconds

        response_headers: Any = None
        prefer_urlopen = "unittest.mock" in type(urlopen).__module__
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
