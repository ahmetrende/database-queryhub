// A request that waits for a DBA, and how a request ended (2026-10-24, batch 2).
//
//   * Pending: an amber card in Results (what happens next, since when, Withdraw),
//     a pale "Waiting for a DBA" pill and a ghost Withdraw in the action bar (no
//     solid orange pill that looks like a button), and a Waiting group on top of
//     the History list. No countdown unless the request has `expiresAt`.
//   * Outcomes: Denied, Withdrawn, Expired and Changes requested are four pills
//     with four words. A filter row (All / Approved / Denied / Expired /
//     Withdrawn) sits on the History list. "Edit and resubmit" opens a NEW,
//     picked tab with the same SQL and target. A withdrawal and an expiry no
//     longer both say "Cancelled."
//   * Mock: every branch above has a seeded request in qh-api-mock.jsx.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';
import { page, sliceFunction, web, tokensCss, isVendorTokens, jsdomOpts as opts } from './_mount.mjs';

const pure = loadInto(bareWindow(), 'qh-data.jsx');
const FILES = ['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx'];

// ---------- pure rules ----------

test('each outcome has its own word, class and title', () => {
  const { qhOutcomeInfo } = pure;
  const words = ['denied', 'withdrawn', 'expired', 'changes_requested'].map(o => qhOutcomeInfo(o).label);
  assert.deepEqual(words, ['Denied', 'Withdrawn', 'Expired', 'Changes requested']);
  const classes = ['denied', 'withdrawn', 'expired', 'changes_requested'].map(o => qhOutcomeInfo(o).cls);
  assert.equal(new Set(classes).size, 4, 'four tones, not one "Rejected"');
  assert.equal(qhOutcomeInfo(null), null);
  assert.equal(qhOutcomeInfo('something new'), null);
});

test('a withdrawal and an expiry read differently, and any other line is left alone', () => {
  const { qhOutcomeMessages } = pure;
  const msgs = [{ kind: 'info', text: 'Submitted RW query as request #7 for DBA approval.', time: 't' }, { kind: 'err', text: 'Cancelled.', time: 't' }];
  assert.equal(qhOutcomeMessages(msgs, 'withdrawn')[1].text, 'Withdrawn by you.');
  assert.equal(qhOutcomeMessages(msgs, 'expired')[1].text, 'Expired: nobody decided in time.');
  assert.equal(qhOutcomeMessages(msgs, 'withdrawn')[0].text, msgs[0].text);
  assert.equal(qhOutcomeMessages(msgs, 'withdrawn')[1].kind, 'err', 'the kind stays');
  // No outcome: the server text stays (an older server, or another status).
  assert.equal(qhOutcomeMessages(msgs, null), msgs);
  assert.equal(qhOutcomeMessages(msgs, 'denied')[1].text, 'Cancelled.');
  // A line that is not the generic one is never replaced.
  const note = [{ kind: 'err', text: 'Rejected by dba.amara: Run this on staging first.', time: 't' }];
  assert.equal(qhOutcomeMessages(note, 'withdrawn')[0].text, note[0].text);
  assert.equal(qhOutcomeMessages(undefined, 'withdrawn'), undefined);
});

test('a history row carries outcome and createdAt, from ONE mapper', () => {
  const row = pure.qhHistoryRow({ id: '5', sql: 'SELECT 1', connectionId: 'c', databaseId: 'd', tier: 'RO', status: 'rejected',
    outcome: 'withdrawn', createdAt: '2026-10-24T10:00:00Z', rowCount: null, approver: null });
  assert.equal(row.outcome, 'withdrawn');
  assert.equal(row.createdAt, '2026-10-24T10:00:00Z');
  assert.equal(row.conn, 'c');
  assert.equal(pure.qhHistoryRow({ id: '6', sql: 'x', status: 'done' }).outcome, null, 'an older server sends none');
  const app = web('qh-app.jsx');
  assert.equal((app.match(/\.map\(qhHistoryRow\)/g) || []).length, 2, 'the first load and the refresh both use it');
  assert.doesNotMatch(app, /when: qhTimeAgo\(h\.createdAt\)/);
});

