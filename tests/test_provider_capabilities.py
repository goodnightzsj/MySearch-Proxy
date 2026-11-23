from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx

from mysearch.clients import MySearchClient
from mysearch.config import MySearchConfig
from mysearch.diagnostics import upstream_request_id
from mysearch.errors import MySearchError
from test_request_diagnostics import make_config, provider
from test_proxy_diagnostics import server


URL = "https://docs.example.test/target"
TEXT = "Page content explaining the requested document and its examples. " * 8


class ExaCapabilitiesTests(unittest.TestCase):
    def client(self, response=None):
        config = make_config()
        config.firecrawl = provider()
        config.tavily = provider()
        config.exa = provider()
        config.exa.name = "exa"
        config.exa.default_paths = {"search": "/exa/search", "contents": "/exa/contents"}
        client = MySearchClient(config=config, keyring=Mock())
        self.addCleanup(client.close)
        client._get_key_or_raise = Mock(return_value=Mock(key="synthetic-key", source="test"))
        client._request_json = Mock(return_value=response or {"results": []})
        return client

    def fallback_client(self, response):
        client = self.client(response)
        client._provider_can_serve = Mock(return_value=True)
        client._scrape_firecrawl = Mock(side_effect=MySearchError("firecrawl unavailable"))
        client._extract_tavily = Mock(side_effect=MySearchError("tavily unavailable"))
        return client

    def test_contents_paths_follow_existing_proxy_and_explicit_base_rules(self):
        for env, expected in [
            ({}, "/contents"),
            ({"MYSEARCH_PROXY_BASE_URL": "https://proxy.test"}, "/exa/contents"),
            ({"MYSEARCH_PROXY_BASE_URL": "https://proxy.test", "MYSEARCH_EXA_BASE_URL": "https://direct.test"}, "/contents"),
            ({"MYSEARCH_EXA_CONTENTS_PATH": "/custom/contents"}, "/custom/contents"),
        ]:
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                self.assertEqual(MySearchConfig.from_env().exa.path("contents"), expected)

    def test_contents_fetches_exact_page_and_keeps_original_result_url(self):
        returned = URL + "/#overview"
        client = self.fallback_client({
            "results": [{"id": URL, "url": returned, "text": TEXT}],
            "statuses": [{"id": URL, "status": "success", "source": "cached"}],
        })
        result = client.extract_url(url=URL)
        sent = client._request_json.call_args.kwargs
        self.assertEqual((sent["method"], sent["path"]), ("POST", "/exa/contents"))
        self.assertEqual(sent["payload"], {"ids": [URL], "text": True})
        self.assertEqual((result["provider"], result["url"], result["content"]), ("exa", returned, TEXT.strip()))
        self.assertEqual(result["metadata"]["requested_url"], URL)
        self.assertIn("firecrawl unavailable", result["metadata"]["fallback_reason"])
        self.assertEqual(client.extract_url(url=URL)["content"], result["content"])
        client._request_json.assert_called_once()

    def test_contents_rejects_other_pages_even_with_matching_result_id(self):
        for returned in [URL + "/other", URL + "?page=2", URL + "#/other", "https://other.test/target"]:
            with self.subTest(returned=returned):
                client = self.fallback_client({"results": [{"id": URL, "url": returned, "text": TEXT}]})
                with self.assertRaisesRegex(MySearchError, "exa contents.*exact URL"):
                    client.extract_url(url=URL)

    def test_contents_accepts_url_id_when_url_field_is_absent(self):
        client = self.client({"results": [{"id": URL, "text": TEXT}]})
        self.assertEqual(client._exa_contents(url=URL)["url"], URL)

    def test_contents_cleans_hydration_before_quality_and_keeps_fenced_examples(self):
        shell = '{"docsConfig":{"$schema":"https://mintlify.com/docs.json"},"pageMetadata":{},"mdxExtracts":[],"dependencies":["Page.js"]}'
        example = "```json\n" + shell + "\n```"
        client = self.fallback_client({"results": [{"url": URL, "text": TEXT + "\n\n" + shell + "\n\n" + example}]})
        self.assertEqual(client.extract_url(url=URL)["content"], TEXT + "\n\n" + example)
        client = self.fallback_client({"results": [{"url": URL, "text": shell}]})
        with self.assertRaisesRegex(MySearchError, "exa contents returned empty text"):
            client.extract_url(url=URL)

    def test_tavily_extract_uses_the_same_content_cleaner(self):
        shell = '{"docsConfig":{"$schema":"https://mintlify.com/docs.json"},"pageMetadata":{},"mdxExtracts":[],"dependencies":["Page.js"]}'
        example = "```json\n" + shell + "\n```"
        client = self.client({"results": [{"url": URL, "raw_content": TEXT + "\n\n" + shell + "\n\n" + example}]})
        result = client.extract_url(url=URL, provider="tavily")
        self.assertEqual(result["content"], TEXT + "\n\n" + example)
        self.assertEqual(result["provider"], "tavily")

    def test_tavily_empty_after_cleaning_continues_to_contents(self):
        shell = '{"docsConfig":{"$schema":"https://mintlify.com/docs.json"},"pageMetadata":{},"mdxExtracts":[],"dependencies":["Page.js"]}'
        client = self.fallback_client({})
        del client._extract_tavily
        client.config.tavily.default_paths["extract"] = "/extract"
        client._request_json.side_effect = [
            {"results": [{"url": URL, "raw_content": shell}]},
            {"results": [{"url": URL, "text": TEXT}]},
        ]
        result = client.extract_url(url=URL)
        self.assertEqual((result["provider"], result["content"]), ("exa", TEXT.strip()))
        self.assertIn("tavily extract returned empty content", result["metadata"]["fallback_reason"])
        self.assertEqual([call.kwargs["path"] for call in client._request_json.call_args_list], ["/extract", "/exa/contents"])

    def test_contents_transport_records_camel_request_id_and_operation(self):
        client = self.fallback_client({})
        del client._request_json
        with patch.object(client._http, "request", return_value=httpx.Response(200, json={
            "requestId": "contents-runtime-123", "results": [{"url": URL, "text": TEXT}],
            "statuses": [{"id": URL, "status": "success"}], "costDollars": {"total": 0.001},
        })) as send:
            result = client.extract_url(url=URL)
        send.assert_called_once()
        calls = result["diagnostics"]["calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]["provider"], calls[0]["operation"], calls[0]["upstream_request_id"]),
                         ("exa", "contents", "contents-runtime-123"))
        self.assertEqual(calls[0]["usage"], [{"name": "cost", "unit": "usd", "value": 0.001}])

    def test_per_url_error_takes_precedence_over_returned_text(self):
        client = self.fallback_client({
            "results": [{"url": URL, "text": TEXT}],
            "statuses": [{"id": URL, "status": "error", "error": {"tag": "CRAWL_HTTP_403", "httpStatusCode": 403}}],
        })
        with self.assertRaisesRegex(MySearchError, r"exa contents status error: CRAWL_HTTP_403 \(HTTP 403\)"):
            client.extract_url(url=URL)

    def test_invalid_or_missing_matching_status_is_not_success(self):
        for statuses in [{}, [], [{"id": URL + "/other", "status": "success"}], [{"id": URL, "status": "pending"}]]:
            with self.subTest(statuses=statuses):
                client = self.client({"results": [{"url": URL, "text": TEXT}], "statuses": statuses})
                with self.assertRaises(MySearchError):
                    client._exa_contents(url=URL)

    def test_empty_and_short_text_retain_failure_reason(self):
        for content in ["", "short"]:
            with self.subTest(content=content):
                client = self.fallback_client({"results": [{"url": URL, "text": content}]})
                with self.assertRaisesRegex(MySearchError, "exa contents.*(empty text|short content)"):
                    client.extract_url(url=URL)

    def test_existing_firecrawl_warning_keeps_contents_failure(self):
        client = self.fallback_client({
            "statuses": [{"id": URL, "status": "error", "error": {"tag": "CRAWL_TIMEOUT"}}], "results": [],
        })
        client._scrape_firecrawl = Mock(return_value={"provider": "firecrawl", "url": URL, "content": "", "metadata": {}})
        result = client.extract_url(url=URL)
        self.assertEqual(result["provider"], "firecrawl")
        self.assertIn("CRAWL_TIMEOUT", result["metadata"]["warning"])

    def test_lite_only_uses_measured_parameter_subset(self):
        client = self.client()
        base = dict(query="background tasks tutorial", max_results=3, include_domains=["docs.example.test"],
                    exclude_domains=None, include_content=False, strategy="fast")
        cases = [({}, "lite"), ({"include_domains": None}, "lite"), ({"include_content": True}, "fast"),
                 ({"exclude_domains": ["other.test"]}, "fast"), ({"from_date": "2026-01-01"}, "fast"),
                 ({"to_date": "2026-02-01"}, "fast"), ({"mode": "pdf"}, "fast"),
                 ({"mode": "github"}, "fast"), ({"intent": "status"}, "fast"),
                 ({"strategy": "balanced"}, "auto"), ({"strategy": "verify"}, "auto"),
                 ({"strategy": "deep"}, "deep"), ({"query": "FastAPI.add_task"}, "auto")]
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                client._search_exa(**{**base, **overrides})
                payload = client._request_json.call_args.kwargs["payload"]
                self.assertEqual(payload["type"], expected)
                self.assertTrue(payload["contents"]["highlights"])
                if expected == "lite":
                    self.assertLessEqual(set(payload), {"query", "type", "numResults", "includeDomains", "contents"})
                    self.assertEqual(payload["contents"], {"highlights": True})

    def test_camel_case_request_id_remains_bounded(self):
        self.assertEqual(upstream_request_id({"requestId": "exa-123"}), "exa-123")
        self.assertEqual(upstream_request_id({"request_id": "old-123", "requestId": "exa-123"}), "old-123")
        for value in ["a" * 129, "private\nvalue", "https://private.test", {}, None]:
            self.assertEqual(upstream_request_id({"requestId": value}), "")


