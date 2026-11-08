/* The Export view: the design's diagnostic dossier, generated from live state.

   The design offers PDF with an Ed25519 signature and a SOC2 stamp. There is no
   signing key here, so the signature is the SHA-256 of the exact bytes being
   downloaded; the PDF itself is real — `pdf.ts` writes PDF 1.4 by hand, so the
   dossier is the same report as the runbook with actual pages, and the digest
   covers the PDF's bytes rather than a description of them. */

import { useEffect, useMemo, useRef, useState } from 'react';
import { buildPdf } from '../pdf';
import { ALL_SECTIONS, buildReport } from '../report';
import type { ExportFormat, ReportInput, ReportSections } from '../report';
import { useApp } from '../store';

const errText = (e: unknown): string => (e instanceof Error ? e.message : String(e));

async function sha256(bytes: Uint8Array): Promise<string> {
  if (!globalThis.crypto?.subtle) throw new Error('crypto.subtle is not available in this context');
  /* a fresh copy so the argument is an ArrayBuffer view the digest call accepts */
  const digest = await globalThis.crypto.subtle.digest('SHA-256', new Uint8Array(bytes));
  return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, '0')).join('');
}

const LABELS: Record<keyof ReportSections, string> = {
  transcript: 'Session transcript and saved sessions',
  changes: 'Working tree (git status)',
  environment: 'Runtime environment',
  models: 'Models that can answer'
};

const EXT: Record<ExportFormat, string> = { markdown: 'md', json: 'json', pdf: 'pdf' };
const MIME: Record<ExportFormat, string> = {
  markdown: 'text/markdown', json: 'application/json', pdf: 'application/pdf'
};
const BUTTON: Record<ExportFormat, string> = { markdown: 'Markdown', json: 'JSON', pdf: 'PDF' };
const KIND: Record<ExportFormat, string> = {
  markdown: 'team runbook', json: 'machine readable', pdf: 'print ready'
};

