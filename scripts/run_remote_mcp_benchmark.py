#!/usr/bin/env python3
import argparse
import base64
import csv
import json
import re
import subprocess
import sys
import urllib.parse
from datetime import date
from pathlib import Path
from typing import Optional


DEFAULT_HOST = "root@192.168.31.122"
DEFAULT_MYSEARCH_URL = "http://127.0.0.1:18000/mcp"
DEFAULT_TAVILY_URL = "http://127.0.0.1:8787/mcp"
DEFAULT_TAVILY_BEARER = "th-yXw6-UINstULph2WxuxQsqcuqVW2K"

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

FIELDNAMES = [
    "benchmark_id",
    "domain",
    "query",
    "prompt_variant",
    "run_date",
    "active_dimensions",
    "run_status",
    "mysearch_tool",
    "mysearch_mode",
    "mysearch_provider_trace",
    "mysearch_summary",
    "mysearch_top_urls",
    "mysearch_citation_count",
    "mysearch_official_mode",
    "mysearch_conflicts",
    "mysearch_latency_ms",
    "mysearch_repeat_variance",
    "mysearch_empty_result",
    "mysearch_timeout",
    "mysearch_fallback_used",
    "tavily_tool",
    "tavily_summary",
    "tavily_top_urls",
    "tavily_citation_count",
    "tavily_latency_ms",
    "tavily_repeat_variance",
    "tavily_empty_result",
    "tavily_timeout",
    "tavily_fallback_used",
    "mysearch_accuracy_score",
    "mysearch_richness_score",
    "mysearch_stability_score",
    "mysearch_constraint_execution_score",
    "mysearch_freshness_score",
    "mysearch_extraction_quality_score",
    "mysearch_efficiency_score",
    "mysearch_explainability_score",
    "mysearch_total_score",
    "tavily_accuracy_score",
    "tavily_richness_score",
    "tavily_stability_score",
    "tavily_constraint_execution_score",
    "tavily_freshness_score",
    "tavily_extraction_quality_score",
    "tavily_efficiency_score",
    "tavily_explainability_score",
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
    parser.add_argument("--tavily-bearer", default=DEFAULT_TAVILY_BEARER)
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
    strategy_hint = row.get("strategy_hint", "").strip()
    if strategy_hint:
        return strategy_hint
    variant = row["prompt_variant"]
    if variant == "strict":
        return "verify"
    if variant == "research":
        return "deep"
    if variant == "status":
        return "verify"
    return "balanced"


def map_tavily_search_depth(row: dict[str, str]) -> str:
    strategy = map_strategy(row)
    if strategy in {"verify", "deep"}:
        return "advanced"
    return "fast"


def map_tavily_time_range(row: dict[str, str]) -> Optional[str]:
    domain = row["domain"]
    if domain in {"新闻", "技术动态 / status", "娱乐", "八卦", "纯 Social / X"}:
        return "month"
    if domain in {"更新日志 / release"}:
        return "year"
    return None


def build_case(row: dict[str, str]) -> dict[str, object]:
    benchmark_id = row["benchmark_id"]
    query = row["query"]
    mode = map_mysearch_mode(row)
    strategy = map_strategy(row)
    strict_domains = parse_pipe_list(row.get("include_domains", "")) or OFFICIAL_DOMAINS.get(benchmark_id, [])
    exclude_domains = parse_pipe_list(row.get("exclude_domains", ""))
    repeat_runs = max(1, int((row.get("repeat_runs") or "1").strip()))

    if row["preferred_tool"] == "extract_url":
        return {
            "benchmark_id": benchmark_id,
            "domain": row["domain"],
            "query": query,
            "prompt_variant": row["prompt_variant"],
            "repeat_runs": repeat_runs,
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

    mysearch_args: dict[str, object] = {
        "query": query,
        "mode": mode,
        "strategy": strategy,
        "max_results": 5,
        "include_answer": True,
        "include_content": parse_optional_bool(
            row.get("include_content"),
            mode in {"docs", "github", "pdf"},
        ),
    }
    if strict_domains:
        mysearch_args["include_domains"] = strict_domains
    if exclude_domains:
        mysearch_args["exclude_domains"] = exclude_domains
    if mode == "social":
        mysearch_args["sources"] = ["x"]

    tavily_args: dict[str, object] = {
        "query": query,
        "max_results": 5,
        "search_depth": map_tavily_search_depth(row),
        "include_raw_content": False,
        "include_images": False,
        "include_image_descriptions": False,
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
        data_lines = []
        for line in text.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        text = "\n".join(data_lines).strip()
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
        for key in ("results", "citations", "pages", "sources", "items"):
            value = blob.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"]:
                        urls.append(item["url"])
        urls.extend(extract_urls_from_text(blob.get("content", "")))
    deduped = []
    seen = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped[:3]


def collect_citation_count(blob):
    if not isinstance(blob, dict):
        return 0
    for key in ("citations", "results", "pages", "sources", "items"):
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


def fallback_used(blob):
    if not isinstance(blob, dict):
        return False
    evidence = blob.get("evidence") if isinstance(blob.get("evidence"), dict) else {}
    route_debug = blob.get("route_debug") if isinstance(blob.get("route_debug"), dict) else {}
    providers = evidence.get("providers_consulted")
    matched = blob.get("matched_providers")
    if blob.get("provider") == "hybrid":
        return True
    if route_debug.get("route_provider") == "hybrid":
        return True
    if isinstance(providers, list) and len(providers) > 1:
        return True
    if isinstance(matched, list) and len(matched) > 1:
        return True
    if evidence.get("exa_discovery_count"):
        return True
    if evidence.get("retry_hint"):
        return True
    if blob.get("xai_arbitration_summary"):
        return True
    return False


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
        extract_error_summary(blob),
        blob.get("_text", ""),
        blob.get("server_name", ""),
    )
    urls = collect_urls(blob)
    trace_blob = {}
    for key in ("provider", "providers_consulted", "matched_providers", "route_debug", "evidence", "official_mode"):
        if key in blob:
            trace_blob[key] = blob[key]
    provider_trace = json.dumps(trace_blob, ensure_ascii=False) if trace_blob else ""
    return {
        "summary": summary[:500],
        "urls": urls,
        "provider_trace": provider_trace[:1200],
        "citation_count": collect_citation_count(blob),
        "official_mode": extract_official_mode(blob),
        "conflicts": collect_conflicts(blob),
        "empty_result": not urls and not summary.strip(),
        "fallback_used": fallback_used(blob),
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
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "remote-bench", "version": "0.1"},
            },
        }
        headers, _ = self._post(payload, self.headers, timeout=20, retries=5)
        self.session_id = headers.get("mcp-session-id")
        notif_headers = dict(self.headers)
        if self.session_id:
            notif_headers["mcp-session-id"] = self.session_id
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        self._post(notif, notif_headers, timeout=20, retries=5)

    def call_tool(self, tool_name, arguments):
        headers = dict(self.headers)
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        _, response_payload = self._post(payload, headers, timeout=120, retries=4)
        return response_payload


