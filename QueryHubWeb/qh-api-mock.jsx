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
//   * It travels to the repo WHOLE, with its hash, as a PROTOTYPE-ONLY file
//     (SYNC.md §2, from 2026-09-24 (f)): the raw prototype and the public
//     README images run over it, so a stale copy breaks them. The Vite build
//     never loads it, and nothing is implemented against it — `qh-api.jsx` +
//     the real endpoints stay the contract.
//   * Invented data only: personas that match nobody, `U0EXAMPLE###` ids,
//     `example.com` / `example.internal` — the commit gate refuses the rest.
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
// `extra` carries the structured fields an error can hold beside its sentence —
// e.g. `expiredOn` on `access_expired`. The real envelope forwards a `detail`
// dict the same way, and the point is that a client reads a FIELD instead of
// parsing the message: matching on wording is how a duplicate once got mistaken
// for the confirm prompt and re-sent with confirmed:true.
const mockFail = (message, status, code, extra) => { const e = new Error(message); e.status = status || 400; e.code = code || 'bad_request'; if (extra) Object.assign(e, extra); return Promise.reject(e); };
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
  const brand = window.qhBrand ? window.qhBrand() : { org: 'Example' };
  return {
    id: base.slackId, slackId: base.slackId, handle: s.handle, name: base.name,
    initials: base.initials, role: s.role || base.role, team: brand.org,
    avatar: window.qhMockAvatar ? window.qhMockAvatar(base.initials) : null,
    mustChangePassword: false,
  };
}

// ---------- Developer-side mock state ----------
const MOCK = {
  // Seeded so each `connectionState` has a row that reaches the UI — a state no
  // seed exercises is a state the UI is never seen in: a retired target, an
  // alias that no longer exists, and one the caller holds no grant on. Real
  // installs get these from POST /saved.
  savedSrv: [
    { id: 'srv_seed1', name: 'Pricing rollback check', connectionId: 'svc-prod-pricing', databaseId: 'pricing_service',
      sql: 'SELECT * FROM price_changes\nORDER BY id DESC\nLIMIT 50;' },
    { id: 'srv_seed2', name: 'Registry reconciliation', connectionId: 'svc-prod-registry', databaseId: 'registry_service',
      sql: "SELECT COUNT(*) FROM reconciliations\nWHERE status <> 'done';" },
    { id: 'srv_seed3', name: 'Legacy ledger export', connectionId: 'svc-prod-ledger-old', databaseId: 'ledger',
      sql: "SELECT * FROM ledger_entries\nWHERE created_at > now() - interval '1 day';" },
    // MOCK: a three-statement script, so the statement switcher on the Results
    // tab is reachable without anyone typing one — the state it exists for was
    // otherwise only ever seen by whoever thought to write two semicolons.
    { id: 'srv_seed4', name: 'KYC sweep (3 steps)', connectionId: 'prod-replica', databaseId: 'users_ro',
      sql: "SELECT count(*) AS pending\nFROM user_kyc\nWHERE verified = false;\n\nSELECT id, email, created_at\nFROM user_kyc\nWHERE verified = false\nORDER BY created_at\nLIMIT 100;\n\nSELECT date_trunc('day', created_at) AS day, count(*) AS seen\nFROM user_kyc\nWHERE created_at > now() - interval '7 days'\nGROUP BY 1\nORDER BY 1;" },
  ],
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
  { version: 'v0.1.0', date: '2026-09-07', sha: '4d66e9b', area: 'Admin', headline: 'Someone can approve for one team without becoming an admin',
    summary: 'Access → Roles: give a person the right to approve, grant or import, bounded to a team, a server and a tier ceiling — instead of making them an admin everywhere.',
    changes: [
      { type: 'new', text: 'Roles tab under Access — approver, granter, importer or admin, each row written as a sentence saying exactly how far it reaches.' },
      { type: 'new', text: 'A role can end on a date; the row says so in the sentence rather than hiding it in a column.' },
      { type: 'new', text: 'Any admin can read the list — “who can approve my team’s requests?” no longer needs a super-admin to answer.' },
      { type: 'changed', text: 'Roles are recorded now but not yet in force: the fleet still reads the admins table, so those rows are marked mirrored and live, and new ones are marked staged.' },
      { type: 'fixed', text: 'Disabling somebody’s account revokes nothing — their roles and grants stand. The list now says so on the row instead of leaving it to be discovered.' },
    ],
    commits: [{ sha: '4d66e9b', msg: 'roles: roleId on 409, distinct refusal codes, enabled honoured in roles()' }, { sha: '0aae199', msg: 'add scoped roles screen + GET/POST/DELETE /admin/roles' }] },
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
  { id: 'q_8ef2', submitter: { name: 'Chen Yu', initials: 'CY', slackId: 'U07CY', trust: null },
    connectionId: 'prod-main', databaseId: 'analytics', env: 'production', tier: 'DDL',
    sql: "ALTER TABLE events\n  ADD COLUMN device_fingerprint text;",
    statements: 1, piiCols: [], estRows: null, estTables: ['events'], reason: 'Need a column for the new anti-fraud signal.',
    submittedAt: isoAgo(1000 * 60 * 24), escalate: true,
    // MOCK (design 2026-09-22 §8): what the DDL escalations screen used to carry
    // on its own, now on the queue row itself. `elevation` is the DDL standing
    // this request runs under — why it was given, when it ends, who gave it.
    // Field names are a proposal; CODE confirms them (DESIGN_TO_CODE_BRIEF).
    elevation: { source: 'user', reason: 'Owns the anti-fraud schema for the 4.12 release.', grantedBy: 'dba.marco', grantedByName: 'Marco Young', grantedAt: isoAgo(1000 * 86400 * 5), expiresAt: isoIn(1000 * 60 * 60 * 30) } },
  // The second DDL shape: the standing comes from a TEAM grant with no end date,
  // and nobody recorded who gave it. Both gaps have to render as gaps.
  { id: 'q_8eb1', submitter: { name: 'Kai Yamada', initials: 'KY', slackId: 'U0EXAMPLE008', trust: 74 },
    connectionId: 'staging', databaseId: 'app_stg', env: 'staging', tier: 'DDL',
    sql: "CREATE INDEX CONCURRENTLY idx_sessions_user\n  ON sessions (user_id, created_at);",
    statements: 1, piiCols: [], estRows: null, estTables: ['sessions'], reason: 'Session lookups time out in the load test.',
    submittedAt: isoAgo(1000 * 60 * 31), escalate: true,
    elevation: { source: 'team', team: 'backend', reason: null, grantedBy: null, grantedByName: null, grantedAt: isoAgo(1000 * 86400 * 90), expiresAt: null } },
  // The third DDL shape (CODE 2026-09-23 §4): `source: 'admin'` — a fleet
  // admin's own standing. No grant sits behind it, so what it lasts is the role.
  { id: 'q_8ea4', submitter: { name: 'Eli Kovac', initials: 'EK', slackId: 'U0EXAMPLE010', trust: 90 },
    connectionId: 'reporting-mssql', databaseId: 'ReportingDW', env: 'production', tier: 'DDL',
    sql: "CREATE NONCLUSTERED INDEX IX_FactTrades_TradeDate\n  ON FactTrades (TradeDate) INCLUDE (UserId, Amount);",
    statements: 1, piiCols: [], estRows: null, estTables: ['FactTrades'], reason: 'The month-end report scans FactTrades by date.',
    submittedAt: isoAgo(1000 * 60 * 47), escalate: true,
    elevation: { source: 'admin', team: null, reason: 'Fleet admin since the reporting migration.', grantedBy: 'dba.amara', grantedByName: 'Amara Osei', grantedAt: isoAgo(1000 * 86400 * 120), expiresAt: null } },
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
  // MOCK: one submitter whose identity source held no display name, so `name`
  // arrives as the bare handle — the case `qhPersonName` exists for. Without a
  // seed like this the queue only ever shows already-formatted names and the
  // derivation is never seen on screen. Deliberately NOT in a bundle: a batch is
  // one person's queries in one approval round, so two submitters under one
  // `bundleId` is data the real API cannot produce.
  { id: 'q_8e42', submitter: { name: 'omar.kane', initials: 'OK', slackId: 'U07OK', trust: 61 },
    connectionId: 'prod-replica', databaseId: 'analytics', env: 'production', tier: 'RO',
    sql: "SELECT day, signups, activations\nFROM funnel_daily\nWHERE day > current_date - 30\nORDER BY day;",
    statements: 1, piiCols: [], estRows: 30, estTables: ['funnel_daily'], reason: 'Monthly funnel numbers for the growth review.',
    submittedAt: isoAgo(1000 * 60 * 47) },
];

// Standing grants: one row per (subject, connection) with a database LIST
// (['*'] = all) and ONE tier. They CAN expire as of migration 096 (CODE brief
// 2026-08-15 (c)) — null is still the common case, so most rows carry none; two
// have a date so the column and its amber "soon" state are visible here.
// `grantedByName` (2026-08-21 (b)) with its three real gaps represented, because
// a shape the mock never sends is a shape the UI is never tested against:
// null on every TEAM grant (that table has no `granted_by` column at all),
// null on a row where nothing was recorded (g_7), and a free-text NOTE instead
// of a principal id (g_8) — someone used the column as a comment field.
const MOCK_GRANTS = [
  { id: 'g_1', subjectType: 'team', subject: 'data-eng', subjectName: 'data-eng', connectionId: 'prod-replica', databases: ['users_ro', 'analytics_ro'], tier: 'RO', expiresAt: null, grantedBy: 'dba.amara', grantedByName: null, grantedAt: isoAgo(1000 * 86400 * 40) },
  { id: 'g_2', subjectType: 'team', subject: 'backend', subjectName: 'backend', connectionId: 'staging', databases: ['app_stg'], tier: 'DDL', expiresAt: null, grantedBy: 'dba.amara', grantedByName: null, grantedAt: isoAgo(1000 * 86400 * 90) },
  { id: 'g_3', subjectType: 'team', subject: 'backend', subjectName: 'backend', connectionId: 'prod-replica', databases: ['users_ro'], tier: 'RO', expiresAt: null, grantedBy: 'dba.amara', grantedByName: null, grantedAt: isoAgo(1000 * 86400 * 15) },
  { id: 'g_4', subjectType: 'user', subject: 'elena.silva', subjectName: 'Elena Silva', connectionId: 'prod-main', databases: ['payments'], tier: 'RW', expiresAt: isoIn(1000 * 86400 * 2), grantedBy: 'dba.marco', grantedByName: 'Marco Young', grantedAt: isoAgo(1000 * 86400 * 3) },
  { id: 'g_5', subjectType: 'user', subject: 'chen.yu', subjectName: 'Chen Yu', connectionId: 'prod-main', databases: ['analytics'], tier: 'DDL', expiresAt: null, grantedBy: 'dba.marco', grantedByName: 'Marco Young', grantedAt: isoAgo(1000 * 86400 * 5) },
  { id: 'g_6', subjectType: 'user', subject: 'ben.donnelly', subjectName: 'Ben Donnelly', connectionId: 'prod-main', databases: ['users'], tier: 'RO', expiresAt: null, grantedBy: 'dba.amara', grantedByName: 'Amara Osei', grantedAt: isoAgo(1000 * 86400 * 20) },
  { id: 'g_7', subjectType: 'user', subject: 'maya.andersen', subjectName: 'Maya Andersen', connectionId: 'prod-replica', databases: ['*'], tier: 'RO', expiresAt: isoIn(1000 * 86400 * 45), grantedBy: null, grantedByName: null, grantedAt: isoAgo(1000 * 86400 * 8) },
  // The case CODE found under question 1 (brief 2026-08-21): an ADMIN-ONLY
  // principal. `dba.amara` holds a grant but has no requesters row, so she is
  // absent from `GET /admin/people` and the client-side lookup can only print
  // the handle. `subjectName` is the whole reason this row reads as a person.
  { id: 'g_8', subjectType: 'user', subject: 'dba.amara', subjectName: 'Amara Osei', connectionId: 'reporting-mssql', databases: ['ReportingDW'], tier: 'RO', expiresAt: null, grantedBy: 'pod import: copied from the access sheet (row 47)', grantedByName: 'pod import: copied from the access sheet (row 47)', grantedAt: isoAgo(1000 * 86400 * 60) },
  // The signed-in mock developer holds ONE database on a three-database server,
  // which is what makes `partial: true` visible in the request-access picker
  // (CODE brief 2026-08-22 §2). Without a row in this state the screen can only
  // be seen implying no access to a server the person queries daily.
  // The leaver the disabled-person case exists for: two production grants still
   // standing behind an account nobody can sign into (CODE brief 2026-09-01 §4).
  { id: 'g_10', subjectType: 'user', subject: 'theo.evans', subjectName: 'Theo Evans', connectionId: 'prod-main', databases: ['payments'], tier: 'RW', expiresAt: null, grantedBy: 'dba.amara', grantedByName: 'Amara Osei', grantedAt: isoAgo(1000 * 60 * 60 * 24 * 210) },
  { id: 'g_9', subjectType: 'user', subject: 'dana.kaur', subjectName: 'Dana Kaur', connectionId: 'prod-main', databases: ['users'], tier: 'RO', expiresAt: null, grantedBy: 'dba.marco', grantedByName: 'Marco Young', grantedAt: isoAgo(1000 * 86400 * 12) },
  // The resolver's rule 2 (CODE 2026-09-23 §3), seeded: an ENDED personal grant
  // still decides. Can's own RW on prod-replica/users ended six days ago, so he
  // has NO access to that database even though data-eng grants it.
  { id: 'g_11', subjectType: 'user', subject: 'chen.yu', subjectName: 'Chen Yu', connectionId: 'prod-replica', databases: ['users_ro'], tier: 'RW', expiresAt: isoAgo(1000 * 86400 * 6), grantedBy: 'dba.marco', grantedByName: 'Marco Young', grantedAt: isoAgo(1000 * 86400 * 36) },
  // A team at two tiers on one connection: `tier` alone would overstate `users`
  // (`mixedTiers` + `perDatabase`, CODE 2026-09-23 §4). Can's own DDL grant on
  // `analytics` makes his prod-main arrive as TWO rows — `key`, not connectionId.
  { id: 'g_13', subjectType: 'team', subject: 'data-eng', subjectName: 'data-eng', connectionId: 'prod-main', databases: ['analytics'], tier: 'RW', expiresAt: null, grantedBy: 'dba.amara', grantedByName: 'Amara Osei', grantedAt: isoAgo(1000 * 86400 * 30) },
  { id: 'g_14', subjectType: 'team', subject: 'data-eng', subjectName: 'data-eng', connectionId: 'prod-main', databases: ['users'], tier: 'RO', expiresAt: null, grantedBy: 'dba.amara', grantedByName: 'Amara Osei', grantedAt: isoAgo(1000 * 86400 * 30) },
];

// MOCK of the Slack directory `GET /admin/people/resolve` looks a principal up
// in (CODE brief 2026-08-22 §3). Two ids it knows and everything else it does
// not, because `known: false` with a name and `known: false` with NO name are
// different answers and the combo says a different sentence for each.
const MOCK_SLACK_DIR = {
  'U08NEW01': { name: 'Ada Dunn', email: 'ada.dunn@example.com' },
  'emma.tate': { name: 'Emma Tate', email: 'emma.tate@example.com' },
};
// Neutral on purpose (CODE 2026-09-24 (e)): an invented address, never the
// brand's domain, so the public export has nothing to rewrite here.
function mockEmail(handle) {
  return String(handle).replace(/[^A-Za-z0-9._-]/g, '') + '@example.com';
}
function mockInitials(name) {
  const parts = String(name).trim().split(/[\s._-]+/).filter(Boolean);
  return ((parts[0] || '?')[0] + (parts[1] ? parts[1][0] : '')).toUpperCase();
}
function mockDirectory(principal) { return MOCK_SLACK_DIR[principal] || MOCK_SLACK_DIR[String(principal).toUpperCase()] || null; }

// Nullable-by-contract fields are REPRESENTED here, not smoothed over: a mock
// that only ever sends the happy shape cannot surface the unhappy one, and that
// has already cost two live crashes (CODE_TO_DESIGN_BRIEF 2026-07-31). So one
// auto-grant never expires, one queue item has no row estimate and one
// submitter has no trust score. `maxRows` is gone entirely — there is no
// per-grant row cap in the model (CODE brief 2026-08-15 (c)).
// `userName` / `createdByName` are the resolved display names GET
// /admin/auto-grants returns beside the ids (CODE brief 2026-08-20 §2). NULL on
// the two team rows is the real fallback case, not laziness: a team is in
// neither people table, so the column has to fall back to the id.
// `databaseId: null` is "every database on the connection" — '*' normalises to
// NULL server-side now (§3).
const MOCK_AUTO = [
  { id: 'a_1', user: 'data-eng (team)', userName: null, tier: 'RO', connectionId: 'prod-replica', databaseId: null, expiresAt: isoIn(1000 * 86400 * 30), createdBy: 'dba.amara', createdByName: 'Amara Osei' },
  { id: 'a_2', user: 'amara.osei', userName: 'Amara Osei', tier: 'RO', connectionId: 'prod-main', databaseId: 'users', expiresAt: null, createdBy: 'dba.marco', createdByName: 'Marco Young' },
  { id: 'a_3', user: 'backend (team)', userName: null, tier: 'RW', connectionId: 'staging', databaseId: 'app_stg', expiresAt: isoIn(1000 * 86400 * 14), createdBy: 'dba.amara', createdByName: 'Amara Osei' },
];

const MOCK_SCOPES = [
  { id: 's_1', admin: 'dba.amara', role: 'super', canApprove: ['RO', 'RW', 'DDL'], connections: ['*'] },
  { id: 's_2', admin: 'dba.marco', role: 'super', canApprove: ['RO', 'RW', 'DDL'], connections: ['*'] },
  // `clara.alvarez` is deliberately a handle from the PEOPLE directory: a team
  // lead who is also a requester is the common case, and it is the only way the
  // approver-standing block on a person's access page has anything to show.
  // `dba.*` stay admin-only principals (no requesters row) — g_8 depends on it.
  { id: 's_3', admin: 'clara.alvarez', role: 'dba', canApprove: ['RO', 'RW'], connections: ['prod-main', 'prod-replica'], teams: ['payments'] },
  { id: 's_4', admin: 'oncall.eli', role: 'dba', canApprove: ['RO'], connections: ['prod-replica'] },
];

// `enabled` is the server's field (CODE brief 2026-09-01 §4) — a disabled person
// stays in the roster, marked and last, because being absent is what makes an
// admin retype an id from memory and create a second principal for one human.
const MOCK_PEOPLE = [
  { id: 'u_elif', handle: 'elena.silva', name: 'Elena Silva', initials: 'ES' },
  { id: 'u_aylin', handle: 'amara.osei', name: 'Amara Osei', initials: 'AO' },
  { id: 'u_burak', handle: 'ben.donnelly', name: 'Ben Donnelly', initials: 'BD' },
  { id: 'u_can', handle: 'chen.yu', name: 'Chen Yu', initials: 'CY' },
  { id: 'u_deniz', handle: 'dana.kaur', name: 'Dana Kaur', initials: 'DK' },
  { id: 'u_okan', handle: 'omar.kane', name: 'Omar Kane', initials: 'OK' },
  { id: 'u_merve', handle: 'maya.andersen', name: 'Maya Andersen', initials: 'MA' },
  { id: 'u_kaan', handle: 'kai.yamada', name: 'Kai Yamada', initials: 'KY' },
  { id: 'u_ceyda', handle: 'clara.alvarez', name: 'Clara Alvarez', initials: 'CA' },
  { id: 'u_emre', handle: 'eli.kovac', name: 'Eli Kovac', initials: 'EK' },
  { id: 'u_mert', handle: 'marco.young', name: 'Marco Young', initials: 'MY' },
  { id: 'u_selin', handle: 'sofia.ahmed', name: 'Sofia Ahmed', initials: 'SA' },
  { id: 'u_tolga', handle: 'theo.evans', name: 'Theo Evans', initials: 'TE', enabled: false },
// `slackId` so the member picker's type-ahead (design 2026-09-22 §5) has a
// Slack id to match on. Invented, in the U0EXAMPLE### shape — never a real one.
].map((p, i) => ({ enabled: true, slackId: 'U0EXAMPLE' + String(i + 1).padStart(3, '0'), ...p }));

