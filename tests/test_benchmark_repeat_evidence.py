from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import run_remote_mcp_benchmark as runner


def helpers():
    namespace = {}
    exec(runner.REMOTE_SCRIPT.split("\npayload = json.loads", 1)[0], namespace)
    return namespace


def response(number, size):
    body = {"provider": "tavily", "summary": f"Sample {number}",
            "results": [{"url": f"https://example.test/{number}", "content": "x" * size}]}
    return {"result": {"content": [{"type": "text", "text": json.dumps(body)}]}}


class BenchmarkRepeatEvidenceTests(unittest.TestCase):
    def test_reload_rejects_missing_or_changed_repeat_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = {"benchmark_id": "repeat", "mysearch_repeat_observations": json.dumps([
                {"run": 1, "raw_payload": response(1, 100), "scored_sample": True},
            ])}
            runner.write_repeat_raw(row, root, "repeat", "mysearch")
            csv_path = root / "output.csv"
            runner.write_output(csv_path, [row])
            runner.load_existing_rows(csv_path)
            path = Path(json.loads(row["mysearch_repeat_observations"])[0]["raw_path"])
            path.write_text("{}")
            with self.assertRaisesRegex(ValueError, "Repeat evidence hash mismatch"):
                runner.load_existing_rows(csv_path)
            path.unlink()
            with self.assertRaises(FileNotFoundError):
                runner.load_existing_rows(csv_path)

    def test_diagnostics_at_any_depth_do_not_change_scores(self):
        namespace = helpers()
        base = {"provider": "tavily", "summary": "Answer",
                "results": [{"url": "https://example.test/a", "content": "text"}],
                "evidence": {"primary": {"provider": "tavily"}}}
        diagnostic = {"provider": "exa", "selected": "tavily+exa", "date": "2026-11-17",
                      "fallback": {"triggered": True, "used": True, "reason": "diagnostic only"}}
        decorated = copy.deepcopy(base)
        decorated["diagnostics"] = diagnostic
        decorated["evidence"]["primary"]["diagnostics"] = [diagnostic]
        decorated["results"][0]["diagnostics"] = diagnostic
        for prefix in ("mysearch", "tavily"):
            scores = []
            for body in (base, decorated):
                summary = namespace["summarize"](body)
                row = {f"{prefix}_{key}": value for key, value in summary.items()}
                row[f"{prefix}_top_urls"] = " | ".join(summary["urls"])
                row[f"{prefix}_raw"] = json.dumps(body)
                scores.append(runner._score_provider({}, row, prefix))
                self.assertFalse(summary["orchestration_used"])
                self.assertFalse(summary["fallback_attempted"])
                self.assertEqual(summary["published_date_count"], 0)
            self.assertEqual(scores[0], scores[1])
        decorated["evidence"]["secondary"] = {"provider": "exa"}
        self.assertTrue(namespace["orchestration_used"](decorated))
        self.assertEqual(runner._trace_provider_names(decorated), {"tavily", "exa"})

    def test_repeat_raw_preserves_real_run_and_median_after_failure(self):
        namespace = helpers()
        responses = [RuntimeError("temporary failure"), response(1, 100),
                     response(2, 300), response(3, 200)]
        observed = namespace["timed_tool_runs"](
            Mock(call_tool=Mock(side_effect=responses)), "search", {}, 4)
        observations = json.loads(observed["repeat_observations"])
        self.assertEqual([item["run"] for item in observations], [1, 2, 3, 4])
        self.assertEqual([item["scored_sample"] for item in observations], [False, False, False, True])
        self.assertEqual(observations[0]["raw_payload"]["phase"], "tools/call")
        self.assertEqual(observations[3]["raw_payload"], responses[3])
        self.assertEqual(json.loads(observed["raw_text"])["summary"], "Sample 3")

    def test_all_failure_retains_every_tool_error_and_exception(self):
        namespace = helpers()
        tool_error = {"result": {"content": [{"type": "text", "text": "Error executing tool: unavailable"}]}}
        with self.assertRaises(namespace["ToolRunsFailed"]) as caught:
            namespace["timed_tool_runs"](
                Mock(call_tool=Mock(side_effect=[RuntimeError("timed out"), tool_error])), "search", {}, 2)
        exc = caught.exception
        self.assertTrue(exc.timeout)
        self.assertEqual(len(exc.observations), 2)
        self.assertTrue(all(not item["success"] and not item["scored_sample"] for item in exc.observations))
        self.assertEqual(exc.observations[1]["raw_payload"], tool_error)

    def test_remote_redacts_repeats_before_local_persistence(self):
        secret = 'synthetic-quoted"secret'
        payload = {"tavily_bearer": secret, "mysearch_url": "http://mysearch.test",
                   "tavily_url": "http://tavily.test", "cases": [{
                       "benchmark_id": "repeat", "mysearch_tool": "search", "mysearch_args": {},
                       "tavily_tool": "tavily_search", "tavily_args": {}, "repeat_runs": 3}]}
        helper_source, execution = runner.REMOTE_SCRIPT.split("\npayload = json.loads", 1)
        for all_failed in (False, True):
            namespace = {"PAYLOAD_B64": base64.b64encode(json.dumps(payload).encode())}
            exec(helper_source, namespace)
            class FakeClient:
                def __init__(self, *args):
                    self.count = 0
                def initialize(self):
                    pass
                def call_tool(self, *args):
                    self.count += 1
                    if all_failed or self.count == 1:
                        raise RuntimeError("upstream echoed " + secret)
                    body = response(self.count, self.count * 100)
                    body["metadata"] = {"echo": secret}
                    return body
            namespace["MCPClient"] = FakeClient
            output = io.StringIO()
            with redirect_stdout(output):
                exec("payload = json.loads" + execution, namespace)
            item = json.loads(output.getvalue())[0]
            self.assertNotIn(secret, json.dumps(item))
            contract = {"benchmark_id": "repeat", "domain": "test", "query": "q", "prompt_variant": "baseline"}
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                actual_write = runner.write_raw
                def checked_write(*args):
                    self.assertNotIn(secret, args[-1])
                    self.assertNotIn(json.dumps(secret)[1:-1], args[-1])
                    if args[0].name == "repeats" or all_failed:
                        self.assertIn("[REDACTED]", args[-1])
                    return actual_write(*args)
                # All repeat responses and selected errors are redacted before write_raw.
                with patch.object(runner, "write_raw", side_effect=checked_write):
                    row = runner.build_output_row(contract, item, root)
                self.assertEqual(len(runner.FIELDNAMES), 100)
                self.assertEqual(len(list(root.glob("*.json"))), 2)
                self.assertEqual(len(list((root / "repeats").glob("*.json"))), 6)
                for prefix in ("mysearch", "tavily"):
                    observations = json.loads(row[f"{prefix}_repeat_observations"])
                    self.assertEqual([entry["run"] for entry in observations], [1, 2, 3])
                    self.assertEqual(sum(entry["scored_sample"] for entry in observations), 0 if all_failed else 1)
                    for entry in observations:
                        self.assertNotIn("raw_payload", entry)
                        data = Path(entry["raw_path"]).read_bytes()
                        self.assertEqual(hashlib.sha256(data).hexdigest(), entry["raw_sha256"])
                        self.assertNotIn(secret.encode(), data)
                        self.assertNotIn(json.dumps(secret)[1:-1].encode(), data)
                if all_failed:
                    self.assertEqual(row["run_status"], "error")
                    self.assertEqual(row["mysearch_total_score"], 0)
                    self.assertEqual(row["tavily_total_score"], 0)


if __name__ == "__main__":
    unittest.main()
