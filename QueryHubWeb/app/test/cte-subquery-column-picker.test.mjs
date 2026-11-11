// CTE and subquery columns, and the column picker (2026-11-11, Devart A20
// and A13).
//
//   * The column list reads a CTE of the statement and a subquery in FROM.
//     Their columns are the output names of the first SELECT. A `*` counts
//     only when the catalog or the live lookup knows every relation that it
//     covers. A column that comes straight from a table keeps its type and
//     its PII mark.
//   * After FROM and JOIN the typing list offers the CTE names first.
//   * In `INSERT INTO t (|` Ctrl+Space lists the columns of t that the list
//     does not name yet.
//   * Each row of the column list has a check box. Ctrl+Space or a click on
//     the box checks the row. Enter inserts the checked columns in check
//     order, joined with ', '.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { bareWindow, loadInto } from './_load.mjs';
import { web } from './_mount.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');

const meta = (name, type, nullable, pk = false, pii = false) => ({ name, type, nullable, pk, pii });
const ORDERS = [meta('id', 'int4', false, true), meta('total', 'numeric', false), meta('note', 'text', true)];
const USERS = [meta('id', 'int8', false, true), meta('email', 'text', true, false, true)];
const SCHEMA = {
  tables: ['orders', 'users'],
  columns: ['id', 'total', 'note', 'email'],
  dbs: [],
  tableCols: { orders: ['id', 'total', 'note'], users: ['id', 'email'] },
  tableColsQ: { 'public.orders': ['id', 'total', 'note'], 'public.users': ['id', 'email'] },
  tableMeta: { orders: ORDERS, users: USERS },
  tableMetaQ: { 'public.orders': ORDERS, 'public.users': USERS },
  qualify: { orders: 'public.orders', users: 'public.users' },
};
const at = (sql, live = null, engine = 'postgres') => {
  const caret = sql.indexOf('|');
  return W.qhColumnList(sql.replace('|', ''), caret, SCHEMA, engine, live);
};
const picks = (r) => r.items.filter(i => !i.header).map(i => i.text);
const heads = (r) => r.items.filter(i => i.header).map(i => (i.alias ? i.label + ' ' + i.alias : i.label));
const row = (r, label) => r.items.find(i => !i.header && i.label === label);

// ---------------------------------------------------------------------------
// CTE columns
// ---------------------------------------------------------------------------

test('a CTE lists its output names: alias, implicit alias, bare column', () => {
  const r = at('with c as (select id, total as t, note n, count(*) from orders group by 1) select | from c');
  assert.equal(r.kind, 'ready');
  assert.deepEqual(picks(r), ['id', 't', 'n'], 'count(*) has no name, so it is left out');
  assert.deepEqual(heads(r), ['c']);
  assert.deepEqual([row(r, 'id').colType, row(r, 't').colType, row(r, 'n').colType], ['int4', 'numeric', 'text']);
  assert.equal(row(r, 'id').pk, true, 'a column straight from a table keeps its primary key mark');
});

test('an explicit CTE column list names the columns by position', () => {
  const r = at('with c (a, b) as (select id, total from orders) select | from c');
  assert.deepEqual(picks(r), ['a', 'b']);
  assert.equal(row(r, 'b').colType, 'numeric', 'the type comes from the column at that position');
  const loose = at('with c (a, b) as (select id, 1 + 2 from orders) select | from c');
  assert.deepEqual(picks(loose), ['a', 'b'], 'names stay when the body cannot be matched by position');
  assert.equal(row(loose, 'a').colType, '', 'no type is guessed then');
  const shifted = at('with c (a, b) as (select 1 + 2, id, total from orders) select | from c');
  assert.deepEqual([row(shifted, 'a').colType, row(shifted, 'b').colType], ['', ''],
    'an item without a name shifts the positions, so no type is taken');
});

test('a CTE star expands from the catalog and keeps the PII mark', () => {
  const r = at('with c as (select * from users) select | from c x join orders o on o.id = x.id');
  assert.deepEqual(picks(r), ['x.id', 'x.email', 'o.id', 'o.total', 'o.note']);
  assert.deepEqual(heads(r), ['c x', 'orders o']);
  assert.equal(row(r, 'email').pii, true);
  const q = at('with c as (select u.* from users u) select x.| from c x');
  assert.deepEqual(picks(q), ['id', 'email'], 'a typed qualifier lists the CTE bare');
});

