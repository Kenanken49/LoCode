/* Settings and Integrations, the two panels the rail opens.

   Settings holds the actions that used to crowd the header (new session, theme,
   refresh) plus the engine base URL. Integrations is the connectivity story:
   provider keys, which model providers the engine can actually reach right now,
   and the quarantine inbox that downloads land in. The model picker itself lives
   in the composer, next to the prompt it applies to. */

import { useEffect, useRef, useState } from 'react';
import type { ChangeEvent, KeyboardEvent, ReactNode } from 'react';
import { getLocalModel, startLocalModel, stopLocalModel } from '../api';
import {
  closePanel, newSession, refreshHealth, refreshInbox, refreshModels, refreshTree,
  saveProviderKey, setBaseUrl, setModel, toggleInbox, toggleTheme, useApp
} from '../store';
import type { LocalModelStatus, ModelRow } from '../types';

function Section({ title, hint, children }: { title: string; hint?: string; children?: ReactNode }) {
  return (
    <section className="panel-sec">
      <h3>{title}</h3>
      {hint && <p className="panel-note">{hint}</p>}
      {children}
    </section>
  );
}

function SettingsBody() {
  const baseUrl = useApp((s) => s.baseUrl);
  const theme = useApp((s) => s.theme);
  const [url, setUrl] = useState(baseUrl);

  const save = (): void => setBaseUrl(url.trim());

  return (
    <>
      <Section title="Session" hint="Clears the transcript and the agent's memory of it. Your files are not touched.">
        <button className="btn" type="button" onClick={() => { void newSession(); }}>New session</button>
      </Section>

      <Section title="Appearance">
        <button className="btn" type="button" onClick={toggleTheme}>
          Switch to {theme === 'light' ? 'dark' : 'light'} theme
        </button>
      </Section>

      <Section title="Refresh" hint="Re-reads the project, the models and the file tree.">
        <button className="btn" type="button" onClick={() => {
          void refreshHealth(); void refreshModels(); void refreshInbox(); void refreshTree();
        }}>Refresh everything</button>
      </Section>

      <Section title="Engine" hint="Empty drives this page's own engine. A URL drives a remote one.">
        <div className="panel-field">
          <input value={url} spellCheck={false} placeholder="http://127.0.0.1:8000"
            aria-label="Engine base URL"
            onChange={(e: ChangeEvent<HTMLInputElement>) => setUrl(e.target.value)}
            onKeyDown={(e: KeyboardEvent<HTMLInputElement>) => { if (e.key === 'Enter') save(); }} />
          <button className="btn" type="button" onClick={save}>Save</button>
        </div>
      </Section>
    </>
  );
}

/* Provider keys. The value goes one way: the engine stores it in
   .locode/keys.json (mode 600) and only ever reports whether one is set and
   where it came from, so nothing here can read a key back out. */
function KeysSection() {
  const keys = useApp((s) => s.keys);
  const busy = useApp((s) => s.keyBusy);
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  const draft = (name: string): string => drafts[name] || '';
  const setDraft = (name: string, value: string): void =>
    setDrafts((d) => ({ ...d, [name]: value }));

  const commit = (name: string): void => {
    const value = draft(name).trim();
    if (!value) return;
    void saveProviderKey(name, value).then(() => setDraft(name, ''));
  };

  return (
    <Section title="Provider keys"
      hint="Pasted keys live on this engine in .locode/keys.json (mode 600) and are never sent back to the browser. A key set in the environment wins.">
      {keys.length === 0 && <p className="panel-note">This engine reported no key-protected providers.</p>}
      {keys.map((k) => (
        <div className="key-row" key={k.name}>
          <div className="key-head">
            <span className="mono">{k.name}</span>
            <span className={'key-state' + (k.set ? ' on' : '')}>
              {k.set ? (k.source === 'env' ? 'from environment' : 'saved') : 'not set'}
            </span>
          </div>
          <div className="panel-field">
            <input type="password" autoComplete="off" spellCheck={false}
              aria-label={'API key for ' + k.name}
              placeholder={k.set ? '•••••••• (paste to replace)' : 'paste key'}
              value={draft(k.name)}
              onChange={(e: ChangeEvent<HTMLInputElement>) => setDraft(k.name, e.target.value)}
              onKeyDown={(e: KeyboardEvent<HTMLInputElement>) => { if (e.key === 'Enter') commit(k.name); }} />
            <button className="btn" type="button"
              disabled={busy === k.name || !draft(k.name).trim()}
              onClick={() => commit(k.name)}>{busy === k.name ? '…' : 'Save'}</button>
            {k.set && k.source !== 'env' && (
              <button className="btn btn-ghost" type="button" disabled={busy === k.name}
                onClick={() => { void saveProviderKey(k.name, ''); }}>Clear</button>
            )}
          </div>
        </div>
      ))}
    </Section>
  );
}

