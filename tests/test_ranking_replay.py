"""Reranking regression tests on a FROZEN candidate set.

Why this file exists
--------------------
Every full benchmark run asks upstream for fresh results, and the candidate set
changes between runs -- measured 2026-09-28: 36 of 46 rows (78%) had a different
candidate set across three archived runs. So a total-score delta cannot be
attributed to a code change; it is dominated by which pages upstream happened to
return.

Concretely: loop44's `non_working_copy` fix could not be observed at all in the
next benchmark run, because that run's candidates did not include `docs-wip` in
the first place -- the ranking code never got the chance to demote it. The A/B
replay of the two trees produced identical orderings.

The fix is to freeze the inputs. `tests/fixtures/ranking_replay_docs_02.json`
holds the candidate set from the loop43 run, where `docs-wip` DID appear and DID
rank first. Replaying it exercises the ordering logic deterministically, with no
network and no upstream drift.

The fixture stores `content_len` / `snippet_len` instead of the text itself.
`postprocess._result_quality_score` (postprocess.py:511) only reads `len()` of
those fields, so lengths reproduce the ordering exactly while keeping the fixture
at ~1.6 KB instead of 165 KB.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mysearch import query_routing, ranking

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "ranking_replay_docs_02.json"

WORKING_COPY_URL = (
    "https://nextjs.org/docs-wip/app/api-reference/functions/generate-metadata"
)
EXPECTED_URL = (
    "https://nextjs.org/docs/app/api-reference/functions/generate-metadata"
)


def load_case(path: Path) -> dict:
    return json.loads(path.read_text())


def rebuild_candidates(case: dict) -> list[dict]:
    """Turn stored lengths back into the fields ranking actually reads."""
    return [
        {
            "url": item["url"],
            "title": item["title"],
            "content": "x" * item["content_len"],
            "snippet": "y" * item["snippet_len"],
            "provider": item["provider"],
        }
        for item in case["candidates"]
    ]


def rank_order(case: dict, candidates: list[dict]) -> list[str]:
    query = case["query"]
    query_tokens = query_routing._query_brand_tokens(query)
    precision_tokens = query_routing._query_precision_tokens(query)
    exact_tokens = query_routing._query_exact_identifier_tokens(query)
    topic_tokens = query_routing._query_topic_specific_tokens(query)
    scored = []
    for item in candidates:
        key = ranking._resource_result_rank(
            query=query,
            mode=case["mode"],
            item=item,
            query_tokens=query_tokens,
            precision_tokens=precision_tokens,
            exact_identifier_tokens=exact_tokens,
            topic_specific_tokens=topic_tokens,
            include_domains=None,
            strict_official=case["strict_official"],
        )
        scored.append((key, item["url"]))
    return [url for _, url in sorted(scored, key=lambda pair: pair[0], reverse=True)]


class FrozenFixtureReplaysTest(unittest.TestCase):
    """The fixture itself must stay usable and discriminating."""

    def test_fixture_contains_the_working_copy_candidate(self) -> None:
        # If this fails the fixture no longer exercises the loop44 defect at all
        # and every other assertion here becomes vacuous.
        urls = [item["url"] for item in load_case(FIXTURE)["candidates"]]
        self.assertIn(WORKING_COPY_URL, urls)
        self.assertIn(EXPECTED_URL, urls)

    def test_rebuilding_from_lengths_reproduces_the_stored_lengths(self) -> None:
        case = load_case(FIXTURE)
        rebuilt = rebuild_candidates(case)
        for stored, item in zip(case["candidates"], rebuilt):
            self.assertEqual(len(item["content"]), stored["content_len"])
            self.assertEqual(len(item["snippet"]), stored["snippet_len"])

    def test_scores_are_deterministic_across_replays(self) -> None:
        # A ranking test is only a regression test if re-running gives the same
        # answer; otherwise it cannot fail on an injected defect.
        case = load_case(FIXTURE)
        first = rank_order(case, rebuild_candidates(case))
        second = rank_order(case, rebuild_candidates(case))
        self.assertEqual(first, second)


class WorkingCopyDemotionTest(unittest.TestCase):
    """loop44: an unpublished working copy must rank below the published page."""

    def test_published_page_outranks_the_working_copy(self) -> None:
        case = load_case(FIXTURE)
        order = rank_order(case, rebuild_candidates(case))
        self.assertIn(WORKING_COPY_URL, order)
        self.assertIn(EXPECTED_URL, order)
        self.assertLess(
            order.index(EXPECTED_URL),
            order.index(WORKING_COPY_URL),
            f"published page must outrank the working copy, got {order}",
        )

    def test_working_copy_is_demoted_when_strict_official(self) -> None:
        case = load_case(FIXTURE)
        strict = dict(case, strict_official=True)
        relaxed = dict(case, strict_official=False)
        candidates = rebuild_candidates(case)
        strict_order = rank_order(strict, candidates)
        relaxed_order = rank_order(relaxed, candidates)
        # The judgement is scoped to strict_official, same as its two siblings
        # (`non_locale_variant` / `non_preview_react_variant`). Outside strict mode
        # the working copy still wins on title length -- that is the documented
        # scope, not a bug.
        self.assertLess(
            strict_order.index(EXPECTED_URL), strict_order.index(WORKING_COPY_URL)
        )
        self.assertLess(
            relaxed_order.index(WORKING_COPY_URL), relaxed_order.index(EXPECTED_URL)
        )

    def test_demotion_is_what_moves_the_working_copy(self) -> None:
        # Mutation check in-test: disabling the working-copy judgement must
        # restore the old (wrong) ordering. If this passes either way, the
        # assertion above proves nothing about `non_working_copy`.
        case = load_case(FIXTURE)
        candidates = rebuild_candidates(case)
        query = case["query"]
        query_tokens = query_routing._query_brand_tokens(query)
        precision_tokens = query_routing._query_precision_tokens(query)
        exact_tokens = query_routing._query_exact_identifier_tokens(query)
        topic_tokens = query_routing._query_topic_specific_tokens(query)

        original = query_routing._looks_like_working_copy_path
        try:
            query_routing._looks_like_working_copy_path = lambda path: False
            scored = []
            for item in candidates:
                key = ranking._resource_result_rank(
                    query=query,
                    mode=case["mode"],
                    item=item,
                    query_tokens=query_tokens,
                    precision_tokens=precision_tokens,
                    exact_identifier_tokens=exact_tokens,
                    topic_specific_tokens=topic_tokens,
                    include_domains=None,
                    strict_official=True,
                )
                scored.append((key, item["url"]))
            mutated = [url for _, url in sorted(scored, key=lambda p: p[0], reverse=True)]
        finally:
            query_routing._looks_like_working_copy_path = original

        self.assertLess(
            mutated.index(WORKING_COPY_URL), mutated.index(EXPECTED_URL),
            "with the judgement disabled the working copy should lead again; "
            "if not, this fixture cannot detect a regression in it",
        )


if __name__ == "__main__":
    unittest.main()
