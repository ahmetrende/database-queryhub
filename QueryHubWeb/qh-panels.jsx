// QueryHub — left sidebar (connections / saved / history) + bottom results panel.

function TierBadge({ tier, sm }) {
  return <span className={'qh-tier tier-' + tier.toLowerCase() + (sm ? ' is-sm' : '')}>{tier}</span>;
}

function StatusPill({ status }) {
  const map = {
    pending:  ['Pending',  'st-pending'],
    approved: ['Approved', 'st-approved'],
    running:  ['Running',  'st-running'],
    done:     ['Done',     'st-done'],
    rejected: ['Rejected', 'st-rejected'],
    failed:   ['Failed',   'st-rejected'],
  };
  const [label, cls] = map[status] || ['—', ''];
  return (
    <span className={'qh-status ' + cls}>
      {status === 'running' && <span className="qh-spin" />}
      {status === 'pending' && <span className="qh-pulse" />}
      {label}
    </span>
  );
}

// ---------- Schema tree (SSMS-like: server → db → tables → columns/indexes) ----------
const TICN = {
  server: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="18" height="7" rx="1.6"/><rect x="3" y="13" width="18" height="7" rx="1.6"/><path d="M7 7.5h.01M7 16.5h.01"/></svg>,
  db: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"><ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3"/></svg>,
  folder: (o) => o
    ? <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2H5l-2 9z"/><path d="M3 18l2-9h17l-2 9a1 1 0 01-1 1H4a1 1 0 01-1-1z"/></svg>
    : <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2z"/></svg>,
  table: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M3 14h18M9 9v11M15 9v11"/></svg>,
  view: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="2.5"/></svg>,
  col: (c, pii) => pii
    ? <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><rect x="4" y="10" width="16" height="10" rx="2"/><path d="M8 10V7a4 4 0 018 0v3"/></svg>
    : c.pk
      ? <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><circle cx="8" cy="15" r="4"/><path d="M11 12l7-7 3 3M16 7l2 2"/></svg>
      : c.fk
        ? <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M10 13a5 5 0 007 0l3-3a5 5 0 00-7-7l-1 1"/><path d="M14 11a5 5 0 00-7 0l-3 3a5 5 0 007 7l1-1"/></svg>
        : <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="3"/></svg>,
  index: (u) => u
    ? <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><circle cx="8" cy="15" r="4"/><path d="M11 12l7-7 3 3M16 7l2 2"/></svg>
    : <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M4 6h16M4 12h16M4 18h10"/></svg>,
  run: () => <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round"><path d="M7 5v14l11-7z"/></svg>,
  roles: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75"/></svg>,
  role: (r) => r && r.kind === 'user'
    ? <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="8" r="4"/><path d="M4 21v-1a6 6 0 0112 0v1"/></svg>
    : <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="9" cy="8" r="3.2"/><circle cx="16.5" cy="9" r="2.6"/><path d="M3 20v-1a5 5 0 019-3M14.5 20v-1a4 4 0 016.5-3"/></svg>,
  // Disabled target. Icon-only inside a tree row: the word is 65px of uppercase
  // chip, which outranked the row's own name at every sidebar width. The glyph is
  // ~14px and the sentence lives in the row's hover title.
  off: () => <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round"><circle cx="12" cy="12" r="9"/><path d="M5.9 5.9l12.2 12.2"/></svg>,
  sys: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 11-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 11-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 110-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06A1.65 1.65 0 009 4.6h.09A1.65 1.65 0 0010 3.09V3a2 2 0 114 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06a1.65 1.65 0 00-.33 1.82V9a1.65 1.65 0 001.51 1H21a2 2 0 110 4h-.09a1.65 1.65 0 00-1.51 1z"/></svg>,
};

function TreeRow({ depth, expandable, open, onToggle, icon, label, sub, tier, right, active, muted, strong, pii, drag, connDrag, dbDrag, onClick, onCtx, onDbl, title, nodeId }) {
  const handle = onClick || (expandable ? onToggle : undefined);
  // Names ellipsize in a narrow sidebar, so every row hovers its own full name —
  // rows with something more to say (a db's server/host, a server's engine/env)
  // pass an explicit title that already opens with the name.
  const rowTitle = title || (typeof label === 'string' ? label : undefined);
  const draggable = !!drag || !!connDrag || !!dbDrag;
  // A database row in the flat view carries BOTH payloads: the SQL text (drop on
  // the editor inserts it) and its organizer key (drop on a group files it).
  const onDragStart = draggable
    ? (e) => {
      if (drag) e.dataTransfer.setData('text/plain', drag);
      if (connDrag) e.dataTransfer.setData('application/x-qh-conn', connDrag);
      if (dbDrag) e.dataTransfer.setData('application/x-qh-db', dbDrag);
      e.dataTransfer.effectAllowed = drag && (connDrag || dbDrag) ? 'copyMove' : (drag ? 'copy' : 'move');
    }
    : undefined;
  return (
    <div title={rowTitle} data-node={nodeId} className={'qh-tr' + (active ? ' is-active' : '') + (muted ? ' is-muted' : '') + (strong ? ' is-strong' : '') + (pii ? ' is-pii' : '') + (drag ? ' is-drag' : '') + (connDrag ? ' is-conn-drag' : '') + (sub ? ' is-two' : '')}
      style={{ paddingLeft: (6 + depth * 14) + 'px' }} onClick={handle} onContextMenu={onCtx} onDoubleClick={onDbl}
      draggable={draggable}
      onDragStart={onDragStart}>
      <span className="qh-tr-caret" onClick={expandable ? (e => { e.stopPropagation(); onToggle(); }) : undefined}>
        {expandable && <span className={'qh-caret' + (open ? ' is-open' : '')}><DBIcons.caret /></span>}
      </span>
      <span className="qh-tr-ic">{icon}</span>
      {/* A row that has to say WHERE it lives says it underneath, not beside:
          side by side, a 25-character server name and the database's own name
          fight over the same 200px and the name is what gets clipped. */}
      {sub
        ? <span className="qh-tr-main"><span className="qh-tr-label">{label}</span><span className="qh-db-srv">{sub}</span></span>
        : <span className="qh-tr-label">{label}</span>}
      {tier && <TierBadge tier={tier} sm />}
      {right}
    </div>
  );
}

