// Three screens read fields that the server sends since 2026-10-22.
//
//   * Masking exemptions: `audience` (everyone, super, dba, team), the team of
//     a team audience, and `expiresAt` / `expired`. The screen printed a `dba`
//     or a `team` row as "everyone", which is the widest reading of a row that
//     is narrower. A widening edit renames its button.
//   * Connections: `health` and `inRotation` on each row. A `readOnly` view
//     for the DBA level hides every write control and keeps Test. A DBA row
//     has no `credentials`, `secretsProvider` or `notes`.
//   * People: `lastQueryAt`. Null means no query. A missing field shows nothing.
//
// The helpers are tested as functions. The screens are mounted for real
// (jsdom), with a fake `st` that records what a save would send.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { bareWindow, loadInto } from './_load.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }
const opts = JSDOM ? {} : { skip: 'jsdom not installed' };

const DAY = 86400000;
const ahead = (d) => new Date(Date.now() + d * DAY).toISOString();
const ago = (min) => new Date(Date.now() - min * 60000).toISOString();

const pure = loadInto(bareWindow(), 'qh-data.jsx', 'qh-admin-access.jsx', 'qh-admin-mask.jsx');
const { mxAudText, mxEndState, mxWidens, mxNorms, MX_AUDIENCES, connHealthState } = pure;

// ---------- helpers ----------

test('the screen offers exactly the four audiences the server takes', () => {
  assert.deepEqual(MX_AUDIENCES.map(a => a[0]).sort(), ['dba', 'everyone', 'super', 'team']);
});

test('a dba or a team audience never reads as everyone', () => {
  const teams = [{ id: '7', name: 'Team Alpha' }];
  assert.equal(mxAudText({ audience: 'dba' }), 'admins only');
  assert.equal(mxAudText({ audience: 'super' }), 'super-admins only');
  assert.equal(mxAudText({ audience: 'everyone' }), 'everyone');
  assert.equal(mxAudText({}), 'everyone');
  assert.equal(mxAudText({ audience: 'team', audienceTeamId: '7', audienceTeamName: 'Ops' }, teams), 'team Ops');
  assert.equal(mxAudText({ audience: 'team', audienceTeamId: '7' }, teams), 'team Team Alpha');
  assert.equal(mxAudText({ audience: 'team', audienceTeamId: '9' }, teams), 'team #9');
  // A value the screen does not know is printed as it came, not as "everyone".
  assert.equal(mxAudText({ audience: 'auditors' }), 'auditors');
  // The `dba` label must not say DBA: it is any admin role, not the DBA level.
  assert.doesNotMatch(MX_AUDIENCES.find(a => a[0] === 'dba').slice(1).join(' '), /DBA/);
});

test('the end state reads the server flag, then the clock', () => {
  const now = Date.parse('2026-10-23T12:00:00Z');
  assert.deepEqual(mxEndState({ expiresAt: null }, now), { kind: 'none', text: 'No end date' });
  assert.equal(mxEndState({ expiresAt: '2026-10-29T12:00:00Z' }, now).text, 'Ends in 6 days');
  assert.equal(mxEndState({ expiresAt: '2026-10-29T12:00:00Z' }, now).kind, 'soon');
  assert.equal(mxEndState({ expiresAt: '2026-10-24T01:00:00Z' }, now).text, 'Ends in 1 day');
  assert.equal(mxEndState({ expiresAt: '2026-12-01T00:00:00Z' }, now).kind, 'ends');
  assert.equal(mxEndState({ expiresAt: '2026-10-22T00:00:00Z' }, now).kind, 'ended');
  // `expired` from the server wins over a clock that disagrees.
  assert.equal(mxEndState({ expiresAt: '2026-10-29T12:00:00Z', expired: true }, now).kind, 'ended');
});

