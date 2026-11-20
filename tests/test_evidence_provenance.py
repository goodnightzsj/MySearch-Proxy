from __future__ import annotations

import unittest
from unittest.mock import patch

from mysearch.clients import MySearchClient, MySearchError, RouteDecision
from mysearch.research.finalize import _augment_evidence_summary, source_evidence
from test_provider_disabled import fixture_config


def payload(provider, url="https://docs.example.test/guide"):
    return {"provider": provider, "results": [{"provider": provider, "url": url, "title": "Guide", "content": "Full guide"}], "citations": []}


class EvidenceProvenanceTests(unittest.TestCase):
    def hybrid(self, client):
        return client._search_hybrid(
            query="search architecture", mode="web", resolved_intent="factual",
            resolved_strategy="balanced", decision=RouteDecision(provider="hybrid", reason="fixture"),
            max_results=3, include_content=True, effective_include_answer=False,
            include_domains=None, exclude_domains=None, allowed_x_handles=None,
            excluded_x_handles=None, from_date=None, to_date=None,
            include_x_images=False, include_x_videos=False,
        )

    def test_failed_branch_is_not_a_verifying_provider_and_error_survives_finalize(self):
        client = MySearchClient(fixture_config())
        self.addCleanup(client.close)
        with patch.object(client, "search", return_value=payload("tavily")), patch.object(client, "_search_xai", side_effect=MySearchError("X capability unavailable")):
            result = self.hybrid(client)
        self.assertEqual(result["evidence"]["providers_consulted"], ["tavily"])
        self.assertEqual(result["evidence"]["verification"], "single-provider")
        final = _augment_evidence_summary(result, query="search architecture", mode="web", intent="factual", include_domains=None)
        self.assertIn("social-search-unavailable", final["evidence"]["conflicts"])
        self.assertIn("X capability unavailable", final["evidence"]["social_error"])

    def test_two_branches_of_one_provider_are_not_cross_provider(self):
        client = MySearchClient(fixture_config())
        self.addCleanup(client.close)
        with patch.object(client, "search", return_value=payload("xai")), patch.object(client, "_search_xai", return_value=payload("xai", "https://x.com/fixture/status/1")):
            result = self.hybrid(client)
        self.assertEqual(result["evidence"]["providers_consulted"], ["xai"])
        self.assertEqual(result["evidence"]["verification"], "single-provider")

    def test_one_official_publisher_via_two_providers_is_not_high_confidence(self):
        result = payload("tavily")
        result["results"][0]["matched_providers"] = ["tavily", "firecrawl"]
        result["evidence"] = {"providers_consulted": ["tavily", "firecrawl"], "verification": "cross-provider"}
        final = _augment_evidence_summary(result, query="example documentation", mode="docs", intent="resource", include_domains=["docs.example.test"])
        self.assertEqual(final["evidence"]["source_diversity"], 1)
        self.assertEqual(final["evidence"]["confidence"], "medium")
        source = source_evidence(results=result["results"], pages=[], citations=[])[0]
        self.assertEqual(source["provider_count"], 2)
        self.assertEqual(source["claim_verification"], "not-assessed")


if __name__ == "__main__":
    unittest.main()