class FirecrawlResultTests(unittest.TestCase):
    def test_exact_duplicates_do_not_repeat_bodies_or_citations(self):
        page = {"url": URL, "title": "Target", "description": "Summary", "markdown": TEXT}
        other = {**page, "markdown": TEXT + "Additional evidence."}
        response = {"data": {"web": [page, dict(page), other], "news": [page]}}
        config = make_config()
        config.firecrawl = provider()
        client = MySearchClient(config=config, keyring=Mock())
        self.addCleanup(client.close)
        client._get_key_or_raise = Mock(return_value=Mock(key="synthetic-key", source="test"))
        client._request_json = Mock(return_value=response)

        result = client._search_firecrawl_once(
            query="Target", max_results=5, categories=[], include_content=True,
        )

        self.assertEqual([row["content"] for row in result["results"]],
                         [TEXT.strip(), other["markdown"], TEXT.strip()])
        self.assertEqual([row["source"] for row in result["results"]], ["web", "web", "news"])
        self.assertEqual(len(result["citations"]), 3)
        self.assertEqual(response["data"]["web"], [page, page, other])


class ExaContentsProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"MYSEARCH_PROXY_DB_PATH": str(Path(self.directory.name) / "proxy.db"), "MYSEARCH_EXA_ENABLED": "true"})
        self.env.start()
        server.db.close_conn()
        server.db.init_db()
        self.token = server.db.create_token("contents-test", service="mysearch")
        self.headers = {"Authorization": f"Bearer {self.token['token']}", "X-MySearch-Request-ID": uuid4().hex}
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        server.db.close_conn()
        self.env.stop()
        self.directory.cleanup()

    async def test_contents_reuses_auth_rotation_sanitization_and_diagnostics(self):
        keys = [{"id": 1, "key": "synthetic-first"}, {"id": 2, "key": "synthetic-second"}]
        payload = {"requestId": "exa-contents-123", "results": [{"url": URL, "text": TEXT}],
                   "statuses": [{"id": URL, "status": "success"}], "costDollars": {"total": 0.001}}
        replies = [httpx.Response(401, json={"error": "invalid api key"}), httpx.Response(200, json=payload)]
        with patch.object(server.pool, "get_next_key", side_effect=keys), patch.object(server.pool, "report_result"), patch.object(
            server.http_client, "post", new=AsyncMock(side_effect=replies),
        ) as send:
            response = await self.client.post("/exa/contents?mode=test", headers=self.headers,
                                              json={"ids": [URL], "text": True, "api_key": "private-client-key"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), payload)
        self.assertEqual(send.await_count, 2)
        for call in send.call_args_list:
            self.assertEqual(call.args[0], f"{server.EXA_API_BASE}/contents")
            self.assertEqual(json.loads(call.kwargs["content"]), {"ids": [URL], "text": True})
            self.assertEqual(call.kwargs["params"], {"mode": "test"})
        self.assertEqual(send.call_args.kwargs["headers"]["x-api-key"], "synthetic-second")
        with patch.object(server, "get_admin_password", return_value="synthetic-admin"):
            response = await self.client.get("/api/diagnostics?service=exa", headers={"X-Admin-Password": "synthetic-admin"})
        data = response.json()
        attempts = data["recent_attempts"]
        self.assertEqual(data["physical_attempts"]["count"], 2)
        self.assertTrue(all(row["operation"] == "contents" for row in attempts))
        self.assertEqual(attempts[0]["upstream_request_id"], "exa-contents-123")
        self.assertEqual(attempts[0]["usage"], [{"name": "cost", "unit": "usd", "value": 0.001}])
        for secret in ["synthetic-first", "synthetic-second", "private-client-key", URL, TEXT]:
            self.assertNotIn(secret, json.dumps(data))

    async def test_contents_requires_exa_authority_and_has_no_arbitrary_route(self):
        wrong = server.db.create_token("tavily-only", service="tavily")
        with patch.object(server.http_client, "post", new=AsyncMock()) as send:
            for headers in [{}, {"Authorization": "Bearer invalid"}, {"Authorization": f"Bearer {wrong['token']}"}]:
                response = await self.client.post("/exa/contents", headers=headers, json={"ids": [URL]})
                self.assertEqual(response.status_code, 401)
            response = await self.client.post("/exa/unapproved", headers=self.headers, json={})
            self.assertEqual(response.status_code, 404)
        send.assert_not_called()

    async def test_contents_target_error_is_forwarded_without_rotating_key(self):
        payload = {"results": [], "statuses": [{"id": URL, "status": "error", "error": {"tag": "CRAWL_HTTP_403", "httpStatusCode": 403}}]}
        with patch.object(server.pool, "get_next_key", return_value={"id": 1, "key": "synthetic-key"}), patch.object(
            server.pool, "report_result",
        ) as report, patch.object(server.http_client, "post", new=AsyncMock(return_value=httpx.Response(200, json=payload))) as send:
            response = await self.client.post("/exa/contents", headers=self.headers, json={"ids": [URL], "text": True})
        self.assertEqual(response.json(), payload)
        send.assert_awaited_once()
        self.assertEqual(report.call_args.kwargs["failure_kind"], "")
