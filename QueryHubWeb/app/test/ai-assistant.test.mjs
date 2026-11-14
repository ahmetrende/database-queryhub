// The AI assistant (M13, PR C): the editor panel, the admin form, the client
// calls, the AI chips and the wiring in qh-app.jsx.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';
import { page, web, jsdomOpts } from './_mount.mjs';

const PANEL_FILES = ['qh-data.jsx', 'qh-ai.jsx'];
const ADMIN_FILES = ['qh-data.jsx', 'qh-modal.jsx', 'qh-admin-access.jsx', 'qh-ai.jsx'];

const STATUS = { enabled: true, default: '1:claude-opus-5-5', userModelChoice: false, models: [] };
const ANSWER = {
  text: 'This lists the orders.\n\n```sql\nSELECT id FROM orders;\n```\n\nIt reads one table.',
  modelName: 'claude-opus-5-5', stop: 'end',
  sent: { promptChars: 5, sqlChars: 0, tableNames: 12, tablesRead: ['public.orders'] },
};

function setValue(p, el, v) {
  return p.React.act(() => {
    const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value').set;
    setter.call(el, v);
    el.dispatchEvent(new p.win.Event('input', { bubbles: true }));
  });
}

async function mountPanel({ complete, sql = 'SELECT 1', conn = 'srv-alpha', db = 'shop', status = STATUS } = {}) {
  const calls = [], inserted = [], toasts = [];
  const p = page(PANEL_FILES, { prep: (win) => { win.qhApi = { aiComplete: (b) => { calls.push(b); return complete(b); } }; } });
  p.render(p.React.createElement(p.win.QhAiPanel, {
    status, tabId: 't1', conn, db, getSql: () => sql,
    onInsert: (t) => inserted.push(t), onClose: () => {}, onToast: (m) => toasts.push(m),
  }));
  await p.flush();
  return { p, calls, inserted, toasts };
}

const button = (p, text) => p.all('button').find(b => b.textContent.trim() === text);

test('segments: only fenced blocks become code', () => {
  const win = bareWindow();
  const { qhAiSegments } = loadInto(win, 'qh-ai.jsx');
  assert.deepEqual(qhAiSegments('a\n```sql\nSELECT 1;\n```\nb'), [
    { kind: 'text', text: 'a\n' }, { kind: 'code', lang: 'sql', text: 'SELECT 1;' }, { kind: 'text', text: '\nb' }]);
  assert.deepEqual(qhAiSegments('<b>x</b>'), [{ kind: 'text', text: '<b>x</b>' }]);
  assert.deepEqual(qhAiSegments('```\nx\n```'), [{ kind: 'code', lang: '', text: 'x' }]);
  // Two blocks stay two blocks, with the text between them.
  assert.deepEqual(qhAiSegments('```sql\nA\n```\nmid\n```sql\nB\n```').map(x => x.kind + ':' + x.text.trim()),
    ['code:A', 'text:mid', 'code:B']);
});

test('the Sent line says what left QueryHub', () => {
  const { qhAiSentLine } = loadInto(bareWindow(), 'qh-ai.jsx');
  assert.equal(qhAiSentLine({ promptChars: 3, sqlChars: 40, tableNames: 1, tablesRead: ['a.b'] }),
    'Sent: your prompt, 40 characters of SQL, 1 table name. Read the detail of a.b.');
  assert.equal(qhAiSentLine({ promptChars: 0, sqlChars: 0, tableNames: 0, tablesRead: [] }), 'Sent: 0 table names.');
});

test('Write SQL sends the prompt without the editor SQL and shows the answer', jsdomOpts, async () => {
  const { p, calls, inserted } = await mountPanel({ complete: () => Promise.resolve(ANSWER) });
  await setValue(p, p.doc.querySelector('.qh-ai-prompt'), 'list orders');
  p.click(button(p, 'Send'));
  await p.flush(); await p.flush();
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], { mode: 'generate', prompt: 'list orders', sql: '', messages: [],
    connectionId: 'srv-alpha', databaseId: 'shop' });
  assert.equal(p.all('.qh-ai-pre')[0].textContent, 'SELECT id FROM orders;');
  assert.match(p.doc.querySelector('.qh-ai-turn.is-ai .qh-ai-meta').textContent, /Sent: your prompt, 12 table names\. Read the detail of public\.orders\./);
  p.click(button(p, 'Insert'));
  assert.deepEqual(inserted, ['SELECT id FROM orders;']);
});

