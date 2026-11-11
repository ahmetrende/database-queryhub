// Run to the caret, format options and the long-run notice (2026-11-11,
// Devart medium batch, PR M5a).
//
//   * I3  Shift+F8 runs from the top of the text to the end of the statement at
//         the caret (qhToCaretRange). It selects that part first, as F8 does.
//   * E3  A popup beside the Format button sets the case, the indent and the
//         layout of Format (QH_FMT_CHOICES, qhFmtLibOptions). Stored in this
//         browser. ClickHouse keeps the case as typed.
//   * I7  A run that took at least the chosen time ends with a toast, and with
//         a browser notification on a hidden page (qhLongRunNotice,
//         qhRaiseRunNotif). The bell menu holds the choice.
// Every engine: PostgreSQL, SQL Server, ClickHouse and Athena.
//
// jsdom rule: compare booleans or text, never pass a DOM node to assert.
import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { format } from 'sql-formatter';

import { bareWindow, loadInto } from './_load.mjs';
import { page, sliceFunction, web, jsdomOpts as opts } from './_mount.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');
const toCaret = (sql, caret, eng = 'postgres') => {
  const r = W.qhToCaretRange(sql, caret, eng);
  return r ? sql.slice(r.start, r.end) : null;
};

// ---------- I3: the range ----------

test('the range ends at the statement of the caret', () => {
  const sql = 'SELECT 1;\nSELECT 2;\nSELECT 3;';
  assert.equal(toCaret(sql, 0), 'SELECT 1');
  assert.equal(toCaret(sql, 13), 'SELECT 1;\nSELECT 2');
  assert.equal(toCaret(sql, sql.length), sql.slice(0, -1), 'the last ; stays out, as with F8');
});

test('a script with no ; splits on its statement words', () => {
  const sql = 'SELECT a\nFROM t\nSELECT 2\nUPDATE t\nSET a = 1';
  assert.equal(toCaret(sql, 3), 'SELECT a\nFROM t');
  assert.equal(toCaret(sql, sql.indexOf('SELECT 2') + 2), 'SELECT a\nFROM t\nSELECT 2');
  assert.equal(toCaret(sql, sql.indexOf('SET')), sql);
});

test('a caret in the blanks after a statement stops at that statement', () => {
  assert.equal(toCaret('SELECT 1;\n\nSELECT 2', 10), 'SELECT 1');
  assert.equal(toCaret('SELECT 1; \nSELECT 2', 10), 'SELECT 1', 'after the ; on its line');
  assert.equal(toCaret('SELECT 1;\n    SELECT 2', 10), 'SELECT 1;\n    SELECT 2', 'the indent of a statement belongs to it');
});

test('a caret before the first statement, or no SQL, gives nothing', () => {
  assert.equal(W.qhToCaretRange('\n\nSELECT 1', 0, 'postgres'), null);
  assert.equal(W.qhToCaretRange('', 0, 'postgres'), null);
  assert.equal(W.qhToCaretRange('   ', 2, 'postgres'), null);
  assert.equal(W.qhToCaretRange('-- only a note', 5, 'postgres'), null);
});

test('a comment at the top runs with the first statement', () => {
  assert.equal(toCaret('-- top\n/*+ hint */\nSELECT 1;\nSELECT 2', 20), '-- top\n/*+ hint */\nSELECT 1');
  assert.equal(toCaret('\n\n  SELECT 1;\nSELECT 2', 5), 'SELECT 1', 'leading blanks stay out');
});

test('strings, quoted names and $$ bodies are data', () => {
  assert.equal(toCaret("SELECT 'a;b';\nSELECT 2", 9), "SELECT 'a;b'");
  assert.equal(toCaret('SELECT $$x;\nSELECT y$$;\nSELECT 2', 4), 'SELECT $$x;\nSELECT y$$');
  assert.equal(toCaret('SELECT [a;b] FROM t;\nSELECT 2', 3, 'mssql'), 'SELECT [a;b] FROM t');
});

