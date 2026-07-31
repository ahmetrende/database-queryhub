// QueryHub — DESIGN MOCK of the API client. NOT part of the product.
//
// ── FOR THE BACKEND / CODE SIDE ──────────────────────────────────────────────
// The shipped app talks to the real server through `qh-api.jsx` (CODE-owned:
// same-origin cookie session, single-flight /auth/refresh, API_BASE = '/api').
// This file is its stand-in for the DESIGN prototype, which runs with no
// backend at all: it implements the SAME `window.qhApi` surface, method for
// method, against in-memory mock data. Every UI file in this project is a
// verbatim copy of the repo's, so a design change here ports back as a plain
// file copy — the mock boundary is this file and nothing else.
//   * Do NOT port this file into the repo, and do not implement anything
//     against it. `qh-api.jsx` + the real endpoints stay the contract.
//   * When an endpoint's SHAPE changes on the server, change it here too and
//     the prototype keeps telling the truth about what the UI receives.
// Anything faked (latency, approval timing, connection probes, CSV export) is
// marked MOCK below.
// ────────────────────────────────────────────────────────────────────────────

const API_BASE = '/api';
window.QH_MOCK = true;            // read by qh-app to skip the result WebSocket

// Build stamp: in production `qh-version.js` injects window.QH_BUILD from git
// HEAD (see the FastAPI `/` route). Here we set a plausible one so the profile
// menu / What's-new header / commit links render.
window.QH_BUILD = { version: 'v0.1.0', date: '2026-07-28 19:40', sha: 'bd9dc57', branch: 'main', repo: 'ahmetrende/queryhub' };

const MOCK_LATENCY = 140;         // MOCK: pretend the network exists
const mockDelay = (v, ms) => new Promise(res => setTimeout(() => res(typeof v === 'function' ? v() : v), ms == null ? MOCK_LATENCY : ms));
const mockFail = (message, status, code) => { const e = new Error(message); e.status = status || 400; e.code = code || 'bad_request'; return Promise.reject(e); };
const isoNow = () => new Date().toISOString();
const isoAgo = (ms) => new Date(Date.now() - ms).toISOString();
const isoIn = (ms) => new Date(Date.now() + ms).toISOString();
const clock = () => new Date().toLocaleTimeString('en-GB', { hour12: false });
let MOCK_SEQ = 900;
const mockId = (p) => p + '_' + (++MOCK_SEQ);
// MOCK of the server's request ids: bare-numeric, so a number reserved for a tab
// reads the same in the tab chip, the results header and the audit log.
let MOCK_QID = 1993;
const nextQid = () => String(++MOCK_QID);

// ---------- Session (MOCK: a localStorage flag stands in for the cookie) ----------
const MOCK_SESSION_KEY = 'qh.mock.session.v1';
const MOCK_USERS = {
  'dana.kaur':  { slackId: 'U04AB12CD', name: 'Dana Kaur', initials: 'DK', role: 'super' },
  'ben.donnelly': { slackId: 'U07BD', name: 'Ben Donnelly', initials: 'BD', role: 'developer' },
  'amara.osei': { slackId: 'U07AZ', name: 'Amara Osei', initials: 'AO', role: 'dba' },
};
function mockSession() {
  try { const s = JSON.parse(localStorage.getItem(MOCK_SESSION_KEY) || 'null'); if (s && s.handle) return s; } catch (e) {}
  return null;
}
function mockSignIn(handle, role) {
  const h = MOCK_USERS[handle] ? handle : 'dana.kaur';
  try { localStorage.setItem(MOCK_SESSION_KEY, JSON.stringify({ handle: h, role: role || MOCK_USERS[h].role, at: Date.now() })); } catch (e) {}
}
function mockSignOut() { try { localStorage.removeItem(MOCK_SESSION_KEY); } catch (e) {} }
function mockUser() {
  const s = mockSession();
  if (!s) return null;
  const base = MOCK_USERS[s.handle] || MOCK_USERS['dana.kaur'];
  const brand = window.qhBrand ? window.qhBrand() : { org: 'Acme' };
  return {
    id: base.slackId, slackId: base.slackId, handle: s.handle, name: base.name,
    initials: base.initials, role: s.role || base.role, team: brand.org,
    avatar: window.qhMockAvatar ? window.qhMockAvatar(base.initials) : null,
    mustChangePassword: false,
  };
}

// ---------- Developer-side mock state ----------
const MOCK = {
  savedSrv: [],                   // server-synced saved queries (POST /saved)
  sessionsSrv: [],                // server-synced named workspaces
  scheduled: [],                  // real scheduled queries
  requests: {},                   // qid -> lifecycle record
  drafts: {},                     // reserved-but-unsubmitted ids (POST /queries/draft)
  notifRead: [],
};

// MOCK: notification feed. Prod = GET /notifications (+ realtime push).
const MOCK_NOTIFICATIONS = [
  { id: 'n1', kind: 'approved', title: 'Query approved', body: 'dba.amara approved your RW query on prod-main/payments — 42 rows updated.', createdAt: isoAgo(1000 * 60 * 4) },
  { id: 'n2', kind: 'endpoint', title: 'Access granted', body: 'RO access to prod-replica/analytics is live. You can query it now.', createdAt: isoAgo(1000 * 60 * 52) },
  { id: 'n3', kind: 'scheduled', title: 'Scheduled query ran', body: '“Daily signup funnel” completed at 02:00 — 1,284 rows ready.', createdAt: isoAgo(1000 * 60 * 60 * 9) },
  { id: 'n4', kind: 'rejected', title: 'Query rejected', body: 'dba.marco asked for a WHERE clause before running the invoices cleanup.', createdAt: isoAgo(1000 * 60 * 60 * 26) },
  { id: 'n5', kind: 'kill', title: 'Kill switch released', body: 'Fleet-wide pause lifted — query traffic resumed.', createdAt: isoAgo(1000 * 60 * 60 * 30) },
];

// MOCK: curated changelog. Prod = GET /changelog (the hand-written entries file).
const MOCK_CHANGELOG = [
  { version: 'v0.1.0', date: '2026-07-28', sha: 'bd9dc57', area: 'Editor', headline: 'Open and save .sql files, and stop a running query',
    summary: 'The editor now round-trips with your filesystem, and a query that is still running can be stopped from the action bar.',
    changes: [
      { type: 'new', text: 'Open .sql / Download .sql next to New query in the sidebar, plus Download from a tab’s context menu.' },
      { type: 'new', text: 'Stop button while a query runs — the server signals the database and escalates if the cancel does not land.' },
      { type: 'improved', text: 'Autocomplete quotes identifiers per engine and knows the active target’s schema.' },
    ],
    commits: [{ sha: 'bd9dc57', msg: 'add local .sql import/export' }, { sha: '7c1a904', msg: 'add cancel for running queries' }] },
  { version: 'v0.1.0', date: '2026-07-26', sha: '4af0c1e', area: 'Sign-in', headline: 'Sign in without Slack',
    summary: 'Deployments that do not use Slack can now use built-in local accounts, with a self-service password change.',
    changes: [
      { type: 'new', text: 'Username / password sign-in when local accounts are enabled.' },
      { type: 'new', text: 'Change password from the profile menu; handed-off accounts are asked to set one on first sign-in.' },
      { type: 'changed', text: 'The sign-in screen shows only the methods your deployment has enabled.' },
    ],
    commits: [{ sha: '4af0c1e', msg: 'add local account login + change password' }] },
  { version: 'v0.1.0', date: '2026-07-24', sha: '2b55ecc', area: 'Admin', headline: 'Connections are managed in the web panel',
    summary: 'Registering a target server, rotating its credentials and testing it no longer needs a shell.',
    changes: [
      { type: 'new', text: 'Add, edit, rotate, enable/disable and delete connections, with per-tier RO/RW/DDL credentials.' },
      { type: 'new', text: 'Test connection — one probe with the stored read-only credential, before anyone depends on it.' },
      { type: 'improved', text: 'A connection starts disabled until its credentials are set, so nothing half-registered goes live.' },
    ],
    commits: [{ sha: '2b55ecc', msg: 'add connection registry CRUD' }, { sha: 'e10b7d2', msg: 'add reachability probes' }] },
  { version: 'v0.1.0', date: '2026-07-22', sha: '9d3f118', area: 'Accessibility', headline: 'Every dialog works from the keyboard',
    summary: 'Modals announce themselves, take focus, close on Escape and keep Tab inside the panel.',
    changes: [
      { type: 'fixed', text: 'Escape closes a dialog; focus returns to whatever opened it.' },
      { type: 'fixed', text: 'Tab no longer walks out of a dialog into the page behind it.' },
      { type: 'improved', text: 'Fonts ship with the app — no third-party request on load, and the UI is intact offline.' },
    ],
    commits: [{ sha: '9d3f118', msg: 'add shared accessible modal shell' }, { sha: 'c0a71bb', msg: 'self-host webfonts' }] },
  { version: 'v0.1.0', date: '2026-07-19', sha: '5e8c2a0', area: 'Approvals', audience: 'approver', headline: 'Approving access creates the grant',
    summary: 'An approved access request now provisions the grant it asked for, instead of leaving the DBA to run it by hand.',
    changes: [
      { type: 'new', text: 'Approving a request writes the per-user grant (requester, target, database, requested tier).' },
      { type: 'improved', text: 'The approval queue refreshes on its own, so two admins cannot both work the same request.' },
    ],
    commits: [{ sha: '5e8c2a0', msg: 'auto-create grant on access-request approval' }] },
];

