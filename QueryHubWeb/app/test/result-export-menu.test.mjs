// More ways to download a result (2026-10-25): the Export menu, the URL builder, and the
// download helper that shows a refusal (413) instead of saving it as a file.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, lstatSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import { bareWindow, loadInto } from './_load.mjs';
import { page, web, jsdomOpts as opts } from './_mount.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, '..', 'src');
const FILES = ['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-results-tools.jsx', 'qh-panels.jsx'];

const api = loadInto(bareWindow(), 'qh-api.jsx');
const dl = loadInto(bareWindow(), 'qh-result-download.jsx');

test('resultFormatUrl builds the address of each shape, with the statement when there is one', () => {
  const base = api.qhApi.resultCsvUrl(7).replace(/\/queries\/7\/result\.csv$/, '');
  const url = api.qhApi.resultFormatUrl;
  assert.equal(url(7, 'json'), base + '/queries/7/result.json');
  assert.equal(url(7, 'md'), base + '/queries/7/result.md');
  assert.equal(url(7, 'sql'), base + '/queries/7/result.sql');
  assert.equal(url(7, 'csv-bom'), base + '/queries/7/result.csv?bom=1');
  assert.equal(url(7, 'json', 2), base + '/queries/7/result.json?statement=2');
  assert.equal(url(7, 'csv-bom', 2), base + '/queries/7/result.csv?bom=1&statement=2');
});

test('the format table says which shape is a plain link and which goes through fetch', () => {
  const f = dl.QH_RESULT_FORMATS;
  assert.deepEqual(Object.keys(f).sort(), ['csv-bom', 'json', 'md', 'sql']);
  assert.deepEqual(['csv-bom', 'json', 'md', 'sql'].map(k => f[k].ext), ['csv', 'json', 'md', 'sql']);
  assert.equal(f['csv-bom'].viaLink, true);
  assert.deepEqual(['json', 'md', 'sql'].map(k => f[k].viaLink), [false, false, false]);
});

function fakeEnv(res) {
  const made = [], revoked = [], timers = [], fetched = [];
  const anchor = { clicks: 0, click() { this.clicks += 1; } };
  return {
    made, revoked, timers, fetched, anchor,
    env: {
      fetch: async (url, opts2) => { fetched.push([url, opts2]); if (res instanceof Error) throw res; return res; },
      document: { createElement: (t) => { made.push(t); return anchor; } },
      URL: { createObjectURL: () => 'blob:one', revokeObjectURL: (u) => revoked.push(u) },
      setTimeout: (fn, ms) => timers.push([fn, ms]),
    },
  };
}

test('a good answer becomes a file with the given name, and the address is released later', async () => {
  const f = fakeEnv({ ok: true, blob: async () => 'BLOB' });
  const r = await dl.qhDownloadResultFile('/api/queries/7/result.json', 'queryhub_result_7.json', f.env);
  assert.deepEqual(r, { ok: true });
  assert.deepEqual(f.fetched, [['/api/queries/7/result.json', { credentials: 'same-origin' }]]);
  assert.deepEqual(f.made, ['a']);
  assert.equal(f.anchor.download, 'queryhub_result_7.json');
  assert.equal(f.anchor.rel, 'noopener');
  assert.equal(f.anchor.href, 'blob:one');
  assert.equal(f.anchor.clicks, 1);
  assert.equal(f.timers.length, 1);
  assert.equal(f.timers[0][1], 10000);
  assert.deepEqual(f.revoked, []);
  f.timers[0][0]();
  assert.deepEqual(f.revoked, ['blob:one']);
});

test('a refusal shows the server message and saves no file', async () => {
  const msg = 'This result has more than 100,000 rows. Download it as CSV instead.';
  const f = fakeEnv({ ok: false, status: 413, json: async () => ({ error: { code: 'too_large', message: msg } }) });
  const r = await dl.qhDownloadResultFile('/x', 'x.json', f.env);
  assert.deepEqual(r, { ok: false, message: msg });
  assert.deepEqual(f.made, [], 'no link was made');
  assert.equal(f.anchor.clicks, 0);
});

test('an error that is not JSON, and a network failure, each get a plain message', async () => {
  const a = fakeEnv({ ok: false, status: 502, json: async () => { throw new Error('not json'); } });
  assert.deepEqual(await dl.qhDownloadResultFile('/x', 'x.md', a.env), { ok: false, message: 'The download failed (502).' });
  const b = fakeEnv(new Error('offline'));
  assert.deepEqual(await dl.qhDownloadResultFile('/x', 'x.md', b.env), { ok: false, message: 'The download failed. Check your connection.' });
  assert.equal(b.anchor.clicks, 0);
});

