from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import run_remote_mcp_benchmark


class RemoteBenchmarkConfigTests(unittest.TestCase):
    def test_missing_tavily_bearer_fails_when_comparator_enabled(self) -> None:
        argv = [
            "run_remote_mcp_benchmark.py",
            "--input-csv",
            "dummy.csv",
            "--output-csv",
            "out.csv",
            "--raw-dir",
            "raw",
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

    def test_missing_tavily_bearer_allowed_in_mysearch_only_mode(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
