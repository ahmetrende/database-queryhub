// Two browser tabs share the open-tabs record (qh.workspace.v1).
//
// Before this, each browser tab wrote its tabs and never read what the other
// wrote, so the last writer won and the other tab's unsaved SQL was gone after
// a reload. qhMergeWorkspace is a three-way merge (base = what this tab last
// wrote or read, mine = its live tabs, theirs = the record in storage now).
// The cases are ported from the IdP workspace autosave tests.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { bareWindow, loadInto } from './_load.mjs';

const win = loadInto(bareWindow(), 'qh-data.jsx');
const { qhMergeWorkspace, qhWorkspaceSlim, qhParseWorkspace, QH_WS_COPY_SUFFIX } = win;

const tab = (sql = 'base', over = {}) => ({ id: 'a', name: 'Report', sql, conn: 'c1', db: 'd1', ...over });
const ws = (tabs, activeId) => ({ tabs, activeId: activeId || (tabs[0] && tabs[0].id) || '' });
const sqls = (tabs) => tabs.map(t => t.sql);
let n = 100;
const newId = () => 't' + (n++);

const CASES = [
  { rule: 'mine unchanged', base: [tab()], mine: [tab()], theirs: [tab('theirs')], want: ['theirs'] },
  { rule: 'theirs unchanged', base: [tab()], mine: [tab('mine')], theirs: [tab()], want: ['mine'] },
  { rule: 'both equal', base: [tab()], mine: [tab('same')], theirs: [tab('same')], want: ['same'] },
  { rule: 'same SQL, different metadata', base: [tab()], mine: [tab('same', { name: 'Mine' })], theirs: [tab('same', { name: 'Theirs' })], want: ['same'] },
  { rule: 'SQL conflict', base: [tab()], mine: [tab('mine')], theirs: [tab('theirs')], want: ['mine', 'theirs'] },
  { rule: 'closed there, unchanged here', base: [tab()], mine: [tab()], theirs: [], want: [] },
  { rule: 'closed there, edited here', base: [tab()], mine: [tab('mine')], theirs: [], want: ['mine'] },
  { rule: 'added here', base: [], mine: [tab('mine')], theirs: [], want: ['mine'] },
  { rule: 'closed here, unchanged there', base: [tab()], mine: [], theirs: [tab()], want: [] },
  { rule: 'closed here, edited there', base: [tab()], mine: [], theirs: [tab('theirs')], want: ['theirs'] },
  { rule: 'added there', base: [], mine: [], theirs: [tab('theirs')], want: ['theirs'] },
  { rule: 'colliding added ids', base: [], mine: [tab('mine')], theirs: [tab('theirs')], want: ['mine', 'theirs'] },
  { rule: 'colliding identical additions', base: [], mine: [tab('same')], theirs: [tab('same')], want: ['same'] },
];

for (const c of CASES) {
  test(`merge: ${c.rule}`, () => {
    const merged = qhMergeWorkspace(ws(c.base), ws(c.mine), ws(c.theirs), newId);
    assert.deepEqual(sqls(merged.tabs), c.want);
    if (c.want.length === 2) {
      assert.equal(merged.tabs[0], c.mine[0], 'mine stays the same object');
      const copy = merged.tabs[1];
      assert.equal(copy.name, 'Report' + QH_WS_COPY_SUFFIX);
      assert.equal(copy.name, 'Report (other browser tab)');
      assert.equal(copy.autoName, false);
      assert.equal(copy.dirty, true);
      assert.notEqual(copy.id, 'a');
    } else if (c.mine.length && c.want[0] === c.mine[0].sql) {
      assert.equal(merged.tabs[0], c.mine[0], 'mine stays the same object');
    }
  });
}

