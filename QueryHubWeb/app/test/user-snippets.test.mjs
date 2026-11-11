// Editor snippets of a person (2026-11-11, Devart medium batch, PR M8 = D3 + D4).
//
//   * D3  The Snippets panel creates, edits, sorts, puts in folders and deletes
//         the person's snippets (SnippetsPanel, qhSnippetGroups).
//   * D4  A shared snippet is visible to every QueryHub user. The typing list
//         offers it after the person's own and the built-in snippets
//         (qhSnippetsFor), so it never changes the `ssf` of another person.
//
// jsdom rule: compare booleans or text, never pass a DOM node to assert.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';
import { page, web, jsdomOpts as opts } from './_mount.mjs';

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');

const SCHEMA = { tables: ['orders'], columns: ['id'], dbs: [], tableCols: { orders: ['id'] } };
const split = (sql) => [sql.replace('|', ''), sql.indexOf('|')];
const snipItems = (sql, snippets, eng = 'postgres') => {
  const [t, c] = split(sql);
  const s = W.qhBuildSuggest(t, c, { ...SCHEMA, snippets }, eng);
  return s ? s.items.filter(i => i.type === 'snippet') : [];
};

const MINE = { id: '1', key: 'topn', description: 'Top rows', body: 'SELECT * FROM $table$ LIMIT 10', engine: null, mine: true };
const MINE_SSF = { id: '2', key: 'ssf', description: 'My ssf', body: 'SELECT id FROM $CURSOR$', engine: null, mine: true };
const SHARED = { id: '3', key: 'lastday', description: null, body: "SELECT * FROM $table$\nWHERE created_at > now() - interval '1 day'", engine: 'postgres', mine: false, owner: 'Kim' };
const SHARED_SSF = { id: '4', key: 'ssf', description: 'Their ssf', body: 'SELECT 1', engine: null, mine: false };

// ---------- the list of the typing list ----------

test('own, built-in, then shared: one snippet per key', () => {
  const list = W.qhSnippetsFor('postgres', [SHARED_SSF, MINE, SHARED]);
  const ssf = list.filter(s => s.key === 'ssf');
  assert.equal(ssf.length, 1);
  assert.equal(ssf[0].from, 'builtin', 'a shared ssf does not replace the built-in one');
  assert.equal(list.find(s => s.key === 'topn').from, 'mine');
  assert.equal(list.find(s => s.key === 'lastday').from, 'shared');
  const own = W.qhSnippetsFor('postgres', [MINE_SSF, SHARED_SSF]).filter(s => s.key === 'ssf');
  assert.equal(own.length, 1);
  assert.equal(own[0].desc, 'My ssf', 'the own ssf replaces the built-in one');
  assert.equal(W.qhSnippetsFor('postgres').length, W.qhSnippetsFor('postgres', []).length, 'no list = the built-in snippets');
});

test('a snippet with an engine goes only to that engine', () => {
  assert.equal(W.qhSnippetsFor('postgres', [SHARED]).some(s => s.key === 'lastday'), true);
  assert.equal(W.qhSnippetsFor('mssql', [SHARED]).some(s => s.key === 'lastday'), false);
  assert.equal(W.qhSnippetsFor('athena', [MINE]).some(s => s.key === 'topn'), true, 'no engine = every engine');
});

test('bad rows of the list are skipped', () => {
  const bad = [null, { key: '', body: 'x', mine: true }, { key: 'nob', mine: true }, { key: 'num', body: 5, mine: true }];
  assert.equal(W.qhSnippetsFor('postgres', bad).length, W.qhSnippetsFor('postgres').length);
  assert.equal(W.qhSnippetsFor('postgres', 'nope').length, W.qhSnippetsFor('postgres').length);
});

test('the typing list offers a person\'s snippet in every position, with its fields', () => {
  const first = snipItems('top|', [MINE]);
  assert.equal(first.length, 1);
  assert.equal(first[0].label, 'topn');
  assert.equal(first[0].text, 'SELECT * FROM <table> LIMIT 10');
  assert.deepEqual(first[0].stops, [[14, 21]]);
  assert.equal(snipItems('SELECT * FROM orders WHERE top|', [MINE]).length, 1, 'inside a statement too');
  assert.equal(snipItems('ssf|', [MINE_SSF])[0].desc, 'My ssf');
  assert.equal(snipItems('las|', [SHARED])[0].desc, 'SELECT * FROM $table$', 'no description: the first line');
});

