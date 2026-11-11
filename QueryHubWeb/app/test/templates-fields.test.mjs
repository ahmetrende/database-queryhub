// Statement templates and snippet fields (2026-11-11, Devart C3 and D2).
//
//   * Tab right after `INSERT INTO t`, `UPDATE t`, `EXEC p` (SQL Server) or
//     `CALL p` (PostgreSQL) writes the rest of the statement. Each value is a
//     field `<name>`, with the type, NOT NULL and PK in a comment.
//   * A snippet body can hold fields `$name$` and the literals $DATE$, $TIME$,
//     $DB$, $SERVER$ and $USER$.
//   * Tab and Shift+Tab go from field to field. An edit inside a field moves
//     the fields after it. Escape, a click outside or an edit outside every
//     field ends them.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { bareWindow, loadInto } from './_load.mjs';
import { web } from './_mount.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');

const meta = (name, type, nullable, pk = false) => ({ name, type, nullable, pk });
const ORDERS = [meta('id', 'int4', false, true), meta('total', 'numeric', false), meta('note', 'text', true)];
const LOG = [meta('at', 'timestamptz', false), meta('msg', 'text', true)];
const SCHEMA = {
  tables: ['orders', 'event_log'],
  columns: [],
  dbs: [],
  tableCols: { orders: ['id', 'total', 'note'], event_log: ['at', 'msg'] },
  tableColsQ: {},
  tableMeta: { orders: ORDERS, event_log: LOG },
  tableMetaQ: { 'public.orders': ORDERS, 'public.event_log': LOG },
  qualify: { orders: 'public.orders', event_log: 'public.event_log' },
  functionSigs: {
    load_day: [{ s: 'dbo', n: 'load_day', k: 'procedure', args: '@d date, @force bit', ret: '' }],
    refresh: [{ s: 'app', n: 'refresh', k: 'procedure', args: 'p_day date, OUT p_n integer, p_force boolean DEFAULT false', ret: '' }],
    twice: [{ s: 'app', n: 'twice', k: 'procedure', args: 'a int', ret: '' }, { s: 'app', n: 'twice', k: 'procedure', args: 'a text', ret: '' }],
    calc: [{ s: 'app', n: 'calc', k: 'function', args: 'a int', ret: 'int' }],
  },
  context: { db: 'shop', server: 'srv-alpha', user: 'Ada' },
};
const tpl = (sql, eng = 'postgres') => {
  const c = sql.indexOf('|');
  return W.qhStatementTemplate(sql.replace('|', ''), c, SCHEMA, eng, null);
};
const fields = (t) => t.stops.map(([a, b]) => t.text.slice(a, b));

// ---------------------------------------------------------------------------
// Snippet markers
// ---------------------------------------------------------------------------

test('fields and literals in a snippet body', () => {
  const r = W.qhSnippetText('DELETE FROM $table$\nWHERE $condition$', '  ');
  assert.equal(r.text, 'DELETE FROM <table>\n  WHERE <condition>');
  assert.deepEqual(r.stops.map(([a, b]) => r.text.slice(a, b)), ['<table>', '<condition>'], 'offsets count the indent');
  assert.equal(r.caretAt, r.text.length, 'no caret marker: the end');
  const h = W.qhSnippetText('-- $DATE$ $TIME$ $USER$\n-- $SERVER$ / $DB$\n$CARET$', '', SCHEMA.context, new Date(2026, 10, 11, 9, 5));
  assert.equal(h.text, '-- 2026-11-11 09:05 Ada\n-- srv-alpha / shop\n');
  assert.equal(h.caretAt, h.text.length);
  assert.equal(h.stops, undefined, 'no field: no stops key');
  const e = W.qhSnippetText('-- $USER$ on $DB$', '', {});
  assert.deepEqual(e.stops.map(([a, b]) => e.text.slice(a, b)), ['<user>', '<db>'], 'an unknown literal becomes a field');
  assert.equal(W.qhSnippetText('x $Foo$ $$ $1 $body$', '').text, 'x $Foo$ $$ $1 <body>', 'other dollar signs stay');
  assert.deepEqual(W.qhSnippetText('SELECT * FROM $CURSOR$', ''), { text: 'SELECT * FROM ', caretAt: 14 }, 'the old shape is unchanged');
});

