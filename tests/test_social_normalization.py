from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mysearch import social_gateway

PROXY_DIR = REPO_ROOT / "proxy"
if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))


def _load_proxy_server_module():
    spec = importlib.util.spec_from_file_location(
        "test_proxy_server_module",
        PROXY_DIR / "server.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


proxy_server = _load_proxy_server_module()


def _payload(*, text: str, citations: list[dict[str, str]] | None = None) -> dict[str, object]:
    return {
        "output": [
            {
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": citations or [],
                    }
                ]
            }
        ]
    }


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: object,
        text: str = "",
        *,
        json_error: bool = False,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or str(payload)
        self._json_error = json_error
        self.headers = headers or {}

    def json(self) -> dict[str, object]:
        if self._json_error:
            raise ValueError("response is not JSON")
        if not isinstance(self._payload, dict):
            return self._payload  # type: ignore[return-value]
        return self._payload


class _FakeHttpClient:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def post(
        self,
        url: str,
        json: dict[str, object],
        headers: dict[str, str],
        timeout: object | None = None,
    ) -> _FakeResponse:
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return self._responses.pop(0)


class _FakeGetClient:
    def __init__(self, responses: dict[str, _FakeResponse]) -> None:
        self._responses = dict(responses)
        self.calls: list[dict[str, object]] = []

    async def get(
        self,
        url: str,
        headers: dict[str, str],
    ) -> _FakeResponse:
        self.calls.append({"url": url, "headers": headers})
        response = self._responses.get(url)
        if response is None:
            return _FakeResponse(404, {"detail": "Not Found"}, "Not Found")
        return response


class _FakeAdminV3Client:
    def __init__(self, responses: dict[str, _FakeResponse]) -> None:
        self._responses = dict(responses)
        self.calls: list[dict[str, object]] = []

    async def post(
        self,
        url: str,
        json: dict[str, object],
        headers: dict[str, str],
        timeout: object | None = None,
    ) -> _FakeResponse:
        self.calls.append(
            {"method": "POST", "url": url, "json": json, "headers": headers, "timeout": timeout}
        )
        return self._responses.get(
            url,
            _FakeResponse(404, {"error": {"message": "Not Found"}}, "Not Found"),
        )

    async def get(
        self,
        url: str,
        headers: dict[str, str],
    ) -> _FakeResponse:
        self.calls.append({"method": "GET", "url": url, "headers": headers})
        return self._responses.get(
            url,
            _FakeResponse(404, {"error": {"message": "Not Found"}}, "Not Found"),
        )


class _FakeRequest:
    def __init__(self, body: dict[str, object]) -> None:
        self._body = body
        self.headers = {"Authorization": "Bearer test-token"}

    async def json(self) -> dict[str, object]:
        return self._body


