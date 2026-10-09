from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from mysearch.clients import MySearchClient
from mysearch.errors import MySearchError
from mysearch.research import finalize


class SourceEvidenceTests(unittest.TestCase):
    def test_search_reports_text_and_provider_agreement_without_claim_verification(self):
        result = finalize._augment_evidence_summary(
            {"provider": "hybrid", "results": [
                {"url": "https://example.test/a", "title": "A", "snippet": "A fact",
                 "matched_providers": ["tavily", "exa"]},
                {"url": "https://example.test/b", "title": "B"},
            ], "citations": [{"url": "https://example.test/a"}]},
            query="a fact", mode="web", intent="factual", include_domains=None,
        )
        sources = result["evidence"]["sources"]
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0]["text_status"], "retrieved")
        self.assertEqual(sources[0]["provider_count"], 2)
        self.assertEqual(sources[0]["claim_verification"], "not-assessed")
        self.assertEqual(sources[1]["text_status"], "missing")
        self.assertEqual(sources[1]["fetch_status"], "not-attempted")

    def test_extraction_failure_does_not_erase_discovery_text_or_claim_success(self):
        sources = finalize.source_evidence(
            results=[{"url": "https://example.test/a", "snippet": "A fact", "provider": "tavily"}],
            pages=[
                {"url": "https://example.test/a", "error": "timeout"},
                {"url": "https://example.test/b", "content": "Extracted body", "provider": "firecrawl"},
                {"url": "https://example.test/c", "content": "Prefetched body", "provider": "discovery_prefetch"},
                {"url": "https://example.test/d", "content": ""},
            ], citations=[],
        )
        self.assertEqual([(s["text_status"], s["fetch_status"]) for s in sources], [
            ("retrieved", "failed"), ("extracted", "succeeded"),
            ("extracted", "prefetched"), ("missing", "empty"),
        ])
        self.assertTrue(all(s["claim_verification"] == "not-assessed" for s in sources))

    def test_catalog_descriptions_and_failed_fetches_are_not_provider_agreement(self):
        sources = finalize.source_evidence(
            results=[
                {"url": "https://example.test/catalog", "provider": "canonical_research_projects",
                 "snippet": "A built-in catalog description"},
                {"url": "https://example.test/a", "provider": "tavily", "snippet": "Retrieved text"},
            ],
            pages=[{"url": "https://example.test/a", "provider": "firecrawl", "error": "timeout"}],
            citations=[{"url": "https://example.test/a", "provider": "hybrid"}],
        )
        self.assertEqual(sources[0]["text_status"], "missing")
        self.assertEqual(sources[0]["provider_count"], 0)
        self.assertEqual(sources[1]["providers"], ["tavily"])
        self.assertEqual(sources[1]["fetch_status"], "failed")


class BoundedRescueTests(unittest.TestCase):
    def setUp(self):
        self.client = MySearchClient()
        self.addCleanup(self.client.close)
        self.client._research_authoritative_rescue_queries = Mock(
            return_value=["original", "variant one", "variant two", "variant three"]
        )
        self.client.search = Mock(return_value={"provider": "tavily", "results": [], "citations": []})

    def rescue(self):
        return self.client._run_research_docs_rescue(
            query="test topic", strategy="deep", max_results=2,
            include_domains=["example.test"], exclude_domains=["excluded.test"],
            from_date="2026-01-01", to_date="2026-10-09",
        )

    def test_stops_when_requested_text_evidence_is_present(self):
        self.client.search.return_value = {
            "provider": "tavily", "results": [
                {"url": f"https://example.test/{index}", "snippet": "Useful source text"}
                for index in range(2)
            ], "citations": [],
        }
        result = self.rescue()
        self.assertEqual(self.client.search.call_count, 1)
        self.assertEqual(result["evidence"]["supplemental_search"]["stop_reason"], "sufficient-evidence")

    def test_three_query_cap_preserves_every_filter(self):
        result = self.rescue()
        self.assertEqual(self.client.search.call_count, 3)
        self.assertEqual(result["evidence"]["supplemental_search"]["stop_reason"], "query-limit")
        for call in self.client.search.call_args_list:
            self.assertEqual(call.kwargs["include_domains"], ["example.test"])
            self.assertEqual(call.kwargs["exclude_domains"], ["excluded.test"])
            self.assertEqual(call.kwargs["from_date"], "2026-01-01")
            self.assertEqual(call.kwargs["to_date"], "2026-10-09")

    def test_exhausted_budget_does_not_start_supplemental_search(self):
        with patch("mysearch.clients.remaining_request_seconds", side_effect=MySearchError("budget exhausted")):
            result = self.rescue()
        self.client.search.assert_not_called()
        self.assertEqual(result["evidence"]["supplemental_search"]["stop_reason"], "budget-exhausted")

    def test_comparison_requires_text_for_each_subject_before_stopping(self):
        self.client.search.side_effect = [
            {"provider": "tavily", "results": [
                {"url": f"https://example.test/tavily/{i}", "snippet": "Tavily source text"}
                for i in range(2)
            ], "citations": []},
            {"provider": "tavily", "results": [
                {"url": "https://example.test/firecrawl", "snippet": "Firecrawl source text"}
            ], "citations": []},
        ]
        result = self.client._run_research_docs_rescue(
            query="compare Tavily and Firecrawl", strategy="deep", max_results=2,
            include_domains=["example.test"], exclude_domains=None,
        )
        self.assertEqual(self.client.search.call_count, 2)
        self.assertEqual(result["evidence"]["supplemental_search"]["stop_reason"], "sufficient-evidence")

    def test_deep_research_only_supplements_a_seed_evidence_gap(self):
        candidates = [{"provider": "tavily", "url": f"https://example.test/{i}",
                       "title": f"Topic {i}", "snippet": "Useful source evidence.",
                       "content": "Useful source evidence. " * 20} for i in range(4)]
        strong = {"provider": "tavily", "results": candidates, "citations": []}
        self.client._provider_can_serve = lambda provider: False
        for weak_seed in (False, True):
            with self.subTest(weak_seed=weak_seed):
                self.client.search.reset_mock(side_effect=True)
                self.client.search.side_effect = [
                    {"provider": "tavily", "results": [], "citations": []} if weak_seed else strong,
                    strong,
                ]
                result = self.client.research(
                    query="test topic", mode="docs", strategy="deep", include_social=False,
                    web_max_results=4, include_domains=["example.test"],
                )
                self.assertEqual(self.client.search.call_count, 2 if weak_seed else 1)
                self.assertEqual(result["evidence"]["supplemental_search"]["stop_reason"], "sufficient-evidence")
                self.assertIn("request_budget", result["evidence"])
                self.assertTrue(result["evidence"]["sources"])
