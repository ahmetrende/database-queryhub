// AI assistant and MCP clients per connection, and the host-change confirm
// (2026-11-20, W4 P1.1 + P1.3).
//
// A super-admin can close one connection to the AI assistant and to MCP
// clients. The query tab hides the AI button for that connection. A host or
// port change with stored logins gets a 409 `confirmation_required`, and the
// form asks before it sends the patch again with `confirmCredentials: true`.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { transformSync } from 'esbuild';

import { bareWindow, loadInto } from './_load.mjs';
import { web } from './_mount.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/^\s*\/\/.*$/gm, ' ');

// ---------- the API client (qh-api.jsx) ----------

function client(reply) {
  const win = bareWindow();
  const calls = [];
  win.dispatchEvent = () => true;
  win.Event = class { constructor(type) { this.type = type; } };
  win.fetch = async (url, opts) => {
    calls.push({ url: String(url), method: (opts && opts.method) || 'GET', body: opts && opts.body ? JSON.parse(opts.body) : null });
    const r = reply || { ok: true, status: 200, body: { changed: true } };
    return { ok: r.ok, status: r.status, json: async () => r.body, text: async () => JSON.stringify(r.body), clone() { return this; } };
  };
  const { qhApi } = loadInto(win, 'qh-api.jsx');
  return { qhApi, calls };
}

test('the client has a PUT for each switch, with body { allowed }', async () => {
  const { qhApi, calls } = client();
  assert.equal(typeof qhApi.adminSetAiAllowed, 'function');
  assert.equal(typeof qhApi.adminSetMcpAllowed, 'function');
  await qhApi.adminSetAiAllowed('prod main', false);
  await qhApi.adminSetMcpAllowed('prod main', 1);
  assert.equal(calls[0].method, 'PUT');
  assert.match(calls[0].url, /\/admin\/connections\/prod%20main\/ai-allowed$/);
  assert.deepEqual(calls[0].body, { allowed: false });
  assert.match(calls[1].url, /\/admin\/connections\/prod%20main\/mcp-allowed$/);
  assert.deepEqual(calls[1].body, { allowed: true });
});

test('the PATCH sends confirmCredentials as given', async () => {
  const { qhApi, calls } = client();
  await qhApi.adminUpdateConnection('alpha', { host: 'h2', confirmCredentials: true });
  assert.equal(calls[0].method, 'PATCH');
  assert.equal(calls[0].body.confirmCredentials, true);
});

test('a 409 confirmation_required keeps its code and reasons on the error', async () => {
  const { qhApi } = client({ ok: false, status: 409,
    body: { error: { code: 'confirmation_required', message: 'Confirm.', reasons: ['The address changes.', 'The logins stay.'] } } });
  await assert.rejects(qhApi.adminUpdateConnection('alpha', { host: 'h2' }), (e) => {
    assert.equal(e.status, 409);
    assert.equal(e.code, 'confirmation_required');
    assert.deepEqual(e.reasons, ['The address changes.', 'The logins stay.']);
    return true;
  });
});

// ---------- the admin data hook (qh-admin-data.jsx) ----------

