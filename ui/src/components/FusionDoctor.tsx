/* The Doctor view: the design's dual-stream trace, run against the engine that is
   actually there.

   The design compares a local daemon with a cloud MicroVM and prints fixed
   numbers for both. This build has one runtime, so the split is the browser that
   dispatches the checks and the Python engine that answers them — two sides of a
   real round trip, with the latency measured here rather than written in. The
   cloud pane is not faked: there is no cloud to report on.

   Every line is a real response. A check that fails prints the engine's own
   error and turns the footer badge red. */

import { useEffect, useRef, useState } from 'react';
import * as http from '../api';
import { useApp } from '../store';

type Side = 'client' | 'engine';
type Mode = 'split' | 'client' | 'engine';

interface Line {
  key: number;
  side: Side;
  tag: string;
  text: string;
  ok: boolean;
  ms: number;
  final?: boolean;
}

interface Check {
  tag: string;
  run: () => Promise<string>;
}

const errText = (e: unknown): string => (e instanceof Error ? e.message : String(e));

const stamp = (ms: number): string => '[' + (ms < 1 ? '<1ms' : Math.round(ms) + 'ms') + ']';

/* The engine's own answers, each one an endpoint the shell already uses. */
function checks(): Check[] {
  return [
    {
      tag: 'HEALTH',
      run: async () => {
        const h = await http.getHealth();
        return 'ok · ' + (h.project?.name || 'workspace') + ' · net ' + (h.network || '?')
          + ' · sandbox ' + (h.sandbox?.mode || 'off');
      }
    },
    {
      tag: 'GIT',
      run: async () => {
        const g = await http.getGitStatus();
        if (!g.initialized) return 'no repository — git is not initialized here';
        const entries = (g.status || '').split('\n').filter(Boolean).length;
        return (g.branch || 'detached') + ' · ' + entries + ' changed entr' + (entries === 1 ? 'y' : 'ies');
      }
    },
    {
      tag: 'MODELS',
      run: async () => {
        const r = await http.getModels();
        const rows = r.models || [];
        return rows.length
          ? rows.length + ' ready · ' + rows.map((m) => m.id).slice(0, 3).join(', ')
          : 'none ready · ' + (r.hint || r.error || 'no provider');
      }
    },
    {
      tag: 'SANDBOX',
      run: async () => {
        const s = await http.getSandbox();
        const caps = [s.memory, s.cpus && s.cpus + 'c'].filter(Boolean).join('/');
        return (s.mode || 'off') + (caps ? ' · ' + caps : '')
          + (s.limits ? ' · ' + s.limits : '')
          + (s.available === false ? ' · unavailable: ' + (s.why || '?') : '');
      }
    },
    {
      tag: 'SERVERS',
      run: async () => {
        const r = await http.getServers();
        const rows = r.servers || r.rows || [];
        return rows.length
          ? rows.map((s) => s.name + ':' + s.port).join(', ')
          : 'none running';
      }
    },
    {
      tag: 'SESSIONS',
      run: async () => {
        const r = await http.getSessions();
        const rows = r.sessions || [];
        return rows.length + ' saved' + (r.current ? ' · open ' + r.current : '');
      }
    },
    {
      tag: 'FILES',
      run: async () => {
        const r = await http.getFiles();
        const rows = r.paths || r.files || [];
        return rows.length + ' in the workspace';
      }
    }
  ];
}

