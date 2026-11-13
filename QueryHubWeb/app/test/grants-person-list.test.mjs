// Grants, layout A (2026-11-13): one row per person or team, with reach,
// source, flags and the nearest end. A click opens the per-server lines.
import test from 'node:test';
import assert from 'node:assert/strict';

import { page, web, tokensCss, jsdomOpts as opts } from './_mount.mjs';

const FILES = ['qh-data.jsx', 'qh-modal.jsx', 'qh-panels.jsx', 'qh-admin-data.jsx', 'qh-admin-access.jsx', 'qh-admin-person.jsx'];

const P1 = 'U0EXAMPLE001', P2 = 'U0EXAMPLE002', P3 = 'U0EXAMPLE003', P4 = 'U0EXAMPLE004';
const DAY = 86400000;
const at = (ms) => new Date(Date.now() + ms).toISOString();
const CONNS = [
  { id: 'prod-main', name: 'prod-main', env: 'production', enabled: true, databases: [{ id: 'orders', name: 'orders' }] },
  { id: 'stage-db', name: 'stage-db', env: 'staging', enabled: true, databases: [{ id: 'orders', name: 'orders' }] },
];
const PEOPLE = [
  { id: P1, handle: P1, name: 'Jordan Ray', initials: 'JR', enabled: true },
  { id: P2, handle: P2, name: 'Sam Lee', initials: 'SL', enabled: true },
  { id: P3, handle: P3, name: 'Alex Kim', initials: 'AK', enabled: true },
  { id: P4, handle: P4, name: 'Riley Stone', initials: 'RS', enabled: false },
];
const TEAMS = [{ id: '7', name: 'Team Alpha', members: [P2], leads: [] }];
const grant = (id, subjectType, subject, connectionId, tier, expiresAt, databases) => ({
  id, subjectType, subject, subjectName: null, connectionId, databases: databases || ['*'], tier, expiresAt: expiresAt || null,
  grantedBy: null, grantedByName: null, grantedAt: at(-30 * DAY),
});
const GRANTS = [
  grant('g1', 'user', P1, 'prod-main', 'RW', null),
  grant('g2', 'team', 'Team Alpha', 'stage-db', 'RO', null),
  grant('g3', 'user', P3, 'stage-db', 'RO', at(-2 * DAY)),
  grant('g4', 'user', P4, 'stage-db', 'RO', at(3 * DAY), ['orders']),
];

function mount({ fleet = [], grants = GRANTS, stExtra = {} } = {}) {
  const ui = page(FILES, { prep: (win) => { win.qhApi = { adminFleetGrants: () => Promise.resolve({ grants: fleet }) }; } });
  const revoked = [];
  const st = {
    connections: CONNS, people: PEOPLE, teams: TEAMS, grants, grantsState: 'ok', autoGrants: [], autoRequests: [],
    resolvePerson: () => Promise.resolve(null), setSubjectGrants() {}, addFleetGrant() {}, revokeFleetGrant() {},
    revokeGrant: (...a) => { revoked.push(a); },
    ...stExtra,
  };
  ui.render(ui.React.createElement(ui.win.GrantsView, { st, user: { name: 'Example Admin', role: 'super' } }));
  const names = () => ui.all('.qh-gra-row .qh-gra-name').map(n => n.textContent);
  const row = (name) => ui.all('.qh-gra-row').find(r => r.querySelector('.qh-gra-name').textContent === name);
  const button = (text, from) => {
    const b = [...(from || ui.doc).querySelectorAll('button')].find(x => x.textContent.trim() === text);
    assert.ok(b, 'no button reads "' + text + '"');
    return b;
  };
  const chip = (label) => ui.all('.qh-gra-chip').find(b => b.textContent.startsWith(label));
  return { ...ui, revoked, names, row, button, chip };
}

test('the list is the default view and hides expired grants behind a footnote', opts, async () => {
  const ui = mount();
  await ui.flush();
  assert.equal(ui.button('Overview').classList.contains('is-active'), true);
  assert.deepEqual(ui.names(), ['Jordan Ray', 'Riley Stone', 'Sam Lee'], 'flagged rows first, then by name');
  assert.equal(ui.row('Alex Kim'), undefined, 'only an ended grant: not active');
  const foot = ui.doc.querySelector('.qh-gra-foot');
  assert.match(foot.textContent, /^1 expired grant hidden/);
  ui.click(ui.button('Show', foot));
  assert.match(ui.doc.querySelector('.qh-gra-foot').textContent, /^Expired grants are shown/);
  // Show lists the person whose only grant ended, and the line says Ended.
  assert.equal(ui.row('Alex Kim').querySelector('.qh-gra-tiers').textContent, 'No live access');
  ui.click(ui.row('Alex Kim').querySelector('.qh-gra-rowbtn'));
  assert.match(ui.row('Alex Kim').querySelector('.qh-gra-line.is-ended .qh-endchip').textContent, /^Ended/);
  ui.click(ui.button('Hide', ui.doc.querySelector('.qh-gra-foot')));
  assert.equal(ui.row('Alex Kim'), undefined);
  // Active off lists them too.
  ui.click(ui.chip('Active'));
  assert.ok(ui.row('Alex Kim'));
});

