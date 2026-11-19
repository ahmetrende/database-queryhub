// Line indent, outdent and line comments in the classic editor.
//
//   * Tab on a selection of more than one line, or of one whole line, indents
//     every line by two spaces (qhSelectionIsBlock, qhIndentLines).
//   * Shift+Tab removes one indent from every touched line: a leading tab or
//     up to two spaces (qhOutdentLines).
//   * Ctrl+/ (Cmd+/ on macOS) adds or removes `-- ` on the touched lines
//     (qhToggleLineComment).
// Each one is one undo step, and the selection stays on the same lines.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { bareWindow, loadInto } from './_load.mjs';
import { web } from './_mount.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');
const r3 = (r) => [r.text, r.selStart, r.selEnd];

// ---------- which selection indents lines ----------

test('a block selection spans lines or covers one whole line', () => {
  assert.equal(W.qhSelectionIsBlock('ab\ncd', 0, 0), false, 'no selection');
  assert.equal(W.qhSelectionIsBlock('ab\ncd', 0, 1), false, 'part of a line');
  assert.equal(W.qhSelectionIsBlock('ab\ncd', 0, 2), true, 'the whole first line');
  assert.equal(W.qhSelectionIsBlock('ab\ncd', 3, 5), true, 'the whole last line');
  assert.equal(W.qhSelectionIsBlock('ab\ncd', 1, 4), true, 'two lines');
});

// ---------- indent ----------

test('Tab indents every selected line and keeps the selection on them', () => {
  assert.deepEqual(r3(W.qhIndentLines('a\nb\nc', 0, 3)), ['  a\n  b\nc', 0, 7]);
  assert.deepEqual(r3(W.qhIndentLines('ab\ncd', 1, 4)), ['  ab\n  cd', 3, 8], 'mid-line ends move with the text');
  assert.deepEqual(r3(W.qhIndentLines('a\nb\nc', 0, 2)), ['  a\nb\nc', 0, 4], 'a selection that ends at a line start leaves that line');
  assert.deepEqual(r3(W.qhIndentLines('a\n\nb', 0, 4)), ['  a\n\n  b', 0, 8], 'an empty line stays empty');
  assert.deepEqual(r3(W.qhIndentLines('a\nb', 3, 0)), ['  a\n  b', 7, 0], 'a backward selection stays backward');
});

// ---------- outdent ----------

test('Shift+Tab removes a tab or up to two spaces, never other text', () => {
  const src = '    a\n  b\n c\nd\n\tx';
  assert.deepEqual(r3(W.qhOutdentLines(src, 0, src.length)), ['  a\nb\nc\nd\nx', 0, 11]);
  assert.deepEqual(r3(W.qhOutdentLines('a\nb', 0, 3)), ['a\nb', 0, 3], 'nothing to remove');
  assert.deepEqual(r3(W.qhOutdentLines(' a', 0, 0)), ['a', 0, 0], 'one space');
});

test('Shift+Tab with no selection outdents the caret line', () => {
  assert.deepEqual(r3(W.qhOutdentLines('x\n   y', 5, 5)), ['x\n y', 3, 3]);
  assert.deepEqual(r3(W.qhOutdentLines('  a', 1, 1)), ['a', 0, 0], 'a caret in the removed indent goes to the line start');
});

// ---------- line comment ----------

test('Ctrl+/ adds -- at the smallest indent of the non-blank lines', () => {
  assert.deepEqual(r3(W.qhToggleLineComment('  a\n    b', 0, 9)), ['  -- a\n  --   b', 0, 15]);
  assert.deepEqual(r3(W.qhToggleLineComment('a\n\nb', 0, 4)), ['-- a\n\n-- b', 0, 10], 'a blank line stays blank');
  assert.deepEqual(r3(W.qhToggleLineComment('-- a\nb', 0, 6)), ['-- -- a\n-- b', 0, 12], 'one plain line: every line gets a comment');
  assert.deepEqual(r3(W.qhToggleLineComment('SELECT 1', 3, 3)), ['-- SELECT 1', 6, 6], 'the caret line');
});

