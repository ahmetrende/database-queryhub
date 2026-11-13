// Batch and fleet run page (M12c): one tab per bundle. It reads GET /bundles/{id}
// until no item waits or runs, counts the states, names every failure and lists
// every item. qh-app.jsx renders the whole app when it loads, so FleetRunPage's
// own source is cut out of the file and mounted for real (jsdom), with the real
// helpers from qh-data.jsx.
import test from 'node:test';
import assert from 'node:assert/strict';

import { page, sliceFunction, web, jsdomOpts as opts } from './_mount.mjs';

const ITEM = (n, state, over = {}) => ({
  queryId: String(100 + n), position: n, connectionId: 'example-' + n, databaseId: 'db' + n,
  state, status: { ok: 'done', failed: 'failed', not_run: 'rejected', running: 'running', waiting: 'pending' }[state],
  rowCount: state === 'ok' ? 3 : null, runMs: state === 'ok' ? 250 : null, detail: null, outcome: null, ...over,
});
const FINISHED = {
  bundleId: '7', kind: 'fleet', onFailure: 'stop', submittedAt: null, justification: 'Check the setting',
  sql: 'SELECT 1', items: [
    ITEM(1, 'ok'),
    ITEM(2, 'failed', { detail: 'permission denied for table ledgers' }),
    ITEM(3, 'not_run', { outcome: 'skipped', detail: 'Not run: example-2 failed first. The fleet run stops at the first failure.' }),
  ],
};

function mount(answers, props = {}) {
  const ui = page(['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx']);
  const w = ui.win;
  w.useState = ui.React.useState;
  w.useEffect = ui.React.useEffect;
  w.QH_BUNDLE_POLL_MS = 15;
  const asked = [];
  w.qhApi = {
    bundle: (id) => {
      asked.push(id);
      const a = answers[Math.min(asked.length, answers.length) - 1];
      return a instanceof Error ? Promise.reject(a) : Promise.resolve(a);
    },
  };
  const Page = sliceFunction('qh-app.jsx', 'FleetRunPage', w);
  const opened = [], kinds = [];
  ui.render(ui.React.createElement(Page, { bundleId: '7', onOpenItem: (i) => opened.push(i), onKind: (k) => kinds.push(k), ...props }));
  return { ...ui, asked, opened, kinds };
}
const wait = (ms) => new Promise(r => setTimeout(r, ms));
const btn = (ui, text) => ui.all('button').find(b => b.textContent.trim().startsWith(text));
const bodyRows = (ui) => ui.all('.qh-bpage-table tbody tr');

test('qhFleetSummary counts every state and names only the ones with items', opts, () => {
  const ui = page(['qh-data.jsx']);
  const s = ui.win.qhFleetSummary([{ state: 'ok' }, { state: 'ok' }, { state: 'failed' }, { state: 'not_run' }]);
  assert.deepEqual({ ...s.counts }, { ok: 2, failed: 1, not_run: 1, running: 0, waiting: 0 });
  assert.equal(s.line, '2 ok · 1 failed · 1 not run');
  assert.equal(s.done, true);
  // An unknown state from a newer server reads as waiting, so the page polls on.
  const t = ui.win.qhFleetSummary([{ state: 'ok' }, { state: 'paused' }]);
  assert.equal(t.counts.waiting, 1);
  assert.equal(t.done, false);
  assert.equal(ui.win.qhFleetSummary([]).done, true);
  assert.equal(ui.win.qhBundleTitle('fleet', '7'), 'Fleet run B#7');
  assert.equal(ui.win.qhBundleTitle('batch', '7'), 'Batch B#7');
  assert.equal(ui.win.qhBundleTitle(null, '7'), 'Bundle B#7');
});

test('a finished run with a failure names the failure first, with its error', opts, async () => {
  const ui = mount([FINISHED]);
  await ui.flush();
  assert.equal(ui.doc.querySelector('.qh-bpage-title').textContent, 'Fleet run B#7');
  assert.deepEqual(ui.kinds, ['fleet']);
  const status = ui.doc.querySelector('.qh-bpage-status');
  assert.equal(status.textContent, 'Finished: 1 ok · 1 failed · 1 not run.');
  assert.ok(status.classList.contains('is-bad'));
  const fails = ui.all('.qh-bpage-fail');
  assert.equal(fails.length, 1);
  assert.match(fails[0].textContent, /example-2 \/ db2/);
  assert.match(fails[0].textContent, /permission denied for table ledgers/);
  assert.match(ui.doc.querySelector('.qh-bpage-meta').textContent, /3 databases · Stops at the first failure/);
  assert.equal(ui.doc.querySelector('.qh-bpage-sql').textContent, 'SELECT 1');
  assert.equal(bodyRows(ui).length, 3);
  // Rows show only for an item that ran.
  assert.equal(bodyRows(ui)[0].querySelectorAll('td')[3].textContent, '3');
  assert.equal(bodyRows(ui)[1].querySelectorAll('td')[3].textContent, '');
  assert.match(bodyRows(ui)[2].textContent, /Not run: example-2 failed first/);
});

