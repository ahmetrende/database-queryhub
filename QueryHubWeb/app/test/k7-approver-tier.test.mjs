// K7 (2026-10-24): an approver approves RO or RW, never DDL. The DBA scope form
// and the Roles form follow the server, and the mock refuses the same bodies.
// (Mount helper copied from admin-batch3b.test.mjs.)
//
//   * Connections: a red banner when a connection fails, "Test again", and the
//     provider as its own column.
//   * Revoke grant: an inline confirm that says what changes.
//   * Admin scope form: PersonPick and connection chips. The payload is the
//     same as before.
//   * Masking: the "Ends soon" filter.
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

const pure = loadInto(bareWindow(), 'qh-data.jsx', 'qh-admin-data.jsx', 'qh-admin-access.jsx');

function mount(View, props) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>',
                        { pretendToBeVisual: true, url: 'https://localhost/#admin/conns' });
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
  loadInto(win, 'qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx', 'qh-admin-data.jsx', 'qh-admin-effective.jsx', 'qh-admin-access.jsx', 'qh-admin-roles.jsx');
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(win[View], props(win))));
  const doc = win.document;
  const all = (sel) => [...doc.querySelectorAll(sel)];
  const flush = () => React.act(() => new Promise(r => setTimeout(r, 0)));
  const click = (el) => React.act(() => el.dispatchEvent(new win.MouseEvent('click', { bubbles: true })));
  return { win, doc, all, flush, click, act: (fn) => React.act(fn) };
}

const conn = (id, name, health) => ({ id, name, engine: 'PostgreSQL', env: 'production', enabled: true, host: 'h', port: 5432,
  defaultDatabase: 'd', databases: [], owners: [], tags: { provider: 'aws' }, health });
const CONNS = [
  conn('c1', 'orders-main', { ok: false, failingSince: '2026-10-23T08:00:00Z', error: 'timeout' }),
  conn('c2', 'billing-main', { ok: true }),
  conn('c3', 'ledger-main', { ok: false, failingSince: '2026-10-23T09:00:00Z', error: 'refused' }),
];
const connSt = (extra) => ({ endpointReqs: [], connectionRows: CONNS, connections: CONNS, pushToast: () => {}, ...extra });



const PEOPLE = [{ id: 'U0EXAMPLE001', handle: 'jordan.ray', name: 'Jordan Ray', initials: 'JR' }];
const stBase = (extra) => ({ people: PEOPLE, teams: [], connections: [], connectionRows: [], pushToast: () => {}, ...extra });
const tierBtns = (ui) => ui.all('.qh-tierchk').map(b => b.textContent.replace('\u2713', '').trim());
const addBtn = (ui) => ui.all('button').find(b => b.textContent === 'Add');

// ---------- the DBA scope form ----------

test('a new DBA scope offers RO and RW, and no DDL', opts, () => {
  const ui = mount('ScopeForm', () => ({ init: { admin: '', role: 'dba', canApprove: ['RO'], connections: '*' }, actor: 'dba.x', st: stBase(), onDone: () => {} }));
  assert.deepEqual(tierBtns(ui), ['RO', 'RW']);
});

test('a super-admin scope still shows every tier', opts, () => {
  const ui = mount('ScopeForm', () => ({ init: { admin: '', role: 'dba', canApprove: ['RO'], connections: '*' }, actor: 'dba.x', st: stBase(), onDone: () => {} }));
  ui.click(ui.all('.qh-seg-opt').find(b => b.textContent === 'super-admin'));
  assert.deepEqual(tierBtns(ui), ['RO', 'RW', 'DDL']);
});

