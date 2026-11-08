/* The Fusion command center (⌘K) — the Stitch workspace shell.

   It replaces the old floating palette: same rows, same keyboard model, but the
   chrome is the designed three-zone shell (sidebar / hero + omnibar / telemetry
   footer). Nothing here is decorative state — every row is derived from the live
   store, and a row that cannot work is disabled instead of pretending.

   Two departures from the design export, both deliberate and visible in the UI:

   - Its Cloud/Local switcher has no meaning in a local-only build, so the
     segmented control switches *views* (Console / Doctor / Export) instead of
     inventing a second runtime.
   - Its sidebar identity block (name, "My Team") is replaced by this workspace's
     project and engine, which are real.

   The doctor and export views are separate components because they talk to the
   engine on their own; the rows below only read the store. */

import { useEffect, useMemo, useRef } from 'react';
import type { ChangeEvent, KeyboardEvent } from 'react';
import {
  abortTurn, closeFusion, initRepo, newSession, openChanges, openFile, openFusion,
  openSessionById, refreshModels, refreshTree, setFusionIndex, setFusionQuery,
  setFusionView, setView, startPreview, stopServer, toggleInbox, togglePlanMode,
  toggleTheme, useApp
} from '../store';
import { FusionDoctor } from './FusionDoctor';
import { FusionExport } from './FusionExport';

interface Command {
  id: string;
  label: string;
  hint: string;
  group: 'run' | 'workspace' | 'git' | 'view';
  enabled?: boolean;
  run: () => void;
}

const GLYPH: Record<Command['group'], string> = { run: '▸', workspace: '⌗', git: '±', view: '◈' };

const hit = (text: string, query: string): boolean =>
  text.toLowerCase().includes(query.toLowerCase());

const when = (updated: number): string => {
  if (!updated) return '';
  const minutes = Math.max(0, Math.round((Date.now() / 1000 - updated) / 60));
  if (minutes < 1) return 'just now';
  if (minutes < 60) return minutes + 'm ago';
  const hours = Math.round(minutes / 60);
  return hours < 24 ? hours + 'h ago' : Math.round(hours / 24) + 'd ago';
};

