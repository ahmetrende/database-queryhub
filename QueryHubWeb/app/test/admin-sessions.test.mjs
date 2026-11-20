// Admin > Sessions (2026-11-20, DBeaver item P2.6).
//
//   * The pure rules: the connection list, the filter, the lock order, the
//     terminate statement and the running time.
//   * The API path of adminSessions.
//   * The nav: a super-admin opens the section, a DBA does not.
//   * Mounted (jsdom): four sessions, a blocked row, a blocker chip, a hidden
//     query, the filter, the lock order, and the terminate button on PostgreSQL only.
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

const pure = loadInto(bareWindow(), 'qh-data.jsx', 'qh-admin-sessions.jsx');

const NOTE = 'The query text of another role is hidden. The monitoring role cannot read it.';
const CONNS = [
  { id: 'pg-main', name: 'pg-main', engine: 'PostgreSQL', engineId: 'postgres', enabled: true, defaultDatabase: 'orders',
    databases: [{ id: 'orders', name: 'orders' }, { id: 'billing', name: 'billing' }] },
  { id: 'ms-main', name: 'ms-main', engine: 'SQL Server', engineId: 'mssql', enabled: true, defaultDatabase: 'sales',
    databases: [{ id: 'sales', name: 'sales' }] },
  { id: 'ch-main', name: 'ch-main', engine: 'ClickHouse', engineId: 'clickhouse', enabled: true, databases: [{ id: 'logs', name: 'logs' }] },
  { id: 'pg-main-r1', name: 'pg-main-r1', engine: 'PostgreSQL', engineId: 'postgres', enabled: true, replicaOf: 'pg-main', databases: [{ id: 'orders', name: 'orders' }] },
  { id: 'pg-old', name: 'pg-old', engine: 'PostgreSQL', engineId: 'postgres', enabled: false, databases: [{ id: 'x', name: 'x' }] },
];

function fixture(engine) {
  const at = Date.parse('2026-11-20T10:00:00Z');
  const ago = (ms) => new Date(at - ms).toISOString();
  return {
    engine, database: 'orders', fetchedAt: new Date(at).toISOString(),
    sessions: [
      { id: 4211, user: 'app_rw', app: 'orders-api', client: '10.0.4.21', state: 'active', wait: 'Lock: transactionid',
        started: ago(3600000), queryStarted: ago(125000), blockedBy: [4187], blocking: [], database: 'orders',
        query: "UPDATE orders SET status = 'paid' WHERE id = 81723;", queryHidden: false },
      { id: 4187, user: 'etl', app: 'nightly-load', client: '10.0.7.3', state: 'idle in transaction', wait: null,
        started: ago(7200000), queryStarted: ago(640000), blockedBy: [], blocking: [4211], database: 'orders',
        query: "UPDATE orders SET region = 'eu';", queryHidden: false },
      { id: 4302, user: 'reporting', app: 'metabase', client: '10.0.9.14', state: 'active', wait: 'IO: DataFileRead',
        started: ago(900000), queryStarted: ago(8000), blockedBy: [], blocking: [], database: 'orders',
        query: 'SELECT region, count(*) FROM orders GROUP BY region;', queryHidden: false },
      { id: 4350, user: 'rds_monitor', app: '', client: null, state: 'idle', wait: null,
        started: ago(86400000), queryStarted: null, blockedBy: [], blocking: [], database: 'orders', query: null, queryHidden: true },
    ],
    locks: [
      { id: 4187, type: 'relation', mode: 'RowExclusiveLock', granted: true, relation: 'public.orders' },
      { id: 4211, type: 'transactionid', mode: 'ShareLock', granted: false, relation: null },
      { id: 4187, type: 'transactionid', mode: 'ExclusiveLock', granted: true, relation: null },
    ],
    notes: [NOTE],
  };
}

// ---------- pure rules ----------

test('the picker lists enabled primaries on PostgreSQL and SQL Server only', () => {
  assert.deepEqual(pure.qhSessConns(CONNS).map(c => c.id), ['pg-main', 'ms-main']);
  assert.deepEqual(pure.qhSessConns(undefined), []);
  assert.equal(pure.qhSessEngine({ engine: 'Microsoft SQL Server 2019' }), 'mssql');
  assert.equal(pure.qhSessDefaultDb(CONNS[0]), 'orders');
  assert.equal(pure.qhSessDefaultDb({ defaultDatabase: 'gone', databases: [{ id: 'a' }] }), 'a');
});

