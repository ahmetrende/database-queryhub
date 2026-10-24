// An auto-approve rule needs an end date, and is RO or RW.
//
// The server refuses a rule made in the app with no end (`end_required`), with
// an end later than `auto_approve_max_days` from now (`end_too_far`), and for
// tier DDL (`tier_not_allowed`). `GET /admin/auto-grants` says the limit as
// `maxDays`. A rule that exists with no end (made with SQL, or older than the
// limit) still shows in the list.
//
// What is pinned here is what an admin can see and click, in every form that
// makes a rule: the Auto-approve form, its Edit form, the grant form with its
// auto-approve box, and the Every server block. The end is required there, "No
// expiry" is not offered, the picks and the date field stop at the limit, Save
// is off with the reason as its title until the end is good, and the end that
// is sent stays inside the limit. The forms are mounted for real (jsdom) with a
// fake `st`. The mock refuses what the server refuses.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { transformSync } from 'esbuild';

import { bareWindow, loadInto } from './_load.mjs';

const require_ = createRequire(import.meta.url);
const HERE = dirname(fileURLToPath(import.meta.url));
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }
const opts = JSDOM ? {} : { skip: 'jsdom not installed' };

const DAY = 86400000;
const PERSON = 'U0EXAMPLE002';
const TIER_SAY = 'An auto-approve rule can be RO or RW. Schema changes always need a DBA.';
const CONNS = [
  { id: 'prod-ledger', name: 'prod-ledger', enabled: true,
    databases: [{ id: 'ledger', name: 'ledger' }, { id: 'audit', name: 'audit' }] },
  { id: 'prod-orders', name: 'prod-orders', enabled: true, databases: [{ id: 'orders', name: 'orders' }] },
];
const EFF = { access: [
  { connectionId: 'prod-ledger', enabled: true, databases: null, allDatabases: true, tier: 'RW',
    perDatabase: [{ database: 'ledger', tier: 'RW' }] },
], autoApprove: [] };
// A day as the date field writes it: local, YYYY-MM-DD.
const ymd = (offsetDays) => {
  const d = new Date(Date.now() + offsetDays * DAY);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
};

function mount(View, stExtra, user) {
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
  win.qhApi = { adminFleetGrants: () => Promise.resolve({ grants: [] }) };
  win.confirm = () => true;
  loadInto(win, 'qh-data.jsx', 'qh-modal.jsx', 'qh-panels.jsx', 'qh-admin-data.jsx',
           'qh-admin-access.jsx', 'qh-admin-person.jsx');
  const rec = { updated: [], saved: [], added: [], fleet: [] };
  const st = {
    connections: CONNS, people: [{ id: PERSON, handle: PERSON, name: 'Jordan Ray', initials: 'JR' }],
    teams: [{ id: '7', name: 'Team Alpha', members: [PERSON] }], grants: [], autoGrants: [], autoRequests: [], scopes: [],
    effectiveAccess: () => Promise.resolve(EFF),
    teamEffectiveAccess: () => Promise.resolve({ access: [] }),
    addAutoGrants: (b) => { rec.saved.push(b); return Promise.resolve({ applied: b.targets.length }); },
    addGrant: (p) => { rec.added.push(p); return Promise.resolve({}); },
    addFleetGrant: (p) => { rec.fleet.push(p); return Promise.resolve({}); },
    updateAutoGrant: (g) => { rec.updated.push(g); },
    resolvePerson: () => Promise.resolve(null),
    revokeAutoGrant() {}, decideAutoRequest() {}, addAutoGrant() {},
    updateGrant() {}, revokeGrant() {}, setSubjectGrants() {},
    ...stExtra,
  };
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(win[View], { st, user: user || { name: 'Example Admin' } })));
  const doc = win.document;
  const all = (sel, from) => [...(from || doc).querySelectorAll(sel)];
  const button = (text, from) => {
    const b = all('button', from).find(x => x.textContent.trim() === text);
    assert.ok(b, 'no button reads "' + text + '"; have: ' + all('button', from).map(x => x.textContent.trim()).join(' | '));
    return b;
  };
  return {
    win, doc, rec, all, button,
    click: (el) => React.act(() => { el.click(); }),
    choose: (select, value) => React.act(() => {
      select.value = value;
      select.dispatchEvent(new win.Event('change', { bubbles: true }));
    }),
    type: (input, value) => React.act(() => {
      const set = Object.getOwnPropertyDescriptor(win.HTMLInputElement.prototype, 'value').set;
      set.call(input, value);
      input.dispatchEvent(new win.Event('input', { bubbles: true }));
    }),
    settle: () => React.act(async () => { await new Promise(r => setTimeout(r, 0)); }),
  };
}

