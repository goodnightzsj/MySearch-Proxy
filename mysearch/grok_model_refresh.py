"""Grok 模型清单的"探测式刷新"逻辑。

## 为什么是"探测式"而不是"采信式"

grok2api 上游有三个能列模型的来源，但**没有一个是可信的可用性判据**（2026-09-17 实测）：

| 来源 | 含 `-non-reasoning`（实测 200） | 含 `grok-4.20-0309`（实测 404） |
|---|---|---|
| `GET /v1/models` | 有 | **有（误报）** |
| `GET /api/admin/v1/models` | **无（漏报）** | 无（正确） |
| 真实推理调用 | 有 | 无（正确） |

所以本模块的判定顺序是：**列候选 → 逐个真实探测 → 只保留探测通过的**。
探测成本是每次真实推理 5-9s，因此刷新必须低频（默认 24h），且探测有上限。

## 排序

上游有两套并存的命名方案，纯字符串或纯数字排序都会错：

- 点分式：`grok-4.6`、`grok-4.5` —— 版本号语义
- 日期式：`grok-4.20-0309`、`grok-4.20-multi-agent-0309` —— `-MMDD` 发布标记

`grok-4.6` 比 `grok-4.20-0309` 新，但数值上 4.6 < 4.20。因此点分式**整体优先于**日期式，
再按数值降序。这是启发式：上游若引入第三种命名方案需同步更新 `grok_model_sort_key`。
"""

from __future__ import annotations

import re
from typing import Any

# 日期式：grok-<major>.<minor>-<MMDD>[-<suffix>]
_DATED_ID = re.compile(r"^grok-(\d+)\.(\d+)-(\d{3,4})(?:-|$)")
# 纯点分式：grok-<major>.<minor>
_DOTTED_ID = re.compile(r"^grok-(\d+)\.(\d+)$")

# 搜索场景只需要文本推理能力；image/video/tts/stt/realtime 与本用途无关。
TEXT_CAPABILITY = "responses"

# 冷门/非文本模型不参与 primary 竞选。
# 注意：这里的排除是**前缀匹配**，所以 `grok-stt` 这类不是 `grok-imagine-` 开头的
# 多模态模型也会漏网——它们由真实探测兜底（探测会失败）。排除表的作用只是
# 减少无谓的探测次数，不是可用性判据。
DEFAULT_EXCLUDED_PREFIXES = (
    "grok-build-",
    "grok-composer-",
    "grok-imagine-",
    "grok-voice-",
    "grok-stt",
    "grok-tts",
    "grok-embed",
)


def grok_model_sort_key(model_id: str) -> tuple[int, int, int, int, str]:
    """越"新"越大。点分式（grok-4.6）整体高于日期式（grok-4.20-0309）。"""
    dated = _DATED_ID.match(model_id)
    if dated:
        return (1, int(dated.group(1)), int(dated.group(2)), int(dated.group(3)), model_id)
    dotted = _DOTTED_ID.match(model_id)
    if dotted:
        return (2, int(dotted.group(1)), int(dotted.group(2)), 0, model_id)
    # 不认识的命名：不参与排序竞争，排在最后。
    return (0, 0, 0, 0, model_id)


def is_eligible_model(model_id: str, excluded_prefixes=DEFAULT_EXCLUDED_PREFIXES) -> bool:
    """是否是可用于搜索默认值的候选（排除工具型/多模态模型）。"""
    if not model_id or not model_id.startswith("grok-"):
        return False
    return not any(model_id.startswith(prefix) for prefix in excluded_prefixes)


def rank_candidates(model_ids) -> list[str]:
    """按"新→旧"排序，去重，只留 eligible 项。"""
    seen: set[str] = set()
    eligible: list[str] = []
    for model_id in model_ids:
        cleaned = str(model_id or "").strip()
        if not cleaned or cleaned in seen:
            continue
        if not is_eligible_model(cleaned):
            continue
        seen.add(cleaned)
        eligible.append(cleaned)
    return sorted(eligible, key=grok_model_sort_key, reverse=True)


def collect_text_candidates(models_payload: Any) -> list[str]:
    """从 `GET /api/admin/v1/models` 的响应里抽出文本推理候选。

    该端点的 `capability=responses` 分类**会漏报**（实测漏掉可用的
    `grok-4.20-0309-non-reasoning`），所以这里只把它当候选来源之一，
    最终可用性一律由真实探测决定。因此 `available` 字段不被采信为判据。
    """
    items = (models_payload or {}).get("items")
    if not isinstance(items, list):
        return []
    candidates: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("capability") or "") != TEXT_CAPABILITY:
            continue
        public_id = str(item.get("publicId") or "").strip()
        if public_id:
            candidates.append(public_id)
    return rank_candidates(candidates)


def collect_candidates_from_model_list(models_payload: Any) -> list[str]:
    """从 OpenAI 兼容的 `GET /v1/models` 响应里抽候选。

    该列表是**超集**（含实测 404 的 `grok-4.20-0309`），只能当候选来源。
    """
    data = models_payload.get("data") if isinstance(models_payload, dict) else None
    if not isinstance(data, list):
        data = models_payload.get("models") if isinstance(models_payload, dict) else None
    if not isinstance(data, list):
        return []
    ids: list[str] = []
    for item in data:
        if isinstance(item, dict):
            model_id = item.get("id") or item.get("model") or ""
        else:
            model_id = item
        if model_id:
            ids.append(str(model_id))
    return rank_candidates(ids)


def merge_candidates(*candidate_lists) -> list[str]:
    """合并多个来源的候选（取并集后统一排序）。

    并集是必要的：两个端点各有漏报，任一单独来源都会丢可用模型。
    """
    merged: list[str] = []
    for candidates in candidate_lists:
        merged.extend(candidates or [])
    return rank_candidates(merged)


def pick_primary_and_fallback(available_ids) -> tuple[str, str]:
    """从**探测通过**的模型里选 primary 与 fallback（按新→旧）。

    返回 `("", "")` 表示没有任何可用模型——调用方应当保留现有配置而不是写入空值。
    """
    ranked = rank_candidates(available_ids)
    if not ranked:
        return "", ""
    if len(ranked) == 1:
        # 只有一个可用时，fallback 留空；has_social_fallback 会判定其不可用，
        # 不会触发指向同一模型的重复请求。
        return ranked[0], ""
    return ranked[0], ranked[1]


__all__ = [
    "TEXT_CAPABILITY",
    "DEFAULT_EXCLUDED_PREFIXES",
    "grok_model_sort_key",
    "is_eligible_model",
    "rank_candidates",
    "collect_text_candidates",
    "collect_candidates_from_model_list",
    "merge_candidates",
    "pick_primary_and_fallback",
]
