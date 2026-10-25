// The whole app, mounted in jsdom on the mock API (2026-10-24, batch 2): the wiring
// that the unit tests cannot reach. A request that waits for a DBA shows the
// Waiting group, the amber card and the pale pill; Withdraw ends it as
// "Withdrawn by you."; a request that a DBA sent back reopens through "Edit and
// resubmit" in a new, picked tab; a bell item opens its request.
// Everything is the real code: qh-app.jsx runs, the mock plays the server.
import test, { after } from 'node:test';
import assert from 'node:assert/strict';

import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { transformSync } from 'esbuild';

import { loadInto } from './_load.mjs';
import { page, jsdomOpts as opts } from './_mount.mjs';

const require_ = createRequire(import.meta.url);
const HERE = dirname(fileURLToPath(import.meta.url));

const BEFORE_MOCK = ['tweaks-panel.jsx', 'qh-data.jsx'];
const AFTER_MOCK = ['qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-results-tools.jsx', 'qh-panels.jsx',
  'qh-admin-data.jsx', 'qh-admin-access.jsx', 'qh-admin-person.jsx', 'qh-admin-effective.jsx', 'qh-admin-roles.jsx', 'qh-admin-mask.jsx',
  'qh-admin-insights.jsx', 'qh-admin-audit.jsx', 'qh-admin-config.jsx', 'qh-admin.jsx', 'qh-whatsnew.jsx', 'qh-home.jsx', 'qh-app.jsx'];

/** The mock is not under src/ (the build must never import it): load it from the root, as the raw prototype does. */
function loadMock(win) {
  const src = readFileSync(resolve(HERE, '..', '..', 'qh-api-mock.jsx'), 'utf8');
  const code = transformSync(src, { loader: 'jsx', jsx: 'transform', jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment' }).code;
  new Function('window', 'with (window) {\n' + code + '\n}')(win);
}

const pages = [];
after(() => { pages.forEach(w => { try { w.close(); } catch (e) { /* closed */ } }); });

async function boot(role) {
  const ui = page(BEFORE_MOCK, { prep: (win) => {
    const client = require_('react-dom/client');
    win.ReactDOM = { ...win.ReactDOM, createRoot: client.createRoot };
    win.localStorage.setItem('qh.mock.session.v1', JSON.stringify({ handle: role === 'developer' ? 'ben.donnelly' : 'dana.kaur', role, at: Date.now() }));
    win.matchMedia = win.matchMedia || (() => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }));
    win.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
    win.Element.prototype.scrollIntoView = function () {};
    win.HTMLElement.prototype.scrollTo = function () {};
  } });
  pages.push(ui.win);
  loadMock(ui.win);
  await ui.flush();
  loadInto(ui.win, ...AFTER_MOCK);
  await until(ui, () => ui.doc.querySelector('.qh-top'));
  return ui;
}
const wait = async (ui, ms) => { await new Promise(r => setTimeout(r, ms)); await ui.flush(); };
/** Poll until `fn` gives a truthy value (the mock answers after a fake delay). */
async function until(ui, fn, ms = 6000) {
  const end = Date.now() + ms;
  for (;;) {
    await wait(ui, 40);
    const v = fn();
    if (v) return v;
    if (Date.now() > end) throw new Error('timed out waiting for: ' + fn.toString().slice(0, 120));
  }
}

const tabs = (ui) => ui.all('.qh-tab:not(.is-welcome)');
const sideTab = (ui, name) => ui.all('.qh-side-tab').find(b => b.getAttribute('aria-label') === name);
const openHistory = async (ui) => { ui.click(sideTab(ui, 'History')); await until(ui, () => ui.doc.querySelector('.qh-hist-item')); };
const histRow = (ui, id) => ui.all('.qh-hist-item').find(e => e.getAttribute('data-hist-id') === id);
const chip = (ui, label) => ui.all('.qh-hfilter').find(b => b.textContent === label);

test('the app boots on the mock', opts, async () => {
  const ui = await boot('developer');
  assert.equal(ui.doc.querySelector('.qh-crash'), null, 'no crash screen');
});

test('History: the Waiting group lists the requests that wait, longest wait first', opts, async () => {
  const ui = await boot('developer');
  await openHistory(ui);
  const rows = ui.all('.qh-wait-row');
  assert.deepEqual(rows.map(r => r.getAttribute('data-pending-id')), ['q_w02', 'q_w01']);
  assert.match(rows[0].textContent, /ALTER TABLE ledger_entries/);
  assert.match(rows[0].textContent, /staging · app_stg · 41m ago/);
  assert.equal(ui.doc.querySelector('.qh-side-body').firstElementChild, ui.doc.querySelector('.qh-wait-group'), 'on top');
});

