// Parameter info and the Problems tab (2026-11-11, Devart medium batch, PR M4).
//
//   * B2  Inside the parentheses of a call, a box above the caret line shows
//         the parameters. The parameter at the caret is bold. `(` or `,` opens
//         it, Ctrl+Shift+Space too, Escape closes it. Up and Down pick an
//         overload. On SQL Server, `EXEC proc a, b` gets the same box. The
//         signatures come from the catalog and from QH_FUNC_SIGS (built-ins of
//         every engine: PostgreSQL, SQL Server, ClickHouse, Athena).
//   * H3  The results panel has a Problems tab: the pre-flight blockers, then
//         every code analysis finding, with the rule code, the line and the
//         column. A click on a row selects that text in the editor.
//   * B4  The row estimate on the hover card shipped in PR 305
//         (hover-info.test.mjs).
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { bareWindow, loadInto } from './_load.mjs';
import { page, web, jsdomOpts as opts } from './_mount.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');
const at = (sql) => [sql.replace('|', ''), sql.indexOf('|')];
const call = (sql, eng = 'postgres') => {
  const [t, c] = at(sql);
  const r = W.qhCallAt(t, c, eng);
  return r ? (r.qual ? r.qual + '.' : '') + r.name + '#' + r.arg + (r.exec ? ' exec' : '') : null;
};

// ---------- B2: where the caret is ----------

test('the call at the caret and the index of its parameter', () => {
  assert.equal(call('SELECT coalesce(|'), 'coalesce#0');
  assert.equal(call('SELECT coalesce(a, |'), 'coalesce#1');
  assert.equal(call('SELECT coalesce(a, (b + |'), 'coalesce#1', 'a grouping paren passes the search outward');
  assert.equal(call('SELECT coalesce((SELECT max(x) FROM t), |'), 'coalesce#1', 'a closed subquery is one argument');
  assert.equal(call('SELECT f(a), g(b, |'), 'g#1');
  assert.equal(call('SELECT public.my_fn(1, 2, |'), 'public.my_fn#2');
  assert.equal(call('SELECT "My Fn"(|'), 'My Fn#0');
  assert.equal(call('SELECT round(x, 2) | FROM t'), null, 'after the closing paren');
});

test('no call in a string, a comment, a subquery, a list or a window', () => {
  assert.equal(call("SELECT coalesce(a, 'x,|"), null);
  assert.equal(call('SELECT 1 -- f(|'), null);
  assert.equal(call('SELECT /* f(a, | */ 1'), null);
  assert.equal(call('SELECT coalesce((SELECT |'), null);
  assert.equal(call('SELECT array(SELECT id FROM t WHERE x = |'), null, 'a named paren that holds a SELECT');
  assert.equal(call('SELECT * FROM t WHERE id IN (|'), null);
  assert.equal(call('SELECT count(*) OVER (PARTITION BY |'), null);
  assert.equal(call('INSERT INTO t VALUES (|'), null);
});

test('SQL Server EXEC: the procedure and the index of its argument', () => {
  assert.equal(call('EXEC dbo.usp_load |', 'mssql'), 'dbo.usp_load#0 exec');
  assert.equal(call('EXEC dbo.usp_load 1, |', 'mssql'), 'dbo.usp_load#1 exec');
  assert.equal(call('EXECUTE @rc = usp_load 1, 2, |', 'mssql'), 'usp_load#2 exec');
  assert.equal(call('SELECT 1\nEXEC usp_load a, |', 'mssql'), 'usp_load#1 exec', 'a statement with no semicolon before it');
  assert.equal(call('EXEC usp_load 1;\n|', 'mssql'), null, 'the statement ended');
  assert.equal(call('EXEC usp_lo|', 'mssql'), null, 'the caret is on the name');
  assert.equal(call('EXEC usp_load 1, |', 'postgres'), null, 'only SQL Server');
});

// ---------- B2: the signatures ----------

test('an argument list splits at the commas outside parentheses and quotes', () => {
  assert.deepEqual(W.qhSigParams('p_id integer, p_note text DEFAULT \'a,b\'::text, p_n numeric(10,2)'),
    ['p_id integer', "p_note text DEFAULT 'a,b'::text", 'p_n numeric(10,2)']);
  assert.deepEqual(W.qhSigParams(''), []);
  assert.deepEqual(W.qhSigParams(null), []);
});

