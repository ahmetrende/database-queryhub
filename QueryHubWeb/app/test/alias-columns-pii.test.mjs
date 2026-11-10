// Alias columns and the PII mark (2026-11-10).
//
//   * Typing `alias.` lists the columns of the table that the alias names, not
//     every column of the database. A schema-qualified table, `sys.` and an
//     unknown alias keep the normal list.
//   * A column that the catalog flags as PII carries a lock in the list.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { bareWindow, loadInto } from './_load.mjs';
import { web } from './_mount.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');

const meta = (name, type, nullable, pk = false, pii = false) => ({ name, type, nullable, pk, pii });
const ORDERS = [meta('id', 'int4', false, true), meta('user_id', 'int8', false), meta('total', 'numeric', false)];
const USERS = [meta('id', 'int8', false, true), meta('email', 'text', true, false, true)];
const SCHEMA = {
  tables: ['orders', 'users'],
  columns: ['id', 'user_id', 'total', 'email', 'stray_pool_column'],
  dbs: [],
  tableCols: { orders: ['id', 'user_id', 'total'], users: ['id', 'email'] },
  tableColsQ: { 'public.orders': ['id', 'user_id', 'total'], 'public.users': ['id', 'email'] },
  tableMeta: { orders: ORDERS, users: USERS },
  tableMetaQ: { 'public.orders': ORDERS, 'public.users': USERS },
  qualify: { orders: 'public.orders', users: 'public.users' },
  piiColumns: new Set(['email']),
};
const split = (sql) => [sql.replace('|', ''), sql.indexOf('|')];
const alias = (sql, live = null) => { const [t, c] = split(sql); return W.qhAliasColumns(t, c, SCHEMA, 'postgres', live); };
const picks = (r) => r.items.filter(i => !i.header).map(i => i.text);
const JOIN = ' from orders o join users u on u.id = o.user_id';

// ---------- the rules ----------

test('the column list carries the PII flag of each column', () => {
  const [t, c] = split('select | from users');
  const r = W.qhColumnList(t, c, SCHEMA, 'postgres', null);
  assert.deepEqual(r.items.filter(i => !i.header).map(i => [i.label, i.pii]), [['id', false], ['email', true]]);
});

test('alias. lists only the columns of the aliased table', () => {
  const r = alias('select u.|' + JOIN);
  assert.equal(r.kind, 'ready');
  assert.deepEqual(picks(r), ['id', 'email'], 'users only, bare names');
  assert.ok(!picks(r).some(n => /user_id|total|stray_pool_column/.test(n)), 'no orders column, no database pool');
  assert.equal(r.start, 9);
  assert.equal(r.end, 9);
  assert.deepEqual(picks(alias('select o.|' + JOIN)), ['id', 'user_id', 'total']);
});

test('a typed prefix after the alias filters that table', () => {
  const r = alias('select u.em|' + JOIN);
  assert.deepEqual(picks(r), ['email']);
  assert.equal(r.start, 9);
  assert.equal(r.end, 11);
});

test('everything else falls back to the normal list', () => {
  assert.equal(alias('select x.|' + JOIN), null, 'an alias that no FROM entry has');
  assert.equal(alias('select u.zz|' + JOIN), null, 'no column of that table matches');
  assert.equal(alias('select * from public.ord|'), null, 'a schema-qualified table');
  assert.equal(alias('select * from sys.dm_|'), null, 'a system schema');
  assert.equal(alias('select |' + JOIN), null, 'no dot: the typing list stays as it was');
  const [t, c] = split('select x.|' + JOIN);
  const s = W.qhBuildSuggest(t, c, SCHEMA, 'postgres', { force: true });
  assert.ok(s && s.items.some(i => i.label === 'stray_pool_column'), 'the fallback still lists the database pool');
});

test('a relation the catalog does not list is asked about once', () => {
  assert.deepEqual(alias('select n.| from nope n', () => undefined), { kind: 'lookup', names: ['nope'] });
  assert.deepEqual(picks(alias('select n.| from nope n', () => ['a', 'b'])), ['a', 'b']);
  assert.equal(alias('select n.| from nope n', () => null), null, 'no such relation: the normal list');
});

test('the typing list marks a bare column that is PII in some table', () => {
  const [t, c] = split('select e|');
  const s = W.qhBuildSuggest(t, c, SCHEMA, 'postgres');
  const email = s.items.find(i => i.label === 'email' && i.type === 'column');
  assert.equal(email.pii, true);
  const plain = W.qhBuildSuggest(t, c, { ...SCHEMA, piiColumns: undefined }, 'postgres');
  assert.ok(!plain.items.find(i => i.label === 'email').pii, 'no set, no mark');
  assert.deepEqual(s.items.map(i => i.label), plain.items.map(i => i.label), 'ranking does not change');
});

test('the app sends the PII flag to the editor', () => {
  const app = web('qh-app.jsx');
  assert.match(app, /pk: !!c\.pk, pii: !!c\.pii,\s+fk: c\.fk \? String\(c\.fk\) : null \}\)\)/, 'tableMeta and tableMetaQ carry pii and fk');
  assert.match(app, /if \(c\.pii\) piiColumns\.add\(c\.name\)/, 'piiColumns collects flagged names');
  assert.match(app, /columnLookup, piiColumns, tableInfo, tableInfoQ, schemas \}/, 'editorSchema returns piiColumns and the table info');
  assert.match(app, /indexes: Array\.isArray\(entry\.indexes\) \? entry\.indexes : \[\]/, 'table info carries the indexes');
});

// ---------- the popup, in the real editor ----------

function mount(initial) {
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
  function Host() {
    const [v, setV] = React.useState(initial);
    return React.createElement(win.SqlEditor, { value: v, docId: 't1', fontSize: 13, schema: SCHEMA, engineId: 'postgres', onChange: setV });
  }
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(Host)));
  const ta = win.document.querySelector('textarea');
  const setValue = Object.getOwnPropertyDescriptor(win.HTMLTextAreaElement.prototype, 'value').set;
  return {
    win,
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
    popup() { return win.document.querySelector('.qh-ac-ed'); },
    opts() { return Array.from(win.document.querySelectorAll('.qh-ac-ed-opt')); },
  };
}

const dom = JSDOM ? {} : { skip: 'jsdom not installed' };

test('typing the dot opens the alias columns with the PII lock', dom, () => {
  const ed = mount('select u' + JOIN);
  ed.typeAt(8, '.');
  assert.ok(ed.popup() && ed.popup().classList.contains('is-cols'), 'the column list, not the normal list');
  assert.deepEqual(ed.opts().map(o => o.querySelector('.qh-ac-ed-name').firstChild.textContent), ['id', 'email']);
  const lock = ed.opts()[1].querySelector('.qh-ac-ed-pii');
  assert.ok(lock, 'email has the PII lock');
  assert.equal(lock.getAttribute('title'), 'PII: masked in results');
  assert.equal(ed.opts()[0].querySelector('.qh-ac-ed-pii'), null, 'id has none');
});

test('the normal typing list marks a PII name in at least one table', dom, () => {
  const ed = mount('select ');
  ed.typeAt(7, 'e');
  const row = ed.opts().find(o => o.querySelector('.qh-ac-ed-name').firstChild.textContent === 'email');
  assert.ok(row, 'email is offered');
  assert.equal(row.querySelector('.qh-ac-ed-pii').getAttribute('title'), 'PII in at least one table');
});
