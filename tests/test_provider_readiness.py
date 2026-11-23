from __future__ import annotations

import unittest
from unittest.mock import patch

from mysearch.clients import MySearchClient, RouteDecision
from mysearch.errors import MySearchError, MySearchHTTPError
from test_provider_disabled import fixture_config


class ProviderReadinessTests(unittest.TestCase):
    def setUp(self):
        self.client = MySearchClient(fixture_config(MYSEARCH_EXA_ENABLED="true"))
        self.addCleanup(self.client.close)

    def search(self):
        return self.client._search_with_fallback(
            primary_provider="exa", query="Generic reference", max_results=3, mode="web", intent="factual",
            decision=RouteDecision(provider="exa", reason="test", fallback_chain=["tavily", "firecrawl"]),
            include_answer=False, include_content=False, include_domains=None, exclude_domains=None,
        )

    def test_gateway_token_count_and_public_health_are_not_operation_proof(self):
        client = self.client
        client.config.xai.managed_key_pool = True
        for evidence in ("public_status", "service_health", "operation"):
            status = {"status": "ok", "error": "", "checked_at": "2026-11-23T00:00:00Z", "evidence": evidence}
            with patch.object(client, "_probe_provider_status", return_value=status):
                provider = client.health()["providers"]["xai"]
            self.assertEqual(provider["available_keys"], 1)
            self.assertEqual(provider["credential_scope"], "gateway")
            self.assertEqual(provider["probe_evidence"], evidence)
            self.assertIs(provider["operation_ready"], True if evidence == "operation" else None)

    def test_known_empty_upstream_pool_skips_without_another_probe_and_expires(self):
        client = self.client
        failure = MySearchHTTPError(provider="exa", status_code=503,
            detail="No available Exa API keys for search; manual key action required", url="https://gateway.test/search")
        with patch("mysearch.cache.time.monotonic", return_value=100), patch.object(
            client, "_probe_provider_request", side_effect=failure,
        ) as probe:
            self.assertFalse(client._provider_can_serve(client.config.exa))
            with patch.object(client, "_dispatch_single_provider", return_value={"provider": "tavily", "results": [{"url": "https://example.test"}]}) as dispatch:
                result, fallback = self.search()
            self.assertEqual(result["provider"], "tavily")
            self.assertEqual(dispatch.call_args.kwargs["provider_name"], "tavily")
            self.assertIn("No available Exa API keys", fallback["errors"][0])
            probe.assert_called_once()
        with patch("mysearch.cache.time.monotonic", return_value=131):
            self.assertEqual(client._known_provider_unavailability(client.config.exa), "")

    def test_network_probe_failure_keeps_actual_operation_eligible(self):
        client = self.client
        with patch.object(client, "_probe_provider_request", side_effect=MySearchError("network timeout")) as probe:
            self.assertTrue(client._provider_can_serve(client.config.exa))
            with patch.object(client, "_dispatch_single_provider", return_value={"provider": "exa", "results": [{"url": "https://example.test"}]}) as dispatch:
                self.search()
            self.assertEqual(dispatch.call_args.kwargs["provider_name"], "exa")
            probe.assert_called_once()

    def test_all_provider_errors_survive_terminal_failure(self):
        with patch.object(self.client, "_dispatch_single_provider", side_effect=[
            MySearchError("first failure"), MySearchError("second failure"), MySearchError("last failure"),
        ]), self.assertRaises(MySearchError) as caught:
            self.search()
        for detail in ("exa: first failure", "tavily: second failure", "firecrawl: last failure"):
            self.assertIn(detail, str(caught.exception))