test('the chips carry counts from the data', opts, async () => {
  const ui = mount();
  await ui.flush();
  assert.equal(ui.chip('Active').textContent, 'Active3');
  assert.equal(ui.chip('Active').getAttribute('aria-pressed'), 'true');
  assert.equal(ui.chip('Ends in 7 days').textContent, 'Ends in 7 days1');
  assert.equal(ui.chip('Auto-approve').textContent, 'Auto-approve0');
  assert.equal(ui.chip('Only write access').querySelector('.qh-gra-chip-n'), null);
});

test('a team grant says where it comes from', opts, async () => {
  const ui = mount();
  await ui.flush();
  const r = ui.row('Sam Lee');
  assert.equal(r.querySelector('.qh-gra-from').textContent, 'via team Team Alpha');
  assert.equal(r.querySelector('.qh-gra-n').textContent, '1 server');
  assert.equal(r.querySelector('.qh-gra-tiers').textContent, 'RO 1');
  assert.equal(ui.row('Jordan Ray').querySelector('.qh-gra-from').textContent, 'direct only');
});

test('RW on a production server and a direct RW grant with no end are flagged in words', opts, async () => {
  const ui = mount();
  await ui.flush();
  const flags = ui.row('Jordan Ray').querySelectorAll('.qh-gra-flag');
  assert.deepEqual([...flags].map(f => f.textContent), ['RW on PROD', 'No end date']);
  assert.ok(flags[0].classList.contains('is-danger'));
  assert.deepEqual([...ui.row('Riley Stone').querySelectorAll('.qh-gra-flag')].map(f => f.textContent), ['Account off']);
  assert.equal(ui.row('Sam Lee').querySelectorAll('.qh-gra-flag').length, 0, 'a standing team grant is not an exception');
});

test('the Ends column shows the nearest end', opts, async () => {
  const ui = mount();
  await ui.flush();
  assert.equal(ui.row('Riley Stone').querySelector('.qh-gra-ends').textContent, 'in 3 days');
  assert.equal(ui.row('Jordan Ray').querySelector('.qh-gra-ends').textContent, 'No end');
});

test('"Only write access" and the search narrow the list', opts, async () => {
  const ui = mount();
  await ui.flush();
  ui.click(ui.chip('Only write access'));
  assert.deepEqual(ui.names(), ['Jordan Ray']);
  ui.click(ui.chip('Only write access'));
  ui.click(ui.chip('Only production'));
  assert.deepEqual(ui.names(), ['Jordan Ray']);
  ui.click(ui.chip('Only production'));
  const input = ui.doc.querySelector('.qh-gra-filters input');
  ui.React.act(() => {
    Object.getOwnPropertyDescriptor(ui.win.HTMLInputElement.prototype, 'value').set.call(input, 'stage-db');
    input.dispatchEvent(new ui.win.Event('input', { bubbles: true }));
  });
  assert.deepEqual(ui.names(), ['Riley Stone', 'Sam Lee']);
});

test('no match shows the empty state, and Clear filters brings everyone back', opts, async () => {
  const ui = mount();
  await ui.flush();
  const input = ui.doc.querySelector('.qh-gra-filters input');
  ui.React.act(() => {
    Object.getOwnPropertyDescriptor(ui.win.HTMLInputElement.prototype, 'value').set.call(input, 'no such name');
    input.dispatchEvent(new ui.win.Event('input', { bubbles: true }));
  });
  const empty = ui.doc.querySelector('.qh-gra-empty');
  assert.match(empty.textContent, /^No one matches these filters\./);
  ui.click(ui.button('Clear filters', empty));
  assert.deepEqual(ui.names(), ['Jordan Ray', 'Riley Stone', 'Alex Kim', 'Sam Lee']);
});

test('a row click opens one line per server, and Edit access opens the editor', opts, async () => {
  const ui = mount();
  await ui.flush();
  const btn = ui.row('Jordan Ray').querySelector('.qh-gra-rowbtn');
  ui.click(btn);
  assert.equal(btn.getAttribute('aria-expanded'), 'true');
  const lines = ui.row('Jordan Ray').querySelectorAll('.qh-gra-line');
  assert.equal(lines.length, 1);
  assert.equal(lines[0].querySelector('.qh-gra-srv').textContent, 'prod-main');
  assert.equal(lines[0].querySelector('.qh-envtag-sm').textContent, 'PROD');
  assert.equal(lines[0].querySelector('.qh-gra-dbs').textContent, 'all');
  assert.equal(lines[0].querySelector('.qh-gra-src').textContent, 'direct');
  ui.click(ui.button('Edit access', ui.row('Jordan Ray')));
  assert.ok(ui.row('Jordan Ray').querySelector('.qh-acced'), 'the access editor opens in the row');
});

