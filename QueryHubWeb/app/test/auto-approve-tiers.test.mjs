// The auto-approve form offers "Reads and writes" only while the server offers it.
//
// The server has a switch (`auto_approve_rw_requests`, off by default). While it
// is off, a request for writes answers 403. GET /auto-approve-requests says what
// is on offer in `tiers`: ["RO"], or ["RO", "RW"]. The form used to enable the
// button for everybody who holds RW, so it led them to an error. Now the form
// reads `tiers` and says why the choice is not there.
//
// A server that sends no `tiers` keeps the old behaviour: the choice is on offer.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import { bareWindow, loadInto } from './_load.mjs';

const win = loadInto(bareWindow(), 'qh-data.jsx');
const { qhAutoTierState } = win;

const HERE = dirname(fileURLToPath(import.meta.url));
const web = (file) => readFileSync(resolve(HERE, '..', '..', file), 'utf8');

const OFF = 'Auto-approve for writes is not available. You can ask for reads only.';
const READ_ONLY = 'You hold read-only here, so only reads can be auto-approved.';

test('the helper is exported for the form', () => {
  assert.equal(typeof qhAutoTierState, 'function');
});

test('while the server offers RW, a person who holds RW can pick it', () => {
  assert.deepEqual(qhAutoTierState(['RO', 'RW'], 'RW'), { rwOk: true, reason: null });
  assert.deepEqual(qhAutoTierState(['RO', 'RW'], 'DDL'), { rwOk: true, reason: null });
  assert.deepEqual(qhAutoTierState(['RO', 'RW'], null), { rwOk: true, reason: null });
});

test('while the server does not offer RW, nobody can pick it, and the reason says so', () => {
  assert.deepEqual(qhAutoTierState(['RO'], 'RW'), { rwOk: false, reason: OFF });
  assert.deepEqual(qhAutoTierState(['RO'], 'DDL'), { rwOk: false, reason: OFF });
  assert.deepEqual(qhAutoTierState(['RO'], null), { rwOk: false, reason: OFF });
});

test('a person who holds read-only keeps the old reason while RW is on offer', () => {
  assert.deepEqual(qhAutoTierState(['RO', 'RW'], 'RO'), { rwOk: false, reason: READ_ONLY });
});

test('a server that sends no tiers keeps the old behaviour', () => {
  for (const tiers of [undefined, null, 'RW', {}]) {
    assert.deepEqual(qhAutoTierState(tiers, 'RW'), { rwOk: true, reason: null });
    assert.deepEqual(qhAutoTierState(tiers, 'RO'), { rwOk: false, reason: READ_ONLY });
  }
});

test('the form reads tiers from the server answer and uses the helper', () => {
  const src = web('qh-panels.jsx');
  assert.match(src, /setTiers\(r\.tiers\)/);
  assert.match(src, /qhAutoTierState\(tiers, held\)/);
  assert.doesNotMatch(src, /const rwOk = !held \|\| held === 'RW'/);
});

test('the mock answers tiers like the server does', () => {
  assert.match(web('qh-api-mock.jsx'), /tiers: \['RO', 'RW'\]/);
});
