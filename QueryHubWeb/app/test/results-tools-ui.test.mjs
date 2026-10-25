// Results grid tools of 2026-10-25: sort, filter, sum bar and JSON view.
//
// The logic lives in qh-results-tools.jsx. These tests mount the real
// ResultsView from qh-panels.jsx and drive it through the DOM.
//
//   a. the sort button reorders the rows and keeps the dragged column width
//   b. the filter keeps matching rows, says "N of M rows", and clears
//   c. a result over 5,000 rows has no sort buttons and a disabled filter
//   d. two selected numeric cells show the sum bar, one cell shows none
//   e. a JSON cell peeks as pretty text, Raw shows the original
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { loadInto } from './_load.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }
const opts = JSDOM ? {} : { skip: 'jsdom not installed' };

const QH_RES_DOC = '{"a":1,"b":[2,3]}';
const qhResRows = () => [
  { name: 'carol', amount: 30, doc: QH_RES_DOC },
  { name: 'alice', amount: 10, doc: 'plain' },
  { name: 'bob', amount: 20, doc: '[1,2]' },
  { name: 'alan', amount: 5, doc: 'x' },
];
const qhResTable = (total, rowsIn) => {
  const rows = rowsIn || qhResRows();
  return { kind: 'table', cols: ['name', 'amount', 'doc'], piiCols: [], rows, total: total == null ? rows.length : total,
    truncated: false, statement: 1, statementCount: 1,
    slice: (o, c) => rows.slice(o, o + c), fetchPage: async (o, c) => rows.slice(o, o + c) };
};

function qhResMount(result) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>',
                        { pretendToBeVisual: true, url: 'https://localhost/' });
  for (const k of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node', 'Event',
                   'KeyboardEvent', 'MouseEvent', 'getComputedStyle', 'requestAnimationFrame',
                   'cancelAnimationFrame', 'localStorage', 'location', 'history']) {
    Object.defineProperty(globalThis, k, { value: dom.window[k], configurable: true, writable: true });
  }
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const React = require_('react');
  const ReactDOM = require_('react-dom/client');
  globalThis.React = React;
  const win = dom.window;
  win.React = React;
  win.ReactDOM = require_('react-dom');
  loadInto(win, 'qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-results-tools.jsx', 'qh-panels.jsx');
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  const render = (res) => React.act(() => root.render(React.createElement(win.ResultsView, { result: res, status: 'done', onToast: () => {}, colMeta: {}, unmasked: false })));
  render(result);
  const doc = win.document;
  const all = (sel) => [...doc.querySelectorAll(sel)];
  const flush = () => React.act(() => new Promise(r => setTimeout(r, 30)));
  const fire = (el, type, init) => React.act(() => { el.dispatchEvent(new win.MouseEvent(type, { bubbles: true, cancelable: true, ...init })); });
  const click = (el) => fire(el, 'click');
  const type = (el, text) => React.act(() => {
    Object.getOwnPropertyDescriptor(win.HTMLInputElement.prototype, 'value').set.call(el, text);
    el.dispatchEvent(new win.Event('input', { bubbles: true }));
  });
  // Filter in JS: the jsdom selector engine may not handle an attribute-ends-with
  // test that holds a colon.
  const names = () => all('td[data-cell]').filter(td => td.getAttribute('data-cell').endsWith(':0')).map(td => td.textContent);
  const cell = (r, c) => all('td[data-cell]').find(td => td.getAttribute('data-cell') === r + ':' + c);
  const widths = () => all('colgroup col').map(c => c.style.width);
  return { win, doc, all, flush, fire, click, type, names, cell, widths, render };
}

