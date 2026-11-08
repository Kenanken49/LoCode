"""LOCODE — a small agentic coding IDE powered by a local model (Ollama).

    pip install -r requirements.txt
    ollama pull qwen2.5-coder:7b
    uvicorn server:app --port 8000
    # open http://localhost:8000
"""
from __future__ import annotations

import asyncio
import difflib
import html
import ipaddress
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import tarfile
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qs, quote_plus, unquote, urlencode, urljoin, urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).resolve().parent
STATE_DIR = BASE / ".locode"
STATE_DIR.mkdir(parents=True, exist_ok=True)
WORKSPACE_STATE = STATE_DIR / "workspace.json"


def _saved_workspace() -> Path | None:
    """The project last opened from the UI — a restart should not forget it."""
    try:
        raw = json.loads(WORKSPACE_STATE.read_text())
        path = Path(str(raw.get("workspace") or "")).resolve()
    except (OSError, ValueError, AttributeError):
        return None
    return path if path.is_dir() else None


# WORKSPACE= in the environment is an explicit instruction and wins over the
# saved project, which is what keeps the test fixtures pointing at their own dirs.
WORKSPACE = (Path(os.environ["WORKSPACE"]) if os.getenv("WORKSPACE")
             else (_saved_workspace() or BASE / "workspace")).resolve()
WORKSPACE.mkdir(parents=True, exist_ok=True)
SESSION_FILE = WORKSPACE / ".locode-session.json"
SESSIONS_DIR = WORKSPACE / ".locode-sessions"

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
DEFAULT_MODEL = os.getenv("MODEL", "qwen2.5-coder:7b")
MAX_STEPS = int(os.getenv("MAX_STEPS", "25"))
CMD_TIMEOUT = int(os.getenv("CMD_TIMEOUT", "180"))

# Sandbox. Every shell command (the agent's run_command and the UI terminal, which
# share one code path) can execute inside a sandbox that has WORKSPACE bind mounted,
# so installs cannot touch the host. Files still live on the host: read_file /
# write_file / edit_file edit the same tree the sandbox sees.
#
#   docker     `docker run` / `docker exec` against a local daemon.
#   namespace  unshare(1) + chroot with a small rootfs: no daemon, no root, nothing
#              to install — works on machines where dockerd cannot run at all.
#   auto       (default) docker when a daemon answers, else the namespace runtime,
#              else the host. This is what lets the app work on a machine with no
#              daemon without anyone setting a variable.
#   off        commands run directly on the host.
#
# `DOCKER_ENABLED=1` still means docker, so existing setups keep working.
SANDBOX_REQUESTED = os.getenv("SANDBOX", "").strip().lower()
if SANDBOX_REQUESTED not in {"auto", "docker", "namespace", "off"}:
    SANDBOX_REQUESTED = "docker" if os.getenv("DOCKER_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on"} else "auto"


def _auto_sandbox() -> str:
    """Resolve SANDBOX=auto: docker daemon, else the built-in namespace runtime.

    Only `auto` probes. An explicit backend is never swapped behind the user's
    back — `SANDBOX=docker` with no daemon still refuses instead of running on the
    host. The probe is one `docker version` (5s cap); `unshare` on PATH is the
    whole namespace requirement here, the rootfs is fetched on first use.
    """
    if shutil.which("docker"):
        try:
            if subprocess.run(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            ).returncode == 0:
                return "docker"
        except (OSError, subprocess.SubprocessError):
            pass
    return "namespace" if shutil.which("unshare") else "off"


SANDBOX = _auto_sandbox() if SANDBOX_REQUESTED == "auto" else SANDBOX_REQUESTED
SANDBOX_ON = SANDBOX != "off"      # "off" is a non-empty string: never test SANDBOX alone
DOCKER_ENABLED = SANDBOX == "docker"
DOCKER_IMAGE = os.getenv("DOCKER_IMAGE", "node:20-bookworm")
DOCKER_MEMORY = os.getenv("DOCKER_MEMORY", "2g")
DOCKER_CPUS = os.getenv("DOCKER_CPUS", "2")
DOCKER_NETWORK = os.getenv("DOCKER_NETWORK", "bridge")
CONTAINER_NAME = os.getenv("DOCKER_CONTAINER_NAME", "locode-sandbox")
CONTAINER_WORKDIR = os.getenv("DOCKER_WORKDIR", "/workspace")
# The namespace backend needs a rootfs and a way to get one. Both are only touched
# when SANDBOX=namespace and a command is actually run.
SANDBOX_ROOTFS = Path(os.getenv("SANDBOX_ROOTFS", str(BASE / ".sandbox-rootfs"))).resolve()
SANDBOX_ROOTFS_URL = os.getenv(
    "SANDBOX_ROOTFS_URL",
    "https://dl-cdn.alpinelinux.org/alpine/v3.20/releases/x86_64/"
    "alpine-minirootfs-3.20.3-x86_64.tar.gz")
SANDBOX_NETWORK = os.getenv("SANDBOX_NETWORK", DOCKER_NETWORK)
# Without cgroup delegation the namespace backend caps memory with RLIMIT_AS and CPUs
# with affinity: both are kernel-enforced, neither is a cgroup quota (see the README).
SANDBOX_MEMORY = os.getenv("SANDBOX_MEMORY", DOCKER_MEMORY)
SANDBOX_CPUS = os.getenv("SANDBOX_CPUS", DOCKER_CPUS)

# Web / browser tools.
NET_ENABLED = os.getenv("LOCODE_NET", "1") != "0"
SEARCH_ENGINE = os.getenv("LOCODE_SEARCH", "duckduckgo")      # duckduckgo | searx
SEARX_URL = os.getenv("LOCODE_SEARX_URL", "").rstrip("/")
BLOCKED_HOSTS = {h.strip().lower() for h in os.getenv("LOCODE_BLOCKED_HOSTS", "").split(",")
                 if h.strip()}
CLONE_HOSTS = {h.strip().lower() for h in os.getenv(
    "LOCODE_CLONE_HOSTS", "github.com,gitlab.com,codeberg.org,bitbucket.org"
).split(",") if h.strip()}
MAX_DOWNLOAD_BYTES = int(os.getenv("MAX_DOWNLOAD_BYTES", str(50 * 1024 * 1024)))
WEB_TEXT_LIMIT = 12000        # chars of page text handed back to the model
WEB_FETCH_BYTES = 3_000_000   # bytes downloaded when turning a page into text
MAX_REDIRECTS = 5
USER_AGENT = "LOCODE/1.0 (+local agent)"
_REDIRECT_CODES = {301, 302, 303, 307, 308}

# Consent. `off` refuses every network tool, `ask` puts each one behind an approval
# card, `auto-safe` (default) reads pages freely but still asks before anything from
# outside touches this machine.
NETWORK_MODE = os.getenv("LOCODE_NETWORK", "auto-safe").strip().lower()
if NETWORK_MODE not in {"off", "ask", "auto-safe"}:
    NETWORK_MODE = "auto-safe"
APPROVAL_TIMEOUT = int(os.getenv("LOCODE_APPROVAL_TIMEOUT", "300"))
# Actions that put bytes on disk. These always need a decision in ask/auto-safe.
DISK_ACTIONS = {"download_file", "clone_repo", "import_file"}

# Downloads and clones are quarantined here until import_file moves them into the
# workspace through an approval card. Deliberately outside WORKSPACE: the file tree,
# the file tools and the sandbox mount must not see unreviewed bytes.
INBOX = Path(os.getenv("LOCODE_INBOX", str(BASE / "inbox"))).resolve()
INBOX.mkdir(parents=True, exist_ok=True)

# Freedom's brain layer: host/RAM probe + budget, curated model catalog, providers.
try:
    from brain.catalog import catalog as brain_catalog
    from brain import providers as brain_providers
    from brain.memory import MemoryEngine
    from brain.models import ModelInfo
    from dataclasses import replace
    _ENGINE = MemoryEngine(
        str(WORKSPACE),
        reserve_gb=float(os.getenv("LOCODE_RESERVE_GB", "0") or 0),
        prefer_local=os.getenv("LOCODE_PREFER_LOCAL", "1") != "0",
        state_dir=str(BASE / ".locode"),
    )
    _ENGINE.refresh()
    _BRAIN_ERROR = ""
except Exception as _e:  # noqa: BLE001 - a missing brain must not stop the IDE
    brain_catalog = None
    brain_providers = None
    MemoryEngine = None
    ModelInfo = None
    replace = None
    _ENGINE = None
    _BRAIN_ERROR = f"{type(_e).__name__}: {_e}"[:200]

IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build",    ".next", "target", ".mypy_cache", ".pytest_cache",
    ".locode-session.json", ".locode-processes.json", ".locode-logs",
    ".locode-sessions",
}

# Seed a README so the tree isn't empty on first run.
if not any(WORKSPACE.iterdir()):
    (WORKSPACE / "README.md").write_text(
        "# Workspace\n\nAsk the agent to build something here.\n"
    )

SYSTEM_PROMPT = """You are LOCODE, an autonomous coding agent running locally on the user's machine.

You have tools to explore the workspace, read and edit files, and run shell commands.
The workspace root is your project directory; all paths are relative to it.

Working style:
- Investigate before acting: list files and read the relevant code first.
- Make focused edits. Use edit_file for surgical changes; write_file for new files or full rewrites.
- Verify: after changes, run the project's tests, linter, or build if available.
- If a command fails, read the error and fix it rather than guessing.
- Keep replies short. Say what you did and why, then stop.

Web tools (web_search, web_fetch, download_file, clone_repo) reach the network only
through a consent gate: public http/https URLs, localhost and private addresses refused,
and in `ask` mode every call waits for the developer to approve it in the UI. Downloads
and clones land in the quarantine inbox, never in the workspace; import_file moves them
in and always asks again. Nothing outside the machine enters the project without a card.

Dev servers, watchers and anything else that never exits must use start_process, not
run_command: run_command waits for the command to finish and kills it at CMD_TIMEOUT.
Use start_server when the user wants to see the running app: it finds the port and LOCODE
opens it in the preview pane. list_processes shows what is running; stop_process ends it.

Never invent file contents. If you need to know something, use a tool.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in the workspace. Optionally pass a subdirectory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative dir, default '.'"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full contents of a file in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact substring in a file. Prefer this over write_file for small changes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string", "description": "Exact text to replace (must be unique)"},
                    "new": {"type": "string", "description": "Replacement text"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command in the workspace directory. Use for builds, tests, installs.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_process",
            "description": "Start a long-running shell command in the background and return immediately with its id (dev servers, watchers, `tail -f`). Its output goes to a log file in the workspace, not to the chat. Never use run_command for something that does not exit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to keep running"},
                    "name": {"type": "string", "description": "Short label, e.g. 'api' or 'vite'"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_process",
            "description": "Stop a background process started with start_process. Pass its id, name or pid; SIGTERM first, SIGKILL if it ignores it.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string", "description": "id, name or pid from list_processes"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_processes",
            "description": "List background processes with their live state (running / exited / stopped) and recent log output. Dead pids are marked exited here.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_server",
            "description": "Start a dev server that keeps running (npm run dev, uvicorn, vite...) and report the port it printed. Use this instead of start_process when the user wants to see the app: LOCODE opens /preview/<name>/ in the preview pane. Never use run_command for a server - it waits and kills it at the timeout.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command, e.g. `npm run dev`"},
                    "name": {"type": "string", "description": "Short label for the preview URL; defaults to the command's first word"},
                    "port": {"type": "integer", "description": "Port if you already know it; otherwise it is parsed from the output"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "checkpoint",
            "description": "Commit all changes to git with a message. Use before risky edits.",
            "parameters": {"type": "object", "properties": {
                "message": {"type": "string"},
            }, "required": ["message"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web (DuckDuckGo by default, or a SearX instance). Returns numbered titles, URLs and snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "description": "1-15 results, default 8"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch a public http/https URL and return its text (HTML is converted to readable text). localhost/private/link-local addresses are refused.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "max_chars": {"type": "integer", "description": "default 12000"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download_file",
            "description": "Download a file from the web into the workspace. Refuses responses larger than MAX_DOWNLOAD_BYTES.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "dest": {"type": "string", "description": "Relative path in the workspace; defaults to the URL's filename"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clone_repo",
            "description": "git clone --depth 1 a repository into the quarantine inbox (never into the workspace). Only hosts in LOCODE_CLONE_HOSTS (github.com, gitlab.com, codeberg.org, bitbucket.org by default). Ask the user to approve, then import_file to move it in.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Repository URL, e.g. https://github.com/owner/repo.git"},
                    "dest": {"type": "string", "description": "Name to save it under in the inbox; defaults to the repo name"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "import_file",
            "description": "Move an item from the quarantine inbox into the workspace. The developer must approve every import - there is no remembered approval. Use it only after download_file or clone_repo, and only when the user asked for the files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "inbox_name": {"type": "string", "description": "The item name returned by download_file / clone_repo"},
                    "dest": {"type": "string", "description": "Relative path in the workspace; defaults to the item name"},
                },
                "required": ["inbox_name"],
            },
        },
    },
]


# ───────────────────────── helpers ─────────────────────────
def safe_path(rel: str) -> Path:
    p = (WORKSPACE / rel).resolve()
    if p != WORKSPACE and WORKSPACE not in p.parents:
        raise ValueError(f"path escapes workspace: {rel}")
    return p


def _diff(old: str, new: str, path: str) -> str:
    if old == new:
        return ""
    d = "".join(difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}", n=3,
    ))
    return d[:4000] + ("\n... (truncated)" if len(d) > 4000 else "")


def _list_files(rel: str = ".") -> str:
    root = safe_path(rel)
    if not root.exists():
        return f"(not found: {rel})"
    if root.is_file():
        return root.relative_to(WORKSPACE).as_posix()
    out = []
    for p in sorted(root.rglob("*")):
        if any(part in IGNORE_DIRS for part in p.relative_to(WORKSPACE).parts):
            continue
        if p.is_file():
            out.append(p.relative_to(WORKSPACE).as_posix())
        if len(out) >= 500:
            out.append("... (truncated)")
            break
    return "\n".join(out) if out else "(empty)"


def _read_file(path: str) -> str:
    p = safe_path(path)
    if not p.is_file():
        return f"(no such file: {path})"
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"(binary file: {path}, {p.stat().st_size} bytes)"
    if len(text) > 30000:
        return text[:30000] + f"\n... (truncated, {len(text)} chars total)"
    return text


def _write_file(path: str, content: str) -> str:
    p = safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    old = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
    p.write_text(content, encoding="utf-8")
    msg = f"Wrote {len(content)} chars to {path}"
    d = _diff(old, content, path)
    return msg + (f"\n\n{d}" if d else " (no changes)")


def _edit_file(path: str, old: str, new: str) -> str:
    p = safe_path(path)
    if not p.is_file():
        return f"(no such file: {path})"
    text = p.read_text(encoding="utf-8")
    n = text.count(old)
    if n == 0:
        return f"ERROR: `old` string not found in {path}. Read the file and use exact text."
    if n > 1:
        return f"ERROR: `old` appears {n} times in {path}. Include more context to make it unique."
    updated = text.replace(old, new, 1)
    p.write_text(updated, encoding="utf-8")
    return f"Edited {path}\n\n{_diff(text, updated, path)}"


# ──────────────────────────── docker sandbox ───────────────────────────

_docker_lock = asyncio.Lock()


def _tail(text: str, n: int = 400) -> str:
    """Docker errors are long; the model only needs the last line or two."""
    return text[-n:] if len(text) > n else text


async def _docker(*args: str, timeout: int = 60) -> tuple[int, str]:
    """Run the docker CLI and return (exit code, combined output)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (FileNotFoundError, OSError) as e:
        return 127, f"docker CLI not found on PATH: {e}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"`docker {' '.join(args)}` timed out after {timeout}s"
    return proc.returncode, out.decode(errors="replace").strip()


