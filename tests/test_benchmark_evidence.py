from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts import run_remote_mcp_benchmark as runner
from scripts import audit_matrix_assertions as audit


class BenchmarkEvidenceTests(unittest.TestCase):
    def test_grounding_ignores_generated_answers_and_metadata_at_every_level(self):
        answer = "Java 4.5"
        for prefix in ("mysearch", "tavily"):
            raw = {
                "answer": answer, "summary": answer, "query": answer,
                "report_markdown": answer, "research_summary": answer,
                "metadata": {"content": answer}, "evidence": {"claim": answer},
                "web_search": {"answer": answer, "summary": answer,
                               "results": [{"url": answer, "title": answer, "snippet": "Java 25"}]},
            }
            row = {f"{prefix}_summary": answer, f"{prefix}_raw": json.dumps(raw)}
            self.assertEqual(runner._claim_groundedness(row, prefix), (0.5, 2))
            raw["pages"] = [{"url": "https://example.test/java", "content": "Java 4.5"}]
            row[f"{prefix}_raw"] = json.dumps(raw)
            self.assertEqual(runner._claim_groundedness(row, prefix), (1.0, 2))

    def test_grounding_factual_answers_without_source_text_is_zero(self):
        for raw in ("", "not valid JSON", '{"answer": "Java 4.5", "results": []}'):
            for prefix in ("mysearch", "tavily"):
                row = {f"{prefix}_summary": "Java 4.5", f"{prefix}_raw": raw}
                self.assertEqual(runner._claim_groundedness(row, prefix), (0.0, 2))

    def test_median_raw_matches_scored_sample_and_saved_artifact(self):
        for tool in ("search", "tavily_search"):
            for fail_first in (False, True):
                with self.subTest(tool=tool, fail_first=fail_first), tempfile.TemporaryDirectory() as directory:
                    _check_median_sample(Path(directory), tool, fail_first)

    def test_body_assertions_ignore_echoes_and_require_each_fact(self):
        contract = {"expected_content_patterns": "returning a response|add_task"}
        echo = "returning a response add_task"
        for prefix in ("mysearch", "tavily"):
            for body in ("", "An unrelated article."):
                raw = {"url": echo, "title": echo, "answer": echo, "summary": echo,
                       "metadata": {"description": echo}, "content": body}
                row = {f"{prefix}_summary": echo, f"{prefix}_raw": json.dumps(raw)}
                with self.subTest(prefix=prefix, body=body):
                    self.assertEqual(runner._assertion_pass_rate(row, prefix, contract), 0.0)
            for raw in (
                {"content": "Run after RETURNING\n a response with add_task."},
                {"results": [{"raw_content": echo}]},
                {"pages": [{"content": echo}]},
            ):
                row = {f"{prefix}_raw": json.dumps(raw)}
                self.assertEqual(runner._assertion_pass_rate(row, prefix, contract), 1.0)
            row = {f"{prefix}_raw": json.dumps({"content": "returning a response"})}
            self.assertEqual(runner._assertion_pass_rate(row, prefix, contract), 0.5)
            self.assertEqual(runner._assertion_pass_rate({}, prefix, contract), 0.0)

    def test_map_assertions_read_discovered_links_not_root_or_top_three(self):
        root = "https://example.com"
        expected = f"{root}/tutorial/first-steps/"
        contract = {"preferred_tool": "map_site", "query": root,
                    "expected_url_patterns": "/tutorial/first-steps/"}
        for prefix in ("mysearch", "tavily"):
            row = {f"{prefix}_top_urls": root,
                   f"{prefix}_raw": json.dumps({"url": expected, "links": [root]})}
            self.assertEqual(runner._assertion_pass_rate(row, prefix, contract), 0.0)
            for target in (expected, {"url": expected}):
                row[f"{prefix}_raw"] = json.dumps(
                    {"links": [root, f"{root}/a", f"{root}/b", target]}
                )
                self.assertEqual(runner._assertion_pass_rate(row, prefix, contract), 1.0)

    def test_body_and_map_audit_detects_empty_results(self):
        for contract in (
            {"expected_content_patterns": "returning a response|add_task"},
            {"preferred_tool": "map_site", "query": "https://example.com",
             "expected_url_patterns": "/tutorial/first-steps/"},
        ):
            result = audit.audit_row(contract)
            self.assertTrue(result["baseline_satisfies_assertions"])
            self.assertTrue(result["blank_results_detected"])
            self.assertTrue(result["falsifiable"])

    def test_ranking_score_tracks_first_relevant_result_in_top_three(self):
        contract = {"primary_dimensions": "ranking_quality",
                    "expected_url_patterns": "/correct", "preferred_tool": "search"}
        for prefix in ("mysearch", "tavily"):
            for rank, expected_score in ((1, 5.0), (2, 2.5), (3, 5 / 3), (4, 0.0)):
                urls = [f"https://example.com/wrong-{i}" for i in range(4)]
                urls[rank - 1] = "https://example.com/correct"
                row = {"run_status": "captured", f"{prefix}_summary": "Answer",
                       f"{prefix}_top_urls": "https://example.com/correct",
                       f"{prefix}_raw": json.dumps({
                           "url": "https://example.com/correct",
                           "results": [{"url": url} for url in urls],
                           "citations": ["https://example.com/correct"],
                       })}
                scored = runner.score_output_row(contract, row)
                with self.subTest(prefix=prefix, rank=rank):
                    self.assertAlmostEqual(scored[f"{prefix}_ranking_quality_score"], expected_score, places=2)

    def test_ranking_requires_an_explicit_judgment_and_is_not_a_default_weight(self):
        self.assertNotIn("ranking_quality", runner._dimension_weights({}))
        with self.assertRaises(ValueError):
            runner._dimension_weights({"primary_dimensions": "ranking_quality"})

    def test_shipped_matrix_is_falsifiable_without_local_task_files(self):
        rows = runner.read_rows(runner.DEFAULT_MATRIX)
        self.assertEqual(len(rows), 48)
        self.assertEqual(len({row["benchmark_id"] for row in rows}), 48)
        for row in rows:
            with self.subTest(benchmark_id=row["benchmark_id"]):
                self.assertTrue(audit.audit_row(row)["falsifiable"])
                weights = runner._dimension_weights(row)
                if row["expected_content_patterns"] or row["preferred_tool"] == "map_site":
                    self.assertIn("assertion_pass_rate", weights)
                if "ranking_quality" in weights:
                    self.assertEqual(row["preferred_tool"], "search")
                    self.assertTrue(row["expected_url_patterns"])

    def test_raw_survives_csv_reload_and_mysearch_only_refresh(self):
        contract = {"benchmark_id": "reload", "domain": "test", "query": "q",
                    "prompt_variant": "baseline", "preferred_tool": "search",
                    "primary_dimensions": "ranking_quality|assertion_pass_rate",
                    "expected_url_patterns": "/correct", "expected_content_patterns": "source fact"}
        raw = json.dumps({"content": "source fact", "results": [{"url": "https://example.com/correct"}]})
        item = {"benchmark_id": "reload", "run_status": "captured"}
        for prefix in ("mysearch", "tavily"):
            item.update({f"{prefix}_summary": "source fact", f"{prefix}_raw": raw,
                         f"{prefix}_top_urls": "https://example.com/correct"})
        with tempfile.TemporaryDirectory() as directory:
            raw_dir = Path(directory)
            output = raw_dir / "output.csv"
            original = runner.build_output_row(contract, item, raw_dir)
            runner.write_output(output, [original])
            order, existing = runner.load_existing_rows(output)
            rescored = runner.merge_output_rows(
                [contract], [], [], raw_dir, existing_order=order,
                existing_rows=existing, preserve_tavily=False,
            )[0]
            self.assertEqual(rescored["mysearch_raw"], raw)
            self.assertEqual(rescored["tavily_ranking_quality_score"], 5.0)
            self.assertEqual(rescored["tavily_assertion_pass_rate"], 1.0)
            refreshed = runner.build_output_row(
                contract, {key: value for key, value in item.items() if not key.startswith("tavily_")},
                raw_dir, existing=existing["reload"], preserve_tavily=True,
            )
            self.assertEqual(refreshed["tavily_raw"], raw)
            self.assertEqual(refreshed["tavily_ranking_quality_score"], 5.0)
            self.assertEqual(refreshed["tavily_assertion_pass_rate"], 1.0)

    def test_missing_referenced_raw_is_an_explicit_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output.csv"
            runner.write_output(path, [{"benchmark_id": "missing",
                                      "notes": f"mysearch_raw={directory}/missing.mysearch.json"}])
            with self.assertRaises(FileNotFoundError):
                runner.load_existing_rows(path)


