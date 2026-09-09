// Every view the admin panel routes to must exist, and must be in the bundle.
//
// SYNC.md's rule for a new design file is that it needs THREE things — the
// prototype's <script> tag, an app/src symlink, and a main.jsx import — because
// with only the first one `vite build` SUCCEEDS and silently omits the file.
// The screen then works in the raw prototype and renders blank in the built app,
// which is the hardest shape of bug to notice: nothing errors at build time and
// the place you check first looks right.
//
// The 2026-09-09 (b) round is the case that makes this worth pinning. The audit
// view MOVED out of qh-admin-insights.jsx into a new file, so the panel's route
// changed from `AuditView2` to `AuditView` — two files, one new, and if either
// half of that had been missed the Insights → Audit trail route would render
// nothing. This test reads the routes out of the panel and resolves each one
// against the files main.jsx actually imports.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const read = (f) => readFileSync(resolve(HERE, '..', 'src', f), 'utf8');
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/^\s*\/\/.*$/gm, ' ');

/** The .jsx files main.jsx imports, in load order — i.e. what the bundle holds. */
function bundled() {
  return [...read('main.jsx').matchAll(/^\s*import\s+'\.\/([^']+\.jsx)'/gm)].map(m => m[1]);
}

test('every admin route names a component the bundle defines', () => {
  const files = bundled();
  const sources = new Map(files.map(f => [f, strip(read(f))]));

  // `{curNav === 'x' && <SomeView ... />}` — the panel's routing table.
  const panel = sources.get('qh-admin.jsx');
  assert.ok(panel, 'qh-admin.jsx is not imported by main.jsx');
  const routes = [...panel.matchAll(/curNav === '([a-z]+)' && <([A-Za-z0-9_]+)/g)]
    .map(m => ({ section: m[1], component: m[2] }));
  assert.ok(routes.length >= 10, 'found only ' + routes.length + ' admin routes');

  const missing = [];
  for (const r of routes) {
    const defined = files.some(f => new RegExp('function\\s+' + r.component + '\\s*\\(').test(sources.get(f)));
    if (!defined) missing.push(`#admin/${r.section} -> <${r.component}>`);
  }
  assert.deepEqual(missing, [],
    'these admin sections route to a component no bundled file defines:\n  ' + missing.join('\n  '));
});

test('every section in the deep-link vocabulary has a route', () => {
  // `QH_ADMIN_SECTIONS` is what `#admin/<section>` accepts. A name in there with
  // no route resolves, sets the nav, and renders an empty pane.
  const panel = strip(read('qh-admin.jsx'));
  const listed = (panel.match(/const QH_ADMIN_SECTIONS = \[([^\]]+)\]/) || [])[1];
  assert.ok(listed, 'QH_ADMIN_SECTIONS not found');
  const sections = [...listed.matchAll(/'([a-z]+)'/g)].map(m => m[1]);
  const routed = new Set([...panel.matchAll(/curNav === '([a-z]+)'/g)].map(m => m[1]));
  const orphans = sections.filter(s => !routed.has(s));
  assert.deepEqual(orphans, [], 'deep-linkable sections with no route: ' + orphans.join(', '));
});

test('a file the panel needs is not left out of the build wiring', () => {
  // The other half of the same failure: the <script> tag exists in the
  // prototype, so the raw page works, but main.jsx never imports the file.
  const html = readFileSync(resolve(HERE, '..', '..', 'QueryHub.html'), 'utf8');
  const tagged = [...html.matchAll(/<script type="text\/babel" src="(qh-[^"]+\.jsx)"/g)].map(m => m[1]);
  const imported = new Set(bundled());
  // qh-api-mock.jsx is prototype-only by design (SYNC.md §3) — the built app
  // loads the real qh-api.jsx in its place.
  const gap = tagged.filter(f => f !== 'qh-api-mock.jsx' && !imported.has(f));
  assert.deepEqual(gap, [],
    'in the prototype but not in the build (vite would omit these silently): ' + gap.join(', '));
});
