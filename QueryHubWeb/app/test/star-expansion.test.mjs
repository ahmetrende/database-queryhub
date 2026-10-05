// `SELECT *` expansion: exactly the columns of what the statement selects from.
//
// The report this pins (2026-10-05): `SELECT * FROM ledgers_v2_p55 WHERE ctid =
// '(42647328,1)'`, Tab on the star, and the select list filled with hundreds of
// unrelated columns. ledgers_v2_p55 is a partition; the catalog folds partitions
// into their parent, so the table was unknown and the editor fell back to EVERY
// column in the database. It also read the FIRST `from` in the whole editor.
// Now: the catalog, then the target itself (`live`), or nothing.
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

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');
const expand = W.qhStarExpansion;

// Shaped like qh-app's editorSchema. `columns` is the whole-database pool the
// old code fell back to — kept here so a regression to it is visible.
const SCHEMA = {
  tables: ['orders', 'users', 'events', 'ledgers_v2'],
  columns: ['id', 'total', 'email', 'database_name', 'pid', 'blocking_pid', 'phase'],
  dbs: [],
  tableCols: { orders: ['id', 'total'], users: ['id', 'email'], events: ['id', 'kind'],
               ledgers_v2: ['tx_id', 'user_id'], 'Weird Table': ['Odd Col'] },
  tableColsQ: { 'public.orders': ['id', 'total'], 'public.users': ['id', 'email'],
                'public.events': ['id', 'kind'], 'audit.events': ['id', 'actor'],
                'public.ledgers_v2': ['tx_id', 'user_id'] },
  qualify: { orders: 'public.orders', users: 'public.users', events: null,
             ledgers_v2: 'public.ledgers_v2' },
};
const at = (sql, live, engine = 'postgres', schema = SCHEMA) => {
  const caret = sql.indexOf('*|') + 1;
  return expand(sql.replace('*|', '*'), caret, schema, engine, live);
};
const text = (r) => (r && r.kind === 'ready' ? r.text : r);

test('the report: an unlisted partition never expands to the whole database', () => {
  const sql = "SELECT *| FROM ledgers_v2_p55 WHERE ctid = '(42647328,1)'";
  assert.equal(at(sql, null), null, 'with nothing to ask, nothing is offered');
  assert.deepEqual(at(sql, () => undefined), { kind: 'lookup', names: ['ledgers_v2_p55'] });
  assert.equal(text(at(sql, () => ['tx_id', 'user_id'])), 'tx_id, user_id');
  assert.equal(at(sql, () => null), null, 'the target does not know it either');
});

test('the statement the caret is in decides, not the first FROM in the editor', () => {
  const two = 'SELECT * FROM orders;\nSELECT *| FROM users';
  assert.equal(text(at(two, null)), 'id, email');
  const first = 'SELECT *| FROM orders;\nSELECT * FROM users';
  assert.equal(text(at(first, null)), 'id, total');
});

test('only a select-list star expands', () => {
  assert.equal(at('SELECT count(*| FROM orders', null), null);
  assert.equal(at('SELECT total *| FROM orders', null), null);
  assert.equal(at("SELECT '*| FROM orders'", null), null);
  assert.equal(at('SELECT 1 -- *|\nFROM orders', null), null);
  assert.equal(at('SELECT *|', null), null, 'no FROM yet, nothing to expand');
  assert.equal(at('UPDATE orders SET total = total *| 2', null), null);
  assert.equal(text(at('SELECT id, *| FROM orders', null)), 'id, total');
  assert.equal(text(at('SELECT DISTINCT *| FROM orders', null)), 'id, total');
  assert.equal(text(at('SELECT DISTINCT ON (o.id) *| FROM orders o', null)), 'id, total');
  assert.equal(text(at('SELECT TOP 10 *| FROM orders', null, 'mssql')), 'id, total');
  assert.equal(text(at('SELECT TOP (10) *| FROM orders', null, 'mssql')), 'id, total');
});

test('a join expands every table, qualified by alias; t.* expands t only', () => {
  const j = 'SELECT *| FROM orders o JOIN users u ON u.id = o.id AND LEFT(u.email, 1) = \'a\'';
  assert.equal(text(at(j, null)), 'o.id, o.total, u.id, u.email');
  const r = at('SELECT u.*| FROM orders o LEFT OUTER JOIN users u ON u.id = o.id', null);
  assert.equal(r.text, 'u.id, u.email');
  assert.equal(r.start, 'SELECT '.length, 'u.* is replaced whole');
  assert.equal(text(at('SELECT *| FROM orders, users', null)), 'orders.id, orders.total, users.id, users.email');
  assert.equal(at('SELECT x.*| FROM orders o', null), null, 'no such alias');
});

