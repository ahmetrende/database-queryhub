// A25 schema refresh (Devart medium batch, M5c).
//
//   * The catalog check: every five minutes while the page is visible, the app
//     asks for the stamp of the database in use and reloads only when the
//     signature changed (`qhStampChanged`). A hidden database is not checked.
//   * "Reload catalog" in the tree menus, for every role. It reads QueryHub's
//     copy again and never the target. The database item says "as of HH:MM".
//   * Databases hidden in this browser (qh.dbhide.v1) leave every tree view;
//     "N hidden databases · Show" lists them again, dimmed.
//   * The Format options label column is 136px and a label never wraps, so
//     "Between statements" stays on one line. macOS Chrome draws it 129px wide.
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
const APP = web('qh-app.jsx');

// ---------- the pure helpers in qh-app.jsx ----------

function appHelper(name, storage) {
  const m = APP.match(/const QH_SCHEMA_CHECK_MS[\s\S]*?\nfunction qhDbHideLoad\(\) \{[\s\S]*?\n\}\n/);
  assert.ok(m, 'the catalog-check helpers are not where this test expects them in qh-app.jsx');
  return new Function('localStorage', m[0] + '\nreturn ' + name + ';')(storage || {});
}

test('only a changed signature counts as a change', () => {
  const changed = appHelper('qhStampChanged');
  assert.equal(changed({ at: '2026-11-11T10:00:00Z', sig: '12:34' }, { snapshotAt: '2026-11-11T11:00:00Z', sig: '12:34' }), false,
    'the hourly snapshot moves the time, not the tables');
  assert.equal(changed({ sig: '12:34' }, { sig: '13:99' }), true);
  assert.equal(changed(null, { sig: '1' }), false, 'nothing held yet');
  assert.equal(changed({ sig: '1' }, null), false, 'no answer');
  assert.equal(changed({ sig: null }, { sig: '1' }), false, 'an old server sent no signature');
  assert.equal(changed({ sig: '1' }, { sig: '' }), false);
});

test('the check runs every five minutes', () => {
  assert.equal(appHelper('QH_SCHEMA_CHECK_MS'), 5 * 60 * 1000);
});

test('the hidden list reads only "conn/db" strings and survives bad storage', () => {
  const store = (v) => ({ getItem: (k) => (k === 'qh.dbhide.v1' ? v : null) });
  assert.deepEqual(appHelper('qhDbHideLoad', store('["srv-alpha/shop","x",3,null,"/y","srv-beta/app"]'))(),
    ['srv-alpha/shop', 'srv-beta/app']);
  assert.deepEqual(appHelper('qhDbHideLoad', store('{"a":1}'))(), []);
  assert.deepEqual(appHelper('qhDbHideLoad', store('not json'))(), []);
  assert.deepEqual(appHelper('qhDbHideLoad', store(null))(), []);
  assert.deepEqual(appHelper('qhDbHideLoad', { getItem: () => { throw new Error('blocked'); } })(), []);
});

// ---------- the wiring in qh-app.jsx ----------

test('the schema keeps the stamp it came with', () => {
  assert.match(APP, /stamp: \{ at: r\.snapshotAt \|\| null, sig: r\.sig \|\| null \}/);
});

test('the check asks for the stamp of the visible database in use, and skips a hidden one', () => {
  const eff = APP.match(/const lastStampCheck = useRef\(Date\.now\(\)\);[\s\S]*?\}, \[tab\.conn, tab\.db, hiddenDbs, loadSchema\]\);/);
  assert.ok(eff, 'the catalog check effect is not where this test expects it');
  const src = eff[0];
  assert.match(src, /document\.visibilityState !== 'visible'\) return;/);
  assert.match(src, /Date\.now\(\) - lastStampCheck\.current < QH_SCHEMA_CHECK_MS\) return;/);
  assert.match(src, /hiddenDbs\.includes\(key\)\) return;/);
  assert.match(src, /qhApi\.schemaStamp\(connId, dbId\)/);
  assert.match(src, /if \(qhStampChanged\(held\.stamp, r\)\) \{\s*loadSchema\(connId, dbId, true\);\s*return qhApi\.connections\(\)/);
  assert.match(src, /addEventListener\('visibilitychange', check\)/);
  assert.match(src, /removeEventListener\('visibilitychange', check\)/);
  assert.match(src, /clearInterval\(iv\)/);
  assert.doesNotMatch(src, /adminSchemaRefresh/, 'the check never reads the target');
});

