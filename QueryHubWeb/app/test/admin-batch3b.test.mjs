// Admin batch 3b of 2026-10-24.
//
//   * Connections: a red banner when a connection fails, "Test again", and the
//     provider as its own column.
//   * Revoke grant: an inline confirm that says what changes.
//   * Admin scope form: PersonPick and connection chips. The payload is the
//     same as before.
//   * Masking: the "Ends soon" filter.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import { bareWindow, loadInto } from './_load.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }
const opts = JSDOM ? {} : { skip: 'jsdom not installed' };

const HERE = dirname(fileURLToPath(import.meta.url));
const web = (file) => readFileSync(resolve(HERE, '..', '..', file), 'utf8');

const pure = loadInto(bareWindow(), 'qh-data.jsx', 'qh-admin-data.jsx', 'qh-admin-access.jsx');

function mount(View, props) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>',
                        { pretendToBeVisual: true, url: 'https://localhost/#admin/conns' });
  for (const k of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node', 'Event',
                   'KeyboardEvent', 'MouseEvent', 'getComputedStyle', 'requestAnimationFrame',
                   'cancelAnimationFrame', 'localStorage', 'location', 'history']) {
    Object.defineProperty(globalThis, k, { value: dom.window[k], configurable: true, writable: true });
  }
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const React = require_('react');
  const ReactDOM = require_('react-dom/client');
  globalThis.React = React;
  const win = dom.window;
  win.React = React;
  win.ReactDOM = require_('react-dom');
  loadInto(win, 'qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx', 'qh-admin-data.jsx', 'qh-admin-effective.jsx', 'qh-admin-access.jsx');
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(win[View], props(win))));
  const doc = win.document;
  const all = (sel) => [...doc.querySelectorAll(sel)];
  const flush = () => React.act(() => new Promise(r => setTimeout(r, 0)));
  const click = (el) => React.act(() => el.dispatchEvent(new win.MouseEvent('click', { bubbles: true })));
  return { win, doc, all, flush, click, act: (fn) => React.act(fn) };
}

const conn = (id, name, health) => ({ id, name, engine: 'PostgreSQL', env: 'production', enabled: true, host: 'h', port: 5432,
  defaultDatabase: 'd', databases: [], owners: [], tags: { provider: 'aws' }, health });
const CONNS = [
  conn('c1', 'orders-main', { ok: false, failingSince: '2026-10-23T08:00:00Z', error: 'timeout' }),
  conn('c2', 'billing-main', { ok: true }),
  conn('c3', 'ledger-main', { ok: false, failingSince: '2026-10-23T09:00:00Z', error: 'refused' }),
];
const connSt = (extra) => ({ endpointReqs: [], connectionRows: CONNS, connections: CONNS, pushToast: () => {}, ...extra });

// ---------- 15: connections ----------

test('the banner sentence counts, names up to three and says since when', () => {
  const say = pure.connBannerSay;
  assert.equal(say([]), null);
  assert.match(say([CONNS[0]]), /^1 connection is failing\. Failing since .+\. orders-main\.$/);
  const four = ['a', 'b', 'c', 'd'].map(n => conn(n, n + '-db', { ok: false, failingSince: '2026-10-23T08:00:00Z' }));
  assert.match(say(four), /^4 connections are failing\./);
  assert.match(say(four), /a-db, b-db, c-db and 1 more\.$/);
});

test('connections: the banner shows for failing rows and Test again tests only those', opts, async () => {
  const asked = [];
  const ui = mount('ConnectionsView', (win) => {
    win.qhApi = { adminTestConnection: (id) => { asked.push(id); return Promise.resolve({ ok: true, latencyMs: 5 }); } };
    globalThis.qhApi = win.qhApi;
    return { st: connSt(), user: { name: 'Jordan Ray', role: 'super' }, readOnly: false };
  });
  const banner = ui.doc.querySelector('.qh-connbanner');
  assert.ok(banner, 'a banner');
  assert.match(banner.textContent, /2 connections are failing\./);
  const btn = ui.all('.qh-connbanner button')[0];
  assert.equal(btn.textContent, 'Test again');
  ui.click(btn);
  await ui.flush();
  assert.deepEqual(asked.sort(), ['c1', 'c3']);
});

test('connections: no banner when every connection is healthy', opts, () => {
  const ok = CONNS.map(c => ({ ...c, health: { ok: true } }));
  const ui = mount('ConnectionsView', () => ({ st: connSt({ connectionRows: ok, connections: ok }), user: { name: 'Jordan Ray', role: 'dba' }, readOnly: true }));
  assert.ok(!ui.doc.querySelector('.qh-connbanner'));
});

test('connections: the provider is its own column with a logo', opts, () => {
  const ui = mount('ConnectionsView', () => ({ st: connSt(), user: { name: 'Jordan Ray', role: 'super' }, readOnly: false }));
  const heads = ui.all('.qh-conntable thead th').map(th => th.textContent.replace(/[↑↓]/g, '').trim());
  assert.ok(heads.includes('Engine') && heads.includes('Provider'), heads.join('|'));
  const first = ui.all('.qh-conntable tbody tr')[0];
  const prov = first.querySelector('td.qh-conn-provtd');
  assert.ok(prov && prov.querySelector('img.qh-prov-logo'), 'logo in the provider cell');
  assert.ok(!first.querySelector('td.qh-conn-engtd img.qh-prov-logo'), 'not under the engine text');
});