test('History: every chip shows its own group of the mock history', opts, async () => {
  const ui = await boot('developer');
  await openHistory(ui);
  const ids = () => ui.all('.qh-hist-item').map(e => e.getAttribute('data-hist-id'));
  ui.click(chip(ui, 'Denied'));
  assert.deepEqual(ids().sort(), ['h4', 'q_e01'].sort());
  ui.click(chip(ui, 'Expired'));
  assert.deepEqual(ids(), ['q_e03']);
  ui.click(chip(ui, 'Withdrawn'));
  assert.deepEqual(ids(), ['q_e02']);
  ui.click(chip(ui, 'Approved'));
  assert.ok(ids().includes('h1') && !ids().includes('q_e01') && !ids().includes('q_w01'));
  ui.click(chip(ui, 'All'));
  assert.ok(ids().includes('q_e04') && ids().includes('q_w01'), 'changes requested and pending are under All');
  // Four different words on the rows.
  const word = (id) => histRow(ui, id).querySelector('.qh-status').textContent;
  assert.deepEqual(['q_e01', 'q_e02', 'q_e03', 'q_e04', 'q_w01'].map(word), ['Denied', 'Withdrawn', 'Expired', 'Changes requested', 'Pending']);
});

test('a waiting request: click opens a tracked tab with the card, the pale pill and Withdraw; Withdraw ends it as withdrawn', opts, async () => {
  const ui = await boot('developer');
  await openHistory(ui);
  const before = tabs(ui).length;
  ui.click(ui.all('.qh-wait-row').find(r => r.getAttribute('data-pending-id') === 'q_w01'));
  await until(ui, () => ui.doc.querySelector('.qh-wait-pill'));
  assert.equal(tabs(ui).length, before + 1, 'a new tab');
  // (b) the action bar: a pale status pill and a ghost Withdraw, no solid pill.
  assert.equal(ui.doc.querySelector('.qh-wait-pill').textContent, 'Waiting for a DBA');
  assert.equal(ui.doc.querySelector('.qh-run'), null);
  assert.equal(ui.doc.querySelector('.qh-actionbar .qh-cancel-run').textContent, 'Withdraw');
  // (a) the amber card in Results, with the time since the submit and no countdown.
  const card = await until(ui, () => ui.doc.querySelector('.qh-pend-card'));
  assert.match(card.textContent, /A DBA decides this\. You get a Slack message and a bell item when they do\./);
  assert.match(card.textContent, /Submitted 7m ago\./);
  assert.doesNotMatch(card.textContent, /expire|left/i);
  assert.equal(ui.doc.querySelector('.qh-res-head .qh-status').textContent, 'Pending');
  // The sidebar group still holds it (an open tab counts, once).
  assert.equal(ui.all('.qh-wait-row').filter(r => r.getAttribute('data-pending-id') === 'q_w01').length, 1);
  // Withdraw from the card: the same action as the action bar's button.
  ui.click([...card.querySelectorAll('button')].find(b => b.textContent === 'Withdraw'));
  await until(ui, () => !ui.doc.querySelector('.qh-wait-pill'), 8000);
  assert.equal(ui.doc.querySelector('.qh-pend-card'), null);
  await until(ui, () => ui.doc.querySelector('.qh-res-head .qh-status.st-withdrawn'));
  assert.equal(ui.doc.querySelector('.qh-res-head .qh-status').textContent, 'Withdrawn');
  // The ended card, and the Messages line: "Withdrawn by you.", not "Cancelled."
  assert.equal(ui.doc.querySelector('.qh-end-card').getAttribute('data-outcome'), 'withdrawn');
  ui.click(ui.all('.qh-res-tab').find(b => b.textContent.startsWith('Messages')));
  const lines = ui.all('.qh-msg-text').map(e => e.textContent);
  assert.ok(lines.includes('Withdrawn by you.'), lines.join(' | '));
  assert.ok(!lines.includes('Cancelled.'));
  // The Waiting group no longer lists it, and the History row says Withdrawn.
  await until(ui, () => !ui.all('.qh-wait-row').some(r => r.getAttribute('data-pending-id') === 'q_w01'));
  assert.equal(histRow(ui, 'q_w01').querySelector('.qh-status').textContent, 'Withdrawn');
});

