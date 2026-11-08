/* The composer zone (the form under the thread).

   Three things live here now: the model the next turn runs on (it belongs with
   the prompt, not in the header), a + that uploads files into the workspace and
   leaves their paths in the draft, and the prompt itself.

   Ctrl/⌘+Enter sends, Esc aborts the running turn, and the textarea grows with
   the draft up to 34% of the viewport — the height cap CSS cannot know about.
   Chunk 4 hands a path here through store.insert; the caret goes right after
   it, so typing continues where the click left off. */

import { useEffect, useRef } from 'react';
import type { ChangeEvent, FormEvent, KeyboardEvent } from 'react';
import {
  abortTurn, openFusion, setDraft, setModel, submitDraft, togglePlanMode, uploadFiles, useApp
} from '../store';
import type { Health, ModelRow, StatusKind } from '../types';

const KIND_DOT: Record<StatusKind, string> = {
  idle: 'var(--faint)', think: 'var(--accent)', run: 'var(--warn)', err: 'var(--err)', ok: 'var(--ok)'
};

/* The Freedom brain reports through the status pill's tooltip: which engine is
   wired up, how many local models actually fit, and why not, if they do not. */
function brainTitle(health: Health | null): string {
  const b = health?.brain;
  if (!b) return '';
  if (b.engine) {
    return 'Freedom brain: ' + (b.tier || '?') + ' · ' + (b.available || 0) + '/' + (b.total || 0)
      + ' models fit' + (b.recommended ? ' · recommended ' + b.recommended : '');
  }
  return 'brain: ' + (b.error || 'unavailable');
}

function modelTitle(row: ModelRow): string {
  return [row.label || row.id, row.provider, row.available ? 'ready' : (row.why || 'unavailable'),
    row.context ? 'ctx ' + row.context : '', row.required_ram_gb ? '~' + row.required_ram_gb + ' GB' : '']
    .filter(Boolean).join(' · ');
}

export function Composer() {
  const draft = useApp((s) => s.draft);
  const busy = useApp((s) => s.busy);
  const status = useApp((s) => s.status);
  const health = useApp((s) => s.health);
  const models = useApp((s) => s.models);
  const modelNote = useApp((s) => s.modelNote);
  const tps = useApp((s) => s.tps);
  const rates = useApp((s) => s.rates);
  const model = useApp((s) => s.model);
  const uploadBusy = useApp((s) => s.uploadBusy);
  const insert = useApp((s) => s.insert);
  const planMode = useApp((s) => s.planMode);
  const ref = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const usedNonce = useRef(0);

  // auto-grow, capped by CSS max-height
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight + 2, Math.round(window.innerHeight * 0.34)) + 'px';
  }, [draft]);

  useEffect(() => {
    if (!insert || insert.nonce === usedNonce.current) return;
    usedNonce.current = insert.nonce;
    const el = ref.current;
    if (!el) return;
    const start = el.selectionStart ?? el.value.length;
    const end = el.selectionEnd ?? el.value.length;
    const lead = start > 0 && !/\s$/.test(el.value.slice(0, start)) ? ' ' : '';
    const next = el.value.slice(0, start) + lead + insert.text + el.value.slice(end);
    setDraft(next);
    const pos = start + lead.length + insert.text.length;
    el.focus();
    requestAnimationFrame(() => el.setSelectionRange(pos, pos));
  }, [insert]);

  const onSubmit = (e: FormEvent<HTMLFormElement>): void => {
    e.preventDefault();
    submitDraft();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submitDraft(); }
    if (e.key === 'Escape' && busy) abortTurn();
  };

  /* Measured, not promised: live while a reply streams, and afterwards the rate
     that model actually held, so comparing two of them needs no stopwatch. */
  const lastRate = model ? rates[model] : undefined;
  const rateText = tps !== null
    ? tps.toFixed(1) + ' tok/s'
    : lastRate !== undefined ? 'last ' + lastRate.toFixed(1) + ' tok/s' : '';
  const rateTitle = tps !== null
    ? 'Live tokens per second, measured over the last 1.5 s · ' + (model || 'this model')
    : 'Last measured on ' + (model || 'this model');

  /* What animates: a turn thinking or a tool running. A finished turn or an
     error keeps its dot still in its own colour, and "idle" is not shown at all
     — the dot is the signal, so an idle platform has nothing to say. */
  const working = busy || status.kind === 'think' || status.kind === 'run';
  const label = status.text === 'idle' ? '' : status.text;

  const onPick = (e: ChangeEvent<HTMLInputElement>): void => {
    const files = Array.from(e.target.files || []);
    e.target.value = '';                    // picking the same file twice must work
    if (files.length) void uploadFiles(files);
  };

  return (
    <form id="composer" autoComplete="off" onSubmit={onSubmit}>
      <div className="composer-top">
        <select id="model-select" className="model-select" aria-label="Model for this thread"
          title={modelNote ? 'Model for this thread — ' + modelNote
            : 'Model for this thread — switched per turn'}
          value={model} onChange={(e: ChangeEvent<HTMLSelectElement>) => setModel(e.target.value)}>
          {/* only rows that can answer are here; the rest are named in the title */}
          {models.length === 0 && <option value="">{modelNote || '— no models —'}</option>}
          {models.map((row) => (
            <option key={row.id} value={row.id} disabled={!row.available} title={modelTitle(row)}>
              {row.available
                ? (row.label || row.id)
                : (row.label || row.id) + ' — ' + (row.why || 'unavailable')}
            </option>
          ))}
        </select>
        <button id="attach" className="icon-btn" type="button" disabled={uploadBusy}
          title="Upload files into the workspace" aria-label="Upload files"
          onClick={() => fileRef.current?.click()}>{uploadBusy ? '…' : '+'}</button>
        <input id="file-input" ref={fileRef} type="file" multiple hidden onChange={onPick} />
        {/* plan mode changes what the next request asks for, so it sits with the
            prompt — and ⌘K is here because this is where a command is typed */}
        <button id="plan-toggle" className="chip-btn" type="button" aria-pressed={planMode}
          title={planMode
            ? 'Plan mode is on: the next turn investigates and proposes, without editing'
            : 'Plan mode: the next turn only plans, no edits'}
          onClick={togglePlanMode}>{planMode ? 'plan on' : 'plan'}</button>
        <button id="fusion-btn" className="chip-btn" type="button"
          title="Command center (⌘K)" aria-label="Command center"
          onClick={() => openFusion()}>⌘K</button>
      </div>

      <textarea id="input" rows={1} spellCheck={false} ref={ref}
        placeholder={planMode
          ? 'Describe what to plan — no files will be touched…'
          : 'Ask LOCODE to build, edit or run something…'}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={onKeyDown} />

      <div className="composer-row">
        <span className="hint">Ctrl+Enter to send · Esc to stop</span>
        <span id="status" className="status" data-kind={status.kind}
          data-working={working ? '1' : '0'} title={brainTitle(health)}>
          <span className="dot" id="status-dot" style={{ background: KIND_DOT[status.kind] }} />
          {label && <span id="status-text">{label}</span>}
        </span>
        {rateText && (
          <span id="tps" className="tps" data-live={tps !== null ? '1' : '0'} title={rateTitle}>
            {rateText}
          </span>
        )}
        <button id="send" className="btn btn-primary" type="submit" disabled={busy}>Send</button>
      </div>
    </form>
  );
}
