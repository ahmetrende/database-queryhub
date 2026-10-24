// "Access ends" on an access-request approval (operator decision, 2026-10-24).
//
//   * The Provision card has an "Access ends" picker. It opens on the server's
//     default days (`accessDefaultDays` of GET /admin/endpoint-requests).
//   * "No expiry" stays a choice: the admin can still give a grant with no end.
//   * An untouched picker sends nothing, so the server applies the same default.
//   * The mock and the API client carry the choice the way the server reads it.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { transformSync } from 'esbuild';

import { bareWindow, loadInto } from './_load.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }
const opts = JSDOM ? {} : { skip: 'jsdom not installed' };

const HERE = dirname(fileURLToPath(import.meta.url));
const DAY = 86400000;

const REQS = [{ id: 'er_31', server: 'prod-reporting-01', database: 'ledger', tier: 'RO',
  reason: 'Weekly revenue report.', requester: 'ben.donnelly', requestedAt: '2026-10-23T08:00:00Z',
  status: 'submitted' }];

function mount(st, props) {
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
  win.qhApi = {};
  loadInto(win, 'qh-data.jsx', 'qh-modal.jsx', 'qh-panels.jsx', 'qh-admin-data.jsx',
           'qh-admin-access.jsx', 'qh-admin-person.jsx', 'qh-admin-mask.jsx');
  const calls = [];
  const full = { connectionRows: [], connections: [], endpointReqs: REQS, pushToast: () => {},
    decideEndpoint: (...a) => calls.push(a), ...st };
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(win.ConnectionsView,
    { st: full, user: { name: 'Example Admin', role: 'super' }, ...(props || {}) })));
  const doc = win.document;
  const card = () => doc.querySelector('.qh-ercard');
  return {
    win, doc, calls,
    select: () => card().querySelector('.qh-ercard-end select'),
    provision: () => [...card().querySelectorAll('button')].find(b => b.textContent.trim() === 'Provision'),
    click: (el) => React.act(() => { el.click(); }),
    choose: (sel, value) => React.act(() => {
      Object.getOwnPropertyDescriptor(win.HTMLSelectElement.prototype, 'value').set.call(sel, value);
      sel.dispatchEvent(new win.Event('change', { bubbles: true }));
    }),
  };
}

const optionLabels = (sel) => [...sel.options].map(o => o.textContent.trim());

test('the picker opens on the default days, with No expiry still a choice', opts, () => {
  const ui = mount({ accessDefaultDays: 90 });
  const sel = ui.select();
  assert.ok(sel, 'the card has an Access ends picker');
  assert.equal(sel.value, '90');
  assert.deepEqual(optionLabels(sel), ['No expiry', '7 days', '30 days', '90 days', 'Until a date…']);
  assert.match(ui.doc.querySelector('.qh-ercard-end').textContent, /^Access ends/);
  assert.ok(!sel.hasAttribute('required'), 'not required for a grant');
});

test('a default that is not a quick pick is added and selected', opts, () => {
  const ui = mount({ accessDefaultDays: 45 });
  const sel = ui.select();
  assert.equal(sel.value, '45');
  assert.deepEqual(optionLabels(sel), ['No expiry', '7 days', '30 days', '45 days', '90 days', 'Until a date…']);
});

test('a missing or bad default counts as 90', opts, () => {
  for (const bad of [undefined, 0, -3, 'x']) {
    assert.equal(mount({ accessDefaultDays: bad }).select().value, '90', String(bad));
  }
});

test('an untouched picker sends no end, so the server applies its default', opts, () => {
  const ui = mount({ accessDefaultDays: 90 });
  ui.click(ui.provision());
  assert.equal(ui.calls.length, 1);
  const [id, ok, , end] = ui.calls[0];
  assert.equal(id, 'er_31');
  assert.equal(ok, true);
  assert.equal(end, null);
});

test('No expiry sends noEnd', opts, () => {
  const ui = mount({ accessDefaultDays: 90 });
  ui.choose(ui.select(), 'none');
  ui.click(ui.provision());
  assert.deepEqual(ui.calls[0][3], { noEnd: true });
});

test('a quick pick sends that end as an ISO time', opts, () => {
  const ui = mount({ accessDefaultDays: 90 });
  ui.choose(ui.select(), '7');
  const before = Date.now();
  ui.click(ui.provision());
  const end = ui.calls[0][3];
  assert.deepEqual(Object.keys(end), ['expiresAt']);
  const at = new Date(end.expiresAt).getTime();
  assert.ok(Math.abs(at - (before + 7 * DAY)) < 60000, end.expiresAt);
});