async def docker_available() -> bool:
    """True when the docker daemon answers. False if only the CLI is installed."""
    code, _ = await _docker("version", "--format", "{{.Server.Version}}", timeout=10)
    return code == 0


async def _container_state() -> str:
    """running / exited / created / ..., or "absent" when there is no such container."""
    code, out = await _docker(
        "inspect", "-f", "{{.State.Status}}", CONTAINER_NAME, timeout=10)
    return out if code == 0 else "absent"


async def docker_start_container() -> str:
    """Make sure the sandbox exists and is running. Returns a short status line.

    Raises RuntimeError (with the docker output) when it cannot be started.
    The container idles on `sleep infinity` so `docker exec` has somewhere to go.
    """
    async with _docker_lock:
        state = await _container_state()
        if state == "running":
            return f"running {CONTAINER_NAME}"
        if state != "absent":
            code, out = await _docker("start", CONTAINER_NAME, timeout=60)
            if code != 0:
                raise RuntimeError(f"docker start {CONTAINER_NAME} failed: {_tail(out)}")
            return f"started {CONTAINER_NAME}"
        code, out = await _docker("image", "inspect", DOCKER_IMAGE, timeout=30)
        if code != 0:
            code, out = await _docker("pull", DOCKER_IMAGE, timeout=900)
            if code != 0:
                raise RuntimeError(f"docker pull {DOCKER_IMAGE} failed: {_tail(out)}")
        code, out = await _docker(
            "run", "-d", "--name", CONTAINER_NAME,
            "--memory", DOCKER_MEMORY,
            "--cpus", DOCKER_CPUS,
            "--network", DOCKER_NETWORK,
            "--volume", f"{WORKSPACE}:{CONTAINER_WORKDIR}",
            "--workdir", CONTAINER_WORKDIR,
            DOCKER_IMAGE, "sleep", "infinity",
            timeout=120,
        )
        if code != 0:
            raise RuntimeError(f"docker run {DOCKER_IMAGE} failed: {_tail(out)}")
        return f"created {CONTAINER_NAME} from {DOCKER_IMAGE}"


async def docker_stop_container() -> str:
    """Remove the sandbox. A missing container is not an error."""
    async with _docker_lock:
        if await _container_state() == "absent":
            return "no sandbox container to remove"
        code, out = await _docker("rm", "-f", CONTAINER_NAME, timeout=60)
        if code != 0:
            raise RuntimeError(f"docker rm {CONTAINER_NAME} failed: {_tail(out)}")
        return f"removed {CONTAINER_NAME}"


async def docker_restart_container() -> str:
    """Remove and recreate, so a changed image or memory limit actually takes effect."""
    stopped = await _stop_all_processes("sandbox recreated")
    await docker_stop_container()
    status = await docker_start_container()
    return status if not stopped else f"{status} · {stopped} processes stopped"


# ─────────────────── namespace sandbox (no daemon, no root) ───────────────────
# unshare(1) plus a small rootfs: private mount table, pid/uts/ipc spaces, an empty
# network when asked for, and a chroot with the workspace bind mounted. The only
# requirements are `unshare` (util-linux), `mount` and `chroot`, which every Linux
# machine already has — no daemon, no images, no privileges.
#
# What it cannot do is cgroups: memory and CPU caps need delegation an unprivileged
# user namespace does not get, so the status reports limits: false instead of
# pretending --memory / --cpus are enforced.

_ns_lock = asyncio.Lock()
NS_MARKER = ".locode-rootfs"       # written into a rootfs LOCODE unpacked itself
NS_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
NS_SCRIPT = r"""
set -u
ROOTFS=$1; WS=$2; WD=$3; NET=$4; PIDNS=$5; MEM_KB=$6
hostname "$LOC_HOST" 2>/dev/null || true
# RLIMIT_AS in KiB, inherited by everything the container runs. Address space, not
# RSS: generous for interpreters, still a hard stop for a runaway allocation.
if [ "$MEM_KB" -gt 0 ] 2>/dev/null; then ulimit -v "$MEM_KB" 2>/dev/null || true; fi
mount --bind "$ROOTFS" "$ROOTFS" || exit 97
mount -t tmpfs tmpfs "$ROOTFS/dev" || exit 98
# The nodes are created here rather than bind-mounted from the host: a device from a
# foreign superblock cannot be opened inside the user namespace (EACCES), while mknod
# on our own tmpfs works. No mknod -> a plain file, which still swallows writes.
for spec in "null 1 3" "zero 1 5" "full 1 7" "random 1 8" "urandom 1 9"; do
    set -- $spec
    mknod -m 666 "$ROOTFS/dev/$1" c "$2" "$3" 2>/dev/null || : > "$ROOTFS/dev/$1" 2>/dev/null
done
mkdir -p "$ROOTFS/dev/shm" && mount -t tmpfs -o mode=1777 tmpfs "$ROOTFS/dev/shm" 2>/dev/null
# A fresh proc mount is only permitted inside a private pid namespace; without one
# the kernel refuses to expose the parent's processes. Share the host's read-only.
if [ "$PIDNS" = "1" ]; then
    mount -t proc proc "$ROOTFS/proc" || exit 99
else
    mount --bind /proc "$ROOTFS/proc" || exit 99
    mount -o remount,ro,bind "$ROOTFS/proc" 2>/dev/null || true
fi
if [ "$NET" != "none" ] && [ -f /etc/resolv.conf ]; then
    : > "$ROOTFS/etc/resolv.conf" 2>/dev/null
    mount --bind /etc/resolv.conf "$ROOTFS/etc/resolv.conf" 2>/dev/null
fi
mkdir -p "$ROOTFS$WD" && mount --bind "$WS" "$ROOTFS$WD" || exit 96
exec chroot "$ROOTFS" /bin/sh -c 'cd "$LOC_PWD" || exit 95
exec /bin/sh -lc "$LOC_CMD"'
"""


def _ns_missing_tool() -> str:
    for tool in ("unshare", "mount", "chroot"):
        if not shutil.which(tool):
            return tool
    return ""


async def namespace_available() -> tuple[bool, str]:
    """Can this machine create an unprivileged user namespace right now?"""
    missing = _ns_missing_tool()
    if missing:
        return False, f"{missing} is not on PATH"
    try:
        proc = await asyncio.create_subprocess_exec(
            "unshare", "--user", "--map-root-user", "true",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    except (FileNotFoundError, OSError) as e:
        return False, f"cannot run unshare: {e}"
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        return False, "unshare --user hung"
    if proc.returncode != 0:
        why = err.decode(errors="replace").strip().splitlines()
        return False, "user namespaces are not permitted here: " + (why[-1] if why else "refused")
    return True, "ready"


def _rootfs_member(member: tarfile.TarInfo) -> tarfile.TarInfo:
    """Alpine links busybox with *absolute* targets (`bin/chown -> /bin/busybox`).

    Inside a chroot an absolute target is always inside the rootfs, but the strict
    `data` extraction filter refuses it, so rewrite it relative to the link's own
    directory. Relative links are then checked by the filter as usual, which keeps
    the traversal protection without loosening anything.
    """
    if not (member.issym() or member.islnk()) or not member.linkname.startswith("/"):
        return member
    if member.islnk():                       # hard links are archive-relative
        member.linkname = member.linkname.lstrip("/")
        return member
    here = os.path.dirname("/" + member.name.lstrip(".").lstrip("/")) or "/"
    member.linkname = os.path.relpath(member.linkname, here)
    return member


def _unpack_rootfs(tar_path: Path) -> None:
    SANDBOX_ROOTFS.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as tf:
        # device nodes and fifos are skipped: /dev is a fresh tmpfs at run time
        members = [_rootfs_member(m) for m in tf.getmembers()
                   if not (m.isdev() or m.isfifo())]
        try:
            tf.extractall(SANDBOX_ROOTFS, members=members, filter="data")
        except TypeError:                                  # Python < 3.12
            tf.extractall(SANDBOX_ROOTFS, members=members)


async def namespace_ensure_rootfs() -> str:
    """Unpack the mini rootfs once, then reuse it. Raises with a readable reason."""
    async with _ns_lock:
        if (SANDBOX_ROOTFS / NS_MARKER).exists():
            return f"rootfs {SANDBOX_ROOTFS}"
        ok, why = await namespace_available()
        if not ok:
            raise RuntimeError(f"namespace sandbox unavailable: {why}")
        if SANDBOX_ROOTFS.exists() and any(SANDBOX_ROOTFS.iterdir()):
            raise RuntimeError(
                f"{SANDBOX_ROOTFS} exists but was not provisioned by LOCODE; "
                "point SANDBOX_ROOTFS at a fresh path (or at a tree containing "
                f"{NS_MARKER}) instead of letting it be overwritten")
        SANDBOX_ROOTFS.parent.mkdir(parents=True, exist_ok=True)
        tmp = SANDBOX_ROOTFS.parent / f".rootfs-{os.urandom(4).hex()}.tar.gz"
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=None) as c:
                async with c.stream("GET", SANDBOX_ROOTFS_URL) as r:
                    r.raise_for_status()
                    with tmp.open("wb") as fh:
                        async for chunk in r.aiter_bytes():
                            fh.write(chunk)
            await asyncio.to_thread(_unpack_rootfs, tmp)
            (SANDBOX_ROOTFS / NS_MARKER).write_text(SANDBOX_ROOTFS_URL + "\n", encoding="utf-8")
        except (httpx.HTTPError, OSError, tarfile.TarError) as e:
            shutil.rmtree(SANDBOX_ROOTFS, ignore_errors=True)
            raise RuntimeError(
                f"could not provision {SANDBOX_ROOTFS}: {type(e).__name__}: {e}")
        finally:
            tmp.unlink(missing_ok=True)
        return f"unpacked {SANDBOX_ROOTFS.name} from {SANDBOX_ROOTFS_URL.rsplit('/', 1)[-1]}"


def _mem_kb(spec: str) -> int:
    """'2g' / '512m' / '1048576' -> KiB (0 = no limit). RLIMIT_AS is expressed in KiB."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmg]?)b?\s*", (spec or "").strip(), re.I)
    if not m:
        return 0
    scale = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(2).lower()]
    return int(float(m.group(1)) * scale // 1024)


def _cpu_affinity(spec: str) -> int:
    """How many cores the sandbox may use, clamped to this machine (0 = no pinning)."""
    try:
        n = int(float(spec))
    except (TypeError, ValueError):
        return 0
    return max(0, min(n, os.cpu_count() or 1))


def _ns_argv(pid_ns: bool = True) -> list[str]:
    """unshare argv for one command.

    pid_ns=False keeps the host pid namespace. A private one is what a one-shot
    `run_command` wants, but a process the registry has to recognise later (dev
    servers, liveness checks, log tails) must keep a pid the host can still see.
    """
    net = SANDBOX_NETWORK.strip().lower()
    argv: list[str] = []
    cpus = _cpu_affinity(SANDBOX_CPUS)
    if cpus:
        # affinity is inherited across unshare and inside the container, so this really
        # is how many cores the command can run on (`--cpus` for people without cgroups)
        argv += ["taskset", "-c", "0" if cpus == 1 else f"0-{cpus - 1}"]
    # --propagation private: without it the mounts below can propagate back into the
    # host's table and the next run fails on its own leftovers.
    argv += ["unshare", "--user", "--map-root-user", "--mount", "--propagation",
             "private", "--ipc", "--uts"]
    if pid_ns:
        argv += ["--pid", "--fork", "--kill-child"]
    else:
        argv.append("--fork")
    if net in {"none", "off", "disabled"}:
        argv.append("--net")
    argv += ["/bin/sh", "-c", NS_SCRIPT, "_",
             str(SANDBOX_ROOTFS), str(WORKSPACE), CONTAINER_WORKDIR, net,
             "1" if pid_ns else "0", str(_mem_kb(SANDBOX_MEMORY))]
    return argv


def _ns_env(command: str) -> dict[str, str]:
    """A clean environment: host variables (and any secrets in them) stay outside."""
    return {
        "PATH": NS_PATH,
        "HOME": "/root",
        "TERM": os.environ.get("TERM", "xterm"),
        "LOC_HOST": CONTAINER_NAME,
        "LOC_PWD": CONTAINER_WORKDIR,
        "LOC_CMD": command,
    }


async def namespace_status() -> dict:
    ok, why = await namespace_available()
    return {
        "enabled": True,
        "mode": "namespace",
        "available": ok,
        "why": why,
        "running": (SANDBOX_ROOTFS / NS_MARKER).exists(),
        "rootfs": str(SANDBOX_ROOTFS),
        "image": SANDBOX_ROOTFS_URL.rsplit("/", 1)[-1],
        "network": SANDBOX_NETWORK,
        "requested": SANDBOX_REQUESTED,
        "limits": "rlimit+affinity",
        "memory": SANDBOX_MEMORY,
        "cpus": SANDBOX_CPUS,
    }


async def namespace_stop() -> str:
    """Nothing is left running: every command is its own container."""
    return "namespace sandbox has no persistent container to stop"


async def namespace_restart() -> str:
    """Re-unpack the rootfs, so a changed SANDBOX_ROOTFS_URL takes effect."""
    stopped = await _stop_all_processes("sandbox recreated")
    if (SANDBOX_ROOTFS / NS_MARKER).exists():
        shutil.rmtree(SANDBOX_ROOTFS, ignore_errors=True)
    status = await namespace_ensure_rootfs()
    return status if not stopped else f"{status} · {stopped} processes stopped"


# ─────────────────────── one contract, two backends ───────────────────────
def sandbox_label() -> str:
    """How the sandbox is named in process listings and tool output."""
    return {"docker": "docker sandbox",
            "namespace": "namespace sandbox"}.get(SANDBOX, "workspace shell")


async def sandbox_available() -> tuple[bool, str]:
    if SANDBOX == "docker":
        ok = await docker_available()
        return ok, ("docker daemon answered per version" if ok else
                    "no docker daemon answered (is it running?) — SANDBOX=namespace needs no daemon")
    if SANDBOX == "namespace":
        return await namespace_available()
    return False, "sandbox is off (set SANDBOX=docker or SANDBOX=namespace)"


async def sandbox_prepare(ensure: bool = True) -> str:
    """Make the sandbox usable. Raises RuntimeError with a readable reason.

    ensure=False only checks readiness. Read-only questions (is this pid alive?)
    use it so they can never create a container or fetch a rootfs just to answer.
    """
    if SANDBOX == "docker":
        if ensure:
            return await docker_start_container()
        if (await _container_state()) != "running":
            raise RuntimeError(f"sandbox container {CONTAINER_NAME} is not running")
        return "running"
    if SANDBOX == "namespace":
        ok, why = await namespace_available()
        if not ok:
            raise RuntimeError(f"namespace sandbox unavailable: {why}")
        if ensure:
            return await namespace_ensure_rootfs()
        if not (SANDBOX_ROOTFS / NS_MARKER).exists():
            raise RuntimeError(f"no rootfs at {SANDBOX_ROOTFS}")
        return "ready"
    return "off"


async def sandbox_stop() -> str:
    if SANDBOX == "docker":
        return await docker_stop_container()
    if SANDBOX == "namespace":
        return await namespace_stop()
    return "sandbox is off"


async def sandbox_restart() -> str:
    if SANDBOX == "docker":
        return await docker_restart_container()
    if SANDBOX == "namespace":
        return await namespace_restart()
    return "sandbox is off"


async def _spawn_shell(command: str, ensure: bool = True, pid_ns: bool = True):
    """Start `command` inside the sandbox, else in the host workspace.

    ensure=False skips preparing it. Read-only checks (is this pid alive?) use it so
    they can never create a container to answer a question. pid_ns only applies to
    the namespace backend and only to commands something must find again later.
    """
    if SANDBOX_ON:
        await sandbox_prepare(ensure)           # RuntimeError -> handled by caller
        if SANDBOX == "docker":
            return await asyncio.create_subprocess_exec(
                "docker", "exec", "--workdir", CONTAINER_WORKDIR, CONTAINER_NAME,
                "sh", "-lc", command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        return await asyncio.create_subprocess_exec(
            *_ns_argv(pid_ns=pid_ns), env=_ns_env(command),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    return await asyncio.create_subprocess_shell(
        command,
        cwd=str(WORKSPACE),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )


async def _run_command(command: str) -> str:
    try:
        proc = await _spawn_shell(command)
    except RuntimeError as e:                    # the sandbox would not come up
        return f"ERROR (sandbox): {e}"
    except (FileNotFoundError, OSError) as e:
        return f"ERROR: {type(e).__name__}: {e}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=CMD_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        # kills the docker CLI only; the process inside the container runs on
        return f"TIMEOUT after {CMD_TIMEOUT}s"
    text = out.decode(errors="replace")
    if len(text) > 20000:
        text = text[:20000] + "\n... (truncated)"
    return f"$ {command}\n{text}\n(exit {proc.returncode})"


# ────────────────────────── process manager ──────────────────────────
# Long-running commands (dev servers, watchers) cannot go through run_command: that
# waits for the command to exit and kills it at CMD_TIMEOUT. These start the command
# detached with its output in the workspace, track the pid, and re-check liveness on
# demand. Pids are checked in the same namespace they were started in, so with a
# sandbox they are container pids verified by `kill -0` inside the container.

PROC_LOG_DIRNAME = ".locode-logs"
PROC_FILE = WORKSPACE / ".locode-processes.json"
PROC_LOG_DIR = WORKSPACE / PROC_LOG_DIRNAME
PROC_KEEP = 20          # exited entries kept; running ones are never dropped
PROC_TAIL = 240         # chars of log output handed back

_proc_lock = asyncio.Lock()


def _load_procs() -> list[dict]:
    try:
        data = json.loads(PROC_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = data.get("processes") if isinstance(data, dict) else data
    return [r for r in (rows or []) if isinstance(r, dict) and r.get("id")]


def _prune(rows: list[dict]) -> list[dict]:
    out, exited = [], 0
    for r in rows:
        if r.get("state") == "running":
            out.append(r)
        elif exited < PROC_KEEP:
            exited += 1
            out.append(r)
    return out


def _save_procs(rows: list[dict]) -> None:
    try:
        PROC_FILE.write_text(json.dumps({"processes": _prune(rows)}, indent=2),
                             encoding="utf-8")
    except OSError:
        pass                        # a lost registry is not worth failing a command over


def _log_tail(rel: str, chars: int = PROC_TAIL) -> str:
    """Last lines of a process log. The file lives in the workspace, so the host can
    read it even when the process itself runs inside the container."""
    if not rel:
        return ""
    try:
        text = (WORKSPACE / rel).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return text[-chars:]


async def _shell_capture(command: str, *, ensure: bool = True,
                         timeout: int = 30) -> tuple[int, str]:
    """Run one shell command through the sandbox/host path and capture its output.

    Everything routed here works with pids the host can see later (starting a
    process, checking one, killing them), so no private pid namespace.
    """
    proc = await _spawn_shell(command, ensure=ensure, pid_ns=False)   # RuntimeError -> caller
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"timed out after {timeout}s"
    return proc.returncode, out.decode(errors="replace").strip()


def _sandbox_host_pids() -> bool:
    """True when sandbox pids are real host pids, so liveness and kills happen here.

    Docker keeps processes inside a named container, so a pid only means something
    through `docker exec`. The namespace backend starts each long-running command as
    a background `unshare` on the host instead: the pid it records is a host pid, and
    `--kill-child` means killing that wrapper takes the container with it.
    """
    return SANDBOX == "namespace"


def _pid_alive_host(pid: int) -> bool:
    """A pid is alive if /proc has it and it is not a zombie."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return stat.rsplit(")", 1)[-1].split()[:1] != ["Z"]


