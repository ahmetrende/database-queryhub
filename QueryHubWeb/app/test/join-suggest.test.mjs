// JOIN suggestions (2026-11-10).
//
//   * After JOIN, the list starts with whole JOIN clauses: the next table, an
//     alias and an ON condition from a foreign key or a shared column.
//   * After ON, the list starts with the conditions between the new table and
//     the tables before it in FROM.
//   * Exact or nothing: an unknown relation, a CTE, a subquery, NATURAL,
//     USING, CROSS JOIN, a string and a comment give null.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';
import { web } from './_mount.mjs';

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');

const col = (name, type, extra = {}) => ({ name, type, nullable: false, pk: false, pii: false, fk: null, ...extra });
const USERS = [col('id', 'int8', { pk: true }), col('email', 'text')];
const ORDERS = [col('id', 'int8', { pk: true }), col('user_id', 'int8', { fk: 'users.id' }), col('shop_id', 'int8')];
// No foreign key from orders.shop_id. shops.shop_id gives a same-name match.
const SHOPS = [col('id', 'int8', { pk: true }), col('name', 'text'), col('shop_id', 'int8')];
const ORDER_ITEMS = [col('id', 'int8', { pk: true }), col('order_id', 'int8', { fk: 'orders.id' })];
const META = { users: USERS, orders: ORDERS, shops: SHOPS, order_items: ORDER_ITEMS };
const names = (cols) => cols.map(c => c.name);
const SCHEMA = {
  tables: ['users', 'orders', 'shops', 'order_items'],
  columns: ['id', 'email', 'user_id', 'shop_id', 'name', 'order_id'],
  dbs: [],
  tableCols: { users: names(USERS), orders: names(ORDERS), shops: names(SHOPS), order_items: names(ORDER_ITEMS) },
  tableColsQ: {},
  tableMeta: META,
  tableMetaQ: {},
};
const split = (sql) => [sql.replace('|', ''), sql.indexOf('|')];
const join = (sql, sch = SCHEMA) => { const [t, c] = split(sql); return W.qhJoinSuggestions(t, c, sch, 'postgres'); };
const on = (sql, sch = SCHEMA) => { const [t, c] = split(sql); return W.qhJoinOnSuggestions(t, c, sch, 'postgres'); };
const rows = (r) => r.items.map(i => [i.text, i.fk]);

// ---------- after JOIN ----------

test('after JOIN the first item is the table a foreign key points from', () => {
  const r = join('select * from users u join |');
  assert.ok(r, 'JOIN items are offered');
  assert.equal(r.items[0].text, 'orders o ON o.user_id = u.id');
  assert.equal(r.items[0].fk, true);
  assert.equal(r.items[0].type, 'join');
  assert.equal(r.start, 27);
  assert.equal(r.end, 27);
});

test('a foreign key the other way, the oi alias and the ranking', () => {
  const r = join('select * from orders o join |');
  assert.deepEqual(rows(r), [
    ['users u ON u.id = o.user_id', true],
    ['order_items oi ON oi.order_id = o.id', true],
    ['shops s ON s.shop_id = o.shop_id', false],
  ], 'foreign keys first, then same name and same type');
});

test('an alias the statement already uses gets a number', () => {
  const r = join('select * from orders u join |');
  assert.equal(r.items[0].text, 'users u2 ON u2.id = u.user_id');
  assert.equal(W.qhJoinAlias('order_items', new Set(['oi', 'oi2']), 'postgres'), 'oi3');
});

test('a column named id alone never matches', () => {
  const r = join('select * from shops s join |');
  assert.ok(r, 'shops has a same-name column with orders');
  assert.deepEqual(rows(r), [['orders o ON o.shop_id = s.shop_id', false]]);
  assert.ok(!r.items.some(i => /\.id = s\.id/.test(i.text)), 'no id = id guess');
  assert.deepEqual(W.qhJoinPairs('users', USERS, 'shops', SHOPS), []);
});

test('a typed prefix filters the tables and the range covers it', () => {
  const r = join('select * from orders o join or|');
  assert.deepEqual(rows(r), [['order_items oi ON oi.order_id = o.id', true]], 'orders is in FROM already');
  assert.equal(r.start, 28);
  assert.equal(r.end, 30);
});

test('the table name is schema-qualified as a table pick is', () => {
  const sch = { ...SCHEMA, qualify: { users: 'sales.users', orders: 'sales.orders', shops: 'sales.shops', order_items: 'sales.order_items' } };
  assert.equal(join('select * from users u join |', sch).items[0].text, 'sales.orders o ON o.user_id = u.id');
});

test('LEFT OUTER JOIN and a second JOIN work too', () => {
  assert.equal(join('select * from users u left outer join |').items[0].text, 'orders o ON o.user_id = u.id');
  const r = join('select * from users u join orders o on o.user_id = u.id join |');
  assert.equal(r.items[0].text, 'order_items oi ON oi.order_id = o.id');
});

// ---------- after ON ----------

test('after ON the condition from the foreign key comes first', () => {
  const r = on('select * from orders o join users u on |');
  assert.ok(r, 'ON items are offered');
  assert.deepEqual(rows(r)[0], ['u.id = o.user_id', true]);
  const typed = on('select * from orders o join shops s on s|');
  assert.deepEqual(rows(typed), [['s.shop_id = o.shop_id', false]]);
  assert.equal(typed.start, 39);
});

// ---------- exact or nothing ----------

test('an unknown relation gives null', () => {
  assert.equal(join('select * from nope n join |'), null);
  assert.equal(on('select * from orders o join nope n on |'), null);
  assert.ok(join('select * from orders o join |'), 'the same shape with known tables answers');
});

test('a comment or a string gives null', () => {
  assert.equal(join('select * from users u /* join | */'), null);
  assert.equal(join('-- select * from users u join |'), null);
  assert.equal(join("select 'from users u join |'"), null);
  assert.ok(join('select * from users u join | -- note'), 'a comment after the caret does not matter');
});

test('CROSS, NATURAL, USING, a CTE and a subquery give null', () => {
  assert.equal(join('select * from users u cross join |'), null);
  assert.equal(join('select * from users u natural join |'), null);
  assert.equal(join('select * from orders o join users u using (id) join |'), null);
  assert.equal(join('with users as (select 1) select * from users u join |'), null);
  assert.equal(join('select * from (select 1) x join |'), null);
  assert.ok(join('select * from users u inner join |'), 'INNER JOIN answers');
});

// ---------- the wiring ----------

test('the editor puts JOIN items first and draws the FK mark', () => {
  const src = web('qh-editor.jsx');
  assert.match(src, /qhJoinSuggestions\(ta\.value, caret, sch, engineId, qhAliasRules\(qhAliasOptsLoad\(\)\.rules\)\) \|\| qhJoinOnSuggestions\(ta\.value, caret, sch, engineId\)/);
  assert.match(src, /items: js\.items\.concat\(s \? s\.items : \[\]\)/);
  assert.match(src, /className="qh-ac-ed-fk" title="Foreign key"/);
  assert.match(web('QueryHub.html'), /\.qh-ac-ed-fk \{/);
});
