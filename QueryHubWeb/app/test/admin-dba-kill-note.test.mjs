// Three admin changes of 2026-10-23, mounted for real (jsdom).
//
//   * The DBA level. GET /me sends `admin.isDba`. A DBA (not a super-admin)
//     opens the kill switch with its button, Effective access and Connections
//     (read-only). Every section they cannot open is a LOCKED row with a title,
//     not a missing one. An admin below the DBA level gets no kill button.
//   * The kill switch history. GET /admin/kill/history lists the pauses. After a
//     switch to ON, the screen says how many reads QueryHub cancelled, and that
//     writes keep running. A missing count is said as unknown, never as zero.
//   * The note on Reject and Request changes. It needs 15 trimmed characters.
//     The send button is disabled until then and says why in its title. Chips
//     add a sentence.
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
const ADMIN = { name: 'Jordan Ray', role: 'dba', isDba: false };
const OLD = { name: 'Jordan Ray', role: 'dba' };            // a server without isDba

const pure = loadInto(bareWindow(), 'qh-data.jsx', 'qh-admin-data.jsx', 'qh-admin.jsx');

// ---------- pure rules ----------

test('the DBA level opens the kill switch, Effective access and Connections, nothing super-only', () => {
  const { qhAdminCanOpen } = pure;
  for (const id of ['approvals', 'kill', 'effective', 'conns', 'roles', 'audit', 'metrics', 'feedback'])
    assert.equal(qhAdminCanOpen(id, DBA), true, id);
  for (const id of ['grants', 'auto', 'scopes', 'teams', 'mask', 'config'])
    assert.equal(qhAdminCanOpen(id, DBA), false, id);
});

test('an admin below the DBA level, or a server without isDba, opens neither DBA section', () => {
  const { qhAdminCanOpen, qhAdminLevel } = pure;
  for (const u of [ADMIN, OLD]) {
    assert.equal(qhAdminLevel(u), 'admin');
    assert.equal(qhAdminCanOpen('effective', u), false);
    assert.equal(qhAdminCanOpen('conns', u), false);
    assert.equal(qhAdminCanOpen('kill', u), true);
    assert.equal(qhAdminCanOpen('roles', u), true);
  }
  // A truthy value that is not `true` is not the DBA level.
  assert.equal(qhAdminLevel({ role: 'dba', isDba: 'yes' }), 'admin');
});

test('a super-admin opens every section', () => {
  const { qhAdminCanOpen } = pure;
  for (const id of ['effective', 'conns', 'grants', 'auto', 'scopes', 'teams', 'mask', 'config'])
    assert.equal(qhAdminCanOpen(id, SUPER), true, id);
});

test('the lock reason names who opens the section', () => {
  assert.equal(pure.qhAdminLockReason('grants'), 'Super-admin only');
  assert.equal(pure.qhAdminLockReason('effective'), 'DBA or super-admin only');
});

test('lasted: minutes, hours, days, and nothing for a missing or reversed time', () => {
  const { qhKillLasted } = pure;
  const t0 = '2026-10-01T10:00:00Z';
  assert.equal(qhKillLasted(t0, '2026-10-01T10:00:42Z'), '42s');
  assert.equal(qhKillLasted(t0, '2026-10-01T10:47:00Z'), '47m');
  assert.equal(qhKillLasted(t0, '2026-10-01T13:05:00Z'), '3h 5m');
  assert.equal(qhKillLasted(t0, '2026-10-01T12:00:00Z'), '2h');
  assert.equal(qhKillLasted(t0, '2026-10-03T14:00:00Z'), '2d 4h');
  assert.equal(qhKillLasted(t0, null), '');
  assert.equal(qhKillLasted(null, t0), '');
  assert.equal(qhKillLasted('2026-10-01T11:00:00Z', t0), '');
});

test('the cancel sentence: a number, zero, and an unknown count', () => {
  const { qhKillCancelSay } = pure;
  const writes = /Writes and schema changes continue, because a cancel can leave a change half done\./;
  assert.match(qhKillCancelSay(3), /^QueryHub cancelled 3 running reads\./);
  assert.match(qhKillCancelSay(1), /^QueryHub cancelled 1 running read\./);
  assert.match(qhKillCancelSay(0), /^No read was running/);
  assert.match(qhKillCancelSay(null), /^QueryHub could not count the cancelled reads\./);
  assert.match(qhKillCancelSay(undefined), /could not count/);
  for (const n of [3, 0, null]) assert.match(qhKillCancelSay(n), writes);
});

