from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import anyio


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "proxy") not in sys.path:
    sys.path.insert(0, str(ROOT / "proxy"))
spec = importlib.util.spec_from_file_location("proxy_diagnostics_test_server", ROOT / "proxy/server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class ProxyDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"MYSEARCH_PROXY_DB_PATH": str(Path(self.directory.name) / "proxy.db")})
        self.env.start()
        server.db.close_conn()
        server.db.init_db()
        self.token = server.db.create_token("diagnostics-test", service="mysearch")
        self.request_id, self.call_id = uuid4().hex, uuid4().hex
        self.headers = {
            "Authorization": f"Bearer {self.token['token']}",
            "X-MySearch-Request-ID": self.request_id,
            "X-MySearch-Call-ID": self.call_id,
            "X-MySearch-Phase": "search",
        }
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        server.db.close_conn()
        self.env.stop()
        self.directory.cleanup()

    def report(self, provider="exa"):
        return {
            "request_id": self.request_id, "tool": "search", "outcome": "success",
            "started_at": datetime.now(timezone.utc).isoformat(), "elapsed_ms": 200,
            "failure_reason": "", "cache_hit": False,
            "diagnostics": {
                "phases": [{"name": "search", "elapsed_ms": 150}], "call_count": 1,
                "calls": [{"call_id": self.call_id, "layer": "runtime", "provider": provider,
                           "operation": "search", "phase": "search", "elapsed_ms": 150,
                           "status_code": 200, "outcome": "success", "failure_reason": "", "usage": []}],
                "usage": [],
            },
        }

    async def diagnostics(self, **params):
        with patch.object(server, "get_admin_password", return_value="admin-test"):
            response = await self.client.get("/api/diagnostics", params=params, headers={"X-Admin-Password": "admin-test"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_bad_key_then_success_is_one_tool_and_two_attempts(self):
        replies = [httpx.Response(401, json={"error": "invalid api key"}), httpx.Response(200, json={"results": []})]
        keys = [{"id": 1, "key": "secret-first"}, {"id": 2, "key": "secret-second"}]
        with patch.object(server.pool, "get_next_key", side_effect=keys), patch.object(server.pool, "report_result"), patch.object(
            server.http_client, "post", new=AsyncMock(side_effect=replies),
        ):
            response = await self.client.post("/exa/search", json={"query": "private-search-text"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        legacy = server.db.get_usage_stats(token_id=self.token["id"])
        self.assertEqual((legacy["today_success"], legacy["today_failed"]), (1, 1))
        response = await self.client.post("/api/mysearch/requests", json=self.report(), headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(server.db.get_usage_stats(token_id=self.token["id"]), legacy)
        data = await self.diagnostics(service="exa")
        self.assertEqual(data["logical_requests"]["count"], 1)
        self.assertEqual(data["logical_requests"]["success_count"], 1)
        self.assertEqual(data["logical_requests"]["p95_ms"], 200)
        self.assertEqual(data["physical_attempts"]["count"], 2)
        self.assertEqual(data["physical_attempts"]["failure_reasons"], {"auth_rejected": 1})
        attempts = data["recent_requests"][0]["attempts"]
        self.assertEqual([item["status_code"] for item in attempts], [401, 200])
        self.assertTrue(all(item["call_id"] == self.call_id for item in attempts))
        serialized = json.dumps(data)
        for secret in ("private-search-text", "secret-first", "secret-second", self.token["token"]):
            self.assertNotIn(secret, serialized)

    async def test_tavily_gateway_404_counts_both_physical_sends(self):
        server.db.set_setting("tavily_mode", "upstream")
        server.db.set_setting("tavily_upstream_base_url", "https://gateway.test")
        server.db.set_setting("tavily_upstream_api_key", "gateway-secret")
        replies = [httpx.Response(404, json={"detail": "missing"}), httpx.Response(200, json={"results": [], "usage": {"credits": 1}})]
        with patch.object(server.http_client, "post", new=AsyncMock(side_effect=replies)) as send:
            response = await self.client.post("/api/search", json={"query": "private-text"}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(send.await_count, 2)
        data = await self.diagnostics(service="tavily")
        self.assertEqual(data["logical_requests"]["count"], 0)
        self.assertEqual(data["physical_attempts"]["count"], 2)
        self.assertEqual(data["recent_attempts"][0]["usage"], [{"name": "credits", "unit": "credits", "value": 1}])

    async def test_firecrawl_key_retry_records_safe_operation_without_job_url(self):
        replies = [httpx.Response(401, json={"error": "invalid key"}), httpx.Response(200, json={"status": "completed", "data": []})]
        keys = [{"id": 1, "key": "firecrawl-secret-a"}, {"id": 2, "key": "firecrawl-secret-b"}]
        with patch.object(server.pool, "get_next_key", side_effect=keys), patch.object(server.pool, "report_result"), patch.object(
            server, "_preferred_crawl_job_key", return_value="",
        ), patch.object(server.http_client, "request", new=AsyncMock(side_effect=replies)):
            response = await self.client.get("/firecrawl/v2/crawl/private-job-id", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        data = await self.diagnostics(service="firecrawl")
        self.assertEqual(data["physical_attempts"]["count"], 2)
        self.assertTrue(all(row["operation"] == "crawl_status" for row in data["recent_attempts"]))
        self.assertNotIn("private-job-id", json.dumps(data))

    async def test_crawl_poll_cumulative_credits_are_not_physical_attempt_usage(self):
        for index, field in enumerate(("creditsUsed", "credits_used")):
            with self.subTest(field=field):
                payloads = [{"status": "scraping", field: count, "data": []} for count in (4, 5, 5)]
                with patch.object(server.pool, "get_next_key", return_value={"id": 1, "key": "synthetic-key"}), patch.object(
                    server.pool, "report_result",
                ), patch.object(server, "_preferred_crawl_job_key", return_value=""), patch.object(
                    server.http_client, "request", new=AsyncMock(side_effect=[httpx.Response(200, json=payload) for payload in payloads]),
                ) as send:
                    responses = [await self.client.get("/firecrawl/v2/crawl/test-job", headers=self.headers) for _ in payloads]
                self.assertEqual([response.status_code for response in responses], [200, 200, 200])
                self.assertEqual([response.json() for response in responses], payloads)
                self.assertEqual(send.await_count, 3)
                details = await self.diagnostics(service="firecrawl")
                self.assertEqual(details["physical_attempts"]["count"], (index + 1) * 3)
                self.assertTrue(all(row["operation"] == "crawl_status" for row in details["recent_attempts"]))
                self.assertTrue(all(row["usage"] == [] for row in details["recent_attempts"]))

    async def test_crawl_status_keeps_explicit_per_request_credits(self):
        payload = {"status": "completed", "creditsUsed": 5, "usage": {"credits": 2}, "data": []}
        with patch.object(server.pool, "get_next_key", return_value={"id": 1, "key": "synthetic-key"}), patch.object(
            server.pool, "report_result",
        ), patch.object(server, "_preferred_crawl_job_key", return_value=""), patch.object(
            server.http_client, "request", new=AsyncMock(return_value=httpx.Response(200, json=payload)),
        ):
            response = await self.client.get("/firecrawl/v2/crawl/test-job", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), payload)
        self.assertEqual((await self.diagnostics(service="firecrawl"))["recent_attempts"][0]["usage"],
                         [{"name": "credits", "unit": "credits", "value": 2}])

    async def test_social_key_retry_and_model_fallback_count_real_sends(self):
        state = {"accepted_tokens": [], "upstream_api_keys": ["social-secret-a", "social-secret-b"],
                 "resolved_upstream_api_key": "social-secret-a", "upstream_base_url": "https://social.test",
                 "upstream_responses_path": "/responses", "model": "primary", "fallback_model": "fallback",
                 "fallback_min_results": 3}
        replies = [httpx.Response(401, json={"error": "invalid key"}), httpx.Response(200, json={}), httpx.Response(200, json={})]
        with patch.object(server, "resolve_social_gateway_state", new=AsyncMock(return_value=state)), patch.object(
            server, "_ordered_social_upstream_keys", return_value=state["upstream_api_keys"],
        ), patch.object(server, "_social_model_cooldown", return_value=("scope", 0, None)), patch.object(
            server, "_schedule_social_upstream_key",
        ), patch.object(server, "should_retry_social_with_fallback", return_value=(True, "few_results")), patch.object(
            server.http_client, "post", new=AsyncMock(side_effect=replies),
        ):
            response = await self.client.post("/social/search", json={"query": "private-text"}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        data = await self.diagnostics(service="social")
        self.assertEqual(data["physical_attempts"]["count"], 3)
        self.assertEqual([row["status_code"] for row in reversed(data["recent_attempts"])], [401, 200, 200])

    async def test_reports_are_authorized_idempotent_and_conflict_on_change(self):
        report = self.report()
        for headers in ({}, {"Authorization": "Bearer invalid-token"}):
            response = await self.client.post("/api/mysearch/requests", json=report, headers=headers)
            self.assertEqual(response.status_code, 401)
        wrong = server.db.create_token("provider-token", service="tavily")
        response = await self.client.post("/api/mysearch/requests", json=report, headers={"Authorization": f"Bearer {wrong['token']}"})
        self.assertEqual(response.status_code, 401)
        first = await self.client.post("/api/mysearch/requests", json=report, headers=self.headers)
        second = await self.client.post("/api/mysearch/requests", json=report, headers=self.headers)
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual((first.json()["created"], second.json()["created"]), (True, False))
        report["elapsed_ms"] += 1
        conflict = await self.client.post("/api/mysearch/requests", json=report, headers=self.headers)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual((await self.diagnostics())["logical_requests"]["count"], 1)
        unauthorized = await self.client.get("/api/diagnostics")
        self.assertEqual(unauthorized.status_code, 401)

    async def test_invalid_metadata_rejected_without_echoing_content(self):
        mutations = [
            lambda row: row.update(query="private-value"),
            lambda row: row.update(request_id="private-value"),
            lambda row: row.update(elapsed_ms=True),
            lambda row: row.update(failure_reason="https://secret.test/?key=private-value"),
            lambda row: row["diagnostics"]["calls"][0].update(url="private-value"),
            lambda row: row["diagnostics"].update(phases=[{"name": "search", "elapsed_ms": 0}] * 65),
            lambda row: row["diagnostics"]["calls"][0].update(usage=[{"name": "credits", "unit": "credits", "value": True}]),
            lambda row: row["diagnostics"].update(call_count=0),
        ]
        for mutate in mutations:
            report = self.report()
            mutate(report)
            response = await self.client.post("/api/mysearch/requests", json=report, headers=self.headers)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertNotIn("private-value", response.text)
        response = await self.client.post("/exa/search", json={}, headers={**self.headers, "X-MySearch-Request-ID": "invalid"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual((await self.diagnostics())["physical_attempts"]["count"], 0)

    async def test_empty_pool_does_not_count_attempt_and_network_error_has_safe_reason(self):
        with patch.object(server.pool, "get_next_key", return_value=None), patch.object(server.pool, "get_retry_after", return_value=None):
            response = await self.client.post("/exa/search", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertEqual((await self.diagnostics())["physical_attempts"]["count"], 0)
        with patch.object(server.pool, "get_next_key", return_value={"id": 1, "key": "secret"}), patch.object(
            server.pool, "report_result",
        ), patch.object(server.http_client, "post", new=AsyncMock(side_effect=httpx.ConnectError("private-url-and-key"))), patch.object(server.logger, "exception"):
            response = await self.client.post("/exa/search", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 502)
        data = await self.diagnostics()
        self.assertEqual(data["physical_attempts"]["count"], 1)
        self.assertEqual(data["recent_attempts"][0]["failure_reason"], "ConnectError")
        self.assertNotIn("private-url-and-key", json.dumps(data))

    async def test_worker_connections_closed_on_success_and_failure(self):
        opened = []
        main_thread = threading.get_ident()

        def unit(fail=False):
            opened.append((threading.get_ident(), server.db.get_conn()))
            if fail:
                raise ValueError("test failure")
            return 7

        result = await asyncio.to_thread(server._diagnostic_db_call, unit)
        self.assertEqual(result, 7)
        with self.assertRaisesRegex(ValueError, "test failure"):
            await asyncio.to_thread(server._diagnostic_db_call, unit, True)
        for thread_id, connection in opened:
            self.assertNotEqual(thread_id, main_thread)
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    async def test_diagnostic_write_failure_does_not_change_search_result(self):
        with patch.object(server.pool, "get_next_key", return_value={"id": 1, "key": "secret"}), patch.object(
            server.pool, "report_result",
        ), patch.object(server.http_client, "post", new=AsyncMock(return_value=httpx.Response(200, json={"results": []}))), patch.object(
            server.db, "save_request_attempts", side_effect=sqlite3.OperationalError("private-db-path"),
        ), patch.object(server.logger, "warning") as warning:
            response = await self.client.post("/exa/search", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        warning.assert_called_once_with("diagnostic_attempt_write_failed:%s", "OperationalError")

    async def test_old_database_upgrade_is_idempotent_and_keeps_usage(self):
        server.db.log_usage(self.token["id"], None, "search", 1, 12, service="exa")
        before = server.db.get_usage_stats()
        conn = server.db.get_conn()
        conn.execute("DROP TABLE tool_requests")
        conn.execute("DROP TABLE request_attempts")
        conn.commit()
        server.db.init_db()
        server.db.init_db()
        self.assertEqual(server.db.get_usage_stats(), before)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"tool_requests", "request_attempts"} <= tables)

    async def test_concurrent_requests_keep_token_and_request_association(self):
        other = server.db.create_token("other", service="mysearch")
        other_request = uuid4().hex
        ready = asyncio.Event()
        sent = 0

        async def send(*args, **kwargs):
            nonlocal sent
            sent += 1
            if sent == 2:
                ready.set()
            await ready.wait()
            return httpx.Response(200, json={"results": []})

        other_headers = {**self.headers, "Authorization": f"Bearer {other['token']}", "X-MySearch-Request-ID": other_request}
        with patch.object(server.pool, "get_next_key", return_value={"id": 1, "key": "secret"}), patch.object(
            server.pool, "report_result",
        ), patch.object(server.http_client, "post", new=send):
            responses = await asyncio.gather(
                self.client.post("/exa/search", json={}, headers=self.headers),
                self.client.post("/exa/search", json={}, headers=other_headers),
            )
        self.assertEqual([response.status_code for response in responses], [200, 200])
        pairs = {(row["token_id"], row["request_id"]) for row in (await self.diagnostics())["recent_attempts"]}
        self.assertEqual(pairs, {(self.token["id"], self.request_id), (other["id"], other_request)})

    async def test_runtime_finished_report_schema_integration(self):
        from mysearch.diagnostics import RequestState, finish_payload
        import time

        state = RequestState(deadline=time.monotonic() + 10, tool="search")
        state.start_call({"call_id": uuid4().hex, "layer": "runtime", "provider": "exa", "operation": "search",
                          "phase": "search", "elapsed_ms": 0, "status_code": None, "outcome": "pending", "usage": []})
        state.cancel("deadline")
        report = finish_payload(state)
        response = await self.client.post("/api/mysearch/requests", json=report, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        data = await self.diagnostics(service="exa")
        self.assertEqual(data["logical_requests"]["outcomes"], {"deadline": 1})
        self.assertEqual(data["physical_attempts"]["count"], 0)

    async def test_asgi_cancellation_persists_completed_cancelled_attempt(self):
        sent = anyio.Event()

        async def send(*args, **kwargs):
            sent.set()
            await anyio.sleep_forever()

        async def request():
            await self.client.post("/exa/search", json={}, headers=self.headers)

        with patch.object(server.pool, "get_next_key", return_value={"id": 1, "key": "synthetic"}), patch.object(
            server.http_client, "post", new=send,
        ), patch.object(server.db, "save_request_attempts") as save:
            async with anyio.create_task_group() as group:
                group.start_soon(request)
                with anyio.fail_after(2):
                    await sent.wait()
                group.cancel_scope.cancel()
            await anyio.sleep(0.02)
        save.assert_called_once()
        attempts = save.call_args.args[0]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["request_id"], self.request_id)
        self.assertEqual(attempts[0]["failure_reason"], "CancelledError")
        self.assertIsNone(attempts[0]["status_code"])

    async def test_huge_json_usage_integer_is_rejected_without_server_error(self):
        report = self.report()
        report["diagnostics"]["calls"][0]["usage"] = [{"name": "credits", "unit": "credits", "value": 10 ** 1000}]
        response = await self.client.post("/api/mysearch/requests", json=report, headers=self.headers)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual((await self.diagnostics())["logical_requests"]["count"], 0)


if __name__ == "__main__":
    unittest.main()
