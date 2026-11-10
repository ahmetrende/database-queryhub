// Quick info on hover: the editor shows what the catalog knows about the table
// or column name under the pointer.
//
// The bug class this pins: a card that guesses. A bare column that two FROM
// tables have, a word in a string or a comment, a keyword and a name nobody
// knows must give no card at all. Two layers. First the RULES (qhHoverInfo and
// qhHoverLines). Then the PLUMBING in the real SqlEditor in jsdom: a resting
// pointer shows the card and Escape hides it.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { bareWindow, loadInto } from './_load.mjs';

const require_ = createRequire(import.meta.url);

// jsdom is a devDependency. Without it, the rules still run.
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }

// ---------------------------------------------------------------------------
// An invented catalog, shaped like editorSchema in qh-app.jsx
// ---------------------------------------------------------------------------

const col = (name, type, extra = {}) => ({ name, type, nullable: true, pk: false, pii: false, fk: null, ...extra });
const ORDERS = [
  col('id', 'bigint', { nullable: false, pk: true }),
  col('user_id', 'bigint', { nullable: false, fk: 'users.id' }),
  col('total', 'numeric'),
];
const USERS = [
  col('id', 'bigint', { nullable: false, pk: true }),
  col('email', 'text', { pii: true }),
];
const ORDERS_INFO = { schema: 'public', approxRows: 12345, indexes: [
  { name: 'orders_pkey', cols: ['id'], unique: true, pk: true },
  { name: 'orders_user_idx', cols: ['user_id'], unique: false, pk: false },
] };
const USERS_INFO = { schema: 'public', approxRows: null, indexes: [
  { name: 'users_pkey', cols: ['id'], unique: true, pk: true },
] };
const names = (cs) => cs.map((c) => c.name);

const SCHEMA = {
  tables: ['orders', 'users'], columns: ['id', 'user_id', 'total', 'email'], dbs: [],
  tableCols: { orders: names(ORDERS), users: names(USERS) },
  tableColsQ: { 'public.orders': names(ORDERS), 'public.users': names(USERS) },
  tableMeta: { orders: ORDERS, users: USERS },
  tableMetaQ: { 'public.orders': ORDERS, 'public.users': USERS },
  tableInfo: { orders: ORDERS_INFO, users: USERS_INFO },
  tableInfoQ: { 'public.orders': ORDERS_INFO, 'public.users': USERS_INFO },
  qualify: { orders: 'public.orders', users: 'public.users' },
};

// Two schemas have `orders`, so the bare name is ambiguous (qualify is null).
const SHOP_ORDERS = [col('id', 'integer', { nullable: false, pk: true }), col('note', 'text')];
const TWO_SCHEMAS = {
  tables: ['orders'], columns: [], dbs: [],
  tableCols: { orders: names(ORDERS) },
  tableMeta: { orders: ORDERS },
  tableMetaQ: { 'public.orders': ORDERS, 'shop.orders': SHOP_ORDERS },
  tableInfo: { orders: ORDERS_INFO },
  tableInfoQ: { 'public.orders': ORDERS_INFO, 'shop.orders': { schema: 'shop', approxRows: 7, indexes: [] } },
  qualify: { orders: null },
};

// ---------------------------------------------------------------------------
// The rules
// ---------------------------------------------------------------------------

const H = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');

/** The offset one character inside the nth match of `needle`. */
function at(sql, needle, nth = 0) {
  let i = -1;
  for (let k = 0; k <= nth; k++) i = sql.indexOf(needle, i + 1);
  assert.ok(i >= 0, `"${needle}" is not in the test SQL`);
  return i + 1;
}
const hover = (sql, needle, nth = 0, schema = SCHEMA) => H.qhHoverInfo(sql, at(sql, needle, nth), schema, 'postgres');