test('every engine: GO on SQL Server, plain statements elsewhere', () => {
  const ms = 'SELECT 1\nGO\nSELECT 2\nGO\nSELECT 3';
  assert.equal(toCaret(ms, ms.indexOf('SELECT 2') + 1, 'mssql'), 'SELECT 1\nGO\nSELECT 2');
  for (const eng of ['clickhouse', 'athena']) {
    const sql = 'SELECT 1\nSELECT 2\nSELECT 3';
    assert.equal(toCaret(sql, 12, eng), 'SELECT 1\nSELECT 2', eng);
  }
});

// ---------- I3: the key in the real editor ----------

function mountEditor(initial, eng = 'postgres') {
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
  const ran = [];
  const docId = 'm5a-' + Math.random();
  function Host() {
    const [v, setV] = React.useState(initial);
    return React.createElement(win.SqlEditor, { value: v, docId, fontSize: 13, schema: null, engineId: eng, onChange: setV,
      onRun: () => ran.push('ALL'), onRunSelection: (s) => ran.push(s) });
  }
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(Host)));
  const ta = win.document.querySelector('textarea');
  return {
    ran,
    get sel() { return [ta.selectionStart, ta.selectionEnd]; },
    caret(i, j = i) { React.act(() => { ta.focus(); ta.setSelectionRange(i, j); }); },
    press(key, mods = {}) {
      const ev = new win.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...mods });
      React.act(() => ta.dispatchEvent(ev));
      return ev;
    },
  };
}
const dom = JSDOM ? {} : { skip: 'jsdom not installed' };

test('Shift+F8 selects from the top to the statement of the caret and runs that part', dom, () => {
  const sql = 'SELECT 1;\nSELECT 2;\nSELECT 3;';
  const ed = mountEditor(sql);
  ed.caret(13);
  const ev = ed.press('F8', { shiftKey: true });
  assert.equal(ev.defaultPrevented, true);
  assert.deepEqual(ed.ran, ['SELECT 1;\nSELECT 2']);
  assert.deepEqual(ed.sel, [0, 18], 'the person sees what runs');
  ed.press('F8', { shiftKey: true });
  assert.deepEqual(ed.ran, ['SELECT 1;\nSELECT 2', 'SELECT 1;\nSELECT 2'], 'a second press runs the same part');
});

test('F8 alone still runs only the statement at the caret', dom, () => {
  const ed = mountEditor('SELECT 1;\nSELECT 2;\nSELECT 3;');
  ed.caret(13);
  ed.press('F8');
  assert.deepEqual(ed.ran, ['SELECT 2']);
});

test('Shift+F8 before the first statement runs nothing', dom, () => {
  const ed = mountEditor('\n\nSELECT 1');
  ed.caret(0);
  const ev = ed.press('F8', { shiftKey: true });
  assert.equal(ev.defaultPrevented, true, 'the key is taken, so the browser does nothing either');
  assert.deepEqual(ed.ran, []);
});

test('the shortcut list names Shift+F8', () => {
  assert.match(web('qh-editor.jsx'), /\['⇧ \+ F8', 'Run from the top to the current statement'\]/);
});

// ---------- E3: the options ----------

function fmtWin(store) {
  const win = bareWindow();
  const data = new Map(Object.entries(store || {}));
  win.localStorage = { getItem: (k) => (data.has(k) ? data.get(k) : null), setItem: (k, v) => data.set(k, String(v)), removeItem: (k) => data.delete(k) };
  win.__qhSqlFormatter = { format };
  loadInto(win, 'qh-sqlformat.jsx');
  return { win, data };
}

test('the cleaner keeps known values and drops the rest', () => {
  const { win } = fmtWin();
  const o = win.qhFmtOptionsClean({ keywordCase: 'upper', indent: '9', expressionWidth: 80, denseOperators: 'yes', bogus: 1 });
  assert.equal(o.keywordCase, 'upper');
  assert.equal(o.indent, '2', 'an unknown indent gets the default');
  assert.equal(o.expressionWidth, 80);
  assert.equal(o.denseOperators, false, 'a string is not a boolean');
  assert.equal('bogus' in o, false);
  assert.deepEqual(Object.keys(win.qhFmtOptionsClean(null)).sort(), Object.keys(win.QH_FMT_CHOICES).sort());
  assert.deepEqual(win.qhFmtOptionsClean('x'), win.QH_FMT_DEFAULTS);
});