test('updateConnection returns the question on 409 and keeps fail() for the rest', () => {
  const src = strip(web('qh-admin-data.jsx'));
  const i = src.indexOf('const updateConnection = (conn, patch) =>');
  assert.ok(i > 0, 'updateConnection is gone');
  const body = src.slice(i, src.indexOf('const removeConnection', i));
  assert.match(body, /e\.status === 409 && e\.code === 'confirmation_required'/);
  assert.match(body, /return \{ confirm: /, 'the 409 resolves to { confirm: reasons }');
  assert.match(body, /fail\(e, 'Update connection failed\.'\); return false;/, 'every other error still fails as before');
  assert.ok(body.indexOf('confirm:') < body.indexOf("fail(e, 'Update connection failed.')"), 'the 409 is checked before fail()');
});

test('the hook gives the form both switch helpers', () => {
  const src = strip(web('qh-admin-data.jsx'));
  assert.match(src, /const setAiAllowed = \(conn, allowed\) => qhApi\.adminSetAiAllowed\(conn, allowed\)/);
  assert.match(src, /const setMcpAllowed = \(conn, allowed\) => qhApi\.adminSetMcpAllowed\(conn, allowed\)/);
  assert.match(src, /setSuperAdminOnly, setAiAllowed, setMcpAllowed,/);
});

// ---------- the connection form (qh-admin-access.jsx) ----------

function formSrc() {
  const src = web('qh-admin-access.jsx');
  const i = src.indexOf('function ConnectionForm(');
  assert.ok(i > 0, 'ConnectionForm is gone');
  return src.slice(i, src.indexOf('\nfunction ', i + 10));
}

test('the form draws the AI and MCP switches beside super-admin-only', () => {
  const f = formSrc();
  assert.match(f, /\['ai', 'AI assistant', aiOn, 'Off: the assistant cannot read this connection\\'s tables\.'\]/);
  assert.match(f, /\['mcp', 'MCP clients', mcpOn, 'Off: MCP clients cannot see or query this connection\.'\]/);
  assert.match(f, /role="switch" aria-checked=\{on\} aria-label=\{label\}/);
  assert.match(f, /st\.setAiAllowed : st\.setMcpAllowed/);
  assert.match(f, /useAcc\(!\(init && init\.aiAllowed === false\)\)/, 'a missing field means allowed');
  assert.match(f, /useAcc\(!\(init && init\.mcpAllowed === false\)\)/);
});

test('the form asks on confirmation_required and sends confirmCredentials: true', () => {
  const f = formSrc();
  assert.match(f, /if \(ok && ok\.confirm\) \{ setCredAsk\(ok\.confirm\); return; \}/);
  assert.match(f, /confirmCredentials === true \? \{ \.\.\.base, confirmCredentials: true \} : base/);
  assert.match(f, /<InlineConfirm[\s\S]*confirmLabel="Keep the login and save" cancelLabel="Cancel"/);
  assert.match(f, /onConfirm=\{\(\) => save\(true\)\} onCancel=\{\(\) => setCredAsk\(null\)\}/);
  assert.match(f, /onClick=\{\(\) => save\(false\)\}/, 'the Save click event never counts as a confirm');
});

test('the connection list tags a closed connection', () => {
  const src = web('qh-admin-access.jsx');
  assert.match(src, /c\.aiAllowed === false && <span className="qh-aimcp-off"[^>]*>AI off<\/span>/);
  assert.match(src, /c\.mcpAllowed === false && <span className="qh-aimcp-off"[^>]*>MCP off<\/span>/);
  assert.match(web('QueryHub.html'), /\/\* 2026-11-20 · AI \/ MCP per connection \*\/\s*\n\s*\.qh-aimcp-off \{/);
});

// ---------- the query tab (qh-app.jsx) ----------

test('the query tab hides AI for a connection with aiAllowed false', () => {
  const app = web('qh-app.jsx');
  assert.match(app, /const aiConnAllowed = !\(conn && conn\.aiAllowed === false\);/);
  assert.match(app, /const canFixAi = !!\(aiStatus\.enabled && aiConnAllowed && /);
  assert.match(app, /onAi=\{aiStatus\.enabled && aiConnAllowed && !tab\.kind \?/);
  assert.match(app, /\{aiStatus\.enabled && aiConnAllowed && aiOpen && !tab\.kind && \(/);
});

// ---------- the mock (qh-api-mock.jsx) ----------

function mock() {
  const win = bareWindow();
  // mockDelay resolves through setTimeout: run it at once. No interval runs.
  win.setTimeout = (fn) => { if (typeof fn === 'function') fn(); return 0; };
  win.clearTimeout = () => {};
  win.setInterval = () => 0;
  win.clearInterval = () => {};
  win.dispatchEvent = () => true;
  win.Event = class { constructor(type) { this.type = type; } };
  loadInto(win, 'qh-data.jsx');
  const src = readFileSync(resolve(HERE, '..', '..', 'qh-api-mock.jsx'), 'utf8');
  const code = transformSync(src, {
    loader: 'jsx', jsx: 'transform',
    jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment',
  }).code;
  new Function('window', 'with (window) {\n' + code + '\n}')(win);
  return win.qhApi;
}

test('the mock has both PUT routes', () => {
  const api = mock();
  assert.equal(typeof api.adminSetAiAllowed, 'function');
  assert.equal(typeof api.adminSetMcpAllowed, 'function');
});

test('the mock seeds one connection with AI off and one with MCP off', async () => {
  const api = mock();
  const r = await api.adminConnections();
  const rows = (r && r.connections) || r || [];
  assert.ok(rows.some(c => c.aiAllowed === false), 'no connection has aiAllowed false');
  assert.ok(rows.some(c => c.mcpAllowed === false), 'no connection has mcpAllowed false');
  assert.ok(rows.filter(c => !c.replicaOf).every(c => typeof c.aiAllowed === 'boolean' && typeof c.mcpAllowed === 'boolean'));
});

test('the mock user list carries aiAllowed', async () => {
  const api = mock();
  const r = await api.connections();
  assert.ok(r.connections.length > 0);
  assert.ok(r.connections.every(c => typeof c.aiAllowed === 'boolean'));
});

test('the mock PATCH asks before a host change keeps the stored logins', async () => {
  const api = mock();
  const r = await api.adminConnections();
  const rows = (r && r.connections) || r || [];
  // A DBA view carries no `credentials`. prod-main has a stored RO login in the seed.
  const row = rows.find(c => c.id === 'prod-main');
  assert.ok(row, 'the seed has no prod-main');
  await assert.rejects(api.adminUpdateConnection(row.id, { host: 'moved.db.internal', credentials: {} }), (e) => {
    assert.equal(e.status, 409);
    assert.equal(e.code, 'confirmation_required');
    assert.ok(Array.isArray(e.reasons) && e.reasons.length > 0);
    return true;
  });
  // The same host is no change: no question.
  await api.adminUpdateConnection(row.id, { host: row.host, port: row.port, credentials: {} });
  const ok = await api.adminUpdateConnection(row.id, { host: 'moved.db.internal', credentials: {}, confirmCredentials: true });
  assert.equal(ok.host, 'moved.db.internal');
});