// ---------- Admin-side mock state (seeds moved here from the old mock hook) ----------
const MOCK_QUEUE = [
  { id: 'q_8f21', submitter: { name: 'Elena Silva', initials: 'ES', slackId: 'U07EF', trust: 92 },
    connectionId: 'prod-main', databaseId: 'payments', env: 'production', tier: 'RW',
    sql: "UPDATE payouts\nSET status = 'retry'\nWHERE status = 'failed'\n  AND created_at::date = current_date;",
    statements: 1, piiCols: [], estRows: 42, estTables: ['payouts'], reason: 'Retrying today\'s failed payouts after the gateway fix.',
    submittedAt: isoAgo(1000 * 60 * 3) },
  { id: 'q_8f0e', submitter: { name: 'Ben Donnelly', initials: 'BD', slackId: 'U07BD', trust: 74 },
    connectionId: 'prod-main', databaseId: 'users', env: 'production', tier: 'RO',
    sql: "SELECT id, email, full_name, tckn, last_seen_at\nFROM users\nWHERE kyc_status = 'pending'\nORDER BY created_at DESC\nLIMIT 200;",
    statements: 1, piiCols: ['email', 'full_name', 'tckn'], estRows: 200, estTables: ['users'],
    reason: 'Compliance needs the pending-KYC list for the weekly review.', submittedAt: isoAgo(1000 * 60 * 11) },
  { id: 'q_8ef2', submitter: { name: 'Chen Yu', initials: 'CY', slackId: 'U07CY', trust: 61 },
    connectionId: 'prod-main', databaseId: 'analytics', env: 'production', tier: 'DDL',
    sql: "ALTER TABLE events\n  ADD COLUMN device_fingerprint text;",
    statements: 1, piiCols: [], estRows: 0, estTables: ['events'], reason: 'Need a column for the new anti-fraud signal.',
    submittedAt: isoAgo(1000 * 60 * 24), escalate: true },
  { id: 'q_8ec7', submitter: { name: 'Dana Kaur', initials: 'DK', slackId: 'U04AB12CD', trust: 88 },
    connectionId: 'prod-main', databaseId: 'payments', env: 'production', tier: 'RW', bundleId: 'bnd_2041',
    sql: "DELETE FROM invoices\nWHERE status = 'draft'\n  AND created_at < now() - interval '90 days';",
    statements: 1, piiCols: [], estRows: 1180, estTables: ['invoices'], reason: 'Cleaning up stale draft invoices per finance request.',
    submittedAt: isoAgo(1000 * 60 * 38) },
  { id: 'q_8e90', submitter: { name: 'Dana Kaur', initials: 'DK', slackId: 'U04AB12CD', trust: 88 },
    connectionId: 'prod-main', databaseId: 'users', env: 'production', tier: 'RW', bundleId: 'bnd_2041',
    sql: "UPDATE users SET status = 'active'\nWHERE id IN (84213, 84500, 84611);",
    statements: 1, piiCols: [], estRows: 3, estTables: ['users'], reason: 'Reactivating 3 accounts after manual verification.',
    submittedAt: isoAgo(1000 * 60 * 39) },
];

// Standing grants: one row per (subject, connection) with a database LIST
// (['*'] = all) and ONE tier. They never expire — only auto-approve is bounded.
const MOCK_GRANTS = [
  { id: 'g_1', subjectType: 'team', subject: 'data-eng', subjectName: 'data-eng', connectionId: 'prod-replica', databases: ['users_ro', 'analytics_ro'], tier: 'RO', grantedBy: 'dba.amara', grantedAt: isoAgo(1000 * 86400 * 40) },
  { id: 'g_2', subjectType: 'team', subject: 'backend', subjectName: 'backend', connectionId: 'staging', databases: ['app_stg'], tier: 'DDL', grantedBy: 'dba.amara', grantedAt: isoAgo(1000 * 86400 * 90) },
  { id: 'g_3', subjectType: 'team', subject: 'backend', subjectName: 'backend', connectionId: 'prod-replica', databases: ['users_ro'], tier: 'RO', grantedBy: 'dba.amara', grantedAt: isoAgo(1000 * 86400 * 15) },
  { id: 'g_4', subjectType: 'user', subject: 'elena.silva', connectionId: 'prod-main', databases: ['payments'], tier: 'RW', grantedBy: 'dba.marco', grantedAt: isoAgo(1000 * 86400 * 3) },
  { id: 'g_5', subjectType: 'user', subject: 'chen.yu', connectionId: 'prod-main', databases: ['analytics'], tier: 'DDL', grantedBy: 'dba.marco', grantedAt: isoAgo(1000 * 86400 * 5) },
  { id: 'g_6', subjectType: 'user', subject: 'ben.donnelly', connectionId: 'prod-main', databases: ['users'], tier: 'RO', grantedBy: 'dba.amara', grantedAt: isoAgo(1000 * 86400 * 20) },
  { id: 'g_7', subjectType: 'user', subject: 'maya.andersen', connectionId: 'prod-replica', databases: ['*'], tier: 'RO', grantedBy: 'dba.amara', grantedAt: isoAgo(1000 * 86400 * 8) },
];

const MOCK_AUTO = [
  { id: 'a_1', user: 'data-eng (team)', tier: 'RO', connectionId: 'prod-replica', databaseId: '*', maxRows: 1000, expiresAt: isoIn(1000 * 86400 * 30), createdBy: 'dba.amara' },
  { id: 'a_2', user: 'amara.osei', tier: 'RO', connectionId: 'prod-main', databaseId: 'users', maxRows: 500, expiresAt: isoIn(1000 * 86400 * 7), createdBy: 'dba.marco' },
  { id: 'a_3', user: 'backend (team)', tier: 'RW', connectionId: 'staging', databaseId: 'app_stg', maxRows: 5000, expiresAt: isoIn(1000 * 86400 * 14), createdBy: 'dba.amara' },
];

const MOCK_SCOPES = [
  { id: 's_1', admin: 'dba.amara', role: 'super', canApprove: ['RO', 'RW', 'DDL'], connections: ['*'] },
  { id: 's_2', admin: 'dba.marco', role: 'super', canApprove: ['RO', 'RW', 'DDL'], connections: ['*'] },
  { id: 's_3', admin: 'lead.clara', role: 'dba', canApprove: ['RO', 'RW'], connections: ['prod-main', 'prod-replica'] },
  { id: 's_4', admin: 'oncall.eli', role: 'dba', canApprove: ['RO'], connections: ['prod-replica'] },
];

const MOCK_PEOPLE = [
  { id: 'u_elif', handle: 'elena.silva', name: 'Elena Silva', initials: 'ES' },
  { id: 'u_aylin', handle: 'amara.osei', name: 'Amara Osei', initials: 'AO' },
  { id: 'u_burak', handle: 'ben.donnelly', name: 'Ben Donnelly', initials: 'BD' },
  { id: 'u_can', handle: 'chen.yu', name: 'Chen Yu', initials: 'CY' },
  { id: 'u_deniz', handle: 'dana.kaur', name: 'Dana Kaur', initials: 'DK' },
  { id: 'u_oguzhan', handle: 'omar.kane', name: 'Omar Kane', initials: 'OK' },
  { id: 'u_merve', handle: 'maya.andersen', name: 'Maya Andersen', initials: 'MA' },
  { id: 'u_kaan', handle: 'kai.yamada', name: 'Kai Yamada', initials: 'KY' },
  { id: 'u_ceyda', handle: 'clara.alvarez', name: 'Clara Alvarez', initials: 'CA' },
  { id: 'u_emre', handle: 'eli.kovac', name: 'Eli Kovac', initials: 'EK' },
  { id: 'u_mert', handle: 'marco.young', name: 'Marco Young', initials: 'MY' },
  { id: 'u_selin', handle: 'sofia.ahmed', name: 'Sofia Ahmed', initials: 'SA' },
];

