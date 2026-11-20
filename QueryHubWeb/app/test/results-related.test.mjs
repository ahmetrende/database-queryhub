// Related rows through a foreign key (2026-11-20, P2.9).
//
// qhRelatedSql writes the SELECT for a new editor tab. qhResultFks and
// qhResultRefs read the editor's catalog view of the database that ran the
// result. The last tests read the menu in ResultsView and the props that
// qh-app.jsx passes.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import { bareWindow, loadInto } from './_load.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const w = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx', 'qh-result-download.jsx', 'qh-results-tools.jsx');
const read = (f) => readFileSync(resolve(HERE, '..', 'src', f), 'utf8');

test('related sql: postgres with LIMIT, a number bare', () => {
  assert.equal(w.qhRelatedSql({ engine: 'postgres', table: 'users', col: 'id', value: 42 }),
    'SELECT * FROM users WHERE id = 42;');
  assert.equal(w.qhRelatedSql({ engine: 'postgres', table: 'orders', col: 'user_id', value: 42, limit: 100 }),
    'SELECT * FROM orders WHERE user_id = 42 LIMIT 100;');
});

test('related sql: mssql puts TOP after SELECT and writes N quotes', () => {
  assert.equal(w.qhRelatedSql({ engine: 'mssql', table: 'dbo.orders', col: 'code', value: 'A1', limit: 100 }),
    "SELECT TOP 100 * FROM dbo.orders WHERE code = N'A1';");
  assert.equal(w.qhRelatedSql({ engine: 'mssql', table: 'users', col: 'id', value: 7 }),
    'SELECT * FROM users WHERE id = 7;');
});

test('related sql: a numeric string is bare only with a numeric type', () => {
  assert.equal(w.qhRelatedLiteral('42', 'postgres', 'bigint'), '42');
  assert.equal(w.qhRelatedLiteral('42', 'postgres', null), "'42'");
  assert.equal(w.qhRelatedLiteral('42', 'postgres', 'varchar(10)'), "'42'");
  assert.equal(w.qhRelatedLiteral('abc', 'postgres', 'int4'), "'abc'");
});

test('related sql: a quote in the value is doubled', () => {
  assert.equal(w.qhRelatedSql({ engine: 'postgres', table: 't', col: 'name', value: "O'Brien" }),
    "SELECT * FROM t WHERE name = 'O''Brien';");
  assert.equal(w.qhRelatedLiteral("it's", 'mssql'), "N'it''s'");
});

test('related sql: identifiers are quoted with the engine quote', () => {
  assert.equal(w.qhRelatedSql({ engine: 'postgres', table: 'Sales.Order', col: 'User Id', value: 1 }),
    'SELECT * FROM "Sales"."Order" WHERE "User Id" = 1;');
  assert.equal(w.qhRelatedSql({ engine: 'mssql', table: 'dbo.order', col: 'user id', value: 1 }),
    'SELECT * FROM dbo.[order] WHERE [user id] = 1;');
});

test('related sql: a NULL value gives no SQL', () => {
  assert.equal(w.qhRelatedSql({ engine: 'postgres', table: 't', col: 'c', value: null }), '');
});

const col = (name, extra) => ({ name, type: 'int8', nullable: true, pk: false, pii: false, fk: null, ...extra });
const USERS = [col('id', { pk: true }), col('name', { type: 'text' })];
const ORDERS = [col('id', { pk: true }), col('user_id', { fk: 'users.id' })];
const NOTES = [col('id', { pk: true }), col('author_id', { fk: 'users.id' })];
const SCHEMA = {
  tableMeta: { users: USERS, orders: ORDERS, notes: NOTES },
  tableMetaQ: { 'public.users': USERS, 'public.orders': ORDERS, 'public.notes': NOTES },
  qualify: { users: 'public.users', orders: 'public.orders', notes: 'public.notes' },
  tableInfo: { users: { schema: 'public' }, orders: { schema: 'public' }, notes: { schema: 'public' } },
};

test('fks: the foreign keys of the one FROM table, with the qualified target', () => {
  assert.deepEqual(w.qhResultFks('SELECT * FROM orders', SCHEMA),
    [{ col: 'user_id', refTable: 'public.users', refCol: 'id' }]);
  assert.deepEqual(w.qhResultFks('SELECT id, user_id FROM public.orders;', SCHEMA),
    [{ col: 'user_id', refTable: 'public.users', refCol: 'id' }]);
});

test('fks: a renamed or missing column, a join or no catalog gives none', () => {
  assert.deepEqual(w.qhResultFks('SELECT id, id + 1 AS user_id FROM orders', SCHEMA), []);
  assert.deepEqual(w.qhResultFks('SELECT id FROM orders', SCHEMA), []);
  assert.deepEqual(w.qhResultFks('SELECT * FROM orders o JOIN users u ON u.id = o.user_id', SCHEMA), []);
  assert.deepEqual(w.qhResultFks('SELECT * FROM orders', {}), []);
});

test('refs: the tables whose foreign keys point at the FROM table', () => {
  assert.deepEqual(w.qhResultRefs('SELECT * FROM users', SCHEMA), [
    { table: 'public.notes', col: 'author_id', refCol: 'id' },
    { table: 'public.orders', col: 'user_id', refCol: 'id' },
  ]);
  assert.deepEqual(w.qhResultRefs('SELECT name FROM users', SCHEMA), []);
});

test('menu: ResultsView offers both items and disables a masked value', () => {
  const src = read('qh-panels.jsx');
  const view = src.slice(src.indexOf('function ResultsView('), src.indexOf('function StepsList('));
  assert.match(view, /Open referenced row/);
  assert.match(view, /Rows that reference this/);
  assert.match(view, /The value is masked\./);
  assert.match(view, /qhRelatedSql\(/);
  assert.match(view, /limit: 100/);
});

test('app: qh-app passes fkOf and refsOf under the ranConn and ranDb rule', () => {
  const src = read('qh-app.jsx');
  assert.match(src, /fkOf=\{\(sql\) => \(tab\.ranConn === tab\.conn && tab\.ranDb === tab\.db \? qhResultFks\(sql, editorSchema\) : \[\]\)\}/);
  assert.match(src, /refsOf=\{\(sql\) => \(tab\.ranConn === tab\.conn && tab\.ranDb === tab\.db \? qhResultRefs\(sql, editorSchema\) : \[\]\)\}/);
  assert.match(src, /onOpenSql=\{openSqlTab\}/);
  const panels = read('qh-panels.jsx');
  assert.match(panels, /fkOf=\{fkOf\} refsOf=\{refsOf\} onOpenSql=\{onOpenSql\}/);
});
