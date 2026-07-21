from __future__ import annotations

import asyncio
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PROXY_ROOT = REPO_ROOT / "proxy"


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class KeyIngestionDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "proxy.db"
        self.env_patch = patch.dict(
            os.environ,
            {"MYSEARCH_PROXY_DB_PATH": str(self.db_path)},
        )
        self.env_patch.start()
        self.db = _load_module(
            f"test_key_ingestion_database_{id(self)}",
            PROXY_ROOT / "database.py",
        )
        self.db.init_db()

    def tearDown(self) -> None:
        self.db.close_conn()
        self.env_patch.stop()
        self.tempdir.cleanup()

    def test_reupload_restores_only_legacy_disabled_key_by_default(self) -> None:
        raw_key = "fc-aaaaaaaaaaaaaaaaaaaa"
        inserted = self.db.ingest_key(raw_key, "first@example.test", "firecrawl")
        self.assertEqual(inserted["status"], "inserted")

        conn = self.db.get_conn()
        conn.execute(
            """
            UPDATE api_keys
            SET active = 0,
                consecutive_fails = 3,
                disabled_reason = '',
                disabled_detail = '',
                disabled_at = NULL
            WHERE id = ?
            """,
            (inserted["id"],),
        )
        conn.commit()

        restored = self.db.ingest_key(raw_key, "second@example.test", "firecrawl")
        self.assertEqual(restored["status"], "reactivated")
        row = dict(self.db.get_key_by_id(inserted["id"]))
        self.assertEqual(row["active"], 1)
        self.assertEqual(row["consecutive_fails"], 0)
        self.assertEqual(row["email"], "second@example.test")

        self.db.toggle_key(inserted["id"], 0)
        protected = self.db.ingest_key(raw_key, service="firecrawl")
        self.assertEqual(protected["status"], "disabled")
        self.assertEqual(self.db.get_key_by_id(inserted["id"])["active"], 0)

        forced = self.db.ingest_key(raw_key, service="firecrawl", reactivate=True)
        self.assertEqual(forced["status"], "reactivated")
        self.assertEqual(self.db.get_key_by_id(inserted["id"])["active"], 1)

    def test_batch_result_distinguishes_changes_duplicates_and_invalid_lines(self) -> None:
        existing_key = "11111111-1111-1111-1111-111111111111"
        new_key = "22222222-2222-2222-2222-222222222222"
        existing = self.db.ingest_key(existing_key, service="exa")
        conn = self.db.get_conn()
        conn.execute(
            "UPDATE api_keys SET active = 0, consecutive_fails = 3 WHERE id = ?",
            (existing["id"],),
        )
        conn.commit()

        result = self.db.ingest_keys_from_text(
            "\n".join(
                [
                    f"old@example.test,{existing_key}",
                    existing_key,
                    new_key,
                    "not-an-exa-key",
                ]
            ),
            service="exa",
        )

        self.assertEqual(result["received"], 4)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["reactivated"], 1)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(result["invalid"], 1)
        self.assertEqual(result["imported"], 2)

    def test_schema_allows_same_literal_in_different_provider_namespaces(self) -> None:
        conn = self.db.get_conn()
        conn.execute(
            "INSERT INTO api_keys (service, key) VALUES (?, ?)",
            ("tavily", "shared-literal"),
        )
        conn.execute(
            "INSERT INTO api_keys (service, key) VALUES (?, ?)",
            ("firecrawl", "shared-literal"),
        )
        conn.commit()
        rows = conn.execute(
            "SELECT service FROM api_keys WHERE key = ? ORDER BY service",
            ("shared-literal",),
        ).fetchall()
        self.assertEqual([row["service"] for row in rows], ["firecrawl", "tavily"])

    def test_init_labels_legacy_disabled_rows_without_enabling_them(self) -> None:
        row = self.db.add_key("tvly-legacy-test-key", service="tavily")
        conn = self.db.get_conn()
        conn.execute(
            "UPDATE api_keys SET active = 0, consecutive_fails = 3 WHERE id = ?",
            (row["id"],),
        )
        conn.commit()

        self.db.init_db()

        migrated = dict(self.db.get_key_by_id(row["id"]))
        self.assertEqual(migrated["active"], 0)
        self.assertEqual(migrated["disabled_reason"], "legacy_failure_threshold")

    def test_init_preserves_legacy_manual_disable_as_manual(self) -> None:
        raw_key = "tvly-legacy-manual-aaaaaaaa"
        row = self.db.add_key(raw_key, service="tavily")
        conn = self.db.get_conn()
        conn.execute(
            "UPDATE api_keys SET active = 0, consecutive_fails = 0 WHERE id = ?",
            (row["id"],),
        )
        conn.commit()

        self.db.init_db()
        migrated = dict(self.db.get_key_by_id(row["id"]))
        self.assertEqual(migrated["disabled_reason"], "manual")

        result = self.db.ingest_key(raw_key, service="tavily")
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(self.db.get_key_by_id(row["id"])["active"], 0)

    def test_init_migrates_real_legacy_schema_and_preserves_existing_rows(self) -> None:
        self.db.close_conn()
        for path in (
            self.db_path,
            Path(f"{self.db_path}-wal"),
            Path(f"{self.db_path}-shm"),
        ):
            path.unlink(missing_ok=True)
        legacy = sqlite3.connect(self.db_path)
        legacy.executescript(
            """
            CREATE TABLE api_keys (
                id INTEGER PRIMARY KEY,
                service TEXT NOT NULL DEFAULT 'tavily',
                key TEXT UNIQUE NOT NULL,
                email TEXT,
                active INTEGER DEFAULT 1,
                total_used INTEGER DEFAULT 0,
                total_failed INTEGER DEFAULT 0,
                consecutive_fails INTEGER DEFAULT 0,
                last_used_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE tokens (
                id INTEGER PRIMARY KEY,
                service TEXT NOT NULL DEFAULT 'tavily',
                token TEXT UNIQUE NOT NULL,
                name TEXT DEFAULT '',
                hourly_limit INTEGER DEFAULT 0,
                daily_limit INTEGER DEFAULT 0,
                monthly_limit INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE usage_logs (
                id INTEGER PRIMARY KEY,
                service TEXT NOT NULL DEFAULT 'tavily',
                token_id INTEGER,
                api_key_id INTEGER,
                endpoint TEXT,
                success INTEGER,
                latency_ms INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        legacy.execute(
            """
            INSERT INTO api_keys (
                id, service, key, email, active, total_used, total_failed,
                consecutive_fails, last_used_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                42,
                "firecrawl",
                "fc-aaaaaaaaaaaaaaaaaaaa",
                "legacy@example.test",
                0,
                9,
                3,
                3,
                "2026-03-20T00:00:00+00:00",
                "2026-03-19 12:00:00",
            ),
        )
        legacy.commit()
        legacy.close()

        self.db.init_db()

        migrated = dict(self.db.get_key_by_id(42))
        self.assertEqual(migrated["service"], "firecrawl")
        self.assertEqual(migrated["email"], "legacy@example.test")
        self.assertEqual(migrated["total_used"], 9)
        self.assertEqual(migrated["active"], 0)
        self.assertEqual(migrated["disabled_reason"], "legacy_failure_threshold")

        conn = self.db.get_conn()
        conn.execute(
            "INSERT INTO api_keys (service, key) VALUES (?, ?)",
            ("exa", "fc-aaaaaaaaaaaaaaaaaaaa"),
        )
        conn.commit()
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM api_keys WHERE key = ?",
                ("fc-aaaaaaaaaaaaaaaaaaaa",),
            ).fetchone()[0],
            2,
        )

    def test_single_ingestion_rejects_key_for_the_wrong_provider(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid firecrawl API key"):
            self.db.ingest_key("tvly-aaaaaaaaaaaaaaaaaaaa", service="firecrawl")

    def test_single_ingestion_keeps_opaque_legacy_credentials_compatible(self) -> None:
        result = self.db.ingest_key("legacy-gateway-credential", service="firecrawl")
        self.assertEqual(result["status"], "inserted")

    def test_repeated_ingestion_remains_idempotent(self) -> None:
        raw_key = "fc-idempotent-aaaaaaaaaaaa"
        first = self.db.ingest_key(raw_key, service="firecrawl")
        second = self.db.ingest_key(raw_key, service="firecrawl")

        self.assertEqual(first["status"], "inserted")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.db.get_all_keys("firecrawl")), 1)


class KeyIngestionApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if str(PROXY_ROOT) not in sys.path:
            sys.path.insert(0, str(PROXY_ROOT))
        cls.server = _load_module(
            "test_key_ingestion_server",
            PROXY_ROOT / "server.py",
        )

    def test_legacy_single_json_contract_is_preserved(self) -> None:
        class Request:
            async def json(self):
                return {
                    "service": "firecrawl",
                    "key": "fc-aaaaaaaaaaaaaaaaaaaa",
                    "email": "registrar@example.test",
                }

        ingestion = {
            "id": 7,
            "status": "reactivated",
            "inserted": False,
            "reactivated": True,
            "metadata_updated": True,
        }
        with patch.object(self.server.db, "ingest_key", return_value=ingestion), patch.object(
            self.server.pool,
            "reload",
        ) as reload_pool:
            result = asyncio.run(self.server.add_keys(Request()))

        self.assertTrue(result["ok"])
        self.assertEqual(result["service"], "firecrawl")
        self.assertEqual(result["status"], "reactivated")
        self.assertEqual(result["imported"], 1)
        reload_pool.assert_called_once_with("firecrawl")

    def test_legacy_batch_json_contract_keeps_imported_field(self) -> None:
        class Request:
            async def json(self):
                return {"service": "exa", "file": "one\ntwo"}

        summary = {
            "received": 2,
            "parsed": 2,
            "inserted": 1,
            "reactivated": 0,
            "duplicates": 1,
            "disabled": 0,
            "invalid": 0,
            "metadata_updated": 0,
            "imported": 1,
        }
        with patch.object(
            self.server.db,
            "ingest_keys_from_text",
            return_value=summary,
        ):
            result = asyncio.run(self.server.add_keys(Request()))

        self.assertEqual(result["service"], "exa")
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["duplicates"], 1)

    def test_upload_token_auth_is_scoped_to_the_ingestion_dependency(self) -> None:
        class Request:
            headers = {"X-Key-Upload-Token": "registrar-secret"}

        with patch.object(
            self.server,
            "MYSEARCH_PROXY_KEY_UPLOAD_TOKEN",
            "registrar-secret",
        ), patch.object(self.server, "verify_admin") as verify_admin:
            self.assertTrue(self.server.verify_key_upload(Request()))

        verify_admin.assert_not_called()

    def test_admin_auth_remains_valid_when_upload_token_is_not_configured(self) -> None:
        class Request:
            headers = {"X-Admin-Password": "admin-secret"}

        with patch.object(
            self.server,
            "MYSEARCH_PROXY_KEY_UPLOAD_TOKEN",
            "",
        ), patch.object(self.server, "verify_admin", return_value=True) as verify_admin:
            self.assertTrue(self.server.verify_key_upload(Request()))

        verify_admin.assert_called_once()

    def test_admin_bearer_is_not_reinterpreted_as_the_upload_token(self) -> None:
        class Request:
            headers = {"Authorization": "Bearer admin-secret"}

        with patch.object(
            self.server,
            "MYSEARCH_PROXY_KEY_UPLOAD_TOKEN",
            "registrar-secret",
        ), patch.object(self.server, "verify_admin", return_value=True) as verify_admin:
            self.assertTrue(self.server.verify_key_upload(Request()))

        verify_admin.assert_called_once()

    def test_wrong_upload_token_does_not_bypass_admin_auth(self) -> None:
        class Request:
            headers = {"X-Key-Upload-Token": "wrong"}

        with patch.object(
            self.server,
            "MYSEARCH_PROXY_KEY_UPLOAD_TOKEN",
            "registrar-secret",
        ), patch.object(
            self.server,
            "verify_admin",
            side_effect=self.server.HTTPException(status_code=401, detail="Unauthorized"),
        ):
            with self.assertRaises(self.server.HTTPException) as ctx:
                self.server.verify_key_upload(Request())

        self.assertEqual(ctx.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
