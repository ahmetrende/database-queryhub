// Design debt of 2026-10-24: the fleet grants Remove asks inline, the end word is
// "No end date", and the masking end state shares the clock of qhEndState.
// Admin batch 3b of 2026-10-24 (helper copied).
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
  loadInto(win, 'qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx', 'qh-admin-data.jsx', 'qh-admin-effective.jsx', 'qh-admin-access.jsx');
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


// ---------- fleet grants: an inline confirm, not window.confirm ----------

test('fleet Remove opens an inline row, Keep access closes it, Revoke access removes', opts, async () => {
  const revoked = [];
  const grants = [{ id: 'f1', subjectType: 'user', subject: 'U0EXAMPLE001', subjectName: 'Jordan Ray', tier: 'ro', autoApprove: true, expiresAt: null, reason: 'Support rota' }];
  const ui = mount('FleetGrantsBlock', (win) => {
    win.qhApi = { adminFleetGrants: () => Promise.resolve({ grants }) };
    win.confirm = () => { throw new Error('window.confirm must not be used'); };
    return { canWrite: true, st: { people: [{ id: 'U0EXAMPLE001', handle: 'jordan.ray', name: 'Jordan Ray' }], teams: [],
      revokeFleetGrant: (id, name) => { revoked.push([id, name]); return Promise.resolve({}); } } };
  });
  await ui.flush();
  const removeBtn = () => ui.all('.qh-fleet-row .qh-revoke')[0];
  assert.ok(removeBtn(), 'a Remove button');
  assert.equal(ui.all('.qh-fleet-confirm').length, 0);
  ui.click(removeBtn());
  const row = ui.doc.querySelector('.qh-fleet-confirm');
  assert.ok(row, 'the confirm row opens');
  assert.match(row.textContent, /Jordan Ray loses read-only access to every server\./);
  assert.match(row.textContent, /QueryHub also revokes the auto-approve on every server\./);
  assert.match(row.textContent, /A query that is running finishes\./);
  assert.equal(revoked.length, 0, 'the first click revokes nothing');
  ui.click([...row.querySelectorAll('button')].find(b => b.textContent === 'Keep access'));
  assert.equal(ui.all('.qh-fleet-confirm').length, 0);
  assert.equal(revoked.length, 0);
  ui.click(removeBtn());
  ui.click([...ui.doc.querySelector('.qh-fleet-confirm').querySelectorAll('button')].find(b => b.textContent === 'Revoke access'));
  await ui.flush();
  assert.deepEqual(revoked, [['f1', 'Jordan Ray']]);
});

// ---------- the word for no end ----------

test('no UI file says "No expiry": the word is "No end date"', () => {
  for (const f of ['qh-admin-access.jsx', 'qh-admin-mask.jsx', 'qh-data.jsx', 'qh-panels.jsx', 'qh-app.jsx', 'qh-admin.jsx']) {
    assert.doesNotMatch(web(f), /No expiry/, f);
  }
  assert.match(web('qh-admin-access.jsx'), /<option value="none">No end date<\/option>/);
});

// ---------- one end rule ----------

test('mxEndState takes its clock and its seven days from qhEndState', () => {
  assert.match(web('qh-admin-mask.jsx'), /qhEndState\(e\.expiresAt, now\)\.state/);
  const w = loadInto(bareWindow(), 'qh-data.jsx', 'qh-admin-data.jsx', 'qh-admin-mask.jsx');
  const now = Date.parse('2026-10-24T00:00:00Z');
  const DAY = 86400000;
  const at = (ms) => new Date(now + ms).toISOString();
  const cases = [
    [at(7 * DAY), 'soon', 'soon'],
    [at(7 * DAY + 3600000), 'ends', 'live'],
    [at(-3600000), 'ended', 'ended'],
    [null, 'none', 'none'],
  ];
  for (const [iso, kind, state] of cases) {
    assert.equal(w.mxEndState({ expiresAt: iso }, now).kind, kind, String(iso));
    assert.equal(w.qhEndState(iso, now).state, state, String(iso));
  }
  assert.equal(w.mxEndState({ expiresAt: at(30 * DAY), expired: true }, now).kind, 'ended', 'the server word wins');
  assert.equal(w.mxEndState({ expiresAt: 'not a date' }, now).kind, 'none');
  assert.equal(w.mxEndState({ expiresAt: at(3 * DAY) }, now).text, 'Ends in 3 days');
});
