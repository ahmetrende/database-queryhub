// Admin batch 3 of 2026-10-24, mounted for real (jsdom).
//
//   * Queue: the first request opens when the queue arrives late. A View access
//     button opens Effective access for the requester. A "Requester holds" line
//     and an expiry line show when the data exists. J, K, A, D, C work, and are
//     ignored in a field or while a dialog is open.
//   * Kill switch: a state banner, "Pause all queries" and "Resume queries".
//   * Nav: a second line on a locked row, a red badge for failing connections.
//   * Metrics: 7 and 30 day tabs, the window goes to the client, and the
//     decision-time card has its empty and error states.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import { bareWindow, loadInto } from './_load.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }
const opts = JSDOM ? {} : { skip: 'jsdom not installed' };

const HERE = dirname(fileURLToPath(import.meta.url));
const web = (file) => readFileSync(resolve(HERE, '..', '..', file), 'utf8');

const SUPER = { name: 'Jordan Ray', role: 'super', isDba: true };
const DBA = { name: 'Jordan Ray', role: 'dba', isDba: true };

const pure = loadInto(bareWindow(), 'qh-data.jsx', 'qh-admin-data.jsx', 'qh-admin-effective.jsx', 'qh-admin.jsx', 'qh-admin-insights.jsx');

// ---------- pure rules ----------

test('expires in: minutes, hours, days, expired, and nothing for null', () => {
  const now = Date.parse('2026-10-24T10:00:00Z');
  const at = (ms) => new Date(now + ms).toISOString();
  assert.equal(pure.qhExpiresIn(null, now), '');
  assert.equal(pure.qhExpiresIn('not a time', now), '');
  assert.equal(pure.qhExpiresIn(at(12 * 60000), now), 'Expires in 12 min');
  assert.equal(pure.qhExpiresIn(at(5000), now), 'Expires in 1 min');
  assert.equal(pure.qhExpiresIn(at(3 * 3600000), now), 'Expires in 3 h');
  assert.equal(pure.qhExpiresIn(at(72 * 3600000), now), 'Expires in 3 d');
  assert.equal(pure.qhExpiresIn(at(-1000), now), 'Expired');
});

test('requester holds: highest live personal grant on that connection and database', () => {
  const it = { connectionId: 'prod-main', databaseId: 'orders', submitter: { slackId: 'U0EXAMPLE001', name: 'jordan.ray' } };
  const now = Date.parse('2026-10-24T10:00:00Z');
  const g = (o) => ({ subjectType: 'user', subject: 'U0EXAMPLE001', connectionId: 'prod-main', databases: ['orders'], tier: 'RO', expiresAt: null, ...o });
  assert.equal(pure.qhRequesterHolds(it, [], now), null);
  assert.equal(pure.qhRequesterHolds(it, null, now), null);
  assert.equal(pure.qhRequesterHolds(it, [g({}), g({ tier: 'RW' })], now), 'RW on orders');
  assert.equal(pure.qhRequesterHolds(it, [g({ databases: ['*'] })], now), 'RO on every database on prod-main');
  assert.equal(pure.qhRequesterHolds(it, [g({ databases: ['other'] })], now), null);
  assert.equal(pure.qhRequesterHolds(it, [g({ connectionId: 'x' })], now), null);
  assert.equal(pure.qhRequesterHolds(it, [g({ subjectType: 'team' })], now), null);
  assert.equal(pure.qhRequesterHolds(it, [g({ expiresAt: '2026-10-01T00:00:00Z' })], now), null, 'an ended grant is not held');
});

test('failing connection count skips healthy, unknown and deleted rows', () => {
  assert.equal(pure.qhFailingConnCount([{ health: { ok: false } }, { health: { ok: true } }, {}, { deleted: true, health: { ok: false } }]), 1);
  assert.equal(pure.qhFailingConnCount(undefined), 0);
});

test('the person in the Effective access hash', () => {
  const win = bareWindow();
  win.location = { hash: '#admin/effective?person=U0EXAMPLE001' };
  loadInto(win, 'qh-data.jsx', 'qh-admin-effective.jsx');
  assert.equal(win.effHashPerson(), 'U0EXAMPLE001');
  win.location.hash = '#admin/effective';
  assert.equal(win.effHashPerson(), '');
  win.location.hash = '#admin/effective?x=1&person=local%3Aann';
  assert.equal(win.effHashPerson(), 'local:ann');
});

