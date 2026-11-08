#!/usr/bin/env bash
# Open LOCODE. Starts the engine only when it is not already answering, then
# shows the shell in the browser; the engine keeps running after this exits, so
# clicking the icon again reuses it instead of starting a second one.
#
#   scripts/locode-run.sh                       # http://127.0.0.1:3000/
#   LOCODE_PORT=8080 scripts/locode-run.sh
#   LOCODE_NO_BROWSER=1 scripts/locode-run.sh   # start the engine only
set -euo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
PORT=${LOCODE_PORT:-3000}
URL="http://127.0.0.1:$PORT/"
STATE=${XDG_STATE_HOME:-$HOME/.local/state}/locode
LOG=$STATE/engine-$PORT.log
PIDFILE=$STATE/engine-$PORT.pid
mkdir -p "$STATE"

# /api/health is the one endpoint that proves the engine is the engine: a port
# squatter that refuses the path is not reused.
healthy() { curl -fsS --max-time 1 "${URL}api/health" > /dev/null 2>&1; }

if ! healthy; then
  PY=$HERE/.venv/bin/python
  if [ ! -x "$PY" ]; then PY=$HERE/.venv/Scripts/python.exe; fi   # Windows venv layout
  if [ ! -x "$PY" ]; then PY=$(command -v python3 || true); fi
  if [ -z "${PY:-}" ]; then
    echo "LOCODE: no .venv and no python3 — run ./install.sh first" >&2
    exit 1
  fi
  # WORKSPACE is dropped on purpose: the desktop launcher inherits the session's
  # environment, and a stale WORKSPACE would open a different project.
  (
    cd "$HERE"
    nohup env -u WORKSPACE "$PY" -m uvicorn server:app --host 127.0.0.1 --port "$PORT" \
      >> "$LOG" 2>&1 &
    echo $! > "$PIDFILE"
  )
  for _ in $(seq 1 75); do
    healthy && break
    sleep 0.2
  done
fi

if ! healthy; then
  echo "LOCODE did not answer on $URL — last lines of $LOG:" >&2
  tail -n 20 "$LOG" >&2 || true
  exit 1
fi

echo "LOCODE: $URL"
echo "engine log: $LOG"
if [ "${LOCODE_NO_BROWSER:-0}" != "1" ]; then
  if command -v xdg-open > /dev/null 2>&1; then
    xdg-open "$URL" > /dev/null 2>&1 || true
  elif command -v open > /dev/null 2>&1; then
    open "$URL" > /dev/null 2>&1 || true
  elif command -v cmd > /dev/null 2>&1; then
    cmd //c start "" "$URL" > /dev/null 2>&1 || true   # Git Bash on Windows
  else
    echo "open $URL in your browser"
  fi
fi
