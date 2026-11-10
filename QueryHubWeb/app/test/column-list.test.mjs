// Ctrl+Space column list (2026-11-09).
//
// In a column position of a SELECT, Ctrl+Space lists every column of the tables
// in that SELECT's FROM list, grouped by table in FROM order. One table gives
// bare names. Several give names qualified by alias, or by table name. Each row
// carries the type, the nullability and the primary key from the catalog. A
// table nobody knows lists nothing. Other positions get the normal list.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { bareWindow, loadInto } from './_load.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');
const list = W.qhColumnList;

const meta = (name, type, nullable, pk = false) => ({ name, type, nullable, pk });
const ORDERS = [meta('id', 'int4', false, true), meta('total', 'numeric', false), meta('note', 'text', true)];
const USERS = [meta('id', 'int8', false, true), meta('email', 'text', true)];
// Shaped like qh-app's editorSchema. `columns` is the whole-database pool. It
// must never reach the column list.
const SCHEMA = {
  tables: ['orders', 'users'],
  columns: ['id', 'total', 'note', 'email', 'stray_pool_column'],
  dbs: [],
  tableCols: { orders: ['id', 'total', 'note'], users: ['id', 'email'] },
  tableColsQ: { 'public.orders': ['id', 'total', 'note'], 'public.users': ['id', 'email'] },
  tableMeta: { orders: ORDERS, users: USERS },
  tableMetaQ: { 'public.orders': ORDERS, 'public.users': USERS },
  qualify: { orders: 'public.orders', users: 'public.users' },
};
const at = (sql, live = null, engine = 'postgres', schema = SCHEMA) => {
  const caret = sql.indexOf('|');
  return list(sql.replace('|', ''), caret, schema, engine, live);
};
const picks = (r) => r.items.filter(i => !i.header).map(i => i.text);
const heads = (r) => r.items.filter(i => i.header).map(i => (i.alias ? i.label + ' ' + i.alias : i.label));

// ---------------------------------------------------------------------------
// The rules
// ---------------------------------------------------------------------------

test('one table: bare names, in catalog order', () => {
  const r = at('select | from orders');
  assert.equal(r.kind, 'ready');
  assert.deepEqual(picks(r), ['id', 'total', 'note']);
  assert.deepEqual(heads(r), ['orders']);
  assert.equal(r.count, 3);
  assert.equal(r.start, 7);
  assert.equal(r.end, 7);
});

test('a join: every table, qualified by alias', () => {
  const r = at('select | from orders o join users u on u.id = o.id');
  assert.deepEqual(picks(r), ['o.id', 'o.total', 'o.note', 'u.id', 'u.email']);
  assert.deepEqual(heads(r), ['orders o', 'users u']);
});

test('a join without aliases: qualified by table name', () => {
  assert.deepEqual(picks(at('select | from orders join users on users.id = orders.id')),
    ['orders.id', 'orders.total', 'orders.note', 'users.id', 'users.email']);
  assert.deepEqual(picks(at('select | from orders, users')),
    ['orders.id', 'orders.total', 'orders.note', 'users.id', 'users.email']);
});

test('groups follow the FROM order', () => {
  const r = at('select | from users u join orders o on o.id = u.id');
  assert.deepEqual(heads(r), ['users u', 'orders o']);
  assert.deepEqual(picks(r), ['u.id', 'u.email', 'o.id', 'o.total', 'o.note']);
});

test('the FROM list may come before or after the caret', () => {
  const tail = ' from orders o join users u on u.id = o.id';
  const want = ['o.id', 'o.total', 'o.note', 'u.id', 'u.email'];
  assert.deepEqual(picks(at('select |' + tail)), want, 'select list');
  assert.deepEqual(picks(at('select id' + tail + ' where |')), want, 'WHERE');
  assert.deepEqual(picks(at('select id' + tail + ' where o.id = 1 and |')), want, 'WHERE after AND');
  assert.deepEqual(picks(at('select *' + ' from orders o join users u on |')), want, 'ON');
  assert.deepEqual(picks(at('select id' + tail + ' order by |')), want, 'ORDER BY');
  assert.deepEqual(picks(at('select id' + tail + ' group by |')), want, 'GROUP BY');
  assert.deepEqual(picks(at('select count(|) from orders')), ['id', 'total', 'note'], 'inside a function call');
});

test('type, nullable and primary key are carried', () => {
  const cols = at('select | from orders').items.filter(i => !i.header);
  assert.deepEqual(cols.map(c => [c.label, c.colType, c.nullable, c.pk]),
    [['id', 'int4', false, true], ['total', 'numeric', false, false], ['note', 'text', true, false]]);
});

test('a subquery or an unknown table gives no invented columns', () => {
  assert.equal(at('select | from (select 1 as x) s'), null, 'a subquery in FROM is not read');
  const none = at('select | from nope');
  assert.equal(none.count, 0);
  assert.deepEqual(none.items, []);
  const half = at('select | from orders o join nope n on n.id = o.id');
  assert.deepEqual(picks(half), ['o.id', 'o.total', 'o.note'], 'still qualified: two tables in FROM');
  assert.ok(!picks(half).some(t => /stray_pool_column|email/.test(t)), 'the database pool never leaks in');
});

