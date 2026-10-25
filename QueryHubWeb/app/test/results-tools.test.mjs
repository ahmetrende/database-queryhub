// Results grid tools: sort, filter, aggregate, pretty JSON and the view hook.
// The pure helpers load into a bare window. The hook runs in jsdom with React.act.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { bareWindow, loadInto } from './_load.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }
const dom = JSDOM ? {} : { skip: 'jsdom not installed' };

const T = loadInto(bareWindow(), 'qh-results-tools.jsx');

// ---------------------------------------------------------------------------
// qhResCompareCells
// ---------------------------------------------------------------------------

test('compare: numbers compare as numbers', () => {
  assert.ok(T.qhResCompareCells('2', '10') < 0);
  assert.ok(T.qhResCompareCells('10', '9') > 0);
  assert.equal(T.qhResCompareCells('1.50', '1.5'), 0);
  assert.ok(T.qhResCompareCells('1e3', '999') > 0);
  assert.ok(T.qhResCompareCells('-3', '2') < 0);
  assert.ok(T.qhResCompareCells(' 7 ', '8') < 0);
});

test('compare: a number sorts before text', () => {
  assert.ok(T.qhResCompareCells('5', 'abc') < 0);
  assert.ok(T.qhResCompareCells('abc', '5') > 0);
});

test('compare: ISO dates compare as plain strings', () => {
  assert.ok(T.qhResCompareCells('2024-01-02', '2024-01-10') < 0);
  assert.ok(T.qhResCompareCells('2024-01-02 10:00:00', '2024-01-02 09:00:00') > 0);
  assert.equal(T.qhResCompareCells('2024-05-05', '2024-05-05'), 0);
});

test('compare: text uses numeric collation and ignores case', () => {
  assert.ok(T.qhResCompareCells('a2', 'a10') < 0);
  assert.ok(T.qhResCompareCells('b', 'a') > 0);
  assert.equal(T.qhResCompareCells('apple', 'APPLE'), 0);
});

// ---------------------------------------------------------------------------
// qhResSortRows
// ---------------------------------------------------------------------------

const keys = (rows) => rows.map((r) => r.k);

test('sort: asc and desc put empty cells last in both directions', () => {
  const rows = [{ k: 'b' }, { k: '' }, { k: 'a' }, { k: null }, { k: 'c' }];
  assert.deepEqual(keys(T.qhResSortRows(rows, { col: 'k', dir: 'asc' })), ['a', 'b', 'c', '', null]);
  assert.deepEqual(keys(T.qhResSortRows(rows, { col: 'k', dir: 'desc' })), ['c', 'b', 'a', '', null]);
});

test('sort: numbers sort as numbers', () => {
  const rows = [{ k: '10' }, { k: '9' }, { k: '100' }];
  assert.deepEqual(keys(T.qhResSortRows(rows, { col: 'k', dir: 'asc' })), ['9', '10', '100']);
});

test('sort: ties keep their original order in both directions', () => {
  const rows = [{ k: '1', id: 'x' }, { k: '0', id: 'y' }, { k: '1', id: 'z' }];
  assert.deepEqual(T.qhResSortRows(rows, { col: 'k', dir: 'asc' }).map((r) => r.id), ['y', 'x', 'z']);
  assert.deepEqual(T.qhResSortRows(rows, { col: 'k', dir: 'desc' }).map((r) => r.id), ['x', 'z', 'y']);
});

test('sort: the input array is not changed', () => {
  const rows = [{ k: 'b' }, { k: 'a' }];
  const out = T.qhResSortRows(rows, { col: 'k', dir: 'asc' });
  assert.notEqual(out, rows);
  assert.deepEqual(keys(rows), ['b', 'a']);
});

test('sort: null sort or an unknown column returns the same array', () => {
  const rows = [{ k: 'b' }, { k: 'a' }];
  assert.equal(T.qhResSortRows(rows, null), rows);
  assert.equal(T.qhResSortRows(rows, { col: 'nope', dir: 'asc' }), rows);
});

// ---------------------------------------------------------------------------
// qhResFilterRows
// ---------------------------------------------------------------------------

const FROWS = [
  { a: 'Apple', b: 'x:1' },
  { a: 'pear', b: null },
  { a: 'plum', b: 'APP' },
];
const FCOLS = ['a', 'b'];