// MOCK: auto-approve requests asked for from the web (design 2026-09-22 §3).
// One waiting, so the admin side of the flow is on screen without first
// filing one from the developer view.
// The windows an ask can name (GET /auto-approve-requests `windowOptions`, CODE
// 2026-09-23 §4): the web's day windows carry `days`, Slack's hour windows do
// not. An ask sends `days` OR `windowMinutes`, never both.
const MOCK_AUTO_WINDOWS = [
  { minutes: 60, label: '1h' }, { minutes: 180, label: '3h' }, { minutes: 480, label: '8h' },
  { minutes: 1440, label: '1 day', days: 1 }, { minutes: 10080, label: '7 days', days: 7 },
  { minutes: 20160, label: '14 days', days: 14 }, { minutes: 43200, label: '30 days', days: 30 },
];
// `days` is NULL on an ask made in Slack, so both places the admin card prints
// the window read `windowLabel` — the second seed is that case, or "for null
// days" is back on screen.
const MOCK_AUTO_REQS = [
  { id: 'ar_12', requester: 'omar.kane', requesterName: 'Omar Kane', connectionId: 'prod-replica', databaseId: 'analytics_ro', tier: 'RO', days: 14, windowMinutes: null, windowLabel: '14 days',
    reason: 'Funnel refresh for Q4 planning — the same read, twenty times a day, for two weeks.', requestedAt: isoAgo(1000 * 60 * 55), status: 'submitted' },
  { id: 'ar_11', requester: 'sofia.ahmed', requesterName: 'Sofia Ahmed', connectionId: 'prod-replica', databaseId: 'users_ro', tier: 'RO', days: null, windowMinutes: 480, windowLabel: '8h',
    reason: 'KYC backlog review this afternoon — the same lookup for every pending case.', requestedAt: isoAgo(1000 * 60 * 18), status: 'submitted' },
];

// MOCK of GET /admin/manual-runs (CODE 2026-09-23 §5): DDL the bot handed to a
// DBA because it may not run it itself — a role without the attribute, or
// "must be owner". `refusal` (the database's own words) is a field we ask CODE
// for; the rest is their shape. The role script's password is already hidden,
// as it is everywhere QueryHub stores one.
const MOCK_MANUAL_RUNS = [
  { id: 'q_8d71', requester: { slackId: 'U0EXAMPLE008', name: 'Kai Yamada' }, connectionId: 'staging', databaseId: 'app_stg', tier: 'DDL',
    sql: 'ALTER TABLE orders OWNER TO app_migrator;', reason: 'Hand the orders table to the migration role before the 4.12 cut.',
    createdAt: isoAgo(1000 * 60 * 190), escalatedAt: isoAgo(1000 * 60 * 150), bundleId: null, refusal: 'must be owner of table orders' },
  { id: 'q_8d9c', requester: { slackId: 'U0EXAMPLE004', name: 'Chen Yu' }, connectionId: 'prod-main', databaseId: 'analytics', tier: 'DDL',
    sql: "CREATE ROLE report_reader LOGIN PASSWORD '***REDACTED***';\nGRANT CONNECT ON DATABASE analytics TO report_reader;", reason: 'Read-only login for the BI tool.',
    createdAt: isoAgo(1000 * 60 * 64), escalatedAt: isoAgo(1000 * 60 * 40), bundleId: null, refusal: 'permission denied to create role' },
];
// The same hand-off as the developer sees it in History.
const MOCK_HIST_HANDOFF = { id: 'q_8d9c', sql: MOCK_MANUAL_RUNS[1].sql, connectionId: 'prod-main', databaseId: 'analytics', tier: 'DDL',
  rowCount: null, approver: 'dba.marco', createdAt: isoAgo(1000 * 60 * 20) };

// Teams do NOT nest — grants resolve through flat membership only.
const MOCK_TEAMS = [
  { id: 't_dataeng', name: 'data-eng', desc: 'Data engineering & analytics platform', members: ['amara.osei', 'chen.yu', 'omar.kane', 'maya.andersen'] },
  // `syncedFrom` (CODE 2026-09-23 §4): the pod sync owns this team's membership.
  { id: 't_backend', name: 'backend', desc: 'Core backend services', members: ['ben.donnelly', 'kai.yamada', 'marco.young'], syncedFrom: 'pods/core-backend' },
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
      { name: 'clara.alvarez', count: 173 }, { name: 'oncall.eli', count: 88 },
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
  // Roles created on the Roles screen. Seeded EMPTY deliberately: the operator's
  // fleet has none either, so the first thing anyone opening the tab sees is the
  // empty state plus the mirrored admin rows — the state the screen was designed
  // for. Seeding a tidy scoped approver here would hide it.
  // One direct row since 2026-09-22: the round adds EDITING, and a screen whose
  // only direct rows are the empty state cannot show an edit. The empty state is
  // still reachable by revoking it.
  roles: [{ id: 'r_seed1', subject: 'clara.alvarez', name: 'Clara Alvarez', role: 'approver', scopeTeamId: 't_payments', scopeTeamName: 'payments',
    scopeTargetId: 'prod-main', scopeTargetName: 'prod-main', maxTier: 'RW', validUntil: null, reason: 'Payments team lead', source: 'direct' }],
  autoReqs: MOCK_AUTO_REQS.slice(), manualRuns: MOCK_MANUAL_RUNS.slice(), manualClosed: {},
  endpointReqs: MOCK_ENDPOINT_REQS.slice(), feedback: MOCK_FEEDBACK.slice(),
  audit: MOCK_AUDIT.slice(), config: JSON.parse(JSON.stringify(MOCK_CONFIG)),
  kill: { enabled: false, message: '', by: null, at: null },
  connections: null,   // built lazily from QH_CONNECTIONS (registry shape)
  mask: null,          // masking exemptions, built lazily by maskSeed()
};
// The fleet-wide masking switch (`pii_mask_on_return` in system config). When it
// is off every exemption on the screen is moot, and the screen has to say so
// rather than implying it is doing something. Read from config so flipping the
// setting in System configuration is visible here.
function maskingOn() { return (ADMIN.config.values || {}).pii_mask_on_return !== 'off'; }

// `enforced` is the fleet's `access_model_v2` switch (CODE brief 2026-09-07 §2).
// FALSE is today's truth: direct rows are recorded and reviewed but the old code
// path still reads the legacy admins table, so only the MIRRORED rows decide
// anything. Flip it to see the screen without the staging banner.
const MOCK_ROLES_ENFORCED = false;
const QH_ROLE_KEYS = ['approver', 'granter', 'importer', 'admin'];
// Display name for a principal that may be a requester, an admin-only id, or
// neither. Same three-source order the server resolves in, so a mirrored admin
// (`dba.*`, no requesters row) still reads as a person.
const MOCK_ADMIN_NAMES = { 'dba.amara': 'Amara Osei', 'dba.marco': 'Marco Young', 'oncall.eli': 'Eli Kovac' };
function mockRoleName(id) {
  const p = ADMIN.people.find(x => x.handle === id || x.id === id);
  if (p) return p.name;
  if (MOCK_ADMIN_NAMES[id]) return MOCK_ADMIN_NAMES[id];
  const dir = mockDirectory(id);
  return dir ? dir.name : id;
}
// Every super-admin in the admins table is a fleet-wide role the new model
// mirrors read-only. Derived on read rather than copied into ADMIN.roles: a
// stored copy could disagree with the table it claims to mirror.
function roleRegistry() {
  // Scope arrives as a nullable id PLUS an explicit boolean, and the screen reads
  // the boolean (CODE brief 2026-09-07 §3): a null id alone cannot be told apart
  // from a field the server forgot to fill, and on an authorization screen that
  // difference is the entire scope. `maxTier` is uppercase on the wire.
  const shape = (r) => {
    const p = ADMIN.people.find(x => x.handle === r.subject || x.id === r.subject);
    return { ...r,
      enabled: p ? p.enabled !== false : true,
      allTeams: !r.scopeTeamId, allTargets: !r.scopeTargetId, anyTier: !r.maxTier,
      maxTier: r.maxTier ? String(r.maxTier).toUpperCase() : null };
  };
  const mirrored = ADMIN.scopes.filter(s => s.role === 'super').map(s => ({
    id: 'mirror:' + s.id, subject: s.admin, name: mockRoleName(s.admin), role: 'admin',
    scopeTeamId: null, scopeTeamName: null, scopeTargetId: null, scopeTargetName: null,
    maxTier: null, validUntil: null, reason: 'super-admin in the admins table', source: 'mirrored',
  }));
  // Ordered `enabled DESC` server-side so a leaver falls to the bottom. Sorted
  // HERE and never in the view: a client-side re-sort is how the server's
  // ordering intent gets overwritten (2026-08-21 §5).
  return [...ADMIN.roles, ...mirrored].map(shape)
    .sort((a, b) => (a.enabled === b.enabled ? 0 : a.enabled ? -1 : 1));
}

// `requestId` is the REQUEST behind the entry (null for grants, scopes,
// auto-approve windows and the kill switch — they belong to no request); `id`
// stays the audit row's own id, which nobody outside the table sees.
const mockAudit = (event, target, kind, extra) => {
  ADMIN.audit = [{ id: mockId('aa'), time: isoNow(), actor: 'dba.amara', event, target: target || '', kind: kind || 'scope', requestId: null, ...(extra || {}) }, ...ADMIN.audit];
};