test('an unknown table asks the live lookup, names only', () => {
  assert.deepEqual(at('select | from nope', () => undefined), { kind: 'lookup', names: ['nope'] });
  const r = at('select | from nope', () => ['a', 'b']);
  assert.deepEqual(picks(r), ['a', 'b']);
  const a = r.items.find(i => i.label === 'a');
  assert.equal(a.colType, '', 'a type is never invented');
  assert.equal(a.nullable, null, 'nullability is never invented');
  assert.equal(at('select | from nope', () => null).count, 0);
});

test('a typed prefix filters, a typed qualifier lists that table bare', () => {
  const r = at('select to| from orders');
  assert.deepEqual(picks(r), ['total']);
  assert.equal(r.start, 7);
  assert.equal(r.end, 9);
  const q = at('select u.| from orders o join users u on u.id = o.id');
  assert.deepEqual(picks(q), ['id', 'email']);
  assert.deepEqual(heads(q), ['users u']);
  assert.equal(q.start, 9);
  assert.equal(q.slot, 7);
  assert.equal(at('select x.| from orders o'), null, 'no such alias');
});

test('positions that are not a column position give no column list', () => {
  assert.equal(at('select * from |'), null, 'a table goes after FROM');
  assert.equal(at('select id | from orders'), null, 'an alias or a keyword goes after a column');
  assert.equal(at("select * from orders where note = 'a|'"), null, 'inside a string');
  assert.equal(at('select 1 -- |\nfrom orders'), null, 'inside a comment');
  assert.equal(at('select *| from orders'), null, 'the star belongs to the * expansion');
  assert.equal(at('select id as | from orders'), null, 'an alias goes after AS');
  assert.equal(at('update orders set total = |'), null, 'not a SELECT');
  assert.equal(at('insert into orders (|'), null, 'not a SELECT');
  assert.equal(at('select |'), null, 'no FROM yet');
});

test('USING joins and a half-typed join list too, the * expansion stays exact', () => {
  assert.deepEqual(picks(at('select | from orders o join users u using (id)')),
    ['o.id', 'o.total', 'o.note', 'u.id', 'u.email']);
  assert.equal(W.qhStarExpansion('SELECT * FROM orders o JOIN users u USING (id)', 8, SCHEMA, 'postgres', null), null);
  assert.deepEqual(picks(at('select | from orders o join ')), ['id', 'total', 'note']);
});

test('names are quoted for the engine', () => {
  const odd = [meta('Odd Col', 'int', true)];
  const S = { tables: ['t'], columns: [], dbs: [], tableCols: {}, tableColsQ: {},
              tableMeta: { t: odd }, tableMetaQ: { 'dbo.t': odd }, qualify: { t: 'dbo.t' } };
  assert.deepEqual(picks(at('select | from t', null, 'mssql', S)), ['[Odd Col]']);
  assert.deepEqual(picks(at('select | from t', null, 'postgres', S)), ['"Odd Col"']);
  assert.deepEqual(picks(at('select | from t a join t b on a.x = b.x', null, 'mssql', S)), ['a.[Odd Col]', 'b.[Odd Col]']);
  for (const eng of ['clickhouse', 'athena']) {
    assert.deepEqual(picks(at('select | from orders', null, eng)), ['id', 'total', 'note'], eng);
  }
});

test('arrow keys skip group headers', () => {
  const items = [{ header: true }, { text: 'a' }, { header: true }, { text: 'b' }];
  assert.equal(W.qhAcStep(items, 1, 1), 3);
  assert.equal(W.qhAcStep(items, 3, 1), 1);
  assert.equal(W.qhAcStep(items, 1, -1), 3);
  assert.equal(W.qhAcStep([{ text: 'a' }, { text: 'b' }, { text: 'c' }], 2, 1), 0, 'a plain list wraps as before');
});

test('the normal list answers with nothing typed only when forced', () => {
  const s = W.qhBuildSuggest('select * from ', 14, SCHEMA, 'postgres');
  assert.equal(s, null, 'typing nothing still opens nothing');
  const f = W.qhBuildSuggest('select * from ', 14, SCHEMA, 'postgres', { force: true });
  assert.ok(f && f.items.some(i => i.label === 'orders'));
});

