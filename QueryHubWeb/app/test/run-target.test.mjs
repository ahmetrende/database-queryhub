// What Run executes: the selection, the whole tab, or nothing.
//
// The bug this pins down: pressing Run with one line of a multi-statement
// script selected submitted the WHOLE script. Every SQL client runs the
// selection instead, and the fallback direction matters here more than in most
// apps — a query goes to a human approver and then to production, so "I
// highlighted one line and all four ran" is not a cosmetic surprise.
//
// The second case is the dangerous one and the reason qhRunTarget exists as a
// function rather than an `if`: a selection that is only comments has nothing to
// execute, and treating it as "no selection" would run the entire tab. The two
// intents must not collapse into each other.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';

const win = loadInto(bareWindow(), 'qh-data.jsx');
const qhRunTarget = win.qhRunTarget;

const SCRIPT = [
  '-- daily reconciliation',
  'SELECT count(*) FROM orders;',
  '-- UPDATE orders SET state = 1;   <- do not run this',
  'SELECT count(*) FROM shipments;',
].join('\n');

test('qhRunTarget is exported for both callers to share', () => {
  assert.equal(typeof qhRunTarget, 'function',
    'the rule must live in one place — Run, F5, ⌘↵ and F8 all read it');
});

test('a selection wins over the whole tab', () => {
  const one = 'SELECT count(*) FROM shipments;';
  assert.deepEqual(qhRunTarget(one, SCRIPT), { kind: 'selection', sql: one });
});

test('a selection spanning a comment and a statement still runs the selection', () => {
  const two = '-- daily reconciliation\nSELECT count(*) FROM orders;';
  assert.deepEqual(qhRunTarget(two, SCRIPT), { kind: 'selection', sql: two });
});

test('a comment-only selection runs NOTHING, not everything', () => {
  // Restore the old behaviour and this is the test that fails: `kind` comes
  // back 'all' with the entire four-statement script as the payload.
  for (const sel of ['-- UPDATE orders SET state = 1;',
                     '-- one\n-- two\n',
                     '/* nothing\n   here */',
                     '-- SELECT 1']) {
    assert.deepEqual(qhRunTarget(sel, SCRIPT), { kind: 'comments' },
      `a selection of ${JSON.stringify(sel)} must not fall back to the whole tab`);
  }
});

test('whitespace is not a selection', () => {
  // Trailing whitespace after a click is not an intent to run one thing.
  assert.deepEqual(qhRunTarget('   \n\t ', SCRIPT), { kind: 'all', sql: SCRIPT });
});

test('no selection runs the whole tab, as before', () => {
  assert.deepEqual(qhRunTarget('', SCRIPT), { kind: 'all', sql: SCRIPT });
  assert.deepEqual(qhRunTarget(null, SCRIPT), { kind: 'all', sql: SCRIPT });
  assert.deepEqual(qhRunTarget(undefined, SCRIPT), { kind: 'all', sql: SCRIPT });
});

test('an empty editor is nothing to run', () => {
  assert.deepEqual(qhRunTarget('', '   \n '), { kind: 'empty' });
});
