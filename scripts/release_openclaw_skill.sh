#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MYSEARCH_DIR="$ROOT_DIR/mysearch"
OPENCLAW_DIR="$ROOT_DIR/openclaw"
RUNTIME_DIR="$OPENCLAW_DIR/runtime/mysearch"
SKILL_SLUG="mysearch"
SKILL_NAME="MySearch"
TAGS="latest,search,web,docs,social,x"
VERSION=""
CHANGELOG=""
SYNC_ONLY=0

usage() {
  cat <<'EOF'
Usage: scripts/release_openclaw_skill.sh [options]

Sync the current MySearch runtime into openclaw/runtime/mysearch,
run a local smoke check, and optionally publish the OpenClaw skill to ClawHub.

Options:
  --version VERSION     Skill version to publish. Defaults to mysearch/__init__.py
  --changelog TEXT      Release changelog. Required unless --sync-only is used
  --tags CSV            Publish tags (default: latest,search,web,docs,social,x)
  --sync-only           Only sync runtime + smoke test; do not publish
  -h, --help            Show this help

Examples:
  bash scripts/release_openclaw_skill.sh --sync-only
  bash scripts/release_openclaw_skill.sh --version 0.1.2 --changelog "Bundle refreshed runtime and docs"
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      VERSION="${2:?missing version}"
      shift 2
      ;;
    --changelog)
      CHANGELOG="${2:?missing changelog}"
      shift 2
      ;;
    --tags)
      TAGS="${2:?missing tags}"
      shift 2
      ;;
    --sync-only)
      SYNC_ONLY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

resolve_version() {
  if [[ -n "$VERSION" ]]; then
    return
  fi

  VERSION="$(python3 - <<'PY' "$MYSEARCH_DIR/__init__.py"
from pathlib import Path
import re
import sys

text = Path(sys.argv[1]).read_text(encoding="utf-8")
match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
if not match:
    raise SystemExit("Unable to find __version__ in mysearch/__init__.py")
print(match.group(1))
PY
)"
}

sync_runtime() {
  echo "Syncing MySearch runtime into OpenClaw bundle..."
  rm -rf "$RUNTIME_DIR" "$OPENCLAW_DIR/scripts/__pycache__" "$OPENCLAW_DIR/runtime/__pycache__"
  mkdir -p "$RUNTIME_DIR"

  local files=(__init__.py clients.py config.py keyring.py)
  for file in "${files[@]}"; do
    install -m 0644 "$MYSEARCH_DIR/$file" "$RUNTIME_DIR/$file"
    echo "  - $file"
  done
}

run_smoke() {
  echo "Running OpenClaw runtime smoke test..."
  python3 -m py_compile \
    "$OPENCLAW_DIR/scripts/mysearch_openclaw.py" \
    "$RUNTIME_DIR/__init__.py" \
    "$RUNTIME_DIR/clients.py" \
    "$RUNTIME_DIR/config.py" \
    "$RUNTIME_DIR/keyring.py"

  python3 "$OPENCLAW_DIR/scripts/mysearch_openclaw.py" health >/dev/null
  echo "Smoke test passed."
}

publish_skill() {
  if [[ -z "$CHANGELOG" ]]; then
    echo "--changelog is required when publishing." >&2
    exit 2
  fi

  echo "Publishing $SKILL_NAME@$VERSION to ClawHub..."
  clawhub publish \
    "$OPENCLAW_DIR" \
    --slug "$SKILL_SLUG" \
    --name "$SKILL_NAME" \
    --version "$VERSION" \
    --tags "$TAGS" \
    --changelog "$CHANGELOG"
}

inspect_release() {
  local tmp_json=""
  tmp_json="$(mktemp)"
  trap 'rm -f "$tmp_json"' EXIT

  echo "Waiting for ClawHub inspect to return the new version..."
  local attempt
  for attempt in {1..12}; do
    if clawhub inspect "$SKILL_SLUG" --version "$VERSION" --json >"$tmp_json" 2>/dev/null; then
      break
    fi
    sleep 2
  done

  if [[ ! -s "$tmp_json" ]]; then
    echo "Publish succeeded, but inspect did not return version $VERSION yet." >&2
    return 1
  fi

  python3 - <<'PY' "$tmp_json"
from pathlib import Path
import json
import sys

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
version = data.get("version", {}) or {}
security = version.get("security", {}) or {}
llm = ((security.get("scanners") or {}).get("llm") or {})

print("ClawHub inspect summary:")
print(f"  version: {version.get('version', '')}")
print(f"  security.status: {security.get('status', '')}")
print(f"  llm.verdict: {llm.get('verdict', '')}")
print(f"  llm.confidence: {llm.get('confidence', '')}")
PY
}

resolve_version
sync_runtime
run_smoke

if [[ "$SYNC_ONLY" == "1" ]]; then
  echo "Runtime sync complete. Skipping ClawHub publish."
  exit 0
fi

publish_skill
inspect_release