test('sort cycles asc, desc, original and keeps the dragged column width', opts, async () => {
  const ui = qhResMount(qhResTable());
  assert.deepEqual(ui.names(), ['carol', 'alice', 'bob', 'alan']);
  // Drag the first column grip: mousedown on the grip, then a window mousemove.
  const grip = ui.all('.qh-col-grip')[0];
  ui.fire(grip, 'mousedown', { clientX: 100 });
  ui.fire(ui.win, 'mousemove', { clientX: 300 });
  ui.fire(ui.win, 'mouseup', {});
  assert.equal(ui.widths()[1], '200px');
  const sortBtn = () => ui.doc.querySelector('button[aria-label="Sort by name"]');
  assert.equal(sortBtn().title, 'Sort ascending');
  ui.click(sortBtn()); await ui.flush();
  assert.deepEqual(ui.names(), ['alan', 'alice', 'bob', 'carol']);
  assert.equal(ui.all('th')[1].getAttribute('aria-sort'), 'ascending');
  assert.equal(ui.widths()[1], '200px');
  assert.equal(sortBtn().title, 'Sort descending');
  ui.click(sortBtn()); await ui.flush();
  assert.deepEqual(ui.names(), ['carol', 'bob', 'alice', 'alan']);
  assert.equal(ui.all('th')[1].getAttribute('aria-sort'), 'descending');
  assert.equal(sortBtn().title, 'Clear sort');
  ui.click(sortBtn()); await ui.flush();
  assert.deepEqual(ui.names(), ['carol', 'alice', 'bob', 'alan']);
  assert.equal(ui.all('th')[1].getAttribute('aria-sort'), null);
  assert.equal(ui.widths()[1], '200px');
});

test('the filter keeps matching rows, shows N of M rows and clears', opts, async () => {
  const ui = qhResMount(qhResTable());
  const input = ui.doc.querySelector('input[aria-label="Filter rows"]');
  assert.equal(input.placeholder, 'Filter rows, or column:value');
  assert.equal(ui.doc.querySelector('.qh-res-filter-clear'), null);
  ui.type(input, 'al'); await ui.flush();
  assert.deepEqual(ui.names(), ['alice', 'alan']);
  assert.match(ui.doc.querySelector('.qh-res-tools').textContent, /2 of 4 rows/);
  ui.click(ui.doc.querySelector('.qh-res-filter-clear')); await ui.flush();
  assert.deepEqual(ui.names(), ['carol', 'alice', 'bob', 'alan']);
  assert.equal(input.value, '');
  assert.ok(!/of 4 rows/.test(ui.doc.querySelector('.qh-res-tools').textContent));
});

test('a result over 5,000 rows has no sort buttons and a disabled filter', opts, async () => {
  const ui = qhResMount(qhResTable(5001));
  assert.equal(ui.all('.qh-th-sort').length, 0);
  const input = ui.doc.querySelector('input[aria-label="Filter rows"]');
  assert.equal(input.disabled, true);
  assert.ok(input.title && input.title.length > 10, 'the disabled filter needs the reason as its title');
});

test('two numeric cells show the sum bar and one cell shows none', opts, async () => {
  const ui = qhResMount(qhResTable());
  ui.fire(ui.cell(0, 1), 'mousedown', {});
  assert.equal(ui.doc.querySelector('.qh-res-sum'), null);
  ui.fire(ui.cell(1, 1), 'mousedown', { shiftKey: true });
  ui.fire(ui.win, 'mouseup', {});
  const bar = ui.doc.querySelector('.qh-res-sum');
  assert.ok(bar, 'the sum bar is missing');
  assert.match(bar.textContent, /2 cells/);
  assert.match(bar.textContent, /Sum 40 /);
  assert.match(bar.textContent, /Avg 20 /);
  assert.match(bar.textContent, /Min 10 /);
  assert.match(bar.textContent, /Max 30/);
});

test('a JSON cell peeks as pretty text and Raw shows the original', opts, async () => {
  const ui = qhResMount(qhResTable());
  const peekCell = async (r, c) => {
    const td = ui.cell(r, c);
    assert.ok(td, 'no cell ' + r + ':' + c + ' in ' + ui.all('td').length + ' cells');
    ui.fire(td, 'contextmenu', { clientX: 10, clientY: 10 });
    const item = ui.all('button').find(b => b.textContent.indexOf('Show full value') === 0);
    assert.ok(item, 'the context menu did not open');
    ui.click(item);
  };
  await peekCell(0, 2);
  const area = () => ui.doc.querySelector('.qh-peek textarea');
  const toggle = () => ui.doc.querySelector('.qh-res-pretty');
  assert.equal(toggle().textContent, 'Pretty');
  assert.equal(toggle().getAttribute('aria-pressed'), 'true');
  assert.ok(area().value.includes('\n'), 'pretty text has line breaks');
  assert.ok(area().value.includes('"a": 1'));
  assert.match(ui.doc.querySelector('.qh-peek-head').textContent, /17 chars/);
  ui.click(toggle());
  assert.equal(toggle().textContent, 'Raw');
  assert.equal(toggle().getAttribute('aria-pressed'), 'false');
  assert.equal(area().value, QH_RES_DOC);
  assert.match(ui.doc.querySelector('.qh-peek-head').textContent, /17 chars/);
  // Another value that is not JSON has no toggle.
  ui.click(ui.all('.qh-peek-head button').find(b => b.textContent.trim() === 'Close'));
  await peekCell(1, 2);
  assert.equal(ui.doc.querySelector('.qh-res-pretty'), null);
  assert.equal(area().value, 'plain');
});

