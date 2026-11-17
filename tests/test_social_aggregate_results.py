from __future__ import annotations

import unittest
from unittest.mock import Mock

from mysearch.clients import MySearchClient
from mysearch.errors import MySearchError
from mysearch.research import social


class SocialAggregateTests(unittest.TestCase):
    def client(self, rows, answer=""):
        client = MySearchClient()
        self.addCleanup(client.close)
        payload = {"results": rows, "answer": answer, "citations": [{"url": row["url"]} for row in rows]}
        client._search_firecrawl = Mock(return_value=payload)
        client._search_tavily_once = Mock(return_value=payload)
        client._search_exa = Mock(return_value=payload)
        return client

    def test_fallbacks_preserve_named_and_anonymous_posts_without_trend_pages(self):
        named = "https://x.com/example/status/123"
        anonymous = "https://x.com/i/status/456"
        explicit = "https://x.com/i/status/789"
        rows = [{"url": url, "title": "Update", "snippet": "Source text"} for url in (
            "https://x.com/i/trending/111", "https://twitter.com/search?q=topic",
            named, anonymous, explicit, "https://x.com/example/status/999",
        )]
        rows[4]["author"] = "Known Author"
        client = self.client(rows)
        for name in ("firecrawl", "tavily", "exa"):
            with self.subTest(provider=name):
                result = getattr(client, f"_search_{name}_social_fallback")(
                    query="topic", max_results=5, from_date=None, to_date=None,
                    fallback_reason="primary unavailable", timeout_seconds=10,
                )
                self.assertEqual([row["url"] for row in result["results"]], [named, anonymous, explicit])
                self.assertEqual([row["author"] for row in result["results"]], ["example", "", "known author"])
                self.assertEqual([row["url"] for row in result["citations"]], [named, anonymous, explicit])

    def test_tavily_clears_answer_only_when_aggregate_evidence_is_removed(self):
        post = {"url": "https://x.com/example/status/123", "title": "Update", "snippet": "Source text"}
        trend = {"url": "https://x.com/i/trending/111", "title": "Trend", "snippet": "Unsupported claim"}
        for rows, answer, expected in (
            ([trend, post], "Unsupported claim https://x.com/i/trending/111", ""),
            ([post], "Supported source summary", "Supported source summary"),
        ):
            with self.subTest(rows=rows):
                client = self.client(rows, answer)
                result = client._search_tavily_social_fallback(
                    query="topic", max_results=5, from_date=None, to_date=None,
                    fallback_reason="primary unavailable", timeout_seconds=10,
                )
                self.assertEqual(result["answer"], expected)
                self.assertEqual([row["url"] for row in result["citations"]], [post["url"]])

    def test_only_aggregate_results_fail_so_the_chain_can_try_the_next_provider(self):
        client = self.client([{"url": "https://x.com/i/trending/111", "title": "Trend", "snippet": "Topic"}])
        for name in ("firecrawl", "tavily", "exa"):
            with self.subTest(provider=name), self.assertRaises(MySearchError):
                getattr(client, f"_search_{name}_social_fallback")(
                    query="topic", max_results=5, from_date=None, to_date=None,
                    fallback_reason="primary unavailable", timeout_seconds=10,
                )

    def test_exa_existing_mirror_support_is_preserved(self):
        url = "https://xcancel.com/example/status/123"
        result = social._normalize_exa_social_fallback_results([
            {"url": url, "title": "Update", "snippet": "Source text"},
        ])
        self.assertEqual([row["url"] for row in result], [url])
