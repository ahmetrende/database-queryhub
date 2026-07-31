// QueryHub — main app: top bar, submit→approval simulation, theme, tweaks.

const { useState, useEffect, useRef, useCallback } = React;

// Keyboard-shortcut labels (mac-aware) surfaced as hover tooltips on action buttons.
const QH_MAC = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
const QH_KBD = { run: QH_MAC ? '⌘ ↵  ·  F5' : 'Ctrl ↵  ·  F5', newq: QH_MAC ? '⌘ ⌥ N' : 'Ctrl Alt N' };
window.QH_KBD = QH_KBD;

function nowTime() {
  const d = new Date();
  const p = (n, l = 2) => String(n).padStart(l, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' +
    p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds()) + '.' + p(d.getMilliseconds(), 3);
}

let TAB_SEQ = 3;
function freshTab(over) {
  return { id: 't' + (TAB_SEQ++), name: 'Untitled query', sql: '', conn: 'prod-replica', db: 'users_ro',
    dirty: false, result: null, status: null, runMs: null, messages: [], audit: [], touchedAt: Date.now(), ...over };
}
function welcomeTab() {
  return { id: 'welcome', kind: 'welcome', name: 'Welcome', sql: '', conn: 'prod-replica', db: 'users_ro',
    dirty: false, result: null, status: null, runMs: null, messages: [], audit: [], touchedAt: Date.now() };
}
function whatsNewTab() {
  return { id: 'whatsnew', kind: 'whatsnew', name: "What's new", sql: '', conn: 'prod-replica', db: 'users_ro',
    dirty: false, result: null, status: null, runMs: null, messages: [], audit: [], touchedAt: Date.now() };
}

// ----- Workspace persistence (resume where you left off) -----
// Open tabs are saved locally; the backend mirrors this per-user and a
// retention job purges tabs/sessions untouched for 30 days (see handoff).
const QH_WS_KEY = 'qh.workspace.v1';
const QH_WS_TTL = 30 * 24 * 3600 * 1000;
function qhLoadWorkspace() {
  try {
    const w = JSON.parse(localStorage.getItem(QH_WS_KEY) || 'null');
    if (!w || !Array.isArray(w.tabs)) return null;
    const now = Date.now();
    const fresh = w.tabs.filter(t => !t.touchedAt || (now - t.touchedAt) < QH_WS_TTL);
    if (!fresh.length) return null;
    let maxN = 0;
    const tabs = fresh.map(t => { const n = parseInt(String(t.id).replace(/\D/g, '')) || 0; if (n > maxN) maxN = n; return { id: t.id, kind: t.kind, name: t.name, sql: t.sql, conn: t.conn, db: t.db, reqId: t.reqId, qid: t.qid, touchedAt: t.touchedAt || now, dirty: false, result: null, status: null, runMs: null, messages: [], audit: [] }; });
    TAB_SEQ = Math.max(TAB_SEQ, maxN + 1);
    return { tabs, activeId: fresh.some(t => t.id === w.activeId) ? w.activeId : tabs[0].id };
  } catch (e) { return null; }
}

// Saved queries library — mirrored to the backend per-user; falls back to local seed.
const QH_SAVED_KEY = 'qh.saved.v1';
function qhLoadSaved() {
  try { const s = JSON.parse(localStorage.getItem(QH_SAVED_KEY) || 'null'); if (Array.isArray(s)) return s; } catch (e) {}
  return [];
}

// Named sessions — a whole workspace (all open tabs) saved under one name, server or local.
const QH_SESSIONS_KEY = 'qh.sessions.v1';
function qhLoadSessions() {
  try { const s = JSON.parse(localStorage.getItem(QH_SESSIONS_KEY) || 'null'); if (Array.isArray(s)) return s; } catch (e) {}
  return [];
}

// Scheduled queries (client-persisted); a real backend runs them on a cron.
const QH_SCHEDULED_KEY = 'qh.scheduled.v1';
function qhLoadScheduled() {
  try { const s = JSON.parse(localStorage.getItem(QH_SCHEDULED_KEY) || 'null'); if (Array.isArray(s)) return s; } catch (e) {}
  return [];
}
// Reopen-closed stack, persisted so it survives a reload.
const QH_CLOSED_KEY = 'qh.closed.v1';
function qhLoadClosed() {
  try { const s = JSON.parse(localStorage.getItem(QH_CLOSED_KEY) || 'null'); if (Array.isArray(s)) return s; } catch (e) {}
  return [];
}

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "theme": "system",
  "brand": "warm",
  "editorFont": 14,
  "sidebarSide": "left",
  "hideSidebar": false
}/*EDITMODE-END*/;

const QH_SESSION_KEY = 'qh.session.v1';
// Last changelog build the user has seen (its sha) — drives the "new" dot on
// the avatar / profile menu. Compared against QH_BUILD.sha (current deploy).
const QH_NEWS_KEY = 'qh.news.v1';
// Connection organizer (favorites / folders). Since the Databases view it files
// individual DATABASES, not just servers — which server exists is common
// knowledge to anyone with a grant, but which databases a person works in is
// theirs. Cleared on sign-out; see the note there.
const QH_CONNORG_KEY = 'qh.connorg.v1';
function qhLatestNewsSha() { return (typeof window !== 'undefined' && window.QH_BUILD && window.QH_BUILD.sha) || ''; }

// Ask the server for the authoritative tier of what's in the editor.
//
// The browser's qhClassify is a keyword guess; the server runs the real
// query_safety analysis for this target's engine, resolves the user's grant on
// this database, and checks the auto-approve windows. Those three inputs are
// exactly what decides Run-vs-Submit, so the UI has to render the server's
// answer, not its own.
//
// Debounced so it fires when typing settles rather than per keystroke, and
// stale responses are discarded by comparing the request key on arrival.
// Returns null until the first answer for the current (sql, conn, db) —
// callers fall back to the local hint until then.
const QH_CLASSIFY_DEBOUNCE_MS = 400;

