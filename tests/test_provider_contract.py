"""Provider 响应契约测试。

覆盖 A 阶段的收敛目标：单一构造器、字段分离、穷举校验。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mysearch.provider_contract import (
    ALL_LEGACY_PROVIDER_VALUES,
    ITEM_SOURCE_KINDS,
    PROVIDER_NAMES,
    ProviderContractError,
    ProviderResponse,
    build_response,
    classify_provider_value,
    collect_provider_values,
)


class CoverageTests(unittest.TestCase):
    """契约必须覆盖 clients.py 里真实存在的每个取值。"""

    def test_contract_covers_every_provider_value_in_clients(self) -> None:
        src = (REPO_ROOT / "mysearch" / "clients.py").read_text(encoding="utf-8")
        found = set(re.findall(r'"provider":\s*"([^"]+)"', src))
        found |= set(re.findall(r'\["provider"\]\s*=\s*"([^"]+)"', src))
        self.assertTrue(found, "expected provider construction sites in clients.py")
        unknown = sorted(v for v in found if classify_provider_value(v) is None)
        self.assertEqual(
            unknown,
            [],
            f"these values appear in clients.py but are not in the contract: {unknown}",
        )

    def test_every_legacy_value_classifies(self) -> None:
        for value in ALL_LEGACY_PROVIDER_VALUES:
            self.assertIsNotNone(classify_provider_value(value), value)


class ClassificationTests(unittest.TestCase):
    def test_real_providers_are_not_synthetic(self) -> None:
        for name in PROVIDER_NAMES:
            self.assertEqual(classify_provider_value(name), "provider")
            self.assertFalse(ProviderResponse.is_synthetic({"provider": name}))

    def test_hybrid_and_unavailable_are_synthetic(self) -> None:
        for value in ("hybrid", "web_unavailable", "social_unavailable"):
            self.assertTrue(ProviderResponse.is_synthetic({"provider": value}), value)

    def test_enrichment_sources_are_synthetic(self) -> None:
        for value in ITEM_SOURCE_KINDS | {"canonical-rescue"}:
            self.assertEqual(classify_provider_value(value), "enrichment", value)

    def test_unknown_value_returns_none(self) -> None:
        self.assertIsNone(classify_provider_value("not_a_provider"))
        self.assertIsNone(classify_provider_value(""))
        self.assertIsNone(classify_provider_value(None))


class BuildTests(unittest.TestCase):
    def test_real_provider_response_carries_its_name(self) -> None:
        payload = build_response(
            provider="tavily",
            query="q",
            results=[{"url": "https://example.com"}],
            citations=[{"url": "https://example.com"}],
        )
        self.assertEqual(payload["provider"], "tavily")
        self.assertEqual(payload["kind"], "provider")
        self.assertNotIn("source_kind", payload)
        self.assertTrue(ProviderResponse.is_real_provider(payload))

    def test_hybrid_response_must_not_name_a_real_provider(self) -> None:
        payload = build_response(kind="hybrid", query="q")
        self.assertEqual(payload["provider"], "")
        self.assertTrue(ProviderResponse.is_synthetic(payload))
        self.assertEqual(ProviderResponse.kind_of(payload), "hybrid")

    def test_enrichment_response_requires_source_kind(self) -> None:
        payload = build_response(
            kind="enrichment",
            source_kind="canonical_research_docs",
            query="q",
        )
        self.assertEqual(payload["source_kind"], "canonical_research_docs")
        with self.assertRaises(ProviderContractError):
            build_response(kind="enrichment", query="q")
        with self.assertRaises(ProviderContractError):
            build_response(kind="enrichment", source_kind="bogus", query="q")

    def test_real_provider_response_requires_a_name(self) -> None:
        with self.assertRaises(ProviderContractError):
            build_response(kind="provider", query="q")

    def test_hybrid_must_not_smuggle_a_provider_name(self) -> None:
        with self.assertRaises(ProviderContractError):
            build_response(kind="hybrid", provider="tavily", query="q")

    def test_unknown_provider_name_is_rejected(self) -> None:
        with self.assertRaises(ProviderContractError):
            build_response(provider="made_up", query="q")

    def test_non_list_results_are_rejected(self) -> None:
        with self.assertRaises(ProviderContractError):
            build_response(provider="tavily", query="q", results="not-a-list")

    def test_extra_fields_pass_through(self) -> None:
        payload = build_response(
            provider="exa",
            query="q",
            fallback={"from": "tavily", "to": "exa", "reason": "sparse"},
        )
        self.assertEqual(payload["fallback"]["to"], "exa")


class MigrationToleranceTests(unittest.TestCase):
    """尚未迁移的旧构造点没有 kind，必须按其 provider 值被正确识别。"""

    def test_legacy_payload_without_kind_infers_kind(self) -> None:
        legacy = {"provider": "hybrid", "results": [], "citations": []}
        ProviderResponse.validate(legacy)  # must not raise
        self.assertEqual(ProviderResponse.kind_of(legacy), "hybrid")
        self.assertTrue(ProviderResponse.is_synthetic(legacy))

    def test_legacy_payload_with_real_provider(self) -> None:
        legacy = {"provider": "tavily", "results": [], "citations": []}
        ProviderResponse.validate(legacy)
        self.assertTrue(ProviderResponse.is_real_provider(legacy))

    def test_legacy_payload_with_unknown_value_is_rejected(self) -> None:
        with self.assertRaises(ProviderContractError):
            ProviderResponse.validate({"provider": "mystery", "results": []})


class CollectTests(unittest.TestCase):
    def test_collects_unique_in_order(self) -> None:
        responses = [
            {"provider": "tavily"},
            {"provider": "firecrawl"},
            {"provider": "tavily"},
            {"provider": ""},
        ]
        self.assertEqual(collect_provider_values(responses), ["tavily", "firecrawl"])


if __name__ == "__main__":
    unittest.main()
