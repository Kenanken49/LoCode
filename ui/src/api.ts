/* Typed client for the engine. Chunk 3's `api()` / `post()` helpers, kept as
   the single place a URL is built: a saved base URL makes LOCODE drive a remote
   engine, otherwise every call is same-origin. */

import type {
  ApprovalsResponse, ChatMessage, FolderRow, GitStatus, Health, InboxItem, KeyRow,
  LocalModelStatus, ModelsResponse, SandboxStatus, ServerRow, SessionsResponse, StreamEvent,
  TemplateRow, WorkspaceInfo
} from './types';

let baseUrl = '';

export function setBaseUrl(value: string): void {
  baseUrl = String(value || '').replace(/\/+$/, '');
}

export function getBaseUrl(): string {
  return baseUrl;
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(baseUrl + path, init);
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    /* FastAPI puts the useful sentence in {"detail": …}; surface that instead of
       the JSON wrapper so a dialog can show "name already exists" as written. */
    let detail = text;
    try {
      const parsed = JSON.parse(text) as { detail?: unknown };
      if (typeof parsed.detail === 'string') detail = parsed.detail;
    } catch { /* not JSON: keep the body */ }
    throw new Error(`${res.status} ${(detail || res.statusText).slice(0, 200)}`);
  }
  return (await res.json()) as T;
}

export function post<T>(path: string, body?: unknown): Promise<T> {
  return api<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'locode' },
    body: JSON.stringify(body ?? {})
  });
}

// ── reads ───────────────────────────────────────────────────────────────────
export const getHealth = () => api<Health>('/api/health');
export const getModels = () => api<ModelsResponse>('/api/models', { cache: 'no-store' });
/* /api/docker/status answers for both backends, despite the route name. no-store
   because a heuristically cached reply kept the chip showing a previous mode
   after the server was restarted with a different SANDBOX. */
export const getSandbox = () => api<SandboxStatus>('/api/docker/status', { cache: 'no-store' });
/* Provider keys in the engine's own store. GET answers {name, set, source} and
   never the value; POST with an empty value clears one. */
export const getKeys = () => api<{ keys?: KeyRow[]; file?: string }>('/api/keys', { cache: 'no-store' });
export const saveKey = (name: string, value: string) =>
  post<{ ok?: boolean; name?: string; set?: boolean; source?: string }>('/api/keys', { name, value });
export const getFiles = () => api<{ files?: string[]; paths?: string[] }>('/api/files');
/* The local model server: what the engine resolved, and the two actions that
   replace a terminal. Both mutations answer data, so a refusal ("no llama-server
   binary at …") reaches the panel as the sentence the engine wrote. */
export const getLocalModel = () =>
  api<LocalModelStatus>('/api/local-model', { cache: 'no-store' });
export const startLocalModel = (bin: string, model: string) =>
  post<LocalModelStatus>('/api/local-model/start', { bin, model });
export const stopLocalModel = () => post<LocalModelStatus>('/api/local-model/stop');
/* The workspace's git view: the branch and `git status --short` verbatim, so the
   drawer can print what git said instead of re-describing it. no-store because a
   status is only ever true for the moment it was read. */
export const getGitStatus = () => api<GitStatus>('/api/git/status', { cache: 'no-store' });
/* One file's diff, as git writes it. `untracked` says the file has no version in
   HEAD, which is why the diff starts at /dev/null. */
export const getGitDiff = (path: string) =>
  api<{ path?: string; diff?: string; untracked?: boolean; truncated?: boolean }>(
    '/api/git/diff?path=' + encodeURIComponent(path), { cache: 'no-store' });
export const getApprovals = () => api<ApprovalsResponse>('/api/approvals');
export const getInbox = () => api<{ items?: InboxItem[]; path?: string }>('/api/inbox');
export const getServers = () => api<{ servers?: ServerRow[]; rows?: ServerRow[] }>('/api/servers');
/* Workspace area: the open project, the folder picker and the starter templates. */
export const getWorkspace = () => api<WorkspaceInfo>('/api/workspace', { cache: 'no-store' });
export const getFolders = (path = '') => api<{ path?: string; parent?: string | null; dirs?: FolderRow[] }>(
  '/api/folders' + (path ? '?path=' + encodeURIComponent(path) : ''));
