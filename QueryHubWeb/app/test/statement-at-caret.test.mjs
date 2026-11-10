// The statement at the caret (2026-11-10).
//
//   * F8 with no selection runs the statement at the caret and selects it first.
//   * Shift+Alt+S selects the statement at the caret and runs nothing.
//   * Only a top-level `;` splits. A `;` in a string, a quoted name, [brackets],
//     a $$ body or a comment is data.
//   * A caret after a `;` on the same line belongs to the statement before it.
//   * A caret in a blank or comment-only part gives no statement.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';
import { page, jsdomOpts } from './_mount.mjs';

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');
const at = (sql, caret) => W.qhStatementAt(sql, caret);
// The range of `frag` in `sql`, found by text, not by the function under test.
const range = (sql, frag) => {
  const start = sql.indexOf(frag);
  assert.ok(start >= 0, 'fixture: ' + frag);
  return { start, end: start + frag.length };
};

// ---------- the rules ----------

test('a caret in the first, second and third statement', () => {
  const sql = 'select 1; select 2;\nselect 3';
  assert.deepEqual(at(sql, 3), { start: 0, end: 8 });
  assert.deepEqual(at(sql, 13), { start: 10, end: 18 });
  assert.deepEqual(at(sql, 24), { start: 20, end: 28 });
  assert.deepEqual(at(sql, 28), { start: 20, end: 28 }, 'the end of the text');
  assert.deepEqual(at(sql, 10), { start: 10, end: 18 }, 'the first letter of a statement');
  assert.deepEqual(at(sql, 8), { start: 0, end: 8 }, 'just before its own ;');
});

test('a ; inside a string is not a split, also with the doubled quote', () => {
  const a = "select 'a;b' as x; select 2";
  assert.deepEqual(at(a, a.indexOf('a;b') + 2), range(a, "select 'a;b' as x"));
  const b = "select 'it''s;' as x; select 2";
  assert.deepEqual(at(b, b.indexOf("s;'") + 1), range(b, "select 'it''s;' as x"));
  assert.deepEqual(at(b, b.length - 1), range(b, 'select 2'));
});

test('a ; inside a quoted identifier or [brackets] is not a split', () => {
  const q = 'select "a;b" from t; select 2';
  assert.deepEqual(at(q, 2), range(q, 'select "a;b" from t'));
  const s = 'select [a;b] from t; select 2';
  assert.deepEqual(at(s, s.indexOf('a;b') + 1), range(s, 'select [a;b] from t'));
  const e = 'select [x]];y] from t; select 2';
  assert.deepEqual(at(e, 1), range(e, 'select [x]];y] from t'), ']] is an escaped bracket');
});

test('a ; inside a dollar-quoted body is not a split', () => {
  const d = 'do $$ begin perform 1; end $$; select 2';
  assert.deepEqual(at(d, d.indexOf('perform')), range(d, 'do $$ begin perform 1; end $$'));
  const t = 'create function f() returns int as $fn$ select 1; $fn$ language sql; select 2';
  assert.deepEqual(at(t, 3), range(t, 'create function f() returns int as $fn$ select 1; $fn$ language sql'));
  assert.deepEqual(at(t, t.length), range(t, 'select 2'));
});

test('a ; inside a -- comment or a /* */ comment is not a split', () => {
  const l = 'select 1 -- a;b\n, 2; select 3';
  assert.deepEqual(at(l, 2), range(l, 'select 1 -- a;b\n, 2'));
  assert.deepEqual(at(l, l.indexOf(', 2')), range(l, 'select 1 -- a;b\n, 2'));
  const b = 'select /* a;b */ 1; select 2';
  assert.deepEqual(at(b, b.indexOf(' 1;')), range(b, 'select /* a;b */ 1'));
});

test('a caret after a ; on the same line gives the statement that ends there', () => {
  const sql = 'select 1; -- one\nselect 2';
  assert.deepEqual(at(sql, 9), { start: 0, end: 8 }, 'right after the ;');
  assert.deepEqual(at(sql, sql.indexOf('one')), { start: 0, end: 8 }, 'in the comment on that line');
  assert.deepEqual(at(sql, sql.indexOf('select 2') + 3), range(sql, 'select 2'), 'that comment is not part of the next statement');
  const two = 'select 1;   select 2';
  assert.deepEqual(at(two, 10), { start: 0, end: 8 });
  assert.deepEqual(at('select 1;', 9), { start: 0, end: 8 }, 'after the last ;');
});

