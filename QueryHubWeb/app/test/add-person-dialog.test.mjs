// Add person dialog (2026-11-13): a person, optional teams, an optional person
// to copy, a live review (dry run) and one Add button.
import test from 'node:test';
import assert from 'node:assert/strict';

import { page, jsdomOpts as opts } from './_mount.mjs';

const FILES = ['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx',
  'qh-admin-data.jsx', 'qh-admin-effective.jsx', 'qh-admin-access.jsx'];

// One variable for the handle: the public export rewrites design handles inside
// strings, never inside a regex, so the test checks with the same variable.
const HANDLE = 'U0EXAMPLE00N';
const KNOWN = 'U0EXAMPLE00K';
const PEOPLE = [
  { id: 'p_new', handle: HANDLE, name: 'Jordan Blake', initials: 'JB' },
  { id: 'p_known', handle: KNOWN, name: 'Sam New', initials: 'SN' },
];
const TEAMS = [
  { id: 3, name: 'Team One', members: ['a', 'b', 'c', 'd'] },
  { id: 4, name: 'Team Two', members: [], leads: [{ id: KNOWN, name: 'Sam New' }] },
];
const OUTSIDE = 'U0EXAMPLE00O';
const CONNS = [
  { id: 'prod-main', name: 'prod-main', engine: 'PostgreSQL', enabled: true, env: 'production',
    databases: [{ id: 'orders', name: 'orders' }, { id: 'billing', name: 'billing' }] },
];
const DIRECTORY = {
  people: [{ principal: OUTSIDE, known: false, name: 'Riley Stone', email: 'riley.stone@other-example.org',
    title: 'Contractor', outsideDomain: true }],
  directory: 'ok', more: false, hiddenOutside: 2,
};

function mount({ failApply } = {}) {
  const ui = page(FILES, { prep: (win) => { win.qhApi = { adminPeopleDirectory: () => Promise.resolve(DIRECTORY) }; } });
  const bodies = [];
  const added = [];
  const st = {
    people: PEOPLE, teams: TEAMS, connections: CONNS, resolvePerson: () => Promise.resolve(null),
    addPerson: (b) => {
      bodies.push({ ...b });
      if (!b.dryRun) return failApply ? Promise.reject(new Error('Unknown team: 9.')) : Promise.resolve({ principal: b.principal, name: 'Jordan Blake' });
      const person = PEOPLE.find(p => p.handle === b.principal) || { name: 'Riley Stone' };
      const outside = b.principal === OUTSIDE;
      const teams = (b.teamIds || []).map(id => {
        const t = TEAMS.find(x => x.id === id);
        return { id, name: t.name, allTargets: false, targets: [{ connectionId: 'prod-main', tier: 'RW', env: 'production' }] };
      });
      const copy = b.copyFrom ? { from: b.copyFrom, fromName: 'Sam New', targets: [], teams: [], autoApprove: 2 } : null;
      return Promise.resolve({ dryRun: true, principal: b.principal, name: person.name,
        email: outside ? 'riley.stone@other-example.org' : 'x@example.com', outsideDomain: outside,
        known: b.principal === KNOWN, teams, copy,
        newTeams: b.newTeams || [], grants: b.grants || [], role: b.role || null,
        warnings: teams.map(t => ({ connectionId: 'prod-main', tier: 'RW', source: t.name }))
          .concat(outside ? [{ kind: 'outside_domain', email: 'riley.stone@other-example.org' }] : []),
        empty: !teams.length && !copy, notify: b.notify });
    },
  };
  ui.render(ui.React.createElement(ui.win.AddPersonDialog, { st, onClose: () => added.push('closed'), onAdded: (p) => added.push(p) }));
  return { ...ui, bodies, added };
}
const wait = (ui) => ui.React.act(() => new Promise(r => setTimeout(r, 360)));
const focus = (ui, el) => ui.React.act(() => { el.dispatchEvent(new ui.win.FocusEvent('focusin', { bubbles: true })); el.focus(); });
const down = (ui, el) => ui.React.act(() => el.dispatchEvent(new ui.win.MouseEvent('mousedown', { bubbles: true, cancelable: true })));
const setText = (ui, el, text) => ui.React.act(() => {
  Object.getOwnPropertyDescriptor(ui.win.HTMLInputElement.prototype, 'value').set.call(el, text);
  el.dispatchEvent(new ui.win.Event('input', { bubbles: true }));
});
function pickPerson(ui, idx, name) {
  const el = ui.all('.qh-pcombo-in')[idx];
  focus(ui, el);
  down(ui, ui.all('.qh-pcombo-opt').find(b => b.textContent.includes(name)));
}
const lastDry = (ui) => [...ui.bodies].reverse().find(b => b.dryRun);