test('Explain sends the editor SQL, and a chat sends the earlier turns', jsdomOpts, async () => {
  const { p, calls } = await mountPanel({ complete: () => Promise.resolve(ANSWER), sql: 'SELECT 2' });
  p.click(button(p, 'Explain'));
  p.click(button(p, 'Send'));
  await p.flush(); await p.flush();
  assert.equal(calls[0].mode, 'explain');
  assert.equal(calls[0].sql, 'SELECT 2');
  p.click(button(p, 'Chat'));
  await setValue(p, p.doc.querySelector('.qh-ai-prompt'), 'why?');
  p.click(button(p, 'Send'));
  await p.flush(); await p.flush();
  assert.deepEqual(calls[1].messages, [{ role: 'user', content: 'Explain' }, { role: 'assistant', content: ANSWER.text }]);
});

test('an error from the server shows as its own message', jsdomOpts, async () => {
  const { p } = await mountPanel({ complete: () => Promise.reject(Object.assign(new Error('You have no access to that database.'), { status: 403 })) });
  await setValue(p, p.doc.querySelector('.qh-ai-prompt'), 'x');
  p.click(button(p, 'Send'));
  await p.flush(); await p.flush();
  assert.equal(p.doc.querySelector('.qh-ai-turn.is-error').textContent, 'You have no access to that database.');
});

test('no connection: the panel says so and does not send', jsdomOpts, async () => {
  const { p, calls } = await mountPanel({ complete: () => Promise.resolve(ANSWER), db: null });
  assert.equal(p.doc.querySelector('.qh-ai-need').textContent, 'Choose a connection and a database first.');
  assert.equal(button(p, 'Send').disabled, true);
  assert.equal(calls.length, 0);
});

test('the model picker shows only when users may choose', jsdomOpts, async () => {
  const models = [{ id: '1:a', provider: 'P', model: 'a' }, { id: '2:b', provider: 'Q', model: 'b' }];
  const one = await mountPanel({ complete: () => Promise.resolve(ANSWER) });
  assert.equal(one.p.all('.qh-ai-model').length, 0);
  const two = await mountPanel({ complete: () => Promise.resolve(ANSWER), status: { ...STATUS, userModelChoice: true, models, default: '2:b' } });
  assert.equal(two.p.all('.qh-ai-model').length, 1);
  await setValue(two.p, two.p.doc.querySelector('.qh-ai-prompt'), 'x');
  two.p.click(button(two.p, 'Send'));
  await two.p.flush();
  assert.equal(two.calls[0].model, '2:b');
});

const ADMIN_VIEW = {
  settings: { enabled: false, defaultProvider: 1, defaultModel: '', userModelChoice: false, effort: '',
    instructions: '', maxCallsPerHour: 30, timeoutSec: 180, schemaMaxTables: 200 },
  providers: [{ id: 1, name: 'Anthropic', kind: 'anthropic', baseUrl: null, models: ['claude-opus-5-5'],
    supportsTools: true, enabled: true, keySet: true, keySetBy: 'U0EXAMPLE001', keySetAt: null }],
  presets: [{ name: 'OpenAI', kind: 'openai', baseUrl: 'https://api.openai.com/v1', models: [] }],
  efforts: ['', 'low', 'high'], ready: false, notReady: 'The assistant is off.', anthropicSdk: true,
};

