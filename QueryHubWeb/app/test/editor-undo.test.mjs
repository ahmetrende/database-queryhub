// Undo / redo in the SQL editor: every change can be taken back, whoever made it.
//
// The bug this pins: the editor's own edits — expanding `*`, a completion, Tab,
// a dropped tree object, a line cut or paste — were written straight into the
// controlled textarea. Writing the value throws the browser's undo stack away,
// so ⌘Z / Ctrl+Z after expanding `*` did nothing at all. The editor now keeps
// its own history (qhUndoRecord in qh-editor.jsx) and owns the keys.
//
// Two layers, like the selection tests: the grouping RULES through the
// exported functions, then the PLUMBING through the real SqlEditor in jsdom,
// with real key presses and real input events.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { bareWindow, loadInto } from './_load.mjs';

const require_ = createRequire(import.meta.url);

// jsdom is a devDependency; without it the rules still run.
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }

// ---------------------------------------------------------------------------
// The rules
// ---------------------------------------------------------------------------

const H = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');

let seq = 0;
const fresh = (text) => H.qhUndoDoc('rules-' + (seq++), text);

/** Type `s` at `caret` one key at a time, as the browser reports typing. */
function typed(h, s, caret) {
  for (const ch of s) {
    H.qhUndoRecord(h, h.text.slice(0, caret) + ch + h.text.slice(caret),
      { kind: 'type', before: [caret, caret], after: [caret + 1, caret + 1] });
    caret += 1;
  }
  return caret;
}

/** Undo until nothing is left; the texts seen on the way, newest first. */
function unwind(h) {
  const seen = [];
  for (let r = H.qhUndoStep(h, -1); r; r = H.qhUndoStep(h, -1)) seen.push(r.text);
  return seen;
}

test('qhTextDiff finds the one changed span', () => {
  assert.deepEqual(H.qhTextDiff('SELECT  FROM t', 'SELECT * FROM t'), { at: 7, del: '', ins: '*' });
  assert.deepEqual(H.qhTextDiff('SELECT * FROM t', 'SELECT a, b FROM t'), { at: 7, del: '*', ins: 'a, b' });
  assert.deepEqual(H.qhTextDiff('abc', ''), { at: 0, del: 'abc', ins: '' });
  assert.deepEqual(H.qhTextDiff('same', 'same'), { at: 4, del: '', ins: '' });
});

test('an app edit is one step; undo and redo put back the text and the caret', () => {
  const h = fresh('SELECT  FROM orders');
  typed(h, '*', 7);
  H.qhUndoRecord(h, 'SELECT id, name FROM orders', { kind: 'complete', before: [8, 8], after: [15, 15] });
  assert.deepEqual(H.qhUndoStep(h, -1), { text: 'SELECT * FROM orders', sel: [8, 8] });
  assert.deepEqual(H.qhUndoStep(h, -1), { text: 'SELECT  FROM orders', sel: [7, 7] });
  assert.equal(H.qhUndoStep(h, -1), null, 'the starting text is not a step');
  assert.deepEqual(H.qhUndoStep(h, 1), { text: 'SELECT * FROM orders', sel: [8, 8] });
  assert.deepEqual(H.qhUndoStep(h, 1), { text: 'SELECT id, name FROM orders', sel: [15, 15] });
  assert.equal(H.qhUndoStep(h, 1), null);
});

test('typing merges by word, the way VS Code groups it', () => {
  const h = fresh('');
  typed(h, 'SELECT * FROM orders', 0);
  assert.deepEqual(unwind(h), ['SELECT * FROM', 'SELECT *', 'SELECT', '']);
});

test('moving the caret between keys starts a new step', () => {
  const h = fresh('');
  typed(h, 'ab', 0);
  typed(h, 'c', 0);            // clicked back to the start, typed again
  assert.deepEqual(unwind(h), ['ab', '']);
});

