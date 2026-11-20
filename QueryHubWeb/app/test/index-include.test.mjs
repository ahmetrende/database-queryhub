// The INCLUDE columns of an index (P1.5): the hover card shows them after the
// key columns, as "ix (a, b) include (c, d)". An index without them keeps the
// old line.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';

const H = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');

const COLS = [
  { name: 'id', type: 'int', nullable: false, pk: true, pii: false, fk: null },
  { name: 'customer_id', type: 'int', nullable: false, pk: false, pii: false, fk: null },
  { name: 'total', type: 'money', nullable: true, pk: false, pii: false, fk: null },
];
const INFO = { schema: 'dbo', approxRows: 10, indexes: [
  { name: 'PK_orders', cols: ['id'], unique: true, pk: true, include: [] },
  { name: 'IX_cover', cols: ['customer_id'], unique: false, pk: false, include: ['total', 'status'] },
  { name: 'IX_old', cols: ['total'], unique: false, pk: false },
] };
const SCHEMA = {
  tables: ['orders'], columns: COLS.map((c) => c.name), dbs: [],
  tableCols: { orders: COLS.map((c) => c.name) },
  tableColsQ: { 'dbo.orders': COLS.map((c) => c.name) },
  tableMeta: { orders: COLS }, tableMetaQ: { 'dbo.orders': COLS },
  tableInfo: { orders: INFO }, tableInfoQ: { 'dbo.orders': INFO },
  qualify: { orders: 'dbo.orders' },
};

test('the hover card shows the included columns after the key columns', () => {
  const sql = 'select * from orders';
  const info = H.qhHoverInfo(sql, sql.indexOf('orders') + 1, SCHEMA, 'postgres');
  assert.ok(info && info.kind === 'table', 'a FROM table must give a card');
  assert.deepEqual(info.indexes.find((x) => x.name === 'IX_cover').include, ['total', 'status']);
  assert.deepEqual(info.indexes.find((x) => x.name === 'IX_old').include, [], 'a payload without include gives []');
  const lines = H.qhHoverLines(info);
  for (const l of ['PK_orders (id) unique', 'IX_cover (customer_id) include (total, status)', 'IX_old (total)']) {
    assert.ok(lines.includes(l), `the card must say "${l}"`);
  }
});
