from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx


ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "proxy"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import database as db
from key_pool import ServiceKeyPool
from mysearch.clients import MySearchClient, MySearchHTTPError
from mysearch.config import MySearchConfig
from mysearch.errors import classify_upstream_key_failure, classify_upstream_operation_failure


class ActiveKeyPoolTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("active_pool_server", ROOT / "proxy/server.py")
        cls.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.server)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.enterContext(patch.dict(os.environ, {"MYSEARCH_PROXY_DB_PATH": str(Path(self.temp.name) / "proxy.db")}))
        db.close_conn()
        db.init_db()
        self.addCleanup(db.close_conn)
        self.pool = ServiceKeyPool()
        self.enterContext(patch.object(self.server, "pool", self.pool))

    def add_key(self, value="fixture-exa", service="exa"):
        return db.add_key(value, service=service)["id"]

    def block(self, key_id, operation="contents", **kwargs):
        self.pool.report_result("exa", key_id, False, failure_kind="team_blocked", operation=operation, **kwargs)

    async def request(self, path, **kwargs):
        token = db.create_token("fixture-client", service="mysearch")["token"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://test") as client:
            return await client.post(path, headers={"Authorization": f"Bearer {token}"}, json={}, **kwargs)

    def test_operation_block_survives_reopen_and_preserves_other_operation(self):
        key_id = self.add_key()
        self.block(key_id)
        db.close_conn()
        db.init_db()
        self.pool = ServiceKeyPool()
        self.assertIsNone(self.pool.get_next_key("exa", operation="contents"))
        self.assertEqual(self.pool.get_next_key("exa", operation="search")["id"], key_id)
        row = dict(db.get_key_by_id(key_id))
        self.assertEqual(row["active"], 1)
        self.assertEqual(json.loads(row["blocked_operations"])["contents"]["reason"], "team_blocked")
        self.assertEqual(row["total_failed"], 1)

    def test_two_blocks_accumulate_and_success_cannot_clear_them(self):
        key_id = self.add_key()
        self.block(key_id, "contents")
        self.block(key_id, "search")
        self.pool.report_result("exa", key_id, True)
        self.pool.reload()
        self.assertIsNone(self.pool.get_next_key("exa"))
        self.assertEqual(len(json.loads(db.get_key_by_id(key_id)["blocked_operations"])), 2)

    def test_explicit_resume_clears_blocks_but_duplicate_import_does_not(self):
        key_id = self.add_key()
        self.block(key_id)
        self.assertEqual(db.ingest_key("fixture-exa", service="exa")["status"], "duplicate")
        self.pool.reload()
        self.assertIsNone(self.pool.get_next_key("exa", operation="contents"))
        result = db.ingest_key("fixture-exa", service="exa", reactivate=True)
        self.assertTrue(result["reactivated"])
        self.pool.invalidate("exa")
        self.pool.reload()
        self.assertEqual(self.pool.get_next_key("exa", operation="contents")["id"], key_id)

    def test_manual_resume_rejects_late_failure_and_manual_disable_wins(self):
        key_id = self.add_key()
        selected = self.pool.get_next_key("exa", operation="contents")
        self.block(key_id)
        self.pool.invalidate("exa")
        db.toggle_key(key_id, True)
        self.pool.reload("exa")
        self.block(key_id, generation=selected["_pool_generation"])
        self.assertEqual(self.pool.get_next_key("exa", operation="contents")["id"], key_id)
        db.toggle_key(key_id, False)
        self.block(key_id)
        self.pool.reload("exa")
        self.assertIsNone(self.pool.get_next_key("exa", operation="search"))
        self.assertEqual(db.get_key_by_id(key_id)["disabled_reason"], "manual")

    def test_missing_operation_fails_before_mutating_usage(self):
        key_id = self.add_key()
        with self.assertRaises(ValueError):
            db.update_key_usage(key_id, False, failure_kind="team_blocked")
        self.assertEqual(db.get_key_by_id(key_id)["total_failed"], 0)

    def test_operation_classifier_keeps_provider_status_and_payload_scope(self):
        for service, status, payload, expected in (
            ("exa", 403, {"tag":"TEAM_BLOCKED"}, "team_blocked"),
            ("exa", 403, {"detail":{"tag":"TEAM_BLOCKED"}}, "team_blocked"),
            ("exa", 200, {"statuses":[{"error":{"tag":"TEAM_BLOCKED"}}]}, ""),
            ("exa", 500, {"tag":"TEAM_BLOCKED"}, ""),
            ("firecrawl", 403, {"tag":"TEAM_BLOCKED"}, ""),
            ("exa", 403, {"error":"a page mentions TEAM_BLOCKED"}, ""),
            ("exa", 403, {"error":"TEAM_BLOCKED"}, ""),
        ):
            with self.subTest(service=service, status=status, payload=payload):
                self.assertEqual(classify_upstream_operation_failure(status, payload, service=service), expected)

    def test_concurrent_operation_updates_do_not_overwrite_each_other(self):
        key_id = self.add_key()
        barrier = threading.Barrier(2)

        def deny(operation):
            try:
                barrier.wait(timeout=5)
                db.update_key_usage(key_id, False, failure_kind="team_blocked", operation=operation)
            finally:
                db.close_conn()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(deny, operation) for operation in ("search", "contents")]
            for future in futures:
                future.result(timeout=10)
        row = dict(db.get_key_by_id(key_id))
        self.assertEqual(row["total_failed"], 2)
        self.assertEqual(set(json.loads(row["blocked_operations"])), {"search", "contents"})

    def test_failed_persistence_rolls_back_usage_and_keeps_pool_snapshot(self):
        key_id = self.add_key()
        self.pool.reload("exa")
        conn = db.get_conn()
        conn.execute("""CREATE TRIGGER fixture_reject_block BEFORE UPDATE OF blocked_operations ON api_keys
                        BEGIN SELECT RAISE(ABORT, 'fixture write failure'); END""")
        conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.block(key_id)
        self.assertEqual(db.get_key_by_id(key_id)["total_failed"], 0)
        self.assertFalse(conn.in_transaction)
        self.assertIsNotNone(self.pool.get_next_key("exa", operation="contents"))

    def test_global_failures_and_cooldown_exclude_keys_for_all_three_pools(self):
        for service in ("tavily", "firecrawl", "exa"):
            with self.subTest(service=service):
                for suffix, kind in (("auth", "auth_rejected"), ("quota", "quota_exhausted"), ("rate", "rate_limited")):
                    key_id = self.add_key(f"fixture-{service}-{suffix}", service)
                    self.pool.report_result(service, key_id, False, failure_kind=kind, retry_after_seconds=60)
                usable = self.add_key(f"fixture-{service}-ok", service)
                self.pool.reload(service)
                for _ in range(4):
                    self.assertEqual(self.pool.get_next_key(service)["id"], usable)

    def test_legacy_schema_migrates_without_losing_keys_or_counts(self):
        legacy = Path(self.temp.name) / "legacy.db"
        with sqlite3.connect(legacy) as conn:
            conn.execute("""CREATE TABLE api_keys (id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL,
                email TEXT, active INTEGER DEFAULT 1, total_used INTEGER DEFAULT 0,
                total_failed INTEGER DEFAULT 0, consecutive_fails INTEGER DEFAULT 0,
                last_used_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
            conn.execute("INSERT INTO api_keys(id,key,active,total_used,consecutive_fails) VALUES (?,?,?,?,?)", (7,"fixture-legacy",0,42,6))
        db.close_conn()
        with patch.dict(os.environ, {"MYSEARCH_PROXY_DB_PATH": str(legacy)}):
            try:
                db.init_db()
                db.init_db()
                row = db.get_key_by_id(7)
                self.assertEqual((row["active"], row["total_used"], row["blocked_operations"]), (0,42,"{}"))
                self.assertEqual(row["disabled_reason"], "legacy_failure_threshold")
            finally:
                db.close_conn()

    async def test_exa_real_proxy_rotates_then_skips_blocked_operation(self):
        bad = self.add_key("fixture-denied")
        good = self.add_key("fixture-working")
        responses = [
            httpx.Response(403, json={"tag":"TEAM_BLOCKED","error":"fixture-denied cannot use this endpoint"}),
            httpx.Response(200, json={"results":[]}),
            httpx.Response(200, json={"results":[]}),
        ]
        with patch.object(self.server.http_client, "post", new=AsyncMock(side_effect=responses)) as upstream:
            self.assertEqual((await self.request("/exa/contents")).status_code, 200)
            self.assertEqual((await self.request("/exa/contents")).status_code, 200)
        self.assertEqual([call.kwargs["headers"]["x-api-key"] for call in upstream.call_args_list],
                         ["fixture-denied","fixture-working","fixture-working"])
        self.assertTrue(all(call.args[0] == f"{self.server.EXA_API_BASE}/contents"
                            for call in upstream.call_args_list))
        row = dict(db.get_key_by_id(bad))
        self.assertEqual(row["active"], 1)
        masked = self.server.mask_key_rows([row])[0]
        self.assertNotIn("fixture-denied", json.dumps(masked))
        self.assertEqual(masked["available_operations"], ["search"])
        self.assertEqual(db.get_key_by_id(good)["total_used"], 2)
        logs = db.get_conn().execute("SELECT failure_reason FROM usage_logs ORDER BY id").fetchall()
        self.assertEqual(logs[0]["failure_reason"], "team_blocked")

    async def test_exhausted_operation_fails_without_request_and_keeps_client_token_valid(self):
        key_id = self.add_key()
        self.block(key_id)
        with patch.object(self.server.http_client, "post", new=AsyncMock()) as upstream:
            response = await self.request("/exa/contents")
        upstream.assert_not_awaited()
        self.assertEqual(response.status_code, 503)
        self.assertIn("contents", response.text)
        self.assertEqual(db.get_key_by_id(key_id)["active"], 1)

    async def test_all_denied_keys_are_tried_once_then_future_requests_fail_locally(self):
        self.add_key("fixture-first")
        self.add_key("fixture-second")
        with patch.object(self.server.http_client, "post", new=AsyncMock(return_value=httpx.Response(
            403, json={"tag":"TEAM_BLOCKED", "error":"Endpoint access denied"},
        ))) as upstream:
            response = await self.request("/exa/search")
            self.assertEqual(response.status_code, 503)
            self.assertIn("search", response.text)
            self.assertEqual((await self.request("/exa/search")).status_code, 503)
        self.assertEqual(upstream.await_count, 2)
        self.assertIsNotNone(self.pool.get_next_key("exa", operation="contents"))

    async def test_managed_runtime_uses_proxy_pool_without_quarantining_its_token(self):
        bad = self.add_key("fixture-denied")
        good = self.add_key("fixture-working")
        self.block(bad)
        token = db.create_token("fixture-runtime", service="mysearch")["token"]
        with patch.dict(os.environ, {"MYSEARCH_PROXY_BASE_URL":"http://test", "MYSEARCH_PROXY_API_KEY":token}, clear=True):
            config = MySearchConfig.from_env()
        runtime = MySearchClient(config)
        loop = asyncio.get_running_loop()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://test") as proxy:
                def send_to_proxy(*args, **kwargs):
                    return asyncio.run_coroutine_threadsafe(proxy.request(*args, **kwargs), loop).result(timeout=5)

                async def contents():
                    return await asyncio.to_thread(runtime._request_json, provider=config.exa, method="POST",
                                                   path=config.exa.default_paths["contents"], payload={}, key=token)

                with patch.object(runtime._http, "request", side_effect=send_to_proxy), patch.object(
                    self.server.http_client, "post", new=AsyncMock(return_value=httpx.Response(200, json={"results":[]})),
                ) as upstream:
                    self.assertEqual(await contents(), {"results":[]})
                    self.assertEqual(upstream.call_args.kwargs["headers"]["x-api-key"], "fixture-working")
                    self.block(good)
                    with self.assertRaises(MySearchHTTPError) as failure:
                        await contents()
                    self.assertEqual(failure.exception.status_code, 503)
                    self.assertEqual(upstream.await_count, 1)
                    self.assertEqual(runtime.keyring.describe()["exa"]["quarantine_reasons"], [])
        finally:
            runtime.close()

    async def test_tavily_and_firecrawl_http_routes_use_only_active_pool(self):
        db.set_setting("tavily_mode", "pool")
        db.set_setting("tavily_upstream_api_key", "fixture-upstream-unused")
        for service, path, method in (("tavily","/api/search","post"), ("firecrawl","/firecrawl/v2/search","request")):
            with self.subTest(service=service):
                disabled = self.add_key(f"fixture-{service}-off", service)
                db.toggle_key(disabled, False)
                self.add_key(f"fixture-{service}-ok", service)
                with patch.object(self.server.http_client, method, new=AsyncMock(return_value=httpx.Response(
                    200, json={"results":[]},
                ))) as upstream:
                    self.assertEqual((await self.request(path)).status_code, 200)
                outgoing = str(upstream.call_args)
                self.assertIn(f"fixture-{service}-ok", outgoing)
                self.assertNotIn(f"fixture-{service}-off", outgoing)
                self.assertNotIn("fixture-upstream-unused", outgoing)

    async def test_cooldown_of_ineligible_key_does_not_mask_operation_exhaustion(self):
        key_id = self.add_key()
        self.block(key_id)
        self.pool.report_result("exa", key_id, False, failure_kind="rate_limited", retry_after_seconds=60)
        with patch.object(self.server.http_client, "post", new=AsyncMock()) as upstream:
            self.assertEqual((await self.request("/exa/contents")).status_code, 503)
            self.assertEqual((await self.request("/exa/search")).status_code, 429)
        upstream.assert_not_awaited()
        conn = db.get_conn()
        conn.execute("UPDATE api_keys SET schedule_until=? WHERE id=?",
                     ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),key_id))
        conn.commit()
        self.pool.reload()
        self.assertIsNone(self.pool.get_next_key("exa", operation="contents"))
        self.assertIsNotNone(self.pool.get_next_key("exa", operation="search"))

    async def test_content_and_unknown_403_do_not_quarantine_endpoint(self):
        key_id = self.add_key()
        for payload in ({"tag":"FEATURE_DISABLED"}, {"tag":"PROHIBITED_CONTENT"}, {"error":"Forbidden"}):
            with self.subTest(payload=payload), patch.object(self.server.http_client, "post", new=AsyncMock(
                return_value=httpx.Response(403, json=payload),
            )) as upstream:
                self.assertEqual((await self.request("/exa/search")).status_code, 403)
                self.assertEqual(upstream.await_count, 1)
        self.assertEqual(db.get_key_by_id(key_id)["blocked_operations"], "{}")
        self.assertEqual(classify_upstream_key_failure(403, {"tag":"TEAM_BLOCKED"}, service="exa"), "")

    async def test_admin_restore_uses_existing_toggle(self):
        key_id = self.add_key()
        self.block(key_id)
        self.server.app.dependency_overrides[self.server.verify_admin] = lambda: None
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://test") as client:
                response = await client.put(f"/api/keys/{key_id}/toggle", json={"active":1})
                self.assertEqual(response.status_code, 200)
                keys = (await client.get("/api/keys?service=exa")).json()["keys"]
            self.assertEqual(keys[0]["blocked_operations"], {})
            self.assertEqual(keys[0]["available_operations"], ["search","contents"])
            self.assertIsNotNone(self.pool.get_next_key("exa", operation="contents"))
        finally:
            self.server.app.dependency_overrides.clear()
