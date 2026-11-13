// Fleet run modal: one SQL text on many (connection, database) pairs. qh-app.jsx
// renders the whole app when it loads, so FleetModal's own source is cut out of
// the file and mounted for real (jsdom). The four qhFleet helpers are defined
// here with small stand-ins, so this test pins the modal and not the helpers.
// The action-bar entry button needs the whole App and is pinned by source text.
import test from 'node:test';
import assert from 'node:assert/strict';

import { page, sliceFunction, web, jsdomOpts as opts } from './_mount.mjs';

const RANK = { RO: 1, RW: 2, DDL: 3 };
const PAIRS = [
  { key: 'a|x', connectionId: 'a', databaseId: 'x', alias: 'exc-a', engine: 'PostgreSQL', env: 'production', tier: 'RW', tags: {}, owners: ['Team One'] },
  { key: 'a|y', connectionId: 'a', databaseId: 'y', alias: 'exc-a', engine: 'PostgreSQL', env: 'production', tier: 'RO', tags: {}, owners: ['Team One'] },
  { key: 'b|z', connectionId: 'b', databaseId: 'z', alias: 'pass-b', engine: 'PostgreSQL', env: 'staging', tier: 'DDL', tags: {}, owners: ['Team Two'] },
];

function mount({ tier = 'RW', props = {} } = {}) {
  const ui = page(['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx']);
  const w = ui.win;
  w.useState = ui.React.useState;
  w.useEffect = ui.React.useEffect;
  w.qhApi = {
    adminConnections: () => Promise.resolve({ connections: [] }),
    classify: () => Promise.resolve({ tier, requiresJustificationWhenReviewed: false }),
  };
  w.qhFleetPairs = () => PAIRS;
  w.qhFleetFilter = (pairs, f) => pairs.filter(p => (!f.glob || new RegExp('^' + f.glob.replace(/\*/g, '.*') + '$').test(p.alias))
    && (!f.engine || p.engine === f.engine) && (!f.env || p.env === f.env) && (!f.owner || p.owners.includes(f.owner)));
  w.qhFleetCovers = (p, need) => RANK[p.tier] >= RANK[need];
  w.qhFleetDefaultOnFailure = (t) => (t === 'RO' ? 'continue' : 'stop');
  w.qhNeedsWhyWhenReviewed = (v) => !!(v && v.requiresJustificationWhenReviewed);
  const Fleet = sliceFunction('qh-app.jsx', 'FleetModal', w);
  const calls = [];
  const tab = { conn: 'a', db: 'x', sql: 'UPDATE t SET a = 1' };
  ui.render(ui.React.createElement(Fleet, { tab, connections: [], onClose() {}, recent: [],
    onSubmit: (x) => { calls.push(x); return Promise.resolve(null); }, ...props }));
  return { ...ui, calls };
}

