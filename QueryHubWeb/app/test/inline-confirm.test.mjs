// Debt batch D1 of 2026-10-24: the six destructive admin actions in
// qh-admin-access.jsx ask in an inline row. None of them calls the browser
// confirm. The mount helper is copied from design-debt.test.mjs.
//
//   1. pause and resume of an auto-approve rule
//   2. delete of a team
//   3. remove of a connection owner
//   4. bulk disable of selected connections
//   5. disable of one connection (a replica says "out of rotation")
//   6. delete of one connection
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import { loadInto } from './_load.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }
const opts = JSDOM ? {} : { skip: 'jsdom not installed' };

const HERE = dirname(fileURLToPath(import.meta.url));
const web = (file) => readFileSync(resolve(HERE, '..', '..', file), 'utf8');


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
  const all = (sel, from) => [...(from || doc).querySelectorAll(sel)];
  const flush = () => React.act(() => new Promise(r => setTimeout(r, 0)));
  const click = (el) => React.act(() => el.dispatchEvent(new win.MouseEvent('click', { bubbles: true })));
  return { win, doc, all, flush, click };
}

const noConfirm = (win) => { win.confirm = () => { throw new Error('window.confirm must not be used'); }; };
const rows = (ui) => ui.all('.qh-revokeconfirm');
const btn = (ui, from, text) => {
  const b = ui.all('button', from).find(x => x.textContent.trim() === text);
  assert.ok(b, 'no button reads "' + text + '"');
  return b;
};

const FUTURE = new Date(Date.now() + 30 * 86400000).toISOString();
const rule = (id, user, name, enabled) => ({ id, user, userName: name, subjectType: 'user', tier: 'RO', connectionId: 'prod-main',
  databaseId: null, expiresAt: FUTURE, enabled, runCount: 3, createdBy: 'U0EXAMPLE009' });

// ---------- 1: auto-approve pause and resume ----------

test('auto-approve pause opens the row, Keep running closes it, Pause rule runs once', opts, async () => {
  const toggled = [];
  const ui = mount('AutoView', (win) => {
    noConfirm(win);
    win.qhApi = {};
    return { user: { name: 'Example Admin' }, st: { autoGrants: [rule('11', 'U0EXAMPLE001', 'Jordan Ray', true)], autoRequests: [], people: [], teams: [], connections: [],
      setAutoGrantEnabled: (id, on) => { toggled.push([id, on]); return Promise.resolve({ changed: true }); } } };
  });
  const card = () => ui.all('.qh-autosub').find(x => x.textContent.includes('Jordan Ray'));
  assert.equal(rows(ui).length, 0);
  ui.click(btn(ui, card(), 'Pause'));
  assert.equal(rows(ui).length, 1, 'the row opens');
  assert.match(rows(ui)[0].textContent, /Pause this auto-approve rule\? A DBA then reviews the queries of Jordan Ray on prod-main/);
  assert.deepEqual(toggled, [], 'the first click calls nothing');
  ui.click(btn(ui, card(), 'Pause'));
  assert.equal(rows(ui).length, 0, 'a second click on Pause closes the row');
  ui.click(btn(ui, card(), 'Pause'));
  ui.click(btn(ui, rows(ui)[0], 'Keep running'));
  assert.equal(rows(ui).length, 0);
  assert.deepEqual(toggled, [], 'Cancel calls nothing');
  ui.click(btn(ui, card(), 'Pause'));
  ui.click(btn(ui, rows(ui)[0], 'Pause rule'));
  await ui.flush();
  assert.deepEqual(toggled, [['11', false]]);
  assert.equal(rows(ui).length, 0);
});

test('auto-approve resume uses Keep paused and Resume rule', opts, async () => {
  const toggled = [];
  const ui = mount('AutoView', (win) => {
    noConfirm(win);
    win.qhApi = {};
    return { user: { name: 'Example Admin' }, st: { autoGrants: [rule('12', 'U0EXAMPLE002', 'Casey Lin', false)], autoRequests: [], people: [], teams: [], connections: [],
      setAutoGrantEnabled: (id, on) => { toggled.push([id, on]); return Promise.resolve({ changed: true }); } } };
  });
  const card = () => ui.all('.qh-autosub').find(x => x.textContent.includes('Casey Lin'));
  ui.click(btn(ui, card(), 'Resume'));
  assert.match(rows(ui)[0].textContent, /Resume this auto-approve rule\? The queries of Casey Lin on prod-main · all databases then run without a DBA again, up to RO/);
  assert.deepEqual(toggled, []);
  ui.click(btn(ui, rows(ui)[0], 'Keep paused'));
  assert.equal(rows(ui).length, 0);
  assert.deepEqual(toggled, []);
  ui.click(btn(ui, card(), 'Resume'));
  ui.click(btn(ui, rows(ui)[0], 'Resume rule'));
  await ui.flush();
  assert.deepEqual(toggled, [['12', true]]);
});

