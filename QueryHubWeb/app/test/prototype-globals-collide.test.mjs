// Two files, one top-level name — fine in the bundle, broken in the prototype.
//
// The build imports each source as an ES module, so a top-level `function X`
// is module-scoped and two files may each have their own. The raw prototype
// loads the same files as Babel <script> tags, where every top-level
// declaration is a GLOBAL: the last file loaded wins, and the earlier file's
// own call sites silently get the other one.
//
// That is how `AuditView` behaved. `qh-panels.jsx` had one for a single run's
// trail and `qh-admin-audit.jsx` one for the admin screen; in the prototype the
// developer view's Audit tab rendered the ADMIN component with no `st` prop and
// threw `Cannot read properties of undefined (reading 'pushToast')`. The build
// showed nothing, which is exactly why this is a test and not a habit.
//
// The mirror image of portal-globals.test.mjs: there the prototype was right
// and the build broken; here the build is right and the prototype broken. Both
// come from the same asymmetry, so both need a check nobody has to remember.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, '..', 'src');

// `qh-api.jsx` and `qh-api-mock.jsx` are ALTERNATIVES — the prototype loads one
// or the other, never both — so they are meant to define the same names.
const ALTERNATIVES = new Set(['qh-api.jsx', 'qh-api-mock.jsx']);

// Empty, and meant to stay that way. It briefly held `qhAgo`, which existed
// twice with DIFFERENT signatures; design removed both copies and put one in
// qh-data.jsx that takes either. An entry here is a known duplicate somebody
// decided to live with — not a place to park a new one.
const KNOWN = new Set();

function topLevelNames(src) {
  const out = new Set();
  for (const m of src.matchAll(/^(?:function|class|const)\s+([A-Za-z_$][\w$]*)/gm)) {
    out.add(m[1]);
  }
  return out;
}

test('no top-level name is declared by two files that load together', () => {
  const owners = new Map();
  const clashes = [];
  for (const f of readdirSync(SRC).filter(f => f.endsWith('.jsx')).sort()) {
    for (const name of topLevelNames(readFileSync(resolve(SRC, f), 'utf8'))) {
      const prev = owners.get(name);
      if (prev === undefined) { owners.set(name, f); continue; }
      if (ALTERNATIVES.has(prev) && ALTERNATIVES.has(f)) continue;
      if (KNOWN.has(name)) continue;
      clashes.push(`${name}: ${prev} and ${f}`);
    }
  }
  assert.deepEqual(clashes, [],
    'in the prototype the later file wins and the earlier one gets a stranger');
});

test('the run trail and the admin screen are not both AuditView', () => {
  const panels = readFileSync(resolve(SRC, 'qh-panels.jsx'), 'utf8');
  assert.match(panels, /function RunAuditView\b/,
    'a run\'s trail is RunAuditView; AuditView is the admin screen');
  assert.doesNotMatch(panels, /function AuditView\b/);
  assert.match(panels, /<RunAuditView\b/, 'and it is what the tab renders');
});