test('the Export menu lists the four new shapes and each one calls onExport with its own name', opts, () => {
  const ui = page(FILES);
  const rows = [{ a: '1' }, { a: '2' }];
  const result = { kind: 'table', cols: ['a'], piiCols: [], rows, total: 2, truncated: false, statement: 1, statementCount: 1,
                   slice: (o, c) => rows.slice(o, o + c), fetchPage: async () => [] };
  const calls = [];
  const base = { tab: 'results', setTab: () => {}, result, messages: [], audit: [], runMs: 5, onExport: (f) => calls.push(f), plan: null,
                 onToast: () => {}, colMeta: {}, reqId: '7', unmasked: false, conn: null, onStatement: () => {}, ranOn: null, status: 'completed' };
  ui.render(ui.React.createElement(ui.win.ResultsPanel, base));
  ui.click(ui.doc.querySelector('.qh-export > button'));
  const items = ui.all('.qh-export-menu button');
  assert.deepEqual(items.map(b => b.textContent), [
    'Download CSV (.csv)', 'Download Excel (.xls)', 'Download CSV for Excel (.csv)', 'Download JSON (.json)',
    'Download Markdown table (.md)', 'Download SQL INSERT (.sql)', 'Copy all as CSV', 'Copy all as TSV']);
  for (const label of ['Download CSV for Excel (.csv)', 'Download JSON (.json)', 'Download Markdown table (.md)', 'Download SQL INSERT (.sql)']) {
    if (!ui.doc.querySelector('.qh-export-menu')) ui.click(ui.doc.querySelector('.qh-export > button'));
    ui.click(ui.all('.qh-export-menu button').find(b => b.textContent === label));
    assert.equal(ui.doc.querySelector('.qh-export-menu'), null, label + ' closes the menu');
  }
  assert.deepEqual(calls, ['csv-bom', 'json', 'md', 'sql']);
});

// The panel sits at the bottom of the window, so the long menu has to open upward there.
function menuAt(top, bottom, innerHeight) {
  const ui = page(FILES);
  const rows = [{ a: '1' }];
  const result = { kind: 'table', cols: ['a'], piiCols: [], rows, total: 1, truncated: false, statement: 1, statementCount: 1,
                   slice: (o, c) => rows.slice(o, o + c), fetchPage: async () => [] };
  const base = { tab: 'results', setTab: () => {}, result, messages: [], audit: [], runMs: 5, onExport: () => {}, plan: null,
                 onToast: () => {}, colMeta: {}, reqId: '7', unmasked: false, conn: null, onStatement: () => {}, ranOn: null, status: 'completed' };
  ui.render(ui.React.createElement(ui.win.ResultsPanel, base));
  Object.defineProperty(ui.win, 'innerHeight', { value: innerHeight, configurable: true });
  ui.doc.querySelector('.qh-export').getBoundingClientRect = () => ({ top, bottom, left: 0, right: 0, width: 0, height: bottom - top });
  ui.click(ui.doc.querySelector('.qh-export > button'));
  return ui.doc.querySelector('.qh-export-menu');
}

test('the Export menu opens upward when the room below is short, and scrolls when neither side fits it', opts, () => {
  const low = menuAt(800, 830, 900);
  assert.ok(low.className.includes('is-up'), 'short room below, long room above: up');
  assert.equal(low.style.maxHeight, '788px');
  const high = menuAt(100, 130, 900);
  assert.ok(!high.className.includes('is-up'), 'long room below: down');
  assert.equal(high.style.maxHeight, '758px');
  const tight = menuAt(120, 150, 300);
  assert.ok(!tight.className.includes('is-up'), 'more room below than above: down');
  assert.equal(tight.style.maxHeight, '160px');
  assert.ok(!menuAt(500, 560, 900).className.includes('is-up'), '340 below is enough room');
  assert.ok(menuAt(500, 561, 900).className.includes('is-up'), '339 below is not enough room');
});

test('the menu style keeps one item per line and has an upward form', () => {
  const css = web('QueryHub.html');
  const rule = (sel) => { const m = css.match(new RegExp(sel.replace(/[.]/g, '\\.') + ' \\{([^}]*)\\}')); return m ? m[1] : ''; };
  assert.match(rule('.qh-export-menu button'), /display: block;/, 'nowrap would line inline buttons up in a row');
  assert.match(rule('.qh-export-menu'), /white-space: nowrap;/);
  assert.match(rule('.qh-export-menu'), /overflow-y: auto;/);
  assert.match(rule('.qh-export-menu.is-up'), /top: auto;\s*bottom: 100%;/);
});

test('the app turns the new names into a download, and the new files are wired into the build', () => {
  const app = web('qh-app.jsx');
  assert.match(app, /const shape = QH_RESULT_FORMATS\[format\];/);
  assert.match(app, /qhApi\.resultFormatUrl\(tab\.qid, format, stN\)/);
  assert.match(app, /qhDownloadResultFile\(url, name\)/);
  const main = readFileSync(resolve(SRC, 'main.jsx'), 'utf8');
  const html = web('QueryHub.html');
  for (const f of ['qh-result-download.jsx', 'qh-results-tools.jsx']) {
    assert.ok(lstatSync(resolve(SRC, f)).isSymbolicLink(), f + ' has a symlink in app/src');
    assert.ok(main.includes("import './" + f + "';"), f + ' is imported in main.jsx');
    assert.ok(html.includes('<script type="text/babel" src="' + f + '"></script>'), f + ' has a script tag');
  }
});
