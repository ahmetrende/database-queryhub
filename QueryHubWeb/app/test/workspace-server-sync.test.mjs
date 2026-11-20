// Open tabs and preferences follow the person to another browser (2026-11-20).
//
// The server keeps one copy of the open tabs (qhWorkspaceSlim) and one copy of
// a whitelist of UI preferences. Server tabs enter the editor only at page
// start and after a 409, never on a timer or from the storage listener: PR 389
// showed that an echo of a write drops keystrokes.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { transformSync } from 'esbuild';
import { bareWindow, loadInto } from './_load.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, '..', 'src');
const win = loadInto(bareWindow(), 'qh-data.jsx');
const {
  qhPrefsSnapshot, qhPrefsApply, qhPrefsTweaks, QH_PREFS_SYNC_KEYS, QH_WS_SERVER_KEY,
  qhWsSyncKey, qhWsIsBlank, qhWsServerBase, qhWsSyncStart, qhWsConflictOf, qhWsSyncStops,
  QH_WS_SYNC_DEBOUNCE_MS, QH_PREFS_POLL_MS, qhWorkspaceSlim,
  qhWsContentKey, qhWsAgreedBase, qhWs409Plan, qhWsKeepLive, qhMergeWorkspace, qhParseWorkspace, QH_WS_COPY_SUFFIX,
} = win;

function memStorage(init) {
  const m = new Map(Object.entries(init || {}));
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k), map: m };
}
const tab = (id, sql, over = {}) => ({ id, name: 'Q ' + id, sql, conn: sql ? 'c1' : null, db: sql ? 'd1' : null, ...over });
let n = 500;
const newId = () => 't' + (n++);

// ---------- preferences ----------

test('prefs snapshot reads only whitelisted keys', () => {
  const s = memStorage({
    'qh.wrap.v1': '1', 'qh.f8blank.v1': '1', 'qh.params.v1': '0',
    'qh.workspace.v1': '{"tabs":[]}', 'qh.saved.v1': '[]', 'qh.unknown.v1': 'x',
  });
  assert.deepEqual(qhPrefsSnapshot(s), { 'qh.wrap.v1': '1', 'qh.f8blank.v1': '1', 'qh.params.v1': '0' });
  assert.deepEqual(qhPrefsSnapshot(null), {});
});

test('the whitelist holds UI preferences and no user data', () => {
  for (const k of ['qh.prefs.v1', 'qh.wrap.v1', 'qh.f8blank.v1', 'qh.params.v1', 'qh.alias.v1', 'qh.fmt.v1']) {
    assert.ok(QH_PREFS_SYNC_KEYS.includes(k), k + ' is missing');
  }
  for (const k of ['qh.workspace.v1', QH_WS_SERVER_KEY, 'qh.saved.v1', 'qh.sessions.v1', 'qh.scheduled.v1',
    'qh.closed.v1', 'qh.session.v1', 'qh.news.v1', 'qh.connorg.v1', 'qh.reason.v1', 'qh.dbhide.v1',
    'qh.notif.v1', 'qh.resultToSlack.v1', 'qh.notif.browser.v1']) {
    assert.ok(!QH_PREFS_SYNC_KEYS.includes(k), k + ' must not sync');
  }
});

test('prefs apply writes whitelisted strings only and never the workspace', () => {
  const s = memStorage({ 'qh.workspace.v1': 'W' });
  const written = qhPrefsApply(s, { 'qh.workspace.v1': 'X', 'qh.wrap.v1': '1', 'evil.key': 'y', 'qh.fmt.v1': 5 }, false);
  assert.deepEqual(written, ['qh.wrap.v1']);
  assert.equal(s.getItem('qh.workspace.v1'), 'W');
  assert.equal(s.getItem('evil.key'), null);
  assert.equal(s.getItem('qh.fmt.v1'), null);
});