test('merge: keeps my order, puts a conflict copy right after mine, appends their additions in their order', () => {
  const mine = [tab('mine'), tab('b', { id: 'b' })];
  const theirs = [tab('d', { id: 'd' }), tab('theirs'), tab('c', { id: 'c' })];
  const merged = qhMergeWorkspace(ws([tab()]), ws(mine), ws(theirs), newId);
  assert.deepEqual(sqls(merged.tabs), ['mine', 'theirs', 'b', 'd', 'c']);
  assert.equal(new Set(merged.tabs.map(t => t.id)).size, 5);
});

test('merge: compares only stored fields and keeps live fields when theirs wins', () => {
  const live = tab('base', { result: { rows: [1] }, status: 'done', runMs: 12, dirty: true, touchedAt: 1 });
  const merged = qhMergeWorkspace(ws([tab('base', { touchedAt: 999 })]), ws([live]), ws([tab('theirs', { touchedAt: 5 })]), newId);
  assert.equal(merged.tabs[0].sql, 'theirs');
  assert.deepEqual(merged.tabs[0].result, { rows: [1] });
  assert.equal(merged.tabs[0].status, 'done');
  assert.equal(merged.tabs[0].runMs, 12);
});

test('merge: a stored flag that is absent there clears mine when theirs wins', () => {
  const mine = tab('base', { autoName: true, unpicked: true });
  const merged = qhMergeWorkspace(ws([mine]), ws([mine]), ws([tab('theirs')]), newId);
  assert.equal(merged.tabs[0].sql, 'theirs');
  assert.ok(!merged.tabs[0].autoName);
  assert.ok(!merged.tabs[0].unpicked);
});

test('merge: a live-only change (result, dirty) is not a change', () => {
  const mine = [tab('base', { result: { rows: [] }, dirty: true })];
  const merged = qhMergeWorkspace(ws([tab()]), ws(mine), ws([tab()]), newId);
  assert.equal(merged.changed, false);
  assert.equal(merged.tabs, mine);
});

test('merge: keeps my active tab, or picks a neighbour when it was closed there', () => {
  const tabs = ['a', 'b', 'c'].map(id => tab(id, { id }));
  assert.equal(qhMergeWorkspace(ws(tabs), ws(tabs, 'b'), ws(tabs, 'a'), newId).activeId, 'b');
  assert.equal(qhMergeWorkspace(ws(tabs), ws(tabs, 'b'), ws([tabs[0], tabs[2]]), newId).activeId, 'c');
});

for (const scenario of ['earlier tabs closed', 'earlier conflict copy']) {
  test(`merge: focuses a surviving neighbour after ${scenario} shifts indices`, () => {
    const base = ['a', 'b', 'c'].map(id => tab(id, { id }));
    const mine = scenario === 'earlier tabs closed' ? base : [{ ...base[0], sql: 'mine' }, base[1], base[2]];
    const theirs = scenario === 'earlier tabs closed'
      ? [base[2], tab('remote addition', { id: 'd' })]
      : [{ ...base[0], sql: 'theirs' }, base[2]];
    assert.equal(qhMergeWorkspace(ws(base), ws(mine, 'b'), ws(theirs), newId).activeId, 'c');
  });
}

test('merge: a conflict copy is a new draft and never reuses a taken id', () => {
  let k = 0;
  const ids = ['a', 'b', 't9'];
  const merged = qhMergeWorkspace(ws([tab()]), ws([tab('mine', { reqId: 7 }), tab('b', { id: 'b' })]),
    ws([tab('theirs', { reqId: 7 })]), () => ids[k++] || 't9');
  assert.equal(merged.tabs[1].sql, 'theirs');
  assert.equal(merged.tabs[1].id, 't9');
  assert.equal(merged.tabs[1].reqId, undefined);
});

test('merge: without a newId the copy id is past every numeric id', () => {
  const merged = qhMergeWorkspace(ws([tab('base', { id: 't4' })]), ws([tab('mine', { id: 't4' })]), ws([tab('theirs', { id: 't4' })]));
  assert.equal(merged.tabs[1].id, 't5');
});

