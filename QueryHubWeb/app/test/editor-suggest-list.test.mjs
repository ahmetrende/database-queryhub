// The typing list and the star (2026-11-11, Devart medium batch, PR M3).
//
//   * A3  `jc` finds JobCandidate and job_candidate by the initials of their
//         words, after the prefix matches (qhNameInitials).
//   * A4  The list holds up to 50 rows. PageUp and PageDown move 8 rows. A
//         corner grip sizes the list, and this browser keeps the size.
//   * A14 Ctrl+Space after GROUP BY offers the select list's columns with no
//         aggregate in one row (qhGroupByFill).
//   * C2  Shift+Alt+E expands every select-list * of the text or of the
//         selection in one step (qhExpandAllStars).
// Every engine. ClickHouse aggregates (uniqExact) count as aggregates too.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { bareWindow, loadInto } from './_load.mjs';
import { web } from './_mount.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }
const dom = JSDOM ? {} : { skip: 'jsdom not installed' };

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');
const at = (sql) => [sql.replace('|', ''), sql.indexOf('|')];
const gb = (sql) => { const [t, c] = at(sql); const r = W.qhGroupByFill(t, c); return r ? r.text : null; };

// ---------- A3: initials ----------

test('the initials of the words of a name', () => {
  assert.equal(W.qhNameInitials('JobCandidate'), 'jc');
  assert.equal(W.qhNameInitials('job_candidate_skill'), 'jcs');
  assert.equal(W.qhNameInitials('userId2'), 'ui2');
  assert.equal(W.qhNameInitials('users'), '', 'one word has no initials');
});

test('initials rank after a prefix and before a substring', () => {
  const schema = { tables: ['orders', 'JobCandidate', 'job_candidate', 'jcx_table', 'xjc'], columns: [], dbs: [] };
  const r = W.qhBuildSuggest('SELECT * FROM jc', 16, schema, 'postgres');
  assert.deepEqual(r.items.map(i => i.label), ['jcx_table', 'JobCandidate', 'job_candidate', 'xjc']);
  const one = W.qhBuildSuggest('SELECT j', 8, schema, 'postgres');
  assert.ok(!one.items.some(i => i.label === 'JobCandidate' && i.rank === 1.5), 'one letter: no initials match');
});

// ---------- A4: list size ----------

test('the list holds up to 50 rows', () => {
  const tables = Array.from({ length: 60 }, (_, i) => 'tab' + i);
  const r = W.qhBuildSuggest('SELECT * FROM tab', 17, { tables, columns: [], dbs: [] }, 'postgres');
  assert.equal(r.items.length, 50);
  assert.equal(W.QH_AC_MAX, 50);
});

test('PageDown and PageUp move 8 rows, skip headers, and stop at the ends', () => {
  const items = [{ header: true }, ...Array.from({ length: 5 }, () => ({})), { header: true }, ...Array.from({ length: 10 }, () => ({}))];
  assert.equal(W.qhAcPage(items, 1, 1, 8), 10, 'the header at 6 is skipped');
  assert.equal(W.qhAcPage(items, 10, 1, 8), 16, 'stops at the last row');
  assert.equal(W.qhAcPage(items, 3, -1, 8), 1, 'stops at the first row, never on a header');
});

// ---------- A14: GROUP BY ----------

test('GROUP BY takes the non-aggregated select columns', () => {
  assert.equal(gb('SELECT a, b AS bb, count(*) AS n, sum(x) FROM t GROUP BY |'), 'a, b');
  assert.equal(gb('SELECT DISTINCT TOP 10 u.name, u.city c, max(o.total) FROM u JOIN o ON 1 = 1 GROUP BY |'), 'u.name, u.city');
  assert.equal(gb('SELECT TOP (5) PERCENT a FROM t GROUP BY |'), 'a');
  assert.equal(gb("SELECT CASE WHEN a > 1 THEN 'x' END AS k, b FROM t GROUP BY |"), "CASE WHEN a > 1 THEN 'x' END, b");
  assert.equal(gb('SELECT a + b total, c FROM t GROUP BY |'), 'a + b, c', 'an alias without AS');
  assert.equal(gb("SELECT 1, 'x', NULL, *, t.*, a FROM t GROUP BY |"), 'a', 'constants and stars drop');
  assert.equal(gb('SELECT a, row_number() OVER (ORDER BY a) rn FROM t GROUP BY |'), 'a', 'a window expression drops');
  assert.equal(gb('SELECT a, uniqExact(b), argMax(c, d) FROM t GROUP BY |'), 'a', 'ClickHouse aggregates');
});

