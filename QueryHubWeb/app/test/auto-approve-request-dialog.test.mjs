// The auto-approve request dialog (2026-10-24).
//
// The "already waiting" line never showed. The dialog read `status === 'submitted'`
// and `requestedAt`, which is the ADMIN list's shape. GET /auto-approve-requests
// (routes_requests.my_window_requests) sends the caller's own asks as
// `status: 'pending'`, `createdAt`, `windowMinutes` and `days`, and no
// `windowLabel`. These tests feed the real API shape. The dialog also says when the
// chosen window would end.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';
import { page, web, jsdomOpts as opts } from './_mount.mjs';

const pure = loadInto(bareWindow(), 'qh-data.jsx');

// One entry as the server sends it. Nothing here is named by the old reader.
const SERVER_ASK = { id: 41, connectionId: 'prod-main', databaseId: 'users', tier: 'RO', windowMinutes: 10080, days: 7,
  reason: 'Weekly lookups.', status: 'pending', decidedByName: null, decidedAt: null,
  createdAt: new Date(Date.now() - 95 * 60000).toISOString() };

test('a pending ask in the real API shape is waiting; a decided one is not', () => {
  assert.equal(pure.qhAskWaiting(SERVER_ASK), true);
  assert.equal(pure.qhAskWaiting({ ...SERVER_ASK, status: 'approved' }), false);
  assert.equal(pure.qhAskWaiting({ ...SERVER_ASK, status: 'rejected' }), false);
  assert.equal(pure.qhAskWaiting({ status: 'submitted' }), true, 'the admin shape still counts');
  assert.equal(pure.qhAskWaiting(null), false);
});

test('the time is createdAt (requestedAt is the admin name)', () => {
  assert.equal(pure.qhAskAt(SERVER_ASK), SERVER_ASK.createdAt);
  assert.equal(pure.qhAskAt({ requestedAt: 'x' }), 'x');
  assert.equal(pure.qhAskAt({}), null);
});

test('the window label has a fallback when the server sends none', () => {
  const L = pure.qhAskWindowLabel;
  assert.equal(L({ windowLabel: '8h' }), '8h');
  assert.equal(L({ windowMinutes: 10080, days: 7 }), '7 days');
  assert.equal(L({ windowMinutes: 1440, days: 1 }), '1 day');
  assert.equal(L({ windowMinutes: 480, days: null }), '8h');
  assert.equal(L({ windowMinutes: 60 }), '1h');
  assert.equal(L({ windowMinutes: 2880 }), '2 days');
  assert.equal(L({ windowMinutes: 90 }), '90 min');
  assert.equal(L({}), '');
  assert.equal(L(null), '');
});

test('the ask for a database is found by connection and database', () => {
  const list = [SERVER_ASK, { ...SERVER_ASK, id: 42, databaseId: 'orders', status: 'approved' }];
  assert.equal(pure.qhAskFor(list, 'prod-main', 'users').id, 41);
  assert.equal(pure.qhAskFor(list, 'prod-main', 'orders'), null, 'decided');
  assert.equal(pure.qhAskFor(list, 'other', 'users'), null);
  assert.equal(pure.qhAskFor(undefined, 'a', 'b'), null);
});

test('Ends on <date>: a local date for a day window, with the clock for an hour window', () => {
  const now = new Date(2026, 9, 24, 10, 30).getTime();      // 24 Oct 2026, 10:30 local
  assert.equal(pure.qhAskWindowEnds(7 * 1440, now), 'Ends on 31 Oct 2026');
  assert.equal(pure.qhAskWindowEnds(14 * 1440, now), 'Ends on 7 Nov 2026');
  assert.equal(pure.qhAskWindowEnds(30 * 1440, now), 'Ends on 23 Nov 2026');
  assert.equal(pure.qhAskWindowEnds(180, now), 'Ends on 24 Oct 2026 at 13:30');
  assert.equal(pure.qhAskWindowEnds(480, new Date(2026, 9, 24, 20, 0).getTime()), 'Ends on 25 Oct 2026 at 04:00');
  assert.equal(pure.qhAskWindowEnds(0, now), '');
  assert.equal(pure.qhAskWindowEnds('x', now), '');
  assert.doesNotMatch(pure.qhAskWindowEnds(7 * 1440, now), /UTC|GMT|Istanbul/, 'no time zone claim');
});

// ---------- mounted, with the real API shape ----------

