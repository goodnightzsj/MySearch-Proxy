#!/usr/bin/env bash
# Verify mysearch/ runtime files are in sync with openclaw/runtime/mysearch/
set -euo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$BASE/mysearch"
DST="$BASE/openclaw/runtime/mysearch"

# 以 bundle 实际发布的 .py 文件为准自动发现，新增 runtime 文件无需再手工登记。
FILES=()
for path in "$DST"/*.py; do
    [[ -f "$path" ]] || continue
    FILES+=("$(basename "$path")")
done
EXIT=0

for f in "${FILES[@]}"; do
    if [[ ! -f "$SRC/$f" ]]; then
        echo "STALE: $DST/$f 没有对应的 $SRC/$f（bundle 独有文件）"
        EXIT=1
        continue
    fi
    if ! diff -q "$SRC/$f" "$DST/$f" >/dev/null 2>&1; then
        echo "DESYNC: $f"
        EXIT=1
    fi
done

if [[ $EXIT -eq 0 ]]; then
    echo "OK: all runtime files in sync (${#FILES[@]} files)"
else
    echo ""
    echo "Fix: cp mysearch/<file>.py openclaw/runtime/mysearch/"
    exit 1
fi
