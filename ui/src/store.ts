/* The shell's single store: it replaces the old `window.state` plus the app.js
   orchestration, and every chunk talks to the engine through the actions here.

   An external store rather than prop threading because the SSE stream, the
   approval poller and the server registry mutate overlapping slices that the
   composer, thread, tree and preview pane all react to. `useApp(selector)`
   subscribes a component to one field.

   Streaming is rAF-batched (chunk 5's 60fps rule): tokens land in a buffer and
   one flush per frame writes them into the message list. */

import { useSyncExternalStore } from 'react';
import * as http from './api';
import { planRequest } from './plan';
import type {
  ApprovalReq, ChatMessage, Health, InboxItem, KeyRow, ModelRow, Msg, SandboxStatus,
  ServerRow, SessionRow, StatusKind, StreamEvent, ToolRow
} from './types';

/* The rail's destinations: home is the chat, the other two open a panel. */
export type View = 'home' | 'settings' | 'integrations';

/* The command center's panes: the console (rows), the doctor trace and the
   report export. */
export type FusionView = 'console' | 'doctor' | 'export';

export interface AppState {
  baseUrl: string;
  model: string;
  models: ModelRow[];
  modelNote: string;            // what is missing, when a provider is not usable
  tps: number | null;           // tokens/sec on the turn that is streaming now
  tpsModel: string;             // the model those tokens came from
  rates: Record<string, number>; // the last measured rate, per model
  health: Health | null;
  sandbox: SandboxStatus | null;
  sandboxBusy: boolean;
  sandboxNote: string;          // short inline outcome: restarted / failed
  sandboxNoteTitle: string;     // the server's own sentence, for the tooltip
  netMode: string;
  status: { text: string; kind: StatusKind };
  busy: boolean;
  messages: Msg[];
  files: string[];
  treeMs: number;
  treeError: string;
  approvals: ApprovalReq[];
  inbox: { open: boolean; items: InboxItem[]; path: string };
  modal: { open: boolean; title: string; sub: string; text: string; logs: boolean };
  preview: { name: string; port: number; nonce: number; visible: boolean };
  server: { name: string; port: number };
  draft: string;
  insert: { text: string; nonce: number } | null;
  theme: 'dark' | 'light';
  view: View;
  uploadBusy: boolean;
  keys: KeyRow[];
  keyBusy: string;              // env name currently being saved
  project: { name: string; path: string; parent: string; root: string };
  newProjectOpen: boolean;
  sidebarOpen: boolean;
  /* ⌘K: the Fusion command center. `view` is the pane it opens on; query and
     index live here for the same reason the rows are derived from the slices
     above — the center cannot offer something the app cannot do. */
  fusion: { open: boolean; view: FusionView; query: string; index: number };
  planMode: boolean;
  changes: {
    open: boolean; initialized: boolean; branch: string; status: string;
    excluded: string[];              // LOCODE's own records git is not showing
    busy: boolean; note: string;      // note = git's own output, never a guess
    /* One file's diff, opened from a row; `path` empty means the list is shown. */
    diff: { path: string; text: string; busy: boolean; error: string };
  };
  /* Past conversations from /api/sessions. `current` is the id the engine has
     open, so ⌘K can say which row you are already in. */
  sessions: { items: SessionRow[]; current: string; busy: boolean };
}

/* The last rate each model actually held, kept across reloads: a number that
   was measured is worth showing again, and it is what makes the picker's models
   comparable without timing them by hand. */
function loadRates(): Record<string, number> {
  try {
    const raw = JSON.parse(localStorage.getItem('locode-tps') || '{}') as Record<string, unknown>;
    const out: Record<string, number> = {};
    Object.keys(raw).forEach((id) => {
      const value = raw[id];
      if (typeof value === 'number' && value > 0) out[id] = value;
    });
    return out;
  } catch { return {}; }
}

const initial: AppState = {
  baseUrl: (localStorage.getItem('locode-base-url') || '').replace(/\/+$/, ''),
  model: '',
  models: [],
  modelNote: '',
  tps: null,
  tpsModel: '',
  rates: loadRates(),
  health: null,
  sandbox: null,
  sandboxBusy: false,
  sandboxNote: '',
  sandboxNoteTitle: '',
  netMode: 'off',
  status: { text: 'idle', kind: 'idle' },
  busy: false,
  messages: [],
  files: [],
  treeMs: 0,
  treeError: '',
  approvals: [],
  inbox: { open: false, items: [], path: '' },
  modal: { open: false, title: '', sub: '', text: '', logs: false },
  preview: { name: '', port: 0, nonce: 0, visible: false },
  server: { name: '', port: 0 },
  draft: '',
  insert: null,
  theme: (localStorage.getItem('locode-theme') === 'light' ? 'light' : 'dark'),
  view: 'home',
  uploadBusy: false,
  keys: [],
  keyBusy: '',
  project: { name: '', path: '', parent: '', root: '' },
  newProjectOpen: false,
  sidebarOpen: false,
  fusion: { open: false, view: 'console', query: '', index: 0 },
  planMode: false,
  changes: {
    open: false, initialized: false, branch: '', status: '', busy: false, note: '',
    excluded: [],
    diff: { path: '', text: '', busy: false, error: '' }
  },
  sessions: { items: [], current: '', busy: false }
};

