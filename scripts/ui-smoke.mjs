/* LOCODE workspace-area smoke check, run through scripts/playwright.mjs.

   Covers the part of the shell that only a real browser exercises: the Code/Chat
   tabs, New chat through the head entry, and the New project dialog end to end
   (labels, template list, folder browser, create, duplicate name, cancel).

   The command center is checked here too, because it is all keyboard and overlay:
   the plan chip (its own pressed state, the placeholder it swaps, and the tip line
   of the Fusion shell that must agree), Ctrl+K opening with the caret in the
   input, `>` narrowing the list to commands, a filter that leaves one row, Enter
   running it, and the Changes drawer it opens — branch, summary, a row's diff,
   `‹ all` back, and closing. For the drawer to have something to show, point this
   at a workspace that is a git repository with a modified and an untracked file.

   The other two Fusion panes are driven for real as well: the doctor runs its
   checks against the engine and must reach an EXIT line, and the export builds a
   report from live state and hashes it.

   It creates projects/pw-demo, which switches the engine's workspace — run it
   against a throwaway engine, or delete the folder and .locode/workspace.json
   afterwards. Returns everything it saw so the runner prints it as JSON. */

const out = {};
await page.goto(base + '/', { waitUntil: 'load' });
await page.waitForSelector('.ws-tab', { timeout: 20000 });

out.viewport = [page.viewportSize().width, page.viewportSize().height];
out.tabs = await page.locator('.ws-tab').allTextContents();
out.codeSelected = await page.locator('.ws-tab').nth(0).getAttribute('aria-selected');
out.project = (await page.locator('.ws-name').textContent()).trim();
out.plusTitle = await page.locator('#sidebar .side-head .icon-btn').getAttribute('title');
out.treeRows = await page.locator('#file-tree .tree-node').allTextContents();

// Chat tab
await page.locator('.ws-tab').nth(1).click();
await page.waitForSelector('.chat-list');
out.chatTitle = (await page.locator('.chat-title').textContent()).trim();
out.chatMeta = (await page.locator('.chat-meta').textContent()).trim();
out.plusTitleOnChat = await page.locator('#sidebar .side-head .icon-btn').getAttribute('title');

// New chat (the runner accepts the confirm)
await page.locator('#sidebar .side-head .icon-btn').click();
await page.waitForTimeout(1000);
out.afterNewChat = [
  (await page.locator('.chat-title').textContent()).trim(),
  (await page.locator('.chat-meta').textContent()).trim()
];
out.messageNodes = await page.locator('#messages > *').count();

// ── plan chip ────────────────────────────────────────────────────────────
const chip = page.locator('#plan-toggle');
await chip.click();
await page.waitForTimeout(150);
out.planOn = {
  pressed: await chip.getAttribute('aria-pressed'),
  label: (await chip.textContent()).trim(),
  placeholder: await page.locator('#input').getAttribute('placeholder')
};
await page.keyboard.press('Control+k');
await page.waitForSelector('#fusion');
out.planOn.fusionTip = (await page.locator('.fx-omni-tip').textContent()).replace(/\s+/g, ' ').trim();
await page.keyboard.press('Escape');
await page.waitForTimeout(150);
out.planOn.fusionClosedByEsc = (await page.locator('#fusion').count()) === 0;
await chip.click();
await page.waitForTimeout(150);
out.planOff = {
  pressed: await chip.getAttribute('aria-pressed'),
  label: (await chip.textContent()).trim(),
  placeholder: await page.locator('#input').getAttribute('placeholder')
};

// ── ⌘K command center (Fusion shell) ─────────────────────────────────────
await page.keyboard.press('Control+k');
await page.waitForSelector('#fusion-rows .fx-row');
out.fusion = {
  rows: await page.locator('#fusion-rows .fx-row').count(),
  focused: await page.evaluate(() => document.activeElement && document.activeElement.id),
  commands: await page.locator('#fusion-rows .fx-row[data-row="command"] .label').allTextContents(),
  selected: (await page.locator('#fusion-rows .fx-row[aria-selected="true"] .label').textContent()).trim(),
  sessionRows: await page.locator('#fusion-rows .fx-row[data-row="session"]').count(),
  sidebarSessions: await page.locator('.fx-session').count(),
  foot: (await page.locator('.fx-omni-foot').textContent()).replace(/\s+/g, ' ').trim(),
  getStarted: (await page.locator('.fx-getstarted-head').textContent()).replace(/\s+/g, ' ').trim(),
  telemetry: (await page.locator('.fx-foot').textContent()).replace(/\s+/g, ' ').trim()
};
// #fusion fades in over 120ms; a screenshot inside that window is translucent and
// shows the chat UI behind it, which is not what the shell looks like settled
await page.waitForTimeout(250);
await page.screenshot({ path: artifactsDir + '/pw-fusion-console.png' });

