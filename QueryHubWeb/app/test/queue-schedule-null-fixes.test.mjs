// Three classic UI fixes (operator approval, 2026-11-18).
//
//   1. Approval queue: "Select all" takes only the cards on screen, and never
//      a DDL card. A short line under the batch bar says so.
//   2. "Custom date & time" schedules the query. It used to fail with
//      "Unknown schedule preset."
//   3. Results: a capped result says so, and SQL NULL shows as `NULL`.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { bareWindow, loadInto } from './_load.mjs';
import { web } from './_mount.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }
const dom = JSDOM ? {} : { skip: 'jsdom not installed' };

// ---------- 1. approval queue ----------

const A = loadInto(bareWindow(), 'qh-data.jsx', 'qh-admin-data.jsx', 'qh-admin-effective.jsx', 'qh-admin.jsx');

test('queue: batch ids are the cards on screen without DDL', () => {
  const shown = [{ id: 'a', tier: 'RO' }, { id: 'b', tier: 'DDL' }, { id: 'c', tier: 'RW', escalate: true }, { id: 'd', tier: 'RW' }];
  assert.deepEqual(A.qhQueueBatchIds(shown), ['a', 'd']);
  assert.deepEqual(A.qhQueueBatchIds([]), []);
});

test('queue: prune drops ids that left the screen and keeps the same array when none drop', () => {
  const sel = ['a', 'b'];
  assert.equal(A.qhQueuePrune(sel, ['a', 'b', 'c']), sel, 'same array: no extra render');
  assert.deepEqual(A.qhQueuePrune(sel, ['a']), ['a']);
  assert.deepEqual(A.qhQueuePrune(sel, []), []);
});

test('queue: the batch note names the DDL cards left out', () => {
  const base = 'Selects only the requests shown. Schema changes (DDL) are approved one by one.';
  assert.equal(A.qhQueueBatchNote(0), base);
  assert.equal(A.qhQueueBatchNote(1), base + ' 1 DDL request is not in the selection.');
  assert.equal(A.qhQueueBatchNote(3), base + ' 3 DDL requests are not in the selection.');
});

const SUPER = { name: 'Jordan Ray', role: 'super', isDba: true };
function row(id, extra) {
  return { id, submitter: { name: 'Jordan Ray', initials: 'JR', slackId: 'U0EXAMPLE001' },
    connectionId: 'prod-main', databaseId: 'orders', env: 'production', tier: 'RW',
    sql: 'UPDATE t SET a = 1 WHERE id = 2;', statements: 1, piiCols: [], estRows: 1,
    estTables: ['t'], reason: 'Fix one row.', submittedAt: '2026-10-23T08:00:00Z', ...extra };
}

function mountQueue(queue) {
  const d = new JSDOM('<!doctype html><div id="root"></div>',
                      { pretendToBeVisual: true, url: 'https://localhost/#admin/approvals' });
  for (const k of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node', 'Event',
                   'KeyboardEvent', 'MouseEvent', 'getComputedStyle', 'requestAnimationFrame',
                   'cancelAnimationFrame', 'localStorage', 'location', 'history']) {
    Object.defineProperty(globalThis, k, { value: d.window[k], configurable: true, writable: true });
  }
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const React = require_('react');
  const ReactDOM = require_('react-dom/client');
  globalThis.React = React;
  const win = d.window;
  win.React = React;
  win.ReactDOM = require_('react-dom');
  loadInto(win, 'qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx', 'qh-admin-data.jsx', 'qh-admin-effective.jsx', 'qh-admin.jsx');
  const calls = [];
  const st = { queue, manualRuns: [], endpointReqs: [], autoRequests: [], grants: [], people: [], teams: [],
    killSwitch: { enabled: false }, killHistory: () => Promise.resolve([]), toggleKill: () => Promise.resolve({}),
    decide: (...a) => calls.push(['decide', ...a]),
    batchApprove: (...a) => calls.push(['batchApprove', ...a]),
    approveBundle: (...a) => calls.push(['approveBundle', ...a]) };
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  const render = (s) => React.act(() => root.render(React.createElement(win.AdminPanel, { st: s, user: SUPER, modeEntry: null })));
  render(st);
  const doc = win.document;
  const click = (el) => React.act(() => el.dispatchEvent(new win.MouseEvent('click', { bubbles: true })));
  const button = (text) => [...doc.querySelectorAll('button')].find(b => b.textContent.trim() === text);
  return { doc, click, button, calls, st, render };
}

const MIXED = [row('q1'), row('q2', { tier: 'DDL' }), row('q3', { tier: 'RO' })];

test('queue: Select all leaves DDL out, and Approve N sends only the cards shown', dom, () => {
  const ui = mountQueue(MIXED);
  const note = ui.doc.querySelector('.qh-batchbar-note');
  assert.ok(note, 'the note shows under the batch bar');
  assert.ok(note.textContent.includes('1 DDL request is not in the selection.'), note.textContent);
  const ddlBox = ui.doc.querySelector('.qh-qcard.is-ddl input[type="checkbox"]');
  assert.equal(ddlBox.disabled, true, 'a DDL card has no live box');
  ui.click(ui.doc.querySelector('.qh-batchbar-all input'));
  const n = ui.button('Approve 2');
  assert.ok(n, 'Approve 2, not Approve 3');
  ui.click(n);
  assert.deepEqual(ui.calls[0].slice(0, 2), ['batchApprove', ['q1', 'q3']]);
});

