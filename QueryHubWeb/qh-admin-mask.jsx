// QueryHub Admin — Masking exemptions (design brief 2026-09-09, simplified 2026-09-15, 2026-09-28).
//
// What this screen exists for: masking decides two ways and both over-reach.
// NAME rules say a column called `address` holds a postal address; VALUE
// detectors read the cell. A database is full of words that mean something else
// in context — `address` is a wallet, `name` is a venue — and when a rule
// misfires the analyst gets [REDACTED] where they needed the value. The
// correction is an exemption.
//
// 2026-09-28 — "Add" starts from where you are standing. The list gained Server
// and Database filters (pick-only, like every other picker here), and the add
// button inherits them: a server sets the server and puts the caret in
// Database; a server AND database set both, pick "One column", caret in Schema.
// The form became one column of one width: the target pickers are all visible
// at once (each disabled until the one before it is answered) instead of
// appearing one by one, one note line at most, one label style, three compact
// radio rows instead of cards, and one action bar whose disabled Save says why.
// No fleet count is written into the copy: where a number helps it is counted
// from the rows.
//
// None of the safety properties moved: the rung is an explicit choice and never
// the result of a blank field, a wide row still looks wide, the two widest
// rungs still need a confirmation, the widest of all still needs it typed, the
// reason is still required, and table and column are still picked, never typed.
const { useState: useMx } = React;

const MxIcon = {
  plus: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round"><path d="M12 5v14M5 12h14" /></svg>,
  eye: () => <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M1.5 12S5 5.5 12 5.5 22.5 12 22.5 12 19 18.5 12 18.5 1.5 12 1.5 12z" /><circle cx="12" cy="12" r="3" /></svg>,
  lock: () => <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="4" y="11" width="16" height="10" rx="2" /><path d="M8 11V8a4 4 0 018 0v3" /></svg>,
  warn: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L14.7 3.9a2 2 0 00-3.4 0z" /><path d="M12 9v4M12 17h.01" /></svg>,
  caret: () => <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round"><path d="M9 6l6 6-6 6" /></svg>,
  x: () => <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18" /></svg>,
  dots: () => <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="1.8" /><circle cx="12" cy="12" r="1.8" /><circle cx="19" cy="12" r="1.8" /></svg>,
};

// The ladder, each rung wider than the last. Rendered in this order everywhere —
// the order IS the argument. `fleet` is off the ladder on purpose: it is wider
// than the top rung and should not sit in the same row as "one column".
const QH_MX_RUNGS = {
  column: { label: 'One column', w: 'One column in one table.' },
  table: { label: 'Whole table', w: 'Every column in one table.' },
  schema: { label: 'Whole schema', w: 'Every column in every table in one schema.' },
  database: { label: 'Whole database', w: 'Masking off for anything read in one database.' },
  server: { label: 'Whole server', w: 'Masking off for every database on one server.' },
  fleet: { label: 'Every server', w: 'Masking off across the whole fleet — every database on every server.' },
};
const QH_MX_ORDER = ['column', 'table', 'schema', 'database', 'server'];
const mxWide = (s) => s !== 'column' && s !== 'table';
// What the row names inside its group label. Every rung has an answer computed
// from the SCOPE, never from whether a field happens to be set — the widest
// reach must never be what an empty field produces (2026-09-07 §3), and that
// holds for the label too.
const mxWhat = (e) => {
  if (e.scope === 'fleet') return e.schema ? 'schema ' + e.schema : 'every schema';
  if (e.scope === 'server') return 'every database';
  if (e.scope === 'database') return 'every table';
  if (e.column) return (e.schema ? e.schema + '.' : '') + (e.table ? e.table + '.' : '') + e.column;
  if (e.table) return (e.schema ? e.schema + '.' : '') + e.table;
  return e.schema ? 'schema ' + e.schema : 'every table';
};

// The reach, as a sentence. Three chips make the reader assemble the meaning,
// and on a screen about reducing protection the meaning is the whole row.
function MxReach({ e }) {
  const conn = <b>{e.connectionName || e.connectionId}</b>;
  const db = <b>{e.databaseName || e.databaseId}</b>;
  if (e.scope === 'fleet') return <div className="qh-mxsent">{e.schema
    ? <>Unmasks <b>every column in schema {e.schema}</b> — <b>every database on every server</b>.</>
    : <>Turns masking off <b>across the whole fleet</b> — every database on every server.</>}</div>;
  if (e.scope === 'server') return <div className="qh-mxsent">Turns masking off for <b>every database on {e.connectionName || e.connectionId}</b>.</div>;
  if (e.scope === 'database') return <div className="qh-mxsent">Turns masking off for <b>everything read in {e.databaseName || e.databaseId}</b>, on {conn}.</div>;
  if (e.scope === 'schema') return <div className="qh-mxsent">Unmasks <b>every column in schema {e.schema}</b> — {db} on {conn}.</div>;
  if (e.scope === 'table') return <div className="qh-mxsent">Unmasks <b>every column in {e.schema}.{e.table}</b> — {db} on {conn}.</div>;
  return <div className="qh-mxsent">Unmasks <b>{e.schema}.{e.table}.{e.column}</b> in {db} on {conn}.</div>;
}

// The three settings in a few words each — the summary that replaces asking.
function mxSummary(e) {
  return [
    e.strength === 'full' ? 'Values not checked' : 'Values still checked',
    e.survivesJoin ? 'stays exempt in joins' : 're-masked on joins',
    e.audience === 'super' ? 'super-admins only' : 'everyone',
  ].join(' · ');
}

// The default a row is measured against is READ FROM THE ROWS, not assumed. A
// row chips only where it disagrees with the majority, so the rule holds
// whatever the fleet's mix turns out to be.
function mxNorms(rows) {
  const most = (key, fallback) => {
    const tally = new Map();
    rows.forEach(r => { const v = r[key]; tally.set(v, (tally.get(v) || 0) + 1); });
    let best = fallback, n = -1;
    tally.forEach((c, v) => { if (c > n) { n = c; best = v; } });
    return best;
  };
  if (!rows || rows.length < 4) return { strength: 'soft', survivesJoin: false, audience: 'everyone' };
  return { strength: most('strength', 'soft'), survivesJoin: !!most('survivesJoin', false), audience: most('audience', 'everyone') };
}
const mxNormShort = (n) => [
  n.strength === 'full' ? 'no masking' : 'soft',
  n.survivesJoin ? 'exempt in joins' : 're-masked on joins',
  n.audience === 'super' ? 'super-admins only' : 'everyone',
].join(' · ');