test('a stored scope with DDL shows it, blocks Save with the reason, and Save opens after the untick', opts, () => {
  const saved = [];
  const ui = mount('ScopeForm', () => ({ init: { id: 'U0EXAMPLE001', admin: 'jordan.ray', role: 'dba', canApprove: ['RO', 'DDL'], connections: '*', expiresAt: null },
    actor: 'dba.x', st: stBase({ saveScope: (...a) => saved.push(a) }), onDone: () => {} }));
  assert.deepEqual(tierBtns(ui), ['RO', 'RW', 'DDL'], 'DDL shows so that it can be unticked');
  const save = () => ui.all('button').find(b => b.textContent === 'Save');
  assert.equal(save().disabled, true);
  assert.match(save().parentElement.title, /An admin scope can approve RO or RW\. Untick DDL, or make this person a super-admin\./);
  ui.click(ui.all('.qh-tierchk').find(b => b.textContent.startsWith('DDL')));
  assert.deepEqual(tierBtns(ui), ['RO', 'RW'], 'the DDL box is gone once it is off');
  assert.equal(save().disabled, false);
  ui.click(save());
  assert.deepEqual(saved[0][0].canApprove, ['RO']);
});

test('a DBA scope with no tier is blocked with its own reason', opts, () => {
  const ui = mount('ScopeForm', () => ({ init: { admin: 'jordan.ray', role: 'dba', canApprove: ['RO'], connections: '*' }, actor: 'dba.x', st: stBase(), onDone: () => {} }));
  ui.click(ui.all('.qh-tierchk').find(b => b.textContent.startsWith('RO')));
  assert.equal(addBtn(ui).disabled, true);
  assert.equal(addBtn(ui).parentElement.title, 'Pick RO or RW.');
});

// ---------- the Roles form ----------

const roleOptions = (ui) => [...ui.doc.querySelectorAll('.qh-rolefield select option')].map(o => o.textContent);
const ceilingSelect = (ui) => [...ui.doc.querySelectorAll('.qh-rolefield')].find(l => /Tier ceiling/.test(l.textContent)).querySelector('select');

test('a new approver role starts on RO, with no "No ceiling" and no DDL', opts, () => {
  const ui = mount('RoleForm', () => ({ st: stBase({ addRole: () => Promise.resolve({}) }), onDone: () => {} }));
  const sel = ceilingSelect(ui);
  assert.equal(sel.value, 'RO');
  assert.deepEqual([...sel.options].map(o => o.textContent), ['Pick RO or RW', 'Read (RO)', 'Write (RW)']);
  assert.ok(![...sel.options].some(o => /No ceiling|DDL/.test(o.textContent)));
});

test('a stored approver with a DDL ceiling opens on the placeholder and Save waits for a pick', opts, () => {
  const init = { id: 7, subject: 'U0EXAMPLE001', role: 'approver', maxTier: 'DDL', scopeTeamId: null, scopeTargetId: null, validUntil: null, reason: 'legacy' };
  const ui = mount('RoleForm', () => ({ st: stBase({ updateRole: () => Promise.resolve({}) }), onDone: () => {}, init }));
  const sel = ceilingSelect(ui);
  assert.equal(sel.value, '');
  const save = ui.all('button').find(b => /^(Save|Add|Widen)/.test(b.textContent.trim()) && b.className.includes('qh-btn-'));
  assert.ok(save && save.disabled, 'Save is off while the ceiling is empty');
  ui.act(() => { sel.value = 'RW'; sel.dispatchEvent(new ui.win.Event('change', { bubbles: true })); });
  const after = ui.all('button').find(b => /^(Save|Add|Widen)/.test(b.textContent.trim()) && b.className.includes('qh-btn-'));
  assert.equal(after.disabled, false);
});

test('the roles help text says what an approver may approve', () => {
  assert.match(web('qh-admin-roles.jsx'), /An approver never approves a schema change: that needs a super-admin\./);
});

// ---------- the mock refuses what the server refuses ----------

test('the mock refuses a DDL scope and an approver without RO or RW with tier_not_allowed', () => {
  const mock = web('qh-api-mock.jsx');
  assert.match(mock, /MOCK_APPROVER_SCOPE_SAY, 400, 'tier_not_allowed'/);
  assert.equal((mock.match(/MOCK_APPROVER_ROLE_SAY, 400, 'tier_not_allowed'/g) || []).length, 2, 'create and edit');
  assert.match(mock, /An admin scope can approve RO or RW\. Schema changes need a super-admin\./);
  assert.match(mock, /An approver needs a tier ceiling of RO or RW\. Schema changes need a super-admin\./);
});
