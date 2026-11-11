// Format profiles (2026-11-11, Devart medium batch, PR M7b = E2).
//
// A profile is a named set of the Format options (QH_FMT_PROFILES). The
// Profile row of the popup sets every option at once. It shows the profile
// that matches the stored options, or "Custom" when none matches. The popup
// stores no profile. The options stay in `qh.fmt.v1` as before.
//
// jsdom rule: compare booleans or text, never pass a DOM node to assert.
import test from 'node:test';
import assert from 'node:assert/strict';
import { format } from 'sql-formatter';

import { bareWindow, loadInto } from './_load.mjs';
import { page, jsdomOpts as opts } from './_mount.mjs';

function fmtWin(store) {
  const win = bareWindow();
  const data = new Map(Object.entries(store || {}));
  win.localStorage = { getItem: (k) => (data.has(k) ? data.get(k) : null), setItem: (k, v) => data.set(k, String(v)), removeItem: (k) => data.delete(k) };
  win.__qhSqlFormatter = { format };
  loadInto(win, 'qh-sqlformat.jsx');
  return win;
}

const SQL = 'select u.id, cast(o.total as decimal(12, 2)) as total, count(o.id) as n from users u join orders o on o.user_id = u.id ' +
  'where u.active = 1 and o.total > 100 group by u.id, o.total; select 1;';
const ids = (win) => win.QH_FMT_PROFILES.map(p => p[0]);

// ---------- the profile list ----------

test('six profiles, in the Devart order, each with its own option set', () => {
  const win = fmtWin();
  assert.deepEqual(ids(win), ['default', 'compact', 'extended', 'indented', 'msdn', 'right']);
  assert.deepEqual(win.QH_FMT_PROFILES.map(p => p[1]), ['Default', 'Compact', 'Extended', 'Indented', 'MSDN SQL', 'Right aligned']);
  const sets = ids(win).map(id => JSON.stringify(win.qhFmtProfileOptions(id)));
  assert.equal(new Set(sets).size, sets.length, 'no two profiles are the same');
  assert.equal(JSON.stringify(win.qhFmtProfileOptions('default')), JSON.stringify(win.QH_FMT_DEFAULTS));
});

test('a profile names only known options, with values from their lists', () => {
  const win = fmtWin();
  for (const [id, , set] of win.QH_FMT_PROFILES) {
    const full = win.qhFmtProfileOptions(id);
    for (const [k, v] of Object.entries(set)) {
      assert.ok(Object.prototype.hasOwnProperty.call(win.QH_FMT_CHOICES, k), id + ' ' + k);
      assert.equal(full[k], v, id + ' ' + k + ' survives the cleaner');
    }
    for (const k of Object.keys(win.QH_FMT_CHOICES)) {
      if (!(k in set)) assert.equal(full[k], win.QH_FMT_DEFAULTS[k], id + ': ' + k + ' is the default');
    }
  }
  assert.equal(win.qhFmtProfileOptions('collapsed'), null, 'an unknown id gives null');
  assert.equal(win.qhFmtProfileOptions(undefined), null);
});

test('the profile of a set of options, or Custom', () => {
  const win = fmtWin();
  for (const id of ids(win)) assert.equal(win.qhFmtProfileOf(win.qhFmtProfileOptions(id)), id, id);
  assert.equal(win.qhFmtProfileOf(null), 'default', 'no stored options = the defaults');
  assert.equal(win.qhFmtProfileOf({ indent: 'x', bogus: 1 }), 'default', 'bad values are cleaned first');
  assert.equal(win.qhFmtProfileOf({ keywordCase: 'upper' }), '', 'one changed option is Custom');
  assert.equal(win.qhFmtProfileOf({ ...win.qhFmtProfileOptions('msdn'), functionCase: 'upper' }), '');
  assert.equal(win.qhFmtProfileOf({ indentStyle: 'tabularRight' }), 'right');
});

// ---------- what each profile does to the text ----------

const run = (win, id, eng = 'postgres') => win.qhFormatSql(SQL, eng, win.qhFmtProfileOptions(id));

test('Default gives the same text as Format gave before the options', () => {
  const win = fmtWin();
  const old = format(SQL, { language: 'postgresql', tabWidth: 2, keywordCase: 'preserve' }).replace(/\n$/, '');
  assert.equal(run(win, 'default'), old);
});

