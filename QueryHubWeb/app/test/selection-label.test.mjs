// The Run button names a selection (2026-11-10). A part of the text that is
// selected runs alone, so the button says "Run selection" (or "Submit
// selection for DBA approval") and a note gives the number of lines. Ctrl+A
// selects the whole text, which runs the whole tab, so the label stays. A
// super-admin never waits for a DBA, so the button says Execute.
import test from 'node:test';
import assert from 'node:assert/strict';

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import { bareWindow, loadInto } from './_load.mjs';
import { page, jsdomOpts, sliceFunction } from './_mount.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const W = loadInto(bareWindow(), 'qh-data.jsx');
const lines = W.qhSelectionLines;
const label = W.qhPrimaryLabel;

const TEXT = 'select 1;\nselect 2\n  from t;\n';

test('no selection, a blank selection or the whole text counts as no selection', () => {
  assert.equal(lines(TEXT, 3, 3), 0);
  assert.equal(lines(TEXT, 9, 10), 0, 'only a line break');
  assert.equal(lines(TEXT, 0, TEXT.length), 0, 'Ctrl+A');
  assert.equal(lines('  ' + TEXT, 2, TEXT.length + 1), 0, 'the whole text without its outer blanks');
  assert.equal(lines('', 0, 0), 0);
});

test('a part of the text counts its lines, and a final line break adds none', () => {
  assert.equal(lines(TEXT, 0, 8), 1);
  assert.equal(lines(TEXT, 10, 26), 2);
  assert.equal(lines(TEXT, 10, 28), 2, 'the line break after the statement');
  assert.equal(lines(TEXT, 26, 10), 2, 'a backward selection');
});

test('the label: Execute for a super-admin, Run or Submit for the others', () => {
  assert.equal(label(true, false, false), 'Execute', 'also when the server does not auto-approve');
  assert.equal(label(true, true, true), 'Execute selection');
  assert.equal(label(false, true, false), 'Run');
  assert.equal(label(false, true, true), 'Run selection');
  assert.equal(label(false, false, false), 'Submit for DBA approval');
  assert.equal(label(false, false, true), 'Submit selection for DBA approval');
});

// ---------- the action bar ----------

const FILES = ['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-results-tools.jsx', 'qh-panels.jsx'];
function mountBar(extra) {
  const ui = page(FILES);
  const w = ui.win;
  w.useRef = ui.React.useRef; w.useState = ui.React.useState; w.useEffect = ui.React.useEffect;
  w.QH_KBD = { run: 'Ctrl Enter', newq: '', wrap: '', stmt: '' };
  // A super-admin's bar also draws the mask switch, a separate component.
  w.MaskToggle = w.MaskToggle || (() => null);
  const Bar = sliceFunction('qh-app.jsx', 'ActionBar', w);
  const props = { pickHint: false, redacted: 0, onRevealRedacted: () => {}, why: '', onWhy: () => {}, conn: null, db: null, connAlias: 'c', dbAlias: 'd', dbTier: 'RW',
    classify: { tier: 'RO', empty: false, multi: false, statements: [] }, pii: { columns: [], star: false }, autoApprove: false, tierExceedsGrant: false,
    busy: false, status: null, hasSql: true, onPrimary: () => {}, killed: false, riskHints: [], riskTop: 'low', onExplain: () => {}, tabCount: 1,
    onOpenBatch: () => {}, schedOpen: false, setSchedOpen: () => {}, onSchedule: () => {}, onCancelRun: () => {}, isSuper: false, unmasked: false,
    onUnmask: () => {}, runOnConn: null, runOn: null, onRunOn: () => {}, selLines: 0, ...extra };
  ui.render(ui.React.createElement(Bar, props));
  const run = ui.doc.querySelector('.qh-run');
  const note = ui.doc.querySelector('.qh-sel-lines');
  return { run, note };
}

test('a super-admin sees Execute and a run button, even when the server says no auto-approve', jsdomOpts, () => {
  const b = mountBar({ isSuper: true, autoApprove: false });
  assert.equal(b.run.textContent, 'Execute');
  assert.ok(!b.run.classList.contains('is-approval'), 'not the approval colour');
  assert.equal(b.note, null);
});

test('a selection changes the label and shows the line count', jsdomOpts, () => {
  let b = mountBar({ isSuper: true, selLines: 3 });
  assert.equal(b.run.textContent, 'Execute selection');
  assert.equal(b.note.textContent, '3 lines selected');
  b = mountBar({ autoApprove: true, selLines: 1 });
  assert.equal(b.run.textContent, 'Run selection');
  assert.equal(b.note.textContent, '1 line selected');
  b = mountBar({ autoApprove: false, selLines: 2 });
  assert.equal(b.run.textContent, 'Submit selection for DBA approval');
  assert.ok(b.run.classList.contains('is-approval'));
});