test('Remove on a direct line asks first, then revokes that grant', opts, async () => {
  const ui = mount();
  await ui.flush();
  ui.click(ui.row('Jordan Ray').querySelector('.qh-gra-rowbtn'));
  ui.click(ui.button('Remove', ui.row('Jordan Ray')));
  const ask = ui.doc.querySelector('.qh-revokeconfirm');
  assert.match(ask.textContent, /Jordan Ray loses RW on prod-main \/ all databases\./);
  ui.click(ui.button('Revoke access', ask));
  assert.equal(ui.revoked.length, 1);
  assert.equal(ui.revoked[0][0], 'g1');
  // A team line has no Remove: the team owns it.
  ui.click(ui.row('Sam Lee').querySelector('.qh-gra-rowbtn'));
  assert.equal([...ui.row('Sam Lee').querySelectorAll('button')].some(b => b.textContent === 'Remove'), false);
});

test('an own grant on the server wins over the team grant there', opts, async () => {
  const ui = mount({ grants: GRANTS.concat([grant('g5', 'user', P2, 'stage-db', 'RW', at(20 * DAY))]) });
  await ui.flush();
  const r = ui.row('Sam Lee');
  assert.equal(r.querySelector('.qh-gra-tiers').textContent, 'RW 1');
  assert.equal(r.querySelector('.qh-gra-from').textContent, 'direct only');
  ui.click(r.querySelector('.qh-gra-rowbtn'));
  const team = [...ui.row('Sam Lee').querySelectorAll('.qh-gra-line')].find(l => l.querySelector('.qh-gra-src').textContent === 'team Team Alpha');
  assert.equal(team.querySelector('.qh-gra-note').textContent, 'not used');
});

test('a fleet-wide grant shows as "every server" and opens in the Manage block', opts, async () => {
  const fleet = [{ id: 'f1', subjectType: 'team', subject: 'team-alpha', subjectName: 'Team Alpha', tier: 'RO', autoApprove: true, enabled: true, expiresAt: null, syncedFrom: null, reason: 'x' }];
  const ui = mount({ fleet });
  await ui.flush();
  const r = ui.row('Sam Lee');
  assert.equal(r.querySelector('.qh-gra-from').textContent, 'every server, via team Team Alpha');
  assert.equal(r.querySelector('.qh-gra-n').textContent, '2 servers');
  assert.deepEqual([...r.querySelectorAll('.qh-gra-flag')].map(f => f.textContent), ['auto-approve']);
  const foot = ui.doc.querySelector('.qh-gra-fleetfoot');
  assert.match(foot.textContent, /^1 fleet-wide grant/);
  assert.equal(ui.doc.querySelector('.qh-fleet'), null, 'no block on top');
  ui.click(ui.button('Manage', foot));
  await ui.flush();
  assert.ok(ui.doc.querySelector('.qh-fleet'));
});

test('the Teams view lists teams with their member count', opts, async () => {
  const ui = mount();
  await ui.flush();
  ui.click(ui.button('Teams'));
  assert.deepEqual(ui.names(), ['Team Alpha']);
  const r = ui.row('Team Alpha');
  assert.equal(r.querySelector('.qh-gra-sub').textContent, '1 member');
  ui.click(r.querySelector('.qh-gra-rowbtn'));
  assert.equal(ui.row('Team Alpha').querySelector('.qh-gra-src').textContent, 'team grant');
});

test('while grants load, the list shows skeleton rows', opts, async () => {
  const ui = mount({ stExtra: { grantsState: 'loading' } });
  await ui.flush();
  assert.ok(ui.doc.querySelector('.qh-gra-skel'));
  assert.equal(ui.all('.qh-gra-row').length, 0);
});

test('By subject and By grant stay reachable', opts, async () => {
  const ui = mount();
  await ui.flush();
  ui.click(ui.button('By grant'));
  assert.ok(ui.doc.querySelector('.qh-acttable'));
  ui.click(ui.button('By subject'));
  assert.ok(ui.doc.querySelector('.qh-subjlist'));
});

test('the qh-gra- styles use tokens that exist and no raw colour', opts, () => {
  const html = web('QueryHub.html');
  const rules = html.split('\n').filter(l => /\.qh-gra[-\s{,:]/.test(l));
  assert.ok(rules.length > 20);
  const tokens = tokensCss();
  for (const l of rules) {
    assert.equal(/#[0-9a-fA-F]{3,8}\b/.test(l), false, 'raw colour: ' + l);
    for (const m of l.matchAll(/var\(--([a-z0-9-]+)/g)) {
      if (m[1] === 'qh-mono') continue;
      assert.ok(tokens.includes('--' + m[1] + ':'), 'unknown token --' + m[1]);
    }
  }
});
