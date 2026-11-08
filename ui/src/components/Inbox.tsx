/* Chunk 8 · inbox drawer.

   Slides in over the chat (it never reflows it) and lists what the consent gate
   quarantined. Import does not copy bytes itself: it pre-fills the composer so
   the agent calls import_file, which is what raises a chunk 7 approval card.
   Delete goes straight to POST /api/inbox/delete — removing quarantined data is
   the safe act, so it needs no approval. */

import { useEffect, useState } from 'react';
import { closeInbox, deleteInboxItem, importInboxItem, refreshInbox, useApp } from '../store';
import type { InboxItem } from '../types';

const SLIDE_MS = 220;

function bytes(n: number): string {
  const v = Number(n) || 0;
  if (v < 1024) return v + ' B';
  if (v < 1024 * 1024) return (v / 1024).toFixed(v < 10240 ? 1 : 0) + ' KB';
  return (v / 1048576).toFixed(1) + ' MB';
}

function Item({ it }: { it: InboxItem }) {
  const [deleting, setDeleting] = useState(false);
  return (
    <div className="inbox-item">
      <div className="inbox-name">{it.name}</div>
      <div className="inbox-meta">{it.kind + ' · ' + bytes(it.bytes) + ' · ' + it.created}</div>
      <div className="inbox-btns">
        <button type="button" className="btn inbox-import"
          title="Ask the agent to move this into the workspace (approval card follows)"
          onClick={() => importInboxItem(it.name)}>Import</button>
        <button type="button" className="btn inbox-delete" disabled={deleting}
          onClick={() => {
            setDeleting(true);
            void deleteInboxItem(it.name).then(() => setDeleting(false));
          }}>Delete</button>
      </div>
    </div>
  );
}

export function Inbox() {
  const inbox = useApp((s) => s.inbox);
  const [mounted, setMounted] = useState(inbox.open);
  const [slid, setSlid] = useState(inbox.open);

  // mount first, then add .open on the next frame so the transform animates
  useEffect(() => {
    if (inbox.open) {
      setMounted(true);
      const r = requestAnimationFrame(() => setSlid(true));
      return () => cancelAnimationFrame(r);
    }
    setSlid(false);
    const t = setTimeout(() => setMounted(false), SLIDE_MS);
    return () => clearTimeout(t);
  }, [inbox.open]);

  useEffect(() => {
    if (!inbox.open) return;
    const onKey = (e: KeyboardEvent): void => { if (e.key === 'Escape') closeInbox(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [inbox.open]);

  return (
    <aside id="inbox" className={slid ? 'open' : ''} hidden={!mounted} aria-label="Inbox quarantine">
      <div className="inbox-head">
        <span>Inbox (quarantine)</span>
        <span className="inbox-actions">
          <button id="inbox-refresh" className="icon-btn" type="button" title="Refresh inbox"
            onClick={() => { void refreshInbox(); }}>⟳</button>
          <button id="inbox-close" className="icon-btn" type="button" title="Close inbox"
            onClick={closeInbox}>✕</button>
        </span>
      </div>

      <div id="inbox-items" className="inbox-items">
        {inbox.items.length === 0
          ? <div className="empty">(empty)</div>
          : inbox.items.map((it) => <Item key={it.name} it={it} />)}
      </div>

      <div className="inbox-foot">{inbox.path}</div>
    </aside>
  );
}
