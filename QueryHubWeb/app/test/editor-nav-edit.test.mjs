// Editor navigation and small edits (2026-11-10, Devart medium batch, PR M1).
//
//   * G2  Alt+PageUp / Alt+PageDown go to the previous or the next statement.
//         Statements without `;` count (qhStatementRanges, qhStatementJump).
//   * G3  Shift+F12 goes to the mate of a bracket, CASE, BEGIN or END (qhPairJump).
//   * G5  The other places of the name under the caret get a tint (qhOccurrences).
//   * G6  The mate of the bracket or keyword at the caret gets a box (qhPairAt).
//   * G10 Alt+Up / Alt+Down move the line or the selected lines (qhMoveLines).
//   * F6  Shift+Alt+; adds the missing semicolons (qhAddSemicolons).
//   * F7  ( ' " [ or ` over a selection wraps it (qhWrapSelection).
// Every engine: PostgreSQL, SQL Server, ClickHouse and Athena. The few
// engine rules are tested by name (GO, BEGIN, IF on SQL Server; DROP INDEX on
// ClickHouse).
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { bareWindow, loadInto } from './_load.mjs';
import { web } from './_mount.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');
const semi = (sql, eng = 'postgres') => { const r = W.qhAddSemicolons(sql, eng); return r ? r.text : null; };

// ---------- F6: missing semicolons ----------

test('a statement on each line gets its semicolon', () => {
  assert.equal(semi('SELECT 1\nSELECT 2'), 'SELECT 1;\nSELECT 2;');
  assert.equal(semi('SELECT a\nFROM t\nWHERE b = 1\nSELECT 2\n'), 'SELECT a\nFROM t\nWHERE b = 1;\nSELECT 2;\n');
  assert.equal(semi('DROP TABLE IF EXISTS x\nCREATE TABLE x (a int)'), 'DROP TABLE IF EXISTS x;\nCREATE TABLE x (a int);');
});

test('nothing to add gives null, and a second run adds nothing', () => {
  assert.equal(W.qhAddSemicolons('SELECT 1;\nSELECT 2;', 'postgres'), null);
  assert.equal(W.qhAddSemicolons('', 'postgres'), null);
  assert.equal(W.qhAddSemicolons('-- only a comment', 'postgres'), null);
  const once = semi('SELECT 1\nSELECT 2\nUPDATE t\nSET a = 1');
  assert.equal(W.qhAddSemicolons(once, 'postgres'), null);
});

test('the semicolon goes after the last word, before a comment', () => {
  assert.equal(semi('SELECT 1 -- one\nSELECT 2 /* two */'), 'SELECT 1; -- one\nSELECT 2; /* two */');
  assert.equal(semi("SELECT 'a;b'\nSELECT 2"), "SELECT 'a;b';\nSELECT 2;", 'a ; in a string is data');
  assert.equal(semi('SELECT $$x;\nSELECT y$$\nSELECT 2'), 'SELECT $$x;\nSELECT y$$;\nSELECT 2;', 'a $$ body is data');
});

test('an unfinished tail gets nothing', () => {
  assert.equal(W.qhAddSemicolons('SELECT *\nFROM', 'postgres'), null);
  assert.equal(W.qhAddSemicolons('SELECT a,', 'postgres'), null);
  assert.equal(semi('SELECT 1\nSELECT * FROM t WHERE'), 'SELECT 1;\nSELECT * FROM t WHERE');
});

