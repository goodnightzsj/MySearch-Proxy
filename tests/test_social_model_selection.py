"""持续选模只依赖搜索交付证据，绝不把目录、普通推理或单次成功当上线门槛。"""
import asyncio
import fcntl
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from mysearch.grok_model_refresh import (
    choose_probe_candidates, collect_text_candidates, is_eligible_model,
    select_measured_models, summarize_model_probes, update_probe_history,
)
from test_social_model_refresh import _load_server_module, PROXY_ROOT


class SelectionTests(unittest.TestCase):
    now = 2_000_000_000

    def history(self, model="Console/grok-4.5", latency=10000, successes=(True, True, True)):
        return {model: [{"at": self.now - (len(successes) - i - 1) * 21601,
                         "search_capable": ok, "latency_ms": latency,
                         "reason": "timeout" if not ok else ""}
                        for i, ok in enumerate(successes)]}

    def test_route_ids_and_multimodal_filter(self):
        self.assertTrue(is_eligible_model("Console/grok-4.5"))
        self.assertTrue(is_eligible_model("Build/grok-4.7"))
        for value in ("Console/grok-voice-latest", "Web/grok-imagine-image", "x/grok-4.5", "grok-x\n"):
            self.assertFalse(is_eligible_model(value))
        self.assertEqual(collect_text_candidates({"items": [
            {"publicId": "Console/grok-4.5", "capability": "responses", "enabled": True},
            {"publicId": "Console/grok-4.3", "capability": "responses", "enabled": False},
        ]}), ["Console/grok-4.5"])

    def test_single_success_or_same_hour_never_promotes(self):
        for history in (self.history(successes=(True,)), self.history()):
            for row in history["Console/grok-4.5"]:
                row["at"] = self.now
            ranking = summarize_model_probes(history, list(history), self.now)
            self.assertEqual(select_measured_models(ranking, "old", "backup")[:2], ("old", "backup"))

    def test_failure_latency_does_not_improve_success_latency(self):
        history = self.history(successes=(True, True, True, True, False))
        history["Console/grok-4.5"][-1]["latency_ms"] = 1
        row = summarize_model_probes(history, list(history), self.now)[0]
        self.assertEqual(row["p90_ms"], 10000)
        self.assertEqual(row["failures"], {"timeout": 1})
        self.assertFalse(row["qualified"])

    def test_old_failure_does_not_satisfy_success_observation_window(self):
        history = self.history(successes=(False, True, True, True, True))
        for row in history["Console/grok-4.5"][1:]:
            row["at"] = self.now
        ranking = summarize_model_probes(history, list(history), self.now)
        self.assertFalse(ranking[0]["qualified"])
        self.assertEqual(select_measured_models(ranking, "old", "backup")[:2], ("old", "backup"))

    def test_material_latency_improvement_promotes_new_model(self):
        history = {**self.history("Console/grok-4.3", 20000), **self.history("Console/grok-4.5", 10000)}
        ranking = summarize_model_probes(history, list(history), self.now)
        self.assertEqual(select_measured_models(ranking, "Console/grok-4.3", "")[:2],
                         ("Console/grok-4.5", "Console/grok-4.3"))

    def test_small_improvement_keeps_incumbent(self):
        history = {**self.history("grok-4.3", 20000), **self.history("grok-4.5", 19000)}
        ranking = summarize_model_probes(history, list(history), self.now)
        self.assertEqual(select_measured_models(ranking, "grok-4.3", "")[0], "grok-4.3")

    def test_expired_history_and_future_samples_not_qualified(self):
        history = self.history()
        for offset in (-15 * 86400, 100000):
            shifted = {model: [{**row, "at": row["at"] + offset} for row in rows] for model, rows in history.items()}
            self.assertFalse(summarize_model_probes(shifted, list(shifted), self.now)[0]["qualified"])

    def test_history_is_bounded_and_does_not_persist_response_or_key(self):
        history = {}
        for i in range(25):
            history = update_probe_history(history, [{"model": "grok-4.5", "search_capable": True,
                                                     "latency_ms": 1000, "response": "private", "key": "secret"}], self.now + i)
        self.assertEqual(len(history["grok-4.5"]), 20)
        self.assertNotIn("private", json.dumps(history))
        self.assertNotIn("secret", json.dumps(history))

    def test_probe_rotation_does_not_starve_ninth_candidate(self):
        candidates = [f"grok-4.{i}" for i in range(12)]
        history = {model: [{"at": self.now}] for model in candidates[:8]}
        selected = choose_probe_candidates(candidates, history, candidates[0], candidates[1])
        self.assertEqual(selected[:2], candidates[:2])
        self.assertIn(candidates[8], selected)
        self.assertEqual(len(selected), 8)


class RefreshIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        import sys
        if str(PROXY_ROOT) not in sys.path:
            sys.path.insert(0, str(PROXY_ROOT))
        cls.server = _load_server_module("test_measured_selection_server")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict("os.environ", {"MYSEARCH_PROXY_DB_PATH": str(Path(self.tmp.name) / "proxy.db")})
        self.env.start()
        self.db_local = patch.object(self.server.db, "_thread_local", threading.local())
        self.db_local.start()
        self.server.db.init_db()

    async def asyncTearDown(self):
        self.server.db.close_conn()
        self.db_local.stop()
        self.env.stop()
        self.tmp.cleanup()

    async def test_sync_requires_complete_event(self):
        server = self.server
        for body, succeeds in ((": heartbeat\n\n", False),
                               ('event: error\ndata: {"code":"modelSyncFailed"}\n\n', False),
                               ('event: complete\ndata: {"synced":4}\n\n', True),
                               ('event: complete\ndata: {"synced":true}\n\n', False)):
            response = httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
            with patch.object(server.http_client, "post", AsyncMock(return_value=response)):
                if succeeds:
                    self.assertEqual(await server.sync_social_upstream_models({"admin_base_url": "http://upstream"}, "test"), {"ok": True, "synced": 4})
                else:
                    with self.assertRaises((RuntimeError, ValueError)):
                        await server.sync_social_upstream_models({"admin_base_url": "http://upstream"}, "test")

    async def test_probe_requires_named_search_tool_and_trusted_citations(self):
        server = self.server
        url = "https://x.com/AnthropicAI/status/1970558198109126942"
        payload = {"output": [{"type": "custom_tool_call", "name": "x_keyword_search"},
                              {"type": "message", "content": [{"type": "output_text", "text": url,
                                "annotations": [{"type": "url_citation", "url": url}]}]}]}
        for kind, name, citations, expected in (("custom_tool_call", "x_keyword_search", True, True),
                                               ("custom_tool_call", "shell", True, False),
                                               ("custom_tool_call", "x_keyword_search", False, False)):
            payload["output"][0] = {"type": kind, "name": name}
            payload["output"][1]["content"][0]["annotations"] = [{"url": url}] if citations else []
            with patch.object(server.http_client, "post", AsyncMock(return_value=httpx.Response(200, json=payload))):
                ok, evidence = await server.probe_social_model("http://upstream", "test", "Console/grok-4.5")
            self.assertEqual(ok, expected)
            self.assertIn("latency_ms", evidence)

    async def test_network_and_rate_limit_are_not_model_capability_failures(self):
        for code, message, reason in ((502, "socks connect failed", "network_socks"), (429, "limited", "rate_limited")):
            with patch.object(self.server.http_client, "post", AsyncMock(return_value=httpx.Response(code, text=message))):
                ok, result = await self.server.probe_social_model("http://upstream", "test", "grok-4.5")
            self.assertFalse(ok)
            self.assertEqual(result["reason"], reason)

    async def test_lock_prevents_overlapping_refresh(self):
        with open(self.server.db.get_db_path() + ".model-refresh.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = await self.server.probe_and_refresh_social_models()
        self.assertEqual(result["reason"], "refresh_already_running")

    async def test_atomic_settings_reject_stale_baseline(self):
        db = self.server.db
        db.set_setting("social_model", "manual-choice")
        self.assertFalse(db.set_settings({"social_model": "automatic", "social_fallback_model": "new"}, expected={"social_model": "old"}))
        self.assertEqual(db.get_setting("social_model"), "manual-choice")
        self.assertIsNone(db.get_setting("social_fallback_model"))

    async def test_empty_fallback_stays_empty(self):
        self.server.db.set_setting("social_fallback_model", "")
        self.assertEqual(self.server.get_runtime_social_config()["fallback_model"], "")

    async def test_admin_auth_required_for_both_endpoints(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://test") as client:
            self.assertEqual((await client.get("/api/settings/social/models")).status_code, 401)
            self.assertEqual((await client.post("/api/settings/social/models/refresh")).status_code, 401)

    def configure(self):
        self.server.db.set_settings({
            "social_mode": "upstream", "social_upstream_base_url": "http://upstream/v1",
            "social_upstream_responses_path": "/responses", "social_upstream_api_key": "test-key",
            "social_admin_base_url": "http://upstream", "social_admin_username": "test-user",
            "social_admin_password": "test-password", "social_model": "grok-old", "social_fallback_model": "grok-backup",
        })

    async def test_sync_then_probe_then_promote_and_reload_history(self):
        self.configure()
        server = self.server
        events = []
        async def sync(*args):
            events.append("sync")
            return {"ok": True, "synced": 1}
        async def probe(*args, **kwargs):
            events.append("probe")
            return True, {"latency_ms": 12000, "status_ids": 3, "tool_calls": 1}
        with patch.object(server, "get_social_admin_v3_access_token", AsyncMock(return_value="test")), \
             patch.object(server, "sync_social_upstream_models", side_effect=sync), \
             patch.object(server, "collect_social_model_candidates", AsyncMock(return_value=(["grok-new"], []))), \
             patch.object(server, "probe_social_model", side_effect=probe):
            for index in range(3):
                with patch.object(server.time, "time", return_value=2_000_000_000 + index * 21601):
                    result = await server.probe_and_refresh_social_models()
                self.assertEqual(result["primary"], "grok-new" if index == 2 else "grok-old")
        self.assertEqual(events, ["sync", "probe", "probe", "probe"])
        server.db.close_conn()
        state = server.get_social_model_selection_state()
        self.assertEqual(len(state["history"]["grok-new"]), 3)
        self.assertEqual(server.get_runtime_social_config()["fallback_model"], "")

    async def test_config_changed_during_probe_preserves_manual_choice(self):
        self.configure()
        server = self.server
        async def probe(*args, **kwargs):
            server.db.set_setting("social_model", "manual")
            return True, {"latency_ms": 1000}
        with patch.object(server, "get_social_admin_v3_access_token", AsyncMock(return_value="test")), \
             patch.object(server, "sync_social_upstream_models", AsyncMock(return_value={"ok": True})), \
             patch.object(server, "collect_social_model_candidates", AsyncMock(return_value=(["grok-new"], []))), \
             patch.object(server, "probe_social_model", side_effect=probe):
            result = await server.probe_and_refresh_social_models()
        self.assertEqual(result["reason"], "configuration_changed_during_probe")
        self.assertEqual(server.db.get_setting("social_model"), "manual")

    async def test_cancellation_releases_file_lock_and_records_attempt(self):
        self.configure()
        server = self.server
        with patch.object(server, "get_social_admin_v3_access_token", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await server.probe_and_refresh_social_models()
        self.assertTrue(server.get_social_model_selection_state()["last_probe_attempt_at"])
        with open(server.db.get_db_path() + ".model-refresh.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    async def test_catalog_fetches_second_page(self):
        pages = [
            {"items": [{"publicId": "grok-a", "capability": "responses"}], "total": 2, "pageSize": 1},
            {"items": [{"publicId": "Console/grok-b", "capability": "responses"}], "total": 2, "pageSize": 1},
        ]
        with patch.object(self.server, "fetch_social_admin_v3_json", AsyncMock(side_effect=pages)) as fetch, \
             patch.object(self.server, "fetch_social_upstream_json", AsyncMock(return_value={"data": []})):
            candidates, _ = await self.server.collect_social_model_candidates({}, "http://upstream", "test", "admin-test")
        self.assertEqual(set(candidates), {"grok-a", "Console/grok-b"})
        self.assertEqual(fetch.await_count, 2)


if __name__ == "__main__":
    unittest.main()
