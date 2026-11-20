// A query tab can carry a default schema for a PostgreSQL connection
// (2026-11-20). The submit sends it only when it is set. The person sees it in
// the target strip before Run. The approver sees it in the queue, and History
// shows it.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';
import { page, jsdomOpts, sliceFunction, web } from './_mount.mjs';

const W = loadInto(bareWindow(), 'qh-data.jsx');
const app = web('qh-app.jsx');
const css = web('QueryHub.html');

const PG = { id: 'c1', name: 'c1', engine: 'PostgreSQL 16', env: 'production', databases: [] };
const MS = { id: 'c2', name: 'c2', engine: 'SQL Server 2022', env: 'production', databases: [] };

// ---------- pure helpers (qh-data.jsx) ----------

test('only a PostgreSQL connection takes a default schema', () => {
  assert.equal(W.qhIsPostgresConn(PG), true);
  assert.equal(W.qhIsPostgresConn({ engine: 'aurora-postgresql' }), true);
  assert.equal(W.qhIsPostgresConn(MS), false);
  assert.equal(W.qhIsPostgresConn({ engine: 'ClickHouse 24' }), false);
  assert.equal(W.qhIsPostgresConn({ id: 'x' }), false, 'no engine string is not PostgreSQL');
  assert.equal(W.qhIsPostgresConn(null), false);
});

test('the tab sends a plain schema name on PostgreSQL, and null otherwise', () => {
  assert.equal(W.qhTabDefaultSchema(PG, 'app'), 'app');
  assert.equal(W.qhTabDefaultSchema(PG, 'a$1_b'), 'a$1_b');
  assert.equal(W.qhTabDefaultSchema(MS, 'app'), null, 'the server refuses it on SQL Server');
  assert.equal(W.qhTabDefaultSchema(PG, null), null);
  assert.equal(W.qhTabDefaultSchema(PG, ''), null);
  assert.equal(W.qhTabDefaultSchema(PG, 'My Schema'), null);
  assert.equal(W.qhTabDefaultSchema(PG, '1abc'), null);
  assert.equal(W.qhTabDefaultSchema(PG, 'a'.repeat(64)), null, 'longer than 63 characters');
  assert.equal(W.qhTabDefaultSchema(PG, 'a'.repeat(63)), 'a'.repeat(63));
});

test('the menu offers the plain names only', () => {
  assert.deepEqual(W.qhSchemaChoices(['app', 'My Schema', 'public', '9x']), ['app', 'public']);
  assert.deepEqual(W.qhSchemaChoices(null), []);
});

// ---------- the workspace keeps the schema ----------

test('the workspace saves the schema only when set, and reads it back', () => {
  const slim = W.qhWorkspaceSlim([{ id: 't1', sql: '', schema: 'app' }, { id: 't2', sql: '' }], 't1', 1).tabs;
  assert.equal(slim[0].schema, 'app');
  assert.equal('schema' in slim[1], false, 'a tab without one keeps the old record shape');
  assert.equal(W.qhWsHydrate(slim[0]).schema, 'app');
  assert.equal(W.qhWsHydrate(slim[1]).schema, null);
  assert.match(web('qh-data.jsx'), /const QH_WS_FIELDS = \[[^\]]*'schema'\]/, 'the merge compares it');
});

test('a schema set in another browser tab comes over in the merge', () => {
  const t = (over) => ({ id: 'a', name: 'Q', sql: 'select 1', conn: 'c1', db: 'd1', ...over });
  const ws = (tabs) => ({ tabs, activeId: 'a' });
  const merged = W.qhMergeWorkspace(ws([t()]), ws([t()]), ws([t({ schema: 'app' })]));
  assert.equal(merged.tabs[0].schema, 'app');
});

// ---------- the submit ----------

