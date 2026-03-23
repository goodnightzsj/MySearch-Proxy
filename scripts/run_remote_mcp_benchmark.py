#!/usr/bin/env python3
import argparse
import base64
import csv
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional


DEFAULT_HOST = "root@192.168.31.122"
DEFAULT_MYSEARCH_URL = "http://127.0.0.1:18000/mcp"
DEFAULT_TAVILY_URL = "http://127.0.0.1:8787/mcp"
DEFAULT_TAVILY_BEARER = "th-yXw6-UINstULph2WxuxQsqcuqVW2K"

OFFICIAL_DOMAINS = {
    "official-web-01": ["openai.com"],
    "docs-01": ["playwright.dev"],
    "pdf-01": ["arxiv.org"],
    "price-01": ["apple.com.cn"],
}

FIELDNAMES = [
    "benchmark_id",
    "run_status",
    "mysearch_summary",
    "tavily_summary",
    "mysearch_top_urls",
    "tavily_top_urls",
    "mysearch_provider_trace",
    "mysearch_accuracy_score",
    "mysearch_richness_score",
    "mysearch_stability_score",
    "mysearch_conditional_score",
    "mysearch_total_score",
    "tavily_accuracy_score",
    "tavily_richness_score",
    "tavily_stability_score",
    "tavily_conditional_score",
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
    return parser.parse_args()


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def map_mysearch_mode(row: dict[str, str]) -> str:
    domain = row["domain"]
    if row["preferred_tool"] == "research":
        return "research"
    if domain == "技术文档":
        return "docs"
    if domain == "GitHub":
        return "github"
    if domain == "PDF":
        return "pdf"
    if domain in {"新闻", "技术动态 / status"}:
        return "news"
    return "web"


def map_strategy(row: dict[str, str]) -> str:
    variant = row["prompt_variant"]
    if variant == "strict":
        return "verify"
    if variant == "research":
        return "deep"
    if variant == "status":
        return "verify"
    return "balanced"


def map_tavily_search_depth(row: dict[str, str]) -> str:
    variant = row["prompt_variant"]
    if variant in {"strict", "research"}:
        return "advanced"
    if variant == "status":
        return "advanced"
    return "fast"


def map_tavily_time_range(row: dict[str, str]) -> Optional[str]:
    domain = row["domain"]
    if domain in {"新闻", "技术动态 / status", "娱乐", "八卦"}:
        return "month"
    return None


def build_case(row: dict[str, str]) -> dict[str, object]:
    benchmark_id = row["benchmark_id"]
    query = row["query"]
    strict_domains = OFFICIAL_DOMAINS.get(benchmark_id, [])

    if row["preferred_tool"] == "extract_url":
        return {
            "benchmark_id": benchmark_id,
            "mysearch_tool": "extract_url",
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
        return {
            "benchmark_id": benchmark_id,
            "mysearch_tool": "research",
            "mysearch_args": {
                "query": query,
                "mode": "research",
                "strategy": "deep",
                "web_max_results": 5,
                "social_max_results": 5,
                "scrape_top_n": 3,
                "include_social": True,
            },
            "tavily_tool": "tavily_research",
            "tavily_args": {
                "input": query,
                "model": "mini",
            },
        }

    mysearch_args: dict[str, object] = {
        "query": query,
        "mode": map_mysearch_mode(row),
        "strategy": map_strategy(row),
        "max_results": 5,
        "include_answer": True,
        "include_content": row["domain"] in {"技术文档", "GitHub", "PDF"},
    }
    if strict_domains:
        mysearch_args["include_domains"] = strict_domains

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

    return {
        "benchmark_id": benchmark_id,
        "mysearch_tool": "search",
        "mysearch_args": mysearch_args,
        "tavily_tool": "tavily_search",
        "tavily_args": tavily_args,
    }


REMOTE_SCRIPT = r"""
import base64
import json
import sys
import urllib.request


def parse_mcp_payload(raw):
    text = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
    text = text.strip()
    if text.startswith("event:") or "\ndata:" in text:
        data_lines = []
        for line in text.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        text = "\n".join(data_lines).strip()
    return json.loads(text)


def parse_tool_content_text(result_payload):
    result = result_payload.get("result", {})
    content = result.get("content", [])
    if not content:
        return "", {}
    text = content[0].get("text", "")
    try:
        return text, json.loads(text)
    except Exception:
        return text, {"_text": text}


def first_nonempty(*values):
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


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
    deduped = []
    seen = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped[:3]


def summarize(blob):
    if not isinstance(blob, dict):
        return "", [], ""
    results = blob.get("results")
    first_result = results[0] if isinstance(results, list) and results else {}
    summary = first_nonempty(
        blob.get("answer", ""),
        blob.get("summary", ""),
        blob.get("research_summary", ""),
        first_result.get("snippet", "") if isinstance(first_result, dict) else "",
        first_result.get("content", "") if isinstance(first_result, dict) else "",
        blob.get("content", ""),
        blob.get("text", ""),
        blob.get("server_name", ""),
    )
    urls = collect_urls(blob)
    trace_blob = {}
    for key in ("provider", "providers_consulted", "matched_providers", "route_debug", "evidence", "official_mode"):
        if key in blob:
            trace_blob[key] = blob[key]
    provider_trace = json.dumps(trace_blob, ensure_ascii=False) if trace_blob else ""
    return summary[:500], urls, provider_trace[:1200]


class MCPClient:
    def __init__(self, url, headers=None):
        self.url = url
        self.headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        self.headers.update(headers or {})
        self.session_id = None

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
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=self.headers, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            self.session_id = resp.headers.get("mcp-session-id")
            _ = parse_mcp_payload(resp.read())
        notif_headers = dict(self.headers)
        if self.session_id:
            notif_headers["mcp-session-id"] = self.session_id
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        req2 = urllib.request.Request(self.url, data=json.dumps(notif).encode(), headers=notif_headers, method="POST")
        with urllib.request.urlopen(req2, timeout=20) as resp2:
            _ = resp2.read()

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
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            return parse_mcp_payload(resp.read())


payload = json.loads(base64.b64decode(sys.argv[1]).decode())
mysearch = MCPClient(payload["mysearch_url"])
mysearch.initialize()
tavily = MCPClient(payload["tavily_url"], {"Authorization": f'Bearer {payload["tavily_bearer"]}'})
tavily.initialize()

output = []
for case in payload["cases"]:
    row = {
        "benchmark_id": case["benchmark_id"],
        "run_status": "captured",
        "mysearch_summary": "",
        "tavily_summary": "",
        "mysearch_top_urls": "",
        "tavily_top_urls": "",
        "mysearch_provider_trace": "",
        "error": "",
    }
    try:
        mysearch_payload = mysearch.call_tool(case["mysearch_tool"], case["mysearch_args"])
        mysearch_text, mysearch_blob = parse_tool_content_text(mysearch_payload)
        summary, urls, provider_trace = summarize(mysearch_blob)
        row["mysearch_summary"] = summary
        row["mysearch_top_urls"] = " | ".join(urls)
        row["mysearch_provider_trace"] = provider_trace
        row["mysearch_raw"] = mysearch_text
    except Exception as exc:
        row["run_status"] = "partial-error"
        row["error"] = f"mysearch: {exc}"
        row["mysearch_raw"] = ""

    try:
        tavily_payload = tavily.call_tool(case["tavily_tool"], case["tavily_args"])
        tavily_text, tavily_blob = parse_tool_content_text(tavily_payload)
        summary, urls, _ = summarize(tavily_blob)
        row["tavily_summary"] = summary
        row["tavily_top_urls"] = " | ".join(urls)
        row["tavily_raw"] = tavily_text
    except Exception as exc:
        row["run_status"] = "partial-error" if row["run_status"] == "captured" else "error"
        row["error"] = (row["error"] + " ; " if row["error"] else "") + f"tavily: {exc}"
        row["tavily_raw"] = ""
    output.append(row)

print(json.dumps(output, ensure_ascii=False))
"""


def run_remote_cases(host: str, mysearch_url: str, tavily_url: str, tavily_bearer: str, cases: list[dict[str, object]]) -> list[dict[str, str]]:
    payload = {
        "mysearch_url": mysearch_url,
        "tavily_url": tavily_url,
        "tavily_bearer": tavily_bearer,
        "cases": cases,
    }
    payload_b64 = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
    cmd = [
        "ssh",
        "-o",
        "ConnectTimeout=10",
        host,
        "python3",
        "-",
        payload_b64,
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True, input=REMOTE_SCRIPT)
    if proc.returncode != 0:
        raise RuntimeError(
            f"remote benchmark failed with exit {proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return json.loads(proc.stdout)


def write_raw(raw_dir: Path, benchmark_id: str, provider: str, text: str) -> str:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{benchmark_id}.{provider}.json"
    path.write_text(text, encoding="utf-8")
    return str(path)


def build_output_rows(results: list[dict[str, str]], raw_dir: Path) -> list[dict[str, str]]:
    output_rows = []
    for item in results:
        notes = []
        mysearch_raw = item.pop("mysearch_raw", "")
        tavily_raw = item.pop("tavily_raw", "")
        if mysearch_raw:
            notes.append(f"mysearch_raw={write_raw(raw_dir, item['benchmark_id'], 'mysearch', mysearch_raw)}")
        if tavily_raw:
            notes.append(f"tavily_raw={write_raw(raw_dir, item['benchmark_id'], 'tavily', tavily_raw)}")
        row = {key: "" for key in FIELDNAMES}
        row.update(item)
        row["winner"] = "pending-review"
        row["winner_reason"] = "raw capture completed; scoring pending"
        row["notes"] = " ; ".join(notes) if notes else "raw capture missing"
        output_rows.append(row)
    return output_rows


def write_output(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    input_path = Path(args.input_csv)
    output_path = Path(args.output_csv)
    raw_dir = Path(args.raw_dir)

    rows = read_rows(input_path)
    if args.benchmark_id:
        wanted = set(args.benchmark_id)
        rows = [row for row in rows if row["benchmark_id"] in wanted]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("No benchmark rows selected", file=sys.stderr)
        return 1

    cases = [build_case(row) for row in rows]
    results = run_remote_cases(
        host=args.host,
        mysearch_url=args.mysearch_url,
        tavily_url=args.tavily_url,
        tavily_bearer=args.tavily_bearer,
        cases=cases,
    )
    output_rows = build_output_rows(results, raw_dir)
    write_output(output_path, output_rows)
    print(f"Wrote {len(output_rows)} rows to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
