// Teams can follow company-SSO groups (2026-11-20):
//
//   * two API methods for GET and PUT /admin/teams/{id}/idp-groups;
//   * an "IdP groups" block in the team edit card: chips, add, remove, Save;
//   * a quiet note while idp_group_sync_enabled is off, and no switch;
//   * an "IdP" chip on a member that the sync added, and on no other member.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';
import { page, web, jsdomOpts as opts } from './_mount.mjs';

const FILES = ['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx',
  'qh-admin-data.jsx', 'qh-admin-effective.jsx', 'qh-admin-access.jsx', 'qh-admin-roles.jsx'];
const H = loadInto(bareWindow(), 'qh-data.jsx', 'qh-admin-data.jsx', 'qh-admin-access.jsx', 'qh-admin-roles.jsx');

const OFF_NOTE = 'Sync is off (idp_group_sync_enabled). Mappings are kept and used when it is turned on.';

// ---------- the API methods ----------

test('qhApi reads and replaces /admin/teams/{id}/idp-groups', () => {
  const api = web('qh-api.jsx');
  assert.match(api, /adminTeamIdpGroups:\(teamId\) => qhFetch\('\/admin\/teams\/' \+ encodeURIComponent\(teamId\) \+ '\/idp-groups'\)/);
  assert.match(api, /adminSetTeamIdpGroups:\(teamId, groups\) => qhFetch\('\/admin\/teams\/' \+ encodeURIComponent\(teamId\) \+ '\/idp-groups', \{ method: 'PUT'/);
  const mock = web('qh-api-mock.jsx');
  assert.match(mock, /adminTeamIdpGroups: \(teamId\) => mockDelay\(\(\) => \(\{ enabled: false,/, 'the mock keeps the sync off');
  assert.match(mock, /adminSetTeamIdpGroups: \(teamId, groups\) => mockDelay\(/);
});

test('the PUT body carries only provider and group', async () => {
  const win = bareWindow();
  const calls = [];
  win.qhFetch = (path, init) => { calls.push([path, init]); return Promise.resolve({}); };
  const src = web('qh-api.jsx');
  const line = src.split('\n').find(l => l.includes('adminSetTeamIdpGroups:'));
  // Evaluate the one method against a stub fetch.
  const fn = new Function('qhFetch', 'return {' + line + '}')(win.qhFetch).adminSetTeamIdpGroups;
  await fn('7', [{ provider: 'corp', group: 'dba', createdAt: 'x', createdBy: 'y' }]);
  assert.equal(calls[0][0], '/admin/teams/7/idp-groups');
  assert.equal(calls[0][1].method, 'PUT');
  assert.deepEqual(JSON.parse(calls[0][1].body), { groups: [{ provider: 'corp', group: 'dba' }] });
});

// ---------- pure helpers ----------

test('member detail: only source idp is an SSO member, absent source is manual', () => {
  const team = { members: ['U0EXAMPLE001', 'U0EXAMPLE002', 'U0EXAMPLE003'], memberRows: [
    { handle: 'U0EXAMPLE001', source: 'idp', idpGroup: 'dba-team' },
    { handle: 'U0EXAMPLE002', source: 'manual' },
    { handle: 'U0EXAMPLE003' },
  ] };
  assert.deepEqual(H.qhTeamMemberIdp(team, 'U0EXAMPLE001'), { group: 'dba-team' });
  assert.equal(H.qhTeamMemberIdp(team, 'U0EXAMPLE002'), null);
  assert.equal(H.qhTeamMemberIdp(team, 'U0EXAMPLE003'), null, 'no source reads as manual');
  assert.equal(H.qhTeamMemberIdp({ members: ['U0EXAMPLE001'] }, 'U0EXAMPLE001'), null, 'an older server');
  assert.deepEqual(H.qhTeamMemberIdp({ members: [{ handle: 'U0EXAMPLE004', source: 'idp', idpGroup: 'g' }] }, 'U0EXAMPLE004'), { group: 'g' });
  assert.equal(H.qhTeamMemberIdp(null, 'U0EXAMPLE001'), null);
});

test('provider ids skip Slack and local accounts', () => {
  const body = { providers: [{ id: 'slack', kind: 'oauth' }, { id: 'corp', kind: 'oauth' }, { id: 'local', kind: 'password' }] };
  assert.deepEqual(H.qhIdpProviderIds(body), ['corp']);
  assert.deepEqual(H.qhIdpProviderIds(null), []);
});

test('add refuses an empty pair, a duplicate and a full list', () => {
  assert.deepEqual(H.qhIdpGroupAdd([], ' corp ', ' dba '), [{ provider: 'corp', group: 'dba' }]);
  assert.equal(H.qhIdpGroupAdd([], 'corp', '  '), null);
  assert.equal(H.qhIdpGroupAdd([], '', 'dba'), null);
  assert.equal(H.qhIdpGroupAdd([{ provider: 'corp', group: 'dba' }], 'corp', 'dba'), null);
  const full = Array.from({ length: 50 }, (_, i) => ({ provider: 'corp', group: 'g' + i }));
  assert.equal(H.qhIdpGroupAdd(full, 'corp', 'one-more'), null);
  assert.equal(H.qhIdpGroupAdd([], 'corp', 'x'.repeat(201)), null);
});

// ---------- the block ----------

function mountBlock({ enabled = false, groups = [], providers = [{ id: 'slack', kind: 'oauth' }, { id: 'corp', kind: 'oauth' }], put } = {}) {
  const ui = page(FILES, { prep: (win) => { win.qhApi = {}; } });
  const puts = [];
  ui.win.qhApi = {
    adminTeamIdpGroups: () => Promise.resolve({ enabled, groups }),
    adminSetTeamIdpGroups: (id, list) => { puts.push([id, list]); return put ? put(id, list) : Promise.resolve({ enabled, groups: list }); },
    providers: () => Promise.resolve({ providers }),
  };
  ui.render(ui.React.createElement(ui.win.TeamIdpGroups, { teamId: 't1' }));
  return { ui, puts };
}

const setVal = (ui, el, v) => ui.React.act(() => {
  const proto = el.tagName === 'SELECT' ? ui.win.HTMLSelectElement.prototype : ui.win.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);
  el.dispatchEvent(new ui.win.Event(el.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
});
const button = (ui, text) => ui.all('.qh-idpg button').find(b => b.textContent.trim() === text);

test('the block lists the groups as chips', opts, async () => {
  const { ui } = mountBlock({ groups: [{ provider: 'corp', group: 'dba-team', createdAt: null, createdBy: null }] });
  await ui.flush();
  const chips = ui.all('.qh-idpg-chip').map(c => c.textContent.trim());
  assert.deepEqual(chips, ['corp / dba-team']);
  const sel = ui.doc.querySelector('.qh-idpg select');
  assert.ok(sel, 'a known provider gives a select');
  assert.deepEqual([...sel.options].map(o => o.value), ['corp'], 'Slack is not an SSO group source');
});

test('add, remove and Save send the whole list with PUT', opts, async () => {
  const { ui, puts } = mountBlock({ groups: [{ provider: 'corp', group: 'old' }] });
  await ui.flush();
  assert.equal(button(ui, 'Save groups').disabled, true, 'nothing to save yet');
  await setVal(ui, ui.doc.querySelector('.qh-idpg input[aria-label="Group name"]'), 'dba-team');
  await ui.click(button(ui, 'Add'));
  await ui.click(ui.doc.querySelector('.qh-idpg-chip button[aria-label="Remove corp / old"]'));
  assert.deepEqual(ui.all('.qh-idpg-chip').map(c => c.textContent.trim()), ['corp / dba-team']);
  await ui.click(button(ui, 'Save groups'));
  await ui.flush();
  assert.equal(puts.length, 1);
  assert.equal(puts[0][0], 't1');
  assert.deepEqual(puts[0][1], [{ provider: 'corp', group: 'dba-team' }]);
  assert.match(ui.doc.querySelector('.qh-idpg').textContent, /Saved\./);
});

test('a refused Save shows the server message and keeps the list', opts, async () => {
  const fail = () => Promise.reject(Object.assign(new Error('Enter a group name of 1 to 200 characters.'), { status: 400 }));
  const { ui } = mountBlock({ put: fail });
  await ui.flush();
  await setVal(ui, ui.doc.querySelector('.qh-idpg input[aria-label="Group name"]'), 'g');
  await ui.click(button(ui, 'Add'));
  await ui.click(button(ui, 'Save groups'));
  await ui.flush();
  assert.match(ui.doc.querySelector('.qh-idpg-err').textContent, /1 to 200 characters/);
  assert.deepEqual(ui.all('.qh-idpg-chip').map(c => c.textContent.trim()), ['corp / g']);
});

test('no known provider gives a text input', opts, async () => {
  const { ui } = mountBlock({ providers: [{ id: 'slack', kind: 'oauth' }] });
  await ui.flush();
  assert.equal(ui.doc.querySelector('.qh-idpg select'), null);
  assert.ok(ui.doc.querySelector('.qh-idpg input[aria-label="SSO provider"]'));
});

test('the off note shows only when the sync is off, and there is no switch', opts, async () => {
  const off = mountBlock({ enabled: false });
  await off.ui.flush();
  assert.equal(off.ui.doc.querySelector('.qh-idpg-off').textContent, OFF_NOTE);
  assert.equal(off.ui.doc.querySelector('.qh-idpg input[type="checkbox"], .qh-idpg [role="switch"]'), null);
  const on = mountBlock({ enabled: true });
  await on.ui.flush();
  assert.equal(on.ui.doc.querySelector('.qh-idpg-off'), null);
});

// ---------- the member chip ----------

test('the IdP chip shows only on a member with source idp', opts, async () => {
  const ui = page(FILES, { prep: (win) => { win.qhApi = {}; } });
  const st = {
    teams: [{ id: 't1', name: 'team-alpha', desc: '', subteams: [], members: ['U0EXAMPLE001', 'U0EXAMPLE002'],
      memberRows: [{ handle: 'U0EXAMPLE001', source: 'idp', idpGroup: 'dba-team' }, { handle: 'U0EXAMPLE002', source: 'manual' }] }],
    people: [{ handle: 'U0EXAMPLE001', name: 'Ada Example', initials: 'AE' }, { handle: 'U0EXAMPLE002', name: 'Ben Example', initials: 'BE' }],
    grants: [], grantsState: 'ok', roles: [], rolesState: 'ok',
  };
  ui.render(ui.React.createElement(ui.win.TeamsView, { st, user: { name: 'Ada Example' } }));
  await ui.flush();
  const pills = ui.all('.qh-memberpill');
  assert.equal(pills.length, 2);
  const chip = pills[0].querySelector('.qh-memberpill-idp');
  assert.ok(chip, 'the SSO member has the chip');
  assert.equal(chip.textContent, 'IdP');
  assert.equal(chip.getAttribute('title'), 'Added by the company SSO group dba-team. The next sign-in removes it if the person leaves the group.');
  assert.equal(pills[1].querySelector('.qh-memberpill-idp'), null, 'a manual member does not change');
});
