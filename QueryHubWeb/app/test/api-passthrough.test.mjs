// The API client must not decide which server fields exist.
//
// `qhApi.result()` cannot return the payload untouched: the grid calls
// `result.slice()` and `result.fetchPage()`, which are client-side closures. So
// the function builds an object — and it used to build it key by key, which
// silently drops anything the list does not name.
//
// That has now cost two fields. `colTypes` went first: the API sent it, the
// grid never received it, and the header tooltip kept guessing types from
// column names. The comment left behind named the trap without closing it, and
// `statements[]` fell into the same hole the week it was added. The asymmetry
// was the tell: `statementCount` arrived because it was listed one line below,
// `statements` did not because it was not.
//
// The rule this pins: the payload is spread FIRST and everything after it is a
// default on top, never the whole list of what survives.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';

/** Load qh-api.jsx with `fetch` answering one canned payload, then ask it for
 *  the result — i.e. exactly what the app does on completion. */
function resultFor(payload) {
  const win = bareWindow();
  win.fetch = async () => ({
    ok: true, status: 200,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
  return loadInto(win, 'qh-api.jsx').qhApi.result(1);
}

const TABLE = {
  kind: 'table', cols: ['id'], rows: [{ id: 1 }], total: 1,
  statement: 2, statementCount: 3,
  statements: [
    { n: 1, kind: 'SELECT', snippet: 'select 1 as asd' },
    { n: 2, kind: 'SELECT', snippet: 'select 2 as ddd' },
    { n: 3, kind: 'SELECT', snippet: 'select 3 as ddddAS' },
  ],
};

test('a field the client never heard of reaches the caller', async () => {
  const res = await resultFor({ ...TABLE, somethingAddedLater: 'kept' });
  assert.equal(res.somethingAddedLater, 'kept');
});

test('statements[] survives, with its labels', async () => {
  const res = await resultFor(TABLE);
  assert.deepEqual(res.statements.map(s => s.snippet),
    ['select 1 as asd', 'select 2 as ddd', 'select 3 as ddddAS']);
  // The pair that exposed the bug: one arrived, the other did not.
  assert.equal(res.statementCount, 3);
});

test('the client-side helpers are still there and still callable', async () => {
  const res = await resultFor(TABLE);
  assert.equal(typeof res.slice, 'function');
  assert.equal(typeof res.fetchPage, 'function');
  assert.deepEqual(res.slice(0, 1), [{ id: 1 }]);
});

test('a helper is not shadowed by a server field of the same name', async () => {
  // The payload is spread first, so anything the server sends called `slice`
  // must lose to the closure the grid actually calls.
  const res = await resultFor({ ...TABLE, slice: 'not a function' });
  assert.equal(typeof res.slice, 'function');
});

test('the defaults still apply on top of a sparse payload', async () => {
  const res = await resultFor({ kind: 'table' });
  assert.deepEqual(res.cols, []);
  assert.deepEqual(res.rows, []);
  assert.equal(res.colTypes, null);
  assert.equal(res.total, 0);
  assert.equal(res.statement, 1);
  assert.equal(res.statementCount, 1);
});

test('an affected-rows result is passed straight through', async () => {
  const res = await resultFor({ kind: 'affected', affected: 9, message: '9 row(s) affected.' });
  assert.equal(res.affected, 9);
});
