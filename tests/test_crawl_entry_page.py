from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from mysearch.diagnostics import current_request
from mysearch.errors import MySearchError
from tests import test_social_deadline


class CrawlEntryPageTests(unittest.TestCase):
    def run_crawl(self, pages, *, url="https://docs.test/topic", limit=2, scrape=None, status="completed", cancel=False):
        client = test_social_deadline.SocialDeadlineTests.client(self)
        client.config.firecrawl.default_paths.update(crawl="/crawl", scrape="/scrape")
        calls = []

        def http(method, target, **kwargs):
            calls.append((method, target, kwargs))
            if target.endswith("/scrape"):
                if cancel:
                    current_request.get().cancel()
                return httpx.Response(200, json=scrape or {"data": {"markdown": ""}})
            return httpx.Response(200, json={"status": status, "total": len(pages), "completed": len(pages), "data": pages})

        with patch.object(client._http, "request", side_effect=http):
            result = client.crawl_site(url=url, limit=limit, max_depth=0, crawl_entire_domain=False)
        self.assertEqual(calls[0][2]["json"], {"url": url, "limit": limit, "maxDiscoveryDepth": 0, "crawlEntireDomain": False})
        return result, calls

    def test_missing_entry_is_first_and_limit_preserves_upstream_counts(self):
        pages = [{"url": f"https://docs.test/page{i}", "markdown": f"Other page {i}"} for i in range(5)]
        result, calls = self.run_crawl(pages, limit=5, scrape={"data": {
            "metadata": {"sourceURL": "https://docs.test/topic/#overview", "title": "Topic"},
            "markdown": "Call add_task after returning a response.",
        }})
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["count"], 5)
        self.assertEqual(result["pages"][0]["content"], "Call add_task after returning a response.")
        self.assertEqual([page["url"] for page in result["pages"]][1:], [page["url"] for page in pages[:4]])
        self.assertEqual(result["metadata"]["total"], 5)
        self.assertEqual(result["metadata"]["completed"], 5)
        self.assertEqual(result["metadata"]["entry_page"]["omitted_pages"], 1)

    def test_existing_entry_needs_no_scrape(self):
        result, calls = self.run_crawl([{"url": "https://docs.test/topic/#overview", "markdown": "Existing"}])
        self.assertEqual(len(calls), 1)
        self.assertNotIn("entry_page", result["metadata"])

    def test_empty_entry_is_replaced_within_limit_one(self):
        result, calls = self.run_crawl([
            {"url": "https://docs.test/topic", "markdown": ""},
            {"url": "https://docs.test/other", "markdown": "Other"},
        ], limit=1, scrape={"data": {"markdown": "Actual entry text"}})
        self.assertEqual(result["pages"], [{"url": "https://docs.test/topic", "title": "", "content": "Actual entry text"}])
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(calls), 2)

    def test_query_and_hash_route_are_distinct_and_failure_is_visible(self):
        for returned in ("https://docs.test/topic?q=other", "https://docs.test/topic#/other"):
            with self.subTest(returned=returned):
                pages = [{"url": returned, "markdown": "Other content"}]
                result, calls = self.run_crawl(pages, scrape={"data": {"url": returned, "markdown": "Other content"}})
                self.assertEqual(len(calls), 2)
                self.assertEqual(result["pages"][0]["url"], returned)
                self.assertEqual(result["metadata"]["entry_page"]["status"], "failed")
                self.assertEqual(result["diagnostics"]["outcome"], "degraded")

    def test_noncompleted_jobs_do_not_scrape(self):
        for status in ("failed", "cancelled", ""):
            with self.subTest(status=status):
                result, calls = self.run_crawl([], status=status)
                self.assertEqual(len(calls), 1)
                self.assertEqual(result["status"], status)

    def test_cancel_during_recovery_is_not_partial_success(self):
        with self.assertRaisesRegex(MySearchError, "budget exhausted"):
            self.run_crawl([], cancel=True)
