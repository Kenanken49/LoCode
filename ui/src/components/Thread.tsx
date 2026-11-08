/* Chunk 5 · chat thread and bubbles, with chunk 6's tool cards inside them.

   The thread is `state.messages`; the stream writes into it through the
   store's rAF batch, so React paints at most one frame per token burst and
   the scroll follows unless the developer has scrolled up to read something.

   `addUserMessage` never runs markdown — whatever the developer typed stays
   literal text. Assistant text goes through renderMarkdown(), which builds
   elements rather than an HTML string, so a model cannot inject markup. */

import { memo, useEffect, useLayoutEffect, useRef } from 'react';
import { renderMarkdown } from '../markdown';
import { toggleToolCard, useApp } from '../store';
import type { Msg, ToolRow } from '../types';

const KEY_ARGS = ['path', 'cmd', 'command', 'url', 'inbox_name', 'name', 'query', 'pattern', 'message'];

/* "edit_file  server/index.ts · +18 −2" — the two most telling args, CSS
   ellipses them; the full JSON stays on hover. */
function summarize(args: Record<string, unknown>): string {
  if (!args || typeof args !== 'object') return '';
  const bits: string[] = [];
  for (const k of KEY_ARGS) {
    const v = args[k];
    if (typeof v === 'string' && v) bits.push(k + '=' + v.replace(/\s+/g, ' ').slice(0, 90));
    if (bits.length === 2) break;
  }
  if (!bits.length) {
    for (const [k, v] of Object.entries(args)) {
      const s = typeof v === 'string' ? v : JSON.stringify(v);
      bits.push(k + '=' + String(s).replace(/\s+/g, ' ').slice(0, 60));
      if (bits.length === 2) break;
    }
  }
  return bits.join(' · ');
}

function diffStats(name: string, args: Record<string, unknown>): { add: number; del: number } | null {
  if (name === 'write_file' && typeof args.content === 'string') {
    return { add: args.content.split('\n').length, del: 0 };
  }
  if (name === 'edit_file') {
    const a = String(args.old_string || '').split('\n');
    const b = String(args.new_string || '').split('\n');
    const seen = new Set(a);
    const keep = b.filter((l) => seen.has(l)).length;
    return { add: Math.max(0, b.length - keep), del: Math.max(0, a.length - keep) };
  }
  return null;
}

const ToolCard = memo(function ToolCard({ msgId, tool }: { msgId: number; tool: ToolRow }) {
  const st = diffStats(tool.name, tool.args);
  const cls = 'tool-card'
    + (tool.open ? ' open' : '')
    + (tool.state === 'ok' ? ' ok' : tool.state === 'err' ? ' err' : '');
  return (
    <div className={cls} data-id={tool.id}>
      <div className="tool-head" onClick={() => toggleToolCard(msgId, tool.id)}>
        <span className="tool-caret">▸</span>
        <span className="tool-name">{tool.name}</span>
        <span className="tool-args" title={JSON.stringify(tool.args, null, 2)}>{summarize(tool.args)}</span>
        {st && (
          <span className="tool-diff">
            <span className="add">+{st.add}</span>
            <span className="del"> −{st.del}</span>
          </span>
        )}
        {/* running pulses (tools.css); errors expand the card, successes stay folded */}
        <span className={'badge ' + tool.state}>
          {tool.state === 'err' ? 'error' : tool.state}
        </span>
      </div>
      <pre className="tool-body">{tool.result}</pre>
    </div>
  );
});

const Message = memo(function Message({ msg }: { msg: Msg }) {
  if (msg.role === 'user') {
    return (
      <div className="msg msg-user">
        <div className="bubble">
          <div className="msg-label">You</div>
          <div style={{ whiteSpace: 'pre-wrap' }}>{msg.text}</div>
        </div>
      </div>
    );
  }
  if (msg.role === 'error') {
    return (
      <div className="msg msg-assistant msg-error">
        <div className="bubble">
          <div className="msg-label">LOCODE</div>
          <div style={{ whiteSpace: 'pre-wrap' }}>{msg.text}</div>
        </div>
      </div>
    );
  }
  return (
    <div className="msg msg-assistant">
      <div className="bubble">
        <div className="msg-label">LOCODE</div>
        <div className="content">{renderMarkdown(msg.text)}</div>
        <div className="tools">
          {msg.tools.map((t) => <ToolCard key={t.id} msgId={msg.id} tool={t} />)}
        </div>
      </div>
    </div>
  );
});

export function Thread() {
  const messages = useApp((s) => s.messages);
  const ref = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const lastId = useRef(0);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onScroll = (): void => {
      stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    };
    el.addEventListener('scroll', onScroll);
    return () => el.removeEventListener('scroll', onScroll);
  }, []);

  // auto-scroll to the bottom on every append; a new bubble forces it
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const fresh = messages.length && messages[messages.length - 1].id !== lastId.current;
    lastId.current = messages.length ? messages[messages.length - 1].id : 0;
    if (fresh || stick.current) el.scrollTop = el.scrollHeight;
  }, [messages]);

  return (
    <div id="messages" ref={ref} aria-live="polite">
      {messages.map((m) => <Message key={m.id} msg={m} />)}
    </div>
  );
}
