// A handle is not a name, and a name is not a handle.
//
// `submitter.name` is whatever the identity source held: a display name for
// most people, and the bare `first.last` login wherever the profile carried
// none. The queue then read as a person on one row and as a login on the next.
// `qhPersonName` title-cases a HANDLE-SHAPED string and returns everything else
// untouched, which is what makes it safe to call on every person field.
//
// The interesting cases are the ones where it must do NOTHING: a real name, a
// bare Slack id, and above all a role-prefixed handle — `dba.amara` names a
// person, but `Dba Aylin` is a surname nobody has. Those resolve server-side
// against both people tables or stay the handle they are.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';

const win = loadInto(bareWindow(), 'qh-data.jsx');
const { qhPersonName, qhIsHandleName } = win;

test('the helpers are exported for every screen that prints a person', () => {
  assert.equal(typeof qhPersonName, 'function');
  assert.equal(typeof qhIsHandleName, 'function');
});

test('a handle becomes a name, in the tr locale', () => {
  // The dotless i is the whole reason the locale is named: `toUpperCase()`
  // gives `Ilker`, which is a different letter in Turkish.
  assert.equal(qhPersonName('ilker.sahin'), '\u0130lker Sahin');
  assert.equal(qhPersonName('mehmet_genc'), 'Mehmet Genc');
});

test('diacritics are never invented', () => {
  // `sahin` -> `Sahin`, never the dotted-s spelling. The missing letter cannot be recovered
  // from an ascii handle, and guessing it is the same failure as guessing the
  // name.
  assert.equal(qhPersonName('ilker.sahin'), '\u0130lker Sahin');
  assert.ok(!qhPersonName('ilker.sahin').includes('\u015e'));
});

test('a role-prefixed handle is refused', () => {
  for (const h of ['dba.amara', 'oncall.eli', 'svc.reporting', 'bot.deploy',
                   'job.nightly', 'auto.approver']) {
    assert.equal(qhPersonName(h), h, h + ' must not be rewritten');
    assert.equal(qhIsHandleName(h), false);
  }
});

test('a real display name is untouched', () => {
  // Written as escapes because the repo is ascii-only; the letters are the
  // point of the case, so they cannot be spelled away.
  for (const n of ['Alex Kim', '\u0130lke \u00c7am', 'Halil \u0130brahim Demir']) {
    assert.equal(qhPersonName(n), n);
    assert.equal(qhIsHandleName(n), false);
  }
});

test('a bare Slack id is untouched', () => {
  // No dot, no underscore: an id is not handle-shaped, and printing `U0example001`
  // would be an invented name over a real identifier.
  assert.equal(qhPersonName('U0EXAMPLE001'), 'U0EXAMPLE001');
  assert.equal(qhIsHandleName('U0EXAMPLE001'), false);
});

test('non-strings and empties pass straight through', () => {
  for (const v of [null, undefined, 0, '', '   ']) {
    assert.equal(qhPersonName(v), v);
  }
});

test('qhIsHandleName is true only when the name was derived', () => {
  // It decides whether the raw handle is still worth showing on `title`.
  assert.equal(qhIsHandleName('ilker.sahin'), true);
  assert.equal(qhIsHandleName('Alex Kim'), false);
});
