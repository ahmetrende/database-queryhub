// Snippets and saved queries at the caret (2026-11-10, Devart D1).
//
//   * A short key in the typing list inserts a piece of SQL: `ssf` + Tab gives
//     `SELECT * FROM ` with the caret after FROM (QH_SNIPPETS, qhBuildSuggest).
//   * A statement snippet shows only for the first word of a statement, an
//     expression snippet only inside one. A write snippet does not show on a
//     read-only engine.
//   * A saved query goes in at the caret on lines of its own (qhInsertBlock),
//     from its Insert button (insertSignal) or by a drag onto the editor.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { bareWindow, loadInto } from './_load.mjs';
import { web } from './_mount.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');

const SCHEMA = {
  tables: ['orders', 'users', 'sessions_log'],
  columns: ['id', 'user_id', 'total', 'active'],
  dbs: [],
  tableCols: { orders: ['id', 'user_id', 'total'], users: ['id', 'active'] },
};
const split = (sql) => [sql.replace('|', ''), sql.indexOf('|')];
const suggest = (sql, eng = 'postgres', opts) => { const [t, c] = split(sql); return W.qhBuildSuggest(t, c, SCHEMA, eng, opts); };
const snips = (sql, eng) => { const s = suggest(sql, eng); return s ? s.items.filter(i => i.type === 'snippet').map(i => i.label) : []; };

// ---------- the list itself ----------

test('no key is the start of a keyword, and every key is a word of two letters or more', () => {
  const kws = W.QH_KEYWORDS.map(k => k.toLowerCase());
  const keys = W.QH_SNIPPETS.map(s => s.key);
  assert.equal(new Set(keys).size, keys.length, 'keys are unique');
  for (const k of keys) {
    assert.match(k, /^[a-z][a-z0-9]+$/, k + ' is one lower-case word');
    const hit = kws.find(w => w.startsWith(k));
    assert.equal(hit, undefined, `${k} is the start of the keyword ${hit}`);
  }
});

test('each body has at most one caret marker', () => {
  for (const sn of W.QH_SNIPPETS) {
    for (const [eng, body] of Object.entries(sn.body)) {
      const n = body.split(W.QH_SNIPPET_CURSOR).length - 1;
      assert.ok(n <= 1, `${sn.key}/${eng} has ${n} markers`);
    }
  }
});

test('the engines get the snippets that they can run', () => {
  const keys = (eng) => W.qhSnippetsFor(eng).map(s => s.key);
  assert.ok(keys('postgres').includes('ii'), 'PostgreSQL gets the write snippets');
  assert.ok(keys('mssql').includes('dfw'), 'SQL Server gets the write snippets');
  for (const eng of ['clickhouse', 'athena']) {
    for (const k of ['ii', 'upw', 'dfw']) assert.ok(!keys(eng).includes(k), `${eng} is read-only: no ${k}`);
    for (const k of ['act', 'blk', 'tsz']) assert.ok(!keys(eng).includes(k), `${eng} has no ${k} body`);
    assert.ok(keys(eng).includes('ssf'), `${eng} gets ssf`);
  }
  const s100 = (eng) => W.qhSnippetsFor(eng).find(s => s.key === 's100').body;
  assert.equal(s100('mssql'), 'SELECT TOP 100 * FROM $CURSOR$');
  assert.equal(s100('postgres'), 'SELECT * FROM $CURSOR$ LIMIT 100');
  assert.match(W.qhSnippetsFor('postgres').find(s => s.key === 'act').body, /pg_stat_activity/);
  assert.match(W.qhSnippetsFor('mssql').find(s => s.key === 'act').body, /sys\.dm_exec_requests/);
});

test('the text drops the marker and gives the caret offset', () => {
  assert.deepEqual(W.qhSnippetText('SELECT * FROM $CURSOR$', ''), { text: 'SELECT * FROM ', caretAt: 14 });
  assert.deepEqual(W.qhSnippetText('SELECT 1', ''), { text: 'SELECT 1', caretAt: 8 }, 'no marker: the end');
  const r = W.qhSnippetText('WITH cte AS (\n  $CURSOR$\n)\nSELECT * FROM cte', '    ');
  assert.equal(r.text, 'WITH cte AS (\n      \n    )\n    SELECT * FROM cte', 'each line after the first gets the indent');
  assert.equal(r.caretAt, 'WITH cte AS (\n      '.length);
});