test('a statement that continues on the next line is not split', () => {
  const one = (sql, eng = 'postgres') => assert.equal(semi(sql, eng), sql + ';', sql);
  one('INSERT INTO t (a, b)\nSELECT a, b FROM s');
  one('INSERT INTO t\nVALUES (1)');
  one('INSERT INTO t\nWITH x AS (SELECT 1 AS a)\nSELECT a FROM x');
  one('WITH x AS (SELECT 1)\nSELECT * FROM x');
  one('WITH x (a) AS (SELECT 1)\nSELECT a FROM x');
  one('SELECT a FROM t\nUNION ALL\nSELECT a FROM u');
  one('CREATE VIEW v AS\nSELECT 1');
  one('EXPLAIN (ANALYZE, BUFFERS)\nSELECT 1');
  one('UPDATE t\nSET a = 1\nWHERE id = 2');
  one('INSERT INTO t VALUES (1)\nON CONFLICT (id) DO UPDATE\nSET a = 1');
  one('MERGE INTO t USING s ON t.id = s.id\nWHEN MATCHED THEN\nUPDATE SET a = s.a\nWHEN NOT MATCHED THEN\nINSERT (a) VALUES (s.a)');
  one('SELECT * FROM t\nFOR\nUPDATE');
  one('SELECT * FROM t FOR UPDATE');
  one('ALTER TABLE t\nDROP COLUMN c');
  one('ALTER TABLE t\nALTER COLUMN c\nSET DEFAULT 0');
  one('ALTER DEFAULT PRIVILEGES IN SCHEMA s\nGRANT SELECT ON TABLES TO r');
  one('CREATE TRIGGER g AFTER INSERT ON t FOR EACH ROW WHEN (true)\nEXECUTE FUNCTION f()');
  one('CREATE FUNCTION f() RETURNS int LANGUAGE sql\nSET search_path = public\nAS $$ SELECT 1 $$');
  one('GRANT SELECT ON t TO u\nWITH GRANT OPTION');
  one('SELECT * FROM unnest(a)\nWITH ORDINALITY AS u (v, n)');
  one('DECLARE c CURSOR FOR\nSELECT 1');
  one('SELECT a\n  , comment\nFROM t');
  one('SELECT *\nFROM t\nWITH (NOLOCK)', 'mssql');
});

test('FOR UPDATE ends a statement', () => {
  assert.equal(semi('SELECT * FROM t FOR UPDATE\nSELECT 2'), 'SELECT * FROM t FOR UPDATE;\nSELECT 2;');
});

test('a new statement after a finished ALTER still splits', () => {
  assert.equal(semi('ALTER TABLE t\nDROP COLUMN c\nALTER TABLE u\nADD x int'), 'ALTER TABLE t\nDROP COLUMN c;\nALTER TABLE u\nADD x int;');
  assert.equal(semi('ALTER TABLE t ADD x int\nDROP TABLE u'), 'ALTER TABLE t ADD x int;\nDROP TABLE u;');
});

test('SQL Server: GO ends a batch and takes no semicolon', () => {
  assert.equal(semi('SET NOCOUNT ON\nSELECT 1\nGO\nSELECT 2', 'mssql'), 'SET NOCOUNT ON;\nSELECT 1;\nGO\nSELECT 2;');
  assert.equal(semi('SELECT 1\nGO 5\nSELECT 2', 'mssql'), 'SELECT 1;\nGO 5\nSELECT 2;');
  assert.equal(semi('SELECT 1;\nGO', 'mssql'), null);
});

test('SQL Server: the body of an IF stays with its IF', () => {
  assert.equal(semi('IF @x = 1\n  SELECT 1\nELSE\n  SELECT 2\nSELECT 3', 'mssql'), 'IF @x = 1\n  SELECT 1\nELSE\n  SELECT 2;\nSELECT 3;');
  assert.equal(semi('IF @x = 1\n  IF @y = 2\n    SELECT 1', 'mssql'), 'IF @x = 1\n  IF @y = 2\n    SELECT 1;', 'a nested IF');
  assert.equal(semi('IF OBJECT_ID(\'t\') IS NOT NULL\n  DROP TABLE t\nCREATE TABLE t (a int)', 'mssql'),
    'IF OBJECT_ID(\'t\') IS NOT NULL\n  DROP TABLE t;\nCREATE TABLE t (a int);');
  assert.equal(semi('IF @x = 1\nBEGIN\n  SELECT 1\n  SELECT 2\nEND\nSELECT 3', 'mssql'),
    'IF @x = 1\nBEGIN\n  SELECT 1;\n  SELECT 2\nEND;\nSELECT 3;');
});

