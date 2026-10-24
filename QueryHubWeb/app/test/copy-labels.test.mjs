// The words (2026-10-24, the portal's): "Approve and run", "Deny", "Withdraw",
// "Submit for DBA approval", "Read/write", "Schema change". Web labels and web
// statuses only. Server enums, API fields and Slack text keep their old values:
// the decision a button sends is still 'reject', the status is still 'rejected'.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';
import { page, web, jsdomOpts as opts } from './_mount.mjs';

const app = web('qh-app.jsx');

test('the tier words in prose', () => {
  const { qhTierProse } = loadInto(bareWindow(), 'qh-data.jsx');
  assert.equal(qhTierProse('RO'), 'Read-only');
  assert.equal(qhTierProse('RW'), 'Read/write');
  assert.equal(qhTierProse('DDL'), 'Schema change');
  assert.equal(qhTierProse('ddl'), 'Schema change');
});

test('the action bar says Submit for DBA approval, Withdraw, and the new tier words', () => {
  assert.match(app, /'Run' : 'Submit for DBA approval'/);
  assert.match(app, /status === 'running' \? 'Stop' : 'Withdraw'/);
  assert.match(app, /const tierLabel = qhTierProse\(classify\.tier\);/);
  assert.doesNotMatch(app, /'Submit for approval'|'Cancel request'|Schema \(DDL\)|Read\/Write/);
});

test('the batch dialog says deny', () => {
  assert.match(app, /A DBA approves \(or denies\) the whole bundle/);
});

test('the approval queue says Approve and run and Deny', () => {
  const admin = web('qh-admin.jsx');
  assert.match(admin, /'Approve and run'/);
  assert.match(admin, /noteMode === 'reject' \? 'Confirm deny' : 'Deny'/);
  assert.match(admin, /Why do you deny it\? The submitter reads this note\./);
  assert.match(admin, /Approved and denied queries move to the/);
  assert.doesNotMatch(admin, /Approve & run|Confirm reject|Why do you reject it/);
  // The decision the button sends is the server's word, unchanged.
  assert.match(admin, /act\('reject'\)/);
});

test('the toasts say denied', () => {
  const data = web('qh-admin-data.jsx');
  assert.match(data, /decision === 'reject' \? 'Denied\. Submitter notified in Slack\.'/);
  assert.match(data, /'Request denied\. QueryHub told the requester in Slack\.'/);
  assert.doesNotMatch(data, /'Rejected\. Submitter/);
});

test('the status chip says Denied for the server status `rejected`', opts, () => {
  const ui = page(['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx']);
  ui.render(ui.React.createElement(ui.win.StatusPill, { status: 'rejected' }));
  const pill = ui.doc.querySelector('.qh-status');
  assert.equal(pill.textContent, 'Denied');
  assert.ok(pill.classList.contains('st-rejected'), 'the class is the server status, unchanged');
  ui.render(ui.React.createElement(ui.win.StatusPill, { status: 'done' }));
  assert.equal(ui.doc.querySelector('.qh-status').textContent, 'Done');
});

test('the access screens use the same tier words in prose', () => {
  const acc = web('qh-admin-access.jsx');
  assert.match(acc, /\['rw', 'Read\/write', 'Optional\./);
  assert.match(acc, /\['ddl', 'Schema change', 'Optional\./);
  assert.match(acc, />Deny<\/button>/);
  assert.doesNotMatch(acc, /'Read\/Write'|Schema \(DDL\)/);
});
