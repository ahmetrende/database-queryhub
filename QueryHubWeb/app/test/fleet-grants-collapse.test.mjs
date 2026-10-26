// Grants screen, "Every server" block (2026-10-26): a long list collapses.
// 29 rows pushed the whole page below the fold, and 27 of them were one sentence.
// The copied auto-approve rows fold into one summary line, the block shows its count,
// and no internal table name reaches the screen.
import test from 'node:test';
import assert from 'node:assert/strict';

import { page, web, jsdomOpts as opts } from './_mount.mjs';

const ACC = ['qh-data.jsx', 'qh-modal.jsx', 'qh-panels.jsx', 'qh-admin-data.jsx', 'qh-admin-access.jsx', 'qh-admin-person.jsx'];

const copy = (i, extra = {}) => ({ id: 'c' + i, subjectType: 'user', subject: 'U0EXAMPLE' + String(i).padStart(3, '0'),
  subjectName: 'Person ' + i, tier: 'ro', autoApprove: true, expiresAt: null, syncedFrom: 'auto_approve_grants',
  reason: 'mirrored from auto_approve_grants', ...extra });

function grants(nCopies) {
  return [
    ...Array.from({ length: nCopies }, (_, i) => copy(i + 1)),
    { id: 'd1', subjectType: 'user', subject: 'U0EXAMPLED01', subjectName: 'Ddl Person', tier: 'ddl', autoApprove: false,
      expiresAt: null, syncedFrom: 'requesters', reason: 'carried from bypass_team_grants' },
    { id: 't1', subjectType: 'team', subject: 'Team One', subjectName: 'Team One', tier: 'ro', autoApprove: true,
      expiresAt: null, reason: 'Team One: every server, reads auto-approved' },
    { id: 't2', subjectType: 'team', subject: 'Team One', subjectName: 'Team One', tier: 'ddl', autoApprove: false,
      expiresAt: null, reason: 'Team One: every server, writes need approval' },
  ];
}

async function mount(list) {
  const ui = page(ACC);
  ui.win.qhApi = { adminFleetGrants: () => Promise.resolve({ grants: list }) };
  ui.render(ui.React.createElement(ui.win.FleetGrantsBlock, { canWrite: true, st: {
    people: [{ id: 'U0EXAMPLED01', handle: 'ddl.person', name: 'Ddl Person' }], teams: [] } }));
  await ui.flush();
  await ui.flush();
  return ui;
}

const rows = (ui) => ui.all('.qh-fleet-row:not(.qh-fleet-sum)');
const moreBtn = (ui) => ui.doc.querySelector('.qh-fleet-more button');

test('a long list shows its count, the rows that matter and one summary line', opts, async () => {
  const ui = await mount(grants(27));
  assert.equal(ui.doc.querySelector('.qh-fleet-count').textContent, '30');
  assert.equal(rows(ui).length, 3, 'the DDL person and the two team rows');
  const sum = ui.doc.querySelector('.qh-fleet-sum');
  assert.ok(sum, 'the summary line is missing');
  assert.match(sum.textContent, /27 people/);
  assert.match(sum.textContent, /auto-approve/);
  assert.match(sum.textContent, /No end date/);
  assert.equal(moreBtn(ui).textContent, 'Show all 30');
});

test('no internal table name reaches the screen, and the label says only synced', opts, async () => {
  const ui = await mount(grants(27));
  const text = ui.doc.querySelector('.qh-fleet').textContent;
  assert.ok(!/auto_approve_grants|bypass_team_grants|synced from/.test(text), text.slice(0, 300));
  assert.ok(/synced/.test(text));
  ui.click(moreBtn(ui));
  const full = ui.doc.querySelector('.qh-fleet').textContent;
  assert.ok(!/auto_approve_grants|bypass_team_grants|synced from/.test(full), 'the full list hides them too');
});

test('Show all lists every row and drops the summary, Show fewer folds it back', opts, async () => {
  const ui = await mount(grants(27));
  ui.click(moreBtn(ui));
  assert.equal(rows(ui).length, 30);
  assert.equal(ui.doc.querySelector('.qh-fleet-sum'), null);
  assert.equal(moreBtn(ui).textContent, 'Show fewer');
  assert.equal(moreBtn(ui).getAttribute('aria-expanded'), 'true');
  ui.click(moreBtn(ui));
  assert.equal(rows(ui).length, 3);
  assert.ok(ui.doc.querySelector('.qh-fleet-sum'));
});

test('a short list stays as it was: every row, no summary, no button', opts, async () => {
  const ui = await mount(grants(3));          // 3 copies + 3 others = 6
  assert.equal(rows(ui).length, 6);
  assert.equal(ui.doc.querySelector('.qh-fleet-sum'), null);
  assert.equal(moreBtn(ui), null);
});

