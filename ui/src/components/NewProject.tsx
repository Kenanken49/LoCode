/* New project dialog.

   Mirrors POST /api/project one field at a time:
     Project name        → name        (the engine validates it, this only hints)
     Choose folder       → parent      (GET /api/folders, browsing inside the app)
     GitHub repository   → repo        (optional; git clone when filled)
     Start from a template → template  (optional; ignored when a repo is given)
   Create project switches the engine's workspace, so the thread is cleared by
   store.createProject and the sidebar re-reads the tree. */

import { useEffect, useState } from 'react';
import { getFolders, getTemplates } from '../api';
import type { FolderRow, TemplateRow } from '../types';
import { closeNewProject, createProject, useApp } from '../store';

export function NewProject() {
  const open = useApp((s) => s.newProjectOpen);
  const project = useApp((s) => s.project);
  const [name, setName] = useState('');
  const [parent, setParent] = useState('');
  const [repo, setRepo] = useState('');
  const [template, setTemplate] = useState('empty');
  const [browsing, setBrowsing] = useState(false);  // picker is open
  const [loading, setLoading] = useState(false);    // its folder list is in flight
  const [dirs, setDirs] = useState<FolderRow[]>([]);
  const [at, setAt] = useState('');
  const [up, setUp] = useState<string | null>(null);
  const [templates, setTemplates] = useState<TemplateRow[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  /* Opens where the current project lives, so the common case is one click. */
  useEffect(() => {
    if (!open) return;
    setError('');
    setBusy(false);
    setBrowsing(false);
    setLoading(false);
    setRepo('');
    setName('');
    setParent(project.root || project.parent || '');
    setTemplate('empty');
    getTemplates()
      .then((r) => setTemplates(r.templates || []))
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, [open, project.root, project.parent]);

  const browse = (path: string): void => {
    setLoading(true);
    getFolders(path)
      .then((r) => {
        setDirs(r.dirs || []);
        setAt(r.path || '');
        setUp(r.parent ?? null);
        setBrowsing(true);
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  };

  if (!open) return null;

  const submit = async (): Promise<void> => {
    if (busy) return;
    setError('');
    if (!name.trim()) { setError('project name is required'); return; }
    setBusy(true);
    const r = await createProject({ name: name.trim(), parent, template, repo: repo.trim() });
    setBusy(false);
    if (!r.ok) setError(r.error || 'could not create the project');
  };

  return (
    <div id="project-modal" role="dialog" aria-modal="true" aria-labelledby="project-title"
      onMouseDown={(e) => { if (e.target === e.currentTarget) closeNewProject(); }}>
      <div className="project-card">
        <div className="project-head">
          <h2 id="project-title">New project</h2>
          <button className="icon-btn" type="button" aria-label="Close" onClick={closeNewProject}>✕</button>
        </div>

        <label className="field">
          <span className="field-label">Project name</span>
          <input className="input" value={name} autoFocus placeholder="my-app"
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') void submit(); }} />
        </label>

        <div className="field">
          <span className="field-label">Choose folder</span>
          <div className="folder-row">
            <input className="input mono" value={parent} readOnly
              placeholder={project.path ? project.path.replace(/\/[^/]+$/, '') : '(app folder)'}
              title={parent || 'the projects folder of the app'} />
            <button className="btn" type="button" disabled={loading}
              onClick={() => browse(parent)}>{loading ? 'Browsing…' : 'Choose folder'}</button>
          </div>
          {browsing && (
            <div className="folder-browser">
              <div className="folder-here" title={at}>
                <span className="folder-path">{at}</span>
                <button className="btn btn-sm" type="button"
                  onClick={() => { setParent(at); setBrowsing(false); }}>Use this folder</button>
              </div>
              {up && (
                <button className="folder-row-btn" type="button" onClick={() => browse(up)}>↑ ..</button>
              )}
              {dirs.map((d) => (
                <button key={d.path} className="folder-row-btn" type="button"
                  title={d.path} onClick={() => browse(d.path)}>▸ {d.name}</button>
              ))}
              {dirs.length === 0 && <div className="folder-none">no sub-folders here</div>}
            </div>
          )}
        </div>

        <label className="field">
          <span className="field-label">GitHub repository <span className="opt">optional</span></span>
          <input className="input mono" value={repo} placeholder="https://github.com/owner/repo"
            onChange={(e) => setRepo(e.target.value)} />
          <span className="field-hint">
            When filled this clones the repository and the template is ignored.
          </span>
        </label>

        <label className="field">
          <span className="field-label">Start from a template</span>
          <select className="input" value={template} disabled={Boolean(repo.trim())}
            onChange={(e) => setTemplate(e.target.value)}>
            {templates.length === 0
              ? <option value="empty">empty</option>
              : templates.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
          </select>
          {repo.trim() !== '' && <span className="field-hint">disabled while a repository is set</span>}
        </label>

        {error && <div className="project-error" role="alert">{error}</div>}

        <div className="project-foot">
          <button className="btn" type="button" onClick={closeNewProject}>Cancel</button>
          <button className="btn btn-primary" type="button" disabled={busy} onClick={() => { void submit(); }}>
            {busy ? 'Creating…' : 'Create project'}
          </button>
        </div>
      </div>
    </div>
  );
}
