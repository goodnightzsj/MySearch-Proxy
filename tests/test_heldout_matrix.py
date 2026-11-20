import csv
import importlib.util
from pathlib import Path
from unittest import TestCase

from scripts.run_remote_mcp_benchmark import build_case


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("holdout", ROOT / "scripts/generate_heldout_matrix.py")
holdout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(holdout)


class HeldoutMatrixTests(TestCase):
    def test_seeded_strata_and_existing_runner_contract(self):
        with (ROOT / "benchmarks/search-matrix.csv").open() as source:
            regression = list(csv.DictReader(source))
        fields = [*regression[0], "include_content"]
        rows = holdout.generate_rows(42, fields)
        self.assertEqual(rows, holdout.generate_rows(42, fields))
        self.assertNotEqual(rows, holdout.generate_rows(43, fields))
        self.assertEqual(len(rows), 32)
        self.assertTrue({row["query"] for row in rows}.isdisjoint(row["query"] for row in regression))
        self.assertEqual({row["preferred_tool"] for row in rows}, {"search", "research", "extract_url", "map_site", "crawl_site"})
        for row in rows:
            case = build_case(row)
            self.assertIn("mysearch_args", case)
            self.assertIn("tavily_args", case)
            self.assertEqual(case["repeat_runs"], int(row["repeat_runs"]))
            self.assertTrue(row["expected_url_patterns"] or row["expected_content_patterns"])
            if row["preferred_tool"] == "search":
                self.assertEqual(case["mysearch_args"]["include_content"], row["include_content"] == "true")
        for profile in holdout.PROFILES:
            selected = [row for row in rows if row["benchmark_id"].startswith("heldout-" + profile[0] + "-")]
            self.assertEqual(len(selected), 2)
            self.assertTrue(any("language=zh" in row["notes"] for row in selected))
            self.assertTrue(any("language=en" in row["notes"] for row in selected))
