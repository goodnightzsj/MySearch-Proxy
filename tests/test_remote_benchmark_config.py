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
