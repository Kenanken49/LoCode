import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

/* Chunk 1 + 2 + the per-chunk stylesheets, in cascade order: tokens first,
   responsive last. Vite bundles these into one stylesheet asset. */
import './styles/tokens.css';
import './styles/base.css';
import './styles/shell.css';
import './styles/header.css';
import './styles/tree.css';
import './styles/chat.css';
import './styles/tools.css';
import './styles/approvals.css';
import './styles/inbox.css';
import './styles/modal.css';
import './styles/preview.css';
import './styles/panels.css';
import './styles/project.css';
import './styles/command.css';
import './styles/fusion.css';
import './styles/responsive.css';

import { App } from './App';
import { setStatus } from './store';

/* Chunk 3 asks for a setStatus() exposed on window. The React shell keeps that
   handle so a console or engine-side update still reaches the composer. */
declare global {
  interface Window { setStatus: typeof setStatus }
}
window.setStatus = setStatus;

createRoot(document.getElementById('root') as HTMLElement).render(
  <StrictMode>
    <App />
  </StrictMode>
);
