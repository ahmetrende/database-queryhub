// The CREATE script of one object (2026-11-12, Devart medium batch, PR M10a = B5).
//
// The server reads it live (GET .../ddl) and writes an audit row. The tree's
// table menu has "Script as CREATE". In the editor, Shift+Alt+D asks for the
// name at the caret (qhObjectNameAt). The script opens in a new tab.
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
const at = (sql, word, off = 0, eng = 'postgres') => W.qhObjectNameAt(sql, sql.indexOf(word) + off, eng);

test('the dotted name at the caret, as typed', () => {
  const sql = 'select * from sales.orders o join "My T" t on t.id = o.id where twice(o.x) > 1';
  assert.equal(at(sql, 'orders'), 'sales.orders');
  assert.equal(at(sql, 'sales'), 'sales.orders', 'the caret on the schema part');
  assert.equal(at(sql, 'orders', 6), 'sales.orders', 'the caret at the end of the name');
  assert.equal(at(sql, '"My T"', 2), '"My T"');
  assert.equal(at(sql, 'twice'), 'twice', 'a function name, without its arguments');
  assert.equal(at(sql, 'select'), null, 'a keyword');
  assert.equal(at(sql, 'join'), null);
  assert.equal(W.qhObjectNameAt("select 'sales.orders'", 10, 'postgres'), null, 'inside a string');
  assert.equal(W.qhObjectNameAt('select [dbo].[Order Lines]', 12, 'mssql'), '[dbo].[Order Lines]');
});

function mount(initial, extra = {}) {
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
  const ctl = { notices: [], scripts: [] };
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(win.SqlEditor, {
    value: initial, docId: 't1', fontSize: 13, schema: { tables: [] }, engineId: 'postgres', onChange: () => {},
    onNotice: (m) => ctl.notices.push(m), onScriptCreate: (n) => ctl.scripts.push(n), ...extra })));
  const ta = win.document.querySelector('textarea');
  return {
    ctl,
    press(pos) {
      ta.focus(); ta.setSelectionRange(pos, pos);
      const ev = new win.KeyboardEvent('keydown', { key: 'Î', code: 'KeyD', shiftKey: true, altKey: true, bubbles: true, cancelable: true });
      React.act(() => ta.dispatchEvent(ev));
      return ev;
    },
  };
}
const dom = JSDOM ? {} : { skip: 'jsdom not installed' };

test('Shift+Alt+D asks for the script of the name at the caret', dom, () => {
  const ed = mount('select * from sales.orders');
  const ev = ed.press(20);
  assert.equal(ev.defaultPrevented, true);
  assert.deepEqual(ed.ctl.scripts, ['sales.orders']);
  ed.press(2);
  assert.deepEqual(ed.ctl.scripts, ['sales.orders'], 'a keyword asks for nothing');
  assert.deepEqual(ed.ctl.notices, ['Put the caret on a table, view, function or procedure name.']);
});

test('the tree menu, the app and the API are wired', () => {
  const panels = web('qh-panels.jsx');
  assert.match(panels, /onScriptCreate\(menu\.c, menu\.db, qhQualify\(menu\.c, menu\.db, menu\.table\)\)/);
  assert.match(panels, /Script as CREATE/);
  assert.match(panels, /<SchemaTree onScriptCreate=\{onScriptCreate\}/);
  const app = web('qh-app.jsx');
  assert.match(app, /onOpenTable=\{openTable\} onScriptCreate=\{scriptCreate\}/);
  assert.match(app, /onNotice=\{pushToast\} onScriptCreate=\{scriptCreateHere\}[ />]/);
  assert.match(app, /qhApi\.objectDdl\(c\.id, db\.id, name\)/);
  assert.match(app, /built from the catalog\. It has no grants, comments or storage options\./);
  assert.match(app, /as the server stores it\./);
  assert.match(web('qh-api.jsx'), /'\/ddl\?name=' \+ encodeURIComponent\(name\)/);
  assert.match(web('qh-api-mock.jsx'), /\n  objectDdl: \(conn, dbn, name\) =>/);
  assert.match(web('qh-editor.jsx'), /\['⇧ \+ ⌥ \/ Alt \+ D', 'CREATE script of the object at the caret'\]/);
});
