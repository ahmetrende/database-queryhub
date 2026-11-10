// Suggestions that know the statement kind (2026-11-10, Devart A2).
//
//   * EXEC (SQL Server) and CALL (PostgreSQL) want a procedure.
//   * DROP, ALTER and CREATE want an object type, then a name of that type.
//   * GRANT and REVOKE want a privilege, then an object, then a role.
//   * REVOKE ... FROM wants a role, never a table.
//   * A word in a string or a comment, or in an earlier statement, changes
//     nothing: the old rules apply.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';
import { web } from './_mount.mjs';

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');

const SCHEMA = {
  tables: ['users', 'orders', 'shops'],
  columns: ['id', 'email', 'user_id'],
  dbs: ['shop', 'shop_archive'],
  tableCols: { users: ['id', 'email'], orders: ['id', 'user_id'], shops: ['id'] },
  tableColsQ: {},
  qualify: { users: 'app.users', orders: 'sales.orders', shops: 'sales.shops' },
  systemTables: [],
  functions: ['calc_total', 'refresh_stats', 'old_helper'],
  functionKind: { calc_total: 'function', refresh_stats: 'procedure' },
  schemas: ['app', 'sales'],
  tableInfo: {
    orders: { schema: 'sales', approxRows: 10, indexes: [{ name: 'ix_orders_user', cols: ['user_id'], unique: false }] },
    users: { schema: 'app', approxRows: 5, indexes: [{ name: 'users_pkey', cols: ['id'], unique: true }] },
  },
  tableInfoQ: {},
};

const split = (sql) => [sql.replace('|', ''), sql.indexOf('|')];
const sug = (sql, eng = 'postgres', opts) => { const [t, c] = split(sql); return W.qhBuildSuggest(t, c, SCHEMA, eng, opts); };
const ctx = (sql, eng = 'postgres') => { const [t, c] = split(sql); return W.qhSuggestContext(t, c, eng); };
const types = (r) => new Set((r ? r.items : []).map(i => i.type));
const texts = (r) => (r ? r.items : []).map(i => i.text);
// The table branch also lists matching functions and databases. The first item
// and the absence of columns are what a table slot promises.
const tableSlot = (r, want) => {
  assert.ok(r, 'a list');
  assert.equal(r.items[0].text, want);
  assert.equal(r.items[0].type, 'table');
  assert.ok(!types(r).has('column'), 'no columns');
  assert.ok(!types(r).has('keyword'), 'no keywords');
};

// ---------- EXEC and CALL ----------

test('EXEC on SQL Server offers procedures, inserted bare', () => {
  const r = sug('EXEC refr|', 'mssql');
  assert.deepEqual(texts(r), ['refresh_stats']);
  assert.equal(r.items[0].type, 'procedure');
  // A routine with no known kind is kept. A plain function is not.
  const all = sug('EXEC |', 'mssql', { force: true });
  assert.ok(texts(all).includes('old_helper'));
  assert.ok(!texts(all).includes('calc_total'));
  assert.ok(texts(all).every(t => !t.includes('(')));
});

test('EXEC @rc = on SQL Server still wants a procedure', () => {
  assert.equal(ctx('EXEC @rc = r|', 'mssql').mode, 'routine');
});

test('CALL on PostgreSQL offers procedures with an open paren', () => {
  assert.deepEqual(texts(sug('CALL ref|')), ['refresh_stats(']);
});

test('EXEC on PostgreSQL keeps the old rules', () => {
  assert.equal(ctx('EXECUTE r|', 'postgres'), null);
});

test('a qualifier after EXEC keeps the routine slot and replaces the token only', () => {
  const r = sug('EXEC dbo.ref|', 'mssql');
  assert.deepEqual(texts(r), ['refresh_stats']);
  assert.equal(r.items[0].start, undefined);
});

// ---------- DROP, ALTER, CREATE ----------

test('DROP offers object types, not tables', () => {
  const r = sug('DROP |', 'postgres', { force: true });
  assert.ok(texts(r).includes('TABLE'));
  assert.ok(texts(r).includes('VIEW'));
  assert.ok(texts(r).includes('MATERIALIZED VIEW'));
  assert.deepEqual([...types(r)], ['keyword']);
  assert.deepEqual(texts(sug('DROP T|')), ['TABLE', 'TRIGGER', 'TYPE']);
});

