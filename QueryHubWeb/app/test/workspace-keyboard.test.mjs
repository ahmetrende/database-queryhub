// Workspace keyboard access (2026-11-19).
//
//   * The sidebar separator and the results grip are WAI-ARIA separators.
//     They take focus, and the arrow keys and Home move them.
//   * The results height survives a reload (qh.resheight.v1).
//   * The connection tree follows the WAI-ARIA tree pattern with a roving
//     tabindex: arrows, Home, End, Enter and Space.
//   * A keyboard context menu (Shift+F10, the Menu key) opens at the row.
//   * The separators, tree rows and editor tabs show a focus ring.
//
// The helpers are pure. The test reads each one from the source file and
// runs it alone, so it needs no browser and no other module.
import test from 'node:test';
import assert from 'node:assert/strict';

import { web } from './_mount.mjs';

/** The source of one top-level `function NAME(...) { ... }` in a file. */
function fnSource(src, name) {
  const start = src.indexOf('function ' + name + '(');
  assert.ok(start >= 0, name + ' is defined');
  const open = src.indexOf('{', src.indexOf(')', start));
  let depth = 0;
  let i = open;
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) break; }
  }
  return src.slice(start, i + 1);
}

/** Runs the named functions from one file and returns them. */
function pick(file, names, prelude = '') {
  const src = web(file);
  const body = prelude + '\n' + names.map(n => fnSource(src, n)).join('\n') + '\nreturn { ' + names.join(', ') + ' };';
  return new Function(body)();
}

const PANELS = web('qh-panels.jsx');
const APP = web('qh-app.jsx');
const HTML = web('QueryHub.html');

const defMatch = APP.match(/const QH_RES_H_DEFAULT = (\d+);/);
const P = pick('qh-panels.jsx', ['qhTreeKeyTarget', 'qhTreeRovingFix', 'qhMenuPoint']);
const A = pick('qh-app.jsx', ['qhLoadResHeight', 'qhResHeightKey', 'qhSideWidthKey'], defMatch ? defMatch[0] : '');

// ---------- the tree keys ----------

// Rows: 0 server (open) > 1 database (open) > 2 table, 3 database (closed), 4 server (closed).
const LEVELS = [1, 2, 3, 2, 1];
const FLAGS = [true, true, undefined, false, false];
const key = (i, k) => P.qhTreeKeyTarget(LEVELS, i, k, FLAGS);

test('ArrowDown and ArrowUp move one row and stop at the ends', () => {
  assert.deepEqual(key(0, 'ArrowDown'), { focus: 1 });
  assert.deepEqual(key(4, 'ArrowDown'), { focus: 4 });
  assert.deepEqual(key(2, 'ArrowUp'), { focus: 1 });
  assert.deepEqual(key(0, 'ArrowUp'), { focus: 0 });
});

test('Home and End go to the first and the last row', () => {
  assert.deepEqual(key(3, 'Home'), { focus: 0 });
  assert.deepEqual(key(1, 'End'), { focus: 4 });
});

test('ArrowRight opens a closed row, then goes to its first child', () => {
  assert.deepEqual(key(3, 'ArrowRight'), { toggle: true });
  assert.deepEqual(key(0, 'ArrowRight'), { focus: 1 });
  assert.deepEqual(key(2, 'ArrowRight'), { focus: 2 }, 'a leaf stays');
  assert.deepEqual(P.qhTreeKeyTarget([1, 1], 0, 'ArrowRight', [true, false]), { focus: 0 }, 'an open row with no child stays');
});

test('ArrowLeft closes an open row, else goes to the parent', () => {
  assert.deepEqual(key(1, 'ArrowLeft'), { toggle: true });
  assert.deepEqual(key(0, 'ArrowLeft'), { toggle: true });
  assert.deepEqual(key(2, 'ArrowLeft'), { focus: 1 });
  assert.deepEqual(key(3, 'ArrowLeft'), { focus: 0 });
  assert.deepEqual(key(4, 'ArrowLeft'), { focus: 4 }, 'a closed top row stays');
});

