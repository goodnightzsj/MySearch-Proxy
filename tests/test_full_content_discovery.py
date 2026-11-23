from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from mysearch.clients import MySearchClient, RouteDecision
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
        self.assertNotIn("scrapeOptions", payload)
        self.assertEqual(result["query"], "API background mode")
        self.assertEqual(result["results"][0]["content"], content)
        self.assertEqual(result["route_debug"]["content_discovery_policy"], "specific-pages")

    def test_ignored_query_exclusion_cannot_scrape_aggregate_urls(self):
        aggregate = "https://docs.example.test/llms-full.txt?download=1"
        page = "https://docs.example.test/background"
        response = {"data": {"web": [
            {"url": aggregate, "title": "All documentation"},
            {"url": page, "title": "Background mode", "description": "Specific guide"},
        ]}}
        content = "Full target body\n" * 10000 + "FINAL SECTION"
        with patch.object(self.client, "_request_json", return_value=response), patch.object(
            self.client, "extract_url", return_value={"provider": "firecrawl", "content": content, "metadata": {}}
        ) as extract:
            result = self.firecrawl()
        self.assertEqual(extract.call_args.kwargs["url"], page)
        extract.assert_called_once()
        self.assertEqual([item["url"] for item in result["results"]], [page])
        self.assertEqual(result["results"][0]["content"], content)
        self.assertEqual(result["route_debug"]["excluded_urls"], [
            {"url": aggregate, "reason": "aggregate-document", "fetch_status": "not-attempted"}
        ])

    def test_aggregate_eligibility_uses_decoded_path_not_query_or_hostname(self):
        from mysearch.query_routing import _is_aggregate_document_url
        for url in ("https://docs.test/llms.txt", "https://docs.test/%6clms-FULL.txt?x=1", "https://docs.test/api/llms-small.txt#part"):
            self.assertTrue(_is_aggregate_document_url(url), url)
        for url in ("https://llms.txt/guide", "https://docs.test/guide?file=llms.txt", "https://docs.test/llms.txt-guide", "https://docs.test/llms-txt/guide"):
            self.assertFalse(_is_aggregate_document_url(url), url)

    def test_discovered_url_is_preserved_when_body_fetch_fails(self):
        from mysearch.errors import MySearchError
        page = "https://docs.example.test/background"
        with patch.object(self.client, "_request_json", return_value={"data": {"web": [{"url": page}]}}), patch.object(
            self.client, "extract_url", side_effect=MySearchError("synthetic upstream failure")
        ):
            result = self.firecrawl()
        self.assertEqual(result["results"][0]["url"], page)
        self.assertEqual(result["results"][0]["content"], "")
        self.assertEqual(result["results"][0]["content_fetch"]["status"], "failed")
        self.assertIn("synthetic upstream failure", result["results"][0]["content_fetch"]["error"])

    def test_explicit_aggregate_document_search_is_unchanged(self):
        content = "Complete aggregate\n" * 20000 + "FINAL AGGREGATE SECTION"
        response = {"data": {"web": [{"url": "https://docs.example.test/llms-full.txt", "markdown": content}]}}
        for query in ("Read https://docs.example.test/llms-full.txt", "LLMS-FULL.TXT", "How to use llms.txt", "llms-small.txt"):
            with self.subTest(query=query), patch.object(self.client, "_request_json", return_value=response) as request:
                result = self.firecrawl(query)
            self.assertEqual(request.call_args.kwargs["payload"]["query"], query)
            self.assertIn("scrapeOptions", request.call_args.kwargs["payload"])
            self.assertEqual(result["results"][0]["content"], content)

    def test_discovery_without_content_and_news_keep_original_query(self):
        for content, categories in ((False, []), (True, ["news"])):
            with self.subTest(content=content, categories=categories), patch.object(self.client, "_request_json", return_value={"data": {}}) as request:
                self.firecrawl(include_content=content, categories=categories)
            self.assertEqual(request.call_args.kwargs["payload"]["query"], "API background mode")

    def test_modes_and_strategies_do_not_override_content_opt_out(self):
        for mode in ("docs", "github", "pdf", "research", "news"):
            for strategy in ("fast", "verify", "deep"):
                with self.subTest(mode=mode, strategy=strategy), patch.object(
                    self.client, "_search_firecrawl", return_value={"results": []},
                ) as search:
                    self.client._dispatch_single_provider(
                        provider_name="firecrawl", query="API guide", max_results=3,
                        mode=mode, intent="tutorial", decision=RouteDecision(provider="firecrawl", reason="test"),
                        include_answer=False, include_content=False, include_domains=None,
                        exclude_domains=None, strategy=strategy,
                    )
                self.assertFalse(search.call_args.kwargs["include_content"])

    def test_snippet_fallback_is_bounded_but_full_body_is_not(self):
        body = "# Guide\n\n" + "Complete body. " * 3000 + "END OF BODY"
        response = {"data": {"web": [{"url": "https://docs.test/guide", "markdown": body}]}}
        for include_content in (False, True):
            with patch.object(self.client, "_request_json", return_value=response):
                item = self.firecrawl(include_content=include_content)["results"][0]
            self.assertLessEqual(len(item["snippet"]), 323)
            self.assertEqual(item["content"], body if include_content else "")

    def test_native_markdown_is_confirmed_by_mime_and_reuses_extract_cache(self):
        body = "# API\n\n| Key | Value |\n| --- | --- |\n| a | b |\n\n```python\nprint('full')\n```\n" + "Details\n" * 10000 + "FINAL"
        url = "https://docs.firecrawl.dev/features/scrape"
        response = httpx.Response(200, text=body, headers={"Content-Type": "text/markdown; charset=utf-8"}, request=httpx.Request("GET", url + ".md"))
        with patch.object(self.client._http, "get", return_value=response) as fetch, patch.object(
            self.client, "_scrape_firecrawl",
        ) as scrape:
            first = self.client.extract_url(url=url)
            second = self.client.extract_url(url=url)
        self.assertEqual(first["content"], body)
        self.assertEqual(first["provider"], "native_markdown")
        self.assertTrue(second["cache"]["extract"]["hit"])
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[0], url + ".md")
        self.assertFalse(fetch.call_args.kwargs["follow_redirects"])
        scrape.assert_not_called()

    def test_bad_prefetched_page_cannot_be_a_snippet_and_recovery_replaces_status(self):
        response = {"data": {"web": [{"url": "https://docs.test/guide", "markdown": "Missing page",
                                     "metadata": {"statusCode": 404}}]}}
        recovered = {"provider": "native_markdown", "content": "Complete new body", "metadata": {"statusCode": 200}}
        with patch.object(self.client, "_request_json", return_value=response), patch.object(
            self.client, "extract_url", return_value=recovered,
        ) as fetch:
            item = self.firecrawl(include_content=False)["results"][0]
            fetch.assert_not_called()
            self.assertEqual(item["snippet"], "")
            self.assertEqual(item["content_fetch"], {"status": "failed", "error": "target HTTP 404"})
            item = self.firecrawl(include_content=True)["results"][0]
        self.assertEqual(item["content"], recovered["content"])
        self.assertEqual(item["metadata"], recovered["metadata"])
        self.assertIsNone(self.client._extract_quality_issue(item))

    def test_social_derived_snippet_is_bounded_without_truncating_full(self):
        from mysearch.research.social import _normalize_social_gateway_response
        body = "Post body " * 5000 + "FINAL"
        result = _normalize_social_gateway_response(
            response={"results": [{"url": "https://x.com/example/status/123", "text": body}]},
            query="example", transport="test",
        )
        self.assertEqual(result["results"][0]["content"], body)
        self.assertLessEqual(len(result["results"][0]["snippet"]), 323)

    def test_native_export_failure_or_html_falls_back_without_false_markdown(self):
        for index, (status, mime, body) in enumerate(((404, "text/markdown", "Missing"), (200, "text/html", "<html>app</html>"), (200, "text/markdown", "<!doctype html><html>app</html>"))):
            url = f"https://docs.tavily.com/documentation/test?case={index}"
            response = httpx.Response(status, text=body, headers={"Content-Type": mime}, request=httpx.Request("GET", url))
            with self.subTest(status=status, mime=mime), patch.object(self.client._http, "get", return_value=response), patch.object(
                self.client, "_scrape_firecrawl", return_value={"provider": "firecrawl", "url": url, "content": "Real body"},
            ) as scrape:
                result = self.client.extract_url(url=url)
            self.assertEqual(result["content"], "Real body")
            scrape.assert_called_once()

    def test_unconfirmed_origins_and_explicit_provider_do_not_get_native_probe(self):
        for url, provider in (("https://docs.example.test/guide", "auto"), ("https://docs.firecrawl.dev/features/scrape", "firecrawl")):
            with patch.object(self.client._http, "get") as fetch, patch.object(self.client, "_scrape_firecrawl", return_value={"provider": "firecrawl", "url": url, "content": "Body"}):
                self.client.extract_url(url=url, provider=provider)
            fetch.assert_not_called()

    def test_summary_never_starts_a_missing_page_fetch(self):
        with patch.object(self.client, "extract_url") as fetch:
            self.assertEqual(self.client._github_release_summary_excerpt({"url": "https://github.com/example/sdk/releases", "snippet": "Releases"}), "")
        fetch.assert_not_called()

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

    def test_auto_reuses_exact_firecrawl_body_without_refreshing_its_ttl(self):
        url = "https://docs.example.test/page"
        body = "Full cached page\n" * 10000
        with patch("mysearch.cache.time.monotonic", return_value=100), patch.object(
            self.client, "_scrape_firecrawl", return_value={"provider": "firecrawl", "url": url, "content": body, "metadata": {}}
        ):
            self.client.extract_url(url=url, provider="firecrawl")
        with patch("mysearch.cache.time.monotonic", return_value=399), patch.object(self.client, "_scrape_firecrawl") as fetch:
            result = self.client.extract_url(url=url)
        fetch.assert_not_called()
        self.assertEqual(result["content"], body)
        with patch("mysearch.cache.time.monotonic", return_value=401), patch.object(
            self.client, "_scrape_firecrawl", return_value={"provider": "firecrawl", "url": url, "content": body + "New revision", "metadata": {}}
        ) as fetch:
            renewed = self.client.extract_url(url=url)
        fetch.assert_called_once()
        self.assertTrue(renewed["content"].endswith("New revision"))

    def test_other_extract_parameters_do_not_reuse_a_different_projection(self):
        url = "https://docs.example.test/page"
        result = {"provider": "firecrawl", "url": url, "content": "Complete page body\n" * 100, "metadata": {}}
        with patch.object(self.client, "_scrape_firecrawl", return_value=result) as fetch:
            self.client.extract_url(url=url, provider="firecrawl")
            self.client.extract_url(url=url, only_main_content=False)
            self.client.extract_url(url=url, formats=["json"])
        self.assertEqual(fetch.call_count, 3)

    def test_pricing_content_requests_live_body_instead_of_cached_reference(self):
        url = "https://docs.example.test/pricing"
        response = {"data": {"web": [{"url": url, "description": "Pricing page"}]}}
        with patch.object(self.client, "_request_json", return_value=response), patch.object(
            self.client, "extract_url", return_value={"provider": "firecrawl", "url": url, "content": "Current pricing body\n" * 100, "metadata": {}}
        ) as fetch:
            self.firecrawl("Current service pricing")
        self.assertEqual(fetch.call_args.kwargs["max_age_seconds"], 0)

    def test_pricing_prefetched_body_is_not_mistaken_for_a_live_fetch(self):
        url = "https://docs.example.test/pricing"
        response = {"data": {"web": [{"url": url, "markdown": "Stale indexed price"}]}}
        with patch.object(self.client, "_request_json", return_value=response), patch.object(
            self.client, "extract_url", return_value={"provider": "firecrawl", "url": url, "content": "Current price\n" * 100, "metadata": {}}
        ) as fetch:
            result = self.firecrawl("Current service pricing")
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.kwargs["max_age_seconds"], 0)
        self.assertTrue(result["results"][0]["content"].startswith("Current price"))

    def test_exclusions_survive_native_domain_fallback(self):
        exclusion = {"url": "https://docs.example.test/llms-full.txt", "reason": "aggregate-document", "fetch_status": "not-attempted"}
        with patch.object(self.client, "_search_firecrawl_once", side_effect=[
            {"results": [], "route_debug": {"excluded_urls": [exclusion]}},
            {"results": [{"url": "https://docs.example.test/guide", "content": "Full guide"}]},
        ]):
            result = self.client._search_firecrawl(query="Guide", max_results=3, categories=[],
                include_content=True, include_domains=["docs.example.test"], exclude_domains=[])
        self.assertEqual(result["route_debug"]["excluded_urls"], [exclusion])
