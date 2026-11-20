// QueryHub Admin — Sessions: the sessions and locks on one connection
// (2026-11-20, DBeaver item P2.6). Super-admin only and read only.
//
// The server reads pg_stat_activity / pg_locks on PostgreSQL and the dm_exec_*
// views on SQL Server. This screen kills nothing. On PostgreSQL a row gives
// the pg_terminate_backend statement for that session. The normal Run path then
// asks for confirmation. SQL Server gets no such button, because QueryHub
// refuses KILL.
const { useState: useSess } = React;

// The engines that have a session view on the server.
const QH_SESS_ENGINES = ['postgres', 'mssql'];

// The engine id of an admin connection row: `engineId` from the server, or the
// display label read by qhEngineId.
function qhSessEngine(c) {
  if (!c) return '';
  if (c.engineId) return String(c.engineId).toLowerCase();
  return typeof qhEngineId === 'function' ? qhEngineId(c.engine) : String(c.engine || '').toLowerCase();
}

// The connections the picker lists: primaries on an engine with sessions. A
// deleted or disabled row and a read replica are left out.
function qhSessConns(rows) {
  return (rows || []).filter(c => c && !c.deleted && c.enabled !== false && !c.replicaOf
    && QH_SESS_ENGINES.indexOf(qhSessEngine(c)) !== -1);
}

// The database the picker starts on: the connection's default, else the first.
function qhSessDefaultDb(c) {
  const dbs = (c && c.databases) || [];
  const ids = dbs.map(d => d.id || d.name);
  if (c && c.defaultDatabase && ids.indexOf(c.defaultDatabase) !== -1) return c.defaultDatabase;
  return ids[0] || (c && c.defaultDatabase) || '';
}

// How long the current statement ran, at the time of the read. '' when unknown.
function qhSessRunning(queryStarted, at) {
  const from = Date.parse(queryStarted || '');
  const to = typeof at === 'number' ? at : Date.parse(at || '');
  if (!isFinite(from) || !isFinite(to)) return '';
  const s = Math.max(0, Math.floor((to - from) / 1000));
  if (s < 1) return '< 1 s';
  if (s < 60) return s + ' s';
  if (s < 3600) return Math.floor(s / 60) + ' min ' + (s % 60) + ' s';
  return Math.floor(s / 3600) + ' h ' + Math.floor((s % 3600) / 60) + ' min';
}

// The sessions whose user, app or query holds the text (case-insensitive).
function qhSessFilter(sessions, text) {
  const q = String(text || '').trim().toLowerCase();
  if (!q) return (sessions || []).slice();
  return (sessions || []).filter(s => [s.user, s.app, s.queryHidden ? '' : s.query]
    .some(v => String(v || '').toLowerCase().indexOf(q) !== -1));
}

// Locks that wait come first. The server order stays inside each group.
function qhSessLocksSorted(locks) {
  const all = (locks || []).slice();
  return all.filter(l => !l.granted).concat(all.filter(l => l.granted));
}

// The statement that ends one PostgreSQL session. Null for an id that is not a
// whole number, so nothing but a number goes into the SQL.
function qhSessTerminateSql(id) {
  const n = Number(id);
  if (!Number.isSafeInteger(n) || n <= 0) return null;
  return 'SELECT pg_terminate_backend(' + n + ');';
}

// The sentence for a failed read.
function qhSessErrorText(e) {
  if (e && e.status === 400) return 'This connection has no session view.';
  if (e && e.status === 403) return 'Only a super-admin can see sessions.';
  return 'Could not load sessions. ' + ((e && e.message) || 'Try again.');
}

function QhSessQuery({ s, notes, open, onToggle }) {
  if (s.queryHidden) {
    const why = (notes || []).join(' ') || 'The server does not show this query.';
    return <span className="qh-asess-hidden" title={why}>hidden</span>;
  }
  if (!s.query) return <span className="qh-asess-muted">—</span>;
  return (
    <button type="button" className={'qh-asess-query' + (open ? ' is-open' : '')} aria-expanded={open}
            title={open ? 'Show one line' : 'Show the full query'} onClick={onToggle}>{s.query}</button>
  );
}

