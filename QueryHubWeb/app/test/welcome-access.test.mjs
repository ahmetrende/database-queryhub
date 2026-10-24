// Welcome (2026-10-24, batch 2): two lists above the cards, "Runs at once" and
// "Goes to a DBA first", built from the connections the person holds.
//   * Runs at once: read-only on a database that auto-approves (qhAutoApproveRO,
//     the database overrides the connection).
//   * Goes to a DBA first: read/write, schema changes, and read-only without
//     auto-approve.
//   * One row per database: tier chip, database, server. Six rows each, then "+N more".
//   * A super-admin gets one line: "You run everything at once".
//   * The API sends no grant end dates, so none is shown.
//   * Loading and empty states say so. The existing cards stay.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';
import { page, web, jsdomOpts as opts } from './_mount.mjs';

const pure = loadInto(bareWindow(), 'qh-data.jsx');
const FILES = ['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx', 'qh-home.jsx'];

const db = (id, tier, extra) => ({ id, name: id, tier, ...extra });
const CONNS = [
  { id: 'a', name: 'alpha-main', autoApproveRO: true, databases: [db('orders', 'RO'), db('billing', 'RW'), db('audit', 'RO', { autoApproveRO: false })] },
  { id: 'b', name: 'beta-main', autoApproveRO: false, databases: [db('users', 'RO'), db('events', 'RO', { autoApproveRO: true }), db('schema_dev', 'DDL')] },
  { id: 'c', name: 'gamma-old', disabled: true, autoApproveRO: true, databases: [db('legacy', 'RO')] },
];

test('the rule: read-only on an auto-approving database runs at once; the rest goes to a DBA', () => {
  const sum = pure.qhAccessSummary(CONNS, false);
  assert.equal(sum.everything, false);
  assert.deepEqual(sum.once.map(r => r.key), ['a/orders', 'b/events']);
  assert.deepEqual(sum.dba.map(r => r.key), ['a/billing', 'a/audit', 'b/users', 'b/schema_dev']);
  // `grantEndsAt` is null when the database entry carries no end (2026-10-24).
  assert.deepEqual(sum.once[0], { key: 'a/orders', server: 'alpha-main', db: 'orders', tier: 'RO', grantEndsAt: null });
});

test('read/write and schema changes go to a DBA even where reads auto-approve', () => {
  const sum = pure.qhAccessSummary([{ id: 'a', name: 'alpha-main', autoApproveRO: true, databases: [db('x', 'RW'), db('y', 'DDL')] }], false);
  assert.deepEqual(sum.once, []);
  assert.deepEqual(sum.dba.map(r => r.tier), ['RW', 'DDL']);
});

test('a database setting beats the connection setting, both ways', () => {
  const sum = pure.qhAccessSummary([{ id: 'a', name: 'n', autoApproveRO: true, databases: [db('off', 'RO', { autoApproveRO: false })] },
    { id: 'b', name: 'm', autoApproveRO: false, databases: [db('on', 'RO', { autoApproveRO: true })] }], false);
  assert.deepEqual(sum.once.map(r => r.key), ['b/on']);
  assert.deepEqual(sum.dba.map(r => r.key), ['a/off']);
});

test('a disabled connection is left out, and nothing breaks on no data', () => {
  assert.equal(pure.qhAccessSummary(CONNS, false).once.concat(pure.qhAccessSummary(CONNS, false).dba).some(r => r.server === 'gamma-old'), false);
  assert.deepEqual(pure.qhAccessSummary(null, false), { everything: false, once: [], dba: [] });
  assert.deepEqual(pure.qhAccessSummary([{ id: 'z' }], false), { everything: false, once: [], dba: [] }, 'a connection with no databases');
});

test('a super-admin runs everything at once', () => {
  assert.deepEqual(pure.qhAccessSummary(CONNS, true), { everything: true, once: [], dba: [] });
});

// ---------- mounted ----------

function mount(props) {
  const ui = page(FILES);
  ui.render(ui.React.createElement(ui.win.AccessSummary, { conns: CONNS, loading: false, isSuper: false, ...props }));
  return ui;
}
const titles = (ui) => ui.all('.qh-access-card .qh-home-card-title').map(e => e.firstChild.textContent);

