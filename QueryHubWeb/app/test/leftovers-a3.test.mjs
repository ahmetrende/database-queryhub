// Leftovers batch A3 (2026-10-24): Withdraw on a waiting auto-approve ask, and an
// optional reason on the two revoke rows.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { transformSync } from 'esbuild';

import { bareWindow, loadInto } from './_load.mjs';
import { page, web, jsdomOpts as opts } from './_mount.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const pure = loadInto(bareWindow(), 'qh-data.jsx');

// ---------- Withdraw ----------

const ASK = { id: 41, connectionId: 'prod-main', databaseId: 'users', tier: 'RO', windowMinutes: 10080, days: 7,
  reason: 'Weekly lookups.', status: 'pending', createdAt: new Date(Date.now() - 95 * 60000).toISOString() };
const CONNS = [{ id: 'prod-main', name: 'prod-main', databases: [{ id: 'users', name: 'users', tier: 'RO' }] }];
const OPTS = [{ minutes: 1440, label: '1 day', days: 1 }, { minutes: 10080, label: '7 days', days: 7 }];

function mountModal(state, withdrawImpl) {
  const ui = page(['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx']);
  const calls = [];
  ui.win.qhApi = { autoApproveRequestWithdraw: (id) => { calls.push(id); return withdrawImpl(id, state); } };
  ui.render(ui.React.createElement(ui.win.RequestAutoApproveModal, {
    conns: CONNS, onClose: () => {}, onSubmit: () => Promise.resolve(),
    load: () => Promise.resolve({ windowOptions: OPTS, tiers: ['RO'], requests: state.requests }) }));
  return { ui, calls };
}
const choose = (ui, sel, v) => ui.React.act(() => {
  const set = Object.getOwnPropertyDescriptor(ui.win.HTMLSelectElement.prototype, 'value').set;
  set.call(sel, v); sel.dispatchEvent(new ui.win.Event('change', { bubbles: true }));
});
const pick = async (ui) => {
  await ui.flush();
  choose(ui, ui.all('select.qh-input')[0], 'prod-main');
  choose(ui, ui.all('select.qh-input')[1], 'users');
};
const withdrawBtn = (ui) => ui.all('.qh-req-wait button').find(b => b.textContent === 'Withdraw');

test('Withdraw shows only while an ask is pending', opts, async () => {
  const none = mountModal({ requests: [{ ...ASK, status: 'withdrawn' }] }, () => Promise.resolve({}));
  await pick(none.ui);
  assert.ok(!withdrawBtn(none.ui), 'a withdrawn ask has no button');
  const wait = mountModal({ requests: [ASK] }, () => Promise.resolve({}));
  await pick(wait.ui);
  assert.ok(withdrawBtn(wait.ui), 'a pending ask has the button');
});

test('a click calls the method with the id and the dialog stops warning', opts, async () => {
  const state = { requests: [ASK] };
  const { ui, calls } = mountModal(state, (id, s) => { s.requests = [{ ...ASK, status: 'withdrawn' }]; return Promise.resolve({ id, status: 'withdrawn' }); });
  await pick(ui);
  ui.click(withdrawBtn(ui));
  await ui.flush();
  assert.deepEqual(calls, [41]);
  assert.ok(!ui.doc.querySelector('.qh-req-wait'), 'no waiting line');
  assert.match(ui.doc.querySelector('[role=status]').textContent, /^Withdrawn\.$/);
});

test('a 409 shows the server message', opts, async () => {
  const e = Object.assign(new Error('A DBA already decided this request.'), { status: 409, code: 'already_decided' });
  const { ui } = mountModal({ requests: [ASK] }, () => Promise.reject(e));
  await pick(ui);
  ui.click(withdrawBtn(ui));
  await ui.flush();
  assert.match(ui.doc.querySelector('.qh-req-warn').textContent, /already decided/);
  assert.ok(ui.doc.querySelector('.qh-req-wait'), 'still listed as waiting');
});