test('filter: a plain query searches every column, ignoring case', () => {
  assert.deepEqual(T.qhResFilterRows(FROWS, FCOLS, 'app'), [FROWS[0], FROWS[2]]);
  assert.deepEqual(T.qhResFilterRows(FROWS, FCOLS, '  PEAR '), [FROWS[1]]);
});

test('filter: name:text searches one column', () => {
  assert.deepEqual(T.qhResFilterRows(FROWS, FCOLS, 'a:app'), [FROWS[0]]);
  assert.deepEqual(T.qhResFilterRows(FROWS, FCOLS, 'A: APP'), [FROWS[0]]);
  assert.deepEqual(T.qhResFilterRows(FROWS, FCOLS, 'b:x'), [FROWS[0]]);
});

test('filter: an unknown column name searches the whole query with the colon', () => {
  assert.deepEqual(T.qhResFilterRows(FROWS, FCOLS, 'x:1'), [FROWS[0]]);
  assert.deepEqual(T.qhResFilterRows(FROWS, FCOLS, 'zzz:app'), []);
});

test('filter: an empty query or empty text returns the same array', () => {
  assert.equal(T.qhResFilterRows(FROWS, FCOLS, ''), FROWS);
  assert.equal(T.qhResFilterRows(FROWS, FCOLS, '   '), FROWS);
  assert.equal(T.qhResFilterRows(FROWS, FCOLS, 'a:'), FROWS);
});

test('filter: a null cell counts as an empty string', () => {
  assert.deepEqual(T.qhResFilterRows(FROWS, FCOLS, 'null'), []);
  assert.deepEqual(T.qhResFilterRows(FROWS, FCOLS, 'b:pear'), []);
});

// ---------------------------------------------------------------------------
// qhResAggregate
// ---------------------------------------------------------------------------

test('aggregate: mixed numbers, text and empties', () => {
  assert.deepEqual(T.qhResAggregate(['1', '2.5', 'abc', '', null, ' 4 ']),
    { count: 4, numeric: 3, sum: 7.5, avg: 2.5, min: 1, max: 4 });
});

test('aggregate: all text gives null numbers', () => {
  assert.deepEqual(T.qhResAggregate(['a', 'b']),
    { count: 2, numeric: 0, sum: null, avg: null, min: null, max: null });
  assert.deepEqual(T.qhResAggregate([]),
    { count: 0, numeric: 0, sum: null, avg: null, min: null, max: null });
});

test('aggregate: exponent, negative and decimal values', () => {
  assert.deepEqual(T.qhResAggregate(['1e3', '-5']),
    { count: 2, numeric: 2, sum: 995, avg: 497.5, min: -5, max: 1000 });
  assert.deepEqual(T.qhResAggregate(['0.5', '1']),
    { count: 2, numeric: 2, sum: 1.5, avg: 0.75, min: 0.5, max: 1 });
});

// ---------------------------------------------------------------------------
// qhPrettyJson
// ---------------------------------------------------------------------------

test('pretty json: objects and arrays', () => {
  assert.equal(T.qhPrettyJson('{"a":1}'), '{\n  "a": 1\n}');
  assert.equal(T.qhPrettyJson('[1,2]'), '[\n  1,\n  2\n]');
  assert.equal(T.qhPrettyJson('{"a":{"b":[1]}}'),
    '{\n  "a": {\n    "b": [\n      1\n    ]\n  }\n}');
});

test('pretty json: leading whitespace is allowed', () => {
  assert.equal(T.qhPrettyJson('  \n {"a":1}'), '{\n  "a": 1\n}');
});

test('pretty json: invalid text, primitives and non-strings give null', () => {
  assert.equal(T.qhPrettyJson('{a:1}'), null);
  assert.equal(T.qhPrettyJson('['), null);
  assert.equal(T.qhPrettyJson('123'), null);
  assert.equal(T.qhPrettyJson('"x"'), null);
  assert.equal(T.qhPrettyJson('null'), null);
  assert.equal(T.qhPrettyJson(null), null);
  assert.equal(T.qhPrettyJson(undefined), null);
  assert.equal(T.qhPrettyJson(5), null);
  assert.equal(T.qhPrettyJson({ a: 1 }), null);
});

test('max rows constant', () => {
  assert.equal(T.QH_RES_MAX_ROWS, 5000);
});

// ---------------------------------------------------------------------------
// qhUseResultView (jsdom)
// ---------------------------------------------------------------------------

const REASON = 'Sorting and filtering work on results up to 5,000 rows. Download the result to sort a larger one.';