http.setBaseUrl(initial.baseUrl);

let state: AppState = initial;
const listeners = new Set<() => void>();

function set(patch: Partial<AppState> | ((s: AppState) => Partial<AppState>)): void {
  const next = typeof patch === 'function' ? patch(state) : patch;
  state = { ...state, ...next };
  listeners.forEach((l) => { l(); });
}

export function getState(): AppState { return state; }

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function useApp<T>(selector: (s: AppState) => T): T {
  return useSyncExternalStore(subscribe, () => selector(state), () => selector(state));
}

const err = (e: unknown): string => (e instanceof Error ? e.message : String(e));

// ── status (chunk 3: exposed on window too) ─────────────────────────────────
export function setStatus(text: string, kind: StatusKind = 'idle'): void {
  set({ status: { text: text || 'idle', kind } });
}

export function netLabel(mode: string): string {
  if (mode === 'ask') return 'on (ask)';
  if (mode === 'auto-safe') return 'on (auto-safe)';
  if (mode.startsWith('on')) return mode;
  return 'off';
}

// ── thread (chunk 5) ────────────────────────────────────────────────────────
let msgSeq = 0;

function pushMsg(m: Msg): void {
  set((s) => ({ messages: [...s.messages, m] }));
}

export function addUserMessage(text: string): number {
  const id = ++msgSeq;
  pushMsg({ id, role: 'user', text, tools: [] });
  return id;
}

export function addAssistantMessage(): number {
  const id = ++msgSeq;
  pushMsg({ id, role: 'assistant', text: '', tools: [] });
  return id;
}

export function addErrorMessage(text: string): number {
  const id = ++msgSeq;
  pushMsg({ id, role: 'error', text, tools: [] });
  return id;
}

export function resetThread(): void {
  stream.clear();
  set({ messages: [] });
}

/* A transcript from the engine as bubbles. Tool cards are not in the session
   file, so a restored turn keeps its text - which is what the conversation was. */
function restoreThread(msgs: ChatMessage[]): void {
  const restored: Msg[] = [];
  for (const m of msgs.slice(-60)) {
    if (m.role === 'user') {
      restored.push({
        id: ++msgSeq, role: 'user',
        text: typeof m.content === 'string' ? m.content : JSON.stringify(m.content), tools: []
      });
    } else if (m.role === 'assistant' && m.content) {
      restored.push({ id: ++msgSeq, role: 'assistant', text: String(m.content), tools: [] });
    }
  }
  set({ messages: restored });
}

function updateMsg(id: number, fn: (m: Msg) => Msg): void {
  set((s) => ({ messages: s.messages.map((m) => (m.id === id ? fn(m) : m)) }));
}

/* tok/s is measured, not guessed: the arrival times of the deltas sent in the
   last 1.5s of the turn. A window rather than "all deltas so far" because that
   average keeps lagging after the model speeds up, and a window rather than the
   last gap because one slow frame would read as 2 tok/s. Updated once per
   animation frame, next to the text, so measuring costs no extra renders. */
const DELTA_WINDOW_MS = 1500;
let stamps: number[] = [];
let turnModel = '';

function resetRate(model: string): void {
  stamps = [];
  turnModel = model;
  if (state.tps !== null || state.tpsModel !== model) set({ tps: null, tpsModel: model });
}

/* Deltas per second over the window; the first one only starts the clock. */
function measure(): void {
  const now = performance.now();
  while (stamps.length > 2 && stamps[0] < now - DELTA_WINDOW_MS) stamps.shift();
  if (stamps.length < 2) return;
  const span = (stamps[stamps.length - 1] - stamps[0]) / 1000;
  if (span <= 0) return;
  const tps = Math.round(((stamps.length - 1) / span) * 10) / 10;
  if (state.tps === null || Math.abs(state.tps - tps) >= 0.2) set({ tps, tpsModel: turnModel });
}

/* rAF-batched append: many tokens per frame, one render per frame. */
const stream = new Map<number, string>();
let raf = 0;

