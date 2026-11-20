from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from mysearch.clients import MySearchClient, MySearchError, RouteDecision
from test_provider_disabled import fixture_config


def search_result(provider="tavily", count=3, content=""):
    results = [
        {"provider": provider, "title": "Search architecture", "url": f"https://source{index}.test/article", "snippet": "Search architecture overview", "content": content}
        for index in range(count)
    ]
    return {"provider": provider, "results": results, "citations": [{"title": item["title"], "url": item["url"]} for item in results], "answer": "Search architecture overview"}


class ThreeProviderCooperationTests(unittest.TestCase):
    def setUp(self):
        self.client = MySearchClient(fixture_config())
        self.addCleanup(self.client.close)
        self.enterContext(patch.object(self.client, "_provider_live_status", return_value="ok"))

    def blend(self, strategy="balanced", include_content=False):
        return self.client._search_web_blended(
            query="search architecture", mode="web", intent="factual", strategy=strategy,
            decision=RouteDecision(provider="tavily", reason="fixture"), max_results=3,
            include_content=include_content, include_answer=False,
            include_domains=None, exclude_domains=None,
        )

    def test_balanced_strong_primary_does_not_call_secondary(self):
        with patch.object(self.client, "_search_tavily", return_value=search_result()) as primary, patch.object(self.client, "_search_firecrawl") as secondary:
            result = self.blend()
        primary.assert_called_once()
        secondary.assert_not_called()
        self.assertEqual(result["evidence"]["verification"], "single-provider")

    def test_balanced_sparse_or_missing_content_calls_secondary_once(self):
        for count, content_requested in ((1, False), (3, True)):
            with self.subTest(count=count, content_requested=content_requested), patch.object(self.client, "_search_tavily", return_value=search_result(count=count)) as primary, patch.object(self.client, "_search_firecrawl", return_value=search_result("firecrawl", content="Full page body")) as secondary:
                result = self.blend(include_content=content_requested)
            primary.assert_called_once()
            secondary.assert_called_once()
            self.assertEqual(result["evidence"]["verification"], "cross-provider")
            self.assertEqual(result["route_selected"], "tavily+firecrawl")
            if content_requested:
                self.assertTrue(any(item["content"] for item in result["results"]))

    def test_balanced_primary_failure_uses_secondary_and_keeps_error(self):
        with patch.object(self.client, "_search_tavily", side_effect=MySearchError("fixture failure")), patch.object(self.client, "_search_firecrawl", return_value=search_result("firecrawl")):
            result = self.blend()
        self.assertEqual(result["primary_search"]["fallback"]["reason"], "fixture failure")
        self.assertEqual(result["provider"], "firecrawl")

    def test_verify_and_deep_keep_cross_provider_search_without_exa(self):
        for strategy in ("verify", "deep"):
            with self.subTest(strategy=strategy), patch.object(self.client, "_search_tavily", return_value=search_result()) as primary, patch.object(self.client, "_search_firecrawl", return_value=search_result("firecrawl")) as secondary, patch.object(self.client, "_search_exa") as exa:
                result = self.blend(strategy)
            primary.assert_called_once()
            secondary.assert_called_once()
            exa.assert_not_called()
            self.assertEqual(result["evidence"]["providers_consulted"], ["tavily", "firecrawl"])

    def test_public_github_mode_uses_developer_category_and_domain_filter(self):
        response = {"data": {"web": [{"title": "Repo", "url": "https://github.com/fixture/repo", "markdown": "Repository README"}]}}
        with patch.object(self.client, "_request_json", return_value=response) as request, patch.object(self.client, "_search_exa") as exa:
            result = self.client.search(query="fixture repo", mode="github", strategy="fast", include_content=True)
        self.assertEqual(request.call_args.kwargs["payload"]["includeDomains"], ["github.com"])
        self.assertEqual(request.call_args.kwargs["payload"]["categories"], [{"type": "developer"}])
        self.assertEqual(result["results"][0]["content"], "Repository README")
        exa.assert_not_called()

    def test_category_mapping_preserves_academic_search_and_rejects_invalid_mix(self):
        self.assertEqual(self.client._normalize_firecrawl_search_categories(["github", "developer"]), ["developer"])
        self.assertEqual(self.client._normalize_firecrawl_search_categories(["research", "pdf"]), ["research", "pdf"])
        with self.assertRaisesRegex(MySearchError, "cannot be combined"):
            self.client._normalize_firecrawl_search_categories(["developer", "pdf"])
        self.assertEqual(self.client._firecrawl_categories("docs", "resource"), ["developer"])
        self.assertEqual(self.client._firecrawl_categories("research", "exploratory"), ["research"])
        policy = self.client._route_policy_for_request(query="product release notes", mode="docs", intent="resource", include_content=False)
        self.assertNotIn("research", policy.firecrawl_categories)

    def test_extract_failure_does_not_attempt_disabled_exa(self):
        with patch.object(self.client, "_scrape_firecrawl", side_effect=MySearchError("fixture scrape failure")), patch.object(self.client, "_extract_tavily", side_effect=MySearchError("fixture extract failure")), patch.object(self.client, "_exa_contents") as exa:
            with self.assertRaisesRegex(MySearchError, "fixture extract failure"):
                self.client.extract_url(url="https://fixture.test/article")
        exa.assert_not_called()

    def test_social_fallback_skips_disabled_exa_and_reports_unavailable(self):
        self.client.config.xai.search_mode = "compatible"
        with patch.object(self.client, "_request_json", return_value={}), patch.object(self.client, "_search_firecrawl_social_fallback", return_value={"results": []}), patch.object(self.client, "_search_tavily_social_fallback", return_value={"results": []}), patch.object(self.client, "_search_exa_social_fallback") as exa:
            result = self.client.search(query="fixture discussion", mode="social")
        exa.assert_not_called()
        self.assertTrue(self.client._is_social_unavailable_result(result))
        self.assertNotIn("exa_social_fallback", str(result.get("fallback")))

    def test_deep_research_has_no_exa_or_duplicate_tavily_support_task(self):
        task_names = []
        execute = self.client._execute_parallel
        def record(tasks, **kwargs):
            task_names.extend(tasks)
            return execute(tasks, **kwargs)
        with patch.object(self.client, "_execute_parallel", side_effect=record), patch.object(self.client, "_run_research_web_discovery", return_value=search_result()), patch.object(self.client, "_run_research_docs_rescue", return_value={"results": []}), patch.object(self.client, "extract_url", return_value={"content": "Full fixture content"}), patch.object(self.client, "_search_exa", Mock(side_effect=AssertionError("Exa was attempted"))):
            result = self.client.research(query="search architecture", intent="exploratory", strategy="deep", include_social=False)
        self.assertNotIn("exa_discovery", task_names)
        self.assertNotIn("tavily_support", task_names)
        self.assertEqual(result["evidence"]["exa_discovery_count"], 0)
        self.assertTrue(result["pages"])
