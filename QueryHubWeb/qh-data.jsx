// QueryHub — mock data + SQL classification + PII detection
// All exported to window for cross-script (Babel) sharing.

// ---------- Brand resolver ----------
// Single place for brand-dependent copy + colors. Reads <html data-brand>.
function qhBrand() {
  const MAP = {
    warm: { org: 'Acme', slack: 'acme.slack.com', mark: '#C4603F', avatarFrom: '#B85A38', avatarTo: '#E0906E' },
  };
  return MAP.warm;
}
window.qhBrand = qhBrand;

// ---------- Connections / databases (mock) ----------
// tier: highest privilege the developer holds on that database
const QH_CONNECTIONS = [
  {
    id: 'prod-main', name: 'prod-main', engine: 'PostgreSQL 15', env: 'production',
    tags: { provider: 'aws', service: 'RDS', account: '4417-0219-0871' },
    databases: [
      { id: 'users',     name: 'users',     tier: 'RO',  tables: ['users', 'user_kyc', 'sessions', 'login_history', 'CustomerNotes'] },
      { id: 'payments',  name: 'payments',  tier: 'RW',  tables: ['transactions', 'payouts', 'cards', 'invoices'] },
      { id: 'analytics', name: 'analytics', tier: 'DDL', tables: ['events', 'daily_metrics', 'p_metrics_funnel', 'cohorts'] },
    ],
  },
  {
    id: 'prod-replica', name: 'prod-replica', engine: 'PostgreSQL 15 (read replica)', env: 'production',
    tags: { provider: 'aws', service: 'RDS', account: '4417-0219-0871' },
    databases: [
      { id: 'users_ro',     name: 'users',     tier: 'RO', tables: ['users', 'user_kyc', 'sessions'] },
      { id: 'analytics_ro', name: 'analytics', tier: 'RO', tables: ['events', 'daily_metrics', 'cohorts'] },
    ],
  },
  {
    id: 'staging', name: 'staging', engine: 'PostgreSQL 15', env: 'staging',
    tags: { provider: 'aws', service: 'RDS', account: '9930-4471-2205' },
    databases: [
      { id: 'app_stg', name: 'app', tier: 'DDL', tables: ['users', 'orders', 'feature_flags'] },
    ],
  },
  {
    id: 'reporting-mssql', name: 'reporting-mssql', engine: 'SQL Server 2022', env: 'production',
    tags: { provider: 'huawei', service: 'ECS', account: 'hw-proj-tr-01' },
    databases: [
      { id: 'ReportingDW', name: 'ReportingDW', tier: 'RO', tables: ['FactTrades', 'DimUser', 'DimAsset', 'AuditLog'] },
    ],
  },
];
// Only engines the product can actually EXECUTE against appear here
// (engines.WIRED_ENGINES = postgres, mssql). The sample fleet used to include
// an Oracle connection — and the screenshots taken from it advertised Oracle,
// ClickHouse, Couchbase and MySQL on the landing page of a tool that runs
// neither. Engine specs exist for more dialects (they gate safety analysis the
// moment a target is tagged), but a connection in this list reads as "you can
// query this", so the list stays honest.


// ---------- Extra fleet for the schema browser (sample data) ----------
// Deliberately generic service names. This is prototype/design-canvas sample
// data, so it must never mirror a real production inventory — a service list
// on its own can identify an organization.
const QH_EXC_SERVICES = [
  ['orders', 'RO'], ['billing', 'RW'], ['conditional-order', 'RO'], ['config', 'DDL'],
  ['pricing', 'RO'], ['payments-gateway', 'RW'], ['events', 'RO'],
  ['statements', 'RO'], ['admin-panel', 'RW'], ['reporting', 'RO'], ['registry', 'RO'],
  ['scoring', 'RW'], ['notification', 'RO'], ['inventory', 'RO'],
];
function qhSvcTables(s) {
  const per = {
    orders: ['orders', 'order_items', 'shipments', 'audit_log'],
    billing: ['invoices', 'billing_ledger', 'payouts', 'audit_log'],
    'conditional-order': ['conditional_orders', 'triggers', 'executions', 'audit_log'],
    config: ['config_entries', 'feature_flags', 'service_registry', 'audit_log'],
    pricing: ['price_lists', 'price_changes', 'discounts', 'audit_log'],
    'payments-gateway': ['payments', 'transfers', 'providers', 'audit_log'],
    events: ['events', 'subscriptions', 'dead_letters', 'audit_log'],
    statements: ['statements', 'ledger_entries', 'balances', 'audit_log'],
    'admin-panel': ['operators', 'panel_actions', 'sessions', 'audit_log'],
    reporting: ['reports', 'report_runs', 'schedules', 'audit_log'],
    registry: ['entities', 'positions', 'reconciliations', 'audit_log'],
    scoring: ['signals', 'scores', 'backfills', 'audit_log'],
    notification: ['notifications', 'templates', 'delivery_log', 'audit_log'],
    inventory: ['stock_items', 'movements', 'locations', 'audit_log'],
  };
  return per[s] || [s.replace(/-/g, '_') + '_records', 'events', 'settings', 'audit_log'];
}
// The sample fleet sits in two clouds on purpose: a half-migrated estate is the
// normal case, and it is the case the provider tag exists for.
QH_EXC_SERVICES.forEach(([s, tier], i) => {
  const dbid = s.replace(/-/g, '_') + '_service';
  const hw = i % 3 === 1;
  QH_CONNECTIONS.push({ id: 'svc-prod-' + s, name: 'svc-prod-' + s, engine: 'PostgreSQL 15', env: 'production',
    tags: hw ? { provider: 'huawei', service: 'RDS', account: 'hw-proj-tr-01' }
             : { provider: 'aws', service: 'RDS', account: '4417-0219-0871' },
    databases: [{ id: dbid, name: dbid, tier, tables: qhSvcTables(s) }] });
});