test('other keys and an empty tree are not handled', () => {
  assert.equal(key(0, 'a'), null);
  assert.equal(key(0, 'Enter'), null, 'Enter is the row click, not a move');
  assert.equal(P.qhTreeKeyTarget([], 0, 'ArrowDown', []), null);
  assert.equal(P.qhTreeKeyTarget(LEVELS, 9, 'ArrowDown', FLAGS), null);
});

// ---------- roving tabindex ----------

const fakeRow = (active) => ({ tabIndex: active ? 0 : -1, classList: { contains: (c) => c === 'is-active' && !!active } });
const fakeTree = (rows, focused = null) => ({ querySelectorAll: () => rows, ownerDocument: { activeElement: focused } });
const tabs = (rows) => rows.map(r => r.tabIndex);

test('with no active row, the first row is the one Tab stop', () => {
  const rows = [fakeRow(), fakeRow(), fakeRow()];
  P.qhTreeRovingFix(fakeTree(rows));
  assert.deepEqual(tabs(rows), [0, -1, -1]);
});

test('the active row is the Tab stop, and only one row is', () => {
  const rows = [fakeRow(), fakeRow(true), fakeRow(true)];
  P.qhTreeRovingFix(fakeTree(rows));
  assert.deepEqual(tabs(rows), [-1, 0, -1]);
});

test('the focused row wins over the active row', () => {
  const rows = [fakeRow(true), fakeRow(), fakeRow()];
  P.qhTreeRovingFix(fakeTree(rows, rows[2]));
  assert.deepEqual(tabs(rows), [-1, -1, 0]);
});

// ---------- keyboard context menu ----------

test('a menu from the keyboard opens at the row, a mouse menu at the pointer', () => {
  const row = { getBoundingClientRect: () => ({ left: 10.4, top: 30, bottom: 52.6, right: 200 }) };
  assert.deepEqual(P.qhMenuPoint({ clientX: 0, clientY: 0, currentTarget: row }), { x: 10, y: 53 });
  assert.deepEqual(P.qhMenuPoint({ clientX: 120, clientY: 340, currentTarget: row }), { x: 120, y: 340 });
  assert.deepEqual(P.qhMenuPoint({ clientX: 0, clientY: 0, currentTarget: null }), { x: 0, y: 0 });
});

test('both tree menu openers use the keyboard fallback', () => {
  assert.match(PANELS, /const openMenu = [^\n]*qhMenuPoint\(e\)/);
  assert.match(PANELS, /const openConnMenu = [^\n]*qhMenuPoint\(e\)/);
});

// ---------- results height ----------

test('the stored results height is a whole number in 80..1600, else 280', () => {
  assert.equal(defMatch && defMatch[1], '280');
  for (const raw of [null, undefined, '', 'abc', '79', '1601', '12.5', '-100', '3e2']) {
    assert.equal(A.qhLoadResHeight(raw, 1000), 280, String(raw));
  }
  assert.equal(A.qhLoadResHeight('500', 1000), 500);
  assert.equal(A.qhLoadResHeight(' 300 ', 1000), 300);
  assert.equal(A.qhLoadResHeight('80', 1000), 80);
});

test('the stored height is capped to the window, never below 160', () => {
  assert.equal(A.qhLoadResHeight('900', 1000), 680);
  assert.equal(A.qhLoadResHeight('400', 300), 160);
  assert.equal(A.qhLoadResHeight('120', 300), 120);
});

test('the grip keys: up 24 taller, down 24 shorter, Home 280, clamped', () => {
  assert.equal(A.qhResHeightKey(280, 'ArrowUp', 600), 304);
  assert.equal(A.qhResHeightKey(280, 'ArrowDown', 600), 256);
  assert.equal(A.qhResHeightKey(500, 'Home', 600), 280);
  assert.equal(A.qhResHeightKey(590, 'ArrowUp', 600), 600);
  assert.equal(A.qhResHeightKey(90, 'ArrowDown', 600), 80);
  assert.equal(A.qhResHeightKey(280, 'x', 600), null);
});