test('every engine has a built-in list and every line reads', () => {
  for (const eng of ['postgres', 'mssql', 'clickhouse', 'athena']) {
    const sigs = W.qhBuiltinSigs(eng);
    assert.ok(sigs.length >= 70, eng + ': ' + sigs.length);
    for (const s of sigs) {
      assert.match(s.name, /^[A-Za-z_][A-Za-z0-9_]*$/, eng + ': ' + s.name);
      assert.ok(s.ret, eng + ': ' + s.name + ' has a result');
      assert.ok(s.params.every(p => p.length > 0), eng + ': ' + s.name + ' has an empty parameter');
    }
    const lines = W.QH_FUNC_SIGS[eng];
    assert.equal(new Set(lines).size, lines.length, eng + ': a line repeats');
  }
});

test('the catalog comes first, then the built-ins of the engine', () => {
  const schema = { functionSigs: {
    coalesce: [{ s: 'util', n: 'coalesce', k: 'function', args: 'a text', ret: 'text' }],
    usp_load: [{ s: 'dbo', n: 'usp_load', k: 'procedure', args: '@id int, @day date', ret: '' },
               { s: 'etl', n: 'usp_load', k: 'procedure', args: '@batch int', ret: '' }],
  } };
  const pg = W.qhSignaturesFor({ name: 'COALESCE', arg: 0 }, schema, 'postgres');
  assert.deepEqual(pg.map(s => s.src + ':' + s.name), ['catalog:util.coalesce', 'builtin:coalesce']);
  const q = W.qhSignaturesFor({ name: 'usp_load', qual: 'ETL', arg: 0, exec: true }, schema, 'mssql');
  assert.deepEqual(q.map(s => s.name + '(' + s.params.join(', ') + ')'), ['etl.usp_load(@batch int)'], 'the schema narrows it');
  assert.deepEqual(W.qhSignaturesFor({ name: 'isnull', arg: 0 }, {}, 'mssql').map(s => s.name), ['ISNULL'], 'case does not matter');
  assert.deepEqual(W.qhSignaturesFor({ name: 'isnull', arg: 0 }, {}, 'postgres'), [], 'not a PostgreSQL built-in');
  assert.deepEqual(W.qhSignaturesFor({ name: 'coalesce', arg: 0, exec: true }, schema, 'mssql'), [], 'EXEC takes procedures only');
  assert.deepEqual(W.qhSignaturesFor({ name: 'toDate', arg: 0 }, {}, 'clickhouse').map(s => s.params.join(', ')), ['expr']);
  assert.deepEqual(W.qhSignaturesFor({ name: 'date_add', arg: 0 }, {}, 'athena').map(s => s.params.length), [3]);
});

test('the first signature with the parameter is picked, and ... takes any number', () => {
  const sub = W.qhSignaturesFor({ name: 'substring', arg: 2 }, {}, 'postgres');
  assert.equal(sub.length, 3);
  assert.equal(W.qhSigPick(sub, 2), 2, 'substring(text, start, count)');
  assert.equal(W.qhSigPick(sub, 0), 0);
  assert.equal(W.qhSigPick(sub, 9), 0, 'none fits: the first one');
  const co = W.qhSignaturesFor({ name: 'coalesce', arg: 0 }, {}, 'postgres')[0];
  assert.equal(W.qhSigActive(co, 1), 1);
  assert.equal(W.qhSigActive(co, 7), 2, 'past the end: the ... stays bold');
  assert.equal(W.qhSigActive({ params: ['x'] }, 3), -1, 'past the end of a fixed list: none');
  assert.equal(W.qhSigActive({ params: [] }, 0), -1);
});

// ---------- B2: the real editor ----------

function mount(initial, eng = 'postgres', schema = { tables: [], columns: [], dbs: [] }) {
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
  const docId = 'm4-' + Math.random();
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
    caret(i) { React.act(() => { ta.focus(); ta.setSelectionRange(i, i); }); },
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
      React.act(() => ta.dispatchEvent(new win.KeyboardEvent('keyup', { key, bubbles: true, ...mods })));
      return ev;
    },
    click() { React.act(() => ta.dispatchEvent(new win.MouseEvent('click', { bubbles: true }))); },
    box() { return win.document.querySelector('.qh-sig'); },
    // A boolean, never the node: a failed assert prints its actual value, and
    // printing a JSDOM node walks the whole page (it ran out of memory once).
    open() { return !!win.document.querySelector('.qh-sig'); },
    bold() { const b = win.document.querySelector('.qh-sig .qh-sig-cur'); return b ? b.textContent : null; },
  };
}
const dom = JSDOM ? {} : { skip: 'jsdom not installed' };

