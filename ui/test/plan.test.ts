/* The shell half of plan mode, checked without a browser.

   `runTurn` sends exactly what planRequest() returns, so these assertions are
   about the request a plan turn really makes: the prefix goes on the message the
   developer just typed, the earlier turns are untouched, and the thread itself
   keeps their own words. */

import assert from 'node:assert/strict';
import { test } from 'node:test';
import { PLAN_PREFIX, planRequest } from '../src/plan.ts';

const thread = [
  { role: 'user', content: 'what does src/app.tsx do?' },
  { role: 'assistant', content: 'It mounts the shell.' },
  { role: 'user', content: 'add a tests directory' }
];

test('plan mode off sends the thread unchanged', () => {
  const sent = planRequest(thread, false);
  assert.equal(sent, thread);          // the same array: nothing is copied or touched
});

test('plan mode prefixes only the last user message', () => {
  const sent = planRequest(thread, true);
  assert.equal(sent.length, thread.length);
  assert.deepEqual(sent.slice(0, -1), thread.slice(0, -1));
  assert.equal(sent[sent.length - 1].role, 'user');
  assert.equal(sent[sent.length - 1].content, PLAN_PREFIX + 'add a tests directory');
});

test('the thread keeps the words that were typed', () => {
  planRequest(thread, true);
  assert.equal(thread[2].content, 'add a tests directory');
});

test('the prefix says what plan mode must not do', () => {
  assert.match(PLAN_PREFIX, /do not write or edit/);
  assert.match(PLAN_PREFIX, /number(ed)? plan/i);
  assert.ok(PLAN_PREFIX.trim().length > 40);
});

test('a history ending in an answer is not prefixed', () => {
  const ended = [{ role: 'user', content: 'hi' }, { role: 'assistant', content: 'hello' }];
  assert.equal(planRequest(ended, true), ended);
});

test('an empty history is not prefixed', () => {
  const empty: { role: string; content: string }[] = [];
  assert.equal(planRequest(empty, true), empty);
});