test('typing and deleting are separate steps; a run of deletes is one', () => {
  const h = fresh('');
  let caret = typed(h, 'abcd', 0);
  for (let i = 0; i < 2; i++) {
    H.qhUndoRecord(h, h.text.slice(0, caret - 1) + h.text.slice(caret),
      { kind: 'back', before: [caret, caret], after: [caret - 1, caret - 1] });
    caret -= 1;
  }
  assert.equal(h.text, 'ab');
  assert.deepEqual(unwind(h), ['abcd', '']);
});

test('typing over a selection starts a new step', () => {
  const h = fresh('');
  typed(h, 'abc', 0);
  H.qhUndoRecord(h, 'aXc', { kind: 'type', before: [1, 2], after: [2, 2] });
  assert.deepEqual(unwind(h), ['abc', '']);
});

test('Enter, a paste and a word delete never merge, even when adjacent', () => {
  assert.equal(H.qhEditKind('insertText'), 'type');
  assert.equal(H.qhEditKind('deleteContentBackward'), 'back');
  assert.equal(H.qhEditKind('deleteContentForward'), 'fwd');
  assert.equal(H.qhEditKind('insertCompositionText'), 'compose');
  assert.equal(H.qhEditKind('insertFromPaste'), 'insertFromPaste');
  assert.equal(H.qhEditKind(undefined), 'edit');
  const h = fresh('');
  for (const [next, kind] of [['a', 'insertLineBreak'], ['ab', 'insertLineBreak'],
                              ['abc', 'insertFromPaste'], ['abcd', 'insertFromPaste']]) {
    const n = h.text.length;
    H.qhUndoRecord(h, next, { kind, before: [n, n], after: [n + 1, n + 1] });
  }
  assert.deepEqual(unwind(h), ['abc', 'ab', 'a', '']);
});

test('a composition is one step, and compositionstart closes the one before', () => {
  const h = fresh('');
  H.qhUndoRecord(h, '^', { kind: 'compose' });
  H.qhUndoRecord(h, 'â', { kind: 'compose' });
  H.qhUndoSeal(h);             // what the compositionstart listener does
  H.qhUndoRecord(h, 'â^', { kind: 'compose' });
  H.qhUndoRecord(h, 'âê', { kind: 'compose' });
  assert.deepEqual(unwind(h), ['â', '']);
});

test('a new edit after an undo clears redo', () => {
  const h = fresh('');
  typed(h, 'abc', 0);
  H.qhUndoStep(h, -1);
  typed(h, 'x', 0);
  assert.equal(H.qhUndoStep(h, 1), null);
});

test('the history keeps the last 500 steps of a tab', () => {
  const h = fresh('');
  for (let i = 1; i <= 520; i++) H.qhUndoRecord(h, 'v' + i, { kind: 'edit' });
  const seen = unwind(h);
  assert.equal(seen.length, 500);
  assert.equal(seen[seen.length - 1], 'v20', 'the oldest twenty steps were dropped');
});

test('histories are per tab; past 64 tabs the least recently used one goes', () => {
  const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');
  const a = W.qhUndoDoc('a', '');
  W.qhUndoRecord(a, 'x', { kind: 'edit' });
  const b = W.qhUndoDoc('b', '');
  W.qhUndoRecord(b, 'y', { kind: 'edit' });
  assert.equal(W.qhUndoDoc('a', 'ignored').text, 'x', 'a tab finds its own history again');
  for (let i = 0; i < 63; i++) W.qhUndoDoc('other-' + i, '');
  assert.equal(W.qhUndoDoc('a', 'ignored').text, 'x', 'touched last, so it stays');
  assert.equal(W.qhUndoDoc('b', 'fresh').text, 'fresh', 'least recently used, so it went');
});

test('qhUndoForget drops every history', () => {
  const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');
  W.qhUndoRecord(W.qhUndoDoc('a', ''), 'secret', { kind: 'edit' });
  W.qhUndoForget();
  const h = W.qhUndoDoc('a', 'next user');
  assert.equal(h.text, 'next user');
  assert.equal(W.qhUndoStep(h, -1), null);
});