test('decision time text and band labels', () => {
  assert.equal(pure.qhDecisionSec(null), '—');
  assert.equal(pure.qhDecisionSec(41), '41 s');
  assert.equal(pure.qhDecisionSec(360), '6 min');
  assert.equal(pure.qhDecisionSec(5400), '1.5 h');
  assert.equal(pure.qhBandLabel({ fromSec: 0, toSec: 300 }), '0 s to 5 min');
  assert.equal(pure.qhBandLabel({ fromSec: 3600, toSec: null }), 'Over 1 h');
});

// ---------- mounted ----------

function mount(url, props, extraWin) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { pretendToBeVisual: true, url });
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
  Object.assign(win, extraWin || {});
  loadInto(win, 'qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx', 'qh-admin-data.jsx', 'qh-admin-effective.jsx', 'qh-admin.jsx', 'qh-admin-insights.jsx');
  for (const name of ['ConnectionsView', 'RolesView', 'GrantsView', 'AutoView', 'ScopesView', 'TeamsView',
                      'MaskingView', 'AuditView', 'FeedbackView', 'SystemConfigView', 'EffectiveAccessView']) {
    win[name] = () => React.createElement('div', { className: 'stub-' + name });
  }
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  const render = (p) => React.act(() => root.render(React.createElement(win.AdminPanel, p)));
  render(props);
  const doc = win.document;
  const all = (sel) => [...doc.querySelectorAll(sel)];
  const flush = () => React.act(() => new Promise(r => setTimeout(r, 0)));
  const click = (el) => React.act(() => el.dispatchEvent(new win.MouseEvent('click', { bubbles: true })));
  const key = (k, target) => React.act(() => (target || doc).dispatchEvent(new win.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true })));
  return { win, doc, all, flush, click, key, render, act: (fn) => React.act(fn) };
}

const Q = (id, name, extra) => ({ id, submitter: { name, initials: name.slice(0, 2), slackId: 'U0EXAMPLE' + id },
  connectionId: 'prod-main', databaseId: 'orders', env: 'production', tier: 'RW', sql: 'UPDATE t SET a = 1 WHERE id = 2;',
  statements: 1, piiCols: [], estRows: 1, estTables: ['t'], reason: 'Fix one row.', submittedAt: '2026-10-23T08:00:00Z', ...extra });
const TWO = [Q('q1', 'Jordan Ray'), Q('q2', 'Sam Lee')];

function baseSt(extra) {
  return { queue: TWO, manualRuns: [], endpointReqs: [], autoRequests: [], grants: [], people: [], teams: [], metrics: {},
    killSwitch: { enabled: false, message: '', by: null, at: null },
    killHistory: () => Promise.resolve([]), toggleKill: () => Promise.resolve({ enabled: true, cancelledCount: 0 }),
    decide: () => {}, batchApprove: () => {}, approveBundle: () => {}, ...extra };
}
const URL_ = (s) => 'https://localhost/#admin/' + s;
const name = (ui) => (ui.doc.querySelector('.qh-adetail-name') || {}).textContent;

test('the first request opens when the queue arrives after the first render', opts, () => {
  const ui = mount(URL_('approvals'), { st: baseSt({ queue: [] }), user: DBA, modeEntry: null });
  assert.ok(ui.doc.querySelector('.qh-aempty.big'), 'nothing to review yet');
  ui.render({ st: baseSt({ queue: TWO }), user: DBA, modeEntry: null });
  assert.equal(name(ui), 'Jordan Ray');
});

test('View access opens Effective access for the requester', opts, () => {
  const ui = mount(URL_('approvals'), { st: baseSt(), user: DBA, modeEntry: null });
  const btn = ui.all('.qh-adetail-viewacc')[0];
  assert.equal(btn.textContent, 'View access');
  ui.click(btn);
  assert.equal(ui.win.location.hash, '#admin/effective?person=U0EXAMPLEq1');
});

