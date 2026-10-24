// Debt batch D2 of 2026-10-24: an inline confirm in Roles and Masking, the metrics
// window in the data hook, and a name in the kill switch history.
// The mount helper is copied from design-debt.test.mjs.
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

const BASE = ['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx', 'qh-admin-data.jsx', 'qh-admin-effective.jsx'];

function mount(files, View, props) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>',
                        { pretendToBeVisual: true, url: 'https://localhost/#admin/roles' });
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
  loadInto(win, ...BASE, ...files);
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(win[View], props(win))));
  const doc = win.document;
  const all = (sel) => [...doc.querySelectorAll(sel)];
  const flush = () => React.act(() => new Promise(r => setTimeout(r, 0)));
  const click = (el) => React.act(() => el.dispatchEvent(new win.MouseEvent('click', { bubbles: true })));
  return { win, doc, all, flush, click };
}
const btn = (root, text) => [...root.querySelectorAll('button')].find(b => b.textContent.trim() === text);

// ---------- 1a. Roles ----------

const ROLE = { id: 'r1', subject: 'U0EXAMPLE001', name: 'Jordan Ray', role: 'approver', source: 'direct', enabled: true,
  allTeams: true, allTargets: true, anyTier: false, maxTier: 'RW', validUntil: null, reason: 'rota' };

test('Roles: Revoke opens an inline row, Keep role closes it, Revoke role removes once', opts, async () => {
  const removed = [];
  const ui = mount(['qh-admin-access.jsx', 'qh-admin-roles.jsx'], 'RolesView', (win) => {
    win.confirm = () => { throw new Error('window.confirm must not be used'); };
    return { canWrite: true, user: { name: 'Sam Doe', role: 'super' },
      st: { roles: [ROLE], rolesEnforced: true, people: [], teams: [], connections: [], removeRole: (id) => { removed.push(id); return Promise.resolve({}); } } };
  });
  const opener = () => ui.all('.qh-rolerow .qh-revoke')[0];
  assert.ok(opener(), 'a Revoke button');
  assert.equal(ui.all('.qh-revokeconfirm').length, 0);
  ui.click(opener());
  const row = ui.doc.querySelector('.qh-revokeconfirm');
  assert.ok(row, 'the first click opens the row');
  assert.match(row.textContent, /Revoke Jordan Ray’s .* role\? They keep their own database access/);
  assert.equal(removed.length, 0);
  ui.click(opener());
  assert.equal(ui.all('.qh-revokeconfirm').length, 0, 'a second click closes it');
  ui.click(opener());
  ui.click(btn(ui.doc.querySelector('.qh-revokeconfirm'), 'Keep role'));
  assert.equal(ui.all('.qh-revokeconfirm').length, 0);
  assert.equal(removed.length, 0, 'cancel makes no call');
  ui.click(opener());
  ui.click(btn(ui.doc.querySelector('.qh-revokeconfirm'), 'Revoke role'));
  assert.deepEqual(removed, ['r1']);
  assert.equal(ui.all('.qh-revokeconfirm').length, 0);
});

// ---------- 1b. Masking ----------

const EXEMPT = { id: 'm1', scope: 'column', connectionId: 'c1', connectionName: 'orders-main', databaseId: 'd1', databaseName: 'orders',
  schema: 'public', table: 'customers', column: 'email', enabled: true, strength: 'soft', survivesJoin: false,
  reason: 'Support needs it', createdBy: 'Jordan Ray', createdAt: '2026-10-01T00:00:00Z', expiresAt: null };