test('the built-in snippets use fields', () => {
  const body = (k) => W.qhSnippetsFor('postgres').find(s => s.key === k).body;
  assert.deepEqual(W.qhSnippetText(body('dfw'), '').stops.length, 2);
  assert.equal(W.qhSnippetText(body('cw'), '').text, 'CASE WHEN <condition> THEN <result> ELSE <other> END');
  assert.equal(W.qhSnippetText(body('exs'), '').text, 'EXISTS (SELECT 1 FROM <table> WHERE <condition>)');
  assert.equal(W.qhSnippetText(body('hdr'), '', SCHEMA.context, new Date(2026, 0, 2)).text, '-- 2026-01-02 Ada\n-- srv-alpha / shop\n');
  const s = W.qhBuildSuggest('hdr', 3, SCHEMA, 'postgres');
  const it = s.items.find(i => i.label === 'hdr');
  assert.ok(it.text.includes('srv-alpha / shop'), 'the typing list fills the literals from the editor context');
  const d = W.qhBuildSuggest('dfw', 3, SCHEMA, 'postgres').items.find(i => i.label === 'dfw');
  assert.deepEqual(d.stops, [[12, 19], [26, 37]], 'a snippet row carries its fields');
});

// ---------------------------------------------------------------------------
// Moving the fields
// ---------------------------------------------------------------------------

test('an edit inside a field moves the fields after it', () => {
  const o = 'DELETE FROM <table>\nWHERE <condition>';
  const ses = { list: [[12, 19], [26, 37]], idx: 0, final: 37 };
  assert.deepEqual(W.qhStopsAfterEdit(ses, o, 'DELETE FROM o\nWHERE <condition>', [12, 19]),
    { list: [[12, 13], [20, 31]], idx: 0, final: 31 }, 'typing over the field');
  assert.deepEqual(W.qhStopsAfterEdit({ list: [[12, 13], [20, 31]], idx: 0, final: 31 }, 'DELETE FROM o\nWHERE <condition>',
    'DELETE FROM or\nWHERE <condition>', [13, 13]), { list: [[12, 14], [21, 32]], idx: 0, final: 32 }, 'typing at the end of the field');
  assert.deepEqual(W.qhStopsAfterEdit(ses, o, 'DELETE FROM <table>\nWHERE x', [26, 37]),
    { list: [[12, 19], [26, 27]], idx: 1, final: 27 }, 'an edit in another field makes it the current one');
  assert.equal(W.qhStopsAfterEdit(ses, o, 'X' + o, [0, 0]), null, 'an edit outside every field ends them');
  assert.equal(W.qhStopsAfterEdit(null, o, o + 'x', null), null);
});

test('an edit on the border of two fields stays in the current one', () => {
  const ses = { list: [[2, 5], [5, 8]], idx: 1, final: 8 };
  assert.deepEqual(W.qhStopsAfterEdit(ses, 'ab<x><y>', 'ab<x>z<y>', [5, 5]), { list: [[2, 5], [5, 9]], idx: 1, final: 9 });
  assert.deepEqual(W.qhStopsAfterEdit({ ...ses, idx: 0 }, 'ab<x><y>', 'ab<x>z<y>', [5, 5]), { list: [[2, 6], [6, 9]], idx: 0, final: 9 });
});

