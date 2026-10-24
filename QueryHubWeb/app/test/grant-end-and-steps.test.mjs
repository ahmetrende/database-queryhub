// 2026-10-24: grant end dates on Welcome and in the tree, and the Steps list on
// the Messages tab. Mount helper copied from design-debt.test.mjs.
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

const pure = loadInto(bareWindow(), 'qh-data.jsx');

function mount(View, props) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>',
                        { pretendToBeVisual: true, url: 'https://localhost/' });
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
  loadInto(win, 'qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx', 'qh-home.jsx');
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(win[View], props(win))));
  const doc = win.document;
  const all = (sel) => [...doc.querySelectorAll(sel)];
  return { win, doc, all };
}

const DAY = 86400000;
const iso = (ms) => new Date(Date.now() + ms).toISOString();

// ---------- the pure helper ----------

test('qhGrantEnd gives text, tone and title for each state', () => {
  assert.equal(pure.qhGrantEnd(null), null);
  assert.equal(pure.qhGrantEnd(''), null);
  const live = pure.qhGrantEnd(iso(45 * DAY + 3600000));
  assert.equal(live.tone, 'calm');
  assert.match(live.text, /^45d left$/);
  assert.match(live.title, /^Ends /);
  const soon = pure.qhGrantEnd(iso(2 * DAY + 3600000));
  assert.equal(soon.tone, 'amber');
  assert.equal(soon.text, 'Ends soon · 2d left');
  assert.equal(soon.short, 'Ends in 2d');
  const gone = pure.qhGrantEnd(iso(-3 * DAY));
  assert.equal(gone.tone, 'muted');
  assert.equal(gone.text, 'Ended');
  assert.match(gone.title, /^Ended /);
});

test('qhAccessSummary keeps grantEndsAt on each row', () => {
  const end = iso(2 * DAY);
  const conns = [{ id: 'c1', name: 'orders', autoApproveRO: true,
    databases: [{ id: 'd1', name: 'app', tier: 'RO', grantEndsAt: end }, { id: 'd2', name: 'log', tier: 'RW' }] }];
  const s = pure.qhAccessSummary(conns, false);
  const all = [...s.once, ...s.dba];
  assert.equal(all.find(r => r.db === 'app').grantEndsAt, end);
  assert.equal(all.find(r => r.db === 'log').grantEndsAt, null);
});

// ---------- Welcome rows ----------

test('Welcome shows the end chip only when there is an end', opts, () => {
  const mk = (id, end) => ({ id, name: id, tier: 'RW', grantEndsAt: end });
  const conns = [{ id: 'c1', name: 'orders', databases: [
    mk('a', iso(2 * DAY + 3600000)), mk('b', iso(45 * DAY + 3600000)), mk('c', null), mk('d', iso(-3 * DAY)),
    mk('e', null), mk('f', null), mk('g', null), mk('h', iso(10 * DAY)) ] }];
  const ui = mount('AccessSummary', () => ({ conns, loading: false, isSuper: false }));
  const chips = ui.all('.qh-grantend');
  // Six rows are shown. Row 7 and 8 sit under "+N more".
  assert.equal(ui.all('.qh-access-row').length, 6);
  assert.equal(chips.length, 3, 'no chip for rows with no end');
  assert.deepEqual(chips.map(c => c.getAttribute('data-end')).sort(), ['ended', 'live', 'soon']);
  assert.ok(chips.every(c => c.getAttribute('title')), 'the full date is in the title');
  assert.match(ui.doc.querySelector('.qh-access-more').textContent, /\+2 more/);
  assert.ok(!/No end date/.test(ui.doc.body.textContent));
});

// ---------- tree chip ----------

test('the tree draws the ends-soon chip only for the soon state', () => {
  const src = web('qh-panels.jsx');
  assert.match(src, /qhGrantEnd\(db\.grantEndsAt\)/);
  assert.match(src, /dbEnd && dbEnd\.state === 'soon'/);
  assert.match(src, /qh-tr-endsoon/);
});

// ---------- Steps list ----------