test('Requester holds and Expires in show only when there is data', opts, () => {
  const none = mount(URL_('approvals'), { st: baseSt(), user: DBA, modeEntry: null });
  assert.equal(none.all('.qh-adetail-holds').length, 0);
  assert.equal(none.all('.qh-adetail-expires').length, 0);
  const grants = [{ subjectType: 'user', subject: 'U0EXAMPLEq1', connectionId: 'prod-main', databases: ['orders'], tier: 'RO', expiresAt: null }];
  const soon = new Date(Date.now() + 30 * 60000 + 5000).toISOString();
  const ui = mount(URL_('approvals'), { st: baseSt({ grants, queue: [Q('q1', 'Jordan Ray', { expiresAt: soon })] }), user: DBA, modeEntry: null });
  assert.equal(ui.all('.qh-adetail-holds')[0].textContent, 'Requester holds: RO on orders');
  assert.equal(ui.all('.qh-adetail-expires')[0].textContent, 'Expires in 30 min');
});

test('keys: J and K move, A approves, D opens the deny note', opts, () => {
  const decided = [];
  const ui = mount(URL_('approvals'), { st: baseSt({ decide: (...a) => decided.push(a) }), user: DBA, modeEntry: null });
  assert.match(ui.doc.querySelector('.qh-adetail-keys').textContent, /J.*next.*K.*previous.*A.*approve and run.*D.*deny.*C.*request changes/);
  ui.key('j');
  assert.equal(name(ui), 'Sam Lee');
  ui.key('j');
  assert.equal(name(ui), 'Sam Lee', 'stops at the last card');
  ui.key('k');
  assert.equal(name(ui), 'Jordan Ray');
  ui.key('d');
  assert.ok(ui.doc.querySelector('.qh-notebox'), 'D opens the note');
  assert.equal(decided.length, 0);
  ui.key('c');
  assert.match(ui.doc.querySelector('.qh-notebox-l').textContent, /What must they change/);
  ui.act(() => { ui.all('.qh-notebox button').find(b => b.textContent === 'Cancel').dispatchEvent(new ui.win.MouseEvent('click', { bubbles: true })); });
  ui.key('a');
  assert.deepEqual(decided.map(d => [d[0], d[1]]), [['q1', 'approve']]);
});

test('keys are ignored in a field and while a dialog is open', opts, () => {
  const decided = [];
  const ui = mount(URL_('approvals'), { st: baseSt({ decide: (...a) => decided.push(a) }), user: DBA, modeEntry: null });
  ui.key('d');   // opens the note, which holds a textarea
  const box = ui.doc.querySelector('.qh-notebox textarea');
  ui.key('a', box);
  ui.key('j', box);
  assert.equal(decided.length, 0, 'typing a in the note approves nothing');
  assert.equal(name(ui), 'Jordan Ray', 'typing j in the note moves nothing');
  const dlg = ui.doc.createElement('div');
  dlg.setAttribute('role', 'dialog');
  ui.doc.body.appendChild(dlg);
  ui.key('j');
  assert.equal(name(ui), 'Jordan Ray', 'an open dialog blocks the keys');
});

test('kill switch: banner and buttons for running and paused', opts, () => {
  const run = mount(URL_('kill'), { st: baseSt(), user: DBA, modeEntry: null });
  assert.match(run.doc.querySelector('.qh-kill-banner.is-running').textContent, /Queries are running/);
  assert.equal(run.all('.qh-kill-card button')[0].textContent.trim(), 'Pause all queries');
  assert.match(run.all('.qh-kill-card button')[0].className, /qh-btn-danger/);
  const off = mount(URL_('kill'), { st: baseSt({ killSwitch: { enabled: true, message: 'Replica lag.', by: 'jordan.ray', at: new Date().toISOString() } }), user: DBA, modeEntry: null });
  const banner = off.doc.querySelector('.qh-kill-banner.is-paused');
  assert.match(banner.textContent, /Queries are paused/);
  assert.match(banner.textContent, /Paused by Jordan Ray/);
  assert.match(banner.textContent, /Reason: Replica lag\./);
  assert.equal(off.all('.qh-kill-card button')[0].textContent.trim(), 'Resume queries');
});

