// The PROD tab colour (2026-11-10).
//
// The operator chose: a PROD tab has a thin orange top line, every other tab
// has no colour. PROD means the words of the PROD env chip ('production',
// 'prod'). STG, TEST, an unknown env and a tab with no target get nothing. The
// tooltip adds "PROD".
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';
import { page, jsdomOpts, web } from './_mount.mjs';

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');
const tab = (id, env, over) => ({ id, name: id, conn: 'srv-alpha', db: 'shop', tier: 'RO', env, ...over });

// ---------- the rule ----------

test('only a production tab is PROD', () => {
  assert.equal(W.qhTabIsProd(tab('a', 'production')), true);
  assert.equal(W.qhTabIsProd(tab('a', 'prod')), true);
  assert.equal(W.qhTabIsProd(tab('a', ' Production ')), true, 'the chip rule trims and ignores case');
  for (const env of ['staging', 'stg', 'test', 'qa', '', undefined, null]) {
    assert.equal(W.qhTabIsProd(tab('a', env)), false, String(env));
  }
});

test('a tab with no target and a special page are never PROD', () => {
  assert.equal(W.qhTabIsProd(tab('a', 'production', { unpicked: true })), false, 'New query');
  assert.equal(W.qhTabIsProd({ id: 'welcome', kind: 'welcome', name: 'Welcome', env: 'production' }), false);
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

test('the PROD tab has the class and the tooltip word, the others do not', jsdomOpts, () => {
  const ui = mountTabs([
    tab('shop (srv-alpha)', 'production', { reqId: 4211 }),
    tab('shop (srv-beta)', 'staging'),
    tab('New query', undefined, { conn: null, db: null, unpicked: true }),
    tab('shop (srv-gamma)', 'prod'),
  ], 'shop (srv-beta)');
  const el = ui.all('.qh-tab');
  assert.deepEqual(el.map(e => e.classList.contains('is-prod')), [true, false, false, true]);
  assert.equal(el[0].getAttribute('title'), 'shop (srv-alpha) · request #4211 · PROD');
  assert.equal(el[1].getAttribute('title'), 'shop (srv-beta)');
  assert.equal(el[2].getAttribute('title'), 'New query');
  assert.equal(el[3].getAttribute('title'), 'shop (srv-gamma) · PROD');
  assert.ok(el[1].classList.contains('is-active'), 'the active marker stays');
  assert.ok(el[0].querySelector('.qh-tab-dot.tier-ro'), 'the tier dot stays');
});

// ---------- the wiring and the colour ----------

test('the app gives each tab of the strip its connection env', () => {
  const app = web('qh-app.jsx');
  assert.match(app, /const tabsForBar = tabs\.map\(x => \(\{ \.\.\.x, tier: qhClassify\(x\.sql\)\.tier,\s+env: x\.kind \? undefined : \(\(conns\.find\(c => c\.id === x\.conn\) \|\| \{\}\)\.env\) \}\)\);/);
});

test('the top line uses the PROD chip colour, in light and in dark', () => {
  const html = web('QueryHub.html');
  const chipLight = html.match(/\.qh-envtag-sm\.env-production \{ background: (var\(--[a-z-]+\))/);
  const chipDark = html.match(/\[data-theme='dark'\] \.qh-envtag-sm\.env-production \{ background: (var\(--[a-z-]+\))/);
  assert.ok(chipLight && chipDark, 'the PROD chip rules exist');
  const lineLight = html.match(/\n\s*\.qh-tab\.is-prod::before \{[^}]*top: 0; height: 2px; background: (var\(--[a-z-]+\))/);
  const lineDark = html.match(/\[data-theme='dark'\] \.qh-tab\.is-prod::before \{ background: (var\(--[a-z-]+\))/);
  assert.ok(lineLight && lineDark, 'the tab rules exist');
  assert.equal(lineLight[1], chipLight[1], 'light: the same token as the chip');
  assert.equal(lineDark[1], chipDark[1], 'dark: the same token as the chip');
});
