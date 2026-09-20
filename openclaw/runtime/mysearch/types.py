"""搜索请求的类型别名。

从 `mysearch/clients.py` 抽出，使路由谓词层（`mysearch/query_routing.py`）能
在不与 `clients` 形成循环导入的前提下复用这些类型。`clients` 继续 re-export，
内部引用路径不变。

这些是 `Literal` 别名，运行时只是 `typing` 对象，没有行为。
"""

from __future__ import annotations

from typing import Literal

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
