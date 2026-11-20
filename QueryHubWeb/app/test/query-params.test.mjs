// Query parameters (2026-11-20, W8 part A): the pure helpers and the editor.
//
//   * qhFindParams finds :name, ${name} and {{name}} in code only. Strings,
//     quoted names, comments and $tag$ bodies are skipped.
//   * `::int`, `:=`, `arr[1:n]`, `{x:UInt8}`, `a:b` and `t.col:x` are not
//     parameters: the `:` follows a name character or another `:`.
//   * Text values are quoted the way each engine wants.
//   * qhHighlight marks each parameter with tk-param.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';
import { web } from './_mount.mjs';

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');

const texts = (sql, engine = 'postgres') =>
  W.qhFindParams(sql, engine).map(p => sql.slice(p.start, p.end));

test('finds the three forms with the right ranges', () => {
  const sql = 'SELECT :id, ${from}, {{ to }}';
  const found = W.qhFindParams(sql, 'postgres');
  assert.deepEqual(found.map(p => [p.name, p.form]), [['id', ':'], ['from', '${'], ['to', '{{']]);
  assert.equal(found[0].start, sql.indexOf(':id'));
  assert.equal(found[0].end, sql.indexOf(':id') + 3);
  assert.equal(found[1].start, sql.indexOf('${from}'));
  assert.equal(found[1].end, sql.indexOf('${from}') + 7);
  assert.equal(found[2].start, sql.indexOf('{{ to }}'));
  assert.equal(found[2].end, sql.length);
  assert.deepEqual(texts(sql), [':id', '${from}', '{{ to }}']);
});

test('the same name twice is two hits and one name', () => {
  const found = W.qhFindParams('SELECT * FROM t WHERE a = :id OR b = :id', 'postgres');
  assert.equal(found.length, 2);
  assert.deepEqual(W.qhParamNames(found), ['id']);
  const mixed = W.qhFindParams('SELECT :b, ${a}, {{b}}, :a', 'postgres');
  assert.deepEqual(W.qhParamNames(mixed), ['b', 'a']);
});

test('a parameter after ( , = or a space counts, names keep their case', () => {
  assert.deepEqual(texts('SELECT f(:x), y=:Y_2,:z'), [':x', ':Y_2', ':z']);
  assert.deepEqual(W.qhFindParams('SELECT :Id', 'postgres')[0].name, 'Id');
});

test('these are not parameters', () => {
  const none = [
    'SELECT x::int',
    'SELECT v := 1',
    'SELECT arr[1:n]',
    "SELECT ':id'",
    "SELECT 'it''s :id'",
    'SELECT 1 -- :id',
    'SELECT /* :id */ 1',
    'SELECT "a:b"',
    'SELECT ":id"',
    'SELECT $$ :id $$',
    'SELECT $fn$ :id ${x} {{y}} $fn$',
    'SELECT a:b',
    'SELECT t.col:x',
    'SELECT (a):x',
    'SELECT arr[1]:x',
    'SELECT :1',
    'SELECT ${1x}',
    'SELECT {{ }}',
  ];
  for (const sql of none) assert.deepEqual(texts(sql), [], sql);
  // ClickHouse query parameters keep their own syntax.
  assert.deepEqual(texts('SELECT {x:UInt8}', 'clickhouse'), []);
  assert.deepEqual(texts("SELECT 'a\\' :id'", 'clickhouse'), []);
});

test('after an unclosed string or comment nothing more is found', () => {
  assert.deepEqual(texts("SELECT :a, ':b"), [':a']);
  assert.deepEqual(texts('SELECT :a /* :b'), [':a']);
});

test('SQL Server: [x :y] is a quoted name, $ is not a dollar quote', () => {
  assert.deepEqual(texts('SELECT [a:b], [x :y]', 'mssql'), []);
  // On other engines [ is a subscript, so a spaced :y is a parameter.
  assert.deepEqual(texts('SELECT arr[x :y]', 'postgres'), [':y']);
  // SQL Server has no $tag$ bodies.
  assert.deepEqual(texts('SELECT $$ :id $$', 'mssql'), [':id']);
});

