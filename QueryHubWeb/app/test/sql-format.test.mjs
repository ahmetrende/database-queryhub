// Format SQL: the shared function qhFormatSql, and the Shift+Alt+F shortcut in
// the real SqlEditor (jsdom). The formatter is the real `sql-formatter` package,
// attached the way the built app attaches it.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { format } from 'sql-formatter';

import { bareWindow, loadInto } from './_load.mjs';
import { web } from './_mount.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }

function fmtWindow(formatter) {
  const win = bareWindow();
  if (formatter !== undefined) win.__qhSqlFormatter = formatter;
  return loadInto(win, 'qh-sqlformat.jsx');
}
const real = () => fmtWindow({ format });

test('postgres: each clause on its own line, casts and quotes kept', () => {
  const out = real().qhFormatSql(
    "select a::int, b from t where x = 'it''s' and y in (1,2) order by 1 limit 10", 'postgres');
  const lines = out.split('\n');
  assert.equal(lines[0], 'select');
  assert.equal(lines[1], '  a::int,');
  assert.ok(lines.includes('from'));
  assert.ok(lines.includes('where'));
  assert.ok(lines.length >= 10, 'one clause or column per line');
  assert.ok(out.includes('a::int'));
  assert.ok(out.includes("'it''s'"));
  assert.ok(!out.endsWith('\n'));
});

test('mssql: [brackets] and N-strings survive', () => {
  const out = real().qhFormatSql("select [a], N'z' from [dbo].[t]", 'mssql');
  assert.ok(out.includes('[a]'), out);
  assert.ok(out.includes("N'z'"), out);
  assert.ok(out.includes('[dbo].[t]'), out);
  assert.ok(out.split('\n').length > 3);
});

test('formatting twice gives what formatting once gives', () => {
  const w = real();
  const once = w.qhFormatSql('select a, b from t where x = 1 and y = 2', 'postgres');
  assert.equal(w.qhFormatSql(once, 'postgres'), once);
});

test('comments survive', () => {
  const out = real().qhFormatSql('select 1 -- x\n, 2 /* y */ from t', 'postgres');
  assert.ok(out.includes('-- x'), out);
  assert.ok(out.includes('/* y */'), out);
});

test('engine ids map to a language; an unknown one is the generic language', () => {
  const seen = [];
  const w = fmtWindow({ format: (q, o) => { seen.push(o); return q; } });
  for (const id of ['postgres', 'mssql', 'clickhouse', 'athena', 'mysql', undefined]) w.qhFormatSql('select 1', id);
  assert.deepEqual(seen.map((o) => o.language),
    ['postgresql', 'transactsql', 'clickhouse', 'trino', 'sql', 'sql']);
  assert.deepEqual(seen[0], { language: 'postgresql', tabWidth: 2, keywordCase: 'preserve' });
  assert.equal(real().qhFormatSql('select a from t', 'mysql').split('\n')[0], 'select');
});

test('one trailing newline from the library is dropped', () => {
  const w = fmtWindow({ format: () => 'select\n  1\n' });
  assert.equal(w.qhFormatSql('select 1', 'postgres'), 'select\n  1');
});

test('empty and whitespace-only input come back unchanged', () => {
  const w = real();
  assert.equal(w.qhFormatSql('', 'postgres'), '');
  assert.equal(w.qhFormatSql('  \n ', 'postgres'), '  \n ');
});

test('a throwing formatter gives null and does not throw', () => {
  const w = fmtWindow({ format: () => { throw new Error('bad input'); } });
  assert.equal(w.qhFormatSql('select 1', 'postgres'), null);
});

test('a missing formatter gives null', () => {
  assert.equal(fmtWindow().qhFormatSql('select 1', 'postgres'), null);
});

// ---------------------------------------------------------------------------
// The shortcut in the real editor
// ---------------------------------------------------------------------------