// Teams do NOT nest — grants resolve through flat membership only.
const MOCK_TEAMS = [
  { id: 't_dataeng', name: 'data-eng', desc: 'Data engineering & analytics platform', members: ['amara.osei', 'chen.yu', 'omar.kane', 'maya.andersen'] },
  { id: 't_backend', name: 'backend', desc: 'Core backend services', members: ['ben.donnelly', 'kai.yamada', 'marco.young'] },
  { id: 't_payments', name: 'payments', desc: 'Payments & payouts', members: ['elena.silva', 'dana.kaur'] },
  { id: 't_compliance', name: 'compliance', desc: 'KYC, audit & regulatory', members: ['sofia.ahmed'] },
  { id: 't_growth', name: 'growth', desc: 'Growth & marketing analytics', members: [] },
  { id: 't_platform', name: 'platform', desc: 'Infra & DBA (super-admins)', members: ['amara.osei', 'marco.young'] },
];

const MOCK_ENDPOINT_REQS = [
  { id: 'er_31', server: 'prod-reporting-01', database: 'ledger', tier: 'RO', reason: 'Weekly revenue report needs read access to the ledger.', requester: 'ben.donnelly', requestedAt: isoAgo(1000 * 60 * 90), status: 'submitted' },
  { id: 'er_30', server: 'prod-main', database: 'referrals', tier: 'RW', reason: 'Fixing duplicated referral bonuses flagged by finance.', requester: 'elena.silva', requestedAt: isoAgo(1000 * 60 * 200), status: 'submitted' },
];

const MOCK_FEEDBACK = [
  { id: 'f_1', user: 'elena.silva', score: 5, comment: 'Approval came through in under a minute. Great.', queryId: 'q_7a10', when: isoAgo(1000 * 60 * 60 * 5) },
  { id: 'f_2', user: 'chen.yu', score: 2, comment: 'DDL escalation took too long, blocked a deploy.', queryId: 'q_79c2', when: isoAgo(1000 * 60 * 60 * 20) },
  { id: 'f_3', user: 'amara.osei', score: 4, comment: 'CSV export is handy. Would love saved query folders.', queryId: 'q_78ff', when: isoAgo(1000 * 60 * 60 * 30) },
  { id: 'f_4', user: 'ben.donnelly', score: 5, comment: 'PII masking just works, no more redaction by hand.', queryId: 'q_78a1', when: isoAgo(1000 * 60 * 60 * 46) },
];

const MOCK_AUDIT = [
  { id: 'aa_1', time: isoAgo(1000 * 60 * 4), actor: 'dba.amara', event: 'Approved query', target: 'elena.silva · prod-main/payments', kind: 'approve', requestId: '1987', tier: 'RW', rows: 42, durationMs: 1840, query: "UPDATE payouts SET status = 'retry' WHERE status = 'failed' AND created_at::date = current_date;" },
  { id: 'aa_2', time: isoAgo(1000 * 60 * 18), actor: 'dba.marco', event: 'Granted DDL', target: 'chen.yu → prod-main / analytics', kind: 'grant' },
  { id: 'aa_3', time: isoAgo(1000 * 60 * 42), actor: 'dba.marco', event: 'Rejected query', target: 'chen.yu · prod-main/analytics', kind: 'reject', requestId: '1981', tier: 'DDL', query: 'ALTER TABLE events ADD COLUMN device_fingerprint text;' },
  { id: 'aa_4', time: isoAgo(1000 * 60 * 66), actor: 'dba.amara', event: 'Created auto-approve grant', target: 'data-eng → prod-replica · RO', kind: 'auto' },
  { id: 'aa_5', time: isoAgo(1000 * 60 * 120), actor: 'system', event: 'Auto-approved', target: 'omar.kane · svc-prod-billing/billing_service', kind: 'auto', requestId: '1974', tier: 'RO', rows: 1, durationMs: 4100, query: 'SELECT * FROM billing_ledger WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1;' },
  { id: 'aa_6', time: isoAgo(1000 * 60 * 168), actor: 'system', event: 'Auto-approved', target: 'ben.donnelly · prod-replica/users_ro', kind: 'auto', requestId: '1968', tier: 'RO', rows: 200, durationMs: 320, query: 'SELECT id, email, kyc_status FROM users WHERE kyc_status = $1 LIMIT 200;' },
  { id: 'aa_7', time: isoAgo(1000 * 60 * 60 * 4), actor: 'dba.amara', event: 'Requested changes', target: 'dana.kaur · prod-main/invoices', kind: 'changes', requestId: '1952', tier: 'RW', query: "DELETE FROM invoices WHERE status = 'draft' AND created_at < now() - interval '90 days';" },
];

// Full /admin/metrics shape (from the p_metrics_* views).
const MOCK_METRICS = (function () {
  const WK = ['05-26', '06-02', '06-09', '06-16', '06-23', '06-30', '07-07', '07-14'];
  const vol = [
    { total: 386, completed: 331, failed: 18, rejected: 27, cancelled: 10, activeUsers: 41 },
    { total: 402, completed: 349, failed: 15, rejected: 29, cancelled: 9, activeUsers: 44 },
    { total: 371, completed: 322, failed: 12, rejected: 26, cancelled: 11, activeUsers: 43 },
    { total: 448, completed: 397, failed: 17, rejected: 24, cancelled: 10, activeUsers: 47 },
    { total: 421, completed: 372, failed: 14, rejected: 25, cancelled: 10, activeUsers: 46 },
    { total: 489, completed: 436, failed: 19, rejected: 23, cancelled: 11, activeUsers: 51 },
    { total: 512, completed: 461, failed: 16, rejected: 24, cancelled: 11, activeUsers: 53 },
    { total: 534, completed: 483, failed: 18, rejected: 21, cancelled: 12, activeUsers: 55 },
  ].map((w, i) => ({ period: WK[i], ...w }));
  const sched = [22, 26, 24, 31, 29, 38, 44, 49];
  const ratedN = [58, 64, 61, 73, 69, 81, 88, 96];
  const ratingAvg = [4.2, 4.3, 4.2, 4.4, 4.4, 4.5, 4.5, 4.6];
  const peakHours = Array.from({ length: 7 }, (_, d) => Array.from({ length: 24 }, (_, h) => {
    const weekend = d === 0 || d === 6;
    let base = (h >= 9 && h <= 18) ? (weekend ? 3 : 14) : (h >= 7 && h <= 21) ? (weekend ? 2 : 6) : (weekend ? 0 : 1);
    if (!weekend && (h === 10 || h === 15)) base += 6;
    if (!weekend && h === 13) base -= 3;
    return Math.max(0, base + ((d * 7 + h * 3) % 5) - 2);
  }));
  return {
    reportStart: '2026-05-26', timezone: 'Europe/Istanbul',
    headline: { total: 3563, completed: 3151, failed: 129, rejected: 199, cancelled: 84,
      successRate: 0.884, autoApproveRate: 0.66, uniqueUsers: 74, targetsTouched: 38,
      p50ApprovalSec: 41, p95ApprovalSec: 512, avgRating: 4.4, ratingCount: 612 },
    costSavings: { completed: 3151, dbaMinutesPerRequest: 12, dbaHourlyUsd: 65,
      dbaHoursSaved: 630, dbaSavingUsd: 40950, avoidedReplicas: 4, infraUsdPerMonth: 5200 },
    volumeWeekly: vol,
    approvalSla: { overall: { p50: 41, p75: 128, p90: 300, p95: 512, p99: 1180 } },
    tierTotals: { RO: 2317, RW: 1004, DDL: 242 },
    peakHours,
    topUsers: [
      { name: 'elena.silva', count: 421 }, { name: 'amara.osei', count: 366 },
      { name: 'ben.donnelly', count: 318 }, { name: 'chen.yu', count: 274 },
      { name: 'dana.kaur', count: 241 }, { name: 'omar.kane', count: 205 },
      { name: 'maya.andersen', count: 188 }, { name: 'kai.yamada', count: 152 },
    ],
    teamUsage: [
      { name: 'data-eng', count: 1284 }, { name: 'backend', count: 968 },
      { name: 'payments', count: 612 }, { name: 'compliance', count: 341 },
      { name: 'growth', count: 218 }, { name: 'platform', count: 140 },
    ],
    adminWorkload: [
      { name: 'dba.amara', count: 486 }, { name: 'dba.marco', count: 442 },
      { name: 'lead.clara', count: 173 }, { name: 'oncall.eli', count: 88 },
    ],
    targetUsage: [
      { name: 'prod-main/payments', count: 742 }, { name: 'prod-replica/users_ro', count: 688 },
      { name: 'prod-main/users', count: 531 }, { name: 'prod-replica/analytics_ro', count: 474 },
      { name: 'svc-prod-billing/billing_service', count: 402 }, { name: 'prod-main/analytics', count: 356 },
      { name: 'staging/app_stg', count: 214 }, { name: 'prod-main/invoices', count: 156 },
    ],
    scheduledUsage: WK.map((p, i) => ({ period: p, scheduled: sched[i], total: vol[i].total, pct: Math.round(sched[i] / vol[i].total * 100) })),
    ratingWeekly: WK.map((p, i) => ({ period: p, avg: ratingAvg[i], count: ratedN[i] })),
    ratingResponse: WK.map((p, i) => ({ period: p, rated: ratedN[i], completed: vol[i].completed, pct: Math.round(ratedN[i] / vol[i].completed * 100) })),
    ratingLow: [
      { user: 'chen.yu', rating: 2, feedback: 'DDL escalation took too long, blocked a deploy.', when: isoAgo(1000 * 60 * 60 * 20) },
      { user: 'kai.yamada', rating: 1, feedback: 'Query timed out at 30s with no clear way to raise the limit.', when: isoAgo(1000 * 60 * 60 * 52) },
      { user: 'maya.andersen', rating: 2, feedback: 'Masking hid a column I actually needed for the report.', when: isoAgo(1000 * 60 * 60 * 73) },
    ],
    csvSummary: { imports: 63, completed: 58, failed: 5, rowsLoaded: 2847213, successRate: 92 },
    csvImports: [
      { id: 'imp_501', when: isoAgo(1000 * 60 * 90), user: 'amara.osei', target: 'prod-main', db: 'analytics', table: 'campaign_costs', isNew: true, status: 'completed', rows: 12840, bytes: 2310000 },
      { id: 'imp_500', when: isoAgo(1000 * 60 * 60 * 5), user: 'dana.kaur', target: 'prod-main', db: 'payments', table: 'fx_rates', isNew: false, status: 'completed', rows: 384, bytes: 41000 },
      { id: 'imp_499', when: isoAgo(1000 * 60 * 60 * 9), user: 'ben.donnelly', target: 'staging', db: 'app_stg', table: 'test_users', isNew: true, status: 'failed', rows: 0, bytes: 0 },
      { id: 'imp_498', when: isoAgo(1000 * 60 * 60 * 26), user: 'elena.silva', target: 'prod-main', db: 'analytics', table: 'partner_dim', isNew: false, status: 'completed', rows: 5120, bytes: 890000 },
    ],
    whoCanWhat: [
      { Team: 'data-eng', RO: 24, RW: 6, DDL: 0 },
      { Team: 'backend', RO: 18, RW: 11, DDL: 3 },
      { Team: 'payments', RO: 9, RW: 7, DDL: 1 },
      { Team: 'compliance', RO: 7, RW: 0, DDL: 0 },
      { Team: 'platform (super)', RO: 5, RW: 5, DDL: 5 },
    ],
  };
})();

