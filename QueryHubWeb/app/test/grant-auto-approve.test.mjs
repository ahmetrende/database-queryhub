// Auto-approve with a grant, and an Auto-approve form that follows reach.
//
// Two changes, both about a waiver never claiming more than the access it
// rides on:
//
//   * the add-grant form can ask for auto-approve in the same call, with a tier
//     capped at the grant's own and never DDL — schema changes are always
//     reviewed, and a tier the server refuses is a control that lies;
//   * the Auto-approve form offers only the connections, databases and tiers
//     the subject can already query, read from their effective access. Before
//     a subject is picked it lists nothing at all.
//
// The forms are mounted for real (jsdom), with a fake `st` in place of the data
// hook, because what is pinned is what an admin can click.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import { bareWindow, loadInto } from './_load.mjs';

const require_ = createRequire(import.meta.url);
const HERE = dirname(fileURLToPath(import.meta.url));
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }
const opts = JSDOM ? {} : { skip: 'jsdom not installed' };

const PERSON = 'U0EXAMPLE002';
const CONNS = [
  { id: 'prod-ledger', name: 'prod-ledger', enabled: true,
    databases: [{ id: 'ledger', name: 'ledger' }, { id: 'audit', name: 'audit' }] },
  { id: 'prod-orders', name: 'prod-orders', enabled: true, databases: [{ id: 'orders', name: 'orders' }] },
  { id: 'prod-other', name: 'prod-other', enabled: true, databases: [{ id: 'misc', name: 'misc' }] },
];
// The person's effective access, in the new model's shape: RO on one database
// of prod-ledger, RW on all of prod-orders, nothing on prod-other.
const EFF = { access: [
  { connectionId: 'prod-ledger', enabled: true, databases: ['ledger'], allDatabases: false, tier: 'RO',
    perDatabase: [{ database: 'ledger', tier: 'RO' }] },
  { connectionId: 'prod-orders', enabled: true, databases: null, allDatabases: true, tier: 'RW',
    perDatabase: [{ database: 'orders', tier: 'RW' }] },
], autoApprove: [] };

function mount(View, stExtra) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>',
                        { pretendToBeVisual: true, url: 'https://localhost/' });
  for (const k of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node', 'Event',
                   'KeyboardEvent', 'getComputedStyle', 'requestAnimationFrame',
                   'cancelAnimationFrame', 'localStorage']) {
    Object.defineProperty(globalThis, k, { value: dom.window[k], configurable: true, writable: true });
  }
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const React = require_('react');
  const ReactDOM = require_('react-dom/client');
  globalThis.React = React;
  const win = dom.window;
  win.React = React;
  win.ReactDOM = require_('react-dom');
  // The Every server block reads its own list (design 2026-10-06 (c)). An empty
  // one is all these tests need: they are about the per-connection grant form.
  win.qhApi = { adminFleetGrants: () => Promise.resolve({ grants: [] }) };
  loadInto(win, 'qh-data.jsx', 'qh-modal.jsx', 'qh-panels.jsx', 'qh-admin-data.jsx',
           'qh-admin-access.jsx', 'qh-admin-person.jsx');
  const rec = { eff: [], teamEff: [], saved: [], added: [] };
  const st = {
    connections: CONNS, people: [{ id: PERSON, handle: PERSON, name: 'Jordan Ray', initials: 'JR' }],
    teams: [{ id: '7', name: 'Team Alpha', members: [PERSON] }],
    grants: [], autoGrants: [], autoRequests: [],
    effectiveAccess: (id) => { rec.eff.push(id); return Promise.resolve(EFF); },
    teamEffectiveAccess: (id) => { rec.teamEff.push(id); return Promise.resolve({ access: [] }); },
    addAutoGrants: (b) => { rec.saved.push(b); return Promise.resolve({ applied: b.targets.length }); },
    addGrant: (p) => { rec.added.push(p); return Promise.resolve({}); },
    resolvePerson: () => Promise.resolve(null),
    revokeAutoGrant() {}, decideAutoRequest() {}, updateAutoGrant() {}, addAutoGrant() {},
    updateGrant() {}, revokeGrant() {}, setSubjectGrants() {},
    ...stExtra,
  };
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(win[View], { st, user: { name: 'Example Admin' } })));
  const doc = win.document;
  const all = (sel) => [...doc.querySelectorAll(sel)];
  const button = (text) => {
    const b = all('button').find(x => x.textContent.trim() === text);
    assert.ok(b, 'no button reads "' + text + '"; have: ' + all('button').map(x => x.textContent.trim()).join(' | '));
    return b;
  };
  return {
    win, doc, rec, all, button,
    click: (el) => React.act(() => { el.click(); }),
    choose: (select, value) => React.act(() => {
      select.value = value;
      select.dispatchEvent(new win.Event('change', { bubbles: true }));
    }),
    settle: () => React.act(async () => { await new Promise(r => setTimeout(r, 0)); }),
  };
}