test('SQL Server: TRY and CATCH blocks, and BEGIN TRAN', () => {
  assert.equal(semi('BEGIN TRY\n  SELECT 1/0\nEND TRY\nBEGIN CATCH\n  SELECT 2\nEND CATCH', 'mssql'),
    'BEGIN TRY\n  SELECT 1/0\nEND TRY\nBEGIN CATCH\n  SELECT 2\nEND CATCH;');
  assert.equal(semi('BEGIN TRAN\nUPDATE t SET a = 1\nCOMMIT', 'mssql'), 'BEGIN TRAN;\nUPDATE t SET a = 1;\nCOMMIT;');
});

test('BEGIN alone is a transaction on PostgreSQL and a block on SQL Server', () => {
  assert.equal(semi('BEGIN\nUPDATE t SET a = 1\nCOMMIT', 'postgres'), 'BEGIN;\nUPDATE t SET a = 1;\nCOMMIT;');
  assert.equal(semi('BEGIN\nUPDATE t SET a = 1\nEND', 'mssql'), 'BEGIN\nUPDATE t SET a = 1\nEND;');
});

test('ClickHouse: ALTER TABLE mutations and DROP INDEX stay in the ALTER', () => {
  assert.equal(semi('ALTER TABLE t\nDROP INDEX i', 'clickhouse'), 'ALTER TABLE t\nDROP INDEX i;');
  assert.equal(semi('ALTER TABLE t\nDELETE WHERE x = 1', 'clickhouse'), 'ALTER TABLE t\nDELETE WHERE x = 1;');
  assert.equal(semi('SELECT 1\nSETTINGS max_threads = 1\nSELECT 2', 'clickhouse'), 'SELECT 1\nSETTINGS max_threads = 1;\nSELECT 2;');
});

test('Athena: plain statements split as on every engine', () => {
  assert.equal(semi('SHOW TABLES\nSELECT 1', 'athena'), 'SHOW TABLES;\nSELECT 1;');
});

test('the ranges carry the bounds without the semicolon', () => {
  const sql = 'SELECT 1;\n  SELECT 2\nSELECT 3';
  assert.deepEqual(W.qhStatementRanges(sql, 'postgres').map(r => sql.slice(r.start, r.end)), ['SELECT 1', 'SELECT 2', 'SELECT 3']);
  assert.deepEqual(W.qhStatementRanges(sql, 'postgres').map(r => r.semi), [true, false, false]);
});

// ---------- G2: statement jump ----------

test('Alt+PageDown and Alt+PageUp targets', () => {
  const sql = 'SELECT 1\nSELECT 2\n\nSELECT 3; SELECT 4';
  const s2 = sql.indexOf('SELECT 2'), s3 = sql.indexOf('SELECT 3'), s4 = sql.indexOf('SELECT 4');
  const j = (c, d) => W.qhStatementJump(sql, c, d, 'postgres');
  assert.equal(j(0, 1), s2);
  assert.equal(j(s2, 1), s3);
  assert.equal(j(s3 + 3, 1), s4);
  assert.equal(j(s4, 1), null, 'no statement after the last one');
  assert.equal(j(s4 + 4, -1), s4, 'up goes to the start of the own statement first');
  assert.equal(j(s4, -1), s3);
  assert.equal(j(s2, -1), 0);
  assert.equal(j(0, -1), null);
});

// ---------- G3 and G6: pairs ----------

test('brackets and keyword pairs', () => {
  const sql = "SELECT CASE WHEN (a + (b)) > 1 THEN ')' END";
  const ps = W.qhPairs(sql).map(p => [sql.slice(p.a.s, p.a.e), sql.slice(p.b.s, p.b.e), p.a.s, p.b.s]);
  assert.deepEqual(ps.map(p => p[0] + '/' + p[1]), ['(/)', '(/)', 'CASE/END'], 'a ) in a string is data');
  const outer = sql.indexOf('(');
  assert.equal(W.qhPairJump(sql, outer), sql.indexOf(') >'), 'from ( to its )');
  assert.equal(W.qhPairJump(sql, sql.indexOf(') >')), outer, 'and back');
  assert.equal(W.qhPairJump(sql, sql.indexOf('CASE') + 2), sql.indexOf('END'));
  assert.equal(W.qhPairJump(sql, sql.indexOf('END')), sql.indexOf('CASE'));
  assert.equal(W.qhPairJump(sql, sql.indexOf('a +')), sql.indexOf(') >'), 'inside a pair: its closing end');
  assert.equal(W.qhPairJump('SELECT 1', 3), null);
});

