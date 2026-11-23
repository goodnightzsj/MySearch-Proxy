from __future__ import annotations

import time
import unittest
from threading import Event
from unittest.mock import patch

from mysearch.clients import MySearchClient, RouteDecision
from test_provider_disabled import fixture_config


class VerifyBudgetTests(unittest.TestCase):
    def test_slow_primary_keeps_full_body_after_optional_window_expires(self):
        client = MySearchClient(fixture_config())
        self.addCleanup(client.close)
        release = Event()
        body = "Complete source evidence\n" * 1000 + "LAST SECTION"
        seen = []

        def primary(**kwargs):
            seen.append(kwargs)
            time.sleep(0.06)
            return {"provider": "firecrawl", "results": [{"provider": "firecrawl", "title": "Document",
                "url": "https://example.test/document.pdf", "content": body}], "citations": []}

        try:
            with patch("mysearch.clients.OPTIONAL_VERIFY_TIMEOUT_SECONDS", 0.01), patch.object(
                client, "_search_firecrawl", side_effect=primary,
            ), patch.object(client, "_search_tavily", side_effect=lambda **_: release.wait(1)):
                result = client._search_web_blended(
                    query="Full document", mode="pdf", intent="resource", strategy="verify",
                    decision=RouteDecision(provider="firecrawl", reason="test"), max_results=3,
                    include_content=True, include_answer=False, include_domains=None, exclude_domains=None,
                    from_date="2026-01-01", to_date="2026-02-01",
                )
            self.assertIsNone(seen[0]["timeout_seconds"])
            self.assertEqual(seen[0]["from_date"], "2026-01-01")
            self.assertEqual(result["results"][0]["content"], body)
            self.assertEqual(result["evidence"]["verification"], "single-provider-secondary-failed")
            self.assertIn("secondary task timed out", result["secondary_error"])
        finally:
            release.set()