def _kill_host(pid: int, group: bool, sig: int) -> None:
    """Signal a host-side sandbox process: its group when it leads one, else the pid."""
    if group:
        try:
            os.killpg(pid, sig)
            return
        except (ProcessLookupError, PermissionError):
            pass                    # no such group here: fall back to the pid alone
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass                        # already gone, or not ours to kill


async def _run_on_host(command: str, timeout: int = 30) -> tuple[int, str]:
    """Run a launcher on the host (never inside a sandbox)."""
    proc = await asyncio.create_subprocess_shell(
        command, cwd=str(WORKSPACE),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"timed out after {timeout}s"
    return proc.returncode, out.decode(errors="replace").strip()


async def _pids_alive(pids: list[int]) -> set[int]:
    """Which of these pids are still up, checked where they were started."""
    wanted = sorted({int(p) for p in pids})
    if not wanted:
        return set()
    if _sandbox_host_pids():
        return {p for p in wanted if _pid_alive_host(p)}
    script = "; ".join(f"kill -0 {p} 2>/dev/null && echo alive {p}" for p in wanted)
    try:
        _, out = await _shell_capture(script, ensure=False, timeout=20)
    except (RuntimeError, OSError):        # sandbox gone -> nothing in it can be alive
        return set()
    alive = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "alive" and parts[1].isdigit():
            alive.add(int(parts[1]))
    return alive


async def _refresh_procs() -> list[dict]:
    """Registry with live state: a running entry whose pid is gone becomes exited."""
    rows = _load_procs()
    pids = [r["pid"] for r in rows if r.get("state") == "running" and r.get("pid")]
    alive = await _pids_alive(pids) if pids else set()
    changed = False
    for r in rows:
        if r.get("state") == "running" and r.get("pid") not in alive:
            r["state"] = "exited"
            r["ended"] = time.strftime("%Y-%m-%d %H:%M:%S")
            changed = True
    if changed:
        async with _proc_lock:
            _save_procs(rows)
    return rows


_setsid_ok: bool | None = None       # probed once: it is a property of the image
# coreutils stdbuf makes line-buffered what libc would block-buffer (non-Python
# servers); `command -v` guards images that ship without it.
_STDF = "$(command -v stdbuf >/dev/null 2>&1 && echo stdbuf -oL -eL)"


async def _has_setsid() -> bool:
    """Can this shell detach a process into its own group? Without setsid there is no
    group to kill, so stop_process falls back to the wrapper pid alone. The answer is
    read from the launcher's own output, because a failing setsid writes to the
    process log, not back to us."""
    global _setsid_ok
    if _sandbox_host_pids():
        return bool(shutil.which("setsid"))    # setsid runs on the host for this backend
    if _setsid_ok is None:
        try:
            code, _ = await _shell_capture("command -v setsid >/dev/null 2>&1", timeout=20)
        except (RuntimeError, OSError):
            return False            # sandbox down: _start_process reports that itself
        _setsid_ok = code == 0
    return _setsid_ok


async def _start_process(command: str, name: str = "") -> str:
    command = (command or "").strip()
    if not command:
        return "ERROR: `command` is required"
    proc_id = os.urandom(4).hex()
    rel = f"{PROC_LOG_DIRNAME}/{proc_id}.log"
    try:
        PROC_LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return f"ERROR: cannot create {PROC_LOG_DIRNAME}: {e}"
    # where the *shell* writes the log: inside the container the workspace is mounted
    # at CONTAINER_WORKDIR, on the host it is the workspace path itself
    # docker writes the log inside the container (the workspace is mounted there);
    # the host-side launcher of the namespace backend writes it directly
    target = f"{CONTAINER_WORKDIR}/{rel}" if DOCKER_ENABLED else str(WORKSPACE / rel)

    def launcher(detach: bool) -> str:
        # setsid makes the command its own session/group leader, so the recorded pid
        # is the one a group kill can reach — `npm run dev` forks the real server, and
        # killing only the wrapper would leave the port bound. nohup alone cannot do
        # that, and `set -m` needs a tty this shell never has.
        #
        # PYTHONUNBUFFERED + stdbuf matter for the preview pane: a dev server writes its
        # "Local: http://127.0.0.1:5173" banner to stdout, and stdout to a file is block
        # buffered, so without them the port never appears in the log and the pane stays
        # blind while the server is actually up.
        lead = "setsid" if detach else "nohup"
        if _sandbox_host_pids():
            # one background `unshare`: its pid is the handle, --kill-child ties the
            # command's life to it, and the redirect happens on the host
            env = " ".join(f"{k}={shlex.quote(v)}" for k, v in _ns_env(command).items())
            argv = " ".join(shlex.quote(a) for a in _ns_argv())
            return (f"export PYTHONUNBUFFERED=1; {lead} env {env} {argv} "
                    f"> {shlex.quote(target)} 2>&1 < /dev/null & echo __pid__=$!")
        return (f"export PYTHONUNBUFFERED=1; {lead} {_STDF} sh -c {shlex.quote(command)} "
                f"> {shlex.quote(target)} 2>&1 < /dev/null & echo __pid__=$!")

    try:
        detach = await _has_setsid()
        if _sandbox_host_pids():
            code, out = await _run_on_host(launcher(detach), timeout=CMD_TIMEOUT)
        else:
            code, out = await _shell_capture(launcher(detach), timeout=CMD_TIMEOUT)
        group = detach
    except RuntimeError as e:
        return f"ERROR (sandbox): {e}"
    found = re.search(r"__pid__=(\d+)", out)
    if not found:
        return f"ERROR: could not start the process (exit {code})\n{_tail(out)}"
    row = {
        "id": proc_id,
        "name": (name or "").strip() or re.split(r"\s+", command)[0][:24],
        "command": command,
        "pid": int(found.group(1)),
        "log": rel,
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "state": "running",
        "group": group,          # False -> this pid leads no group, plain kill only
    }
    await asyncio.sleep(0.5)                    # give it a moment to fall over
    if row["pid"] not in await _pids_alive([row["pid"]]):
        row["state"] = "exited"
        row["ended"] = row["started"]
    async with _proc_lock:
        _save_procs([row] + _load_procs())
    tail = _log_tail(rel)
    if row["state"] != "running":
        return (f"start_process: {row['name']} ({proc_id}) exited immediately.\n"
                f"log: {rel}\n\n{tail or '(no output)'}")
    where = sandbox_label()
    return (f"Started {row['name']} ({proc_id}, pid {row['pid']}) in the {where}.\n"
            f"output -> {rel}\nStop it with stop_process(id=\"{proc_id}\")."
            + (f"\n\n{tail}" if tail else ""))


async def _stop_process(target: str) -> str:
    target = (target or "").strip()
    if not target:
        return "ERROR: `id` is required — an id, name or pid from list_processes"
    rows = await _refresh_procs()
    hits = [r for r in rows if r["id"] == target or r.get("name") == target]
    if not hits and target.isdigit():
        hits = [r for r in rows if str(r.get("pid")) == target]
    if not hits:
        return f"ERROR: no tracked process {target!r}. Call list_processes first."
    if len(hits) > 1:
        return f"ERROR: {len(hits)} processes match {target!r}; pass the id instead"
    row = hits[0]
    if row.get("state") != "running":
        return f"{row['id']} ({row['name']}) is already {row.get('state', 'stopped')}"
    pid = int(row["pid"])
    # the group first, so the dev server itself dies with its wrapper; a pid that is
    # not a leader just makes the group kill fail over to the plain one
    if row.get("group") is False:
        term, hard = f"kill -TERM {pid} 2>/dev/null", f"kill -KILL {pid} 2>/dev/null"
    else:
        term = f"kill -TERM -{pid} 2>/dev/null || kill -TERM {pid} 2>/dev/null"
        hard = f"kill -KILL -{pid} 2>/dev/null || kill -KILL {pid} 2>/dev/null"
    host_side = _sandbox_host_pids()
    grouped = row.get("group") is not False
    try:
        if host_side:
            _kill_host(pid, grouped, signal.SIGTERM)
        else:
            await _shell_capture(term, timeout=20)
        for _ in range(6):                       # up to 3s for a graceful exit
            await asyncio.sleep(0.5)
            if pid not in await _pids_alive([pid]):
                break
        else:
            if host_side:
                _kill_host(pid, grouped, signal.SIGKILL)
            else:
                await _shell_capture(hard, timeout=20)
            for _ in range(4):                   # SIGKILL still needs a moment to land
                await asyncio.sleep(0.25)
                if pid not in await _pids_alive([pid]):
                    break
    except RuntimeError as e:
        return f"ERROR (sandbox): {e}"
    alive = pid in await _pids_alive([pid])
    if not alive:
        row["state"] = "stopped"
        row["ended"] = time.strftime("%Y-%m-%d %H:%M:%S")
    async with _proc_lock:
        _save_procs(rows)
    if alive:
        return f"ERROR: {row['id']} ({row['name']}) ignored SIGTERM and SIGKILL"
    tail = _log_tail(row.get("log", ""), 400)
    return (f"Stopped {row['name']} ({row['id']}, pid {pid})."
            + (f"\nlast output:\n{tail}" if tail else ""))


async def _list_processes() -> str:
    rows = await _refresh_procs()
    if not rows:
        return ("No tracked processes. Use start_process for dev servers and any "
                "other command that never exits.")
    running = sum(1 for r in rows if r.get("state") == "running")
    where = sandbox_label()
    lines = [f"{running} running / {len(rows)} tracked in the {where}:"]
    for i, r in enumerate(rows):
        lines.append(f"- {r['id']}  {str(r.get('state', '?')):<8} pid {str(r.get('pid', '?')):<8} "
                     f"{r.get('name', '')}  ::  {str(r.get('command', ''))[:90]}")
        if i < 5:                                # tails only for the newest few
            tail = _log_tail(r.get("log", ""))
            if tail:
                lines.append("    " + tail.replace("\n", "\n    ")[-PROC_TAIL:])
    return "\n".join(lines)


async def _stop_all_processes(reason: str) -> int:
    """Kill everything tracked. The sandbox being recreated and the workspace being
    reset both throw these processes away without asking first."""
    rows = _load_procs()
    running = [r for r in rows if r.get("state") == "running" and r.get("pid")]
    if not running:
        return 0
    if _sandbox_host_pids():
        for r in running:
            _kill_host(int(r["pid"]), r.get("group") is not False, signal.SIGKILL)
        return len(running)
    kills = []
    for r in running:
        p = int(r["pid"])
        kills.append(f"kill -9 {p} 2>/dev/null" if r.get("group") is False
                     else f"kill -9 -{p} 2>/dev/null; kill -9 {p} 2>/dev/null")
    try:
        await _shell_capture("; ".join(kills), ensure=False, timeout=20)
    except (RuntimeError, OSError, ValueError):
        pass                     # nothing to kill: the container is already gone
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for r in running:
        r["state"], r["ended"], r["note"] = "stopped", now, reason
    async with _proc_lock:
        _save_procs(rows)
    return len(running)


# ───────────────────────── consent / approvals ─────────────────────────
_NET_OFF = "ERROR: web tools are disabled (LOCODE_NETWORK=off)"

_pending: dict[str, dict] = {}          # id -> approval row, resolved by the UI
_approved_domains: set[str] = set()     # session scope only: nothing is persisted
_approved_actions: set[str] = set()
_approval_seq = 0


def _approval_required(action: str, host: str) -> bool:
    """Does this call need a card right now? Remembered domains/actions skip it."""
    if NETWORK_MODE == "off":
        return False                       # refused earlier, never a card
    if action in _approved_actions:
        return False
    if host and host in _approved_domains:
        return False
    if NETWORK_MODE == "ask":
        return True
    return action in DISK_ACTIONS          # auto-safe: only bytes on disk ask


async def _consent(action: str, url: str, host: str, detail: str) -> str | None:
    """None -> allowed. Anything else is the tool result to hand back to the model."""
    global _approval_seq
    if NETWORK_MODE == "off":
        return _NET_OFF
    if not _approval_required(action, host):
        return None
    _approval_seq += 1
    ap = {
        "id": f"{time.strftime('%H%M%S')}-{_approval_seq:03d}",
        "action": action,
        "url": url,
        "host": host,
        "detail": detail,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "expires": time.time() + APPROVAL_TIMEOUT,
        "decision": "",
    }
    _pending[ap["id"]] = ap
    try:
        while time.time() < ap["expires"] and not ap["decision"]:
            await asyncio.sleep(0.2)
        if ap["decision"] == "allow":
            return None
        if ap["decision"] == "deny":
            return "denied by developer"
        return f"ERROR: no approval for {action} within {APPROVAL_TIMEOUT}s — nothing was fetched."
    finally:
        _pending.pop(ap["id"], None)


def _resolve_approval(ap_id: str, approved: bool, remember: list[str]) -> str:
    ap = _pending.get(ap_id)
    if not ap:
        return "gone"                      # already answered or timed out
    for r in remember or []:
        if r == "domain" and ap["host"]:
            _approved_domains.add(ap["host"])
        elif r == "action":
            _approved_actions.add(ap["action"])
    ap["decision"] = "allow" if approved else "deny"
    return ap["decision"]


# ───────────────────────── quarantine inbox ─────────────────────────
def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9.+-]+", "-", (text or "").lower()).strip("-.")
    return s[:32] or "item"


