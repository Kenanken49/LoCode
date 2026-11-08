#!/usr/bin/env node
/* Headless Chromium runner for LOCODE UI checks.

   The hosted browser tool cannot run here: its Playwright sandbox strips
   TypeScript with `mode: "transform"`, and Node 26 only accepts `mode: "strip"`,
   so every script fails with `The property 'options.mode' must be one of:
   'strip'. Received 'transform'` before a page is even opened.
   This runner replaces it locally.

   Usage:
     bash scripts/pw-setup.sh                 # once: browser + its libraries
     source /tmp/locode-playwright/env.sh     # CHROME, LD_LIBRARY_PATH, fontconfig
     node scripts/playwright.mjs scripts/ui-smoke.mjs http://127.0.0.1:3000 artifacts

   The script body runs inside an async function with `page`, `base` and
   `artifactsDir` in scope; its return value is printed as JSON and the process
   exits non-zero when the script throws. Dialogs are accepted (a developer
   pressing OK) and page errors are logged — which is what a UI check wants.
*/
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';

/* playwright-core lives in the scratch dir (PW_ROOT, written by pw-setup.sh), not
   in this repo. NODE_PATH is ignored for ES module imports, so resolve it the
   CommonJS way instead. */
const requireFrom = createRequire((process.env.PW_ROOT || process.cwd()) + '/');
const { chromium } = requireFrom('playwright-core');

const script = process.argv[2];
const base = process.argv[3] || 'http://127.0.0.1:3000';
const artifactsDir = process.argv[4] || 'artifacts';

if (!script) {
  console.error('usage: node scripts/playwright.mjs <script.mjs> [baseUrl] [artifactsDir]');
  process.exit(2);
}

/* CHROME comes from pw-setup.sh: the bundled build that ships its own libraries
   except the three NSS ones, which the sysroot supplies. */
const browser = await chromium.launch({
  executablePath: process.env.CHROME || undefined,
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
});
const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
const page = await context.newPage();

page.on('dialog', async (d) => {
  console.log('[dialog]', d.type(), JSON.stringify(d.message()));
  await d.accept();
});
page.on('console', (m) => { if (m.type() === 'error') console.log('[console.error]', m.text()); });
page.on('pageerror', (e) => console.log('[pageerror]', e.message));

const fn = new Function(
  'page', 'base', 'artifactsDir',
  'return (async () => {\n' + readFileSync(script, 'utf8') + '\n})();'
);

try {
  const out = await fn(page, base, artifactsDir);
  console.log('RESULT ' + JSON.stringify(out, null, 2));
} catch (e) {
  console.error('FAILED ' + (e && e.message));
  process.exitCode = 1;
} finally {
  await browser.close();
}