test('the auto-approve box is hidden without a reference person, then unchecked with the warning', opts, async () => {
  const ui = mount();
  assert.equal(ui.doc.querySelector('.qh-addp-auto'), null);
  assert.equal(ui.doc.querySelector('.qh-addp-warn-auto'), null);
  pickPerson(ui, 0, 'Jordan Blake');
  pickPerson(ui, 1, 'Sam New');
  await wait(ui);
  const box = ui.doc.querySelector('.qh-addp-auto');
  assert.ok(box, 'the box appears with a reference person');
  assert.equal(box.checked, false);
  const warn = ui.doc.querySelector('.qh-addp-warn-auto');
  assert.match(warn.textContent, /Auto-approve skips the DBA review: this person's reads run without anyone checking them\./);
  assert.match(warn.textContent, /2 rules/);
  assert.equal(lastDry(ui).includeAutoApprove, false);
  ui.click(box);
  await wait(ui);
  assert.equal(lastDry(ui).includeAutoApprove, true);
  assert.equal(lastDry(ui).copyFrom, KNOWN);
  assert.match(ui.doc.querySelector('.qh-addp-copyline').textContent, /2 auto-approve rules/);
});

test('notify is checked by default and is sent', opts, async () => {
  const ui = mount();
  assert.equal(ui.doc.querySelector('.qh-addp-notify').checked, true);
  pickPerson(ui, 0, 'Jordan Blake');
  await wait(ui);
  assert.equal(lastDry(ui).principal, HANDLE);
  assert.equal(lastDry(ui).notify, true);
  ui.click(ui.doc.querySelector('.qh-addp-notify'));
  await wait(ui);
  assert.equal(lastDry(ui).notify, false);
});

test('a team typed and confirmed with Enter is sent, and its chip button removes it', opts, async () => {
  const ui = mount();
  pickPerson(ui, 0, 'Jordan Blake');
  const input = ui.doc.querySelector('.qh-addp-team-in');
  focus(ui, input);
  setText(ui, input, 'team o');
  assert.match(ui.doc.querySelector('.qh-addp-teamopt').textContent, /Team One4 members/);
  ui.key(input, 'Enter');
  await wait(ui);
  assert.deepEqual(lastDry(ui).teamIds, [3]);
  assert.equal(ui.all('.qh-addp-chip').length, 1);
  ui.click(ui.doc.querySelector('.qh-addp-chip button[aria-label="Remove team Team One"]'));
  await wait(ui);
  assert.deepEqual(lastDry(ui).teamIds, []);
  assert.equal(ui.all('.qh-addp-chip').length, 0);
});

test('the review warns about RW on a production server, and says so when nothing is chosen', opts, async () => {
  const ui = mount();
  pickPerson(ui, 0, 'Jordan Blake');
  await wait(ui);
  assert.match(ui.doc.querySelector('.qh-addp-emptyline').textContent,
    /They can sign in but reach no server yet\. They can ask with Request database access\./);
  const input = ui.doc.querySelector('.qh-addp-team-in');
  focus(ui, input);
  setText(ui, input, 'one');
  ui.key(input, 'Enter');
  await wait(ui);
  assert.equal(ui.doc.querySelector('.qh-addp-emptyline'), null);
  assert.match(ui.doc.querySelector('.qh-addp-warn').textContent, /RW on a production server: prod-main \(from Team One\)/);
  assert.match(ui.doc.querySelector('.qh-addp-team').textContent, /Team One · 1 server · RW/);
});

test('a known person disables Add person and offers Open their access', opts, async () => {
  const ui = mount();
  pickPerson(ui, 0, 'Sam New');
  await wait(ui);
  assert.match(ui.doc.querySelector('.qh-addp-known').textContent, /Already in QueryHub\./);
  const go = ui.doc.querySelector('.qh-addp-go');
  assert.equal(go.disabled, true);
  assert.match(go.title, /already in QueryHub/);
  ui.click(ui.doc.querySelector('.qh-addp-open'));
  assert.equal(ui.added[0], KNOWN);
});

test('Add person sends dryRun false and calls onAdded', opts, async () => {
  const ui = mount();
  pickPerson(ui, 0, 'Jordan Blake');
  await wait(ui);
  const go = ui.doc.querySelector('.qh-addp-go');
  assert.equal(go.disabled, false);
  ui.click(go);
  await ui.flush();
  const sent = ui.bodies[ui.bodies.length - 1];
  assert.equal(sent.dryRun, false);
  assert.equal(sent.principal, HANDLE);
  assert.deepEqual(ui.added, [HANDLE, 'closed']);
});

test('the Slack box says Notify on Slack', opts, async () => {
  const ui = mount();
  const label = ui.doc.querySelector('.qh-addp-notify').closest('label');
  assert.match(label.textContent, /Notify on Slack/);
  assert.doesNotMatch(label.textContent, /Tell them/);
});

test('the team list closes after a pick, with Enter and with a click', opts, async () => {
  const ui = mount();
  const input = ui.doc.querySelector('.qh-addp-team-in');
  focus(ui, input);
  setText(ui, input, 'team o');
  assert.ok(ui.doc.querySelector('.qh-addp-pop'));
  ui.key(input, 'Enter');
  assert.equal(ui.doc.querySelector('.qh-addp-pop'), null);
  assert.equal(input.value, '');
  setText(ui, input, 'team t');
  down(ui, ui.doc.querySelector('.qh-addp-teamopt'));
  assert.equal(ui.doc.querySelector('.qh-addp-pop'), null);
  assert.equal(ui.all('.qh-addp-chip').length, 2);
});

test('a team lead shows in parentheses in the list and on the chip, and no lead shows none', opts, async () => {
  const ui = mount();
  const input = ui.doc.querySelector('.qh-addp-team-in');
  focus(ui, input);
  setText(ui, input, 'team');
  const opts2 = ui.all('.qh-addp-teamopt').map(b => b.firstChild.textContent);
  assert.deepEqual(opts2, ['Team One', 'Team Two (Sam New)']);
  setText(ui, input, 'team t');
  down(ui, ui.doc.querySelector('.qh-addp-teamopt'));
  assert.match(ui.doc.querySelector('.qh-addp-chip').textContent, /Team Two \(Sam New\)/);
});

test('an outside-domain hit has a chip, the picker counts hidden people, the review warns', opts, async () => {
  const ui = mount();
  const el = ui.all('.qh-pcombo-in')[0];
  focus(ui, el);
  setText(ui, el, 'riley');
  down(ui, ui.doc.querySelector('.qh-pcombo-find'));
  await ui.flush();
  const hit = ui.doc.querySelector('.qh-pcombo-hit');
  assert.match(hit.textContent, /Riley Stone · Contractor/);
  assert.match(hit.querySelector('.qh-addp-outside').textContent, /Outside other-example\.org/);
  assert.match(ui.doc.querySelector('.qh-addp-hidden').textContent, /2 people outside the company domain are hidden\./);
  down(ui, hit);
  await wait(ui);
  assert.match(ui.doc.querySelector('.qh-addp-outside-warn').textContent, /This email is outside other-example\.org\. Check before adding\./);
  assert.equal(ui.all('.qh-addp-outside-warn').length, 1);
});

test('Esc with a change asks first, Keep editing stays, Discard closes', opts, async () => {
  const ui = mount();
  pickPerson(ui, 0, 'Jordan Blake');
  ui.key(ui.doc.body, 'Escape');
  assert.match(ui.doc.querySelector('.qh-addp-discard').textContent, /Discard your changes\?/);
  assert.deepEqual(ui.added, []);
  const btn = (t) => ui.all('.qh-addp-discard button').find(b => b.textContent === t);
  ui.click(btn('Keep editing'));
  assert.equal(ui.doc.querySelector('.qh-addp-discard'), null);
  ui.click(ui.doc.querySelector('.qh-addp-cancel'));
  assert.ok(ui.doc.querySelector('.qh-addp-discard'), 'Cancel asks too');
  ui.click(btn('Discard'));
  assert.deepEqual(ui.added, ['closed']);
});

test('the close icon asks when something changed', opts, async () => {
  const ui = mount();
  pickPerson(ui, 0, 'Jordan Blake');
  ui.click(ui.doc.querySelector('.qh-icon-btn[aria-label="Close"]'));
  assert.ok(ui.doc.querySelector('.qh-addp-discard'));
  assert.deepEqual(ui.added, []);
});

test('Esc with no change closes at once', opts, async () => {
  const ui = mount();
  ui.key(ui.doc.body, 'Escape');
  assert.equal(ui.doc.querySelector('.qh-addp-discard'), null);
  assert.deepEqual(ui.added, ['closed']);
});

test('a New team row, a Servers row and a Role choice are sent in the body', opts, async () => {
  const ui = mount();
  pickPerson(ui, 0, 'Jordan Blake');
  ui.click(ui.doc.querySelector('.qh-addp-add-nt'));
  setText(ui, ui.doc.querySelector('.qh-addp-nt-name'), 'Team Nine');
  ui.click(ui.doc.querySelector('.qh-addp-nt-role [data-v="lead"]'));
  ui.click(ui.doc.querySelector('.qh-addp-add-srv'));
  down(ui, ui.all('.qh-connpick-opt').find(b => b.textContent.includes('prod-main')));
  ui.click(ui.doc.querySelector('.qh-addp-tier [data-v="RW"]'));
  ui.click(ui.doc.querySelector('.qh-addp-role [data-v="approver"]'));
  ui.click(ui.doc.querySelector('.qh-addp-ceil [data-v="RW"]'));
  await wait(ui);
  const d = lastDry(ui);
  assert.deepEqual(d.newTeams, [{ name: 'Team Nine', lead: true }]);
  assert.deepEqual(d.grants, [{ connectionId: 'prod-main', databases: null, tier: 'RW', expiresAt: null }]);
  assert.deepEqual(d.role, { role: 'approver', scopeTeamId: null, scopeTargetId: null, maxTier: 'RW' });
  assert.match(ui.doc.querySelector('.qh-addp-newteamline').textContent, /Team Nine/);
  assert.match(ui.doc.querySelector('.qh-addp-grantline').textContent, /prod-main · RW · all databases · no end date/);
  assert.match(ui.doc.querySelector('.qh-addp-roleline').textContent, /Approver · up to RW/);
  ui.click(ui.doc.querySelector('.qh-addp-go'));
  await ui.flush();
  const sent = ui.bodies[ui.bodies.length - 1];
  assert.equal(sent.dryRun, false);
  assert.deepEqual(sent.grants, d.grants);
  assert.deepEqual(sent.role, d.role);
  assert.deepEqual(sent.newTeams, d.newTeams);
});

test('a refused apply shows the server message and keeps the dialog open', opts, async () => {
  const ui = mount({ failApply: true });
  pickPerson(ui, 0, 'Jordan Blake');
  await wait(ui);
  ui.click(ui.doc.querySelector('.qh-addp-go'));
  await ui.flush();
  assert.equal(ui.doc.querySelector('.qh-addp-submit-err').textContent, 'Unknown team: 9.');
  assert.deepEqual(ui.added, []);
});
