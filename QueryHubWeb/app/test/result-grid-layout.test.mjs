// Result grid layout (ported from the IdP app).
//
//   * Row 9: the row-number column is sticky on horizontal scroll. Column
//     widths come from the first page and stay fixed. A numeric column aligns
//     right by its reported type.
//   * Row 13: the column edge fits on double-click, steps by 16 px with the
//     arrow keys, and Escape restores the width it had when it got focus.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { createRequire } from 'node:module';

import { bareWindow, loadInto } from './_load.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }
const dom = JSDOM ? {} : { skip: 'jsdom not installed' };

const HERE = dirname(fileURLToPath(import.meta.url));

// ---------- the pure helpers ----------

const H = loadInto(bareWindow(), 'qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-results-tools.jsx', 'qh-panels.jsx');

test('column kind: engine type names map to number or other', () => {
  const numbers = ['int', 'integer', 'bigint', 'smallint', 'numeric', 'numeric(10,2)', 'decimal', 'real',
    'double precision', 'float', 'float8', 'money', 'int2', 'int4', 'int8', 'tinyint', 'Nullable(Int32)',
    'UInt64', 'INTEGER'];
  for (const t of numbers) assert.equal(H.qhColumnKind(t), 'number', t);
  const others = ['text', 'varchar(20)', 'timestamp', 'interval', 'int4range', 'bool', 'uuid', 'jsonb',
    '_int4', 'int4[]', 'Array(Int32)', '', null, undefined];
  for (const t of others) assert.equal(H.qhColumnKind(t), 'other', String(t));
});

test('cell alignment: type first, then the JS value when the type is unknown', () => {
  assert.equal(H.qhIsNumCell('int4', '42'), true, 'a numeric string in a numeric column');
  assert.equal(H.qhIsNumCell('text', 'abc'), false);
  assert.equal(H.qhIsNumCell(undefined, 5), true, 'unknown type: value check');
  assert.equal(H.qhIsNumCell(undefined, '5'), false);
});

test('fit width has a minimum and a maximum', () => {
  assert.equal(H.qhFitColWidth('a', ['x']), Math.max(56, Math.round(4 * 7.6) + 28));
  assert.equal(H.qhFitColWidth('a', ['x'.repeat(5000)]), 600);
  assert.ok(H.qhFitColWidth('a', ['x'.repeat(40)]) > H.qhFitColWidth('a', ['x'.repeat(10)]));
});

test('grip keys: arrows step 16, Escape restores, other keys are not ours', () => {
  assert.equal(H.qhGripKeyWidth('ArrowRight', 100, 100), 116);
  assert.equal(H.qhGripKeyWidth('ArrowLeft', 100, 100), 84);
  assert.equal(H.qhGripKeyWidth('ArrowLeft', 50, 50), 48, 'never below the minimum');
  assert.equal(H.qhGripKeyWidth('Escape', 140, 100), 100);
  assert.equal(H.qhGripKeyWidth('Escape', 140, null), null);
  assert.equal(H.qhGripKeyWidth('a', 100, 100), null);
});

// ---------- the sticky row-number CSS ----------

test('the row-number column is sticky with an opaque background', () => {
  const html = readFileSync(resolve(HERE, '..', '..', 'QueryHub.html'), 'utf8');
  const td = html.match(/\.qh-table-grid td\.qh-td-idx\s*\{([^}]*)\}/);
  assert.ok(td, 'a sticky rule for the row-number cell');
  assert.match(td[1], /position:\s*sticky/);
  assert.match(td[1], /left:\s*0/);
  assert.match(td[1], /background:\s*var\(--bg-white\)/);
  const th = html.match(/\.qh-table-grid th\.qh-th-idx\s*\{([^}]*)\}/);
  assert.ok(th, 'a sticky rule for the # header');
  assert.match(th[1], /left:\s*0/);
  // A selected header is tinted. The tint is translucent, so it needs an
  // opaque base, or the cells that scroll under the sticky header show through.
  const sel = html.match(/\.qh-table-grid th\.is-sel\s*\{([^}]*)\}/);
  assert.ok(sel, 'a rule for a selected header');
  assert.match(sel[1], /background:[^;]*var\(--bg-medium\)/);
});

// ---------- the real grid ----------