// `onOpenSql({ conn, db, sql })` opens a new editor tab. Without it the button
// copies the statement instead.
function AdminSessions({ st, onOpenSql, onToast }) {
  const conns = qhSessConns((st && (st.connectionRows || st.connections)) || []);
  const [conn, setConn] = useSess('');
  const [db, setDb] = useSess('');
  const [tick, setTick] = useSess(0);
  // { key, data } after an answer, { key, error } after a failure.
  const [res, setRes] = useSess(null);
  const [filter, setFilter] = useSess('');
  const [open, setOpen] = useSess({});
  const [copied, setCopied] = useSess(null);
  const key = conn ? conn + '\u0000' + db + '\u0000' + tick : '';

  React.useEffect(() => {
    if (!conn) return undefined;
    let live = true;
    qhApi.adminSessions(conn, db || null)
      .then(r => { if (live) setRes({ key, data: r || {} }); })
      .catch(e => { if (live) setRes({ key, error: e || {} }); });
    return () => { live = false; };
  }, [key]);

  const cur = conns.find(c => c.id === conn) || null;
  const pickConn = (id) => {
    const c = conns.find(x => x.id === id) || null;
    setConn(id); setDb(qhSessDefaultDb(c)); setOpen({});
  };
  const fresh = res && res.key === key ? res : null;
  const loading = !!conn && !fresh;
  const data = fresh && fresh.data;
  const sessions = (data && data.sessions) || [];
  const shown = qhSessFilter(sessions, filter);
  const locks = qhSessLocksSorted((data && data.locks) || []);
  const notes = (data && data.notes) || [];
  const isPg = !!data && String(data.engine || '').toLowerCase() === 'postgres';
  const at = data ? (Date.parse(data.fetchedAt || '') || Date.now()) : Date.now();
  const blocked = sessions.filter(s => (s.blockedBy || []).length).length;
  const waiting = locks.filter(l => !l.granted).length;

  const terminate = (s) => {
    const sql = qhSessTerminateSql(s.id);
    if (!sql) return;
    if (onOpenSql) { onOpenSql({ conn, db: (data && data.database) || db, sql }); return; }
    qhCopyText(sql).then(ok => {
      setCopied(ok ? s.id : null);
      if (onToast) onToast(ok ? 'Statement copied. Paste it in the editor to run it.' : 'Could not copy. The browser blocked clipboard access.');
    });
  };

  const head = (
    <div className="qh-aview-head"><div>
      <div className="qh-aview-title">Sessions</div>
      <div className="qh-aview-sub">Who is connected, what runs, and which locks wait. Read only. Super-admin only.</div>
    </div></div>
  );

  const pickers = (
    <div className="qh-asess-bar">
      <select className="qh-select" aria-label="Connection" value={conn} onChange={(e) => pickConn(e.target.value)}>
        <option value="">Pick a connection…</option>
        {conns.map(c => <option key={c.id} value={c.id}>{c.name || c.id}</option>)}
      </select>
      {cur && (
        <select className="qh-select" aria-label="Database" value={db} onChange={(e) => { setDb(e.target.value); setOpen({}); }}>
          {(cur.databases || []).map(d => <option key={d.id || d.name} value={d.id || d.name}>{d.name || d.id}</option>)}
        </select>
      )}
      <button type="button" className="qh-btn qh-btn-ghost qh-btn-sm" disabled={!conn || loading} onClick={() => setTick(tick + 1)}>
        {loading ? 'Loading…' : 'Refresh'}</button>
      {data && data.fetchedAt && <span className="qh-asess-at">Read at {new Date(at).toLocaleTimeString()}</span>}
      <div className="qh-flex1" />
      {data && <input className="qh-input qh-input-sm qh-asess-filter" type="search" placeholder="Filter user, app, query"
                      aria-label="Filter sessions" value={filter} onChange={(e) => setFilter(e.target.value)} />}
    </div>
  );

  let body = null;
  if (!conns.length) body = <div className="qh-asess-state">No connection on PostgreSQL or SQL Server.</div>;
  else if (!conn) body = <div className="qh-asess-state">Pick a connection to see its sessions and locks.</div>;
  else if (loading) body = <div className="qh-asess-state" aria-busy="true">Loading…</div>;
  else if (fresh.error) body = (
    <div className="qh-asess-state is-error" role="alert">{qhSessErrorText(fresh.error)}
      {' '}<button type="button" className="qh-btn qh-btn-ghost qh-btn-sm" onClick={() => setTick(tick + 1)}>Try again</button></div>
  );
  else body = (
    <>
      {notes.length > 0 && <div className="qh-asess-notes">{notes.map((n, i) => <div key={i}>{n}</div>)}</div>}
      <div className="qh-asess-sum">{sessions.length} session{sessions.length === 1 ? '' : 's'} · {blocked} blocked · {waiting} lock{waiting === 1 ? '' : 's'} waiting</div>

      <div className="qh-mcard qh-asess-card">
        <div className="qh-mcard-title">Sessions</div>
        {shown.length === 0 ? (
          <div className="qh-asess-state">{sessions.length ? 'No session matches the filter.' : 'No session on this database.'}</div>
        ) : (
          <div className="qh-tablewrap"><table className="qh-atable qh-asess-table">
            <thead><tr><th>ID</th><th>User</th><th>App</th><th>State</th><th>Wait</th><th title="At the time of the read">Running for</th><th>Blocked by</th><th>Query</th>{isPg && <th aria-label="Actions" />}</tr></thead>
            <tbody>
              {shown.map(s => {
                const by = s.blockedBy || [], blk = s.blocking || [];
                return (
                  <tr key={s.id} data-session={s.id} className={'qh-asess-row' + (by.length ? ' is-blocked' : '')}>
                    <td className="qh-mono">{s.id}{blk.length > 0 && <span className="qh-asess-chip" title={'Blocks ' + blk.join(', ')}>Blocking {blk.length}</span>}</td>
                    <td>{s.user || <span className="qh-asess-muted">—</span>}</td>
                    <td>{s.app || <span className="qh-asess-muted">—</span>}</td>
                    <td>{s.state || <span className="qh-asess-muted">—</span>}</td>
                    <td>{s.wait || <span className="qh-asess-muted">—</span>}</td>
                    <td className="qh-mono">{qhSessRunning(s.queryStarted, at) || <span className="qh-asess-muted">—</span>}</td>
                    <td className="qh-mono">{by.length ? by.join(', ') : <span className="qh-asess-muted">—</span>}</td>
                    <td className="qh-asess-qcell"><QhSessQuery s={s} notes={notes} open={!!open[s.id]}
                      onToggle={() => setOpen(o => ({ ...o, [s.id]: !o[s.id] }))} /></td>
                    {isPg && <td>{qhSessTerminateSql(s.id) && (
                      <button type="button" className="qh-btn qh-btn-ghost qh-btn-sm qh-asess-term" onClick={() => terminate(s)}
                              title="Puts SELECT pg_terminate_backend(…) in the editor. Nothing runs until you run it.">
                        {onOpenSql ? 'Terminate in editor' : (copied === s.id ? 'Copied' : 'Copy terminate SQL')}</button>)}</td>}
                  </tr>
                );
              })}
            </tbody>
          </table></div>
        )}
      </div>

      <div className="qh-mcard qh-asess-card">
        <div className="qh-mcard-title">Locks</div>
        {locks.length === 0 ? <div className="qh-asess-state">No lock on this database.</div> : (
          <div className="qh-tablewrap"><table className="qh-atable qh-asess-locks">
            <thead><tr><th>Session</th><th>Type</th><th>Mode</th><th>Granted</th><th>Relation</th></tr></thead>
            <tbody>
              {locks.map((l, i) => (
                <tr key={i} data-lock-session={l.id}>
                  <td className="qh-mono">{l.id}</td>
                  <td>{l.type || '—'}</td>
                  <td className="qh-mono">{l.mode || '—'}</td>
                  <td>{l.granted ? 'Yes' : <span className="qh-asess-no">No</span>}</td>
                  <td className="qh-mono">{l.relation || <span className="qh-asess-muted">—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table></div>
        )}
      </div>
    </>
  );

  return <div className="qh-apad qh-asess">{head}{pickers}{body}</div>;
}

Object.assign(window, { QH_SESS_ENGINES, qhSessEngine, qhSessConns, qhSessDefaultDb, qhSessRunning, qhSessFilter,
  qhSessLocksSorted, qhSessTerminateSql, qhSessErrorText, QhSessQuery, AdminSessions });
