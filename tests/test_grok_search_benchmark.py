"""Benchmark failures remain visible and model-written metadata is not ground truth."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("grok_benchmark", ROOT / "scripts/benchmark_grok_search.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def sample(url="https://x.com/AnthropicAI/status/1970558198109126942", **fields):
    return {"type": "sample", "case": {"id": "test", "query": "release"},
            "model": "grok-4.5", "http_status": 200, "latency_ms": 100,
            "payload": {"output": [
                {"type": "custom_tool_call", "name": "x_keyword_search"},
                {"type": "message", "content": [{"type": "output_text",
                    "text": json.dumps({"results": [{"url": url, "text": "Release post", **fields}]}),
                    "annotations": [{"type": "url_citation", "url": url}]}]},
            ]}}


class BenchmarkTests(unittest.IsolatedAsyncioTestCase):
    def test_cli_stdout_is_jsonl_even_with_startup_warnings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.jsonl"
            path.write_text(json.dumps(sample()))
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/benchmark_grok_search.py"), "--replay", str(path)],
                capture_output=True, text=True, check=True, timeout=20,
                env={**os.environ, "ADMIN_PASSWORD": "admin", "MYSEARCH_PROXY_DB_PATH": str(Path(directory) / "test.db")})
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["probe_ok"])
        self.assertIn("security", result.stderr)

    async def test_bad_sample_does_not_hide_following_success(self):
        bad = {**sample(), "payload": {"output_text": '{"answer":42}'}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in (bad, sample())))
            output = io.StringIO()
            with patch("sys.stdout", output):
                await benchmark.main(SimpleNamespace(replay=path))
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertFalse(rows[0]["probe_ok"])
        self.assertEqual(rows[0]["probe_reason"], "invalid_response")
        self.assertTrue(rows[1]["probe_ok"])

    async def test_citation_author_overrides_conflicting_model_metadata(self):
        record = sample(handle="OpenAI")
        record["case"]["allowed_x_handles"] = ["OpenAI"]
        result = await benchmark.evaluate(record)
        self.assertIn("handle:0", result["constraint_violations"])
        self.assertNotEqual(result["constraint_status"], "pass")

    async def test_anonymous_citation_metadata_is_not_independent_verification(self):
        record = sample(url="https://x.com/i/status/1970558198109126942",
                        handle="OpenAI", created_at="2026-09-30T01:00:00Z")
        record["case"].update(allowed_x_handles=["OpenAI"], from_date="2026-09-01")
        result = await benchmark.evaluate(record)
        self.assertEqual(set(result["constraint_unknown"]), {"handle:0", "date:0"})
        self.assertEqual(result["constraint_status"], "unknown")


if __name__ == "__main__":
    unittest.main()
