// The request-access dialog (2026-10-24): the provider logo and env chip on the
// rows of the picker, one sentence under each tier, RW and DDL locked on a
// read-only engine with a reason, and a hint that a DBA reads the reason. The
// free-text new-endpoint path stays, and so does the 5-character minimum.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';
import { page, web, jsdomOpts as opts } from './_mount.mjs';

const ROWS = [
  { connectionId: 'prod-main', name: 'prod-main', engine: 'postgres', env: 'production', databases: ['payments', 'ledger'], partial: false,
    tags: { provider: 'aws', service: 'RDS' } },
  { connectionId: 'archive', name: 'archive', engine: 'athena', env: 'production', databases: ['events'], partial: true,
    tags: { provider: 'aws', service: 'Athena' } },
  { connectionId: 'scratch', name: 'scratch', engine: 'postgres', env: 'test', databases: ['tmp'], partial: false },
];

function mount() {
  const ui = page(['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx']);
  const sent = [];
  ui.render(ui.React.createElement(ui.win.RequestAccessModal, {
    onClose: () => {}, onSubmit: (r) => sent.push(r), load: () => Promise.resolve({ connections: ROWS }) }));
  return { ...ui, sent };
}
const type = (ui, el, v) => {
  const proto = el.tagName === 'TEXTAREA' ? ui.win.HTMLTextAreaElement : ui.win.HTMLInputElement;
  const set = Object.getOwnPropertyDescriptor(proto.prototype, 'value').set;
  ui.React.act(() => { set.call(el, v); el.dispatchEvent(new ui.win.Event('input', { bubbles: true })); });
};
const rowBtn = (ui, name) => ui.all('.qh-reqrow').find(b => b.querySelector('.qh-reqrow-name').textContent.startsWith(name));
const tierBtn = (ui, word) => ui.all('.qh-tierpick button').find(b => b.textContent.endsWith(word));

test('the rows of the picker show the cloud logo and the env chip', opts, async () => {
  const ui = mount();
  await ui.flush();
  const main = rowBtn(ui, 'prod-main');
  assert.equal(main.querySelector('img.qh-req-prov').getAttribute('alt'), 'AWS');
  assert.equal(main.querySelector('.qh-envtag-sm').textContent, 'PROD');
  assert.equal(rowBtn(ui, 'scratch').querySelector('.qh-envtag-sm').textContent, 'TEST');
  assert.equal(rowBtn(ui, 'scratch').querySelector('img.qh-req-prov'), null, 'no provider tag, no logo');
  assert.ok(rowBtn(ui, 'archive').querySelector('.qh-reqrow-partial'));
});

test('each tier has one short sentence under it, in the portal words', opts, async () => {
  const ui = mount();
  await ui.flush();
  ui.click(rowBtn(ui, 'prod-main'));
  const cols = ui.all('.qh-tierpick-col');
  assert.equal(cols.length, 3);
  assert.deepEqual(cols.map(c => c.querySelector('button').textContent), ['RORead-only', 'RWRead/write', 'DDLSchema change']);
  const { QH_TIER_BLURB } = ui.win;
  assert.deepEqual(cols.map(c => c.querySelector('.qh-tierpick-d').textContent), [QH_TIER_BLURB.RO, QH_TIER_BLURB.RW, QH_TIER_BLURB.DDL]);
  for (const t of Object.values(QH_TIER_BLURB)) assert.ok(t.split(/\s+/).length <= 12, t);
  assert.ok(cols.every(c => !c.querySelector('button').disabled), 'a Postgres target takes all three');
});

test('a read-only engine locks RW and DDL, says why, and falls back to read-only', opts, async () => {
  const ui = mount();
  await ui.flush();
  ui.click(rowBtn(ui, 'prod-main'));
  ui.click(tierBtn(ui, 'Schema change'));
  assert.equal(tierBtn(ui, 'Schema change').getAttribute('aria-pressed'), 'true');
  ui.click(ui.all('.qh-reqalt').find(b => b.textContent === 'Change'));
  ui.click(rowBtn(ui, 'archive'));
  const rw = tierBtn(ui, 'Read/write'), ddl = tierBtn(ui, 'Schema change'), ro = tierBtn(ui, 'Read-only');
  assert.equal(rw.disabled, true);
  assert.equal(ddl.disabled, true);
  assert.equal(ro.disabled, false);
  assert.match(rw.title, /Amazon Athena is read-only in QueryHub\. Only read-only access exists on it\./);
  assert.equal(ro.getAttribute('aria-pressed'), 'true', 'the earlier Schema choice fell back');
  assert.match(ui.doc.querySelector('.qh-tierpick').parentNode.textContent, /read-only in QueryHub/);
});