test('Reload catalog reads QueryHub only, and the sidebar gets it with the hidden list', () => {
  const fn = APP.match(/const reloadCatalog = async \(connId, dbName\) => \{[\s\S]*?\n  \};/);
  assert.ok(fn, 'reloadCatalog is not where this test expects it');
  assert.doesNotMatch(fn[0], /adminSchemaRefresh/, 'no target read');
  assert.match(fn[0], /loadSchema\(connId, d\.id, true\)/);
  assert.match(fn[0], /qhApi\.connections\(\)/);
  assert.match(APP, /onReloadCatalog=\{reloadCatalog\} hiddenDbs=\{hiddenDbs\} onToggleHiddenDb=\{toggleHiddenDb\}/);
  assert.match(APP, /localStorage\.setItem\(QH_DBHIDE_KEY, JSON\.stringify\(hiddenDbs\)\)/);
});

test('sign-out forgets the hidden list, like the folders and favorites', () => {
  const out = APP.match(/const signOut = \(\) => \{[\s\S]*?\n  \};/);
  assert.ok(out, 'signOut is not where this test expects it');
  assert.match(out[0], /QH_CONNORG_KEY, QH_REASON_KEY,\s*QH_DBHIDE_KEY\]\.forEach\(\s*\(k\) => localStorage\.removeItem\(k\)\)/);
  assert.match(out[0], /setHiddenDbs\(\[\]\);/);
});

test('the API client and the mock both know the stamp route', () => {
  assert.match(web('qh-api.jsx'), /schemaStamp: \(conn, dbn\) => qhFetch\('\/connections\/' \+ encodeURIComponent\(conn\) \+\s*'\/databases\/' \+ encodeURIComponent\(dbn\) \+ '\/schema\/stamp'\)/);
  assert.match(web('qh-api-mock.jsx'), /schemaStamp: \(conn, dbn\) => mockDelay\(/);
});

test('the Format options label column fits "Between statements" on one line', () => {
  assert.match(web('QueryHub.html'), /\.qh-fmt-row \{ display: grid; grid-template-columns: 136px minmax\(0, 1fr\);/);
  assert.match(web('QueryHub.html'), /\.qh-fmt-row > span \{ white-space: nowrap; \}/);
  const css = readFileSync(resolve(HERE, '..', 'src', 'index.css'), 'utf8');
  assert.match(css, /grid-template-columns: 136px minmax\(0, 1fr\)/, 'the generated index.css is committed with the change');
  assert.match(css, /\.qh-fmt-row > span \{ white-space: nowrap; \}/, 'the generated index.css is committed with the change');
});

// ---------- the tree, mounted ----------

const CONNS = [{ id: 'srv-alpha', name: 'srv-alpha', engine: 'PostgreSQL 16', env: 'production', host: 'srv-alpha.example.internal', port: 5432,
  databases: [{ id: 'shop', name: 'shop', tier: 'RO', tables: [] }, { id: 'billing', name: 'billing', tier: 'RO', tables: [] },
              { id: 'audit', name: 'audit', tier: 'RO', tables: [] }] }];

function mount({ hidden = [], view = 'servers', stampAt = null } = {}) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { pretendToBeVisual: true, url: 'https://localhost/' });
  for (const k of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node', 'Event', 'KeyboardEvent', 'MouseEvent',
                   'getComputedStyle', 'requestAnimationFrame', 'cancelAnimationFrame', 'localStorage', 'location', 'history']) {
    Object.defineProperty(globalThis, k, { value: dom.window[k], configurable: true, writable: true });
  }
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const React = require_('react');
  const ReactDOM = require_('react-dom/client');
  globalThis.React = React;
  const win = dom.window;
  win.React = React;
  win.ReactDOM = require_('react-dom');
  win.localStorage.setItem('qh.treeview.v1', view);
  loadInto(win, 'qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx');
  const calls = { hide: [], reload: [], refresh: [] };
  const noop = () => {};
  const schemaCache = stampAt ? { 'srv-alpha/shop': { tables: {}, views: [], stamp: { at: stampAt, sig: '3:9' } } } : {};
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  const render = (hiddenDbs) => React.act(() => root.render(React.createElement(win.Sidebar, {
    mode: 'conns', setMode: noop, conns: CONNS, connsLoading: false, schemaCache, onLoadSchema: noop, onLoadRoles: noop, rolesCache: {},
    active: { conn: 'srv-alpha', db: 'shop' }, onPick: noop, bindOnClick: false, saved: [], onLoadSaved: noop, onDeleteSaved: noop,
    sessions: [], scheduled: [], history: [], onLoadHistory: noop, onNewQuery: noop, onNewTab: noop, onOpenTable: noop,
    onRequestEndpoint: noop, onRequestAuto: noop, onToast: noop, canRefresh: false, isSuper: false,
    onRefreshSchema: (c, d) => calls.refresh.push([c, d]),
    onReloadCatalog: (c, d) => calls.reload.push([c, d]),
    hiddenDbs, onToggleHiddenDb: (k) => calls.hide.push(k) })));
  render(hidden);
  const doc = win.document;
  const click = (el) => React.act(() => el.dispatchEvent(new win.MouseEvent('click', { bubbles: true })));
  const ctx = (el) => React.act(() => el.dispatchEvent(new win.MouseEvent('contextmenu', { bubbles: true, clientX: 20, clientY: 20 })));
  const dbNames = () => [...doc.querySelectorAll('[data-node]')].map(e => e.getAttribute('data-node'))
    .filter(n => /^srv-alpha\/[a-z]+$/.test(n)).map(n => n.split('/')[1]);
  const row = (name) => doc.querySelector('[data-node="srv-alpha/' + name + '"]');
  const button = (re) => [...doc.querySelectorAll('button')].find(b => re.test(b.textContent));
  const line = () => doc.querySelector('.qh-dbhide-line');
  const server = () => [...doc.querySelectorAll('.qh-tr')].find(e => !e.getAttribute('data-node') && e.textContent.includes('srv-alpha'));
  // Server rows start closed (only the seed's own server opens by default).
  if (view === 'servers') click(server());
  return { win, doc, click, ctx, dbNames, row, button, line, server, calls, render };
}

