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


class ConflictingVersionClaimsTests(unittest.TestCase):
    """同一版本问题被不同来源用**不同主版本号**回答时必须报警。

    缺陷（loop38 在 `failure-version-attribution-01` 上实测）：该行的
    `evidence.conflicts` 是 `[]`、`confidence` 是 `high`，而池子里三个来源
    互相矛盾 —— oracle.com 说 `JDK 26`、wikipedia 说 `Java SE 27`。
    既有的判据全在看**来源结构**（多样性、provider 数、官方源覆盖），
    没有一条看内容是否一致。
    """

    JAVA_RESULTS = [
        {
            "url": "https://www.oracle.com/java/technologies/downloads",
            "title": "Java Downloads - Oracle",
            "snippet": (
                "JDK 26 is the latest release of the Java SE Platform. "
                "JDK 25 is the latest Long-Term Support (LTS) release of the Java SE Platform."
            ),
        },
        {
            "url": "https://en.wikipedia.org/wiki/Java_version_history",
            "title": "Java version history",
            "snippet": "| Latest version:Java SE 27 | | 71 | September 15, 2026 |",
        },
        {
            "url": "https://www.jrebel.com/blog/java-lts",
            "title": "What is Java LTS and Why Does It Matter?",
            "snippet": (
                "## What is the Latest Version of Java?\n\n"
                "> The latest version of Java is Java 25, which is also a Java LTS version.\n\n"
                "### Java 21\n\nJava 21 was released in September 2023. "
                "Java 21 is scheduled to receive premier support through September 2028."
            ),
        },
    ]

    def test_official_and_third_party_disagreement_is_reported(self) -> None:
        claims = software_version.conflicting_version_claims(
            query="latest stable version of Java",
            results=self.JAVA_RESULTS,
        )
        self.assertEqual(
            claims,
            {25: ["www.jrebel.com"], 26: ["www.oracle.com"], 27: ["en.wikipedia.org"]},
        )

    def test_future_support_clause_does_not_suppress_the_whole_page(self) -> None:
        """否定判据必须按**条款**生效，不能按整篇。

        实测 jrebel 那段里 `The latest version of Java is Java 25` 是有效声明，
        而同一页面的**别处**写着 "Java 21 is scheduled to receive premier support"。
        整篇级判据会因为那个 `scheduled` 把 25 一起丢掉 —— 结论碰巧对，
        理由却是错的：换成任何一页提到未来版本的真实声明都会被连带漏掉。
        """
        claims = software_version.conflicting_version_claims(
            query="latest stable version of Java",
            results=[self.JAVA_RESULTS[2]],
        )
        self.assertEqual(claims.get(21), None)
        self.assertIn(25, software_version._asserted_versions_from_text(
            self.JAVA_RESULTS[2]["snippet"], subject_tokens=("Java", "java")
        ))

    def test_bare_major_versions_are_read_from_official_spellings(self) -> None:
        """`JDK 26` / `Java SE 27` 是官方写法，不能因为"没带点"就看不见。

        与 `_software_version_candidates_from_text` 的目的**相反**：那个要挑出
        正确答案所以收得极紧（只认带点的语义版本号）；这个要在"回答者自己都看
        不见"时报警，必须认裸主版本号。收紧靠**句法**（版本号必须是"最新"的
        宾语），不靠放宽主语锚点 —— 放宽锚点会同时放行
        `Minecraft Java Edition 26.1.2`，那是 loop36 刚建立起来的保护。
        """
        claims = software_version.conflicting_version_claims(
            query="latest stable version of Java",
            results=self.JAVA_RESULTS,
        )
        self.assertIn(26, claims)
        self.assertIn(27, claims)

    def test_lts_version_is_not_mistaken_for_the_latest_release(self) -> None:
        """`JDK 25 is the latest Long-Term Support (LTS) release` 不是主版本声明。

        这句里的 `latest` 修饰的是 `LTS`，断言的是"哪个是最新 LTS"，
        而不是"哪个是最新正式版"。实测把 25 也算进来的话，
        oracle 单页就会自报 26 与 25 两个值。
        """
        oracle_only = [self.JAVA_RESULTS[0]]
        claims = software_version.conflicting_version_claims(
            query="latest stable version of Java",
            results=oracle_only,
        )
        self.assertEqual(claims, {})

    def test_agreement_across_sources_is_not_a_conflict(self) -> None:
        results = [
            {
                "url": "https://docs.python.org/3/",
                "title": "Python docs",
                "snippet": "The latest version of Python is 3.14.7.",
            },
            {
                "url": "https://www.python.org/downloads",
                "title": "Python downloads",
                "snippet": "Python 3.14.7 is the latest release of Python.",
            },
        ]
        self.assertEqual(
            software_version.conflicting_version_claims(
                query="what is the latest stable version of Python", results=results
            ),
            {},
        )

    def test_non_version_queries_are_ignored(self) -> None:
        self.assertEqual(
            software_version.conflicting_version_claims(
                query="2026 Oscars best picture winner", results=self.JAVA_RESULTS
            ),
            {},
        )

    def test_conflict_defers_confidence_and_surfaces_in_evidence(self) -> None:
        """冲突必须**走到 evidence**：标签、具体值、confidence 三者齐备。

        只测检测函数的返回值是"通过得不对"——实测按这个写法做的变异
        （把写 evidence 的那两行注释掉）测试**依然全绿**，因为它验的是
        函数返回了什么，不是调用方有没有用上。这里走 `_augment_evidence_summary`
        这条真实入口。
        """
        from mysearch.clients import MySearchClient

        client = MySearchClient()
        enriched = client._augment_evidence_summary(
            result={
                "provider": "hybrid",
                "results": self.JAVA_RESULTS,
                "citations": [
                    {"title": item["title"], "url": item["url"]} for item in self.JAVA_RESULTS
                ],
                "evidence": {
                    "providers_consulted": ["tavily", "firecrawl"],
                    "verification": "cross-provider",
                },
            },
            query="latest stable version of Java",
            mode="web",
            intent="factual",
            include_domains=None,
        )

        evidence = enriched["evidence"]
        self.assertIn("conflicting-version-claims", evidence["conflicts"])
        self.assertEqual(
            evidence["conflicting_version_claims"],
            {
                "27": ["en.wikipedia.org"],
                "26": ["www.oracle.com"],
                "25": ["www.jrebel.com"],
            },
        )
        self.assertEqual(evidence["confidence"], "medium")

    def test_conflict_detail_names_the_competing_values(self) -> None:
        """只给标签的话"哪里不一致"是不可见的。

        `conflicting-version-claims` 这个标签和 `low-source-diversity` 之类
        形状相同、内容不同：前者必须带出**具体是哪几个版本、谁在说**，
        否则用户看不到分歧在哪，仲裁方也只能把分歧重新猜一遍。
        """
        from mysearch.research import finalize

        detail = finalize._conflicting_version_claims_detail(
            query="latest stable version of Java",
            results=self.JAVA_RESULTS,
        )
        self.assertEqual(
            detail,
            {
                "27": ["en.wikipedia.org"],
                "26": ["www.oracle.com"],
                "25": ["www.jrebel.com"],
            },
        )
        self.assertEqual(
            finalize._conflicting_version_claims_detail(
                query="2026 Oscars best picture winner", results=self.JAVA_RESULTS
            ),
            {},
        )

    def test_arbitration_prompt_carries_the_competing_values(self) -> None:
        """仲裁问题里要出现具体版本号，不能只有标签。"""
        from mysearch.clients import MySearchClient
        from mysearch.research import finalize

        client = MySearchClient()
        captured: dict[str, object] = {}
        client._search_xai = lambda **kwargs: (  # type: ignore[method-assign]
            captured.update(kwargs),
            {"answer": "", "citations": []},
        )[1]
        client._provider_can_serve = lambda provider: True  # type: ignore[method-assign]
        client.config.xai.search_mode = "official"

        client._apply_xai_arbitration(
            query="latest stable version of Java",
            result={
                "provider": "hybrid",
                "results": self.JAVA_RESULTS,
                "evidence": {
                    "providers_consulted": ["tavily", "firecrawl"],
                    "conflicts": ["conflicting-version-claims"],
                    "conflicting_version_claims": finalize._conflicting_version_claims_detail(
                        query="latest stable version of Java", results=self.JAVA_RESULTS
                    ),
                },
            },
            include_domains=None,
            exclude_domains=None,
            from_date=None,
            to_date=None,
        )

        prompt = str(captured.get("query") or "")
        self.assertIn("Reported versions:", prompt)
        self.assertIn("27 (en.wikipedia.org)", prompt)
        self.assertIn("26 (www.oracle.com)", prompt)