async function mountAdmin(api) {
  const toasts = [];
  const p = page(ADMIN_FILES, { url: 'https://localhost/#admin/ai', prep: (win) => { win.qhApi = api; } });
  p.render(p.React.createElement(p.win.AiAdminView, { st: { pushToast: (m) => toasts.push(m) } }));
  await p.flush(); await p.flush();
  return { p, toasts };
}

test('the admin form shows the state and never a key', jsdomOpts, async () => {
  const { p } = await mountAdmin({ adminAi: () => Promise.resolve(ADMIN_VIEW) });
  assert.equal(p.doc.querySelector('.qh-ai-ready').textContent, 'Off: The assistant is off.');
  assert.equal(p.doc.querySelector('.qh-ai-keystate').textContent, 'API key set by U0EXAMPLE001.');
  assert.equal(p.doc.querySelector('.qh-ai-keyin').getAttribute('type'), 'password');
  assert.equal(p.doc.querySelector('.qh-ai-keyin').value, '');
});

test('turning it on saves only the changed setting', jsdomOpts, async () => {
  const saved = [];
  const { p, toasts } = await mountAdmin({
    adminAi: () => Promise.resolve(ADMIN_VIEW),
    adminAiSave: (s) => { saved.push(s); return Promise.resolve({ ...ADMIN_VIEW, settings: { ...ADMIN_VIEW.settings, ...s }, ready: true }); },
  });
  p.click(p.doc.querySelector('[aria-label="Turn on the assistant"]'));
  p.click(button(p, 'Save changes'));
  await p.flush(); await p.flush();
  assert.deepEqual(saved, [{ enabled: true }]);
  assert.equal(p.doc.querySelector('.qh-ai-ready').textContent, 'On for users');
  assert.deepEqual(toasts, ['AI settings saved.']);
});

test('a new key goes to the server once and the field empties', jsdomOpts, async () => {
  const sent = [];
  const { p } = await mountAdmin({
    adminAi: () => Promise.resolve(ADMIN_VIEW),
    adminAiProviderSave: (id, b) => { sent.push([id, b]); return Promise.resolve(ADMIN_VIEW); },
  });
  await setValue(p, p.doc.querySelector('.qh-ai-keyin'), '  sk-test-1  ');
  p.click(button(p, 'Save key'));
  await p.flush(); await p.flush();
  assert.deepEqual(sent, [[1, { apiKey: 'sk-test-1' }]]);
  assert.equal(p.doc.querySelector('.qh-ai-keyin').value, '');
});

test('delete asks inline first, and the default provider cannot be deleted', jsdomOpts, async () => {
  const deleted = [];
  const other = { ...ADMIN_VIEW.providers[0], id: 2, name: 'DeepSeek', kind: 'openai', baseUrl: 'https://api.example.test' };
  const view = { ...ADMIN_VIEW, providers: [ADMIN_VIEW.providers[0], other] };
  const { p } = await mountAdmin({
    adminAi: () => Promise.resolve(view),
    adminAiProviderDelete: (id) => { deleted.push(id); return Promise.resolve(ADMIN_VIEW); },
  });
  const dels = p.all('.qh-ai-prov button').filter(b => b.textContent === 'Delete');
  assert.equal(dels[0].disabled, true);
  p.click(dels[1]);
  assert.equal(deleted.length, 0);
  // Keep closes the question and deletes nothing.
  p.click(button(p, 'Keep'));
  await p.flush();
  assert.equal(deleted.length, 0);
  assert.equal(p.all('.qh-revokeconfirm').length, 0);
  p.click(dels[1]);
  p.click(p.doc.querySelector('.qh-revokeconfirm .qh-btn-danger'));
  await p.flush(); await p.flush();
  assert.deepEqual(deleted, [2]);
});