test('queue: the DDL filter leaves nothing to batch, and a refetch drops a gone id', dom, () => {
  const ui = mountQueue(MIXED);
  ui.click(ui.doc.querySelector('.qh-batchbar-all input'));
  assert.ok(ui.button('Approve 2'));
  // The queue reloads without q1: the selection keeps only q3.
  ui.render({ ...ui.st, queue: [MIXED[1], MIXED[2]] });
  assert.ok(ui.button('Approve 1'), 'q1 left the screen and the selection');
  // The DDL-only filter: no card on screen can go in a batch.
  ui.click([...ui.doc.querySelectorAll('button')].find(b => b.textContent.startsWith('DDL')));
  assert.equal(ui.button('Approve 1'), undefined, 'nothing is selected');
  assert.equal(ui.doc.querySelector('.qh-batchbar-all input').disabled, true);
});

// ---------- 2. custom schedule ----------

const API = loadInto(bareWindow(), 'qh-data.jsx', 'qh-api.jsx');
const NOW = new Date(2026, 10, 18, 14, 0, 0, 0).getTime();   // local time

test('schedule: a custom local time becomes its UTC ISO run-at', () => {
  const want = new Date(2026, 10, 20, 9, 30, 0, 0).toISOString();
  assert.equal(API.qhScheduleToISO('2026-11-20 09:30', NOW), want, 'the value the popup sends');
  assert.equal(API.qhScheduleToISO('2026-11-20T09:30', NOW), want, 'the raw datetime-local value');
});

test('schedule: a past, an impossible or an unreadable time gives null', () => {
  assert.equal(API.qhScheduleToISO('2026-11-18 13:59', NOW), null, 'in the past');
  assert.equal(API.qhScheduleToISO('2026-11-18 14:00', NOW), null, 'now is not the future');
  assert.equal(API.qhScheduleToISO('2027-02-30 10:00', NOW), null, 'Feb 30');
  assert.equal(API.qhScheduleToISO('next week', NOW), null);
  assert.equal(API.qhScheduleToISO('', NOW), null);
});

test('schedule: the presets still work', () => {
  assert.equal(API.qhScheduleToISO('In 1 hour', NOW), new Date(2026, 10, 18, 15, 0, 0, 0).toISOString());
  assert.equal(API.qhScheduleToISO('Tonight 02:00', NOW), new Date(2026, 10, 19, 2, 0, 0, 0).toISOString());
  assert.equal(API.qhScheduleToISO('Tomorrow 09:00', NOW), new Date(2026, 10, 19, 9, 0, 0, 0).toISOString());
});

test('schedule: the old toast is gone, and a bad time keeps the popup open', () => {
  const src = web('qh-app.jsx');
  assert.ok(!src.includes('Unknown schedule preset.'));
  const at = src.indexOf("pushToast('Select a date and time in the future.')");
  assert.ok(at > 0);
  assert.ok(src.indexOf('setSchedOpen(false);', at) > at, 'the popup closes only after the check');
});

// ---------- 3. capped result and NULL ----------

const T = loadInto(bareWindow(), 'qh-data.jsx', 'qh-results-tools.jsx');

test('NULL: null and undefined are NULL, and an empty cell only in a typed column that is not text', () => {
  assert.equal(T.qhResIsNull(null), true);
  assert.equal(T.qhResIsNull(undefined, 'text'), true);
  assert.equal(T.qhResIsNull('', 'int4'), true);
  assert.equal(T.qhResIsNull('', 'timestamptz'), true);
  assert.equal(T.qhResIsNull('', 'text'), false, 'text: the CSV cannot tell');
  assert.equal(T.qhResIsNull('', undefined), false, 'no type: stays empty');
  assert.equal(T.qhResIsNull('0', 'int4'), false);
  assert.equal(T.qhResIsNull('NULL', 'text'), false, 'the word is a value');
});

test('cap: the warning shows when the grid has every row the server kept', () => {
  const rows = [{ a: '1' }, { a: '2' }];
  const note = T.qhResCapNote({ kind: 'table', rows, total: 2, truncated: true });
  assert.equal(note, 'The server capped this result at 2 rows. There can be more rows than shown.');
  assert.equal(T.qhResCapNote({ kind: 'table', rows, total: 2, truncated: false }), '');
  assert.equal(T.qhResCapNote({ kind: 'affected', truncated: true }), '');
});

test('cap: only the first page sent (more pages to fetch) is not a cap, unless the server says capped', () => {
  const rows = [{ a: '1' }];
  assert.equal(T.qhResCapNote({ kind: 'table', rows, total: 50, truncated: true }), '');
  assert.ok(T.qhResCapNote({ kind: 'table', rows, total: 50, truncated: true, capped: true }).includes('at 50 rows'));
  assert.equal(T.qhResCapNote({ kind: 'table', rows, total: 1, truncated: true, capped: false }), '');
  // A capped stored result keeps the note even when the page is not truncated.
  assert.ok(T.qhResCapNote({ kind: 'table', rows, total: 2, truncated: false, capped: true }).includes('at 2 rows'));
});

test('grid: the cell renders NULL and the toolbar reads the cap from baseResult', () => {
  const src = web('qh-panels.jsx');
  assert.ok(src.includes("{isNull ? 'NULL' : markCell(dispVal(r, c), c)}"));
  assert.ok(src.includes('const capNote = qhResCapNote(baseResult);'));
});
