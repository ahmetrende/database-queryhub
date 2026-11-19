// The People tab of the Teams screen, ported from the IdP app (row 19 of the
// IdP-better list):
//
//   * a Role column, derived from the admin roles list;
//   * a Status column from `enabled`, empty when the field is absent;
//   * a "without a team" toggle chip that limits the table;
//   * a warning panel for sign-in addresses that two people share.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';
import { page, web, jsdomOpts as opts } from './_mount.mjs';

const FILES = ['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx',
  'qh-admin-data.jsx', 'qh-admin-effective.jsx', 'qh-admin-access.jsx', 'qh-admin-roles.jsx'];
// The pure helpers load without a DOM, as in k7-approver-tier.test.mjs.
const H = loadInto(bareWindow(), 'qh-data.jsx', 'qh-admin-data.jsx', 'qh-admin-access.jsx', 'qh-admin-roles.jsx');

const admin = (extra) => ({ role: 'admin', enabled: true, allTeams: true, allTargets: true, anyTier: true, validUntil: null, ...extra });

// ---------- role derivation ----------

test('role of a row: super, dba, limited, scoped, granter, developer', () => {
  assert.equal(H.qhRoleOfRow(admin()), 'super');
  assert.equal(H.qhRoleOfRow(admin({ anyTier: false, maxTier: 'RW' })), 'dba', 'a tier ceiling still reads as DBA');
  assert.equal(H.qhRoleOfRow(admin({ allTeams: false, scopeTeamId: 't1' })), 'limited', 'one team');
  assert.equal(H.qhRoleOfRow(admin({ allTargets: false, scopeTargetId: 'prod-main' })), 'limited', 'one connection');
  assert.equal(H.qhRoleOfRow(admin({ validUntil: '2026-12-31T00:00:00Z' })), 'limited', 'an end date');
  assert.equal(H.qhRoleOfRow(admin({ enabled: false })), 'limited', 'a disabled admin row');
  assert.equal(H.qhRoleOfRow({ role: 'approver', enabled: true }), 'scoped');
  assert.equal(H.qhRoleOfRow({ role: 'granter', enabled: true }), 'granter');
  assert.equal(H.qhRoleOfRow({ role: 'importer', enabled: true }), 'developer', 'a removed role is no authority');
  assert.equal(H.qhRoleOfRow(null), 'developer');
});

test('role of a row: missing scope booleans fall back to the scope ids', () => {
  assert.equal(H.qhRoleOfRow({ role: 'admin', enabled: true }), 'super');
  assert.equal(H.qhRoleOfRow({ role: 'admin', enabled: true, maxTier: 'RO' }), 'dba');
  assert.equal(H.qhRoleOfRow({ role: 'admin', enabled: true, scopeTeamId: 't1' }), 'limited');
  assert.equal(H.qhRoleOfRow({ role: 'admin', enabled: true, scopeTargetId: 'prod-main' }), 'limited');
});

test('role of a person: the highest enabled row, matched on handle or id', () => {
  const roles = [
    { subject: 'U0EXAMPLE001', role: 'granter', enabled: true },
    { subject: 'U0EXAMPLE001', role: 'approver', enabled: true },
    { subject: 'U0EXAMPLE002', ...admin(), enabled: false },
    { subject: 'U0EXAMPLE002', role: 'granter', enabled: true },
    { subject: 'U0EXAMPLE003', ...admin({ anyTier: false, maxTier: 'RW' }) },
  ];
  assert.equal(H.qhRoleOf({ handle: 'U0EXAMPLE001' }, roles), 'scoped', 'approver ranks over granter');
  assert.equal(H.qhRoleOf({ handle: 'U0EXAMPLE002' }, roles), 'granter', 'a disabled row is skipped');
  assert.equal(H.qhRoleOf({ id: 'U0EXAMPLE003', name: 'Ada Example' }, roles), 'dba', 'a DBA reader gets only id');
  assert.equal(H.qhRoleOf({ handle: 'U0EXAMPLE009' }, roles), 'developer', 'nobody');
  assert.equal(H.qhRoleOf({ handle: 'U0EXAMPLE001' }, null), 'developer');
});

test('role labels and the Role cell', () => {
  assert.equal(H.qhRoleLabel('super'), 'Super-admin');
  assert.equal(H.qhRoleLabel('dba'), 'DBA');
  assert.equal(H.qhRoleLabel('limited'), 'Limited admin');
  assert.equal(H.qhRoleLabel('scoped'), H.QH_ROLE_DEFS.approver.label);
  assert.equal(H.qhRoleLabel('granter'), H.QH_ROLE_DEFS.granter.label);
  assert.equal(H.qhRoleLabel('developer'), 'Developer');
  const p = { handle: 'U0EXAMPLE001' };
  const roles = [{ subject: 'U0EXAMPLE001', role: 'granter', enabled: true }];
  assert.equal(H.qhPersonRoleCell(p, roles, 'ok'), 'Granter');
  assert.equal(H.qhPersonRoleCell(p, [], 'ok'), 'Developer');
  assert.equal(H.qhPersonRoleCell(p, roles, 'loading'), '—', 'roles not loaded yet');
  assert.equal(H.qhPersonRoleCell(p, roles, 'error'), '—', 'a failed read is not "nobody"');
  assert.equal(H.qhPersonRoleCell(p, roles, undefined), '—');
});