test('BEGIN END blocks, TRY and CATCH, and a BEGIN that starts a transaction', () => {
  const t = 'BEGIN TRY\n  SELECT 1\nEND TRY\nBEGIN CATCH\n  BEGIN\n    SELECT 2\n  END\nEND CATCH';
  const ps = W.qhPairs(t).map(p => t.slice(p.a.s, p.a.e) + '/' + t.slice(p.b.s, p.b.e));
  assert.deepEqual(ps.sort(), ['BEGIN CATCH/END CATCH', 'BEGIN TRY/END TRY', 'BEGIN/END'].sort());
  assert.deepEqual(W.qhPairs('BEGIN TRAN\nUPDATE t SET a = 1\nCOMMIT'), []);
  assert.deepEqual(W.qhPairs('BEGIN;\nUPDATE t SET a = 1;\nEND;'), [], 'BEGIN; and END; are a transaction');
  assert.deepEqual(W.qhPairs('SELECT (CASE WHEN a THEN 1) END'), [{ a: { s: 7, e: 8 }, b: { s: 26, e: 27 } }],
    'a ) drops the CASE inside it');
  assert.deepEqual(W.qhPairs('SELECT CASE WHEN x THEN (1 END)'), [{ a: { s: 24, e: 25 }, b: { s: 30, e: 31 } }],
    'an END does not close across a bracket');
});

test('the pair at the caret: inside or touching an end', () => {
  const sql = 'f(a)';
  assert.deepEqual(W.qhPairAt(sql, 2), { self: { s: 1, e: 2 }, mate: { s: 3, e: 4 } }, 'right after (');
  assert.deepEqual(W.qhPairAt(sql, 3).self, { s: 3, e: 4 }, 'right before )');
  assert.equal(W.qhPairAt(sql, 0), null);
});

// ---------- G5: occurrences ----------

test('every place of the name under the caret', () => {
  const sql = 'SELECT u.id FROM users u WHERE u.id = 1';
  const o = (c) => W.qhOccurrences(sql, c).map(r => sql.slice(r.start, r.end) + '@' + r.start);
  assert.deepEqual(o(7), ['u@7', 'u@23', 'u@31']);
  assert.deepEqual(o(sql.indexOf('id') + 2), ['id@9', 'id@33'], 'a caret at the end of the word');
  assert.deepEqual(o(1), [], 'a keyword marks nothing');
  assert.deepEqual(o(sql.indexOf('users')), [], 'a name that occurs once marks nothing');
  assert.deepEqual(o(sql.length), [], 'a number marks nothing');
});

test('quoted names match, and @x stays apart from x', () => {
  const q = 'SELECT "Name", name, [NAME] FROM t';
  assert.equal(W.qhOccurrences(q, q.indexOf('name')).length, 3);
  const v = 'DECLARE @x int; SELECT x, @x FROM t';
  const r = W.qhOccurrences(v, v.indexOf('@x') + 1).map(m => v.slice(m.start, m.end));
  assert.deepEqual(r, ['@x', '@x']);
  assert.deepEqual(W.qhOccurrences(v, v.indexOf(' x,') + 1), [], 'the column x occurs once');
});

test('the caret marks combine pairs and names, sorted', () => {
  const sql = 'SELECT f(a) + a FROM t';
  const m = W.qhCaretMarks(sql, sql.indexOf('a)') + 1);
  assert.deepEqual(m.map(x => x.kind + ':' + sql.slice(x.start, x.end)), ['pair:(', 'occ:a', 'pair:)', 'occ:a']);
});

