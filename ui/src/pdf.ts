/* A real PDF, small enough to audit.

   No dependency and no network: PDF 1.4 with the two base-14 fonts every reader
   already carries (Courier for the report body, Helvetica-Bold for headings), one
   content stream per page, a cross-reference table holding true byte offsets, and
   a trailer. The caller hands in the Markdown the export already builds, so the
   PDF and the .md report hold the same text — the file is laid out, not
   regenerated from different inputs.

   Deterministic on purpose: the only timestamps are the ones already in the
   report text, so the same input produces the same bytes and the SHA-256 the panel
   shows is a check on this exact file rather than an ornament. */

export interface PdfMeta {
  title: string;
}

export interface PdfResult {
  bytes: Uint8Array;
  pages: number;
  lines: number;
}

/* A4 at 72dpi, the page size the export's own layout assumes. */
const PAGE_W = 595.28;
const PAGE_H = 841.89;
const MARGIN_X = 48;
const MARGIN_TOP = 60;
const MARGIN_BOTTOM = 56;
const BODY_SIZE = 9;
const BODY_LEAD = 12.2;
const RULE_GRAY = 0.55;

/* Courier advances 0.6em, so 9pt fits 499.28pt / 5.4pt = 92 columns. */
const COLS = 92;

type Font = 'F1' | 'F2';

type Row =
  | { kind: 'text'; font: Font; size: number; lead: number; text: string }
  | { kind: 'rule'; lead: number };

/* WinAnsi is what the base fonts are declared with; anything it lacks is spelled
   out in ASCII rather than dropped, so a status line never loses its arrow. */
const SPELLED: Record<string, string> = {
  '⏎': ' / ',
  '→': '->',
  '←': '<-',
  '↑': '^',
  '↓': 'v',
  '≥': '>=',
  '≤': '<=',
  '—': '-',
  '–': '-',
  '’': "'",
  '‘': "'",
  '“': '"',
  '”': '"',
  '…': '...',
  '•': '*',
  '×': 'x',
  '✓': 'ok',
  '✗': 'x',
  '\u00a0': ' '
};

/* Latin-1 plus the typographic marks WinAnsi keeps; everything else becomes '?'. */
const WINANSI_EXTRA: Record<string, string> = {
  '·': '\xb7',
  '°': '\xb0',
  '±': '\xb1',
  'µ': '\xb5',
  '¼': '\xbc',
  '½': '\xbd',
  '¾': '\xbe'
};

function safe(text: string): string {
  let out = '';
  for (const ch of text) {
    if (SPELLED[ch] !== undefined) out += SPELLED[ch];
    else if (WINANSI_EXTRA[ch] !== undefined) out += WINANSI_EXTRA[ch];
    else if (ch.charCodeAt(0) <= 0xff) out += ch;
    else out += '?';
  }
  return out;
}

/* A PDF literal string: the three specials escaped, and every byte outside
   printable ASCII written as octal so the token stays valid in any encoding. */
function literal(text: string): string {
  let out = '';
  for (const ch of text) {
    const code = ch.charCodeAt(0);
    if (ch === '\\' || ch === '(' || ch === ')') out += '\\' + ch;
    else if (code < 32 || code > 126) out += '\\' + code.toString(8).padStart(3, '0');
    else out += ch;
  }
  return out;
}

function wrap(text: string, cols: number): string[] {
  if (!text) return [''];
  const out: string[] = [];
  let line = '';
  for (const word of text.split(' ')) {
    let rest = word;
    while (rest.length > cols) {
      if (line) {
        out.push(line);
        line = '';
      }
      out.push(rest.slice(0, cols));
      rest = rest.slice(cols);
    }
    if (!rest) continue;
    if (!line) line = rest;
    else if (line.length + 1 + rest.length <= cols) line += ' ' + rest;
    else {
      out.push(line);
      line = rest;
    }
  }
  out.push(line);
  return out;
}

/* Markdown in, printed rows out: headings get the bold face, fenced blocks are
   printed as written (the fence markers are structure, not content), and the
   inline emphasis the report uses is removed rather than shown. */