test('the SQL Server object types differ', () => {
  const r = sug('DROP S|', 'mssql');
  assert.ok(texts(r).includes('SYNONYM'));
  assert.ok(!texts(sug('DROP E|', 'mssql')).includes('EXTENSION'));
});

test('CREATE also offers OR REPLACE and UNIQUE INDEX', () => {
  assert.ok(texts(sug('CREATE O|')).includes('OR REPLACE'));
  assert.ok(texts(sug('CREATE U|')).includes('UNIQUE INDEX'));
  assert.ok(texts(sug('CREATE O|', 'mssql')).includes('OR ALTER'));
  // After CREATE OR REPLACE the type comes next, not OR REPLACE again.
  assert.ok(!texts(sug('CREATE OR REPLACE |', 'postgres', { force: true })).includes('OR REPLACE'));
});

test('DROP TABLE IF EXISTS offers tables', () => {
  tableSlot(sug('DROP TABLE IF EXISTS ord|'), 'sales.orders');
  tableSlot(sug('DROP TABLE users, ord|'), 'sales.orders');
  tableSlot(sug('DROP MATERIALIZED VIEW IF EXISTS ord|'), 'sales.orders');
});

test('Ctrl+Space after DROP TABLE lists tables', () => {
  const r = sug('DROP TABLE |', 'postgres', { force: true });
  assert.deepEqual([...types(r)], ['table', 'system', 'database', 'function'].filter(t => types(r).has(t)));
  assert.ok(texts(r).includes('app.users'));
});

test('DROP FUNCTION offers functions, not procedures, with no paren', () => {
  const r = sug('DROP FUNCTION IF EXISTS |', 'postgres', { force: true });
  assert.deepEqual(texts(r).sort(), ['calc_total', 'old_helper']);
  assert.deepEqual(texts(sug('DROP PROCEDURE refr|')), ['refresh_stats']);
  assert.deepEqual(texts(sug('DROP PROC refr|', 'mssql')), ['refresh_stats']);
});

test('DROP SCHEMA offers schemas', () => {
  const r = sug('DROP SCHEMA s|');
  assert.deepEqual(texts(r), ['sales']);
  assert.equal(r.items[0].type, 'schema');
});

test('DROP INDEX offers index names, schema-qualified on PostgreSQL only', () => {
  assert.deepEqual(texts(sug('DROP INDEX ix|')), ['sales.ix_orders_user']);
  assert.deepEqual(texts(sug('DROP INDEX CONCURRENTLY IF EXISTS ix|')), ['sales.ix_orders_user']);
  assert.deepEqual(texts(sug('DROP INDEX ix|', 'mssql')), ['ix_orders_user']);
  assert.equal(sug('DROP INDEX ix|').items[0].type, 'index');
  // SQL Server names the table after ON.
  tableSlot(sug('DROP INDEX ix_orders_user ON ord|', 'mssql'), 'sales.orders');
});

test('CREATE INDEX ... ON and CREATE TRIGGER ... ON offer tables, not columns', () => {
  const r = sug('CREATE INDEX ix ON |', 'postgres', { force: true });
  assert.ok(types(r).has('table'));
  assert.ok(!types(r).has('column'));
  tableSlot(sug('CREATE UNIQUE INDEX CONCURRENTLY ix ON ord|'), 'sales.orders');
  tableSlot(sug('CREATE INDEX ON ONLY ord|'), 'sales.orders');
  tableSlot(sug('CREATE TRIGGER trg AFTER INSERT ON ord|'), 'sales.orders');
  // Inside the column list the old rules give columns.
  assert.equal(ctx('CREATE INDEX ix ON orders (u|'), null);
});

test('ALTER TABLE offers tables, and a later slot keeps the old rules', () => {
  tableSlot(sug('ALTER TABLE ord|'), 'sales.orders');
  assert.equal(ctx('ALTER TABLE orders ADD |'), null);
});

test('CREATE TABLE ... AS SELECT ... FROM keeps the old FROM rule', () => {
  assert.equal(ctx('CREATE TABLE t2 AS SELECT * FROM o|'), null);
  tableSlot(sug('CREATE TABLE t2 AS SELECT * FROM ord|'), 'sales.orders');
});

// ---------- GRANT and REVOKE ----------