test('a withdrawn ask is not waiting', () => {
  assert.equal(pure.qhAskWaiting({ ...ASK, status: 'withdrawn' }), false);
  assert.equal(pure.qhAskFor([{ ...ASK, status: 'withdrawn' }], 'prod-main', 'users'), null);
});

function loadMock(handle) {
  const win = bareWindow();
  win.localStorage = { getItem: () => JSON.stringify({ handle }), setItem: () => {}, removeItem: () => {} };
  win.setInterval = () => 0;
  win.clearInterval = () => {};
  loadInto(win, 'qh-data.jsx');
  const src = readFileSync(resolve(HERE, '..', '..', 'qh-api-mock.jsx'), 'utf8');
  const code = transformSync(src, { loader: 'jsx', jsx: 'transform', jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment' }).code;
  new Function('window', 'with (window) {\n' + code + '\n}')(win);
  return win.qhApi;
}

test('mock withdraw: 200 with an integer id, then 409, and 404 for an unknown id', async () => {
  const api = loadMock('ben.donnelly');
  const ok = await api.autoApproveRequestWithdraw('ar_10');
  assert.deepEqual(ok, { id: 10, status: 'withdrawn' });
  const list = await api.autoApproveRequests();
  assert.equal(list.requests.find(r => r.id === 'ar_10').status, 'withdrawn', 'the list keeps the row');
  await assert.rejects(api.autoApproveRequestWithdraw('ar_10'), (e) => e.status === 409 && e.code === 'already_decided');
  await assert.rejects(api.autoApproveRequestWithdraw('ar_nope'), (e) => e.status === 404 && e.code === 'not_found');
});

test('the real client posts to the withdraw route and sends a body only for a reason', () => {
  const src = web('qh-api.jsx');
  assert.match(src, /autoApproveRequestWithdraw:\(id\) => qhFetch\('\/auto-approve-requests\/' \+ encodeURIComponent\(id\) \+ '\/withdraw', \{ method: 'POST' \}\)/);
  const win = bareWindow();
  loadInto(win, 'qh-api.jsx');
  assert.deepEqual(win.qhRevokeBody(), {});
  assert.deepEqual(win.qhRevokeBody({ reason: '   ' }), {});
  assert.deepEqual(win.qhRevokeBody({ reason: ' left the team ' }), { body: '{"reason":"left the team"}' });
});

// ---------- the reason on revoke ----------

const type = (ui, input, value) => ui.React.act(() => {
  const set = Object.getOwnPropertyDescriptor(ui.win.HTMLInputElement.prototype, 'value').set;
  set.call(input, value);
  input.dispatchEvent(new ui.win.Event('input', { bubbles: true }));
});
const btn = (ui, root, text) => [...root.querySelectorAll('button')].find(b => b.textContent.trim() === text);
const ACC = ['qh-data.jsx', 'qh-modal.jsx', 'qh-panels.jsx', 'qh-admin-data.jsx', 'qh-admin-access.jsx', 'qh-admin-person.jsx'];

function mountGrants(revoked) {
  const ui = page(ACC);
  ui.win.qhApi = { adminFleetGrants: () => Promise.resolve({ grants: [] }) };
  const g = { id: 'u:x:101', subjectType: 'user', subject: 'U0EXAMPLE101', subjectName: 'Jordan Ray', connectionId: 'prod-main',
    databases: ['*'], tier: 'RO', expiresAt: null, grantedBy: null, grantedByName: null, grantedAt: new Date().toISOString() };
  const st = { connections: [], people: [], teams: [], grants: [g], autoGrants: [], autoRequests: [], scopes: [],
    resolvePerson: () => Promise.resolve(null), revokeAutoGrant() {}, decideAutoRequest() {}, updateAutoGrant() {}, addAutoGrant() {},
    updateGrant() {}, setSubjectGrants() {}, removeScope() {}, extendGrant() {},
    revokeGrant: (...a) => { revoked.push(a); } };
  ui.render(ui.React.createElement(ui.win.GrantsView, { st, user: { name: 'Example Admin', role: 'super' } }));
  ui.click(ui.all('button').find(b => b.textContent.trim() === 'By grant'));
  ui.click(ui.all('button.qh-revoke')[0]);
  return ui;
}

test('grants row: the reason input has maxLength 500 and the label', opts, () => {
  const ui = mountGrants([]);
  const input = ui.doc.querySelector('.qh-revokeconfirm input');
  assert.equal(input.maxLength, 500);
  assert.equal(input.placeholder, 'Reason (optional)');
  assert.ok(input.getAttribute('aria-label'));
});

test('grants row: a typed reason reaches revokeGrant, an empty one sends none', opts, () => {
  const revoked = [];
  const ui = mountGrants(revoked);
  const row = () => ui.doc.querySelector('.qh-revokeconfirm');
  type(ui, row().querySelector('input'), '  left the team ');
  ui.click(btn(ui, row(), 'Revoke access'));
  assert.equal(revoked.length, 1);
  assert.equal(revoked[0][0], 'u:x:101');
  assert.deepEqual(revoked[0][2], { reason: 'left the team' });
  ui.click(ui.all('button.qh-revoke')[0]);
  ui.click(btn(ui, row(), 'Revoke access'));
  assert.equal(revoked[1][2], undefined, 'no reason, no option');
});

test('grants row: Keep access clears the reason', opts, () => {
  const ui = mountGrants([]);
  const row = () => ui.doc.querySelector('.qh-revokeconfirm');
  type(ui, row().querySelector('input'), 'draft');
  ui.click(btn(ui, row(), 'Keep access'));
  assert.ok(!row());
  ui.click(ui.all('button.qh-revoke')[0]);
  assert.equal(row().querySelector('input').value, '');
});

function mountFleet(revoked) {
  const ui = page(ACC);
  const grants = [{ id: 'f1', subjectType: 'user', subject: 'U0EXAMPLE001', subjectName: 'Jordan Ray', tier: 'ro', autoApprove: false, expiresAt: null, reason: 'Support rota' }];
  ui.win.qhApi = { adminFleetGrants: () => Promise.resolve({ grants }) };
  ui.render(ui.React.createElement(ui.win.FleetGrantsBlock, { canWrite: true, st: { people: [{ id: 'U0EXAMPLE001', handle: 'jordan.ray', name: 'Jordan Ray' }], teams: [],
    revokeFleetGrant: (...a) => { revoked.push(a); return Promise.resolve({}); } } }));
  return ui;
}

test('fleet row: maxLength 500, reason passed on Revoke access, none when empty, Keep access clears', opts, async () => {
  const revoked = [];
  const ui = mountFleet(revoked);
  await ui.flush();
  const row = () => ui.doc.querySelector('.qh-fleet-confirm');
  ui.click(ui.all('.qh-fleet-row .qh-revoke')[0]);
  assert.equal(row().querySelector('input').maxLength, 500);
  type(ui, row().querySelector('input'), 'draft');
  ui.click(btn(ui, row(), 'Keep access'));
  ui.click(ui.all('.qh-fleet-row .qh-revoke')[0]);
  assert.equal(row().querySelector('input').value, '', 'cleared');
  type(ui, row().querySelector('input'), 'rota ended');
  ui.click(btn(ui, row(), 'Revoke access'));
  await ui.flush();
  assert.deepEqual(revoked[0], ['f1', 'Jordan Ray', { reason: 'rota ended' }]);
  ui.click(ui.all('.qh-fleet-row .qh-revoke')[0]);
  ui.click(btn(ui, row(), 'Revoke access'));
  await ui.flush();
  assert.equal(revoked[1][2], undefined);
});

test('mock revoke methods put the reason in the audit text', () => {
  const mock = web('qh-api-mock.jsx');
  assert.match(mock, /adminDelGrant: \(id, o\)[\s\S]*?\(why \? ' · reason: ' \+ why : ''\)/);
  assert.match(mock, /adminRevokeFleetGrant: \(id, o\)[\s\S]*?\(why \? ' · reason: ' \+ why : ''\)/);
});
