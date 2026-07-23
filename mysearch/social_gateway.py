"""Structured social search gateway for MySearch compatible mode."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request

logger = logging.getLogger(__name__)

# r7 A1: 改从 grok_registry 子模块导入，避免触发 config 顶层 bootstrap 副作用。
from .grok_registry import _BUILTIN_GROK_MODELS, _resolve_grok_models


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _grok_default_primary() -> str:
    """social gateway 主模型默认值。

    优先使用用户在 `MYSEARCH_GROK_MODELS` / `MYSEARCH_GROK_EXTRA_MODELS` 配置的清单第 1 项，
    回退到内置 basic 层第 1 项（`grok-4.20-0309`）。这样 `SOCIAL_GATEWAY_MODEL` 与
    `MYSEARCH_GROK_MODELS` 在零配置下天然一致。
    """

    resolved = _resolve_grok_models()
    if resolved:
        return resolved[0].id
    return _BUILTIN_GROK_MODELS[0].id


def _grok_default_fallback(primary: str) -> str:
    """social gateway fallback 模型默认值。

    取清单第 2 项；若清单只有 1 项则与主模型相同（此时 `has_social_fallback`
    会自动判定 fallback 不可用，不会触发额外请求）。
    """

    resolved = _resolve_grok_models()
    if len(resolved) >= 2:
        return resolved[1].id
    if len(_BUILTIN_GROK_MODELS) >= 2:
        return _BUILTIN_GROK_MODELS[1].id
    return primary


def _normalize_path(value: str, default: str) -> str:
    normalized = value.strip() or default
    if not normalized.startswith("/"):
        return f"/{normalized}"
    return normalized


def _derive_admin_base_url(upstream_base_url: str) -> str:
    if upstream_base_url.endswith("/v1"):
        return upstream_base_url[:-3]
    return upstream_base_url


UPSTREAM_BASE_URL = _env_str("SOCIAL_GATEWAY_UPSTREAM_BASE_URL", "https://api.x.ai/v1").rstrip("/")
UPSTREAM_RESPONSES_PATH = _normalize_path(
    _env_str("SOCIAL_GATEWAY_UPSTREAM_RESPONSES_PATH", "/responses"),
    "/responses",
)
UPSTREAM_API_KEY = _env_str("SOCIAL_GATEWAY_UPSTREAM_API_KEY")


# r7 A2: MODEL / FALLBACK_MODEL 保留 module 顶层快照（backward-compat：
# 历史调用方、tests 通过 `social_gateway.MODEL` 访问），但请求路径必须改用
# current_*() 实时函数。这样：
#   - 启动期 env 已就绪：快照 == 实时值（行为不变）
#   - 常驻进程运行时 env 被改：current_*() 反映最新，MODEL 快照仍是启动值
#   - 外部模块通过 PEP 562 也能拿到当前值（见 __getattr__）
def current_model() -> str:
    """实时获取 social gateway 主模型——优先 env override，回落到 registry 首项。"""
    explicit = _env_str("SOCIAL_GATEWAY_MODEL")
    return explicit or _grok_default_primary()


def current_fallback_model() -> str:
    """实时获取 social gateway fallback——优先 env override，回落到 registry 第 2 项。"""
    explicit = _env_str("SOCIAL_GATEWAY_FALLBACK_MODEL")
    if explicit:
        return explicit
    return _grok_default_fallback(current_model())


MODEL = current_model()
FALLBACK_MODEL = current_fallback_model()
GATEWAY_TOKEN = _env_str("SOCIAL_GATEWAY_TOKEN")
ADMIN_BASE_URL = _env_str("SOCIAL_GATEWAY_ADMIN_BASE_URL") or _derive_admin_base_url(UPSTREAM_BASE_URL)
ADMIN_VERIFY_PATH = _normalize_path(
    _env_str("SOCIAL_GATEWAY_ADMIN_VERIFY_PATH", "/v1/admin/verify"),
    "/v1/admin/verify",
)
ADMIN_CONFIG_PATH = _normalize_path(
    _env_str("SOCIAL_GATEWAY_ADMIN_CONFIG_PATH", "/admin/api/config"),
    "/admin/api/config",
)
ADMIN_TOKENS_PATH = _normalize_path(
    _env_str("SOCIAL_GATEWAY_ADMIN_TOKENS_PATH", "/admin/api/tokens"),
    "/admin/api/tokens",
)
ADMIN_APP_KEY = _env_str("SOCIAL_GATEWAY_ADMIN_APP_KEY")
ADMIN_USERNAME = _env_str("SOCIAL_GATEWAY_ADMIN_USERNAME")
ADMIN_PASSWORD = _env_str("SOCIAL_GATEWAY_ADMIN_PASSWORD")
V3_ADMIN_PREFIX = "/api/admin/v1"
try:
    CACHE_TTL_SECONDS = max(5, int(_env_str("SOCIAL_GATEWAY_CACHE_TTL_SECONDS", "60")))
except (TypeError, ValueError):
    CACHE_TTL_SECONDS = 60
try:
    SOCIAL_GATEWAY_TIMEOUT_SECONDS = max(
        30,
        int(_env_str("SOCIAL_GATEWAY_TIMEOUT_SECONDS", "120")),
    )
except (TypeError, ValueError):
    SOCIAL_GATEWAY_TIMEOUT_SECONDS = 120
try:
    FALLBACK_MIN_RESULTS = max(1, int(_env_str("SOCIAL_GATEWAY_FALLBACK_MIN_RESULTS", "3")))
except (TypeError, ValueError):
    FALLBACK_MIN_RESULTS = 3

http_client = httpx.AsyncClient(
    timeout=SOCIAL_GATEWAY_TIMEOUT_SECONDS,
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
)
state_cache: dict[str, Any] = {"expires_at": 0.0, "value": None}
social_upstream_key_schedule: dict[str, dict[str, Any]] = {}
social_upstream_key_cursor = 0
social_upstream_key_lock = threading.Lock()
state_lock: asyncio.Lock | None = None
state_lock_loop: asyncio.AbstractEventLoop | None = None
admin_session_cache: dict[str, Any] = {
    "fingerprint": "",
    "access_token": "",
    "expires_at": 0.0,
}
admin_session_lock: asyncio.Lock | None = None
admin_session_lock_loop: asyncio.AbstractEventLoop | None = None


def get_state_lock() -> asyncio.Lock:
    global state_lock, state_lock_loop
    loop = asyncio.get_running_loop()
    if state_lock is None or state_lock_loop is not loop:
        state_lock = asyncio.Lock()
        state_lock_loop = loop
    return state_lock


def get_admin_session_lock() -> asyncio.Lock:
    global admin_session_lock, admin_session_lock_loop
    loop = asyncio.get_running_loop()
    if admin_session_lock is None or admin_session_lock_loop is not loop:
        admin_session_lock = asyncio.Lock()
        admin_session_lock_loop = loop
    return admin_session_lock


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        yield
    finally:
        await http_client.aclose()


app = FastAPI(title="MySearch Social Gateway", lifespan=lifespan)


def extract_token(request: Request, body: dict[str, Any] | None) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    if body and isinstance(body.get("api_key"), str):
        return body["api_key"]
    return None


def unique_preserve_order(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        value = (item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def parse_secret_values(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return unique_preserve_order(re.split(r"[\n,]", value))
    if isinstance(value, (list, tuple, set)):
        return unique_preserve_order([str(item) for item in value])
    return []


def build_empty_social_stats() -> dict[str, Any]:
    return {
        "token_total": 0,
        "token_normal": 0,
        "token_limited": 0,
        "token_invalid": 0,
        "chat_remaining": 0,
        "image_remaining": 0,
        "video_remaining": None,
        "total_calls": 0,
        "nsfw_enabled": 0,
        "nsfw_disabled": 0,
        "pool_count": 0,
        "pools": [],
    }


def _parse_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    if len(value) <= 8:
        return f"{value[:2]}***{value[-2:]}"
    if len(value) <= 12:
        return f"{value[:3]}***{value[-3:]}"
    return f"{value[:6]}***{value[-4:]}"


def unwrap_social_tokens_payload(tokens_payload: Any) -> Any:
    if isinstance(tokens_payload, dict):
        for key_name in ("tokens", "data", "items", "result", "pools"):
            candidate = tokens_payload.get(key_name)
            if isinstance(candidate, dict):
                return candidate
            if isinstance(candidate, list):
                return {"default": candidate}
        return tokens_payload
    if isinstance(tokens_payload, list):
        return {"default": tokens_payload}
    return {}


def flatten_social_tokens(tokens_payload: Any) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    normalized = unwrap_social_tokens_payload(tokens_payload)
    if not isinstance(normalized, dict):
        return flat

    for pool_name, items in normalized.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, str):
                token_value = item
                status = "active"
                quota = 0
                use_count = 0
                tags: list[str] = []
            elif isinstance(item, dict):
                token_value = str(item.get("token") or "")
                status = str(item.get("status") or "active").strip().lower()
                quota = max(0, _parse_int(item.get("quota")))
                use_count = max(0, _parse_int(item.get("use_count")))
                raw_tags = item.get("tags") or []
                tags = (
                    [str(tag).strip() for tag in raw_tags if str(tag).strip()]
                    if isinstance(raw_tags, list)
                    else []
                )
            else:
                continue

            flat.append(
                {
                    "pool": str(pool_name),
                    "token_masked": mask_secret(token_value),
                    "status": status,
                    "quota": quota,
                    "use_count": use_count,
                    "tags": tags,
                }
            )
    return flat


def build_social_token_stats(tokens_payload: Any) -> dict[str, Any]:
    stats = build_empty_social_stats()
    flat_tokens = flatten_social_tokens(tokens_payload)

    if not flat_tokens:
        return stats

    active_tokens = [item for item in flat_tokens if item["status"] == "active"]
    cooling_tokens = [item for item in flat_tokens if item["status"] == "cooling"]
    invalid_tokens = [
        item for item in flat_tokens if item["status"] not in {"active", "cooling"}
    ]
    chat_remaining = sum(item["quota"] for item in active_tokens)
    pools: dict[str, dict[str, Any]] = {}
    for item in flat_tokens:
        pool = pools.setdefault(
            item["pool"],
            {"pool": item["pool"], "count": 0, "active": 0, "cooling": 0, "invalid": 0},
        )
        pool["count"] += 1
        if item["status"] == "active":
            pool["active"] += 1
        elif item["status"] == "cooling":
            pool["cooling"] += 1
        else:
            pool["invalid"] += 1

    stats.update(
        {
            "token_total": len(flat_tokens),
            "token_normal": len(active_tokens),
            "token_limited": len(cooling_tokens),
            "token_invalid": len(invalid_tokens),
            "chat_remaining": chat_remaining,
            "image_remaining": chat_remaining // 2,
            "total_calls": sum(item["use_count"] for item in flat_tokens),
            "nsfw_enabled": sum("nsfw" in item["tags"] for item in flat_tokens),
            "nsfw_disabled": sum("nsfw" not in item["tags"] for item in flat_tokens),
            "pool_count": len(pools),
            "pools": sorted(pools.values(), key=lambda item: item["pool"]),
        }
    )
    return stats


def build_v3_account_stats(
    summary_payload: dict[str, Any],
    dashboard_payload: dict[str, Any],
) -> dict[str, Any]:
    resources = dashboard_payload.get("resources")
    if not isinstance(resources, dict):
        resources = {}
    usage = dashboard_payload.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    providers = summary_payload.get("providers")
    if not isinstance(providers, dict):
        providers = {}

    total = max(0, _parse_int(summary_payload.get("total")))
    available = max(0, _parse_int(summary_payload.get("available")))
    recovering = max(0, _parse_int(summary_payload.get("recovering")))
    attention = max(0, _parse_int(summary_payload.get("attention")))
    pools: list[dict[str, Any]] = []
    for provider_name, provider_payload in providers.items():
        if not isinstance(provider_payload, dict):
            continue
        provider_total = max(0, _parse_int(provider_payload.get("total")))
        provider_available = max(0, _parse_int(provider_payload.get("available")))
        pools.append(
            {
                "pool": str(provider_name),
                "count": provider_total,
                "active": provider_available,
                "cooling": 0,
                "invalid": max(0, provider_total - provider_available),
            }
        )

    stats = build_empty_social_stats()
    stats.update(
        {
            "schema": "grok2api_v3_accounts",
            "token_total": total,
            "token_normal": available,
            "token_limited": recovering,
            "token_invalid": attention,
            "chat_remaining": None,
            "image_remaining": None,
            "total_calls": max(0, _parse_int(resources.get("allTimeRequests"))),
            "requests_24h": max(0, _parse_int(usage.get("requests"))),
            "successful_requests_24h": max(
                0,
                _parse_int(usage.get("successfulRequests")),
            ),
            "failed_requests_24h": max(0, _parse_int(usage.get("failedRequests"))),
            "account_total": total,
            "account_available": available,
            "account_recovering": recovering,
            "account_attention": attention,
            "pool_count": len(pools),
            "pools": sorted(pools, key=lambda item: item["pool"]),
        }
    )
    return stats


def build_gateway_mode(state: dict[str, Any]) -> str:
    if state.get("admin_api_version") == "v3":
        if state["manual_upstream_key"] or state["manual_gateway_token"]:
            return "v3-managed"
        return "v3-observe"
    if state["admin_connected"] and (state["manual_upstream_key"] or state["manual_gateway_token"]):
        return "hybrid"
    if state["admin_connected"]:
        return "admin-auto"
    return "manual"


def build_token_source(state: dict[str, Any]) -> str:
    if state["manual_gateway_token"]:
        return "manual SOCIAL_GATEWAY_TOKEN"
    if state["admin_connected"] and state["admin_api_keys"]:
        return "grok2api app.api_key"
    if state["manual_upstream_key"]:
        return "SOCIAL_GATEWAY_UPSTREAM_API_KEY"
    return "not_configured"


async def fetch_admin_json(path: str) -> dict[str, Any]:
    if not ADMIN_APP_KEY:
        raise RuntimeError("Missing SOCIAL_GATEWAY_ADMIN_APP_KEY")
    response = await http_client.get(
        f"{ADMIN_BASE_URL}{path}",
        headers={"Authorization": f"Bearer {ADMIN_APP_KEY}"},
    )
    try:
        payload = response.json()
    except Exception:
        payload = None
    if response.status_code >= 400:
        detail = ""
        if isinstance(payload, dict):
            detail = str(payload.get("detail") or payload.get("message") or "")
        if not detail:
            detail = response.text[:240] or f"HTTP {response.status_code}"
        raise RuntimeError(f"{path} -> {detail}")
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} -> expected JSON object")
    return payload


class AdminUnauthorizedError(RuntimeError):
    pass


def _admin_error_detail(path: str, response: httpx.Response, payload: Any) -> str:
    detail = ""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            detail = str(error.get("message") or error.get("code") or "")
        if not detail:
            detail = str(payload.get("detail") or payload.get("message") or "")
    if not detail:
        detail = response.text.strip()[:240] or f"HTTP {response.status_code}"
    return f"{path} -> {detail}"


def _admin_session_fingerprint() -> str:
    raw = "\0".join((ADMIN_BASE_URL, ADMIN_USERNAME, ADMIN_PASSWORD))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def clear_admin_session(fingerprint: str = "") -> None:
    if fingerprint and admin_session_cache.get("fingerprint") != fingerprint:
        return
    admin_session_cache.update(
        {"fingerprint": "", "access_token": "", "expires_at": 0.0}
    )


async def get_v3_admin_access_token() -> str:
    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        raise RuntimeError("grok2api v3 admin username and password are required")
    fingerprint = _admin_session_fingerprint()
    now = time.time()
    if (
        admin_session_cache.get("fingerprint") == fingerprint
        and admin_session_cache.get("access_token")
        and admin_session_cache.get("expires_at", 0) > now + 30
    ):
        return str(admin_session_cache["access_token"])

    async with get_admin_session_lock():
        now = time.time()
        if (
            admin_session_cache.get("fingerprint") == fingerprint
            and admin_session_cache.get("access_token")
            and admin_session_cache.get("expires_at", 0) > now + 30
        ):
            return str(admin_session_cache["access_token"])

        path = f"{V3_ADMIN_PREFIX}/auth/login"
        response = await http_client.post(
            f"{ADMIN_BASE_URL}{path}",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
            headers={"Content-Type": "application/json"},
        )
        try:
            payload = response.json()
        except Exception:
            payload = None
        if response.status_code >= 400:
            raise RuntimeError(_admin_error_detail(path, response, payload))
        data = payload.get("data") if isinstance(payload, dict) else None
        tokens = data.get("tokens") if isinstance(data, dict) else None
        access_token = tokens.get("accessToken") if isinstance(tokens, dict) else ""
        if not isinstance(access_token, str) or not access_token.strip():
            raise RuntimeError(f"{path} -> access token missing from response")

        expires_at = now + 600
        expires_text = tokens.get("accessTokenExpiresAt") if isinstance(tokens, dict) else ""
        if isinstance(expires_text, str) and expires_text.strip():
            try:
                parsed = datetime.fromisoformat(expires_text.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                expires_at = parsed.timestamp()
            except ValueError:
                pass
        admin_session_cache.update(
            {
                "fingerprint": fingerprint,
                "access_token": access_token.strip(),
                "expires_at": expires_at,
            }
        )
        return access_token.strip()


async def fetch_v3_admin_json(path: str, access_token: str) -> dict[str, Any]:
    response = await http_client.get(
        f"{ADMIN_BASE_URL}{path}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        payload = response.json()
    except Exception:
        payload = None
    if response.status_code == 401:
        raise AdminUnauthorizedError(_admin_error_detail(path, response, payload))
    if response.status_code >= 400:
        raise RuntimeError(_admin_error_detail(path, response, payload))
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} -> expected data object")
    return data


async def fetch_v3_admin_state() -> tuple[dict[str, Any], dict[str, Any]]:
    fingerprint = _admin_session_fingerprint()
    for attempt in range(2):
        access_token = await get_v3_admin_access_token()
        try:
            return await asyncio.gather(
                fetch_v3_admin_json(
                    f"{V3_ADMIN_PREFIX}/accounts/summary",
                    access_token,
                ),
                fetch_v3_admin_json(
                    f"{V3_ADMIN_PREFIX}/dashboard?period=24h&timezone=UTC",
                    access_token,
                ),
            )
        except AdminUnauthorizedError:
            clear_admin_session(fingerprint)
            if attempt:
                raise
    raise RuntimeError("grok2api v3 admin authentication failed")


def build_admin_path_candidates(path: str, *, kind: str) -> list[str]:
    normalized = _normalize_path(path, f"/admin/api/{kind}")
    candidates: list[str] = [normalized]

    variants = {
        normalized,
        normalized.replace("/v1/admin/", "/admin/api/"),
        normalized.replace("/api/v1/admin/", "/admin/api/"),
        normalized.replace("/admin/api/", "/v1/admin/"),
        normalized.replace("/admin/api/", "/api/v1/admin/"),
        normalized.replace("/v1/admin/", "/api/v1/admin/"),
        normalized.replace("/api/v1/admin/", "/v1/admin/"),
    }

    ordered = [
        f"/admin/api/{kind}",
        f"/v1/admin/{kind}",
        f"/api/v1/admin/{kind}",
    ]
    for candidate in ordered:
        if candidate in variants and candidate not in candidates:
            candidates.append(candidate)
    for candidate in variants:
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _safe_get(d: Any, key: str) -> Any:
    if isinstance(d, dict):
        return d.get(key)
    return None


def extract_admin_api_keys(admin_config: dict[str, Any]) -> list[str]:
    app = _safe_get(admin_config, "app")
    data = _safe_get(admin_config, "data")
    config = _safe_get(admin_config, "config")
    candidates: list[Any] = [
        _safe_get(app, "api_key"),
        _safe_get(admin_config, "api_key"),
        _safe_get(admin_config, "app_key"),
        _safe_get(_safe_get(data, "app"), "api_key"),
        _safe_get(_safe_get(config, "app"), "api_key"),
    ]
    resolved: list[str] = []
    for candidate in candidates:
        resolved.extend(parse_secret_values(candidate))
    return unique_preserve_order(resolved)


async def fetch_admin_json_with_fallback(path: str, *, kind: str) -> tuple[dict[str, Any], str]:
    errors: list[str] = []
    for candidate in build_admin_path_candidates(path, kind=kind):
        try:
            payload = await fetch_admin_json(candidate)
            return payload, candidate
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError("; ".join(errors[:3]))


async def resolve_gateway_state(force: bool = False) -> dict[str, Any]:
    now = time.time()
    cached = state_cache.get("value")
    if not force and cached and state_cache.get("expires_at", 0) > now:
        return cached

    async with get_state_lock():
        now = time.time()
        cached = state_cache.get("value")
        if not force and cached and state_cache.get("expires_at", 0) > now:
            return cached

        has_admin_username = bool(ADMIN_USERNAME)
        has_admin_password = bool(ADMIN_PASSWORD)
        v3_admin_configured = has_admin_username and has_admin_password
        legacy_admin_configured = bool(ADMIN_APP_KEY)
        if v3_admin_configured:
            admin_auth_mode = "v3_credentials"
        elif has_admin_username or has_admin_password:
            admin_auth_mode = "v3_credentials_incomplete"
        elif legacy_admin_configured:
            admin_auth_mode = "legacy_app_key"
        else:
            admin_auth_mode = "not_configured"

        state = {
            "upstream_base_url": UPSTREAM_BASE_URL,
            "upstream_responses_path": UPSTREAM_RESPONSES_PATH,
            "admin_base_url": ADMIN_BASE_URL,
            "admin_verify_path": ADMIN_VERIFY_PATH,
            "admin_config_path": ADMIN_CONFIG_PATH,
            "admin_tokens_path": ADMIN_TOKENS_PATH,
            "manual_upstream_key": bool(UPSTREAM_API_KEY),
            "manual_gateway_token": bool(GATEWAY_TOKEN),
            "upstream_api_keys": parse_secret_values(UPSTREAM_API_KEY),
            "accepted_tokens": parse_secret_values(GATEWAY_TOKEN),
            "admin_api_keys": [],
            "resolved_upstream_api_key": "",
            "stats": build_empty_social_stats(),
            "admin_configured": bool(
                ADMIN_BASE_URL and (v3_admin_configured or legacy_admin_configured)
            ),
            "admin_connected": False,
            "admin_auth_mode": admin_auth_mode,
            "admin_api_version": "",
            "token_source": "not_configured",
            "error": "",
            "mode": "manual",
            # r7 A2: 用 current_*() 实时获取，跟随运行时 env 变更
            "model": current_model(),
            "fallback_model": current_fallback_model(),
            "fallback_min_results": FALLBACK_MIN_RESULTS,
        }

        if admin_auth_mode == "v3_credentials_incomplete":
            state["error"] = "grok2api v3 admin username and password must both be configured"
        elif v3_admin_configured and ADMIN_BASE_URL:
            try:
                admin_summary, admin_dashboard = await fetch_v3_admin_state()
                state["admin_connected"] = True
                state["admin_api_version"] = "v3"
                state["stats"] = build_v3_account_stats(admin_summary, admin_dashboard)
            except Exception as exc:
                state["error"] = str(exc)
        elif legacy_admin_configured and ADMIN_BASE_URL:
            try:
                (admin_config, resolved_config_path), (admin_tokens, resolved_tokens_path) = await asyncio.gather(
                    fetch_admin_json_with_fallback(ADMIN_CONFIG_PATH, kind="config"),
                    fetch_admin_json_with_fallback(ADMIN_TOKENS_PATH, kind="tokens"),
                )
                state["admin_config_path"] = resolved_config_path
                state["admin_tokens_path"] = resolved_tokens_path
                admin_api_keys = extract_admin_api_keys(admin_config)
                state["admin_connected"] = True
                state["admin_api_version"] = "v2"
                state["admin_api_keys"] = admin_api_keys
                if not state["upstream_api_keys"]:
                    state["upstream_api_keys"] = admin_api_keys
                if not state["accepted_tokens"]:
                    state["accepted_tokens"] = admin_api_keys
                state["stats"] = build_social_token_stats(admin_tokens)
            except Exception as exc:
                state["error"] = str(exc)

        if not state["accepted_tokens"] and state["upstream_api_keys"]:
            state["accepted_tokens"] = list(state["upstream_api_keys"])

        state["upstream_api_keys"] = unique_preserve_order(state["upstream_api_keys"])
        state["accepted_tokens"] = unique_preserve_order(state["accepted_tokens"])
        state["resolved_upstream_api_key"] = state["upstream_api_keys"][0] if state["upstream_api_keys"] else ""
        state["token_source"] = build_token_source(state)
        state["mode"] = build_gateway_mode(state)

        state_cache["value"] = state
        state_cache["expires_at"] = now + CACHE_TTL_SECONDS
        return state


def verify_gateway_token(token_value: str | None, accepted_tokens: list[str]) -> None:
    if not accepted_tokens:
        raise HTTPException(status_code=503, detail="Social gateway is not configured")
    if not token_value:
        raise HTTPException(status_code=401, detail="Missing API token")
    matched = False
    for expected in accepted_tokens:
        if hmac.compare_digest(token_value, expected):
            matched = True
    if not matched:
        raise HTTPException(status_code=401, detail="Invalid token")


def extract_response_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str) and payload.get("output_text").strip():
        return payload["output_text"].strip()

    parts: list[str] = []
    for item in payload.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content") or []
        if isinstance(content, str) and content.strip():
            parts.append(content.strip())
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
                continue
            if isinstance(text, dict) and isinstance(text.get("value"), str) and text["value"].strip():
                parts.append(text["value"].strip())
    return "\n".join(parts).strip()


def extract_json_object(text: str) -> dict[str, Any]:
    candidates: list[str] = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)

    fenced = re.findall(r"```(?:json)?\s*(.*?)```", stripped, flags=re.DOTALL | re.IGNORECASE)
    candidates.extend(item.strip() for item in fenced if item.strip())

    decoder = json.JSONDecoder()
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = json.loads(candidate)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            return parsed

        start = candidate.find("{")
        while start != -1:
            try:
                parsed, _ = decoder.raw_decode(candidate[start:])
            except Exception:
                start = candidate.find("{", start + 1)
                continue
            if isinstance(parsed, dict):
                return parsed
            start = candidate.find("{", start + 1)
    return {}


def normalize_citation(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    url = item.get("url") or item.get("target_url") or item.get("link") or item.get("source_url") or ""
    title = (
        item.get("title")
        or item.get("source_title")
        or item.get("display_text")
        or item.get("text")
        or ""
    )
    if not url and not title:
        return None
    normalized = dict(item)
    normalized["url"] = url
    normalized["title"] = title
    return normalized


def extract_upstream_citations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_citations = payload.get("citations") or []
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()

    if isinstance(raw_citations, list):
        for item in raw_citations:
            citation = normalize_citation(item)
            if citation is None:
                continue
            url = citation.get("url", "")
            if url and url in seen:
                continue
            if url:
                seen.add(url)
            normalized.append(citation)

    if normalized:
        return normalized

    for output_item in payload.get("output", []) or []:
        if not isinstance(output_item, dict):
            continue
        content_items = output_item.get("content") or []
        if not isinstance(content_items, list):
            continue
        for content_item in content_items:
            if not isinstance(content_item, dict):
                continue
            for annotation in content_item.get("annotations") or []:
                citation = normalize_citation(annotation)
                if citation is None:
                    continue
                url = citation.get("url", "")
                if url and url in seen:
                    continue
                if url:
                    seen.add(url)
                normalized.append(citation)
    return normalized


def normalize_result_item(item: Any) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None
    url = (item.get("url") or item.get("link") or "").strip()
    title = (item.get("title") or item.get("author") or item.get("handle") or url).strip()
    text = (
        item.get("text")
        or item.get("content")
        or item.get("body")
        or item.get("snippet")
        or item.get("summary")
        or ""
    ).strip()
    result = {
        "title": title,
        "url": url,
        "text": text,
        "content": (item.get("content") or text).strip(),
        "snippet": (item.get("snippet") or item.get("summary") or text).strip(),
        "author": (item.get("author") or item.get("username") or item.get("handle") or "").strip(),
        "handle": (item.get("handle") or item.get("username") or "").strip().lstrip("@"),
        "created_at": (item.get("created_at") or item.get("published_at") or "").strip(),
        "why_relevant": (item.get("why_relevant") or item.get("reason") or "").strip(),
    }
    if not result["url"] and not result["title"] and not result["text"]:
        return None
    return result


SOCIAL_HOST_ALIASES = {
    "x.com",
    "www.x.com",
    "twitter.com",
    "www.twitter.com",
    "mobile.x.com",
    "mobile.twitter.com",
}


def looks_synthetic_social_status_id(status_id: str) -> bool:
    digits = (status_id or "").strip()
    if len(digits) < 12 or not digits.isdigit():
        return False

    repeated_sequences = [
        "0123456789" * 4,
        "1234567890" * 4,
        "9876543210" * 4,
        "0987654321" * 4,
        "".join(f"{i}{i}" for i in range(10)) * 3,
        "".join(f"{i}{i}" for i in range(9, -1, -1)) * 3,
    ]
    for sequence in repeated_sequences:
        if digits in sequence or digits[:-1] in sequence:
            return True

    for size in range(1, 5):
        pattern = digits[:size]
        if pattern and (pattern * ((len(digits) // size) + 1))[: len(digits)] == digits:
            return True
    return False


def normalize_social_match_url(url: str) -> str:
    raw_url = (url or "").strip()
    if not raw_url:
        return ""
    try:
        parsed = urlparse(raw_url)
    except Exception:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""

    host = parsed.netloc.lower()
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/")
    if not path:
        path = "/"

    if host not in SOCIAL_HOST_ALIASES:
        return ""

    parts = [part for part in path.split("/") if part]
    if len(parts) < 3 or parts[1].lower() != "status" or not parts[2].isdigit():
        return ""
    if looks_synthetic_social_status_id(parts[2]):
        return ""
    handle = parts[0].lstrip("@").lower()
    return f"https://x.com/{handle}/status/{parts[2]}"


def is_supported_social_result_url(url: str) -> bool:
    return bool(normalize_social_match_url(url))


def build_trusted_social_citations(payload: dict[str, Any]) -> list[dict[str, str]]:
    citations: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in extract_upstream_citations(payload):
        url = (item.get("url") or "").strip()
        match_url = normalize_social_match_url(url)
        if not match_url or match_url in seen:
            continue
        seen.add(match_url)
        citations.append(
            {
                "title": (item.get("title") or "").strip(),
                "url": url,
                "match_url": match_url,
            }
        )
    return citations


def build_social_result(
    citation: dict[str, str] | None = None,
    matched: dict[str, str] | None = None,
) -> dict[str, str]:
    citation = citation or {}
    matched = matched or {}
    url = (citation.get("url") or matched.get("url") or "").strip()
    title = (
        citation.get("title")
        or matched.get("title")
        or matched.get("author")
        or matched.get("handle")
        or url
    ).strip()
    text = (matched.get("text") or "").strip()
    content = (matched.get("content") or text).strip()
    snippet = (matched.get("snippet") or text).strip()
    author = (matched.get("author") or "").strip()
    handle = (matched.get("handle") or "").strip().lstrip("@")
    created_at = (matched.get("created_at") or "").strip()
    why_relevant = (matched.get("why_relevant") or "").strip()
    return {
        "title": title,
        "url": url,
        "text": text,
        "content": content,
        "snippet": snippet,
        "author": author,
        "handle": handle,
        "created_at": created_at,
        "why_relevant": why_relevant,
    }


def build_upstream_payload(body: dict[str, Any], model: str | None = None) -> tuple[dict[str, Any], int]:
    query = str(body.get("query") or "").strip()
    try:
        max_results = max(1, min(int(body.get("max_results") or 5), 10))
    except (TypeError, ValueError):
        max_results = 5
    tools: list[dict[str, Any]] = [{"type": "x_search"}]
    tool = tools[0]
    if body.get("allowed_x_handles"):
        tool["allowed_x_handles"] = body["allowed_x_handles"]
    if body.get("excluded_x_handles"):
        tool["excluded_x_handles"] = body["excluded_x_handles"]
    if body.get("from_date"):
        tool["from_date"] = body["from_date"]
    if body.get("to_date"):
        tool["to_date"] = body["to_date"]
    if body.get("include_x_images"):
        tool["enable_image_understanding"] = True
    if body.get("include_x_videos"):
        tool["enable_video_understanding"] = True

    prompt = (
        "Use x_search to find relevant X posts.\n"
        f"Query: {query}\n"
        f'Return JSON only with this schema and no markdown: {{"answer": string, "results": [{{"title": string, '
        f'"url": string, "text": string, "author": string, "handle": string, "created_at": string, '
        f'"why_relevant": string}}]}}.\n'
        f"Return up to {max_results} results. Prefer direct x.com status URLs. "
        "Use empty strings for unknown fields."
    )
    # r7 A2: 默认值实时解析；显式 `model` 入参（per-request override）仍优先。
    _model_default = current_model()
    return (
        {
            "model": (model or _model_default).strip() or _model_default,
            "input": [{"role": "user", "content": prompt}],
            "tools": tools,
            "temperature": 0,
            "store": False,
            "stream": False,
        },
        max_results,
    )


def build_social_search_upstream_payload(body: dict[str, Any]) -> dict[str, Any]:
    payload, _ = build_upstream_payload(body)
    return payload


def count_social_results(payload: dict[str, Any] | None) -> int:
    return len((payload or {}).get("results") or [])


def count_social_citations(payload: dict[str, Any] | None) -> int:
    return len((payload or {}).get("citations") or [])


def redact_secret_text(value: Any, *secrets: Any) -> str:
    text = str(value or "")
    for secret in secrets:
        normalized = str(secret or "")
        if normalized:
            text = text.replace(normalized, "<redacted>")
    return text


def _social_key_fingerprint(key: str) -> str:
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()


def _classify_social_key_failure(status_code: int, detail: str = "") -> str:
    normalized = " ".join(str(detail or "").lower().split())
    quota_markers = (
        "quota_exhausted", "quota exhausted", "insufficient_quota", "insufficient quota",
        "credits exhausted", "credit exhausted", "credits limit", "credit limit",
        "exceeded your credits", "no credits remaining", "billing limit", "usage limit",
        "plan limit", "resource_exhausted",
    )
    auth_markers = (
        "invalid api key", "invalid_api_key", "api key is invalid", "api key has expired",
        "expired api key", "revoked api key", "invalid token", "token is invalid",
        "token has expired", "expired token", "revoked token", "bad credentials",
        "authentication failed",
    )
    has_quota_marker = any(marker in normalized for marker in quota_markers)
    if status_code in {402, 432} or (status_code in {403, 429} and has_quota_marker):
        return "quota_exhausted"
    if status_code == 429:
        return "rate_limited"
    if status_code == 401 or (status_code == 403 and any(marker in normalized for marker in auth_markers)):
        return "auth_rejected"
    return ""


def _parse_retry_after_header(headers: Any) -> int | None:
    if headers is None or not hasattr(headers, "get"):
        return None
    raw_retry_after = str(headers.get("retry-after") or "").strip()
    if not raw_retry_after:
        return None
    try:
        return max(1, min(86400, math.ceil(float(raw_retry_after))))
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        retry_at = parsedate_to_datetime(raw_retry_after)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(
            1,
            min(86400, math.ceil((retry_at - datetime.now(timezone.utc)).total_seconds())),
        )
    except (TypeError, ValueError, OverflowError):
        return None


def _available_social_upstream_keys(keys: list[str] | None) -> list[str]:
    now = time.time()
    available: list[str] = []
    with social_upstream_key_lock:
        for key in unique_preserve_order(keys or []):
            fingerprint = _social_key_fingerprint(key)
            schedule = social_upstream_key_schedule.get(fingerprint)
            if schedule and schedule.get("until") is not None:
                try:
                    if float(schedule["until"]) <= now:
                        social_upstream_key_schedule.pop(fingerprint, None)
                        schedule = None
                except (TypeError, ValueError):
                    social_upstream_key_schedule.pop(fingerprint, None)
                    schedule = None
            if schedule is None:
                available.append(key)
    return available


def _ordered_social_upstream_keys(keys: list[str] | None) -> list[str]:
    global social_upstream_key_cursor
    available = _available_social_upstream_keys(keys)
    if len(available) <= 1:
        return available
    with social_upstream_key_lock:
        start = social_upstream_key_cursor % len(available)
        social_upstream_key_cursor = (start + 1) % len(available)
    return available[start:] + available[:start]


def _schedule_social_upstream_key(
    key: str,
    failure_kind: str,
    retry_after_seconds: int | None = None,
) -> None:
    with social_upstream_key_lock:
        social_upstream_key_schedule[_social_key_fingerprint(key)] = {
            "reason": failure_kind,
            "until": (
                time.time() + max(1, int(retry_after_seconds or 60))
                if failure_kind == "rate_limited"
                else None
            ),
        }


def _social_upstream_pool_failure(keys: list[str] | None) -> tuple[str, int | None]:
    fingerprints = {_social_key_fingerprint(key) for key in unique_preserve_order(keys or [])}
    now = time.time()
    with social_upstream_key_lock:
        states = [
            state
            for fingerprint, state in social_upstream_key_schedule.items()
            if fingerprint in fingerprints
        ]
    delays = [
        max(1, math.ceil(float(state["until"]) - now))
        for state in states
        if state.get("reason") == "rate_limited" and state.get("until") is not None
    ]
    if delays:
        return "rate_limited", min(delays)
    terminal = [
        str(state.get("reason") or "")
        for state in states
        if state.get("reason") in {"quota_exhausted", "auth_rejected"}
    ]
    return (terminal[0], None) if terminal else ("", None)


def build_social_attempt_summary(
    model: str,
    ok: bool,
    *,
    response: dict[str, Any] | None = None,
    error: str = "",
    status_code: int | None = None,
    latency_ms: int | None = None,
    failure_kind: str = "",
    retry_after_seconds: int | None = None,
) -> dict[str, Any]:
    attempt: dict[str, Any] = {
        "model": model,
        "ok": bool(ok),
        "status_code": status_code,
        "result_count": count_social_results(response),
        "citation_count": count_social_citations(response),
    }
    if latency_ms is not None:
        attempt["latency_ms"] = latency_ms
    if failure_kind:
        attempt["failure_kind"] = failure_kind
    if retry_after_seconds is not None:
        attempt["retry_after_seconds"] = retry_after_seconds
    if error:
        attempt["error"] = error
    if response is not None:
        attempt["response"] = response
    return attempt


def has_social_fallback(primary_model: str, fallback_model: str) -> bool:
    primary = (primary_model or "").strip()
    fallback = (fallback_model or "").strip()
    return bool(primary and fallback and fallback != primary)


def effective_social_fallback_threshold(min_results: int, max_results: int) -> int:
    try:
        configured = max(1, int(min_results or 1))
    except (TypeError, ValueError):
        configured = 1
    try:
        requested = max(1, int(max_results or 1))
    except (TypeError, ValueError):
        requested = 1
    return min(configured, requested)


def should_retry_social_with_fallback(
    primary_model: str,
    fallback_model: str,
    response: dict[str, Any] | None,
    min_results: int,
    max_results: int,
) -> tuple[bool, str]:
    if not has_social_fallback(primary_model, fallback_model):
        return False, ""
    threshold = effective_social_fallback_threshold(min_results, max_results)
    if count_social_results(response) >= threshold:
        return False, ""
    return True, "result_count_below_threshold"


def choose_preferred_social_attempt(
    primary_attempt: dict[str, Any] | None,
    fallback_attempt: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not fallback_attempt or not fallback_attempt.get("ok"):
        return primary_attempt
    if not primary_attempt or not primary_attempt.get("ok"):
        return fallback_attempt

    primary_count = int(primary_attempt.get("result_count") or 0)
    fallback_count = int(fallback_attempt.get("result_count") or 0)
    if fallback_count > primary_count:
        return fallback_attempt

    primary_citations = int(primary_attempt.get("citation_count") or 0)
    fallback_citations = int(fallback_attempt.get("citation_count") or 0)
    if fallback_count == primary_count and fallback_citations > primary_citations:
        return fallback_attempt

    return primary_attempt


def build_social_route_metadata(
    selected_attempt: dict[str, Any] | None,
    attempts: list[dict[str, Any]],
    *,
    fallback_model: str,
    fallback_reason: str,
    fallback_min_results: int,
    requested_max_results: int,
) -> dict[str, Any]:
    primary_model = attempts[0]["model"] if attempts else ""
    selected_model = (selected_attempt or {}).get("model") or primary_model
    route_attempts: list[dict[str, Any]] = []
    for item in attempts:
        route_item: dict[str, Any] = {
            "model": item.get("model", ""),
            "ok": bool(item.get("ok")),
            "status_code": item.get("status_code"),
            "result_count": int(item.get("result_count") or 0),
            "citation_count": int(item.get("citation_count") or 0),
        }
        if item.get("latency_ms") is not None:
            route_item["latency_ms"] = item["latency_ms"]
        if item.get("failure_kind"):
            route_item["failure_kind"] = item["failure_kind"]
        if item.get("retry_after_seconds") is not None:
            route_item["retry_after_seconds"] = item["retry_after_seconds"]
        if item.get("error"):
            route_item["error"] = item["error"]
        route_attempts.append(route_item)

    fallback_attempted = len(attempts) > 1
    fallback_target = attempts[1]["model"] if fallback_attempted else (fallback_model or "").strip()
    return {
        "selected_model": selected_model,
        "attempt_count": len(attempts),
        "attempts": route_attempts,
        "fallback": {
            "configured": has_social_fallback(primary_model, fallback_model),
            "triggered": fallback_attempted,
            "used": bool(fallback_attempted and selected_model == fallback_target),
            "reason": fallback_reason or "",
            "threshold": effective_social_fallback_threshold(
                fallback_min_results,
                requested_max_results,
            ),
            "from": primary_model,
            "to": fallback_target,
            "selected_model": selected_model,
        },
    }


def attach_social_route_metadata(
    response: dict[str, Any] | None,
    selected_attempt: dict[str, Any] | None,
    attempts: list[dict[str, Any]],
    *,
    fallback_model: str,
    fallback_reason: str,
    fallback_min_results: int,
    requested_max_results: int,
) -> dict[str, Any]:
    payload = dict(response or {})
    tool_usage = dict(payload.get("tool_usage") or {})
    tool_usage["social_search_calls"] = len(attempts)
    tool_usage["model"] = (selected_attempt or {}).get("model") or tool_usage.get("model") or ""
    payload["tool_usage"] = tool_usage
    payload["route"] = build_social_route_metadata(
        selected_attempt,
        attempts,
        fallback_model=fallback_model,
        fallback_reason=fallback_reason,
        fallback_min_results=fallback_min_results,
        requested_max_results=requested_max_results,
    )
    return payload


def extract_social_upstream_error(
    upstream_body: dict[str, Any] | Any,
    fallback_detail: str = "Social search failed",
    *secrets: Any,
) -> str:
    detail = ""
    if isinstance(upstream_body, dict):
        error = upstream_body.get("error") or {}
        if isinstance(error, dict):
            detail = error.get("message") or ""
        if not detail:
            detail = upstream_body.get("detail") or ""
    if not detail:
        detail = fallback_detail
    return redact_secret_text(detail, *secrets)[:300]


async def execute_social_search_attempt(
    query: str,
    body: dict[str, Any],
    state: dict[str, Any],
    model: str,
    max_results: int,
) -> dict[str, Any]:
    upstream_payload, _ = build_upstream_payload(body, model=model)
    configured_keys = state.get("upstream_api_keys") or [state.get("resolved_upstream_api_key")]
    upstream_keys = _ordered_social_upstream_keys(configured_keys)
    if not upstream_keys:
        failure_kind, retry_after_seconds = _social_upstream_pool_failure(configured_keys)
        if failure_kind == "rate_limited":
            return build_social_attempt_summary(
                model,
                False,
                error="All social upstream API keys are rate limited",
                status_code=429,
                latency_ms=0,
                failure_kind=failure_kind,
                retry_after_seconds=retry_after_seconds,
            )
        return build_social_attempt_summary(
            model,
            False,
            error="All social upstream API keys are unavailable; manual key action required",
            status_code=503,
            latency_ms=0,
            failure_kind=failure_kind,
        )

    last_key_failure: dict[str, Any] | None = None
    last_key_failure_kind = ""
    rate_limit_failure: dict[str, Any] | None = None
    for upstream_key in upstream_keys:
        start = time.monotonic()
        try:
            response = await http_client.post(
                f"{state['upstream_base_url']}{state['upstream_responses_path']}",
                json=upstream_payload,
                headers={"Authorization": f"Bearer {upstream_key}"},
            )
        except Exception:
            latency_ms = int((time.monotonic() - start) * 1000)
            logger.warning("social search upstream request failed")
            return build_social_attempt_summary(
                model,
                False,
                error="upstream request failed",
                status_code=502,
                latency_ms=latency_ms,
            )

        latency_ms = int((time.monotonic() - start) * 1000)
        try:
            upstream_body = response.json()
        except Exception:
            upstream_body = None

        if response.status_code >= 400:
            safe_error = extract_social_upstream_error(
                upstream_body,
                getattr(response, "text", "").strip(),
                *configured_keys,
                UPSTREAM_API_KEY,
                GATEWAY_TOKEN,
                ADMIN_APP_KEY,
                ADMIN_PASSWORD,
                *state.get("accepted_tokens", []),
            )
            classification_detail = redact_secret_text(
                json.dumps(upstream_body, ensure_ascii=False)
                if isinstance(upstream_body, (dict, list))
                else safe_error,
                *configured_keys,
                UPSTREAM_API_KEY,
                GATEWAY_TOKEN,
                ADMIN_APP_KEY,
                ADMIN_PASSWORD,
                *state.get("accepted_tokens", []),
            )
            failure_kind = _classify_social_key_failure(response.status_code, classification_detail)
            retry_after_seconds = _parse_retry_after_header(getattr(response, "headers", None))
            if failure_kind == "rate_limited" and retry_after_seconds is None:
                retry_after_seconds = 60
            logger.warning("social upstream error %s: %s", response.status_code, safe_error)
            attempt = build_social_attempt_summary(
                model,
                False,
                error=safe_error or f"Upstream returned {response.status_code}",
                status_code=response.status_code,
                latency_ms=latency_ms,
                failure_kind=failure_kind,
                retry_after_seconds=retry_after_seconds,
            )
            if failure_kind:
                _schedule_social_upstream_key(upstream_key, failure_kind, retry_after_seconds)
                last_key_failure = attempt
                last_key_failure_kind = failure_kind
                if failure_kind == "rate_limited" and (
                    rate_limit_failure is None
                    or int(attempt.get("retry_after_seconds") or 60)
                    < int(rate_limit_failure.get("retry_after_seconds") or 60)
                ):
                    rate_limit_failure = attempt
                continue
            return attempt

        if upstream_body is None:
            logger.warning("social upstream non-JSON response")
            return build_social_attempt_summary(
                model,
                False,
                error="Upstream returned non-JSON",
                status_code=502,
                latency_ms=latency_ms,
            )
        break
    else:
        if rate_limit_failure is not None:
            return rate_limit_failure
        if last_key_failure is not None:
            if last_key_failure_kind in {"auth_rejected", "quota_exhausted"}:
                last_key_failure = dict(last_key_failure)
                last_key_failure["status_code"] = 503
                last_key_failure["error"] = (
                    "Social upstream API key pool is unavailable; manual key action required"
                )
            return last_key_failure
        return build_social_attempt_summary(
            model,
            False,
            error="All social upstream API keys are unavailable; manual key action required",
            status_code=503,
            latency_ms=0,
        )

    if not isinstance(upstream_body, dict):
        return build_social_attempt_summary(
            model,
            False,
            error="Upstream returned non-dict JSON response",
            status_code=502,
            latency_ms=latency_ms,
        )

    normalized = normalize_search_response(
        query,
        upstream_body,
        max_results,
        model=model,
    )
    return build_social_attempt_summary(
        model,
        True,
        response=normalized,
        status_code=response.status_code,
        latency_ms=latency_ms,
    )


def normalize_search_response(
    query: str,
    payload: dict[str, Any],
    max_results: int,
    *,
    model: str | None = None,
) -> dict[str, Any]:
    text = extract_response_text(payload)
    structured = extract_json_object(text)
    parsed_results = structured.get("results") if isinstance(structured, dict) else []
    answer = (structured.get("answer") or "").strip() if isinstance(structured, dict) else ""
    if not answer:
        answer = text

    trusted_citations = build_trusted_social_citations(payload)
    trusted_map = {item["match_url"]: item for item in trusted_citations}
    matched_results: dict[str, dict[str, str]] = {}
    fallback_results: list[dict[str, str]] = []
    seen_fallback: set[str] = set()

    for item in parsed_results or []:
        normalized = normalize_result_item(item)
        if normalized is None:
            continue
        match_url = normalize_social_match_url(normalized.get("url", ""))
        if trusted_map:
            if match_url and match_url in trusted_map and match_url not in matched_results:
                matched_results[match_url] = normalized
            continue
        if not match_url or match_url in seen_fallback:
            continue
        seen_fallback.add(match_url)
        fallback_results.append(normalized)
        if len(fallback_results) >= max_results:
            break

    if trusted_citations:
        citations = [
            {"title": item.get("title", ""), "url": item.get("url", "")}
            for item in trusted_citations[:max_results]
        ]
        results = [
            build_social_result(citation=item, matched=matched_results.get(item["match_url"]))
            for item in trusted_citations[:max_results]
        ]
    else:
        results = fallback_results[:max_results]
        citations = [
            {"title": item.get("title", ""), "url": item.get("url", "")}
            for item in results
            if is_supported_social_result_url(item.get("url", ""))
        ]

    return {
        "query": query,
        "answer": answer,
        "results": results,
        "citations": citations,
        "tool_usage": {
            "social_search_calls": 1,
            "model": model or payload.get("model") or current_model(),
        },
    }


def normalize_social_search_response(
    query: str,
    payload: dict[str, Any],
    max_results: int,
    *,
    model: str | None = None,
) -> dict[str, Any]:
    return normalize_search_response(query, payload, max_results, model=model)


def social_attempt_http_exception(attempt: dict[str, Any]) -> HTTPException:
    status_code = max(400, int(attempt.get("status_code") or 502))
    headers = None
    retry_after_seconds = attempt.get("retry_after_seconds")
    if status_code == 429 and retry_after_seconds is not None:
        headers = {"Retry-After": str(max(1, int(retry_after_seconds)))}
    return HTTPException(
        status_code=status_code,
        detail=attempt.get("error") or "Social search failed",
        headers=headers,
    )


async def _build_health_payload() -> dict[str, Any]:
    state = await resolve_gateway_state(force=False)
    configured_keys = state.get("upstream_api_keys") or [state.get("resolved_upstream_api_key")]
    configured_keys = unique_preserve_order(configured_keys)
    available_keys = _available_social_upstream_keys(configured_keys)
    return {
        "ok": bool(available_keys and state["accepted_tokens"]),
        "mode": state["mode"],
        "upstream_base_url": state["upstream_base_url"],
        "upstream_responses_path": state["upstream_responses_path"],
        "admin_base_url": state["admin_base_url"],
        "admin_verify_path": state["admin_verify_path"],
        "admin_config_path": state["admin_config_path"],
        "admin_tokens_path": state["admin_tokens_path"],
        "model": state["model"],
        "fallback_model": state["fallback_model"],
        "fallback_min_results": state["fallback_min_results"],
        "token_source": state["token_source"],
        "admin_configured": state["admin_configured"],
        "admin_connected": state["admin_connected"],
        "admin_auth_mode": state["admin_auth_mode"],
        "admin_api_version": state["admin_api_version"],
        "accepted_token_count": len(state["accepted_tokens"]),
        "upstream_api_key_count": len(state["upstream_api_keys"]),
        "upstream_available_key_count": len(available_keys),
        "upstream_unavailable_key_count": max(0, len(configured_keys) - len(available_keys)),
        "token_configured": bool(state["accepted_tokens"]),
        "upstream_key_configured": bool(state["resolved_upstream_api_key"]),
        "stats": state["stats"],
        "error": "Social gateway configuration requires attention" if state["error"] else "",
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    return await _build_health_payload()


@app.get("/social/health")
async def social_health() -> dict[str, Any]:
    return await _build_health_payload()


@app.post("/social/search")
async def social_search(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Expected JSON request body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected JSON request body")

    query = str(body.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Missing query")

    source = (body.get("source") or "x").strip().lower()
    if source != "x":
        raise HTTPException(status_code=400, detail="Only source=x is supported")

    state = await resolve_gateway_state(force=False)
    token_value = extract_token(request, body)
    verify_gateway_token(token_value, state["accepted_tokens"])
    if not state.get("upstream_api_keys") and not state.get("resolved_upstream_api_key"):
        raise HTTPException(status_code=503, detail="Missing social upstream API key")
    _, max_results = build_upstream_payload(body)
    attempts = []
    primary_model = str(body.get("model") or state["model"]).strip() or state["model"]
    primary_attempt = await execute_social_search_attempt(
        query,
        body,
        state,
        primary_model,
        max_results,
    )
    attempts.append(primary_attempt)

    fallback_model = state.get("fallback_model", "")
    fallback_min_results = state.get("fallback_min_results", FALLBACK_MIN_RESULTS)
    fallback_reason = ""

    if primary_attempt.get("ok"):
        selected_attempt = primary_attempt
        should_retry, fallback_reason = should_retry_social_with_fallback(
            primary_model,
            fallback_model,
            primary_attempt.get("response"),
            fallback_min_results,
            max_results,
        )
        if should_retry:
            fallback_attempt = await execute_social_search_attempt(
                query,
                body,
                state,
                fallback_model,
                max_results,
            )
            attempts.append(fallback_attempt)
            selected_attempt = choose_preferred_social_attempt(primary_attempt, fallback_attempt)

        return attach_social_route_metadata(
            selected_attempt.get("response"),
            selected_attempt,
            attempts,
            fallback_model=fallback_model,
            fallback_reason=fallback_reason,
            fallback_min_results=fallback_min_results,
            requested_max_results=max_results,
        )

    if has_social_fallback(primary_model, fallback_model) and not primary_attempt.get("failure_kind"):
        fallback_reason = "upstream_error"
        fallback_attempt = await execute_social_search_attempt(
            query,
            body,
            state,
            fallback_model,
            max_results,
        )
        attempts.append(fallback_attempt)
        if fallback_attempt.get("ok"):
            return attach_social_route_metadata(
                fallback_attempt.get("response"),
                fallback_attempt,
                attempts,
                fallback_model=fallback_model,
                fallback_reason=fallback_reason,
                fallback_min_results=fallback_min_results,
                requested_max_results=max_results,
            )
        raise social_attempt_http_exception(fallback_attempt)

    raise social_attempt_http_exception(primary_attempt)


def main() -> None:
    host = _env_str("SOCIAL_GATEWAY_HOST", "127.0.0.1")
    try:
        port = int(_env_str("SOCIAL_GATEWAY_PORT", "9875"))
    except (TypeError, ValueError):
        port = 9875
    uvicorn.run("mysearch.social_gateway:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
