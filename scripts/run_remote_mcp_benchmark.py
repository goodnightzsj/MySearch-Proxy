#!/usr/bin/env python3
import argparse
import base64
import csv
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

try:
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - py311 fallback
    import tomli as tomllib  # type: ignore[no-redef]


# 目标主机与端点不硬编码：默认值来自环境变量，示例见 scripts/benchmark.env.example。
# 未设置时留空，由 --host / --mysearch-url / --tavily-url 显式传入。
DEFAULT_HOST = os.environ.get("MYSEARCH_BENCHMARK_HOST", "")
DEFAULT_MYSEARCH_URL = os.environ.get("MYSEARCH_BENCHMARK_MYSEARCH_URL", "http://127.0.0.1:18000/mcp")
DEFAULT_TAVILY_URL = os.environ.get("MYSEARCH_BENCHMARK_TAVILY_URL", "http://127.0.0.1:8787/mcp")
DEFAULT_TAVILY_BEARER = os.environ.get("MYSEARCH_BENCHMARK_TAVILY_BEARER", "")
DEFAULT_CODEX_CONFIG = str((Path(os.getenv("CODEX_HOME", "~/.codex")).expanduser() / "config.toml"))
DEFAULT_TAVILY_MCP_SERVER = "tavily-hikari"
FIRECRAWL_CRAWL_MAP_TOOLS = {"map_site", "crawl_site"}
FIRECRAWL_CRAWL_MAP_COOLDOWN_SECONDS = 65

#: 仅作**兜底**：CSV 的 `include_domains` 列优先（见 build_case）。
#: 这 9 条当前与 CSV 完全重复，所以实际增量为 0。保留是为了 CSV 列被清空时
#: 不静默丢覆盖，但若两者开始漂移，以 CSV 为准。
OFFICIAL_DOMAINS = {
    "official-web-01": ["openai.com"],
    "docs-01": ["playwright.dev"],
    "github-01": ["github.com"],
    "pdf-01": ["arxiv.org"],
    "price-01": ["apple.com.cn"],
    "status-01": ["openai.com"],
    "changelog-01": ["nextjs.org"],
    "localization-01": ["openai.com"],
    "strict-constraint-01": ["openai.com"],
}

BENCHMARK_DIMENSIONS = (
    "authority_precision",
    "semantic_discovery",
    "provider_orchestration",
    "multi_source_fusion",
    "content_fidelity",
    "freshness_signal",
    "site_coverage",
    "traceability",
    "resilience",
    "efficiency",
    "claim_groundedness",
    "assertion_pass_rate",
)

#: `claim_groundedness` 提取专名时排除的词。取的是**句首高频词**：
#: 英文答案几乎都以 `The` 开头，若不排除，"The latest stable version of
#: Java is 26.1.2" 会因为 `The` 恰好在正文里而虚高比例。
_GROUNDING_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "based", "be", "been", "but", "by",
    "current", "for", "from", "has", "have", "however", "in", "is", "it",
    "its", "latest", "new", "no", "not", "of", "on", "or", "per", "please",
    "released", "stable", "that", "the", "their", "there", "these", "this",
    "to", "version", "was", "were", "which", "with",
})

