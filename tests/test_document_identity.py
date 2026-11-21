from __future__ import annotations

import unittest

from mysearch import postprocess, query_routing
from mysearch.research import finalize, shaping


class DocumentIdentityTests(unittest.TestCase):
    def test_duplicate_anchors_do_not_outvote_independent_retrieval(self):
        primary = {"provider": "tavily", "results": [
            {"url": "https://example.com/a#one"},
            {"url": "https://example.com/a#two", "content": "richer section"},
            {"url": "https://example.com/b"},
        ]}
        merged = shaping._merge_search_payloads(
            primary_result=primary,
            secondary_result={"provider": "firecrawl", "results": [{"url": "https://example.com/b"}]},
            max_results=3,
        )
        self.assertEqual([r["url"] for r in merged["results"]], [
            "https://example.com/b", "https://example.com/a#two",
        ])
        self.assertEqual(merged["results"][0]["matched_providers"], ["firecrawl", "tavily"])
        self.assertEqual(merged["results"][1]["content"], "richer section")
        again = shaping._merge_search_payloads(
            primary_result=merged,
            secondary_result={"provider": "exa", "results": [{"url": "https://example.com/b"}]},
            max_results=3,
        )
        self.assertEqual(again["results"][0]["matched_providers"], ["exa", "firecrawl", "tavily"])

    def test_single_source_dedupes_before_top_k_and_preserves_original_url(self):
        result = finalize._finalize_search_result(
            {"provider": "tavily", "results": [
                {"url": "https://example.com/guide#intro", "title": "Guide"},
                {"url": "https://example.com/guide#api", "title": "Guide"},
                {"url": "https://example.com/other", "title": "Other"},
            ]},
            query="guide", mode="web", intent="general", include_domains=None,
            result_profile="resource", max_results=2,
        )
        self.assertEqual([r["url"] for r in result["results"]], [
            "https://example.com/guide#intro", "https://example.com/other",
        ])
        self.assertEqual([c["url"] for c in result["citations"]], [r["url"] for r in result["results"]])

    def test_identity_preserves_case_sensitive_path_query_and_hash_routes(self):
        identity = postprocess._result_url_identity
        self.assertEqual(identity("https://EXAMPLE.com/A/#one"), identity("https://example.com/A#two"))
        for left, right in [("/A", "/a"), ("/?q=A", "/?q=a"), ("/#/A", "/#/a")]:
            self.assertNotEqual(identity("https://example.com" + left), identity("https://example.com" + right))


class DocumentHydrationTests(unittest.TestCase):
    shell = '{"docsConfig":{"$schema":"https://mintlify.com/docs.json"},"pageMetadata":{},"mdxExtracts":[],"dependencies":["Page.js"]}'

    def test_removes_only_hydration_line_and_keeps_following_content(self):
        cleaned = query_routing._clean_extract_content("# API\n\nActual contract.\n\n " + self.shell + "\n\nMore examples.")
        self.assertEqual(cleaned, "# API\n\nActual contract.\n\nMore examples.")

    def test_preserves_code_examples_even_with_nested_shorter_fences(self):
        for fence in ("```", "~~~~", "````"):
            text = "# Example\n\n" + fence + "json\n" + self.shell + "\n" + fence
            self.assertEqual(query_routing._clean_extract_content(text), text)
        text = "````markdown\n```json\n" + self.shell + "\n```\n````"
        self.assertEqual(query_routing._clean_extract_content(text), text)

    def test_preserves_plain_config_and_prose_mentions(self):
        for text in ('{"docsConfig":{"theme":"aspen"}}', 'Use {"docsConfig": to configure a site.'):
            self.assertEqual(query_routing._clean_extract_content(text), text)

    def test_hydration_keeps_prose_after_json_on_same_line(self):
        text = "# API\n\n" + self.shell + " Important final contract.\n\n| Field | Type |\n| a | str |"
        self.assertEqual(query_routing._clean_extract_content(text),
                         "# API\n\nImportant final contract.\n\n| Field | Type |\n| a | str |")

    def test_invalid_or_indented_literal_payload_is_preserved(self):
        for text in (self.shell[:-1], "    " + self.shell, "\t" + self.shell):
            self.assertEqual(query_routing._clean_extract_content(text), text.strip())
