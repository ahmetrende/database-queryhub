// QueryHub Admin — Masking exemptions (design brief 2026-09-09).
//
// What this screen exists for: masking decides two ways and both over-reach.
// NAME rules say a column called `address` holds a postal address; VALUE
// detectors read the cell. A database is full of words that mean something else
// in context — `address` is a wallet, `name` is a venue, `table_name` is
// catalog metadata — and when a rule misfires the analyst gets [REDACTED] where
// they needed the value. The correction is an exemption, and all 30 that exist
// today were typed into psql by hand.
//
// Every exemption WIDENS what people can see. The screen's job is not to make
// that easy, it is to make it legible: the person writing one should see exactly
// what they are opening, and the person reading it in six months should be able
// to tell why. Two consequences of that, in the layout:
//   1. The wide rungs are grouped ABOVE the narrow ones and drawn differently.
//      27 of 30 are one column; a server-wide row must not scan like one of them.
//   2. The reason is a required step in the flow, not a note field at the bottom.
//      It is what an auditor reads, and today's 30 rows all have one.
const { useState: useMx } = React;

const MxIcon = {
  plus: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round"><path d="M12 5v14M5 12h14" /></svg>,
  eye: () => <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M1.5 12S5 5.5 12 5.5 22.5 12 22.5 12 19 18.5 12 18.5 1.5 12 1.5 12z" /><circle cx="12" cy="12" r="3" /></svg>,
  lock: () => <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="4" y="11" width="16" height="10" rx="2" /><path d="M8 11V8a4 4 0 018 0v3" /></svg>,
  warn: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L14.7 3.9a2 2 0 00-3.4 0z" /><path d="M12 9v4M12 17h.01" /></svg>,
  check: () => <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5" /></svg>,
};

// The ladder, each rung wider than the last. Rendered in this order everywhere —
// the order IS the argument, and shuffling it in one place would undo it.
const QH_MX_RUNGS = {
  column: { label: 'One column', of: 'in one table', count: 'the narrowest thing there is' },
  schema: { label: 'A whole schema', of: 'every column in it', count: 'a few exist' },
  database: { label: 'A whole database', of: 'masking off for anything read there', count: 'rare' },
  server: { label: 'A whole server', of: 'masking off for every database on it', count: 'rare' },
};
const QH_MX_ORDER = ['column', 'schema', 'database', 'server'];
const mxWide = (s) => s !== 'column';
const mxTarget = (e) => (e.schema ? e.schema + '.' : '') + (e.table ? e.table + '.' : '') + (e.column || '');

// The reach, as a sentence. Same job as `RoleSentence` on the Roles screen and
// the same reason: three chips make the reader assemble the meaning, and on a
// screen about reducing protection the meaning is the whole row. Bold carries
// what differs between two rows of the same rung.
function MxReach({ e }) {
  const conn = <b>{e.connectionName || e.connectionId}</b>;
  const db = <b>{e.databaseName || e.databaseId}</b>;
  if (e.scope === 'server') return <div className="qh-mxsent">Turns masking off for <b>every database on {e.connectionName || e.connectionId}</b>.</div>;
  if (e.scope === 'database') return <div className="qh-mxsent">Turns masking off for <b>everything read in {e.databaseName || e.databaseId}</b>, on {conn}.</div>;
  if (e.scope === 'schema') return <div className="qh-mxsent">Unmasks <b>every column in schema {e.schema}</b> — {db} on {conn}.</div>;
  return <div className="qh-mxsent">Unmasks <b>{e.schema}.{e.table}.{e.column}</b> in {db} on {conn}.</div>;
}

// The two consequences, in the words the form asks them in. Soft is the subtle
// one and the one most people want without knowing the word, so it is described
// by what still happens rather than by what it turns off.
function mxStrengthLine(e) {
  return e.strength === 'soft'
    ? 'Values are still read — an email or a card number landing here is still masked.'
    : 'Nothing here is masked, by name or by content.';
}
function mxJoinLine(e) {
  return e.survivesJoin
    ? 'Still exempt when the query joins a masked table.'
    : 'Re-masked when the query joins a masked table.';
}

