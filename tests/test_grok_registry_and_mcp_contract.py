"""grok registry 纯逻辑与 MCP 工具契约的回归测试。

这两块此前没有任何测试覆盖：grok_registry.py 是纯 env 解析逻辑，
mysearch/server.py 则决定了对外暴露的工具签名——签名变更会直接破坏调用方。
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mysearch import grok_registry  # noqa: E402


class GrokModelSanitizeTests(unittest.TestCase):
    def test_drops_invalid_ids_and_keeps_order(self) -> None:
        result = grok_registry._sanitize_grok_model_ids(
            ["grok-4.5", "bad id", "grok-4.3", "", "  grok-4.5  ", "grok/x:y_z.1"]
        )
        # "bad id" 含空格被丢弃；重复的 grok-4.5 去重；其余保序。
        self.assertEqual(result, ["grok-4.5", "grok-4.3", "grok/x:y_z.1"])

    def test_dedup_is_case_sensitive(self) -> None:
        self.assertEqual(
            grok_registry._sanitize_grok_model_ids(["Grok-4.5", "grok-4.5"]),
            ["Grok-4.5", "grok-4.5"],
        )

    def test_truncates_at_max_entries(self) -> None:
        raw = [f"model-{index}" for index in range(grok_registry._MAX_GROK_MODEL_ENTRIES + 50)]
        self.assertEqual(len(grok_registry._sanitize_grok_model_ids(raw)), grok_registry._MAX_GROK_MODEL_ENTRIES)

    def test_rejects_overlong_id(self) -> None:
        self.assertEqual(grok_registry._sanitize_grok_model_ids(["a" * 129]), [])


class GrokModelResolveTests(unittest.TestCase):
    def _resolve(self, **env):
        clean = {
            key: value
            for key, value in os.environ.items()
            if key not in {"MYSEARCH_GROK_MODELS", "MYSEARCH_GROK_EXTRA_MODELS"}
        }
        clean.update(env)
        with patch.dict(os.environ, clean, clear=True):
            return grok_registry._resolve_grok_models()

    def test_defaults_to_builtin_models(self) -> None:
        self.assertEqual(self._resolve(), grok_registry._BUILTIN_GROK_MODELS)

    def test_override_replaces_builtins_entirely(self) -> None:
        models = self._resolve(MYSEARCH_GROK_MODELS="grok-a, grok-b")
        self.assertEqual([m.id for m in models], ["grok-a", "grok-b"])
        self.assertTrue(all(m.source == "user" for m in models))

    def test_extras_append_after_builtins(self) -> None:
        models = self._resolve(MYSEARCH_GROK_EXTRA_MODELS="grok-4.5, grok-new")
        self.assertEqual([m.id for m in models], ["grok-4.20-0309", "grok-4.3", "grok-4.5", "grok-new"])
        # grok-4.5 已在内置清单中，不应重复出现。
        self.assertEqual([m.id for m in models].count("grok-4.5"), 1)

    def test_invalid_override_falls_back_to_builtins(self) -> None:
        # 过滤后为空 -> 回退内置，而不是返回空清单。
        self.assertEqual(
            self._resolve(MYSEARCH_GROK_MODELS="bad id,!!!"),
            grok_registry._BUILTIN_GROK_MODELS,
        )


class McpToolContractTests(unittest.TestCase):
    """锁定对外工具名与关键参数，防止无意破坏调用方契约。"""

    EXPECTED_TOOLS = {
        "search": {
            "required": {"query"},
            "params": {"query", "mode", "intent", "strategy", "provider", "sources", "max_results"},
        },
        "extract_url": {"required": {"url"}, "params": {"url", "formats", "only_main_content", "provider"}},
        "research": {"required": {"query"}, "params": {"query", "web_max_results", "scrape_top_n"}},
        "map_site": {"required": {"url"}, "params": {"url", "limit", "search"}},
        "crawl_site": {"required": {"url"}, "params": {"url", "limit", "max_depth"}},
        "mysearch_health": {"required": set(), "params": set()},
    }

    @classmethod
    def setUpClass(cls) -> None:
        from mysearch.config import MySearchConfig
        from mysearch.server import build_mcp

        cls._client, mcp = build_mcp(MySearchConfig.from_env())
        cls._tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}

    @classmethod
    def tearDownClass(cls) -> None:
        cls._client.close()

    def test_exposes_exactly_the_documented_tools(self) -> None:
        self.assertEqual(set(self._tools), set(self.EXPECTED_TOOLS))

    def test_tool_signatures_match_contract(self) -> None:
        for name, expected in self.EXPECTED_TOOLS.items():
            with self.subTest(tool=name):
                schema = self._tools[name].inputSchema
                self.assertTrue(
                    expected["required"].issubset(set(schema.get("required") or [])),
                    f"{name} 缺少必需参数: {expected['required'] - set(schema.get('required') or [])}",
                )
                present = set((schema.get("properties") or {}).keys())
                self.assertTrue(
                    expected["params"].issubset(present),
                    f"{name} 缺少参数: {expected['params'] - present}",
                )

    def test_sources_and_domain_filters_accept_string_or_list(self) -> None:
        # MCP 入口用 _ensure_list 兼容标量输入，避免模型把单元素数组写成字符串而校验失败。
        for tool_name, param in [
            ("search", "sources"),
            ("search", "include_domains"),
            ("search", "allowed_x_handles"),
            ("extract_url", "formats"),
        ]:
            with self.subTest(tool=tool_name, param=param):
                prop = self._tools[tool_name].inputSchema["properties"][param]
                rendered = str(prop)
                self.assertIn("array", rendered)
                self.assertIn("string", rendered)


if __name__ == "__main__":
    unittest.main()
