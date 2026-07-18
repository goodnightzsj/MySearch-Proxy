from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx


REPO_ROOT = Path(__file__).resolve().parents[1]
PROXY_ROOT = REPO_ROOT / "proxy"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(PROXY_ROOT) not in sys.path:
    sys.path.insert(0, str(PROXY_ROOT))

from mysearch.clients import (
    MySearchClient,
    MySearchHTTPError,
    _parse_retry_after_seconds,
)
from mysearch.config import MySearchConfig, ProviderConfig
from mysearch.keyring import MySearchKeyRing
import key_pool as proxy_key_pool


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _minimal_provider(name: str, keys: list[str]) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        base_url="https://example.com",
        auth_mode="bearer",
        auth_header="Authorization",
        auth_scheme="Bearer",
        auth_field="api_key",
        default_paths={"search": "/search"},
        api_keys=keys,
    )


def _minimal_config(tavily_keys: list[str]) -> MySearchConfig:
    return MySearchConfig(
        server_name="test",
        timeout_seconds=10,
        xai_social_timeout_seconds=120,
        xai_model="grok-test",
        xai_models=(),
        max_parallel_workers=2,
        search_cache_ttl_seconds=0,
        extract_cache_ttl_seconds=0,
        mcp_host="127.0.0.1",
        mcp_port=8000,
        mcp_mount_path="/",
        mcp_sse_path="/sse",
        mcp_streamable_http_path="/mcp",
        mcp_stateless_http=False,
        tavily=_minimal_provider("tavily", tavily_keys),
        firecrawl=_minimal_provider("firecrawl", ["firecrawl-key"]),
        exa=_minimal_provider("exa", ["exa-key"]),
        xai=_minimal_provider("xai", ["xai-key"]),
    )


class DirectKeySchedulingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = _minimal_config(["first-key", "second-key"])
        self.ring = MySearchKeyRing(self.config)

    def test_rate_limited_key_recovers_after_retry_after(self) -> None:
        with patch("mysearch.keyring.time.monotonic", return_value=100.0):
            self.ring.quarantine(
                "tavily",
                "first-key",
                "rate_limited",
                retry_after_seconds=30,
            )
            self.assertEqual(self.ring.describe()["tavily"]["count"], 1)

        with patch("mysearch.keyring.time.monotonic", return_value=131.0):
            self.assertEqual(self.ring.describe()["tavily"]["count"], 2)

    def test_direct_retry_after_accepts_http_date(self) -> None:
        retry_at = datetime.now(timezone.utc) + timedelta(seconds=30)
        parsed = _parse_retry_after_seconds(
            {"Retry-After": format_datetime(retry_at, usegmt=True)}
        )

        self.assertIsNotNone(parsed)
        self.assertGreaterEqual(parsed, 1)
        self.assertLessEqual(parsed, 30)

    def test_quota_exhausted_key_requires_explicit_reload(self) -> None:
        self.ring.quarantine("tavily", "first-key", "quota_exhausted")
        self.assertEqual(self.ring.describe()["tavily"]["count"], 1)
        self.ring.reload()
        self.assertEqual(self.ring.describe()["tavily"]["count"], 2)

    def test_terminal_quarantine_cannot_be_downgraded_by_late_429(self) -> None:
        with patch("mysearch.keyring.time.monotonic", return_value=100.0):
            self.ring.quarantine("tavily", "first-key", "quota_exhausted")
        with patch("mysearch.keyring.time.monotonic", return_value=110.0):
            self.ring.quarantine(
                "tavily",
                "first-key",
                "rate_limited",
                retry_after_seconds=30,
            )
        with patch("mysearch.keyring.time.monotonic", return_value=1000.0):
            description = self.ring.describe()["tavily"]

        self.assertEqual(description["count"], 1)
        self.assertEqual(description["quarantine_reasons"], ["quota_exhausted"])

    def test_late_shorter_429_cannot_shorten_existing_cooldown(self) -> None:
        with patch("mysearch.keyring.time.monotonic", return_value=100.0):
            self.ring.quarantine(
                "tavily",
                "first-key",
                "rate_limited",
                retry_after_seconds=120,
            )
        with patch("mysearch.keyring.time.monotonic", return_value=110.0):
            self.ring.quarantine(
                "tavily",
                "first-key",
                "rate_limited",
                retry_after_seconds=30,
            )
        with patch("mysearch.keyring.time.monotonic", return_value=141.0):
            self.assertEqual(self.ring.describe()["tavily"]["count"], 1)
        with patch("mysearch.keyring.time.monotonic", return_value=221.0):
            self.assertEqual(self.ring.describe()["tavily"]["count"], 2)

    def test_request_rotates_after_429_and_keeps_next_key_for_later_calls(self) -> None:
        client = MySearchClient(config=self.config, keyring=self.ring)
        calls: list[str] = []

        def request_once(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs["key"])
            if kwargs["key"] == "first-key":
                raise MySearchHTTPError(
                    provider="tavily",
                    status_code=429,
                    detail="rate limit exceeded",
                    url="https://example.com/search",
                    retry_after_seconds=30,
                )
            return {"ok": True}

        client._request_json_once = request_once  # type: ignore[method-assign]
        provider = self.config.tavily
        self.assertEqual(
            client._request_json(
                provider=provider,
                method="POST",
                path="/search",
                payload={},
                key="first-key",
            ),
            {"ok": True},
        )
        self.assertEqual(
            client._request_json(
                provider=provider,
                method="GET",
                path="/status",
                payload=None,
                key="first-key",
            ),
            {"ok": True},
        )
        self.assertEqual(calls, ["first-key", "second-key", "second-key"])

    def test_every_direct_provider_rotates_after_key_limit(self) -> None:
        for provider_name in ("tavily", "firecrawl", "exa", "xai"):
            with self.subTest(provider=provider_name):
                config = _minimal_config(["tavily-key"])
                provider = getattr(config, provider_name)
                provider.api_keys = [f"{provider_name}-first", f"{provider_name}-second"]
                ring = MySearchKeyRing(config)
                client = MySearchClient(config=config, keyring=ring)
                calls: list[str] = []

                def request_once(**kwargs):  # type: ignore[no-untyped-def]
                    calls.append(kwargs["key"])
                    if kwargs["key"].endswith("-first"):
                        raise MySearchHTTPError(
                            provider=provider_name,
                            status_code=429,
                            detail="rate limit exceeded",
                            url="https://example.com/search",
                            retry_after_seconds=30,
                        )
                    return {"ok": True}

                client._request_json_once = request_once  # type: ignore[method-assign]
                result = client._request_json(
                    provider=provider,
                    method="POST",
                    path="/search",
                    payload={},
                    key=f"{provider_name}-first",
                )
                self.assertEqual(result, {"ok": True})
                self.assertEqual(
                    calls,
                    [f"{provider_name}-first", f"{provider_name}-second"],
                )

    def test_429_quota_detail_is_permanently_quarantined(self) -> None:
        error = MySearchHTTPError(
            provider="exa",
            status_code=429,
            detail="You have exceeded your credits for this billing period",
            url="https://example.com/search",
        )
        self.assertEqual(error.key_failure_kind, "quota_exhausted")

    def test_plural_credits_limit_is_permanently_quarantined(self) -> None:
        error = MySearchHTTPError(
            provider="exa",
            status_code=429,
            detail="You have exceeded your credits limit.",
            url="https://example.com/search",
        )
        self.assertEqual(error.key_failure_kind, "quota_exhausted")

    def test_bare_403_does_not_quarantine_a_key(self) -> None:
        error = MySearchHTTPError(
            provider="firecrawl",
            status_code=403,
            detail="forbidden by upstream policy",
            url="https://example.com/search",
        )
        self.assertEqual(error.key_failure_kind, "")
        self.assertFalse(error.is_auth_error)

    def test_credential_specific_403_is_quarantined(self) -> None:
        error = MySearchHTTPError(
            provider="exa",
            status_code=403,
            detail={"error": "invalid api key"},
            url="https://example.com/search",
        )
        self.assertEqual(error.key_failure_kind, "auth_rejected")

    def test_structured_error_code_is_preserved_for_key_rotation(self) -> None:
        client = MySearchClient(config=self.config, keyring=self.ring)
        calls: list[str] = []

        def request(_method, _url, **kwargs):  # type: ignore[no-untyped-def]
            selected_key = kwargs["headers"]["Authorization"].removeprefix("Bearer ")
            calls.append(selected_key)
            if selected_key == "first-key":
                return httpx.Response(
                    403,
                    json={"error": "Forbidden", "code": "invalid_api_key"},
                )
            return httpx.Response(200, json={"ok": True})

        with patch.object(client._http, "request", side_effect=request):
            result = client._request_json(
                provider=self.config.tavily,
                method="POST",
                path="/search",
                payload={},
                key="first-key",
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, ["first-key", "second-key"])
        self.assertEqual(self.ring.describe()["tavily"]["quarantine_reasons"], ["auth_rejected"])

    def test_scope_403_does_not_quarantine_a_key(self) -> None:
        error = MySearchHTTPError(
            provider="exa",
            status_code=403,
            detail="unauthorized for this endpoint; additional scope required",
            url="https://example.com/search",
        )
        self.assertEqual(error.key_failure_kind, "")

    def test_quota_words_on_server_errors_do_not_quarantine_a_key(self) -> None:
        for status_code in (400, 500):
            with self.subTest(status_code=status_code):
                error = MySearchHTTPError(
                    provider="tavily",
                    status_code=status_code,
                    detail="documentation for the usage limit field",
                    url="https://example.com/search",
                )
                self.assertEqual(error.key_failure_kind, "")

    def test_managed_pool_does_not_quarantine_proxy_token(self) -> None:
        provider = self.config.tavily
        provider.managed_key_pool = True
        client = MySearchClient(config=self.config, keyring=self.ring)
        client._request_json_once = lambda **_: (_ for _ in ()).throw(  # type: ignore[method-assign]
            MySearchHTTPError(
                provider="tavily",
                status_code=429,
                detail="upstream pool limited",
                url="https://proxy.example/search",
                retry_after_seconds=30,
            )
        )

        with self.assertRaises(MySearchHTTPError):
            client._request_json(
                provider=provider,
                method="POST",
                path="/search",
                payload={},
                key="first-key",
            )
        self.assertEqual(self.ring.describe()["tavily"]["count"], 2)

    def test_managed_pool_auth_failure_rotates_proxy_token(self) -> None:
        provider = self.config.tavily
        provider.managed_key_pool = True
        client = MySearchClient(config=self.config, keyring=self.ring)
        calls: list[str] = []

        def request_once(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs["key"])
            if kwargs["key"] == "first-key":
                raise MySearchHTTPError(
                    provider="tavily",
                    status_code=401,
                    detail="invalid proxy credential",
                    url="https://proxy.example/search",
                )
            return {"ok": True}

        client._request_json_once = request_once  # type: ignore[method-assign]
        result = client._request_json(
            provider=provider,
            method="POST",
            path="/search",
            payload={},
            key="first-key",
        )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, ["first-key", "second-key"])

    def test_proxy_mode_uses_only_proxy_token_for_managed_providers(self) -> None:
        env = {
            "MYSEARCH_PROXY_BASE_URL": "http://proxy.example",
            "MYSEARCH_PROXY_API_KEY": "mysp-proxy-token",
            "MYSEARCH_FIRECRAWL_API_KEYS": "fc-direct-a,fc-direct-b",
            "MYSEARCH_EXA_API_KEYS": "exa-direct-a,exa-direct-b",
            "MYSEARCH_XAI_API_KEYS": "xai-direct-a,xai-direct-b",
        }
        with patch.dict(os.environ, env, clear=True):
            config = MySearchConfig.from_env()

        for provider in (config.firecrawl, config.exa, config.xai):
            with self.subTest(provider=provider.name):
                self.assertEqual(provider.api_keys, ["mysp-proxy-token"])
                self.assertIsNone(provider.keys_file)
                self.assertTrue(provider.managed_key_pool)

    def test_explicit_direct_provider_config_wins_over_proxy_base(self) -> None:
        env = {
            "MYSEARCH_PROXY_BASE_URL": "http://proxy.example",
            "MYSEARCH_PROXY_API_KEY": "mysp-proxy-token",
            "MYSEARCH_FIRECRAWL_BASE_URL": "https://direct.firecrawl.example",
            "MYSEARCH_FIRECRAWL_API_KEYS": "fc-direct-a,fc-direct-b",
            "MYSEARCH_EXA_BASE_URL": "https://direct.exa.example",
            "MYSEARCH_EXA_API_KEYS": "exa-direct-a,exa-direct-b",
            "MYSEARCH_XAI_SEARCH_MODE": "official",
            "MYSEARCH_XAI_API_KEYS": "xai-direct-a,xai-direct-b",
        }
        with patch.dict(os.environ, env, clear=True):
            config = MySearchConfig.from_env()

        self.assertEqual(config.firecrawl.base_url, "https://direct.firecrawl.example")
        self.assertEqual(config.firecrawl.path("search"), "/v2/search")
        self.assertEqual(config.firecrawl.api_keys, ["fc-direct-a", "fc-direct-b"])
        self.assertFalse(config.firecrawl.managed_key_pool)
        self.assertEqual(config.exa.base_url, "https://direct.exa.example")
        self.assertEqual(config.exa.path("search"), "/search")
        self.assertEqual(config.exa.auth_header, "x-api-key")
        self.assertEqual(config.exa.api_keys, ["exa-direct-a", "exa-direct-b"])
        self.assertFalse(config.exa.managed_key_pool)
        self.assertEqual(config.xai.api_keys, ["xai-direct-a", "xai-direct-b"])
        self.assertFalse(config.xai.managed_key_pool)

    def test_tavily_proxy_route_does_not_follow_firecrawl_direct_override(self) -> None:
        env = {
            "MYSEARCH_PROXY_BASE_URL": "http://proxy.example",
            "MYSEARCH_PROXY_API_KEY": "mysp-proxy-token",
            "MYSEARCH_FIRECRAWL_BASE_URL": "https://direct.firecrawl.example",
            "MYSEARCH_TAVILY_MODE": "gateway",
        }
        with patch.dict(os.environ, env, clear=True):
            config = MySearchConfig.from_env()

        self.assertEqual(config.tavily.base_url, "http://proxy.example")
        self.assertEqual(config.tavily.path("search"), "/api/search")
        self.assertEqual(config.tavily.api_keys, ["mysp-proxy-token"])

    def test_external_tavily_gateway_never_receives_proxy_token(self) -> None:
        env = {
            "MYSEARCH_PROXY_BASE_URL": "http://proxy.example",
            "MYSEARCH_PROXY_API_KEY": "mysp-proxy-secret",
            "MYSEARCH_TAVILY_MODE": "gateway",
            "MYSEARCH_TAVILY_GATEWAY_BASE_URL": "https://gateway.example",
        }
        with patch.dict(os.environ, env, clear=True):
            config = MySearchConfig.from_env()

        self.assertEqual(config.tavily.base_url, "https://gateway.example")
        self.assertEqual(config.tavily.api_keys, [])

    def test_probe_rate_limit_cache_expires_with_retry_after(self) -> None:
        client = MySearchClient(config=self.config, keyring=self.ring)
        now = [100.0]
        probe = Mock(
            side_effect=[
                MySearchHTTPError(
                    provider="tavily",
                    status_code=429,
                    detail="rate limit exceeded",
                    url="https://example.com/search",
                    retry_after_seconds=30,
                ),
                None,
            ]
        )
        client._probe_provider_request = probe  # type: ignore[method-assign]

        with patch("mysearch.clients.time.monotonic", side_effect=lambda: now[0]):
            first = client._probe_provider_status(self.config.tavily, 2)
            now[0] = 120.0
            cached = client._probe_provider_status(self.config.tavily, 2)
            now[0] = 131.0
            recovered = client._probe_provider_status(self.config.tavily, 2)

        self.assertEqual(first["status"], "http_error")
        self.assertEqual(cached["status"], "http_error")
        self.assertEqual(recovered["status"], "ok")
        self.assertEqual(probe.call_count, 2)

    def test_probe_cache_does_not_hide_an_earlier_key_recovery(self) -> None:
        client = MySearchClient(config=self.config, keyring=self.ring)
        ring_now = [100.0]
        client_now = [100.0]
        calls: list[str] = []

        def probe(_provider, key):  # type: ignore[no-untyped-def]
            calls.append(key)
            if len(calls) == 1 and key == "first-key":
                self.ring.quarantine(
                    "tavily",
                    key,
                    "rate_limited",
                    retry_after_seconds=30,
                )
                self.ring.quarantine(
                    "tavily",
                    "second-key",
                    "rate_limited",
                    retry_after_seconds=120,
                )
                raise MySearchHTTPError(
                    provider="tavily",
                    status_code=429,
                    detail="rate limit exceeded",
                    url="https://example.com/search",
                    retry_after_seconds=120,
                )

        client._probe_provider_request = probe  # type: ignore[method-assign]
        with patch("mysearch.keyring.time.monotonic", side_effect=lambda: ring_now[0]), patch(
            "mysearch.clients.time.monotonic",
            side_effect=lambda: client_now[0],
        ):
            first_available = client._provider_can_serve(self.config.tavily)
            ring_now[0] = 131.0
            client_now[0] = 131.0
            recovered_available = client._provider_can_serve(self.config.tavily)

        self.assertFalse(first_available)
        self.assertTrue(recovered_available)
        self.assertEqual(calls, ["first-key", "first-key"])

    def test_explicit_reload_invalidates_all_key_failure_probe_caches(self) -> None:
        cases = (
            (401, "invalid api key", None),
            (429, "quota exhausted", None),
            (429, "rate limit exceeded", 300),
        )
        for status_code, detail, retry_after_seconds in cases:
            with self.subTest(status_code=status_code, detail=detail):
                ring = MySearchKeyRing(self.config)
                client = MySearchClient(config=self.config, keyring=ring)
                probe = Mock(
                    side_effect=[
                        MySearchHTTPError(
                            provider="tavily",
                            status_code=status_code,
                            detail=detail,
                            url="https://example.com/search",
                            retry_after_seconds=retry_after_seconds,
                        ),
                        None,
                    ]
                )
                client._probe_provider_request = probe  # type: ignore[method-assign]

                self.assertFalse(client._provider_can_serve(self.config.tavily))
                ring.reload()
                self.assertTrue(client._provider_can_serve(self.config.tavily))
                self.assertEqual(probe.call_count, 2)

    def test_direct_compatible_xai_rotates_real_keys(self) -> None:
        env = {
            "MYSEARCH_XAI_SEARCH_MODE": "compatible",
            "MYSEARCH_XAI_SOCIAL_BASE_URL": "https://social.example/v1",
            "MYSEARCH_XAI_API_KEYS": "xai-first,xai-second",
        }
        with patch.dict(os.environ, env, clear=True):
            config = MySearchConfig.from_env()

        self.assertFalse(config.xai.managed_key_pool)
        ring = MySearchKeyRing(config)
        client = MySearchClient(config=config, keyring=ring)
        calls: list[str] = []

        def request_once(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs["key"])
            if kwargs["key"] == "xai-first":
                raise MySearchHTTPError(
                    provider="xai",
                    status_code=429,
                    detail="rate limit exceeded",
                    url="https://social.example/v1/responses",
                    retry_after_seconds=30,
                )
            return {"ok": True}

        client._request_json_once = request_once  # type: ignore[method-assign]
        self.assertEqual(
            client._request_json(
                provider=config.xai,
                method="POST",
                path="/responses",
                payload={},
                key="xai-first",
            ),
            {"ok": True},
        )
        self.assertEqual(calls, ["xai-first", "xai-second"])

    def test_proxy_provider_without_proxy_token_fails_closed(self) -> None:
        env = {
            "MYSEARCH_PROXY_BASE_URL": "http://proxy.example",
            "MYSEARCH_FIRECRAWL_API_KEY": "fc-direct-must-not-be-proxy-token",
            "MYSEARCH_EXA_API_KEY": "exa-direct-must-not-be-proxy-token",
            "MYSEARCH_XAI_API_KEY": "xai-direct-must-not-be-proxy-token",
        }
        with patch.dict(os.environ, env, clear=True):
            config = MySearchConfig.from_env()

        for provider in (config.firecrawl, config.exa, config.xai):
            with self.subTest(provider=provider.name):
                self.assertEqual(provider.api_keys, [])
                self.assertTrue(provider.managed_key_pool)

    def test_firecrawl_crawl_polling_stays_on_job_creation_key(self) -> None:
        config = _minimal_config(["tavily-key"])
        config.firecrawl.api_keys = ["firecrawl-first", "firecrawl-second"]
        ring = MySearchKeyRing(config)
        client = MySearchClient(config=config, keyring=ring)
        calls: list[tuple[str, str]] = []

        def request_once(**kwargs):  # type: ignore[no-untyped-def]
            method = str(kwargs["method"]).upper()
            selected_key = kwargs["key"]
            calls.append((method, selected_key))
            if method == "POST" and selected_key == "firecrawl-first":
                raise MySearchHTTPError(
                    provider="firecrawl",
                    status_code=429,
                    detail="rate limit exceeded",
                    url="https://example.com/v2/crawl",
                    retry_after_seconds=30,
                )
            if method == "POST":
                return {"id": "job-1"}
            return {"status": "completed", "data": []}

        client._request_json_once = request_once  # type: ignore[method-assign]
        result = client._crawl_firecrawl(
            url="https://example.com/docs",
            poll_interval_seconds=0,
            max_poll_attempts=1,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            calls,
            [
                ("POST", "firecrawl-first"),
                ("POST", "firecrawl-second"),
                ("GET", "firecrawl-second"),
            ],
        )

    def test_firecrawl_crawl_polling_retries_429_on_the_same_key(self) -> None:
        config = _minimal_config(["tavily-key"])
        config.firecrawl.api_keys = ["firecrawl-only"]
        ring = MySearchKeyRing(config)
        client = MySearchClient(config=config, keyring=ring)
        calls: list[tuple[str, str]] = []

        def request_once(**kwargs):  # type: ignore[no-untyped-def]
            method = str(kwargs["method"]).upper()
            selected_key = kwargs["key"]
            calls.append((method, selected_key))
            if method == "POST":
                return {"id": "job-1"}
            if len([call for call in calls if call[0] == "GET"]) == 1:
                raise MySearchHTTPError(
                    provider="firecrawl",
                    status_code=429,
                    detail="rate limit exceeded",
                    url="https://example.com/v2/crawl/job-1",
                )
            return {"status": "completed", "data": []}

        client._request_json_once = request_once  # type: ignore[method-assign]
        with patch("mysearch.clients.time.sleep") as sleep:
            result = client._crawl_firecrawl(
                url="https://example.com/docs",
                poll_interval_seconds=0,
                max_poll_attempts=1,
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            calls,
            [
                ("POST", "firecrawl-only"),
                ("GET", "firecrawl-only"),
                ("GET", "firecrawl-only"),
            ],
        )
        sleep.assert_any_call(60)

    def test_firecrawl_crawl_polling_waits_past_http_timeout_on_the_same_key(self) -> None:
        config = _minimal_config(["tavily-key"])
        config.firecrawl.api_keys = ["firecrawl-only"]
        ring = MySearchKeyRing(config)
        client = MySearchClient(config=config, keyring=ring)
        calls: list[tuple[str, str]] = []

        def request_once(**kwargs):  # type: ignore[no-untyped-def]
            method = str(kwargs["method"]).upper()
            selected_key = kwargs["key"]
            calls.append((method, selected_key))
            if method == "POST":
                return {"id": "job-1"}
            if len([call for call in calls if call[0] == "GET"]) == 1:
                raise MySearchHTTPError(
                    provider="firecrawl",
                    status_code=429,
                    detail="rate limit exceeded",
                    url="https://example.com/v2/crawl/job-1",
                    retry_after_seconds=60,
                )
            return {"status": "completed", "data": []}

        client._request_json_once = request_once  # type: ignore[method-assign]
        with patch("mysearch.clients.time.sleep") as sleep:
            result = client._crawl_firecrawl(
                url="https://example.com/docs",
                poll_interval_seconds=0,
                max_poll_attempts=1,
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(calls[-2:], [("GET", "firecrawl-only"), ("GET", "firecrawl-only")])
        sleep.assert_any_call(60)

    def test_pinned_key_retry_refuses_unbounded_retry_after(self) -> None:
        client = MySearchClient(config=self.config, keyring=self.ring)
        client._request_json_once = Mock(  # type: ignore[method-assign]
            side_effect=MySearchHTTPError(
                provider="firecrawl",
                status_code=429,
                detail="rate limit exceeded",
                url="https://example.com/v2/crawl/job-1",
                retry_after_seconds=121,
            )
        )

        with patch("mysearch.clients.time.sleep") as sleep, self.assertRaises(MySearchHTTPError):
            client._request_json_with_transient_retry_selected(
                provider=self.config.firecrawl,
                method="GET",
                path="/v2/crawl/job-1",
                payload=None,
                key="firecrawl-key",
                allow_key_rotation=False,
            )

        sleep.assert_not_called()

    def test_managed_firecrawl_429_is_not_retried_locally(self) -> None:
        config = _minimal_config(["tavily-key"])
        config.firecrawl.managed_key_pool = True
        ring = MySearchKeyRing(config)
        client = MySearchClient(config=config, keyring=ring)

        for operation in (
            lambda: client._map_firecrawl(url="https://example.com"),
            lambda: client._crawl_firecrawl(
                url="https://example.com",
                max_poll_attempts=1,
            ),
        ):
            with self.subTest(operation=operation):
                request_once = Mock(
                    side_effect=MySearchHTTPError(
                        provider="firecrawl",
                        status_code=429,
                        detail="rate limit exceeded",
                        url="https://proxy.example/firecrawl",
                        retry_after_seconds=30,
                    )
                )
                client._request_json_once = request_once  # type: ignore[method-assign]
                with patch("mysearch.clients.time.sleep") as sleep, self.assertRaises(
                    MySearchHTTPError
                ):
                    operation()
                self.assertEqual(request_once.call_count, 1)
                sleep.assert_not_called()


class ProxyDatabaseSchedulingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "proxy.db"
        self.env_patch = patch.dict(
            os.environ,
            {"MYSEARCH_PROXY_DB_PATH": str(self.db_path)},
        )
        self.env_patch.start()
        self.db = _load_module(
            f"test_provider_key_database_{id(self)}",
            PROXY_ROOT / "database.py",
        )
        self.db.init_db()

    def tearDown(self) -> None:
        self.db.close_conn()
        self.env_patch.stop()
        self.tempdir.cleanup()

    def _add_key(self, value: str = "tvly-test-key") -> int:
        row = self.db.add_key(value, service="tavily")
        return int(row["id"])

    def test_rate_limit_cools_down_without_permanent_disable(self) -> None:
        key_id = self._add_key()
        self.db.update_key_usage(
            key_id,
            False,
            failure_kind="rate_limited",
            failure_detail="too many requests",
            retry_after_seconds=30,
        )
        row = dict(self.db.get_key_by_id(key_id))
        self.assertEqual(row["active"], 1)
        self.assertEqual(row["disabled_reason"], "rate_limited")
        self.assertEqual(self.db.get_active_keys("tavily"), [])

        conn = self.db.get_conn()
        conn.execute(
            "UPDATE api_keys SET schedule_until = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (key_id,),
        )
        conn.commit()
        self.assertEqual(len(self.db.get_active_keys("tavily")), 1)

    def test_quota_exhaustion_requires_manual_enable(self) -> None:
        key_id = self._add_key()
        self.db.update_key_usage(
            key_id,
            False,
            failure_kind="quota_exhausted",
            failure_detail="credits exhausted",
        )
        row = dict(self.db.get_key_by_id(key_id))
        self.assertEqual(row["active"], 0)
        self.assertEqual(row["disabled_reason"], "quota_exhausted")

        self.db.toggle_key(key_id, 1)
        restored = dict(self.db.get_key_by_id(key_id))
        self.assertEqual(restored["active"], 1)
        self.assertEqual(restored["disabled_reason"], "")

    def test_neutral_failures_never_disable_credential(self) -> None:
        key_id = self._add_key()
        for _ in range(5):
            self.db.update_key_usage(key_id, False)
        row = dict(self.db.get_key_by_id(key_id))
        self.assertEqual(row["active"], 1)
        self.assertEqual(row["consecutive_fails"], 5)
        self.assertEqual(row["disabled_reason"], "")

    def test_usage_sync_with_zero_remaining_quarantines_key(self) -> None:
        key_id = self._add_key()
        self.db.update_key_remote_usage(
            key_id,
            key_used=100,
            key_limit=100,
            key_remaining=0,
        )
        row = dict(self.db.get_key_by_id(key_id))
        self.assertEqual(row["active"], 0)
        self.assertEqual(row["disabled_reason"], "quota_exhausted")

    def test_account_quota_zero_also_quarantines_key(self) -> None:
        key_id = self._add_key()
        self.db.update_key_remote_usage(
            key_id,
            key_used=10,
            key_limit=100,
            key_remaining=90,
            account_used=100,
            account_limit=100,
            account_remaining=0,
        )
        row = dict(self.db.get_key_by_id(key_id))
        self.assertEqual(row["active"], 0)
        self.assertEqual(row["disabled_reason"], "quota_exhausted")

    def test_usage_sync_error_redacts_the_persisted_key(self) -> None:
        raw_key = "tvly-secret-value"
        key_id = self._add_key(raw_key)

        self.db.update_key_remote_usage_error(
            key_id,
            f"invalid api key: {raw_key}",
        )

        row = dict(self.db.get_key_by_id(key_id))
        self.assertNotIn(raw_key, row["usage_sync_error"])
        self.assertIn("<redacted>", row["usage_sync_error"])

    def test_next_schedule_delay_tracks_the_earliest_cooled_key(self) -> None:
        first_id = self._add_key("tvly-first-key")
        second_id = self._add_key("tvly-second-key")
        self.db.update_key_usage(
            first_id,
            False,
            failure_kind="rate_limited",
            retry_after_seconds=30,
        )
        self.db.update_key_usage(
            second_id,
            False,
            failure_kind="rate_limited",
            retry_after_seconds=60,
        )
        delay = self.db.get_next_key_schedule_delay("tavily")
        self.assertIsNotNone(delay)
        self.assertGreater(delay, 0)
        self.assertLessEqual(delay, 30)

    def test_late_success_does_not_clear_a_future_cooldown(self) -> None:
        key_id = self._add_key()
        self.db.update_key_usage(
            key_id,
            False,
            failure_kind="rate_limited",
            retry_after_seconds=120,
        )
        initial = dict(self.db.get_key_by_id(key_id))

        self.db.update_key_usage(key_id, True)
        after_success = dict(self.db.get_key_by_id(key_id))
        self.assertEqual(after_success["schedule_until"], initial["schedule_until"])
        self.assertEqual(after_success["disabled_reason"], "rate_limited")

        self.db.update_key_usage(
            key_id,
            False,
            failure_kind="rate_limited",
            retry_after_seconds=30,
        )
        after_shorter_limit = dict(self.db.get_key_by_id(key_id))
        self.assertEqual(after_shorter_limit["schedule_until"], initial["schedule_until"])

    def test_late_rate_limit_does_not_reactivate_terminal_key(self) -> None:
        key_id = self._add_key()
        self.db.update_key_usage(
            key_id,
            False,
            failure_kind="quota_exhausted",
        )
        self.db.update_key_usage(
            key_id,
            False,
            failure_kind="rate_limited",
            retry_after_seconds=30,
        )
        row = dict(self.db.get_key_by_id(key_id))
        self.assertEqual(row["active"], 0)
        self.assertEqual(row["disabled_reason"], "quota_exhausted")
        self.assertIsNone(row["schedule_until"])


class ProxyPoolCooldownTests(unittest.TestCase):
    def test_cooled_key_rejoins_pool_while_other_keys_remain(self) -> None:
        key_pool = proxy_key_pool.ServiceKeyPool()
        keys = [
            {"id": 1, "key": "first-key"},
            {"id": 2, "key": "second-key"},
        ]
        now = [100.0]

        def active_keys(_service):
            return keys if now[0] >= 130.0 else [keys[1]]

        with patch.object(proxy_key_pool, "get_active_keys", side_effect=active_keys) as get_active, patch.object(
            proxy_key_pool,
            "update_key_usage",
        ), patch.object(
            proxy_key_pool,
            "get_next_key_schedule_delay",
            return_value=None,
        ), patch.object(proxy_key_pool.time, "monotonic", side_effect=lambda: now[0]):
            key_pool.reload("tavily")
            key_pool.report_result(
                "tavily",
                1,
                False,
                failure_kind="rate_limited",
                retry_after_seconds=30,
            )
            now[0] = 120.0
            self.assertEqual(key_pool.get_next_key("tavily")["id"], 2)
            self.assertEqual(get_active.call_count, 1)

            now[0] = 131.0
            selected_ids = {
                key_pool.get_next_key("tavily")["id"],
                key_pool.get_next_key("tavily")["id"],
            }
            self.assertEqual(selected_ids, {1, 2})
            self.assertEqual(get_active.call_count, 2)

    def test_proxy_bare_403_is_not_a_key_failure(self) -> None:
        self.assertEqual(
            proxy_key_pool.classify_upstream_key_failure(
                403,
                "forbidden by upstream policy",
            ),
            "",
        )
        self.assertEqual(
            proxy_key_pool.classify_upstream_key_failure(
                403,
                "unauthorized for this endpoint; additional scope required",
            ),
            "",
        )

    def test_proxy_quota_words_require_a_quota_status(self) -> None:
        for status_code in (200, 400, 500):
            with self.subTest(status_code=status_code):
                self.assertEqual(
                    proxy_key_pool.classify_upstream_key_failure(
                        status_code,
                        "documentation for the usage limit field",
                    ),
                    "",
                )

    def test_proxy_plural_credits_limit_is_terminal(self) -> None:
        self.assertEqual(
            proxy_key_pool.classify_upstream_key_failure(
                429,
                "You have exceeded your credits limit.",
            ),
            "quota_exhausted",
        )

class ProxyForwardingSchedulingTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _load_module(
            "test_provider_key_proxy_server",
            PROXY_ROOT / "server.py",
        )

    def setUp(self) -> None:
        self.setting_patch = patch.object(self.server.db, "set_setting")
        self.set_setting = self.setting_patch.start()

    def tearDown(self) -> None:
        self.server.social_upstream_key_schedule.clear()
        self.server.social_upstream_key_cursor = 0
        self.setting_patch.stop()

    async def test_exa_429_retries_next_unique_key(self) -> None:
        class Request:
            method = "POST"
            headers = {"authorization": "Bearer exat-client"}
            query_params = {}

            async def body(self):
                return b'{"query":"test"}'

        keys = [
            {"id": 1, "key": "exa-first"},
            {"id": 2, "key": "exa-second"},
        ]
        responses = [
            httpx.Response(
                429,
                json={"error": "rate limit exceeded"},
                headers={"retry-after": "20"},
            ),
            httpx.Response(200, json={"results": []}),
        ]
        selected_headers: list[str] = []

        async def post(*args, **kwargs):  # type: ignore[no-untyped-def]
            selected_headers.append(kwargs["headers"]["x-api-key"])
            return responses.pop(0)

        def next_key(_service, *, exclude_ids=None):  # type: ignore[no-untyped-def]
            excluded = set(exclude_ids or ())
            return next((key for key in keys if key["id"] not in excluded), None)

        with patch.object(self.server, "get_token_row_or_401", return_value={"id": 9}), patch.object(
            self.server.pool,
            "get_next_key",
            side_effect=next_key,
        ), patch.object(self.server.pool, "report_result") as report_result, patch.object(
            self.server.db,
            "log_usage",
        ), patch.object(self.server.http_client, "post", new=AsyncMock(side_effect=post)):
            response = await self.server.proxy_exa_search(Request())

        self.assertEqual(json.loads(response.body), {"results": []})
        self.assertEqual(selected_headers, ["exa-first", "exa-second"])
        self.assertEqual(report_result.call_args_list[0].kwargs["failure_kind"], "rate_limited")
        self.assertEqual(report_result.call_args_list[0].kwargs["retry_after_seconds"], 20)
        self.assertEqual(report_result.call_args_list[1].kwargs["failure_kind"], "")

    async def test_firecrawl_crawl_status_checks_other_keys_without_quarantining_404(self) -> None:
        class Request:
            method = "GET"
            headers = {"authorization": "Bearer fctk-client"}
            query_params = {}

            async def body(self):
                return b""

        keys = [
            {"id": 1, "key": "firecrawl-first"},
            {"id": 2, "key": "firecrawl-second"},
        ]
        responses = [
            httpx.Response(404, json={"error": "job not found"}),
            httpx.Response(200, json={"status": "completed", "data": []}),
        ]
        selected_headers: list[str] = []

        async def request(*args, **kwargs):  # type: ignore[no-untyped-def]
            selected_headers.append(kwargs["headers"]["Authorization"])
            return responses.pop(0)

        def next_key(_service, *, exclude_ids=None):  # type: ignore[no-untyped-def]
            excluded = set(exclude_ids or ())
            return next((key for key in keys if key["id"] not in excluded), None)

        with patch.object(self.server, "get_token_row_or_401", return_value={"id": 9}), patch.object(
            self.server.pool,
            "get_next_key",
            side_effect=next_key,
        ), patch.object(self.server.pool, "report_result") as report_result, patch.object(
            self.server.db,
            "log_usage",
        ), patch.object(self.server.http_client, "request", new=AsyncMock(side_effect=request)):
            response = await self.server.proxy_firecrawl("v2/crawl/job-1", Request())

        self.assertEqual(json.loads(response.body), {"status": "completed", "data": []})
        self.assertEqual(
            selected_headers,
            ["Bearer firecrawl-first", "Bearer firecrawl-second"],
        )
        self.assertEqual(report_result.call_args_list[0].kwargs["failure_kind"], "")

    async def test_firecrawl_crawl_status_preserves_429_over_other_account_404(self) -> None:
        class Request:
            method = "GET"
            headers = {"authorization": "Bearer fctk-client"}
            query_params = {}

            async def body(self):
                return b""

        keys = [
            {"id": 1, "key": "firecrawl-job-key"},
            {"id": 2, "key": "firecrawl-other-key"},
        ]
        responses = [
            httpx.Response(
                429,
                json={"error": "rate limit exceeded"},
                headers={"retry-after": "20"},
            ),
            httpx.Response(404, json={"error": "job not found"}),
        ]

        def next_key(_service, *, exclude_ids=None):  # type: ignore[no-untyped-def]
            excluded = set(exclude_ids or ())
            return next((key for key in keys if key["id"] not in excluded), None)

        with patch.object(self.server, "get_token_row_or_401", return_value={"id": 9}), patch.object(
            self.server.pool,
            "get_next_key",
            side_effect=next_key,
        ), patch.object(self.server.pool, "report_result"), patch.object(
            self.server.db,
            "log_usage",
        ), patch.object(
            self.server.http_client,
            "request",
            new=AsyncMock(side_effect=responses),
        ):
            response = await self.server.proxy_firecrawl("v2/crawl/job-1", Request())

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers.get("retry-after"), "20")

    async def test_mixed_rate_and_terminal_failures_prefer_recoverable_key(self) -> None:
        class Request:
            method = "POST"
            headers = {"authorization": "Bearer exat-client"}
            query_params = {}

            async def body(self):
                return b'{"query":"test"}'

        keys = [
            {"id": 1, "key": "exa-first"},
            {"id": 2, "key": "exa-second"},
        ]

        for responses in (
            [
                httpx.Response(429, json={"error": "rate limit exceeded"}, headers={"retry-after": "15"}),
                httpx.Response(401, json={"error": "invalid api key"}),
            ],
            [
                httpx.Response(401, json={"error": "invalid api key"}),
                httpx.Response(429, json={"error": "rate limit exceeded"}, headers={"retry-after": "15"}),
            ],
        ):
            with self.subTest(order=[response.status_code for response in responses]):
                def next_key(_service, *, exclude_ids=None):  # type: ignore[no-untyped-def]
                    excluded = set(exclude_ids or ())
                    return next((key for key in keys if key["id"] not in excluded), None)

                with patch.object(self.server, "get_token_row_or_401", return_value={"id": 9}), patch.object(
                    self.server.pool,
                    "get_next_key",
                    side_effect=next_key,
                ), patch.object(self.server.pool, "report_result"), patch.object(
                    self.server.db,
                    "log_usage",
                ), patch.object(
                    self.server.http_client,
                    "post",
                    new=AsyncMock(side_effect=list(responses)),
                ):
                    response = await self.server.proxy_exa_search(Request())

                self.assertEqual(response.status_code, 429)
                self.assertEqual(response.headers.get("retry-after"), "15")

    async def test_exa_key_failure_response_redacts_upstream_keys(self) -> None:
        class Request:
            method = "POST"
            headers = {"authorization": "Bearer exat-client"}
            query_params = {}

            async def body(self):
                return b'{"query":"test"}'

        keys = [
            {"id": 1, "key": "exa-secret-first"},
            {"id": 2, "key": "exa-secret-second"},
        ]
        selected_keys: list[str] = []

        async def post(*args, **kwargs):  # type: ignore[no-untyped-def]
            selected_key = kwargs["headers"]["x-api-key"]
            selected_keys.append(selected_key)
            return httpx.Response(
                401,
                json={"error": f"invalid api key: {selected_key}"},
            )

        def next_key(_service, *, exclude_ids=None):  # type: ignore[no-untyped-def]
            excluded = set(exclude_ids or ())
            return next((key for key in keys if key["id"] not in excluded), None)

        with patch.object(self.server, "get_token_row_or_401", return_value={"id": 9}), patch.object(
            self.server.pool,
            "get_next_key",
            side_effect=next_key,
        ), patch.object(self.server.pool, "report_result") as report_result, patch.object(
            self.server.db,
            "log_usage",
        ), patch.object(self.server.http_client, "post", new=AsyncMock(side_effect=post)):
            response = await self.server.proxy_exa_search(Request())

        response_text = response.body.decode("utf-8")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(selected_keys, ["exa-secret-first", "exa-secret-second"])
        self.assertNotIn("exa-secret-first", response_text)
        self.assertNotIn("exa-secret-second", response_text)
        for call in report_result.call_args_list:
            self.assertNotIn("exa-secret", call.kwargs["failure_detail"])

    def test_provider_error_redacts_before_truncating_and_keeps_structured_code(self) -> None:
        raw_key = "super-secret-provider-key-abcdef"
        response = httpx.Response(
            403,
            json={
                "error": f"{'x' * 490}{raw_key}",
                "code": "invalid_api_key",
            },
        )

        failure_kind, detail, _retry_after = self.server._upstream_key_failure(
            response,
            raw_key,
        )

        self.assertEqual(failure_kind, "auth_rejected")
        self.assertNotIn(raw_key, detail)
        self.assertNotIn(raw_key[:12], detail)

    def test_social_error_redacts_before_truncating(self) -> None:
        raw_key = "super-secret-social-key-abcdef"
        detail = self.server.extract_social_upstream_error(
            {"error": {"message": f"{'x' * 290}{raw_key}"}},
            "",
            raw_key,
        )

        self.assertNotIn(raw_key, detail)
        self.assertNotIn(raw_key[:12], detail)

    async def test_social_429_uses_second_upstream_key(self) -> None:
        self.server.social_upstream_key_schedule.clear()
        self.server.social_upstream_key_cursor = 0
        responses = [
            httpx.Response(
                429,
                json={"error": {"message": "rate limit exceeded"}},
                headers={"retry-after": "15"},
            ),
            httpx.Response(
                200,
                json={"output_text": '{"answer":"ok","results":[]}'},
            ),
        ]
        selected_headers: list[str] = []

        async def post(*args, **kwargs):  # type: ignore[no-untyped-def]
            selected_headers.append(kwargs["headers"]["Authorization"])
            return responses.pop(0)

        state = {
            "upstream_base_url": "http://social.example/v1",
            "upstream_responses_path": "/responses",
            "upstream_api_keys": ["social-first", "social-second"],
            "resolved_upstream_api_key": "social-first",
        }
        with patch.object(self.server.http_client, "post", new=AsyncMock(side_effect=post)):
            result = await self.server.execute_social_search_attempt(
                "test",
                {},
                state,
                "grok-test",
                5,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            selected_headers,
            ["Bearer social-first", "Bearer social-second"],
        )
        self.assertEqual(len(self.server.social_upstream_key_schedule), 1)

    async def test_social_all_keys_rate_limited_skips_model_fallback(self) -> None:
        class Request:
            headers = {"Authorization": "Bearer client-token"}

            async def json(self):
                return {"query": "test", "source": "x", "max_results": 5}

        state = {
            "upstream_base_url": "http://social.example/v1",
            "upstream_responses_path": "/responses",
            "upstream_api_keys": ["social-first", "social-second"],
            "resolved_upstream_api_key": "social-first",
            "accepted_tokens": ["client-token"],
            "model": "grok-primary",
            "fallback_model": "grok-fallback",
            "fallback_min_results": 3,
        }
        response = httpx.Response(
            429,
            json={"error": {"message": "rate limit exceeded"}},
            headers={"retry-after": "15"},
        )

        with patch.object(
            self.server,
            "resolve_social_gateway_state",
            new=AsyncMock(return_value=state),
        ), patch.object(self.server.db, "log_usage"), patch.object(
            self.server.http_client,
            "post",
            new=AsyncMock(return_value=response),
        ) as post:
            with self.assertRaises(self.server.HTTPException) as raised:
                await self.server.proxy_social_search(Request())

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.headers.get("Retry-After"), "15")
        self.assertEqual(post.await_count, 2)

    async def test_social_healthy_keys_rotate_between_requests(self) -> None:
        self.server.social_upstream_key_schedule.clear()
        self.server.social_upstream_key_cursor = 0
        selected_headers: list[str] = []

        async def post(*args, **kwargs):  # type: ignore[no-untyped-def]
            selected_headers.append(kwargs["headers"]["Authorization"])
            return httpx.Response(
                200,
                json={"output_text": '{"answer":"ok","results":[]}'},
            )

        state = {
            "upstream_base_url": "http://social.example/v1",
            "upstream_responses_path": "/responses",
            "upstream_api_keys": ["social-first", "social-second"],
            "resolved_upstream_api_key": "social-first",
        }
        with patch.object(self.server.http_client, "post", new=AsyncMock(side_effect=post)):
            for _ in range(2):
                result = await self.server.execute_social_search_attempt(
                    "test",
                    {},
                    state,
                    "grok-test",
                    5,
                )
                self.assertTrue(result["ok"])

        self.assertEqual(
            selected_headers,
            ["Bearer social-first", "Bearer social-second"],
        )

    async def test_social_terminal_pool_failure_is_reported_as_gateway_unavailable(self) -> None:
        state = {
            "upstream_base_url": "http://social.example/v1",
            "upstream_responses_path": "/responses",
            "upstream_api_keys": ["social-first", "social-second"],
            "resolved_upstream_api_key": "social-first",
        }

        async def post(*args, **kwargs):  # type: ignore[no-untyped-def]
            selected_key = kwargs["headers"]["Authorization"].removeprefix("Bearer ")
            return httpx.Response(
                401,
                json={"error": {"message": f"invalid api key: {selected_key}"}},
            )

        with patch.object(self.server.http_client, "post", new=AsyncMock(side_effect=post)):
            result = await self.server.execute_social_search_attempt(
                "test",
                {},
                state,
                "grok-test",
                5,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 503)
        self.assertNotIn("social-first", result["error"])
        self.assertNotIn("social-second", result["error"])

    async def test_social_quarantined_key_can_be_resumed_by_stable_id(self) -> None:
        raw_key = "social-secret-key"
        self.server._schedule_social_upstream_key(raw_key, "quota_exhausted")
        key_id = self.server._social_key_fingerprint(raw_key)[:12]

        resumed = self.server._resume_social_upstream_key(key_id, [raw_key])

        self.assertTrue(resumed)
        self.assertEqual(self.server._available_social_upstream_keys([raw_key]), [raw_key])
        self.assertEqual(self.server.db.set_setting.call_args.args[1], "{}")

    def test_proxy_retry_after_accepts_http_date(self) -> None:
        retry_at = datetime.now(timezone.utc) + timedelta(seconds=30)
        parsed = self.server._parse_retry_after_header(
            {"retry-after": format_datetime(retry_at, usegmt=True)}
        )

        self.assertIsNotNone(parsed)
        self.assertGreaterEqual(parsed, 1)
        self.assertLessEqual(parsed, 30)

    async def test_social_resume_api_restores_quarantined_key(self) -> None:
        raw_key = "social-secret-key"
        self.server._schedule_social_upstream_key(raw_key, "quota_exhausted")
        key_id = self.server._social_key_fingerprint(raw_key)[:12]
        state = {"upstream_api_keys": [raw_key]}

        with patch.object(
            self.server,
            "resolve_social_gateway_state",
            new=AsyncMock(return_value=state),
        ), patch.object(
            self.server,
            "build_settings_payload",
            new=AsyncMock(return_value={"social": {}}),
        ):
            result = await self.server.resume_social_upstream_key(key_id)

        self.assertTrue(result["resumed"])
        self.assertEqual(self.server._available_social_upstream_keys([raw_key]), [raw_key])

    async def test_social_key_pool_replace_clears_quarantine_schedule(self) -> None:
        class Request:
            async def json(self):
                return {"upstream_api_key": "replacement-key"}

        self.server._schedule_social_upstream_key("old-key", "quota_exhausted")
        self.set_setting.reset_mock()
        with patch.object(
            self.server,
            "build_settings_payload",
            new=AsyncMock(return_value={"social": {}}),
        ):
            await self.server.update_social_settings(Request())

        self.assertEqual(self.server.social_upstream_key_schedule, {})
        self.assertIn(
            (("social_upstream_api_key", "replacement-key"), {}),
            [(call.args, call.kwargs) for call in self.set_setting.call_args_list],
        )
        self.assertIn(
            (("social_upstream_key_schedule", "{}"), {}),
            [(call.args, call.kwargs) for call in self.set_setting.call_args_list],
        )

    async def test_social_key_pool_clear_removes_value_and_schedule(self) -> None:
        class Request:
            async def json(self):
                return {"clear_upstream_api_key": True}

        self.server._schedule_social_upstream_key("old-key", "rate_limited", 30)
        self.set_setting.reset_mock()
        with patch.object(
            self.server,
            "build_settings_payload",
            new=AsyncMock(return_value={"social": {}}),
        ):
            await self.server.update_social_settings(Request())

        self.assertEqual(self.server.social_upstream_key_schedule, {})
        self.assertIn(
            (("social_upstream_api_key", ""), {}),
            [(call.args, call.kwargs) for call in self.set_setting.call_args_list],
        )
        self.assertIn(
            (("social_upstream_key_schedule", "{}"), {}),
            [(call.args, call.kwargs) for call in self.set_setting.call_args_list],
        )

    async def test_gateway_pool_only_hides_explicit_terminal_key_failures(self) -> None:
        terminal = self.server._safe_upstream_error_response(
            httpx.Response(401, json={"error": "invalid api key"}),
            "social-secret",
            gateway_pool_exhausted=True,
        )
        policy_denial = self.server._safe_upstream_error_response(
            httpx.Response(403, json={"error": "forbidden by endpoint policy"}),
            "social-secret",
            gateway_pool_exhausted=True,
        )

        self.assertEqual(terminal.status_code, 503)
        self.assertEqual(policy_denial.status_code, 403)

    async def test_usage_sync_error_redacts_key_before_return_and_persist(self) -> None:
        raw_key = "tvly-secret-value"
        update_error = Mock()
        with patch.object(
            self.server,
            "fetch_remote_usage_tavily",
            new=AsyncMock(
                side_effect=self.server.HTTPException(
                    status_code=401,
                    detail=f"invalid api key: {raw_key}",
                )
            ),
        ), patch.object(self.server.db, "update_key_remote_usage_error", update_error):
            result = await self.server.sync_usage_for_key_row(
                {"id": 7, "service": "tavily", "key": raw_key}
            )

        self.assertNotIn(raw_key, result["detail"])
        self.assertNotIn(raw_key, update_error.call_args.args[1])

    async def test_firecrawl_account_quota_uses_official_field_and_deduplicates_unknown_account(self) -> None:
        normalized = self.server.normalize_usage_payload(
            "firecrawl",
            {
                "current": {
                    "data": {
                        "remainingCredits": 75,
                        "planCredits": 100,
                    }
                },
                "historical": {
                    "periods": [
                        {
                            "startDate": "2026-07-01T00:00:00Z",
                            "endDate": "2026-07-31T23:59:59Z",
                            "apiKey": "primary",
                            "totalCredits": 25,
                        }
                    ]
                },
            },
        )
        self.assertEqual(normalized["account_used"], 25)
        self.assertEqual(normalized["account_limit"], 100)

        row = {
            "service": "firecrawl",
            "email": "",
            "usage_key_used": None,
            "usage_account_used": 25,
            "usage_account_limit": 100,
            "usage_account_remaining": 75,
            "usage_synced_at": "2026-07-18T00:00:00+00:00",
            "usage_sync_error": "",
        }
        summary = self.server.build_real_quota_summary(
            [{"id": 1, **row}, {"id": 2, **row}]
        )
        self.assertEqual(summary["total_limit"], 100)
        self.assertEqual(summary["total_used"], 25)
        self.assertEqual(summary["total_remaining"], 75)
        self.assertTrue(summary["account_identity_ambiguous"])

    async def test_social_terminal_schedule_survives_reload_without_exposing_key(self) -> None:
        raw_key = "social-secret-key"
        self.server._schedule_social_upstream_key(raw_key, "quota_exhausted")
        persisted = self.server.db.set_setting.call_args.args[1]

        self.server.social_upstream_key_schedule.clear()
        with patch.object(self.server.db, "get_setting", return_value=persisted):
            self.server._load_social_upstream_key_schedule()

        self.assertEqual(self.server._available_social_upstream_keys([raw_key]), [])
        statuses = self.server._social_upstream_key_statuses([raw_key])
        self.assertEqual(statuses[0]["disabled_reason"], "quota_exhausted")
        self.assertFalse(statuses[0]["schedulable"])
        self.assertNotIn(raw_key, json.dumps(statuses))

    async def test_social_health_is_down_when_all_keys_are_quarantined(self) -> None:
        self.server.social_upstream_key_schedule.clear()
        self.server._schedule_social_upstream_key("social-only", "quota_exhausted")
        state = {
            "resolved_upstream_api_key": "social-only",
            "upstream_api_keys": ["social-only"],
            "accepted_tokens": ["client-token"],
            "mode": "manual",
            "upstream_base_url": "http://social.example/v1",
            "upstream_responses_path": "/responses",
            "admin_base_url": "",
            "model": "grok-test",
            "fallback_model": "",
            "fallback_min_results": 1,
            "token_source": "manual",
            "admin_configured": False,
            "admin_connected": False,
            "admin_auth_mode": "",
            "admin_api_version": "",
            "stats": {},
            "error": "",
        }
        payload = self.server._build_social_health_payload(state)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["upstream_available_key_count"], 0)
        self.assertEqual(payload["upstream_unavailable_key_count"], 1)

    def test_public_social_health_hides_admin_error_details(self) -> None:
        leaked_secret = "g2a-admin-access-token-secret"
        state = {
            "resolved_upstream_api_key": "social-only",
            "upstream_api_keys": ["social-only"],
            "accepted_tokens": ["client-token"],
            "mode": "manual",
            "upstream_base_url": "http://social.example/v1",
            "upstream_responses_path": "/responses",
            "admin_base_url": "http://social.example",
            "model": "grok-test",
            "fallback_model": "",
            "fallback_min_results": 1,
            "token_source": "manual",
            "admin_configured": True,
            "admin_connected": False,
            "admin_auth_mode": "v3_credentials",
            "admin_api_version": "v3",
            "stats": {},
            "error": f"upstream returned access token {leaked_secret}",
        }

        payload = self.server._build_social_health_payload(state)

        self.assertNotIn(leaked_secret, payload["error"])
        self.assertEqual(payload["error"], "Social gateway configuration requires attention")


class SoftwareVersionRegressionTests(unittest.TestCase):
    def test_nested_official_evidence_overrides_truncated_stale_results(self) -> None:
        client = MySearchClient()
        result = {
            "answer": "The latest stable version of Python is 3.14.3.",
            "results": [
                {
                    "title": "Latest Python version",
                    "url": "https://example.com/python",
                    "snippet": "The latest stable version of Python is 3.14.3.",
                }
            ],
            "primary_search": {
                "results": [
                    {
                        "title": "Download Python",
                        "url": "https://www.python.org/downloads/",
                        "snippet": "Download the latest version of Python. Download Python 3.14.6.",
                    }
                ]
            },
            "evidence": {},
        }

        updated = client._apply_software_version_answer_override(
            query="what is the latest stable version of Python",
            mode="web",
            intent="factual",
            result=result,
        )
        self.assertEqual(
            updated["answer"],
            "The latest stable version of Python is 3.14.6.",
        )


if __name__ == "__main__":
    unittest.main()