// `>` is the documented way to keep file and session rows out of a command search
await page.locator('#fusion-input').fill('>');
await page.waitForTimeout(150);
out.fusion.commandsOnly = await page.locator('#fusion-rows .fx-row').count();
out.fusion.commandsOnlySessionRows = await page.locator('#fusion-rows .fx-row[data-row="session"]').count();

await page.locator('#fusion-input').fill('>changes');
await page.waitForTimeout(200);
out.fusion.filtered = await page.locator('#fusion-rows .fx-row .label').allTextContents();
await page.keyboard.press('Enter');
await page.waitForSelector('.chg-row', { timeout: 20000 });
out.fusion.closedAfterRun = (await page.locator('#fusion').count()) === 0;

// ── changes drawer ───────────────────────────────────────────────────────
out.changes = {
  visible: await page.locator('#changes').isVisible(),
  branch: (await page.locator('.chg-branch .mono').textContent()).trim(),
  summary: (await page.locator('.chg-sum').textContent()).replace(/\s+/g, ' ').trim(),
  rows: await page.locator('.chg-row').allTextContents(),
  foot: (await page.locator('.chg-foot').textContent()).replace(/\s+/g, ' ').trim(),
  own: (await page.locator('#chg-own').textContent()).replace(/\s+/g, ' ').trim()
};
await page.screenshot({ path: artifactsDir + '/pw-changes-drawer.png' });

await page.locator('.chg-row', { hasText: 'notes.txt' }).click();
// .diff-head appears as soon as the path is known; the lines arrive with
// /api/git/diff a round trip later, and .chg-diff only exists once they do.
await page.waitForSelector('.chg-diff');
out.diff = {
  head: (await page.locator('.diff-head').textContent()).replace(/\s+/g, ' ').trim(),
  back: (await page.locator('.diff-back').textContent()).trim(),
  hunks: await page.locator('.chg-diff .diff-hunk').count(),
  metaLines: await page.locator('.chg-diff .diff-meta').count(),
  added: await page.locator('.chg-diff .diff-add').allTextContents(),
  removed: await page.locator('.chg-diff .diff-del').allTextContents()
};
await page.screenshot({ path: artifactsDir + '/pw-changes-diff.png' });

await page.locator('.diff-back').click();
await page.waitForSelector('.chg-row');
out.diff.backToList = (await page.locator('.diff-head').count()) === 0
  && (await page.locator('.chg-row').count()) > 0;

// a file git has never seen is a diff from /dev/null, not an empty pane
await page.locator('.chg-row', { hasText: 'extra.txt' }).click();
await page.waitForSelector('.chg-diff');
out.diff.untracked = (await page.locator('.chg-diff .diff-meta').allTextContents())
  .map((line) => line.trim()).filter((line) => line.startsWith('---') || line.startsWith('+++'));
await page.locator('.diff-back').click();
await page.waitForSelector('.chg-row');

await page.locator('#changes .chg-actions button[aria-label="Close"]').click();
await page.waitForTimeout(200);
out.changesClosed = (await page.locator('#changes').isVisible()) === false;

// ── doctor pane: the engine's own answers, measured ──────────────────────
await page.keyboard.press('Control+k');
await page.waitForSelector('#fusion');
await page.locator('.fx-tab', { hasText: 'Doctor' }).click();
await page.waitForSelector('#fx-doctor-run');
await page.locator('#fx-doctor-run').click();
await page.waitForFunction(
  () => (document.querySelector('#fx-trace')?.textContent || '').includes('EXIT'),
  null, { timeout: 30000 });
out.doctor = {
  lines: await page.locator('#fx-trace .fx-line').count(),
  tags: await page.locator('#fx-trace .fx-line .tag').allTextContents(),
  text: (await page.locator('#fx-trace').textContent()).replace(/\s+/g, ' ').trim().slice(0, 400),
  foot: (await page.locator('.fx-doc-foot').textContent()).replace(/\s+/g, ' ').trim()
};
await page.screenshot({ path: artifactsDir + '/pw-fusion-doctor.png' });

// ── export pane: a report built from live state, then hashed ─────────────
await page.locator('.fx-tab', { hasText: 'Export' }).click();
await page.waitForSelector('#fx-preview');
await page.waitForFunction(
  () => ((document.querySelector('#fx-digest')?.textContent || '').trim().replace(/\s/g, '').length >= 64),
  null, { timeout: 15000 });