// ---------------------------------------------------------------------------
// The plumbing: the real editor, real keys
// ---------------------------------------------------------------------------

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
  const ctl = {};
  function Host() {
    const [v, setV] = React.useState(initial);
    ctl.v = v;
    return React.createElement(win.SqlEditor, { value: v, docId: 't1', fontSize: 13, schema: SCHEMA, engineId: 'postgres', onChange: setV });
  }
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(Host)));
  const ta = win.document.querySelector('textarea');
  const setValue = Object.getOwnPropertyDescriptor(win.HTMLTextAreaElement.prototype, 'value').set;
  const ed = {
    win, ta, ctl, act: React.act,
    caret(pos) { ta.focus(); ta.setSelectionRange(pos, pos); },
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
    ctrlSpace() { return ed.press(' ', { ctrlKey: true, code: 'Space' }); },
    popup() { return win.document.querySelector('.qh-ac-ed'); },
    opts() { return Array.from(win.document.querySelectorAll('.qh-ac-ed-opt')); },
    hi() { return win.document.querySelector('.qh-ac-ed .is-hi'); },
  };
  return ed;
}

const dom = JSDOM ? {} : { skip: 'jsdom not installed' };
const JOIN = 'select  from orders o join users u on u.id = o.id';

test('Ctrl+Space in an empty select list opens every column', dom, () => {
  const ed = mount(JOIN);
  ed.caret(7);
  assert.equal(ed.popup(), null);
  const ev = ed.ctrlSpace();
  assert.ok(ev.defaultPrevented, 'the editor owns Ctrl+Space');
  assert.ok(ed.popup() && ed.popup().classList.contains('is-cols'));
  assert.equal(ed.opts().length, 5);
  const groups = Array.from(ed.win.document.querySelectorAll('.qh-ac-ed-group'));
  assert.deepEqual(groups.map(g => g.textContent), ['orderso', 'usersu']);
  const first = ed.opts()[0].textContent;
  assert.ok(first.startsWith('id') && first.includes('PK') && first.includes('int4') && first.includes('NOT NULL'), first);
  assert.ok(ed.opts()[2].textContent.endsWith('textNULL'), 'note is nullable');
  assert.equal(ed.ctl.v, JOIN, 'opening the list types nothing');
});

test('typing filters the list and Enter inserts the qualified name', dom, () => {
  const ed = mount(JOIN);
  ed.caret(7);
  ed.ctrlSpace();
  ed.typeAt(7, 'e');
  ed.typeAt(8, 'm');
  assert.equal(ed.opts().length, 1);
  assert.ok(ed.opts()[0].textContent.startsWith('email'));
  ed.press('Enter');
  assert.equal(ed.ctl.v, 'select u.email from orders o join users u on u.id = o.id');
  assert.equal(ed.popup(), null);
});

test('a space after the typed name leaves the column list', dom, () => {
  const ed = mount('select  from orders');
  ed.caret(7);
  ed.ctrlSpace();
  ed.typeAt(7, 'i');
  assert.equal(ed.opts().length, 1, 'filtered to id');
  ed.typeAt(8, ' ');
  assert.equal(ed.popup(), null);
});

test('Escape closes the list', dom, () => {
  const ed = mount(JOIN);
  ed.caret(7);
  ed.ctrlSpace();
  assert.ok(ed.popup());
  ed.press('Escape');
  assert.equal(ed.popup(), null);
  assert.equal(ed.ctl.v, JOIN);
});

test('group headers cannot be picked, by key or by mouse', dom, () => {
  const ed = mount(JOIN);
  ed.caret(7);
  ed.ctrlSpace();
  assert.ok(ed.hi().textContent.startsWith('id'), 'the first column is picked, not the header');
  ed.press('ArrowDown');
  ed.press('ArrowDown');
  ed.press('ArrowDown');
  assert.ok(ed.hi() && ed.hi().classList.contains('qh-ac-ed-opt'), 'the header between the groups is skipped');
  assert.ok(ed.hi().textContent.startsWith('id') && ed.hi().textContent.includes('int8'), 'u.id');
  ed.press('ArrowDown');
  ed.press('ArrowDown');
  assert.ok(ed.hi().textContent.includes('int4'), 'wraps to o.id, past the first header');
  ed.press('ArrowUp');
  assert.ok(ed.hi().textContent.startsWith('email'), 'up from the first column wraps to the last');
  const head = ed.win.document.querySelector('.qh-ac-ed-group');
  ed.act(() => head.dispatchEvent(new ed.win.MouseEvent('mousedown', { bubbles: true, cancelable: true })));
  assert.equal(ed.ctl.v, JOIN, 'a click on a header inserts nothing');
  assert.ok(ed.popup(), 'and keeps the list open');
});

test('Ctrl+Space in another position opens the normal list', dom, () => {
  const ed = mount('select * from ');
  ed.caret(14);
  ed.ctrlSpace();
  assert.ok(ed.popup(), 'the normal list opens with nothing typed');
  assert.ok(!ed.popup().classList.contains('is-cols'));
  assert.ok(ed.opts().some(o => o.textContent.startsWith('orders')));
});

test('the list that opens while typing works as before', dom, () => {
  const ed = mount('select * from ');
  ed.typeAt(14, 'o');
  assert.ok(ed.popup() && !ed.popup().classList.contains('is-cols'));
  assert.ok(ed.opts().some(o => o.textContent.startsWith('orders')));
});