function MxRow({ e, canWrite, moot, onToggle, onRemove, onAlso }) {
  const wide = mxWide(e.scope);
  return (
    <div className={'qh-mxrow' + (wide ? ' is-wide' : '') + (e.enabled ? '' : ' is-off') + (moot ? ' is-moot' : '')}>
      <div className="qh-mxrow-main">
        <div className="qh-mxrow-top">
          <span className="qh-mxpath">{e.connectionName}<span className="qh-mxsep">/</span>{e.scope === 'server' ? <span className="qh-mxall">all databases</span> : e.databaseName}
            {mxTarget(e) && <><span className="qh-mxsep">·</span>{mxTarget(e)}</>}</span>
          {/* A scope chip appears only on the WIDE rungs. 27 of 30 rows are one
              column, so the default needs no label and the exception carries
              one — a chip on every row would make them all read the same. */}
          {wide && <span className={'qh-mxchip is-' + e.scope}>{e.scope === 'server' ? 'whole server' : e.scope === 'database' ? 'whole database' : 'whole schema'}</span>}
          {!e.enabled && <span className="qh-mxchip is-off">off</span>}
          {e.missing && <span className="qh-mxchip is-gone">matches nothing</span>}
          {e.audience === 'super' && <span className="qh-mxchip is-aud">super-admins only</span>}
        </div>
        <MxReach e={e} />
        <div className="qh-mxlines">
          <span>{mxStrengthLine(e)}</span>
          <span>{mxJoinLine(e)}</span>
        </div>
        {/* Why the column is masked in the first place. Half the exemptions
            written are for a column somebody was surprised to see masked, and
            the surprise comes from not knowing which rule fired. */}
        {e.maskedBy && <div className="qh-mxwhy">Caught by the name rule <code>{e.maskedBy.key}</code> — {e.maskedBy.label}, {e.maskedBy.mask === 'full' ? 'fully' : 'partially'} masked.</div>}
        {/* A schema changed underneath it. It is still enforced, it just matches
            nothing — which is a different thing from being turned off, and the
            two must not read alike. */}
        {e.missing && <div className="qh-mxwhy is-gone">That table or column is not in the catalog any more. The exemption is still enforced; it matches nothing today.</div>}
        {e.reason && <div className="qh-mxreason">“{e.reason}”</div>}
        <div className="qh-mxmeta">{e.createdBy}{e.createdAt ? ' · ' + qhAgo(e.createdAt) : ''}</div>
        {/* The same database usually lives on more than one server, and an
            exemption on one is routinely missing on the other. A SUGGESTION,
            not an error: the operator may have meant exactly one of them. */}
        {e.enabled && (e.alsoOn || []).length > 0 && canWrite && (
          <div className="qh-mxsug">
            <MxIcon.eye />
            <span>{e.table}.{e.column} is not exempt on {e.alsoOn.map(a => a.connectionName).join(', ')}, which {e.alsoOn.length > 1 ? 'hold' : 'holds'} the same database.</span>
            <button className="qh-linkbtn" onClick={() => onAlso(e, e.alsoOn[0])}>Add it there</button>
          </div>
        )}
      </div>
      <div className="qh-mxacts">
        {canWrite ? <>
          {/* Turning one off is the fastest way to close an accidental exposure,
              so it is one action from the list and never behind the form. It
              does not delete: an exemption that was a mistake is itself a fact
              worth keeping. */}
          <button className="qh-btn qh-btn-sm" onClick={() => onToggle(e)}>{e.enabled ? 'Turn off' : 'Turn on'}</button>
          <button className="qh-mxdel" onClick={() => onRemove(e)}>Remove</button>
        </> : <span className="qh-rolelock"><MxIcon.lock />read-only</span>}
      </div>
    </div>
  );
}