test('GROUP BY: a subquery, a typed word, and where it gives nothing', () => {
  assert.equal(gb('SELECT a FROM (SELECT b, count(*) FROM s GROUP BY |) q'), 'b', 'the inner SELECT');
  const [t, c] = at('SELECT a, b FROM t GROUP BY a|');
  assert.deepEqual(W.qhGroupByFill(t, c), { start: c - 1, end: c, text: 'a, b', count: 2 }, 'the typed word is replaced');
  assert.equal(gb('SELECT a FROM t ORDER BY |'), null);
  assert.equal(gb('SELECT count(*) FROM t GROUP BY |'), null, 'only aggregates');
  assert.equal(gb('GROUP BY |'), null, 'no SELECT');
  assert.equal(gb('SELECT a FROM t; SELECT b, c FROM u GROUP BY |'), 'b, c', 'its own statement');
});

// ---------- C2: expand every star ----------

const SCH = { tables: ['orders', 'users'], columns: [], dbs: [], tableCols: { orders: ['id', 'total'], users: ['id', 'name'] } };

test('every select-list star of the text expands', () => {
  const sql = 'SELECT * FROM orders;\nSELECT u.* FROM users u;\nSELECT count(*) FROM orders';
  const r = W.qhExpandAllStars(sql, SCH, 'postgres', null, null);
  assert.equal(r.edits.length, 2, 'count(*) stays');
  assert.equal(W.qhApplyEdits(sql, r.edits, 0).text,
    'SELECT id, total FROM orders;\nSELECT u.id, u.name FROM users u;\nSELECT count(*) FROM orders');
});

test('only the stars of a range, and a relation to ask about first', () => {
  const sql = 'SELECT * FROM orders;\nSELECT * FROM users';
  const r = W.qhExpandAllStars(sql, SCH, 'postgres', null, [0, 20]);
  assert.equal(W.qhApplyEdits(sql, r.edits, 0).text, 'SELECT id, total FROM orders;\nSELECT * FROM users');
  const g = W.qhExpandAllStars('SELECT * FROM ghost', SCH, 'postgres', () => undefined, null);
  assert.deepEqual(g, { edits: [], names: ['ghost'] });
});

test('applied edits move a caret after them', () => {
  const r = W.qhApplyEdits('ab*cd*', [{ start: 2, end: 3, text: 'XYZ' }, { start: 5, end: 6, text: 'Q' }], 6);
  assert.deepEqual(r, { text: 'abXYZcdQ', caret: 8 });
});

// ---------- the real editor ----------

function mount(initial, eng = 'postgres', schema = SCH) {
  const d = new JSDOM('<!doctype html><div id="root"></div>', { pretendToBeVisual: true, url: 'https://localhost/' });
  for (const k of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node', 'Event',
                   'KeyboardEvent', 'MouseEvent', 'InputEvent', 'getComputedStyle', 'requestAnimationFrame',
                   'cancelAnimationFrame', 'localStorage']) {
    Object.defineProperty(globalThis, k, { value: d.window[k], configurable: true, writable: true });
  }
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const React = require_('react');
  const ReactDOM = require_('react-dom/client');
  globalThis.React = React;
  const win = d.window;
  win.React = React;
  win.ReactDOM = require_('react-dom');
  loadInto(win, 'qh-data.jsx', 'qh-editor.jsx');
  const ctl = {};
  const docId = 'm3-' + Math.random();
  function Host() {
    const [v, setV] = React.useState(initial);
    ctl.text = v;
    return React.createElement(win.SqlEditor, { value: v, docId, fontSize: 13, schema, engineId: eng, onChange: setV });
  }
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(Host)));
  const ta = win.document.querySelector('textarea');
  const setValue = Object.getOwnPropertyDescriptor(win.HTMLTextAreaElement.prototype, 'value').set;
  return {
    win, ta, ctl,
    caret(i, j = i) { React.act(() => { ta.focus(); ta.setSelectionRange(i, j); }); },
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
    mouse(el, type, init = {}) { React.act(() => el.dispatchEvent(new win.MouseEvent(type, { bubbles: true, cancelable: true, ...init }))); },
    wait(ms) { return React.act(() => new Promise(r => setTimeout(r, ms))); },
    opts() { return Array.from(win.document.querySelectorAll('.qh-ac-ed-opt')); },
    hi() { const o = win.document.querySelector('.qh-ac-ed-opt.is-hi'); return o ? o.textContent : null; },
  };
}