test('an edit widens when more people, more values or more time are unmasked', () => {
  const base = { strength: 'soft', survivesJoin: false, audience: 'super', expiresAt: ahead(10) };
  const w = (patch, from) => mxWidens(from || base, { ...(from || base), ...patch });
  assert.deepEqual(w({}), []);
  assert.deepEqual(w({ strength: 'full' }), ['values are not checked']);
  assert.deepEqual(w({ survivesJoin: true }), ['it stays exempt in joins']);
  assert.deepEqual(w({ audience: 'dba' }), ['more people see the values']);
  assert.deepEqual(w({ audience: 'super' }, { ...base, audience: 'dba' }), [], 'dba to super narrows');
  assert.deepEqual(w({ audience: 'team', audienceTeamId: '1' }, { ...base, audience: 'everyone' }), [], 'everyone to a team narrows');
  assert.deepEqual(w({ audienceTeamId: '2' }, { ...base, audience: 'team', audienceTeamId: '1' }), ['more people see the values'], 'another team widens');
  assert.deepEqual(w({ audience: 'everyone' }, { ...base, audience: 'team', audienceTeamId: '1' }), ['more people see the values']);
  assert.deepEqual(w({ expiresAt: ahead(30) }), ['it ends later']);
  assert.deepEqual(w({ expiresAt: null }), ['it has no end date']);
  assert.deepEqual(w({ expiresAt: ahead(2) }), [], 'an earlier end narrows');
  const ended = { ...base, expiresAt: ago(60 * 24), expired: true };
  assert.deepEqual(mxWidens(ended, { ...ended, expiresAt: ahead(5), expired: false }), ['it applies again']);
});

test('the norms are read from the rows, the audience and the end included', () => {
  const rows = [1, 2, 3, 4].map(i => ({ id: String(i), strength: 'soft', survivesJoin: false, audience: 'team', audienceTeamId: '3', expiresAt: i < 4 ? ahead(9) : null }));
  const n = mxNorms(rows);
  assert.equal(n.audience, 'team:3');
  assert.equal(n.ends, true);
  assert.equal(mxNorms(rows.slice(0, 2)).ends, false, 'under four rows the defaults hold');
});

test('the health chip says healthy, failing or not checked, in words', () => {
  assert.equal(connHealthState(null).text, 'Not checked');
  assert.match(connHealthState(null).title, /skips this engine/);
  const ok = connHealthState({ ok: true, checkedAt: ago(4), failingSince: null, error: null });
  assert.equal(ok.text, 'Healthy');
  assert.match(ok.title, /Last check 4m ago/);
  const bad = connHealthState({ ok: false, checkedAt: ago(3), failingSince: ago(120), error: 'x' });
  assert.equal(bad.text, 'Failing since 2h ago');
  assert.equal(connHealthState({ ok: false, checkedAt: null, failingSince: null, error: null }).text, 'Failing');
});

// ---------- mounted screens ----------

function mount(View, st, props) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { pretendToBeVisual: true, url: 'https://localhost/' });
  for (const k of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node', 'Event',
                   'KeyboardEvent', 'MouseEvent', 'getComputedStyle', 'requestAnimationFrame',
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
  win.qhApi = { adminTestConnection: () => Promise.resolve({ ok: true, latencyMs: 9 }) };
  loadInto(win, 'qh-data.jsx', 'qh-modal.jsx', 'qh-panels.jsx', 'qh-admin-data.jsx',
           'qh-admin-access.jsx', 'qh-admin-person.jsx', 'qh-admin-mask.jsx');
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(win[View], { st, user: { name: 'Example Admin', role: 'super' }, ...(props || {}) })));
  const doc = win.document;
  const all = (sel, from) => [...(from || doc).querySelectorAll(sel)];
  const ui = {
    win, doc, all,
    act: (fn) => React.act(fn),
    buttons: (from) => all('button', from).map(b => b.textContent.trim()),
    button: (text, from) => {
      const b = all('button', from).find(x => x.textContent.trim() === text);
      assert.ok(b, 'no button reads "' + text + '"; have: ' + all('button', from).map(x => x.textContent.trim()).join(' | '));
      return b;
    },
    click: (el) => React.act(() => { el.click(); }),
    settle: () => React.act(async () => { await new Promise(r => setTimeout(r, 0)); }),
    type: (el, text) => React.act(() => {
      const proto = el.tagName === 'TEXTAREA' ? win.HTMLTextAreaElement.prototype : win.HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, text);
      el.dispatchEvent(new win.Event('input', { bubbles: true }));
    }),
    choose: (sel, value) => React.act(() => {
      Object.getOwnPropertyDescriptor(win.HTMLSelectElement.prototype, 'value').set.call(sel, value);
      sel.dispatchEvent(new win.Event('change', { bubbles: true }));
    }),
  };
  return ui;
}