test('text literals follow the engine', () => {
  assert.equal(W.qhParamLiteral("O'Brien", 'text', 'postgres'), "'O''Brien'");
  assert.equal(W.qhParamLiteral("O'Brien", 'text', 'athena'), "'O''Brien'");
  assert.equal(W.qhParamLiteral("O'Brien", 'text', 'mssql'), "N'O''Brien'");
  assert.equal(W.qhParamLiteral("O'Brien", 'text', 'clickhouse'), "'O\\'Brien'");
  assert.equal(W.qhParamLiteral('a\\b', 'text', 'clickhouse'), "'a\\\\b'");
  assert.equal(W.qhParamLiteral('a\\b', 'text', 'postgres'), "'a\\b'");
  assert.equal(W.qhParamLiteral('now()', 'sql', 'postgres'), 'now()');
  assert.equal(W.qhParamLiteral('', 'text', 'postgres'), "''");
});

test('guess mode: numbers, NULL, TRUE and FALSE are SQL', () => {
  for (const v of ['42', '-1.5', ' 7 ', 'null', 'NULL', 'TRUE', 'false']) {
    assert.equal(W.qhParamGuessMode(v), 'sql', v);
  }
  for (const v of ['abc', '2026-01-01', '', '1e5', '1.', "O'Brien"]) {
    assert.equal(W.qhParamGuessMode(v), 'text', v);
  }
});

test('apply replaces each range, a missing value keeps its text', () => {
  const sql = 'SELECT * FROM t WHERE id = :id AND name = ${who} AND x = :missing OR id = {{ id }}';
  const found = W.qhFindParams(sql, 'postgres');
  const values = { id: { v: '42', mode: 'sql' }, who: { v: "O'Brien", mode: 'text' } };
  assert.equal(W.qhApplyParams(sql, found, values, 'postgres'),
    "SELECT * FROM t WHERE id = 42 AND name = 'O''Brien' AND x = :missing OR id = 42");
  assert.equal(W.qhApplyParams(sql, found, values, 'mssql'),
    "SELECT * FROM t WHERE id = 42 AND name = N'O''Brien' AND x = :missing OR id = 42");
  // null `found` finds the parameters again. No mode uses the guess.
  assert.equal(W.qhApplyParams('SELECT :a, :b', null, { a: { v: '5' }, b: { v: 'x' } }, 'postgres'),
    "SELECT 5, 'x'");
});

test('preview uses NULL for a missing value', () => {
  const sql = 'SELECT :id, ${who}, :constructor';
  assert.equal(W.qhParamPreviewText(sql, { id: { v: '1', mode: 'sql' } }, 'postgres'),
    'SELECT 1, NULL, NULL');
  assert.equal(W.qhParamPreviewText(sql, null, 'postgres'), 'SELECT NULL, NULL, NULL');
  const plain = 'SELECT 1';
  assert.equal(W.qhParamPreviewText(plain, {}, 'postgres'), plain);
});

test('at most 200 parameters', () => {
  const sql = 'SELECT ' + Array.from({ length: 250 }, (_, k) => ':p' + k).join(', ');
  assert.equal(W.QH_PARAM_MAX, 200);
  assert.equal(W.qhFindParams(sql, 'postgres').length, 200);
});

test('highlight marks parameters, strings and comments keep their colour', () => {
  assert.ok(W.qhHighlight('SELECT :id').includes('<span class="tk-param">:id</span>'));
  assert.ok(W.qhHighlight('SELECT ${from}').includes('<span class="tk-param">${from}</span>'));
  assert.ok(W.qhHighlight('SELECT {{ to }}').includes('<span class="tk-param">{{ to }}</span>'));
  assert.ok(!W.qhHighlight("SELECT ':id'").includes('tk-param'));
  assert.ok(!W.qhHighlight('SELECT 1 -- :id').includes('tk-param'));
  assert.ok(!W.qhHighlight('SELECT x::int').includes('tk-param'));
  // The text around a parameter is unchanged.
  assert.equal(W.qhHighlight('foo = :id'), 'foo = <span class="tk-param">:id</span>');
});

test('the keyboard popup has the Ask-for-parameters box and the CSS exists', () => {
  const src = web('qh-editor.jsx');
  assert.ok(src.includes('askParams, onToggleAskParams }) {'));
  assert.ok(src.includes("{'Ask for :name, ${name} and {{name}} values at Run'}"));
  const html = web('QueryHub.html');
  for (const cls of ['.tk-param', '.qh-params-row', '.qh-params-name', '.qh-params-preview']) {
    assert.ok(html.includes(cls + ' {'), cls);
  }
});