def timed_tool_runs(client, tool_name, arguments, repeat_runs):
    latencies = []
    errors = []
    timeout_flag = False
    first_success = None
    raw_text = ""
    for _ in range(max(1, int(repeat_runs or 1))):
        started = time.perf_counter()
        try:
            payload = client.call_tool(tool_name, arguments)
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            text, blob = parse_tool_content_text(payload)
            tool_error = blob.get("_tool_error") if isinstance(blob, dict) else None
            if isinstance(tool_error, str) and tool_error.strip():
                errors.append(tool_error.strip())
                if "timed out" in tool_error.lower() or "timeout" in tool_error.lower():
                    timeout_flag = True
                continue
            latencies.append(elapsed_ms)
            if first_success is None:
                first_success = summarize(blob)
                raw_text = text
        except Exception as exc:
            message = str(exc)
            errors.append(message)
            if "timed out" in message.lower():
                timeout_flag = True
    if first_success is None:
        raise RuntimeError(" ; ".join(errors[:3]) or "all repeats failed")
    variance_ms = 0.0
    if len(latencies) >= 2:
        variance_ms = round(max(latencies) - min(latencies), 1)
    return {
        "summary": first_success["summary"],
        "urls": first_success["urls"],
        "provider_trace": first_success["provider_trace"],
        "citation_count": first_success["citation_count"],
        "official_mode": first_success["official_mode"],
        "conflicts": first_success["conflicts"],
        "empty_result": first_success["empty_result"],
        "fallback_used": first_success["fallback_used"],
        "latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
        "repeat_variance": variance_ms,
        "timeout": timeout_flag,
        "partial_error": bool(errors),
        "error": " ; ".join(errors[:3]),
        "raw_text": raw_text,
    }


