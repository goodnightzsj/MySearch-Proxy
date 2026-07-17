"""Grok 模型 registry —— 不依赖 mysearch.config 顶层 bootstrap 副作用。

r7 A1：从 `mysearch/config.py` 抽出。`config.py` 在模块导入时会读
`~/.codex/config.toml` / `mysearch/.env` 等（`_bootstrap_runtime_env`），
独立部署的 `social_gateway` / proxy 不应该被动触发这些副作用。

任何只需要 Grok registry（GrokModelSpec / _BUILTIN_GROK_MODELS /
_resolve_grok_models / _sanitize_grok_model_ids）的模块都应当
`from mysearch.grok_registry import ...`，**不要**经过 mysearch.config。
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass as _dataclass


def _dataclass_compat(*args, **kwargs):
    if sys.version_info < (3, 10):
        kwargs.pop("slots", None)
    return _dataclass(*args, **kwargs)


def _get_list_local(*names: str) -> list[str]:
    """与 config._get_list 行为一致，但不触发 config 模块加载。

    保持独立实现是为了让 grok_registry 自包含——下游可以单独导入而不带入
    `_bootstrap_runtime_env` 副作用。
    """

    for name in names:
        value = os.getenv(name)
        if value is None or not value.strip():
            continue
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


@_dataclass_compat(slots=True, frozen=True)
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
    GrokModelSpec(id="grok-4.20-0309", tier="basic", source="builtin"),
    GrokModelSpec(id="grok-4.3", tier="basic", source="builtin"),
    GrokModelSpec(id="grok-4.5", tier="advanced", source="builtin"),
)


# 模型 ID 允许的字符集：字母数字、点、下划线、冒号、连字符、斜杠；长度上限 128 字节。
_GROK_MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:/\-]{1,128}$")
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

    override = _sanitize_grok_model_ids(_get_list_local("MYSEARCH_GROK_MODELS"))
    if override:
        return tuple(
            GrokModelSpec(id=mid, tier="custom", source="user") for mid in override
        )

    extras = _sanitize_grok_model_ids(_get_list_local("MYSEARCH_GROK_EXTRA_MODELS"))
    if not extras:
        return _BUILTIN_GROK_MODELS

    builtin_ids = {m.id for m in _BUILTIN_GROK_MODELS}
    appended = tuple(
        GrokModelSpec(id=mid, tier="custom", source="user")
        for mid in extras
        if mid not in builtin_ids
    )
    return _BUILTIN_GROK_MODELS + appended


__all__ = [
    "GrokModelSpec",
    "_BUILTIN_GROK_MODELS",
    "_GROK_MODEL_ID_PATTERN",
    "_MAX_GROK_MODEL_ENTRIES",
    "_sanitize_grok_model_ids",
    "_resolve_grok_models",
]
