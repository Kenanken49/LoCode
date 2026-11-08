# LOCODE

A local-first coding agent: one FastAPI process that serves both the browser shell and
the model loop. LOCODE is the merge of two earlier builds — **LocalCode** (the workspace
agent: tools, SSE chat, process registry, optional Docker sandbox) and **Freedom** (the
"brain" layer: model catalog, RAM budgeting, provider adapters, session memory). The
backend lives in `server.py`; the frontend is a React + TypeScript app in `ui/` that Vite
builds to `ui/dist`, which FastAPI serves from the same origin — one process, one port,
no CORS, no separate dev server needed to run it.

```
locode/
├── server.py          # everything the server does: tools, gate, SSE, routes (~2.3k lines)
├── brain/             # Freedom's platform layer, renamed so it can never shadow stdlib `platform`
│   ├── catalog.py     #   model catalog + tier lookup
│   ├── memory.py      #   MemoryEngine: RAM probe, reserve, recommendation
│   ├── models.py      #   ModelInfo, replace()
│   └── providers.py   #   llama.cpp / Ollama / Anthropic / Gemini adapters
├── ui/                # the shell: React 19 + TypeScript (strict), no `any` in app code
│   ├── src/main.tsx   #   mount + the `window.setStatus` seam chunk 3 asks for
│   ├── src/App.tsx    #   chunk 2's grid: header / sidebar / column / approvals / inbox
│   ├── src/store.ts   #   external store (useSyncExternalStore) — no window.state, no redux
│   ├── src/api.ts     #   every /api call + the SSE reader
│   ├── src/markdown.tsx # chunk 5's renderer, returning elements instead of HTML strings
│   ├── src/components/*.tsx · src/styles/*.css
│   └── dist/          #   build output (git-ignored; the server mounts this)
├── requirements.txt   # fastapi, uvicorn, httpx
└── workspace/         # the agent's working directory (auto-created, git-ignored)
```

## Run it

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

cd ui && npm install && npm run build && cd ..   # → ui/dist
uvicorn server:app --host 127.0.0.1 --port 3000
# → http://127.0.0.1:3000  (serves ui/dist/index.html)
```

`ui/dist` is build output, so a clone without it still starts; `/` then renders a page that
prints the one command you need. For hacking on the shell, run `npm run dev` in `ui/` (Vite
proxies `/api` and `/preview` to :3000, so the same backend serves both).

`/api/models` answers with what can answer *right now*: a running `llama-server` has its
`/v1/models` discovered and each served GGUF becomes a selectable model (driven through the
OpenAI chat dialect, so the GGUF's own chat template applies), Ollama tags come from
`/api/tags` rather than the open port, and a provider's row appears only once its key is set.
Everything else comes back in `unavailable` with its reason (`missing API key (set
$GROQ_API_KEY)`, …) and is named in the picker's tooltip instead of being offered as a choice
that cannot run. The picker sits in the composer, next to the prompt it applies to; the `+`
beside it uploads files into `workspace/uploads/` and leaves their paths in the draft.

Cloud providers need a key, and it no longer has to be an environment variable. Paste it in
The same server can be started from the app. Integrations → **Local model server** shows what the
engine resolved — the binary from `LLAMA_BIN`, then `llama-server` on `PATH`, then `models/llama-server`;
the GGUFs from every directory in `LLAMA_MODELS` (comma separated), or `LLAMA_MODEL` — listed by name
and size, selectable, with `Start server` / `Stop` beside them. Choosing a different GGUF while one is running reloads the server with it, because llama.cpp serves one
model per process and the panel refuses to switch a server it did not start. Start spawns `${LLAMA_BIN:-llama-server} -m <gguf> --host
<host> --port <port> -c LLAMA_CTX -t LLAMA_THREADS` in its own process group and records the pid in
`.locode/llama.json`; the panel follows `/health` through `503` (loading) to `200` (ready), drops the
models cache so the GGUF appears in the picker without a restart, and prints the tail of
`.locode/llama-server.log` under the buttons. Stop signals that pid's group only: a server started
in a terminal is reported as running but never killed, because a stop button must not guess whose
process it is. A missing binary is named with its path rather than a generic failure.

Integrations → Provider keys: the engine writes `.locode/keys.json` (mode 600, git-ignored) and
`/api/keys` reports only *whether* a key is set and whether it came from the environment, never
the value. The models cache drops on save, so the provider's rows turn `ready` immediately. An
environment key still wins over a stored one.

Qwen3.8 Flash is one of those cloud rows, declared against Alibaba Model Studio's
OpenAI-compatible endpoint: `DASHSCOPE_API_KEY` — from the environment or pasted in
Integrations — makes `qwen3.8-flash` (1M context, text and image in, tool calls) selectable in
the same picker, and `FREEDOM_QWEN_URL` / `FREEDOM_QWEN_MODEL` move that row to another
compatible host or snapshot without editing code. Without the key the row is listed in
`unavailable` as `missing API key (set $DASHSCOPE_API_KEY)` instead of being offered.

Without any of those servers the catalog still lists every known model, and the engine
base URL (Settings) can point at any OpenAI-compatible endpoint (LM Studio, vLLM).

## Install it as a desktop app

One command puts LOCODE on the machine it lives on — no root, nothing system-wide:

```bash
cd locode && ./install.sh          # add --rebuild to rebuild the shell from ui/src
```

It creates `.venv` with the engine's three dependencies, builds `ui/dist` when it is missing,
installs the LC icon into `~/.local/share/icons`, writes an application entry
(`~/.local/share/applications/locode.desktop`) and copies the same shortcut to the Desktop,
marked trusted so GNOME will launch it. The shortcut runs `scripts/locode-run.sh`, which starts
the engine in the background only if `/api/health` is not already answering, then opens
`http://127.0.0.1:3000/`; clicking it again reuses the running engine instead of starting a
second one. `LOCODE_PORT=8080 scripts/locode-run.sh` moves the port and
`LOCODE_NO_BROWSER=1` starts the engine without opening a browser (server, SSH, tests).

