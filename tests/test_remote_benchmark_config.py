from __future__ import annotations

import ast
import csv
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import run_remote_mcp_benchmark

# runner 不再内置默认真实目标主机（改由 MYSEARCH_BENCHMARK_HOST 提供），
# 测试显式传入文档用示例地址。
TEST_BENCHMARK_HOST = "root@172.16.0.10"


class RemoteBenchmarkConfigTests(unittest.TestCase):
    def test_run_remote_cases_keeps_bearer_out_of_process_arguments(self) -> None:
        bearer = "th-sensitive-bearer"
        with patch.object(
            run_remote_mcp_benchmark.subprocess,
            "run",
            return_value=SimpleNamespace(stdout="[]", stderr="", returncode=0),
        ) as run:
            result = run_remote_mcp_benchmark.run_remote_cases(
                host="root@example.test",
                mysearch_url="http://127.0.0.1:18000/mcp",
                tavily_url="http://127.0.0.1:8787/mcp",
                tavily_bearer=bearer,
                cases=[],
            )

        self.assertEqual(result, [])
        command = run.call_args.args[0]
        self.assertEqual(command[-2:], ["python3", "-"])
        self.assertNotIn(bearer, " ".join(command))

        remote_source = run.call_args.kwargs["input"]
        namespace: dict[str, object] = {}
        exec(remote_source.splitlines()[0], namespace)
        payload = json.loads(
            run_remote_mcp_benchmark.base64.b64decode(namespace["PAYLOAD_B64"]).decode()
        )
        self.assertEqual(payload["tavily_bearer"], bearer)

    def test_fieldnames_use_loop9_dimensions_and_explicit_orchestration_contract(self) -> None:
        for provider in ("mysearch", "tavily"):
            for dimension in run_remote_mcp_benchmark.BENCHMARK_DIMENSIONS:
                self.assertIn(f"{provider}_{dimension}_score", run_remote_mcp_benchmark.FIELDNAMES)
            self.assertIn(f"{provider}_orchestration_used", run_remote_mcp_benchmark.FIELDNAMES)
            self.assertIn(f"{provider}_fallback_attempted", run_remote_mcp_benchmark.FIELDNAMES)
            self.assertIn(f"{provider}_fallback_reason", run_remote_mcp_benchmark.FIELDNAMES)
            self.assertIn(f"{provider}_repeat_observations", run_remote_mcp_benchmark.FIELDNAMES)
            self.assertIn(f"{provider}_content_char_count", run_remote_mcp_benchmark.FIELDNAMES)
            self.assertIn(f"{provider}_content_noise_hits", run_remote_mcp_benchmark.FIELDNAMES)
            self.assertIn(f"{provider}_expected_answer_match", run_remote_mcp_benchmark.FIELDNAMES)
        self.assertIn("expected_answer_patterns", run_remote_mcp_benchmark.FIELDNAMES)
        self.assertNotIn("mysearch_accuracy_score", run_remote_mcp_benchmark.FIELDNAMES)
        self.assertNotIn("tavily_richness_score", run_remote_mcp_benchmark.FIELDNAMES)

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

    def test_summarize_keeps_provider_trace_valid_json_without_conflating_orchestration(self) -> None:
        namespace: dict[str, object] = {}
        helper_source = run_remote_mcp_benchmark.REMOTE_SCRIPT.split("\npayload = json.loads", 1)[0]
        exec(helper_source, namespace)
        blob = {
            "provider": "hybrid",
            "summary": "captured response",
            "results": [{"url": "https://example.com/result"}],
            "evidence": {
                "providers_consulted": ["tavily", "exa"],
                "retry_hint": "broaden verification",
                "diagnostic": "x" * 2000,
            },
        }

        summarized = namespace["summarize"](blob)
        trace = json.loads(summarized["provider_trace"])

        self.assertGreater(len(summarized["provider_trace"]), 1200)
        self.assertEqual(trace["provider"], "hybrid")
        self.assertTrue(summarized["orchestration_used"])
        self.assertFalse(summarized["fallback_attempted"])
        self.assertFalse(summarized["fallback_used"])
        self.assertEqual(summarized["fallback_reason"], "")

    def test_summarize_derives_fallback_only_from_actual_fallback_metadata(self) -> None:
        namespace: dict[str, object] = {}
        helper_source = run_remote_mcp_benchmark.REMOTE_SCRIPT.split("\npayload = json.loads", 1)[0]
        exec(helper_source, namespace)

        summarized = namespace["summarize"](
            {
                "provider": "exa",
                "summary": "fallback response",
                "results": [{"url": "https://example.com/result"}],
                "fallback": {"from": "tavily", "to": "exa", "reason": "primary returned no results"},
            }
        )

        self.assertTrue(summarized["fallback_attempted"])
        self.assertTrue(summarized["fallback_used"])
        self.assertEqual(summarized["fallback_reason"], "primary returned no results")

    def test_summarize_does_not_turn_derived_metrics_into_provider_trace(self) -> None:
        namespace: dict[str, object] = {}
        helper_source = run_remote_mcp_benchmark.REMOTE_SCRIPT.split("\npayload = json.loads", 1)[0]
        exec(helper_source, namespace)

        summarized = namespace["summarize"](
            {
                "results": [
                    {
                        "url": "https://example.com/result",
                        "content": "provider result content",
                        "published_date": "2026-07-18",
                    }
                ]
            }
        )

        self.assertEqual(summarized["provider_trace"], "")
        self.assertEqual(summarized["published_date_count"], 1)

    def test_summarize_measures_raw_content_instead_of_snippet_length(self) -> None:
        namespace: dict[str, object] = {}
        helper_source = run_remote_mcp_benchmark.REMOTE_SCRIPT.split("\npayload = json.loads", 1)[0]
        exec(helper_source, namespace)

        raw_content = "Canonical documentation body " * 60
        summarized = namespace["summarize"](
            {
                "results": [
                    {
                        "url": "https://example.com/docs",
                        "content": "short search snippet",
                        "raw_content": raw_content,
                    }
                ]
            }
        )

        self.assertEqual(summarized["content_char_count"], len(raw_content.strip()))
        self.assertEqual(summarized["content_item_count"], 1)
        self.assertEqual(summarized["content_noise_hits"], 0)

    def test_timed_tool_runs_retains_cold_and_warm_observations_and_result_stability(self) -> None:
        namespace: dict[str, object] = {}
        helper_source = run_remote_mcp_benchmark.REMOTE_SCRIPT.split("\npayload = json.loads", 1)[0]
        exec(helper_source, namespace)

        class FakeClient:
            def __init__(self) -> None:
                self.calls = 0

            def call_tool(self, tool_name, arguments):  # type: ignore[no-untyped-def]
                self.calls += 1
                return {
                    "result": {
                        "content": [
                            {
                                "text": json.dumps(
                                    {
                                        "summary": f"response {self.calls}",
                                        "results": [{"url": f"https://example.com/{self.calls}"}],
                                    }
                                )
                            }
                        ]
                    }
                }

        perf_counter_values = [0.0, 0.1, 1.0, 1.05, 2.0, 2.02]
        with patch.object(namespace["time"], "perf_counter", side_effect=perf_counter_values):
            observed = namespace["timed_tool_runs"](FakeClient(), "search", {"query": "x"}, 3, 75)

        observations = json.loads(observed["repeat_observations"])
        variance = json.loads(observed["repeat_variance"])
        self.assertEqual([item["summary"] for item in observations], ["response 1", "response 2", "response 3"])
        self.assertEqual([item["cache_state"] for item in observations], ["cold", "warm", "warm"])
        self.assertEqual([item["latency_ms"] for item in observations], [100.0, 50.0, 20.0])
        self.assertEqual(observed["cold_latency_ms"], 100.0)
        self.assertEqual(observed["warm_latency_ms"], 35.0)
        self.assertTrue(observed["latency_budget_exceeded"])
        self.assertEqual(variance["latency_range_ms"], 80.0)
        self.assertLess(variance["result_stability"], 1.0)

    def _remote_repeat_variance(self):
        namespace: dict[str, object] = {}
        helper_source = run_remote_mcp_benchmark.REMOTE_SCRIPT.split("\npayload = json.loads", 1)[0]
        exec(helper_source, namespace)
        return namespace["repeat_variance"]

    def test_repeat_variance_treats_all_empty_runs_as_unstable(self) -> None:
        # 回归：上游可返回 HTTP 200 但结果为空，"transport 成功"不等于"有结果"。
        # 修复前这种运行 consistency_parts 为空 -> consistency 默认 1.0，
        # 配合 success_ratio=1.0 反而拿到 result_stability=1.0（resilience 满分）。
        # 实证据：loop27 的 research-01 / longtail-academic-01 三次全返回
        # "Research request failed" 却得 5.0，且已同时被标为
        # tavily-*-upstream-plan-limited。
        repeat_variance = self._remote_repeat_variance()
        all_error = [
            {
                "success": True,
                "urls": [],
                "summary": "Research request failed",
                "citation_count": 0,
                "content_char_count": 0,
                "latency_ms": 300.0,
            }
            for _ in range(3)
        ]
        variance = repeat_variance(all_error)
        self.assertEqual(variance["result_stability"], 0.0)
        self.assertEqual(variance["nonempty_runs"], 0)
        self.assertEqual(variance["successful_runs"], 3)

    def test_repeat_variance_keeps_url_less_research_synthesis_stable(self) -> None:
        # 反向回归：正当的 research 综合答案不带 URL，但有真实正文。
        # 判据若只看 urls 会把这类结果误判为不稳定。
        repeat_variance = self._remote_repeat_variance()
        legit = [
            {
                "success": True,
                "urls": [],
                "summary": "## Top Search-Oriented MCP Servers",
                "citation_count": 0,
                "content_char_count": 10525,
                "latency_ms": 900.0,
            },
            {
                "success": True,
                "urls": [],
                "summary": "## Top Search-Oriented MCP Servers",
                "citation_count": 0,
                "content_char_count": 10525,
                "latency_ms": 800.0,
            },
        ]
        variance = repeat_variance(legit)
        self.assertEqual(variance["result_stability"], 1.0)
        self.assertEqual(variance["nonempty_runs"], 2)

    def test_repeat_variance_scales_with_intermittent_empty_runs(self) -> None:
        # loop30 official-web-01 的形态：3 次里只有 1 次有结果。
        repeat_variance = self._remote_repeat_variance()
        mixed = [
            {
                "success": True,
                "urls": ["https://developers.openai.com/api/docs/pricing"],
                "summary": "pricing",
                "citation_count": 5,
                "content_char_count": 500,
                "latency_ms": 387.0,
            },
            {
                "success": True,
                "urls": [],
                "summary": "Search failed",
                "citation_count": 0,
                "content_char_count": 0,
                "latency_ms": 549.0,
            },
            {
                "success": True,
                "urls": [],
                "summary": "Search failed",
                "citation_count": 0,
                "content_char_count": 0,
                "latency_ms": 336.0,
            },
        ]
        variance = repeat_variance(mixed)
        self.assertEqual(variance["result_stability"], 0.333)
        self.assertEqual(variance["nonempty_runs"], 1)

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
                "latency_budget_ms": "20000",
                "sources_hint": "web|x",
            }
        )

        self.assertEqual(case["mysearch_tool"], "search")
        self.assertEqual(case["mysearch_args"]["sources"], ["web", "x"])
        self.assertEqual(case["tavily_tool"], "tavily_search")
        self.assertEqual(case["latency_budget_ms"], 20000.0)

    def test_build_case_requests_content_for_active_content_fidelity(self) -> None:
        case = run_remote_mcp_benchmark.build_case(
            {
                "benchmark_id": "content-row",
                "domain": "Docs",
                "query": "OpenAI API pricing official Chinese",
                "prompt_variant": "strict",
                "preferred_tool": "search",
                "mode_hint": "web",
                "strategy_hint": "verify",
                "primary_dimensions": "authority_precision",
                "secondary_dimensions": "content_fidelity",
                "repeat_runs": "1",
            }
        )

        self.assertTrue(case["mysearch_args"]["include_content"])
        self.assertTrue(case["tavily_args"]["include_raw_content"])

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
            "--host",
            TEST_BENCHMARK_HOST,
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
                "--host",
                TEST_BENCHMARK_HOST,
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
                "--host",
                TEST_BENCHMARK_HOST,
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
                "--host",
                TEST_BENCHMARK_HOST,
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

    def test_missing_host_fails_fast_before_touching_remote(self) -> None:
        # runner 不再内置默认真实目标主机；未提供时必须显式失败，
        # 而不是拿空 host 去连（会静默连错或抛难懂的 ssh 错误）。
        argv = [
            "run_remote_mcp_benchmark.py",
            "--input-csv",
            "dummy.csv",
            "--output-csv",
            "out.csv",
            "--raw-dir",
            "raw",
            "--mysearch-only",
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
        # 显式清空 env 与该模块的默认值，避免本地已配置 MYSEARCH_BENCHMARK_HOST 时行为漂移。
        with patch.dict(os.environ, {"MYSEARCH_BENCHMARK_HOST": ""}), patch.object(
            run_remote_mcp_benchmark, "DEFAULT_HOST", ""
        ), patch.object(sys, "argv", argv), patch.object(
            run_remote_mcp_benchmark, "read_rows", return_value=[row]
        ), patch.object(
            run_remote_mcp_benchmark, "run_remote_cases"
        ) as run_remote_cases:
            self.assertEqual(run_remote_mcp_benchmark.main(), 1)
            run_remote_cases.assert_not_called()

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

    def test_build_output_row_enforces_latency_budget_and_scores_successful_dual_run(self) -> None:
        input_row = {
            "benchmark_id": "case-budget",
            "query": "OpenAI pricing",
            "domain": "Web",
            "prompt_variant": "strict",
            "include_domains": "openai.com",
            "exclude_domains": "",
            "strict_required": "true",
            "primary_dimensions": "authority_precision|efficiency",
            "secondary_dimensions": "traceability|resilience",
            "latency_budget_ms": "100",
            "notes": "",
        }
        variance = json.dumps(
            {
                "latency_range_ms": 0.0,
                "result_stability": 1.0,
                "successful_runs": 1,
                "attempted_runs": 1,
            }
        )
        item = {
            "benchmark_id": "case-budget",
            "run_status": "captured",
            "mysearch_tool": "search",
            "mysearch_mode": "web",
            "mysearch_summary": "official pricing result",
            "mysearch_top_urls": "https://openai.com/api/pricing",
            "mysearch_citation_count": 1,
            "mysearch_repeat_variance": variance,
            "mysearch_repeat_observations": json.dumps([{"success": True, "latency_ms": 120.0}]),
            "mysearch_empty_result": False,
            "tavily_tool": "tavily_search",
            "tavily_summary": "official pricing result",
            "tavily_top_urls": "https://openai.com/api/pricing",
            "tavily_citation_count": 1,
            "tavily_repeat_variance": variance,
            "tavily_repeat_observations": json.dumps([{"success": True, "latency_ms": 80.0}]),
            "tavily_empty_result": False,
        }

        row = run_remote_mcp_benchmark.build_output_row(input_row, item, Path("raw"))

        self.assertEqual(row["run_status"], "budget-exceeded")
        self.assertTrue(row["mysearch_latency_budget_exceeded"])
        self.assertFalse(row["tavily_latency_budget_exceeded"])
        self.assertLess(row["mysearch_efficiency_score"], row["tavily_efficiency_score"])
        for provider in ("mysearch", "tavily"):
            for dimension in run_remote_mcp_benchmark.BENCHMARK_DIMENSIONS:
                self.assertGreaterEqual(row[f"{provider}_{dimension}_score"], 0.0)
                self.assertLessEqual(row[f"{provider}_{dimension}_score"], 5.0)
            self.assertGreater(row[f"{provider}_total_score"], 0.0)
        self.assertEqual(row["winner"], "tavily")
        self.assertNotEqual(row["winner"], "pending-review")
        self.assertIn("semantic correctness was not inferred", row["winner_reason"])

    def test_authority_precision_requires_expected_canonical_url_at_the_top(self) -> None:
        input_row = {
            "benchmark_id": "official-pricing",
            "query": "OpenAI API pricing official",
            "domain": "Web",
            "prompt_variant": "strict",
            "include_domains": "openai.com",
            "expected_url_patterns": "/api/pricing|/api/docs/pricing",
            "strict_required": "true",
            "primary_dimensions": "authority_precision",
            "secondary_dimensions": "traceability",
            "latency_budget_ms": "15000",
            "notes": "",
        }
        common = {
            "run_status": "captured",
            "mysearch_tool": "search",
            "mysearch_mode": "web",
            "mysearch_citation_count": 2,
            "mysearch_empty_result": False,
            "tavily_tool": "tavily_search",
            "tavily_summary": "canonical result",
            "tavily_top_urls": "https://openai.com/api/pricing | https://openai.com/careers/pricing-strategist",
            "tavily_citation_count": 2,
            "tavily_empty_result": False,
        }
        item = {
            **common,
            "mysearch_summary": "domain-correct but misranked result",
            "mysearch_top_urls": "https://openai.com/careers/pricing-strategist | https://openai.com/api/pricing",
        }

        row = run_remote_mcp_benchmark.build_output_row(input_row, item, Path("raw"))

        self.assertEqual(row["mysearch_authority_precision_score"], 4.0)
        self.assertEqual(row["tavily_authority_precision_score"], 5.0)

    def test_explicit_expected_answer_contract_rejects_stale_factual_summary(self) -> None:
        input_row = {
            "benchmark_id": "python-version",
            "query": "what is the latest stable version of Python",
            "domain": "Facts",
            "prompt_variant": "baseline",
            "expected_answer_patterns": "3.14.6",
            "primary_dimensions": "authority_precision|freshness_signal",
            "secondary_dimensions": "traceability|resilience",
            "latency_budget_ms": "15000",
            "notes": "",
        }
        item = {
            "benchmark_id": "python-version",
            "run_status": "captured",
            "mysearch_tool": "search",
            "mysearch_mode": "web",
            "mysearch_summary": "The latest stable version of Python is 3.14.6.",
            "mysearch_top_urls": "https://www.python.org/downloads/",
            "mysearch_citation_count": 1,
            "mysearch_empty_result": False,
            "tavily_tool": "tavily_search",
            "tavily_summary": "As of February 2026, Python 3.14.3 is latest.",
            "tavily_top_urls": "https://example.com/stale-python-version",
            "tavily_citation_count": 1,
            "tavily_empty_result": False,
        }

        row = run_remote_mcp_benchmark.build_output_row(input_row, item, Path("raw"))

        self.assertTrue(row["mysearch_expected_answer_match"])
        self.assertFalse(row["tavily_expected_answer_match"])
        self.assertEqual(row["mysearch_authority_precision_score"], 3.0)
        self.assertEqual(row["mysearch_freshness_signal_score"], 5.0)
        self.assertEqual(row["tavily_freshness_signal_score"], 0.0)
        self.assertEqual(row["winner"], "mysearch")
        self.assertIn("explicit expected-answer contract", row["winner_reason"])

    def test_expected_answer_contract_respects_boundaries_and_negation(self) -> None:
        matcher = run_remote_mcp_benchmark._summary_matches_expected_answer

        self.assertTrue(matcher("Python v3.14.6 is the latest stable release.", ["3.14.6"]))
        self.assertFalse(matcher("Python 3.14.60 is the latest stable release.", ["3.14.6"]))
        self.assertFalse(matcher("Python 3.14.6 is not the latest stable release.", ["3.14.6"]))
        self.assertFalse(matcher("The latest stable version is not 3.14.6.", ["3.14.6"]))

    def test_partial_merge_syncs_current_expected_answer_contract(self) -> None:
        input_row = {
            "benchmark_id": "python-version",
            "query": "what is the latest stable version of Python",
            "domain": "Facts",
            "prompt_variant": "baseline",
            "expected_answer_patterns": "3.14.6",
            "primary_dimensions": "freshness_signal",
            "secondary_dimensions": "traceability",
            "latency_budget_ms": "15000",
            "notes": "",
        }
        existing = {key: "" for key in run_remote_mcp_benchmark.FIELDNAMES}
        existing.update(
            {
                "benchmark_id": "python-version",
                "run_status": "captured",
                "expected_answer_patterns": "3.14.5",
                "mysearch_tool": "search",
                "mysearch_summary": "Python 3.14.6 is the latest stable release.",
                "mysearch_top_urls": "https://www.python.org/downloads/",
                "mysearch_citation_count": "1",
                "mysearch_empty_result": "False",
                "tavily_tool": "tavily_search",
                "tavily_summary": "Python 3.14.3 is the latest stable release.",
                "tavily_top_urls": "https://example.com/stale",
                "tavily_citation_count": "1",
                "tavily_empty_result": "False",
            }
        )

        rows = run_remote_mcp_benchmark.merge_output_rows(
            [input_row],
            [],
            [],
            Path("raw"),
            existing_order=["python-version"],
            existing_rows={"python-version": existing},
            preserve_tavily=False,
        )

        self.assertEqual(rows[0]["expected_answer_patterns"], "3.14.6")
        self.assertTrue(rows[0]["mysearch_expected_answer_match"])
        self.assertFalse(rows[0]["tavily_expected_answer_match"])

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


class TimeWindowAlignmentTests(unittest.TestCase):
    """Tavily 与 MySearch 必须拿到等价的时间窗。

    历史缺陷：runner 只给 Tavily 传 `time_range`，MySearch 侧不传
    `from_date`/`to_date`，于是 11 行上 Tavily 有日期过滤而 MySearch 没有 ——
    "freshness 不如 Tavily" 可能只是评测条件不对等造成的假象。
    """

    def _row(self, domain: str, benchmark_id: str = "x-01") -> dict[str, str]:
        return {"benchmark_id": benchmark_id, "domain": domain}

    def test_tavily_and_mysearch_windows_agree_for_every_domain(self) -> None:
        domains = [
            "新闻",
            "技术动态 / status",
            "娱乐",
            "八卦",
            "纯 Social / X",
            "更新日志 / release",
            "网页",
            "技术文档",
            "PDF",
        ]
        for domain in domains:
            row = self._row(domain)
            tavily_has = bool(run_remote_mcp_benchmark.map_tavily_time_range(row))
            from_date, to_date = run_remote_mcp_benchmark.map_mysearch_date_bounds(row)
            with self.subTest(domain=domain):
                self.assertEqual(
                    tavily_has,
                    bool(from_date),
                    f"{domain}: 一侧有日期过滤而另一侧没有",
                )
                if from_date:
                    self.assertLess(from_date, to_date)
                    self.assertRegex(from_date, r"^\d{4}-\d{2}-\d{2}$")
                    self.assertRegex(to_date, r"^\d{4}-\d{2}-\d{2}$")

    def test_month_window_is_thirty_days(self) -> None:
        from_date, to_date = run_remote_mcp_benchmark.map_mysearch_date_bounds(
            self._row("新闻")
        )
        start = date.fromisoformat(from_date)
        end = date.fromisoformat(to_date)
        self.assertEqual((end - start).days, 30)

    def test_year_window_is_365_days(self) -> None:
        from_date, to_date = run_remote_mcp_benchmark.map_mysearch_date_bounds(
            self._row("更新日志 / release")
        )
        start = date.fromisoformat(from_date)
        end = date.fromisoformat(to_date)
        self.assertEqual((end - start).days, 365)

    def test_domains_without_a_time_range_send_neither_side(self) -> None:
        row = self._row("网页")
        self.assertIsNone(run_remote_mcp_benchmark.map_tavily_time_range(row))
        self.assertEqual(
            run_remote_mcp_benchmark.map_mysearch_date_bounds(row), ("", "")
        )

    def test_build_case_wires_the_window_into_mysearch_args(self) -> None:
        row = {
            "benchmark_id": "news-01",
            "domain": "新闻",
            "query": "q",
            "prompt_variant": "baseline",
            "preferred_tool": "search",
            "mode_hint": "news",
            "strategy_hint": "verify",
            "repeat_runs": "1",
            "latency_budget_ms": "15000",
        }
        case = run_remote_mcp_benchmark.build_case(row)
        mysearch_args = case["mysearch_args"]
        tavily_args = case["tavily_args"]
        self.assertIn("from_date", mysearch_args)
        self.assertIn("to_date", mysearch_args)
        self.assertEqual(tavily_args.get("time_range"), "month")

    def test_build_case_omits_the_window_when_tavily_has_none(self) -> None:
        row = {
            "benchmark_id": "web-01",
            "domain": "网页",
            "query": "q",
            "prompt_variant": "baseline",
            "preferred_tool": "search",
            "mode_hint": "web",
            "strategy_hint": "balanced",
            "repeat_runs": "1",
            "latency_budget_ms": "15000",
        }
        case = run_remote_mcp_benchmark.build_case(row)
        self.assertNotIn("from_date", case["mysearch_args"])
        self.assertNotIn("time_range", case["tavily_args"])


class AutoStrategyTests(unittest.TestCase):
    """空 `strategy_hint` 必须表示 `auto`，而不是按 prompt_variant 猜。

    `_resolve_strategy` 的推导分支只有调用方不传 strategy 时才走；历史实现里
    空值会退化成按 variant 猜（strict→verify 等），使那条推导链零覆盖。
    """

    def _row(self, strategy_hint: str, variant: str = "baseline") -> dict[str, str]:
        return {"strategy_hint": strategy_hint, "prompt_variant": variant}

    def test_empty_hint_maps_to_auto(self) -> None:
        self.assertEqual(run_remote_mcp_benchmark.map_strategy(self._row("")), "auto")

    def test_empty_hint_is_not_inferred_from_prompt_variant(self) -> None:
        for variant in ("strict", "research", "status", "baseline"):
            with self.subTest(variant=variant):
                self.assertEqual(
                    run_remote_mcp_benchmark.map_strategy(self._row("", variant)),
                    "auto",
                )

    def test_explicit_hint_still_wins(self) -> None:
        for hint in ("fast", "balanced", "verify", "deep", "auto"):
            with self.subTest(hint=hint):
                self.assertEqual(
                    run_remote_mcp_benchmark.map_strategy(self._row(hint)), hint
                )

    def test_tavily_depth_treats_auto_like_fast(self) -> None:
        self.assertEqual(
            run_remote_mcp_benchmark.map_tavily_search_depth(self._row("")), "fast"
        )
        self.assertEqual(
            run_remote_mcp_benchmark.map_tavily_search_depth(self._row("verify")),
            "advanced",
        )

    def test_shipped_matrix_keeps_exactly_one_auto_row(self) -> None:
        """矩阵里应当**恰好一行**不钉 strategy，用来覆盖 `_resolve_strategy` 的推导。

        这条测试原先断言"每行都显式给 strategy"（即 0 行 auto），把它当时
        记录的缺口当成了期望行为 —— loop18 的 P1 写得很清楚：
        runner 侧 `map_strategy` 的空值已改为返回 `auto`，但矩阵全填了
        strategy_hint，于是 5 个推导分支零覆盖，"机制修了、覆盖没修"。

        现在 `fast-02`（纯事实快问）留空，应推导为 fast。
        断言精确到 1 行：多行留空会让矩阵大面积变成 auto，
        少到 0 行则缺口重新出现。
        """
        matrix = (
            REPO_ROOT
            / ".codex-tasks"
            / "20260530-provider-optimization-loop-v2"
            / "raw"
            / "loop11-benchmark-input-final.csv"
        )
        if not matrix.exists():  # 任务目录可能未随仓库分发
            self.skipTest("benchmark matrix not present")
        with matrix.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        auto = [r["benchmark_id"] for r in rows if run_remote_mcp_benchmark.map_strategy(r) == "auto"]
        self.assertEqual(
            len(auto),
            1,
            f"应恰好一行解析为 auto（覆盖 _resolve_strategy 推导），实得 {auto}",
        )
        self.assertEqual(auto, ["fast-02"])


class ContentMetricsFairnessTests(unittest.TestCase):
    """`content_metrics` 必须把两侧**同一语义的字段**都计入。

    Tavily 把每条结果的摘要放在 `content`；MySearch 把同样的东西放在 `snippet`。
    若只读 `content`，在 `content_fidelity` 未激活的行上（两侧都取不到正文）
    Tavily 仍有几千字符而 MySearch 记 0 —— 两边不可比。实测 loop24 有 23 行如此，
    计入 `snippet` 后 41/45 行的 MySearch 内容量上升（总量 +52%）。
    """

    def _load(self):
        # The measurement helpers live inside the runner's REMOTE_SCRIPT string,
        # which is what actually executes against the deployed server.
        source = (REPO_ROOT / "scripts" / "run_remote_mcp_benchmark.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "REMOTE_SCRIPT":
                namespace = {"PAYLOAD_B64": ""}
                try:
                    exec(compile(node.value.value, "<remote>", "exec"), namespace)
                except Exception:
                    pass
                return namespace["content_metrics"]
        raise AssertionError("REMOTE_SCRIPT not found")

    def test_counts_the_snippet_field(self) -> None:
        metrics = self._load()
        blob = {"results": [{"url": "https://x", "content": "", "snippet": "a" * 900}]}
        self.assertEqual(metrics(blob)["char_count"], 900)

    def test_snippet_and_content_are_not_double_counted(self) -> None:
        metrics = self._load()
        blob = {"results": [{"url": "https://x", "content": "c" * 300, "snippet": "s" * 300}]}
        self.assertEqual(metrics(blob)["char_count"], 600)

    def test_raw_content_still_wins_over_the_snippet(self) -> None:
        metrics = self._load()
        blob = {"results": [{"url": "https://x", "raw_content": "r" * 500, "snippet": "s" * 300}]}
        self.assertEqual(metrics(blob)["char_count"], 500)


class MatrixContractTests(unittest.TestCase):
    """守护矩阵必须持续覆盖几个"机制修了、覆盖没修"的口子。

    loop18 的 P1 记录了这个模式：runner 侧把 `map_strategy` 的空值从
    "按 prompt_variant 猜"改成返回 `auto`，但**矩阵 45 行全部显式填了
    strategy_hint**，于是 `_resolve_strategy` 的 5 个推导分支依旧零覆盖，
    改机制没有产生任何可观察的差异。这组测试把覆盖本身钉住。
    """

    MATRIX = (
        REPO_ROOT
        / ".codex-tasks"
        / "20260530-provider-optimization-loop-v2"
        / "raw"
        / "loop11-benchmark-input-final.csv"
    )

    def setUp(self) -> None:
        # `.codex-tasks/` 被 gitignore，矩阵不随仓库分发；CI 的全新 clone 里
        # 没有这个文件。缺文件时跳过，而不是让整套测试崩掉。
        if not self.MATRIX.exists():
            self.skipTest("benchmark matrix not present")

    def _rows(self) -> list[dict[str, str]]:
        with self.MATRIX.open(encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def test_matrix_has_a_row_that_resolves_to_auto_strategy(self) -> None:
        rows = self._rows()
        auto = [row["benchmark_id"] for row in rows if run_remote_mcp_benchmark.map_strategy(row) == "auto"]
        self.assertTrue(
            auto,
            "矩阵里没有任何一行解析出 strategy=auto，"
            "_resolve_strategy 的推导分支重新变成零覆盖",
        )

    def test_news_rows_have_a_falsifiable_answer_expectation(self) -> None:
        """新闻类行必须有可证伪的答案期望值。

        没有期望值时，runner 的 authority_precision 走"无断言"分支，
        freshness_signal 退化成只看有没有日期（不看内容对不对）——
        这些行测的是"跑通了"，不是"答对了"。

        实测证明了这条必要性：loop33 的健康 run 里 news-03 抽出
        "Best Actor winner: John Malkovich"（那是**威尼斯电影节**的奖），
        entertainment-02 抽出 "Morton Gould"（**1967 年**的格莱美）——
        两行当时都没有期望值，所以没有任何计分项能发现它们答错了。
        """
        rows = {row["benchmark_id"]: row for row in self._rows()}
        news = sorted(bid for bid in rows if bid.startswith("news-"))
        self.assertTrue(news, "矩阵里没有 news-* 行")
        missing = sorted(
            bid
            for bid in news
            if not rows[bid]["expected_answer_patterns"].strip()
        )
        self.assertEqual(missing, [], f"这些新闻行没有答案期望值: {missing}")

    def test_extract_and_crawl_rows_document_why_they_have_no_url_expectation(self) -> None:
        """抽取/爬取行**无法**用 expected_url_patterns 证伪 —— 记录这个缺口。

        `collect_urls` 对 extract_url/map_site/crawl_site 的响应会先取
        `blob["url"]`，而输入就是那个 URL（实测 loop33：
        `extract-01` 的 top_urls[0] == 查询 URL）。所以给这些行填
        `expected_url_patterns` 会是一个**恒真断言**，还给
        authority_precision 白送 +1.5 分。真正的缺口在 runner：
        没有任何计分项检查"抽取到的正文是否包含某个事实"。
        """
        rows = {row["benchmark_id"]: row for row in self._rows()}
        scoped = sorted(
            bid
            for bid in rows
            if bid.startswith(("extract-", "hard-extract-", "crawl-map-"))
        )
        self.assertTrue(scoped, "矩阵里没有抽取/爬取行")
        # 恒真断言比没有断言更糟：它把一个真空包装成"已覆盖"。
        offenders = [
            bid for bid in scoped if rows[bid]["expected_url_patterns"].strip()
        ]
        self.assertEqual(
            offenders,
            [],
            "抽取/爬取行不该有 expected_url_patterns —— 输入 URL 会被回显，"
            f"断言恒真: {offenders}",
        )


if __name__ == "__main__":
    unittest.main()
