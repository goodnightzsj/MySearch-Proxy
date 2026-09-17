"""Proxy 侧 social 模型定时刷新的契约测试。

覆盖三件事：
1. `_root_of_upstream` 的归一（双 `/v1` 前缀是实测踩过的坑）。
2. TTL 判定（从未刷新过要触发首次刷新；关闭时不触发）。
3. 刷新失败时**不得清空现有配置**——保留现值比写入空值安全。

这里不 mock `probe_social_model` 的真实网络行为，只覆盖纯逻辑与失败分支；
真实探测由 `scripts/refresh_grok_models.py` 的端到端运行验证。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PROXY_ROOT = REPO_ROOT / "proxy"


def _load_server_module(module_name: str):
    """加载 proxy/server.py。

    与 tests/test_proxy_usage_stats_batch.py 同样的手法：server.py 与
    proxy/database.py 是同级模块（`import database as db`），需要把 proxy/ 放进
    sys.path，并给一个临时 DB，避免污染工作区里的 proxy/data/proxy.db。
    """
    spec = importlib.util.spec_from_file_location(module_name, PROXY_ROOT / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class RootOfUpstreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory()
        os.environ["MYSEARCH_PROXY_DB_PATH"] = str(Path(cls._tmpdir.name) / "proxy.db")
        if str(PROXY_ROOT) not in sys.path:
            sys.path.insert(0, str(PROXY_ROOT))
        cls.server = _load_server_module("test_social_model_refresh_server")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmpdir.cleanup()

    def test_strips_v1_suffix(self) -> None:
        self.assertEqual(
            self.server._root_of_upstream("http://host:8000/v1"),
            "http://host:8000",
        )

    def test_keeps_bare_host(self) -> None:
        self.assertEqual(
            self.server._root_of_upstream("http://host:8000"),
            "http://host:8000",
        )

    def test_trailing_slash_normalized(self) -> None:
        self.assertEqual(
            self.server._root_of_upstream("http://host:8000/v1/"),
            "http://host:8000",
        )

    def test_composed_paths_have_single_prefix(self) -> None:
        root = self.server._root_of_upstream("http://host:8000/v1")
        self.assertEqual(f"{root}/v1/models", "http://host:8000/v1/models")
        self.assertEqual(f"{root}/api/admin/v1/models", "http://host:8000/api/admin/v1/models")

    def test_empty_input_is_safe(self) -> None:
        self.assertEqual(self.server._root_of_upstream(""), "")
        self.assertEqual(self.server._root_of_upstream(None), "")


class StalenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory()
        os.environ["MYSEARCH_PROXY_DB_PATH"] = str(Path(cls._tmpdir.name) / "proxy.db")
        if str(PROXY_ROOT) not in sys.path:
            sys.path.insert(0, str(PROXY_ROOT))
        cls.server = _load_server_module("test_social_model_staleness_server")
        cls.server.db.init_db()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmpdir.cleanup()

    def _set_refreshed_at(self, value):
        self.server.db.set_setting("social_model_refreshed_at", value)

    def test_never_refreshed_is_stale(self) -> None:
        """首次启动应触发一次刷新。"""
        self._set_refreshed_at("")
        self.assertTrue(self.server._is_social_model_refresh_stale())

    def test_recent_refresh_is_not_stale(self) -> None:
        self._set_refreshed_at(datetime.now(timezone.utc).isoformat())
        self.assertFalse(self.server._is_social_model_refresh_stale())

    def test_old_refresh_is_stale(self) -> None:
        old = datetime.now(timezone.utc) - timedelta(seconds=self.server.SOCIAL_MODEL_REFRESH_TTL_SECONDS + 60)
        self._set_refreshed_at(old.isoformat())
        self.assertTrue(self.server._is_social_model_refresh_stale())

    def test_unparseable_timestamp_is_treated_as_stale(self) -> None:
        """时间戳损坏时宁可刷新一次，也不要永久不刷新。"""
        self._set_refreshed_at("not-a-timestamp")
        self.assertTrue(self.server._is_social_model_refresh_stale())

    def test_ttl_zero_disables_refresh(self) -> None:
        with patch.object(self.server, "SOCIAL_MODEL_REFRESH_TTL_SECONDS", 0):
            self._set_refreshed_at("")
            self.assertFalse(self.server._is_social_model_refresh_stale())


class RefreshFailureTests(unittest.TestCase):
    """刷新失败必须保留现有配置，绝不清空。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory()
        os.environ["MYSEARCH_PROXY_DB_PATH"] = str(Path(cls._tmpdir.name) / "proxy.db")
        if str(PROXY_ROOT) not in sys.path:
            sys.path.insert(0, str(PROXY_ROOT))
        cls.server = _load_server_module("test_social_model_failure_server")
        cls.server.db.init_db()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmpdir.cleanup()

    def setUp(self) -> None:
        self.server.db.set_setting("social_model", "keep-me")
        self.server.db.set_setting("social_fallback_model", "keep-fallback")

    async def _run_refresh(self, *, config, candidates, probes):
        server = self.server
        # 必须同时 patch 两个网络函数：只 patch probe_social_model 的话，
        # fetch_social_upstream_json 会真的去请求 config 里的假主机直到超时
        # （实测让本测试从 0.2s 变成 18.5s）。
        with patch.object(server, "get_runtime_social_config", return_value=config), \
             patch.object(server, "collect_text_candidates", return_value=candidates), \
             patch.object(server, "collect_candidates_from_model_list", return_value=[]), \
             patch.object(server, "merge_candidates", return_value=candidates), \
             patch.object(server, "probe_social_model", side_effect=probes), \
             patch.object(server, "fetch_social_upstream_json", return_value=None), \
             patch.object(server, "get_social_admin_v3_access_token",
                          side_effect=RuntimeError("admin disabled in test")), \
             patch.object(server, "pick_primary_and_fallback",
                          side_effect=lambda ids: (ids[0], ids[1] if len(ids) > 1 else "") if ids else ("", "")):
            return await server.probe_and_refresh_social_models()

    def test_unconfigured_upstream_keeps_config(self) -> None:
        import asyncio

        result = asyncio.run(self._run_refresh(config={"upstream_base_url": "", "upstream_api_key": ""},
                                              candidates=[], probes=[]))
        self.assertFalse(result["ok"])
        self.assertEqual(self.server.get_setting_text("social_model", ""), "keep-me")

    def test_no_candidates_keeps_config(self) -> None:
        import asyncio

        result = asyncio.run(self._run_refresh(
            config={"upstream_base_url": "http://h:8000/v1", "upstream_api_key": "k"},
            candidates=[], probes=[]))
        self.assertFalse(result["ok"])
        self.assertEqual(self.server.get_setting_text("social_model", ""), "keep-me")

    def test_all_probes_failing_keeps_config(self) -> None:
        """全不可用时不得把 social_model 写成空字符串。"""
        import asyncio

        result = asyncio.run(self._run_refresh(
            config={"upstream_base_url": "http://h:8000/v1", "upstream_api_key": "k"},
            candidates=["grok-a", "grok-b"],
            probes=[False, False]))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "no model passed probing")
        self.assertEqual(self.server.get_setting_text("social_model", ""), "keep-me")
        self.assertEqual(self.server.get_setting_text("social_fallback_model", ""), "keep-fallback")

    def test_successful_probe_writes_both_models(self) -> None:
        import asyncio

        result = asyncio.run(self._run_refresh(
            config={"upstream_base_url": "http://h:8000/v1", "upstream_api_key": "k"},
            candidates=["grok-new", "grok-old"],
            probes=[True, True]))
        self.assertTrue(result["ok"])
        self.assertEqual(result["primary"], "grok-new")
        self.assertEqual(self.server.get_setting_text("social_model", ""), "grok-new")
        self.assertEqual(self.server.get_setting_text("social_fallback_model", ""), "grok-old")
        # 刷新时间戳必须写入，否则下一轮又会被判为 stale。
        self.assertTrue(self.server.get_setting_text("social_model_refreshed_at", ""))
        self.assertFalse(self.server._is_social_model_refresh_stale())

    def test_single_available_model_leaves_fallback_empty_but_not_stale_config(self) -> None:
        import asyncio

        result = asyncio.run(self._run_refresh(
            config={"upstream_base_url": "http://h:8000/v1", "upstream_api_key": "k"},
            candidates=["grok-only"],
            probes=[True]))
        self.assertTrue(result["ok"])
        self.assertEqual(result["primary"], "grok-only")
        self.assertEqual(result["fallback"], "")


if __name__ == "__main__":
    unittest.main()
