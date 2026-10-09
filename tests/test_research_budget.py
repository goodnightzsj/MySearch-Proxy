from __future__ import annotations

import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

from mysearch.errors import MySearchError
from mysearch.providers.base import ProviderTransport, budgeted_research, wait_before_retry


class ProbeTransport(ProviderTransport):
    def __init__(self, timeout=0.3):
        self.config = SimpleNamespace(timeout_seconds=timeout, max_parallel_workers=2)
        self._executor = ThreadPoolExecutor(max_workers=2)
        self._http = Mock()
        self._http.get.return_value = SimpleNamespace(status_code=200, text="ok")

    @budgeted_research
    def research(self, action):
        return action()


class ResearchBudgetTests(unittest.TestCase):
    def client(self, timeout=0.3):
        client = ProbeTransport(timeout)
        self.addCleanup(client._executor.shutdown, wait=True)
        return client

    def test_single_parallel_task_obeys_explicit_timeout(self):
        client = self.client()
        release = Event()
        try:
            started = time.monotonic()
            results, errors = client._execute_parallel(
                {"slow": lambda: release.wait(0.4)}, timeout_seconds=0.02, max_workers=1
            )
            self.assertLess(time.monotonic() - started, 0.25)
            self.assertFalse(results)
            self.assertIn("slow", errors)
        finally:
            release.set()

    def test_nested_branches_receive_the_remaining_request_budget(self):
        client = self.client()

        def action():
            time.sleep(0.04)
            result, errors = client._execute_parallel({
                "a": lambda: client._request_text(url="https://example.test/a"),
                "b": lambda: client._request_text(url="https://example.test/b"),
            }, max_workers=2)
            self.assertFalse(errors)
            return result

        self.assertEqual(set(client.research(action)), {"a", "b"})
        for call in client._http.get.call_args_list:
            self.assertGreater(call.kwargs["timeout"], 0)
            self.assertLess(call.kwargs["timeout"], 0.28)

    def test_expired_worker_cannot_start_another_http_request(self):
        client = self.client(0.02)
        release, done = Event(), Event()

        def action():
            release.wait(0.4)
            try:
                client._request_text(url="https://example.test/late")
            finally:
                done.set()

        try:
            with self.assertRaises(MySearchError):
                client.research(action)
        finally:
            release.set()
        self.assertTrue(done.wait(0.5))
        client._http.get.assert_not_called()

    def test_retry_wait_cannot_renew_the_request_budget(self):
        client = self.client(0.1)
        started = time.monotonic()
        with self.assertRaisesRegex(MySearchError, "budget"):
            client.research(lambda: wait_before_retry(1.5))
        self.assertLess(time.monotonic() - started, 0.3)

    def test_json_request_uses_remaining_budget(self):
        client = self.client()
        provider = SimpleNamespace(
            name="firecrawl", base_url="https://example.test", auth_mode="bearer",
            auth_scheme="Bearer", auth_header="Authorization",
        )
        client._http.request.return_value = SimpleNamespace(status_code=200, headers={}, text='{"ok": true}')

        def action():
            time.sleep(0.04)
            return client._request_json_once(
                provider=provider, method="POST", path="/search", payload={}, key="test-key",
            )

        self.assertEqual(client.research(action), {"ok": True})
        self.assertGreater(client._http.request.call_args.kwargs["timeout"], 0)
        self.assertLess(client._http.request.call_args.kwargs["timeout"], 0.28)

    def test_returning_request_interrupts_retry_wait_in_late_branch(self):
        client = self.client()
        entered, done = Event(), Event()

        def late_retry():
            entered.set()
            try:
                wait_before_retry(0.2)
            finally:
                done.set()

        def action():
            client._execute_parallel({"late": late_retry}, max_workers=1, timeout_seconds=0.03)
            return "partial"

        self.assertEqual(client.research(action), "partial")
        self.assertTrue(entered.is_set())
        self.assertTrue(done.wait(0.1))

    def test_independent_requests_and_later_calls_do_not_share_cancellation(self):
        client = self.client()
        entered = Event()

        def slower():
            entered.set()
            time.sleep(0.05)
            return client._request_text(url="https://example.test/slow")

        with ThreadPoolExecutor(max_workers=2) as callers:
            slow = callers.submit(client.research, slower)
            self.assertTrue(entered.wait(0.5))
            self.assertEqual(client.research(lambda: "fast"), "fast")
            self.assertEqual(slow.result(timeout=1), (200, "ok"))
        client._request_text(url="https://example.test/outside")
        self.assertEqual(client._http.get.call_args.kwargs["timeout"], 0.3)