A shortcut can only point at something that exists on that machine, so this installs LOCODE
where the project is checked out: the model still comes from a local llama.cpp server, Ollama,
or a provider key saved in Settings.

## Open it on your own computer

A hosted preview URL puts the hosting layer in front of this server: without the owner's session
it answers 403 before a request reaches the app. LOCODE itself has no login and no gate — it
binds the loopback interface and serves plain HTTP — so the direct way to use it is to run it
where you are:

```bash
cd locode
./install.sh              # venv, ui/dist and the LC shortcut (once)
scripts/locode-run.sh     # starts the engine, opens http://127.0.0.1:3000/
```

`scripts/locode-run.sh` reuses an engine that already answers `/api/health`, so running it again
just opens the tab; it binds `127.0.0.1` only, so nothing on the network reaches it, and
`LOCODE_PORT=8080` moves the port. `./install.sh` needs Python 3.9+ and, when `ui/dist` is
missing, npm to build the shell.

The model is whatever that machine has: Integrations → **Local model server** starts a llama.cpp
server (`LLAMA_BIN` for the binary, `LLAMA_MODELS` for the directories holding `*.gguf`), Ollama
on `:11434` is discovered as tags, and a provider key in Integrations reaches the cloud ones.
Nothing else has to be configured for the shell to open.

A machine can only run a checkout it has. If the app opens here in the preview panel but not on
your own computer, that copy of the code has to exist there too — the two commands above are all
it needs once it does.

## Windows

Both shells work on a Windows checkout; only the commands you type differ. What the machine
needs first: Python 3.9+ (the `py` launcher is what installs it), Node with npm (Vite builds the
shell), and a model of some kind — a llama.cpp server, Ollama for Windows, or a provider key.

**Git Bash** — the Unix path, unchanged:

```bash
cd locode
./install.sh                      # .venv (Scripts/python.exe) + ui/dist; no shortcut on Windows
scripts/locode-run.sh             # starts the engine, opens http://127.0.0.1:3000/
```

**PowerShell** — `install.sh` is a bash script, so this is the same work spelled out once:

```powershell
cd locode
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
cd ui
npm ci
node .\node_modules\typescript\bin\tsc --noEmit
node .\node_modules\vite\bin\vite.js build
cd ..
.\scripts\locode-run.ps1          # starts the engine, opens http://127.0.0.1:3000/
```

From then on `.\scripts\locode-run.ps1` is the launcher: it reuses an engine that already answers
`/api/health`, so a second run only opens the tab. `-Port 8080` moves the port and `-NoBrowser`
starts the engine without opening one (server, SSH, tests). In Git Bash the same two knobs are
`LOCODE_PORT=8080` and `LOCODE_NO_BROWSER=1`.