function rowsOf(markdown: string): Row[] {
  const rows: Row[] = [];
  let fenced = false;

  for (const raw of markdown.replace(/\r/g, '').split('\n')) {
    const line = raw.replace(/\s+$/, '');
    if (line.trim() === '```') {
      fenced = !fenced;
      continue;
    }
    if (fenced) {
      rows.push({ kind: 'text', font: 'F1', size: BODY_SIZE, lead: BODY_LEAD, text: safe(line) });
      continue;
    }
    if (line.startsWith('## ')) {
      const text = safe(line.slice(3).replace(/\*\*/g, '').replace(/`/g, ''));
      rows.push({ kind: 'rule', lead: 8 });
      rows.push({ kind: 'text', font: 'F2', size: 11, lead: 15, text });
      continue;
    }
    if (line.startsWith('# ')) {
      const text = safe(line.slice(2).replace(/\*\*/g, '').replace(/`/g, ''));
      rows.push({ kind: 'text', font: 'F2', size: 15, lead: 20, text });
      continue;
    }
    if (/^-{3,}$/.test(line.trim())) {
      rows.push({ kind: 'rule', lead: 14 });
      continue;
    }
    for (const part of wrap(safe(line.replace(/\*\*/g, '').replace(/`/g, '')), COLS)) {
      rows.push({ kind: 'text', font: 'F1', size: BODY_SIZE, lead: BODY_LEAD, text: part });
    }
  }
  return rows;
}

function paginate(rows: Row[]): Row[][] {
  const pages: Row[][] = [];
  let page: Row[] = [];
  let y = PAGE_H - MARGIN_TOP;
  for (const row of rows) {
    if (y - row.lead < MARGIN_BOTTOM) {
      pages.push(page);
      page = [];
      y = PAGE_H - MARGIN_TOP;
    }
    page.push(row);
    y -= row.lead;
  }
  pages.push(page);
  return pages;
}

function content(pages: Row[][], title: string): string[] {
  return pages.map((rows, index) => {
    const out: string[] = ['q', 'BT'];
    let y = PAGE_H - MARGIN_TOP;
    for (const row of rows) {
      if (row.kind === 'rule') {
        const ly = (y + row.lead * 0.45).toFixed(2);
        out.push(
          'ET', `${RULE_GRAY} G`, '0.5 w',
          `${MARGIN_X} ${ly} m ${(PAGE_W - MARGIN_X * 2).toFixed(2)} 0 l S`,
          '0 G', 'BT'
        );
        y -= row.lead;
        continue;
      }
      out.push(`/${row.font} ${row.size} Tf`);
      out.push(`1 0 0 1 ${MARGIN_X} ${y.toFixed(2)} Tm`);
      out.push(`(${literal(row.text)}) Tj`);
      y -= row.lead;
    }
    out.push('ET');
    /* the footer names the file and its place in the run */
    out.push(
      '0 G', 'BT', '/F1 7.5 Tf',
      `1 0 0 1 ${MARGIN_X} 30 Tm`,
      `(${literal(safe(title))}  ·  page ${index + 1} of ${pages.length}) Tj`,
      'ET', 'Q'
    );
    return out.join('\n') + '\n';
  });
}

export function buildPdf(markdown: string, meta: PdfMeta): PdfResult {
  const rows = rowsOf(markdown);
  const pages = paginate(rows);
  const streams = content(pages, meta.title);
  const kids = pages.map((_, i) => `${6 + i * 2} 0 R`).join(' ');

  const objects: string[] = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    `<< /Type /Pages /Kids [${kids}] /Count ${pages.length} /MediaBox [0 0 ${PAGE_W} ${PAGE_H}] >>`,
    '<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>',
    `<< /Title (${literal(safe(meta.title))}) /Producer (LOCODE) /Creator (LOCODE command center) >>`
  ];
  pages.forEach((_, i) => {
    objects.push(
      '<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> '
      + `/MediaBox [0 0 ${PAGE_W} ${PAGE_H}] /Contents ${7 + i * 2} 0 R >>`
    );
    objects.push(`<< /Length ${streams[i].length} >>\nstream\n${streams[i]}endstream`);
  });

  let out = '%PDF-1.4\n%\xe2\xe3\xcf\xd3\n';
  const offsets: number[] = [];
  objects.forEach((body, i) => {
    offsets.push(out.length);
    out += `${i + 1} 0 obj\n${body}\nendobj\n`;
  });

  const xref = out.length;
  out += `xref\n0 ${objects.length + 1}\n`;
  out += '0000000000 65535 f \n';
  for (const offset of offsets) {
    out += `${offset.toString().padStart(10, '0')} 00000 n \n`;
  }
  out += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R /Info 5 0 R >>\n`;
  out += `startxref\n${xref}\n%%EOF\n`;

  const bytes = new Uint8Array(out.length);
  for (let i = 0; i < out.length; i++) bytes[i] = out.charCodeAt(i) & 0xff;

  return { bytes, pages: pages.length, lines: rows.filter((r) => r.kind === 'text').length };
}
