// While the kill switch is on, every button that approves and runs is off
// (operator decision, 2026-10-24). The server refuses it too (409
// `kill_switch_on`). Deny and Request changes stay open.
//
//   * "Approve and run", "Approve N", "Approve all" and "Approve whole bundle"
//     are disabled and say "Kill switch is on. Resume queries first."
//   * With the switch off, they are live and have no title.
//   * The mock refuses the same way the server does.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { transformSync } from 'esbuild';

import { bareWindow, loadInto } from './_load.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }
const opts = JSDOM ? {} : { skip: 'jsdom not installed' };

const HERE = dirname(fileURLToPath(import.meta.url));
const SAY = 'Kill switch is on. Resume queries first.';
const SUPER = { name: 'Jordan Ray', role: 'super', isDba: true };

function row(id, extra) {
  return { id, submitter: { name: 'Jordan Ray', initials: 'JR', slackId: 'U0EXAMPLE001' },
    connectionId: 'prod-main', databaseId: 'orders', env: 'production', tier: 'RW',
    sql: 'UPDATE t SET a = 1 WHERE id = 2;', statements: 1, piiCols: [], estRows: 1,
    estTables: ['t'], reason: 'Fix one row.', submittedAt: '2026-10-23T08:00:00Z', ...extra };
}
const QUEUE = [
  row('q1'),
  row('q2', { bundleId: 'b1', bundlePosition: 1, bundleSize: 2 }),
  row('q3', { bundleId: 'b1', bundlePosition: 2, bundleSize: 2 }),
];

function mount(killSwitch) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>',
                        { pretendToBeVisual: true, url: 'https://localhost/#admin/approvals' });
  for (const k of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node', 'Event',
                   'KeyboardEvent', 'MouseEvent', 'getComputedStyle', 'requestAnimationFrame',
                   'cancelAnimationFrame', 'localStorage', 'location', 'history']) {
    Object.defineProperty(globalThis, k, { value: dom.window[k], configurable: true, writable: true });
  }
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const React = require_('react');
  const ReactDOM = require_('react-dom/client');
  globalThis.React = React;
  const win = dom.window;
  win.React = React;
  win.ReactDOM = require_('react-dom');
  loadInto(win, 'qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx', 'qh-admin-data.jsx', 'qh-admin-effective.jsx', 'qh-admin.jsx');
  const calls = [];
  const st = { queue: QUEUE, manualRuns: [], endpointReqs: [], autoRequests: [], grants: [], people: [], teams: [],
    killSwitch, killHistory: () => Promise.resolve([]), toggleKill: () => Promise.resolve({}),
    decide: (...a) => calls.push(['decide', ...a]),
    batchApprove: (...a) => calls.push(['batchApprove', ...a]),
    approveBundle: (...a) => calls.push(['approveBundle', ...a]) };
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(win.AdminPanel, { st, user: SUPER, modeEntry: null })));
  const doc = win.document;
  const click = (el) => React.act(() => el.dispatchEvent(new win.MouseEvent('click', { bubbles: true })));
  const button = (text) => [...doc.querySelectorAll('button')].find(b => b.textContent.trim() === text);
  return { win, doc, click, button, calls, React };
}

const ON = { enabled: true, message: 'Replica lag.', by: 'jordan.ray', at: '2026-10-23T08:00:00Z' };
const OFF = { enabled: false, message: '', by: null, at: null };

test('switch on: Approve and run is disabled with the reason, Deny and Request changes are live', opts, () => {
  const ui = mount(ON);
  const approve = ui.button('Approve and run');
  assert.ok(approve, 'the primary button is on the card');
  assert.equal(approve.disabled, true);
  assert.equal(approve.title, SAY);
  ui.click(approve);
  assert.deepEqual(ui.calls, [], 'a click on the disabled button does nothing');
  for (const name of ['Deny', 'Request changes']) {
    const b = ui.button(name);
    assert.ok(b, name);
    assert.equal(b.disabled, false, name);
  }
});