out.export = {
  rows: await page.locator('.fx-scope-row').count(),
  digestLength: (await page.locator('#fx-digest').textContent()).replace(/\s/g, '').length,
  bytes: (await page.locator('.fx-modal-right .fx-field-label em').textContent()).trim(),
  head: (await page.locator('#fx-preview').textContent()).split('\n').slice(0, 3),
  hasTranscript: (await page.locator('#fx-preview').textContent()).includes('SESSION TRANSCRIPT')
};
await page.locator('.fx-scope-row input').first().uncheck();
await page.waitForTimeout(250);
out.export.afterUncheck = (await page.locator('#fx-preview').textContent()).includes('SESSION TRANSCRIPT');
await page.locator('.fx-format', { hasText: 'Bundle' }).click();
await page.waitForTimeout(300);
out.export.json = (await page.locator('#fx-preview').textContent()).slice(0, 30);

// the PDF is checked as a file: its own bytes are hashed here, independently of
// the hash the panel printed from the same buffer
await page.locator('.fx-format', { hasText: 'Dossier' }).click();
await page.waitForFunction(
  () => (document.querySelector('#fx-preview')?.textContent || '').startsWith('%PDF-1.4'),
  null, { timeout: 15000 });
const fs = await import('node:fs');
const crypto = await import('node:crypto');
const [download] = await Promise.all([
  page.waitForEvent('download'),
  page.locator('#fx-download').click()
]);
const savedPdf = artifactsDir + '/locode-diagnostic.pdf';
await download.saveAs(savedPdf);
const pdfBytes = fs.readFileSync(savedPdf);
const digests = (await page.locator('#fx-digest').textContent()).replace(/\s/g, '');
out.export.pdf = {
  suggested: download.suggestedFilename(),
  bytes: pdfBytes.length,
  magic: pdfBytes.subarray(0, 8).toString('latin1'),
  tail: pdfBytes.subarray(-6).toString('latin1').trim(),
  pageObjects: (pdfBytes.toString('latin1').match(/\/Type \/Page[^s]/g) || []).length,
  browserDigest: digests,
  digestMatchesFile: crypto.createHash('sha256').update(pdfBytes).digest('hex') === digests,
  preview: (await page.locator('#fx-preview').textContent()).split('\n')[0]
};
await page.screenshot({ path: artifactsDir + '/pw-fusion-export.png' });
await page.keyboard.press('Escape');
await page.waitForTimeout(150);
out.fusion.closedByEsc = (await page.locator('#fusion').count()) === 0;

// Code tab → New project
await page.locator('.ws-tab').nth(0).click();
await page.waitForSelector('#file-tree');
await page.locator('#sidebar .side-head .icon-btn').click();
await page.waitForSelector('#project-modal');
out.labels = await page.locator('.project-card .field-label').allTextContents();
out.buttons = await page.locator('.project-card button').allTextContents();
out.templates = await page.locator('.project-card option').allTextContents();
out.defaultFolder = await page.locator('.project-card .folder-row input').inputValue();
out.cardBox = await page.locator('.project-card').boundingBox();

await page.locator('.project-card .folder-row button').click();
await page.waitForSelector('.folder-browser');
out.browserRows = (await page.locator('.folder-row-btn').allTextContents()).slice(0, 5);
await page.screenshot({ path: artifactsDir + '/pw-newproject.png' });

await page.locator('.project-card input').first().fill('pw-demo');
await page.locator('.project-card select').selectOption('static');
await page.locator('.project-card button', { hasText: 'Create project' }).click();
await page.waitForFunction(() => !document.querySelector('#project-modal'), null, { timeout: 20000 });
await page.waitForTimeout(600);
out.afterCreate = [
  (await page.locator('.ws-name').textContent()).trim(),
  await page.locator('#file-tree .tree-node').allTextContents()
];
out.workspace = await page.evaluate(() => fetch('/api/workspace').then((r) => r.json()));
await page.screenshot({ path: artifactsDir + '/pw-after-create.png' });

// The same name again must surface the engine's sentence inside the dialog
await page.locator('#sidebar .side-head .icon-btn').click();
await page.waitForSelector('#project-modal');
await page.locator('.project-card input').first().fill('pw-demo');
await page.locator('.project-card button', { hasText: 'Create project' }).click();
await page.waitForSelector('.project-error', { timeout: 15000 });
out.dupError = (await page.locator('.project-error').textContent()).trim();
await page.locator('.project-card button', { hasText: 'Cancel' }).click();
out.cancelClosed = (await page.locator('#project-modal').count()) === 0;

return out;
