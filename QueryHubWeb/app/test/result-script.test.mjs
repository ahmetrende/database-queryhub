// Generate script as (2026-11-10): the result grid copies INSERT, WHERE and
// UPDATE text built from the selected cells. It never writes a masked value.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import { bareWindow, loadInto } from './_load.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const w = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx', 'qh-result-download.jsx');
const gen = (kind, opts) => w.qhResultScript(kind, { table: 'orders', engineId: 'postgres', ...opts });

test('INSERT writes one line per row with numbers bare and strings quoted', () => {
  const out = gen('insert', { columns: ['id', 'city'], rows: [[1, 'Rome'], [2, 'Oslo']] });
  assert.equal(out, "INSERT INTO orders (id, city) VALUES (1, 'Rome');\n"
    + "INSERT INTO orders (id, city) VALUES (2, 'Oslo');");
});

test('INSERT writes NULL for null and undefined, and doubles a quote', () => {
  const out = gen('insert', { columns: ['id', 'city', 'zip'], rows: [[3, null, undefined], [4, "O'Hare", "it's 'x'"]] });
  assert.equal(out, 'INSERT INTO orders (id, city, zip) VALUES (3, NULL, NULL);\n'
    + "INSERT INTO orders (id, city, zip) VALUES (4, 'O''Hare', 'it''s ''x''');");
});

test('WHERE IN on one column removes duplicates and puts NULL in its own OR', () => {
  const out = gen('where-in', { columns: ['city'], rows: [['Rome'], ['Oslo'], ['Rome'], [null]] });
  assert.equal(out, "WHERE city IN ('Rome', 'Oslo') OR city IS NULL");
});

test('WHERE IN with only NULL values writes IS NULL', () => {
  assert.equal(gen('where-in', { columns: ['city'], rows: [[null], [null]] }), 'WHERE city IS NULL');
});

test('WHERE IN on several columns falls back to AND / OR with a comment', () => {
  const out = gen('where-in', { columns: ['id', 'city'], rows: [[1, 'Rome'], [2, 'Oslo']] });
  assert.equal(out, '-- Several columns: written as AND / OR.\n'
    + "WHERE (id = 1 AND city = 'Rome')\n   OR (id = 2 AND city = 'Oslo')");
});

test('WHERE AND OR writes one condition per row and IS NULL for a null', () => {
  const out = gen('where-andor', { columns: ['id', 'city'], rows: [[1, 'Rome'], [2, null]] });
  assert.equal(out, "WHERE (id = 1 AND city = 'Rome')\n   OR (id = 2 AND city IS NULL)");
});

test('UPDATE takes the key from the full row and leaves it out of SET', () => {
  const base = {
    allColumns: ['id', 'city', 'zip'], pkCols: ['id'],
    fullRows: [[10, 'Rome', '001'], [11, null, '002']],
  };
  const onlyCity = gen('update', { ...base, columns: ['city'], rows: [['Rome'], [null]] });
  assert.equal(onlyCity, "UPDATE orders SET city = 'Rome' WHERE id = 10;\n"
    + 'UPDATE orders SET city = NULL WHERE id = 11;');
  const withKey = gen('update', { ...base, columns: ['id', 'city'], rows: [[10, 'Rome'], [11, null]] });
  assert.equal(withKey, onlyCity);
});

test('UPDATE without a usable key writes the placeholder and the warning first', () => {
  const opts = { columns: ['city'], rows: [['Rome'], ['Oslo']], allColumns: ['id', 'city'], fullRows: [[1, 'Rome'], [2, 'Oslo']] };
  const expected = '-- No primary key in the result. Add a WHERE condition before you run this.\n'
    + "UPDATE orders SET city = 'Rome' WHERE /* add a key */;\n"
    + "UPDATE orders SET city = 'Oslo' WHERE /* add a key */;";
  assert.equal(gen('update', { ...opts, pkCols: [] }), expected);
  // A key column that the result does not hold is no key.
  assert.equal(gen('update', { ...opts, pkCols: ['order_no'] }), expected);
});

test('a numeric column type keeps a number bare, and other text stays quoted', () => {
  const out = gen('insert', {
    columns: ['qty', 'note', 'price', 'flag'], types: { qty: 'int4', price: 'numeric(10,2)' },
    rows: [['42', '42', 'n/a', true], [7, 'x', '3.50', false]],
  });
  assert.equal(out, "INSERT INTO orders (qty, note, price, flag) VALUES (42, '42', 'n/a', true);\n"
    + "INSERT INTO orders (qty, note, price, flag) VALUES (7, 'x', 3.50, false);");
});