test('the defaults give the same text as Format gave before the options', () => {
  const { win } = fmtWin();
  const samples = ['select a, b from t where x = 1 and y = 2 or z = 3',
    'select u.id, count(*) as n from users u join orders o on o.user_id = u.id group by u.id; select 2;',
    "select a::int, b from t where x = 'it''s' and y in (1,2) order by 1 limit 10"];
  for (const sql of samples) {
    const old = format(sql, { language: 'postgresql', tabWidth: 2, keywordCase: 'preserve' }).replace(/\n$/, '');
    assert.equal(win.qhFormatSql(sql, 'postgres'), old, sql);
  }
});

test('indent, case and layout reach the formatter', () => {
  const { win } = fmtWin();
  const lib = (o, eng = 'postgres') => win.qhFmtLibOptions(o, eng);
  assert.equal(lib({ indent: '4' }).tabWidth, 4);
  assert.equal(lib({ indent: '4' }).useTabs, false);
  assert.equal(lib({ indent: 'tab' }).useTabs, true);
  const up = win.qhFormatSql('select count(x) from t where a = 1 and b = 2', 'postgres', { keywordCase: 'upper', functionCase: 'upper' });
  assert.equal(up.split('\n')[0], 'SELECT');
  assert.ok(up.includes('COUNT(x)'), up);
  const tab = win.qhFormatSql('select a from t where b = 1', 'postgres', { indentStyle: 'tabularLeft' });
  assert.equal(tab.split('\n')[0], 'select    a');
  const after_ = win.qhFormatSql('select a from t where b = 1 and c = 2', 'postgres', { logicalOperatorNewline: 'after' });
  assert.ok(after_.includes('b = 1 and\n'), after_);
  const dense = win.qhFormatSql('select a + b from t', 'postgres', { denseOperators: true, newlineBeforeSemicolon: true });
  assert.ok(dense.includes('a+b'), dense);
  const two = win.qhFormatSql('select 1; select 2;', 'postgres', { linesBetweenQueries: 0 });
  assert.equal(two.includes('\n\n'), false, two);
});

test('the stored options apply when the call passes none', () => {
  const { win } = fmtWin({ 'qh.fmt.v1': JSON.stringify({ keywordCase: 'upper', indent: '4' }) });
  const out = win.qhFormatSql('select a from t', 'postgres');
  assert.equal(out, 'SELECT\n    a\nFROM\n    t');
  win.qhFmtOptionsSave({ keywordCase: 'lower', indent: 'x' });
  assert.deepEqual(JSON.parse(win.localStorage.getItem('qh.fmt.v1')).indent, '2', 'save cleans too');
  const bad = fmtWin({ 'qh.fmt.v1': '{not json' }).win;
  assert.equal(bad.qhFormatSql('select a from t', 'postgres'), 'select\n  a\nfrom\n  t', 'bad stored text gives the defaults');
});

test('ClickHouse keeps every case as typed: names are case sensitive there', () => {
  const { win } = fmtWin();
  const all = { keywordCase: 'upper', functionCase: 'upper', dataTypeCase: 'upper', identifierCase: 'upper', indent: '4' };
  const lib = win.qhFmtLibOptions(all, 'clickhouse');
  for (const k of ['keywordCase', 'functionCase', 'dataTypeCase', 'identifierCase']) assert.equal(lib[k], 'preserve', k);
  assert.equal(lib.tabWidth, 4, 'the layout options still apply');
  // The library reads `events` as a keyword: an upper keyword case would
  // change the name of the table.
  const out = win.qhFormatSql('select toDate(ts), CAST(x AS UInt64) from events', 'clickhouse', all);
  assert.ok(out.includes('toDate(ts)'), out);
  assert.ok(out.includes('UInt64'), out);
  assert.ok(out.includes('\n    events'), out);
  assert.ok(out.startsWith('select'), out);
  for (const eng of ['postgres', 'mssql', 'athena']) {
    for (const k of ['keywordCase', 'functionCase', 'dataTypeCase', 'identifierCase']) assert.equal(win.qhFmtLibOptions(all, eng)[k], 'upper', eng + ' ' + k);
  }
});