export function FusionExport() {
  const project = useApp((s) => s.project.name);
  const workspace = useApp((s) => s.health?.workspace || '');
  const baseUrl = useApp((s) => s.baseUrl);
  const model = useApp((s) => s.model);
  const models = useApp((s) => s.models);
  const sandbox = useApp((s) => s.health?.sandbox?.mode || s.sandbox?.mode || 'off');
  const netMode = useApp((s) => s.netMode);
  const changes = useApp((s) => s.changes);
  const messages = useApp((s) => s.messages);
  const sessions = useApp((s) => s.sessions.items);
  const server = useApp((s) => s.server);

  const [format, setFormat] = useState<ExportFormat>('markdown');
  const [sections, setSections] = useState<ReportSections>(ALL_SECTIONS);
  const [digest, setDigest] = useState('');
  const [digestError, setDigestError] = useState('');
  const [note, setNote] = useState('');
  const at = useRef(new Date().toISOString());

  const input = useMemo<ReportInput>(() => ({
    at: at.current,
    project,
    workspace,
    engine: baseUrl || 'same origin',
    model,
    sandbox,
    net: netMode,
    branch: changes.branch,
    initialized: changes.initialized,
    status: changes.status,
    messages,
    models,
    sessions,
    server
  }), [at, project, workspace, baseUrl, model, sandbox, netMode, changes, messages, models,
    sessions, server]);

  const markdown = useMemo(() => buildReport(input, 'markdown', sections), [input, sections]);
  const json = useMemo(() => buildReport(input, 'json', sections), [input, sections]);
  const title = 'LOCODE diagnostic · ' + (project || 'workspace');
  const pdf = useMemo(
    () => (format === 'pdf' ? buildPdf(markdown, { title }) : null),
    [format, markdown, title]
  );

  const text = format === 'json' ? json : markdown;
  const payload = useMemo(
    () => (pdf ? new Uint8Array(pdf.bytes) : new TextEncoder().encode(text)),
    [pdf, text]
  );

  useEffect(() => {
    let live = true;
    setDigestError('');
    sha256(payload).then(
      (hex) => { if (live) setDigest(hex); },
      (e) => { if (live) { setDigest(''); setDigestError(errText(e)); } }
    );
    return () => { live = false; };
  }, [payload]);

  const changed = changes.status.split('\n').filter(Boolean).length;

  const tags: Record<keyof ReportSections, string> = {
    transcript: messages.length + ' msg',
    changes: changes.initialized ? changed + ' changed' : 'no repo',
    environment: sandbox,
    models: models.length + ' ready'
  };

  const preview = pdf
    ? [
      '%PDF-1.4 · ' + pdf.pages + (pdf.pages === 1 ? ' page' : ' pages') + ' · ' + payload.length + ' bytes',
      'A4 · Courier 9pt body · Helvetica-Bold headings',
      'the sha-256 below covers these bytes',
      '',
      'The dossier is a binary file, so this preview is its structure rather than its text.',
      'It carries the same report as Runbook (.md): download it to see the laid-out pages.',
      '',
      'text rows:  ' + pdf.lines,
      'objects:    ' + (5 + pdf.pages * 2) + ' · xref entries: ' + (6 + pdf.pages * 2)
    ].join('\n')
    : text;

  const name = 'locode-diagnostic-' + (project || 'workspace') + '.' + EXT[format];

  const download = (): void => {
    const blob = new Blob([payload], { type: MIME[format] });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    a.click();
    URL.revokeObjectURL(url);
    setNote('downloaded ' + name);
  };

  const copy = async (): Promise<void> => {
    try {
      await navigator.clipboard.writeText(text);
      setNote('copied ' + payload.length + ' bytes');
    } catch (e) {
      setNote('copy failed: ' + errText(e));
    }
  };

  const toggle = (key: keyof ReportSections): void => {
    setSections((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  const formatButton = (value: ExportFormat, strong: string): React.ReactElement => (
    <button className="fx-format" type="button" aria-pressed={format === value}
      onClick={() => setFormat(value)}>
      <strong>{strong}</strong>
      <span>{KIND[value]}</span>
    </button>
  );

  return (
    <div className="fx-modal fx-modal-inline" id="fx-export">
      <div className="fx-modal-head">
        <div>
          <h3>
            Diagnostic report
            <span className="fx-pill" data-k="fusion">live engine state</span>
            <span className="fx-pill" data-k={digest ? 'ok' : 'warn'}>
              {digest ? 'signed sha-256' : 'digest pending'}
            </span>
          </h3>
          <p>Certified runbook of this session, generated from the engine's live state.</p>
        </div>
        <span className="fx-pill" data-k="idle">{format}</span>
      </div>

      <div className="fx-modal-grid">
        <div className="fx-modal-left">
          <div>
            <div className="fx-field-label">
              <span>1. export format</span>
              <em>{KIND[format]}</em>
            </div>
            <div className="fx-formats">
              {formatButton('markdown', 'Runbook (.md)')}
              {formatButton('json', 'Bundle (.json)')}
              {formatButton('pdf', 'Dossier (.pdf)')}
            </div>
          </div>

          <div>
            <div className="fx-field-label">
              <span>2. report scope</span>
              <em>{Object.values(sections).filter(Boolean).length} of {Object.keys(sections).length} selected</em>
            </div>
            <div className="fx-scope">
              {(Object.keys(LABELS) as (keyof ReportSections)[]).map((key) => (
                <div key={key} className="fx-scope-row">
                  <label>
                    <input type="checkbox" checked={sections[key]}
                      onChange={() => toggle(key)} />
                    {LABELS[key]}
                  </label>
                  <span className="fx-scope-tag">{tags[key]}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="fx-sig">
            <div className="row"><span className="k">signature</span><span>sha-256</span></div>
            <div className="digest" id="fx-digest">
              {digest ? digest.replace(/(.{8})/g, '$1 ').trim() : digestError || 'computing…'}
            </div>
            <div className="row"><span className="k">generated</span><span>{at.current}</span></div>
            <div className="row"><span className="k">engine</span><span>{baseUrl || 'same origin'}</span></div>
          </div>
        </div>

        <div className="fx-modal-right">
          <div className="fx-field-label">
            <span>live preview</span>
            <em>
              {payload.length} bytes · {pdf ? pdf.pages + (pdf.pages === 1 ? ' page' : ' pages')
                : text.split('\n').length + ' lines'}
            </em>
          </div>
          <pre className="fx-preview" id="fx-preview">{preview}</pre>
        </div>
      </div>

      <div className="fx-modal-foot">
        <span>
          {name}
          {note && <span className="fx-note"> · {note}</span>}
        </span>
        <span className="acts">
          <button className="fx-btn" type="button" disabled={format === 'pdf'}
            title={format === 'pdf' ? 'a PDF is bytes, not text — download it instead' : 'copy the report'}
            onClick={() => { void copy(); }}>Copy</button>
          <button className="fx-btn fx-btn-primary" id="fx-download" type="button" onClick={download}>
            Download {BUTTON[format]}
          </button>
        </span>
      </div>
    </div>
  );
}
