import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from mysearch.clients import MySearchClient
from mysearch.config import MySearchConfig, ProviderConfig
from mysearch.keyring import MySearchKeyRing


class _FakeResponse:
    def __init__(self, *, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class MySearchTests(unittest.TestCase):
    def _provider(
        self,
        *,
        name: str,
        base_url: str,
        auth_mode: str,
        paths: dict[str, str],
        alternate_base_urls: dict[str, str] | None = None,
        search_mode: str = "official",
        api_keys: list[str] | None = None,
        keys_file: Path | None = None,
    ) -> ProviderConfig:
        return ProviderConfig(
            name=name,
            base_url=base_url,
            auth_mode=auth_mode,  # type: ignore[arg-type]
            auth_header="Authorization",
            auth_scheme="Bearer",
            auth_field="api_key",
            default_paths=paths,
            alternate_base_urls=alternate_base_urls or {},
            search_mode=search_mode,  # type: ignore[arg-type]
            api_keys=api_keys or [],
            keys_file=keys_file,
        )

    def _config(
        self,
        *,
        tavily_file: Path | None = None,
        firecrawl_file: Path | None = None,
        xai_file: Path | None = None,
        xai_search_mode: str = "official",
        xai_paths: dict[str, str] | None = None,
        xai_alternate_base_urls: dict[str, str] | None = None,
    ) -> MySearchConfig:
        return MySearchConfig(
            server_name="MySearch",
            timeout_seconds=30,
            xai_model="grok-test-model",
            tavily=self._provider(
                name="tavily",
                base_url="https://api.tavily.com",
                auth_mode="body",
                paths={"search": "/search", "extract": "/extract"},
                api_keys=["tvly-env-key-1", "tvly-env-key-2"],
                keys_file=tavily_file,
            ),
            firecrawl=self._provider(
                name="firecrawl",
                base_url="https://api.firecrawl.dev",
                auth_mode="bearer",
                paths={"search": "/v2/search", "scrape": "/v2/scrape"},
                api_keys=["fc-env-key"],
                keys_file=firecrawl_file,
            ),
            xai=self._provider(
                name="xai",
                base_url="https://api.x.ai/v1",
                auth_mode="bearer",
                paths=xai_paths or {"responses": "/responses", "social_search": ""},
                alternate_base_urls=xai_alternate_base_urls,
                search_mode=xai_search_mode,
                api_keys=["xai-env-key"],
                keys_file=xai_file,
            ),
        )

    def test_keyring_loads_env_and_file_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tavily_file = Path(temp_dir) / "accounts.txt"
            tavily_file.write_text(
                "\n".join(
                    [
                        "user1@example.com,password,tvly-file-key-1",
                        "tvly-file-key-2",
                    ]
                ),
                encoding="utf-8",
            )

            keyring = MySearchKeyRing(self._config(tavily_file=tavily_file))
            info = keyring.describe()

            self.assertEqual(info["tavily"]["count"], 4)
            self.assertEqual(info["firecrawl"]["count"], 1)
            self.assertEqual(info["xai"]["count"], 1)
            self.assertIn("env", info["tavily"]["sources"])
            self.assertIn("file", info["tavily"]["sources"])

    def test_route_prefers_xai_for_social_queries(self) -> None:
        client = MySearchClient(self._config())
        decision = client._route_search(
            query="what are people saying about MCP on X",
            mode="social",
            intent="status",
            provider="auto",
            sources=["x"],
            include_content=False,
            allowed_x_handles=None,
            excluded_x_handles=None,
        )

        self.assertEqual(decision.provider, "xai")
        self.assertEqual(decision.sources, ["x"])

    def test_route_prefers_firecrawl_for_docs_queries(self) -> None:
        client = MySearchClient(self._config())
        decision = client._route_search(
            query="Firecrawl pricing changelog",
            mode="docs",
            intent="resource",
            provider="auto",
            sources=["web"],
            include_content=False,
            allowed_x_handles=None,
            excluded_x_handles=None,
        )

        self.assertEqual(decision.provider, "firecrawl")
        self.assertEqual(decision.firecrawl_categories, ["research"])

    def test_route_prefers_tavily_for_news_queries(self) -> None:
        client = MySearchClient(self._config())
        decision = client._route_search(
            query="latest MCP server updates",
            mode="auto",
            intent="news",
            provider="auto",
            sources=["web"],
            include_content=False,
            allowed_x_handles=None,
            excluded_x_handles=None,
        )

        self.assertEqual(decision.provider, "tavily")
        self.assertEqual(decision.tavily_topic, "news")

    def test_route_returns_hybrid_for_web_and_x_sources(self) -> None:
        client = MySearchClient(self._config())
        decision = client._route_search(
            query="latest MySearch feedback",
            mode="auto",
            intent="status",
            provider="auto",
            sources=["web", "x"],
            include_content=False,
            allowed_x_handles=None,
            excluded_x_handles=None,
        )

        self.assertEqual(decision.provider, "hybrid")

    def test_resolve_intent_and_strategy_for_comparison_queries(self) -> None:
        client = MySearchClient(self._config())
        intent = client._resolve_intent(
            query="Tavily vs Firecrawl for docs search",
            mode="auto",
            intent="auto",
            sources=["web"],
        )
        strategy = client._resolve_strategy(
            mode="auto",
            intent=intent,
            strategy="auto",
            sources=["web"],
            include_content=False,
        )

        self.assertEqual(intent, "comparison")
        self.assertEqual(strategy, "verify")

    def test_search_verify_strategy_blends_tavily_and_firecrawl(self) -> None:
        client = MySearchClient(self._config())
        client._search_tavily = Mock(
            return_value={
                "provider": "tavily",
                "query": "compare MySearch providers",
                "answer": "Tavily is fast.",
                "results": [
                    {
                        "provider": "tavily",
                        "title": "Shared Result",
                        "url": "https://example.com/shared",
                        "snippet": "shared snippet",
                        "content": "",
                    },
                    {
                        "provider": "tavily",
                        "title": "Tavily Only",
                        "url": "https://example.com/tavily-only",
                        "snippet": "tavily only",
                        "content": "",
                    },
                ],
                "citations": [{"title": "Shared Result", "url": "https://example.com/shared"}],
            }
        )
        client._search_firecrawl = Mock(
            return_value={
                "provider": "firecrawl",
                "query": "compare MySearch providers",
                "answer": "",
                "results": [
                    {
                        "provider": "firecrawl",
                        "title": "Shared Result",
                        "url": "https://example.com/shared",
                        "snippet": "shared snippet from firecrawl",
                        "content": "full shared content",
                    },
                    {
                        "provider": "firecrawl",
                        "title": "Firecrawl Only",
                        "url": "https://example.com/firecrawl-only",
                        "snippet": "firecrawl only",
                        "content": "",
                    },
                ],
                "citations": [{"title": "Firecrawl Only", "url": "https://example.com/firecrawl-only"}],
            }
        )

        response = client.search(
            query="Tavily vs Firecrawl for docs search",
            mode="auto",
            intent="comparison",
            strategy="verify",
            sources=["web"],
            max_results=3,
        )

        self.assertEqual(response["provider"], "hybrid")
        self.assertEqual(response["route"]["selected"], "firecrawl+tavily")
        self.assertEqual(response["intent"], "comparison")
        self.assertEqual(response["strategy"], "verify")
        self.assertEqual(response["evidence"]["matched_results"], 1)
        self.assertEqual(response["results"][0]["matched_providers"], ["firecrawl", "tavily"])
        self.assertEqual(len(response["citations"]), 2)

    def test_health_reports_xai_search_mode(self) -> None:
        client = MySearchClient(self._config(xai_search_mode="compatible"))
        health = client.health()

        self.assertEqual(health["providers"]["xai"]["search_mode"], "compatible")

    def test_build_xai_payload_uses_current_responses_shape(self) -> None:
        client = MySearchClient(self._config())
        payload = client._build_xai_responses_payload(
            query="search X for Tavily feedback",
            sources=["x"],
            max_results=4,
            include_domains=None,
            exclude_domains=None,
            allowed_x_handles=["tavilyai"],
            excluded_x_handles=None,
            from_date="2026-03-01",
            to_date="2026-03-16",
            include_x_images=True,
            include_x_videos=False,
        )

        self.assertEqual(payload["model"], "grok-test-model")
        self.assertEqual(payload["tools"][0]["type"], "x_search")
        self.assertEqual(payload["tools"][0]["allowed_x_handles"], ["tavilyai"])
        self.assertTrue(payload["tools"][0]["enable_image_understanding"])
        self.assertIn("Return up to 4 relevant results", payload["input"][0]["content"])

    def test_request_json_supports_bearer_auth(self) -> None:
        client = MySearchClient(self._config())
        client.session.request = Mock(
            return_value=_FakeResponse(status_code=200, payload={"ok": True})
        )

        response = client._request_json(
            provider=client.config.firecrawl,
            method="POST",
            path="/v2/search",
            payload={"query": "hello"},
            key="fc-test-key",
        )

        self.assertEqual(response, {"ok": True})
        _, kwargs = client.session.request.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer fc-test-key")
        self.assertEqual(kwargs["json"], {"query": "hello"})

    def test_request_json_supports_body_auth(self) -> None:
        client = MySearchClient(self._config())
        client.session.request = Mock(
            return_value=_FakeResponse(status_code=200, payload={"ok": True})
        )

        response = client._request_json(
            provider=client.config.tavily,
            method="POST",
            path="/search",
            payload={"query": "hello"},
            key="tvly-test-key",
        )

        self.assertEqual(response, {"ok": True})
        _, kwargs = client.session.request.call_args
        self.assertEqual(kwargs["headers"], {})
        self.assertEqual(
            kwargs["json"],
            {"query": "hello", "api_key": "tvly-test-key"},
        )

    def test_extract_xai_citations_falls_back_to_annotations(self) -> None:
        client = MySearchClient(self._config())
        citations = client._extract_xai_citations(
            {
                "output": [
                    {
                        "content": [
                            {
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url": "https://example.com/post",
                                        "title": "Example Post",
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        )

        self.assertEqual(
            citations,
            [
                {
                    "type": "url_citation",
                    "url": "https://example.com/post",
                    "title": "Example Post",
                }
            ],
        )

    def test_request_json_reports_non_json_http_errors(self) -> None:
        client = MySearchClient(self._config())
        client.session.request = Mock(
            return_value=_FakeResponse(status_code=502, payload=None, text="upstream bad gateway")
        )

        with self.assertRaisesRegex(Exception, "HTTP 502: upstream bad gateway"):
            client._request_json(
                provider=client.config.firecrawl,
                method="POST",
                path="/v2/search",
                payload={"query": "hello"},
                key="fc-test-key",
            )

    def test_compatible_xai_mode_uses_provider_base_url_when_social_base_missing(self) -> None:
        client = MySearchClient(
            self._config(
                xai_search_mode="compatible",
                xai_paths={"responses": "/responses", "social_search": "/social/search"},
            )
        )
        client.session.request = Mock(
            return_value=_FakeResponse(
                status_code=200,
                payload={
                    "answer": "People on X are discussing MCP.",
                    "results": [],
                    "citations": [],
                },
            )
        )

        client._search_xai(
            query="search X for MCP",
            sources=["x"],
            max_results=5,
            allowed_x_handles=None,
            excluded_x_handles=None,
            from_date=None,
            to_date=None,
            include_x_images=False,
            include_x_videos=False,
        )

        args, _ = client.session.request.call_args
        self.assertEqual(args[1], "https://api.x.ai/v1/social/search")

    def test_compatible_xai_mode_uses_custom_social_gateway(self) -> None:
        client = MySearchClient(
            self._config(
                xai_search_mode="compatible",
                xai_paths={"responses": "/responses", "social_search": "/social/search"},
                xai_alternate_base_urls={"social_search": "https://social.example.com"},
            )
        )
        client.session.request = Mock(
            return_value=_FakeResponse(
                status_code=200,
                payload={
                    "answer": "People on X are positive about MySearch.",
                    "results": [
                        {
                            "author": "builder1",
                            "url": "https://x.com/builder1/status/1",
                            "text": "MySearch looks great",
                        }
                    ],
                },
            )
        )

        response = client._search_xai(
            query="search X for MySearch feedback",
            sources=["x"],
            max_results=5,
            allowed_x_handles=["builder1"],
            excluded_x_handles=None,
            from_date="2026-03-01",
            to_date="2026-03-16",
            include_x_images=False,
            include_x_videos=False,
        )

        self.assertEqual(response["provider"], "custom_social")
        self.assertEqual(response["results"][0]["title"], "builder1")
        args, kwargs = client.session.request.call_args
        self.assertEqual(args[1], "https://social.example.com/social/search")
        self.assertEqual(kwargs["json"]["allowed_x_handles"], ["builder1"])
        self.assertEqual(kwargs["json"]["from_date"], "2026-03-01")
        self.assertTrue(kwargs["json"]["source"] == "x")
        self.assertTrue(kwargs["json"]["query"].startswith("search X for MySearch feedback"))

    def test_compatible_xai_mode_rejects_web_search(self) -> None:
        client = MySearchClient(
            self._config(
                xai_search_mode="compatible",
                xai_paths={"responses": "/responses", "social_search": "/social/search"},
            )
        )

        with self.assertRaisesRegex(Exception, "only supports social/X queries"):
            client._search_xai(
                query="search the web for MySearch",
                sources=["web"],
                max_results=5,
                allowed_x_handles=None,
                excluded_x_handles=None,
                from_date=None,
                to_date=None,
                include_x_images=False,
                include_x_videos=False,
            )


if __name__ == "__main__":
    unittest.main()
