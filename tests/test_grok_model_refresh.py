"""grok_model_refresh 的排序 / 候选抽取 / primary 选取测试。

这些断言刻意**不硬编码具体模型 ID**（除了用于验证排序语义的构造数据）：
上游模型线会变，内置清单由 `scripts/refresh_grok_models.py` 刷新，
把测试绑死在某个版本号上会让每次刷新都误报失败。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mysearch.grok_model_refresh import (  # noqa: E402
    collect_candidates_from_model_list,
    collect_text_candidates,
    grok_model_sort_key,
    is_eligible_model,
    merge_candidates,
    pick_primary_and_fallback,
    rank_candidates,
)


class SortKeyTests(unittest.TestCase):
    def test_dotted_version_outranks_dated_version(self) -> None:
        """grok-4.6 必须排在 grok-4.20-0309 之前。

        数值上 4.6 < 4.20，纯数字比较会得到相反结论——这是本模块存在的理由。
        """
        self.assertGreater(
            grok_model_sort_key("grok-4.6"),
            grok_model_sort_key("grok-4.20-0309"),
        )

    def test_newer_dotted_version_outranks_older(self) -> None:
        self.assertGreater(
            grok_model_sort_key("grok-4.6"),
            grok_model_sort_key("grok-4.3"),
        )

    def test_newer_date_outranks_older_date_within_same_minor(self) -> None:
        self.assertGreater(
            grok_model_sort_key("grok-4.20-0309"),
            grok_model_sort_key("grok-4.20-0101"),
        )

    def test_unknown_naming_does_not_raise_and_sorts_last(self) -> None:
        unknown = grok_model_sort_key("grok-xyz")
        self.assertLess(unknown, grok_model_sort_key("grok-4.3"))

    def test_suffix_variants_share_the_same_date_rank(self) -> None:
        """`-reasoning` / `-non-reasoning` 后缀不应改变日期排序位。"""
        plain = grok_model_sort_key("grok-4.20-0309")
        reasoning = grok_model_sort_key("grok-4.20-0309-reasoning")
        self.assertEqual(plain[1:4], reasoning[1:4])


class EligibilityTests(unittest.TestCase):
    def test_multimodal_and_tool_models_are_excluded(self) -> None:
        for model_id in (
            "grok-imagine-image",
            "grok-imagine-video",
            "grok-voice-latest",
            "grok-stt",
            "grok-build-0.1",
            "grok-composer-2.5-fast",
        ):
            self.assertFalse(is_eligible_model(model_id), model_id)

    def test_text_models_are_eligible(self) -> None:
        for model_id in ("grok-4.6", "grok-4.3", "grok-4.20-0309-non-reasoning"):
            self.assertTrue(is_eligible_model(model_id), model_id)

    def test_non_grok_and_empty_are_excluded(self) -> None:
        self.assertFalse(is_eligible_model(""))
        self.assertFalse(is_eligible_model("gpt-5"))
        self.assertFalse(is_eligible_model(None))


class RankCandidatesTests(unittest.TestCase):
    def test_dedupes_and_orders_newest_first(self) -> None:
        ranked = rank_candidates(["grok-4.3", "grok-4.6", "grok-4.3"])
        self.assertEqual(ranked, ["grok-4.6", "grok-4.3"])

    def test_filters_ineligible(self) -> None:
        ranked = rank_candidates(["grok-4.6", "grok-stt", "grok-imagine-image"])
        self.assertEqual(ranked, ["grok-4.6"])


class CandidateCollectionTests(unittest.TestCase):
    def test_admin_endpoint_keeps_only_text_capability(self) -> None:
        payload = {
            "items": [
                {"publicId": "grok-4.6", "capability": "responses"},
                {"publicId": "grok-imagine-image", "capability": "image"},
                {"publicId": "grok-4.6", "capability": "responses"},
            ]
        }
        self.assertEqual(collect_text_candidates(payload), ["grok-4.6"])

    def test_admin_endpoint_tolerates_malformed_payload(self) -> None:
        for payload in ({}, {"items": None}, {"items": "nope"}, [], None):
            self.assertEqual(collect_text_candidates(payload), [], repr(payload))

    def test_openai_model_list_accepts_both_shapes(self) -> None:
        self.assertEqual(
            collect_candidates_from_model_list({"data": [{"id": "grok-4.6"}]}),
            ["grok-4.6"],
        )
        self.assertEqual(
            collect_candidates_from_model_list({"models": ["grok-4.6"]}),
            ["grok-4.6"],
        )

    def test_union_recovers_items_missing_from_either_endpoint(self) -> None:
        """两个端点各有漏报，取并集才能拿到完整候选。

        实测：admin 端点漏掉可用的 `-non-reasoning`，`/v1/models` 多出实测 404 的
        `grok-4.20-0309`。并集保留两者，由真实探测决定去留。
        """
        admin_side = collect_text_candidates(
            {"items": [{"publicId": "grok-4.6", "capability": "responses"}]}
        )
        list_side = collect_candidates_from_model_list(
            {"data": [{"id": "grok-4.20-0309-non-reasoning"}]}
        )
        merged = merge_candidates(admin_side, list_side)
        self.assertIn("grok-4.6", merged)
        self.assertIn("grok-4.20-0309-non-reasoning", merged)

    def test_merge_handles_empty_inputs(self) -> None:
        self.assertEqual(merge_candidates([], None, []), [])


class PickModelsTests(unittest.TestCase):
    def test_picks_newest_as_primary_and_next_as_fallback(self) -> None:
        primary, fallback = pick_primary_and_fallback(["grok-4.3", "grok-4.6"])
        self.assertEqual(primary, "grok-4.6")
        self.assertEqual(fallback, "grok-4.3")

    def test_single_model_leaves_fallback_empty(self) -> None:
        """只有一个可用时 fallback 留空——指向同一模型的 fallback 是无意义的重试。"""
        primary, fallback = pick_primary_and_fallback(["grok-4.6"])
        self.assertEqual(primary, "grok-4.6")
        self.assertEqual(fallback, "")

    def test_no_available_model_returns_blanks(self) -> None:
        """全不可用时返回空——调用方应保留现有配置，而不是写入空模型。"""
        self.assertEqual(pick_primary_and_fallback([]), ("", ""))
        self.assertEqual(pick_primary_and_fallback(["grok-stt"]), ("", ""))

    def test_only_probed_models_are_considered(self) -> None:
        """传入的应是探测通过的清单；未通过的不会出现在这里。"""
        primary, _ = pick_primary_and_fallback(["grok-4.3", "grok-4.20-0309-reasoning"])
        self.assertEqual(primary, "grok-4.3")


class BuiltinRegistryTests(unittest.TestCase):
    """内置清单必须与"探测式刷新"的排序结论一致。"""

    def test_builtin_list_is_ordered_newest_first(self) -> None:
        from mysearch.grok_registry import _BUILTIN_GROK_MODELS

        ids = [spec.id for spec in _BUILTIN_GROK_MODELS]
        self.assertEqual(ids, rank_candidates(ids), "内置清单顺序应与排序结论一致")

    def test_builtin_list_has_no_duplicates(self) -> None:
        from mysearch.grok_registry import _BUILTIN_GROK_MODELS

        ids = [spec.id for spec in _BUILTIN_GROK_MODELS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_builtin_entries_are_eligible_text_models(self) -> None:
        from mysearch.grok_registry import _BUILTIN_GROK_MODELS

        for spec in _BUILTIN_GROK_MODELS:
            self.assertTrue(is_eligible_model(spec.id), spec.id)

    def test_no_bare_dated_model_without_suffix(self) -> None:
        """`grok-4.20-0309`（无后缀）实测 404，且曾占据首位。

        它会让零配置部署的默认 primary 指向不存在的模型——这个断言防止它被加回。
        带后缀的 `-reasoning` / `-non-reasoning` 是不同模型，不受此限。
        """
        from mysearch.grok_registry import _BUILTIN_GROK_MODELS

        for spec in _BUILTIN_GROK_MODELS:
            self.assertNotEqual(spec.id, "grok-4.20-0309")


class BaseUrlNormalizationTests(unittest.TestCase):
    """`SOCIAL_GATEWAY_UPSTREAM_BASE_URL` 带 `/v1`，但脚本拼接的三类路径各自已含前缀。

    保留 `/v1` 再拼会得到 `/v1/v1/responses`、`/v1/api/admin/v1/...` 这类双前缀地址，
    一律 404 —— 这是实测踩过的坑（候选能拿到但探测全失败）。
    """

    def test_strips_v1_suffix(self) -> None:
        from scripts.refresh_grok_models import _root_base_url

        self.assertEqual(_root_base_url("http://host:8000/v1"), "http://host:8000")

    def test_keeps_url_without_suffix(self) -> None:
        from scripts.refresh_grok_models import _root_base_url

        self.assertEqual(_root_base_url("http://host:8000"), "http://host:8000")

    def test_trailing_slash_is_normalized(self) -> None:
        from scripts.refresh_grok_models import _root_base_url

        self.assertEqual(_root_base_url("http://host:8000/v1/"), "http://host:8000")

    def test_strips_full_responses_path(self) -> None:
        from scripts.refresh_grok_models import _root_base_url

        self.assertEqual(_root_base_url("http://host:8000/v1/responses"), "http://host:8000")

    def test_composed_probe_url_has_single_prefix(self) -> None:
        """归一后的拼装结果必须只有一个 `/v1`。"""
        from scripts.refresh_grok_models import _root_base_url

        root = _root_base_url("http://host:8000/v1")
        self.assertEqual(f"{root}/v1/responses", "http://host:8000/v1/responses")
        self.assertEqual(f"{root}/api/admin/v1/models", "http://host:8000/api/admin/v1/models")


if __name__ == "__main__":
    unittest.main()
