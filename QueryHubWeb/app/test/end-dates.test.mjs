// End dates on the access screens: extend a grant, pause a rule, end a scope.
//
// The API has served these since 2026-10-22:
//   * PATCH /admin/grants/{id} takes `expiresAt` and `reason`. An ended grant
//     answers 409 `grant_ended`.
//   * GET /admin/auto-grants adds `enabled` and `runCount`, and PATCH
//     /admin/auto-grants/{id} takes `enabled`. An ended rule answers 409.
//   * POST /admin/scopes takes `expiresAt`. Left out, the stored end is kept.
//
// What is pinned here is what an admin can see and click: one rule for "ends
// soon" (`qhEndState`), no Extend and no Pause on something that has ended, a
// paused rule that says so in words, a run count only where the API gave one,
// and a scope form that sends `expiresAt` only when the admin chose an end.
// The screens are mounted for real (jsdom) with a fake `st`.
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
const at = (ms) => new Date(Date.now() + ms).toISOString();

// --- the one rule ---------------------------------------------------------------

const { qhEndState } = loadInto(bareWindow(), 'qh-data.jsx');
const NOW = Date.parse('2026-10-23T12:00:00Z');
const off = (ms) => new Date(NOW + ms).toISOString();

test('no end date is its own state, and the caller picks the word', () => {
  assert.deepEqual(qhEndState(null, NOW), { state: 'none', label: '' });
  assert.deepEqual(qhEndState('', NOW), { state: 'none', label: '' });
});

test('an end more than seven days away is live and says the time left', () => {
  assert.deepEqual(qhEndState(off(45 * DAY), NOW), { state: 'live', label: '45d left' });
  assert.equal(qhEndState(off(7 * DAY + 60000), NOW).state, 'live');
});

test('an end within seven days is soon, in words, with the time left', () => {
  assert.deepEqual(qhEndState(off(7 * DAY), NOW), { state: 'soon', label: 'Ends soon · 7d left' });
  assert.deepEqual(qhEndState(off(3 * DAY), NOW), { state: 'soon', label: 'Ends soon · 3d left' });
  // Under 48 hours the count is in hours: 47 hours never reads "1d left".
  assert.deepEqual(qhEndState(off(47 * 3600000), NOW), { state: 'soon', label: 'Ends soon · 47h left' });
  assert.deepEqual(qhEndState(off(20 * 60000), NOW), { state: 'soon', label: 'Ends soon · 20m left' });
});

test('an end that has passed is ended, and the moment itself counts as passed', () => {
  assert.deepEqual(qhEndState(off(-6 * DAY), NOW), { state: 'ended', label: 'Ended 6d ago' });
  assert.equal(qhEndState(off(0), NOW).state, 'ended');
});

test('a date that does not parse shows as it came, never as no end', () => {
  assert.deepEqual(qhEndState('soon-ish', NOW), { state: 'live', label: 'Ends soon-ish' });
});

// --- the screens ----------------------------------------------------------------

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
  const asked = [];
  win.confirm = (text) => { asked.push(text); return true; };
  loadInto(win, 'qh-data.jsx', 'qh-modal.jsx', 'qh-panels.jsx', 'qh-admin-data.jsx',
           'qh-admin-access.jsx', 'qh-admin-person.jsx');
  const rec = { extended: [], toggled: [], scopes: [] };
  const st = {
    connections: [], people: [], teams: [], grants: [], autoGrants: [], autoRequests: [], scopes: [],
    extendGrant: (id, b) => { rec.extended.push([id, b]); return Promise.resolve({}); },
    setAutoGrantEnabled: (id, on) => { rec.toggled.push([id, on]); return Promise.resolve({ changed: true }); },
    saveScope: (s) => { rec.scopes.push(s); },
    resolvePerson: () => Promise.resolve(null),
    revokeAutoGrant() {}, decideAutoRequest() {}, updateAutoGrant() {}, addAutoGrant() {},
    updateGrant() {}, revokeGrant() {}, setSubjectGrants() {}, removeScope() {},
    ...stExtra,
  };
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(win[View], { st, user: user || { name: 'Example Admin', role: 'super' } })));
  const doc = win.document;
  const all = (sel, from) => [...(from || doc).querySelectorAll(sel)];
  const buttons = (from) => all('button', from).map(b => b.textContent.trim());
  const button = (text, from) => {
    const b = all('button', from).find(x => x.textContent.trim() === text);
    assert.ok(b, 'no button reads "' + text + '"; have: ' + buttons(from).join(' | '));
    return b;
  };
  // A table row, or in the by-subject view the one rule row of that subject's
  // card (the name is in the card head, the rule in the row under it).
  const rowOf = (text) => {
    const r = all('tr, .qh-autosub').find(x => x.textContent.includes(text));
    assert.ok(r, 'no row holds "' + text + '"');
    return r.classList.contains('qh-autosub') ? r.querySelector('.qh-autosub-row') : r;
  };
  return {
    win, doc, rec, asked, all, button, buttons, rowOf,
    click: (el) => React.act(() => { el.click(); }),
    type: (input, value) => React.act(() => {
      const set = Object.getOwnPropertyDescriptor(win.HTMLInputElement.prototype, 'value').set;
      set.call(input, value);
      input.dispatchEvent(new win.Event('input', { bubbles: true }));
    }),
    settle: () => React.act(async () => { await new Promise(r => setTimeout(r, 0)); }),
  };
}