const fileOf = (path: string): string => path.split(/[\\/]/).pop() || path;
const sameModel = (left: string, right: string): boolean =>
  !!left && !!right && (left === right || fileOf(left) === fileOf(right));
const fileSize = (bytes?: number): string =>
  !bytes ? '' : bytes >= 1e9 ? (bytes / 1e9).toFixed(1) + ' GB' : Math.round(bytes / 1e6) + ' MB';

/* The local model server. The GGUF the composer lists is served by llama.cpp on
   this machine, and starting it used to mean a terminal and three remembered
   flags. The engine resolves both paths and owns the pid it spawns; this shows
   what it resolved, lets either path be corrected, and prints the tail of the
   server's own log — "it did not start" is only useful with the reason. */
function LocalModelSection() {
  const [status, setStatus] = useState<LocalModelStatus | null>(null);
  const [bin, setBin] = useState('');
  const [model, setModel] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  /* Every action takes a ticket. A start's follow-up polls take seconds; without
     this, one that resolves after Stop painted "ready" over a server that was
     already gone. A stale reply is dropped instead of applied. */
  const ticket = useRef(0);

  const apply = (next: LocalModelStatus): void => {
    setStatus(next);
    if (next.bin) setBin((b) => b || next.bin || '');
    // what is actually being served wins over what the engine would start: a
    // server launched elsewhere must not be shown under the wrong file name
    const serving = next.served?.[0];
    if (serving) setModel((m) => m || serving || next.model || '');
    else if (next.model) setModel((m) => m || next.model || '');
  };

  useEffect(() => {
    const mine = ticket.current;
    getLocalModel()
      .then((next) => { if (ticket.current === mine) apply(next); })
      .catch((e: unknown) => setError(String(e)));
  }, []);

  const start = async (): Promise<void> => {
    const mine = ++ticket.current;
    // start vs switch is decided by what was on screen when the click landed: the
    // follow-up polls outlive the click, and relabelling the button from a row the
    // user selected afterwards claimed a switch that had not been asked for
    setBusy(switchNeeded ? 'switch' : 'start');
    setError('');
    try {
      const reply = await startLocalModel(bin.trim(), model.trim());
      if (ticket.current !== mine) return;
      apply(reply);
      if (!reply.ok) { setError(reply.error || 'the engine did not start a server'); return; }
      await refreshModels();
      // llama-server answers 503 while it loads the weights: follow it to ready
      // instead of calling it up because a pid exists.
      const deadline = Date.now() + 60_000;
      while (Date.now() < deadline) {
        await new Promise((done) => setTimeout(done, 1200));
        if (ticket.current !== mine) return;
        const next = await getLocalModel();
        if (ticket.current !== mine) return;
        apply(next);
        if (next.running) { await refreshModels(); return; }
        if (next.health === 'unreachable' && !next.managed) return;
      }
      setError('the server is still loading after 60s — see the log below');
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy('');
    }
  };

  const stop = async (): Promise<void> => {
    const mine = ++ticket.current;          // cancels a start's pending polls
    setBusy('stop');
    setError('');
    try {
      const reply = await stopLocalModel();
      if (ticket.current !== mine) return;
      apply(reply);
      if (!reply.ok) setError(reply.error || 'the engine did not stop a server');
      await refreshModels();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy('');
    }
  };

  const label = !status ? 'reading…'
    : status.running ? 'ready'
      : status.health === 'loading' || status.managed ? 'loading'
        : 'stopped';

  const serving = status?.served?.[0] || '';
  const switchNeeded = !!status?.running && !!model && !sameModel(model, serving);

  return (
    <Section title="Local model server"
      hint="One llama.cpp server per model: pick a GGUF below and start it. Choosing another one reloads the server with that file, and only a server this panel started is ever stopped.">
      <div className="lm-head">
        <span className={'dot' + (status?.running ? ' on' : '')} />
        <span className="mono">{status?.url || '—'}</span>
        <span className={'lm-state' + (status?.running ? ' on' : '')}>{label}</span>
      </div>
      {status?.served?.length ? (
        <p className="panel-note mono">{status.served.join(' · ')}</p>
      ) : null}
      <label className="lm-field">
        <span>server binary</span>
        <input value={bin} spellCheck={false} placeholder="llama-server"
          onChange={(e: ChangeEvent<HTMLInputElement>) => setBin(e.target.value)} />
      </label>
      {(status?.candidates?.length ?? 0) > 0 && (
        <div className="lm-models" role="group" aria-label="GGUF models found on this machine">
          {(status?.candidates || []).map((row) => {
            const isServing = !!serving && sameModel(row.path, serving);
            return (
              <button key={row.path} type="button" className="lm-model" title={row.path}
                aria-current={sameModel(row.path, model) ? 'true' : undefined}
                onClick={() => setModel(row.path)}>
                <span className={'dot' + (isServing ? ' on' : '')} />
                <span className="lm-model-name">{row.name}</span>
                <span className="lm-model-size">{isServing ? 'serving' : fileSize(row.bytes)}</span>
              </button>
            );
          })}
        </div>
      )}
      <label className="lm-field">
        <span>{status?.candidates?.length ? 'or another .gguf path' : 'model (.gguf)'}</span>
        <input value={model} spellCheck={false} list="lm-models"
          placeholder={status?.model_dir || 'model.gguf'}
          onChange={(e: ChangeEvent<HTMLInputElement>) => setModel(e.target.value)} />
        <datalist id="lm-models">
          {(status?.models || []).map((path) => <option key={path} value={path} />)}
        </datalist>
      </label>
      <div className="lm-actions">
        {/* disabled until the first read lands: an empty path pair would be a
            click that starts whatever the engine guessed, not what is shown */}
        <button className="btn" type="button"
          disabled={busy !== '' || !status || (!!status.running && !switchNeeded)}
          onClick={() => { void start(); }}>
          {busy === 'switch' ? 'Switching…' : busy === 'start' ? 'Starting…'
            : switchNeeded ? 'Switch model' : 'Start server'}
        </button>
        <button className="btn btn-ghost" type="button" disabled={busy !== '' || !status?.managed}
          onClick={() => { void stop(); }}>{busy === 'stop' ? 'Stopping…' : 'Stop'}</button>
      </div>
      {status?.running && !status.managed && (
        <p className="panel-note">started outside LOCODE — left running, because this panel stops only its own</p>
      )}
      {error && <p className="lm-err">{error}</p>}
      {/* collapsed: the log is the diagnosis, not the control — open it and the
          section's status row does not scroll away under the panel header */}
      {status?.log ? (
        <details className="lm-log-wrap">
          <summary>server log · .locode/llama-server.log</summary>
          <pre className="lm-log">{status.log}</pre>
        </details>
      ) : null}
    </Section>
  );
}

