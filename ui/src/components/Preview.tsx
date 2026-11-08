/* Chunk 11 · live preview pane.

   ▶ Preview does not shell out: it asks the agent to run `start_server`, so the
   same approval + tool-card path covers it. The SSE stream reports the port
   (server_started) and the pane frames it through /preview/<name>/ — a
   same-origin proxy the engine serves, so the iframe needs no CORS.

   Close hides the pane and drops the iframe's src; the tracked server keeps
   running, which is why the header still shows Stop afterwards. */

import { hidePreview, reloadPreview, setStatus, showLogs, useApp } from '../store';

export function Preview() {
  const preview = useApp((s) => s.preview);
  const src = preview.name
    ? '/preview/' + encodeURIComponent(preview.name) + '/' + (preview.nonce ? '?r=' + preview.nonce : '')
    : undefined;

  return (
    <div id="preview-pane" hidden={!preview.visible}>
      <div className="pv-bar">
        <span id="pv-url" className="pv-url mono">
          {preview.name
            ? 'http://127.0.0.1:' + preview.port + '/  →  /preview/' + preview.name + '/'
            : ''}
        </span>
        <span className="pv-actions">
          <button id="pv-reload" className="icon-btn" type="button" title="Reload preview"
            onClick={reloadPreview}>⟳</button>
          <button id="pv-logs" className="btn btn-ghost" type="button"
            onClick={() => { if (preview.name) void showLogs(preview.name); }}>Logs</button>
          <button id="pv-close" className="icon-btn" type="button"
            title="Close pane (server keeps running)"
            onClick={() => { hidePreview(); setStatus('preview closed (server still running)', 'idle'); }}>✕</button>
        </span>
      </div>
      <iframe id="pv-frame" title="App preview"
        sandbox="allow-scripts allow-same-origin allow-forms" src={src} />
    </div>
  );
}
