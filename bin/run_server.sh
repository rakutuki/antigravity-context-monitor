#!/usr/bin/env sh
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if command -v python3 >/dev/null 2>&1; then
  exec python3 -u "$SCRIPT_DIR/server.py" "$@"
else
  exec python -u "$SCRIPT_DIR/server.py" "$@"
fi