// What differs from the norm, and nothing else. Absence of a chip is information.
function MxFlags({ e, norms }) {
  const n = norms || { strength: 'soft', survivesJoin: false, audience: 'everyone' };
  const wide = mxWide(e.scope);
  return <>
    {wide && <span className={'qh-mxchip is-' + e.scope}>{e.scope === 'fleet' ? 'every server' : e.scope === 'server' ? 'whole server' : e.scope === 'database' ? 'whole database' : 'whole schema'}</span>}
    {e.scope === 'table' && <span className="qh-mxchip">whole table</span>}
    {e.strength !== n.strength && (e.strength === 'full'
      ? <span className="qh-mxchip is-full" title="No masking at all — the name rule AND the value detectors are off here">no masking</span>
      : <span className="qh-mxchip is-soft" title="Soft — the name rule is off, but values are still checked for emails, card numbers and national ids">soft</span>)}
    {e.survivesJoin !== n.survivesJoin && (e.survivesJoin
      ? <span className="qh-mxchip is-join" title="Stays exempt when the query joins a masked table">in joins</span>
      : <span className="qh-mxchip" title="Re-masked when the query joins a masked table">not in joins</span>)}
    {e.audience !== n.audience && (e.audience === 'super'
      ? <span className="qh-mxchip is-aud" title="Only super-admins see it unmasked. Everyone else sees the mask.">super only</span>
      : <span className="qh-mxchip" title="Everyone who can read the table sees it unmasked">everyone</span>)}
    {!e.enabled && <span className="qh-mxchip is-off" title="Disabled. QueryHub masks this column again and keeps the record.">off</span>}
    {e.missing && <span className="qh-mxchip is-gone" title="Still enforced, but that table or column is not in the catalog any more">matches nothing</span>}
  </>;
}

// ---------- Small shared pieces ----------
function MxStep({ n, t, hint }) {
  return <div className="qh-mxstep"><span className="qh-mxstep-n">{n}</span><span className="qh-mxstep-t">{t}</span>{hint && <span className="qh-mxstep-h">{hint}</span>}</div>;
}
// The Save button, and — when it cannot be pressed — why, on the button itself.
// A note under the bar was one more line of prose on a form that had too many.
function MxSave({ label, why, danger, busy, onClick }) {
  return (
    <span className="qh-mxsavewrap" title={why || undefined}>
      <button className={'qh-btn qh-btn-sm ' + (danger ? 'qh-btn-danger' : 'qh-btn-primary')} disabled={!!why || busy} onClick={onClick}>{busy ? 'Saving…' : label}</button>
    </span>
  );
}
// Row actions that are not the two used most. Same idiom (and CSS) as the
// Connections row menu: portalled, placed from the button, opens upward near the
// bottom, closes on pick, outside click, Escape, scroll and resize.
function MxRowMenu({ items }) {
  const [pos, setPos] = useMx(null);
  const btnRef = React.useRef(null), popRef = React.useRef(null);
  const open = pos != null;
  const list = items.filter(Boolean);
  const place = () => {
    const r = btnRef.current.getBoundingClientRect();
    const up = window.innerHeight - r.bottom < 40 * list.length + 24;
    setPos({ right: window.innerWidth - r.right, top: up ? r.top - 4 : r.bottom + 4, up });
  };
  React.useEffect(() => {
    if (!open) return undefined;
    const off = (e) => { if (!(btnRef.current && btnRef.current.contains(e.target)) && !(popRef.current && popRef.current.contains(e.target))) setPos(null); };
    const shut = () => setPos(null);
    const esc = (e) => { if (e.key === 'Escape') setPos(null); };
    document.addEventListener('mousedown', off); document.addEventListener('keydown', esc);
    window.addEventListener('scroll', shut, true); window.addEventListener('resize', shut);
    return () => { document.removeEventListener('mousedown', off); document.removeEventListener('keydown', esc); window.removeEventListener('scroll', shut, true); window.removeEventListener('resize', shut); };
  }, [open]);
  return (
    <>
      <button ref={btnRef} className={'qh-rowbtn qh-rowmenu-btn' + (open ? ' is-open' : '')} onClick={() => (open ? setPos(null) : place())} aria-haspopup="menu" aria-expanded={open} aria-label="More actions"><MxIcon.dots /></button>
      {open && ReactDOM.createPortal(
        <div ref={popRef} className={'qh-rowmenu-pop' + (pos.up ? ' is-up' : '')} role="menu" style={{ top: pos.top, right: pos.right }}>
          {list.map(it => (
            <button key={it.label} role="menuitem" className={'qh-rowmenu-item' + (it.danger ? ' is-danger' : '')} onClick={() => { setPos(null); it.on(); }}>
              <span>{it.label}</span>
            </button>
          ))}
        </div>, document.body)}
    </>
  );
}

// ---------- The three settings, shared by add and edit ----------
// Three rows of two, in one width. The option most live rows use is marked as
// the usual one — counted, not written down.
const MX_SETTINGS = [
  { k: 'strength', label: 'Values', opts: [
    ['soft', 'still checked', 'Emails, card numbers and ids are still caught.'],
    ['full', 'not checked', 'Nothing here is masked, by name or by content.']] },
  { k: 'survivesJoin', label: 'Joins', opts: [
    [false, 're-masked', 'A join to a masked table masks it again.'],
    [true, 'stays exempt', 'For tables that are almost always joined.']] },
  { k: 'audience', label: 'Who', opts: [
    ['everyone', 'everyone', 'Anyone who can read the table.'],
    ['super', 'super-admins only', 'Everyone else still gets the mask.']] },
];
function MxOptions({ f, set, norms, joinStats, table, fleet }) {
  return (
    <div className="qh-mxsettings">
      {MX_SETTINGS.map(q => (
        <React.Fragment key={q.k}>
          <div className="qh-mxset" role="radiogroup" aria-label={q.label}>
            <span className="qh-mxset-k">{q.label}</span>
            {q.opts.map(([v, l, h]) => {
              const on = f[q.k] === v;
              const hint = fleet && q.k === 'audience' && v === 'everyone' ? 'Not advised fleet-wide — start restricted.' : h;
              return (
                <label key={String(v)} className={'qh-mxradio' + (on ? ' is-on' : '')}>
                  <input type="radio" name={'mx-' + q.k} checked={on} onChange={() => set({ [q.k]: v })} />
                  <span className="qh-mxradio-t"><span className="qh-mxradio-l">{l}{norms && norms[q.k] === v && <span className="qh-mxradio-d">usual</span>}</span><span className="qh-mxradio-h">{hint}</span></span>
                </label>
              );
            })}
          </div>
          {/* The screen cannot count how this table is really queried. The
              server can, and that count is the evidence the decision needs. */}
          {q.k === 'survivesJoin' && joinStats && <div className="qh-mxevidence">Of the {joinStats.total} queries against <code>{table}</code> in the last {joinStats.days} days, <b>{joinStats.joined}</b> joined another table.</div>}
        </React.Fragment>
      ))}
    </div>
  );
}