def _inbox_dir(name: str) -> Path:
    """Resolve a quarantine name, refusing anything that escapes INBOX."""
    p = (INBOX / (name or "")).resolve()
    if p == INBOX or INBOX not in p.parents:
        raise ValueError(f"not a valid inbox item: {name!r}")
    return p


def _tree_bytes(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _inbox_items() -> list[dict]:
    out = []
    for item in sorted(INBOX.iterdir(), reverse=True) if INBOX.is_dir() else []:
        if item.name.startswith("."):
            continue
        out.append({
            "name": item.name,
            "kind": "dir" if item.is_dir() else "file",
            "bytes": _tree_bytes(item),
            "created": time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(item.stat().st_mtime)),
        })
    return out


# ───────────────────────── servers (processes with a port) ─────────────────────────
_PORT_RE = re.compile(
    r"(?:https?://)?(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|\[?::\]?):(\d{2,5})\b")


async def _patch_proc(proc_id: str, **fields) -> None:
    async with _proc_lock:
        rows = _load_procs()
        for r in rows:
            if r["id"] == proc_id:
                r.update(fields)
        _save_procs(rows)


def _listen_sockets(out: str) -> list[tuple[int, int]]:
    """(pid, port) pairs from `ss -ltnp` output — only LISTEN sockets with an owner."""
    rows: list[tuple[int, int]] = []
    for line in (out or "").splitlines():
        pm = re.search(r"pid=(\d+)", line)
        if not pm:
            continue
        local = next((f for f in line.split()
                      if re.search(r"[:.\]](\d{2,5})$", f)), "")
        m = re.search(r"(\d{2,5})$", local)
        if m and 1024 <= int(m.group(1)) <= 65535:
            rows.append((int(pm.group(1)), int(m.group(1))))
    return rows


async def _descendants(pid: int) -> set[int]:
    """pid plus every process under it, so a wrapper that forked the real server
    still shows up. Same shell namespace as the launcher (container or host)."""
    try:
        _, out = await _shell_capture("ps -eo pid=,ppid=", ensure=False, timeout=20)
    except (RuntimeError, OSError):
        return {pid}
    kids: dict[int, list[int]] = {}
    for line in out.splitlines():
        f = line.split()
        if len(f) == 2 and f[0].isdigit() and f[1].isdigit():
            kids.setdefault(int(f[1]), []).append(int(f[0]))
    seen, stack = {pid}, [pid]
    while stack:
        for k in kids.get(stack.pop(), []):
            if k not in seen:
                seen.add(k)
                stack.append(k)
    return seen


async def _port_by_socket(row: dict) -> int | None:
    """Fallback when the server never printed a URL: ask the kernel who is listening."""
    try:
        _, out = await _shell_capture(
            "ss -ltnp 2>/dev/null || netstat -ltnp 2>/dev/null", ensure=False, timeout=20)
    except (RuntimeError, OSError):
        return None
    tree = await _descendants(int(row["pid"]))
    ours = await _descendants(os.getpid())          # never proxy through ourselves
    ports = [p for pid, p in _listen_sockets(out) if pid in tree and pid not in ours]
    return min(ports) if ports else None


def _scan_port(text: str) -> int | None:
    """First plausible port in a dev server's own output."""
    for m in _PORT_RE.finditer(text or ""):
        try:
            port = int(m.group(1))
        except ValueError:
            continue
        if 1024 <= port <= 65535:
            return port
    return None


async def _find_port(row: dict, tries: int = 16) -> int | None:
    """Dev servers print their URL a moment after starting, so keep looking. If the
    banner never arrives (buffered, quiet, or a server that says nothing), fall back to
    the listening socket the process actually opened."""
    for i in range(tries):
        port = _scan_port(_log_tail(row["log"], 4000))
        if port:
            return port
        if row["pid"] not in await _pids_alive([row["pid"]]):
            return None
        if i >= 6 and (port := await _port_by_socket(row)):
            return port
        await asyncio.sleep(0.5)
    return await _port_by_socket(row)


def _human_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "0 B"


def _server_rows() -> list[dict]:
    return [r for r in _load_procs() if r.get("server")]


# ───────────────────────── web / browser ─────────────────────────


class _Fetched(NamedTuple):
    url: str
    status: int
    content_type: str
    body: bytes


def _ip_is_public(ip) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local)
    )


def _resolves_public(host: str) -> bool | None:
    """True: every address is public. False: one is local/private. None: DNS failed."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return None
    addrs = {i[4][0].split("%")[0] for i in infos}
    if not addrs:
        return None
    return all(_ip_is_public(ipaddress.ip_address(a)) for a in addrs)


async def _url_guard(url: str, *, allow_private: bool = False) -> tuple[str, str]:
    """Check scheme, blocklist and resolved IPs. Returns (url, hostname)."""
    url = (url or "").strip()
    if not NET_ENABLED:
        raise ValueError(_NET_OFF.removeprefix("ERROR: "))
    if not re.match(r"^https?://", url, re.I):
        raise ValueError(f"only http/https URLs are allowed, got: {url[:80] or '(empty)'}")
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError(f"missing hostname: {url[:80]}")
    if host in BLOCKED_HOSTS or any(host.endswith("." + b) for b in BLOCKED_HOSTS):
        raise ValueError(f"host is in LOCODE_BLOCKED_HOSTS: {host}")
    if not allow_private:
        public = await asyncio.to_thread(_resolves_public, host)
        if public is None:
            raise ValueError(f"could not resolve host: {host}")
        if not public:
            raise ValueError(
                f"refused {host}: localhost, private and link-local addresses "
                "are blocked by design"
            )
    return url, host


async def _http_get(url: str, *, max_bytes: int, timeout: float = 30.0,
                    allow_private: bool = False) -> _Fetched:
    """GET with redirects followed one hop at a time, re-guarding every hop."""
    current = url
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False,
                                 headers={"User-Agent": USER_AGENT}) as c:
        for _hop in range(MAX_REDIRECTS + 1):
            current, _host = await _url_guard(current, allow_private=allow_private)
            async with c.stream("GET", current) as r:
                if r.status_code in _REDIRECT_CODES:
                    loc = r.headers.get("location")
                    await r.aread()
                    if not loc:
                        raise ValueError(f"HTTP {r.status_code} with no Location header")
                    current = urljoin(current, loc)
                    continue
                if r.status_code >= 400:
                    await r.aread()
                    raise ValueError(f"HTTP {r.status_code} for {current}")
                try:
                    declared = int(r.headers.get("content-length") or 0)
                except ValueError:
                    declared = 0
                if declared > max_bytes:
                    raise ValueError(
                        f"{current} is {declared} bytes, over the {max_bytes} byte limit")
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        raise ValueError(f"aborted: response exceeded {max_bytes} bytes")
                return _Fetched(current, r.status_code,
                                r.headers.get("content-type", ""), bytes(buf))
    raise ValueError(f"too many redirects (>{MAX_REDIRECTS}) for {url}")


_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header",
    "footer", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "dd", "dt",
    "nav", "aside", "main", "form", "figure", "figcaption", "hr",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "img":
            alt = dict(attrs).get("alt") or ""
            if alt:
                self.parts.append(f"\n[image: {alt}]\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _inline(markup: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", markup))).strip()


def _html_to_text(raw: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:  # noqa: BLE001  malformed markup should not kill the tool
        pass
    body = re.sub(r"[ \t\xa0]+", " ", "".join(parser.parts))
    lines: list[str] = []
    for line in (ln.strip() for ln in body.splitlines()):
        if line:
            lines.append(line)
        elif lines and lines[-1]:
            lines.append("")
    title = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    head = _inline(title.group(1)) if title else ""
    return (f"{head}\n\n" if head else "") + "\n".join(lines).strip()


def _ddg_unwrap(href: str) -> str:
    href = html.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    u = urlparse(href)
    if u.netloc.endswith("duckduckgo.com") and u.path.startswith("/l/"):
        target = parse_qs(u.query).get("uddg", [""])[0]
        if target:
            return unquote(target)
    return href


_LINK_RE = re.compile(
    r"<a\b([^>]*?class=[\"'](?:result__a|result-link)[\"'][^>]*?)>(.*?)</a>", re.S | re.I)


async def _search_duckduckgo(query: str, limit: int) -> list[tuple[str, str, str]]:
    q = quote_plus(query)
    rows: list[tuple[str, str, str]] = []
    for base in ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/"):
        try:
            got = await _http_get(f"{base}?q={q}", max_bytes=1_500_000, timeout=20)
        except ValueError:
            break
        raw = got.body.decode("utf-8", errors="replace")
        rows = []
        snippets = re.findall(r"<a[^>]*class=[\"']result__snippet[\"'][^>]*>(.*?)</a>",
                              raw, re.S | re.I)
        if not snippets:
            snippets = re.findall(r"<td[^>]*class=[\"']result-snippet[\"'][^>]*>(.*?)</td>",
                                  raw, re.S | re.I)
        for i, m in enumerate(_LINK_RE.finditer(raw)):
            href = re.search(r"href=[\"'](.*?)[\"']", m.group(1), re.S)
            if not href:
                continue
            url = _ddg_unwrap(href.group(1))
            if not url.startswith("http"):
                continue
            rows.append((_inline(m.group(2)), url,
                         _inline(snippets[i]) if i < len(snippets) else ""))
        if rows:
            break
    return rows[:limit]


async def _search_searx(query: str, limit: int) -> list[tuple[str, str, str]]:
    if not SEARX_URL:
        raise ValueError("LOCODE_SEARCH=searx but LOCODE_SEARX_URL is not set")
    url = f"{SEARX_URL}/search?" + urlencode({"q": query, "format": "json"})
    # The operator configured this endpoint, so it may live on a private address.
    got = await _http_get(url, max_bytes=2_000_000, timeout=20, allow_private=True)
    data = json.loads(got.body.decode("utf-8", errors="replace"))
    return [((r.get("title") or "").strip(), (r.get("url") or "").strip(),
             (r.get("content") or "").strip())
            for r in (data.get("results") or [])[:limit]]


async def _web_search(query: str, limit: int = 8) -> str:
    if not NET_ENABLED:
        return _NET_OFF
    query = (query or "").strip()
    if not query:
        return "ERROR: empty search query"
    limit = max(1, min(int(limit or 8), 15))
    try:
        rows = (await _search_searx(query, limit) if SEARCH_ENGINE == "searx"
                else await _search_duckduckgo(query, limit))
    except Exception as e:  # noqa: BLE001
        return f"ERROR: search failed: {type(e).__name__}: {e}"
    if not rows:
        return f"No results for {query!r} ({SEARCH_ENGINE})."
    blocks = []
    for i, (title, url, snippet) in enumerate(rows, 1):
        line = f"{i}. {title or url}\n   {url}"
        if snippet:
            line += f"\n   {snippet[:300]}"
        blocks.append(line)
    return f"Results for {query!r} via {SEARCH_ENGINE}:\n\n" + "\n\n".join(blocks)


async def _web_fetch(url: str, max_chars: int = WEB_TEXT_LIMIT) -> str:
    if not NET_ENABLED:
        return _NET_OFF
    denied = await _consent("web_fetch", (url or "").strip(),
                            (urlparse(url).hostname or "").lower(),
                            f"GET {(url or '')[:160]}\nHost: {urlparse(url).hostname or '?'}"
                            f". Body up to {WEB_FETCH_BYTES:,} bytes, read only.")
    if denied:
        return denied
    max_chars = max(500, min(int(max_chars or WEB_TEXT_LIMIT), 40000))
    try:
        got = await _http_get(url, max_bytes=WEB_FETCH_BYTES, timeout=30)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: fetch failed: {type(e).__name__}: {e}"
    raw = got.body.decode("utf-8", errors="replace")
    start = raw.lstrip()[:200].lower()
    if "html" in got.content_type.lower() or start.startswith("<!doctype html") or start.startswith("<html"):
        text = _html_to_text(raw)
    else:
        text = raw.strip()
    head = f"{got.url} — HTTP {got.status}, {len(got.body)} bytes, {got.content_type or 'unknown type'}"
    if not text:
        return f"{head}\n\n(no readable text in response)"
    if len(text) > max_chars:
        return (f"{head}\n\n--- {len(text)} chars of text, showing first {max_chars} ---\n\n"
                + text[:max_chars])
    return f"{head}\n\n{text}"


async def _download_file(url: str, dest: str = "") -> str:
    """Fetch into the quarantine inbox. Nothing reaches the workspace until the
    developer approves import_file, which asks a second time."""
    if not NET_ENABLED:
        return _NET_OFF
    try:
        target, host = await _url_guard(url)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    name = Path((dest or "").strip() or urlparse(target).path or "").name or f"{host}-download"
    denied = await _consent("download_file", target, host,
                            f"GET {target[:160]}\nHost: {host}. Up to {MAX_DOWNLOAD_BYTES:,} bytes. "
                            f"Lands in the inbox as {name}, not in the workspace.")
    if denied:
        return denied
    try:
        got = await _http_get(target, max_bytes=MAX_DOWNLOAD_BYTES, timeout=120)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: download failed: {type(e).__name__}: {e}"
    item = f"{time.strftime('%Y%m%d-%H%M%S')}-{_slug(name)}"
    p = INBOX / item
    p.mkdir(parents=True, exist_ok=False)
    (p / _slug(name)).write_bytes(got.body)
    (p / "SOURCE.txt").write_text(
        f"{got.url}\n{got.content_type or 'unknown type'}\n{len(got.body)} bytes\nrequested as {name}\n",
        encoding="utf-8")
    return (f"Downloaded {len(got.body)} bytes into the inbox as {item}/ (quarantined — "
            f"not in the workspace). Show it to the user, then call "
            f'import_file(inbox_name="{item}", dest="{name}") to move it in.')


async def _clone_repo(url: str, dest: str = "") -> str:
    if not NET_ENABLED:
        return _NET_OFF
    try:
        target, host = await _url_guard(url)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    allowed = {h.strip("/") for h in CLONE_HOSTS}
    if not any(host == a or host.endswith("." + a) for a in allowed):
        return (f"ERROR: {host} is not an allowed clone host "
                f"(LOCODE_CLONE_HOSTS: {', '.join(sorted(allowed)) or 'none'})")
    name = (dest or "").strip().strip("/") or Path(urlparse(target).path).stem
    if not name or name in (".", ".."):
        return "ERROR: could not derive a directory name; pass dest"
    denied = await _consent("clone_repo", target, host,
                            f"git clone --depth 1 {target[:160]}\nHost: {host}. "
                            f"Lands in the inbox as {name}/, not in the workspace.")
    if denied:
        return denied
    item = f"{time.strftime('%Y%m%d-%H%M%S')}-{_slug(name)}"
    p = INBOX / item
    if p.exists():
        return f"ERROR: {item} already exists in the inbox"
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", "--depth", "1", target, item, cwd=str(INBOX),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=CMD_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
        return f"ERROR: git clone timed out after {CMD_TIMEOUT}s"
    text = out.decode(errors="replace")[-2000:]
    if proc.returncode != 0:
        shutil.rmtree(p, ignore_errors=True)
        return f"ERROR: git clone failed (exit {proc.returncode})\n{text}"
    files = sum(1 for f in p.rglob("*") if f.is_file() and ".git" not in f.parts)
    return (f"Cloned {target} into the inbox as {item}/ ({files} files, quarantined).\n{text}\n"
            f'Show it to the user, then import_file(inbox_name="{item}", dest="{name}") to move it in.')


async def _import_file(inbox_name: str, dest: str = "") -> str:
    """Move a quarantined download/clone into the workspace. Always asks; never
    remembers, because "allow once" is the point of the gate."""
    try:
        src = _inbox_dir(inbox_name)
    except ValueError as e:
        return f"ERROR: {e}"
    if not src.exists():
        avail = ", ".join(i["name"] for i in _inbox_items()) or "(empty)"
        return f"ERROR: no inbox item {inbox_name!r}. Available: {avail}"
    name = (dest or "").strip().strip("/") or src.name.replace(
        f"{src.name.split('-')[0]}-", "", 1)
    try:
        dst = safe_path(name)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    if dst == WORKSPACE:
        return "ERROR: dest cannot be the workspace root"
    if dst.exists():
        return f"ERROR: {name} already exists in the workspace; pick another dest"
    size = _tree_bytes(src)
    denied = await _consent("import_file", f"inbox:///{src.name}", "",
                            f"Move {src.name} ({_human_bytes(size)}) into the workspace "
                            f"at {name}. This is the last confirmation before unreviewed "
                            "code is inside your project.")
    if denied:
        return denied if denied.startswith("ERROR") or denied == "denied by developer" \
            else f"ERROR: {denied}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(src), str(dst))
    except OSError as e:
        return f"ERROR: move failed: {e}"
    rel = dst.relative_to(WORKSPACE).as_posix()
    return f"Imported {src.name} from the inbox to {rel}/ ({_human_bytes(size)})."


async def _start_server(command: str, name: str = "", port_hint: int = 0) -> str:
    """Launch a dev server and find the port it printed, so the UI can proxy it."""
    command = (command or "").strip()
    if not command:
        return "ERROR: `command` is required (e.g. `npm run dev`)"
    started = await _start_process(command, name or "server")
    m = re.search(r"\(([0-9a-f]{8})[\",]", started)
    if not m:
        return started
    row = next((r for r in _load_procs() if r["id"] == m.group(1)), None)
    if not row:
        return started
    label = re.sub(r"[^a-z0-9-]+", "-", (name or row["name"] or "app").lower()).strip("-") or "app"
    port = int(port_hint or 0) or await _find_port(row)
    await _patch_proc(row["id"], server=True, port=port, server_name=label)
    if not port:
        return (f"{label} started ({row['id']}) but no port appeared in its output.\n"
                f"{started}\nLogs: GET /api/servers/logs?name={label}&tail=200")
    return (f"{label} is running on port {port} ({row['id']}). "
            f"Preview: /preview/{label}/ — the UI opens it in the preview pane.\n{started}")


async def execute_tool(name: str, args: dict) -> str:
    try:
        if name == "list_files":
            return _list_files(args.get("path", "."))
        if name == "read_file":
            return _read_file(args["path"])
        if name == "write_file":
            return _write_file(args["path"], args["content"])
        if name == "edit_file":
            return _edit_file(args["path"], args["old"], args["new"])
        if name == "run_command":
            return await _run_command(args["command"])
        if name == "start_process":
            return await _start_process(args["command"], args.get("name", ""))
        if name == "stop_process":
            return await _stop_process(args["id"])
        if name == "list_processes":
            return await _list_processes()
        if name == "start_server":
            return await _start_server(args["command"], args.get("name", ""),
                                       args.get("port", 0))
        if name == "import_file":
            return await _import_file(args["inbox_name"], args.get("dest", ""))
        if name == "checkpoint":
            await _exclude_session_files()
            await _run_git("add", "-A")
            return await _run_git("commit", "-m", args.get("message", "checkpoint"))
        if name == "web_search":
            return await _web_search(args.get("query", ""), args.get("limit", 8))
        if name == "web_fetch":
            return await _web_fetch(args["url"], args.get("max_chars", WEB_TEXT_LIMIT))
        if name == "download_file":
            return await _download_file(args["url"], args.get("dest", ""))
        if name == "clone_repo":
            return await _clone_repo(args["url"], args.get("dest", ""))
        return f"ERROR: unknown tool {name}"
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {type(e).__name__}: {e}"


def sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


# ───────────────────────── app ─────────────────────────
app = FastAPI(title="LOCODE")

# The bundle is ~260 KB of JavaScript: gzip turns that into one quick round trip
# instead of a noticeable wait, which matters most on a remote engine.
app.add_middleware(GZipMiddleware, minimum_size=512)


@app.middleware("http")
async def _cache_headers(request: Request, call_next):
    """Vite hashes asset file names, so one URL never changes content; index.html
    must be revalidated so a rebuild shows up on the next load."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/assets/"):
        response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
    elif path in ("/", "/index.html"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response

# The front end is a React + TypeScript app (ui/), built by Vite into ui/dist.
# That directory is build output, so the asset mount is guarded: a clone without
# it still starts, and the index tells you the one command to run.
UI = BASE / "ui" / "dist"
if (UI / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=UI / "assets"), name="ui-assets")