function flush(): void {
  raf = 0;
  if (!stream.size) return;
  const deltas = new Map(stream);
  stream.clear();
  measure();
  set((s) => ({
    messages: s.messages.map((m) => {
      const d = deltas.get(m.id);
      return d ? { ...m, text: m.text + d } : m;
    })
  }));
}

export function appendText(id: number, chunk: string): void {
  if (!chunk) {
    if (!stream.has(id)) stream.set(id, '');   // force a paint for an empty push
  } else {
    stream.set(id, (stream.get(id) || '') + chunk);
  }
  if (!raf) raf = requestAnimationFrame(flush);
}

// ── tool cards (chunk 6) ────────────────────────────────────────────────────
export function addToolCard(msgId: number, id: string, name: string, args: Record<string, unknown>): void {
  const card: ToolRow = { id, name, args, state: 'running', result: 'running…', open: false };
  updateMsg(msgId, (m) => ({ ...m, tools: [...m.tools, card] }));
}

export function finishToolCard(msgId: number, toolId: string, ok: boolean, result: unknown): void {
  const text = typeof result === 'string' ? result : JSON.stringify(result, null, 2);
  updateMsg(msgId, (m) => ({
    ...m,
    tools: m.tools.map((t) => (t.id === toolId
      ? { ...t, state: ok ? 'ok' as const : 'err' as const, result: text, open: t.open || !ok }
      : t))
  }));
}

export function toggleToolCard(msgId: number, toolId: string): void {
  updateMsg(msgId, (m) => ({
    ...m,
    tools: m.tools.map((t) => (t.id === toolId ? { ...t, open: !t.open } : t))
  }));
}

// ── the turn loop (app.js) ──────────────────────────────────────────────────
let aborter: AbortController | null = null;

function history(): ChatMessage[] {
  return state.messages
    .filter((m) => m.role !== 'error' && m.text)
    .map((m) => ({ role: m.role, content: m.text }));
}

function handleEvent(ev: StreamEvent, msgId: number): boolean {
  switch (ev.type) {
    case 'token':
      stamps.push(performance.now());
      appendText(msgId, ev.text || '');
      setStatus('thinking…', 'think');
      break;

    case 'tool_call':
      addToolCard(msgId, String(ev.id ?? ''), ev.name || 'tool', ev.args || {});
      setStatus('running tool: ' + (ev.name || 'tool'), 'run');
      break;

    case 'tool_result': {
      const raw = typeof ev.result === 'string' ? ev.result : JSON.stringify(ev.result ?? '');
      const ok = !(/^(ERROR|denied)/i.test(raw) || /denied by developer/i.test(raw));
      finishToolCard(msgId, String(ev.id ?? ''), ok, raw);
      setStatus(ok ? 'thinking…' : 'tool failed', ok ? 'think' : 'err');
      break;
    }

    case 'file_changed':
    case 'files_changed':
      void refreshTree();
      /* an open changes drawer follows the turn instead of going stale */
      if (state.changes.open) void refreshGit();
      break;

    case 'server_started': {
      const name = String(ev.name || 'app');
      showPreview(name, Number(ev.port) || 0);
      setStatus('preview: ' + name + ':' + (ev.port || '?'), 'ok');
      break;
    }

    case 'error':
      updateMsg(msgId, (m) => ({ ...m, text: m.text + (m.text ? '\n\n' : '') + '⚠ ' + (ev.text || 'error') }));
      setStatus('error', 'err');
      return true;

    case 'done':
      if (ev.model) set({ model: ev.model });
      setStatus('idle', 'idle');
      break;

    default:
      break;
  }
  return false;
}

