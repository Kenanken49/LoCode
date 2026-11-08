/* Chunk 9 · file preview modal, and chunk 10 · server logs in the same shell.

   One <pre> with pre-wrap and a single text node — a 5000-line file costs one
   paint and no parsing. Escape, backdrop click and Close all dismiss it; a click
   inside the card does not. Logs land scrolled to the tail, files to the top. */

import { useEffect, useRef } from 'react';
import { closeModal, useApp } from '../store';

export function Modal() {
  const modal = useApp((s) => s.modal);
  const body = useRef<HTMLPreElement>(null);
  const lastFocus = useRef<HTMLElement | null>(null);
  const open = useRef(false);

  useEffect(() => {
    if (modal.open && !open.current) {
      lastFocus.current = document.activeElement as HTMLElement | null;
      document.getElementById('modal-close')?.focus();
      const el = body.current;
      if (el) el.scrollTop = modal.logs ? el.scrollHeight : 0;   // logs end at the tail
    }
    if (!modal.open && open.current) lastFocus.current?.focus?.();
    open.current = modal.open;
  }, [modal.open, modal.logs]);

  useEffect(() => {
    if (!modal.open) return;
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') { e.stopPropagation(); closeModal(); }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [modal.open]);

  const onBackdrop = (e: { target: EventTarget | null; currentTarget: EventTarget | null }): void => {
    if (e.target === e.currentTarget) closeModal();            // backdrop only, not the card
  };

  return (
    <div id="modal" hidden={!modal.open} role="dialog" aria-modal="true" aria-labelledby="modal-title"
      onClick={onBackdrop} onMouseDown={onBackdrop}>
      <div className="modal-card">
        <div className="modal-head">
          <span id="modal-title" className="mono">
            {modal.title}
            {modal.sub && <span className="sub"> {modal.sub}</span>}
          </span>
          <button id="modal-close" className="btn" type="button" onClick={closeModal}>Close</button>
        </div>
        <pre id="modal-body" ref={body} className={'mono' + (modal.logs ? ' logs' : '')}>{modal.text}</pre>
      </div>
    </div>
  );
}