test('the results height is stored and is not cleared at sign-out', () => {
  assert.match(APP, /localStorage\.getItem\('qh\.resheight\.v1'\)/);
  assert.match(APP, /localStorage\.setItem\('qh\.resheight\.v1'/);
  const code = APP.split('\n').filter(l => l.includes('qh.resheight.v1') && !l.trim().startsWith('//'));
  for (const l of code) assert.doesNotMatch(l, /removeItem/);
});

// ---------- sidebar width ----------

test('the sidebar keys move the separator in the arrow direction', () => {
  assert.equal(A.qhSideWidthKey(300, 'ArrowRight', 'left'), 316);
  assert.equal(A.qhSideWidthKey(300, 'ArrowLeft', 'left'), 284);
  assert.equal(A.qhSideWidthKey(300, 'ArrowLeft', 'right'), 316, 'a right sidebar grows to the left');
  assert.equal(A.qhSideWidthKey(300, 'ArrowRight', 'right'), 284);
  assert.equal(A.qhSideWidthKey(400, 'Home', 'left'), 264);
  assert.equal(A.qhSideWidthKey(556, 'ArrowRight', 'left'), 560);
  assert.equal(A.qhSideWidthKey(230, 'ArrowLeft', 'left'), 224);
  assert.equal(A.qhSideWidthKey(300, 'Enter', 'left'), null);
});

// ---------- markup ----------

test('the sidebar separator is a focusable vertical separator', () => {
  const m = PANELS.match(/<div className="qh-side-resizer"[\s\S]*?\/>/);
  assert.ok(m, 'the resizer is rendered');
  for (const s of ['role="separator"', 'aria-orientation="vertical"', 'aria-label="Resize the sidebar"',
    'aria-valuemin={224}', 'aria-valuemax={560}', 'aria-valuenow=', 'tabIndex={0}', 'onKeyDown={onResizerKey}', 'onDoubleClick={onResizerFit}']) {
    assert.ok(m[0].includes(s), s);
  }
  assert.match(APP, /onResizerKey=\{onResizerKey\}/);
});

test('the results grip is a focusable horizontal separator', () => {
  const m = APP.match(/<div className="qh-res-grip"[\s\S]*?<span \/><\/div>/);
  assert.ok(m, 'the grip is rendered');
  for (const s of ['role="separator"', 'aria-orientation="horizontal"', 'aria-label="Resize the results panel"',
    'aria-valuemin={80}', 'aria-valuemax=', 'aria-valuenow={resH}', 'tabIndex={0}', 'onKeyDown={onGripKey}', 'onMouseDown={onDragStart}', 'onDoubleClick=']) {
    assert.ok(m[0].includes(s), s);
  }
});

test('the tree and its rows carry the tree roles', () => {
  assert.match(PANELS, /role="tree" aria-label="Connections"/);
  for (const s of ['role="treeitem"', 'aria-level={(depth || 0) + 1}', 'aria-expanded={expandable ? !!open : undefined}',
    'aria-selected={!!active}', 'tabIndex={active ? 0 : -1}', 'onKeyDown={onKeyDown}']) {
    assert.ok(PANELS.includes(s), s);
  }
  assert.match(PANELS, /Object\.assign\(window, \{[^}]*qhTreeKeyTarget[^}]*\}\)/);
  assert.match(PANELS, /Object\.assign\(window, \{[^}]*qhMenuPoint[^}]*\}\)/);
});

// ---------- focus rings ----------

test('separators, tree rows and tabs show the focus ring', () => {
  const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  for (const sel of ['.qh-side-resizer', '.qh-res-grip', '.qh-tr', '.qh-tab']) {
    const re = new RegExp(esc(sel + ':focus-visible') + '\\s*\\{[^}]*outline: none;[^}]*box-shadow: 0 0 0 2px var\\(--brand-adaptive-medium\\)');
    assert.match(HTML, re, sel);
  }
  assert.match(HTML, /\.qh-side-resizer:focus-visible::after \{ background: var\(--brand-green\); \}/);
  assert.match(HTML, /\.qh-res-grip:focus-visible span \{ background: var\(--brand-green\); \}/);
});