// ---------- E3: the popup in the tab bar ----------

function mountTabs(engine, store, onFormat) {
  const ui = page(['qh-data.jsx', 'qh-modal.jsx', 'qh-editor.jsx', 'qh-sqlformat.jsx'], { prep: (win) => {
    for (const [k, v] of Object.entries(store || {})) win.localStorage.setItem(k, v);
    win.__qhSqlFormatter = { format };
  } });
  const noop = () => {};
  ui.render(ui.React.createElement(ui.win.EditorTabs, {
    tabs: [{ id: 'a', name: 'Query 1', tier: 'RO' }], activeId: 'a', onSelect: noop, onClose: noop, onNew: noop,
    wrap: false, onToggleWrap: noop, onCloseOthers: noop, onCloseRight: noop, onCloseAll: noop, onDuplicate: noop,
    onRename: noop, onCopySql: noop, onFormat: onFormat || noop, fmtEngine: engine }));
  return ui;
}
const pop = (ui) => ui.doc.querySelector('.qh-fmt-pop');
const selectOf = (ui, label) => ui.all('.qh-fmt-row').find(r => r.textContent.startsWith(label)).querySelector('select');
function choose(ui, sel, value) {
  ui.React.act(() => { sel.value = value; sel.dispatchEvent(new ui.win.Event('change', { bubbles: true })); });
}

test('the chevron opens the popup with every option and a preview', opts, () => {
  const ui = mountTabs('postgres');
  assert.equal(!!pop(ui), false, 'closed at first');
  const btn = ui.doc.querySelector('[aria-label="Format options"]');
  assert.equal(btn.getAttribute('aria-expanded'), 'false');
  ui.click(btn);
  assert.equal(!!pop(ui), true);
  assert.equal(btn.getAttribute('aria-expanded'), 'true');
  assert.equal(ui.all('.qh-fmt-row select').length, 10, 'the Profile row (M7b) and the nine options');
  assert.equal(ui.all('.qh-fmt-check input').length, 3, 'two format boxes and the alias switch');
  assert.equal(ui.doc.querySelector('.qh-fmt-prev').textContent.split('\n')[0], 'select');
  ui.click(btn);
  assert.equal(!!pop(ui), false, 'a second click closes it');
});

test('a change is stored at once and the preview follows it', opts, () => {
  const ui = mountTabs('postgres');
  ui.click(ui.doc.querySelector('[aria-label="Format options"]'));
  choose(ui, selectOf(ui, 'Keywords'), 'upper');
  assert.equal(JSON.parse(ui.win.localStorage.getItem('qh.fmt.v1')).keywordCase, 'upper');
  assert.equal(ui.doc.querySelector('.qh-fmt-prev').textContent.split('\n')[0], 'SELECT');
  choose(ui, selectOf(ui, '( ) on one line'), '120');
  assert.equal(JSON.parse(ui.win.localStorage.getItem('qh.fmt.v1')).expressionWidth, 120, 'a number stays a number');
  const dense = ui.all('.qh-fmt-check input')[0];
  ui.click(dense);
  assert.equal(JSON.parse(ui.win.localStorage.getItem('qh.fmt.v1')).denseOperators, true);
  ui.click(ui.all('.qh-fmt-foot button').find(b => b.textContent === 'Reset'));
  assert.deepEqual(JSON.parse(ui.win.localStorage.getItem('qh.fmt.v1')), ui.win.QH_FMT_DEFAULTS);
  assert.equal(selectOf(ui, 'Keywords').value, 'preserve');
});

