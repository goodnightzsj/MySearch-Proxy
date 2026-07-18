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


class FactualNewsRoutingTests(unittest.TestCase):
    def test_software_version_queries_not_treated_as_news(self) -> None:
        client = MySearchClient()
        for query in [
            "python latest stable version",
            "node current version",
            "rust newest release",
            "what is the latest stable version of go",
            "postgres current stable",
            "newest version of kubernetes",
        ]:
            self.assertFalse(
                client._looks_like_news_query(query.lower()),
                msg=f"should not be news: {query!r}",
            )

    def test_genuine_news_queries_still_detected(self) -> None:
        client = MySearchClient()
        for query in [
            "latest news today",
            "box office this week",
            "breaking news anthropic",
        ]:
            self.assertTrue(
                client._looks_like_news_query(query.lower()),
                msg=f"should be news: {query!r}",
            )


class ExtractContentCleanupTests(unittest.TestCase):
    def _dirty_sample(self) -> str:
        languages = "\n\n".join([
            "Afrikaans", "Albanian", "Amharic", "Arabic", "Armenian",
            "Azerbaijani", "Basque", "Belarusian", "Bengali", "Bulgarian",
            "Bosnian", "Burmese", "Catalan", "Cebuano", "Chinese", "Croatian",
            "Czech", "Danish", "Dutch", "English", "Zulu",
        ])
        return (
            "# Real Title\n\n"
            "This is the real body paragraph that must survive cleanup.\n\n"
            "![diagram](data:image/svg+xml,%3Csvg%3E%3Cpath/%3E%3C/svg%3E)\n\n"
            "![](<Base64-Image-Removed>)\n\n"
            "Ask AI\n\n"
            "hCaptcha\n\n"
            "I am human\n\n"
            + languages + "\n\n"
            "EN\n\n"
            "[hCaptcha logo, opens new window with more information]"
            "(https://www.hcaptcha.com/what-is-hcaptcha-about)\n\n"
            "Final real sentence stays."
        )

    def test_removes_hcaptcha_widget_and_broken_images(self) -> None:
        client = MySearchClient()
        cleaned = client._clean_extract_content(self._dirty_sample())
        # real content survives
        self.assertIn("real body paragraph that must survive", cleaned)
        self.assertIn("Final real sentence stays.", cleaned)
        # noise removed
        self.assertNotIn("Base64-Image-Removed", cleaned)
        self.assertNotIn("data:image", cleaned)
        self.assertNotIn("hCaptcha", cleaned)
        self.assertNotIn("hcaptcha.com", cleaned)
        self.assertNotIn("Afrikaans", cleaned)
        self.assertNotIn("Zulu", cleaned)
        self.assertNotIn("I am human", cleaned)

    def test_preserves_prose_mentioning_a_single_language(self) -> None:
        client = MySearchClient()
        prose = (
            "The library ships docs in English and Chinese.\n\n"
            "Spanish translations are community maintained."
        )
        self.assertEqual(client._clean_extract_content(prose), prose)

    def test_empty_content_passthrough(self) -> None:
        client = MySearchClient()
        self.assertEqual(client._clean_extract_content(""), "")

    def test_removes_real_world_hcaptcha_tail_with_variant_spellings(self) -> None:
        # Mirrors a live Firecrawl extract: the trailing hCaptcha widget uses
        # non-standard language spellings (Galacian/Gujurati/Teluga) and a partial
        # list, so name-set matching alone misses it; the signature-anchored cut
        # must still remove the whole widget.
        client = MySearchClient()
        variant_langs = "\n\n".join([
            "Gaelic", "Galacian", "Georgian", "German", "Greek", "Gujurati",
            "Haitian", "Kirghiz", "Oriya", "Persian", "Polish", "Romanian",
            "Russian", "Samoan", "Sinhalese", "Southern Sotho", "Teluga",
        ])
        dirty = (
            "# Background Tasks\n\n"
            "You can define background tasks to be run after returning a response.\n\n"
            "## Recap\n\n"
            "Import and use BackgroundTasks to add background tasks.\n\n"
            "Back to top\n\n"
            "### Filters\n\n"
            "#### Tags\n\n"
            "Ask AI\n\n"
            "hCaptcha\n\n"
            "'I am human', Select in order to trigger the challenge, or to bypass "
            "it if you have an accessibility cookie\n\n"
            + variant_langs
        )
        cleaned = client._clean_extract_content(dirty)
        self.assertIn("Import and use BackgroundTasks to add background tasks.", cleaned)
        self.assertIn("# Background Tasks", cleaned)
        self.assertNotIn("hCaptcha", cleaned)
        self.assertNotIn("I am human", cleaned)
        self.assertNotIn("accessibility cookie", cleaned)
        self.assertNotIn("Galacian", cleaned)
        self.assertNotIn("Teluga", cleaned)
        self.assertNotIn("### Filters", cleaned)
        self.assertNotIn("Ask AI", cleaned)

    def test_cleanup_rechecks_trailing_widget_after_language_block_removal(self) -> None:
        client = MySearchClient()
        known_languages = "\n\n".join(sorted(client._HCAPTCHA_LANGUAGES)[:60])
        variant_languages = "\n\n".join(
            ["Galacian", "Gujurati", "Kirghiz", "Oriya", "Teluga"]
        )
        dirty = (
            "# Learn\n\n"
            "Official tutorial content that must survive the cleanup pass. "
            "This section explains the supported learning path, introduces the core "
            "concepts, and links readers to the next chapters without including any "
            "verification-widget instructions.\n\n"
            "Back to top\n\n"
            "### Filters\n\n"
            "#### Tags\n\n"
            "Ask AI\n\n"
            "hCaptcha\n\n"
            "'I am human', Select in order to trigger the challenge, or to bypass "
            "it if you have an accessibility cookie\n\n"
            + known_languages
            + "\n\n"
            + variant_languages
        )

        cleaned = client._clean_extract_content(dirty)

        self.assertIn("Official tutorial content", cleaned)
        self.assertNotIn("hCaptcha", cleaned)
        self.assertNotIn("Galacian", cleaned)
        self.assertNotIn("Teluga", cleaned)
        self.assertNotIn("### Filters", cleaned)
        self.assertNotIn("Ask AI", cleaned)
        self.assertNotIn("Select in order to trigger the challenge", cleaned)

    def test_removes_embedded_cloudflare_browser_challenge_without_truncating_page(self) -> None:
        client = MySearchClient()
        dirty = (
            "# Comparison\n\n"
            "Substantive comparison content before the browser widget.\n\n"
            "Checking your Browser...\n\n"
            "Verifying...\n\n"
            "Stuck? [Troubleshoot](https://challenges.cloudflare.com/cdn-cgi/"
            "challenge-platform/test)\n\n"
            "Verification failed\n\n"
            "Verification expired\n\n"
            "[Privacy](https://www.cloudflare.com/privacypolicy/) - "
            "[Help](https://challenges.cloudflare.com/cdn-cgi/challenge-platform/help)\n\n"
            "## Latest Blogs\n\n"
            "Substantive footer content after the browser widget."
        )

        cleaned = client._clean_extract_content(dirty)

        self.assertIn("Substantive comparison content", cleaned)
        self.assertIn("Substantive footer content", cleaned)
        self.assertNotIn("Checking your Browser", cleaned)
        self.assertNotIn("Verification failed", cleaned)
        self.assertNotIn("challenge-platform", cleaned)

    def test_preserves_incomplete_browser_challenge_like_prose(self) -> None:
        client = MySearchClient()
        prose = (
            "The incident report says checking your browser failed while loading "
            "challenges.cloudflare.com, but it contains no captured widget block."
        )

        self.assertEqual(client._clean_extract_content(prose), prose)