test('a single copied row is not summarised on its own', opts, async () => {
  const list = [...grants(1), ...Array.from({ length: 6 }, (_, i) => ({
    id: 'x' + i, subjectType: 'team', subject: 'T' + i, subjectName: 'Team ' + i, tier: 'ro', autoApprove: false,
    expiresAt: null, reason: 'r' }))];
  const ui = await mount(list);
  assert.equal(ui.doc.querySelector('.qh-fleet-sum'), null, 'one copy is shown as a row');
  assert.equal(rows(ui).length, 5);
});

test('the summary counts the copies that have an end date', opts, async () => {
  const list = grants(27).map((g, i) => (g.syncedFrom === 'auto_approve_grants' && i < 7)
    ? { ...g, expiresAt: '2027-01-24T12:00:00Z' } : g);
  const ui = await mount(list);
  assert.match(ui.doc.querySelector('.qh-fleet-sum').textContent, /20 with no end date/);
});

test('a team copy and a DDL copy are never folded into the summary', opts, async () => {
  const list = grants(27);
  const ui = await mount(list);
  const texts = rows(ui).map(r => r.textContent);
  assert.ok(texts.some(t => /Ddl Person/.test(t) && /ddl/i.test(t)), texts.join(' | '));
  assert.ok(texts.some(t => /Team One/.test(t)), texts.join(' | '));
  assert.ok(ui.doc.querySelector('.qh-fleet-sum').textContent.indexOf('Ddl Person') < 0);
});

const plain = (i, extra = {}) => ({ id: 'p' + i, subjectType: 'user', subject: 'U0PLAIN' + String(i).padStart(3, '0'),
  subjectName: 'Plain ' + i, tier: 'ro', autoApprove: false, expiresAt: null, reason: 'r' + i, ...extra });

test('seven rows collapse, six rows do not', opts, async () => {
  const six = await mount(Array.from({ length: 6 }, (_, i) => plain(i)));
  assert.equal(moreBtn(six), null);
  const seven = await mount(Array.from({ length: 7 }, (_, i) => plain(i)));
  assert.equal(moreBtn(seven).textContent, 'Show all 7');
  assert.equal(rows(seven).length, 5);
});

test('only a synced, auto-approve, RO, person row folds into the summary', opts, async () => {
  const base = Array.from({ length: 4 }, (_, i) => copy(i + 1));           // 4 real copies
  const lookalikes = [
    copy(90, { id: 'l1', syncedFrom: null, reason: 'l1 reason' }),            // not synced
    copy(91, { id: 'l2', autoApprove: false, reason: 'l2 reason' }),          // not auto-approve
    copy(92, { id: 'l3', tier: 'ddl', reason: 'l3 reason' }),                  // not RO
    copy(93, { id: 'l4', subjectType: 'team', subject: 'Team Four', subjectName: 'Team Four', reason: 'l4 reason' }),  // a team
  ];
  const ui = await mount([...base, ...lookalikes]);                           // 8 rows: collapsed
  assert.match(ui.doc.querySelector('.qh-fleet-sum').textContent, /4 people/);
  assert.equal(rows(ui).length, 4, 'each lookalike stays a normal row');
  const text = rows(ui).map(r => r.textContent).join(' | ');
  for (const k of ['Person 90', 'Person 91', 'Person 92', 'Team Four']) assert.ok(text.includes(k), k + ' in ' + text);
});

test('an empty list shows no count and no button', opts, async () => {
  const ui = await mount([]);
  assert.equal(ui.doc.querySelector('.qh-fleet-count'), null);
  assert.equal(moreBtn(ui), null);
});

test('the summary names its people for a keyboard user too: a link opens the list', opts, async () => {
  const ui = await mount(grants(27));
  const sum = ui.doc.querySelector('.qh-fleet-sum');
  assert.ok(sum.querySelector('.qh-fleet-who').getAttribute('title').includes('Person 1'), 'the tooltip names the people');
  const link = [...sum.querySelectorAll('button')].find(b => b.textContent === 'Show them');
  assert.ok(link, 'the Show them link is missing');
  ui.click(link);
  assert.equal(rows(ui).length, 30);
  assert.equal(ui.doc.querySelector('.qh-fleet-sum'), null);
});

test('the subtitle no longer claims the block is read-only', opts, async () => {
  const ui = await mount(grants(3));
  const sub = ui.doc.querySelector('.qh-fleet-sub').textContent;
  assert.ok(!/Read-only access/.test(sub));
  assert.match(sub, /Each row shows its tier/);
});

test('the style for the count and the button row exists', () => {
  const css = web('QueryHub.html');
  assert.match(css, /\.qh-fleet-count \{[^}]*border-radius/);
  assert.match(css, /\.qh-fleet-more \{[^}]*border-top/);
});