function useServerClassify(sql, connId, dbId) {
  const [verdict, setVerdict] = React.useState(null);
  const keyRef = React.useRef('');
  const key = connId + '|' + dbId + '|' + sql;

  React.useEffect(() => {
    const api = window.qhApi;
    const trimmed = (sql || '').trim();
    // No API (offline prototype), nothing typed, or no target picked: there is
    // nothing authoritative to ask for.
    if (!api || !api.classify || !trimmed || !connId || !dbId) {
      setVerdict(null);
      keyRef.current = '';
      return undefined;
    }
    keyRef.current = key;
    const timer = setTimeout(() => {
      api.classify({ connectionId: connId, databaseId: dbId, sql: trimmed })
        .then(r => { if (keyRef.current === key) setVerdict(r); })
        // A failed classify must not break typing or block submission — the
        // local hint stays in charge and the server still has the final say at
        // submit time.
        .catch(() => { if (keyRef.current === key) setVerdict(null); });
    }, QH_CLASSIFY_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [key]);

  return verdict;
}

function App() {
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);

  // Auth is real: the session lives in an httpOnly cookie and the user is
  // loaded from /api/me at boot (effect below). Nothing is persisted in JS.
  const [user, setUser] = useState(null);
  const [authChecked, setAuthChecked] = useState(false);
  const isSuper = !!(user && user.role === 'super');   // UI hint only; the server enforces access
  const signIn = (u) => setUser(u);   // legacy prop; real sign-in is a redirect
  const signOut = () => {
    qhApi.signout().catch(() => {});
    // Storage isolation: the per-user cache/UI-state keys are
    // browser-global, so on a shared machine the next person would see the
    // previous user's saved queries, sessions, scheduled list and open tabs.
    // Clear them on sign-out.
    // Deliberately NOT cleared: qh.treeview.v1 and qh.sidewidth.v1 — chrome
    // preferences, not user data, and resetting them on every sign-out would
    // just be a papercut on a shared machine.
    try {
      [QH_WS_KEY, QH_SAVED_KEY, QH_SESSIONS_KEY, QH_SCHEDULED_KEY,
       QH_CLOSED_KEY, QH_SESSION_KEY, QH_NEWS_KEY, QH_CONNORG_KEY].forEach(
        (k) => localStorage.removeItem(k));
    } catch (e) { /* private mode / storage disabled — nothing to clear */ }
    setUser(null);
  };

  const wsInit = useRef();
  if (!wsInit.current) {
    const loaded = qhLoadWorkspace();
    if (loaded) {
      const tabs = loaded.tabs.some(x => x.kind === 'welcome') ? loaded.tabs : [welcomeTab(), ...loaded.tabs];
      wsInit.current = { tabs, activeId: loaded.activeId };
    } else {
      wsInit.current = {
        tabs: [
          welcomeTab(),
          { id: 't1', name: 'Query 1', sql: '', conn: null, db: null, dirty: false, result: null, status: null, runMs: null, messages: [], audit: [], touchedAt: Date.now() },
        ], activeId: 'welcome',
      };
    }
  }
  const [tabs, setTabs] = useState(wsInit.current.tabs);
  const [activeId, setActiveId] = useState(wsInit.current.activeId);
  const [dlModal, setDlModal] = useState(null);   // .sql download modal: { id, name }
  const [sideMode, setSideMode] = useState('conns');
  const [resTab, setResTab] = useState('results');
  const [resH, setResH] = useState(280);
  const [schedOpen, setSchedOpen] = useState(false);
  const [reqOpen, setReqOpen] = useState(false);
  const [batchOpen, setBatchOpen] = useState(false);
  const [closePrompt, setClosePrompt] = useState(null);
  const [saveDest, setSaveDest] = useState('server');
  const [savedList, setSavedList] = useState(qhLoadSaved);
  const [sessions, setSessions] = useState(qhLoadSessions);
  const [sessionModal, setSessionModal] = useState(false);
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [pwOpen, setPwOpen] = useState(false);   // voluntary change-password overlay (local accounts)
  const closedStack = useRef(qhLoadClosed());
  const newTabRef = useRef(null);   // ⌘⌥N handler lives in a []-dep effect; keep the latest newTab here
  const [scheduled, setScheduled] = useState(qhLoadScheduled);
  const [lastSync, setLastSync] = useState(null);
  const mounted = useRef(false);
  const [toast, setToast] = useState(null);
  const [view, setView] = useState(() => (typeof location !== 'undefined' && (location.hash || '').indexOf('#admin') === 0) ? 'admin' : 'dev'); // dev | admin (hash-synced)
  const [adminRole, setAdminRole] = useState('dba'); // dba | super
  // The admin-panel "Viewing as" role follows the caller's REAL role: a
  // super-admin defaults to super (and may simulate dba); a scoped "dba" admin
  // is pinned to dba. The server enforces scope regardless of this toggle.
  useEffect(() => { setAdminRole(user && user.role === 'super' ? 'super' : 'dba'); }, [user && user.role]);
  const pushToast = (msg) => { setToast(msg); setTimeout(() => setToast(null), 4200); };
  const admin = useAdminState(pushToast, view === 'admin',
                              !!(user && (user.role === 'dba' || user.role === 'super')));

  // Deep-link the top-level view: #admin/<section> ↔ admin panel (AdminPanel
  // owns the <section> suffix). Entering the developer view clears the hash.
  // window.history — `history` is a state var in this component (would shadow).
  useEffect(() => {
    const onHash = () => setView((location.hash || '').indexOf('#admin') === 0 ? 'admin' : 'dev');
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  useEffect(() => {
    if (view === 'dev' && (location.hash || '').indexOf('#admin') === 0) window.history.replaceState(null, '', location.pathname + location.search);
  }, [view]);

  // Real developer data (loaded from the API once signed in; empty until then).
  const [conns, setConns] = useState([]);
  const [devLoadError, setDevLoadError] = useState(false);
  const [history, setHistory] = useState([]);
  const [schemaCache, setSchemaCache] = useState({});
  const schemaReq = useRef({});
  // A successful fetch used to be permanent: the cache had no TTL and was
  // only ever reset by a failure, so a tab left open all day kept serving the
  // schema as it looked at load — new tables never appeared, and an admin's
  // "refresh schema" changed nothing on screen. Re-fetch when the entry is
  // older than this, and let callers force it.
  const SCHEMA_TTL_MS = 5 * 60 * 1000;
  const loadSchema = useCallback((connId, dbId, force) => {
    if (!connId || !dbId) return;
    const key = connId + '/' + dbId;
    const at = schemaReq.current[key];
    if (!force && at && (at === true || Date.now() - at < SCHEMA_TTL_MS)) return;
    schemaReq.current[key] = true;
    qhApi.schema(connId, dbId).then(r => {
      // Key by "schema.table" AND (first one wins) by the bare name. Keying
      // by name alone let two same-named tables in different schemas
      // overwrite each other — one of them silently showed the other's
      // columns. The bare key stays for lookups that have no schema in hand.
      const tables = {};
      const addTbl = (t) => {
        const entry = { schema: t.schema, columns: t.columns || [],
                        indexes: t.indexes || [], approxRows: t.approxRows };
        if (t.schema) tables[t.schema + '.' + t.name] = entry;
        if (!tables[t.name]) tables[t.name] = entry;
      };
      (r.tables || []).forEach(addTbl);
      (r.views || []).forEach(addTbl);   // views carry columns now too
      schemaReq.current[key] = Date.now();   // stamp: lets the TTL expire it
      setSchemaCache(prev => ({ ...prev, [key]: {
        tables, views: (r.views || []).map(v => v.name) } }));
    }).catch(() => { schemaReq.current[key] = false; });
  }, []);

  // Server roles (super-only): lazily loaded when the Roles branch opens,
  // then cached. Mirrors the schema cache; the server enforces super access,
  // so a non-super caller simply gets a 403 and an empty branch.
  const [rolesCache, setRolesCache] = useState({});
  const rolesReq = useRef({});
  const loadRoles = useCallback((connId) => {
    if (!connId || rolesReq.current[connId]) return;
    rolesReq.current[connId] = true;
    qhApi.roles(connId)
      .then(r => setRolesCache(prev => ({ ...prev, [connId]: r.roles || [] })))
      .catch(() => { rolesReq.current[connId] = false; });
  }, []);

  const tab = tabs.find(x => x.id === activeId) || tabs[0];
  const patch = (id, p) => setTabs(ts => ts.map(x => x.id === id ? { ...x, ...p } : x));
  const selectTab = (id) => { setActiveId(id); setTabs(ts => ts.map(x => x.id === id ? { ...x, touchedAt: Date.now() } : x)); };

  // persist workspace (slim) whenever tabs or selection change
  useEffect(() => {
    try { localStorage.setItem(QH_SAVED_KEY, JSON.stringify(savedList)); } catch (e) {}
    if (mounted.current && savedList.some(x => x.dest === 'server')) setLastSync(Date.now());
  }, [savedList]);
  useEffect(() => {
    try { localStorage.setItem(QH_SESSIONS_KEY, JSON.stringify(sessions)); } catch (e) {}
    if (mounted.current && sessions.some(x => x.dest === 'server')) setLastSync(Date.now());
  }, [sessions]);
  useEffect(() => {
    try { localStorage.setItem(QH_SCHEDULED_KEY, JSON.stringify(scheduled)); } catch (e) {}
  }, [scheduled]);
  useEffect(() => { mounted.current = true; }, []);
  // Reopen last closed tab — Ctrl/Cmd + Shift + T
  useEffect(() => {
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.altKey && (e.code === 'KeyN' || e.key === 'n' || e.key === 'N')) { e.preventDefault(); if (newTabRef.current) newTabRef.current(); return; }
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && (e.key === 'T' || e.key === 't')) {
        e.preventDefault();
        const snap = closedStack.current.pop();
        if (!snap) { pushToast('No recently closed tabs to reopen.'); return; }
        const f = freshTab({ name: snap.name, sql: snap.sql, conn: snap.conn, db: snap.db, dirty: true });
        setTabs(ts => [...ts, f]); setActiveId(f.id);
        try { localStorage.setItem(QH_CLOSED_KEY, JSON.stringify(closedStack.current)); } catch (err) {}
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
  useEffect(() => {
    try {
      const slim = { activeId, savedAt: Date.now(), tabs: tabs.map(t => ({ id: t.id, kind: t.kind, name: t.name, sql: t.sql, conn: t.conn, db: t.db, // reqId is persisted so a reload does not change the number a tab has
        // been showing. Without it every reload would reserve a new id.
        reqId: t.reqId, qid: t.qid, touchedAt: t.touchedAt || Date.now() })) };
      localStorage.setItem(QH_WS_KEY, JSON.stringify(slim));
    } catch (e) {}
  }, [tabs, activeId]);

  // Tabs restored from a workspace saved before this feature (or from a session
  // where the reservation failed) carry no request id. Give each one a number,
  // once — reqId is persisted, so this does not repeat on every reload, and a
  // tab that already has one is left alone.
  useEffect(() => {
    if (!qhApi || !qhApi.reserveDraft) return;
    const missing = tabs.filter(t => !t.kind && !t.reqId && !t.qid);
    if (!missing.length) return;
    missing.forEach(t => {
      qhApi.reserveDraft()
        .then(r => { if (r && r.id) patch(t.id, { reqId: r.id }); })
        .catch(() => {});
    });
    // Deliberately keyed on the ids that still need one, not on `tabs`: keying
    // on tabs would re-run on every keystroke and reserve an id per character.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabs.map(t => (!t.kind && !t.reqId && !t.qid) ? t.id : '').join(',')]);

  // ----- auth boot: load the real signed-in user from the session cookie -----
  useEffect(() => {
    let alive = true;
    qhApi.me()
      .then(m => { if (alive) { window.QH_TZ = (m && m.displayTz) || 'Europe/Istanbul'; setUser(m.user || null); } })
      .catch(() => { if (alive) setUser(null); })
      .finally(() => { if (alive) setAuthChecked(true); });
    const onSignedOut = () => setUser(null);
    window.addEventListener('qh:signed-out', onSignedOut);
    return () => { alive = false; window.removeEventListener('qh:signed-out', onSignedOut); };
  }, []);

  // ----- load real developer data once signed in -----
  // Tracked like the admin panel (Promise.allSettled): any failed section flips
  // devLoadError so the UI shows an error + Retry instead of a silently-empty
  // sidebar / home. Per-section refreshes elsewhere keep their own handling.
  const reloadDev = useCallback(() => {
    if (!user) return;
    setDevLoadError(false);
    const jobs = [
      qhApi.connections().then(r => setConns(r.connections || [])),
      qhApi.history().then(r => setHistory((r.history || []).map(h => ({
        id: h.id, sql: h.sql, conn: h.connectionId, db: h.databaseId, tier: h.tier,
        status: h.status, rows: h.rowCount, when: qhTimeAgo(h.createdAt), approver: h.approver,
      })))),
      // Merge the user's server-saved queries into the Saved library (server
      // rows are dest:'server'; on a name+conn+db clash the server row wins).
      qhApi.saved().then(r => {
        const server = (r.saved || []).map(s => ({
          id: s.id, name: s.name, conn: s.connectionId, db: s.databaseId, sql: s.sql, dest: 'server' }));
        setSavedList(list => {
          const localOnly = list.filter(l => !server.some(
            s => s.name === l.name && s.conn === l.conn && s.db === l.db));
          return [...server, ...localOnly];
        });
      }),
      // Server-synced named workspaces: merge server rows with local-only ones.
      qhApi.sessions().then(r => {
        const server = r.sessions || [];
        setSessions(list => [...server, ...list.filter(l => l.dest !== 'server')]);
      }),
      // Scheduled panel reflects the real scheduled queries (from `requests`).
      qhApi.scheduled().then(r => setScheduled((r.scheduled || []).map(s => ({
        ...s, when: s.when ? new Date(s.when).toLocaleString() : '' })))),
    ];
    Promise.allSettled(jobs).then(res => {
      if (res.some(x => x.status === 'rejected')) setDevLoadError(true);
    });
  }, [user]);

  useEffect(() => { reloadDev(); }, [reloadDev]);

  // Point any tab whose connection no longer resolves at the first real one.
  useEffect(() => {
    if (!conns.length) return;
    setTabs(ts => ts.map(x => conns.find(c => c.id === x.conn) ? x
      : { ...x, conn: conns[0].id, db: (conns[0].databases[0] || {}).id || null }));
  }, [conns]);

  // Fetch the active db's schema so autocomplete has real columns.
  useEffect(() => { loadSchema(tab.conn, tab.db); }, [tab.conn, tab.db, loadSchema]);

  // ----- theme -----
  useEffect(() => {
    const apply = () => {
      let th = t.theme;
      if (th === 'system') th = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
      document.documentElement.setAttribute('data-theme', th);
    };
    apply();
    if (t.theme === 'system') {
      const mq = window.matchMedia('(prefers-color-scheme: dark)');
      mq.addEventListener('change', apply);
      return () => mq.removeEventListener('change', apply);
    }
  }, [t.theme]);

  // ----- brand theme -----
  useEffect(() => {
    document.documentElement.setAttribute('data-brand', 'warm');
    // recolor the favicon to the active brand
    const col = (window.qhBrand ? window.qhBrand().mark : '#C4603F');
    const svg = "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' rx='24' fill='" + col + "'/><g fill='none' stroke='#fff' stroke-width='6' stroke-linecap='round' stroke-linejoin='round'><ellipse cx='50' cy='33' rx='24' ry='8'/><path d='M26 33 V67 C26 71.4 36.7 75 50 75 C63.3 75 74 71.4 74 67 V33'/><path d='M40 48 L48.5 55 L40 62' stroke-width='5.5'/><path d='M52 62 L60 62' stroke-width='5.5'/></g></svg>";
    const href = 'data:image/svg+xml;utf8,' + encodeURIComponent(svg);
    document.querySelectorAll("link[rel='icon'], link[rel='apple-touch-icon']").forEach(l => { l.href = href; });
  }, [t.brand]);

  const resolvedDark = (() => {
    if (t.theme === 'dark') return true;
    if (t.theme === 'light') return false;
    return window.matchMedia('(prefers-color-scheme: dark)').matches;
  })();

  // ----- derived -----
  // Local classification is a provisional hint (see qhClassify): it does not
  // know the target's engine, the grant, or the auto-approve windows. The
  // server's verdict replaces it once POST /classify answers, so the tier chip
  // and the Run-vs-Submit label reflect what will actually happen.
  const localClassify = qhClassify(tab.sql);
  const server = useServerClassify(tab.sql, tab.conn, tab.db);
  const classify = React.useMemo(() => {
    if (!server) return localClassify;
    return {
      tier: server.tier,
      statements: server.statements > 1
        ? new Array(server.statements).fill({ kw: '', tier: server.tier })
        : localClassify.statements,
      multi: server.statements > 1,
      empty: localClassify.empty,
      blocked: server.blocked,
      blockers: server.blockers,
      provisional: false,
    };
  }, [server, tab.sql]);
  const pii = qhDetectPII(tab.sql);
  const conn = conns.find(c => c.id === tab.conn);
  const db = conn && conn.databases.find(d => d.id === tab.db);
  // Column name -> { type, notNull } from the schema snapshot, for the result
  // grid's header tooltip. This is now the FALLBACK: a completed result carries
  // its real per-column types from the driver (`result.colTypes`, migration
  // 083) and those win. The snapshot is still consulted for the two things the
  // driver cannot answer — nullability (psycopg reports null_ok=None for every
  // column) and results that ran before 083 existed.
  //
  // The ambiguity guard stays, and it is the reason driver types were needed: a
  // name found in more than one table with a different definition is dropped,
  // and `id` / `user_id` / `created_at` live in dozens of tables — so the
  // columns people hover most were exactly the ones this map never had.
  const colMeta = React.useMemo(() => {
    const sch = schemaCache[tab.conn + '/' + tab.db];
    if (!sch) return {};
    const seen = {};
    Object.keys(sch.tables || {}).forEach(k => {
      if (k.indexOf('.') === -1) return;          // skip the bare-name alias
      (sch.tables[k].columns || []).forEach(col => {
        const meta = { type: col.type, notNull: col.nullable === false };
        if (!(col.name in seen)) seen[col.name] = meta;
        else if (!seen[col.name] || seen[col.name].type !== meta.type
                 || seen[col.name].notNull !== meta.notNull) {
          seen[col.name] = null;                  // ambiguous
        }
      });
    });
    const out = {};
    Object.keys(seen).forEach(n => { if (seen[n]) out[n] = seen[n]; });
    return out;
  }, [tab.conn, tab.db, schemaCache]);

  const editorSchema = React.useMemo(() => {
    const refs = db ? (db.tableRefs || (db.tables || []).map(n => ({ s: null, n }))) : [];
    const sch = schemaCache[tab.conn + '/' + tab.db];
    const cols = new Set();
    const tableCols = {};
    // `qualify[name]` is what actually gets inserted when a table is picked:
    // the real schema from the catalog, not the old public/dbo guess. When a
    // bare name exists in two schemas we leave it unqualified rather than
    // guess wrong — the tree still offers both, fully qualified.
    const qualify = {};
    const seen = {};
    refs.forEach(({ s, n }) => {
      const entry = (sch && (sch.tables[(s ? s + '.' : '') + n] || sch.tables[n])) || null;
      const cs = entry ? entry.columns.map(c => c.name) : [];
      if (!tableCols[n]) tableCols[n] = cs;
      cs.forEach(c => cols.add(c));
      seen[n] = (seen[n] || 0) + 1;
      if (s) qualify[n] = seen[n] > 1 ? null : s + '.' + n;
    });
    // Engine system catalog (pg_catalog / information_schema / sys.*). The
    // snapshot deliberately skips these, so they were missing from
    // autocomplete entirely even though the sidebar already listed them —
    // people had to remember `pg_stat_activity` exactly.
    const conn = conns.find(c => c.id === tab.conn);
    const engMeta = (typeof qhEngine === 'function' && conn) ? qhEngine(conn) : null;
    const systemTables = engMeta && engMeta.system
      ? [].concat(...Object.values(engMeta.system)) : [];
    const dbs = [...new Set(conns.flatMap(c => c.databases.map(d => d.name)))];
    return { tables: refs.map(r => r.n), columns: [...cols], dbs, tableCols,
             qualify, systemTables };
  }, [tab.conn, tab.db, schemaCache, conns]);
  const dbTier = db ? db.tier : 'RO';
  // Prefer the server's answer on both of these. The local fallback is only in
  // play before the first response (or if /classify is unreachable), and the
  // fallback for tierExceedsGrant is deliberately permissive: a wrong "exceeds"
  // disables the submit button for a query the server would accept, which is a
  // client-only outage the user cannot work around.
  const autoApprove = server ? server.willAutoApprove
    : (isSuper || (localClassify.tier === 'RO' && qhAutoApproveRO(conn, db)));
  const tierExceedsGrant = server ? server.tierExceedsGrant : false;
  const editorEngine = qhEngineId(conn && conn.engine);
  const busy = tab.status === 'pending' || tab.status === 'approved' || tab.status === 'running';
  const killed = !!(admin.killSwitch && admin.killSwitch.enabled);
  const riskHints = qhRiskHints(tab.sql, classify);
  const riskTop = riskHints.some(h => h.level === 'high') ? 'high' : riskHints.some(h => h.level === 'med') ? 'med' : 'low';

  // keep tab tier dot synced for tabs bar
  const tabsForBar = tabs.map(x => ({ ...x, tier: qhClassify(x.sql).tier }));
  const queryTabsForBar = tabsForBar.filter(x => !x.kind);
  const isWelcome = tab.kind === 'welcome';
  const isWhatsNew = tab.kind === 'whatsnew';

  // ----- editor change -----
  const onCode = (sql) => patch(activeId, { sql, dirty: true, touchedAt: Date.now() });

  const openWelcome = () => { setTabs(ts => ts.some(x => x.kind === 'welcome') ? ts : [welcomeTab(), ...ts]); setActiveId('welcome'); };
  const goHome = () => { setView('dev'); openWelcome(); };

  // What's-new page — its own kinded tab (like Welcome). The unseen dot clears
  // when opened (records the current build sha in localStorage).
  const [seenNewsSha, setSeenNewsSha] = useState(() => { try { return localStorage.getItem(QH_NEWS_KEY) || ''; } catch (e) { return ''; } });
  const latestNewsSha = qhLatestNewsSha();
  const unseenNews = !!latestNewsSha && seenNewsSha !== latestNewsSha;
  const markNewsSeen = () => {
    setSeenNewsSha(latestNewsSha);
    try { localStorage.setItem(QH_NEWS_KEY, latestNewsSha); } catch (e) {}
  };
  const openWhatsNew = () => {
    setView('dev');
    setTabs(ts => ts.some(x => x.kind === 'whatsnew') ? ts : [...ts, whatsNewTab()]);
    setActiveId('whatsnew');
    markNewsSeen();
  };

  // Create the tab first, then focus it by its real id (avoids racing the
  // setTabs updater's TAB_SEQ++ with a 't'+(TAB_SEQ-1) guess).
  const [edFocus, setEdFocus] = React.useState(0);
  const focusEditor = () => setEdFocus(x => x + 1);

  const newQueryOn = (c, db) => {
    const f = freshTab({ name: c.name, conn: c.id, db: db ? db.id : c.databases[0].id });
    setTabs(ts => [...ts, f]); setActiveId(f.id); focusEditor(); reserveFor(f.id);  };

  const pickDb = (c, d) => patch(activeId, { conn: c.id, db: d.id });

  const loadSaved = (s) => {
    const f = freshTab({ name: s.name, sql: s.sql, conn: s.conn, db: s.db });
    setTabs(ts => [...ts, f]); setActiveId(f.id); reserveFor(f.id);
    setSideMode('conns');  };
  const loadHistory = (h) => {
    const f = freshTab({ name: h.sql.slice(0, 24), sql: h.sql, conn: h.conn, db: h.db });
    setTabs(ts => [...ts, f]); setActiveId(f.id); reserveFor(f.id);  };

  // Reserve the request id for a tab the moment it exists, then patch it in.
  // Fire-and-forget on purpose: the number is a convenience, so a slow or failed
  // call leaves the tab perfectly usable and the id simply arrives at submit.
  const reserveFor = (tabId) => {
    qhApi.reserveDraft()
      .then(r => { if (r && r.id) patch(tabId, { reqId: r.id }); })
      .catch(() => {});
  };

  const newTab = () => {
    const c = conns.find(x => x.id === tab.conn) || conns[0];
    const f = freshTab(c ? { name: c.name, conn: c.id, db: tab.db } : {});
    setTabs(ts => [...ts, f]); setActiveId(f.id); reserveFor(f.id);  };

  // ---- Save/open a tab's SQL as a .sql file (client-side; no backend) ----
  const requestDownloadSql = (id) => {
    const t = tabs.find(x => x.id === id);
    if (!t) return;
    if (!(t.sql && t.sql.trim())) { pushToast('Nothing to save — tab is empty.'); return; }
    const base = ((t.name || 'query').replace(/[^\w.\- ]+/g, '').trim().replace(/\s+/g, '-')) || 'query';
    setDlModal({ id, name: (/\.sql$/i.test(base) ? base : base + '.sql') });
  };
  const performDownloadSql = async (filename) => {
    const id = dlModal && dlModal.id;
    const t = tabs.find(x => x.id === id);
    const sql = t ? (t.sql || '') : '';
    const fname = /\.sql$/i.test(filename) ? filename : filename + '.sql';
    setDlModal(null);
    try {
      if (window.showSaveFilePicker) {
        const handle = await window.showSaveFilePicker({ suggestedName: fname, types: [{ description: 'SQL file', accept: { 'application/sql': ['.sql'] } }] });
        const w = await handle.createWritable(); await w.write(sql); await w.close();
        pushToast('Saved ' + fname + '.');
      } else {
        const url = URL.createObjectURL(new Blob([sql], { type: 'application/sql' }));
        const a = document.createElement('a'); a.href = url; a.download = fname;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        pushToast('Saved ' + fname + ' to your downloads.');
      }
    } catch (e) { if (e && e.name === 'AbortError') return; pushToast('Could not save the file.'); }
  };
  // Open a local .sql file into a new query tab (keeps the active tab's conn/db).
  const openSqlFile = (name, text) => {
    const base = ((name || 'query').replace(/\.sql$/i, '')).trim() || 'Untitled query';
    const src = (tab && !tab.kind) ? tab : null;
    const over = { name: base, sql: text || '' };
    if (src) { over.conn = src.conn; over.db = src.db; }
    const f = freshTab(over);
    setTabs(ts => [...ts, f]); setActiveId(f.id); focusEditor();
    pushToast('Opened ' + base + '.sql in a new query tab.');
  };
  const activeHasSql = !!(tab && !tab.kind && tab.sql && tab.sql.trim());
  newTabRef.current = newTab;
  const closeTabNow = (id) => {
    setTabs(ts => {
      const i = ts.findIndex(x => x.id === id);
      const next = ts.filter(x => x.id !== id);
      if (!next.length) { setActiveId('welcome'); return [welcomeTab()]; }
      if (id === activeId) setActiveId(next[Math.max(0, i - 1)].id);
      return next;
    });
  };
  const closeOthersNow = (id) => { setActiveId(id); setTabs(ts => ts.filter(x => x.id === id)); };
  const closeRightNow = (id) => {
    const ii = tabs.findIndex(x => x.id === id);
    const ai = tabs.findIndex(x => x.id === activeId);
    if (ai > ii) setActiveId(id);
    setTabs(ts => { const i = ts.findIndex(x => x.id === id); return i < 0 ? ts : ts.slice(0, i + 1); });
  };
  const closeAllNow = () => { setTabs([welcomeTab()]); setActiveId('welcome'); };

  // Closing dirty (unsaved) tabs asks first; pristine tabs close immediately.
  const isDirtyTab = (tb) => tb.dirty && tb.sql && tb.sql.trim();
  const victimsFor = (kind, id) => {
    if (kind === 'others') return tabs.filter(x => x.id !== id);
    if (kind === 'all') return tabs.slice();
    if (kind === 'right') { const i = tabs.findIndex(x => x.id === id); return i < 0 ? [] : tabs.slice(i + 1); }
    return tabs.filter(x => x.id === id);
  };
  const performClose = (kind, id) => {
    const removed = victimsFor(kind, id).filter(t => t.sql && t.sql.trim());
    for (const t of removed) closedStack.current.push({ name: t.name, sql: t.sql, conn: t.conn, db: t.db });
    if (closedStack.current.length > 30) closedStack.current = closedStack.current.slice(-30);
    try { localStorage.setItem(QH_CLOSED_KEY, JSON.stringify(closedStack.current)); } catch (e) {}
    if (kind === 'others') closeOthersNow(id);
    else if (kind === 'all') { closeAllNow(); }
    else if (kind === 'right') closeRightNow(id);
    else closeTabNow(id);
  };
  const requestClose = (kind, id) => {
    const victims = victimsFor(kind, id).filter(isDirtyTab);
    if (victims.length) setClosePrompt({ kind, id, victims });
    else performClose(kind, id);
  };
  const saveSession = () => {
    if (!closePrompt) return;
    const { kind, id, victims } = closePrompt;
    const stamp = Date.now();
    const entries = victims.map((tb, i) => ({ id: 'usv_' + stamp + '_' + i, name: tb.name, conn: tb.conn, db: tb.db, sql: tb.sql, dest: saveDest }));
    if (saveDest === 'server') entries.forEach(e => qhApi.saveSnippet({ name: e.name, connectionId: e.conn, databaseId: e.db, sql: e.sql }).catch(() => {}));
    setSavedList(list => {
      let next = list.slice();
      for (const e of entries) {
        const i = next.findIndex(x => x.name === e.name && x.conn === e.conn && x.db === e.db);
        if (i >= 0) next[i] = { ...next[i], sql: e.sql, dest: e.dest }; else next = [e, ...next];
      }
      return next;
    });
    setTabs(ts => ts.map(x => victims.some(v => v.id === x.id) ? { ...x, dirty: false } : x));
    pushToast(victims.length + (victims.length > 1 ? ' queries' : ' query') + ' saved ' + (saveDest === 'server' ? 'to server — synced to your account.' : 'to this browser.'));
    setClosePrompt(null);
    performClose(kind, id);
  };
  const discardAndClose = () => { if (!closePrompt) return; const { kind, id } = closePrompt; setClosePrompt(null); performClose(kind, id); };
  const reorderTabs = (fromId, toId) => setTabs(ts => {
    const from = ts.findIndex(x => x.id === fromId), to = ts.findIndex(x => x.id === toId);
    if (from < 0 || to < 0 || from === to) return ts;
    const arr = ts.slice(); const [m] = arr.splice(from, 1); arr.splice(to, 0, m); return arr;
  });
  const deleteSaved = (id) => {
    const s = savedList.find(x => x.id === id);
    if (s && s.dest === 'server') qhApi.deleteSnippet(id).catch(() => {});
    setSavedList(list => list.filter(x => x.id !== id));
  };
  const saveNamedSession = (name, dest) => {
    const nm = (name || '').trim(); if (!nm) return;
    const snapTabs = tabs.filter(t => t.sql && t.sql.trim()).map(t => ({ name: t.name, sql: t.sql, conn: t.conn, db: t.db }));
    if (!snapTabs.length) { pushToast('Nothing to save — all tabs are empty.'); return; }
    setSessionModal(false);
    if (dest === 'server') {
      qhApi.saveSessionSrv({ name: nm, tabs: snapTabs.map(t => ({ name: t.name, sql: t.sql, connectionId: t.conn, databaseId: t.db })) })
        .then(s => setSessions(list => [s, ...list.filter(x => !(x.dest === 'server' && x.name === nm))]))
        .catch(e => pushToast((e && e.message) || 'Could not save workspace to the server.'));
    } else {
      const snap = { id: 'ses_' + Date.now(), name: nm, dest, savedAt: Date.now(), tabs: snapTabs };
      setSessions(list => { const i = list.findIndex(x => x.name === nm && x.dest !== 'server'); if (i >= 0) { const n = list.slice(); n[i] = snap; return n; } return [snap, ...list]; });
    }
    pushToast('Workspace “' + nm + '” saved ' + (dest === 'server' ? 'to server — synced to your account.' : 'to this browser.') + ' (' + snapTabs.length + ' tabs)');
  };
  const restoreSession = (sess) => {
    if (!sess.tabs || !sess.tabs.length) return;
    const fresh = sess.tabs.map(t => freshTab({ name: t.name, sql: t.sql, conn: t.conn, db: t.db }));
    setTabs(ts => [...ts, ...fresh]); setActiveId(fresh[0].id); setSideMode('conns');    pushToast('Opened “' + sess.name + '” — ' + fresh.length + ' tab' + (fresh.length > 1 ? 's' : '') + '.');
  };
  const deleteSession = (id) => {
    const s = sessions.find(x => x.id === id);
    if (s && s.dest === 'server') qhApi.deleteSessionSrv(id).catch(() => {});
    setSessions(list => list.filter(x => x.id !== id));
  };
  const duplicateTab = (id) => {
    const src = tabs.find(x => x.id === id);
    if (!src) return;
    const clone = freshTab({ name: /\bcopy$/.test(src.name) ? src.name : src.name + ' copy', sql: src.sql, conn: src.conn, db: src.db });
    setTabs(ts => { const i = ts.findIndex(x => x.id === id); return i < 0 ? [...ts, clone] : [...ts.slice(0, i + 1), clone, ...ts.slice(i + 1)]; });
    setActiveId(clone.id);  };
  const renameTab = (id, name) => { const nm = (name || '').trim(); if (nm) patch(id, { name: nm }); };
  const copyTabSql = (id) => {
    const t = tabs.find(x => x.id === id);
    if (!t) return;
    if (!(t.sql && t.sql.trim())) { pushToast('Nothing to copy — tab is empty.'); return; }
    try { qhCopyText(t.sql).then(ok => pushToast(ok ? 'SQL copied to clipboard.' : 'Could not copy — the browser blocked clipboard access.')); }
    catch (e) { pushToast('Could not copy SQL.'); }
  };

  // ----- run / submit flow -----
  const audit = (id, actor, event) => setTabs(ts => ts.map(x => x.id === id ? { ...x, audit: [...x.audit, { actor, event, time: nowTime() }] } : x));

  const POLL_MS = 1500;
  const trackers = useRef({});
  const stopTrack = (id) => {
    const tr = trackers.current[id];
    if (!tr) return;
    if (tr.interval) clearInterval(tr.interval);
    if (tr.ws) { try { tr.ws.close(); } catch (e) {} }
    delete trackers.current[id];
  };
  useEffect(() => () => Object.keys(trackers.current).forEach(stopTrack), []);

  const refreshHistory = () => qhApi.history().then(r => setHistory((r.history || []).map(h => ({
    id: h.id, sql: h.sql, conn: h.connectionId, db: h.databaseId, tier: h.tier,
    status: h.status, rows: h.rowCount, when: qhTimeAgo(h.createdAt), approver: h.approver,
  })))).catch(() => {});

  const applyStatus = async (id, qid, sres) => {
    setTabs(ts => ts.map(x => x.id === id ? {
      ...x, status: sres.status, runMs: sres.runMs, messages: sres.messages || [], audit: sres.audit || [],
    } : x));
    if (sres.scheduledFor && sres.status !== 'running' && sres.status !== 'done'
        && new Date(sres.scheduledFor) > new Date()) { setResTab('messages'); return true; }
    if (sres.status === 'done' || sres.status === 'failed' || sres.status === 'rejected') {
      refreshHistory();
      if (sres.status === 'done') {
        try {
          const res = await qhApi.result(qid);
          setTabs(ts => ts.map(x => x.id === id ? { ...x, result: res } : x));
          setResTab('results');
        } catch (e) { setResTab('messages'); }
      } else { setResTab('messages'); }
      return true;
    }
    return false;
  };

  const refreshOnce = async (id, qid) => {
    try { if (await applyStatus(id, qid, await qhApi.status(qid))) stopTrack(id); }
    catch (e) { if (e.status === 401) stopTrack(id); }
  };
  const startPolling = (id, qid) => {
    if (trackers.current[id] && trackers.current[id].interval) return;
    trackers.current[id] = { ...(trackers.current[id] || {}),
                             interval: setInterval(() => refreshOnce(id, qid), POLL_MS) };
  };
  const track = (id, qid) => {
    stopTrack(id);
    refreshOnce(id, qid);
    let opened = false, ws;
    // DESIGN MOCK: no server, so there is no /stream socket to open — go
    // straight to polling instead of failing a WebSocket first. The served app
    // never sets QH_MOCK, so it keeps the stream-with-poll-fallback path.
    if (window.QH_MOCK) { startPolling(id, qid); return; }
    try {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(proto + '://' + location.host + API_BASE + '/queries/' + qid + '/stream');
    } catch (e) { startPolling(id, qid); return; }
    trackers.current[id] = { ...(trackers.current[id] || {}), ws };
    const fallback = setTimeout(() => { if (!opened) { try { ws.close(); } catch (e) {} startPolling(id, qid); } }, 3000);
    ws.onopen = () => { opened = true; };
    ws.onmessage = () => { refreshOnce(id, qid); };
    ws.onclose = () => { clearTimeout(fallback); if (trackers.current[id]) startPolling(id, qid); };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  };

  const submitToServer = async (id, runAtISO, sqlOverride) => {
    const cur = tabs.find(x => x.id === id);
    if (!cur) return;
    setResTab('messages');
    patch(id, { status: 'pending', result: null,
      messages: [{ kind: 'info', text: 'Submitting to QueryHub…', time: nowTime() }] });
    try {
      const r = await qhApi.submit({
        connectionId: cur.conn, databaseId: cur.db, sql: sqlOverride || cur.sql,
        name: cur.name, justification: cur.justification || null,
        schedule: runAtISO ? { runAt: runAtISO } : null,
        // The id this tab has shown since it opened. The server takes it over
        // when it is still ours to take; a draft that expired gets a fresh id
        // instead, which is why qid below comes from the RESPONSE, not from here.
        draftId: cur.reqId || null,
      });
      patch(id, { qid: r.id, reqId: r.id, status: r.status });
      track(id, r.id);
    } catch (e) {
      patch(id, { status: null,
        messages: [{ kind: 'err', text: e.message || 'Submit failed.', time: nowTime() }] });
      setResTab('messages');
    }
  };

  // EXPLAIN preview (read-only, no execution). Server plans a single RO
  // statement and returns the plan + risk hints; we show them in the Plan tab.
  const explain = async () => {
    if (!tab.sql.trim() || busy || !tab.conn) return;
    if (!['RO', 'RW'].includes(classify.tier)) { pushToast('Plan preview is for read-only or read-write queries (not DDL).'); return; }
    try {
      const view = await qhApi.explain({ connectionId: tab.conn, databaseId: tab.db, sql: tab.sql });
      patch(activeId, { plan: view });
      setResTab('plan');
    } catch (e) { pushToast((e && e.message) || 'Could not preview the plan.'); }
  };

  // Stop a query that is running on the database right now. The server signals
  // the actual backend, escalating from a cancel to closing the connection —
  // necessary, because a database blocked writing results to a slow client
  // ignores a plain cancel (measured: a 300s statement timeout was still
  // running at 578s, and only a terminate ended it).
  //
  // The toast reports which of the two happened, because "cancelled" and "we
  // had to close the connection" are different facts and the second is worth
  // knowing. Status is left to the normal poll/stream to pick up, so the UI
  // never claims a stop the server did not confirm.
  const cancelRun = async () => {
    if (!tab.qid) return;
    try {
      const r = await qhApi.cancelRun(tab.qid);
      pushToast(r && r.message ? r.message : 'Stop requested.');
    } catch (e) {
      pushToast((e && e.message) || 'Could not stop the query.');
    }
  };

  const primary = () => {
    if (killed) { pushToast('Kill switch is engaged — query execution is paused.'); return; }
    if (!tab.sql.trim() || busy || !tab.conn) return;
    if (tierExceedsGrant) { pushToast(classify.tier + ' exceeds your ' + dbTier + ' grant on this database.'); return; }
    submitToServer(activeId);
  };

  // F8 — submit the selected text as a one-off (server runs it as its own query)
  const runSelection = (selText) => {
    const t = (selText || '').trim();
    if (!t || busy || !tab.conn) return;
    if (killed) { pushToast('Kill switch is engaged — query execution is paused.'); return; }
    submitToServer(activeId, null, t);
  };

  // Batch — one bundle = one approval round (the server groups them). Each tab
  // is patched to pending, then tracked by the request id the bundle returns
  // (items come back in submit order).
  const submitBatch = (ids) => {
    if (killed) { pushToast('Kill switch is engaged — query execution is paused.'); return; }
    const valid = ids.filter(id => { const x = tabs.find(t => t.id === id); return x && x.sql.trim() && x.conn; });
    if (!valid.length) return;
    setBatchOpen(false); setActiveId(valid[0]); setResTab('messages');
    const items = valid.map(id => { const x = tabs.find(t => t.id === id); return { connectionId: x.conn, databaseId: x.db, sql: x.sql, name: x.name }; });
    valid.forEach(id => patch(id, { status: 'pending', result: null,
      messages: [{ kind: 'info', text: 'Submitted in a batch — one approval round.', time: nowTime() }] }));
    qhApi.submitBatch({ items }).then(r => {
      (r.items || []).forEach((it, i) => {
        const tabId = valid[i];
        if (tabId) { patch(tabId, { qid: it.queryId, status: it.status }); track(tabId, it.queryId); }
      });
      refreshHistory();
      pushToast(valid.length + ' queries submitted as one batch (B#' + r.bundleId + ').');
    }).catch(e => {
      valid.forEach(id => patch(id, { status: null,
        messages: [{ kind: 'err', text: (e && e.message) || 'Batch submit failed.', time: nowTime() }] }));
      pushToast((e && e.message) || 'Batch submit failed.');
    });
  };

  const refreshScheduled = () => qhApi.scheduled().then(r => setScheduled((r.scheduled || []).map(s => ({
    ...s, when: s.when ? new Date(s.when).toLocaleString() : '' })))).catch(() => {});
  const schedule = (when) => {
    setSchedOpen(false);
    const iso = qhScheduleToISO(when);
    if (!iso) { pushToast('Unknown schedule preset.'); return; }
    submitToServer(activeId, iso);
    // The scheduled query now lives in `requests`; pull it into the panel once
    // the submit has landed.
    setTimeout(refreshScheduled, 800);
    pushToast('Query scheduled for ' + when + ' — see the Scheduled panel.');
  };
  const cancelScheduled = (id) => {
    qhApi.cancelScheduledSrv(id).then(refreshScheduled).catch(() => {});
    setScheduled(list => list.filter(x => x.id !== id));
  };
  const openScheduled = (s) => { const f = freshTab({ name: s.name, sql: s.sql, conn: s.conn, db: s.db }); setTabs(ts => [...ts, f]); setActiveId(f.id); };

  const exportResult = (format) => {
    if (!tab.result || tab.result.kind !== 'table') return;
    const cols = tab.result.cols;
    const CAP = 5000;
    const n = Math.min(tab.result.total, CAP);
    const capped = tab.result.total > CAP;
    const rows = tab.result.slice(0, n);
    const cell = (c, r) => { const v = r[c]; return v == null ? '' : v; };   // server results are already PII-masked
    const fname = (tab.name || 'query').replace(/\s+/g, '_');

    if (format === 'copy-csv' || format === 'copy-tsv') {
      const sep = format === 'copy-tsv' ? '\t' : ',';
      const q = format === 'copy-csv' ? (v => '"' + String(v).replace(/"/g, '""') + '"') : (v => String(v));
      const lines = [cols.map(q).join(sep)];
      for (const r of rows) lines.push(cols.map(c => q(cell(c, r))).join(sep));
      qhCopyText(lines.join('\n')).then(ok => pushToast(ok
        ? 'Copied ' + n.toLocaleString() + ' row' + (n > 1 ? 's' : '') + (capped ? ' (first ' + CAP.toLocaleString() + ')' : '') + ' as ' + (sep === '\t' ? 'TSV' : 'CSV') + '.'
        : 'Could not copy — the browser blocked clipboard access.'));
      return;
    }

    // Full-result downloads stream from the server (ALL rows, already PII-
    // masked) — no 5,000-row cap. The client fallback below only runs if the
    // server request id is somehow absent.
    if ((format === 'csv' || format === 'xlsx') && tab.qid) {
      const url = format === 'xlsx' ? qhApi.resultXlsxUrl(tab.qid) : qhApi.resultCsvUrl(tab.qid);
      const a = document.createElement('a'); a.href = url; a.rel = 'noopener'; a.click();
      pushToast('Downloading full result as ' + (format === 'xlsx' ? 'Excel (.xlsx)' : 'CSV') + '…');
      return;
    }

    const dl = (blob, name) => { const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name; a.click(); };
    if (format === 'xlsx') {
      const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      let xml = '<?xml version="1.0"?>\n<?mso-application progid="Excel.Sheet"?>\n<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><Worksheet ss:Name="' + esc((tab.name || 'Result').slice(0, 28)) + '"><Table>';
      xml += '<Row>' + cols.map(c => '<Cell><Data ss:Type="String">' + esc(c) + '</Data></Cell>').join('') + '</Row>';
      for (const r of rows) xml += '<Row>' + cols.map(c => { const v = cell(c, r); const num = /^-?\d+(\.\d+)?$/.test(String(v)); return '<Cell><Data ss:Type="' + (num ? 'Number' : 'String') + '">' + esc(v) + '</Data></Cell>'; }).join('') + '</Row>';
      xml += '</Table></Worksheet></Workbook>';
      dl(new Blob([xml], { type: 'application/vnd.ms-excel' }), fname + '.xls');
      if (capped) pushToast('Exported first ' + CAP.toLocaleString() + ' of ' + tab.result.total.toLocaleString() + ' rows.');
    } else {
      const esc = v => '"' + String(v).replace(/"/g, '""') + '"';
      const lines = [cols.join(',')];
      for (const r of rows) lines.push(cols.map(c => esc(cell(c, r))).join(','));
      dl(new Blob([lines.join('\n')], { type: 'text/csv' }), fname + '.csv');
      if (capped) pushToast('Exported first ' + CAP.toLocaleString() + ' of ' + tab.result.total.toLocaleString() + ' rows.');
    }
  };

  // resize results panel
  const drag = useRef(null);
  const onDragStart = (e) => {
    const container = e.currentTarget.parentElement;
    const ab = container ? container.querySelector('.qh-actionbar') : null;
    const abH = ab ? ab.offsetHeight : 0;
    const gripH = e.currentTarget.offsetHeight || 10;
    const maxH = Math.max(160, (container ? container.clientHeight : 800) - abH - gripH - 52);
    drag.current = { y: e.clientY, h: resH, maxH };
    document.body.style.cursor = 'row-resize'; window.addEventListener('mousemove', onDragMove); window.addEventListener('mouseup', onDragEnd);
  };
  const onDragMove = (e) => { if (drag.current) setResH(Math.max(80, Math.min(drag.current.maxH, drag.current.h - (e.clientY - drag.current.y)))); };
  const onDragEnd = () => { drag.current = null; document.body.style.cursor = ''; window.removeEventListener('mousemove', onDragMove); window.removeEventListener('mouseup', onDragEnd); };

  const openTable = (c, db, table, opts) => {
    const o = opts || {};
    const nm = o.mode === 'count' ? table + ' · count' : table;
    const f = freshTab({ name: nm, sql: qhSelectSql(c, db, table, o), conn: c.id, db: db.id });
    setTabs(ts => [...ts, f]); setActiveId(f.id); focusEditor();  };

  const [sideWidth, setSideWidth] = useState(() => { const n = parseInt(localStorage.getItem('qh.sidewidth.v1'), 10); return (n >= 224 && n <= 560) ? n : 264; });
  useEffect(() => { try { localStorage.setItem('qh.sidewidth.v1', String(sideWidth)); } catch (e) {} }, [sideWidth]);
  const onResizerDown = (e) => {
    e.preventDefault();
    const startX = e.clientX, startW = sideWidth, side = t.sidebarSide;
    const move = (ev) => { const dx = ev.clientX - startX; const w = side === 'right' ? startW - dx : startW + dx; setSideWidth(Math.max(224, Math.min(560, Math.round(w)))); };
    const up = () => { document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up); document.body.style.cursor = ''; document.body.style.userSelect = ''; };
    document.addEventListener('mousemove', move); document.addEventListener('mouseup', up);
    document.body.style.cursor = 'col-resize'; document.body.style.userSelect = 'none';
  };
  const toggleSide = () => setTweak('hideSidebar', !t.hideSidebar);

  const sideEl = !t.hideSidebar && (
    <Sidebar mode={sideMode} setMode={setSideMode} conns={conns} schemaCache={schemaCache} onLoadSchema={loadSchema} rolesCache={rolesCache} onLoadRoles={loadRoles}
      active={{ conn: tab.conn, db: tab.db }} onPick={pickDb}
      saved={savedList} onLoadSaved={loadSaved} onDeleteSaved={deleteSaved}
      sessions={sessions} onSaveSession={() => setSessionModal(true)} onRestoreSession={restoreSession} onDeleteSession={deleteSession}
      scheduled={scheduled} onOpenScheduled={openScheduled} onCancelScheduled={cancelScheduled}
      history={history} onLoadHistory={loadHistory}
      width={sideWidth} onResizerDown={onResizerDown}
      onRequestEndpoint={() => setReqOpen(true)} onOpenTable={openTable} onNewQuery={newQueryOn} onNewTab={newTab}
      onOpenSqlFile={openSqlFile} onDownloadSql={() => requestDownloadSql(activeId)} canDownloadSql={activeHasSql} isSuper={isSuper} />
  );

  const submitRequest = async (req) => {
    setReqOpen(false);
    try {
      await qhApi.requestEndpoint({ server: req.server, database: req.database, tier: req.tier, reason: req.reason });
      pushToast(req.tier + ' access to ' + req.server + '/' + (req.database || '(any db)') + ' requested — sent to DBA in Slack.');
    } catch (e) { pushToast(e.message || 'Request failed.'); }
  };
  const submitFeedback = (fb) => {
    // Backend (live): POST /api/feedback { type, subject, details, severity } (context — view, user,
    // browser/app version — attached server-side from the session). Records to the audit log + DMs admins.
    setFeedbackOpen(false);
    qhApi.feedback({ type: fb.type, subject: fb.subject, details: fb.details, severity: fb.severity || undefined, view })
      .then(() => pushToast(fb.type === 'bug' ? 'Bug report sent to the QueryHub team — thank you!' : 'Feedback sent to the QueryHub team — thank you!'))
      .catch((e) => pushToast((e && e.message) || 'Could not send feedback — please try again.'));
  };

  if (!user) return <LoginScreen onSignedIn={signIn} brand={t.brand} />;
  // A local account flagged must_change_pw is blocked until it sets a new
  // one (server also rejects submits with 403 password_change_required).
  if (user.mustChangePassword) return <ChangePasswordScreen forced />;

  return (
    <div className="qh-root">
      {pwOpen && <ChangePasswordScreen onCancel={() => setPwOpen(false)} />}
      <TopChrome resolvedDark={resolvedDark} theme={t.theme} setTheme={(v) => setTweak('theme', v)} user={user} onSignOut={signOut}
        onChangePassword={() => setPwOpen(true)}
        view={view} setView={setView} pendingCount={admin.queue.length} onGoHome={goHome} role={(user && user.role) || 'developer'} lastSync={lastSync} onFeedback={() => setFeedbackOpen(true)} onWhatsNew={openWhatsNew} unseenNews={unseenNews} onDismissNews={markNewsSeen} />

      {view === 'admin' && <AdminPanel st={admin} adminRole={adminRole} setAdminRole={setAdminRole} user={user} />}

      {killed && view === 'dev' && (
        <div className="qh-killbanner">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9"/><path d="M15 9l-6 6M9 9l6 6"/></svg>
          Query execution is paused fleet-wide by <b>{admin.killSwitch.by || 'an admin'}</b>{admin.killSwitch.message ? ' — ' + admin.killSwitch.message : ''} (kill switch). Submitting is disabled until it's released.
        </div>
      )}

      {view === 'dev' && (
      <div className={'qh-main side-' + t.sidebarSide}>
        {t.sidebarSide === 'left' && sideEl}

        <div className="qh-work">
          {devLoadError && (
            <div className="qh-load-error" role="alert" style={{ margin: '10px 14px 4px' }}>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L14.7 3.9a2 2 0 00-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg>
              <span>Couldn't load your workspace data (connections, saved, history). Check your connection and try again.</span>
              <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={reloadDev}>Retry</button>
            </div>
          )}
          <div className="qh-worktop">
            <button className="qh-side-toggle" onClick={toggleSide} title={t.hideSidebar ? 'Show sidebar' : 'Hide sidebar'} aria-label={t.hideSidebar ? 'Show sidebar' : 'Hide sidebar'}>
              {t.hideSidebar
                ? <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/><path d="M13.5 9l3 3-3 3"/></svg>
                : <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/><path d="M16.5 9l-3 3 3 3"/></svg>}
            </button>
            <EditorTabs tabs={tabsForBar} activeId={activeId} onSelect={selectTab} onClose={(id) => requestClose('one', id)} onNew={newTab}
              onCloseOthers={(id) => requestClose('others', id)} onCloseRight={(id) => requestClose('right', id)} onCloseAll={() => requestClose('all', activeId)} onDuplicate={duplicateTab} onRename={renameTab} onCopySql={copyTabSql} onDownloadSql={requestDownloadSql} onReorder={reorderTabs} />
          </div>

          {isWelcome ? (
            <HomeScreen user={user} openTabs={queryTabsForBar}
              onFocusTab={(id) => { if (id) setActiveId(id); else newTab(); }}
              onNewQuery={newTab}
              onSaveSession={() => setSessionModal(true)}
              sessions={sessions} onRestoreSession={restoreSession} onDeleteSession={deleteSession}
              scheduled={scheduled} onOpenScheduled={openScheduled} onCancelScheduled={cancelScheduled}
              history={history} onLoadHistory={loadHistory}
              saved={savedList} onLoadSaved={loadSaved} onDeleteSaved={deleteSaved}
              onBrowse={() => setSideMode('conns')} onWhatsNew={openWhatsNew} unseenNews={unseenNews} />
          ) : isWhatsNew ? (
            <WhatsNew />
          ) : (
            <>
              <ActionBar
                conn={conn} db={db} dbTier={dbTier} classify={classify} pii={pii}
                autoApprove={autoApprove} tierExceedsGrant={tierExceedsGrant} busy={busy} status={tab.status}
                hasSql={!!tab.sql.trim()} onPrimary={primary} onExplain={explain} killed={killed}
                riskHints={riskHints} riskTop={riskTop} onCancelRun={cancelRun}
                tabCount={queryTabsForBar.length} onOpenBatch={() => setBatchOpen(true)}
                schedOpen={schedOpen} setSchedOpen={setSchedOpen} onSchedule={schedule}
              />

              <div className="qh-ed-host">
                <SqlEditor value={tab.sql} onChange={onCode} fontSize={t.editorFont} onRun={primary} onRunSelection={runSelection} schema={editorSchema} engineId={editorEngine} focusSignal={edFocus} />
              </div>

              <div className="qh-res-grip" onMouseDown={onDragStart}><span /></div>
              <div style={{ height: resH, flexShrink: 0 }}>
                <ResultsPanel colMeta={colMeta} tab={resTab} setTab={setResTab} result={tab.result} messages={tab.messages}
                  audit={tab.audit} status={tab.status} runMs={tab.runMs} onExport={exportResult} plan={tab.plan} onToast={pushToast} reqId={tab.reqId} />
              </div>
            </>
          )}
        </div>

        {t.sidebarSide === 'right' && sideEl}
      </div>
      )}

      {reqOpen && <RequestEndpointModal onClose={() => setReqOpen(false)} onSubmit={submitRequest} />}
      {feedbackOpen && <FeedbackModal user={user} view={view} onClose={() => setFeedbackOpen(false)} onSubmit={submitFeedback} />}
      {dlModal && <DownloadSqlModal defaultName={dlModal.name} onConfirm={performDownloadSql} onCancel={() => setDlModal(null)} />}
      {batchOpen && (
        <BatchModal tabs={tabs} activeId={activeId} onClose={() => setBatchOpen(false)} onSubmit={submitBatch} />
      )}
      {closePrompt && (
        <CloseConfirmModal victims={closePrompt.victims} dest={saveDest} setDest={setSaveDest}
          onSave={saveSession} onDiscard={discardAndClose} onCancel={() => setClosePrompt(null)} />
      )}
      {sessionModal && (
        <SessionSaveModal defaultName={(tabs.find(x => x.id === activeId) || {}).name || ''}
          onSave={saveNamedSession} onCancel={() => setSessionModal(false)} />
      )}
      {toast && (
        <div className="qh-toast">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6 9 17l-5-5"/></svg>
          {toast}
        </div>
      )}

      {/* Design-tool scaffolding. Inert in production: qhDesignMode() is only
          true for the prototype, so the panel and its host protocol never
          load for a real user. */}
      {qhDesignMode() && <TweaksPanel>
        <TweakSection label="Design system" />
        <TweakRadio label="Brand" value={t.brand} options={[{ value: 'warm', label: 'Warm' }]} onChange={(v) => setTweak('brand', v)} />
        <TweakSection label="Theme" />
        <TweakRadio label="Mode" value={t.theme} options={['system', 'light', 'dark']} onChange={(v) => setTweak('theme', v)} />
        <TweakSection label="Editor" />
        <TweakSlider label="Font size" value={t.editorFont} min={11} max={20} step={1} unit="px" onChange={(v) => setTweak('editorFont', v)} />
        <TweakSection label="Layout" />
        <TweakRadio label="Sidebar side" value={t.sidebarSide} options={['left', 'right']} onChange={(v) => setTweak('sidebarSide', v)} />
        <TweakToggle label="Hide sidebar" value={t.hideSidebar} onChange={(v) => setTweak('hideSidebar', v)} />
      </TweaksPanel>}
    </div>
  );
}

// ---------- Avatar (Slack photo with initials fallback) ----------
function Avatar({ user, size = 34 }) {
  const [err, setErr] = useState(false);
  const px = size + 'px';
  if (user && user.avatar && !err) {
    return <img className="qh-avatar-img" src={user.avatar} alt={user.name} style={{ width: px, height: px }} onError={() => setErr(true)} draggable={false} />;
  }
  return <span className="qh-avatar-fallback" style={{ width: px, height: px, fontSize: Math.round(size * 0.36) + 'px' }}>{user ? user.initials : '?'}</span>;
}

// ---------- Notifications (bell + panel) ----------
function notifIcon(kind) {
  const p = { fill: 'none', stroke: 'currentColor', strokeWidth: 1.9, strokeLinecap: 'round', strokeLinejoin: 'round' };
  if (kind === 'approved') return <svg width="16" height="16" viewBox="0 0 24 24" {...p}><path d="M20 6L9 17l-5-5"/></svg>;
  if (kind === 'rejected') return <svg width="16" height="16" viewBox="0 0 24 24" {...p}><path d="M18 6L6 18M6 6l12 12"/></svg>;
  if (kind === 'scheduled') return <svg width="16" height="16" viewBox="0 0 24 24" {...p}><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>;
  if (kind === 'endpoint') return <svg width="16" height="16" viewBox="0 0 24 24" {...p}><circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/></svg>;
  if (kind === 'kill') return <svg width="16" height="16" viewBox="0 0 24 24" {...p}><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>;
  if (kind === 'news') return <svg width="16" height="16" viewBox="0 0 24 24" {...p}><path d="M12 3l1.8 4.5L18 9l-4.2 1.5L12 15l-1.8-4.5L6 9l4.2-1.5z"/><path d="M18.5 14.5l.9 2.1 2.1.9-2.1.9-.9 2.1-.9-2.1-2.1-.9 2.1-.9z"/></svg>;
  return <svg width="16" height="16" viewBox="0 0 24 24" {...p}><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></svg>;
}

function NotificationBell({ unseenNews, onOpenNews, onDismissNews }) {
  // Real feed: GET /notifications (newest first), read state mirrored server-side
  // via POST /notifications/read so it follows the user across devices. The
  // localStorage mirror keeps the badge instant across reloads.
  const [list, setList] = useState(() => window.QH_NOTIFICATIONS || []);
  const [open, setOpen] = useState(false);
  const [readIds, setReadIds] = useState(() => { try { const r = JSON.parse(localStorage.getItem('qh.notif.v1')); if (Array.isArray(r)) return r; } catch (e) {} return []; });
  useEffect(() => { try { localStorage.setItem('qh.notif.v1', JSON.stringify(readIds)); } catch (e) {} }, [readIds]);
  useEffect(() => {
    let alive = true;
    const load = () => qhApi.notifications().then(r => {
      if (!alive) return;
      const items = (r.notifications || []).map(n => ({ ...n, when: n.when || qhTimeAgo(n.createdAt) }));
      setList(items);
      const srvRead = items.filter(n => n.read).map(n => n.id);
      if (srvRead.length) setReadIds(prev => [...new Set([...prev, ...srvRead])]);
    }).catch(() => {});
    load();
    const iv = setInterval(load, 60000);
    return () => { alive = false; clearInterval(iv); };
  }, []);
  const isUnread = (n) => !readIds.includes(n.id);
  // A pending changelog release shows as one extra unread "news" item at the top.
  const unread = list.filter(isUnread).length + (unseenNews ? 1 : 0);
  const markRead = (id) => {
    setReadIds(r => r.includes(id) ? r : [...r, id]);
    qhApi.notificationsRead({ ids: [id] }).catch(() => {});
  };
  const markAll = () => {
    setReadIds(list.map(n => n.id));
    qhApi.notificationsRead({ all: true }).catch(() => {});
    if (unseenNews && onDismissNews) onDismissNews();
  };
  const openNews = () => { setOpen(false); if (onOpenNews) onOpenNews(); };
  return (
    <div className="qh-notif">
      <button className="qh-icon-btn qh-notif-btn" onClick={() => setOpen(o => !o)} aria-label={'Notifications' + (unread ? ' (' + unread + ' unread)' : '')} title="Notifications">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M18 8a6 6 0 10-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 01-3.4 0"/></svg>
        {unread > 0 && <span className="qh-notif-badge">{unread > 9 ? '9+' : unread}</span>}
      </button>
      {open && (<>
        <div className="qh-notif-backdrop" onClick={() => setOpen(false)} />
        <div className="qh-notif-menu">
          <div className="qh-notif-head">
            <span className="qh-notif-h-title">Notifications{unread > 0 && <span className="qh-notif-h-count">{unread}</span>}</span>
            {unread > 0 && <button className="qh-notif-clear" onClick={markAll}>Mark all read</button>}
          </div>
          <div className="qh-notif-list">
            {list.length === 0 && !unseenNews && <div className="qh-notif-empty">You're all caught up.</div>}
            {unseenNews && (
              <button className="qh-notif-item is-unread" onClick={openNews}>
                <span className="qh-notif-ic k-news">{notifIcon('news')}</span>
                <span className="qh-notif-tx">
                  <span className="qh-notif-title">New updates available</span>
                  <span className="qh-notif-body">See what's changed in this release.</span>
                  <span className="qh-notif-when">Just now</span>
                </span>
                <span className="qh-notif-udot" />
              </button>
            )}
            {list.map(n => (
              <button key={n.id} className={'qh-notif-item' + (isUnread(n) ? ' is-unread' : '')} onClick={() => markRead(n.id)}>
                <span className={'qh-notif-ic k-' + n.kind}>{notifIcon(n.kind)}</span>
                <span className="qh-notif-tx">
                  <span className="qh-notif-title">{n.title}</span>
                  <span className="qh-notif-body">{n.body}</span>
                  <span className="qh-notif-when">{n.when}</span>
                </span>
                {isUnread(n) && <span className="qh-notif-udot" />}
              </button>
            ))}
          </div>
        </div>
      </>)}
    </div>
  );
}

// ---------- Top chrome (logo + theme toggle) ----------
function TopChrome({ resolvedDark, theme, setTheme, user, onSignOut, onChangePassword, view, setView, pendingCount, onGoHome, role, lastSync, onFeedback, onWhatsNew, unseenNews, onDismissNews }) {
  const toggle = () => setTheme(resolvedDark ? 'light' : 'dark');
  const [menu, setMenu] = useState(false);
  // Only admins (dba/super) get the Developer↔Admin switch. A plain developer
  // has nothing on the Admin side, so the toggle is hidden — their role is
  // already shown by the role badge on the right.
  const isAdmin = role === 'dba' || role === 'super';
  return (
    <header className="qh-top">
      <button className="qh-brand" onClick={onGoHome} title="Go to QueryHub home">
        <QHMark size={30} variant="green" radius={0.3} />
        <span className="qh-brand-name">QueryHub</span>
        <span className="qh-brand-tag">web</span>
      </button>
      {isAdmin && (
      <div className="qh-viewswitch">
        <button className={'qh-vs-opt' + (view === 'dev' ? ' is-active' : '')} onClick={() => setView('dev')}>
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M8 9l3 3-3 3M13 15h3"/><rect x="3" y="4" width="18" height="16" rx="2"/></svg>
          Developer
        </button>
        <button className={'qh-vs-opt' + (view === 'admin' ? ' is-active' : '')} onClick={() => setView('admin')}>
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
          Admin
          {pendingCount > 0 && <span className="qh-vs-badge">{pendingCount}</span>}
        </button>
      </div>
      )}
      <div className="qh-top-right">
        <span className="qh-slack-note">
          <span className="qh-slack-dot" />
          {view === 'admin' ? 'Actions mirror to Slack' : role === 'super' ? 'Full access · no approval' : 'Approvals run in Slack'}
        </span>
        <span className={'qh-role-badge role-' + (role || 'developer')} title="Change under Tweaks → Access"><span className="qh-role-emoji">{role === 'super' ? '⭐' : role === 'dba' ? '🔑' : '💻'}</span>{role === 'super' ? 'Super admin' : role === 'dba' ? 'DBA' : 'Developer'}</span>
        <button className="qh-icon-btn" onClick={onFeedback} title="Send feedback or report a bug" aria-label="Send feedback">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M3 11l18-5v12L3 14z"/><path d="M11.6 16.8a3 3 0 11-5.8-1.6"/></svg>
        </button>
        <button className="qh-icon-btn" onClick={toggle} title="Toggle theme" aria-label="Toggle theme">
          {resolvedDark
            ? <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="4.5"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4"/></svg>
            : <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12.8A8.5 8.5 0 1111.2 3a6.6 6.6 0 009.8 9.8z"/></svg>}
        </button>
        <NotificationBell unseenNews={unseenNews} onOpenNews={onWhatsNew} onDismissNews={onDismissNews} />
        <div className="qh-user">
          <button className="qh-avatar-btn" onClick={() => setMenu(m => !m)} onBlur={() => setTimeout(() => setMenu(false), 140)} title={user ? user.name : ''}>
            <Avatar user={user} size={34} />
          </button>
          {menu && user && (
            <div className="qh-user-menu">
              <div className="qh-user-info">
                <div className="qh-user-idrow">
                  <Avatar user={user} size={40} />
                  <div className="qh-user-idtext">
                    <div className="qh-user-name">{user.name}</div>
                    <div className="qh-user-mail">{user.email}</div>
                  </div>
                </div>
                <div className="qh-user-slack"><SlackMark size={12} />{user.team} · {user.slackId}</div>
                <div className="qh-user-sync">{lastSync ? 'Server-synced · ' + qhAgo(lastSync) : 'Server sync: nothing this session'}</div>
                <div className="qh-user-ver">
                  <span>{(window.QH_BUILD && window.QH_BUILD.version) || window.QH_VERSION}{window.QH_BUILD && window.QH_BUILD.date ? ' · ' + window.QH_BUILD.date : ''}</span>
                  {window.QH_BUILD && window.QH_BUILD.sha && (qhCommitUrl(window.QH_BUILD.sha)
                    ? <a className="qh-user-sha qh-mono" href={qhCommitUrl(window.QH_BUILD.sha)} target="_blank" rel="noopener noreferrer" onMouseDown={(e) => e.stopPropagation()} title="View this build on GitHub">{window.QH_BUILD.sha}</a>
                    : <span className="qh-user-sha qh-mono">{window.QH_BUILD.sha}</span>)}
                </div>
              </div>
              <button className="qh-user-item" onMouseDown={(e) => { e.preventDefault(); onWhatsNew(); }}>
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12 3l1.9 3.9 4.3.6-3.1 3 .7 4.3-3.8-2-3.8 2 .7-4.3-3.1-3 4.3-.6z"/></svg>
                What's new
                {unseenNews && <span className="qh-user-newpill">New</span>}
              </button>
              {user && user.provider === 'local' && onChangePassword && (
                <button className="qh-user-item" onMouseDown={(e) => { e.preventDefault(); onChangePassword(); }}>
                  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="11" width="18" height="10" rx="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg>
                  Change password
                </button>
              )}
              <button className="qh-user-signout" onMouseDown={(e) => { e.preventDefault(); onSignOut(); }}>
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9"/></svg>
              Sign out
            </button>
            </div>
          )}
        </div>
      </div>
    </header>
  );
}

// ---------- Action bar (target context + security + actions) ----------
function ActionBar({ conn, db, dbTier, classify, pii, autoApprove, tierExceedsGrant, busy, status, hasSql, onPrimary, killed, riskHints, riskTop, onExplain, tabCount, onOpenBatch, schedOpen, setSchedOpen, onSchedule, onCancelRun }) {
  const tierLabel = { RO: 'Read-only', RW: 'Read/Write', DDL: 'Schema (DDL)' }[classify.tier];
  const highRisks = (riskHints || []).filter(h => h.level !== 'low');
  const schedBtnRef = useRef(null);
  const [schedPos, setSchedPos] = useState(null);
  const [customWhen, setCustomWhen] = useState('');
  const nowLocal = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  const toggleSched = () => {
    if (schedOpen) { setSchedOpen(false); return; }
    const r = schedBtnRef.current.getBoundingClientRect();
    setSchedPos({ top: r.bottom + 6, right: Math.max(8, window.innerWidth - r.right) });
    setSchedOpen(true);
  };
  let primaryLabel = autoApprove ? 'Run' : 'Submit for approval';
  if (killed) primaryLabel = 'Paused (kill switch)';
  else if (busy) primaryLabel = status === 'pending' ? 'Awaiting DBA approval…' : status === 'running' ? 'Running…' : 'Approved — running…';

  return (
    <div className="qh-actionbar">
      <button className={'qh-btn qh-btn-primary qh-run' + (autoApprove ? '' : ' is-approval') + (busy ? ' is-waiting' : '')}
        data-kbd={QH_KBD.run}
        onClick={() => onPrimary()} disabled={!hasSql || busy || tierExceedsGrant || killed}>
        {busy && <span className="qh-spin light" />}
        {!busy && autoApprove && <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round"><path d="M7 5v14l11-7z"/></svg>}
        {!busy && !autoApprove && <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 2 11 13M22 2l-7 20-4-9-9-4z"/></svg>}
        {primaryLabel}
      </button>

      {/* One button for "stop this", from the moment there is something to
          stop. What it DOES depends on how far the request got, and the label
          says which — they are genuinely different acts, and one word for both
          would leave the user unsure whether their query already touched the
          database:
            pending / approved   it has not run: withdraw the request, which
                                 also takes it off the admins' queue.
            running              it is on the database now: the server cancels
                                 the backend and escalates to closing the
                                 connection when the cancel does not land,
                                 because a backend blocked writing results
                                 ignores a cancel outright (measured: a 300s
                                 statement timeout still running at 578s).
          Anything terminal hides it — there is nothing left to take back, and
          offering the button would imply there is. */}
      {onCancelRun && (status === 'running' || status === 'pending' || status === 'approved') && (
        <button className={'qh-btn qh-btn-ghost qh-cancel-run' + (status === 'running' ? ' is-terminate' : '')}
          onClick={() => onCancelRun()}
          title={status === 'running'
            ? 'Stop this query on the database'
            : 'Withdraw this request before it runs'}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" strokeWidth="2" strokeLinejoin="round">
            <rect x="6" y="6" width="12" height="12" rx="1.5" />
          </svg>
          {status === 'running' ? 'Stop' : 'Cancel request'}
        </button>
      )}

      <div className="qh-ab-div" />

      <div className="qh-target">
        {/* The toolbar KEEPS its env tag on purpose: this is the line you read
            just before pressing Run, so PROD belongs here even though the list
            no longer repeats it on every row. And when the chosen connection is
            disabled, say it here loudest — it is the last moment before the
            query goes to a retired instance that will answer anyway. */}
        {conn && conn.disabled && <span className="qh-conn-off" title="Disabled — retired or parked. It will still answer, with stale data.">disabled</span>}
        {conn && <span className={'qh-envtag-sm env-' + conn.env} title={conn.env}>{conn.env === 'production' ? 'PROD' : conn.env === 'staging' ? 'STG' : conn.env}</span>}
        <span className="qh-target-conn">{conn ? conn.name : '—'}</span>
        <span className="qh-target-slash">/</span>
        <span className="qh-target-db">{db ? db.name : '—'}</span>
        {/* The badge is the tier YOU are granted here (the chip further right is
            what the query you wrote classifies as). The label used to be spelled
            out next to it and read as noise, so it moved to the hover. */}
        <span className="qh-target-tier" title={'You have ' + dbTier + ' permission'
          + { RO: ' (read-only)', RW: ' (read/write)', DDL: ' (schema changes)' }[dbTier]
          + (conn && db ? ' on ' + conn.name + '/' + db.name : '')}>
          <TierBadge tier={dbTier} sm />
        </span>
      </div>

      <div className="qh-ab-spacer" />

      {hasSql && !classify.empty && (
        <div className="qh-sec">
          <span className={'qh-sec-badge tier-' + classify.tier.toLowerCase()}>
            <span className="qh-sec-dot" />{classify.tier}
            <span className="qh-sec-label">{tierLabel}</span>
          </span>
          {classify.multi && <span className="qh-sec-multi">{classify.statements.length} statements</span>}
          {highRisks.length > 0 && (
            <span className={'qh-risk-chip risk-' + riskTop} title={highRisks.map(h => h.text).join('\n')} onClick={onExplain}>
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L14.7 3.9a2 2 0 00-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg>
              {highRisks.length} risk{highRisks.length > 1 ? 's' : ''}
            </span>
          )}
          {(pii.columns.length > 0 || pii.star) && (
            <span className="qh-pii-chip" title={pii.columns.map(c => c.label).join(', ')}>
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="4" y="10" width="16" height="10" rx="2"/><path d="M8 10V7a4 4 0 018 0v3"/></svg>
              {pii.star ? 'PII may be masked' : pii.columns.length + ' PII column' + (pii.columns.length > 1 ? 's' : '') + ' masked'}
            </span>
          )}
        </div>
      )}

      {tierExceedsGrant && (
        <span className="qh-grant-warn">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L14.7 3.9a2 2 0 00-3.4 0z"/></svg>
          {classify.tier} exceeds your {dbTier} grant
        </span>
      )}

      <div className="qh-ab-actions">
        <button className="qh-btn qh-btn-ghost" onClick={onExplain} disabled={!hasSql || busy || (classify && !['RO', 'RW'].includes(classify.tier))} title={classify && classify.tier === 'DDL' ? "EXPLAIN can't plan DDL (ALTER/CREATE/DROP) — only RO & RW queries" : "EXPLAIN + risk hints — plans the query, never executes"}>
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3M11 8v6M8 11h6"/></svg>
          Explain
        </button>
        {tabCount > 1 && (
          <button className="qh-btn qh-btn-ghost" onClick={onOpenBatch} disabled={busy || killed} title="Submit several queries in one approval round">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><path d="M17.5 14v7M14 17.5h7"/></svg>
            Batch
          </button>
        )}
        <button ref={schedBtnRef} className="qh-btn qh-btn-ghost" onClick={toggleSched} disabled={!hasSql || busy}>
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="13" r="8"/><path d="M12 9v4l2.5 1.5M9 2h6"/></svg>
          Schedule
        </button>
      </div>

      {schedOpen && schedPos && (
        <>
          <div className="qh-ctx-backdrop" onClick={() => setSchedOpen(false)} onContextMenu={(e) => { e.preventDefault(); setSchedOpen(false); }} />
          <div className="qh-sched-pop" style={{ position: 'fixed', top: schedPos.top, right: schedPos.right, zIndex: 91 }} onClick={(e) => e.stopPropagation()}>
            <div className="qh-sched-title">Schedule this query</div>
            {['In 1 hour', 'Tonight 02:00', 'Tomorrow 09:00'].map(w => (
              <button key={w} className="qh-sched-opt" onClick={() => onSchedule(w)}>{w}</button>
            ))}
            <div className="qh-ctx-sep" />
            <div className="qh-sched-custom">
              <label className="qh-sched-clabel">Custom date &amp; time</label>
              <input type="datetime-local" className="qh-sched-cinput" value={customWhen} min={nowLocal} onChange={(e) => setCustomWhen(e.target.value)} />
              <button className="qh-btn qh-btn-primary qh-btn-sm" disabled={!customWhen} onClick={() => onSchedule(customWhen.replace('T', ' '))}>Schedule</button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

// ---------- Unsaved-changes on close ----------
function CloseConfirmModal({ victims, dest, setDest, onSave, onDiscard, onCancel }) {
  const many = victims.length > 1;
  return (
    <QhModal onClose={onCancel}>
      <div className="qh-modal-head">
        <div>
          <div className="qh-modal-title">Save changes before closing?</div>
          <div className="qh-modal-sub">{many ? victims.length + ' tabs have' : 'This tab has'} unsaved edits. Save {many ? 'them' : 'it'} to your query library — they'll appear under <b>Saved</b> in the sidebar — or close without saving.</div>
        </div>
        <button className="qh-icon-btn" onClick={onCancel} aria-label="Close"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
      </div>
      <div className="qh-modal-body">
        <div className="qh-closelist">
          {victims.map(v => (
            <div key={v.id} className="qh-closelist-row">
              <span className={'qh-tab-dot tier-' + qhClassify(v.sql).tier.toLowerCase()} />
              <span className="qh-closelist-name">{v.name}</span>
              <span className="qh-closelist-meta">{v.conn} · {v.db}</span>
            </div>
          ))}
        </div>
        <div className="qh-field">
          <div className="qh-field-lbl">Save to</div>
          <div className="qh-seg">
            <button className={'qh-seg-opt' + (dest === 'server' ? ' is-active' : '')} onClick={() => setDest('server')}>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="3" width="20" height="8" rx="2"/><rect x="2" y="13" width="20" height="8" rx="2"/><path d="M6 7h.01M6 17h.01"/></svg>
              Server · synced
            </button>
            <button className={'qh-seg-opt' + (dest === 'local' ? ' is-active' : '')} onClick={() => setDest('local')}>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="4" width="20" height="14" rx="2"/><path d="M8 21h8M12 18v3"/></svg>
              This browser
            </button>
          </div>
        </div>
      </div>
      <div className="qh-modal-foot" style={{ justifyContent: 'space-between' }}>
        <button className="qh-btn qh-btn-danger" onClick={onDiscard}>Don't save</button>
        <div style={{ display: 'flex', gap: 10 }}>
          <button className="qh-btn qh-btn-ghost" onClick={onCancel}>Cancel</button>
          <button className="qh-btn qh-btn-primary" onClick={onSave}>Save &amp; close</button>
        </div>
      </div>
    </QhModal>
  );
}

// ---------- Save workspace as a named session ----------
function SessionSaveModal({ defaultName, onSave, onCancel }) {
  const [name, setName] = useState(defaultName || '');
  const [dest, setDest] = useState('server');
  const submit = () => { if (name.trim()) onSave(name, dest); };
  return (
    <QhModal onClose={onCancel}>
      <div className="qh-modal-head">
        <div>
          <div className="qh-modal-title">Save workspace</div>
          <div className="qh-modal-sub">Save every open tab as one named session. Restore it later from the <b>Sessions</b> panel — on this browser, or any device if synced.</div>
        </div>
        <button className="qh-icon-btn" onClick={onCancel} aria-label="Close"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
      </div>
      <div className="qh-modal-body">
        <div className="qh-field">
          <div className="qh-field-lbl">Session name</div>
          <input className="qh-input" autoFocus value={name} placeholder="e.g. Payments incident — Jul 15"
            onChange={(e) => setName(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') submit(); }} />
        </div>
        <div className="qh-field">
          <div className="qh-field-lbl">Save to</div>
          <div className="qh-seg">
            <button className={'qh-seg-opt' + (dest === 'server' ? ' is-active' : '')} onClick={() => setDest('server')}>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="3" width="20" height="8" rx="2"/><rect x="2" y="13" width="20" height="8" rx="2"/><path d="M6 7h.01M6 17h.01"/></svg>
              Server · synced
            </button>
            <button className={'qh-seg-opt' + (dest === 'local' ? ' is-active' : '')} onClick={() => setDest('local')}>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="4" width="20" height="14" rx="2"/><path d="M8 21h8M12 18v3"/></svg>
              This browser
            </button>
          </div>
        </div>
      </div>
      <div className="qh-modal-foot">
        <button className="qh-btn qh-btn-ghost" onClick={onCancel}>Cancel</button>
        <button className="qh-btn qh-btn-primary" disabled={!name.trim()} onClick={submit}>Save workspace</button>
      </div>
    </QhModal>
  );
}

// ---------- Download a tab's SQL as a .sql file ----------
function DownloadSqlModal({ defaultName, onConfirm, onCancel }) {
  const [name, setName] = useState(defaultName || 'query.sql');
  const canPick = typeof window !== 'undefined' && 'showSaveFilePicker' in window;
  const clean = (name || '').trim();
  const submit = () => { if (clean) onConfirm(clean); };
  return (
    <QhModal onClose={onCancel}>
      <div className="qh-modal-head">
        <div>
          <div className="qh-modal-title">Download query</div>
          <div className="qh-modal-sub">Save this query to your computer as a <b>.sql</b> file.</div>
        </div>
        <button className="qh-icon-btn" onClick={onCancel} aria-label="Close"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
      </div>
      <div className="qh-modal-body">
        <div className="qh-field">
          <div className="qh-field-lbl">File name</div>
          <input className="qh-input" autoFocus value={name} onChange={(e) => setName(e.target.value)}
            onFocus={(e) => { const d = e.target.value.lastIndexOf('.'); e.target.setSelectionRange(0, d > 0 ? d : e.target.value.length); }}
            onKeyDown={(e) => { if (e.key === 'Enter') submit(); }} placeholder="query.sql" />
        </div>
        <div className="qh-field">
          <div className="qh-field-lbl">Saves to</div>
          <div className="qh-dl-dest">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z"/></svg>
            {canPick ? 'A save dialog will open so you can choose the folder.' : "Your browser’s downloads folder."}
          </div>
        </div>
      </div>
      <div className="qh-modal-foot">
        <button className="qh-btn qh-btn-ghost" onClick={onCancel}>Cancel</button>
        <button className="qh-btn qh-btn-primary" disabled={!clean} onClick={submit}>Download</button>
      </div>
    </QhModal>
  );
}

// ---------- Feedback / bug report ----------
function FeedbackModal({ user, view, onClose, onSubmit }) {
  const [type, setType] = useState('bug');
  const [subject, setSubject] = useState('');
  const [details, setDetails] = useState('');
  const [severity, setSeverity] = useState('med');
  const valid = subject.trim() && details.trim();
  const submit = () => { if (valid) onSubmit({ type, subject: subject.trim(), details: details.trim(), severity: type === 'bug' ? severity : null }); };
  return (
    <QhModal onClose={onClose}>
      <div className="qh-modal-head">
        <div>
          <div className="qh-modal-title">{type === 'bug' ? 'Report a bug' : 'Send feedback'}</div>
          <div className="qh-modal-sub">Report a problem or share an idea — it goes straight to the QueryHub team.</div>
        </div>
        <button className="qh-icon-btn" onClick={onClose} aria-label="Close"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg></button>
      </div>
      <div className="qh-modal-body">
        <div className="qh-field">
          <div className="qh-field-lbl">What is this?</div>
          <div className="qh-seg">
            <button className={'qh-seg-opt' + (type === 'bug' ? ' is-active' : '')} onClick={() => setType('bug')}>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M8 6V4a4 4 0 018 0v2M6 10h12v5a6 6 0 01-12 0zM2 13h4M18 13h4M4 8l2 1.5M20 8l-2 1.5M4 18l2-1.5M20 18l-2-1.5"/></svg>
              Bug report
            </button>
            <button className={'qh-seg-opt' + (type === 'idea' ? ' is-active' : '')} onClick={() => setType('idea')}>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M9 18h6M10 22h4M12 2a7 7 0 00-4 12.7c.6.5 1 1.3 1 2.1h6c0-.8.4-1.6 1-2.1A7 7 0 0012 2z"/></svg>
              Idea / feedback
            </button>
          </div>
        </div>
        {type === 'bug' && (
          <div className="qh-field">
            <div className="qh-field-lbl">Severity</div>
            <div className="qh-seg">
              {[['low', 'Low'], ['med', 'Medium'], ['high', 'High']].map(([v, l]) => (
                <button key={v} className={'qh-seg-opt' + (severity === v ? ' is-active' : '')} onClick={() => setSeverity(v)}>{l}</button>
              ))}
            </div>
          </div>
        )}
        <div className="qh-field">
          <div className="qh-field-lbl">{type === 'bug' ? 'Summary' : 'Subject'}</div>
          <input className="qh-input" autoFocus value={subject} onChange={(e) => setSubject(e.target.value)}
            placeholder={type === 'bug' ? 'e.g. Results grid freezes on the 1000-row page' : 'e.g. Let me pin favourite connections'} />
        </div>
        <div className="qh-field">
          <div className="qh-field-lbl">{type === 'bug' ? 'What happened?' : 'Details'}</div>
          <textarea className="qh-input qh-textarea" rows={5} value={details} onChange={(e) => setDetails(e.target.value)}
            placeholder={type === 'bug' ? 'Steps to reproduce, what you expected, and what happened instead.' : 'Tell us what would make QueryHub better.'} />
        </div>
        <div className="qh-fb-meta">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></svg>
          Attached automatically: {view === 'admin' ? 'Admin panel' : 'Developer view'} · {(user && user.name) || 'you'} · browser &amp; app version
        </div>
      </div>
      <div className="qh-modal-foot">
        <button className="qh-btn qh-btn-ghost" onClick={onClose}>Cancel</button>
        <button className="qh-btn qh-btn-primary" disabled={!valid} onClick={submit}>{type === 'bug' ? 'Send bug report' : 'Send feedback'}</button>
      </div>
    </QhModal>
  );
}

// ---------- Batch / bundle modal ----------
function BatchModal({ tabs, activeId, onClose, onSubmit }) {
  const eligible = tabs.filter(x => x.sql.trim());
  const [sel, setSel] = useState(() => eligible.map(x => x.id));
  const toggle = (id) => setSel(s => s.includes(id) ? s.filter(x => x !== id) : [...s, id]);
  return (
    <QhModal onClose={onClose}>
      <div className="qh-modal-head">
        <div>
          <div className="qh-modal-title">Submit as a batch</div>
          <div className="qh-modal-sub">Queue several queries into one approval round. A DBA approves (or rejects) the whole bundle at once in Slack; each result comes back here.</div>
        </div>
        <button className="qh-icon-btn" onClick={onClose} aria-label="Close"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
      </div>
      <div className="qh-modal-body">
        <div className="qh-batch-list">
          {eligible.map(x => {
            const cl = qhClassify(x.sql);
            return (
              <label key={x.id} className={'qh-batch-item' + (sel.includes(x.id) ? ' is-on' : '')}>
                <input type="checkbox" checked={sel.includes(x.id)} onChange={() => toggle(x.id)} />
                <span className="qh-qcheck-box"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5"/></svg></span>
                <span className="qh-batch-name">{x.name}</span>
                <span className="qh-batch-target">{x.conn}/{x.db}</span>
                <TierBadge tier={cl.tier} sm />
              </label>
            );
          })}
        </div>
      </div>
      <div className="qh-modal-foot">
        <button className="qh-btn qh-btn-ghost" onClick={onClose}>Cancel</button>
        <button className="qh-btn qh-btn-primary is-approval" disabled={!sel.length} onClick={() => onSubmit(sel)}>
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 2 11 13M22 2l-7 20-4-9-9-4z"/></svg>
          Submit {sel.length} as one bundle
        </button>
      </div>
    </QhModal>
  );
}

// A single render error used to blank the entire page: React unmounts the tree
// on an uncaught error, so a bad row in one panel took the whole app with it and
// the user saw white. The boundary keeps the failure legible and recoverable —
// and, importantly for a tool people run queries in, it says the work may still
// be running server-side rather than implying it vanished.
class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // Console only: no telemetry endpoint exists, and inventing one would be a
    // silent egress from a page that deliberately has none.
    console.error('QueryHub UI error:', error, info && info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="qh-crash" role="alert">
        <h1>Something broke in the interface</h1>
        <p>
          The page hit an unexpected error and stopped rendering. Nothing was
          submitted or cancelled by this — any query already approved keeps
          running on the server.
        </p>
        <pre className="qh-crash-detail">{String(this.state.error && this.state.error.message || this.state.error)}</pre>
        <p>
          <button type="button" onClick={() => window.location.reload()}>Reload the page</button>
        </p>
        <p className="qh-crash-hint">
          If it happens again, the browser console has the component stack —
          that is the useful part in a bug report.
        </p>
      </div>
    );
  }
}

Object.assign(window, { App, ErrorBoundary });
ReactDOM.createRoot(document.getElementById('root')).render(
  <ErrorBoundary><App /></ErrorBoundary>
);
