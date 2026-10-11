import os
import unittest
from contextlib import closing
from unittest.mock import patch

from mysearch.clients import MySearchClient, RouteDecision


class SearchAnswerProbeTests(unittest.TestCase):
    def test_answer_probe_only_runs_for_eligible_supplements(self):
        for mode, strategy, intent, expected_calls in (
            ("official", "fast", "factual", 0),
            ("compatible", "fast", "factual", 0),
            ("compatible", "verify", "resource", 0),
            ("official", "verify", "resource", 1),
        ):
            with self.subTest(mode=mode, strategy=strategy, intent=intent), \
                 patch.dict(os.environ, {"MYSEARCH_XAI_API_KEY": "test-key",
                                         "MYSEARCH_XAI_SEARCH_MODE": mode}, clear=True), \
                 closing(MySearchClient()) as client, \
                 patch.object(client, "_probe_provider_request") as probe, \
                 patch.object(client, "_search_xai", return_value={"answer": "Supported answer"}) as search:
                result = client._postprocess_search(
                    result={"provider": "exa", "answer": "", "results": [
                        {"url": "https://example.com/source", "title": "Source", "snippet": "Source text"}],
                        "citations": []},
                    query="example topic", mode="web", provider="auto", resolved_intent=intent,
                    resolved_strategy=strategy, decision=RouteDecision(provider="exa", reason="test"),
                    normalized_sources=["web"], include_content=False, effective_include_answer=True,
                    include_domains=None, exclude_domains=None, max_results=5, candidate_max_results=5,
                    cacheable=False, cache_key="",
                )
                self.assertEqual(probe.call_count, expected_calls)
                self.assertEqual(search.call_count, expected_calls)
                self.assertEqual(result["answer"], "Supported answer" if expected_calls else "")

    def test_arbitration_probe_only_runs_for_eligible_conflicts(self):
        for mode, providers, official_mode, expected in (
            ("compatible", ["tavily", "exa"], "off", False),
            ("official", ["tavily"], "off", False),
            ("official", ["tavily", "exa"], "strict", False),
            ("official", ["tavily", "exa"], "off", True),
        ):
            with self.subTest(mode=mode, providers=providers, official_mode=official_mode), \
                 patch.dict(os.environ, {"MYSEARCH_XAI_API_KEY": "test-key",
                                         "MYSEARCH_XAI_SEARCH_MODE": mode}, clear=True), \
                 closing(MySearchClient()) as client, \
                 patch.object(client, "_probe_provider_request") as probe:
                eligible = client._should_attempt_xai_arbitration(
                    result={"provider": "hybrid", "evidence": {
                        "providers_consulted": providers, "official_mode": official_mode}},
                    decision=RouteDecision(provider="tavily", reason="test"), strategy="verify",
                    conflicts=["conflicting-version-claims"],
                )
                self.assertEqual(eligible, expected)
                self.assertEqual(probe.call_count, int(expected))


if __name__ == "__main__":
    unittest.main()
