// The approve step on a schema change, mounted for real (jsdom).
//
//   * The first Approve on a DDL card opens a step. It does not approve.
//   * One input per distinct target. The confirm button stays disabled until
//     each input matches its target (trimmed, case-insensitive).
//   * An optional note goes with the decision.
//   * A card that is not DDL still approves in one click.
//   * A bundle with a DDL item gets the same step before "Approve whole bundle".
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { bareWindow, loadInto } from './_load.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }
const opts = JSDOM ? {} : { skip: 'jsdom not installed' };

const DBA = { name: 'Jordan Ray', role: 'dba', isDba: true };

const pure = loadInto(bareWindow(), 'qh-data.jsx', 'qh-admin-data.jsx', 'qh-admin.jsx');

// ---------- pure rules ----------

test('the targets: one per distinct connection, DDL rows only', () => {
  const { qhDdlTargets } = pure;
  assert.deepEqual(qhDdlTargets([
    { id: 1, tier: 'DDL', connectionId: 'prod-main' },
    { id: 2, tier: 'RW', connectionId: 'prod-side' },
    { id: 3, escalate: true, tier: 'RW', connectionId: 'prod-main' },
    { id: 4, tier: 'DDL', connectionId: 'prod-other' },
  ]), ['prod-main', 'prod-other']);
  assert.deepEqual(qhDdlTargets([{ id: 1, tier: 'RO', connectionId: 'x' }]), []);
  assert.deepEqual(qhDdlTargets(null), []);
});

test('the match ignores case and outer spaces, and needs every target', () => {
  const { qhDdlTargetsTyped, qhDdlTypeWhy } = pure;
  assert.equal(qhDdlTargetsTyped(['prod-main'], ['  PROD-Main ']), true);
  assert.equal(qhDdlTargetsTyped(['prod-main'], ['prod-mai']), false);
  assert.equal(qhDdlTargetsTyped(['a', 'b'], ['a', '']), false);
  assert.equal(qhDdlTargetsTyped(['a', 'b'], ['A', 'B']), true);
  assert.equal(qhDdlTargetsTyped(['a'], undefined), false);
  assert.equal(qhDdlTargetsTyped([], []), false, 'nothing to confirm is never a match');
  assert.equal(qhDdlTypeWhy(['a', 'b'], ['a', '']), 'Type "b" to confirm.');
  assert.equal(qhDdlTypeWhy(['a', 'b'], []), 'Type "a", "b" to confirm.');
  assert.equal(qhDdlTypeWhy(['a'], ['A']), null);
});

// ---------- mounted ----------

function mount(props) {
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
  for (const name of ['ConnectionsView', 'RolesView', 'GrantsView', 'AutoView', 'ScopesView', 'TeamsView',
                      'MaskingView', 'AuditView', 'MetricsView', 'FeedbackView', 'SystemConfigView']) {
    win[name] = () => React.createElement('div', { className: 'stub-' + name });
  }
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(win.AdminPanel, props)));
  const doc = win.document;
  const all = (sel) => [...doc.querySelectorAll(sel)];
  const click = (el) => React.act(() => el.dispatchEvent(new win.MouseEvent('click', { bubbles: true })));
  const type = (el, value) => {
    const proto = el.tagName === 'TEXTAREA' ? win.HTMLTextAreaElement.prototype : win.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    React.act(() => { setter.call(el, value); el.dispatchEvent(new win.Event('input', { bubbles: true })); });
  };
  const key = (k) => React.act(() => doc.body.dispatchEvent(new win.KeyboardEvent('keydown', { key: k, bubbles: true })));
  const btn = (re) => all('.qh-adetail-actions button, .qh-bundle-note button').find(b => re.test(b.textContent.trim()));
  return { win, doc, all, click, type, key, btn };
}

const BASE = { submitter: { name: 'Jordan Ray', initials: 'JR', slackId: 'U0EXAMPLE001' },
  databaseId: 'orders', env: 'production', statements: 1, piiCols: [], estRows: 1, estTables: ['t'],
  reason: 'Add one column.', submittedAt: '2026-11-19T08:00:00Z' };
