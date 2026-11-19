// The editor tab strip works from the keyboard and with a screen reader (2026-11-19).
//
//   * The strip is a tablist. Each tab is a tab with an id and aria-selected.
//   * Roving tabindex: only the active tab is in the Tab order.
//   * ArrowRight / ArrowLeft go to the next / previous tab and wrap around.
//     Home / End go to the first / last tab. Each one selects the tab and focuses it.
//   * Delete closes the focused tab. F2 renames it. Enter and Space select it.
//   * Keys typed in the rename input do not move or close tabs.
import test from 'node:test';
import assert from 'node:assert/strict';

import { page, jsdomOpts } from './_mount.mjs';

const TABS = [
  { id: 'a', name: 'shop (srv-alpha)', conn: 'srv-alpha', db: 'shop', tier: 'RO' },
  { id: 'b', name: 'ledger (srv-alpha)', conn: 'srv-alpha', db: 'ledger', tier: 'RW' },
  { id: 'c', name: 'shop (srv-beta)', conn: 'srv-beta', db: 'shop', tier: 'RO' },
];

function mountTabs(tabs, activeId) {
  const ui = page(['qh-data.jsx', 'qh-modal.jsx', 'qh-editor.jsx']);
  const noop = () => {};
  const calls = { select: [], close: [], rename: [] };
  ui.render(ui.React.createElement(ui.win.EditorTabs, {
    tabs, activeId,
    onSelect: (id) => calls.select.push(id),
    onClose: (id) => calls.close.push(id),
    onRename: (id, name) => calls.rename.push([id, name]),
    onNew: noop, wrap: false, onToggleWrap: noop, onFormat: noop,
    onCloseOthers: noop, onCloseRight: noop, onCloseAll: noop, onDuplicate: noop,
    onCopySql: noop, onDownloadSql: noop, onReorder: noop }));
  const tab = (id) => ui.doc.getElementById('qh-tab-' + id);
  return { ...ui, calls, tab };
}

test('the strip is a tablist and each tab has its role and aria state', jsdomOpts, () => {
  const ui = mountTabs(TABS, 'b');
  const list = ui.doc.querySelector('.qh-tabs');
  assert.equal(list.getAttribute('role'), 'tablist');
  assert.equal(list.getAttribute('aria-label'), 'Open tabs');
  const tabsEl = ui.all('.qh-tab');
  assert.deepEqual(tabsEl.map(el => el.id), ['qh-tab-a', 'qh-tab-b', 'qh-tab-c']);
  assert.deepEqual(tabsEl.map(el => el.getAttribute('role')), ['tab', 'tab', 'tab']);
  assert.deepEqual(tabsEl.map(el => el.getAttribute('aria-selected')), ['false', 'true', 'false']);
  assert.equal(tabsEl[0].getAttribute('aria-keyshortcuts'), 'Delete F2');
});

test('only the active tab is in the Tab order', jsdomOpts, () => {
  const ui = mountTabs(TABS, 'b');
  assert.deepEqual(ui.all('.qh-tab').map(el => el.tabIndex), [-1, 0, -1]);
});

test('a welcome tab lists Delete but not F2', jsdomOpts, () => {
  const ui = mountTabs([{ id: 'w', name: 'Welcome', kind: 'welcome' }, ...TABS], 'w');
  assert.equal(ui.tab('w').getAttribute('aria-keyshortcuts'), 'Delete');
});

test('ArrowRight goes to the next tab and wraps from the last to the first', jsdomOpts, () => {
  const ui = mountTabs(TABS, 'a');
  ui.key(ui.tab('a'), 'ArrowRight');
  assert.deepEqual(ui.calls.select, ['b']);
  assert.equal(ui.doc.activeElement, ui.tab('b'), 'focus moves to the next tab');
  ui.key(ui.tab('c'), 'ArrowRight');
  assert.deepEqual(ui.calls.select, ['b', 'a'], 'the last tab wraps to the first');
  assert.equal(ui.doc.activeElement, ui.tab('a'));
});