test('shapes that cannot be listed exactly offer nothing', () => {
  assert.equal(at('SELECT *| FROM orders JOIN users USING (id)', null), null);
  assert.equal(at('SELECT *| FROM orders NATURAL JOIN users', null), null);
  assert.equal(at('SELECT *| FROM (SELECT 1) s', null), null);
  assert.equal(at('SELECT *| FROM generate_series(1, 3) g', null), null);
  assert.equal(at('SELECT *| FROM orders AS o(a, b)', null), null);
  assert.equal(at('SELECT *| FROM orders o CROSS APPLY fn(o.id) f', null, 'mssql'), null);
});

test('names resolve like the server would', () => {
  assert.equal(text(at('SELECT *| FROM public.orders', null)), 'id, total');
  assert.equal(text(at('SELECT *| FROM ORDERS', null)), 'id, total', 'unquoted is case-insensitive');
  assert.equal(text(at('SELECT *| FROM audit.events', null)), 'id, actor');
  // A bare name two schemas share is left to the server's search_path.
  assert.deepEqual(at('SELECT *| FROM events', () => undefined), { kind: 'lookup', names: ['events'] });
  // A quoted name is exact, so "Orders" is not orders.
  assert.deepEqual(at('SELECT *| FROM "Orders"', () => undefined), { kind: 'lookup', names: ['"Orders"'] });
  assert.equal(text(at('SELECT *| FROM "Weird Table"', null)), '"Odd Col"', 'output is quoted per engine');
  assert.equal(text(at('SELECT *| FROM orders WITH (NOLOCK)', null, 'mssql')), 'id, total');
});

test('a star in a subquery reads its own FROM', () => {
  assert.equal(text(at('SELECT s.total FROM (SELECT *| FROM orders) s', null)), 'id, total');
  assert.equal(text(at('SELECT *|, (SELECT max(id) FROM users) AS m FROM orders', null)), 'id, total');
});

test('a catalog table with no columns loaded yet is asked about too', () => {
  const thin = { ...SCHEMA, tableCols: { orders: [] }, tableColsQ: {}, qualify: { orders: 'public.orders' } };
  assert.deepEqual(at('SELECT *| FROM orders', () => undefined, 'postgres', thin), { kind: 'lookup', names: ['orders'] });
});

// ---------------------------------------------------------------------------
// qh-app's cache for the live answers
// ---------------------------------------------------------------------------

const HERE = dirname(fileURLToPath(import.meta.url));
const APP_SRC = readFileSync(resolve(HERE, '..', 'src', 'qh-app.jsx'), 'utf8');

function lookupWith(api) {
  const m = APP_SRC.match(/const QH_LIVE_COLS_RETRY_MS[\s\S]*?\n}\n/);
  assert.ok(m, 'qhLiveColumnLookup is not where this test expects it in qh-app.jsx');
  return new Function('qhApi', m[0] + '\nreturn qhLiveColumnLookup;')(api);
}

test('the live lookup asks once per name and shares the answer', async () => {
  let calls = 0;
  const make = lookupWith({ tableColumns: () => { calls++; return Promise.resolve({ columns: [{ name: 'tx_id' }, { name: 'user_id' }] }); } });
  const cache = new Map();
  const look = make(cache, 'exc-x', 'db1');
  assert.equal(look.peek('ledgers_v2_p55'), undefined);
  const [a, b] = await Promise.all([look.fetch('ledgers_v2_p55'), look.fetch('ledgers_v2_p55')]);
  assert.deepEqual(a, ['tx_id', 'user_id']);
  assert.deepEqual(b, a);
  assert.equal(calls, 1);
  assert.deepEqual(look.peek('ledgers_v2_p55'), ['tx_id', 'user_id']);
  assert.equal(make(cache, 'exc-x', 'other').peek('ledgers_v2_p55'), undefined, 'per database');
});

test('a miss is remembered for a minute, then asked again', async () => {
  let calls = 0;
  const make = lookupWith({ tableColumns: () => { calls++; return Promise.reject(new Error('404')); } });
  const cache = new Map();
  const look = make(cache, 'exc-x', 'db1');
  assert.equal(await look.fetch('nope'), null);
  assert.equal(look.peek('nope'), null);
  await look.fetch('nope');
  assert.equal(calls, 1);
  cache.get('exc-x/db1|nope').at -= 61 * 1000;
  assert.equal(look.peek('nope'), undefined);
  await look.fetch('nope');
  assert.equal(calls, 2);
});

