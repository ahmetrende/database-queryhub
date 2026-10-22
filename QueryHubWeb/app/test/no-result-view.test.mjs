// A statement with no result set says so at the top left, and Messages opens.
//
// The panel used to centre a big green "0" over "0 row(s) affected". It read
// as a result, and for a read it used the wrong verb: a SELECT affects nothing.
// Now the panel states the fact where a grid would start, in words that fit the
// statement. The run finishes on the Messages tab, because Results has nothing
// to look at.
//
// The wording lives in `qhAffectedInfo` once. These tests pin the wording for
// each case and the tab choice. They also guard three places where a regression
// could hide. The component must use the helper. The completion path must pick
// the tab through it. The CSS must not centre the block again.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import { bareWindow, loadInto } from './_load.mjs';

const win = loadInto(bareWindow(), 'qh-data.jsx');
const { qhAffectedInfo, qhTabAfterResult, qhMockResult } = win;

const HERE = dirname(fileURLToPath(import.meta.url));
const web = (file) => readFileSync(resolve(HERE, '..', '..', file), 'utf8');

test('the helpers are exported for the panel and the app', () => {
  assert.equal(typeof qhAffectedInfo, 'function');
  assert.equal(typeof qhTabAfterResult, 'function');
});

test('a read with no result set says "No result" and does not say "affected"', () => {
  const info = qhAffectedInfo({ kind: 'affected', affected: 0, tier: 'RO' });
  assert.deepEqual(info, { title: 'No result', detail: 'The statement ran. It returned no rows.' });
});

test('a write that matched nothing says so and keeps the count', () => {
  assert.deepEqual(qhAffectedInfo({ kind: 'affected', affected: 0, tier: 'RW' }),
    { title: 'No result', detail: 'The statement ran. It affected 0 rows.' });
});

test('a write with no count says that the server reported none', () => {
  assert.deepEqual(qhAffectedInfo({ kind: 'affected', affected: null, tier: 'RW' }),
    { title: 'No result', detail: 'The statement ran. The server reported no row count.' });
});

test('a write that changed rows keeps the count in the title', () => {
  assert.equal(qhAffectedInfo({ kind: 'affected', affected: 1, tier: 'RW' }).title, '1 row affected');
  assert.equal(qhAffectedInfo({ kind: 'affected', affected: 5, tier: 'RW' }).title, '5 rows affected');
  assert.equal(qhAffectedInfo({ kind: 'affected', affected: 5, tier: 'RW' }).detail,
    'The statement returned no result set.');
});

test('a schema change says "Statement executed"', () => {
  assert.deepEqual(qhAffectedInfo({ kind: 'affected', affected: 0, tier: 'DDL' }),
    { title: 'Statement executed', detail: 'The statement returned no result set.' });
});

test('a server that sends no tier still gets a true sentence', () => {
  assert.deepEqual(qhAffectedInfo({ kind: 'affected', affected: 0 }),
    { title: 'No result', detail: 'The statement ran. It returned no rows.' });
  assert.equal(qhAffectedInfo({ kind: 'affected', affected: 3 }).title, '3 rows affected');
});

test('no case prints a zero as "affected" in the title', () => {
  for (const tier of ['RO', 'RW', 'DDL', undefined]) {
    for (const affected of [0, null, undefined]) {
      const { title } = qhAffectedInfo({ kind: 'affected', affected, tier });
      assert.ok(!/\b0\b.*affected/.test(title), `${tier}/${affected}: ${title}`);
    }
  }
});

test('Messages opens when there is no grid, and Results when there is', () => {
  assert.equal(qhTabAfterResult({ kind: 'affected', affected: 0 }), 'messages');
  assert.equal(qhTabAfterResult({ kind: 'affected', affected: 7, tier: 'RW' }), 'messages');
  assert.equal(qhTabAfterResult({ kind: 'table', cols: [], total: 0 }), 'results');
  assert.equal(qhTabAfterResult(null), 'results');
  assert.equal(qhTabAfterResult(undefined), 'results');
});

test('the mock result carries the tier, as the server does', () => {
  assert.equal(qhMockResult('UPDATE t SET a = 1', { tier: 'RW' }).tier, 'RW');
  assert.equal(qhMockResult('CREATE TABLE t (a int)', { tier: 'DDL' }).tier, 'DDL');
});

test('the mock keeps the tier when it rebuilds an affected result', () => {
  // The real client passes the payload through. The mock builds a new object, and
  // a field it does not name is gone. The prototype would then say "No result"
  // for every schema change.
  const line = web('qh-api-mock.jsx').split('\n').find(l => l.includes("kind: 'affected', affected: r.affected"));
  assert.ok(line, 'the mock no longer rebuilds an affected result: update this test');
  assert.match(line, /tier: r\.tier/);
});

test('the results panel words an affected result through the helper', () => {
  const src = web('qh-panels.jsx');
  const at = src.indexOf("if (result.kind === 'affected')");
  assert.ok(at > 0, 'the branch for a result with no grid is gone');
  const branch = src.slice(at, at + 400);
  assert.match(branch, /qhAffectedInfo\(result\)/);
  assert.match(branch, /className="qh-noresult"/);
  assert.ok(!src.includes('qh-affected'), 'the old centred block is back');
});

test('a finished run picks its tab through the helper', () => {
  const src = web('qh-app.jsx');
  const at = src.indexOf('const res = await qhApi.result(qid);');
  assert.ok(at > 0, 'the completion path moved: update this test');
  const next = src.slice(at, at + 420);
  assert.match(next, /setResTab\(qhTabAfterResult\(res\)\)/);
  assert.ok(!/setResTab\('results'\)/.test(next), 'the completion path forces Results again');
});

test('the CSS keeps the block at the top left', () => {
  const css = web('QueryHub.html');
  const rule = css.match(/\.qh-noresult\s*\{([^}]*)\}/);
  assert.ok(rule, 'no .qh-noresult rule');
  assert.match(rule[1], /align-items:\s*flex-start/);
  assert.ok(!/justify-content:\s*center/.test(rule[1]), 'the block is centred again');
  assert.ok(!/align-items:\s*center/.test(rule[1]), 'the block is centred again');
  assert.ok(!/height:\s*100%/.test(rule[1]), 'a full-height box invites centring');
  assert.ok(!css.includes('.qh-affected'), 'a rule for the old block remains');
});