test('ArrowLeft goes to the previous tab and wraps from the first to the last', jsdomOpts, () => {
  const ui = mountTabs(TABS, 'b');
  ui.key(ui.tab('b'), 'ArrowLeft');
  assert.deepEqual(ui.calls.select, ['a']);
  ui.key(ui.tab('a'), 'ArrowLeft');
  assert.deepEqual(ui.calls.select, ['a', 'c'], 'the first tab wraps to the last');
  assert.equal(ui.doc.activeElement, ui.tab('c'));
});

test('Home and End go to the first and the last tab', jsdomOpts, () => {
  const ui = mountTabs(TABS, 'b');
  ui.key(ui.tab('b'), 'End');
  assert.deepEqual(ui.calls.select, ['c']);
  assert.equal(ui.doc.activeElement, ui.tab('c'));
  ui.key(ui.tab('c'), 'Home');
  assert.deepEqual(ui.calls.select, ['c', 'a']);
  assert.equal(ui.doc.activeElement, ui.tab('a'));
});

test('Enter and Space select the focused tab', jsdomOpts, () => {
  const ui = mountTabs(TABS, 'a');
  ui.key(ui.tab('c'), 'Enter');
  ui.key(ui.tab('b'), ' ');
  assert.deepEqual(ui.calls.select, ['c', 'b']);
});

test('Delete closes the focused tab', jsdomOpts, () => {
  const ui = mountTabs(TABS, 'a');
  ui.key(ui.tab('b'), 'Delete');
  assert.deepEqual(ui.calls.close, ['b']);
});

test('F2 starts one rename, of the focused tab', jsdomOpts, () => {
  const ui = mountTabs(TABS, 'a');
  // Tab b is focused but not active. The window F2 handler renames the active
  // tab, so a second rename would move the input to tab a.
  ui.key(ui.tab('b'), 'F2');
  const inputs = ui.all('.qh-tab-rename');
  assert.equal(inputs.length, 1, 'one rename input');
  assert.ok(ui.tab('b').contains(inputs[0]), 'the input is in the focused tab');
  assert.equal(inputs[0].value, 'ledger (srv-alpha)');
});

test('F2 on a welcome tab starts no rename', jsdomOpts, () => {
  const ui = mountTabs([{ id: 'w', name: 'Welcome', kind: 'welcome' }, ...TABS], 'w');
  ui.key(ui.tab('w'), 'F2');
  assert.equal(ui.all('.qh-tab-rename').length, 0);
});

test('keys typed in the rename input do not move, select or close tabs', jsdomOpts, () => {
  const ui = mountTabs(TABS, 'a');
  ui.key(ui.tab('b'), 'F2');
  const input = ui.doc.querySelector('.qh-tab-rename');
  for (const k of ['ArrowRight', 'ArrowLeft', 'Home', 'End', ' ', 'Delete', 'F2']) ui.key(input, k);
  assert.deepEqual(ui.calls.select, [], 'no tab is selected');
  assert.deepEqual(ui.calls.close, [], 'no tab closes');
  assert.equal(ui.all('.qh-tab-rename').length, 1, 'the rename stays open');
  assert.ok(ui.tab('b').contains(ui.doc.querySelector('.qh-tab-rename')));
});

test('a tab in rename does not close on Delete and does not list it', jsdomOpts, () => {
  const ui = mountTabs(TABS, 'a');
  ui.key(ui.tab('b'), 'F2');
  assert.equal(ui.tab('b').getAttribute('aria-keyshortcuts'), 'F2');
  ui.key(ui.tab('b'), 'Delete');
  assert.deepEqual(ui.calls.close, []);
});

test('a keyboard context menu opens under the tab', jsdomOpts, () => {
  const ui = mountTabs(TABS, 'a');
  ui.React.act(() => ui.tab('b').dispatchEvent(new ui.win.MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 0, clientY: 0 })));
  const menu = ui.doc.querySelector('.qh-ctxmenu');
  assert.ok(menu, 'the menu opens');
  assert.equal(menu.querySelector('.qh-ctx-title').textContent, 'ledger (srv-alpha)');
});
