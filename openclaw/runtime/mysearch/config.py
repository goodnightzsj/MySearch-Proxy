"""MySearch 通用配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


MODULE_DIR = Path(__file__).resolve().parent
ROOT_DIR = MODULE_DIR.parent
AuthMode = Literal["bearer", "body"]
XAISearchMode = Literal["official", "compatible"]


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


def _load_dotenv() -> None:
    # 优先读取 mysearch/.env，使 MySearch 在当前单仓目录下也能独立运行。
    for env_path in (MODULE_DIR / ".env", ROOT_DIR / ".env"):
        _load_env_file(env_path)


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
    return int(value.strip())


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


_load_dotenv()


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
    xai_model: str
    tavily: ProviderConfig
    firecrawl: ProviderConfig
    xai: ProviderConfig

    @classmethod
    def from_env(cls) -> "MySearchConfig":
        return cls(
            server_name=_get_str("MYSEARCH_NAME", "MYSEARCH_SERVER_NAME", default="MySearch"),
            timeout_seconds=_get_int("MYSEARCH_TIMEOUT_SECONDS", 45),
            xai_model=_get_str(
                "MYSEARCH_XAI_MODEL",
                default="grok-4.20-beta-latest-non-reasoning",
            ),
            tavily=ProviderConfig(
                name="tavily",
                base_url=_normalize_base_url(
                    _get_str("MYSEARCH_TAVILY_BASE_URL", default="https://api.tavily.com")
                ),
                auth_mode=_get_str("MYSEARCH_TAVILY_AUTH_MODE", default="body"),  # type: ignore[arg-type]
                auth_header=_get_str("MYSEARCH_TAVILY_AUTH_HEADER", default="Authorization"),
                auth_scheme=_get_str("MYSEARCH_TAVILY_AUTH_SCHEME", default="Bearer"),
                auth_field=_get_str("MYSEARCH_TAVILY_AUTH_FIELD", default="api_key"),
                default_paths={
                    "search": _normalize_path(
                        _get_str("MYSEARCH_TAVILY_SEARCH_PATH", default="/search")
                    ),
                    "extract": _normalize_path(
                        _get_str("MYSEARCH_TAVILY_EXTRACT_PATH", default="/extract")
                    ),
                },
                api_keys=[
                    *_get_list("MYSEARCH_TAVILY_API_KEYS"),
                    *([_get_str("MYSEARCH_TAVILY_API_KEY")] if _get_str("MYSEARCH_TAVILY_API_KEY") else []),
                ],
                keys_file=_resolve_path(
                    "MYSEARCH_TAVILY_KEYS_FILE",
                    "MYSEARCH_TAVILY_ACCOUNTS_FILE",
                    default_name="accounts.txt",
                ),
            ),
            firecrawl=ProviderConfig(
                name="firecrawl",
                base_url=_normalize_base_url(
                    _get_str(
                        "MYSEARCH_FIRECRAWL_BASE_URL",
                        default="https://api.firecrawl.dev",
                    )
                ),
                auth_mode=_get_str("MYSEARCH_FIRECRAWL_AUTH_MODE", default="bearer"),  # type: ignore[arg-type]
                auth_header=_get_str("MYSEARCH_FIRECRAWL_AUTH_HEADER", default="Authorization"),
                auth_scheme=_get_str("MYSEARCH_FIRECRAWL_AUTH_SCHEME", default="Bearer"),
                auth_field=_get_str("MYSEARCH_FIRECRAWL_AUTH_FIELD", default="api_key"),
                default_paths={
                    "search": _normalize_path(
                        _get_str("MYSEARCH_FIRECRAWL_SEARCH_PATH", default="/v2/search")
                    ),
                    "scrape": _normalize_path(
                        _get_str("MYSEARCH_FIRECRAWL_SCRAPE_PATH", default="/v2/scrape")
                    ),
                },
                api_keys=[
                    *_get_list("MYSEARCH_FIRECRAWL_API_KEYS"),
                    *(
                        [_get_str("MYSEARCH_FIRECRAWL_API_KEY")]
                        if _get_str("MYSEARCH_FIRECRAWL_API_KEY")
                        else []
                    ),
                ],
                keys_file=_resolve_path(
                    "MYSEARCH_FIRECRAWL_KEYS_FILE",
                    "MYSEARCH_FIRECRAWL_ACCOUNTS_FILE",
                    default_name="firecrawl_accounts.txt",
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
                    "social_search": _normalize_path(
                        _get_str("MYSEARCH_XAI_SOCIAL_SEARCH_PATH", default="/social/search")
                    ),
                },
                alternate_base_urls={
                    "social_search": _normalize_base_url(
                        _get_str("MYSEARCH_XAI_SOCIAL_BASE_URL")
                    )
                },
                search_mode=_get_str("MYSEARCH_XAI_SEARCH_MODE", default="official"),  # type: ignore[arg-type]
                api_keys=[
                    *_get_list("MYSEARCH_XAI_API_KEYS"),
                    *([_get_str("MYSEARCH_XAI_API_KEY")] if _get_str("MYSEARCH_XAI_API_KEY") else []),
                ],
                keys_file=_resolve_path("MYSEARCH_XAI_KEYS_FILE"),
            ),
        )
