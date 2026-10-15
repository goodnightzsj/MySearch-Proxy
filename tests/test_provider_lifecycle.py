"""Regression checks for provider disable/recovery boundaries."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "proxy"))


class ProviderLifecycleTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tempdir.cleanup)
        env = patch.dict(os.environ, {"MYSEARCH_PROXY_DB_PATH": str(Path(cls.tempdir.name) / "proxy.db")})
        env.start()
        cls.addClassCleanup(env.stop)
        spec = importlib.util.spec_from_file_location("test_provider_lifecycle_server", ROOT / "proxy/server.py")
        cls.server = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.server
        spec.loader.exec_module(cls.server)
        from mysearch import social_gateway
        cls.gateway = social_gateway
        cls.server.db.close_conn()
        cls.server.db.init_db()
        cls.addClassCleanup(cls.server.db.close_conn)

    def setUp(self):
        self.state = {
            "upstream_base_url": "https://gateway.example/v1",
            "upstream_responses_path": "/responses",
            "upstream_api_keys": ["test-client-key"],
            "resolved_upstream_api_key": "test-client-key",
        }
        for module in (self.server, self.gateway):
            module.social_upstream_key_schedule.clear()

    def tearDown(self):
        for module in (self.server, self.gateway):
            module.social_upstream_key_schedule.clear()

    async def test_upstream_quota_cools_only_the_model_and_recovers_after_restart(self):
        for module in (self.server, self.gateway):
            with self.subTest(module=module.__name__):
                quota = httpx.Response(429, json={"error": {"code": "upstream_quota_exhausted"}})
                success = httpx.Response(200, json={"output_text": '{"answer":"ok","results":[]}'})
                with patch.object(module.http_client, "post", new=AsyncMock(side_effect=[quota, success, success])) as post:
                    with patch.object(module.time, "time", return_value=1000):
                        result = await module.execute_social_search_attempt("test", {}, self.state, "model-a", 1)
                        self.assertEqual(result["failure_kind"], "upstream_quota_exhausted")
                        self.assertGreater(result["retry_after_seconds"], 0)
                        self.assertEqual(module._available_social_upstream_keys(self.state["upstream_api_keys"]), ["test-client-key"])
                        if module is self.server:
                            saved = self.server.db.get_setting("social_upstream_key_schedule")
                            self.assertNotIn("test-client-key", saved)
                            self.server.social_upstream_key_schedule.clear()
                            self.server._load_social_upstream_key_schedule()
                        blocked = await module.execute_social_search_attempt("test", {}, self.state, "model-a", 1)
                        self.assertFalse(blocked["ok"])
                        self.assertEqual(post.await_count, 1)
                        other = await module.execute_social_search_attempt("test", {}, self.state, "model-b", 1)
                        self.assertTrue(other["ok"])
                    with patch.object(module.time, "time", return_value=1000 + result["retry_after_seconds"]):
                        recovered = await module.execute_social_search_attempt("test", {}, self.state, "model-a", 1)
                        self.assertTrue(recovered["ok"])
                        self.assertEqual(post.await_count, 3)

    def test_legacy_quota_is_not_silently_unquarantined(self):
        key = "test-client-key"
        self.server.db.set_setting("social_upstream_key_schedule", json.dumps({
            self.server._social_key_fingerprint(key): {"reason": "quota_exhausted", "until": None},
        }))
        self.server._load_social_upstream_key_schedule()
        self.assertEqual(self.server._available_social_upstream_keys([key]), [])

    async def test_model_quota_survives_http_boundary_without_disabling_client_key(self):
        from mysearch.config import ProviderConfig
        from mysearch.errors import MySearchHTTPError
        from mysearch.keyring import MySearchKeyRing
        from mysearch.providers.base import ProviderTransport

        for module in (self.server, self.gateway):
            with self.subTest(module=module.__name__):
                state = {**self.state, "accepted_tokens": ["test-client-token"], "model": "model-a", "fallback_model": ""}
                resolver = "resolve_social_gateway_state" if module is self.server else "resolve_gateway_state"
                responses = [
                    httpx.Response(429, json={"error": {"code": "upstream_quota_exhausted", "message": "quota exhausted"}}, headers={"Retry-After": "17"}),
                    httpx.Response(200, json={"output_text": '{"answer":"ok","results":[]}'}),
                ]
                with patch.object(module, resolver, new=AsyncMock(return_value=state)), patch.object(module.http_client, "post", new=AsyncMock(side_effect=responses)) as post:
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://test") as client:
                        for _ in range(2):
                            response = await client.post("/social/search", json={"query": "test"}, headers={"Authorization": "Bearer test-client-token"})
                            self.assertEqual(response.status_code, 429)
                            self.assertGreater(int(response.headers["Retry-After"]), 0)
                            config = SimpleNamespace(timeout_seconds=10, **{
                                name: ProviderConfig(
                                    name=name, base_url="http://test", auth_mode="bearer",
                                    auth_header="Authorization", auth_scheme="Bearer", auth_field="api_key",
                                    default_paths={}, api_keys=["test-client-token"] if name == "xai" else [],
                                ) for name in ("tavily", "firecrawl", "exa", "xai")
                            })
                            transport = ProviderTransport()
                            transport.config = config
                            transport.keyring = MySearchKeyRing(config)
                            with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
                                response.status_code, content=response.content, headers=response.headers,
                            ))) as http:
                                transport._http = http
                                with self.assertRaises(MySearchHTTPError) as raised:
                                    transport._request_json(provider=config.xai, method="POST", path="/social/search", payload={}, key="test-client-token")
                            self.assertEqual(raised.exception.key_failure_kind, "upstream_quota_exhausted")
                            self.assertTrue(transport.keyring.is_available("xai", "test-client-token"))
                        self.assertEqual(post.await_count, 1)
                        state["fallback_model"] = "model-b"
                        response = await client.post("/social/search", json={"query": "test"}, headers={"Authorization": "Bearer test-client-token"})
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.json()["route"]["selected_model"], "model-b")
                        self.assertEqual(post.await_count, 2)

    def test_tavily_paygo_and_unknown_quota_do_not_disable_usable_keys(self):
        cases = [
            ({"paygo_usage": 10, "paygo_limit": 100}, 20, 90, True),
            ({"paygo_usage": 100, "paygo_limit": 100}, 20, 0, False),
            ({}, 20, None, True),
            ({"paygo_usage": 10, "paygo_limit": 100}, 100, 90, False),
        ]
        for i, (paygo, key_used, remaining, enabled) in enumerate(cases):
            with self.subTest(paygo=paygo, key_used=key_used):
                normalized = self.server.normalize_usage_payload("tavily", {
                    "key": {"usage": key_used, "limit": 100},
                    "account": {"current_plan": "test", "plan_usage": 1000, "plan_limit": 1000, **paygo},
                })
                self.assertEqual(normalized["account_remaining"], remaining)
                key_id = self.server.db.add_key(f"paygo-test-{i}", service="tavily")["id"]
                self.server.db.update_key_remote_usage(key_id, **normalized)
                self.assertEqual(bool(self.server.db.get_key_by_id(key_id)["active"]), enabled)

    async def test_due_model_allows_one_recovery_probe_and_honors_retry_after(self):
        for module in (self.server, self.gateway):
            with self.subTest(module=module.__name__):
                started, finish = asyncio.Event(), asyncio.Event()

                async def post(*args, **kwargs):
                    started.set()
                    await finish.wait()
                    return httpx.Response(200, json={"output_text": '{"answer":"ok","results":[]}'})

                with patch.object(module.time, "time", return_value=1000):
                    with patch.object(module.http_client, "post", new=AsyncMock(return_value=httpx.Response(
                        429, json={"error": {"code": "upstream_quota_exhausted"}}, headers={"Retry-After": "17"},
                    ))):
                        result = await module.execute_social_search_attempt("test", {}, self.state, "model-a", 1)
                    self.assertEqual(result["retry_after_seconds"], 17)
                    self.assertEqual(module._social_model_recovery({**self.state, "model": "model-a"})["retry_after_seconds"], 17)
                    self.assertEqual(module._social_model_recovery({**self.state, "model": "model-a", "fallback_model": "model-b"})["retry_after_seconds"], 0)
                with patch.object(module.time, "time", return_value=1017), patch.object(module.http_client, "post", new=AsyncMock(side_effect=post)) as mock_post:
                    probe = asyncio.create_task(module.execute_social_search_attempt("test", {}, self.state, "model-a", 1))
                    try:
                        await asyncio.wait_for(started.wait(), timeout=1)
                        peer = await module.execute_social_search_attempt("test", {}, self.state, "model-a", 1)
                        self.assertFalse(peer["ok"])
                        self.assertEqual(mock_post.await_count, 1)
                    finally:
                        finish.set()
                        result = await asyncio.wait_for(probe, timeout=1)
                    self.assertTrue(result["ok"])
                    self.assertEqual(module._social_model_recovery({**self.state, "model": "model-a"})["retry_after_seconds"], 0)

    async def test_social_save_unchanged_identity_preserves_quarantine(self):
        self.server.db.set_setting("social_mode", "upstream")
        self.server.db.set_setting("social_upstream_api_key", "test-client-key")
        self.server._schedule_social_upstream_key("test-client-key", "auth_rejected")
        generation = self.server.social_upstream_key_generation
        for body in (
            {"mode": "upstream", "model": "new-model"},
            {"mode": "upstream", "upstream_api_key": "test-client-key"},
            {"upstream_api_key": ""},
        ):
            request = type("Request", (), {"json": AsyncMock(return_value=body)})()
            with patch.object(self.server, "build_settings_payload", new=AsyncMock(return_value={})):
                await self.server.update_social_settings(request)
            self.assertEqual(self.server._available_social_upstream_keys(["test-client-key"]), [])
            self.assertEqual(self.server.social_upstream_key_generation, generation)

    def test_routine_pool_reload_keeps_inflight_auth_failure(self):
        from key_pool import ServiceKeyPool
        row = self.server.db.add_key("reload-test", service="exa")
        pool = ServiceKeyPool()
        pool.reload("exa")
        selected = pool.get_next_key("exa")
        self.assertEqual(selected["id"], row["id"])
        pool.reload("exa")
        self.assertTrue(pool.report_result(
            "exa", row["id"], False, failure_kind="auth_rejected",
            generation=selected["_pool_generation"],
        ))
        updated = self.server.db.get_key_by_id(row["id"])
        self.assertFalse(updated["active"])
        self.assertEqual(updated["total_failed"], 1)
        self.assertIsNone(pool.get_next_key("exa"))

    def test_social_late_rate_limit_cannot_override_terminal_or_longer_cooldown(self):
        for module in (self.server, self.gateway):
            for reason in ("auth_rejected", "quota_exhausted", "rate_limited"):
                with self.subTest(module=module.__name__, reason=reason):
                    module.social_upstream_key_schedule.clear()
                    with patch.object(module.time, "time", return_value=1000):
                        module._schedule_social_upstream_key("test-client-key", reason, 120)
                    with patch.object(module.time, "time", return_value=1010):
                        module._schedule_social_upstream_key("test-client-key", "rate_limited", 5)
                    with patch.object(module.time, "time", return_value=1016):
                        self.assertEqual(module._available_social_upstream_keys(["test-client-key"]), [])
                        entry = module.social_upstream_key_schedule[module._social_key_fingerprint("test-client-key")]
                        self.assertEqual(entry["reason"], reason)
                        self.assertEqual(entry["until"], 1120 if reason == "rate_limited" else None)

    def test_usage_sync_preserves_manual_and_permanent_reasons(self):
        for reason in ("manual", "auth_rejected", "account_banned", "sponsor_verification_expired"):
            with self.subTest(reason=reason):
                row = self.server.db.add_key(f"preserve-{reason}", service="firecrawl")
                if reason == "manual":
                    self.server.db.toggle_key(row["id"], False)
                else:
                    self.server.db.update_key_usage(row["id"], False, failure_kind=reason, failure_detail="original cause")
                before = dict(self.server.db.get_key_by_id(row["id"]))
                for remaining in (0, 50):
                    self.server.db.update_key_remote_usage(row["id"], account_remaining=remaining)
                    after = self.server.db.get_key_by_id(row["id"])
                    self.assertFalse(after["active"])
                    for field in ("disabled_reason", "disabled_detail", "disabled_at"):
                        self.assertEqual(after[field], before[field])

    def test_only_confirmed_recovered_quota_reenables_keys(self):
        cases = [
            ("tavily", 50, 50, True),
            ("tavily", None, 50, False),
            ("tavily", 50, None, False),
            ("tavily", 0, 50, False),
            ("firecrawl", None, 50, True),
            ("exa", None, 50, False),
        ]
        for i, (service, key_remaining, account_remaining, enabled) in enumerate(cases):
            with self.subTest(service=service, key_remaining=key_remaining, account_remaining=account_remaining):
                row = self.server.db.add_key(f"recover-{i}", service=service)
                self.server.db.update_key_usage(row["id"], False, failure_kind="quota_exhausted")
                self.server.db.update_key_remote_usage(row["id"], key_remaining=key_remaining, account_remaining=account_remaining)
                after = self.server.db.get_key_by_id(row["id"])
                self.assertEqual(bool(after["active"]), enabled)
                if enabled:
                    self.assertEqual(after["disabled_reason"], "")
                    self.assertEqual(after["consecutive_fails"], 0)

    def test_late_quota_failure_cannot_make_auth_or_manual_disable_recoverable(self):
        for reason in ("manual", "auth_rejected", "account_banned", "sponsor_verification_expired"):
            with self.subTest(reason=reason):
                row = self.server.db.add_key(f"late-quota-{reason}", service="firecrawl")
                if reason == "manual":
                    self.server.db.toggle_key(row["id"], False)
                else:
                    self.server.db.update_key_usage(row["id"], False, failure_kind=reason)
                self.server.db.update_key_usage(row["id"], False, failure_kind="quota_exhausted")
                self.server.db.update_key_remote_usage(row["id"], account_remaining=50)
                after = self.server.db.get_key_by_id(row["id"])
                self.assertFalse(after["active"])
                self.assertEqual(after["disabled_reason"], reason)

        row = self.server.db.add_key("quota-then-auth", service="firecrawl")
        for reason in ("quota_exhausted", "auth_rejected"):
            self.server.db.update_key_usage(row["id"], False, failure_kind=reason)
        self.server.db.update_key_remote_usage(row["id"], account_remaining=50)
        after = self.server.db.get_key_by_id(row["id"])
        self.assertFalse(after["active"])
        self.assertEqual(after["disabled_reason"], "auth_rejected")

    def test_quota_sync_started_before_new_failure_cannot_reenable_key(self):
        row = self.server.db.add_key("stale-quota-sync", service="tavily")
        started = datetime.now(timezone.utc).isoformat()
        self.server.db.update_key_usage(row["id"], False, failure_kind="quota_exhausted")
        self.server.db.update_key_remote_usage(
            row["id"], key_remaining=50, account_remaining=50, sync_started_at=started,
        )
        self.assertFalse(self.server.db.get_key_by_id(row["id"])["active"])


if __name__ == "__main__":
    unittest.main()
