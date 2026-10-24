// The bell (2026-10-24, batch 2).
//
//   * A click opens the subject of an item when it can be read from the item. The
//     server builds the id from the subject (q<request id>-dec, q<request id>-sched,
//     er<access request id>, kill<audit id>), and the payload has no other field,
//     so the id is the only source. A request opens its tab, or the History list.
//     An admin item opens #admin/<section> for an admin. With nothing to open, the
//     click only marks the item read. The payload shape is unchanged.
//   * A switch "Browser notifications" in the bell menu. Off by default. Stored in
//     localStorage. Turning it on asks the browser for permission FROM THAT CLICK
//     and never on load. When it is on and the permission is granted, a new unread
//     item raises one Notification, once (dedupe by item id).
// A fake Notification stands in for the browser's.
import test, { after } from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';
import { page, sliceFunction, web, jsdomOpts as opts } from './_mount.mjs';

const pure = loadInto(bareWindow(), 'qh-data.jsx');

// ---------- what an item opens ----------

test('the id says what a bell item is about', () => {
  const { qhNotifSubject } = pure;
  assert.deepEqual(qhNotifSubject({ id: 'q4521-dec' }), { kind: 'request', id: '4521' });
  assert.deepEqual(qhNotifSubject({ id: 'q4521-sched' }), { kind: 'request', id: '4521' });
  assert.deepEqual(qhNotifSubject({ id: 'qq_e01-dec' }, { isAdmin: false }), { kind: 'request', id: 'q_e01' }, 'a request id is not always a number (the mock)');
  assert.deepEqual(qhNotifSubject({ id: 'kill77' }, { isAdmin: true }), { kind: 'admin', section: 'kill' });
  assert.deepEqual(qhNotifSubject({ id: 'er41' }, { isAdmin: true }), { kind: 'admin', section: 'conns' });
});

test('an admin item opens for an admin only; anything else has no subject', () => {
  const { qhNotifSubject } = pure;
  assert.equal(qhNotifSubject({ id: 'kill77' }, { isAdmin: false }), null);
  assert.equal(qhNotifSubject({ id: 'kill77' }), null);
  assert.equal(qhNotifSubject({ id: 'er41' }, { isAdmin: false }), null);
  for (const id of ['n1', '', 'x-dec', 'killer', 'er', 'q-dec', 'q5', 'q5-other']) assert.equal(qhNotifSubject({ id }, { isAdmin: true }), null, id);
  assert.equal(qhNotifSubject(null, { isAdmin: true }), null);
  assert.equal(qhNotifSubject({}, { isAdmin: true }), null);
  assert.equal(qhNotifSubject({ id: 77 }, { isAdmin: true }), null, 'a number is not an id of the feed');
});

test('every id the server builds is read by the rule (the builder and the reader agree)', () => {
  // The ids come from _derive_notifications in routes_queries.py.
  const py = web('../src/queryhub/web/routes_queries.py');
  for (const shape of ['f"q{r[\'id\']}-dec"', 'f"q{r[\'id\']}-sched"', 'f"er{r[\'id\']}"', 'f"kill{r[\'id\']}"']) {
    assert.ok(py.includes(shape), 'the server still builds ' + shape);
  }
});

// ---------- the bell, mounted ----------

class FakeNotification {
  static permission = 'default';
  static asked = 0;
  static answer = 'granted';
  static shown = [];
  constructor(title, opt) { this.title = title; this.opt = opt; FakeNotification.shown.push({ title, tag: opt && opt.tag, body: opt && opt.body }); }
  static requestPermission() { FakeNotification.asked++; FakeNotification.permission = FakeNotification.answer; return Promise.resolve(FakeNotification.answer); }
}
function resetFake(permission, answer) {
  FakeNotification.permission = permission; FakeNotification.answer = answer || 'granted';
  FakeNotification.asked = 0; FakeNotification.shown = [];
}

// The bell polls on a 60 s interval. Close every page at the end, or that timer
// keeps the process alive and the run never ends.
const pages = [];
after(() => { pages.forEach(w => { try { w.close(); } catch (e) { /* closed */ } }); });