// ---------- Schema generator: columns / indexes / views (deterministic mock) ----------
function qhColumnsFor(table) {
  const t = String(table).toLowerCase();
  const cols = [{ name: 'id', type: 'bigint', pk: true, nullable: false }];
  const add = (name, type, o = {}) => cols.push({ name, type, nullable: !o.nn, fk: o.fk });
  if (/user|kyc|customer|participant|operator|address/.test(t)) { add('email', 'text'); add('full_name', 'text'); add('tckn', 'varchar(11)'); }
  if (/payout|payment|invoice|commission|ledger|balance|pnl|realized|statement/.test(t)) { add('user_id', 'bigint', { fk: 'users.id' }); add('amount', 'numeric(18,2)'); add('currency', 'varchar(8)'); }
  if (/order|trade|execution|signal|position|security|reconciliation/.test(t)) { add('user_id', 'bigint', { fk: 'users.id' }); add('symbol', 'varchar(16)'); add('side', 'varchar(4)'); add('price', 'numeric(18,8)'); add('qty', 'numeric(28,8)'); }
  if (/event|log|delivery|notification|action|transfer|subscription|reward/.test(t)) { add('user_id', 'bigint', { fk: 'users.id' }); add('type', 'text'); add('payload', 'jsonb'); }
  if (/session|login/.test(t)) { add('user_id', 'bigint', { fk: 'users.id' }); add('ip', 'inet'); add('user_agent', 'text'); }
  if (/card/.test(t)) { add('user_id', 'bigint', { fk: 'users.id' }); add('card_no', 'varchar(19)'); add('brand', 'text'); }
  if (/config|flag|setting|registry|template|rule|schedule|provider/.test(t)) { add('key', 'text', { nn: true }); add('value', 'jsonb'); add('enabled', 'boolean', { nn: true }); }
  if (/block|transaction|onchain|wallet|custody/.test(t)) { add('hash', 'varchar(66)', { nn: true }); add('height', 'bigint'); add('confirmations', 'integer'); }
  add('status', 'text'); add('created_at', 'timestamptz', { nn: true }); add('updated_at', 'timestamptz');
  const seen = new Set();
  return cols.filter(c => !seen.has(c.name) && seen.add(c.name));
}
function qhIndexesFor(table) {
  const names = qhColumnsFor(table).map(c => c.name);
  const idx = [{ name: 'pk_' + table, cols: ['id'], unique: true, pk: true }];
  if (names.includes('email')) idx.push({ name: 'uq_' + table + '_email', cols: ['email'], unique: true });
  if (names.includes('hash')) idx.push({ name: 'uq_' + table + '_hash', cols: ['hash'], unique: true });
  if (names.includes('user_id')) idx.push({ name: 'idx_' + table + '_user_id', cols: ['user_id'], unique: false });
  if (names.includes('symbol')) idx.push({ name: 'idx_' + table + '_symbol', cols: ['symbol'], unique: false });
  idx.push({ name: 'idx_' + table + '_created_at', cols: ['created_at'], unique: false });
  return idx;
}
function qhViewsFor(dbId) {
  if (/analytics/.test(dbId)) return ['p_metrics_funnel', 'p_metrics_retention', 'daily_active_users'];
  if (dbId === 'payments') return ['v_failed_payouts', 'v_daily_revenue'];
  return [];
}

// ---------- Saved queries / snippets ----------
const QH_SAVED = [
  { id: 's1', name: 'Active users last 24h', conn: 'prod-replica', db: 'users_ro',
    sql: "SELECT id, email, last_seen_at\nFROM users\nWHERE last_seen_at > now() - interval '24 hours'\nORDER BY last_seen_at DESC\nLIMIT 100;" },
  { id: 's2', name: 'Failed payouts today', conn: 'prod-main', db: 'payments',
    sql: "SELECT id, user_id, amount, status, created_at\nFROM payouts\nWHERE status = 'failed'\n  AND created_at::date = current_date;" },
  { id: 's3', name: 'Daily signup funnel', conn: 'prod-replica', db: 'analytics_ro',
    sql: "SELECT day, signups, verified, first_trade\nFROM p_metrics_funnel\nWHERE day > current_date - 14\nORDER BY day;" },
  { id: 's4', name: 'Reset feature flag (staging)', conn: 'staging', db: 'app_stg',
    sql: "UPDATE feature_flags\nSET enabled = false\nWHERE key = 'new_wallet_ui';" },
];

// ---------- Query history (mock) ----------
const QH_HISTORY = [
  { id: 'h1', sql: 'SELECT count(*) FROM transactions WHERE created_at::date = current_date;', conn: 'prod-main', db: 'payments', tier: 'RO', status: 'done', rows: 1, when: '2 min ago', approver: 'auto-approve' },
  { id: 'h2', sql: "UPDATE users SET status = 'active' WHERE id = 84213;", conn: 'prod-main', db: 'users', tier: 'RW', status: 'done', rows: 1, when: '34 min ago', approver: 'dba.admin' },
  { id: 'h3', sql: 'SELECT * FROM user_kyc WHERE verified = false LIMIT 50;', conn: 'prod-replica', db: 'users_ro', tier: 'RO', status: 'done', rows: 50, when: '1 h ago', approver: 'auto-approve' },
  { id: 'h4', sql: 'ALTER TABLE events ADD COLUMN device_id text;', conn: 'prod-main', db: 'analytics', tier: 'DDL', status: 'rejected', rows: 0, when: 'Yesterday', approver: 'dba.ops', note: 'Use migration pipeline for schema changes.' },
];

// ---------- SQL classifier: RO / RW / DDL ----------
//
// PROVISIONAL, and deliberately pessimistic. The server's query_safety.analyze()
// is the only authority: it decides the credential tier, and the app replaces
// this verdict with the server's as soon as POST /api/classify answers (see
// useServerClassify in qh-app.jsx). This exists so the tier chip and the
// Run-vs-Submit label are not blank while you type.
//
// Two rules follow from being a *hint* in a security UI:
//   1. Unknown leading keyword -> DDL, never RO. Guessing "read-only" for
//      something we don't recognise is how `REFRESH MATERIALIZED VIEW` ends up
//      behind a green Run button.
//   2. Keyword lists mirror query_safety.py. If you add a keyword there, add it
//      here — but rule 1 means a missed keyword degrades to "needs approval"
//      rather than to "runs instantly".
const QH_DDL_KW = ['CREATE','ALTER','DROP','TRUNCATE','RENAME','COMMENT','GRANT','REVOKE',
                   'VACUUM','ANALYZE','REINDEX','CLUSTER','REFRESH','REASSIGN'];
const QH_RW_KW  = ['INSERT','UPDATE','DELETE','MERGE','UPSERT','REPLACE','CALL'];
// Leading keywords that are genuinely reads. Everything not in one of the three
// lists is treated as DDL by rule 1 above.
const QH_RO_KW  = ['SELECT','WITH','TABLE','VALUES','SHOW','EXPLAIN','DESCRIBE','DESC','SET','FETCH'];

