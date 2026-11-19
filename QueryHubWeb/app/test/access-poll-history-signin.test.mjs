// Four items ported from the IdP app (rows 6, 10, 15 and 16 of the
// IdP-better list):
//
//   * Row 6: the access-request dialog stays open. A sent request shows its
//     id, the server's own 503 shows as saved with a warning, and any other
//     failure keeps the form and the typed reason.
//   * Row 10: the request status poll backs off on errors, slows for a
//     request that waits for a DBA, and pauses in a hidden browser tab.
//   * Row 15: a click on a History row opens that run with its result.
//     "New tab with this SQL" keeps the old action.
//   * Row 16: until /me answers, the page shows a loader, not the sign-in card.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';
import { page, web, jsdomOpts as opts } from './_mount.mjs';

const H = loadInto(bareWindow(), 'qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx');

// ---------- row 6: the outcome of an access request ----------

test('request outcome: an id after a send, a warning for the server 503, an error otherwise', () => {
  assert.deepEqual(H.qhReqSentState({ id: 'er_12', status: 'submitted' }), { id: 'er_12' });
  assert.deepEqual(H.qhReqSentState(undefined), { id: null });
  const saved = H.qhReqFailState({ status: 503, message: 'QueryHub saved your request, but no active admin can review it. Contact the DBA team.' });
  assert.match(saved.warn, /saved your request/);
  assert.deepEqual(H.qhReqFailState({ status: 503, message: 'Request failed (503)' }), { err: 'The request was not sent. Try again.' },
    'a 503 with no message comes from a proxy: nothing was saved');
  assert.deepEqual(H.qhReqFailState({ status: 400, message: 'There is no database "x" on prod-main.' }), { err: 'There is no database "x" on prod-main.' });
  assert.deepEqual(H.qhReqFailState(null), { err: 'The request was not sent. Try again.' });
});

const ROWS = [{ connectionId: 'prod-main', name: 'prod-main', engine: 'postgres', env: 'production', databases: ['payments'], partial: false }];

function mountDialog(onSubmit) {
  const ui = page(['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx']);
  let closed = 0;
  ui.render(ui.React.createElement(ui.win.RequestAccessModal, {
    onClose: () => { closed++; }, onSubmit, load: () => Promise.resolve({ connections: ROWS }) }));
  return { ...ui, closed: () => closed };
}
const type = (ui, el, v) => {
  const set = Object.getOwnPropertyDescriptor(ui.win.HTMLTextAreaElement.prototype, 'value').set;
  ui.React.act(() => { set.call(el, v); el.dispatchEvent(new ui.win.Event('input', { bubbles: true })); });
};
const sendBtn = (ui) => [...ui.doc.querySelectorAll('.qh-modal-foot button')].find(b => /Send request|Sending/.test(b.textContent));
async function fillAndSend(ui) {
  await ui.flush();
  ui.click(ui.all('.qh-reqrow')[0]);   // one database: it is picked at once
  type(ui, ui.doc.querySelector('textarea'), 'Monthly ledger check');
  ui.click(sendBtn(ui));
  await ui.flush();
}

test('a sent request shows its id in the dialog, which stays open', opts, async () => {
  const ui = mountDialog(() => Promise.resolve({ id: 'er_42', status: 'submitted' }));
  await fillAndSend(ui);
  const ok = ui.doc.querySelector('.qh-req-ok');
  assert.ok(ok, 'the outcome is in the dialog');
  assert.match(ok.textContent, /You sent request er_42\. You get a DM when a DBA decides\./);
  assert.equal(ui.closed(), 0, 'the dialog is still open');
  const close = [...ui.doc.querySelectorAll('.qh-modal-foot button')].find(b => b.textContent === 'Close');
  ui.click(close);
  assert.equal(ui.closed(), 1);
});

test('a refused request keeps the form and the reason, and says why under it', opts, async () => {
  const ui = mountDialog(() => Promise.reject(Object.assign(new Error('You already have 3 open requests.'), { status: 409 })));
  await fillAndSend(ui);
  const alert = ui.doc.querySelector('.qh-modal-body .qh-req-warn[role="alert"]');
  assert.equal(alert && alert.textContent, 'You already have 3 open requests.');
  assert.equal(ui.doc.querySelector('textarea').value, 'Monthly ledger check', 'the typed reason is kept');
  assert.equal(sendBtn(ui).disabled, false, 'the person can send again');
  assert.equal(ui.closed(), 0);
});

test('the server 503 shows the request as saved, with no second send', opts, async () => {
  const ui = mountDialog(() => Promise.reject(Object.assign(new Error('QueryHub saved your request, but no active admin can review it. Contact the DBA team.'), { status: 503 })));
  await fillAndSend(ui);
  assert.equal(ui.doc.querySelector('.qh-modal-title').textContent, 'Request saved');
  assert.match(ui.doc.querySelector('.qh-req-warn').textContent, /saved your request/);
  assert.equal(sendBtn(ui), undefined, 'no Send button: a second send would make a duplicate');
});