test('the note counts trimmed characters, one per letter', () => {
  const { qhNoteCheck, QH_NOTE_MIN } = pure;
  assert.equal(QH_NOTE_MIN, 15);
  assert.deepEqual(qhNoteCheck('   '), { n: 0, ok: false, count: '0 of 15',
    why: 'Write at least 15 characters. The submitter reads this note.' });
  assert.equal(qhNoteCheck('  Too short.  ').count, '10 of 15');
  assert.equal(qhNoteCheck('éééééééééééééé').ok, false);          // 14 letters
  assert.equal(qhNoteCheck('ééééééééééééééé').ok, true);           // 15 letters
  assert.equal(qhNoteCheck('Add a WHERE clause.').count, '19 characters');
  assert.equal(qhNoteCheck('Add a WHERE clause.').why, null);
  assert.equal(qhNoteCheck(null).n, 0);
});

test('a chip adds its sentence once, after a full stop', () => {
  const { qhNoteAdd } = pure;
  assert.equal(qhNoteAdd('', 'Run this on a test database first.'), 'Run this on a test database first.');
  assert.equal(qhNoteAdd('Wrong table', 'Add a WHERE clause.'), 'Wrong table. Add a WHERE clause.');
  assert.equal(qhNoteAdd('Wrong table.', 'Add a WHERE clause.'), 'Wrong table. Add a WHERE clause.');
  assert.equal(qhNoteAdd('Add a WHERE clause.', 'Add a WHERE clause.'), 'Add a WHERE clause.');
});

// ---------- mounted ----------

function mount(win0, View, props) {
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
  // The other screens are not under test. A stub records the props it got.
  const seen = {};
  for (const name of ['ConnectionsView', 'RolesView', 'GrantsView', 'AutoView', 'ScopesView', 'TeamsView',
                      'MaskingView', 'AuditView', 'MetricsView', 'FeedbackView', 'SystemConfigView']) {
    win[name] = (p) => { seen[name] = p; return React.createElement('div', { className: 'stub-' + name }); };
  }
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(win[View], props(win))));
  const doc = win.document;
  const all = (sel) => [...doc.querySelectorAll(sel)];
  const flush = () => React.act(() => new Promise(r => setTimeout(r, 0)));
  return { win, doc, all, seen, React, flush, act: (fn) => React.act(fn) };
}

const QUEUE = [{ id: 'q1', submitter: { name: 'Jordan Ray', initials: 'JR', slackId: 'U0EXAMPLE001' },
  connectionId: 'prod-main', databaseId: 'orders', env: 'production', tier: 'RW', sql: 'UPDATE t SET a = 1 WHERE id = 2;',
  statements: 1, piiCols: [], estRows: 1, estTables: ['t'], reason: 'Fix one row.', submittedAt: '2026-10-23T08:00:00Z' }];

function baseSt(extra) {
  return { queue: QUEUE, manualRuns: [], endpointReqs: [], autoRequests: [], grants: [], people: [], teams: [],
    killSwitch: { enabled: false, message: '', by: null, at: null },
    killHistory: () => Promise.resolve([]), toggleKill: () => Promise.resolve({ enabled: true, cancelledCount: 0 }),
    decide: () => {}, batchApprove: () => {}, approveBundle: () => {}, ...extra };
}

