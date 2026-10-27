// Admin > Metrics: the Channel mix block (2026-10-27).
// The server answers with `channelMix` and `channelWeekly` (metrics_defs.channel_mix).
// The view only draws them: requests and distinct people per channel, the share the
// server computed, a weekly stack, and the window the screen already has.
import test from 'node:test';
import assert from 'node:assert/strict';

import { page, web, jsdomOpts } from './_mount.mjs';

const FILES = ['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-results-tools.jsx',
  'qh-panels.jsx', 'qh-admin-data.jsx', 'qh-admin-insights.jsx'];

const MIX = {
  totalRequests: 100, totalUsers: 10,
  channels: [
    { key: 'slack', label: 'Slack', requests: 30, requestShare: 30, users: 8, userShare: 80 },
    { key: 'web', label: 'Web', requests: 60, requestShare: 60, users: 6, userShare: 60 },
    { key: 'idp', label: 'IdP', requests: 9, requestShare: 9, users: 1, userShare: 10 },
    { key: 'carrier-pigeon', label: 'carrier-pigeon', requests: 1, requestShare: 1, users: 1, userShare: 10 },
  ],
  segments: [{ label: 'Slack + Web', users: 5 }, { label: 'Slack', users: 3 }, { label: 'Slack + Web + IdP', users: 1 }, { label: 'carrier-pigeon', users: 1 }],
};
const WEEKLY = [
  { period: '2026-W42', byChannel: { slack: { requests: 10, users: 5 }, web: { requests: 30, users: 4 } } },
  { period: '2026-W43', byChannel: { slack: { requests: 20, users: 7 }, web: { requests: 30, users: 5 }, idp: { requests: 9, users: 1 } } },
];
const payload = (over = {}) => ({ headline: { total: 100 }, channelMix: MIX, channelWeekly: WEEKLY,
  decisionTime: { requests: 0, p50Sec: null, p90Sec: null, bands: [], byServer: [], byTeam: [] }, ...over });

function mount(adminMetrics, st = { metrics: {} }) {
  const calls = [];
  const p = page(FILES, {
    url: 'https://localhost/#admin/metrics',
    prep: (win) => { win.qhApi = { adminMetrics: (w) => { calls.push(w); return adminMetrics(w); } }; },
  });
  p.render(p.React.createElement(p.win.MetricsView, { st }));
  return { ...p, calls };
}
const card = (p) => p.all('.qh-chan');
const rows = (p) => p.all('.qh-chan tbody tr[data-channel]');

test('one row per channel with requests, people and the share the server sent', jsdomOpts, async () => {
  const p = mount(() => Promise.resolve(payload()));
  await p.flush();
  assert.equal(p.calls[0], '30d', '30 days is the default');
  assert.deepEqual(rows(p).map(r => r.getAttribute('data-channel')), ['slack', 'web', 'idp', 'carrier-pigeon']);
  const slack = rows(p)[0].textContent;
  assert.match(slack, /Slack/);
  assert.match(slack, /30/);
  assert.match(slack, /30%/);
  assert.match(slack, /8/);
  assert.match(slack, /80%/);
  const total = p.doc.querySelector('.qh-chantotal').textContent;
  assert.match(total, /All channels/);
  assert.match(total, /100/);
  assert.match(total, /10/);
  assert.match(p.doc.querySelector('.qh-chan').textContent, /Channel mix · Last 30 days/);
});

test('the view prints the share it was given and recomputes nothing', jsdomOpts, async () => {
  const odd = { ...MIX, channels: [{ key: 'web', label: 'Web', requests: 60, requestShare: 33.3, users: 6, userShare: 44.4 }] };
  const p = mount(() => Promise.resolve(payload({ channelMix: odd })));
  await p.flush();
  const t = rows(p)[0].textContent;
  assert.match(t, /33\.3%/);
  assert.match(t, /44\.4%/);
});

test('an unknown channel shows as itself, with the neutral tone', jsdomOpts, async () => {
  const p = mount(() => Promise.resolve(payload()));
  await p.flush();
  const pigeon = rows(p)[3];
  assert.match(pigeon.textContent, /carrier-pigeon/);
  assert.ok(pigeon.querySelector('.seg-chan-other'));
  assert.ok(rows(p)[0].querySelector('.seg-chan-slack'));
  assert.ok(rows(p)[1].querySelector('.seg-chan-web'));
  assert.ok(rows(p)[2].querySelector('.seg-chan-idp'));
});