test('GRANT offers privileges', () => {
  assert.deepEqual(texts(sug('GRANT SEL|')), ['SELECT']);
  assert.ok(texts(sug('GRANT SELECT, U|')).includes('UPDATE'));
  assert.ok(texts(sug('GRANT SELECT, U|')).includes('USAGE'));
  assert.ok(texts(sug('GRANT A|')).includes('ALL PRIVILEGES'));
  assert.ok(texts(sug('GRANT V|', 'mssql')).includes('VIEW DEFINITION'));
  assert.ok(texts(sug('REVOKE GRANT OPTION FOR S|')).includes('SELECT'));
});

test('a column list of a privilege keeps the old rules', () => {
  assert.equal(ctx('GRANT SELECT (id, |'), null);
});

test('GRANT ... ON offers tables, then object classes', () => {
  const r = sug('GRANT SELECT ON |', 'postgres', { force: true });
  assert.ok(texts(r).includes('app.users'));
  assert.ok(texts(r).includes('TABLE'));
  assert.ok(texts(r).includes('ALL TABLES IN SCHEMA'));
  const firstKw = r.items.findIndex(i => i.type === 'keyword');
  const lastTable = r.items.map(i => i.type).lastIndexOf('table');
  assert.ok(lastTable < firstKw, 'tables first');
  tableSlot(sug('GRANT SELECT ON TABLE ord|'), 'sales.orders');
  tableSlot(sug('GRANT SELECT ON users, ord|'), 'sales.orders');
});

test('GRANT ... ON SCHEMA and IN SCHEMA offer schemas', () => {
  assert.deepEqual(texts(sug('GRANT USAGE ON SCHEMA ap|')), ['app']);
  assert.deepEqual(texts(sug('GRANT SELECT ON ALL TABLES IN SCHEMA s|')), ['sales']);
  assert.deepEqual(texts(sug('GRANT SELECT ON SCHEMA::s|', 'mssql')), ['sales']);
});

test('GRANT ... ON FUNCTION and ON DATABASE', () => {
  assert.deepEqual(texts(sug('GRANT EXECUTE ON FUNCTION c|')), ['calc_total']);
  assert.deepEqual(texts(sug('GRANT CONNECT ON DATABASE shop_a|')), ['shop_archive']);
});

test('GRANT ... TO and REVOKE ... FROM offer PUBLIC only, never a table', () => {
  assert.deepEqual(texts(sug('GRANT SELECT ON orders TO |', 'postgres', { force: true })), ['PUBLIC']);
  assert.deepEqual(texts(sug('REVOKE SELECT ON orders FROM |', 'postgres', { force: true })), ['PUBLIC']);
  assert.equal(sug('REVOKE SELECT ON orders FROM o|'), null);
  assert.deepEqual(texts(sug('GRANT SELECT ON orders TO reader, P|')), ['PUBLIC']);
});

// ---------- what does not count ----------

test('a keyword in a string or a comment does not switch the mode', () => {
  assert.equal(ctx("SELECT 'drop ' || |"), null);
  assert.equal(ctx('SELECT /* grant */ |'), null);
  assert.equal(ctx('-- drop table\nSELECT * FROM |'), null);
  // The caret inside a string or a comment gives no context at all.
  assert.equal(ctx("SELECT 'DROP |"), null);
  assert.equal(ctx('-- DROP |'), null);
});

test('only the statement at the caret counts', () => {
  assert.equal(ctx('DROP TABLE a; SELECT * FROM t WHERE |'), null);
  assert.equal(ctx('SELECT 1; DROP |').mode, 'objtype');
  // A `;` in a string does not split.
  assert.equal(ctx("SELECT ';'; GRANT |").mode, 'privilege');
});

test('the old rules still hold', () => {
  assert.equal(ctx('SELECT * FROM |'), null);
  tableSlot(sug('SELECT * FROM ord|'), 'sales.orders');
  const r = sug('SELECT u| FROM users');
  assert.ok(types(r).has('column'));
});

// ---------- the list ----------

test('the new kinds have labels and the FK mark sits outside the name', () => {
  const src = web('qh-editor.jsx');
  assert.match(src, /procedure: 'proc', schema: 'schema', index: 'index'/);
  // The FK span follows the closing tag of the name span, so the ellipsis of
  // a long name never cuts it.
  assert.match(src, /<\/span>\) : null}<\/span>\s*\{it\.fk \?/);
  assert.match(web('QueryHub.html'), /\.qh-ac-ed-fk \{ flex: none;/);
  assert.match(web('qh-app.jsx'), /tableInfo, tableInfoQ, schemas \}/);
});
