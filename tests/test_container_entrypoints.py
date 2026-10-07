from __future__ import annotations

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINTS = (ROOT / "docker/combined-entrypoint.sh", ROOT / "mysearch/docker-entrypoint.sh")


class ContainerEntrypointTests(unittest.TestCase):
    def run_entrypoint(self, entrypoint, *, bootstrap_exit=0, token="test-token", explicit=""):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            fake_python = directory / "python"
            fake_python.write_text(
                "#!/usr/bin/env bash\n"
                "case \"$*\" in\n"
                "  *bootstrap_proxy_token.py)\n"
                "    printf '%s' \"$TEST_BOOTSTRAP_TOKEN\"\n"
                "    exit \"$TEST_BOOTSTRAP_EXIT\" ;;\n"
                "  '-m uvicorn '*) exec sleep 30 ;;\n"
                "  *) printf '%s' \"${MYSEARCH_PROXY_API_KEY:-}\" > \"$TEST_MCP_STARTED\" ;;\n"
                "esac\n"
            )
            fake_python.chmod(0o755)
            marker = directory / "started"
            env = {key: value for key, value in os.environ.items() if not key.startswith("MYSEARCH_")}
            env.update(
                PATH=f"{directory}{os.pathsep}{os.environ['PATH']}",
                MYSEARCH_PROXY_BOOTSTRAP_TOKEN="test-bootstrap-only",
                MYSEARCH_PROXY_API_KEY=explicit,
                TEST_BOOTSTRAP_TOKEN=token,
                TEST_BOOTSTRAP_EXIT=str(bootstrap_exit),
                TEST_MCP_STARTED=str(marker),
            )
            result = subprocess.run(
                ["bash", str(entrypoint), "python", "-m", "mysearch"],
                env=env, capture_output=True, text=True, timeout=5,
            )
            return result, marker.read_text() if marker.exists() else None

    def test_failed_bootstrap_never_starts_mcp(self):
        for entrypoint in ENTRYPOINTS:
            with self.subTest(entrypoint=entrypoint.name):
                result, started = self.run_entrypoint(entrypoint, bootstrap_exit=7, token="")
                self.assertEqual(result.returncode, 7, result.stderr)
                self.assertIsNone(started)

    def test_empty_bootstrap_never_starts_mcp(self):
        for entrypoint in ENTRYPOINTS:
            with self.subTest(entrypoint=entrypoint.name):
                result, started = self.run_entrypoint(entrypoint, token="")
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(started)
                self.assertIn("empty", result.stderr.lower())

    def test_success_exports_token_without_logging_it(self):
        for entrypoint in ENTRYPOINTS:
            with self.subTest(entrypoint=entrypoint.name):
                result, started = self.run_entrypoint(entrypoint)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(started, "test-token")
                self.assertNotIn("test-token", result.stdout + result.stderr)

    def test_explicit_token_skips_bootstrap(self):
        for entrypoint in ENTRYPOINTS:
            with self.subTest(entrypoint=entrypoint.name):
                result, started = self.run_entrypoint(entrypoint, bootstrap_exit=7, explicit="test-explicit")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(started, "test-explicit")


class BootstrapPollingTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "bootstrap_polling_test", ROOT / "mysearch/scripts/bootstrap_proxy_token.py"
        )
        self.bootstrap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bootstrap)

    def run_bootstrap(self, times, responses, **extra_env):
        env = {
            "MYSEARCH_PROXY_BASE_URL": "http://proxy:9874",
            "MYSEARCH_PROXY_BOOTSTRAP_TOKEN": "test-bootstrap-only",
            **extra_env,
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env, clear=True), \
             patch.object(self.bootstrap.time, "time", side_effect=times), \
             patch.object(self.bootstrap.time, "sleep"), \
             patch.object(self.bootstrap.urllib.request, "urlopen", side_effect=responses) as request, \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.bootstrap.main()
        return code, stdout.getvalue(), stderr.getvalue(), request.call_count

    def test_default_budget_allows_proxy_start_after_sixty_seconds(self):
        response = contextlib.nullcontext(io.BytesIO(b'{"token":"test-token"}'))
        code, output, _, calls = self.run_bootstrap(
            [0, 0, 90], [URLError("Connection refused"), response]
        )
        self.assertEqual(code, 0)
        self.assertEqual(output.strip(), "test-token")
        self.assertEqual(calls, 2)

    def test_configured_timeout_is_not_ignored(self):
        code, output, error, calls = self.run_bootstrap(
            [0, 0, 3], [URLError("Connection refused")], MYSEARCH_PROXY_BOOTSTRAP_TIMEOUT_SECONDS="2"
        )
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("Connection refused", error)
        self.assertEqual(calls, 1)

    def test_empty_response_fails_without_outputting_a_token(self):
        response = contextlib.nullcontext(io.BytesIO(b'{"token":""}'))
        code, output, error, _ = self.run_bootstrap(
            [0, 0, 3], [response], MYSEARCH_PROXY_BOOTSTRAP_TIMEOUT_SECONDS="2"
        )
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("empty token", error)