test('Ctrl+Space after GROUP BY offers the non-aggregated columns first, and Enter inserts them', dom, async () => {
  const sql = 'SELECT a, b, count(*) FROM t GROUP BY ';
  const ed = mount(sql);
  ed.caret(sql.length);
  ed.press(' ', { ctrlKey: true, code: 'Space' });
  const first = ed.opts()[0];
  assert.ok(first && /^Group by a, b/.test(first.textContent), first && first.textContent);
  ed.press('Enter');
  assert.equal(ed.ctl.text, sql + 'a, b');
});

test('a typed space after GROUP BY opens no GROUP BY row', dom, async () => {
  const ed = mount('SELECT a, b FROM t GROUP BY');
  ed.caret(27);
  ed.type(' ');
  assert.ok(!ed.opts().some(o => /^Group by/.test(o.textContent)));
});

test('PageDown moves 8 rows in the open list', dom, async () => {
  const tables = Array.from({ length: 20 }, (_, i) => 'tab' + String(i).padStart(2, '0'));
  const ed = mount('SELECT * FROM ', 'postgres', { tables, columns: [], dbs: [] });
  ed.caret(14);
  ed.type('t');
  assert.equal(ed.opts().length, 20);
  assert.match(ed.hi(), /^tab00/);
  ed.press('PageDown');
  assert.match(ed.hi(), /^tab08/);
  ed.press('PageUp');
  assert.match(ed.hi(), /^tab00/);
});

test('Shift+Alt+E expands every star in one undo step', dom, async () => {
  const sql = 'SELECT * FROM orders;\nSELECT * FROM users';
  const ed = mount(sql);
  ed.caret(0);
  ed.press('Í', { shiftKey: true, altKey: true, code: 'KeyE' });
  await ed.wait(30);
  assert.equal(ed.ctl.text, 'SELECT id, total FROM orders;\nSELECT id, name FROM users');
  ed.press('z', { ctrlKey: true });
  assert.equal(ed.ctl.text, sql);
});

test('the corner grip sizes the list and the browser keeps the size', dom, async () => {
  const tables = Array.from({ length: 20 }, (_, i) => 'tab' + i);
  const ed = mount('SELECT * FROM ', 'postgres', { tables, columns: [], dbs: [] });
  ed.caret(14);
  ed.type('t');
  const grip = ed.win.document.querySelector('.qh-ac-ed .qh-ac-grip');
  assert.ok(grip, 'the grip is in the list');
  ed.mouse(grip, 'mousedown', { clientX: 100, clientY: 100 });
  ed.mouse(ed.win, 'mousemove', { clientX: 400, clientY: 300 });
  ed.mouse(ed.win, 'mouseup', { clientX: 400, clientY: 300 });
  const saved = JSON.parse(ed.win.localStorage.getItem('qh.ac.size.v1'));
  assert.ok(saved && saved.w >= 190 && saved.h >= 120, JSON.stringify(saved));
  const list = ed.win.document.querySelector('.qh-ac-ed');
  assert.equal(list.style.maxHeight, saved.h + 'px');
  assert.equal(document.activeElement, ed.ta, 'the editor keeps the focus');
});

test('the grip CSS and the shortcut rows', () => {
  const html = web('QueryHub.html');
  assert.match(html, /\.qh-ac-grip \{ position: sticky; bottom: 0;/);
  const src = web('qh-editor.jsx');
  for (const k of ['Expand every * of the text or selection', 'The non-aggregated SELECT columns', 'Move 8 rows; drag the corner to resize']) {
    assert.ok(src.includes(k), k);
  }
});
