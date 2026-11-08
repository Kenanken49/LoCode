/* Changes: the git view of the workspace.

   /api/git/status answers the branch and `git status --short` verbatim, so this
   drawer prints those lines instead of re-describing them, and commits through
   /api/git/commit, which returns git's own output. That output is what makes
   "nothing to commit" arrive as a sentence rather than as a failed request.

   A row opens /api/git/diff for that file. The status line says *what* changed
   and the diff says *how* — a commit message has to be written against the
   second one, which is why the form stays at the foot while the diff is open.

   No poller: git status walks the disk, and the drawer is only open when someone
   asked for it. Opening refreshes, the ⟳ button refreshes, a commit refreshes. */

import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import {
  closeChanges, closeFileDiff, commitChanges, initRepo, openFileDiff, refreshGit, useApp
} from '../store';

type Kind = 'new' | 'mod' | 'del' | 'ren' | 'untracked';

interface Row { code: string; path: string; target: string; kind: Kind; }

const KINDS: Kind[] = ['new', 'mod', 'del', 'ren', 'untracked'];

const SUMMARY: Record<Kind, string> = {
  new: 'new', mod: 'modified', del: 'deleted', ren: 'renamed', untracked: 'untracked'
};

/* `XY path` — two status columns, a space, then the path. A rename appends
   ` -> old`, which is left on the path on purpose: it is what git said. The
   diff is asked for `target`, the name the file has now. */
function parse(status: string): Row[] {
  return status.split('\n').map((line) => line.replace(/\s+$/, '')).filter(Boolean).map((line) => {
    const code = line.slice(0, 2).trim() || 'M';
    const path = (line.slice(3) || line).trim();
    const target = path.includes(' -> ') ? path.slice(path.indexOf(' -> ') + 4) : path;
    const kind: Kind = code === '??' ? 'untracked'
      : code.includes('D') ? 'del'
        : code.includes('A') ? 'new'
          : code.includes('R') ? 'ren'
            : 'mod';
    return { code, path, target, kind };
  });
}

/* Which kind of line this is, from git's own prefixes. The file headers are
   checked first: `+++` and `---` would otherwise read as an added and a removed
   line. */
function lineClass(line: string): string {
  if (line.startsWith('diff ') || line.startsWith('+++') || line.startsWith('---')
    || line.startsWith('index ') || line.startsWith('new file') || line.startsWith('deleted file')) {
    return 'diff-meta';
  }
  if (line.startsWith('@@')) return 'diff-hunk';
  if (line.startsWith('+')) return 'diff-add';
  if (line.startsWith('-')) return 'diff-del';
  return '';
}

export function Changes() {
  const changes = useApp((s) => s.changes);
  const [message, setMessage] = useState('');
  const rows = parse(changes.status);
  const excluded = changes.excluded;
  const diff = changes.diff;
  const lines = diff.text ? diff.text.replace(/\n$/, '').split('\n') : [];

  useEffect(() => {
    if (changes.open) void refreshGit();
  }, [changes.open]);

  const counts = rows.reduce<Record<Kind, number>>(
    (acc, row) => { acc[row.kind] += 1; return acc; },
    { new: 0, mod: 0, del: 0, ren: 0, untracked: 0 });

  const summary = KINDS.filter((k) => counts[k] > 0)
    .map((k) => counts[k] + ' ' + SUMMARY[k]).join(' · ');

  const onSubmit = (e: FormEvent<HTMLFormElement>): void => {
    e.preventDefault();
    void commitChanges(message);
  };

  return (
    <aside id="changes" className={changes.open ? 'open' : ''} hidden={!changes.open}
      aria-label="Changes">
      <div className="chg-head">
        <span>Changes</span>
        <div className="chg-actions">
          <button className="icon-btn" type="button" title="Re-read git status"
            aria-label="Refresh git status" onClick={() => { void refreshGit(); }}>⟳</button>
          <button className="icon-btn" type="button" title="Close" aria-label="Close"
            onClick={closeChanges}>✕</button>
        </div>
      </div>

      <div className="chg-branch">
        <span className="mono">
          {changes.initialized ? (changes.branch || '(no commits yet)') : 'no repository'}
        </span>
        <span className="chg-sum">
          {summary || (changes.initialized ? 'clean' : '')}
        </span>
      </div>

      <div className="chg-body">
        {diff.path ? (
          <>
            <div className="diff-head">
              <button className="diff-back" type="button" onClick={closeFileDiff}>
                ‹ all
              </button>
              <span className="mono" title={diff.path}>{diff.path}</span>
            </div>

            {diff.busy && <p className="chg-note">Reading the diff…</p>}
            {diff.error && <p className="chg-note">{diff.error}</p>}
            {!diff.busy && !diff.error && lines.length === 0 && (
              <p className="chg-note">git reports no difference in this file.</p>
            )}
            {lines.length > 0 && (
              <pre className="chg-diff" aria-label={'Diff of ' + diff.path}>
                {lines.map((line, i) => (
                  <span key={i} className={lineClass(line)}>{line || ' '}</span>
                ))}
              </pre>
            )}
          </>
        ) : (
          <>
            {rows.length > 0 && (
              <ul className="chg-list">
                {rows.map((row) => (
                  <li key={row.code + ' ' + row.path}>
                    <button className="chg-row" type="button" data-kind={row.kind}
                      title={SUMMARY[row.kind] + ' · ' + row.code + ' · open the diff'}
                      onClick={() => openFileDiff(row.target)}>
                      <span className="chg-code mono">{row.code}</span>
                      <span className="chg-path">{row.path}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}

            {changes.initialized && rows.length === 0 && (
              <p className="chg-note">Nothing to commit — the working tree matches HEAD.</p>
            )}

            {!changes.initialized && (
              <p className="chg-note">
                This workspace is not a git repository. Initializing runs <code>git init</code> and
                commits what is here now.
              </p>
            )}

            {/* The drawer is the only place a missing transcript would be noticed, so
                it names what git is not showing and why. The engine reports only the
                records that exist, so this line is never an advert for empty paths. */}
            {changes.initialized && excluded.length > 0 && (
              <p className="chg-note" id="chg-own">
                LOCODE keeps <code>{excluded.join(', ')}</code> out of git — its own records,
                not the project's work, so they never appear here or in a commit.
              </p>
            )}
          </>
        )}

        {changes.note && <pre className="chg-out">{changes.note}</pre>}
      </div>

      <form className="chg-foot" onSubmit={onSubmit}>
        {changes.initialized ? (
          <>
            <input value={message} spellCheck={false} placeholder="Commit message"
              aria-label="Commit message"
              onChange={(e) => setMessage(e.target.value)} />
            <button className="btn btn-primary" type="submit" disabled={changes.busy}>
              {changes.busy ? '…' : 'Commit all'}
            </button>
          </>
        ) : (
          <button className="btn" type="button" disabled={changes.busy}
            onClick={() => { void initRepo(); }}>
            {changes.busy ? '…' : 'Initialize repository'}
          </button>
        )}
      </form>
    </aside>
  );
}