test('the preview is the first line, cut at 48 characters', () => {
  assert.equal(W.qhSnippetPreview('\n  SELECT 1\nFROM t'), 'SELECT 1');
  assert.equal(W.qhSnippetPreview('x'.repeat(60)), 'x'.repeat(47) + '…');
  assert.equal(W.qhSnippetPreview(''), '');
});

// ---------- the form check ----------

test('the form check: key, keyword start, own duplicate, lengths, SQL', () => {
  const ok = { key: 'topn', body: 'SELECT 1' };
  assert.equal(W.qhSnippetFormError(ok, []), null);
  assert.match(W.qhSnippetFormError({ ...ok, key: 't' }, []), /2 to 32 characters/);
  assert.match(W.qhSnippetFormError({ ...ok, key: '1abc' }, []), /a letter first/);
  assert.match(W.qhSnippetFormError({ ...ok, key: 'a-b' }, []), /2 to 32/);
  assert.match(W.qhSnippetFormError({ ...ok, key: 'sel' }, []), /start of the keyword SELECT/);
  assert.match(W.qhSnippetFormError({ ...ok, key: 'TOPN' }, [{ id: '9', key: 'topn' }]), /You have a snippet with the key topn/);
  assert.equal(W.qhSnippetFormError({ ...ok, id: '9' }, [{ id: '9', key: 'topn' }]), null, 'the same snippet is no duplicate');
  assert.match(W.qhSnippetFormError({ ...ok, body: '  \n ' }, []), /Write the SQL/);
  assert.match(W.qhSnippetFormError({ ...ok, body: 'x'.repeat(20001) }, []), /20000/);
  assert.match(W.qhSnippetFormError({ ...ok, description: 'd'.repeat(201) }, []), /200 characters/);
  assert.match(W.qhSnippetFormError({ ...ok, folder: 'f'.repeat(65) }, []), /64 characters/);
});

test('an engine keyword counts too: TOP is SQL Server only', () => {
  assert.equal(W.QH_KEYWORDS.some(k => k.toLowerCase().startsWith('top')), false, 'TOP is not in the shared list');
  const mssqlTop = W.qhKeywordsFor('mssql').some(k => k.toLowerCase().startsWith('top'));
  const msg = W.qhSnippetFormError({ key: 'top', body: 'x' }, []);
  assert.equal(msg !== null, mssqlTop, msg);
});

// ---------- the groups of the panel ----------

const PANEL = [
  { id: '1', key: 'b1', body: 'x', folder: 'Reports', mine: true, updatedAt: '2026-11-01T00:00:00Z' },
  { id: '2', key: 'a1', body: 'x', folder: null, mine: true, updatedAt: '2026-11-03T00:00:00Z' },
  { id: '3', key: 'c1', body: 'x', folder: 'Admin', mine: true, updatedAt: '2026-11-02T00:00:00Z' },
  { id: '4', key: 'z9', body: 'x', folder: 'Ignored', shared: true, mine: false, owner: 'Kim', updatedAt: '2026-11-04T00:00:00Z' },
];
const shape = (gs) => gs.map(g => g.title + ':' + g.items.map(i => i.key).join(','));

test('groups: by folder, by key, by last change; shared by others last', () => {
  const G = loadInto(bareWindow(), 'qh-data.jsx', 'qh-modal.jsx', 'qh-editor.jsx', 'qh-panels.jsx');
  assert.deepEqual(shape(G.qhSnippetGroups(PANEL, 'folder')), ['Admin:c1', 'Reports:b1', 'No folder:a1', 'Shared by others:z9']);
  assert.deepEqual(shape(G.qhSnippetGroups(PANEL, 'key')), ['My snippets:a1,b1,c1', 'Shared by others:z9']);
  assert.deepEqual(shape(G.qhSnippetGroups(PANEL, 'recent')), ['My snippets:a1,c1,b1', 'Shared by others:z9']);
  assert.deepEqual(shape(G.qhSnippetGroups(PANEL.filter(x => !x.folder || !x.mine), 'folder')), ['My snippets:a1', 'Shared by others:z9'], 'no folders: one plain group');
  assert.deepEqual(G.qhSnippetGroups(null, 'folder'), []);
});

// ---------- the panel ----------