test('a caret on a blank line or in a comment-only part gives null', () => {
  const sql = 'select 1;\n\nselect 2;';
  assert.equal(at(sql, 10), null, 'the blank line between the statements');
  assert.deepEqual(at(sql, 11), range(sql, 'select 2'));
  assert.equal(at('select 1;\n-- the end', 15), null, 'a comment after the last statement');
  assert.equal(at('select 1;\n\n', 11), null);
});

test('a comment on its own line above a statement runs with it', () => {
  const sql = 'select 1;\n-- second\nselect 2';
  assert.deepEqual(at(sql, sql.indexOf('select 2')), range(sql, '-- second\nselect 2'));
});

test('a text with no ; is one statement, trimmed', () => {
  const sql = '  select 1\n  from t  ';
  const want = range(sql, 'select 1\n  from t');
  assert.deepEqual(at(sql, 0), want, 'the indent of the first line');
  assert.deepEqual(at(sql, sql.indexOf('from')), want);
  assert.deepEqual(at(sql, sql.length), want, 'the blanks after it, on its last line');
  assert.deepEqual(at('select 1', 0), { start: 0, end: 8 });
});

test('empty and blank text give null', () => {
  assert.equal(at('', 0), null);
  assert.equal(at('   \n  ', 3), null);
  assert.equal(at('-- only a note', 4), null);
});

// ---------- the keys, in the real editor ----------

function mount(value) {
  const ui = page(['qh-data.jsx', 'qh-editor.jsx']);
  const sent = [];
  ui.render(ui.React.createElement(ui.win.SqlEditor, {
    value, docId: 't1', fontSize: 13, engineId: 'postgres', onChange: () => {},
    schema: { tables: [], columns: [], dbs: [] }, onRunSelection: (s) => sent.push(s), onRun: () => sent.push('RUN') }));
  const ta = ui.doc.querySelector('textarea');
  const press = (key, mods = {}) => {
    const ev = new ui.win.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...mods });
    ui.React.act(() => ta.dispatchEvent(ev));
    return ev;
  };
  const caret = (a, b = a) => { ta.focus(); ta.setSelectionRange(a, b); };
  return { ta, sent, press, caret };
}

const SCRIPT = 'select 1;\n\nselect 2\n  from t;\nselect 3';

test('F8 with no selection runs the statement at the caret and selects it', jsdomOpts, () => {
  const ed = mount(SCRIPT);
  const want = range(SCRIPT, 'select 2\n  from t');
  ed.caret(SCRIPT.indexOf('from'));
  const ev = ed.press('F8');
  assert.ok(ev.defaultPrevented);
  assert.deepEqual(ed.sent, ['select 2\n  from t'], 'the whole statement, not the line');
  assert.deepEqual([ed.ta.selectionStart, ed.ta.selectionEnd], [want.start, want.end], 'the person sees what runs');
});

test('F8 on a blank line runs nothing', jsdomOpts, () => {
  const ed = mount(SCRIPT);
  ed.caret(10);
  ed.press('F8');
  assert.deepEqual(ed.sent, []);
});

test('F8 with a selection runs the selection as before', jsdomOpts, () => {
  const ed = mount(SCRIPT);
  ed.caret(0, 6);
  ed.press('F8');
  assert.deepEqual(ed.sent, ['select']);
});

test('Shift+Alt+S selects the statement at the caret and runs nothing', jsdomOpts, () => {
  const ed = mount(SCRIPT);
  const want = range(SCRIPT, 'select 3');
  ed.caret(SCRIPT.length - 1);
  // e.key is not S on a Mac with Alt held, so the editor reads e.code.
  const ev = ed.press('Í', { code: 'KeyS', shiftKey: true, altKey: true });
  assert.ok(ev.defaultPrevented);
  assert.deepEqual([ed.ta.selectionStart, ed.ta.selectionEnd], [want.start, want.end]);
  assert.deepEqual(ed.sent, [], 'it runs nothing');
});

test('the shortcut list names both keys', () => {
  const src = W.EditorTabs.toString();
  assert.match(src, /Run selection \/ current statement/);
  assert.match(src, /Select current statement/);
});