test('the popup opens on the stored options, and Format now formats the tab', opts, () => {
  let calls = 0;
  const ui = mountTabs('mssql', { 'qh.fmt.v1': JSON.stringify({ keywordCase: 'lower', indent: 'tab' }) }, () => { calls += 1; });
  ui.click(ui.doc.querySelector('[aria-label="Format options"]'));
  assert.equal(selectOf(ui, 'Keywords').value, 'lower');
  assert.equal(selectOf(ui, 'Indent').value, 'tab');
  ui.click(ui.all('.qh-fmt-foot button').find(b => b.textContent === 'Format now'));
  assert.equal(calls, 1);
  assert.equal(!!pop(ui), true, 'the popup stays open, so the person sees the result');
});

test('on ClickHouse the four case options are locked and the popup says why', opts, () => {
  const ui = mountTabs('clickhouse', { 'qh.fmt.v1': JSON.stringify({ keywordCase: 'upper' }) });
  ui.click(ui.doc.querySelector('[aria-label="Format options"]'));
  for (const l of ['Keywords', 'Functions', 'Data types', 'Names']) assert.equal(selectOf(ui, l).disabled, true, l);
  assert.equal(selectOf(ui, 'Keywords').value, 'preserve', 'a locked row shows what Format does');
  assert.equal(selectOf(ui, 'Indent').disabled, false);
  assert.equal(ui.doc.querySelector('.qh-fmt-note').textContent, 'ClickHouse names are case sensitive. The case stays as typed.');
  assert.equal(ui.doc.querySelector('.qh-fmt-prev').textContent.split('\n')[0], 'select', 'the preview shows the same');
  const pg = mountTabs('postgres');
  pg.click(pg.doc.querySelector('[aria-label="Format options"]'));
  assert.equal(pg.all('.qh-fmt-row select').some(s => s.disabled), false);
  assert.equal(!!pg.doc.querySelector('.qh-fmt-note'), false);
});