const rows = (ui) => ui.all('.qh-fleetm-row');
const box = (row) => row.querySelector('input[type=checkbox]');
const btn = (ui, text) => ui.all('button').find(b => b.textContent.startsWith(text));
const setVal = (ui, el, v) => ui.React.act(() => {
  const proto = el.tagName === 'SELECT' ? ui.win.HTMLSelectElement.prototype
    : el.tagName === 'TEXTAREA' ? ui.win.HTMLTextAreaElement.prototype : ui.win.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);
  el.dispatchEvent(new ui.win.Event(el.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
});

test('a pair that does not cover the SQL tier cannot be selected', opts, async () => {
  const ui = mount({ tier: 'RW' });
  await ui.flush();
  const ro = rows(ui)[1];
  assert.equal(box(ro).disabled, true);
  assert.match(ro.textContent, /Your access here is RO/);
  ui.click(box(ro));
  assert.equal(box(rows(ui)[1]).checked, false);
  assert.match(ui.doc.querySelector('.qh-fleetm-count').textContent, /^0 of 3 selected/);
});

test('the selection survives a filter change, and select all shown picks only covering rows', opts, async () => {
  const ui = mount({ tier: 'RW' });
  await ui.flush();
  ui.click(box(rows(ui)[0]));
  setVal(ui, ui.doc.querySelector('input[aria-label="Server name filter"]'), 'pass-*');
  assert.equal(rows(ui).length, 1);
  setVal(ui, ui.doc.querySelector('input[aria-label="Server name filter"]'), '');
  assert.equal(box(rows(ui)[0]).checked, true, 'still selected after the filter was cleared');
  setVal(ui, ui.doc.querySelector('input[aria-label="Server name filter"]'), 'exc-*');
  ui.click(btn(ui, 'Select all shown'));
  setVal(ui, ui.doc.querySelector('input[aria-label="Server name filter"]'), '');
  assert.deepEqual(rows(ui).map(r => box(r).checked), [true, false, false], 'the RO row and the hidden row stay off');
  ui.click(btn(ui, 'Clear all'));
  assert.match(ui.doc.querySelector('.qh-fleetm-count').textContent, /^0 of 3 selected/);
});

test('RW needs the typed count before submit enables', opts, async () => {
  const ui = mount({ tier: 'RW' });
  await ui.flush();
  const submit = () => btn(ui, 'Submit fleet run');
  assert.equal(submit().disabled, true, 'nothing selected');
  ui.click(box(rows(ui)[0]));
  ui.click(box(rows(ui)[2]));
  assert.equal(submit().textContent, 'Submit fleet run (2)');
  assert.equal(submit().disabled, true, 'the count is not typed yet');
  setVal(ui, ui.doc.querySelector('.qh-fleetm-confirm'), '1');
  assert.equal(submit().disabled, true);
  setVal(ui, ui.doc.querySelector('.qh-fleetm-confirm'), '2');
  assert.equal(submit().disabled, false);
});

test('an RO statement needs no typed count and defaults to continue', opts, async () => {
  const ui = mount({ tier: 'RO' });
  await ui.flush();
  assert.equal(ui.doc.querySelector('.qh-fleetm-confirm'), null);
  assert.equal(ui.doc.querySelector('input[name=qh-fleetm-of]:checked').parentNode.textContent.trim(), 'Continue after a failure');
});

test('onSubmit receives the frozen pair list, the failure choice and the reason', opts, async () => {
  const ui = mount({ tier: 'RW' });
  await ui.flush();
  ui.click(box(rows(ui)[0]));
  ui.click(box(rows(ui)[2]));
  ui.click(ui.all('input[name=qh-fleetm-of]')[1]);
  setVal(ui, ui.doc.querySelector('textarea'), 'Roll out the fix');
  setVal(ui, ui.doc.querySelector('.qh-fleetm-confirm'), '2');
  ui.click(btn(ui, 'Submit fleet run'));
  assert.equal(ui.calls.length, 1);
  assert.deepEqual(ui.calls[0].pairs.map(p => p.key), ['a|x', 'b|z']);
  assert.equal(ui.calls[0].onFailure, 'continue');
  assert.equal(ui.calls[0].why, 'Roll out the fix');
});

test('a server refusal shows verbatim, keeps the modal open and keeps the selection', opts, async () => {
  const ui = mount({ tier: 'RW', props: { onSubmit: () => Promise.resolve('Refused: exc-a / x has no access.') } });
  await ui.flush();
  ui.click(box(rows(ui)[0]));
  setVal(ui, ui.doc.querySelector('.qh-fleetm-confirm'), '1');
  ui.click(btn(ui, 'Submit fleet run'));
  await ui.flush();
  assert.equal(ui.doc.querySelector('.qh-fleetm-err').textContent, 'Refused: exc-a / x has no access.');
  assert.equal(box(rows(ui)[0]).checked, true);
});

test('the action bar shows the entry only to an admin, and the app wires it', () => {
  const src = web('qh-app.jsx');
  assert.match(src, /\{isAdminUser && \(\s*<button[^>]*onClick=\{onOpenFleet\}/);
  assert.match(src, /isAdminUser=\{isAdminUser\} onOpenFleet=\{\(\) => setFleetOpen\(true\)\}/);
  assert.match(src, /fleet: true, onFailure/);
});