// ---------- status ----------

test('status cell: Active, Disabled, or nothing when enabled is absent', () => {
  assert.equal(H.qhPersonStatus({ enabled: true }), 'Active');
  assert.equal(H.qhPersonStatus({ enabled: false }), 'Disabled');
  assert.equal(H.qhPersonStatus({ id: 'U0EXAMPLE001', name: 'Ada Example' }), null);
  assert.equal(H.qhPersonStatus(null), null);
});

// ---------- the no-team filter ----------

const PEOPLE = [
  { handle: 'U0EXAMPLE001', name: 'Ada Example' },
  { handle: 'U0EXAMPLE002', name: 'Ben Example' },
  { handle: 'U0EXAMPLE003', name: 'Cem Example' },
];
const TEAMS = [{ id: 't1', name: 'team-alpha', members: ['U0EXAMPLE001'] }, { id: 't2', name: 'team-beta', members: ['U0EXAMPLE002'] }];

test('no-team predicate and the People rows', () => {
  assert.equal(H.qhHasNoTeam('U0EXAMPLE001', TEAMS), false);
  assert.equal(H.qhHasNoTeam('U0EXAMPLE003', TEAMS), true);
  assert.equal(H.qhHasNoTeam('U0EXAMPLE003', null), true);
  const names = (rows) => rows.map(p => p.handle);
  assert.deepEqual(names(H.qhPeopleRows(PEOPLE, TEAMS, '', false)), ['U0EXAMPLE001', 'U0EXAMPLE002', 'U0EXAMPLE003']);
  assert.deepEqual(names(H.qhPeopleRows(PEOPLE, TEAMS, '', true)), ['U0EXAMPLE003']);
  assert.deepEqual(names(H.qhPeopleRows(PEOPLE, TEAMS, 'beta', false)), ['U0EXAMPLE002'], 'a team name matches');
  assert.deepEqual(names(H.qhPeopleRows(PEOPLE, TEAMS, '  ADA ', false)), ['U0EXAMPLE001'], 'trimmed, any case');
  assert.deepEqual(names(H.qhPeopleRows(PEOPLE, TEAMS, 'ada', true)), [], 'both filters apply');
});

// ---------- shared addresses ----------

const DUP = { total: 1, duplicates: [{ email: 'shared@example.com', accounts: [
  { table: 'requesters', principal: 'U0EXAMPLE001', name: 'Ada Example', enabled: true },
  { table: 'admins', principal: 'U0EXAMPLE002', name: 'Ben Example', enabled: false },
] }] };

test('the shared-address list: only a body with total > 0 shows', () => {
  assert.deepEqual(H.qhDupAddrList(DUP), DUP.duplicates);
  assert.deepEqual(H.qhDupAddrList({ total: 0, duplicates: [] }), []);
  assert.deepEqual(H.qhDupAddrList({ total: 2 }), [], 'no list, nothing to show');
  assert.deepEqual(H.qhDupAddrList(null), []);
});

function mountPanel(call) {
  const ui = page(FILES, { prep: (win) => { win.qhApi = {}; } });
  ui.win.qhApi = { adminDuplicateAddresses: call };
  ui.render(ui.React.createElement(ui.win.DupAddressPanel));
  return ui;
}

test('the panel shows the address and both accounts', opts, async () => {
  let calls = 0;
  const ui = mountPanel(() => { calls++; return Promise.resolve(DUP); });
  await ui.flush();
  assert.equal(calls, 1);
  const panel = ui.doc.querySelector('.qh-dupaddr');
  assert.ok(panel, 'the panel shows');
  assert.match(panel.textContent, /Shared sign-in addresses/);
  assert.match(panel.textContent, /QueryHub refuses a sign-in with that address\./);
  assert.match(panel.textContent, /shared@example\.com/);
  const rows = [...panel.querySelectorAll('ul ul li')].map(li => li.textContent);
  assert.equal(rows.length, 2);
  assert.match(rows[0], /requesters · U0EXAMPLE001 · Ada Example · Enabled/);
  assert.match(rows[1], /admins · U0EXAMPLE002 · Ben Example · Disabled/);
});

test('the panel renders nothing on a 403 or an empty list', opts, async () => {
  const denied = mountPanel(() => Promise.reject(Object.assign(new Error('Super-admin only.'), { status: 403 })));
  await denied.flush();
  assert.equal(denied.doc.querySelector('.qh-dupaddr'), null);
  assert.equal(denied.doc.getElementById('root').textContent, '');
  const empty = mountPanel(() => Promise.resolve({ duplicates: [], total: 0 }));
  await empty.flush();
  assert.equal(empty.doc.querySelector('.qh-dupaddr'), null);
});

// ---------- the API method ----------

test('qhApi.adminDuplicateAddresses reads /admin/people/duplicate-addresses', () => {
  assert.match(web('qh-api.jsx'), /adminDuplicateAddresses: \(\) => qhFetch\('\/admin\/people\/duplicate-addresses'\)/);
  assert.match(web('qh-api-mock.jsx'), /adminDuplicateAddresses: \(\) => mockDelay\(/);
});
