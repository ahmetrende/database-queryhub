// Table and column comments from the catalog (P2.4).
//
// The schema route sends `comment` on a table, a view or a column when the
// catalog has one. The editor hover card shows it as plain text, the schema
// tree shows it in the row tip, and the results grid header shows it in its
// tooltip. These tests pin the rules, and that the app keeps the field when it
// normalizes the payload.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';
import { sliceFunction, web } from './_mount.mjs';

const col = (name, type, extra = {}) => ({ name, type, nullable: true, pk: false, pii: false, fk: null, ...extra });
const ORDERS = [
  col('id', 'bigint', { nullable: false, pk: true, comment: 'Order id.' }),
  col('note', 'text'),
];
const ORDERS_INFO = { schema: 'public', approxRows: 10, indexes: [], comment: 'Customer orders.' };
const names = (cs) => cs.map((c) => c.name);
const SCHEMA = {
  tables: ['orders'], columns: names(ORDERS), dbs: [],
  tableCols: { orders: names(ORDERS) }, tableColsQ: { 'public.orders': names(ORDERS) },
  tableMeta: { orders: ORDERS }, tableMetaQ: { 'public.orders': ORDERS },
  tableInfo: { orders: ORDERS_INFO }, tableInfoQ: { 'public.orders': ORDERS_INFO },
  qualify: { orders: 'public.orders' },
};

const H = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');
const hover = (sql, needle) => H.qhHoverInfo(sql, sql.indexOf(needle) + 1, SCHEMA, 'postgres');

// ---------- the hover card ----------

test('the table card shows the table comment under the name', () => {
  const info = hover('SELECT * FROM orders', 'orders');
  assert.equal(info.kind, 'table');
  assert.equal(info.comment, 'Customer orders.');
  const lines = H.qhHoverLines(info);
  assert.equal(lines[0], 'public.orders');
  assert.equal(lines[1], 'Customer orders.');
});

test('the column card shows the column comment under the name', () => {
  const info = hover('SELECT id FROM orders', 'id');
  assert.equal(info.kind, 'column');
  assert.equal(info.comment, 'Order id.');
  assert.equal(H.qhHoverLines(info)[1], 'Order id.');
});

test('a column without a comment gets no comment line', () => {
  const info = hover('SELECT note FROM orders', 'note');
  assert.equal(info.comment, undefined);
  assert.deepEqual(H.qhHoverLines(info).slice(0, 2), ['public.orders.note', 'text']);
});

test('the comment stays plain text and long text is cut at 300 characters', () => {
  assert.equal(H.qhHoverComment('<b>x</b> & y'), '<b>x</b> & y', 'no HTML is built or escaped twice');
  assert.equal(H.qhHoverComment('line one\nline two'), 'line one line two');
  assert.equal(H.qhHoverComment('   '), null);
  assert.equal(H.qhHoverComment(null), null);
  const long = H.qhHoverComment('a'.repeat(1000));
  assert.equal(long.length, 300);
  assert.ok(long.endsWith('…'));
});

// ---------- the tree and the results header ----------

const P = bareWindow();
sliceFunction('qh-panels.jsx', 'qhCommentTitle', P);
sliceFunction('qh-panels.jsx', 'qhResultColTitle', P);

test('a tree row tip adds the comment on a new line', () => {
  assert.equal(P.qhCommentTitle('public.orders', 'Customer orders.'), 'public.orders\nCustomer orders.');
  assert.equal(P.qhCommentTitle('note', null), undefined, 'no comment keeps the label tip');
  assert.equal(P.qhCommentTitle('note', '  '), undefined);
});

test('the results header tooltip adds the comment on a new line', () => {
  assert.equal(P.qhResultColTitle('id', { type: 'bigint', notNull: true, comment: 'Order id.' }),
    'id — bigint not null\nOrder id.');
  assert.equal(P.qhResultColTitle('id', { type: 'bigint', notNull: true }), 'id — bigint not null');
  assert.equal(P.qhResultColTitle('x', null, 'int4'), 'x — int4');
  assert.equal(P.qhResultColTitle('x', null, undefined), 'x');
  assert.equal(P.qhResultColTitle('id', { type: 'bigint', comment: 'Order id.' }, 'int8'), 'id — int8\nOrder id.');
});

// ---------- the app keeps the field ----------

test('loadSchema, colMeta and editorSchema keep the comment', () => {
  const app = web('qh-app.jsx');
  assert.match(app, /if \(t\.comment\) entry\.comment = String\(t\.comment\);/, 'loadSchema keeps the table comment');
  assert.match(app, /if \(col\.comment\) meta\.comment = String\(col\.comment\);/, 'colMeta keeps the column comment');
  assert.match(app, /comment: c\.comment \? String\(c\.comment\) : null/, 'editorSchema keeps the column comment');
  assert.match(app, /comment: entry\.comment \? String\(entry\.comment\) : null/, 'editorSchema keeps the table comment');
});