FIELDNAMES = [
    "benchmark_id",
    "domain",
    "query",
    "prompt_variant",
    "run_date",
    "active_dimensions",
    "run_status",
    "latency_budget_ms",
    "expected_answer_patterns",
    "mysearch_tool",
    "mysearch_mode",
    "mysearch_provider_trace",
    "mysearch_summary",
    "mysearch_top_urls",
    "mysearch_citation_count",
    "mysearch_content_char_count",
    "mysearch_content_item_count",
    "mysearch_content_noise_hits",
    "mysearch_duplicate_url_count",
    "mysearch_published_date_count",
    "mysearch_expected_answer_match",
    "mysearch_official_mode",
    "mysearch_conflicts",
    "mysearch_latency_ms",
    "mysearch_repeat_variance",
    "mysearch_repeat_observations",
    "mysearch_cold_latency_ms",
    "mysearch_warm_latency_ms",
    "mysearch_latency_budget_exceeded",
    "mysearch_empty_result",
    "mysearch_timeout",
    "mysearch_orchestration_used",
    "mysearch_fallback_attempted",
    "mysearch_fallback_reason",
    "mysearch_fallback_used",
    "tavily_tool",
    "tavily_provider_trace",
    "tavily_summary",
    "tavily_top_urls",
    "tavily_citation_count",
    "tavily_content_char_count",
    "tavily_content_item_count",
    "tavily_content_noise_hits",
    "tavily_duplicate_url_count",
    "tavily_published_date_count",
    "tavily_expected_answer_match",
    "mysearch_claim_groundedness_ratio",
    "mysearch_grounded_token_count",
    "mysearch_assertion_pass_rate",
    "tavily_claim_groundedness_ratio",
    "tavily_grounded_token_count",
    "tavily_assertion_pass_rate",
    "tavily_latency_ms",
    "tavily_repeat_variance",
    "tavily_repeat_observations",
    "tavily_cold_latency_ms",
    "tavily_warm_latency_ms",
    "tavily_latency_budget_exceeded",
    "tavily_empty_result",
    "tavily_timeout",
    "tavily_orchestration_used",
    "tavily_fallback_attempted",
    "tavily_fallback_reason",
    "tavily_fallback_used",
    *(f"mysearch_{dimension}_score" for dimension in BENCHMARK_DIMENSIONS),
    "mysearch_total_score",
    *(f"tavily_{dimension}_score" for dimension in BENCHMARK_DIMENSIONS),
    "tavily_total_score",
    "winner",
    "winner_reason",
    "structural_failure",
    "optimization_hint",
    "notes",
    "error",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run dual MCP benchmark against remote mysearch and tavily-hikari via SSH.")
    parser.add_argument(
        "--input-csv",
        default=".codex-tasks/20260323-mysearch-vs-tavily-epic/tasks/20260323-baseline-benchmark/batch/workers-input.csv",
    )
    parser.add_argument(
        "--output-csv",
        default=".codex-tasks/20260323-mysearch-vs-tavily-epic/tasks/20260323-baseline-benchmark/batch/workers-output.csv",
    )
    parser.add_argument(
        "--raw-dir",
        default=".codex-tasks/20260323-mysearch-vs-tavily-epic/tasks/20260323-baseline-benchmark/raw",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--mysearch-url", default=DEFAULT_MYSEARCH_URL)
    parser.add_argument("--tavily-url", default=DEFAULT_TAVILY_URL)
    parser.add_argument("--tavily-bearer", default=os.environ.get("TAVILY_MCP_BEARER", DEFAULT_TAVILY_BEARER))
    parser.add_argument("--codex-config", default=DEFAULT_CODEX_CONFIG)
    parser.add_argument("--tavily-mcp-server", default=DEFAULT_TAVILY_MCP_SERVER)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--benchmark-id", action="append", default=[])
    parser.add_argument("--mysearch-only", action="store_true")
    parser.add_argument("--reuse-output-csv", default="")
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=0,
        help="Run the selected benchmark rows in multiple SSH batches to avoid long-lived connection resets.",
    )
    return parser.parse_args()


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def parse_pipe_list(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split("|") if item.strip()]


def parse_optional_bool(value: Optional[str], default: bool) -> bool:
    normalized = (value or "").strip().lower()
    if not normalized:
        return default
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def join_pipe_list(values: list[str]) -> str:
    return " | ".join(value for value in values if value)


def active_dimensions(row: dict[str, str]) -> str:
    primary = row.get("primary_dimensions", "").strip()
    secondary = row.get("secondary_dimensions", "").strip()
    bits = []
    if primary:
        bits.append(f"primary={primary}")
    if secondary:
        bits.append(f"secondary={secondary}")
    return "; ".join(bits)


def _extract_authorization_header(headers: object) -> str:
    if not isinstance(headers, dict):
        return ""
    for key, value in headers.items():
        if str(key).lower() != "authorization":
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_bearer_token(value: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        return ""
    if cleaned.lower().startswith("bearer "):
        return cleaned[7:].strip()
    return cleaned


def resolve_tavily_bearer(
    current_value: str,
    *,
    codex_config_path: Path,
    mcp_server_name: str,
) -> str:
    if str(current_value or "").strip():
        return str(current_value).strip()
    if not codex_config_path.exists():
        return ""
    try:
        config = tomllib.loads(codex_config_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    mcp_servers = config.get("mcp_servers")
    if not isinstance(mcp_servers, dict):
        return ""
    server = mcp_servers.get(mcp_server_name)
    if not isinstance(server, dict):
        return ""
    auth_header = _extract_authorization_header(server.get("headers"))
    if not auth_header:
        auth_header = _extract_authorization_header(server.get("http_headers"))
    return _extract_bearer_token(auth_header)


def map_mysearch_mode(row: dict[str, str]) -> str:
    mode_hint = row.get("mode_hint", "").strip()
    if mode_hint:
        return mode_hint
    domain = row["domain"]
    if row["preferred_tool"] == "research":
        return "research"
    if domain == "技术文档":
        return "docs"
    if domain == "GitHub":
        return "github"
    if domain == "PDF":
        return "pdf"
    if domain == "新闻":
        return "news"
    if domain == "纯 Social / X":
        return "social"
    return "web"


def map_strategy(row: dict[str, str]) -> str:
    """矩阵的 `strategy_hint` 决定传给工具的 strategy。

    留空表示 ``"auto"``（真实调用方的默认形态），用来覆盖
    `_resolve_strategy` 的推导分支 —— 那些分支只有在调用方不传 strategy 时才会走。
    历史实现里空值会退化成按 `prompt_variant` 猜，导致 auto 推导零覆盖。
    """
    strategy_hint = row.get("strategy_hint", "").strip()
    if strategy_hint:
        return strategy_hint
    return "auto"


def map_tavily_search_depth(row: dict[str, str]) -> str:
    strategy = map_strategy(row)
    if strategy in {"verify", "deep"}:
        return "advanced"
    return "fast"


import re as _re

#: 查询里点名了年份时，时间过滤就是在排除答案本身。
#:
#: 实测（loop34，隔离复现）：`2026 Oscars best picture winner` 配
#: `from_date=2026-08-23 .. to_date=2026-09-22` 只返回 1 条结果，抽出的答案是
#: **奖项定义**（britannica 的 "presented annually by the Academy…"）；同一查询
#: 去掉窗口后返回 `Best Picture winner: One Battle after Another`（正确，且
#: oscars.org 有直接证据）。原因是 2026 奥斯卡在 **3 月**、格莱美在 **2 月**，
#: 都在 30 天窗之外 —— 这类查询的答案按定义就在窗口外，硬过滤它等于制造
#: 不可能任务，测的不是能力。
#:
#: **注意**：Tavily 的 `time_range` 在它那一侧同样是硬过滤（parse 成 start_date），
#: 所以这个缺陷对两侧**对称**存在，不是单边不公平。修正的是"行本身不可满足"。
#:
#: 只对**点名年份**的行生效。"latest …" 类查询不带年份，仍然保留窗口 ——
#: 那正是它们要测的新鲜度能力。
_EXPLICIT_YEAR_RE = _re.compile(r"\b(?:19|20)\d{2}\b")

#: 明确要求"最新"的行，即使顺带提了年份也要保留窗口。
#: 反例：`latest celebrity breakup rumors 2026` —— 年份只是语境，它要测的正是
#: 能不能找到**当下**的八卦；按年份去窗会把这个能力一起删掉。
_FRESHNESS_ASK_RE = _re.compile(r"\blatest\b|\bcurrent\b|\brecent\b|\bnow\b|最新|最近|当前")


def _row_names_a_year(row: dict[str, str]) -> bool:
    return bool(_EXPLICIT_YEAR_RE.search(row.get("query", "")))


def _row_wants_freshness(row: dict[str, str]) -> bool:
    return bool(_FRESHNESS_ASK_RE.search(row.get("query", "").lower()))


def map_tavily_time_range(row: dict[str, str]) -> Optional[str]:
    domain = row["domain"]
    if domain in {"新闻", "技术动态 / status", "娱乐", "八卦", "纯 Social / X"}:
        # 点名年份且**不**要求"最新"的行不做时间过滤（见上面 _EXPLICIT_YEAR_RE 的说明）。
        if _row_names_a_year(row) and not _row_wants_freshness(row):
            return None
        return "month"
    if domain in {"更新日志 / release"}:
        return "year"
    return None


#: Tavily `time_range` 值的等价天数，用来给 MySearch 传同一时间窗。
#: 对齐两个 provider 的检索条件；否则 Tavily 拿到时间过滤而 MySearch 没有，
#: "freshness 不如 Tavily" 可能只是评测条件不对等造成的假象。
TIME_RANGE_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}


def map_mysearch_date_bounds(row: dict[str, str]) -> tuple[str, str]:
    """与 `map_tavily_time_range` 等价的时间窗，转成 MySearch 的 from_date/to_date。

    返回 ``(from_date, to_date)``，无时间过滤时返回 ``("", "")``。
    """
    time_range = map_tavily_time_range(row)
    if not time_range:
        return "", ""
    days = TIME_RANGE_DAYS.get(time_range)
    if not days:
        return "", ""
    today = date.today()
    return (today - timedelta(days=days)).isoformat(), today.isoformat()


def build_case(row: dict[str, str]) -> dict[str, object]:
    benchmark_id = row["benchmark_id"]
    query = row["query"]
    mode = map_mysearch_mode(row)
    strategy = map_strategy(row)
    sources_hint = parse_pipe_list(row.get("sources_hint", ""))
    strict_domains = parse_pipe_list(row.get("include_domains", "")) or OFFICIAL_DOMAINS.get(benchmark_id, [])
    exclude_domains = parse_pipe_list(row.get("exclude_domains", ""))
    repeat_runs = max(1, int((row.get("repeat_runs") or "1").strip()))
    latency_budget_ms = max(0.0, float((row.get("latency_budget_ms") or "0").strip()))

    if row["preferred_tool"] == "extract_url":
        return {
            "benchmark_id": benchmark_id,
            "domain": row["domain"],
            "query": query,
            "prompt_variant": row["prompt_variant"],
            "repeat_runs": repeat_runs,
            "latency_budget_ms": latency_budget_ms,
            "active_dimensions": active_dimensions(row),
            "mysearch_tool": "extract_url",
            "mysearch_mode": "extract",
            "mysearch_args": {
                "url": query,
                "only_main_content": True,
            },
            "tavily_tool": "tavily_extract",
            "tavily_args": {
                "urls": [query],
                "extract_depth": "advanced",
                "format": "markdown",
            },
        }

    if row["preferred_tool"] == "map_site":
        return {
            "benchmark_id": benchmark_id,
            "domain": row["domain"],
            "query": query,
            "prompt_variant": row["prompt_variant"],
            "repeat_runs": 1,
            "latency_budget_ms": latency_budget_ms,
            "active_dimensions": active_dimensions(row),
            "mysearch_tool": "map_site",
            "mysearch_mode": "map",
            "mysearch_args": {
                "url": query,
                "limit": 10,
            },
            "tavily_tool": "tavily_map",
            "tavily_args": {
                "url": query,
                "limit": 10,
                "max_depth": 1,
                "max_breadth": 10,
            },
        }

    if row["preferred_tool"] == "crawl_site":
        return {
            "benchmark_id": benchmark_id,
            "domain": row["domain"],
            "query": query,
            "prompt_variant": row["prompt_variant"],
            "repeat_runs": 1,
            "latency_budget_ms": latency_budget_ms,
            "active_dimensions": active_dimensions(row),
            "mysearch_tool": "crawl_site",
            "mysearch_mode": "crawl",
            "mysearch_args": {
                "url": query,
                "limit": 5,
                "max_depth": 1,
            },
            "tavily_tool": "tavily_crawl",
            "tavily_args": {
                "url": query,
                "limit": 5,
                "max_depth": 1,
                "max_breadth": 10,
                "format": "markdown",
                "extract_depth": "basic",
            },
        }

    if row["preferred_tool"] == "research":
        mysearch_args: dict[str, object] = {
            "query": query,
            "mode": "research",
            "strategy": strategy,
            "web_max_results": 6 if row["domain"] in {"长尾研究 / 学术比较"} else 5,
            "social_max_results": 5 if row["domain"] in {"纯 Social / X"} else 3,
            "scrape_top_n": 4 if strategy == "deep" else 3,
            "include_social": row["domain"] in {"新闻", "娱乐", "八卦", "技术动态 / status", "纯 Social / X"},
        }
        if strict_domains:
            mysearch_args["include_domains"] = strict_domains
        if exclude_domains:
            mysearch_args["exclude_domains"] = exclude_domains
        return {
            "benchmark_id": benchmark_id,
            "domain": row["domain"],
            "query": query,
            "prompt_variant": row["prompt_variant"],
            "repeat_runs": repeat_runs,
            "latency_budget_ms": latency_budget_ms,
            "active_dimensions": active_dimensions(row),
            "mysearch_tool": "research",
            "mysearch_mode": "research",
            "mysearch_args": mysearch_args,
            "tavily_tool": "tavily_research",
            "tavily_args": {
                "input": query,
                "model": "mini",
            },
        }

    content_fidelity_active = "content_fidelity" in active_dimensions(row)
    mysearch_args: dict[str, object] = {
        "query": query,
        "mode": mode,
        "strategy": strategy,
        "max_results": 5,
        "include_answer": True,
        "include_content": parse_optional_bool(
            row.get("include_content"),
            content_fidelity_active or mode in {"docs", "github", "pdf"},
        ),
    }
    if strict_domains:
        mysearch_args["include_domains"] = strict_domains
    if exclude_domains:
        mysearch_args["exclude_domains"] = exclude_domains
    if sources_hint:
        mysearch_args["sources"] = sources_hint
    elif mode == "social":
        mysearch_args["sources"] = ["x"]
    mysearch_from_date, mysearch_to_date = map_mysearch_date_bounds(row)
    if mysearch_from_date:
        mysearch_args["from_date"] = mysearch_from_date
        mysearch_args["to_date"] = mysearch_to_date

    tavily_args: dict[str, object] = {
        "query": query,
        "max_results": 5,
        "search_depth": map_tavily_search_depth(row),
        "include_raw_content": content_fidelity_active,
        "include_images": False,
        "include_image_descriptions": False,
        # 这里**不能**加 include_answer：本仓 runner 的 mysearch_args 侧传
        # include_answer=True，看上去像不对称，但 tavily_search MCP 工具
        # 不接受该参数（线上 tools/list 实测，参数只有 country / end_date /
        # exact_match / exclude_domains / include_domains / include_favicon /
        # include_image_descriptions / include_images / include_raw_content /
        # max_results / query / search_depth / start_date / time_range / topic）。
        # 传了会让每次调用返回 -32603 validation error，把整个对照侧打挂。
        # 且 Tavily 本来就会自合成 answer（loop33 健康 run 里 tavily_summary
        # 45/45 非空），所以此处没有公平性缺口。
    }
    time_range = map_tavily_time_range(row)
    if time_range:
        tavily_args["time_range"] = time_range
    if strict_domains:
        tavily_args["include_domains"] = strict_domains
    if exclude_domains:
        tavily_args["exclude_domains"] = exclude_domains

    return {
        "benchmark_id": benchmark_id,
        "domain": row["domain"],
        "query": query,
        "prompt_variant": row["prompt_variant"],
        "repeat_runs": repeat_runs,
        "latency_budget_ms": latency_budget_ms,
        "active_dimensions": active_dimensions(row),
        "mysearch_tool": "search",
        "mysearch_mode": mode,
        "mysearch_args": mysearch_args,
        "tavily_tool": "tavily_search",
        "tavily_args": tavily_args,
    }


REMOTE_SCRIPT = r"""
import base64
import json
import re
import sys
import time
import urllib.error
import urllib.request


def parse_mcp_payload(raw):
    text = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
    text = text.strip()
    if not text:
        return {}
    if text.startswith("event:") or "\ndata:" in text:
        text = re.sub(r"(?m)^:.*(?:\n|$)", "", text)
        text = re.sub(r"(?m)^(event|id|retry):.*(?:\n|$)", "", text)
        text = re.sub(r"(?m)^data:\s?", "", text)
        text = text.replace("\r", "").replace("\n", "")
        text = text.strip()
    if not text:
        return {}
    return json.loads(text)


def parse_tool_content_text(result_payload):
    result = result_payload.get("result", {})
    content = result.get("content", [])
    if not content:
        return "", {}
    text = content[0].get("text", "")
    if isinstance(text, str) and text.strip().startswith("Error executing tool"):
        return text, {"_text": text, "_tool_error": text}
    try:
        return text, json.loads(text)
    except Exception:
        return text, {"_text": text}


def is_recoverable_mcp_session_error(error_text):
    lowered = str(error_text or "").lower()
    return (
        "session not found" in lowered
        or "missing mcp-session-id" in lowered
        or "session_required" in lowered
        or "must include mcp-session-id" in lowered
        or "session_unavailable" in lowered
        or "please reconnect to initialize a new session" in lowered
    )


def is_tavily_comparator_limit_error(tool_name, error_text):
    lowered = str(error_text or "").lower()
    return str(tool_name or "").startswith("tavily") and any(
        token in lowered
        for token in (
            "quota_exhausted",
            "http 429",
            "excessive requests",
            "rate limit",
            "usage limit",
            "plan limit",
        )
    )


def should_preserve_captured_tavily_error(run_status, tool_name, error_text):
    return str(run_status or "") == "captured" and is_tavily_comparator_limit_error(tool_name, error_text)


def is_nonfatal_tavily_repeat_error(tool_name, error_text):
    return is_tavily_comparator_limit_error(tool_name, error_text)


def first_nonempty(*values):
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def extract_urls_from_text(text):
    if not isinstance(text, str) or not text.strip():
        return []
    urls = []
    for match in re.findall(r"https?://[^\s<>\]\)]+", text):
        cleaned = match.rstrip(".,);:!?")
        if cleaned:
            urls.append(cleaned)
    deduped = []
    seen = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def collect_urls(blob):
    urls = []
    if isinstance(blob, dict):
        if isinstance(blob.get("url"), str) and blob["url"]:
            urls.append(blob["url"])
        for key in ("results", "citations", "pages", "sources", "items", "links"):
            value = blob.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item:
                        urls.append(item)
                    elif isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"]:
                        urls.append(item["url"])
        urls.extend(extract_urls_from_text(blob.get("content", "")))
    deduped = []
    seen = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped[:3]


def collect_duplicate_url_count(blob):
    # Count items within a collection whose URL names a page the same
    # collection already listed. Collapses trailing slashes and host case so
    # /pricing and /pricing/ count as one page, mirroring the runtime's
    # `_result_url_identity`. Fragments are kept: `#overview` and
    # `#model-availability` are distinct deep links, not duplicates.
    # Collections are counted independently because `citations` is a superset
    # of `results` in a MySearch response, so one shared `seen` set would
    # report every ordinary overlap as a duplicate.
    # Observation only: this does not feed any dimension score.
    if not isinstance(blob, dict):
        return 0
    duplicates = 0
    for key in ("results", "pages", "sources", "items", "citations", "links"):
        value = blob.get(key)
        if not isinstance(value, list):
            continue
        seen = set()
        for item in value:
            url = item if isinstance(item, str) else (
                item.get("url") if isinstance(item, dict) else None
            )
            if not isinstance(url, str) or not url.strip():
                continue
            parsed = urllib.parse.urlparse(url.strip().lower())
            if not parsed.netloc:
                continue
            identity = urllib.parse.urlunparse(
                parsed if not parsed.path else parsed._replace(path=parsed.path.rstrip("/"))
            )
            if identity in seen:
                duplicates += 1
            else:
                seen.add(identity)
    return duplicates


def collect_citation_count(blob):
    if not isinstance(blob, dict):
        return 0
    for key in ("citations", "results", "pages", "sources", "items", "links"):
        value = blob.get(key)
        if isinstance(value, list) and value:
            return len(value)
    extracted = extract_urls_from_text(blob.get("content", ""))
    if extracted:
        return len(extracted)
    return 0


def extract_error_summary(blob):
    if not isinstance(blob, dict):
        return ""
    detail = blob.get("detail")
    detail_text = ""
    if isinstance(detail, dict):
        detail_text = first_nonempty(
            detail.get("error", ""),
            detail.get("message", ""),
            detail.get("detail", ""),
        )
    return first_nonempty(
        blob.get("error", ""),
        blob.get("message", ""),
        detail_text,
    )


def collect_conflicts(blob):
    if not isinstance(blob, dict):
        return []
    candidates = []
    value = blob.get("conflicts")
    if isinstance(value, list):
        candidates.extend(str(item).strip() for item in value if str(item).strip())
    evidence = blob.get("evidence")
    if isinstance(evidence, dict):
        ev_conflicts = evidence.get("conflicts")
        if isinstance(ev_conflicts, list):
            candidates.extend(str(item).strip() for item in ev_conflicts if str(item).strip())
    deduped = []
    seen = set()
    for item in candidates:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped[:6]


def extract_official_mode(blob):
    if not isinstance(blob, dict):
        return ""
    value = blob.get("official_mode")
    if isinstance(value, str) and value.strip():
        return value.strip()
    evidence = blob.get("evidence")
    if isinstance(evidence, dict):
        value = evidence.get("official_mode")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def fallback_metadata(blob):
    attempted = False
    used = False
    reasons = []
    if not isinstance(blob, dict):
        return attempted, used, ""
    for container in walk_dicts(blob):
        fallback = container.get("fallback")
        if not isinstance(fallback, dict):
            continue
        has_state = any(key in fallback for key in ("configured", "triggered", "used"))
        reason = str(fallback.get("reason") or "").strip()
        fallback_from = str(fallback.get("from") or "").strip()
        fallback_to = str(fallback.get("to") or "").strip()
        entry_attempted = bool(fallback.get("triggered") or fallback.get("used"))
        if not has_state:
            entry_attempted = bool(reason or fallback_from or fallback_to)
        entry_used = bool(fallback.get("used")) if has_state else bool(entry_attempted and fallback_to)
        attempted = attempted or entry_attempted
        used = used or entry_used
        if entry_attempted:
            detail = reason or " -> ".join(part for part in (fallback_from, fallback_to) if part)
            if detail and detail not in reasons:
                reasons.append(detail)
    return attempted, used, " | ".join(reasons)


def orchestration_used(blob):
    if not isinstance(blob, dict):
        return False
    providers = set()
    for item in walk_dicts(blob):
        provider = item.get("provider")
        if isinstance(provider, str) and provider.strip():
            if provider.strip().lower() == "hybrid":
                return True
            providers.add(provider.strip())
        for key in ("providers_consulted", "matched_providers"):
            values = item.get(key)
            if isinstance(values, list):
                providers.update(str(value).strip() for value in values if str(value).strip())
        route_provider = item.get("route_provider")
        if isinstance(route_provider, str) and route_provider.strip().lower() == "hybrid":
            return True
        selected = item.get("selected")
        if isinstance(selected, str) and "+" in selected:
            return True
    return len(providers) > 1


def collect_published_date_count(blob):
    count = 0
    for item in walk_dicts(blob):
        for key in ("published_date", "published_at", "date"):
            if isinstance(item.get(key), str) and item[key].strip():
                count += 1
                break
    return count


def collection_summary(blob):
    if not isinstance(blob, dict):
        return ""
    for key, label in (
        ("links", "Mapped URLs"),
        ("pages", "Crawled pages"),
        ("results", "Results"),
    ):
        value = blob.get(key)
        if isinstance(value, list):
            count = blob.get("count") if isinstance(blob.get("count"), int) else len(value)
            return f"{label}: {count}"
    return ""


def content_metrics(blob):
    if not isinstance(blob, dict):
        return {"char_count": 0, "item_count": 0, "noise_hits": 0}

    values = []
    seen = set()

    def add(value):
        if not isinstance(value, str):
            return
        normalized = value.strip()
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        values.append(normalized)

    for key in ("content", "raw_content", "markdown", "text", "report", "research_summary"):
        add(blob.get(key))
    for collection_key in ("results", "pages"):
        collection = blob.get(collection_key)
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, dict):
                continue
            raw_content = item.get("raw_content")
            if isinstance(raw_content, str) and raw_content.strip():
                add(raw_content)
                continue
            # `snippet` carries MySearch's per-result provider text -- the same
            # role Tavily puts in `content`. Without it the two sides are not
            # comparable: on rows where content_fidelity is inactive, Tavily's
            # `content` still holds its snippet while MySearch keeps that same
            # text in `snippet`, so MySearch scored 0 while Tavily scored ~4-9k.
            for key in ("content", "markdown", "text", "snippet"):
                add(item.get(key))

    noise_markers = (
        "hcaptcha",
        "captcha challenge",
        "verify you are human",
        "checking your browser",
        "attention required | cloudflare",
    )
    noise_hits = sum(
        1
        for value in values
        for marker in noise_markers
        if marker in value.lower()
    )
    return {
        "char_count": sum(len(value) for value in values),
        "item_count": len(values),
        "noise_hits": noise_hits,
    }


def summarize(blob):
    if not isinstance(blob, dict):
        return {
            "summary": "",
            "urls": [],
            "provider_trace": "",
            "citation_count": 0,
            "official_mode": "",
            "conflicts": [],
            "empty_result": True,
            "orchestration_used": False,
            "fallback_attempted": False,
            "fallback_reason": "",
            "fallback_used": False,
        }
    results = blob.get("results")
    first_result = results[0] if isinstance(results, list) and results else {}
    summary = first_nonempty(
        blob.get("answer", ""),
        blob.get("summary", ""),
        blob.get("research_summary", ""),
        blob.get("report", ""),
        blob.get("response", ""),
        blob.get("output", "") if isinstance(blob.get("output"), str) else "",
        blob.get("xai_arbitration_summary", ""),
        first_result.get("snippet", "") if isinstance(first_result, dict) else "",
        first_result.get("content", "") if isinstance(first_result, dict) else "",
        blob.get("content", ""),
        blob.get("text", ""),
        collection_summary(blob),
        extract_error_summary(blob),
        blob.get("_text", ""),
        blob.get("server_name", ""),
    )
    urls = collect_urls(blob)
    fallback_attempted, fallback_used, fallback_reason = fallback_metadata(blob)
    used_orchestration = orchestration_used(blob)
    published_date_count = collect_published_date_count(blob)
    measured_content = content_metrics(blob)
    trace_blob = {}
    for key in (
        "provider",
        "providers_consulted",
        "matched_providers",
        "route_debug",
        "evidence",
        "official_mode",
        "route",
        "fallback",
    ):
        if key in blob:
            trace_blob[key] = blob[key]
    provider_trace = json.dumps(trace_blob, ensure_ascii=False) if trace_blob else ""
    return {
        "summary": summary[:500],
        "urls": urls,
        "provider_trace": provider_trace,
        "citation_count": collect_citation_count(blob),
        "content_char_count": measured_content["char_count"],
        "content_item_count": measured_content["item_count"],
        "content_noise_hits": measured_content["noise_hits"],
        "duplicate_url_count": collect_duplicate_url_count(blob),
        "published_date_count": published_date_count,
        "official_mode": extract_official_mode(blob),
        "conflicts": collect_conflicts(blob),
        "empty_result": not urls and not summary.strip(),
        "orchestration_used": used_orchestration,
        "fallback_attempted": fallback_attempted,
        "fallback_reason": fallback_reason,
        "fallback_used": fallback_used,
    }


def classify_tavily_structural_failure(raw_text, benchmark_id):
    if not isinstance(raw_text, str) or not raw_text.strip():
        return ""
    try:
        blob = json.loads(raw_text)
    except Exception:
        return ""
    if not isinstance(blob, dict):
        return ""
    status = blob.get("status")
    detail = blob.get("detail")
    detail_text = ""
    if isinstance(detail, dict):
        detail_text = first_nonempty(detail.get("error", ""), detail.get("message", ""))
    lowered = f"{status} {blob.get('error', '')} {detail_text}".lower()
    if "research" not in str(benchmark_id).lower():
        return ""
    if "excessive requests" in lowered or str(status) == "429":
        return "tavily-research-upstream-rate-limited"
    if "usage limit" in lowered or str(status) == "432":
        return "tavily-research-upstream-plan-limited"
    return ""


class MCPClient:
    def __init__(self, url, headers=None):
        self.url = url
        self.headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        self.headers.update(headers or {})
        self.session_id = None

    def _post(self, payload, headers, timeout, retries=4):
        data = json.dumps(payload).encode()
        last_error = None
        for attempt in range(retries):
            req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.headers, parse_mcp_payload(resp.read())
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode(errors="replace")
                except Exception:
                    body = ""
                last_error = RuntimeError(f"HTTP {exc.code}: {body[:300]}")
                if exc.code in {429, 500, 502, 503, 504} and attempt + 1 < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last_error
            except Exception as exc:
                last_error = exc
                if attempt + 1 < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise RuntimeError("unreachable post retry state")

    def initialize(self):
        payload = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {
                    "name": "codex-mcp-client",
                    "title": "Codex",
                    "version": "0.116.0",
                },
            },
        }
        headers, _ = self._post(payload, self.headers, timeout=20, retries=5)
        self.session_id = headers.get("mcp-session-id")
        notif_headers = dict(self.headers)
        if self.session_id:
            notif_headers["mcp-session-id"] = self.session_id
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        self._post(notif, notif_headers, timeout=20, retries=5)

    def call_tool(self, tool_name, arguments):
        payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        headers = dict(self.headers)
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        try:
            _, response_payload = self._post(payload, headers, timeout=120, retries=4)
            return response_payload
        except Exception as exc:
            if not is_recoverable_mcp_session_error(str(exc)):
                raise
        self.initialize()
        headers = dict(self.headers)
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        _, response_payload = self._post(payload, headers, timeout=120, retries=4)
        return response_payload


def normalize_summary(value):
    return " ".join(str(value or "").lower().split())


# 远端自包含的数值判据（REMOTE_SCRIPT 里取不到本地的 _as_float）。
# 注意：本字符串内不要用三引号 docstring，会提前终止外层 REMOTE_SCRIPT。
def _positive_number(value):
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def repeat_variance(observations):
    successful = [item for item in observations if item.get("success")]
    attempted_count = len(observations)
    # 空结果不是成功。上游可以在 HTTP 200 下返回空 results（Tavily 实测如此），
    # 而 `success` 只看传输层，于是"什么都没返回"会算进成功数。更糟的是：全空运行
    # 会让 consistency_parts 为空 -> consistency 落到默认 1.0，配合 success_ratio=1.0
    # 反而拿满分。loop27 的 research-01 / longtail-academic-01 就是这样在全部重复
    # 都只返回 "Research request failed" 的情况下 resilience 得 5.0，而 runner 同时
    # 已把它们标为 tavily-*-upstream-plan-limited。
    #
    # 判据取"有无任何可用产出"：URL 或正文文本。实测分界很干净 —— 正当的 research
    # 综合答案虽不带 URL，但有 8.5k~16k 正文；错误串只有 10~23 字符且正文为 0。
    # 因此不能只判 urls（会误杀前者），也不能只判 summary 非空（错误串正是非空的）。
    status_ok = [
        item
        for item in successful
        if item.get("urls") or _positive_number(item.get("content_char_count"))
    ]
    success_ratio = len(status_ok) / attempted_count if attempted_count else 0.0
    summary_match_rate = 1.0
    url_overlap = 1.0
    consistency_parts = []
    if len(status_ok) >= 2:
        first_summary = normalize_summary(status_ok[0].get("summary"))
        summaries = [normalize_summary(item.get("summary")) for item in status_ok[1:]]
        if first_summary or any(summaries):
            summary_match_rate = sum(value == first_summary for value in summaries) / len(summaries)
            consistency_parts.append(summary_match_rate)
        first_urls = set(status_ok[0].get("urls") or [])
        overlaps = []
        for item in status_ok[1:]:
            current_urls = set(item.get("urls") or [])
            union = first_urls | current_urls
            if union:
                overlaps.append(len(first_urls & current_urls) / len(union))
        if overlaps:
            url_overlap = sum(overlaps) / len(overlaps)
            consistency_parts.append(url_overlap)
    consistency = sum(consistency_parts) / len(consistency_parts) if consistency_parts else 1.0
    latencies = [float(item["latency_ms"]) for item in observations if item.get("latency_ms") is not None]
    return {
        "latency_range_ms": round(max(latencies) - min(latencies), 1) if len(latencies) >= 2 else 0.0,
        "result_stability": round(consistency * success_ratio, 3),
        "summary_match_rate": round(summary_match_rate, 3),
        "url_overlap": round(url_overlap, 3),
        "successful_runs": len(successful),
        "nonempty_runs": len(status_ok),
        "attempted_runs": attempted_count,
    }


def timed_tool_runs(client, tool_name, arguments, repeat_runs, latency_budget_ms=0):
    latencies = []
    errors = []
    timeout_flag = False
    first_success = None
    raw_text = ""
    observations = []
    fallback_reasons = []
    used_orchestration = False
    fallback_attempted = False
    fallback_used = False
    for run_index in range(max(1, int(repeat_runs or 1))):
        started = time.perf_counter()
        try:
            payload = client.call_tool(tool_name, arguments)
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            text, blob = parse_tool_content_text(payload)
            tool_error = blob.get("_tool_error") if isinstance(blob, dict) else None
            if isinstance(tool_error, str) and tool_error.strip():
                message = tool_error.strip()
                observations.append(
                    {
                        "run": run_index + 1,
                        "cache_state": "cold" if run_index == 0 else "warm",
                        "success": False,
                        "latency_ms": elapsed_ms,
                        "summary": "",
                        "urls": [],
                        "error": message[:500],
                    }
                )
                if first_success is not None and is_nonfatal_tavily_repeat_error(tool_name, message):
                    continue
                errors.append(message)
                if "timed out" in tool_error.lower() or "timeout" in tool_error.lower():
                    timeout_flag = True
                continue
            summarized = summarize(blob)
            latencies.append(elapsed_ms)
            observations.append(
                {
                    "run": run_index + 1,
                    "cache_state": "cold" if run_index == 0 else "warm",
                    "success": True,
                    "latency_ms": elapsed_ms,
                    "summary": summarized["summary"],
                    "urls": summarized["urls"],
                    "citation_count": summarized["citation_count"],
                    "content_char_count": summarized["content_char_count"],
                    "content_item_count": summarized["content_item_count"],
                    "content_noise_hits": summarized["content_noise_hits"],
                    "duplicate_url_count": summarized["duplicate_url_count"],
                    "empty_result": summarized["empty_result"],
                }
            )
            used_orchestration = used_orchestration or summarized["orchestration_used"]
            fallback_attempted = fallback_attempted or summarized["fallback_attempted"]
            fallback_used = fallback_used or summarized["fallback_used"]
            if summarized["fallback_reason"] and summarized["fallback_reason"] not in fallback_reasons:
                fallback_reasons.append(summarized["fallback_reason"])
            if first_success is None:
                first_success = summarized
                raw_text = text
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            message = str(exc)
            observations.append(
                {
                    "run": run_index + 1,
                    "cache_state": "cold" if run_index == 0 else "warm",
                    "success": False,
                    "latency_ms": elapsed_ms,
                    "summary": "",
                    "urls": [],
                    "error": message[:500],
                }
            )
            if first_success is not None and is_nonfatal_tavily_repeat_error(tool_name, message):
                continue
            errors.append(message)
            if "timed out" in message.lower():
                timeout_flag = True
    if first_success is not None and str(tool_name or "").startswith("tavily"):
        errors = [message for message in errors if not is_tavily_comparator_limit_error(tool_name, message)]
    if first_success is None:
        raise RuntimeError(" ; ".join(errors[:3]) or "all repeats failed")
    variance = repeat_variance(observations)
    observation_latencies = [float(item["latency_ms"]) for item in observations]
    warm_latencies = observation_latencies[1:]
    budget_ms = float(latency_budget_ms or 0)
    return {
        "summary": first_success["summary"],
        "urls": first_success["urls"],
        "provider_trace": first_success["provider_trace"],
        "citation_count": first_success["citation_count"],
        "content_char_count": first_success["content_char_count"],
        "content_item_count": first_success["content_item_count"],
        "content_noise_hits": first_success["content_noise_hits"],
        "duplicate_url_count": first_success["duplicate_url_count"],
        "published_date_count": first_success["published_date_count"],
        "official_mode": first_success["official_mode"],
        "conflicts": first_success["conflicts"],
        "empty_result": first_success["empty_result"],
        "orchestration_used": used_orchestration,
        "fallback_attempted": fallback_attempted,
        "fallback_reason": " | ".join(fallback_reasons),
        "fallback_used": fallback_used,
        "latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
        "repeat_variance": json.dumps(variance, ensure_ascii=False, sort_keys=True),
        "repeat_observations": json.dumps(observations, ensure_ascii=False),
        "cold_latency_ms": observation_latencies[0] if observation_latencies else 0.0,
        "warm_latency_ms": round(sum(warm_latencies) / len(warm_latencies), 1) if warm_latencies else "",
        "latency_budget_exceeded": bool(budget_ms and any(value > budget_ms for value in observation_latencies)),
        "timeout": timeout_flag,
        "partial_error": bool(errors),
        "error": " ; ".join(errors[:3]),
        "raw_text": raw_text,
    }


payload = json.loads(base64.b64decode(PAYLOAD_B64).decode())
mysearch = None
mysearch_init_error = ""
try:
    mysearch = MCPClient(payload["mysearch_url"])
    mysearch.initialize()
except Exception as exc:
    mysearch_init_error = str(exc)
tavily = None
tavily_init_error = ""
if not payload.get("mysearch_only"):
    try:
        tavily = MCPClient(payload["tavily_url"], {"Authorization": f'Bearer {payload["tavily_bearer"]}'})
        tavily.initialize()
    except Exception as exc:
        tavily_init_error = str(exc)

output = []
for case in payload["cases"]:
    row = {
        "benchmark_id": case["benchmark_id"],
        "domain": case.get("domain", ""),
        "query": case.get("query", ""),
        "prompt_variant": case.get("prompt_variant", ""),
        "active_dimensions": case.get("active_dimensions", ""),
        "run_status": "captured",
        "latency_budget_ms": case.get("latency_budget_ms", 0),
        "mysearch_tool": case["mysearch_tool"],
        "mysearch_mode": case.get("mysearch_mode", ""),
        "mysearch_summary": "",
        "mysearch_top_urls": "",
        "mysearch_provider_trace": "",
        "mysearch_citation_count": 0,
        "mysearch_content_char_count": 0,
        "mysearch_content_item_count": 0,
        "mysearch_content_noise_hits": 0,
        "mysearch_duplicate_url_count": 0,
        "mysearch_published_date_count": 0,
        "mysearch_official_mode": "",
        "mysearch_conflicts": "",
        "mysearch_latency_ms": "",
        "mysearch_repeat_variance": "",
        "mysearch_repeat_observations": "",
        "mysearch_cold_latency_ms": "",
        "mysearch_warm_latency_ms": "",
        "mysearch_latency_budget_exceeded": False,
        "mysearch_empty_result": False,
        "mysearch_timeout": False,
        "mysearch_orchestration_used": False,
        "mysearch_fallback_attempted": False,
        "mysearch_fallback_reason": "",
        "mysearch_fallback_used": False,
        "tavily_tool": case.get("tavily_tool", ""),
        "tavily_provider_trace": "",
        "tavily_summary": "",
        "tavily_top_urls": "",
        "tavily_citation_count": 0,
        "tavily_content_char_count": 0,
        "tavily_content_item_count": 0,
        "tavily_content_noise_hits": 0,
        "tavily_duplicate_url_count": 0,
        "tavily_published_date_count": 0,
        "tavily_latency_ms": "",
        "tavily_repeat_variance": "",
        "tavily_repeat_observations": "",
        "tavily_cold_latency_ms": "",
        "tavily_warm_latency_ms": "",
        "tavily_latency_budget_exceeded": False,
        "tavily_empty_result": False,
        "tavily_timeout": False,
        "tavily_orchestration_used": False,
        "tavily_fallback_attempted": False,
        "tavily_fallback_reason": "",
        "tavily_fallback_used": False,
        "error": "",
    }
    repeat_runs = case.get("repeat_runs", 1)
    latency_budget_ms = case.get("latency_budget_ms", 0)

    if mysearch is None:
        row["run_status"] = "partial-error"
        row["error"] = f"mysearch-init: {mysearch_init_error}"
        row["mysearch_raw"] = json.dumps({"error": mysearch_init_error, "phase": "initialize"}, ensure_ascii=False)
    else:
        try:
            observed = timed_tool_runs(
                mysearch,
                case["mysearch_tool"],
                case["mysearch_args"],
                repeat_runs,
                latency_budget_ms,
            )
            row["mysearch_summary"] = observed["summary"]
            row["mysearch_top_urls"] = " | ".join(observed["urls"])
            row["mysearch_provider_trace"] = observed["provider_trace"]
            row["mysearch_citation_count"] = observed["citation_count"]
            row["mysearch_content_char_count"] = observed["content_char_count"]
            row["mysearch_content_item_count"] = observed["content_item_count"]
            row["mysearch_content_noise_hits"] = observed["content_noise_hits"]
            row["mysearch_duplicate_url_count"] = observed["duplicate_url_count"]
            row["mysearch_published_date_count"] = observed["published_date_count"]
            row["mysearch_official_mode"] = observed["official_mode"]
            row["mysearch_conflicts"] = " | ".join(observed["conflicts"])
            row["mysearch_latency_ms"] = observed["latency_ms"]
            row["mysearch_repeat_variance"] = observed["repeat_variance"]
            row["mysearch_repeat_observations"] = observed["repeat_observations"]
            row["mysearch_cold_latency_ms"] = observed["cold_latency_ms"]
            row["mysearch_warm_latency_ms"] = observed["warm_latency_ms"]
            row["mysearch_latency_budget_exceeded"] = observed["latency_budget_exceeded"]
            row["mysearch_empty_result"] = observed["empty_result"]
            row["mysearch_timeout"] = observed["timeout"]
            row["mysearch_orchestration_used"] = observed["orchestration_used"]
            row["mysearch_fallback_attempted"] = observed["fallback_attempted"]
            row["mysearch_fallback_reason"] = observed["fallback_reason"]
            row["mysearch_fallback_used"] = observed["fallback_used"]
            row["mysearch_raw"] = observed["raw_text"]
            if observed["partial_error"]:
                row["run_status"] = "partial-error"
                row["error"] = f"mysearch-repeat: {observed['error']}"
        except Exception as exc:
            row["run_status"] = "partial-error"
            row["error"] = f"mysearch: {exc}"
            row["mysearch_raw"] = ""

    if tavily is not None:
        try:
            observed = timed_tool_runs(
                tavily,
                case["tavily_tool"],
                case["tavily_args"],
                repeat_runs,
                latency_budget_ms,
            )
            row["tavily_provider_trace"] = observed["provider_trace"]
            row["tavily_summary"] = observed["summary"]
            row["tavily_top_urls"] = " | ".join(observed["urls"])
            row["tavily_citation_count"] = observed["citation_count"]
            row["tavily_content_char_count"] = observed["content_char_count"]
            row["tavily_content_item_count"] = observed["content_item_count"]
            row["tavily_content_noise_hits"] = observed["content_noise_hits"]
            row["tavily_duplicate_url_count"] = observed["duplicate_url_count"]
            row["tavily_published_date_count"] = observed["published_date_count"]
            row["tavily_latency_ms"] = observed["latency_ms"]
            row["tavily_repeat_variance"] = observed["repeat_variance"]
            row["tavily_repeat_observations"] = observed["repeat_observations"]
            row["tavily_cold_latency_ms"] = observed["cold_latency_ms"]
            row["tavily_warm_latency_ms"] = observed["warm_latency_ms"]
            row["tavily_latency_budget_exceeded"] = observed["latency_budget_exceeded"]
            row["tavily_empty_result"] = observed["empty_result"]
            row["tavily_timeout"] = observed["timeout"]
            row["tavily_orchestration_used"] = observed["orchestration_used"]
            row["tavily_fallback_attempted"] = observed["fallback_attempted"]
            row["tavily_fallback_reason"] = observed["fallback_reason"]
            row["tavily_fallback_used"] = observed["fallback_used"]
            row["tavily_raw"] = observed["raw_text"]
            if observed["partial_error"]:
                row["run_status"] = "partial-error" if row["run_status"] == "captured" else row["run_status"]
                row["error"] = (row["error"] + " ; " if row["error"] else "") + f"tavily-repeat: {observed['error']}"
        except Exception as exc:
            error_text = str(exc)
            if not should_preserve_captured_tavily_error(row["run_status"], case["tavily_tool"], error_text):
                row["run_status"] = "partial-error" if row["run_status"] == "captured" else "error"
            row["error"] = (row["error"] + " ; " if row["error"] else "") + f"tavily: {error_text}"
            row["tavily_raw"] = json.dumps({"error": error_text, "phase": "tools/call"}, ensure_ascii=False)
    elif tavily_init_error:
        if not should_preserve_captured_tavily_error(row["run_status"], case["tavily_tool"], tavily_init_error):
            row["run_status"] = "partial-error" if row["run_status"] == "captured" else row["run_status"]
        row["error"] = (row["error"] + " ; " if row["error"] else "") + f"tavily-init: {tavily_init_error}"
        row["tavily_raw"] = json.dumps({"error": tavily_init_error, "phase": "initialize"}, ensure_ascii=False)
    if row["run_status"] == "captured" and (
        row["mysearch_latency_budget_exceeded"] or row["tavily_latency_budget_exceeded"]
    ):
        row["run_status"] = "budget-exceeded"
    output.append(row)

print(json.dumps(output, ensure_ascii=False))
"""


def run_remote_cases(
    host: str,
    mysearch_url: str,
    tavily_url: str,
    tavily_bearer: str,
    cases: list[dict[str, object]],
    *,
    mysearch_only: bool = False,
) -> list[dict[str, str]]:
    payload = {
        "mysearch_url": mysearch_url,
        "tavily_url": tavily_url,
        "tavily_bearer": tavily_bearer,
        "cases": cases,
        "mysearch_only": mysearch_only,
    }
    payload_b64 = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
    remote_source = f"PAYLOAD_B64 = {payload_b64!r}\n{REMOTE_SCRIPT}"
    cmd = [
        "ssh",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=6",
        host,
        "python3",
        "-",
    ]
    timeout_seconds = estimate_remote_batch_timeout_seconds(cases)
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            input=remote_source,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        timeout_rows: list[dict[str, str]] = []
        message = f"remote-benchmark-timeout after {int(exc.timeout)}s"
        for case in cases:
            timeout_rows.append(
                {
                    "benchmark_id": str(case.get("benchmark_id", "")),
                    "run_status": "partial-error",
                    "error": message,
                    "mysearch_raw": "",
                    "tavily_raw": "",
                }
            )
        return timeout_rows
    parsed_stdout = None
    if proc.stdout.strip():
        try:
            parsed_stdout = json.loads(proc.stdout)
        except json.JSONDecodeError:
            parsed_stdout = None
    if proc.returncode != 0:
        if isinstance(parsed_stdout, list):
            warning = proc.stderr.strip()
            if warning:
                for item in parsed_stdout:
                    if isinstance(item, dict):
                        item["_remote_transport_warning"] = warning
            return parsed_stdout
        raise RuntimeError(
            f"remote benchmark failed with exit {proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    if parsed_stdout is not None:
        return parsed_stdout
    return json.loads(proc.stdout)


def estimate_remote_case_timeout_seconds(case: dict[str, object]) -> int:
    repeat_runs = max(1, int(case.get("repeat_runs", 1) or 1))
    mysearch_tool = str(case.get("mysearch_tool", "") or "")
    tavily_tool = str(case.get("tavily_tool", "") or "")
    if mysearch_tool == "research" or tavily_tool == "tavily_research":
        return max(600, 240 * repeat_runs)
    if mysearch_tool == "crawl_site" or tavily_tool == "tavily_crawl":
        return max(600, 240 * repeat_runs)
    if mysearch_tool == "map_site" or tavily_tool == "tavily_map":
        return max(420, 150 * repeat_runs)
    if mysearch_tool == "extract_url" or tavily_tool == "tavily_extract":
        return max(420, 150 * repeat_runs)
    if repeat_runs >= 3:
        return 360
    return 300


def estimate_remote_batch_timeout_seconds(cases: list[dict[str, object]]) -> int:
    if not cases:
        return 300
    return max(300, sum(estimate_remote_case_timeout_seconds(case) for case in cases))


def write_raw(raw_dir: Path, benchmark_id: str, provider: str, text: str) -> str:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{benchmark_id}.{provider}.json"
    path.write_text(text, encoding="utf-8")
    return str(path)


def is_recoverable_mcp_session_error(error_text: str) -> bool:
    lowered = str(error_text or "").lower()
    return (
        "session not found" in lowered
        or "missing mcp-session-id" in lowered
        or "session_required" in lowered
        or "must include mcp-session-id" in lowered
        or "session_unavailable" in lowered
        or "please reconnect to initialize a new session" in lowered
    )


def extract_tavily_error_text(error_text: str) -> str:
    chunks = [
        chunk.strip()
        for chunk in str(error_text or "").split(" ; ")
        if chunk.strip()
    ]
    return " ; ".join(
        chunk
        for chunk in chunks
        if chunk.lower().startswith(("tavily:", "tavily-repeat:", "tavily-init:"))
    )


def classify_tavily_structural_failure(
    raw_text: str,
    benchmark_id: str,
    error_text: str = "",
) -> str:
    lowered_error = extract_tavily_error_text(error_text).lower()
    if (
        "quota_exhausted" in lowered_error
        or "excessive requests" in lowered_error
        or "http 429" in lowered_error
    ):
        if "research" in str(benchmark_id).lower():
            return "tavily-research-upstream-rate-limited"
        return "tavily-search-upstream-rate-limited"
    if "usage limit" in lowered_error or "http 432" in lowered_error:
        if "research" in str(benchmark_id).lower():
            return "tavily-research-upstream-plan-limited"
        return "tavily-search-upstream-plan-limited"
    if (
        "timed out awaiting tools/call" in lowered_error
        or " timed out" in lowered_error
        or " timeout" in lowered_error
    ):
        if "tavily_research" in lowered_error or "research" in str(benchmark_id).lower():
            return "tavily-research-tool-timeout"
        return "tavily-search-tool-timeout"
    if (
        "tavily: http 502" in lowered_error
        or lowered_error.count("http 502") >= 2
    ):
        return "tavily-upstream-502"
    if is_recoverable_mcp_session_error(lowered_error) or (
        "mcp-session" in lowered_error and "transport" in lowered_error
    ):
        return "tavily-mcp-session-transport-blocked"
    if not raw_text.strip():
        return ""
    try:
        blob = json.loads(raw_text)
    except Exception:
        return ""
    if not isinstance(blob, dict):
        return ""
    detail = blob.get("detail")
    detail_text = ""
    if isinstance(detail, dict):
        for key in ("error", "message", "detail"):
            value = detail.get(key, "")
            if isinstance(value, str) and value.strip():
                detail_text = value.strip()
                break
    lowered = f"{blob.get('status', '')} {blob.get('error', '')} {detail_text}".lower()
    if "excessive requests" in lowered or str(blob.get("status")) == "429":
        if "research" in str(benchmark_id).lower():
            return "tavily-research-upstream-rate-limited"
        return "tavily-search-upstream-rate-limited"
    if "usage limit" in lowered or str(blob.get("status")) == "432":
        if "research" in str(benchmark_id).lower():
            return "tavily-research-upstream-plan-limited"
        if "extract" in str(benchmark_id).lower():
            return "tavily-extract-upstream-plan-limited"
        return "tavily-search-upstream-plan-limited"
    return ""


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_float(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _json_value(value: object, default: object) -> object:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _row_urls(row: dict[str, object], prefix: str) -> list[str]:
    return [value.strip() for value in str(row.get(f"{prefix}_top_urls", "")).split(" | ") if value.strip()]


def _hostname_matches(hostname: str, domain: str) -> bool:
    normalized_host = hostname.lower().strip(".")
    normalized_domain = domain.lower().strip(".")
    return normalized_host == normalized_domain or normalized_host.endswith(f".{normalized_domain}")


def _url_satisfies_constraints(url: str, include_domains: list[str], exclude_domains: list[str]) -> bool:
    hostname = (urllib.parse.urlparse(url).hostname or "").lower()
    if not hostname:
        return False
    if any(_hostname_matches(hostname, domain) for domain in exclude_domains):
        return False
    return not include_domains or any(_hostname_matches(hostname, domain) for domain in include_domains)


def _trace_provider_names(trace: dict[str, object]) -> set[str]:
    providers: set[str] = set()
    stack: list[object] = [trace]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            provider = value.get("provider")
            if isinstance(provider, str) and provider.strip() and provider.strip().lower() != "hybrid":
                providers.add(provider.strip())
            for key in ("providers_consulted", "matched_providers"):
                items = value.get(key)
                if isinstance(items, list):
                    providers.update(str(item).strip() for item in items if str(item).strip())
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return providers


def _repeat_metrics(row: dict[str, object], prefix: str) -> tuple[float, list[dict[str, object]]]:
    variance = _json_value(row.get(f"{prefix}_repeat_variance"), {})
    observations = _json_value(row.get(f"{prefix}_repeat_observations"), [])
    stability = 1.0
    if isinstance(variance, dict):
        stability = max(0.0, min(1.0, _as_float(variance.get("result_stability", 1.0))))
    typed_observations = [item for item in observations if isinstance(item, dict)] if isinstance(observations, list) else []
    return stability, typed_observations


def _provider_captured(row: dict[str, object], prefix: str) -> bool:
    return bool(
        str(row.get(f"{prefix}_summary", "")).strip()
        or _row_urls(row, prefix)
        or _as_float(row.get(f"{prefix}_citation_count")) > 0
    ) and not _as_bool(row.get(f"{prefix}_empty_result"))


def _clamp_score(value: float) -> float:
    return round(max(0.0, min(5.0, value)), 2)


def _efficiency_score(
    row: dict[str, object],
    prefix: str,
    latency_budget_ms: float,
    observations: list[dict[str, object]],
) -> tuple[float, bool]:
    latencies = [
        _as_float(item.get("latency_ms"))
        for item in observations
        if item.get("latency_ms") not in {None, ""}
    ]
    if not latencies:
        latency = _as_float(row.get(f"{prefix}_latency_ms"))
        if latency:
            latencies.append(latency)
    if not latencies:
        return 0.0, False
    if latency_budget_ms <= 0:
        return 3.0, False
    ratio = max(latencies) / latency_budget_ms
    exceeded = ratio > 1.0
    if exceeded:
        return _clamp_score(2.0 - ((ratio - 1.0) * 4.0)), True
    return _clamp_score(3.0 + (1.0 - ratio) * 2.0), False


def _grounding_tokens(answer: str) -> list[str]:
    """`answer` 里必须能回查出处的事实性 token：版本号与专名。

    只取这两类，是因为它们**可证伪**且**易漂移**：版本号是本轮真实缺陷的载体
    （Java 被答成 `26.1.2`，那是 Minecraft Java Edition 的版本），而专名是
    奖项/产品类答案的载体。普通词汇不取 —— 它们在任何语料里都"能找到"，
    会让这个指标恒真。
    """
    text = str(answer or "")
    if not text.strip():
        return []
    found: list[str] = []
    # 尾部断言只排除"后面还接着数字"（`1.2.3` 这种整体），句末句号必须放行 ——
    # 答案几乎总以 `26.1.2.` 结尾，用 `(?![\w.])` 会把版本号整个漏掉，
    # 使这一项退化成"只查专名"。
    for match in re.finditer(r"(?<![\w.])v?\d+(?:\.\d+)+(?!\.?\d)", text):
        found.append(match.group(0))
    for match in re.finditer(r"\b[A-Z][A-Za-z0-9]*(?:\.[A-Za-z0-9]+)*\b", text):
        token = match.group(0)
        if len(token) >= 3 and token.lower() not in _GROUNDING_STOPWORDS:
            found.append(token)
    # 保序去重，避免重复 token 把比例算歪。
    seen: set[str] = set()
    unique: list[str] = []
    for token in found:
        key = token.lower()
        if key not in seen:
            seen.add(key)
            unique.append(token)
    return unique


def _grounding_haystack(row: dict[str, object], prefix: str) -> str:
    """该 provider **自己**返回的正文，作为回查依据。

    用 `raw_text` 而不是 `summary`：summary 就是被检的 `answer` 本身，
    拿它做依据会变成恒真判定。两侧各查各的，所以对 Tavily 同样成立。
    """
    raw = str(row.get(f"{prefix}_raw", "") or "")
    if not raw:
        return ""
    parts = [raw]
    parsed = _json_value(raw, None)
    if isinstance(parsed, dict):
        # 正文既可能在顶层，也可能在 results[].content/snippet 里。
        parts.append(json.dumps(parsed, ensure_ascii=False))
    return " ".join(parts).lower()


def _claim_groundedness(row: dict[str, object], prefix: str) -> tuple[float, int]:
    """`answer` 里的事实 token 有多少能在这家 provider 的正文里找到。

    返回 `(比例, token 数)`。**没有 answer 或没有事实 token 时返回比例 1.0**：
    不发明断言就不该被扣分 —— 该给分的是 `assertion_pass_rate`，
    这里只惩罚"说了源里没有的话"。

    **实测边界（2026-09-23，必须记住）**：这个指标**抓不住 Java 那个案例** ——
    编造的 `26.1.2` 确实出现在源文本里（那是 Minecraft Java Edition 的版本），
    所以它 grounded=1.00。它能抓住的是另一类：答案里出现**源里根本没有**的 token
    （`4.5` / `10.7.3` 这两个用户报告的旧值正是如此，ratio=0.50）。
    两个缺陷所以需要两个指标：这里管"凭空捏造"，`assertion_pass_rate`
    与主语归属（产品侧的 `_version_is_anchored_to_subject`）管"张冠李戴"。
    """
    answer = str(row.get(f"{prefix}_summary", "") or "").strip()
    tokens = _grounding_tokens(answer)
    if not tokens:
        return 1.0, 0
    haystack = _grounding_haystack(row, prefix)
    if not haystack:
        return 1.0, len(tokens)
    grounded = sum(1 for token in tokens if token.lower() in haystack)
    return grounded / len(tokens), len(tokens)


#: 一条结果里**可以**承载正文的字段名。判据是"是否落在这些字段的任一"，
#: 而不是点名某几个 —— 两侧的字段名本来就不同（社交通道用
#: `snippet`/`author`，web 通道用 `content`），点名会变成"字段名对了才给分"。
_TEXT_BEARING_FIELDS = ("content", "text", "snippet", "summary", "description")
#: 结果的标题类字段。
_TITLE_FIELDS = ("title", "name")
#: 社交结果**特有**的作者字段。只有存在这类字段时才要求它非空。
_AUTHOR_FIELDS = ("author", "handle", "username", "user", "screen_name")


def _header_grounding_ratio(row: dict[str, object], prefix: str) -> float:
    """每条结果是否**带上了实质文本** —— social 缺陷的判据。

    social 真实缺陷（2026-09-22）：`results[]` 除 url 外全为空 —— 先是
    `title`/`snippet`/`author` 全是空串，更早是这些键**根本不存在**。
    `semantic_discovery` / `site_coverage` 只看条数与域名数，所以
    "5 条全空"与"5 条齐全"得分完全相同。

    **不点名具体字段名**：两侧字段名本就不同，点名会把"Tavily 返回 web
    结果、没有 author 键"判成缺陷 —— 那是误杀，不是发现。实测反例：
    同一行 Tavily 的每条结果都有 `title` + `content`，只因缺 `author`
    被算成 0.333。判据改为"有标题**且**有任一正文承载字段"；若该结果
    **声明了**作者类字段，再要求它非空。

    **只在 url 非空时**计入，避免把"没有结果"算成"字段填写很差"；
    没有结果由 `empty_result` / `assertion_pass_rate` 表达。
    """
    raw = str(row.get(f"{prefix}_raw", "") or "")
    parsed = _json_value(raw, None)
    if not isinstance(parsed, dict):
        return 1.0
    items = parsed.get("results")
    if not isinstance(items, list) or not items:
        return 1.0
    ratios: list[float] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if not str(item.get("url") or "").strip():
            continue
        checks = [
            any(str(item.get(key) or "").strip() for key in _TITLE_FIELDS),
            any(str(item.get(key) or "").strip() for key in _TEXT_BEARING_FIELDS),
        ]
        # 作者字段只在**该结果自己带了**这类键时才计入。
        declared_author_fields = [key for key in _AUTHOR_FIELDS if key in item]
        if declared_author_fields:
            checks.append(
                any(str(item.get(key) or "").strip() for key in declared_author_fields)
            )
        ratios.append(sum(checks) / len(checks))
    if not ratios:
        return 1.0
    return sum(ratios) / len(ratios)


def _assertion_pass_rate(row: dict[str, object], prefix: str, input_row: dict[str, str]) -> float:
    """该行**断言**的通过比例。目前是三组可证伪判据的等权平均。

    这是"只测有没有东西、不测东西对不对"这个盲区的修法：断言不通过就掉分，
    而断言必须能在**旧代码上失败、新代码上通过**才允许进矩阵。
    """
    checks: list[float] = []

    urls = _row_urls(row, prefix)
    expected_url_patterns = [
        value.lower() for value in parse_pipe_list(input_row.get("expected_url_patterns", ""))
    ]
    if expected_url_patterns:
        matched = any(
            any(pattern in url.lower() for pattern in expected_url_patterns) for url in urls
        )
        checks.append(1.0 if matched else 0.0)

    expected_answer_patterns = [
        value.lower() for value in parse_pipe_list(input_row.get("expected_answer_patterns", ""))
    ]
    if expected_answer_patterns:
        summary = str(row.get(f"{prefix}_summary", "") or "")
        checks.append(1.0 if _summary_matches_expected_answer(summary, expected_answer_patterns) else 0.0)

    if str(input_row.get("domain", "")).strip().lower() == "纯 social / x":
        checks.append(_header_grounding_ratio(row, prefix))

    if not checks:
        return 1.0
    return sum(checks) / len(checks)


def _summary_matches_expected_answer(summary: str, patterns: list[str]) -> bool:
    normalized_summary = summary.lower()
    for raw_pattern in patterns:
        pattern = raw_pattern.strip().lower()
        if not pattern:
            continue
        escaped = re.escape(pattern)
        if re.fullmatch(r"\d+(?:\.\d+)+", pattern):
            matcher = re.compile(rf"(?<![\w.])v?{escaped}(?!\w)")
        else:
            matcher = re.compile(rf"(?<!\w){escaped}(?!\w)")
        for match in matcher.finditer(normalized_summary):
            before = normalized_summary[max(0, match.start() - 64) : match.start()]
            after = normalized_summary[match.end() : match.end() + 48]
            negated_before = re.search(
                r"(?:\bnot|\bnever|\bno longer|\bisn't|\baren't|\bwasn't|\bweren't|"
                r"\bis not|\bare not|\bwas not|\bwere not)\s+(?:the\s+)?$",
                before,
            ) or re.search(
                r"\b(?:not|no longer)\s+(?:the\s+)?(?:latest|current|stable|correct)"
                r"(?:\s+\w+){0,3}\s*[:=]?\s*$",
                before,
            )
            negated_after = re.match(
                r"\s*(?:(?:is|was|are|were)\s+)?(?:not\b|no longer\b|outdated\b|"
                r"obsolete\b|stale\b|incorrect\b|wrong\b)",
                after,
            )
            if not negated_before and not negated_after:
                return True
    return False


def _score_provider(
    input_row: dict[str, str],
    row: dict[str, object],
    prefix: str,
) -> dict[str, float]:
    if not _provider_captured(row, prefix):
        return {dimension: 0.0 for dimension in BENCHMARK_DIMENSIONS}

    summary = str(row.get(f"{prefix}_summary", "")).strip()
    urls = _row_urls(row, prefix)
    citation_count = max(0.0, _as_float(row.get(f"{prefix}_citation_count")))
    trace_value = _json_value(row.get(f"{prefix}_provider_trace"), {})
    trace = trace_value if isinstance(trace_value, dict) else {}
    provider_names = _trace_provider_names(trace)
    include_domains = parse_pipe_list(input_row.get("include_domains", "")) or OFFICIAL_DOMAINS.get(
        input_row.get("benchmark_id", ""), []
    )
    expected_url_patterns = [value.lower() for value in parse_pipe_list(input_row.get("expected_url_patterns", ""))]
    expected_answer_patterns = [
        value.lower() for value in parse_pipe_list(input_row.get("expected_answer_patterns", ""))
    ]
    expected_answer_match = _summary_matches_expected_answer(summary, expected_answer_patterns)
    row[f"{prefix}_expected_answer_match"] = (
        expected_answer_match if expected_answer_patterns else ""
    )
    exclude_domains = parse_pipe_list(input_row.get("exclude_domains", ""))
    compliant_count = sum(_url_satisfies_constraints(url, include_domains, exclude_domains) for url in urls)
    constraint_ratio = compliant_count / len(urls) if urls else 0.0
    strict_required = parse_optional_bool(input_row.get("strict_required"), False)
    orchestration = (
        _as_bool(row.get(f"{prefix}_orchestration_used"))
        or _as_bool(trace.get("orchestration_used"))
        or str(trace.get("provider", "")).strip().lower() == "hybrid"
        or len(provider_names) > 1
    )
    fallback_attempted = _as_bool(row.get(f"{prefix}_fallback_attempted")) or _as_bool(
        trace.get("fallback_attempted")
    )
    fallback_reason = str(row.get(f"{prefix}_fallback_reason", "") or trace.get("fallback_reason", "")).strip()
    fallback_used = _as_bool(row.get(f"{prefix}_fallback_used")) if fallback_attempted else False
    row[f"{prefix}_orchestration_used"] = orchestration
    row[f"{prefix}_fallback_attempted"] = fallback_attempted
    row[f"{prefix}_fallback_reason"] = fallback_reason if fallback_attempted else ""
    row[f"{prefix}_fallback_used"] = fallback_used
    stability, observations = _repeat_metrics(row, prefix)
    latency_budget_ms = max(0.0, _as_float(input_row.get("latency_budget_ms")))
    efficiency, measured_budget_exceeded = _efficiency_score(row, prefix, latency_budget_ms, observations)
    budget_exceeded = measured_budget_exceeded or _as_bool(row.get(f"{prefix}_latency_budget_exceeded"))
    if budget_exceeded:
        efficiency = min(efficiency, 2.0)
    row[f"{prefix}_latency_budget_exceeded"] = budget_exceeded

    canonical_matches = [
        url
        for url in urls
        if any(pattern in url.lower() for pattern in expected_url_patterns)
    ]
    canonical_top_match = bool(
        urls
        and expected_url_patterns
        and any(pattern in urls[0].lower() for pattern in expected_url_patterns)
    )
    if include_domains and expected_url_patterns:
        authority = (3.0 * constraint_ratio) + (1.0 if canonical_matches else 0.0) + (1.0 if canonical_top_match else 0.0)
    elif include_domains:
        authority = 5.0 * constraint_ratio
    elif expected_url_patterns:
        authority = 2.0 + (1.5 if canonical_matches else 0.0) + (1.5 if canonical_top_match else 0.0)
    else:
        authority = 2.5 + min(1.5, len(urls) * 0.5) + (0.5 if trace else 0.0)
    if strict_required and include_domains and constraint_ratio < 1.0:
        authority = min(authority, 1.0)

    semantic_discovery = 2.0 + min(1.5, len(urls) * 0.5) + min(1.5, citation_count / 5.0)
    provider_orchestration = 5.0 if orchestration else 4.0 if fallback_attempted else 3.0 if trace else 2.0
    domain_count = len({urllib.parse.urlparse(url).hostname for url in urls if urllib.parse.urlparse(url).hostname})
    if orchestration and len(provider_names) > 1:
        multi_source_fusion = 5.0
    elif orchestration or domain_count > 1:
        multi_source_fusion = 4.0
    elif citation_count > 1:
        multi_source_fusion = 3.0
    else:
        multi_source_fusion = 2.0

    content_char_count = max(0.0, _as_float(row.get(f"{prefix}_content_char_count")))
    content_item_count = max(0.0, _as_float(row.get(f"{prefix}_content_item_count")))
    content_noise_hits = max(0.0, _as_float(row.get(f"{prefix}_content_noise_hits")))
    content_fidelity = 1.0
    content_fidelity += 1.0 if content_char_count >= 200 else 0.5 if content_char_count > 0 else 0.0
    content_fidelity += 1.5 if content_char_count >= 1000 else 0.75 if content_char_count >= 500 else 0.0
    content_fidelity += 1.0 if content_item_count > 0 else 0.0
    content_fidelity += 0.5 if content_char_count > 0 and content_noise_hits == 0 else 0.0
    if content_noise_hits:
        content_fidelity -= min(2.0, content_noise_hits)

    published_date_count = _as_float(row.get(f"{prefix}_published_date_count"))
    has_explicit_date = bool(re.search(r"\b20\d{2}(?:[-/]\d{1,2})?\b", summary))
    if expected_answer_patterns:
        freshness_signal = 5.0 if expected_answer_match else 0.0
    elif published_date_count:
        # 没有断言时**不给满分**：只要页面上有一个日期就能拿 5.0，等于从不校验
        # 答案是否正确。loop34 里 5 行正是这样拿到假 5.0，把净优势抬高约 94 分。
        freshness_signal = 3.0
    elif has_explicit_date:
        freshness_signal = 2.5
    else:
        freshness_signal = 1.5
    site_coverage = 2.0 + min(2.0, citation_count / 3.0) + min(1.0, domain_count / 2.0)
    traceability = 1.5 + (1.5 if trace else 0.0) + min(1.5, citation_count / 3.0) + (0.5 if urls else 0.0)

    resilience = 5.0 * stability
    if _as_bool(row.get(f"{prefix}_timeout")):
        resilience = 0.0
    elif fallback_attempted and not fallback_used:
        resilience = min(resilience, 4.0)

    # 两个断言类计分项：前十个维度只数"有没有东西"，这两项问"东西对不对"。
    # social 字段全空与版本号编造两次都是从这个缺口漏过去的。
    groundedness_ratio, groundedness_token_count = _claim_groundedness(row, prefix)
    claim_groundedness_score = 5.0 * groundedness_ratio
    assertion_rate = _assertion_pass_rate(row, prefix, input_row)
    assertion_pass_rate_score = 5.0 * assertion_rate
    row[f"{prefix}_claim_groundedness_ratio"] = round(groundedness_ratio, 4)
    row[f"{prefix}_grounded_token_count"] = groundedness_token_count
    row[f"{prefix}_assertion_pass_rate"] = round(assertion_rate, 4)

    scores = {
        "authority_precision": authority,
        "semantic_discovery": semantic_discovery,
        "provider_orchestration": provider_orchestration,
        "multi_source_fusion": multi_source_fusion,
        "content_fidelity": content_fidelity,
        "freshness_signal": freshness_signal,
        "site_coverage": site_coverage,
        "traceability": traceability,
        "resilience": resilience,
        "efficiency": efficiency,
        "claim_groundedness": claim_groundedness_score,
        "assertion_pass_rate": assertion_pass_rate_score,
    }
    return {dimension: _clamp_score(scores[dimension]) for dimension in BENCHMARK_DIMENSIONS}


def _dimension_weights(input_row: dict[str, str]) -> dict[str, float]:
    weights: dict[str, float] = {}
    for dimension in parse_pipe_list(input_row.get("primary_dimensions", "")):
        if dimension in BENCHMARK_DIMENSIONS:
            weights[dimension] = 2.0
    for dimension in parse_pipe_list(input_row.get("secondary_dimensions", "")):
        if dimension in BENCHMARK_DIMENSIONS:
            weights.setdefault(dimension, 1.0)
    return weights or {dimension: 1.0 for dimension in BENCHMARK_DIMENSIONS}


def score_output_row(input_row: dict[str, str], row: dict[str, object]) -> dict[str, object]:
    weights = _dimension_weights(input_row)
    totals: dict[str, float] = {}
    for prefix in ("mysearch", "tavily"):
        scores = _score_provider(input_row, row, prefix)
        for dimension, score in scores.items():
            row[f"{prefix}_{dimension}_score"] = score
        weighted_score = sum(scores[dimension] * weight for dimension, weight in weights.items())
        total = round((weighted_score / sum(weights.values())) * 10.0, 2)
        row[f"{prefix}_total_score"] = total
        totals[prefix] = total

    if str(row.get("run_status", "")) == "captured" and (
        _as_bool(row.get("mysearch_latency_budget_exceeded"))
        or _as_bool(row.get("tavily_latency_budget_exceeded"))
    ):
        row["run_status"] = "budget-exceeded"

    if _provider_captured(row, "mysearch") and _provider_captured(row, "tavily"):
        if totals["mysearch"] > totals["tavily"]:
            winner = "mysearch"
        elif totals["tavily"] > totals["mysearch"]:
            winner = "tavily"
        else:
            winner = "tie"
        row["winner"] = winner
        semantic_note = (
            "semantic correctness used only the explicit expected-answer contract"
            if parse_pipe_list(input_row.get("expected_answer_patterns", ""))
            else "semantic correctness was not inferred"
        )
        row["winner_reason"] = (
            f"observable contract score: mysearch={totals['mysearch']:.2f}, "
            f"tavily={totals['tavily']:.2f}; {semantic_note}"
        )
    else:
        row["winner"] = "incomplete"
        row["winner_reason"] = "dual result incomplete; no comparative winner"
    return row


def load_existing_rows(path: Path) -> tuple[list[str], dict[str, dict[str, str]]]:
    if not path.exists():
        return [], {}
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    normalized = {}
    for row in rows:
        normalized[row["benchmark_id"]] = {key: row.get(key, "") for key in FIELDNAMES}
    return [row["benchmark_id"] for row in rows], normalized


def sync_input_contract(row: dict[str, object], input_row: dict[str, str]) -> None:
    row.update(
        {
            "benchmark_id": input_row["benchmark_id"],
            "domain": input_row["domain"],
            "query": input_row["query"],
            "prompt_variant": input_row["prompt_variant"],
            "active_dimensions": active_dimensions(input_row),
            "latency_budget_ms": input_row.get("latency_budget_ms", "") or "",
            "expected_answer_patterns": input_row.get("expected_answer_patterns", "") or "",
        }
    )


def build_output_row(
    input_row: dict[str, str],
    item: dict[str, str],
    raw_dir: Path,
    *,
    existing: Optional[dict[str, str]] = None,
    preserve_tavily: bool = False,
) -> dict[str, str]:
    existing = existing or {}
    row = {key: "" for key in FIELDNAMES}
    if preserve_tavily:
        row.update(existing)
    row.update(
        {
            "run_date": date.today().isoformat(),
            "mysearch_tool": item.get("mysearch_tool", ""),
            "mysearch_mode": item.get("mysearch_mode", ""),
            "tavily_tool": item.get("tavily_tool", ""),
        }
    )
    for key, value in item.items():
        if key not in row:
            continue
        if preserve_tavily and key.startswith("tavily_") and value in {"", None, False, 0, "0"}:
            continue
        row[key] = value
    sync_input_contract(row, input_row)

    mysearch_raw = item.get("mysearch_raw", "")
    tavily_raw = item.get("tavily_raw", "")
    raw_notes = []
    if mysearch_raw:
        raw_notes.append(f"mysearch_raw={write_raw(raw_dir, input_row['benchmark_id'], 'mysearch', mysearch_raw)}")
    if tavily_raw:
        raw_notes.append(f"tavily_raw={write_raw(raw_dir, input_row['benchmark_id'], 'tavily', tavily_raw)}")
    elif preserve_tavily and existing.get("notes"):
        raw_notes.extend(
            note.strip()
            for note in existing["notes"].split(" ; ")
            if note.strip().startswith("tavily_raw=")
        )

    note_chunks = []
    if input_row.get("notes"):
        note_chunks.append(input_row["notes"].strip())
    note_chunks.extend(raw_notes)
    row["notes"] = " ; ".join(chunk for chunk in note_chunks if chunk)
    row["structural_failure"] = existing.get("structural_failure", "") if preserve_tavily else ""
    row["optimization_hint"] = existing.get("optimization_hint", "") if preserve_tavily else ""
    if not row["structural_failure"]:
        tavily_failure = classify_tavily_structural_failure(
            tavily_raw,
            input_row["benchmark_id"],
            row.get("error", ""),
        )
        if tavily_failure:
            row["structural_failure"] = tavily_failure
    # `claim_groundedness` 必须回查 provider 自己的正文，而正文只在 raw 里。
    # 这两列**留在内存**供计分（`merge_output_rows` 还会再算一遍），
    # 由 `write_output` 在落盘时过滤掉 —— CSV 不存 raw（体积大且与 raw/ 目录重复）。
    for prefix, raw_text in (("mysearch", mysearch_raw), ("tavily", tavily_raw)):
        row[f"{prefix}_raw"] = raw_text
    score_output_row(input_row, row)
    return row


def merge_output_rows(
    input_rows: list[dict[str, str]],
    selected_rows: list[dict[str, str]],
    results: list[dict[str, str]],
    raw_dir: Path,
    *,
    existing_order: list[str],
    existing_rows: dict[str, dict[str, str]],
    preserve_tavily: bool,
) -> list[dict[str, str]]:
    result_map = {item["benchmark_id"]: item for item in results}
    input_row_map = {row["benchmark_id"]: row for row in input_rows}
    selected_ids = {row["benchmark_id"] for row in selected_rows}
    active_ids = {row["benchmark_id"] for row in input_rows}
    merged = {
        benchmark_id: row
        for benchmark_id, row in existing_rows.items()
        if benchmark_id in active_ids
    }

    for benchmark_id in selected_ids:
        if benchmark_id not in result_map:
            continue
        merged[benchmark_id] = build_output_row(
            input_row_map[benchmark_id],
            result_map[benchmark_id],
            raw_dir,
            existing=existing_rows.get(benchmark_id),
            preserve_tavily=preserve_tavily,
        )

    ordered_ids = []
    seen = set()
    for row in input_rows:
        benchmark_id = row["benchmark_id"]
        if benchmark_id in merged and benchmark_id not in seen:
            ordered_ids.append(benchmark_id)
            seen.add(benchmark_id)
    output_rows = []
    for benchmark_id in ordered_ids:
        row = merged[benchmark_id]
        sync_input_contract(row, input_row_map[benchmark_id])
        score_output_row(input_row_map[benchmark_id], row)
        output_rows.append(row)
    return output_rows


def write_output(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def batched_rows(rows: list[dict[str, str]], chunk_size: int) -> list[list[dict[str, str]]]:
    if not rows:
        return []
    effective_chunk_size = len(rows) if chunk_size <= 0 else chunk_size
    batches: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    for row in rows:
        preferred_tool = row.get("preferred_tool", "").strip()
        if preferred_tool in FIRECRAWL_CRAWL_MAP_TOOLS:
            if current:
                batches.append(current)
                current = []
            batches.append([row])
            continue
        current.append(row)
        if len(current) >= effective_chunk_size:
            batches.append(current)
            current = []
    if current:
        batches.append(current)
    return batches


def batch_uses_firecrawl_crawl_map(rows: list[dict[str, str]]) -> bool:
    return any(row.get("preferred_tool", "").strip() in FIRECRAWL_CRAWL_MAP_TOOLS for row in rows)


def main() -> int:
    args = parse_args()
    input_path = Path(args.input_csv)
    output_path = Path(args.output_csv)
    raw_dir = Path(args.raw_dir)
    resolved_tavily_bearer = resolve_tavily_bearer(
        args.tavily_bearer,
        codex_config_path=Path(args.codex_config).expanduser(),
        mcp_server_name=args.tavily_mcp_server,
    )

    all_input_rows = read_rows(input_path)
    selected_rows = all_input_rows
    if args.benchmark_id:
        wanted = set(args.benchmark_id)
        selected_rows = [row for row in selected_rows if row["benchmark_id"] in wanted]
    if args.limit:
        selected_rows = selected_rows[: args.limit]
    if not selected_rows:
        print("No benchmark rows selected", file=sys.stderr)
        return 1
    if not str(args.host or "").strip():
        print(
            "Missing benchmark SSH host. Pass --host user@host, or set MYSEARCH_BENCHMARK_HOST."
            " See scripts/benchmark.env.example.",
            file=sys.stderr,
        )
        return 1
    if not args.mysearch_only and not str(resolved_tavily_bearer or "").strip():
        print(
            "Missing Tavily comparator bearer. Set TAVILY_MCP_BEARER, pass --tavily-bearer, or configure mcp_servers."
            f".{args.tavily_mcp_server}.headers.Authorization in {Path(args.codex_config).expanduser()}.",
            file=sys.stderr,
        )
        return 1

    reuse_path = Path(args.reuse_output_csv) if args.reuse_output_csv else output_path
    existing_order, existing_rows = load_existing_rows(reuse_path)
    output_rows = [existing_rows[benchmark_id] for benchmark_id in existing_order if benchmark_id in existing_rows]

    batches = batched_rows(selected_rows, args.chunk_size)
    for batch_index, batch_rows in enumerate(batches, start=1):
        cases = [build_case(row) for row in batch_rows]
        results = run_remote_cases(
            host=args.host,
            mysearch_url=args.mysearch_url,
            tavily_url=args.tavily_url,
            tavily_bearer=resolved_tavily_bearer,
            cases=cases,
            mysearch_only=args.mysearch_only,
        )
        output_rows = merge_output_rows(
            all_input_rows,
            batch_rows,
            results,
            raw_dir,
            existing_order=existing_order,
            existing_rows=existing_rows,
            preserve_tavily=args.mysearch_only,
        )
        write_output(output_path, output_rows)
        existing_order, existing_rows = load_existing_rows(output_path)
        print(
            f"Wrote {len(output_rows)} rows to {output_path} "
            f"(batch {batch_index}/{len(batches)})"
        )
        if batch_index < len(batches):
            next_batch = batches[batch_index]
            if batch_uses_firecrawl_crawl_map(batch_rows) and batch_uses_firecrawl_crawl_map(next_batch):
                time.sleep(FIRECRAWL_CRAWL_MAP_COOLDOWN_SECONDS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