test('the client calls hit the documented paths', async () => {
  const calls = [];
  const win = bareWindow();
  win.fetch = async (url, o) => { calls.push([url, (o && o.method) || 'GET', o && o.body ? JSON.parse(o.body) : null]);
    return { ok: true, status: 200, json: async () => ({}) }; };
  const { qhApi } = loadInto(win, 'qh-api.jsx');
  await qhApi.aiStatus();
  await qhApi.aiComplete({ mode: 'chat' });
  await qhApi.adminAi();
  await qhApi.adminAiSave({ enabled: true });
  await qhApi.adminAiProviderAdd({ name: 'x' });
  await qhApi.adminAiProviderSave(3, { enabled: false });
  await qhApi.adminAiProviderDelete(3);
  await qhApi.adminAiTest(3);
  assert.deepEqual(calls.map(c => [c[0].replace(/^.*\/api/, ''), c[1]]), [
    ['/ai/status', 'GET'], ['/ai/complete', 'POST'], ['/admin/ai', 'GET'], ['/admin/ai', 'PUT'],
    ['/admin/ai/providers', 'POST'], ['/admin/ai/providers/3', 'PUT'], ['/admin/ai/providers/3', 'DELETE'],
    ['/admin/ai/test', 'POST']]);
  assert.deepEqual(calls[3][2], { settings: { enabled: true } });
  assert.deepEqual(calls[7][2], { providerId: 3 });
});