async function runTurn(): Promise<void> {
  const text = state.draft.trim();
  if (!text || state.busy) return;

  set({ draft: '' });
  addUserMessage(text);
  /* The thread as sent, minus the empty reply. Plan mode is a request contract,
     not a UI-only flag: planRequest() prefixes the message the developer typed
     and leaves the thread they see alone (see plan.ts, and ui/test/plan.test.ts). */
  const snapshot = planRequest(history(), state.planMode);
  const msgId = addAssistantMessage();

  resetRate(state.model);
  set({ busy: true });
  aborter = new AbortController();
  setStatus('thinking…', 'think');

  let sawError = false;
  try {
    const res = await http.openChatStream(snapshot, state.model, aborter.signal);
    for await (const ev of http.readStream(res)) {
      if (handleEvent(ev, msgId)) sawError = true;
    }
    /* requestAnimationFrame does not run in a background tab, so a turn can end
       with its whole reply still in the buffer. Write it out before deciding the
       reply was empty — otherwise the placeholder lands first and the text is
       appended after it (`…need one._2` was a real symptom). */
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    flush();
    if (!sawError) {
      const final = state.messages.find((m) => m.id === msgId);
      if (final && !final.text && !final.tools.length) {
        updateMsg(msgId, (m) => ({
          ...m,
          text: '_the model sent no text this turn — send again to retry._'
        }));
      }
    }
  } catch (e) {
    if (err(e) === 'AbortError' || (e instanceof DOMException && e.name === 'AbortError')) {
      setStatus('stopped', 'idle');
    } else {
      addErrorMessage('chat failed: ' + err(e));
      setStatus('error', 'err');
    }
  } finally {
    /* Flush before clearing: a turn that finishes inside one animation frame
       (this 0.5B model answers in a single token) used to lose its whole reply
       to `stream.clear()` here — the bubble stayed empty and got the "no text
       reply" placeholder instead, while curl showed the text fine. */
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    flush();
    stream.clear();
    /* Keep the rate under the model that produced it: switching models then
       shows each one's own measured pace instead of the other's. */
    if (state.tps !== null && turnModel) {
      const rates = { ...state.rates, [turnModel]: state.tps };
      set({ rates, tps: null });
      try { localStorage.setItem('locode-tps', JSON.stringify(rates)); } catch { /* private mode */ }
    }
    stamps = [];
    set({ busy: false });
    aborter = null;
    if (!sawError) setStatus('idle', 'idle');
    void saveThread().then(() => refreshSessions());
    void refreshTree();
    void refreshServers();
  }
}

export function sendPrompt(text: string): void {
  if (state.busy) { setStatus('busy — wait for this turn', 'run'); return; }
  set({ draft: text });
  void runTurn();
}

export function submitDraft(): void { void runTurn(); }

export function abortTurn(): void {
  if (aborter) aborter.abort();
}

// ── header data (chunk 3) ───────────────────────────────────────────────────
export async function refreshHealth(): Promise<void> {
  try {
    const h = await http.getHealth();
    set({ health: h, netMode: h.network || 'off' });
    await Promise.all([refreshTree(), refreshServers(), refreshSandbox()]);
  } catch (e) {
    set({ netMode: 'off' });
    setStatus('health: ' + err(e), 'err');
  }
}

/* Which backend confines a command, and whether it caps memory/CPU. Kept out of
   refreshHealth's payload so a stopped daemon cannot make the header lie. */
export async function refreshSandbox(): Promise<void> {
  try {
    set({ sandbox: await http.getSandbox() });
  } catch { /* not fatal: the chip falls back to its unknown state */ }
}

/* The backend can change while this tab does nothing: `POST /api/docker/restart`
   from the terminal, or the server coming back with a different SANDBOX after a
   restart. Polling (only while visible) and one refresh on focus keep the chip
   honest without anyone pressing ⟳. */
let sandboxTimer: number | null = null;
let sandboxNoteTimer: number | null = null;

/* Clicking the chip. The endpoint answers data (ok/status/error) rather than an
   HTTP code, so the outcome is shown in the chip itself for a few seconds and
   the status is re-read right away instead of waiting for the next poll. */
export async function restartSandbox(): Promise<void> {
  if (state.sandboxBusy) return;
  set({ sandboxBusy: true, sandboxNote: 'restarting…', sandboxNoteTitle: '' });
  try {
    const r = await http.restartSandbox();
    /* the chip has room for one word: the endpoint's sentence goes in the title,
       because "unpacked .sandbox-rootfs from alpine-minirootfs-…" overflowed the
       header on a narrow window */
    set({
      sandboxNote: r.ok ? 'restarted' : 'failed',
      sandboxNoteTitle: r.ok ? (r.status || '') : (r.error || 'unknown error')
    });
    await refreshSandbox();
  } catch (e) {
    set({ sandboxNote: 'failed', sandboxNoteTitle: err(e) });
  } finally {
    set({ sandboxBusy: false });
    if (sandboxNoteTimer !== null) window.clearTimeout(sandboxNoteTimer);
    sandboxNoteTimer = window.setTimeout(
      () => set({ sandboxNote: '', sandboxNoteTitle: '' }), 6000);
  }
}

export function startSandboxWatch(intervalMs = 8000): void {
  if (sandboxTimer !== null) return;
  void refreshSandbox();
  sandboxTimer = window.setInterval(() => {
    if (!document.hidden) void refreshSandbox();
  }, intervalMs);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) void refreshSandbox();
  });
}

