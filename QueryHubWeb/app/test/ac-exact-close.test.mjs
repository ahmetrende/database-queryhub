// A suggestion list that only repeats the typed word does not open
// (2026-11-10). After `= NULL` the list showed one row, NULL, and while it
// was open it hid the code-analysis line under `= NULL`. Ctrl+Space still
// opens the list.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { bareWindow, loadInto } from './_load.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }
const dom = JSDOM ? {} : { skip: 'jsdom not installed' };

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');

test('a list adds nothing only when every row is the typed word', () => {
  assert.equal(W.qhListAddsNothing([{ text: 'NULL', type: 'keyword' }], 'NULL'), true);
  assert.equal(W.qhListAddsNothing([{ text: 'NULL ', type: 'keyword' }], 'null'), true, 'case and a trailing space do not count');
  assert.equal(W.qhListAddsNothing([{ text: 'NULL', type: 'keyword' }, { text: 'NULLIF', type: 'function' }], 'NULL'), false);
  assert.equal(W.qhListAddsNothing([{ text: 'NULL', type: 'keyword' }], 'NUL'), false);
  assert.equal(W.qhListAddsNothing([{ text: 'SELECT * FROM ', type: 'snippet' }], 'ssf'), false);
  assert.equal(W.qhListAddsNothing([{ text: 'ssf', type: 'snippet' }], 'ssf'), false, 'a snippet always counts');
  assert.equal(W.qhListAddsNothing([], 'NULL'), false);
  assert.equal(W.qhListAddsNothing([{ header: true, label: 'x' }, { text: 'id', type: 'column' }], 'id'), true, 'a group header is not a row');
});

function mount(initial) {
  const d = new JSDOM('<!doctype html><div id="root"></div>', { pretendToBeVisual: true, url: 'https://localhost/' });
  for (const k of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node', 'Event',
                   'KeyboardEvent', 'InputEvent', 'getComputedStyle', 'requestAnimationFrame',
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
  function Host() {
    const [v, setV] = React.useState(initial);
    return React.createElement(win.SqlEditor, { value: v, docId: 't1', fontSize: 13, schema: { tables: ['t'], columns: ['a'], dbs: [] }, engineId: 'postgres', onChange: setV });
  }
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(Host)));
  const ta = win.document.querySelector('textarea');
  const setValue = Object.getOwnPropertyDescriptor(win.HTMLTextAreaElement.prototype, 'value').set;
  return {
    ta,
    type(s) {
      ta.focus();
      for (const ch of s) {
        const a = ta.selectionStart, v = ta.value;
        React.act(() => {
          setValue.call(ta, v.slice(0, a) + ch + v.slice(ta.selectionEnd));
          ta.setSelectionRange(a + 1, a + 1);
          ta.dispatchEvent(new win.InputEvent('input', { inputType: 'insertText', data: ch, bubbles: true }));
        });
      }
    },
    press(key, mods = {}) { React.act(() => ta.dispatchEvent(new win.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...mods }))); },
    rows() { return Array.from(win.document.querySelectorAll('.qh-ac-ed-opt')).map(r => r.querySelector('.qh-ac-ed-name').firstChild.textContent); },
  };
}

test('typing the last letter of NULL closes the list', dom, () => {
  const ed = mount('SELECT * FROM t WHERE a = ');
  ed.ta.focus();
  ed.ta.setSelectionRange(ed.ta.value.length, ed.ta.value.length);
  ed.type('NUL');
  assert.ok(ed.rows().includes('NULL'), 'NUL still offers NULL');
  ed.type('L');
  assert.deepEqual(ed.rows(), [], 'NULL is complete: no list');
});

test('Ctrl+Space still opens the list on a complete word', dom, () => {
  const ed = mount('SELECT * FROM t WHERE a = NULL');
  ed.ta.focus();
  ed.ta.setSelectionRange(ed.ta.value.length, ed.ta.value.length);
  ed.press(' ', { ctrlKey: true });
  assert.ok(ed.rows().length > 0, 'the list opens');
});
