// Go to definition (2026-11-12, Devart medium batch, PR M10b = G1).
//
// Cmd/Ctrl+B, or Cmd/Ctrl+click, on a name: an alias goes to its definition
// after FROM or JOIN, a CTE name goes to its name after WITH (both select the
// name in the text), any other name shows its table or view row in the tree
// (qhDefinitionAt, qhTreeObject, qhTreeTableId). F12 belongs to the browser.
//
// jsdom rule: compare booleans or text, never pass a DOM node to assert.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { bareWindow, loadInto } from './_load.mjs';
import { web } from './_mount.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');
const SQL = 'with recent as (select id from orders) select o.id, r.id from sales.orders o join recent r on r.id = o.id';
const def = (word, off = 0) => W.qhDefinitionAt(SQL, SQL.indexOf(word) + off, 'postgres');
const slice = (d) => SQL.slice(d.start, d.end);

test('an alias goes to its definition, a CTE to its name', () => {
  const a = def('o.id');
  assert.equal(a.kind, 'text');
  assert.equal(a.start, SQL.indexOf(' o join') + 1, 'the o after sales.orders');
  assert.equal(slice(a), 'o');
  const r = def('recent r', 1);
  assert.equal(r.kind, 'text');
  assert.equal(r.start, SQL.indexOf('recent'), 'the CTE name after WITH');
  assert.equal(slice(def('r.id')), 'r', 'the alias r of the CTE');
  const col = 'with recent as (select 1 as x) select o.recent from orders o';
  assert.deepEqual({ ...W.qhDefinitionAt(col, col.indexOf('recent from') + 1, 'postgres') }, { kind: 'object', name: 'o.recent' },
    'a column after o. with the name of a CTE is no CTE');
});

test('any other name is an object; a keyword is nothing; an alias defined twice says so', () => {
  assert.deepEqual({ ...def('orders o') }, { kind: 'object', name: 'sales.orders' });
  assert.deepEqual({ ...def('orders)') }, { kind: 'object', name: 'orders' });
  assert.equal(def('select o.id'), null);
  const twice = 'select o.id from orders o where exists (select 1 from users o)';
  assert.equal(W.qhDefinitionAt(twice, 7, 'postgres').kind, 'none');
  assert.match(W.qhDefinitionAt(twice, 7, 'postgres').message, /defined twice/);
  assert.deepEqual({ ...W.qhDefinitionAt('select x.id from t', 8, 'postgres') }, { kind: 'object', name: 'x.id' },
    'a qualifier with no alias is a name');
});

// ---------- the tree node ----------

const C = { id: 'srv-alpha', engine: 'postgres' };
const DB = { id: 'shop', name: 'shop', tableRefs: [{ s: 'sales', n: 'orders' }, { s: 'public', n: 'orders' },
  { s: 'public', n: 'users' }, { s: 'public', n: 'big_orders' }] };
const SCH = { views: ['big_orders'], tables: {} };

test('the tree node of a typed name', () => {
  assert.deepEqual({ ...W.qhTreeObject(C, DB, SCH, 'users') }, { schema: 'public', table: 'users', view: false });
  assert.deepEqual({ ...W.qhTreeObject(C, DB, SCH, 'Users') }, { schema: 'public', table: 'users', view: false }, 'any case');
  assert.deepEqual({ ...W.qhTreeObject(C, DB, SCH, 'sales.orders') }, { schema: 'sales', table: 'orders', view: false });
  assert.deepEqual({ ...W.qhTreeObject(C, DB, SCH, '"public"."big_orders"') }, { schema: 'public', table: 'big_orders', view: true });
  assert.deepEqual({ ...W.qhTreeObject(C, DB, SCH, 'shop.public.users') }, { schema: 'public', table: 'users', view: false },
    'the name of this database');
  assert.equal(W.qhTreeObject(C, DB, SCH, 'orders'), null, 'two schemas have orders: no guess');
  assert.equal(W.qhTreeObject(C, DB, SCH, 'nothing'), null);
  assert.equal(W.qhTreeObject(C, DB, SCH, 'other.public.users'), null);
  assert.equal(W.qhTreeObject(C, null, SCH, 'users'), null);
});

