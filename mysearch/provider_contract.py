"""Provider 响应的单一契约。

`mysearch/clients.py` 里原本有 70+ 处内联构造 provider 字段，形状与取值靠约定
维持。本模块把它们收敛成一处，并拆开被重载的 ``provider`` key。

## 被重载的 `provider`

原实现把同一个 key 用在三个层级上，取值互不重叠但语义完全不同：

- **响应级** ``response["provider"]``：谁产生了这次响应。
  拆为 :attr:`ProviderResponse.kind`（真实 provider / 合成结果 / 失败占位）
  与 :attr:`ProviderResponse.provider`（真实 provider 名，合成时为 ``""``）。
- **结果项级** ``item["provider"]``：这一条结果源自什么。
  见 :data:`ITEM_SOURCE_KINDS`。

响应级历史取值共 16 个，包含 4 个从未出现在任何常量表里的值
（``canonical-rescue``、``github_raw``、``discovery_prefetch``、
``custom_social``），这正是"靠约定维持"导致的漂移。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

# --- 响应类别 -------------------------------------------------------------

#: 真实 provider 适配器。
PROVIDER_NAMES: frozenset[str] = frozenset({"tavily", "firecrawl", "exa", "xai"})

#: Social/X 路径上的 provider 变体：真实 provider 的降级实现。
SOCIAL_PROVIDER_VARIANTS: frozenset[str] = frozenset(
    {"exa_social_fallback", "tavily_social_fallback", "custom_social"}
)

#: 多 provider 交叉检索的合成结果。它不是任何单一 provider 的产物。
KIND_HYBRID = "hybrid"

#: 失败占位：调用方据此判断"这条路没结果"，而不是"provider 返回了空"。
KIND_UNAVAILABLE = "unavailable"

#: 由权威来源预取的中间产物，服务于研究路径而非直接回答。
KIND_ENRICHMENT = "enrichment"

#: 结果项级来源类别（``item["provider"]``）。
ITEM_SOURCE_KINDS: frozenset[str] = frozenset(
    {
        "canonical_research_docs",
        "canonical_research_projects",
        "discovery_prefetch",
        "github_raw",
    }
)

#: 历史响应级取值 → 契约类别。用于迁移期识别与校验。
_LEGACY_KIND_BY_VALUE: dict[str, str] = {
    **{name: "provider" for name in PROVIDER_NAMES},
    **{name: "provider" for name in SOCIAL_PROVIDER_VARIANTS},
    "hybrid": KIND_HYBRID,
    "web_unavailable": KIND_UNAVAILABLE,
    "social_unavailable": KIND_UNAVAILABLE,
    "social_gateway_unavailable": KIND_UNAVAILABLE,
    "canonical_research_docs": KIND_ENRICHMENT,
    "canonical_research_projects": KIND_ENRICHMENT,
    "canonical-rescue": KIND_ENRICHMENT,
    "discovery_prefetch": KIND_ENRICHMENT,
    "github_raw": KIND_ENRICHMENT,
}

ALL_LEGACY_PROVIDER_VALUES: frozenset[str] = frozenset(_LEGACY_KIND_BY_VALUE)


def classify_provider_value(value: Any) -> str | None:
    """把一个历史 ``provider`` 取值映射到契约类别；未知返回 ``None``。"""
    return _LEGACY_KIND_BY_VALUE.get(str(value or ""))


class ProviderContractError(ValueError):
    """响应不符合 provider 契约。"""


class ProviderResponse:
    """provider 响应的构造器、校验器与查询入口。

    分离后的字段约定（**新增字段，旧字段保留以维持行为**）:

    - ``provider``：真实 provider 名；合成/失败/预取响应为 ``""``。
    - ``kind``：``"provider"`` / ``"hybrid"`` / ``"unavailable"`` / ``"enrichment"``。
    - ``source_kind``：仅 enrichment 响应携带，记录具体来源类别。
    """

    @classmethod
    def build(
        cls,
        *,
        provider: str = "",
        kind: str = "provider",
        source_kind: str = "",
        results: Sequence[Mapping[str, Any]] = (),
        citations: Sequence[Mapping[str, Any]] = (),
        query: str = "",
        answer: str = "",
        transport: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """构造一个符合契约的响应字典。"""
        for key, value in (("results", results), ("citations", citations)):
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
                raise ProviderContractError(
                    f"{key} must be a sequence of mappings, got {type(value).__name__}"
                )
        payload: dict[str, Any] = {"provider": provider}
        if transport is not None:
            payload["transport"] = transport
        payload["query"] = query
        payload["answer"] = answer
        payload["results"] = [dict(item) for item in results]
        payload["citations"] = [dict(item) for item in citations]
        payload["kind"] = kind
        if source_kind:
            payload["source_kind"] = source_kind
        payload.update(extra)
        cls.validate(payload)
        return payload

    @classmethod
    def validate(cls, payload: Mapping[str, Any]) -> None:
        """校验分离后的响应结构。"""
        kind = payload.get("kind")
        if kind is None:
            # 迁移期：尚未迁移的旧构造点没有 kind，按 provider 值推断。
            inferred = classify_provider_value(payload.get("provider"))
            if inferred is None:
                raise ProviderContractError(
                    f"response has no 'kind' and its 'provider' value "
                    f"{payload.get('provider')!r} is not a known contract value"
                )
            return
        if kind not in {"provider", KIND_HYBRID, KIND_UNAVAILABLE, KIND_ENRICHMENT}:
            raise ProviderContractError(f"unknown response kind {kind!r}")

        provider = str(payload.get("provider") or "")
        if kind == "provider":
            if not provider:
                raise ProviderContractError(
                    "a real-provider response must name its provider"
                )
            if provider not in PROVIDER_NAMES | SOCIAL_PROVIDER_VARIANTS:
                raise ProviderContractError(
                    f"{provider!r} is not a known provider name"
                )
        elif provider:
            raise ProviderContractError(
                f"a {kind!r} response must not carry a real provider name "
                f"(got {provider!r}); use 'source_kind' instead"
            )

        if kind == KIND_ENRICHMENT:
            source_kind = str(payload.get("source_kind") or "")
            if source_kind not in ITEM_SOURCE_KINDS:
                raise ProviderContractError(
                    f"enrichment response needs a known 'source_kind', got {source_kind!r}"
                )

        for key in ("results", "citations"):
            value = payload.get(key)
            if value is not None and not isinstance(value, list):
                raise ProviderContractError(
                    f"response {key!r} must be a list, got {type(value).__name__}"
                )

    # --- 查询 -------------------------------------------------------------

    @staticmethod
    def kind_of(payload: Mapping[str, Any]) -> str:
        """响应的契约类别；旧构造点按其 provider 值推断。"""
        kind = payload.get("kind")
        if isinstance(kind, str) and kind:
            return kind
        return classify_provider_value(payload.get("provider")) or ""

    @staticmethod
    def provider_name(payload: Mapping[str, Any]) -> str:
        return str(payload.get("provider") or "")

    @classmethod
    def is_real_provider(cls, payload: Mapping[str, Any]) -> bool:
        return cls.kind_of(payload) == "provider"

    @classmethod
    def is_synthetic(cls, payload: Mapping[str, Any]) -> bool:
        kind = cls.kind_of(payload)
        return kind in {KIND_HYBRID, KIND_UNAVAILABLE, KIND_ENRICHMENT}

    @classmethod
    def is_hybrid(cls, payload: Mapping[str, Any]) -> bool:
        """该响应是否为多 provider 交叉检索的合成结果。

        ``KIND_HYBRID`` 在分类表里恰好只有一个成员（``"hybrid"``），所以本
        判定与历史上的 ``payload.get("provider") == "hybrid"`` 完全等价。
        """
        return cls.kind_of(payload) == KIND_HYBRID


def build_response(**kwargs: Any) -> dict[str, Any]:
    """``ProviderResponse.build`` 的函数式别名。"""
    return ProviderResponse.build(**kwargs)


def collect_provider_values(responses: Iterable[Mapping[str, Any]]) -> list[str]:
    """按出现顺序收集响应级 provider 值。"""
    seen: list[str] = []
    for response in responses:
        value = ProviderResponse.provider_name(response)
        if value and value not in seen:
            seen.append(value)
    return seen
