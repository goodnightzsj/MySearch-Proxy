from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


REPO_ROOT = Path(__file__).resolve().parents[1]
PROXY_ROOT = REPO_ROOT / "proxy"


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class ProxyTavilySettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if str(PROXY_ROOT) not in sys.path:
            sys.path.insert(0, str(PROXY_ROOT))
        cls.module = _load_module(
            "test_proxy_server_tavily_settings",
            PROXY_ROOT / "server.py",
        )

    def test_get_runtime_tavily_config_defaults_to_auto(self) -> None:
        with patch.object(self.module.db, "get_setting", side_effect=lambda _key, default=None: default):
            config = self.module.get_runtime_tavily_config()

        self.assertEqual(config["mode"], "auto")
        self.assertEqual(config["upstream_base_url"], "https://api.tavily.com")
        self.assertEqual(config["upstream_search_path"], "/search")
        self.assertEqual(config["upstream_extract_path"], "/extract")
        self.assertEqual(config["upstream_api_key"], "")
        self.assertEqual(config["upstream_admin_base_url"], "")
        self.assertEqual(config["upstream_admin_headers"], "")
        self.assertEqual(config["upstream_admin_cookie"], "")

    def test_get_runtime_tavily_config_reads_upstream_settings(self) -> None:
        values = {
            "tavily_mode": "upstream",
            "tavily_upstream_base_url": "http://127.0.0.1:8787/api/tavily",
            "tavily_upstream_search_path": "/search",
            "tavily_upstream_extract_path": "/extract",
            "tavily_upstream_api_key": "th-demo-token",
            "tavily_upstream_admin_base_url": "http://127.0.0.1:8787",
            "tavily_upstream_admin_headers": '{"X-Forwarded-User":"admin@example.com"}',
            "tavily_upstream_admin_cookie": "hikari_admin_session=test",
        }

        def fake_get_setting(key, default=None):
            return values.get(key, default)

        with patch.object(self.module.db, "get_setting", side_effect=fake_get_setting):
            config = self.module.get_runtime_tavily_config()

        self.assertEqual(config["mode"], "upstream")
        self.assertEqual(config["upstream_base_url"], "http://127.0.0.1:8787/api/tavily")
        self.assertEqual(config["upstream_api_key"], "th-demo-token")
        self.assertEqual(config["upstream_admin_base_url"], "http://127.0.0.1:8787")
        self.assertIn("X-Forwarded-User", config["upstream_admin_headers"])
        self.assertEqual(config["upstream_admin_cookie"], "hikari_admin_session=test")

    def test_usage_sync_meta_is_disabled_in_tavily_upstream_mode(self) -> None:
        values = {
            "tavily_mode": "upstream",
            "tavily_upstream_base_url": "http://127.0.0.1:8787/api/tavily",
            "tavily_upstream_api_key": "th-demo-token",
        }

        def fake_get_setting(key, default=None):
            return values.get(key, default)

        with patch.object(self.module.db, "get_setting", side_effect=fake_get_setting):
            meta = self.module.build_usage_sync_meta_for_dashboard("tavily", [{"id": 1}, {"id": 2}])

        self.assertFalse(meta["supported"])
        self.assertEqual(meta["requested"], 2)
        self.assertIn("上游 Gateway", meta["detail"])

    def test_probe_tavily_connection_falls_back_to_api_tavily_on_404(self) -> None:
        config = {
            "mode": "upstream",
            "upstream_base_url": "http://127.0.0.1:8787",
            "upstream_search_path": "/search",
            "upstream_extract_path": "/extract",
            "upstream_api_key": "gateway-token-without-th-prefix",
        }

        class _Response:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload
                self.headers = {"content-type": "application/json"}
                self.text = ""

            def json(self):
                return self._payload

        async def _run():
            responses = [
                _Response(404, {"detail": "Not Found"}),
                _Response(200, {"results": [{"title": "ok"}]}),
            ]
            call_urls = []

            async def fake_post(url, json):
                call_urls.append(url)
                return responses.pop(0)

            with patch.object(self.module, "http_client") as fake_client:
                fake_client.post.side_effect = fake_post
                return await self.module.probe_tavily_connection(config, [])

        result = asyncio.run(_run())
        self.assertTrue(result["ok"])
        self.assertEqual(result["request_target"], "http://127.0.0.1:8787/api/tavily/search")
        self.assertIn("/api/tavily", result["detail"])

    def test_console_pages_render_successfully(self) -> None:
        with TestClient(self.module.app) as client:
            response = client.get("/")
            mysearch_response = client.get("/mysearch")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers.get("content-type", ""))
        self.assertEqual(mysearch_response.status_code, 200)
        self.assertIn("text/html", mysearch_response.headers.get("content-type", ""))

    def test_fetch_tavily_upstream_summary_reads_public_summary_only(self) -> None:
        config = {
            "upstream_base_url": "http://127.0.0.1:8787/api/tavily",
            "upstream_api_key": "th-demo-token",
            "upstream_admin_base_url": "",
            "upstream_admin_headers": "",
            "upstream_admin_cookie": "",
        }

        class _Response:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload
                self.headers = {"content-type": "application/json"}
                self.text = ""

            def json(self):
                return self._payload

        async def _run():
            async def fake_get(url, headers=None):
                self.assertEqual(url, "http://127.0.0.1:8787/api/summary")
                self.assertIsNone(headers)
                return _Response(
                    200,
                    {
                        "active_keys": 2,
                        "exhausted_keys": 1,
                        "quarantined_keys": 0,
                        "total_requests": 30,
                        "success_count": 25,
                        "error_count": 5,
                        "quota_exhausted_count": 1,
                        "total_quota_limit": 3000,
                        "total_quota_remaining": 1800,
                    },
                )

            with patch.object(self.module, "http_client") as fake_client:
                fake_client.get.side_effect = fake_get
                return await self.module.fetch_tavily_upstream_summary(config)

        result = asyncio.run(_run())
        self.assertTrue(result["available"])
        self.assertEqual(result["summary_source"], "hikari_public_summary")
        self.assertEqual(result["capability_level"], "public_summary")
        self.assertFalse(result["key_detail_supported"])
        self.assertIn("公共摘要", result["detail"])

    def test_fetch_tavily_upstream_summary_aggregates_admin_keys_when_configured(self) -> None:
        config = {
            "upstream_base_url": "http://127.0.0.1:8787/api/tavily",
            "upstream_api_key": "th-demo-token",
            "upstream_admin_base_url": "http://127.0.0.1:8787",
            "upstream_admin_headers": '{"X-Forwarded-User":"admin@example.com","X-Forwarded-Admin":"true"}',
            "upstream_admin_cookie": "",
        }

        class _Response:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload
                self.headers = {"content-type": "application/json"}
                self.text = ""

            def json(self):
                return self._payload

        async def _run():
            async def fake_get(url, headers=None):
                if url == "http://127.0.0.1:8787/api/summary":
                    return _Response(200, {"active_keys": 9, "total_quota_remaining": 9999, "last_activity": "2026-04-16T12:00:00Z"})
                if url == "http://127.0.0.1:8787/api/keys":
                    self.assertEqual(headers["X-Forwarded-User"], "admin@example.com")
                    self.assertEqual(headers["X-Forwarded-Admin"], "true")
                    return _Response(
                        200,
                        [
                            {"status": "active", "quota_limit": 1000, "quota_remaining": 500, "total_requests": 10, "success_count": 8, "error_count": 2, "quota_exhausted_count": 0},
                            {"status": "exhausted", "quota_limit": 1000, "quota_remaining": 0, "total_requests": 12, "success_count": 10, "error_count": 2, "quota_exhausted_count": 1},
                            {"status": "active", "quota_limit": 2000, "quota_remaining": 1500, "total_requests": 20, "success_count": 19, "error_count": 1, "quota_exhausted_count": 0},
                        ],
                    )
                raise AssertionError(f"unexpected url: {url}")

            with patch.object(self.module, "http_client") as fake_client:
                fake_client.get.side_effect = fake_get
                return await self.module.fetch_tavily_upstream_summary(config)

        result = asyncio.run(_run())
        self.assertTrue(result["available"])
        self.assertEqual(result["summary_source"], "hikari_admin_keys")
        self.assertEqual(result["capability_level"], "admin_keys")
        self.assertTrue(result["key_detail_supported"])
        self.assertTrue(result["admin_connected"])
        self.assertEqual(result["active_keys"], 2)
        self.assertEqual(result["exhausted_keys"], 1)
        self.assertEqual(result["total_quota_limit"], 4000)
        self.assertEqual(result["total_quota_remaining"], 2000)


if __name__ == "__main__":
    unittest.main()