const TEAMS = [{ id: '3', name: 'Team Alpha', members: [] }, { id: '4', name: 'Team Beta', members: [] }];
const mx = (id, extra) => ({ id, scope: 'column', connectionId: 'prod-main', connectionName: 'prod-main', databaseId: 'orders', databaseName: 'orders',
  schema: 'public', table: 'venues', column: 'c' + id, strength: 'soft', survivesJoin: false, audience: 'everyone', audienceTeamId: null,
  audienceTeamName: null, expiresAt: null, expired: false, enabled: true, reason: 'Reason ' + id, createdBy: 'U0EXAMPLE001',
  createdAt: ago(60 * 24 * 3), ...extra });
const MASK = [
  mx('1'), mx('2'), mx('3'), mx('4'),
  mx('5', { audience: 'dba' }),
  mx('6', { audience: 'team', audienceTeamId: '3', audienceTeamName: 'Team Alpha' }),
  mx('7', { expiresAt: ahead(6) }),
  mx('8', { expiresAt: ago(60 * 24 * 2), expired: true }),
];
function maskSt(rec) {
  return {
    maskExemptions: MASK, maskMeta: { maskingEnabled: true, nameRules: 3, valueDetectors: [] }, teams: TEAMS, connections: [],
    updateMaskExemption: (id, patch) => { rec.push(['patch', id, patch]); return Promise.resolve({}); },
    addMaskExemption: (b) => { rec.push(['add', b]); return Promise.resolve({}); },
    maskCatalog: () => Promise.resolve({ schemas: [] }), maskPreview: () => Promise.resolve({}),
  };
}
const item = (ui, col) => ui.all('.qh-mxitem').find(x => (x.querySelector('.qh-mxtitle') || {}).textContent === 'public.venues.' + col);

test('each audience shows its own chip, and an ended row reads quiet', opts, () => {
  const ui = mount('MaskingView', maskSt([]));
  assert.equal(item(ui, 'c1').querySelectorAll('.qh-mxchip').length, 0, 'a row that matches the norm has no chip');
  assert.match(item(ui, 'c5').textContent, /admins only/);
  assert.doesNotMatch(item(ui, 'c5').querySelector('.qh-mxline1').textContent, /everyone/);
  assert.match(item(ui, 'c6').textContent, /team Team Alpha/);
  assert.match(item(ui, 'c7').textContent, /Ends in 6 days/);
  assert.ok(item(ui, 'c8').classList.contains('is-ended'));
  assert.match(item(ui, 'c8').textContent, /ended/);
  // Most rows have no end, so "no end date" is the norm and never a chip.
  assert.doesNotMatch(ui.doc.querySelector('.qh-mxlist').textContent, /no end date/);
  assert.match(ui.doc.querySelector('.qh-mxlegend').textContent, /no end date/);
  // The open row states the audience and the end, whatever the norm.
  ui.click(item(ui, 'c1').querySelector('.qh-mxitem-top'));
  assert.match(item(ui, 'c1').querySelector('.qh-mxlines').textContent, /everyone · no end date/);
  ui.click(ui.button('Ended' + '1'));
  assert.equal(ui.all('.qh-mxitem').length, 1, 'the Ended filter keeps the ended row only');
});

test('a widening edit says so and renames its button. A narrowing one does not', opts, () => {
  const rec = [];
  const ui = mount('MaskingView', maskSt(rec));
  ui.click(item(ui, 'c5').querySelector('.qh-mxitem-top'));
  ui.click(ui.button('Edit', item(ui, 'c5')));
  const radio = (label) => ui.all('.qh-mxradio', item(ui, 'c5')).find(l => l.querySelector('.qh-mxradio-l').textContent.replace('usual', '') === label).querySelector('input');
  ui.click(radio('super-admins only'));
  ui.button('Save changes', item(ui, 'c5'));
  ui.click(radio('everyone'));
  assert.match(item(ui, 'c5').textContent, /This edit widens the exemption: more people see the values\./);
  ui.click(ui.button('Widen this exemption', item(ui, 'c5')));
  assert.deepEqual(rec[0], ['patch', '5', { strength: 'soft', survivesJoin: false, audience: 'everyone', reason: 'Reason 5' }],
    'an untouched end is not sent');
});

