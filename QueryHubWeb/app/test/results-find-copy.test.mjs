// Result grid: find, aggregates and copy formats (2026-11-11, Devart medium
// batch, PR M2).
//
//   * J1 The filter box has Match case, Whole word and Regular expression.
//        Each match in a cell gets a mark. Enter, Shift+Enter and the arrow
//        buttons go to the next or the previous match (qhResMatcher).
//   * J2 The sum bar shows Distinct, and Min and Max of text and dates
//        (qhResAggregate).
//   * J3 The grid menu copies the selection as CSV, JSON, XML or HTML
//        (qhResCopyAs).
//   * J4 The grid menu copies column names, with or without types
//        (qhResColumnNames).
// The grid does not depend on the engine, so every engine gets all four.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { bareWindow, loadInto } from './_load.mjs';
import { web } from './_mount.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }
const dom = JSDOM ? {} : { skip: 'jsdom not installed' };

const T = loadInto(bareWindow(), 'qh-results-tools.jsx');
const COLS = ['name', 'note'];
const ROWS = [
  { name: 'Apple', note: 'red apple pie' },
  { name: 'apple', note: 'pineapple' },
  { name: 'Éclair', note: 'éclair crème' },
  { name: 'pear', note: null },
];
const names = (rows) => rows.map(r => r.name);

// ---------- J1: the matcher and the filter ----------

test('the switches change what the filter keeps', () => {
  const f = (q, o) => names(T.qhResFilterRows(ROWS, COLS, q, o));
  assert.deepEqual(f('apple'), ['Apple', 'apple'], 'no switch: the old substring filter, any case');
  assert.deepEqual(f('Apple', { caseSensitive: true }), ['Apple']);
  assert.deepEqual(f('apple', { word: true }), ['Apple', 'apple'], 'pineapple is no whole word');
  assert.deepEqual(f('note:apple', { word: true }), ['Apple'], 'one column, whole word');
  assert.deepEqual(f('^ap', { regex: true }), ['Apple', 'apple']);
  assert.deepEqual(f('name:^p', { regex: true }), ['pear']);
  assert.deepEqual(f('éclair', { word: true }), ['Éclair'], 'a whole word that starts with a letter outside ASCII');
  assert.equal(T.qhResFilterRows(ROWS, COLS, '(', { regex: true }), ROWS, 'a broken regex keeps every row');
});

test('the matcher gives every range, and an error for a broken regex', () => {
  const m = T.qhResMatcher('a', {});
  assert.deepEqual(m.ranges('banana'), [[1, 2], [3, 4], [5, 6]]);
  assert.equal(m.test('xyz'), false);
  assert.deepEqual(T.qhResMatcher('a.c', {}).ranges('abc a.c'), [[4, 7]], 'no switch: a dot is a dot');
  assert.deepEqual(T.qhResMatcher('a.c', { regex: true }).ranges('abc'), [[0, 3]]);
  assert.deepEqual(T.qhResMatcher('x*', { regex: true }).ranges('ab'), [], 'an empty match marks nothing');
  assert.deepEqual(T.qhResMatcher('(', { regex: true }), { error: 'Not a valid regular expression.' });
  assert.equal(T.qhResMatcher('', {}), null);
  assert.equal(T.qhResMatcher('a', {}).ranges('a'.repeat(500)).length, 200, 'at most 200 marks in a cell');
});

test('the filter text splits into a column and the text', () => {
  assert.deepEqual(T.qhResFilterSplit('NAME: pie', COLS), { column: 'name', text: 'pie' });
  assert.deepEqual(T.qhResFilterSplit('x:1', COLS), { column: undefined, text: 'x:1' });
  assert.equal(T.qhResOptsOn({}), false);
  assert.equal(T.qhResOptsOn({ regex: true }), true);
});

// ---------- J2: aggregates ----------

test('Distinct, and Min and Max of text and dates', () => {
  const a = T.qhResAggregate(['b', 'a', 'b', '', null]);
  assert.equal(a.distinct, 2);
  assert.equal(a.tmin, 'a');
  assert.equal(a.tmax, 'b');
  const d = T.qhResAggregate(['2026-11-10', '2025-01-01 10:00', '2026-02-03']);
  assert.deepEqual([d.tmin, d.tmax], ['2025-01-01 10:00', '2026-11-10']);
  const n = T.qhResAggregate(['1', '1', 'x']);
  assert.deepEqual([n.distinct, n.tmin, n.tmax, n.sum], [2, null, null, 2], 'a number is there: no text min and max');
});

// ---------- J3 and J4: copy formats ----------