class SocialNormalizationTests(unittest.TestCase):
    def test_fake_model_url_is_dropped_when_citation_disagrees(self) -> None:
        payload = _payload(
            text='{"answer":"summary","results":[{"url":"https://x.com/fake/status/1234567890123456789","text":"fabricated"}]}',
            citations=[
                {
                    "url": "https://x.com/OpenAI/status/1901234567890123456",
                    "title": "OpenAI launches update",
                }
            ],
        )

        for module in (social_gateway, proxy_server):
            result = module.normalize_social_search_response("OpenAI", payload, 5)
            self.assertEqual(len(result["results"]), 1)
            self.assertEqual(
                result["results"][0]["url"],
                "https://x.com/OpenAI/status/1901234567890123456",
            )
            self.assertEqual(result["results"][0]["text"], "")
            self.assertEqual(
                result["citations"],
                [
                    {
                        "title": "OpenAI launches update",
                        "url": "https://x.com/OpenAI/status/1901234567890123456",
                    }
                ],
            )

    def test_matching_twitter_alias_merges_model_fields_into_trusted_citation(self) -> None:
        payload = _payload(
            text='{"answer":"summary","results":[{"url":"https://twitter.com/openai/status/1901234567890123456","text":"real post text","author":"OpenAI","handle":"@OpenAI","created_at":"2026-03-19T12:00:00Z","why_relevant":"launch context"}]}',
            citations=[
                {
                    "url": "https://x.com/OpenAI/status/1901234567890123456?utm_source=test",
                    "title": "OpenAI launches update",
                }
            ],
        )

        for module in (social_gateway, proxy_server):
            result = module.normalize_social_search_response("OpenAI", payload, 5)
            self.assertEqual(len(result["results"]), 1)
            item = result["results"][0]
            self.assertEqual(
                item["url"],
                "https://x.com/OpenAI/status/1901234567890123456?utm_source=test",
            )
            self.assertEqual(item["text"], "real post text")
            self.assertEqual(item["author"], "OpenAI")
            self.assertEqual(item["handle"], "OpenAI")
            self.assertEqual(item["why_relevant"], "launch context")

    def test_citation_only_payload_still_returns_usable_results(self) -> None:
        payload = _payload(
            text="No structured JSON here.",
            citations=[
                {
                    "url": "https://x.com/modelcontextproto/status/1902234567890123456",
                    "title": "MCP ecosystem update",
                },
                {
                    "url": "https://x.com/modelcontextproto/status/1902234567890123456",
                    "title": "duplicate",
                },
            ],
        )

        for module in (social_gateway, proxy_server):
            result = module.normalize_social_search_response(
                "Model Context Protocol",
                payload,
                5,
            )
            self.assertEqual(len(result["results"]), 1)
            self.assertEqual(
                result["results"][0]["title"],
                "MCP ecosystem update",
            )
            self.assertEqual(result["results"][0]["text"], "")

    def test_without_citations_only_plausible_status_urls_survive(self) -> None:
        payload = _payload(
            text='{"answer":"summary","results":[{"url":"https://x.com/openai/status/1903234567890123456","text":"kept"},{"url":"https://x.com/OpenAI/status/1234567890123456789","text":"drop synthetic"},{"url":"https://modelcontextprotocol.io/docs/getting-started/intro","text":"drop non-social"},{"url":"https://x.com/openai","text":"drop profile"},{"url":"notaurl","text":"drop invalid"}]}'
        )

        for module in (social_gateway, proxy_server):
            result = module.normalize_social_search_response("OpenAI", payload, 5)
            self.assertEqual(
                [item["url"] for item in result["results"]],
                ["https://x.com/openai/status/1903234567890123456"],
            )