If PowerShell refuses the script ("running scripts is disabled on this system"), the execution
policy is a machine setting that this project does not change. Run it once as

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\locode-run.ps1
```

or allow just this file with `Unblock-File .\scripts\locode-run.ps1`.

**Line endings.** Windows installs usually set `core.autocrlf=true`, which rewrites `*.sh` to CRLF
on checkout; bash then fails on the first line with `$'\r': command not found`, and `install.sh`
looks broken when it is only mistranslated. `locode/.gitattributes` pins the scripts to LF, so a
fresh clone is already correct. A checkout made before that file existed needs one pass:

```bash
cd locode
git config core.autocrlf input     # stop converting on the way in
git checkout -- .                  # rewrite the working copies as LF
```

**Models on Windows.** llama.cpp publishes Windows builds: unzip a release and point `LLAMA_BIN` at
the `llama-server.exe` inside it, with `LLAMA_MODELS` naming the folders that hold your `*.gguf`
files. Both are read by Integrations → **Local model server**, which starts and stops the server
for you:

```bash
export LLAMA_BIN="C:/tools/llama/llama-server.exe"
export LLAMA_MODELS="C:/models"
```

Ollama for Windows listens on `127.0.0.1:11434` and is discovered as tags with nothing to
configure. A cloud row (Qwen3.8 Flash, Groq, …) needs only its key pasted in Integrations →
**Provider keys**, which writes `.locode/keys.json` and never logs the value.

The desktop-shortcut half of `install.sh` is Linux-only — Windows has no XDG app menu — so on
Windows the launcher scripts are the shortcut: pin `locode-run.ps1` to the taskbar or Start menu.

## The shell (chunk map)

| Chunk | React files | What it is |
|---|---|---|
| 1 | `styles/tokens.css` `styles/base.css` | every colour through `--var`, body 14px/1.55, styled scrollbars |
| 2 | `App.tsx` `styles/shell.css` | `main` grid `270px 1fr`, inner `section` flex column `min-height:0` |
| 3 | `components/Header.tsx` `styles/header.css` | `/api/health` → stack pills + dev command, ▶ Preview / ■ Stop, sandbox chip (click = restart), net pill |
| 4 | `components/Tree.tsx` `styles/tree.css` | `/api/files` → `▸` dirs (insert `path/`), `·` files (open the modal), 13px/level |
| 5 | `components/Thread.tsx` `markdown.tsx` `styles/chat.css` | user/assistant bubbles, rAF-batched token append, markdown, 900px thread |
| 6 | `components/Thread.tsx` `styles/tools.css` | tool cards with pulsing running badge, diff counts, error auto-expand, 280px body |
| 7 | `components/Approvals.tsx` `styles/approvals.css` | the consent gate — polls `/api/approvals` at 700ms, Allow/Deny → `/api/approvals/resolve` |
| 8 | `components/Inbox.tsx` `styles/inbox.css` | quarantine drawer over the chat: Import pre-fills the composer, Delete removes the item |
| 9 | `components/Modal.tsx` `styles/modal.css` | file preview: `GET /api/file` into one `<pre>` (pre-wrap), Esc / backdrop / Close |
| 10 | (same modal) `Modal.tsx` | server logs: `GET /api/servers/logs?name=app&tail=300`, autoscroll to the tail |
| 11 | `components/Preview.tsx` `styles/preview.css` | ▶ Preview runs `start_server`, the SSE port opens a 42vh pane on `/preview/app/`, Stop / Reload / Logs |
| 12 | `styles/responsive.css` | ≥1200 · 900–1199 · 600–899 · <600 bands, no horizontal scroll at any width |
| 13 | `components/Rail.tsx` `components/Panels.tsx` `styles/panels.css` | thin left rail — Home · Integrations · Settings; session/theme/refresh/base URL in Settings, provider reachability + inbox in Integrations |
| 14 | `components/Fusion.tsx` `FusionDoctor.tsx` `FusionExport.tsx` `report.ts` `pdf.ts` `styles/fusion.css` | `⌘K` Fusion command center — sidebar, omnibar over the shell's own actions, files and past sessions, a doctor trace and a hashed report export |
| 15 | `components/Changes.tsx` `styles/command.css` | changes drawer over `/api/git/status` + `/api/git/commit`, and one file's diff |

`⌘K` (or `Ctrl+K`) opens the **Fusion command center** — the shell reworked from the supplied
Stitch design (`DESIGN.md` plus its component suite): a 240px sidebar with the project, the engine
and the saved sessions, a hero with the view switcher, an omnibar over the same rows as before,
and a telemetry strip printing the branch, the changed count, the sandbox, the net mode and the
model from live state. Its rows are derived from the store — is the repository initialized, is a
turn running, is a server up — so an action that would not work is disabled rather than offered.
It also searches the file tree (`>` restricts it to commands) and opens a hit in the file modal.

Two things the export could not supply honestly are said in the UI instead of faked. It has a
Cloud/Local switcher, which means nothing in a local-only build, so the segmented control switches
panes (Console · Doctor · Export). Its doctor screen compares a local daemon with a cloud MicroVM,
so the split here is the browser that dispatches the checks and the engine that answers them,
with every latency measured rather than written in: seven real checks (`/api/health`, git, models,
sandbox, servers, sessions, files), the engine's own error printed for any that fails, and
`--strict` to stop at the first one. The export builds its report from live state (`report.ts`,
pure and tested) as Markdown or JSON, previews the exact bytes it will download, and signs them
with their SHA-256 — a real digest of a real file rather than an invented Ed25519 stamp. PDF is
the third format, written by `pdf.ts`: PDF 1.4 by hand — base-14 Courier and Helvetica-Bold,
A4 pages, paginated content streams and a cross-reference table of true byte offsets — laid out
from the same Markdown the runbook holds, so its digest covers the PDF's own bytes.

**Plan mode** is a chip beside the prompt: it
sends the next request with an instruction to investigate and propose instead of editing,
while the thread keeps the words you actually typed. The **Changes** drawer prints
`git status --short` line by line and commits through git, showing git's own output — which is
how "nothing to commit" arrives as a sentence rather than as a failed request. Clicking a row in
that drawer opens the file's own `git diff` (`GET /api/git/diff?path=`), so the change can be read
before it is committed; an untracked file is diffed against `/dev/null` instead of being called
unchanged, and the message field stays at the foot while the diff is open. LOCODE's own records
— `.locode-session.json` and `.locode-sessions/` — are written into `.git/info/exclude` when a
repository is initialized, read or committed against, so the transcript never shows up as a
change and `Commit all` cannot sweep it into the project's history; the exclude file is
per-checkout, so it stays out of the tree. When one of those records exists the drawer says so
below the list, naming the files and why they are missing — the absence is explained where it
would otherwise be noticed, rather than left to look like a bug.

Past conversations are rows in the same center. Each session is its own file under
`<workspace>/.locode-sessions/`; `.locode-session.json` stays the one that is *open* (the
shape it always had, plus its id), so nothing that read the old file broke. `GET
/api/sessions` lists them newest-first, titled with the first thing you said and how many
messages they hold; `⌘K` shows the six most recent and filters them as you type, with the
open one listed disabled. Choosing another one saves the thread you are leaving, calls
`POST /api/sessions/open` — which also clears the brain's memory, so two conversations cannot
bleed into each other — and restores its transcript. **New session** now archives the open
conversation instead of overwriting it, and a save names its session (`{id, messages}`) so an
empty thread is never mistaken for "start a new one".

`api.ts` wires the loop: `sendPrompt()` POSTs to `/api/chat` with a raw `fetch` (the
response is an SSE stream, not JSON) and dispatches each frame into `store.ts`. The store is
a plain external store read with `useSyncExternalStore`, so streaming tokens go through the
same rAF batcher the spec describes — it just mutates store state instead of a live DOM node.
State types are in `types.ts`; nothing in `src/` uses `any` or a non-null assertion.

## Network consent (the part that decides whether you trust it)

`LOCODE_NETWORK` picks the mode; `LOCODE_NET=0` is a hard kill switch for all web tools.

| Mode | Behaviour |
|---|---|
| `off` | web tools are refused outright |
| `ask` | **every** web tool call raises an approval card |
| `auto-safe` (default) | `web_search` / `web_fetch` run free; anything that touches the disk — `download_file`, `clone_repo`, `import_file` — raises a card |

A card offers **Allow once** / **Deny**, plus "remember this **domain**" and "remember this
**action**" checkboxes. `import_file` deliberately has **no remember option**: bytes already
sitting in the inbox still have to earn their way into the workspace, one explicit click at
a time (the server strips `remember` for that action regardless of what the client sends).
Cards expire after `LOCODE_APPROVAL_TIMEOUT` seconds (default 300) and a denied or expired
card returns the reason to the model, so it can take a different route.

Downloads and clones never land in the project. They go to `LOCODE_INBOX`
(default `./inbox/<timestamp>-<slug>/`) with a `SOURCE.txt` note, and the agent must call
`import_file` — which is the second, separate approval — to move them in.

`/api/approvals/resolve`, `/api/inbox/delete` and `/api/servers/stop` reject cross-origin
requests, so a page other than this shell cannot answer the gate. The guard runs before the
approval queue is consulted for anything but `web_search`/`web_fetch`, and even after an
Allow, `import_file` and the fetch path re-check the resolved host — `169.254.169.254`,
`localhost` and RFC1918 addresses are refused with the card still on screen.

In `ask` mode consent comes first, so a link-local or loopback URL does raise a card that
cannot succeed (Allow → `refused … blocked by design`). That is deliberate: the gate answers
"should the agent reach out at all", the SSRF guard answers "is this address ever allowed",
and the second one is not negotiable by clicking.

## Servers and preview

`start_server` (or the ▶ Preview button, which sends the detected dev command as a prompt)
registers a process flagged `server: True`. The port is scraped from its log, with a
`/proc` socket fallback, and announced on the stream as `server_started`. The shell then
opens an iframe on `/preview/<name>/`, a same-origin httpx proxy that rewrites redirects and
injects a `<base href>` so assets resolve. `sandbox="allow-scripts allow-same-origin
allow-forms"`; Close hides the pane but leaves the server running (the header keeps the
`app:5173` chip and ■ Stop until you stop it for real).

