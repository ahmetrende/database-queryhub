// Which branch of the tree a relation belongs in.
//
// The bug: `GET /connections` listed every relation of a database — tables and
// views together — and dropped the kind. The tree drew that whole list under
// "Tables", then drew the views a SECOND time under "Views" once the lazy
// schema load answered. One real database read "Tables 41" for 2 tables and 39
// monitoring views, each of which also appeared below under Views.
//
// The refs still carry views — a view is as queryable as a table and
// autocomplete wants it — so the rule that separates them is the one thing
// that has to be right, and it is here rather than inside a React closure so
// that it can be.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';

const win = loadInto(bareWindow(), 'qh-data.jsx');
const { qhSplitRelations, qhIsViewRef } = win;

const REFS = [
  { s: 'public', n: 'orders', k: 'table' },
  { s: 'dba', n: 'blocking_sessions', k: 'view' },
  { s: 'public', n: 'daily_totals', k: 'matview' },
  { s: 'public', n: 'order_items', k: 'partitioned' },
];

test('the split is exported, so tree and search share one rule', () => {
  assert.equal(typeof qhSplitRelations, 'function');
  assert.equal(typeof qhIsViewRef, 'function');
});

test('a view goes to Views and nothing goes to both', () => {
  const { tables, views } = qhSplitRelations(REFS, []);
  assert.deepEqual(tables.map(r => r.n), ['orders', 'order_items']);
  assert.deepEqual(views.map(r => r.n), ['blocking_sessions', 'daily_totals']);
  const seen = new Set(tables.map(r => r.n));
  for (const v of views) assert.ok(!seen.has(v.n), v.n + ' is listed twice');
});

test('a materialized view is a view', () => {
  assert.ok(qhIsViewRef({ n: 'daily_totals', k: 'matview' }));
  assert.ok(!qhIsViewRef({ n: 'orders', k: 'table' }));
  assert.ok(!qhIsViewRef({ n: 'x', k: 'foreign' }));
});

test('the order the catalog sent survives the split', () => {
  const { tables } = qhSplitRelations(
    [{ n: 'b', k: 'table' }, { n: 'v', k: 'view' }, { n: 'a', k: 'table' }], []);
  assert.deepEqual(tables.map(r => r.n), ['b', 'a'],
    'the tree renders this list as-is; re-sorting it is a different screen');
});

test('without a kind, the loaded schema decides', () => {
  // An older payload, and the prototype's mock, send bare names only.
  const refs = [{ s: 'public', n: 'orders' }, { s: 'dba', n: 'index_info' }];
  const { tables, views } = qhSplitRelations(refs, ['index_info']);
  assert.deepEqual(tables.map(r => r.n), ['orders']);
  assert.deepEqual(views.map(r => r.n), ['index_info']);
});

test('before the schema loads, an unkinded ref stays a table', () => {
  // The alternative — guessing — would move rows between branches under the
  // reader as soon as the lazy load answered.
  const refs = [{ s: 'dba', n: 'index_info' }];
  assert.deepEqual(qhSplitRelations(refs, []).tables.map(r => r.n), ['index_info']);
});

test('a view that matched no ref is still listed', () => {
  // The mock keeps views in a separate generator, so they are never in `tables`.
  const { views } = qhSplitRelations([{ n: 'orders', k: 'table' }], ['v_daily']);
  assert.deepEqual(views, [{ s: null, n: 'v_daily' }]);
});

test('an empty database splits into two empty branches', () => {
  assert.deepEqual(qhSplitRelations([], []), { tables: [], views: [] });
  assert.deepEqual(qhSplitRelations(undefined, undefined), { tables: [], views: [] });
});
