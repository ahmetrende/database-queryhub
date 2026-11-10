// Inline blockers: the editor draws a red wavy line under the words that the
// server pre-flight blocks.
//
// The bug class this pins: a blocker message told the user what was wrong but
// not where, so they hunted for the word. Two layers. First the RULES that map
// a message to a range in the SQL (qhBlockerRanges). Then the PLUMBING in the
// real SqlEditor in jsdom: the marks hide while the user types, come back after
// a pause, and show at once after a paste or a new reveal signal.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { bareWindow, loadInto } from './_load.mjs';

const require_ = createRequire(import.meta.url);

// jsdom is a devDependency. Without it, the rules still run.
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }

// ---------------------------------------------------------------------------
// The rules
// ---------------------------------------------------------------------------

const H = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');

const ranges = (sql, ...messages) => H.qhBlockerRanges(sql, messages);
const spans = (sql, ...messages) => ranges(sql, ...messages).map((r) => [r.start, r.end]);

test('rule a: a quoted fragment is found, even in a leading word', () => {
  assert.deepEqual(
    spans('selec 1', "Unrecognized SQL: the leading word 'selec' is not allowed."),
    [[0, 5]]);
});

test('rule a: a backtick fragment is found, case-insensitive', () => {
  const sql = "SET log_statement = 'none'";
  const out = ranges(sql, 'Changing the `LOG_STATEMENT` logging setting is blocked.');
  assert.equal(out.length, 1);
  assert.equal(sql.slice(out[0].start, out[0].end), 'log_statement');
  assert.equal(out[0].message, 'Changing the `LOG_STATEMENT` logging setting is blocked.');
});

test('rule a: a double-quoted fragment is found inside a string literal', () => {
  const sql = "select 'secret_token' from t";
  const out = ranges(sql, 'The value "secret_token" is not allowed.');
  assert.deepEqual(out.map((r) => sql.slice(r.start, r.end)), ['secret_token']);
});

test('rule b: an upper-case keyword matches the word, not the comment', () => {
  const sql = '-- fix\nupdate users set a = 1';
  assert.deepEqual(spans(sql, 'UPDATE without a WHERE clause is blocked.'), [[7, 13]]);
});

test('rule b: a keyword in a comment is never matched', () => {
  const sql = '/* delete */ select 1';
  const out = ranges(sql, 'DELETE without a WHERE clause is blocked.');
  // No match for DELETE: the rule falls through to the first word.
  assert.deepEqual(out.map((r) => sql.slice(r.start, r.end)), ['select']);
});

test('rule b: a two-word phrase covers both words and the gap', () => {
  const sql = 'explain  analyze select 1';
  assert.deepEqual(spans(sql, 'EXPLAIN ANALYZE is not allowed here.'),
    [[0, sql.indexOf('analyze') + 'analyze'.length]]);
});

test('rule b: a keyword inside a string literal is skipped', () => {
  const sql = "select 'delete' as x; delete from t";
  const at = sql.lastIndexOf('delete');
  assert.deepEqual(spans(sql, 'DELETE without a WHERE clause is blocked.'), [[at, at + 6]]);
});

test('rule c: a parse message points at the last word', () => {
  const sql = 'select * from t wher';
  const out = ranges(sql, 'Could not parse the statement.');
  assert.deepEqual(out.map((r) => sql.slice(r.start, r.end)), ['wher']);
  assert.deepEqual(spans(sql, 'Syntax problem near the end.'), [[16, 20]]);
});

test('rule d: any other message points at the first word after a comment', () => {
  const sql = '/* x */ select 1; drop table t';
  const out = ranges(sql, 'Mixed-tier submission rejected.');
  assert.deepEqual(out.map((r) => sql.slice(r.start, r.end)), ['select']);
  assert.equal(out[0].start, 8);
});

test('rule a wins over rule b when both could match', () => {
  const sql = 'select 1; truncate t';
  const out = ranges(sql, "SELECT is fine but 'truncate' is blocked.");
  assert.deepEqual(out.map((r) => sql.slice(r.start, r.end)), ['truncate']);
});

test('rule b takes the keywords in the order the message names them', () => {
  // The list puts WITH before INSERT. The message names INSERT first, and the
  // INSERT is the statement QueryHub refuses.
  const sql = 'with x as (insert into t values (1) returning *) select * from x';
  const out = ranges(sql, 'CTE-embedded INSERT is not allowed inside WITH.');
  assert.deepEqual(out.map((r) => sql.slice(r.start, r.end)), ['insert']);
});

test('two messages on the same word give one range, joined by a newline', () => {
  const a = "The leading word 'selec' is not allowed.";
  const b = "Unrecognized command 'selec'.";
  const out = ranges('selec 1', a, b);
  assert.equal(out.length, 1);
  assert.deepEqual([out[0].start, out[0].end], [0, 5]);
  assert.equal(out[0].message, a + '\n' + b);
});

test('ranges are sorted by start', () => {
  const sql = 'select 1; drop table t';
  const out = H.qhBlockerRanges(sql, [
    'DROP is blocked here.',
    "The word 'select' is not allowed.",
  ]);
  assert.deepEqual(out.map((r) => r.start), [0, 10]);
});

test('empty sql and an empty blocker list give no ranges', () => {
  assert.deepEqual(H.qhBlockerRanges('', ['DROP is blocked.']), []);
  assert.deepEqual(H.qhBlockerRanges('select 1', []), []);
});

// ---------------------------------------------------------------------------
// The plumbing: the real editor, real input events, real timers
// ---------------------------------------------------------------------------

const SCHEMA = { tables: [], columns: [], dbs: [], tableCols: {} };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const MSG = "Unrecognized SQL: the leading word 'selec' is not allowed.";