test('a hidden database leaves the server view until Show lists it, dimmed', opts, () => {
  const ui = mount({ hidden: ['srv-alpha/billing'] });
  assert.deepEqual(ui.dbNames().sort(), ['audit', 'shop']);
  assert.equal(ui.line().textContent.replace(/\s+/g, ' ').trim(), '1 hidden database · Show');
  ui.click(ui.button(/^Show$/));
  assert.deepEqual(ui.dbNames().sort(), ['audit', 'billing', 'shop']);
  assert.equal(ui.row('billing').classList.contains('is-muted'), true, 'a hidden database shows dimmed');
  assert.equal(ui.row('shop').classList.contains('is-muted'), false);
  ui.click(ui.button(/^Hide again$/));
  assert.deepEqual(ui.dbNames().sort(), ['audit', 'shop']);
});

test('a hidden database leaves the database view too, and the count says so', opts, () => {
  const ui = mount({ hidden: ['srv-alpha/billing', 'srv-alpha/audit'], view: 'dbs' });
  assert.deepEqual(ui.dbNames(), ['shop']);
  assert.match(ui.doc.querySelector('.qh-treeview-n').textContent, /^1 databases?$/);
  assert.equal(ui.line().textContent.replace(/\s+/g, ' ').trim(), '2 hidden databases · Show');
});

test('no hidden database, no line; a key for a database that is gone does not count', opts, () => {
  assert.equal(mount().line(), null);
  assert.equal(mount({ hidden: ['srv-gone/shop', 'srv-alpha/nope'] }).line(), null);
});

test('the database menu hides it and reloads its catalog, with the time it is from', opts, () => {
  const at = new Date(); at.setHours(9, 5, 0, 0);
  const ui = mount({ stampAt: at.toISOString() });
  ui.ctx(ui.row('shop'));
  const reload = ui.button(/^Reload catalog/);
  assert.ok(reload, 'Reload catalog is in the menu for a role without Refresh schema');
  assert.equal(reload.textContent.trim(), 'Reload catalog · as of 09:05');
  assert.equal(ui.button(/^Refresh schema/), undefined, 'Refresh schema stays an admin item');
  ui.click(reload);
  assert.deepEqual(ui.calls.reload, [['srv-alpha', 'shop']]);
  assert.deepEqual(ui.calls.refresh, [], 'the target is not asked');
  ui.ctx(ui.row('shop'));
  ui.click(ui.button(/^Hide this database$/));
  assert.deepEqual(ui.calls.hide, ['srv-alpha/shop']);
});

test('a catalog from another day says the date', opts, () => {
  const ui = mount({ stampAt: '2026-11-09T10:15:00Z' });
  ui.ctx(ui.row('shop'));
  assert.match(ui.button(/^Reload catalog/).textContent.trim(), /^Reload catalog · as of 9 Nov \d\d:\d\d$/);
});

test('a database that has no stamp yet offers the reload without a time', opts, () => {
  const ui = mount();
  ui.ctx(ui.row('billing'));
  assert.equal(ui.button(/^Reload catalog/).textContent.trim(), 'Reload catalog');
});

test('a shown hidden database offers to show it again', opts, () => {
  const ui = mount({ hidden: ['srv-alpha/billing'] });
  ui.click(ui.button(/^Show$/));
  ui.ctx(ui.row('billing'));
  ui.click(ui.button(/^Show this database again$/));
  assert.deepEqual(ui.calls.hide, ['srv-alpha/billing']);
});

test('the server menu reloads the catalog of the server', opts, () => {
  const ui = mount();
  assert.ok(ui.server(), 'the server row exists');
  ui.ctx(ui.server());
  ui.click(ui.button(/^Reload catalog$/));
  assert.deepEqual(ui.calls.reload, [['srv-alpha', null]]);
});