// ---------- 17: revoke confirm ----------

test('revoke: a confirm row says what changes and the DELETE waits for it', () => {
  const src = web('qh-admin-access.jsx');
  assert.match(src, /revokeId === g\.id && <tr className="qh-editrow qh-revokerow">/);
  assert.match(src, /loses \{g\.tier\} on \{qhGrantTarget\(g\)\}\. A query that is running finishes\./);
  // The first Revoke click opens the row. It does not call revokeGrant.
  assert.doesNotMatch(src, /className="qh-revoke" onClick=\{\(\) => st\.revokeGrant\(g\.id, actor\)\}/);
  assert.match(src, /setRevokeId\(null\); st\.revokeGrant\(g\.id, actor\)/);
});

// ---------- 18: scope form ----------

test('scope connection list: names only, no star, no blanks', () => {
  assert.deepEqual(pure.scopeConnList('*'), []);
  assert.deepEqual(pure.scopeConnList(''), []);
  assert.deepEqual(pure.scopeConnList('a, b ,,c'), ['a', 'b', 'c']);
});

test('scope form: pick a person and connections, the payload stays {admin, role, canApprove, connections}', opts, async () => {
  const saved = [];
  const people = [{ id: 'U0EXAMPLE001', handle: 'jordan.ray', name: 'Jordan Ray', initials: 'JR' }];
  const st = connSt({ people, saveScope: (...a) => saved.push(a) });
  const ui = mount('ScopeForm', () => ({ init: { admin: '', role: 'dba', canApprove: ['RO'], connections: '*' }, actor: 'dba.jordan', st, onDone: () => {} }));
  const save = () => ui.all('button').find(b => b.textContent === 'Add');
  assert.equal(save().disabled, true, 'empty admin blocks the save');
  assert.equal(save().parentElement.title, 'Pick the admin first.');
  assert.ok(ui.doc.querySelector('.qh-pcombo'), 'PersonPick, not a text box');
  assert.match(ui.doc.querySelector('.qh-scopeform-all').textContent, /Every connection/);
  // Pick the person.
  const input = ui.doc.querySelector('.qh-pcombo-in');
  ui.act(() => input.dispatchEvent(new ui.win.FocusEvent('focusin', { bubbles: true })));
  ui.act(() => input.focus());
  const opt = ui.all('.qh-pcombo-opt')[0];
  assert.ok(opt, 'the list opens');
  ui.act(() => opt.dispatchEvent(new ui.win.MouseEvent('mousedown', { bubbles: true, cancelable: true })));
  assert.equal(save().disabled, false);
  // Pick a connection through the connection picker.
  const cin = ui.doc.querySelector('#scope-conn');
  ui.act(() => cin.focus());
  const copt = ui.all('.qh-connpick-opt').find(b => /billing-main/.test(b.textContent));
  assert.ok(copt);
  ui.act(() => copt.dispatchEvent(new ui.win.MouseEvent('mousedown', { bubbles: true, cancelable: true })));
  assert.equal(ui.all('.qh-scopeform-chip')[0].textContent.replace('×', ''), 'billing-main');
  ui.click(save());
  assert.equal(saved.length, 1);
  const body = saved[0][0];
  assert.equal(body.admin, 'jordan.ray');
  assert.equal(body.role, 'dba');
  assert.deepEqual(body.canApprove, ['RO']);
  assert.deepEqual(body.connections, ['billing-main']);
});

// ---------- 19: masking ----------

test('masking: the Ends soon filter keeps rows that end within 7 days, and counts them', () => {
  const src = web('qh-admin-mask.jsx');
  assert.match(src, /soon: \(e\) => mxEndState\(e\)\.kind === 'soon'/);
  assert.match(src, /\['soon', 'Ends soon'\]/);
  const { mxEndState } = loadInto(bareWindow(), 'qh-data.jsx', 'qh-admin-data.jsx', 'qh-admin-mask.jsx');
  const now = Date.parse('2026-10-24T00:00:00Z');
  assert.equal(mxEndState({ expiresAt: '2026-10-28T00:00:00Z' }, now).kind, 'soon');
  assert.equal(mxEndState({ expiresAt: '2026-12-28T00:00:00Z' }, now).kind, 'ends');
});

test('masking: the chips are not upper-cased by CSS', () => {
  const html = web('QueryHub.html');
  const rule = html.match(/\n\s*\.qh-mxchip \{[^}]*\}/)[0];
  assert.doesNotMatch(rule, /text-transform: uppercase/);
});

test('kill switch: the Pause button is a solid red fill, not an outline', () => {
  const html = web('QueryHub.html');
  const rule = html.match(/\.qh-btn\.qh-kill-pause \{[^}]*\}/)[0];
  assert.match(rule, /background: var\(--danger-solid-medium\)/);
  assert.match(rule, /color: #fff/);
  assert.match(html, /\[data-theme='dark'\] \.qh-btn\.qh-kill-pause \{ color: #1F2229; \}/);
});