function mount(result) {
  const d = new JSDOM('<!doctype html><div id="root"></div>', { pretendToBeVisual: true, url: 'https://localhost/' });
  for (const k of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node', 'Event',
                   'KeyboardEvent', 'MouseEvent', 'getComputedStyle', 'requestAnimationFrame',
                   'cancelAnimationFrame', 'localStorage', 'location', 'history']) {
    Object.defineProperty(globalThis, k, { value: d.window[k], configurable: true, writable: true });
  }
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const React = require_('react');
  const ReactDOM = require_('react-dom/client');
  globalThis.React = React;
  const win = d.window;
  win.React = React;
  win.ReactDOM = require_('react-dom');
  loadInto(win, 'qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-results-tools.jsx', 'qh-panels.jsx');
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(win.ResultsView, { result, status: 'done', onToast: () => {}, colMeta: {}, unmasked: false })));
  const doc = win.document;
  const flush = () => React.act(() => new Promise(r => setTimeout(r, 30)));
  const fire = (el, type, init) => React.act(() => { el.dispatchEvent(new win.MouseEvent(type, { bubbles: true, cancelable: true, ...init })); });
  const key = (el, k) => React.act(() => el.dispatchEvent(new win.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true })));
  const widths = () => [...doc.querySelectorAll('colgroup col')].slice(1).map(c => c.style.width);
  const grip = (i) => doc.querySelectorAll('.qh-col-grip')[i];
  const nextPage = async () => { fire(doc.querySelector('button[aria-label="Next page"]'), 'click'); await flush(); };
  return { React, win, doc, flush, fire, key, widths, grip, nextPage };
}

const table = (rows, cols, extra = {}) => ({ kind: 'table', cols, piiCols: [], rows, total: rows.length, truncated: false,
  statement: 1, statementCount: 1, slice: (o, c) => rows.slice(o, o + c), fetchPage: async (o, c) => rows.slice(o, o + c), ...extra });

// 150 rows: the first page is short, the second page has very long names.
const longRows = () => Array.from({ length: 150 }, (_, i) => ({ id: String(i + 1), name: i < 100 ? 'ab' : 'x'.repeat(200) }));

test('a numeric string in a numeric column aligns right', dom, async () => {
  const ui = mount(table([{ id: '42', name: 'abc' }], ['id', 'name'], { colTypes: { id: 'int4', name: 'text' } }));
  await ui.flush();
  const cells = [...ui.doc.querySelectorAll('td[data-cell]')];
  assert.ok(cells.find(td => td.getAttribute('data-cell') === '0:0').className.includes('qh-td-num'));
  assert.ok(!cells.find(td => td.getAttribute('data-cell') === '0:1').className.includes('qh-td-num'));
});

test('widths stay the same when the page changes', dom, async () => {
  const ui = mount(table(longRows(), ['id', 'name'], { colTypes: { id: 'int4', name: 'text' } }));
  await ui.flush();
  const before = ui.widths();
  assert.equal(before.length, 2);
  await ui.nextPage();
  assert.match(ui.doc.querySelector('.qh-pager-page').textContent, /Page 2/);
  assert.deepEqual(ui.widths(), before, 'no re-measure on page 2');
});

test('a dragged width is kept across a page change', dom, async () => {
  const ui = mount(table(longRows(), ['id', 'name']));
  await ui.flush();
  ui.fire(ui.grip(1), 'mousedown', { clientX: 100, button: 0 });
  ui.fire(ui.win, 'mousemove', { clientX: 400 });
  ui.fire(ui.win, 'mouseup', {});
  assert.equal(ui.widths()[1], '300px');
  await ui.nextPage();
  assert.equal(ui.widths()[1], '300px');
});

test('the edge is a focusable separator', dom, async () => {
  const ui = mount(table(longRows(), ['id', 'name']));
  const g = ui.grip(1);
  assert.equal(g.getAttribute('role'), 'separator');
  assert.equal(g.getAttribute('aria-orientation'), 'vertical');
  assert.equal(g.getAttribute('aria-label'), 'Resize column name');
  assert.equal(g.getAttribute('tabindex'), '0');
});

test('double-click on the edge fits the column to the loaded page', dom, async () => {
  const ui = mount(table(longRows(), ['id', 'name']));
  await ui.flush();
  ui.fire(ui.grip(1), 'dblclick', {});
  // The loaded page holds only 'ab' values, so the fit equals the helper result.
  assert.equal(ui.widths()[1], ui.win.qhFitColWidth('name', Array(100).fill('ab')) + 'px');
});

test('ArrowRight adds 16 px and Escape restores the width at focus', dom, async () => {
  const ui = mount(table(longRows(), ['id', 'name']));
  await ui.flush();
  const start = parseInt(ui.widths()[0], 10);
  const g = ui.grip(0);
  ui.React.act(() => g.focus());
  ui.key(g, 'ArrowRight');
  assert.equal(ui.widths()[0], (start + 16) + 'px');
  ui.key(g, 'ArrowRight');
  assert.equal(ui.widths()[0], (start + 32) + 'px');
  ui.key(g, 'ArrowLeft');
  assert.equal(ui.widths()[0], (start + 16) + 'px');
  ui.key(g, 'Escape');
  assert.equal(ui.widths()[0], start + 'px');
});