function mountHook(first) {
  const d = new JSDOM('<!doctype html><div id="root"></div>',
                      { pretendToBeVisual: true, url: 'https://localhost/' });
  for (const k of ['window', 'document', 'navigator']) {
    Object.defineProperty(globalThis, k, { value: d.window[k], configurable: true, writable: true });
  }
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const React = require_('react');
  const ReactDOM = require_('react-dom/client');
  const act = React.act;
  const win = d.window;
  win.React = React;
  loadInto(win, 'qh-results-tools.jsx');
  const ctl = {};
  function Host() {
    const [result, setResult] = React.useState(first);
    ctl.setResult = setResult;
    ctl.v = win.qhUseResultView(result);
    return null;
  }
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  act(() => root.render(React.createElement(Host)));
  return {
    ctl,
    get v() { return ctl.v; },
    act: (fn) => act(fn),
    // Let every pending promise and effect finish.
    async settle() {
      for (let i = 0; i < 5; i += 1) {
        await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
      }
    },
    unmount() { act(() => root.unmount()); },
  };
}

const NAMES = ['beta', 'Alpha', 'gamma', 'alpine', 'delta', 'Alp2', 'alp3'];
const NS = ['10', '2', '', '33', '5', '7', null];

// Builds a table result. `inline` rows are the first page. `perCall` caps a fetch.
function mkResult(inline, extra = {}) {
  const all = NAMES.map((name, i) => ({ n: NS[i], name }));
  const calls = [];
  const perCall = extra.perCall || 2000;
  const result = {
    kind: 'table',
    cols: ['n', 'name'],
    rows: all.slice(0, inline),
    total: all.length,
    truncated: false,
    slice: (o, c) => all.slice(0, inline).slice(o, o + c),
    fetchPage: (o, c) => { calls.push([o, c]); return Promise.resolve(all.slice(o, o + Math.min(c, perCall))); },
    ...extra,
  };
  delete result.perCall;
  return { result, calls, all };
}

const names = (rows) => rows.map((r) => r.name);

test('hook: no sort and no filter gives the same object and no fetch', dom, () => {
  const { result, calls } = mkResult(3);
  const h = mountHook(result);
  assert.equal(h.v.view, result);
  assert.equal(h.v.enabled, true);
  assert.equal(h.v.reason, null);
  assert.equal(h.v.sort, null);
  assert.equal(h.v.filter, '');
  assert.equal(h.v.loading, false);
  assert.equal(h.v.error, null);
  assert.equal(h.v.shown, 7);
  assert.equal(h.v.total, 7);
  assert.deepEqual(calls, []);
  h.unmount();
});

test('hook: null result returns the fixed shape', dom, () => {
  const h = mountHook(null);
  assert.equal(h.v.view, null);
  assert.equal(h.v.enabled, false);
  assert.equal(h.v.reason, null);
  assert.equal(h.v.sort, null);
  assert.equal(h.v.filter, '');
  assert.equal(h.v.loading, false);
  assert.equal(h.v.error, null);
  assert.equal(h.v.shown, 0);
  assert.equal(h.v.total, 0);
  h.act(() => { h.v.setFilter('x'); h.v.toggleSort('n'); });
  assert.equal(h.v.sort, null);
  assert.equal(h.v.filter, '');
  h.unmount();
});

test('hook: toggleSort cycles none, asc, desc, none', dom, () => {
  const { result } = mkResult(7);
  const h = mountHook(result);
  h.act(() => h.v.toggleSort('n'));
  assert.deepEqual(h.v.sort, { col: 'n', dir: 'asc' });
  h.act(() => h.v.toggleSort('n'));
  assert.deepEqual(h.v.sort, { col: 'n', dir: 'desc' });
  h.act(() => h.v.toggleSort('n'));
  assert.equal(h.v.sort, null);
  h.act(() => h.v.toggleSort('n'));
  h.act(() => h.v.toggleSort('name'));
  assert.deepEqual(h.v.sort, { col: 'name', dir: 'asc' });
  h.unmount();
});

test('hook: a result that is fully inline sorts without any fetch', dom, () => {
  const { result, calls } = mkResult(7);
  const h = mountHook(result);
  h.act(() => h.v.toggleSort('n'));
  assert.notEqual(h.v.view, result);
  assert.deepEqual(names(h.v.view.rows),
    ['Alpha', 'delta', 'Alp2', 'beta', 'alpine', 'gamma', 'alp3']);
  assert.deepEqual(calls, []);
  h.unmount();
});

