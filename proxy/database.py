"""
SQLite 数据库管理
"""
import atexit
import math
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "data", "proxy.db")
_thread_local = threading.local()
SUPPORTED_SERVICES = ("tavily", "firecrawl", "exa")
TOKEN_SERVICES = SUPPORTED_SERVICES + ("mysearch",)
TOKEN_PREFIX = {
    "tavily": "tvly-",
    "firecrawl": "fctk-",
    "exa": "exat-",
    "mysearch": "mysp-",
}
KEY_PATTERNS = {
    "tavily": r"(tvly-[A-Za-z0-9\-_]{20,})",
    "firecrawl": r"(fc-[A-Za-z0-9\-_]{20,})",
    "exa": r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})",
}

KEY_USAGE_COLUMNS = {
    "usage_key_used": "INTEGER",
    "usage_key_limit": "INTEGER",
    "usage_key_remaining": "INTEGER",
    "usage_account_plan": "TEXT DEFAULT ''",
    "usage_account_used": "INTEGER",
    "usage_account_limit": "INTEGER",
    "usage_account_remaining": "INTEGER",
    "usage_synced_at": "TEXT",
    "usage_sync_error": "TEXT DEFAULT ''",
    "disabled_reason": "TEXT DEFAULT ''",
    "disabled_detail": "TEXT DEFAULT ''",
    "disabled_at": "TEXT",
    "schedule_until": "TEXT",
}


def normalize_service(service):
    service = (service or "tavily").strip().lower()
    if service not in SUPPORTED_SERVICES:
        raise ValueError(f"unsupported service: {service}")
    return service


def normalize_token_service(service):
    service = (service or "tavily").strip().lower()
    if service not in TOKEN_SERVICES:
        raise ValueError(f"unsupported token service: {service}")
    return service


def get_db_path():
    configured = (os.environ.get("MYSEARCH_PROXY_DB_PATH") or "").strip()
    return configured or DEFAULT_DB_PATH


def get_conn():
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.Error:
            _thread_local.conn = None
    db_path = get_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _thread_local.conn = conn
    return conn


def close_conn():
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _thread_local.conn = None