test('chained and recursive CTEs', () => {
  const r = at('with a as (select id from orders), b as (select a.id, 5 five from a) select | from b');
  assert.deepEqual(picks(r), ['id', 'five']);
  assert.equal(row(r, 'id').colType, 'int4', 'the type travels through both CTEs');
  const rec = at('with recursive r as (select id from orders union all select r.id from r) select | from r');
  assert.deepEqual(picks(rec), ['id'], 'the anchor SELECT names the columns');
  const self = at('with c as (select * from c) select | from c');
  assert.equal(self.count, 0, 'a CTE that reads itself lists nothing');
});

test('a CTE star over an unknown table asks the live lookup once', () => {
  assert.deepEqual(at('with c as (select * from nope) select | from c', () => undefined), { kind: 'lookup', names: ['nope'] });
  assert.deepEqual(picks(at('with c as (select * from nope) select | from c', () => ['a', 'b'])), ['a', 'b']);
  assert.equal(at('with c as (select * from nope) select | from c', () => null).count, 0, 'nobody knows it: nothing');
  assert.equal(at('with c as (select * from nope) select | from c').count, 0, 'no lookup: nothing');
  assert.equal(at('with c as (select * from orders o join nope n on n.id = o.id) select | from c').count, 0,
    'a star over one unknown relation lists nothing, never a part of the columns');
  assert.deepEqual(at('with c as (select id from nope) select | from c join nope2 x on x.id = c.id', () => undefined),
    { kind: 'lookup', names: ['nope2'] }, 'a relation that only gives types is never asked about');
  let asked = 0;
  const r = at('with c as (select id from nope) select | from c', () => { asked++; return undefined; });
  assert.deepEqual(picks(r), ['id'], 'a named column needs no lookup');
  assert.equal(asked, 1, 'the type check may peek, but it never asks the server');
});

test('a CTE name is never sent to the server', () => {
  const seen = [];
  const r = at('with recent as (select id from orders) select | from recent', (n) => { seen.push(n); return undefined; });
  assert.deepEqual(picks(r), ['id']);
  assert.deepEqual(seen, [], 'the CTE is read from the text');
});

// ---------------------------------------------------------------------------
// Subquery columns
// ---------------------------------------------------------------------------

test('a subquery in FROM lists its output names', () => {
  const r = at('select | from (select id, email as mail from users) s');
  assert.deepEqual(picks(r), ['id', 'mail']);
  assert.deepEqual(heads(r), ['(subquery) s']);
  assert.equal(row(r, 'mail').pii, true, 'a renamed PII column keeps its mark');
  const q = at('select s.| from (select o.id oid, u.email from orders o join users u on u.id = o.id) s');
  assert.deepEqual(picks(q), ['oid', 'email']);
  assert.equal(row(q, 'oid').colType, 'int4');
});

test('column aliases of a subquery, a subquery with no alias, LATERAL', () => {
  assert.deepEqual(picks(at("select | from (select 1 as one, 'x' lbl) s (p, q)")), ['p', 'q']);
  const bare = at('select | from (select id from orders)');
  assert.deepEqual(picks(bare), ['id']);
  assert.deepEqual(heads(bare), ['(subquery)']);
  const two = at('select | from (select id from orders) join users u on u.id = 1');
  assert.deepEqual(picks(two), ['id', 'u.id', 'u.email'], 'no alias: the subquery columns go in bare');
  const lat = at('select | from users u cross join lateral (select total from orders o where o.id = u.id) t');
  assert.deepEqual(picks(lat), ['u.id', 'u.email', 't.total']);
});

test('output names follow the engine', () => {
  assert.deepEqual(picks(at('with c as (select Total, id AS Ident from orders) select | from c')), ['total', 'ident'],
    'PostgreSQL folds a name without quotes');
  assert.deepEqual(picks(at('with c as (select id AS "Ident" from orders) select | from c')), ['"Ident"'], 'a quoted name stays');
  assert.deepEqual(picks(at('with c as (select tot = total, id AS Ident from orders) select | from c', null, 'mssql')),
    ['tot', 'Ident'], 'SQL Server keeps the case and reads name = expr');
});