export async function refreshModels(): Promise<void> {
  try {
    const m = await http.getModels();
    const models = m.models || [];
    const fallback = m.default && models.some((r) => r.id === m.default)
      ? m.default
      : (models.find((r) => r.available) || {}).id || '';
    /* The engine only sends models that can answer; everything else arrives as
       `unavailable` so the picker can say what needs a key or a server instead
       of offering a choice that cannot run. */
    const missing = m.unavailable || [];
    const note = m.hint
      || (missing.length
        ? missing.length + ' provider' + (missing.length === 1 ? '' : 's')
          + ' not set up: ' + missing.slice(0, 4).map((u) => u.id).join(', ')
        : '');
    set((s) => ({ models, model: s.model || fallback, modelNote: note }));
    if (m.error) setStatus('models: ' + m.error, 'err');
  } catch (e) {
    setStatus('models: ' + err(e), 'err');
  }
}

export function setModel(id: string): void { set({ model: id }); }

// ── provider keys (Integrations) ────────────────────────────────────────────
export async function refreshKeys(): Promise<void> {
  try {
    set({ keys: (await http.getKeys()).keys || [] });
  } catch { /* the panel keeps whatever it last knew */ }
}

/* Saving a key must make the picker change immediately: the engine drops its
   models cache server-side, and refreshModels() re-reads availability here. */
export async function saveProviderKey(name: string, value: string): Promise<void> {
  if (state.keyBusy) return;
  set({ keyBusy: name });
  try {
    const r = await http.saveKey(name, value);
    setStatus(r.set ? name + ' saved' : name + ' cleared', 'ok');
    await Promise.all([refreshKeys(), refreshModels()]);
  } catch (e) {
    setStatus('key: ' + err(e), 'err');
  } finally {
    set({ keyBusy: '' });
  }
}

export function setBaseUrl(value: string): void {
  const baseUrl = String(value || '').trim().replace(/\/+$/, '');
  http.setBaseUrl(baseUrl);
  set({ baseUrl });
  localStorage.setItem('locode-base-url', baseUrl);
  setStatus(baseUrl ? 'engine: ' + baseUrl : 'engine: local', 'ok');
  void refreshModels();
  void refreshHealth();
}

export async function newSession(): Promise<void> {
  if (!window.confirm('New session? This clears the message thread and the agent\'s memory of it. Your files are not touched.')) return;
  try {
    await http.resetSession();
    resetThread();
    setStatus('idle', 'idle');
    await Promise.all([refreshHealth(), refreshSessions()]);
  } catch (e) {
    setStatus('reset failed: ' + err(e), 'err');
  }
}

/* The transcript the engine keeps. An empty thread is not saved: an empty POST
   with no id means "start a new session", and sending one would archive the
   conversation that is actually open. */
async function saveThread(): Promise<void> {
  const msgs = history();
  if (!msgs.length) return;
  try {
    await http.saveSession(msgs, state.sessions.current);
  } catch { /* best effort: a turn must not fail because its save did */ }
}

export async function refreshSessions(): Promise<void> {
  try {
    const r = await http.getSessions();
    set((s) => ({ sessions: {
      ...s.sessions, items: r.sessions || [], current: r.current || ''
    } }));
  } catch { /* history is a convenience; a missing list must not break the shell */ }
}

/* Switching conversations saves the open one first - the transcript a turn left
   on disk must not be replaced by an older thread - then restores the chosen one
   into the same message list the thread renders. */
export async function openSessionById(id: string): Promise<void> {
  if (state.busy || state.sessions.busy || !id || id === state.sessions.current) return;
  set((s) => ({ sessions: { ...s.sessions, busy: true } }));
  try {
    await saveThread();
    const r = await http.openSession(id);
    resetThread();
    restoreThread(r.messages || []);
    setStatus('session: ' + (r.title || id), 'ok');
    await refreshSessions();
  } catch (e) {
    setStatus('session: ' + err(e), 'err');
  } finally {
    set((s) => ({ sessions: { ...s.sessions, busy: false } }));
  }
}

export function toggleTheme(): void {
  const theme: 'dark' | 'light' = state.theme === 'light' ? 'dark' : 'light';
  localStorage.setItem('locode-theme', theme);
  document.documentElement.dataset.theme = theme;
  set({ theme });
}

export function setView(view: View): void { set({ view }); }
export function closePanel(): void { set({ view: 'home' }); }
export function toggleSidebar(): void { set((s) => ({ sidebarOpen: !s.sidebarOpen })); }

// ── command center (⌘K) ─────────────────────────────────────────────────────
/* Opening resets the query: the center is a fresh look at the app, not a
   continuation of the last search. A view can be requested for a deep link. */
export function openFusion(view: FusionView = 'console'): void {
  set({ fusion: { open: true, view, query: '', index: 0 } });
  /* Its telemetry strip states the branch and the changed count, so the status
     has to be read on open: the drawer used to be the only thing that fetched
     it, which left the strip claiming "git none" inside a repository. */
  void refreshGit();
}
export function closeFusion(): void { set((s) => ({ fusion: { ...s.fusion, open: false } })); }