test('the filter reads user, app and query, and never a hidden query', () => {
  const s = fixture('postgres').sessions;
  assert.deepEqual(pure.qhSessFilter(s, 'ETL').map(x => x.id), [4187]);
  assert.deepEqual(pure.qhSessFilter(s, 'orders').map(x => x.id), [4211, 4187, 4302]);
  assert.equal(pure.qhSessFilter(s, '  ').length, 4);
  assert.deepEqual(pure.qhSessFilter([{ id: 1, user: 'a', query: 'secret', queryHidden: true }], 'secret'), []);
});

test('locks that wait come first, the server order stays inside each group', () => {
  const sorted = pure.qhSessLocksSorted(fixture('postgres').locks);
  assert.deepEqual(sorted.map(l => [l.id, l.granted]), [[4211, false], [4187, true], [4187, true]]);
  assert.equal(sorted[1].mode, 'RowExclusiveLock');
});

test('the terminate statement takes a whole number only', () => {
  assert.equal(pure.qhSessTerminateSql(4211), 'SELECT pg_terminate_backend(4211);');
  assert.equal(pure.qhSessTerminateSql('4211'), 'SELECT pg_terminate_backend(4211);');
  assert.equal(pure.qhSessTerminateSql('1); DROP TABLE t; --'), null);
  assert.equal(pure.qhSessTerminateSql(0), null);
  assert.equal(pure.qhSessTerminateSql(null), null);
});

test('running time at the read', () => {
  const at = Date.parse('2026-11-20T10:00:00Z');
  const ago = (ms) => new Date(at - ms).toISOString();
  assert.equal(pure.qhSessRunning(null, at), '');
  assert.equal(pure.qhSessRunning(ago(400), at), '< 1 s');
  assert.equal(pure.qhSessRunning(ago(8000), at), '8 s');
  assert.equal(pure.qhSessRunning(ago(125000), at), '2 min 5 s');
  assert.equal(pure.qhSessRunning(ago(2 * 3600000 + 4 * 60000), at), '2 h 4 min');
});

// ---------- API path ----------

test('adminSessions calls GET /api/admin/connections/{conn}/sessions with the database', async () => {
  const win = bareWindow();
  const urls = [];
  win.fetch = async (url, o) => { urls.push([url, (o && o.method) || 'GET']); return { ok: true, status: 200, json: async () => ({}), text: async () => '{}' }; };
  const api = loadInto(win, 'qh-api.jsx').qhApi;
  await api.adminSessions('prod main', 'orders/x');
  await api.adminSessions('pg-main');
  assert.deepEqual(urls, [
    ['/api/admin/connections/prod%20main/sessions?database=orders%2Fx', 'GET'],
    ['/api/admin/connections/pg-main/sessions', 'GET'],
  ]);
});

// ---------- nav ----------

test('Sessions is a super-admin section', () => {
  const win = loadInto(bareWindow(), 'qh-data.jsx', 'qh-admin-data.jsx', 'qh-admin-effective.jsx', 'qh-admin.jsx');
  assert.equal(win.qhAdminCanOpen('sessions', { role: 'super', isDba: true }), true);
  assert.equal(win.qhAdminCanOpen('sessions', { role: 'dba', isDba: true }), false);
  assert.equal(win.qhAdminCanOpen('sessions', { role: 'admin' }), false);
  const src = web('qh-admin.jsx');
  assert.match(src, /QH_ADMIN_NEED = \{[^}]*sessions: 'super'/);
  assert.match(src, /QH_ADMIN_SECTIONS = \[[^\]]*'sessions'/);
  assert.match(src, /\['sessions', 'Sessions', AdminIcons\.sessions/);
  assert.match(src, /curNav === 'sessions' && <AdminSessions /);
});

// ---------- mounted ----------

function mount(props, api) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { pretendToBeVisual: true, url: 'https://localhost/#admin/sessions' });
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
  win.qhApi = api;
  loadInto(win, 'qh-data.jsx', 'qh-admin-sessions.jsx');
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(win.AdminSessions, props)));
  const doc = win.document;
  const all = (sel) => [...doc.querySelectorAll(sel)];
  const flush = () => React.act(() => new Promise(r => setTimeout(r, 0)));
  const click = (el) => React.act(() => el.dispatchEvent(new win.MouseEvent('click', { bubbles: true })));
  const pick = async (id) => {
    const sel = doc.querySelector('select[aria-label="Connection"]');
    sel.value = id;
    React.act(() => sel.dispatchEvent(new win.Event('change', { bubbles: true })));
    await flush();
  };
  const type = (sel, text) => {
    const el = doc.querySelector(sel);
    Object.getOwnPropertyDescriptor(win.HTMLInputElement.prototype, 'value').set.call(el, text);
    React.act(() => el.dispatchEvent(new win.Event('input', { bubbles: true })));
  };
  return { win, doc, all, flush, click, pick, type };
}

