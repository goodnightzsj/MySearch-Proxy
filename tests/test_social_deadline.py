from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from mysearch.diagnostics import RequestState, current_deadline, request_context
from mysearch.errors import MySearchError
from tests.test_request_diagnostics import ProbeClient, make_config, provider


class SocialDeadlineTests(unittest.TestCase):
    def client(self):
        providers = {}
        for name in ("xai", "firecrawl", "tavily", "exa"):
            item = provider(f"https://{name}.test")
            item.name = name
            item.default_paths["social_search"] = "/social/search"
            providers[name] = item
        keyring = Mock()
        keyring.get_next.return_value = SimpleNamespace(key="synthetic-key", source="env")
        client = ProbeClient(config=make_config(**providers), keyring=keyring)
        self.addCleanup(client.close)
        return client

    def social(self, client):
        return client._search_xai_compatible(
            query="project releases", sources=["x"], max_results=5,
            allowed_x_handles=None, excluded_x_handles=None,
            from_date=None, to_date=None, include_x_images=False, include_x_videos=False,
        )

    def test_domain_retries_share_window_and_leave_next_provider_time(self):
        client = self.client()
        clock = [0.0]
        attempts = []

        def http(method, url, **kwargs):
            timeout = kwargs["timeout"]
            attempts.append((url, timeout))
            if "xai.test" in url:
                return httpx.Response(503, json={"error": "unavailable"})
            if "firecrawl.test" in url:
                clock[0] += min(24, timeout)
                return httpx.Response(200, json={"data": {"web": []}})
            self.assertIn("tavily.test", url)
            clock[0] += 2
            return httpx.Response(200, json={"results": [{
                "url": "https://x.com/project/status/123", "content": "Release notes",
            }]})

        with patch("time.monotonic", side_effect=lambda: clock[0]), patch.object(client._http, "request", side_effect=http):
            result = client.search(lambda: self.social(client), mode="social")
        self.assertEqual(result["provider"], "tavily_social_fallback")
        self.assertEqual([timeout for url, timeout in attempts if "firecrawl" in url], [30, 6])
        self.assertEqual(clock[0], 32)
        self.assertEqual(result["diagnostics"]["outcome"], "degraded")
        self.assertIsNone(current_deadline.get())

    def test_short_parent_and_cancellation_prevent_more_http(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                client = self.client()
                clock = [0.0]
                state = RequestState(deadline=7, tool="search")

                def http(method, url, **kwargs):
                    self.assertLessEqual(kwargs["timeout"], 7)
                    if cancel:
                        state.cancel()
                    else:
                        clock[0] = 7
                    return httpx.Response(200, json={"data": {"web": []}})

                with patch("time.monotonic", side_effect=lambda: clock[0]), request_context(state), patch.object(client._http, "request", side_effect=http) as send:
                    with self.assertRaises(MySearchError):
                        client._search_firecrawl_social_fallback(
                            query="topic", max_results=5, from_date=None, to_date=None,
                            fallback_reason="unavailable", timeout_seconds=30,
                        )
                self.assertEqual(send.call_count, 1)
                self.assertIsNone(current_deadline.get())

    def test_direct_adapter_call_also_bounds_domain_retries(self):
        client = self.client()
        clock = [0.0]

        def http(method, url, **kwargs):
            clock[0] += kwargs["timeout"]
            return httpx.Response(200, json={"data": {"web": []}})

        with patch("time.monotonic", side_effect=lambda: clock[0]), patch.object(client._http, "request", side_effect=http) as send:
            with self.assertRaises(MySearchError):
                client._search_firecrawl_social_fallback(
                    query="topic", max_results=5, from_date=None, to_date=None,
                    fallback_reason="unavailable", timeout_seconds=3,
                )
        self.assertEqual(send.call_count, 1)
        self.assertEqual(clock[0], 3)
        self.assertIsNone(current_deadline.get())