function mount(snippets, { canDeleteShared = false, save, del, store } = {}) {
  const ui = page(['qh-data.jsx', 'qh-modal.jsx', 'qh-editor.jsx', 'qh-panels.jsx'], { prep: (win) => {
    for (const [k, v] of Object.entries(store || {})) win.localStorage.setItem(k, v);
  } });
  const calls = { save: [], del: [] };
  const state = { list: snippets.slice() };
  const rerender = () => ui.render(ui.React.createElement(ui.win.SnippetsPanel, {
    snippets: state.list, canDeleteShared,
    onSave: (id, body) => { calls.save.push([id, body]); return save ? save(id, body) : Promise.resolve(); },
    onDelete: (id) => { calls.del.push(id); return del ? del(id) : Promise.resolve(); },
  }));
  rerender();
  return { ui, calls, rerender, state };
}
const rowOf = (ui, key) => ui.all('.qh-snip-row').find(r => r.querySelector('.qh-snip-key').textContent === key);
const field = (ui, label) => ui.all('.qh-snip-f').find(l => l.querySelector('span').textContent === label).querySelector('input, textarea, select');
function type(ui, el, value) {
  const proto = el.tagName === 'TEXTAREA' ? ui.win.HTMLTextAreaElement.prototype : el.tagName === 'SELECT' ? ui.win.HTMLSelectElement.prototype : ui.win.HTMLInputElement.prototype;
  const set = Object.getOwnPropertyDescriptor(proto, 'value').set;
  ui.React.act(() => { set.call(el, value); el.dispatchEvent(new ui.win.Event(el.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true })); });
}
const btn = (ui, text) => ui.all('button').find(b => b.textContent === text);

test('the panel lists groups; an empty list says how to start', opts, () => {
  const { ui } = mount(PANEL);
  assert.deepEqual(ui.all('.qh-snip-gtitle').map(x => x.textContent), ['Admin', 'Reports', 'No folder', 'Shared by others']);
  assert.equal(rowOf(ui, 'z9').querySelector('.qh-snip-meta').textContent, 'By Kim');
  assert.equal(!!rowOf(ui, 'z9').querySelector('.qh-snip-del'), false, 'no delete on another person\'s snippet');
  assert.equal(!!rowOf(ui, 'b1').querySelector('.qh-snip-del'), true);
  const empty = mount([]).ui;
  assert.equal(empty.doc.querySelector('.qh-side-empty-title').textContent, 'No snippets of yours yet');
});

test('the sort is kept in this browser', opts, () => {
  const { ui } = mount(PANEL);
  const sel = ui.doc.querySelector('.qh-snip-sort select');
  ui.React.act(() => { sel.value = 'key'; sel.dispatchEvent(new ui.win.Event('change', { bubbles: true })); });
  assert.deepEqual(ui.all('.qh-snip-gtitle').map(x => x.textContent), ['My snippets', 'Shared by others']);
  assert.equal(ui.win.localStorage.getItem('qh.snipsort.v1'), 'key');
  const again = mount(PANEL, { store: { 'qh.snipsort.v1': 'recent' } }).ui;
  assert.equal(again.doc.querySelector('.qh-snip-sort select').value, 'recent', 'a new page opens on the stored sort');
  const bad = mount(PANEL, { store: { 'qh.snipsort.v1': 'bogus' } }).ui;
  assert.equal(bad.doc.querySelector('.qh-snip-sort select').value, 'folder');
  assert.deepEqual(bad.all('.qh-snip-gtitle').map(x => x.textContent), ['Admin', 'Reports', 'No folder', 'Shared by others'], 'an unknown stored sort groups by folder');
});

test('New snippet: the form checks first, then sends a clean body', opts, async () => {
  const { ui, calls } = mount(PANEL);
  ui.click(btn(ui, 'New snippet'));
  assert.equal(ui.doc.querySelector('.qh-snip-title').textContent, 'New snippet');
  type(ui, field(ui, 'Key'), 'sel');
  type(ui, field(ui, 'SQL'), 'SELECT 1');
  ui.click(btn(ui, 'Save'));
  assert.match(ui.doc.querySelector('.qh-snip-err').textContent, /start of the keyword SELECT/);
  assert.equal(calls.save.length, 0, 'a refused form sends nothing');
  type(ui, field(ui, 'Key'), ' TopN ');
  type(ui, field(ui, 'Description'), '  Top rows ');
  type(ui, field(ui, 'Engine'), 'mssql');
  type(ui, field(ui, 'Folder'), ' Reports ');
  ui.click(ui.doc.querySelector('.qh-snip-share input'));
  ui.click(btn(ui, 'Save'));
  await ui.flush();
  assert.deepEqual(calls.save, [[null, { key: 'topn', description: 'Top rows', body: 'SELECT 1', engine: 'mssql', folder: 'Reports', shared: true }]]);
  assert.equal(!!ui.doc.querySelector('.qh-snip-form'), false, 'a saved form closes');
});