test('a reason fix on an ended row does not send an end, and a new end widens', opts, async () => {
  const rec = [];
  const ui = mount('MaskingView', maskSt(rec));
  ui.click(item(ui, 'c8').querySelector('.qh-mxitem-top'));
  ui.click(ui.button('Edit', item(ui, 'c8')));
  ui.type(item(ui, 'c8').querySelector('textarea'), 'Reason 8, checked again');
  ui.click(ui.button('Save changes', item(ui, 'c8')));
  await ui.settle();
  assert.equal('expiresAt' in rec[0][2], false);
  ui.click(ui.button('Edit', item(ui, 'c8')));
  ui.choose(item(ui, 'c8').querySelector('.qh-exppick select'), '30');
  assert.match(item(ui, 'c8').textContent, /it applies again/);
  ui.click(ui.button('Widen this exemption', item(ui, 'c8')));
  const sent = Date.parse(rec[1][2].expiresAt);
  assert.ok(Math.abs(sent - (Date.now() + 30 * DAY)) < 60000, 'the end is 30 days ahead');
});

test('a team audience needs a team and sends its id', opts, () => {
  const rec = [];
  const ui = mount('MaskingView', maskSt(rec));
  ui.click(item(ui, 'c2').querySelector('.qh-mxitem-top'));
  ui.click(ui.button('Edit', item(ui, 'c2')));
  const team = ui.all('.qh-mxradio', item(ui, 'c2')).find(l => /one team/.test(l.textContent)).querySelector('input');
  ui.click(team);
  const save = ui.all('.qh-mxsavewrap', item(ui, 'c2'))[0];
  assert.equal(save.getAttribute('title'), 'Pick a team.');
  const combo = item(ui, 'c2').querySelector('input[aria-label="Team"]');
  ui.click(combo);
  const opt = ui.all('[role="option"]').find(o => o.textContent === 'Team Beta');
  ui.act(() => { opt.dispatchEvent(new ui.win.MouseEvent('mousedown', { bubbles: true, cancelable: true })); });
  ui.click(ui.button('Save changes', item(ui, 'c2')));
  assert.deepEqual(rec[0][2].audience, { kind: 'team', teamId: '4' });
});

test('a new fleet-wide row sends its audience and its end', opts, async () => {
  const rec = [];
  const ui = mount('MaskingView', maskSt(rec));
  ui.click(ui.button('Add an exemption'));
  ui.click(ui.button('…or every server'));
  ui.type(ui.doc.querySelector('.qh-mxtype input'), 'every server');
  ui.type(ui.doc.querySelector('textarea[aria-label="Why"]'), 'Toolkit schema on every server.');
  ui.click(ui.doc.querySelector('.qh-mxsum'));
  ui.choose(ui.doc.querySelector('.qh-mxform .qh-exppick select'), '7');
  assert.match(ui.doc.querySelector('.qh-mxsum-t').textContent, /super-admins only · ends /);
  ui.click(ui.button('Turn masking off across the whole fleet'));
  await ui.settle();
  assert.equal(rec[0][0], 'add');
  assert.equal(rec[0][1].audience, 'super');
  assert.ok(Math.abs(Date.parse(rec[0][1].expiresAt) - (Date.now() + 7 * DAY)) < 60000);
});

// ---------- Connections ----------

// What a DBA who is not a super-admin reads: no credentials, secretsProvider or notes.
const dbaRow = (id, extra) => ({ id, name: id, engine: 'PostgreSQL', engineId: 'postgres', env: 'production', enabled: true,
  host: id + '.db.example.internal', port: 5432, defaultDatabase: 'app', tags: {}, replicaOf: null, superAdminOnly: false,
  databases: [{ id: 'app', name: 'app' }], owners: [], health: null, inRotation: null, ...extra });