export function Fusion() {
  const fusion = useApp((s) => s.fusion);
  const planMode = useApp((s) => s.planMode);
  const busy = useApp((s) => s.busy);
  const server = useApp((s) => s.server);
  const files = useApp((s) => s.files);
  const repo = useApp((s) => s.changes.initialized);
  const branch = useApp((s) => s.changes.branch);
  const changes = useApp((s) => s.changes.status);
  const sessions = useApp((s) => s.sessions.items);
  const openId = useApp((s) => s.sessions.current);
  const switching = useApp((s) => s.sessions.busy);
  const models = useApp((s) => s.models);
  const model = useApp((s) => s.model);
  const projectName = useApp((s) => s.project.name);
  const health = useApp((s) => s.health);
  const baseUrl = useApp((s) => s.baseUrl);
  const netMode = useApp((s) => s.netMode);
  const inputRef = useRef<HTMLInputElement>(null);

  const commands = useMemo<Command[]>(() => [
    { id: 'session.new', group: 'run', label: 'New session',
      hint: 'clears the thread and the agent memory', run: () => { void newSession(); } },
    { id: 'plan.toggle', group: 'run',
      label: planMode ? 'Turn plan mode off' : 'Turn plan mode on',
      hint: planMode ? 'the next turn may edit again' : 'the next turn only plans',
      run: togglePlanMode },
    { id: 'turn.stop', group: 'run', label: 'Stop the running turn',
      hint: busy ? 'aborts the stream' : 'nothing is running', enabled: busy, run: abortTurn },
    { id: 'preview.start', group: 'run', label: 'Start the preview server',
      hint: 'runs the detected dev command', run: startPreview },
    { id: 'server.stop', group: 'run', label: 'Stop the preview server',
      hint: server.name ? server.name + ':' + server.port : 'no server running',
      enabled: Boolean(server.name), run: () => { void stopServer(); } },
    { id: 'changes.open', group: 'git', label: 'Review changes & commit',
      hint: repo ? 'git status, then commit all' : 'no repository yet', run: openChanges },
    { id: 'git.init', group: 'git', label: 'Initialize git repository',
      hint: 'git init plus one commit', enabled: !repo, run: () => { void initRepo(); } },
    { id: 'tree.refresh', group: 'workspace', label: 'Refresh the file tree',
      hint: 're-reads the workspace listing', run: () => { void refreshTree(); } },
    { id: 'models.refresh', group: 'workspace', label: 'Re-read the model list',
      hint: 'what can answer right now', run: () => { void refreshModels(); } },
    { id: 'theme.toggle', group: 'view', label: 'Toggle the theme', hint: 'dark / light',
      run: toggleTheme },
    { id: 'inbox.open', group: 'view', label: 'Open the inbox',
      hint: 'quarantined downloads', run: toggleInbox },
    { id: 'view.settings', group: 'view', label: 'Settings',
      hint: 'session, appearance, engine URL', run: () => setView('settings') },
    { id: 'view.integrations', group: 'view', label: 'Integrations',
      hint: 'providers, keys, inbox', run: () => setView('integrations') },
    { id: 'view.home', group: 'view', label: 'Back to the chat',
      hint: 'close the panel', run: () => setView('home') }
  ], [planMode, busy, server.name, server.port, repo]);

  const query = fusion.query.trim();
  const commandQuery = query.startsWith('>') ? query.slice(1).trim() : query;

  const visible = useMemo(() => {
    if (!commandQuery) return commands;
    return commands.filter((c) =>
      hit(c.label, commandQuery) || hit(c.hint, commandQuery) || hit(c.group, commandQuery));
  }, [commands, commandQuery]);

  const fileMatches = useMemo(() => {
    if (!query || query.startsWith('>')) return [];
    return files.filter((p) => hit(p, query)).slice(0, 8);
  }, [files, query]);

  const resumes = useMemo(() => {
    if (query.startsWith('>')) return [];
    const q = query.trim().toLowerCase();
    return sessions.filter((s) => !q || hit(s.title, q)).slice(0, 6);
  }, [sessions, query]);

  const total = visible.length + resumes.length + fileMatches.length;
  const active = total ? Math.min(fusion.index, total - 1) : 0;

  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent): void => {
      if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault();
        if (fusion.open) closeFusion(); else openFusion();
        return;
      }
      if (e.key === 'Escape' && fusion.open) {
        e.preventDefault();
        closeFusion();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [fusion.open]);

  useEffect(() => {
    if (!fusion.open || fusion.view !== 'console') return;
    const el = inputRef.current;
    if (!el) return;
    el.focus();
  }, [fusion.open, fusion.view]);

  useEffect(() => {
    if (!fusion.open) return;
    const row = document.querySelector('#fusion-rows .fx-row[aria-selected="true"]');
    if (row) row.scrollIntoView({ block: 'nearest' });
  }, [fusion.open, active]);

  const run = (index: number): void => {
    if (index < visible.length) {
      const cmd = visible[index];
      if (!cmd || cmd.enabled === false) return;
      closeFusion();
      cmd.run();
      return;
    }
    const sessionIndex = index - visible.length;
    if (sessionIndex < resumes.length) {
      const row = resumes[sessionIndex];
      if (!row || row.id === openId || switching || busy) return;
      closeFusion();
      void openSessionById(row.id);
      return;
    }
    const path = fileMatches[index - visible.length - resumes.length];
    if (!path) return;
    closeFusion();
    void openFile(path);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>): void => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (total) setFusionIndex((active + 1) % total);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (total) setFusionIndex((active - 1 + total) % total);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      run(active);
    }
  };

  if (!fusion.open) return null;

  /* "Get started" is a readiness read, not a tutorial: each line is something
     the app can check, and the count is how many are already true. */
  const steps = [
    { title: 'Initialize the repository', done: repo },
    { title: 'Pick a model that can answer', done: models.length > 0 },
    { title: 'Save a session', done: sessions.length > 0 },
    { title: 'Start the preview server', done: Boolean(server.name) }
  ];
  const doneCount = steps.filter((s) => s.done).length;
  const changedCount = changes.split('\n').filter(Boolean).length;
  const sandboxMode = health?.sandbox?.mode || 'off';

  return (
    <section id="fusion" role="dialog" aria-modal="true" aria-label="Fusion command center">
      <aside className="fx-side">
        <div className="fx-brand">
          <span className="fx-mark" aria-hidden="true">LC</span>
          <span className="fx-brand-text">
            <span className="fx-brand-name">LoCode <em>Studio</em></span>
            <span className="fx-ver">fusion engine</span>
          </span>
        </div>

        <button id="fusion-new" className="fx-new" type="button"
          onClick={() => { closeFusion(); void newSession(); }}>
          <span className="g" aria-hidden="true">+</span> New session
        </button>

        <nav className="fx-nav" aria-label="Command center views">
          <button className="fx-nav-item" type="button" aria-current={fusion.view === 'console'}
            onClick={() => setFusionView('console')}>
            <span className="g" aria-hidden="true">⌘</span> Console
          </button>
          <button className="fx-nav-item" type="button" aria-current={fusion.view === 'doctor'}
            onClick={() => setFusionView('doctor')}>
            <span className="g" aria-hidden="true">✓</span> Doctor
          </button>
          <button className="fx-nav-item" type="button" aria-current={fusion.view === 'export'}
            onClick={() => setFusionView('export')}>
            <span className="g" aria-hidden="true">↓</span> Export report
          </button>
          <div className="fx-nav-sep" />
          <button className="fx-nav-item" type="button"
            onClick={() => { closeFusion(); openChanges(); }}>
            <span className="g" aria-hidden="true">±</span> Review changes
          </button>
          <button className="fx-nav-item" type="button"
            onClick={() => { closeFusion(); toggleInbox(); }}>
            <span className="g" aria-hidden="true">⧉</span> Inbox
          </button>
          <button className="fx-nav-item" type="button"
            onClick={() => { closeFusion(); setView('settings'); }}>
            <span className="g" aria-hidden="true">⚙</span> Settings
          </button>
          <button className="fx-nav-item" type="button"
            onClick={() => { closeFusion(); setView('integrations'); }}>
            <span className="g" aria-hidden="true">◈</span> Integrations
          </button>
        </nav>

        <div className="fx-side-cap"><span>Sessions</span><span>{sessions.length}</span></div>
        <div className="fx-sessions">
          {sessions.length === 0 && (
            <span className="fx-empty">no saved sessions yet</span>
          )}
          {sessions.slice(0, 8).map((row) => {
            const isOpen = row.id === openId;
            return (
              <button key={row.id} type="button" className="fx-session" title={row.title}
                disabled={isOpen || busy || switching}
                onClick={() => { closeFusion(); void openSessionById(row.id); }}>
                <span className="fx-session-title">{row.title}</span>
                <span className="fx-session-meta">
                  {isOpen ? 'open now' : row.messages + ' msg' + (row.updated ? ' · ' + when(row.updated) : '')}
                </span>
              </button>
            );
          })}
        </div>

        <div className="fx-side-foot">
          <span>project <b>{projectName || '—'}</b></span>
          <span>engine <b>{baseUrl || 'same origin'}</b></span>
        </div>
      </aside>

      <div className="fx-main">
        <header className="fx-hero">
          <div className="fx-hero-l">
            <span className="fx-mark" aria-hidden="true">LC</span>
            <span className="fx-hero-title">
              <h2>LoCode <em>Studio</em></h2>
              <span className="fx-hero-sub">fusion engine · command center</span>
            </span>
          </div>
          <div className="fx-tabs" role="tablist" aria-label="Command center view">
            {(['console', 'doctor', 'export'] as const).map((view) => (
              <button key={view} className="fx-tab" type="button" role="tab"
                aria-selected={fusion.view === view}
                onClick={() => setFusionView(view)}>
                {view === 'console' ? 'Console' : view === 'doctor' ? 'Doctor' : 'Export'}
              </button>
            ))}
          </div>
        </header>

        {fusion.view === 'console' && (
          <div className="fx-body">
            <div className="fx-omni">
              <div className="fx-omni-tip">
                <span>Search sessions, files and commands — <kbd>&gt;</kbd> for commands only</span>
                <span>{planMode ? 'plan mode on' : 'plan mode off'}</span>
              </div>
              <input id="fusion-input" className="fx-input" ref={inputRef} spellCheck={false}
                value={fusion.query} placeholder="Ask LoCode to build, diagnose, or orchestrate…"
                aria-label="Command, file or session"
                onChange={(e: ChangeEvent<HTMLInputElement>) => setFusionQuery(e.target.value)}
                onKeyDown={onKeyDown} />

              <div id="fusion-rows" className="fx-rows" role="listbox" aria-label="Results">
                {total === 0 && (
                  <p className="fx-empty">Nothing matches “{query}”.</p>
                )}
                {visible.length > 0 && <div className="fx-group">Commands</div>}
                {visible.map((cmd, i) => (
                  <button key={cmd.id} type="button" role="option" className="fx-row"
                    data-row="command" aria-selected={i === active}
                    disabled={cmd.enabled === false} title={cmd.hint}
                    onClick={() => { run(i); }}>
                    <span className="g" aria-hidden="true">{GLYPH[cmd.group]}</span>
                    <span className="label">{cmd.label}</span>
                    <span className="hint">{cmd.hint}</span>
                  </button>
                ))}
                {resumes.length > 0 && <div className="fx-group">Sessions</div>}
                {resumes.map((row, i) => {
                  const index = visible.length + i;
                  const isOpen = row.id === openId;
                  return (
                    <button key={'session:' + row.id} type="button" role="option" className="fx-row"
                      data-row="session" aria-selected={index === active} title={row.title}
                      disabled={isOpen || busy || switching} onClick={() => { run(index); }}>
                      <span className="g" aria-hidden="true">↺</span>
                      <span className="label">{row.title}</span>
                      <span className="hint">
                        {isOpen ? 'open now'
                          : row.messages + ' msg' + (row.messages === 1 ? '' : 's')
                            + (row.updated ? ' · ' + when(row.updated) : '')}
                      </span>
                    </button>
                  );
                })}
                {fileMatches.length > 0 && <div className="fx-group">Files</div>}
                {fileMatches.map((path, i) => {
                  const index = visible.length + resumes.length + i;
                  return (
                    <button key={'file:' + path} type="button" role="option" className="fx-row"
                      data-row="file" aria-selected={index === active} title={path}
                      onClick={() => { run(index); }}>
                      <span className="g" aria-hidden="true">·</span>
                      <span className="label mono">{path}</span>
                      <span className="hint">open</span>
                    </button>
                  );
                })}
              </div>

              <div className="fx-omni-foot">
                <span>↑↓ navigate · ⏎ run · esc close</span>
                <span>{total} row{total === 1 ? '' : 's'} · model <b>{model || 'none'}</b></span>
              </div>
            </div>

            <div className="fx-getstarted">
              <div className="fx-getstarted-head">
                <span>Get started</span>
                <span className="n">{doneCount} of {steps.length}</span>
              </div>
              {steps.map((step) => (
                <span key={step.title} className="fx-step" data-done={step.done ? '1' : '0'}>
                  <span className="g" aria-hidden="true">{step.done ? '✓' : '○'}</span>
                  {step.title}
                </span>
              ))}
            </div>
          </div>
        )}

        {fusion.view === 'doctor' && (
          <div className="fx-body"><FusionDoctor /></div>
        )}

        {fusion.view === 'export' && (
          <div className="fx-body">
            <FusionExport />
          </div>
        )}

        <footer className="fx-foot">
          <span className="fx-foot-l">
            <span>engine <b>{baseUrl || 'same origin'}</b></span>
            <span>sandbox <b>{sandboxMode}</b></span>
            <span>net <b>{netMode}</b></span>
            <span>git <b>{repo ? (branch || 'detached') : 'none'}</b></span>
            <span>changed <b>{changedCount}</b></span>
          </span>
          <span className="fx-pill" data-k={repo && models.length ? 'ok' : 'warn'}>
            {repo && models.length ? 'ready' : 'setup incomplete'}
          </span>
        </footer>
      </div>
    </section>
  );
}