test('prefs apply with onlyMissing keeps the keys this browser has', () => {
  const s = memStorage({ 'qh.wrap.v1': '0' });
  assert.deepEqual(qhPrefsApply(s, { 'qh.wrap.v1': '1', 'qh.f8blank.v1': '1' }, true), ['qh.f8blank.v1']);
  assert.equal(s.getItem('qh.wrap.v1'), '0');
  assert.equal(s.getItem('qh.f8blank.v1'), '1');
});

test('prefs apply refuses a non-object and a value that is too long', () => {
  const s = memStorage();
  assert.deepEqual(qhPrefsApply(s, null), []);
  assert.deepEqual(qhPrefsApply(s, ['qh.wrap.v1']), []);
  assert.deepEqual(qhPrefsApply(s, { 'qh.wrap.v1': 'x'.repeat(5000) }), []);
});

test('tweaks from a stored qh.prefs.v1: known keys of the default type, never brand', () => {
  const defaults = { theme: 'auto', brand: 'default', fontSize: 13 };
  const raw = JSON.stringify({ theme: 'dark', brand: 'warm', fontSize: 'big', extra: 1 });
  assert.deepEqual(qhPrefsTweaks(raw, defaults), { theme: 'dark' });
  assert.deepEqual(qhPrefsTweaks('not json', defaults), {});
  assert.deepEqual(qhPrefsTweaks(null, defaults), {});
});

// ---------- open tabs: pure plan ----------

test('the sync key ignores the clocks and sees the content', () => {
  const a = [tab('t1', 'select 1', { touchedAt: 1 })];
  const b = [tab('t1', 'select 1', { touchedAt: 2 })];
  assert.equal(qhWsSyncKey(a, 't1'), qhWsSyncKey(b, 't1'));
  assert.notEqual(qhWsSyncKey(a, 't1'), qhWsSyncKey([tab('t1', 'select 2')], 't1'));
  assert.notEqual(qhWsSyncKey(a, 't1'), qhWsSyncKey(a, 'welcome'));
});

test('blank: only the welcome page and empty unbound query tabs', () => {
  assert.equal(qhWsIsBlank([{ id: 'welcome', kind: 'welcome' }, tab('t1', '')]), true);
  assert.equal(qhWsIsBlank([{ id: 'welcome', kind: 'welcome' }, tab('t1', 'select 1')]), false);
  assert.equal(qhWsIsBlank([tab('t1', '', { conn: 'c1' })]), false);
  assert.equal(qhWsIsBlank([{ id: 'bundle-1', kind: 'bundle' }]), false);
});

test('start: sync off, server empty, or local blank', () => {
  const local = { tabs: [{ id: 'welcome', kind: 'welcome', name: 'Welcome', sql: '' }, tab('t1', '')], activeId: 't1' };
  assert.equal(qhWsSyncStart(local, { enabled: false }, null, newId).action, 'off');
  assert.equal(qhWsSyncStart(local, null, null, newId).action, 'off');
  const empty = qhWsSyncStart(local, { enabled: true, data: null, version: 0 }, null, newId);
  assert.deepEqual([empty.action, empty.version], ['push', 0]);
  const server = { enabled: true, version: 7, data: qhWorkspaceSlim([tab('t4', 'select 4')], 't4', 1) };
  const take = qhWsSyncStart(local, server, null, newId);
  assert.equal(take.action, 'take');
  assert.equal(take.version, 7);
  assert.deepEqual(take.rec.tabs.map(t => t.sql), ['select 4']);
});

test('start: a tab changed only on the other device is taken, not copied, with the stored base', () => {
  const base = qhWsServerBase(JSON.stringify({ version: 3, data: qhWorkspaceSlim([tab('t1', 'a'), tab('t2', 'b')], 't1', 1) }));
  assert.equal(base.version, 3);
  const local = { tabs: [tab('t1', 'a local'), tab('t2', 'b')], activeId: 't1' };
  const server = { enabled: true, version: 4, data: qhWorkspaceSlim([tab('t1', 'a'), tab('t2', 'b remote')], 't2', 2) };
  const plan = qhWsSyncStart(local, server, base.rec, newId);
  assert.equal(plan.action, 'merge');
  assert.equal(plan.version, 4);
  assert.deepEqual(plan.merged.tabs.map(t => t.sql), ['a local', 'b remote']);
});

