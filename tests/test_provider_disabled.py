from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "proxy"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import database as db
from mysearch.clients import MySearchClient, MySearchHTTPError, RouteDecision
from mysearch.config import MySearchConfig


def fixture_config(**env):
    with patch.dict(os.environ, env, clear=True):
        config = MySearchConfig.from_env()
    for provider in (config.tavily, config.firecrawl, config.exa, config.xai):
        provider.keys_file = None
        provider.api_keys = [f"fixture-{provider.name}"]
    return config


class DisabledProviderTests(unittest.TestCase):
    def client(self, **env):
        client = MySearchClient(fixture_config(**env))
        self.addCleanup(client.close)
        return client

    def test_default_disable_applies_to_direct_and_proxy_config_without_erasing_credentials(self):
        for env in ({}, {"MYSEARCH_PROXY_BASE_URL": "http://fixture", "MYSEARCH_PROXY_API_KEY": "fixture-token"}):
            with self.subTest(env=tuple(env)):
                client = self.client(**env)
                self.assertFalse(client.config.exa.enabled)
                self.assertEqual(client.config.exa.api_keys, ["fixture-exa"])
                self.assertFalse(client.keyring.has_provider("exa"))
                self.assertEqual(client.keyring.describe()["exa"]["count"], 0)

    def test_explicit_enable_retains_exa_key_configuration(self):
        client = self.client(MYSEARCH_EXA_ENABLED="true")
        self.assertTrue(client.config.exa.enabled)
        self.assertEqual(client._get_key_or_raise(client.config.exa).key, "fixture-exa")

    def test_disabled_provider_files_are_not_read(self):
        config = fixture_config()
        config.exa.keys_file = Path("/fixture/disabled-keys")
        with patch.object(Path, "exists", side_effect=AssertionError("disabled file was accessed")):
            client = MySearchClient(config)
        self.addCleanup(client.close)
        self.assertFalse(client.keyring.has_provider("exa"))

    def test_health_reports_disabled_without_an_exa_probe(self):
        client = self.client()
        with patch.object(client, "_probe_provider_request") as probe:
            health = client.health()
        self.assertEqual({call.args[0].name for call in probe.call_args_list}, {"tavily", "firecrawl", "xai"})
        self.assertFalse(health["providers"]["exa"]["enabled"])
        self.assertEqual(health["providers"]["exa"]["live_status"], "disabled")
        self.assertEqual(health["routing_defaults"]["fallback"], "firecrawl")
        self.assertFalse(client._provider_can_serve(client.config.exa))

    def test_explicit_search_fails_before_cache_or_network(self):
        client = self.client()
        with patch.object(client, "_cache_get") as cache, patch.object(client._http, "request") as send:
            with self.assertRaises(MySearchHTTPError) as caught:
                client.search(query="fixture", provider="exa")
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.detail["code"], "provider_disabled")
        cache.assert_not_called()
        send.assert_not_called()

    def test_transport_rejects_disabled_provider_even_when_key_was_supplied(self):
        client = self.client()
        with patch.object(client._http, "request") as send:
            with self.assertRaises(MySearchHTTPError) as caught:
                client._request_json(provider=client.config.exa, method="POST", path="/contents", payload={}, key="fixture-exa")
        self.assertEqual(caught.exception.detail["code"], "provider_disabled")
        send.assert_not_called()

    def test_route_and_fallback_exclude_disabled_provider_even_if_all_probes_fail(self):
        client = self.client()
        with patch.object(client, "_provider_live_status", return_value=None):
            decision = client._route_search(query="fixture comparison", mode="web", intent="comparison", provider="auto", sources=["web"], include_content=False, include_domains=None, allowed_x_handles=None, excluded_x_handles=None)
        self.assertNotEqual(decision.provider, "exa")
        self.assertNotIn("exa", decision.fallback_chain or [])
        self.assertFalse(decision.allow_exa_rescue)
        dispatch = Mock(return_value={"provider": "firecrawl", "results": [{"url": "https://fixture.test/", "content": "fixture body"}], "citations": []})
        with patch.object(client, "_dispatch_single_provider", dispatch):
            client._search_with_fallback(primary_provider="firecrawl", query="fixture", max_results=1, mode="web", intent="factual", decision=RouteDecision(provider="firecrawl", reason="fixture", fallback_chain=["exa", "tavily"]), include_answer=False, include_content=False, include_domains=None, exclude_domains=None)
        self.assertEqual([call.kwargs["provider_name"] for call in dispatch.call_args_list], ["firecrawl"])

    def test_search_extract_and_social_cache_identity_includes_policy(self):
        client = self.client()
        def keys():
            return (
                client._build_search_cache_key(query="fixture", mode="web", resolved_intent="factual", resolved_strategy="fast", provider="auto", normalized_sources=["web"], include_content=False, include_answer=False, include_domains=None, exclude_domains=None, decision=RouteDecision(provider="tavily", reason="fixture")),
                client._build_extract_cache_key(url="https://fixture.test/", formats=["markdown"], only_main_content=True, provider="auto"),
                client._build_social_cache_key(query="fixture", max_results=1, allowed_x_handles=None, excluded_x_handles=None, from_date=None, to_date=None, include_x_images=False, include_x_videos=False),
            )
        disabled = keys()
        client.config.exa.enabled = True
        enabled = keys()
        self.assertTrue(all(left != right for left, right in zip(disabled, enabled)))


class DisabledProxyTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("disabled_provider_server", ROOT / "proxy/server.py")
        cls.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.server)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.enterContext(patch.dict(os.environ, {"MYSEARCH_PROXY_DB_PATH": str(Path(temp.name) / "fixture.db"), "MYSEARCH_EXA_ENABLED": "false"}))
        db.close_conn()
        db.init_db()
        self.addCleanup(db.close_conn)
        db.add_key("fixture-exa", service="exa")
        self.token = db.create_token("fixture-client", service="mysearch")["token"]

    async def test_search_and_contents_preserve_data_and_do_not_select_or_send(self):
        before = [tuple(row) for row in db.get_conn().execute("SELECT id,active,blocked_operations,total_used,total_failed FROM api_keys")]
        with patch.object(self.server.pool, "get_next_key") as select, patch.object(self.server.http_client, "post", AsyncMock()) as send:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://fixture") as client:
                for path in ("/exa/search", "/exa/contents"):
                    response = await client.post(path, headers={"Authorization": f"Bearer {self.token}"}, json={})
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json()["detail"]["code"], "provider_disabled")
        select.assert_not_called()
        send.assert_not_awaited()
        after = [tuple(row) for row in db.get_conn().execute("SELECT id,active,blocked_operations,total_used,total_failed FROM api_keys")]
        self.assertEqual(before, after)
        self.assertEqual(db.get_conn().execute("SELECT count(*) FROM usage_logs").fetchone()[0], 0)

    async def test_disabled_endpoint_still_requires_valid_authentication(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://fixture") as client:
            response = await client.post("/exa/search", json={})
        self.assertEqual(response.status_code, 401)

    async def test_console_policy_and_count_follow_shared_flag(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://fixture") as client:
            for enabled in (False, True):
                with self.subTest(enabled=enabled), patch.dict(os.environ, {"MYSEARCH_EXA_ENABLED": str(enabled).lower()}):
                    for path in ("/", "/mysearch"):
                        response = await client.get(path)
                        self.assertEqual(response.status_code, 200)
                        self.assertIn(f"window.EXA_ENABLED = {str(enabled).lower()};", response.text)
                        if path == "/":
                            self.assertIn(f"{4 if enabled else 3} Providers", response.text)


@unittest.skipUnless(shutil.which("node"), "Node.js is required for console behavior checks")
class DisabledConsoleTests(unittest.TestCase):
    def test_policy_filters_navigation_cards_and_persisted_exa_preference(self):
        source = (ROOT / "proxy/static/js/console.js").read_text(encoding="utf-8")
        policy = source[source.index("const SERVICE_META ="):source.index("let latestServices =")]
        functions = []
        for name in ("renderServiceSwitcher", "getQuickstartProviderCards"):
            start = re.search(rf"^function {name}\(", source, re.MULTILINE).start()
            end = start + re.search(r"^}$", source[start:], re.MULTILINE).end()
            functions.append(source[start:end])
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                harness = f"""
const assert = require('node:assert/strict');
const window = {{EXA_ENABLED: {str(enabled).lower()}}};
const storage = new Map([['workspace', 'exa']]);
const ACTIVE_SERVICE_KEY = 'workspace', STORAGE_KEY = 'password', LEGACY_STORAGE_KEY = 'old-password';
const localStorage = {{getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)}};
const root = {{innerHTML: ''}}, document = {{getElementById: () => root}};
const getWorkspaceSnapshot = () => ({{}}), workspaceSignal = () => ({{tone: 'ok'}});
const fmtNum = value => String(value), escapeHtml = value => String(value);
const getTavilyRuntimeState = () => ({{effectiveMode: 'local'}}), getTavilyUpstreamSummary = () => ({{}});
const getSocialUpstreamState = () => ({{canProxySearch: true}}), socialTokenSourceLabel = () => 'fixture';
const tavilyModeSourceLabel = () => 'local', getServiceDisplayLabel = value => SERVICE_META[value]?.label || value;
const latestServices = {{tavily: {{keys_active: 1}}, exa: {{keys_active: 1, keys_total: 1}}, firecrawl: {{keys_active: 1, keys_total: 1}}}};
const latestSocial = {{mode: 'local'}};
"""
                assertions = """
assert.equal(Boolean(SERVICE_META.exa), window.EXA_ENABLED);
assert.equal(Boolean(WORKSPACE_META.exa), window.EXA_ENABLED);
assert.equal(activeService, window.EXA_ENABLED ? 'exa' : 'tavily');
assert.equal(storage.get(ACTIVE_SERVICE_KEY), activeService);
renderServiceSwitcher(latestServices, latestSocial);
assert.equal(root.innerHTML.includes('data-service="exa"'), window.EXA_ENABLED);
const cards = getQuickstartProviderCards();
assert.equal(cards.length, window.EXA_ENABLED ? 4 : 3);
assert.equal(cards.some(card => card.label === 'Exa'), window.EXA_ENABLED);
"""
                result = subprocess.run(["node"], input=harness + policy + "\n".join(functions) + assertions, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