// ---------------------------------------------------------------------------
// The plumbing: the real editor, real keys, real input events
// ---------------------------------------------------------------------------

const SCHEMA = {
  tables: ['orders'], columns: ['id', 'name', 'total'], dbs: [],
  tableCols: { orders: ['id', 'name', 'total'] },
};

/** Mount the real SqlEditor under a parent shaped like qh-app: one text per tab. */
function mount(docs, active) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>',
                        { pretendToBeVisual: true, url: 'https://localhost/' });
  // defineProperty, not assignment — see editor-selection.test.mjs.
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
  win.ReactDOM = require_('react-dom');       // the completion list is portalled
  loadInto(win, 'qh-data.jsx', 'qh-editor.jsx');
  const { SqlEditor } = win;
  assert.ok(SqlEditor, 'qh-editor.jsx did not publish SqlEditor');

  const ctl = {};
  function Host() {
    const [texts, setTexts] = React.useState(docs);
    const [cur, setCur] = React.useState(active);
    ctl.texts = texts;
    ctl.switchTo = (id) => setCur(id);
    ctl.replace = (text) => setTexts((d) => ({ ...d, [cur]: text }));
    return React.createElement(SqlEditor, {
      value: texts[cur], docId: cur, fontSize: 13, schema: SCHEMA, engineId: 'postgres',
      onChange: (v) => setTexts((d) => ({ ...d, [cur]: v })),
    });
  }
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  act(() => root.render(React.createElement(Host)));
  const ta = win.document.querySelector('textarea');
  assert.ok(ta, 'the editor rendered no textarea');
  const setValue = Object.getOwnPropertyDescriptor(win.HTMLTextAreaElement.prototype, 'value').set;

  const ed = {
    win, ta, ctl,
    get text() { return ta.value; },
    get sel() { return [ta.selectionStart, ta.selectionEnd]; },
    caret(i, j = i) { ta.focus(); ta.setSelectionRange(i, j); },
    // One edit the way a browser delivers it: beforeinput (selection still the
    // old one), then the text changes, then input. The native setter bypasses
    // React's value tracker, as a real keystroke does.
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
    type(s) {
      for (const ch of s) {
        const [a, b] = ed.sel, v = ta.value;
        ed.input('insertText', v.slice(0, a) + ch + v.slice(b), a + 1, ch);
      }
    },
    backspace() {
      const [a, b] = ed.sel, v = ta.value;
      if (a !== b) ed.input('deleteContentBackward', v.slice(0, a) + v.slice(b), a);
      else ed.input('deleteContentBackward', v.slice(0, a - 1) + v.slice(a), a - 1);
    },
    press(key, mods = {}) {
      const ev = new win.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...mods });
      act(() => ta.dispatchEvent(ev));
      return ev;
    },
    undo() { return ed.press('z', { metaKey: true }); },
    redo() { return ed.press('z', { metaKey: true, shiftKey: true }); },
    popup() { return win.document.querySelector('.qh-ac-ed'); },
    act,
  };
  return ed;
}

const dom = JSDOM ? {} : { skip: 'jsdom not installed' };

test('the report: * expanded with Tab, then ⌘Z gives the * back', dom, () => {
  const ed = mount({ t1: 'SELECT  FROM orders' }, 't1');
  ed.caret(7);
  ed.type('*');
  assert.ok(ed.popup() && /Expand \* → 3 columns/.test(ed.popup().textContent),
    'typing * after SELECT should offer the expansion');
  ed.press('Tab');
  assert.equal(ed.text, 'SELECT id, name, total FROM orders');

  const ev = ed.undo();
  assert.ok(ev.defaultPrevented, 'the editor must own ⌘Z');
  assert.equal(ed.text, 'SELECT * FROM orders');
  assert.deepEqual(ed.sel, [8, 8], 'the caret goes back behind the *');
  assert.equal(ed.ctl.texts.t1, ed.text, 'the parent holds what the editor shows');

  ed.undo();
  assert.equal(ed.text, 'SELECT  FROM orders');
  assert.deepEqual(ed.sel, [7, 7]);

  ed.redo();
  assert.equal(ed.text, 'SELECT * FROM orders');
  ed.redo();
  assert.equal(ed.text, 'SELECT id, name, total FROM orders');
  assert.deepEqual(ed.sel, [22, 22]);
});