payload = json.loads(base64.b64decode(sys.argv[1]).decode())
mysearch = MCPClient(payload["mysearch_url"])
mysearch.initialize()
tavily = None
if not payload.get("mysearch_only"):
    tavily = MCPClient(payload["tavily_url"], {"Authorization": f'Bearer {payload["tavily_bearer"]}'})
    tavily.initialize()

output = []
for case in payload["cases"]:
    row = {
        "benchmark_id": case["benchmark_id"],
        "domain": case.get("domain", ""),
        "query": case.get("query", ""),
        "prompt_variant": case.get("prompt_variant", ""),
        "active_dimensions": case.get("active_dimensions", ""),
        "run_status": "captured",
        "mysearch_tool": case["mysearch_tool"],
        "mysearch_mode": case.get("mysearch_mode", ""),
        "mysearch_summary": "",
        "mysearch_top_urls": "",
        "mysearch_provider_trace": "",
        "mysearch_citation_count": 0,
        "mysearch_official_mode": "",
        "mysearch_conflicts": "",
        "mysearch_latency_ms": "",
        "mysearch_repeat_variance": "",
        "mysearch_empty_result": False,
        "mysearch_timeout": False,
        "mysearch_fallback_used": False,
        "tavily_tool": case.get("tavily_tool", ""),
        "tavily_summary": "",
        "tavily_top_urls": "",
        "tavily_citation_count": 0,
        "tavily_latency_ms": "",
        "tavily_repeat_variance": "",
        "tavily_empty_result": False,
        "tavily_timeout": False,
        "tavily_fallback_used": False,
        "error": "",
    }
    repeat_runs = case.get("repeat_runs", 1)

    try:
        observed = timed_tool_runs(mysearch, case["mysearch_tool"], case["mysearch_args"], repeat_runs)
        row["mysearch_summary"] = observed["summary"]
        row["mysearch_top_urls"] = " | ".join(observed["urls"])
        row["mysearch_provider_trace"] = observed["provider_trace"]
        row["mysearch_citation_count"] = observed["citation_count"]
        row["mysearch_official_mode"] = observed["official_mode"]
        row["mysearch_conflicts"] = " | ".join(observed["conflicts"])
        row["mysearch_latency_ms"] = observed["latency_ms"]
        row["mysearch_repeat_variance"] = observed["repeat_variance"]
        row["mysearch_empty_result"] = observed["empty_result"]
        row["mysearch_timeout"] = observed["timeout"]
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
            observed = timed_tool_runs(tavily, case["tavily_tool"], case["tavily_args"], repeat_runs)
            row["tavily_summary"] = observed["summary"]
            row["tavily_top_urls"] = " | ".join(observed["urls"])
            row["tavily_citation_count"] = observed["citation_count"]
            row["tavily_latency_ms"] = observed["latency_ms"]
            row["tavily_repeat_variance"] = observed["repeat_variance"]
            row["tavily_empty_result"] = observed["empty_result"]
            row["tavily_timeout"] = observed["timeout"]
            row["tavily_fallback_used"] = observed["fallback_used"]
            row["tavily_raw"] = observed["raw_text"]
            if observed["partial_error"]:
                row["run_status"] = "partial-error" if row["run_status"] == "captured" else row["run_status"]
                row["error"] = (row["error"] + " ; " if row["error"] else "") + f"tavily-repeat: {observed['error']}"
        except Exception as exc:
            row["run_status"] = "partial-error" if row["run_status"] == "captured" else "error"
            row["error"] = (row["error"] + " ; " if row["error"] else "") + f"tavily: {exc}"
            row["tavily_raw"] = ""
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
        payload_b64,
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True, input=REMOTE_SCRIPT)
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