// ---------- The add flow ----------
// Server → database → how far → the two consequences → who → why → preview.
// The rung is an explicit CHOICE and never the result of leaving a field blank:
// that is the single most important thing about this form, because the
// text-field version it replaces reached "whole server" exactly that way.
function MxForm({ st, seed, onDone }) {
  const conns = (st.connections || []).filter(c => c.enabled !== false);
  const [f, setF] = useMx(() => ({
    connectionId: (seed && seed.connectionId) || '', databaseId: (seed && seed.databaseId) || '',
    scope: 'column', schema: (seed && seed.schema) || '', table: (seed && seed.table) || '', column: (seed && seed.column) || '',
    strength: (seed && seed.strength) || 'soft', survivesJoin: !!(seed && seed.survivesJoin),
    audience: 'everyone', reason: '', confirmed: false,
  }));
  const [cat, setCat] = useMx(null);
  const [catErr, setCatErr] = useMx(null);
  const [prev, setPrev] = useMx(null);
  const [prevBusy, setPrevBusy] = useMx(false);
  const [busy, setBusy] = useMx(false);
  const [err, setErr] = useMx(null);
  const set = (patch) => { setF(x => ({ ...x, ...patch })); setErr(null); setPrev(null); };

  const conn = conns.find(c => c.id === f.connectionId) || null;
  const dbs = conn ? (conn.databases || []) : [];
  const db = dbs.find(d => d.id === f.databaseId) || null;

  // The catalog is fetched per database, in the form, and never cached in the
  // hook: a snapshot of the previous database is the one way this picker can
  // offer a table that is not there.
  React.useEffect(() => {
    setCat(null); setCatErr(null);
    if (!f.connectionId || !f.databaseId) return undefined;
    let live = true;
    st.maskCatalog(f.connectionId, f.databaseId)
      .then(r => { if (live) setCat(r); })
      .catch(e => { if (live) setCatErr((e && e.message) || 'No catalog snapshot for that database.'); });
    return () => { live = false; };
  }, [f.connectionId, f.databaseId]);

  const schemas = cat ? cat.schemas : [];
  const schema = schemas.find(s => s.name === f.schema) || null;
  const tables = schema ? schema.tables : [];
  const table = tables.find(t => t.name === f.table) || null;
  const columns = table ? table.columns : [];
  const col = columns.find(c => c.name === f.column) || null;

  const wide = mxWide(f.scope);
  const needsConfirm = f.scope === 'database' || f.scope === 'server';
  // A prefilled target (Add it there, from a sibling server's suggestion) can
  // name a column the catalog snapshot does not have. The form says so and
  // blocks Save rather than leaving a blank picker beside a button that names
  // the column: "picked from the catalog, never typed" has to hold on the
  // prefill path too, or the rule is only true of the paths nobody worries about.
  const colGone = f.scope === 'column' && !!f.column && !!cat && !!table && !col;
  const ready = !!f.connectionId
    && (f.scope === 'server' || !!f.databaseId)
    && (f.scope !== 'schema' || !!f.schema)
    && (f.scope !== 'column' || (!!f.schema && !!f.table && !!f.column));
  const bad = !ready || colGone || !f.reason.trim() || (needsConfirm && !f.confirmed);

  const preview = { connectionId: f.connectionId, databaseId: f.databaseId, scope: f.scope, schema: f.schema, table: f.table, column: f.column };
  const runPreview = () => {
    if (!ready || prevBusy) return;
    setPrevBusy(true);
    st.maskPreview(preview).then(r => { setPrev(r); setPrevBusy(false); }).catch(() => { setPrev({ error: true }); setPrevBusy(false); });
  };

  const save = () => {
    if (bad || busy) return;
    setBusy(true); setErr(null);
    st.addMaskExemption({ ...preview, strength: f.strength, survivesJoin: f.survivesJoin, audience: f.audience, reason: f.reason.trim() })
      .then(() => { setBusy(false); onDone(); })
      .catch(e => { setBusy(false); setErr({ msg: (e && e.message) || 'Could not add the exemption.', code: e && e.code }); });
  };

  // The button says the consequence, in the words of the rung. "Save" on a
  // control that turns masking off for a production server is the one label
  // that would make the wide rungs feel like the narrow ones.
  const saveLabel = !ready ? 'Add exemption'
    : f.scope === 'server' ? 'Turn masking off for all of ' + (conn ? conn.name : '')
      : f.scope === 'database' ? 'Turn masking off for ' + (db ? db.name : '')
        : f.scope === 'schema' ? 'Unmask every column in ' + f.schema
          : 'Unmask ' + f.table + '.' + f.column;

  const asRow = { ...preview, connectionName: conn ? conn.name : '', databaseName: db ? db.name : '',
    strength: f.strength, survivesJoin: f.survivesJoin };

  return (
    <div className="qh-mxform">
      <div className="qh-accedit-label">Where<span className="qh-accedit-hint">The server and database the exemption is written against.</span></div>
      <div className="qh-mxfields">
        <label className="qh-rolefield"><span className="qh-rolefield-l">Server</span>
          <select className="qh-select" value={f.connectionId} onChange={e => set({ connectionId: e.target.value, databaseId: '', schema: '', table: '', column: '' })}>
            <option value="">Pick a server…</option>
            {conns.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select></label>
        {f.scope !== 'server' && <label className="qh-rolefield"><span className="qh-rolefield-l">Database</span>
          <select className="qh-select" disabled={!conn} value={f.databaseId} onChange={e => set({ databaseId: e.target.value, schema: '', table: '', column: '' })}>
            <option value="">{conn ? 'Pick a database…' : 'Pick a server first'}</option>
            {dbs.map(d => <option key={d.id} value={d.id}>{d.name}</option>)}
          </select></label>}
      </div>

      <div className="qh-accedit-label" style={{ marginTop: 16 }}>How far it reaches
        <span className="qh-accedit-hint">Each step down is wider than the one above it. Pick the rung — a wide exemption is never the result of leaving a field empty.</span></div>
      <div className="qh-mxrungs">
        {QH_MX_ORDER.map(k => (
          <button key={k} type="button" className={'qh-mxrung' + (f.scope === k ? ' is-on' : '') + (mxWide(k) ? ' is-wide' : '')}
                  onClick={() => set({ scope: k, confirmed: false, table: k === 'column' ? f.table : '', column: k === 'column' ? f.column : '', schema: (k === 'column' || k === 'schema') ? f.schema : '' })}>
            <span className="qh-mxrung-l">{QH_MX_RUNGS[k].label}{f.scope === k && <MxIcon.check />}</span>
            <span className="qh-mxrung-w">{QH_MX_RUNGS[k].of}</span>
          </button>
        ))}
      </div>

      {/* Table and column are PICKED from the catalog, never typed — 156,000
          known columns, and a typo writes an exemption that silently matches
          nothing. Each column shows what currently masks it, which is the
          question that brought most people to this screen. */}
      {(f.scope === 'column' || f.scope === 'schema') && (
        <div className="qh-mxfields" style={{ marginTop: 12 }}>
          <label className="qh-rolefield"><span className="qh-rolefield-l">Schema</span>
            <select className="qh-select" disabled={!cat} value={f.schema} onChange={e => set({ schema: e.target.value, table: '', column: '' })}>
              <option value="">{cat ? 'Pick a schema…' : 'Loading catalog…'}</option>
              {schemas.map(s => <option key={s.name} value={s.name}>{s.name}</option>)}
            </select></label>
          {f.scope === 'column' && <label className="qh-rolefield"><span className="qh-rolefield-l">Table</span>
            <select className="qh-select" disabled={!schema} value={f.table} onChange={e => set({ table: e.target.value, column: '' })}>
              <option value="">{schema ? 'Pick a table…' : 'Pick a schema first'}</option>
              {tables.map(t => <option key={t.name} value={t.name}>{t.name}</option>)}
            </select></label>}
          {f.scope === 'column' && <label className="qh-rolefield qh-mxcolfield"><span className="qh-rolefield-l">Column</span>
            <select className="qh-select" disabled={!table} value={f.column} onChange={e => set({ column: e.target.value })}>
              <option value="">{table ? 'Pick a column…' : 'Pick a table first'}</option>
              {columns.map(c => <option key={c.name} value={c.name}>{c.name}{c.rule ? ' — masked as ' + c.rule.label : ' — not masked'}</option>)}
            </select></label>}
        </div>
      )}
      {catErr && <div className="qh-roleform-err">{catErr} Pick another database, or ask for a catalog snapshot to be taken.</div>}
      {colGone && <div className="qh-roleform-err"><code>{f.column}</code> is not in {conn ? conn.name : 'this server'}’s catalog snapshot for {f.schema}.{f.table} — pick a column from the list, or have the snapshot refreshed if you know it is there.</div>}
      {/* Exempting a column nothing masks is not an error, but it is almost
          always a mis-pick, so it is said before Save rather than discovered
          when the [REDACTED] does not go away. */}
      {f.scope === 'column' && col && !col.rule && <div className="qh-mxnote">Nothing masks <code>{col.name}</code> today — no name rule matches it. An exemption here only changes what the six value detectors do.</div>}
      {f.scope === 'column' && col && col.rule && <div className="qh-mxnote">Masked today by the name rule <code>{col.rule.key}</code> — {col.rule.label}, {col.rule.mask === 'full' ? 'fully' : 'partially'} masked.</div>}

      {/* The wide rungs get the weight here, once, rather than as a banner on
          every row of the list. The confirm is a checkbox and not a typed
          server name: three of these exist and the people writing them are
          right to; the point is that it cannot happen by tabbing past. */}
      {ready && !colGone && (
        <div className={'qh-mxreach' + (wide ? ' is-wide' : '')}>
          {wide && <MxIcon.warn />}
          <div>
            <MxReach e={asRow} />
            {needsConfirm && <label className="qh-mxconfirm">
              <input type="checkbox" checked={f.confirmed} onChange={e => setF(x => ({ ...x, confirmed: e.target.checked }))} />
              <span>I mean to switch masking off for {f.scope === 'server' ? 'every database on ' + (conn ? conn.name : '') : 'everything in ' + (db ? db.name : '')}.</span>
            </label>}
          </div>
        </div>
      )}

      <div className="qh-accedit-label" style={{ marginTop: 16 }}>What it stops</div>
      <div className="qh-mxq">
        <div className="qh-mxq-t">Still check the values here for emails, card numbers and national ids?</div>
        <div className="qh-mxopts">
          <button type="button" className={'qh-mxopt' + (f.strength === 'soft' ? ' is-on' : '')} onClick={() => set({ strength: 'soft' })}>
            <span className="qh-mxopt-l">Yes — keep reading values</span>
            <span className="qh-mxopt-w">Stops the name rule only. If somebody’s email ever lands here it is still masked.</span></button>
          <button type="button" className={'qh-mxopt' + (f.strength === 'full' ? ' is-on' : '')} onClick={() => set({ strength: 'full' })}>
            <span className="qh-mxopt-l">No — stop masking entirely</span>
            <span className="qh-mxopt-w">Name and content both. Nothing here is masked again until this is turned off.</span></button>
        </div>
      </div>
      <div className="qh-mxq">
        <div className="qh-mxq-t">If a query joins this to a table that is still masked, does the exemption hold?</div>
        <div className="qh-mxopts">
          <button type="button" className={'qh-mxopt' + (!f.survivesJoin ? ' is-on' : '')} onClick={() => set({ survivesJoin: false })}>
            <span className="qh-mxopt-l">No — re-mask the join</span>
            <span className="qh-mxopt-w">The safe default. A join is how a protected column rides along inside an innocent-looking query.</span></button>
          <button type="button" className={'qh-mxopt' + (f.survivesJoin ? ' is-on' : '')} onClick={() => set({ survivesJoin: true })}>
            <span className="qh-mxopt-l">Yes — stay exempt</span>
            <span className="qh-mxopt-w">For tables that are almost always joined, where re-masking makes the exemption useless in practice.</span></button>
        </div>
        {/* The screen cannot count how this table is really queried. The server
            can, and that count is the evidence the decision needs. */}
        {prev && prev.joinStats && <div className="qh-mxevidence">Of the {prev.joinStats.total} queries against <code>{f.table}</code> in the last {prev.days} days, <b>{prev.joinStats.joined}</b> joined another table.</div>}
      </div>

      <div className="qh-accedit-label" style={{ marginTop: 16 }}>Who it applies to<span className="qh-accedit-hint">Almost always everyone. One of the thirty is narrower.</span></div>
      <select className="qh-select" style={{ maxWidth: 260 }} value={f.audience} onChange={e => set({ audience: e.target.value })}>
        <option value="everyone">Everyone who can read it</option>
        <option value="super">Super-admins only</option>
      </select>

      <div className="qh-accedit-label" style={{ marginTop: 16 }}>Why<span className="qh-accedit-hint">Required. This is what an auditor reads when asking why this data stopped being protected — say what you checked, not that you checked it.</span></div>
      <textarea className="qh-input qh-mxreason-in" rows={3} value={f.reason} onChange={e => set({ reason: e.target.value })}
                placeholder="e.g. Holds the counterparty wallet address, not a postal one. 42 of 51 queries in March joined customers, so it survives joins." />

      <div className="qh-mxprev">
        <div className="qh-mxprev-head">
          <div className="qh-roleprev-h">Before saving</div>
          <button className="qh-btn qh-btn-sm" disabled={!ready || prevBusy} onClick={runPreview}>{prevBusy ? 'Checking…' : prev ? 'Check again' : 'Show what this changes'}</button>
        </div>
        {!prev && <div className="qh-mxprev-idle">{ready ? 'Runs a recent real query against this table and shows one row masked both ways.' : 'Pick a target first.'}</div>}
        {prev && prev.error && <div className="qh-mxprev-idle">Could not build a preview. The exemption is still what the sentence above says.</div>}
        {/* No sample is a finding, not an empty box. */}
        {prev && !prev.error && !prev.seen && !prev.wide && <div className="qh-mxprev-idle">Nothing has queried <code>{f.table}</code> in the last {prev.days} days, so there is no real row to show. The change is what the sentence above says.</div>}
        {prev && prev.wide && <div className="qh-mxprev-idle">This reaches <b>{prev.tables}</b> tables and <b>{prev.maskedColumns}</b> columns that are masked today. Too wide for a single sample row — that count is the preview.</div>}
        {prev && prev.seen && (
          <div className="qh-mxdiffwrap">
            <div className="qh-mxprev-q"><code>{prev.sql}</code><span className="qh-mxprev-by">{prev.by} · {qhAgo(prev.at)}</span></div>
            <div className="qh-tablewrap">
              <table className="qh-mxdiff">
                <thead><tr><th />{prev.columns.map(c => <th key={c} className={c === f.column ? 'is-t' : ''}>{c}</th>)}</tr></thead>
                <tbody>
                  <tr><td className="qh-mxdiff-k">Today</td>{prev.columns.map(c => <td key={c} className={c === f.column ? 'is-t is-masked' : ''}>{prev.before[c]}</td>)}</tr>
                  <tr><td className="qh-mxdiff-k">After</td>{prev.columns.map(c => <td key={c} className={c === f.column ? 'is-t is-clear' : ''}>{prev.after[c]}</td>)}</tr>
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>

      {err && <div className="qh-roleform-err">{err.msg}</div>}
      <div className="qh-accedit-acts">
        <button className="qh-btn qh-btn-sm" onClick={onDone}>Cancel</button>
        <button className={'qh-btn qh-btn-sm ' + (wide ? 'qh-btn-danger' : 'qh-btn-primary')} disabled={bad || busy} onClick={save}>{busy ? 'Saving…' : saveLabel}</button>
      </div>
      {ready && !f.reason.trim() && <div className="qh-mxnote">A reason is required — an exemption without one is unreadable to whoever finds it next.</div>}
    </div>
  );
}

// ---------- Empty state ----------
// A fresh install. It teaches the ladder, because the ladder is the thing that
// makes this screen safe to use and it is not guessable from a list of zero.
function MxEmpty({ canWrite, onStart, meta }) {
  return (
    <div className="qh-roleempty">
      <div className="qh-roleempty-h">Nothing is exempt from masking</div>
      <p className="qh-roleempty-p">QueryHub masks personal data two ways: <b>{meta.nameRules || 50} name rules</b> (a column called <code>email</code> holds an email) and <b>{(meta.valueDetectors || []).length || 6} value detectors</b> reading the cell itself. Both over-reach — a column called <code>address</code> may hold a wallet, and a column called <code>name</code> a venue. An exemption is how you correct one.</p>
      <dl className="qh-roledefs">
        {QH_MX_ORDER.map(k => <div key={k}><dt>{QH_MX_RUNGS[k].label}</dt><dd>{QH_MX_RUNGS[k].of.charAt(0).toUpperCase() + QH_MX_RUNGS[k].of.slice(1)}.</dd></div>)}
        <div><dt>Soft or full</dt><dd>Soft stops the name rule and keeps reading values, so a stray email is still caught. Full stops both.</dd></div>
        <div><dt>Joins</dt><dd>By default a join to a masked table re-masks the column, because a join is how a protected column rides along inside an innocent-looking query.</dd></div>
      </dl>
      {canWrite
        ? <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={onStart}><MxIcon.plus />Add the first exemption</button>
        : <div className="qh-role-why">Adding an exemption needs a super-admin.</div>}
    </div>
  );
}

// ---------- The view ----------
function MaskingView({ st, user }) {
  const canWrite = !!(user && user.role === 'super');
  const [q, setQ] = useMx('');
  const [filter, setFilter] = useMx('all');
  const [adding, setAdding] = useMx(false);
  const [seed, setSeed] = useMx(null);
  const all = st.maskExemptions || [];
  const meta = st.maskMeta || {};
  const moot = meta.maskingEnabled === false;

  const match = (e) => {
    const t = q.trim().toLowerCase();
    if (!t) return true;
    return [e.connectionName, e.databaseName, e.schema, e.table, e.column, e.reason, e.createdBy]
      .filter(Boolean).join(' ').toLowerCase().includes(t);
  };
  const keep = (e) => filter === 'all' ? true
    : filter === 'wide' ? mxWide(e.scope)
      : filter === 'off' ? !e.enabled
        : filter === 'gone' ? e.missing : true;
  const rows = all.filter(keep).filter(match);
  const wideRows = rows.filter(e => mxWide(e.scope));
  const colRows = rows.filter(e => !mxWide(e.scope));

  // Column rows are grouped by server because that is how they are read: an
  // operator arrives holding one server's name, not one column's.
  const byServer = (() => {
    const m = new Map();
    colRows.forEach(e => { const k = e.connectionName || e.connectionId; if (!m.has(k)) m.set(k, []); m.get(k).push(e); });
    return [...m.entries()].sort((a, b) => (a[0] > b[0] ? 1 : -1));
  })();

  const start = (s) => { setSeed(s || null); setAdding(true); };
  const remove = (e) => {
    if (!window.confirm('Remove this exemption? ' + (e.column ? e.table + '.' + e.column : e.connectionName) + ' goes back to being masked, and the reason on the row goes with it. Turning it off keeps the record.')) return;
    st.removeMaskExemption(e.id);
  };

  return (
    <div className="qh-apad">
      <div className="qh-aview-head">
        <div><div className="qh-aview-title">Masking exemptions</div>
          <div className="qh-aview-sub">Where masking is deliberately switched off, and why. Every row here widens what people can see.</div></div>
        {canWrite && !adding && all.length > 0 && <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={() => start(null)}><MxIcon.plus />Add an exemption</button>}
      </div>

      {/* Masking off fleet-wide makes every row below moot. Said at the top,
          once, rather than letting the screen imply it is doing something. */}
      {moot && <div className="qh-rolenote is-warn"><MxIcon.warn /><span>Masking is switched off fleet-wide in <a href="#admin/config">System configuration</a> — nothing on this screen is having any effect right now.</span></div>}
      {!canWrite && <div className="qh-rolenote"><MxIcon.lock />Masking exemptions are super-admin only — this is the one screen whose purpose is to reduce protection.</div>}

      {adding && canWrite && <MxForm st={st} seed={seed} onDone={() => { setAdding(false); setSeed(null); }} />}

      {all.length === 0 && !adding && <MxEmpty canWrite={canWrite} onStart={() => start(null)} meta={meta} />}

      {all.length > 0 && (
        <>
          <div className="qh-conn-controls">
            <div className="qh-search sm">
              <svg className="qh-search-ic" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="7" /><path d="M21 21l-4.3-4.3" /></svg>
              <input className="qh-search-in" placeholder="Filter by server, table, column, reason…" value={q} onChange={e => setQ(e.target.value)} />
              {q && <button className="qh-search-x" onMouseDown={e => { e.preventDefault(); setQ(''); }} aria-label="Clear"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18" /></svg></button>}
            </div>
            <div className="qh-seg qh-seg-sm">
              {[['all', 'All ' + all.length], ['wide', 'Wider than a column'], ['off', 'Turned off'], ['gone', 'Matching nothing']].map(([v, l]) => (
                <button key={v} className={'qh-seg-opt' + (filter === v ? ' is-active' : '')} onClick={() => setFilter(v)}>{l}</button>
              ))}
            </div>
            <div className="qh-mxcount">{(meta.nameRules || 0)} name rules · {(meta.valueDetectors || []).length} value detectors</div>
          </div>

          {/* The wide ones first and drawn differently. Grouping is what keeps a
              server-wide exemption from being one of twenty-seven lookalikes. */}
          {wideRows.length > 0 && (
            <>
              <div className="qh-section-label">Wider than one column · {wideRows.length}</div>
              <div className="qh-aview-sub" style={{ marginBottom: 10 }}>These switch masking off for more than a single column. Legitimate — a public reference database has nothing to protect — but they are the rows to read first.</div>
              <div className="qh-mxlist">{wideRows.map(e => <MxRow key={e.id} e={e} canWrite={canWrite} moot={moot} onToggle={x => st.setMaskExemptionEnabled(x.id, !x.enabled)} onRemove={remove} onAlso={() => {}} />)}</div>
            </>
          )}

          {colRows.length > 0 && <div className="qh-section-label" style={{ marginTop: wideRows.length ? 18 : 0 }}>Single columns · {colRows.length}</div>}
          {byServer.map(([k, list]) => (
            <div key={k} className="qh-mxgroup">
              <div className="qh-mxgroup-h">{k}<span className="qh-mxgroup-n">{list.length}</span></div>
              <div className="qh-mxlist">{list.map(e => (
                <MxRow key={e.id} e={e} canWrite={canWrite} moot={moot}
                       onToggle={x => st.setMaskExemptionEnabled(x.id, !x.enabled)} onRemove={remove}
                       onAlso={(row, a) => start({ connectionId: a.connectionId, databaseId: a.databaseId, schema: row.schema, table: row.table, column: row.column, strength: row.strength, survivesJoin: row.survivesJoin })} />
              ))}</div>
            </div>
          ))}
          {rows.length === 0 && <div className="qh-conn-empty">No exemptions match your filter.</div>}
        </>
      )}
    </div>
  );
}

Object.assign(window, { MaskingView, MxReach, QH_MX_RUNGS });
