// The local copy of the open tabs belongs to one person (B03, 2026-11-22).
//
// Sign-out clears the per-user keys, but an expired session does not. On a
// shared browser the next person then saw the open tabs and SQL of the last
// person. qh.workspace.owner.v1 now names the owner. A sign-in by another person
// drops the stored copy. The same person keeps it, so an expiry loses no draft.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { bareWindow, loadInto } from './_load.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const APP = readFileSync(resolve(HERE, '..', '..', 'qh-app.jsx'), 'utf8');
const win = loadInto(bareWindow(), 'qh-data.jsx');
const { QH_WS_OWNER_KEY, QH_WS_SERVER_KEY, qhWsOwnerOf, qhWsOwnerPlan, qhWsClaimStorage } = win;

function memStorage(init) {
  const m = new Map(Object.entries(init || {}));
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k), map: m };
}
const WS = '{"activeId":"t1","tabs":[{"id":"t1","sql":"select 1"}]}';
const USER_KEYS = ['qh.workspace.v1', 'qh.saved.v1', 'qh.sessions.v1', 'qh.dbhide.v1'];
const stored = (owner) => memStorage({
  'qh.workspace.v1': WS, 'qh.saved.v1': '[1]', 'qh.sessions.v1': '[2]', 'qh.dbhide.v1': '["c/d"]',
  [QH_WS_SERVER_KEY]: '{"version":3}', 'qh.wrap.v1': '1',
  ...(owner == null ? {} : { [QH_WS_OWNER_KEY]: owner }),
});

test('the owner key has its own name', () => {
  assert.equal(QH_WS_OWNER_KEY, 'qh.workspace.owner.v1');
});

test('the owner id is the id that the server sync used', () => {
  assert.equal(qhWsOwnerOf({ slackId: 'S1', id: 'I1', email: 'a@x' }), 'S1');
  assert.equal(qhWsOwnerOf({ id: 'I1', email: 'a@x' }), 'I1');
  assert.equal(qhWsOwnerOf({ email: 'a@x' }), 'a@x');
  assert.equal(qhWsOwnerOf({}), 'me');
  assert.equal(qhWsOwnerOf(null), '');
});

test('plan: none without a user, stamp without an owner, keep or drop by owner', () => {
  assert.equal(qhWsOwnerPlan('A', ''), 'none');
  assert.equal(qhWsOwnerPlan(null, 'A'), 'stamp');
  assert.equal(qhWsOwnerPlan('', 'A'), 'stamp');
  assert.equal(qhWsOwnerPlan('A', 'A'), 'keep');
  assert.equal(qhWsOwnerPlan('A', 'B'), 'drop');
});

test('another person signs in: the stored workspace and the per-user keys go', () => {
  const s = stored('A');
  assert.equal(qhWsClaimStorage(s, 'B', USER_KEYS), 'drop');
  for (const k of USER_KEYS) assert.equal(s.getItem(k), null, k + ' must go');
  assert.equal(s.getItem(QH_WS_SERVER_KEY), null);
  assert.equal(s.getItem(QH_WS_OWNER_KEY), 'B');
  // A preference is not user data: it stays.
  assert.equal(s.getItem('qh.wrap.v1'), '1');
});

test('the same person signs in again: nothing changes', () => {
  const s = stored('A');
  const before = new Map(s.map);
  assert.equal(qhWsClaimStorage(s, 'A', USER_KEYS), 'keep');
  assert.deepEqual(s.map, before);
});

test('a stored workspace with no owner stays and the first person owns it', () => {
  const s = stored(null);
  assert.equal(qhWsClaimStorage(s, 'A', USER_KEYS), 'stamp');
  assert.equal(s.getItem('qh.workspace.v1'), WS);
  assert.equal(s.getItem(QH_WS_SERVER_KEY), '{"version":3}');
  assert.equal(s.getItem(QH_WS_OWNER_KEY), 'A');
  // The next person is another person now.
  assert.equal(qhWsClaimStorage(s, 'B', USER_KEYS), 'drop');
  assert.equal(s.getItem('qh.workspace.v1'), null);
});