## Sandboxes: docker, or namespaces with no daemon

Either backend confines `run_command` / `start_process` / `start_server` — one shared code
path — with the workspace bind-mounted, so an `npm install` cannot touch the host. Files
still live on the host tree that `read_file` / `write_file` / `edit_file` change.

`SANDBOX` defaults to `auto`: a Docker daemon if one answers, else the built-in namespace
runtime, else the host. That is what makes the app confined on a machine with no daemon and
no configuration — nothing to install, no image to pull. Only `auto` probes; an explicit
backend is never swapped behind your back, so `SANDBOX=docker` without a daemon still refuses
rather than falling back to the host.

- `SANDBOX=docker` (the older `DOCKER_ENABLED=1` still means this): `docker run` /
  `docker exec` against `DOCKER_IMAGE` (default `node:20-bookworm`), with `DOCKER_MEMORY` /
  `DOCKER_CPUS` enforced by cgroups. Needs a reachable daemon.
- `SANDBOX=namespace` (what `auto` picks when no daemon answers): `unshare(1)` + `chroot`
  over a mini rootfs (`SANDBOX_ROOTFS`, fetched
  once from `SANDBOX_ROOTFS_URL` — a 3 MB Alpine tarball) with no daemon, no root and no
  images. That tarball is busybox only: `sh`, `awk`, `sed`, `tar`, `grep` and friends work,
  `node`/`python` do not. Point `SANDBOX_ROOTFS_URL` at a rootfs that carries your toolchain
  (any x86_64 distro rootfs tarball works) when the project needs one — the workspace is
  mounted the same way either way. A one-shot command gets a private mount table, a private pid namespace and
  uts/ipc namespaces, `--net` when `SANDBOX_NETWORK=none`, a clean environment (no host
  variables, so no keys leak in) and the workspace at `/workspace`. A long-running command
  (`start_process`, dev servers) is started as a background `unshare` on the host instead, so
  the pid the registry records is a real host pid, its log lands where the host can read it,
  and `--kill-child` means stopping it takes the whole container with it. Limits are enforced
  without cgroups: `SANDBOX_MEMORY` becomes `RLIMIT_AS` (`ulimit -v` in the sandbox) and
  `SANDBOX_CPUS` becomes CPU affinity (`taskset -c 0..N-1`), both kernel-enforced and inherited
  by everything the command starts. They are not identical to docker's caps — address space
  rather than RSS, a core count rather than a quota — so `/api/docker/status` reports
  `limits: "rlimit+affinity"` (docker reports `"cgroups"`) rather than claiming equality.
- `SANDBOX=off`: commands run on the host, which is why the consent gate exists. Set it
explicitly to opt out of confinement.

