"""Grok 模型清单的"探测式刷新"逻辑。

## 为什么是"探测式"而不是"采信式"

grok2api 上游有三个能列模型的来源，但**没有一个是可信的可用性判据**（2026-09-17 实测）：

| 来源 | 含 `-non-reasoning`（实测 200） | 含 `grok-4.20-0309`（实测 404） |
|---|---|---|
| `GET /v1/models` | 有 | **有（误报）** |
| `GET /api/admin/v1/models` | **无（漏报）** | 无（正确） |
| 真实推理调用 | 有 | 无（正确） |

所以本模块的判定顺序是：**列候选 → 逐个真实探测 → 只保留探测通过的**。
探测成本是每次真实推理 15-55s，因此刷新必须低频（默认 24h），且探测有上限。

## 探测必须校验"工具真的被调用了"，不能只看 HTTP 200

这是踩过的坑：`grok-4.6` / `grok-4.5` 对 `/v1/responses` 返回 **200**，
但 output 里**没有任何 tool_call**，正文里却出现 ` ```invoke tool ` /
`<tool_call>` 这类文本形态的调用意图。

**2026-09-22 更正归因**：当时把原因写成"凭训练数据编造 X 帖子"，**这个归因是错的**。
真实原因是 grok2api 的**上游通道差异**，与模型版本无关：

| 通道（`model_routes.provider`） | 服务端工具 | 覆盖 |
|---|---|---|
| `grok_console` | ✅ `x_search` | 4.3、4.20 全系、`Console/grok-4.5` |
| `grok_build` | ❌ 被 `buildXSearchResponseFilter` 过滤 | 4.7、4.6、`Build/grok-4.5` |

判据是通道：同一 `grok-4.5` 走 `Console/grok-4.5` 时实测正常发出
`custom_tool_call`，走 `Build/grok-4.5` 则没有。`grok_build` 是 xAI 的
agent/coding 通道，工具集是 shell/文件类，不含 `x_search`。
4.6/4.7 搜不了是因为 xAI 只提供 Build 版本（`Console/grok-4.7` 实测 404）。

**这不改变本模块的行为**：探测仍是必要的——它按实际输出裁定，天然覆盖通道差异。
只验 200 仍会让这类模型被选为搜索主模型，产出看似正常实则无依据的结果。
探测必须发送真实的 `x_search` 工具并断言：

1. 响应含 tool_call 类 output item，**且**
2. 响应含真实 status ID

（探测查询选用必然有结果的词，所以第 2 条不成立即意味着没有发生真实搜索。）

实测支持 x_search 的模型（2026-09-17）：`grok-4.3`、`grok-4.20-0309-reasoning`、
`grok-4.20-0309-non-reasoning`、`grok-4.20-multi-agent-0309`。

## 排序与"不追版本"

排序只用于**现有模型失效后**挑选替补，不是刷新的目标。原因：上游命名跨两套方案
（点分式 `grok-4.3` / 日期式 `grok-4.20-0309`），**无法从名字可靠判断谁"更新"**——
数值上 4.6 < 4.20，但语义上 `grok-4.6` 比 `grok-4.20-0309` 新。按名字排序切换模型
等于赌版本语义，实测已因此把不支持搜索的模型推上生产。

因此 `pick_primary_and_fallback` 的语义是**发现失效并恢复**：
现有模型仍在 capable 列表里就保留，失效了才用排序结果替补。

排序规则：点分式整体优先于日期式，再按数值降序；同日期内 `non-reasoning` 优先于
`reasoning`（搜索不需要推理深度，而 reasoning 实测慢约 3 倍：40.6s vs 14.9s）。
"""

from __future__ import annotations

import re
from typing import Any

# 日期式：grok-<major>.<minor>-<MMDD>[-<suffix>]
_DATED_ID = re.compile(r"^grok-(\d+)\.(\d+)-(\d{3,4})(?:-(.+))?$")
# 纯点分式：grok-<major>.<minor>
_DOTTED_ID = re.compile(r"^grok-(\d+)\.(\d+)$")

# 搜索场景只需要文本推理能力；image/video/tts/stt/realtime 与本用途无关。
TEXT_CAPABILITY = "responses"

# 探测用的工具类型与查询。查询选用必然有结果的词，这样"没有 status ID"
# 就能可靠地推出"没有发生真实搜索"。
SEARCH_TOOL_TYPE = "x_search"
PROBE_QUERY = "OpenAI latest announcement"

# 判定"真实搜索结果"的凭据：X 帖子 URL 里的数字 status ID。
_STATUS_ID = re.compile(r"(?:x|twitter)\.com/[A-Za-z0-9_]+/status/(\d+)")