// ---------- 2: team delete ----------

test('team Delete opens the row, Keep closes it, Delete team removes once', opts, async () => {
  const removed = [];
  const ui = mount('TeamsView', (win) => {
    noConfirm(win);
    win.qhApi = {};
    return { user: { name: 'Example Admin' }, st: { teams: [{ id: 't1', name: 'team-alpha', desc: '', members: [], subteams: [] }], people: [], grants: [], grantsState: 'ready',
      removeTeam: (id, actor) => { removed.push([id, actor]); } } };
  });
  const del = () => ui.all('.qh-teamcard-acts .qh-revoke')[0];
  assert.equal(rows(ui).length, 0);
  ui.click(del());
  assert.equal(rows(ui).length, 1);
  assert.match(rows(ui)[0].textContent, /Delete team “team-alpha”\? The members stay\. QueryHub deletes only the team\./);
  assert.deepEqual(removed, []);
  ui.click(del());
  assert.equal(rows(ui).length, 0, 'a second click closes the row');
  ui.click(del());
  ui.click(btn(ui, rows(ui)[0], 'Keep'));
  assert.equal(rows(ui).length, 0);
  assert.deepEqual(removed, []);
  ui.click(del());
  ui.click(btn(ui, rows(ui)[0], 'Delete team'));
  assert.equal(removed.length, 1);
  assert.equal(removed[0][0], 't1');
});

// ---------- connections: shared setup for 3 to 6 ----------

const conn = (id, name, extra) => ({ id, name, engine: 'PostgreSQL', env: 'production', enabled: true, host: 'h.example.internal', port: 5432,
  defaultDatabase: 'd', databases: [], owners: [], tags: { provider: 'aws' }, health: { ok: true }, ...extra });

function mountConns(list, api, stExtra) {
  const calls = { enabled: [], removed: [], bulk: [], ownerRemoved: [] };
  const ui = mount('ConnectionsView', (win) => {
    noConfirm(win);
    win.qhApi = {
      adminConnectionOwners: () => Promise.resolve({ owners: [{ id: '7', displayName: 'Team Alpha' }], approvers: [], teams: [], maxTier: 'RO' }),
      adminRemoveConnectionOwner: (cid, oid) => { calls.ownerRemoved.push([cid, oid]); return Promise.resolve({ owners: [], approvers: [], changed: {} }); },
      ...api,
    };
    return { user: { name: 'Example Admin', role: 'super' }, st: { endpointReqs: [], connectionRows: list, connections: list, pushToast: () => {},
      setConnectionEnabled: (id, on) => { calls.enabled.push([id, on]); },
      removeConnection: (id) => { calls.removed.push(id); },
      bulkConnections: (b) => { calls.bulk.push(b); return Promise.resolve({}); },
      ...stExtra } };
  });
  return { ui, calls };
}
// Open the row menu of the n-th table row and pick an item by its label.
async function pickMenu(ui, n, label) {
  const menus = ui.all('.qh-conntable tbody tr:not(.qh-revokerow) .qh-rowmenu-btn');
  ui.click(menus[n]);
  const item = ui.all('.qh-rowmenu-item').find(x => x.querySelector('span').textContent === label);
  assert.ok(item, 'no menu item "' + label + '"');
  ui.click(item);
  await ui.flush();
}
const connRow = (ui) => ui.all('.qh-conntable .qh-revokerow .qh-revokeconfirm');

// ---------- 3: owner remove ----------

test('owner Remove opens the row, Keep owner closes it, Remove owner runs once', opts, async () => {
  const { ui, calls } = mountConns([conn('c1', 'orders-main')]);
  await pickMenu(ui, 0, 'Owners');
  const ownerBtn = () => ui.all('.qh-owner-row button').find(b => b.textContent === 'Remove');
  assert.ok(ownerBtn(), 'the owner list loaded');
  assert.equal(rows(ui).length, 0);
  ui.click(ownerBtn());
  assert.equal(rows(ui).length, 1);
  assert.match(rows(ui)[0].textContent, /Remove Team Alpha as an owner of orders-main\? QueryHub then updates the approvers of this connection\./);
  assert.deepEqual(calls.ownerRemoved, []);
  ui.click(ownerBtn());
  assert.equal(rows(ui).length, 0, 'a second click closes the row');
  ui.click(ownerBtn());
  ui.click(btn(ui, rows(ui)[0], 'Keep owner'));
  assert.equal(rows(ui).length, 0);
  assert.deepEqual(calls.ownerRemoved, []);
  ui.click(ownerBtn());
  ui.click(btn(ui, rows(ui)[0], 'Remove owner'));
  await ui.flush();
  assert.deepEqual(calls.ownerRemoved, [['c1', '7']]);
});

// ---------- 4: bulk disable ----------

