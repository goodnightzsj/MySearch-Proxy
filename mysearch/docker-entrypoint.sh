#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${MYSEARCH_PROXY_API_KEY:-}" && -n "${MYSEARCH_PROXY_BOOTSTRAP_TOKEN:-}" ]]; then
  # Keep assignment separate from export so bootstrap failures reach set -e.
  MYSEARCH_PROXY_API_KEY="$(
    python /app/mysearch/scripts/bootstrap_proxy_token.py
  )"
  if [[ -z "${MYSEARCH_PROXY_API_KEY}" ]]; then
    echo "Proxy token bootstrap returned an empty token; refusing to start MCP." >&2
    exit 1
  fi
  export MYSEARCH_PROXY_API_KEY
fi

exec "$@"
