// Alias rename and name qualification (2026-11-12, Devart medium batch, PR M9).
//
//   * F1  Shift+F6 renames the table alias at the caret: its definition and
//         every `alias.` of the statement (qhAliasAt, qhRenameAlias). F2 stays
//         the tab rename.
//   * F4  Shift+Alt+Q puts the schema before a bare table and the alias or
//         table before a bare column (qhQualifyNames). A name it is not sure
//         about stays as typed.
//
// jsdom rule: compare booleans or text, never pass a DOM node to assert.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { bareWindow, loadInto } from './_load.mjs';
import { web } from './_mount.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');
const SCHEMA = {
  tables: ['users', 'orders', 'audit'],
  qualify: { users: 'public.users', orders: 'sales.orders', audit: null },
  tableCols: { users: ['id', 'name', 'active'], orders: ['id', 'user_id', 'total', 'created_at'], audit: ['id'] },
  tableColsQ: { 'public.users': ['id', 'name', 'active'], 'sales.orders': ['id', 'user_id', 'total', 'created_at'] },
};
const renamed = (sql, at, name, eng = 'postgres') => {
  const r = W.qhRenameAlias(sql, sql.indexOf(at), name, eng);
  return r.error ? 'ERR ' + r.error : W.qhApplyEdits(sql, r.edits, 0).text;
};
const qualified = (sql, range, eng = 'postgres') => {
  const r = W.qhQualifyNames(sql, SCHEMA, eng, range);
  return { text: W.qhApplyEdits(sql, r.edits, 0).text, tables: r.tables, columns: r.columns, twice: r.twice.join(',') };
};

// ---------- F1: the alias ----------

const Q = 'select o.id, o.total from orders o join users u on u.id = o.user_id where o.total > 5';

test('the caret on the definition or on a use finds every place', () => {
  const def = W.qhAliasAt(Q, Q.indexOf(' o join') + 1);
  const use = W.qhAliasAt(Q, Q.indexOf('o.total'));
  assert.equal(def.name, 'o');
  assert.equal(def.places.length, 5);
  assert.deepEqual(use.places, def.places);
  assert.equal(W.qhAliasAt(Q, Q.indexOf('o.id') + 1).places.length, 5, 'the caret at the end of the alias');
  assert.equal(def.taken.has('u'), true);
  assert.equal(def.taken.has('users'), true);
  assert.equal(def.taken.has('o'), false);
});

test('a column, a keyword or a table name is no alias', () => {
  assert.equal(W.qhAliasAt(Q, Q.indexOf('total')), null, 'the column after o.');
  assert.equal(W.qhAliasAt(Q, 2), null, 'select');
  assert.equal(W.qhAliasAt(Q, Q.indexOf('orders') + 2), null, 'the table');
  assert.equal(W.qhAliasAt('select o.id from orders', 7), null, 'a qualifier with no alias of that name');
  assert.equal(W.qhRenameAlias(Q, 2, 'x').error, 'Put the caret on a table alias.');
  assert.equal(W.qhAliasAt('select o from t o where o.o = 1', 7), null, 'a bare column with the name of an alias');
});

test('rename writes every place and nothing else', () => {
  assert.equal(renamed(Q, 'o.total', 'ord'), 'select ord.id, ord.total from orders ord join users u on u.id = ord.user_id where ord.total > 5');
  assert.equal(renamed('select "o".id, o.x from t AS o', 'o.x', 'p'), 'select p.id, p.x from t AS p', 'quoted use and AS');
  assert.equal(renamed('select o from t o where o.o = 1', 'o.o', 'k'), 'select o from t k where k.o = 1', 'a column named o stays');
  const two = 'select o.a from t o; select o.b from u o';
  assert.equal(renamed(two, 'o.b', 'x'), 'select o.a from t o; select x.b from u x', 'only the statement of the caret');
});

test('rename refuses a bad, a keyword or a used name, and an alias defined twice', () => {
  assert.match(renamed(Q, 'o.total', '1x'), /letters, digits and _/);
  assert.match(renamed(Q, 'o.total', 'a-b'), /letters, digits and _/);
  assert.match(renamed(Q, 'o.total', 'select'), /SQL keyword/);
  assert.match(renamed(Q, 'o.total', 'join'), /SQL keyword/);
  assert.match(renamed(Q, 'o.total', 'apply'), /SQL keyword/, 'a FROM-list word that is no typing-list keyword');
  assert.match(renamed(Q, 'o.total', 'U'), /is in this statement already/);
  assert.match(renamed(Q, 'o.total', 'users'), /is in this statement already/);
  assert.equal(renamed(Q, 'o.total', 'o'), Q, 'the same name changes nothing');
  const twice = 'select o.id from orders o where exists (select 1 from users o where o.id = 1)';
  assert.match(renamed(twice, 'o.id', 'x'), /defined twice in this statement/);
});

