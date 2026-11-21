import copy
import json
import unittest
from unittest.mock import Mock

from mcp.shared.memory import create_connected_server_and_client_session

from mysearch.clients import MySearchClient
from mysearch.errors import MySearchError, MySearchHTTPError, classify_upstream_key_failure
from mysearch.query_routing import _is_retryable_transient_error
import test_mcp_runtime as mcp_tests
from test_request_diagnostics import provider, make_config


class ProviderFreshnessTests(unittest.TestCase):
    def client(self):
        config = make_config()
        config.firecrawl = provider()
        config.tavily = provider()
        client = MySearchClient(config=config, keyring=Mock())
        self.addCleanup(client.close)
        client._get_key_or_raise = Mock(return_value=Mock(key="fake", source="test"))
        return client

    def test_explicit_freshness_bypasses_local_cache_and_preserves_default_cache(self):
        client = self.client()
        def response(**kwargs):
            age = kwargs.get("max_age_seconds")
            return {"provider": "firecrawl", "url": "https://example.test/page", "content": str(age) * 200, "metadata": {}}
        client._scrape_firecrawl = Mock(side_effect=response)
        default = client.extract_url(url="https://example.test/page", provider="firecrawl")
        fresh = client.extract_url(url="https://example.test/page", provider="firecrawl", max_age_seconds=0)
        client.extract_url(url="https://example.test/page", provider="firecrawl", max_age_seconds=0)
        cached = client.extract_url(url="https://example.test/page", provider="firecrawl")
        self.assertEqual(client._scrape_firecrawl.call_count, 3)
        self.assertEqual(default["content"], cached["content"])
        self.assertTrue(cached["cache"]["extract"]["hit"])
        self.assertFalse(fresh["cache"]["extract"]["hit"])

    def test_freshness_validation_prevents_unsupported_fallback_claims(self):
        client = self.client()
        client._scrape_firecrawl = Mock()
        for age, selected in [(-1, "firecrawl"), (True, "firecrawl"), (1.5, "firecrawl"), (0, "auto"), (0, "tavily")]:
            with self.subTest(age=age, provider=selected), self.assertRaises(MySearchError):
                client.extract_url(url="https://example.test", provider=selected, max_age_seconds=age)
        client._scrape_firecrawl.assert_not_called()

    def test_firecrawl_age_uses_milliseconds_and_default_is_omitted(self):
        client = self.client()
        client._request_json = Mock(return_value={"data": {"markdown": "body", "metadata": {}}})
        for age, expected in [(None, None), (0, 0), (60, 60000)]:
            client._scrape_firecrawl(url="https://example.test", formats=["markdown"], only_main_content=True, max_age_seconds=age)
            payload = client._request_json.call_args.kwargs["payload"]
            if expected is None:
                self.assertNotIn("maxAge", payload)
            else:
                self.assertEqual(payload["maxAge"], expected)

    def test_tavily_fast_is_limited_and_usage_is_requested(self):
        client = self.client()
        client._request_json = Mock(return_value={"results": []})
        base = dict(query="documentation", max_results=3, topic="general", include_answer=False,
                    include_content=False, include_domains=None, exclude_domains=None, strategy="fast")
        for overrides, expected in [({}, "fast"), ({"strategy": "balanced"}, "basic"),
                                    ({"strategy": "verify"}, "advanced"), ({"strategy": "deep"}, "advanced"),
                                    ({"include_content": True}, "advanced"), ({"topic": "news"}, "basic"),
                                    ({"from_date": "2026-11-01"}, "basic"), ({"exclude_domains": ["example.test"]}, "basic")]:
            with self.subTest(overrides=overrides):
                client._search_tavily_once(**{**base, **overrides})
                payload = client._request_json.call_args.kwargs["payload"]
                self.assertEqual(payload["search_depth"], expected)
                self.assertTrue(payload["include_usage"])

    def test_tavily_separates_fetch_age_from_publication_window(self):
        client = self.client()
        client._request_json = Mock(return_value={"results": []})
        base = dict(max_results=3, topic="general", include_answer=False,
                    include_content=False, include_domains=None, exclude_domains=None)
        for query, dates, fresh in [
            ("service pricing", {}, True), ("service status outage", {}, True),
            ("database architecture", {}, False),
            ("database release notes", {"from_date": "2026-10-01"}, False),
            ("service pricing", {"to_date": "2026-10-01"}, True),
        ]:
            with self.subTest(query=query, dates=dates):
                client._search_tavily_once(query=query, days=7, **dates, **base)
                payload = client._request_json.call_args.kwargs["payload"]
                self.assertTrue(payload["include_published_date"])
                self.assertEqual(payload.get("max_age_hours"), 0 if fresh else None)
                self.assertEqual(payload.get("filter_by_published_date", False), bool(dates))
                if fresh:
                    self.assertNotIn("days", payload)
                if dates:
                    self.assertTrue(payload.get("start_date") or payload.get("end_date"))

    def test_flex_capacity_is_transient_and_does_not_disable_key(self):
        error = MySearchHTTPError(provider="tavily", status_code=434, detail="Flex tier capacity exceeded", url="https://fixture.test")
        self.assertTrue(_is_retryable_transient_error(error))
        self.assertEqual(classify_upstream_key_failure(434, error.detail, service="tavily"), "")
        self.assertFalse(_is_retryable_transient_error(MySearchHTTPError(provider="firecrawl", status_code=434, detail="other", url="https://fixture.test")))
        client = self.client()
        client._request_json = Mock(side_effect=error)
        with self.assertRaises(MySearchHTTPError):
            client._search_tavily_once(query="service pricing", max_results=1, topic="general",
                                      include_answer=False, include_content=False, include_domains=None, exclude_domains=None)
        self.assertEqual(client._request_json.call_count, 1)
        self.assertEqual(client._request_json.call_args.kwargs["payload"]["max_age_hours"], 0)


class ExtractWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_windows_preserve_cached_full_payload_and_text_structured_parity(self):
        client = mcp_tests.StubClient()
        client.payloads["extract_url"]["content"] = "中文abcdef"
        original = copy.deepcopy(client.payloads["extract_url"])
        mcp = mcp_tests.McpRuntimeTests.build(self, client)
        async with create_connected_server_and_client_session(mcp) as session:
            first = await session.call_tool("extract_url", {"url": "https://result.test", "max_chars": 3})
            second = await session.call_tool("extract_url", {"url": "https://result.test", "offset": 3, "max_chars": 9})
            full = await session.call_tool("extract_url", {"url": "https://result.test"})
            self.assertEqual(first.structuredContent["content"] + second.structuredContent["content"], original["content"])
            self.assertEqual(first.structuredContent["content_window"]["next_offset"], 3)
            self.assertIsNone(second.structuredContent["content_window"]["next_offset"])
            self.assertEqual({key: value for key, value in full.structuredContent.items() if key != "diagnostics"}, original)
            self.assertEqual(full.structuredContent["diagnostics"]["outcome"], "success")
            self.assertEqual(json.loads(first.content[0].text), first.structuredContent)
            for args in [{"offset": -1}, {"max_chars": 0}, {"max_chars": True}]:
                invalid = await session.call_tool("extract_url", {"url": "https://result.test", **args})
                self.assertTrue(invalid.isError)
        self.assertEqual(client.payloads["extract_url"], original)