test('start: equal tabs on both sides change nothing', () => {
  const local = { tabs: [tab('t1', 'a')], activeId: 't1' };
  const server = { enabled: true, version: 2, data: qhWorkspaceSlim([tab('t1', 'a')], 't1', 1) };
  const plan = qhWsSyncStart(local, server, null, newId);
  assert.equal(plan.action, 'merge');
  assert.equal(plan.merged.changed, false);
  assert.equal(plan.merged.tabs, local.tabs);
});

test('a 409 carries the server copy at the top level, under detail or under error', () => {
  const data = { activeId: 't1', tabs: [] };
  assert.deepEqual(qhWsConflictOf({ status: 409, body: { version: 5, data } }), { version: 5, data });
  assert.deepEqual(qhWsConflictOf({ status: 409, body: { detail: { version: 6, data } } }), { version: 6, data });
  assert.deepEqual(qhWsConflictOf({ status: 409, body: { error: { version: 7, data: null } } }), { version: 7, data: null });
  assert.equal(qhWsConflictOf({ status: 500, body: { version: 1, data } }), null);
  assert.equal(qhWsConflictOf(null), null);
});

test('a 404 or a 413 stops the sync, other errors do not', () => {
  assert.equal(qhWsSyncStops({ status: 404 }), true);
  assert.equal(qhWsSyncStops({ status: 413 }), true);
  assert.equal(qhWsSyncStops({ status: 409 }), false);
  assert.equal(qhWsSyncStops({ status: 500 }), false);
  assert.equal(qhWsSyncStops(null), false);
});

// ---------- open tabs: what a 409 means ----------

// A record as the server stores it: through JSON, so undefined fields drop out.
const rec = (tabs, activeId) => qhParseWorkspace(JSON.parse(JSON.stringify(qhWorkspaceSlim(tabs, activeId, 1))));

test('409: two browser tabs sent the same tabs: adopt, even with another order and active tab', () => {
  const sent = rec([tab('t1', 'a'), tab('t2', 'b')], 't1');
  const theirs = rec([tab('t2', 'b'), tab('t1', 'a')], 't2');
  assert.equal(qhWsContentKey(sent.tabs), qhWsContentKey(theirs.tabs));
  // The person typed after the PUT left: the sent copy still matches, nothing is merged.
  const live = { tabs: [tab('t1', 'a typed'), tab('t2', 'b')], activeId: 't1' };
  const old = rec([tab('t1', 'old'), tab('t2', 'b')], 't1');
  assert.deepEqual(qhWs409Plan({ sent, live, theirs, base: old, local: old }), { kind: 'adopt' });
  // The live tabs match theirs although the sent copy does not.
  const live2 = { tabs: [tab('t1', 'a'), tab('t2', 'b')], activeId: 't2' };
  assert.equal(qhWs409Plan({ sent: rec([tab('t1', 'x')], 't1'), live: live2, theirs, base: old, local: null }).kind, 'adopt');
});

test('409: another content with a server base merges on that base', () => {
  const base = rec([tab('t1', 'a')], 't1');
  const theirs = rec([tab('t1', 'a'), tab('t9', 'z')], 't9');
  const plan = qhWs409Plan({ sent: rec([tab('t1', 'a2')], 't1'), live: { tabs: [tab('t1', 'a2')], activeId: 't1' }, theirs, base, local: null });
  assert.equal(plan.kind, 'merge');
  assert.equal(plan.base, base);
});