test('a click outside or Escape closes the popup; the shortcut list closes it too', opts, () => {
  const ui = mountTabs('postgres');
  const btn = ui.doc.querySelector('[aria-label="Format options"]');
  ui.click(btn);
  ui.React.act(() => ui.doc.body.dispatchEvent(new ui.win.Event('pointerdown', { bubbles: true })));
  assert.equal(!!pop(ui), false);
  ui.click(btn);
  ui.React.act(() => ui.doc.dispatchEvent(new ui.win.KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
  assert.equal(!!pop(ui), false);
  ui.click(btn);
  ui.click(ui.doc.querySelector('[aria-label="Keyboard shortcuts"]'));
  assert.equal(!!pop(ui), false);
  assert.equal(!!ui.doc.querySelector('.qh-kb-pop'), true);
});

test('the app passes the engine of the open tab to the tab bar', () => {
  assert.match(web('qh-app.jsx'), /onFormat=\{\(\) => setFmtSig\(n => n \+ 1\)\} fmtEngine=\{editorEngine\}/);
});

// ---------- I7: the notice ----------

const D = loadInto(bareWindow(), 'qh-data.jsx');

test('a run that took the chosen time or longer gets a notice', () => {
  const n = (r, sec = 10) => D.qhLongRunNotice(r, sec);
  assert.equal(n({ status: 'done', runMs: 9999, rowCount: 5, name: 'Q' }), null);
  assert.deepEqual(n({ status: 'done', runMs: 10000, rowCount: 1234, name: 'Daily users' }),
    { title: 'Query finished', text: '“Daily users” finished in 10.0 s. 1,234 rows.' });
  assert.equal(n({ status: 'done', runMs: 61000, rowCount: 1, name: 'Q' }).text, '“Q” finished in 1 min 1 s. 1 row.');
  assert.equal(n({ status: 'done', runMs: 12000, rowCount: null, name: 'Q' }).text, '“Q” finished in 12.0 s.');
  assert.equal(n({ status: 'done', runMs: 12000, rowCount: -1, name: 'Q' }).text, '“Q” finished in 12.0 s.');
  assert.deepEqual(n({ status: 'failed', runMs: 30000, name: 'Q' }), { title: 'Query failed', text: '“Q” failed after 30.0 s.' });
  assert.equal(n({ status: 'done', runMs: 12000 }).text, '“Query” finished in 12.0 s.');
});

test('no notice when it is off, without a run time, for a refusal or a hand-off to a DBA', () => {
  const r = { status: 'done', runMs: 60000, rowCount: 5, name: 'Q' };
  assert.equal(D.qhLongRunNotice(r, 0), null);
  assert.equal(D.qhLongRunNotice({ ...r, runMs: null }, 10), null);
  assert.equal(D.qhLongRunNotice({ ...r, runMs: NaN }, 10), null);
  assert.equal(D.qhLongRunNotice({ ...r, status: 'rejected' }, 10), null);
  assert.equal(D.qhLongRunNotice({ ...r, status: 'running' }, 10), null);
  assert.equal(D.qhLongRunNotice({ ...r, status: 'failed', awaitingDba: true }, 10), null);
  assert.equal(D.qhLongRunNotice(null, 10), null);
});

test('the choice is stored in this browser; 10 s is the default', () => {
  const win = bareWindow();
  const data = new Map();
  win.localStorage = { getItem: (k) => (data.has(k) ? data.get(k) : null), setItem: (k, v) => data.set(k, String(v)) };
  loadInto(win, 'qh-data.jsx');
  assert.equal(win.qhLongRunLoad(), 10);
  win.qhLongRunStore(0);
  assert.equal(win.qhLongRunLoad(), 0, 'off is a choice, not a missing value');
  win.qhLongRunStore(300);
  assert.equal(win.qhLongRunLoad(), 300);
  data.set('qh.longrun.v1', '7');
  assert.equal(win.qhLongRunLoad(), 10, 'a value that is not in the list gives the default');
  assert.deepEqual(win.QH_LONGRUN_CHOICES.map(([v]) => v), [0, 5, 10, 30, 60, 300]);
  const broken = bareWindow();
  broken.localStorage = { getItem: () => { throw new Error('blocked'); }, setItem: () => { throw new Error('blocked'); } };
  loadInto(broken, 'qh-data.jsx');
  assert.equal(broken.qhLongRunLoad(), 10);
  broken.qhLongRunStore(5);   // does not throw
});

class FakeNote {
  static permission = 'granted';
  static made = [];
  constructor(title, o) { this.title = title; this.o = o; this.closed = false; FakeNote.made.push(this); }
  close() { this.closed = true; }
}

test('a browser notification only for a hidden page, with the switch on and permission', () => {
  const note = { title: 'Query finished', text: '“Q” finished in 12.0 s.' };
  FakeNote.made = [];
  FakeNote.permission = 'granted';
  assert.equal(D.qhRaiseRunNotif(note, { Ctor: FakeNote, hidden: false, on: true }), null, 'a visible page has the toast');
  assert.equal(D.qhRaiseRunNotif(note, { Ctor: FakeNote, hidden: true, on: false }), null, 'the bell switch is off');
  assert.equal(D.qhRaiseRunNotif(null, { Ctor: FakeNote, hidden: true, on: true }), null);
  assert.equal(D.qhRaiseRunNotif(note, { Ctor: null, hidden: true, on: true }), null);
  FakeNote.permission = 'default';
  assert.equal(D.qhRaiseRunNotif(note, { Ctor: FakeNote, hidden: true, on: true }), null, 'it never asks');
  assert.equal(FakeNote.made.length, 0);
  FakeNote.permission = 'granted';
  let opened = 0;
  const n = D.qhRaiseRunNotif(note, { Ctor: FakeNote, hidden: true, on: true, tag: 'qh-run-42', onClick: () => { opened += 1; } });
  assert.equal(FakeNote.made.length, 1);
  assert.equal(n.title, 'Query finished');
  assert.deepEqual(n.o, { body: '“Q” finished in 12.0 s.', tag: 'qh-run-42' });
  n.onclick();
  assert.equal(opened, 1, 'a click opens the tab');
  assert.equal(n.closed, true);
});

test('the app raises the notice once, only for a run that the page saw before it ended', () => {
  const app = web('qh-app.jsx');
  const body = app.slice(app.indexOf('const applyStatus = async'), app.indexOf('const refreshOnce = async'));
  assert.match(body, /if \(sres\.status !== 'done' && sres\.status !== 'failed' && sres\.status !== 'rejected'\) liveRuns\.current\.add\(qkey\);/);
  assert.match(body, /else if \(liveRuns\.current\.delete\(qkey\)\) \{/);
  assert.match(body, /qhLongRunNotice\(\{ status: sres\.status, runMs: sres\.runMs, rowCount: sres\.rowCount, name: own && own\.name, awaitingDba: !!sres\.awaitingDba \}, qhLongRunLoad\(\)\)/);
  assert.match(body, /pushToast\(note\.text, sres\.status === 'failed' \? 'err' : null\)/);
  assert.match(body, /qhRaiseRunNotif\(note, \{ Ctor: window\.Notification, hidden: document\.hidden, on: qhBrowserNotifStored\(\)/);
  assert.match(body, /onClick: \(\) => \{ setView\('dev'\); setActiveId\(id\); \}/);
  assert.ok(body.indexOf('liveRuns.current.add') < body.indexOf("if (sres.scheduledFor"), 'before the early return of a scheduled run');
  assert.match(app, /const pushToast = \(msg, kind\) => \{ setToast\(msg\); setToastKind\(kind \|\| null\);/);
  assert.match(app, /className=\{'qh-toast' \+ \(toastKind === 'err' \? ' is-err' : ''\)\}/);
});

// ---------- I7: the choice in the bell menu ----------

const pages = [];
after(() => { pages.forEach(w => { try { w.close(); } catch (e) { /* closed */ } }); });

function mountBell(store) {
  const ui = page(['qh-data.jsx'], { prep: (win) => {
    for (const [k, v] of Object.entries(store || {})) win.localStorage.setItem(k, v);
    win.useState = win.React.useState; win.useEffect = win.React.useEffect; win.useRef = win.React.useRef;
    win.qhTimeAgo = () => 'now';
    win.qhApi = { notifications: () => Promise.resolve({ notifications: [] }), notificationsRead: () => Promise.resolve({ ok: true }) };
  } });
  pages.push(ui.win);
  sliceFunction('qh-app.jsx', 'notifIcon', ui.win);
  const Bell = sliceFunction('qh-app.jsx', 'NotificationBell', ui.win);
  ui.render(ui.React.createElement(Bell, { unseenNews: false, isAdminUser: false, onOpenSubject: () => {} }));
  return ui;
}

test('the bell menu sets the long-run time; the hint says what it does', opts, async () => {
  const ui = mountBell();
  await ui.flush();
  ui.click(ui.doc.querySelector('.qh-notif-btn'));
  const sel = ui.doc.querySelector('.qh-notif-longrun select');
  assert.equal(sel.value, '10');
  assert.deepEqual([...sel.options].map(o => o.textContent), ['Off', '5 s', '10 s', '30 s', '1 min', '5 min']);
  const hint = () => ui.doc.getElementById('qh-notif-longrun-hint').textContent;
  assert.equal(hint(), 'A run of 10 s or more shows a notice when it ends. If browser notifications are on, a hidden page also gets one.');
  choose(ui, sel, '60');
  assert.equal(ui.win.localStorage.getItem('qh.longrun.v1'), '60');
  assert.match(hint(), /^A run of 1 min or more/);
  choose(ui, sel, '0');
  assert.equal(ui.win.localStorage.getItem('qh.longrun.v1'), '0');
  assert.equal(hint(), 'Off. A long run ends with no notice.');
});

test('the bell menu opens on the stored time', opts, async () => {
  const ui = mountBell({ 'qh.longrun.v1': '30' });
  await ui.flush();
  ui.click(ui.doc.querySelector('.qh-notif-btn'));
  assert.equal(ui.doc.querySelector('.qh-notif-longrun select').value, '30');
});
