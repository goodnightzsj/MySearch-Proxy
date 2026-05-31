from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import run_remote_mcp_benchmark


class RemoteBenchmarkConfigTests(unittest.TestCase):
    def test_is_recoverable_mcp_session_error_handles_session_required_variant(self) -> None:
        self.assertTrue(
            run_remote_mcp_benchmark.is_recoverable_mcp_session_error(
                'HTTP 400: {"error":"session_required","message":"MCP requests after initialize must include mcp-session-id."}'
            )
        )

    def test_is_recoverable_mcp_session_error_handles_session_unavailable_variant(self) -> None:
        self.assertTrue(
            run_remote_mcp_benchmark.is_recoverable_mcp_session_error(
                'HTTP 404: {"error":"session_unavailable","message":"MCP session is unavailable, please reconnect to initialize a new session."}'
            )
        )

    def test_classify_tavily_structural_failure_maps_session_required_variant(self) -> None:
        self.assertEqual(
            run_remote_mcp_benchmark.classify_tavily_structural_failure(
                "",
                "strict-constraint-03",
                'tavily: HTTP 400: {"error":"session_required","message":"MCP requests after initialize must include mcp-session-id."}',
            ),
            "tavily-mcp-session-transport-blocked",
        )

    def test_classify_tavily_structural_failure_maps_session_unavailable_variant(self) -> None:
        self.assertEqual(
            run_remote_mcp_benchmark.classify_tavily_structural_failure(
                "",
                "news-01",
                'tavily: HTTP 404: {"error":"session_unavailable","message":"MCP session is unavailable, please reconnect to initialize a new session."}',
            ),
            "tavily-mcp-session-transport-blocked",
        )

    def test_mcp_client_reinitializes_on_session_required_error(self) -> None:
        namespace: dict[str, object] = {}
        helper_source = run_remote_mcp_benchmark.REMOTE_SCRIPT.split("\npayload = json.loads", 1)[0]
        exec(helper_source, namespace)
        client = namespace["MCPClient"]("http://example.com/mcp")
        client.session_id = "stale-session"
        calls: list[tuple[str, dict[str, str]]] = []

        def fake_post(payload, headers, timeout, retries=4):  # type: ignore[no-untyped-def]
            calls.append((payload["method"], dict(headers)))
            method = payload["method"]
            if method == "tools/call" and len([item for item in calls if item[0] == "tools/call"]) == 1:
                raise RuntimeError(
                    'HTTP 400: {"error":"session_required","message":"MCP requests after initialize must include mcp-session-id."}'
                )
            if method == "initialize":
                return {"mcp-session-id": "fresh-session"}, {}
            if method == "notifications/initialized":
                return {}, {}
            return {}, {"ok": True}

        client._post = fake_post  # type: ignore[method-assign]

        result = client.call_tool("tavily_search", {"query": "OpenAI webhooks official"})

        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.session_id, "fresh-session")
        self.assertEqual(calls[0][0], "tools/call")
        self.assertEqual(calls[-1][0], "tools/call")
        self.assertEqual(calls[-1][1].get("mcp-session-id"), "fresh-session")

    def test_mcp_client_reinitializes_on_session_unavailable_error(self) -> None:
        namespace: dict[str, object] = {}
        helper_source = run_remote_mcp_benchmark.REMOTE_SCRIPT.split("\npayload = json.loads", 1)[0]
        exec(helper_source, namespace)
        client = namespace["MCPClient"]("http://example.com/mcp")
        client.session_id = "stale-session"
        calls: list[tuple[str, dict[str, str]]] = []

        def fake_post(payload, headers, timeout, retries=4):  # type: ignore[no-untyped-def]
            calls.append((payload["method"], dict(headers)))
            method = payload["method"]
            if method == "tools/call" and len([item for item in calls if item[0] == "tools/call"]) == 1:
                raise RuntimeError(
                    'HTTP 404: {"error":"session_unavailable","message":"MCP session is unavailable, please reconnect to initialize a new session."}'
                )
            if method == "initialize":
                return {"mcp-session-id": "fresh-session"}, {}
            if method == "notifications/initialized":
                return {}, {}
            return {}, {"ok": True}

        client._post = fake_post  # type: ignore[method-assign]

        result = client.call_tool("tavily_search", {"query": "OpenAI webhooks official"})

        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.session_id, "fresh-session")
        self.assertEqual(calls[0][0], "tools/call")
        self.assertEqual(calls[-1][0], "tools/call")
        self.assertEqual(calls[-1][1].get("mcp-session-id"), "fresh-session")

    def test_remote_helper_parses_sse_payload_with_continuation_lines(self) -> None:
        namespace: dict[str, object] = {}
        helper_source = run_remote_mcp_benchmark.REMOTE_SCRIPT.split("\npayload = json.loads", 1)[0]
        exec(helper_source, namespace)

        payload = (
            '{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"'
            '{\\"query\\":\\"2026 Oscars best picture result\\",'
            '\\"results\\":[{\\"url\\":\\"https://example.com\\",'
            '\\"content\\":\\"line1\\\\nline2\\"}]}"},{"type":"text","text":"ignored"}]}}'
        )
        raw = (
            ": ping - 2026-05-29 20:18:34.458477+00:00\n"
            ": ping - 2026-05-29 20:18:49.459287+00:00\n"
            "event: message\n"
            f"data: {payload[:160]}\n"
            f"{payload[160:320]}\n"
            f"{payload[320:]}\n\n"
        ).encode()

        parsed = namespace["parse_mcp_payload"](raw)

        self.assertEqual(parsed["result"]["content"][0]["type"], "text")
        self.assertIn("2026 Oscars best picture result", parsed["result"]["content"][0]["text"])

    def test_remote_helper_preserves_captured_row_for_tavily_quota_limit(self) -> None:
        namespace: dict[str, object] = {}
        helper_source = run_remote_mcp_benchmark.REMOTE_SCRIPT.split("\npayload = json.loads", 1)[0]
        exec(helper_source, namespace)

        self.assertTrue(
            namespace["should_preserve_captured_tavily_error"](
                "captured",
                "tavily_research",
                'HTTP 429: {"error":"quota_exhausted","hourlyAny":{"limit":100,"used":100}}',
            )
        )
        self.assertFalse(
            namespace["should_preserve_captured_tavily_error"](
                "partial-error",
                "tavily_research",
                'HTTP 429: {"error":"quota_exhausted","hourlyAny":{"limit":100,"used":100}}',
            )
        )

    def test_timed_tool_runs_ignores_tavily_quota_exhausted_repeat_after_success(self) -> None:
        namespace: dict[str, object] = {}
        helper_source = run_remote_mcp_benchmark.REMOTE_SCRIPT.split("\npayload = json.loads", 1)[0]
        exec(helper_source, namespace)

        class FakeClient:
            def __init__(self) -> None:
                self.calls = 0

            def call_tool(self, tool_name, arguments):  # type: ignore[no-untyped-def]
                self.calls += 1
                if self.calls == 1:
                    return {
                        "result": {
                            "content": [
                                {
                                    "text": '{"summary":"ok","results":[{"url":"https://example.com"}],"evidence":{"providers_consulted":["tavily"]}}',
                                }
                            ]
                        }
                    }
                raise RuntimeError(
                    'HTTP 429: {"error":"quota_exhausted","hourlyAny":{"limit":100,"used":100}}'
                )

        observed = namespace["timed_tool_runs"](FakeClient(), "tavily_research", {"input": "x"}, 3)

        self.assertFalse(observed["partial_error"])
        self.assertEqual(observed["error"], "")

    def test_timed_tool_runs_ignores_tavily_quota_errors_before_late_success(self) -> None:
        namespace: dict[str, object] = {}
        helper_source = run_remote_mcp_benchmark.REMOTE_SCRIPT.split("\npayload = json.loads", 1)[0]
        exec(helper_source, namespace)

        class FakeClient:
            def __init__(self) -> None:
                self.calls = 0

            def call_tool(self, tool_name, arguments):  # type: ignore[no-untyped-def]
                self.calls += 1
                if self.calls < 3:
                    raise RuntimeError(
                        'HTTP 429: {"error":"quota_exhausted","hourlyAny":{"limit":100,"used":100}}'
                    )
                return {
                    "result": {
                        "content": [
                            {
                                "text": '{"summary":"ok","results":[{"url":"https://example.com"}],"evidence":{"providers_consulted":["tavily"]}}',
                            }
                        ]
                    }
                }

        observed = namespace["timed_tool_runs"](FakeClient(), "tavily_search", {"query": "x"}, 3)

        self.assertFalse(observed["partial_error"])
        self.assertEqual(observed["error"], "")

    def test_classify_tavily_structural_failure_maps_research_quota_exhausted_from_error_text(self) -> None:
        self.assertEqual(
            run_remote_mcp_benchmark.classify_tavily_structural_failure(
                "",
                "research-03",
                'tavily: HTTP 429: {"error":"quota_exhausted","hourlyAny":{"limit":100,"used":100}}',
            ),
            "tavily-research-upstream-rate-limited",
        )

    def test_classify_tavily_structural_failure_ignores_mysearch_rate_limit_text(self) -> None:
        self.assertEqual(
            run_remote_mcp_benchmark.classify_tavily_structural_failure(
                '{"results": ["https://example.com"]}',
                "crawl-map-01",
                "mysearch: Error executing tool map_site: firecrawl request failed (HTTP 429): Rate limit exceeded",
            ),
            "",
        )

    def test_classify_tavily_structural_failure_reads_only_tavily_error_chunk(self) -> None:
        self.assertEqual(
            run_remote_mcp_benchmark.classify_tavily_structural_failure(
                "",
                "crawl-map-01",
                "mysearch: HTTP 429 from firecrawl ; tavily: HTTP 429: quota_exhausted",
            ),
            "tavily-search-upstream-rate-limited",
        )

    def test_estimate_remote_case_timeout_seconds_gives_research_more_budget(self) -> None:
        self.assertEqual(
            run_remote_mcp_benchmark.estimate_remote_case_timeout_seconds(
                {
                    "mysearch_tool": "research",
                    "tavily_tool": "tavily_research",
                    "repeat_runs": 2,
                }
            ),
            600,
        )
        self.assertEqual(
            run_remote_mcp_benchmark.estimate_remote_case_timeout_seconds(
                {
                    "mysearch_tool": "search",
                    "tavily_tool": "tavily_search",
                    "repeat_runs": 3,
                }
            ),
            360,
        )

    def test_estimate_remote_batch_timeout_seconds_sums_case_budgets(self) -> None:
        self.assertEqual(
            run_remote_mcp_benchmark.estimate_remote_batch_timeout_seconds(
                [
                    {"mysearch_tool": "research", "tavily_tool": "tavily_research", "repeat_runs": 2},
                    {"mysearch_tool": "search", "tavily_tool": "tavily_search", "repeat_runs": 3},
                ]
            ),
            960,
        )

    def test_build_case_uses_sources_hint_for_hybrid_search_row(self) -> None:
        case = run_remote_mcp_benchmark.build_case(
            {
                "benchmark_id": "hybrid-web-x-01",
                "domain": "技术动态 / status",
                "query": "OpenAI background mode latest status reactions",
                "prompt_variant": "status",
                "preferred_tool": "search",
                "mode_hint": "web",
                "strategy_hint": "verify",
                "primary_dimensions": "freshness|richness|explainability",
                "secondary_dimensions": "stability|efficiency",
                "repeat_runs": "3",
                "sources_hint": "web|x",
            }
        )

        self.assertEqual(case["mysearch_tool"], "search")
        self.assertEqual(case["mysearch_args"]["sources"], ["web", "x"])
        self.assertEqual(case["tavily_tool"], "tavily_search")

    def test_build_case_maps_site_mapping_tools(self) -> None:
        case = run_remote_mcp_benchmark.build_case(
            {
                "benchmark_id": "crawl-map-01",
                "domain": "站点地图",
                "query": "https://fastapi.tiangolo.com",
                "prompt_variant": "map",
                "preferred_tool": "map_site",
                "mode_hint": "map",
                "strategy_hint": "balanced",
                "primary_dimensions": "coverage|constraint_execution",
                "secondary_dimensions": "efficiency|stability",
                "repeat_runs": "2",
            }
        )

        self.assertEqual(case["mysearch_tool"], "map_site")
        self.assertEqual(case["mysearch_mode"], "map")
        self.assertEqual(case["repeat_runs"], 1)
        self.assertEqual(case["mysearch_args"]["url"], "https://fastapi.tiangolo.com")
        self.assertEqual(case["mysearch_args"]["limit"], 10)
        self.assertEqual(case["tavily_tool"], "tavily_map")
        self.assertEqual(case["tavily_args"]["max_depth"], 1)

    def test_build_case_maps_site_crawl_tools(self) -> None:
        case = run_remote_mcp_benchmark.build_case(
            {
                "benchmark_id": "crawl-map-02",
                "domain": "站点爬取",
                "query": "https://fastapi.tiangolo.com/tutorial/background-tasks/",
                "prompt_variant": "crawl",
                "preferred_tool": "crawl_site",
                "mode_hint": "crawl",
                "strategy_hint": "balanced",
                "primary_dimensions": "coverage|extraction_quality",
                "secondary_dimensions": "efficiency|stability",
                "repeat_runs": "2",
            }
        )

        self.assertEqual(case["mysearch_tool"], "crawl_site")
        self.assertEqual(case["mysearch_mode"], "crawl")
        self.assertEqual(case["repeat_runs"], 1)
        self.assertEqual(case["mysearch_args"]["max_depth"], 1)
        self.assertEqual(case["mysearch_args"]["limit"], 5)
        self.assertEqual(case["tavily_tool"], "tavily_crawl")
        self.assertEqual(case["tavily_args"]["extract_depth"], "basic")

    def test_batched_rows_isolate_firecrawl_map_and_crawl_cases(self) -> None:
        batches = run_remote_mcp_benchmark.batched_rows(
            [
                {"benchmark_id": "a", "preferred_tool": "search"},
                {"benchmark_id": "b", "preferred_tool": "extract_url"},
                {"benchmark_id": "c", "preferred_tool": "map_site"},
                {"benchmark_id": "d", "preferred_tool": "crawl_site"},
                {"benchmark_id": "e", "preferred_tool": "search"},
            ],
            3,
        )
        self.assertEqual(
            [[row["benchmark_id"] for row in batch] for batch in batches],
            [["a", "b"], ["c"], ["d"], ["e"]],
        )

    def test_batch_uses_firecrawl_crawl_map_detects_special_batches(self) -> None:
        self.assertTrue(
            run_remote_mcp_benchmark.batch_uses_firecrawl_crawl_map(
                [{"benchmark_id": "c", "preferred_tool": "map_site"}]
            )
        )
        self.assertFalse(
            run_remote_mcp_benchmark.batch_uses_firecrawl_crawl_map(
                [{"benchmark_id": "a", "preferred_tool": "search"}]
            )
        )

    def test_main_isolates_firecrawl_batches_and_only_cools_down_between_adjacent_special_batches(self) -> None:
        argv = [
            "run_remote_mcp_benchmark.py",
            "--input-csv",
            "dummy.csv",
            "--output-csv",
            "out.csv",
            "--raw-dir",
            "raw",
            "--tavily-bearer",
            "token",
            "--chunk-size",
            "3",
        ]
        rows = [
            {
                "benchmark_id": "search-1",
                "query": "OpenAI pricing",
                "domain": "Web",
                "preferred_tool": "search",
                "prompt_variant": "balanced",
                "primary_dimensions": "",
                "secondary_dimensions": "",
                "repeat_runs": "2",
            },
            {
                "benchmark_id": "map-1",
                "query": "https://fastapi.tiangolo.com",
                "domain": "站点地图",
                "preferred_tool": "map_site",
                "prompt_variant": "map",
                "primary_dimensions": "",
                "secondary_dimensions": "",
                "repeat_runs": "2",
            },
            {
                "benchmark_id": "crawl-1",
                "query": "https://fastapi.tiangolo.com/tutorial/background-tasks/",
                "domain": "站点爬取",
                "preferred_tool": "crawl_site",
                "prompt_variant": "crawl",
                "primary_dimensions": "",
                "secondary_dimensions": "",
                "repeat_runs": "2",
            },
            {
                "benchmark_id": "search-2",
                "query": "OpenAI background mode",
                "domain": "Web",
                "preferred_tool": "search",
                "prompt_variant": "balanced",
                "primary_dimensions": "",
                "secondary_dimensions": "",
                "repeat_runs": "2",
            },
        ]
        batch_cases: list[list[dict[str, object]]] = []

        def fake_run_remote_cases(**kwargs):  # type: ignore[no-untyped-def]
            cases = kwargs["cases"]
            batch_cases.append(cases)
            return [{"benchmark_id": case["benchmark_id"]} for case in cases]

        with patch.object(sys, "argv", argv), patch.object(
            run_remote_mcp_benchmark,
            "read_rows",
            return_value=rows,
        ), patch.object(
            run_remote_mcp_benchmark,
            "load_existing_rows",
            return_value=([], {}),
        ), patch.object(
            run_remote_mcp_benchmark,
            "run_remote_cases",
            side_effect=fake_run_remote_cases,
        ), patch.object(
            run_remote_mcp_benchmark,
            "merge_output_rows",
            return_value=[],
        ), patch.object(
            run_remote_mcp_benchmark,
            "write_output",
        ), patch.object(
            run_remote_mcp_benchmark.time,
            "sleep",
        ) as sleep:
            self.assertEqual(run_remote_mcp_benchmark.main(), 0)

        self.assertEqual(
            [[case["benchmark_id"] for case in cases] for cases in batch_cases],
            [["search-1"], ["map-1"], ["crawl-1"], ["search-2"]],
        )
        self.assertEqual(batch_cases[1][0]["repeat_runs"], 1)
        self.assertEqual(batch_cases[2][0]["repeat_runs"], 1)
        sleep.assert_called_once_with(run_remote_mcp_benchmark.FIRECRAWL_CRAWL_MAP_COOLDOWN_SECONDS)

    def test_summarize_handles_map_and_crawl_collections(self) -> None:
        namespace: dict[str, object] = {}
        helper_source = run_remote_mcp_benchmark.REMOTE_SCRIPT.split("\npayload = json.loads", 1)[0]
        exec(helper_source, namespace)

        mapped = namespace["summarize"](
            {
                "links": [
                    {"url": "https://example.com/a"},
                    "https://example.com/b",
                ],
                "count": 2,
            }
        )
        crawled = namespace["summarize"](
            {
                "pages": [
                    {"url": "https://example.com/a", "content": "A"},
                    {"url": "https://example.com/b", "content": "B"},
                ],
                "count": 2,
            }
        )

        self.assertEqual(mapped["summary"], "Mapped URLs: 2")
        self.assertEqual(mapped["urls"], ["https://example.com/a", "https://example.com/b"])
        self.assertFalse(mapped["empty_result"])
        self.assertEqual(crawled["summary"], "Crawled pages: 2")
        self.assertEqual(crawled["citation_count"], 2)

    def test_map_and_crawl_receive_longer_timeout_budgets(self) -> None:
        self.assertEqual(
            run_remote_mcp_benchmark.estimate_remote_case_timeout_seconds(
                {"mysearch_tool": "map_site", "tavily_tool": "tavily_map", "repeat_runs": 2}
            ),
            420,
        )
        self.assertEqual(
            run_remote_mcp_benchmark.estimate_remote_case_timeout_seconds(
                {"mysearch_tool": "crawl_site", "tavily_tool": "tavily_crawl", "repeat_runs": 1}
            ),
            600,
        )

    def test_missing_tavily_bearer_fails_when_comparator_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            argv = [
                "run_remote_mcp_benchmark.py",
                "--input-csv",
                "dummy.csv",
                "--output-csv",
                "out.csv",
                "--raw-dir",
                "raw",
                "--codex-config",
                str(Path(tmpdir) / "missing-config.toml"),
            ]
            with patch.object(sys, "argv", argv), patch.object(
                run_remote_mcp_benchmark,
                "read_rows",
                return_value=[
                    {
                        "benchmark_id": "case-1",
                        "query": "OpenAI pricing",
                        "domain": "Web",
                        "preferred_tool": "search",
                        "prompt_variant": "balanced",
                        "primary_dimensions": "",
                        "secondary_dimensions": "",
                    }
                ],
            ), patch.object(run_remote_mcp_benchmark, "load_existing_rows", return_value=([], {})):
                self.assertEqual(run_remote_mcp_benchmark.main(), 1)

    def test_resolve_tavily_bearer_reads_codex_mcp_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(
                """
[mcp_servers.tavily-hikari]
headers = { Authorization = "Bearer th-from-inline-headers" }
""".strip(),
                encoding="utf-8",
            )
            self.assertEqual(
                run_remote_mcp_benchmark.resolve_tavily_bearer(
                    "",
                    codex_config_path=config_path,
                    mcp_server_name="tavily-hikari",
                ),
                "th-from-inline-headers",
            )

    def test_main_uses_codex_config_bearer_when_cli_and_env_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(
                """
[mcp_servers.tavily-hikari.http_headers]
Authorization = "Bearer th-from-http-headers"
""".strip(),
                encoding="utf-8",
            )
            argv = [
                "run_remote_mcp_benchmark.py",
                "--input-csv",
                "dummy.csv",
                "--output-csv",
                "out.csv",
                "--raw-dir",
                "raw",
                "--codex-config",
                str(config_path),
            ]
            row = {
                "benchmark_id": "case-1",
                "query": "OpenAI pricing",
                "domain": "Web",
                "preferred_tool": "search",
                "prompt_variant": "balanced",
                "primary_dimensions": "",
                "secondary_dimensions": "",
                "repeat_runs": "1",
            }
            with patch.object(sys, "argv", argv), patch.object(
                run_remote_mcp_benchmark,
                "read_rows",
                return_value=[row],
            ), patch.object(run_remote_mcp_benchmark, "load_existing_rows", return_value=([], {})), patch.object(
                run_remote_mcp_benchmark,
                "run_remote_cases",
                return_value=[
                    {
                        "benchmark_id": "case-1",
                        "mysearch": {"ok": True, "blob": {}, "summary": "", "top_urls": []},
                        "tavily": {"ok": True, "blob": {}, "summary": "", "top_urls": []},
                    }
                ],
            ) as run_remote_cases, patch.object(run_remote_mcp_benchmark, "write_output") as write_output:
                self.assertEqual(run_remote_mcp_benchmark.main(), 0)
                write_output.assert_called_once()
                self.assertEqual(run_remote_cases.call_args.kwargs["tavily_bearer"], "th-from-http-headers")

    def test_missing_tavily_bearer_allowed_in_mysearch_only_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            argv = [
                "run_remote_mcp_benchmark.py",
                "--input-csv",
                "dummy.csv",
                "--output-csv",
                "out.csv",
                "--raw-dir",
                "raw",
                "--mysearch-only",
                "--codex-config",
                str(Path(tmpdir) / "missing-config.toml"),
            ]
            row = {
                "benchmark_id": "case-1",
                "query": "OpenAI pricing",
                "domain": "Web",
                "preferred_tool": "search",
                "prompt_variant": "balanced",
                "primary_dimensions": "",
                "secondary_dimensions": "",
                "repeat_runs": "1",
            }
            with patch.object(sys, "argv", argv), patch.object(
                run_remote_mcp_benchmark,
                "read_rows",
                return_value=[row],
            ), patch.object(run_remote_mcp_benchmark, "load_existing_rows", return_value=([], {})), patch.object(
                run_remote_mcp_benchmark,
                "run_remote_cases",
                return_value=[
                    {
                        "benchmark_id": "case-1",
                        "mysearch": {"ok": True, "blob": {}, "summary": "", "top_urls": []},
                        "tavily": {"ok": False, "blob": {}, "summary": "", "top_urls": []},
                    }
                ],
            ), patch.object(run_remote_mcp_benchmark, "write_output") as write_output:
                self.assertEqual(run_remote_mcp_benchmark.main(), 0)
                write_output.assert_called_once()

    def test_build_output_row_preserves_existing_tavily_columns_in_mysearch_only_mode(self) -> None:
        input_row = {
            "benchmark_id": "case-1",
            "query": "OpenAI pricing",
            "domain": "Web",
            "prompt_variant": "balanced",
            "primary_dimensions": "",
            "secondary_dimensions": "",
            "notes": "",
        }
        existing = {key: "" for key in run_remote_mcp_benchmark.FIELDNAMES}
        existing.update(
            {
                "benchmark_id": "case-1",
                "tavily_tool": "tavily_search",
                "tavily_summary": "existing Tavily summary",
                "tavily_top_urls": "https://openai.com/api/pricing",
                "tavily_citation_count": "1",
                "tavily_empty_result": "False",
                "notes": "tavily_raw=raw/case-1.tavily.json",
            }
        )
        item = {
            "benchmark_id": "case-1",
            "run_status": "captured",
            "mysearch_tool": "search",
            "mysearch_mode": "web",
            "mysearch_summary": "new MySearch summary",
            "tavily_tool": "tavily_search",
            "tavily_summary": "",
            "tavily_top_urls": "",
            "tavily_citation_count": 0,
            "tavily_empty_result": False,
        }

        row = run_remote_mcp_benchmark.build_output_row(
            input_row,
            item,
            Path("raw"),
            existing=existing,
            preserve_tavily=True,
        )

        self.assertEqual(row["mysearch_summary"], "new MySearch summary")
        self.assertEqual(row["tavily_summary"], "existing Tavily summary")
        self.assertEqual(row["tavily_top_urls"], "https://openai.com/api/pricing")
        self.assertEqual(row["tavily_citation_count"], "1")
        self.assertEqual(row["tavily_empty_result"], "False")
        self.assertIn("tavily_raw=raw/case-1.tavily.json", row["notes"])

    def test_build_output_row_clears_stale_structural_failure_on_normal_rerun(self) -> None:
        input_row = {
            "benchmark_id": "crawl-map-01",
            "domain": "站点地图",
            "query": "https://fastapi.tiangolo.com",
            "prompt_variant": "map",
            "primary_dimensions": "",
            "secondary_dimensions": "",
            "notes": "",
        }
        existing = {key: "" for key in run_remote_mcp_benchmark.FIELDNAMES}
        existing.update(
            {
                "benchmark_id": "crawl-map-01",
                "structural_failure": "tavily-search-upstream-rate-limited",
                "optimization_hint": "old hint",
            }
        )
        item = {
            "benchmark_id": "crawl-map-01",
            "run_status": "captured",
            "mysearch_tool": "map_site",
            "mysearch_mode": "map",
            "tavily_tool": "tavily_map",
            "mysearch_raw": '{"links":["https://fastapi.tiangolo.com"]}',
            "tavily_raw": '{"results":["https://fastapi.tiangolo.com"]}',
        }

        row = run_remote_mcp_benchmark.build_output_row(
            input_row,
            item,
            Path("raw"),
            existing=existing,
            preserve_tavily=False,
        )

        self.assertEqual(row["structural_failure"], "")
        self.assertEqual(row["optimization_hint"], "")


if __name__ == "__main__":
    unittest.main()