// ---------- F4: qualify ----------

// A table whose columns have the names that the skip rules are about.
const ODD = { qualify: { t: 'public.t' }, tableCols: { t: ['id', 'date', 'n', 's', 'int', 'true', 'sum'] },
  tableColsQ: { 'public.t': ['id', 'date', 'n', 's', 'int', 'true', 'sum'] } };

test('a function, a cast, a typed literal, TRUE and output aliases stay, even when a column has that name', () => {
  const sql = "select sum(id) as s, count(*) n, id::int, DATE '2020-01-01', true from t order by s, n";
  const r = W.qhQualifyNames(sql, ODD, 'postgres');
  assert.equal(W.qhApplyEdits(sql, r.edits, 0).text,
    "select sum(t.id) as s, count(*) n, t.id::int, DATE '2020-01-01', true from public.t order by s, n");
});

test('a subquery column goes to the subquery relation only', () => {
  const r = qualified('select name from users where id in (select id from orders)');
  assert.equal(r.text, 'select users.name from public.users where users.id in (select orders.id from sales.orders)');
});

test('schema before a bare table, alias before a bare column; a column of two tables stays', () => {
  const r = qualified("select name, total, id from users u join orders o on user_id = u.id where active = true and created_at > DATE '2026-01-01' order by total desc");
  assert.equal(r.text, "select u.name, o.total, id from public.users u join sales.orders o on o.user_id = u.id where u.active = true and o.created_at > DATE '2026-01-01' order by o.total desc");
  assert.deepEqual([r.tables, r.columns, r.twice], [2, 6, 'id']);
});

test('no alias: the table name; output aliases, functions and casts stay', () => {
  const r = qualified('select name, sum(total) as s, count(*) n, total::int from users join orders on user_id = users.id group by name order by s, n');
  assert.equal(r.text, 'select users.name, sum(orders.total) as s, count(*) n, orders.total::int from public.users join sales.orders on orders.user_id = users.id group by users.name order by s, n');
});

test('variables, parameters and names already qualified stay', () => {
  assert.equal(qualified('select u.name from users u where id = @id and active = :a').text,
    'select u.name from public.users u where u.id = @id and u.active = :a');
  assert.equal(qualified('select u.name from public.users u').text, 'select u.name from public.users u');
});

test('a block with an unknown relation qualifies no column; each SELECT is its own block', () => {
  assert.equal(qualified('select name from users join audit on true').text, 'select name from public.users join audit on true',
    'audit is in two schemas (qualify null): no columns, its own name stays');
  assert.equal(qualified('select name from users join nowhere on true').text, 'select name from public.users join nowhere on true');
  assert.equal(qualified('select x.name from (select name from users) x').text, 'select x.name from (select users.name from public.users) x');
  assert.equal(qualified('with users as (select 1 as name) select name from users').text, 'with users as (select 1 as name) select name from users',
    'a CTE is no table of the catalog');
  assert.equal(qualified('select name from users union select total from orders').text,
    'select users.name from public.users union select orders.total from sales.orders');
});

test('only the selection, and only SELECT blocks', () => {
  const sql = 'select name from users; select total from orders';
  const second = qualified(sql, [sql.indexOf('select total'), sql.length]);
  assert.equal(second.text, 'select name from users; select orders.total from sales.orders');
  assert.equal(qualified("update users set name = 'x' where id = 1").text, "update users set name = 'x' where id = 1");
  const none = W.qhQualifyNames('select 1', SCHEMA, 'postgres');
  assert.deepEqual([none.edits.length, none.tables, none.columns], [0, 0, 0]);
});

test('SQL Server quotes a schema that needs it', () => {
  const sch = { ...SCHEMA, qualify: { ...SCHEMA.qualify, users: 'my schema.users' }, tableColsQ: { ...SCHEMA.tableColsQ, 'my schema.users': ['id', 'name', 'active'] } };
  const r = W.qhQualifyNames('select name from users', sch, 'mssql');
  assert.equal(W.qhApplyEdits('select name from users', r.edits, 0).text, 'select users.name from [my schema].users');
});

// ---------- the editor ----------