const soon = (ms) => new Date(Date.now() - ms).toISOString();
function mountBell({ items, isAdmin = false, fake = true, store = {}, onOpenSubject } = {}) {
  const calls = { read: [], open: [] };
  const ui = page(['qh-data.jsx'], { prep: (win) => {
    for (const [k, v] of Object.entries(store)) win.localStorage.setItem(k, v);
    if (fake) win.Notification = FakeNotification;
    win.useState = win.React.useState; win.useEffect = win.React.useEffect; win.useRef = win.React.useRef;
    win.qhTimeAgo = () => 'now';
    win.qhApi = { notifications: () => Promise.resolve({ notifications: items }), notificationsRead: (b) => { calls.read.push(b); return Promise.resolve({ ok: true }); } };
  } });
  const w = ui.win;
  pages.push(w);
  sliceFunction('qh-app.jsx', 'notifIcon', w);
  const Bell = sliceFunction('qh-app.jsx', 'NotificationBell', w);
  ui.render(ui.React.createElement(Bell, { unseenNews: false, isAdminUser: isAdmin, onOpenSubject: onOpenSubject || ((s) => calls.open.push(s)) }));
  return { ...ui, calls };
}
const openMenu = async (ui) => { await ui.flush(); ui.click(ui.doc.querySelector('.qh-notif-btn')); };
const ITEMS = () => [
  { id: 'q91-dec', kind: 'approved', title: 'Query approved', body: 'b1', createdAt: soon(2 * 60000), read: false },
  { id: 'kill77', kind: 'kill', title: 'Execution paused', body: 'b2', createdAt: soon(60 * 60000), read: false },
  { id: 'er41', kind: 'endpoint', title: 'Endpoint provisioned', body: 'b3', createdAt: soon(61 * 60000), read: false },
  { id: 'n9', kind: 'other', title: 'No subject', body: 'b4', createdAt: soon(62 * 60000), read: false },
];
const item = (ui, title) => ui.all('.qh-notif-item').find(b => b.textContent.includes(title));

test('a click marks the item read and opens a request', opts, async () => {
  const ui = mountBell({ items: ITEMS() });
  await openMenu(ui);
  ui.click(item(ui, 'Query approved'));
  assert.deepEqual(ui.calls.read, [{ ids: ['q91-dec'] }]);
  assert.deepEqual(ui.calls.open, [{ kind: 'request', id: '91' }]);
  assert.equal(ui.doc.querySelector('.qh-notif-menu'), null, 'the menu closes when it opens a subject');
});

test('admin items open their screen for an admin and only mark read for anyone else', opts, async () => {
  const a = mountBell({ items: ITEMS(), isAdmin: true });
  await openMenu(a);
  a.click(item(a, 'Execution paused'));
  assert.deepEqual(a.calls.open, [{ kind: 'admin', section: 'kill' }]);
  a.click(a.doc.querySelector('.qh-notif-btn'));   // the first click closed the menu: open it again
  a.click(item(a, 'Endpoint provisioned'));
  assert.deepEqual(a.calls.open[1], { kind: 'admin', section: 'conns' });
  const b = mountBell({ items: ITEMS(), isAdmin: false });
  await openMenu(b);
  b.click(item(b, 'Execution paused'));
  assert.deepEqual(b.calls.open, [], 'a developer cannot open the admin screens');
  assert.deepEqual(b.calls.read, [{ ids: ['kill77'] }], 'it only marks read');
  assert.ok(b.doc.querySelector('.qh-notif-menu'), 'the menu stays open');
});

test('an item with no subject only marks read', opts, async () => {
  const ui = mountBell({ items: ITEMS(), isAdmin: true });
  await openMenu(ui);
  ui.click(item(ui, 'No subject'));
  assert.deepEqual(ui.calls.read, [{ ids: ['n9'] }]);
  assert.deepEqual(ui.calls.open, []);
});

// ---------- the switch ----------

const sw = (ui) => ui.doc.querySelector('.qh-notif-switch input');
const hint = (ui) => ui.doc.querySelector('.qh-notif-switch-hint').textContent;

test('the switch is off by default, and nothing asks the browser on load or on opening the menu', opts, async () => {
  resetFake('default');
  const ui = mountBell({ items: ITEMS() });
  await openMenu(ui);
  assert.equal(sw(ui).checked, false);
  assert.equal(sw(ui).getAttribute('role'), 'switch');
  assert.match(ui.doc.querySelector('.qh-notif-switch-label').textContent, /^Browser notifications$/);
  assert.equal(FakeNotification.asked, 0);
  assert.equal(FakeNotification.shown.length, 0);
  assert.match(hint(ui), /^Off\./);
});

