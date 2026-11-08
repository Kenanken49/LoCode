/* Shared shapes between the LOCODE engine (locode/server.py) and this shell.
   Every field mirrors a JSON key the server actually sends — checked against
   /api/health, /api/models, /api/files, /api/approvals, /api/inbox, /api/servers. */

export type StatusKind = 'idle' | 'think' | 'run' | 'err' | 'ok';

export interface ProjectInfo {
  name?: string;
  stack?: string[];
  frameworks?: string[];
  dev_command?: string;
  files?: number;
}

export interface BrainInfo {
  engine?: boolean;
  tier?: string;
  ram_gb?: number;
  available?: number;
  total?: number;
  recommended?: string;
  error?: string;
}

export interface SandboxStatus {
  enabled?: boolean;
  mode?: string;               // "off" | "docker" | "namespace"
  requested?: string;          // "auto" | the explicit SANDBOX value
  available?: boolean;
  running?: boolean;
  why?: string;
  limits?: string;             // "cgroups" | "rlimit+affinity"; absent -> no caps
  memory?: string;
  cpus?: string;
  image?: string;
  rootfs?: string;
}

export interface Health {
  app?: string;
  ok?: boolean;
  project?: ProjectInfo;
  network?: string;            // "on (ask)" | "on (auto-safe)" | "off"
  network_mode?: string;       // "ask" | "auto-safe" | "off"
  approval_timeout?: number;
  workspace?: string;
  inbox?: string;
  brain?: BrainInfo;
  sandbox?: SandboxStatus;
}

/* The local llama.cpp server behind the composer's GGUF. `health` is a probe of
   /health, not a memory: "unreachable" is the honest "stopped", and `managed`
   says whether the pid in the engine's state file is one it spawned itself. */
export interface LocalModelCandidate {
  path: string;
  name: string;                // the file name, which is what a row shows
  bytes: number;
  dir?: string;                // the LLAMA_MODELS directory it came from
}

export interface LocalModelStatus {
  url?: string;
  running?: boolean;
  health?: string;             // "ready" | "loading" | "unreachable"
  served?: string[];
  managed?: boolean;
  pid?: number | null;
  started?: string | null;
  bin?: string;
  bin_source?: string;         // "LLAMA_BIN" | "PATH" | "models/" | "missing"
  bin_exists?: boolean;
  model?: string;
  model_exists?: boolean;
  models?: string[];           // the same paths, flat, for the input's datalist
  candidates?: LocalModelCandidate[];
  model_dir?: string;
  model_dirs?: string[];
  log?: string;
  ok?: boolean;
  already?: boolean;
  starting?: boolean;
  stopped?: boolean;
  switched?: boolean;
  error?: string;
}

export interface ModelRow {
  id: string;
  label?: string;
  provider?: string;
  kind?: string;
  context?: number;
  params_b?: number;
  tools?: boolean;
  available?: boolean;
  fits?: boolean;
  why?: string;
  note?: string;
  required_ram_gb?: number;
}

/* One provider key, as /api/keys reports it: the name is the env var the catalog
   reads, and the value never leaves the engine. */
export interface KeyRow {
  name: string;
  set?: boolean;
  source?: string;             // "env" | "saved" | ""
}

export interface FolderRow {
  name: string;
  path: string;
}

export interface TemplateRow {
  id: string;
  label: string;
  files?: string[];
}

export interface WorkspaceInfo {
  workspace?: string;
  name?: string;
  parent?: string;
  projects_root?: string;
}

/* /api/git/status: `initialized: false` alone when there is no repository, and
   `status` is `git status --short` verbatim. */
export interface GitStatus {
  initialized?: boolean;
  status?: string;
  branch?: string;
  /* LOCODE's own records that are being kept out of git (the transcript and its
     history), named only when they exist — the drawer says why they are absent. */
  excluded?: string[];
}

/* One past conversation from /api/sessions: `title` is the first thing the
   developer said, `messages` how long the transcript is and `updated` a unix
   timestamp, so a command-center row can be drawn without opening it. */
export interface SessionRow {
  id: string;
  title: string;
  messages: number;
  updated: number;
}

export interface SessionsResponse {
  sessions?: SessionRow[];
  current?: string;             // the conversation that is open right now
}

export interface UnavailableModel {
  id: string;
  provider?: string;
  why?: string;
}

export interface ModelsResponse {
  models?: ModelRow[];          // only models that can answer right now
  default?: string;
  error?: string;
  unavailable?: UnavailableModel[];   // real providers that need a key/server
  hint?: string | null;               // what to do when nothing is usable
}

export interface ChatMessage {
  role: string;
  content: string;
}

export interface ToolRow {
  id: string;
  name: string;
  args: Record<string, unknown>;
  state: 'running' | 'ok' | 'err';
  result: string;
  open: boolean;
}

export interface Msg {
  id: number;
  role: 'user' | 'assistant' | 'error';
  text: string;
  tools: ToolRow[];
}

export interface ApprovalReq {
  id: string;
  action: string;
  url?: string;
  host?: string;
  detail?: string;
  created?: string;
  expires?: string;
  remaining?: number;
  decision?: string | null;
}

export interface ApprovalsResponse {
  approvals?: ApprovalReq[];
  mode?: string;
  remembered?: { domains?: string[]; actions?: string[] };
}

export interface InboxItem {
  name: string;
  kind: string;
  bytes: number;
  created: string;
}

export interface ServerRow {
  id?: string;
  name: string;
  command?: string;
  port: number;
  state: string;
  url?: string;
  log?: string;
  started?: string;
}

export type StreamEvent =
  | { type: 'token'; text?: string }
  | { type: 'tool_call'; id?: string; name?: string; args?: Record<string, unknown> }
  | { type: 'tool_result'; id?: string; result?: string }
  | { type: 'file_changed'; path?: string }
  | { type: 'files_changed'; paths?: string[] }
  | { type: 'server_started'; name?: string; port?: number }
  | { type: 'error'; text?: string }
  | { type: 'done'; model?: string };
