/* The PDF the export downloads, checked as a file rather than as a picture.

   There is no PDF tool in this environment (no qpdf, no pdftotext, no pypdf), so
   the checks below are structural on purpose: the cross-reference table is read
   back and every offset in it must land exactly on the object it claims, which is
   the part a hand-written writer gets wrong. Text is then unescaped out of the
   content streams to prove the report actually arrived on the page. */

import assert from 'node:assert/strict';
import { test } from 'node:test';
import { buildPdf } from '../src/pdf.ts';

const latin1 = (bytes: Uint8Array): string => {
  let out = '';
  for (const b of bytes) out += String.fromCharCode(b);
  return out;
};

const report = [
  '# LOCODE // DIAGNOSTIC REPORT',
  '',
  'generated: 2026-11-08T00:00:00.000Z',
  'project:   demo',
  'engine:    same origin',
  '',
  '## 1. ENVIRONMENT',
  '',
  '- sandbox: namespace',
  '- network: off',
  '- repository: git · main',
  '',
  '## 2. WORKING TREE',
  '',
  '`1 entry`',
  '',
  '```',
  ' M notes.txt',
  '```',
  '',
  '## 3. SESSION TRANSCRIPT',
  '',
  '**developer** — fix the header',
  '',
  '---',
  'locode · 2026-11-08T00:00:00.000Z',
  ''
].join('\n');

/* Every string drawn by a Tj, unescaped, in file order. */
function drawn(text: string): string {
  let out = '';
  const re = /\((?:[^()\\]|\\.)*\) Tj/g;
  for (const match of text.matchAll(re)) {
    const body = match[0].slice(1, -4);
    let s = '';
    for (let i = 0; i < body.length; i++) {
      const ch = body[i];
      if (ch !== '\\') {
        s += ch;
        continue;
      }
      const next = body[i + 1];
      if (next >= '0' && next <= '7') {
        s += String.fromCharCode(parseInt(body.slice(i + 1, i + 4), 8));
        i += 3;
      } else {
        s += next;
        i += 1;
      }
    }
    out += s + '\n';
  }
  return out;
}

function xrefOffsets(text: string): number[] {
  const startMatch = /startxref\n(\d+)\n/.exec(text);
  assert.ok(startMatch, 'the trailer points at the table');
  const start = Number(startMatch[1]);
  assert.equal(text.slice(start, start + 4), 'xref', 'startxref points at the xref keyword');
  const lines = text.slice(start).split('\n');
  const count = Number(lines[1].split(' ')[1]);
  const offsets: number[] = [];
  for (let i = 0; i < count; i++) offsets.push(Number(lines[2 + i].slice(0, 10)));
  return offsets;
}

test('the bytes are a PDF a reader can open', () => {
  const { bytes, pages } = buildPdf(report, { title: 'locode diagnostic' });
  const text = latin1(bytes);
  assert.ok(text.startsWith('%PDF-1.4\n'));
  assert.ok(text.endsWith('%%EOF\n'));
  assert.equal(pages, 1);
  assert.match(text, /\/Type \/Catalog/);
  assert.match(text, /\/BaseFont \/Courier/);
  assert.match(text, /\/BaseFont \/Helvetica-Bold/);
});

test('every cross-reference offset lands on its object', () => {
  const text = latin1(buildPdf(report, { title: 'locode diagnostic' }).bytes);
  const offsets = xrefOffsets(text);
  assert.ok(offsets.length > 5);
  assert.equal(offsets[0], 0, 'the free entry is zero');
  offsets.slice(1).forEach((offset, i) => {
    assert.equal(
      text.slice(offset, offset + `${i + 1} 0 obj`.length),
      `${i + 1} 0 obj`,
      `object ${i + 1} is at ${offset}`
    );
  });
  assert.match(text, new RegExp(`/Size ${offsets.length}`));
});

test('the report text is on the page, markdown removed', () => {
  const text = latin1(buildPdf(report, { title: 'locode diagnostic' }).bytes);
  const page = drawn(text);
  assert.match(page, /LOCODE \/\/ DIAGNOSTIC REPORT/);
  assert.match(page, /generated: 2026-11-08T00:00:00\.000Z/);
  assert.match(page, /sandbox: namespace/);
  assert.match(page, / M notes\.txt/);
  assert.match(page, /developer - fix the header/);
  assert.doesNotMatch(page, /^#/m);
  assert.doesNotMatch(page, /\*\*/);
  assert.doesNotMatch(page, /```/);
});

test('a long report is paginated and every page says where it is', () => {
  const long = ['# LONG REPORT', ''];
  for (let i = 0; i < 400; i++) long.push('line ' + String(i).padStart(3, '0') + ' of the working tree');
  const { pages, lines } = buildPdf(long.join('\n'), { title: 'locode diagnostic' });
  assert.ok(pages >= 5, 'pagination actually splits');
  assert.ok(lines >= 400);
  const text = latin1(buildPdf(long.join('\n'), { title: 'locode diagnostic' }).bytes);
  const page = drawn(text);
  for (let i = 1; i <= pages; i++) assert.match(page, new RegExp(`page ${i} of ${pages}`));
  assert.equal((text.match(/\/Type \/Page[^s]/g) ?? []).length, pages);
});

test('every stream declares the length a reader will read', () => {
  const text = latin1(buildPdf(report, { title: 't' }).bytes);
  let seen = 0;
  for (const m of text.matchAll(/\/Length (\d+) >>\nstream\n([\s\S]*?)endstream/g)) {
    seen += 1;
    assert.equal(m[2].length, Number(m[1]), 'the stream is exactly as long as it claims');
  }
  assert.equal(seen, 1, 'one content stream per page');
});

test('the same report produces the same file', () => {
  const a = buildPdf(report, { title: 'locode diagnostic' }).bytes;
  const b = buildPdf(report, { title: 'locode diagnostic' }).bytes;
  assert.equal(latin1(a), latin1(b));
});

test('characters WinAnsi lacks are spelled, not lost', () => {
  const text = latin1(buildPdf('arrow ⏎ and ≥ 3 · done', { title: 't' }).bytes);
  const page = drawn(text);
  assert.match(page, /arrow \/ and >= 3/);
  assert.match(page, /· done/, 'the middle dot survives as WinAnsi b7');
});