class FirecrawlMapCrawlTests(unittest.TestCase):
    def test_map_site_builds_request_and_parses_links(self) -> None:
        client = MySearchClient()
        client._get_key_or_raise = _fake_key  # type: ignore[method-assign]
        captured: dict[str, object] = {}

        def fake_request_json(**kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return {
                "success": True,
                "links": [
                    {"url": "https://site/a", "title": "A", "description": "da"},
                    "https://site/b",
                    {"no_url": True},
                ],
            }

        client._request_json = fake_request_json  # type: ignore[method-assign]
        out = client.map_site(url="https://site", limit=10, search="docs")

        self.assertEqual(captured["method"], "POST")
        self.assertTrue(str(captured["path"]).endswith("/map"))
        payload = captured["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["url"], "https://site")
        self.assertEqual(payload["limit"], 10)
        self.assertEqual(payload["search"], "docs")
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["links"][0]["url"], "https://site/a")
        self.assertEqual(out["links"][1]["url"], "https://site/b")

    def test_crawl_site_polls_status_and_cleans_pages(self) -> None:
        client = MySearchClient()
        client._get_key_or_raise = _fake_key  # type: ignore[method-assign]
        calls: list[dict[str, object]] = []

        def fake_request_json(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            if str(kwargs.get("method")).upper() == "POST":
                return {"success": True, "id": "job-1"}
            return {
                "status": "completed",
                "total": 2,
                "completed": 2,
                "data": [
                    {
                        "markdown": "page one body",
                        "metadata": {"sourceURL": "https://s/a", "title": "A"},
                    },
                    {
                        "markdown": "![](<Base64-Image-Removed>)\n\npage two body",
                        "metadata": {"sourceURL": "https://s/b"},
                    },
                ],
            }

        client._request_json = fake_request_json  # type: ignore[method-assign]
        out = client.crawl_site(url="https://s", limit=5, max_depth=2)

        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["pages"][0]["url"], "https://s/a")
        self.assertIn("page two body", out["pages"][1]["content"])
        self.assertNotIn("Base64-Image-Removed", out["pages"][1]["content"])
        # POST then at least one GET status poll
        self.assertEqual(str(calls[0]["method"]).upper(), "POST")
        self.assertEqual(calls[0]["payload"]["maxDiscoveryDepth"], 2)
        self.assertEqual(str(calls[1]["method"]).upper(), "GET")
        self.assertTrue(str(calls[1]["path"]).endswith("/crawl/job-1"))


if __name__ == "__main__":
    unittest.main()