function SchemaTree({ conns, schemaCache, onLoadSchema, rolesCache, onLoadRoles, active, open, setOpen, onPickDb, onOpenTable, onNewQuery, isSuper, reveal, narrow }) {
  const tog = (id) => setOpen(o => ({ ...o, [id]: !o[id] }));
  const isOpen = (id) => !!open[id];
  const [menu, setMenu] = React.useState(null);
  const openMenu = (e, c, db, table) => { e.preventDefault(); setMenu({ x: Math.min(e.clientX, window.innerWidth - 224), y: Math.min(e.clientY, window.innerHeight - 220), c, db, table }); };
  const newQ = (c, db) => { if (onNewQuery) onNewQuery(c, db); };

  // reveal a node found via search: scroll it into view within the sidebar and pulse it
  const treeRef = React.useRef(null);
  React.useEffect(() => {
    if (!reveal || !reveal.id) return;
    const root = treeRef.current; if (!root) return;
    const raf = requestAnimationFrame(() => {
      const el = root.querySelector('[data-node="' + reveal.id.replace(/"/g, '\\"') + '"]');
      if (!el) return;
      const sc = root.closest('.qh-side-body');
      if (sc) {
        const er = el.getBoundingClientRect(), sr = sc.getBoundingClientRect();
        if (er.top < sr.top + 8 || er.bottom > sr.bottom - 8) sc.scrollTop += (er.top - sr.top) - (sr.height / 2 - er.height / 2);
      }
      el.classList.remove('is-reveal'); void el.offsetWidth; el.classList.add('is-reveal');
      setTimeout(() => el.classList.remove('is-reveal'), 1600);
    });
    return () => cancelAnimationFrame(raf);
  }, [reveal]);

  // ----- user connection organization: favorites + folders (persisted) -----
  const [org, setOrg] = React.useState(() => { try { const o = JSON.parse(localStorage.getItem('qh.connorg.v1')); if (o && Array.isArray(o.favorites) && Array.isArray(o.folders)) return { favorites: o.favorites, folders: o.folders, dbFavorites: Array.isArray(o.dbFavorites) ? o.dbFavorites : [], dbFolders: Array.isArray(o.dbFolders) ? o.dbFolders : [] }; } catch (e) {} return { favorites: [], folders: [], dbFavorites: [], dbFolders: [] }; });
  React.useEffect(() => { try { localStorage.setItem('qh.connorg.v1', JSON.stringify(org)); } catch (e) {} }, [org]);
  // Server-grouped tree, or one flat list of every database the user can reach
  // (persisted). The flat view is for people who think in databases, not hosts:
  // which box a database sits on is an operational detail, so it moves to the
  // hover title — the endpoint is still one hover away when it matters.
  const [treeView, setTreeView] = React.useState(() => { try { return localStorage.getItem('qh.treeview.v1') === 'dbs' ? 'dbs' : 'servers'; } catch (e) { return 'servers'; } });
  React.useEffect(() => { try { localStorage.setItem('qh.treeview.v1', treeView); } catch (e) {} }, [treeView]);
  const [renaming, setRenaming] = React.useState(null);
  const [dropZone, setDropZone] = React.useState(null);
  const isFav = (id) => org.favorites.includes(id);
  const toggleFav = (id) => setOrg(o => ({ ...o, favorites: o.favorites.includes(id) ? o.favorites.filter(x => x !== id) : [...o.favorites, id] }));
  const folderOf = (id) => org.folders.find(f => f.conns.includes(id));
  const moveToFolder = (id, folderId) => setOrg(o => ({ ...o, folders: o.folders.map(f => ({ ...f, conns: f.id === folderId ? [...f.conns.filter(x => x !== id), id] : f.conns.filter(x => x !== id) })) }));
  const addFolder = (withConn) => { const id = 'fold_' + Date.now().toString(36); setOrg(o => ({ ...o, folders: [...o.folders, { id, name: 'New folder', conns: withConn ? [withConn] : [] }] })); setOpen(s => ({ ...s, ['grp:' + id]: true })); setRenaming(id); };
  const renameFolder = (id, name) => setOrg(o => ({ ...o, folders: o.folders.map(f => f.id === id ? { ...f, name: name || 'Folder' } : f) }));
  const deleteFolder = (id) => setOrg(o => ({ ...o, folders: o.folders.filter(f => f.id !== id) }));
  const gOpen = (k) => open[k] !== false;
  const gTog = (k) => setOpen(o => ({ ...o, [k]: !(o[k] !== false) }));
  const openConnMenu = (e, c) => { e.preventDefault(); setMenu({ x: Math.min(e.clientX, window.innerWidth - 236), y: Math.min(e.clientY, window.innerHeight - 320), conn: c }); };
  const onDropZone = (zone, id) => { if (zone === 'fav') { if (!isFav(id)) toggleFav(id); } else if (zone === 'all') { moveToFolder(id, null); if (isFav(id)) toggleFav(id); } else { moveToFolder(id, zone); } };
  // The database view files DATABASES, not servers — same gestures, own keys
  // (`connId/dbId`), so favouriting `analytics` on prod-main does not drag
  // prod-replica's `analytics` along with it.
  const dbKey = (c, db) => c.id + '/' + db.id;
  const isDbFav = (k) => org.dbFavorites.includes(k);
  const toggleDbFav = (k) => setOrg(o => ({ ...o, dbFavorites: o.dbFavorites.includes(k) ? o.dbFavorites.filter(x => x !== k) : [...o.dbFavorites, k] }));
  const dbFolderOf = (k) => org.dbFolders.find(f => f.dbs.includes(k));
  const moveDbToFolder = (k, folderId) => setOrg(o => ({ ...o, dbFolders: o.dbFolders.map(f => ({ ...f, dbs: f.id === folderId ? [...f.dbs.filter(x => x !== k), k] : f.dbs.filter(x => x !== k) })) }));
  const addDbFolder = (withDb) => { const id = 'dbf_' + Date.now().toString(36); setOrg(o => ({ ...o, dbFolders: [...o.dbFolders, { id, name: 'New folder', dbs: withDb ? [withDb] : [] }] })); setOpen(s => ({ ...s, ['grp:' + id]: true })); setRenaming(id); };
  const renameDbFolder = (id, name) => setOrg(o => ({ ...o, dbFolders: o.dbFolders.map(f => f.id === id ? { ...f, name: name || 'Folder' } : f) }));
  const deleteDbFolder = (id) => setOrg(o => ({ ...o, dbFolders: o.dbFolders.filter(f => f.id !== id) }));
  const onDropZoneDb = (zone, k) => { if (zone === 'dbfav') { if (!isDbFav(k)) toggleDbFav(k); } else if (zone === 'dball') { moveDbToFolder(k, null); if (isDbFav(k)) toggleDbFav(k); } else { moveDbToFolder(k, zone); } };
  const dz = (zone, kind) => {
    const type = kind === 'db' ? 'application/x-qh-db' : 'application/x-qh-conn';
    return {
      active: dropZone === zone,
      onDragOver: (e) => { if (Array.from(e.dataTransfer.types).includes(type)) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; if (dropZone !== zone) setDropZone(zone); } },
      onDragLeave: () => setDropZone(z => (z === zone ? null : z)),
      onDrop: (e) => { e.preventDefault(); const id = e.dataTransfer.getData(type); setDropZone(null); if (id) (kind === 'db' ? onDropZoneDb : onDropZone)(zone, id); },
    };
  };
  const renderGroup = ({ zone, kind, gk, icon, favHead, label, count, actions, empty, emptyHint, body }) => {
    const z = dz(zone, kind);
    return (
      <div key={gk} className={'qh-grp-wrap' + (z.active ? ' is-dropzone' : '')} onDragOver={z.onDragOver} onDragLeave={z.onDragLeave} onDrop={z.onDrop}>
        <div className="qh-grp" onDoubleClick={() => gTog(gk)}>
          <button className="qh-grp-toggle" onClick={() => gTog(gk)} aria-label={gOpen(gk) ? 'Collapse' : 'Expand'}>{gOpen(gk) ? '−' : '+'}</button>
          <span className={'qh-grp-ic' + (favHead ? ' is-fav' : '')}>{icon}</span>
          {label}
          {count != null && <span className="qh-grp-count">{count}</span>}
          {actions && <span className="qh-grp-actions">{actions}</span>}
        </div>
        {gOpen(gk) && (empty ? <div className={'qh-grp-empty' + (z.active ? ' is-dropzone' : '')}>{emptyHint}</div> : body)}
      </div>
    );
  };

  // One database subtree, rendered from `base` depth so the same code serves the
  // server-grouped tree (server ▸ db ▸ …) and the flat database list.
  const dbNode = (c, db, base, rightExtra, dbDragKey, sub) => {
    const sid = c.id;
    const eng = qhEngineId(c.engine);
    const qi = (x) => qhQuoteIdentFor(x, eng);
    const did = sid + '/' + db.id;
    const act = active && active.conn === c.id && active.db === db.id;
    const sch = schemaCache ? schemaCache[c.id + '/' + db.id] : null;
    const loaded = !!sch;
    const views = sch ? sch.views : [];
    // One entry per (schema, table). Iterating bare names collapsed two
    // same-named tables from different schemas into a single node (and a
    // duplicate React key) — real here: `crm` has 37 tables in `dba` and 35
    // in `public`.
    const refs = db.tableRefs || (db.tables || []).map(n => ({ s: qhSchemaFor(c, db), n }));
    const qq = (name, s) => qhQualify(c, db, name, s);
    return (
      <div key={did}>
        <TreeRow depth={base} expandable open={isOpen(did)} onToggle={() => { tog(did); onLoadSchema && onLoadSchema(c.id, db.id); }}
          icon={rightExtra !== undefined ? <img className="qh-engine-logo" src={qhEngineLogo(c)} alt={qhEngine(c).label} draggable={false} /> : TICN.db()}
          label={db.name} sub={sub} tier={db.tier} active={act} drag={qhQuoteIdent(db.name)} dbDrag={dbDragKey} nodeId={did}
          title={rightExtra !== undefined ? db.name + ' · ' + c.name + ' · ' + (c.host || c.name) + (c.port ? ':' + c.port : '') + ' · ' + c.engine : undefined}
          right={rightExtra}
          onCtx={(e) => openMenu(e, c, db)} onDbl={() => newQ(c, db)} />
        {isOpen(did) && (
          <>
            <TreeRow depth={base + 1} expandable open={isOpen(did + '/t')} onToggle={() => tog(did + '/t')} muted drag={refs.map(r => qq(r.n, r.s)).join(', ')}
              icon={TICN.folder(isOpen(did + '/t'))} label="Tables" right={<span className="qh-tr-count">{refs.length}</span>} />
            {isOpen(did + '/t') && refs.map(({ s: tsch, n: tb }) => {
              const tid = did + '/t/' + tsch + '.' + tb;
              const td = sch && (sch.tables[tsch + '.' + tb] || sch.tables[tb]) ? (sch.tables[tsch + '.' + tb] || sch.tables[tb]) : null;
              const cols = td ? td.columns : [];
              const idxs = td ? td.indexes : [];
              const approx = td ? td.approxRows : null;
              return (
                <div key={tid}>
                  <TreeRow depth={base + 2} expandable open={isOpen(tid)} onToggle={() => tog(tid)} icon={TICN.table()} label={tsch + '.' + tb} drag={qq(tb, tsch)} nodeId={tid}
                    onDbl={() => onOpenTable(c, db, tb)} onCtx={(e) => openMenu(e, c, db, tb)}
                    right={<>{approx != null && <span className="qh-tr-rows" title={approx.toLocaleString() + ' rows (est.)'}>{qhFmtRows(approx)}</span>}<button className="qh-tr-run" title="Open SELECT" onClick={(e) => { e.stopPropagation(); onOpenTable(c, db, tb); }}>{TICN.run()}</button></>} />
                  {isOpen(tid) && (
                    <>
                      <TreeRow depth={base + 3} expandable open={isOpen(tid + '/c')} onToggle={() => tog(tid + '/c')} muted drag={qhQuoteList(cols.map(cc => cc.name))} icon={TICN.folder(isOpen(tid + '/c'))} label="Columns" right={<span className="qh-tr-count">{loaded ? cols.length : '…'}</span>} />
                      {isOpen(tid + '/c') && cols.map(col => {
                        const cpii = !!col.pii;
                        return <TreeRow key={col.name} depth={base + 4} pii={cpii} muted drag={qhQuoteIdent(col.name)} nodeId={tid + '/c/' + col.name} icon={TICN.col(col, cpii)} label={col.name}
                          right={<span className="qh-col-type">{col.type}{col.nullable ? '' : ' ·nn'}</span>} />;
                      })}
                      <TreeRow depth={base + 3} expandable open={isOpen(tid + '/i')} onToggle={() => tog(tid + '/i')} muted icon={TICN.folder(isOpen(tid + '/i'))} label="Indexes" right={<span className="qh-tr-count">{loaded ? idxs.length : '…'}</span>} />
                      {isOpen(tid + '/i') && idxs.map(ix => (
                        <TreeRow key={ix.name} depth={base + 4} muted drag={qhQuoteIdent(ix.name)} icon={TICN.index(ix.unique)} label={ix.name}
                          right={<span className="qh-col-type">{(ix.cols || []).join(', ')}{ix.unique ? ' ·uniq' : ''}</span>} />
                      ))}
                    </>
                  )}
                </div>
              );
            })}
            {views.length > 0 && (
              <>
                <TreeRow depth={base + 1} expandable open={isOpen(did + '/v')} onToggle={() => tog(did + '/v')} muted drag={views.map(qq).join(', ')} icon={TICN.folder(isOpen(did + '/v'))} label="Views" right={<span className="qh-tr-count">{views.length}</span>} />
                {isOpen(did + '/v') && views.map(v => (
                  <TreeRow key={v} depth={base + 2} icon={TICN.view()} label={((sch && sch.tables[v] && sch.tables[v].schema) || qhSchemaFor(c, db)) + '.' + v} drag={qq(v)} onDbl={() => onOpenTable(c, db, v)} onCtx={(e) => openMenu(e, c, db, v)}
                    right={<button className="qh-tr-run" title="Open SELECT" onClick={(e) => { e.stopPropagation(); onOpenTable(c, db, v); }}>{TICN.run()}</button>} />
                ))}
              </>
            )}
            {isSuper && (
              <>
                <TreeRow depth={base + 1} expandable open={isOpen(did + '/sys')} onToggle={() => tog(did + '/sys')} muted icon={TICN.sys()} label={qhEngine(c).catalogLabel} />
                {isOpen(did + '/sys') && Object.entries(qhEngine(c).system).map(([grp, objs]) => (
                  <React.Fragment key={grp}>
                    <TreeRow depth={base + 2} expandable open={isOpen(did + '/sys/' + grp)} onToggle={() => tog(did + '/sys/' + grp)} muted icon={TICN.folder(isOpen(did + '/sys/' + grp))} label={grp} right={<span className="qh-tr-count">{objs.length}</span>} />
                    {isOpen(did + '/sys/' + grp) && objs.map(o => (
                      <TreeRow key={o} depth={base + 3} muted drag={qi(o)} icon={TICN.view()} label={o} />
                    ))}
                  </React.Fragment>
                ))}
              </>
            )}
          </>
        )}
      </div>
    );
  };

  const serverNode = (c) => {
        const sid = c.id;
        const eng = qhEngineId(c.engine);
        const qi = (x) => qhQuoteIdentFor(x, eng);
        const ql = (a) => (a || []).map(qi).join(', ');
        return (
          <div key={sid}>
            <TreeRow depth={0} expandable open={isOpen(sid)} onToggle={() => tog(sid)} strong
              title={c.name + ' · ' + c.engine + ' · ' + c.env + (c.disabled ? ' · disabled' : '')}
              icon={<img className="qh-engine-logo" src={qhEngineLogo(c)} alt={qhEngine(c).label} draggable={false} />} label={srvLabel(c)} connDrag={sid}
              // A disabled connection stays in an admin's list so their saved
              // queries and history can still resolve its alias — but it has to
              // SAY so, because the reason a target gets disabled is that nobody
              // should query it any more, and a retired instance still answers:
              // it just holds stale data. That is the accident this label exists
              // to prevent: a target retired the same day was still sitting in
              // the picker looking exactly like the live one.
              right={c.disabled
                ? <span className="qh-conn-off is-icon" aria-label="Disabled" title="Disabled — retired or parked. It would still answer, with stale data. Pick it only deliberately.">{TICN.off()}</span>
                : (QH_SHOW_ENV_TAGS
                    ? <span className={'qh-envtag-sm env-' + c.env}>{c.env === 'production' ? 'PROD' : c.env === 'staging' ? 'STG' : c.env}</span>
                    : null)}
              onCtx={(e) => openConnMenu(e, c)} onDbl={() => { newQ(c, c.databases[0]); }} />
            {isOpen(sid) && (<>
            {c.databases.map(db => dbNode(c, db, 1))}
            {isSuper && (
              <>
                <TreeRow depth={1} expandable open={isOpen(sid + '/roles')} onToggle={() => { tog(sid + '/roles'); onLoadRoles && onLoadRoles(c.id); }} muted icon={TICN.roles()} label={qhEngine(c).rolesLabel} right={<span className="qh-tr-count">{rolesCache && rolesCache[c.id] ? rolesCache[c.id].length : '…'}</span>} />
                {isOpen(sid + '/roles') && ((rolesCache && rolesCache[c.id]) || []).map(role => (
                  <TreeRow key={role.name} depth={2} muted drag={qi(role.name)} icon={TICN.role(role)} label={role.name}
                    right={<span className="qh-role-note">{role.sup ? 'superuser' : role.note}{role.login ? '' : ' · nologin'}</span>} />
                ))}
              </>
            )}
            </>)}
          </div>
        );
  };

  // The head every target in the fleet shares (`svc-prod-`) is dimmed, and
  // hidden outright while the sidebar is narrow — see qhFleetPrefixes.
  const foldPre = React.useMemo(() => qhFleetPrefixes(conns), [conns]);
  const srvLabel = (c) => { const sp = qhSplitName(c.name, foldPre); return sp[0] ? <><span className="qh-name-pre">{sp[0]}</span>{sp[1]}</> : c.name; };

  const favServers = conns.filter(c => isFav(c.id));
  const ungrouped = conns.filter(c => !folderOf(c.id));
  // Every database the caller can reach. The server decides what is in this
  // list — GET /connections already returns only granted targets.
  const flatDbs = React.useMemo(() => {
    const rows = [];
    (conns || []).forEach(c => (c.databases || []).forEach(db => rows.push({ c, db })));
    rows.sort((a, b) => a.db.name.localeCompare(b.db.name) || a.c.name.localeCompare(b.c.name));
    return rows;
  }, [conns]);
  // Two servers can hold a database of the same name (users on prod-main and on
  // prod-replica). Pretending they are one row would be a lie, so those rows
  // carry the server name; the unambiguous ones stay clean.
  const dupDbNames = React.useMemo(() => {
    const seen = {}, dup = new Set();
    flatDbs.forEach(({ db }) => { if (seen[db.name]) dup.add(db.name); seen[db.name] = 1; });
    return dup;
  }, [flatDbs]);
  const dbRow = (row) => dbNode(row.c, row.db, 0, (
    row.c.disabled
      ? <span className="qh-conn-off is-icon" aria-label="Disabled" title="Disabled — retired or parked. It would still answer, with stale data. Pick it only deliberately.">{TICN.off()}</span>
      : null
  ), dbKey(row.c, row.db), dupDbNames.has(row.db.name) ? srvLabel(row.c) : null);
  const favDbs = flatDbs.filter(r => isDbFav(dbKey(r.c, r.db)));
  const ungroupedDbs = flatDbs.filter(r => !dbFolderOf(dbKey(r.c, r.db)));
  const ICN_STAR = <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round"><path d="M12 3l2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9 6.8 19.2l1-5.8L3.5 9.2l5.9-.9z"/></svg>;
  const ICN_RENAME = <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/></svg>;
  const ICN_FOLDER_DEL = <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18M8 6V4a1 1 0 011-1h6a1 1 0 011 1v2M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg>;
  const ICN_FOLDER_NEW = <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2z"/><path d="M12 11v6M9 14h6"/></svg>;

  return (
    <div className={'qh-tree' + (narrow ? ' is-narrow' : '')} ref={treeRef}>
      <div className="qh-treeview" role="group" aria-label="Browse by">
        <button className={treeView === 'servers' ? 'is-on' : ''} onClick={() => setTreeView('servers')} aria-label="Server view">
          {TICN.server()}<span>Server view</span>
        </button>
        <button className={treeView === 'dbs' ? 'is-on' : ''} onClick={() => setTreeView('dbs')} aria-label="Database view">
          {TICN.db()}<span>Database view</span>
        </button>
        <span className="qh-treeview-n">{treeView === 'dbs' ? flatDbs.length + ' databases' : conns.length + ' servers'}</span>
      </div>

      {treeView === 'dbs' ? (<>
      {renderGroup({ kind: 'db', zone: 'dbfav', gk: 'grp:dbfav', favHead: true, icon: ICN_STAR,
        label: <span className="qh-grp-label">Favorites</span>, count: favDbs.length || null,
        empty: favDbs.length === 0, emptyHint: 'Drag a database here, or right-click → Add to favorites.',
        body: favDbs.map(dbRow) })}

      {org.dbFolders.map(f => {
        const list = flatDbs.filter(r => f.dbs.includes(dbKey(r.c, r.db)));
        return renderGroup({ kind: 'db', zone: f.id, gk: 'grp:' + f.id,
          icon: TICN.folder(gOpen('grp:' + f.id)),
          label: renaming === f.id
            ? <input className="qh-grp-rename" autoFocus defaultValue={f.name} onClick={e => e.stopPropagation()}
                onKeyDown={e => { if (e.key === 'Enter') { renameDbFolder(f.id, e.target.value.trim()); setRenaming(null); } else if (e.key === 'Escape') setRenaming(null); }}
                onBlur={e => { renameDbFolder(f.id, e.target.value.trim()); setRenaming(null); }} />
            : <span className="qh-grp-label">{f.name}</span>,
          count: renaming === f.id ? null : (list.length || null),
          actions: (<>
            <button className="qh-grp-act" title="Rename" onClick={(e) => { e.stopPropagation(); setRenaming(f.id); }}>{ICN_RENAME}</button>
            <button className="qh-grp-act" title="Delete folder" onClick={(e) => { e.stopPropagation(); deleteDbFolder(f.id); }}>{ICN_FOLDER_DEL}</button>
          </>),
          empty: list.length === 0, emptyHint: 'Empty — drag databases here.',
          body: list.map(dbRow) });
      })}

      {renderGroup({ kind: 'db', zone: 'dball', gk: 'grp:alldbs',
        icon: TICN.db(), label: <span className="qh-grp-label">All databases</span>, count: ungroupedDbs.length || null,
        actions: (<button className="qh-grp-act" title="New folder" onClick={(e) => { e.stopPropagation(); addDbFolder(); }}>{ICN_FOLDER_NEW}</button>),
        empty: ungroupedDbs.length === 0, emptyHint: 'Every database is filed in a folder above.',
        body: ungroupedDbs.map(dbRow) })}
      </>) : (<>
      {renderGroup({ zone: 'fav', gk: 'grp:fav', favHead: true,
        icon: <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round"><path d="M12 3l2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9 6.8 19.2l1-5.8L3.5 9.2l5.9-.9z"/></svg>,
        label: <span className="qh-grp-label">Favorites</span>, count: favServers.length || null,
        empty: favServers.length === 0, emptyHint: 'Drag a connection here, or right-click → Add to favorites.',
        body: favServers.map(serverNode) })}

      {org.folders.map(f => {
        const list = conns.filter(c => f.conns.includes(c.id));
        return renderGroup({ zone: f.id, gk: 'grp:' + f.id,
          icon: TICN.folder(gOpen('grp:' + f.id)),
          label: renaming === f.id
            ? <input className="qh-grp-rename" autoFocus defaultValue={f.name} onClick={e => e.stopPropagation()}
                onKeyDown={e => { if (e.key === 'Enter') { renameFolder(f.id, e.target.value.trim()); setRenaming(null); } else if (e.key === 'Escape') setRenaming(null); }}
                onBlur={e => { renameFolder(f.id, e.target.value.trim()); setRenaming(null); }} />
            : <span className="qh-grp-label">{f.name}</span>,
          count: renaming === f.id ? null : (list.length || null),
          actions: (<>
            <button className="qh-grp-act" title="Rename" onClick={(e) => { e.stopPropagation(); setRenaming(f.id); }}><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/></svg></button>
            <button className="qh-grp-act" title="Delete folder" onClick={(e) => { e.stopPropagation(); deleteFolder(f.id); }}><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18M8 6V4a1 1 0 011-1h6a1 1 0 011 1v2M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg></button>
          </>),
          empty: list.length === 0, emptyHint: 'Empty — drag connections here.',
          body: list.map(serverNode) });
      })}

      {renderGroup({ zone: 'all', gk: 'grp:all',
        icon: TICN.server(), label: <span className="qh-grp-label">All connections</span>, count: ungrouped.length || null,
        actions: (<button className="qh-grp-act" title="New folder" onClick={(e) => { e.stopPropagation(); addFolder(); }}><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2z"/><path d="M12 11v6M9 14h6"/></svg></button>),
        empty: ungrouped.length === 0, emptyHint: 'Every connection is filed in a folder above.',
        body: ungrouped.map(serverNode) })}
      </>)}

      {menu && (
        <>
          <div className="qh-ctx-backdrop" onClick={() => setMenu(null)} onContextMenu={(e) => { e.preventDefault(); setMenu(null); }} />
          <div className={'qh-ctxmenu' + (menu.table ? ' qh-ctxmenu-wide' : '')} style={{ left: menu.x, top: menu.y }}>
            {menu.table ? (
              <>
                <div className="qh-ctx-title">{menu.table}</div>
                <button onClick={() => { onOpenTable(menu.c, menu.db, menu.table, { limit: 100 }); setMenu(null); }}>{TICN.table()}Select top 100 rows</button>
                <div className="qh-ctx-sep" />
                <button onClick={() => { onOpenTable(menu.c, menu.db, menu.table, { mode: 'count' }); setMenu(null); }}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M4 9h16M4 15h16M10 3 8 21M16 3l-2 18"/></svg>Select row count
                </button>
                <div className="qh-ctx-sep" />
                <button onClick={() => { newQ(menu.c, menu.db); setMenu(null); }}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M12 5v14M5 12h14"/></svg>New query here
                </button>
                <button onClick={() => { qhCopyText(qhQualify(menu.c, menu.db, menu.table)); setMenu(null); }}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>Copy name
                </button>
              </>
            ) : menu.conn ? (
              <>
                <div className="qh-ctx-title">{menu.conn.name}</div>
                <button onClick={() => { newQ(menu.conn, menu.conn.databases[0]); setMenu(null); }}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M12 5v14M5 12h14"/></svg>New query
                </button>
                <button onClick={() => { onPickDb(menu.conn, menu.conn.databases[0]); setMenu(null); }}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M9 11l3 3L22 4M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/></svg>Set as target
                </button>
                <div className="qh-ctx-sep" />
                <button onClick={() => { toggleFav(menu.conn.id); setMenu(null); }}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill={isFav(menu.conn.id) ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round"><path d="M12 3l2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9 6.8 19.2l1-5.8L3.5 9.2l5.9-.9z"/></svg>{isFav(menu.conn.id) ? 'Remove from favorites' : 'Add to favorites'}
                </button>
                <div className="qh-ctx-sep" />
                <div className="qh-ctx-label">Move to folder</div>
                {org.folders.map(f => { const here = f.conns.includes(menu.conn.id); return (
                  <button key={f.id} disabled={here} onClick={() => { moveToFolder(menu.conn.id, f.id); setMenu(null); }}>
                    <span className="qh-tr-ic">{TICN.folder(false)}</span>{f.name}{here && <span className="qh-ck">{'✓'}</span>}
                  </button>
                ); })}
                <button onClick={() => { addFolder(menu.conn.id); setMenu(null); }}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2z"/><path d="M12 11v6M9 14h6"/></svg>New folder…
                </button>
                {folderOf(menu.conn.id) && (
                  <button onClick={() => { moveToFolder(menu.conn.id, null); setMenu(null); }}>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>Remove from folder
                  </button>
                )}
              </>
            ) : (
              <>
                <div className="qh-ctx-title">{menu.c.name}{menu.db ? '/' + menu.db.name : ''}</div>
                <button onClick={() => { newQ(menu.c, menu.db); setMenu(null); }}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M12 5v14M5 12h14"/></svg>New query
                </button>
                <button onClick={() => { onPickDb(menu.c, menu.db); setMenu(null); }}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M9 11l3 3L22 4M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/></svg>Set as target
                </button>
                {treeView === 'dbs' && menu.db && (() => {
                  const k = dbKey(menu.c, menu.db);
                  return (<>
                    <div className="qh-ctx-sep" />
                    <button onClick={() => { toggleDbFav(k); setMenu(null); }}>
                      <svg width="14" height="14" viewBox="0 0 24 24" fill={isDbFav(k) ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round"><path d="M12 3l2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9 6.8 19.2l1-5.8L3.5 9.2l5.9-.9z"/></svg>{isDbFav(k) ? 'Remove from favorites' : 'Add to favorites'}
                    </button>
                    <div className="qh-ctx-sep" />
                    <div className="qh-ctx-label">Move to folder</div>
                    {org.dbFolders.map(f => { const here = f.dbs.includes(k); return (
                      <button key={f.id} disabled={here} onClick={() => { moveDbToFolder(k, f.id); setMenu(null); }}>
                        <span className="qh-tr-ic">{TICN.folder(false)}</span>{f.name}{here && <span className="qh-ck">{'✓'}</span>}
                      </button>
                    ); })}
                    <button onClick={() => { addDbFolder(k); setMenu(null); }}>{ICN_FOLDER_NEW}New folder…</button>
                    {dbFolderOf(k) && (
                      <button onClick={() => { moveDbToFolder(k, null); setMenu(null); }}>
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>Remove from folder
                      </button>
                    )}
                  </>);
                })()}
              </>
            )}
          </div>
        </>
      )}
    </div>
  );
}

// ---------- Sidebar ----------
function qhAgo(ts) {
  if (!ts) return 'just now';
  const s = Math.floor((Date.now() - ts) / 1000);
  if (s < 60) return 'just now';
  const m = Math.floor(s / 60); if (m < 60) return m + 'm ago';
  const h = Math.floor(m / 60); if (h < 24) return h + 'h ago';
  return Math.floor(h / 24) + 'd ago';
}
const ICN_TRASH = <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18M8 6V4a1 1 0 011-1h6a1 1 0 011 1v2M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6M10 11v6M14 11v6"/></svg>;
function OriginBadge({ dest }) {
  const local = dest === 'local';
  return (
    <span className={'qh-origin ' + (local ? 'is-local' : 'is-server')}>
      {local
        ? <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="4" width="20" height="14" rx="2"/><path d="M8 21h8"/></svg>
        : <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="3" width="20" height="8" rx="2"/><rect x="2" y="13" width="20" height="8" rx="2"/></svg>}
      {local ? 'Local' : 'Synced'}
    </span>
  );
}

function Sidebar({ mode, setMode, conns, schemaCache, onLoadSchema, rolesCache, onLoadRoles, active, onPick, saved, onLoadSaved, onDeleteSaved, sessions, onSaveSession, onRestoreSession, onDeleteSession, scheduled, onOpenScheduled, onCancelScheduled, history, onLoadHistory, collapsed, onRequestEndpoint, onOpenTable, onNewQuery, onNewTab, onOpenSqlFile, onDownloadSql, canDownloadSql, isSuper, width, onResizerDown, onResizerFit }) {
  const [open, setOpen] = React.useState(() => ({ 'prod-main': true, 'prod-replica': true }));
  const [q, setQ] = React.useState('');
  const sqlFileRef = React.useRef(null);
  const onSqlFileChosen = (e) => {
    const f = e.target.files && e.target.files[0];
    if (f) { const r = new FileReader(); r.onload = () => onOpenSqlFile && onOpenSqlFile(f.name, String(r.result || '')); r.readAsText(f); }
    e.target.value = '';
  };
  const [hi, setHi] = React.useState(0);
  const [focused, setFocused] = React.useState(false);
  const [reveal, setReveal] = React.useState(null);
  const modes = [['conns', DBIcons.tree], ['saved', DBIcons.star], ['sessions', DBIcons.layers], ['scheduled', DBIcons.calendar], ['history', DBIcons.clock]];

  // flat searchable list of conn/db pairs
  const flat = [];
  for (const c of conns) for (const db of c.databases) flat.push({ c, db });
  const term = q.trim().toLowerCase();
  const isTierTerm = ['ro', 'rw', 'ddl'].includes(term);
  const matches = React.useMemo(() => {
    if (!term) return [];
    const out = [];
    for (const { c, db } of flat) {
      if (c.name.toLowerCase().includes(term) || db.name.toLowerCase().includes(term) || c.engine.toLowerCase().includes(term) || (isTierTerm && db.tier.toLowerCase() === term))
        out.push({ kind: 'db', c, db });
    }
    if (term.length >= 2 && !isTierTerm) {
      for (const { c, db } of flat) for (const tb of (db.tables || [])) { if (tb.toLowerCase().includes(term)) out.push({ kind: 'table', c, db, table: tb }); if (out.length > 40) break; }
      for (const { c, db } of flat) {
        const sch = schemaCache && schemaCache[c.id + '/' + db.id];
        if (!sch) continue;
        for (const tb of Object.keys(sch.tables)) { for (const col of (sch.tables[tb].columns || [])) if (col.name.toLowerCase().includes(term)) out.push({ kind: 'column', c, db, table: tb, col: col.name, pii: !!col.pii }); if (out.length > 60) break; }
        if (out.length > 60) break;
      }
    }
    return out.slice(0, 30);
  }, [term, schemaCache]);

  const choose = (m) => {
    setQ(''); setHi(0);
    const sid = m.c.id, did = sid + '/' + m.db.id;
    const exp = { [sid]: true };
    let id = did;
    if (m.kind === 'db') {
      // a direct DB match opens a fresh query targeting that DB
      if (onNewQuery) onNewQuery(m.c, m.db);
    } else {
      exp[did] = true; exp[did + '/t'] = true;
      const tid = did + '/t/' + m.table;
      if (m.kind === 'column') { exp[tid] = true; exp[tid + '/c'] = true; id = tid + '/c/' + m.col; }
      else id = tid;
      if (onOpenTable) onOpenTable(m.c, m.db, m.table);
    }
    setOpen(o => ({ ...o, ...exp }));
    setReveal({ id, n: Date.now() });
  };
  const onSearchKey = (e) => {
    if (!matches.length) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); setHi(h => (h + 1) % matches.length); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setHi(h => (h - 1 + matches.length) % matches.length); }
    else if (e.key === 'Enter') { e.preventDefault(); choose(matches[Math.min(hi, matches.length - 1)]); }
    else if (e.key === 'Escape') { setQ(''); setFocused(false); }
  };

  const showDrop = focused && term && matches.length > 0;

  return (
    <div className={'qh-side' + (collapsed ? ' is-collapsed' : '')} style={width ? { width: width + 'px' } : undefined}>
      {onResizerDown && <div className="qh-side-resizer" onMouseDown={onResizerDown} onDoubleClick={onResizerFit} title="Drag to resize · double-click to fit the widest name" />}
      <div className="qh-side-switch">
        {modes.map(([m, Icon]) => (
          <button key={m} className={'qh-side-tab' + (mode === m ? ' is-active' : '')} onClick={() => setMode(m)} aria-label={m === 'conns' ? 'Connections' : m === 'saved' ? 'Saved' : m === 'sessions' ? 'Sessions' : m === 'scheduled' ? 'Scheduled' : 'History'}>
            <Icon /><span>{m === 'conns' ? 'Connections' : m === 'saved' ? 'Saved' : m === 'sessions' ? 'Sessions' : m === 'scheduled' ? 'Scheduled' : 'History'}</span>
          </button>
        ))}
      </div>

      {mode === 'conns' && (
        <div className="qh-side-top qh-side-top-q">
          <button className="qh-newq-btn" data-kbd={(window.QH_KBD || {}).newq} onClick={onNewTab}>
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 5v14M5 12h14"/></svg>
            New query
          </button>
          <button className="qh-newq-sq" onClick={() => sqlFileRef.current && sqlFileRef.current.click()} title="Open a .sql file from your computer" aria-label="Open .sql file">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M6 14l1.45-2.9A2 2 0 019.24 10H21a1 1 0 01.95 1.32l-2.1 6.3A2 2 0 0117.95 19H4a2 2 0 01-2-2V5a2 2 0 012-2h3.93a2 2 0 011.66.9l.82 1.2a2 2 0 001.66.9H18a2 2 0 012 2v2"/></svg>
          </button>
          <button className="qh-newq-sq" disabled={!canDownloadSql} onClick={() => canDownloadSql && onDownloadSql && onDownloadSql()} title="Save the current query as a .sql file" aria-label="Save .sql">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>
          </button>
          <input ref={sqlFileRef} type="file" accept=".sql,text/plain,text/sql" style={{ display: 'none' }} onChange={onSqlFileChosen} />
        </div>
      )}

      {mode === 'conns' && (
        <div className="qh-search-wrap">
          <div className={'qh-search' + (focused ? ' is-focused' : '')}>
            <svg className="qh-search-ic" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>
            <input
              className="qh-search-in"
              placeholder="Search server, database, table, column…"
              value={q}
              onChange={(e) => { setQ(e.target.value); setHi(0); setFocused(true); }}
              onFocus={() => setFocused(true)}
              onBlur={() => setTimeout(() => setFocused(false), 120)}
              onKeyDown={onSearchKey}
              spellCheck={false}
            />
            {q && <button className="qh-search-x" onMouseDown={(e) => { e.preventDefault(); setQ(''); }} aria-label="Clear">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
            </button>}
          </div>
          {showDrop && (
            <div className="qh-ac">
              {matches.map((m, i) => (
                <button key={m.kind + m.c.id + m.db.id + (m.table || '') + (m.col || '')} className={'qh-ac-opt' + (i === hi ? ' is-hi' : '')}
                  onMouseEnter={() => setHi(i)} onMouseDown={(e) => { e.preventDefault(); choose(m); }}>
                  {m.kind === 'db' && <><img className="qh-engine-logo qh-ac-logo" src={qhEngineLogo(m.c)} alt="" draggable={false} /><span className="qh-ac-text"><b>{m.c.name}</b><span className="qh-ac-slash">/</span>{m.db.name}</span>{m.c.disabled ? <span className="qh-conn-off">disabled</span> : (QH_SHOW_ENV_TAGS ? <span className={'qh-envtag-sm env-' + m.c.env}>{m.c.env === 'production' ? 'PROD' : m.c.env === 'staging' ? 'STG' : m.c.env}</span> : null)}<TierBadge tier={m.db.tier} sm /></>}
                  {m.kind === 'table' && <><span className="qh-ac-ic">{TICN.table()}</span><span className="qh-ac-text">{qhSchemaFor(m.c, m.db) + '.' + m.table}<span className="qh-ac-loc">{m.c.name}/{m.db.name}</span></span><span className="qh-ac-kind">table</span></>}
                  {m.kind === 'column' && <><span className="qh-ac-ic">{TICN.col({}, !!m.pii)}</span><span className="qh-ac-text">{m.col}<span className="qh-ac-loc">{m.db.name}.{m.table}</span></span><span className="qh-ac-kind">col</span></>}
                </button>
              ))}
            </div>
          )}
          {focused && term && matches.length === 0 && (
            <div className="qh-ac"><div className="qh-ac-none">No match for “{q}”</div></div>
          )}
        </div>
      )}

      <div className="qh-side-body">
        {mode === 'conns' && <SchemaTree conns={conns} schemaCache={schemaCache} onLoadSchema={onLoadSchema} rolesCache={rolesCache} onLoadRoles={onLoadRoles} active={active} open={open} setOpen={setOpen} onPickDb={onPick} onOpenTable={onOpenTable} onNewQuery={onNewQuery} isSuper={isSuper} reveal={reveal} narrow={!width || width < 330} />}

        {mode === 'saved' && saved.length === 0 && (
          <div className="qh-side-empty">
            <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M12 17.3l-6.2 3.3 1.2-6.9L2 8.9l7-1L12 1.5l3 6.4 7 1-5 4.8 1.2 6.9z"/></svg>
            <div className="qh-side-empty-title">No saved queries yet</div>
            <div className="qh-side-empty-sub">Save one from a tab's right-click menu, or when closing a tab with unsaved edits.</div>
          </div>
        )}
        {mode === 'saved' && saved.map(s => (
          <div key={s.id} className="qh-saved" onClick={() => onLoadSaved(s)} title="Open in a new tab">
            <div className="qh-saved-name">{s.name}</div>
            <div className="qh-saved-meta"><OriginBadge dest={s.dest} /><span>{s.conn} · {s.db}</span></div>
            <button className="qh-row-del" onClick={(e) => { e.stopPropagation(); onDeleteSaved(s.id); }} title="Delete" aria-label="Delete saved query">{ICN_TRASH}</button>
          </div>
        ))}

        {mode === 'sessions' && (
          <div className="qh-side-top">
            <button className="qh-newq-btn" onClick={onSaveSession}>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M19 21H5a2 2 0 01-2-2V5a2 2 0 012-2h11l5 5v11a2 2 0 01-2 2z"/><path d="M17 21v-8H7v8M7 3v5h8"/></svg>
              Save current workspace
            </button>
          </div>
        )}
        {mode === 'sessions' && sessions.length === 0 && (
          <div className="qh-side-empty">
            <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2 2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>
            <div className="qh-side-empty-title">No saved sessions</div>
            <div className="qh-side-empty-sub">Save all open tabs as one named workspace, then restore it anytime — here or on another device.</div>
          </div>
        )}
        {mode === 'sessions' && sessions.map(s => (
          <div key={s.id} className="qh-saved" onClick={() => onRestoreSession(s)} title="Open all tabs from this workspace">
            <div className="qh-saved-name">{s.name}</div>
            <div className="qh-saved-meta"><OriginBadge dest={s.dest} /><span>{s.tabs.length} tab{s.tabs.length > 1 ? 's' : ''} · {qhAgo(s.savedAt)}</span></div>
            <button className="qh-row-del" onClick={(e) => { e.stopPropagation(); onDeleteSession(s.id); }} title="Delete" aria-label="Delete session">{ICN_TRASH}</button>
          </div>
        ))}

        {mode === 'scheduled' && (scheduled || []).length === 0 && (
          <div className="qh-side-empty">
            <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="18" height="17" rx="2"/><path d="M3 9h18M8 2v4M16 2v4M12 13v3l2 1"/></svg>
            <div className="qh-side-empty-title">No scheduled queries</div>
            <div className="qh-side-empty-sub">Use <b>Schedule</b> on a query — a preset or a custom date &amp; time — and it appears here until it runs.</div>
          </div>
        )}
        {mode === 'scheduled' && (scheduled || []).map(s => (
          <div key={s.id} className="qh-saved" onClick={() => onOpenScheduled(s)} title="Open this query in a new tab">
            <div className="qh-saved-name">{s.name}</div>
            <div className="qh-saved-meta"><span className="qh-sched-chip"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>{s.when}</span><span>{s.conn} · {s.db}</span></div>
            <button className="qh-row-del" onClick={(e) => { e.stopPropagation(); onCancelScheduled(s.id); }} title="Cancel schedule" aria-label="Cancel schedule">{ICN_TRASH}</button>
          </div>
        ))}

        {mode === 'history' && history.map(h => (
          <button key={h.id} className="qh-hist" onClick={() => onLoadHistory(h)}>
            <div className="qh-hist-top">
              <TierBadge tier={h.tier} sm />
              <StatusPill status={h.status} />
              <span className="qh-hist-when">{h.when}</span>
            </div>
            <div className="qh-hist-sql">{h.sql}</div>
            <div className="qh-hist-meta">{h.conn} · {h.db}{h.approver ? ' · ' + h.approver : ''}</div>
          </button>
        ))}
      </div>

      {mode === 'conns' && (
        <div className="qh-side-foot">
          <button className="qh-req-btn" onClick={onRequestEndpoint}>
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 5v14M5 12h14"/></svg>
            Request new endpoint
          </button>
        </div>
      )}
    </div>
  );
}

// ---------- Request-endpoint modal ----------
function RequestEndpointModal({ onClose, onSubmit }) {
  const [server, setServer] = React.useState('');
  const [database, setDatabase] = React.useState('');
  const [tier, setTier] = React.useState('RO');
  const [reason, setReason] = React.useState('');
  const valid = server.trim() && database.trim() && reason.trim();

  return (
    <QhModal onClose={onClose}>
      <div className="qh-modal-head">
        <div>
          <div className="qh-modal-title">Request a new endpoint</div>
          <div className="qh-modal-sub">Goes to the DBA team in Slack for review. You'll get a DM when it's provisioned.</div>
        </div>
        <button className="qh-icon-btn" onClick={onClose} aria-label="Close">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
        </button>
      </div>
      <div className="qh-modal-body">
        <label className="qh-field">
          <span className="qh-field-lbl">Server / host</span>
          <input className="qh-input" placeholder="e.g. prod-reporting-01" value={server} onChange={(e) => setServer(e.target.value)} />
        </label>
        <label className="qh-field">
          <span className="qh-field-lbl">Database name</span>
          <input className="qh-input" placeholder="e.g. ledger" value={database} onChange={(e) => setDatabase(e.target.value)} />
        </label>
        <div className="qh-field">
          <span className="qh-field-lbl">Access tier</span>
          <div className="qh-seg">
            {[['RO', 'Read-only'], ['RW', 'Read/Write'], ['DDL', 'Schema']].map(([v, l]) => (
              <button key={v} className={'qh-seg-opt' + (tier === v ? ' is-active' : '')} onClick={() => setTier(v)}>
                <TierBadge tier={v} sm />{l}
              </button>
            ))}
          </div>
        </div>
        <label className="qh-field">
          <span className="qh-field-lbl">Reason for access</span>
          <textarea className="qh-input qh-textarea" rows="3" placeholder="What do you need this for? Helps the DBA approve faster." value={reason} onChange={(e) => setReason(e.target.value)} />
        </label>
      </div>
      <div className="qh-modal-foot">
        <button className="qh-btn qh-btn-ghost" onClick={onClose}>Cancel</button>
        <button className="qh-btn qh-btn-primary is-approval" disabled={!valid} onClick={() => onSubmit({ server, database, tier, reason })}>
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 2 11 13M22 2l-7 20-4-9-9-4z"/></svg>
          Send request
        </button>
      </div>
    </QhModal>
  );
}

// ---------- Bottom results panel ----------
function ResultsPanel({ tab, setTab, result, messages, audit, status, runMs, onExport, plan, onToast, colMeta, reqId }) {
  const [exp, setExp] = React.useState(false);
  // Click-away / Escape, not mouse-out: the 6px gap between the button and the
  // menu used to close it mid-reach. See qhUseDismiss.
  const closeExp = React.useCallback(() => setExp(false), []);
  const expRef = qhUseDismiss(exp, closeExp);
  const tabs = [['results', 'Results'], ['plan', 'Plan'], ['messages', 'Messages'], ['audit', 'Audit log']];
  const total = result && result.kind === 'table' ? result.total : null;

  return (
    <div className="qh-results">
      <div className="qh-res-head">
        <div className="qh-res-tabs">
          {tabs.map(([id, label]) => (
            <button key={id} className={'qh-res-tab' + (tab === id ? ' is-active' : '')} onClick={() => setTab(id)}>
              {label}
              {id === 'messages' && messages.length > 0 && <span className="qh-res-count">{messages.length}</span>}
              {id === 'plan' && plan && plan.hints && plan.hints.some(h => h.level === 'high') && <span className="qh-res-dot" />}
            </button>
          ))}
        </div>
        <div className="qh-res-actions">
          {reqId && <span className="qh-res-req" title="Request id — reserved when this tab opened">#{reqId}</span>}
          {status && <StatusPill status={status} />}
          {runMs != null && <span className="qh-res-time">{runMs} ms</span>}
          {total != null && <span className="qh-res-rows">{total.toLocaleString()} rows</span>}
          {result && result.kind === 'table' && (
            <>
              <button className="qh-btn qh-btn-ghost qh-btn-sm" title="Copy the whole result as CSV to the clipboard" onClick={() => onExport('copy-csv')}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
                Copy CSV
              </button>
              <div className="qh-export" ref={expRef}>
                <button className="qh-btn qh-btn-ghost qh-btn-sm" onClick={() => setExp(e => !e)}
                        aria-haspopup="menu" aria-expanded={exp}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>
                  Export
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><path d="M6 9l6 6 6-6"/></svg>
                </button>
                {exp && (
                  <div className="qh-export-menu">
                    <button onClick={() => { setExp(false); onExport('csv'); }}>Download CSV (.csv)</button>
                    <button onClick={() => { setExp(false); onExport('xlsx'); }}>Download Excel (.xls)</button>
                    <div className="qh-ctx-sep" />
                    <button onClick={() => { setExp(false); onExport('copy-csv'); }}>Copy all as CSV</button>
                    <button onClick={() => { setExp(false); onExport('copy-tsv'); }}>Copy all as TSV</button>
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </div>

      <div className="qh-res-body">
        {tab === 'results' && <ResultsView result={result} status={status} onToast={onToast} colMeta={colMeta} />}
        {tab === 'plan' && <PlanView plan={plan} />}
        {tab === 'messages' && <MessagesView messages={messages} />}
        {tab === 'audit' && <AuditView audit={audit} />}
      </div>
    </div>
  );
}

function PlanView({ plan }) {
  if (!plan) return <div className="qh-empty"><DBIcons.play /><div>Press Explain to preview the plan and risk hints.</div><div className="qh-empty-hint">No execution — static analysis only</div></div>;
  const { plan: p, hints } = plan;
  return (
    <div className="qh-planwrap">
      <div className="qh-plan-hints">
        {hints.map((h, i) => (
          <div key={i} className={'qh-hint risk-' + h.level}>
            <span className="qh-hint-dot" />
            <span className="qh-hint-lvl">{h.level === 'high' ? 'High' : h.level === 'med' ? 'Medium' : 'OK'}</span>
            <span className="qh-hint-text">{h.text}</span>
          </div>
        ))}
      </div>
      <div className="qh-plan-tree">
        <div className="qh-plan-meta">Planning: {p.planningMs} ms · est. {p.rows.toLocaleString()} rows · {p.scan}</div>
        {p.nodes.map((n, i) => (
          <div key={i} className={'qh-plan-node' + (n.warn ? ' is-warn' : '')} style={{ paddingLeft: (10 + n.d * 22) + 'px' }}>
            {n.d > 0 && <span className="qh-plan-arrow">→</span>}
            <span className="qh-plan-op">{n.op}</span>
            <span className="qh-plan-detail">{n.detail}</span>
            {n.warn && <span className="qh-plan-warn">seq scan</span>}
          </div>
        ))}
      </div>
    </div>
  );
}

const QH_PAGE_SIZES = [100, 500, 1000];
function ResultsView({ result, status, onToast, colMeta }) {
  // Column header tooltip. Two sources, in this order:
  //
  //   1. `result.colTypes[c]` — what the DRIVER reported for this exact result
  //      (migration 083). Authoritative, and it covers aliases, expressions,
  //      aggregates and joins that no catalog lookup can resolve.
  //   2. the schema snapshot (`colMeta`) — fallback for results that ran before
  //      083, and the only source for `not null`, which the driver does not
  //      report reliably (psycopg gives null_ok=None for every column).
  //
  // Nullability shows only when the snapshot is unambiguous about the name, so a
  // driver type can appear on its own. That is the honest rendering: the type is
  // known, the nullability is not.
  const colTitle = (c) => {
    const m = colMeta && colMeta[c];
    const type = (result && result.colTypes && result.colTypes[c]) || (m && m.type);
    if (!type) return c;
    return c + ' — ' + type + (m && m.notNull ? ' not null' : '');
  };
  const [pageSize, setPageSize] = React.useState(100);
  const [page, setPage] = React.useState(0);
  const [colW, setColW] = React.useState({});
  const [sel, setSel] = React.useState(() => new Set());
  // Keyboard cursor (the 'current cell'), distinct from `anchor`, which is
  // where a shift-extended rectangle is measured from.
  const [cursor, setCursor] = React.useState(null);
  const viewRef = React.useRef(null);
  const [colSel, setColSel] = React.useState(() => new Set());
  const [anchor, setAnchor] = React.useState(null);
  const colAnchorRef = React.useRef(null);
  const dragRef = React.useRef(null);
  const resizeRef = React.useRef(null);
  const scrollRef = React.useRef(null);

  const isTable = result && result.kind === 'table';
  React.useEffect(() => { setPage(0); setColW({}); setSel(new Set()); setColSel(new Set()); setAnchor(null); setCursor(null); }, [result]);

  React.useEffect(() => {
    const up = () => { dragRef.current = null; resizeRef.current = null; };
    const move = (e) => {
      if (resizeRef.current) {
        const { col, startX, startW } = resizeRef.current;
        const w = Math.max(48, Math.round(startW + (e.clientX - startX)));
        setColW(prev => ({ ...prev, [col]: w }));
      }
    };
    window.addEventListener('mouseup', up);
    window.addEventListener('mousemove', move);
    return () => { window.removeEventListener('mouseup', up); window.removeEventListener('mousemove', move); };
  }, []);

  const total = isTable ? result.total : 0;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const safePage = Math.min(page, totalPages - 1);
  const offset = safePage * pageSize;
  // Rows for the current page. The inline first page (result.rows) covers the
  // common case with zero network; pages beyond it stream from /rows via
  // result.fetchPage. The prototype mock has no .rows/.fetchPage, so it always
  // falls through to the synchronous slice. Selection/copy read result.slice.
  const [rows, setRows] = React.useState(() => (isTable && result && result.slice) ? result.slice(0, pageSize) : []);
  React.useEffect(() => {
    if (!isTable) { setRows([]); return; }
    const loaded = (result.rows || []).length;
    if (offset + pageSize <= loaded || !result.fetchPage) { setRows(result.slice(offset, pageSize)); return; }
    let alive = true;
    result.fetchPage(offset, pageSize)
      .then(rs => { if (alive) setRows(rs); })
      .catch(() => { if (alive) setRows(result.slice(offset, pageSize)); });
    return () => { alive = false; };
  }, [result, offset, pageSize, isTable]);
  const cols = isTable ? result.cols : [];
  const dispVal = (row, c) => { const v = row[c]; return v == null ? '' : String(v); };

  // Default column widths. The grid is `table-layout: fixed` so that a
  // dragged width is actually honoured — auto layout re-measured content on
  // every render and simply overrode the <colgroup>, which is why the resize
  // grip appeared to do nothing. Fixed layout means every column needs a
  // width, so derive a sensible one from the header and a sample of the
  // rows instead of letting the browser split the table evenly.
  const defW = React.useMemo(() => {
    const w = {};
    const sample = rows.slice(0, 30);
    cols.forEach(c => {
      let n = String(c).length;
      for (const r of sample) { const l = dispVal(r, c).length; if (l > n) n = l; }
      w[c] = Math.max(96, Math.min(420, Math.round(n * 7.6) + 28));
    });
    return w;
  }, [cols, rows]);
  const widthOf = (c) => colW[c] || defW[c] || 160;

  // Peek: the grid ellipsises at the column width and disables text selection
  // (drag-select owns the mouse), so a long value could be neither read nor
  // partially copied. Double-click opens the full value in a selectable box.
  const [peek, setPeek] = React.useState(null);

  // Grid context menu. The browser's own menu never offers "Copy" here: the
  // grid is user-select:none (drag-select owns the mouse), so there is no text
  // selection for it to act on. A data grid has to bring its own — this is the
  // SSMS behaviour (Copy / Copy with Headers) people expect.
  const [gmenu, setGmenu] = React.useState(null);
  const gridContextMenu = (ri, ci, e) => {
    e.preventDefault();
    // Right-clicking outside the current selection moves the selection to that
    // cell first, so the menu always acts on what you pointed at.
    if (ri != null && ci != null && !sel.has(keyOf(ri, ci)) && !colSel.has(ci)) {
      setSel(new Set([keyOf(ri, ci)])); setColSel(new Set());
      setAnchor({ r: ri, c: ci }); setCursor({ r: ri, c: ci });
    }
    setGmenu({ x: Math.min(e.clientX, window.innerWidth - 230), y: Math.min(e.clientY, window.innerHeight - 190), r: ri, c: ci });
  };
  const menuCopy = (withHeaders) => {
    const text = selectionClipboardText(withHeaders);
    setGmenu(null);
    if (!text) { onToast && onToast('Nothing selected to copy.'); return; }
    qhCopyText(text).then(ok => onToast && onToast(ok
      ? (withHeaders ? 'Copied with headers (TSV).' : 'Copied (TSV).')
      : 'Could not copy — the browser blocked clipboard access.'));
  };

  const keyOf = (r, c) => r + ':' + c;
  const rectKeys = (a, b) => {
    const s = new Set();
    const r0 = Math.min(a.r, b.r), r1 = Math.max(a.r, b.r), c0 = Math.min(a.c, b.c), c1 = Math.max(a.c, b.c);
    for (let r = r0; r <= r1; r++) for (let c = c0; c <= c1; c++) s.add(keyOf(r, c));
    return s;
  };
  const union = (a, b) => { const s = new Set(a); b.forEach(k => s.add(k)); return s; };

  // Every way of starting a selection has to take focus. preventDefault (which
  // drag-select needs, so the browser doesn't select page text) also stops the
  // browser moving focus, so we move it ourselves — otherwise focus stays
  // wherever it was, typically the SQL editor, and grid keystrokes are judged
  // against that element instead of the grid. Only cellDown used to do this,
  // so a selection made by clicking a header or a row number left focus in the
  // editor and its Ctrl+C was ambiguous between the two.
  const focusGrid = () => {
    if (viewRef.current) viewRef.current.focus({ preventScroll: true });
  };
  const cellDown = (r, c, e) => {
    // A right-click fires mousedown FIRST, so without this the selection
    // collapsed to the cell under the pointer before the context menu could
    // read it — right-clicking a multi-cell selection appeared to forget it.
    // (It "worked" with Ctrl/Shift held only because those branches extend
    // rather than replace.) Selection changes belong to the primary button;
    // onContextMenu decides what to do for the secondary one.
    if (e.button === 2) return;
    e.preventDefault();
    focusGrid();
    setColSel(new Set());
    if (e.shiftKey && anchor) { setSel(rectKeys(anchor, { r, c })); dragRef.current = { mode: 'range', anchor }; return; }
    if (e.metaKey || e.ctrlKey) {
      const next = new Set(sel); const k = keyOf(r, c);
      next.has(k) ? next.delete(k) : next.add(k);
      setSel(next); setAnchor({ r, c }); dragRef.current = { mode: 'add', anchor: { r, c }, base: next };
      return;
    }
    setSel(new Set([keyOf(r, c)])); setAnchor({ r, c }); setCursor({ r, c }); dragRef.current = { mode: 'range', anchor: { r, c } };
  };
  const cellEnter = (r, c) => {
    const d = dragRef.current; if (!d) return;
    const rect = rectKeys(d.anchor, { r, c });
    setSel(d.mode === 'add' ? union(d.base, rect) : rect);
  };
  const colDown = (c, e) => {
    if (e.button === 2) return;   // see cellDown
    e.preventDefault();
    focusGrid();
    setSel(new Set());
    if (e.shiftKey && colAnchorRef.current != null) {
      const a = Math.min(colAnchorRef.current, c), b = Math.max(colAnchorRef.current, c);
      const s = new Set(); for (let i = a; i <= b; i++) s.add(i); setColSel(s); return;
    }
    if (e.metaKey || e.ctrlKey) { const s = new Set(colSel); s.has(c) ? s.delete(c) : s.add(c); setColSel(s); colAnchorRef.current = c; return; }
    setColSel(new Set([c])); colAnchorRef.current = c;
  };
  const rowDown = (r, e) => {
    if (e.button === 2) return;   // see cellDown
    e.preventDefault();
    setColSel(new Set());
    const rowKeys = new Set(); for (let c = 0; c < cols.length; c++) rowKeys.add(keyOf(r, c));
    if (e.metaKey || e.ctrlKey) { setSel(union(sel, rowKeys)); setAnchor({ r, c: 0 }); return; }
    setSel(rowKeys); setAnchor({ r, c: 0 });
  };
  const selectAll = () => { setSel(new Set()); setColSel(new Set(cols.map((_, i) => i))); };

  const buildCopy = (sep) => {
    if (!sel.size) return '';
    const byRow = {};
    sel.forEach(k => { const [r, c] = k.split(':').map(Number); (byRow[r] = byRow[r] || []).push(c); });
    const rIdx = Object.keys(byRow).map(Number).sort((a, b) => a - b);
    return rIdx.map(r => byRow[r].sort((a, b) => a - b).map(c => dispVal(rows[r], cols[c])).join(sep)).join('\n');
  };
  const copySelection = () => {
    if (colSel.size) {
      const CAP = 5000;
      const cs = [...colSel].sort((a, b) => a - b);
      const n = Math.min(total, CAP);
      const all = result.slice(0, n);
      const header = cs.map(ci => cols[ci]).join('\t');
      const body = all.map(r => cs.map(ci => dispVal(r, cols[ci])).join('\t')).join('\n');
      qhCopyText(header + '\n' + body).then(ok => onToast && onToast(ok
        ? 'Copied ' + cs.length + ' column' + (cs.length > 1 ? 's' : '') + ' × ' + n.toLocaleString() + ' rows' + (total > CAP ? ' (first 5,000)' : '') + ' as TSV.'
        : 'Could not copy — the browser blocked clipboard access.'));
      return;
    }
    if (!sel.size) return;
    const text = buildCopy('\t');
    qhCopyText(text).then(ok => onToast && onToast(ok
      ? sel.size + ' cell' + (sel.size > 1 ? 's' : '') + ' copied (tab-separated — pastes as a grid).'
      : 'Could not copy — the browser blocked clipboard access.'));
  };
  // The clipboard text for the current selection, or '' when nothing is
  // selected. Shared by the native `copy` event handler and the grid's own
  // context menu. `withHeaders` prepends the column names of the selected
  // columns (SSMS's "Copy with Headers").
  const selectionClipboardText = (withHeaders) => {
    if (!colSel.size && sel.size && withHeaders) {
      const ci = [...new Set([...sel].map(k => Number(k.split(':')[1])))].sort((a, b) => a - b);
      return ci.map(i => cols[i]).join('\t') + '\n' + buildCopy('\t');
    }
    if (colSel.size) {
      const CAP = 5000;
      const cs = [...colSel].sort((a, b) => a - b);
      const all = result.slice(0, Math.min(total, CAP));
      return [cs.map(ci => cols[ci]).join('\t')]
        .concat(all.map(r => cs.map(ci => dispVal(r, cols[ci])).join('\t'))).join('\n');
    }
    return sel.size ? buildCopy('\t') : '';
  };
  // Keyboard cell navigation. Selection used to be mouse-only: without this,
  // a keyboard-only user could read the grid but never select or copy a cell.
  // Arrows move the cursor and select that one cell; Shift+arrow extends the
  // rectangle from the anchor, matching what shift-click already does.
  const gridFocused = () => {
    const host = viewRef.current;
    return !!(host && (host === document.activeElement || host.contains(document.activeElement)));
  };
  const moveCursor = (dr, dc, extend, absolute) => {
    if (!cols.length || !rows.length) return;
    const cur = cursor || anchor || { r: 0, c: 0 };
    const target = absolute || {
      r: Math.min(rows.length - 1, Math.max(0, cur.r + dr)),
      c: Math.min(cols.length - 1, Math.max(0, cur.c + dc)),
    };
    setColSel(new Set());
    setCursor(target);
    if (extend) {
      const a = anchor || cur;
      setAnchor(a);
      setSel(rectKeys(a, target));
    } else {
      setAnchor(target);
      setSel(new Set([keyOf(target.r, target.c)]));
    }
    // Keep the cursor on screen — a grid can be much wider and longer than
    // the viewport, and arrow keys that scroll nothing feel broken.
    const host = scrollRef.current;
    if (host) {
      const cell = host.querySelector('[data-cell="' + target.r + ':' + target.c + '"]');
      if (cell && cell.scrollIntoView) cell.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }
  };
  // Copy is driven by the NATIVE `copy` event, not by intercepting the
  // keystroke. `clipboardData.setData` is synchronous and needs no permission,
  // so it succeeds in exactly the cases where the async clipboard API quietly
  // refuses (unfocused document, gesture rules) — and it also makes
  // right-click → Copy and the Edit menu work, which the keydown hook never
  // did. The keydown path below is kept only as a fallback for a browser that
  // raises no `copy` event (the grid is user-select:none, so there is usually
  // no DOM selection to trigger one).
  const copyHandledRef = React.useRef(0);
  React.useEffect(() => {
    const fromField = (t) => !!t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
    const onCopy = (e) => {
      if (!(sel.size || colSel.size)) return;
      if (fromField(e.target) || fromField(document.activeElement)) return;
      if (window.getSelection && String(window.getSelection()).length) return;
      const text = selectionClipboardText();
      if (!text || !e.clipboardData) return;
      e.clipboardData.setData('text/plain', text);
      e.preventDefault();
      copyHandledRef.current = Date.now();
      onToast && onToast(colSel.size
        ? 'Copied ' + colSel.size + ' column' + (colSel.size > 1 ? 's' : '') + ' as TSV.'
        : sel.size + ' cell' + (sel.size > 1 ? 's' : '') + ' copied (tab-separated — pastes as a grid).');
    };
    document.addEventListener('copy', onCopy);
    return () => document.removeEventListener('copy', onCopy);
  });
  React.useEffect(() => {
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === 'c' || e.key === 'C')
          && (sel.size || colSel.size) && gridFocused()) {
        // The gate is "is the grid focused", the same test Ctrl+A uses. It
        // used to be "yield only to a field that has a selection of its OWN",
        // because back then a grid click left focus in the SQL editor and a
        // focus test would have blocked every grid copy. Now all three ways of
        // starting a selection call focusGrid(), so focus is the honest
        // signal — and it has to be, because the editor's Ctrl+C copies the
        // caret's whole line when nothing is selected (VS Code behaviour).
        // Under the old rule the grid stole exactly that keystroke, since a
        // collapsed caret satisfied "no selection of its own".
        if (window.getSelection && String(window.getSelection()).length) return;
        // Copy SYNCHRONOUSLY, inside the keystroke's own gesture. Deferring
        // this (waiting to see whether the browser raises `copy`) loses the
        // user activation, and both execCommand and the async clipboard API
        // then refuse — which is why the deferred version copied nothing.
        // The grid is user-select:none, so with no DOM selection most
        // browsers never raise `copy` here anyway.
        e.preventDefault(); copySelection();
        return;
      }
      if ((e.metaKey || e.ctrlKey) && (e.key === 'a' || e.key === 'A') && gridFocused()) {
        e.preventDefault(); selectAll();
        return;
      }
      if (e.metaKey || e.ctrlKey || e.altKey || !gridFocused()) return;
      const ae = document.activeElement;
      if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA')) return;
      const ext = e.shiftKey;
      switch (e.key) {
        case 'ArrowDown':  e.preventDefault(); moveCursor(1, 0, ext); break;
        case 'ArrowUp':    e.preventDefault(); moveCursor(-1, 0, ext); break;
        case 'ArrowRight': e.preventDefault(); moveCursor(0, 1, ext); break;
        case 'ArrowLeft':  e.preventDefault(); moveCursor(0, -1, ext); break;
        case 'Home':       e.preventDefault(); moveCursor(0, 0, ext, { r: (cursor || anchor || { r: 0 }).r, c: 0 }); break;
        case 'End':        e.preventDefault(); moveCursor(0, 0, ext, { r: (cursor || anchor || { r: 0 }).r, c: cols.length - 1 }); break;
        case 'PageDown':   e.preventDefault(); moveCursor(10, 0, ext); break;
        case 'PageUp':     e.preventDefault(); moveCursor(-10, 0, ext); break;
        default: break;
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  if (!result) {
    return <div className="qh-empty">
      <DBIcons.play />
      <div>Run a query to see results here.</div>
      <div className="qh-empty-hint">⌘/Ctrl + Enter to run</div>
    </div>;
  }
  if (result.kind === 'affected') {
    return <div className="qh-affected">
      <div className="qh-affected-num">{result.affected}</div>
      <div className="qh-affected-msg">{result.message}</div>
    </div>;
  }

  const startGrip = (c, e) => {
    e.preventDefault(); e.stopPropagation();
    const th = e.currentTarget.closest('th');
    resizeRef.current = { col: c, startX: e.clientX, startW: th ? th.getBoundingClientRect().width : 120 };
  };
  const allSelected = colSel.size === cols.length && cols.length > 0;
  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(offset + rows.length, total);

  return (
    <div className="qh-resultsview" tabIndex={0} ref={viewRef} role="grid"
         aria-label="Query result" aria-rowcount={total}>
      <div className="qh-table-scroll" ref={scrollRef}>
        {/* Explicit total width: `table-layout: fixed` is only honoured when
            the table itself has a width — with the stylesheet's `width: auto`
            the browser kept auto-sizing from content and the dragged <col>
            widths were ignored. Summing the columns also means the table
            overflows into the scroller instead of squeezing columns, which is
            what makes a wide result readable. */}
        <table className="qh-table qh-table-grid"
          style={{ width: 56 + cols.reduce((n, c) => n + widthOf(c), 0) }}>
          <colgroup>
            <col style={{ width: 56 }} />
            {cols.map(c => <col key={c} style={{ width: widthOf(c) }} />)}
          </colgroup>
          <thead>
            <tr>
              <th className={'qh-th-idx' + (allSelected ? ' is-sel' : '')}
                onMouseDown={(e) => { if (e.button === 2) return; e.preventDefault(); selectAll(); }}
                onContextMenu={(e) => { if (!sel.size && !colSel.size) selectAll(); gridContextMenu(null, null, e); }}
                title="Select all — right-click to copy the whole result">#</th>
              {cols.map((c, ci) => {
                const pii = (result.piiCols || []).includes(c);
                const colOn = colSel.has(ci);
                return (
                  <th key={c} className={colOn ? 'is-sel' : ''} onMouseDown={(e) => colDown(ci, e)}
                    onContextMenu={(e) => {
                      // Right-clicking a column you haven't selected selects
                      // it first, so "Copy" always means what you pointed at.
                      if (!colSel.has(ci)) { setColSel(new Set([ci])); setSel(new Set()); }
                      gridContextMenu(null, ci, e);
                    }}
                    title={colTitle(c)}>
                    <span className="qh-th-name">{c}{pii && <span className="qh-pii-dot" title="Masked (PII)" />}</span>
                    <span className="qh-col-grip" onMouseDown={(e) => startGrip(c, e)} />
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, ri) => (
              <tr key={ri}>
                <td className="qh-td-idx" onMouseDown={(e) => rowDown(ri, e)}
                  onContextMenu={(e) => {
                    const rowKeys = new Set();
                    for (let c = 0; c < cols.length; c++) rowKeys.add(keyOf(ri, c));
                    // Keep a multi-row selection; otherwise take this row.
                    const already = sel.has(keyOf(ri, 0));
                    if (!already) { setSel(rowKeys); setColSel(new Set()); setAnchor({ r: ri, c: 0 }); }
                    gridContextMenu(null, null, e);
                  }}>{(offset + ri + 1).toLocaleString()}</td>
                {cols.map((c, ci) => {
                  const pii = (result.piiCols || []).includes(c);
                  const seld = sel.has(keyOf(ri, ci)) || colSel.has(ci);
                  return (
                    <td key={c} data-cell={ri + ':' + ci} aria-selected={seld || undefined}
                      className={(seld ? 'qh-td-sel ' : '')
                        + (cursor && cursor.r === ri && cursor.c === ci ? 'qh-td-cursor ' : '')
                        + (pii ? 'qh-td-masked' : (typeof r[c] === 'number' ? 'qh-td-num' : ''))}
                      title={dispVal(r, c)}
                      onMouseDown={(e) => cellDown(ri, ci, e)} onMouseEnter={() => cellEnter(ri, ci)}
                      onContextMenu={(e) => gridContextMenu(ri, ci, e)}
                      onDoubleClick={(e) => {
                        const el = e.currentTarget;
                        // Only worth a popover when the value is actually
                        // clipped — opening one for a value you can already
                        // read in full is just a box in the way.
                        if (el.scrollWidth <= el.clientWidth) return;
                        const b = el.getBoundingClientRect();
                        setPeek({ col: c, value: dispVal(r, c),
                          top: Math.min(b.bottom + 6, window.innerHeight - 260),
                          left: Math.min(b.left, window.innerWidth - 420) });
                      }}>
                      {dispVal(r, c)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {gmenu && (
        <>
          <div className="qh-ctx-backdrop" onClick={() => setGmenu(null)} onContextMenu={(e) => { e.preventDefault(); setGmenu(null); }} />
          <div className="qh-ctxmenu" style={{ left: gmenu.x, top: gmenu.y }}>
            <button onClick={() => menuCopy(false)}>Copy</button>
            <button onClick={() => menuCopy(true)}>Copy with headers</button>
            <div className="qh-ctx-sep" />
            <button onClick={() => { if (gmenu.c != null) { setColSel(new Set([gmenu.c])); setSel(new Set()); } setGmenu(null); }}>Select column</button>
            <button onClick={() => { selectAll(); setGmenu(null); }}>Select all</button>
            <div className="qh-ctx-sep" />
            <button onClick={() => {
              const r = rows[gmenu.r], c = cols[gmenu.c];
              setGmenu(null);
              if (r && c != null) setPeek({ col: c, value: dispVal(r, c), top: gmenu.y, left: gmenu.x });
            }}>Show full value…</button>
          </div>
        </>
      )}
      {peek && (
        <>
          <div className="qh-ctx-backdrop" onClick={() => setPeek(null)} />
          <div className="qh-peek" style={{ top: peek.top, left: peek.left }} onMouseDown={(e) => e.stopPropagation()}>
            <div className="qh-peek-head">
              <b>{peek.col}</b>
              <span>{peek.value.length.toLocaleString()} chars</span>
              <span className="qh-flex1" />
              <button className="qh-btn qh-btn-ghost qh-btn-sm"
                onClick={() => qhCopyText(peek.value).then(ok => onToast && onToast(ok ? 'Cell copied.' : 'Could not copy — the browser blocked clipboard access.'))}>Copy</button>
              <button className="qh-btn qh-btn-ghost qh-btn-sm" onClick={() => setPeek(null)}>Close</button>
            </div>
            <textarea readOnly value={peek.value} autoFocus onFocus={(e) => e.target.select()} />
          </div>
        </>
      )}
      <div className="qh-pager">
        <div className="qh-pager-left">
          <span className="qh-pager-rows">{from.toLocaleString()}–{to.toLocaleString()} of {total.toLocaleString()}</span>
          {(sel.size > 0 || colSel.size > 0)
            ? <span className="qh-pager-sel">· {colSel.size ? colSel.size + ' column' + (colSel.size > 1 ? 's' : '') + ' (all rows)' : sel.size.toLocaleString() + ' cells'} selected — ⌘/Ctrl+C to copy</span>
            : <span className="qh-pager-sel">· drag a column edge to resize · double-click a cell for the full value</span>}
        </div>
        <div className="qh-pager-right">
          <label className="qh-pager-size">
            Rows/page
            <select value={pageSize} onChange={(e) => { setPageSize(Number(e.target.value)); setPage(0); }}>
              {QH_PAGE_SIZES.map(s => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>
          <div className="qh-pager-nav">
            <button disabled={safePage <= 0} onClick={() => setPage(0)} title="First page" aria-label="First page"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M18 6l-6 6 6 6M11 6l-6 6 6 6"/></svg></button>
            <button disabled={safePage <= 0} onClick={() => setPage(p => Math.max(0, p - 1))} title="Previous page" aria-label="Previous page"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M15 6l-6 6 6 6"/></svg></button>
            <span className="qh-pager-page">Page {(safePage + 1).toLocaleString()} / {totalPages.toLocaleString()}</span>
            <button disabled={safePage >= totalPages - 1} onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))} title="Next page" aria-label="Next page"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M9 6l6 6-6 6"/></svg></button>
            <button disabled={safePage >= totalPages - 1} onClick={() => setPage(totalPages - 1)} title="Last page" aria-label="Last page"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M6 6l6 6-6 6M13 6l6 6-6 6"/></svg></button>
          </div>
        </div>
      </div>
    </div>
  );
}

function MessagesView({ messages }) {
  if (!messages.length) return <div className="qh-empty"><div>No messages.</div></div>;
  return <div className="qh-msgs">
    {messages.map((m, i) => (
      <div key={i} className={'qh-msg msg-' + m.kind}>
        <span className="qh-msg-time">{m.time}</span>
        <span className="qh-msg-text">{m.text}</span>
      </div>
    ))}
  </div>;
}

function AuditView({ audit }) {
  if (!audit.length) return <div className="qh-empty"><div>Audit entries appear after you submit a query.</div></div>;
  return <div className="qh-audit">
    {audit.map((a, i) => (
      <div key={i} className="qh-audit-row">
        <span className="qh-audit-time">{a.time}</span>
        <span className="qh-audit-actor">{a.actor}</span>
        <span className="qh-audit-event">{a.event}</span>
      </div>
    ))}
  </div>;
}

const DBIcons = {
  layers: () => <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2 2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>,
  tree: () => <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="18" height="5" rx="1.5"/><rect x="3" y="15" width="18" height="5" rx="1.5"/><path d="M7 9v4M7 13h6v2"/></svg>,
  star: () => <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round"><path d="M12 3l2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9 6.8 19.2l1-5.8L3.5 9.2l5.9-.9z"/></svg>,
  clock: () => <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>,
  caret: () => <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><path d="M9 6l6 6-6 6"/></svg>,
  db: () => <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"><ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/></svg>,
  play: () => <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"><path d="M8 5v14l11-7z"/></svg>,
  calendar: () => <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="18" height="17" rx="2"/><path d="M3 9h18M8 2v4M16 2v4"/></svg>,
};

Object.assign(window, { Sidebar, ResultsPanel, ResultsView, TierBadge, StatusPill, DBIcons, RequestEndpointModal, OriginBadge, qhAgo });