class FileWrite(BaseModel):
    path: str
    content: str


class ExecRequest(BaseModel):
    command: str


class ChatRequest(BaseModel):
    messages: list[dict]
    model: str | None = None
    base_url: str | None = None    # OpenAI-compatible endpoint override (chunk 3)


@app.get("/")
def index():
    # chunk 2's shell document, now produced by the React build
    if (UI / "index.html").is_file():
        return FileResponse(UI / "index.html")
    return Response(
        "LOCODE UI is not built yet:\n\n  cd locode/ui && npm install && npm run build\n",
        status_code=503,
        media_type="text/plain",
    )


@app.get("/api/files")
def api_files():
    out = []
    for p in WORKSPACE.rglob("*"):
        if p.is_file() and not any(
            part in IGNORE_DIRS for part in p.relative_to(WORKSPACE).parts
        ):
            out.append(p.relative_to(WORKSPACE).as_posix())
    out = sorted(out)
    # `paths` is the name the wireframe spec uses; `files` is the older client contract
    return {"files": out, "paths": out}


@app.get("/api/file")
def api_get_file(path: str):
    p = safe_path(path)
    if not p.is_file():
        raise HTTPException(404, "not found")
    return {"path": path, "content": p.read_text(encoding="utf-8", errors="replace")}


@app.put("/api/file")
def api_put_file(body: FileWrite):
    p = safe_path(body.path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body.content, encoding="utf-8")
    return {"ok": True}


UPLOADS = WORKSPACE / "uploads"
UPLOAD_MAX = int(os.getenv("LOCODE_UPLOAD_MAX", str(25 * 1024 * 1024)))


@app.post("/api/upload")
async def api_upload(request: Request, name: str = ""):
    """A file the developer picked with the composer's +.

    Raw body with the name in the query string, so no multipart parser is needed.
    It lands in workspace/uploads/ and nowhere else; the UI puts the returned path
    in the prompt, and the agent treats it like any other workspace file.
    """
    clean = Path(unquote(name or "")).name
    if not clean or clean in {".", ".."}:
        raise HTTPException(400, "a filename is required (?name=file.txt)")
    body = await request.body()
    if not body:
        raise HTTPException(400, "empty upload")
    if len(body) > UPLOAD_MAX:
        raise HTTPException(413, f"upload is larger than {UPLOAD_MAX // 1048576} MB")
    UPLOADS.mkdir(parents=True, exist_ok=True)
    dest = UPLOADS / clean
    stem, suffix = Path(clean).stem, Path(clean).suffix
    n = 1
    while dest.exists():                       # never overwrite an earlier upload
        dest = UPLOADS / f"{stem}-{n}{suffix}"
        n += 1
    dest.write_bytes(body)
    return {"ok": True, "path": str(dest.relative_to(WORKSPACE)), "bytes": len(body)}


@app.post("/api/exec")
async def api_exec(body: ExecRequest):
    return {"output": await _run_command(body.command)}


# ────────────────────────── sandbox status ───────────────────────────
# The route names stay `/api/docker/*` so existing clients and docs keep working;
# both backends answer through them.
@app.get("/api/docker/status")
async def api_docker_status(response: Response):
    """What the UI pill shows. Cheap enough to poll on boot and after each command."""
    # a cached copy of this outlives a restart with a different SANDBOX, and the
    # chip would then name the backend that is no longer running
    response.headers["Cache-Control"] = "no-store"
    if SANDBOX == "namespace":
        return await namespace_status()
    if not DOCKER_ENABLED:
        return {"enabled": False, "mode": "off", "requested": SANDBOX_REQUESTED}
    available = await docker_available()
    running = False
    if available:
        running = (await _container_state()) == "running"
    return {
        "enabled": True,
        "mode": "docker",
        "requested": SANDBOX_REQUESTED,
        "available": available,
        "running": running,
        "image": DOCKER_IMAGE,
        "container": CONTAINER_NAME,
        "memory": DOCKER_MEMORY,
        "cpus": DOCKER_CPUS,
        "network": DOCKER_NETWORK,
        "limits": "cgroups",
    }


@app.post("/api/docker/restart")
async def api_docker_restart():
    """Recreate the sandbox. Errors come back as data, not HTTP codes: the pill
    shows them as a chat notice either way."""
    if not SANDBOX_ON:
        return {"ok": False,
                "error": "sandbox disabled: start the server with SANDBOX=docker or "
                         "SANDBOX=namespace"}
    ok, why = await sandbox_available()
    if not ok:
        return {"ok": False, "error": why}
    try:
        return {"ok": True, "status": await sandbox_restart()}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}


# ────────────────────── process status ──────────────────────
@app.get("/api/processes")
async def api_processes():
    """What the terminal chip shows: tracked processes with live state. Read-only —
    starting and stopping them is the agent's job (or a `kill` in the terminal)."""
    rows = await _refresh_procs()
    out = []
    for r in rows:
        exited = r.get("state") != "running"
        out.append({
            "id": r.get("id"), "name": r.get("name"), "command": r.get("command"),
            "pid": r.get("pid"), "state": r.get("state"), "log": r.get("log"),
            "started": r.get("started"), "ended": r.get("ended"), "note": r.get("note"),
            "tail": _log_tail(r.get("log", ""), 300) if exited else "",
        })
    return {
        "processes": out,
        "running": sum(1 for r in rows if r.get("state") == "running"),
        "sandbox": SANDBOX,
    }


