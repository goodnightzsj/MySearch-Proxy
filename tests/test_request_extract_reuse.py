from __future__ import annotations

import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Event
from unittest.mock import patch

from mysearch.clients import MySearchClient
from mysearch.diagnostics import current_deadline, request_context
from mysearch.errors import MySearchError, MySearchHTTPError
from test_provider_disabled import fixture_config


class RequestExtractReuseTests(unittest.TestCase):
    def setUp(self):
        self.client = MySearchClient(fixture_config())
        self.addCleanup(self.client.close)
        self.state = self.client.new_request("research")
        self.state.claimed = True
        self.addCleanup(self.client.finish_request, self.state)
        self.enterContext(request_context(self.state))
        self.url = "https://docs.fixture.test/path?a=1"
        self.body = "Full body\n" * 10000 + "FINAL SECTION"

    def fetch(self, **kwargs):
        return self.client.extract_url(url=kwargs.pop("url", self.url), provider="firecrawl", **kwargs)

    def test_parallel_identical_fresh_scrapes_share_send_and_isolate_results(self):
        started, release = Event(), Event()
        def send(**kwargs):
            started.set()
            self.assertTrue(release.wait(2))
            return {"data": {"markdown": self.body, "metadata": {"creditsUsed": 1}}}
        with patch.object(self.client, "_request_json", side_effect=send) as request, ThreadPoolExecutor(2) as pool:
            a = pool.submit(copy_context().run, self.fetch, max_age_seconds=0)
            self.assertTrue(started.wait(1))
            b = pool.submit(copy_context().run, self.fetch, max_age_seconds=0)
            release.set()
            first, second = a.result(), b.result()
        self.assertEqual(request.call_count, 1)
        first["metadata"]["creditsUsed"] = 999
        self.assertEqual(second["metadata"]["creditsUsed"], 1)
        self.assertEqual(second["content"], self.body)

    def test_identity_retains_query_fragment_formats_scope_and_freshness(self):
        with patch.object(self.client, "_request_json", return_value={"data": {"markdown": self.body}}) as request:
            for args in ({}, {"url": self.url + "#route"}, {"url": self.url.replace("a=1", "a=2")},
                         {"formats": ["json"]}, {"only_main_content": False}, {"max_age_seconds": 0},
                         {"max_age_seconds": 10}):
                self.fetch(**args)
        self.assertEqual(request.call_count, 7)

    def test_transient_failure_is_not_memoized_and_retry_stays_with_extraction_owner(self):
        failure = MySearchHTTPError(provider="firecrawl", status_code=503, detail="temporary", url=self.url)
        with patch.object(self.client, "_request_json", side_effect=[failure, {"data": {"markdown": self.body}}]) as request:
            result = self.fetch(max_age_seconds=0)
            reused = self.fetch(max_age_seconds=0)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(result["content"], reused["content"])

    def test_waiting_branch_obeys_its_deadline_without_cancelling_owner(self):
        started, release = Event(), Event()
        def send(**kwargs):
            started.set()
            self.assertTrue(release.wait(2))
            return {"data": {"markdown": self.body}}
        with patch.object(self.client, "_request_json", side_effect=send) as request, ThreadPoolExecutor(1) as pool:
            owner = pool.submit(copy_context().run, self.fetch, max_age_seconds=0)
            self.assertTrue(started.wait(1))
            deadline = current_deadline.set(time.monotonic() + 0.03)
            try:
                with self.assertRaisesRegex(MySearchError, "budget exhausted"):
                    self.fetch(max_age_seconds=0)
            finally:
                current_deadline.reset(deadline)
                release.set()
            self.assertEqual(owner.result()["content"], self.body)
            self.assertFalse(self.state.cancelled.is_set())
        self.assertEqual(request.call_count, 1)

    def test_separate_requests_do_not_reuse_explicit_live_fetch(self):
        with patch.object(self.client, "_request_json", return_value={"data": {"markdown": self.body}}) as request:
            self.fetch(max_age_seconds=0)
            other = self.client.new_request("extract_url")
            try:
                with request_context(other):
                    self.fetch(max_age_seconds=0)
            finally:
                self.client.finish_request(other)
        self.assertEqual(request.call_count, 2)

    def test_primary_recovers_after_short_optional_scrape_owner_expires(self):
        from mysearch.diagnostics import remaining_seconds
        started = Event()
        calls = []

        def send(**kwargs):
            calls.append(remaining_seconds())
            if len(calls) == 1:
                started.set()
                time.sleep(0.05)
                remaining_seconds()
            return {"data": {"markdown": self.body}}

        def optional():
            token = current_deadline.set(time.monotonic() + 0.02)
            try:
                return self.fetch(max_age_seconds=0)
            finally:
                current_deadline.reset(token)

        with patch.object(self.client, "_request_json", side_effect=send), ThreadPoolExecutor(1) as pool:
            owner = pool.submit(copy_context().run, optional)
            self.assertTrue(started.wait(1))
            recovered = self.fetch(max_age_seconds=0)
            with self.assertRaises(MySearchError):
                owner.result()
        self.assertEqual(recovered["content"], self.body)
        self.assertEqual(len(calls), 2)
        self.assertGreater(calls[1], calls[0])
        self.assertFalse(self.state.cancelled.is_set())