test('hook: filter and sort load the missing rows once', dom, async () => {
  const { result, calls } = mkResult(3);
  const h = mountHook(result);
  await h.act(async () => { h.v.toggleSort('n'); h.v.setFilter('alp'); });
  await h.settle();
  assert.deepEqual(calls, [[3, 2000]]);
  assert.equal(h.v.loading, false);
  assert.deepEqual(names(h.v.view.rows), ['Alpha', 'Alp2', 'alpine', 'alp3']);
  assert.equal(h.v.view.total, 4);
  assert.equal(h.v.view.truncated, false);
  assert.equal(h.v.view.fetchPage, undefined);
  assert.deepEqual(names(h.v.view.slice(1, 2)), ['Alp2', 'alpine']);
  assert.equal(h.v.shown, 4);
  assert.equal(h.v.total, 7);

  await h.act(async () => { h.v.toggleSort('n'); });
  await h.settle();
  assert.deepEqual(names(h.v.view.rows), ['alpine', 'Alp2', 'Alpha', 'alp3']);

  await h.act(async () => { h.v.setFilter(''); });
  await h.settle();
  // The sort is still desc on n. 33, 10, 7, 5, 2, then the empties in original order.
  assert.deepEqual(names(h.v.view.rows),
    ['alpine', 'beta', 'Alp2', 'delta', 'Alpha', 'gamma', 'alp3']);
  assert.equal(h.v.shown, 7);

  await h.act(async () => { h.v.toggleSort('n'); });
  await h.settle();
  assert.equal(h.v.view, result);
  assert.deepEqual(calls, [[3, 2000]], 'no second fetch');
  h.unmount();
});

test('hook: rows load one chunk at a time and an empty chunk stops the loop', dom, async () => {
  const a = mkResult(3, { perCall: 2 });
  const h = mountHook(a.result);
  await h.act(async () => { h.v.setFilter('a'); });
  await h.settle();
  assert.deepEqual(a.calls, [[3, 2000], [5, 2000]]);
  assert.equal(h.v.loading, false);
  assert.equal(h.v.shown, 7);
  h.unmount();

  const calls = [];
  const rows = [{ n: '1', name: 'x' }, { n: '2', name: 'y' }];
  const short = {
    kind: 'table', cols: ['n', 'name'], rows: rows.slice(0, 1), total: 10, truncated: false,
    slice: () => [], fetchPage: (o, c) => { calls.push([o, c]); return Promise.resolve(o === 1 ? [rows[1]] : []); },
  };
  const h2 = mountHook(short);
  await h2.act(async () => { h2.v.setFilter('y'); });
  await h2.settle();
  assert.deepEqual(calls, [[1, 2000], [2, 2000]]);
  assert.deepEqual(names(h2.v.view.rows), ['y']);
  h2.unmount();
});

test('hook: while rows load, loading is true and the view is the result', dom, async () => {
  const { result } = mkResult(3);
  let release;
  result.fetchPage = () => new Promise((r) => { release = r; });
  const h = mountHook(result);
  h.act(() => { h.v.setFilter('alp'); });
  assert.equal(h.v.loading, true);
  assert.equal(h.v.view, result);
  assert.equal(h.v.error, null);
  await h.act(async () => { release(NAMES.slice(3).map((name, i) => ({ n: NS[3 + i], name }))); });
  await h.settle();
  assert.equal(h.v.loading, false);
  // Inline rows are beta, Alpha, gamma. The fetched rows are alpine, delta, Alp2, alp3.
  assert.deepEqual(names(h.v.view.rows), ['Alpha', 'alpine', 'Alp2', 'alp3']);
  h.unmount();
});

test('hook: more than 5000 rows is disabled and never fetches', dom, async () => {
  const { result, calls } = mkResult(3, { total: 6000 });
  const h = mountHook(result);
  assert.equal(h.v.enabled, false);
  assert.equal(h.v.reason, REASON);
  await h.act(async () => { h.v.toggleSort('n'); h.v.setFilter('a'); });
  await h.settle();
  assert.equal(h.v.view, result);
  assert.equal(h.v.reason, REASON);
  assert.deepEqual(calls, []);
  h.unmount();
});

test('hook: a truncated result is disabled', dom, async () => {
  const { result, calls } = mkResult(7, { truncated: true });
  const h = mountHook(result);
  assert.equal(h.v.enabled, false);
  assert.equal(h.v.reason, REASON);
  await h.act(async () => { h.v.setFilter('a'); });
  await h.settle();
  assert.equal(h.v.view, result);
  assert.deepEqual(calls, []);
  h.unmount();
});