test('a DBA sees locked rows with a title, and Connections opens read-only', opts, () => {
  const ui = mount(null, 'AdminPanel', () => ({ st: baseSt(), user: DBA, modeEntry: null }));
  const locked = ui.all('.qh-anav-item.is-locked');
  assert.deepEqual(locked.map(b => b.textContent.trim()),
    ['Grants', 'Auto-approve', 'Admin scopes', 'Teams', 'Masking exemptions', 'System configuration']);
  for (const b of locked) {
    assert.equal(b.getAttribute('aria-disabled'), 'true');
    assert.equal(b.title, 'Super-admin only');
    assert.ok(b.querySelector('.qh-lock svg'), 'a lock icon, not colour alone');
  }
  // A click on a locked row changes nothing.
  ui.act(() => locked[0].dispatchEvent(new ui.win.MouseEvent('click', { bubbles: true })));
  assert.ok(ui.doc.querySelector('.qh-aqueue'), 'still on the queue');
  // Connections, Effective access and Kill switch are live rows.
  const live = ui.all('.qh-anav-item:not(.is-locked)').map(b => b.textContent.trim());
  for (const name of ['Kill switch', 'Effective access', 'Roles', 'Connections']) assert.ok(live.includes(name), name);
  const conns = ui.all('.qh-anav-item').find(b => b.textContent.trim() === 'Connections');
  ui.act(() => conns.dispatchEvent(new ui.win.MouseEvent('click', { bubbles: true })));
  assert.equal(ui.seen.ConnectionsView.readOnly, true);
});

test('a super-admin sees no locked row, and Connections is not read-only', opts, () => {
  const ui = mount(null, 'AdminPanel', () => ({ st: baseSt(), user: SUPER, modeEntry: null }));
  assert.equal(ui.all('.qh-anav-item.is-locked').length, 0);
  const conns = ui.all('.qh-anav-item').find(b => b.textContent.trim() === 'Connections');
  ui.act(() => conns.dispatchEvent(new ui.win.MouseEvent('click', { bubbles: true })));
  assert.equal(ui.seen.ConnectionsView.readOnly, false);
});

test('an admin below the DBA level sees Effective access and Connections locked for DBAs', opts, () => {
  const ui = mount(null, 'AdminPanel', () => ({ st: baseSt(), user: ADMIN, modeEntry: null }));
  const byName = Object.fromEntries(ui.all('.qh-anav-item.is-locked').map(b => [b.textContent.trim(), b.title]));
  assert.equal(byName['Effective access'], 'DBA or super-admin only');
  assert.equal(byName['Connections'], 'DBA or super-admin only');
  assert.equal(byName['Roles'], undefined, 'Roles stays readable');
});

test('a DBA deep link to a super-only section lands on the queue', opts, () => {
  const ui = mount(null, 'AdminPanel', (win) => { win.history.replaceState(null, '', '#admin/grants');
    return { st: baseSt(), user: DBA, modeEntry: null }; });
  assert.ok(ui.doc.querySelector('.qh-aqueue'));
  assert.ok(!ui.seen.GrantsView, 'Grants never rendered');
});

const HIST = [
  { startedAt: '2026-10-23T08:00:00Z', by: 'jordan.ray', message: 'Replica lag.', endedAt: null, endedBy: null, cancelledCount: 3 },
  { startedAt: '2026-10-20T08:00:00Z', by: 'U0EXAMPLE002', message: null, endedAt: '2026-10-20T08:06:00Z', endedBy: 'jordan.ray', cancelledCount: 0 },
  { startedAt: '2026-09-01T08:00:00Z', by: null, message: 'Old pause.', endedAt: '2026-09-01T11:00:00Z', endedBy: null, cancelledCount: null },
];

test('the history table: who as a name, an open pause, and the three counts', opts, async () => {
  const ui = mount(null, 'AdminPanel', (win) => { win.history.replaceState(null, '', '#admin/kill');
    return { st: baseSt({ killHistory: () => Promise.resolve(HIST) }), user: ADMIN, modeEntry: null }; });
  await ui.flush();
  const rows = ui.all('.qh-kill-hist tbody tr').map(tr => [...tr.children].map(td => td.textContent.trim()));
  assert.equal(rows.length, 3);
  assert.equal(rows[0][1], 'Jordan Ray');
  assert.equal(rows[0][3], 'Still on');
  assert.equal(rows[0][4], '3 reads');
  assert.equal(rows[1][1], 'U0EXAMPLE002', 'a bare id stays an id');
  assert.equal(rows[1][2], 'No reason given');
  assert.equal(rows[1][3], '6mreleased by Jordan Ray');
  assert.equal(rows[1][4], 'None');
  assert.equal(rows[2][1], 'Not recorded');
  assert.equal(rows[2][3], '3hreleased');
  assert.equal(rows[2][4], 'Not counted');
  // Below the DBA level: no button and no reason field, only the sentence.
  assert.equal(ui.all('.qh-kill-card button').length, 0);
  assert.ok(!ui.doc.querySelector('.qh-kill-msg'));
  assert.match(ui.doc.querySelector('.qh-kill-ro').textContent, /Only a DBA or a super-admin can engage it/);
});