// The registry rows the admin Connections screen edits. Passwords are never
// returned by the real API — only {username, configured, placeholder} per tier.
const DEFAULT_PORT = { postgres: 5432, mssql: 1433, oracle: 1521, mysql: 3306, clickhouse: 9440 };
const mockEngineName = (e) => e === 'mssql' ? 'SQL Server 2022' : e === 'clickhouse' ? 'ClickHouse 24.8' : 'PostgreSQL 15';
const mockEngineVer = (e) => e === 'mssql' ? '16.0.4125' : e === 'clickhouse' ? '24.8.4.13' : '15.6';
function connRegistry() {
  if (ADMIN.connections) return ADMIN.connections;
  const engineOf = (c) => (window.qhEngineId ? window.qhEngineId(c.engine) : 'postgres');
  const built = (window.QH_CONNECTIONS || []).map(c => {
    const eid = engineOf(c);
    return {
      // MOCK: one retired target, kept in the fleet on purpose — its alias still
      // resolves for old saved queries and history, and the UI has to say it is
      // disabled rather than hide it.
      id: c.id, name: c.name, engine: c.engine, engineId: eid, env: c.env, enabled: c.id !== 'svc-prod-pricing',
      // Where the machine lives. One bag per connection; databases inherit it.
      tags: { ...(c.tags || {}) },
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
  // Disabled targets arrive LAST, as the server sends them (`enabled DESC,
  // alias` — CODE brief 2026-08-20 §5). Mocking the tidy alphabetical order is
  // exactly what would let a client-side re-sort look right here and undo the
  // server's ordering in production, so the retired target sits at the end.
  // Read replicas (CODE 2026-09-23 (e)): rows of the registry with `replicaOf`
  // = the primary's name. No credential of their own — they run on the
  // primary's login — and `enabled` means "in rotation". One in, one out, so
  // both states render.
  const rep = (p, suffix, on) => { const b = built.find(x => x.id === p); if (!b) return null;
    return { ...b, id: p + '-' + suffix, name: p + '-' + suffix, replicaOf: b.name, enabled: on,
      host: p + '-' + suffix + '.db.internal', notes: '', databases: b.databases.map(x => ({ ...x })),
      credentials: { ro: { username: '', configured: false, placeholder: false }, rw: { username: '', configured: false, placeholder: false }, ddl: { username: '', configured: false, placeholder: false } } }; };
  const all = built.map(c => ({ ...c, replicaOf: null })).concat([rep('prod-main', 'r1', true), rep('reporting-mssql', 'r1', false)].filter(Boolean));
  ADMIN.connections = all.filter(c => c.enabled !== false).concat(all.filter(c => c.enabled === false));
  return ADMIN.connections;
}

// ---------- MOCK: the audit trail (design 2026-09-09 (c)) ----------
// Round (b) assumed action names were namespaced (`<area>.<object>.<verb>`) and
// derived both dimensions by splitting on the dot. CODE measured it: THEY ARE
// NOT. Every name below is a real one from the live trail, and classification
// works the way CODE's answer to Q2 describes — an ordered pattern table read by
// one function, first match wins, unmapped names falling through to
// `unclassified` by construction.
//
// The demonstration the round needs is not a big number of action types: it is
// that NINE patterns classify all 57 real names, so the next hundred need no new
// pattern. A per-action list is what failed; this is what replaces it.
const QH_AUD_PATTERNS = [
  // Most specific first. Protection before usage before access, because
  // `result_unmasked` and `effective_access_viewed` both contain words the
  // later patterns would claim.
  [/^pii_|^result_unmasked$|^result_(export|download)/, 'protection'],
  [/^slack_sql_opened$|_viewed$/, 'usage'],
  [/^execution_|^(submitted|started|completed|failed)$|_(submitted|started|completed|failed)$/, 'requests'],
  [/^(approved|rejected|auto_approved|changes_requested|escalated|withdrawn|bundle_approved)/, 'requests'],
  [/^(grant|role|admin|team|user|whitelist|auto_approve)/, 'access'],
  [/^(target|connection|schema|migration|credential)/, 'connections'],
  [/^(security_)?config|^row_limit|^kill_switch/, 'config'],
];
const QH_AUD_VERBS = [
  [/_(added|granted|registered|enabled|whitelisted|onboarded)$|_add$/, 'added'],
  [/_(revoked|removed|deleted|disabled|offboarded|exempted)$|^withdrawn$/, 'removed'],
  [/_(updated|changed|synced|set|rotated|refreshed|resealed|migrated_cloud)$|^config_change$/, 'changed'],
  [/^result_unmasked$|^result_(export|download)|_viewed$/, 'read'],
  [/^(approved|rejected|auto_approved|changes_requested|escalated|bundle_approved)/, 'decided'],
  // NEW in (c). `execution_runaway_stopped` and `execution_orphaned` were
  // neither configuration nor structure — nothing in the (b) set was about
  // *something happened during a run*. It also gives the hidden lifecycle slice
  // a name in the vocabulary: including it fills an effect that already exists
  // rather than distorting the other five.
  [/^execution_|^(submitted|started|completed|failed)$|_(submitted|started|completed|failed)$/, 'ran'],
];
function audClassify(action) {
  const n = String(action);
  const cat = QH_AUD_PATTERNS.find(p => p[0].test(n));
  const vb = QH_AUD_VERBS.find(p => p[0].test(n));
  return { category: cat ? cat[1] : 'unclassified', effect: vb ? vb[1] : 'other', classified: !!cat };
}
// Weights are CODE's measured per-kind totals (round (c) §1.1–1.3), exactly:
// requests 6,044 · usage 1,224 · protection 444 · access 373 · connections 94 ·
// config 93 · unclassified 15 = 8,287 visible.
const QH_AUD_TYPES = (() => {
  const t = (action, label, weight, actorKind) => ({ action, label, weight, actorKind, ...audClassify(action) });
  return [
    // Requests — 78% of it is the auto-approver, which is the round's biggest finding.
    t('auto_approved', 'Auto-approved query', 4468, 'auto'),
    t('auto_approved_fingerprint', 'Auto-approved · query fingerprint match', 175, 'auto'),
    t('approved', 'Approved query', 1180, 'person'),
    t('changes_requested', 'Requested changes', 88, 'person'),
    t('rejected', 'Rejected query', 53, 'person'),
    t('withdrawn', 'Withdrew own request', 34, 'person'),
    t('escalated', 'Escalated to DDL review', 32, 'person'),
    t('bundle_approved', 'Approved bundle', 5, 'person'),
    t('execution_runaway_stopped', 'Stopped a runaway query', 5, 'job'),
    t('execution_orphaned', 'Execution orphaned', 4, 'job'),
    // Usage — 1,178 of these are one action: somebody opened the /sql modal.
    t('slack_sql_opened', 'Opened the /sql modal', 1178, 'person'),
    t('effective_access_viewed', 'Viewed someone’s effective access', 46, 'person'),
    // Data protection — the larger half of the old Access bucket. One event
    // under five names is deliberately kept as five names: collapsing them in
    // the mock would hide the exact problem the screen has to survive.
    t('pii_masking_exempted', 'Masking switched off', 276, 'job'),
    t('result_exported', 'Exported result set', 145, 'person'),
    t('pii_exemption_added', 'Added masking exemption', 13, 'person'),
    t('pii_rule_updated', 'Changed a PII name rule', 4, 'person'),
    t('pii_masking_exemption_added', 'Added masking exemption', 2, 'person'),
    // Two rows on the whole trail — and that is the argument FOR the effect
    // dimension, not against it: nobody could ask this question before.
    t('result_unmasked', 'Viewed unmasked personal data', 2, 'person'),
    t('pii_exemption_changed', 'Changed masking exemption', 1, 'person'),
    t('pii_pattern_add', 'Added a PII pattern', 1, 'person'),
    // Access — what is left once usage is removed is small and all authority.
    t('grant_added', 'Granted access', 120, 'person'),
    t('whitelisted', 'Whitelisted person', 61, 'person'),
    t('grant_revoked', 'Revoked grant', 58, 'person'),
    t('admin_scope_updated', 'Updated admin scope', 44, 'person'),
    t('auto_approve_granted', 'Created auto-approve grant', 24, 'person'),
    t('role_added', 'Added role', 18, 'person'),
    t('team_updated', 'Updated team', 16, 'person'),
    t('admin_added', 'Added admin', 12, 'person'),
    t('role_revoked', 'Revoked role', 7, 'person'),
    t('team_approvers_synced', 'Synced team approvers', 5, 'job'),
    t('user_onboarded', 'Onboarded user', 4, 'person'),
    t('user_offboarded', 'Offboarded user', 4, 'person'),
    // Connections.
    t('target_disabled', 'Disabled connection', 38, 'person'),
    t('credential_rotated', 'Rotated credentials', 11, 'person'),
    t('schema_refreshed', 'Refreshed schema snapshot', 11, 'job'),
    t('target_enabled', 'Enabled connection', 7, 'person'),
    t('target_registered', 'Registered connection', 7, 'person'),
    t('target_migrated_cloud', 'Migrated target to cloud', 5, 'job'),
    t('target_credential_set', 'Set target credential', 4, 'person'),
    t('connection_updated', 'Updated connection', 3, 'person'),
    t('migration_resealed', 'Resealed migration', 3, 'job'),
    t('target_deleted', 'Deleted connection', 3, 'person'),
    t('target_test', 'Tested connection', 2, 'person'),
    // Configuration. `config_changed` and `config_change` are the same event
    // under two names — kept as two, because it is the clearest illustration of
    // why a per-action chip list cannot hold.
    t('security_config_changed', 'Changed a security setting', 68, 'job'),
    t('config_changed', 'Changed a setting', 8, 'person'),
    t('config_change', 'Changed a setting', 6, 'person'),
    t('kill_switch_engaged', 'Engaged kill switch', 4, 'person'),
    t('kill_switch_released', 'Released kill switch', 4, 'person'),
    t('row_limit_override_set', 'Set a row-limit override', 3, 'person'),
    // Unclassified — 15 rows the pattern table deliberately does not claim.
    // Its job is to be visible and embarrassing, not to be absorbed.
    t('pod_rebalance_executed', null, 4, 'job'),
    t('digest_sent', null, 3, 'job'),
    t('quota_recalculated', null, 3, 'job'),
    t('legacy_import_finished', null, 2, 'job'),
    t('slackapp_token_refreshed', null, 2, 'job'),
    t('index_advisor_ran', null, 1, 'job'),
  ];
})();
// Deterministic, so the trail does not reshuffle under the operator between
// reloads — an audit screen whose rows move is one nobody trusts.
function audRand(seed) { let s = seed >>> 0; return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 4294967296); }
// THREE actor kinds, because 61% of the trail is machine activity and rendering
// a person for a machine action states something untrue. `via` names the
// authority a machine acted under — the honest answer to "who approved this"
// when the answer is "nobody looked".
const QH_AUD_PEOPLE = [
  { handle: 'dba.amara', name: 'Amara Osei' }, { handle: 'dba.marco', name: 'Marco Young' },
  { handle: 'dana.kaur', name: 'Dana Kaur' }, { handle: 'clara.alvarez', name: 'Clara Alvarez' },
  { handle: 'elena.silva', name: 'Elena Silva' }, { handle: 'ben.donnelly', name: 'Ben Donnelly' },
  // Bare ids are resolved to names where a name exists, so what is left is a
  // principal nobody ever named. Rare, and it must never render blank.
  { handle: 'U07QK4T2X', name: null },
];
const QH_AUD_JOBS = ['svc-migrator', 'pii-backfill', 'schema-crawler', 'security-baseline'];
// A requester is a principal id and, usually, a name. `request.requester` is
// the NAME and is null when none was recorded — never the string "—", which is
// what it used to be and what made this column impossible to fall back from;
// `request.requesterId` carries the raw principal id beside it. One seed has no
// name, because the fallback only exists on screen if a row exercises it.
const QH_AUD_REQUESTERS = [
  { handle: 'elena.silva', name: 'Elena Silva' },
  { handle: 'chen.yu', name: 'Chen Yu' },
  { handle: 'ben.donnelly', name: 'Ben Donnelly' },
  { handle: 'sam.archer', name: 'Sam Archer' },
  { handle: 'omar.kane', name: 'Omar Kane' },
  { handle: 'maya.andersen', name: 'Maya Andersen' },
  { handle: 'U08RQ2M4V', name: null },
];
// Payload sizes, measured (§1.6): average 60 characters, 25 rows over 400, the
// longest visible 1,086 — and 9,882 in the hidden slice. All four are seeded,
// because the rail has to be judged against the extremes, not the average.
const AUD_LONG = 'Raised after the weekly reconciliation job timed out four times in a row. p95 of successful runs on that connection is 41s and p99 is 96s, so 120s clears both with headroom; the previous 30s was set in 2024 when nothing on this fleet ran longer than a lookup. Reverting is safe — nothing depends on the higher ceiling except that job, and the job is idempotent. Checked with the payments team before applying: they confirmed no downstream consumer reads the intermediate table while the job holds it. Rolled out to prod-main first and watched for an hour before the rest of the fleet. No change to the statement timeout on any replica, which stays at 30s deliberately so a runaway analytical query cannot sit on a replica for two minutes. The reconciliation job is the only consumer that has ever needed more than thirty seconds, and it runs once a week at 03:00 against a connection nothing else reads at that hour, so the wider ceiling is not exposed to interactive traffic at all. Reviewed with the DBA on call and recorded here rather than in the ticket, because the ticket will be closed long before anybody asks why this number is what it is.';
function audDetail(ty, rnd, ctx) {
  const a = ty.action;
  if (/^(security_)?config|^row_limit/.test(a)) {
    const k = ['statement_timeout_ms', 'max_rows_returned', 'pii_mask_on_return', 'approval_reminder_minutes', 'export_row_cap'][Math.floor(rnd() * 5)];
    const o = { setting: k, from: String(Math.floor(rnd() * 30000)), to: String(Math.floor(rnd() * 30000)), scope: 'fleet-wide' };
    // ~25 of the 93 config rows carry the paragraph, which is CODE's measured
    // count of payloads over 400 characters.
    if (rnd() > 0.73) o.note = AUD_LONG;
    return o;
  }
  if (/^pii_/.test(a)) return { scope: 'column', strength: rnd() > 0.7 ? 'full' : 'soft', survivesJoin: rnd() > 0.6,
    column: ctx.conn + '/' + ctx.db + '.users.address',
    reason: 'Holds the counterparty wallet address, not a postal one. 42 of 51 queries in March joined customers, so it survives joins.' };
  if (a === 'result_unmasked') return { requestId: ctx.reqId, columns: ['email', 'tckn'], rowsRevealed: Math.floor(rnd() * 200) + 1, justification: 'Compliance case #4471' };
  if (/^result_export/.test(a)) return { format: rnd() > 0.5 ? 'xlsx' : 'csv', rows: Math.floor(rnd() * 5000), maskedColumns: Math.floor(rnd() * 4) };
  if (a === 'slack_sql_opened') return { channel: ['#data-requests', '#backend', '#compliance'][Math.floor(rnd() * 3)] };
  if (a === 'effective_access_viewed') return { subject: ctx.requester };
  // Approvals are tested BEFORE the access branch: `auto_approved` starts with
  // "auto_" and the access pattern would claim it, giving 4,643 approval rows a
  // GRANT payload — an `expiresAt` on an approval, and a `databases` naming a
  // different target from the request block in the same panel. Two thirds of the
  // default view, on the one panel this round exists to get right.
  if (/^(approved|rejected|auto_approved|changes_requested|escalated|withdrawn|bundle)/.test(a)) return { tier: ctx.tier, statements: 1 + Math.floor(rnd() * 3), piiColumns: rnd() > 0.7 ? ['email'] : [] };
  if (/^(grant|role|admin|auto_approve_granted|whitelist)/.test(a)) return { subject: ctx.requester, tier: ctx.tier, databases: [ctx.db], expiresAt: rnd() > 0.7 ? isoIn(1000 * 86400 * 30) : null };
  if (/^(target|connection|credential)/.test(a)) return { connection: ctx.conn, host: ctx.conn + '.db.example.internal:5432' };
  if (/^(schema|migration)/.test(a)) return { connection: ctx.conn, tables: Math.floor(rnd() * 400), durationMs: Math.floor(rnd() * 90000) };
  if (/^kill_switch/.test(a)) return { scope: 'fleet-wide', message: 'Payments incident — holding all writes until the gateway is confirmed healthy.' };
  if (/^execution_/.test(a)) return { requestId: ctx.reqId, stoppedAfterMs: Math.floor(rnd() * 600000) + 60000, reason: 'Exceeded the statement timeout and held a lock on payouts.' };
  if (/^team_/.test(a)) return { team: ['data-eng', 'backend', 'compliance'][Math.floor(rnd() * 3)], members: Math.floor(rnd() * 12) + 1 };
  // 60 visible rows carry no payload at all.
  return rnd() > 0.5 ? {} : { note: 'No structured payload was recorded for this action type.' };
}
const MOCK_AUDIT_TRAIL = (() => {
  const rnd = audRand(20260909);
  const conns = ['prod-main', 'prod-replica', 'reporting-mssql', 'svc-prod-billing', 'staging', 'svc-prod-orders'];
  const dbs = ['payments', 'users', 'analytics', 'billing_service', 'app_stg', 'orders_service'];
  const tiers = ['RO', 'RO', 'RO', 'RW', 'RW', 'DDL'];
  const sqls = ["SELECT id, email, kyc_status FROM users WHERE kyc_status = $1 LIMIT 200;",
    "UPDATE payouts SET status = 'retry' WHERE status = 'failed' AND created_at::date = current_date;",
    "ALTER TABLE events ADD COLUMN device_fingerprint text;",
    "DELETE FROM invoices WHERE status = 'draft' AND created_at < now() - interval '90 days';"];
  const pool = [];
  QH_AUD_TYPES.forEach(ty => { for (let i = 0; i < ty.weight; i++) pool.push(ty); });
  const rows = pool.map((ty, i) => {
    // Actor kind is a property of the action type, not a coin flip: an approval
    // is either the auto-approver's or a person's, and 78% of Requests is the
    // auto-approver. A few job rows land on person-typed admin actions.
    let kind = ty.actorKind;
    if (kind === 'person' && /^(pii_|security_)/.test(ty.action) && rnd() > 0.7) kind = 'job';
    const p = QH_AUD_PEOPLE[Math.floor(rnd() * QH_AUD_PEOPLE.length)];
    const conn = conns[Math.floor(rnd() * conns.length)], db = dbs[Math.floor(rnd() * dbs.length)];
    const tier = tiers[Math.floor(rnd() * tiers.length)];
    const rq = QH_AUD_REQUESTERS[Math.floor(rnd() * QH_AUD_REQUESTERS.length)];
    const requester = rq.handle;
    const reqId = String(1200 + Math.floor(rnd() * 800));
    const actor = kind === 'auto'
      ? { kind: 'auto', handle: 'auto-approver', name: null,
          via: ty.action === 'auto_approved_fingerprint'
            ? 'query fingerprint matched an earlier approval'
            : 'auto-approve window · ' + requester + ' → ' + conn + ' ' + tier }
      : kind === 'job'
        ? { kind: 'job', handle: QH_AUD_JOBS[Math.floor(rnd() * QH_AUD_JOBS.length)], name: null, via: null }
        : { kind: 'person', handle: p.handle, name: p.name, via: null };
    // 77% of rows attach to a request (§1.5 — the (b) brief had this the wrong
    // way round). Usage, connections and config rows are the 23% that do not.
    const bound = ty.category === 'requests' || ty.category === 'protection'
      || (ty.category === 'access' && rnd() > 0.72);
    const ctx = { conn, db, tier, requester, reqId };
    return { id: 'aa_' + (i + 1),
      time: isoAgo(Math.floor(rnd() * 1000 * 86400 * 92) + 1000 * 60 * 3),
      actor, action: ty.action, actionLabel: ty.label, category: ty.category, effect: ty.effect,
      classified: ty.classified,
      target: ty.category === 'requests' ? requester + ' · ' + conn + '/' + db
        : ty.category === 'config' ? 'fleet-wide'
          : ty.category === 'connections' ? conn
            : ty.category === 'protection' ? conn + '/' + db + '.users.address'
              : ty.category === 'usage' ? (actor.name || actor.handle)
                : requester + ' → ' + conn,
      detail: audDetail(ty, rnd, ctx),
      request: bound ? { id: reqId, requester: rq.name, requesterId: rq.handle, connection: conn, database: db, tier,
        rows: Math.floor(rnd() * 4000), durationMs: Math.floor(rnd() * 12000) + 60,
        sql: sqls[Math.floor(rnd() * sqls.length)] } : null,
      slice: null };
  });
  // The hidden slice: 17,773 rows, 9 action types, 2.1x the visible trail. Kept
  // lean because "include them" triples the list and the screen has to render
  // that without the mock becoming the bottleneck.
  const life = [];
  const LIFE = [['submitted', 4600], ['started', 4600], ['completed', 4290], ['failed', 396],
    ['import_submitted', 1300], ['import_completed', 1240], ['schedule_started', 700],
    ['schedule_completed', 640], ['schedule_failed', 7]];
  LIFE.forEach(([a, n]) => {
    for (let i = 0; i < n; i++) {
      const conn = conns[Math.floor(rnd() * conns.length)], db = dbs[Math.floor(rnd() * dbs.length)];
      const rq = QH_AUD_REQUESTERS[Math.floor(rnd() * QH_AUD_REQUESTERS.length)];
      const requester = rq.handle;
      const cl = audClassify(a);
      life.push({ id: 'al_' + a + '_' + i, time: isoAgo(Math.floor(rnd() * 1000 * 86400 * 92) + 1000 * 60 * 3),
        actor: { kind: 'person', handle: requester, name: null, via: null }, action: a,
        actionLabel: 'Request ' + a.replace(/_/g, ' '), category: cl.category, effect: cl.effect,
        classified: cl.classified, target: requester + ' · ' + conn + '/' + db,
        // The 9,882-character payload lives here, on exactly one row: if the
        // include control ever exposes this slice the rail has to survive it.
        detail: (a === 'failed' && i === 0)
          ? { stage: a, requestId: '1741', error: AUD_LONG.repeat(14).slice(0, 9882) }
          : { stage: a },
        request: { id: String(1200 + Math.floor(rnd() * 800)), requester: rq.name, requesterId: rq.handle, connection: conn, database: db,
          tier: tiers[Math.floor(rnd() * tiers.length)], rows: null, durationMs: null, sql: sqls[0] },
        slice: 'lifecycle' });
    }
  });
  return rows.concat(life).sort((a, b) => (a.time < b.time ? 1 : -1));
})();
// The two declared exclusions. A LIST, not two sentences: a third one will
// arrive, and the scope line has to stay honest without growing a paragraph.
// `usage` is a real kind when included — an auditor never arrives asking about
// it, but "what was this person doing around the incident" is a real question
// and a kind that cannot be switched on cannot answer it.
const QH_AUD_SLICES = [
  { id: 'lifecycle', label: 'per-request lifecycle', sub: 'the queue and history screens show these request by request',
    match: (r) => r.slice === 'lifecycle' },
  { id: 'usage', label: 'product usage', sub: 'somebody opened a screen — no authority changed',
    category: 'usage', match: (r) => r.category === 'usage' },
];
function audSearch(p) {
  const o = p || {};
  const term = String(o.q || '').trim().toLowerCase();
  const cats = o.categories && o.categories.length ? o.categories : null;
  const effs = o.effects && o.effects.length ? o.effects : null;
  const actors = o.actorKinds && o.actorKinds.length ? o.actorKinds : null;
  const include = o.include || [];
  const from = o.from ? Date.parse(o.from) : null;
  const to = o.to ? Date.parse(o.to) : null;
  const hidden = QH_AUD_SLICES.filter(s => !include.includes(s.id));
  const base = MOCK_AUDIT_TRAIL.filter(r => !hidden.some(s => s.match(r)));
  const hay = (r) => [r.actor.handle, r.actor.name, r.actor.via, r.action, r.actionLabel, r.target,
    r.request && r.request.requester, r.request && r.request.requesterId,
    r.request && r.request.database, r.request && r.request.connection,
    r.request && r.request.sql, r.request && r.request.id, JSON.stringify(r.detail)]
    .filter(Boolean).join(' ').toLowerCase();
  const searched = term ? base.filter(r => hay(r).includes(term)) : base;
  const timed = searched.filter(r => {
    const t = Date.parse(r.time);
    return (from == null || t >= from) && (to == null || t <= to);
  });
  // Facets are counted BEFORE the kind/effect/actor filter, so every chip can
  // state the count it would produce.
  const facets = { categories: {}, effects: {}, actorKinds: {} };
  timed.forEach(r => {
    facets.categories[r.category] = (facets.categories[r.category] || 0) + 1;
    facets.effects[r.effect] = (facets.effects[r.effect] || 0) + 1;
    facets.actorKinds[r.actor.kind] = (facets.actorKinds[r.actor.kind] || 0) + 1;
  });
  const rows = timed.filter(r => (!cats || cats.includes(r.category))
    && (!effs || effs.includes(r.effect)) && (!actors || actors.includes(r.actor.kind)));
  const off = o.cursor ? Number(o.cursor) : 0;
  const limit = Math.min(Number(o.limit) || 60, 300);
  const seen = {};
  base.forEach(r => { seen[r.action] = r.classified; });
  const names = Object.keys(seen);
  return {
    rows: rows.slice(off, off + limit),
    cursor: off + limit < rows.length ? String(off + limit) : null,
    matched: rows.length,
    searchTotal: timed.length,
    total: base.length,                       // what the screen can see right now
    grandTotal: MOCK_AUDIT_TRAIL.length,      // what the table holds
    facets,
    actionTypes: { total: names.length, unclassified: names.filter(n => !seen[n]).length },
    // Each exclusion states what INCLUDING it would add, so "show everything"
    // cannot be mistaken for a few more rows when it triples the list.
    exclusions: QH_AUD_SLICES.map(s => ({ id: s.id, label: s.label, sub: s.sub,
      // The kind this slice IS, when it is one. Lets the screen connect the two
      // controls instead of showing a kind chip reading 0 next to an exclusion
      // saying +1,224 — two true numbers that read as a contradiction.
      category: s.category || null,
      included: include.includes(s.id),
      rows: MOCK_AUDIT_TRAIL.filter(r => s.match(r)).length })),
  };
}

// ---------- MOCK: masking exemptions (design 2026-09-09) ----------
// Masking decides two ways and both over-reach: NAME rules ("a column called
// `address` holds a postal address") and six VALUE detectors reading the cell
// content. An exemption is the operator's correction. All 30 that exist today
// were typed into psql by hand, which is what the screen replaces.
//
// Field names are the ones asked of CODE (DESIGN_TO_CODE_BRIEF § 2026-09-09) —
// invented here so the prototype has something to render, and named for what
// they mean rather than for how the resolver stores them:
//   scope        'column' | 'schema' | 'database' | 'server' — a ladder
//   strength     'soft' (stop matching the NAME, keep reading values) | 'full'
//   survivesJoin does it hold when the query joins a still-masked table
//   audience     'everyone' | 'super'
// `scope` is sent EXPLICITLY rather than inferred from which of table/column is
// null: the difference between "one column" and "this whole server" must not be
// a blank field, on the read side either (2026-09-07 §3, same lesson).
const MASK_DETECTORS = ['email', 'IBAN', 'card number', 'TCKN', 'VKN', 'passport'];
// Tables the story needs that the sample fleet does not have. They are folded
// into the catalog so the picker can reach them — an exemption pointing at a
// table the catalog does not know is the `missing` state, and it must be a
// state we CHOSE, not an accident of the seed data.
const MASK_STORY_TABLES = {
  'prod-main/payments': { wallet_screening: ['address', 'chain', 'risk_score'], merchant_venues: ['name', 'city', 'mcc'] },
  'prod-main/users': { crm_notes: ['full_name', 'note', 'author'] },
  'reporting-mssql/ReportingDW': { MetaColumns: ['table_name', 'column_name', 'data_type'] },
};
function maskSchemas(connId, dbId) {
  const s = ['public'];
  if (connId === 'reporting-mssql') return ['dbo'];
  if (/user/.test(dbId)) s.push('kyc');
  if (/payment|billing/.test(dbId)) s.push('finance');
  return s;
}
function maskHash(s) { let h = 0; for (let i = 0; i < String(s).length; i++) h = (h * 31 + String(s).charCodeAt(i)) >>> 0; return h; }
// The database's NAME, which is what makes two connections hold "the same"
// database — the ids differ (`users` vs `users_ro`).
function maskDbName(connId, dbId) {
  const c = (window.QH_CONNECTIONS || []).find(x => x.id === connId);
  const d = c && (c.databases || []).find(x => x.id === dbId);
  return d ? d.name : null;
}
// The catalog the picker reads. Columns carry the rule that currently catches
// them, because half the exemptions written are for a column somebody was
// surprised to see masked and the surprise comes from not knowing which rule
// fired (brief § "Show what a column is currently masked as and why").
function maskCatalogFor(connId, dbId) {
  if (!ADMIN.mask) ADMIN.mask = maskSeed();
  const raw = (window.QH_CONNECTIONS || []).find(c => c.id === connId);
  if (!raw) return null;
  const db = (raw.databases || []).find(d => d.id === dbId || d.name === dbId);
  if (!db) return null;
  const cat = window.QH_PII_CATALOG || {};
  const story = MASK_STORY_TABLES[connId + '/' + db.id] || {};
  // Columns named by an exemption exist by definition — the row is enforced
  // against them today. Matched on the database NAME rather than its id, so the
  // same logical database on a sibling connection (a primary and its migrated
  // copy) yields the same catalog: `alsoOn` asserts a column is on the sibling,
  // and the picker there has to be able to offer it. Two sources of truth
  // disagreeing is how *Add it there* prefilled a column the picker never had.
  const extra = (ADMIN.mask || []).filter(e => e.scope === 'column' && !e._missing && e.table
    && (e.connectionId === connId ? e.databaseId === db.id : maskDbName(e.connectionId, e.databaseId) === db.name));
  const schemas = maskSchemas(connId, db.id);
  const colObj = (name) => {
    const g = (window.qhColumnsFor ? window.qhColumnsFor('x') : []).find(c => c.name === name);
    const r = cat[name];
    return { name, type: (g && g.type) || (/_at$/.test(name) ? 'timestamptz' : 'text'),
      rule: r ? { key: name, label: r.label, mask: r.mask } : null };
  };
  return {
    schemas: schemas.map((sc, i) => {
      const tables = (i === 0 ? (db.tables || []) : (db.tables || []).slice(0, 2).map(t => t + '_history')).slice();
      if (i === 0) Object.keys(story).forEach(t => { if (tables.indexOf(t) < 0) tables.push(t); });
      return { name: sc, tables: tables.map(t => {
        const base = (window.qhColumnsFor ? window.qhColumnsFor(t) : []).map(c => ({ name: c.name, type: c.type,
          rule: cat[c.name] ? { key: c.name, label: cat[c.name].label, mask: cat[c.name].mask } : null }));
        const names = base.map(c => c.name);
        (story[t] || []).forEach(n => { if (names.indexOf(n) < 0) { base.push(colObj(n)); names.push(n); } });
        // Anything an exemption already points at exists by definition — the
        // row is enforced against it today.
        extra.filter(e => e.table === t && e.schema === sc).forEach(e => {
          if (names.indexOf(e.column) < 0) { base.push(colObj(e.column)); names.push(e.column); }
        });
        return { name: t, columns: base };
      }) };
    }),
  };
}
// 30 rows across 7 servers, because that is the operator's fleet today. Ten are
// written out — every state the screen has to draw has a seed, or the state only
// exists in the code that renders it. The rest are filler so the list is the
// length the real one is: a design that reads well at 4 rows and not at 30 has
// not been tested against the thing it replaces.
function maskSeed() {
  const D = (n) => new Date(Date.now() - n * 86400000).toISOString();
  const rows = [
    { connectionId: 'prod-main', databaseId: 'payments', schema: 'public', table: 'wallet_screening', column: 'address',
      scope: 'column', strength: 'soft', survivesJoin: true, audience: 'everyone', enabled: true, createdBy: 'dba.amara', createdAt: D(94),
      reason: 'Holds the counterparty wallet address, not a postal one. Survives joins because 42 of the 51 queries written against this table in March joined customers — re-masking made the exemption useless in practice.' },
    { connectionId: 'prod-main', databaseId: 'payments', schema: 'public', table: 'merchant_venues', column: 'name',
      scope: 'column', strength: 'soft', survivesJoin: false, audience: 'everyone', enabled: true, createdBy: 'dba.amara', createdAt: D(71),
      reason: 'Venue label (“Harbour Branch”), not a person’s name.' },
    { connectionId: 'reporting-mssql', databaseId: 'ReportingDW', schema: 'dbo', table: 'MetaColumns', column: 'table_name',
      scope: 'column', strength: 'full', survivesJoin: true, audience: 'everyone', enabled: true, createdBy: 'dba.marco', createdAt: D(120),
      reason: 'Database metadata. `table_name` is a catalog column — it has never held a person and the value detectors have nothing to find in it.' },
    { connectionId: 'prod-main', databaseId: 'users', schema: 'kyc', table: 'crm_notes', column: 'full_name',
      scope: 'column', strength: 'full', survivesJoin: false, audience: 'super', enabled: true, createdBy: 'dba.amara', createdAt: D(38),
      reason: 'Case handlers cannot work the queue without the name. Restricted to super-admins until the CRM migration lands.' },
    { connectionId: 'prod-main', databaseId: 'users', schema: 'public', table: 'users', column: 'address',
      scope: 'column', strength: 'soft', survivesJoin: false, audience: 'everyone', enabled: true, createdBy: 'dba.marco', createdAt: D(52),
      reason: 'Delivery city only; the street line moved to a separate table in 2024.' },
    { connectionId: 'svc-prod-reporting', databaseId: 'reporting_service', schema: 'public', table: null, column: null,
      scope: 'schema', strength: 'soft', survivesJoin: false, audience: 'everyone', enabled: true, createdBy: 'dba.marco', createdAt: D(61),
      reason: 'Every column in this schema is a lookup code. Kept soft so a stray email in a free-text field is still caught.' },
    { connectionId: 'prod-replica', databaseId: 'analytics_ro', schema: null, table: null, column: null,
      scope: 'database', strength: 'full', survivesJoin: true, audience: 'everyone', enabled: true, createdBy: 'dba.amara', createdAt: D(150),
      reason: 'Rollups only — every table here is aggregated, with no row-level person data. Masking here only ever produced [REDACTED] inside metric labels.' },
    { connectionId: 'staging', databaseId: 'app_stg', schema: null, table: null, column: null,
      scope: 'database', strength: 'full', survivesJoin: true, audience: 'everyone', enabled: true, createdBy: 'dba.marco', createdAt: D(210),
      reason: 'Staging is seeded by the fixtures job — synthetic data only, no production copy has ever been loaded into it.' },
    { connectionId: 'svc-prod-registry', databaseId: null, schema: null, table: null, column: null,
      scope: 'server', strength: 'full', survivesJoin: true, audience: 'everyone', enabled: true, createdBy: 'dba.amara', createdAt: D(180),
      reason: 'Public reference registry, mirrored from the open dataset. Reviewed by Legal on 2026-03-11; nothing in it is personal.' },
    // The widest row on the fleet, and the reason the ladder grew a rung it did
    // not have (CODE round (c) item 2): target NULL means every server.
    { connectionId: null, databaseId: null, schema: 'dba', table: null, column: null,
      scope: 'fleet', strength: 'full', survivesJoin: true, audience: 'super', enabled: true, createdBy: 'dba.amara', createdAt: D(300),
      reason: 'The `dba` schema is our own operational metadata on every server — job state, lock snapshots, catalog dumps. Masking it produced [REDACTED] inside the tooling that diagnoses the fleet. Restricted to super-admins and reviewed each quarter.' },
    { connectionId: 'prod-replica', databaseId: 'users_ro', schema: 'public', table: 'user_kyc', column: 'passport_no',
      scope: 'column', strength: 'full', survivesJoin: false, audience: 'everyone', enabled: true, createdBy: 'dba.marco', createdAt: D(240), _missing: true,
      reason: 'Passport capture was dropped from KYC in January.' },
    { connectionId: 'svc-prod-orders', databaseId: 'orders_service', schema: 'public', table: 'shipments', column: 'recipient_name',
      scope: 'column', strength: 'soft', survivesJoin: false, audience: 'everyone', enabled: true, createdBy: 'dba.amara', createdAt: D(160), _missing: true,
      reason: 'Renamed to `consignee` in the 2026-05 migration.' },
    { connectionId: 'prod-main', databaseId: 'analytics', schema: 'public', table: 'cohorts', column: 'email',
      scope: 'column', strength: 'full', survivesJoin: false, audience: 'everyone', enabled: false, createdBy: 'dba.amara', createdAt: D(88),
      reason: 'Opened for the churn model. The model reads a hash now — turned off rather than deleted so the decision stays on the record.' },
  ];
  // Filler: real targets on the service fleet, so the list is 30 rows long and
  // the grouping has something to group.
  const fleet = (window.QH_CONNECTIONS || []).filter(c => /^svc-prod-/.test(c.id) && c.id !== 'svc-prod-registry');
  const cols = ['email', 'full_name', 'address', 'iban', 'phone', 'card_no', 'tckn'];
  fleet.forEach((c, i) => {
    const db = c.databases[0];
    const n = i < 5 ? 2 : 1;   // 30 rows in total, which is the number that exists today
    for (let k = 0; k < n; k++) {
      rows.push({ connectionId: c.id, databaseId: db.id, schema: 'public', table: db.tables[(i + k) % db.tables.length], column: cols[(i + k * 3) % cols.length],
        scope: 'column', strength: (i + k) % 4 === 0 ? 'full' : 'soft', survivesJoin: (i + k) % 3 === 0, audience: 'everyone',
        enabled: true, createdBy: (i + k) % 2 ? 'dba.amara' : 'dba.marco', createdAt: D(20 + i * 7 + k),
        reason: 'Service-owned lookup column; the name rule catches it but the values are internal identifiers.' });
    }
  });
  return rows.map((r, i) => ({ id: 'mx_' + (i + 1), ...r })).slice(0, 30);
}
// The same database usually lives on more than one server (a primary and a
// migrated copy) and an exemption on one is routinely missing on the other.
// Surfaced as a SUGGESTION on the row rather than an error: the operator may
// have meant exactly one of them, and a screen that calls a deliberate choice a
// mistake gets its warnings ignored.
function maskAlsoOn(e, all) {
  if (e.scope !== 'column') return [];
  const raw = window.QH_CONNECTIONS || [];
  const mine = raw.find(c => c.id === e.connectionId);
  const dbName = ((mine && (mine.databases || []).find(d => d.id === e.databaseId)) || {}).name;
  if (!dbName) return [];
  return raw.filter(c => c.id !== e.connectionId)
    .map(c => ({ c, d: (c.databases || []).find(d => d.name === dbName && (d.tables || []).indexOf(e.table) >= 0) }))
    .filter(x => x.d)
    .filter(x => !all.some(o => o.connectionId === x.c.id && o.databaseId === x.d.id && o.table === e.table && o.column === e.column))
    .map(x => ({ connectionId: x.c.id, connectionName: x.c.name, databaseId: x.d.id }));
}
// The audit line names the target at its rung, so a server-wide row and a
// column row cannot read alike in the log either.
function maskAuditTarget(e) {
  if (e.scope === 'fleet') return 'every server' + (e.schema ? ' · schema ' + e.schema : '') + ' (fleet-wide)';
  if (e.scope === 'server') return e.connectionId + ' (whole server)';
  if (e.scope === 'database') return e.connectionId + '/' + e.databaseId + ' (whole database)';
  if (e.scope === 'schema') return e.connectionId + '/' + e.databaseId + '.' + e.schema + ' (whole schema)';
  if (e.scope === 'table') return e.connectionId + '/' + e.databaseId + ' ' + e.schema + '.' + e.table + ' (whole table)';
  return e.connectionId + '/' + e.databaseId + ' ' + e.schema + '.' + e.table + '.' + e.column;
}
function maskRegistry() {  if (!ADMIN.mask) ADMIN.mask = maskSeed();
  const cat = window.QH_PII_CATALOG || {};
  const reg = connRegistry();
  return ADMIN.mask.map(e => {
    const c = reg.find(x => x.id === e.connectionId) || {};
    const db = (c.databases || []).find(d => d.id === e.databaseId);
    const r = e.column ? cat[e.column] : null;
    return { ...e,
      // The wildcards are NAMED on the wire rather than served as blanks: a
      // fleet-wide row whose connection is null must not render the same as a
      // row whose connection the server failed to fill (2026-09-07 §3).
      connectionName: e.scope === 'fleet' ? 'every server' : (c.name || e.connectionId),
      env: c.env || null,
      databaseName: e.scope === 'fleet' || e.scope === 'server' ? 'every database' : (db ? db.name : e.databaseId),
      // Which named rule catches this column today. Null on a wider rung — a
      // schema exemption is not answering a question about one rule.
      maskedBy: r ? { key: e.column, label: r.label, mask: r.mask } : null,
      missing: !!e._missing,
      alsoOn: maskAlsoOn(e, ADMIN.mask),
    };
  });
}
// The preview. The honest question is "what will people see that they cannot see
// now?", and answering it today means a psql session plus knowing which of two
// internal resolvers applies. So: a REAL recent query against that table, one
// sample row masked both ways. When nothing has queried the table, say so —
// an empty box reads as a broken preview, not as an absence of evidence.
function maskSample(col, h) {
  const s = {
    email: ['ada.dunn@example.com', 'm.kent@example.com'], full_name: ['Ada Dunn', 'Milo Kent'],
    address: ['bc1qar0srrr7xfkvy5l643lydnw9re59gtzz', '0x71C7656EC7ab88b098defB751B7401B5f6d8976F'],
    iban: ['TR33 0006 1005 1978 6457 8413 26', 'TR64 0001 0021 8712 3456 7890 12'],
    card_no: ['5218 7612 3456 7890', '4506 3477 1234 5678'], tckn: ['12345678901', '98765432109'],
    phone: ['+90 532 111 22 33', '+90 555 444 33 22'], name: ['Harbour Branch', 'Hillside Office'],
    table_name: ['pg_class', 'transactions'],
  }[col];
  if (s) return s[h % s.length];
  if (/hash|tx/.test(col)) return '0x' + (h.toString(16) + 'a3f19c4b2e').slice(0, 12);
  if (/height|qty|count|score|amount|price/.test(col)) return String(1000 + (h % 90000));
  if (/_at$|date/.test(col)) return new Date(Date.now() - (h % 30) * 86400000).toISOString().slice(0, 10);
  if (/status|state|side|type|chain/.test(col)) return ['active', 'pending', 'settled'][h % 3];
  return 'tr-0' + (100 + (h % 800));
}
function maskMaskedForm(col, val) {
  const c = (window.QH_PII_CATALOG || {})[col];
  if (!c) return val;
  if (c.mask === 'full') return '••••••••';
  const s = String(val);
  return s.slice(0, 2) + '•'.repeat(Math.max(3, s.length - 4)) + s.slice(-2);
}

// Resolve one person's REAL reach. Field names are the server's (CODE brief
// 2026-08-21 (c)+(d)): `access` / `autoApprove` / `admin` / `rowLimitOverride`,
// and `teams` is objects rather than strings.
// **Their own grant displaces the team's on the whole SERVER** (rule 4, CODE
// 2026-09-24 (b)): an own grant on any database of a connection means the
// team's grants reach them on no database of it — unless that own grant is set
// to merge with the team (`mergeWithTeam`), when the two add up per database.
// That is the picker's and submit's rule, so the screen now agrees with what
// they can select. An ENDED own grant still decides (rule 2): no access, not a
// fall-through — but only when nothing of theirs is live on that connection.
// The team view reads the same helper, or the two halves would disagree.
const EFF_RANK = { RO: 0, RW: 1, DDL: 2 };
const effLive = (g) => !g.expiresAt || new Date(g.expiresAt).getTime() > Date.now();
const effDbs = (g) => (!g.databases || !g.databases.length || g.databases.indexOf('*') >= 0) ? ['*'] : g.databases.slice();
const effCovers = (g, db) => { const d = effDbs(g); return d[0] === '*' || d.indexOf(db) >= 0; };
// One connection. `own` = this person's rows there, live AND ended; `team` =
// live team rows. Returns database ('*' = every one) → decision.
function effDecide(own, team) {
  const out = {};
  const best = (gs, db) => gs.filter(g => effCovers(g, db)).sort((a, b) => (EFF_RANK[b.tier] - EFF_RANK[a.tier]) || (a.subject < b.subject ? -1 : 1))[0];
  const keysOf = (gs) => { const k = new Set(); gs.forEach(g => effDbs(g).forEach(d => k.add(d))); return [...k]; };
  const displacing = own.filter(g => !g.mergeWithTeam);
  const liveOwn = own.filter(effLive);
  if (displacing.length) {
    if (liveOwn.length) {
      // Theirs decides every database; the team's reach nothing here.
      keysOf(liveOwn).forEach(db => { const g = best(liveOwn, db); out[db] = { source: 'user', tier: g.tier, team: null, expiresAt: g.expiresAt || null }; });
    } else {
      // Nothing of theirs is live: the ended grant is the answer on every
      // database the team would have given them, and on its own.
      const ended = displacing.slice().sort((a, b) => (a.expiresAt < b.expiresAt ? 1 : -1))[0];
      keysOf(team.concat(displacing)).forEach(db => { out[db] = { blocked: true, tier: ended.tier, expiresAt: ended.expiresAt }; });
    }
    return out;
  }
  // Merging own grants (or none): per database, the higher of theirs and the
  // teams'. Several teams merge into one decision, so ONE team is reported —
  // the highest tier, then by name.
  keysOf(liveOwn.concat(team)).forEach(db => {
    const o = best(liveOwn, db), t = best(team, db);
    if (o && (!t || EFF_RANK[o.tier] >= EFF_RANK[t.tier])) out[db] = { source: 'user', tier: o.tier, team: null, expiresAt: o.expiresAt || null };
    else if (t) out[db] = { source: 'team', tier: t.tier, team: t.subject, expiresAt: t.expiresAt || null };
  });
  return out;
}
// Decisions → rows: one per (source, tier, team) on a connection, each with a
// `key`, because `connectionId` repeats when the tiers differ (CODE §4).
function effRows(cid, dec) {
  const m = {};
  Object.keys(dec).forEach(db => {
    const d = dec[db];
    if (d.blocked) return;
    const k = cid + ':' + d.source + ':' + d.tier + (d.team ? ':' + d.team : '');
    const r = m[k] || (m[k] = { key: k, tier: d.tier, dbs: [], source: d.source, sourceTeam: d.team || null, expiresAt: d.expiresAt });
    r.dbs.push(db);
    if (d.expiresAt && (!r.expiresAt || d.expiresAt < r.expiresAt)) r.expiresAt = d.expiresAt;
  });
  return Object.keys(m).map(k => { const r = m[k], all = r.dbs.indexOf('*') >= 0;
    return { key: r.key, connectionId: cid, tier: r.tier, databases: all ? null : r.dbs, allDatabases: all,
      source: r.source, sourceTeam: r.sourceTeam, expiresAt: r.expiresAt || null }; });
}
function effectiveFor(p) {
  const teamRows = ADMIN.teams.filter(t => (t.members || []).indexOf(p.handle) >= 0);
  const teams = teamRows.map(t => t.name);
  const own = ADMIN.grants.filter(g => g.subjectType === 'user' && g.subject === p.handle);
  const team = ADMIN.grants.filter(g => g.subjectType === 'team' && teams.indexOf(g.subject) >= 0 && effLive(g));
  let access = [];
  // `blocked` (CODE 2026-09-23 (b) §2): where their own ENDED grant is why a
  // team's does not reach them — the same decision the team view's
  // `overriddenFor` with `expired: true` is read from. Only where a team
  // grants the database; an ended grant with no team behind it is just ended.
  const blockedBy = {};
  [...new Set(own.concat(team).map(g => g.connectionId))].forEach(cid => {
    const tg = team.filter(g => g.connectionId === cid);
    const dec = effDecide(own.filter(g => g.connectionId === cid), tg);
    access = access.concat(effRows(cid, dec));
    Object.keys(dec).forEach(db => {
      const d = dec[db];
      if (!d.blocked) return;
      const t = tg.find(g => db === '*' ? true : effCovers(g, db));
      if (!t) return;
      const k = cid + ':' + d.expiresAt + ':' + t.subject;
      (blockedBy[k] = blockedBy[k] || { connectionId: cid, databases: [], endedAt: d.expiresAt, team: t.subject }).databases.push(db);
    });
  });
  const sc = ADMIN.scopes.find(s => s.admin === p.handle) || null;
  const ceiling = (sc ? (sc.canApprove || []) : []).slice().sort((a, b) => EFF_RANK[b] - EFF_RANK[a])[0] || null;
  const scopeTargets = sc ? ((sc.connections || []).indexOf('*') >= 0 ? [] : (sc.connections || [])) : [];
  // An admin reaches a target without a row anywhere. `admin_or_bypass` is that
  // third source — rendering it as "direct" would be true and not the truth.
  if (sc) {
    (scopeTargets.length ? scopeTargets : connRegistry().filter(c => !c.replicaOf).map(c => c.id)).forEach(cid => {
      if (!access.some(t => t.connectionId === cid)) access.push({ key: cid + ':admin', connectionId: cid, tier: ceiling || 'RO', databases: null,
        allDatabases: true, source: 'admin_or_bypass', sourceTeam: null, expiresAt: null });
    });
  }
  return {
    person: { id: p.id, handle: p.handle, name: p.name },
    teams: teamRows.map(t => ({ id: t.id, name: t.name })),
    access,
    // An admin gets [] — no grant is consulted for them.
    blocked: sc && sc.role === 'super' ? [] : Object.keys(blockedBy).map(k => blockedBy[k]),
    // An auto-approve window with no connection is fleet-wide: `allTargets`.
    autoApprove: ADMIN.auto.filter(a => a.user === p.handle || teams.some(t => a.user === t + ' (team)'))
      .map(a => ({ connectionId: a.connectionId || null, databaseId: a.databaseId || null,
        allTargets: !a.connectionId, tier: a.tier, expiresAt: a.expiresAt || null,
        via: a.user === p.handle ? null : a.user })),
    admin: sc ? { admin: true, superAdmin: sc.role === 'super', maxTier: ceiling, scopeTargets,
      // "Every target" and "no targets" are two different sentences and used to
      // arrive as the same empty array (CODE brief 2026-09-01 §5). They are read
      // from these flags now, never inferred from a length.
      scopeTargetsAll: (sc.connections || []).indexOf('*') >= 0,
      scopeTeams: sc.teams || [], scopeTeamsAll: !sc.teams,
      canGrant: sc.role === 'super' || ADMIN.roles.some(r => r.subject === p.handle && r.role === 'granter') } : null,
    // Nullable by contract, so exactly one person carries one: a cap lives in
    // user_row_limit_overrides, keyed to a PERSON rather than to a grant.
    rowLimitOverride: p.handle === 'chen.yu' ? { maxRows: 250000 } : null,
  };
}

// MOCK of `connectionState` on GET /saved and /history (CODE brief 2026-08-21
// (b)): ok | no_access | retired | gone | none. The three failure reasons used
// to be indistinguishable in the payload — two produced the same
// confident-looking alias and the third a bare row id — which is why a tab could
// only ever say '—'.
// Two designated targets, because a state no seed row exercises is a state the
// UI is never seen in: one alias the caller holds no grant on, and one whose
// grant has lapsed (that second one answers 403 access_expired on submit).
const MOCK_NO_ACCESS = ['svc-prod-registry'];
const MOCK_EXPIRED_GRANT = { 'svc-prod-scoring': '2026-08-14' };
function connStateFor(connId) {
  if (!connId) return 'none';
  const row = connRegistry().find(c => c.id === connId);
  if (!row) return 'gone';
  if (row.enabled === false) return 'retired';
  if (MOCK_NO_ACCESS.indexOf(connId) >= 0) return 'no_access';
  return 'ok';
}

// What `GET /requestable` answers: the enabled targets the caller CANNOT reach,
// carrying only the databases they lack (CODE brief 2026-08-22 §2). Their own
// `GET /connections` cannot answer this — it holds what they already have, which
// is exactly why the old request form asked them to type the name of a server
// they had never been shown.
// Held scope is read from GRANT rows only, deliberately: an admin's bypass is not
// a grant, and resolving it as one would empty this list for every admin — the
// screen would be unreachable for the people most likely to look at it.
const MOCK_MAINT_DBS = ['postgres', 'rdsadmin', 'master', 'msdb', 'tempdb'];
// MOCK: one designated server with no catalogue snapshot yet, which is the
// `databases: []` case — still requestable, and the one row that exercises the
// typeable database field. Server-side this comes from the snapshot table.
const MOCK_NO_SNAPSHOT = ['svc-prod-inventory'];
function heldScopeFor(handle) {
  const teams = ADMIN.teams.filter(t => (t.members || []).indexOf(handle) >= 0).map(t => t.name);
  const held = {};
  ADMIN.grants
    .filter(g => (g.subjectType === 'user' && g.subject === handle) || (g.subjectType === 'team' && teams.indexOf(g.subject) >= 0))
    .forEach(g => {
      const all = !g.databases || !g.databases.length || g.databases.indexOf('*') >= 0;
      if (all || held[g.connectionId] === true) { held[g.connectionId] = true; return; }
      const set = held[g.connectionId] || new Set();
      // A grant stores either the database id or its name; both have to count as
      // held, or a granted pair comes back as requestable.
      g.databases.forEach(d => set.add(String(d)));
      held[g.connectionId] = set;
    });
  return held;
}

// ---------- Query lifecycle (MOCK: a clock-driven state machine) ----------
const APPROVAL_MS = 4000;   // MOCK: how long a review "takes"
const RUN_MS = 1400;        // MOCK: execution time
const TIER_RANK = { RO: 0, RW: 1, DDL: 2 };
// A stored password is gone for good (CODE 2026-09-23 §5): a role script keeps
// this literal in its place, and sending it back is a 422 rather than a role
// whose password is literally the placeholder.
const QH_MOCK_REDACTED = /'\*\*\*REDACTED\*\*\*'/;
const QH_MOCK_REDACTED_MSG = "This statement's password was hidden when QueryHub stored it. Type the password in again, then submit.";
// MOCK: which DDL the bot may not run itself and hands to a DBA (§5) — a role
// statement needs an attribute the bot's role lacks; an ownership change says
// "must be owner". Anything else runs as before.
const QH_MOCK_HANDOFF = [[/\b(create|alter|drop)\s+(role|user)\b/i, 'permission denied to create role'], [/\bowner\s+to\b/i, 'must be owner of the table']];
function mockHandoffWhy(sql) { const h = QH_MOCK_HANDOFF.find(x => x[0].test(sql || '')); return h ? h[1] : null; }

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
  const auto = isSuper || (cl.tier === 'RO' && autoRO);
  return {
    tier: cl.tier, statements: (cl.statements || []).length || 1,
    blocked: false, blockers: [], warnings: (window.qhRiskHints ? window.qhRiskHints(sql, cl) : []).filter(h => h.level !== 'low').map(h => h.text),
    grantedTier: granted, tierExceedsGrant: exceeds,
    willAutoApprove: auto,
    // The reader of this field is the approver, so the rule is "will a human
    // be asked?" — not "is it dangerous?". An auto-approved request has no
    // approver and is not asked for one (the audit trail still answers why it
    // ran: it records the grant, and the grant carries its own reason).
    // A SCHEDULED request always is: the grant can expire before the run time
    // and the request then falls back to human approval — and so does a BUNDLE,
    // which always goes to one human approval round. That is one question, so
    // since 2026-08-01 it has one name: requiresJustificationWhenReviewed.
    // requiresJustificationWhenScheduled is kept as an alias for one release.
    // Both are decided here, server-side — the client must not re-derive them.
    requiresJustification: cl.tier !== 'RO' && !auto,
    requiresJustificationWhenReviewed: cl.tier !== 'RO',
    requiresJustificationWhenScheduled: cl.tier !== 'RO',
  };
}

// MOCK of the server's destructive-statement gate. The real rules — and the real
// sentences — live server-side: a super-admin is asked, never refused. This
// mirrors them so the prototype exercises the 409 → confirm → re-send path.
// The wording matters more than the detection here: each reason names the
// specific consequence, because it is written to be read at the deciding moment.
function mockDestructiveReasons(sql) {
  const strip = window.qhStripComments || ((s) => s);
  const out = [];
  String(sql || '').split(';').forEach(part => {
    const s = strip(part).replace(/\s+/g, ' ').trim();
    if (!s) return;
    const low = s.toLowerCase();
    const kind = /^update\b/.test(low) ? 'UPDATE' : /^delete\b/.test(low) ? 'DELETE' : null;
    if (/^drop\b/.test(low)) out.push('DROP cannot be undone — the objects and every row in them are gone once this runs.');
    else if (/^truncate\b/.test(low)) out.push('TRUNCATE empties the table completely and does not log the rows it removes, so there is nothing to recover them from.');
    else if (/^alter\s+table\b/.test(low) && /\bdrop\s+(column|constraint)\b/.test(low)) out.push('Dropping a column or a constraint destroys the data in it, and anything that reads it starts failing immediately.');
    else if (kind && !/\bwhere\b/.test(low)) out.push('This ' + kind + ' has no WHERE clause, so it ' + (kind === 'UPDATE' ? 'rewrites' : 'removes') + ' every row in the table.');
    else if (kind && /\bwhere\s+(1\s*=\s*1|true)\b/.test(low)) out.push('The WHERE clause on this ' + kind + ' is always true, so it matches every row in the table.');
  });
  return out.filter((r, i) => out.indexOf(r) === i);
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
    justification: (body.justification && String(body.justification).trim()) || null,
    // Per request, never per session — and it rides back out on the result, so
    // the grid can state which it is instead of the client remembering.
    unmasked: !!body.unmasked,
    t0: Date.now(), scheduledFor: runAt ? new Date(runAt).toISOString() : null,
    // MOCK: the DBA rejects an ad-hoc DROP from a requester who needs approval.
    // A super-admin's DROP is confirmed instead (the 409 in submit) and then
    // runs — destructive SQL is never refused for them, which is the point.
    rejected: !v.willAutoApprove && /\bdrop\s+(table|database|schema)\b/i.test(body.sql),
    handToDba: v.tier === 'DDL' ? mockHandoffWhy(body.sql) : null,
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
  // Handed to a DBA (CODE 2026-09-23 §5): the database refused the bot, so this
  // is `failed` + `awaitingDba` — no spinner and no Stop, because nothing is
  // running and nothing will until a DBA runs it by hand and closes it.
  if (rec.handToDba && el >= approvedAt + 600) {
    push('info', 'Running on ' + rec.conn + '/' + rec.db + '…');
    push('err', 'The database refused the bot: ' + rec.handToDba + '.');
    const closed = ADMIN.manualClosed[rec.id];
    if (!closed) {
      mockHandOff(rec);
      push('info', 'Handed to a DBA — the bot may not run this itself. A DBA runs it by hand, and you get a DM when they have.');
      aud('executor', 'Handed to a DBA to run by hand');
      return { status: 'failed', awaitingDba: true, runMs: null, messages: msg, audit };
    }
    aud(closed.by, closed.completed ? 'Ran it by hand' : 'Marked the hand-run failed');
    push(closed.completed ? 'ok' : 'err', closed.completed ? 'Run by hand by ' + closed.by + '.' : 'A DBA could not run it: ' + closed.reason);
    return { status: closed.completed ? 'done' : 'failed', awaitingDba: false, runMs: null, messages: msg, audit };
  }
  if (el < approvedAt + RUN_MS) {
    push('info', 'Running on ' + rec.conn + '/' + rec.db + '…');
    return { status: 'running', runMs: null, messages: msg, audit };
  }
  const runMs = RUN_MS + (rec.id.length * 7 % 300);
  // CODE 2026-09-23 (e): a read-only query on a primary with a healthy replica
  // in rotation ran there. One info line, no replica name — nobody picks it.
  if (rec.tier === 'RO' && connRegistry().some(c => c.replicaOf === rec.conn && c.enabled !== false)) {
    push('info', 'Ran on a read replica, about 0.4 s behind the primary.');
  }
  push('ok', 'Completed in ' + runMs + ' ms.');
  aud('executor', 'Ran with the ' + rec.tier + ' credential');
  return { status: 'done', runMs, messages: msg, audit };
}
// Registers a hand-off in GET /admin/manual-runs the first time the request is
// seen in that state — the server writes it when the bot gives up.
function mockHandOff(rec) {
  if (ADMIN.manualRuns.some(x => x.id === rec.id)) return;
  const me = mockUser() || {};
  ADMIN.manualRuns = ADMIN.manualRuns.concat([{ id: rec.id, requester: { slackId: me.slackId || null, name: me.name || null },
    connectionId: rec.conn, databaseId: rec.db, tier: rec.tier, sql: rec.sql, reason: rec.justification,
    createdAt: new Date(rec.t0).toISOString(), escalatedAt: isoNow(), bundleId: rec.bundleId, refusal: rec.handToDba }]);
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

// MOCK of multi-statement storage. The server stores ONE TABLE PER STATEMENT and
// `?statement=N` (1-based, default 1) picks one, answering with `statement` +
// `statementCount` (CODE brief 2026-08-20 §12–15). Before that a multi-statement
// run answered 409 and rendered nothing at all.
// The split has to be the real one (`qhSplitStatements`): a semicolon inside a
// string literal is not a statement boundary, and splitting on `;` would hand
// statement 2 a fragment of statement 1.
function mockStatements(sql) {
  const strip = window.qhStripComments || ((s) => s);
  const split = window.qhSplitStatements || ((s) => [s]);
  const parts = split(strip(String(sql || ''))).filter(s => String(s).trim());
  return parts.length ? parts : [String(sql || '')];
}
// MOCK of `statements[]` (CODE brief 2026-08-24): the array rides on EVERY
// result response and describes all N, because the open menu names the ones that
// are not on screen. Captured from the split that RAN, comments stripped and
// string literals KEPT — blanking them is the bug CODE caught on the live check
// (`SELECT 2 AS two, 'x' AS label` had come out as `SELECT 2 AS two, AS label`).
// `n` is authoritative: the client must read the field, not the array position.
// The whitespace collapse MIRRORS the server, which stores the snippet already
// single-line (no newline, tab or double space survives — their test asserts it,
// 2026-08-24 (b)). Do not add a second collapse on the render side.
function stmtLabels(texts) {
  return texts.map((t, i) => {
    const one = String(t).replace(/\s+/g, ' ').trim();
    const kw = (one.match(/^[a-z_]+/i) || [''])[0].toUpperCase();
    return { n: i + 1, kind: kw || 'SQL', snippet: one.length > 80 ? one.slice(0, 80) + '…' : one };
  });
}
function resultFor(rec, n) {
  const texts = mockStatements(rec.sql);
  const count = texts.length;
  const idx = Math.min(Math.max(parseInt(n || 1, 10) || 1, 1), count);
  // One cache entry per statement. A single-statement request keeps exactly the
  // shape it always had (`rec.result`), so nothing that reads it changes.
  if (count === 1) {
    if (!rec.result) {
      const cl = window.qhClassify ? window.qhClassify(rec.sql) : { tier: rec.tier };
      rec.result = window.qhMockResult(rec.sql, cl);
    }
  } else {
    if (!rec.results) rec.results = {};
    if (!rec.results[idx]) {
      const one = texts[idx - 1];
      const cl = window.qhClassify ? window.qhClassify(one) : { tier: rec.tier };
      rec.results[idx] = window.qhMockResult(one, cl);
    }
  }
  const r = count === 1 ? rec.result : rec.results[idx];
  if (r.kind !== 'table') return { kind: 'affected', affected: r.affected || 0, message: r.message || '', statement: idx, statementCount: count, statements: stmtLabels(texts) };
  // MOCK of the server's PII masking. Real API: rows are masked in the executor
  // before they are ever stored, and `unmasked: true` on POST /queries — a
  // super-admin capability — is what skips it. The flag belongs to the REQUEST,
  // so it comes back on the result and the UI states the truth about the rows it
  // is holding rather than the truth about what it asked for.
  const piiCols = (r.cols || []).filter(c => (window.QH_PII_CATALOG || {})[c]);
  const maskRow = (row) => {
    const o = { ...row };
    piiCols.forEach(c => { if (o[c] != null) o[c] = window.qhMaskValue(c, o[c]); });
    return o;
  };
  const page = (offset, count) => {
    const rows = r.slice(offset, count);
    return rec.unmasked ? rows : rows.map(maskRow);
  };
  return {
    kind: 'table', cols: r.cols || [], piiCols: r.piiCols || piiCols,
    // Null once there is more than one statement, as the server sends it — the
    // driver reports types for the statement it is executing, not for a set.
    // Column tooltips fall back to the schema cache, which is per database.
    colTypes: count > 1 ? null : (r.colTypes || mockColTypes(r.cols || [])),
    // This statement's OWN row count. The request's row_count is the sum across
    // statements and would page the grid past the end of this table.
    total: r.total || 0, truncated: !!r.truncated, unmasked: !!rec.unmasked,
    statement: idx, statementCount: count,
    statements: stmtLabels(texts),
    slice: page,
    // Real API pulls windows beyond the first page from /queries/:id/rows,
    // carrying the same ?statement=N.
    fetchPage: (offset, cnt) => mockDelay(() => page(offset, cnt), 220),
  };
}

// MOCK: CSV/XLSX download. The real endpoints stream a server-built file; here
// we hand the browser a data: URL built from the mock rows (both as CSV).
function resultDataUrl(id, statement) {
  const rec = MOCK.requests[id];
  if (!rec) return 'data:text/csv,';
  const r = resultFor(rec, statement);
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

// The approver has to see WHERE the statement will run, not only against which
// alias — so the queue payload carries the target's registry tags, resolved
// server-side (a client cannot be trusted to join them, and a stale join here
// would put the wrong cloud next to a DROP).
// Registry-tag rules, mirrored from the server: lower-case keys matching
// ^[a-z][a-z0-9_-]{0,31}$ (they become search tokens, so a key with a space
// could not be typed back), at most 24 keys, 120 characters per value, and an
// empty value drops its key rather than storing a blank.
function mockNormTags(bag) {
  const out = {};
  const keys = Object.keys(bag || {});
  if (keys.length > 24) return { error: 'At most 24 tags per connection.' };
  for (const raw of keys) {
    const k = String(raw).toLowerCase();
    const v = bag[raw] == null ? '' : String(bag[raw]).trim();
    if (!v) continue;
    if (!/^[a-z][a-z0-9_-]{0,31}$/.test(k)) return { error: 'Invalid tag key “' + raw + '” — letters, digits, - and _ only, starting with a letter.' };
    if (v.length > 120) return { error: 'Tag “' + k + '” is longer than 120 characters.' };
    out[k] = v;
  }
  return { tags: out };
}

function connTagsOf(id) {
  const c = connRegistry().find(x => x.id === id);
  return c ? { ...(c.tags || {}) } : {};
}

// ---------- The mock client ----------
const qhApi = {
  // ---- auth / identity ----
  me: () => mockDelay(null, 90).then(() => {
    const u = mockUser();
    if (!u) return mockFail('Not signed in.', 401, 'unauthenticated');
    return { user: u, displayTz: 'Europe/Istanbul',
      // Whether this deployment has Slack at all — the same value the backend
      // gates every Slack path on. The default install profile has none, and
      // three strings in the UI used to claim approvals run there regardless.
      // Flip it (or Tweaks → Access → "No Slack") to see the other copy.
      slackEnabled: true,
      admin: { canApprove: u.role !== 'developer', role: u.role, connections: ['*'] } };
  }),
  providers: () => mockDelay({
    // Three kinds, on purpose: Slack, a company IdP (`kind:'oauth'`, its own
    // button and its own /start since 2026-08-14) and local accounts. A mock with
    // Slack alone cannot show the defect that round fixed — every oauth provider
    // rendering as one Slack button.
    providers: [
      { id: 'slack', kind: 'oauth', label: 'Sign in with Slack' },
      { id: 'entra', kind: 'oauth', label: 'Sign in with company SSO' },
      { id: 'local', kind: 'password', label: 'Local account' },
    ],
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
  // WHO asks decides what ships (CODE brief 2026-08-21 §3): the route branches
  // `list_all()` for an admin and `list_enabled()` for everyone else, so a
  // developer never receives a disabled target at all — the `.qh-conn-off`
  // marker in the tree is a thing only an admin can ever see. An admin gets the
  // retired aliases, flagged and last, because saved queries and history still
  // reference them. `host` is display-only — the endpoint the sidebar shows on
  // hover when it lists databases instead of servers.
  connections: () => mockDelay(() => {
    // `host` / `port` ship to EVERY caller (CODE_TO_DESIGN_BRIEF 2026-08-15 (b)):
    // the loop that builds this payload is grant-filtered, so it can only carry
    // the address of a machine the caller already reads and writes through us,
    // and the credentials are per-target and held by the bot. The account-class
    // tags stay admin-only — an account id is not an address anyone pastes into a
    // ticket or a psql line, so that argument does not carry them.
    const adminView = !!(mockUser() && mockUser().role !== 'developer');
    const narrowTags = (t) => {
      const o = {};
      if (t.provider) o.provider = t.provider;
      if (t.service) o.service = t.service;
      return o;
    };
    // A replica is never a target anyone picks: every list shows the primary.
    return { connections: connRegistry().filter(c => !c.replicaOf && (adminView || c.enabled !== false)).map(c => ({
    id: c.id, name: c.name, engine: c.engine, env: c.env, autoApproveRO: c.autoApproveRO,
    disabled: c.enabled === false,
    host: c.host, port: c.port,
    // Registry tags, read-only: which cloud runs this machine, which service of
    // theirs, and — for an admin — the account it is billed to. Display-only, and
    // it ships on this payload because the sidebar and the results header both
    // have to say where a query ran without a second round trip.
    tags: adminView ? { ...(c.tags || {}) } : narrowTags(c.tags || {}),
    // The tree, the autocomplete and drag-insert all read the table list off
    // THIS payload (`tableRefs`, falling back to bare `tables`) — the schema
    // endpoint only fills in columns/indexes when a node is opened. The real
    // GET /connections must carry it too, or the tree renders empty databases.
    databases: c.databases.map(d => ({ ...d, tables: ((targetOf(c.id, d.id).db || {}).tables) || [],
      autoApproveRO: d.tier === 'RO' && c.env !== 'production' ? true : undefined })),
  })) };
  }),
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
  saved: () => mockDelay(() => ({ saved: MOCK.savedSrv.map(s => ({ ...s, connectionState: connStateFor(s.connectionId) })) })),
  history: () => mockDelay(() => {
    const rows = (window.QH_HISTORY || []).map((h, i) => ({
      id: h.id, sql: h.sql, connectionId: h.conn, databaseId: h.db, tier: h.tier, status: h.status,
      rowCount: h.rows, approver: h.approver, createdAt: isoAgo(1000 * 60 * (2 + i * 37)),
      connectionState: connStateFor(h.conn), awaitingDba: false,
    }));
    // MOCK (CODE 2026-09-23 §5): a hand-off stays `failed` + `awaitingDba`
    // until a DBA closes it — and its stored text has the password hidden.
    const mr = MOCK_HIST_HANDOFF, closed = ADMIN.manualClosed[mr.id];
    rows.splice(1, 0, { ...mr, status: closed ? (closed.completed ? 'done' : 'failed') : 'failed', awaitingDba: !closed,
      connectionState: connStateFor(mr.connectionId) });
    return { history: rows };
  }),
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
    if (QH_MOCK_REDACTED.test(body.sql || '')) return mockFail(QH_MOCK_REDACTED_MSG, 422, 'validation');
    // A lapsed grant is its OWN code with the date as a field — not the same 403
    // as "you were never allowed here" (CODE brief 2026-08-21 (b)). The prose
    // stays in `message` for anywhere that only prints it.
    const exp = MOCK_EXPIRED_GRANT[body.connectionId];
    if (exp) {
      const nice = new Date(exp + 'T00:00:00').toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
      return mockFail('Your access to ' + body.connectionId + ' expired on ' + nice + '. Ask an admin to extend it.',
        403, 'access_expired', { expiredOn: exp });
    }
    // Unmasked results are a super-admin capability and THIS is the gate; the UI
    // showing the switch to no one else is a courtesy, not the protection.
    if (body.unmasked && !(mockUser() && mockUser().role === 'super')) {
      return mockFail('Unmasked results are available to super-admins only.', 403, 'forbidden');
    }
    // Destructive SQL is not refused — it is asked about once, and the identical
    // request with confirmed:true runs. Since 2026-08-15 the server sends the
    // distinct code AND the reasons as an array (`message` keeps them
    // newline-joined for an older client), so this sends all three — otherwise the
    // prototype exercises the no-code fallback the real API no longer uses.
    const confirmReasons = mockDestructiveReasons(body.sql);
    if (confirmReasons.length && !body.confirmed) {
      const err = new Error(confirmReasons.join('\n'));
      err.status = 409; err.code = 'confirmation_required'; err.reasons = confirmReasons;
      return Promise.reject(err);
    }
    // The OTHER 409, and the reason qhConfirmReasons needs a ladder at all: an
    // identical request already in flight. Wording copied from the live backend
    // (CODE_TO_DESIGN_BRIEF 2026-08-15 §2) — it matches none of QH_DUP_409's
    // alternations on purpose, so sending it proves the CODE decides, not the text.
    const dup = Object.keys(MOCK.requests).map(k => MOCK.requests[k]).find(r => (
      r.sql === body.sql && r.conn === body.connectionId && r.db === body.databaseId
      && ['pending', 'approved', 'running'].indexOf(statusOf(r).status) >= 0
    ));
    if (dup) {
      const err = new Error('You already have an active request (#' + dup.id + ', status=' + statusOf(dup).status + ') with the same query on this target+database.');
      err.status = 409; err.code = 'conflict';
      return Promise.reject(err);
    }
    // The server is the one that enforces this; the client asks /classify for
    // the same two flags so the field appears before Run, not after a 400.
    const v = verdictFor(body.sql, body.connectionId, body.databaseId);
    if ((body.schedule ? v.requiresJustificationWhenReviewed : v.requiresJustification)
        && !String(body.justification || '').trim()) {
      return mockFail('Justification is required for ' + v.tier + ' queries.', 400, 'justification_required');
    }
    const rec = newRequest(body);
    if (rec.scheduledFor) {
      MOCK.scheduled = [{ id: rec.id, name: body.name || 'Scheduled query', sql: body.sql, conn: rec.conn, db: rec.db,
        tier: rec.tier, when: rec.scheduledFor, status: 'scheduled' }, ...MOCK.scheduled];
    }
    return { id: rec.id, status: statusOf(rec).status };
  }, 300),
  submitBatch: (body) => mockDelay(() => {
    // A bundle answers the confirmation question the same way, with `Item N:` in
    // front of each reason and ONE confirmed flag for the whole round.
    const bItems = body.items || [];
    if (bItems.some(it => QH_MOCK_REDACTED.test(it.sql || ''))) return mockFail(QH_MOCK_REDACTED_MSG, 422, 'validation');
    const bReasons = [];
    bItems.forEach((it, i) => mockDestructiveReasons(it.sql).forEach(r => bReasons.push('Item ' + (i + 1) + ': ' + r)));
    if (bReasons.length && !body.confirmed) {
      const err = new Error(bReasons.join('\n'));
      err.status = 409; err.code = 'confirmation_required'; err.reasons = bReasons;
      return Promise.reject(err);
    }
    // One justification for the whole bundle, matching the API and Slack: a
    // bundle always meets a human approver, so the rule is the same one that
    // makes a scheduled request need a reason.
    const needs = (body.items || []).some(it => verdictFor(it.sql, it.connectionId, it.databaseId).requiresJustificationWhenReviewed);
    if (needs && !String(body.justification || '').trim()) {
      return mockFail('Justification is required for this bundle.', 400, 'justification_required');
    }
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
  result: (id, statement) => mockDelay(() => {
    const rec = MOCK.requests[id];
    if (!rec) return mockFail('Unknown request.', 404, 'not_found');
    return resultFor(rec, statement);
  }, 180),
  rows: (id, o, l, statement) => mockDelay(() => {
    const rec = MOCK.requests[id];
    const r = rec ? resultFor(rec, statement) : null;
    return { rows: r && r.kind === 'table' ? r.slice(o, l) : [] };
  }, 200),
  // Export follows the switcher: `?statement=N` gives THAT table. Omitted, it is
  // the whole artefact — which is what Export on Result 2 used to send, under the
  // XLSX media type (CODE brief 2026-08-21 (d)).
  resultCsvUrl: (id, statement) => resultDataUrl(id, statement),
  resultXlsxUrl: (id, statement) => resultDataUrl(id, statement),
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
  // GET /requestable — what is MISSING. Excluded server-side: the control plane,
  // the maintenance databases, disabled targets and any pair already granted.
  requestable: () => mockDelay(() => {
    const me = mockUser();
    if (!me) return mockFail('Not signed in.', 401, 'unauthenticated');
    const held = heldScopeFor(me.handle);
    const out = [];
    connRegistry().forEach(c => {
      if (c.enabled === false || c.replicaOf) return;  // a retired target, or a replica, is not a thing to ask for
      if (held[c.id] === true) return;                 // whole server already held
      const mine = held[c.id] || null;
      const snap = MOCK_NO_SNAPSHOT.indexOf(c.id) >= 0 ? [] : (c.databases || []);
      const dbs = snap
        .filter(d => MOCK_MAINT_DBS.indexOf(String(d.name).toLowerCase()) < 0)
        .filter(d => !(mine && (mine.has(d.name) || mine.has(d.id))))
        .map(d => d.name);
      // Nothing left to ask for on a catalogued server means the row itself has
      // no reason to be in the list. A server with NO snapshot keeps its row.
      if (snap.length && !dbs.length) return;
      out.push({ connectionId: c.id, name: c.name, engine: c.engine, env: c.env, databases: dbs, partial: !!mine });
    });
    return { connections: out };
  }, 320),
  // POST /auto-approve-requests (design 2026-09-22 §3, NEW — CODE builds it).
  // A developer ASKS; granting stays an admin action. Refusals mirror what the
  // grant path would refuse, so a request that can never be granted is stopped
  // here rather than landing in a DBA's list: DDL is never auto-approvable, the
  // window is bounded, and the reason is required.
  requestAutoApprove: (b) => mockDelay(() => {
    const me = mockUser();
    if (!me) return mockFail('Not signed in.', 401, 'unauthenticated');
    if (String(b.tier).toUpperCase() === 'DDL') return mockFail('Schema changes are always reviewed — DDL cannot be auto-approved.', 400, 'ddl_never_auto');
    // `days` (1, 7, 14, 30) OR `windowMinutes` (60, 180, 480) — one of the
    // offered windows, never a free number (CODE 2026-09-23 §4).
    const days = b.days != null ? parseInt(b.days, 10) : null;
    const mins = b.windowMinutes != null ? parseInt(b.windowMinutes, 10) : null;
    const opt = MOCK_AUTO_WINDOWS.find(o => days != null ? o.days === days : (mins != null && !o.days && o.minutes === mins));
    if (!opt) return mockFail('Pick one of the offered windows.', 400, 'bad_window');
    const cn = connRegistry().find(c => c.id === b.connectionId);
    const held = cn ? (cn.databases || []).find(d => d.id === b.databaseId || d.name === b.databaseId) : null;
    if (String(b.tier).toUpperCase() === 'RW' && held && String(held.tier).toUpperCase() === 'RO') return mockFail('You hold read-only access there, so only reads can skip review.', 403, 'tier_exceeds_grant');
    if (!String(b.reason || '').trim()) return mockFail('Say why — it is what the DBA decides on.', 400, 'reason_required');
    const dupe = ADMIN.autoReqs.find(r => r.status === 'submitted' && r.requester === me.handle && r.connectionId === b.connectionId && (r.databaseId || null) === (b.databaseId || null));
    if (dupe) return mockFail('You already have a request waiting for this database.', 409, 'duplicate');
    const row = { id: mockId('ar'), requester: me.handle, requesterName: me.name, connectionId: b.connectionId, databaseId: b.databaseId || null,
      tier: String(b.tier).toUpperCase(), days: opt.days || null, windowMinutes: opt.days ? null : opt.minutes, windowLabel: opt.label,
      reason: String(b.reason).trim(), requestedAt: new Date().toISOString(), status: 'submitted' };
    ADMIN.autoReqs = [row, ...ADMIN.autoReqs];
    mockAudit('Requested auto-approve', me.handle + ' → ' + b.connectionId + '/' + (b.databaseId || 'all databases') + ' · ' + row.tier + ' · ' + opt.label, 'auto');
    return row;
  }, 260),
  // GET /auto-approve-requests — the offered windows, and the caller's OWN asks,
  // so the modal can say "you asked, waiting" instead of meeting a 409.
  autoApproveRequests: () => mockDelay(() => {
    const me = mockUser();
    if (!me) return mockFail('Not signed in.', 401, 'unauthenticated');
    return { windowOptions: MOCK_AUTO_WINDOWS.map(o => ({ ...o })), requests: ADMIN.autoReqs.filter(r => r.requester === me.handle) };
  }, 160),
  requestEndpoint: (b) => mockDelay(() => {
    // A picked row is AUTHORITATIVE and there is NO fallback to the free text
    // (§2): falling back is what produced requests nobody could resolve. Unknown
    // or disabled → 404, a database not on that connection → 400.
    if (b.connectionId) {
      const c = connRegistry().find(x => x.id === b.connectionId);
      if (!c || c.enabled === false) return mockFail('That target is not available to request.', 404, 'not_found');
      const dbs = c.databases || [];
      // Skipped for a server with no catalogued databases, same as the
      // auto-approve check: refusing every name on a freshly onboarded server
      // would block onboarding in order to catch a typo.
      if (b.database && dbs.length && !dbs.some(d => d.id === b.database || d.name === b.database)) {
        return mockFail('There is no database “' + b.database + '” on ' + c.name + '.', 400, 'unknown_database');
      }
    }
    ADMIN.endpointReqs = [{ id: mockId('er'), connectionId: b.connectionId || null, server: b.server, database: b.database, tier: b.tier,
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
  // The queue carries `justification` as canonical (the real backend has always
  // had it on the requests row) with `reason` kept as the legacy alias for one
  // release — same as GET /admin/queue since 2026-07-31.
  // `bundlePosition` / `bundleSize` ride along beside `bundleId` (2026-08-20),
  // null on a standalone request — enough for the queue to collapse a batch into
  // one group and order it as written. Computed here from the rows that share a
  // bundle, which is what the server does with a window function.
  adminQueue: () => mockDelay(() => {
    const size = {}, seen = {};
    ADMIN.queue.forEach(it => { if (it.bundleId) size[it.bundleId] = (size[it.bundleId] || 0) + 1; });
    return { queue: ADMIN.queue.map(it => {
      let position = null;
      if (it.bundleId) { seen[it.bundleId] = (seen[it.bundleId] || 0) + 1; position = seen[it.bundleId]; }
      return { ...it, justification: it.justification || it.reason || null, tags: connTagsOf(it.connectionId),
        bundlePosition: position, bundleSize: it.bundleId ? size[it.bundleId] : null };
    }) };
  }, 200),
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
    // `subjects: []` beside `subject` — exactly one is used, `subjects` when it is
    // non-empty (CODE brief 2026-09-01 §1). All-or-nothing by construction: every
    // id is checked BEFORE a row is written, and a bad one refuses the whole call
    // naming which. A team subject refuses a list — a team is already a set of
    // people, and a list there would silently grant to the first one.
    const many = (b.subjects || []).filter(Boolean);
    if (many.length) {
      if (b.subjectType === 'team') return mockFail('A team grant takes one subject — a team is already a set of people. Nothing was written.', 400, 'bad_request');
      const bad = many.find(s => !/^[A-Za-z0-9._@-]{3,}$/.test(String(s)));
      if (bad) return mockFail(bad + ' is not a principal id (expected a Slack user id or local:<username>). Nothing was written.', 400, 'bad_request');
    }
    const dbs = (b.databases && b.databases.length) ? b.databases : (b.databaseId ? [b.databaseId] : ['*']);
    // A date already past is refused rather than stored inert: the row would
    // read to the admin as access given and grant nothing (server: 400).
    if (b.expiresAt && new Date(b.expiresAt) <= new Date()) return mockFail('That expiry is already in the past — the grant would be dead on arrival.', 400, 'invalid_expiry');
    const exp = b.expiresAt || null;
    const expNote = exp ? ' · expires ' + exp.slice(0, 10) : '';
    // The three statements a grant is made of, split into one helper both entry
    // points call (the server did the same to `grants.grant`): whitelist the
    // principal if it is new, replace an existing row for the pair, or insert.
    const writeOne = (subject) => {
      // `grants.grant` whitelists an unknown principal in the SAME transaction —
      // name / email / tz from Slack — so granting IS the create and there is no
      // POST /admin/people (CODE brief 2026-08-22 §3). Mirrored here, with one
      // deliberate exception: an ADMIN-ONLY principal (a scopes row and no
      // requesters row) is left alone, or editing g_8 in a demo would quietly
      // erase the case that `subjectName` exists for.
      if (b.subjectType === 'user' && subject && !ADMIN.people.some(p => p.handle === subject || p.id === subject)
          && !ADMIN.scopes.some(s => s.admin === subject)) {
        const dir = mockDirectory(subject);
        const name = (dir && dir.name) || subject;
        ADMIN.people = ADMIN.people.concat([{ id: 'u_' + String(subject).replace(/[^A-Za-z0-9]/g, '').toLowerCase(),
          handle: subject, name, initials: mockInitials(name), email: mockEmail(subject), enabled: true }]);
        mockAudit('Whitelisted person', subject + (dir && dir.name ? ' · ' + dir.name : ''), 'grant');
      }
      const ex = ADMIN.grants.find(g => g.subjectType === b.subjectType && g.subject === subject && g.connectionId === b.connectionId);
      // Re-granting REPLACES the expiry, including clearing it: an admin
      // re-issuing a grant with no date means no date.
      if (ex) { ex.databases = dbs; ex.tier = b.tier; ex.expiresAt = exp; return { id: ex.id, updated: true }; }
      const row = { id: mockId('g'), subjectType: b.subjectType, subject,
        // The server resolves the display name against both people tables; for a
        // team it IS the team name (CODE brief 2026-08-21 §1).
        subjectName: b.subjectType === 'team' ? subject : ((ADMIN.people.find(p => p.handle === subject) || {}).name || null),
        connectionId: b.connectionId, databases: dbs, tier: b.tier, expiresAt: exp, grantedBy: 'dba.amara', grantedAt: isoNow() };
      ADMIN.grants = [row, ...ADMIN.grants];
      return { id: row.id, updated: false };
    };
    if (many.length) {
      // Duplicates are collapsed in the order they were sent.
      const subs = many.filter((s, i) => many.indexOf(s) === i);
      const res = subs.map(writeOne);
      mockAudit('Granted ' + b.tier + expNote + ' · ' + subs.length + ' people', subs.join(', ') + ' → ' + b.connectionId, 'grant');
      return { id: res[0].id, subjects: subs };
    }
    const one = writeOne(b.subject);
    mockAudit((one.updated ? 'Updated grant · ' : 'Granted ') + b.tier + expNote, b.subject + ' → ' + b.connectionId, 'grant');
    return { id: one.id };
  }, 240),
  adminDelGrant: (id) => mockDelay(() => {
    const g = ADMIN.grants.find(x => x.id === id);
    ADMIN.grants = ADMIN.grants.filter(x => x.id !== id);
    if (g) mockAudit('Revoked grant', g.subject + ' → ' + g.connectionId, 'reject');
    return null;
  }, 200),
  adminAutoGrants: () => mockDelay(() => ({ autoGrants: ADMIN.auto.slice() }), 180),
  adminAddAutoGrant: (b) => mockDelay(() => {
    // What the server does with the scope, mirrored so the form cannot drift
    // back to sending a magic string: '*' / '' / all / any mean every database
    // and normalise to NULL, and a database that is not on the connection is a
    // 400 rather than a grant that quietly matches nothing (§3).
    const raw = b.databaseId == null ? '' : String(b.databaseId);
    const dbId = ['', '*', 'all', 'any'].indexOf(raw.toLowerCase()) >= 0 ? null : raw;
    const conn = connRegistry().find(c => c.id === b.connectionId);
    // The check is SKIPPED for a connection with no catalogued databases: a
    // freshly onboarded server has no schema snapshot yet, and refusing every
    // name on it would block onboarding in order to catch a typo (CODE 2026-08-21).
    if (dbId && conn && (conn.databases || []).length && !(conn.databases || []).some(d => d.id === dbId || d.name === dbId)) {
      return mockFail('There is no database "' + dbId + '" on ' + b.connectionId + '.', 400, 'unknown_database');
    }
    const person = ADMIN.people.find(p => p.handle === b.user);
    const row = { ...b, databaseId: dbId, id: mockId('a'), userName: person ? person.name : null, createdBy: 'dba.amara', createdByName: 'Amara Osei' };
    ADMIN.auto = [row, ...ADMIN.auto];
    mockAudit('Created auto-approve grant', b.user + ' → ' + b.connectionId + '/' + (dbId || 'all databases') + ' · ' + b.tier, 'auto');
    return row;
  }, 220),
  adminDelAutoGrant: (id) => mockDelay(() => { ADMIN.auto = ADMIN.auto.filter(x => x.id !== id); mockAudit('Revoked auto-approve grant', '', 'reject'); return null; }, 180),
  // POST /admin/auto-grants/bulk (CODE 2026-09-23 §4): one subject — a person OR
  // a team — many targets, ONE tier, ONE window, ALL OR NOTHING. Any refusal is
  // a 409 listing every refused target, and nothing is written.
  adminAddAutoGrantsBulk: (b) => mockDelay(() => {
    const type = b.subjectType === 'team' ? 'team' : 'user';
    const tier = String(b.tier || '').toUpperCase();
    if (tier === 'DDL') return mockFail('Schema changes are always reviewed — DDL cannot be auto-approved.', 400, 'ddl_never_auto');
    if (['RO', 'RW'].indexOf(tier) < 0) return mockFail('tier must be RO or RW.', 400, 'bad_tier');
    const team = type === 'team' ? ADMIN.teams.find(t => t.name === b.subject || t.id === b.subject) : null;
    if (type === 'team' && !team) return mockFail('No such team.', 404, 'no_team');
    const user = type === 'team' ? team.name + ' (team)' : String(b.subject || '').trim();
    if (!user) return mockFail('Pick who this is for.', 400, 'bad_subject');
    const targets = (b.targets || []).map(t => ({ connectionId: t.connectionId,
      databaseId: t.databaseId == null || ['', '*', 'all', 'any'].indexOf(String(t.databaseId).toLowerCase()) >= 0 ? null : String(t.databaseId) }));
    if (!targets.length) return mockFail('Add at least one target.', 400, 'no_targets');
    const refused = [], seen = {};
    targets.forEach(t => {
      const k = t.connectionId + '/' + (t.databaseId || '*');
      const c = connRegistry().find(x => x.id === t.connectionId);
      if (seen[k]) refused.push({ target: t, reason: 'Listed twice.' });
      else if (!c) refused.push({ target: t, reason: 'No such connection.' });
      else if (c.enabled === false) refused.push({ target: t, reason: c.name + ' is disabled.' });
      else if (t.databaseId && (c.databases || []).length && !c.databases.some(d => d.id === t.databaseId || d.name === t.databaseId)) refused.push({ target: t, reason: 'There is no database “' + t.databaseId + '” on ' + c.name + '.' });
      else if (ADMIN.auto.some(a => a.user === user && a.connectionId === t.connectionId && (a.databaseId || null) === t.databaseId)) refused.push({ target: t, reason: 'Already exempt here.' });
      seen[k] = 1;
    });
    if (refused.length) return mockFail(refused.length + ' of ' + targets.length + ' refused — nothing was written.', 409, 'refused', { refused });
    if (b.dryRun) return { dryRun: true, applied: 0, ids: [], targets };
    const exp = b.expiresAt || (b.expiresInMinutes ? isoIn(60000 * b.expiresInMinutes) : null);
    const person = type === 'user' ? ADMIN.people.find(p => p.handle === user || p.id === user) : null;
    const rows = targets.map(t => ({ id: mockId('a'), user, userName: person ? person.name : null, tier, connectionId: t.connectionId,
      databaseId: t.databaseId, expiresAt: exp, reason: b.reason || null, createdBy: 'dba.amara', createdByName: 'Amara Osei' }));
    ADMIN.auto = rows.concat(ADMIN.auto);
    mockAudit('Created auto-approve grants', user + ' → ' + targets.length + ' target' + (targets.length === 1 ? '' : 's') + ' · ' + tier, 'auto');
    return { applied: rows.length, ids: rows.map(r => r.id), targets };
  }, 260),
  // GET /admin/auto-approve-requests + POST /admin/auto-approve-requests/{id}/decision
  // (design 2026-09-22 §3, NEW). Approve WRITES the auto-approve row — the
  // window starts at the decision, not at the ask, so a request that sat for two
  // days still gets the length that was asked for.
  adminAutoRequests: () => mockDelay(() => ({ requests: ADMIN.autoReqs.filter(r => r.status === 'submitted') }), 160),
  adminDecideAutoRequest: (id, b) => mockDelay(() => {
    const r = ADMIN.autoReqs.find(x => x.id === id);
    if (!r || r.status !== 'submitted') return mockFail('That request was already decided.', 409, 'decided');
    r.status = b && b.approve ? 'approved' : 'rejected';
    let grantId = null;
    if (b && b.approve) {
      grantId = mockId('a');
      const ms = r.days ? 86400000 * r.days : 60000 * (r.windowMinutes || 60);
      ADMIN.auto = [{ id: grantId, user: r.requester, userName: r.requesterName, tier: r.tier, connectionId: r.connectionId, databaseId: r.databaseId,
        expiresAt: isoIn(ms), createdBy: 'dba.amara', createdByName: 'Amara Osei', reason: r.reason }, ...ADMIN.auto];
    }
    mockAudit(b && b.approve ? 'Granted requested auto-approve' : 'Declined auto-approve request', r.requester + ' → ' + r.connectionId + ' · ' + (r.windowLabel || ''), b && b.approve ? 'auto' : 'reject');
    return { id, status: r.status, grantId };
  }, 220),
  adminScopes: () => mockDelay(() => ({ scopes: ADMIN.scopes.slice() }), 180),
  adminSaveScope: (b) => mockDelay(() => {
    const ex = ADMIN.scopes.find(s => s.id === b.admin || s.admin === b.admin);
    if (ex) { Object.assign(ex, { role: b.role, canApprove: b.canApprove, connections: b.connections }); mockAudit('Updated admin scope', ex.admin, 'scope'); return ex; }
    const row = { id: mockId('s'), admin: b.admin, role: b.role, canApprove: b.canApprove, connections: b.connections };
    ADMIN.scopes = [row, ...ADMIN.scopes];
    mockAudit('Added admin', b.admin + ' · ' + b.role, 'scope');
    return row;
  }, 240),
  // ---- admin: roles (scoped approver / granter / importer / admin) ----
  // The new authorization model can express a SCOPED approver — "approves this
  // team's requests, up to RW, on this server" — which the admins table never
  // could. `GET` returns direct rows (created here, editable) followed by rows
  // MIRRORED from the admins table: read-only, and shown rather than filtered
  // out because they are what the system actually enforces. A list that omitted
  // them would disagree with the answers people get.
  adminRoles: () => mockDelay(() => ({ roles: roleRegistry(), enforced: MOCK_ROLES_ENFORCED }), 200),
  // PATCH /admin/roles/{id} (design 2026-09-22 §6, NEW). The subject is the one
  // field that cannot change — a role moved to somebody else is a revoke and a
  // grant, and has to read that way in the audit log. Everything else runs the
  // SAME checks as create, and the reply carries `before` so the audit row (and
  // the toast) can say what the row was.
  adminUpdateRole: (id, b) => mockDelay(() => {
    if (String(id).indexOf('mirror:') === 0) return mockFail('This role mirrors the admins table — change it in Admin scopes.', 409, 'mirrored');
    const r = ADMIN.roles.find(x => x.id === id);
    if (!r) return mockFail('No such active role.', 404, 'not_found');
    if (r.source === 'synced') return mockFail('This role is kept by a sync — change it at its source.', 409, 'synced');
    if (b.subject && b.subject !== r.subject) return mockFail('A role cannot move to another person — revoke it and create one for them.', 400, 'subject_immutable');
    // A field that is SENT is set; a field left out is kept (CODE 2026-09-23 §4).
    // A bare null cannot say both "not editing" and "widen", so widening and
    // removing are flags of their own: scopeTeamAll / scopeTargetAll widen a
    // scope to everything, clearMaxTier / clearValidUntil drop the ceiling / end.
    const has = (k) => Object.prototype.hasOwnProperty.call(b, k) && b[k] != null;
    const role = has('role') ? b.role : r.role;
    if (QH_ROLE_KEYS.indexOf(role) < 0) return mockFail('Unknown role.', 400, 'bad_role');
    const teamId = b.scopeTeamAll ? null : has('scopeTeamId') ? b.scopeTeamId : r.scopeTeamId;
    const targetId = b.scopeTargetAll ? null : has('scopeTargetId') ? b.scopeTargetId : r.scopeTargetId;
    const tier = b.clearMaxTier ? null : has('maxTier') ? String(b.maxTier).toUpperCase() : r.maxTier;
    const until = b.clearValidUntil ? null : has('validUntil') ? b.validUntil : r.validUntil;
    if (role === 'admin' && (teamId || targetId || tier)) return mockFail('An admin is fleet-wide. Use approver for a scoped role.', 400, 'admin_scope');
    if (tier && role !== 'admin' && role !== 'approver') return mockFail('A tier ceiling only applies to admin and approver.', 400, 'tier_scope');
    const team = teamId ? (ADMIN.teams || []).find(t => t.id === teamId) : null;
    if (teamId && !team) return mockFail('No such team.', 404, 'no_team');
    // The alias — what the form holds — is accepted as well as the id.
    const conn = targetId ? connRegistry().find(c => c.id === targetId || c.name === targetId) : null;
    if (targetId && !conn) return mockFail('No such connection.', 404, 'no_target');
    const before = { ...r };
    const next = { role, scopeTeamId: team ? team.id : null, scopeTeamName: team ? team.name : null,
      scopeTargetId: conn ? conn.id : null, scopeTargetName: conn ? conn.name : null,
      maxTier: tier || null, validUntil: until || null, reason: has('reason') ? (b.reason || null) : r.reason };
    const same = Object.keys(next).every(k => (next[k] || null) === (r[k] || null));
    if (same) return { id: r.id, replaced: false, before, changed: false, name: r.name };
    Object.assign(r, next);
    mockAudit('Changed role', r.name + ' · ' + before.role + (before.scopeTeamName ? ' · ' + before.scopeTeamName : '') + ' → ' + r.role + (r.scopeTeamName ? ' · ' + r.scopeTeamName : ''), 'scope');
    return { id: r.id, replaced: false, before, changed: true, name: r.name };
  }, 260),
  adminAddRole: (b) => mockDelay(() => {
    if (QH_ROLE_KEYS.indexOf(b.role) < 0) return mockFail('Unknown role. One of: ' + QH_ROLE_KEYS.join(', ') + '.', 400, 'bad_role');
    // Admin is fleet-wide by definition, so a scope on it is a 400 and not a
    // silently-dropped field. The form retires the scope controls when admin is
    // picked; this is the backstop, mirroring the server.
    if (b.role === 'admin' && (b.scopeTeamId || b.scopeTargetId || b.maxTier)) {
      return mockFail('An admin is fleet-wide. Use approver for a scoped role.', 400, 'admin_scope');
    }
    const tier = b.maxTier ? String(b.maxTier).toUpperCase() : null;
    if (tier && ['RO', 'RW', 'DDL'].indexOf(tier) < 0) return mockFail('maxTier must be RO, RW or DDL.', 400, 'bad_tier');
    // Nothing reads a ceiling on a granter or an importer, so it is refused
    // rather than stored (CODE brief 2026-09-07 §1) — a stored value nothing
    // reads is the `databaseId: '*'` bug in another field.
    if (tier && b.role !== 'admin' && b.role !== 'approver') {
      return mockFail('A tier ceiling only applies to admin and approver — nothing reads it on a ' + b.role + '. Leave it empty.', 400, 'tier_scope');
    }
    const known = ADMIN.people.find(p => p.handle === b.subject || p.id === b.subject)
      || ADMIN.scopes.find(s => s.admin === b.subject || s.id === b.subject);
    if (!known) {
      // A directory hit with no QueryHub account is still a 404: the role is
      // keyed to a principal the app has never seen sign in. The message names
      // the person when the directory knows them, because "unknown id" for
      // somebody who obviously exists reads as a bug in the picker.
      const dir = mockDirectory(b.subject);
      return mockFail((dir ? dir.name : b.subject) + ' has no QueryHub account yet — they need to sign in once before a role can be given.', 404, 'no_account');
    }
    const team = (ADMIN.teams || []).find(t => t.id === b.scopeTeamId || t.name === b.scopeTeamId);
    if (b.scopeTeamId && !team) return mockFail('No such team.', 404, 'no_team');
    const conn = connRegistry().find(c => c.id === b.scopeTargetId || c.name === b.scopeTargetId);
    // Migration 110's partial unique index: a role is immutable, so a second post
    // over the same scope is a 409 naming the row rather than a second live row
    // that resolves to the wider of the two while the narrower keeps rendering
    // like a restriction in force (CODE brief 2026-09-07 §6).
    const dupe = ADMIN.roles.find(r => r.subject === (known.handle || known.admin || b.subject) && r.role === b.role
      && (r.scopeTeamId || null) === (team ? team.id : null) && (r.scopeTargetId || null) === (conn ? conn.id : null));
    if (dupe) {
      return mockFail('This person already holds ' + b.role + ' over that scope (role ' + dupe.id + ', already exists and is unchanged). Revoke it first if you meant to change its ceiling or expiry.', 409, 'duplicate', { roleId: dupe.id });
    }
    const row = {
      id: mockId('r'), subject: known.handle || known.admin || b.subject, name: mockRoleName(b.subject),
      role: b.role,
      scopeTeamId: team ? team.id : null, scopeTeamName: team ? team.name : null,
      scopeTargetId: conn ? conn.id : null, scopeTargetName: conn ? conn.name : null,
      maxTier: tier, validUntil: b.validUntil || null,
      reason: b.reason || null, source: 'direct',
    };
    ADMIN.roles = [row, ...ADMIN.roles];
    mockAudit('Added role', row.name + ' · ' + row.role + (row.scopeTeamName ? ' · ' + row.scopeTeamName : '') + (row.maxTier ? ' · up to ' + row.maxTier : ''), 'scope');
    return row;
  }, 260),
  adminDelRole: (id) => mockDelay(() => {
    // A mirrored row is refused with the source named, because "revoke failed"
    // leaves the admin with nowhere to go.
    if (String(id).indexOf('mirror:') === 0) {
      return mockFail('This role mirrors the admins table — remove it there instead.', 409, 'mirrored');
    }
    const r = ADMIN.roles.find(x => x.id === id);
    if (!r) return mockFail('No such active role.', 404, 'not_found');
    ADMIN.roles = ADMIN.roles.filter(x => x.id !== id);
    if (r) mockAudit('Removed role', r.name + ' · ' + r.role, 'reject');
    return null;
  }, 200),
  adminDelScope: (id) => mockDelay(() => {
    const s = ADMIN.scopes.find(x => x.id === id);
    if (s && s.role === 'super' && ADMIN.scopes.filter(x => x.role === 'super').length < 2) return mockFail('Refusing to remove the last super-admin.', 409, 'conflict');
    ADMIN.scopes = ADMIN.scopes.filter(x => x.id !== id);
    if (s) mockAudit('Removed admin', s.admin, 'reject');
    return null;
  }, 200),

  // ---- admin: connections registry ----
  adminConnections: () => mockDelay(() => ({ connections: connRegistry().slice() }), 220),
  // The tag vocabulary, DERIVED from the fleet rather than stored beside it: a
  // key exists because some connection carries it, so a key nobody uses cannot
  // linger in the picker after its last target is gone. Reserved keys are always
  // present, at zero usage if need be. Values come with counts, so the form can
  // offer what the fleet already says instead of inviting a fourth spelling of
  // the same account id.
  adminTagKeys: () => mockDelay(() => {
    const keys = {};
    (window.QH_TAG_KEYS || []).forEach(t => { keys[t.key] = { key: t.key, label: t.label, reserved: true, count: 0, values: {} }; });
    connRegistry().forEach(c => Object.keys(c.tags || {}).forEach(k => {
      const v = c.tags[k];
      if (v == null || v === '') return;
      if (!keys[k]) keys[k] = { key: k, label: k, reserved: false, count: 0, values: {} };
      keys[k].count++;
      keys[k].values[v] = (keys[k].values[v] || 0) + 1;
    }));
    return { keys: Object.keys(keys).map(k => ({ ...keys[k], values: Object.keys(keys[k].values).sort().map(v => ({ value: v, count: keys[k].values[v] })) })) };
  }, 180),
  adminCreateConnection: (b) => mockDelay(() => {
    const reg = connRegistry();
    if (reg.some(c => c.name === b.alias)) return mockFail('A connection named “' + b.alias + '” already exists.', 409, 'conflict');
    const eid = b.engine || 'postgres';
    const creds = { ro: { username: '', configured: false, placeholder: false }, rw: { username: '', configured: false, placeholder: false }, ddl: { username: '', configured: false, placeholder: false } };
    Object.keys(b.credentials || {}).forEach(t => { if (creds[t]) creds[t] = { username: (b.credentials[t] || {}).username || '', configured: !!(b.credentials[t] || {}).password, placeholder: false }; });
    const row = { id: b.alias, name: b.alias, engineId: eid, engine: mockEngineName(eid),
      env: 'production', enabled: false, host: b.host, port: b.port || DEFAULT_PORT[eid], defaultDatabase: b.defaultDatabase,
      notes: b.notes || '', databases: [{ id: b.defaultDatabase, name: b.defaultDatabase, tier: 'RO' }], credentials: creds,
      tags: mockNormTags(b.tags || {}).tags || {} };
    ADMIN.connections = [...reg, row];
    mockAudit('Registered connection', row.name + ' · ' + row.host, 'scope');
    return row;
  }, 420),
  adminUpdateConnection: (conn, b) => mockDelay(() => {
    const row = connRegistry().find(c => c.id === conn);
    if (!row) return mockFail('Unknown connection.', 404, 'not_found');
    if (b.alias) { row.name = b.alias; }
    ['host', 'port', 'defaultDatabase', 'notes'].forEach(k => { if (b[k] != null) row[k] = b[k]; });
    if (b.engine) { row.engineId = b.engine; row.engine = mockEngineName(b.engine); }
    if (b.enabled != null) row.enabled = !!b.enabled;
    // Tags arrive as the WHOLE bag and replace it. A merge patch cannot express
    // “this key is gone”, and a tag that survives its own deletion is worse than
    // no tag at all: it keeps answering for a machine that has moved.
    // `null`/absent = not editing tags; `{}` clears them. Server-side limits
    // (CODE_TO_DESIGN_BRIEF 2026-08-15 §1.2) are enforced here too, so the form
    // meets them in the prototype rather than in production.
    if (b.tags) {
      const t = mockNormTags(b.tags);
      if (t.error) return mockFail(t.error, 400, 'invalid_tags');
      row.tags = t.tags;
    } else if (b.tags !== undefined && b.tags !== null) {
      row.tags = {};
    }
    Object.keys(b.credentials || {}).forEach(t => {
      const inc = b.credentials[t] || {};
      row.credentials[t] = { username: inc.username || (row.credentials[t] || {}).username || '', configured: inc.password ? true : !!(row.credentials[t] || {}).configured, placeholder: false };
    });
    mockAudit('Updated connection', row.name + (b.tags ? ' · ' + ((window.qhHosting && window.qhHosting(row)) || 'untagged') : ''), 'scope');
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
      : { ok: true, error: null, latencyMs: 18 + ((b.host || '').length * 3 % 40), serverVersion: mockEngineVer(b.engine) };
  }, 700),
  adminTestConnection: (conn) => mockDelay(() => {
    const row = connRegistry().find(c => c.id === conn);
    if (!row) return mockFail('Unknown connection.', 404, 'not_found');
    const login = row.replicaOf ? (connRegistry().find(c => c.name === row.replicaOf) || row) : row;
    if (!login.credentials.ro.configured) return { ok: false, error: 'no read-only credentials stored', latencyMs: null, serverVersion: null };
    return { ok: true, error: null, latencyMs: 16 + (row.id.length * 5 % 40), serverVersion: mockEngineVer(row.engineId) };
  }, 620),
  // POST /connections/{conn}/schema-refresh, with `?database=<name>` for ONE
  // database instead of every database on the connection (2026-08-20). Requires
  // the `review` capability, so the sidebar hides the item for a developer.
  adminSchemaRefresh: (conn, database) => mockDelay(() => {
    const row = connRegistry().find(c => c.id === conn);
    if (!row) return mockFail('Unknown connection.', 404, 'not_found');
    const dbs = database ? row.databases.filter(d => d.name === database || d.id === database) : row.databases;
    if (database && !dbs.length) return mockFail('There is no database "' + database + '" on ' + conn + '.', 400, 'unknown_database');
    const n = dbs.reduce((a, d) => a + ((targetOf(row.id, d.id).db || {}).tables || []).length, 0);
    mockAudit('Refreshed schema snapshot', conn + (database ? '/' + database : ''), 'scope');
    return { tables: n, databases: dbs.length };
  }, 900),
  // POST /admin/connections/bulk (CODE 2026-09-23 §6b): several connections,
  // enable / disable and/or ONE credential set on all of them. All or nothing —
  // any refusal is a 409 naming every refused connection, and nothing is
  // written. Enabling still needs a real read-only credential, which may arrive
  // in the same call, so it is checked against what the row WOULD hold.
  adminBulkConnections: (b) => mockDelay(() => {
    const names = (b && b.connections) || [];
    if (!names.length) return mockFail('Pick at least one connection.', 400, 'no_connections');
    const creds = (b && b.credentials) || {};
    if (Object.keys(creds).some(t => ['ro', 'rw', 'ddl'].indexOf(t) < 0 || !String((creds[t] || {}).username || '').trim() || !(creds[t] || {}).password)) {
      return mockFail('A credential needs a username and a password.', 400, 'bad_credentials');
    }
    const refused = [], rows = [];
    names.forEach(n => {
      const c = connRegistry().find(x => x.id === n || x.name === n);
      if (!c) { refused.push({ connection: n, status: 404, reason: 'No such connection.' }); return; }
      const ro = creds.ro ? { configured: true, placeholder: false } : ((c.credentials || {}).ro || {});
      // A replica runs on its primary's login: the credential rule is not its.
      if (b.enabled === true && !c.replicaOf && !(ro.configured && !ro.placeholder)) {
        refused.push({ connection: c.name, status: 409, reason: ro.placeholder ? 'its read-only credential is still the import placeholder' : 'it has no read-only credential' });
      } else rows.push(c);
    });
    if (refused.length) return mockFail(refused.length + ' of ' + names.length + ' refused — nothing was changed.', 409, 'refused', { refused });
    const results = rows.map(c => ({ connection: c.name, enabled: b.enabled != null ? !!b.enabled : c.enabled !== false, credentials: Object.keys(creds) }));
    if (b.dryRun) return { dryRun: true, applied: 0, results };
    rows.forEach(c => {
      if (b.enabled != null) c.enabled = !!b.enabled;
      Object.keys(creds).forEach(t => { c.credentials[t] = { username: String(creds[t].username).trim(), configured: true, placeholder: false }; });
    });
    mockAudit('Changed connections in bulk', rows.map(c => c.name).join(', ') + (b.enabled != null ? ' · ' + (b.enabled ? 'enabled' : 'disabled') : '')
      + (Object.keys(creds).length ? ' · ' + Object.keys(creds).join('/') + ' credential' : ''), 'scope');
    return { applied: rows.length, results };
  }, 420),
  // GET /admin/manual-runs + POST /admin/manual-runs/{id}/close (CODE 2026-09-23
  // §5): what the bot handed to a DBA, oldest first. Marking one failed needs a
  // reason (400), and it is 409 once it is not waiting any more.
  adminManualRuns: () => mockDelay(() => ({ items: ADMIN.manualRuns.slice().sort((a, b) => (a.escalatedAt < b.escalatedAt ? -1 : 1)) }), 180),
  adminCloseManualRun: (id, b) => mockDelay(() => {
    const it = ADMIN.manualRuns.find(x => x.id === id);
    if (!it) return mockFail('This is not waiting for a DBA any more.', 409, 'not_waiting');
    const completed = !!(b && b.completed);
    if (!completed && !String((b && b.reason) || '').trim()) return mockFail('Say why it failed — the requester reads it.', 400, 'reason_required');
    ADMIN.manualRuns = ADMIN.manualRuns.filter(x => x.id !== id);
    ADMIN.manualClosed[id] = { completed, reason: (b && b.reason) || null, by: 'dba.amara' };
    mockAudit(completed ? 'Ran a handed-off query by hand' : 'Marked a handed-off query failed', it.connectionId + '/' + it.databaseId + ' · #' + id, completed ? 'scope' : 'reject');
    return { id, status: completed ? 'done' : 'failed' };
  }, 240),

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
  // GET /admin/people is EVERY principal an admin can name (CODE brief 2026-09-01
  // §4): requesters, admins with no requesters row, and identities that exist only
  // as the subject of a grant or an auto-approve row. Enabled first — the order is
  // the server's, but the marking is read from `enabled`, so the client never has
  // to infer a state from a position. Not paginated: a page boundary in a picker
  // is a person you cannot find.
  adminPeople: () => mockDelay(() => {
    const rows = ADMIN.people.map(p => ({ ...p, enabled: p.enabled !== false, kind: 'requester' }));
    const seen = new Set(rows.map(r => r.handle));
    const extra = (handle, kind) => {
      if (!handle || seen.has(handle)) return;
      seen.add(handle);
      const g = ADMIN.grants.find(x => x.subject === handle && x.subjectName);
      const name = (g && g.subjectName) || handle;
      rows.push({ id: 'p_' + handle.replace(/[^A-Za-z0-9]/g, ''), handle, name, initials: mockInitials(name),
        email: mockEmail(handle), enabled: true, kind });
    };
    ADMIN.scopes.forEach(s => extra(s.admin, 'admin'));
    ADMIN.grants.filter(g => g.subjectType === 'user').forEach(g => extra(g.subject, 'grant_only'));
    ADMIN.auto.forEach(a => extra(String(a.user || '').indexOf(' (team)') >= 0 ? null : a.user, 'grant_only'));
    return { people: rows.sort((a, b) => (a.enabled === b.enabled ? 0 : a.enabled ? -1 : 1)) };
  }, 160),

  // GET /admin/people/resolve?principal=… (CODE brief 2026-08-22 §3). Three
  // answers, and the combo says a different sentence for each: a person QueryHub
  // already has; `known: false` WITH a name — not here yet, but the directory
  // says who it is; and `known: false` with no name, which is the mistype, caught
  // before the grant exists rather than after someone cannot sign in.
  adminResolvePerson: (principal) => mockDelay(() => {
    const me = mockUser();
    if (!me || me.role === 'developer') return mockFail('Admin only.', 403, 'forbidden');
    const raw = String(principal || '').trim();
    if (!raw) return mockFail('No principal given.', 400, 'bad_request');
    const p = ADMIN.people.find(x => x.handle === raw || x.id === raw);
    if (p) return { principal: p.handle, known: true, name: p.name, email: p.email || mockEmail(p.handle),
      enabled: true, admin: ADMIN.scopes.some(s => s.admin === p.handle) };
    // An admin-only principal IS known — it has an admins row — and is exactly
    // the case the people list cannot see.
    const sc = ADMIN.scopes.find(s => s.admin === raw);
    if (sc) {
      const g = ADMIN.grants.find(x => x.subject === raw && x.subjectName);
      return { principal: raw, known: true, name: (g && g.subjectName) || raw, email: mockEmail(raw), enabled: true, admin: true };
    }
    const dir = mockDirectory(raw);
    if (dir) return { principal: raw, known: false, name: dir.name, email: dir.email || null, enabled: true, admin: false };
    return { principal: raw, known: false, name: null, email: null, enabled: false, admin: false };
  }, 240),

  // ---- admin: one person's resolved access (CODE brief 2026-08-20 §6) ----
  // The SAME resolution a submission performs — own grants plus every grant of
  // every team they are in, more permissive winning per connection — so this
  // panel cannot disagree with what happens when they press Run. That is the
  // whole point of it existing rather than the admin reading two tables.
  adminEffectiveAccess: (id) => mockDelay(() => {
    // The roster is wider than the requesters table (§4), so an admin-only or
    // grant-only principal has to resolve too — it is exactly the identity whose
    // reach nobody can otherwise see.
    const p = ADMIN.people.find(x => x.id === id || x.handle === id)
      || (ADMIN.scopes.some(s => s.admin === id) || ADMIN.grants.some(g => g.subject === id)
        ? { id, handle: id, name: (ADMIN.grants.find(g => g.subject === id && g.subjectName) || {}).subjectName || id }
        : null);
    if (!p) return mockFail('Unknown person.', 404, 'not_found');
    return effectiveFor(p);
  }, 260),
  // GET /admin/teams/{id}/effective-access (design 2026-09-22 §1, NEW — the
  // brief says it will be added to match the person endpoint). A team resolves
  // to its OWN live grants; what it cannot say alone is where a member's
  // personal grant takes precedence, so each row carries `overriddenFor` — the
  // members for whom this row is NOT the answer. Plus the people who approve
  // the team's requests, because approval scope is team × connection × tier.
  adminTeamEffectiveAccess: (id) => mockDelay(() => {
    const t = ADMIN.teams.find(x => x.id === id || x.name === id);
    if (!t) return mockFail('Unknown team.', 404, 'not_found');
    const person = (h) => ADMIN.people.find(p => p.handle === h) || { handle: h, name: h, enabled: true };
    const mine = ADMIN.grants.filter(g => g.subjectType === 'team' && g.subject === t.name && effLive(g));
    // One row per CONNECTION with `perDatabase` under it (CODE 2026-09-23 §4):
    // `tier` is the highest, so where `mixedTiers` is true a single tier would
    // overstate the rest. `enabled` is the connection's own switch.
    const access = [...new Set(mine.map(g => g.connectionId))].map(cid => {
      const gs = mine.filter(g => g.connectionId === cid);
      const per = {};
      gs.forEach(g => effDbs(g).forEach(db => { const cur = per[db];
        if (!cur || EFF_RANK[g.tier] > EFF_RANK[cur.tier]) per[db] = { database: db === '*' ? null : db, tier: g.tier, expiresAt: g.expiresAt || null }; }));
      const perDatabase = Object.keys(per).map(k => per[k]);
      const tiers = [...new Set(perDatabase.map(x => x.tier))].sort((a, b) => EFF_RANK[b] - EFF_RANK[a]);
      const all = !!per['*'];
      const ends = perDatabase.map(x => x.expiresAt).filter(Boolean).sort();
      // `overriddenFor` is decided PER SERVER (rule 4, CODE 2026-09-24 (b)): a
      // member with their own (non-merging) grant anywhere on this connection
      // is listed, even when it names another database. Each entry says what
      // they get instead — THEIR databases, grouped by tier and end. An ended
      // entry appears only when nothing of theirs is live here.
      const over = {};
      (t.members || []).forEach(h => {
        const own = ADMIN.grants.filter(x => x.subjectType === 'user' && x.subject === h && x.connectionId === cid);
        if (!own.some(x => !x.mergeWithTeam)) return;
        const dec = effDecide(own, gs);
        const ended = own.filter(x => !effLive(x) && !x.mergeWithTeam).sort((a, b) => (a.expiresAt < b.expiresAt ? 1 : -1))[0];
        if (Object.keys(dec).some(db => dec[db].blocked)) {
          over[h + ':ended'] = { handle: h, name: person(h).name, tier: ended.tier, databases: effDbs(ended), expired: true, expiresAt: ended.expiresAt };
          return;
        }
        Object.keys(dec).forEach(db => {
          const d = dec[db];
          if (d.source !== 'user') return;
          const k = h + ':' + d.tier + ':' + (d.expiresAt || '');
          (over[k] = over[k] || { handle: h, name: person(h).name, tier: d.tier, databases: [], expired: false, expiresAt: d.expiresAt || null }).databases.push(db);
        });
      });
      Object.keys(over).forEach(k => { const d = over[k].databases; if (d.indexOf('*') >= 0) over[k].databases = ['*']; });
      return { connectionId: cid, tier: tiers[0], databases: all ? null : Object.keys(per), allDatabases: all,
        perDatabase, mixedTiers: tiers.length > 1, enabled: (connRegistry().find(c => c.id === cid) || {}).enabled !== false,
        source: 'team', sourceTeam: t.name, expiresAt: ends[0] || null, overriddenFor: Object.keys(over).map(k => over[k]) };
    });
    const approvers = ADMIN.scopes.filter(s => s.role !== 'super' && (s.teams || []).indexOf(t.name) >= 0).map(s => ({
      handle: s.admin, name: mockRoleName(s.admin),
      maxTier: (s.canApprove || []).slice().sort((a, b) => EFF_RANK[b] - EFF_RANK[a])[0] || null,
      scopeTargetsAll: (s.connections || []).indexOf('*') >= 0, scopeTargets: (s.connections || []).filter(c => c !== '*') }))
      .concat(ADMIN.roles.filter(r => r.role === 'approver' && r.scopeTeamId === t.id && !ADMIN.scopes.some(s => s.admin === r.subject)).map(r => ({
        handle: r.subject, name: r.name, maxTier: r.maxTier, scopeTargetsAll: !r.scopeTargetId, scopeTargets: r.scopeTargetId ? [r.scopeTargetId] : [], via: 'role' })));
    return {
      kind: 'team', team: { id: t.id, name: t.name, desc: t.desc || '', syncedFrom: t.syncedFrom || null },
      members: (t.members || []).map(h => { const p = person(h); return { handle: h, name: p.name, enabled: p.enabled !== false, slackId: p.slackId || null }; }),
      access,
      autoApprove: ADMIN.auto.filter(a => a.user === t.name + ' (team)').map(a => ({ connectionId: a.connectionId || null, databaseId: a.databaseId || null,
        allTargets: !a.connectionId, tier: a.tier, expiresAt: a.expiresAt || null, via: null })),
      approvers,
      // Fleet-wide approvers are counted, not listed: every team has them, and
      // a list of the same names under every team says nothing about THIS one.
      superApprovers: ADMIN.scopes.filter(s => s.role === 'super').length,
    };
  }, 260),

  // POST /admin/people/{id}/copy-access — { source, includeTeams, includeAutoApprove,
  // tier, mode, dryRun } (CODE brief 2026-09-01 §2).
  // includeTeams FALSE (the UI default) flattens: the whole union written as
  // explicit per-user grants — what hand-copying loses, and what survives teams
  // becoming pods. TRUE joins the same teams and copies only the source's OWN
  // grants; the team access then arrives through membership, and so does whatever
  // those teams are granted later.
  // `mode: 'replace'` also REVOKES what the source does not have, so the person
  // ends up matching the source exactly; `dryRun` resolves the whole thing and
  // writes nothing, which is what the confirm step is drawn from.
  adminCopyAccess: (id, b) => mockDelay(() => {
    const p = ADMIN.people.find(x => x.id === id || x.handle === id);
    const src = ADMIN.people.find(x => x.id === (b || {}).source || x.handle === (b || {}).source);
    if (!p || !src) return mockFail('Unknown person.', 404, 'not_found');
    if (p.handle === src.handle) return mockFail('That is the same person.', 400, 'bad_request');
    const mode = (b && b.mode) === 'replace' ? 'replace' : 'merge';
    const dry = !!(b && b.dryRun);
    const eff = effectiveFor(src);
    const rows = (b && b.includeTeams)
      ? eff.access.filter(t => t.source === 'user')
      : eff.access.filter(t => t.source !== 'admin_or_bypass');
    const keep = new Set(rows.map(t => t.connectionId));
    // Replace only ever touches rows written against THIS person. What a team
    // gives them is changed on the team, and the control plane is in neither half.
    const doomed = mode === 'replace'
      ? ADMIN.grants.filter(g => g.subjectType === 'user' && g.subject === p.handle && !keep.has(g.connectionId))
      : [];
    const autoRows = (b && b.includeAutoApprove) ? ADMIN.auto.filter(a => a.user === src.handle) : [];
    const teamNames = (b && b.includeTeams)
      ? eff.teams.map(t => t.name).filter(name => {
        const t = ADMIN.teams.find(x => x.name === name);
        return t && (t.members || []).indexOf(p.handle) < 0;
      }) : [];
    if (dry) {
      // By server ALIAS, not by id: the sentence an admin confirms has to name
      // the things they recognise.
      return { dryRun: true, mode, wouldWrite: [...new Set(rows.map(t => t.connectionId))],
        wouldRevoke: doomed.map(g => ({ targetId: g.id, connectionId: g.connectionId, tier: g.tier })),
        wouldCopyAutoApprove: autoRows.length, wouldJoinTeams: teamNames };
    }
    let written = 0;
    rows.forEach(t => {
      const dbs = t.allDatabases ? ['*'] : (t.databases || ['*']);
      const tier = (b && b.tier) || t.tier;
      const ex = ADMIN.grants.find(g => g.subjectType === 'user' && g.subject === p.handle && g.connectionId === t.connectionId && g.tier === tier);
      if (ex) { ex.databases = dbs; ex.tier = tier; }
      else ADMIN.grants = [{ id: mockId('g'), subjectType: 'user', subject: p.handle, subjectName: p.name,
        connectionId: t.connectionId, databases: dbs, tier, expiresAt: null,
        grantedBy: 'dba.amara', grantedByName: 'Amara Osei', grantedAt: isoNow() }, ...ADMIN.grants];
      written++;
    });
    const revoked = doomed.map(g => ({ targetId: g.id, connectionId: g.connectionId, tier: g.tier }));
    if (revoked.length) ADMIN.grants = ADMIN.grants.filter(g => !doomed.some(d => d.id === g.id));
    let autoApproveCopied = 0;
    autoRows.forEach(a => {
      const ex = ADMIN.auto.find(x => x.user === p.handle && x.connectionId === a.connectionId && x.databaseId === a.databaseId);
      if (ex) { ex.tier = a.tier; ex.expiresAt = a.expiresAt; }
      else ADMIN.auto = [{ ...a, id: mockId('a'), user: p.handle, userName: p.name, createdByName: 'Amara Osei' }, ...ADMIN.auto];
      autoApproveCopied++;
    });
    const teams = teamNames.filter(name => {
      const t = ADMIN.teams.find(x => x.name === name);
      if (!t) return false;
      t.members = (t.members || []).concat([p.handle]);
      return true;
    });
    mockAudit('Copied access from ' + src.handle + (mode === 'replace' ? ' (replace)' : ''),
      p.handle + ' · ' + written + ' connection' + (written === 1 ? '' : 's')
      + (revoked.length ? ' · revoked ' + revoked.length : '')
      + (autoApproveCopied ? ' · ' + autoApproveCopied + ' auto-approve' : '')
      + (teams.length ? ' · joined ' + teams.join(', ') : ''), 'grant');
    // `written` is a COUNT and always was; `writtenTargets` is the names, which is
    // what a sentence needs (CODE brief 2026-09-01 §2).
    return { written, writtenTargets: rows.map(t => t.connectionId), revoked, autoApproveCopied, teams, mode };
  }, 340),
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

  // ---- admin: masking exemptions ----
  // Super-admin only — this is the one screen whose whole purpose is to reduce
  // protection, so the 403 is enforced at the endpoint and not only in the nav.
  adminMaskExemptions: () => mockDelay(() => {
    if (!mockUser() || mockUser().role !== 'super') return mockFail('Super-admin only.', 403, 'forbidden');
    return { exemptions: maskRegistry(), maskingEnabled: maskingOn(),
      nameRules: Object.keys(window.QH_PII_CATALOG || {}).length, valueDetectors: MASK_DETECTORS };
  }, 220),
  adminMaskCatalog: (connectionId, databaseId) => mockDelay(() => {
    const cat = maskCatalogFor(connectionId, databaseId);
    if (!cat) return mockFail('No catalog snapshot for that database.', 404, 'no_snapshot');
    return cat;
  }, 240),
  adminMaskPreview: (b) => mockDelay(() => {
    if (b.scope !== 'column') {
      // A schema/database/server exemption has no single table to sample, so
      // the preview says what it covers instead of inventing a row. Better an
      // honest count than a sample that implies the change is one column wide.
      const cat = maskCatalogFor(b.connectionId, b.databaseId);
      const tables = cat ? cat.schemas.reduce((n, s) => n + (b.scope === 'schema' && s.name !== b.schema ? 0 : s.tables.length), 0) : 0;
      const cols = cat ? cat.schemas.reduce((n, s) => n + (b.scope === 'schema' && s.name !== b.schema ? 0 : s.tables.reduce((m, t) => m + t.columns.filter(c => c.rule).length, 0)), 0) : 0;
      return { seen: false, wide: true, tables, maskedColumns: cols };
    }
    const h = maskHash(b.connectionId + b.table + b.column);
    // ~1 in 6 tables has not been queried in the window. That state is on the
    // screen because it is the one where the operator has no evidence and has
    // to decide anyway.
    if (h % 6 === 0) return { seen: false, wide: false, days: 30 };
    const others = (window.qhColumnsFor ? window.qhColumnsFor(b.table) : []).map(c => c.name)
      .filter(n => n !== b.column).slice(0, 3);
    const cols = ['id'].concat(others.filter(n => n !== 'id')).concat([b.column]);
    const row = {};
    cols.forEach((c, i) => { row[c] = c === 'id' ? String(100340 + (h % 900)) : maskSample(c, h + i); });
    const before = {}, after = {};
    cols.forEach(c => { before[c] = maskMaskedForm(c, row[c]); after[c] = c === b.column ? row[c] : maskMaskedForm(c, row[c]); });
    const total = 6 + (h % 40);
    return { seen: true, wide: false, days: 30,
      sql: 'SELECT ' + cols.join(', ') + '\nFROM ' + b.schema + '.' + b.table + '\nWHERE created_at > now() - interval \'7 days\'\nLIMIT 50;',
      by: h % 2 ? 'ben.donnelly' : 'sam.archer', at: new Date(Date.now() - (1 + h % 9) * 86400000).toISOString(),
      columns: cols, before, after,
      // The evidence the join decision needs. The screen cannot count this for
      // the person; the server can.
      joinStats: { total, joined: Math.round(total * ((h % 5) / 5)) } };
  }, 320),
  adminAddMaskExemption: (b) => mockDelay(() => {
    if (!mockUser() || mockUser().role !== 'super') return mockFail('Super-admin only.', 403, 'forbidden');
    if (!ADMIN.mask) ADMIN.mask = maskSeed();
    if (['column', 'table', 'schema', 'database', 'server', 'fleet'].indexOf(b.scope) < 0) return mockFail('Unknown scope.', 400, 'bad_scope');
    // The reason is not a note field — it is what an auditor reads when asking
    // why a column stopped being protected, so the server requires it too.
    if (!b.reason || !String(b.reason).trim()) return mockFail('An exemption needs a reason — it is the only record of why this data stopped being protected.', 400, 'reason_required');
    const reg = connRegistry();
    // Fleet-wide carries no connection — that IS its reach. Checked against the
    // SCOPE so a missing connectionId on any other rung is still a 404 rather
    // than silently becoming the widest row in the model.
    const c = b.scope === 'fleet' ? null : reg.find(x => x.id === b.connectionId);
    if (b.scope !== 'fleet' && !c) return mockFail('No such connection.', 404, 'unknown_target');
    if (b.scope !== 'server' && b.scope !== 'fleet') {
      const db = (c.databases || []).find(d => d.id === b.databaseId);
      if (!db) return mockFail('No such database on ' + c.name + '.', 404, 'unknown_target');
    }
    const same = (x) => x.connectionId === b.connectionId && (x.databaseId || null) === (b.databaseId || null)
      && (x.schema || null) === (b.schema || null) && (x.table || null) === (b.table || null) && (x.column || null) === (b.column || null);
    const dupe = ADMIN.mask.find(same);
    if (dupe) return mockFail('That target is already exempt (' + dupe.id + ')' + (dupe.enabled ? '' : ', currently turned off') + '.', 409, 'duplicate', { exemptionId: dupe.id });
    const row = { id: mockId('mx'), connectionId: b.scope === 'fleet' ? null : b.connectionId, databaseId: b.databaseId || null,
      schema: b.schema || null, table: b.table || null, column: b.column || null,
      scope: b.scope, strength: b.strength === 'full' ? 'full' : 'soft', survivesJoin: !!b.survivesJoin,
      audience: b.audience === 'super' ? 'super' : 'everyone', reason: String(b.reason).trim(),
      enabled: true, createdBy: (mockUser() && mockUser().handle) || 'admin', createdAt: isoNow() };
    ADMIN.mask = [row, ...ADMIN.mask];
    mockAudit('Added masking exemption', maskAuditTarget(row), 'scope');
    return row;
  }, 300),
  adminSetMaskExemption: (id, enabled) => mockDelay(() => {
    if (!ADMIN.mask) ADMIN.mask = maskSeed();
    const r = ADMIN.mask.find(x => x.id === id);
    if (!r) return mockFail('No such exemption.', 404, 'not_found');
    r.enabled = !!enabled;
    mockAudit(enabled ? 'Enabled masking exemption' : 'Turned off masking exemption', maskAuditTarget(r), enabled ? 'scope' : 'reject');
    return r;
  }, 200),
  // PATCH /admin/mask-exemptions/{id} — the CONSEQUENCES and the reason, never
  // the reach. Soft-vs-full, the join answer, the audience and the reason are
  // all corrections to a decision about a target that has not changed, and
  // making them re-typeable is what turned this screen from a wizard into a
  // settings screen. Where it reaches stays immutable: an exemption whose
  // reason still describes the old scope is worse than two rows, so narrowing
  // is still turn-off-and-create.
  //
  // Built to CODE's shipped contract (2026-09-15 (b)): any subset of the four
  // consequence fields plus `enabled`; a reach field is REFUSED and the message
  // NAMES it; an unknown field is refused too (`extra: forbid`, so a field
  // neither side has thought of errors instead of being silently dropped); an
  // edit that changes nothing writes nothing and says `changed: false` —
  // otherwise every visit to the form would stamp an edit nobody made. The
  // reply is the STORED row, from the same builder the list reads, so a screen
  // rendering the response cannot show an edit the database refused.
  adminUpdateMaskExemption: (id, patch) => mockDelay(() => {
    if (!mockUser() || mockUser().role !== 'super') return mockFail('Super-admin only.', 403, 'forbidden');
    if (!ADMIN.mask) ADMIN.mask = maskSeed();
    const r = ADMIN.mask.find(x => x.id === id);
    if (!r) return mockFail('No such exemption.', 404, 'not_found');
    const p = patch || {};
    const reach = ['scope', 'connectionId', 'databaseId', 'schema', 'table', 'column'].filter(k => p[k] !== undefined);
    if (reach.length)
      return mockFail('Where an exemption reaches cannot be edited — turn this one off and write a new one. Sent: ' + reach.join(', ') + '.', 400, 'reach_immutable');
    const allowed = ['strength', 'survivesJoin', 'audience', 'reason', 'enabled'];
    const extra = Object.keys(p).filter(k => allowed.indexOf(k) < 0);
    if (extra.length) return mockFail('Unknown field' + (extra.length > 1 ? 's' : '') + ': ' + extra.join(', ') + '.', 400, 'forbid');
    if (p.reason !== undefined && !String(p.reason).trim())
      return mockFail('An exemption needs a reason — it is the only record of why this data stopped being protected.', 400, 'reason_required');
    const next = {
      strength: p.strength !== undefined ? (p.strength === 'full' ? 'full' : 'soft') : r.strength,
      survivesJoin: p.survivesJoin !== undefined ? !!p.survivesJoin : r.survivesJoin,
      audience: p.audience !== undefined ? (p.audience === 'super' ? 'super' : 'everyone') : r.audience,
      reason: p.reason !== undefined ? String(p.reason).trim() : r.reason,
      enabled: p.enabled !== undefined ? !!p.enabled : r.enabled,
    };
    if (allowed.every(k => next[k] === r[k])) return { ...r, changed: false };
    Object.assign(r, next);
    r.updatedBy = (mockUser() || {}).handle || 'you';
    r.updatedAt = isoNow();
    mockAudit('Changed masking exemption', maskAuditTarget(r), 'scope');
    return r;
  }, 260),
  adminDelMaskExemption: (id) => mockDelay(() => {
    if (!ADMIN.mask) ADMIN.mask = maskSeed();
    const r = ADMIN.mask.find(x => x.id === id);
    if (!r) return mockFail('No such exemption.', 404, 'not_found');
    ADMIN.mask = ADMIN.mask.filter(x => x.id !== id);
    mockAudit('Removed masking exemption', maskAuditTarget(r), 'reject');
    return null;
  }, 200),

  // ---- admin: insights & config ----
  // ---- admin: audit ----
  // `adminAudit()` (no args) stays the recent-window read the rest of the panel
  // refreshes after a write. The audit SCREEN uses adminAuditSearch, which is
  // paged, faceted and searches the whole table server-side.
  adminAuditSearch: (params) => mockDelay(() => audSearch(params), 280),
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

// MOCK of qh-api.jsx's `qh:connections-changed` (CODE 2026-09-23 §2): fired after
// a SUCCESSFUL create, update, delete, schema refresh or bulk change, so the
// shell re-reads /connections instead of keeping the list it read at sign-in.
// A dry run changes nothing and fires nothing.
['adminCreateConnection', 'adminUpdateConnection', 'adminDeleteConnection', 'adminSchemaRefresh', 'adminBulkConnections'].forEach(k => {
  const f = qhApi[k];
  qhApi[k] = (...a) => f(...a).then(r => {
    if (!(a[0] && a[0].dryRun)) { try { window.dispatchEvent(new Event('qh:connections-changed')); } catch (e) { /* no window */ } }
    return r;
  });
});

Object.assign(window, { qhApi, qhSignInWithSlack, qhTimeAgo, qhScheduleToISO, API_BASE });