test('a table in FROM shows its rows, primary key, indexes and foreign keys', () => {
  const sql = 'select * from orders where total > 0';
  const info = hover(sql, 'orders');
  assert.ok(info, 'a FROM table must give a card');
  assert.equal(info.kind, 'table');
  assert.equal(info.name, 'public.orders');
  assert.equal(sql.slice(info.start, info.end), 'orders');
  assert.equal(info.approxRows, 12345);
  assert.equal(info.columns, 3);
  assert.deepEqual(info.pk, ['id']);
  assert.deepEqual(info.indexes.find((x) => x.name === 'orders_user_idx'),
    { name: 'orders_user_idx', cols: ['user_id'], unique: false });
  assert.deepEqual(info.fks, [{ col: 'user_id', ref: 'users.id' }]);
  const lines = H.qhHoverLines(info);
  assert.equal(lines[0], 'public.orders');
  for (const l of ['~12,345 rows', '3 columns', 'PK: id', 'orders_pkey (id) unique',
                   'orders_user_idx (user_id)', 'fk user_id → users.id']) {
    assert.ok(lines.includes(l), `the card must say "${l}"`);
  }
});

test('alias.col resolves through the FROM list, with its foreign key and index', () => {
  const sql = 'select o.user_id from orders o';
  const info = hover(sql, 'user_id');
  assert.ok(info, 'o.user_id must give a card');
  assert.equal(info.kind, 'column');
  assert.equal(info.table, 'public.orders');
  assert.equal(info.name, 'user_id');
  assert.equal(sql.slice(info.start, info.end), 'user_id');
  assert.equal(info.fk, 'users.id');
  assert.equal(info.nullable, false);
  assert.deepEqual(info.indexes, ['orders_user_idx']);
  const lines = H.qhHoverLines(info);
  assert.deepEqual(lines, ['public.orders.user_id', 'bigint', 'NOT NULL', 'FK → users.id', 'Indexed: orders_user_idx']);
});

test('a bare column that the one FROM table has shows its PII mark', () => {
  const info = hover('select email from users', 'email');
  assert.ok(info, 'a bare column of the only FROM table must give a card');
  assert.equal(info.kind, 'column');
  assert.equal(info.table, 'public.users');
  assert.equal(info.pii, true);
  assert.ok(H.qhHoverLines(info).includes('PII · masked in results'));
});

test('a bare column that two FROM tables have gives no card', () => {
  const sql = 'select id from orders o join users u on u.id = o.user_id';
  assert.equal(hover(sql, 'id'), null, 'orders and users both have id: no guess');
  // The qualified name in the same statement still resolves.
  const q = H.qhHoverInfo(sql, sql.indexOf('u.id') + 2, SCHEMA, 'postgres');
  assert.ok(q && q.kind === 'column' && q.table === 'public.users' && q.pk === true);
});

test('a word inside a string or a comment gives no card', () => {
  const sql = "select 'orders' from users -- orders\n/* users */";
  assert.equal(hover(sql, 'orders', 0), null, 'inside a string');
  assert.equal(hover(sql, 'orders', 1), null, 'inside a line comment');
  assert.equal(hover(sql, 'users', 1), null, 'inside a block comment');
  assert.ok(hover(sql, 'users', 0), 'the real FROM table still gives a card');
});

test('a keyword gives no card', () => {
  const sql = 'select * from orders where total > 0';
  assert.equal(hover(sql, 'select'), null);
  assert.equal(hover(sql, 'from'), null);
  assert.equal(hover(sql, 'where'), null);
});

test('an unknown name gives no card', () => {
  assert.equal(hover('select * from shop_items', 'shop_items'), null, 'a table the catalog does not list');
  assert.equal(hover('select nope from orders', 'nope'), null, 'a column no FROM table has');
  assert.equal(hover('select o.nope from orders o', 'nope'), null, 'a qualified column the table does not have');
});