function IntegrationsBody() {
  const models = useApp((s) => s.models);
  const model = useApp((s) => s.model);
  const inbox = useApp((s) => s.inbox);

  const groups = new Map<string, ModelRow[]>();
  for (const row of models) {
    const key = row.provider || 'other';
    const list = groups.get(key);
    if (list) list.push(row); else groups.set(key, [row]);
  }

  return (
    <>
      <LocalModelSection />
      <KeysSection />

      <Section title="Model providers" hint="What this engine can reach right now. Selecting one uses it for the next turn.">
        {models.length === 0 && <p className="panel-note">The engine has not reported any models yet.</p>}
        {[...groups].map(([provider, rows]) => (
          <div className="prov" key={provider}>
            <div className="prov-head">
              <span>{provider}</span>
              <span className="faint">{rows.filter((r) => r.available).length}/{rows.length} ready</span>
            </div>
            {rows.map((row) => (
              <button key={row.id} type="button" className="prov-row" disabled={!row.available}
                aria-current={row.id === model ? 'true' : undefined}
                title={(row.note || row.id) + (row.why ? ' — ' + row.why : '')}
                onClick={() => { setModel(row.id); closePanel(); }}>
                <span className={'dot' + (row.available ? ' on' : '')} />
                <span className="prov-name">{row.label || row.id}</span>
                <span className="prov-why">
                  {row.available ? (row.id === model ? 'in use' : 'ready') : (row.why || 'unavailable')}
                </span>
              </button>
            ))}
          </div>
        ))}
      </Section>

      <Section title="Inbox" hint="Downloads and clones wait here, quarantined, until you approve an import.">
        <button className="btn" type="button" onClick={toggleInbox}>
          {inbox.items.length} item{inbox.items.length === 1 ? '' : 's'} · open inbox
        </button>
      </Section>

      <Section title="Uploads" hint="The + in the composer writes into uploads/ in the workspace and puts the paths in your prompt." />
    </>
  );
}

export function Panels() {
  const view = useApp((s) => s.view);
  if (view === 'home') return null;
  const settings = view === 'settings';
  return (
    <>
      <div id="panel-scrim" onClick={closePanel} />
      <aside id="panel" aria-label={settings ? 'Settings' : 'Integrations'}>
        <div className="panel-head">
          <span>{settings ? 'Settings' : 'Integrations'}</span>
          <button className="icon-btn" type="button" title="Close" aria-label="Close"
            onClick={closePanel}>✕</button>
        </div>
        <div className="panel-body">
          {settings ? <SettingsBody /> : <IntegrationsBody />}
        </div>
      </aside>
    </>
  );
}