test('bulk Disable opens the row with the count, Keep enabled closes it, Disable runs the bulk write', opts, async () => {
  const { ui, calls } = mountConns([conn('c1', 'orders-main'), conn('c2', 'billing-main')]);
  ui.click(ui.all('input[aria-label="Select orders-main"]')[0]);
  const bar = () => ui.all('.qh-connbulk-bar')[0];
  assert.ok(bar(), 'the bulk bar shows');
  ui.click(btn(ui, bar(), 'Disable'));
  assert.equal(rows(ui).length, 1);
  assert.match(rows(ui)[0].textContent, /Disable 1 connection\? Developers lose access to them until you enable them again\. Running queries continue\./);
  assert.deepEqual(calls.bulk, [], 'the first click calls nothing');
  ui.click(btn(ui, bar(), 'Disable'));
  assert.equal(rows(ui).length, 0, 'a second click closes the row');
  ui.click(btn(ui, bar(), 'Disable'));
  ui.click(btn(ui, rows(ui)[0], 'Keep enabled'));
  assert.equal(rows(ui).length, 0);
  assert.deepEqual(calls.bulk, []);
  ui.click(btn(ui, bar(), 'Disable'));
  ui.click(btn(ui, rows(ui)[0], 'Disable'));
  await ui.flush();
  await ui.flush();
  assert.equal(calls.bulk.length, 2, 'the dry run and the write');
  assert.equal(calls.bulk[0].dryRun, true);
  assert.equal(calls.bulk[1].dryRun, false);
  assert.equal(calls.bulk[1].enabled, false);
  assert.deepEqual(calls.bulk[1].connections, ['orders-main']);
});

// ---------- 5: disable one connection ----------

test('connection Disable opens the row, Keep enabled closes it, Disable runs once', opts, async () => {
  const { ui, calls } = mountConns([conn('c1', 'orders-main')]);
  await pickMenu(ui, 0, 'Disable');
  assert.equal(connRow(ui).length, 1);
  assert.match(connRow(ui)[0].textContent, /Disable “orders-main”\? Developers lose access to it until you enable it again\. Running queries continue\./);
  assert.deepEqual(calls.enabled, [], 'the first click calls nothing');
  await pickMenu(ui, 0, 'Disable');
  assert.equal(connRow(ui).length, 0, 'the same menu item again closes the row');
  await pickMenu(ui, 0, 'Disable');
  ui.click(btn(ui, connRow(ui)[0], 'Keep enabled'));
  assert.equal(connRow(ui).length, 0);
  assert.deepEqual(calls.enabled, []);
  await pickMenu(ui, 0, 'Disable');
  ui.click(btn(ui, connRow(ui)[0], 'Disable'));
  assert.deepEqual(calls.enabled, [['c1', false]]);
  assert.equal(connRow(ui).length, 0);
});

test('a replica Disable says out of rotation in the same row', opts, async () => {
  const list = [conn('c1', 'orders-main'), conn('c2', 'orders-r1', { replicaOf: 'orders-main' })];
  const { ui, calls } = mountConns(list);
  await pickMenu(ui, 1, 'Take out of rotation');
  assert.match(connRow(ui)[0].textContent, /Take “orders-r1” out of rotation\? Read-only queries on orders-main run on the primary until you enable it again\. Nobody loses access\./);
  assert.deepEqual(calls.enabled, []);
  ui.click(btn(ui, connRow(ui)[0], 'Disable'));
  assert.deepEqual(calls.enabled, [['c2', false]]);
});

// ---------- 6: delete one connection ----------

test('connection Delete opens the row, Keep closes it, Delete connection runs once', opts, async () => {
  const { ui, calls } = mountConns([conn('c1', 'orders-main')]);
  await pickMenu(ui, 0, 'Delete');
  assert.equal(connRow(ui).length, 1);
  assert.match(connRow(ui)[0].textContent, /Delete “orders-main”\? If any query history or live grants still refer to it, QueryHub disables it and does not delete it\./);
  assert.deepEqual(calls.removed, [], 'the first click calls nothing');
  await pickMenu(ui, 0, 'Delete');
  assert.equal(connRow(ui).length, 0, 'the same menu item again closes the row');
  await pickMenu(ui, 0, 'Delete');
  ui.click(btn(ui, connRow(ui)[0], 'Keep'));
  assert.equal(connRow(ui).length, 0);
  assert.deepEqual(calls.removed, []);
  await pickMenu(ui, 0, 'Delete');
  ui.click(btn(ui, connRow(ui)[0], 'Delete connection'));
  assert.deepEqual(calls.removed, ['c1']);
  assert.equal(connRow(ui).length, 0);
});

// ---------- source ----------

test('qh-admin-access.jsx has no window.confirm and exports InlineConfirm', () => {
  const src = web('qh-admin-access.jsx');
  assert.doesNotMatch(src, /window\.confirm/);
  assert.match(src, /Object\.assign\(window, \{ InlineConfirm,/);
});