const optionValues = (select) => [...select.options].map(o => o.value);

// --- the Auto-approve form ------------------------------------------------------

test('before a subject is picked the form lists no connection at all', opts, () => {
  const ui = mount('AutoView');
  ui.click(ui.button('New exemption'));
  assert.equal(ui.all('.qh-autobulk-row').length, 0);
  assert.match(ui.doc.body.textContent, /Pick the person or team first/);
  assert.deepEqual(ui.rec.eff, [], 'nobody picked, nothing fetched');
});

test('once a person is known it offers only what they can query, capped at their tier', opts, async () => {
  const fleet = { id: '41', user: PERSON, userName: 'Jordan Ray', subjectType: 'user', tier: 'RO',
                  connectionId: null, databaseId: null, expiresAt: null, createdByName: 'Example Admin' };
  const ui = mount('AutoView', { autoGrants: [fleet] });
  ui.click(ui.button('Add targets for Jordan Ray'));
  await ui.settle();
  assert.deepEqual(ui.rec.eff, [PERSON]);

  const [connSel, dbSel] = ui.all('.qh-autobulk-row select');
  assert.deepEqual(optionValues(connSel), ['prod-ledger', 'prod-orders'], 'prod-other is not theirs');
  // prod-ledger: only the database they hold, and RO is all they can run there.
  assert.equal(connSel.value, 'prod-ledger');
  assert.deepEqual(optionValues(dbSel), ['', 'ledger']);
  const tierButtons = () => ui.all('.qh-autobulk-opt .qh-seg-opt').map(b => b.textContent.trim());
  assert.deepEqual(tierButtons(), ['RO']);

  ui.choose(connSel, 'prod-orders');
  assert.deepEqual(tierButtons(), ['RO', 'RW']);
  ui.click(ui.button('RW'));
  ui.click(ui.button('Create 1 exemption'));
  await ui.settle();
  const [saved] = ui.rec.saved;
  assert.equal(saved.tier, 'RW');
  assert.deepEqual(saved.targets.map(t => [t.connectionId, t.databaseId]), [['prod-orders', null]]);
});

test('a subject who can query nothing is told so instead of being offered the fleet', opts, async () => {
  const ui = mount('AutoView', {
    autoGrants: [{ id: 'ag:3', user: 'Team Alpha', subjectType: 'team', tier: 'RO',
                   connectionId: 'prod-ledger', databaseId: 'ledger', expiresAt: null }],
  });
  // The server sends a team as its plain name with subjectType, not "<name> (team)".
  ui.click(ui.button('Add targets for Team Alpha'));
  await ui.settle();
  assert.deepEqual(ui.rec.teamEff, ['7'], 'a team is asked about by its id');
  assert.equal(ui.all('.qh-autobulk-row').length, 0);
  assert.match(ui.doc.body.textContent, /cannot query any connection yet/);
});

test('a fleet-wide row says where it applies instead of "all databases"', opts, () => {
  const fleet = { id: '41', user: PERSON, userName: 'Jordan Ray', subjectType: 'user', tier: 'RO',
                  connectionId: null, databaseId: null, expiresAt: null };
  const ui = mount('AutoView', { autoGrants: [fleet] });
  assert.match(ui.doc.querySelector('.qh-autosub-t').textContent, /^every server they can reach$/);
  ui.click(ui.button('All rows'));
  assert.match(ui.doc.querySelector('.qh-acttable tbody td.qh-mono').textContent,
               /^every server they can reach$/);
});

// --- the add-grant form -----------------------------------------------------------

