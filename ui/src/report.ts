/* The diagnostic report the command center exports.

   Pure on purpose: every value comes in as an argument, including the
   timestamp, so the same input always produces the same bytes and a test can
   assert on them. The component measures the digest of exactly this text, which
   is what makes the signature line in the modal meaningful rather than decorative. */

import type { ModelRow, Msg, SessionRow } from './types';

export type ReportFormat = 'markdown' | 'json';

/* The export's three buttons. `pdf` is not a fourth report: it is the Markdown
   report laid out by `pdf.ts`, which is why the builder never takes it. */
export type ExportFormat = ReportFormat | 'pdf';

/* What the export covers. The design calls them "sections"; here each one is a
   real slice of the state the engine is holding. */
export interface ReportSections {
  transcript: boolean;
  changes: boolean;
  environment: boolean;
  models: boolean;
}

export const ALL_SECTIONS: ReportSections = {
  transcript: true, changes: true, environment: true, models: true
};

export interface ReportInput {
  at: string;                  // ISO timestamp, passed in so output is deterministic
  project: string;
  workspace: string;
  engine: string;              // base URL, or the same-origin path
  model: string;
  sandbox: string;
  net: string;
  branch: string;
  initialized: boolean;
  status: string;              // git status --short, verbatim
  messages: Msg[];
  models: ModelRow[];
  sessions: SessionRow[];
  server: { name: string; port: number };
}

const clean = (text: string): string => text.replace(/\r/g, '').replace(/[ \t]+$/gm, '');

/* One line per message in the heading table: a transcript with newlines inside a
   list item is unreadable, and the full text is what the session file already
   holds. */
const flat = (text: string): string => clean(text).replace(/\s*\n+\s*/g, ' ⏎ ').trim();

export function buildReport(input: ReportInput, format: ReportFormat, sections: ReportSections): string {
  const transcript = sections.transcript
    ? input.messages.filter((m) => m.role !== 'error' && m.text.trim())
    : [];
  const models = sections.models ? input.models : [];
  const changed = sections.changes ? input.status.split('\n').map((l) => l.trimEnd()).filter(Boolean) : [];

  if (format === 'json') {
    const body = {
      report: 'locode-diagnostic',
      generated_at: input.at,
      project: input.project,
      workspace: input.workspace,
      engine: input.engine,
      model: input.model,
      sections: { ...sections },
      environment: sections.environment
        ? { sandbox: input.sandbox, net: input.net, branch: input.branch, repository: input.initialized, server: input.server }
        : null,
      changes: sections.changes ? { initialized: input.initialized, branch: input.branch, entries: changed } : null,
      models: sections.models
        ? models.map((m) => ({ id: m.id, provider: m.provider || '', available: Boolean(m.available) }))
        : null,
      transcript: sections.transcript
        ? transcript.map((m) => ({ role: m.role, text: clean(m.text) }))
        : null,
      sessions: sections.transcript
        ? input.sessions.map((s) => ({ id: s.id, title: s.title, messages: s.messages }))
        : null
    };
    return JSON.stringify(body, null, 2) + '\n';
  }

  const out: string[] = [
    '# LOCODE // DIAGNOSTIC REPORT',
    '',
    'generated: ' + input.at,
    'project:   ' + (input.project || '—'),
    'workspace: ' + (input.workspace || '—'),
    'engine:    ' + input.engine,
    'model:     ' + (input.model || '—'),
    ''
  ];

  let n = 1;
  if (sections.environment) {
    out.push(
      '## ' + (n++) + '. ENVIRONMENT',
      '',
      '- sandbox: ' + (input.sandbox || '—'),
      '- network: ' + input.net,
      '- repository: ' + (input.initialized ? 'git · ' + (input.branch || 'detached') : 'none'),
      '- preview: ' + (input.server.name ? input.server.name + ':' + input.server.port : 'not running'),
      ''
    );
  }
  if (sections.changes) {
    out.push('## ' + (n++) + '. WORKING TREE', '');
    if (!input.initialized) out.push('no repository — git is not initialized in this workspace');
    else if (!changed.length) out.push('clean — nothing modified since HEAD');
    else {
      out.push('`' + changed.length + ' entr' + (changed.length === 1 ? 'y' : 'ies') + '`', '', '```');
      out.push(...changed);
      out.push('```');
    }
    out.push('');
  }
  if (sections.models) {
    out.push('## ' + (n++) + '. MODELS', '');
    if (!models.length) out.push('none configured');
    else out.push(...models.map((m) =>
      '- `' + m.id + '` · ' + (m.provider || 'provider') + ' · ' + (m.available ? 'ready' : (m.why || 'unavailable'))));
    out.push('');
  }
  if (sections.transcript) {
    out.push('## ' + (n++) + '. SESSION TRANSCRIPT', '');
    if (!transcript.length) out.push('(the thread is empty)');
    else {
      for (const m of transcript) {
        out.push('**' + (m.role === 'user' ? 'developer' : 'agent') + '** — ' + flat(m.text), '');
      }
    }
    out.push('## ' + (n++) + '. SAVED SESSIONS', '');
    if (!input.sessions.length) out.push('no saved sessions yet');
    else out.push(...input.sessions.map((s) => '- ' + s.title + ' · ' + s.messages + ' msg' + (s.messages === 1 ? '' : 's')));
    out.push('');
  }

  out.push('---', 'locode · ' + input.at, '');
  return out.join('\n');
}
