// 2026-10-24: grant end dates on Welcome and in the tree, and the Steps list on
// the Messages tab. Mount helper copied from design-debt.test.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import { bareWindow, loadInto } from './_load.mjs';
import { tokensCss } from './_mount.mjs';

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
const rowsText = (ui) => ui.all('.qh-step').map(r => ['.qh-step-word', '.qh-step-what'].map(q => r.querySelector(q).textContent).filter(Boolean).join(' '));

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
    assert.deepEqual(rowsText(ui), want[k], k);
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

test('by shows only when present', opts, () => {
  const ui = mount('MessagesView', () => ({ messages: MSGS, timeline: CASES.approved }));
  assert.equal(ui.all('.qh-step-by').length, 1);
  assert.equal(ui.all('.qh-step-by')[0].textContent, 'by Jordan Ray');
});

// ---------- the columns: clock, dot, step, what happened, wait ----------

const CLOCK = /^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}$/;
const AT0 = Date.parse('2026-10-31T02:41:30.792Z');
const stepAt = (ms) => new Date(AT0 + ms).toISOString();

test('the clock is the server time verbatim, with the age in the title', opts, () => {
  const tl = [{ step: 'sent', at: stepAt(0), time: '2026-10-31 05:41:30.792' }];
  const ui = mount('MessagesView', () => ({ messages: MSGS, timeline: tl }));
  const t = ui.all('.qh-step-time')[0];
  assert.equal(t.textContent, '2026-10-31 05:41:30.792');
  assert.equal(t.getAttribute('datetime'), tl[0].at);
  assert.match(t.getAttribute('title'), /ago$/);
});

test('a server that sends no time gets the same layout from at', opts, () => {
  const ui = mount('MessagesView', () => ({ messages: MSGS, timeline: CASES.completed }));
  for (const t of ui.all('.qh-step-time')) assert.match(t.textContent, CLOCK);
});

test('every row has the five columns in one order', opts, () => {
  const ui = mount('MessagesView', () => ({ messages: MSGS, timeline: CASES.completed }));
  for (const r of ui.all('.qh-step')) {
    assert.deepEqual([...r.children].map(c => c.className.split(' ')[0]),
      ['qh-step-time', 'qh-step-rail', 'qh-step-word', 'qh-step-what', 'qh-step-gap']);
  }
});

test('the wait since the step before shows on every row but the first', opts, () => {
  const tl = [{ step: 'sent', at: stepAt(0) }, { step: 'decided', at: stepAt(134000), outcome: 'approved', by: 'Jordan Ray' },
    { step: 'started', at: stepAt(134052) }, { step: 'finished', at: stepAt(134480), outcome: 'completed' }];
  const ui = mount('MessagesView', () => ({ messages: MSGS, timeline: tl }));
  const gaps = ui.all('.qh-step-gap').map(g => g.textContent);
  assert.deepEqual(gaps, ['', '+2 min 14 s', '+52 ms', '+428 ms']);
  assert.equal(ui.all('.qh-step-gap')[1].getAttribute('title'), 'Time since Sent');
  assert.equal(ui.all('.qh-step-gap')[3].getAttribute('title'), 'Time since Started');
});

test('a wait that goes backwards or cannot be read shows nothing', opts, () => {
  const tl = [{ step: 'sent', at: stepAt(5000) }, { step: 'decided', at: stepAt(0), outcome: 'approved' },
    { step: 'started', at: 'not a time' }];
  const ui = mount('MessagesView', () => ({ messages: MSGS, timeline: tl }));
  assert.deepEqual(ui.all('.qh-step-gap').map(g => g.textContent), ['', '', '']);
  assert.equal(ui.all('.qh-steps-total').length, 0);
});

test('the head says how long the whole run took, from which step to which', opts, () => {
  const tl = [{ step: 'sent', at: stepAt(0) }, { step: 'decided', at: stepAt(134000), outcome: 'approved' }];
  const ui = mount('MessagesView', () => ({ messages: MSGS, timeline: tl }));
  assert.equal(ui.all('.qh-steps-total')[0].textContent, '2 min 14 s from Sent to Decided');
  const one = mount('MessagesView', () => ({ messages: MSGS, timeline: CASES.pending }));
  assert.equal(one.all('.qh-steps-total').length, 0);
});

