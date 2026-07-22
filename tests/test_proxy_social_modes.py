from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx


REPO_ROOT = Path(__file__).resolve().parents[1]
PROXY_ROOT = REPO_ROOT / "proxy"


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class ProxySocialModeTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if str(PROXY_ROOT) not in sys.path:
            sys.path.insert(0, str(PROXY_ROOT))
        cls.server = _load_module(
            "test_proxy_server_social_modes",
            PROXY_ROOT / "server.py",
        )

    def _base_config(self, **overrides):
        config = {
            "mode": "local",
            "mode_source": "configured",
            "local_base_url": "https://api.x.ai/v1",
            "local_responses_path": "/responses",
            "local_api_key": "local-key",
            "upstream_base_url": "https://gateway.example/v1",
            "upstream_responses_path": "/responses",
            "upstream_api_key": "upstream-key",
            "admin_base_url": "https://gateway.example",
            "admin_verify_path": "/v1/admin/verify",
            "admin_config_path": "/admin/api/config",
            "admin_tokens_path": "/admin/api/tokens",
            "admin_username": "",
            "admin_password": "",
            "admin_app_key": "",
            "gateway_token": "client-token",
            "model": "grok-4.20-0309",
            "fallback_model": "grok-4.3",
            "fallback_min_results": 3,
            "cache_ttl_seconds": 60,
        }
        config.update(overrides)
        return config

    def test_legacy_manual_config_becomes_local_without_losing_key_or_url(self) -> None:
        values = {
            "social_upstream_base_url": "https://legacy.example/v1",
            "social_upstream_responses_path": "/responses",
            "social_upstream_api_key": "legacy-key",
        }

        with patch.object(
            self.server.db,
            "get_setting",
            side_effect=lambda key, default=None: values.get(key, default),
        ):
            config = self.server.get_runtime_social_config()

        self.assertEqual(config["mode"], "local")
        self.assertEqual(config["mode_source"], "legacy_local")
        self.assertEqual(config["local_base_url"], "https://legacy.example/v1")
        self.assertEqual(config["local_api_key"], "legacy-key")

    def test_legacy_admin_config_becomes_upstream_without_copying_key_to_local_pool(self) -> None:
        values = {
            "social_upstream_api_key": "g2a-upstream-key",
            "social_admin_username": "admin",
            "social_admin_password": "secret",
        }

        with patch.object(
            self.server.db,
            "get_setting",
            side_effect=lambda key, default=None: values.get(key, default),
        ):
            config = self.server.get_runtime_social_config()

        self.assertEqual(config["mode"], "upstream")
        self.assertEqual(config["mode_source"], "legacy_upstream")
        self.assertEqual(config["upstream_api_key"], "g2a-upstream-key")
        self.assertEqual(config["local_api_key"], "")

    def test_explicit_local_mode_never_reuses_upstream_key(self) -> None:
        values = {
            "social_mode": "local",
            "social_upstream_api_key": "upstream-key",
        }

        with patch.object(
            self.server.db,
            "get_setting",
            side_effect=lambda key, default=None: values.get(key, default),
        ):
            config = self.server.get_runtime_social_config()

        self.assertEqual(config["mode"], "local")
        self.assertEqual(config["local_api_key"], "")
        self.assertEqual(config["upstream_api_key"], "upstream-key")

    def test_local_environment_key_precedes_legacy_fallback(self) -> None:
        values = {"social_upstream_api_key": "legacy-key"}

        with patch.object(
            self.server.db,
            "get_setting",
            side_effect=lambda key, default=None: values.get(key, default),
        ), patch.object(
            self.server,
            "SOCIAL_GATEWAY_LOCAL_API_KEY",
            "environment-local-key",
        ):
            config = self.server.get_runtime_social_config()

        self.assertEqual(config["mode"], "local")
        self.assertEqual(config["local_api_key"], "environment-local-key")

    async def test_local_mode_does_not_contact_admin_or_use_upstream_key(self) -> None:
        config = self._base_config(
            mode="local",
            admin_username="admin",
            admin_password="secret",
        )

        with patch.object(
            self.server,
            "fetch_social_admin_v3_state",
            new=AsyncMock(side_effect=AssertionError("local mode must not contact admin")),
        ) as fetch_admin:
            state = await self.server.resolve_social_gateway_state_for_config(config)

        fetch_admin.assert_not_awaited()
        self.assertEqual(state["mode"], "local")
        self.assertEqual(state["upstream_base_url"], "https://api.x.ai/v1")
        self.assertEqual(state["upstream_api_keys"], ["local-key"])
        self.assertNotIn("upstream-key", state["upstream_api_keys"])
        self.assertFalse(state["admin_connected"])

    async def test_upstream_mode_does_not_use_local_key_pool(self) -> None:
        state = await self.server.resolve_social_gateway_state_for_config(
            self._base_config(mode="upstream")
        )

        self.assertEqual(state["mode"], "upstream")
        self.assertEqual(state["upstream_base_url"], "https://gateway.example/v1")
        self.assertEqual(state["upstream_api_keys"], ["upstream-key"])
        self.assertNotIn("local-key", state["upstream_api_keys"])

    async def test_local_search_uses_only_local_target_and_key(self) -> None:
        state = await self.server.resolve_social_gateway_state_for_config(
            self._base_config(mode="local")
        )
        response = httpx.Response(
            200,
            json={"output_text": '{"answer":"ok","results":[]}'},
        )

        with patch.object(
            self.server.http_client,
            "post",
            new=AsyncMock(return_value=response),
        ) as post:
            result = await self.server.execute_social_search_attempt(
                "test",
                {},
                state,
                "grok-test",
                5,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(post.await_args.args[0], "https://api.x.ai/v1/responses")
        self.assertEqual(
            post.await_args.kwargs["headers"]["Authorization"],
            "Bearer local-key",
        )

    async def test_upstream_search_uses_only_upstream_target_and_key(self) -> None:
        state = await self.server.resolve_social_gateway_state_for_config(
            self._base_config(mode="upstream")
        )
        response = httpx.Response(
            200,
            json={"output_text": '{"answer":"ok","results":[]}'},
        )

        with patch.object(
            self.server.http_client,
            "post",
            new=AsyncMock(return_value=response),
        ) as post:
            result = await self.server.execute_social_search_attempt(
                "test",
                {},
                state,
                "grok-test",
                5,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(post.await_args.args[0], "https://gateway.example/v1/responses")
        self.assertEqual(
            post.await_args.kwargs["headers"]["Authorization"],
            "Bearer upstream-key",
        )

    async def test_empty_local_url_never_falls_back_to_upstream_target(self) -> None:
        config = self._base_config(
            mode="local",
            local_base_url="",
            local_responses_path="",
        )

        state = await self.server.resolve_social_gateway_state_for_config(config)

        self.assertEqual(state["upstream_base_url"], "https://api.x.ai/v1")
        self.assertEqual(state["upstream_responses_path"], "/responses")

    async def test_settings_reject_unknown_mode_before_persisting_it(self) -> None:
        class Request:
            async def json(self):
                return {"mode": "hybrid"}

        with patch.object(self.server.db, "set_setting") as set_setting:
            with self.assertRaises(self.server.HTTPException) as raised:
                await self.server.update_social_settings(Request())

        self.assertEqual(raised.exception.status_code, 400)
        set_setting.assert_not_called()

    async def test_settings_validate_all_fields_before_persisting_mode(self) -> None:
        class Request:
            async def json(self):
                return {"mode": "local", "cache_ttl_seconds": "invalid"}

        with patch.object(self.server.db, "set_setting") as set_setting:
            with self.assertRaises(self.server.HTTPException) as raised:
                await self.server.update_social_settings(Request())

        self.assertEqual(raised.exception.status_code, 400)
        set_setting.assert_not_called()

    async def test_settings_persist_mode_and_local_connector_independently(self) -> None:
        class Request:
            async def json(self):
                return {
                    "mode": "local",
                    "local_base_url": "https://api.x.ai/v1",
                    "local_responses_path": "/responses",
                    "local_api_key": "new-local-key",
                }

        with patch.object(self.server.db, "set_setting") as set_setting, patch.object(
            self.server,
            "build_settings_payload",
            new=AsyncMock(return_value={"social": {"mode": "local"}}),
        ):
            result = await self.server.update_social_settings(Request())

        calls = {call.args for call in set_setting.call_args_list}
        self.assertIn(("social_mode", "local"), calls)
        self.assertIn(("social_local_base_url", "https://api.x.ai/v1"), calls)
        self.assertIn(("social_local_responses_path", "/responses"), calls)
        self.assertIn(("social_local_api_key", "new-local-key"), calls)
        self.assertNotIn(("social_upstream_api_key", "new-local-key"), calls)
        self.assertEqual(result["social"]["mode"], "local")

    async def test_first_local_save_migrates_legacy_key_without_reentry(self) -> None:
        values = {
            "social_upstream_base_url": "https://legacy.example/v1",
            "social_upstream_responses_path": "/responses",
            "social_upstream_api_key": "legacy-key",
        }

        class Request:
            async def json(self):
                return {"mode": "local"}

        with patch.object(
            self.server.db,
            "get_setting",
            side_effect=lambda key, default=None: values.get(key, default),
        ), patch.object(self.server.db, "set_setting") as set_setting, patch.object(
            self.server,
            "build_settings_payload",
            new=AsyncMock(return_value={"social": {"mode": "local"}}),
        ):
            await self.server.update_social_settings(Request())

        calls = {call.args for call in set_setting.call_args_list}
        self.assertIn(("social_mode", "local"), calls)
        self.assertIn(("social_local_api_key", "legacy-key"), calls)


if __name__ == "__main__":
    unittest.main()