// ---------- the typing list ----------

test('an exact key goes first, with the text and the caret of the snippet', () => {
  const s = suggest('ssf|');
  const it = s.items[0];
  assert.equal(it.type, 'snippet');
  assert.equal(it.label, 'ssf');
  assert.equal(it.desc, 'SELECT * FROM');
  assert.equal(it.text, 'SELECT * FROM ');
  assert.equal(it.caretAt, 14);
  assert.equal(s.start, 0);
  assert.equal(s.end, 3);
  assert.equal(suggest('act|').items[0].label, 'act', 'an exact key beats a column that it starts');
});

test('a key that the token only starts goes after the keywords', () => {
  const labels = suggest('up|').items.map(i => i.label);
  assert.ok(labels.indexOf('UPDATE') >= 0 && labels.indexOf('upw') > labels.indexOf('UPDATE'), labels.join(','));
  assert.deepEqual(snips('s|'), [], 'one letter gives none');
  assert.deepEqual(snips('ss|'), ['ssf']);
});

test('a statement snippet needs the first word of a statement', () => {
  assert.deepEqual(snips('ssf|'), ['ssf']);
  assert.deepEqual(snips('  ssf|'), ['ssf'], 'an indent is fine');
  assert.deepEqual(snips('SELECT 1;\nssf|'), ['ssf'], 'after a ;');
  assert.deepEqual(snips('SELECT 1\n\nssf|'), ['ssf'], 'a line of its own after a blank line');
  assert.deepEqual(snips('SELECT 1\nssf|'), [], 'the next line of the same statement');
  assert.deepEqual(snips('SELECT a FROM orders WHERE ssf|'), []);
});

test('an expression snippet needs a word inside a statement', () => {
  assert.deepEqual(snips('SELECT id, cw|'), ['cw']);
  assert.deepEqual(snips('SELECT * FROM orders WHERE exs|'), ['exs']);
  assert.deepEqual(snips('cw|'), [], 'not as the first word');
});

test('a qualifier, a table slot, a string and a comment get no snippet', () => {
  assert.deepEqual(snips('SELECT x.ssf|'), []);
  assert.deepEqual(snips('SELECT * FROM ssf|'), [], 'a table slot');
  assert.deepEqual(snips('-- ssf|'), [], 'a comment');
  assert.deepEqual(snips("SELECT 'ssf|"), [], 'a string');
  assert.deepEqual(snips('DROP TABLE ssf|'), [], 'a slot of its own');
});

test('a read-only engine gets no write snippet in the list', () => {
  assert.deepEqual(snips('ii|', 'postgres'), ['ii']);
  assert.deepEqual(snips('ii|', 'clickhouse'), []);
  assert.deepEqual(snips('dfw|', 'athena'), []);
  assert.deepEqual(snips('ssf|', 'athena'), ['ssf']);
});

test('the lines of a snippet get the indent of its line', () => {
  const it = suggest('    sgc|').items[0];
  assert.equal(it.label, 'sgc');
  assert.equal(it.text, 'SELECT , COUNT(*) AS cnt\n    FROM \n    GROUP BY \n    ORDER BY cnt DESC');
  assert.equal(it.caretAt, 7);
});

// ---------- a saved query at the caret ----------

