#!/usr/bin/env bash
set -euo pipefail
PORT="${PORT:-8870}"
PID="$(pgrep -f "uvicorn app.main:app .*--port ${PORT}" | head -n 1 || true)"
if [[ -z "$PID" ]]; then
  echo "My English Dictionary is not running on port ${PORT}."
  exit 0
fi
kill "$PID"
echo "Stopped My English Dictionary (PID ${PID})."
