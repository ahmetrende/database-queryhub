// A helper one file defines and another file calls has to be on `window`.
//
// These sources are plain scripts, not modules: they share nothing by import,
// only by `Object.assign(window, {...})` at the bottom of each file. The raw
// prototype hides that — Babel makes every top-level declaration a global, so a
// helper is reachable from everywhere whether or not anybody exported it. The
// Vite build gives each file its own module scope, where the same call is a
// ReferenceError that takes the screen to the error boundary.
//
// `qhEndpointHover` shipped that way on 2026-09-10 and the admin Connections
// screen threw `qhEndpointHover is not defined` for five days: defined in
// qh-data.jsx, called from qh-admin-access.jsx, never exported. The prototype
// was fine the whole time.
//
// Same asymmetry as portal-globals.test.mjs (a ReactDOM member the shim lacks),
// opposite direction from prototype-globals-collide.test.mjs (two files, one
// name). Three tests, one fault line: what the prototype shares by accident,
// the build shares only on purpose.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, '..', 'src');
const FILES = readdirSync(SRC).filter(f => f.endsWith('.jsx')).sort();

/** Comments out, so a name mentioned in prose is not read as a reference. */
function code(src) {
  return src.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/^.*?\/\/.*$/gm, m =>
    // Keep the part before `//`; a URL inside a string is not a comment start,
    // but no identifier scan cares about what follows one either way.
    m.slice(0, m.indexOf('//')));
}

function declaredIn(src) {
  const out = new Set();
  for (const m of code(src).matchAll(
      /^(?:async\s+)?(?:function|const|class|let|var)\s+([A-Za-z_$][\w$]*)/gm)) {
    out.add(m[1]);
  }
  return out;
}

function exportedBy(src) {
  const out = new Set();
  for (const block of code(src).matchAll(/Object\.assign\(window,\s*\{([\s\S]*?)\}\s*\)/g)) {
    for (const m of (block[1] + '}').matchAll(/([A-Za-z_$][\w$]*)\s*[,:}]/g)) out.add(m[1]);
  }
  return out;
}

// The project's own naming convention is what makes this checkable: shared
// helpers are `qhSomething`, shared constants `QH_SOMETHING`. Not preceded by a
// dot (a property), not followed by a colon (an object key), not `typeof`
// guarded (a deliberate "might not exist" probe).
const REF = /(?<![.\w$])(qh[A-Z][A-Za-z0-9_$]*|QH_[A-Z][A-Z0-9_]*)\b(?!\s*:)/g;

test('every shared qh* name a file uses is on window or its own', () => {
  const declared = new Map(), onWindow = new Set();
  for (const f of FILES) {
    const src = readFileSync(resolve(SRC, f), 'utf8');
    declared.set(f, declaredIn(src));
    for (const n of exportedBy(src)) onWindow.add(n);
  }
  const missing = [];
  for (const f of FILES) {
    const src = code(readFileSync(resolve(SRC, f), 'utf8'))
      .replace(/typeof\s+(qh[A-Z]\w*|QH_[A-Z0-9_]+)/g, '');
    for (const m of new Set([...src.matchAll(REF)].map(x => x[1]))) {
      if (declared.get(f).has(m) || onWindow.has(m)) continue;
      missing.push(`${f} calls ${m}, and no file puts it on window`);
    }
  }
  assert.deepEqual(missing, []);
});

test('the endpoint hover helper is exported', () => {
  // The specific regression, stated so a failure names it rather than the rule.
  const data = readFileSync(resolve(SRC, 'qh-data.jsx'), 'utf8');
  assert.match(data, /function qhEndpointHover\b/);
  assert.ok(exportedBy(data).has('qhEndpointHover'),
    'qh-admin-access.jsx renders the connections table with it');
});