test('SQL Server gets N strings, bracket identifiers and 1 / 0 for a boolean', () => {
  const out = w.qhResultScript('insert', {
    table: '[sales].[orders]', engineId: 'mssql',
    columns: ['Order Id', 'city', 'paid'], rows: [[1, "D'Arc", true], [2, 'Oslo', false]],
  });
  assert.equal(out, "INSERT INTO [sales].[orders] ([Order Id], city, paid) VALUES (1, N'D''Arc', 1);\n"
    + "INSERT INTO [sales].[orders] ([Order Id], city, paid) VALUES (2, N'Oslo', 0);");
  const pg = gen('where-in', { columns: ['Order Id'], rows: [['a']] });
  assert.equal(pg, `WHERE "Order Id" IN ('a')`);
});

test('a masked column is skipped with a comment and its value never appears', () => {
  const secret = 'XX00SECRET0001';
  const opts = {
    columns: ['id', 'city', 'iban'], rows: [[1, 'Rome', secret], [2, 'Oslo', secret]], masked: new Set(['iban']),
    allColumns: ['id', 'city', 'iban'], fullRows: [[1, 'Rome', secret], [2, 'Oslo', secret]], pkCols: ['id'],
  };
  for (const kind of ['insert', 'where-in', 'where-andor', 'update']) {
    const out = gen(kind, opts);
    assert.ok(out.split('\n').includes('-- Skipped masked column: iban'), kind + ': ' + out);
    assert.ok(!out.includes(secret), kind + ' leaked the masked value');
    assert.ok(/\bid\b/.test(out.split('\n').filter(l => !l.startsWith('--')).join('\n')), kind + ' lost the id column');
  }
  // UPDATE with only the masked column selected gives the comment and nothing else.
  assert.match(gen('update', { ...opts, columns: ['iban'], rows: [[secret], [secret]] }), /^-- Skipped masked column: iban$/);
});

test('when every selected column is masked, only the comment comes back', () => {
  const out = gen('insert', { columns: ['iban', 'card_no'], rows: [['XX11', '4111']], masked: ['iban', 'card_no'] });
  assert.equal(out, '-- Skipped masked columns: iban, card_no');
});

test('a masked key column is never used as the UPDATE key', () => {
  const out = gen('update', {
    columns: ['city'], rows: [['Rome']], masked: new Set(['iban']),
    allColumns: ['iban', 'city'], fullRows: [['XX00KEY', 'Rome']], pkCols: ['iban'],
  });
  assert.ok(!out.includes('XX00KEY'));
  assert.match(out, /WHERE \/\* add a key \*\/;$/);
});

test('no table gives table_name and a first comment line', () => {
  const out = w.qhResultScript('insert', { engineId: 'postgres', columns: ['id'], rows: [[1]] });
  assert.equal(out, '-- Replace table_name with the real table.\nINSERT INTO table_name (id) VALUES (1);');
});

test('UPDATE is offered on postgres and mssql only', () => {
  assert.ok(w.qhResultScriptKinds('postgres').includes('update'));
  assert.ok(w.qhResultScriptKinds('mssql').includes('update'));
  assert.ok(!w.qhResultScriptKinds('clickhouse').includes('update'));
  assert.deepEqual(w.qhResultScriptKinds('clickhouse'), ['insert', 'where-in', 'where-andor']);
  assert.equal(w.qhResultScript('update', { engineId: 'clickhouse', table: 'orders', columns: ['id'], rows: [[1]] }), '');
});

test('the table comes from a FROM with exactly one relation', () => {
  const t = w.qhResultScriptTable;
  assert.equal(t('SELECT id, city FROM shop.orders WHERE id = 1', 'postgres'), 'shop.orders');
  assert.equal(t('select * from "Order Lines" o;', 'postgres'), '"Order Lines"');
  assert.equal(t('SELECT TOP 5 * FROM [dbo].[orders] WITH (NOLOCK)', 'mssql'), 'dbo.orders');
  assert.equal(t('SELECT * FROM orders o JOIN users u ON u.id = o.user_id', 'postgres'), null);
  assert.equal(t('SELECT * FROM orders, users', 'postgres'), null);
  assert.equal(t('SELECT id FROM orders UNION SELECT id FROM users', 'postgres'), null);
  assert.equal(t('WITH x AS (SELECT 1) SELECT * FROM x', 'postgres'), null);
  assert.equal(t('SELECT * FROM (SELECT 1) s', 'postgres'), null);
  assert.equal(t('SELECT 1 FROM orders; SELECT 2 FROM users', 'postgres'), null);
  assert.equal(t('', 'postgres'), null);
});

