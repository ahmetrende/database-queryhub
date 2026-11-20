// Run asks for parameter values (2026-11-20), the app side. `submitToServer`
// opens a dialog when the SQL has :name, ${name} or {{name}} parameters. Continue
// sends the final SQL only. Batch and fleet refuse such a tab. The values stay in
// memory. qh-app.jsx renders the whole app when it loads, so the dialog's own
// source is cut out of the file and mounted for real (jsdom).
import test from 'node:test';
import assert from 'node:assert/strict';

import { page, sliceFunction, web, jsdomOpts as opts } from './_mount.mjs';

const APP = web('qh-app.jsx');
const between = (src, a, b) => {
  const i = src.indexOf(a);
  assert.ok(i >= 0, a + ' is in qh-app.jsx');
  const j = src.indexOf(b, i + a.length);
  assert.ok(j > i, b + ' follows ' + a);
  return src.slice(i, j);
};

test('submitToServer asks for the values before it sends anything', () => {
  const body = between(APP, 'const submitToServer = async', 'const pickStatement');
  assert.match(body, /askParams && !o\.paramsDone/);
  assert.match(body, /qhFindParams\(/);
  assert.match(body, /setParamAsk\(/);
  // The gate comes before the tab turns pending and before the request.
  const gate = body.indexOf('o.paramsDone');
  assert.ok(gate < body.indexOf("status: 'pending'"), 'the gate is before the pending patch');
  assert.ok(gate < body.indexOf('qhApi.submit('), 'the gate is before the request');
  assert.ok(gate > body.indexOf('That target is not available'), 'the gate is after the target check');
});

test('Continue and the confirm re-send pass paramsDone', () => {
  assert.match(APP, /submitToServer\(c\.id, c\.runAtISO, c\.sqlOverride, \{ confirmed: true, paramsDone: true \}\)/);
  assert.match(APP, /submitToServer\(a\.id, a\.runAtISO, finalSql, \{ \.\.\.a\.opts, paramsDone: true \}\)/);
});

test('the setting is qh.params.v1, on by default', () => {
  assert.match(APP, /localStorage\.getItem\('qh\.params\.v1'\) !== '0'/);
  assert.match(APP, /askParams=\{askParams\} onToggleAskParams=\{/);
});

test('batch and fleet refuse a tab with parameters', () => {
  assert.match(APP, /has parameters\. Run it on its own to fill them in\./);
  const batch = between(APP, 'const submitBatch = ', 'const submitFleet = ');
  assert.ok(batch.indexOf('paramsRefusal(') >= 0 && batch.indexOf('paramsRefusal(') < batch.indexOf('qhApi.submitBatch('));
  const fleet = between(APP, 'const submitFleet = ', 'const refreshScheduled');
  assert.ok(fleet.indexOf('paramsRefusal(') >= 0 && fleet.indexOf('paramsRefusal(') < fleet.indexOf('qhApi.submitBatch('));
});

test('the live verdict reads the preview text', () => {
  assert.match(APP, /qhParamPreviewText\(judged\.text, paramVals\.current\[activeId\] \|\| \{\}, paramEngine\)/);
  assert.match(APP, /useServerClassify\(verdictText, tab\.conn, tab\.db\)/);
  assert.match(APP, /qhClassify\(verdictText\)/);
});

test('parameter values never go to storage', () => {
  for (const line of APP.split('\n')) {
    if (line.includes('paramVals')) assert.ok(!/localStorage|sessionStorage/.test(line), line);
  }
  assert.doesNotMatch(APP, /(localStorage|sessionStorage)\.setItem\([^)]*(paramVals|values)/);
  assert.match(APP, /QhParamsDialog, qhAppParamTag/, 'both are on window');
});

const SQL = 'SELECT * FROM t WHERE a = :id AND b = :name';

function mount(props) {
  const ui = page(['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx']);
  sliceFunction('qh-app.jsx', 'qhAppParamTag', ui.win);
  const Dialog = sliceFunction('qh-app.jsx', 'QhParamsDialog', ui.win);
  const calls = { go: [], cancel: 0 };
  ui.render(ui.React.createElement(Dialog, {
    names: ['id', 'name'], initial: {}, engineId: 'postgres', sql: SQL,
    onContinue: (values, finalSql) => { calls.go.push({ values, finalSql }); },
    onCancel: () => { calls.cancel++; }, ...props }));
  const type = (el, v) => ui.React.act(() => {
    const set = Object.getOwnPropertyDescriptor(ui.win.HTMLInputElement.prototype, 'value').set;
    set.call(el, v);
    el.dispatchEvent(new ui.win.Event('input', { bubbles: true }));
  });
  return { ...ui, calls, type };
}

const rows = (ui) => ui.all('.qh-params-row');
const input = (ui, i) => rows(ui)[i].querySelector('input');
const modeBtn = (ui, i, label) => [...rows(ui)[i].querySelectorAll('button')].find(b => b.textContent === label);
const cont = (ui) => [...ui.doc.querySelectorAll('.qh-modal-foot button')].find(b => b.textContent === 'Continue');

test('two names give two rows, and the first field has the focus', opts, () => {
  const ui = mount();
  assert.equal(ui.doc.querySelector('.qh-modal-title').textContent, 'Parameter values');
  assert.equal(rows(ui).length, 2);
  assert.deepEqual(ui.all('.qh-params-name').map(e => e.textContent), [':id', ':name']);
  assert.equal(ui.doc.activeElement, input(ui, 0));
});

test('a Text value is quoted for the engine in the preview', opts, () => {
  const ui = mount();
  ui.type(input(ui, 1), "O'Brien");
  assert.match(ui.doc.querySelector('.qh-params-preview').textContent, /'O''Brien'/);
});

test('an empty SQL value disables Continue, and Enter sends the final SQL', opts, () => {
  const ui = mount();
  ui.type(input(ui, 1), "O'Brien");
  ui.click(modeBtn(ui, 0, 'SQL'));
  assert.equal(modeBtn(ui, 0, 'SQL').getAttribute('aria-pressed'), 'true');
  assert.equal(cont(ui).disabled, true, 'an empty SQL value');
  ui.key(input(ui, 0), 'Enter');
  assert.equal(ui.calls.go.length, 0, 'Enter does nothing while Continue is disabled');
  ui.type(input(ui, 0), '42');
  assert.equal(cont(ui).disabled, false);
  ui.key(input(ui, 0), 'Enter');
  assert.equal(ui.calls.go.length, 1);
  assert.equal(ui.calls.go[0].finalSql, "SELECT * FROM t WHERE a = 42 AND b = 'O''Brien'");
  assert.equal(ui.calls.go[0].values.id.mode, 'sql');
  assert.equal(ui.calls.go[0].values.name.v, "O'Brien");
});

test('the last values come back, and Escape cancels', opts, () => {
  const ui = mount({ initial: { id: { v: '7', mode: 'text', picked: true }, name: { v: 'x', mode: 'text', picked: false } } });
  assert.equal(input(ui, 0).value, '7');
  assert.equal(modeBtn(ui, 0, 'Text').getAttribute('aria-pressed'), 'true', 'a picked type stays');
  ui.key(input(ui, 0), 'Escape');
  assert.equal(ui.calls.cancel, 1);
  assert.equal(ui.calls.go.length, 0);
});