function mount(text, formatter) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>',
                        { pretendToBeVisual: true, url: 'https://localhost/' });
  for (const k of ['window', 'document', 'navigator', 'HTMLElement', 'Element',
                   'Node', 'Event', 'KeyboardEvent', 'InputEvent', 'getComputedStyle',
                   'requestAnimationFrame', 'cancelAnimationFrame', 'localStorage']) {
    Object.defineProperty(globalThis, k, { value: dom.window[k], configurable: true, writable: true });
  }
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const React = require_('react');
  const ReactDOM = require_('react-dom/client');
  const act = React.act;
  globalThis.React = React;
  const win = dom.window;
  win.React = React;
  win.ReactDOM = require_('react-dom');
  if (formatter) win.__qhSqlFormatter = formatter;
  loadInto(win, 'qh-data.jsx', 'qh-editor.jsx', 'qh-sqlformat.jsx');
  const ctl = {};
  function Host() {
    const [t, setT] = React.useState(text);
    const [sig, setSig] = React.useState(0);
    ctl.text = t;
    ctl.setSig = setSig;
    return React.createElement(win.SqlEditor, {
      value: t, docId: 'f1', fontSize: 13, engineId: 'postgres', formatSignal: sig,
      schema: { tables: [], columns: [], dbs: [], tableCols: {} },
      onChange: setT,
    });
  }
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  act(() => root.render(React.createElement(Host)));
  const ta = win.document.querySelector('textarea');
  const press = (key, mods = {}) => {
    const ev = new win.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...mods });
    act(() => ta.dispatchEvent(ev));
    return ev;
  };
  return {
    ta, ctl,
    get text() { return ta.value; },
    get sel() { return [ta.selectionStart, ta.selectionEnd]; },
    caret(i, j = i) { ta.focus(); ta.setSelectionRange(i, j); },
    format() { return press('Ø', { code: 'KeyF', shiftKey: true, altKey: true }); },
    clickFormat() { act(() => ctl.setSig(n => n + 1)); },
    undo() { return press('z', { metaKey: true }); },
    redo() { return press('z', { metaKey: true, shiftKey: true }); },
  };
}

const dom = JSDOM ? {} : { skip: 'jsdom not installed' };

test('Shift+Alt+F with a selection formats only the selection; Undo is one step', dom, () => {
  const ed = mount('x\nselect a from t\ny', { format });
  ed.caret(2, 17);
  assert.equal(ed.text.slice(2, 17), 'select a from t');
  assert.ok(ed.format().defaultPrevented);
  assert.equal(ed.text, 'x\nselect\n  a\nfrom\n  t\ny');
  assert.equal(ed.ctl.text, ed.text, 'the parent holds what the editor shows');
  assert.deepEqual(ed.sel, [2 + 'select\n  a\nfrom\n  t'.length, 2 + 'select\n  a\nfrom\n  t'.length]);
  ed.undo();
  assert.equal(ed.text, 'x\nselect a from t\ny');
  // Only an edit that went through commit() knows the selection it replaced.
  assert.deepEqual(ed.sel, [2, 17], 'Undo puts the selection back');
  ed.undo();
  assert.equal(ed.text, 'x\nselect a from t\ny', 'the format was a single step');
  ed.redo();
  assert.equal(ed.text, 'x\nselect\n  a\nfrom\n  t\ny');
});

test('Shift+Alt+F without a selection formats the whole text', dom, () => {
  const ed = mount('select a from t', { format });
  ed.caret(3);
  ed.format();
  assert.equal(ed.text, 'select\n  a\nfrom\n  t');
  assert.deepEqual(ed.sel, [ed.text.length, ed.text.length]);
  ed.undo();
  assert.equal(ed.text, 'select a from t');
  assert.deepEqual(ed.sel, [3, 3], 'Undo puts the caret back');
});

test('a failing or missing formatter changes nothing and leaves no step', dom, () => {
  const bad = mount('select a from t', { format: () => { throw new Error('x'); } });
  bad.format();
  assert.equal(bad.text, 'select a from t');
  bad.undo();
  assert.equal(bad.text, 'select a from t');
  const none = mount('select a from t');
  none.format();
  assert.equal(none.text, 'select a from t');
});

test('text that is already formatted adds no history entry', dom, () => {
  const ed = mount('select\n  a\nfrom\n  t', { format });
  ed.format();
  assert.equal(ed.text, 'select\n  a\nfrom\n  t');
  ed.undo();
  assert.equal(ed.text, 'select\n  a\nfrom\n  t');
});

// ---------------------------------------------------------------------------
// The Format button in the tab bar: it bumps a signal, the editor formats
// ---------------------------------------------------------------------------

