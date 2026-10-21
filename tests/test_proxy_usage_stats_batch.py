"""Proxy 用量统计聚合的回归测试。

重点是 get_token_usage_stats 的批量结果必须与逐个调用 get_usage_stats 完全等价，
以及 api_keys 热路径索引在 init_db 后确实存在。
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


def _load_database_module(module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, PROXY_ROOT / "database.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class TokenUsageStatsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "proxy.db"
        self._env = patch.dict(os.environ, {"MYSEARCH_PROXY_DB_PATH": str(self.db_path)})
        self._env.start()
        self.db = _load_database_module("test_database_usage_stats_module")
        self.db.init_db()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmpdir.cleanup()

    def _seed(self):
        conn = self.db.get_conn()
        now = datetime.now(timezone.utc)
        conn.execute("INSERT INTO tokens (service, token, name) VALUES ('tavily','tok-a','A')")
        conn.execute("INSERT INTO tokens (service, token, name) VALUES ('tavily','tok-b','B')")
        conn.execute("INSERT INTO tokens (service, token, name) VALUES ('mysearch','tok-c','C')")
        conn.commit()
        ids = [row["id"] for row in conn.execute("SELECT id FROM tokens ORDER BY id")]

        def log(token_id, success, when, service="tavily"):
            conn.execute(
                "INSERT INTO usage_logs (service, token_id, endpoint, success, latency_ms, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (service, token_id, "/x", success, 10, when.strftime("%Y-%m-%d %H:%M:%S")),
            )

        for _ in range(5):
            log(ids[0], 1, now)
        for _ in range(3):
            log(ids[0], 0, now)
        log(ids[0], 1, now - timedelta(days=40))  # 计入月统计，不计入今日
        for _ in range(2):
            log(ids[1], 1, now)
        log(ids[2], 1, now, service="mysearch")
        conn.commit()
        return ids

    def test_batch_matches_per_token_calls(self) -> None:
        ids = self._seed()
        for token_id in ids:
            for service in ("tavily", "mysearch", "firecrawl"):
                with self.subTest(token_id=token_id, service=service):
                    batch = self.db.get_token_usage_stats([token_id], service=service)[token_id]
                    single = self.db.get_usage_stats(token_id=token_id, service=service)
                    self.assertEqual(batch, single)

    def test_batch_returns_zeroes_for_tokens_without_usage(self) -> None:
        self._seed()
        stats = self.db.get_token_usage_stats([999999], service="tavily")
        self.assertEqual(
            stats[999999],
            {
                "today_success": 0,
                "today_failed": 0,
                "month_success": 0,
                "hour_count": 0,
                "today_count": 0,
                "month_count": 0,
            },
        )

    def test_batch_handles_empty_input(self) -> None:
        self.assertEqual(self.db.get_token_usage_stats([], service="tavily"), {})
        self.assertEqual(self.db.get_token_usage_stats(None, service="tavily"), {})

    def test_batch_mysearch_skips_service_filter(self) -> None:
        ids = self._seed()
        # mysearch 不做 service 过滤，因此 tavily 记录的 token 也会被计入。
        stats = self.db.get_token_usage_stats(ids, service="mysearch")
        self.assertEqual(stats[ids[0]]["today_count"], 8)


class ApiKeysHotPathIndexTests(unittest.TestCase):
    def test_schedule_index_survives_init_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "proxy.db"
            with patch.dict(os.environ, {"MYSEARCH_PROXY_DB_PATH": str(db_path)}):
                module = _load_database_module("test_database_schedule_index_module")
                module.init_db()
                rows = module.get_conn().execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='api_keys'"
                ).fetchall()
        names = {row["name"] for row in rows}
        self.assertIn("idx_api_keys_schedule", names)


class UsageLogFailureReasonTests(unittest.TestCase):
    """失败诊断列：`success=0` 的调用必须能回答"为什么失败"。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "proxy.db"
        self._env = patch.dict(os.environ, {"MYSEARCH_PROXY_DB_PATH": str(self.db_path)})
        self._env.start()
        self.db = _load_database_module("test_database_failure_reason_module")
        self.db.init_db()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmpdir.cleanup()

    def _row(self):
        return self.db.get_conn().execute(
            "SELECT service, success, latency_ms, failure_reason, upstream_status FROM usage_logs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def test_failure_reason_and_status_are_persisted(self) -> None:
        self.db.log_usage(
            None, 7, "v2/search", 0, 10005, service="firecrawl",
            failure_reason="rate_limited", upstream_status=429,
        )
        row = self._row()
        self.assertEqual(row["failure_reason"], "rate_limited")
        self.assertEqual(row["upstream_status"], 429)

    def test_success_leaves_reason_empty(self) -> None:
        self.db.log_usage(None, 7, "v2/search", 1, 800, service="firecrawl")
        row = self._row()
        self.assertEqual(row["failure_reason"], "")
        self.assertIsNone(row["upstream_status"])

    def test_migration_adds_columns_to_legacy_table(self) -> None:
        """老库（无这两列）打开时必须自动补列，且旧行取到默认值。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "legacy.db"
            with patch.dict(os.environ, {"MYSEARCH_PROXY_DB_PATH": str(db_path)}):
                module = _load_database_module("test_database_legacy_migration_module")
                conn = module.get_conn()
                conn.execute(
                    "CREATE TABLE usage_logs ("
                    "id INTEGER PRIMARY KEY, service TEXT NOT NULL DEFAULT 'tavily',"
                    "token_id INTEGER, api_key_id INTEGER, endpoint TEXT, success INTEGER,"
                    "latency_ms INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
                )
                conn.execute(
                    "INSERT INTO usage_logs (service, endpoint, success, latency_ms) VALUES ('tavily','/x',0,10)"
                )
                conn.commit()
                module.init_db()
                columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(usage_logs)").fetchall()
                }
                legacy = conn.execute(
                    "SELECT failure_reason, upstream_status FROM usage_logs"
                ).fetchone()
        self.assertIn("failure_reason", columns)
        self.assertIn("upstream_status", columns)
        self.assertEqual(legacy["failure_reason"], "")
        self.assertIsNone(legacy["upstream_status"])


if __name__ == "__main__":
    unittest.main()
