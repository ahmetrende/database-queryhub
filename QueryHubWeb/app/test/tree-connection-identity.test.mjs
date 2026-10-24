// A connection row in the tree shows the cloud it runs on and its environment
// (2026-10-24, asked after the portal's design): the provider logo (24x14), then
// the env chip, in the row's right zone. Pinned here:
//   * the server row and the flat database row both carry them;
//   * a connection with no provider tag has no logo, and no empty box;
//   * the chip says PROD, STG or TEST, and QH_SHOW_ENV_TAGS still gates it;
//   * the logo and the chip never shrink, so the name ellipsizes first;
//   * the hover label is unchanged.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';
import { page, web, jsdomOpts as opts } from './_mount.mjs';

const CONNS = [
  { id: 'prod-main', name: 'prod-main', engine: 'PostgreSQL 15', env: 'production', host: 'prod-main.example.internal', port: 5432,
    tags: { provider: 'aws', service: 'RDS' }, databases: [{ id: 'orders', name: 'orders', tier: 'RO', tables: [] }] },
  { id: 'stage-app', name: 'stage-app', engine: 'PostgreSQL 15', env: 'staging',
    tags: { provider: 'huawei', service: 'RDS' }, databases: [{ id: 'app', name: 'app', tier: 'RW', tables: [] }] },
  { id: 'test-box', name: 'test-box', engine: 'PostgreSQL 15', env: 'test', databases: [{ id: 'scratch', name: 'scratch', tier: 'DDL', tables: [] }] },
  { id: 'old-box', name: 'old-box', engine: 'PostgreSQL 15', env: 'production', disabled: true,
    tags: { provider: 'onprem' }, databases: [{ id: 'legacy', name: 'legacy', tier: 'RO', tables: [] }] },
];

function mountTree(view) {
  const ui = page(['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx'], {
    prep: (win) => { win.localStorage.setItem('qh.treeview.v1', view); },
  });
  const noop = () => {};
  ui.render(ui.React.createElement(ui.win.Sidebar, {
    mode: 'conns', setMode: noop, conns: CONNS, connsLoading: false, schemaCache: {}, onLoadSchema: noop, onLoadRoles: noop, rolesCache: {},
    active: { conn: 'prod-main', db: 'orders' }, onPick: noop, bindOnClick: false, saved: [], onLoadSaved: noop, onDeleteSaved: noop,
    sessions: [], scheduled: [], history: [], onLoadHistory: noop, onNewQuery: noop, onNewTab: noop, onOpenTable: noop,
    onRequestEndpoint: noop, onRequestAuto: noop, onToast: noop, canRefresh: false, isSuper: false }));
  return ui;
}
// A server row has no node id; it is found by its name. A database row has one.
const rowFor = (ui, node) => (node.includes('/')
  ? ui.all('[data-node]').find(e => e.getAttribute('data-node') === node)
  : ui.all('.qh-tr').find(e => (e.querySelector('.qh-tr-label') || {}).textContent === node && !e.getAttribute('data-node')));
const chipText = (row) => (row.querySelector('.qh-envtag-sm') || {}).textContent;

test('the env chip word is one rule: PROD, STG, TEST, and an unknown word stays as it is', () => {
  const { qhEnvChipWord } = loadInto(bareWindow(), 'qh-data.jsx');
  assert.equal(qhEnvChipWord('production'), 'PROD');
  assert.equal(qhEnvChipWord('staging'), 'STG');
  assert.equal(qhEnvChipWord('test'), 'TEST');
  assert.equal(qhEnvChipWord('sandbox'), 'sandbox');
  assert.equal(qhEnvChipWord(null), '');
});

test('the env chip flag is on, with a dated comment', () => {
  const src = web('qh-data.jsx');
  assert.match(src, /const QH_SHOW_ENV_TAGS = true;/);
  assert.match(src, /2026-10-24: ON again/);
});

test('server view: every connection row has its logo and env chip', opts, () => {
  const ui = mountTree('servers');
  const prod = rowFor(ui, 'prod-main');
  assert.equal(chipText(prod), 'PROD');
  const logo = prod.querySelector('img.qh-prov-logo.qh-tr-prov');
  assert.ok(logo, 'the provider logo');
  assert.equal(logo.getAttribute('alt'), 'AWS');
  assert.match(logo.getAttribute('src'), /providers\/aws/);
  // The logo sits before the chip, and both are in the right zone, after the name.
  const kids = [...prod.children];
  const at = (el) => kids.indexOf(el);
  assert.ok(at(prod.querySelector('.qh-tr-label')) < at(logo) && at(logo) < at(prod.querySelector('.qh-envtag-sm')));
  assert.equal(chipText(rowFor(ui, 'stage-app')), 'STG');
  assert.match(rowFor(ui, 'stage-app').querySelector('img.qh-tr-prov').getAttribute('src'), /huawei/);
  assert.equal(chipText(rowFor(ui, 'test-box')), 'TEST');
});

test('a connection with no provider tag has no logo box at all', opts, () => {
  const ui = mountTree('servers');
  assert.equal(rowFor(ui, 'test-box').querySelector('img.qh-prov-logo'), null);
});

test('a disabled connection keeps its logo and chip and adds the off icon', opts, () => {
  const ui = mountTree('servers');
  const row = rowFor(ui, 'old-box');
  assert.ok(row.querySelector('img.qh-tr-prov'));
  assert.equal(chipText(row), 'PROD');
  assert.ok(row.querySelector('.qh-conn-off.is-icon'));
});

test('the hover label of a server row is unchanged', opts, () => {
  const ui = mountTree('servers');
  const tip = rowFor(ui, 'prod-main').getAttribute('data-tip');
  assert.match(tip, /^prod-main\nPostgreSQL 15 · production\nprod-main\.example\.internal:5432\nAWS · RDS/);
  assert.equal(rowFor(ui, 'prod-main').querySelector('.qh-prov-logo').getAttribute('title'), null, 'no second tooltip on the logo');
});

test('database view: the database row carries the identity of its connection', opts, () => {
  const ui = mountTree('dbs');
  const row = rowFor(ui, 'prod-main/orders');
  assert.equal(chipText(row), 'PROD');
  assert.ok(row.querySelector('img.qh-tr-prov'));
  assert.equal(chipText(rowFor(ui, 'test-box/scratch')), 'TEST');
});

test('the CSS: logo 24x14, and neither the logo nor the chip shrinks in a tree row', () => {
  const css = web('QueryHub.html');
  assert.match(css, /\.qh-tr-prov, \.qh-req-prov, \.qh-conf-prov \{ width: 24px; height: 14px;/);
  assert.match(css, /\.qh-tr > \.qh-prov-logo, \.qh-tr > \.qh-envtag-sm \{ flex: 0 0 auto; \}/);
  // The old rule let the chip shrink with weight 1000.
  assert.doesNotMatch(css, /\.qh-tr \.qh-pgchip, \.qh-tr \.qh-envtag-sm/);
});
