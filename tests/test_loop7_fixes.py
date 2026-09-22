from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mysearch.clients import MySearchClient
from mysearch.research import software_version


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

    def test_version_candidates_scope_prerelease_markers_to_the_same_sentence(self) -> None:
        client = MySearchClient()

        candidates = client._software_version_candidates_from_text(
            "Python 3.14.6 is the latest stable release. "
            "Development versions of Python 3.15 are available for testing."
        )

        self.assertEqual([candidate[0] for candidate in candidates], ["3.14.6"])

    def test_exact_patch_release_beats_major_only_status_page(self) -> None:
        client = MySearchClient()
        result = {
            "answer": "The latest stable version of Python is 3.14.3.",
            "results": [
                {
                    "title": "Python documentation by version",
                    "url": "https://www.python.org/doc/versions",
                    "snippet": "Release versions. Python 3.14.",
                },
                {
                    "title": "The latest Python version",
                    "url": "https://phoenixnap.com/kb/latest-python-version",
                    "snippet": "The latest stable version of Python is 3.14.3.",
                },
            ],
            "primary_search": {
                "results": [
                    {
                        "title": "Download Python",
                        "url": "https://www.python.org/downloads/",
                        "snippet": "Download the latest version of Python. Download Python 3.14.6.",
                    }
                ]
            },
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

    def test_version_index_table_does_not_displace_asserted_answer(self) -> None:
        """A versions table listing future branches must not win on authority.

        Loop 12 regression: the real devguide page renders its branches as a
        table, so "future Python 3.16" is nowhere near the "3.16" cell and the
        prerelease marker filter cannot see it. The table row scores only the
        generic positive marker, yet devguide.python.org/versions/ is a
        canonical host and outranked the page that actually asserts the answer.
        """
        client = MySearchClient()
        result = {
            "answer": "",
            "results": [
                {
                    "title": "Status of Python versions - Python Developer's Guide",
                    "url": "https://devguide.python.org/versions/",
                    "snippet": (
                        "Supported versions. Python 3.8 Python 3.11 Python 3.12 "
                        "Python 3.13 Python 3.14 Python 3.15 Python 3.16"
                    ),
                    "content": "",
                },
                {
                    "title": "Download Python - Python.org",
                    "url": "https://www.python.org/downloads/",
                    "snippet": "Download the latest version of Python. Download Python 3.14.7.",
                    "content": "",
                },
                {
                    "title": "History of Python - Wikipedia",
                    "url": "https://en.wikipedia.org/wiki/History_of_Python",
                    "snippet": "Python 3.14.6 is the latest stable release.",
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
            "The latest stable version of Python is 3.14.7.",
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

        client._request_json_once = fake_request_json  # type: ignore[method-assign]
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

        client._request_json_once = fake_request_json  # type: ignore[method-assign]
        client.crawl_site(
            url="https://fastapi.tiangolo.com/tutorial/background-tasks/",
            limit=5,
            max_depth=1,
            crawl_entire_domain=False,
        )

        payload = calls[0]["payload"]
        assert isinstance(payload, dict)
        self.assertFalse(payload["crawlEntireDomain"])


class SoftwareVersionGroundingTests(unittest.TestCase):
    """版本号必须属于**被问的那个软件**，否则宁可不说。

    实测（2026-09-22，生产）：`latest stable version of Java` 反复返回源里
    不存在的版本号 —— `4.5`、`10.7.3`，以及部署 `918f752` 后仍未修好的
    `26.1.2`（那是 **Minecraft Java Edition** 的版本，出自
    `gamercubic.com`，Tavily 侧 `answer=None`，句子是我们自己合成的）。
    根因是归属校验只问"附近出现过主语吗"，而主语名常常**属于别的产品**。
    现在要求主语是版本号的**紧邻锚点**。
    """

    def _candidates(self, text: str, subject: str) -> list[str]:
        return [
            version
            for version, _tuple, _score in software_version._software_version_candidates_from_text(
                text, subject_tokens=(subject,)
            )
        ]

    def test_other_products_version_is_rejected(self) -> None:
        # `javafx` 里含 `java`，子串匹配会放行 —— 必须用词边界。
        self.assertEqual(self._candidates("JavaFX 10.7.3 released", "Java"), [])
        self.assertEqual(self._candidates("Gradle 4.5 stable release", "Java"), [])

    def test_subject_owned_by_another_product_is_rejected(self) -> None:
        """紧邻锚点判定：`Minecraft Java Edition 26.1.2` 的锚点是 `Edition`。

        这是 918f752 部署后仍在生产中复现的缺陷（2026-09-22）。
        """
        self.assertEqual(
            self._candidates(
                "The latest stable Java version covered here is Minecraft Java Edition 26.1.2.",
                "Java",
            ),
            [],
        )
        self.assertEqual(
            self._candidates("The latest stable Minecraft Java version is 26.1.2.", "Java"),
            [],
        )

    def test_a_version_label_between_subject_and_number_is_not_a_bridge(self) -> None:
        """`version` 出现在主语与数字之间，说明主语名属于别的产品，不是锚点。"""
        self.assertEqual(self._candidates("Java version is 26.1.2.", "Java"), [])
        self.assertEqual(
            self._candidates("The latest stable version of Java is 25.0.1.", "Java"),
            ["25.0.1"],
        )

    def test_subject_used_as_a_qualifier_of_another_product_is_rejected(self) -> None:
        """锚点恰是主语还不够 —— 主语可能是别的产品的**限定语**。

        这是加锚定后的第 4 个变体（2026-09-23）：Minecraft 页被挡掉后，
        答案落到了 `Aspose.Cells for Node.js via Java 25.12` —— 那 25.12 是
        Aspose 的版本，紧邻的实词却**正是** `Java`。
        """
        self.assertEqual(
            self._candidates("Aspose.Cells for Node.js via Java 25.12 is available.", "Java"),
            [],
        )
        self.assertEqual(
            self._candidates("The latest stable version of Aspose.Cells for Java is 25.12.", "Java"),
            [],
        )

    def test_the_whole_java_extraction_declines_to_answer(self) -> None:
        """端到端：源里只有别家产品的版本时，宁可**不回答**也不编造。

        用的是修复前实际抓到的 payload（含 Minecraft 与 Aspose 两页），
        旧代码在这一份上产出 `…is 26.1.2.`。
        """
        results = [
            {
                "url": "https://javawithus.com/faq/latest-version-of-java",
                "title": "What Is the Latest Version of Java? (2026)",
                "snippet": "As of 2026, the latest Java versions are Java 25 LTS and Java 26.",
            },
            {
                "url": "https://gamercubic.com/latest-version-of-minecraft-java",
                "title": "Latest Version of Minecraft: Java, Bedrock",
                "snippet": "The latest stable Java version covered here is Minecraft Java Edition 26.1.2.",
            },
            {
                "url": "https://forum.aspose.com/t/request-for-latest-stable-version/323912",
                "title": "Request for Latest Stable Version - Aspose.Cells for Node.js via Java",
                "snippet": "The latest stable version, Aspose.Cells for Node.js via Java 25.12, is available.",
            },
        ]
        self.assertEqual(
            software_version._extract_software_version_answer(
                query="latest stable version of Java", results=results
            ),
            "",
        )

    def test_subject_mention_makes_the_version_eligible(self) -> None:
        self.assertEqual(
            self._candidates("Python 3.14.7 is the latest stable release", "Python"),
            ["3.14.7"],
        )
        self.assertEqual(
            self._candidates("The latest stable version of Node.js is 26.7.0.", "Node.js"),
            ["26.7.0"],
        )

    def test_largest_version_still_wins_among_the_subject_s_own_versions(self) -> None:
        """主语校验之后，"最大者即最新"才是成立的启发式，不能被削弱。"""
        text = (
            "Python 3.14.3 is the latest stable release. "
            "Python 3.14.6 is the latest stable release with security fixes."
        )
        self.assertEqual(self._candidates(text, "Python"), ["3.14.3", "3.14.6"])


if __name__ == "__main__":
    unittest.main()