// ---------- G10: move lines ----------

test('Alt+Up and Alt+Down move a line and keep the caret on it', () => {
  assert.deepEqual(W.qhMoveLines('a\nb\nc', 2, 2, -1), { value: 'b\na\nc', sel: [0, 0] });
  assert.deepEqual(W.qhMoveLines('a\nb\nc', 2, 2, 1), { value: 'a\nc\nb', sel: [4, 4] });
  assert.equal(W.qhMoveLines('a\nb', 0, 0, -1), null, 'the top line');
  assert.equal(W.qhMoveLines('a\nb', 2, 2, 1), null, 'the last line');
  assert.deepEqual(W.qhMoveLines('aa\nbb\ncc', 1, 4, 1), { value: 'cc\naa\nbb', sel: [4, 7] }, 'two selected lines');
  assert.deepEqual(W.qhMoveLines('a\nb\nc', 0, 2, 1), { value: 'b\na\nc', sel: [2, 4] }, 'a selection that ends at a line start leaves that line');
});

// ---------- F7: wrap the selection ----------

test('a bracket or a quote over a selection wraps it', () => {
  assert.deepEqual(W.qhWrapSelection('select abc', 7, 10, '('), { value: 'select (abc)', sel: [8, 11] });
  assert.deepEqual(W.qhWrapSelection('x', 0, 1, "'"), { value: "'x'", sel: [1, 2] });
  assert.deepEqual(W.qhWrapSelection('x', 0, 1, '['), { value: '[x]', sel: [1, 2] });
  assert.equal(W.qhWrapSelection('x', 0, 0, '('), null, 'no selection');
  assert.equal(W.qhWrapSelection('x', 0, 1, 'a'), null, 'another key');
  assert.equal(W.qhWrapSelection('x', 0, 1, 'constructor'), null, 'no prototype key');
});

// ---------- the real editor ----------

function mount(initial, eng = 'postgres') {
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
  const docId = 'm1-' + Math.random();
  function Host() {
    const [v, setV] = React.useState(initial);
    ctl.text = v;
    return React.createElement(win.SqlEditor, { value: v, docId, fontSize: 13, schema: null, engineId: eng, onChange: setV });
  }
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(Host)));
  const ta = win.document.querySelector('textarea');
  return {
    win, ta, ctl,
    get sel() { return [ta.selectionStart, ta.selectionEnd]; },
    caret(i, j = i) { React.act(() => { ta.focus(); ta.setSelectionRange(i, j); }); },
    press(key, mods = {}) {
      const ev = new win.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...mods });
      React.act(() => ta.dispatchEvent(ev));
      return ev;
    },
    wait(ms) { return React.act(() => new Promise(r => setTimeout(r, ms))); },
    up(key = 'ArrowRight') { React.act(() => ta.dispatchEvent(new win.KeyboardEvent('keyup', { key, bubbles: true }))); },
  };
}
const dom = JSDOM ? {} : { skip: 'jsdom not installed' };

test('Alt+Down moves the line, keeps the selection, and one Ctrl+Z undoes it', dom, async () => {
  const ed = mount('SELECT 1\nSELECT 2\nSELECT 3');
  ed.caret(2);
  const ev = ed.press('ArrowDown', { altKey: true });
  assert.equal(ev.defaultPrevented, true);
  assert.equal(ed.ctl.text, 'SELECT 2\nSELECT 1\nSELECT 3');
  await ed.wait(30);
  assert.deepEqual(ed.sel, [11, 11]);
  ed.press('z', { ctrlKey: true });
  assert.equal(ed.ctl.text, 'SELECT 1\nSELECT 2\nSELECT 3');
});

test('Shift+Alt+; adds the semicolons, keeps the caret, and undoes in one step', dom, async () => {
  const ed = mount('SELECT 1\nSELECT 2', 'mssql');
  ed.caret(12);
  const ev = ed.press('Ú', { shiftKey: true, altKey: true, code: 'Semicolon' });
  assert.equal(ev.defaultPrevented, true);
  assert.equal(ed.ctl.text, 'SELECT 1;\nSELECT 2;');
  await ed.wait(30);
  assert.deepEqual(ed.sel, [13, 13], 'the caret moved one on for the ; before it');
  ed.press('z', { ctrlKey: true });
  assert.equal(ed.ctl.text, 'SELECT 1\nSELECT 2');
});