test('the submit sends defaultSchema only when the tab has one', () => {
  assert.match(app, /const defSchema = qhTabDefaultSchema\(conns\.find\(c => c\.id === cur\.conn\), cur\.schema\);/);
  assert.match(app, /\.\.\.\(defSchema \? \{ defaultSchema: defSchema \} : null\),/);
  // A batch item keeps its tab's schema too, sent only when set.
  const batch = app.slice(app.indexOf('const submitBatch = '), app.indexOf('const submitBatch = ') + 1500);
  assert.match(batch, /\.\.\.\(ds \? \{ defaultSchema: ds \} : null\)/);
});

test('a new target clears the schema, and the bar gets the schema props', () => {
  assert.match(app, /tab\.conn !== c\.id \|\| tab\.db !== d\.id \? \{ schema: null \} : null/);
  assert.match(app, /schemas=\{qhIsPostgresConn\(conn\) \? qhSchemaChoices\(editorSchema\.schemas\) : null\}/);
  assert.match(app, /schema=\{qhTabDefaultSchema\(conn, tab\.schema\)\} onSchema=\{\(v\) => patch\(activeId, \{ schema: v \}\)\}/);
  assert.match(app, /const showSchema = !!\(onSchema && conn && db && schemas && \(schemas\.length > 1 \|\| schema\)\);/);
  // The control sits after the database name, before the tier badge.
  const strip = app.slice(app.indexOf('<div className="qh-target">'), app.indexOf('<div className="qh-ab-spacer" />'));
  const db = strip.indexOf('qh-target-db'), pick = strip.indexOf('<QhSchemaPick'), tier = strip.indexOf('qh-target-tier');
  assert.ok(db > 0 && pick > db && tier > pick);
});

// ---------- the action bar ----------

const FILES = ['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-results-tools.jsx', 'qh-panels.jsx'];
function mountBar(extra) {
  const ui = page(FILES);
  const w = ui.win;
  w.useRef = ui.React.useRef; w.useState = ui.React.useState; w.useEffect = ui.React.useEffect;
  sliceFunction('qh-app.jsx', 'qhActionBarOverflows', w); sliceFunction('qh-app.jsx', 'qhFitActionBar', w);
  sliceFunction('qh-app.jsx', 'RunOnOpt', w); sliceFunction('qh-app.jsx', 'QhSchemaPick', w);
  w.QH_DSCHEMA_TITLE = 'Unqualified names resolve to this schema first. The approver sees it.';
  w.QH_KBD = { run: 'Ctrl Enter', newq: '', wrap: '', stmt: '' };
  w.MaskToggle = w.MaskToggle || (() => null);
  const Bar = sliceFunction('qh-app.jsx', 'ActionBar', w);
  const props = { pickHint: false, redacted: 0, onRevealRedacted: () => {}, why: '', onWhy: () => {}, conn: PG, db: { id: 'd1', name: 'd1', tier: 'RW' },
    connAlias: 'c1', dbAlias: 'd1', dbTier: 'RW',
    classify: { tier: 'RO', empty: false, multi: false, statements: [] }, pii: { columns: [], star: false }, autoApprove: false, tierExceedsGrant: false,
    busy: false, status: null, hasSql: true, onPrimary: () => {}, killed: false, riskHints: [], riskTop: 'low', onExplain: () => {}, tabCount: 1,
    onOpenBatch: () => {}, schedOpen: false, setSchedOpen: () => {}, onSchedule: () => {}, onCancelRun: () => {}, isSuper: false, unmasked: false,
    onUnmask: () => {}, runOnConn: null, runOn: null, onRunOn: () => {}, selLines: 0, schemas: null, schema: null, onSchema: () => {}, ...extra };
  ui.render(ui.React.createElement(Bar, props));
  return ui;
}

test('no schema control without two schemas, or without the schema props', jsdomOpts, () => {
  assert.equal(mountBar({ schemas: null }).doc.querySelector('.qh-dschema'), null, 'not PostgreSQL: the app passes null');
  assert.equal(mountBar({ schemas: ['public'] }).doc.querySelector('.qh-dschema'), null, 'one schema');
  assert.equal(mountBar({ schemas: ['app', 'public'], onSchema: undefined }).doc.querySelector('.qh-dschema'), null);
});

