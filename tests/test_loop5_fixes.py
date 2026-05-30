from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mysearch.clients import MySearchClient


def _fake_key(_provider):
    return types.SimpleNamespace(key="firecrawl-key", source="env")


class NewsOverSuppressionTests(unittest.TestCase):
    def test_explicit_news_overrides_version_whitelist(self) -> None:
        # Entertainment/event queries that happen to contain a tech-version
        # negative ("newest release" / "latest release") must still be treated
        # as news when an explicit hard-news marker is present.
        client = MySearchClient()
        for query in [
            "taylor swift newest release news today",
            "latest unstable release news",
            "breaking news about the newest release of the film",
        ]:
            self.assertTrue(
                client._looks_like_news_query(query.lower()),
                msg=f"should be news (explicit marker): {query!r}",
            )

    def test_software_version_queries_still_not_news(self) -> None:
        # Regression guard: the Loop 4 fix must remain intact for version queries
        # that carry no explicit news marker.
        client = MySearchClient()
        for query in [
            "what is the latest stable version of python",
            "node current version",
            "rust newest release",
            "kubernetes latest release",
        ]:
            self.assertFalse(
                client._looks_like_news_query(query.lower()),
                msg=f"should not be news: {query!r}",
            )


class ExtractOverTruncationTests(unittest.TestCase):
    def test_standalone_button_label_in_prose_is_preserved(self) -> None:
        # A page that is *about* hCaptcha can contain a standalone "I am human"
        # line followed by genuine prose. The trailing-widget cut must not drop
        # that real content (kept stays above the 30% floor, so the prose-after
        # guard is what protects it).
        client = MySearchClient()
        intro = (
            "This article explains how human verification widgets work in "
            "modern web applications and why they exist to deter automated "
            "abuse across login and signup forms everywhere today."
        )
        doc = (
            "# Article\n\n"
            + intro + "\n\n"
            "The verification form includes a checkbox labeled as follows.\n\n"
            "I am human\n\n"
            "This sentence after the label is genuine prose explaining the "
            "verification flow in enough detail to clearly exceed the widget "
            "artifact threshold and therefore must be preserved."
        )
        cleaned = client._clean_extract_content(doc)
        self.assertIn("genuine prose explaining the", cleaned)
        self.assertIn("must be preserved.", cleaned)
        self.assertIn(intro, cleaned)

    def test_real_widget_tail_still_stripped(self) -> None:
        # Positive control: an actual trailing widget (no real prose after the
        # signature) is still removed.
        client = MySearchClient()
        doc = (
            "# Page\n\n"
            "Real body content that must remain after cleanup.\n\n"
            "Ask AI\n\n"
            "hCaptcha\n\n"
            "I am human\n\n"
            "EN"
        )
        cleaned = client._clean_extract_content(doc)
        self.assertIn("Real body content that must remain after cleanup.", cleaned)
        self.assertNotIn("hCaptcha", cleaned)
        self.assertNotIn("I am human", cleaned)
        self.assertNotIn("Ask AI", cleaned)

    def test_trailing_empty_heading_residue_removed(self) -> None:
        client = MySearchClient()
        doc = "# Doc\n\nReal content here.\n\n### Filters"
        cleaned = client._clean_extract_content(doc)
        self.assertIn("Real content here.", cleaned)
        self.assertIn("# Doc", cleaned)
        self.assertNotIn("### Filters", cleaned)

    def test_leading_and_mid_headings_with_body_are_kept(self) -> None:
        client = MySearchClient()
        doc = "# Title\n\n## Section\n\nBody under section that stays."
        cleaned = client._clean_extract_content(doc)
        self.assertIn("# Title", cleaned)
        self.assertIn("## Section", cleaned)
        self.assertIn("Body under section that stays.", cleaned)


class CrawlDepthParamTests(unittest.TestCase):
    def test_crawl_site_sends_max_discovery_depth(self) -> None:
        # Firecrawl v2 crawl uses `maxDiscoveryDepth`; the legacy `maxDepth`
        # spelling is silently ignored by the API.
        client = MySearchClient()
        client._get_key_or_raise = _fake_key  # type: ignore[method-assign]
        calls: list[dict[str, object]] = []

        def fake_request_json(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            if str(kwargs.get("method")).upper() == "POST":
                return {"success": True, "id": "job-9"}
            return {"status": "completed", "total": 1, "completed": 1,
                    "data": [{"markdown": "body", "metadata": {"sourceURL": "https://s/a"}}]}

        client._request_json = fake_request_json  # type: ignore[method-assign]
        client.crawl_site(url="https://s", limit=5, max_depth=3)

        post_payload = calls[0]["payload"]
        self.assertEqual(post_payload["maxDiscoveryDepth"], 3)
        self.assertNotIn("maxDepth", post_payload)


if __name__ == "__main__":
    unittest.main()