const CONNS = [
  dbaRow('prod-main', { health: { ok: true, checkedAt: ago(4), failingSince: null, error: null } }),
  dbaRow('prod-main-r1', { replicaOf: 'prod-main', inRotation: true, health: { ok: true, checkedAt: ago(4), failingSince: null, error: null } }),
  dbaRow('prod-main-r2', { replicaOf: 'prod-main', inRotation: false, health: null }),
  dbaRow('billing', { health: { ok: false, checkedAt: ago(3), failingSince: ago(95), error: 'timeout <b>expired</b>' } }),
  dbaRow('reporting'),
];
const connSt = () => ({ connectionRows: CONNS, connections: CONNS.filter(c => !c.replicaOf), endpointReqs: [], pushToast: () => {} });
const connRow = (ui, name) => ui.all('.qh-conntable tbody tr').find(r => (r.querySelector('.qh-conn-namecell b') || {}).textContent === name);

test('a read-only list hides every write control and keeps Test', opts, () => {
  const ui = mount('ConnectionsView', connSt(), { readOnly: true });
  const text = ui.doc.body.textContent;
  assert.doesNotMatch(text, /undefined|null|NaN/);
  assert.equal(ui.buttons().filter(t => t === 'Add connection').length, 0);
  assert.equal(ui.buttons().filter(t => t === 'Edit').length, 0);
  assert.equal(ui.all('.qh-rowmenu-btn').length, 0, 'the row menu holds only writes');
  assert.equal(ui.all('.qh-conncheck').length, 0, 'no bulk selection');
  assert.equal(ui.buttons().filter(t => t === 'Test').length, CONNS.length);
  // The same rows as a super-admin get the write controls back.
  const su = mount('ConnectionsView', connSt(), { readOnly: false });
  assert.ok(su.buttons().includes('Add connection'));
  assert.equal(su.buttons().filter(t => t === 'Edit').length, CONNS.length);
});

test('each row says its health and each replica its rotation, in words', opts, () => {
  const ui = mount('ConnectionsView', connSt(), { readOnly: true });
  assert.equal(connRow(ui, 'prod-main').querySelector('.qh-health').textContent, 'Healthy');
  assert.equal(connRow(ui, 'reporting').querySelector('.qh-health').textContent, 'Not checked');
  const bad = connRow(ui, 'billing');
  assert.equal(bad.querySelector('.qh-health').textContent, 'Failing since 1h ago');
  // The error is text: the markup in it is not parsed.
  assert.equal(bad.querySelector('.qh-health-errtext').textContent, 'timeout <b>expired</b>');
  assert.equal(bad.querySelector('.qh-health-errtext b'), null);
  assert.match(connRow(ui, 'prod-main-r1').textContent, /in rotation/);
  assert.doesNotMatch(connRow(ui, 'prod-main-r1').textContent, /out of rotation/);
  assert.match(connRow(ui, 'prod-main-r2').textContent, /out of rotation/);
  const rot = ui.all('.qh-expiry', connRow(ui, 'prod-main-r2')).find(x => x.textContent === 'out of rotation');
  assert.match(rot.getAttribute('title'), /Routing is off/);
  ui.click(ui.button('Failing1'));
  assert.deepEqual(ui.all('.qh-conntable tbody tr').map(r => r.querySelector('.qh-conn-namecell b').textContent), ['billing']);
});

// ---------- People ----------

test('a person card says when they last sent a query', opts, () => {
  const people = [
    { id: 'U0EXAMPLE001', handle: 'U0EXAMPLE001', name: 'Jordan Ray', initials: 'JR', enabled: true, kind: 'requester', lastQueryAt: ago(5) },
    { id: 'U0EXAMPLE002', handle: 'U0EXAMPLE002', name: 'Sam Lee', initials: 'SL', enabled: true, kind: 'requester', lastQueryAt: null },
    { id: 'U0EXAMPLE003', handle: 'U0EXAMPLE003', name: 'Alex Kim', initials: 'AK', enabled: true, kind: 'requester' },
  ];
  const ui = mount('PersonAccessView', { people, grants: [], connections: [], teams: [] });
  const card = (name) => ui.all('.qh-percard').find(c => c.querySelector('.qh-percard-name').textContent.startsWith(name));
  assert.equal(card('Jordan Ray').querySelector('.qh-percard-last').textContent, 'Last query 5m ago');
  assert.equal(card('Sam Lee').querySelector('.qh-percard-last').textContent, 'No queries yet');
  assert.equal(card('Alex Kim').querySelector('.qh-percard-last'), null, 'an absent field shows nothing');
});