test('Ctrl+Z undoes, and Ctrl+Shift+Z and Ctrl+Y redo, off the Mac', dom, () => {
  const ed = mount({ t1: '' }, 't1');
  ed.caret(0);
  ed.type('SELECT 1');
  assert.ok(ed.press('z', { ctrlKey: true }).defaultPrevented);
  assert.equal(ed.text, 'SELECT');
  ed.press('z', { ctrlKey: true });
  assert.equal(ed.text, '');
  ed.press('y', { ctrlKey: true });
  assert.equal(ed.text, 'SELECT');
  ed.press('Z', { ctrlKey: true, shiftKey: true });
  assert.equal(ed.text, 'SELECT 1');
});

test('⌘Z with nothing to undo is still the editor\'s, and changes nothing', dom, () => {
  const ed = mount({ t1: 'SELECT 1' }, 't1');
  assert.ok(ed.undo().defaultPrevented);
  assert.equal(ed.text, 'SELECT 1');
  assert.ok(ed.redo().defaultPrevented);
  assert.equal(ed.text, 'SELECT 1');
});

test('Alt combinations are not undo: AltGr and the wrap toggle pass through', dom, () => {
  const ed = mount({ t1: '' }, 't1');
  ed.caret(0);
  ed.type('x');
  assert.ok(!ed.press('z', { ctrlKey: true, altKey: true }).defaultPrevented);
  assert.ok(!ed.press('z', { altKey: true }).defaultPrevented);
  assert.equal(ed.text, 'x');
});

test('Tab indentation undoes', dom, () => {
  const ed = mount({ t1: 'SELECT 1' }, 't1');
  ed.caret(0);
  ed.press('Tab');
  assert.equal(ed.text, '  SELECT 1');
  ed.undo();
  assert.equal(ed.text, 'SELECT 1');
  assert.deepEqual(ed.sel, [0, 0]);
});

test('a completion undoes to what was typed', dom, () => {
  const ed = mount({ t1: 'SELECT * FROM ' }, 't1');
  ed.caret(14);
  ed.type('ord');
  assert.ok(ed.popup(), 'typing a table prefix should open the list');
  ed.press('Enter');
  assert.equal(ed.text, 'SELECT * FROM orders');
  ed.undo();
  assert.equal(ed.text, 'SELECT * FROM ord');
  ed.undo();
  assert.equal(ed.text, 'SELECT * FROM ');
});

test('a whole-line cut and its paste each undo', dom, () => {
  const ed = mount({ t1: 'SELECT 1;\nSELECT 2;' }, 't1');
  ed.caret(3);
  ed.press('x', { ctrlKey: true });
  assert.equal(ed.text, 'SELECT 2;');
  ed.undo();
  assert.equal(ed.text, 'SELECT 1;\nSELECT 2;');
  assert.deepEqual(ed.sel, [3, 3]);

  // The line-shaped paste of the same text.
  ed.press('x', { ctrlKey: true });
  ed.caret(0);
  const paste = new ed.win.Event('paste', { bubbles: true, cancelable: true });
  Object.defineProperty(paste, 'clipboardData', { value: { getData: () => 'SELECT 1;\n' } });
  ed.act(() => ed.ta.dispatchEvent(paste));
  assert.equal(ed.text, 'SELECT 1;\nSELECT 2;');
  ed.undo();
  assert.equal(ed.text, 'SELECT 2;');
});

