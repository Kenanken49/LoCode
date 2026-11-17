#!/usr/bin/env bash
# Every check LOCODE has, in one command.
#
#   ./check.sh        build the shell, test the shell, run the engine suite
#
# Each step runs even when an earlier one failed, so a single run reports
# everything that is broken instead of only the first thing; the exit status is
# non-zero if any step failed. Nothing here needs a model, a key or the network.
#
# Not included: the browser run in scripts/ui-smoke.mjs. It needs a Chromium
# provisioned by scripts/pw-setup.sh and a git workspace with changes to show,
# so it stays a separate, deliberate run (README, "Browser checks").
set -uo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$HERE"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf 'check: %s\n' "$*" >&2; exit 1; }

command -v npm > /dev/null 2>&1 || die "npm is not on PATH — it builds and tests the shell"
[ -d ui/node_modules ] || die "ui/node_modules is missing — run: ( cd ui && npm install )"
[ -f ui/package-lock.json ] || die "ui/package-lock.json is missing — the shell's deps are unpinned"

# install.sh puts the engine in .venv; a system python works too, but the suite
# starts server.py, which needs the dependencies to import.
PY=.venv/bin/python
if [ ! -x "$PY" ]; then PY=.venv/Scripts/python.exe; fi   # Windows venv layout
if [ ! -x "$PY" ]; then
  for cand in python3 python "py -3"; do
    if $cand -c "import fastapi, uvicorn, httpx" > /dev/null 2>&1; then PY=$cand; break; fi
  done
fi
[ -n "$PY" ] || die "no python3 on PATH (./install.sh creates .venv)"
"$PY" -c 'import fastapi, uvicorn, httpx' 2> /dev/null \
  || die "$PY cannot import the engine's dependencies — run ./install.sh"

FAILED=()
run() {
  local name=$1 start=$SECONDS
  shift
  say "$name"
  "$@"
  local rc=$?
  if [ "$rc" -eq 0 ]; then
    printf '\033[32mok\033[0m: %s (%ss)\n' "$name" "$((SECONDS - start))"
  else
    printf '\033[31mFAILED\033[0m: %s exited %s\n' "$name" "$rc"
    FAILED+=("$name")
  fi
}

# tsc --noEmit then vite build: the types are part of the build, not a separate pass
run "shell build" bash -c 'cd ui && node node_modules/typescript/bin/tsc --noEmit && node node_modules/vite/bin/vite.js build'
# the request builder plan mode depends on, without a browser
run "shell tests" bash -c 'cd ui && node --test test/*.test.ts'
# git endpoints, session history, and the prefix that reaches the provider
run "engine suite" "$PY" tests/test_git_and_plan.py

if [ ${#FAILED[@]} -eq 0 ]; then
  printf '\n\033[32mall checks passed\033[0m in %ss\n' "$SECONDS"
else
  printf '\n\033[31m%d of 3 checks failed\033[0m: %s\n' "${#FAILED[@]}" "${FAILED[*]}"
  exit 1
fi