test('the filter groups: approved, denied, expired, withdrawn; pending and changes only under All', () => {
  const { qhHistoryFilter, qhHistoryKind } = pure;
  const rows = [
    { id: 1, status: 'done' }, { id: 2, status: 'failed', awaitingDba: true }, { id: 3, status: 'rejected', outcome: 'denied' },
    { id: 4, status: 'rejected', outcome: 'expired' }, { id: 5, status: 'rejected', outcome: 'withdrawn' },
    { id: 6, status: 'rejected', outcome: 'changes_requested' }, { id: 7, status: 'pending' }, { id: 8, status: 'rejected' },
  ];
  const ids = (k) => qhHistoryFilter(rows, k).map(r => r.id);
  assert.deepEqual(ids('all'), [1, 2, 3, 4, 5, 6, 7, 8]);
  assert.deepEqual(ids('approved'), [1, 2]);
  assert.deepEqual(ids('denied'), [3, 8], 'a rejected row with no outcome reads as denied, as before');
  assert.deepEqual(ids('expired'), [4]);
  assert.deepEqual(ids('withdrawn'), [5]);
  assert.equal(qhHistoryKind(rows[5]), 'changes_requested');
  assert.equal(qhHistoryKind(rows[6]), 'pending');
  assert.deepEqual(pure.QH_HIST_FILTERS.map(f => f[1]), ['All', 'Approved', 'Denied', 'Expired', 'Withdrawn']);
});

test('the Waiting list: open tabs first, then history rows no tab holds, longest wait first', () => {
  const { qhPendingRequests } = pure;
  const tabs = [
    { id: 't1', name: 'Fix tier', sql: 'UPDATE a SET b = 1', conn: 'prod-main', db: 'payments', status: 'pending', qid: '11', submittedAt: '2026-10-24T10:10:00Z' },
    { id: 't2', name: 'Done one', sql: 'SELECT 1', status: 'done', qid: '12' },
    { id: 'welcome', kind: 'welcome', status: 'pending', qid: '13' },
    { id: 't3', name: 'Blank', sql: '', status: 'pending' },
  ];
  const history = [
    { id: '11', status: 'pending', sql: 'dup of the tab', conn: 'c', db: 'd', createdAt: '2026-10-24T10:10:00Z' },
    { id: '20', status: 'pending', sql: 'ALTER TABLE ledger\n  ADD COLUMN memo text;', conn: 'staging', db: 'app_stg', createdAt: '2026-10-24T09:00:00Z' },
    { id: '21', status: 'rejected', sql: 'x' },
    { id: '22', status: 'pending', sql: 'no time', conn: 'c', db: 'd' },
  ];
  const got = qhPendingRequests(tabs, history);
  assert.deepEqual(got.map(p => p.id), ['20', '11', '22'], 'oldest first, unknown time last, no duplicate of an open tab');
  assert.equal(got[1].tabId, 't1');
  assert.equal(got[1].title, 'Fix tier', 'the tab name');
  assert.equal(got[0].title, 'ALTER TABLE ledger ADD COLUMN memo text;', 'else the start of the SQL, on one line');
  assert.equal(got[0].tabId, null);
  assert.deepEqual(qhPendingRequests([], []), []);
  assert.deepEqual(qhPendingRequests(null, null), []);
});

