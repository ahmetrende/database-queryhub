// Audit insight callout (2026-10-24): one calm line per insight above the audit list.
// It shows nothing while loading, on failure and for an empty list.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { transformSync } from 'esbuild';

import { bareWindow, loadInto } from './_load.mjs';
import { page, web, tokensCss, isVendorTokens, jsdomOpts } from './_mount.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const FILES = ['qh-data.jsx', 'qh-admin-audit.jsx'];

const RO = { tier: 'RO', total: 500, ruleId: 'ag:1', ruleLabel: 'Jordan Ray on prod-main (RO)', count: 310, percent: 62 };
const RW = { tier: 'RW', total: 40, ruleId: 'ag:2', ruleLabel: 'Casey Lin on billing-main (RW)', count: 30, percent: 75 };

async function mountInsight(api) {
  const p = page(FILES, { url: 'https://localhost/#admin/audit', prep: (win) => { win.qhApi = { adminAuditInsight: api }; } });
  p.render(p.React.createElement(p.win.AuditInsight));
  await p.flush();
  await p.flush();
  return p;
}

test('the callout says the exact sentence for RO', jsdomOpts, async () => {
  const p = await mountInsight(() => Promise.resolve({ windowDays: 30, insights: [RO] }));
  assert.equal(p.all('.qh-audinsight-line').length, 1);
  assert.equal(p.all('.qh-audinsight-line')[0].textContent,
    'One rule produced 62% of the RO queries that QueryHub approved by itself in the last 30 days: Jordan Ray on prod-main (RO), 310 of 500.');
});

test('the callout says write queries for RW and shows one line per insight', jsdomOpts, async () => {
  const p = await mountInsight(() => Promise.resolve({ windowDays: 30, insights: [RO, RW] }));
  const lines = p.all('.qh-audinsight-line').map(n => n.textContent);
  assert.equal(lines.length, 2);
  assert.equal(lines[1],
    'One rule produced 75% of the write queries that QueryHub approved by itself in the last 30 days: Casey Lin on billing-main (RW), 30 of 40.');
});

test('an empty list renders nothing, not an empty box', jsdomOpts, async () => {
  const p = await mountInsight(() => Promise.resolve({ windowDays: 30, insights: [] }));
  assert.equal(p.all('.qh-audinsight').length, 0);
  assert.equal(p.doc.getElementById('root').innerHTML, '');
});

test('a failed call renders nothing, for a rejection and for a throw', jsdomOpts, async () => {
  const a = await mountInsight(() => Promise.reject(new Error('boom')));
  assert.equal(a.doc.getElementById('root').innerHTML, '');
  const b = await mountInsight(() => { throw new Error('boom'); });
  assert.equal(b.doc.getElementById('root').innerHTML, '');
});

test('nothing shows while the call is pending', jsdomOpts, async () => {
  const p = await mountInsight(() => new Promise(() => {}));
  assert.equal(p.doc.getElementById('root').innerHTML, '');
});

test('Review rule opens the Auto-approve screen', jsdomOpts, async () => {
  const p = await mountInsight(() => Promise.resolve({ windowDays: 30, insights: [RO] }));
  const btn = p.all('.qh-audinsight button')[0];
  assert.equal(btn.textContent, 'Review rule');
  p.click(btn);
  assert.equal(p.win.location.hash, '#admin/auto');
});

test('the client calls the route and keeps unknown fields', async () => {
  const win = bareWindow();
  const calls = [];
  win.fetch = async (url, o) => { calls.push({ url, method: (o && o.method) || 'GET' });
    return { ok: true, status: 200, json: async () => ({ windowDays: 7, insights: [], extra: { a: 1 } }) }; };
  const r = await loadInto(win, 'qh-api.jsx').qhApi.adminAuditInsight();
  assert.equal(calls[0].url, '/api/admin/audit/insight');
  assert.equal(calls[0].method, 'GET');
  assert.deepEqual(r.extra, { a: 1 });
  assert.equal(r.windowDays, 7);
});

test('the mock covers both branches through the window flag', async () => {
  const win = bareWindow();
  win.setTimeout = (f) => { Promise.resolve().then(f); return 0; };
  win.clearTimeout = () => {};
  win.setInterval = () => 0;
  win.clearInterval = () => {};
  loadInto(win, 'qh-data.jsx');
  const src = readFileSync(resolve(HERE, '..', '..', 'qh-api-mock.jsx'), 'utf8');
  const code = transformSync(src, { loader: 'jsx', jsx: 'transform',
    jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment' }).code;
  new Function('window', 'with (window) {\n' + code + '\n}')(win);
  const full = await win.qhApi.adminAuditInsight();
  assert.equal(full.insights.length, 1);
  assert.equal(full.insights[0].ruleLabel, 'Jordan Ray on prod-main (RO)');
  assert.equal(full.insights[0].count, 310);
  assert.equal(full.insights[0].total, 500);
  assert.equal(full.insights[0].percent, 62);
  win.QH_MOCK_INSIGHT_EMPTY = true;
  assert.deepEqual((await win.qhApi.adminAuditInsight()).insights, []);
});

// ---------- contrast ----------
const css = web('QueryHub.html');
const tokens = tokensCss();
function hexOf(name, theme) {
  const cut = tokens.indexOf("[data-theme='dark']");
  const dark = tokens.slice(cut);
  const light = tokens.slice(0, cut);
  const re = new RegExp(name + ':\\s*(#[0-9A-Fa-f]{6})');
  const m = theme === 'dark' ? dark.match(re) || light.match(re) : light.match(re);
  assert.ok(m, name + ' in ' + theme);
  return m[1];
}
const lin = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
const rgb = (h) => { const n = parseInt(h.slice(1), 16); return [n >> 16, (n >> 8) & 255, n & 255]; };
const lum = (c) => 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2]);
const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };

test('the callout text passes 4.5:1 on the amber tint in both themes', () => {
  const body = css.replace(/\/\*[\s\S]*?\*\//g, ' ').match(/\.qh-audinsight \{([^{}]*)\}/);
  assert.ok(body, 'a .qh-audinsight rule');
  assert.match(body[1], /background: var\(--warning-adaptive-light\)/);
  assert.match(body[1], /color: var\(--fg-primary\)/);
  assert.doesNotMatch(body[1], /#[0-9a-fA-F]{3,8}\b/);
  const a = Number((tokens.match(/--warning-adaptive-light:\s*rgba\(245,\s*158,\s*11,\s*([\d.]+)\)/) || [])[1]);
  assert.ok(a > 0 && a < 1, 'the tint alpha');
  const out = {};
  for (const theme of ['light', 'dark']) {
    const ground = rgb(hexOf('--bg-white', theme));
    const tint = [245, 158, 11].map((c, i) => Math.round(c * a + ground[i] * (1 - a)));
    out[theme] = ratio(rgb(hexOf('--fg-primary', theme)), tint);
    assert.ok(out[theme] >= 4.5, theme + ' ' + out[theme].toFixed(2));
  }
  if (isVendorTokens()) assert.ok(out.light >= 4.5 && out.dark >= 4.5);
});