const CONNS = [{ id: 'prod-main', name: 'prod-main', databases: [{ id: 'users', name: 'users', tier: 'RO' }, { id: 'orders', name: 'orders', tier: 'RO' }] }];
const LOAD = { windowOptions: [{ minutes: 60, label: '1h' }, { minutes: 1440, label: '1 day', days: 1 }, { minutes: 10080, label: '7 days', days: 7 }],
  tiers: ['RO', 'RW'], requests: [SERVER_ASK] };

function mount(load) {
  const ui = page(['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx']);
  ui.render(ui.React.createElement(ui.win.RequestAutoApproveModal, {
    conns: CONNS, onClose: () => {}, onSubmit: () => Promise.resolve(), load: () => Promise.resolve(load) }));
  return ui;
}
const choose = (ui, sel, v) => ui.React.act(() => {
  const set = Object.getOwnPropertyDescriptor(ui.win.HTMLSelectElement.prototype, 'value').set;
  set.call(sel, v); sel.dispatchEvent(new ui.win.Event('change', { bubbles: true }));
});

test('an ask the server calls pending shows the waiting line, with its age and window', opts, async () => {
  const ui = mount(LOAD);
  await ui.flush();
  const [conn, db] = ui.all('select.qh-input');
  choose(ui, conn, 'prod-main');
  choose(ui, ui.all('select.qh-input')[1], 'users');
  const wait = ui.doc.querySelector('.qh-req-wait');
  assert.ok(wait, 'the waiting line');
  assert.match(wait.textContent, /^You asked for this 1h ago — 7 days, and it is still waiting for a DBA\./);
  assert.ok(!ui.doc.querySelector('.qh-autoreq-say'), 'the "if granted" sentence steps aside');
  // The option itself says so too, and a database with no ask has no line.
  assert.match(ui.all('select.qh-input')[1].textContent, /users — asked, waiting/);
  choose(ui, ui.all('select.qh-input')[1], 'orders');
  assert.ok(!ui.doc.querySelector('.qh-req-wait'));
});

test('a decided ask does not block the form', opts, async () => {
  const ui = mount({ ...LOAD, requests: [{ ...SERVER_ASK, status: 'approved' }] });
  await ui.flush();
  choose(ui, ui.all('select.qh-input')[0], 'prod-main');
  choose(ui, ui.all('select.qh-input')[1], 'users');
  assert.ok(!ui.doc.querySelector('.qh-req-wait'));
});

test('the chosen window shows when it would end, and follows the choice', opts, async () => {
  const ui = mount(LOAD);
  await ui.flush();
  const ends = () => (ui.doc.querySelector('.qh-autoreq-ends') || {}).textContent;
  assert.match(ends(), /^Ends on \d{1,2} [A-Z][a-z]{2} \d{4} if a DBA grants it now\.$/);
  const seven = ends();
  ui.click(ui.all('.qh-seg-wrap button').find(b => b.textContent === '1h'));
  assert.match(ends(), /^Ends on \d{1,2} [A-Z][a-z]{2} \d{4} at \d\d:\d\d if a DBA grants it now\.$/);
  ui.click(ui.all('.qh-seg-wrap button').find(b => b.textContent === '7 days'));
  assert.equal(ends(), seven);
});

test('the dialog source reads the shared helpers, not the admin field names', () => {
  const src = web('qh-panels.jsx');
  assert.match(src, /const waitingOn = \(cid, dbid\) => qhAskFor\(mine, cid, dbid\);/);
  assert.doesNotMatch(src, /r\.status === 'submitted' && r\.connectionId/);
  assert.doesNotMatch(src, /waiting\.requestedAt|waiting\.windowLabel/);
});

test('the mock answers the caller its OWN asks in the real shape, and seeds one', () => {
  const mock = web('qh-api-mock.jsx');
  assert.match(mock, /requests: ADMIN\.autoReqs\.filter\(r => r\.requester === me\.handle\)\.map\(mockOwnAsk\)/);
  const map = mock.match(/const mockOwnAsk = [\s\S]*?\n\}\);/)[0];
  assert.match(map, /status: r\.status === 'submitted' \? 'pending' : r\.status/);
  assert.match(map, /createdAt: r\.requestedAt/);
  assert.doesNotMatch(map, /windowLabel|requestedAt:/, 'the user shape has neither');
  // The open-source export swaps the persona name, so the handle is not pinned.
  assert.match(mock, /id: 'ar_10', requester: '[a-z][a-z.]*'/);
});