# 同日期变体的偏好：搜索不需要推理深度，而 reasoning 变体实测慢约 3 倍
# （40.6s vs 14.9s）。数值越大越优先。
_SUFFIX_RANK = {"non-reasoning": 2, "": 1, "reasoning": 1}
_DEFAULT_SUFFIX_RANK = 0

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


def build_probe_payload(model_id: str, query: str = PROBE_QUERY) -> dict[str, Any]:
    """构造探测请求体：必须带真实搜索工具，否则测不出模型是否会用工具。"""
    return {
        "model": model_id,
        "input": query,
        "tools": [{"type": SEARCH_TOOL_TYPE}],
        "stream": False,
    }


def count_tool_calls(payload: Any) -> int:
    """统计 output 里的工具调用 item 数。

    上游用 `custom_tool_call`；用子串匹配以兼容其它 tool_call 命名。
    """
    if not isinstance(payload, dict):
        return 0
    items = payload.get("output")
    if not isinstance(items, list):
        return 0
    count = 0
    for item in items:
        if isinstance(item, dict) and "tool_call" in str(item.get("type") or ""):
            count += 1
    return count


def extract_status_ids(payload: Any) -> list[str]:
    """从整个响应里抽出 X 帖子的 status ID（保序去重）。"""
    if payload is None:
        return []
    import json

    try:
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    except Exception:
        text = str(payload)
    seen: set[str] = set()
    ids: list[str] = []
    for found in _STATUS_ID.findall(text):
        if found not in seen:
            seen.add(found)
            ids.append(found)
    return ids


def evaluate_search_probe(payload: Any) -> dict[str, Any]:
    """判定一次探测响应是否证明该模型**真的能搜索**。

    只看 HTTP 状态是不够的：实测 `grok-4.6` 返回 200 却完全不调用 `x_search`，
    正文里只有 ` ```invoke tool ` / `<tool_call>` 这类**文本形态的调用意图**
    （原因是它走 grok2api 的 `grok_build` 通道，该通道不含 x_search 工具，
    见模块文档的 2026-09-22 更正）。必须同时看到工具调用与真实 status ID。
    """
    tool_calls = count_tool_calls(payload)
    status_ids = extract_status_ids(payload)
    return {
        "tool_calls": tool_calls,
        "status_ids": len(status_ids),
        "search_capable": tool_calls > 0 and len(status_ids) > 0,
    }


def grok_model_sort_key(model_id: str) -> tuple[int, int, int, int, int, str]:
    """越"新"越大。点分式（grok-4.6）整体高于日期式（grok-4.20-0309）。"""
    dated = _DATED_ID.match(model_id)
    if dated:
        suffix = (dated.group(4) or "").strip()
        return (
            1,
            int(dated.group(1)),
            int(dated.group(2)),
            int(dated.group(3)),
            _SUFFIX_RANK.get(suffix, _DEFAULT_SUFFIX_RANK),
            model_id,
        )
    dotted = _DOTTED_ID.match(model_id)
    if dotted:
        return (2, int(dotted.group(1)), int(dotted.group(2)), 0, 0, model_id)
    # 不认识的命名：不参与排序竞争，排在最后。
    return (0, 0, 0, 0, 0, model_id)



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


def pick_primary_and_fallback(capable_ids, current_primary: str = "") -> tuple[str, str]:
    """从**探测通过**的模型里选 primary 与 fallback。

    传入的必须是**已确认支持搜索工具**的模型 ID——本函数不做可用性判定。

    优先保留 `current_primary`（若它仍在 capable_ids 里）：上游命名跨两套方案
    （点分式 `grok-4.3` / 日期式 `grok-4.20-0309`），**无法从名字可靠判断谁"更新"**，
    按名字排序切换模型等于赌版本语义——实测已因此把一个不支持搜索的模型推上生产。
    所以本函数的职责是**发现失效并恢复**，不是追版本：

    - 现有模型仍可用 -> 保留（不制造无谓变更）
    - 现有模型失效   -> 切到排序最优的可用模型（恢复）

    返回 `("", "")` 表示没有可用模型，调用方应保留现有配置而非写入空值。
    """
    ranked = rank_candidates(capable_ids)
    if not ranked:
        return "", ""
    current = str(current_primary or "").strip()
    if current and current in ranked:
        primary = current
    else:
        primary = ranked[0]
    fallback = next((model_id for model_id in ranked if model_id != primary), "")
    return primary, fallback



__all__ = [
    "TEXT_CAPABILITY",
    "SEARCH_TOOL_TYPE",
    "PROBE_QUERY",
    "DEFAULT_EXCLUDED_PREFIXES",
    "build_probe_payload",
    "count_tool_calls",
    "extract_status_ids",
    "evaluate_search_probe",
    "grok_model_sort_key",
    "is_eligible_model",
    "rank_candidates",
    "collect_text_candidates",
    "collect_candidates_from_model_list",
    "merge_candidates",
    "pick_primary_and_fallback",
]
