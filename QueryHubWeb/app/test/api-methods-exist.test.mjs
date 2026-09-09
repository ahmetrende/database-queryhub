// Every `qhApi.x()` a screen calls has to exist on the client.
//
// This is not a style rule. `reloadAll` in the admin data hook builds its jobs
// as an ARRAY LITERAL of already-started promises:
//
//     const jobs = [ qhApi.adminQueue().then(...), qhApi.adminGrants().then(...), ... ];
//     return Promise.allSettled(jobs).then(...)
//
// so a name the client does not have is not a rejected promise that
// `allSettled` absorbs — it is a synchronous TypeError thrown while the array
// is being built. `setLoading(true)` has already run and `setLoading(false)`
// never does, which means ONE missing method leaves the whole admin panel
// stuck on its loading state: the approval queue, the audit log, everything,
// not just the screen that wanted the new call.
//
// It nearly shipped that way. The design round of 2026-09-09 added the masking
// exemptions screen, and its data-hook delta calls `adminMaskExemptions` — a
// method that did not exist on this side of the port, because the API client is
// code-owned and the hook is design-owned. Nothing in either file was wrong on
// its own.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import { bareWindow, loadInto } from './_load.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const read = (f) => readFileSync(resolve(HERE, '..', 'src', f), 'utf8');

/** The client, as the app gets it. */
function client() {
  const win = bareWindow();
  win.fetch = async () => ({ ok: true, status: 200, json: async () => ({}), text: async () => '{}' });
  return loadInto(win, 'qh-api.jsx').qhApi;
}

// Callers, in load order. Any file that reaches for the client belongs here.
const CALLERS = [
  'qh-admin-data.jsx', 'qh-data.jsx', 'qh-admin-access.jsx',
  'qh-admin-person.jsx', 'qh-admin-roles.jsx', 'qh-admin-mask.jsx',
  'qh-admin-insights.jsx', 'qh-admin-config.jsx', 'qh-admin.jsx',
  'qh-panels.jsx', 'qh-editor.jsx', 'qh-home.jsx', 'qh-whatsnew.jsx',
];

test('no screen calls a client method that does not exist', () => {
  const api = client();
  const missing = [];
  for (const f of CALLERS) {
    // Comments are stripped first: a method named in prose is not a call, and
    // these files carry a lot of prose.
    const src = read(f)
      .replace(/\/\*[\s\S]*?\*\//g, ' ')
      .replace(/^\s*\/\/.*$/gm, ' ');
    for (const m of src.matchAll(/\bqhApi\.([A-Za-z0-9_]+)\s*\(/g)) {
      if (typeof api[m[1]] !== 'function') missing.push(`${f} -> qhApi.${m[1]}`);
    }
  }
  assert.deepEqual(missing, [],
    'these calls would throw at runtime:\n  ' + missing.join('\n  '));
});

test('reloadAll still builds its jobs as one array literal', () => {
  // The reason the test above matters. If this ever becomes a list of lazy
  // thunks, a missing method degrades to a rejected promise and the blast
  // radius shrinks to one section — at which point this test should be
  // rewritten, not deleted.
  const src = read('qh-admin-data.jsx');
  const i = src.indexOf('const jobs = [');
  assert.ok(i > 0, 'reloadAll no longer has a `const jobs = [` literal');
  const body = src.slice(i, src.indexOf('];', i));
  assert.ok(/qhApi\.\w+\(\)\.then\(/.test(body),
    'jobs entries are no longer started eagerly inside the literal');
});