const optionValues = (select) => [...select.options].map(o => o.value);
const rule = (extra) => ({ id: '11', user: PERSON, userName: 'Jordan Ray', subjectType: 'user', tier: 'RO',
  connectionId: 'prod-ledger', databaseId: null, expiresAt: null, createdByName: 'Example Admin',
  enabled: true, runCount: 3, ...extra });

// Open the Edit form of the one rule on the screen.
function edit(r, stExtra) {
  const ui = mount('AutoView', { autoGrants: [r], ...stExtra });
  ui.click(ui.button('Edit'));
  const form = ui.doc.querySelector('.qh-autosub-edit .qh-addrow');
  assert.ok(form, 'the Edit form opened');
  const select = form.querySelector('.qh-exppick select');
  const saveWrap = () => ui.button('Save', form).parentElement;
  return { ui, form, select, save: () => ui.button('Save', form), saveWrap };
}

// --- the helpers ---------------------------------------------------------------

const win0 = loadInto(bareWindow(), 'qh-data.jsx', 'qh-admin-data.jsx', 'qh-admin-access.jsx');


test('the picks are +7, +30 and +90 where they fit, and the limit itself', () => {
  const { endPicks, endDefault } = win0;
  assert.deepEqual(endPicks(90), [7, 30, 90]);
  assert.deepEqual(endPicks(365), [7, 30, 90, 365]);
  assert.deepEqual(endPicks(30), [7, 30]);
  assert.deepEqual(endPicks(14), [7, 14]);
  assert.deepEqual(endPicks(5), [5]);
  assert.equal(endDefault(90), '30');
  assert.equal(endDefault(14), '14');
  for (const m of [1, 5, 7, 14, 30, 90, 365]) assert.ok(endPicks(m).map(String).includes(endDefault(m)), 'the default is a pick at ' + m);
});

test('autoMaxOf reads the limit the server sent, and says 90 when it sent none', () => {
  const { autoMaxOf } = win0;
  assert.equal(autoMaxOf({ autoMaxDays: 30 }), 30);
  for (const st of [undefined, {}, { autoMaxDays: 0 }, { autoMaxDays: 'x' }, { autoMaxDays: null }]) assert.equal(autoMaxOf(st), 90);
});

test('expIso clamps an end to the limit minus a minute, and leaves other forms alone', () => {
  const { expIso } = win0;
  const at = (iso) => Date.parse(iso) - Date.now();
  // A quick pick at the limit counts from now and is a minute short of it.
  assert.ok(Math.abs(at(expIso({ ttl: '90', expDate: '' }, null, 90)) - (90 * DAY - 60000)) < 5000);
  // A picked day ends at 23:59:59, which can be hours past the limit: clamped.
  assert.ok(at(expIso({ ttl: 'date', expDate: ymd(90) }, null, 90)) <= 90 * DAY - 60000 + 1000);
  // A pick within the limit is not moved.
  assert.ok(Math.abs(at(expIso({ ttl: '7', expDate: '' }, null, 90)) - 7 * DAY) < 5000);
  // Without a limit nothing is clamped (grants, scopes, roles).
  assert.ok(Math.abs(at(expIso({ ttl: '365', expDate: '' })) - 365 * DAY) < 5000);
  assert.equal(expIso({ ttl: 'none' }, null, 90), null);
});