test('the history: empty, and a failed read with Try again', opts, async () => {
  let n = 0;
  const ui = mount(null, 'AdminPanel', (win) => { win.history.replaceState(null, '', '#admin/kill');
    return { st: baseSt({ killHistory: () => (++n === 1 ? Promise.reject(new Error('x')) : Promise.resolve([])) }), user: DBA, modeEntry: null }; });
  await ui.flush();
  assert.match(ui.doc.querySelector('.qh-kill-hist-state').textContent, /Could not load the pauses\./);
  const retry = ui.all('.qh-kill-hist-state button')[0];
  ui.act(() => retry.dispatchEvent(new ui.win.MouseEvent('click', { bubbles: true })));
  await ui.flush();
  assert.equal(ui.doc.querySelector('.qh-kill-hist-state').textContent, 'No pauses on record.');
});

test('a DBA engages the switch: the count is said, and the history is read again', opts, async () => {
  let reads = 0;
  const calls = [];
  const ui = mount(null, 'AdminPanel', (win) => { win.history.replaceState(null, '', '#admin/kill');
    return { st: baseSt({
      killHistory: () => { reads++; return Promise.resolve(reads > 1 ? HIST.slice(0, 1) : []); },
      toggleKill: (on, actor, msg) => { calls.push([on, msg]); return Promise.resolve({ enabled: true, cancelledCount: 3 }); },
    }), user: DBA, modeEntry: null }; });
  await ui.flush();
  const btn = ui.all('.qh-kill-card button').find(b => b.textContent.trim() === 'Engage kill switch');
  assert.ok(btn, 'a DBA has the button');
  ui.act(() => btn.dispatchEvent(new ui.win.MouseEvent('click', { bubbles: true })));
  await ui.flush();
  assert.deepEqual(calls, [[true, '']]);
  assert.equal(ui.doc.querySelector('.qh-kill-said').textContent,
    'QueryHub cancelled 3 running reads. Writes and schema changes continue, because a cancel can leave a change half done.');
  assert.equal(ui.doc.querySelector('.qh-kill-said').getAttribute('role'), 'status');
  assert.equal(reads, 2);
  assert.equal(ui.all('.qh-kill-hist tbody tr').length, 1);
});

test('a refused switch says no count', opts, async () => {
  const ui = mount(null, 'AdminPanel', (win) => { win.history.replaceState(null, '', '#admin/kill');
    return { st: baseSt({ toggleKill: () => Promise.resolve(null) }), user: DBA, modeEntry: null }; });
  await ui.flush();
  const btn = ui.all('.qh-kill-card button')[0];
  ui.act(() => btn.dispatchEvent(new ui.win.MouseEvent('click', { bubbles: true })));
  await ui.flush();
  assert.ok(!ui.doc.querySelector('.qh-kill-said'));
});

function typeInto(ui, el, value) {
  const setter = Object.getOwnPropertyDescriptor(ui.win.HTMLTextAreaElement.prototype, 'value').set;
  ui.act(() => { setter.call(el, value); el.dispatchEvent(new ui.win.Event('input', { bubbles: true })); });
}

