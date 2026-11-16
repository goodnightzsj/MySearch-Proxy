from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ContainerPackagingTests(unittest.TestCase):
    def test_copied_sources_import_in_isolation_without_io(self):
        targets = (
            ("proxy/Dockerfile", ROOT, True),
            ("mysearch/Dockerfile", ROOT / "mysearch", False),
            ("Dockerfile.stack", ROOT, False),
        )
        for dockerfile, context, proxy_only in targets:
            with self.subTest(dockerfile=dockerfile), tempfile.TemporaryDirectory() as temp:
                stage = Path(temp)
                text = (ROOT / dockerfile).read_text()
                workdir = "/"
                for line in text.splitlines():
                    if line.startswith("WORKDIR "):
                        workdir = line.split()[1]
                    if not line.startswith("COPY "):
                        continue
                    _, source, destination = shlex.split(line)
                    source = context / source
                    destination = stage / (Path(workdir) / destination).relative_to("/")
                    if not source.is_dir():
                        continue
                    # Only runtime source/assets are needed; never read local credentials or data.
                    for item in source.rglob("*"):
                        if item.is_file() and item.suffix in {".py", ".css", ".js", ".html"}:
                            copied = destination / item.relative_to(source)
                            copied.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(item, copied)
                current = stage / workdir.lstrip("/")
                current.mkdir(parents=True, exist_ok=True)
                python_path = re.search(r"\bPYTHONPATH=(\S+)", text)
                paths = [str(current)]
                if python_path:
                    paths.extend(str(stage / item.lstrip("/")) for item in python_path[1].split(":"))
                smoke = [shlex.split(line.removeprefix("RUN --network=none ")) for line in text.splitlines()
                         if line.startswith("RUN --network=none ")]
                self.assertEqual(len(smoke), 1, "Each image must fail its build when runtime imports fail")
                self.assertEqual(smoke[0][:2], ["python", "-c"])
                code = smoke[0][2].replace("/app", str(stage / "app"))
                script = f"""
import importlib.abc
from pathlib import Path
import sys
sys.path[:0] = {paths!r}
def audit(event, arguments):
    if event in {{'socket.connect', 'socket.getaddrinfo', 'sqlite3.connect'}}:
        raise AssertionError('Import performed I/O: ' + event)
    if event == 'open' and isinstance(arguments[0], str):
        name = Path(arguments[0]).name
        if name.startswith('.env') or name in {{'config.toml', 'accounts.txt'}}:
            raise AssertionError('Import read external configuration')
sys.addaudithook(audit)
class NoMcp(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'mcp' or fullname.startswith('mcp.'):
            raise ImportError('Standalone Proxy has no MCP dependency')
if {proxy_only!r}:
    sys.meta_path.insert(0, NoMcp())
exec({code!r})
import mysearch
assert Path(mysearch.__file__).is_relative_to({str(stage)!r})
"""
                result = subprocess.run(
                    [sys.executable, "-I", "-B", "-c", script], cwd=current,
                    env={"PATH": os.defpath, "ADMIN_PASSWORD": "container-import-smoke-only",
                         "MYSEARCH_PROXY_DB_PATH": str(stage / "must-not-exist.db")},
                    capture_output=True, text=True, timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((stage / "must-not-exist.db").exists())

    def test_proxy_build_callers_use_repository_context(self):
        root_compose = (ROOT / "docker-compose.yml").read_text()
        proxy_compose = (ROOT / "proxy/docker-compose.yml").read_text()
        workflow = (ROOT / ".github/workflows/docker-publish.yml").read_text()
        self.assertRegex(root_compose, r"proxy:\s+build:\s+context: \.\s+dockerfile: proxy/Dockerfile")
        self.assertRegex(proxy_compose, r"build:\s+context: \.\.\s+dockerfile: proxy/Dockerfile")
        self.assertRegex(workflow, r"target: proxy\s+target_label: proxy\s+context: \.\s+dockerfile: ./proxy/Dockerfile")

    def test_root_context_excludes_local_data_and_credentials(self):
        ignored = set((ROOT / ".dockerignore").read_text().splitlines())
        for pattern in ("proxy/data", "**/.env", "**/.env.*", "**/accounts.txt",
                        "**/*.db", "**/*.db-*", "**/*.sqlite", "**/*.sqlite3"):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, ignored)


if __name__ == "__main__":
    unittest.main()