test('autoEndWhy gives the reason an end cannot be saved, and null for a good one', () => {
  const { autoEndWhy } = win0;
  assert.match(autoEndWhy({ ttl: 'none' }, 90), /Pick an end date\. An auto-approve rule cannot be open-ended\./);
  assert.equal(autoEndWhy({ ttl: '30' }, 90), null);
  assert.equal(autoEndWhy({ ttl: '90' }, 90), null);
  assert.match(autoEndWhy({ ttl: '90' }, 30), /at most 30 days from now/);
  assert.equal(autoEndWhy({ ttl: 'date', expDate: ymd(3) }, 14), null);
  assert.match(autoEndWhy({ ttl: 'date', expDate: ymd(15) }, 14), /at most 14 days from now/);
  assert.match(autoEndWhy({ ttl: 'date', expDate: ymd(-1) }, 14), /from today on/);
  assert.match(autoEndWhy({ ttl: 'date', expDate: '' }, 14), /Pick an end date/);
  assert.equal(autoEndWhy({ ttl: 'keep', expiresAt: new Date(Date.now() + 10 * DAY).toISOString() }, 14), null);
  assert.match(autoEndWhy({ ttl: 'keep', expiresAt: new Date(Date.now() + 40 * DAY).toISOString() }, 14), /Pick a new end date/);
  assert.match(autoEndWhy({ ttl: 'keep', expiresAt: null }, 14), /Pick a new end date/);
});

// --- Edit a rule: the form asks for an end ------------------------------------------

test('a rule with no end opens with the end required, 30 days picked, and says so', opts, () => {
  const { ui, form, select, save, saveWrap } = edit(rule());
  assert.match(form.textContent, /This rule has no end\. Pick one to save\./);
  assert.deepEqual(optionValues(select), ['7', '30', '90', 'date'], 'no "No expiry" and no "Keep"');
  assert.equal(select.value, '30');
  assert.equal(save().disabled, false);
  assert.equal(saveWrap().title, '');
  ui.click(save());
  const [sent] = ui.rec.updated;
  assert.equal(sent.id, '11');
  const at = Date.parse(sent.expiresAt) - Date.now();
  assert.ok(Math.abs(at - 30 * DAY) < 5000, 'ends in 30 days: ' + at);
});

test('the picks and the date field stop at the limit the server sent', opts, () => {
  const { ui, form, select, save, saveWrap } = edit(rule(), { autoMaxDays: 14 });
  assert.deepEqual(optionValues(select), ['7', '14', 'date']);
  assert.equal(select.value, '14', 'the default is the limit when it is shorter than 30');
  ui.choose(select, 'date');
  const date = form.querySelector('input[type=date]');
  assert.equal(date.max, ymd(14));
  // Past the limit: Save is off, and the reason is on its title.
  ui.type(date, ymd(20));
  assert.equal(save().disabled, true);
  assert.match(saveWrap().title, /The end can be at most 14 days from now\./);
  assert.ok(date.classList.contains('is-err'));
  // Inside it: Save works, and the end sent is inside the limit.
  ui.type(date, ymd(3));
  assert.equal(save().disabled, false);
  ui.click(save());
  const at = Date.parse(ui.rec.updated[0].expiresAt) - Date.now();
  assert.ok(at > 2 * DAY && at < 4 * DAY + DAY, 'about three days: ' + at);
});

test('the last day of the limit sends an end inside it, not 23:59 of that day', opts, () => {
  const { ui, form, select, save } = edit(rule(), { autoMaxDays: 14 });
  ui.choose(select, 'date');
  ui.type(form.querySelector('input[type=date]'), ymd(14));
  assert.equal(save().disabled, false);
  ui.click(save());
  const at = Date.parse(ui.rec.updated[0].expiresAt) - Date.now();
  assert.ok(at <= 14 * DAY - 60000 + 1000 && at > 13 * DAY, 'clamped to the limit minus a minute: ' + at);
});

