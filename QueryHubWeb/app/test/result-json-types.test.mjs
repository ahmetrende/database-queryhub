// Copy as JSON and XML with typed columns (2026-11-11).
//
// Web results come from the stored CSV file: every cell is text and a NULL is
// ''. With the column types, a number column gives a JSON number (its own text,
// so a bigint keeps every digit), a boolean column gives true or false, and an
// empty cell of a column that is not text gives null (XML: null="true"). A text
// column keeps '' as "". The JSON download uses the same rule; the Python test
// tests/test_result_json_types.py holds the same cases.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';
import { web } from './_mount.mjs';

const T = loadInto(bareWindow(), 'qh-data.jsx', 'qh-results-tools.jsx');

const KINDS = [
  ['int4', 'num'], ['int8', 'num'], ['numeric(10,2)', 'num'], ['float8', 'num'],
  ['double precision', 'num'], ['bigint', 'num'], ['decimal(18, 2)', 'num'],
  ['Nullable(Int64)', 'num'], ['UInt32', 'num'], ['Float64', 'num'],
  ['LowCardinality(Nullable(String))', 'text'], ['bool', 'bool'], ['boolean', 'bool'],
  ['bit', 'bool'], ['text', 'text'], ['varchar(20)', 'text'],
  ['character varying', 'text'], ['nvarchar', 'text'], ['String', 'text'],
  ['date', 'other'], ['timestamptz', 'other'], ['uuid', 'other'], ['jsonb', 'other'],
  ['int4[]', 'other'], ['Array(String)', 'other'], ['', 'text'], [null, 'text'],
];

test('the kind of each SQL type, the same table as the server test', () => {
  for (const [type, kind] of KINDS) assert.equal(T.qhResJsonKind(type), kind, String(type));
});

test('one cell: numbers keep their text, empties are null, text and masked values stay strings', () => {
  const cases = [
    ['12', 'num', '12'], ['-0.5', 'num', '-0.5'], ['1e5', 'num', '1e5'],
    ['9007199254740993', 'num', '9007199254740993'],
    ['', 'num', 'null'], ['1***', 'num', '"1***"'], ['NaN', 'num', '"NaN"'], ['007', 'num', '"007"'],
    ['t', 'bool', 'true'], ['False', 'bool', 'false'], ['1', 'bool', 'true'],
    ['', 'bool', 'null'], ['maybe', 'bool', '"maybe"'],
    ['', 'text', '""'], ['12', 'text', '"12"'],
    ['', 'other', 'null'], ['2026-01-01', 'other', '"2026-01-01"'],
  ];
  for (const [v, k, out] of cases) assert.equal(T.qhResJsonCell(v, k), out, v + ' ' + k);
  assert.equal(T.qhResJsonCell(null, 'text'), 'null');
  assert.equal(T.qhResJsonCell(3, 'text'), '3', 'a real number stays a number');
  assert.equal(T.qhResJsonCell(true, 'other'), 'true');
});

test('Copy as JSON with types', () => {
  const cols = ['id', 'amount', 'big', 'ok', 'd', 'name'];
  const types = { id: 'int4', amount: 'numeric(10,2)', big: 'int8', ok: 'bool', d: 'date', name: 'text' };
  const rows = [['1', '10.50', '9007199254740993', 't', '2026-01-01', 'a'], ['2', '', '', '', '', '']];
  const text = T.qhResCopyAs('json', cols, rows, types);
  assert.ok(text.includes('"big": 9007199254740993'), 'the bigint keeps every digit');
  assert.ok(text.includes('"amount": 10.50'));
  assert.deepEqual(JSON.parse(text), [
    { id: 1, amount: 10.5, big: 9007199254740993, ok: true, d: '2026-01-01', name: 'a' },
    { id: 2, amount: null, big: null, ok: null, d: null, name: '' },
  ]);
});

test('the layout is the two-space layout of JSON.stringify', () => {
  const rows = [['1', 'a'], ['2', null]];
  assert.equal(T.qhResCopyAs('json', ['id', 'note'], rows), JSON.stringify([{ id: '1', note: 'a' }, { id: '2', note: null }], null, 2));
  assert.equal(T.qhResCopyAs('json', ['id'], []), '[]');
  assert.equal(T.qhResCopyAs('json', [], [[]]), JSON.stringify([{}], null, 2));
});

test('without types every value stays a string, as before', () => {
  assert.deepEqual(JSON.parse(T.qhResCopyAs('json', ['a'], [['1'], ['']])), [{ a: '1' }, { a: '' }]);
});

test('Copy as XML marks an empty cell of a typed column as null', () => {
  const x = T.qhResCopyAs('xml', ['n', 's'], [['', '']], { n: 'int4', s: 'text' });
  assert.ok(x.includes('<field name="n" null="true"/>'));
  assert.ok(x.includes('<field name="s"></field>'));
});

test('the grid gives the copy the column types', () => {
  const src = web('qh-panels.jsx');
  assert.ok(src.includes('d.src.map(r => d.ci.map(i => r[cols[i]])), colTypeMap());'));
  assert.ok(src.includes('qhResColumnNames(ci.map(i => cols[i]), colTypeMap(), withTypes);'));
});