# ───────────────────────── git ─────────────────────────
async def _run_git(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(WORKSPACE),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return out.decode(errors="replace")

async def _git_identity() -> list:
    # A fresh box has no user.name/user.email, so `git commit` refuses with
    # "Author identity unknown" - and the first commit is what Initialize makes.
    # These are `-c` overrides for THIS invocation only: ~/.gitconfig is never
    # written, so a developer's own identity still wins where one exists.
    name = (await _run_git("config", "user.name")).strip()
    email = (await _run_git("config", "user.email")).strip()
    args: list = []
    if not name:
        args += ["-c", "user.name=LOCODE"]
    if not email:
        args += ["-c", "user.email=locode@localhost"]
    return args


# LOCODE keeps its transcript beside the project (.locode-session.json and one file
# per conversation under .locode-sessions/). Those are the engine's records, not the
# developer's work: git read them as untracked changes and "Commit all" swept the
# conversation into the project's history. .git/info/exclude keeps them out of both,
# and because it is per-checkout it never becomes a file the developer commits.
SESSION_EXCLUDES = (".locode-session.json", ".locode-sessions/")


async def _exclude_session_files() -> None:
    """Hide the engine's own session files from git, idempotently."""
    if not (WORKSPACE / ".git").is_dir():
        return
    info = WORKSPACE / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    path = info / "exclude"
    current = path.read_text(errors="replace") if path.exists() else ""
    lines = current.splitlines()
    missing = [entry for entry in SESSION_EXCLUDES if entry not in lines]
    if not missing:
        return
    text = current
    if text and not text.endswith("\n"):
        text += "\n"
    if not any(line.startswith("# LOCODE session history") for line in lines):
        text += "# LOCODE session history - the engine's records, not the project's\n"
    text += "\n".join(missing) + "\n"
    path.write_text(text)


@app.get("/api/git/status")
async def api_git_status():
    if not (WORKSPACE / ".git").exists():
        return {"initialized": False}
    await _exclude_session_files()
    branch = (await _run_git("rev-parse", "--abbrev-ref", "HEAD")).strip()
    if branch.startswith("fatal:") or branch == "HEAD":
        branch = "(no commits yet)"
    # Which of LOCODE's own records are being kept out of git right now. Only the
    # ones on disk are named: the drawer should explain the absence of a file that
    # exists, not advertise paths that were never written.
    hidden = [entry for entry in SESSION_EXCLUDES if (WORKSPACE / entry.rstrip("/")).exists()]
    return {"initialized": True,
            "status": await _run_git("status", "--short"),
            "branch": branch,
            "excluded": hidden}


@app.post("/api/git/init")
async def api_git_init():
    await _run_git("init")
    await _exclude_session_files()
    await _run_git("add", "-A")
    ident = await _git_identity()
    out = await _run_git(*ident, "commit", "-m", "LOCODE: initial commit")
    # `ok` used to be unconditional, so a refused first commit still reported
    # success and the shell showed an empty repository as if it had a commit.
    return {"ok": "fatal:" not in out, "output": out.strip()}


@app.post("/api/git/commit")
async def api_git_commit(body: dict):
    msg = (body or {}).get("message") or "LOCODE: checkpoint"
    await _exclude_session_files()
    await _run_git("add", "-A")
    ident = await _git_identity()
    out = await _run_git(*ident, "commit", "-m", msg)
    return {"output": out}

DIFF_LIMIT = 400_000          # bytes of one file's diff a drawer may be sent


@app.get("/api/git/diff")
async def api_git_diff(path: str = ""):
    """One file's unified diff, as git writes it.

    `git status --short` says *what* changed; this says *how*, and a commit
    message has to be written against the second one. An untracked file has no
    version in HEAD, so it is diffed against /dev/null - the same shape, every
    line added - instead of being reported as "no changes".
    """
    rel = unquote(path).strip()
    if not rel:
        raise HTTPException(400, "path is required")
    target = (WORKSPACE / rel).resolve()
    # a diff is a read of the workspace: never of something outside it
    if target != WORKSPACE and WORKSPACE not in target.parents:
        raise HTTPException(400, "that path is outside the workspace")
    rel = target.relative_to(WORKSPACE).as_posix()
    status = await _run_git("status", "--short", "--", rel)
    untracked = any(line.startswith("??") for line in status.splitlines())
    # A rename is two paths: diffed alone the new name has no version in HEAD, so
    # git would report the whole file as added - the opposite of what happened.
    # The pair comes from the full status because a pathspec holding only the new
    # name makes git print that one line as an addition.
    source = ""
    for line in (await _run_git("status", "--short")).splitlines():
        if " -> " in line and line[3:].split(" -> ")[-1].strip() == rel:
            source = line[3:].split(" -> ")[0].strip()
    if untracked:
        out = await _run_git("diff", "--no-color", "--no-index", "--", "/dev/null", rel)
    elif source:
        out = await _run_git("diff", "HEAD", "--no-color", "--", source, rel)
    else:
        out = await _run_git("diff", "HEAD", "--no-color", "--", rel)
        # a repository whose first commit was refused has no HEAD to compare with
        if "fatal:" in out:
            out = await _run_git("diff", "--no-color", "--", rel)
    truncated = len(out) > DIFF_LIMIT
    return {"path": rel, "untracked": untracked, "truncated": truncated,
            "diff": out[:DIFF_LIMIT]}


# ─────────────────────── session history ───────────────────────
# One transcript used to be the whole story: "New session" overwrote it, so the
# previous conversation was gone. Every session now also keeps its own file under
# .locode-sessions/, and .locode-session.json stays the one that is open — the
# shape older clients read, plus the id that ties it to its history file.
SESSION_ID_RE = re.compile(r"^[0-9a-f]{8,32}$")


def _session_path(sid: str) -> Path:
    return SESSIONS_DIR / (sid + ".json")


def _read_json_file(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _session_messages(record: dict) -> list:
    messages = record.get("messages")
    return messages if isinstance(messages, list) else []


def _session_title(messages: list) -> str:
    """What the developer first said - that is what the conversation is about."""
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        text = content if isinstance(content, str) else json.dumps(content)
        text = " ".join(text.split())
        if text:
            return text[:60] + ("…" if len(text) > 60 else "")
    return "(empty session)"


def _write_session(sid: str, messages: list) -> None:
    """The open transcript and its history copy, written together.

    Two files because the open session has to keep answering the old
    GET /api/session shape, while history needs one file per conversation that
    opening another one must not touch.
    """
    title = _session_title(messages)
    SESSION_FILE.write_text(json.dumps(
        {"id": sid, "title": title, "messages": messages}, indent=2))
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = _session_path(sid)
    created = _read_json_file(path).get("created")
    path.write_text(json.dumps({
        "id": sid, "title": title, "created": created or time.time(),
        "updated": time.time(), "messages": messages,
    }, indent=2))


def _open_session() -> dict:
    """The conversation that is open, adopted into history on first read.

    A transcript written before history existed has no id; it gets one and a
    history file, instead of staying invisible until the next turn.
    """
    record = _read_json_file(SESSION_FILE) if SESSION_FILE.is_file() else {}
    messages = _session_messages(record)
    sid = record.get("id") if isinstance(record.get("id"), str) else ""
    if not (sid and SESSION_ID_RE.match(sid)):
        sid = os.urandom(6).hex()
        _write_session(sid, messages)
    elif not _session_path(sid).is_file():
        _write_session(sid, messages)
    return {"id": sid, "title": _session_title(messages), "messages": messages}


@app.get("/api/session")
async def api_get_session():
    if not SESSION_FILE.is_file():
        return {"id": "", "messages": []}
    return _open_session()


@app.post("/api/session")
async def api_put_session(body: dict):
    body = body or {}
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise HTTPException(400, "messages must be a list")
    pinned = body.get("id") if isinstance(body.get("id"), str) else ""
    if pinned and not SESSION_ID_RE.match(pinned):
        raise HTTPException(400, "not a session id")
    current = _open_session() if SESSION_FILE.is_file() else {"id": "", "messages": []}
    # An empty transcript with no id is "New session" from the UI: it starts a new
    # conversation and leaves the open one in history. An empty save that names
    # its session is a plain save, which is how a switch keeps the thread it leaves.
    if pinned:
        sid = pinned
    elif messages:
        sid = current["id"] or os.urandom(6).hex()
    elif current["messages"]:
        sid = os.urandom(6).hex()          # archive what is open, start an empty one
    else:
        sid = current["id"] or os.urandom(6).hex()   # already empty: do not pile up
    _write_session(sid, messages)
    # A new conversation must clear the brain's memory too, or the thread looks
    # blank while the model still remembers every turn of the last one.
    if not messages and _ENGINE is not None:
        _ENGINE.conversation.clear()
        _ENGINE.save()
    return {"ok": True, "id": sid}


@app.get("/api/sessions")
async def api_list_sessions():
    """Past conversations of this workspace, newest first, with the open one named."""
    current = _open_session()["id"] if SESSION_FILE.is_file() else ""
    paths = sorted(SESSIONS_DIR.glob("*.json")) if SESSIONS_DIR.is_dir() else []
    rows = []
    for path in paths:
        record = _read_json_file(path)
        sid = record.get("id") if isinstance(record.get("id"), str) else path.stem
        if not SESSION_ID_RE.match(sid or ""):
            continue
        messages = _session_messages(record)
        rows.append({
            "id": sid,
            "title": str(record.get("title") or _session_title(messages)),
            "messages": len(messages),
            "updated": float(record.get("updated") or 0),
        })
    rows.sort(key=lambda r: r["updated"], reverse=True)
    return {"sessions": rows, "current": current}


@app.post("/api/sessions/open")
async def api_open_session(body: dict):
    """Point the engine at a past conversation and answer its transcript."""
    sid = str((body or {}).get("id") or "")
    if not SESSION_ID_RE.match(sid):
        raise HTTPException(400, "not a session id")
    path = _session_path(sid)
    if not path.is_file():
        raise HTTPException(404, "no such session")
    record = _read_json_file(path)
    messages = _session_messages(record)
    title = str(record.get("title") or _session_title(messages))
    SESSION_FILE.write_text(json.dumps(
        {"id": sid, "title": title, "messages": messages}, indent=2))
    # The brain's memory belongs to the conversation that is open, so opening
    # another one forgets the previous context instead of mixing the two.
    if _ENGINE is not None:
        _ENGINE.conversation.clear()
        _ENGINE.save()
    return {"id": sid, "title": title, "messages": messages}


# ───────────────────────── reset ─────────────────────────
@app.post("/api/reset")
async def api_reset(body: dict):
    if (body or {}).get("confirm") != "yes":
        raise HTTPException(400, 'pass {"confirm":"yes"} to reset')
    # the wipe below deletes the process registry and its logs, so stop the tracked
    # processes first instead of leaving them running with no record of them
    await _stop_all_processes("workspace reset")
    keep = {".git", ".locode-session.json", ".locode-sessions"}
    for item in WORKSPACE.iterdir():
        if item.name in keep:
            continue
        shutil.rmtree(item) if item.is_dir() else item.unlink()
    return {"ok": True}


# ───────────────────────── model layer: Ollama + Freedom's catalog ─────────────────────────
_CLS_FOR = {"llama.cpp": "LlamaCpp", "ollama": "Ollama", "anthropic": "Anthropic",
            "gemini": "Gemini"}
_MODELS_CACHE: tuple[float, dict] | None = None
MODELS_CACHE_TTL = 20


def _catalog_entry(model_id: str):
    if brain_catalog is None:
        return None
    for m in brain_catalog():
        if m.id == model_id:
            return m
    return None


def _brain_provider(m):
    """Instantiate Freedom's adapter for a catalog entry."""
    cls = getattr(brain_providers, _CLS_FOR.get(m.provider, "OpenAICompat"))
    return cls(m)


def _turn_source(model_id: str, base_url: str = ""):
    """("ollama", None) streams through Ollama; ("provider", p) uses the brain adapter."""
    m = _catalog_entry(model_id)
    if base_url:
        mm = replace(m, base_url=base_url) if m is not None else ModelInfo(
            id=model_id, provider="openai-compat", label=model_id, kind="cloud",
            base_url=base_url, tools=True, note="custom base url")
        return "provider", _brain_provider(mm)
    if m is None:
        # An id the catalog does not know is servable only when Ollama has that
        # tag pulled; anything else must error instead of being handed to a
        # server that would answer it with something unrelated.
        tags, _err = _ollama_tags()
        return ("ollama", None) if model_id in tags else ("unknown", None)
    if m.provider == "ollama":
        return "ollama", None
    return "provider", _brain_provider(m)


def _ollama_tags() -> tuple[list[str], str]:
    try:
        with httpx.Client(timeout=3) as c:
            r = c.get(f"{OLLAMA_URL}/api/tags")
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])], ""
    except Exception as e:  # noqa: BLE001
        return [], f"{OLLAMA_URL} unreachable ({type(e).__name__})"


def _fits(m, ok: bool) -> bool:
    if _ENGINE is None or m.kind != "local":
        return True
    try:
        return bool(_ENGINE.fit_model(int(m.required_ram_gb * 1e9)))
    except Exception:  # noqa: BLE001
        return True


def _model_rows() -> tuple[list[dict], str, list[dict]]:
    """Installed Ollama tags first (they stream), then the catalog probed for
    availability.

    Only models that can answer right now are returned as rows; the rest come
    back as `unavailable` so the composer can say what is missing instead of
    offering a pick that cannot run.
    """
    rows: list[dict] = []
    hidden: list[dict] = []
    tags, ollama_err = _ollama_tags()
    for t in tags:
        m = _catalog_entry(t)
        rows.append({
            "id": t, "label": m.label if m else t, "provider": "ollama",
            "kind": "local", "context": m.context if m else 8192,
            "params_b": m.params_b if m else 0.0, "tools": True, "vision": False,
            "available": True, "why": "installed in Ollama", "fits": True,
            "stream": True, "required_ram_gb": m.required_ram_gb if m else 0.0,
            "note": (m.note if m else "") or "local",
        })
    if brain_catalog is not None:
        for m in brain_catalog():
            if m.id in tags:
                continue
            try:
                ok, why = _brain_provider(m).available()
            except Exception as e:  # noqa: BLE001
                ok, why = False, f"{type(e).__name__}: {e}"
            if not ok:
                hidden.append({"id": m.id, "provider": m.provider, "why": str(why)[:180]})
                continue
            rows.append({
                **m.to_dict(),
                "available": bool(ok), "why": str(why)[:180], "fits": _fits(m, ok),
                "stream": False,
            })
    return rows, ollama_err, hidden


def _models_payload(force: bool = False) -> dict:
    global _MODELS_CACHE
    now = time.time()
    if not force and _MODELS_CACHE and now - _MODELS_CACHE[0] < MODELS_CACHE_TTL:
        return _MODELS_CACHE[1]
    rows, ollama_err, unavailable = _model_rows()
    default = DEFAULT_MODEL
    if not any(r["id"] == default and r["available"] for r in rows):
        # A local model that answers beats a cloud row whose key is merely set:
        # the first thing the composer offers should be the one you can talk to
        # with no network and no billing.
        pick = next((r for r in rows if r["available"] and r["kind"] == "local"
                     and r.get("fits", True) and r["tools"]), None) \
            or next((r for r in rows if r["available"] and r["kind"] == "local"), None) \
            or next((r for r in rows if r["available"] and r.get("fits", True) and r["tools"]), None) \
            or next((r for r in rows if r["available"]), None)
        if pick:
            default = pick["id"]
    payload = {
        "models": rows,
        "names": [r["id"] for r in rows],
        "default": default,
        "error": ollama_err or None,
        "unavailable": unavailable,
        "hint": (None if rows else
                 "no model can answer yet: start llama-server on 127.0.0.1:8080, "
                 "or paste a provider key in Integrations"),
        "ollama": OLLAMA_URL,
        "engine": {"budget": _ENGINE.budget.as_dict(), "host": _ENGINE.host.snapshot()} if _ENGINE else None,
    }
    _MODELS_CACHE = (now, payload)
    return payload


# ───────────────────────── projects (workspace area) ─────────────────────────
PROJECTS_ROOT = BASE / "projects"

# Small, real starters written verbatim into a new project; "empty" writes
# nothing, which is what a clone-from-GitHub project needs.
TEMPLATES: dict[str, dict] = {
    "empty": {"label": "Empty folder", "files": {}},
    "static": {"label": "Static site (HTML + CSS)", "files": {
        "index.html": "<!doctype html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"utf-8\">\n"
                      "  <title>__NAME__</title>\n</head>\n<body>\n  <h1>__NAME__</h1>\n</body>\n</html>\n",
        "style.css": "body { font: 16px/1.5 system-ui, sans-serif; margin: 3rem auto; max-width: 40rem; }\n",
    }},
    "python": {"label": "Python script", "files": {
        "app.py": "def main() -> None:\n    print(\"__NAME__ ready\")\n\n\nif __name__ == \"__main__\":\n    main()\n",
    }},
    "node": {"label": "Node script", "files": {
        "index.js": "console.log('__NAME__ ready');\n",
    }},
}

def _bind_workspace(path: Path) -> None:
    """Point the engine at another project without restarting it.

    Every path derived from WORKSPACE at import time is rebound here, the choice
    is saved for the next start, and the brain forgets the previous project's
    conversation — memory must not leak across projects.
    """
    global WORKSPACE, SESSION_FILE, SESSIONS_DIR, PROC_FILE, PROC_LOG_DIR, UPLOADS
    WORKSPACE = Path(path).resolve()
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    SESSION_FILE = WORKSPACE / ".locode-session.json"
    SESSIONS_DIR = WORKSPACE / ".locode-sessions"
    PROC_FILE = WORKSPACE / ".locode-processes.json"
    PROC_LOG_DIR = WORKSPACE / PROC_LOG_DIRNAME
    UPLOADS = WORKSPACE / "uploads"
    WORKSPACE_STATE.write_text(json.dumps({"workspace": str(WORKSPACE)}, indent=2))
    if _ENGINE is not None:
        _ENGINE.conversation.clear()
        _ENGINE.save()


def _inside_app(path: Path) -> bool:
    """Folder browsing and project creation stay inside this application dir."""
    return path == BASE or BASE in path.parents


@app.get("/api/workspace")
async def api_workspace():
    return {"workspace": str(WORKSPACE), "name": WORKSPACE.name,
            "parent": str(WORKSPACE.parent), "projects_root": str(PROJECTS_ROOT)}


@app.get("/api/folders")
async def api_folders(path: str = ""):
    """Directories under the app folder, for the New project folder picker."""
    # projects/ only appears with the first project, so an empty install browses
    # the app folder instead of 404-ing the picker
    root = Path(unquote(path)).resolve() if path else (PROJECTS_ROOT if PROJECTS_ROOT.is_dir() else BASE)
    if not _inside_app(root):
        raise HTTPException(400, "that folder is outside the app directory")
    if root == PROJECTS_ROOT and not root.is_dir():
        # the dialog opens here before the first project exists: browsing it is
        # the moment projects/ should appear, rather than a 404 in the picker
        root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise HTTPException(404, "no such folder")
    dirs = [{"name": d.name, "path": str(d)} for d in sorted(root.iterdir(), key=lambda d: d.name.lower())
            if d.is_dir() and not d.name.startswith(".") and d.name not in IGNORE_DIRS]
    return {"path": str(root), "parent": str(root.parent) if root != BASE else None, "dirs": dirs}


@app.get("/api/templates")
async def api_templates():
    return {"templates": [{"id": key, "label": val["label"], "files": sorted(val["files"])}
                          for key, val in TEMPLATES.items()]}