function mount(initial, extra = {}) {
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
  const ctl = { notices: [] };
  function Host() {
    const [v, setV] = React.useState(initial);
    ctl.v = v;
    return React.createElement(win.SqlEditor, { value: v, docId: 't1', fontSize: 13, schema: SCHEMA, engineId: 'postgres',
      onChange: setV, onNotice: (m) => ctl.notices.push(m), ...extra });
  }
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(Host)));
  const ta = win.document.querySelector('textarea');
  const ed = {
    win, ta, ctl, React,
    caret(pos) { ta.focus(); ta.setSelectionRange(pos, pos); },
    select(a, b) { ta.focus(); ta.setSelectionRange(a, b); },
    press(key, mods = {}, el = ta) {
      const ev = new win.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...mods });
      React.act(() => el.dispatchEvent(ev));
      return ev;
    },
    box() { return win.document.querySelector('.qh-rename'); },
    input() { return win.document.querySelector('.qh-rename input'); },
    setInput(v) {
      const el = ed.input();
      const set = Object.getOwnPropertyDescriptor(win.HTMLInputElement.prototype, 'value').set;
      React.act(() => { set.call(el, v); el.dispatchEvent(new win.Event('input', { bubbles: true })); });
    },
  };
  return ed;
}
const dom = JSDOM ? {} : { skip: 'jsdom not installed' };

test('Shift+F6 opens the box; Enter renames in one undo step', dom, () => {
  const ed = mount(Q);
  ed.caret(Q.indexOf('o.total'));
  const ev = ed.press('F6', { shiftKey: true });
  assert.equal(ev.defaultPrevented, true);
  assert.equal(ed.box().querySelector('span').textContent, 'Rename alias o · 5 places');
  assert.equal(ed.input().value, 'o');
  ed.setInput('ord');
  ed.press('Enter', {}, ed.input());
  assert.equal(ed.ctl.v, 'select ord.id, ord.total from orders ord join users u on u.id = ord.user_id where ord.total > 5');
  assert.equal(!!ed.box(), false, 'the box closes');
  ed.press('z', { ctrlKey: true });
  assert.equal(ed.ctl.v, Q, 'one undo restores the text');
});

test('one place says place', dom, () => {
  const ed = mount('select 1 from orders o');
  ed.caret(21);
  ed.press('F6', { shiftKey: true });
  assert.equal(ed.box().querySelector('span').textContent, 'Rename alias o \u00b7 1 place');
});

test('the box keeps a refused name and says why; Escape closes it', dom, () => {
  const ed = mount(Q);
  ed.caret(Q.indexOf('o.total'));
  ed.press('F6', { shiftKey: true });
  ed.setInput('u');
  ed.press('Enter', {}, ed.input());
  assert.equal(ed.ctl.v, Q, 'nothing changes');
  assert.equal(ed.box().querySelector('.qh-rename-err').textContent, 'The name u is in this statement already. Use a different name.');
  ed.press('Escape', {}, ed.input());
  assert.equal(!!ed.box(), false);
});

test('Shift+F6 on no alias says so, and the box goes away by itself', dom, async () => {
  const ed = mount(Q);
  ed.caret(2);
  ed.press('F6', { shiftKey: true });
  assert.equal(ed.box().textContent, 'Put the caret on a table alias.');
  assert.equal(!!ed.input(), false);
  await ed.React.act(() => new Promise(r => setTimeout(r, 3100)));
  assert.equal(!!ed.box(), false);
});

test('Shift+Alt+Q qualifies the text, or the selection, and reports it', dom, () => {
  const sql = 'select name, id from users u join orders o on user_id = u.id';
  const ed = mount(sql);
  ed.caret(0);
  const ev = ed.press('Œ', { shiftKey: true, altKey: true, code: 'KeyQ' });
  assert.equal(ev.defaultPrevented, true);
  assert.equal(ed.ctl.v, 'select u.name, id from public.users u join sales.orders o on o.user_id = u.id');
  assert.deepEqual(ed.ctl.notices, ['Qualified 2 tables and 2 columns. id is in two tables: left as typed.']);
  ed.press('z', { ctrlKey: true });
  assert.equal(ed.ctl.v, sql);
  const ed2 = mount('select 1');
  ed2.caret(0);
  ed2.press('Œ', { shiftKey: true, altKey: true, code: 'KeyQ' });
  assert.deepEqual(ed2.ctl.notices, ['No name to qualify.']);
  const ed3 = mount('select name from users; select total from orders');
  ed3.select(24, 49);
  ed3.press('Œ', { shiftKey: true, altKey: true, code: 'KeyQ' });
  assert.equal(ed3.ctl.v, 'select name from users; select orders.total from sales.orders');
  assert.deepEqual(ed3.ctl.notices, ['Qualified 1 table and 1 column.']);
});

test('F2 is not taken; the shortcut list and the app wiring', () => {
  const src = web('qh-editor.jsx');
  assert.match(src, /\['⇧ \+ F6', 'Rename the alias at the caret'\]/);
  assert.match(src, /\['⇧ \+ ⌥ \/ Alt \+ Q', 'Qualify table and column names'\]/);
  assert.match(src, /\['F2', 'Rename tab'\]/);
  assert.match(web('qh-app.jsx'), /lints=\{lintRanges\} onNotice=\{pushToast\}[ />]/);
});