test('a running request hides the line count', jsdomOpts, () => {
  const b = mountBar({ autoApprove: true, selLines: 2, busy: true, status: 'running' });
  assert.equal(b.note, null);
});

// ---------- the editor reports the selection ----------

function mountEditor(value) {
  const ui = page(['qh-data.jsx', 'qh-editor.jsx']);
  const seen = [];
  ui.render(ui.React.createElement(ui.win.SqlEditor, {
    value, docId: 't1', fontSize: 13, engineId: 'postgres', onChange: () => {},
    schema: { tables: [], columns: [], dbs: [] }, onRunSelection: () => {}, onRun: () => {},
    onSelectionChange: (n) => seen.push(n) }));
  const ta = ui.doc.querySelector('textarea');
  const select = (a, b) => {
    ui.React.act(() => { ta.focus(); ta.setSelectionRange(a, b); ta.dispatchEvent(new ui.win.Event('select', { bubbles: true })); });
  };
  return { seen, select, ta, win: ui.win };
}

test('the editor reports the selection when it changes, once per change', jsdomOpts, () => {
  const ed = mountEditor(TEXT);
  ed.select(10, 26);
  ed.select(10, 26);
  ed.select(0, TEXT.length);
  assert.deepEqual(ed.seen.map(x => (x ? x.lines : 0)), [0, 2, 0], 'start, a two-line part, then Ctrl+A');
  assert.deepEqual(ed.seen[1], { lines: 2, start: 10, text: TEXT.slice(10, 26) });
  assert.equal(ed.seen[2], null, 'Ctrl+A is no selection');
});

test('an input reports the selection in the next task, never inside the input event', jsdomOpts, async () => {
  const ed = mountEditor(TEXT);
  ed.select(10, 26);
  assert.equal(ed.seen.length, 2);
  ed.ta.setSelectionRange(11, 11);
  ed.ta.dispatchEvent(new ed.win.Event('input', { bubbles: true }));
  assert.equal(ed.seen.length, 2, 'nothing while the input event runs: React has not seen the new text yet');
  await new Promise(r => setTimeout(r, 5));
  assert.equal(ed.seen.length, 3, 'then the report comes');
  assert.equal(ed.seen[2], null, 'the caret is no selection');
});

// ---------- the verdicts judge the selection ----------

test('the judged text is the selection when Run sends only the selection', () => {
  const sql = 'select 1;\nupdate t set a = 1;';
  assert.deepEqual(W.qhJudgedText({ lines: 1, start: 10, text: 'update t set a = 1' }, sql),
    { text: 'update t set a = 1', offset: 10, selection: true });
  assert.deepEqual(W.qhJudgedText(null, sql), { text: sql, offset: 0, selection: false });
  assert.deepEqual(W.qhJudgedText({ lines: 1, start: 3, text: '   ' }, sql), { text: sql, offset: 0, selection: false },
    'a blank selection runs the whole tab');
});

test('a blocker range found in the selection moves to its place in the tab', () => {
  const r = [{ start: 0, end: 5, message: 'm' }];
  assert.deepEqual(W.qhShiftRanges(r, 12), [{ start: 12, end: 17, message: 'm' }]);
  assert.equal(W.qhShiftRanges(r, 0), r);
  assert.deepEqual(W.qhShiftRanges(null, 4), []);
});

test('the app judges the selection: classify, blockers, PII, risks and Explain', () => {
  const app = readFileSync(resolve(HERE, '..', 'src', 'qh-app.jsx'), 'utf8');
  assert.match(app, /const judged = qhJudgedText\(sel, tab\.sql\);/);
  assert.match(app, /const localClassify = qhClassify\(judged\.text\);/);
  assert.match(app, /useServerClassify\(judged\.text, tab\.conn, tab\.db\)/);
  assert.match(app, /qhShiftRanges\(qhBlockerRanges\(judged\.text, server\.blockers \|\| \[\]\), judged\.offset\)/);
  assert.match(app, /const pii = qhDetectPII\(judged\.text\);/);
  assert.match(app, /const riskHints = qhRiskHints\(judged\.text, classify\);/);
  assert.match(app, /qhApi\.explain\(\{ connectionId: tab\.conn, databaseId: tab\.db, sql: judged\.text\.trim\(\) \}\)/);
  assert.match(app, /onSelectionChange=\{setSel\}/);
});
