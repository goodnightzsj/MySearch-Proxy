from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mysearch import query_routing, ranking
from mysearch.research import sections


def _item(url: str, title: str = "", snippet: str = "") -> dict[str, object]:
    return {"url": url, "title": title, "snippet": snippet}


class BrandTokenMatchesDottedHostTest(unittest.TestCase):
    """品牌名带点号时必须匹配得到域名。

    真实缺陷（loop43 `docs-02` / `changelog-01`）：query 里写 `Next.js`，
    域名是 `nextjs.org` —— 裸子串匹配 `"next.js" in "nextjs.org"` 为假，
    于是 `host_brand_match` / `registered_domain_label_match` 双双为 False，
    `_is_probably_official_resource_result` 判 False，official 通路整体短路，
    未发布的 `docs-wip` 版本页留在第 1 位，`docs-02` 的 `assertion_pass_rate`
    掉到 0.0、margin 由 +5 翻成 −9.22。

    对照 `playwright.dev`（token 不带点）与 `react.dev` 都正常，所以只有
    `nextjs.org` 受害 —— 这正是"点号"这个变量的作用。
    """

    def test_dotted_brand_matches_the_dotless_domain(self) -> None:
        for token, domain in (
            ("next.js", "nextjs.org"),
            ("node.js", "nodejs.org"),
            ("vue.js", "vuejs.org"),
        ):
            with self.subTest(token=token):
                self.assertTrue(
                    query_routing._brand_matches_host(
                        query_tokens=[token], hostname=domain, registered_domain=domain
                    )
                )

    def test_brands_that_never_had_a_dot_still_match(self) -> None:
        # 既有行为不能回退：`playwright` / `react` 本就能匹配。
        for token, domain in (("playwright", "playwright.dev"), ("react", "react.dev")):
            with self.subTest(token=token):
                self.assertTrue(
                    query_routing._brand_matches_host(
                        query_tokens=[token], hostname=domain, registered_domain=domain
                    )
                )

    def test_an_unrelated_brand_does_not_match(self) -> None:
        self.assertFalse(
            query_routing._brand_matches_host(
                query_tokens=["acme"], hostname="nextjs.org", registered_domain="nextjs.org"
            )
        )

    def test_a_short_normalized_token_cannot_match_by_containment(self) -> None:
        # 归一化分支要求 >=4 字符，否则过短 token 会靠"包含"大面积误命中
        # （`js` 是 `nextjsorg` 的子串）。
        #
        # 只测**归一化分支本身**：`_brand_matches_host` 里 `js`/`or` 会先命中
        # 既有的裸子串分支并返回 True，把归一化分支的行为掩盖掉
        # （实测：合并写法下把门槛从 4 降到 1，测试仍全绿）。
        for token in ("js", "or", "nx"):
            with self.subTest(token=token):
                self.assertFalse(
                    query_routing._brand_token_matches_normalized_host(
                        token, hostname="nextjs.org", registered_domain="nextjs.org"
                    )
                )

    def test_the_normalized_branch_does_match_at_the_threshold(self) -> None:
        # 门槛的另一侧：达到长度且确实是域名前缀时应当命中。
        # 两条合起来把门槛钉在 4 —— 降到 1 会让 `js`/`or` 也通过归一化分支。
        self.assertTrue(
            query_routing._brand_token_matches_normalized_host(
                "next", hostname="nextjs.org", registered_domain="nextjs.org"
            )
        )
        self.assertTrue(
            query_routing._brand_token_matches_normalized_host(
                "next.js", hostname="nextjs.org", registered_domain="nextjs.org"
            )
        )

    def test_registered_domain_label_matches_handles_the_dot(self) -> None:
        self.assertTrue(
            query_routing._registered_domain_label_matches(
                registered_domain="nextjs.org", query_tokens=["next.js"]
            )
        )
        self.assertFalse(
            query_routing._registered_domain_label_matches(
                registered_domain="nextjs.org", query_tokens=["acme"]
            )
        )

    def test_the_official_policy_accepts_the_nextjs_page(self) -> None:
        # 端到端：修复前这 5 条全部判 False（official 通路短路）。
        result = sections._result_matches_official_policy(
            item=_item(
                "https://nextjs.org/docs/app/api-reference/functions/generate-metadata",
                "Functions: generateMetadata",
            ),
            mode="docs",
            query_tokens=query_routing._query_brand_tokens("Next.js generateMetadata docs"),
            include_domains=None,
            strict_official=True,
        )
        self.assertTrue(result)


