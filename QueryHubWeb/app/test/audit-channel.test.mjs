// Audit page: the channel of a request and its decision facts (2026-10-26).
// The rail (AudDetail) names the door the request came through (Slack, Web, IdP, MCP),
// who decided it, when, and the note. The list row carries a small pill with the channel.
import test from 'node:test';
import assert from 'node:assert/strict';

import { page, web, jsdomOpts } from './_mount.mjs';

// TierBadge lives in qh-panels.jsx (which needs the files before it) and qhDurMs in
// qh-admin-insights.jsx: the rail reads both.
const FILES = ['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-results-tools.jsx',
  'qh-panels.jsx', 'qh-admin-data.jsx', 'qh-admin-insights.jsx', 'qh-admin-audit.jsx'];

function req(extra = {}) {
  return { id: 7745, requester: 'Casey Lin', requesterId: 'U1', connection: 'prod-main', database: 'payments',
    tier: 'RW', status: 'completed', error: null, rows: 12, durationMs: 340, sql: 'select 1',
    origin: 'idp', channel: 'IdP', submittedAt: '2026-10-26T09:00:00Z', decidedAt: '2026-10-26T09:05:00Z',
    decidedBy: 'Jordan Ray', decidedById: 'U7', decisionNote: 'Plan looks fine.', ...extra };
}

function row(request) {
  return { id: 3, time: '2026-10-26T09:06:00Z', actor: { handle: 'U7', name: 'Jordan Ray', kind: 'person', via: null },
    action: 'completed', actionLabel: 'Request completed', category: 'requests', effect: 'ran', classified: true,
    outcome: null, target: 'prod-main/payments', detail: {}, request };
}

function rail(request) {
  const p = page(FILES, { url: 'https://localhost/#admin/audit' });
  p.render(p.React.createElement(p.win.AudDetail, { row: row(request), onClose: () => {}, pushToast: () => {} }));
  const text = () => p.doc.querySelector('.qh-auddetail').textContent;
  const dd = (label) => {
    const dt = p.all('dt').find(n => n.textContent === label);
    return dt ? dt.parentElement.querySelector('dd') : null;
  };
  return { p, text, dd };
}

for (const [origin, channel] of [['slack', 'Slack'], ['web', 'Web'], ['idp', 'IdP'], ['mcp', 'MCP']]) {
  test('the rail names the channel: ' + channel, jsdomOpts, () => {
    const { dd } = rail(req({ origin, channel }));
    const c = dd('Channel');
    assert.ok(c, 'no Channel row');
    assert.equal(c.textContent, channel);
    assert.equal(c.getAttribute('data-channel'), origin);
  });
}

test('the rail shows who decided, when, and the note', jsdomOpts, () => {
  const { dd } = rail(req());
  assert.ok(dd('Decided by').textContent.includes('Jordan Ray'));
  assert.ok(/ · /.test(dd('Decided by').textContent), 'the decision time follows the name');
  assert.equal(dd('Decision note').textContent, 'Plan looks fine.');
  assert.ok(dd('Submitted'), 'the submit time shows');
});

test('the rail hides the decision rows of an undecided request', jsdomOpts, () => {
  const { dd } = rail(req({ decidedBy: null, decidedById: null, decidedAt: null, decisionNote: null }));
  assert.equal(dd('Decided by'), null);
  assert.equal(dd('Decision note'), null);
});

test('a decider without a decision time shows the name alone', jsdomOpts, () => {
  const { dd } = rail(req({ decidedAt: null }));
  assert.equal(dd('Decided by').textContent, 'Jordan Ray');
});

test('a request with no channel field shows no Channel row', jsdomOpts, () => {
  const { dd } = rail(req({ origin: undefined, channel: undefined }));
  assert.equal(dd('Channel'), null);
});

test('a row with no request shows no channel and no decision rows', jsdomOpts, () => {
  const { dd, text } = rail(null);
  assert.equal(dd('Channel'), null);
  assert.equal(dd('Decided by'), null);
  assert.ok(text().includes('No request behind this one'));
});

test('the list row carries a channel pill, and the style exists', () => {
  const src = web('qh-admin-audit.jsx');
  assert.match(src, /r\.request && r\.request\.channel && <span className="qh-audpill is-chan"/);
  assert.match(src, /title=\{'This request came in through ' \+ r\.request\.channel\}>\{r\.request\.channel\}<\/span>/,
    'the pill reads the display name, not the stored value');
  const css = web('QueryHub.html');
  assert.match(css, /\.qh-audpill\.is-chan \{[^}]*background:/);
});

test('the mock sends every field the rail reads, for all four channels', () => {
  const src = web('qh-api-mock.jsx');
  for (const k of ['origin', 'channel', 'submittedAt', 'decidedAt', 'decidedBy', 'decidedById', 'decisionNote']) {
    assert.ok(src.includes(k), k + ' is missing in the mock');
  }
  for (const c of ["'Slack'", "'Web'", "'IdP'", "'MCP'"]) assert.ok(src.includes(c), c);
});
