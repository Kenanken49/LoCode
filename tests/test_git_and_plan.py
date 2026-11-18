"""Automated check: the git endpoints, the session history and the plan-mode path.

    cd locode && python3 tests/test_git_and_plan.py

Needs no model, no key and no network. Every engine runs on a throwaway workspace
inside a temporary copy of this app, with an empty HOME so it has no git identity
of its own; the plan checks add a fake OpenAI-compatible endpoint that records
what it was actually sent.

The plan path spans both halves of the app, so both are checked: the shell's
request builder by `ui/test/plan.test.ts` (Node), and the server side here — the
prefix read out of `ui/src/plan.ts` is posted to /api/chat and must arrive at the
provider verbatim, ahead of the words the developer typed.

Git methods are numbered because they are a sequence: they walk one repository
through its life (init, edits, commit) instead of rebuilding it per case. The
session class gets its own engine and workspace, because history files would show
up as untracked changes and disturb that repository's status.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCODE = HERE.parent
UI = LOCODE / "ui"
PLAN_TS = UI / "src" / "plan.ts"
VENV_PY = LOCODE / ".venv" / "bin" / "python"
PYTHON = str(VENV_PY if VENV_PY.exists() else Path(sys.executable))
NODE = shutil.which("node") or "node"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _open(url: str, body: dict | None = None, method: str = "GET",
          timeout: float = 60.0) -> tuple[int, bytes]:
    # `method` is explicit because POST /api/git/init takes no body: inferring POST
    # from the body sent it as a GET, which answers 405 and hides the whole suite.
    data = None
    if method == "POST":
        data = json.dumps(body if body is not None else {}).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Engine:
    """One engine process on a throwaway workspace, under a temporary copy of the app.

    The copy matters: the state dir and `projects/` live next to `server.py`, and a
    check must not write into the LOCODE that is running right now.
    """

    def __init__(self, root: Path, files: dict[str, str] | None = None,
                 extra_env: dict[str, str] | None = None) -> None:
        self.root = root
        self.app = root / "app"
        self.workspace = root / "repo"
        self.home = root / "home"
        for directory in (self.app, self.workspace, self.home):
            directory.mkdir(parents=True, exist_ok=True)
        shutil.copy2(LOCODE / "server.py", self.app / "server.py")
        shutil.copytree(LOCODE / "brain", self.app / "brain",
                        ignore=shutil.ignore_patterns("__pycache__"))
        for name, text in (files or {}).items():
            path = self.workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

        self.port = free_port()
        self.base = "http://127.0.0.1:%d" % self.port
        env = dict(os.environ)
        env.update({
            "WORKSPACE": str(self.workspace),
            "HOME": str(self.home),          # no ~/.gitconfig: no identity to fall back on
            "LOCODE_NETWORK": "off",
            "OLLAMA_URL": "http://127.0.0.1:%d" % free_port(),   # nothing listens there
            "PYTHONUNBUFFERED": "1",
        })
        env.update(extra_env or {})       # a check that needs its own binary or URL
        self.log_path = root / "engine.log"
        self.log = self.log_path.open("wb")
        self.proc = subprocess.Popen(
            [PYTHON, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=str(self.app), env=env, stdout=self.log, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(self.base + "/api/health", timeout=2):
                    return
            except OSError:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.3)
        self.stop()
        raise RuntimeError("engine did not answer:\n" + self.log_path.read_text()[-3000:])

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()

    def get(self, path: str, timeout: float = 60.0) -> tuple[int, dict]:
        status, raw = _open(self.base + path, None, "GET", timeout)
        return status, json.loads(raw or b"{}")

    def post(self, path: str, body: dict | None = None, timeout: float = 60.0) -> tuple[int, dict]:
        status, raw = _open(self.base + path, body, "POST", timeout)
        return status, json.loads(raw or b"{}")

    def post_raw(self, path: str, body: dict, timeout: float = 60.0) -> tuple[int, bytes]:
        return _open(self.base + path, body, "POST", timeout)


class FakeProvider(BaseHTTPRequestHandler):
    """An OpenAI-compatible endpoint that records the request and streams two deltas."""

    requests: list[dict] = []
    deltas = ("Plan: ", "1. add tests/")

    def do_POST(self) -> None:               # noqa: N802 - the name http.server calls
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        type(self).requests.append({"path": self.path, "body": body})
        frames = ['data: ' + json.dumps({"choices": [{"delta": {"content": d}}]})
                  for d in type(self).deltas]
        payload = ("\n\n".join(frames) + "\n\ndata: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:    # keep the test output readable
        pass


class GitAndPlan(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="locode-check-"))
        cls.engine = Engine(cls.tmp, files={
            "keep.txt": "unchanged\n",
            "modified.txt": "line one\nline two\n",
            "gone.txt": "bye\n",
        })
        cls.ws = cls.engine.workspace
        cls.home = cls.engine.home

        cls.fake_port = free_port()
        cls.fake = ThreadingHTTPServer(("127.0.0.1", cls.fake_port), FakeProvider)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        cls.fake_url = "http://127.0.0.1:%d" % cls.fake_port

        read = subprocess.run(
            [NODE, "--input-type=module", "-e",
             "import(%s).then((m) => process.stdout.write(m.PLAN_PREFIX))"
             % json.dumps(PLAN_TS.as_uri())],
            capture_output=True, text=True, timeout=120,
        )
        if read.returncode != 0 or not read.stdout.strip():
            raise RuntimeError("could not read PLAN_PREFIX from ui/src/plan.ts:\n"
                               + read.stderr[-2000:])
        cls.plan_prefix = read.stdout

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.stop()
        cls.fake.shutdown()
        cls.fake.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ── helpers ────────────────────────────────────────────────────────────
    def api(self, path: str, body: dict | None = None, method: str = "GET") -> tuple[int, dict]:
        return self.engine.get(path) if method == "GET" else self.engine.post(path, body)

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=str(self.ws), capture_output=True,
                              text=True, check=True).stdout

    def diff(self, path: str) -> dict:
        status, body = self.api("/api/git/diff?path=" + urllib.parse.quote(path))
        self.assertEqual(status, 200, body)
        return body

    # ── the shell's half of the plan path ──────────────────────────────────
    def test_00_the_shell_builds_the_plan_request(self) -> None:
        run = subprocess.run([NODE, "--test", str(UI / "test" / "plan.test.ts")],
                             cwd=str(UI), capture_output=True, text=True, timeout=180)
        self.assertEqual(run.returncode, 0, (run.stdout + run.stderr)[-3000:])

    # ── git, in the order a repository actually changes ────────────────────
    def test_10_status_before_init(self) -> None:
        status, body = self.api("/api/git/status")
        self.assertEqual(status, 200)
        self.assertIs(body.get("initialized"), False, body)

    def test_11_init_commits_without_a_git_identity(self) -> None:
        status, body = self.api("/api/git/init", method="POST")
        self.assertEqual(status, 200)
        self.assertIs(body.get("ok"), True, body)
        self.assertIn("initial commit", body.get("output") or "", body)

    def test_12_the_engine_did_not_write_a_global_identity(self) -> None:
        # the `-c user.name/-c user.email` overrides are per invocation, which is the
        # whole point: the developer's ~/.gitconfig is never created or edited
        self.assertFalse((self.home / ".gitconfig").exists())
        self.assertFalse((self.ws / ".git" / "config").read_text().count("user.email"))

    def test_13_status_is_clean_and_named(self) -> None:
        status, body = self.api("/api/git/status")
        self.assertEqual(status, 200)
        self.assertIs(body.get("initialized"), True, body)
        self.assertTrue(body.get("branch"), body)
        self.assertEqual((body.get("status") or "").strip(), "", body)

    def test_14_status_reports_one_of_each_kind(self) -> None:
        (self.ws / "modified.txt").write_text("line one changed\nline two\nline three\n")
        (self.ws / "gone.txt").unlink()
        subprocess.run(["git", "mv", "keep.txt", "renamed.txt"], cwd=str(self.ws), check=True)
        (self.ws / "new.txt").write_text("hello from an untracked file\n")
        status, body = self.api("/api/git/status")
        text = body.get("status") or ""
        self.assertEqual(status, 200)
        for expected in ("M modified.txt", "D gone.txt", "R  keep.txt -> renamed.txt", "?? new.txt"):
            self.assertIn(expected, text, text)

    def test_14b_session_files_are_not_changes(self) -> None:
        # the transcript lives beside the project; it is the engine's record, not the
        # developer's work, so it must not appear in the drawer as a change
        (self.ws / ".locode-sessions").mkdir(exist_ok=True)
        (self.ws / ".locode-sessions" / "abc123abc123.json").write_text('{"messages": []}\n')
        (self.ws / ".locode-session.json").write_text('{"id": "abc123abc123", "messages": []}\n')
        status, body = self.api("/api/git/status")
        text = body.get("status") or ""
        self.assertEqual(status, 200)
        self.assertNotIn(".locode-session", text, text)
        self.assertNotIn(".locode-sessions", text, text)
        self.assertIn("M modified.txt", text, text)      # real changes are still listed
        # the drawer is told what is being hidden, so the absence is explained
        self.assertEqual(sorted(body.get("excluded") or []),
                         [".locode-session.json", ".locode-sessions/"], body)

    def test_15_diff_of_a_modified_file(self) -> None:
        body = self.diff("modified.txt")
        text = body.get("diff") or ""
        self.assertIn("-line one", text)
        self.assertIn("+line one changed", text)
        self.assertIn("+line three", text)
        self.assertIs(body.get("untracked"), False, body)
        self.assertEqual(body.get("path"), "modified.txt")

    def test_16_diff_of_an_untracked_file_starts_at_dev_null(self) -> None:
        body = self.diff("new.txt")
        text = body.get("diff") or ""
        self.assertIs(body.get("untracked"), True, body)
        self.assertIn("+hello from an untracked file", text)
        self.assertIn("--- /dev/null", text)

    def test_17_diff_of_a_renamed_file_uses_both_names(self) -> None:
        body = self.diff("renamed.txt")
        text = body.get("diff") or ""
        self.assertIn("rename from keep.txt", text)
        self.assertIn("rename to renamed.txt", text)

    def test_18_diff_of_a_deleted_file_shows_the_removal(self) -> None:
        body = self.diff("gone.txt")
        text = body.get("diff") or ""
        self.assertIn("-bye", text)
        self.assertIn("+++ /dev/null", text)

    def test_19_diff_refuses_paths_outside_the_workspace(self) -> None:
        for path in ("../../etc/passwd", "/etc/passwd", "..%2F..%2Fetc%2Fpasswd"):
            status, _ = self.api("/api/git/diff?path=" + path)
            self.assertEqual(status, 400, path)
        status, _ = self.api("/api/git/diff")
        self.assertEqual(status, 400)

    def test_20_commit_records_the_message_and_clears_status(self) -> None:
        status, body = self.api("/api/git/commit", {"message": "LOCODE: automated check"},
                                method="POST")
        self.assertEqual(status, 200)
        self.assertIn("LOCODE: automated check", body.get("output") or "", body)
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(), "LOCODE: automated check")
        _, after = self.api("/api/git/status")
        self.assertEqual((after.get("status") or "").strip(), "", after)

    def test_21_diff_after_commit_is_empty(self) -> None:
        self.assertEqual(self.diff("modified.txt").get("diff"), "")

    def test_22_commit_all_did_not_sweep_the_history_in(self) -> None:
        tracked = self.git("ls-tree", "-r", "--name-only", "HEAD")
        self.assertNotIn(".locode-session", tracked)
        # excluded, not deleted: the engine's own records are still on disk
        self.assertTrue((self.ws / ".locode-session.json").is_file())
        _, body = self.api("/api/git/status")
        self.assertEqual((body.get("status") or "").strip(), "", body)

    # ── the server's half of the plan path ─────────────────────────────────
    def chat(self, messages: list[dict]) -> list[dict]:
        FakeProvider.requests.clear()
        status, raw = self.engine.post_raw("/api/chat", {
            "messages": messages, "model": "plan-check-model", "base_url": self.fake_url,
        })
        self.assertEqual(status, 200)
        return [json.loads(line[5:].strip()) for line in raw.decode().splitlines()
                if line.startswith("data:")]

    def test_30_a_plain_request_reaches_the_model_as_typed(self) -> None:
        events = self.chat([{"role": "user", "content": "Add a tests directory"}])
        sent = FakeProvider.requests[-1]
        self.assertTrue(sent["path"].endswith("/chat/completions"), sent["path"])
        self.assertEqual(sent["body"]["messages"][0]["role"], "system")
        self.assertEqual(sent["body"]["messages"][-1]["content"], "Add a tests directory")
        self.assertEqual([e.get("text") for e in events if e.get("type") == "token"],
                         ["Plan: ", "1. add tests/"])
        self.assertTrue(any(e.get("type") == "done" and e.get("model") == "plan-check-model"
                            for e in events), events)

    def test_31_the_plan_prefix_reaches_the_model_verbatim(self) -> None:
        typed = "Add a tests directory"
        events = self.chat([{"role": "user", "content": self.plan_prefix + typed}])
        messages = FakeProvider.requests[-1]["body"]["messages"]
        last = messages[-1]
        self.assertEqual(last["role"], "user")
        self.assertTrue(last["content"].startswith(self.plan_prefix), last["content"][:120])
        self.assertEqual(last["content"], self.plan_prefix + typed)
        # the instruction must survive the hop: same words, not a summary
        self.assertIn("do not write or edit", last["content"])
        self.assertTrue(any(e.get("type") == "token" for e in events), events)
        self.assertTrue(any(e.get("type") == "done" for e in events), events)

    def test_32_earlier_turns_are_not_prefixed(self) -> None:
        thread = [
            {"role": "user", "content": "what is in the workspace?"},
            {"role": "assistant", "content": "Three files."},
            {"role": "user", "content": self.plan_prefix + "add a tests directory"},
        ]
        self.chat(thread)
        sent = FakeProvider.requests[-1]["body"]["messages"]
        self.assertEqual(sent[1]["content"], "what is in the workspace?")
        self.assertEqual(sent[2]["content"], "Three files.")
        self.assertEqual(sum(1 for m in sent if m["content"].startswith("Plan mode:")), 1)


class SessionHistory(unittest.TestCase):
    """Transcript history: one file per conversation, and switching between them."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="locode-sessions-"))
        # a transcript written before this feature existed: messages, no id
        cls.engine = Engine(cls.tmp, files={
            ".locode-session.json": json.dumps({"messages": [
                {"role": "user", "content": "legacy conversation"},
                {"role": "assistant", "content": "kept"},
            ]}),
        })
        cls.ws = cls.engine.workspace

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path: str) -> dict:
        status, body = self.engine.get(path)
        self.assertEqual(status, 200, body)
        return body

    def post(self, path: str, body: dict | None = None) -> dict:
        status, answer = self.engine.post(path, body)
        self.assertEqual(status, 200, answer)
        return answer

    def ids(self) -> list[str]:
        return [row["id"] for row in self.get("/api/sessions")["sessions"]]

    def test_40_a_transcript_from_before_history_is_adopted(self) -> None:
        session = self.get("/api/session")
        self.assertEqual([m["content"] for m in session["messages"]],
                         ["legacy conversation", "kept"], session)
        self.assertRegex(session["id"], r"^[0-9a-f]{12}$")
        listed = self.get("/api/sessions")
        self.assertEqual(listed["current"], session["id"])
        self.assertEqual(len(listed["sessions"]), 1, listed)
        row = listed["sessions"][0]
        self.assertEqual(row["id"], session["id"])
        self.assertEqual(row["title"], "legacy conversation")
        self.assertEqual(row["messages"], 2)
        # history is a real file beside the transcript, not just an API view
        self.assertTrue((self.ws / ".locode-sessions" / (session["id"] + ".json")).is_file())

    def test_41_a_save_titles_the_conversation_with_its_first_line(self) -> None:
        first = "  Summarise   the\nuploads folder for the release  "
        session = self.post("/api/session", {"messages": [
            {"role": "user", "content": first},
            {"role": "assistant", "content": "two files"},
            {"role": "user", "content": "and the sizes"},
        ]})
        row = self.get("/api/sessions")["sessions"][0]
        self.assertEqual(row["id"], session["id"])
        self.assertEqual(row["title"], "Summarise the uploads folder for the release")
        self.assertNotIn("\n", row["title"])
        self.assertEqual(row["messages"], 3)

    def test_42_a_long_first_line_is_cut_with_an_ellipsis(self) -> None:
        long_line = ("please plan a release checklist for the uploads feature including"
                     " sizes and owners")
        self.post("/api/session", {"messages": [{"role": "user", "content": long_line}]})
        title = self.get("/api/sessions")["sessions"][0]["title"]
        self.assertEqual(len(title), 61, title)          # 60 characters plus the ellipsis
        self.assertTrue(title.endswith("…"), title)
        self.assertTrue(long_line.startswith(title[:-1]), title)

    def test_43_new_session_archives_the_open_one(self) -> None:
        before = self.get("/api/sessions")
        archived = next(r for r in before["sessions"] if r["id"] == before["current"])
        fresh = self.post("/api/session", {"messages": []})
        self.assertNotEqual(fresh["id"], archived["id"])
        after = self.get("/api/sessions")
        self.assertEqual(after["current"], fresh["id"])
        self.assertEqual(sorted(self.ids()), sorted([archived["id"], fresh["id"]]))
        kept = next(r for r in after["sessions"] if r["id"] == archived["id"])
        self.assertEqual((kept["title"], kept["messages"]), (archived["title"], archived["messages"]))
        empty = next(r for r in after["sessions"] if r["id"] == fresh["id"])
        self.assertEqual((empty["title"], empty["messages"]), ("(empty session)", 0))

    def test_44_new_session_on_an_empty_session_does_not_pile_up(self) -> None:
        current = self.get("/api/sessions")["current"]
        again = self.post("/api/session", {"messages": []})
        self.assertEqual(again["id"], current)
        self.assertEqual(len(self.ids()), 2)

    def test_45_opening_a_session_restores_its_transcript(self) -> None:
        listed = self.get("/api/sessions")
        archived = next(r for r in listed["sessions"] if r["id"] != listed["current"])
        opened = self.post("/api/sessions/open", {"id": archived["id"]})
        self.assertEqual(opened["id"], archived["id"])
        self.assertEqual(len(opened["messages"]), archived["messages"])
        first = opened["messages"][0]
        self.assertEqual(first["role"], "user")
        self.assertTrue(first["content"].startswith(archived["title"][:-1]), first["content"])
        current = self.get("/api/session")
        self.assertEqual(current["id"], archived["id"])
        self.assertEqual(current["messages"], opened["messages"])
        self.assertEqual(self.get("/api/sessions")["current"], archived["id"])

    def test_46_a_save_that_names_its_session_stays_in_it(self) -> None:
        current = self.get("/api/session")
        saved = self.post("/api/session", {"id": current["id"], "messages": current["messages"]})
        self.assertEqual(saved["id"], current["id"])
        self.assertEqual(len(self.ids()), 2)
        self.assertEqual(self.get("/api/sessions")["current"], current["id"])
        self.assertEqual(self.get("/api/session")["messages"], current["messages"])

    def test_47_bad_session_requests_are_refused(self) -> None:
        status, _ = self.engine.post("/api/sessions/open", {"id": "deadbeefdead"})
        self.assertEqual(status, 404)
        for bad in ("../../etc/passwd", "/etc/passwd", "", "not-a-session-id"):
            status, _ = self.engine.post("/api/sessions/open", {"id": bad})
            self.assertEqual(status, 400, bad)
        status, _ = self.engine.post("/api/session", {"messages": "not a list"})
        self.assertEqual(status, 400)
        status, _ = self.engine.post("/api/session", {"id": "../x", "messages": []})
        self.assertEqual(status, 400)

    def test_48_the_history_is_one_file_per_session(self) -> None:
        listed = self.get("/api/sessions")
        on_disk = sorted(p.name for p in (self.ws / ".locode-sessions").glob("*.json"))
        self.assertEqual(on_disk, sorted(row["id"] + ".json" for row in listed["sessions"]))
        opened = json.loads((self.ws / ".locode-session.json").read_text())
        self.assertEqual(opened.get("id"), listed["current"])
        self.assertEqual(len(opened.get("messages") or []), 1)