// ---------------------------------------------------------------------------
// Edge cases added after the mutation check
// ---------------------------------------------------------------------------

const qhResSelect = (ui, a, b) => {
  ui.fire(ui.cell(a[0], a[1]), 'mousedown', {});
  ui.fire(ui.cell(b[0], b[1]), 'mousedown', { shiftKey: true });
  ui.fire(ui.win, 'mouseup', {});
};

test('a new result resets the dragged column width, a sort does not', opts, async () => {
  const ui = qhResMount(qhResTable());
  ui.fire(ui.all('.qh-col-grip')[0], 'mousedown', { clientX: 100 });
  ui.fire(ui.win, 'mousemove', { clientX: 300 });
  ui.fire(ui.win, 'mouseup', {});
  assert.equal(ui.widths()[1], '200px');
  ui.render(qhResTable());
  await ui.flush();
  assert.notEqual(ui.widths()[1], '200px');
});

test('a sort clears the selected cells and the sum bar', opts, async () => {
  const ui = qhResMount(qhResTable());
  qhResSelect(ui, [0, 1], [1, 1]);
  assert.ok(ui.doc.querySelector('.qh-res-sum'), 'the sum bar shows before the sort');
  ui.click(ui.doc.querySelector('button[aria-label="Sort by name"]')); await ui.flush();
  assert.equal(ui.doc.querySelector('.qh-res-sum'), null);
});

test('only the sorted column shows the sort state', opts, async () => {
  const ui = qhResMount(qhResTable());
  ui.click(ui.doc.querySelector('button[aria-label="Sort by name"]')); await ui.flush();
  const th = ui.all('th');
  assert.equal(th[1].getAttribute('aria-sort'), 'ascending');
  assert.equal(th[2].getAttribute('aria-sort'), null);
  const nameBtn = ui.doc.querySelector('button[aria-label="Sort by name"]');
  const amountBtn = ui.doc.querySelector('button[aria-label="Sort by amount"]');
  assert.ok(nameBtn.className.includes('is-on'));
  assert.ok(!amountBtn.className.includes('is-on'));
  assert.equal(amountBtn.title, 'Sort ascending');
});

test('pressing a sort button does not select the column', opts, async () => {
  const ui = qhResMount(qhResTable());
  ui.fire(ui.doc.querySelector('button[aria-label="Sort by name"]'), 'mousedown', {});
  assert.equal(ui.all('th.is-sel').length, 0);
});

test('a selected column feeds the sum bar with every row', opts, async () => {
  const ui = qhResMount(qhResTable());
  ui.fire(ui.all('th')[2], 'mousedown', {});
  ui.fire(ui.win, 'mouseup', {});
  const bar = ui.doc.querySelector('.qh-res-sum');
  assert.ok(bar, 'the sum bar is missing');
  assert.match(bar.textContent, /4 cells/);
  assert.match(bar.textContent, /Sum 65 /);
  assert.match(bar.textContent, /Avg 16\.25 /);
  assert.match(bar.textContent, /Min 5 /);
  assert.match(bar.textContent, /Max 30/);
});

test('the sum bar rounds to six decimals', opts, async () => {
  const rows = [
    { name: 'a', amount: 1.5, doc: '' },
    { name: 'b', amount: 2.25, doc: '' },
    { name: 'c', amount: 1.23456789, doc: '' },
  ];
  const ui = qhResMount(qhResTable(null, rows));
  qhResSelect(ui, [0, 1], [2, 1]);
  const text = ui.doc.querySelector('.qh-res-sum').textContent;
  assert.match(text, /3 cells/);
  assert.match(text, /Sum 4\.984568 /);
  assert.match(text, /Avg 1\.661523 /);
  assert.match(text, /Min 1\.234568 /);
  assert.match(text, /Max 2\.25/);
});