test('App returns the submit promise and closes nothing itself', () => {
  const app = web('qh-app.jsx');
  assert.match(app, /const submitRequest = \(req\) => qhApi\.requestEndpoint\(/);
  assert.doesNotMatch(app, /sent to DBA in Slack\./, 'the old toast is gone');
});

// ---------- row 10: the status poll ----------

test('poll delay: 1.5 s, doubled per failure up to 30 s', () => {
  const now = 1_000_000;
  assert.equal(H.qhPollDelay({ status: 'running', startedAt: now }, now), 1500);
  assert.deepEqual([1, 2, 3, 4, 5, 6].map(f => H.qhPollDelay({ failures: f }, now)), [1500, 3000, 6000, 12000, 24000, 30000]);
});

test('poll delay: a request that waits for a DBA slows to 10 s', () => {
  const now = Date.parse('2026-11-19T12:00:00Z');
  const fresh = new Date(now - 5000).toISOString();
  assert.equal(H.qhPollDelay({ status: 'pending', startedAt: now - 5000, createdAt: fresh }, now), 1500, 'the first 15 s stay fast');
  assert.equal(H.qhPollDelay({ status: 'pending', startedAt: now - 15000, createdAt: fresh }, now), 10000);
  const old = new Date(now - 61000).toISOString();
  assert.equal(H.qhPollDelay({ status: 'pending', startedAt: now, createdAt: old }, now), 10000, 'an old request is slow at once');
  assert.equal(H.qhPollDelay({ status: 'pending', startedAt: now }, now), 1500, 'no createdAt: the poll age decides');
  assert.equal(H.qhPollDelay({ status: 'running', startedAt: now - 60000, createdAt: old }, now), 1500, 'a running request stays fast');
});

test('App polls with a timer chain that pauses in a hidden tab', () => {
  const app = web('qh-app.jsx');
  assert.doesNotMatch(app, /setInterval\(\(\) => refreshOnce/, 'no fixed interval');
  assert.match(app, /tr\.timer = setTimeout\(\(\) => pollTick\(id, qid\), qhPollDelay\(tr\)\)/);
  assert.match(app, /if \(document\.visibilityState === 'hidden'\) return;\n    tr\.inFlight = true;/);
  assert.match(app, /document\.addEventListener\('visibilitychange', onShow\)/);
  assert.match(app, /e\.status === 401 \|\| e\.status === 403 \|\| e\.status === 404\) \{ stopTrack\(id\); return; \}/);
  assert.match(app, /tr\.failures = Math\.min\(\(tr\.failures \|\| 0\) \+ 1, 6\)/);
});

// ---------- row 15: History opens the run ----------

test('the tab of a History row is the request itself, and the status read wins', () => {
  const row = { id: 812, sql: 'select 1', conn: 'prod-main', db: 'payments', status: 'done', state: null, createdAt: '2026-11-19T10:00:00Z' };
  const t = H.qhHistoryRunTab(row, { sql: 'select 1 as x', connectionId: 'prod-main', databaseId: 'ledger', status: 'done', submittedAt: '2026-11-19T10:00:01Z' });
  assert.equal(t.qid, '812');
  assert.equal(t.reqId, '812');
  assert.equal(t.sql, 'select 1 as x');
  assert.equal(t.db, 'ledger');
  assert.equal(t.ranSql, 'select 1 as x');
  assert.equal(t.status, 'done');
  assert.equal(t.autoName, false);
  const fallback = H.qhHistoryRunTab(row, null);
  assert.deepEqual([fallback.sql, fallback.conn, fallback.db, fallback.submittedAt], ['select 1', 'prod-main', 'payments', '2026-11-19T10:00:00Z']);
});

test('App opens a History row as a run and keeps "New tab with this SQL"', () => {
  const app = web('qh-app.jsx');
  assert.match(app, /onLoadHistory=\{openHistoryRun\} onCopyHistory=\{loadHistory\}/);
  assert.match(app, /history=\{history\} onLoadHistory=\{openHistoryRun\}\n/, 'Home opens the run too');
  assert.doesNotMatch(app, /onLoadHistory=\{loadHistory\}/);
});

test('a History row has a "New tab with this SQL" action, except a row sent back', opts, async () => {
  const ui = page(['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx']);
  const opened = [], copied = [];
  const history = [
    { id: 1, sql: 'select 1', conn: 'prod-main', db: 'payments', tier: 'RO', status: 'done', when: '', outcome: null },
    { id: 2, sql: 'update t set x = 1', conn: 'prod-main', db: 'payments', tier: 'RW', status: 'rejected', when: '', outcome: 'changes_requested' },
  ];
  ui.render(ui.React.createElement(ui.win.Sidebar, { mode: 'history', setMode: () => {}, conns: [], history, saved: [], sessions: [], scheduled: [], pending: [],
    onLoadHistory: (h) => opened.push(h.id), onCopyHistory: (h) => copied.push(h.id), onResubmit: () => {} }));
  await ui.flush();
  const items = ui.all('.qh-hist-item');
  assert.equal(items.length, 2);
  ui.click(items[0].querySelector('button.qh-hist'));
  assert.deepEqual(opened, [1]);
  const copy = items[0].querySelector('button.qh-hist-copy');
  assert.equal(copy.getAttribute('aria-label'), 'New tab with this SQL');
  ui.click(copy);
  assert.deepEqual(copied, [1]);
  assert.deepEqual(opened, [1], 'the copy action does not also open the run');
  assert.equal(items[1].querySelector('button.qh-hist-copy'), null, 'Edit and resubmit covers a row sent back');
});

// ---------- row 16: no sign-in card while /me loads ----------

test('App shows a loader until /me answers, before the sign-in card', () => {
  const app = web('qh-app.jsx');
  const body = app.slice(app.indexOf('function App()'));
  const gate = body.indexOf('if (!authChecked) return <div className="qh-login" role="status">');
  assert.ok(gate > 0, 'a loader gate on authChecked');
  assert.ok(gate < body.indexOf('if (!user) return <LoginScreen'), 'before the sign-in card');
  assert.match(body.slice(gate, gate + 200), /qh-sr-only">Loading QueryHub</);
});
