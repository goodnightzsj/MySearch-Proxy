"""搜索请求的类型别名。

从 `mysearch/clients.py` 抽出，使路由谓词层（`mysearch/query_routing.py`）能
在不与 `clients` 形成循环导入的前提下复用这些类型。`clients` 继续 re-export，
内部引用路径不变。

这些是 `Literal` 别名与少数无行为的数据载体。本模块也是 `dataclass(slots=True)`
兼容垫片的唯一所有者，需要它的上层从这里导入。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass as _dataclass
from typing import Literal


def dataclass(*args, **kwargs):
    """`dataclass(slots=True)` 在 3.10 以前不存在；低版本静默降级为非 slots 版本。"""
    if sys.version_info < (3, 10):
        kwargs.pop("slots", None)
    return _dataclass(*args, **kwargs)

SearchMode = Literal["auto", "web", "news", "social", "docs", "research", "github", "pdf"]

#: `SearchMode` 的取值元组，顺序与 Literal 声明一致。
SEARCH_MODES: tuple[SearchMode, ...] = (
    "auto",
    "web",
    "news",
    "social",
    "docs",
    "research",
    "github",
    "pdf",
)
SearchIntent = Literal[
    "auto",
    "factual",
    "status",
    "comparison",
    "tutorial",
    "exploratory",
    "news",
    "resource",
]
ResolvedSearchIntent = Literal[
    "factual",
    "status",
    "comparison",
    "tutorial",
    "exploratory",
    "news",
    "resource",
]
SearchStrategy = Literal["auto", "fast", "balanced", "verify", "deep"]
ProviderName = Literal["auto", "tavily", "firecrawl", "exa", "xai"]


@dataclass(slots=True)
class RouteDecision:
    """一次搜索的路由结果：选中的 provider 与其参数化选项。

    定义在这里而不是 `clients.py`，因为路由下游的纯函数层
    （`research/cache_keys`、`research/responses`）需要引用它，而在
    `clients` 里定义会迫使它们反向依赖编排层、形成环。
    `clients` 继续 re-export，原有导入路径不变。
    """

    provider: str
    reason: str
    tavily_topic: str = "general"
    firecrawl_categories: list[str] | None = None
    sources: list[str] | None = None
    fallback_chain: list[str] | None = None
    result_profile: Literal["off", "web", "news", "resource"] = "off"
    allow_exa_rescue: bool = False


@dataclass(slots=True)
class SearchRoutePolicy:
    """一个 mode 的路由策略：首选 provider、回退链与参数化选项。

    与 `RouteDecision` 同因：下游的路由谓词层（`query_routing`）需要引用它，
    定义在 `clients` 会迫使那种反向依赖。`clients` 继续 re-export。
    """

    key: str
    provider: str
    fallback_chain: tuple[str, ...] = ()
    tavily_topic: str = "general"
    firecrawl_categories: tuple[str, ...] = ()
    result_profile: Literal["off", "web", "news", "resource"] = "off"
    allow_exa_rescue: bool = False
