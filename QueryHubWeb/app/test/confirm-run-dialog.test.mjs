// "Confirm before this runs" (2026-10-24): a red icon tile, the target as a chip
// (provider logo, env tag, engine logo, name / db), the statement echoed in a
// scrollable code box, a solid red primary button and a ghost Cancel. Keyboard:
// Cancel holds the focus, so Enter cancels and only a click confirms. Escape
// cancels. qh-app.jsx renders the whole app when it loads, so the dialog's own
// source is cut out of the file and mounted for real (jsdom).
import test from 'node:test';
import assert from 'node:assert/strict';

import { page, sliceFunction, web, jsdomOpts as opts } from './_mount.mjs';

const CONN = { id: 'prod-main', name: 'prod-main', engine: 'PostgreSQL 15', env: 'production', tags: { provider: 'aws', service: 'RDS' } };
const SQL = 'DELETE FROM payouts\nWHERE status = \'failed\';';

function mount(props) {
  const ui = page(['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx']);
  const Confirm = sliceFunction('qh-app.jsx', 'ConfirmRunModal', ui.win);
  const calls = { go: 0, cancel: 0 };
  ui.render(ui.React.createElement(Confirm, {
    reasons: ['This statement deletes every row that matches.'], target: 'prod-main / payments', env: 'production', tier: 'RW',
    scheduled: false, sql: SQL, conn: CONN, onConfirm: () => { calls.go++; }, onCancel: () => { calls.cancel++; }, ...props }));
  return { ...ui, calls };
}

test('the dialog shows the icon tile, the target chip and the statement', opts, () => {
  const ui = mount();
  const q = (s) => ui.doc.querySelector(s);
  assert.ok(q('.qh-conf-ic svg'), 'the red icon tile');
  const chip = q('.qh-conf-target');
  const order = ['.qh-conf-prov', '.qh-envtag-sm', '.qh-conf-eng', '.qh-conf-tgt'].map(s => [...chip.children].indexOf(chip.querySelector(s)));
  assert.deepEqual(order, [0, 1, 2, 3], 'provider logo, env tag, engine logo, name / db');
  assert.equal(chip.querySelector('.qh-envtag-sm').textContent, 'PROD');
  assert.equal(chip.querySelector('.qh-conf-tgt').textContent, 'prod-main / payments');
  assert.equal(q('.qh-conf-sql').textContent, SQL, 'the statement, as written');
  assert.equal(q('.qh-conf-sql').tagName, 'PRE');
  assert.match(q('.qh-modal-title').textContent, /Confirm before this runs/);
});

test('the statement is text, never markup', opts, () => {
  const ui = mount({ sql: 'SELECT \'<img src=x onerror=alert(1)>\'' });
  assert.equal(ui.doc.querySelector('.qh-conf-sql img'), null);
  assert.match(ui.doc.querySelector('.qh-conf-sql').textContent, /<img src=x/);
});

test('no statement and no connection: the dialog still renders', opts, () => {
  const ui = mount({ sql: null, conn: null, env: null });
  assert.equal(ui.doc.querySelector('.qh-conf-sql'), null);
  assert.equal(ui.doc.querySelector('.qh-conf-prov'), null);
  assert.ok(ui.doc.querySelector('.qh-conf-reasons li'));
});

test('Cancel holds the focus, so Enter cancels, and the red button is a click', opts, () => {
  const ui = mount();
  const cancel = [...ui.doc.querySelectorAll('.qh-modal-foot button')].find(b => b.textContent === 'Cancel');
  assert.equal(cancel.hasAttribute('data-autofocus'), true);
  assert.equal(ui.doc.activeElement, cancel);
  const go = ui.doc.querySelector('.qh-conf-go');
  assert.equal(go.textContent, 'Run it');
  assert.ok(!/qh-btn-danger/.test(go.className), 'the solid red style, not the outlined one');
  ui.click(go);
  assert.deepEqual([ui.calls.go, ui.calls.cancel], [1, 0]);
  ui.click(cancel);
  assert.deepEqual([ui.calls.go, ui.calls.cancel], [1, 1]);
});

test('Escape cancels', opts, () => {
  const ui = mount();
  ui.key(ui.doc.querySelector('.qh-conf-sql'), 'Escape');
  assert.equal(ui.calls.cancel, 1);
  assert.equal(ui.calls.go, 0);
});

test('a scheduled run says so on the button and in the lead sentence', opts, () => {
  const ui = mount({ scheduled: true });
  assert.equal(ui.doc.querySelector('.qh-conf-go').textContent, 'Schedule it');
  assert.match(ui.doc.querySelector('.qh-modal-sub').textContent, /QueryHub did not set the schedule/);
});

test('the app hands the dialog the statement and the connection', () => {
  const src = web('qh-app.jsx');
  assert.match(src, /sql: sqlOverride \|\| cur\.sql, conn: c2 \|\| null/);
  assert.match(src, /sql=\{confirmRun\.sql\} conn=\{confirmRun\.conn\}/);
});

test('the CSS: the code box stops at 8 lines and scrolls, the button is solid red', () => {
  const css = web('QueryHub.html');
  assert.match(css, /\.qh-conf-sql \{[^}]*max-height: calc\(8 \* 1\.5em \+ 22px\);[^}]*overflow: auto;[^}]*font-family: var\(--qh-mono\);/);
  assert.match(css, /\.qh-conf-go \{ font-weight: 700; background: var\(--danger-solid-medium\); color: var\(--fg-fix-white\);/);
  assert.match(css, /\.qh-conf-ic \{[^}]*background: var\(--danger-solid-medium\)/);
});
