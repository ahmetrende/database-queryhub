// A single click on a database in the tree uses it in a blank, unpicked tab
// (2026-10-23, asked after a portal user could not pick a database by clicking it).
//
//   * The rule is one pure function, `qhTabBindsOnClick`: a query tab, flagged
//     `unpicked`, with no SQL in it. Nothing else is rebound by a click.
//   * The tree calls `onPick` on a single click only while that rule holds. Once the
//     tab is picked, a click only opens or closes the row, so browsing the tree never
//     changes a tab's connection. A double-click opens a new query on the database.
//   * The row's tooltip says how to use and change a database. A blank tab says
//     it on its target strip.
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

const { qhTabBindsOnClick, QH_PICK_HINT } = loadInto(bareWindow(), 'qh-data.jsx');

// ---------- the rule ----------

test('only a blank, unpicked query tab binds on a click', () => {
  assert.equal(qhTabBindsOnClick({ unpicked: true, sql: '' }), true);
  assert.equal(qhTabBindsOnClick({ unpicked: true, sql: '  \n ' }), true, 'spaces are blank');
  assert.equal(qhTabBindsOnClick({ unpicked: true }), true, 'no sql field is blank');
  assert.equal(qhTabBindsOnClick({ unpicked: true, sql: 'SELECT 1' }), false, 'a tab with SQL keeps its target');
  assert.equal(qhTabBindsOnClick({ unpicked: false, sql: '' }), false, 'a picked tab keeps its target');
  assert.equal(qhTabBindsOnClick({ sql: '' }), false, 'a tab from a saved query or the tree was picked by intent');
  assert.equal(qhTabBindsOnClick({ unpicked: true, kind: 'welcome', sql: '' }), false, 'Welcome is not a query tab');
  assert.equal(qhTabBindsOnClick({ unpicked: true, kind: 'whatsnew', sql: '' }), false);
  assert.equal(qhTabBindsOnClick(null), false);
  assert.equal(qhTabBindsOnClick({ unpicked: 'yes', sql: '' }), false, 'only true counts');
});

test('the hint says how to use a database and how to change it', () => {
  assert.match(QH_PICK_HINT, /Click a database in the tree/);
  assert.match(QH_PICK_HINT, /double-click/);
  assert.match(QH_PICK_HINT, /right-click/);
  assert.ok(QH_PICK_HINT.split(/\s+/).length <= 25);
});

// ---------- the tab flag, in the app ----------

test('the app flags the first tab and a blank new tab as unpicked, and a pick clears the flag', () => {
  const app = web('qh-app.jsx');
  assert.match(app, /id: 't1', name: 'Query 1'[^}]*unpicked: true/, 'the first tab');
  assert.match(app, /const newTab = \(\) => \{[\s\S]*?unpicked: true[\s\S]*?\};/, 'a blank new tab');
  assert.match(app, /const pickDb = \(c, d\) => patch\(activeId, \{ conn: c\.id, db: d\.id, unpicked: false \}\)/, 'a pick');
  assert.match(app, /const bindOnClick = qhTabBindsOnClick\(tab\)/);
  assert.match(app, /bindOnClick=\{bindOnClick\}/, 'the sidebar gets the rule');
  assert.match(app, /pickHint=\{bindOnClick\}/, 'the target strip gets the rule');
  // Tabs made from a saved query, a history row, the tree or a session are picked by intent.
  assert.doesNotMatch(app, /freshTab\(\{ name: s\.name[^}]*unpicked/);
  assert.doesNotMatch(app, /freshTab\(\{ name: nm[^}]*unpicked/);
});

// ---------- the tree, mounted ----------

function mountSidebar(bindOnClick, onPick) {
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
  loadInto(win, 'qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx');
  const conns = [{ id: 'prod-main', name: 'prod-main', engine: 'PostgreSQL 15', env: 'production', host: 'prod-main.example.internal', port: 5432,
    databases: [{ id: 'orders', name: 'orders', tier: 'RO', tables: [] }, { id: 'billing', name: 'billing', tier: 'RO', tables: [] }] }];
  const noop = () => {};
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(win.Sidebar, {
    mode: 'conns', setMode: noop, conns, connsLoading: false, schemaCache: {}, onLoadSchema: noop, onLoadRoles: noop, rolesCache: {},
    active: { conn: 'prod-main', db: 'orders' }, onPick, bindOnClick, saved: [], onLoadSaved: noop, onDeleteSaved: noop,
    sessions: [], scheduled: [], history: [], onLoadHistory: noop, onNewQuery: noop, onNewTab: noop, onOpenTable: noop,
    onRequestEndpoint: noop, onRequestAuto: noop, onToast: noop, canRefresh: false, isSuper: false })));
  const click = (el) => React.act(() => el.dispatchEvent(new win.MouseEvent('click', { bubbles: true })));
  const dblclick = (el) => React.act(() => el.dispatchEvent(new win.MouseEvent('dblclick', { bubbles: true })));
  const rows = () => [...win.document.querySelectorAll('[data-node]')];
  return { win, doc: win.document, click, dblclick, rows, React };
}

function dbRow(ui, name) {
  const all = [...ui.doc.querySelectorAll('.qh-tr, [class*="qh-tr-row"], [data-node]')];
  const el = all.find(e => (e.getAttribute('data-node') || '').endsWith('/' + name));
  assert.ok(el, 'database row ' + name + ' exists; rows: ' + all.map(e => e.getAttribute('data-node')).join(', '));
  return el;
}

test('a click on a database row uses it in a blank, unpicked tab', opts, () => {
  const picks = [];
  const ui = mountSidebar(true, (c, d) => picks.push(c.id + '/' + d.id));
  ui.click(dbRow(ui, 'billing'));
  assert.deepEqual(picks, ['prod-main/billing']);
});

test('a click does not bind when the tab is picked or holds SQL', opts, () => {
  const picks = [];
  const ui = mountSidebar(false, (c, d) => picks.push(c.id + '/' + d.id));
  ui.click(dbRow(ui, 'billing'));
  ui.click(dbRow(ui, 'orders'));
  ui.click(dbRow(ui, 'billing'));
  assert.deepEqual(picks, [], 'browsing the tree changes nothing');
});

test('the database row tooltip says how to use and change it', opts, () => {
  const ui = mountSidebar(false, () => {});
  const row = dbRow(ui, 'billing');
  const tip = row.getAttribute('data-tip') || row.getAttribute('title') || row.innerHTML;
  assert.match(tip, /Click uses it in a blank tab/);
  assert.match(tip, /Double-click opens a new query/);
  assert.match(tip, /Right-click shows more/);
});
