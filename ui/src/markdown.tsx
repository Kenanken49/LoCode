/* Chunk 5's markdown renderer, as React elements.

   Like the old DOM version it builds nodes rather than a string of HTML, so a
   model cannot inject markup through its reply. Fenced code, # headings,
   - / 1. lists, > quotes, --- rules and inline `code`, **bold**, *em*,
   [link](http://…) are supported; anything else is a paragraph. */

import type { ReactNode } from 'react';

let seq = 0;
const key = (): string => `md-${seq++}`;

type Maker = (m: RegExpExecArray) => ReactNode;

const INLINE: ReadonlyArray<[RegExp, Maker]> = [
  [/^`([^`]+)`/, (m) => <code key={key()}>{m[1]}</code>],
  [/^\*\*([^*]+)\*\*/, (m) => <strong key={key()}>{m[1]}</strong>],
  [/^__([^_]+)__/, (m) => <strong key={key()}>{m[1]}</strong>],
  [/^\*([^*]+)\*/, (m) => <em key={key()}>{m[1]}</em>],
  [/^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/, (m) => (
    <a key={key()} href={m[2]} target="_blank" rel="noopener noreferrer">{m[1]}</a>
  )]
];

function inline(src: string): ReactNode[] {
  const out: ReactNode[] = [];
  let text = src;
  let guard = 0;
  while (text && guard++ < 400) {
    let hit = false;
    for (const [re, make] of INLINE) {
      const m = re.exec(text);
      if (m) { out.push(make(m)); text = text.slice(m[0].length); hit = true; break; }
    }
    if (hit) continue;
    // jump to the next marker instead of eating it, so a span that starts
    // mid-line (`and `code` here`) still becomes a <code>
    const next = text.search(/[`*]|\[(?:[^\]]+)\]\(/);
    if (next === -1) { out.push(text); break; }
    if (next > 0) { out.push(text.slice(0, next)); text = text.slice(next); continue; }
    out.push(text[0]);                       // stray marker, keep it literal
    text = text.slice(1);
  }
  return out;
}

export function renderMarkdown(src: string): ReactNode[] {
  const blocks: ReactNode[] = [];
  const lines = String(src ?? '').replace(/\r\n/g, '\n').split('\n');
  let i = 0;
  let para: string[] | null = null;
  let list: { tag: 'ul' | 'ol'; items: ReactNode[] } | null = null;

  const flushPara = (): void => {
    if (!para) return;
    blocks.push(<p key={key()}>{inline(para.join('\n'))}</p>);
    para = null;
  };
  const flushList = (): void => {
    if (!list) return;
    const items = list.items;
    blocks.push(list.tag === 'ul'
      ? <ul key={key()}>{items}</ul>
      : <ol key={key()}>{items}</ol>);
    list = null;
  };
  const flushAll = (): void => { flushPara(); flushList(); };

  while (i < lines.length) {
    const line = lines[i];

    if (/^```/.test(line)) {                       // fenced code
      flushAll();
      const lang = line.slice(3).trim();
      const buf: string[] = [];
      i += 1;
      while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i += 1; }
      i += 1;
      blocks.push(
        <pre key={key()}>
          <code data-lang={lang || undefined}>{buf.join('\n') + (buf.length ? '\n' : '')}</code>
        </pre>
      );
      continue;
    }

    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) {
      flushAll();
      const nodes = inline(h[2]);
      const level = h[1].length;
      blocks.push(level === 1 ? <h1 key={key()}>{nodes}</h1>
        : level === 2 ? <h2 key={key()}>{nodes}</h2>
        : level === 3 ? <h3 key={key()}>{nodes}</h3>
        : <h4 key={key()}>{nodes}</h4>);
      i += 1; continue;
    }

    if (/^\s*([-*_])\s*\1\s*\1[\s-*_]*$/.test(line)) {
      flushAll(); blocks.push(<hr key={key()} />); i += 1; continue;
    }

    const ul = /^\s*[-*+]\s+(.*)$/.exec(line);
    const ol = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (ul || ol) {
      flushPara();
      const tag: 'ul' | 'ol' = ul ? 'ul' : 'ol';
      if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
      list.items.push(<li key={key()}>{inline((ul ?? ol)![1])}</li>);
      i += 1; continue;
    }

    if (/^>\s?/.test(line)) {
      flushAll();
      blocks.push(<blockquote key={key()}>{inline(line.replace(/^>\s?/, ''))}</blockquote>);
      i += 1; continue;
    }

    if (!line.trim()) { flushAll(); i += 1; continue; }

    flushList();
    (para ??= []).push(line);
    i += 1;
  }
  flushAll();
  return blocks;
}
