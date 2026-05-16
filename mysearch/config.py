"""MySearch 通用配置。"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass as _dataclass, field
from pathlib import Path
from typing import Literal

try:
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - py310 fallback
    tomllib = None  # type: ignore[assignment]


def dataclass(*args, **kwargs):
    if sys.version_info < (3, 10):
        kwargs.pop("slots", None)
    return _dataclass(*args, **kwargs)


MODULE_DIR = Path(__file__).resolve().parent
ROOT_DIR = MODULE_DIR.parent
AuthMode = Literal["bearer", "body"]
TavilyMode = Literal["official", "gateway"]
XAISearchMode = Literal["official", "compatible"]
MCPTransport = Literal["stdio", "sse", "streamable-http"]


def _load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] == value[-1:] and value[:1] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _load_mapping_env(raw_env: dict[str, object]) -> None:
    for key, value in raw_env.items():
        if not isinstance(value, str):
            continue
        cleaned = value.strip()
        if not cleaned:
            continue
        os.environ.setdefault(key, cleaned)


def _parse_codex_mysearch_env(config_text: str) -> dict[str, str]:
    if tomllib is not None:
        try:
            data = tomllib.loads(config_text)
            env = ((data.get("mcp_servers") or {}).get("mysearch") or {}).get("env") or {}
            if isinstance(env, dict):
                return {
                    key: value.strip()
                    for key, value in env.items()
                    if isinstance(value, str) and value.strip()
                }
        except Exception:
            pass

    env: dict[str, str] = {}
    in_section = False
    for raw_line in config_text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            in_section = line == "[mcp_servers.mysearch.env]"
            continue
        if not in_section or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] == value[-1:] and value[:1] in {"'", '"'}:
            value = value[1:-1]
        if key and value:
            env[key] = value
    return env


def _load_codex_mcp_env() -> None:
    config_path = Path(os.getenv("CODEX_HOME", "~/.codex")).expanduser() / "config.toml"
    if not config_path.exists():
        return

    try:
        env = _parse_codex_mysearch_env(config_path.read_text(encoding="utf-8"))
    except Exception:
        return

    _load_mapping_env(env)


def _load_dotenv() -> None:
    # .env 只作为本地单仓调试兜底，不覆盖宿主已注入的配置。
    for env_path in (MODULE_DIR / ".env", ROOT_DIR / ".env"):
        _load_env_file(env_path)


def _bootstrap_runtime_env() -> None:
    _load_codex_mcp_env()
    _load_dotenv()


def _get_str(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except (ValueError, TypeError):
        return default


def _get_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_list(*names: str) -> list[str]:
    for name in names:
        value = os.getenv(name)
        if value is None or not value.strip():
            continue
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


@dataclass(slots=True, frozen=True)
class GrokModelSpec:
    """单个 Grok 模型登记项。

    `source` = `"builtin"` 表示来自项目内置默认清单（与上游 chenyme/grok2api
    basic 层一致），`"user"` 表示来自环境变量自定义。`tier` 仅作展示用，
    不参与请求校验——用户每次请求传入的 `model` 仍会原样透传给上游。
    """

    id: str
    tier: str = "custom"
    source: str = "user"


_BUILTIN_GROK_MODELS: tuple[GrokModelSpec, ...] = (
    GrokModelSpec(id="grok-4.20-fast", tier="basic", source="builtin"),
    GrokModelSpec(id="grok-4.20-0309-non-reasoning", tier="basic", source="builtin"),
    GrokModelSpec(id="grok-4.3-beta", tier="basic", source="builtin"),
)


# 模型 ID 允许的字符集：字母数字、点、下划线、冒号、连字符、斜杠；长度上限 128 字节。
# 拦截换行 / 引号 / 空格中间字符，避免运维误把含特殊字符的 env 注入到日志与
# `mysearch_health.known_grok_models` 返回体里造成污染。
_GROK_MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:/\-]{1,128}$")
# 条目数上限：防止 env-driven 超长清单造成 startup 阶段内存压力。
_MAX_GROK_MODEL_ENTRIES = 256


def _sanitize_grok_model_ids(raw: list[str]) -> list[str]:
    """过滤非法 ID，保序去重，截断到上限。

    - 不符合 `_GROK_MODEL_ID_PATTERN` 的条目被静默跳过。
    - 去重区分大小写（与上游 grok2api 保持一致）。
    - 超过 `_MAX_GROK_MODEL_ENTRIES` 的多余条目截断。
    """

    seen: set[str] = set()
    filtered: list[str] = []
    for item in raw:
        if len(filtered) >= _MAX_GROK_MODEL_ENTRIES:
            break
        cleaned = item.strip()
        if not cleaned or cleaned in seen:
            continue
        if not _GROK_MODEL_ID_PATTERN.match(cleaned):
            continue
        seen.add(cleaned)
        filtered.append(cleaned)
    return filtered


def _resolve_grok_models() -> tuple[GrokModelSpec, ...]:
    """合并内置 basic-tier 默认清单与用户自定义条目。

    - `MYSEARCH_GROK_MODELS`：逗号分隔；非空时**完全替换**内置清单。
    - `MYSEARCH_GROK_EXTRA_MODELS`：逗号分隔；在内置清单之后追加，重复 ID 去重。
    两个 env 均未设置时返回 `_BUILTIN_GROK_MODELS`。

    用户提供的 ID 会经过 `_sanitize_grok_model_ids` 过滤，非法字符或超长条目
    静默跳过；如果过滤后清单为空，回退到 `_BUILTIN_GROK_MODELS`。
    """

    override = _sanitize_grok_model_ids(_get_list("MYSEARCH_GROK_MODELS"))
    if override:
        return tuple(
            GrokModelSpec(id=mid, tier="custom", source="user") for mid in override
        )

    extras = _sanitize_grok_model_ids(_get_list("MYSEARCH_GROK_EXTRA_MODELS"))
    if not extras:
        return _BUILTIN_GROK_MODELS

    builtin_ids = {m.id for m in _BUILTIN_GROK_MODELS}
    appended = tuple(
        GrokModelSpec(id=mid, tier="custom", source="user")
        for mid in extras
        if mid not in builtin_ids
    )
    return _BUILTIN_GROK_MODELS + appended


def _normalize_base_url(url: str) -> str:
    return url.rstrip("/")


def _normalize_path(path: str) -> str:
    if not path:
        return ""
    if not path.startswith("/"):
        return f"/{path}"
    return path


def _resolve_path(*names: str, default_name: str | None = None) -> Path | None:
    raw = _get_str(*names)
    if raw:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = ROOT_DIR / candidate
        return candidate

    if default_name:
        candidate = ROOT_DIR / default_name
        if candidate.exists():
            return candidate
    return None


def _provider_base_url(
    *,
    explicit_names: tuple[str, ...],
    proxy_base_url: str,
    default: str,
) -> str:
    explicit = _get_str(*explicit_names)
    if explicit:
        return _normalize_base_url(explicit)
    if proxy_base_url:
        return _normalize_base_url(proxy_base_url)
    return _normalize_base_url(default)


def _provider_path(
    *,
    explicit_name: str,
    proxy_base_url: str,
    proxy_default: str,
    default: str,
) -> str:
    explicit = _get_str(explicit_name)
    if explicit:
        return _normalize_path(explicit)
    if proxy_base_url:
        return _normalize_path(proxy_default)
    return _normalize_path(default)


def _get_tavily_mode(proxy_base_url: str) -> TavilyMode:
    explicit = _get_str("MYSEARCH_TAVILY_MODE")
    if explicit:
        if explicit not in ("official", "gateway"):
            raise ValueError(
                f"MYSEARCH_TAVILY_MODE must be 'official' or 'gateway', got '{explicit}'"
            )
        return explicit  # type: ignore[return-value]
    if _get_str(
        "MYSEARCH_TAVILY_GATEWAY_BASE_URL",
        "MYSEARCH_TAVILY_GATEWAY_TOKEN",
        "MYSEARCH_TAVILY_GATEWAY_API_KEY",
    ) or _get_list("MYSEARCH_TAVILY_GATEWAY_TOKENS", "MYSEARCH_TAVILY_GATEWAY_API_KEYS"):
        return "gateway"
    return "gateway" if proxy_base_url else "official"


def _tavily_gateway_base_url(proxy_base_url: str, default: str) -> str:
    explicit = _get_str("MYSEARCH_TAVILY_GATEWAY_BASE_URL")
    if explicit:
        return _normalize_base_url(explicit)
    if proxy_base_url:
        return _normalize_base_url(proxy_base_url)
    return _normalize_base_url(default)


def _tavily_gateway_path(
    *,
    explicit_name: str,
    explicit_gateway_base_url: str,
    proxy_base_url: str,
    proxy_default: str,
    default: str,
) -> str:
    explicit = _get_str(explicit_name)
    if explicit:
        return _normalize_path(explicit)
    if explicit_gateway_base_url:
        return _normalize_path(default)
    if proxy_base_url:
        return _normalize_path(proxy_default)
    return _normalize_path(default)


_bootstrap_runtime_env()


@dataclass(slots=True)
class ProviderConfig:
    name: str
    base_url: str
    auth_mode: AuthMode
    auth_header: str
    auth_scheme: str
    auth_field: str
    default_paths: dict[str, str]
    alternate_base_urls: dict[str, str] = field(default_factory=dict)
    provider_mode: str = ""
    search_mode: XAISearchMode = "official"
    api_keys: list[str] = field(default_factory=list)
    keys_file: Path | None = None

    def path(self, key: str) -> str:
        return self.default_paths.get(key, "")

    def base_url_for(self, key: str) -> str:
        return self.alternate_base_urls.get(key) or self.base_url


@dataclass(slots=True)
class MySearchConfig:
    server_name: str
    timeout_seconds: int
    xai_social_timeout_seconds: int
    xai_model: str
    xai_models: tuple[GrokModelSpec, ...]
    max_parallel_workers: int
    search_cache_ttl_seconds: int
    extract_cache_ttl_seconds: int
    mcp_host: str
    mcp_port: int
    mcp_mount_path: str
    mcp_sse_path: str
    mcp_streamable_http_path: str
    mcp_stateless_http: bool
    tavily: ProviderConfig
    firecrawl: ProviderConfig
    exa: ProviderConfig
    xai: ProviderConfig

    @classmethod
    def from_env(cls) -> "MySearchConfig":
        proxy_base_url = _get_str("MYSEARCH_PROXY_BASE_URL")
        proxy_api_key = _get_str("MYSEARCH_PROXY_API_KEY")
        tavily_mode = _get_tavily_mode(proxy_base_url)
        tavily_gateway_base_url = _get_str("MYSEARCH_TAVILY_GATEWAY_BASE_URL")
        tavily_gateway_token = _get_str(
            "MYSEARCH_TAVILY_GATEWAY_TOKEN",
            "MYSEARCH_TAVILY_GATEWAY_API_KEY",
        )
        return cls(
            server_name=_get_str("MYSEARCH_NAME", "MYSEARCH_SERVER_NAME", default="MySearch"),
            timeout_seconds=_get_int("MYSEARCH_TIMEOUT_SECONDS", 45),
            xai_social_timeout_seconds=max(30, _get_int("MYSEARCH_XAI_SOCIAL_TIMEOUT_SECONDS", 120)),
            xai_model=_get_str(
                "MYSEARCH_XAI_MODEL",
                default="grok-4.20-fast",
            ),
            xai_models=_resolve_grok_models(),
            max_parallel_workers=max(1, _get_int("MYSEARCH_MAX_PARALLEL_WORKERS", 4)),
            search_cache_ttl_seconds=max(0, _get_int("MYSEARCH_SEARCH_CACHE_TTL_SECONDS", 120)),
            extract_cache_ttl_seconds=max(0, _get_int("MYSEARCH_EXTRACT_CACHE_TTL_SECONDS", 300)),
            mcp_host=_get_str("MYSEARCH_MCP_HOST", default="127.0.0.1"),
            mcp_port=_get_int("MYSEARCH_MCP_PORT", 8000),
            mcp_mount_path=_normalize_path(_get_str("MYSEARCH_MCP_MOUNT_PATH", default="/")),
            mcp_sse_path=_normalize_path(_get_str("MYSEARCH_MCP_SSE_PATH", default="/sse")),
            mcp_streamable_http_path=_normalize_path(
                _get_str("MYSEARCH_MCP_STREAMABLE_HTTP_PATH", default="/mcp")
            ),
            mcp_stateless_http=_get_bool("MYSEARCH_MCP_STATELESS_HTTP", False),
            tavily=ProviderConfig(
                name="tavily",
                base_url=(
                    _tavily_gateway_base_url(
                        proxy_base_url=proxy_base_url,
                        default="https://api.tavily.com",
                    )
                    if tavily_mode == "gateway"
                    else _provider_base_url(
                        explicit_names=("MYSEARCH_TAVILY_BASE_URL",),
                        proxy_base_url="",
                        default="https://api.tavily.com",
                    )
                ),
                auth_mode=(
                    _get_str(
                        "MYSEARCH_TAVILY_GATEWAY_AUTH_MODE",
                        default="bearer",
                    )
                    if tavily_mode == "gateway"
                    else _get_str("MYSEARCH_TAVILY_AUTH_MODE", default="body")
                ),  # type: ignore[arg-type]
                auth_header=(
                    _get_str("MYSEARCH_TAVILY_GATEWAY_AUTH_HEADER", default="Authorization")
                    if tavily_mode == "gateway"
                    else _get_str("MYSEARCH_TAVILY_AUTH_HEADER", default="Authorization")
                ),
                auth_scheme=(
                    _get_str("MYSEARCH_TAVILY_GATEWAY_AUTH_SCHEME", default="Bearer")
                    if tavily_mode == "gateway"
                    else _get_str("MYSEARCH_TAVILY_AUTH_SCHEME", default="Bearer")
                ),
                auth_field=(
                    _get_str("MYSEARCH_TAVILY_GATEWAY_AUTH_FIELD", default="api_key")
                    if tavily_mode == "gateway"
                    else _get_str("MYSEARCH_TAVILY_AUTH_FIELD", default="api_key")
                ),
                default_paths={
                    "search": (
                        _tavily_gateway_path(
                            explicit_name="MYSEARCH_TAVILY_GATEWAY_SEARCH_PATH",
                            explicit_gateway_base_url=tavily_gateway_base_url,
                            proxy_base_url=proxy_base_url,
                            proxy_default="/api/search",
                            default="/search",
                        )
                        if tavily_mode == "gateway"
                        else _provider_path(
                            explicit_name="MYSEARCH_TAVILY_SEARCH_PATH",
                            proxy_base_url="",
                            proxy_default="/api/search",
                            default="/search",
                        )
                    ),
                    "extract": (
                        _tavily_gateway_path(
                            explicit_name="MYSEARCH_TAVILY_GATEWAY_EXTRACT_PATH",
                            explicit_gateway_base_url=tavily_gateway_base_url,
                            proxy_base_url=proxy_base_url,
                            proxy_default="/api/extract",
                            default="/extract",
                        )
                        if tavily_mode == "gateway"
                        else _provider_path(
                            explicit_name="MYSEARCH_TAVILY_EXTRACT_PATH",
                            proxy_base_url="",
                            proxy_default="/api/extract",
                            default="/extract",
                        )
                    ),
                },
                provider_mode=tavily_mode,
                api_keys=[
                    *(
                        _get_list(
                            "MYSEARCH_TAVILY_GATEWAY_TOKENS",
                            "MYSEARCH_TAVILY_GATEWAY_API_KEYS",
                        )
                        if tavily_mode == "gateway"
                        else _get_list("MYSEARCH_TAVILY_API_KEYS")
                    ),
                    *(
                        [tavily_gateway_token]
                        if tavily_mode == "gateway" and tavily_gateway_token
                        else ([proxy_api_key] if tavily_mode == "gateway" and proxy_api_key else [])
                    ),
                    *(
                        [_get_str("MYSEARCH_TAVILY_API_KEY")]
                        if tavily_mode != "gateway" and _get_str("MYSEARCH_TAVILY_API_KEY")
                        else []
                    ),
                ],
                keys_file=(
                    None
                    if tavily_mode == "gateway"
                    else _resolve_path(
                        "MYSEARCH_TAVILY_KEYS_FILE",
                        "MYSEARCH_TAVILY_ACCOUNTS_FILE",
                        default_name="accounts.txt",
                    )
                ),
            ),
            firecrawl=ProviderConfig(
                name="firecrawl",
                base_url=_provider_base_url(
                    explicit_names=("MYSEARCH_FIRECRAWL_BASE_URL",),
                    proxy_base_url=proxy_base_url,
                    default="https://api.firecrawl.dev",
                ),
                auth_mode=_get_str("MYSEARCH_FIRECRAWL_AUTH_MODE", default="bearer"),  # type: ignore[arg-type]
                auth_header=_get_str("MYSEARCH_FIRECRAWL_AUTH_HEADER", default="Authorization"),
                auth_scheme=_get_str("MYSEARCH_FIRECRAWL_AUTH_SCHEME", default="Bearer"),
                auth_field=_get_str("MYSEARCH_FIRECRAWL_AUTH_FIELD", default="api_key"),
                default_paths={
                    "search": _provider_path(
                        explicit_name="MYSEARCH_FIRECRAWL_SEARCH_PATH",
                        proxy_base_url=proxy_base_url,
                        proxy_default="/firecrawl/v2/search",
                        default="/v2/search",
                    ),
                    "scrape": _provider_path(
                        explicit_name="MYSEARCH_FIRECRAWL_SCRAPE_PATH",
                        proxy_base_url=proxy_base_url,
                        proxy_default="/firecrawl/v2/scrape",
                        default="/v2/scrape",
                    ),
                },
                api_keys=[
                    *_get_list("MYSEARCH_FIRECRAWL_API_KEYS"),
                    *(
                        [_get_str("MYSEARCH_FIRECRAWL_API_KEY")]
                        if _get_str("MYSEARCH_FIRECRAWL_API_KEY")
                        else ([proxy_api_key] if proxy_api_key else [])
                    ),
                ],
                keys_file=_resolve_path(
                    "MYSEARCH_FIRECRAWL_KEYS_FILE",
                    "MYSEARCH_FIRECRAWL_ACCOUNTS_FILE",
                    default_name="firecrawl_accounts.txt",
                ),
            ),
            exa=ProviderConfig(
                name="exa",
                base_url=_provider_base_url(
                    explicit_names=("MYSEARCH_EXA_BASE_URL",),
                    proxy_base_url=proxy_base_url,
                    default="https://api.exa.ai",
                ),
                auth_mode=_get_str("MYSEARCH_EXA_AUTH_MODE", default="bearer"),  # type: ignore[arg-type]
                auth_header=_get_str(
                    "MYSEARCH_EXA_AUTH_HEADER",
                    default="Authorization" if proxy_base_url else "x-api-key",
                ),
                auth_scheme=_get_str(
                    "MYSEARCH_EXA_AUTH_SCHEME",
                    default="Bearer" if proxy_base_url else "",
                ),
                auth_field=_get_str("MYSEARCH_EXA_AUTH_FIELD", default="api_key"),
                default_paths={
                    "search": _provider_path(
                        explicit_name="MYSEARCH_EXA_SEARCH_PATH",
                        proxy_base_url=proxy_base_url,
                        proxy_default="/exa/search",
                        default="/search",
                    ),
                },
                api_keys=[
                    *_get_list("MYSEARCH_EXA_API_KEYS"),
                    *(
                        [_get_str("MYSEARCH_EXA_API_KEY")]
                        if _get_str("MYSEARCH_EXA_API_KEY")
                        else ([proxy_api_key] if proxy_api_key else [])
                    ),
                ],
                keys_file=_resolve_path(
                    "MYSEARCH_EXA_KEYS_FILE",
                    "MYSEARCH_EXA_ACCOUNTS_FILE",
                    default_name="exa_accounts.txt",
                ),
            ),
            xai=ProviderConfig(
                name="xai",
                base_url=_normalize_base_url(
                    _get_str("MYSEARCH_XAI_BASE_URL", default="https://api.x.ai/v1")
                ),
                auth_mode=_get_str("MYSEARCH_XAI_AUTH_MODE", default="bearer"),  # type: ignore[arg-type]
                auth_header=_get_str("MYSEARCH_XAI_AUTH_HEADER", default="Authorization"),
                auth_scheme=_get_str("MYSEARCH_XAI_AUTH_SCHEME", default="Bearer"),
                auth_field=_get_str("MYSEARCH_XAI_AUTH_FIELD", default="api_key"),
                default_paths={
                    "responses": _normalize_path(
                        _get_str("MYSEARCH_XAI_RESPONSES_PATH", default="/responses")
                    ),
                    "social_search": _provider_path(
                        explicit_name="MYSEARCH_XAI_SOCIAL_SEARCH_PATH",
                        proxy_base_url=proxy_base_url,
                        proxy_default="/social/search",
                        default="/social/search",
                    ),
                    "social_health": _provider_path(
                        explicit_name="MYSEARCH_XAI_SOCIAL_HEALTH_PATH",
                        proxy_base_url=proxy_base_url,
                        proxy_default="/social/health",
                        default="/social/health",
                    ),
                },
                alternate_base_urls={
                    "social_search": _normalize_base_url(
                        _get_str("MYSEARCH_XAI_SOCIAL_BASE_URL") or proxy_base_url
                    ),
                    "social_health": _normalize_base_url(
                        _get_str("MYSEARCH_XAI_SOCIAL_BASE_URL") or proxy_base_url
                    ),
                },
                search_mode=_get_str(
                    "MYSEARCH_XAI_SEARCH_MODE",
                    default="compatible" if proxy_base_url else "official",
                ),  # type: ignore[arg-type]
                api_keys=[
                    *_get_list("MYSEARCH_XAI_API_KEYS"),
                    *(
                        [_get_str("MYSEARCH_XAI_API_KEY")]
                        if _get_str("MYSEARCH_XAI_API_KEY")
                        else ([proxy_api_key] if proxy_api_key else [])
                    ),
                ],
                keys_file=_resolve_path("MYSEARCH_XAI_KEYS_FILE"),
            ),
        )
