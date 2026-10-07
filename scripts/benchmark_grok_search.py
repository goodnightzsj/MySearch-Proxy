#!/usr/bin/env python3
"""Capture a frozen Grok search benchmark or replay it through the current probe.

Run live inside the configured proxy container with --cases-json and --models.
Replay locally with --replay capture.jsonl. JSONL stdout contains public benchmark
responses but no credentials. This never updates model settings or probe history.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time
from contextlib import redirect_stdout
from datetime import date, datetime
from urllib.parse import urlsplit
from unittest.mock import AsyncMock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1] if __file__ != "<stdin>" else Path.cwd()
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "proxy"))
with redirect_stdout(sys.stderr):
    import server  # noqa: E402


def emit(record):
    print(json.dumps(record, ensure_ascii=False), flush=True)


async def evaluate(record):
    response = httpx.Response(record["http_status"], json=record.get("payload"))
    with patch.object(server.http_client, "post", AsyncMock(return_value=response)):
        ok, evidence = await server.probe_social_model(
            "http://benchmark.invalid", "benchmark-placeholder", record["model"], query=record["case"]["query"])
    try:
        normalized = server.normalize_social_search_response(
            record["case"]["query"], record["payload"], 3, model=record["model"])
        rows = normalized["results"]
    except (ValueError, TypeError, AttributeError):
        ok, evidence, rows = False, {"reason": "invalid_response"}, []
    violations = []
    unknown = []
    case = record["case"]
    allowed = {handle.lower().lstrip("@") for handle in case.get("allowed_x_handles", [])}
    excluded = {handle.lower().lstrip("@") for handle in case.get("excluded_x_handles", [])}
    for index, row in enumerate(rows):
        path = urlsplit(row["url"]).path.strip("/").split("/")
        reported_handle = str(row.get("handle") or "").lower().lstrip("@")
        cited_handle = path[0].lower() if len(path) == 3 and path[0].lower() != "i" else ""
        handle = cited_handle or reported_handle
        if allowed or excluded:
            if not cited_handle:
                unknown.append(f"handle:{index}")
            if (handle and allowed and handle not in allowed) or handle in excluded:
                violations.append(f"handle:{index}")
            if cited_handle and reported_handle and cited_handle != reported_handle:
                unknown.append(f"handle_metadata_conflict:{index}")
        if case.get("from_date") or case.get("to_date"):
            # A model-written timestamp is not independent evidence of publication time.
            unknown.append(f"date:{index}")
            try:
                created = datetime.fromisoformat(row.get("created_at", "").replace("Z", "+00:00")).date()
            except ValueError:
                continue
            else:
                if ((case.get("from_date") and created < date.fromisoformat(case["from_date"]))
                        or (case.get("to_date") and created > date.fromisoformat(case["to_date"]))):
                    violations.append(f"date:{index}")
    if not rows and (allowed or excluded or case.get("from_date") or case.get("to_date")):
        unknown.append("no_results")
    return {"probe_ok": ok, "probe_reason": evidence.get("reason"),
            "result_count": len(rows),
            "content_count": sum(bool(str(row.get("text") or "").strip()) for row in rows),
            "constraint_violations": violations, "constraint_unknown": unknown,
            "constraint_status": "failed" if violations else "unknown" if unknown else "pass",
            "excerpts": [row.get("text", "")[:160] for row in rows],
            "urls": [row.get("url") for row in rows]}


async def main(args):
    try:
        if args.replay:
            for line in Path(args.replay).read_text().splitlines():
                record = json.loads(line)
                if record.get("type") != "sample":
                    continue
                if record.get("http_status") == 200 and isinstance(record.get("payload"), dict):
                    emit({"type": "replay", "case_id": record["case"]["id"], "model": record["model"],
                          "captured_latency_ms": record["latency_ms"], **await evaluate(record)})
                else:
                    emit({"type": "replay", "case_id": record["case"]["id"], "model": record["model"],
                          "http_status": record.get("http_status"), "error": record.get("error"), "probe_ok": False})
            return

        cases = json.loads(args.cases_json)
        if not isinstance(cases, list) or not cases or len(cases) * len(args.models) > 6:
            raise ValueError("Each live round requires 1-6 case/model pairs")
        if len({case["id"] for case in cases}) != len(cases):
            raise ValueError("Case IDs must be unique")
        for case in cases:
            if not isinstance(case.get("query"), str) or not case["query"].strip():
                raise ValueError("Each case requires a nonempty query")
        if not all(server.merge_candidates([model]) for model in args.models):
            raise ValueError("Ineligible Grok model ID")
        manifest = json.dumps(cases, ensure_ascii=False, sort_keys=True).encode()
        emit({"type": "manifest", "sha256": hashlib.sha256(manifest).hexdigest(), "cases": cases, "models": args.models})
        config = server.get_runtime_social_config()
        if config["mode"] != "upstream" or config["upstream_responses_path"] != "/responses":
            raise ValueError("Live benchmark requires configured Grok upstream /responses")
        server._load_social_upstream_key_schedule()
        keys = server._ordered_social_upstream_keys(server.parse_secret_values(config.get("upstream_api_key")))
        if not keys:
            raise ValueError("No configured upstream key")
        root = server._root_of_upstream(config["upstream_base_url"])
        for case_index, case in enumerate(cases):
            # Counterbalance request order without adding concurrent upstream load.
            models = args.models[case_index:] + args.models[:case_index]
            for model in models:
                record = {"type": "sample", "case": case, "model": model}
                started = time.monotonic()
                try:
                    response = await asyncio.wait_for(server.http_client.post(
                        f"{root}/v1/responses", json=server.build_social_search_upstream_payload(case, model),
                        headers={"Authorization": f"Bearer {keys[0]}"}, timeout=40), timeout=40)
                    record["http_status"] = response.status_code
                    if response.status_code == 200:
                        payload = response.json()
                        # Only successful public-query responses are retained; error bodies may echo credentials.
                        record["payload"] = json.loads(server.redact_secret_text(json.dumps(payload), *keys))
                    else:
                        record["error"] = "upstream_http_error"
                        record["retry_after_seconds"] = server._parse_retry_after_header(response.headers)
                except (asyncio.TimeoutError, httpx.TimeoutException):
                    record["error"] = "timeout"
                except (httpx.RequestError, ValueError) as exc:
                    record["error"] = type(exc).__name__
                record["latency_ms"] = round((time.monotonic() - started) * 1000)
                emit(record)
                if record.get("http_status") in (401, 403, 429):
                    raise RuntimeError("Authentication/rate limit rejected benchmark; round stopped")
    finally:
        await server.http_client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--cases-json")
    mode.add_argument("--replay", type=Path)
    parser.add_argument("--models", nargs="+", default=[])
    args = parser.parse_args()
    if args.cases_json and not args.models:
        parser.error("--models is required for a live round")
    asyncio.run(main(args))