class AssertedMajorVersionAnswerTests(unittest.TestCase):
    """官方页只写**裸主版本号**时，答案不能只靠透传上游。

    生产实测（2026-09-24）：`latest stable version of Java` 答 `JDK 25`
    （上游透传的过期值），而同一结果集里 oracle.com 写着
    `JDK 27 is the latest release of the Java SE Platform.`。
    根因：`_software_version_candidates_from_text` 只认带点版本号，官方页
    通篇裸主版本号 → 零候选 → 直接返回空。

    这里全部走 `_apply_software_version_answer_override` 这条**真实入口**：
    只测 `_extract_software_version_answer` 的返回值，无法证明调用方用上了它。
    """

    ORACLE_LIVE = [
        {
            "url": "https://ops.java/releases/",
            "title": "JDK Releases - Ops.java",
            "snippet": "# JDK Releases\n| | 2028-09-19 | JDK 31 | |",
        },
        {
            "url": "https://www.oracle.com/java/technologies/downloads/",
            "title": "Java Downloads | Oracle",
            "snippet": (
                "JDK 27 is the latest release of the Java SE Platform. "
                "JDK 25 is the latest Long-Term Support (LTS) release of the Java SE Platform."
            ),
        },
        {
            "url": "https://en.wikipedia.org/wiki/Java_version_history",
            "title": "Java version history - Wikipedia",
            "snippet": "| Latest version:Java SE 27 | | 71 | September 15, 2026 |",
        },
    ]

    def _override(self, *, query: str, results: list[dict[str, object]], answer: str = ""):
        client = MySearchClient()
        return client._apply_software_version_answer_override(
            query=query,
            mode="web",
            intent="factual",
            result={"answer": answer, "results": results, "evidence": {}},
        )

    def test_stale_upstream_answer_is_corrected_from_official_page(self) -> None:
        updated = self._override(
            query="latest stable version of Java",
            results=self.ORACLE_LIVE,
            answer=(
                "The latest stable version of Java is JDK 25. "
                "It is an LTS release with long-term support."
            ),
        )
        self.assertEqual(updated["answer"], "The latest stable version of Java is 27.")
        self.assertEqual(
            updated["evidence"]["answer_source"], "software-version-extraction"
        )

    def test_official_spellings_are_read_in_all_three_forms(self) -> None:
        for text in (
            "JDK 27 is the latest release of the Java SE Platform.",
            "Latest version: Java SE 27",
            "Latest version:Java SE 27",
            "The latest version of Java is JDK 27.",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    software_version._asserted_versions_from_text(
                        text, subject_tokens=("Java", "java")
                    ),
                    [27],
                )

    def test_disagreeing_sources_are_not_guessed(self) -> None:
        """多个来源各执一词时不猜 —— 那该由冲突检测与 confidence 表达。

        这条同时守住本模块"宁可不说也不编造"的既有契约。
        """
        results = list(self.ORACLE_LIVE) + [
            {
                "url": "https://www.jrebel.com/blog/java-lts",
                "title": "What is Java LTS?",
                "snippet": "> The latest version of Java is Java 25, which is also a Java LTS version.",
            }
        ]
        updated = self._override(query="latest stable version of Java", results=results)
        self.assertEqual(updated["answer"], "")

    def test_other_products_version_still_cannot_answer(self) -> None:
        """loop36 的保护不能因为接进应答路径而失守。

        `Minecraft Java Edition 27 is the latest release` 的紧邻实词是
        `Edition`（既非主语也非版本标记）。放宽锚点会重新放行这类编造，
        所以修法是**句法**而不是放宽锚点。
        """
        results = [
            {
                "url": "https://gamercubic.com/latest-version-of-minecraft-java",
                "title": "Latest Version of Minecraft: Java, Bedrock",
                "snippet": "The latest stable Java version covered here is Minecraft Java Edition 26.1.2.",
            },
            {
                "url": "https://gamercubic.com/other",
                "title": "Minecraft Java Edition",
                "snippet": "Minecraft Java Edition 27 is the latest release.",
            },
            {
                "url": "https://forum.aspose.com/t/example",
                "title": "Aspose.Cells for Node.js via Java",
                "snippet": "Aspose.Cells for Node.js via Java 27 is the latest release.",
            },
        ]
        self.assertEqual(self._override(query="latest stable version of Java", results=results)["answer"], "")

    def test_a_different_subject_cannot_borrow_java_s_marker(self) -> None:
        """`JDK` / `SE` 是 Java 专属标记，不能被别的主语借走。

        实测泄漏：一个 Python 查询把 `JDK 27 is the latest release of the
        Java SE Platform` 读成 Python 的最新版 —— 官方页里主语名在数字
        **右侧**，左侧只剩标记，所以标记锚点还要求句内出现被问主语。
        """
        for query in ("what is the latest stable version of Python",
                      "latest stable version of Kubernetes"):
            with self.subTest(query=query):
                updated = self._override(query=query, results=self.ORACLE_LIVE)
                self.assertEqual(updated["answer"], "")

    def test_thousands_separator_is_not_a_version(self) -> None:
        """千分位数字不是版本号：`36,954,000` 会被 `current` 左侧命中形态 2。"""
        results = [
            {
                "url": "https://www.macrotrends.net/global-metrics/cities/tokyo/population",
                "title": "Tokyo, Japan Metro Area Population",
                "snippet": (
                    "The current metro area population of Tokyo in 2026 is 36,954,000, "
                    "a 0.22% decline from 2025."
                ),
            }
        ]
        self.assertEqual(
            software_version._asserted_versions_from_text(
                results[0]["snippet"], subject_tokens=("Tokyo", "population")
            ),
            [],
        )

    def test_python_dotted_versions_are_unaffected(self) -> None:
        """带点版本号仍由原有路径处理，回退分支不改变既有行为。"""
        updated = self._override(
            query="what is the latest stable version of Python",
            results=[
                {
                    "url": "https://www.python.org/downloads/",
                    "title": "Python downloads",
                    "snippet": "The latest stable version of Python is 3.14.7.",
                }
            ],
        )
        self.assertEqual(
            updated["answer"], "The latest stable version of Python is 3.14.7."
        )


if __name__ == "__main__":
    unittest.main()