FAKE_LLAMA = '''#!/usr/bin/env python3
"""A llama-server stand-in: the two endpoints LOCODE probes and nothing else, so
the start/stop path is checked without weights, a build or a GPU."""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8080
MODEL = sys.argv[sys.argv.index("-m") + 1] if "-m" in sys.argv else "/models/fake-qwen.gguf"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body, code = b'{"status":"ok"}', 200
        elif self.path == "/v1/models":
            body, code = json.dumps({"data": [{"id": MODEL}]}).encode(), 200
        else:
            body, code = b"{}", 404
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
'''

# Windows cannot exec a script that starts with a shebang ([WinError 193]), so the
# same body is launched through a .cmd shim there and LLAMA_BIN points at the shim.
# %~dp0 is the shim's own directory; %* passes -m and the flags through untouched.
FAKE_LLAMA_SHIM = '@echo off\r\n"%s" "%%~dp0fake-llama-server.py" %%*\r\n'


def pid_alive(pid: int) -> bool:
    """Is that pid still there? os.kill(pid, 0) cannot answer it on Windows."""
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)    # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259            # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class LocalModelControl(unittest.TestCase):
    """Integrations' start/stop: the engine spawns, discovers and stops the server.

    Methods are numbered because they are one lifecycle: resolve, start, ask again,
    stop. The last two use their own engines, because "no binary" and "nothing of
    mine to stop" are states this workspace must not reach in the middle of the run.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="locode-llama-"))
        # The engine spawns LLAMA_BIN as a program, so the stand-in has to be one:
        # the script itself where the kernel honours its shebang, and a .cmd shim
        # around the very same script on Windows, which has no shebang to honour.
        (cls.tmp / "fake-llama-server.py").write_text(FAKE_LLAMA, encoding="utf-8")
        if os.name == "nt":
            binary = cls.tmp / "fake-llama-server.cmd"
            binary.write_text(FAKE_LLAMA_SHIM % PYTHON, encoding="utf-8", newline="")
        else:
            binary = cls.tmp / "fake-llama-server"
            binary.write_text(FAKE_LLAMA, encoding="utf-8")
            binary.chmod(0o755)
        models = cls.tmp / "models"
        models.mkdir()
        cls.gguf = models / "fake-qwen.gguf"
        cls.gguf.write_bytes(b"GGUF")
        # a second directory proves LLAMA_MODELS takes a list, not one path
        cls.other_dir = cls.tmp / "more-models"
        cls.other_dir.mkdir()
        cls.other = cls.other_dir / "other-qwen.gguf"
        cls.other.write_bytes(b"GGUF" * 512)
        cls.binary = binary
        cls.port = free_port()
        cls.engine = Engine(cls.tmp / "root", extra_env={
            "LLAMA_BIN": str(binary),
            "LLAMA_MODELS": str(models) + "," + str(cls.other_dir),
            "LLAMA_MODEL": str(cls.gguf),
            "FREEDOM_LLAMA_URLS": "http://127.0.0.1:%d" % cls.port,
        })

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.engine.post("/api/local-model/stop")
        except OSError:
            pass
        cls.engine.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def state(self) -> dict:
        status, body = self.engine.get("/api/local-model")
        self.assertEqual(status, 200, body)
        return body

    def wait_ready(self, seconds: float = 20.0) -> dict:
        deadline = time.time() + seconds
        state = self.state()
        while time.time() < deadline and not state["running"]:
            time.sleep(0.3)
            state = self.state()
        return state

    def test_50_status_reports_what_it_found_and_what_it_did_not(self) -> None:
        state = self.state()
        self.assertEqual(state["url"], "http://127.0.0.1:%d" % self.port)
        self.assertFalse(state["running"], state)
        self.assertEqual(state["health"], "unreachable")
        self.assertFalse(state["managed"], state)
        self.assertIsNone(state["pid"])
        self.assertEqual(state["bin"], str(self.binary))
        self.assertEqual(state["bin_source"], "LLAMA_BIN")
        self.assertTrue(state["bin_exists"])
        self.assertEqual(state["model"], str(self.gguf))
        self.assertTrue(state["model_exists"])
        self.assertIn(str(self.gguf), state["models"])

    def test_51_start_spawns_the_server_this_engine_owns(self) -> None:
        status, answer = self.engine.post("/api/local-model/start", {})
        self.assertEqual(status, 200, answer)
        self.assertTrue(answer["ok"], answer)
        self.assertTrue(answer["starting"], answer)
        self.assertIsInstance(answer["pid"], int)
        state = self.wait_ready()
        self.assertTrue(state["running"], state)
        self.assertTrue(state["managed"], state)
        self.assertEqual(state["pid"], answer["pid"])
        # last two components, because the separator is not the same on both platforms
        self.assertTrue(any(Path(row).parts[-2:] == ("models", "fake-qwen.gguf")
                            for row in state["served"]), state["served"])
        # the picker must see it without a restart: the cache is dropped on start
        models = self.engine.get("/api/models")[1]
        self.assertTrue(any(Path(row).parts[-2:] == ("models", "fake-qwen.gguf")
                            for row in models["names"]), models["names"])

    def test_52_start_again_reuses_the_running_one(self) -> None:
        before = self.state()
        status, answer = self.engine.post("/api/local-model/start", {})
        self.assertEqual(status, 200, answer)
        self.assertTrue(answer["ok"], answer)
        self.assertTrue(answer["already"], answer)
        self.assertEqual(answer["pid"], before["pid"])
        self.assertEqual(self.state()["pid"], before["pid"])

    def test_56_every_gguf_in_every_directory_is_a_candidate(self) -> None:
        state = self.state()
        rows = {row["name"]: row for row in state["candidates"]}
        self.assertIn("fake-qwen.gguf", rows, rows)
        self.assertIn("other-qwen.gguf", rows, rows)
        self.assertEqual(rows["other-qwen.gguf"]["bytes"], len(b"GGUF" * 512))
        self.assertEqual(rows["fake-qwen.gguf"]["bytes"], len(b"GGUF"))
        self.assertEqual(rows["other-qwen.gguf"]["path"], str(self.other))
        self.assertEqual(len(state["model_dirs"]), 2, state["model_dirs"])
        self.assertIn(str(self.other_dir), state["model_dirs"])
        self.assertIn(str(self.other), state["models"])

    def test_57_the_chosen_model_is_the_one_that_gets_served(self) -> None:
        status, answer = self.engine.post("/api/local-model/start", {"model": str(self.other)})
        self.assertEqual(status, 200, answer)
        self.assertTrue(answer["ok"], answer)
        state = self.wait_ready()
        self.assertTrue(state["running"], state)
        self.assertTrue(any(row.endswith("other-qwen.gguf") for row in state["served"]),
                        state["served"])
        self.assertIn("-m " + str(self.other), state["log"])

    def test_58_another_choice_reloads_the_server_with_it(self) -> None:
        before = self.state()
        status, answer = self.engine.post("/api/local-model/start", {"model": str(self.gguf)})
        self.assertEqual(status, 200, answer)
        self.assertTrue(answer["ok"], answer)
        self.assertTrue(answer["switched"], answer)
        state = self.wait_ready()
        self.assertTrue(state["running"], state)
        self.assertNotEqual(state["pid"], before["pid"], state)
        self.assertTrue(any(row.endswith("fake-qwen.gguf") for row in state["served"]),
                        state["served"])
        names = self.engine.get("/api/models")[1]["names"]
        self.assertTrue(any(row.endswith("fake-qwen.gguf") for row in names), names)

    def test_53_stop_ends_the_server_it_started(self) -> None:
        pid = self.state()["pid"]
        status, answer = self.engine.post("/api/local-model/stop")
        self.assertEqual(status, 200, answer)
        self.assertTrue(answer["ok"], answer)
        self.assertTrue(answer["stopped"], answer)
        self.assertFalse(answer["running"], answer)
        self.assertFalse(answer["managed"], answer)
        self.assertFalse(pid_alive(int(pid)), "pid %s outlived the stop" % pid)
        self.assertEqual(self.wait_ready(2.0)["health"], "unreachable")

    def test_54_stop_with_nothing_of_its_own_refuses(self) -> None:
        status, answer = self.engine.post("/api/local-model/stop")
        self.assertEqual(status, 200, answer)
        self.assertFalse(answer["ok"], answer)
        self.assertIn("another process", answer["error"])

    def test_55_a_missing_binary_is_named_not_guessed(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="locode-llama-bad-"))
        engine = None
        try:
            engine = Engine(tmp, extra_env={
                "LLAMA_BIN": "/nonexistent/llama-server",
                "LLAMA_MODEL": str(self.gguf),
                "FREEDOM_LLAMA_URLS": "http://127.0.0.1:%d" % free_port(),
            })
            status, answer = engine.post("/api/local-model/start", {})
            self.assertEqual(status, 200, answer)
            self.assertFalse(answer["ok"], answer)
            self.assertIn("/nonexistent/llama-server", answer["error"])
            self.assertFalse(answer["bin_exists"])
        finally:
            if engine is not None:
                engine.stop()
            shutil.rmtree(tmp, ignore_errors=True)


class QwenCloudModel(unittest.TestCase):
    """Qwen3.8 Flash is declared, not hoped for: hidden without its key, ready with it.

    Two engines, because "no key" and "key set" are different states of the same
    catalog and the check must not depend on what this machine happens to export.
    """

    def test_59_qwen_flash_is_wired_in_as_a_cloud_model(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="locode-qwen-"))
        engines: list[Engine] = []
        try:
            engines.append(Engine(tmp / "no-key", extra_env={
                "DASHSCOPE_API_KEY": "",
                "FREEDOM_LLAMA_URLS": "http://127.0.0.1:%d" % free_port(),
            }))
            payload = engines[0].get("/api/models")[1]
            hidden = {row["id"]: row for row in payload["unavailable"]}
            self.assertIn("qwen3.8-flash", hidden, hidden)
            self.assertIn("DASHSCOPE_API_KEY", hidden["qwen3.8-flash"]["why"])
            self.assertNotIn("qwen3.8-flash", payload["names"], payload["names"])

            engines.append(Engine(tmp / "keyed", extra_env={
                "DASHSCOPE_API_KEY": "sk-check-not-a-real-key",
                "FREEDOM_LLAMA_URLS": "http://127.0.0.1:%d" % free_port(),
            }))
            payload = engines[1].get("/api/models")[1]
            rows = {row["id"]: row for row in payload["models"]}
            self.assertIn("qwen3.8-flash", rows, payload["names"])
            row = rows["qwen3.8-flash"]
            self.assertEqual(row["provider"], "openai-compat")
            self.assertEqual(row["kind"], "cloud")
            self.assertEqual(row["context"], 1000000)
            self.assertTrue(row["tools"], row)
            self.assertTrue(row["vision"], row)
            self.assertIn("dashscope-intl.aliyuncs.com", row["base_url"])
            self.assertEqual(row["api_key_env"], "DASHSCOPE_API_KEY")
            self.assertTrue(row["available"], row)
            # the keys panel offers the name that makes this row work, and says
            # where the key came from without ever reading the value back
            keys = {k["name"]: k for k in engines[1].get("/api/keys")[1]["keys"]}
            self.assertIn("DASHSCOPE_API_KEY", keys, keys)
            self.assertTrue(keys["DASHSCOPE_API_KEY"]["set"], keys)
            self.assertEqual(keys["DASHSCOPE_API_KEY"]["source"], "env")
        finally:
            for engine in engines:
                engine.stop()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