/* Switching panes drops the selection for the same reason a new query does: the
   rows of one pane are not the rows of the next. */
export function setFusionView(view: FusionView): void {
  set((s) => ({ fusion: { ...s.fusion, view, index: 0 } }));
}

/* A new query restarts the selection: keeping index 4 while the list shrinks to
   two rows would leave Enter pointing at nothing. */
export function setFusionQuery(query: string): void {
  set((s) => ({ fusion: { ...s.fusion, query, index: 0 } }));
}

export function setFusionIndex(index: number): void {
  set((s) => ({ fusion: { ...s.fusion, index } }));
}

/* Plan mode changes the next request, not the shell: the prefix itself lives in
   plan.ts, where ui/test/plan.test.ts can check it without a browser. */
export function togglePlanMode(): void {
  const planMode = !state.planMode;
  set({ planMode });
  setStatus(planMode ? 'plan mode on — the next turn only plans' : 'plan mode off',
    planMode ? 'run' : 'idle');
}

export function setPlanMode(on: boolean): void {
  if (state.planMode !== on) togglePlanMode();
}

// ── changes (git) ───────────────────────────────────────────────────────────
export function openChanges(): void {
  set((s) => ({ changes: { ...s.changes, open: true } }));
  void refreshGit();
}

/* Closing the drawer forgets the diff it was showing: the next open starts at
   the file list, which is the list the reader asked for. */
export function closeChanges(): void {
  set((s) => ({ changes: {
    ...s.changes, open: false, diff: { path: '', text: '', busy: false, error: '' }
  } }));
}

/* The diff of one file, as git writes it. A response that was cut says so in the
   drawer rather than ending mid-line without a word. */
export function openFileDiff(path: string): void {
  const target = path.includes(' -> ') ? path.slice(path.indexOf(' -> ') + 4) : path;
  set((s) => ({ changes: {
    ...s.changes, diff: { path: target, text: '', busy: true, error: '' }
  } }));
  http.getGitDiff(target).then(
    (r) => set((s) => ({ changes: { ...s.changes, diff: {
      path: r.path || target,
      text: r.diff || '',
      busy: false,
      error: r.truncated ? 'diff cut at 400 KB — open the file for the rest' : ''
    } } })),
    (e) => set((s) => ({ changes: { ...s.changes, diff: {
      path: target, text: '', busy: false, error: 'diff: ' + err(e)
    } } }))
  );
}

export function closeFileDiff(): void {
  set((s) => ({ changes: {
    ...s.changes, diff: { path: '', text: '', busy: false, error: '' }
  } }));
}

export async function refreshGit(): Promise<void> {
  try {
    const g = await http.getGitStatus();
    /* `note` is left alone: it holds the last commit's output, and a refresh is
       exactly what a commit does right after producing it. */
    set((s) => ({ changes: {
      ...s.changes, initialized: Boolean(g.initialized), branch: g.branch || '',
      status: g.status || '', excluded: g.excluded || []
    } }));
  } catch (e) {
    set((s) => ({ changes: { ...s.changes, note: 'git: ' + err(e) } }));
  }
}

/* Commit answers git's own output rather than an HTTP error, so "nothing to
   commit" arrives as a sentence in the drawer. */
export async function commitChanges(message: string): Promise<void> {
  if (state.changes.busy) return;
  set((s) => ({ changes: { ...s.changes, busy: true, note: '' } }));
  try {
    const r = await http.gitCommit(message.trim() || 'LOCODE: checkpoint');
    await Promise.all([refreshGit(), refreshTree()]);
    set((s) => ({ changes: { ...s.changes, note: (r.output || 'committed').trim() } }));
    setStatus('committed', 'ok');
  } catch (e) {
    set((s) => ({ changes: { ...s.changes, note: 'commit: ' + err(e) } }));
    setStatus('commit failed', 'err');
  } finally {
    set((s) => ({ changes: { ...s.changes, busy: false } }));
  }
}

export async function initRepo(): Promise<void> {
  if (state.changes.busy) return;
  set((s) => ({ changes: { ...s.changes, busy: true, note: '' } }));
  try {
    const r = await http.gitInit();
    await Promise.all([refreshGit(), refreshTree()]);
    /* git's own sentence, because init also makes the first commit and that can
       still be refused — reporting "initialized" over a failed commit lied. */
    set((s) => ({ changes: { ...s.changes, note: (r.output || 'repository initialized').trim() } }));
    setStatus(r.ok === false ? 'git init: first commit refused' : 'repository initialized',
      r.ok === false ? 'err' : 'ok');
  } catch (e) {
    set((s) => ({ changes: { ...s.changes, note: 'init: ' + err(e) } }));
    setStatus('git init failed', 'err');
  } finally {
    set((s) => ({ changes: { ...s.changes, busy: false } }));
  }
}