Both answer through `/api/health` (`sandbox.mode`) and `/api/docker/status|restart`; the
route names predate the second backend and are kept for existing clients. The header's
`sandbox:` chip reads the latter: the backend name (`off` / `docker` / `namespace`), the
numbers it actually caps (`2g/2c` = memory / CPU cores), and `✓` when those caps are
enforced against `✕` when they are not, with the mechanism (`cgroups` or
`rlimit+affinity`) in its tooltip. A daemon that is configured but down reads `✕`, not `✓`.
The chip re-reads the status every 8 seconds while the page is visible and once when the tab
regains focus, so a restart from the terminal (or the server returning on a different backend)
shows up without pressing `⟳`. It is also the restart control: clicking it posts
`/api/docker/restart` and prints the outcome (`recreated …`, or `failed: …` in the error colour)
in the chip for six seconds, which is why it is a button and disabled when `SANDBOX=off`. With the sandbox
on, a failure comes back as `ERROR (sandbox): …` and the command does **not** run on the host.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `WORKSPACE` | `./workspace` | the project directory the agent edits |
| `OLLAMA_URL` | `http://localhost:11434` | local inference endpoint |
| `LLAMA_BIN` / `LLAMA_MODEL` | — | llama-server binary and GGUF the Integrations panel starts |
| `LLAMA_MODELS` | `./models` | directories scanned for `*.gguf` (comma separated) |
| `LLAMA_CTX` / `LLAMA_THREADS` | 4096 / half the CPUs | context and thread count passed to llama-server |
| `MODEL` | `qwen2.5-coder:7b` | fallback model id |
| `MAX_STEPS` / `CMD_TIMEOUT` | 25 / 180 | tool-loop budget per turn / command timeout |
| `LOCODE_NETWORK` | `auto-safe` | consent mode (`off` \| `ask` \| `auto-safe`) |
| `LOCODE_NET` | `1` | `0` disables every web tool |
| `LOCODE_APPROVAL_TIMEOUT` | `300` | seconds before a card expires |
| `LOCODE_INBOX` | `./inbox` | quarantine root |
| `LOCODE_SEARCH` / `LOCODE_SEARX_URL` | `duckduckgo` | search backend |
| `LOCODE_BLOCKED_HOSTS` / `LOCODE_CLONE_HOSTS` | — | host allow/deny lists |
| `MAX_DOWNLOAD_BYTES` | 50 MiB | download cap |
| `LOCODE_RESERVE_GB` / `LOCODE_PREFER_LOCAL` | 0 / 1 | brain RAM reserve, local-first ranking |
| `SANDBOX` | `auto` | `auto` \| `docker` \| `namespace` \| `off`; `auto` = daemon → namespace → host |
| `SANDBOX_ROOTFS` / `SANDBOX_ROOTFS_URL` | `./.sandbox-rootfs` / Alpine minirootfs | namespace rootfs and where to fetch it |
| `SANDBOX_NETWORK` | `DOCKER_NETWORK` | `none` gives the namespace sandbox an empty network |
| `SANDBOX_MEMORY` / `SANDBOX_CPUS` | `DOCKER_MEMORY` / `DOCKER_CPUS` | namespace limits: `ulimit -v` address space and CPU affinity |
| `DOCKER_*` | see above | docker backend: image, limits, name, workdir |

## HTTP surface

`GET /` · `/api/health` · `/api/models` · `/api/files` · `/api/file` · `/api/approvals` ·
`/api/approvals/resolve` · `/api/inbox` · `/api/inbox/delete` · `/api/servers` ·
`/api/servers/logs` · `/api/servers/stop` · `/api/processes` · `/api/git/status|init|commit|diff` ·
`/api/local-model` · `/api/local-model/start|stop` ·
`/api/session` · `/api/sessions` · `/api/sessions/open` · `/api/reset` ·
`/api/docker/status|restart` · `/preview/{name}/{path}` ·
`POST /api/chat` (SSE: `token` `tool_call` `tool_result` `file_changed` `files_changed`
`server_started` `error` `done`).

`POST /api/reset` is **destructive**: it stops tracked processes and clears the workspace
except `.git` and the session file. Nothing in the shell calls it — see the note below.

## Verification

`server.py` and `brain/` are covered by two throwaway suites (consent gate, inbox, servers,
preview proxy, provider plumbing) — 45 + 28 assertions, all passing.

The check that ships with the repository is `tests/test_git_and_plan.py`, and it needs no
model, key or network: it copies the engine into a temporary app directory (so its own state
dir and `projects/` are not shared), starts it on a temporary git repository with an empty
`HOME` — so it has no git identity to fall back on — and points `/api/chat` at a fake
OpenAI-compatible endpoint that records the request it was actually sent. 25 checks, and one
command runs them beside the build:

```bash
./check.sh                                        # build + node tests + the engine suite
```

`check.sh` runs every step even when one fails, so a single run reports everything that is broken,
and exits non-zero if anything did. Individually:

```bash
cd locode && python3 tests/test_git_and_plan.py   # git endpoints + the plan-mode path
cd ui && npm test                                 # the request the shell builds for plan mode
```

The git half walks one repository through its life (status before init → `git init` plus a
first commit with no identity anywhere → one modified, deleted, renamed and untracked file →
their diffs → a commit that records its message → an empty diff after it) and asserts that
`~/.gitconfig` was never created. The plan half spans both halves of the app: `ui/test/plan.test.ts`
checks `planRequest()` (prefix on the last user message only, earlier turns untouched, the
thread keeps the words typed), and the Python suite reads `PLAN_PREFIX` out of `ui/src/plan.ts`
with Node, posts it to `/api/chat` against the fake provider, and asserts the instruction
arrives verbatim — with a control case where it must not appear at all.

A third engine, on its own workspace, covers the transcript history: a conversation written
before the feature existed is adopted (id assigned, history file written, row titled from its
first line), a save re-titles it with whitespace collapsed, a long first line is cut at 60
characters plus an ellipsis, New session archives the open one instead of overwriting it and
does not pile up empty sessions, opening a session restores its transcript and makes it
current, a save that names its session stays in it, unknown/relative/absolute ids are refused
with 404/400, and the directory ends up holding exactly one file per listed session while
`.locode-session.json` names the open one. The shell was then
driven in a real browser against the live server, one iframe per band at 1440 / 1280 / 1000
/ 760 / 500 / 360px, with no horizontal scroll and no header overlap at any width:

