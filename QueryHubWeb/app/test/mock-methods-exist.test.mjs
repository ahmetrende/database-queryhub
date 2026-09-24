// Every `qhApi.x()` a screen calls has to exist on the prototype's mock too.
//
// `qh-api-mock.jsx` is the raw prototype's stand-in for `qh-api.jsx`: the
// prototype and the public README images run over it, and the Vite build never
// loads it. It is design-owned and travels whole with each round that changes
// it (SYNC.md §2, from 2026-09-24 (f)). Before that it did not travel at all,
// and the repo copy fell behind the screens by nine days. The first screen to
// call a method it lacked took the whole prototype down on load:
// `qhApi.adminAutoRequests is not a function`, from an admin data hook that
// builds its jobs eagerly (see api-methods-exist.test.mjs for why one missing
// method stops the entire panel, not one section).
//
// Same derivation of the callers as that test, from main.jsx's imports, so a
// screen added in a later round is checked without anyone listing it here.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { transformSync } from 'esbuild';

import { bareWindow, loadInto } from './_load.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const read = (f) => readFileSync(resolve(HERE, '..', 'src', f), 'utf8');

/** The mock, as the raw prototype gets it: after qh-data.jsx, like its tag. */
function mock() {
  const win = bareWindow();
  // The mock schedules its fake latency and approval timing with timers; none
  // of them may keep the test process alive, and none has to fire here.
  win.setTimeout = () => 0;
  win.clearTimeout = () => {};
  win.setInterval = () => 0;
  win.clearInterval = () => {};
  loadInto(win, 'qh-data.jsx');
  // Not under src/: the build must never import it, so there is no symlink.
  const src = readFileSync(resolve(HERE, '..', '..', 'qh-api-mock.jsx'), 'utf8');
  const code = transformSync(src, {
    loader: 'jsx', jsx: 'transform',
    jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment',
  }).code;
  new Function('window', 'with (window) {\n' + code + '\n}')(win);
  return win.qhApi;
}

function callers() {
  const main = read('main.jsx');
  const out = [];
  for (const m of main.matchAll(/^\s*import\s+'\.\/([^']+\.jsx)'/gm)) out.push(m[1]);
  return out;
}

test('the mock implements every client method a screen calls', () => {
  const api = mock();
  assert.ok(api && typeof api === 'object', 'qh-api-mock.jsx did not publish window.qhApi');
  const files = callers();
  assert.ok(files.length >= 10, 'main.jsx yielded only ' + files.length + ' callers');
  const missing = [];
  for (const f of files) {
    const src = read(f)
      .replace(/\/\*[\s\S]*?\*\//g, ' ')
      .replace(/^\s*\/\/.*$/gm, ' ');
    for (const m of src.matchAll(/\bqhApi\.([A-Za-z0-9_]+)\s*\(/g)) {
      if (typeof api[m[1]] !== 'function') missing.push(`${f} -> qhApi.${m[1]}`);
    }
  }
  assert.deepEqual([...new Set(missing)], [],
    'the raw prototype would throw on these calls:\n  ' + [...new Set(missing)].join('\n  '));
});