// ── workspace tree (chunk 4) ────────────────────────────────────────────────
export async function refreshTree(): Promise<void> {
  const t0 = performance.now();
  try {
    const r = await http.getFiles();
    const paths = r.paths || r.files || [];    // spec says `paths`; older builds say `files`
    set({ files: paths, treeMs: Math.round(performance.now() - t0), treeError: '' });
  } catch (e) {
    set({ files: [], treeError: err(e) });
    setStatus('tree: ' + err(e), 'err');
  }
}

export function insertPath(text: string): void {
  set({ insert: { text, nonce: Date.now() } });
}

export function setDraft(value: string): void { set({ draft: value }); }

/* Files the developer picked with the composer's +. They go to the engine as raw
   bodies (no multipart parser needed) and land in workspace/uploads/; the paths
   are left in the draft so the next turn knows what they are. */
export async function uploadFiles(files: File[]): Promise<void> {
  if (!files.length || state.uploadBusy) return;
  set({ uploadBusy: true });
  const paths: string[] = [];
  const failed: string[] = [];
  for (const f of files) {
    try {
      const r = await http.uploadFile(f.name, f);
      if (r.ok && r.path) paths.push(r.path);
      else failed.push(f.name + ': ' + (r.error || 'refused'));
    } catch (e) {
      failed.push(f.name + ': ' + err(e));
    }
  }
  set({ uploadBusy: false });
  if (paths.length) {
    const current = state.draft.trimEnd();
    setDraft((current ? current + '\n\n' : '') + '[uploaded: ' + paths.join(' ') + ']');
    setStatus('uploaded ' + paths.length + ' file' + (paths.length > 1 ? 's' : ''), 'ok');
    void refreshTree();
  }
  if (failed.length) setStatus('upload failed — ' + failed.join('; '), 'err');
}

// ── approvals (chunk 7) ─────────────────────────────────────────────────────
export async function pollApprovals(): Promise<void> {
  try {
    const r = await http.getApprovals();
    const list = r.approvals || [];
    const patch: Partial<AppState> = { approvals: list };
    if (r.mode) patch.netMode = netLabel(r.mode);
    set(patch);
    if (list.length && !state.busy) {
      setStatus(list.length + ' approval' + (list.length > 1 ? 's' : '') + ' waiting', 'run');
    }
  } catch { /* the poll is background noise when the server is away */ }
}

export function resolveApproval(id: string, approved: boolean, remember: string[]): Promise<unknown> {
  return http.resolveApproval(id, approved, remember).then(() => {
    setStatus(approved ? (remember.length ? 'allowed + remembered' : 'allowed') : 'denied by developer',
      approved ? 'ok' : 'err');
  });
}

// ── inbox (chunk 8) ─────────────────────────────────────────────────────────
export async function refreshInbox(): Promise<void> {
  try {
    const r = await http.getInbox();
    set((s) => ({ inbox: { ...s.inbox, items: r.items || [], path: r.path || '' } }));
  } catch (e) {
    set((s) => ({ inbox: { ...s.inbox, items: [], path: 'inbox: ' + err(e) } }));
  }
}

export function toggleInbox(): void {
  const open = !state.inbox.open;
  set((s) => ({ inbox: { ...s.inbox, open } }));
  if (open) void refreshInbox();
}

export function closeInbox(): void { set((s) => ({ inbox: { ...s.inbox, open: false } })); }

/* Import never copies bytes here: it fills the composer so the agent calls
   import_file, which is what raises the chunk 7 approval card. */
export function importInboxItem(name: string): void {
  set({ draft: 'Use import_file with inbox_name="' + name + '"' });
  closeInbox();
}

export async function deleteInboxItem(name: string): Promise<void> {
  if (!window.confirm('Delete ' + name + ' from the inbox? This cannot be undone.')) return;
  setStatus('deleting ' + name, 'think');
  try {
    await http.deleteInboxItem(name);
    await refreshInbox();
    setStatus('deleted ' + name, 'ok');
  } catch (e) {
    setStatus('delete failed: ' + err(e), 'err');
  }
}

// ── modals (chunks 9 + 10) ──────────────────────────────────────────────────
export async function openFile(path: string): Promise<void> {
  setStatus('opening ' + path, 'think');
  try {
    const f = await http.readFile(path);
    const content = f.content || '(empty file)';
    const lines = f.content ? f.content.split('\n').length : 0;
    set({ modal: { open: true, title: f.path || path, sub: lines + ' lines', text: content, logs: false } });
    setStatus('idle', 'idle');
  } catch (e) {
    setStatus('file: ' + err(e), 'err');
  }
}