test('typing ( opens the box, a comma moves the bold parameter, Escape closes it', dom, () => {
  const ed = mount('SELECT ');
  ed.caret(7);
  ed.type('coalesce');
  assert.equal(ed.open(), false, 'no box before the paren');
  ed.type('(');
  assert.ok(ed.open(), 'the box opens');
  assert.match(ed.box().textContent, /^coalesce\(value1, value2, \.\.\.\) → first non-null value$/);
  assert.equal(ed.bold(), 'value1');
  ed.type('a,');
  assert.equal(ed.bold(), 'value2');
  ed.type(' b, c,');
  assert.equal(ed.bold(), '...');
  const esc = ed.press('Escape');
  assert.ok(esc.defaultPrevented);
  assert.equal(ed.open(), false, 'Escape closes it');
  ed.press(' ', { ctrlKey: true, shiftKey: true, code: 'Space' });
  assert.ok(ed.open(), 'Ctrl+Shift+Space opens it again');
  ed.type(')');
  assert.equal(ed.open(), false, 'the closing paren ends the call');
});

test('a click into a call does not open the box; a call that was open follows the caret', dom, () => {
  const sql = 'SELECT nullif(a, b), length(c)';
  const ed = mount(sql);
  ed.caret(15);
  ed.click();
  assert.equal(ed.open(), false, 'a click alone opens nothing');
  ed.press(' ', { ctrlKey: true, shiftKey: true, code: 'Space' });
  assert.equal(ed.bold(), 'value1');
  ed.caret(17);
  ed.press('ArrowRight');
  assert.equal(ed.bold(), 'value2', 'the caret moved to the second argument');
  ed.caret(29);
  ed.click();
  assert.equal(ed.open(), false, 'another call was not opened');
});

test('Up and Down pick the overload and keep the caret; one signature leaves the arrows alone', dom, () => {
  const ed = mount('SELECT ');
  ed.caret(7);
  ed.type('substring(');
  assert.match(ed.box().textContent, /^‹1 of 3›substring\(text FROM start FOR count\)/);
  const down = ed.press('ArrowDown');
  assert.ok(down.defaultPrevented, 'the caret stays');
  assert.match(ed.box().textContent, /^‹2 of 3›substring\(text, start\)/);
  ed.type('x');
  assert.match(ed.box().textContent, /^‹2 of 3›/, 'typing keeps the picked overload');
  ed.press('ArrowUp');
  ed.press('ArrowUp');
  assert.match(ed.box().textContent, /^‹3 of 3›substring\(text, start, count\)/);
  ed.type('x, 1, ');
  assert.match(ed.box().textContent, /^‹3 of 3›/, 'a picked overload stays');
  const ed2 = mount('SELECT ');
  ed2.caret(7);
  ed2.type('length(');
  const up = ed2.press('ArrowUp');
  assert.ok(!up.defaultPrevented, 'one signature: the arrow moves the caret');
});

test('the catalog signature on SQL Server EXEC, and no box for an unknown function', dom, () => {
  const schema = { tables: [], columns: [], dbs: [],
    functionSigs: { usp_load: [{ s: 'dbo', n: 'usp_load', k: 'procedure', args: '@id int, @day date', ret: '' }] } };
  const ed = mount('EXEC dbo.usp_load 1', 'mssql', schema);
  ed.caret(19);
  ed.type(',');
  assert.equal(ed.box().textContent, 'dbo.usp_load @id int, @day dateprocedure');
  assert.equal(ed.bold(), '@day date');
  const ed2 = mount('SELECT ');
  ed2.caret(7);
  ed2.type('no_such_fn(');
  assert.equal(ed2.open(), false);
});