/** Mount the real SqlEditor with issues computed from the blocker messages. */
function mount(text, messages) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>',
                        { pretendToBeVisual: true, url: 'https://localhost/' });
  for (const k of ['window', 'document', 'navigator', 'HTMLElement', 'Element',
                   'Node', 'Event', 'KeyboardEvent', 'InputEvent', 'getComputedStyle',
                   'requestAnimationFrame', 'cancelAnimationFrame', 'localStorage']) {
    Object.defineProperty(globalThis, k, {
      value: dom.window[k], configurable: true, writable: true,
    });
  }
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;

  const React = require_('react');
  const ReactDOM = require_('react-dom/client');
  const act = React.act;
  globalThis.React = React;
  const win = dom.window;
  win.React = React;
  win.ReactDOM = require_('react-dom');
  loadInto(win, 'qh-data.jsx', 'qh-editor.jsx');
  const { SqlEditor, qhBlockerRanges } = win;
  assert.ok(SqlEditor && qhBlockerRanges, 'qh-editor.jsx did not publish what the test needs');

  // Computed once, like a parent that holds the last server answer.
  const issues = qhBlockerRanges(text, messages);
  const ctl = {};
  function Host() {
    const [value, setValue] = React.useState(text);
    const [signal, setSignal] = React.useState(0);
    ctl.bump = () => setSignal((n) => n + 1);
    return React.createElement(SqlEditor, {
      value, docId: 't1', fontSize: 13, schema: SCHEMA, engineId: 'postgres',
      onChange: setValue, issues, revealSignal: signal,
    });
  }
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  act(() => root.render(React.createElement(Host)));
  const ta = win.document.querySelector('textarea');
  assert.ok(ta, 'the editor rendered no textarea');
  const setValue = Object.getOwnPropertyDescriptor(win.HTMLTextAreaElement.prototype, 'value').set;

  const ed = {
    win, ta, ctl, act, issues,
    marks: () => [...win.document.querySelectorAll('pre.qh-issues mark.qh-issue')],
    status: () => win.document.querySelector('.qh-editor [role="status"]'),
    input(inputType, next, caretAfter, data = null) {
      act(() => {
        const bi = new win.InputEvent('beforeinput', { inputType, data, bubbles: true, cancelable: true });
        ta.dispatchEvent(bi);
        if (bi.defaultPrevented) return;
        setValue.call(ta, next);
        ta.setSelectionRange(caretAfter, caretAfter);
        ta.dispatchEvent(new win.InputEvent('input', { inputType, data, bubbles: true }));
      });
    },
    // Type one char at the end, so the marked word keeps its place.
    typeAtEnd(ch) {
      const v = ta.value;
      ed.input('insertText', v + ch, v.length + 1, ch);
    },
    async wait(ms) { await act(async () => { await sleep(ms); }); },
  };
  return ed;
}

const dom = JSDOM ? {} : { skip: 'jsdom not installed' };

test('the mark and the status text show for a blocked word', dom, () => {
  const ed = mount('selec 1', [MSG]);
  const marks = ed.marks();
  assert.equal(marks.length, 1);
  assert.equal(marks[0].textContent, 'selec');
  const st = ed.status();
  assert.ok(st, 'the status element must always exist');
  assert.ok(st.textContent.includes(MSG), 'the status must hold the message');
});

test('no issues, no marks, and the status element stays empty', dom, () => {
  const ed = mount('select 1', []);
  assert.equal(ed.marks().length, 0);
  assert.ok(ed.status());
  assert.equal(ed.status().textContent, '');
});

test('a typing edit hides the marks at once and they return after a pause', dom, async () => {
  const ed = mount('selec 1', [MSG]);
  assert.equal(ed.marks().length, 1);
  ed.typeAtEnd('2');
  assert.equal(ed.marks().length, 0, 'typing must hide the marks at once');
  assert.equal(ed.status().textContent, '', 'the status must be empty while hidden');
  await ed.wait(500);
  assert.equal(ed.marks().length, 0, 'still hidden before the pause ends');
  await ed.wait(600);
  assert.equal(ed.marks().length, 1, 'the marks come back 1000 ms after the last edit');
  assert.equal(ed.marks()[0].textContent, 'selec');
  assert.ok(ed.status().textContent.includes(MSG));
});

test('a delete edit hides the marks too', dom, () => {
  const ed = mount('selec 1', [MSG]);
  ed.input('deleteContentBackward', 'selec ', 6);
  assert.equal(ed.marks().length, 0);
});

test('each typing edit restarts the pause', dom, async () => {
  const ed = mount('selec 1', [MSG]);
  ed.typeAtEnd('2');
  await ed.wait(700);
  ed.typeAtEnd('3');
  await ed.wait(700);
  assert.equal(ed.marks().length, 0, '1400 ms after the first edit but 700 after the last');
  await ed.wait(500);
  assert.equal(ed.marks().length, 1);
});

test('a paste edit shows the marks without waiting', dom, () => {
  const ed = mount('selec 1', [MSG]);
  ed.typeAtEnd('2');
  assert.equal(ed.marks().length, 0);
  ed.input('insertFromPaste', 'selec 123', 9);
  assert.equal(ed.marks().length, 1, 'a paste must show the marks at once');
  assert.ok(ed.status().textContent.includes(MSG));
});

test('a new revealSignal shows the marks right after a typing edit', dom, () => {
  const ed = mount('selec 1', [MSG]);
  ed.typeAtEnd('2');
  assert.equal(ed.marks().length, 0);
  ed.act(() => ed.ctl.bump());
  assert.equal(ed.marks().length, 1, 'the signal must show the marks at once');
  assert.ok(ed.status().textContent.includes(MSG));
});