test('the selection before the edit decides where a run of equal characters changed', () => {
  const ses = { list: [[0, 0]], idx: 0, final: 2 };
  assert.equal(W.qhStopsAfterEdit(ses, 'aa', 'aaa', null), null, 'without it the edit looks like it is at the end');
  assert.deepEqual(W.qhStopsAfterEdit(ses, 'aa', 'aaa', [0, 0]), { list: [[0, 1]], idx: 0, final: 3 });
});

// ---------------------------------------------------------------------------
// Templates
// ---------------------------------------------------------------------------

test('INSERT INTO t gives every column, each value a field with a comment', () => {
  const t = tpl('insert into orders|');
  assert.equal(t.kind, 'insert');
  assert.equal(t.text, ' (id, total, note)\nVALUES (\n  <id>,     -- int4 NOT NULL, PK\n  <total>,  -- numeric NOT NULL\n  <note>    -- text\n)');
  assert.deepEqual(fields(t), ['<id>', '<total>', '<note>']);
  assert.equal(t.start, 'insert into orders'.length);
  assert.equal(t.caretAt, t.text.length);
  assert.equal(t.count, 3);
  assert.deepEqual(fields(tpl('INSERT INTO public.event_log |;')), ['<at>', '<msg>'], 'qualified, a space, a semicolon after it');
  assert.equal(tpl('insert orders|', 'mssql').kind, 'insert', 'SQL Server may leave out INTO');
  assert.equal(tpl('insert orders|'), null, 'PostgreSQL needs INTO');
});

test('UPDATE t sets every other column and finds the row by its primary key', () => {
  const t = tpl('update orders|');
  assert.equal(t.text, '\nSET total = <total>,  -- numeric NOT NULL\n    note = <note>     -- text\nWHERE id = <id>       -- int4 NOT NULL, PK');
  assert.deepEqual(fields(t), ['<total>', '<note>', '<id>']);
  const nk = tpl('update event_log|');
  assert.ok(nk.text.endsWith('\nWHERE <condition>'), 'no primary key: a condition field');
  assert.deepEqual(fields(nk), ['<at>', '<msg>', '<condition>']);
  const ind = tpl('    update orders|');
  assert.ok(ind.text.startsWith('\n    SET total'), 'the lines keep the indent of the statement');
});

test('the empty parts of the ii and upw snippets are replaced', () => {
  const sql = 'insert into orders| ()\nVALUES ()';
  const t = tpl(sql);
  assert.equal(t.end, sql.length - 1, 'up to the end of the skeleton');
  const u = 'update orders|\nSET \nWHERE ';
  assert.equal(tpl(u).end, u.indexOf('WHERE') + 5 - 1);
  assert.equal(tpl('insert into orders| (id) values (1)'), null, 'a written column list stays');
  assert.equal(tpl('update orders| set a = 1'), null);
});

test('the template stops at a semicolon or a blank line', () => {
  const sql = 'insert into orders|\n\nselect 1';
  assert.equal(tpl(sql).end, 'insert into orders'.length, 'the next statement after a blank line stays');
  assert.equal(tpl('insert into orders|\nselect 1'), null, 'no blank line: it is one statement');
  assert.equal(tpl('select 1; insert into orders|').kind, 'insert', 'the statement after a semicolon');
});

test('EXEC p (SQL Server) and CALL p (PostgreSQL) give the parameters', () => {
  const e = tpl('exec dbo.load_day|', 'mssql');
  assert.equal(e.text, '\n  @d = <d>,          -- date\n  @force = <force>   -- bit');
  assert.deepEqual(fields(e), ['<d>', '<force>']);
  const c = tpl('call refresh|');
  assert.equal(c.text, '(\n  <p_day>,    -- date\n  <p_n>,      -- integer, OUT\n  <p_force>   -- boolean DEFAULT false\n)');
  assert.equal(tpl('call app.refresh|').kind, 'call', 'qualified by its schema');
  assert.equal(tpl('call other.refresh|'), null, 'another schema');
  assert.equal(tpl('call twice|'), null, 'two overloads: no guess');
  assert.equal(tpl('call calc|'), null, 'a function is no procedure');
  assert.equal(tpl('exec dbo.load_day|'), null, 'EXEC is SQL Server only');
  assert.equal(tpl('call refresh|', 'mssql'), null, 'CALL is PostgreSQL only');
});