test('a waiting request whose status carries expiresAt names the date, and only that one', opts, async () => {
  const ui = await boot('developer');
  await openHistory(ui);
  ui.click(ui.all('.qh-wait-row').find(r => r.getAttribute('data-pending-id') === 'q_w02'));
  // The card shows at once; `expiresAt` arrives with the first status answer.
  const card = await until(ui, () => { const c = ui.doc.querySelector('.qh-pend-card'); return c && /expires on/.test(c.textContent) && c; });
  assert.match(card.textContent, /Submitted 41m ago\./);
  assert.match(card.textContent, /If nobody decides, the request expires on /);
  assert.doesNotMatch(card.textContent, /minutes? left|min left|countdown/i, 'a date, never a countdown');
});

test('Edit and resubmit opens a NEW picked tab with the same SQL and target', opts, async () => {
  const ui = await boot('developer');
  await openHistory(ui);
  const before = tabs(ui).length;
  const btn = histRow(ui, 'q_e04').querySelector('.qh-hist-resub');
  assert.ok(btn, 'on the changes-requested row');
  assert.ok(!histRow(ui, 'q_e01').querySelector('.qh-hist-resub'), 'not on a denied one');
  ui.click(btn);
  await ui.flush();
  assert.equal(tabs(ui).length, before + 1, 'a NEW tab');
  const active = ui.doc.querySelector('.qh-tab.is-active');
  assert.match(active.textContent, /UPDATE users SET status/);
  // The target strip shows the request's target, and the tab is picked: the "click a
  // database to use it here" hint is for an unpicked tab only.
  assert.match(ui.doc.querySelector('.qh-target').textContent, /prod-main/);
  assert.equal(ui.doc.querySelector('.qh-target-hint'), null, 'picked, not unpicked');
});

test('a bell item for a request opens the History list at its row', opts, async () => {
  const ui = await boot('developer');
  const seen = [];
  ui.win.Element.prototype.scrollIntoView = function () { seen.push(this.getAttribute('data-hist-id')); };
  ui.click(ui.doc.querySelector('.qh-notif-btn'));
  const item = await until(ui, () => ui.all('.qh-notif-item').find(b => b.textContent.includes('Query rejected')));
  ui.click(item);
  await until(ui, () => histRow(ui, 'q_e01'));
  await until(ui, () => seen.includes('q_e01'));
  assert.ok(ui.doc.querySelector('.qh-side-tab[aria-label="History"]').classList.contains('is-active'));
  assert.ok(ui.doc.querySelector('.qh-hist-item.is-flash'), 'the row is marked');
  assert.equal(ui.doc.querySelector('.qh-notif-menu'), null, 'the menu closed');
});

test('an admin item opens #admin/<section> for an admin; a developer only marks it read', opts, async () => {
  const sup = await boot('super');
  sup.click(sup.doc.querySelector('.qh-notif-btn'));
  const kill = await until(sup, () => sup.all('.qh-notif-item').find(b => b.textContent.includes('Kill switch released')));
  sup.click(kill);
  await until(sup, () => sup.doc.querySelector('.qh-admin'));
  assert.equal(sup.win.location.hash, '#admin/kill');
  const dev = await boot('developer');
  dev.click(dev.doc.querySelector('.qh-notif-btn'));
  const item = await until(dev, () => dev.all('.qh-notif-item').find(b => b.textContent.includes('Kill switch released')));
  dev.click(item);
  await dev.flush();
  assert.equal(dev.doc.querySelector('.qh-admin'), null, 'a developer stays in the editor');
  assert.ok(dev.doc.querySelector('.qh-notif-menu'), 'the menu stays open');
  assert.notEqual(dev.win.location.hash, '#admin/kill');
});

test('Welcome: the lists sit above the cards; a super-admin gets one line', opts, async () => {
  const dev = await boot('developer');
  dev.click(dev.doc.querySelector('.qh-brand'));
  await until(dev, () => dev.doc.querySelector('.qh-home'));
  await until(dev, () => dev.doc.querySelector('.qh-access-card, .qh-access-all'));
  const titles = dev.all('.qh-access-card .qh-home-card-title').map(e => e.firstChild.textContent);
  assert.deepEqual(titles, ['Runs at once', 'Goes to a DBA first']);
  const kids = [...dev.doc.querySelector('.qh-home-inner').children];
  assert.ok(kids.indexOf(dev.doc.querySelector('.qh-access-grid')) < kids.indexOf(dev.doc.querySelector('.qh-home-grid')), 'above the cards');
  assert.ok(dev.all('.qh-home-grid .qh-home-card').length >= 5, 'the cards are kept');
  const sup = await boot('super');
  sup.click(sup.doc.querySelector('.qh-brand'));
  await until(sup, () => sup.doc.querySelector('.qh-access-all'));
  assert.match(sup.doc.querySelector('.qh-access-all').textContent, /^You run everything at once/);
  assert.equal(sup.all('.qh-access-card').length, 0);
});