test('Ctrl+/ removes -- and one space when every non-blank line has it', () => {
  const src = '-- a\n  --b\n\n--  c';
  assert.deepEqual(r3(W.qhToggleLineComment(src, 0, src.length)), ['a\n  b\n\n c', 0, 9]);
  assert.deepEqual(r3(W.qhToggleLineComment('-- SELECT 1', 6, 6)), ['SELECT 1', 3, 3]);
  assert.deepEqual(r3(W.qhToggleLineComment('  \n', 0, 0)), ['  \n', 0, 0], 'only blank lines: no change');
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
  const docId = 'ind-' + Math.random();
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
  };
}
const dom = JSDOM ? {} : { skip: 'jsdom not installed' };

test('Tab on two lines indents both, keeps the selection, and one Ctrl+Z undoes it', dom, async () => {
  const ed = mount('SELECT 1\nSELECT 2');
  ed.caret(0, 17);
  const ev = ed.press('Tab');
  assert.equal(ev.defaultPrevented, true);
  assert.equal(ed.ctl.text, '  SELECT 1\n  SELECT 2');
  await ed.wait(30);
  assert.deepEqual(ed.sel, [0, 21]);
  ed.press('z', { ctrlKey: true });
  assert.equal(ed.ctl.text, 'SELECT 1\nSELECT 2');
});

test('Tab on part of one line still replaces it with two spaces', dom, () => {
  const ed = mount('SELECT abc');
  ed.caret(7, 10);
  ed.press('Tab');
  assert.equal(ed.ctl.text, 'SELECT   ');
});

test('Shift+Tab outdents the caret line and one Ctrl+Z undoes it', dom, async () => {
  const ed = mount('  SELECT 1\n    FROM t');
  ed.caret(3);
  assert.equal(ed.press('Tab', { shiftKey: true }).defaultPrevented, true);
  assert.equal(ed.ctl.text, 'SELECT 1\n    FROM t');
  await ed.wait(30);
  assert.deepEqual(ed.sel, [1, 1]);
  ed.press('z', { ctrlKey: true });
  assert.equal(ed.ctl.text, '  SELECT 1\n    FROM t');
});

test('Ctrl+/ and Cmd+/ toggle the comment, and one Ctrl+Z undoes it', dom, async () => {
  const ed = mount('SELECT 1\nSELECT 2');
  ed.caret(0, 17);
  assert.equal(ed.press('/', { ctrlKey: true, code: 'Slash' }).defaultPrevented, true);
  assert.equal(ed.ctl.text, '-- SELECT 1\n-- SELECT 2');
  await ed.wait(30);
  assert.deepEqual(ed.sel, [0, 23]);
  ed.press('z', { ctrlKey: true });
  assert.equal(ed.ctl.text, 'SELECT 1\nSELECT 2');
  ed.caret(2);
  ed.press('/', { metaKey: true, code: 'Slash' });
  assert.equal(ed.ctl.text, '-- SELECT 1\nSELECT 2');
  ed.caret(5);
  ed.press('/', { metaKey: true, code: 'Slash' });
  assert.equal(ed.ctl.text, 'SELECT 1\nSELECT 2');
});

test('Ctrl+/ works on a layout that types / with Shift, and Ctrl+Shift+/ on US does not', dom, () => {
  const ed = mount('SELECT 1');
  ed.caret(2);
  // Turkish Q: Shift+7 types `/`.
  assert.equal(ed.press('/', { ctrlKey: true, shiftKey: true, code: 'Digit7' }).defaultPrevented, true);
  assert.equal(ed.ctl.text, '-- SELECT 1');
  // The physical US slash key, whatever it types on this layout.
  ed.press('.', { ctrlKey: true, code: 'Slash' });
  assert.equal(ed.ctl.text, 'SELECT 1');
  // US layout: Ctrl+Shift+/ types `?`. It is not the comment key.
  ed.press('?', { ctrlKey: true, shiftKey: true, code: 'Slash' });
  assert.equal(ed.ctl.text, 'SELECT 1');
});

// ---------- sources ----------

test('the shortcut list names the new keys', () => {
  const src = web('qh-editor.jsx');
  for (const k of ['Indent / outdent the selected lines', 'Comment or uncomment the lines']) assert.ok(src.includes(k), k);
});
