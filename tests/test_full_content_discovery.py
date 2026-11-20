from __future__ import annotations

import unittest
from unittest.mock import patch

from mysearch.clients import MySearchClient
from test_provider_disabled import fixture_config


class FullContentDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.client = MySearchClient(fixture_config())
        self.addCleanup(self.client.close)

    def firecrawl(self, query="API background mode", *, include_content=True, categories=None):
        return self.client._search_firecrawl_once(
            query=query, max_results=3, categories=categories or [],
            include_content=include_content,
        )

    def test_content_discovery_prefers_specific_pages_without_truncating_full(self):
        content = "完整正文\n" * 20000 + "END OF DOCUMENT"
        response = {"data": {"web": [{"url": "https://docs.example.test/background", "title": "Background mode", "description": "Specific guide", "markdown": content}]}}
        with patch.object(self.client, "_request_json", return_value=response) as request:
            result = self.firecrawl()
        payload = request.call_args.kwargs["payload"]
        self.assertEqual(payload["query"], "API background mode -inurl:llms-full.txt -inurl:llms.txt")
        self.assertEqual(payload["scrapeOptions"], {"formats": ["markdown"], "onlyMainContent": True})
        self.assertEqual(result["query"], "API background mode")
        self.assertEqual(result["results"][0]["content"], content)
        self.assertEqual(result["route_debug"]["content_discovery_policy"], "specific-pages")

    def test_explicit_aggregate_document_search_is_unchanged(self):
        for query in ("Read https://docs.example.test/llms-full.txt", "LLMS-FULL.TXT", "How to use llms.txt"):
            with self.subTest(query=query), patch.object(self.client, "_request_json", return_value={"data": {}}) as request:
                self.firecrawl(query)
            self.assertEqual(request.call_args.kwargs["payload"]["query"], query)

    def test_discovery_without_content_and_news_keep_original_query(self):
        for content, categories in ((False, []), (True, ["news"])):
            with self.subTest(content=content, categories=categories), patch.object(self.client, "_request_json", return_value={"data": {}}) as request:
                self.firecrawl(include_content=content, categories=categories)
            self.assertEqual(request.call_args.kwargs["payload"]["query"], "API background mode")

    def test_explicit_extract_of_aggregate_document_still_returns_full_body(self):
        content = "Aggregate document body\n" * 10000 + "FINAL PAGE"
        with patch.object(self.client, "_scrape_firecrawl", return_value={"provider": "firecrawl", "url": "https://docs.example.test/llms-full.txt", "content": content, "metadata": {}}):
            result = self.client.extract_url(url="https://docs.example.test/llms-full.txt", provider="firecrawl")
        self.assertEqual(result["content"], content)

    def test_identical_extract_reuses_existing_cache_and_preserves_caller_isolation(self):
        extracted = {"provider": "firecrawl", "url": "https://docs.example.test/page", "content": "Complete page content\n" * 20, "metadata": {"title": "Guide"}}
        with patch.object(self.client, "_scrape_firecrawl", return_value=extracted) as scrape:
            first = self.client.extract_url(url=extracted["url"], provider="firecrawl")
            first["metadata"]["title"] = "Changed by caller"
            second = self.client.extract_url(url=extracted["url"], provider="firecrawl")
        scrape.assert_called_once()
        self.assertEqual(second["metadata"]["title"], "Guide")
        self.assertTrue(second["cache"]["extract"]["hit"])