for (const [mode, open, send] of [['reject', 'Reject', 'Confirm reject'], ['changes', 'Request changes', 'Send request']]) {
  test(mode + ': the note needs 15 characters, and a chip fills it', opts, () => {
    const decided = [];
    const ui = mount(null, 'AdminPanel', () => ({ st: baseSt({ decide: (...a) => decided.push(a) }), user: DBA, modeEntry: null }));
    const btn = (t) => ui.all('.qh-adetail-actions button').find(b => b.textContent.trim() === t);
    ui.act(() => btn(open).dispatchEvent(new ui.win.MouseEvent('click', { bubbles: true })));
    const box = ui.doc.querySelector('.qh-notebox textarea');
    assert.ok(box, 'the note opens');
    assert.equal(btn(send).disabled, true);
    assert.equal(btn(send).title, 'Write at least 15 characters. The submitter reads this note.');
    typeInto(ui, box, '  Too short  ');
    assert.equal(ui.doc.querySelector('.qh-notebox-n').textContent, '9 of 15');
    assert.equal(btn(send).disabled, true);
    ui.act(() => btn(send).dispatchEvent(new ui.win.MouseEvent('click', { bubbles: true })));
    assert.equal(decided.length, 0, 'a disabled send sends nothing');
    const chip = ui.all('.qh-notechip').find(b => b.textContent === 'Add a WHERE clause');
    ui.act(() => chip.dispatchEvent(new ui.win.MouseEvent('click', { bubbles: true })));
    assert.equal(ui.doc.querySelector('.qh-notebox textarea').value, 'Too short. Add a WHERE clause to limit the rows.');
    assert.equal(btn(send).disabled, false);
    assert.equal(btn(send).title, '');
    ui.act(() => btn(send).dispatchEvent(new ui.win.MouseEvent('click', { bubbles: true })));
    assert.deepEqual(decided.map(d => [d[0], d[1], d[3]]), [['q1', mode, 'Too short. Add a WHERE clause to limit the rows.']]);
  });
}

test('the chips are the four sentence-case labels, each with its text as a title', opts, () => {
  const ui = mount(null, 'AdminPanel', () => ({ st: baseSt(), user: DBA, modeEntry: null }));
  const reject = ui.all('.qh-adetail-actions button').find(b => b.textContent.trim() === 'Reject');
  ui.act(() => reject.dispatchEvent(new ui.win.MouseEvent('click', { bubbles: true })));
  const chips = ui.all('.qh-notechip');
  assert.deepEqual(chips.map(c => c.textContent), ['Try on test first', 'Add a WHERE clause', 'Link an incident', 'Inside change freeze']);
  for (const c of chips) assert.ok(pure.qhNoteCheck(c.title).ok, c.title + ' meets the minimum alone');
});

test('a note belongs to one request: another card closes it', opts, () => {
  const two = QUEUE.concat([{ ...QUEUE[0], id: 'q2', submitter: { name: 'Sam Lee', initials: 'SL', slackId: 'U0EXAMPLE003' } }]);
  const ui = mount(null, 'AdminPanel', () => ({ st: baseSt({ queue: two }), user: DBA, modeEntry: null }));
  const reject = ui.all('.qh-adetail-actions button').find(b => b.textContent.trim() === 'Reject');
  ui.act(() => reject.dispatchEvent(new ui.win.MouseEvent('click', { bubbles: true })));
  typeInto(ui, ui.doc.querySelector('.qh-notebox textarea'), 'This note is for the first request.');
  const second = ui.all('.qh-qcard-main')[1];
  ui.act(() => second.dispatchEvent(new ui.win.MouseEvent('click', { bubbles: true })));
  assert.ok(!ui.doc.querySelector('.qh-notebox'));
  ui.act(() => ui.all('.qh-adetail-actions button').find(b => b.textContent.trim() === 'Reject')
    .dispatchEvent(new ui.win.MouseEvent('click', { bubbles: true })));
  assert.equal(ui.doc.querySelector('.qh-notebox textarea').value, '');
});

test('approve needs no note', opts, () => {
  const decided = [];
  const ui = mount(null, 'AdminPanel', () => ({ st: baseSt({ decide: (...a) => decided.push(a) }), user: DBA, modeEntry: null }));
  const approve = ui.all('.qh-adetail-actions button')[0];
  ui.act(() => approve.dispatchEvent(new ui.win.MouseEvent('click', { bubbles: true })));
  assert.deepEqual(decided.map(d => [d[0], d[1]]), [['q1', 'approve']]);
});

// ---------- Effective access for a DBA ----------

const EFF = { slackId: 'U0EXAMPLE001', name: 'Jordan Ray', known: true, teams: [{ id: '7', name: 'Team Alpha' }],
  access: [], blocked: [], autoApprove: [], admin: null, rowLimitOverride: null };