def _check_median_sample(tmp_path, tool, fail_first):
    namespace = {}
    exec(runner.REMOTE_SCRIPT.split("\npayload = json.loads", 1)[0], namespace)
    samples = [
        {
            "summary": f"Sample {number}",
            "results": [{"url": f"https://example.com/{number}", "content": "x" * size}],
        }
        for number, size in [(1, 100), (2, 300), (3, 200)]
    ]
    responses = [
        {"result": {"content": [{"type": "text", "text": json.dumps(sample)}]}}
        for sample in samples
    ]
    if fail_first:
        responses.insert(0, RuntimeError("temporary upstream failure"))
    observed = namespace["timed_tool_runs"](
        Mock(call_tool=Mock(side_effect=responses)), tool, {}, len(responses)
    )
    assert observed["summary"] == "Sample 3"
    assert observed["content_char_count"] == 200
    assert observed["urls"] == ["https://example.com/3"]
    assert json.loads(observed["raw_text"]) == samples[2]
    assert observed["partial_error"] is fail_first

    prefix = "tavily" if tool.startswith("tavily") else "mysearch"
    input_row = {"benchmark_id": "median", "domain": "test", "query": "q", "prompt_variant": "baseline"}
    row = runner.build_output_row(
        input_row, {f"{prefix}_raw": observed["raw_text"]}, tmp_path
    )
    assert json.loads(row[f"{prefix}_raw"]) == samples[2]
    artifact = row["notes"].split(f"{prefix}_raw=", 1)[1]
    assert json.loads((tmp_path / artifact.split("/")[-1]).read_text()) == samples[2]