- **1–2** every colour resolves from `tokens.css`; body computes 14px/1.55; scrollbars styled.
- **3** a Python workspace renders `[python] [fastapi] · uvicorn app:app --reload --port 3000`;
  `net: on (ask)`; 15 models in the select; below 1200px the select + base URL move into the
  ⚙ popover; typing a base URL and blurring writes `localStorage['locode-base-url']` and the
  shell re-probes through it (pointing it at a wrong port flips the status to `Failed to fetch`,
  then back to `engine: local`). New clears the thread **and** the engine's memory
  (`/api/session` → 0 messages) while all four workspace files stay on disk.
- **4** 13px per level measured at depths 1–3 (8 / 21 / 34px); `▸ src/` inserts into the
  composer; `(empty workspace)` on an empty root.
- **5–6** markdown produced `<h2>` / `<ul>` / `<pre data-lang>`; a 6-card turn kept the running
  badge pulsing and auto-expanded only the errored card, whose body scrolled inside 280px.
- **7** Allow once, Allow + remember-domain, Allow + remember-action (second same-domain fetch
  raised **no** card), `import_file` with zero checkboxes, Deny → `denied by developer` in the
  tool card, two concurrent cards stacked newest-on-top inside the viewport, expiry at
  `LOCODE_APPROVAL_TIMEOUT=10` → both streams errored `no approval … nothing was fetched`, and
  `169.254.169.254` still blocked after Allow.
- **8** the drawer opens over the thread with zero reflow (`#messages` width and composer top
  unchanged), Import pre-fills `Use import_file with inbox_name="…"`, Delete cancel keeps the
  item, confirm empties it.
- **9–10** a 5,000-line / 278,892-char file painted in the modal in 652ms; logs reuse the same
  shell and autoscroll to the tail.
- **11** the pane is 42vh, the iframe loads `/preview/app/` through the proxy, Stop kills the
  server and Close leaves it running (the header chip + ■ Stop persist either way).

The band matrix was re-measured after the rail landed, in this VM's browser, one same-origin
iframe per band at 360 / 500 / 760 / 1000 / 1280 / 1440px, waiting for `/api/models` to fill the
composer first: zero document overflow and zero header overflow at every width, the rail 44px
below 600 and 52px above it, the model picker inside `#composer` at every band, and no settings
button left in the header.

### Browser checks (the hosted Playwright bridge cannot run here)

The hosted bridge strips the script's TypeScript with `mode: "transform"`, and this VM's Node 26
only accepts `mode: "strip"`, so it fails with `The property 'options.mode' must be one of:
'strip'. Received 'transform'` before a page is even opened — no script can work around it.
`scripts/` carries a local replacement that needs no root:

```bash
bash scripts/pw-setup.sh                 # chromium + its 3 missing libs + a font database
source /tmp/locode-playwright/env.sh     # CHROME, LD_LIBRARY_PATH, FONTCONFIG_FILE, NODE_PATH
node scripts/playwright.mjs scripts/ui-smoke.mjs http://127.0.0.1:3000 artifacts
```

`pw-setup.sh` installs `playwright-core` and a Chromium that bundles its own libraries, then
unpacks the three it still needs (`libnspr4`, `libnss3`, and DejaVu for fontconfig) from Ubuntu's
main archive into `/tmp/locode-playwright/sysroot`; nothing is installed system-wide. Without a
font database Skia aborts with `SkFontMgr_FontConfigInterface ... Not implemented`.
`playwright.mjs` accepts dialogs the way a developer pressing OK would, prints the script's return
value as JSON and exits non-zero when it throws. `ui-smoke.mjs` covers the tabs, New chat, the
whole New project dialog, and the Fusion command center: the plan chip (its pressed state, the
placeholder it swaps, and the tip line that has to agree with it), Ctrl+K with the caret already
in the input, `>` narrowing the list to commands, a one-row filter whose ⏎ opens the Changes
drawer, then the drawer itself — branch, summary, a modified file's diff with its added and
removed lines, the untracked file diffing from `/dev/null`, `‹ all` back to the list, and closing
it — and finally the other two panes: the doctor has to reach an `EXIT` line with its seven real
check tags, and the export has to show a 64-hex digest, drop the transcript from the preview when
that section is unchecked, and turn into JSON when the bundle is picked, then download a PDF that starts `%PDF-1.4`, ends
`%%EOF` and hashes to the digest on screen. The drawer needs a git
workspace with changes to show anything, so point that run at one.

The run creates `projects/pw-demo` and switches the engine there, so delete that folder and
`.locode/workspace.json` afterwards.

The rail, the panels and the local model were then driven against the live engine:

- **Discovery, not guesswork.** The `llama-server` on `127.0.0.1:8080` reported one model;
  `/api/models` lists that id as `openai-compat · ready`, makes it the **default**, and
  `POST /api/chat` streamed the model's own text (`Hello! How can I assist you today?` — one
  `token` frame, then `done`). The request goes to the server's `/v1/chat/completions`, so the
  GGUF's chat template applies instead of the old `<|user|>` prompt guesswork.
- **Honest unavailability.** The Ollama on this box is a stub: it answers *any* model name with
  canned text (`{"model": "mock", "content": "Nothing more to do."}`), and every catalog
  Ollama tag used to read `ready` because the port answered. Rows now come from `/api/tags`
  (nothing pulled → no rows), and an id the catalog does not know errors instead of being
  handed to that server, so its canned reply can never surface as a model's answer.
- **Upload.** The composer's `+` posted a 42-byte file to `/api/upload?name=ui-upload.txt`; it
  landed in `workspace/uploads/ui-upload.txt`, the draft became
  `[uploaded: uploads/ui-upload.txt]`, the status read `uploaded 1 file`, and the file tree
  showed `uploads/`.