def _clone_into(url: str, dest: Path) -> tuple[bool, str]:
    """Shallow clone for a new project. Host allowlist, same as the agent tool."""
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        return False, "the repository must be an http(s) URL"
    host = (parsed.hostname or "").lower()
    if host not in CLONE_HOSTS:
        return False, (f"{host or 'that host'} is not an allowed clone host "
                       f"(LOCODE_CLONE_HOSTS: {', '.join(sorted(CLONE_HOSTS)) or 'none'})")
    try:
        proc = subprocess.run(["git", "clone", "--depth", "1", url, str(dest)],
                              capture_output=True, text=True, timeout=CMD_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"git clone timed out after {CMD_TIMEOUT}s"
    except OSError as exc:
        return False, f"git clone could not run: {exc}"
    if proc.returncode != 0:
        return False, "git clone failed: " + ((proc.stderr or proc.stdout or "").strip()[:300] or "unknown error")
    return True, "cloned"


@app.post("/api/project")
async def api_new_project(body: dict):
    """Create a project folder, fill or clone it, then switch the engine to it."""
    body = body or {}
    name = str(body.get("name") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
        raise HTTPException(400, "project name: 1-64 letters, digits, dot, dash or underscore")
    parent = Path(unquote(str(body.get("parent") or ""))).resolve() if body.get("parent") else PROJECTS_ROOT
    if not _inside_app(parent):
        raise HTTPException(400, "that folder is outside the app directory")
    dest = parent / name
    if dest.exists():
        raise HTTPException(409, f"{name} already exists in {parent.name}")
    template = str(body.get("template") or "empty")
    if template not in TEMPLATES:
        raise HTTPException(400, "unknown template")
    repo = str(body.get("repo") or "").strip()
    try:
        parent.mkdir(parents=True, exist_ok=True)
        if repo:
            ok, why = _clone_into(repo, dest)
            if not ok:
                shutil.rmtree(dest, ignore_errors=True)
                raise HTTPException(400, why)
        else:
            dest.mkdir(parents=True)
            for rel, text in TEMPLATES[template]["files"].items():
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text.replace("__NAME__", name))
    except HTTPException:
        raise
    except Exception as exc:                      # noqa: BLE001 - the folder is rolled back
        shutil.rmtree(dest, ignore_errors=True)
        raise HTTPException(500, f"could not create the project: {exc}")
    await _stop_all_processes("project switched")   # their cwd belongs to the old project
    _bind_workspace(dest)
    return {"ok": True, "workspace": str(dest), "name": dest.name, "template": template,
            "repo": repo or None,
            "files": sum(1 for q in dest.rglob("*") if q.is_file())}




# ───────────────────────── provider keys (Integrations) ─────────────────────────
KEYS_FILE = Path(os.getenv("LOCODE_KEYS", str(BASE / ".locode" / "keys.json")))
_KEY_NAMES: list[str] = []


def _key_names() -> list[str]:
    """Every api_key_env the catalog knows about — nothing else may be stored.

    Cached: resolving the catalog probes local servers, which /api/keys must not
    do on every panel open.
    """
    global _KEY_NAMES
    if not _KEY_NAMES and brain_catalog is not None:
        _KEY_NAMES = sorted({m.api_key_env for m in brain_catalog() if m.api_key_env})
    return _KEY_NAMES


def _read_keys() -> dict:
    try:
        data = json.loads(KEYS_FILE.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def _write_keys(keys: dict) -> None:
    KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = KEYS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(keys, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(KEYS_FILE)


def _refresh_models_cache() -> None:
    """A new key changes every availability answer; drop the 20s cache."""
    global _MODELS_CACHE
    _MODELS_CACHE = None


@app.get("/api/keys")
async def api_keys():
    """Which provider keys are configured — never the values themselves."""
    saved = _read_keys()
    rows = []
    for name in _key_names():
        env = (os.environ.get(name) or "").strip()
        rows.append({"name": name, "set": bool(env or saved.get(name)),
                     "source": "env" if env else ("saved" if saved.get(name) else "")})
    return {"keys": rows, "file": str(KEYS_FILE)}


@app.post("/api/keys")
async def api_set_key(body: dict):
    """Store or clear one provider key. An empty value clears it."""
    name = str((body or {}).get("name") or "").strip()
    if name not in _key_names():
        raise HTTPException(400, "unknown key name — the catalog decides which providers exist")
    raw = (body or {}).get("value")
    if raw is None:
        raise HTTPException(400, "pass {name, value}")
    value = str(raw).strip()
    if len(value) > 4096 or any(ch in value for ch in "\r\n\t "):
        raise HTTPException(400, "that does not look like an API key")
    keys = _read_keys()
    if value:
        keys[name] = value
    else:
        keys.pop(name, None)
    _write_keys(keys)
    _refresh_models_cache()
    env = (os.environ.get(name) or "").strip()
    return {"ok": True, "name": name, "set": bool(value),
            "source": "env" if env else ("saved" if value else "")}


# ───────────────────────── project + brain status ─────────────────────────
_FW_BY_DEP = {"react": "react", "vue": "vue", "svelte": "svelte", "@angular/core": "angular",
              "next": "next", "nuxt": "nuxt", "express": "express", "fastify": "fastify",
              "solid-js": "solid", "preact": "preact"}
_PY_FW = ("fastapi", "django", "flask", "starlette", "litestar", "streamlit", "pydantic")


def _project_info() -> dict:
    """What the header bar shows as pills: language stack, frameworks, dev command."""
    name, stack, frameworks, command = WORKSPACE.name or "workspace", [], [], ""
    try:
        present = {p.name: p for p in WORKSPACE.iterdir() if p.is_file()}
    except OSError:
        present = {}
    if "package.json" in present:
        try:
            pkg = json.loads(present["package.json"].read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pkg = {}
        name = pkg.get("name") or name
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        stack.append("node")
        for lib, tag in _FW_BY_DEP.items():
            if lib in deps and tag not in frameworks:
                frameworks.append(tag)
        for cfg in ("vite.config.ts", "vite.config.js", "vitest.config.ts", "tailwind.config.js"):
            if cfg in present and "vite" not in frameworks:
                frameworks.append("vite")
                break
        scripts = pkg.get("scripts") or {}
        for pref, cmd in (("dev", "npm run dev"), ("start", "npm start"), ("serve", "npm run serve")):
            if pref in scripts:
                command = cmd
                break
    for marker in ("requirements.txt", "pyproject.toml"):
        if marker not in present:
            continue
        if "python" not in stack:
            stack.append("python")
        text = present[marker].read_text(encoding="utf-8", errors="replace").lower()
        for fw in _PY_FW:
            if re.search(rf"\b{fw}\b", text) and fw not in frameworks:
                frameworks.append(fw)
        if not command:
            if "uvicorn" in text:
                entry = next((p.name for p in WORKSPACE.glob("*.py")
                              if "FastAPI(" in p.read_text(encoding="utf-8", errors="replace")[:4000]),
                             "main")
                command = f"uvicorn {entry.split('.')[0]}:app --reload --port 3000"
            elif "streamlit run" in text:
                command = "streamlit run app.py"
        break
    for marker, tag, cmd in (("go.mod", "go", "go run ."), ("Cargo.toml", "rust", "cargo run"),
                             ("pom.xml", "java", "mvn spring-boot:run"),
                             ("manage.py", "django", "python manage.py runserver")):
        if marker in present:
            if tag not in stack:
                stack.append(tag)
            if not command:
                command = cmd
    if any(k.startswith(("index.", "main.")) for k in present):
        for ext in ("html", "htm"):
            if f"index.{ext}" in present:
                if "html" not in stack:
                    stack.append("html")
                command = command or "python -m http.server 3000"
                break
    return {
        "name": name, "stack": stack or ["empty"], "frameworks": frameworks[:6],
        "dev_command": command, "files": sum(1 for _ in WORKSPACE.rglob("*") if _.is_file()),
    }


def _brain_status() -> dict:
    out = {"engine": bool(_ENGINE), "tier": "", "ram_gb": 0.0, "available": 0, "total": 0,
           "recommended": "", "error": _BRAIN_ERROR}
    if _ENGINE is None:
        return out
    b = _ENGINE.budget
    payload = _models_payload()
    out.update({
        "tier": b.tier, "ram_gb": round(_ENGINE.host.total_ram / 1e9, 1),
        "free_gb": round(_ENGINE.host.available_ram / 1e9, 1),
        "context_tokens": b.context_tokens,
        "available": sum(1 for r in payload["models"] if r["available"]),
        "total": len(payload["models"]),
        "recommended": payload["default"],
    })
    return out


def _network_label() -> str:
    return "off" if not NET_ENABLED or NETWORK_MODE == "off" else f"on ({NETWORK_MODE})"


def _same_origin(request: Request) -> None:
    """The approval gate is a security boundary: only this page may answer it.
    A cross-origin form post carries an Origin that does not match our Host."""
    raw = request.headers.get("origin") or request.headers.get("referer") or ""
    if not raw:
        return
    host = urlparse(raw).netloc
    if host and host != request.headers.get("host"):
        raise HTTPException(403, "cross-origin request refused")


# ───────────────────────── routes ─────────────────────────
@app.get("/api/models")
def api_models():
    return _models_payload()


# ── local model server (Integrations: start/stop without a shell) ─────────────
# The GGUF the composer lists comes from a llama.cpp server on llama_url(). That
# used to mean a terminal, three flags and a model path typed from memory. These
# are the same three things behind two buttons: the engine resolves the binary and
# the model, spawns the server as its own process group, records the pid, and stops
# only what it started. A server someone else launched is reported, never killed.
LLAMA_STATE = STATE_DIR / "llama.json"
LLAMA_LOG = STATE_DIR / "llama-server.log"
# LLAMA_MODELS may list several directories (comma or newline separated): models
# live wherever they were downloaded, and the panel shows all of them.
LLAMA_MODEL_DIRS = [Path(part).expanduser() for part in
                    re.split(r"[,\n]", os.getenv("LLAMA_MODELS", "") or "")
                    if part.strip()] or [BASE / "models"]
LLAMA_CTX = int(os.getenv("LLAMA_CTX", "4096"))
LLAMA_THREADS = int(os.getenv("LLAMA_THREADS", str(max(1, (os.cpu_count() or 2) // 2))))
LLAMA_BIN_HINT = os.getenv("LLAMA_BIN", "").strip()
LLAMA_MODEL_HINT = os.getenv("LLAMA_MODEL", "").strip()


class LocalModelStart(BaseModel):
    """Both optional: an empty field means the path the panel already shows."""
    bin: str = ""
    model: str = ""


def _llama_base() -> str:
    from brain.catalog import llama_url          # one place decides the URL
    return llama_url().rstrip("/")


def _llama_parts() -> tuple[str, int]:
    parsed = urlparse(_llama_base())
    return (parsed.hostname or "127.0.0.1"), int(parsed.port or 8080)


def _llama_bin() -> tuple[str, str]:
    """(path, where that path came from) — LLAMA_BIN, PATH, then models/."""
    on_path = shutil.which("llama-server") or ""
    for raw, source in ((LLAMA_BIN_HINT, "LLAMA_BIN"), (on_path, "PATH"),
                        (str(BASE / "models" / "llama-server"), "models/")):
        if raw and Path(raw).expanduser().is_file():
            return str(Path(raw).expanduser()), source
    return (LLAMA_BIN_HINT or on_path), (
        "LLAMA_BIN" if LLAMA_BIN_HINT else "PATH" if on_path else "missing")


def _llama_candidates() -> list[dict]:
    """Every GGUF the panel can offer, with a name and a size per row.

    A directory that does not exist is skipped silently — an unset second path is
    a normal state, and the panel already says which directories it scanned.
    """
    rows: list[dict] = []
    seen: set[str] = set()
    for directory in LLAMA_MODEL_DIRS:
        try:
            files = sorted(directory.glob("*.gguf"))
        except OSError:
            continue
        for path in files:
            if not path.is_file():
                continue
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            rows.append({"path": str(path), "name": path.name, "bytes": size,
                         "dir": str(directory)})
    return rows


def _llama_model(chosen: str = "") -> str:
    pick = (chosen or LLAMA_MODEL_HINT or "").strip()
    if pick:
        return str(Path(pick).expanduser())
    candidates = _llama_candidates()
    return candidates[0]["path"] if candidates else ""


def _llama_saved() -> dict:
    try:
        raw = json.loads(LLAMA_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


_LLAMA_PROC: subprocess.Popen | None = None


def _zombie(pid: int) -> bool:
    """An exited child nobody reaped still answers kill(0); it is not running."""
    try:
        fields = (Path("/proc") / str(pid) / "stat").read_text().split()
    except OSError:
        return False
    return len(fields) > 2 and fields[2] == "Z"


def _alive(pid: int) -> bool:
    """Is that pid still there? Signal 0 asks the kernel and touches nothing."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not _zombie(pid)


def _llama_log_tail(chars: int = 1200) -> str:
    try:
        return LLAMA_LOG.read_text(encoding="utf-8", errors="replace")[-chars:].strip()
    except OSError:
        return ""


def _local_model_status() -> dict:
    """What is answering, what the engine would start, and why it could not.

    `health` is a probe, not a memory: llama-server answers 503 while it loads and
    200 once it can serve, and an unreachable port is the honest "stopped".
    """
    base = _llama_base()
    saved = _llama_saved()
    pid = int(saved.get("pid") or 0)
    health, served = "unreachable", []
    try:
        with httpx.Client(timeout=1.5) as client:
            reply = client.get(base + "/health")
            health = "ready" if reply.status_code == 200 else "loading"
            if health == "ready":
                rows = client.get(base + "/v1/models").json().get("data") or []
                served = [str(row.get("id")) for row in rows if row.get("id")]
    except (httpx.HTTPError, OSError, ValueError, AttributeError):
        health = "unreachable"
    binary, source = _llama_bin()
    model = _llama_model(str(saved.get("model") or ""))
    candidates = _llama_candidates()
    return {
        "url": base, "running": health == "ready", "health": health, "served": served,
        "managed": _alive(pid), "pid": pid or None, "started": saved.get("started"),
        "bin": binary, "bin_source": source,
        "bin_exists": bool(binary) and Path(binary).is_file(),
        "model": model, "model_exists": bool(model) and Path(model).is_file(),
        "models": [row["path"] for row in candidates],
        "candidates": candidates,
        "model_dir": str(LLAMA_MODEL_DIRS[0]),
        "model_dirs": [str(d) for d in LLAMA_MODEL_DIRS],
        "log": _llama_log_tail(),
    }


@app.get("/api/local-model")
def api_local_model():
    return _local_model_status()


def _same_model(left: str, right: str) -> bool:
    """llama.cpp reports the GGUF path as the model id; a moved file keeps its name."""
    try:
        if Path(left).resolve() == Path(right).resolve():
            return True
    except OSError:
        pass
    return bool(left and right) and Path(left).name == Path(right).name


async def _stop_llama_server() -> dict:
    """Stop the server this engine spawned. Anything else is left alone."""
    global _LLAMA_PROC
    pid = int(_llama_saved().get("pid") or 0)
    if not pid:
        return {"ok": False, "error": "no local model server was started from this panel — "
                                     f"anything answering on {_llama_base()} belongs to another "
                                     "process and is left alone"}
    if _alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        deadline = time.time() + 10
        while time.time() < deadline and _alive(pid):
            await asyncio.sleep(0.2)
        if _alive(pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            await asyncio.sleep(0.4)
    proc = _LLAMA_PROC
    if proc is not None and proc.pid == pid:
        try:
            proc.wait(timeout=5)       # reap it: a zombie still answers kill(0)
        except subprocess.TimeoutExpired:
            pass
        _LLAMA_PROC = None
    LLAMA_STATE.unlink(missing_ok=True)
    if _alive(pid):
        return {"ok": False, "pid": pid, "error": f"pid {pid} is still running after SIGKILL"}
    return {"ok": True, "stopped": True, "pid": pid}


@app.post("/api/local-model/start")
async def api_local_model_start(req: LocalModelStart, request: Request):
    global _LLAMA_PROC, _MODELS_CACHE
    _same_origin(request)                    # starting a process is not a cross-site action
    status = await asyncio.to_thread(_local_model_status)
    host, port = _llama_parts()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return {"ok": False, **status,
                "error": f"{status['url']} is not a local endpoint — these controls only "
                         "manage a server on this machine"}
    binary, _source = _llama_bin()
    if not binary or not Path(binary).is_file():
        return {"ok": False, **status,
                "error": f"no llama-server binary at {binary or 'PATH'} — set LLAMA_BIN to "
                         "the path of the built binary"}
    model = _llama_model(req.model)
    if not model or not Path(model).is_file():
        where = " or ".join(str(d) for d in LLAMA_MODEL_DIRS) or "models/"
        return {"ok": False, **status,
                "error": f"no GGUF model at {model or where} — put one in {where} or "
                         "set LLAMA_MODEL"}
    switched = False
    if status["running"]:
        # llama.cpp loads one GGUF per process: the same model is a no-op, another
        # one replaces the server this engine started.
        if any(_same_model(serving, model) for serving in status.get("served") or []):
            return {"ok": True, "already": True, **status}
        if not status["managed"]:
            return {"ok": False, **status,
                    "error": f"{status['url']} is serving another model and this panel did not "
                             "start it — stop it where it runs, then start this one here"}
        stopped = await _stop_llama_server()
        if not stopped.get("ok"):
            return {"ok": False, **status,
                    "error": "could not stop the running server: " + str(stopped.get("error") or "")}
        switched = True
        status = await asyncio.to_thread(_local_model_status)
    command = [binary, "-m", model, "--host", host, "--port", str(port),
               "-c", str(LLAMA_CTX), "-t", str(LLAMA_THREADS)]
    try:
        LLAMA_LOG.parent.mkdir(parents=True, exist_ok=True)
        with LLAMA_LOG.open("ab") as log:
            log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(command)}\n".encode())
            log.flush()
            # start_new_session: the recorded pid leads a group, so stop reaches the
            # threads llama-server spawns rather than a wrapper that already forked.
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, cwd=str(BASE),
                                    start_new_session=True)
    except (OSError, ValueError) as e:
        return {"ok": False, **status, "error": f"could not start {binary}: {e}"}
    _LLAMA_PROC = proc                 # kept so stop can reap it instead of leaving a zombie
    LLAMA_STATE.write_text(json.dumps({
        "pid": proc.pid, "model": model, "bin": binary, "url": status["url"],
        "command": command, "started": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, indent=2), encoding="utf-8")
    await asyncio.sleep(1.2)          # a wrong binary fails now; a right one keeps loading
    if proc.poll() is not None:
        LLAMA_STATE.unlink(missing_ok=True)
        out = {"ok": False, "error": f"llama-server exited immediately (code {proc.returncode})"}
        out.update(await asyncio.to_thread(_local_model_status))
        out["log"] = _llama_log_tail()        # the reason is in those first lines
        return out
    _MODELS_CACHE = None              # the next /api/models must see the new server
    out = {"ok": True, "starting": True, "switched": switched}
    out.update(await asyncio.to_thread(_local_model_status))
    return out


@app.post("/api/local-model/stop")
async def api_local_model_stop(request: Request):
    global _MODELS_CACHE
    _same_origin(request)
    stopped = await _stop_llama_server()
    _MODELS_CACHE = None
    out = await asyncio.to_thread(_local_model_status)
    out.update(stopped)               # the pid that was stopped, or why it was not
    return out


@app.get("/api/health")
def api_health():
    """One call for the header bar: project, network gate, brain, sandbox."""
    return {
        "app": "LOCODE",
        "ok": True,
        "project": _project_info(),
        "network": _network_label(),
        "network_mode": NETWORK_MODE if NET_ENABLED else "off",
        "approval_timeout": APPROVAL_TIMEOUT,
        "workspace": str(WORKSPACE),
        "inbox": str(INBOX),
        "sandbox": {"mode": SANDBOX, "requested": SANDBOX_REQUESTED,
                    "docker": DOCKER_ENABLED,
                    "image": DOCKER_IMAGE if DOCKER_ENABLED
                             else SANDBOX_ROOTFS_URL.rsplit("/", 1)[-1],
                    "container": CONTAINER_NAME,
                    "rootfs": str(SANDBOX_ROOTFS) if SANDBOX == "namespace" else None},
        "brain": _brain_status(),
    }


# ───────────────────────── approvals (the gate) ─────────────────────────
class ApprovalResolve(BaseModel):
    id: str
    approved: bool
    remember: list[str] = []      # "domain" | "action" - never sent by import cards


@app.get("/api/approvals")
def api_approvals():
    now = int(time.time())
    for ap in list(_pending.values()):
        if ap["expires"] <= now and not ap["decision"]:
            ap["decision"] = "timeout"     # the waiting tool call picks this up
    return {
        "approvals": [{**ap, "remaining": max(0, int(ap["expires"] - now))}
                      for ap in reversed(list(_pending.values()))],
        "mode": NETWORK_MODE,
        "remembered": {"domains": sorted(_approved_domains), "actions": sorted(_approved_actions)},
    }


@app.post("/api/approvals/resolve")
def api_approvals_resolve(body: ApprovalResolve, request: Request):
    _same_origin(request)
    remember = [r for r in (body.remember or []) if r in ("domain", "action")]
    if body.id in _pending and _pending[body.id]["action"] == "import_file":
        remember = []                      # an import is always a fresh decision
    decision = _resolve_approval(body.id, body.approved, remember)
    if decision == "gone":
        raise HTTPException(404, "approval already answered or expired")
    return {"ok": True, "decision": decision,
            "remembered": {"domains": sorted(_approved_domains),
                           "actions": sorted(_approved_actions)}}


# ───────────────────────── quarantine inbox ─────────────────────────
class InboxDelete(BaseModel):
    name: str


@app.get("/api/inbox")
def api_inbox():
    return {"items": _inbox_items(), "path": str(INBOX)}


@app.post("/api/inbox/delete")
def api_inbox_delete(body: InboxDelete, request: Request):
    _same_origin(request)
    try:
        p = _inbox_dir(body.name)          # refuses traversal and absolute escapes
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not p.exists():
        raise HTTPException(404, f"no inbox item {body.name!r}")
    shutil.rmtree(p) if p.is_dir() else p.unlink()
    return {"ok": True, "removed": body.name}


# ───────────────────────── dev servers + preview proxy ─────────────────────────
class ServerStop(BaseModel):
    name: str


def _server_by_name(name: str) -> dict | None:
    for r in _server_rows():
        if name in (r.get("server_name"), r.get("name"), r["id"]):
            return r
    return None


@app.get("/api/servers")
async def api_servers():
    rows = []
    alive = await _pids_alive([r["pid"] for r in _server_rows() if r["pid"]])
    for r in _server_rows():
        running = r["pid"] in alive
        if not running and r.get("state") != "running":
            continue          # a dead server is not a preview: the header Stop goes away
        rows.append({"id": r["id"], "name": r.get("server_name") or r["name"],
                     "command": r.get("command", ""), "port": r.get("port") or 0,
                     "state": "running" if running else r.get("state", "exited"),
                     "url": f"/preview/{r.get('server_name') or r['name']}/" if r.get("port") else "",
                     "log": r.get("log", ""), "started": r.get("started", "")})
    return {"servers": rows}


@app.get("/api/servers/logs")
def api_server_logs(name: str, tail: int = 300):
    r = _server_by_name(name)
    if not r:
        raise HTTPException(404, f"no server named {name!r}")
    lines = _log_tail(r.get("log", ""), 1_000_000).splitlines()
    tail = max(1, min(int(tail or 300), 5000))
    return {"name": r.get("server_name") or r["name"], "log": r.get("log", ""),
            "lines": lines[-tail:], "total": len(lines)}


@app.post("/api/servers/stop")
async def api_servers_stop(body: ServerStop, request: Request):
    _same_origin(request)
    r = _server_by_name(body.name)
    if not r:
        return {"ok": False, "message": f"no server named {body.name!r}"}
    message = await _stop_process(r["id"])
    return {"ok": not message.startswith("ERROR"), "message": message}


_BASE_RE = re.compile(r"(<head[^>]*>)", re.I)


def _rewrite_html(body: bytes, prefix: str) -> bytes:
    """Give the iframe a base href so relative asset URLs resolve through the proxy."""
    text = body.decode("utf-8", errors="replace")
    if _BASE_RE.search(text):
        text = _BASE_RE.sub(rf'\1<base href="{prefix}/">', text, count=1)
    else:
        text = f'<head><base href="{prefix}/"></head>{text}'
    return text.encode("utf-8")


def _rewrite_location(loc: str, prefix: str, port: int) -> str:
    """Keep an upstream redirect inside the proxy instead of escaping to the port."""
    parsed = urlparse(loc)
    if parsed.netloc in (f"127.0.0.1:{port}", f"localhost:{port}"):
        return prefix + (parsed.path or "/") + (("?" + parsed.query) if parsed.query else "")
    if loc.startswith("/"):
        return prefix + loc
    return f"{prefix}/{loc}"


@app.get("/preview/{name}")
async def preview_root(name: str):
    return RedirectResponse(f"/preview/{name}/", status_code=307)


@app.api_route("/preview/{name}/{path:path}",
               methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def preview_proxy(name: str, path: str, request: Request):
    r = _server_by_name(name)
    if not r or not r.get("port"):
        raise HTTPException(404, f"{name} has no port yet - start it with start_server")
    if r.get("state") == "stopped" or r.get("ended"):
        raise HTTPException(502, f"{name} is not running any more")
    prefix = f"/preview/{name}"
    q = request.url.query
    target = f"http://127.0.0.1:{r['port']}/{path.lstrip('/')}" + (f"?{q}" if q else "")
    fwd = {k: v for k, v in request.headers.items()
           if k.lower() not in {"host", "connection", "accept-encoding", "content-length",
                                "origin", "referer"}}
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            up = await c.request(request.method, target, headers=fwd,
                                 content=await request.body(), follow_redirects=False)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502,
                            f"{name} on port {r['port']} did not answer: {type(e).__name__}: {e}")
    headers = {k: v for k, v in up.headers.items()
               if k.lower() not in {"transfer-encoding", "content-encoding", "content-length",
                                   "connection", "set-cookie", "location"}}
    body = up.content
    ct = up.headers.get("content-type", "")
    if "text/html" in ct and body:
        body = _rewrite_html(body, prefix)
    if up.headers.get("location"):
        headers["location"] = _rewrite_location(up.headers["location"], prefix, int(r["port"]))
    return Response(content=body, status_code=up.status_code, headers=headers,
                    media_type=ct or None)


# ───────────────────────── the agent loop ─────────────────────────
_SERVER_LINE = re.compile(r"^(\S+) is running on port (\d{2,5})")


@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    """One loop, two back ends: Ollama tags stream through /api/chat, every other
    catalog model runs on Freedom's provider adapters. Both emit the same events."""
    model = req.model or DEFAULT_MODEL
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + req.messages
    kind, provider = _turn_source(model, (req.base_url or "").strip())
    if kind == "unknown":
        async def unknown():
            yield sse({"type": "error",
                       "text": (f"{model} is not served by this engine: start it "
                                "(llama-server on 127.0.0.1:8080, ollama pull …) "
                                "or pick another model")})
        return StreamingResponse(unknown(), media_type="text/event-stream")
    max_tokens = min(2048, max(320, _ENGINE.budget.context_tokens // 2)) if _ENGINE else 1024
    if _ENGINE is not None:
        last_user = next((m.get("content") for m in reversed(req.messages)
                          if m.get("role") == "user"), "")
        if isinstance(last_user, str) and last_user.strip():
            _ENGINE.conversation.add("user", last_user[:8000])

    async def turn_ollama():
        parts: list[str] = []
        calls: list[dict] = []
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                        "POST", f"{OLLAMA_URL}/api/chat",
                        json={"model": model, "messages": messages,
                              "tools": TOOLS, "stream": True}) as r:
                    if r.status_code != 200:
                        body = (await r.aread()).decode(errors="replace")
                        yield {"type": "error", "text": f"Ollama {r.status_code}: {body[:400]}"}
                        return
                    async for line in r.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        msg = chunk.get("message") or {}
                        if msg.get("content"):
                            parts.append(msg["content"])
                            yield {"type": "token", "text": msg["content"]}
                        if msg.get("tool_calls"):
                            calls.extend(msg["tool_calls"])
                        if chunk.get("done"):
                            break
        except httpx.ConnectError:
            yield {"type": "error",
                   "text": f"Cannot reach Ollama at {OLLAMA_URL}. Is it running?"}
            return
        yield {"type": "turn", "content": "".join(parts), "tool_calls": calls}

    def _as_turn_calls(calls):
        return [{"function": {"name": c.get("name", ""), "arguments": c.get("args") or {}}}
                for c in (calls or [])]

    async def turn_provider():
        """Stream this model's tokens as they are produced.

        The adapter's stream() is sync (urllib), so it runs in a thread and every
        delta is handed to this coroutine through a queue: the browser paints the
        first token after one model step rather than after the whole answer.
        """
        parts: list[str] = []
        calls: list = []
        streamer = getattr(provider, "stream", None)
        if streamer is not None:
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue = asyncio.Queue()

            def pump() -> None:
                try:
                    for kind_, item in streamer(messages, tools=TOOLS, temperature=0.2,
                                                max_tokens=max_tokens):
                        loop.call_soon_threadsafe(queue.put_nowait, (kind_, item))
                except Exception as exc:            # noqa: BLE001 - reported below
                    loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, ("end", None))

            threading.Thread(target=pump, daemon=True).start()
            while True:
                kind_, item = await queue.get()
                if kind_ == "end":
                    break
                if kind_ == "error":
                    if not parts:
                        yield {"type": "error", "text": f"{model}: {item}"[:400]}
                        return
                    break
                if kind_ == "text":
                    parts.append(item)
                    yield {"type": "token", "text": item}
                elif kind_ == "calls":
                    calls = item
            text = "".join(parts)
            if not calls and text:
                calls = brain_providers.parse_tool_blocks(text)
            if text or calls:
                yield {"type": "turn", "content": text, "tool_calls": _as_turn_calls(calls)}
                return
        # adapters without a streaming path: one call, then the same events
        try:
            gen = await asyncio.to_thread(lambda: provider.generate(
                messages, tools=TOOLS, temperature=0.2, max_tokens=max_tokens))
        except Exception as e:  # noqa: BLE001 - a provider crash must not kill the stream
            yield {"type": "error", "text": f"{model}: {type(e).__name__}: {e}"[:400]}
            return
        if gen.error and not gen.tool_calls:
            yield {"type": "error", "text": gen.error[:400]}
            return
        if gen.text:
            yield {"type": "token", "text": gen.text}
        yield {"type": "turn", "content": gen.text or "",
               "tool_calls": _as_turn_calls(gen.tool_calls or [])}

    async def gen():
        call_id = 0
        turn = turn_ollama if kind == "ollama" else turn_provider
        for _step in range(MAX_STEPS):
            content, tool_calls = "", []
            async for ev in turn():
                if ev["type"] == "token":
                    yield sse(ev)
                elif ev["type"] == "error":
                    yield sse(ev)
                    return
                else:
                    content, tool_calls = ev["content"], ev["tool_calls"]

            if not tool_calls:
                if _ENGINE is not None and content:
                    _ENGINE.conversation.add("assistant", content[:8000])
                    _ENGINE.save()
                yield sse({"type": "done", "model": model})
                return

            if kind == "ollama":
                messages.append({"role": "assistant", "content": content,
                                 "tool_calls": tool_calls})
            else:
                messages.append({"role": "assistant", "content": content})

            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                raw = fn.get("arguments", {})
                if isinstance(raw, str):
                    try:
                        args = json.loads(raw)
                    except json.JSONDecodeError:
                        args = {}
                else:
                    args = raw or {}

                call_id += 1
                yield sse({"type": "tool_call", "id": call_id, "name": name, "args": args})

                result = await execute_tool(name, args)
                yield sse({"type": "tool_result", "id": call_id, "name": name,
                           "result": result[:6000]})

                if name in ("write_file", "edit_file") and args.get("path"):
                    yield sse({"type": "file_changed", "path": args["path"]})
                elif name in ("download_file", "clone_repo", "import_file") \
                        and not result.startswith("ERROR"):
                    yield sse({"type": "files_changed"})
                elif name == "start_server":
                    m = _SERVER_LINE.match(result)
                    if m:
                        yield sse({"type": "server_started", "name": m.group(1),
                                   "port": int(m.group(2)),
                                   "url": f"/preview/{m.group(1)}/"})

                messages.append({"role": "tool", "content": result})

        yield sse({"type": "done", "note": "max steps reached"})

    return StreamingResponse(gen(), media_type="text/event-stream")