test('two lists with their titles, one row per database with tier chip, name and server', opts, () => {
  const ui = mount();
  assert.deepEqual(titles(ui), ['Runs at once', 'Goes to a DBA first']);
  const once = ui.all('.qh-access-card')[0];
  const rows = once.querySelectorAll('.qh-access-row');
  assert.equal(rows.length, 2);
  assert.equal(rows[0].querySelector('.qh-tier').textContent, 'RO');
  assert.equal(rows[0].querySelector('.qh-home-row-name').textContent, 'orders');
  assert.equal(rows[0].querySelector('.qh-home-row-meta').textContent, 'alpha-main');
  const dba = ui.all('.qh-access-card')[1].querySelectorAll('.qh-access-row');
  assert.deepEqual([...dba].map(r => r.querySelector('.qh-tier').textContent), ['RW', 'RO', 'RO', 'DDL']);
  assert.equal(ui.all('.qh-access-card .qh-home-card-count').map(e => e.textContent).join(','), '2,4');
});

test('six rows each, then "+N more"', opts, () => {
  const many = [{ id: 'a', name: 'alpha-main', autoApproveRO: false, databases: Array.from({ length: 9 }, (_, i) => db('d' + i, 'RW')) }];
  const ui = mount({ conns: many });
  const dba = ui.all('.qh-access-card')[1];
  assert.equal(dba.querySelectorAll('.qh-access-row').length, 6);
  assert.equal(dba.querySelector('.qh-access-more').textContent, '+3 more');
  assert.equal(ui.all('.qh-access-card')[0].querySelector('.qh-access-more'), null);
  const exactly = mount({ conns: [{ id: 'a', name: 'n', databases: Array.from({ length: 6 }, (_, i) => db('d' + i, 'RW')) }] });
  assert.equal(exactly.doc.querySelector('.qh-access-more'), null, 'six is not "more"');
});

test('a super-admin sees one line and no lists', opts, () => {
  const ui = mount({ isSuper: true });
  assert.equal(ui.doc.querySelector('.qh-access-all').textContent, 'You run everything at once. No query waits for a DBA.');
  assert.equal(ui.all('.qh-access-card').length, 0);
});

test('loading says so: placeholders, no counts, no empty text', opts, () => {
  const ui = mount({ conns: [], loading: true });
  assert.equal(ui.all('.qh-home-skel').length, 2);
  assert.equal(ui.all('.qh-access-card .qh-home-card-count').length, 0);
  assert.equal(ui.all('.qh-skel-sr').map(e => e.textContent).join(','), 'Loading,Loading');
  assert.equal(ui.doc.querySelector('.qh-home-empty'), null);
});

test('empty: one list empty says so, and no database at all says so once', opts, () => {
  const onlyDba = mount({ conns: [{ id: 'a', name: 'n', autoApproveRO: false, databases: [db('x', 'RW')] }] });
  assert.equal(onlyDba.doc.querySelector('.qh-access-card .qh-home-empty').textContent, 'No database runs at once for you.');
  const onlyOnce = mount({ conns: [{ id: 'a', name: 'n', autoApproveRO: true, databases: [db('x', 'RO')] }] });
  assert.equal(onlyOnce.all('.qh-access-card')[1].querySelector('.qh-home-empty').textContent, 'No database goes to a DBA first.');
  const none = mount({ conns: [] });
  assert.match(none.doc.querySelector('.qh-access-all').textContent, /You hold no database yet\. Use Request database access in the sidebar\./);
  assert.equal(none.all('.qh-access-card').length, 0);
  const allOff = mount({ conns: [CONNS[2]] });
  assert.match(allOff.doc.querySelector('.qh-access-all').textContent, /no database yet/, 'only a disabled connection counts as none');
});

test('no grant end date anywhere: the API has none', opts, () => {
  const ui = mount();
  assert.doesNotMatch(ui.doc.body.textContent, /\b(ends|until|expires|expiry|left)\b|\d{4}-\d{2}-\d{2}/i);
});

// ---------- wiring ----------

test('the Welcome page puts the lists above the cards and keeps every card', () => {
  const home = web('qh-home.jsx');
  const at = (s) => { const i = home.indexOf(s); assert.ok(i >= 0, s); return i; };
  assert.ok(at('<AccessSummary conns={conns}') < at('<div className="qh-home-grid">'));
  for (const t of ['Current session', 'Saved sessions', 'Recent history', 'Saved queries', 'Scheduled']) at('title="' + t + '"');
  const app = web('qh-app.jsx');
  assert.match(app, /conns=\{conns\} connsLoading=\{connsLoading\} isSuper=\{isSuper\}/);
});

test('the lists are in the dated style block, with tokens only', () => {
  const css = web('QueryHub.html');
  for (const sel of ['.qh-access-grid', '.qh-access-row', '.qh-access-more', '.qh-access-all']) assert.ok(css.includes(sel + ' {'), sel);
});