test('a DBA picks people from the queue or by id, with no team view', opts, async () => {
  const asked = [];
  const ui = mount(null, 'EffectiveAccessView', () => ({ user: DBA,
    st: baseSt({ people: [{ id: 'x', handle: 'should.not.show', name: 'Hidden Person' }],
      effectiveAccess: (id) => { asked.push(id); return Promise.resolve({ ...EFF, slackId: id }); } }) }));
  assert.ok(!ui.doc.querySelector('.qh-eff-kind'), 'no People / Teams switch');
  const rows = ui.all('.qh-eff-pick-row').map(b => b.textContent);
  assert.equal(rows.length, 1);
  assert.match(rows[0], /Jordan Ray/);
  ui.act(() => ui.all('.qh-eff-pick-row')[0].dispatchEvent(new ui.win.MouseEvent('click', { bubbles: true })));
  await ui.flush();
  assert.deepEqual(asked, ['U0EXAMPLE001']);
  // A team is a name, not a link to a view that needs a super-admin.
  assert.equal(ui.all('button.qh-eff-teamlink').length, 0);
  assert.equal(ui.all('span.qh-eff-teamlink')[0].textContent, 'Team Alpha');
  // A Slack id typed in the search opens that person.
  const input = ui.doc.querySelector('.qh-search-in');
  const setter = Object.getOwnPropertyDescriptor(ui.win.HTMLInputElement.prototype, 'value').set;
  ui.act(() => { setter.call(input, 'U0EXAMPLE009'); input.dispatchEvent(new ui.win.Event('input', { bubbles: true })); });
  const look = ui.all('.qh-eff-pick-row').find(b => /Look up U0EXAMPLE009/.test(b.textContent));
  assert.ok(look);
  ui.act(() => look.dispatchEvent(new ui.win.MouseEvent('click', { bubbles: true })));
  await ui.flush();
  assert.deepEqual(asked, ['U0EXAMPLE001', 'U0EXAMPLE009']);
  // Text that is not an id offers no lookup.
  ui.act(() => { setter.call(input, 'jordan'); input.dispatchEvent(new ui.win.Event('input', { bubbles: true })); });
  assert.equal(ui.all('.qh-eff-pick-row').filter(b => /Look up/.test(b.textContent)).length, 0);
});

test('a super-admin keeps the full lists and the team links', opts, async () => {
  const ui = mount(null, 'EffectiveAccessView', () => ({ user: SUPER,
    st: baseSt({ people: [{ id: 'U0EXAMPLE001', handle: 'U0EXAMPLE001', name: 'Jordan Ray', initials: 'JR' }],
      teams: [{ id: '7', name: 'Team Alpha', members: [] }], effectiveAccess: () => Promise.resolve(EFF) }) }));
  assert.ok(ui.doc.querySelector('.qh-eff-kind'));
  ui.act(() => ui.all('.qh-eff-pick-row')[0].dispatchEvent(new ui.win.MouseEvent('click', { bubbles: true })));
  await ui.flush();
  assert.equal(ui.all('button.qh-eff-teamlink').length, 1);
});

// ---------- /me plumbing and the clients ----------

test('the app reads isDba from the admin block of /me, false when absent', () => {
  assert.match(web('qh-app.jsx'), /setUser\(m\.user \? \{ \.\.\.m\.user, isDba: !!\(m\.admin && m\.admin\.isDba === true\) \} : null\)/);
});

test('both clients have the history method, and the mock answers the real shapes', () => {
  assert.match(web('qh-api.jsx'), /adminKillHistory:\(\)\s*=> qhFetch\('\/admin\/kill\/history'\)/);
  const mock = web('qh-api-mock.jsx');
  assert.match(mock, /adminKillHistory: \(\) => mockDelay/);
  assert.match(mock, /cancelledCount: b\.enabled \? 2 : 0/);
  assert.match(mock, /isDba: u\.role === 'super' \|\| \(u\.role === 'dba'/);
  // One seeded pause per count branch: a number, zero, and null.
  for (const re of [/cancelledCount: 3 \}/, /cancelledCount: 0 \}/, /cancelledCount: null \}/]) assert.match(mock, re);
});