const DDL = { ...BASE, id: 'q1', connectionId: 'prod-main', tier: 'DDL', escalate: true, sql: 'ALTER TABLE t ADD COLUMN b int;' };
const RW = { ...BASE, id: 'q2', connectionId: 'prod-main', tier: 'RW', sql: 'UPDATE t SET a = 1 WHERE id = 2;' };

function baseSt(extra) {
  return { queue: [DDL], manualRuns: [], endpointReqs: [], autoRequests: [], grants: [], people: [], teams: [],
    killSwitch: { enabled: false, message: '', by: null, at: null },
    killHistory: () => Promise.resolve([]), toggleKill: () => Promise.resolve({ enabled: true, cancelledCount: 0 }),
    decide: () => {}, batchApprove: () => {}, approveBundle: () => {}, ...extra };
}

test('a DDL card: the first Approve opens the step and decides nothing', opts, () => {
  const decided = [];
  const ui = mount({ st: baseSt({ decide: (...a) => decided.push(a) }), user: DBA, modeEntry: null });
  ui.click(ui.btn(/^Approve schema change$/));
  assert.equal(decided.length, 0);
  assert.ok(ui.doc.querySelector('.qh-approvestep'), 'the step opens');
  assert.match(ui.doc.querySelector('.qh-approvestep-warn').textContent,
    /^This approves schema-changing statements\. Read them, then retype each target to confirm\.$/);
  const inputs = ui.all('.qh-approvestep-in');
  assert.equal(inputs.length, 1);
  assert.equal(inputs[0].placeholder, 'prod-main');
  assert.equal(ui.doc.querySelector('label[for="qh-retype-0"]').textContent, 'Type "prod-main" to confirm');
  const confirm = ui.btn(/^Confirm schema change$/);
  assert.equal(confirm.disabled, true);
  assert.equal(confirm.title, 'Type "prod-main" to confirm.');
});

test('the confirm button enables only on a match, any case, and sends the note', opts, () => {
  const decided = [];
  const ui = mount({ st: baseSt({ decide: (...a) => decided.push(a) }), user: DBA, modeEntry: null });
  ui.click(ui.btn(/^Approve schema change$/));
  const input = ui.doc.querySelector('.qh-approvestep-in');
  ui.type(input, 'prod-mai');
  assert.equal(ui.btn(/^Confirm schema change$/).disabled, true);
  ui.click(ui.btn(/^Confirm schema change$/));
  assert.equal(decided.length, 0, 'a disabled confirm sends nothing');
  ui.type(input, '  PROD-MAIN ');
  assert.equal(ui.btn(/^Confirm schema change$/).disabled, false);
  ui.type(ui.doc.querySelector('#qh-approve-note'), '  Checked the WHERE clause.  ');
  ui.click(ui.btn(/^Confirm schema change$/));
  assert.deepEqual(decided.map(d => [d[0], d[1], d[3]]), [['q1', 'approve', 'Checked the WHERE clause.']]);
});

test('an empty note goes as no note', opts, () => {
  const decided = [];
  const ui = mount({ st: baseSt({ decide: (...a) => decided.push(a) }), user: DBA, modeEntry: null });
  ui.click(ui.btn(/^Approve schema change$/));
  ui.type(ui.doc.querySelector('.qh-approvestep-in'), 'prod-main');
  ui.click(ui.btn(/^Confirm schema change$/));
  assert.equal(decided.length, 1);
  assert.equal(decided[0][3], undefined);
});

test('Cancel closes the step, and a new step starts empty', opts, () => {
  const ui = mount({ st: baseSt(), user: DBA, modeEntry: null });
  ui.click(ui.btn(/^Approve schema change$/));
  ui.type(ui.doc.querySelector('.qh-approvestep-in'), 'prod-main');
  ui.click(ui.all('.qh-approvestep button').find(b => b.textContent === 'Cancel'));
  assert.ok(!ui.doc.querySelector('.qh-approvestep'));
  assert.ok(ui.btn(/^Approve schema change$/));
  ui.click(ui.btn(/^Approve schema change$/));
  assert.equal(ui.doc.querySelector('.qh-approvestep-in').value, '');
});

