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


class SoftwareVersionFixTests(unittest.TestCase):
    def test_version_query_overrides_stale_primary_answer(self) -> None:
        client = MySearchClient()
        result = {
            "answer": (
                "The latest stable version of Python is 3.13. "
                "It was released in October 2024."
            ),
            "results": [
                {
                    "title": "What is the 'true' latest stable version of Python WRT library ... - Reddit",
                    "url": "https://www.reddit.com/r/learnpython/comments/example",
                    "snippet": "The official latest stable is 3.12.",
                    "content": "",
                },
                {
                    "title": "The latest Python version: Python 3.14 - Liquid Web",
                    "url": "https://www.liquidweb.com/blog/latest-python-version",
                    "snippet": (
                        "What's the latest Python version? "
                        "The latest stable version of Python is 3.14."
                    ),
                    "content": "",
                },
            ],
            "evidence": {},
        }

        updated = client._apply_software_version_answer_override(
            query="what is the latest stable version of Python",
            mode="web",
            intent="factual",
            result=result,
        )

        self.assertEqual(
            updated["answer"],
            "The latest stable version of Python is 3.14.",
        )
        self.assertEqual(
            updated["evidence"]["answer_source"],
            "software-version-extraction",
        )

    def test_version_query_reranks_reference_page_over_community_thread(self) -> None:
        client = MySearchClient()
        reddit = {
            "title": "What is the 'true' latest stable version of Python - Reddit",
            "url": "https://www.reddit.com/r/learnpython/comments/example",
            "snippet": "The official latest stable is 3.12.",
            "content": "",
        }
        liquidweb = {
            "title": "The latest Python version: Python 3.14 - Liquid Web",
            "url": "https://www.liquidweb.com/blog/latest-python-version",
            "snippet": "The latest stable version of Python is 3.14.",
            "content": "",
        }

        reranked = client._rerank_general_results(
            query="what is the latest stable version of Python",
            result_profile="web",
            results=[reddit, liquidweb],
            include_domains=None,
        )

        self.assertEqual(reranked[0]["url"], liquidweb["url"])

    def test_version_query_ignores_future_development_branch_versions(self) -> None:
        client = MySearchClient()
        result = {
            "answer": "",
            "results": [
                {
                    "title": "Status of Python versions - Python Developer's Guide",
                    "url": "https://devguide.python.org/versions/",
                    "snippet": (
                        "The main branch is currently the future Python 3.16, "
                        "and is the only branch that accepts new features."
                    ),
                    "content": "",
                },
                {
                    "title": "Download Python - Python.org",
                    "url": "https://www.python.org/downloads/",
                    "snippet": (
                        "Download the latest version of Python. Download Python 3.14.6. "
                        "Looking for Python with a different OS? Windows, Linux/Unix, "
                        "macOS, Android, iOS, other. Want to help test development "
                        "versions of Python 3.15? For more information visit the "
                        "Python Developer's Guide."
                    ),
                    "content": "",
                },
                {
                    "title": "The latest Python version: Python 3.14 - Liquid Web",
                    "url": "https://www.liquidweb.com/blog/latest-python-version/",
                    "snippet": (
                        "What's the latest Python version? "
                        "The latest stable version of Python is 3.14."
                    ),
                    "content": "",
                },
            ],
            "evidence": {},
        }

        updated = client._apply_software_version_answer_override(
            query="what is the latest stable version of Python",
            mode="web",
            intent="factual",
            result=result,
        )

        self.assertEqual(
            updated["answer"],
            "The latest stable version of Python is 3.14.6.",
        )
        self.assertEqual(
            updated["evidence"]["answer_source"],
            "software-version-extraction",
        )


class CrawlBreadthFixTests(unittest.TestCase):
    def test_crawl_site_defaults_to_crawl_entire_domain(self) -> None:
        client = MySearchClient()
        client._get_key_or_raise = _fake_key  # type: ignore[method-assign]
        calls: list[dict[str, object]] = []

        def fake_request_json(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            if str(kwargs.get("method")).upper() == "POST":
                return {"success": True, "id": "job-1"}
            return {"status": "completed", "total": 0, "completed": 0, "data": []}

        client._request_json = fake_request_json  # type: ignore[method-assign]
        client.crawl_site(
            url="https://fastapi.tiangolo.com/tutorial/background-tasks/",
            limit=5,
            max_depth=1,
        )

        payload = calls[0]["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["maxDiscoveryDepth"], 1)
        self.assertTrue(payload["crawlEntireDomain"])

    def test_crawl_site_can_opt_out_of_crawl_entire_domain(self) -> None:
        client = MySearchClient()
        client._get_key_or_raise = _fake_key  # type: ignore[method-assign]
        calls: list[dict[str, object]] = []

        def fake_request_json(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            if str(kwargs.get("method")).upper() == "POST":
                return {"success": True, "id": "job-2"}
            return {"status": "completed", "total": 0, "completed": 0, "data": []}

        client._request_json = fake_request_json  # type: ignore[method-assign]
        client.crawl_site(
            url="https://fastapi.tiangolo.com/tutorial/background-tasks/",
            limit=5,
            max_depth=1,
            crawl_entire_domain=False,
        )

        payload = calls[0]["payload"]
        assert isinstance(payload, dict)
        self.assertFalse(payload["crawlEntireDomain"])


if __name__ == "__main__":
    unittest.main()
