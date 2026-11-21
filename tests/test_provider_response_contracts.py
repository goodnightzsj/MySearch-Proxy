from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from mysearch.clients import MySearchClient
from mysearch.diagnostics import extract_usage
from proxy.diagnostics import Usage
from test_provider_disabled import fixture_config


class ProviderResponseContractsTests(unittest.TestCase):
    def test_scrape_metadata_credits_preserve_zero_and_do_not_sum_aliases(self):
        for value in (0, 1, 3.5):
            for data in ({"metadata": {"creditsUsed": value}}, {"metadata": {"credits_used": value}}):
                with self.subTest(value=value, data=data):
                    expected = [{"name": "credits", "unit": "credits", "value": value}]
                    self.assertEqual(extract_usage({"data": data}, "scrape"), expected)
                    self.assertEqual(extract_usage(data, "scrape"), expected)
                    self.assertEqual(extract_usage({"data": data, "creditsUsed": 7, "usage": {"credits": 2}}, "scrape"),
                                     [{"name": "credits", "unit": "credits", "value": 2}])

    def test_scrape_metadata_does_not_invent_or_recount_usage(self):
        for value in (None, True, -1, "2", float("nan"), float("inf"), 1e13):
            self.assertEqual(extract_usage({"data": {"metadata": {"creditsUsed": value}}}, "scrape"), [])
        for data in (None, [], {"metadata": "invalid"}):
            self.assertEqual(extract_usage({"data": data}, "scrape"), [])
        for operation in ("search", "crawl_status"):
            self.assertEqual(extract_usage({"data": [{"metadata": {"creditsUsed": 5}}]}, operation), [])

    def test_xai_reported_fetch_counts_keep_distinct_units_and_strict_contract(self):
        payload = {"usage": {"server_side_tool_usage_details": {
            "x_posts_fetched": 25, "x_users_fetched": 0, "unknown": 123,
        }}, "tool_usage": {"x_search_calls": 2}}
        expected = [
            {"name": "x_search_calls", "unit": "requests", "value": 2},
            {"name": "x_posts_fetched", "unit": "posts", "value": 25},
            {"name": "x_users_fetched", "unit": "users", "value": 0},
        ]
        self.assertEqual(extract_usage(payload, "responses"), expected)
        self.assertEqual([Usage.parse_obj(row).dict() for row in expected], expected)
        with self.assertRaises(ValueError):
            Usage.parse_obj({"name": "x_posts_fetched", "unit": "requests", "value": 25})
        self.assertEqual(extract_usage({"tool_usage": {"social_search_calls": 1}}),
                         [{"name": "social_search_calls", "unit": "requests", "value": 1}])

    def test_real_scrape_normalization_and_runtime_diagnostic_share_reported_credit(self):
        client = MySearchClient(fixture_config())
        self.addCleanup(client.close)
        response = {"success": True, "data": {"markdown": "# Full page\n\n" + "Useful content. " * 80,
                    "metadata": {"creditsUsed": 1, "cacheState": "hit"}}}
        with patch.object(client._http, "request", return_value=httpx.Response(200, json=response)):
            result = client.extract_url(url="https://fixture.test/page", provider="firecrawl")
        self.assertEqual(result["metadata"]["creditsUsed"], 1)
        self.assertEqual(result["diagnostics"]["usage"], [
            {"provider": "firecrawl", "name": "credits", "unit": "credits", "value": 1},
        ])

    def test_news_snippet_survives_without_becoming_full_content(self):
        client = MySearchClient(fixture_config())
        self.addCleanup(client.close)
        response = {"data": {"news": [{"title": "New event", "url": "https://fixture.test/news",
                    "snippet": "The confirmed event occurred today.", "date": "2026-11-21"}]}}
        with patch.object(client, "_request_json", return_value=response):
            result = client._search_firecrawl_once(query="news", max_results=3, categories=["news"], include_content=True)
        self.assertEqual(result["results"][0]["snippet"], response["data"]["news"][0]["snippet"])
        self.assertEqual(result["results"][0]["content"], "")
        self.assertEqual(result["results"][0]["published_date"], "2026-11-21")
