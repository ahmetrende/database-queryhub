// Pure helpers behind the fleet-run picker: glob, pair list, filter, tier rules.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';

const win = loadInto(bareWindow(), 'qh-data.jsx');
const {
  qhFleetGlob, qhFleetPairs, qhFleetFilter,
  qhFleetTierRank, qhFleetCovers, qhFleetDefaultOnFailure,
} = win;

test('the helpers are exported', () => {
  for (const f of [qhFleetGlob, qhFleetPairs, qhFleetFilter, qhFleetTierRank, qhFleetCovers, qhFleetDefaultOnFailure]) {
    assert.equal(typeof f, 'function');
  }
});

test('glob: star matches any run, also empty', () => {
  assert.equal(qhFleetGlob('exc-*', 'exc-orders'), true);
  assert.equal(qhFleetGlob('exc-*', 'exc-'), true);
  assert.equal(qhFleetGlob('exc-*', 'pass-orders'), false);
  assert.equal(qhFleetGlob('*-db', 'a-b-db'), true);
  assert.equal(qhFleetGlob('*-db', 'a-b-dbx'), false);
});

test('glob: question mark matches exactly one character', () => {
  assert.equal(qhFleetGlob('db?', 'db1'), true);
  assert.equal(qhFleetGlob('db?', 'db'), false);
  assert.equal(qhFleetGlob('db?', 'db12'), false);
});

test('glob: dot and dash are literal', () => {
  assert.equal(qhFleetGlob('a.b', 'a.b'), true);
  assert.equal(qhFleetGlob('a.b', 'axb'), false);
  assert.equal(qhFleetGlob('a-b', 'a-b'), true);
  assert.equal(qhFleetGlob('a+b', 'aab'), false);
  assert.equal(qhFleetGlob('a(b)', 'a(b)'), true);
});

test('glob: a blank or whitespace pattern matches everything', () => {
  assert.equal(qhFleetGlob('', 'anything'), true);
  assert.equal(qhFleetGlob('   ', 'anything'), true);
  assert.equal(qhFleetGlob(undefined, 'anything'), true);
  assert.equal(qhFleetGlob(' , ,', 'anything'), true);
});

test('glob: a comma list is OR, parts are trimmed', () => {
  assert.equal(qhFleetGlob('exc-*, pass-*', 'pass-ledger'), true);
  assert.equal(qhFleetGlob('exc-*, pass-*', 'exc-ledger'), true);
  assert.equal(qhFleetGlob('exc-*, pass-*', 'other-ledger'), false);
  assert.equal(qhFleetGlob('exc-*,,  ,pass-*', 'pass-x'), true);
});

test('glob: case-insensitive, whole text must match', () => {
  assert.equal(qhFleetGlob('EXC-*', 'exc-orders'), true);
  assert.equal(qhFleetGlob('exc', 'exc-orders'), false);
});

const CONNS = [
  { id: 'c2', name: 'pass-ledger', engine: 'postgres', env: 'prod', databases: [
    { id: 'ledger', name: 'ledger', tier: 'RW' }, { id: 'audit', name: 'audit', tier: 'RO' } ] },
  { id: 'c1', name: 'exc-orders', engine: 'postgres', env: 'prod', databases: [{ id: 'orders', name: 'orders', tier: 'DDL' }] },
  { id: 'c3', name: 'exc-dead', engine: 'postgres', env: 'prod', disabled: true, databases: [{ id: 'x', tier: 'RO' }] },
  { id: 'c4', name: 'exc-star', engine: 'postgres', env: 'prod', databases: '*' },
  { id: 'c5', name: 'sql-main', engine: 'mssql', env: 'stage', databases: [
    { id: 'main', name: 'main', tier: null }, { id: 'rep', name: 'rep', tier: '' }, { id: 'ok', name: 'ok', tier: 'RO' } ] },
  { id: 'c6', engine: 'mysql', databases: [{ id: 'd', tier: 'RO' }] },
];
const META = {
  c1: { tags: { provider: 'aws', service: 'billing' }, owners: [{ name: 'u1', displayName: 'Ada Test' }] },
  c2: { tags: { provider: 'huawei' }, owners: [{ name: 'bob' }] },
};

test('pairs: skips disabled, star, and no-tier entries', () => {
  const keys = qhFleetPairs(CONNS, META).map(p => p.key);
  assert.ok(!keys.some(k => k.startsWith('c3/')));
  assert.ok(!keys.some(k => k.startsWith('c4/')));
  assert.ok(!keys.includes('c5/main'));
  assert.ok(!keys.includes('c5/rep'));
  assert.ok(keys.includes('c5/ok'));
});

test('pairs: sorted by alias then database, key is connId/dbId', () => {
  const pairs = qhFleetPairs(CONNS, META);
  assert.deepEqual(pairs.map(p => p.key), ['c6/d', 'c1/orders', 'c2/audit', 'c2/ledger', 'c5/ok']);
});