test('Edit and resubmit: same SQL and target, a picked tab, nothing sent', () => {
  const over = pure.qhResubmitOver({ name: 'Fix tier', sql: 'UPDATE a SET b = 1', conn: 'prod-main', db: 'payments', unpicked: true });
  assert.deepEqual(over, { name: 'Fix tier', sql: 'UPDATE a SET b = 1', conn: 'prod-main', db: 'payments', unpicked: false });
  assert.equal(pure.qhTabBindsOnClick({ ...over, kind: undefined }), false, 'a click in the tree never rebinds it');
  // A history row has no name: the SQL start names the tab.
  assert.equal(pure.qhResubmitOver({ sql: 'UPDATE users SET status = 1 WHERE id = 5 AND more', conn: 'c', db: 'd' }).name.length <= 24, true);
  const app = web('qh-app.jsx');
  const fn = app.slice(app.indexOf('const resubmitFrom ='), app.indexOf('// A bell item that points at a request'));
  assert.match(fn, /freshTab\(qhResubmitOver\(src\)\)/);
  assert.doesNotMatch(fn, /qhApi\./, 'client side only: the resubmit sends nothing');
  assert.doesNotMatch(fn, /unpicked: true/);
});

// ---------- the pills ----------

test('the status pill: four outcomes, four words; no outcome keeps the old pill', opts, () => {
  const ui = page(FILES);
  const pill = (props) => { ui.render(ui.React.createElement(ui.win.StatusPill, props)); return ui.doc.querySelector('.qh-status'); };
  for (const [o, word, cls] of [['denied', 'Denied', 'st-rejected'], ['withdrawn', 'Withdrawn', 'st-withdrawn'],
    ['expired', 'Expired', 'st-expired'], ['changes_requested', 'Changes requested', 'st-changes']]) {
    const p = pill({ status: 'rejected', outcome: o });
    assert.equal(p.textContent, word);
    assert.ok(p.classList.contains(cls), o + ' ' + cls);
    assert.equal(p.getAttribute('data-outcome'), o);
    assert.ok(p.getAttribute('title'), 'a title says what happened');
  }
  assert.equal(pill({ status: 'rejected' }).textContent, 'Denied', 'an older server');
  assert.equal(pill({ status: 'rejected', outcome: null }).textContent, 'Denied');
  // An outcome on a status that is not `rejected` is not shown as an end.
  assert.equal(pill({ status: 'done', outcome: 'withdrawn' }).textContent, 'Done');
  assert.equal(pill({ status: 'pending' }).textContent, 'Pending');
});

// ---------- the cards ----------

test('the amber card: what happens next, since when, Withdraw; no countdown without expiresAt', opts, () => {
  const ui = page(FILES);
  let withdrawn = 0;
  const since = new Date(Date.now() - 7 * 60000).toISOString();
  ui.render(ui.React.createElement(ui.win.PendingCard, { since, expiresAt: null, onWithdraw: () => { withdrawn++; } }));
  const card = ui.doc.querySelector('.qh-pend-card');
  assert.ok(card, 'the card');
  assert.match(card.textContent, /Waiting for a DBA/);
  assert.match(card.textContent, /A DBA decides this\. You get a Slack message and a bell item when they do\./);
  assert.match(card.textContent, /Submitted 7m ago\./);
  assert.doesNotMatch(card.textContent, /expire|minutes|min left/i, 'no countdown, no minutes left');
  const btn = [...card.querySelectorAll('button')].find(b => b.textContent === 'Withdraw');
  assert.ok(btn, 'the Withdraw button');
  ui.click(btn);
  assert.equal(withdrawn, 1);
});

test('the card names the expiry only when the request carries expiresAt', opts, () => {
  const ui = page(FILES);
  const at = new Date(Date.now() + 3600000).toISOString();
  ui.render(ui.React.createElement(ui.win.PendingCard, { since: null, expiresAt: at, onWithdraw: () => {} }));
  const t = ui.doc.querySelector('.qh-pend-card').textContent;
  assert.match(t, /If nobody decides, the request expires on /);
  assert.doesNotMatch(t, /Submitted/, 'no time of the submit: no "since" line, never "just now"');
});

test('without Slack the card does not promise a Slack message', opts, () => {
  const ui = page(FILES);
  ui.render(ui.React.createElement(ui.win.PendingCard, { since: null, expiresAt: null, onWithdraw: () => {}, slackEnabled: false }));
  const t = ui.doc.querySelector('.qh-pend-card').textContent;
  assert.match(t, /You get a bell item when they do\./);
  assert.doesNotMatch(t, /Slack/);
});