test('409 before the first PUT: the base is the tabs that agree with the local record, never null', () => {
  const theirs = rec([tab('t1', 'a'), tab('t2', 'b remote')], 't1');
  const local = rec([tab('t1', 'a'), tab('t2', 'b'), tab('t3', 'c')], 't1');
  const live = { tabs: [tab('t1', 'a typed'), tab('t2', 'b'), tab('t3', 'c')], activeId: 't1' };
  const plan = qhWs409Plan({ sent: rec(live.tabs, 't1'), live, theirs, base: null, local });
  assert.equal(plan.kind, 'merge');
  assert.deepEqual(plan.base.tabs.map(t => t.id), ['t1']);
  const merged = qhMergeWorkspace(plan.base, live, theirs, newId);
  // t1 changed only here: no conflict copy. t2 differs on both sides with no
  // common copy: a copy, no loss. t3 is only local: it stays (a raw local base would drop it).
  assert.deepEqual(merged.tabs.map(t => t.sql), ['a typed', 'b', 'b remote', 'c']);
  assert.equal(merged.tabs.filter(t => t.name.endsWith(QH_WS_COPY_SUFFIX)).length, 1);
  assert.equal(merged.tabs.find(t => t.id === 't1').name, 'Q t1');
});

test('409 with no base, no local record and no sent copy: take the version and send again', () => {
  const theirs = rec([tab('t1', 'a')], 't1');
  assert.deepEqual(qhWs409Plan({ sent: null, live: { tabs: [tab('t1', 'b')], activeId: 't1' }, theirs, base: null, local: null }), { kind: 'retry' });
  // Only the sent copy: it is the agreement source.
  const plan = qhWs409Plan({ sent: rec([tab('t1', 'a'), tab('t2', 'x')], 't1'), live: { tabs: [tab('t1', 'b')], activeId: 't1' }, theirs, base: null, local: null });
  assert.equal(plan.kind, 'merge');
  assert.deepEqual(plan.base.tabs.map(t => t.id), ['t1']);
  assert.equal(qhWsAgreedBase(theirs), null);
});

test('keep live: the active tab keeps its object while its SQL is the same', () => {
  const t1 = tab('t1', 'select 1'), t2 = tab('t2', 'select 2');
  const prev = [t1, t2];
  // Nothing stored changed: the same array comes back.
  assert.equal(qhWsKeepLive(prev, [{ ...t1 }, { ...t2 }], 't1'), prev);
  // A rename of the active tab only: its object stays (the editor keeps the caret).
  const out = qhWsKeepLive(prev, [{ ...t1, name: 'renamed' }, { ...t2, sql: 'select 22' }], 't1');
  assert.equal(out[0], t1);
  assert.notEqual(out[1], t2);
  assert.equal(out[1].sql, 'select 22');
  // New SQL in the active tab: the new object goes in.
  assert.equal(qhWsKeepLive(prev, [{ ...t1, sql: 'select 11' }, t2], 't1')[0].sql, 'select 11');
});

test('the timings: 3 s quiet before a PUT, prefs checked every 5 s', () => {
  assert.equal(QH_WS_SYNC_DEBOUNCE_MS, 3000);
  assert.equal(QH_PREFS_POLL_MS, 5000);
});

// ---------- qh-app.jsx: the shape of the sync ----------

const app = readFileSync(resolve(SRC, 'qh-app.jsx'), 'utf8');
const block = app.slice(app.indexOf('const wsUserKey = '), app.indexOf('const forgetServerWorkspace'));

test('qh-app.jsx: one GET of the workspace at start, after the person is known', () => {
  assert.ok(block.length > 1000, 'the sync block moved');
  assert.equal(app.split("qhApi.workspaceGet('workspace')").length - 1, 1);
  assert.equal(app.split("qhApi.workspaceGet('prefs')").length - 1, 1);
  assert.match(block, /if \(!wsUserKey \|\| !qhApi\.workspaceGet\) return undefined;/);
  assert.match(block, /qhWsSyncStart\(\{ tabs: wsTabsRef\.current, activeId: wsActiveRef\.current \}, r, stored && stored\.rec, wsNewId\)/);
});

test('qh-app.jsx: a PUT waits for 3 s of quiet and sends exactly qhWorkspaceSlim', () => {
  assert.match(block, /setTimeout\(\(\) => \{ s\.timer = null; if \(wsPushRef\.current\) wsPushRef\.current\(false\); \}, QH_WS_SYNC_DEBOUNCE_MS\)/);
  assert.match(block, /const data = qhWorkspaceSlim\(list, active\);/);
  assert.match(block, /qhApi\.workspacePut\('workspace', data, s\.version\)/);
});

