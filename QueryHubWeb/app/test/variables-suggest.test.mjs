// Variables complete and show their type (2026-11-20, P2.12).
//
//   * SQL Server: after `@` the list is the variables and parameters of the
//     caret's batch, declared before the caret. After `@@` it is the global
//     functions. A string or a comment gives nothing.
//   * PostgreSQL: inside a routine body, the DECLARE variables and the named
//     parameters of the routine go first in the list.
//   * Hover on a known variable shows its type and its declaration line.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');

const SCHEMA = {
  tables: ['users', 'orders'],
  columns: ['id', 'email'],
  dbs: [],
  tableCols: { users: ['id', 'email'], orders: ['id'] },
  tableColsQ: {},
  qualify: {},
  systemTables: [],
  functions: [],
  functionKind: {},
  schemas: [],
  tableInfo: {},
  tableInfoQ: {},
  tableMeta: {},
  tableMetaQ: {},
};

const split = (sql) => [sql.replace('|', ''), sql.indexOf('|')];
const sug = (sql, eng = 'mssql', opts) => { const [t, c] = split(sql); return W.qhBuildSuggest(t, c, SCHEMA, eng, opts); };
const labels = (r) => (r ? r.items : []).map(i => i.label);
const vars = (sql, eng) => { const [t, c] = split(sql); return W.qhVariablesAt(t, c, eng); };

// ---------- SQL Server ----------

test('a typed @name lists the matching variable with its type', () => {
  const sql = 'DECLARE @total decimal(10, 2), @n int; SELECT @t|';
  const [t] = split(sql);
  const r = sug(sql);
  assert.deepEqual(labels(r), ['@total']);
  assert.equal(r.items[0].text, '@total');
  assert.equal(r.items[0].type, 'variable');
  assert.equal(r.items[0].desc, 'decimal(10, 2)');
  assert.equal(r.start, t.lastIndexOf('@t'), 'the range covers the @ sign');
  assert.equal(r.end, t.length);
});

test('a bare @ lists every variable', () => {
  const r = sug('DECLARE @total decimal(10, 2), @n int; SELECT @|');
  assert.deepEqual(labels(r).sort(), ['@n', '@total']);
});

test('a declaration after the caret or before a GO line is not listed', () => {
  assert.deepEqual(labels(sug('DECLARE @a int; SELECT @| ; DECLARE @b int;')), ['@a']);
  assert.deepEqual(labels(sug('DECLARE @old int;\nGO\nDECLARE @new int; SELECT @|')), ['@new']);
});

test('a table variable has the type TABLE', () => {
  const v = vars('DECLARE @t TABLE (id int, name varchar(10)); SELECT * FROM @|', 'mssql');
  assert.equal(v.length, 1);
  assert.equal(v[0].name, '@t');
  assert.equal(v[0].type, 'TABLE');
});

test('procedure parameters are listed as parameters', () => {
  const r = sug("CREATE PROCEDURE p @a int, @b varchar(10) = 'x' OUTPUT AS SELECT @|");
  assert.deepEqual(labels(r).sort(), ['@a', '@b']);
  assert.ok(r.items.every(i => i.type === 'parameter'));
  const b = r.items.find(i => i.label === '@b');
  assert.equal(b.desc, 'varchar(10)');
  const f = vars('CREATE FUNCTION dbo.f (@x int = 1, @y nvarchar(50)) RETURNS int AS BEGIN RETURN @|', 'mssql');
  assert.deepEqual(f.map(x => x.name + ' ' + x.type + ' ' + x.kind), ['@x int parameter', '@y nvarchar(50) parameter']);
});

test('@@ lists the global functions', () => {
  const r = sug('SELECT @@ro|');
  assert.deepEqual(labels(r), ['@@ROWCOUNT']);
  assert.equal(r.items[0].type, 'system');
  assert.ok(labels(sug('SELECT @@|')).includes('@@TRANCOUNT'));
});

test('@ in a string or a comment gives nothing', () => {
  assert.equal(sug("DECLARE @a int; SELECT '@|"), null);
  assert.equal(sug('DECLARE @a int; -- @|'), null);
  assert.equal(sug("DECLARE @a int; SELECT '@|", 'mssql', { force: true }), null, 'Ctrl+Space too');
});