test('Compact: no indent and no empty line', () => {
  const win = fmtWin();
  const out = run(win, 'compact');
  assert.equal(out.split('\n').some(l => /^\s/.test(l)), false, out);
  assert.equal(out.includes('\n\n'), false, out);
  assert.ok(out.includes('\nu.id,\n'), out);
  const lib = win.qhFmtLibOptions({ indent: '0' }, 'postgres');
  assert.equal(lib.tabWidth, 0);
  assert.equal(lib.useTabs, false);
});

test('Extended: two empty lines between statements', () => {
  const win = fmtWin();
  const out = run(win, 'extended');
  assert.ok(out.includes(';\n\n\nselect'), out);
  assert.equal(out.includes('\n\n\n\n'), false, out);
});

test('Indented: four spaces', () => {
  const win = fmtWin();
  const out = run(win, 'indented');
  assert.ok(out.includes('\n    u.id,\n'), out);
  assert.equal(out.split('\n')[0], 'select', 'the case stays as typed');
});

test('MSDN SQL: keywords and data types in upper case, names and functions as typed', () => {
  const win = fmtWin();
  const out = run(win, 'msdn');
  assert.equal(out.split('\n')[0], 'SELECT');
  assert.ok(out.includes('DECIMAL(12, 2)'), out);
  assert.ok(out.includes('count(o.id)'), out);
  assert.ok(out.includes('JOIN orders o ON o.user_id = u.id'), out);
  assert.ok(out.includes('\n  u.id,'), 'two spaces, as in Default');
});

test('Right aligned: the keywords end in one column', () => {
  const win = fmtWin();
  const lines = run(win, 'right').split('\n');
  const end = (re) => { const l = lines.find(x => re.test(x)); return l.indexOf(l.trim().split(' ')[0]) + l.trim().split(' ')[0].length; };
  assert.equal(end(/^\s*select u\.id/), end(/^\s*from /));
  assert.equal(end(/^\s*from /), end(/^\s*where /));
  assert.ok(lines.find(x => /^\s*from /.test(x)).startsWith('  '), lines.join('\n'));
});

test('ClickHouse keeps the case as typed under MSDN SQL too', () => {
  const win = fmtWin();
  const out = win.qhFormatSql('select toDate(ts), CAST(x AS UInt64) from events', 'clickhouse', win.qhFmtProfileOptions('msdn'));
  assert.ok(out.startsWith('select'), out);
  assert.ok(out.includes('UInt64'), out);
  assert.ok(out.includes('\n  events'), out);
});

// ---------- the Profile row in the popup ----------

function mountTabs(engine, store) {
  const ui = page(['qh-data.jsx', 'qh-modal.jsx', 'qh-editor.jsx', 'qh-sqlformat.jsx'], { prep: (win) => {
    for (const [k, v] of Object.entries(store || {})) win.localStorage.setItem(k, v);
    win.__qhSqlFormatter = { format };
  } });
  const noop = () => {};
  ui.render(ui.React.createElement(ui.win.EditorTabs, {
    tabs: [{ id: 'a', name: 'Query 1', tier: 'RO' }], activeId: 'a', onSelect: noop, onClose: noop, onNew: noop,
    wrap: false, onToggleWrap: noop, onCloseOthers: noop, onCloseRight: noop, onCloseAll: noop, onDuplicate: noop,
    onRename: noop, onCopySql: noop, onFormat: noop, fmtEngine: engine }));
  ui.click(ui.doc.querySelector('[aria-label="Format options"]'));
  return ui;
}
const selectOf = (ui, label) => ui.all('.qh-fmt-row').find(r => r.textContent.startsWith(label)).querySelector('select');
const optionTexts = (sel) => Array.from(sel.options).map(o => o.textContent);
const stored = (ui) => JSON.parse(ui.win.localStorage.getItem('qh.fmt.v1'));
const firstLine = (ui) => ui.doc.querySelector('.qh-fmt-prev').textContent.split('\n')[0];
function choose(ui, sel, value) {
  ui.React.act(() => { sel.value = value; sel.dispatchEvent(new ui.win.Event('change', { bubbles: true })); });
}

