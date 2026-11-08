#!/usr/bin/env bash
# Install LOCODE for the current user: the Python engine in .venv and the built
# shell. On Linux it also writes a launcher with an LC icon into the applications
# menu and on the Desktop; on Windows the launcher scripts are the shortcut
# (scripts/locode-run.sh for Git Bash, scripts/locode-run.ps1 for PowerShell).
#
#   ./install.sh              install or refresh whatever is missing
#   ./install.sh --rebuild    also rebuild the shell (ui/dist) from source
#   PYTHON=python3.12 ./install.sh
#
# Nothing goes system-wide and nothing needs root: the desktop entry is written
# to the user's own data directory, exactly like a hand-made app shortcut.
set -euo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$HERE"

REBUILD=0
for arg in "$@"; do
  if [ "$arg" = "--rebuild" ]; then REBUILD=1; fi
done

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf 'install: %s\n' "$*" >&2; exit 1; }

# The interpreter: python3 on Unix; on Windows the `py` launcher knows where the
# installs are, while `python3` there is often a Store stub that refuses to run.
PY=${PYTHON:-}
if [ -z "$PY" ]; then
  for cand in python3 python "py -3"; do
    if $cand -c 'import sys' > /dev/null 2>&1; then PY=$cand; break; fi
  done
fi
[ -n "$PY" ] || die "no working python on PATH (set PYTHON=... to pick one)"
$PY -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "LOCODE needs Python 3.9 or newer"

say "engine (python)"
# A Windows venv keeps its interpreter in Scripts/python.exe, Unix in bin/python.
VENV_PY=.venv/bin/python
if [ -x .venv/Scripts/python.exe ]; then VENV_PY=.venv/Scripts/python.exe; fi
if [ ! -x "$VENV_PY" ]; then
  $PY -m venv .venv || die "could not create .venv (is python3-venv installed?)"
  if [ -x .venv/Scripts/python.exe ]; then VENV_PY=.venv/Scripts/python.exe; else VENV_PY=.venv/bin/python; fi
fi
"$VENV_PY" -m pip install --quiet --upgrade pip > /dev/null
"$VENV_PY" -m pip install --quiet -r requirements.txt
"$VENV_PY" -c 'import fastapi, uvicorn, httpx' || die "engine dependencies did not import"
echo "ok: $("$VENV_PY" -V)"

say "shell (ui/dist)"
if [ "$REBUILD" = 1 ] || [ ! -f ui/dist/index.html ]; then
  command -v npm > /dev/null 2>&1 || die "npm is needed to build the shell, and ui/dist is missing"
  ( cd ui && if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi )
  # npm's Windows script runner drops the build silently (exit 1, no output),
  # so the entries are called directly; both shells run this fine.
  ( cd ui && node node_modules/typescript/bin/tsc --noEmit && node node_modules/vite/bin/vite.js build )
fi
[ -f ui/dist/index.html ] || die "ui/dist/index.html is still missing"
echo "ok: $(du -sh ui/dist | cut -f1) of shell"

say "launcher"
chmod +x scripts/locode-run.sh

case "$(uname -s 2>/dev/null || echo unknown)" in
  MINGW*|MSYS*|CYGWIN*)
    # Windows has no XDG app menu, so a .desktop file would land in a folder that
    # nothing reads. The two launcher scripts are the shortcut there instead.
    echo "windows: no desktop entry (that is a Linux thing)"
    echo "start it with scripts/locode-run.sh        (Git Bash)"
    echo "             scripts/locode-run.ps1         (PowerShell)"
    say "done"
    echo "The engine answers on http://127.0.0.1:${LOCODE_PORT:-3000}/ and keeps running after the"
    echo "launcher exits. Models: a local llama.cpp server, Ollama, or a provider key in Settings."
    exit 0
    ;;
esac

DATA=${XDG_DATA_HOME:-$HOME/.local/share}
APPS=$DATA/applications
PNG_DIR=$DATA/icons/hicolor/256x256/apps
SVG_DIR=$DATA/icons/hicolor/scalable/apps
mkdir -p "$APPS" "$PNG_DIR" "$SVG_DIR"
install -m 644 assets/locode-256.png "$PNG_DIR/locode.png"
if [ -f assets/locode.svg ]; then
  install -m 644 assets/locode.svg "$SVG_DIR/locode.svg"
fi

# Exec/Path are absolute: a desktop launcher inherits almost no environment, and
# it is started from an arbitrary working directory.
cat > "$APPS/locode.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Version=1.0
Name=LOCODE
GenericName=Local coding agent
Comment=Build and edit this project with a local model
Exec="$HERE/scripts/locode-run.sh"
Path=$HERE
Icon=$PNG_DIR/locode.png
Terminal=false
Categories=Development;
Keywords=code;agent;ai;local;llm;
StartupNotify=true
DESKTOP
chmod 755 "$APPS/locode.desktop"
echo "ok: $APPS/locode.desktop"

DESKTOP_DIR=$(xdg-user-dir DESKTOP 2>/dev/null || true)
[ -n "${DESKTOP_DIR:-}" ] || DESKTOP_DIR=$HOME/Desktop
if [ -d "$DESKTOP_DIR" ]; then
  install -m 755 "$APPS/locode.desktop" "$DESKTOP_DIR/LOCODE.desktop"
  # GNOME only runs a .desktop from the Desktop once it is marked trusted; this
  # is a no-op everywhere else, and harmless when gio is absent.
  if command -v gio > /dev/null 2>&1; then
    gio set "$DESKTOP_DIR/LOCODE.desktop" metadata::trusted true > /dev/null 2>&1 || true
  fi
  echo "ok: $DESKTOP_DIR/LOCODE.desktop"
else
  echo "no Desktop folder at $DESKTOP_DIR — the shortcut is in the app menu"
fi
if command -v update-desktop-database > /dev/null 2>&1; then
  update-desktop-database "$APPS" > /dev/null 2>&1 || true
fi

say "done"
echo "Start it with the LC icon, or from a terminal: scripts/locode-run.sh"
echo "The engine answers on http://127.0.0.1:${LOCODE_PORT:-3000}/ and keeps running after the"
echo "launcher exits. Models: a local llama.cpp server, Ollama, or a provider key in Settings."
