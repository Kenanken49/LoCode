/* Plan mode's request contract, kept out of the store so it can be tested
   without a browser.

   The thread keeps the words the developer typed — that is what they will see
   and what a restored session replays — and only the request carries the
   instruction to investigate and propose instead of editing. `>` in the command
   center filters commands; this prefix is what actually stops the turn from
   writing. */

import type { ChatMessage } from './types';

export const PLAN_PREFIX = 'Plan mode: do not write or edit any file and do not run anything that changes '
  + 'the workspace. Investigate first, then reply with a short numbered plan — what you would change, '
  + 'in which files, and the risks — and stop. I will approve it before any edit.\n\n';

/* The messages to send for one turn. Only a turn that ends with a user message
   can be a plan request: a history that ends with an answer is not something to
   instruct, and an empty one has nothing to prefix. */
export function planRequest(messages: ChatMessage[], planMode: boolean): ChatMessage[] {
  if (!planMode || !messages.length) return messages;
  const last = messages[messages.length - 1];
  if (last.role !== 'user') return messages;
  return [...messages.slice(0, -1), { role: 'user', content: PLAN_PREFIX + last.content }];
}