test('a schema-qualified table resolves through tableMetaQ', () => {
  const pub = hover('select * from public.orders', 'orders', 0, TWO_SCHEMAS);
  assert.ok(pub, 'public.orders must give a card');
  assert.equal(pub.name, 'public.orders');
  assert.equal(pub.columns, 3);
  const shop = hover('select * from shop.orders', 'orders', 0, TWO_SCHEMAS);
  assert.ok(shop, 'shop.orders must give a card');
  assert.equal(shop.name, 'shop.orders');
  assert.equal(shop.columns, 2);
  assert.equal(shop.approxRows, 7);
  // The bare name is in two schemas, and the schema word itself is no table.
  assert.equal(hover('select * from orders', 'orders', 0, TWO_SCHEMAS), null);
  assert.equal(hover('select * from public.orders', 'public', 0, TWO_SCHEMAS), null);
});

// ---------------------------------------------------------------------------
// The plumbing: the real editor, a real pointer event, real timers
// ---------------------------------------------------------------------------

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// jsdom has no layout. The stub gives the word "orders" one rectangle.
const RECT = { left: 100, right: 150, top: 20, bottom: 36 };

/** Mount the real SqlEditor with the invented catalog. */
function mount(text) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>',
                        { pretendToBeVisual: true, url: 'https://localhost/' });
  for (const k of ['window', 'document', 'navigator', 'HTMLElement', 'Element',
                   'Node', 'Event', 'KeyboardEvent', 'InputEvent', 'MouseEvent', 'getComputedStyle',
                   'requestAnimationFrame', 'cancelAnimationFrame', 'localStorage']) {
    Object.defineProperty(globalThis, k, {
      value: dom.window[k], configurable: true, writable: true,
    });
  }
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;

  const React = require_('react');
  const ReactDOM = require_('react-dom/client');
  const act = React.act;
  globalThis.React = React;
  const win = dom.window;
  win.React = React;
  win.ReactDOM = require_('react-dom');
  win.Range.prototype.getClientRects = function getClientRects() {
    return this.toString() === 'orders' ? [RECT] : [];
  };
  loadInto(win, 'qh-data.jsx', 'qh-editor.jsx');
  const { SqlEditor } = win;
  assert.ok(SqlEditor, 'qh-editor.jsx did not publish SqlEditor');

  function Host() {
    const [value, setValue] = React.useState(text);
    return React.createElement(SqlEditor, {
      value, docId: 't1', fontSize: 13, schema: SCHEMA, engineId: 'postgres', onChange: setValue,
    });
  }
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  act(() => root.render(React.createElement(Host)));
  const ta = win.document.querySelector('textarea');
  assert.ok(ta, 'the editor rendered no textarea');
  return {
    win, ta, act,
    card: () => win.document.querySelector('.qh-hover-card'),
    move(x, y) { act(() => { ta.dispatchEvent(new win.MouseEvent('mousemove', { clientX: x, clientY: y, bubbles: true })); }); },
    key(k) { act(() => { ta.dispatchEvent(new win.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true })); }); },
    async wait(ms) { await act(async () => { await sleep(ms); }); },
  };
}

const dom = JSDOM ? {} : { skip: 'jsdom not installed' };

test('a pointer that rests on a table shows the card, and Escape hides it', dom, async () => {
  const ed = mount('select * from orders');
  ed.move(120, 28);
  assert.equal(ed.card(), null, 'the card must wait for the pointer to rest');
  await ed.wait(500);
  const card = ed.card();
  assert.ok(card, 'the card must show after the pause');
  assert.equal(card.getAttribute('role'), 'tooltip');
  assert.equal(card.hasAttribute('tabindex'), false, 'the card must never take focus');
  assert.ok(card.textContent.includes('public.orders'));
  assert.ok(card.textContent.includes('~12,345 rows'));
  ed.key('Escape');
  assert.equal(ed.card(), null, 'Escape must hide the card');
});

test('a pointer that leaves the word hides the card, and off every name none comes back', dom, async () => {
  const ed = mount('select * from orders');
  ed.move(120, 28);
  await ed.wait(500);
  assert.ok(ed.card(), 'the card must show after the pause');
  ed.move(125, 30);
  assert.ok(ed.card(), 'a move on the same word keeps the card');
  ed.move(400, 300);
  assert.equal(ed.card(), null, 'a move off the word must hide the card at once');
  await ed.wait(500);
  assert.equal(ed.card(), null, 'no name under the pointer, no card');
});