test('turning it on asks the browser once, from the click, and stores the choice', opts, async () => {
  resetFake('default', 'granted');
  const ui = mountBell({ items: ITEMS() });
  await openMenu(ui);
  ui.click(sw(ui));
  await ui.flush();
  assert.equal(FakeNotification.asked, 1);
  assert.equal(sw(ui).checked, true);
  assert.equal(ui.win.localStorage.getItem('qh.notif.browser.v1'), '1');
  // The items that were in the feed count as seen: turning the switch on shows none.
  assert.equal(FakeNotification.shown.length, 0);
  // Turning it off asks nothing and stores the choice.
  ui.click(sw(ui));
  await ui.flush();
  assert.equal(FakeNotification.asked, 1);
  assert.equal(sw(ui).checked, false);
  assert.equal(ui.win.localStorage.getItem('qh.notif.browser.v1'), '0');
});

test('a no from the browser keeps the switch off and says why', opts, async () => {
  resetFake('default', 'denied');
  const ui = mountBell({ items: ITEMS() });
  await openMenu(ui);
  ui.click(sw(ui));
  await ui.flush();
  assert.equal(sw(ui).checked, false);
  assert.equal(ui.win.localStorage.getItem('qh.notif.browser.v1'), '0');
  assert.match(hint(ui), /did not give permission/);
});

test('a site that is already blocked is not asked again', opts, async () => {
  resetFake('denied');
  const ui = mountBell({ items: ITEMS() });
  await openMenu(ui);
  ui.click(sw(ui));
  await ui.flush();
  assert.equal(FakeNotification.asked, 0);
  assert.equal(sw(ui).checked, false);
  assert.match(hint(ui), /blocks notifications for this site/);
});

test('a browser with no Notification API keeps the switch off and says so', opts, async () => {
  const ui = mountBell({ items: ITEMS(), fake: false });
  await openMenu(ui);
  ui.click(sw(ui));
  await ui.flush();
  assert.equal(sw(ui).checked, false);
  assert.match(hint(ui), /no notifications/);
});

test('a permission that is already granted turns on without a prompt', opts, async () => {
  resetFake('granted');
  const ui = mountBell({ items: ITEMS() });
  await openMenu(ui);
  ui.click(sw(ui));
  await ui.flush();
  assert.equal(FakeNotification.asked, 0);
  assert.equal(sw(ui).checked, true);
});

// ---------- raising ----------

test('with the switch on and the permission granted, a new unread item raises one Notification', opts, async () => {
  resetFake('granted');
  const items = ITEMS();
  const ui = mountBell({ items, store: { 'qh.notif.browser.v1': '1', 'qh.notif.raised.v1': JSON.stringify(['kill77', 'er41', 'n9']) } });
  await ui.flush();
  assert.deepEqual(FakeNotification.shown, [{ title: 'Query approved', tag: 'q91-dec', body: 'b1' }], 'the one new item');
  // The ids are saved, so a reload does not raise it again.
  assert.ok(JSON.parse(ui.win.localStorage.getItem('qh.notif.raised.v1')).includes('q91-dec'));
});

test('a reload does not raise an item twice', opts, async () => {
  resetFake('granted');
  const ui = mountBell({ items: ITEMS(), store: { 'qh.notif.browser.v1': '1', 'qh.notif.raised.v1': JSON.stringify(['q91-dec', 'kill77', 'er41', 'n9']) } });
  await ui.flush();
  assert.deepEqual(FakeNotification.shown, []);
});

test('with the switch off, nothing is raised', opts, async () => {
  resetFake('granted');
  const ui = mountBell({ items: ITEMS() });
  await ui.flush();
  assert.deepEqual(FakeNotification.shown, []);
});

test('an item the person already read is not raised', opts, async () => {
  resetFake('granted');
  const items = ITEMS().map(i => ({ ...i, read: i.id === 'q91-dec' }));
  const ui = mountBell({ items, store: { 'qh.notif.browser.v1': '1' } });
  await ui.flush();
  assert.deepEqual(FakeNotification.shown, [], 'q91 was read; the others are older than ten minutes');
});

// ---------- the raise rule, alone ----------

