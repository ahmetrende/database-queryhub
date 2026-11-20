// SQL for several tree objects at once (2026-11-20, P2.10).
//
// Ctrl/Cmd+click and Shift+click select tables and views of one database in
// the tree. With 2 or more selected, the table menu has an "N tables" group:
// "SELECT with JOINs" (qhJoinScript), "SELECT * for each" (qhEachSelectScript)
// and "Copy names". Nothing runs.
//
// jsdom rule: compare booleans or text, never pass a DOM node to assert.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';
import { page, web, jsdomOpts as opts } from './_mount.mjs';

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');

const SCH = { tables: {
  'sales.users': { schema: 'sales', columns: [{ name: 'id', type: 'bigint' }, { name: 'email', type: 'text' }], indexes: [] },
  'sales.orders': { schema: 'sales', columns: [{ name: 'id', type: 'bigint' }, { name: 'user_id', type: 'bigint', fk: 'sales.users.id' }], indexes: [] },
  'sales.order_items': { schema: 'sales', columns: [{ name: 'id', type: 'bigint' }, { name: 'order_id', type: 'bigint', fk: 'orders.id' }], indexes: [] },
  'sales.audit_log': { schema: 'sales', columns: [{ name: 'id', type: 'bigint' }, { name: 'payload', type: 'jsonb' }], indexes: [] },
  'sales.offers': { schema: 'sales', columns: [{ name: 'id', type: 'bigint' }, { name: 'region', type: 'text' }], indexes: [] },
  'sales.outlets': { schema: 'sales', columns: [{ name: 'id', type: 'bigint' }, { name: 'region', type: 'text' }], indexes: [] },
}, views: [] };
const T = (...names) => names.map(name => ({ schema: 'sales', name }));

test('qhJoinScript: two tables with a foreign key join on the FK columns', () => {
  assert.equal(W.qhJoinScript(T('users', 'orders'), SCH, 'postgres'),
    'SELECT u.*, o.*\nFROM sales.users u\nJOIN sales.orders o ON o.user_id = u.id;');
});

test('qhJoinScript: a foreign key in the other direction', () => {
  assert.equal(W.qhJoinScript(T('orders', 'users'), SCH, 'postgres'),
    'SELECT o.*, u.*\nFROM sales.orders o\nJOIN sales.users u ON u.id = o.user_id;');
});

test('qhJoinScript: three tables in a chain, in any order', () => {
  assert.equal(W.qhJoinScript(T('users', 'orders', 'order_items'), SCH, 'postgres'),
    'SELECT u.*, o.*, oi.*\nFROM sales.users u\nJOIN sales.orders o ON o.user_id = u.id\nJOIN sales.order_items oi ON oi.order_id = o.id;');
  // users links to nothing before orders joins, so a second pass joins it.
  assert.equal(W.qhJoinScript(T('order_items', 'users', 'orders'), SCH, 'postgres'),
    'SELECT oi.*, o.*, u.*\nFROM sales.order_items oi\nJOIN sales.orders o ON o.id = oi.order_id\nJOIN sales.users u ON u.id = o.user_id;');
});

test('qhJoinScript: no relation gives a comment line and no CROSS JOIN', () => {
  const sql = W.qhJoinScript(T('users', 'audit_log'), SCH, 'postgres');
  assert.equal(sql, '-- no join found for sales.audit_log\nSELECT u.*\nFROM sales.users u;');
  assert.equal(/CROSS/i.test(sql), false);
});

test('qhJoinScript: an alias clash gets a number, a same-name column joins', () => {
  assert.equal(W.qhJoinScript(T('offers', 'outlets'), SCH, 'postgres'),
    'SELECT o.*, o2.*\nFROM sales.offers o\nJOIN sales.outlets o2 ON o2.region = o.region;');
});

test('qhJoinScript: SQL Server quoting', () => {
  const sch = { tables: {
    'dbo.Order Lines': { columns: [{ name: 'Id', type: 'int' }, { name: 'OrderId', type: 'int', fk: 'dbo.Orders.Id' }] },
    'dbo.Orders': { columns: [{ name: 'Id', type: 'int' }] },
  } };
  const sql = W.qhJoinScript([{ schema: 'dbo', name: 'Order Lines' }, { schema: 'dbo', name: 'Orders' }], sch, 'mssql');
  assert.match(sql, /^SELECT ol\.\*, o\.\*\n/);
  assert.match(sql, /FROM dbo\.\[Order Lines\] ol\n/);
  assert.match(sql, /JOIN dbo\.Orders o ON o\.Id = ol\.OrderId;$/);
});

test('qhEachSelectScript: one limited SELECT per table, TOP on SQL Server', () => {
  assert.equal(W.qhEachSelectScript(T('users', 'orders'), 'postgres'),
    'SELECT * FROM sales.users LIMIT 100;\nSELECT * FROM sales.orders LIMIT 100;');
  assert.equal(W.qhEachSelectScript([{ schema: 'dbo', name: 'Order Lines' }, { schema: 'dbo', name: 'customers' }], 'mssql'),
    'SELECT TOP 100 * FROM dbo.[Order Lines];\nSELECT TOP 100 * FROM dbo.customers;');
  assert.equal(W.qhJoinScript([], SCH, 'postgres'), '');
});