test('the ended card: every outcome says what happened; only changes requested has the button', opts, () => {
  const ui = page(FILES);
  let n = 0;
  for (const o of ['denied', 'withdrawn', 'expired', 'changes_requested']) {
    ui.render(ui.React.createElement(ui.win.EndedCard, { outcome: o, onResubmit: () => { n++; } }));
    const card = ui.doc.querySelector('.qh-end-card');
    assert.equal(card.getAttribute('data-outcome'), o);
    assert.match(card.textContent, /Nothing ran\./);
    const b = [...card.querySelectorAll('button')].find(x => x.textContent === 'Edit and resubmit');
    assert.equal(!!b, o === 'changes_requested', o);
    if (b) ui.click(b);
  }
  assert.equal(n, 1);
});

test('the ended card shows the DBA note for a denial and for changes, and never invents one', opts, () => {
  const { qhEndNote } = pure;
  const msgs = [{ kind: 'info', text: 'Submitted RW query as request #5 for DBA approval.' }, { kind: 'err', text: 'Rejected by dba.marco: Add a WHERE clause on the batch id.' }];
  assert.equal(qhEndNote(msgs, 'changes_requested'), 'Rejected by dba.marco: Add a WHERE clause on the batch id.');
  assert.equal(qhEndNote(msgs, 'denied'), 'Rejected by dba.marco: Add a WHERE clause on the batch id.');
  assert.equal(qhEndNote(msgs, 'withdrawn'), null, 'a withdrawal has no DBA note');
  assert.equal(qhEndNote(msgs, 'expired'), null);
  assert.equal(qhEndNote([msgs[0]], 'denied'), null, 'no line, no note');
  assert.equal(qhEndNote(undefined, 'denied'), null);
  const ui = page(FILES);
  ui.render(ui.React.createElement(ui.win.EndedCard, { outcome: 'changes_requested', note: msgs[1].text, onResubmit: () => {} }));
  assert.equal(ui.doc.querySelector('.qh-end-note').textContent, msgs[1].text);
  ui.render(ui.React.createElement(ui.win.EndedCard, { outcome: 'withdrawn', note: null }));
  assert.equal(ui.doc.querySelector('.qh-end-note'), null);
});

test('a request that ends with an outcome lands on Results (the card), any other end on Messages', () => {
  const app = web('qh-app.jsx');
  assert.match(app, /setResTab\(sres\.status === 'rejected' && qhOutcomeInfo\(sres\.outcome\) \? 'results' : 'messages'\)/);
  assert.match(app, /endNote=\{qhEndNote\(tab\.messages, tab\.outcome\)\}/);
});

test('the results panel shows the card in place of "Run a query"', opts, () => {
  const ui = page(FILES);
  const Panel = ui.win.ResultsPanel;
  const base = { tab: 'results', setTab: () => {}, result: null, messages: [], audit: [], runMs: null, onExport: () => {}, plan: null, onToast: () => {}, colMeta: {}, reqId: '7', unmasked: false, conn: null, onStatement: () => {}, ranOn: null };
  ui.render(ui.React.createElement(Panel, { ...base, status: 'pending', pending: { since: null, expiresAt: null }, onWithdraw: () => {} }));
  assert.ok(ui.doc.querySelector('.qh-pend-card'));
  assert.equal(ui.doc.querySelector('.qh-empty'), null, 'not "Run a query to see results here."');
  ui.render(ui.React.createElement(Panel, { ...base, status: 'rejected', outcome: 'changes_requested', onResubmit: () => {} }));
  assert.ok(ui.doc.querySelector('.qh-end-card'));
  assert.equal(ui.doc.querySelector('.qh-res-head .qh-status').textContent, 'Changes requested', 'the header pill carries the outcome');
  ui.render(ui.React.createElement(Panel, { ...base, status: null }));
  assert.equal(ui.doc.querySelector('.qh-pend-card'), null);
  assert.ok(ui.doc.querySelector('.qh-empty'), 'a blank tab keeps its empty state');
  ui.render(ui.React.createElement(Panel, { ...base, status: 'rejected' }));
  assert.equal(ui.doc.querySelector('.qh-end-card'), null, 'a rejected request with no outcome keeps the old view');
});