test('qhTplParams reads both engines', () => {
  assert.deepEqual(W.qhTplParams('double precision, integer', 'postgres').map(p => [p.name, p.named, p.type]),
    [['arg1', false, 'double precision'], ['arg2', false, 'integer']]);
  assert.deepEqual(W.qhTplParams('n numeric(10,2) DEFAULT 1.5, INOUT x int', 'postgres').map(p => [p.name, p.type, p.def, p.out]),
    [['n', 'numeric(10,2)', '1.5', false], ['x', 'int', '', true]]);
  assert.deepEqual(W.qhTplParams('@a int = 3, @b varchar(10) OUTPUT', 'mssql').map(p => [p.name, p.type, p.def, p.out]),
    [['@a', 'int', '3', false], ['@b', 'varchar(10)', '', true]]);
});

test('no template where it cannot be exact', () => {
  for (const eng of ['clickhouse', 'athena']) assert.equal(tpl('insert into orders|', eng), null, eng + ' is read-only');
  assert.equal(tpl('insert into nope|'), null, 'a table the catalog does not know');
  assert.equal(tpl('insert into ord|ers'), null, 'the caret inside the name');
  assert.equal(tpl('insert into orders.|'), null, 'a name that ends on a dot');
  assert.equal(tpl('insert into orders| select 1'), null);
  assert.equal(tpl("select 'insert into orders|'"), null, 'inside a string');
  assert.equal(tpl('-- insert into orders|'), null, 'inside a comment');
  assert.equal(tpl('insert into|'), null);
  assert.equal(tpl('delete from orders|'), null, 'DELETE has no template');
  const item = W.qhTemplateItem(tpl('update orders|'));
  assert.equal(item.label, 'Template: UPDATE, 3 columns');
  assert.equal(item.type, 'template');
});

// ---------------------------------------------------------------------------
// The real editor
// ---------------------------------------------------------------------------

function mount(initial, engineId = 'postgres', extra = {}) {
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
  const ctl = {};
  function Host() {
    const [v, setV] = React.useState(initial);
    ctl.v = v;
    return React.createElement(win.SqlEditor, { value: v, docId: 't1', fontSize: 13, schema: SCHEMA, engineId, onChange: setV, ...extra });
  }
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(Host)));
  const ta = win.document.querySelector('textarea');
  const setValue = Object.getOwnPropertyDescriptor(win.HTMLTextAreaElement.prototype, 'value').set;
  const frame = () => new Promise(r => win.requestAnimationFrame(r));
  const ed = {
    win, ta, ctl, frame,
    caret(pos) { ta.focus(); ta.setSelectionRange(pos, pos); },
    sel() { return ta.value.slice(ta.selectionStart, ta.selectionEnd); },
    type(s) {
      for (const ch of s) {
        const a = ta.selectionStart, b = ta.selectionEnd, v = ta.value;
        React.act(() => {
          ta.dispatchEvent(new win.InputEvent('beforeinput', { inputType: 'insertText', data: ch, bubbles: true, cancelable: true }));
          setValue.call(ta, v.slice(0, a) + ch + v.slice(b));
          ta.setSelectionRange(a + 1, a + 1);
          ta.dispatchEvent(new win.InputEvent('input', { inputType: 'insertText', data: ch, bubbles: true }));
        });
      }
    },
    press(key, mods = {}) {
      const ev = new win.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...mods });
      React.act(() => ta.dispatchEvent(ev));
      return ev;
    },
    click() { React.act(() => ta.dispatchEvent(new win.MouseEvent('click', { bubbles: true }))); },
    marks() { return Array.from(win.document.querySelectorAll('.qh-stops .qh-stop')).map(m => m.textContent + (m.classList.contains('is-cur') ? '*' : '')); },
    popup() { return win.document.querySelector('.qh-ac-ed'); },
  };
  return ed;
}