test('Shift+Alt+; on a selection changes only the selection', dom, async () => {
  const ed = mount('SELECT 1\nSELECT 2\nSELECT 3');
  ed.caret(0, 17);
  ed.press(':', { shiftKey: true, altKey: true, code: 'Semicolon' });
  assert.equal(ed.ctl.text, 'SELECT 1;\nSELECT 2;\nSELECT 3');
  await ed.wait(30);
  assert.deepEqual(ed.sel, [0, 19]);
});

test('( over a selection wraps it; ( with no selection types as usual', dom, async () => {
  const ed = mount('SELECT a + b');
  ed.caret(7, 12);
  const ev = ed.press('(');
  assert.equal(ev.defaultPrevented, true);
  assert.equal(ed.ctl.text, 'SELECT (a + b)');
  await ed.wait(30);
  assert.deepEqual(ed.sel, [8, 13]);
  ed.caret(3);
  assert.equal(ed.press('(').defaultPrevented, false, 'the browser types the (');
  ed.caret(0, 6);
  assert.equal(ed.press('(', { ctrlKey: true }).defaultPrevented, false, 'Ctrl+( is not a wrap');
  assert.equal(ed.ctl.text, 'SELECT (a + b)');
  assert.equal(ed.press('[', { ctrlKey: true, altKey: true }).defaultPrevented, true, 'AltGr+[ wraps');
  assert.equal(ed.ctl.text, '[SELECT] (a + b)');
});

test('Alt+PageDown and Shift+F12 move the caret', dom, () => {
  const ed = mount('SELECT (1)\nSELECT 2');
  ed.caret(0);
  assert.equal(ed.press('PageDown', { altKey: true }).defaultPrevented, true);
  assert.deepEqual(ed.sel, [11, 11]);
  ed.press('PageUp', { altKey: true });
  assert.deepEqual(ed.sel, [0, 0]);
  ed.caret(7);
  ed.press('F12', { shiftKey: true });
  assert.deepEqual(ed.sel, [9, 9]);
  ed.caret(9);
  ed.press('|', { ctrlKey: true, shiftKey: true, code: 'Backslash' });
  assert.deepEqual(ed.sel, [7, 7]);
});

test('the caret layer marks the name and the pair, and clears on a selection', dom, async () => {
  const ed = mount('SELECT f(a) + a FROM t');
  ed.caret(10);
  ed.up();
  await ed.wait(160);
  const marks = () => Array.from(ed.win.document.querySelectorAll('.qh-marks mark')).map(m => m.className + ':' + m.textContent);
  assert.deepEqual(marks(), ['qh-pair:(', 'qh-occ:a', 'qh-pair:)', 'qh-occ:a']);
  const layers = Array.from(ed.win.document.querySelectorAll('.qh-code-wrap > pre')).map(p => p.className);
  assert.equal(layers[0], 'qh-pre qh-marks', 'the layer is under the text');
  ed.caret(0, 6);
  ed.up();
  await ed.wait(160);
  assert.deepEqual(marks(), []);
});

// ---------- sources ----------

test('the layer CSS and the shortcut list', () => {
  const html = web('QueryHub.html');
  assert.match(html, /\.qh-marks \{ color: transparent; pointer-events: none; \}/);
  assert.match(html, /\.qh-occ \{ background: var\(--bg-strong\)/, 'a grey fill, not the faint brand tint');
  assert.match(html, /\.qh-pair \{ background: var\(--bg-medium\)/);
  const src = web('qh-editor.jsx');
  for (const k of ['Previous / next statement', 'Move line up / down', 'Add missing semicolons', 'Wrap the selection',
                   'Go to the matching bracket, CASE/END or BEGIN/END']) {
    assert.ok(src.includes(k), k);
  }
});