test('Masking: Remove opens an inline row, Keep exemption closes it, Remove exemption removes once', opts, async () => {
  const removed = [];
  const ui = mount(['qh-admin-access.jsx', 'qh-admin-roles.jsx', 'qh-admin-mask.jsx'], 'MaskingView', (win) => {
    win.confirm = () => { throw new Error('window.confirm must not be used'); };
    return { user: { name: 'Sam Doe', role: 'super' },
      st: { maskExemptions: [EXEMPT], maskMeta: { maskingEnabled: true }, connections: [], teams: [], people: [],
        removeMaskExemption: (id) => { removed.push(id); return Promise.resolve({}); } } };
  });
  ui.click(ui.doc.querySelector('.qh-mxitem-top'));
  const openMenu = () => ui.click(ui.doc.querySelector('[aria-label="More actions"]'));
  const pickRemove = () => ui.click([...ui.doc.querySelectorAll('[role=menuitem]')].find(b => b.textContent.trim() === 'Remove'));
  assert.equal(ui.all('.qh-revokeconfirm').length, 0);
  openMenu(); pickRemove();
  const row = ui.doc.querySelector('.qh-revokeconfirm');
  assert.ok(row, 'the first click opens the row');
  assert.match(row.textContent, /Remove this exemption\? QueryHub masks customers\.email again/);
  assert.equal(removed.length, 0);
  openMenu(); pickRemove();
  assert.equal(ui.all('.qh-revokeconfirm').length, 0, 'a second click closes it');
  openMenu(); pickRemove();
  ui.click(btn(ui.doc.querySelector('.qh-revokeconfirm'), 'Keep exemption'));
  assert.equal(ui.all('.qh-revokeconfirm').length, 0);
  assert.equal(removed.length, 0, 'cancel makes no call');
  openMenu(); pickRemove();
  ui.click(btn(ui.doc.querySelector('.qh-revokeconfirm'), 'Remove exemption'));
  assert.deepEqual(removed, ['m1']);
});

test('neither Roles nor Masking calls window.confirm', () => {
  for (const f of ['qh-admin-roles.jsx', 'qh-admin-mask.jsx']) {
    assert.doesNotMatch(web(f), /window\.confirm|[^.\w]confirm\(/, f);
    assert.match(web(f), /qh-revokeconfirm/, f);
  }
});

// ---------- 2. The metrics window ----------

test('the data hook asks for the window the Metrics screen opens on', () => {
  assert.match(web('qh-admin-data.jsx'), /qhApi\.adminMetrics\('30d'\)/);
  assert.doesNotMatch(web('qh-admin-data.jsx'), /qhApi\.adminMetrics\(\)/);
  assert.match(web('qh-admin-insights.jsx'), /useIns\('30d'\)/);
});

// ---------- 3. A name in the kill history ----------

const PEOPLE = [{ id: 'U0EXAMPLE001', handle: 'jordan.ray', name: 'Jordan Ray' }];
const HIST = [
  { startedAt: '2026-10-20T10:00:00Z', endedAt: '2026-10-20T10:05:00Z', by: 'U0EXAMPLE001', endedBy: 'jordan.ray', message: 'a', cancelledCount: 1 },
  { startedAt: '2026-10-21T10:00:00Z', endedAt: '2026-10-21T10:05:00Z', by: 'U0EXAMPLE999', endedBy: 'U0EXAMPLE998', message: 'b', cancelledCount: 0 },
  { startedAt: '2026-10-22T10:00:00Z', endedAt: '2026-10-22T10:05:00Z', by: 'Sam Doe', endedBy: 'Sam Doe', message: 'c', cancelledCount: 0 },
];

test('Kill history: an id shows the name, an unknown id stays, a named row stays', opts, () => {
  const ui = mount(['qh-admin.jsx'], 'KillHistory', () => ({ rows: HIST, onRetry: () => {}, people: PEOPLE }));
  const rows = ui.all('tbody tr');
  assert.equal(rows.length, 3);
  const by = (i) => rows[i].querySelectorAll('td')[1].textContent;
  const rel = (i) => rows[i].querySelectorAll('td')[3].textContent;
  assert.equal(by(0), 'Jordan Ray');
  assert.match(rel(0), /released by Jordan Ray/);
  assert.equal(by(1), 'U0EXAMPLE999');
  assert.match(rel(1), /released by U0EXAMPLE998/);
  assert.equal(by(2), 'Sam Doe');
  assert.match(rel(2), /released by Sam Doe/);
});

test('qhKillWho looks up by id or handle and keeps the raw value when nobody matches', () => {
  const w = loadInto(bareWindow(), 'qh-data.jsx', 'qh-admin-data.jsx', 'qh-admin-effective.jsx', 'qh-admin.jsx');
  assert.equal(w.qhKillWho('U0EXAMPLE001', PEOPLE), 'Jordan Ray');
  assert.equal(w.qhKillWho('jordan.ray', PEOPLE), 'Jordan Ray');
  assert.equal(w.qhKillWho('U0EXAMPLE404', PEOPLE), 'U0EXAMPLE404');
  assert.equal(w.qhKillWho('U0EXAMPLE001', undefined), 'U0EXAMPLE001');
});