test('the Profile row comes first and shows Default with no stored options', opts, () => {
  const ui = mountTabs('postgres');
  const rows = ui.all('.qh-fmt-row');
  assert.equal(rows[0].textContent.startsWith('Profile'), true);
  assert.equal(rows[0].classList.contains('qh-fmt-prof'), true);
  const prof = selectOf(ui, 'Profile');
  assert.equal(prof.value, 'default');
  assert.deepEqual(optionTexts(prof), ['Default', 'Compact', 'Extended', 'Indented', 'MSDN SQL', 'Right aligned']);
  assert.deepEqual(optionTexts(selectOf(ui, 'Indent')), ['2 spaces', '4 spaces', 'Tab', 'No indent']);
});

test('a profile sets every option at once, stores them and the preview follows', opts, () => {
  const ui = mountTabs('postgres', { 'qh.fmt.v1': JSON.stringify({ identifierCase: 'lower', denseOperators: true }) });
  assert.equal(selectOf(ui, 'Profile').value, '', 'stored options that match no profile are Custom');
  assert.equal(optionTexts(selectOf(ui, 'Profile')).at(-1), 'Custom');
  choose(ui, selectOf(ui, 'Profile'), 'msdn');
  assert.deepEqual(stored(ui), ui.win.qhFmtProfileOptions('msdn'), 'the other options go back to their defaults');
  assert.equal(selectOf(ui, 'Keywords').value, 'upper');
  assert.equal(selectOf(ui, 'Data types').value, 'upper');
  assert.equal(selectOf(ui, 'Names').value, 'preserve');
  assert.equal(ui.all('.qh-fmt-check input')[0].checked, false, 'the dense box is off again');
  assert.equal(firstLine(ui), 'SELECT');
  assert.equal(optionTexts(selectOf(ui, 'Profile')).includes('Custom'), false, 'Custom goes away');
  choose(ui, selectOf(ui, 'Profile'), 'compact');
  assert.equal(selectOf(ui, 'Indent').value, '0');
  assert.equal(stored(ui).indent, '0');
  assert.equal(firstLine(ui), 'select');
});

test('a change to one option turns the profile into Custom; Reset gives Default', opts, () => {
  const ui = mountTabs('mssql', { 'qh.fmt.v1': JSON.stringify({ indentStyle: 'tabularRight' }) });
  assert.equal(selectOf(ui, 'Profile').value, 'right', 'the popup opens on the stored profile');
  choose(ui, selectOf(ui, 'Indent'), '4');
  assert.equal(selectOf(ui, 'Profile').value, '');
  assert.equal(optionTexts(selectOf(ui, 'Profile')).at(-1), 'Custom');
  choose(ui, selectOf(ui, 'Indent'), '2');
  assert.equal(selectOf(ui, 'Profile').value, 'right', 'back to the profile values = the profile again');
  ui.click(ui.all('.qh-fmt-foot button').find(b => b.textContent === 'Reset'));
  assert.equal(selectOf(ui, 'Profile').value, 'default');
});

test('on ClickHouse MSDN SQL is shown but the case rows stay locked', opts, () => {
  const ui = mountTabs('clickhouse');
  choose(ui, selectOf(ui, 'Profile'), 'msdn');
  assert.equal(selectOf(ui, 'Profile').value, 'msdn');
  assert.equal(stored(ui).keywordCase, 'upper', 'stored for the other engines');
  assert.equal(selectOf(ui, 'Keywords').disabled, true);
  assert.equal(selectOf(ui, 'Keywords').value, 'preserve');
  assert.equal(firstLine(ui), 'select');
});

// ---------- the labels fit the select (2026-11-11) ----------

test('every option label of the popup fits its select: at most 20 characters', () => {
  const win = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx', 'qh-sqlformat.jsx');
  const labels = win.QH_FMT_ROWS.flatMap(([, , list]) => list.map(([, l]) => l)).concat(win.QH_FMT_PROFILES.map(p => p[1]));
  const long = labels.filter(l => l.length > 20);
  assert.equal(long.join(' | '), '', 'macOS Chrome cut "Keywords in a column, right" (27)');
  const layout = win.QH_FMT_ROWS.find(r => r[0] === 'indentStyle')[2];
  assert.equal(layout.map(([, l]) => l).join(' | '), 'Standard | Left aligned | Right aligned');
});
