// Search-token completion and the confirmation-409 decision.
//
// Both are pure functions design asked to be covered here, and both are the
// kind that look obviously right and are not: one edits a string the user is
// mid-way through typing, the other decides whether a 409 means "answer this
// question" or "you already sent this" — and getting the second wrong re-sends
// a duplicate with confirmed:true and duplicates it for real.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';

const win = loadInto(bareWindow(), 'qh-data.jsx');
const { qhTagVocab, qhTokenSuggest, qhApplyToken, qhConfirmReasons } = win;

const CONNS = [
  { id: 'a', name: 'prod-main', engine: 'PostgreSQL 15', env: 'production',
    tags: { provider: 'aws', service: 'RDS', account: '4417' } },
  { id: 'b', name: 'prod-hw', engine: 'PostgreSQL 15', env: 'production',
    tags: { provider: 'huawei', service: 'ECS' } },
  { id: 'c', name: 'untagged', engine: 'PostgreSQL 15', env: 'staging' },
];

// ---- vocabulary -------------------------------------------------------------

test('the vocabulary comes from the connections in hand, not an endpoint', () => {
  const v = qhTagVocab(CONNS);
  assert.ok(v.keys.includes('provider'));
  assert.deepEqual(v.values.provider, ['aws', 'huawei']);
  assert.deepEqual(v.values.service, ['ECS', 'RDS']);
});

test('connection fields join the vocabulary so env: and engine: work for free', () => {
  const v = qhTagVocab(CONNS);
  assert.ok(v.values.env.includes('production'));
});

test('an untagged connection contributes nothing and breaks nothing', () => {
  assert.doesNotThrow(() => qhTagVocab([{ id: 'x', name: 'x' }]));
  assert.doesNotThrow(() => qhTagVocab(null));
});

// ---- key completion ---------------------------------------------------------

test('a bare fragment completes to a key with its colon', () => {
  const v = qhTagVocab(CONNS);
  const s = qhTokenSuggest('pro', v);
  assert.ok(s.some(x => x.insert === 'provider:'));
});

test('a key that is already complete is not offered back', () => {
  // Otherwise the popup sits there suggesting exactly what is on screen.
  const v = qhTagVocab(CONNS);
  assert.equal(qhTokenSuggest('provider', v).some(x => x.key === 'provider'), false);
});

test('an empty box suggests nothing', () => {
  assert.deepEqual(qhTokenSuggest('', qhTagVocab(CONNS)), []);
  assert.deepEqual(qhTokenSuggest('   ', qhTagVocab(CONNS)), []);
});

// ---- value completion -------------------------------------------------------

test('after the colon it completes values of THAT key only', () => {
  const v = qhTagVocab(CONNS);
  const s = qhTokenSuggest('provider:h', v);
  assert.deepEqual(s.map(x => x.value), ['huawei']);
});

test('a completed value ends with a space, so the next token can be typed', () => {
  const v = qhTagVocab(CONNS);
  assert.equal(qhTokenSuggest('provider:h', v)[0].insert, 'provider:huawei ');
});

test('an unknown key offers nothing rather than every value it has', () => {
  assert.deepEqual(qhTokenSuggest('nosuchkey:a', qhTagVocab(CONNS)), []);
});

test('the list is capped', () => {
  const many = Array.from({ length: 40 }, (_, i) => (
    { id: 'i' + i, name: 'n' + i, tags: { provider: 'p' + i } }));
  assert.ok(qhTokenSuggest('provider:p', qhTagVocab(many)).length <= 6);
});

// ---- applying a completion --------------------------------------------------

test('applying a token replaces only the word being typed', () => {
  assert.equal(qhApplyToken('provider:aws serv', 'service:ECS '),
               'provider:aws service:ECS ');
});

test('applying to an empty box just inserts', () => {
  assert.equal(qhApplyToken('', 'provider:'), 'provider:');
});

test('a trailing space means a NEW token, not an edit of the last one', () => {
  assert.equal(qhApplyToken('provider:aws ', 'service:'),
               'provider:aws service:');
});

// ---- the 409 decision -------------------------------------------------------

test('the distinct code is the first thing consulted', () => {
  assert.deepEqual(
    qhConfirmReasons({ status: 409, code: 'confirmation_required',
                       reasons: ['DROP TABLE users — every row is lost.'] }),
    ['DROP TABLE users — every row is lost.']);
});

test('a duplicate is not the question', () => {
  // The dangerous direction: answered as the question, it is re-sent with
  // confirmed:true and duplicated for real.
  assert.equal(
    qhConfirmReasons({ status: 409, code: 'conflict',
                       message: 'You already have an active request (#5).' }),
    null);
});

test('a non-409 is never the question', () => {
  assert.equal(qhConfirmReasons({ status: 403, code: 'forbidden' }), null);
  assert.equal(qhConfirmReasons(null), null);
});

test('the server now sends both, and the list wins over the joined message', () => {
  const r = qhConfirmReasons({
    status: 409, code: 'confirmation_required',
    message: 'a b', reasons: ['a', 'b'] });
  assert.deepEqual(r, ['a', 'b']);
});
