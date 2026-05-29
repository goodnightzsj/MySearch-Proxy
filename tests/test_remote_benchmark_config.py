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

    def test_classify_tavily_structural_failure_maps_session_required_variant(self) -> None:
        self.assertEqual(
            run_remote_mcp_benchmark.classify_tavily_structural_failure(
                "",
                "strict-constraint-03",
                'tavily: HTTP 400: {"error":"session_required","message":"MCP requests after initialize must include mcp-session-id."}',
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


if __name__ == "__main__":
    unittest.main()