def write_raw(raw_dir: Path, benchmark_id: str, provider: str, text: str) -> str:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{benchmark_id}.{provider}.json"
    path.write_text(text, encoding="utf-8")
    return str(path)


def classify_tavily_structural_failure(raw_text: str, benchmark_id: str) -> str:
    if not raw_text.strip() or "research" not in str(benchmark_id).lower():
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
        return "tavily-research-upstream-rate-limited"
    if "usage limit" in lowered or str(blob.get("status")) == "432":
        return "tavily-research-upstream-plan-limited"
    return ""


def load_existing_rows(path: Path) -> tuple[list[str], dict[str, dict[str, str]]]:
    if not path.exists():
        return [], {}
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    normalized = {}
    for row in rows:
        normalized[row["benchmark_id"]] = {key: row.get(key, "") for key in FIELDNAMES}
    return [row["benchmark_id"] for row in rows], normalized


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
            "benchmark_id": input_row["benchmark_id"],
            "domain": input_row["domain"],
            "query": input_row["query"],
            "prompt_variant": input_row["prompt_variant"],
            "run_date": date.today().isoformat(),
            "active_dimensions": active_dimensions(input_row),
            "mysearch_tool": item.get("mysearch_tool", ""),
            "mysearch_mode": item.get("mysearch_mode", ""),
            "tavily_tool": item.get("tavily_tool", ""),
        }
    )
    row.update({k: v for k, v in item.items() if k in row})

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
    row["winner"] = existing.get("winner", "pending-review") if preserve_tavily else "pending-review"
    row["winner_reason"] = existing.get("winner_reason", "matrix raw capture completed; scoring pending") if preserve_tavily else "matrix raw capture completed; scoring pending"
    row["structural_failure"] = existing.get("structural_failure", "")
    row["optimization_hint"] = existing.get("optimization_hint", "")
    if not row["structural_failure"]:
        tavily_failure = classify_tavily_structural_failure(tavily_raw, input_row["benchmark_id"])
        if tavily_failure:
            row["structural_failure"] = tavily_failure
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
    merged = dict(existing_rows)

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
    for benchmark_id in existing_order:
        if benchmark_id in merged and benchmark_id not in seen:
            ordered_ids.append(benchmark_id)
            seen.add(benchmark_id)
    for row in input_rows:
        benchmark_id = row["benchmark_id"]
        if benchmark_id in merged and benchmark_id not in seen:
            ordered_ids.append(benchmark_id)
            seen.add(benchmark_id)
    return [merged[benchmark_id] for benchmark_id in ordered_ids]


def write_output(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def batched_rows(rows: list[dict[str, str]], chunk_size: int) -> list[list[dict[str, str]]]:
    if chunk_size <= 0 or chunk_size >= len(rows):
        return [rows]
    return [rows[index : index + chunk_size] for index in range(0, len(rows), chunk_size)]


def main() -> int:
    args = parse_args()
    input_path = Path(args.input_csv)
    output_path = Path(args.output_csv)
    raw_dir = Path(args.raw_dir)

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

    reuse_path = Path(args.reuse_output_csv) if args.reuse_output_csv else output_path
    existing_order, existing_rows = load_existing_rows(reuse_path)
    output_rows = [existing_rows[benchmark_id] for benchmark_id in existing_order if benchmark_id in existing_rows]

    for batch_index, batch_rows in enumerate(batched_rows(selected_rows, args.chunk_size), start=1):
        cases = [build_case(row) for row in batch_rows]
        results = run_remote_cases(
            host=args.host,
            mysearch_url=args.mysearch_url,
            tavily_url=args.tavily_url,
            tavily_bearer=args.tavily_bearer,
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
            f"(batch {batch_index}/{len(batched_rows(selected_rows, args.chunk_size))})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