test('the tree id is the one a search pick reveals', () => {
  const P = loadInto(bareWindow(), 'qh-data.jsx', 'qh-modal.jsx', 'qh-editor.jsx', 'qh-panels.jsx');
  assert.equal(P.qhTreeTableId('srv-alpha', 'shop', '/v', 'public', 'big_orders'), 'srv-alpha/shop/v/public.big_orders');
  const panels = web('qh-panels.jsx');
  assert.match(panels, /tid: qhTreeTableId\(c\.id, db\.id, branch, tsch, r\.n\)/);
  assert.match(panels, /setReveal\(\{ id: qhTreeTableId\(sid, r\.dbId, branch, r\.schema, r\.table\), n: r\.n \}\);/);
  assert.match(panels, /\}, \[revealTable && revealTable\.n\]\);/);
  const app = web('qh-app.jsx');
  assert.match(app, /onRevealObject=\{revealObjectHere\} \/>/);
  assert.match(app, /revealTable=\{treeReveal\}/);
  assert.match(app, /qhTreeObject\(c, db, schemaCache\[tab\.conn \+ '\/' \+ tab\.db\], name\)/);
  assert.match(app, /setSideMode\('conns'\);\n\s+setTreeReveal\(\{ connId: c\.id, dbId: db\.id, \.\.\.hit, n: Date\.now\(\) \}\);/);
  assert.match(web('qh-editor.jsx'), /\['⌘ \/ Ctrl \+ B · ⌘ \/ Ctrl \+ click', 'Go to the definition: alias, CTE, or the object in the tree'\]/);
});

// ---------- the editor ----------

function mount(initial) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { pretendToBeVisual: true, url: 'https://localhost/' });
  for (const k of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node', 'Event',
                   'KeyboardEvent', 'InputEvent', 'getComputedStyle', 'requestAnimationFrame',
                   'cancelAnimationFrame', 'localStorage']) {
    Object.defineProperty(globalThis, k, { value: dom.window[k], configurable: true, writable: true });
  }
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const React = require_('react');
  const ReactDOM = require_('react-dom/client');
  globalThis.React = React;
  const win = dom.window;
  win.React = React;
  win.ReactDOM = require_('react-dom');
  loadInto(win, 'qh-data.jsx', 'qh-editor.jsx');
  const ctl = { notices: [], reveals: [] };
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(win.SqlEditor, {
    value: initial, docId: 't1', fontSize: 13, schema: { tables: [] }, engineId: 'postgres', onChange: () => {},
    onNotice: (m) => ctl.notices.push(m), onRevealObject: (n) => ctl.reveals.push(n) })));
  const ta = win.document.querySelector('textarea');
  return {
    ctl,
    sel() { return [ta.selectionStart, ta.selectionEnd]; },
    key(pos, mods = { ctrlKey: true }) {
      ta.focus(); ta.setSelectionRange(pos, pos);
      const ev = new win.KeyboardEvent('keydown', { key: 'b', code: 'KeyB', bubbles: true, cancelable: true, ...mods });
      React.act(() => ta.dispatchEvent(ev));
      return ev;
    },
    click(pos, mods = { metaKey: true }) {
      ta.focus(); ta.setSelectionRange(pos, pos);
      React.act(() => ta.dispatchEvent(new win.MouseEvent('click', { bubbles: true, ...mods })));
    },
  };
}
const dom = JSDOM ? {} : { skip: 'jsdom not installed' };

test('Ctrl+B selects the alias definition; Cmd+click the CTE name', dom, () => {
  const ed = mount(SQL);
  const ev = ed.key(SQL.indexOf('o.id'));
  assert.equal(ev.defaultPrevented, true);
  const at = SQL.indexOf(' o join') + 1;
  assert.deepEqual(ed.sel(), [at, at + 1]);
  ed.click(SQL.indexOf('recent r') + 2);
  assert.deepEqual(ed.sel(), [SQL.indexOf('recent'), SQL.indexOf('recent') + 6]);
});

test('an object name goes to the tree; a keyword gets a notice; a plain click does nothing', dom, () => {
  const ed = mount(SQL);
  ed.key(SQL.indexOf('orders o') + 2);
  assert.deepEqual(ed.ctl.reveals, ['sales.orders']);
  ed.click(SQL.indexOf('orders)'), { ctrlKey: true });
  assert.deepEqual(ed.ctl.reveals, ['sales.orders', 'orders']);
  ed.key(SQL.indexOf('select o') + 1);
  assert.deepEqual(ed.ctl.notices, ['Put the caret on an alias, a CTE or an object name.']);
  ed.click(SQL.indexOf('orders)'), {});
  assert.equal(ed.ctl.reveals.length, 2, 'no modifier: a normal click');
  ed.key(SQL.indexOf('orders)'), { ctrlKey: true, shiftKey: true });
  assert.equal(ed.ctl.reveals.length, 2, 'Ctrl+Shift+B is not go to definition');
});