const CC = ['id', 'note'];
const CR = [[1, 'a, "b"'], [2, null], [3, '<x> & y']];

test('CSV quotes what needs quotes, and an empty cell is empty', () => {
  assert.equal(T.qhResCopyAs('csv', CC, CR), 'id,note\n1,"a, ""b"""\n2,\n3,<x> & y');
});

test('CSV quotes a comma, an edge space and a line break', () => {
  assert.equal(T.qhResCopyAs('csv', ['a'], [['x,y'], [' pad'], ['two\nlines']]), 'a\n"x,y"\n" pad"\n"two\nlines"');
});

test('JSON keeps numbers and nulls, and a repeated name gets _2', () => {
  assert.deepEqual(JSON.parse(T.qhResCopyAs('json', CC, CR)), [{ id: 1, note: 'a, "b"' }, { id: 2, note: null }, { id: 3, note: '<x> & y' }]);
  assert.deepEqual(JSON.parse(T.qhResCopyAs('json', ['id', 'id'], [[1, 2]])), [{ id: 1, id_2: 2 }]);
});

test('XML escapes, marks a null, and drops control characters', () => {
  const x = T.qhResCopyAs('xml', CC, CR);
  assert.match(x, /^<\?xml version="1\.0" encoding="UTF-8"\?>\n<rows>/);
  assert.match(x, /<field name="note">&lt;x&gt; &amp; y<\/field>/);
  assert.match(x, /<field name="note" null="true"\/>/);
  assert.match(T.qhResCopyAs('xml', ['a'], [['x\u0001y']]), /<field name="a">xy<\/field>/);
});

test('HTML is a table with escaped cells', () => {
  const h = T.qhResCopyAs('html', CC, CR);
  assert.match(h, /<thead>\n    <tr><th>id<\/th><th>note<\/th><\/tr>/);
  assert.match(h, /<tr><td>3<\/td><td>&lt;x&gt; &amp; y<\/td><\/tr>/);
  assert.match(h, /<tr><td>2<\/td><td><\/td><\/tr>/);
  assert.equal(T.qhResCopyAs('pdf', CC, CR), '', 'an unknown kind gives nothing');
});

test('column names, with and without types', () => {
  assert.equal(T.qhResColumnNames(['id', 'note'], { id: 'int4' }, false), 'id, note');
  assert.equal(T.qhResColumnNames(['id', 'note'], { id: 'int4' }, true), 'id int4,\nnote');
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
  const copied = [];
  win.qhCopyText = (t) => { copied.push(t); return Promise.resolve(true); };
  const toasts = [];
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(win.ResultsView, { result, status: 'done', onToast: (m) => toasts.push(m), colMeta: {}, unmasked: false })));
  const doc = win.document;
  const all = (sel) => [...doc.querySelectorAll(sel)];
  const flush = () => React.act(() => new Promise(r => setTimeout(r, 30)));
  const fire = (el, type, init) => React.act(() => { el.dispatchEvent(new win.MouseEvent(type, { bubbles: true, cancelable: true, ...init })); });
  const type = (el, text) => React.act(() => {
    Object.getOwnPropertyDescriptor(win.HTMLInputElement.prototype, 'value').set.call(el, text);
    el.dispatchEvent(new win.Event('input', { bubbles: true }));
  });
  const key = (el, k, mods = {}) => React.act(() => el.dispatchEvent(new win.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true, ...mods })));
  const cell = (r, c) => all('td[data-cell]').find(td => td.getAttribute('data-cell') === r + ':' + c);
  return { win, doc, all, flush, fire, type, key, cell, copied, toasts };
}
const table = (rows, cols, extra = {}) => ({ kind: 'table', cols, piiCols: [], rows, total: rows.length, truncated: false,
  statement: 1, statementCount: 1, slice: (o, c) => rows.slice(o, o + c), fetchPage: async (o, c) => rows.slice(o, o + c), ...extra });