test('the span text reads as a short phrase at every size', () => {
  const f = pure.qhSpanText;
  const table = [[0, '0 ms'], [52, '52 ms'], [999, '999 ms'], [1000, '1 s'], [1400, '1.4 s'], [9950, '10 s'],
    [10000, '10 s'], [42000, '42 s'], [59600, '1 min'], [120000, '2 min'], [134000, '2 min 14 s'],
    [3600000, '1 h'], [3900000, '1 h 5 min'], [183600000, '2 d 3 h']];
  for (const [ms, want] of table) assert.equal(f(ms), want, String(ms));
  for (const bad of [-1, NaN, undefined, null, '5']) assert.equal(f(bad), '', String(bad));
});

// ---------- outcome: the word, the chip colour and the last dot ----------

test('each outcome has a chip with its word and its tone', opts, () => {
  const tone = { approved: 'ok', completed: 'ok', denied: 'bad', failed: 'bad',
    changes: 'warn', withdrawn: 'warn', expired: 'warn' };
  for (const [k, want] of Object.entries(tone)) {
    const ui = mount('MessagesView', () => ({ messages: MSGS, timeline: CASES[k] }));
    const chips = ui.all('.qh-step-outcome');
    assert.ok(chips.length >= 1, k);
    // The last chip is the end of the request. An earlier "Approved" stays ok.
    const last = chips[chips.length - 1];
    assert.ok(last.classList.contains('tone-' + want), k + ' ' + last.textContent);
    for (const c of chips) assert.ok(c.textContent.length > 3, 'a chip has text, never colour alone');
  }
  const failed = mount('MessagesView', () => ({ messages: MSGS, timeline: CASES.failed })).all('.qh-step-outcome');
  assert.deepEqual(failed.map(c => c.className.split(' ').pop()), ['tone-ok', 'tone-bad']);
});

test('the last dot turns red for a refusal or a failure and amber for the rest', opts, () => {
  const cls = (k) => mount('MessagesView', () => ({ messages: MSGS, timeline: CASES[k] }))
    .all('.qh-step').pop().className;
  assert.match(cls('denied'), /is-last tone-bad/);
  assert.match(cls('failed'), /is-last tone-bad/);
  for (const k of ['changes', 'withdrawn', 'expired']) assert.match(cls(k), /is-last tone-warn/, k);
  assert.match(cls('completed'), /is-last tone-ok/);
  assert.doesNotMatch(cls('pending'), /tone-/);
});

test('an approval by QueryHub reads as an auto-approval, like the Messages line', opts, () => {
  const tl = [{ step: 'sent', at: stepAt(0) }, { step: 'decided', at: stepAt(50), outcome: 'approved', by: 'QueryHub' }];
  const ui = mount('MessagesView', () => ({ messages: MSGS, timeline: tl }));
  assert.deepEqual(rowsText(ui), ['Sent', 'Decided Auto-approved by QueryHub']);
  assert.ok(ui.all('.qh-step-outcome')[0].classList.contains('tone-ok'));
});

// ---------- the CSS: the traps the old rules fell into ----------

test('the Steps CSS keeps its columns together and uses real tokens', () => {
  const html = web('QueryHub.html');
  const rule = (sel) => {
    const m = html.match(new RegExp('\\n\\s*' + sel.replace(/[.]/g, '\\.') + ' \\{([^}]*)\\}'));
    assert.ok(m, 'no rule for ' + sel);
    return m[1];
  };
  // A time pushed to the far edge of a wide panel was the defect.
  assert.doesNotMatch(rule('.qh-step-time'), /margin-left:\s*auto/);
  assert.doesNotMatch(rule('.qh-step-gap'), /margin-left:\s*auto/);
  // --qh-sans never existed: the var() fell back to the mono of the log.
  assert.doesNotMatch(html, /var\(\s*--qh-sans/);
  assert.match(rule('.qh-steps'), /font-family:\s*var\(--font-sans\)/);
  assert.match(rule('.qh-step-time'), /font-family:\s*var\(--qh-mono\)/);
  assert.match(rule('.qh-step'), /grid-template-columns:\s*max-content 24px 72px minmax\(0, 250px\) max-content/);
  // Every custom property the Steps rules read exists in the tokens or the page.
  const a = html.indexOf('.qh-steps {'), b = html.indexOf('/* Bell menu footer');
  const known = tokensCss() + html;
  for (const name of new Set([...html.slice(a, b).matchAll(/var\((--[a-z0-9-]+)/g)].map(m => m[1]))) {
    assert.ok(known.includes(name + ':'), name + ' is not defined');
  }
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