test('the grid menu builds the script from the selection and passes the masked columns', () => {
  const src = readFileSync(resolve(HERE, '..', 'src', 'qh-panels.jsx'), 'utf8');
  assert.match(src, /Generate script as<\/div>/);
  assert.match(src, /qhResultScript\(kind, \{[\s\S]*?masked: piiSet/);
  assert.match(src, /qhResultScriptTable\(sourceSql, engineId\)/);
  assert.match(src, /onClick=\{\(\) => menuScript\(k\)\}/);
});

// The UPDATE key (2026-11-10). It comes from the catalog of the database that
// ran the statement, and only when the select list returns the key as it is.
const META = (cols) => cols.map(([name, pk]) => ({ name, pk: !!pk }));
const PK_SCHEMA = {
  tableMeta: { orders: META([['id', 1], ['total']]), users: META([['id', 1], ['email']]) },
  tableMetaQ: { 'shop.orders': META([['id', 1], ['total']]), 'billing.orders': META([['ref', 1], ['total']]) },
  qualify: { users: 'public.users', orders: null },
};
const pkOf = (sql) => w.qhResultScriptPk(sql, PK_SCHEMA);

test('the key comes from the catalog for a star, t.* or the plain key column', () => {
  assert.deepEqual(pkOf('SELECT * FROM users'), ['id']);
  assert.deepEqual(pkOf('SELECT u.* FROM users u'), ['id']);
  assert.deepEqual(pkOf('SELECT id, email FROM users'), ['id']);
  assert.deepEqual(pkOf('SELECT u.id, u.email FROM users u WHERE u.id > 3'), ['id']);
  assert.deepEqual(pkOf('SELECT DISTINCT id FROM users'), ['id']);
});

test('a qualified table reads its own schema, and the name match ignores case', () => {
  assert.deepEqual(pkOf('SELECT * FROM shop.orders'), ['id']);
  assert.deepEqual(pkOf('SELECT * FROM billing.orders'), ['ref']);
  assert.deepEqual(pkOf('SELECT * FROM Shop.Orders'), ['id']);
});

test('a bare name that two schemas hold gives no key', () => {
  assert.deepEqual(pkOf('SELECT * FROM orders'), []);
});

test('a column renamed to the key name drops the key', () => {
  assert.deepEqual(pkOf('SELECT email AS id FROM users'), []);
  assert.deepEqual(pkOf('SELECT id + 1 id FROM users'), []);
  assert.deepEqual(pkOf('SELECT *, email AS id FROM users'), []);
  assert.deepEqual(pkOf('SELECT email FROM users'), []);
});

test('a join, a subquery or several statements give no key', () => {
  assert.deepEqual(pkOf('SELECT * FROM users u JOIN orders o ON o.id = u.id'), []);
  assert.deepEqual(pkOf('SELECT * FROM (SELECT * FROM users) s'), []);
  assert.deepEqual(pkOf('SELECT * FROM users; SELECT * FROM users'), []);
  assert.deepEqual(pkOf('SELECT * FROM unknown_table'), []);
  assert.deepEqual(w.qhResultScriptPk('SELECT * FROM users', null), []);
});

test('the relation parts come back unquoted for the catalog lookup', () => {
  assert.deepEqual(w.qhResultScriptRelation('SELECT * FROM "shop"."Orders"'), ['shop', 'Orders']);
  assert.deepEqual(w.qhResultScriptRelation('SELECT * FROM [dbo].[orders]'), ['dbo', 'orders']);
  assert.equal(w.qhResultScriptRelation('SELECT 1'), null);
});

test('the results panel sends the statement on screen and the key lookup to the grid', () => {
  const src = readFileSync(resolve(HERE, '..', 'src', 'qh-panels.jsx'), 'utf8');
  assert.match(src, /const stSql = stParts\.length === stCount \? \(stParts\[stN - 1\] \|\| null\) : null;/);
  assert.match(src, /sourceSql=\{stSql\} pkOf=\{pkOf\}/);
  assert.match(src, /pkCols: \(pkOf && sourceSql\) \? pkOf\(sourceSql\) : \[\]/);
  const app = readFileSync(resolve(HERE, '..', 'src', 'qh-app.jsx'), 'utf8');
  assert.match(app, /ranSql: sqlOverride \|\| cur\.sql, ranDb: cur\.db,/);
  assert.match(app, /tab\.ranConn === tab\.conn && tab\.ranDb === tab\.db \? qhResultScriptPk\(sql, editorSchema\) : \[\]/);
});