test('a filter marks the matches; Enter and the arrows walk them', dom, async () => {
  const ui = mount(table(ROWS.map(r => ({ ...r })), COLS));
  const box = ui.doc.querySelector('.qh-res-filter');
  ui.type(box, 'apple');
  await ui.flush();
  assert.deepEqual(ui.all('mark.qh-res-hit').map(m => m.textContent), ['Apple', 'apple', 'apple', 'apple']);
  assert.match(ui.doc.querySelector('.qh-res-hitn').textContent, /^4 matches$/);
  ui.key(box, 'Enter');
  await ui.flush();
  assert.match(ui.doc.querySelector('.qh-res-hitn').textContent, /^1 of 4 matches$/);
  assert.ok(ui.cell(0, 0).className.includes('qh-td-sel'), 'the first match cell is selected');
  ui.key(box, 'Enter', { shiftKey: true });
  await ui.flush();
  assert.match(ui.doc.querySelector('.qh-res-hitn').textContent, /^4 of 4 matches$/, 'Shift+Enter from the first goes to the last');
  assert.ok(ui.cell(1, 1).className.includes('qh-td-sel'));
  ui.fire(ui.doc.querySelector('button[aria-label="Next match"]'), 'click');
  await ui.flush();
  assert.match(ui.doc.querySelector('.qh-res-hitn').textContent, /^1 of 4 matches$/, 'the arrow wraps round');
});

test('the switches: Whole word drops pineapple, a broken regex shows an error', dom, async () => {
  const ui = mount(table(ROWS.map(r => ({ ...r })), COLS));
  const box = ui.doc.querySelector('.qh-res-filter');
  ui.type(box, 'apple');
  const word = ui.doc.querySelector('button[aria-label="Whole word"]');
  ui.fire(word, 'click');
  await ui.flush();
  assert.equal(word.getAttribute('aria-pressed'), 'true');
  assert.deepEqual(ui.all('mark.qh-res-hit').map(m => m.textContent), ['Apple', 'apple', 'apple'], 'pineapple has no mark');
  ui.fire(ui.doc.querySelector('button[aria-label="Regular expression"]'), 'click');
  ui.type(box, '(');
  await ui.flush();
  assert.equal(ui.doc.querySelector('.qh-res-tools-note.is-error').textContent, 'Not a valid regular expression.');
  assert.equal(ui.all('mark.qh-res-hit').length, 0);
});

test('the sum bar shows Distinct and the text Min and Max', dom, async () => {
  const ui = mount(table([{ v: 'b' }, { v: 'a' }, { v: 'b' }], ['v']));
  ui.fire(ui.doc.querySelector('th:not(.qh-th-idx)'), 'mousedown');
  await ui.flush();
  const bar = ui.doc.querySelector('.qh-res-sum').textContent;
  assert.match(bar, /Distinct 2/);
  assert.match(bar, /Min a · Max b/);
});

test('the grid menu copies as CSV, JSON, XML, HTML and copies column names', dom, async () => {
  const ui = mount(table([{ id: 1, note: 'x' }, { id: 2, note: null }], ['id', 'note'], { colTypes: { id: 'int4', note: 'text' } }));
  const menu = () => {
    ui.fire(ui.cell(0, 0), 'contextmenu');
    return ui.doc.querySelector('.qh-ctxmenu');
  };
  // Select both rows first: the row number of row 1, then Ctrl on row 2.
  ui.fire(ui.all('td.qh-td-idx')[0], 'mousedown');
  ui.fire(ui.all('td.qh-td-idx')[1], 'mousedown', { ctrlKey: true });
  await ui.flush();
  for (const [label, check] of [['CSV', (t) => t === 'id,note\n1,x\n2,'],
                                ['JSON', (t) => JSON.parse(t)[1].note === null],
                                ['XML', (t) => t.includes('<field name="note" null="true"/>')],
                                ['HTML', (t) => t.includes('<th>id</th><th>note</th>')]]) {
    const btn = [...menu().querySelectorAll('button.qh-ctx-chip')].find(b => b.textContent === label);
    assert.ok(btn, label + ' button');
    ui.fire(btn, 'click');
    await ui.flush();
    assert.ok(check(ui.copied[ui.copied.length - 1]), label + ': ' + ui.copied[ui.copied.length - 1]);
    assert.match(ui.toasts[ui.toasts.length - 1], new RegExp('Copied 2 rows as ' + label + '\\.'));
  }
  const names = [...menu().querySelectorAll('button')].find(b => b.textContent === 'Copy column names');
  ui.fire(names, 'click');
  await ui.flush();
  assert.equal(ui.copied[ui.copied.length - 1], 'id, note');
  const typed = [...menu().querySelectorAll('button')].find(b => b.textContent === 'Copy column names and types');
  ui.fire(typed, 'click');
  await ui.flush();
  assert.equal(ui.copied[ui.copied.length - 1], 'id int4,\nnote text');
});

test('the CSS of the new parts', () => {
  const html = web('QueryHub.html');
  for (const sel of ['.qh-res-opt.is-on', '.qh-res-hit {', '.qh-ctx-row {', '.qh-ctxmenu .qh-ctx-row button.qh-ctx-chip']) {
    assert.ok(html.includes(sel), sel);
  }
});
