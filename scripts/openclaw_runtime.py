"""Build and verify the OpenClaw client's transitive Python runtime."""

from __future__ import annotations

import argparse
import ast
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


def runtime_files(source: Path) -> list[Path]:
    pending: list[Path] = []
    seen: set[Path] = set()

    def add_module(module: str, *, required: bool = True) -> None:
        if module != "mysearch" and not module.startswith("mysearch."):
            return
        path = source.joinpath(*module.split(".")[1:])
        package = path / "__init__.py"
        file = path.with_suffix(".py")
        if package.is_file():
            pending.append(package.relative_to(source))
        elif module != "mysearch" and file.is_file():
            pending.append(file.relative_to(source))
        elif not path.is_dir():
            if required:
                raise ValueError(f"Missing runtime module: {module}")
            return
        for parent in path.parents:
            if parent == source.parent:
                break
            init = parent / "__init__.py"
            if init.is_file():
                pending.append(init.relative_to(source))

    add_module("mysearch.clients")
    while pending:
        relative = pending.pop()
        if relative in seen:
            continue
        seen.add(relative)
        tree = ast.parse((source / relative).read_text(encoding="utf-8"), filename=str(relative))
        package = ".".join(("mysearch", *relative.parent.parts))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    add_module(alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    module = importlib.util.resolve_name("." * node.level + module, package)
                add_module(module)
                for alias in node.names:
                    # An imported name can be a submodule or an attribute.
                    add_module(f"{module}.{alias.name}", required=False)
    return sorted(seen)


def check_runtime(source: Path, destination: Path) -> bool:
    expected = set(runtime_files(source))
    actual = {path.relative_to(destination) for path in destination.rglob("*.py")}
    failures = [f"MISSING: {path}" for path in sorted(expected - actual)]
    failures.extend(f"STALE: {path}" for path in sorted(actual - expected))
    failures.extend(
        f"DESYNC: {path}"
        for path in sorted(expected & actual)
        if (source / path).read_bytes() != (destination / path).read_bytes()
    )
    if failures:
        print("\n".join(failures))
        print("Fix: bash scripts/release_openclaw_skill.sh --sync-only")
        return False
    print(f"OK: all runtime files in sync ({len(expected)} files)")
    return True


def validate_runtime(destination: Path, files: list[Path]) -> None:
    modules = [
        ".".join(("mysearch", *path.with_suffix("").parts)).removesuffix(".__init__")
        for path in files
    ]
    # -I excludes PYTHONPATH/cwd; checking every loaded module also detects installed fallbacks.
    subprocess.run(
        [
            sys.executable, "-I", "-B", "-c",
            "import importlib, pathlib, sys; "
            "root = pathlib.Path(sys.argv[1]).resolve(); "
            "sys.path.insert(0, str(root.parent)); "
            "[importlib.import_module(name) for name in sys.argv[2:]]; "
            "assert all(pathlib.Path(location).resolve().is_relative_to(root) "
            "for name, module in sys.modules.items() "
            "if name == 'mysearch' or name.startswith('mysearch.') "
            "for location in ([module.__file__] if module.__file__ else module.__path__)), "
            "'Runtime escaped bundle'",
            str(destination), *modules,
        ],
        cwd=destination.parent,
        check=True,
    )


def sync_runtime(source: Path, destination: Path) -> None:
    files = runtime_files(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".mysearch-build-", dir=destination.parent) as directory:
        staged = Path(directory) / "mysearch"
        for relative in files:
            target = staged / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / relative, target)
            target.chmod(0o644)
        validate_runtime(staged, files)
        backup = destination.with_name(f".{destination.name}.previous-{uuid.uuid4().hex}")
        if destination.exists():
            destination.rename(backup)
        try:
            staged.rename(destination)
        except OSError:
            if backup.exists():
                backup.rename(destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    for relative in files:
        print(f"  - {relative}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("sync", "check"))
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    source, destination = args.source.resolve(), args.destination.resolve()
    if args.command == "check":
        return 0 if check_runtime(source, destination) else 1
    sync_runtime(source, destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