test('hook: a failed fetch sets the error and keeps the result as the view', dom, async () => {
  const { result } = mkResult(3);
  result.fetchPage = () => Promise.reject(new Error('boom'));
  const h = mountHook(result);
  await h.act(async () => { h.v.setFilter('a'); });
  await h.settle();
  assert.equal(h.v.error, 'Could not load every row.');
  assert.equal(h.v.view, result);
  assert.equal(h.v.loading, false);
  h.unmount();
});

test('hook: a new result object resets sort, filter and the cache', dom, async () => {
  const first = mkResult(3);
  const h = mountHook(first.result);
  await h.act(async () => { h.v.toggleSort('n'); h.v.setFilter('alp'); });
  await h.settle();
  assert.equal(h.v.shown, 4);

  const second = mkResult(3);
  await h.act(async () => { h.ctl.setResult(second.result); });
  await h.settle();
  assert.equal(h.v.sort, null);
  assert.equal(h.v.filter, '');
  assert.equal(h.v.error, null);
  assert.equal(h.v.view, second.result);
  assert.deepEqual(second.calls, []);

  await h.act(async () => { h.v.setFilter('alp'); });
  await h.settle();
  assert.deepEqual(second.calls, [[3, 2000]]);
  assert.equal(h.v.shown, 4);
  h.unmount();
});

// ---------------------------------------------------------------------------
// Edge cases added after the mutation check
// ---------------------------------------------------------------------------

test('sort: a cell with only spaces counts as empty and goes last', () => {
  const rows = [{ k: 'b' }, { k: '  ' }, { k: 'a' }];
  assert.deepEqual(keys(T.qhResSortRows(rows, { col: 'k', dir: 'asc' })), ['a', 'b', '  ']);
  assert.deepEqual(keys(T.qhResSortRows(rows, { col: 'k', dir: 'desc' })), ['b', 'a', '  ']);
});

test('aggregate: a cell with only spaces is not counted', () => {
  assert.deepEqual(T.qhResAggregate(['  ', '1']),
    { count: 1, numeric: 1, sum: 1, avg: 1, min: 1, max: 1 });
});

test('filter: the column name ignores case and outer spaces', () => {
  const rows = [{ Name: 'xray', b: 'q' }, { Name: 'plain', b: 'xylo' }];
  assert.deepEqual(T.qhResFilterRows(rows, ['Name', 'b'], 'name:xr'), [rows[0]]);
  assert.deepEqual(T.qhResFilterRows(rows, ['Name', 'b'], ' name :xr'), [rows[0]]);
});

test('hook: exactly 5000 rows is still enabled', dom, () => {
  const { result } = mkResult(3, { total: 5000 });
  const h = mountHook(result);
  assert.equal(h.v.enabled, true);
  assert.equal(h.v.reason, null);
  h.unmount();
});

test('hook: a filter of only spaces does not fetch and keeps the result', dom, async () => {
  const { result, calls } = mkResult(3);
  const h = mountHook(result);
  await h.act(async () => { h.v.setFilter('   '); });
  await h.settle();
  assert.deepEqual(calls, []);
  assert.equal(h.v.view, result);
  h.unmount();
});

test('hook: rows fetched once are reused when sort comes back on', dom, async () => {
  const { result, calls } = mkResult(3);
  const h = mountHook(result);
  await h.act(async () => { h.v.toggleSort('n'); });
  await h.settle();
  assert.deepEqual(calls, [[3, 2000]]);
  await h.act(async () => { h.v.setSort(null); });
  await h.settle();
  await h.act(async () => { h.v.toggleSort('name'); });
  await h.settle();
  assert.deepEqual(calls, [[3, 2000]], 'no second fetch');
  assert.equal(h.v.view.rows.length, 7);
  h.unmount();
});

test('hook: a fetch error belongs to its own result only', dom, async () => {
  const bad = mkResult(3);
  bad.result.fetchPage = () => Promise.reject(new Error('boom'));
  const h = mountHook(bad.result);
  await h.act(async () => { h.v.setFilter('a'); });
  await h.settle();
  assert.equal(h.v.error, 'Could not load every row.');
  const good = mkResult(3);
  await h.act(async () => { h.ctl.setResult(good.result); });
  await h.settle();
  assert.equal(h.v.error, null);
  assert.equal(h.v.view, good.result);
  h.unmount();
});