test('people are grouped by the channels they used, and one person is singular', jsdomOpts, async () => {
  const p = mount(() => Promise.resolve(payload()));
  await p.flush();
  const text = p.doc.querySelector('.qh-chan').textContent;
  assert.match(text, /People by the channels they used/);
  assert.match(text, /Slack \+ Web5 people/);
  assert.match(text, /Slack \+ Web \+ IdP1 person/);
  assert.match(text, /counts once in each channel row and once in the total/);
});

test('the weekly stack has one column per week and one segment per channel', jsdomOpts, async () => {
  const p = mount(() => Promise.resolve(payload()));
  await p.flush();
  const cols = p.all('.qh-chan .qh-stackcol');
  assert.equal(cols.length, 2);
  assert.equal(cols[1].querySelectorAll('.qh-stackseg').length, 4);
  assert.match(cols[1].getAttribute('title'), /2026-W43 · 59 requests/);
  assert.match(cols[1].getAttribute('title'), /IdP 9 \(1 person\)/);
  assert.match(cols[1].getAttribute('title'), /Slack 20 \(7 people\)/);
  assert.equal(cols[1].querySelector('.qh-stackseg.seg-chan-idp').style.flexGrow, '9');
  assert.equal(p.all('.qh-chan .qh-legend .lg').length, 4, 'one legend entry per channel');
});

test('a window switch asks again and never shows the other window\'s numbers', jsdomOpts, async () => {
  const week = { ...MIX, totalRequests: 7, channels: [{ key: 'web', label: 'Web', requests: 7, requestShare: 100, users: 2, userShare: 100 }], segments: [] };
  let release;
  const p = mount((w) => w === '7d'
    ? new Promise(res => { release = () => res(payload({ channelMix: week, channelWeekly: [] })); })
    : Promise.resolve(payload()), { metrics: { headline: { total: 100 } } });
  await p.flush();
  assert.ok(p.doc.querySelector('.qh-kpi-grid'), 'the full screen is on, not only the loading shell');
  const tab = p.all('.qh-mwin [role="tab"]').find(b => b.textContent === 'Last 7 days');
  p.click(tab);
  assert.deepEqual(p.calls, ['30d', '7d']);
  const busy = p.doc.querySelector('.qh-chan[aria-busy="true"]');
  assert.ok(busy, 'the card says it is loading');
  assert.match(busy.textContent, /Channel mix · Last 7 days/);
  assert.equal(rows(p).length, 0, 'no 30-day row sits under a 7-day title');
  release();
  await p.flush();
  assert.equal(rows(p).length, 1);
  assert.match(card(p)[0].textContent, /Channel mix · Last 7 days/);
});

test('no channel, a failed read and an old server each say so', jsdomOpts, async () => {
  const empty = mount(() => Promise.resolve(payload({ channelMix: { totalRequests: 0, totalUsers: 0, channels: [], segments: [] }, channelWeekly: [] })));
  await empty.flush();
  assert.match(card(empty)[0].textContent, /No request came in during this window\./);
  const bad = mount(() => Promise.reject(new Error('x')));
  await bad.flush();
  assert.match(bad.doc.querySelector('.qh-chan[role="alert"]').textContent, /Could not load the channel mix/);
  const old = mount(() => Promise.resolve({ headline: { total: 1 } }));
  await old.flush();
  assert.match(old.doc.querySelector('.qh-chan[role="alert"]').textContent, /Could not load the channel mix/,
    'a server without channelMix is an error, not an empty table');
});

test('the mock answers both windows with the same shape, and the CSS uses tokens', () => {
  const mock = web('qh-api-mock.jsx');
  assert.match(mock, /channelMix: \{\s*totalRequests/);
  assert.match(mock, /channelWeekly: vol\.map/);
  assert.match(mock, /const chanDays = w7/);
  const html = web('QueryHub.html');
  for (const k of ['slack', 'web', 'idp', 'mcp', 'other']) {
    const m = html.match(new RegExp('\\.seg-chan-' + k + ' \\{ --seg: ([^;]+);'));
    assert.ok(m, 'no tone for ' + k);
    assert.match(m[1], /^var\(--(sup-[a-z]+-medium|fg-tertiary)\)$/, k + ' uses a token, not a raw colour');
  }
});