test('the grant form asks for auto-approve with a tier capped at the grant, never DDL', opts, () => {
  const ui = mount('GrantsView');
  ui.click(ui.button('By grant'));
  ui.click(ui.button('New grant'));
  ui.click(ui.button('team'));
  const teamSel = ui.all('.qh-addrow select').find(s => optionValues(s).includes('Team Alpha'));
  ui.choose(teamSel, 'Team Alpha');

  const box = ui.all('.qh-addrow label.qh-percopy-tm input[type=checkbox]')[0];
  assert.ok(box, 'no auto-approve box on the add form');
  ui.click(box);
  const autoTiers = () => ui.all('.qh-addrow .qh-seg-opt').map(b => b.textContent.trim())
    .filter(t => t.startsWith('up to'));
  assert.deepEqual(autoTiers(), ['up to RO'], 'an RO grant carries an RO waiver at most');
  ui.click(ui.all('.qh-addrow .qh-seg-opt').find(b => b.textContent.trim() === 'DDL'));
  assert.deepEqual(autoTiers(), ['up to RO', 'up to RW'], 'DDL is never waived');
  ui.click(ui.button('up to RW'));
  // Lowering the grant pulls the waiver down with it.
  ui.click(ui.all('.qh-addrow .qh-seg-opt').find(b => b.textContent.trim() === 'RO'));
  assert.deepEqual(autoTiers(), ['up to RO']);
  ui.click(ui.button('Add'));
  const [p] = ui.rec.added;
  assert.equal(p.subjectType, 'team');
  assert.equal(p.autoApprove, true);
  assert.equal(p.autoApproveTier, 'RO');
});

test('an untouched box sends no auto-approve, and editing a grant offers none', opts, () => {
  const g = { id: 'u:' + PERSON + ':53', subjectType: 'user', subject: PERSON, subjectName: 'Jordan Ray',
              connectionId: 'prod-ledger', databases: ['*'], tier: 'RW', expiresAt: null };
  const ui = mount('GrantsView', { grants: [g] });
  ui.click(ui.button('By grant'));
  ui.click(ui.all('button').find(b => b.textContent.trim() === 'Edit'));
  assert.equal(ui.all('.qh-addrow label.qh-percopy-tm').length, 0);
});

// --- the pieces, without a DOM ----------------------------------------------------

test('reach is read per database where the answer says so', () => {
  const win = loadInto(bareWindow(), 'qh-data.jsx', 'qh-admin-data.jsx', 'qh-admin-access.jsx');
  const reach = win.qhAutoReach(EFF);
  assert.deepEqual(reach['prod-ledger'], { top: 'RO', all: false, dbs: { ledger: 'RO' } });
  assert.deepEqual(reach['prod-orders'], { top: 'RW', all: true, dbs: { orders: 'RW' } });
  // A team's row names null for "every database"; mixed tiers keep their own.
  const team = win.qhAutoReach({ access: [{ connectionId: 'c', tier: 'RW', allDatabases: true,
    databases: ['a'], perDatabase: [{ database: null, tier: 'RO' }, { database: 'a', tier: 'RW' }] }] });
  assert.deepEqual(team.c, { top: 'RW', all: true, dbs: { a: 'RW' } });
  // The old model: no perDatabase, one tier for the databases it names.
  const legacy = win.qhAutoReach({ access: [{ connectionId: 'c', tier: 'RO', databases: ['x', 'y'] }] });
  assert.deepEqual(legacy.c, { top: 'RO', all: false, dbs: { x: 'RO', y: 'RO' } });
  assert.deepEqual(win.qhAutoReach(null), {});
});

test('the toast names a skipped waiver', () => {
  const win = loadInto(bareWindow(), 'qh-data.jsx', 'qh-admin-data.jsx');
  const say = win.qhGrantAutoSay;
  assert.equal(say({}), '');
  assert.equal(say({ autoApprove: { tier: 'RO', written: 2, skipped: [] } }), ' Auto-approve up to RO is on.');
  const why = 'already covered by auto-approve #12 (up to RO on every server they can reach, no expiry)';
  assert.equal(say({ autoApprove: { tier: 'RO', written: 0, skipped: [{ reason: why }] } }),
               ' Auto-approve skipped: ' + why + '.');
  assert.match(say({ autoApprove: { tier: 'RO', written: 1, skipped: [{ reason: why }] } }),
               /1 added, 1 skipped/);
});

test('the grant payload is spread first, so the auto-approve fields reach the server', async () => {
  // The hook: nothing it does not name may be dropped on the way.
  const hook = readFileSync(resolve(HERE, '..', 'src', 'qh-admin-data.jsx'), 'utf8');
  assert.match(hook, /qhApi\.adminAddGrant\(\{ \.\.\.g,/);
  // The client: the body goes out exactly as handed over.
  const win = bareWindow();
  let sent = null;
  win.fetch = async (url, init) => { sent = JSON.parse(init.body);
    return { ok: true, status: 201, json: async () => ({}), text: async () => '{}' }; };
  await loadInto(win, 'qh-api.jsx').qhApi.adminAddGrant({ subject: PERSON, autoApprove: true, autoApproveTier: 'RO' });
  assert.equal(sent.autoApprove, true);
  assert.equal(sent.autoApproveTier, 'RO');
});