const grant = (id, name, expiresAt) => ({ id, subjectType: 'user', subject: 'U0EXAMPLE' + id.slice(-3),
  subjectName: name, connectionId: 'prod-main', databases: ['*'], tier: 'RO', expiresAt,
  grantedBy: null, grantedByName: null, grantedAt: at(-30 * DAY) });
const GRANTS = [
  grant('u:x:101', 'Jordan Ray', null),
  grant('u:x:102', 'Casey Lin', at(2 * DAY)),
  grant('u:x:103', 'Morgan Lee', at(40 * DAY)),
  grant('u:x:104', 'Riley Park', at(-6 * DAY)),
];

function byGrant(stExtra) {
  const ui = mount('GrantsView', { grants: GRANTS, ...stExtra });
  ui.click(ui.button('By grant'));
  return ui;
}

test('only a grant that has an end it has not reached offers Extend', opts, () => {
  const ui = byGrant();
  const extendIn = (name) => ui.buttons(ui.rowOf(name)).includes('Extend');
  assert.equal(extendIn('Jordan Ray'), false, 'no end: nothing to extend');
  assert.equal(extendIn('Casey Lin'), true);
  assert.equal(extendIn('Morgan Lee'), true);
  assert.equal(extendIn('Riley Park'), false, 'an ended grant is not extended');
  assert.match(ui.rowOf('Riley Park').textContent, /Ended 6d ago/);
  assert.match(ui.rowOf('Casey Lin').textContent, /Ends soon · \d+h left/);
  assert.match(ui.rowOf('Jordan Ray').textContent, /No expiry/);
});

test('Extend counts from the current end, says what changes, and sends expiresAt with the reason', opts, async () => {
  const ui = byGrant();
  ui.click(ui.button('Extend', ui.rowOf('Morgan Lee')));
  const form = ui.doc.querySelector('.qh-extend');
  assert.ok(form, 'the Extend form opened');
  assert.match(form.textContent, /Morgan Lee keeps RO access to prod-main/);
  ui.click(ui.button('+30 days', form));
  ui.type(form.querySelector('.qh-extend-why'), 'Quarter close');
  ui.click(ui.button('Extend this grant', form));
  await ui.settle();
  const [[id, body]] = ui.rec.extended;
  assert.equal(id, 'u:x:103');
  assert.equal(body.reason, 'Quarter close');
  const moved = Date.parse(body.expiresAt) - Date.parse(GRANTS[2].expiresAt);
  assert.ok(Math.abs(moved - 30 * DAY) < 5000, '+30 days from the current end, not from today: ' + moved);
});

test('an earlier end says Shorten, and no end says so before Save', opts, () => {
  const ui = byGrant();
  ui.click(ui.button('Extend', ui.rowOf('Morgan Lee')));
  const form = ui.doc.querySelector('.qh-extend');
  ui.click(ui.button('Pick a date', form));
  assert.ok(ui.buttons(form).includes('Shorten this grant'), 'today is before the current end');
  ui.click(ui.button('No end date', form));
  assert.match(form.textContent, /with no end date, until someone revokes it/);
  assert.ok(ui.buttons(form).includes('Extend this grant'));
});