export const getTemplates = () => api<{ templates?: TemplateRow[] }>('/api/templates');
export const createProject = (body: { name: string; parent: string; repo?: string; template?: string }) =>
  post<{ ok?: boolean; workspace?: string; name?: string; files?: number }>('/api/project', body);
export const readFile = (path: string) =>
  api<{ path?: string; content?: string; error?: string }>('/api/file?path=' + encodeURIComponent(path));
export const serverLogs = (name: string, tail = 300) =>
  api<{ name?: string; log?: string; lines?: string[]; total?: number }>(
    '/api/servers/logs?name=' + encodeURIComponent(name) + '&tail=' + tail
  );

// ── writes ──────────────────────────────────────────────────────────────────
export const resolveApproval = (id: string, approved: boolean, remember: string[]) =>
  post<{ ok?: boolean; decision?: string }>('/api/approvals/resolve', { id, approved, remember });
export const deleteInboxItem = (name: string) => post<{ ok?: boolean }>('/api/inbox/delete', { name });
export const stopServer = (name: string) => post<{ ok?: boolean }>('/api/servers/stop', { name });
/* Both answer data rather than HTTP codes: commit returns git's own output, which
   is how "nothing to commit" reaches the drawer as a sentence. */
export const gitInit = () => post<{ ok?: boolean; output?: string }>('/api/git/init');
export const gitCommit = (message: string) =>
  post<{ output?: string }>('/api/git/commit', { message });
/* Recreates the sandbox (docker container or namespace rootfs). Answers data, not
   HTTP codes: { ok, status } on success, { ok: false, error } when it cannot. */
export const restartSandbox = () =>
  post<{ ok?: boolean; status?: string; error?: string }>('/api/docker/restart');
/* A file the developer picked with the composer's +. Raw body + ?name=, so the
   engine needs no multipart parser; it answers the path it wrote. */
export const uploadFile = (name: string, body: Blob) =>
  api<{ ok?: boolean; path?: string; bytes?: number; error?: string }>(
    '/api/upload?name=' + encodeURIComponent(name),
    { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body });

/* New session = an empty transcript. Deliberately NOT POST /api/reset: that
   endpoint is LocalCode's workspace wipe (it deletes every file in WORKSPACE
   when you pass confirm:"yes"), and a "New" button must never eat the project. */
export const resetSession = () =>
  post<{ ok?: boolean; id?: string }>('/api/session', { messages: [] });
/* A save names its session, so an empty thread cannot be read as "start a new
   one" and archive the conversation that is actually open. */
export const saveSession = (messages: ChatMessage[], id = '') =>
  post<{ ok?: boolean; id?: string }>('/api/session', id ? { messages, id } : { messages });
export const loadSession = () => api<{ id?: string; messages?: ChatMessage[] }>('/api/session');
/* Session history: /api/sessions lists past conversations, /api/sessions/open
   points the engine at one and answers its transcript. */
export const getSessions = () => api<SessionsResponse>('/api/sessions', { cache: 'no-store' });
export const openSession = (id: string) =>
  post<{ id?: string; title?: string; messages?: ChatMessage[] }>('/api/sessions/open', { id });

/* The turn body. This is the one call that must NOT go through api(): the
   response is an SSE stream, and api() would try to parse it as JSON. */
export function openChatStream(
  messages: ChatMessage[],
  model: string | null,
  signal: AbortSignal
): Promise<Response> {
  return fetch(baseUrl + '/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    signal,
    body: JSON.stringify({ messages, model: model || null, base_url: baseUrl || null })
  });
}

/* Split an SSE buffer into events. Yields every complete `data: {...}` frame;
   a malformed frame is skipped rather than killing the turn. */
export async function* readStream(res: Response): AsyncGenerator<StreamEvent> {
  if (!res.body) throw new Error('no stream from /api/chat');
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  for (;;) {
    const read = await reader.read();
    if (read.done) break;
    buf += decoder.decode(read.value, { stream: true });
    let nl = buf.indexOf('\n\n');
    while (nl >= 0) {
      const raw = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 2);
      nl = buf.indexOf('\n\n');
      if (!raw.startsWith('data:')) continue;
      let ev: StreamEvent;
      try { ev = JSON.parse(raw.slice(5).trim()) as StreamEvent; } catch { continue; }
      yield ev;
    }
  }
}
