#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
DEST_DIR="$CODEX_HOME/skills/mysearch"
FORCE="${1:-}"

if [[ -e "$DEST_DIR" && "$FORCE" != "--force" ]]; then
  echo "Skill already exists at $DEST_DIR"
  echo "Re-run with --force to replace it."
  exit 1
fi

if [[ -e "$DEST_DIR" ]]; then
  rm -rf "$DEST_DIR"
fi

mkdir -p "$(dirname "$DEST_DIR")"
cp -R "$SOURCE_DIR" "$DEST_DIR"

echo "Installed MySearch skill to $DEST_DIR"
echo "Restart Codex to pick up the new skill."