test('two text cells show a count and no sum', opts, async () => {
  const ui = qhResMount(qhResTable());
  qhResSelect(ui, [0, 0], [1, 0]);
  const bar = ui.doc.querySelector('.qh-res-sum');
  assert.ok(bar, 'the sum bar is missing');
  assert.match(bar.textContent, /2 cells/);
  assert.ok(!/Sum/.test(bar.textContent));
});

// A result whose first page holds 2 of 4 rows. A fetch for offset 0 answers at once.
// Any other fetch waits for `release`, or fails when `fail` is set.
function qhResPartial(fail) {
  const rows = qhResRows();
  const held = [];
  const result = { kind: 'table', cols: ['name', 'amount', 'doc'], piiCols: [], rows: rows.slice(0, 2), total: 4,
    truncated: false, statement: 1, statementCount: 1,
    slice: (o, c) => rows.slice(0, 2).slice(o, o + c),
    fetchPage: (o, c) => {
      if (o === 0) return Promise.resolve(rows.slice(0, c));
      if (fail) return Promise.reject(new Error('boom'));
      return new Promise((resolve) => { held.push(() => resolve(rows.slice(o, o + c))); });
    } };
  return { result, release: () => held.forEach((f) => f()) };
}

test('the filter says Loading while rows load, then N of M', opts, async () => {
  const p = qhResPartial(false);
  const ui = qhResMount(p.result);
  const input = ui.doc.querySelector('input[aria-label="Filter rows"]');
  ui.type(input, 'al'); await ui.flush();
  const tools = () => ui.doc.querySelector('.qh-res-tools').textContent;
  assert.match(tools(), /Loading all rows/);
  assert.ok(!/of 4 rows/.test(tools()));
  p.release(); await ui.flush();
  assert.ok(!/Loading all rows/.test(tools()));
  assert.match(tools(), /2 of 4 rows/);
});

test('a failed load shows the error and no count', opts, async () => {
  const p = qhResPartial(true);
  const ui = qhResMount(p.result);
  const input = ui.doc.querySelector('input[aria-label="Filter rows"]');
  ui.type(input, 'al'); await ui.flush();
  const note = ui.doc.querySelector('.qh-res-tools-note.is-error');
  assert.ok(note, 'the error note is missing');
  assert.equal(note.textContent, 'Could not load every row.');
  assert.ok(!/of 4 rows/.test(ui.doc.querySelector('.qh-res-tools').textContent));
});

test('Ctrl+C in the filter box is left to the box', opts, async () => {
  const ui = qhResMount(qhResTable());
  qhResSelect(ui, [0, 1], [1, 1]);
  const press = (target) => {
    const ev = new ui.win.KeyboardEvent('keydown', { key: 'c', ctrlKey: true, bubbles: true, cancelable: true });
    target.dispatchEvent(ev);
    return ev.defaultPrevented;
  };
  assert.equal(press(ui.doc.querySelector('.qh-resultsview')), true, 'the grid takes Ctrl+C when it has focus');
  const input = ui.doc.querySelector('input[aria-label="Filter rows"]');
  input.focus();
  assert.equal(ui.doc.activeElement, input);
  assert.equal(press(input), false, 'the filter box keeps Ctrl+C');
});

test('Copy in the JSON view copies what the view shows', opts, async () => {
  const ui = qhResMount(qhResTable());
  const copied = [];
  ui.win.qhCopyText = (t) => { copied.push(t); return Promise.resolve(true); };
  const td = ui.cell(0, 2);
  ui.fire(td, 'contextmenu', { clientX: 10, clientY: 10 });
  ui.click(ui.all('button').find(b => b.textContent.indexOf('Show full value') === 0));
  const copyBtn = () => ui.all('.qh-peek-head button').find(b => b.textContent.trim() === 'Copy');
  const toggle = () => ui.doc.querySelector('.qh-res-pretty');
  ui.click(copyBtn());
  assert.ok(copied[0].includes('\n') && copied[0].includes('"a": 1'), 'Pretty copies the indented text');
  ui.click(toggle());
  ui.click(copyBtn());
  assert.equal(copied[1], QH_RES_DOC);
  ui.click(toggle());
  assert.equal(toggle().textContent, 'Pretty');
  assert.equal(toggle().getAttribute('aria-pressed'), 'true');
  assert.ok(ui.doc.querySelector('.qh-peek textarea').value.includes('\n'));
});
