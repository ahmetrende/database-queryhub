// Effective access for an admin below super-admin (2026-11-12), mounted in jsdom.
//
// The list said "Can query 172 connections · approver" for an admin who could
// reach 62: disabled connections counted, sorted in among the usable ones, and
// the approver card said "up to RW" under rows that said DDL. Now the count is
// of usable connections, disabled ones have their own count and sort last,
// the label names the role, and a line says which of their own requests they
// approve. A read-only engine row and a super-admin-only row say why they
// differ.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { loadInto } from './_load.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }
const opts = JSDOM ? {} : { skip: 'jsdom not installed' };

const SUPER = { name: 'Jordan Ray', role: 'super', isDba: true };
const row = (id, extra) => ({ connectionId: id, key: id, enabled: true, tier: 'DDL', source: 'admin_or_bypass',
  allDatabases: true, databases: null, perDatabase: [], mixedTiers: false, readOnlyEngine: false,
  superAdminOnly: false, ...extra });
const EFF = { slackId: 'U0EXAMPLE001', name: 'Sam Lee', known: true, teams: [], blocked: [], autoApprove: [],
  rowLimitOverride: null,
  access: [row('a-old', { enabled: false }), row('b-ledger'), row('c-ticks', { tier: 'RO', readOnlyEngine: true }),
           row('d-control', { tier: 'RO', source: 'user', superAdminOnly: true }),
           row('e-search', { credentialTiers: ['ro'] })],
  admin: { enabled: true, superAdmin: false, role: 'admin', selfApproves: 'ro', maxTier: 'RW',
           scopeTeams: [], scopeTargets: [], scopeTeamsAll: true, scopeTargetsAll: true, canGrant: false } };

async function mount(eff) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>',
                        { pretendToBeVisual: true, url: 'https://localhost/#admin/effective' });
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
  loadInto(win, 'qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx', 'qh-admin-data.jsx', 'qh-admin-effective.jsx');
  const st = { people: [{ id: 'U0EXAMPLE001', handle: 'U0EXAMPLE001', name: 'Sam Lee', initials: 'SL' }],
    teams: [], grants: [], queue: [], manualRuns: [], effectiveAccess: () => Promise.resolve(eff) };
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(win.EffectiveAccessView, { st, user: SUPER })));
  const all = (sel) => [...win.document.querySelectorAll(sel)];
  React.act(() => all('.qh-eff-pick-row')[0].dispatchEvent(new win.MouseEvent('click', { bubbles: true })));
  await React.act(() => new Promise(r => setTimeout(r, 0)));
  return { doc: win.document, all };
}

test('the count is of usable connections, the disabled ones are counted apart', opts, async () => {
  const ui = await mount(EFF);
  const sum = ui.doc.querySelector('.qh-eff-sum').textContent;
  assert.match(sum, /Can query 4 connections/);
  assert.match(sum, /1 disabled/);
  assert.match(sum, /· admin/);
  assert.doesNotMatch(sum, /approver/);
});

test('a disabled connection sorts after the usable ones', opts, async () => {
  const ui = await mount(EFF);
  const names = ui.all('.qh-eff-row .qh-eff-conn').map(e => e.textContent);
  assert.deepEqual(names, ['b-ledger', 'c-ticks', 'd-control', 'e-search', 'a-old']);
});

test('the approver card says which of their own requests they approve', opts, async () => {
  const ui = await mount(EFF);
  assert.match(ui.doc.querySelector('.qh-eff-appr').textContent, /Their own RW and DDL requests go to another admin/);
  assert.match(ui.doc.querySelector('.qh-eff-appr').textContent, /every connection except the super-admin-only ones/);
});

test('a read-only engine row and a super-admin-only row say why they differ', opts, async () => {
  const ui = await mount(EFF);
  const rows = ui.all('.qh-eff-row');
  const ticks = rows.find(r => r.querySelector('.qh-eff-conn').textContent === 'c-ticks');
  const ctrl = rows.find(r => r.querySelector('.qh-eff-conn').textContent === 'd-control');
  assert.match(ticks.textContent, /read-only engine/);
  assert.match(ctrl.textContent, /Super-admin only connection/);
  const ledger = rows.find(r => r.querySelector('.qh-eff-conn').textContent === 'b-ledger');
  assert.doesNotMatch(ledger.textContent, /read-only engine|Super-admin only/);
});

test('a row whose tier has no login says so', opts, async () => {
  const ui = await mount(EFF);
  const rows = ui.all('.qh-eff-row');
  const search = rows.find(r => r.querySelector('.qh-eff-conn').textContent === 'e-search');
  assert.match(search.textContent, /no RW\/DDL login/);
  const ledger = rows.find(r => r.querySelector('.qh-eff-conn').textContent === 'b-ledger');
  assert.doesNotMatch(ledger.textContent, /login/);
});
