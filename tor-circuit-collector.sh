#!/usr/bin/env bash
set -euo pipefail

# tor-circuit-collector.sh
# Run tor-circuit-collector.py from any current working directory.
# Keep this .sh file in the same directory as tor-circuit-collector.py, or
# symlink this wrapper from a directory on your PATH.

resolve_script_dir() {
  local source="${BASH_SOURCE[0]}"
  while [[ -L "$source" ]]; do
    local dir
    dir="$(cd -P -- "$(dirname -- "$source")" >/dev/null 2>&1 && pwd)"
    source="$(readlink -- "$source")"
    [[ "$source" != /* ]] && source="$dir/$source"
  done
  cd -P -- "$(dirname -- "$source")" >/dev/null 2>&1 && pwd
}

SCRIPT_DIR="$(resolve_script_dir)"
PY_SCRIPT="$SCRIPT_DIR/tor-circuit-collector.py"

if [[ ! -f "$PY_SCRIPT" ]]; then
  echo "Error: could not find tor-circuit-collector.py next to this runner: $PY_SCRIPT" >&2
  exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Error: Python executable not found: $PYTHON_BIN" >&2
  echo "Set PYTHON_BIN=/path/to/python or install python3." >&2
  exit 127
fi

exec "$PYTHON_BIN" "$PY_SCRIPT" "$@"