// ---------------------------------------------------------------------------
// The plumbing: the real editor asks, then offers when the answer arrives
// ---------------------------------------------------------------------------

function mount(initial, schema) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { pretendToBeVisual: true, url: 'https://localhost/' });
  for (const k of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node', 'Event',
                   'KeyboardEvent', 'InputEvent', 'getComputedStyle', 'requestAnimationFrame',
                   'cancelAnimationFrame', 'localStorage']) {
    Object.defineProperty(globalThis, k, { value: dom.window[k], configurable: true, writable: true });
  }
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const React = require_('react');
  const ReactDOM = require_('react-dom/client');
  globalThis.React = React;
  const win = dom.window;
  win.React = React;
  win.ReactDOM = require_('react-dom');
  loadInto(win, 'qh-data.jsx', 'qh-editor.jsx');
  const ctl = {};
  function Host() {
    const [v, setV] = React.useState(initial);
    ctl.v = v;
    return React.createElement(win.SqlEditor, { value: v, docId: 't1', fontSize: 13, schema, engineId: 'postgres', onChange: setV });
  }
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(Host)));
  const ta = win.document.querySelector('textarea');
  const setValue = Object.getOwnPropertyDescriptor(win.HTMLTextAreaElement.prototype, 'value').set;
  return {
    win, ta, ctl, act: React.act,
    typeAt(pos, ch) {
      ta.focus();
      ta.setSelectionRange(pos, pos);
      React.act(() => {
        ta.dispatchEvent(new win.InputEvent('beforeinput', { inputType: 'insertText', data: ch, bubbles: true, cancelable: true }));
        setValue.call(ta, ta.value.slice(0, pos) + ch + ta.value.slice(pos));
        ta.setSelectionRange(pos + 1, pos + 1);
        ta.dispatchEvent(new win.InputEvent('input', { inputType: 'insertText', data: ch, bubbles: true }));
      });
    },
    press(key, mods = {}) {
      const ev = new win.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...mods });
      React.act(() => ta.dispatchEvent(ev));
      return ev;
    },
    popup() { return win.document.querySelector('.qh-ac-ed'); },
  };
}

const dom = JSDOM ? {} : { skip: 'jsdom not installed' };

test('the editor asks about an unlisted table and offers its columns when they arrive', dom, async () => {
  const asked = [];
  let answer;
  const cache = new Map();
  const columnLookup = {
    peek: (n) => cache.get(n),
    fetch: (n) => { asked.push(n); return new Promise((r) => { answer = (cols) => { cache.set(n, cols); r(cols); }; }); },
  };
  const ed = mount("SELECT  FROM ledgers_v2_p55 WHERE ctid = '(42647328,1)'", { ...SCHEMA, columnLookup });
  ed.typeAt(7, '*');
  assert.deepEqual(asked, ['ledgers_v2_p55']);
  assert.equal(ed.popup(), null, 'nothing is offered while the answer is out');
  await ed.act(async () => { answer(['tx_id', 'user_id']); await new Promise((r) => setTimeout(r, 0)); });
  assert.ok(ed.popup() && /Expand \* → 2 columns/.test(ed.popup().textContent));
  ed.press('Tab');
  assert.equal(ed.ctl.v, "SELECT tx_id, user_id FROM ledgers_v2_p55 WHERE ctid = '(42647328,1)'");
  ed.press('z', { metaKey: true });
  assert.equal(ed.ctl.v, "SELECT * FROM ledgers_v2_p55 WHERE ctid = '(42647328,1)'");
});

test('an answer that arrives after the caret moved offers nothing', dom, async () => {
  let answer;
  const cache = new Map();
  const columnLookup = {
    peek: (n) => cache.get(n),
    fetch: (n) => new Promise((r) => { answer = (cols) => { cache.set(n, cols); r(cols); }; }),
  };
  const ed = mount('SELECT  FROM ledgers_v2_p55', { ...SCHEMA, columnLookup });
  ed.typeAt(7, '*');
  ed.ta.setSelectionRange(0, 0);
  await ed.act(async () => { answer(['tx_id']); await new Promise((r) => setTimeout(r, 0)); });
  assert.equal(ed.popup(), null);
});