// Typed + grouped bot_config (GET /admin/config → PUT /admin/config).
const MOCK_CONFIG = {
  groups: [
    { id: 'approval', title: 'Approval & review', items: [
      { key: 'approval_ro_default', label: 'Read-only default', type: 'str', description: 'What happens to an RO query with no matching grant: auto | review.' },
      { key: 'approval_rw_default', label: 'Read-write default', type: 'str', description: 'RW carries a review unless a bounded auto-approve grant matches: auto | review.' },
      { key: 'ddl_always_review', label: 'DDL always reviewed', type: 'bool', description: 'Schema changes can never be auto-approved — enforced server-side.' },
      { key: 'approval_timeout_min', label: 'Approval timeout (min)', type: 'int', description: 'Pending queries auto-expire this long after submission with no decision.' },
      { key: 'auto_approve_max_rows', label: 'Auto-approve row ceiling', type: 'int', description: 'Hard cap on rows any auto-approve grant may return, regardless of the grant.' },
    ] },
    { id: 'execution', title: 'Execution limits', items: [
      { key: 'statement_timeout_sec', label: 'Statement timeout (sec)', type: 'int', description: 'Cancel any statement still running after this long.' },
      { key: 'max_rows_returned', label: 'Max rows returned', type: 'int', description: 'Upper bound on a result set before the grid truncates it.' },
      { key: 'default_page_size', label: 'Default page size', type: 'int', description: 'Rows fetched per page in the results grid.' },
      { key: 'export_row_cap', label: 'Export & copy cap', type: 'int', description: 'Ceiling on CSV / XLSX export and clipboard copy.' },
    ] },
    { id: 'pii', title: 'Data protection & PII', items: [
      { key: 'pii_mask_on_return', label: 'Mask PII on return', type: 'bool', description: 'Detected PII columns are masked in results and exports.' },
      { key: 'pii_strip_from_export', label: 'Strip PII from exports', type: 'bool', description: 'Remove PII columns entirely from CSV / XLSX rather than masking them.' },
      { key: 'pii_detection', label: 'Detection sensitivity', type: 'str', description: 'How aggressively the classifier flags columns as PII: strict | balanced | relaxed.' },
    ] },
    { id: 'slack', title: 'Slack integration', items: [
      { key: 'slack_workspace', label: 'Workspace', type: 'str', description: 'Connected Slack workspace. Blank disables Slack entirely (web-only approvals).' },
      { key: 'slack_approval_channel', label: 'Approval channel', type: 'str', description: 'Channel where new requests are posted for DBA review.' },
      { key: 'slack_slash_command', label: 'Slash command', type: 'str', description: 'Command developers type to submit a query from Slack.' },
    ] },
    { id: 'security', title: 'Security & sessions', items: [
      { key: 'session_ttl_hours', label: 'Session lifetime (hours)', type: 'int', description: 'How long a signed-in session stays valid before re-auth.' },
      { key: 'require_sso', label: 'Require SSO', type: 'bool', description: 'Only allow sign-in through the corporate identity provider.' },
      { key: 'ip_allowlist', label: 'IP allowlist', type: 'str', description: 'Restrict access to these CIDRs / addresses. Blank allows any.' },
    ] },
    { id: 'retention', title: 'Retention', items: [
      { key: 'audit_retention_days', label: 'Audit log retention (days)', type: 'int', description: 'Admin audit trail is kept at least this long (immutable).' },
      { key: 'history_retention_days', label: 'Query history retention (days)', type: 'int', description: 'Per-developer run history older than this is purged.' },
      { key: 'results_ttl_hours', label: 'Result cache TTL (hours)', type: 'int', description: 'How long a delivered result is kept before purge.' },
    ] },
    { id: 'web', title: 'Web UI', items: [
      { key: 'web_auth_slack_enabled', label: 'Slack sign-in enabled', type: 'bool', description: 'Allow signing in to the web via Slack OIDC.' },
      { key: 'web_auth_local_enabled', label: 'Local accounts enabled', type: 'bool', description: 'Allow built-in username / password accounts (vanilla profile).' },
      { key: 'web_base_url', label: 'Web base URL', type: 'str', description: 'Public base URL of the web app.' },
      { key: 'web_cookie_secure', label: 'Secure cookies', type: 'bool', description: 'Set the Secure flag on session cookies.' },
      { key: 'web_display_timezone', label: 'Display timezone', type: 'tz', description: 'Timezone the admin UI formats timestamps in.' },
    ] },
  ],
  values: {
    approval_ro_default: 'auto', approval_rw_default: 'review', ddl_always_review: 'on',
    approval_timeout_min: '120', auto_approve_max_rows: '5000',
    statement_timeout_sec: '30', max_rows_returned: '100000', default_page_size: '500', export_row_cap: '5000',
    pii_mask_on_return: 'on', pii_strip_from_export: 'on', pii_detection: 'balanced',
    slack_workspace: 'example.slack.com', slack_approval_channel: '#dba-approvals', slack_slash_command: '/sql',
    session_ttl_hours: '12', require_sso: 'on', ip_allowlist: '',
    audit_retention_days: '365', history_retention_days: '90', results_ttl_hours: '72',
    web_auth_slack_enabled: 'on', web_auth_local_enabled: 'on', web_base_url: 'https://queryhub.internal',
    web_cookie_secure: 'on', web_display_timezone: 'Europe/Istanbul',
  },
};

const ADMIN = {
  queue: MOCK_QUEUE.slice(), grants: MOCK_GRANTS.slice(), auto: MOCK_AUTO.slice(),
  scopes: MOCK_SCOPES.slice(), people: MOCK_PEOPLE.slice(), teams: MOCK_TEAMS.slice(),
  endpointReqs: MOCK_ENDPOINT_REQS.slice(), feedback: MOCK_FEEDBACK.slice(),
  audit: MOCK_AUDIT.slice(), config: JSON.parse(JSON.stringify(MOCK_CONFIG)),
  kill: { enabled: false, message: '', by: null, at: null },
  connections: null,   // built lazily from QH_CONNECTIONS (registry shape)
};

// `requestId` is the REQUEST behind the entry (null for grants, scopes,
// auto-approve windows and the kill switch — they belong to no request); `id`
// stays the audit row's own id, which nobody outside the table sees.
const mockAudit = (event, target, kind, extra) => {
  ADMIN.audit = [{ id: mockId('aa'), time: isoNow(), actor: 'dba.amara', event, target: target || '', kind: kind || 'scope', requestId: null, ...(extra || {}) }, ...ADMIN.audit];
};

