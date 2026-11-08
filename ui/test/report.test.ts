/* The report the export view downloads, checked without a browser.

   The builder takes its timestamp as an argument and reads nothing else from the
   environment, so these assertions are about the exact bytes the modal hashes and
   hands to the download: the same input must always produce the same text. */

import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ALL_SECTIONS, buildReport } from '../src/report.ts';
import type { ReportInput } from '../src/report.ts';

const input: ReportInput = {
  at: '2026-11-08T00:00:00.000Z',
  project: 'demo',
  workspace: '/tmp/demo',
  engine: 'same origin',
  model: 'llama3',
  sandbox: 'namespace',
  net: 'off',
  branch: 'main',
  initialized: true,
  status: ' M notes.txt\n?? extra.txt',
  messages: [
    { id: 1, role: 'user', text: 'fix the header', tools: [] },
    { id: 2, role: 'assistant', text: 'Done.\nSecond line.', tools: [] }
  ],
  models: [{ id: 'llama3', provider: 'ollama', available: true }],
  sessions: [{ id: 's1', title: 'fix the header', messages: 2, updated: 0 }],
  server: { name: '', port: 0 }
};

test('markdown carries the identity, the tree and the transcript', () => {
  const text = buildReport(input, 'markdown', ALL_SECTIONS);
  assert.match(text, /^# LOCODE \/\/ DIAGNOSTIC REPORT/);
  assert.match(text, /generated: 2026-11-08T00:00:00.000Z/);
  assert.match(text, /## \d+\. ENVIRONMENT/);
  assert.match(text, / M notes\.txt/);
  assert.match(text, /llama3/);
});

test('a message keeps its place but not its line breaks', () => {
  const text = buildReport(input, 'markdown', ALL_SECTIONS);
  assert.match(text, /\*\*agent\*\* — Done\. ⏎ Second line\./);
});

test('an unchecked section is nowhere in the output', () => {
  const text = buildReport(input, 'markdown', { ...ALL_SECTIONS, transcript: false });
  assert.doesNotMatch(text, /SESSION TRANSCRIPT/);
  assert.doesNotMatch(text, /fix the header/);
  /* the sections that were kept are still numbered from one */
  assert.match(text, /## 1\. ENVIRONMENT/);
});

test('json mirrors the selection and parses', () => {
  const text = buildReport(input, 'json', { ...ALL_SECTIONS, models: false });
  const body = JSON.parse(text) as {
    sections: Record<string, boolean>;
    transcript: { role: string }[];
    models: unknown;
  };
  assert.equal(body.sections.models, false);
  assert.equal(body.models, null);
  assert.equal(body.transcript.length, 2);
});

test('the same input produces the same bytes', () => {
  assert.equal(
    buildReport(input, 'markdown', ALL_SECTIONS),
    buildReport(input, 'markdown', ALL_SECTIONS)
  );
});

test('a workspace without a repository says so instead of an empty block', () => {
  const text = buildReport({ ...input, initialized: false, status: '' }, 'markdown', ALL_SECTIONS);
  assert.match(text, /no repository/);
});