const apiFor = (calls, fail) => ({
  adminSessions: (conn, db) => {
    calls.push([conn, db]);
    if (fail) return Promise.reject(fail);
    return Promise.resolve(fixture(conn === 'ms-main' ? 'mssql' : 'postgres'));
  },
});

test('PostgreSQL: four rows, blocked tint, blocker chip, hidden query, lock order', opts, async () => {
  const calls = [], opened = [];
  const m = mount({ st: { connections: CONNS }, onOpenSql: (x) => opened.push(x) }, apiFor(calls));
  assert.deepEqual(m.all('select[aria-label="Connection"] option').map(o => o.value), ['', 'pg-main', 'ms-main']);
  assert.match(m.doc.body.textContent, /Pick a connection to see its sessions and locks\./);
  assert.equal(calls.length, 0, 'nothing loads before a pick');

  await m.pick('pg-main');
  assert.deepEqual(calls, [['pg-main', 'orders']]);
  assert.equal(m.all('tr.qh-asess-row').length, 4);
  assert.ok(m.doc.querySelector('tr[data-session="4211"]').classList.contains('is-blocked'));
  assert.ok(!m.doc.querySelector('tr[data-session="4187"]').classList.contains('is-blocked'));
  assert.equal(m.doc.querySelector('tr[data-session="4187"] .qh-asess-chip').textContent, 'Blocking 1');
  const hidden = m.doc.querySelector('tr[data-session="4350"] .qh-asess-hidden');
  assert.equal(hidden.textContent, 'hidden');
  assert.equal(hidden.getAttribute('title'), NOTE);
  assert.match(m.doc.querySelector('tr[data-session="4211"]').textContent, /2 min 5 s/);

  const locks = m.all('table.qh-asess-locks tbody tr');
  assert.equal(locks.length, 3);
  assert.equal(locks[0].getAttribute('data-lock-session'), '4211');
  assert.equal(locks[0].querySelector('.qh-asess-no').textContent, 'No');

  const terms = m.all('.qh-asess-term');
  assert.equal(terms.length, 4);
  assert.equal(terms[0].textContent, 'Terminate in editor');
  m.click(m.doc.querySelector('tr[data-session="4211"] .qh-asess-term'));
  assert.deepEqual(opened, [{ conn: 'pg-main', db: 'orders', sql: 'SELECT pg_terminate_backend(4211);' }]);

  m.type('input[aria-label="Filter sessions"]', 'etl');
  assert.deepEqual(m.all('tr.qh-asess-row').map(r => r.getAttribute('data-session')), ['4187']);
  m.type('input[aria-label="Filter sessions"]', 'zzz');
  assert.match(m.doc.body.textContent, /No session matches the filter\./);

  m.click(m.all('button').find(b => b.textContent === 'Refresh'));
  await m.flush();
  assert.equal(calls.length, 2, 'Refresh reads again');
});

test('SQL Server: no terminate button', opts, async () => {
  const m = mount({ st: { connections: CONNS }, onOpenSql: () => {} }, apiFor([]));
  await m.pick('ms-main');
  assert.equal(m.all('tr.qh-asess-row').length, 4);
  assert.equal(m.all('.qh-asess-term').length, 0);
  assert.doesNotMatch(m.doc.body.textContent, /Terminate in editor/);
});

test('without an editor hook the button copies the statement', opts, async () => {
  const m = mount({ st: { connections: CONNS } }, apiFor([]));
  await m.pick('pg-main');
  assert.equal(m.all('.qh-asess-term')[0].textContent, 'Copy terminate SQL');
});

test('a 400 says the connection has no session view', opts, async () => {
  const err = Object.assign(new Error('no'), { status: 400 });
  const m = mount({ st: { connections: CONNS } }, apiFor([], err));
  await m.pick('pg-main');
  assert.match(m.doc.querySelector('[role="alert"]').textContent, /This connection has no session view\./);
});
