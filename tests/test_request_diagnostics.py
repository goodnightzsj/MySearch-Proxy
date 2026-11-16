from __future__ import annotations

import copy
import io
import json
import os
import unittest
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from mysearch.clients import MySearchClient
from mysearch.config import MySearchConfig, ProviderConfig
from mysearch.diagnostics import (
    MAX_CALLS,
    MAX_PHASES,
    diagnosed,
    extract_usage,
    phase,
    request_context,
    upstream_request_id,
)
from mysearch.errors import MySearchError, MySearchHTTPError
from mysearch.providers.base import budgeted_research


def make_config(**overrides):
    values = dict(
        timeout_seconds=45, research_timeout_seconds=90,
        crawl_timeout_seconds=90, xai_social_timeout_seconds=120,
        max_parallel_workers=2, search_cache_ttl_seconds=60,
        extract_cache_ttl_seconds=60, proxy_base_url="", proxy_api_key="",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def provider(base_url="https://provider.test"):
    return ProviderConfig(
        name="tavily", base_url=base_url, auth_mode="bearer",
        auth_header="Authorization", auth_scheme="Bearer", auth_field="api_key",
        default_paths={"search": "/search"}, api_keys=["synthetic-provider-key"],
    )


class ProbeClient(MySearchClient):
    @diagnosed("search")
    def search(self, action, **kwargs):
        return action()

    @diagnosed("research")
    @budgeted_research
    def research(self, action, **kwargs):
        return action()

    @diagnosed("extract_url")
    def extract_url(self, action, **kwargs):
        return action()


class RequestDiagnosticsTests(unittest.TestCase):
    def client(self, cls=ProbeClient, **overrides):
        client = cls(config=make_config(**overrides), keyring=Mock())
        self.addCleanup(client.close)
        return client

    def send(self, client, selected=None):
        return client._request_json_once(
            provider=selected or provider(), method="POST", path="/search",
            payload={"query": "private query"}, key="synthetic-provider-key",
        )

    def test_nested_root_has_one_report_and_preserves_upstream_id(self):
        client = self.client(proxy_base_url="https://proxy.test", proxy_api_key="mysp_fake")
        reports = []

        def report(*args, **kwargs):
            reports.append(copy.deepcopy(kwargs["json"]))
            return httpx.Response(200, request=httpx.Request("POST", args[0]))

        with patch.object(client._http, "post", side_effect=report), patch.object(
            client._http, "request", return_value=httpx.Response(200, json={
                "request_id": "upstream-123", "results": [{"title": "private title"}],
                "usage": {"credits": 2, "unknown": "private query"},
            }),
        ) as http:
            result = client.research(lambda: client.search(lambda: self.send(client, provider("https://proxy.test"))))
        self.assertEqual(len(reports), 1)
        self.assertEqual(result["request_id"], "upstream-123")
        diagnostics = result["diagnostics"]
        self.assertEqual(diagnostics["request_id"], reports[0]["request_id"])
        self.assertEqual(diagnostics["call_count"], 1)
        self.assertEqual(diagnostics["calls"][0]["layer"], "runtime")
        self.assertEqual(diagnostics["calls"][0]["upstream_request_id"], "upstream-123")
        self.assertEqual(diagnostics["usage"], [{"provider": "tavily", "name": "credits", "unit": "credits", "value": 2}])
        self.assertEqual(http.call_args.kwargs["headers"]["X-MySearch-Request-ID"], diagnostics["request_id"])
        self.assertEqual(http.call_args.kwargs["headers"]["X-MySearch-Call-ID"], diagnostics["calls"][0]["call_id"])
        for private in ("private query", "private title", "synthetic-provider-key", "mysp_fake", "https://"):
            self.assertNotIn(private, json.dumps(reports))

    def test_explicit_proxy_configuration_required_for_headers_and_report(self):
        client = self.client()
        gateway = provider()
        gateway.managed_key_pool = True
        with patch.object(client._http, "post") as report, patch.object(
            client._http, "request", return_value=httpx.Response(200, json={"results": []}),
        ) as http:
            result = client.search(lambda: self.send(client, gateway))
        report.assert_not_called()
        self.assertFalse(any(name.startswith("X-MySearch") for name in http.call_args.kwargs["headers"]))
        self.assertEqual(result["diagnostics"]["outcome"], "empty")

    def test_result_outcomes_and_wire_contract(self):
        from proxy.diagnostics import RequestReport

        client = self.client()
        for result, expected in [
            ({"results": [1]}, "success"),
            ({"results": []}, "empty"),
            ({"provider": "tavily_social_fallback", "results": [1]}, "degraded"),
            ({"kind": "unavailable", "results": []}, "unavailable"),
            ({"results": [1], "fallback": {"from": "exa"}}, "degraded"),
        ]:
            with self.subTest(expected=expected):
                state = client.new_request("search")
                with request_context(state):
                    actual = client.search(lambda: result)
                self.assertEqual(actual["diagnostics"]["outcome"], expected)
                validated = RequestReport.parse_obj(state.final)
                self.assertEqual(validated.request_id, actual["diagnostics"]["request_id"])

    def test_config_preserves_explicit_proxy_and_excludes_secret_field_from_repr(self):
        with patch.dict(os.environ, {"MYSEARCH_PROXY_BASE_URL": "https://proxy.test/", "MYSEARCH_PROXY_API_KEY": "mysp_fake"}, clear=True):
            config = MySearchConfig.from_env()
        self.assertEqual(config.proxy_base_url, "https://proxy.test")
        self.assertEqual(config.proxy_api_key, "mysp_fake")
        self.assertFalse(MySearchConfig.__dataclass_fields__["proxy_api_key"].repr)

    def test_invalid_auth_does_not_count_as_sent_http(self):
        client = self.client()
        invalid = provider()
        invalid.auth_mode = "invalid"
        state = client.new_request("search")
        with request_context(state), patch.object(client._http, "request") as http:
            with self.assertRaises(MySearchError):
                client.search(lambda: self.send(client, invalid))
        http.assert_not_called()
        self.assertEqual(state.final["diagnostics"]["call_count"], 0)

    def test_explicit_proxy_does_not_forward_ids_to_direct_provider(self):
        client = self.client(proxy_base_url="https://proxy.test", proxy_api_key="mysp_fake")
        with patch.object(client._http, "post", return_value=httpx.Response(200, request=httpx.Request("POST", "https://proxy.test"))), patch.object(
            client._http, "request", return_value=httpx.Response(200, json={}),
        ) as http:
            client.search(lambda: self.send(client))
        self.assertNotIn("X-MySearch-Request-ID", http.call_args.kwargs["headers"])

    def test_cache_has_fresh_request_id_and_contains_no_diagnostics(self):
        client = self.client(cls=MySearchClient)
        content = {"provider": "tavily", "content": "Useful article content. " * 50}
        with patch.object(client, "_extract_tavily", return_value=content) as extract:
            first = client.extract_url(url="https://article.test/page", provider="tavily")
            second = client.extract_url(url="https://article.test/page", provider="tavily")
        extract.assert_called_once()
        self.assertNotEqual(first["diagnostics"]["request_id"], second["diagnostics"]["request_id"])
        self.assertTrue(second["cache"]["extract"]["hit"])
        self.assertEqual(second["diagnostics"]["call_count"], 0)
        key = client._build_extract_cache_key(url="https://article.test/page", formats=["markdown"], only_main_content=True, provider="tavily")
        self.assertNotIn("diagnostics", client._cache_get("extract", key))

    def test_transport_error_keeps_exception_type_but_report_has_no_text(self):
        client = self.client()
        state = client.new_request("search")
        with request_context(state), patch.object(client._http, "request", return_value=httpx.Response(503, json={"error": "private failure details"})):
            with self.assertRaises(MySearchHTTPError):
                client.search(lambda: self.send(client))
        self.assertEqual(state.final["outcome"], "error")
        self.assertEqual(state.final["failure_reason"], "MySearchHTTPError")
        self.assertEqual(state.final["diagnostics"]["calls"][0]["status_code"], 503)
        self.assertNotIn("private failure", json.dumps(state.final))

    def test_key_rotation_records_two_runtime_calls_with_separate_ids(self):
        client = self.client()
        client.keyring.generation = 0
        client.keyring.has_configured_provider.return_value = True
        client.keyring.is_available.return_value = True
        client.keyring.get_next.return_value = SimpleNamespace(key="synthetic-second-key")
        with patch.object(client._http, "request", side_effect=[
            httpx.Response(401, json={"error": "invalid api key"}),
            httpx.Response(200, json={"results": [1], "usage": {"credits": 2}}),
        ]):
            result = client.search(lambda: client._request_json(
                provider=provider(), method="POST", path="/search", payload={}, key="synthetic-first-key",
            ))
        calls = result["diagnostics"]["calls"]
        self.assertEqual([call["status_code"] for call in calls], [401, 200])
        self.assertEqual([call["outcome"] for call in calls], ["error", "success"])
        self.assertNotEqual(calls[0]["call_id"], calls[1]["call_id"])
        self.assertEqual(result["diagnostics"]["usage"][0]["value"], 2)

    def test_urlopen_test_hook_keeps_diagnostics(self):
        client = self.client()
        response = io.BytesIO(b'{"results": [1], "usage": {"credits": 1}}')
        response.status = 200
        with patch("mysearch.providers.base.urlopen", return_value=response):
            result = client.search(lambda: self.send(client))
        self.assertEqual(result["diagnostics"]["call_count"], 1)
        self.assertEqual(result["diagnostics"]["calls"][0]["status_code"], 200)

    def test_parallel_roots_have_independent_contexts(self):
        client = self.client()
        ready, release = Event(), Event()
        results, errors = [], []

        def worker():
            try:
                def action():
                    ready.set()
                    release.wait(2)
                    return {"results": [1]}
                results.append(client.search(action))
            except BaseException as exc:
                errors.append(exc)

        thread = Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(ready.wait(1))
            second = client.search(lambda: {"results": [2]})
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(errors)
        self.assertEqual(len(results), 1)
        self.assertNotEqual(second["diagnostics"]["request_id"], results[0]["diagnostics"]["request_id"])
        self.assertEqual(results[0]["diagnostics"]["outcome"], "success")

    def test_report_failure_is_bounded_and_does_not_change_result(self):
        client = self.client(proxy_base_url="https://proxy.test/", proxy_api_key="mysp_fake")
        sent = []

        def fail(*args, **kwargs):
            sent.append(copy.deepcopy(kwargs["json"]))
            raise httpx.ConnectError("private failure text")

        with patch.object(client._http, "post", side_effect=fail) as post, self.assertLogs("mysearch.providers.base", level="WARNING") as logs:
            result = client.search(lambda: {"results": [1]})
        self.assertEqual(result["results"], [1])
        self.assertEqual(result["diagnostics"]["warning"], "telemetry_failed")
        self.assertNotIn("warning", sent[0]["diagnostics"])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[0], "https://proxy.test/api/mysearch/requests")
        self.assertEqual(post.call_args.kwargs["timeout"], 2.0)
        self.assertFalse(post.call_args.kwargs["follow_redirects"])
        self.assertNotIn("private failure", " ".join(logs.output))

    def test_cancelled_terminal_freezes_pending_call_and_cannot_be_replaced(self):
        client = self.client()
        started, release = Event(), Event()
        state = client.new_request("search")

        def send(*args, **kwargs):
            started.set()
            release.wait(2)
            return httpx.Response(200, json={"usage": {"credits": 99}})

        with patch.object(client._http, "request", side_effect=send):
            with request_context(state):
                # The transport runs under the same request context as an offloaded MCP call.
                def worker():
                    with request_context(state):
                        return self.send(client)
                future = client._submit_tracked(client._executor, worker)
            try:
                self.assertTrue(started.wait(1))
                state.cancel()
                client.finish_request(state)
                before = copy.deepcopy(state.final)
                self.assertEqual(before["outcome"], "cancelled")
                self.assertEqual(before["diagnostics"]["calls"][0]["outcome"], "pending")
            finally:
                release.set()
                future.result(timeout=2)
            client.finish_request(state, result={"results": [1]})
            self.assertEqual(state.final, before)

    def test_bounded_calls_phases_and_usage_only_keep_whitelisted_quantities(self):
        client = self.client()
        with patch.object(client._http, "request", return_value=httpx.Response(200, json={"usage": {"credits": 1}})):
            def action():
                for _ in range(MAX_CALLS + 2):
                    with phase("discovery"):
                        self.send(client)
                return {"results": [1]}
            result = client.search(action)
        diagnostics = result["diagnostics"]
        self.assertEqual(len(diagnostics["calls"]), MAX_CALLS)
        self.assertEqual(diagnostics["calls_truncated"], 2)
        self.assertEqual(len(diagnostics["phases"]), MAX_PHASES)
        self.assertGreater(diagnostics["phases_truncated"], 0)
        self.assertEqual(diagnostics["usage"][0]["value"], MAX_CALLS + 2)
        self.assertEqual(extract_usage({"usage": {"input_tokens": 0, "output_tokens": True, "total_tokens": float("inf"), "credits": -1}, "costDollars": {"total": 0.002}, "tool_usage": {"x_search_calls": 3, "unknown": 9}}), [
            {"name": "input_tokens", "unit": "tokens", "value": 0},
            {"name": "cost", "unit": "usd", "value": 0.002},
            {"name": "x_search_calls", "unit": "requests", "value": 3},
        ])
        self.assertEqual(extract_usage({}), [])
        self.assertEqual(upstream_request_id({"request_id": "https://private.test/query"}), "")

    def test_close_waits_for_shared_and_temporary_workers_before_http_close(self):
        for temporary in (False, True):
            with self.subTest(temporary=temporary):
                client = self.client()
                started, release, closed = Event(), Event(), Event()

                def worker():
                    started.set()
                    release.wait(2)

                if temporary:
                    results, errors = client._execute_parallel({"slow": worker}, max_workers=1, timeout_seconds=0.02)
                    self.assertEqual(results, {})
                    self.assertIn("slow", errors)
                else:
                    client._submit_tracked(client._executor, worker)
                self.assertTrue(started.wait(1))
                with patch.object(client._http, "close", side_effect=closed.set) as http_close:
                    closers = [Thread(target=client.close) for _ in range(2)]
                    for closer in closers:
                        closer.start()
                    try:
                        self.assertFalse(closed.wait(0.03))
                        with self.assertRaisesRegex(MySearchError, "closing"):
                            client.new_request("search")
                    finally:
                        release.set()
                        for closer in closers:
                            closer.join(2)
                    self.assertTrue(closed.is_set())
                    self.assertTrue(all(not closer.is_alive() for closer in closers))
                    http_close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
