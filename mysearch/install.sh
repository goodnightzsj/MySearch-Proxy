#!/usr/bin/env bash
set -euo pipefail

MYSEARCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$MYSEARCH_DIR/.." && pwd)"

if [[ -f "$MYSEARCH_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$MYSEARCH_DIR/.env"
  set +a
fi

exec "$ROOT_DIR/install.sh"