test('another card drops the typed values', opts, () => {
  const other = { ...DDL, id: 'q3', submitter: { name: 'Sam Lee', initials: 'SL', slackId: 'U0EXAMPLE003' } };
  const ui = mount({ st: baseSt({ queue: [DDL, other] }), user: DBA, modeEntry: null });
  ui.click(ui.btn(/^Approve schema change$/));
  ui.type(ui.doc.querySelector('.qh-approvestep-in'), 'prod-main');
  ui.click(ui.all('.qh-qcard-main')[1]);
  assert.ok(!ui.doc.querySelector('.qh-approvestep'));
});

test('a card that is not DDL still approves in one click', opts, () => {
  const decided = [];
  const ui = mount({ st: baseSt({ queue: [RW], decide: (...a) => decided.push(a) }), user: DBA, modeEntry: null });
  ui.click(ui.btn(/^Approve and run$/));
  assert.deepEqual(decided.map(d => [d[0], d[1], d[3]]), [['q2', 'approve', undefined]]);
  assert.ok(!ui.doc.querySelector('.qh-approvestep'));
});

test('the A key on a DDL card opens the step and decides nothing', opts, () => {
  const decided = [];
  const ui = mount({ st: baseSt({ decide: (...a) => decided.push(a) }), user: DBA, modeEntry: null });
  ui.key('a');
  assert.equal(decided.length, 0);
  assert.ok(ui.doc.querySelector('.qh-approvestep'));
  assert.ok(ui.btn(/^Confirm schema change$/));
});

test('the kill switch still wins: no step, no decision', opts, () => {
  const decided = [];
  const ui = mount({ st: baseSt({ killSwitch: { enabled: true, message: '', by: null, at: null },
    decide: (...a) => decided.push(a) }), user: DBA, modeEntry: null });
  const approve = ui.btn(/^Approve schema change$/);
  assert.equal(approve.disabled, true);
  ui.key('a');
  assert.ok(!ui.doc.querySelector('.qh-approvestep'));
  assert.equal(decided.length, 0);
});

test('a bundle with a DDL item: whole-bundle approve needs each DDL target typed', opts, () => {
  const bundled = [
    { ...RW, id: 'b1', bundleId: 'B7', bundlePosition: 1, bundleSize: 3, connectionId: 'prod-side' },
    { ...DDL, id: 'b2', bundleId: 'B7', bundlePosition: 2, bundleSize: 3, connectionId: 'prod-main' },
    { ...DDL, id: 'b3', bundleId: 'B7', bundlePosition: 3, bundleSize: 3, connectionId: 'prod-other' },
  ];
  const approved = [];
  const ui = mount({ st: baseSt({ queue: bundled, approveBundle: (...a) => approved.push(a) }), user: DBA, modeEntry: null });
  ui.click(ui.btn(/^Approve whole bundle$/));
  assert.equal(approved.length, 0);
  const inputs = ui.all('.qh-approvestep-in');
  assert.deepEqual(inputs.map(i => i.placeholder), ['prod-main', 'prod-other']);
  assert.ok(!ui.doc.querySelector('#qh-approve-note'), 'the bundle path carries no note');
  assert.equal(ui.btn(/^Confirm whole bundle$/).disabled, true);
  ui.type(inputs[0], 'prod-main');
  assert.equal(ui.btn(/^Confirm whole bundle$/).title, 'Type "prod-other" to confirm.');
  ui.type(ui.all('.qh-approvestep-in')[1], 'Prod-Other');
  ui.click(ui.btn(/^Confirm whole bundle$/));
  assert.deepEqual(approved.map(a => a[0]), ['B7']);
});

test('a bundle with no DDL item approves in one click', opts, () => {
  const bundled = [
    { ...RW, id: 'b1', bundleId: 'B8', bundlePosition: 1, bundleSize: 2 },
    { ...RW, id: 'b2', bundleId: 'B8', bundlePosition: 2, bundleSize: 2 },
  ];
  const approved = [];
  const ui = mount({ st: baseSt({ queue: bundled, approveBundle: (...a) => approved.push(a) }), user: DBA, modeEntry: null });
  ui.click(ui.btn(/^Approve whole bundle$/));
  assert.deepEqual(approved.map(a => a[0]), ['B8']);
  assert.ok(!ui.doc.querySelector('.qh-approvestep'));
});