const dom = JSDOM ? {} : { skip: 'jsdom not installed' };

test('Tab writes the template and Tab goes from field to field', dom, async () => {
  const ed = mount('insert into orders');
  ed.caret(18);
  const ev = ed.press('Tab');
  assert.ok(ev.defaultPrevented);
  assert.ok(ed.ctl.v.startsWith('insert into orders (id, total, note)\nVALUES (\n  <id>,'));
  await ed.frame();
  assert.equal(ed.sel(), '<id>', 'the first field is selected');
  assert.deepEqual(ed.marks(), ['<id>*', '<total>', '<note>']);
  ed.type('7');
  assert.ok(ed.ctl.v.includes('  7,     -- int4'), 'typing replaces the field');
  ed.press('Tab');
  assert.equal(ed.sel(), '<total>', 'the next field moved with the edit');
  ed.type('9.5');
  ed.press('Tab', { shiftKey: true });
  assert.equal(ed.sel(), '7', 'Shift+Tab goes back');
  ed.press('Tab');
  ed.press('Tab');
  assert.equal(ed.sel(), '<note>');
  ed.press('Tab');
  assert.equal(ed.ta.selectionStart, ed.ctl.v.length, 'Tab on the last field puts the caret at the end');
  assert.deepEqual(ed.marks(), [], 'and ends the fields');
  ed.press('Tab');
  assert.ok(ed.ctl.v.endsWith(')  '), 'after that, Tab indents again');
});

test('Escape and a click outside end the fields', dom, async () => {
  const ed = mount('update orders');
  ed.caret(13);
  ed.press('Tab');
  await ed.frame();
  assert.equal(ed.marks().length, 3);
  ed.press('Escape');
  assert.deepEqual(ed.marks(), [], 'Escape');
  const ed2 = mount('update orders');
  ed2.caret(13);
  ed2.press('Tab');
  await ed2.frame();
  ed2.caret(2);
  ed2.click();
  assert.deepEqual(ed2.marks(), [], 'a click outside every field');
  const ed3 = mount('update orders');
  ed3.caret(13);
  ed3.press('Tab');
  await ed3.frame();
  ed3.caret(0);
  ed3.type('x');
  assert.deepEqual(ed3.marks(), [], 'an edit outside every field');
});

test('one Ctrl+Z takes the template back', dom, async () => {
  const ed = mount('update orders');
  ed.caret(13);
  ed.press('Tab');
  await ed.frame();
  ed.press('z', { ctrlKey: true });
  assert.equal(ed.ctl.v, 'update orders');
  assert.deepEqual(ed.marks(), []);
  const ed2 = mount('update orders');
  ed2.caret(13);
  ed2.press('Tab');
  await ed2.frame();
  ed2.type('7');
  ed2.press('z', { ctrlKey: true });
  assert.ok(ed2.ctl.v.includes('SET total = <total>,'), 'the undo takes the typing back');
  assert.deepEqual(ed2.marks(), [], 'and ends the fields, so no field points at old offsets');
});

test('Ctrl+Space offers the template first', dom, async () => {
  const ed = mount('exec dbo.load_day', 'mssql');
  ed.caret(17);
  ed.press(' ', { ctrlKey: true, code: 'Space' });
  const first = ed.popup() && ed.popup().querySelector('.qh-ac-ed-opt');
  assert.ok(first && first.textContent.startsWith('Template: EXEC, 2 parameters'), first && first.textContent);
  ed.press('Enter');
  assert.equal(ed.ctl.v, 'exec dbo.load_day\n  @d = <d>,          -- date\n  @force = <force>   -- bit');
  await ed.frame();
  assert.equal(ed.sel(), '<d>');
});