test('a block goes on lines of its own', () => {
  assert.deepEqual(W.qhInsertBlock('', 0, 0, 'SELECT 1'), { value: 'SELECT 1', caret: 8 });
  assert.deepEqual(W.qhInsertBlock('SELECT 2', 8, 8, 'SELECT 1'), { value: 'SELECT 2\nSELECT 1', caret: 17 }, 'text before the caret');
  assert.deepEqual(W.qhInsertBlock('SELECT 2', 0, 0, 'SELECT 1'), { value: 'SELECT 1\nSELECT 2', caret: 8 }, 'text after the caret');
  assert.deepEqual(W.qhInsertBlock('a\n\nb', 2, 2, 'SELECT 1\n'), { value: 'a\nSELECT 1\nb', caret: 10 }, 'a blank line: no newline, trailing space trimmed');
  assert.deepEqual(W.qhInsertBlock('SELECT x FROM t', 7, 8, 'SELECT 1'), { value: 'SELECT \nSELECT 1\n FROM t', caret: 16 }, 'over a selection');
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
  function Host() {
    const [v, setV] = React.useState(initial);
    const [sig, setSig] = React.useState(null);
    ctl.text = v;
    ctl.insert = (text) => setSig(x => ({ text, n: (x ? x.n : 0) + 1 }));
    return React.createElement(win.SqlEditor, { value: v, docId: 't1', fontSize: 13, schema: SCHEMA, engineId: eng, onChange: setV, insertSignal: sig });
  }
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(Host)));
  const ta = win.document.querySelector('textarea');
  const setValue = Object.getOwnPropertyDescriptor(win.HTMLTextAreaElement.prototype, 'value').set;
  const ed = {
    win, ta, ctl, act: React.act,
    get sel() { return [ta.selectionStart, ta.selectionEnd]; },
    caret(i, j = i) { ta.focus(); ta.setSelectionRange(i, j); },
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
    // Wait for the animation frame that puts the caret in place.
    frame() { return React.act(() => new Promise(r => setTimeout(r, 40))); },
    opts() { return Array.from(win.document.querySelectorAll('.qh-ac-ed-opt')); },
  };
  return ed;
}

const dom = JSDOM ? {} : { skip: 'jsdom not installed' };

test('ssf + Tab writes SELECT * FROM with the caret after FROM', dom, async () => {
  const ed = mount('');
  ed.caret(0);
  ed.type('ssf');
  const row = ed.opts()[0];
  assert.ok(row, 'the list is open');
  assert.equal(row.querySelector('.qh-ac-ed-name').firstChild.textContent, 'ssf');
  assert.equal(row.querySelector('.qh-ac-ed-desc').textContent, 'SELECT * FROM');
  assert.equal(row.querySelector('.qh-ac-ed-type').textContent, 'snip');
  assert.equal(row.getAttribute('title'), 'SELECT * FROM ', 'the row shows the full text on hover');
  const ev = ed.press('Tab');
  assert.ok(ev.defaultPrevented, 'Tab picks the snippet and does not indent');
  assert.equal(ed.ta.value, 'SELECT * FROM ');
  await ed.frame();
  assert.deepEqual(ed.sel, [14, 14]);
  const z = ed.press('z', { metaKey: true });
  assert.ok(z.defaultPrevented);
  assert.equal(ed.ta.value, 'ssf', 'one undo step gives the key back');
});

test('a snippet with a marker puts the caret at the marker', dom, async () => {
  const ed = mount('SELECT 1;\n');
  ed.caret(10);
  ed.type('upw');
  ed.press('Tab');
  assert.equal(ed.ta.value, 'SELECT 1;\nUPDATE \nSET \nWHERE ');
  await ed.frame();
  assert.deepEqual(ed.sel, [17, 17], 'after UPDATE, on the first line');
});

test('the Insert signal puts a saved query at the caret, as one undo step', dom, async () => {
  const ed = mount('SELECT 2');
  ed.caret(8);
  ed.act(() => ed.ctl.insert('SELECT 1 FROM orders\n'));
  assert.equal(ed.ta.value, 'SELECT 2\nSELECT 1 FROM orders');
  assert.equal(ed.ctl.text, ed.ta.value, 'the parent holds the new text');
  await ed.frame();
  assert.deepEqual(ed.sel, [29, 29]);
  ed.press('z', { metaKey: true });
  assert.equal(ed.ta.value, 'SELECT 2');
});