// ---------- the action bar ----------

function mountBar(extra) {
  const ui = page(FILES);
  const w = ui.win;
  w.useRef = ui.React.useRef; w.useState = ui.React.useState; w.useEffect = ui.React.useEffect;
  w.QH_KBD = { run: 'Ctrl Enter', newq: '', wrap: '', stmt: '' };
  const Bar = sliceFunction('qh-app.jsx', 'ActionBar', w);
  const calls = { cancel: 0, primary: 0 };
  const props = { pickHint: false, redacted: 0, onRevealRedacted: () => {}, why: '', onWhy: () => {}, conn: null, db: null, connAlias: 'c', dbAlias: 'd', dbTier: 'RW',
    classify: { tier: 'RW', empty: false, multi: false, statements: [] }, pii: { columns: [], star: false }, autoApprove: false, tierExceedsGrant: false,
    busy: false, status: null, hasSql: true, onPrimary: () => { calls.primary++; }, killed: false, riskHints: [], riskTop: 'low', onExplain: () => {}, tabCount: 1,
    onOpenBatch: () => {}, schedOpen: false, setSchedOpen: () => {}, onSchedule: () => {}, onCancelRun: () => { calls.cancel++; }, isSuper: false, unmasked: false,
    onUnmask: () => {}, runOnConn: null, runOn: null, onRunOn: () => {}, ...extra };
  ui.render(ui.React.createElement(Bar, props));
  return { ...ui, calls };
}

test('action bar, pending: a pale status pill and a ghost Withdraw, no solid pill', opts, () => {
  const ui = mountBar({ busy: true, status: 'pending' });
  const pill = ui.doc.querySelector('.qh-wait-pill');
  assert.ok(pill, 'the pill');
  assert.equal(pill.textContent, 'Waiting for a DBA');
  assert.notEqual(pill.tagName, 'BUTTON', 'a status is not a button');
  assert.equal(ui.doc.querySelector('.qh-run'), null, 'no solid orange pill that looks clickable');
  assert.doesNotMatch(ui.doc.body.textContent, /Awaiting DBA approval/);
  const w = ui.doc.querySelector('.qh-cancel-run');
  assert.equal(w.textContent, 'Withdraw');
  assert.ok(w.classList.contains('qh-btn-ghost'));
  ui.click(w);
  assert.equal(ui.calls.cancel, 1, 'the same action the old button ran');
});

test('action bar, any other state: the run button is back and there is no pill', opts, () => {
  for (const [busy, status, label] of [[false, null, 'Submit for DBA approval'], [true, 'approved', 'Approved — running…'], [true, 'running', 'Running…']]) {
    const ui = mountBar({ busy, status });
    assert.equal(ui.doc.querySelector('.qh-wait-pill'), null, status + ' has no pill');
    assert.equal(ui.doc.querySelector('.qh-run').textContent, label);
  }
  const ui = mountBar({ busy: true, status: 'running' });
  assert.equal(ui.doc.querySelector('.qh-cancel-run').textContent, 'Stop');
});

// ---------- the sidebar ----------

