from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mysearch.clients import MySearchClient, MySearchError, MySearchHTTPError, RouteDecision


class _FakeResponse:
    def __init__(self, text: str, status: int = 200) -> None:
        self._text = text
        self.status = status

    def read(self) -> bytes:
        return self._text.encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class MySearchClientTests(unittest.TestCase):
    def test_parse_result_timestamp_supports_rfc822(self) -> None:
        client = MySearchClient()

        parsed = client._parse_result_timestamp("Sun, 22 Mar 2026 20:43:36 GMT")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.isoformat(), "2026-03-22T20:43:36+00:00")

    def test_news_rerank_prefers_newer_rfc822_result(self) -> None:
        client = MySearchClient()
        results = [
            {
                "provider": "exa",
                "title": "Older entertainment hit",
                "url": "https://deadline.com/2026/03/older-entertainment-hit",
                "published_date": "2026-03-09T00:00:00.000Z",
            },
            {
                "provider": "tavily",
                "title": "Newer entertainment hit",
                "url": "https://fortune.com/2026/03/22/newer-entertainment-hit/",
                "published_date": "Sun, 22 Mar 2026 20:43:36 GMT",
                "snippet": "Fresh box office update",
            },
        ]

        ranked = client._rerank_general_results(
            query="2026 highest grossing movie opening weekend",
            result_profile="news",
            results=results,
            include_domains=None,
        )

        self.assertEqual(ranked[0]["provider"], "tavily")
        self.assertEqual(ranked[0]["title"], "Newer entertainment hit")

    def test_news_rerank_prefers_query_relevant_story_over_generic_newer_article(self) -> None:
        client = MySearchClient()
        results = [
            {
                "provider": "tavily",
                "title": "Report: Giants Tried To Trade For Notable Veteran Linebacker",
                "url": "https://nfltraderumors.co/report-giants-tried-to-trade-for-notable-veteran-linebacker/",
                "published_date": "Mon, 23 Mar 2026 13:00:00 GMT",
                "snippet": "NFL trade rumor roundup.",
            },
            {
                "provider": "tavily",
                "title": "Barry Keoghan Reveals He Hid From Online Hate After Sabrina Carpenter Split",
                "url": "https://www.tmz.com/2026/03/21/barry-keoghan-talks-online-haters/",
                "published_date": "Sat, 21 Mar 2026 15:54:42 GMT",
                "snippet": "The actor addressed breakup rumors after the split.",
            },
        ]

        ranked = client._rerank_general_results(
            query="latest celebrity breakup rumors 2026",
            result_profile="news",
            results=results,
            include_domains=None,
        )

        self.assertIn("Split", ranked[0]["title"])

    def test_pdf_rerank_prefers_primary_named_paper_over_derivative_variants(self) -> None:
        client = MySearchClient()
        results = [
            {
                "provider": "tavily",
                "title": "DeepSeek-R1 Thoughtology: Let's think about LLM ...",
                "url": "https://arxiv.org/abs/2504.07128",
            },
            {
                "provider": "tavily",
                "title": "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs ...",
                "url": "https://arxiv.org/html/2501.12948v1",
            },
        ]

        ranked = client._rerank_resource_results(
            query="DeepSeek R1 paper pdf",
            mode="pdf",
            results=results,
            include_domains=None,
        )

        self.assertIn("Incentivizing Reasoning Capability", ranked[0]["title"])

    def test_pdf_rerank_prefers_non_derivative_abs_page_over_survey_title(self) -> None:
        client = MySearchClient()
        results = [
            {
                "provider": "exa",
                "title": "[2505.00551] 100 Days After DeepSeek-R1: A Survey on Replication Studies and More Directions for Reasoning Language Models",
                "url": "https://arxiv.org/abs/2505.00551",
            },
            {
                "provider": "exa",
                "title": "Computer Science > Computation and Language",
                "url": "https://arxiv.org/abs/2501.12948?sfnsn=scwspmo",
            },
        ]

        ranked = client._rerank_resource_results(
            query="DeepSeek R1 paper pdf",
            mode="pdf",
            results=results,
            include_domains=["arxiv.org"],
        )

        self.assertEqual(ranked[0]["url"], "https://arxiv.org/abs/2501.12948")

    def test_canonical_result_url_rewrites_arxiv_html_variant_to_abs(self) -> None:
        client = MySearchClient()

        self.assertEqual(
            client._canonical_result_url("https://arxiv.org/html/2501.12948v1"),
            "https://arxiv.org/abs/2501.12948",
        )
        self.assertEqual(
            client._canonical_result_url("https://arxiv.gg/abs/2501.12948"),
            "https://arxiv.org/abs/2501.12948",
        )

    def test_pdf_query_tokenization_keeps_short_model_suffix(self) -> None:
        client = MySearchClient()

        brand_tokens = client._query_brand_tokens("DeepSeek R1 paper pdf")
        precision_tokens = client._query_precision_tokens("DeepSeek R1 paper pdf")
        subject_tokens = client._paper_query_subject_tokens(
            query="DeepSeek R1 paper pdf",
            query_tokens=brand_tokens,
            precision_tokens=precision_tokens,
        )

        self.assertNotIn("r1", brand_tokens)
        self.assertNotIn("r1", precision_tokens)
        self.assertEqual(subject_tokens[:2], ["deepseek", "r1"])

    def test_derivative_paper_title_with_named_prefix_does_not_block_exa_rescue(self) -> None:
        client = MySearchClient()

        strong_match = client._has_strong_pdf_match(
            query="DeepSeek R1 paper pdf",
            results=[
                {
                    "title": "DeepSeek-R1 Thoughtology: Let's think about LLM reasoning",
                    "url": "https://arxiv.org/pdf/2504.07128",
                }
            ],
        )

        self.assertFalse(strong_match)

    def test_pricing_keywords_alone_do_not_trigger_docs_mode(self) -> None:
        client = MySearchClient()

        self.assertFalse(client._looks_like_docs_query("openai pricing"))
        self.assertFalse(client._looks_like_docs_query("苹果 m4 macbook air 价格"))
        self.assertEqual(
            client._resolve_intent(
                query="苹果 M4 MacBook Air 国行价格 官方",
                mode="auto",
                intent="auto",
                sources=["web"],
            ),
            "factual",
        )

    def test_docs_tutorial_query_uses_tutorial_policy_without_strict_resource_mode(self) -> None:
        client = MySearchClient()
        query = "Playwright test.step tutorial example"

        resolved_intent = client._resolve_intent(
            query=query,
            mode="docs",
            intent="auto",
            sources=["web"],
        )
        policy = client._route_policy_for_request(
            query=query,
            mode="docs",
            intent=resolved_intent,
            include_content=False,
        )

        self.assertEqual(resolved_intent, "tutorial")
        self.assertEqual(policy.key, "tutorial")
        self.assertEqual(policy.provider, "exa")
        self.assertFalse(
            client._should_use_strict_resource_policy(
                query=query,
                mode="docs",
                intent=resolved_intent,
                include_domains=None,
            )
        )
        self.assertFalse(client._should_rerank_resource_results(mode="docs", intent=resolved_intent))

    def test_changelog_query_uses_tavily_news_policy(self) -> None:
        client = MySearchClient()

        policy = client._route_policy_for_request(
            query="Next.js 16 release notes official",
            mode="docs",
            intent="resource",
            include_content=False,
        )

        self.assertEqual(policy.key, "changelog")
        self.assertEqual(policy.provider, "tavily")
        self.assertEqual(policy.tavily_topic, "news")
        self.assertEqual(policy.firecrawl_categories, ("news",))

    def test_request_json_auth_error_mentions_rejected_key(self) -> None:
        client = MySearchClient()
        provider = client.config.tavily
        error = HTTPError(
            url="https://example.com/search",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=io.BytesIO(
                b'{"error":"The account associated with this API key has been deactivated."}'
            ),
        )

        with patch("mysearch.clients.urlopen", side_effect=error):
            with self.assertRaises(MySearchHTTPError) as ctx:
                client._request_json(
                    provider=provider,
                    method="POST",
                    path=provider.path("search"),
                    payload={"query": "openai"},
                    key="test-key",
                )

        self.assertEqual(ctx.exception.provider, "tavily")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("configured but the API key was rejected", str(ctx.exception))
        self.assertIn("deactivated", str(ctx.exception))

    def test_health_reports_live_auth_error(self) -> None:
        client = MySearchClient()
        client._probe_provider_status = lambda provider, key_count: {  # type: ignore[method-assign]
            "status": "auth_error" if provider.name == "tavily" else "ok",
            "error": "tavily is configured but the API key was rejected (HTTP 401): deactivated"
            if provider.name == "tavily"
            else "",
            "checked_at": "2026-03-20T00:00:00+00:00",
        }

        payload = client.health()

        self.assertEqual(payload["providers"]["tavily"]["live_status"], "auth_error")
        self.assertIn("deactivated", payload["providers"]["tavily"]["live_error"])
        self.assertEqual(payload["providers"]["firecrawl"]["live_status"], "ok")

    def test_xai_compatible_health_probe_uses_root_health_endpoint(self) -> None:
        client = MySearchClient()
        provider = client.config.xai
        provider.search_mode = "compatible"
        provider.default_paths["social_search"] = "/social/search"
        provider.default_paths["social_health"] = "/social/health"
        provider.alternate_base_urls["social_search"] = "http://gateway.example/v1"
        provider.alternate_base_urls["social_health"] = "http://gateway.example/v1"
        calls: list[dict[str, object]] = []

        def fake_request_json(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            return {"status": "ok"}

        client._request_json = fake_request_json  # type: ignore[method-assign]

        client._probe_provider_request(provider, "gateway-token")

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["method"], "GET")
        self.assertEqual(calls[0]["path"], "/health")
        self.assertEqual(calls[0]["base_url"], "http://gateway.example")

    def test_xai_compatible_health_probe_falls_back_when_root_health_missing(self) -> None:
        client = MySearchClient()
        provider = client.config.xai
        provider.search_mode = "compatible"
        provider.default_paths["social_search"] = "/social/search"
        provider.default_paths["social_health"] = "/social/health"
        provider.alternate_base_urls["social_search"] = "http://gateway.example/admin?foo=1"
        provider.alternate_base_urls["social_health"] = "http://gateway.example/admin?foo=1"
        calls: list[dict[str, object]] = []

        def fake_request_json(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            if kwargs["path"] == "/health":
                raise MySearchHTTPError(
                    provider="xai",
                    status_code=404,
                    detail="not found",
                    url="http://gateway.example/health",
                )
            return {"provider": "custom_social", "results": [{"url": "https://x.com/openai/status/1"}]}

        client._request_json = fake_request_json  # type: ignore[method-assign]

        client._probe_provider_request(provider, "gateway-token")

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["method"], "GET")
        self.assertEqual(calls[0]["path"], "/health")
        self.assertEqual(calls[1]["method"], "POST")
        self.assertEqual(calls[1]["path"], "/social/search")
        self.assertEqual(calls[1]["payload"]["max_results"], 1)
        self.assertEqual(calls[1]["payload"]["model"], "grok-4.1-fast")

    def test_xai_official_health_probe_uses_status_page(self) -> None:
        client = MySearchClient()
        provider = client.config.xai
        provider.search_mode = "official"
        calls: list[dict[str, object]] = []

        def fake_request_text(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            return 200, "API (us-east-1.api.x.ai) available"

        client._request_text = fake_request_text  # type: ignore[method-assign]

        client._probe_provider_request(provider, "official-key")

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["url"], "https://status.x.ai/")

    def test_xai_official_health_probe_falls_back_to_fast_responses_when_status_check_fails(self) -> None:
        client = MySearchClient()
        provider = client.config.xai
        provider.search_mode = "official"
        provider.default_paths["responses"] = "/responses"
        text_calls: list[dict[str, object]] = []
        json_calls: list[dict[str, object]] = []

        def fake_request_text(**kwargs):  # type: ignore[no-untyped-def]
            text_calls.append(kwargs)
            raise MySearchError("unable to determine xAI API status from status.x.ai")

        def fake_request_json(**kwargs):  # type: ignore[no-untyped-def]
            json_calls.append(kwargs)
            return {"id": "resp_123", "status": "completed"}

        client._request_text = fake_request_text  # type: ignore[method-assign]
        client._request_json = fake_request_json  # type: ignore[method-assign]

        client._probe_provider_request(provider, "official-key")

        self.assertEqual(len(text_calls), 1)
        self.assertEqual(len(json_calls), 1)
        self.assertEqual(json_calls[0]["path"], "/responses")
        self.assertEqual(json_calls[0]["payload"]["model"], "grok-4.1-fast")

    def test_xai_compatible_search_timeout_falls_back_to_tavily_x_results(self) -> None:
        client = MySearchClient()
        provider = client.config.xai
        provider.search_mode = "compatible"
        provider.default_paths["social_search"] = "/social/search"
        provider.alternate_base_urls["social_search"] = "http://gateway.example/v1"
        client._get_key_or_raise = lambda provider: type(  # type: ignore[method-assign]
            "Record",
            (),
            {"key": "gateway-token", "source": "env"},
        )()
        calls: list[dict[str, object]] = []

        def fake_request_json(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            raise MySearchError("xai request timeout after 45s: http://127.0.0.1:9874/social/search")

        client._request_json = fake_request_json  # type: ignore[method-assign]
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "Fallback social summary",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "OpenAI on X",
                    "url": "https://x.com/OpenAI/status/123",
                    "snippet": "Latest OpenAI post",
                    "content": "",
                }
            ],
            "citations": [{"title": "OpenAI on X", "url": "https://x.com/OpenAI/status/123"}],
        }

        result = client._search_xai_compatible(
            query="latest OpenAI X posts GPT-5",
            sources=["x"],
            max_results=5,
            allowed_x_handles=None,
            excluded_x_handles=None,
            from_date=None,
            to_date=None,
            include_x_images=False,
            include_x_videos=False,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["timeout_seconds"], 20)
        self.assertEqual(result["provider"], "tavily_social_fallback")
        self.assertEqual(result["results"][0]["url"], "https://x.com/OpenAI/status/123")
        self.assertEqual(result["fallback"]["from"], "xai_compatible")

    def test_github_blob_raw_urls_try_common_branch_aliases(self) -> None:
        client = MySearchClient()

        raw_urls = client._github_blob_raw_urls(
            "https://github.com/openai/openai-node/blob/main/README.md"
        )

        self.assertEqual(
            raw_urls,
            [
                "https://raw.githubusercontent.com/openai/openai-node/main/README.md",
                "https://raw.githubusercontent.com/openai/openai-node/master/README.md",
            ],
        )

    def test_extract_github_blob_raw_falls_back_to_master(self) -> None:
        client = MySearchClient()

        def fake_urlopen(request, timeout):
            if request.full_url.endswith("/main/README.md"):
                raise ValueError("404")
            return _FakeResponse("# OpenAI Node README")

        with patch("mysearch.clients.urlopen", side_effect=fake_urlopen):
            result = client._extract_github_blob_raw(
                url="https://github.com/openai/openai-node/blob/main/README.md"
            )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["provider"], "github_raw")
        self.assertEqual(
            result["metadata"]["raw_url"],
            "https://raw.githubusercontent.com/openai/openai-node/master/README.md",
        )

    def test_firecrawl_domain_filtered_search_falls_back_to_tavily(self) -> None:
        client = MySearchClient()
        client.keyring.has_provider = lambda provider: provider == "tavily"  # type: ignore[method-assign]
        client._probe_provider_status = lambda provider, key_count: {  # type: ignore[method-assign]
            "status": "ok",
            "error": "",
            "checked_at": "2026-03-20T00:00:00+00:00",
        }
        client._search_firecrawl_once = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "firecrawl",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [],
            "citations": [],
        }
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "Responses | OpenAI API Reference",
                    "url": "https://platform.openai.com/docs/api-reference/responses",
                    "snippet": "OpenAI Responses API docs",
                    "content": "",
                }
            ],
            "citations": [
                {
                    "title": "Responses | OpenAI API Reference",
                    "url": "https://platform.openai.com/docs/api-reference/responses",
                }
            ],
        }

        result = client._search_firecrawl(
            query="OpenAI Responses API docs",
            max_results=5,
            categories=["technical"],
            include_content=False,
            include_domains=["openai.com"],
            exclude_domains=None,
        )

        self.assertEqual(result["provider"], "hybrid")
        self.assertEqual(result["route_selected"], "firecrawl+tavily")
        self.assertEqual(result["fallback"]["from"], "firecrawl")
        self.assertEqual(result["fallback"]["to"], "tavily")
        self.assertEqual(len(result["results"]), 1)

    def test_firecrawl_domain_filtered_search_retries_without_site_filter(self) -> None:
        client = MySearchClient()
        client.keyring.has_provider = lambda provider: False  # type: ignore[method-assign]

        def fake_search_firecrawl_once(**kwargs):  # type: ignore[no-untyped-def]
            query = kwargs["query"]
            if query.startswith("site:docs.firecrawl.dev "):
                return {
                    "provider": "firecrawl",
                    "transport": "env",
                    "query": query,
                    "answer": "",
                    "results": [],
                    "citations": [],
                }
            return {
                "provider": "firecrawl",
                "transport": "env",
                "query": query,
                "answer": "",
                "results": [
                    {
                        "provider": "firecrawl",
                        "source": "web",
                        "title": "Scrape - Firecrawl Docs",
                        "url": "https://docs.firecrawl.dev/api-reference/endpoint/scrape",
                        "snippet": "Official Firecrawl docs",
                        "content": "",
                    },
                    {
                        "provider": "firecrawl",
                        "source": "web",
                        "title": "Firecrawl tutorial recap",
                        "url": "https://example.com/firecrawl-scrape-guide",
                        "snippet": "Third-party recap",
                        "content": "",
                    },
                ],
                "citations": [
                    {
                        "title": "Scrape - Firecrawl Docs",
                        "url": "https://docs.firecrawl.dev/api-reference/endpoint/scrape",
                    },
                    {
                        "title": "Firecrawl tutorial recap",
                        "url": "https://example.com/firecrawl-scrape-guide",
                    },
                ],
            }

        client._search_firecrawl_once = fake_search_firecrawl_once  # type: ignore[method-assign]

        result = client._search_firecrawl(
            query="Firecrawl docs scrape api",
            max_results=5,
            categories=["technical"],
            include_content=False,
            include_domains=["docs.firecrawl.dev"],
            exclude_domains=None,
        )

        self.assertEqual(result["provider"], "firecrawl")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(
            result["results"][0]["url"],
            "https://docs.firecrawl.dev/api-reference/endpoint/scrape",
        )
        self.assertEqual(
            result["route_debug"]["domain_filter_mode"],
            "client_filter_retry",
        )
        self.assertEqual(
            result["route_debug"]["retried_include_domains"],
            ["docs.firecrawl.dev"],
        )

    def test_firecrawl_domain_filtered_search_skips_tavily_auth_error_fallback(self) -> None:
        client = MySearchClient()
        client.keyring.has_provider = lambda provider: provider in {"tavily", "firecrawl"}  # type: ignore[method-assign]
        client._probe_provider_status = lambda provider, key_count: {  # type: ignore[method-assign]
            "status": "auth_error" if provider.name == "tavily" else "ok",
            "error": "tavily rejected" if provider.name == "tavily" else "",
            "checked_at": "2026-03-20T00:00:00+00:00",
        }
        client._search_firecrawl_once = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "firecrawl",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [],
            "citations": [],
        }
        client._search_tavily = lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not call tavily"))  # type: ignore[method-assign]

        result = client._search_firecrawl(
            query="Firecrawl docs scrape api",
            max_results=5,
            categories=["technical"],
            include_content=False,
            include_domains=["docs.firecrawl.dev"],
            exclude_domains=None,
        )

        self.assertEqual(result["provider"], "firecrawl")
        self.assertEqual(result["results"], [])

    def test_tavily_domain_filtered_search_retries_with_site_query(self) -> None:
        client = MySearchClient()
        calls: list[dict[str, object]] = []

        def fake_search_tavily_once(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(dict(kwargs))
            if kwargs["query"] == "OpenAI Responses API docs":
                return {
                    "provider": "tavily",
                    "transport": "env",
                    "query": kwargs["query"],
                    "answer": "",
                    "results": [],
                    "citations": [],
                }
            return {
                "provider": "tavily",
                "transport": "env",
                "query": kwargs["query"],
                "answer": "",
                "results": [
                    {
                        "provider": "tavily",
                        "source": "web",
                        "title": "Responses | OpenAI API Reference",
                        "url": "https://platform.openai.com/docs/api-reference/responses",
                        "snippet": "Official OpenAI docs",
                        "content": "",
                    },
                    {
                        "provider": "tavily",
                        "source": "web",
                        "title": "Community recap",
                        "url": "https://example.com/openai-responses-guide",
                        "snippet": "Third-party article",
                        "content": "",
                    }
                ],
                "citations": [
                    {
                        "title": "Responses | OpenAI API Reference",
                        "url": "https://platform.openai.com/docs/api-reference/responses",
                    },
                    {
                        "title": "Community recap",
                        "url": "https://example.com/openai-responses-guide",
                    }
                ],
            }

        client._search_tavily_once = fake_search_tavily_once  # type: ignore[method-assign]

        result = client._search_tavily(
            query="OpenAI Responses API docs",
            max_results=5,
            topic="general",
            include_answer=False,
            include_content=False,
            include_domains=["openai.com"],
            exclude_domains=None,
        )

        self.assertEqual(
            result["results"][0]["url"],
            "https://platform.openai.com/docs/api-reference/responses",
        )
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["route_debug"]["domain_filter_mode"], "site_query_retry")
        self.assertEqual(result["route_debug"]["retried_include_domains"], ["openai.com"])
        self.assertEqual(calls[1]["query"], "site:openai.com OpenAI Responses API docs")
        self.assertIsNone(calls[1]["include_domains"])

    def test_tavily_domain_filtered_search_falls_back_to_firecrawl(self) -> None:
        client = MySearchClient()
        client.keyring.has_provider = lambda provider: provider == "firecrawl"  # type: ignore[method-assign]
        client._probe_provider_status = lambda provider, key_count: {  # type: ignore[method-assign]
            "status": "ok",
            "error": "",
            "checked_at": "2026-03-20T00:00:00+00:00",
        }
        client._search_tavily_once = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [],
            "citations": [],
        }
        client._search_firecrawl_once = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "firecrawl",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "firecrawl",
                    "source": "web",
                    "title": "Responses | OpenAI API Reference",
                    "url": "https://platform.openai.com/docs/api-reference/responses",
                    "snippet": "Official OpenAI docs",
                    "content": "",
                }
            ],
            "citations": [
                {
                    "title": "Responses | OpenAI API Reference",
                    "url": "https://platform.openai.com/docs/api-reference/responses",
                }
            ],
        }

        result = client._search_tavily(
            query="OpenAI Responses API docs",
            max_results=5,
            topic="general",
            include_answer=False,
            include_content=False,
            include_domains=["openai.com"],
            exclude_domains=None,
        )

        self.assertEqual(result["provider"], "hybrid")
        self.assertEqual(result["route_selected"], "tavily+firecrawl")
        self.assertEqual(result["fallback"]["from"], "tavily")
        self.assertEqual(result["fallback"]["to"], "firecrawl")
        self.assertEqual(len(result["results"]), 1)

    def test_docs_blended_search_reranks_official_results_ahead_of_third_party(self) -> None:
        client = MySearchClient()
        official_url = "https://platform.openai.com/docs/api-reference/responses"
        reddit_url = "https://www.reddit.com/r/OpenAI/comments/example"
        arxiv_url = "https://arxiv.org/abs/2401.00001"

        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "OpenAI Responses API docs discussion",
                    "url": reddit_url,
                    "snippet": "Reddit thread about the Responses API",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "Responses | OpenAI API Reference",
                    "url": official_url,
                    "snippet": "Official OpenAI Responses API reference",
                    "content": "",
                },
            ],
            "citations": [
                {"title": "OpenAI Responses API docs discussion", "url": reddit_url},
                {"title": "Responses | OpenAI API Reference", "url": official_url},
            ],
        }
        client._search_firecrawl = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "firecrawl",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "firecrawl",
                    "source": "web",
                    "title": "Attention Is All You Need for OpenAI responses",
                    "url": arxiv_url,
                    "snippet": "Paper result that should not outrank official docs",
                    "content": "",
                },
                {
                    "provider": "firecrawl",
                    "source": "web",
                    "title": "Responses | OpenAI API Reference",
                    "url": official_url,
                    "snippet": "Official OpenAI Responses API reference",
                    "content": "Request and response schema details",
                },
            ],
            "citations": [
                {"title": "Attention Is All You Need for OpenAI responses", "url": arxiv_url},
                {"title": "Responses | OpenAI API Reference", "url": official_url},
            ],
        }

        result = client._search_web_blended(
            query="OpenAI Responses API docs",
            mode="docs",
            intent="resource",
            strategy="balanced",
            decision=RouteDecision(provider="tavily", reason="test", tavily_topic="general"),
            max_results=5,
            include_content=False,
            include_answer=False,
            include_domains=None,
            exclude_domains=None,
        )

        self.assertEqual(result["results"][0]["url"], official_url)
        self.assertEqual(result["citations"][0]["url"], official_url)
        self.assertIn(reddit_url, [item["url"] for item in result["results"][1:]])
        self.assertIn(arxiv_url, [item["url"] for item in result["results"][1:]])

    def test_docs_blended_search_prioritizes_include_domains(self) -> None:
        client = MySearchClient()
        official_url = "https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering"
        medium_url = "https://medium.com/@writer/anthropic-prompting-notes"
        youtube_url = "https://www.youtube.com/watch?v=anthropic-docs"

        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "Anthropic prompt engineering notes",
                    "url": medium_url,
                    "snippet": "Third-party write-up",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "Prompt engineering - Anthropic",
                    "url": official_url,
                    "snippet": "Official Anthropic docs",
                    "content": "",
                },
            ],
            "citations": [
                {"title": "Anthropic prompt engineering notes", "url": medium_url},
                {"title": "Prompt engineering - Anthropic", "url": official_url},
            ],
        }
        client._search_firecrawl = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "firecrawl",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "firecrawl",
                    "source": "web",
                    "title": "Anthropic docs overview video",
                    "url": youtube_url,
                    "snippet": "Third-party video recap",
                    "content": "",
                },
                {
                    "provider": "firecrawl",
                    "source": "web",
                    "title": "Prompt engineering - Anthropic",
                    "url": official_url,
                    "snippet": "Official Anthropic docs",
                    "content": "Official prompt engineering guidance",
                },
            ],
            "citations": [
                {"title": "Anthropic docs overview video", "url": youtube_url},
                {"title": "Prompt engineering - Anthropic", "url": official_url},
            ],
        }

        result = client._search_web_blended(
            query="Anthropic prompt engineering docs",
            mode="docs",
            intent="resource",
            strategy="balanced",
            decision=RouteDecision(provider="tavily", reason="test", tavily_topic="general"),
            max_results=5,
            include_content=False,
            include_answer=False,
            include_domains=["anthropic.com"],
            exclude_domains=None,
        )

        urls = [item["url"] for item in result["results"]]
        first_non_anthropic = next(
            index for index, url in enumerate(urls) if "anthropic.com" not in url
        )
        first_anthropic = next(
            index for index, url in enumerate(urls) if "anthropic.com" in url
        )

        self.assertEqual(result["results"][0]["url"], official_url)
        self.assertEqual(result["citations"][0]["url"], official_url)

    def test_search_route_reason_surfaces_secondary_provider_auth_error(self) -> None:
        client = MySearchClient()
        client.keyring.has_provider = lambda provider: provider in {"tavily", "firecrawl"}  # type: ignore[method-assign]
        client._probe_provider_status = lambda provider, key_count: {  # type: ignore[method-assign]
            "status": "ok",
            "error": "",
            "checked_at": "2026-03-20T00:00:00+00:00",
        }
        client._route_search = lambda **kwargs: RouteDecision(  # type: ignore[method-assign]
            provider="tavily",
            reason="普通网页检索默认走 Tavily",
            tavily_topic="general",
        )
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "Official docs",
                    "url": "https://docs.example.com/page",
                    "snippet": "Official docs",
                    "content": "",
                }
            ],
            "citations": [{"title": "Official docs", "url": "https://docs.example.com/page"}],
        }

        def fail_firecrawl(**kwargs):  # type: ignore[no-untyped-def]
            raise MySearchHTTPError(
                provider="firecrawl",
                status_code=401,
                detail="The account associated with this API key has been deactivated.",
                url="https://example.com/search",
            )

        client._search_firecrawl = fail_firecrawl  # type: ignore[method-assign]

        result = client.search(
            query="example search",
            mode="auto",
            strategy="balanced",
            provider="auto",
            include_answer=False,
        )

        self.assertEqual(result["provider"], "tavily")
        self.assertIn("secondary provider issue", result["route"]["reason"])
        self.assertIn("configured but the API key was rejected", result["route"]["reason"])

    def test_docs_route_skips_tavily_when_live_probe_reports_auth_error(self) -> None:
        client = MySearchClient()
        client.keyring.has_provider = lambda provider: provider in {"tavily", "firecrawl"}  # type: ignore[method-assign]
        client._probe_provider_status = lambda provider, key_count: {  # type: ignore[method-assign]
            "status": "auth_error" if provider.name == "tavily" else "ok",
            "error": "tavily rejected" if provider.name == "tavily" else "",
            "checked_at": "2026-03-20T00:00:00+00:00",
        }

        decision = client._route_search(
            query="newapi cache 官方文档",
            mode="docs",
            intent="resource",
            provider="auto",
            sources=["web"],
            include_content=False,
            include_domains=None,
            allowed_x_handles=None,
            excluded_x_handles=None,
        )

        self.assertEqual(decision.provider, "firecrawl")

    def test_web_route_prefers_exa_when_tavily_probe_is_degraded(self) -> None:
        client = MySearchClient()
        client.keyring.has_provider = lambda provider: provider in {"tavily", "firecrawl", "exa"}  # type: ignore[method-assign]

        def fake_probe(provider, key_count):  # type: ignore[no-untyped-def]
            if provider.name == "tavily":
                return {
                    "status": "http_error",
                    "error": "proxy_error",
                    "checked_at": "2026-03-24T00:00:00+00:00",
                }
            return {
                "status": "ok",
                "error": "",
                "checked_at": "2026-03-24T00:00:00+00:00",
            }

        client._probe_provider_status = fake_probe  # type: ignore[method-assign]

        decision = client._route_search(
            query="best model context protocol server 2026",
            mode="web",
            intent="factual",
            provider="auto",
            sources=["web"],
            include_content=False,
            include_domains=None,
            allowed_x_handles=None,
            excluded_x_handles=None,
        )

        self.assertEqual(decision.provider, "exa")
        self.assertEqual(decision.fallback_chain, ["firecrawl", "tavily"])

    def test_blended_search_requires_live_ok_providers(self) -> None:
        client = MySearchClient()
        client.keyring.has_provider = lambda provider: provider in {"tavily", "firecrawl"}  # type: ignore[method-assign]

        def fake_probe(provider, key_count):  # type: ignore[no-untyped-def]
            return {
                "status": "http_error" if provider.name == "tavily" else "ok",
                "error": "proxy_error" if provider.name == "tavily" else "",
                "checked_at": "2026-03-24T00:00:00+00:00",
            }

        client._probe_provider_status = fake_probe  # type: ignore[method-assign]

        self.assertFalse(
            client._should_blend_web_providers(
                requested_provider="auto",
                decision=RouteDecision(provider="firecrawl", reason="fallback", result_profile="web"),
                sources=["web"],
                strategy="balanced",
                mode="web",
                intent="factual",
                include_domains=None,
            )
        )

    def test_search_reranks_direct_docs_results_to_official_first(self) -> None:
        client = MySearchClient()
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "Playwright test.step Guide",
                    "url": "https://www.checklyhq.com/blog/playwright-test-step-guide/",
                    "snippet": "Third-party guide",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "test.step | Playwright",
                    "url": "https://playwright.dev/docs/api/class-test",
                    "snippet": "Official Playwright docs",
                    "content": "",
                },
            ],
            "citations": [
                {
                    "title": "Playwright test.step Guide",
                    "url": "https://www.checklyhq.com/blog/playwright-test-step-guide/",
                },
                {
                    "title": "test.step | Playwright",
                    "url": "https://playwright.dev/docs/api/class-test",
                },
            ],
        }

        result = client.search(
            query="Playwright test.step docs",
            mode="docs",
            strategy="fast",
            provider="tavily",
            include_answer=False,
        )

        self.assertEqual(result["results"][0]["url"], "https://playwright.dev/docs/api/class-test")
        self.assertEqual(result["citations"][0]["url"], "https://playwright.dev/docs/api/class-test")
        self.assertEqual(result["evidence"]["official_source_count"], 1)
        self.assertEqual(result["evidence"]["confidence"], "high")
        self.assertNotIn("mixed-official-and-third-party", result["evidence"]["conflicts"])

    def test_search_strict_official_mode_filters_to_official_results(self) -> None:
        client = MySearchClient()
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "Playwright test.step Guide",
                    "url": "https://www.checklyhq.com/blog/playwright-test-step-guide/",
                    "snippet": "Third-party guide",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "test.step | Playwright",
                    "url": "https://playwright.dev/docs/api/class-test",
                    "snippet": "Official Playwright docs",
                    "content": "",
                },
            ],
            "citations": [
                {
                    "title": "Playwright test.step Guide",
                    "url": "https://www.checklyhq.com/blog/playwright-test-step-guide/",
                },
                {
                    "title": "test.step | Playwright",
                    "url": "https://playwright.dev/docs/api/class-test",
                },
            ],
        }

        result = client.search(
            query="Playwright test.step official docs",
            mode="docs",
            strategy="fast",
            provider="tavily",
            include_domains=["playwright.dev"],
            include_answer=False,
        )

        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["url"], "https://playwright.dev/docs/api/class-test")
        self.assertEqual(result["evidence"]["official_mode"], "strict")
        self.assertTrue(result["evidence"]["official_filter_applied"])
        self.assertEqual(result["evidence"]["official_source_count"], 1)
        self.assertNotIn("mixed-official-and-third-party", result["evidence"]["conflicts"])

    def test_search_strict_official_mode_keeps_results_but_flags_unmet(self) -> None:
        client = MySearchClient()
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "OpenAI API Pricing Guide",
                    "url": "https://apidog.com/blog/openai-api-pricing/",
                    "snippet": "Third-party pricing guide",
                    "content": "",
                },
            ],
            "citations": [
                {
                    "title": "OpenAI API Pricing Guide",
                    "url": "https://apidog.com/blog/openai-api-pricing/",
                },
            ],
        }

        result = client.search(
            query="OpenAI pricing official",
            mode="web",
            strategy="fast",
            provider="tavily",
            include_answer=False,
        )

        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["evidence"]["official_mode"], "strict")
        self.assertFalse(result["evidence"]["official_filter_applied"])
        self.assertIn("strict-official-unmet", result["evidence"]["conflicts"])
        self.assertEqual(result["evidence"]["confidence"], "low")

    def test_search_strict_official_mode_counts_official_hits_for_web_queries(self) -> None:
        client = MySearchClient()
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "API Pricing | OpenAI",
                    "url": "https://openai.com/api/pricing/",
                    "snippet": "Official pricing page",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "OpenAI API Pricing Guide",
                    "url": "https://apidog.com/blog/openai-api-pricing/",
                    "snippet": "Third-party pricing guide",
                    "content": "",
                },
            ],
            "citations": [
                {"title": "API Pricing | OpenAI", "url": "https://openai.com/api/pricing/"},
                {
                    "title": "OpenAI API Pricing Guide",
                    "url": "https://apidog.com/blog/openai-api-pricing/",
                },
            ],
        }

        result = client.search(
            query="OpenAI pricing official",
            mode="web",
            strategy="fast",
            provider="tavily",
            include_answer=False,
        )

        self.assertEqual(result["results"][0]["url"], "https://openai.com/api/pricing/")
        self.assertEqual(result["evidence"]["official_mode"], "strict")
        self.assertEqual(result["evidence"]["official_source_count"], 1)
        self.assertTrue(result["evidence"]["official_filter_applied"])
        self.assertNotIn("strict-official-unmet", result["evidence"]["conflicts"])
        self.assertEqual(
            result["summary"],
            "Top official match: API Pricing | OpenAI (openai.com)",
        )

    def test_search_strict_official_mode_reranks_locale_variant_below_canonical_page(self) -> None:
        client = MySearchClient()
        client._search_exa = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "exa",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "exa",
                    "source": "web",
                    "title": "Precios de la API - OpenAI",
                    "url": "https://openai.com/es-419/api/pricing/",
                    "snippet": "Localized pricing page",
                    "content": "",
                },
                {
                    "provider": "exa",
                    "source": "web",
                    "title": "API Pricing - OpenAI",
                    "url": "https://openai.com/api/pricing/",
                    "snippet": "Canonical pricing page",
                    "content": "",
                },
            ],
            "citations": [
                {"title": "Precios de la API - OpenAI", "url": "https://openai.com/es-419/api/pricing/"},
                {"title": "API Pricing - OpenAI", "url": "https://openai.com/api/pricing/"},
            ],
        }

        result = client.search(
            query="OpenAI API pricing official",
            mode="web",
            strategy="verify",
            provider="exa",
            include_answer=False,
        )

        self.assertEqual(result["results"][0]["url"], "https://openai.com/api/pricing/")
        self.assertEqual(
            result["summary"],
            "Top official match: API Pricing - OpenAI (openai.com)",
        )

    def test_docs_mode_enters_strict_resource_policy(self) -> None:
        client = MySearchClient()

        mode = client._resolve_official_result_mode(
            query="Next.js generateMetadata",
            mode="docs",
            intent="resource",
            include_domains=None,
        )

        self.assertEqual(mode, "strict")

    def test_search_uses_exa_rescue_for_sparse_web_results(self) -> None:
        client = MySearchClient()
        client._provider_can_serve = lambda provider: True  # type: ignore[method-assign]
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "Sparse result",
                    "url": "https://example.com/one",
                    "snippet": "Only one result",
                    "content": "",
                }
            ],
            "citations": [
                {"title": "Sparse result", "url": "https://example.com/one"},
            ],
        }
        client._search_exa = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "exa",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "exa",
                    "source": "web",
                    "title": "Long tail reference",
                    "url": "https://exa.example.com/two",
                    "snippet": "Recovered long-tail source",
                    "content": "",
                },
                {
                    "provider": "exa",
                    "source": "web",
                    "title": "Another result",
                    "url": "https://exa.example.com/three",
                    "snippet": "Recovered another source",
                    "content": "",
                },
            ],
            "citations": [
                {"title": "Long tail reference", "url": "https://exa.example.com/two"},
                {"title": "Another result", "url": "https://exa.example.com/three"},
            ],
        }

        result = client.search(
            query="best open source vector database comparison for offline agents",
            mode="web",
            strategy="fast",
            provider="tavily",
            max_results=3,
            include_answer=False,
        )

        self.assertEqual(result["provider"], "hybrid")
        self.assertEqual(result["fallback"]["to"], "exa")
        self.assertEqual(result["results"][0]["url"], "https://exa.example.com/two")

    def test_search_pdf_verify_uses_exa_rescue_for_weak_results(self) -> None:
        client = MySearchClient()
        client._provider_can_serve = lambda provider: provider.name in {"firecrawl", "exa"}  # type: ignore[method-assign]
        client._search_firecrawl = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "firecrawl",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "firecrawl",
                    "source": "web",
                    "title": "DeepSeek-R1 Thoughtology",
                    "url": "https://arxiv.org/pdf/2504.07128",
                    "snippet": "Related paper",
                    "content": "",
                },
                {
                    "provider": "firecrawl",
                    "source": "web",
                    "title": "Another paper",
                    "url": "https://arxiv.org/pdf/2505.12625",
                    "snippet": "Other paper",
                    "content": "",
                },
                {
                    "provider": "firecrawl",
                    "source": "web",
                    "title": "Paper three",
                    "url": "https://arxiv.org/pdf/2503.11486",
                    "snippet": "Third paper",
                    "content": "",
                },
            ],
            "citations": [
                {"title": "DeepSeek-R1 Thoughtology", "url": "https://arxiv.org/pdf/2504.07128"},
                {"title": "Another paper", "url": "https://arxiv.org/pdf/2505.12625"},
                {"title": "Paper three", "url": "https://arxiv.org/pdf/2503.11486"},
            ],
        }
        client._search_exa = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "exa",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "exa",
                    "source": "web",
                    "title": "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning",
                    "url": "https://arxiv.org/abs/2501.12948",
                    "snippet": "Exact paper page",
                    "content": "",
                }
            ],
            "citations": [
                {
                    "title": "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning",
                    "url": "https://arxiv.org/abs/2501.12948",
                }
            ],
        }

        result = client.search(
            query="DeepSeek R1 paper pdf",
            mode="pdf",
            strategy="verify",
            max_results=3,
            provider="auto",
            include_domains=["arxiv.org"],
            include_answer=False,
        )

        self.assertEqual(result["provider"], "hybrid")
        self.assertEqual(result["fallback"]["to"], "exa")
        self.assertEqual(result["results"][0]["url"], "https://arxiv.org/abs/2501.12948")

    def test_rerank_general_news_prefers_mainstream_article_shape(self) -> None:
        client = MySearchClient()

        reranked = client._rerank_general_results(
            query="2026 oscar winners",
            result_profile="news",
            include_domains=None,
            results=[
                {
                    "provider": "tavily",
                    "title": "Oscars 2026 winners list",
                    "url": "https://news-aggregate.example.com/oscars-winners",
                    "snippet": "aggregated summary",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "Oscars 2026 winners list",
                    "url": "https://www.latimes.com/entertainment-arts/awards/story/2026-03-15/oscars-2026-winners-list-full-results",
                    "snippet": "Los Angeles Times coverage",
                    "content": "",
                    "published_date": "2026-03-15T09:00:00+00:00",
                },
            ],
        )

        self.assertEqual(
            reranked[0]["url"],
            "https://www.latimes.com/entertainment-arts/awards/story/2026-03-15/oscars-2026-winners-list-full-results",
        )

    def test_rerank_general_web_prefers_exact_official_pricing_page(self) -> None:
        client = MySearchClient()

        reranked = client._rerank_general_results(
            query="OpenAI pricing official",
            result_profile="web",
            include_domains=["openai.com"],
            results=[
                {
                    "provider": "exa",
                    "title": "Confused about OpenAI pricing",
                    "url": "https://community.openai.com/t/confused-about-openai-batch-api-gpt-4o-mini-pricing-why-are-the-total-costs-higher/936262",
                    "snippet": "Official community discussion about pricing",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "Compare OpenAI API models",
                    "url": "https://developers.openai.com/api/docs/models/compare",
                    "snippet": "Compare model capabilities",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "API Pricing | OpenAI",
                    "url": "https://openai.com/api/pricing/",
                    "snippet": "Official pricing page",
                    "content": "",
                },
            ],
        )

        self.assertEqual(reranked[0]["url"], "https://openai.com/api/pricing/")

    def test_rerank_general_web_prefers_canonical_buy_page_over_sku_detail(self) -> None:
        client = MySearchClient()

        reranked = client._rerank_general_results(
            query="MacBook Air M5 国行价格 官方",
            result_profile="web",
            include_domains=["apple.com.cn"],
            results=[
                {
                    "provider": "exa",
                    "title": "13 英寸 MacBook Air - 银色",
                    "url": "https://www.apple.com.cn/shop/buy-mac/macbook-air/13-%E8%8B%B1%E5%AF%B8-m5-%E8%8A%AF%E7%89%87-8-%E6%A0%B8-gpu-16gb-%E7%BB%9F%E4%B8%80%E5%86%85%E5%AD%98-256gb-ssd-%E5%AD%98%E5%82%A8%E9%93%B6%E8%89%B2",
                    "snippet": "SKU detail page",
                    "content": "",
                },
                {
                    "provider": "exa",
                    "title": "购买 MacBook Air",
                    "url": "https://www.apple.com.cn/shop/buy-mac/macbook-air",
                    "snippet": "Canonical buy page",
                    "content": "",
                },
            ],
        )

        self.assertEqual(reranked[0]["url"], "https://www.apple.com.cn/shop/buy-mac/macbook-air")

    def test_official_policy_prefers_canonical_buy_page_over_specific_sku(self) -> None:
        client = MySearchClient()

        result = client._apply_official_resource_policy(
            query="MacBook Air M5 国行价格 官方",
            mode="web",
            intent="factual",
            include_domains=["apple.com.cn"],
            result={
                "results": [
                    {
                        "provider": "exa",
                        "title": "购买MacBook Air 15 英寸(M5) - 午夜色- 24GB/4TB - Apple (中国大陆)",
                        "url": "https://www.apple.com.cn/shop/buy-mac/macbook-air/15-inch-midnight-m5-chip-10-core-cpu-10-core-gpu-24gb-memory-4tb-storage",
                        "snippet": "",
                        "content": "",
                    },
                    {
                        "provider": "exa",
                        "title": "13 英寸和15 英寸MacBook Air - Apple (中国大陆)",
                        "url": "https://www.apple.com.cn/macbook-air/",
                        "snippet": "",
                        "content": "",
                    },
                    {
                        "provider": "exa",
                        "title": "购买 MacBook Air",
                        "url": "https://www.apple.com.cn/shop/buy-mac/macbook-air",
                        "snippet": "",
                        "content": "",
                    },
                ],
                "citations": [
                    {
                        "title": "购买MacBook Air 15 英寸(M5) - 午夜色- 24GB/4TB - Apple (中国大陆)",
                        "url": "https://www.apple.com.cn/shop/buy-mac/macbook-air/15-inch-midnight-m5-chip-10-core-cpu-10-core-gpu-24gb-memory-4tb-storage",
                    },
                    {
                        "title": "13 英寸和15 英寸MacBook Air - Apple (中国大陆)",
                        "url": "https://www.apple.com.cn/macbook-air/",
                    },
                    {
                        "title": "购买 MacBook Air",
                        "url": "https://www.apple.com.cn/shop/buy-mac/macbook-air",
                    },
                ],
                "evidence": {},
            },
        )

        self.assertEqual(result["results"][0]["url"], "https://www.apple.com.cn/shop/buy-mac/macbook-air")
        self.assertEqual(result["citations"][0]["url"], "https://www.apple.com.cn/shop/buy-mac/macbook-air")

    def test_rerank_general_web_prefers_status_page_for_status_queries(self) -> None:
        client = MySearchClient()

        reranked = client._rerank_general_results(
            query="OpenAI latest status update",
            result_profile="web",
            include_domains=None,
            results=[
                {
                    "provider": "tavily",
                    "title": "Responses API errors when using background mode - IsDown",
                    "url": "https://isdown.app/status/openai/incidents/554117-responses-api-errors-when-using-background-mode",
                    "snippet": "Aggregator incident page",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "Responses API errors when using background mode - OpenAI Status",
                    "url": "https://status.openai.com/incidents/abc123",
                    "snippet": "Incident details",
                    "content": "",
                },
            ],
        )

        self.assertEqual(reranked[0]["url"], "https://status.openai.com/incidents/abc123")

    def test_rerank_resource_results_prefers_exact_query_token_page(self) -> None:
        client = MySearchClient()

        reranked = client._rerank_resource_results(
            query="Playwright test.step docs",
            mode="docs",
            include_domains=None,
            results=[
                {
                    "provider": "tavily",
                    "title": "Test class | Playwright",
                    "url": "https://playwright.dev/docs/api/class-test",
                    "snippet": "General test API reference",
                    "content": "",
                },
                {
                    "provider": "exa",
                    "title": "test.step | Playwright",
                    "url": "https://playwright.dev/docs/api/class-teststep",
                    "snippet": "Exact test.step API reference",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "TestStepInfo class | Playwright",
                    "url": "https://playwright.dev/docs/api/class-teststepinfo",
                    "snippet": "test.step related API",
                    "content": "",
                },
            ],
        )

        self.assertEqual(reranked[0]["url"], "https://playwright.dev/docs/api/class-teststep")

    def test_rerank_resource_results_prefers_non_locale_official_variant(self) -> None:
        client = MySearchClient()

        reranked = client._rerank_resource_results(
            query="OpenAI API pricing official",
            mode="web",
            include_domains=None,
            results=[
                {
                    "provider": "exa",
                    "title": "Precios de la API - OpenAI",
                    "url": "https://openai.com/es-419/api/pricing/",
                    "snippet": "Localized pricing page",
                    "content": "",
                },
                {
                    "provider": "exa",
                    "title": "API Pricing - OpenAI",
                    "url": "https://openai.com/api/pricing/",
                    "snippet": "Canonical pricing page",
                    "content": "",
                },
                {
                    "provider": "exa",
                    "title": "Pricing | OpenAI API",
                    "url": "https://developers.openai.com/api/docs/pricing/",
                    "snippet": "Docs pricing reference",
                    "content": "",
                },
            ],
        )

        self.assertEqual(reranked[0]["url"], "https://openai.com/api/pricing/")

    def test_rerank_resource_results_prefers_release_blog_for_changelog_query(self) -> None:
        client = MySearchClient()

        reranked = client._rerank_resource_results(
            query="Next.js 16 release notes official",
            mode="docs",
            include_domains=["nextjs.org"],
            results=[
                {
                    "provider": "tavily",
                    "title": "Guides: Next.js MCP Server",
                    "url": "https://nextjs.org/docs/app/guides/mcp",
                    "snippet": "Generic MCP guide",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "Next.js 16",
                    "url": "https://nextjs.org/blog/next-16",
                    "snippet": "Official release announcement",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "Upgrading: Version 16",
                    "url": "https://nextjs.org/docs/app/guides/upgrading/version-16",
                    "snippet": "Migration guide",
                    "content": "",
                },
            ],
        )

        self.assertEqual(reranked[0]["url"], "https://nextjs.org/blog/next-16")

    def test_pdf_verify_blend_promotes_exact_paper_page(self) -> None:
        client = MySearchClient()
        client._provider_can_serve = lambda provider: provider.name in {"tavily", "firecrawl"}  # type: ignore[method-assign]
        client._search_firecrawl = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "firecrawl",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "firecrawl",
                    "source": "web",
                    "title": "DeepSeek-R1 Thoughtology",
                    "url": "https://arxiv.org/pdf/2504.07128",
                    "snippet": "Related paper",
                    "content": "",
                }
            ],
            "citations": [{"title": "DeepSeek-R1 Thoughtology", "url": "https://arxiv.org/pdf/2504.07128"}],
        }
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning",
                    "url": "https://arxiv.org/abs/2501.12948",
                    "snippet": "The exact paper page",
                    "content": "",
                }
            ],
            "citations": [
                {
                    "title": "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning",
                    "url": "https://arxiv.org/abs/2501.12948",
                }
            ],
        }

        result = client._search_web_blended(
            query="DeepSeek R1 paper pdf",
            mode="pdf",
            intent="resource",
            strategy="verify",
            decision=RouteDecision(
                provider="firecrawl",
                reason="pdf primary",
                firecrawl_categories=("pdf",),
                result_profile="resource",
            ),
            max_results=4,
            include_content=False,
            include_answer=False,
            include_domains=None,
            exclude_domains=None,
        )

        self.assertEqual(result["results"][0]["url"], "https://arxiv.org/abs/2501.12948")

    def test_pdf_verify_blend_falls_back_to_exa_when_primary_and_secondary_fail(self) -> None:
        client = MySearchClient()
        client._provider_can_serve = lambda provider: provider.name in {"tavily", "firecrawl", "exa"}  # type: ignore[method-assign]
        client._search_firecrawl = lambda **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
            MySearchError("firecrawl quota exhausted")
        )
        client._search_tavily = lambda **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
            MySearchError("tavily upstream failed")
        )
        client._search_exa = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "exa",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "exa",
                    "source": "web",
                    "title": "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning",
                    "url": "https://arxiv.org/abs/2501.12948",
                    "snippet": "Exact paper page",
                    "content": "",
                }
            ],
            "citations": [
                {
                    "title": "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning",
                    "url": "https://arxiv.org/abs/2501.12948",
                }
            ],
            "evidence": {
                "providers_consulted": ["exa"],
                "verification": "single-provider",
            },
        }

        result = client._search_web_blended(
            query="DeepSeek R1 paper pdf",
            mode="pdf",
            intent="resource",
            strategy="verify",
            decision=RouteDecision(
                provider="firecrawl",
                reason="pdf primary",
                firecrawl_categories=("pdf",),
                result_profile="resource",
                allow_exa_rescue=True,
            ),
            max_results=4,
            include_content=False,
            include_answer=False,
            include_domains=["arxiv.org"],
            exclude_domains=None,
        )

        self.assertEqual(result["provider"], "exa")
        self.assertEqual(result["fallback"]["to"], "exa")
        self.assertEqual(result["results"][0]["url"], "https://arxiv.org/abs/2501.12948")

    def test_pdf_verify_uses_exa_boost_for_weak_firecrawl_match(self) -> None:
        client = MySearchClient()
        client._provider_can_serve = lambda provider: provider.name in {"firecrawl", "exa"}  # type: ignore[method-assign]
        client._search_firecrawl = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "firecrawl",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "firecrawl",
                    "source": "web",
                    "title": "Investigating Local Censorship in DeepSeek's R1 ...",
                    "url": "https://arxiv.org/abs/2505.12625",
                    "snippet": "Related but not the primary paper",
                    "content": "Related but not the primary paper" if kwargs.get("include_content") else "",
                }
            ],
            "citations": [
                {
                    "title": "Investigating Local Censorship in DeepSeek's R1 ...",
                    "url": "https://arxiv.org/abs/2505.12625",
                }
            ],
        }
        client._search_exa = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "exa",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "exa",
                    "source": "web",
                    "title": "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning",
                    "url": "https://arxiv.org/abs/2501.12948",
                    "snippet": "The exact paper page",
                    "content": "",
                }
            ],
            "citations": [
                {
                    "title": "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning",
                    "url": "https://arxiv.org/abs/2501.12948",
                }
            ],
        }

        result = client.search(
            query="DeepSeek R1 paper pdf",
            mode="pdf",
            strategy="verify",
            provider="auto",
            include_answer=False,
            include_content=True,
            include_domains=["arxiv.org"],
            max_results=5,
        )

        self.assertTrue(result["evidence"]["pdf_exa_boost"])
        self.assertEqual(result["results"][0]["url"], "https://arxiv.org/abs/2501.12948")

    def test_pdf_verify_uses_tavily_boost_when_exa_still_lacks_exact_paper_title(self) -> None:
        client = MySearchClient()
        client._provider_can_serve = lambda provider: provider.name in {"firecrawl", "exa", "tavily"}  # type: ignore[method-assign]
        client._search_firecrawl = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "firecrawl",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "firecrawl",
                    "source": "web",
                    "title": "Insights into DeepSeek-V3: Scaling Challenges and Reflections on Hardware for AI Architectures",
                    "url": "https://arxiv.org/abs/2505.09343",
                    "snippet": "Wrong but related paper",
                    "content": "Wrong but related paper" if kwargs.get("include_content") else "",
                }
            ],
            "citations": [
                {
                    "title": "Insights into DeepSeek-V3: Scaling Challenges and Reflections on Hardware for AI Architectures",
                    "url": "https://arxiv.org/abs/2505.09343",
                }
            ],
        }
        client._search_exa = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "exa",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "exa",
                    "source": "web",
                    "title": "Computer Science > Computation and Language",
                    "url": "https://arxiv.org/abs/2501.12948",
                    "snippet": "",
                    "content": "",
                }
            ],
            "citations": [
                {"title": "Computer Science > Computation and Language", "url": "https://arxiv.org/abs/2501.12948"},
            ],
        }
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning",
                    "url": "https://arxiv.org/html/2501.12948v1",
                    "snippet": "Exact paper page",
                    "content": "",
                }
            ],
            "citations": [
                {
                    "title": "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning",
                    "url": "https://arxiv.org/html/2501.12948v1",
                }
            ],
        }

        result = client.search(
            query="DeepSeek R1 paper pdf",
            mode="pdf",
            strategy="verify",
            provider="auto",
            include_answer=False,
            include_content=True,
            include_domains=["arxiv.org"],
            max_results=5,
        )

        self.assertTrue(result["evidence"]["pdf_exa_boost"])
        self.assertTrue(result["evidence"]["pdf_tavily_boost"])
        self.assertEqual(result["results"][0]["url"], "https://arxiv.org/abs/2501.12948")
        self.assertIn("DeepSeek-R1", result["summary"])

    def test_pdf_title_enrichment_promotes_generic_arxiv_entry(self) -> None:
        client = MySearchClient()
        client._provider_can_serve = lambda provider: provider.name == "exa"  # type: ignore[method-assign]
        client._search_exa = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "exa",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "exa",
                    "source": "web",
                    "title": "Insights into DeepSeek-V3: Scaling Challenges and Reflections on Hardware for AI Architectures",
                    "url": "https://arxiv.org/abs/2505.09343",
                    "snippet": "Wrong but related paper",
                    "content": "",
                },
                {
                    "provider": "exa",
                    "source": "web",
                    "title": "Computer Science > Computation and Language",
                    "url": "https://arxiv.org/abs/2501.12948",
                    "snippet": "",
                    "content": "",
                },
            ],
            "citations": [
                {
                    "title": "Insights into DeepSeek-V3: Scaling Challenges and Reflections on Hardware for AI Architectures",
                    "url": "https://arxiv.org/abs/2505.09343",
                },
                {
                    "title": "Computer Science > Computation and Language",
                    "url": "https://arxiv.org/abs/2501.12948",
                },
            ],
        }
        client._fetch_arxiv_title = lambda url: (  # type: ignore[method-assign]
            "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning"
            if "2501.12948" in url
            else ""
        )

        result = client.search(
            query="DeepSeek R1 paper pdf",
            mode="pdf",
            strategy="verify",
            provider="exa",
            include_answer=False,
            include_domains=["arxiv.org"],
            max_results=5,
        )

        self.assertEqual(result["results"][0]["url"], "https://arxiv.org/abs/2501.12948")
        self.assertTrue(result["evidence"]["pdf_title_enrichment"])
        self.assertIn("DeepSeek-R1", result["summary"])

    def test_resolve_research_plan_adapts_docs_and_news_budgets(self) -> None:
        client = MySearchClient()

        docs_plan = client._resolve_research_plan(
            query="OpenAI pricing official",
            mode="docs",
            intent="resource",
            strategy="balanced",
            web_max_results=5,
            social_max_results=5,
            scrape_top_n=4,
            include_social=True,
            include_domains=None,
        )
        news_plan = client._resolve_research_plan(
            query="2026 oscars winners",
            mode="news",
            intent="news",
            strategy="deep",
            web_max_results=5,
            social_max_results=2,
            scrape_top_n=3,
            include_social=True,
            include_domains=None,
        )

        self.assertEqual(docs_plan["web_mode"], "docs")
        self.assertEqual(docs_plan["scrape_top_n"], 2)
        self.assertGreaterEqual(news_plan["web_max_results"], 6)
        self.assertGreaterEqual(news_plan["social_max_results"], 4)
        self.assertGreaterEqual(news_plan["scrape_top_n"], 4)

    def test_firecrawl_primary_verify_requests_content(self) -> None:
        client = MySearchClient()
        firecrawl_calls: list[dict[str, object]] = []

        def fake_firecrawl(**kwargs):  # type: ignore[no-untyped-def]
            firecrawl_calls.append(kwargs)
            return {
                "provider": "firecrawl",
                "transport": "env",
                "query": kwargs["query"],
                "answer": "",
                "results": [
                    {
                        "provider": "firecrawl",
                        "source": "web",
                        "title": "OpenAI Pricing",
                        "url": "https://openai.com/api/pricing/",
                        "snippet": "Official pricing",
                        "content": "Official pricing content" if kwargs["include_content"] else "",
                    }
                ],
                "citations": [
                    {"title": "OpenAI Pricing", "url": "https://openai.com/api/pricing/"}
                ],
            }

        client._search_firecrawl = fake_firecrawl  # type: ignore[method-assign]
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [],
            "citations": [],
        }

        client._search_web_blended(
            query="OpenAI pricing official",
            mode="docs",
            intent="resource",
            strategy="verify",
            decision=RouteDecision(
                provider="firecrawl",
                reason="docs primary",
                firecrawl_categories=("research",),
                result_profile="resource",
            ),
            max_results=3,
            include_content=False,
            include_answer=False,
            include_domains=["openai.com"],
            exclude_domains=None,
        )

        self.assertTrue(firecrawl_calls)
        self.assertTrue(firecrawl_calls[0]["include_content"])

    def test_exa_search_uses_keyword_mode_for_official_pricing_query(self) -> None:
        client = MySearchClient()
        request_payloads: list[dict[str, object]] = []

        client._get_key_or_raise = lambda provider: type(  # type: ignore[method-assign]
            "FakeKey",
            (),
            {"key": "test-key", "source": "env"},
        )()

        def fake_request_json(**kwargs):  # type: ignore[no-untyped-def]
            request_payloads.append(dict(kwargs["payload"]))
            return {"results": []}

        client._request_json = fake_request_json  # type: ignore[method-assign]

        client._search_exa(
            query="MacBook Air M5 国行价格 官方",
            max_results=5,
            include_domains=["apple.com.cn"],
            exclude_domains=None,
            include_content=False,
            mode="web",
            intent="factual",
        )

        self.assertEqual(request_payloads[0]["type"], "keyword")

    def test_exa_search_uses_research_paper_category_for_pdf_mode(self) -> None:
        client = MySearchClient()
        request_payloads: list[dict[str, object]] = []

        client._get_key_or_raise = lambda provider: type(  # type: ignore[method-assign]
            "FakeKey",
            (),
            {"key": "test-key", "source": "env"},
        )()

        def fake_request_json(**kwargs):  # type: ignore[no-untyped-def]
            request_payloads.append(dict(kwargs["payload"]))
            return {"results": []}

        client._request_json = fake_request_json  # type: ignore[method-assign]

        client._search_exa(
            query="DeepSeek R1 paper pdf",
            max_results=5,
            include_domains=["arxiv.org"],
            exclude_domains=None,
            include_content=False,
            mode="pdf",
            intent="resource",
        )

        self.assertEqual(request_payloads[0]["category"], "research paper")

    def test_search_verify_conflicts_trigger_xai_arbitration(self) -> None:
        client = MySearchClient()
        client._provider_can_serve = lambda provider: True  # type: ignore[method-assign]
        client.keyring.has_provider = lambda provider: True  # type: ignore[method-assign]
        client._probe_provider_status = lambda provider, key_count: {  # type: ignore[method-assign]
            "status": "ok",
            "error": "",
            "checked_at": "2026-03-24T00:00:00+00:00",
        }
        client.config.xai.search_mode = "official"
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "Update note",
                    "url": "https://updates.example.com/post-a",
                    "snippet": "Provider A",
                    "content": "",
                }
            ],
            "citations": [{"title": "Update note", "url": "https://updates.example.com/post-a"}],
        }
        client._search_firecrawl = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "firecrawl",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "firecrawl",
                    "source": "web",
                    "title": "Update note mirror",
                    "url": "https://blog.updates.example.com/post-b",
                    "snippet": "Provider B",
                    "content": "Mirror content" if kwargs.get("include_content") else "",
                }
            ],
            "citations": [
                {"title": "Update note mirror", "url": "https://blog.updates.example.com/post-b"}
            ],
        }
        client._search_xai = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "xai",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "The mirror post is newer but both sources describe the same rollout.",
            "results": [
                {
                    "provider": "xai",
                    "source": "web",
                    "title": "Arbitration source",
                    "url": "https://news.example.com/rollout",
                    "snippet": "",
                    "content": "",
                }
            ],
            "citations": [
                {"title": "Arbitration source", "url": "https://news.example.com/rollout"},
                {"title": "Second source", "url": "https://another.example.com/rollout"},
            ],
        }

        result = client.search(
            query="vendor rollout status",
            mode="web",
            strategy="verify",
            provider="auto",
            include_answer=False,
            max_results=5,
        )

        self.assertEqual(result["evidence"]["arbitration_source"], "xai")
        self.assertEqual(
            result["evidence"]["xai_arbitration_summary"],
            "The mirror post is newer but both sources describe the same rollout.",
        )
        self.assertEqual(result["evidence"]["xai_arbitration_confidence"], "high")
        self.assertEqual(result["evidence"]["answer_source"], "xai_arbitration")
        self.assertIn("low-source-diversity", result["evidence"]["conflicts"])

    def test_research_falls_back_to_local_summary_when_xai_summary_missing(self) -> None:
        client = MySearchClient()
        client._provider_can_serve = lambda provider: False if provider.name == "xai" else True  # type: ignore[method-assign]
        client.search = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "intent": "resource",
            "strategy": "deep",
            "answer": "",
            "results": [
                {
                    "title": "Primary finding",
                    "url": "https://docs.example.com/primary",
                    "snippet": "Background mode lets requests run asynchronously.",
                    "content": "",
                }
            ],
            "citations": [
                {"title": "Primary finding", "url": "https://docs.example.com/primary"},
            ],
            "evidence": {
                "providers_consulted": ["tavily"],
                "verification": "single-provider",
                "citation_count": 1,
                "source_diversity": 1,
                "source_domains": ["example.com"],
                "official_source_count": 1,
                "official_mode": "strict",
                "confidence": "high",
                "conflicts": [],
            },
        }
        client.extract_url = lambda **kwargs: {  # type: ignore[method-assign]
            "url": kwargs["url"],
            "provider": "firecrawl",
            "content": "Background mode lets requests run asynchronously and finish later without blocking the client.",
            "cache": {"extract": {"hit": False, "ttl_seconds": 300}},
        }

        result = client.research(
            query="OpenAI background mode official docs",
            mode="docs",
            strategy="deep",
            include_social=False,
            scrape_top_n=1,
        )

        self.assertIn("Primary finding:", result["research_summary"])
        self.assertIn("Coverage:", result["research_summary"])
        self.assertIn("Primary finding", result["research_summary"])
        self.assertEqual(result["summary"], result["research_summary"])
        self.assertEqual(result["confidence"], "high")

    def test_research_summary_fallback_uses_title_based_synthesis_for_comparison_queries(self) -> None:
        client = MySearchClient()

        summary = client._build_research_summary_fallback(
            query="best search MCP server 2026",
            web_search={"intent": "exploratory", "answer": ""},
            pages=[
                {
                    "url": "https://fast.io/resources/best-mcp-servers-search/",
                    "excerpt": "This long page starts with marketing copy that should not dominate the summary.",
                }
            ],
            citations=[
                {
                    "title": "Best MCP Servers for Search in 2026 - Top 10 Tools",
                    "url": "https://fast.io/resources/best-mcp-servers-search/",
                },
                {
                    "title": "List of Top MCP Servers for March 20, 2026",
                    "url": "https://mcpmarket.com/daily/top-mcp-server-list-march-20-2026",
                },
            ],
            social=None,
            evidence={
                "providers_consulted": ["firecrawl", "exa"],
                "citation_count": 2,
                "confidence": "medium",
                "research_plan": {"scrape_top_n": 2},
            },
        )

        self.assertIn("comparative rather than authoritative", summary)
        self.assertIn("Best MCP Servers for Search in 2026 - Top 10 Tools", summary)
        self.assertNotIn("marketing copy", summary)

    def test_research_anchors_web_discovery_to_tavily_for_generic_queries(self) -> None:
        client = MySearchClient()
        search_calls: list[dict[str, object]] = []

        def fake_search(**kwargs):  # type: ignore[no-untyped-def]
            search_calls.append(kwargs)
            return {
                "provider": "tavily",
                "intent": "exploratory",
                "strategy": "deep",
                "answer": "",
                "results": [
                    {
                        "title": "Primary result",
                        "url": "https://example.com/primary",
                        "snippet": "Primary result",
                        "content": "",
                    }
                ],
                "citations": [{"title": "Primary result", "url": "https://example.com/primary"}],
                "evidence": {
                    "providers_consulted": ["tavily"],
                    "verification": "single-provider",
                    "citation_count": 1,
                    "source_diversity": 1,
                    "source_domains": ["example.com"],
                    "official_source_count": 0,
                    "official_mode": "off",
                    "confidence": "medium",
                    "conflicts": [],
                },
            }

        client.search = fake_search  # type: ignore[method-assign]
        client.extract_url = lambda **kwargs: {  # type: ignore[method-assign]
            "url": kwargs["url"],
            "provider": "firecrawl",
            "content": "content",
            "cache": {"extract": {"hit": False, "ttl_seconds": 300}},
        }
        client.research(
            query="best search MCP server 2026",
            mode="web",
            strategy="deep",
            include_social=False,
            scrape_top_n=1,
        )

        self.assertTrue(search_calls)
        self.assertEqual(search_calls[0]["provider"], "tavily")

    def test_research_falls_back_to_exa_discovery_when_web_discovery_fails(self) -> None:
        client = MySearchClient()
        client._provider_can_serve = lambda provider: provider.name == "exa"  # type: ignore[method-assign]

        def failing_search(**kwargs):  # type: ignore[no-untyped-def]
            raise MySearchError("tavily request failed (HTTP 503): upstream unavailable")

        client.search = failing_search  # type: ignore[method-assign]
        client._search_exa = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "exa",
            "transport": "env",
            "query": kwargs["query"],
            "results": [
                {
                    "provider": "exa",
                    "title": "Best MCP Servers for Search in 2026 - Top 10 Tools",
                    "url": "https://fast.io/resources/best-mcp-servers-search/",
                    "snippet": "Comparison-heavy page",
                    "content": "",
                }
            ],
            "citations": [
                {
                    "title": "Best MCP Servers for Search in 2026 - Top 10 Tools",
                    "url": "https://fast.io/resources/best-mcp-servers-search/",
                }
            ],
        }
        client.extract_url = lambda **kwargs: {  # type: ignore[method-assign]
            "url": kwargs["url"],
            "provider": "firecrawl",
            "content": "A structured comparison of MCP servers for search-focused agent workflows.",
            "cache": {"extract": {"hit": False, "ttl_seconds": 300}},
        }

        result = client.research(
            query="best search MCP server 2026",
            mode="web",
            strategy="deep",
            include_social=False,
            scrape_top_n=1,
        )

        self.assertEqual(result["web_search"]["provider"], "exa")
        self.assertEqual(result["web_search"]["fallback"]["to"], "exa")
        self.assertIn("comparative rather than authoritative", result["summary"])
        self.assertEqual(result["evidence"]["providers_consulted"], ["exa"])


if __name__ == "__main__":
    unittest.main()