test('other readers still refuse a subquery', () => {
  const toks = W.qhSqlTokens('select * from (select 1) s');
  const depth = [];
  let d = 0;
  for (const t of toks) { if (t.v === ')') d--; depth.push(d); if (t.v === '(') d++; }
  assert.equal(W.qhReadFromList(toks, depth, 1, 0, true), null, 'without the flag a subquery gives null');
  assert.ok(W.qhReadFromList(toks, depth, 1, 0, true, true)[0].sub, 'with the flag it is read');
  assert.equal(W.qhStarExpansion('select * from (select 1 as x) s', 8, SCHEMA, 'postgres', null), null, 'the * expansion stays exact');
});

test('qhCteDefs reads the WITH list only', () => {
  const defs = (sql) => {
    const S = W.qhSqlTokens(sql);
    const depth = [];
    let d = 0;
    for (const t of S) { if (t.v === ')') d--; depth.push(d); if (t.v === '(') d++; }
    return Array.from(W.qhCteDefs(S, depth).keys());
  };
  assert.deepEqual(defs('with a as (select 1), b (x) as materialized (select 2) select 1'), ['a', 'b']);
  assert.deepEqual(defs('with x as not materialized (select 1) select 1'), ['x']);
  assert.deepEqual(defs('with 1 as x select x'), [], 'a ClickHouse scalar WITH is no CTE');
  assert.deepEqual(defs('select top 5 with ties id from t order by id'), []);
  assert.deepEqual(defs('select * from t with (nolock)'), [], 'a table hint is no CTE');
});

// ---------------------------------------------------------------------------
// CTE names in the typing list
// ---------------------------------------------------------------------------

test('after FROM and JOIN the CTE names come first', () => {
  const s = (sql, opts) => { const c = sql.indexOf('|'); return W.qhBuildSuggest(sql.replace('|', ''), c, SCHEMA, 'postgres', opts); };
  const a = s('with recent as (select 1) select * from re|');
  assert.equal(a.items[0].type, 'cte');
  assert.equal(a.items[0].text, 'recent');
  const b = s('with recent as (select 1) select * from orders join |', { force: true });
  assert.deepEqual(b.items.slice(0, 2).map(i => i.type + ':' + i.text), ['cte:recent', 'table:public.orders']);
  const q = s('with "Recent" as (select 1) select * from |', { force: true });
  assert.equal(q.items[0].text, '"Recent"', 'a CTE name goes in as written');
  assert.ok(!s('with recent as (select 1) insert into |', { force: true }).items.some(i => i.type === 'cte'), 'not after INTO');
  assert.ok(!s('select * from |; with recent as (select 1) select 1', { force: true }).items.some(i => i.type === 'cte'),
    'a CTE of another statement is not offered');
  assert.equal(W.QH_AC_TYPE_LABEL ? W.QH_AC_TYPE_LABEL.cte : 'cte', 'cte');
});

// ---------------------------------------------------------------------------
// The INSERT column list
// ---------------------------------------------------------------------------

test('INSERT INTO t ( lists the columns that the list does not name yet', () => {
  const r = at('insert into orders (|');
  assert.deepEqual(picks(r), ['id', 'total', 'note']);
  assert.deepEqual(heads(r), ['orders']);
  assert.equal(r.qual, '');
  assert.deepEqual(picks(at('insert into orders (id, |) values (1, 2)')), ['total', 'note']);
  assert.deepEqual(picks(at('insert into public.orders (to|')), ['total'], 'a qualified table and a typed prefix');
  assert.deepEqual(picks(at('insert orders (|', null, 'mssql')), ['id', 'total', 'note'], 'SQL Server may leave out INTO');
  assert.deepEqual(at('insert into nope (|', () => undefined), { kind: 'lookup', names: ['nope'] });
});

test('positions that are no INSERT column list', () => {
  assert.equal(at('insert into orders (id) values (|'), null, 'a value goes after VALUES');
  assert.equal(at('insert into orders (id |'), null, 'after a name');
  assert.equal(at('insert into (|'), null, 'no table name yet');
  assert.equal(at('select coalesce(|) from orders').items.length > 0, true, 'a function call stays a SELECT column position');
  assert.ok(!heads(at('select coalesce(|) from orders')).includes('coalesce'));
  assert.equal(at('merge into orders t using users u on t.id = u.id when not matched then insert (|'), null);
});

// ---------------------------------------------------------------------------
// The real editor: the column picker
// ---------------------------------------------------------------------------