class WorkingCopyPathRanksBelowPublishedTest(unittest.TestCase):
    """同一站点的**未发布副本**必须排在发布版之后。

    实测（loop43 `docs-02`）：`nextjs.org/docs-wip/app/...` 与期望的
    `nextjs.org/docs/app/...` 是同一页的两个版本，但 `docs-wip` 的标题多了
    一个 `Next.js`（`Functions: generateMetadata | Next.js` vs `Functions:
    generateMetadata`），而 `nextjs` 属于 topic token（本来就在域名里），
    于是它靠 `topic_total_hits` 4 > 3 **赢在"标题重复了域名"**，与权威性无关。
    """

    def test_recognises_working_copy_segments(self) -> None:
        for path in (
            "/docs-wip/app/api-reference/functions/generate-metadata",
            "/preview/docs/x",
            "/docs/staging/y",
            "/canary/z",
        ):
            with self.subTest(path=path):
                self.assertTrue(query_routing._looks_like_working_copy_path(path))

    def test_does_not_fire_on_normal_paths(self) -> None:
        for path in (
            "/docs/app/api-reference/functions/generate-metadata",
            "/docs/15/app/api-reference/functions/generate-metadata",
            "/blog/next-16",
            "/reference/react/useActionState",
        ):
            with self.subTest(path=path):
                self.assertFalse(query_routing._looks_like_working_copy_path(path))

    def test_does_not_fire_on_a_substring_inside_a_slug(self) -> None:
        # 只认**独立的路径段**，`previewing-guide` 这类 slug 不该被误伤。
        self.assertFalse(query_routing._looks_like_working_copy_path("/docs/previewing-guide"))

    def _rank(self, url: str, title: str) -> tuple[int, ...]:
        query = "Next.js generateMetadata docs"
        return ranking._resource_result_rank(
            query=query,
            mode="docs",
            item=_item(url, title),
            query_tokens=query_routing._query_brand_tokens(query),
            precision_tokens=query_routing._query_precision_tokens(query),
            exact_identifier_tokens=query_routing._query_exact_identifier_tokens(query),
            topic_specific_tokens=query_routing._query_topic_specific_tokens(query),
            include_domains=None,
            strict_official=True,
        )

    def test_the_published_page_outranks_the_working_copy(self) -> None:
        # 关键：`docs-wip` 的标题更长（含 `Next.js`），若不看路径只看标题
        # 它确实会赢。这条测试锁住"路径形态必须能压过标题那点优势"。
        working_copy = self._rank(
            "https://nextjs.org/docs-wip/app/api-reference/functions/generate-metadata",
            "Functions: generateMetadata | Next.js",
        )
        published = self._rank(
            "https://nextjs.org/docs/app/api-reference/functions/generate-metadata",
            "Functions: generateMetadata",
        )
        self.assertGreater(published, working_copy)

    def test_a_non_strict_query_is_unaffected(self) -> None:
        # 判据只对 `strict_official` 生效，与既有两个同类判据同一层级。
        # 所以非 strict 下 `docs-wip` **仍然赢** —— 因为它的标题多一个 `Next.js`
        # （`nextjs` 是 topic token，本来就在域名里），`topic_total_hits` 4 > 3。
        # 这条测试锁住的正是"判据的作用域"，不是"问题普遍解决了"。
        query = "Next.js generateMetadata docs"
        common = {
            "query": query,
            "mode": "docs",
            "query_tokens": query_routing._query_brand_tokens(query),
            "precision_tokens": query_routing._query_precision_tokens(query),
            "exact_identifier_tokens": query_routing._query_exact_identifier_tokens(query),
            "topic_specific_tokens": query_routing._query_topic_specific_tokens(query),
            "include_domains": None,
        }
        working_copy = ranking._resource_result_rank(
            item=_item(
                "https://nextjs.org/docs-wip/app/api-reference/functions/generate-metadata",
                "Functions: generateMetadata | Next.js",
            ),
            strict_official=False,
            **common,
        )
        published = ranking._resource_result_rank(
            item=_item(
                "https://nextjs.org/docs/app/api-reference/functions/generate-metadata",
                "Functions: generateMetadata",
            ),
            strict_official=False,
            **common,
        )
        self.assertGreater(working_copy, published)


if __name__ == "__main__":
    unittest.main()