test('the line of a declaration is 1-based in the full text', () => {
  const v = vars('SELECT 1;\n\nDECLARE @total decimal(10, 2);\nSELECT @|', 'mssql');
  assert.equal(v[0].line, 3);
});

// ---------- PostgreSQL ----------

const PG = 'CREATE FUNCTION f(p_id int, OUT p_n text) RETURNS int AS $$ DECLARE v_count int := 0; BEGIN SELECT ';

test('a routine body lists its variables and parameters first', () => {
  const r = sug(PG + 'v_c|', 'postgres');
  assert.ok(r);
  assert.equal(r.items[0].label, 'v_count');
  assert.equal(r.items[0].type, 'variable');
  assert.equal(r.items[0].desc, 'int');
  const p = sug(PG + 'p_|', 'postgres');
  const params = p.items.filter(i => i.type === 'parameter');
  assert.deepEqual(params.map(i => i.label).sort(), ['p_id', 'p_n']);
  assert.equal(params.find(i => i.label === 'p_n').desc, 'text');
});

test('ALIAS FOR, CONSTANT and an unnamed parameter', () => {
  const v = vars('CREATE FUNCTION f(int, b text) RETURNS int AS $$ DECLARE a ALIAS FOR $1; k CONSTANT numeric(5, 1) := 2; BEGIN |', 'postgres');
  assert.deepEqual(v.map(x => x.name + ':' + x.type + ':' + x.kind), ['b:text:parameter', 'a:ALIAS FOR $1:variable', 'k:numeric(5, 1):variable']);
});

test('outside a body PostgreSQL lists no variables', () => {
  assert.deepEqual(vars('DECLARE v int; SELECT v|', 'postgres'), []);
  const r = sug('SELECT v_c|', 'postgres');
  assert.ok(!r || r.items.every(i => i.type !== 'variable'));
});

// ---------- Hover ----------

test('hover on @total shows its type and its line', () => {
  const sql = 'SELECT 1;\n\nDECLARE @total decimal(10, 2);\nSELECT @total;';
  const info = W.qhHoverInfo(sql, sql.lastIndexOf('@total') + 2, SCHEMA, 'mssql');
  assert.ok(info);
  assert.equal(info.kind, 'variable');
  assert.equal(info.name, '@total');
  assert.equal(info.type, 'decimal(10, 2)');
  assert.equal(info.decl, 'variable');
  assert.equal(info.line, 3);
  assert.deepEqual(W.qhHoverLines(info), ['@total  decimal(10, 2)', 'Variable, declared on line 3']);
  // The declaration itself has the card too.
  assert.equal(W.qhHoverInfo(sql, sql.indexOf('@total') + 2, SCHEMA, 'mssql').kind, 'variable');
  // An unknown name shows nothing.
  assert.equal(W.qhHoverInfo('SELECT @nope', 9, SCHEMA, 'mssql'), null);
});

test('hover on a body variable and a parameter in PostgreSQL', () => {
  const sql = 'CREATE FUNCTION f(p_id int) RETURNS int AS $$\nDECLARE\n  v_count int := 0;\nBEGIN\n  RETURN v_count + p_id;\nEND $$ LANGUAGE plpgsql;';
  const v = W.qhHoverInfo(sql, sql.lastIndexOf('v_count') + 1, SCHEMA, 'postgres');
  assert.ok(v);
  assert.equal(v.kind, 'variable');
  assert.equal(v.type, 'int');
  assert.equal(v.line, 3);
  assert.equal(sql.slice(v.start, v.end), 'v_count');
  const p = W.qhHoverInfo(sql, sql.lastIndexOf('p_id') + 1, SCHEMA, 'postgres');
  assert.equal(p.decl, 'parameter');
  assert.deepEqual(W.qhHoverLines(p), ['p_id  int', 'Parameter, declared on line 1']);
});

test('the list labels name the two kinds', () => {
  assert.equal(W.QH_AC_TYPE_LABEL.variable, 'var');
  assert.equal(W.QH_AC_TYPE_LABEL.parameter, 'param');
});