test('"Until a date" opens on today, and a past date blocks Provision and says why', opts, () => {
  const ui = mount({ accessDefaultDays: 90 });
  ui.choose(ui.select(), 'date');
  const input = ui.doc.querySelector('.qh-ercard-end input[type=date]');
  assert.ok(input, 'a date field shows');
  assert.equal(ui.provision().disabled, false, 'today is a valid end');
  ui.win.React.act(() => {
    Object.getOwnPropertyDescriptor(ui.win.HTMLInputElement.prototype, 'value').set.call(input, '2020-01-01');
    input.dispatchEvent(new ui.win.Event('input', { bubbles: true }));
  });
  const b = ui.provision();
  assert.equal(b.disabled, true);
  assert.equal(b.title, 'Pick an end date from today on.');
});

test('a read-only list has no picker and no Provision', opts, () => {
  const ui = mount({ accessDefaultDays: 90 }, { readOnly: true });
  assert.equal(ui.doc.querySelector('.qh-ercard-end'), null);
  assert.equal(ui.provision(), undefined);
});

// ---------- the API client ----------

function clientBodies() {
  const win = bareWindow();
  const sent = [];
  win.fetch = async (url, init) => {
    sent.push({ url, body: JSON.parse(init.body) });
    return { ok: true, status: 200, json: async () => ({}), text: async () => '{}' };
  };
  const api = loadInto(win, 'qh-api.jsx').qhApi;
  return { api, sent };
}

test('the client sends noEnd or expiresAt, and nothing else, with an approval', async () => {
  const { api, sent } = clientBodies();
  await api.adminDecideEndpoint('er_5', true, null, null);
  await api.adminDecideEndpoint('er_5', true, null, { noEnd: true });
  await api.adminDecideEndpoint('er_5', true, null, { expiresAt: '2027-01-01T00:00:00Z', stray: 1 });
  await api.adminDecideEndpoint('er_5', false, 'No.');
  assert.deepEqual(sent.map(s => s.body), [
    { approve: true, note: null },
    { approve: true, note: null, noEnd: true },
    { approve: true, note: null, expiresAt: '2027-01-01T00:00:00Z' },
    { approve: false, note: 'No.' },
  ]);
  assert.match(sent[0].url, /\/admin\/endpoint-requests\/5\/decision$/);
});

// ---------- the mock ----------

async function mockApi() {
  const win = bareWindow();
  win.setTimeout = (fn) => { fn(); return 0; };
  win.clearTimeout = () => {};
  win.setInterval = () => 0;
  win.clearInterval = () => {};
  loadInto(win, 'qh-data.jsx');
  const src = readFileSync(resolve(HERE, '..', '..', 'qh-api-mock.jsx'), 'utf8');
  const code = transformSync(src, {
    loader: 'jsx', jsx: 'transform',
    jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment',
  }).code;
  new Function('window', 'with (window) {\n' + code + '\n}')(win);
  return win.qhApi;
}

const near = (iso, days) => Math.abs(new Date(iso).getTime() - (Date.now() + days * DAY)) < 60000;

// Each test loads a fresh mock, so every one starts with the same pending requests.
async function firstPending() {
  const api = await mockApi();
  const list = await api.adminEndpointReqs();
  const req = list.requests.find(r => r.status === 'submitted');
  assert.ok(req, 'the mock has a pending request');
  return { api, req, list };
}

test('the mock lists the default days', async () => {
  const { list } = await firstPending();
  assert.equal(list.accessDefaultDays, 90);
});

test('the mock: no end sent gives a grant that ends in 90 days', async () => {
  const { api, req } = await firstPending();
  const out = await api.adminDecideEndpoint(req.id, true, null, null);
  assert.ok(near(out.expiresAt, 90), String(out.expiresAt));
});

test('the mock: noEnd gives a grant with no end', async () => {
  const { api, req } = await firstPending();
  const out = await api.adminDecideEndpoint(req.id, true, null, { noEnd: true });
  assert.equal(out.expiresAt, null);
});

test('the mock: an explicit end wins', async () => {
  const { api, req } = await firstPending();
  const chosen = new Date(Date.now() + 10 * DAY).toISOString();
  const out = await api.adminDecideEndpoint(req.id, true, null, { expiresAt: chosen });
  assert.equal(out.expiresAt, chosen);
});

test('the mock refuses noEnd with an end, and a past end, in the server words', async () => {
  const { api, req } = await firstPending();
  await assert.rejects(api.adminDecideEndpoint(req.id, true, null,
    { noEnd: true, expiresAt: new Date(Date.now() + DAY).toISOString() }),
    (e) => e.status === 400 && e.code === 'bad_request' && e.message === 'Send an end date or noEnd, not both.');
  await assert.rejects(api.adminDecideEndpoint(req.id, true, null,
    { expiresAt: new Date(Date.now() - DAY).toISOString() }),
    (e) => e.status === 400 && e.code === 'end_in_past');
  const still = (await api.adminEndpointReqs()).requests.find(r => r.id === req.id);
  assert.equal(still.status, 'submitted', 'a refusal leaves the request pending');
});

test('the mock: a deny writes no grant and answers no end', async () => {
  const { api, req } = await firstPending();
  const out = await api.adminDecideEndpoint(req.id, false, 'No.');
  assert.deepEqual(out, { ok: true });
});