test('the shortcut row and the CSS of the box', () => {
  assert.ok(web('qh-editor.jsx').includes("['Ctrl + ⇧ + Space', 'Parameter info of the function at the caret']"));
  const html = web('QueryHub.html');
  assert.match(html, /\.qh-sig \{ position: fixed; z-index: 97;/);
  assert.match(html, /\.qh-sig-cur \{ color: var\(--fg-primary\); font-weight: 700;/);
});

test('the app gives the editor the catalog signatures by lower-case name', () => {
  const src = web('qh-app.jsx');
  assert.ok(src.includes('(functionSigs[k] = functionSigs[k] || []).push({ s: f.s || null, n: f.n, k: f.k, args:'));
  assert.ok(/qualify, systemTables, functions, functionKind, functionSigs,/.test(src));
});

// ---------- H3: the findings list ----------

test('findings keep one row per rule hit; the marks still join overlaps', () => {
  const sql = "SELECT * FROM t WHERE name = NULL AND note LIKE '%x'";
  const f = W.qhLintFindings(sql, 'postgres');
  assert.ok(f.length >= 2, JSON.stringify(f.map(x => x.id)));
  for (const x of f) {
    assert.match(x.id, /^QA\d{3}$/);
    assert.ok(x.title && x.text);
    assert.equal(x.message, x.title + ' (' + x.id + ')\n' + x.text);
  }
  const marks = W.qhLint(sql, 'postgres');
  for (const m of marks) assert.deepEqual(Object.keys(m).sort(), ['end', 'level', 'message', 'start']);
  assert.ok(marks.length <= f.length);
});

test('the rows: blockers first as errors, then findings with line and column', () => {
  const sql = 'SELECT 1;\nSELECT *\n  FROM t WHERE x = NULL';
  const rows = W.qhProblems(sql, W.qhLintFindings(sql, 'postgres'),
    [{ message: 'DROP is not allowed', start: 0, end: 6 }, { message: 'Too long' }]);
  assert.equal(rows[0].level, 'error');
  assert.equal(rows[0].code, 'Blocked');
  assert.deepEqual([rows[0].line, rows[0].col], [1, 1]);
  assert.deepEqual([rows[1].start, rows[1].line, rows[1].col], [null, null, null], 'a blocker with no place');
  const nul = rows.find(r => r.code === 'QA101');
  assert.ok(nul, JSON.stringify(rows.map(r => r.code)));
  assert.deepEqual([nul.line, nul.col], [3, 18]);
  assert.equal(sql.slice(nul.start, nul.end), '= NULL');
  assert.deepEqual(W.qhProblems('', [], []), []);
});

const PANEL_FILES = ['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-results-tools.jsx', 'qh-panels.jsx'];
const panelBase = (extra) => ({ tab: 'problems', setTab: () => {}, result: null, messages: [], audit: [], runMs: null,
  onExport: () => {}, plan: null, onToast: () => {}, colMeta: {}, reqId: null, unmasked: false, conn: null,
  onStatement: () => {}, ranOn: null, status: 'idle', ...extra });

test('the Problems tab counts the rows, and a click selects the text of the row', opts, () => {
  const ui = page(PANEL_FILES);
  const picked = [];
  const problems = [
    { level: 'error', code: 'Blocked', title: 'The pre-flight check blocks this statement', text: 'No place', start: null, end: null, line: null, col: null },
    { level: 'warn', code: 'QA101', title: 'Comparison with NULL', text: 'Use IS NULL.', start: 30, end: 36, line: 3, col: 18 },
  ];
  ui.render(ui.React.createElement(ui.win.ResultsPanel, panelBase({ problems, onProblem: (p) => picked.push(p), lintOn: true })));
  const tab = ui.all('.qh-res-tab').find(b => b.textContent.startsWith('Problems'));
  assert.ok(tab, 'the tab is there');
  assert.equal(tab.querySelector('.qh-res-count').textContent, '2');
  assert.ok(tab.querySelector('.qh-res-count').classList.contains('is-err'));
  const rows = ui.all('.qh-probs-t tbody tr');
  assert.deepEqual(rows.map(r => [...r.children].map(td => td.textContent)), [
    ['Error', 'Blocked', 'The pre-flight check blocks this statement — No place', '', ''],
    ['Warning', 'QA101', 'Comparison with NULL — Use IS NULL.', '3', '18'],
  ]);
  ui.click(rows[0]);
  assert.equal(picked.length, 0, 'a row with no place selects nothing');
  assert.ok(!rows[0].classList.contains('is-go'));
  ui.click(rows[1]);
  assert.equal(picked.length, 1);
  assert.equal(picked[0].code, 'QA101');
});

test('no problems: an empty list, and a note when code analysis is off', opts, () => {
  const ui = page(PANEL_FILES);
  ui.render(ui.React.createElement(ui.win.ResultsPanel, panelBase({ problems: [], lintOn: false })));
  assert.match(ui.doc.querySelector('.qh-res-body').textContent, /No problems in this tab\.Code analysis is off\./);
  const tab = ui.all('.qh-res-tab').find(b => b.textContent.startsWith('Problems'));
  assert.equal(!!tab.querySelector('.qh-res-count'), false);
  ui.render(ui.React.createElement(ui.win.ResultsPanel, panelBase({ problems: [], lintOn: true })));
  assert.ok(!ui.doc.querySelector('.qh-res-body').textContent.includes('Code analysis is off'));
});

test('the app builds the rows and selects the row text in the editor', () => {
  const src = web('qh-app.jsx');
  assert.ok(src.includes('() => (lint ? qhLintFindings(lintText, editorEngine) : [])'));
  assert.ok(src.includes('() => qhProblems(lintText, lintFindings, blockerRows)'));
  assert.ok(src.includes("onProblem={(p) => { if (p && p.start != null) setReveal({ start: p.start, end: p.end, n: Date.now() }); }}"));
  assert.ok(src.includes('problems={problems} lintOn={lint}'));
});