const ROWS = [
  { id: '1', sql: 'SELECT 1', conn: 'c', db: 'd', tier: 'RO', status: 'done', when: '1h ago', approver: 'auto-approve', outcome: null },
  { id: '2', sql: 'DELETE FROM a', conn: 'c', db: 'd', tier: 'RW', status: 'rejected', when: '2h ago', outcome: 'denied' },
  { id: '3', sql: 'UPDATE b SET x = 1', conn: 'c', db: 'd', tier: 'RW', status: 'rejected', when: '3h ago', outcome: 'withdrawn' },
  { id: '4', sql: 'CREATE INDEX i', conn: 'c', db: 'd', tier: 'DDL', status: 'rejected', when: '1d ago', outcome: 'expired' },
  { id: '5', sql: 'UPDATE users SET z = 2', conn: 'prod-main', db: 'users', tier: 'RW', status: 'rejected', when: '5h ago', outcome: 'changes_requested' },
  { id: '6', sql: 'UPDATE c SET y = 3', conn: 'prod-main', db: 'payments', tier: 'RW', status: 'pending', when: '7m ago', outcome: null, createdAt: new Date(Date.now() - 7 * 60000).toISOString() },
];
function mountHistory(extra, prep) {
  const ui = page(FILES, prep ? { prep } : undefined);
  const noop = () => {};
  const calls = { pending: [], resub: [], load: [] };
  ui.render(ui.React.createElement(ui.win.Sidebar, {
    mode: 'history', setMode: noop, conns: [], connsLoading: false, schemaCache: {}, onLoadSchema: noop, onLoadRoles: noop, rolesCache: {},
    active: { conn: null, db: null }, onPick: noop, bindOnClick: false, saved: [], onLoadSaved: noop, onDeleteSaved: noop,
    sessions: [], scheduled: [], history: ROWS, onLoadHistory: (h) => calls.load.push(h.id), onNewQuery: noop, onNewTab: noop, onOpenTable: noop,
    onRequestEndpoint: noop, onRequestAuto: noop, onToast: noop, canRefresh: false, isSuper: false,
    pending: [{ id: '6', tabId: null, title: 'UPDATE c SET y = 3', sql: 'UPDATE c SET y = 3', conn: 'prod-main', db: 'payments', since: ROWS[5].createdAt }],
    onOpenPending: (p) => calls.pending.push(p.id), onResubmit: (h) => calls.resub.push(h.id), ...extra }));
  return { ...ui, calls };
}
const histIds = (ui) => ui.all('.qh-hist-item').map(e => e.getAttribute('data-hist-id'));

test('history: a filter chip row, and each chip shows its own group', opts, () => {
  const ui = mountHistory();
  assert.deepEqual(ui.all('.qh-hfilter').map(b => b.textContent), ['All', 'Approved', 'Denied', 'Expired', 'Withdrawn']);
  assert.deepEqual(histIds(ui), ['1', '2', '3', '4', '5', '6']);
  const chip = (t) => ui.all('.qh-hfilter').find(b => b.textContent === t);
  ui.click(chip('Denied'));
  assert.deepEqual(histIds(ui), ['2']);
  assert.equal(chip('Denied').getAttribute('aria-pressed'), 'true');
  ui.click(chip('Expired'));
  assert.deepEqual(histIds(ui), ['4']);
  ui.click(chip('Withdrawn'));
  assert.deepEqual(histIds(ui), ['3']);
  ui.click(chip('Approved'));
  assert.deepEqual(histIds(ui), ['1']);
  ui.click(chip('All'));
  assert.equal(histIds(ui).length, 6);
});

test('history: each row carries its own outcome pill', opts, () => {
  const ui = mountHistory();
  const word = (id) => ui.all('.qh-hist-item').find(e => e.getAttribute('data-hist-id') === id).querySelector('.qh-status').textContent;
  assert.deepEqual(['1', '2', '3', '4', '5', '6'].map(word), ['Done', 'Denied', 'Withdrawn', 'Expired', 'Changes requested', 'Pending']);
});

test('history: an empty filter says so', opts, () => {
  const ui = mountHistory({ history: ROWS.filter(r => r.outcome !== 'expired') });
  ui.click(ui.all('.qh-hfilter').find(b => b.textContent === 'Expired'));
  assert.equal(histIds(ui).length, 0);
  assert.match(ui.doc.querySelector('.qh-side-empty').textContent, /Nothing here/);
});

