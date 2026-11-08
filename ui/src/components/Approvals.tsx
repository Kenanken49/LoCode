/* Chunk 7 · approval cards — the security gate.

   Polls /api/approvals every 700ms (the timer lives in App); every pending
   request becomes a card, newest on top, no duplicates. Allow/Deny posts
   /api/approvals/resolve {id, approved, remember} and the card fades out over
   var(--fade) before it leaves the DOM.

   Remember is deliberately narrow: "domain" only when the request has a host,
   "action" always, and NEVER for import_file — an import moves quarantined
   bytes into the workspace, so it stays a per-file decision (the server strips
   `remember` for that action too; the card does not even offer it).

   The countdown starts from the server's own `remaining`, so clock skew between
   this browser and the engine cannot extend or cut short a decision window. */

import { useEffect, useState } from 'react';
import type { ChangeEvent } from 'react';
import { resolveApproval, setStatus, useApp } from '../store';
import type { ApprovalReq } from '../types';

const FADE_MS = 150;
const TITLE: Record<string, string> = {
  fetch_url: 'Fetch URL', fetch_docs: 'Fetch docs', search_web: 'Web search',
  download_file: 'Download file', clone_repo: 'Clone repo', import_file: 'Import from inbox'
};

function fmtLeft(sec: number): string {
  const m = Math.floor(sec / 60), s = sec % 60;
  return m ? m + 'm ' + (s < 10 ? '0' + s : s) + 's' : s + 's';
}

interface CardState { req: ApprovalReq; fading: boolean }

function Card({ card, onDrop }: { card: CardState; onDrop: () => void }) {
  const req = card.req;
  const isImport = req.action === 'import_file';
  const [left, setLeft] = useState(() => Math.max(0, Number(req.remaining) || 0));
  const [gone, setGone] = useState(false);          // resolved, refused or expired locally
  const [sent, setSent] = useState(false);
  const [keepDomain, setKeepDomain] = useState(false);
  const [keepAction, setKeepAction] = useState(false);
  const fading = card.fading || gone;

  useEffect(() => {
    if (fading) {
      // the .out animation runs for var(--fade), then the card leaves the DOM
      const t = setTimeout(onDrop, FADE_MS);
      return () => clearTimeout(t);
    }
    const tick = setInterval(() => setLeft((l) => Math.max(0, l - 1)), 1000);
    return () => clearInterval(tick);
  }, [fading, req.id]);

  useEffect(() => { if (left === 0 && !fading) setGone(true); }, [left, fading]);

  const decide = (approved: boolean): void => {
    if (sent || fading) return;
    setSent(true);
    const remember = approved && !isImport
      ? [(keepDomain && req.host ? 'domain' : ''), (keepAction ? 'action' : '')].filter(Boolean)
      : [];
    resolveApproval(req.id, approved, remember)
      .then(() => setGone(true))
      .catch((e: unknown) => {
        // 404 = the tool call already timed out server-side; drop the card either way
        if (String((e as Error).message).indexOf('404') >= 0) setGone(true);
        else { setSent(false); setStatus('approval failed: ' + String((e as Error).message), 'err'); }
      });
  };

  const check = (setter: (v: boolean) => void) =>
    (e: ChangeEvent<HTMLInputElement>): void => setter(e.target.checked);

  return (
    <div className={'ap-card' + (isImport ? ' import' : '') + (fading ? ' out' : '')} data-id={req.id}>
      <div className="ap-title">
        <span className="ap-warn">⚠</span>
        <span>{'Approval required — ' + (TITLE[req.action] || req.action)}</span>
      </div>

      {req.url && <div className="ap-url">{(req.action === 'search_web' ? 'query ' : '') + req.url}</div>}

      <div className="ap-detail">{req.detail || ''}</div>

      <div className="ap-actions">
        {/* classes, not ids: two cards stack vertically and ids would collide */}
        <button type="button" className="btn ap-allow" disabled={sent || fading}
          onClick={() => decide(true)}>Allow once</button>
        <button type="button" className="btn btn-ghost ap-deny" disabled={sent || fading}
          onClick={() => decide(false)}>Deny</button>
      </div>

      {isImport ? (
        <div className="ap-note">
          No remember option: an import moves quarantined files into your workspace, so it is asked every time.
        </div>
      ) : (
        <div className="ap-remember">
          {req.host && (
            <label>
              <input type="checkbox" value="domain" checked={keepDomain} onChange={check(setKeepDomain)} />
              <span>Remember for <code>{req.host}</code></span>
            </label>
          )}
          <label>
            <input type="checkbox" value="action" checked={keepAction} onChange={check(setKeepAction)} />
            <span>Remember for all <code>{TITLE[req.action] || req.action}</code></span>
          </label>
        </div>
      )}

      <div className="ap-detail ap-expiry">
        {'Expires in ' + fmtLeft(left) + (req.host ? ' · host ' + req.host : '')}
      </div>
    </div>
  );
}

export function Approvals() {
  const approvals = useApp((s) => s.approvals);
  const [cards, setCards] = useState<CardState[]>([]);

  const drop = (id: string): void => setCards((prev) => prev.filter((c) => c.req.id !== id));

  /* Reconcile the drawn cards with what the engine still has pending: a request
     answered elsewhere or timed out starts fading, a new one is prepended (the
     server sends newest first), and anything already drawn just refreshes its
     countdown. No duplicates. */
  useEffect(() => {
    setCards((prev) => {
      const live = new Map(approvals.map((a) => [a.id, a]));
      const kept = prev.map((c) => {
        const fresh = live.get(c.req.id);
        if (fresh) { live.delete(c.req.id); return { req: fresh, fading: c.fading }; }
        return { ...c, fading: true };
      });
      const arriving = [...live.values()].map((req) => ({ req, fading: false }));
      return [...arriving, ...kept]
        .filter((c, i, all) => all.findIndex((o) => o.req.id === c.req.id) === i);
    });
  }, [approvals]);

  return (
    <div id="approvals" aria-live="assertive">
      {cards.map((c) => <Card key={c.req.id} card={c} onDrop={() => drop(c.req.id)} />)}
    </div>
  );
}