test('Format button: with a selection it formats only the selection; Undo is one step and puts the selection back', dom, () => {
  const ed = mount('x\nselect a from t\ny', { format });
  ed.caret(2, 17);
  ed.clickFormat();
  assert.equal(ed.text, 'x\nselect\n  a\nfrom\n  t\ny');
  assert.equal(ed.ctl.text, ed.text, 'the parent holds what the editor shows');
  ed.undo();
  assert.equal(ed.text, 'x\nselect a from t\ny');
  assert.deepEqual(ed.sel, [2, 17], 'Undo puts the selection back');
  ed.undo();
  assert.equal(ed.text, 'x\nselect a from t\ny', 'the format was a single step');
});

test('Format button: without a selection it formats the whole text', dom, () => {
  const ed = mount('select a from t', { format });
  ed.caret(3);
  ed.clickFormat();
  assert.equal(ed.text, 'select\n  a\nfrom\n  t');
  ed.undo();
  assert.equal(ed.text, 'select a from t');
  assert.deepEqual(ed.sel, [3, 3], 'Undo puts the caret back');
});

test('Format button: nothing happens at mount, and a second press on formatted text adds no step', dom, () => {
  const ed = mount('select a from t', { format });
  assert.equal(ed.text, 'select a from t', 'mounting does not format');
  ed.clickFormat();
  ed.clickFormat();
  assert.equal(ed.text, 'select\n  a\nfrom\n  t');
  ed.undo();
  assert.equal(ed.text, 'select a from t', 'one Undo undoes the only format step');
});

test('Format button: with no formatter in the build it changes nothing', dom, () => {
  const ed = mount('select a from t', null);
  ed.caret(0, 6);
  ed.clickFormat();
  assert.equal(ed.text, 'select a from t');
});

function mountTabs(extra, formatter) {
  const dom2 = new JSDOM('<!doctype html><div id="root"></div>',
                         { pretendToBeVisual: true, url: 'https://localhost/' });
  for (const k of ['window', 'document', 'navigator', 'HTMLElement', 'Element',
                   'Node', 'Event', 'KeyboardEvent', 'MouseEvent', 'InputEvent', 'getComputedStyle',
                   'requestAnimationFrame', 'cancelAnimationFrame', 'localStorage']) {
    Object.defineProperty(globalThis, k, { value: dom2.window[k], configurable: true, writable: true });
  }
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const React = require_('react');
  const ReactDOM = require_('react-dom/client');
  const act = React.act;
  globalThis.React = React;
  const win = dom2.window;
  win.React = React;
  win.ReactDOM = require_('react-dom');
  if (formatter) win.__qhSqlFormatter = formatter;
  loadInto(win, 'qh-data.jsx', 'qh-modal.jsx', 'qh-editor.jsx', 'qh-sqlformat.jsx');
  const noop = () => {};
  const props = {
    tabs: [{ id: 'a', name: 'Query 1', tier: 'RO' }], activeId: 'a',
    onSelect: noop, onClose: noop, onNew: noop, wrap: false, onToggleWrap: noop,
    onCloseOthers: noop, onCloseRight: noop, onCloseAll: noop, onDuplicate: noop,
    onRename: noop, onCopySql: noop, ...extra,
  };
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  act(() => root.render(React.createElement(win.EditorTabs, props)));
  return {
    button: win.document.querySelector('[aria-label="Format SQL"]'),
    click(el) { act(() => el.dispatchEvent(new win.MouseEvent('click', { bubbles: true }))); },
    win,
  };
}

test('the tab bar shows a Format button that calls onFormat, with the shortcut in its tooltip', dom, () => {
  let calls = 0;
  const t = mountTabs({ onFormat: () => { calls += 1; } }, { format });
  assert.ok(t.button, 'the button is there');
  assert.match(t.button.getAttribute('data-kbd'), /^Format SQL {2}· /);
  t.click(t.button);
  assert.equal(calls, 1);
});

test('the tab bar has no Format button without a formatter, and none without onFormat', dom, () => {
  assert.equal(mountTabs({ onFormat: () => {} }, null).button, null);
  assert.equal(mountTabs({}, { format }).button, null);
});

test('the app wires the button to the editor and lists the shortcut', () => {
  const app = web('qh-app.jsx');
  assert.match(app, /onFormat=\{\(\) => setFmtSig\(n => n \+ 1\)\}/);
  assert.match(app, /formatSignal=\{fmtSig\}/);
  assert.match(app, /fmt: QH_MAC \? '⇧ ⌥ F' : 'Shift Alt F'/);
  assert.match(web('qh-editor.jsx'), /\['⇧ \+ ⌥ \/ Alt \+ F', 'Format SQL'\]/);
});
