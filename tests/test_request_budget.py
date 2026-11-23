from __future__ import annotations

import time
import unittest
from threading import Event, Thread, get_ident
from unittest.mock import Mock, patch

import httpx

from mysearch.diagnostics import current_request, operation_timeout, remaining_seconds, request_context
from mysearch.errors import MySearchError
from mysearch.providers.base import wait_before_retry
from tests.test_request_diagnostics import ProbeClient, make_config, provider


class RequestBudgetTests(unittest.TestCase):
    def client(self, **overrides):
        client = ProbeClient(config=make_config(**overrides), keyring=Mock())
        self.addCleanup(client.close)
        return client

    def test_default_root_budgets_preserve_operation_semantics(self):
        config = make_config()
        for tool, arguments, expected in [
            ("search", {}, 45), ("research", {}, 90), ("crawl_site", {}, 210),
            ("search", {"mode": "social"}, 120),
            ("search", {"sources": ["x"]}, 120),
            ("search", {"provider": "xai"}, 120),
            ("search", {"allowed_x_handles": ["example"]}, 120),
        ]:
            with self.subTest(tool=tool, arguments=arguments):
                self.assertEqual(operation_timeout(config, tool, arguments), expected)

    def test_nested_operation_uses_root_id_and_smaller_budget(self):
        client = self.client(timeout_seconds=0.05, research_timeout_seconds=1)
        seen = []

        def child():
            seen.append((current_request.get().request_id, remaining_seconds()))
            return {"content": "text"}

        def root():
            seen.append((current_request.get().request_id, remaining_seconds()))
            result = client.extract_url(child)
            self.assertNotIn("diagnostics", result)
            return {"results": [result]}

        result = client.research(root)
        self.assertEqual({item[0] for item in seen}, {result["diagnostics"]["request_id"]})
        self.assertGreater(seen[0][1], 0.5)
        self.assertLessEqual(seen[1][1], 0.05)
        self.assertFalse(client._active_requests)

    def test_queue_time_is_part_of_budget_and_expiry_sends_no_http(self):
        client = self.client(timeout_seconds=0.01)
        state = client.new_request("search")
        state.deadline = time.monotonic() - 1
        with request_context(state), patch.object(client._http, "request") as http:
            with self.assertRaises(MySearchError):
                client.search(lambda: client._request_json_once(provider=provider(), method="POST", path="/search", payload={}, key="fake"))
        http.assert_not_called()
        self.assertEqual(state.final["outcome"], "deadline")
        self.assertEqual(state.final["diagnostics"]["call_count"], 0)

    def test_returned_fallback_is_preserved_when_root_budget_has_elapsed(self):
        client = self.client()
        unavailable = {"kind": "unavailable", "results": [], "fallback_reason": "social budget exhausted"}

        def fallback():
            current_request.get().deadline = time.monotonic() - 1
            return unavailable

        result = client.search(fallback, mode="social")
        self.assertEqual(result["kind"], "unavailable")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["diagnostics"]["outcome"], "deadline")

    def test_short_retry_wait_preserves_request_and_allows_next_http(self):
        client = self.client()
        with patch.object(client._http, "get", return_value=httpx.Response(200, text="ok")) as http:
            def retry():
                state = current_request.get()
                wait_before_retry(0.001)
                self.assertIs(current_request.get(), state)
                return {"results": [client._request_text(url="https://example.test")]}
            result = client.search(retry)
        self.assertEqual(result["diagnostics"]["outcome"], "success")
        http.assert_called_once()

    def test_cancel_prevents_new_http_and_wakes_retry_wait(self):
        client = self.client()
        state = client.new_request("search")
        started, done = Event(), Event()
        errors = []

        def waiter():
            try:
                with request_context(state):
                    started.set()
                    wait_before_retry(10)
            except MySearchError as exc:
                errors.append(exc)
            finally:
                done.set()

        thread = Thread(target=waiter)
        thread.start()
        self.assertTrue(started.wait(1))
        state.cancel()
        self.assertTrue(done.wait(0.5))
        thread.join(1)
        self.assertEqual(len(errors), 1)
        with request_context(state), patch.object(client._http, "get") as http:
            with self.assertRaises(MySearchError):
                client._request_text(url="https://private.test")
        http.assert_not_called()
        client.finish_request(state)
        self.assertEqual(state.final["outcome"], "cancelled")

    def test_parallel_children_share_request_and_finish_real_phases(self):
        client = self.client()
        state = client.new_request("search")
        with request_context(state), patch.object(client._http, "get", return_value=httpx.Response(200, text="ok")):
            def root():
                values, errors = client._execute_parallel({
                    "primary": lambda: client._request_text(url="https://one.test"),
                    "secondary": lambda: client._request_text(url="https://two.test"),
                })
                self.assertFalse(errors)
                return {"results": list(values.values())}
            result = client.search(root)
        calls = result["diagnostics"]["calls"]
        self.assertEqual({call["phase"] for call in calls}, {"primary", "secondary"})
        self.assertEqual(set(state.progress_snapshot()), {"primary", "secondary", "search"})
        self.assertEqual(result["diagnostics"]["call_count"], 2)

    def test_single_branch_without_timeout_reuses_worker(self):
        client = self.client()
        owner = get_ident()

        def root():
            values, errors = client._execute_parallel({"primary": get_ident})
            self.assertFalse(errors)
            self.assertEqual(values["primary"], owner)
            return {"results": [1]}

        client.search(root)

    def test_parallel_budget_retains_completed_branch_results(self):
        client = self.client(timeout_seconds=0.03)
        release = Event()

        def root():
            values, errors = client._execute_parallel({
                "primary": lambda: "useful result",
                "secondary": lambda: release.wait(2),
            })
            self.assertEqual(values, {"primary": "useful result"})
            self.assertIn("secondary", errors)
            return {"results": list(values.values())}

        try:
            result = client.search(root)
            self.assertEqual(result["results"], ["useful result"])
            self.assertEqual(result["diagnostics"]["outcome"], "deadline")
        finally:
            release.set()

    def test_close_cancels_active_root_then_waits_for_it_to_finish(self):
        client = self.client()
        state = client.new_request("search")
        closed = Event()
        thread = Thread(target=lambda: (client.close(), closed.set()))
        thread.start()
        try:
            self.assertTrue(state.cancelled.wait(1))
            self.assertFalse(closed.is_set())
            client.finish_request(state)
            self.assertTrue(closed.wait(1))
        finally:
            client.finish_request(state)
            thread.join(2)
        self.assertEqual(state.final["outcome"], "cancelled")

    def test_optional_deadline_does_not_cancel_primary_or_allow_late_http(self):
        client = self.client(timeout_seconds=1)
        expired = Event()
        observations = []

        def optional():
            time.sleep(0.05)
            try:
                remaining_seconds()
                client._request_text(url="https://late.test")
            except MySearchError:
                observations.append(current_request.get().cancelled.is_set())
            finally:
                expired.set()

        def primary():
            self.assertTrue(expired.wait(0.5))
            remaining_seconds()
            return "Complete primary body"

        with patch.object(client._http, "get") as http:
            def root():
                values, errors = client._execute_parallel(
                    {"primary": primary, "secondary": optional}, max_workers=2,
                    task_timeouts={"secondary": 0.01},
                )
                self.assertIn("secondary", errors)
                return {"results": [values["primary"]]}
            result = client.search(root)
        self.assertEqual(result["results"], ["Complete primary body"])
        self.assertEqual(observations, [False])
        http.assert_not_called()

    def test_optional_window_includes_queue_time(self):
        client = self.client(timeout_seconds=1)
        optional = Mock()
        values, errors = client._execute_parallel(
            {"primary": lambda: (time.sleep(0.05), "done")[1], "secondary": optional},
            max_workers=1, task_timeouts={"secondary": 0.01},
        )
        self.assertEqual(values, {"primary": "done"})
        self.assertIn("secondary", errors)
        optional.assert_not_called()


if __name__ == "__main__":
    unittest.main()
