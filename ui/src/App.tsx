import { useEffect } from 'react';
import { Header } from './components/Header';
import { Rail } from './components/Rail';
import { Panels } from './components/Panels';
import { NewProject } from './components/NewProject';
import { Tree } from './components/Tree';
import { Thread } from './components/Thread';
import { Composer } from './components/Composer';
import { Approvals } from './components/Approvals';
import { Inbox } from './components/Inbox';
import { Modal } from './components/Modal';
import { Preview } from './components/Preview';
import { Fusion } from './components/Fusion';
import { Changes } from './components/Changes';
import { boot, pollApprovals, useApp } from './store';

const POLL_MS = 700;   // chunk 7's cadence

export function App() {
  const sidebarOpen = useApp((s) => s.sidebarOpen);

  useEffect(() => { void boot(); }, []);

  /* Approvals are polled on their own timer, so a stuck card cannot block the
     thread (chunk 7). */
  useEffect(() => {
    void pollApprovals();
    const timer = setInterval(() => { void pollApprovals(); }, POLL_MS);
    const onVisible = (): void => { if (!document.hidden) void pollApprovals(); };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, []);

  return (
    <div id="app" data-sidebar={sidebarOpen ? 'open' : ''}>
      {/* thin left rail: Home · Integrations · Settings */}
      <Rail />

      <div id="stage">
        {/* Chunk 3 · header bar */}
        <Header />

        {/* Chunk 2 · layout grid */}
        <main id="layout">
          {/* Chunk 4 · workspace tree */}
          <Tree />

          <section id="col">
            {/* Chunk 5 + 6 · thread and tool cards */}
            <Thread />
            {/* Chunk 11 · preview pane, between messages and composer */}
            <Preview />
            <Composer />
          </section>
        </main>
      </div>

      {/* Chunk 7 · approval cards float above everything (security gate) */}
      <Approvals />
      {/* Chunk 8 · inbox drawer */}
      <Inbox />
      {/* Chunks 9 + 10 · shared modal shell */}
      <Modal />
      {/* Settings / Integrations, opened by the rail */}
      <Panels />
      {/* New project dialog, opened from the workspace area */}
      <NewProject />
      {/* Changes drawer: git status + commit, opened from ⌘K */}
      <Changes />
      {/* ⌘K command center (Fusion shell), above every panel and card */}
      <Fusion />
    </div>
  );
}
