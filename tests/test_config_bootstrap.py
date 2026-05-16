from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class ConfigBootstrapTests(unittest.TestCase):
    def _preserve_env(self, *keys: str) -> dict[str, str | None]:
        snapshot: dict[str, str | None] = {}
        for key in keys:
            snapshot[key] = os.environ.get(key)
            os.environ.pop(key, None)
        return snapshot

    def _restore_env(self, snapshot: dict[str, str | None]) -> None:
        for key, value in snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_tavily_gateway_mode_prefers_gateway_token_and_disables_local_key_file(self) -> None:
        snapshot = self._preserve_env(
            "MYSEARCH_PROXY_BASE_URL",
            "MYSEARCH_PROXY_API_KEY",
            "MYSEARCH_TAVILY_MODE",
            "MYSEARCH_TAVILY_GATEWAY_BASE_URL",
            "MYSEARCH_TAVILY_GATEWAY_SEARCH_PATH",
            "MYSEARCH_TAVILY_GATEWAY_EXTRACT_PATH",
            "MYSEARCH_TAVILY_GATEWAY_TOKEN",
            "MYSEARCH_TAVILY_API_KEY",
            "MYSEARCH_TAVILY_KEYS_FILE",
        )
        try:
            os.environ["MYSEARCH_TAVILY_MODE"] = "gateway"
            os.environ["MYSEARCH_TAVILY_GATEWAY_BASE_URL"] = "http://127.0.0.1:8787/api/tavily"
            os.environ["MYSEARCH_TAVILY_GATEWAY_TOKEN"] = "th-demo-token"
            os.environ["MYSEARCH_TAVILY_API_KEY"] = "tvly-official-key"
            os.environ["MYSEARCH_TAVILY_KEYS_FILE"] = "accounts.txt"

            module = _load_module(
                "test_mysearch_config_tavily_gateway_mode",
                REPO_ROOT / "mysearch" / "config.py",
            )
            config = module.MySearchConfig.from_env()

            self.assertEqual(config.tavily.provider_mode, "gateway")
            self.assertEqual(config.tavily.base_url, "http://127.0.0.1:8787/api/tavily")
            self.assertEqual(config.tavily.path("search"), "/search")
            self.assertEqual(config.tavily.path("extract"), "/extract")
            self.assertEqual(config.tavily.auth_mode, "bearer")
            self.assertEqual(config.tavily.api_keys, ["th-demo-token"])
            self.assertIsNone(config.tavily.keys_file)
        finally:
            self._restore_env(snapshot)

    def test_tavily_official_mode_ignores_proxy_token_and_keeps_local_pool(self) -> None:
        snapshot = self._preserve_env(
            "MYSEARCH_PROXY_BASE_URL",
            "MYSEARCH_PROXY_API_KEY",
            "MYSEARCH_TAVILY_MODE",
            "MYSEARCH_TAVILY_BASE_URL",
            "MYSEARCH_TAVILY_SEARCH_PATH",
            "MYSEARCH_TAVILY_EXTRACT_PATH",
            "MYSEARCH_TAVILY_API_KEY",
            "MYSEARCH_TAVILY_KEYS_FILE",
        )
        try:
            os.environ["MYSEARCH_PROXY_BASE_URL"] = "https://proxy.example.com"
            os.environ["MYSEARCH_PROXY_API_KEY"] = "mysp-token"
            os.environ["MYSEARCH_TAVILY_MODE"] = "official"
            os.environ["MYSEARCH_TAVILY_API_KEY"] = "tvly-direct-key"
            os.environ["MYSEARCH_TAVILY_KEYS_FILE"] = "custom-accounts.txt"

            module = _load_module(
                "test_mysearch_config_tavily_official_mode",
                REPO_ROOT / "mysearch" / "config.py",
            )
            config = module.MySearchConfig.from_env()

            self.assertEqual(config.tavily.provider_mode, "official")
            self.assertEqual(config.tavily.base_url, "https://api.tavily.com")
            self.assertEqual(config.tavily.path("search"), "/search")
            self.assertEqual(config.tavily.path("extract"), "/extract")
            self.assertEqual(config.tavily.auth_mode, "body")
            self.assertEqual(config.tavily.api_keys, ["tvly-direct-key"])
            self.assertEqual(config.tavily.keys_file, REPO_ROOT / "custom-accounts.txt")
        finally:
            self._restore_env(snapshot)

    def test_codex_config_env_wins_over_dotenv_and_dotenv_fills_missing_values(self) -> None:
        snapshot = self._preserve_env(
            "CODEX_HOME",
            "MYSEARCH_PROXY_BASE_URL",
            "MYSEARCH_PROXY_API_KEY",
            "MYSEARCH_TIMEOUT_SECONDS",
        )
        try:
            with TemporaryDirectory() as tmpdir:
                temp_root = Path(tmpdir)
                codex_home = temp_root / ".codex"
                codex_home.mkdir(parents=True)
                (codex_home / "config.toml").write_text(
                    """
[mcp_servers.mysearch]
command = "python3"

[mcp_servers.mysearch.env]
MYSEARCH_PROXY_BASE_URL = "https://config.example.com"
MYSEARCH_PROXY_API_KEY = "config-token"
""".strip(),
                    encoding="utf-8",
                )

                module_dir = temp_root / "mysearch"
                module_dir.mkdir(parents=True)
                (module_dir / ".env").write_text(
                    "\n".join(
                        [
                            "MYSEARCH_PROXY_BASE_URL=https://dotenv.example.com",
                            "MYSEARCH_PROXY_API_KEY=dotenv-token",
                            "MYSEARCH_TIMEOUT_SECONDS=91",
                        ]
                    ),
                    encoding="utf-8",
                )

                os.environ["CODEX_HOME"] = str(codex_home)
                module = _load_module(
                    "test_mysearch_config_bootstrap",
                    REPO_ROOT / "mysearch" / "config.py",
                )
                module.MODULE_DIR = module_dir
                module.ROOT_DIR = temp_root
                module._bootstrap_runtime_env()

                self.assertEqual(
                    os.environ.get("MYSEARCH_PROXY_BASE_URL"),
                    "https://config.example.com",
                )
                self.assertEqual(
                    os.environ.get("MYSEARCH_PROXY_API_KEY"),
                    "config-token",
                )
                self.assertEqual(os.environ.get("MYSEARCH_TIMEOUT_SECONDS"), "91")
        finally:
            self._restore_env(snapshot)

    def test_openclaw_wrapper_reads_skill_env_from_openclaw_json(self) -> None:
        snapshot = self._preserve_env(
            "OPENCLAW_CONFIG_PATH",
            "MYSEARCH_PROXY_BASE_URL",
            "MYSEARCH_PROXY_API_KEY",
        )
        try:
            with TemporaryDirectory() as tmpdir:
                temp_root = Path(tmpdir)
                state_dir = temp_root / ".openclaw"
                skill_dir = state_dir / "skills" / "mysearch"
                skill_dir.mkdir(parents=True)
                (state_dir / "openclaw.json").write_text(
                    json.dumps(
                        {
                            "skills": {
                                "entries": {
                                    "mysearch": {
                                        "env": {
                                            "MYSEARCH_PROXY_BASE_URL": "https://openclaw.example.com",
                                            "MYSEARCH_PROXY_API_KEY": "openclaw-token",
                                        }
                                    }
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )

                module = _load_module(
                    "test_mysearch_openclaw_wrapper",
                    REPO_ROOT / "openclaw" / "scripts" / "mysearch_openclaw.py",
                )
                module._load_openclaw_skill_env(skill_dir)

                self.assertEqual(
                    os.environ.get("MYSEARCH_PROXY_BASE_URL"),
                    "https://openclaw.example.com",
                )
                self.assertEqual(
                    os.environ.get("MYSEARCH_PROXY_API_KEY"),
                    "openclaw-token",
                )
        finally:
            self._restore_env(snapshot)

    def test_config_parser_falls_back_without_tomllib(self) -> None:
        module = _load_module(
            "test_mysearch_config_parser_fallback",
            REPO_ROOT / "mysearch" / "config.py",
        )
        module.tomllib = None
        env = module._parse_codex_mysearch_env(
            """
[mcp_servers.mysearch.env]
MYSEARCH_PROXY_BASE_URL = "https://fallback.example.com"
MYSEARCH_PROXY_API_KEY = "fallback-token"
""".strip()
        )

        self.assertEqual(
            env,
            {
                "MYSEARCH_PROXY_BASE_URL": "https://fallback.example.com",
                "MYSEARCH_PROXY_API_KEY": "fallback-token",
            },
        )


class GrokModelRegistryTests(unittest.TestCase):
    def _preserve_env(self, *keys: str) -> dict[str, str | None]:
        snapshot: dict[str, str | None] = {}
        for key in keys:
            snapshot[key] = os.environ.get(key)
            os.environ.pop(key, None)
        return snapshot

    def _restore_env(self, snapshot: dict[str, str | None]) -> None:
        for key, value in snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_builtin_basic_tier_models_loaded_by_default(self) -> None:
        snapshot = self._preserve_env("MYSEARCH_GROK_MODELS", "MYSEARCH_GROK_EXTRA_MODELS")
        try:
            module = _load_module(
                "test_mysearch_grok_models_default",
                REPO_ROOT / "mysearch" / "config.py",
            )
            models = module._resolve_grok_models()
            ids = [m.id for m in models]
            self.assertEqual(
                ids,
                ["grok-4.20-fast", "grok-4.20-0309-non-reasoning", "grok-4.3-beta"],
            )
            for m in models:
                self.assertEqual(m.tier, "basic")
                self.assertEqual(m.source, "builtin")
        finally:
            self._restore_env(snapshot)

    def test_extra_models_appended_and_deduped(self) -> None:
        snapshot = self._preserve_env("MYSEARCH_GROK_MODELS", "MYSEARCH_GROK_EXTRA_MODELS")
        try:
            os.environ["MYSEARCH_GROK_EXTRA_MODELS"] = (
                "grok-4.20-auto, grok-4.20-expert, grok-4.20-fast"
            )
            module = _load_module(
                "test_mysearch_grok_models_extras",
                REPO_ROOT / "mysearch" / "config.py",
            )
            models = module._resolve_grok_models()
            ids = [m.id for m in models]
            self.assertEqual(
                ids,
                [
                    "grok-4.20-fast",
                    "grok-4.20-0309-non-reasoning",
                    "grok-4.3-beta",
                    "grok-4.20-auto",
                    "grok-4.20-expert",
                ],
            )
            sources = {m.id: m.source for m in models}
            tiers = {m.id: m.tier for m in models}
            self.assertEqual(sources["grok-4.20-fast"], "builtin")
            self.assertEqual(sources["grok-4.20-auto"], "user")
            self.assertEqual(sources["grok-4.20-expert"], "user")
            # 追加项的 tier 必须显式为 `custom`；防止未来重构把默认值改回 `basic`。
            self.assertEqual(tiers["grok-4.20-auto"], "custom")
            self.assertEqual(tiers["grok-4.20-expert"], "custom")
        finally:
            self._restore_env(snapshot)

    def test_blank_or_punctuation_only_grok_models_falls_back_to_builtin(self) -> None:
        """空字符串 / 纯逗号 / 全空白条目都应回退到内置清单。"""
        snapshot = self._preserve_env("MYSEARCH_GROK_MODELS", "MYSEARCH_GROK_EXTRA_MODELS")
        for raw in ("", ",", "  ,  ,"):
            try:
                os.environ["MYSEARCH_GROK_MODELS"] = raw
                module = _load_module(
                    f"test_mysearch_grok_models_blank_{abs(hash(raw))}",
                    REPO_ROOT / "mysearch" / "config.py",
                )
                models = module._resolve_grok_models()
                ids = [m.id for m in models]
                self.assertEqual(
                    ids,
                    ["grok-4.20-fast", "grok-4.20-0309-non-reasoning", "grok-4.3-beta"],
                    f"blank env {raw!r} should fall back to builtin",
                )
            finally:
                self._restore_env(snapshot)

    def test_invalid_characters_in_grok_model_id_are_filtered(self) -> None:
        """含换行/引号/空格中间字符的 ID 应被静默跳过，防止污染日志与 health body。"""
        snapshot = self._preserve_env("MYSEARCH_GROK_MODELS", "MYSEARCH_GROK_EXTRA_MODELS")
        try:
            os.environ["MYSEARCH_GROK_EXTRA_MODELS"] = (
                'grok-valid-1,grok bad space,"grok-quoted",grok-valid-2,'
                + "grok\nnewline,grok-valid-3"
            )
            module = _load_module(
                "test_mysearch_grok_models_sanitize",
                REPO_ROOT / "mysearch" / "config.py",
            )
            models = module._resolve_grok_models()
            user_ids = [m.id for m in models if m.source == "user"]
            self.assertEqual(
                user_ids,
                ["grok-valid-1", "grok-valid-2", "grok-valid-3"],
            )
        finally:
            self._restore_env(snapshot)

    def test_oversized_grok_models_list_is_truncated(self) -> None:
        """超过 _MAX_GROK_MODEL_ENTRIES 的多余条目应被截断，防 OOM。"""
        snapshot = self._preserve_env("MYSEARCH_GROK_MODELS", "MYSEARCH_GROK_EXTRA_MODELS")
        try:
            os.environ["MYSEARCH_GROK_MODELS"] = ",".join(
                f"grok-bulk-{i}" for i in range(1000)
            )
            module = _load_module(
                "test_mysearch_grok_models_truncate",
                REPO_ROOT / "mysearch" / "config.py",
            )
            models = module._resolve_grok_models()
            self.assertLessEqual(len(models), module._MAX_GROK_MODEL_ENTRIES)
        finally:
            self._restore_env(snapshot)

    def test_grok_model_id_length_boundary(self) -> None:
        """模型 ID 长度边界：128 字节 OK，129 字节被拒。"""
        snapshot = self._preserve_env("MYSEARCH_GROK_MODELS", "MYSEARCH_GROK_EXTRA_MODELS")
        try:
            # 用 grok- 前缀 + 数字填充到精确字节长度
            id_128 = "grok-" + ("a" * 123)
            id_129 = "grok-" + ("a" * 124)
            self.assertEqual(len(id_128), 128)
            self.assertEqual(len(id_129), 129)
            os.environ["MYSEARCH_GROK_EXTRA_MODELS"] = f"{id_128},{id_129}"
            module = _load_module(
                "test_mysearch_grok_models_length_boundary",
                REPO_ROOT / "mysearch" / "config.py",
            )
            user_ids = [m.id for m in module._resolve_grok_models() if m.source == "user"]
            self.assertIn(id_128, user_ids)
            self.assertNotIn(id_129, user_ids)
        finally:
            self._restore_env(snapshot)

    def test_single_model_override_keeps_fallback_aligned(self) -> None:
        """`MYSEARCH_GROK_MODELS=single-only` 时 social_gateway helpers 不应崩溃，
        fallback 退化为内置 basic 层第 2 项作为兜底（保证 has_social_fallback 仍能成立）。"""
        snapshot = self._preserve_env("MYSEARCH_GROK_MODELS", "MYSEARCH_GROK_EXTRA_MODELS")
        # 同时确保 sys.path 含 repo root，让 mysearch 作为 package 可导入
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        # 清掉缓存以让 module 重新求值新 env
        for mod_name in ("mysearch.social_gateway", "mysearch.config", "mysearch"):
            sys.modules.pop(mod_name, None)
        try:
            os.environ["MYSEARCH_GROK_MODELS"] = "grok-only-one"
            import importlib
            social_gateway = importlib.import_module("mysearch.social_gateway")
            primary = social_gateway._grok_default_primary()
            fallback = social_gateway._grok_default_fallback(primary)
            self.assertEqual(primary, "grok-only-one")
            self.assertEqual(fallback, "grok-4.20-0309-non-reasoning")
        finally:
            self._restore_env(snapshot)
            # 重置 module cache，避免污染后续测试
            for mod_name in ("mysearch.social_gateway", "mysearch.config", "mysearch"):
                sys.modules.pop(mod_name, None)

    def test_full_override_replaces_builtins(self) -> None:
        snapshot = self._preserve_env("MYSEARCH_GROK_MODELS", "MYSEARCH_GROK_EXTRA_MODELS")
        try:
            os.environ["MYSEARCH_GROK_MODELS"] = (
                "grok-private-tuned, grok-4.20-heavy , grok-private-tuned"
            )
            os.environ["MYSEARCH_GROK_EXTRA_MODELS"] = "grok-4.20-auto"
            module = _load_module(
                "test_mysearch_grok_models_override",
                REPO_ROOT / "mysearch" / "config.py",
            )
            models = module._resolve_grok_models()
            ids = [m.id for m in models]
            self.assertEqual(ids, ["grok-private-tuned", "grok-4.20-heavy"])
            for m in models:
                self.assertEqual(m.tier, "custom")
                self.assertEqual(m.source, "user")
        finally:
            self._restore_env(snapshot)


if __name__ == "__main__":
    unittest.main()
