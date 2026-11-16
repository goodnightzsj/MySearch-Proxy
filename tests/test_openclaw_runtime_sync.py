from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("openclaw_runtime", REPO_ROOT / "scripts/openclaw_runtime.py")
assert spec is not None and spec.loader is not None
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class OpenClawRuntimeTests(unittest.TestCase):
    def write_source(self, source: Path, files: dict[str, str]) -> None:
        for name, text in files.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def test_nested_absolute_relative_and_package_imports(self) -> None:
        files = {
            "__init__.py": "from . import version\n",
            "version.py": "VALUE = 1\n",
            "clients.py": "from mysearch.research import nested\nimport mysearch.providers.base\n",
            "research/__init__.py": "from . import exported\n",
            "research/exported.py": "from ..version import VALUE\n",
            "research/nested/__init__.py": "from . import leaf\n",
            "research/nested/leaf.py": "from ...providers import base\n",
            "providers/__init__.py": "",
            "providers/base.py": "from ..version import VALUE\n",
        }
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source/mysearch"
            destination = Path(directory) / "bundle/mysearch"
            self.write_source(source, {**files, "server.py": "raise RuntimeError('host only')\n"})
            self.assertEqual(set(runtime.runtime_files(source)), {Path(name) for name in files})
            with contextlib.redirect_stdout(io.StringIO()):
                runtime.sync_runtime(source, destination)
                self.assertTrue(runtime.check_runtime(source, destination))
            self.assertTrue((destination / "research/nested/leaf.py").is_file())
            self.assertFalse((destination / "server.py").exists())

    def test_check_rejects_missing_new_dependency_stale_files_and_drift(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source/mysearch"
            destination = Path(directory) / "bundle/mysearch"
            self.write_source(source, {"__init__.py": "", "clients.py": ""})
            shutil.copytree(source, destination)
            self.write_source(source, {"clients.py": "from . import required\n", "required.py": "VALUE = 1\n"})
            self.write_source(destination, {"obsolete.py": ""})
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertFalse(runtime.check_runtime(source, destination))
            self.assertIn("MISSING: required.py", output.getvalue())
            self.assertIn("STALE: obsolete.py", output.getvalue())
            self.assertIn("DESYNC: clients.py", output.getvalue())

    def test_failed_import_preserves_old_bundle_and_ignores_pythonpath(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source/mysearch"
            destination = Path(directory) / "bundle/mysearch"
            self.write_source(source, {"__init__.py": "", "clients.py": "import external_source_only\n"})
            self.write_source(source.parent, {"external_source_only.py": "VALUE = 1\n"})
            self.write_source(destination, {"clients.py": "# previous runtime\n"})
            with patch.dict(os.environ, {"PYTHONPATH": str(source.parent)}):
                result = subprocess.run(
                    [sys.executable, str(REPO_ROOT / "scripts/openclaw_runtime.py"), "sync", str(source), str(destination)],
                    cwd=source.parent, text=True, capture_output=True,
                )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("No module named 'external_source_only'", result.stderr)
            self.assertEqual((destination / "clients.py").read_text(), "# previous runtime\n")
            self.assertEqual(list(destination.parent.iterdir()), [destination])

    def test_missing_absolute_dependency_fails_before_replacing_bundle(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source/mysearch"
            destination = Path(directory) / "bundle/mysearch"
            self.write_source(source, {"__init__.py": "", "clients.py": "import mysearch.missing\n"})
            self.write_source(destination, {"clients.py": "# previous runtime\n"})
            with self.assertRaisesRegex(ValueError, "Missing runtime module: mysearch.missing"):
                runtime.sync_runtime(source, destination)
            self.assertEqual((destination / "clients.py").read_text(), "# previous runtime\n")

    def test_failed_replacement_restores_previous_bundle(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source/mysearch"
            destination = Path(directory) / "bundle/mysearch"
            self.write_source(source, {"__init__.py": "", "clients.py": ""})
            self.write_source(destination, {"clients.py": "# previous runtime\n"})
            rename = Path.rename

            def fail_staged_rename(path: Path, target: Path) -> Path:
                if path.parent.name.startswith(".mysearch-build-"):
                    raise OSError("simulated replacement failure")
                return rename(path, target)

            with patch.object(Path, "rename", fail_staged_rename):
                with self.assertRaisesRegex(OSError, "simulated replacement failure"):
                    runtime.sync_runtime(source, destination)
            self.assertEqual((destination / "clients.py").read_text(), "# previous runtime\n")
            self.assertEqual(list(destination.parent.iterdir()), [destination])

    def test_sync_only_shell_uses_shared_builder_without_publishing(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "mysearch"
            self.write_source(source, {
                "__init__.py": '__version__ = "0.1.0"\n',
                "clients.py": "from .providers import base\n",
                "providers/__init__.py": "",
                "providers/base.py": "VALUE = 1\n",
            })
            self.write_source(root, {"openclaw/scripts/mysearch_openclaw.py": (
                "import sys\nfrom pathlib import Path\n"
                "assert sys.argv[1:] == ['health']\n"
                "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))\n"
                "from mysearch.providers.base import VALUE\nassert VALUE == 1\n"
            )})
            (root / "scripts").mkdir()
            for name in ("release_openclaw_skill.sh", "check_sync.sh", "openclaw_runtime.py"):
                shutil.copyfile(REPO_ROOT / "scripts" / name, root / "scripts" / name)
            # The release script's python3 must use the same interpreter as the test runner.
            (root / "bin").mkdir()
            (root / "bin/python3").symlink_to(sys.executable)
            env = {**os.environ, "PATH": str(root / "bin") + os.pathsep + os.environ.get("PATH", "")}
            result = subprocess.run(
                ["bash", str(root / "scripts/release_openclaw_skill.sh"), "--sync-only"],
                cwd=root, env=env, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Smoke test passed.", result.stdout)
            self.assertIn("Skipping ClawHub publish.", result.stdout)
            check = subprocess.run(
                ["bash", str(root / "scripts/check_sync.sh")],
                cwd=root, env=env, text=True, capture_output=True,
            )
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_real_runtime_build_imports_without_repository_path(self) -> None:
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "runtime/mysearch"
            with contextlib.redirect_stdout(io.StringIO()):
                runtime.sync_runtime(REPO_ROOT / "mysearch", destination)
                self.assertTrue(runtime.check_runtime(REPO_ROOT / "mysearch", destination))
            self.assertTrue((destination / "research/__init__.py").is_file())
            self.assertTrue((destination / "providers/firecrawl_crawl.py").is_file())
            self.assertFalse((destination / "server.py").exists())


if __name__ == "__main__":
    unittest.main()
