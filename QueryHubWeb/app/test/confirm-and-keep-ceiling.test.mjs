// Three admin and workspace fixes taken from the IdP app (2026-11-19).
//
//   1. An edit of an admin role keeps its tier ceiling. Before, every edit of
//      such a row (a new end date, a new reason) cleared the ceiling.
//   2. Revoke of an auto-approve rule and Remove of an admin scope ask first,
//      in an inline row.
//   3. Delete of a saved query or a saved workspace asks first. A server
//      delete that fails keeps the row.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { loadInto } from './_load.mjs';
import { web } from './_mount.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }
const opts = JSDOM ? {} : { skip: 'jsdom not installed' };

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
  // qh-app.jsx mounts the whole app when it loads. The stub root keeps that
  // render out of this test, which mounts one component on its own.
  win.ReactDOM = { ...require_('react-dom'), createRoot: () => ({ render() {}, unmount() {} }) };
  win.confirm = () => { throw new Error('window.confirm must not be used'); };
  loadInto(win, ...files);
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(win[View], props(win))));
  const doc = win.document;
  const all = (sel, from) => [...(from || doc).querySelectorAll(sel)];
  const flush = () => React.act(() => new Promise(r => setTimeout(r, 0)));
  const click = (el) => React.act(() => el.dispatchEvent(new win.MouseEvent('click', { bubbles: true })));
  const act = (fn) => React.act(fn);
  return { win, doc, all, flush, click, act };
}

const ADMIN_FILES = ['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx',
  'qh-admin-data.jsx', 'qh-admin-effective.jsx', 'qh-admin-access.jsx', 'qh-admin-roles.jsx'];
const btn = (ui, text, from) => {
  const b = ui.all('button', from).find(x => x.textContent.trim() === text);
  assert.ok(b, 'no button reads "' + text + '"');
  return b;
};
const rows = (ui) => ui.all('.qh-revokeconfirm');
const PEOPLE = [{ id: 'U0EXAMPLE001', handle: 'jordan.ray', name: 'Jordan Ray', initials: 'JR' }];

// ---------- 1. admin role ceiling ----------

test('an edit of an admin role with a ceiling sends the stored ceiling back', opts, async () => {
  const calls = [];
  const init = { id: 9, subject: 'U0EXAMPLE001', role: 'admin', maxTier: 'RW', scopeTeamId: null, scopeTargetId: null,
    validUntil: null, reason: 'old reason', source: 'direct' };
  const ui = mount(ADMIN_FILES, 'RoleForm', () => ({ init, onDone: () => {},
    st: { people: PEOPLE, teams: [], connections: [], connectionRows: [], pushToast: () => {},
      updateRole: (id, next, was) => { calls.push({ id, next, was }); return Promise.resolve({}); } } }));
  const reason = ui.all('input, textarea').find(x => x.value === 'old reason');
  assert.ok(reason, 'the reason field is on the form');
  ui.act(() => {
    const set = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(reason), 'value').set;
    set.call(reason, 'new reason');
    reason.dispatchEvent(new ui.win.Event('input', { bubbles: true }));
  });
  assert.doesNotMatch(ui.doc.body.textContent, /Tier ceiling.*no ceiling/, 'the summary does not show a cleared ceiling');
  const save = ui.all('button').find(b => /^(Save|Widen)/.test(b.textContent.trim()) && b.className.includes('qh-btn-'));
  assert.ok(save && !save.disabled, 'Save is on');
  ui.click(save);
  await ui.flush();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].next.maxTier, 'RW', 'the ceiling stays');
  assert.equal(calls[0].next.reason, 'new reason');
});

test('updateRole sends no tier field when the ceiling did not move', () => {
  const src = web('qh-admin-data.jsx');
  assert.match(src, /if \(moved\('maxTier'\)\) \{ if \(r\.maxTier\) body\.maxTier = r\.maxTier; else body\.clearMaxTier = true; \}/);
  const roles = web('qh-admin-roles.jsx');
  assert.match(roles, /editing && fleetWide && init\.role === 'admin' \? \(init\.maxTier \|\| null\) : null/,
    'only an admin-to-admin edit keeps the stored ceiling');
});

// ---------- 2. revoke and remove ask first ----------

const FUTURE = new Date(Date.now() + 30 * 86400000).toISOString();
const rule = { id: '21', user: 'U0EXAMPLE001', userName: 'Jordan Ray', subjectType: 'user', tier: 'RO', connectionId: 'prod-main',
  databaseId: null, expiresAt: FUTURE, enabled: true, runCount: 3, createdBy: 'U0EXAMPLE009' };