test('nav: a locked row says who to ask, and Connections shows failing connections', opts, () => {
  const rows = [{ id: 'a', health: { ok: false } }, { id: 'b', health: { ok: true } }, { id: 'c', deleted: true, health: { ok: false } }];
  const ui = mount(URL_('approvals'), { st: baseSt({ connectionRows: rows, connections: rows }), user: DBA, modeEntry: null });
  const locked = ui.all('.qh-anav-item.is-locked');
  assert.ok(locked.length > 0);
  for (const b of locked) assert.equal(b.querySelector('.qh-anav-ask').textContent, 'Ask a super-admin');
  const conns = ui.all('.qh-anav-item').find(b => b.textContent.startsWith('Connections'));
  assert.equal(conns.querySelector('.qh-anav-badge.is-fail').textContent, '1');
  const sup = mount(URL_('approvals'), { st: baseSt(), user: SUPER, modeEntry: null });
  assert.equal(sup.all('.qh-anav-ask').length, 0);
  assert.equal(sup.all('.qh-anav-badge.is-fail').length, 0, 'no failing connection, no badge');
});

// ---------- metrics ----------

const DEC = { requests: 40, p50Sec: 360, p90Sec: 900,
  bands: [{ fromSec: 0, toSec: 300, count: 20 }, { fromSec: 300, toSec: 900, count: 10 }, { fromSec: 900, toSec: 3600, count: 6 }, { fromSec: 3600, toSec: null, count: 4 }],
  byServer: Array.from({ length: 7 }, (_, i) => ({ name: 'srv-' + i, requests: 10 - i, p50Sec: 120, p90Sec: 600 })),
  byTeam: [{ name: 'backend', requests: 30, p50Sec: 60, p90Sec: 300 }] };

test('metrics: tabs pass the window, the card shows median, p90, bands and five rows', opts, async () => {
  const calls = [];
  const adminMetrics = (w) => { calls.push(w); return Promise.resolve({ headline: { total: 5 }, window: w, windowStart: '2026-09-24T00:00:00Z', decisionTime: w === '7d' ? { ...DEC, requests: 9, p50Sec: 60 } : DEC }); };
  const ui = mount(URL_('metrics'), { st: baseSt(), user: DBA, modeEntry: null }, { qhApi: { adminMetrics } });
  await ui.flush();
  assert.deepEqual(calls, ['30d'], '30 days is the default');
  const card = () => ui.doc.querySelector('.qh-dec');
  assert.match(card().textContent, /Decision time · Last 30 days/);
  assert.match(card().textContent, /6 min/);
  assert.match(card().textContent, /15 min/);
  assert.equal(ui.all('.qh-dec-band').length, 4);
  assert.equal(ui.all('.qh-dec-row').filter(r => /srv-/.test(r.textContent)).length, 5, 'at most five server rows');
  assert.equal(ui.all('.qh-dec-row').filter(r => /backend/.test(r.textContent)).length, 1);
  const tab = ui.all('.qh-mwin [role="tab"]').find(b => b.textContent === 'Last 7 days');
  ui.click(tab);
  await ui.flush();
  assert.deepEqual(calls, ['30d', '7d']);
  assert.match(card().textContent, /Decision time · Last 7 days/);
  assert.equal(ui.all('.qh-mwin [aria-selected="true"]')[0].textContent, 'Last 7 days');
});

test('metrics: an empty window and a failed read say so', opts, async () => {
  const empty = mount(URL_('metrics'), { st: baseSt(), user: DBA, modeEntry: null },
    { qhApi: { adminMetrics: () => Promise.resolve({ headline: { total: 0 }, decisionTime: { requests: 0, p50Sec: null, p90Sec: null, bands: [], byServer: [], byTeam: [] } }) } });
  await empty.flush();
  assert.match(empty.doc.querySelector('.qh-dec').textContent, /No request was decided in this window\./);
  const bad = mount(URL_('metrics'), { st: baseSt(), user: DBA, modeEntry: null },
    { qhApi: { adminMetrics: () => Promise.reject(new Error('x')) } });
  await bad.flush();
  assert.match(bad.doc.querySelector('.qh-dec[role="alert"]').textContent, /Could not load the decision time/);
});

// ---------- clients ----------

test('both clients take the window, and the mock answers both windows', () => {
  assert.match(web('qh-api.jsx'), /adminMetrics:\s*\(window\) => qhFetch\('\/admin\/metrics' \+ \(window \? '\?window=' \+ encodeURIComponent\(window\) : ''\)\)/);
  const mock = web('qh-api-mock.jsx');
  assert.match(mock, /adminMetrics: \(window\) => mockDelay/);
  assert.match(mock, /decisionTime: \{/);
  assert.match(mock, /const w7 = window === '7d'/);
});