test('a 409 grant_ended is said in plain words, without the API path', opts, async () => {
  const fail = () => { const e = new Error('This grant ended on 25 Oct 2026 14:00 UTC. Grant the access again with POST /admin/grants.'); e.status = 409; e.code = 'grant_ended'; return Promise.reject(e); };
  const ui = byGrant({ extendGrant: fail });
  ui.click(ui.button('Extend', ui.rowOf('Casey Lin')));
  const form = ui.doc.querySelector('.qh-extend');
  ui.click(ui.button('Extend this grant', form));
  await ui.settle();
  const err = form.querySelector('.qh-roleform-err');
  assert.ok(err, 'the refusal shows beside the form');
  assert.match(err.textContent, /^This grant ended on .+\. QueryHub cannot extend an ended grant\. Use Edit to grant the access again\.$/);
  assert.doesNotMatch(err.textContent, /POST|\/admin\//);
});

const rule = (id, user, extra) => ({ id, user, userName: user, subjectType: 'user', tier: 'RO',
  connectionId: 'prod-main', databaseId: null, expiresAt: null, createdByName: 'Example Admin', ...extra });
const RULES = [
  rule('11', 'Jordan Ray', { enabled: true, runCount: 214 }),
  rule('12', 'Casey Lin', { enabled: false, runCount: 37 }),
  rule('ag:13', 'Morgan Lee', { enabled: true, runCount: 0, expiresAt: at(3 * DAY) }),
  rule('14', 'Riley Park', { enabled: true, runCount: 9, expiresAt: at(-3600000) }),
  rule('15', 'Avery Quinn', {}),
];

for (const view of ['subject', 'flat']) {
  test('rules (' + view + '): paused, run count and ended are said in words', opts, () => {
    const ui = mount('AutoView', { autoGrants: RULES });
    if (view === 'flat') ui.click(ui.button('All rows'));
    const row = (n) => ui.rowOf(n);
    assert.match(row('Jordan Ray').textContent, /Ran 214 times/);
    assert.match(row('Morgan Lee').textContent, /Never ran/);
    assert.doesNotMatch(row('Avery Quinn').textContent, /Ran|Never ran/, 'no runCount, no words');
    assert.ok(row('Casey Lin').querySelector('.qh-pausedchip'), 'a paused rule says Paused');
    assert.equal(row('Casey Lin').querySelector('.qh-pausedchip').textContent, 'Paused');
    assert.equal(row('Jordan Ray').querySelector('.qh-pausedchip'), null);
    assert.ok(ui.buttons(row('Jordan Ray')).includes('Pause'));
    assert.ok(ui.buttons(row('Casey Lin')).includes('Resume'));
    // A team rule (`ag:`) takes the PATCH too: the route reads both id forms.
    assert.ok(ui.buttons(row('Morgan Lee')).includes('Pause'));
    const ended = ui.buttons(row('Riley Park'));
    assert.ok(!ended.includes('Pause') && !ended.includes('Resume'), 'no switch on an ended rule: ' + ended.join('|'));
    assert.match(row('Riley Park').textContent, /Ended \d+[mh] ago/);
  });
}

test('pause says the rule is kept before it runs, then sends enabled false', opts, async () => {
  const ui = mount('AutoView', { autoGrants: RULES });
  ui.click(ui.button('Pause', ui.rowOf('Jordan Ray')));
  await ui.settle();
  assert.equal(ui.asked.length, 1);
  assert.match(ui.asked[0], /QueryHub does not revoke the rule\./);
  assert.match(ui.asked[0], /A DBA then reviews the queries of Jordan Ray on prod-main/);
  assert.deepEqual(ui.rec.toggled, [['11', false]]);
  ui.click(ui.button('Resume', ui.rowOf('Casey Lin')));
  await ui.settle();
  assert.match(ui.asked[1], /run without a DBA again, up to RO, with no end date\./);
  assert.deepEqual(ui.rec.toggled[1], ['12', true]);
});

test('Edit is off on a paused rule, and the reason is on the control', opts, () => {
  const ui = mount('AutoView', { autoGrants: RULES });
  const edit = ui.button('Edit', ui.rowOf('Casey Lin'));
  assert.equal(edit.disabled, true);
  assert.match(edit.parentElement.title, /Resume the rule first/);
  assert.equal(ui.button('Edit', ui.rowOf('Jordan Ray')).disabled, false);
});

const SCOPES = [
  { id: 'U0EXAMPLE001', admin: 'Jordan Ray', role: 'super', canApprove: ['RO', 'RW', 'DDL'], connections: ['*'], expiresAt: null },
  { id: 'U0EXAMPLE002', admin: 'Casey Lin', role: 'dba', canApprove: ['RO'], connections: ['prod-main'], expiresAt: at(2 * DAY) },
];

test('a scope without an end reads Permanent, and one that ends soon says so', opts, () => {
  const ui = mount('ScopesView', { scopes: SCOPES });
  assert.match(ui.rowOf('Jordan Ray').textContent, /Permanent/);
  assert.match(ui.rowOf('Casey Lin').textContent, /Ends soon · \d+h left/);
});

// The admin of a new scope is chosen in PersonPick (2026-10-24), not typed.
const PEOPLE = [{ id: 'U0EXAMPLE009', handle: 'sam.new', name: 'Sam New', initials: 'SN' }];
function pickAdmin(ui, form) {
  const input = form.querySelector('.qh-pcombo-in');
  React.act(() => { input.dispatchEvent(new ui.win.FocusEvent('focusin', { bubbles: true })); });
  React.act(() => { input.focus(); });
  const opt = ui.all('.qh-pcombo-opt', form)[0];
  assert.ok(opt, 'the person list opens');
  React.act(() => { opt.dispatchEvent(new ui.win.MouseEvent('mousedown', { bubbles: true, cancelable: true })); });
}

test('a new DBA scope starts on No end date, and a pick sends expiresAt', opts, () => {
  const ui = mount('ScopesView', { scopes: SCOPES, people: PEOPLE });
  ui.click(ui.button('Add admin'));
  const form = ui.doc.querySelector('.qh-addrow');
  assert.equal(form.querySelector('.qh-endpick [aria-pressed="true"]').textContent, 'No end date');
  pickAdmin(ui, form);
  ui.click(ui.button('+30 days', form));
  assert.match(form.textContent, /After this date the scope stops\. The admin can no longer approve requests through it\./);
  ui.click(ui.button('Add', form));
  const [s] = ui.rec.scopes;
  assert.ok(Math.abs(Date.parse(s.expiresAt) - (Date.now() + 30 * DAY)) < 5000);
});

test('a super-admin is offered no end date and sends null', opts, () => {
  const ui = mount('ScopesView', { scopes: SCOPES, people: PEOPLE });
  ui.click(ui.button('Add admin'));
  const form = ui.doc.querySelector('.qh-addrow');
  pickAdmin(ui, form);
  ui.click(ui.button('super-admin', form));
  assert.equal(form.querySelector('.qh-endpick'), null);
  assert.match(form.textContent, /A super-admin has no end date\./);
  ui.click(ui.button('Add', form));
  assert.ok('expiresAt' in ui.rec.scopes[0]);
  assert.equal(ui.rec.scopes[0].expiresAt, null);
});

test('editing a scope that ends keeps its end unless the admin picks another', opts, () => {
  const ui = mount('ScopesView', { scopes: SCOPES });
  ui.click(ui.button('Edit', ui.rowOf('Casey Lin')));
  const form = ui.doc.querySelector('.qh-editrow .qh-addrow');
  assert.equal(form.querySelector('.qh-endpick [aria-pressed="true"]').textContent, 'Keep the current end');
  ui.click(ui.button('Save', form));
  assert.equal('expiresAt' in ui.rec.scopes[0], false, 'left out, the server keeps the end');
});

// --- the real client sends what the routes read ----------------------------------

function clientCalls() {
  const win = bareWindow();
  const calls = [];
  win.fetch = async (url, o) => { calls.push({ url, method: o.method, body: o.body ? JSON.parse(o.body) : null });
    return { ok: true, status: 200, json: async () => ({}) }; };
  return { api: loadInto(win, 'qh-api.jsx').qhApi, calls };
}

test('Extend is a PATCH that always carries expiresAt, null for no end', async () => {
  const { api, calls } = clientCalls();
  await api.adminExtendGrant('t:7:12', { expiresAt: null });
  await api.adminExtendGrant('u:U0EXAMPLE001:12', { expiresAt: '2026-11-30T20:59:59.000Z', reason: 'Quarter close' });
  assert.deepEqual(calls[0], { url: '/api/admin/grants/t%3A7%3A12', method: 'PATCH', body: { expiresAt: null, reason: null } });
  assert.deepEqual(calls[1].body, { expiresAt: '2026-11-30T20:59:59.000Z', reason: 'Quarter close' });
});

test('pause and resume are a PATCH with enabled only, for both id forms', async () => {
  const { api, calls } = clientCalls();
  await api.adminSetAutoGrant('ag:13', false);
  await api.adminSetAutoGrant('11', 1);
  assert.deepEqual(calls[0], { url: '/api/admin/auto-grants/ag%3A13', method: 'PATCH', body: { enabled: false } });
  assert.deepEqual(calls[1].body, { enabled: true });
});

// --- the mock enforces what the server enforces ----------------------------------

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

test('the mock refuses to extend an ended grant with 409 grant_ended', async () => {
  const api = mockApi();
  const { grants } = await api.adminGrants();
  const ended = grants.find(g => g.expiresAt && Date.parse(g.expiresAt) < Date.now());
  const live = grants.find(g => g.expiresAt && Date.parse(g.expiresAt) > Date.now());
  assert.ok(ended && live, 'the mock seeds an ended grant and a live one');
  await assert.rejects(api.adminExtendGrant(ended.id, { expiresAt: at(30 * DAY) }), e => e.status === 409 && e.code === 'grant_ended');
  const next = at(60 * DAY);
  const r = await api.adminExtendGrant(live.id, { expiresAt: next, reason: null });
  assert.equal(r.expiresAt, next);
  await assert.rejects(api.adminExtendGrant(live.id, { expiresAt: at(-DAY) }), e => e.status === 400);
});

test('the mock seeds every rule state and refuses a switch on an ended rule', async () => {
  const api = mockApi();
  const { autoGrants } = await api.adminAutoGrants();
  const ended = autoGrants.find(a => a.expiresAt && Date.parse(a.expiresAt) < Date.now());
  assert.ok(ended, 'an ended rule');
  assert.ok(autoGrants.some(a => a.enabled === false), 'a paused rule');
  assert.ok(autoGrants.some(a => a.runCount === 0), 'a rule that never ran');
  assert.ok(autoGrants.some(a => a.runCount > 0 && a.enabled !== false), 'a rule that runs');
  await assert.rejects(api.adminSetAutoGrant(ended.id, false), e => e.status === 409 && e.code === 'rule_ended');
  const on = autoGrants.find(a => a.enabled !== false && a !== ended);
  const r = await api.adminSetAutoGrant(on.id, false);
  assert.equal(r.enabled, false);
  assert.equal(r.changed, true);
  assert.equal((await api.adminSetAutoGrant(on.id, false)).changed, false, 'a no-op says so');
});

test('the mock keeps a scope end unless one is sent, and a super-admin never ends', async () => {
  const api = mockApi();
  const { scopes } = await api.adminScopes();
  const ends = scopes.find(s => s.expiresAt);
  assert.ok(ends && scopes.some(s => !s.expiresAt));
  const kept = await api.adminSaveScope({ admin: ends.id, role: 'dba', canApprove: ['RO'], connections: ends.connections });
  assert.equal(kept.expiresAt, ends.expiresAt);
  await assert.rejects(api.adminSaveScope({ admin: ends.id, role: 'super', canApprove: [], connections: ['*'] }), e => e.status === 400);
  const cleared = await api.adminSaveScope({ admin: ends.id, role: 'dba', canApprove: ['RO'], connections: ends.connections, expiresAt: null });
  assert.equal(cleared.expiresAt, null);
});