test('auto-approve Revoke opens a row, Keep rule closes it, Revoke rule revokes once', opts, async () => {
  const revoked = [];
  const ui = mount(ADMIN_FILES, 'AutoView', (win) => {
    win.qhApi = {};
    return { user: { name: 'Example Admin' }, st: { autoGrants: [rule], autoRequests: [], people: PEOPLE, teams: [], connections: [],
      revokeAutoGrant: (id, actor) => { revoked.push([id, actor]); } } };
  });
  const card = () => ui.all('.qh-autosub').find(x => x.textContent.includes('Jordan Ray'));
  ui.click(btn(ui, 'Revoke', card()));
  assert.equal(rows(ui).length, 1, 'the row opens');
  assert.match(rows(ui)[0].textContent, /Revoke this auto-approve rule\? A DBA then reviews the RO queries of Jordan Ray on prod-main/);
  assert.match(rows(ui)[0].textContent, /You cannot undo this\./);
  assert.deepEqual(revoked, [], 'the first click revokes nothing');
  ui.click(btn(ui, 'Keep rule', rows(ui)[0]));
  assert.equal(rows(ui).length, 0);
  assert.deepEqual(revoked, []);
  ui.click(btn(ui, 'Pause', card()));
  ui.click(btn(ui, 'Revoke', card()));
  assert.equal(rows(ui).length, 1, 'one row at a time: Revoke closes the pause row');
  ui.click(btn(ui, 'Revoke rule', rows(ui)[0]));
  assert.deepEqual(revoked, [['21', 'dba.example']]);
  assert.equal(rows(ui).length, 0);
});

test('admin scope Remove opens a row, Keep scope closes it, Remove scope removes once', opts, () => {
  const removed = [];
  const scope = (id, admin) => ({ id, admin, role: 'dba', canApprove: ['RO'], connections: ['prod-main'], expiresAt: null });
  const ui = mount(ADMIN_FILES, 'ScopesView', (win) => {
    win.qhApi = {};
    return { user: { name: 'Example Admin' }, st: { scopes: [scope('s1', 'jordan.ray'), scope('s2', 'casey.lin')], people: PEOPLE, teams: [], connections: [],
      removeScope: (id, actor) => { removed.push([id, actor]); } } };
  });
  const tr = () => ui.all('tr').find(x => x.textContent.includes('jordan.ray') && !x.querySelector('.qh-revokeconfirm'));
  ui.click(btn(ui, 'Remove', tr()));
  assert.equal(rows(ui).length, 1);
  assert.match(rows(ui)[0].textContent, /Remove the admin scope of jordan\.ray\? jordan\.ray then cannot approve queries on prod-main\. You cannot undo this\./);
  assert.deepEqual(removed, []);
  ui.click(btn(ui, 'Keep scope', rows(ui)[0]));
  assert.equal(rows(ui).length, 0);
  ui.click(btn(ui, 'Remove', tr()));
  ui.click(btn(ui, 'Remove scope', rows(ui)[0]));
  assert.deepEqual(removed, [['s1', 'dba.example']]);
});

// ---------- 3. delete asks first, a failed delete keeps the row ----------

test('a saved query or a workspace delete opens the confirm and waits for the server', () => {
  const app = web('qh-app.jsx');
  assert.doesNotMatch(app, /deleteSnippet\(id\)\.catch\(\(\) => \{\}\)/, 'no silent catch on a saved-query delete');
  assert.doesNotMatch(app, /deleteSessionSrv\(id\)\.catch\(\(\) => \{\}\)/, 'no silent catch on a workspace delete');
  assert.match(app, /setDelAsk\(\{ kind: 'saved', id, name: s\.name, server: s\.dest === 'server' \}\)/);
  assert.match(app, /setDelAsk\(\{ kind: 'session', id, name: s\.name, server: s\.dest === 'server' \}\)/);
  // The row goes only in the success branch of the server call.
  assert.match(app, /\.then\(\(\) => \{ drop\(\); setDelBusy\(false\); setDelAsk\(null\); \}\)/);
  assert.match(app, /is still saved\.', 'err'\)/);
  assert.match(app, /\{delAsk && <DeleteConfirmModal /);
});

test('the delete confirm names the item and where it lives', opts, () => {
  let confirmed = 0, cancelled = 0;
  const ui = mount(['qh-data.jsx', 'qh-modal.jsx', 'qh-app.jsx'], 'DeleteConfirmModal', () => ({
    ask: { kind: 'saved', id: 'q1', name: 'daily orders', server: true }, busy: false,
    onConfirm: () => { confirmed += 1; }, onCancel: () => { cancelled += 1; } }));
  assert.match(ui.doc.body.textContent, /Delete the saved query\?/);
  assert.match(ui.doc.body.textContent, /QueryHub deletes “daily orders” from the server\. You cannot undo this\./);
  ui.click(btn(ui, 'Keep'));
  ui.click(btn(ui, 'Delete'));
  assert.equal(cancelled, 1);
  assert.equal(confirmed, 1);
});
