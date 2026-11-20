// Group the rows of a page (2026-11-20, P2.11).
//
// qhGroupRows counts the rows that the grid already holds. These tests check
// the counts, the NULL group, masked values, three columns, the numeric
// column, the 200 cap and the sort order. The last test reads the panel's
// source in ResultsView.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import { bareWindow, loadInto } from './_load.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const T = loadInto(bareWindow(), 'qh-results-tools.jsx');
const COLS = ['city', 'kind', 'year', 'amount', 'email'];
const ROWS = [
  { city: 'Ankara', kind: 'a', year: 2024, amount: '10', email: 'j***@x.com' },
  { city: 'Izmir', kind: 'b', year: 2024, amount: '5', email: 'j***@x.com' },
  { city: 'Ankara', kind: 'a', year: 2025, amount: '2.5', email: 'k***@x.com' },
  { city: null, kind: 'a', year: 2025, amount: null, email: 'k***@x.com' },
  { city: 'Ankara', kind: 'b', year: 2025, amount: '-1', email: 'j***@x.com' },
  { city: undefined, kind: 'b', year: 2024, amount: 'n/a', email: 'j***@x.com' },
];

test('group: counts one column and gives the total and the group count', () => {
  const g = T.qhGroupRows(COLS, ROWS, ['city']);
  assert.equal(g.total, 6);
  assert.equal(g.distinct, 3);
  assert.deepEqual(g.groups.map(x => [x.key, x.count]), [
    [['Ankara'], 3], [['(null)'], 2], [['Izmir'], 1],
  ]);
});

test('group: null and undefined group as "(null)"', () => {
  const g = T.qhGroupRows(COLS, ROWS, ['city']);
  const n = g.groups.find(x => x.key[0] === T.QH_GROUP_NULL);
  assert.equal(T.QH_GROUP_NULL, '(null)');
  assert.equal(n.count, 2);
});

test('group: an empty string is NULL only in a typed column that is not text', () => {
  const rows = [{ n: '' }, { n: '' }, { n: '1' }];
  assert.deepEqual(T.qhGroupRows(['n'], rows, ['n'], null, { n: 'int4' }).groups[0].key, ['(null)']);
  assert.deepEqual(T.qhGroupRows(['n'], rows, ['n']).groups[0].key, ['']);
});

test('group: a masked value groups as its masked text', () => {
  const g = T.qhGroupRows(COLS, ROWS, ['email']);
  assert.deepEqual(g.groups.map(x => [x.key[0], x.count]), [['j***@x.com', 4], ['k***@x.com', 2]]);
});

test('group: three columns in pick order', () => {
  const g = T.qhGroupRows(COLS, ROWS, ['kind', 'city', 'year']);
  assert.equal(g.groups[0].key.length, 3);
  assert.deepEqual(g.groups[0].key, ['a', 'Ankara', '2024']);
  assert.equal(g.distinct, 6);
  // A fourth column, an unknown column and a duplicate are dropped.
  const h = T.qhGroupRows(COLS, ROWS, ['kind', 'nope', 'kind', 'city', 'year', 'email']);
  assert.equal(h.groups[0].key.length, 3);
});

test('group: a numeric column adds sum, min and max, and skips text and NULL', () => {
  const g = T.qhGroupRows(COLS, ROWS, ['city'], 'amount');
  const ank = g.groups.find(x => x.key[0] === 'Ankara');
  assert.equal(ank.sum, 11.5);
  assert.equal(ank.min, -1);
  assert.equal(ank.max, 10);
  const nul = g.groups.find(x => x.key[0] === '(null)');
  assert.equal(nul.sum, null);
  assert.equal(nul.min, null);
  assert.equal(nul.max, null);
  // Without a numeric column a group has only key and count.
  assert.deepEqual(Object.keys(T.qhGroupRows(COLS, ROWS, ['city']).groups[0]).sort(), ['count', 'key']);
});

test('group: the list stops at 200 groups, distinct counts them all', () => {
  const rows = [];
  for (let i = 0; i < 450; i++) rows.push({ id: String(i) });
  const g = T.qhGroupRows(['id'], rows, ['id']);
  assert.equal(T.QH_GROUP_MAX, 200);
  assert.equal(g.groups.length, 200);
  assert.equal(g.distinct, 450);
  assert.equal(g.total, 450);
});

test('group: sort by count, largest first, ties in first-seen order', () => {
  const rows = ['x', 'y', 'y', 'z', 'z', 'z', 'w'].map(v => ({ v }));
  const g = T.qhGroupRows(['v'], rows, ['v']);
  assert.deepEqual(g.groups.map(x => x.key[0]), ['z', 'y', 'x', 'w']);
});

test('group: array rows read by column position', () => {
  const g = T.qhGroupRows(['a', 'b'], [[1, 'p'], [2, 'p'], [3, 'q']], ['b']);
  assert.deepEqual(g.groups.map(x => [x.key[0], x.count]), [['p', 2], ['q', 1]]);
});

test('group: no column picked gives no groups', () => {
  assert.deepEqual(T.qhGroupRows(COLS, ROWS, []), { groups: [], total: 6, distinct: 0 });
});

test('group: ResultsView has the Group button and the page-only note', () => {
  const src = readFileSync(resolve(HERE, '..', 'src', 'qh-panels.jsx'), 'utf8');
  const view = src.slice(src.indexOf('function ResultsView('), src.indexOf('function StepsList('));
  assert.match(view, /qh-group-toggle/);
  assert.match(view, /qhGroupRows\(/);
  assert.match(view, /Groups the rows on this page only\./);
  assert.match(view, /rows on this page/);
});
