// A person the own UI refuses must reach the portal notice page.
//
// The server answers every API call of a refused person with 403
// `ui_super_admin_only`, and a refresh with the same 403. The client sends
// that person to /portal-redirect. A person who skips the page meets the same
// refusal on the next request, because every call and every poller goes
// through qhFetch. Any other 403 must not move the page.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';

const REFUSED = { error: { code: 'ui_super_admin_only', message: 'Refused: QueryHub opens through the portal. Sign in there.' } };

function reply(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body, text: async () => JSON.stringify(body), clone() { return reply(status, body); } };
}

/** Load qh-api.jsx with a fetch that answers by path, and a location that records. */
function load(answers) {
  const win = bareWindow();
  const moved = [];
  win.location = { replace: (u) => moved.push(u), href: '', search: '' };
  win.dispatchEvent = () => true;
  win.fetch = async (url) => {
    const a = answers[url.replace('/api', '')];
    return typeof a === 'function' ? a() : a;
  };
  return { api: loadInto(win, 'qh-api.jsx').qhApi, moved };
}

test('a 403 ui_super_admin_only sends the page to the notice', async () => {
  const { api, moved } = load({ '/connections': reply(403, REFUSED) });
  await assert.rejects(() => api.connections(), (e) => e.code === 'ui_super_admin_only');
  assert.deepEqual(moved, ['/portal-redirect']);
});

test('another 403 does not move the page', async () => {
  const { api, moved } = load({ '/connections': reply(403, { error: { code: 'forbidden', message: 'No.' } }) });
  await assert.rejects(() => api.connections(), (e) => e.code === 'forbidden');
  assert.deepEqual(moved, []);
});

test('a refresh that answers 403 ui_super_admin_only sends the page to the notice', async () => {
  const { api, moved } = load({
    '/connections': reply(401, { error: { code: 'unauthenticated', message: 'expired' } }),
    '/auth/refresh': reply(403, REFUSED),
  });
  await assert.rejects(() => api.connections(), (e) => e.code === 'unauthenticated');
  assert.deepEqual(moved, ['/portal-redirect']);
});

test('a refresh that answers 401 keeps the sign-in screen and does not move the page', async () => {
  const { api, moved } = load({
    '/connections': reply(401, { error: { code: 'unauthenticated', message: 'expired' } }),
    '/auth/refresh': reply(401, { error: { code: 'unauthenticated', message: 'gone' } }),
  });
  await assert.rejects(() => api.connections(), (e) => e.code === 'unauthenticated');
  assert.deepEqual(moved, []);
});
