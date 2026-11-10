// Tab names and widths (2026-11-09).
//
//   * A tab that a person did not rename is auto-named: `DB_NAME (SERVER_NAME)`,
//     with the full connection alias. A repeat gets the smallest free number.
//     A number stays when another tab closes.
//   * A tab with no target yet is `New query`, with a grey dot.
//   * A renamed tab keeps its name. An empty rename makes it auto-named again.
//   * The strip shows no request number. The tooltip has the name and the number.
//   * The tab width comes from CSS flex, not from JavaScript.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';
import { page, jsdomOpts, web } from './_mount.mjs';

const H = loadInto(bareWindow(), 'qh-data.jsx');

const conns = [
  { id: 'srv-alpha', name: 'srv-alpha', databases: [{ id: 'shop', name: 'shop' }, { id: 'ledger', name: 'ledger' }] },
  { id: 'srv-beta', name: 'srv-beta', databases: [{ id: 'shop', name: 'shop' }] },
];
const tab = (id, over) => ({ id, name: '', sql: '', conn: 'srv-alpha', db: 'shop', autoName: true, ...over });
const names = (ts) => ts.map(t => t.name);

// ---------- the rules ----------

test('an auto name is DB_NAME (SERVER_NAME) with the full alias', () => {
  assert.equal(H.qhAutoTabName(tab('a'), conns, []), 'shop (srv-alpha)');
  assert.equal(H.qhAutoTabName(tab('a', { conn: 'srv-beta' }), conns, []), 'shop (srv-beta)');
});

test('a repeat gets 2, then 3', () => {
  const ts = H.qhNameTabs([tab('a'), tab('b'), tab('c')], conns);
  assert.deepEqual(names(ts), ['shop (srv-alpha)', 'shop (srv-alpha) 2', 'shop (srv-alpha) 3']);
});

test('a closed tab frees its number, and nothing is renumbered', () => {
  const ts = H.qhNameTabs([tab('a'), tab('b'), tab('c')], conns);
  const left = ts.filter(t => t.id !== 'b');
  assert.equal(H.qhNameTabs(left, conns), left, 'closing a tab renames nobody');
  assert.deepEqual(names(left), ['shop (srv-alpha)', 'shop (srv-alpha) 3']);
  const more = H.qhNameTabs([...left, tab('d')], conns);
  assert.equal(more[2].name, 'shop (srv-alpha) 2', 'the smallest free number is used again');
});

test('a tab that changes its target gets a new name and the others keep theirs', () => {
  const ts = H.qhNameTabs([tab('a'), tab('b', { db: 'ledger' })], conns);
  assert.deepEqual(names(ts), ['shop (srv-alpha)', 'ledger (srv-alpha)']);
  const moved = H.qhNameTabs(ts.map(t => (t.id === 'b' ? { ...t, db: 'shop' } : t)), conns);
  assert.deepEqual(names(moved), ['shop (srv-alpha)', 'shop (srv-alpha) 2']);
});

test('a renamed tab keeps its name when its database changes', () => {
  const ts = H.qhNameTabs([tab('a', { name: 'month end', autoName: false, db: 'ledger' })], conns);
  assert.equal(ts[0].name, 'month end');
  const moved = H.qhNameTabs([{ ...ts[0], db: 'shop' }], conns);
  assert.equal(moved[0].name, 'month end');
});

test('an empty rename makes the tab auto-named again', () => {
  // renameTab in qh-app.jsx sets autoName: true for an empty name.
  const ts = H.qhNameTabs([tab('a', { name: 'month end', autoName: true })], conns);
  assert.equal(ts[0].name, 'shop (srv-alpha)');
  const app = web('qh-app.jsx');
  assert.match(app, /if \(!nm\) \{ if \(cur\.autoName !== true\) patch\(id, \{ autoName: true \}\); return; \}/);
  assert.match(app, /patch\(id, \{ name: nm, autoName: false \}\)/);
});

test('a tab with no target is New query', () => {
  assert.equal(H.qhAutoTabName(tab('a', { unpicked: true }), conns, []), 'New query');
  assert.equal(H.qhAutoTabName(tab('a', { conn: null, db: null }), conns, []), 'New query');
  assert.equal(H.qhTabNoTarget(tab('a', { unpicked: true })), true);
  assert.equal(H.qhTabNoTarget(tab('a')), false);
  assert.equal(H.qhTabNoTarget({ kind: 'welcome' }), false, 'Welcome is not a query tab');
});

test('New query never gets a number, also when a tab loses its target', () => {
  // Two tabs already show New query. A third tab loses its database. It is
  // New query too, not New query 2.
  const open = [tab('a', { name: 'New query', unpicked: true }), tab('b', { name: 'New query', unpicked: true })];
  const lost = tab('c', { name: 'shop (srv-alpha)', unpicked: true });
  assert.equal(H.qhAutoTabName(lost, conns, open), 'New query');
  const named = H.qhNameTabs([...open, lost], conns);
  assert.deepEqual(named.map((t) => t.name), ['New query', 'New query', 'New query']);
});