test('raise rule: once per id, only recent unread items, at most three, and the ids are kept either way', () => {
  const { qhRaiseBrowserNotifs } = pure;
  resetFake('granted');
  const raised = new Set();
  const many = Array.from({ length: 5 }, (_, i) => ({ id: 'q' + i + '-dec', title: 't' + i, body: '', createdAt: soon(60000) }));
  const first = qhRaiseBrowserNotifs(many, { Ctor: FakeNotification, raised });
  assert.equal(first.length, 3, 'at most three at once');
  assert.equal(raised.size, 5, 'the two that were not shown are marked, so they never fire later');
  assert.deepEqual(qhRaiseBrowserNotifs(many, { Ctor: FakeNotification, raised }), [], 'the same ids never raise twice');
  const old = [{ id: 'q9-dec', title: 'old', body: '', createdAt: soon(11 * 60000) }];
  assert.deepEqual(qhRaiseBrowserNotifs(old, { Ctor: FakeNotification, raised }), [], 'older than ten minutes');
  assert.ok(raised.has('q9-dec'));
  const noTime = [{ id: 'q10-dec', title: 'x', body: '' }];
  assert.deepEqual(qhRaiseBrowserNotifs(noTime, { Ctor: FakeNotification, raised }), [], 'no time, no raise');
});

test('raise rule: no permission raises nothing, and a blocked item is still marked', () => {
  const { qhRaiseBrowserNotifs } = pure;
  resetFake('denied');
  const raised = new Set();
  const out = qhRaiseBrowserNotifs([{ id: 'q1-dec', title: 't', createdAt: soon(1000) }], { Ctor: FakeNotification, raised });
  assert.deepEqual(out, []);
  assert.equal(FakeNotification.shown.length, 0);
  assert.deepEqual(qhRaiseBrowserNotifs([{ id: 'q2-dec', title: 't', createdAt: soon(1000) }], { Ctor: undefined, raised }), []);
});

test('the permission prompt is only in the switch handler', () => {
  const app = web('qh-app.jsx');
  assert.equal((app.match(/requestPermission/g) || []).length, 0, 'the app never calls it directly');
  const data = web('qh-data.jsx');
  assert.equal((data.match(/Ctor\.requestPermission\(\)/g) || []).length, 1, 'one place, in qhBrowserNotifEnable');
  const bell = app.slice(app.indexOf('function NotificationBell'), app.indexOf('// ---------- Editor'));
  // It is called from toggleBrowser (a click handler), and not from an effect.
  assert.match(bell, /const toggleBrowser = \(\) => \{[\s\S]*?qhBrowserNotifEnable\(window\.Notification\)/);
  const effects = [...bell.matchAll(/useEffect\(\(\) => \{[\s\S]*?\}, \[[^\]]*\]\);/g)].map(m => m[0]).join('\n');
  assert.doesNotMatch(effects, /qhBrowserNotifEnable|requestPermission/);
});

// ---------- the app opens the subject ----------

test('a request item opens its tab when one is open, else the History list at that row', () => {
  const app = web('qh-app.jsx');
  const fn = app.slice(app.indexOf('const openRequest ='), app.indexOf('const newTab ='));
  assert.match(fn, /x\.qid && String\(x\.qid\) === String\(id\)/);
  assert.match(fn, /x\.reqId && String\(x\.reqId\) === String\(id\)/);
  assert.match(fn, /setActiveId\(own\.id\)/);
  assert.match(fn, /setSideMode\('history'\)/);
  assert.match(fn, /setHistFocus\(\{ id: String\(id\), n: Date\.now\(\) \}\)/);
  assert.match(fn, /if \(t\.hideSidebar\) setTweak\('hideSidebar', false\)/, 'a hidden sidebar is shown');
});

test('an admin item opens #admin/<section>, for an admin only; the payload shape is unchanged', () => {
  const app = web('qh-app.jsx');
  const fn = app.slice(app.indexOf('const openSubject ='), app.indexOf('const sideEl ='));
  assert.match(fn, /sub\.kind === 'request'\) openRequest\(sub\.id\)/);
  assert.match(fn, /sub\.kind === 'admin' && isAdminUser\) \{ window\.location\.hash = '#admin\/' \+ sub\.section; setView\('admin'\); \}/);
  // The two sections exist in the admin panel's deep-link vocabulary.
  const admin = web('qh-admin.jsx');
  assert.match(admin, /QH_ADMIN_SECTIONS = \[[^\]]*'kill'[^\]]*'conns'/);
  // The server payload is not changed by this round: no new field on a notification.
  const py = web('../src/queryhub/web/routes_queries.py');
  assert.doesNotMatch(py, /"requestId"/);
});
