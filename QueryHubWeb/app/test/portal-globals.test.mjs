// The bundle's `window.ReactDOM` is a hand-written shim, and the prototype's is
// the full react-dom UMD from a CDN. So every ReactDOM.* the design writes works
// in the prototype by construction, and works in the built app only if someone
// remembered to add it to globals.js.
//
// The 2026-08-27 round made the editor's autocomplete portal itself to
// document.body. `createPortal` lives in `react-dom`, not `react-dom/client`,
// and the shim exported only `createRoot` — so the built app would have thrown
// "ReactDOM.createPortal is not a function" the first time a suggestion list
// opened, on a screen the prototype renders perfectly. Same asymmetry as a stale
// index.css: the raw prototype is fine and only the build is broken.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, '..', 'src');
const globals = readFileSync(resolve(SRC, 'globals.js'), 'utf8');

/** Every `ReactDOM.<member>` the design-owned sources reference. */
function membersUsed() {
  const used = new Map();
  for (const f of readdirSync(SRC).filter(f => f.endsWith('.jsx'))) {
    const src = readFileSync(resolve(SRC, f), 'utf8');
    for (const m of src.matchAll(/\bReactDOM\.([A-Za-z_$][\w$]*)/g)) {
      if (!used.has(m[1])) used.set(m[1], f);
    }
  }
  return used;
}

test('globals.js exports every ReactDOM member the components call', () => {
  const shim = globals.slice(globals.indexOf('window.ReactDOM'));
  for (const [member, file] of membersUsed()) {
    assert.ok(shim.includes(member),
      `${file} calls ReactDOM.${member}, which window.ReactDOM does not expose — ` +
      `the prototype has it from the CDN UMD, the bundle would throw`);
  }
});

test('createPortal is imported from react-dom, not react-dom/client', () => {
  // The client entry has createRoot and hydrateRoot only. Importing createPortal
  // from it is not a type error anywhere — it just arrives undefined.
  assert.match(globals, /import \{[^}]*createPortal[^}]*\} from 'react-dom'/);
});

// --- the JSX and the CSS have to agree on the coordinate system --------------

test('the autocomplete menu is positioned fixed, because its coords are viewport', () => {
  // qh-editor.jsx builds top/left from getBoundingClientRect — viewport
  // coordinates. With `position: absolute` those would be read against the
  // document instead, which is correct only while the page happens not to
  // scroll. The pair is silent when it disagrees, so it is pinned here.
  const editor = readFileSync(resolve(SRC, 'qh-editor.jsx'), 'utf8');
  const css = readFileSync(resolve(SRC, 'index.css'), 'utf8');
  assert.match(editor, /getBoundingClientRect\(\)/);
  assert.match(editor, /ReactDOM\.createPortal\(/);
  const rule = css.match(/\.qh-ac-ed \{[^}]*\}/);
  assert.ok(rule, '.qh-ac-ed rule missing from the generated CSS');
  assert.match(rule[0], /position: fixed/);
});

test('the menu sits above the panels and below anything modal', () => {
  const css = readFileSync(resolve(SRC, 'index.css'), 'utf8');
  const z = (sel) => {
    const m = css.match(new RegExp('\\' + sel + ' \\{[^}]*z-index: (\\d+)'));
    return m ? Number(m[1]) : null;
  };
  const ac = z('.qh-ac-ed');
  assert.ok(ac > z('.qh-ctxmenu'), 'must outrank the tab context menu');
  assert.ok(ac < z('.qh-modal-overlay'), 'a dialog opening over it must win');
  assert.ok(ac < z('.qh-toast'), 'toasts stay on top');
});