test('a server error keeps the form open with its message', opts, async () => {
  const { ui } = mount(PANEL, { save: () => Promise.reject(Object.assign(new Error('You have a snippet with the key topn. Use a different key.'), { status: 409 })) });
  ui.click(btn(ui, 'New snippet'));
  type(ui, field(ui, 'Key'), 'topn');
  type(ui, field(ui, 'SQL'), 'SELECT 1');
  ui.click(btn(ui, 'Save'));
  await ui.flush();
  assert.equal(!!ui.doc.querySelector('.qh-snip-form'), true);
  assert.equal(ui.doc.querySelector('.qh-snip-err').textContent, 'You have a snippet with the key topn. Use a different key.');
});

test('a click on an own row edits it; Copy makes a new own snippet of a shared one', opts, async () => {
  const { ui, calls } = mount(PANEL);
  ui.click(rowOf(ui, 'b1'));
  assert.equal(ui.doc.querySelector('.qh-snip-title').textContent, 'Edit snippet');
  assert.equal(field(ui, 'Folder').value, 'Reports');
  ui.click(btn(ui, 'Save'));
  await ui.flush();
  assert.equal(calls.save[0][0], '1', 'an edit sends the id');
  ui.click(rowOf(ui, 'z9'));
  assert.equal(!!ui.doc.querySelector('.qh-snip-form'), false, 'a click on another person\'s row does not edit');
  ui.click(rowOf(ui, 'z9').querySelector('.qh-snip-btn'));
  assert.equal(ui.doc.querySelector('.qh-snip-title').textContent, 'New snippet');
  assert.equal(field(ui, 'Key').value, 'z9');
  assert.equal(field(ui, 'Folder').value, '', 'the copy has no folder of the other person');
  assert.equal(ui.doc.querySelector('.qh-snip-share input').checked, false, 'the copy is not shared');
  ui.click(btn(ui, 'Cancel'));
  assert.equal(!!ui.doc.querySelector('.qh-snip-form'), false);
});

test('the note for a key of a built-in snippet', opts, () => {
  const { ui } = mount([]);
  ui.click(btn(ui, 'New snippet'));
  type(ui, field(ui, 'Key'), 'ssf');
  assert.equal(ui.doc.querySelector('.qh-snip-note').textContent, 'This key replaces the built-in snippet ssf for you.');
});

test('delete needs a second click; an admin also deletes a shared snippet', opts, async () => {
  const { ui, calls } = mount(PANEL);
  const delOf = (k) => rowOf(ui, k).querySelector('.qh-snip-del');
  ui.click(delOf('b1'));
  assert.equal(calls.del.length, 0);
  assert.equal(delOf('b1').textContent, 'Delete?');
  ui.click(delOf('b1'));
  await ui.flush();
  assert.deepEqual(calls.del, ['1']);
  const admin = mount(PANEL, { canDeleteShared: true });
  const d = admin.ui.all('.qh-snip-row').find(r => r.querySelector('.qh-snip-key').textContent === 'z9').querySelector('.qh-snip-del');
  admin.ui.click(d); admin.ui.click(d);
  await admin.ui.flush();
  assert.deepEqual(admin.calls.del, ['4']);
});

// ---------- the wiring ----------

test('the app loads, saves and passes the snippets; the sidebar has the tab', () => {
  const app = web('qh-app.jsx');
  assert.match(app, /qhApi\.editorSnippets\(\)\.then\(r => \{\n\s+setUserSnippets\(/);
  assert.match(app, /snippets={userSnippets} canDeleteShared={canDelShared} onSaveSnippet={saveEditorSnippet} onDeleteSnippet={deleteEditorSnippet}/);
  assert.match(app, /id \? qhApi\.updateEditorSnippet\(id, body\) : qhApi\.createEditorSnippet\(body\)/);
  const panels = web('qh-panels.jsx');
  assert.match(panels, /\.\.\.\(onSaveSnippet \? \[\['snippets', DBIcons\.braces\]\] : \[\]\)/);
  assert.match(panels, /mode === 'snippets' && onSaveSnippet && <SnippetsPanel/);
  const api = web('qh-api.jsx');
  const mock = web('qh-api-mock.jsx');
  for (const n of ['editorSnippets', 'createEditorSnippet', 'updateEditorSnippet', 'deleteEditorSnippet']) {
    assert.match(api, new RegExp('\\n  ' + n + ':'), 'qh-api.jsx ' + n);
    assert.match(mock, new RegExp('\\n  ' + n + ': \\('), 'qh-api-mock.jsx ' + n);
  }
});