test('no user changes nothing, and storage that throws changes nothing', () => {
  const s = stored('A');
  const before = new Map(s.map);
  assert.equal(qhWsClaimStorage(s, '', USER_KEYS), 'none');
  assert.deepEqual(s.map, before);
  const off = { getItem: () => { throw new Error('off'); }, setItem: () => { throw new Error('off'); }, removeItem: () => {} };
  assert.equal(qhWsClaimStorage(off, 'A', USER_KEYS), 'none');
});

// ---------- qh-app.jsx: where the check runs ----------

const claim = () => {
  const m = APP.match(/const wsClaim = \(u\) => \{[\s\S]*?\n  \};/);
  assert.ok(m, 'wsClaim is not where this test expects it');
  return m[0];
};

test('qh-app.jsx: the check runs right before setUser, at boot and on the legacy sign-in', () => {
  assert.match(APP, /wsClaim\(m && m\.user\); setUser\(m\.user \? /);
  assert.match(APP, /const signIn = \(u\) => \{ wsClaim\(u\); setUser\(u\); \};/);
  // An expired session only clears the user. It never drops the tabs.
  assert.match(APP, /const onSignedOut = \(\) => setUser\(null\);/);
  // Nothing renders the tabs before /me answers.
  assert.match(APP, /if \(!authChecked\) return /);
});

test('qh-app.jsx: a drop clears the sign-out list and resets the tabs in memory', () => {
  const body = claim();
  assert.match(body, /qhWsClaimStorage\(localStorage, qhWsOwnerOf\(u\), qhUserLocalKeys\(\)\)/);
  assert.match(body, /if \(plan !== 'drop'\) return;/);
  const drop = body.slice(body.indexOf("if (plan !== 'drop') return;"));
  assert.match(drop, /setTabs\(blank\.tabs\);/);
  assert.match(drop, /wsTabsRef\.current = blank\.tabs;/);
  assert.match(drop, /wsBaseRef\.current = \{ raw: null, rec: \{ activeId: '', tabs: \[\] \} \};/);
  assert.match(drop, /setSavedList\(\[\]\);/);
  assert.match(drop, /qhUndoForget\(\);/);
});

test('qh-app.jsx: one key list, used by sign-out and by the owner check', () => {
  assert.equal(APP.split('QH_CONNORG_KEY, QH_REASON_KEY').length - 1, 1);
  const so = APP.match(/const signOut = \(\) => \{[\s\S]*?\n  \};/)[0];
  assert.match(so, /qhUserLocalKeys\(\)\.forEach\(\(k\) => localStorage\.removeItem\(k\)\)/);
  // Sign-out keeps the owner, so a stale page of that person cannot hand its tabs to the next one.
  const keys = APP.match(/function qhUserLocalKeys\(\) \{[\s\S]*?\n\}/)[0];
  assert.doesNotMatch(keys.slice(keys.indexOf('return')), /QH_WS_OWNER_KEY/);
});

test('qh-app.jsx: the server sync keys on the owner id', () => {
  assert.match(APP, /const wsUserKey = qhWsOwnerOf\(user\);/);
});

test('qh-app.jsx: a page of another person neither writes, merges nor sends the tabs', () => {
  assert.match(APP, /return !owner \|\| owner === wsOwnerRef\.current;/);
  const write = APP.slice(APP.indexOf('const stored = wsStoredRef.current;'), APP.indexOf('wsWrite(list, active);'));
  assert.match(write, /if \(!wsMine\(\)\) return;/);
  const listen = APP.slice(APP.indexOf('const onStorage'), APP.indexOf("window.addEventListener('storage'"));
  assert.match(listen, /if \(!wsMine\(\)\) return;/);
  const push = APP.slice(APP.indexOf('const wsPush = (keepalive) => {'), APP.indexOf('const key = qhWsSyncKey(list, active);'));
  assert.match(push, /if \(!wsMine\(\)\) \{ wsSyncOff\('another person signed in'\); return; \}/);
});