- **Provider keys.** Pasting a Groq key in Integrations stored it (`.locode/keys.json`, `-rw-------`),
  cleared the input, offered Clear, and flipped `llama-3.3-70b-versatile` from `missing API key`
  to `ready` without a restart; `/api/keys` returned it as `set: true, source: saved` and never
  contained the value. Clear returned the row to `not set`. `OPENAI_API_KEY` stayed `from
  environment` with no Clear button, and an unknown name was refused with 400.
- **Rail and panels.** Home / Integrations / Settings at 44px (below 600) and 52px wide, with the
  rail still clickable while a panel is open. Four layout bugs were found and fixed this way: a
  full-width panel offset past the right edge, its left edge clipped under the rail, long provider
  reasons widening the panel off screen, and the sandbox chip printing literal `\n`.
- **Session history.** `GET /api/session` adopted the transcript that predated history (8 messages,
  titled from its first user line) into `.locode-sessions/`, and `GET /api/sessions` listed it as
  the open one. A New session (`POST /api/session {messages: []}`) archived it and took a new empty
  id; `⌘K` then showed both — the open one disabled as `open now`, the archived one as
  `8 msgs · just now` — and clicking it restored all 8 bubbles verbatim with the status
  `session: Write a Python function add(a, b) with a docstring.`. Typing `add` filtered the list to
  that one row, the arrow keys walked all 16 rows with the selected one kept in view, and switching
  back to the empty session cleared the thread. Refusals, over HTTP on a throwaway workspace:
  `../../etc/passwd` → 400, an unknown id → 404, a non-list transcript → 400, and a save that names
  its session stayed in that session instead of creating another one.
- **Per-file diffs.** Against a scratch repository holding one file of each kind (modified, deleted,
  renamed, untracked), the drawer read `master` and `1 modified · 1 deleted · 1 renamed · 1
  untracked`; a row opened `modified.txt` with `@@ -1,2 +1,3 @@`, `-line one` in the removed colour
  and `+line one changed` / `+line three` in the added one, with the commit field still at the foot
  and no horizontal scroll. The untracked file opened as an addition from `/dev/null`, the renamed
  row asked for the name the file has now and git answered `rename from renamed-src.txt` / `rename
  to renamed-dst.txt`, `removed.txt` showed `-bye`, and `‹ all` returned to the list. Refused over
  HTTP: `../../etc/passwd` and `/etc/passwd` → 400, a missing `path` → 400; an unchanged file
  answers an empty diff, which the drawer prints as "git reports no difference in this file".

Two paths that used to be stubbed are now exercised for real:

- **Container isolation.** This VM has no reachable Docker daemon, and it cannot be made
  reachable: the socket is root-only, `sudo` is blocked by `no_new_privs`, there is no user
  systemd manager (`/run/user/<uid>` does not exist), every cgroup is root-owned, and
  `dockerd` 29.1.3 segfaults on startup inside an unprivileged user namespace (`NewDaemon` →
  `Shutdown` nil dereference) whether or not cgroups are involved. That is why the container
  path was exercised through a CLI stand-in — and why the namespace backend exists, since it
  needs no daemon at all and is verified natively above. `runc` and user namespaces *do* work, so a
  `docker`-shaped CLI over `unshare` + `chroot` (kept in `/tmp`, not shipped) was put on `PATH`
  and LOCODE's own `docker_start_container()` / `_run_command()` drove it: hostname
  `locode-sandbox`, `id -u` → 0, `/etc/alpine-release` → 3.20.3, a container `/` with no host
  directories, `/workspace` mounted from the host, and a file written inside the container
  appearing in the workspace on the host. Run against the genuine unreachable daemon with
  `DOCKER_ENABLED=1`, `_run_command` answers `ERROR (docker): docker pull … permission denied`
  and the command **never** executes on the host — the marker file it would have written was
  absent. The failure mode is refuse, not silently fall back. `--memory` / `--cpus` are the one
  part that cannot be honoured here, because cgroup delegation is unavailable.
- **The namespace backend, with no daemon at all.** `SANDBOX=namespace` unpacked the Alpine
  minirootfs on first use (0.4 s, 3.4 MB) and then `run_command` reported hostname
  `locode-sandbox`, `id -u` → 0, Alpine 3.20.3, `/proc` holding 3 pids, `/etc/hostname` from the
  rootfs instead of the host's, the workspace at `/workspace` with a write landing in the host
  tree, and `secret=[none]` for a variable exported only outside the sandbox. A long-running
  command started with `start_process` stayed `running` in the registry under a real host pid,
  echoed `boot-inside` / `locode-sandbox` into a log the host could read, was listed as
  `1 running / 1 tracked in the namespace sandbox`, and `stop_process` took it down (host pid
  gone). With `unshare` removed from
  `PATH` the same call answered `ERROR (sandbox): namespace sandbox unavailable: unshare is not
  on PATH` and left no file on the host, the host mount table stayed clean (mounts are private
  to each command), the docker backend answers through the same contract, and with `SANDBOX=off`
  commands run on the host as before. The limits were measured, not assumed: with
  `SANDBOX_MEMORY=64m` a single 64 MB allocation (`busybox dd bs=64M`) is refused with
  `dd: out of memory` (also through `/api/exec`) while the same command succeeds at the 2g
  default; streaming copies still run under the cap, because `RLIMIT_AS` bounds address space,
  not bytes moved. `/proc/self/status` reports `Cpus_allowed_list: 0` for `SANDBOX_CPUS=1`
  against `0-1` for 2, and both hold for `start_process` children as well.
