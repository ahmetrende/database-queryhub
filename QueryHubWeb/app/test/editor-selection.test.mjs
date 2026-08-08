// The wiring, in a real DOM: does the parent actually learn what is selected?
//
// run-target.test.mjs pins the RULE. This file pins the PLUMBING, and it exists
// because the plumbing is where the first attempt was wrong: SqlEditor pushed
// the selection up through React's `onSelect`, which never fired, so the parent
// silently believed nothing was selected and Run kept running the whole tab —
// i.e. the reported bug survived its own fix, with passing unit tests.
//
// So this renders the real SqlEditor, selects real text, and presses real keys.
// It also covers the case a keystroke test would miss: the toolbar Run button
// lives outside the editor and blurs the textarea first, so the selection has to
// survive blur.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { bareWindow, loadInto } from './_load.mjs';

const require_ = createRequire(import.meta.url);

// jsdom is a devDependency; a checkout that only installed runtime deps still
// runs the rest of the suite rather than failing on an import.
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }

const SCRIPT = [
  '-- daily reconciliation',
  'SELECT count(*) FROM orders;',
  '-- UPDATE orders SET state = 1;   <- do not run this',
  'SELECT count(*) FROM shipments;',
].join('\n');

/** Mount the real editor with the real run decision behind it. */
function mount() {
  const dom = new JSDOM('<!doctype html><div id="root"></div>',
                        { pretendToBeVisual: true, url: 'https://localhost/' });
  for (const k of ['window', 'document', 'navigator', 'HTMLElement', 'Element',
                   'Node', 'Event', 'KeyboardEvent', 'getComputedStyle',
                   'requestAnimationFrame', 'cancelAnimationFrame', 'localStorage']) {
    globalThis[k] = dom.window[k];
  }
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;

  const React = require_('react');
  const ReactDOM = require_('react-dom/client');
  const { act } = require_('react-dom/test-utils');
  globalThis.React = React;

  const win = dom.window;
  win.React = React;
  loadInto(win, 'qh-data.jsx', 'qh-editor.jsx');
  const { SqlEditor, qhRunTarget } = win;
  assert.ok(SqlEditor && qhRunTarget, 'the sources did not publish what the app uses');

  // The parent, reduced to the decision under test — same shape as qh-app.jsx.
  const state = { ran: null };
  const selGet = { current: null };
  const curSel = () => (selGet.current ? selGet.current() : '');
  const runSelection = (sel) => {
    const t = qhRunTarget(sel, '');
    if (t.kind === 'comments') { state.ran = { via: 'refused' }; return; }
    if (t.kind !== 'selection') return;
    state.ran = { via: 'selection', sql: t.sql };
  };
  const primary = () => {
    const t = qhRunTarget(curSel(), SCRIPT);
    if (t.kind === 'comments') { state.ran = { via: 'refused' }; return; }
    if (t.kind === 'selection') { runSelection(t.sql); return; }
    state.ran = { via: 'all', sql: SCRIPT };
  };

  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  act(() => root.render(React.createElement(SqlEditor, {
    value: SCRIPT, onChange: () => {}, fontSize: 13,
    onRun: primary, onRunSelection: runSelection, selectionGetter: selGet,
    schema: { tables: [], columns: [], dbs: [] }, engineId: 'postgres',
  })));
  const ta = win.document.querySelector('textarea');
  assert.ok(ta, 'the editor rendered no textarea');

  return {
    curSel, primary, state,
    select(needle) {
      const i = SCRIPT.indexOf(needle);
      assert.ok(i >= 0, 'test needle is not in the script: ' + needle);
      act(() => {
        ta.selectionStart = i; ta.selectionEnd = i + needle.length;
        ta.dispatchEvent(new win.Event('select', { bubbles: true }));
      });
    },
    clear() {
      act(() => {
        ta.selectionStart = ta.selectionEnd = 0;
        ta.dispatchEvent(new win.Event('select', { bubbles: true }));
      });
    },
    blur() { act(() => ta.blur()); },
    press(key) {
      act(() => ta.dispatchEvent(new win.KeyboardEvent(
        'keydown', { key, bubbles: true, cancelable: true })));
    },
    took() { const r = state.ran; state.ran = null; return r; },
  };
}

const opts = JSDOM ? {} : { skip: 'jsdom not installed' };

test('F5 with one line selected runs only that line', opts, () => {
  const ed = mount();
  ed.select('SELECT count(*) FROM shipments;');
  ed.press('F5');
  assert.deepEqual(ed.took(),
    { via: 'selection', sql: 'SELECT count(*) FROM shipments;' });
});

test('F5 on a commented-out line refuses instead of running the script', opts, () => {
  const ed = mount();
  ed.select('-- UPDATE orders SET state = 1;');
  ed.press('F5');
  assert.deepEqual(ed.took(), { via: 'refused' });
});

test('F5 with nothing selected still runs the whole tab', opts, () => {
  const ed = mount();
  ed.clear();
  ed.press('F5');
  assert.deepEqual(ed.took(), { via: 'all', sql: SCRIPT });
});

test('F8 is unchanged and shares the same refusal', opts, () => {
  const ed = mount();
  ed.select('SELECT count(*) FROM orders;');
  ed.press('F8');
  assert.deepEqual(ed.took(),
    { via: 'selection', sql: 'SELECT count(*) FROM orders;' });
  ed.select('-- daily reconciliation');
  ed.press('F8');
  assert.deepEqual(ed.took(), { via: 'refused' });
});

test('the parent can read the live selection at all', opts, () => {
  const ed = mount();
  ed.select('FROM orders');
  assert.equal(ed.curSel(), 'FROM orders',
    'SqlEditor never published a selection reader — Run cannot see the selection');
});

test('the selection survives blur, so the toolbar button sees it', opts, () => {
  // Clicking Run moves focus off the textarea before the handler runs. If the
  // selection were tracked by focus, the button would run the whole tab while
  // the identical keystroke ran the selection.
  const ed = mount();
  ed.select('FROM orders');
  ed.blur();
  assert.equal(ed.curSel(), 'FROM orders');
  ed.primary();
  assert.deepEqual(ed.took(), { via: 'selection', sql: 'FROM orders' });
});