test('pairs: fields come from the connection, tier from the database', () => {
  const p = qhFleetPairs(CONNS, META).find(x => x.key === 'c1/orders');
  assert.deepEqual(p, {
    key: 'c1/orders', connectionId: 'c1', databaseId: 'orders', alias: 'exc-orders',
    engine: 'postgres', env: 'prod', tier: 'DDL',
    tags: { provider: 'aws', service: 'billing' }, owners: ['Ada Test'],
  });
});

test('pairs: owner falls back to name, alias to id, missing meta to empty', () => {
  const pairs = qhFleetPairs(CONNS, META);
  assert.deepEqual(pairs.find(x => x.key === 'c2/ledger').owners, ['bob']);
  const noMeta = pairs.find(x => x.key === 'c6/d');
  assert.equal(noMeta.alias, 'c6');
  assert.equal(noMeta.env, '');
  assert.deepEqual(noMeta.tags, {});
  assert.deepEqual(noMeta.owners, []);
  assert.equal(qhFleetPairs(CONNS).length, pairs.length);
  assert.deepEqual(qhFleetPairs(undefined, undefined), []);
});

const PAIRS = qhFleetPairs(CONNS, META);
const keysOf = (f) => qhFleetFilter(PAIRS, f).map(p => p.key);

test('filter: empty fields do not filter', () => {
  assert.equal(qhFleetFilter(PAIRS, {}).length, PAIRS.length);
  assert.equal(qhFleetFilter(PAIRS, undefined).length, PAIRS.length);
  assert.equal(qhFleetFilter(PAIRS, { glob: '', engine: '', env: '', tag: '', owner: '' }).length, PAIRS.length);
});

test('filter: glob matches the alias or alias/database', () => {
  assert.deepEqual(keysOf({ glob: 'exc-*' }), ['c1/orders']);
  assert.deepEqual(keysOf({ glob: 'pass-ledger/a*' }), ['c2/audit']);
  assert.deepEqual(keysOf({ glob: 'exc-*, sql-*' }), ['c1/orders', 'c5/ok']);
});

test('filter: engine and env are exact', () => {
  assert.deepEqual(keysOf({ engine: 'mssql' }), ['c5/ok']);
  assert.deepEqual(keysOf({ env: 'stage' }), ['c5/ok']);
  assert.deepEqual(keysOf({ engine: 'postgres', env: 'stage' }), []);
});

test('filter: tag key=value and bare value', () => {
  assert.deepEqual(keysOf({ tag: 'provider=AWS' }), ['c1/orders']);
  assert.deepEqual(keysOf({ tag: 'service=aws' }), []);
  assert.deepEqual(keysOf({ tag: 'billing' }), ['c1/orders']);
  assert.deepEqual(keysOf({ tag: 'HUAWEI' }), ['c2/audit', 'c2/ledger']);
  assert.deepEqual(keysOf({ tag: 'nothing' }), []);
});

test('filter: owner is case-insensitive', () => {
  assert.deepEqual(keysOf({ owner: 'ada test' }), ['c1/orders']);
  assert.deepEqual(keysOf({ owner: 'BOB' }), ['c2/audit', 'c2/ledger']);
  assert.deepEqual(keysOf({ owner: 'zed' }), []);
});

test('filter: fields combine with AND', () => {
  assert.deepEqual(keysOf({ glob: 'pass-*', owner: 'bob', tag: 'provider=huawei' }), ['c2/audit', 'c2/ledger']);
  assert.deepEqual(keysOf({ glob: 'pass-*', owner: 'ada test' }), []);
});

test('tier rank is 0, 1, 2 and -1 otherwise', () => {
  assert.equal(qhFleetTierRank('RO'), 0);
  assert.equal(qhFleetTierRank('rw'), 1);
  assert.equal(qhFleetTierRank('Ddl'), 2);
  assert.equal(qhFleetTierRank('admin'), -1);
  assert.equal(qhFleetTierRank(''), -1);
  assert.equal(qhFleetTierRank(null), -1);
});

test('covers: held tier must reach the needed tier', () => {
  assert.equal(qhFleetCovers({ tier: 'RW' }, 'RO'), true);
  assert.equal(qhFleetCovers({ tier: 'RW' }, 'rw'), true);
  assert.equal(qhFleetCovers({ tier: 'RO' }, 'RW'), false);
  assert.equal(qhFleetCovers({ tier: 'DDL' }, 'RW'), true);
  assert.equal(qhFleetCovers({ tier: 'RW' }, 'DDL'), false);
  assert.equal(qhFleetCovers({ tier: '' }, 'RO'), false);
  assert.equal(qhFleetCovers({ tier: 'RW' }, 'bogus'), false);
  assert.equal(qhFleetCovers(null, 'RO'), false);
});

test('default on-failure: continue for RO, stop otherwise', () => {
  assert.equal(qhFleetDefaultOnFailure('RO'), 'continue');
  assert.equal(qhFleetDefaultOnFailure('ro'), 'continue');
  assert.equal(qhFleetDefaultOnFailure('RW'), 'stop');
  assert.equal(qhFleetDefaultOnFailure('DDL'), 'stop');
  assert.equal(qhFleetDefaultOnFailure(undefined), 'stop');
});