// ---------- Editing one in place ----------
// Reach is absent from this form on purpose, and the line says so with the
// action that does change it. The three settings are open from the start
// (CODE 2026-10-06): behind a "Change" link they read as optional, and an
// operator who wanted "survives joins" took Replace instead and hit a 409.
function MxEdit({ e, st, norms, onDone, onReplace }) {
  const [f, setF] = useMx({ strength: e.strength || 'soft', survivesJoin: !!e.survivesJoin, audience: e.audience || 'everyone', reason: e.reason || '' });
  const [busy, setBusy] = useMx(false);
  const [err, setErr] = useMx(null);
  const set = (patch) => { setF(x => ({ ...x, ...patch })); setErr(null); };
  const dirty = f.strength !== (e.strength || 'soft') || f.survivesJoin !== !!e.survivesJoin
    || f.audience !== (e.audience || 'everyone') || f.reason.trim() !== (e.reason || '');
  const why = !f.reason.trim() ? 'Write a reason.' : !dirty ? 'Change a field first.' : null;
  const save = () => {
    if (busy || why) return;
    setBusy(true);
    st.updateMaskExemption(e.id, { strength: f.strength, survivesJoin: f.survivesJoin, audience: f.audience, reason: f.reason.trim() })
      .then(() => { setBusy(false); onDone(); })
      .catch(x => { setBusy(false); setErr((x && x.message) || 'Could not save the change.'); });
  };
  return (
    <div className="qh-mxeditbox">
      <div className="qh-mxeditwhat"><MxReach e={e} /><span className="qh-mxfixed">Change the settings here. To change where it reaches, <button className="qh-linkbtn" onClick={() => onReplace(e)}>replace it</button>.</span></div>
      <div className="qh-mxfield"><span className="qh-mxlab">Why</span>
        <textarea className="qh-input qh-mxreason-in" rows={2} value={f.reason} onChange={x => set({ reason: x.target.value })} /></div>
      <div className="qh-mxfield"><span className="qh-mxlab">Settings</span>
        <MxOptions f={f} set={set} norms={norms} /></div>
      {err && <div className="qh-roleform-err">{err}</div>}
      <div className="qh-mxacts">
        <button className="qh-btn qh-btn-sm" onClick={onDone}>Cancel</button>
        <MxSave label="Save changes" why={why} busy={busy} onClick={save} />
      </div>
    </div>
  );
}

// ---------- One row ----------
// Closed: what is exempt (mono) with its exceptions, the reason on one line, the
// age. Open: the reach, the three settings in one line, what masks it, the full
// reason, who and when — then two buttons and a menu for the rest.
function MxItem({ e, canWrite, moot, norms, open, onToggleOpen, st, onReplace, onAlso, onRemove, onAddHere }) {
  const [editing, setEditing] = useMx(false);
  const wide = mxWide(e.scope);
  return (
    <div className={'qh-mxitem' + (wide ? ' is-wide' : '') + (e.enabled ? '' : ' is-off') + (open ? ' is-open' : '') + (moot ? ' is-moot' : '')}>
      <button className="qh-mxitem-top" onClick={() => { setEditing(false); onToggleOpen(); }} aria-expanded={open}>
        <span className="qh-mxcaret"><MxIcon.caret /></span>
        <span className="qh-mxbody">
          <span className="qh-mxline1">
            <span className="qh-mxtitle">{mxWhat(e)}</span>
            <MxFlags e={e} norms={norms} />
          </span>
          {e.reason && <span className="qh-mxsaid">{e.reason}</span>}
        </span>
        <span className="qh-mxwhen">{e.createdAt ? qhAgo(e.createdAt) : ''}</span>
      </button>
      {open && (
        <div className="qh-mxdetail">
          {editing
            ? <MxEdit e={e} st={st} norms={norms} onDone={() => setEditing(false)} onReplace={onReplace} />
            : <>
              <MxReach e={e} />
              <div className="qh-mxlines">{mxSummary(e)}</div>
              {e.maskedBy && <div className="qh-mxwhy">Caught by the name rule <code>{e.maskedBy.key}</code> — {e.maskedBy.label}, {e.maskedBy.mask === 'full' ? 'fully' : 'partially'} masked.</div>}
              {e.missing && <div className="qh-mxwhy is-gone">That table or column is not in the catalog now. The exemption still applies, but it matches nothing today.</div>}
              {e.reason && <div className="qh-mxreason">“{e.reason}”</div>}
              <div className="qh-mxmeta">{qhPersonName(e.createdBy)}{e.createdAt ? ' · ' + qhAgo(e.createdAt) : ''}{e.updatedBy ? ' · edited by ' + qhPersonName(e.updatedBy) + (e.updatedAt ? ' ' + qhAgo(e.updatedAt) : '') : ''}</div>
              {/* The same database usually lives on more than one server. A
                  SUGGESTION: the operator may have meant exactly one of them. */}
              {e.enabled && (e.alsoOn || []).length > 0 && canWrite && (
                <div className="qh-mxsug">
                  <MxIcon.eye />
                  <span>{e.table}.{e.column} is not exempt on {e.alsoOn.map(a => a.connectionName).join(', ')}, which {e.alsoOn.length > 1 ? 'hold' : 'holds'} the same database.</span>
                  <button className="qh-linkbtn" onClick={() => onAlso(e, e.alsoOn[0])}>Add it there</button>
                </div>
              )}
              <div className="qh-mxrowacts">
                {canWrite ? <>
                  {/* Turning one off is the fastest way to close an accidental
                      exposure, so it stays a button and never behind a menu. */}
                  <button className="qh-btn qh-btn-sm" onClick={() => st.setMaskExemptionEnabled(e.id, !e.enabled)}>{e.enabled ? 'Disable' : 'Enable'}</button>
                  <button className="qh-btn qh-btn-sm" onClick={() => setEditing(true)}>Edit</button>
                  <MxRowMenu items={[
                    e.table && { label: 'Another column in ' + e.table, on: () => onAddHere(e) },
                    { label: 'Replace', on: () => onReplace(e) },
                    { label: 'Remove', danger: true, on: () => onRemove(e) },
                  ]} />
                </> : <span className="qh-rolelock"><MxIcon.lock />read-only</span>}
              </div>
            </>}
        </div>
      )}
    </div>
  );
}