test('a dropped tree object undoes', dom, () => {
  const ed = mount({ t1: 'SELECT 1' }, 't1');
  const drop = new ed.win.Event('drop', { bubbles: true, cancelable: true });
  Object.defineProperty(drop, 'dataTransfer', { value: { getData: () => 'orders ', types: ['text/plain'] } });
  Object.defineProperty(drop, 'clientX', { value: 0 });
  Object.defineProperty(drop, 'clientY', { value: 0 });
  ed.act(() => ed.ta.dispatchEvent(drop));
  assert.equal(ed.text, 'orders SELECT 1');
  ed.undo();
  assert.equal(ed.text, 'SELECT 1');
});

test('typing, paste and delete through the browser undo too', dom, () => {
  const ed = mount({ t1: '' }, 't1');
  ed.caret(0);
  ed.type('SELECT ');
  ed.input('insertFromPaste', 'SELECT name FROM t', 18);
  ed.backspace();
  assert.equal(ed.text, 'SELECT name FROM ');
  ed.undo();
  assert.equal(ed.text, 'SELECT name FROM t');
  ed.undo();
  assert.equal(ed.text, 'SELECT ');
  ed.undo();
  assert.equal(ed.text, 'SELECT');
});

test('text the app puts in from outside undoes like any other edit', dom, () => {
  const ed = mount({ t1: 'SELECT 1' }, 't1');
  ed.act(() => ed.ctl.replace('SELECT 42'));
  assert.equal(ed.text, 'SELECT 42');
  ed.undo();
  assert.equal(ed.text, 'SELECT 1');
  ed.redo();
  assert.equal(ed.text, 'SELECT 42');
});

test('each tab keeps its own history across switches', dom, () => {
  const ed = mount({ a: 'A', b: 'B' }, 'a');
  ed.caret(1);
  ed.type('x');
  ed.act(() => ed.ctl.switchTo('b'));
  assert.equal(ed.text, 'B');
  ed.undo();
  assert.equal(ed.text, 'B', 'tab b has nothing to undo; tab a\'s step must not leak into it');
  ed.caret(1);
  ed.type('y');
  ed.act(() => ed.ctl.switchTo('a'));
  assert.equal(ed.text, 'Ax');
  ed.undo();
  assert.equal(ed.text, 'A');
  ed.act(() => ed.ctl.switchTo('b'));
  ed.undo();
  assert.equal(ed.text, 'B');
  assert.deepEqual(ed.ctl.texts, { a: 'A', b: 'B' });
});

test('the browser\'s own Undo (menu, context menu) is answered with ours', dom, () => {
  const ed = mount({ t1: 'SELECT  FROM orders' }, 't1');
  ed.caret(7);
  ed.type('*');
  ed.press('Tab');
  const bi = new ed.win.InputEvent('beforeinput', { inputType: 'historyUndo', bubbles: true, cancelable: true });
  ed.act(() => ed.ta.dispatchEvent(bi));
  assert.ok(bi.defaultPrevented, 'the browser must not run its own (empty) undo');
  assert.equal(ed.text, 'SELECT * FROM orders');
});

test('a browser undo that could not be cancelled is overruled by ours', dom, () => {
  // No beforeinput first: a browser that only reports the input. Its own result
  // is thrown away and the step comes from our history.
  const ed = mount({ t1: '' }, 't1');
  ed.caret(0);
  ed.type('ab');
  const setValue = Object.getOwnPropertyDescriptor(ed.win.HTMLTextAreaElement.prototype, 'value').set;
  ed.act(() => {
    setValue.call(ed.ta, 'something else');
    ed.ta.dispatchEvent(new ed.win.InputEvent('input', { inputType: 'historyUndo', bubbles: true }));
  });
  assert.equal(ed.text, '');
  assert.equal(ed.ctl.texts.t1, '');
});

test('sign-out forgets the history', dom, () => {
  const ed = mount({ t1: '' }, 't1');
  ed.caret(0);
  ed.type('SELECT secret');
  ed.win.qhUndoForget();
  ed.undo();
  assert.equal(ed.text, 'SELECT secret', 'nothing of the previous history may come back');
});