test('qh-app wires the panel, the insert and the mark', () => {
  const app = web('qh-app.jsx');
  assert.match(app, /\.\.\.\(cur\.aiAssisted \? \{ aiAssisted: true \} : null\)/);
  assert.match(app, /patch\(activeId, \{ aiAssisted: true \}\)/);
  assert.match(app, /onAi=\{aiStatus\.enabled && !tab\.kind \?/);
  assert.match(app, /aiStatus\.enabled && aiOpen && !tab\.kind && \(/);
  // The status loads on its own, never inside the eager `jobs` list.
  const jobs = app.slice(app.indexOf('const jobs = ['), app.indexOf('Promise.allSettled(jobs)'));
  assert.ok(!jobs.includes('aiStatus'));
});

test('the queue and history show the AI chip', () => {
  assert.match(web('qh-admin.jsx'), /it\.aiAssisted && <span className="qh-ai-chip"/);
  assert.match(web('qh-panels.jsx'), /h\.aiAssisted && <span className="qh-ai-chip"/);
  assert.match(web('qh-data.jsx'), /aiAssisted: !!h\.aiAssisted/);
  assert.match(web('qh-admin.jsx'), /ai: 'super'/);
});

// ---- who can use it -----------------------------------------------------------

function pick(p, el, v) {
  return p.React.act(() => {
    const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value').set;
    setter.call(el, v);
    el.dispatchEvent(new p.win.Event('change', { bubbles: true }));
  });
}

const AUD_VIEW = {
  ...ADMIN_VIEW,
  audience: { count: 2, roles: { super_admin: 'Super-admins', dba: 'DBAs', approver: 'Approvers' },
    rules: [{ id: 1, kind: 'role', value: 'super_admin' },
            { id: 2, kind: 'person', value: 'U0EXAMPLE009' }] },
};

async function mountAud(api) {
  const p = page(ADMIN_FILES, { url: 'https://localhost/#admin/ai', prep: (win) => { win.qhApi = api; } });
  const st = { pushToast: () => {}, teams: [{ id: 4, name: 'Data' }, { id: 5, name: 'Backend' }],
    people: [{ id: 'U0EXAMPLE009', handle: 'U0EXAMPLE009', name: 'Sam Lee' }], resolvePerson: () => {} };
  p.render(p.React.createElement(p.win.AiAdminView, { st }));
  await p.flush(); await p.flush();
  return p;
}

const addButton = (p) => p.all('.qh-ai-audadd button.qh-btn-primary')[0];

test('the audience card lists the rules and the count, with no Block', jsdomOpts, async () => {
  const p = await mountAud({ adminAi: () => Promise.resolve(AUD_VIEW) });
  assert.equal(p.doc.querySelector('.qh-ai-audcount').textContent, '2 people can use it now.');
  const rows = p.all('.qh-ai-audrow').map(r => r.querySelector('.qh-ai-kind').textContent + ' ' + r.querySelector('.qh-ai-audname').textContent);
  assert.deepEqual(rows, ['Role Super-admins', 'Person Sam Lee (U0EXAMPLE009)']);
  assert.ok(!/Block/.test(p.doc.querySelector('.qh-cfg-sections').textContent));
  assert.equal(p.all('[aria-label="Allow or block"]').length, 0);
});

test('several roles go to the server in one call', jsdomOpts, async () => {
  const added = [];
  const p = await mountAud({ adminAi: () => Promise.resolve(AUD_VIEW),
    adminAiAudienceAdd: (b) => { added.push(b); return Promise.resolve(AUD_VIEW); } });
  await pick(p, p.doc.querySelector('[aria-label="Whom to add"]'), 'role');
  assert.equal(addButton(p).disabled, true);
  const boxes = p.all('.qh-ai-audrole input');
  // Super-admins already has a rule, so its box is off.
  assert.equal(boxes[0].disabled, true);
  p.click(boxes[1]);
  p.click(boxes[2]);
  assert.equal(addButton(p).textContent, 'Add 2 rules');
  p.click(addButton(p));
  await p.flush(); await p.flush();
  assert.deepEqual(added, [{ kind: 'role', values: ['dba', 'approver'] }]);
});

test('several teams go to the server as strings', jsdomOpts, async () => {
  const added = [];
  const p = await mountAud({ adminAi: () => Promise.resolve(AUD_VIEW),
    adminAiAudienceAdd: (b) => { added.push(b); return Promise.resolve(AUD_VIEW); } });
  // Teams is the first choice. TeamMultiPick lists the teams as buttons.
  const input = p.doc.querySelector('.qh-addp-team-in');
  await setValue(p, input, 'a');
  for (const name of ['Data', 'Backend']) {
    const opt = p.all('.qh-addp-teamopt').find(b => b.textContent.startsWith(name));
    assert.ok(opt, name);
    p.React.act(() => opt.dispatchEvent(new p.win.MouseEvent('mousedown', { bubbles: true })));
    p.click(opt);
    await setValue(p, p.doc.querySelector('.qh-addp-team-in'), 'a');
  }
  p.click(addButton(p));
  await p.flush(); await p.flush();
  assert.equal(added.length, 1);
  assert.equal(added[0].kind, 'team');
  assert.deepEqual(added[0].values.slice().sort(), ['4', '5']);
});

test('everyone needs no value, and Remove deletes one rule', jsdomOpts, async () => {
  const added = [], removed = [];
  const p = await mountAud({ adminAi: () => Promise.resolve(AUD_VIEW),
    adminAiAudienceAdd: (b) => { added.push(b); return Promise.resolve(AUD_VIEW); },
    adminAiAudienceRemove: (id) => { removed.push(id); return Promise.resolve(AUD_VIEW); } });
  await pick(p, p.doc.querySelector('[aria-label="Whom to add"]'), 'everyone');
  assert.equal(addButton(p).textContent, 'Add everyone');
  p.click(addButton(p));
  await p.flush(); await p.flush();
  assert.deepEqual(added, [{ kind: 'everyone', values: [] }]);
  p.click(p.all('.qh-ai-audrow button')[1]);
  await p.flush(); await p.flush();
  assert.deepEqual(removed, [2]);
});

test('the audience client calls hit the documented paths', async () => {
  const calls = [];
  const win = bareWindow();
  win.fetch = async (url, o) => { calls.push([url.replace(/^.*\/api/, ''), (o && o.method) || 'GET', o && o.body ? JSON.parse(o.body) : null]);
    return { ok: true, status: 200, json: async () => ({}) }; };
  const { qhApi } = loadInto(win, 'qh-api.jsx');
  await qhApi.adminAiAudienceAdd({ kind: 'team', values: ['4', '5'] });
  await qhApi.adminAiAudienceRemove(7);
  assert.deepEqual(calls, [['/admin/ai/audience', 'POST', { kind: 'team', values: ['4', '5'] }],
    ['/admin/ai/audience/7', 'DELETE', null]]);
});