// ---------- A picker you can type into ----------
// Typing FILTERS the list (starts-with first, then contains), ↑/↓ and Enter
// pick, Escape puts the last answer back. It never accepts free text: leaving
// the field without picking keeps what was there. The list is portalled.
// `autoFocus` puts the caret in without popping the list — a list that opens by
// itself over a form that just appeared is one more thing to dismiss.
function MxCombo({ value, options, onPick, placeholder, disabled, clearOnPick, wide, small, autoFocus, onClear, ariaLabel }) {
  const [q, setQ] = useMx(null);
  const [hi, setHi] = useMx(0);
  const [pos, setPos] = useMx(null);
  const inRef = React.useRef(null), listRef = React.useRef(null);
  const quiet = React.useRef(false), didFocus = React.useRef(false);
  const cur = options.find(o => o.value === value);
  const open = pos != null;
  const needle = (q || '').trim().toLowerCase();
  const shown = !needle ? options : options
    .map(o => ({ o, i: o.label.toLowerCase().indexOf(needle) }))
    .filter(x => x.i >= 0).sort((a, b) => ((a.i === 0 ? 0 : 1) - (b.i === 0 ? 0 : 1)) || a.o.label.localeCompare(b.o.label))
    .map(x => x.o);
  const CAP = 300;
  const place = () => {
    const r = inRef.current.getBoundingClientRect();
    const below = window.innerHeight - r.bottom, up = below < 240 && r.top > below;
    setPos({ left: r.left, width: Math.max(r.width, wide ? 380 : 260), top: up ? r.top - 4 : r.bottom + 4, up, max: Math.max(160, Math.min(300, (up ? r.top : below) - 16)) });
  };
  const close = () => { setPos(null); setQ(null); setHi(0); };
  const pick = (o) => { if (!o) return; onPick(o.value); close(); if (clearOnPick && inRef.current) inRef.current.focus(); };
  React.useEffect(() => {
    if (!autoFocus || disabled || didFocus.current || !inRef.current) return;
    didFocus.current = true; quiet.current = true;
    inRef.current.focus({ preventScroll: true });
    quiet.current = false;
  }, [autoFocus, disabled]);
  React.useEffect(() => {
    if (!open) return undefined;
    const off = (e) => { if (!(inRef.current && inRef.current.contains(e.target)) && !(listRef.current && listRef.current.contains(e.target))) close(); };
    const shut = (e) => { if (!(listRef.current && listRef.current.contains(e.target))) close(); };
    document.addEventListener('mousedown', off); window.addEventListener('scroll', shut, true); window.addEventListener('resize', close);
    return () => { document.removeEventListener('mousedown', off); window.removeEventListener('scroll', shut, true); window.removeEventListener('resize', close); };
  }, [open]);
  React.useEffect(() => {
    const el = listRef.current && listRef.current.querySelector('.is-hi');
    if (el && listRef.current) { const l = listRef.current; if (el.offsetTop < l.scrollTop) l.scrollTop = el.offsetTop; else if (el.offsetTop + el.offsetHeight > l.scrollTop + l.clientHeight) l.scrollTop = el.offsetTop + el.offsetHeight - l.clientHeight; }
  }, [hi, open]);
  const key = (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); if (!open) place(); setHi(h => Math.min(h + 1, Math.min(shown.length, CAP) - 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setHi(h => Math.max(h - 1, 0)); }
    else if (e.key === 'Enter') { if (open && shown[hi]) { e.preventDefault(); pick(shown[hi]); } }
    else if (e.key === 'Escape') { if (open) { e.preventDefault(); e.stopPropagation(); close(); } }
    else if (e.key === 'Tab') close();
  };
  const showX = onClear && value && !disabled;
  return (
    <span className={'qh-mxcombo' + (wide ? ' is-wide' : '') + (small ? ' is-sm' : '') + (value && small ? ' is-set' : '')}>
      <input ref={inRef} className="qh-input qh-mxcombo-in" disabled={disabled} placeholder={placeholder} spellCheck={false} autoComplete="off"
        role="combobox" aria-expanded={open} aria-autocomplete="list" aria-label={ariaLabel || placeholder}
        value={q != null ? q : (clearOnPick ? '' : (cur ? cur.label : ''))}
        onFocus={() => { if (!open && !quiet.current) place(); }} onClick={() => { if (!open) place(); }}
        onChange={e => { setQ(e.target.value); setHi(0); if (!open) place(); }} onKeyDown={key} />
      {showX
        ? <button type="button" className="qh-mxcombo-x" aria-label="Clear" onMouseDown={e => e.preventDefault()} onClick={() => { close(); onClear(); }}><MxIcon.x /></button>
        : <svg className="qh-mxcombo-caret" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M6 9l6 6 6-6" /></svg>}
      {open && ReactDOM.createPortal(
        <div ref={listRef} className={'qh-mxcombo-list' + (pos.up ? ' is-up' : '')} role="listbox" style={{ left: pos.left, top: pos.top, width: pos.width, maxHeight: pos.max }}>
          {shown.length === 0 && <div className="qh-mxcombo-none">{options.length ? 'Nothing matches “' + (q || '').trim() + '”.' : 'Nothing to pick here.'}</div>}
          {shown.slice(0, CAP).map((o, i) => (
            <div key={o.value} role="option" aria-selected={o.value === value}
              className={'qh-mxcombo-opt' + (i === hi ? ' is-hi' : '') + (o.value === value ? ' is-cur' : '')}
              onMouseDown={e => { e.preventDefault(); pick(o); }} onMouseEnter={() => setHi(i)}>
              <span className="qh-mxcombo-l">{o.label}</span>{o.hint && <span className={'qh-mxcombo-h' + (o.hintWarn ? ' is-warn' : '')}>{o.hint}</span>}
            </div>
          ))}
          {shown.length > CAP && <div className="qh-mxcombo-none">{shown.length - CAP} more — keep typing to narrow it.</div>}
        </div>, document.body)}
    </span>
  );
}

// ---------- The add flow ----------
// One column, one width, three numbered sections. The rung is an explicit CHOICE
// and never the result of leaving a field blank — still the single most
// important thing about this form.
// Replace mode (`seed.replaceOf`, CODE 2026-10-06): the form opens on the
// original's reach. While the reach is still the original's, Save writes the
// settings onto that row (PATCH) — a new row there is a 409 duplicate. Once the
// reach moves, Save writes the new row(s) and then disables the original, so a
// replace leaves one live row, not two.
function mxRef(id) { return /^\d+$/.test(String(id)) ? '#' + id : String(id); }
function MxForm({ st, seed, norms, onDone }) {
  const conns = (st.connections || []).filter(c => c.enabled !== false);
  const n = norms || { strength: 'soft', survivesJoin: false, audience: 'everyone' };
  const orig = (seed && seed.replaceOf) || null;
  const [f, setF] = useMx(() => ({
    connectionId: (seed && seed.connectionId) || '', databaseId: (seed && seed.databaseId) || '',
    scope: (seed && seed.scope) || 'column', schema: (seed && seed.schema) || '', table: (seed && seed.table) || '', column: '',
    columns: (seed && seed.column) ? [seed.column] : [],
    strength: (seed && seed.strength) || n.strength, survivesJoin: seed && seed.survivesJoin != null ? !!seed.survivesJoin : !!n.survivesJoin,
    audience: (orig && orig.audience) || n.audience || 'everyone', reason: (orig && orig.reason) || '', confirmed: false, typed: '',
  }));
  const focus = seed && seed.focus;
  const [more, setMore] = useMx(false);
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
  // hook: a snapshot of the previous database could offer a table that is not there.
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
  // Several columns of ONE table, in one pass: one target, one reason, one set
  // of consequences, written once per column.
  const picked = f.scope === 'column' ? (f.columns || []) : [];
  const col = picked.length === 1 ? columns.find(c => c.name === picked[0]) || null : null;
  const addCol = (name) => { if (name && picked.indexOf(name) < 0) set({ columns: picked.concat([name]), column: '' }); };
  const dropCol = (name) => set({ columns: picked.filter(c => c !== name) });

  const wide = mxWide(f.scope);
  const needsConfirm = f.scope === 'database' || f.scope === 'server';
  // Fleet-wide is the only confirmation that cannot be clicked: there is no
  // server name on screen to read, so the confirmation IS the sentence.
  const FLEET_PHRASE = 'every server';
  const needsType = f.scope === 'fleet';
  const typedOk = !needsType || f.typed.trim().toLowerCase() === FLEET_PHRASE;
  // "Picked from the catalog, never typed" has to hold on the prefill path too.
  const gonePicked = f.scope === 'column' && !!cat && !!table ? picked.filter(c => !columns.some(x => x.name === c)) : [];
  const colGone = gonePicked.length > 0;
  const missing = f.scope === 'fleet' ? null
    : !f.connectionId ? 'a server'
      : f.scope !== 'server' && !f.databaseId ? 'a database'
        : (f.scope === 'schema' || f.scope === 'table' || f.scope === 'column') && !f.schema ? 'a schema'
          : (f.scope === 'table' || f.scope === 'column') && !f.table ? 'a table'
            : f.scope === 'column' && !picked.length ? 'at least one column' : null;
  const readyAll = !missing;
  // Same place as the original, rung and every path field.
  const samePlace = !!orig && f.scope === orig.scope && (f.connectionId || null) === (orig.connectionId || null)
    && (f.databaseId || null) === (orig.databaseId || null) && (f.schema || null) === (orig.schema || null)
    && (f.table || null) === (orig.table || null);
  const sameReach = samePlace && (f.scope !== 'column' || (picked.length === 1 && picked[0] === orig.column));
  // The original's column plus others: that column is a duplicate and the rest are new.
  const mixed = samePlace && f.scope === 'column' && picked.length > 1 && picked.indexOf(orig.column) >= 0;
  const why = missing ? 'Pick ' + missing + '.'
    : colGone ? 'Pick columns that are in the catalog.'
      : mixed ? 'Remove ' + orig.column + ', or pick only ' + orig.column + '. ' + mxRef(orig.id) + ' covers it.'
        : !sameReach && needsConfirm && !f.confirmed ? 'Tick the confirmation above.'
          : !sameReach && !typedOk ? 'Type “' + FLEET_PHRASE + '” to confirm.'
            : !f.reason.trim() ? 'Write a reason.' : null;

  const preview = { connectionId: f.connectionId, databaseId: f.databaseId, scope: f.scope, schema: f.schema, table: f.table, column: picked[0] || f.column };
  const runPreview = () => {
    if (!readyAll || prevBusy) return;
    setPrevBusy(true);
    st.maskPreview(preview).then(r => { setPrev(r); setPrevBusy(false); }).catch(() => { setPrev({ error: true }); setPrevBusy(false); });
  };

  const save = () => {
    if (why || busy) return;
    setBusy(true); setErr(null);
    const settings = { strength: f.strength, survivesJoin: f.survivesJoin, audience: f.audience, reason: f.reason.trim() };
    if (sameReach) {
      st.updateMaskExemption(orig.id, settings)
        .then(() => { setBusy(false); onDone(); })
        .catch(e => { setBusy(false); setErr({ msg: (e && e.message) || 'Could not save the settings.', code: e && e.code }); });
      return;
    }
    const bodies = f.scope === 'column'
      ? picked.map(c => ({ ...preview, column: c, ...settings }))
      : [{ ...preview, ...settings }];
    // One at a time, and it stops at the first refusal: a partial result has to
    // say exactly which ones landed.
    bodies.reduce((chain, b, i) => chain.then(() => st.addMaskExemption(b).catch(e => {
      const done = i > 0 ? ' The first ' + i + ' were written.' : '';
      throw { message: ((e && e.message) || 'Could not add the exemption.') + (f.scope === 'column' && picked.length > 1 ? ' Stopped at ' + b.column + '.' + done : ''), code: e && e.code };
    })), Promise.resolve())
      // A real replace disables the original only AFTER every new row landed.
      // If that last call fails, the new rows stay and the error says so.
      .then(() => orig && orig.enabled !== false && st.retireMaskExemption(orig.id).catch(e => {
        throw { message: 'QueryHub wrote the new exemption, but did not disable ' + mxRef(orig.id) + '. ' + ((e && e.message) || '') + ' Disable it by hand.', code: e && e.code };
      }))
      .then(() => { setBusy(false); onDone(); })
      .catch(e => { setBusy(false); setErr({ msg: (e && e.message) || 'Could not add the exemption.', code: e && e.code }); });
  };

  // The button says the consequence, in the words of the rung.
  const saveLabel = !readyAll ? (orig ? 'Replace' : 'Add exemption')
    : sameReach ? 'Save these settings on ' + mxRef(orig.id)
    : f.scope === 'fleet' ? 'Turn masking off across the whole fleet'
      : f.scope === 'server' ? 'Turn masking off for all of ' + (conn ? conn.name : '')
        : f.scope === 'database' ? 'Turn masking off for ' + (db ? db.name : '')
          : f.scope === 'schema' ? 'Unmask every column in ' + f.schema
            : f.scope === 'table' ? 'Unmask every column in ' + f.table
              : picked.length > 1 ? 'Unmask ' + picked.length + ' columns in ' + f.table
                : 'Unmask ' + f.table + '.' + (picked[0] || f.column);

  const asRow = { ...preview, connectionName: conn ? conn.name : '', databaseName: db ? db.name : '', strength: f.strength, survivesJoin: f.survivesJoin };
  const isFleet = f.scope === 'fleet';
  const showDb = f.scope !== 'server' && !isFleet;
  const showSchema = f.scope === 'column' || f.scope === 'table' || f.scope === 'schema' || isFleet;
  const showTable = f.scope === 'column' || f.scope === 'table';
  const showCols = f.scope === 'column';
  const pickScope = (k) => set({ scope: k, confirmed: false, typed: '', audience: f.scope === 'fleet' && f.audience === 'super' ? (n.audience || 'everyone') : f.audience,
    table: (k === 'column' || k === 'table') ? f.table : '',
    columns: k === 'column' ? (f.columns || []) : [], column: '',
    schema: (k === 'column' || k === 'table' || k === 'schema') ? f.schema : '' });

  // At most ONE note under the target: an error first, else what masks the
  // picked column today, else nothing.
  const note = catErr ? { bad: true, t: <>{catErr} Pick another database, or ask for a catalog snapshot.</> }
    : colGone ? { bad: true, t: <><code>{gonePicked.join(', ')}</code> {gonePicked.length > 1 ? 'are' : 'is'} not in the catalog snapshot for {f.schema}.{f.table}. Pick from the list, or have the snapshot refreshed.</> }
      : col && !col.rule ? { t: <>Nothing masks <code>{col.name}</code> today. An exemption here only changes what the value detectors do.</> }
        : col && col.rule ? { t: <>Masked today by the name rule <code>{col.rule.key}</code> — {col.rule.label}, {col.rule.mask === 'full' ? 'fully' : 'partially'} masked.</> }
          : null;

  return (
    <div className="qh-mxform">
      {orig && <div className="qh-mxnote">{sameReach
        ? <>Replacing {mxRef(orig.id)}. The target is the same, so Save changes the settings on {mxRef(orig.id)}. Change the target to write a new exemption.</>
        : <>Replacing {mxRef(orig.id)}. Save writes the new exemption, then disables {mxRef(orig.id)}.</>}</div>}
      <MxStep n="1" t="Target" />
      <div className="qh-mxscope">
        <div className="qh-seg qh-seg-sm">
          {QH_MX_ORDER.map(k => (
            <button key={k} type="button" className={'qh-seg-opt' + (f.scope === k ? ' is-active' : '') + (mxWide(k) ? ' is-wide' : '')} onClick={() => pickScope(k)}>{QH_MX_RUNGS[k].label}</button>
          ))}
        </div>
        {!isFleet
          ? <button type="button" className="qh-linkbtn qh-mxfleetlink" onClick={() => set({ scope: 'fleet', confirmed: false, typed: '', connectionId: '', databaseId: '', table: '', columns: [], column: '', audience: 'super' })}>…or every server</button>
          : <button type="button" className="qh-linkbtn qh-mxfleetlink" onClick={() => set({ scope: 'column', typed: '', schema: '', audience: n.audience || 'everyone' })}>back to one server</button>}
      </div>

      {/* Every picker the rung needs, visible at once, each disabled until the
          one before it has an answer — so what is still missing is on screen. */}
      <div className="qh-mxgrid">
        <div className="qh-mxfield"><span className="qh-mxlab">Server</span>
          <MxCombo ariaLabel="Server" disabled={isFleet} value={f.connectionId} placeholder={isFleet ? 'Every server' : 'Pick a server'}
            options={conns.map(c => ({ value: c.id, label: c.name, hint: c.env || null }))}
            onPick={v => set({ connectionId: v, databaseId: '', schema: '', table: '', columns: [], column: '' })} /></div>
        {showDb && <div className="qh-mxfield"><span className="qh-mxlab">Database</span>
          <MxCombo ariaLabel="Database" autoFocus={focus === 'database'} disabled={!conn} value={f.databaseId} placeholder="Pick a database"
            options={dbs.map(d => ({ value: d.id, label: d.name }))}
            onPick={v => set({ databaseId: v, schema: '', table: '', columns: [], column: '' })} /></div>}
        {showSchema && <div className="qh-mxfield"><span className="qh-mxlab">Schema{isFleet && <span className="qh-mxlab-h"> · optional</span>}</span>
          {isFleet
            // No catalog to read across the fleet, so this one rung takes a
            // typed schema name. The exception the rule earns.
            ? <input className="qh-input" placeholder="empty = every schema" value={f.schema} onChange={e => set({ schema: e.target.value })} />
            : <MxCombo ariaLabel="Schema" autoFocus={focus === 'schema'} disabled={!cat} value={f.schema}
              placeholder={f.databaseId && !cat && !catErr ? 'Loading…' : 'Pick a schema'}
              options={schemas.map(x => ({ value: x.name, label: x.name }))}
              onPick={v => set({ schema: v, table: '', columns: [], column: '' })} />}</div>}
        {showTable && <div className="qh-mxfield"><span className="qh-mxlab">Table</span>
          <MxCombo ariaLabel="Table" disabled={!schema} value={f.table} placeholder="Pick a table"
            options={tables.map(t => ({ value: t.name, label: t.name }))}
            onPick={v => set({ table: v, columns: [], column: '' })} /></div>}
        {showCols && <div className="qh-mxfield qh-mxgrid-cols"><span className="qh-mxlab">Columns{picked.length > 1 && <span className="qh-mxlab-h"> · one exemption each, same reason</span>}</span>
          <div className="qh-mxcols">
            {picked.map(c => (
              <span key={c} className="qh-mxcolchip">{c}
                <button type="button" title={'Remove ' + c} onClick={() => dropCol(c)}><MxIcon.x /></button>
              </span>
            ))}
            <MxCombo ariaLabel="Columns" clearOnPick wide autoFocus={focus === 'columns'} disabled={!table} value="" placeholder={picked.length ? 'Add another column' : 'Pick columns'}
              options={columns.filter(c => picked.indexOf(c.name) < 0).map(c => ({ value: c.name, label: c.name, hint: c.rule ? 'masked as ' + c.rule.label : 'not masked', hintWarn: !c.rule }))}
              onPick={addCol} />
          </div></div>}
      </div>
      {note && <div className={'qh-mxnote' + (note.bad ? ' is-bad' : '')}>{note.t}</div>}

      {/* The reach, said once, and the confirmation for the rungs that need one. */}
      {readyAll && !colGone && !sameReach && (
        <div className={'qh-mxreach' + (wide ? ' is-wide' : '')}>
          {wide && <MxIcon.warn />}
          <div>
            <MxReach e={asRow} />
            {needsConfirm && <label className="qh-mxconfirm">
              <input type="checkbox" checked={f.confirmed} onChange={e => setF(x => ({ ...x, confirmed: e.target.checked }))} />
              <span>I mean to switch masking off for {f.scope === 'server' ? 'every database on ' + (conn ? conn.name : '') : 'everything in ' + (db ? db.name : '')}.</span>
            </label>}
            {needsType && <label className="qh-mxtype">
              <span>Type <b>{FLEET_PHRASE}</b> to confirm this reaches all of them.</span>
              <input className="qh-input" value={f.typed} onChange={e => setF(x => ({ ...x, typed: e.target.value }))} placeholder={FLEET_PHRASE} />
            </label>}
          </div>
        </div>
      )}

      <MxStep n="2" t="Why" hint="Required. What you checked, for whoever reads this later." />
      <textarea className="qh-input qh-mxreason-in" rows={2} aria-label="Why" value={f.reason} onChange={e => set({ reason: e.target.value })}
                placeholder="e.g. Holds a wallet address, not a postal one." />

      <MxStep n="3" t="Settings" />
      {orig ? <MxOptions f={f} set={set} norms={n} fleet={isFleet} table={f.table} joinStats={prev && prev.joinStats ? { ...prev.joinStats, days: prev.days } : null} /> : <>
      <button type="button" className="qh-mxsum" onClick={() => setMore(m => !m)} aria-expanded={more}>
        <span className="qh-mxsum-t">{mxSummary(f)}</span><span className="qh-linkbtn">{more ? 'Done' : 'Change settings'}</span>
      </button>
      {more && <MxOptions f={f} set={set} norms={n} fleet={isFleet} table={f.table} joinStats={prev && prev.joinStats ? { ...prev.joinStats, days: prev.days } : null} />}
      </>}

      {prev && (
        <div className="qh-mxprev">
          {prev.error && <div className="qh-mxprev-idle">Could not build a preview. The exemption is still what the sentence above says.</div>}
          {!prev.error && !prev.seen && !prev.wide && <div className="qh-mxprev-idle">No query read <code>{f.table}</code> in the last {prev.days} days, so there is no real row to show.</div>}
          {prev.wide && <div className="qh-mxprev-idle">This reaches <b>{prev.tables}</b> tables and <b>{prev.maskedColumns}</b> columns that are masked today. Too wide for one sample row — that count is the preview.</div>}
          {prev.seen && (
            <div className="qh-mxdiffwrap">
              {picked.length > 1 && <div className="qh-mxprev-idle">Shown for <code>{preview.column}</code>; the other {picked.length - 1} change the same way.</div>}
              <div className="qh-mxprev-q"><code>{prev.sql}</code><span className="qh-mxprev-by">{prev.by} · {qhAgo(prev.at)}</span></div>
              <div className="qh-tablewrap">
                <table className="qh-mxdiff">
                  <thead><tr><th />{prev.columns.map(c => <th key={c} className={c === preview.column ? 'is-t' : ''}>{c}</th>)}</tr></thead>
                  <tbody>
                    <tr><td className="qh-mxdiff-k">Today</td>{prev.columns.map(c => <td key={c} className={c === preview.column ? 'is-t is-masked' : ''}>{prev.before[c]}</td>)}</tr>
                    <tr><td className="qh-mxdiff-k">After</td>{prev.columns.map(c => <td key={c} className={c === preview.column ? 'is-t is-clear' : ''}>{prev.after[c]}</td>)}</tr>
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {err && <div className="qh-roleform-err">{err.msg}</div>}
      <div className="qh-mxacts is-form">
        <button className="qh-btn qh-btn-sm" onClick={onDone}>Cancel</button>
        <button className="qh-btn qh-btn-sm" disabled={!readyAll || prevBusy} onClick={runPreview}>{prevBusy ? 'Checking…' : prev ? 'Check again' : 'Show what this changes'}</button>
        <MxSave label={saveLabel} why={why} danger={wide} busy={busy} onClick={save} />
      </div>
    </div>
  );
}

// ---------- Empty state ----------
function MxEmpty({ canWrite, onStart, meta }) {
  const rules = meta.nameRules, dets = (meta.valueDetectors || []).length;
  return (
    <div className="qh-roleempty">
      <div className="qh-roleempty-h">Nothing is exempt from masking</div>
      <p className="qh-roleempty-p">QueryHub masks personal data two ways: <b>{rules ? rules + ' name rules' : 'name rules'}</b> (a column called <code>email</code> holds an email) and <b>{dets ? dets + ' value detectors' : 'value detectors'}</b> reading the cell itself. Both over-reach — a column called <code>address</code> may hold a wallet. An exemption is how you correct one.</p>
      <dl className="qh-roledefs">
        {QH_MX_ORDER.map(k => <div key={k}><dt>{QH_MX_RUNGS[k].label}</dt><dd>{QH_MX_RUNGS[k].w}</dd></div>)}
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
  const [fServer, setFServer] = useMx('');
  const [fDb, setFDb] = useMx('');
  const [adding, setAdding] = useMx(false);
  const [formKey, setFormKey] = useMx(0);
  const [seed, setSeed] = useMx(null);
  const [openId, setOpenId] = useMx(null);
  const padRef = React.useRef(null);
  const all = st.maskExemptions || [];
  const meta = st.maskMeta || {};
  const moot = meta.maskingEnabled === false;
  const norms = mxNorms(all);

  // Filter options: every connection the admin can see, plus any a row names
  // that is not in that list — so a server with no exemptions yet is still a
  // place you can filter to and add from. Pick-only, like every picker here.
  const serverOpts = (() => {
    const m = new Map();
    (st.connections || []).forEach(c => m.set(c.id, { value: c.id, label: c.name, n: 0 }));
    all.forEach(e => {
      if (e.scope === 'fleet' || !e.connectionId) return;
      if (!m.has(e.connectionId)) m.set(e.connectionId, { value: e.connectionId, label: e.connectionName || e.connectionId, n: 0 });
      m.get(e.connectionId).n++;
    });
    return [...m.values()].sort((a, b) => a.label.localeCompare(b.label)).map(o => ({ value: o.value, label: o.label, hint: o.n ? String(o.n) : null }));
  })();
  const dbOpts = (() => {
    if (!fServer) return [];
    const m = new Map();
    const c = (st.connections || []).find(x => x.id === fServer);
    ((c && c.databases) || []).forEach(d => m.set(d.id, { value: d.id, label: d.name, n: 0 }));
    all.forEach(e => {
      if (e.connectionId !== fServer || !e.databaseId || e.scope === 'server' || e.scope === 'fleet') return;
      if (!m.has(e.databaseId)) m.set(e.databaseId, { value: e.databaseId, label: e.databaseName || e.databaseId, n: 0 });
      m.get(e.databaseId).n++;
    });
    return [...m.values()].sort((a, b) => a.label.localeCompare(b.label)).map(o => ({ value: o.value, label: o.label, hint: o.n ? String(o.n) : null }));
  })();

  // A place filter shows what APPLIES there: rows on it, and the wider rows
  // above it (the server's own row, a fleet-wide row) — they reach it too.
  const inPlace = (e) => {
    if (!fServer) return true;
    if (e.scope === 'fleet') return true;
    if (e.connectionId !== fServer) return false;
    if (!fDb || e.scope === 'server') return true;
    return e.databaseId === fDb;
  };
  const match = (e) => {
    const t = q.trim().toLowerCase();
    if (!t) return true;
    return [e.schema, e.table, e.column, e.reason, e.connectionName, e.databaseName].filter(Boolean).join(' ').toLowerCase().includes(t);
  };
  const keeps = { all: () => true, wide: (e) => mxWide(e.scope), off: (e) => !e.enabled, gone: (e) => !!e.missing };
  const base = all.filter(inPlace).filter(match);
  const count = (k) => base.filter(keeps[k]).length;
  const rows = base.filter(keeps[filter] || keeps.all);

  // Sorted by server and database, grouped under ONE label each.
  const groups = (() => {
    const m = new Map();
    rows.slice()
      .sort((x, y) => {
        const k = (r) => (r.scope === 'fleet' ? '' : (r.connectionName || r.connectionId || '') + '\u0000' + (r.scope === 'server' ? '' : (r.databaseName || r.databaseId || '')));
        if (k(x) !== k(y)) return k(x) > k(y) ? 1 : -1;
        const w = (r) => (mxWide(r.scope) ? 0 : 1);
        if (w(x) !== w(y)) return w(x) - w(y);
        return mxWhat(x) > mxWhat(y) ? 1 : -1;
      })
      .forEach(e => {
        const key = e.scope === 'fleet' ? 'every server'
          : (e.connectionName || e.connectionId) + (e.scope === 'server' ? '' : ' / ' + (e.databaseName || e.databaseId));
        if (!m.has(key)) m.set(key, { key, rows: [], seed: e });
        m.get(key).rows.push(e);
      });
    return [...m.values()];
  })();

  const start = (s) => {
    setSeed(s || null); setAdding(true); setOpenId(null); setFormKey(k => k + 1);
    if (padRef.current) padRef.current.scrollTop = 0;
  };
  // The main button starts from what the filters say: a server sets the server
  // and waits in Database; a server and a database set both, pick one column,
  // and wait in Schema. No filter, no seed.
  const startFromFilter = () => start(fServer && fDb ? { connectionId: fServer, databaseId: fDb, scope: 'column', focus: 'schema' }
    : fServer ? { connectionId: fServer, scope: 'column', focus: 'database' } : null);
  const remove = (e) => {
    if (!window.confirm('Remove this exemption? QueryHub masks ' + (e.column ? e.table + '.' + e.column : e.connectionName) + ' again, and the reason goes with the row. To keep the record, disable it.')) return;
    st.removeMaskExemption(e.id);
  };
  // Replace = the same target, pre-filled, with the original attached: Save
  // patches it while the reach is unchanged, and disables it after a new reach.
  const replace = (e) => start({ connectionId: e.connectionId, databaseId: e.databaseId, scope: e.scope, schema: e.schema, table: e.table, column: e.column, strength: e.strength, survivesJoin: e.survivesJoin, replaceOf: e });
  const addHere = (e) => start({ connectionId: e.connectionId, databaseId: e.databaseId, scope: 'column', schema: e.schema, table: e.table, strength: e.strength, survivesJoin: e.survivesJoin, focus: 'columns' });
  const addInGroup = (g) => start(g.seed.scope === 'server'
    ? { connectionId: g.seed.connectionId, scope: 'column', focus: 'database' }
    : { connectionId: g.seed.connectionId, databaseId: g.seed.databaseId, scope: 'column', focus: 'schema' });
  const groupPlace = (g) => g.seed.scope === 'server' ? (g.seed.connectionName || g.seed.connectionId) : (g.seed.databaseName || g.seed.databaseId);

  return (
    <div className="qh-apad" ref={padRef}>
      <div className="qh-aview-head">
        <div><div className="qh-aview-title">Masking exemptions</div>
          <div className="qh-aview-sub">Where masking is switched off, and why — {all.length} row{all.length === 1 ? '' : 's'}</div></div>
        {canWrite && !adding && all.length > 0 && <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={startFromFilter}><MxIcon.plus />Add an exemption</button>}
      </div>

      {moot && <div className="qh-rolenote is-warn"><MxIcon.warn /><span>Masking is switched off fleet-wide in <a href="#admin/config">System configuration</a> — nothing on this screen is having any effect right now.</span></div>}
      {!canWrite && <div className="qh-rolenote"><MxIcon.lock />Masking exemptions are super-admin only — this is the one screen whose purpose is to reduce protection.</div>}

      {adding && canWrite && <MxForm key={formKey} st={st} seed={seed} norms={norms} onDone={() => { setAdding(false); setSeed(null); }} />}

      {all.length === 0 && !adding && <MxEmpty canWrite={canWrite} onStart={() => start(null)} meta={meta} />}

      {all.length > 0 && (
        <>
          <div className="qh-mxtools">
            <div className="qh-search sm">
              <svg className="qh-search-ic" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="7" /><path d="M21 21l-4.3-4.3" /></svg>
              <input className="qh-search-in" placeholder="Table, column or reason…" value={q} onChange={e => setQ(e.target.value)} />
              {q && <button className="qh-search-x" onMouseDown={e => { e.preventDefault(); setQ(''); }} aria-label="Clear"><MxIcon.x /></button>}
            </div>
            <MxCombo small ariaLabel="Server filter" value={fServer} placeholder="All servers" options={serverOpts}
              onPick={v => { setFServer(v); setFDb(''); }} onClear={() => { setFServer(''); setFDb(''); }} />
            <MxCombo small ariaLabel="Database filter" key={'db-' + fServer} disabled={!fServer} value={fDb} placeholder="All databases" options={dbOpts}
              onPick={setFDb} onClear={() => setFDb('')} />
            <div className="qh-seg qh-seg-sm">
              {[['all', 'All'], ['wide', 'Wider than a column'], ['off', 'Disabled'], ['gone', 'Matching nothing']].map(([v, l]) => (
                <button key={v} className={'qh-seg-opt' + (filter === v ? ' is-active' : '')} onClick={() => setFilter(v)}>{l}<span className="qh-mxseg-n">{count(v)}</span></button>
              ))}
            </div>
            <span className="qh-mxlegend" title="What most rows use. A row shows a chip only where it differs.">Defaults: {mxNormShort(norms)}</span>
          </div>

          {groups.map(g => (
            <div key={g.key} className="qh-mxgroup">
              <div className="qh-mxgroup-h">
                <span className="qh-mxgroup-n">{g.key}</span>
                <span className="qh-mxgroup-c">{g.rows.length}</span>
                {canWrite && g.seed.scope !== 'fleet' && (
                  <button className="qh-mxadd" title={'Add in ' + groupPlace(g)} aria-label={'Add in ' + groupPlace(g)} onClick={() => addInGroup(g)}><MxIcon.plus /></button>
                )}
              </div>
              <div className="qh-mxlist">{g.rows.map(e => (
                <MxItem key={e.id} e={e} canWrite={canWrite} moot={moot} norms={norms} st={st}
                        open={openId === e.id} onToggleOpen={() => setOpenId(x => (x === e.id ? null : e.id))}
                        onRemove={remove} onReplace={replace} onAddHere={addHere}
                        onAlso={(row, a) => start({ connectionId: a.connectionId, databaseId: a.databaseId, scope: 'column', schema: row.schema, table: row.table, column: row.column, strength: row.strength, survivesJoin: row.survivesJoin })} />
              ))}</div>
            </div>
          ))}
          {rows.length === 0 && <div className="qh-conn-empty">{fServer && !q && filter === 'all' ? 'Nothing is exempt here yet.' : 'No exemptions match your filter.'}</div>}
        </>
      )}
    </div>
  );
}

Object.assign(window, { MaskingView, MxReach, QH_MX_RUNGS });