// The registry rows the admin Connections screen edits. Passwords are never
// returned by the real API — only {username, configured, placeholder} per tier.
const DEFAULT_PORT = { postgres: 5432, mssql: 1433, oracle: 1521, mysql: 3306 };
function connRegistry() {
  if (ADMIN.connections) return ADMIN.connections;
  const engineOf = (c) => (window.qhEngineId ? window.qhEngineId(c.engine) : 'postgres');
  ADMIN.connections = (window.QH_CONNECTIONS || []).map(c => {
    const eid = engineOf(c);
    return {
      // MOCK: one retired target, kept in the fleet on purpose — its alias still
      // resolves for old saved queries and history, and the UI has to say it is
      // disabled rather than hide it.
      id: c.id, name: c.name, engine: c.engine, engineId: eid, env: c.env, enabled: c.id !== 'svc-prod-pricing',
      host: c.id.replace(/[^a-z0-9-]/g, '-') + '.db.internal', port: DEFAULT_PORT[eid] || 5432,
      defaultDatabase: (c.databases[0] || {}).name || 'postgres', notes: '',
      databases: c.databases.map(d => ({ id: d.id, name: d.name, tier: d.tier })),
      autoApproveRO: !!c.autoApproveRO,
      credentials: {
        ro: { username: 'qh_ro', configured: true, placeholder: false },
        rw: { username: 'qh_rw', configured: c.env !== 'production' || c.id === 'prod-main', placeholder: false },
        ddl: { username: 'qh_ddl', configured: c.id === 'staging' || c.id === 'prod-main', placeholder: c.id === 'prod-main' },
      },
    };
  });
  return ADMIN.connections;
}

// ---------- Query lifecycle (MOCK: a clock-driven state machine) ----------
const APPROVAL_MS = 4000;   // MOCK: how long a review "takes"
const RUN_MS = 1400;        // MOCK: execution time
const TIER_RANK = { RO: 0, RW: 1, DDL: 2 };

function targetOf(connId, dbId) {
  const conn = (window.QH_CONNECTIONS || []).find(c => c.id === connId) || null;
  const db = conn ? (conn.databases.find(d => d.id === dbId) || null) : null;
  return { conn, db };
}
function verdictFor(sql, connId, dbId) {
  const cl = window.qhClassify ? window.qhClassify(sql) : { tier: 'RO', statements: [] };
  const { conn, db } = targetOf(connId, dbId);
  const granted = (db && db.tier) || 'RO';
  const isSuper = !!(mockUser() && mockUser().role === 'super');
  const exceeds = !isSuper && (TIER_RANK[cl.tier] || 0) > (TIER_RANK[granted] || 0);
  const autoRO = window.qhAutoApproveRO ? window.qhAutoApproveRO(conn, db) : false;
  return {
    tier: cl.tier, statements: (cl.statements || []).length || 1,
    blocked: false, blockers: [], warnings: (window.qhRiskHints ? window.qhRiskHints(sql, cl) : []).filter(h => h.level !== 'low').map(h => h.text),
    grantedTier: granted, tierExceedsGrant: exceeds,
    willAutoApprove: isSuper || (cl.tier === 'RO' && autoRO),
    requiresJustification: cl.tier !== 'RO',
  };
}

function newRequest(body, bundleId) {
  const v = verdictFor(body.sql, body.connectionId, body.databaseId);
  const runAt = body.schedule && body.schedule.runAt ? new Date(body.schedule.runAt).getTime() : null;
  // A tab reserves its id when it opens; submitting claims that same number when
  // it is still the caller's to claim, and otherwise silently gets a fresh one —
  // which is why the client must read the id from the RESPONSE.
  const claimed = body.draftId && MOCK.drafts[body.draftId] ? String(body.draftId) : null;
  if (claimed) delete MOCK.drafts[claimed];
  const rec = {
    id: claimed || nextQid(), sql: body.sql, conn: body.connectionId, db: body.databaseId,
    tier: v.tier, auto: v.willAutoApprove, bundleId: bundleId || null,
    t0: Date.now(), scheduledFor: runAt ? new Date(runAt).toISOString() : null,
    rejected: /\bdrop\s+(table|database|schema)\b/i.test(body.sql),
  };
  MOCK.requests[rec.id] = rec;
  return rec;
}

// Derive the current status from the clock, so a tab that polls sees the
// request move pending → approved → running → done like the real one.
function statusOf(rec) {
  const el = Date.now() - rec.t0;
  const msg = [];
  const push = (kind, text) => msg.push({ kind, text, time: clock() });
  const audit = [];
  const aud = (actor, event) => audit.push({ actor, event, time: clock() });

  // Taken back by the requester. Withdrawn (never executed) and stopped
  // (statement killed mid-flight) are different facts, so the message says
  // which one happened; the status enum has no separate value for either.
  if (rec.stopped) {
    push(rec.stopped === 'withdrawn' ? 'info' : 'err',
      rec.stopped === 'withdrawn'
        ? 'Withdrawn by you — the request left the DBA queue without running.'
        : 'Stopped on the database — the statement was cancelled mid-run.');
    aud('you', rec.stopped === 'withdrawn' ? 'Withdrew request' : 'Stopped running query');
    return { status: 'failed', runMs: null, messages: msg, audit };
  }
  if (rec.scheduledFor && new Date(rec.scheduledFor).getTime() > Date.now()) {
    push('info', 'Scheduled for ' + new Date(rec.scheduledFor).toLocaleString() + ' — it will run without you.');
    aud('you', 'Scheduled ' + rec.tier + ' query');
    return { status: 'scheduled', runMs: null, messages: msg, audit, scheduledFor: rec.scheduledFor };
  }
  const approvedAt = rec.auto ? 0 : APPROVAL_MS;
  if (rec.rejected && el >= approvedAt) {
    push('err', 'Rejected by dba.amara — a DROP needs a migration, not an ad-hoc query.');
    aud('dba.amara', 'Rejected request');
    return { status: 'rejected', runMs: null, messages: msg, audit };
  }
  if (el < approvedAt) {
    push('info', 'Submitted — waiting for DBA review' + (rec.bundleId ? ' (batch ' + rec.bundleId + ')' : '') + '.');
    aud('you', 'Submitted ' + rec.tier + ' query');
    return { status: 'pending', runMs: null, messages: msg, audit };
  }
  aud('you', 'Submitted ' + rec.tier + ' query');
  if (rec.auto) push('ok', 'Auto-approved — read-only with a matching grant.');
  else { push('ok', 'Approved by dba.amara.'); aud('dba.amara', 'Approved request'); }
  if (el < approvedAt + RUN_MS) {
    push('info', 'Running on ' + rec.conn + '/' + rec.db + '…');
    return { status: 'running', runMs: null, messages: msg, audit };
  }
  const runMs = RUN_MS + (rec.id.length * 7 % 300);
  push('ok', 'Completed in ' + runMs + ' ms.');
  aud('executor', 'Ran with the ' + rec.tier + ' credential');
  return { status: 'done', runMs, messages: msg, audit };
}

// MOCK of the driver-reported column types (real API: the cursor description,
// so aliases, expressions and modifiers come out right). Name-based here, which
// is all the deterministic mock generator can honestly claim to know.
function mockColTypes(cols) {
  const t = {};
  cols.forEach(c => {
    const n = String(c).toLowerCase();
    if (/^count|_count$|^n_|^total$/.test(n)) t[c] = 'int8';
    else if (/(^|_)id$/.test(n)) t[c] = 'int8';
    else if (/_at$|^when$|_date$/.test(n)) t[c] = 'timestamptz';
    else if (/amount|balance|price|fee|rate|volume/.test(n)) t[c] = 'numeric(18,8)';
    else if (/email/.test(n)) t[c] = 'varchar(120)';
    else if (/phone|msisdn/.test(n)) t[c] = 'varchar(20)';
    else if (/^is_|^has_|enabled|active$/.test(n)) t[c] = 'bool';
    else if (/status|tier|kind|type$/.test(n)) t[c] = 'varchar(24)';
    else if (/uuid|token|hash/.test(n)) t[c] = 'uuid';
    else t[c] = 'text';
  });
  return t;
}

function resultFor(rec) {
  if (!rec.result) {
    const cl = window.qhClassify ? window.qhClassify(rec.sql) : { tier: rec.tier };
    rec.result = window.qhMockResult(rec.sql, cl);
  }
  const r = rec.result;
  if (r.kind !== 'table') return { kind: 'affected', affected: r.affected || 0, message: r.message || '' };
  return {
    kind: 'table', cols: r.cols || [], piiCols: r.piiCols || [], colTypes: r.colTypes || mockColTypes(r.cols || []),
    total: r.total || 0, truncated: !!r.truncated,
    slice: (offset, count) => r.slice(offset, count),
    // Real API pulls windows beyond the first page from /queries/:id/rows.
    fetchPage: (offset, count) => mockDelay(() => r.slice(offset, count), 220),
  };
}