test('a drop puts a saved query on its own lines, a name in the line', dom, () => {
  const drop = (ed, text, types) => {
    const ev = new ed.win.Event('drop', { bubbles: true, cancelable: true });
    Object.defineProperty(ev, 'dataTransfer', { value: { types, getData: (t) => (t === 'text/plain' ? text : '1') } });
    ed.act(() => ed.ta.dispatchEvent(ev));
  };
  const a = mount('SELECT * FROM ');
  a.caret(14);
  drop(a, 'SELECT 1', ['text/plain', W.QH_DRAG_BLOCK]);
  assert.equal(a.ta.value, 'SELECT * FROM \nSELECT 1', 'a saved query');
  const b = mount('SELECT * FROM ');
  b.caret(14);
  drop(b, 'orders', ['text/plain']);
  assert.equal(b.ta.value, 'SELECT * FROM orders', 'a tree name stays in the line');
});

// ---------- the wiring ----------

test('a saved row has an Insert button and drags its SQL as a block', () => {
  const panels = web('qh-panels.jsx');
  assert.match(panels, /onLoadSaved, onInsertSaved, onDeleteSaved/, 'Sidebar takes onInsertSaved');
  assert.match(panels, /className="qh-row-ins" onClick=\{\(e\) => \{ e\.stopPropagation\(\); onInsertSaved\(s\); \}\}/);
  assert.match(panels, /aria-label="Insert saved query at caret"/);
  assert.match(panels, /draggable=\{!!onInsertSaved\}/);
  assert.match(panels, /setData\('text\/plain', s\.sql \|\| ''\)/);
  assert.match(panels, /setData\(QH_DRAG_BLOCK, '1'\)/);
});

test('the app sends the saved SQL to the editor, or opens a tab where there is none', () => {
  const app = web('qh-app.jsx');
  assert.match(app, /onLoadSaved=\{loadSaved\} onInsertSaved=\{insertSaved\}/);
  assert.match(app, /formatSignal=\{fmtSig\} insertSignal=\{insSig\}/);
  assert.match(app, /if \(tab\.kind \|\| view !== 'dev'\) \{ loadSaved\(s\); return; \}/);
  assert.match(app, /setInsSig\(x => \(\{ text: s\.sql \|\| '', n: \(x \? x\.n : 0\) \+ 1 \}\)\)/);
});

test('the shortcuts list names the snippets, and the styles exist', () => {
  const ed = web('qh-editor.jsx');
  assert.match(ed, /\['ssf \+ Tab', 'Snippet: type a key, pick it \(list below\)'\]/);
  assert.match(ed, /\{QH_SNIPPETS\.map\(sn => \(/);
  const html = web('QueryHub.html');
  for (const sel of ['.ac-t-snippet', '.qh-ac-ed-desc', '.qh-row-ins', '.qh-kb-sub']) {
    assert.ok(html.includes(sel + ' {') || html.includes(sel + ','), sel + ' has a rule');
  }
});

// ---------- the empty-editor hint ----------

test('an empty editor shows the hint, and the hint names a real key with its real text', dom, () => {
  const ed = mount('');
  assert.equal(ed.ta.getAttribute('placeholder'), W.QH_EDITOR_HINT, 'the textarea carries the hint');
  const m = W.QH_EDITOR_HINT.match(/Tip: (\w+) \+ Tab gives (.+?)\.\n/);
  assert.ok(m, 'the hint names a key and its text');
  for (const eng of ['postgres', 'mssql', 'clickhouse']) {
    const sn = W.qhSnippetsFor(eng).find(s => s.key === m[1]);
    assert.ok(sn, m[1] + ' is a snippet key on ' + eng);
    assert.ok(!sn.expr, m[1] + ' starts a statement');
    assert.equal(W.qhSnippetText(sn.body, '').text.trimEnd(), m[2], 'the key gives the named text on ' + eng);
  }
  assert.match(W.QH_EDITOR_HINT, /⌨ in the tab bar/, 'the hint says where the full list is');
});

test('the hint has its own colour, because the textarea text is transparent', () => {
  const html = web('QueryHub.html');
  assert.match(html, /\.qh-ta \{[^}]*color: transparent/, 'the textarea text is transparent');
  assert.match(html, /\.qh-ta::placeholder \{ color: var\(--fg-tertiary\); opacity: 1; \}/);
});