const T = (s) => new Date(Date.now() - s * 60000).toISOString();
const CASES = {
  pending:   [{ step: 'sent', at: T(5) }],
  approved:  [{ step: 'sent', at: T(5) }, { step: 'decided', at: T(4), outcome: 'approved', by: 'Jordan Ray' }, { step: 'started', at: T(3) }],
  completed: [{ step: 'sent', at: T(5) }, { step: 'decided', at: T(4), outcome: 'approved' }, { step: 'started', at: T(3) }, { step: 'finished', at: T(2), outcome: 'completed' }],
  failed:    [{ step: 'sent', at: T(5) }, { step: 'decided', at: T(4), outcome: 'approved' }, { step: 'started', at: T(3) }, { step: 'finished', at: T(2), outcome: 'failed' }],
  denied:    [{ step: 'sent', at: T(5) }, { step: 'decided', at: T(4), outcome: 'denied', by: 'Jordan Ray' }],
  changes:   [{ step: 'sent', at: T(5) }, { step: 'decided', at: T(4), outcome: 'changes_requested' }],
  withdrawn: [{ step: 'sent', at: T(5) }, { step: 'decided', at: T(4), outcome: 'withdrawn' }],
  expired:   [{ step: 'sent', at: T(5) }, { step: 'decided', at: T(4), outcome: 'expired' }],
};
const MSGS = [{ kind: 'info', time: '10:00:00', text: 'First line.' }, { kind: 'ok', time: '10:00:01', text: '184 rows in 412 ms.' }];
const rowsText = (ui) => ui.all('.qh-step').map(r => [...r.children].map(c => c.textContent).filter(Boolean).join(' '));

test('Steps lists the words and outcomes for each status', opts, () => {
  const want = {
    pending: ['Sent'], approved: ['Sent', 'Decided Approved by Jordan Ray', 'Started'],
    completed: ['Sent', 'Decided Approved', 'Started', 'Finished Completed'],
    failed: ['Sent', 'Decided Approved', 'Started', 'Finished Failed'],
    denied: ['Sent', 'Decided Denied by Jordan Ray'], changes: ['Sent', 'Decided Changes requested'],
    withdrawn: ['Sent', 'Decided Withdrawn'], expired: ['Sent', 'Decided Expired'],
  };
  for (const [k, tl] of Object.entries(CASES)) {
    const ui = mount('MessagesView', () => ({ messages: MSGS, timeline: tl }));
    // The time text ends each row ("5m ago"), so compare the start.
    const got = rowsText(ui).map(t => t.replace(/ \d+[smhd] ago$/, ''));
    assert.deepEqual(got, want[k], k);
  }
});

test('Steps sits under the first line, above the rest, with the strong last dot', opts, () => {
  const ui = mount('MessagesView', () => ({ messages: MSGS, timeline: CASES.completed }));
  const kids = [...ui.doc.querySelector('.qh-msgs').children].map(e => e.className);
  assert.match(kids[0], /qh-msg/);
  assert.match(kids[1], /qh-steps/);
  assert.match(kids[2], /qh-msg/);
  const steps = ui.all('.qh-step');
  assert.equal(steps.filter(s => s.classList.contains('is-last')).length, 1);
  assert.ok(steps[steps.length - 1].classList.contains('is-last'));
});

test('by shows only when present, and the time has an absolute title', opts, () => {
  const ui = mount('MessagesView', () => ({ messages: MSGS, timeline: CASES.approved }));
  assert.equal(ui.all('.qh-step-by').length, 1);
  const t = ui.all('.qh-step-time')[0];
  assert.ok(t.getAttribute('title').length > 4);
  assert.match(t.textContent, /ago$/);
});

test('no timeline, or an empty one, renders no Steps at all', opts, () => {
  for (const tl of [undefined, null, []]) {
    const ui = mount('MessagesView', () => ({ messages: MSGS, timeline: tl }));
    assert.equal(ui.all('.qh-steps').length, 0);
    assert.equal(ui.all('.qh-msg').length, 2);
  }
});

// ---------- the field survives the client, the poll and the mock ----------

test('the API client does not rebuild connections or the status payload', () => {
  const api = web('qh-api.jsx');
  assert.match(api, /connections: *\(\) *=> qhFetch\('\/connections'\)/);
  assert.match(api, /status: *\(id\) *=> qhFetch\('\/queries\/' \+ id\)/);
});

test('the tab keeps timeline on every status update', () => {
  assert.match(web('qh-app.jsx'), /timeline: Array\.isArray\(sres\.timeline\) \? sres\.timeline : null/);
  assert.match(web('qh-app.jsx'), /timeline=\{tab\.timeline \|\| null\}/);
});

test('the mock seeds all four end states and a timeline for each status', () => {
  const m = web('qh-api-mock.jsx');
  const fn = m.slice(m.indexOf('function mockGrantEnd'), m.indexOf('function mockGrantEnd') + 600);
  assert.match(fn, /isoIn\(2 \* day\)/);
  assert.match(fn, /isoIn\(45 \* day\)/);
  assert.match(fn, /Date\.now\(\) - 3 \* day/);
  assert.match(fn, /return null/);
  assert.match(m, /grantEndsAt: mockGrantEnd\(/);
  assert.match(m, /return \{ \.\.\.s, timeline: mockTimeline\(rec, s\) \}/);
  for (const w of ['sent', 'decided', 'started', 'finished']) assert.match(m, new RegExp("step: '" + w + "'"));
});