test('qhTreeMultiPick: toggle, range, and a new database starts over', () => {
  const it = (name, db = 'shop') => ({ conn: 'pg1', db, schema: 'sales', name });
  const names = (st) => st.sel.map(x => x.name).join(',');
  let st = W.qhTreeMultiPick({ sel: [], anchor: null }, it('users'), 'toggle');
  st = W.qhTreeMultiPick(st, it('orders'), 'toggle');
  assert.equal(names(st), 'users,orders');
  st = W.qhTreeMultiPick(st, it('users'), 'toggle');
  assert.equal(names(st), 'orders');
  const order = [it('a'), it('b'), it('c'), it('d')];
  st = W.qhTreeMultiPick({ sel: [], anchor: it('d') }, it('b'), 'range', order);
  assert.equal(names(st), 'b,c,d');
  assert.equal(st.anchor.name, 'd', 'a range keeps its anchor');
  st = W.qhTreeMultiPick(st, it('x', 'other'), 'toggle');
  assert.equal(names(st), 'x');
});

test('the tree, the menu and the CSS are wired', () => {
  const panels = web('qh-panels.jsx');
  assert.match(panels, /multi=\{inMulti\} onClick=\{\(e\) => relClick\(e, mit, mOrder, tid\)\}/);
  assert.match(panels, /onKeyDown=\{onTreeKey\}/);
  assert.match(panels, /qhJoinScript\(m\.multi, /);
  const html = web('QueryHub.html');
  assert.match(html, /\.qh-tr\.is-multi \{/);
  const editor = web('qh-editor.jsx');
  assert.match(editor, /qhJoinScript, qhEachSelectScript, qhTreeMultiPick,/);
});

// ----- jsdom: the real tree -----
const CONNS = [{ id: 'pg1', name: 'pg1', engine: 'PostgreSQL 15', env: 'test',
  databases: [{ id: 'shop', name: 'shop', tier: 'RO',
    tableRefs: [{ s: 'sales', n: 'users' }, { s: 'sales', n: 'orders' }, { s: 'sales', n: 'order_items' }] }] }];

function mountTree() {
  const ui = page(['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx'], {
    prep: (win) => { win.localStorage.setItem('qh.treeview.v1', 'dbs'); },
  });
  const noop = () => {};
  const calls = { newQuery: [] };
  ui.render(ui.React.createElement(ui.win.Sidebar, {
    mode: 'conns', setMode: noop, conns: CONNS, connsLoading: false, schemaCache: { 'pg1/shop': SCH }, onLoadSchema: noop, onLoadRoles: noop, rolesCache: {},
    active: null, onPick: noop, bindOnClick: false, saved: [], onLoadSaved: noop, onDeleteSaved: noop,
    sessions: [], scheduled: [], history: [], onLoadHistory: noop, onNewQuery: (c, db) => calls.newQuery.push(c.id + '/' + db.id), onNewTab: noop, onOpenTable: noop,
    onRequestEndpoint: noop, onRequestAuto: noop, onToast: noop, canRefresh: false, isSuper: false }));
  const node = (id) => ui.all('[data-node]').find(e => e.getAttribute('data-node') === id);
  ui.click(node('pg1/shop'));
  ui.click(ui.all('.qh-tr').find(e => (e.querySelector('.qh-tr-label') || {}).textContent === 'Tables'));
  const row = (n) => node('pg1/shop/t/sales.' + n);
  const mouse = (el, type, init) => ui.React.act(() => el.dispatchEvent(new ui.win.MouseEvent(type, { bubbles: true, cancelable: true, clientX: 20, clientY: 20, ...init })));
  const multi = () => ui.all('.qh-tr.is-multi').map(e => e.getAttribute('data-node')).sort().join(',');
  const menuText = () => ui.all('.qh-ctxmenu button').map(b => b.textContent);
  const closeMenu = () => { const b = ui.doc.querySelector('.qh-ctx-backdrop'); if (b) ui.click(b); };
  return { ui, calls, row, mouse, multi, menuText, closeMenu };
}

test('Ctrl+click adds is-multi, Escape clears it, a plain click clears it', opts, () => {
  const t = mountTree();
  assert.equal(!!t.row('users'), true, 'the table rows are open');
  t.mouse(t.row('users'), 'click', { ctrlKey: true });
  t.mouse(t.row('orders'), 'click', { metaKey: true });
  assert.equal(t.multi(), 'pg1/shop/t/sales.orders,pg1/shop/t/sales.users');
  t.ui.key(t.row('users'), 'Escape');
  assert.equal(t.multi(), '');
  t.mouse(t.row('users'), 'click', { ctrlKey: true });
  t.mouse(t.row('order_items'), 'click', { shiftKey: true });
  assert.equal(t.ui.all('.qh-tr.is-multi').length, 3, 'Shift+click selects the range');
  t.mouse(t.row('orders'), 'click');
  assert.equal(t.multi(), '', 'a plain click clears the selection');
});

test('the menu shows the "N tables" group only with 2 or more selected', opts, () => {
  const t = mountTree();
  t.mouse(t.row('users'), 'click', { ctrlKey: true });
  t.mouse(t.row('users'), 'contextmenu');
  assert.equal(t.menuText().some(s => s.includes('SELECT with JOINs')), false, 'one selected: no group');
  t.closeMenu();
  t.mouse(t.row('orders'), 'click', { ctrlKey: true });
  t.mouse(t.row('orders'), 'contextmenu');
  const items = t.menuText();
  assert.equal(items.some(s => s.includes('SELECT with JOINs')), true);
  assert.equal(items.some(s => s.includes('SELECT * for each')), true);
  assert.equal(items.some(s => s.includes('Copy names')), true);
  assert.equal(t.ui.all('.qh-ctx-label').some(e => e.textContent === '2 tables'), true);
  // Without onOpenSql the tree opens a new query tab on the selection's database.
  t.ui.click(t.ui.all('.qh-ctxmenu button').find(b => b.textContent.includes('SELECT with JOINs')));
  assert.deepEqual(t.calls.newQuery, ['pg1/shop']);
});
