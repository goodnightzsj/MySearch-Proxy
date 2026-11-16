#!/usr/bin/env bash
# Verify mysearch/ runtime files are in sync with openclaw/runtime/mysearch/
set -euo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$BASE/mysearch"
DST="$BASE/openclaw/runtime/mysearch"

python3 "$BASE/scripts/openclaw_runtime.py" check "$SRC" "$DST"
