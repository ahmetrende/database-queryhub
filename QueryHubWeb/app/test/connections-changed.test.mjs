// A connection enabled in Admin has to reach the sidebar without a page reload.
//
// The app shell reads /connections once, at sign-in, and the admin panel only
// refreshed its own list — so an enabled connection stayed missing from the
// sidebar and the query tab's picker until a full reload. The client now
// announces every change that can alter what /connections answers, and the
// shell re-reads on the announcement. This pins the announcing half: which
// calls announce, that only a success announces, and that a read does not.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';

function client({ ok = true } = {}) {
  const win = bareWindow();
  const events = [];
  win.dispatchEvent = (e) => { events.push(e.type); return true; };
  win.Event = class { constructor(type) { this.type = type; } };
  win.fetch = async () => ({
    ok, status: ok ? 200 : 400,
    json: async () => (ok ? { id: 'alpha' } : { error: { code: 'bad_request', message: 'no' } }),
    text: async () => '{}',
  });
  const { qhApi } = loadInto(win, 'qh-api.jsx');
  return { qhApi, events };
}

for (const [name, call] of [
  ['enable or disable', (api) => api.adminUpdateConnection('alpha', { enabled: true })],
  ['create', (api) => api.adminCreateConnection({ alias: 'alpha' })],
  ['delete', (api) => api.adminDeleteConnection('alpha')],
  ['schema refresh', (api) => api.adminSchemaRefresh('alpha')],
  ['bulk enable', (api) => api.adminBulkConnections({ connections: ['alpha'], enabled: true })],
]) {
  test(`${name} tells the app the connection list changed`, async () => {
    const { qhApi, events } = client();
    await call(qhApi);
    assert.deepEqual(events, ['qh:connections-changed']);
  });
}

test('a refused change announces nothing', async () => {
  const { qhApi, events } = client({ ok: false });
  await assert.rejects(qhApi.adminUpdateConnection('alpha', { enabled: true }));
  assert.deepEqual(events, []);
});

test('reading the list is not a change', async () => {
  const { qhApi, events } = client();
  await qhApi.adminConnections();
  await qhApi.adminTestConnection('alpha');
  assert.deepEqual(events, []);
});

test('a bulk dry run checks and announces nothing', async () => {
  const { qhApi, events } = client();
  await qhApi.adminBulkConnections({ connections: ['alpha'], enabled: true, dryRun: true });
  assert.deepEqual(events, []);
});
