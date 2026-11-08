/* Chunk 4 · workspace area.

   Two views behind one segmented control:
     Code — GET /api/files → the file tree, plus the open project's name
     Chat — the conversation this project keeps, and New chat
   The ＋ in the head is contextual: it opens New project on the Code tab and
   starts a fresh chat on the Chat tab.

   Tree behaviour is unchanged from chunk 4: ▸ dirs fold on the glyph and insert
   "path/" on the row, · files open the preview modal, indent is 13px per level. */

import { useState } from 'react';
import type { MouseEvent, ReactNode } from 'react';
import {
  insertPath, newSession, openFile, openNewProject, refreshTree, useApp
} from '../store';

interface FileLeaf { name: string; path: string }
interface DirNode { dirs: Map<string, DirNode>; files: FileLeaf[]; path: string }

function buildTree(paths: string[]): DirNode {
  const root: DirNode = { dirs: new Map(), files: [], path: '' };
  for (const p of paths) {
    const parts = String(p).split('/');
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      let next = node.dirs.get(parts[i]);
      if (!next) {
        next = { dirs: new Map(), files: [], path: parts.slice(0, i + 1).join('/') };
        node.dirs.set(parts[i], next);
      }
      node = next;
    }
    node.files.push({ name: parts[parts.length - 1], path: p });
  }
  return root;
}

function Row({ cls, glyph, name, path, depth, open, onClick }: {
  cls: 'dir' | 'file'; glyph: string; name: string; path: string;
  depth: number; open?: boolean; onClick: (e: MouseEvent<HTMLDivElement>) => void;
}) {
  return (
    <div className={'tree-node ' + cls}
      style={{ paddingLeft: 'calc(' + depth + ' * var(--indent) + 8px)' }}
      title={path} data-open={open === undefined ? undefined : open ? '1' : '0'}
      onClick={onClick}>
      <span className="glyph">{glyph}</span>
      <span className="name">{name}</span>
    </div>
  );
}

const byName = (a: { name: string }, b: { name: string }): number => a.name.localeCompare(b.name);

function Node({ node, depth, collapsed, fold, onDir, onFile }: {
  node: DirNode; depth: number; collapsed: Set<string>;
  fold: (path: string, open: boolean) => void;
  onDir: (path: string) => void; onFile: (path: string) => void;
}): ReactNode {
  const out: ReactNode[] = [];
  const dirs = [...node.dirs.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  for (const [name, child] of dirs) {
    const open = !collapsed.has(child.path);
    out.push(
      <Row key={'d-' + child.path} cls="dir" glyph={open ? '▾' : '▸'} name={name + '/'}
        path={child.path} depth={depth} open={open}
        onClick={(e) => {
          // the glyph folds, the rest of the row hands the path to the composer
          if ((e.target as HTMLElement).classList.contains('glyph')) fold(child.path, open);
          else onDir(child.path);
        }} />,
      <div key={'k-' + child.path} className="tree-children" hidden={!open}>
        <Node node={child} depth={depth + 1} collapsed={collapsed} fold={fold} onDir={onDir} onFile={onFile} />
      </div>
    );
  }
  for (const f of node.files.slice().sort(byName)) {
    out.push(
      <Row key={'f-' + f.path} cls="file" glyph="·" name={f.name} path={f.path} depth={depth}
        onClick={() => onFile(f.path)} />
    );
  }
  return out;
}

export function Tree() {
  const files = useApp((s) => s.files);
  const treeMs = useApp((s) => s.treeMs);
  const treeError = useApp((s) => s.treeError);
  const messages = useApp((s) => s.messages);
  const project = useApp((s) => s.project);
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [tab, setTab] = useState<'code' | 'chat'>('code');

  const fold = (path: string, wasOpen: boolean): void => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (wasOpen) next.add(path); else next.delete(path);
      return next;
    });
  };

  /* The conversation's own title: what it was opened with, not a generated one. */
  const firstUser = messages.find((m) => m.role === 'user');
  const chatTitle = (firstUser?.text || '').split('\n')[0].slice(0, 70) || 'New conversation';
  const turns = messages.filter((m) => m.role !== 'error').length;

  return (
    <aside id="sidebar" aria-label="Workspace">
      <div className="side-head">
        <div className="ws-tabs" role="tablist" aria-label="Workspace view">
          <button type="button" role="tab" className="ws-tab" aria-selected={tab === 'code'}
            onClick={() => setTab('code')}>Code</button>
          <button type="button" role="tab" className="ws-tab" aria-selected={tab === 'chat'}
            onClick={() => setTab('chat')}>Chat</button>
        </div>
        <button className="icon-btn" type="button"
          aria-label={tab === 'code' ? 'New project' : 'New chat'}
          title={tab === 'code' ? 'New project' : 'New chat'}
          onClick={() => { if (tab === 'code') openNewProject(); else void newSession(); }}>＋</button>
      </div>

      {tab === 'code' ? (
        <>
          <div className="ws-project" title={project.path || 'workspace'}>
            <span className="ws-name">{project.name || 'workspace'}</span>
            <button id="tree-refresh" className="icon-btn" type="button" title="Refresh file tree"
              onClick={() => { void refreshTree(); }}>⟳</button>
          </div>

          <div id="file-tree" className="tree">
            {treeError
              ? <div className="empty">tree: {treeError}</div>
              : files.length === 0
                ? <div className="empty">(empty workspace)</div>
                : <Node node={buildTree(files)} depth={0} collapsed={collapsed} fold={fold}
                    onDir={(p) => insertPath(p + '/')} onFile={(p) => { void openFile(p); }} />}
          </div>

          <div className="tree-count">
            {files.length ? files.length + ' files · ' + treeMs + ' ms' : 'no files yet'}
          </div>
        </>
      ) : (
        <div className="chat-list">
          <button type="button" className="chat-item" aria-current="true"
            title="The conversation open in the column">
            <span className="chat-title">{chatTitle}</span>
            <span className="chat-meta">{turns} message{turns === 1 ? '' : 's'} · current</span>
          </button>
          <button type="button" className="btn chat-new" onClick={() => { void newSession(); }}>
            ＋ New chat
          </button>
          <p className="chat-note">
            This build keeps one conversation per project. New chat clears this one and the
            agent's memory of it — your files stay.
          </p>
        </div>
      )}
    </aside>
  );
}