test('history: the Waiting group is on top, amber, with target and since; a click opens it', opts, () => {
  const ui = mountHistory();
  const group = ui.doc.querySelector('.qh-wait-group');
  assert.ok(group);
  const body = group.closest('.qh-side-body');
  assert.equal(body.firstElementChild, group, 'on top of the list');
  const row = group.querySelector('.qh-wait-row');
  assert.match(row.textContent, /UPDATE c SET y = 3/);
  assert.match(row.textContent, /prod-main · payments · 7m ago/);
  assert.match(group.querySelector('.qh-wait-title').textContent, /Waiting1/);
  ui.click(row);
  assert.deepEqual(ui.calls.pending, ['6']);
});

test('history: no Waiting group when nothing waits', opts, () => {
  const ui = mountHistory({ pending: [] });
  assert.equal(ui.doc.querySelector('.qh-wait-group'), null);
});

test('history: Edit and resubmit shows on a changes-requested row only, and hands over the row', opts, () => {
  const ui = mountHistory();
  const btns = ui.all('.qh-hist-resub');
  assert.equal(btns.length, 1);
  assert.equal(btns[0].textContent, 'Edit and resubmit');
  assert.equal(btns[0].closest('.qh-hist-item').getAttribute('data-hist-id'), '5');
  ui.click(btns[0]);
  assert.deepEqual(ui.calls.resub, ['5']);
  assert.deepEqual(ui.calls.load, [], 'the row click is a different act');
});

test('history: a bell item scrolls to its row and marks it, then the mark fades', opts, async () => {
  const seen = [];
  // jsdom has no scrollIntoView: give every element one before the effect looks.
  const ui = mountHistory({ histFocus: { id: '3', n: 1 } }, (win) => {
    win.Element.prototype.scrollIntoView = function () { seen.push(this.getAttribute('data-hist-id')); };
  });
  await ui.flush();
  assert.deepEqual(seen, ['3'], 'the row of the request');
  assert.ok(ui.all('.qh-hist-item').find(e => e.getAttribute('data-hist-id') === '3').classList.contains('is-flash'));
  assert.ok(!ui.all('.qh-hist-item').find(e => e.getAttribute('data-hist-id') === '2').classList.contains('is-flash'));
});

test('history: a bell item for a request that is not in the list scrolls nowhere and marks nothing', opts, async () => {
  const seen = [];
  const ui = mountHistory({ histFocus: { id: '999', n: 1 } }, (win) => {
    win.Element.prototype.scrollIntoView = function () { seen.push(1); };
  });
  await ui.flush();
  assert.deepEqual(seen, []);
  assert.equal(ui.all('.qh-hist-item.is-flash').length, 0);
});

// ---------- source rules ----------

test('the app wires the pieces: status payload, Waiting list, card props', () => {
  const app = web('qh-app.jsx');
  assert.match(app, /messages: qhOutcomeMessages\(msgs, sres\.outcome \|\| null\)/);
  assert.match(app, /outcome: sres\.outcome \|\| null, expiresAt: sres\.expiresAt \|\| null/);
  assert.match(app, /const pendingReqs = qhPendingRequests\(tabs, history\)/);
  assert.match(app, /pending=\{pendingReqs\} onOpenPending=\{openPending\} onResubmit=\{resubmitFrom\}/);
  assert.match(app, /pending=\{tab\.status === 'pending' \? \{ since: tab\.submittedAt \|\| null, expiresAt: tab\.expiresAt \|\| null \} : null\}/);
  assert.match(app, /onWithdraw=\{cancelRun\}/, 'the card runs the same action as the action bar');
  // A request that waits shows its card, and the history is read again for the sidebar.
  assert.match(app, /if \(r\.status === 'pending'\) \{ setResTab\('results'\); refreshHistory\(\); \}/);
});

test('openPending: its own tab when one is open, else a tab that tracks the request', () => {
  const app = web('qh-app.jsx');
  const fn = app.slice(app.indexOf('const openPending ='), app.indexOf('// "Edit and resubmit"'));
  assert.match(fn, /tabs\.find\(x => !x\.kind && x\.qid && String\(x\.qid\) === p\.id\)/);
  assert.match(fn, /status: 'pending'/);
  assert.match(fn, /track\(f\.id, p\.id\)/);
  assert.doesNotMatch(fn, /unpicked: true/);
});