class SocialFallbackRouteTests(unittest.IsolatedAsyncioTestCase):
    async def _run_route(
        self,
        module,
        *,
        responses: list[_FakeResponse],
        query: str = "Model Context Protocol",
        max_results: int = 5,
    ) -> tuple[dict[str, object], _FakeHttpClient]:
        fake_client = _FakeHttpClient(responses)
        request = _FakeRequest({"query": query, "source": "x", "max_results": max_results})
        original_http_client = module.http_client

        if module is social_gateway:
            original_resolve = module.resolve_gateway_state
            original_verify = module.verify_gateway_token
            module.resolve_gateway_state = _fake_gateway_state  # type: ignore[assignment]
            module.verify_gateway_token = lambda token, accepted_tokens: None  # type: ignore[assignment]
            route = module.social_search
        else:
            original_resolve = module.resolve_social_gateway_state
            original_verify = module.verify_social_gateway_token
            module.resolve_social_gateway_state = _fake_proxy_state  # type: ignore[assignment]
            module.verify_social_gateway_token = lambda token, accepted_tokens: None  # type: ignore[assignment]
            route = module.proxy_social_search

        module.http_client = fake_client
        try:
            result = await route(request)
        finally:
            module.http_client = original_http_client
            if module is social_gateway:
                module.resolve_gateway_state = original_resolve  # type: ignore[assignment]
                module.verify_gateway_token = original_verify  # type: ignore[assignment]
            else:
                module.resolve_social_gateway_state = original_resolve  # type: ignore[assignment]
                module.verify_social_gateway_token = original_verify  # type: ignore[assignment]

        return result, fake_client

    async def test_low_result_count_triggers_fallback_and_prefers_better_model(self) -> None:
        primary = _payload(
            text='{"answer":"mini","results":[{"url":"https://x.com/mcp/status/1904234567890123456","text":"one"}]}',
            citations=[{"url": "https://x.com/mcp/status/1904234567890123456", "title": "one"}],
        )
        fallback = _payload(
            text='{"answer":"fast","results":[{"url":"https://x.com/mcp/status/1904234567890123456","text":"one"},{"url":"https://x.com/openai/status/1904234567890123457","text":"two"},{"url":"https://x.com/anthropic/status/1904234567890123458","text":"three"}]}',
            citations=[
                {"url": "https://x.com/mcp/status/1904234567890123456", "title": "one"},
                {"url": "https://x.com/openai/status/1904234567890123457", "title": "two"},
                {"url": "https://x.com/anthropic/status/1904234567890123458", "title": "three"},
            ],
        )

        for module in (social_gateway, proxy_server):
            result, client = await self._run_route(
                module,
                responses=[
                    _FakeResponse(200, primary),
                    _FakeResponse(200, fallback),
                ],
            )
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(client.calls[0]["json"]["model"], "grok-4.20-0309")
            self.assertEqual(client.calls[1]["json"]["model"], "grok-4.3")
            self.assertEqual(result["tool_usage"]["social_search_calls"], 2)
            self.assertEqual(result["tool_usage"]["model"], "grok-4.3")
            self.assertEqual(result["route"]["selected_model"], "grok-4.3")

    async def test_requested_model_overrides_primary_model(self) -> None:
        primary = _payload(
            text='{"answer":"custom","results":[{"url":"https://x.com/mcp/status/1904234567890123456","text":"one"}]}',
            citations=[{"url": "https://x.com/mcp/status/1904234567890123456", "title": "one"}],
        )

        for module in (social_gateway, proxy_server):
            fake_client = _FakeHttpClient([_FakeResponse(200, primary)])
            request = _FakeRequest(
                {
                    "query": "Model Context Protocol",
                    "source": "x",
                    "max_results": 1,
                    "model": "grok-4.20-multi-agent-0309",
                }
            )
            original_http_client = module.http_client

            if module is social_gateway:
                original_resolve = module.resolve_gateway_state
                original_verify = module.verify_gateway_token
                module.resolve_gateway_state = _fake_gateway_state  # type: ignore[assignment]
                module.verify_gateway_token = lambda token, accepted_tokens: None  # type: ignore[assignment]
                route = module.social_search
            else:
                original_resolve = module.resolve_social_gateway_state
                original_verify = module.verify_social_gateway_token
                module.resolve_social_gateway_state = _fake_proxy_state  # type: ignore[assignment]
                module.verify_social_gateway_token = lambda token, accepted_tokens: None  # type: ignore[assignment]
                route = module.proxy_social_search

            module.http_client = fake_client
            try:
                result = await route(request)
            finally:
                module.http_client = original_http_client
                if module is social_gateway:
                    module.resolve_gateway_state = original_resolve  # type: ignore[assignment]
                    module.verify_gateway_token = original_verify  # type: ignore[assignment]
                else:
                    module.resolve_social_gateway_state = original_resolve  # type: ignore[assignment]
                    module.verify_social_gateway_token = original_verify  # type: ignore[assignment]

            self.assertEqual(fake_client.calls[0]["json"]["model"], "grok-4.20-multi-agent-0309")
            self.assertEqual(result["route"]["selected_model"], "grok-4.20-multi-agent-0309")
            self.assertFalse(result["route"]["fallback"]["triggered"])
            self.assertFalse(result["route"]["fallback"]["used"])
            self.assertEqual(result["route"]["fallback"]["reason"], "")
            self.assertEqual(len(result["results"]), 1)

    async def test_enough_results_keeps_primary_model(self) -> None:
        primary = _payload(
            text='{"answer":"mini","results":[{"url":"https://x.com/mcp/status/1905234567890123456","text":"one"},{"url":"https://x.com/openai/status/1905234567890123457","text":"two"},{"url":"https://x.com/anthropic/status/1905234567890123458","text":"three"}]}',
            citations=[
                {"url": "https://x.com/mcp/status/1905234567890123456", "title": "one"},
                {"url": "https://x.com/openai/status/1905234567890123457", "title": "two"},
                {"url": "https://x.com/anthropic/status/1905234567890123458", "title": "three"},
            ],
        )

        for module in (social_gateway, proxy_server):
            result, client = await self._run_route(
                module,
                responses=[_FakeResponse(200, primary)],
            )
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(result["tool_usage"]["social_search_calls"], 1)
            self.assertEqual(result["tool_usage"]["model"], "grok-4.20-0309")
            self.assertEqual(result["route"]["selected_model"], "grok-4.20-0309")
            self.assertFalse(result["route"]["fallback"]["triggered"])
            self.assertFalse(result["route"]["fallback"]["used"])

    async def test_max_results_one_does_not_force_fallback(self) -> None:
        primary = _payload(
            text='{"answer":"mini","results":[{"url":"https://x.com/mcp/status/1906234567890123456","text":"one"}]}',
            citations=[{"url": "https://x.com/mcp/status/1906234567890123456", "title": "one"}],
        )

        for module in (social_gateway, proxy_server):
            result, client = await self._run_route(
                module,
                responses=[_FakeResponse(200, primary)],
                max_results=1,
            )
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(result["tool_usage"]["social_search_calls"], 1)
            self.assertFalse(result["route"]["fallback"]["triggered"])
            self.assertEqual(result["route"]["fallback"]["threshold"], 1)

    async def test_upstream_error_falls_back_to_secondary_model(self) -> None:
        fallback = _payload(
            text='{"answer":"fast","results":[{"url":"https://x.com/mcp/status/1907234567890123456","text":"one"},{"url":"https://x.com/openai/status/1907234567890123457","text":"two"}]}',
            citations=[
                {"url": "https://x.com/mcp/status/1907234567890123456", "title": "one"},
                {"url": "https://x.com/openai/status/1907234567890123457", "title": "two"},
            ],
        )

        for module in (social_gateway, proxy_server):
            result, client = await self._run_route(
                module,
                responses=[
                    _FakeResponse(500, {"error": {"message": "primary unavailable"}}),
                    _FakeResponse(200, fallback),
                ],
            )
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(result["tool_usage"]["social_search_calls"], 2)
            self.assertEqual(result["tool_usage"]["model"], "grok-4.3")
            self.assertEqual(result["route"]["selected_model"], "grok-4.3")
            self.assertEqual(result["route"]["fallback"]["reason"], "upstream_error")
            self.assertTrue(result["route"]["fallback"]["used"])
            self.assertEqual(len(result["results"]), 2)


class StandaloneSocialKeySchedulingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        social_gateway.social_upstream_key_schedule.clear()
        social_gateway.social_upstream_key_cursor = 0

    def tearDown(self) -> None:
        social_gateway.social_upstream_key_schedule.clear()
        social_gateway.social_upstream_key_cursor = 0

    def test_upstream_error_redacts_secret_before_truncating(self) -> None:
        secret = "standalone-social-secret-abcdef"
        detail = social_gateway.extract_social_upstream_error(
            {"error": {"message": f"prefix {secret} {'x' * 290}"}},
            "",
            secret,
        )

        self.assertNotIn(secret, detail)
        self.assertNotIn(secret[:12], detail)

    async def test_rate_limited_keys_without_retry_after_use_default_cooldown(self) -> None:
        fake_client = _FakeHttpClient(
            [
                _FakeResponse(429, {"error": {"message": "rate limit exceeded"}}),
                _FakeResponse(429, {"error": {"message": "rate limit exceeded"}}),
            ]
        )
        state = {
            "upstream_base_url": "http://social.example/v1",
            "upstream_responses_path": "/responses",
            "upstream_api_keys": ["standalone-first", "standalone-second"],
            "resolved_upstream_api_key": "standalone-first",
        }
        original_http_client = social_gateway.http_client
        social_gateway.http_client = fake_client
        try:
            attempt = await social_gateway.execute_social_search_attempt(
                "test", {}, state, "grok-test", 5
            )
        finally:
            social_gateway.http_client = original_http_client

        self.assertEqual(attempt["status_code"], 429)
        self.assertEqual(attempt["retry_after_seconds"], 60)
        self.assertEqual(
            social_gateway.social_attempt_http_exception(attempt).headers["Retry-After"],
            "60",
        )

    async def test_rate_limited_key_rotates_to_next_configured_key(self) -> None:
        fake_client = _FakeHttpClient(
            [
                _FakeResponse(
                    429,
                    {"error": {"message": "rate limit exceeded"}},
                    headers={"retry-after": "15"},
                ),
                _FakeResponse(200, {"output_text": '{"answer":"ok","results":[]}'}),
            ]
        )
        state = {
            "upstream_base_url": "http://social.example/v1",
            "upstream_responses_path": "/responses",
            "upstream_api_keys": ["standalone-first", "standalone-second"],
            "resolved_upstream_api_key": "standalone-first",
        }
        original_http_client = social_gateway.http_client
        social_gateway.http_client = fake_client
        try:
            result = await social_gateway.execute_social_search_attempt(
                "test", {}, state, "grok-test", 5
            )
        finally:
            social_gateway.http_client = original_http_client

        self.assertTrue(result["ok"])
        self.assertEqual(
            [call["headers"]["Authorization"] for call in fake_client.calls],
            ["Bearer standalone-first", "Bearer standalone-second"],
        )

    async def test_all_rate_limited_keys_skip_model_fallback_and_keep_retry_after(self) -> None:
        async def resolve_gateway_state(force: bool = False) -> dict[str, object]:
            return {
                "upstream_base_url": "http://social.example/v1",
                "upstream_responses_path": "/responses",
                "upstream_api_keys": ["standalone-first", "standalone-second"],
                "resolved_upstream_api_key": "standalone-first",
                "accepted_tokens": ["client-token"],
                "model": "grok-primary",
                "fallback_model": "grok-fallback",
                "fallback_min_results": 3,
            }

        fake_client = _FakeHttpClient(
            [
                _FakeResponse(
                    429,
                    {"error": {"message": "rate limit exceeded"}},
                    headers={"retry-after": "15"},
                ),
                _FakeResponse(
                    429,
                    {"error": {"message": "rate limit exceeded"}},
                    headers={"retry-after": "20"},
                ),
            ]
        )
        request = _FakeRequest({"query": "test", "source": "x", "max_results": 5})
        original_http_client = social_gateway.http_client
        original_resolve = social_gateway.resolve_gateway_state
        original_verify = social_gateway.verify_gateway_token
        social_gateway.http_client = fake_client
        social_gateway.resolve_gateway_state = resolve_gateway_state  # type: ignore[assignment]
        social_gateway.verify_gateway_token = lambda token, accepted: None  # type: ignore[assignment]
        try:
            with self.assertRaises(social_gateway.HTTPException) as raised:
                await social_gateway.social_search(request)
        finally:
            social_gateway.http_client = original_http_client
            social_gateway.resolve_gateway_state = original_resolve  # type: ignore[assignment]
            social_gateway.verify_gateway_token = original_verify  # type: ignore[assignment]

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.headers.get("Retry-After"), "15")
        self.assertEqual(len(fake_client.calls), 2)

    async def test_public_health_hides_admin_error_details(self) -> None:
        leaked_secret = "standalone-admin-access-secret"

        async def resolve_gateway_state(force: bool = False) -> dict[str, object]:
            return {
                "resolved_upstream_api_key": "standalone-key",
                "upstream_api_keys": ["standalone-key"],
                "accepted_tokens": ["client-token"],
                "mode": "manual",
                "upstream_base_url": "http://social.example/v1",
                "upstream_responses_path": "/responses",
                "admin_base_url": "http://social.example",
                "admin_verify_path": "/v1/admin/verify",
                "admin_config_path": "/admin/api/config",
                "admin_tokens_path": "/admin/api/tokens",
                "model": "grok-test",
                "fallback_model": "",
                "fallback_min_results": 1,
                "token_source": "manual",
                "admin_configured": True,
                "admin_connected": False,
                "admin_auth_mode": "v3_credentials",
                "admin_api_version": "v3",
                "stats": {},
                "error": f"admin login returned {leaked_secret}",
            }

        original_resolve = social_gateway.resolve_gateway_state
        social_gateway.resolve_gateway_state = resolve_gateway_state  # type: ignore[assignment]
        try:
            payload = await social_gateway._build_health_payload()
        finally:
            social_gateway.resolve_gateway_state = original_resolve  # type: ignore[assignment]

        self.assertEqual(payload["error"], "Social gateway configuration requires attention")
        self.assertNotIn(leaked_secret, str(payload))


class SocialAdminCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _v3_responses(base_url: str) -> dict[str, _FakeResponse]:
        return {
            f"{base_url}/api/admin/v1/auth/login": _FakeResponse(
                200,
                {
                    "data": {
                        "tokens": {
                            "accessToken": "admin-jwt",
                            "accessTokenExpiresAt": "2099-01-01T00:00:00Z",
                        }
                    }
                },
            ),
            f"{base_url}/api/admin/v1/accounts/summary": _FakeResponse(
                200,
                {
                    "data": {
                        "total": 6,
                        "available": 4,
                        "recovering": 1,
                        "attention": 1,
                        "providers": {
                            "grok_build": {"total": 4, "available": 3},
                            "grok_web": {"total": 2, "available": 1},
                        },
                    }
                },
            ),
            f"{base_url}/api/admin/v1/dashboard?period=24h&timezone=UTC": _FakeResponse(
                200,
                {
                    "data": {
                        "resources": {"allTimeRequests": 37},
                        "usage": {
                            "requests": 9,
                            "successfulRequests": 8,
                            "failedRequests": 1,
                        },
                    }
                },
            ),
        }

    async def test_proxy_server_uses_grok2api_v3_admin_contract(self) -> None:
        base_url = "http://example.test:8000"
        fake_client = _FakeAdminV3Client(self._v3_responses(base_url))
        original_http_client = proxy_server.http_client
        proxy_server.http_client = fake_client
        proxy_server._clear_social_admin_session()
        config = {
            "upstream_base_url": f"{base_url}/v1",
            "upstream_responses_path": "/responses",
            "admin_base_url": base_url,
            "admin_verify_path": "/v1/admin/verify",
            "admin_config_path": "/admin/api/config",
            "admin_tokens_path": "/admin/api/tokens",
            "admin_username": "admin",
            "admin_password": "password123",
            "admin_app_key": "legacy-key-must-not-win",
            "upstream_api_key": "g2a-client-key",
            "gateway_token": "client-token",
            "model": "grok-4.20-0309",
            "fallback_model": "grok-4.3",
            "fallback_min_results": 3,
            "cache_ttl_seconds": 60,
        }
        try:
            state = await proxy_server.resolve_social_gateway_state_for_config(config)
        finally:
            proxy_server.http_client = original_http_client
            proxy_server._clear_social_admin_session()

        self.assertTrue(state["admin_connected"])
        self.assertEqual(state["admin_auth_mode"], "v3_credentials")
        self.assertEqual(state["admin_api_version"], "v3")
        self.assertEqual(state["mode"], "upstream")
        self.assertEqual(state["stats"]["account_total"], 6)
        self.assertEqual(state["stats"]["account_available"], 4)
        self.assertEqual(state["stats"]["requests_24h"], 9)
        self.assertEqual(state["stats"]["total_calls"], 37)
        called_urls = {str(item["url"]) for item in fake_client.calls}
        self.assertIn(f"{base_url}/api/admin/v1/auth/login", called_urls)
        self.assertNotIn(f"{base_url}/admin/api/config", called_urls)

    async def test_standalone_gateway_uses_v3_session_cache(self) -> None:
        base_url = "http://example.test:8000"
        fake_client = _FakeAdminV3Client(self._v3_responses(base_url))
        original_values = {
            "http_client": social_gateway.http_client,
            "admin_base_url": social_gateway.ADMIN_BASE_URL,
            "admin_username": social_gateway.ADMIN_USERNAME,
            "admin_password": social_gateway.ADMIN_PASSWORD,
            "admin_app_key": social_gateway.ADMIN_APP_KEY,
        }
        social_gateway.http_client = fake_client
        social_gateway.ADMIN_BASE_URL = base_url
        social_gateway.ADMIN_USERNAME = "admin"
        social_gateway.ADMIN_PASSWORD = "password123"
        social_gateway.ADMIN_APP_KEY = "legacy-key-must-not-win"
        social_gateway.clear_admin_session()
        social_gateway.state_cache.update({"value": None, "expires_at": 0.0})
        try:
            first = await social_gateway.resolve_gateway_state(force=True)
            second = await social_gateway.resolve_gateway_state(force=True)
        finally:
            social_gateway.http_client = original_values["http_client"]
            social_gateway.ADMIN_BASE_URL = original_values["admin_base_url"]
            social_gateway.ADMIN_USERNAME = original_values["admin_username"]
            social_gateway.ADMIN_PASSWORD = original_values["admin_password"]
            social_gateway.ADMIN_APP_KEY = original_values["admin_app_key"]
            social_gateway.clear_admin_session()
            social_gateway.state_cache.update({"value": None, "expires_at": 0.0})

        self.assertTrue(first["admin_connected"])
        self.assertEqual(first["admin_api_version"], "v3")
        self.assertEqual(second["stats"]["account_attention"], 1)
        login_calls = [
            item
            for item in fake_client.calls
            if item["method"] == "POST" and str(item["url"]).endswith("/auth/login")
        ]
        self.assertEqual(len(login_calls), 1)

    async def test_social_gateway_falls_back_to_latest_admin_paths(self) -> None:
        original_http_client = social_gateway.http_client
        social_gateway.state_cache["value"] = None
        social_gateway.state_cache["expires_at"] = 0.0
        social_gateway.ADMIN_APP_KEY = "admin-key"
        social_gateway.ADMIN_BASE_URL = "http://example.test:8000"
        social_gateway.ADMIN_CONFIG_PATH = "/v1/admin/config"
        social_gateway.ADMIN_TOKENS_PATH = "/v1/admin/tokens"
        fake_client = _FakeGetClient(
            {
                "http://example.test:8000/admin/api/config": _FakeResponse(
                    200,
                    {"app": {"api_key": "upstream-key"}},
                ),
                "http://example.test:8000/admin/api/tokens": _FakeResponse(
                    200,
                    {"ssoBasic": [{"token": "client-token", "status": "active", "quota": 8}]},
                ),
            }
        )
        social_gateway.http_client = fake_client
        try:
            state = await social_gateway.resolve_gateway_state(force=True)
        finally:
            social_gateway.http_client = original_http_client

        self.assertTrue(state["admin_connected"])
        self.assertEqual(state["admin_config_path"], "/admin/api/config")
        self.assertEqual(state["admin_tokens_path"], "/admin/api/tokens")
        self.assertEqual(state["resolved_upstream_api_key"], "upstream-key")
        self.assertEqual(state["stats"]["token_total"], 1)
        called_urls = {item["url"] for item in fake_client.calls}
        self.assertIn("http://example.test:8000/v1/admin/config", called_urls)
        self.assertIn("http://example.test:8000/admin/api/config", called_urls)
        self.assertIn("http://example.test:8000/v1/admin/tokens", called_urls)
        self.assertIn("http://example.test:8000/admin/api/tokens", called_urls)

    async def test_proxy_server_falls_back_to_latest_admin_paths(self) -> None:
        original_http_client = proxy_server.http_client
        config = {
            "upstream_base_url": "http://example.test/v1",
            "upstream_responses_path": "/responses",
            "admin_base_url": "http://example.test:8000",
            "admin_verify_path": "/v1/admin/verify",
            "admin_config_path": "/v1/admin/config",
            "admin_tokens_path": "/v1/admin/tokens",
            "admin_app_key": "admin-key",
            "upstream_api_key": "",
            "gateway_token": "",
            "model": "grok-4.20-0309",
            "fallback_model": "grok-4.3",
            "fallback_min_results": 3,
            "cache_ttl_seconds": 60,
        }
        fake_client = _FakeGetClient(
            {
                "http://example.test:8000/admin/api/config": _FakeResponse(
                    200,
                    {"data": {"app": {"api_key": "proxy-upstream-key"}}},
                ),
                "http://example.test:8000/admin/api/tokens": _FakeResponse(
                    200,
                    {"tokens": {"ssoBasic": [{"token": "proxy-client-token", "status": "active", "quota": 5}]}},
                ),
            }
        )
        proxy_server.http_client = fake_client
        try:
            state = await proxy_server.resolve_social_gateway_state_for_config(config)
        finally:
            proxy_server.http_client = original_http_client

        self.assertTrue(state["admin_connected"])
        self.assertEqual(state["admin_config_path"], "/admin/api/config")
        self.assertEqual(state["admin_tokens_path"], "/admin/api/tokens")
        self.assertEqual(state["resolved_upstream_api_key"], "proxy-upstream-key")
        self.assertEqual(state["stats"]["token_total"], 1)
        called_urls = {item["url"] for item in fake_client.calls}
        self.assertIn("http://example.test:8000/v1/admin/config", called_urls)
        self.assertIn("http://example.test:8000/admin/api/config", called_urls)
        self.assertIn("http://example.test:8000/v1/admin/tokens", called_urls)
        self.assertIn("http://example.test:8000/admin/api/tokens", called_urls)

    async def test_v3_spa_fallback_is_not_treated_as_admin_json(self) -> None:
        original_http_client = proxy_server.http_client
        config = {
            "upstream_base_url": "http://example.test/v1",
            "upstream_responses_path": "/responses",
            "admin_base_url": "http://example.test:8000",
            "admin_verify_path": "/v1/admin/verify",
            "admin_config_path": "/admin/api/config",
            "admin_tokens_path": "/admin/api/tokens",
            "admin_app_key": "legacy-admin-key",
            "upstream_api_key": "g2a-client-key",
            "gateway_token": "",
            "model": "grok-4.20-0309",
            "fallback_model": "grok-4.3",
            "fallback_min_results": 3,
            "cache_ttl_seconds": 60,
        }
        spa_response = _FakeResponse(200, None, "<html>grok2api v3</html>", json_error=True)
        fake_client = _FakeGetClient(
            {
                "http://example.test:8000/admin/api/config": spa_response,
                "http://example.test:8000/v1/admin/config": spa_response,
                "http://example.test:8000/api/v1/admin/config": spa_response,
            }
        )
        proxy_server.http_client = fake_client
        try:
            state = await proxy_server.resolve_social_gateway_state_for_config(config)
        finally:
            proxy_server.http_client = original_http_client

        self.assertFalse(state["admin_connected"])
        self.assertEqual(state["mode"], "upstream")
        self.assertEqual(state["resolved_upstream_api_key"], "g2a-client-key")
        self.assertIn("expected JSON object", state["error"])

    async def test_standalone_gateway_rejects_v3_spa_as_admin_json(self) -> None:
        from mysearch import social_gateway

        original_http_client = social_gateway.http_client
        original_admin_base_url = social_gateway.ADMIN_BASE_URL
        original_admin_app_key = social_gateway.ADMIN_APP_KEY
        social_gateway.http_client = _FakeGetClient(
            {
                "http://example.test:8000/admin/api/config": _FakeResponse(
                    200,
                    None,
                    "<html>grok2api v3</html>",
                    json_error=True,
                )
            }
        )
        social_gateway.ADMIN_BASE_URL = "http://example.test:8000"
        social_gateway.ADMIN_APP_KEY = "legacy-admin-key"
        try:
            with self.assertRaisesRegex(RuntimeError, "expected JSON object"):
                await social_gateway.fetch_admin_json("/admin/api/config")
        finally:
            social_gateway.http_client = original_http_client
            social_gateway.ADMIN_BASE_URL = original_admin_base_url
            social_gateway.ADMIN_APP_KEY = original_admin_app_key


async def _fake_gateway_state(force: bool = False) -> dict[str, object]:
    return {
        "upstream_base_url": "http://example.test/v1",
        "upstream_responses_path": "/responses",
        "accepted_tokens": ["test-token"],
        "resolved_upstream_api_key": "upstream-key",
        "model": "grok-4.20-0309",
        "fallback_model": "grok-4.3",
        "fallback_min_results": 3,
    }


async def _fake_proxy_state(force: bool = False) -> dict[str, object]:
    return {
        "upstream_base_url": "http://example.test/v1",
        "upstream_responses_path": "/responses",
        "accepted_tokens": ["test-token"],
        "resolved_upstream_api_key": "upstream-key",
        "model": "grok-4.20-0309",
        "fallback_model": "grok-4.3",
        "fallback_min_results": 3,
    }


if __name__ == "__main__":
    unittest.main()