test('the lock rule: read-only engines only, RO is never locked', () => {
  const { qhTierLockWhy, qhEngineReadOnly } = loadInto(bareWindow(), 'qh-data.jsx');
  for (const engine of ['clickhouse', 'athena', 'ClickHouse 24.8', 'Amazon Athena (engine v3)']) {
    assert.equal(qhEngineReadOnly({ engine }), true, engine);
    assert.equal(qhTierLockWhy({ engine }, 'RO'), '');
    assert.notEqual(qhTierLockWhy({ engine }, 'RW'), '');
    assert.notEqual(qhTierLockWhy({ engine }, 'DDL'), '');
  }
  for (const engine of ['postgres', 'PostgreSQL 15', 'mssql', 'SQL Server 2022']) {
    assert.equal(qhEngineReadOnly({ engine }), false, engine);
    assert.equal(qhTierLockWhy({ engine }, 'DDL'), '');
  }
});

test('the reason hint, and the 5-character minimum the server enforces', opts, async () => {
  const ui = mount();
  await ui.flush();
  ui.click(rowBtn(ui, 'prod-main'));
  const sel = ui.doc.querySelector('select.qh-input');
  ui.React.act(() => { const set = Object.getOwnPropertyDescriptor(ui.win.HTMLSelectElement.prototype, 'value').set; set.call(sel, 'payments'); sel.dispatchEvent(new ui.win.Event('change', { bubbles: true })); });
  assert.match(ui.doc.querySelector('textarea').parentNode.textContent, /Say why you need it\. A DBA reads this\./);
  const send = () => [...ui.doc.querySelectorAll('.qh-modal-foot button')].find(b => /Send request/.test(b.textContent));
  assert.equal(ui.win.QH_REQ_REASON_MIN, 5);
  type(ui, ui.doc.querySelector('textarea'), ' abcd ');
  assert.equal(send().disabled, true, '4 characters after trimming');
  assert.match(send().title, /at least 5 characters/);
  type(ui, ui.doc.querySelector('textarea'), 'abcde');
  assert.equal(send().disabled, false);
  ui.click(send());
  assert.deepEqual(ui.sent.map(r => [r.connectionId, r.database, r.tier, r.reason]), [['prod-main', 'payments', 'RO', 'abcde']]);
});

test('the free-text path for a server QueryHub does not have is still there', opts, async () => {
  const ui = mount();
  await ui.flush();
  ui.click(ui.all('.qh-reqalt').find(b => /Not listed/.test(b.textContent)));
  const inputs = ui.all('.qh-modal-body input.qh-input');
  type(ui, inputs[0], 'new-host-01');
  type(ui, inputs[1], 'ledger');
  assert.equal(ui.all('.qh-tierpick button').every(b => !b.disabled), true, 'no connection, no lock');
  type(ui, ui.doc.querySelector('textarea'), 'Needed for the weekly report');
  const send = [...ui.doc.querySelectorAll('.qh-modal-foot button')].find(b => /Send request/.test(b.textContent));
  assert.equal(send.disabled, false);
  ui.click(send);
  assert.deepEqual(ui.sent.map(r => [r.connectionId, r.server, r.database]), [[null, 'new-host-01', 'ledger']]);
});

test('the mock gives the picker tags, and one read-only target to lock', () => {
  const mock = web('qh-api-mock.jsx');
  assert.match(mock, /partial: !!mine,\s*tags: mockNarrowTags\(c\.tags\) \}\);/);
  assert.match(mock, /id: 'archive-athena'/);
  assert.doesNotMatch(mock.match(/const mockNarrowTags[\s\S]*?\n\};/)[0], /account/, 'never the account');
});