test('the styles: dated block, tokens only, a dark partner is not needed', () => {
  const css = web('QueryHub.html');
  assert.match(css, /2026-10-24 · Request lifecycle, outcomes, Welcome lists, bell \(batch 2\)/);
  const at = css.indexOf('2026-10-24 · Request lifecycle');
  const block = css.slice(css.lastIndexOf('/*', at), css.indexOf('</style>'));
  assert.doesNotMatch(block.replace(/\/\*[\s\S]*?\*\//g, ''), /#[0-9a-fA-F]{3,8}\b/, 'tokens only');
  for (const sel of ['.qh-wait-pill', '.st-withdrawn', '.st-expired', '.st-changes', '.qh-pend-card', '.qh-end-card', '.qh-wait-group', '.qh-hfilter']) {
    assert.ok(block.includes(sel + ' {') || block.includes(sel + ','), sel);
  }
});

// ---------- contrast, computed ----------
function hexOf(name, theme) {
  const tokens = tokensCss();
  const cut = tokens.indexOf("[data-theme='dark']");
  const re = new RegExp(name + ':\\s*(#[0-9A-Fa-f]{6})');
  const m = theme === 'dark' ? (tokens.slice(cut).match(re) || tokens.slice(0, cut).match(re)) : tokens.slice(0, cut).match(re);
  assert.ok(m, name + ' ' + theme);
  return m[1];
}
const rgb = (h) => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));
const lin = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
const lum = (c) => 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2]);
const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
const over = (tint, a, bg) => tint.map((v, i) => Math.round(v * a + bg[i] * (1 - a)));

test('the new pills and the card text pass 4.5:1 on white and on the light panel, light and dark', () => {
  const amber = [245, 158, 11], inkL = [31, 34, 41], inkD = [255, 255, 255];
  for (const [theme, panels] of [['light', ['#FFFFFF', '#F9FAFB']], ['dark', ['#181A20', '#1F2229']]]) {
    const warn = rgb(hexOf('--warning-solid-strong', theme));
    const sys = rgb(hexOf('--system-solid-light', theme));
    for (const p of panels) {
      const bg = rgb(p);
      const amberTint = over(amber, 0.10, bg);
      const neutral = over(theme === 'light' ? inkL : inkD, 0.04, bg);
      assert.ok(ratio(warn, amberTint) >= 4.5, 'waiting / changes ' + theme + ' ' + p + ' ' + ratio(warn, amberTint));
      assert.ok(ratio(sys, neutral) >= 4.5, 'withdrawn / expired ' + theme + ' ' + p + ' ' + ratio(sys, neutral));
      // The card's body text is --fg-primary (opaque) on the amber tint.
      const fg = rgb(theme === 'light' ? '#1F2229' : '#FFFFFF');
      assert.ok(ratio(fg, amberTint) >= 4.5, 'card text ' + theme);
    }
  }
  if (isVendorTokens()) assert.equal(ratio(rgb(hexOf('--warning-solid-strong', 'light')), over([245, 158, 11], 0.10, rgb('#FFFFFF'))).toFixed(2), '6.56');
  if (isVendorTokens()) assert.equal(ratio(rgb(hexOf('--system-solid-light', 'light')), over([31, 34, 41], 0.04, rgb('#FFFFFF'))).toFixed(2), '5.75');
});

// ---------- the mock covers every branch ----------

test('the mock seeds a pending request and one request for each outcome', () => {
  const mock = web('qh-api-mock.jsx');
  for (const end of ["'pending'", "'denied'", "'withdrawn'", "'expired'", "'changes_requested'"]) {
    assert.ok(mock.includes("end: " + end), end);
  }
  assert.match(mock, /text: 'Cancelled\.'/, 'the generic server line, so the UI wording is exercised');
  assert.match(mock, /expiresAt: null/, 'the dev routes send no expiresAt');
});