export function FusionDoctor() {
  const baseUrl = useApp((s) => s.baseUrl);
  const [lines, setLines] = useState<Line[]>([]);
  const [running, setRunning] = useState(false);
  const [strict, setStrict] = useState(false);
  const [mode, setMode] = useState<Mode>('split');
  const [total, setTotal] = useState<number | null>(null);
  const [failed, setFailed] = useState(0);
  const [note, setNote] = useState('');
  const seq = useRef(0);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (bottom.current) bottom.current.scrollIntoView({ block: 'nearest' });
  }, [lines]);

  const push = (side: Side, tag: string, text: string, ok: boolean, ms: number, final = false): void => {
    seq.current += 1;
    const key = seq.current;
    setLines((prev) => [...prev, { key, side, tag, text, ok, ms, final }]);
  };

  const run = async (): Promise<void> => {
    if (running) return;
    setRunning(true);
    setLines([]);
    setFailed(0);
    setTotal(null);
    setNote('');
    seq.current = 0;

    const started = performance.now();
    push('client', 'ENV', 'viewport ' + window.innerWidth + '×' + window.innerHeight
      + ' @' + window.devicePixelRatio + 'x · online ' + (navigator.onLine ? 'yes' : 'no'), true, 0);
    push('client', 'ENGINE', (baseUrl || window.location.origin) + ' · dispatched 7 checks', true, 0);

    let failures = 0;
    for (const check of checks()) {
      const t0 = performance.now();
      try {
        const detail = await check.run();
        push('engine', check.tag, detail, true, performance.now() - t0);
      } catch (e) {
        failures += 1;
        push('engine', check.tag, errText(e), false, performance.now() - t0);
        setFailed(failures);
        if (strict) {
          push('engine', 'STRICT', 'stopped after the first failure', false, performance.now() - started);
          break;
        }
      }
    }

    const elapsed = performance.now() - started;
    setTotal(Math.round(elapsed));
    setFailed((n) => {
      const count = Math.max(n, failures);
      push('engine', 'EXIT', failures === 0
        ? 'all checks passed · code 0'
        : failures + ' check' + (failures === 1 ? '' : 's') + ' failed',
      failures === 0, elapsed, true);
      return count;
    });
    setRunning(false);
  };

  const copy = async (): Promise<void> => {
    const text = lines
      .map((l) => stamp(l.ms) + ' [' + l.tag + '] ' + l.text)
      .join('\n');
    try {
      await navigator.clipboard.writeText(text);
      setNote('copied ' + lines.length + ' lines');
    } catch (e) {
      setNote('copy failed: ' + errText(e));
    }
  };

  const shown = lines.filter((l) => mode === 'split' || l.side === mode);
  const client = shown.filter((l) => l.side === 'client');
  const engine = shown.filter((l) => l.side === 'engine');
  const state = running ? 'run' : total === null ? 'idle' : failed ? 'err' : 'ok';
  const badge = running ? 'STREAMING TRACE' : total === null ? 'DAEMON STANDBY'
    : failed ? 'CRITICAL FAULT' : 'ALL CHECKS PASSED';

  const pane = (side: Side, rows: Line[], title: string, sub: string) => (
    <div className="fx-pane" data-side={side}>
      <div className="fx-pane-head">{title}<span>{sub}</span></div>
      <div className="fx-pane-body">
        {rows.length === 0 && <span className="fx-pane-empty">// no output yet</span>}
        {rows.map((l) => (
          <div key={l.key} className="fx-line" data-ok={l.ok ? '1' : '0'} data-final={l.final ? '1' : '0'}>
            <span className="t">{stamp(l.ms)}</span> <span className="tag">[{l.tag}]</span> {l.text}
          </div>
        ))}
        <div ref={bottom} />
      </div>
    </div>
  );

  return (
    <div className="fx-doc">
      <div className="fx-doc-head">
        <div className="fx-doc-title">
          <h3>
            locode doctor
            <span className="fx-pill" data-k="fusion">daemon diagnostic</span>
            <span className="fx-pill" data-k={state}>{badge}</span>
          </h3>
          <p>Socket, repository, model, sandbox, server and tree checks, run now against this engine.</p>
        </div>
        <div className="fx-tabs" role="tablist" aria-label="Trace layout">
          {(['split', 'client', 'engine'] as const).map((m) => (
            <button key={m} className="fx-tab" type="button" role="tab" aria-selected={mode === m}
              onClick={() => setMode(m)}>
              {m === 'split' ? 'Split' : m === 'client' ? 'Client' : 'Engine'}
            </button>
          ))}
        </div>
      </div>

      <div className="fx-flags">
        <label><input type="checkbox" checked={strict} onChange={() => setStrict((v) => !v)} /> --strict</label>
        <span className="sep">|</span>
        <span>7 checks · {baseUrl || 'same origin'}</span>
        <span className="sep">|</span>
        <button id="fx-doctor-run" className="fx-btn fx-btn-primary" type="button"
          disabled={running} onClick={() => { void run(); }}>
          {running ? 'Streaming trace…' : 'Run engine trace'}
        </button>
        <button id="fx-doctor-copy" className="fx-btn" type="button" disabled={!lines.length}
          onClick={() => { void copy(); }}>
          Copy logs
        </button>
        {note && <span className="fx-note">{note}</span>}
      </div>

      <div className="fx-term" id="fx-trace" data-mode={mode}>
        {(mode === 'split' || mode === 'client') && pane('client', mode === 'split' ? client : shown,
          'Client', 'browser · ' + window.devicePixelRatio + 'x')}
        {(mode === 'split' || mode === 'engine') && pane('engine', mode === 'split' ? engine : shown,
          'Engine', (baseUrl || 'same origin'))}
      </div>

      <div className="fx-doc-foot">
        <span>
          checks <b>{engine.filter((l) => l.tag !== 'EXIT').length}</b> ·
          failed <b className={failed ? 'bad' : 'ok'}>{failed}</b> ·
          elapsed <b>{total === null ? '—' : total + 'ms'}</b>
        </span>
        <span>
          no cloud worker in this build — the trace is this engine, not a remote replica
        </span>
      </div>
    </div>
  );
}