function qhStripComments(sql) {
  return sql.replace(/--[^\n]*/g, ' ').replace(/\/\*[\s\S]*?\*\//g, ' ');
}

// Split on `;` but only at top level: a semicolon inside a string literal,
// a quoted identifier or a dollar-quoted body is data, not a separator.
// Without this, `SELECT ';DROP TABLE x'` counted as two statements and got
// classified DDL — which then *blocked* a legal read for an RO-grant user.
function qhSplitStatements(sql) {
  const out = [];
  let buf = '', i = 0;
  while (i < sql.length) {
    const ch = sql[i];
    if (ch === "'" || ch === '"') {
      const q = ch;
      buf += ch; i++;
      while (i < sql.length) {
        if (sql[i] === q) {
          // '' / "" is an embedded quote, not the end of the literal.
          if (sql[i + 1] === q) { buf += q + q; i += 2; continue; }
          buf += q; i++; break;
        }
        buf += sql[i]; i++;
      }
      continue;
    }
    // Postgres dollar quoting: $$ … $$ or $tag$ … $tag$.
    if (ch === '$') {
      const m = /^\$[A-Za-z_]*\$/.exec(sql.slice(i));
      if (m) {
        const tag = m[0];
        const end = sql.indexOf(tag, i + tag.length);
        const stop = end === -1 ? sql.length : end + tag.length;
        buf += sql.slice(i, stop); i = stop;
        continue;
      }
    }
    if (ch === ';') { out.push(buf); buf = ''; i++; continue; }
    buf += ch; i++;
  }
  out.push(buf);
  return out.map(s => s.trim()).filter(Boolean);
}

// Returns { tier, statements:[{kw,tier}], multi:bool }
function qhClassify(sql) {
  const clean = qhStripComments(sql);
  const stmts = qhSplitStatements(clean);
  let tier = 'RO';
  const detail = [];
  for (const s of stmts) {
    const m = s.match(/[A-Za-z]+/);
    const kw = m ? m[0].toUpperCase() : '';
    let t;
    if (QH_DDL_KW.includes(kw)) t = 'DDL';
    else if (QH_RW_KW.includes(kw)) t = 'RW';
    else if (QH_RO_KW.includes(kw)) t = 'RO';
    else t = 'DDL';               // rule 1: unrecognised is not read-only
    detail.push({ kw, tier: t });
    if (t === 'DDL') tier = 'DDL';
    else if (t === 'RW' && tier !== 'DDL') tier = 'RW';
  }
  return { tier, statements: detail, multi: stmts.length > 1, empty: stmts.length === 0,
           provisional: true };
}

// ---------- PII column catalog ----------
// name -> human label + masking strategy
const QH_PII_CATALOG = {
  email:        { label: 'Email address',   mask: 'partial' },
  phone:        { label: 'Phone number',    mask: 'partial' },
  phone_number: { label: 'Phone number',    mask: 'partial' },
  tckn:         { label: 'TR ID (TCKN)',    mask: 'full' },
  national_id:  { label: 'National ID',     mask: 'full' },
  vkn:          { label: 'Tax no (VKN)',    mask: 'full' },
  iban:         { label: 'IBAN',            mask: 'partial' },
  card_no:      { label: 'Card number',     mask: 'partial' },
  card_number:  { label: 'Card number',     mask: 'partial' },
  pan:          { label: 'Card PAN',        mask: 'partial' },
  password:     { label: 'Password hash',   mask: 'full' },
  password_hash:{ label: 'Password hash',   mask: 'full' },
  full_name:    { label: 'Full name',       mask: 'partial' },
  birth_date:   { label: 'Date of birth',   mask: 'full' },
  address:      { label: 'Postal address',  mask: 'partial' },
};

// Scan SQL for catalog columns. Returns array of {col,label,mask}.
function qhDetectPII(sql) {
  const clean = qhStripComments(sql).toLowerCase();
  const found = [];
  const seen = new Set();
  for (const col of Object.keys(QH_PII_CATALOG)) {
    const re = new RegExp('(^|[^a-z0-9_])' + col + '([^a-z0-9_]|$)');
    if (re.test(clean) && !seen.has(col)) {
      seen.add(col);
      found.push({ col, ...QH_PII_CATALOG[col] });
    }
  }
  // SELECT * implies all columns -> warn that PII may be present
  const star = /select\s+\*/.test(clean);
  return { columns: found, star };
}

// ---------- Postgres identifier quoting ----------
// Unquoted identifiers fold to lower-case in PG; anything with an uppercase
// letter, special char, leading digit, or that isn't a plain [a-z_][a-z0-9_]*
// must be double-quoted to round-trip. Applied on autocomplete + drag insert.
function qhQuoteIdent(name) {
  const s = String(name);
  if (/^[a-z_][a-z0-9_]*$/.test(s)) return s;
  return '"' + s.replace(/"/g, '""') + '"';
}
function qhQuoteList(arr) { return (arr || []).map(qhQuoteIdent).join(', '); }

// ---------- Engine abstraction (Postgres today; MSSQL / Oracle / MySQL ready) ----------
// UI reads everything engine-specific from here so adding an engine is data-only:
// identifier quoting, the version badge, and the system-catalog / roles trees.
const QH_ENGINES = {
  postgres: { label: 'PostgreSQL', badge: 'PG', q: ['"', '"'], foldsLower: true, rolesLabel: 'Roles & users', catalogLabel: 'System catalog',
    system: { 'System views': ['pg_stat_activity', 'pg_stat_user_tables', 'pg_locks', 'pg_settings'], 'Catalogs': ['pg_class', 'pg_namespace', 'pg_roles', 'pg_database'], 'information_schema': ['tables', 'columns', 'table_privileges', 'role_table_grants'] } },
  mssql: { label: 'SQL Server', badge: 'MSSQL', q: ['[', ']'], foldsLower: false, rolesLabel: 'Logins & users', catalogLabel: 'System catalog',
    system: { 'Dynamic mgmt views': ['sys.dm_exec_sessions', 'sys.dm_exec_requests', 'sys.dm_tran_locks'], 'Catalog views': ['sys.objects', 'sys.columns', 'sys.schemas', 'sys.server_principals'], 'INFORMATION_SCHEMA': ['TABLES', 'COLUMNS', 'TABLE_PRIVILEGES'] } },
  oracle: { label: 'Oracle', badge: 'ORA', q: ['"', '"'], foldsLower: false, rolesLabel: 'Users & roles', catalogLabel: 'Data dictionary',
    system: { 'Dynamic views': ['V$SESSION', 'V$SQL', 'V$LOCK'], 'Dictionary': ['ALL_TABLES', 'ALL_TAB_COLUMNS', 'DBA_USERS', 'DBA_ROLES'] } },
  mysql: { label: 'MySQL', badge: 'MYSQL', q: ['`', '`'], foldsLower: false, rolesLabel: 'Users & grants', catalogLabel: 'System schema',
    system: { 'performance_schema': ['threads', 'events_statements_current'], 'information_schema': ['TABLES', 'COLUMNS', 'USER_PRIVILEGES'], 'mysql': ['user', 'db', 'tables_priv'] } },
  clickhouse: { label: 'ClickHouse', badge: 'CH', q: ['`', '`'], foldsLower: false, rolesLabel: 'Users & roles', catalogLabel: 'System tables',
    system: { 'system': ['system.processes', 'system.query_log', 'system.tables', 'system.columns', 'system.parts'] } },
  couchbase: { label: 'Couchbase', badge: 'CB', q: ['`', '`'], foldsLower: false, rolesLabel: 'Users & roles', catalogLabel: 'System keyspaces',
    system: { 'system': ['system:keyspaces', 'system:indexes', 'system:datastores', 'system:dual'] } },
};
// Environment tags (PROD / STG) in the connection LIST and the omnibox
// suggestions. Off by the operator's decision: the fleet is prod-first, so the
// badge was on almost every row and carried no information there — the toolbar
// above the editor still shows it, which is where it matters (it is what you
// read just before pressing Run).
//
// A flag rather than deleted code: "keep it hidden in case we need it later"
// was the explicit ask, and a flag is the version of that which cannot rot.
const QH_SHOW_ENV_TAGS = false;

const QH_ENGINE_LOGO = { postgres: '/brand/engines/postgres.svg', mssql: '/brand/engines/mssql.svg', oracle: '/brand/engines/oracle.svg', mysql: '/brand/engines/mysql.svg', clickhouse: '/brand/engines/clickhouse.svg', couchbase: '/brand/engines/couchbase.svg' };
// `window.__resources` only exists in the standalone/offline export, where the
// bundler has inlined each logo and swapped the path for a blob URL (see the
// ext-resource-dependency metas in QueryHub.html).
// The paths above are root-absolute because the served app mounts its assets at
// the site root. The design prototype is opened from a subpath, so there the
// leading slash is dropped and the file resolves relative to the page.
function qhEngineLogo(conn) {
  let id = qhEngineId(conn && conn.engine);
  if (!QH_ENGINE_LOGO[id]) id = 'postgres';
  const r = window.__resources;
  if (r && r['engine_' + id]) return r['engine_' + id];
  const p = QH_ENGINE_LOGO[id];
  return (window.QH_MOCK && p.charAt(0) === '/') ? p.slice(1) : p;
}
function qhEngineId(engineStr) {
  const s = String(engineStr || '').toLowerCase();
  if (s.includes('sql server') || s.includes('mssql')) return 'mssql';
  if (s.includes('oracle')) return 'oracle';
  if (s.includes('mysql') || s.includes('maria')) return 'mysql';
  if (s.includes('clickhouse')) return 'clickhouse';
  if (s.includes('couchbase')) return 'couchbase';
  return 'postgres';
}
function qhEngine(conn) { return QH_ENGINES[qhEngineId(conn && conn.engine)] || QH_ENGINES.postgres; }
function qhEngineBadge(conn) { const v = (String(conn && conn.engine || '').match(/\d+/) || [''])[0]; return qhEngine(conn).badge + v; }
// ---------- Where a target lives: connection tags ----------
// One tag bag per CONNECTION — a server, or a managed instance. Its databases
// INHERIT it: a database has no location of its own, and a second copy of the
// value on every database is only a way for the two to disagree later.
// Three keys are reserved (`provider`, `service`, `account`); everything else is
// a free key:value pair a DBA-admin invents on the connection form. This map is
// what the UI can DRAW, not what may exist — the server owns the vocabulary, and
// an unknown provider degrades to plain text rather than to nothing.
const QH_PROVIDERS = {
  aws:    { label: 'AWS',          logo: '/brand/providers/aws.svg',    logoDark: '/brand/providers/aws-dark.svg', services: ['RDS', 'Aurora', 'EC2'] },
  huawei: { label: 'Huawei Cloud', logo: '/brand/providers/huawei.svg', services: ['RDS', 'ECS', 'GaussDB'] },
  onprem: { label: 'On-prem',      logo: '/brand/providers/onprem.svg', services: ['Bare metal', 'VM'] },
};
const QH_TAG_KEYS = [
  { key: 'provider', label: 'Cloud provider',    hint: 'Who runs the machine' },
  { key: 'service',  label: 'Service',           hint: 'A managed instance, or a VM someone patches' },
  { key: 'account',  label: 'Account / project', hint: 'Which cloud account it is billed to' },
];
const QH_TAG_RESERVED = QH_TAG_KEYS.map(t => t.key);
// Same blob-URL indirection as the engine logos — see qhEngineLogo. A provider
// whose mark is dark ink (the AWS wordmark is navy) ships a dark-theme variant:
// read live off <html data-theme> like qhBrand does, so a theme switch repaints
// it on the next render instead of leaving an invisible logo behind.
function qhProviderLogo(id) {
  const p = QH_PROVIDERS[id];
  if (!p) return '';
  const dark = typeof document !== 'undefined' && document.documentElement.getAttribute('data-theme') === 'dark';
  const key = dark && p.logoDark ? id + '_dark' : id;
  const path = dark && p.logoDark ? p.logoDark : p.logo;
  const r = window.__resources;
  if (r && r['prov_' + key]) return r['prov_' + key];
  return (window.QH_MOCK && path.charAt(0) === '/') ? path.slice(1) : path;
}
function qhTags(conn) { return (conn && conn.tags) || {}; }
function qhProvider(conn) { return QH_PROVIDERS[qhTags(conn).provider] || null; }
// 'AWS · RDS' — the short form that fits a table cell or a group header.
function qhHosting(conn) {
  const t = qhTags(conn), p = QH_PROVIDERS[t.provider];
  if (!p) return '';
  return p.label + (t.service ? ' · ' + t.service : '');
}
// Everything the tags say, in one string. The developer-side placement is the
// hover, so this has to be complete on its own — nothing else carries it.
function qhHostingFull(conn) {
  const t = qhTags(conn), parts = [];
  const h = qhHosting(conn);
  if (h) parts.push(h);
  if (t.account) parts.push('account ' + t.account);
  qhCustomTags(conn).forEach(([k, v]) => parts.push(k + ' ' + v));
  return parts.join(' · ');
}
function qhCustomTags(conn) {
  const t = qhTags(conn);
  return Object.keys(t).filter(k => QH_TAG_RESERVED.indexOf(k) < 0 && t[k] != null && t[k] !== '')
    .sort().map(k => [k, String(t[k])]);
}
// `provider:aws service:rds` in the sidebar search. Bare words stay a name
// search, so a token can only ever NARROW what the tree already shows.
function qhParseTagQuery(q) {
  const filters = [], words = [];
  String(q || '').trim().split(/\s+/).forEach(w => {
    const m = w.match(/^([a-z][a-z0-9_.-]*):(.*)$/i);
    if (m) filters.push({ k: m[1].toLowerCase(), v: m[2].toLowerCase() });
    else if (w) words.push(w);
  });
  return { text: words.join(' '), filters };
}
// A filter with no value (`provider:`) asks whether the key is set at all.
// Unknown keys fall back to the connection's own fields, so `env:staging` and
// `engine:sql` work without anyone having to tag them.
const QH_TAG_CONN_FIELDS = ['env', 'engine', 'host', 'name'];
function qhTagMatch(conn, filters) {
  if (!filters || !filters.length) return true;
  const t = qhTags(conn);
  return filters.every(f => {
    let val = t[f.k];
    if (f.k === 'provider') { const p = QH_PROVIDERS[t.provider]; val = p ? t.provider + ' ' + p.label : t.provider; }
    if ((val == null || val === '') && QH_TAG_CONN_FIELDS.indexOf(f.k) >= 0) val = conn && conn[f.k];
    if (val == null || val === '') return false;
    return f.v === '' || String(val).toLowerCase().includes(f.v);
  });
}
// The tag vocabulary the fleet actually carries — what the search box can
// complete. Derived from the connections in hand, so a key or value appears in
// the picker only while some target still has it.
function qhTagVocab(conns) {
  const values = {};
  const add = (k, v) => { if (v == null || v === '') return; (values[k] = values[k] || {})[String(v)] = 1; };
  (conns || []).forEach(c => {
    const t = qhTags(c);
    Object.keys(t).forEach(k => add(k, t[k]));
    QH_TAG_CONN_FIELDS.forEach(f => { if (f !== 'name') add(f, c[f]); });
  });
  const keys = QH_TAG_KEYS.map(t => t.key);
  Object.keys(values).sort().forEach(k => { if (keys.indexOf(k) < 0) keys.push(k); });
  const out = {};
  Object.keys(values).forEach(k => { out[k] = Object.keys(values[k]).sort(); });
  return { keys, values: out };
}
// Completions for the word being typed: keys while there is no colon, values
// after it. A filter syntax nobody can discover is a filter syntax nobody uses,
// and the place to say it is the moment someone types the first three letters.
function qhTokenSuggest(q, vocab) {
  const s = String(q || '');
  const cur = s.split(/\s+/).pop() || '';
  if (!cur) return [];
  const ci = cur.indexOf(':');
  const out = [];
  if (ci < 0) {
    const lc = cur.toLowerCase();
    (vocab.keys || []).forEach(k => {
      if (k.indexOf(lc) === 0 && k !== lc) out.push({ key: k, value: null, insert: k + ':', label: k + ':' });
    });
  } else {
    const k = cur.slice(0, ci).toLowerCase(), frag = cur.slice(ci + 1).toLowerCase();
    ((vocab.values || {})[k] || []).forEach(v => {
      if (v.toLowerCase().indexOf(frag) >= 0 && v.toLowerCase() !== frag) out.push({ key: k, value: v, insert: k + ':' + v + ' ', label: k + ':' + v });
    });
  }
  return out.slice(0, 6);
}
// Replace the word being typed with the completion, leaving the rest alone —
// `provider:aws use|` must not lose the token that is already there.
function qhApplyToken(q, insert) {
  const s = String(q || '');
  const m = s.match(/[^\s]*$/);
  return s.slice(0, s.length - (m ? m[0].length : 0)) + insert;
}

// One group per provider+service actually present in the fleet. Untagged
// targets get their own group rather than disappearing: an unlabelled server is
// a gap in the registry, and hiding it is how it stays one.
function qhProviderGroups(conns) {
  const map = new Map();
  (conns || []).forEach(c => {
    const t = qhTags(c);
    const pid = QH_PROVIDERS[t.provider] ? t.provider : '';
    const svc = pid ? (t.service || '') : '';
    const key = pid + '|' + svc;
    if (!map.has(key)) map.set(key, { key, providerId: pid, service: svc, label: pid ? qhHosting(c) : 'Untagged', conns: [] });
    map.get(key).conns.push(c);
  });
  return Array.from(map.values()).sort((a, b) => (a.providerId ? 0 : 1) - (b.providerId ? 0 : 1) || a.label.localeCompare(b.label));
}

function qhQuoteIdentFor(name, engineId) {
  const e = QH_ENGINES[engineId] || QH_ENGINES.postgres;
  const s = String(name);
  const plain = e.foldsLower ? /^[a-z_][a-z0-9_]*$/ : /^[A-Za-z_][A-Za-z0-9_]*$/;
  if (plain.test(s)) return s;
  const [o, c] = e.q;
  return o + s.split(c).join(c + c) + c;
}
// Server-level login roles / users — super-admin only. Real rows come from the
// engine catalog (pg_roles / sys.server_principals / DBA_USERS).
function qhServerRoles(connId) {
  return [
    { name: 'app_service', kind: 'role', login: true, sup: false, note: 'application' },
    { name: 'readonly', kind: 'group', login: false, sup: false, note: 'group' },
    { name: 'analyst_ro', kind: 'group', login: false, sup: false, note: 'group' },
    { name: 'dba.admin', kind: 'user', login: true, sup: true, note: 'DBA' },
    { name: 'dba.ops', kind: 'user', login: true, sup: true, note: 'DBA' },
    { name: 'dev.sample', kind: 'user', login: true, sup: false, note: 'developer' },
    { name: 'replication', kind: 'role', login: true, sup: false, note: 'system' },
  ];
}

// Deterministic approximate row count per table (mock — real value comes from
// the schema-catalog snapshot / pg_class.reltuples on the backend).
function qhApproxRows(name) {
  let h = 0; const s = String(name);
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  if (/audit_log|log|event|delivery|transaction|block/.test(s)) return 200000 + (h % 4200000);
  const buckets = [8, 42, 320, 2600, 18000, 140000, 920000];
  const base = buckets[h % buckets.length];
  return base + (h % Math.max(1, Math.floor(base * 0.5)));
}
function qhFmtRows(n) { return n >= 1e6 ? (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M' : n >= 1e3 ? (n / 1e3).toFixed(n >= 1e4 ? 0 : 1).replace(/\.0$/, '') + 'k' : String(n); }

// ---------- Risk hints (static safety analysis for pre-flight) ----------
function qhRiskHints(sql, cl) {
  const t = qhStripComments(sql || '').toLowerCase();
  const hints = [];
  if (!t.trim()) return hints;
  if (cl && cl.multi) hints.push({ level: 'high', text: cl.statements.length + ' statements in one request — split them' });
  if (/\b(update|delete)\b/.test(t) && !/\bwhere\b/.test(t)) hints.push({ level: 'high', text: 'UPDATE/DELETE without WHERE — affects every row' });
  if (/where\s+1\s*=\s*1|\bor\s+1\s*=\s*1|\bor\s+true\b/.test(t)) hints.push({ level: 'high', text: 'Always-true WHERE (1=1 / OR TRUE)' });
  if (/\b(drop|truncate)\b/.test(t)) hints.push({ level: 'high', text: 'Destructive DDL (DROP / TRUNCATE)' });
  if (/select\s+\*/.test(t)) hints.push({ level: 'med', text: 'SELECT * returns all columns — may include PII' });
  if (/\bselect\b/.test(t) && !/\blimit\b/.test(t) && (!cl || cl.tier === 'RO')) hints.push({ level: 'med', text: 'No LIMIT — result set may be large' });
  if (/\blike\s+'%/.test(t)) hints.push({ level: 'low', text: 'Leading-wildcard LIKE — cannot use an index' });
  if (!hints.length) hints.push({ level: 'low', text: 'No obvious risks detected' });
  return hints;
}

// ---------- Mock EXPLAIN plan ----------
function qhExplainPlan(sql, cl) {
  const t = qhStripComments(sql || '').toLowerCase();
  const table = (t.match(/from\s+([a-z0-9_.]+)/) || [])[1] || (t.match(/(?:update|into|table)\s+([a-z0-9_.]+)/) || [])[1] || 'table';
  const indexed = /\bwhere\b/.test(t);
  const scan = indexed ? 'Index Scan' : 'Seq Scan';
  const rows = indexed ? (1 + Math.floor(Math.random() * 400)) : (5000 + Math.floor(Math.random() * 90000));
  const cost = (rows * 0.011).toFixed(2);
  const nodes = [];
  if (/\blimit\b/.test(t)) nodes.push({ d: 0, op: 'Limit', detail: '(cost=0.00..' + (cost / 4).toFixed(2) + ')' });
  const base = nodes.length;
  if (/\bjoin\b/.test(t)) {
    nodes.push({ d: base, op: 'Hash Join', detail: '(cost=0.00..' + (rows * 0.02).toFixed(2) + ' rows=' + rows + ')' });
    nodes.push({ d: base + 1, op: scan, detail: 'on ' + table + ' (rows=' + rows + ')', warn: scan === 'Seq Scan' });
    nodes.push({ d: base + 1, op: 'Hash', detail: 'buckets=1024 batches=1' });
    nodes.push({ d: base + 2, op: 'Seq Scan', detail: 'on joined relation' });
  } else {
    if (/order\s+by/.test(t)) { nodes.push({ d: base, op: 'Sort', detail: 'sort method: quicksort' }); }
    nodes.push({ d: base + (/order\s+by/.test(t) ? 1 : 0), op: scan, detail: 'on ' + table + ' (cost=0.00..' + cost + ' rows=' + rows + ')', warn: scan === 'Seq Scan' });
  }
  return { nodes, planningMs: (Math.random() * 2 + 0.1).toFixed(2), rows, scan, table };
}

// ---------- Mock result builder ----------
function qhMaskValue(col, raw) {
  const c = QH_PII_CATALOG[col];
  if (!c) return raw;
  if (c.mask === 'full') return '••••••••';
  const s = String(raw);
  if (col === 'email') { const [a,b] = s.split('@'); return (a||'').slice(0,2) + '•••@' + (b||''); }
  return s.slice(0, 2) + '••••' + s.slice(-2);
}

// ---------- Result generation (paged, DataGrip-style) ----------
// Results can be huge (millions of rows). We never materialize the whole set:
// the result exposes total + a deterministic slice(offset, limit) so the UI
// pages through on demand. A real backend would stream/cursor these pages.
function qhLcg(seed) {
  let s = (seed * 2654435761) % 2147483647;
  if (s <= 0) s += 2147483646;
  return () => { s = (s * 16807) % 2147483647; return (s - 1) / 2147483646; };
}
const QH_COLGEN = {
  id: (r) => 80000 + Math.floor(r() * 900000),
  user_id: (r) => 80000 + Math.floor(r() * 900000),
  order_id: (r) => 500000 + Math.floor(r() * 900000),
  email: (r) => ['ayse','mehmet','can','elif','deniz','burak','zeynep','emre','sena','kaan'][Math.floor(r()*10)] + Math.floor(r()*9999) + '@example.com',
  amount: (r) => (r() * 5000).toFixed(2),
  balance: (r) => (r() * 250000).toFixed(2),
  status: (r) => ['active','pending','failed','done','cancelled'][Math.floor(r()*5)],
  created_at: (r) => '2026-0' + (1+Math.floor(r()*6)) + '-' + String(1+Math.floor(r()*28)).padStart(2,'0') + ' ' + String(Math.floor(r()*24)).padStart(2,'0') + ':' + String(Math.floor(r()*60)).padStart(2,'0'),
  last_seen_at: (r) => '2026-06-' + String(1+Math.floor(r()*28)).padStart(2,'0') + ' ' + String(Math.floor(r()*24)).padStart(2,'0') + ':' + String(Math.floor(r()*60)).padStart(2,'0'),
  day: (r, i) => '2026-05-' + String(1 + (i % 28)).padStart(2,'0'),
  signups: (r) => 200 + Math.floor(r()*400),
  verified: (r) => 100 + Math.floor(r()*300),
  first_trade: (r) => 40 + Math.floor(r()*120),
  tckn: () => '12345678901',
  iban: () => 'TR330006100519786457841326',
  phone: () => '+90 532 000 00 00',
  full_name: (r) => ['Jamie Lee','Chris Park','Alex Kim','Jordan Ray','Robin Fox','Sam Carter'][Math.floor(r()*6)],
  count: (r, i, tot) => tot || 0,
};
function qhColsFromSql(clean) {
  let cols = [];
  const sel = clean.match(/select\s+([\s\S]*?)\s+from/);
  if (sel && !sel[1].includes('*')) {
    cols = sel[1].split(',').map(c => c.trim().split(/\s+as\s+|\s+/).pop().split('.').pop().replace(/[()*]/g, '')).filter(Boolean).slice(0, 12);
  }
  if (cols.length === 0) cols = ['id', 'user_id', 'email', 'full_name', 'amount', 'status', 'created_at', 'last_seen_at'];
  return cols;
}
function qhResultTotal(clean) {
  const agg = /\b(count|sum|avg|min|max)\s*\(/.test(clean);
  const grouped = /group\s+by/.test(clean);
  if (agg && !grouped) return 1;
  const lim = clean.match(/\blimit\s+(\d+)/);
  if (lim) return parseInt(lim[1], 10);
  if (grouped) return 8 + Math.floor(Math.random() * 40);
  // unbounded scan → large, realistic row count
  return 120000 + Math.floor(Math.random() * 4800000);
}
function qhSliceRows(cols, offset, limit, total) {
  const rows = [];
  const end = Math.min(offset + limit, total);
  for (let i = offset; i < end; i++) {
    const row = {};
    cols.forEach((c, ci) => {
      const gen = QH_COLGEN[c] || (() => '—');
      row[c] = gen(qhLcg((i + 1) * 131 + ci * 7 + 1), i, total);
    });
    rows.push(row);
  }
  return rows;
}
function qhMockResult(sql, classify) {
  const clean = qhStripComments(sql).toLowerCase();
  if (classify.tier !== 'RO') {
    const n = (clean.includes('where')) ? (3 + Math.floor(Math.random()*40)) : 0;
    return { kind: 'affected', affected: n,
      message: classify.tier === 'DDL' ? 'Statement executed.' : `${n} row(s) affected.` };
  }
  const cols = qhColsFromSql(clean);
  const total = qhResultTotal(clean);
  const res = { kind: 'table', cols, total };
  res.slice = (offset, limit) => qhSliceRows(cols, offset, limit, total);
  return res;
}

// ---------- Engine-aware helpers (multi-engine; added v4) ----------
// Whether RO queries auto-approve on this target. Server sends `autoApproveRO`
// per connection AND per database (db overrides conn); read it, never hardcode.
function qhAutoApproveRO(conn, db) {
  if (db && db.autoApproveRO != null) return !!db.autoApproveRO;
  return !!(conn && conn.autoApproveRO);
}
// Default schema/namespace per engine (SSMS dbo.Table, Postgres public.table, …).
function qhSchemaFor(conn, db) {
  if (db && db.schema) return db.schema;
  const eng = qhEngineId(conn && conn.engine);
  if (eng === 'mssql') return 'dbo';
  if (eng === 'oracle') return (db && db.name) || 'APP';
  if (eng === 'mysql' || eng === 'clickhouse') return (db && db.name) || 'default';
  if (eng === 'couchbase') return '_default';
  return 'public';
}
// The REAL schema of one table, from the catalog (`tableRefs`). qhSchemaFor
// above is only a per-engine guess; on this fleet it is wrong more often than
// right (most tables are not in public/dbo), which meant generated SQL pointed
// at a schema the table isn't in. Fall back to the guess only when the catalog
// has nothing to say — an unknown table, or an older payload.
function qhSchemaOf(conn, db, name) {
  const refs = db && db.tableRefs;
  if (refs) { for (let i = 0; i < refs.length; i++) if (refs[i].n === name) return refs[i].s; }
  return qhSchemaFor(conn, db);
}
function qhQualify(conn, db, name, schema) {
  const eng = qhEngineId(conn && conn.engine);
  const s = schema || qhSchemaOf(conn, db, name);
  return qhQuoteIdentFor(s, eng) + '.' + qhQuoteIdentFor(name, eng);
}
// Engine-aware SELECT builder for the tree context menu ("Select top N rows").
function qhSelectSql(conn, db, name, opts) {
  const o = opts || {};
  const ref = qhQualify(conn, db, name);
  if (o.mode === 'count') return 'SELECT COUNT(*)\nFROM ' + ref + ';';
  if (o.mode === 'all') return 'SELECT *\nFROM ' + ref + ';';
  const n = o.limit || 100;
  const eng = qhEngineId(conn && conn.engine);
  if (eng === 'mssql') return 'SELECT TOP ' + n + ' *\nFROM ' + ref + ';';
  if (eng === 'oracle') return 'SELECT *\nFROM ' + ref + '\nFETCH FIRST ' + n + ' ROWS ONLY;';
  return 'SELECT *\nFROM ' + ref + '\nLIMIT ' + n + ';';
}

// Build stamp — TR time (UTC+3), yyyy-MM-dd HH:mm.
// PROTOTYPE ONLY: hardcoded, so it never changes on its own. In production this
// must come from the server/build (see DESIGN_TO_CODE_BRIEF §6) — inject at
// build time or return it on GET /api/me — not a client constant.
const QH_VERSION = '2026-07-18 19:40';
// Build metadata — overridden at runtime from window.__QH_BUILD__ (injected by
// the FastAPI `/` route from git HEAD; see qh-version.js). This constant is the
// fallback for the raw prototype served without the backend.
const QH_BUILD = { version: QH_VERSION, date: QH_VERSION, sha: '', branch: '', repo: '' };
// GitHub commit URL for a short SHA — null when no repo slug is configured
// (web_repo_slug), in which case the What's-new page shows the SHA un-linked.
function qhCommitUrl(sha) {
  const b = (typeof window !== 'undefined' && window.QH_BUILD) || QH_BUILD;
  return (b && b.repo && sha) ? ('https://github.com/' + b.repo + '/commit/' + sha) : null;
}

// ---------- Notifications (fallback seed; prod = GET /notifications + POST /notifications/read) ----------
// Developer-facing feed: approval decisions, scheduled runs, endpoint grants, fleet events.
// The real bell replaces this list from the API; this seed only renders in the
// raw prototype served without the backend.
const QH_NOTIFICATIONS = [];

// ---------- Clipboard ----------
// One place that actually knows whether the copy worked.
//
// `navigator.clipboard.writeText` returns a PROMISE: wrapping it in a
// synchronous try/catch (as every call site used to) catches nothing, so a
// rejected write — document not focused, non-secure context, permission
// denied, Safari's user-gesture rule — surfaced as a cheerful "Copied!" toast
// while the clipboard still held the previous value. That is the "sometimes it
// doesn't copy" report.
//
// Awaits the real result, and falls back to the legacy execCommand path (which
// works in contexts where the async API refuses) before admitting failure.
// Resolves true only when something actually reached the clipboard.
function qhCopyText(text) {
  const s = String(text == null ? '' : text);
  const legacy = () => {
    try {
      const ta = document.createElement('textarea');
      ta.value = s;
      // Keep it off-screen but focusable; readOnly stops mobile keyboards.
      ta.setAttribute('readonly', '');
      ta.style.cssText = 'position:fixed;top:0;left:-9999px;opacity:0';
      document.body.appendChild(ta);
      const sel = document.getSelection();
      const prev = sel && sel.rangeCount ? sel.getRangeAt(0) : null;
      // Selecting the scratch textarea also FOCUSES it, so the element the
      // user was working in loses focus and its caret — copying a line in the
      // SQL editor made the caret vanish and dropped you out of the field.
      // Remember where focus was, and where the caret sat inside it.
      const from = document.activeElement;
      const fromField = from && typeof from.selectionStart === 'number';
      const at = fromField ? [from.selectionStart, from.selectionEnd] : null;
      ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      // ALWAYS clear first. Selecting the scratch textarea leaves a document
      // selection behind; if there was nothing to restore we used to leave it
      // in place, and the grid's copy handlers bail when
      // `window.getSelection()` is non-empty (a real text selection must win).
      // So the first copy poisoned every copy after it — copy once, then
      // nothing.
      if (sel) { sel.removeAllRanges(); if (prev) sel.addRange(prev); }
      if (from && from.isConnected && from.focus) {
        from.focus({ preventScroll: true });
        if (at && from.setSelectionRange) from.setSelectionRange(at[0], at[1]);
      }
      return ok;
    } catch (e) { return false; }
  };
  // Order matters. The SYNCHRONOUS execCommand path runs first because it is
  // bound to the user gesture that is still on the stack (the keydown / click
  // that asked for the copy). The async clipboard API is rejected precisely in
  // those cases — unfocused document, no user activation, insecure origin —
  // and awaiting it also means the caller has already left the gesture, so a
  // retry there is refused too. Async stays as the fallback for callers that
  // legitimately copy outside a gesture.
  if (legacy()) return Promise.resolve(true);
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(s).then(() => true, () => false);
    }
  } catch (e) { /* fall through */ }
  return Promise.resolve(false);
}

// ---- what Run should execute ------------------------------------------
// One rule, shared by the toolbar button, F5, ⌘↵ and F8, so "what will Run
// do?" has a single answer no matter how it is pressed.
//
//   selection with SQL in it -> that selection, nothing else
//   selection that is ONLY comments -> nothing, and say so
//   no selection -> the whole tab
//
// The middle case is the reason this is a function rather than an `if`. Falling
// back to the whole tab when a selection has no statement in it is the
// dangerous answer: someone highlights one commented-out line of a long script
// and the entire script runs. "Nothing selected" and "a selection that means
// nothing" are different intents and must not collapse into each other.
function qhRunTarget(selText, allText) {
  const sel = String(selText == null ? '' : selText).trim();
  if (sel) {
    return qhStripComments(sel).trim() ? { kind: 'selection', sql: sel }
                                       : { kind: 'comments' };
  }
  const all = String(allText == null ? '' : allText).trim();
  return all ? { kind: 'all', sql: all } : { kind: 'empty' };
}

// ---- long target names ------------------------------------------------
// Real fleets give every host the same head: `svc-prod-orders`,
// `svc-prod-billing`, `svc-prod-catalog`. In a list where every row carries it,
// that head is the part of the name that says nothing — and it costs 9 of the
// ~25 characters a 264px sidebar can hold, which is how a database row ended
// up showing its server and losing its own name.
// qhFleetPrefixes returns the heads worth folding (delimiter-aligned, carried
// by at least three targets), longest first; qhSplitName cuts a name into
// [head, tail] so the UI can dim the head and drop it first. Nothing is lost:
// the full name stays in the row's hover title and comes back when the
// sidebar is wide enough to hold it.
const QH_FOLD_MIN = 3;
function qhSplitName(name, prefixes) {
  const n = String(name || '');
  const p = (prefixes || []).find(x => n.length > x.length && n.slice(0, x.length) === x);
  return p ? [p, n.slice(p.length)] : ['', n];
}
function qhFleetPrefixes(conns) {
  const list = (conns || []).map(c => String(c.name || ''));
  const count = {};
  list.forEach(n => { for (let i = 0; i < n.length - 1; i++) { if (n[i] === '-' || n[i] === '_' || n[i] === '.') { const p = n.slice(0, i + 1); count[p] = (count[p] || 0) + 1; } } });
  let pres = Object.keys(count).filter(p => count[p] >= QH_FOLD_MIN && p.length >= 4).sort((a, b) => b.length - a.length);
  // Folding must never make two targets read alike: `svc-prod-orders` and
  // `svc-stg-orders` would both come out as `orders`. A head whose tail another
  // target already owns is not foldable.
  for (let pass = 0; pass < 3 && pres.length; pass++) {
    const tails = {}, bad = {};
    list.forEach(n => { const sp = qhSplitName(n, pres); (tails[sp[1]] = tails[sp[1]] || []).push(sp[0]); });
    Object.keys(tails).forEach(t => { if (tails[t].length > 1) tails[t].forEach(p => { if (p) bad[p] = 1; }); });
    if (!Object.keys(bad).length) break;
    pres = pres.filter(p => !bad[p]);
  }
  return pres;
}

// ---------- POST /queries 409 — a question, or a duplicate? ----------
// Destructive SQL (DROP, TRUNCATE, ALTER … DROP COLUMN/CONSTRAINT, an UPDATE or
// DELETE with no WHERE) is not refused for a super-admin: it comes back as 409
// carrying one or more human-readable reasons, and the SAME request re-sent with
// `confirmed: true` runs. The trouble is that 409 also means "duplicate request"
// on this API, and the two must never be confused — answering a duplicate with a
// confirm dialog re-sends it and duplicates it again.
//
// So the test is POSITIVE, in this order:
//   1. `code: 'confirmation_required'` — the distinct code asked for in the brief.
//      The day it exists, the text matching below is dead and can be deleted.
//   2. a `reasons` array on the error — only the confirmation carries one.
//   3. code `conflict` (or none) with a message that does not read as a
//      duplicate. Fragile, which is why it is last and why it is one regex.
// Returns the reasons to SHOW, or null when this 409 is not the question. The
// reasons are never shortened or reworded: they name the specific consequence,
// and that sentence is the whole point of asking.
const QH_DUP_409 = /\b(duplicate|already (submitted|queued|pending|running|in flight))\b/i;
function qhSplitReasons(text) {
  return String(text == null ? '' : text).split(/\r?\n+/).map(s => s.trim()).filter(Boolean);
}
function qhConfirmReasons(err) {
  if (!err || err.status !== 409) return null;
  const code = err.code || '';
  const list = Array.isArray(err.reasons)
    ? err.reasons.map(r => String(r == null ? '' : r).trim()).filter(Boolean) : null;
  if (code === 'confirmation_required') return (list && list.length) ? list : qhSplitReasons(err.message);
  // The server sends the distinct code now, so `conflict` is DEFINITIVELY not
  // the question — it is duplicate or rate-limit. Step 3 of the ladder above
  // used to fall through to text matching here, and that was not merely
  // fragile: the real duplicate wording ("You already have an active request
  // (#5, status=pending)") matches none of QH_DUP_409's alternations, so a
  // duplicate WAS being shown as a confirm dialog whose confirmation the
  // server then refused again. Measured 2026-08-14 against the live backend.
  if (code) return (code === 'conflict') ? null : ((list && list.length) ? list : null);
  // No code at all: an older server, or a transport that lost it. Keep the
  // last-resort text read for that case only, still guarded by the regex.
  if (list && list.length) return list;
  const msg = String(err.message == null ? '' : err.message).trim();
  if (!msg || QH_DUP_409.test(msg)) return null;
  return qhSplitReasons(msg);
}

Object.assign(window, {
  qhCopyText, qhFleetPrefixes, qhSplitName, qhRunTarget, qhConfirmReasons, qhSplitReasons,
  QH_VERSION, QH_BUILD, qhCommitUrl, QH_NOTIFICATIONS,
  QH_CONNECTIONS, QH_SAVED, QH_HISTORY, QH_PII_CATALOG,
  qhClassify, qhDetectPII, qhMockResult, qhMaskValue, qhStripComments,
  qhColumnsFor, qhIndexesFor, qhViewsFor,
  qhRiskHints, qhExplainPlan, qhQuoteIdent, qhQuoteList, qhApproxRows, qhFmtRows,
  QH_SHOW_ENV_TAGS,
  QH_ENGINES, qhEngineId, qhEngine, qhEngineBadge, qhEngineLogo, qhQuoteIdentFor, qhServerRoles,
  QH_PROVIDERS, QH_TAG_KEYS, QH_TAG_RESERVED, qhProviderLogo, qhProvider, qhTags, qhHosting, qhHostingFull,
  qhCustomTags, qhParseTagQuery, qhTagMatch, qhProviderGroups, qhTagVocab, qhTokenSuggest, qhApplyToken,
  qhAutoApproveRO, qhSchemaFor, qhSchemaOf, qhQualify, qhSelectSql,
});