test('a snippet with fields: Tab moves through them', dom, async () => {
  const ed = mount('');
  ed.caret(0);
  ed.type('dfw');
  ed.press('Tab');
  assert.equal(ed.ctl.v, 'DELETE FROM <table>\nWHERE <condition>');
  await ed.frame();
  assert.equal(ed.sel(), '<table>');
  ed.type('orders');
  ed.press('Escape');                                         // closes the typing list
  ed.press('Tab');
  assert.equal(ed.sel(), '<condition>');
});

test('Tab elsewhere and on a read-only engine still indents', dom, () => {
  const ed = mount('select 1');
  ed.caret(8);
  ed.press('Tab');
  assert.equal(ed.ctl.v, 'select 1  ');
  const ch = mount('insert into orders', 'clickhouse');
  ch.caret(18);
  ch.press('Tab');
  assert.equal(ch.ctl.v, 'insert into orders  ');
});

test('a selected field is no selection: Run, F8 and the verdicts take the whole text', dom, async () => {
  const getter = { current: null };
  const seen = [], ran = [];
  const ed = mount('update orders', 'postgres', { selectionGetter: getter, onSelectionChange: (s) => seen.push(s), onRunSelection: (s) => ran.push(s) });
  ed.caret(13);
  ed.press('Tab');
  await ed.frame();
  assert.equal(ed.sel(), '<total>');
  assert.equal(getter.current(), '', 'the Run button gets no selection');
  ed.ta.dispatchEvent(new ed.win.Event('select', { bubbles: true }));
  assert.equal(seen[seen.length - 1], null, 'the parent hears of no selection');
  ed.press('F8');
  assert.equal(ran.length, 1);
  assert.ok(ran[0].startsWith('update orders\nSET total = <total>'), 'F8 runs the statement, not the field');
  ed.press('Escape');
  ed.ta.setSelectionRange(0, 6);
  assert.equal(getter.current(), 'update', 'a plain selection still counts');
});

test('a quote typed over a field replaces it, it does not wrap it', dom, async () => {
  const ed = mount('insert into orders');
  ed.caret(18);
  ed.press('Tab');
  await ed.frame();
  const ev = ed.press("'");
  assert.equal(ev.defaultPrevented, false, 'the browser types the quote over the field');
  assert.ok(ed.ctl.v.includes('  <id>,'), 'no wrap');
  ed.type("'x'");
  assert.ok(ed.ctl.v.includes("  'x',     -- int4"), ed.ctl.v);
  const p = mount('select 1');
  p.ta.focus();
  p.ta.setSelectionRange(7, 8);
  assert.equal(p.press("'").defaultPrevented, true, 'a plain selection still wraps');
});

// ---------------------------------------------------------------------------
// The wiring
// ---------------------------------------------------------------------------

test('shortcut rows, styles and the editor context', () => {
  const src = web('qh-editor.jsx');
  assert.match(src, /\['Tab after INSERT INTO t, UPDATE t, EXEC p, CALL p', 'Statement template'\]/);
  assert.match(src, /\['Tab \/ ⇧ \+ Tab in a template or snippet', 'Next \/ previous field'\]/);
  const html = web('QueryHub.html');
  assert.match(html, /\.qh-stop \{ color: transparent; background: transparent;/, 'a mark is yellow by default');
  assert.match(html, /\.qh-stop\.is-cur \{/);
  assert.match(html, /\.ac-t-snippet, \.ac-t-template \{/);
  const app = web('qh-app.jsx');
  assert.match(app, /const context = \{ db: tab\.db \|\| '', server: conn \? String\(conn\.name \|\| conn\.id \|\| ''\) : '',/);
  assert.match(app, /schemas, context,\n\s+snippets: userSnippets \};\n  \}, \[tab\.conn, tab\.db, schemaCache, conns, user, userSnippets\]\);/);
});
