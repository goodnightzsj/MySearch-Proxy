import time
import unittest
from unittest.mock import Mock, patch

from mysearch.clients import MySearchClient
from mysearch.diagnostics import RequestState, current_deadline, remaining_seconds, request_context, result_outcome
from mysearch.errors import MySearchError
from tests.test_request_diagnostics import make_config


class AwardPageBudgetTests(unittest.TestCase):
    query = "2026 Grammy Record of the Year winner"

    def setUp(self):
        self.client = MySearchClient(config=make_config(), keyring=Mock())
        self.addCleanup(self.client.close)
        self.items = [{"title": "The complete list of 2026 Grammy winners and nominees", "url": f"https://example.com/{i}"} for i in range(5)]

    def extract(self, **kwargs):
        return self.client._extract_result_event_answer_from_top_page(
            query=self.query, results=self.items, **kwargs)

    def test_repeated_override_does_not_refetch_a_page(self):
        cache = {}
        with patch.object(self.client, "extract_url", return_value={"content": ""}) as fetch:
            self.assertEqual(self.extract(page_answers=cache), "")
            self.assertEqual(self.extract(page_answers=cache), "")
        self.assertEqual(fetch.call_count, 5)

    def test_cached_answer_is_used_without_network(self):
        cache = {}
        with patch.object(self.client, "extract_url", return_value={
            "content": 'Record of the Year: "Not Like Us", Kendrick Lamar'}) as fetch:
            first = self.extract(page_answers=cache)
            self.assertEqual(self.extract(page_answers=cache), first)
        self.assertIn("Not Like Us", first)
        self.assertEqual(fetch.call_count, 1)

    def test_prefetched_page_is_read_before_any_network(self):
        self.items[0]["content"] = 'Record of the Year: "Not Like Us", Kendrick Lamar'
        with patch.object(self.client, "extract_url") as fetch, patch.object(
            self.client, "_extract_result_event_answer_from_official_page_html", return_value=""
        ) as html:
            answer = self.extract(page_answers={})
        self.assertIn("Not Like Us", answer)
        fetch.assert_not_called()
        html.assert_not_called()

    def test_release_body_is_not_hidden_by_nonempty_snippet(self):
        with patch.object(self.client, "extract_url") as fetch:
            answer = self.client._github_release_summary_excerpt({
                "url": "https://github.com/example/sdk/releases",
                "snippet": "SDK releases and changelog",
                "content": "## 7.2.1 (2026-11-19)\nFixes\n## 7.2.0 (2026-11-18)",
            })
        self.assertEqual(answer, "Latest release 7.2.1 (2026-11-19)")
        fetch.assert_not_called()

    def test_rescue_can_read_new_pages_within_same_budget(self):
        cache = {}
        with patch.object(self.client, "extract_url", return_value={"content": ""}) as fetch:
            self.extract(page_answers=cache)
            self.items = [{**self.items[0], "url": "https://example.com/rescue"}]
            self.extract(page_answers=cache)
        self.assertEqual(fetch.call_count, 6)

    def test_optional_deadline_stops_calls_and_restores_parent(self):
        clock = [100.0]
        state = RequestState(deadline=145, tool="search", claimed=True)
        def fetch(**kwargs):
            self.assertLessEqual(remaining_seconds(), 5)
            clock[0] = 106
            raise MySearchError("page timeout")
        with patch("mysearch.diagnostics.time.monotonic", side_effect=lambda: clock[0]):
            with request_context(state), patch.object(self.client, "extract_url", side_effect=fetch) as http:
                self.assertEqual(self.extract(page_deadline=105), "")
                self.assertIsNone(current_deadline.get())
                self.assertEqual(remaining_seconds(), 39)
                self.assertFalse(state.cancelled.is_set())
        self.assertEqual(http.call_count, 1)

    def test_root_cancellation_is_not_swallowed(self):
        state = RequestState(deadline=time.monotonic() + 45, tool="search", claimed=True)
        def fetch(**kwargs):
            state.cancel()
            raise MySearchError("cancelled")
        with request_context(state), patch.object(self.client, "extract_url", side_effect=fetch):
            with self.assertRaises(MySearchError):
                self.extract()
        self.assertIsNone(current_deadline.get())

    def test_optional_deadline_race_returns_partial_without_cancelling_root(self):
        clock = [100.0]
        state = RequestState(deadline=145, tool="search", claimed=True)
        def expired(**kwargs):
            clock[0] = 106
            remaining_seconds()
        with patch("mysearch.diagnostics.time.monotonic", side_effect=lambda: clock[0]):
            with request_context(state), patch.object(self.client, "_read_result_event_pages", side_effect=expired):
                self.assertEqual(self.extract(page_deadline=105), "")
                self.assertEqual(remaining_seconds(), 39)
                self.assertFalse(state.cancelled.is_set())

    def test_exhausted_evidence_is_explicit_and_degraded(self):
        with patch.object(self.client, "extract_url") as fetch:
            result = self.client._apply_result_event_answer_override(
                query=self.query, mode="news", intent="news", strategy="verify",
                result={"answer": "", "results": self.items}, page_deadline=time.monotonic() - 1)
        fetch.assert_not_called()
        self.assertTrue(result["evidence"]["result_event_page_budget_exhausted"])
        self.assertEqual(result_outcome("search", result), "degraded")