// MOCK: CSV/XLSX download. The real endpoints stream a server-built file; here
// we hand the browser a data: URL built from the mock rows (both as CSV).
function resultDataUrl(id) {
  const rec = MOCK.requests[id];
  if (!rec) return 'data:text/csv,';
  const r = resultFor(rec);
  if (r.kind !== 'table') return 'data:text/csv;charset=utf-8,' + encodeURIComponent('affected\n' + (r.affected || 0));
  const cap = Math.min(r.total, 5000);
  const esc = (v) => { const s = String(v == null ? '' : v); return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
  const lines = [r.cols.join(',')].concat(r.slice(0, cap).map(row => row.map(esc).join(',')));
  return 'data:text/csv;charset=utf-8,' + encodeURIComponent(lines.join('\n'));
}

// ---------- Schema (built from the deterministic mock generators) ----------
function schemaPayload(connId, dbId) {
  const { conn, db } = targetOf(connId, dbId);
  if (!conn || !db) return { tables: [], views: [] };
  const schema = window.qhSchemaFor ? window.qhSchemaFor(conn, db) : 'public';
  const tbl = (name) => ({ name, schema, columns: window.qhColumnsFor(name), indexes: window.qhIndexesFor(name), approxRows: window.qhApproxRows(name) });
  return {
    tables: (db.tables || []).map(tbl),
    views: (window.qhViewsFor ? window.qhViewsFor(db.id) : []).map(v => ({ name: v, schema, columns: window.qhColumnsFor(v), indexes: [], approxRows: window.qhApproxRows(v) })),
  };
}

// ---------- The mock client ----------
const qhApi = {
  // ---- auth / identity ----
  me: () => mockDelay(null, 90).then(() => {
    const u = mockUser();
    if (!u) return mockFail('Not signed in.', 401, 'unauthenticated');
    return { user: u, displayTz: 'Europe/Istanbul',
      admin: { canApprove: u.role !== 'developer', role: u.role, connections: ['*'] } };
  }),
  providers: () => mockDelay({
    providers: [{ id: 'slack', kind: 'oauth', label: 'Slack' }, { id: 'local', kind: 'password', label: 'Local account' }],
    orgLabel: (window.qhBrand ? window.qhBrand().org : null),
  }, 120),
  // MOCK: any non-empty password is accepted. A username matching a mock
  // person signs in as them (so the developer / DBA / super views are reachable).
  localLogin: (username, password) => mockDelay(null, 420).then(() => {
    if (!password) return mockFail('Enter your password.', 401, 'invalid_credentials');
    const key = String(username).toLowerCase().replace(/\s+/g, '.');
    if (password === 'wrong') return mockFail('Wrong username or password.', 401, 'invalid_credentials');
    mockSignIn(MOCK_USERS[key] ? key : 'dana.kaur');
    return { ok: true };
  }),
  localChangePassword: (cur, next) => mockDelay(null, 380).then(() => {
    if (!cur) return mockFail('Enter your current password.', 400, 'bad_request');
    if (String(next).length < 8) return mockFail('New password must be at least 8 characters.', 400, 'bad_request');
    mockSignOut();   // real server revokes every session → back to login
    return { ok: true };
  }),
  signout: () => { mockSignOut(); return mockDelay({ ok: true }, 60); },
  changelog: () => mockDelay({ releases: MOCK_CHANGELOG }, 200),

  // ---- targets / schema ----
  // Disabled targets are RETURNED, flagged: hiding them would break the saved
  // queries and history that reference the alias. `host` is display-only — the
  // endpoint the sidebar shows on hover when it lists databases instead of
  // servers (real API: expose it read-only on GET /connections; never secrets).
  connections: () => mockDelay(() => ({ connections: connRegistry().map(c => ({
    id: c.id, name: c.name, engine: c.engine, env: c.env, autoApproveRO: c.autoApproveRO,
    disabled: c.enabled === false, host: c.host, port: c.port,
    // The tree, the autocomplete and drag-insert all read the table list off
    // THIS payload (`tableRefs`, falling back to bare `tables`) — the schema
    // endpoint only fills in columns/indexes when a node is opened. The real
    // GET /connections must carry it too, or the tree renders empty databases.
    databases: c.databases.map(d => ({ ...d, tables: ((targetOf(c.id, d.id).db || {}).tables) || [],
      autoApproveRO: d.tier === 'RO' && c.env !== 'production' ? true : undefined })),
  })) })),
  schema: (conn, dbn) => mockDelay(() => schemaPayload(conn, dbn), 260),
  roles: (conn) => mockDelay(() => {
    if (!mockUser() || mockUser().role !== 'super') return mockFail('Super-admin only.', 403, 'forbidden');
    return { roles: window.qhServerRoles ? window.qhServerRoles(conn) : [] };
  }, 240),

  // POST /queries/draft — reserves the id a new tab will submit under, so the
  // number is quotable before there is anything to submit. Same auth gates as
  // submitting; an unclaimed draft is reaped server-side.
  reserveDraft: () => mockDelay(() => {
    if (!mockUser()) return mockFail('Not signed in.', 401, 'unauthenticated');
    const id = nextQid();
    MOCK.drafts[id] = Date.now();
    return { id };
  }, 140),

  // ---- saved / history / sessions / scheduled ----
  saved: () => mockDelay(() => ({ saved: MOCK.savedSrv.slice() })),
  history: () => mockDelay(() => ({ history: (window.QH_HISTORY || []).map((h, i) => ({
    id: h.id, sql: h.sql, connectionId: h.conn, databaseId: h.db, tier: h.tier, status: h.status,
    rowCount: h.rows, approver: h.approver, createdAt: isoAgo(1000 * 60 * (2 + i * 37)),
  })) })),
  saveSnippet: (b) => mockDelay(() => {
    const row = { id: mockId('srv'), name: b.name, connectionId: b.connectionId, databaseId: b.databaseId, sql: b.sql };
    MOCK.savedSrv = [row, ...MOCK.savedSrv.filter(s => !(s.name === row.name && s.connectionId === row.connectionId && s.databaseId === row.databaseId))];
    return row;
  }),
  deleteSnippet: (id) => mockDelay(() => { MOCK.savedSrv = MOCK.savedSrv.filter(s => s.id !== id); return null; }),
  sessions: () => mockDelay(() => ({ sessions: MOCK.sessionsSrv.slice() })),
  saveSessionSrv: (b) => mockDelay(() => {
    const row = { id: mockId('ses'), name: b.name, dest: 'server', savedAt: Date.now(),
      tabs: (b.tabs || []).map(t => ({ name: t.name, sql: t.sql, conn: t.connectionId, db: t.databaseId })) };
    MOCK.sessionsSrv = [row, ...MOCK.sessionsSrv.filter(s => s.name !== row.name)];
    return row;
  }),
  deleteSessionSrv: (id) => mockDelay(() => { MOCK.sessionsSrv = MOCK.sessionsSrv.filter(s => s.id !== id); return null; }),
  scheduled: () => mockDelay(() => ({ scheduled: MOCK.scheduled.slice() })),
  cancelScheduledSrv: (id) => mockDelay(() => { MOCK.scheduled = MOCK.scheduled.filter(s => s.id !== id); return null; }),

  // ---- submit / track / results ----
  submit: (body) => mockDelay(() => {
    if (ADMIN.kill.enabled) return mockFail('Kill switch is engaged — query execution is paused.', 503, 'kill_switch');
    const rec = newRequest(body);
    if (rec.scheduledFor) {
      MOCK.scheduled = [{ id: rec.id, name: body.name || 'Scheduled query', sql: body.sql, conn: rec.conn, db: rec.db,
        tier: rec.tier, when: rec.scheduledFor, status: 'scheduled' }, ...MOCK.scheduled];
    }
    return { id: rec.id, status: statusOf(rec).status };
  }, 300),
  submitBatch: (body) => mockDelay(() => {
    const bundleId = mockId('bnd');
    return { bundleId, items: (body.items || []).map(it => {
      const rec = newRequest(it, bundleId);
      return { id: rec.id, status: statusOf(rec).status };
    }) };
  }, 340),
  status: (id) => mockDelay(() => {
    const rec = MOCK.requests[id];
    if (!rec) return mockFail('Unknown request.', 404, 'not_found');
    return statusOf(rec);
  }, 110),
  result: (id) => mockDelay(() => {
    const rec = MOCK.requests[id];
    if (!rec) return mockFail('Unknown request.', 404, 'not_found');
    return resultFor(rec);
  }, 180),
  rows: (id, o, l) => mockDelay(() => {
    const rec = MOCK.requests[id];
    const r = rec ? resultFor(rec) : null;
    return { rows: r && r.kind === 'table' ? r.slice(o, l) : [] };
  }, 200),
  resultCsvUrl: (id) => resultDataUrl(id),
  resultXlsxUrl: (id) => resultDataUrl(id),
  // POST /queries/{id}/cancel — one endpoint, three outcomes. The pre-execution
  // states withdraw the request (it never touches the database); running kills
  // the statement; anything terminal is a no-op. Callers must not collapse
  // `outcome` into success/error: which act happened is the point.
  cancelRun: (id) => mockDelay(() => {
    const rec = MOCK.requests[id];
    if (!rec) return mockFail('Unknown request.', 404, 'not_found');
    const st = statusOf(rec).status;
    if (st === 'pending' || st === 'approved' || st === 'scheduled') {
      rec.stopped = 'withdrawn';
      ADMIN.queue = ADMIN.queue.filter(x => x.id !== id);
      return { outcome: 'withdrawn', message: 'Request withdrawn — it is off the DBA queue.' };
    }
    if (st === 'running') {
      rec.stopped = 'terminated';
      return { outcome: 'terminated', message: 'Cancel signalled to the database — the statement was stopped.' };
    }
    return { outcome: 'not_running', message: 'Nothing to stop — this request already finished.' };
  }, 260),
  explain: (b) => mockDelay(() => {
    const cl = window.qhClassify(b.sql);
    return { plan: window.qhExplainPlan(b.sql, cl), hints: window.qhRiskHints(b.sql, cl) };
  }, 320),
  classify: (b) => mockDelay(() => verdictFor(b.sql, b.connectionId, b.databaseId), 150),

  // ---- misc developer actions ----
  requestEndpoint: (b) => mockDelay(() => {
    ADMIN.endpointReqs = [{ id: mockId('er'), server: b.server, database: b.database, tier: b.tier,
      reason: b.reason, requester: (mockUser() || {}).handle || 'you', requestedAt: isoNow(), status: 'submitted' }, ...ADMIN.endpointReqs];
    return { ok: true };
  }, 300),
  feedback: (b) => mockDelay(() => {
    ADMIN.feedback = [{ id: mockId('f'), user: (mockUser() || {}).handle || 'you', score: null,
      comment: b.subject + ' — ' + b.details, queryId: null, when: isoNow(), type: b.type, severity: b.severity }, ...ADMIN.feedback];
    return { ok: true };
  }, 280),
  notifications: () => mockDelay(() => ({ notifications: MOCK_NOTIFICATIONS.map(n => ({ ...n, read: MOCK.notifRead.includes(n.id) })) }), 220),
  notificationsRead: (b) => mockDelay(() => {
    if (b && b.all) MOCK.notifRead = MOCK_NOTIFICATIONS.map(n => n.id);
    else if (b && b.ids) MOCK.notifRead = [...new Set([...MOCK.notifRead, ...b.ids])];
    return { ok: true };
  }, 80),

  // ---- admin: queue & kill switch ----
  adminQueue: () => mockDelay(() => ({ queue: ADMIN.queue.slice() }), 200),
  adminDecision: (id, b) => mockDelay(() => {
    const it = ADMIN.queue.find(x => x.id === id);
    ADMIN.queue = ADMIN.queue.filter(x => x.id !== id);
    if (it) {
      const label = b.decision === 'approve' ? 'Approved query' : b.decision === 'reject' ? 'Rejected query' : 'Requested changes';
      mockAudit(label + (b.note ? ' · ' + b.note : ''), it.submitter.name + ' · ' + it.connectionId + '/' + it.databaseId,
        b.decision === 'approve' ? 'approve' : b.decision === 'reject' ? 'reject' : 'changes', { tier: it.tier, query: it.sql, requestId: it.requestId || it.id });
    }
    return { ok: true };
  }, 260),
  adminBatchApprove: (ids) => mockDelay(() => {
    const items = ADMIN.queue.filter(x => ids.includes(x.id));
    ADMIN.queue = ADMIN.queue.filter(x => !ids.includes(x.id));
    items.forEach(it => mockAudit('Approved query (batch)', it.submitter.name + ' · ' + it.connectionId + '/' + it.databaseId, 'approve', { tier: it.tier, query: it.sql, requestId: it.requestId || it.id }));
    return { approved: items.length };
  }, 320),
  adminKillGet: () => mockDelay(() => ({ ...ADMIN.kill }), 140),
  adminKillSet: (b) => mockDelay(() => {
    ADMIN.kill = { enabled: !!b.enabled, message: b.enabled ? (b.message || '') : '', by: b.enabled ? 'dba.amara' : null, at: b.enabled ? isoNow() : null };
    mockAudit(b.enabled ? 'Engaged kill switch — all execution paused' : 'Released kill switch', 'global · fleet-wide', b.enabled ? 'reject' : 'approve');
    return { ...ADMIN.kill };
  }, 220),

  // ---- admin: access (grants / auto-approve / scopes) ----
  adminGrants: () => mockDelay(() => ({ grants: ADMIN.grants.slice() }), 200),
  adminAddGrant: (b) => mockDelay(() => {
    const dbs = (b.databases && b.databases.length) ? b.databases : (b.databaseId ? [b.databaseId] : ['*']);
    const ex = ADMIN.grants.find(g => g.subjectType === b.subjectType && g.subject === b.subject && g.connectionId === b.connectionId);
    if (ex) { ex.databases = dbs; ex.tier = b.tier; mockAudit('Updated grant · ' + b.tier, b.subject + ' → ' + b.connectionId, 'grant'); return { id: ex.id }; }
    const row = { id: mockId('g'), subjectType: b.subjectType, subject: b.subject, subjectName: b.subjectType === 'team' ? b.subject : undefined,
      connectionId: b.connectionId, databases: dbs, tier: b.tier, grantedBy: 'dba.amara', grantedAt: isoNow() };
    ADMIN.grants = [row, ...ADMIN.grants];
    mockAudit('Granted ' + b.tier, b.subject + ' → ' + b.connectionId, 'grant');
    return { id: row.id };
  }, 240),
  adminDelGrant: (id) => mockDelay(() => {
    const g = ADMIN.grants.find(x => x.id === id);
    ADMIN.grants = ADMIN.grants.filter(x => x.id !== id);
    if (g) mockAudit('Revoked grant', g.subject + ' → ' + g.connectionId, 'reject');
    return null;
  }, 200),
  adminAutoGrants: () => mockDelay(() => ({ autoGrants: ADMIN.auto.slice() }), 180),
  adminAddAutoGrant: (b) => mockDelay(() => {
    const row = { ...b, id: mockId('a'), createdBy: 'dba.amara' };
    ADMIN.auto = [row, ...ADMIN.auto];
    mockAudit('Created auto-approve grant', b.user + ' → ' + b.connectionId + '/' + (b.databaseId || '*') + ' · ' + b.tier, 'auto');
    return row;
  }, 220),
  adminDelAutoGrant: (id) => mockDelay(() => { ADMIN.auto = ADMIN.auto.filter(x => x.id !== id); mockAudit('Revoked auto-approve grant', '', 'reject'); return null; }, 180),
  adminScopes: () => mockDelay(() => ({ scopes: ADMIN.scopes.slice() }), 180),
  adminSaveScope: (b) => mockDelay(() => {
    const ex = ADMIN.scopes.find(s => s.id === b.admin || s.admin === b.admin);
    if (ex) { Object.assign(ex, { role: b.role, canApprove: b.canApprove, connections: b.connections }); mockAudit('Updated admin scope', ex.admin, 'scope'); return ex; }
    const row = { id: mockId('s'), admin: b.admin, role: b.role, canApprove: b.canApprove, connections: b.connections };
    ADMIN.scopes = [row, ...ADMIN.scopes];
    mockAudit('Added admin', b.admin + ' · ' + b.role, 'scope');
    return row;
  }, 240),
  adminDelScope: (id) => mockDelay(() => {
    const s = ADMIN.scopes.find(x => x.id === id);
    if (s && s.role === 'super' && ADMIN.scopes.filter(x => x.role === 'super').length < 2) return mockFail('Refusing to remove the last super-admin.', 409, 'conflict');
    ADMIN.scopes = ADMIN.scopes.filter(x => x.id !== id);
    if (s) mockAudit('Removed admin', s.admin, 'reject');
    return null;
  }, 200),

  // ---- admin: connections registry ----
  adminConnections: () => mockDelay(() => ({ connections: connRegistry().slice() }), 220),
  adminCreateConnection: (b) => mockDelay(() => {
    const reg = connRegistry();
    if (reg.some(c => c.name === b.alias)) return mockFail('A connection named “' + b.alias + '” already exists.', 409, 'conflict');
    const eid = b.engine || 'postgres';
    const creds = { ro: { username: '', configured: false, placeholder: false }, rw: { username: '', configured: false, placeholder: false }, ddl: { username: '', configured: false, placeholder: false } };
    Object.keys(b.credentials || {}).forEach(t => { if (creds[t]) creds[t] = { username: (b.credentials[t] || {}).username || '', configured: !!(b.credentials[t] || {}).password, placeholder: false }; });
    const row = { id: b.alias, name: b.alias, engineId: eid, engine: eid === 'mssql' ? 'SQL Server 2022' : 'PostgreSQL 15',
      env: 'production', enabled: false, host: b.host, port: b.port || DEFAULT_PORT[eid], defaultDatabase: b.defaultDatabase,
      notes: b.notes || '', databases: [{ id: b.defaultDatabase, name: b.defaultDatabase, tier: 'RO' }], credentials: creds };
    ADMIN.connections = [...reg, row];
    mockAudit('Registered connection', row.name + ' · ' + row.host, 'scope');
    return row;
  }, 420),
  adminUpdateConnection: (conn, b) => mockDelay(() => {
    const row = connRegistry().find(c => c.id === conn);
    if (!row) return mockFail('Unknown connection.', 404, 'not_found');
    if (b.alias) { row.name = b.alias; }
    ['host', 'port', 'defaultDatabase', 'notes'].forEach(k => { if (b[k] != null) row[k] = b[k]; });
    if (b.engine) { row.engineId = b.engine; row.engine = b.engine === 'mssql' ? 'SQL Server 2022' : 'PostgreSQL 15'; }
    if (b.enabled != null) row.enabled = !!b.enabled;
    Object.keys(b.credentials || {}).forEach(t => {
      const inc = b.credentials[t] || {};
      row.credentials[t] = { username: inc.username || (row.credentials[t] || {}).username || '', configured: inc.password ? true : !!(row.credentials[t] || {}).configured, placeholder: false };
    });
    mockAudit('Updated connection', row.name, 'scope');
    return row;
  }, 380),
  adminDeleteConnection: (conn) => mockDelay(() => {
    const row = connRegistry().find(c => c.id === conn);
    if (!row) return mockFail('Unknown connection.', 404, 'not_found');
    const inUse = ADMIN.grants.some(g => g.connectionId === conn);
    if (inUse) { row.enabled = false; mockAudit('Disabled connection (in use)', row.name, 'reject'); return { deleted: false, disabled: true, reason: 'Connection “' + row.name + '” still has live grants — disabled instead of deleted.' }; }
    ADMIN.connections = connRegistry().filter(c => c.id !== conn);
    mockAudit('Deleted connection', row.name, 'reject');
    return { deleted: true, disabled: false, reason: null };
  }, 360),
  // MOCK reachability probes: a host with "bad"/"unknown" in it fails, the rest
  // answer ok. Real ones open a connection with the tier's stored credential.
  adminTestNewConnection: (b) => mockDelay(() => {
    const bad = /bad|unknown|invalid/i.test(b.host || '');
    return bad ? { ok: false, error: 'could not translate host name to an address', latencyMs: null, serverVersion: null }
      : { ok: true, error: null, latencyMs: 18 + ((b.host || '').length * 3 % 40), serverVersion: b.engine === 'mssql' ? '16.0.4125' : '15.6' };
  }, 700),
  adminTestConnection: (conn) => mockDelay(() => {
    const row = connRegistry().find(c => c.id === conn);
    if (!row) return mockFail('Unknown connection.', 404, 'not_found');
    if (!row.credentials.ro.configured) return { ok: false, error: 'no read-only credentials stored', latencyMs: null, serverVersion: null };
    return { ok: true, error: null, latencyMs: 16 + (row.id.length * 5 % 40), serverVersion: row.engineId === 'mssql' ? '16.0.4125' : '15.6' };
  }, 620),
  adminSchemaRefresh: (conn) => mockDelay(() => {
    const row = connRegistry().find(c => c.id === conn);
    const n = row ? row.databases.reduce((a, d) => a + ((targetOf(row.id, d.id).db || {}).tables || []).length, 0) : 0;
    mockAudit('Refreshed schema snapshot', conn, 'scope');
    return { tables: n };
  }, 900),

  // ---- admin: endpoint requests, teams & people ----
  adminEndpointReqs: () => mockDelay(() => ({ requests: ADMIN.endpointReqs.slice() }), 180),
  adminDecideEndpoint: (id, approve, note) => mockDelay(() => {
    const er = ADMIN.endpointReqs.find(x => String(x.id).replace(/^er_/, '') === String(id).replace(/^er_/, ''));
    if (!er) return mockFail('Unknown request.', 404, 'not_found');
    er.status = approve ? 'provisioned' : 'rejected';
    if (approve) {
      // Approval auto-creates the grant it asked for (0.1.0 behaviour).
      ADMIN.grants = [{ id: mockId('g'), subjectType: 'user', subject: er.requester, connectionId: er.server,
        databases: [er.database], tier: er.tier, grantedBy: 'dba.amara', grantedAt: isoNow() }, ...ADMIN.grants];
    }
    mockAudit(approve ? 'Provisioned endpoint' : 'Rejected endpoint request', er.server + '/' + er.database + ' · ' + er.tier, approve ? 'grant' : 'reject');
    return { ok: true };
  }, 300),
  adminPeople: () => mockDelay(() => ({ people: ADMIN.people.slice() }), 160),
  adminTeams: () => mockDelay(() => ({ teams: ADMIN.teams.map(t => ({ ...t, subteams: [] })) }), 160),
  adminSaveTeam: (t) => mockDelay(() => {
    if (t.id) {
      const ex = ADMIN.teams.find(x => x.id === t.id);
      if (ex) Object.assign(ex, { name: t.name, desc: t.desc, members: t.members || [] });
      mockAudit('Updated team', t.name + ' · ' + (t.members || []).length + ' members', 'scope');
      return ex;
    }
    const row = { id: mockId('t'), name: t.name, desc: t.desc || '', members: t.members || [] };
    ADMIN.teams = [row, ...ADMIN.teams];
    mockAudit('Created team', row.name, 'scope');
    return row;
  }, 260),
  adminDelTeam: (id) => mockDelay(() => {
    const t = ADMIN.teams.find(x => x.id === id);
    ADMIN.teams = ADMIN.teams.filter(x => x.id !== id);
    if (t) { ADMIN.grants = ADMIN.grants.filter(g => !(g.subjectType === 'team' && g.subject === t.name)); mockAudit('Deleted team', t.name, 'reject'); }
    return null;
  }, 240),
  adminSetPersonTeams: (handle, teamIds) => mockDelay(() => {
    ADMIN.teams = ADMIN.teams.map(t => {
      const has = t.members.includes(handle), should = (teamIds || []).includes(t.id);
      if (has === should) return t;
      return { ...t, members: should ? [...t.members, handle] : t.members.filter(m => m !== handle) };
    });
    mockAudit('Updated team membership', handle + ' · ' + (teamIds || []).length + ' teams', 'scope');
    return { ok: true };
  }, 240),

  // ---- admin: insights & config ----
  adminAudit: () => mockDelay(() => ({ audit: ADMIN.audit.slice() }), 220),
  adminMetrics: () => mockDelay(() => MOCK_METRICS, 380),
  adminFeedback: () => mockDelay(() => ({ feedback: ADMIN.feedback.slice() }), 180),
  adminConfig: () => mockDelay(() => ({ config: JSON.parse(JSON.stringify(ADMIN.config)) }), 220),
  adminConfigSave: (changes) => mockDelay(() => {
    ADMIN.config.values = { ...ADMIN.config.values, ...(changes || {}) };
    const n = Object.keys(changes || {}).length;
    mockAudit('Updated system configuration', n + ' setting' + (n === 1 ? '' : 's') + ' · fleet-wide', 'scope');
    return { config: JSON.parse(JSON.stringify(ADMIN.config)), applied: n };
  }, 320),
};

// Slack SSO is a full-page redirect in production. MOCK: sign in as the demo
// super-admin and reload, which is the same observable outcome.
function qhSignInWithSlack() { mockSignIn('dana.kaur'); setTimeout(() => window.location.reload(), 420); }

function qhTimeAgo(iso) {
  if (!iso) return '';
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + ' min ago';
  if (s < 86400) return Math.floor(s / 3600) + ' h ago';
  if (s < 172800) return 'Yesterday';
  return Math.floor(s / 86400) + ' d ago';
}

function qhScheduleToISO(when) {
  const d = new Date();
  if (when === 'In 1 hour') d.setHours(d.getHours() + 1);
  else if (when === 'Tonight 02:00') { d.setDate(d.getDate() + 1); d.setHours(2, 0, 0, 0); }
  else if (when === 'Tomorrow 09:00') { d.setDate(d.getDate() + 1); d.setHours(9, 0, 0, 0); }
  else return null;
  return d.toISOString();
}

Object.assign(window, { qhApi, qhSignInWithSlack, qhTimeAgo, qhScheduleToISO, API_BASE });