test('slim record keeps the shape an older page reads', () => {
  const slim = qhWorkspaceSlim([tab('x', { result: { rows: [] }, unpicked: false, autoName: true })], 'a', 42);
  assert.equal(slim.activeId, 'a');
  assert.equal(slim.savedAt, 42);
  assert.deepEqual(Object.keys(slim.tabs[0]).sort(),
    ['autoName', 'bundleId', 'conn', 'db', 'id', 'kind', 'name', 'qid', 'reqId', 'sql', 'touchedAt', 'unpicked'].sort());
  assert.equal(slim.tabs[0].touchedAt, 42);
  const round = qhParseWorkspace(JSON.stringify(slim));
  assert.equal(round.tabs[0].sql, 'x');
  assert.equal(qhParseWorkspace('not json'), null);
  assert.equal(qhParseWorkspace('{"tabs":5}'), null);
});

test('two writers converge: each keeps its own SQL after the merge', () => {
  const start = [tab('base')];
  const aEdit = [tab('A wrote this')];
  const bEdit = [tab('B wrote this')];
  // A writes first. B then writes from the same base: B merges A's record in.
  const theirsAtB = ws(aEdit);
  const atB = qhMergeWorkspace(ws(start), ws(bEdit), theirsAtB, newId);
  assert.deepEqual(sqls(atB.tabs), ['B wrote this', 'A wrote this']);
  // A reads B's merged record with its own write as the base: nothing is lost.
  const atA = qhMergeWorkspace(ws(aEdit), ws(aEdit), ws(atB.tabs), newId);
  assert.deepEqual(sqls(atA.tabs).sort(), ['A wrote this', 'B wrote this']);
});

// qh-app.jsx is the page; read the source to check the wiring is still there.
const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'src');
const app = readFileSync(resolve(SRC, 'qh-app.jsx'), 'utf8');

test('qh-app.jsx listens for storage writes to qh.workspace.v1', () => {
  assert.match(app, /const QH_WS_KEY = 'qh\.workspace\.v1'/);
  assert.match(app, /window\.addEventListener\('storage', onStorage\)/);
  const i = app.indexOf('const onStorage');
  assert.notEqual(i, -1, 'the storage listener moved');
  const body = app.slice(i, app.indexOf("window.addEventListener('storage'", i));
  assert.match(body, /e\.key !== QH_WS_KEY/);
  assert.match(body, /qhMergeWorkspace\(/);
});

test('qh-app.jsx does not write back the tabs it just took from storage', () => {
  // A write-back of the other browser tab's record can land after that tab's next
  // keystroke. The other tab then reads it as a change and drops the keystroke.
  const i = app.indexOf('const wsStoredRef = useRef(null);');
  assert.notEqual(i, -1, 'the echo guard moved');
  const effect = app.slice(i, app.indexOf('}, [tabs, activeId]);', i));
  assert.match(effect, /if \(stored && stored\.tabs === tabs && stored\.activeId === activeId\) return;/);
  assert.match(effect, /wsStoredRef\.current = \{ tabs: merged\.tabs, activeId: merged\.activeId \};\s*\}\s*\}\s*\}\s*wsWrite\(list, active\);/);
  const h = app.indexOf('const onStorage');
  const body = app.slice(h, app.indexOf("window.addEventListener('storage'", h));
  // Only when storage already holds the same tab contents. A merge that kept a
  // tab here (closed there, edited here) must still be written.
  assert.match(body, /if \(same\) wsStoredRef\.current = \{ tabs: merged\.tabs, activeId: merged\.activeId \};/);
  assert.match(body, /\} else if \(!same\) wsWrite\(merged\.tabs, merged\.activeId\);/);
});

test('qh-app.jsx reads storage and merges before it writes', () => {
  const i = app.indexOf('const wsWrite');
  assert.notEqual(i, -1, 'the workspace writer moved');
  const effect = app.slice(i, app.indexOf('}, [tabs, activeId]);', i));
  assert.match(effect, /localStorage\.getItem\(QH_WS_KEY\)/);
  assert.match(effect, /qhMergeWorkspace\(/);
});