test('a tab loaded without the flag is migrated by its name', () => {
  const legacy = (name, over) => ({ id: name, name, sql: '', conn: 'srv-alpha', db: 'ledger', ...over });
  const ts = H.qhNameTabs([
    legacy('Untitled query'), legacy('Query 4'), legacy('srv-alpha'),
    legacy('ledger (srv-alpha)'), legacy('month end'),
  ], conns);
  assert.deepEqual(ts.map(t => t.autoName), [true, true, true, true, false]);
  assert.equal(ts[4].name, 'month end', 'a manual name is kept');
  assert.equal(ts[3].name, 'ledger (srv-alpha)', 'a name that already fits is kept');
  assert.deepEqual([ts[0].name, ts[1].name, ts[2].name],
    ['ledger (srv-alpha) 2', 'ledger (srv-alpha) 3', 'ledger (srv-alpha) 4'], 'the others are numbered');
});

test('a legacy tab waits for the connections before it is decided', () => {
  const t = { id: 'x', name: 'srv-alpha', conn: 'srv-alpha', db: 'shop' };
  assert.equal(H.qhTabIsAuto(t, []), null);
  const ts = [t];
  assert.equal(H.qhNameTabs(ts, []), ts, 'no change without connections');
});

test('kinds keep their names', () => {
  const ts = [{ id: 'welcome', kind: 'welcome', name: 'Welcome', conn: 'srv-alpha', db: 'shop' }];
  assert.equal(H.qhNameTabs(ts, conns), ts);
});

test('the app saves the flag in the workspace and in a session', () => {
  const app = web('qh-app.jsx');
  assert.match(app, /const slim = [\s\S]*?autoName: t\.autoName/, 'the workspace saves it');
  assert.match(app, /autoName: \(t\.autoName === true \|\| t\.autoName === false\) \? t\.autoName : undefined/, 'the workspace loads it');
  assert.match(app, /connectionId: t\.conn, databaseId: t\.db, autoName: t\.autoName/, 'a server session sends it');
  assert.match(app, /setTabs\(ts => qhNameTabs\(ts, conns\)\)/, 'the app names the tabs');
});

// ---------- the strip, mounted ----------

function mountTabs(tabs, activeId) {
  const ui = page(['qh-data.jsx', 'qh-modal.jsx', 'qh-editor.jsx']);
  const noop = () => {};
  ui.render(ui.React.createElement(ui.win.EditorTabs, {
    tabs, activeId, onSelect: noop, onClose: noop, onNew: noop, wrap: false, onToggleWrap: noop, onFormat: noop,
    onCloseOthers: noop, onCloseRight: noop, onCloseAll: noop, onDuplicate: noop, onRename: noop,
    onCopySql: noop, onDownloadSql: noop, onReorder: noop }));
  return ui;
}

test('the strip shows no request number, and the tooltip has it', jsdomOpts, () => {
  const ui = mountTabs([
    { id: 'a', name: 'shop (srv-alpha)', conn: 'srv-alpha', db: 'shop', tier: 'RO', reqId: 4211 },
    { id: 'b', name: 'ledger (srv-alpha)', conn: 'srv-alpha', db: 'ledger', tier: 'RW', reqId: 4212 },
  ], 'a');
  const tabsEl = ui.all('.qh-tab');
  assert.equal(ui.all('.qh-tab-req').length, 0, 'no #N chip');
  assert.doesNotMatch(tabsEl[0].textContent, /#4211/);
  assert.equal(tabsEl[0].getAttribute('title'), 'shop (srv-alpha) · request #4211', 'the active tab has a tooltip');
  assert.equal(tabsEl[1].getAttribute('title'), 'ledger (srv-alpha) · request #4212');
});

test('a New query tab has a grey dot, a tab with a target has its tier colour', jsdomOpts, () => {
  const ui = mountTabs([
    { id: 'a', name: 'New query', conn: 'srv-alpha', db: 'shop', unpicked: true, tier: 'RO' },
    { id: 'b', name: 'shop (srv-alpha)', conn: 'srv-alpha', db: 'shop', tier: 'RW' },
  ], 'a');
  const dots = ui.all('.qh-tab-dot');
  assert.ok(dots[0].classList.contains('tier-none'), 'grey dot');
  assert.ok(!dots[0].classList.contains('tier-ro'));
  assert.ok(dots[1].classList.contains('tier-rw'));
  assert.equal(ui.all('.qh-tab')[0].getAttribute('title'), 'New query', 'no request number, only the name');
});

// ---------- the widths ----------

test('a tab grows to its name and shrinks with CSS flex', () => {
  const html = web('QueryHub.html');
  assert.match(html, /\.qh-tab \{[^}]*flex: 0 1 auto; min-width: 104px; max-width: 320px;/);
  assert.match(html, /\.qh-tab \.qh-tab-name \{ flex: 1 1 auto; min-width: 0; \}/, 'the name gets the ellipsis');
  assert.match(html, /\.qh-tab-dot\.tier-none \{ background: var\(--fg-tertiary\); \}/);
});
