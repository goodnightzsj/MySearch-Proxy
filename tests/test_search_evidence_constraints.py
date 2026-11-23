from __future__ import annotations

import unittest
from unittest.mock import patch

from mysearch import postprocess, query_routing
from mysearch.clients import MySearchClient
from mysearch.diagnostics import result_outcome
from mysearch.research.finalize import source_evidence
from test_provider_disabled import fixture_config


class SearchEvidenceConstraintsTests(unittest.TestCase):
    def setUp(self):
        self.client = MySearchClient(fixture_config())
        self.addCleanup(self.client.close)

    def test_query_hints_preserve_subject_negative_and_temporal_qualifiers(self):
        for query in (
            'Who won the 2038 Abel Prize? Verify whether it has been awarded, exclude predictions.',
            '1987 Nobel Prize in computing winner, did this category exist?',
            '2028 BAFTA best film winner -nominees site:bafta.org before:2028-03-01',
            'Which team won the 2024 regional contest, not the national championship?',
        ):
            refined = query_routing._refined_award_result_query(query)
            self.assertTrue(refined.startswith(query), refined)
            self.assertEqual(query_routing._refined_award_result_query(refined), refined)

    def test_refinement_preserves_explicit_date_and_excluded_domains(self):
        self.client.config.firecrawl.enabled = False
        with patch.object(self.client, "_request_json", return_value={"results": []}) as request:
            self.client._maybe_refine_tavily_result_event_discovery(
                query="2028 BAFTA film winner", mode="news", intent="news",
                result={"provider": "tavily", "results": []}, max_results=3,
                include_domains=None, exclude_domains=["excluded.test"],
                from_date="2028-01-01", to_date="2028-03-01",
            )
        self.assertTrue(request.called)
        for call in request.call_args_list:
            payload = call.kwargs["payload"]
            self.assertIn("2028 BAFTA film winner", payload["query"])
            self.assertEqual(payload["start_date"], "2028-01-01")
            self.assertEqual(payload["end_date"], "2028-03-01")
            self.assertEqual(payload["exclude_domains"], ["excluded.test"])

    def test_page_http_errors_fall_back_without_caching_or_invalidating_keys(self):
        page = {"provider": "firecrawl", "url": "https://example.test/page", "content": "Contact our support team for help."}
        recovered = {"provider": "tavily", "url": page["url"], "content": "Actual document body"}
        for status in (403, 404, 410, 429, 500, "404"):
            with self.subTest(status=status):
                page["metadata"] = {"statusCode": status}
                with patch.object(self.client, "_scrape_firecrawl", return_value=page) as scrape:
                    for _ in range(2):
                        result = self.client.extract_url(url=page["url"], provider="firecrawl")
                        self.assertIn("target HTTP", result["warning"])
                        self.assertFalse(result["cache"]["extract"]["hit"])
                    self.assertEqual(scrape.call_count, 2)
                with patch.object(self.client, "_scrape_firecrawl", return_value=page), patch.object(
                    self.client, "_extract_tavily", return_value=recovered,
                ):
                    # Separate URLs keep each fallback case independent of the successful cache.
                    result = self.client.extract_url(url=f"{page['url']}?status={status}")
                    self.assertEqual(result["content"], recovered["content"])
                    self.assertIn("target HTTP", result["fallback"]["reason"])
                self.assertTrue(self.client.keyring.has_provider("firecrawl"))

    def test_success_statuses_and_crawl_error_pages_use_same_quality_rule(self):
        for status in (200, 201, 204, 299, 304, "200", None):
            self.assertIsNone(postprocess._extract_quality_issue({
                "content": "Actual body", "metadata": {"statusCode": status},
            }))
        result = query_routing._build_firecrawl_crawl_result(
            url="https://example.test", limit=2, transport="env", status_payload={
                "status": "completed", "data": [
                    {"markdown": "Resource unavailable", "metadata": {"statusCode": 404, "sourceURL": "https://example.test/missing"}},
                    {"markdown": "Complete body", "metadata": {"statusCode": 200, "sourceURL": "https://example.test/ok"}},
                ],
            },
        )
        self.assertEqual(result["pages"][0]["content"], "")
        self.assertEqual(result["pages"][0]["warning"], "target HTTP 404")
        self.assertEqual(result["pages"][1]["content"], "Complete body")

    def test_multiple_domains_do_not_verify_a_claim(self):
        result = {"answer": "", "results": [{"title": "Unrelated announcement", "url": "https://example.test"}],
                  "evidence": {"verification": "cross-provider", "source_diversity": 3, "claim_verification": "not-assessed"}}
        summary = self.client._build_search_summary_fallback(query="Unverified premise", mode="web", intent="factual", result=result)
        self.assertNotIn("corroborated", summary)
        self.assertIn("answer not verified", summary)
        self.assertEqual(result["answer"], "")

    def test_error_page_is_not_successful_extracted_evidence(self):
        page = {"url": "https://example.test/missing", "provider": "firecrawl", "content": "Resource unavailable",
                "metadata": {"statusCode": 404}, "warning": "target HTTP 404"}
        evidence = source_evidence(results=[], pages=[page], citations=[])[0]
        self.assertEqual(evidence["text_status"], "missing")
        self.assertEqual(evidence["fetch_status"], "failed")
        self.assertEqual(evidence["providers"], [])
        self.assertEqual(result_outcome("extract_url", page), "degraded")