atexit.register(close_conn)


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY,
            service TEXT NOT NULL DEFAULT 'tavily',
            key TEXT NOT NULL,
            email TEXT,
            active INTEGER DEFAULT 1,
            total_used INTEGER DEFAULT 0,
            total_failed INTEGER DEFAULT 0,
            consecutive_fails INTEGER DEFAULT 0,
            last_used_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(service, key)
        );

        CREATE TABLE IF NOT EXISTS tokens (
            id INTEGER PRIMARY KEY,
            service TEXT NOT NULL DEFAULT 'tavily',
            token TEXT UNIQUE NOT NULL,
            name TEXT DEFAULT '',
            hourly_limit INTEGER DEFAULT 0,
            daily_limit INTEGER DEFAULT 0,
            monthly_limit INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS usage_logs (
            id INTEGER PRIMARY KEY,
            service TEXT NOT NULL DEFAULT 'tavily',
            token_id INTEGER,
            api_key_id INTEGER,
            endpoint TEXT,
            success INTEGER,
            latency_ms INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_logs(created_at);
        CREATE INDEX IF NOT EXISTS idx_usage_token ON usage_logs(token_id);

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    _ensure_service_columns(conn)
    _ensure_usage_columns(conn)
    _ensure_provider_scoped_key_uniqueness(conn)
    _label_legacy_disabled_keys(conn)
    # 当前产品策略：关闭 token 级调用限流，统一把历史限额字段归零。
    conn.execute(
        """
        UPDATE tokens
        SET hourly_limit = 0,
            daily_limit = 0,
            monthly_limit = 0
        WHERE hourly_limit != 0 OR daily_limit != 0 OR monthly_limit != 0
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_service_created ON usage_logs(service, created_at)")
    conn.commit()
    pass  # connection reused via thread-local


_VALID_TABLES = frozenset({"api_keys", "tokens", "usage_logs", "settings"})


def _table_columns(conn, table_name):
    if table_name not in _VALID_TABLES:
        raise ValueError(f"invalid table name: {table_name}")
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def _ensure_service_columns(conn):
    service_columns = {
        "api_keys": "TEXT NOT NULL DEFAULT 'tavily'",
        "tokens": "TEXT NOT NULL DEFAULT 'tavily'",
        "usage_logs": "TEXT NOT NULL DEFAULT 'tavily'",
    }
    for table_name, definition in service_columns.items():
        existing = _table_columns(conn, table_name)
        if "service" not in existing:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN service {definition}")


def _ensure_usage_columns(conn):
    existing = _table_columns(conn, "api_keys")
    for name, definition in KEY_USAGE_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE api_keys ADD COLUMN {name} {definition}")


def _unique_index_columns(conn, table_name):
    indexes = conn.execute(f"PRAGMA index_list({table_name})").fetchall()
    unique_columns = []
    for index in indexes:
        if not index["unique"]:
            continue
        columns = conn.execute(f"PRAGMA index_info({index['name']})").fetchall()
        unique_columns.append(tuple(column["name"] for column in columns))
    return unique_columns


def _ensure_provider_scoped_key_uniqueness(conn):
    unique_columns = _unique_index_columns(conn, "api_keys")
    if ("service", "key") in unique_columns and ("key",) not in unique_columns:
        return

    columns = [row["name"] for row in conn.execute("PRAGMA table_info(api_keys)")]
    quoted_columns = ", ".join(f'"{column}"' for column in columns)
    conn.execute("SAVEPOINT migrate_api_keys_provider_scope")
    try:
        conn.execute("ALTER TABLE api_keys RENAME TO api_keys_legacy")
        conn.execute(
            """
            CREATE TABLE api_keys (
                id INTEGER PRIMARY KEY,
                service TEXT NOT NULL DEFAULT 'tavily',
                key TEXT NOT NULL,
                email TEXT,
                active INTEGER DEFAULT 1,
                total_used INTEGER DEFAULT 0,
                total_failed INTEGER DEFAULT 0,
                consecutive_fails INTEGER DEFAULT 0,
                last_used_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                usage_key_used INTEGER,
                usage_key_limit INTEGER,
                usage_key_remaining INTEGER,
                usage_account_plan TEXT DEFAULT '',
                usage_account_used INTEGER,
                usage_account_limit INTEGER,
                usage_account_remaining INTEGER,
                usage_synced_at TEXT,
                usage_sync_error TEXT DEFAULT '',
                disabled_reason TEXT DEFAULT '',
                disabled_detail TEXT DEFAULT '',
                disabled_at TEXT,
                schedule_until TEXT,
                UNIQUE(service, key)
            )
            """
        )
        conn.execute(
            f"INSERT INTO api_keys ({quoted_columns}) SELECT {quoted_columns} FROM api_keys_legacy"
        )
        conn.execute("DROP TABLE api_keys_legacy")
        conn.execute("RELEASE SAVEPOINT migrate_api_keys_provider_scope")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT migrate_api_keys_provider_scope")
        conn.execute("RELEASE SAVEPOINT migrate_api_keys_provider_scope")
        raise


def _label_legacy_disabled_keys(conn):
    conn.execute(
        """
        UPDATE api_keys
        SET disabled_reason = 'legacy_failure_threshold',
            disabled_detail = 'disabled by the legacy consecutive failure threshold'
        WHERE active = 0
          AND COALESCE(disabled_reason, '') = ''
          AND disabled_at IS NULL
          AND consecutive_fails >= 3
        """
    )
    conn.execute(
        """
        UPDATE api_keys
        SET disabled_reason = 'manual',
            disabled_detail = 'manual disable migrated from the legacy key state'
        WHERE active = 0
          AND COALESCE(disabled_reason, '') = ''
          AND disabled_at IS NULL
        """
    )


def _service_where(service, normalizer=normalize_service):
    if not service:
        return "", []
    return " WHERE service = ?", [normalizer(service)]


def _query_all(conn, table_name, service=None):
    if table_name not in _VALID_TABLES:
        raise ValueError(f"invalid table name: {table_name}")
    where_sql, params = _service_where(service)
    return conn.execute(f"SELECT * FROM {table_name}{where_sql} ORDER BY id", params).fetchall()


# ═══ Settings ═══

def get_setting(key, default=None):
    conn = get_conn()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        pass  # connection reused via thread-local


def set_setting(key, value):
    conn = get_conn()
    try:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
    finally:
        pass  # connection reused via thread-local


# ═══ API Keys ═══

def add_key(key, email="", service="tavily"):
    service = normalize_service(service)
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO api_keys (service, key, email) VALUES (?, ?, ?)",
            (service, key, email),
        )
        conn.commit()
        return conn.execute(
            "SELECT * FROM api_keys WHERE service = ? AND key = ?",
            (service, key),
        ).fetchone()
    finally:
        pass  # connection reused via thread-local


def get_all_keys(service=None):
    conn = get_conn()
    try:
        return _query_all(conn, "api_keys", service)
    finally:
        pass  # connection reused via thread-local


def get_key_by_id(key_id):
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
    finally:
        pass  # connection reused via thread-local


def get_token_by_id(token_id):
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM tokens WHERE id = ?", (token_id,)).fetchone()
    finally:
        pass  # connection reused via thread-local


def get_active_keys(service=None):
    conn = get_conn()
    try:
        where_sql, params = _service_where(service)
        sql = f"SELECT * FROM api_keys{where_sql}"
        if where_sql:
            sql += " AND active = 1"
        else:
            sql += " WHERE active = 1"
        sql += " AND (schedule_until IS NULL OR schedule_until <= ?)"
        params.append(datetime.now(timezone.utc).isoformat())
        sql += " ORDER BY id"
        return conn.execute(sql, params).fetchall()
    finally:
        pass  # connection reused via thread-local


def get_next_key_schedule_delay(service=None):
    """Return seconds until the next temporarily cooled key becomes schedulable."""
    conn = get_conn()
    try:
        where_sql, params = _service_where(service)
        now = datetime.now(timezone.utc)
        sql = f"SELECT MIN(schedule_until) AS next_at FROM api_keys{where_sql}"
        if where_sql:
            sql += " AND active = 1 AND schedule_until > ?"
        else:
            sql += " WHERE active = 1 AND schedule_until > ?"
        params.append(now.isoformat())
        row = conn.execute(sql, params).fetchone()
        raw_next = str((row["next_at"] if row else "") or "").strip()
        if not raw_next:
            return None
        next_at = datetime.fromisoformat(raw_next)
        if next_at.tzinfo is None:
            next_at = next_at.replace(tzinfo=timezone.utc)
        return max(0.0, (next_at - now).total_seconds())
    except (TypeError, ValueError):
        return None
    finally:
        pass  # connection reused via thread-local


def normalize_retry_after_seconds(value, default=60):
    try:
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError("retry delay must be finite")
        return max(1, min(86400, math.ceil(numeric)))
    except (TypeError, ValueError, OverflowError):
        return max(1, min(86400, int(default)))


def update_key_usage(
    key_id,
    success,
    *,
    failure_kind="",
    failure_detail="",
    retry_after_seconds=None,
):
    conn = get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        if success:
            conn.execute(
                """
                UPDATE api_keys
                SET total_used = total_used + 1,
                    consecutive_fails = 0,
                    last_used_at = ?,
                    disabled_reason = CASE
                        WHEN active = 1 AND (schedule_until IS NULL OR schedule_until <= ?)
                        THEN '' ELSE disabled_reason END,
                    disabled_detail = CASE
                        WHEN active = 1 AND (schedule_until IS NULL OR schedule_until <= ?)
                        THEN '' ELSE disabled_detail END,
                    schedule_until = CASE
                        WHEN active = 1 AND schedule_until IS NOT NULL AND schedule_until <= ?
                        THEN NULL ELSE schedule_until END,
                    disabled_at = CASE
                        WHEN active = 1 AND (schedule_until IS NULL OR schedule_until <= ?)
                        THEN NULL ELSE disabled_at END
                WHERE id = ?
                """,
                (now, now, now, now, now, key_id),
            )
        else:
            normalized_kind = (failure_kind or "").strip().lower()
            normalized_detail = " ".join(str(failure_detail or "").split())[:500]
            conn.execute(
                "UPDATE api_keys SET total_failed = total_failed + 1, consecutive_fails = consecutive_fails + 1, last_used_at = ? WHERE id = ?",
                (now, key_id),
            )
            if normalized_kind == "rate_limited":
                cooldown_seconds = normalize_retry_after_seconds(retry_after_seconds)
                schedule_until = (
                    datetime.now(timezone.utc) + timedelta(seconds=cooldown_seconds)
                ).isoformat()
                conn.execute(
                    """
                    UPDATE api_keys
                    SET disabled_reason = ?,
                        disabled_detail = ?,
                        disabled_at = ?,
                        schedule_until = CASE
                            WHEN schedule_until IS NULL OR schedule_until < ?
                            THEN ? ELSE schedule_until END
                    WHERE id = ? AND active = 1
                    """,
                    (
                        normalized_kind,
                        normalized_detail,
                        now,
                        schedule_until,
                        schedule_until,
                        key_id,
                    ),
                )
            elif normalized_kind:
                conn.execute(
                    """
                    UPDATE api_keys
                    SET active = 0,
                        disabled_reason = ?,
                        disabled_detail = ?,
                        disabled_at = ?,
                        schedule_until = NULL
                    WHERE id = ?
                    """,
                    (normalized_kind, normalized_detail, now, key_id),
                )
        conn.commit()
    finally:
        pass  # connection reused via thread-local


def toggle_key(key_id, active):
    conn = get_conn()
    try:
        if active:
            conn.execute(
                """
                UPDATE api_keys
                SET active = 1,
                    consecutive_fails = 0,
                    disabled_reason = '',
                    disabled_detail = '',
                    disabled_at = NULL,
                    schedule_until = NULL
                WHERE id = ?
                """,
                (key_id,),
            )
        else:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                UPDATE api_keys
                SET active = 0,
                    consecutive_fails = 0,
                    disabled_reason = 'manual',
                    disabled_detail = '',
                    disabled_at = ?,
                    schedule_until = NULL
                WHERE id = ?
                """,
                (now, key_id),
            )
        conn.commit()
    finally:
        pass  # connection reused via thread-local


def delete_key(key_id):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
        conn.commit()
    finally:
        pass  # connection reused via thread-local


def _normalize_ingested_key(key, service):
    if not isinstance(key, str):
        raise ValueError("key must be a string")
    normalized = key.strip()
    if not normalized:
        raise ValueError(f"invalid {service} API key")
    for known_service, pattern in KEY_PATTERNS.items():
        if known_service != service and re.fullmatch(pattern, normalized):
            raise ValueError(f"invalid {service} API key")
    # Keep the original single-key API compatible with opaque gateway credentials;
    # batch imports remain format-filtered by ingest_keys_from_text().
    return normalized


def ingest_key(key, email="", service="tavily", *, reactivate=False):
    service = normalize_service(service)
    normalized_key = _normalize_ingested_key(key, service)
    if email is None:
        normalized_email = ""
    elif isinstance(email, str):
        normalized_email = email.strip()
    else:
        raise ValueError("email must be a string")

    conn = get_conn()
    cursor = conn.execute(
        "INSERT OR IGNORE INTO api_keys (service, key, email) VALUES (?, ?, ?)",
        (service, normalized_key, normalized_email),
    )
    conn.commit()
    if cursor.rowcount == 1:
        return {
            "id": cursor.lastrowid,
            "status": "inserted",
            "inserted": True,
            "reactivated": False,
            "metadata_updated": False,
        }

    row = conn.execute(
        "SELECT * FROM api_keys WHERE service = ? AND key = ?",
        (service, normalized_key),
    ).fetchone()
    if row is None:
        raise sqlite3.IntegrityError("key ingestion conflict did not resolve to an existing row")
    row = dict(row)
    disabled_reason = str(row.get("disabled_reason") or "").strip()
    is_legacy_disabled = not row["active"] and disabled_reason in {
        "",
        "legacy_failure_threshold",
    }
    should_reactivate = bool(reactivate) and (
        not row["active"] or disabled_reason or row.get("schedule_until")
    )
    should_reactivate = should_reactivate or is_legacy_disabled
    metadata_updated = bool(normalized_email and normalized_email != (row.get("email") or ""))

    updates = []
    params = []
    if metadata_updated:
        updates.append("email = ?")
        params.append(normalized_email)
    if should_reactivate:
        updates.extend(
            [
                "active = 1",
                "consecutive_fails = 0",
                "disabled_reason = ''",
                "disabled_detail = ''",
                "disabled_at = NULL",
                "schedule_until = NULL",
            ]
        )
    if updates:
        params.extend([service, normalized_key])
        conn.execute(
            f"UPDATE api_keys SET {', '.join(updates)} WHERE service = ? AND key = ?",
            params,
        )
        conn.commit()

    if should_reactivate:
        status = "reactivated"
    elif not row["active"]:
        status = "disabled"
    else:
        status = "duplicate"
    return {
        "id": row["id"],
        "status": status,
        "inserted": False,
        "reactivated": should_reactivate,
        "metadata_updated": metadata_updated,
    }


def ingest_keys_from_text(text, service="tavily", *, reactivate=False):
    if not isinstance(text, str):
        raise ValueError("file must be a string")
    service = normalize_service(service)
    pattern = KEY_PATTERNS[service]
    summary = {
        "received": 0,
        "parsed": 0,
        "inserted": 0,
        "reactivated": 0,
        "duplicates": 0,
        "disabled": 0,
        "invalid": 0,
        "metadata_updated": 0,
        "imported": 0,
    }
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        summary["received"] += 1
        match = re.search(pattern, line)
        if not match:
            summary["invalid"] += 1
            continue
        summary["parsed"] += 1
        parts = line.split(",")
        email = parts[0].strip() if len(parts) >= 2 else ""
        result = ingest_key(
            match.group(1),
            email,
            service,
            reactivate=reactivate,
        )
        status_field = "duplicates" if result["status"] == "duplicate" else result["status"]
        summary[status_field] += 1
        if result["metadata_updated"]:
            summary["metadata_updated"] += 1
    summary["imported"] = summary["inserted"] + summary["reactivated"]
    return summary


def import_keys_from_text(text, service="tavily"):
    """从批量文本导入不同服务的 key。"""
    return ingest_keys_from_text(text, service=service)["imported"]


def update_key_remote_usage(
    key_id,
    *,
    key_used=None,
    key_limit=None,
    key_remaining=None,
    account_plan="",
    account_used=None,
    account_limit=None,
    account_remaining=None,
    synced_at=None,
):
    conn = get_conn()
    try:
        conn.execute(
            """
            UPDATE api_keys
            SET usage_key_used = ?,
                usage_key_limit = ?,
                usage_key_remaining = ?,
                usage_account_plan = ?,
                usage_account_used = ?,
                usage_account_limit = ?,
                usage_account_remaining = ?,
                usage_synced_at = ?,
                usage_sync_error = ''
            WHERE id = ?
            """,
            (
                key_used,
                key_limit,
                key_remaining,
                account_plan or "",
                account_used,
                account_limit,
                account_remaining,
                synced_at or datetime.now(timezone.utc).isoformat(),
                key_id,
            ),
        )
        quota_exhausted = any(
            value is not None and int(value) <= 0
            for value in (key_remaining, account_remaining)
        )
        if quota_exhausted:
            now = synced_at or datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                UPDATE api_keys
                SET active = 0,
                    disabled_reason = 'quota_exhausted',
                    disabled_detail = 'provider usage sync reported no remaining quota',
                    disabled_at = ?,
                    schedule_until = NULL
                WHERE id = ?
                """,
                (now, key_id),
            )
        conn.commit()
    finally:
        pass  # connection reused via thread-local


def update_key_remote_usage_error(key_id, error_message):
    conn = get_conn()
    try:
        row = conn.execute("SELECT key FROM api_keys WHERE id = ?", (key_id,)).fetchone()
        safe_error = str(error_message or "").strip()
        if row and row["key"]:
            safe_error = safe_error.replace(str(row["key"]), "<redacted>")
        conn.execute(
            "UPDATE api_keys SET usage_sync_error = ? WHERE id = ?",
            (safe_error[:200], key_id),
        )
        conn.commit()
    finally:
        pass  # connection reused via thread-local


# ═══ Tokens ═══

def create_token(name="", service="tavily"):
    service = normalize_token_service(service)
    token = TOKEN_PREFIX[service] + secrets.token_urlsafe(24)
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO tokens (service, token, name) VALUES (?, ?, ?)",
            (service, token, name),
        )
        conn.commit()
        return conn.execute("SELECT * FROM tokens WHERE token = ?", (token,)).fetchone()
    finally:
        pass  # connection reused via thread-local


def get_all_tokens(service=None):
    conn = get_conn()
    try:
        where_sql, params = _service_where(service, normalize_token_service)
        return conn.execute(f"SELECT * FROM tokens{where_sql} ORDER BY id", params).fetchall()
    finally:
        pass  # connection reused via thread-local


def get_token_by_value(token_value):
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM tokens WHERE token = ?", (token_value,)).fetchone()
    finally:
        pass  # connection reused via thread-local


def get_token_by_name(name, service="tavily"):
    service = normalize_token_service(service)
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM tokens WHERE service = ? AND name = ? ORDER BY id LIMIT 1",
            (service, name),
        ).fetchone()
    finally:
        pass  # connection reused via thread-local


def delete_token(token_id):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
        conn.commit()
    finally:
        pass  # connection reused via thread-local


# ═══ Usage Logs ═══

def log_usage(token_id, api_key_id, endpoint, success, latency_ms, service="tavily"):
    service = normalize_token_service(service)
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO usage_logs (service, token_id, api_key_id, endpoint, success, latency_ms)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (service, token_id, api_key_id, endpoint, success, latency_ms),
        )
        conn.commit()
    finally:
        pass  # connection reused via thread-local


def get_usage_stats(token_id=None, service=None):
    """获取用量统计。"""
    conn = get_conn()
    try:
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        month = now.strftime("%Y-%m")
        hour_ago = now.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")

        filters = []
        filter_params = []
        if service and service != "mysearch":
            filters.append("service = ?")
            filter_params.append(normalize_service(service))
        if token_id is not None:
            filters.append("token_id = ?")
            filter_params.append(token_id)

        def count(condition, extra_params=None):
            where_parts = [condition] + filters
            sql = "SELECT COUNT(*) as c FROM usage_logs WHERE " + " AND ".join(where_parts)
            params = list(extra_params or []) + filter_params
            row = conn.execute(sql, params).fetchone()
            return row["c"]

        return {
            "today_success": count("success = 1 AND created_at >= ?", [today]),
            "today_failed": count("success = 0 AND created_at >= ?", [today]),
            "month_success": count("success = 1 AND created_at >= ?", [month]),
            "hour_count": count("created_at >= ?", [hour_ago]),
            "today_count": count("created_at >= ?", [today]),
            "month_count": count("created_at >= ?", [month]),
        }
    finally:
        pass  # connection reused via thread-local


def check_quota(token_id, hourly_limit, daily_limit, monthly_limit, service=None):
    """保留兼容接口：当前版本不对 token 做调用限额拦截。"""
    return True, ""