function mount(initial, engineId = 'postgres') {
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
    return React.createElement(win.SqlEditor, { value: v, docId: 't1', fontSize: 13, schema: SCHEMA, engineId, onChange: setV });
  }
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(Host)));
  const ta = win.document.querySelector('textarea');
  const setValue = Object.getOwnPropertyDescriptor(win.HTMLTextAreaElement.prototype, 'value').set;
  const ed = {
    win, ta, ctl,
    caret(pos) { ta.focus(); ta.setSelectionRange(pos, pos); },
    type(s) {
      for (const ch of s) {
        const a = ta.selectionStart, v = ta.value;
        React.act(() => {
          ta.dispatchEvent(new win.InputEvent('beforeinput', { inputType: 'insertText', data: ch, bubbles: true, cancelable: true }));
          setValue.call(ta, v.slice(0, a) + ch + v.slice(a));
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
    ctrlSpace() { return ed.press(' ', { ctrlKey: true, code: 'Space' }); },
    down(el) { React.act(() => el.dispatchEvent(new win.MouseEvent('mousedown', { bubbles: true, cancelable: true }))); },
    popup() { return win.document.querySelector('.qh-ac-ed'); },
    opts() { return Array.from(win.document.querySelectorAll('.qh-ac-ed-opt')); },
    boxes() { return Array.from(win.document.querySelectorAll('.qh-ac-ed-chk')); },
    marks() { return ed.boxes().map(b => b.textContent); },
    foot() { const f = win.document.querySelector('.qh-ac-ed-foot'); return f ? f.textContent : null; },
  };
  return ed;
}

const dom = JSDOM ? {} : { skip: 'jsdom not installed' };
const JOIN = 'select  from orders o join users u on u.id = o.id';

test('every column row has a check box, and Ctrl+Space checks the highlighted one', dom, () => {
  const ed = mount(JOIN);
  ed.caret(7);
  ed.ctrlSpace();
  assert.equal(ed.boxes().length, 5, 'one box per column row, none on a group header');
  assert.equal(ed.foot(), 'Ctrl + Space or a click on the box: check a column');
  const ev = ed.ctrlSpace();
  assert.ok(ev.defaultPrevented);
  assert.deepEqual(ed.marks(), ['1', '', '', '', '']);
  assert.equal(ed.boxes()[0].getAttribute('aria-checked'), 'true');
  ed.press('ArrowDown');
  ed.press('ArrowDown');
  ed.ctrlSpace();
  assert.deepEqual(ed.marks(), ['1', '', '2', '', '']);
  assert.equal(ed.foot(), 'Enter: insert 2 columns');
  assert.equal(ed.ctl.v, JOIN, 'checking types nothing');
  ed.ctrlSpace();
  assert.deepEqual(ed.marks(), ['1', '', '', '', ''], 'Ctrl+Space again unchecks');
});

test('Enter inserts the checked columns in check order', dom, async () => {
  const ed = mount(JOIN);
  ed.caret(7);
  ed.ctrlSpace();
  ed.press('ArrowDown');
  ed.press('ArrowDown');
  ed.ctrlSpace();                                            // o.note
  ed.press('ArrowUp');
  ed.press('ArrowUp');
  ed.ctrlSpace();                                            // o.id
  ed.press('Enter');
  assert.equal(ed.ctl.v, 'select o.note, o.id from orders o join users u on u.id = o.id');
  assert.equal(ed.popup(), null);
  await new Promise(r => ed.win.requestAnimationFrame(r));
  assert.equal(ed.ta.selectionStart, 'select o.note, o.id'.length, 'the caret goes after the inserted text');
});

test('a click on the box checks the row and keeps the list open', dom, () => {
  const ed = mount(JOIN);
  ed.caret(7);
  ed.ctrlSpace();
  ed.down(ed.boxes()[4]);
  assert.ok(ed.popup(), 'the list stays open');
  assert.deepEqual(ed.marks(), ['', '', '', '', '1']);
  assert.equal(ed.ctl.v, JOIN);
  ed.down(ed.boxes()[1]);
  assert.deepEqual(ed.marks(), ['', '2', '', '', '1']);
  ed.down(ed.opts()[0].querySelector('.qh-ac-ed-name'));
  assert.equal(ed.ctl.v, 'select u.email, o.total from orders o join users u on u.id = o.id',
    'a click on a row inserts the checked columns, as Enter does');
});

test('typing filters the list and the checks stay', dom, () => {
  const ed = mount(JOIN);
  ed.caret(7);
  ed.ctrlSpace();
  ed.ctrlSpace();                                            // o.id
  ed.type('em');
  assert.equal(ed.opts().length, 1, 'filtered to email');
  ed.ctrlSpace();                                            // u.email
  assert.equal(ed.foot(), 'Enter: insert 2 columns', 'the hidden check still counts');
  ed.press('Enter');
  assert.equal(ed.ctl.v, 'select o.id, u.email from orders o join users u on u.id = o.id', 'the typed prefix is replaced');
});

test('Enter inserts the checks also when the typed word is the highlighted column', dom, () => {
  const ed = mount('select  from orders');
  ed.caret(7);
  ed.ctrlSpace();
  ed.press('ArrowDown');
  ed.ctrlSpace();                                            // total
  ed.type('id');
  assert.equal(ed.opts().length, 1);
  ed.ctrlSpace();                                            // id
  const ev = ed.press('Enter');
  assert.ok(ev.defaultPrevented, 'Enter is no new line here');
  assert.equal(ed.ctl.v, 'select total, id from orders');
});

test('with nothing checked, Enter inserts the highlighted column as before', dom, () => {
  const ed = mount(JOIN);
  ed.caret(7);
  ed.ctrlSpace();
  ed.press('ArrowDown');
  ed.press('Enter');
  assert.equal(ed.ctl.v, 'select o.total from orders o join users u on u.id = o.id');
});

test('after a typed qualifier each checked column gets it', dom, () => {
  const ed = mount('select  from orders o join users u on u.id = o.id');
  ed.caret(7);
  ed.type('u.');
  assert.ok(ed.popup() && ed.popup().classList.contains('is-cols'), 'the alias list opens');
  ed.press('ArrowDown');
  ed.ctrlSpace();                                            // email
  ed.press('ArrowUp');
  ed.ctrlSpace();                                            // id
  ed.press('Enter');
  assert.equal(ed.ctl.v, 'select u.email, u.id from orders o join users u on u.id = o.id');
});

test('the INSERT column list takes several columns', dom, () => {
  const ed = mount('insert into orders () values (1, 2)');
  ed.caret(20);
  ed.ctrlSpace();
  assert.equal(ed.opts().length, 3);
  ed.press('ArrowDown');
  ed.ctrlSpace();
  ed.press('ArrowDown');
  ed.ctrlSpace();
  ed.press('Enter');
  assert.equal(ed.ctl.v, 'insert into orders (total, note) values (1, 2)');
});

test('a plain Space still types, and the normal list has no boxes', dom, () => {
  const ed = mount(JOIN);
  ed.caret(7);
  ed.ctrlSpace();
  const ev = ed.press(' ');
  assert.equal(ev.defaultPrevented, false, 'the editor leaves a plain Space to the browser');
  const n = mount('select * from ');
  n.caret(14);
  n.ctrlSpace();
  assert.ok(n.popup() && !n.popup().classList.contains('is-cols'));
  assert.equal(n.boxes().length, 0);
  assert.equal(n.foot(), null);
  n.ctrlSpace();
  assert.ok(n.popup(), 'Ctrl+Space in the normal list opens it again');
});

test('CTE columns in the real editor', dom, () => {
  const sql = 'with c as (select id, total as t from orders) select  from c';
  const ed = mount(sql);
  ed.caret(sql.indexOf('select  from') + 7);
  ed.ctrlSpace();
  assert.deepEqual(ed.opts().map(o => o.querySelector('.qh-ac-ed-name').textContent), ['id', 't']);
});

// ---------------------------------------------------------------------------
// The wiring
// ---------------------------------------------------------------------------

test('the shortcut row and the styles exist', () => {
  const src = web('qh-editor.jsx');
  assert.match(src, /\['Ctrl \+ Space in the column list', 'Check a column; Enter inserts the checked ones'\]/);
  const html = web('QueryHub.html');
  assert.match(html, /\.qh-ac-ed\.is-cols \.qh-ac-ed-opt \{ display: grid; grid-template-columns: 16px /);
  assert.match(html, /\.qh-ac-ed-chk\.is-on \{/);
  assert.match(html, /\.qh-ac-ed-foot \{/);
  assert.match(html, /\.ac-t-table, \.ac-t-cte \{/);
});
