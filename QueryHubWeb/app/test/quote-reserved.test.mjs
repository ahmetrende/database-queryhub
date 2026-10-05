// Identifiers the editor writes are quoted when the engine needs it — reserved
// words included.
//
// Measured before the fix (2026-10-05): odd names were already quoted the right
// way per engine ("x" / [x] / `x`, the closing quote doubled), but a column
// called `user`, `order` or `group` came out bare. `order` breaks the query;
// `user` is worse, because PostgreSQL and SQL Server read it as the current
// login and return that on every row instead of the column.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');
const q = (n, e) => W.qhQuoteIdentFor(n, e);

test('PostgreSQL quotes its reserved words, and only those', () => {
  for (const w of ['user', 'order', 'group', 'select', 'left', 'limit', 'table', 'default', 'end'])
    assert.equal(q(w, 'postgres'), '"' + w + '"', w);
  for (const w of ['user_id', 'key', 'name', 'status', 'created_at', 'type', 'value'])
    assert.equal(q(w, 'postgres'), w, w + ' is not reserved and stays bare');
});

test('SQL Server brackets its reserved words, in any case', () => {
  for (const w of ['user', 'KEY', 'Order', 'group', 'percent', 'file', 'index', 'plan'])
    assert.equal(q(w, 'mssql'), '[' + w + ']', w);
  for (const w of ['UserId', 'CreatedAt', 'name', 'limit'])
    assert.equal(q(w, 'mssql'), w, w + ' stays bare');
});

test('Athena and ClickHouse quote their own way', () => {
  assert.equal(q('order', 'athena'), '"order"');
  assert.equal(q('date', 'athena'), 'date', 'not reserved in Trino');
  assert.equal(q('select', 'clickhouse'), '`select`');
  assert.equal(q('final', 'clickhouse'), '`final`');
  assert.equal(q('user_id', 'clickhouse'), 'user_id');
});

test('what already worked still works', () => {
  assert.equal(q('CreatedAt', 'postgres'), '"CreatedAt"');
  assert.equal(q('order date', 'mssql'), '[order date]');
  assert.equal(q('x]y', 'mssql'), '[x]]y]');
  assert.equal(q('a"b', 'postgres'), '"a""b"');
  assert.equal(q('2fa', 'clickhouse'), '`2fa`');
  assert.equal(q('user', undefined), '"user"', 'an unknown engine is PostgreSQL');
  assert.equal(q('user', 'oracle'), 'user', 'Oracle folds to upper case; quoting would change the column');
  assert.equal(W.qhQuoteIdent('user'), '"user"', 'the engine-less helper (tree drags) too');
});

test('the * expansion and autocomplete write the quoted form', () => {
  const schema = { tables: ['accounts'], columns: [], dbs: [],
                   tableCols: { accounts: ['id', 'user', 'order', 'key'] } };
  const sql = 'SELECT * FROM accounts';
  const pg = W.qhStarExpansion(sql, 8, schema, 'postgres', null);
  assert.equal(pg.text, 'id, "user", "order", key');
  const ms = W.qhStarExpansion(sql, 8, schema, 'mssql', null);
  assert.equal(ms.text, 'id, [user], [order], [key]');
  const s = W.qhBuildSuggest('SELECT us', 9, { tables: [], columns: ['user'], dbs: [] }, 'postgres');
  assert.ok(s.items.some(i => i.text === '"user"'), 'a suggested column is inserted quoted');
});
