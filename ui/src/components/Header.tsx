/* Header bar: project context on the left, runtime readouts on the right.

   Controls moved out of here: New / refresh / theme / base URL are in the rail's
   Settings panel, providers are in Integrations, and the model picker sits in
   the composer with the prompt it applies to. What stays is what the header is
   for — where you are (pills, dev command, preview) and what confines the work
   (sandbox chip, net consent). */

import {
  restartSandbox, startPreview, stopServer, toggleSidebar, useApp
} from '../store';
import type { ProjectInfo, SandboxStatus } from '../types';

function StackPills({ project }: { project?: ProjectInfo }) {
  const stack = project?.stack || [];
  const frameworks = project?.frameworks || [];
  const all = [...stack, ...frameworks];
  return (
    <div id="stack-pills" className="stack-pills" aria-label="Detected stack">
      {all.map((tag, i) => (
        /* Chunk 12 truncates by class, never by counting inline styles: the
           first pill survives every band, anything past the third is an extra. */
        <span key={tag + '-' + i}
          className={'pill' + (frameworks.includes(tag) ? ' framework' : '')
            + (i === 0 ? ' lead' : i >= 3 ? ' extra' : '')}>
          {tag}
        </span>
      ))}
      {all.length > 1 && (
        <span className={'pill more' + (all.length > 3 ? '' : ' few')}>
          {all.length > 3 && <span className="n">+{all.length - 3}</span>}
          <span className="e">…</span>
        </span>
      )}
    </div>
  );
}

function SandboxChip({ status }: { status: SandboxStatus | null }) {
  const mode = status?.mode || 'off';
  /* a configured cap that cannot run (daemon down) is not an enforced cap */
  const usable = status?.available !== false;
  const caps = Boolean(status?.limits) && usable;
  /* the numbers themselves, not just the ✓: "2g/2c" is what the runtime caps */
  const limits = caps
    ? [status?.memory, status?.cpus && status.cpus + 'c'].filter(Boolean).join('/')
    : '';
  const detail = !usable
    ? 'backend unavailable: ' + (status?.why || status?.mode || 'not ready')
    : caps
      ? [status?.memory && 'memory ' + status.memory, status?.cpus && 'cpus ' + status.cpus,
          status?.limits].filter(Boolean).join(' · ')
      : 'commands run on the host, so no memory or CPU caps';
  /* the chip shows the backend that was resolved, so say where it came from when
     SANDBOX=auto picked something other than the literal request */
  const resolved = status?.requested && status.requested !== mode
    ? ' (resolved from ' + status.requested + ')' : '';
  const label = 'Sandbox: ' + mode + resolved + ' — ' + detail;
  const busy = useApp((s) => s.sandboxBusy);
  const note = useApp((s) => s.sandboxNote);
  const noteTitle = useApp((s) => s.sandboxNoteTitle);
  const off = mode === 'off';
  const noteKind = busy ? 'busy' : note.startsWith('failed') ? 'err' : note ? 'ok' : '';
  return (
    /* a button, not a label: it restarts the sandbox and reports right here */
    <button id="sandbox" type="button" className="sbx" data-box={mode}
      data-caps={caps ? '1' : '0'} data-note={noteKind}
      disabled={busy || off}
      title={note
        ? (noteTitle ? note + ' — ' + noteTitle : note)
        : busy ? 'Restarting the sandbox…'
        : label + (off ? ' · restart unavailable while the sandbox is off' : ' · click to restart')}
      aria-label={label + (off ? '' : ' · click to restart')}
      onClick={() => { void restartSandbox(); }}>
      <span className="dot" aria-hidden="true" />
      {note
        ? <span id="sandbox-text" className="note" role="status">{note}</span>
        : (<>             <span className="lab">sandbox: </span>             <span id="sandbox-text">{mode}</span>             {limits && <span className="lim" aria-label={'limits ' + limits}>{limits}</span>}             <span className="caps" aria-hidden="true">{caps ? '✓' : '✕'}</span>           </>)}
    </button>
  );
}

export function Header() {
  const health = useApp((s) => s.health);
  const netMode = useApp((s) => s.netMode);
  const sandbox = useApp((s) => s.sandbox);
  const server = useApp((s) => s.server);

  const project = health?.project;
  const devCommand = project?.dev_command || '';
  const netKind = netMode.includes('ask') ? 'ask' : netMode.includes('auto-safe') ? 'auto-safe' : 'off';

  return (
    <header id="topbar">
      <button id="menu-btn" className="icon-btn" type="button"
        aria-label="Open workspace" title="Workspace" onClick={toggleSidebar}>☰</button>

      <div id="project-bar" className="project-bar">
        <StackPills project={project} />
        <code id="dev-command" className="devcmd mono" data-empty={devCommand ? '0' : '1'}
          title={'Dev command for ' + (project?.name || 'workspace')}>{devCommand}</code>
        <button id="preview-toggle" className="btn" type="button"
          hidden={!!server.name} onClick={startPreview}>▶ Preview</button>
        <button id="server-stop" className="btn btn-ghost" type="button"
          hidden={!server.name} onClick={() => { void stopServer(); }}>
          ■ Stop <span id="server-name">{server.name ? server.name + ':' + server.port : ''}</span>
        </button>
      </div>

      <div className="actions">
        <SandboxChip status={sandbox} />
        <span id="net" className="net" data-net={netKind}
          title="Web access consent mode — approvals appear as cards">
          <span className="dot" id="net-dot" />
          <span id="net-text">net: {netMode}</span>
        </span>
      </div>
    </header>
  );
}
