from __future__ import annotations

import unittest
from unittest.mock import patch

from mysearch.clients import MySearchClient


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


if __name__ == "__main__":
    unittest.main()