test('a count chip filters the table, and the failures list stays complete', opts, async () => {
  const ui = mount([FINISHED]);
  await ui.flush();
  ui.click(btn(ui, 'OK'));
  assert.equal(bodyRows(ui).length, 1);
  assert.equal(btn(ui, 'OK').getAttribute('aria-pressed'), 'true');
  assert.equal(ui.all('.qh-bpage-fail').length, 1);
  ui.click(btn(ui, 'OK'));
  assert.equal(bodyRows(ui).length, 3);
  ui.click(btn(ui, 'Not run'));
  assert.equal(bodyRows(ui).length, 1);
  ui.click(btn(ui, 'All'));
  assert.equal(bodyRows(ui).length, 3);
});

test('Open passes the item to the app', opts, async () => {
  const ui = mount([FINISHED]);
  await ui.flush();
  ui.click(ui.doc.querySelector('.qh-bpage-fail button'));
  ui.click(bodyRows(ui)[0].querySelector('button'));
  assert.deepEqual(ui.opened.map(i => i.queryId), ['102', '101']);
});

test('the page polls while an item waits or runs, and stops when all ended', opts, async () => {
  const waiting = { ...FINISHED, items: [ITEM(1, 'waiting'), ITEM(2, 'waiting')] };
  const running = { ...FINISHED, items: [ITEM(1, 'ok'), ITEM(2, 'running')] };
  const ended = { ...FINISHED, items: [ITEM(1, 'ok'), ITEM(2, 'ok')] };
  const ui = mount([waiting, running, ended]);
  await ui.flush();
  assert.equal(ui.doc.querySelector('.qh-bpage-status').textContent,
    'Waiting for approval. One approval covers all 2 databases.');
  for (let i = 0; i < 20 && ui.asked.length < 3; i++) { await wait(20); await ui.flush(); }
  assert.equal(ui.asked.length, 3);
  assert.equal(ui.doc.querySelector('.qh-bpage-status').textContent, 'Finished: all 2 databases ok.');
  await wait(60); await ui.flush();
  assert.equal(ui.asked.length, 3);
});

test('a running run counts what finished', opts, async () => {
  // The second answer ends the run, so the page stops polling and the test ends.
  const ui = mount([{ ...FINISHED, items: [ITEM(1, 'ok'), ITEM(2, 'running'), ITEM(3, 'waiting', { status: 'approved' })] }, FINISHED]);
  await ui.flush();
  assert.equal(ui.doc.querySelector('.qh-bpage-status').textContent,
    '1 of 3 finished. This page updates every 3 seconds.');
  for (let i = 0; i < 20 && ui.asked.length < 2; i++) { await wait(20); await ui.flush(); }
  assert.equal(ui.asked.length, 2);
});

test('a read error shows the server message and Try again reads again', opts, async () => {
  const e = new Error('No such bundle.');
  const ui = mount([e, FINISHED]);
  await ui.flush();
  assert.equal(ui.doc.querySelector('.qh-bpage-err span').textContent, 'No such bundle.');
  ui.click(btn(ui, 'Try again'));
  await ui.flush();
  assert.equal(ui.asked.length, 2);
  assert.equal(ui.doc.querySelector('.qh-bpage-err'), null);
  assert.equal(bodyRows(ui).length, 3);
});

test('the app opens a bundle tab after a fleet submit, and from History', () => {
  const src = web('qh-app.jsx');
  assert.match(src, /openBundle\(r\.bundleId, qhBundleTitle\('fleet', String\(r\.bundleId\)\)\)/);
  assert.match(src, /isBundle \? \(\s*<FleetRunPage key=\{tab\.bundleId\} bundleId=\{tab\.bundleId\} onOpenItem=\{openBundleItem\}/);
  assert.match(src, /onOpenBundle=\{\(id\) => openBundle\(id\)\}/);
  // The workspace keeps the bundle id, so a reload reopens the same page.
  assert.equal((src.match(/bundleId: t\.bundleId \|\| undefined/g) || []).length, 2);
  const panels = web('qh-panels.jsx');
  assert.match(panels, /h\.bundleId && onOpenBundle && \(/);
});

test('the modal has its own class names, and the admin grants block keeps its rules', () => {
  const src = web('qh-app.jsx');
  const start = src.indexOf('function FleetModal(');
  const end = src.indexOf('\n}\n', start);
  assert.equal(/qh-fleet-/.test(src.slice(start, end)), false);
  const html = web('QueryHub.html');
  // The modal's narrow confirm field once applied to the grants' revoke confirm.
  assert.equal(/\.qh-fleet-confirm\s*\{\s*max-width/.test(html), false);
  assert.match(html, /\.qh-fleetm-confirm \{ max-width: 120px; \}/);
});

test('the page has its own class names: .qh-bundle-chip belongs to the admin queue', () => {
  const src = web('qh-app.jsx');
  const start = src.indexOf('function FleetRunPage(');
  assert.equal(/qh-bundle/.test(src.slice(start, src.indexOf('\n}\n', start))), false);
  const html = web('QueryHub.html');
  assert.equal((html.match(/^\s*\.qh-bundle-chip \{/gm) || []).length, 1);
});