test('a rule whose end is inside the limit can keep it, and keeps it as it was', opts, () => {
  const end = new Date(Date.now() + 10 * DAY).toISOString();
  const { ui, form, select, save } = edit(rule({ expiresAt: end }));
  assert.equal(select.value, 'keep');
  assert.match([...select.options][0].textContent, /^Keep \(/);
  assert.doesNotMatch(form.textContent, /This rule has no end/);
  ui.click(save());
  assert.equal(ui.rec.updated[0].expiresAt, end);
});

test('a rule whose end is past the limit cannot keep it', opts, () => {
  const end = new Date(Date.now() + 40 * DAY).toISOString();
  const { ui, form, select, save } = edit(rule({ expiresAt: end }), { autoMaxDays: 14 });
  assert.ok(!optionValues(select).includes('keep'));
  assert.match(form.textContent, /This rule ends later than the limit of 14 days\. Pick an end within it to save\./);
  assert.equal(select.value, '14');
  ui.click(save());
  assert.ok(Date.parse(ui.rec.updated[0].expiresAt) - Date.now() <= 14 * DAY);
});

test('the Edit form offers RO and RW, never DDL, and a DDL rule must be changed to save', opts, () => {
  const { ui, form, save, saveWrap } = edit(rule({ tier: 'DDL' }));
  const tiers = () => ui.all('.qh-seg-opt', form).map(b => b.textContent.trim()).filter(t => /^(RO|RW|DDL)$/.test(t));
  assert.deepEqual(tiers(), ['RO', 'RW']);
  assert.match(form.textContent, new RegExp(TIER_SAY.replace(/[.]/g, '\\.')));
  assert.equal(save().disabled, true);
  assert.ok(saveWrap().title.includes(TIER_SAY));
  ui.click(ui.all('.qh-seg-opt', form).find(b => b.textContent.trim() === 'RW'));
  assert.equal(save().disabled, false);
  ui.click(save());
  assert.equal(ui.rec.updated[0].tier, 'RW');
});

// --- The Auto-approve form -----------------------------------------------------------

async function bulkForm(stExtra) {
  const fleet = rule({ id: '41', connectionId: null });
  const ui = mount('AutoView', { autoGrants: [fleet], ...stExtra });
  ui.click(ui.button('Add targets for Jordan Ray'));
  await ui.settle();
  const form = ui.doc.querySelector('.qh-autobulk');
  assert.ok(form);
  const select = form.querySelector('.qh-autobulk-opts .qh-exppick select');
  assert.ok(select, 'the end picker is in the form');
  const create = () => ui.button('Create 1 exemption', form);
  return { ui, form, select, create };
}

test('the Auto-approve form requires an end: no "No end date", 30 days picked', opts, async () => {
  const { select, create } = await bulkForm();
  assert.deepEqual(optionValues(select), ['7', '30', '90', 'date']);
  assert.equal(select.value, '30');
  assert.equal(create().disabled, false);
});

test('the Auto-approve form stops at the limit and sends an end inside it', opts, async () => {
  const { ui, form, select, create } = await bulkForm({ autoMaxDays: 30 });
  assert.deepEqual(optionValues(select), ['7', '30', 'date']);
  ui.choose(select, 'date');
  const date = form.querySelector('input[type=date]');
  assert.equal(date.max, ymd(30));
  ui.type(date, ymd(31));
  assert.equal(create().disabled, true);
  assert.match(create().parentElement.title, /at most 30 days from now/);
  ui.type(date, ymd(5));
  assert.equal(create().disabled, false);
  assert.equal(create().parentElement.title, '');
  ui.click(create());
  await ui.settle();
  const [saved] = ui.rec.saved;
  const at = Date.parse(saved.expiresAt) - Date.now();
  assert.ok(at > 4 * DAY && at < 6 * DAY, 'about five days: ' + at);
  assert.equal(saved.tier, 'RO');
});

test('the Auto-approve form offers RO and RW only', opts, async () => {
  const { ui, form } = await bulkForm();
  const tiers = ui.all('.qh-autobulk-opt .qh-seg-opt', form).map(b => b.textContent.trim());
  assert.ok(tiers.every(t => t === 'RO' || t === 'RW'), tiers.join('|'));
});

test('the Auto-approve screen still lists a rule with no end', opts, () => {
  const ui = mount('AutoView', { autoGrants: [rule()] });
  assert.match(ui.doc.querySelector('.qh-autosub-row').textContent, /No expiry/);
  assert.match(ui.doc.querySelector('.qh-autosub-sum').textContent, /1 with no end date/);
});

// --- The grant form: the box makes the end required -----------------------------------

function grantForm(stExtra) {
  const ui = mount('GrantsView', stExtra);
  ui.click(ui.button('By grant'));
  ui.click(ui.button('New grant'));
  ui.click(ui.button('team'));
  const form = ui.doc.querySelector('.qh-addrow');
  ui.choose(ui.all('select', form).find(s => optionValues(s).includes('Team Alpha')), 'Team Alpha');
  const select = () => form.querySelector('.qh-exppick select');
  const box = form.querySelector('label.qh-percopy-tm input[type=checkbox]');
  return { ui, form, select, box };
}

test('a grant may have no end, until the auto-approve box is ticked', opts, () => {
  const { ui, form, select, box } = grantForm();
  assert.ok(optionValues(select()).includes('none'));
  assert.equal(select().value, 'none');
  ui.click(box);
  assert.deepEqual(optionValues(select()), ['7', '30', '90', 'date'], 'the box hides "No expiry"');
  assert.equal(select().value, '30', 'and starts the end on 30 days');
  ui.click(box);
  assert.ok(optionValues(select()).includes('none'), 'unticked, the grant may have no end again');
  assert.match(form.textContent, /Auto-approve/);
});

test('with the box ticked the end stops at the limit, and Add says why it is off', opts, () => {
  const { ui, form, select, box } = grantForm({ autoMaxDays: 14 });
  ui.click(box);
  assert.deepEqual(optionValues(select()), ['7', '14', 'date']);
  ui.choose(select(), 'date');
  ui.type(form.querySelector('input[type=date]'), ymd(30));
  const add = ui.button('Add', form);
  assert.equal(add.disabled, true);
  assert.match(add.parentElement.title, /at most 14 days from now/);
  assert.match(form.textContent, /An auto-approve rule needs an end date, at most 14 days from now\./);
});

test('a grant with the box ticked sends an end that is inside the limit', opts, () => {
  const { ui, form, box } = grantForm();
  ui.click(box);
  ui.click(ui.button('Add', form));
  const [p] = ui.rec.added;
  assert.equal(p.autoApprove, true);
  const at = Date.parse(p.expiresAt) - Date.now();
  assert.ok(Math.abs(at - 30 * DAY) < 5000, 'ends in 30 days: ' + at);
});

test('a grant without the box sends no end, as before', opts, () => {
  const { ui, form } = grantForm();
  ui.click(ui.button('Add', form));
  const [p] = ui.rec.added;
  assert.equal(p.expiresAt, null);
  assert.equal(p.autoApprove, undefined);
});

// --- The Every server block ------------------------------------------------------------

test('reads that skip review on every server need an end, and plain access does not', opts, () => {
  const ui = mount('GrantsView', { autoMaxDays: 30 }, { name: 'Example Admin', role: 'super' });
  ui.click(ui.button('Add'));
  const form = ui.doc.querySelector('.qh-fleet-form');
  const select = () => form.querySelector('.qh-exppick select');
  assert.ok(optionValues(select()).includes('none'));
  const box = [...form.querySelectorAll('label.qh-percopy-tm')].find(l => /Reads skip review/.test(l.textContent)).querySelector('input');
  ui.click(box);
  assert.deepEqual(optionValues(select()), ['7', '30', 'date']);
  assert.equal(select().value, '30');
  assert.equal(ui.button('Grant RO on every server', form).disabled, true, 'no person yet');
  ui.click(box);
  assert.ok(optionValues(select()).includes('none'));
});

// --- the mock refuses what the server refuses ---------------------------------------------

function mockApi() {
  const win = bareWindow();
  win.setTimeout = (f) => { Promise.resolve().then(f); return 0; };
  win.clearTimeout = () => {};
  win.setInterval = () => 0;
  win.clearInterval = () => {};
  loadInto(win, 'qh-data.jsx');
  const src = readFileSync(resolve(HERE, '..', '..', 'qh-api-mock.jsx'), 'utf8');
  const code = transformSync(src, { loader: 'jsx', jsx: 'transform',
    jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment' }).code;
  new Function('window', 'with (window) {\n' + code + '\n}')(win);
  return win.qhApi;
}
const iso = (ms) => new Date(Date.now() + ms).toISOString();
const code = (c, status = 400) => (e) => e.code === c && e.status === status;

test('the mock says the limit with the list', async () => {
  const r = await mockApi().adminAutoGrants();
  assert.equal(r.maxDays, 90);
  assert.ok(r.autoGrants.some(a => !a.expiresAt), 'a rule with no end still exists in the list');
});

test('the mock refuses a rule with no end, too long an end and DDL, on the single route', async () => {
  const api = mockApi();
  const b = { user: PERSON, connectionId: 'prod-main', databaseId: null, tier: 'RO' };
  await assert.rejects(api.adminAddAutoGrant({ ...b, expiresAt: null }), code('end_required'));
  await assert.rejects(api.adminAddAutoGrant({ ...b, expiresAt: iso(100 * DAY) }), code('end_too_far'));
  await assert.rejects(api.adminAddAutoGrant({ ...b, expiresAt: iso(-DAY) }), code('end_in_past'));
  await assert.rejects(api.adminAddAutoGrant({ ...b, tier: 'DDL', expiresAt: iso(DAY) }),
    (e) => e.code === 'tier_not_allowed' && e.message === TIER_SAY);
});

test('the mock checks the end of the bulk route before a dry run, and refuses DDL', async () => {
  const api = mockApi();
  const b = { subjectType: 'user', subject: PERSON, targets: [{ connectionId: 'prod-main' }], tier: 'RO', dryRun: true };
  await assert.rejects(api.adminAddAutoGrantsBulk({ ...b, expiresAt: null }), code('end_required'));
  await assert.rejects(api.adminAddAutoGrantsBulk({ ...b, expiresAt: iso(100 * DAY) }), code('end_too_far'));
  await assert.rejects(api.adminAddAutoGrantsBulk({ ...b, tier: 'DDL', expiresAt: iso(DAY) }), code('tier_not_allowed'));
});

test('the mock refuses a grant that carries auto-approve with no end, or for DDL', async () => {
  const api = mockApi();
  const b = { subjectType: 'user', subject: PERSON, connectionId: 'prod-main', tier: 'RW', autoApprove: true, autoApproveTier: 'RO' };
  await assert.rejects(api.adminAddGrant({ ...b }), code('end_required'));
  await assert.rejects(api.adminAddGrant({ ...b, expiresAt: iso(100 * DAY) }), code('end_too_far'));
  await assert.rejects(api.adminAddGrant({ ...b, expiresAt: iso(DAY), autoApproveTier: 'DDL' }), code('tier_not_allowed'));
  // A grant alone may still have no end.
  const r = await api.adminAddGrant({ subjectType: 'user', subject: PERSON, connectionId: 'prod-main', tier: 'RO' });
  assert.ok(r.id);
});

test('the mock copies a rule with no end under the limit, and skips a DDL rule', async () => {
  const api = mockApi();
  const { autoGrants } = await api.adminAutoGrants();
  const src = autoGrants.find(a => !a.expiresAt && a.subjectType !== 'team' && !String(a.user).includes('(team)'));
  assert.ok(src, 'a seeded rule with no end');
  const { people } = await api.adminPeople();
  const from = people.find(p => p.handle === src.user);
  const to = people.find(p => p.handle !== src.user && !autoGrants.some(a => a.user === p.handle));
  assert.ok(from && to, 'two people');
  const r = await api.adminCopyAccess(to.id, { source: from.id, includeTeams: false, includeAutoApprove: true });
  assert.ok(r.autoApproveCopied >= 1);
  const after = (await api.adminAutoGrants()).autoGrants.filter(a => a.user === to.handle);
  assert.equal(after.length, r.autoApproveCopied);
  for (const a of after) {
    assert.ok(a.expiresAt, 'every copied rule has an end');
    assert.ok(Date.parse(a.expiresAt) <= Date.now() + 90 * DAY + 1000);
  }
});

test('the real client sends maxDays through untouched: it names no field of the list', () => {
  const src = readFileSync(resolve(HERE, '..', '..', 'qh-api.jsx'), 'utf8');
  assert.match(src, /adminAutoGrants:\s*\(\)\s*=>\s*qhFetch\('\/admin\/auto-grants'\)/);
});