- **A genuinely model-driven turn.** `llama.cpp` was built from source in this VM and a real
  Qwen2.5-Coder-0.5B GGUF served by `llama-server` on :8080. With `FREEDOM_LLAMA_URL` and
  `FREEDOM_LLAMA_MODEL` set, `/api/models` lists it as `llama.cpp · available`, and
  `POST /api/chat` performed real inference: 368 prompt tokens at 122 tok/s and 112 generated
  tokens at 34 tok/s according to the server's own timing log, arriving as one `token` frame
  with the model's text (6.3s end to end) plus a `done` frame. The prose is a 0.5B model's own
  guess — that is the point: nothing scripted it.

The mutating-tool suites (approvals, inbox, downloads) still use a scripted provider, which only
chooses the tool call; the gate, the URL guard, the fetch and the SSE frames around it are real.

## Deviations and rough edges in the spec

Written down rather than silently patched:

1. **Chunk 3 says `New` → `POST /api/reset`.** That endpoint is LocalCode's workspace wipe
   (it needs `{confirm:"yes"}` and deletes every file in `WORKSPACE` except `.git` and the
   session). New now calls `POST /api/session {messages:[]}` and the server clears the memory
   engine on an empty transcript. A chat-thread button must not be able to delete your project;
   the endpoint still exists for scripts.
2. **The model select is in the composer, not the header.** Chunk 3's mockup puts it in the top
   bar and chunk 12 then spends a band rule hiding it. A model applies to a prompt, so it now
   sits with the prompt; the header keeps project context and the two runtime readouts, and the
   rest of the old header controls (new session, theme, refresh, base URL) live in the rail's
   Settings panel. Provider reachability moved to Integrations.
3. **Chunk 12 has no rule for the project bar overflowing inside a band.** A 36-char dev command
   plus pills plus actions did not fit 1280px while the model and base-URL fields were inline.
   With those gone the bar has room; `header.css` keeps the contract explicit anyway: pills are
   `flex:none` and the dev command shrinks first with an ellipsis (full text in its `title`).
4. **Chunk 3's `+N` pill vs chunk 12's `…`.** `.pill.extra` (the `+2` count) shows at ≥1200px,
   `.pill.more` (the `…`) below 900px; they were colliding in the 900–1199 band, so `.few` is now
   hidden there explicitly.
5. **Chunk 3's `open-inbox` event is not a React thing.** The Inbox button sets `inbox.open` in the
   store instead of dispatching a `CustomEvent`; chunk 8's drawer reads the store. Same behaviour,
   different mechanism, and nothing outside the app can dispatch it any more.
6. **File names in every chunk header are `static/*.css` / `static/*.js`.** They are
   `ui/src/styles/*.css` and `ui/src/components/*.tsx`; the chunk map above is the crosswalk.
7. **`tool_result` carries no `ok` flag.** A read of a missing path comes back as a result string
   like `(no such file: x)`, and chunk 6's card can only show a green `ok` badge for it. Making the
   badge honest means adding a status field to the SSE frame, which is a server change, not a shell one.
8. **Chunk 7's mockup text is illustrative.** It draws `FETCH URL` and "up to 5,242,880 bytes"; the
   live card names the actual tool (`web_fetch`) and prints the configured `MAX_FETCH_BYTES`
   (3,000,000 by default), because a consent card that misstates the cap is worse than one that
   differs from the picture.
9. **Ask-mode consent fires before the SSRF guard**, so a loopback or link-local URL raises a card
   that cannot be honoured. It is still blocked server-side; the ordering is the price of asking
   permission rather than silently refusing.

## Only real models, and why it is faster

`/api/models` used to mix what the machine *advertises* with what it can actually run: an
`offline-orchestrator` that could not code, four LFM rows for a project that is not checked
out beside locode, two Ollama tags nobody had pulled, and a "start llama-server" placeholder.
All of those are gone. The picker now lists only models that answer, and everything else is
reported as `unavailable` with the reason — a key, a download or a server.

The same pass made the replies stream:

- **Real token streaming.** `OpenAICompat.stream` reads the endpoint's SSE and assembles
  tool-call deltas, and the server bridges it to the browser through a queue. Measured on the
  local 0.5B GGUF: 62 token frames over 2.19s with the first at 0.46s, where it used to be one
  2.2s block at the end. Anthropic and Gemini stream too: `Anthropic.stream` assembles its
  `input_json_delta` tool arguments, `Gemini.stream` splits the part frames, and both emit the
  same `token` / `calls` events the bridge already handles. The composer measures the
  arriving deltas over a 1.5 s window and shows `12.4 tok/s` next to the status while a reply
  streams, then keeps that rate under the model that produced it (`last 12.4 tok/s`) so two
  models can be compared without a stopwatch. The status word "idle" is gone: the dot next to
  the composer pulses while a turn or a tool is running and holds still when nothing is,
  so the composer never claims to be busy at rest or at rest while working.
- **No invented answers.** An id the catalog does not know is served only if Ollama has that
  tag pulled; otherwise the turn errors. This matters here because the Ollama on this box is a
  stub that replies to any model name with canned text.
- **Compression and caching.** gzip takes the bundle from 264 KB to 81 KB; `/assets/*` is
  `immutable` (Vite hashes the names) and `index.html` is `no-cache`, so a rebuild is picked up
  on the next load without re-downloading what did not change.
- **Measured afterwards, loopback:** `/api/health` 1.4ms, `/api/models` 1.9ms, `/api/files`
  1.8ms, `/api/workspace` 1.1ms, `/api/templates` 1.0ms, engine start to first 200 655ms.