test('two schemas show the control with a placeholder, and a chosen name shows', jsdomOpts, () => {
  let ui = mountBar({ schemas: ['app', 'public'] });
  const btn = ui.doc.querySelector('.qh-dschema-toggle');
  assert.ok(btn);
  assert.equal(ui.doc.querySelector('.qh-dschema-ph').textContent, 'schema');
  assert.ok(!btn.classList.contains('is-set'));
  ui = mountBar({ schemas: ['app', 'public'], schema: 'app' });
  assert.equal(ui.doc.querySelector('.qh-dschema-v').textContent, 'app');
  assert.ok(ui.doc.querySelector('.qh-dschema-toggle').classList.contains('is-set'));
  ui = mountBar({ schemas: [], schema: 'app' });
  assert.ok(ui.doc.querySelector('.qh-dschema-v'), 'a schema that is set stays on screen');
});

test('the menu lists Server default first, and a pick reports the value', jsdomOpts, () => {
  const seen = [];
  const ui = mountBar({ schemas: ['app', 'public'], schema: 'app', onSchema: (v) => seen.push(v) });
  const click = (el) => ui.React.act(() => { el.dispatchEvent(new ui.win.MouseEvent('click', { bubbles: true })); });
  click(ui.doc.querySelector('.qh-dschema-toggle'));
  const items = [...ui.doc.querySelectorAll('.qh-dschema-pop [role="menuitemradio"]')];
  assert.deepEqual(items.map(i => i.querySelector('.qh-runon-t').textContent), ['Server default', 'app', 'public']);
  assert.equal(items[1].getAttribute('aria-checked'), 'true');
  click(items[0]);
  assert.deepEqual(seen, [null], 'Server default clears it');
  assert.equal(ui.doc.querySelector('.qh-dschema-pop'), null, 'the menu closes');
  click(ui.doc.querySelector('.qh-dschema-toggle'));
  click(ui.doc.querySelectorAll('.qh-dschema-pop [role="menuitemradio"]')[2]);
  assert.deepEqual(seen, [null, 'public']);
});

// ---------- History and the approval queue ----------

test('History rows and the queue show the schema chip', () => {
  assert.equal(W.qhHistoryRow({ id: 1, defaultSchema: 'app' }).defaultSchema, 'app');
  assert.equal(W.qhHistoryRow({ id: 1 }).defaultSchema, null);
  assert.match(web('qh-panels.jsx'), /\{h\.defaultSchema && <span className="qh-dschema-chip" title="Default schema for unqualified names">schema \{h\.defaultSchema\}<\/span>\}/);
  assert.match(web('qh-admin.jsx'), /\{it\.defaultSchema && <span className="qh-dschema-chip" title="Default schema for unqualified names">schema \{it\.defaultSchema\}<\/span>\}/);
});

test('the mock keeps the schema on a request and shows one in each list', () => {
  const mock = web('qh-api-mock.jsx');
  assert.match(mock, /defaultSchema: body\.defaultSchema \|\| null,/);
  assert.match(mock, /defaultSchema: 'app', submittedAt:/);
  assert.match(mock, /function mockHistSchema\(h\) \{ return h && h\.id === 'h2' \? 'app' : null; \}/);
});

// ---------- CSS ----------

test('the CSS block exists and the placeholder goes at fit step 2', () => {
  assert.match(css, /\/\* 2026-11-20 · Default schema of a tab/);
  assert.match(css, /\.qh-dschema-toggle \{/);
  assert.match(css, /\.qh-dschema-pop \{ position: fixed;/);
  assert.match(css, /\.qh-actionbar\[data-fit~="2"\] \.qh-dschema-ph \{ display: none; \}/);
  assert.match(css, /\.qh-dschema-v \{[^}]*max-width: 160px; overflow: hidden; text-overflow: ellipsis; \}/);
  assert.match(css, /\/\* 2026-11-20 · Default schema chip/);
  assert.match(css, /\.qh-dschema-chip \{/);
});