export async function showLogs(name: string, tail = 300): Promise<void> {
  try {
    const r = await http.serverLogs(name, tail);
    const text = (r.lines || []).join('\n');
    set({ modal: {
      open: true,
      title: name + ' — server logs',
      sub: (r.total || 0) + ' lines total',
      text: text || '(no output yet)',
      logs: true
    } });
  } catch (e) {
    setStatus('logs: ' + err(e), 'err');
  }
}

export function closeModal(): void {
  set((s) => ({ modal: { ...s.modal, open: false, text: '' } }));
}

// ── preview pane (chunk 11) ─────────────────────────────────────────────────
export function showPreview(name: string, port: number): void {
  set({
    preview: { name, port, nonce: 0, visible: true },
    server: { name, port }
  });
  setStatus('preview: ' + name + ':' + (port || '?'), 'ok');
}

/* Close hides the pane and drops the iframe's src; the tracked server keeps
   running, which is why the header still shows Stop afterwards. */
export function hidePreview(): void {
  set({ preview: { name: '', port: 0, nonce: 0, visible: false } });
}

export function reloadPreview(): void {
  if (!state.preview.name) return;
  set((s) => ({ preview: { ...s.preview, nonce: Date.now() } }));
}

export function startPreview(): void {
  const cmd = state.health?.project?.dev_command || '';
  if (!cmd) { setStatus('no dev command detected — tell the agent what to run', 'err'); return; }
  sendPrompt('Run start_server with name="app" and command="' + cmd + '", then tell me the port.');
}

export async function stopServer(): Promise<void> {
  const name = state.server.name;
  if (!name) return;
  try {
    await http.stopServer(name);
    set({ server: { name: '', port: 0 } });
    hidePreview();
    setStatus('server stopped', 'ok');
  } catch (e) {
    setStatus('stop failed: ' + err(e), 'err');
  }
}

/* The registry is the truth about what is running, but a row can land a moment
   after the SSE server_started frame — so never close a pane we are showing. */
export async function refreshServers(): Promise<void> {
  try {
    const r = await http.getServers();
    const rows = (r.servers || r.rows || []) as ServerRow[];
    const live = rows.filter((s) => s.state === 'running' && s.port);
    if (!live.length) {
      if (!state.preview.visible) set({ server: { name: '', port: 0 } });
      return;
    }
    const s = live[live.length - 1];
    set({ server: { name: s.name, port: s.port } });
    if (!state.preview.visible && s.port) showPreview(s.name, s.port);
  } catch { /* no servers endpoint yet */ }
}

// ── workspace area: project + dialogs ───────────────────────────────────────
export async function refreshWorkspace(): Promise<void> {
  try {
    const w = await http.getWorkspace();
    /* root is where new projects go: the engine's projects/ folder, not the
       parent of whatever workspace happens to be open. */
    set({ project: {
      name: w.name || '', path: w.workspace || '', parent: w.parent || '',
      root: w.projects_root || ''
    } });
  } catch { /* the dialog then falls back to the engine's own parent */ }
}

export function openNewProject(): void { set({ newProjectOpen: true }); }
export function closeNewProject(): void { set({ newProjectOpen: false }); }

/* Creating a project switches the engine's workspace server-side, so the thread
   is cleared (the new project has no transcript) and everything else re-reads. */
export async function createProject(
  input: { name: string; parent: string; repo?: string; template?: string }
): Promise<{ ok: boolean; error?: string }> {
  setStatus('creating ' + input.name + '…', 'think');
  try {
    const r = await http.createProject(input);
    resetThread();
    set({ newProjectOpen: false });
    await Promise.all([refreshWorkspace(), refreshHealth(), refreshModels(), refreshTree()]);
    setStatus('project ' + (r.name || input.name) + ' ready', 'ok');
    return { ok: true };
  } catch (e) {
    const message = err(e);
    setStatus('project: ' + message, 'err');
    return { ok: false, error: message };
  }
}

// ── boot (app.js) ───────────────────────────────────────────────────────────
export async function boot(): Promise<void> {
  setStatus('idle', 'idle');
  document.documentElement.dataset.theme = state.theme;
  try {
    const s = await http.loadSession();
    restoreThread(Array.isArray(s.messages) ? s.messages : []);
  } catch { /* a fresh engine has no session file yet */ }
  await Promise.all([
    refreshHealth(), refreshModels(), refreshKeys(), refreshWorkspace(), refreshSessions()
  ]);
  startSandboxWatch();
}