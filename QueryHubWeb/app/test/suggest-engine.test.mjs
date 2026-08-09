// Two gaps design reported on 2026-07-30, and the shapes they must not regress to.
//
// (H) No function was EVER suggested. The catalog did not scan routines at all,
//     so `count`, a stored procedure, and the operator's own helpers were all
//     invisible — and the pool was Postgres-shaped for every engine.
// (I) `sys.dm_` suggested nothing while the bare `dm_` worked. The system pool
//     stores QUALIFIED names, so after a dot the token is only the tail and the
//     stored string could never prefix-match it.
//
// Both are exercised through the real qhBuildSuggest, with the pools shaped the
// way routes_data now sends them.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';

const win = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');
const suggest = win.qhBuildSuggest;

// Shaped like the editorSchema qh-app builds: bare-name pools plus the maps that
// decide what gets INSERTED.
const SCHEMA = {
  tables: ['orders', 'order_items', 'dm_report'],
  columns: ['order_id', 'amount'],
  dbs: ['app'],
  tableCols: {},
  qualify: { orders: 'sales.orders', order_items: 'sales.order_items' },
  systemTables: ['sys.dm_exec_sessions', 'sys.dm_exec_requests', 'sys.objects',
                 'pg_catalog.pg_class'],
  functions: ['count_orders', 'order_total', 'dm_summary'],
  functionKind: { count_orders: 'function', order_total: 'function',
                  dm_summary: 'procedure' },
};

const at = (sql, engine = 'postgres') => suggest(sql, sql.length, SCHEMA, engine);
const labels = (r) => (r ? r.items.map(i => i.label) : []);
const pick = (r, label) => (r ? r.items.find(i => i.label === label) : undefined);

// ---------------------------------------------------------------------------
// (H) functions exist at all
// ---------------------------------------------------------------------------

test('a function is offered in a select list', () => {
  const r = at('select count_ord');
  assert.ok(labels(r).includes('count_orders'),
    'no routine was suggested — the pool the catalog now fills is not being read');
});

test('a function inserts itself called, with the caret inside the parens', () => {
  const it = pick(at('select order_tot'), 'order_total');
  assert.equal(it.text, 'order_total(',
    'accepting a function should leave you typing the argument, not the parens');
  assert.equal(it.type, 'function');
});

test('a table-valued function is offered after FROM', () => {
  // A routine can be the FROM entry, so table mode must not exclude the pool.
  assert.ok(labels(at('select * from dm_su')).includes('dm_summary'));
});

test('a column still outranks a same-prefixed function in a select list', () => {
  // Ordering matters more than membership here: adding a pool must not push the
  // thing people actually type out of a 12-item list.
  const l = labels(at('select order_'));
  assert.ok(l.indexOf('order_id') < l.indexOf('order_total'),
    `column should come first, got ${JSON.stringify(l.slice(0, 4))}`);
});

// ---------------------------------------------------------------------------
// (I) qualified system objects after a dot
// ---------------------------------------------------------------------------

test('sys.dm_ offers the dm_* system views', () => {
  const l = labels(at('select * from sys.dm_'));
  assert.ok(l.includes('sys.dm_exec_sessions'),
    'the reported bug: a dotted system qualifier matched nothing');
  assert.ok(l.includes('sys.dm_exec_requests'));
});

test('accepting one replaces the token only, never the qualifier', () => {
  // The doubled-schema bug, one dot to the left: the qualifier is already typed.
  const it = pick(at('select * from sys.dm_exec_s'), 'sys.dm_exec_sessions');
  assert.equal(it.text, 'dm_exec_sessions',
    'the insert re-qualified a name the user had already qualified');
});

test('a foreign qualifier is not offered inside sys.', () => {
  const l = labels(at('select * from sys.pg_cl'));
  assert.ok(!l.includes('pg_catalog.pg_class'),
    'pg_catalog.* is noise inside sys. — the qualifier must be respected');
});

test('the bare form keeps working', () => {
  // It always did; this is the half that must not break while fixing the other.
  assert.ok(labels(at('select * from dm_')).includes('dm_report'));
});

test('an unqualified system name is still reachable without a dot', () => {
  assert.ok(labels(at('select * from sys.ob')).includes('sys.objects'));
});

// ---------------------------------------------------------------------------
// engine dimension
// ---------------------------------------------------------------------------

test('quoting follows the engine, for functions too', () => {
  // MSSQL brackets what Postgres double-quotes. A pool that ignores the engine
  // produces SQL the target rejects.
  const S = { ...SCHEMA, functions: ['Order Total'], functionKind: {} };
  const pg = suggest('select Order T', 14, S, 'postgres');
  const ms = suggest('select Order T', 14, S, 'mssql');
  const g = (r) => (r.items.find(i => i.label === 'Order Total') || {}).text;
  assert.equal(g(pg), '"Order Total"(');
  assert.equal(g(ms), '[Order Total](');
});

test('an engine with no routines in the catalog simply offers none', () => {
  // A spec-only engine has no routine scan, so the pool arrives empty. That is a
  // real answer and must not throw.
  const S = { ...SCHEMA, functions: [], functionKind: {} };
  const r = suggest('select count_ord', 17, S, 'clickhouse');
  assert.equal(r, null, 'an empty pool with no other match should yield nothing');
});

test('a missing functions pool does not throw', () => {
  // Older payloads, and the design prototype's mock, have no `functions` key.
  const { functions, functionKind, ...S } = SCHEMA;
  assert.doesNotThrow(() => suggest('select ord', 10, S, 'postgres'));
});
