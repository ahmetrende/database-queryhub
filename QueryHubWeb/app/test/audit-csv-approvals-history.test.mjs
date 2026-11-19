// Two admin features ported from the IdP app (2026-11-19).
//   1. The audit log downloads the rows on screen as CSV.
//   2. Approvals has a History tab with the 50 newest decisions.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { page, jsdomOpts, web } from './_mount.mjs';
import { loadInto } from './_load.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }
const dom = JSDOM ? {} : { skip: 'jsdom not installed' };

// ---------- 1. audit CSV ----------

const FILES = ['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-results-tools.jsx',
  'qh-panels.jsx', 'qh-admin-data.jsx', 'qh-admin-insights.jsx', 'qh-admin-audit.jsx'];

function auditWin() { return page(FILES, { url: 'https://localhost/#admin/audit' }).win; }

function arow(id, extra) {
  return { id, time: '2026-11-18T10:00:00Z', actor: { handle: 'U7', name: 'Jordan Ray', kind: 'person', via: null },
    action: 'approved', actionLabel: 'Approved', category: 'requests', effect: 'decided', classified: true,
    outcome: null, target: 'prod-main/orders', detail: {}, request: null, ...extra };
}

test('csv: cells are quoted per RFC 4180 and formula starts get a quote mark', dom, () => {
  const w = auditWin();
  assert.equal(w.qhCsvCell('plain'), 'plain');
  assert.equal(w.qhCsvCell('a,b'), '"a,b"');
  assert.equal(w.qhCsvCell('say "hi"'), '"say ""hi"""');
  assert.equal(w.qhCsvCell('line\nbreak'), '"line\nbreak"');
  for (const c of ['=1+1', '+1', '-1', '@x']) assert.equal(w.qhCsvCell(c), "'" + c, c);
});

test('csv: BOM, header row, ISO time and one line per row', dom, () => {
  const w = auditWin();
  const text = w.qhAuditCsv([
    arow(1, { detail: { note: 'x' } }),
    arow(2, { actor: { kind: 'auto', handle: 'auto' }, request: { id: 9, connection: 'prod-main', database: 'users', sql: '=SELECT 1' } }),
  ]);
  assert.equal(text.charCodeAt(0), 0xFEFF, 'UTF-8 BOM first');
  const lines = text.slice(1).split('\r\n');
  assert.equal(lines[0], 'time,actor,action,target,details');
  assert.ok(lines[1].startsWith('2026-11-18T10:00:00.000Z,Jordan Ray,Approved,prod-main/orders,'), lines[1]);
  assert.ok(lines[1].includes('"{""note"":""x""}"'), 'details as one quoted text field');
  assert.ok(lines[2].includes('Auto-approver,Approved,prod-main/users,'), lines[2]);
  assert.ok(lines[2].endsWith("'=SELECT 1"), 'formula guard on the details');
});

test('csv: the file name carries the local date', dom, () => {
  const w = auditWin();
  assert.equal(w.qhAuditCsvName(new Date(2026, 10, 9)), 'queryhub-audit-2026-11-09.csv');
});

// ---------- 2. approvals history ----------

const SUPER = { name: 'Jordan Ray', role: 'super', isDba: true };
function qrow(id, extra) {
  return { id, submitter: { name: 'Jordan Ray', initials: 'JR', slackId: 'U0EXAMPLE001' },
    connectionId: 'prod-main', databaseId: 'orders', env: 'production', tier: 'RW',
    sql: 'UPDATE t SET a = 1 WHERE id = 2;', statements: 1, piiCols: [], estRows: 1,
    estTables: ['t'], reason: 'Fix one row.', submittedAt: '2026-10-23T08:00:00Z', ...extra };
}
function decision(id, time, label, note) {
  return arow(id, { time, actionLabel: label, action: label.toLowerCase(),
    request: { id: 100 + id, requester: 'Casey Lin', connection: 'prod-main', database: 'payments', tier: 'RW', decisionNote: note || null } });
}

function mountQueue(queue, auditRows) {
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
  const searches = [];
  win.qhApi = { adminAuditSearch: (p) => { searches.push(p); return Promise.resolve({ rows: auditRows }); } };
  const st = { queue, manualRuns: [], endpointReqs: [], autoRequests: [], grants: [], people: [], teams: [],
    killSwitch: { enabled: false }, killHistory: () => Promise.resolve([]), toggleKill: () => Promise.resolve({}),
    decide: () => {}, batchApprove: () => {}, approveBundle: () => {} };
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(win.AdminPanel, { st, user: SUPER, modeEntry: null })));
  const doc = win.document;
  const clickHistory = async () => {
    const b = [...doc.querySelectorAll('button')].find(x => x.textContent.trim() === 'History');
    await React.act(async () => { b.dispatchEvent(new win.MouseEvent('click', { bubbles: true })); });
  };
  return { doc, clickHistory, searches };
}

test('history: newest first, with who decided, the requester and the note', dom, async () => {
  const ui = mountQueue([qrow('q1')], [
    decision(1, '2026-11-17T09:00:00Z', 'Rejected', 'Too broad.'),
    decision(2, '2026-11-18T09:00:00Z', 'Approved'),
  ]);
  assert.ok(ui.doc.querySelector('.qh-batchbar'), 'batch bar on the queue');
  await ui.clickHistory();
  assert.deepEqual(ui.searches[0], { categories: ['requests'], effects: ['decided'], limit: 50 });
  const rows = [...ui.doc.querySelectorAll('.qh-qhistory-row')];
  assert.equal(rows.length, 2);
  assert.ok(rows[0].textContent.includes('Approved'), 'newest first');
  assert.ok(rows[1].textContent.includes('Rejected'));
  assert.ok(rows[1].textContent.includes('Note: Too broad.'));
  assert.ok(rows[1].textContent.includes('Casey Lin'));
  assert.ok(rows[1].textContent.includes('prod-main/payments'));
  assert.ok(rows[1].textContent.includes('Jordan Ray'));
});

test('history: the batch bar and Select all are off', dom, async () => {
  const ui = mountQueue([qrow('q1')], [decision(1, '2026-11-18T09:00:00Z', 'Approved')]);
  await ui.clickHistory();
  assert.equal(ui.doc.querySelector('.qh-batchbar'), null);
  assert.equal(ui.doc.querySelector('.qh-batchbar-all'), null);
});

test('history: shows the 50 newest and the empty state', dom, async () => {
  const many = Array.from({ length: 60 }, (_, i) => decision(i + 1, new Date(Date.UTC(2026, 10, 1, 0, i)).toISOString(), 'Approved'));
  const a = mountQueue([qrow('q1')], many);
  await a.clickHistory();
  assert.equal(a.doc.querySelectorAll('.qh-qhistory-row').length, 50);
  const b = mountQueue([qrow('q1')], []);
  await b.clickHistory();
  assert.ok(b.doc.body.textContent.includes('No decisions yet.'));
});

test('history: the decide keys and the detail panel are off', () => {
  const src = web('qh-admin.jsx');
  assert.match(src, /keyRef\.current = \(e\) => \{\n\s+\/\/ History lists decided requests[^\n]*\n\s+if \(showHist\) return;/);
  assert.match(src, /\{showHist \? <div className="qh-aempty big">/);
});