test('switch on: Approve all (batch head) and Approve whole bundle are disabled', opts, () => {
  const ui = mount(ON);
  const all = ui.button('Approve all');
  assert.equal(all.disabled, true);
  assert.equal(all.title, SAY);
  ui.click(all);
  // Select a row of the batch, so its detail card shows "Approve whole bundle".
  const inBatch = ui.doc.querySelector('.qh-qbundle .qh-qcard-main');
  assert.ok(inBatch, 'the batch has a row to select');
  ui.click(inBatch);
  const whole = ui.button('Approve whole bundle');
  assert.ok(whole, 'the bundle button shows on a batch row');
  assert.equal(whole.disabled, true);
  assert.equal(whole.title, SAY);
  ui.click(whole);
  assert.deepEqual(ui.calls, []);
});

test('switch on: Approve N is disabled once rows are selected', opts, () => {
  const ui = mount(ON);
  ui.click(ui.doc.querySelector('.qh-batchbar-all input'));
  const n = ui.button('Approve 3');
  assert.ok(n, 'Approve 3 shows after Select all');
  assert.equal(n.disabled, true);
  assert.equal(n.title, SAY);
  ui.click(n);
  assert.deepEqual(ui.calls, []);
  assert.equal(ui.button('Clear').disabled, false, 'Clear stays live');
});

test('switch off: every approve button is live and has no title', opts, () => {
  const ui = mount(OFF);
  const approve = ui.button('Approve and run');
  assert.equal(approve.disabled, false);
  assert.ok(!approve.title);
  const all = ui.button('Approve all');
  assert.equal(all.disabled, false);
  assert.ok(!all.title);
  ui.click(ui.doc.querySelector('.qh-qbundle .qh-qcard-main'));
  assert.equal(ui.button('Approve whole bundle').disabled, false);
  ui.click(ui.doc.querySelector('.qh-batchbar-all input'));
  const n = ui.button('Approve 3');
  assert.equal(n.disabled, false);
  ui.click(n);
  assert.equal(ui.calls[0][0], 'batchApprove');
});

test('a missing kill state counts as off, and the server has the last word', opts, () => {
  const ui = mount(undefined);
  assert.equal(ui.button('Approve and run').disabled, false);
});

// ---------- the mock ----------

async function mockApi() {
  const win = bareWindow();
  win.setTimeout = (fn) => { fn(); return 0; };
  win.clearTimeout = () => {};
  win.setInterval = () => 0;
  win.clearInterval = () => {};
  win.localStorage = {
    getItem: () => JSON.stringify({ handle: 'dana.kaur', role: 'super', at: 1 }),
    setItem: () => {}, removeItem: () => {},
  };
  loadInto(win, 'qh-data.jsx');
  const src = readFileSync(resolve(HERE, '..', '..', 'qh-api-mock.jsx'), 'utf8');
  const code = transformSync(src, {
    loader: 'jsx', jsx: 'transform',
    jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment',
  }).code;
  new Function('window', 'with (window) {\n' + code + '\n}')(win);
  return win.qhApi;
}

test('the mock refuses an approve with the switch on, and lets a deny through', async () => {
  const api = await mockApi();
  const { queue } = await api.adminQueue();
  assert.ok(queue.length >= 3, 'the mock queue has rows to decide');
  await api.adminKillSet({ enabled: true, message: 'test' });

  await assert.rejects(api.adminDecision(queue[0].id, { decision: 'approve' }),
    (e) => e.status === 409 && e.code === 'kill_switch_on' && e.message === SAY);
  await assert.rejects(api.adminBatchApprove([queue[0].id, queue[1].id]),
    (e) => e.status === 409 && e.code === 'kill_switch_on' && e.message === SAY);
  assert.equal((await api.adminQueue()).queue.length, queue.length, 'nothing left the queue');

  await api.adminDecision(queue[0].id, { decision: 'reject', note: 'Needs a WHERE clause.' });
  await api.adminDecision(queue[1].id, { decision: 'changes', note: 'Add a LIMIT to this one.' });
  assert.equal((await api.adminQueue()).queue.length, queue.length - 2);

  await api.adminKillSet({ enabled: false });
  await api.adminDecision(queue[2].id, { decision: 'approve' });
  assert.equal((await api.adminQueue()).queue.length, queue.length - 3);
});