test('qh-app.jsx: a 409 follows qhWs409Plan: adopt first, then merge on the plan base or send again', () => {
  const i = block.indexOf('const c = qhWsConflictOf(e);');
  assert.notEqual(i, -1);
  const tail = block.slice(i, block.indexOf('wsPushRef.current = wsPush;'));
  assert.match(tail, /qhWs409Plan\(\{/);
  assert.match(tail, /sent: qhParseWorkspace\(data\)/);
  assert.match(tail, /local = wsBaseRef\.current && wsBaseRef\.current\.rec/);
  // Adopt comes before the conflict limit and never touches the tabs.
  const adopt = tail.indexOf("if (plan.kind === 'adopt') {");
  assert.ok(adopt !== -1 && adopt < tail.indexOf('if (s.conflicts >= 1)'));
  const adoptBody = tail.slice(adopt, tail.indexOf('return;', adopt));
  assert.doesNotMatch(adoptBody, /wsApplyServer|setTabs|qhMergeWorkspace/);
  assert.match(adoptBody, /wsKeepBase\(c\.version, theirs\)/);
  assert.match(tail, /if \(s\.conflicts >= 1\) \{ wsSyncOff\(/);
  assert.match(tail, /qhMergeWorkspace\(plan\.base, \{ tabs: prev, activeId: prevActive \}, theirs, wsNewId\)/);
  assert.match(tail, /if \(plan\.kind === 'retry'\) \{ wsPush\(false\); return; \}/);
  // The PUT after a merge waits for the render that applies it.
  assert.doesNotMatch(tail.slice(tail.indexOf('qhMergeWorkspace(plan.base')), /wsPush\(false\)/);
  assert.match(block, /if \(qhWsSyncStops\(e\)\) \{ wsSyncOff\('HTTP ' \+ e\.status\); return; \}/);
});

test('qh-app.jsx: server tabs merge inside setTabs(prev => ...), on the newest tabs', () => {
  const i = block.indexOf('const wsApplyServer = ');
  const body = block.slice(i, block.indexOf('const wsPushRef = useRef(null);'));
  assert.match(body, /setTabs\(prev => \{/);
  assert.match(body, /const merged = mergeOf\(prev, active\);/);
  assert.match(body, /qhWsKeepLive\(prev, merged\.tabs, active\)/);
  // No precomputed list goes into setTabs anywhere in the sync block.
  assert.doesNotMatch(block, /setTabs\((list|merged\.tabs|plan\.merged\.tabs)\)/);
  assert.doesNotMatch(block, /plan\.merged\.changed/);
  // "Blank" at start is decided on prev, not on the ref.
  assert.match(block, /wsApplyServer\(\(prev, prevActive\) => \(qhWsIsBlank\(prev\)/);
  assert.match(block, /qhMergeWorkspace\(startBase, \{ tabs: prev, activeId: prevActive \}, plan\.rec, wsNewId\)/);
});

test('qh-app.jsx: pagehide sends the last change with keepalive', () => {
  assert.match(app, /window\.addEventListener\('pagehide', onHide\)/);
  assert.match(block, /qhApi\.workspacePut\('workspace', data, s\.version, \{ keepalive: true \}\)/);
  assert.match(block, /qhApi\.workspacePut\('prefs', snap, p\.version, \{ keepalive: true \}\)/);
});

test('qh-app.jsx: the storage listener never applies or sends server data', () => {
  const h = app.indexOf('const onStorage');
  const body = app.slice(h, app.indexOf("window.addEventListener('storage'", h));
  assert.doesNotMatch(body, /workspaceGet|workspacePut|wsApplyServer|qhWsSyncStart/);
  // Server tabs reach setTabs only through wsApplyServer, called at start and on a 409.
  assert.equal(block.split('wsApplyServer(').length - 1, 3);
});

test('qh-app.jsx: sign-out clears the stored server copy and keeps the server copy', () => {
  const i = app.indexOf('const signOut = () => {');
  const so = app.slice(i, app.indexOf('setUser(null);', i));
  assert.match(so, /localStorage\.removeItem\(QH_WS_SERVER_KEY\)/);
  assert.doesNotMatch(so, /workspaceDelete/);
});

// ---------- the client and the mock ----------

test('qh-api.jsx: the three calls hit /api/me/workspace/{kind}', async () => {
  const calls = [];
  const w = bareWindow();
  w.fetch = async (url, opts) => {
    calls.push({ url, opts });
    if (opts.method === 'PUT' && JSON.parse(opts.body).baseVersion === 1) {
      return { status: 409, ok: false, json: async () => ({ version: 2, data: { activeId: '', tabs: [] }, updatedAt: 'x' }) };
    }
    if (opts.method === 'DELETE') return { status: 204, ok: true, json: async () => null };
    return { status: 200, ok: true, json: async () => ({ version: 4 }) };
  };
  const api = loadInto(w, 'qh-api.jsx').qhApi;
  await api.workspaceGet('prefs');
  assert.equal(calls[0].url, '/api/me/workspace/prefs');
  await api.workspacePut('workspace', { a: 1 }, 3, { keepalive: true });
  assert.equal(calls[1].opts.method, 'PUT');
  assert.deepEqual(JSON.parse(calls[1].opts.body), { data: { a: 1 }, baseVersion: 3 });
  assert.equal(calls[1].opts.keepalive, true);
  await api.workspacePut('workspace', { a: 1 }, 0);
  assert.equal(calls[2].opts.keepalive, undefined);
  const e = await api.workspacePut('workspace', { a: 2 }, 1).then(() => null, (err) => err);
  assert.equal(e.status, 409);
  assert.deepEqual(win.qhWsConflictOf(e), { version: 2, data: { activeId: '', tabs: [] } });
  assert.equal(await api.workspaceDelete('workspace'), null);
  assert.equal(calls[4].opts.method, 'DELETE');
});

function mock() {
  const w = bareWindow();
  w.setTimeout = (fn) => { if (typeof fn === 'function') fn(); return 0; };
  w.clearTimeout = () => {};
  w.setInterval = () => 0;
  w.clearInterval = () => {};
  w.dispatchEvent = () => true;
  w.Event = class { constructor(type) { this.type = type; } };
  loadInto(w, 'qh-data.jsx');
  const src = readFileSync(resolve(HERE, '..', '..', 'qh-api-mock.jsx'), 'utf8');
  const code = transformSync(src, { loader: 'jsx', jsx: 'transform', jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment' }).code;
  new Function('window', 'with (window) {\n' + code + '\n}')(w);
  return w;
}

test('mock: get, put, a 409 with the server copy, delete', async () => {
  const w = mock();
  const api = w.qhApi;
  assert.deepEqual(await api.workspaceGet('workspace'), { enabled: true, data: null, version: 0 });
  // Through JSON, as the server stores it: undefined fields drop out.
  const one = JSON.parse(JSON.stringify(qhWorkspaceSlim([tab('t1', 'select 1')], 't1', 1)));
  assert.equal((await api.workspacePut('workspace', one, 0)).version, 1);
  const got = await api.workspaceGet('workspace');
  assert.equal(got.version, 1);
  assert.deepEqual(got.data, one);
  const e = await api.workspacePut('workspace', { tabs: [] }, 0).then(() => null, (err) => err);
  assert.equal(e.status, 409);
  assert.deepEqual(w.qhWsConflictOf(e), { version: 1, data: one });
  assert.equal((await api.workspacePut('workspace', { tabs: [] }, 1)).version, 2);
  await api.workspaceDelete('workspace');
  assert.equal((await api.workspaceGet('workspace')).version, 0);
  const big = await api.workspacePut('prefs', { x: 'y'.repeat(300 * 1024) }, 0).then(() => null, (err) => err);
  assert.equal(big.status, 413);
  assert.equal(w.qhWsSyncStops(big), true);
});
