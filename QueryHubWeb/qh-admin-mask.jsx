// QueryHub Admin — Masking exemptions (design brief 2026-09-09, simplified 2026-09-15).
//
// What this screen exists for: masking decides two ways and both over-reach.
// NAME rules say a column called `address` holds a postal address; VALUE
// detectors read the cell. A database is full of words that mean something else
// in context — `address` is a wallet, `name` is a venue, `table_name` is
// catalog metadata — and when a rule misfires the analyst gets [REDACTED] where
// they needed the value. The correction is an exemption.
//
// 2026-09-15 — THE MODEL WAS NEVER THE COMPLICATED PART. The first cut printed
// the whole model on every row and asked every question on every add, so a
// screen whose real content is "30 columns are exempt, here is why" read as
// thirty paragraphs and a nine-part form. Three rules now:
//   1. A ROW SAYS WHAT IS TRUE OF IT ALONE. 27 of 30 are one column, soft,
//      re-masked on join, everyone — that is the default, and a row only prints
//      what DIFFERS from it. The paragraph still exists; it is one click away,
//      in the row's own detail. Absence of a chip is information now.
//   2. THE DEFAULTS ARE NOT QUESTIONS. Soft and re-mask-on-join are right 27
//      times out of 30, so they are stated as a summary line with one Change,
//      not two two-option interrogations every time.
//   3. WHAT CAN BE FIXED IN PLACE, IS. The consequences and the reason are
//      editable on the row (PATCH) — they are corrections to a decision about
//      a target that has not changed. The REACH is still immutable: an
//      exemption whose reason describes its old scope is worse than two rows.
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
};

// The ladder, each rung wider than the last. Rendered in this order everywhere —
// the order IS the argument, and shuffling it in one place would undo it. The
// argument used to be made by printing all five widths at once; it is now made
// by the ONE sentence under the picked rung, which is the rung the reader is
// actually deciding about.
//
// `table` and `fleet` were added in the 2026-09-09 (c) round: without `table`,
// somebody who wants one table has to pick `schema`, which is WIDER — a missing
// rung that forces the wider choice is the exact failure the ladder prevents.
// `fleet` exists because one of the 30 live exemptions IS fleet-wide, and a
// reach the screen cannot produce gets typed into psql instead.
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
const mxTarget = (e) => (e.schema ? e.schema + '.' : '') + (e.table ? e.table + '.' : '') + (e.column || '');
// What the row names, with no trailing dot and no half-path: a schema-wide row
// says the schema, a fleet-wide row says nothing here because the line already
// opens with "every server". `dba.` was a path with its own tail cut off.
const mxWhat = (e) => {
  // The group label above the row already says which server and database, so the
  // row says what is exempt INSIDE it — and every rung has an answer, computed
  // from the scope rather than from whether a field happens to be set. A
  // caller-side fallback for the no-target rungs is how the `database` rung
  // printed "every server", the widest claim on the screen, on a row whose own
  // sentence said otherwise: the widest reach must never be what an empty field
  // produces (2026-09-07 §3), and that holds for the LABEL too.
  if (e.scope === 'fleet') return e.schema ? 'schema ' + e.schema : 'every schema';
  if (e.scope === 'server') return 'every database';
  if (e.scope === 'database') return 'every table';
  if (e.column) return (e.schema ? e.schema + '.' : '') + (e.table ? e.table + '.' : '') + e.column;
  if (e.table) return (e.schema ? e.schema + '.' : '') + e.table;
  return e.schema ? 'schema ' + e.schema : 'every table';
};
// The default an exemption is written with, and the reason a row can be one
// line: everything below is what 27 of the 30 live rows say, so saying it is
// saying nothing. A row that differs prints the difference.
const mxIsDefault = (e) => (e.strength || 'soft') === 'soft' && !e.survivesJoin && (e.audience || 'everyone') === 'everyone';

// The reach, as a sentence. Same job as `RoleSentence` on the Roles screen and
// the same reason: three chips make the reader assemble the meaning, and on a
// screen about reducing protection the meaning is the whole row.
function MxReach({ e }) {
  const conn = <b>{e.connectionName || e.connectionId}</b>;
  const db = <b>{e.databaseName || e.databaseId}</b>;
  // Fleet-wide is read from the SCOPE, never from a missing connection: the
  // widest row in the model must not be the thing a blank field produces.
  if (e.scope === 'fleet') return <div className="qh-mxsent">{e.schema
    ? <>Unmasks <b>every column in schema {e.schema}</b> — <b>every database on every server</b>.</>
    : <>Turns masking off <b>across the whole fleet</b> — every database on every server.</>}</div>;
  if (e.scope === 'server') return <div className="qh-mxsent">Turns masking off for <b>every database on {e.connectionName || e.connectionId}</b>.</div>;
  if (e.scope === 'database') return <div className="qh-mxsent">Turns masking off for <b>everything read in {e.databaseName || e.databaseId}</b>, on {conn}.</div>;
  if (e.scope === 'schema') return <div className="qh-mxsent">Unmasks <b>every column in schema {e.schema}</b> — {db} on {conn}.</div>;
  if (e.scope === 'table') return <div className="qh-mxsent">Unmasks <b>every column in {e.schema}.{e.table}</b> — {db} on {conn}.</div>;
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
// The three settings in three words each, for the summary line that replaces
// asking them. Reading it has to be faster than opening it, or it is just a
// closed form.
function mxSummary(e) {
  return [
    e.strength === 'full' ? 'Masking off entirely' : 'Soft — values still checked',
    e.survivesJoin ? 'Stays exempt in joins' : 'Re-masked on joins',
    e.audience === 'super' ? 'Super-admins only' : 'Everyone',
  ].join(' · ');
}

// The default a row is measured against is READ FROM THE ROWS, not assumed. The
// first cut hardcoded "soft, re-masked, everyone" from a 27-of-30 count, and on
// the real fleet most rows are `full` — so `NO MASKING` printed in red on nearly
// every row, which is how a chip stops being information and becomes noise. The
// majority answer per field is stated once above the list; a row chips only
// where it disagrees, so the rule holds whatever the fleet's mix turns out to be.
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
function mxNormLine(n) {
  return [
    n.strength === 'full' ? 'masking off entirely' : 'soft — values still checked',
    n.survivesJoin ? 'still exempt in joins' : 're-masked on joins',
    n.audience === 'super' ? 'super-admins only' : 'everyone who can read it',
  ].join(' · ');
}

// What differs from the norm, and nothing else. Absence of a chip is
// information. Short words and `nowrap`, because chips that wrap put the stacked
// lines straight back — each carries its full meaning on hover, and the row's
// detail spells all of them out in sentences.
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
      ? <span className="qh-mxchip is-aud" title="Only super-admins see it unmasked; everyone else still gets the mask">super only</span>
      : <span className="qh-mxchip" title="Everyone who can read the table sees it unmasked">everyone</span>)}
    {!e.enabled && <span className="qh-mxchip is-off" title="Turned off — this column is masked again, and the record is kept">off</span>}
    {e.missing && <span className="qh-mxchip is-gone" title="Still enforced, but that table or column is not in the catalog any more">matches nothing</span>}
  </>;
}

// ---------- The three settings, shared by add and edit ----------
// One component, because a setting asked one way when it is created and another
// way when it is corrected is two sets of rules for one field.
function MxOptions({ f, set, joinStats, table }) {
  return (
    <div className="qh-mxsettings">
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
        {joinStats && <div className="qh-mxevidence">Of the {joinStats.total} queries against <code>{table}</code> in the last {joinStats.days} days, <b>{joinStats.joined}</b> joined another table.</div>}
      </div>
      <label className="qh-rolefield"><span className="qh-rolefield-l">Who it applies to</span>
        <select className="qh-select" style={{ maxWidth: 260 }} value={f.audience} onChange={e => set({ audience: e.target.value })}>
          <option value="everyone">Everyone who can read it</option>
          <option value="super">Super-admins only</option>
        </select></label>
    </div>
  );
}

// ---------- Editing one in place ----------
// Reach is absent from this form on purpose, and the line says so with the
// action that does do it. Offering a disabled scope picker would imply the
// restriction is a limitation rather than the decision it is.
function MxEdit({ e, st, onDone, onReplace }) {
  const [f, setF] = useMx({ strength: e.strength || 'soft', survivesJoin: !!e.survivesJoin, audience: e.audience || 'everyone', reason: e.reason || '' });
  const [more, setMore] = useMx(false);
  const [busy, setBusy] = useMx(false);
  const [err, setErr] = useMx(null);
  const set = (patch) => { setF(x => ({ ...x, ...patch })); setErr(null); };
  const dirty = f.strength !== (e.strength || 'soft') || f.survivesJoin !== !!e.survivesJoin
    || f.audience !== (e.audience || 'everyone') || f.reason.trim() !== (e.reason || '');
  const save = () => {
    if (busy || !dirty || !f.reason.trim()) return;
    setBusy(true);
    st.updateMaskExemption(e.id, { strength: f.strength, survivesJoin: f.survivesJoin, audience: f.audience, reason: f.reason.trim() })
      .then(() => { setBusy(false); onDone(); })
      .catch(x => { setBusy(false); setErr((x && x.message) || 'Could not save the change.'); });
  };
  return (
    <div className="qh-mxeditbox">
      <div className="qh-mxeditwhat"><MxReach e={e} /><span className="qh-mxfixed">Where it reaches is fixed — <button className="qh-linkbtn" onClick={() => onReplace(e)}>replace it</button> to change that.</span></div>
      <label className="qh-rolefield"><span className="qh-rolefield-l">Why it is exempt</span>
        <textarea className="qh-input qh-mxreason-in" rows={2} value={f.reason} onChange={x => set({ reason: x.target.value })} /></label>
      <button type="button" className="qh-mxsum" onClick={() => setMore(m => !m)}>
        <span className="qh-mxsum-t">{mxSummary(f)}</span><span className="qh-linkbtn">{more ? 'Done' : 'Change'}</span>
      </button>
      {more && <MxOptions f={f} set={set} />}
      {err && <div className="qh-roleform-err">{err}</div>}
      <div className="qh-accedit-acts">
        <button className="qh-btn qh-btn-sm" onClick={onDone}>Cancel</button>
        <button className="qh-btn qh-btn-sm qh-btn-primary" disabled={busy || !dirty || !f.reason.trim()} onClick={save}>{busy ? 'Saving…' : 'Save changes'}</button>
      </div>
      {!f.reason.trim() && <div className="qh-mxnote">A reason is required — an exemption without one is unreadable to whoever finds it next.</div>}
    </div>
  );
}

// ---------- One row ----------
// TWO lines, not one and not eight. The one-line version fixed the eight-line
// version and created its own problem: target, chips, a 200-character reason and
// a timestamp on one line, so the reason — the most useful sentence on the row
// — was the thing that got truncated, and the line was too long to scan.
//
// Now the row is a heading and a caption: what is exempt (mono, the column the
// eye runs down) with its exceptions, and the reason underneath in quieter type,
// one line, full text one click away. The server and database are NOT repeated
// here — the list is sorted by them and `MxGroup` prints each one once.
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
            ? <MxEdit e={e} st={st} onDone={() => setEditing(false)} onReplace={onReplace} />
            : <>
              <MxReach e={e} />
              <div className="qh-mxlines">
                <span>{mxStrengthLine(e)}</span>
                <span>{mxJoinLine(e)}</span>
                {e.audience === 'super' && <span>Only super-admins see it unmasked; everyone else still gets the mask.</span>}
              </div>
              {/* Why the column is masked in the first place. Half the exemptions
                  written are for a column somebody was surprised to see masked,
                  and the surprise comes from not knowing which rule fired. */}
              {e.maskedBy && <div className="qh-mxwhy">Caught by the name rule <code>{e.maskedBy.key}</code> — {e.maskedBy.label}, {e.maskedBy.mask === 'full' ? 'fully' : 'partially'} masked.</div>}
              {/* A schema changed underneath it. Still enforced, matching
                  nothing — a different thing from being turned off. */}
              {e.missing && <div className="qh-mxwhy is-gone">That table or column is not in the catalog any more. The exemption is still enforced; it matches nothing today.</div>}
              {e.reason && <div className="qh-mxreason">“{e.reason}”</div>}
              <div className="qh-mxmeta">{qhPersonName(e.createdBy)}{e.createdAt ? ' · ' + qhAgo(e.createdAt) : ''}{e.updatedBy ? ' · edited by ' + qhPersonName(e.updatedBy) + (e.updatedAt ? ' ' + qhAgo(e.updatedAt) : '') : ''}</div>
              {/* The same database usually lives on more than one server, and an
                  exemption on one is routinely missing on the other. A
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
                      exposure, so it stays one action and never behind the form.
                      It does not delete: an exemption that was a mistake is
                      itself a fact worth keeping. */}
                  <button className="qh-btn qh-btn-sm" onClick={() => st.setMaskExemptionEnabled(e.id, !e.enabled)}>{e.enabled ? 'Turn off' : 'Turn back on'}</button>
                  <button className="qh-btn qh-btn-sm" onClick={() => setEditing(true)}>Edit</button>
                  {/* Standing at a table is the moment the next exemption gets
                      written — four of the live rows are sibling columns of one
                      table, each of which cost a walk back through five
                      pickers. This lands in the form with everything but the
                      column already answered. */}
                  {e.table && <button className="qh-linkbtn" onClick={() => onAddHere(e)}>+ another column in {e.table}</button>}
                  <button className="qh-linkbtn" onClick={() => onReplace(e)}>Replace</button>
                  <button className="qh-mxdel" onClick={() => onRemove(e)}>Remove</button>
                </> : <span className="qh-rolelock"><MxIcon.lock />read-only</span>}
              </div>
            </>}
        </div>
      )}
    </div>
  );
}

// ---------- The add flow ----------
// Three steps: what stops being masked, why, and the settings that already have
// the right answer. The rung is an explicit CHOICE and never the result of
// leaving a field blank — that is still the single most important thing about
// this form, because the text-field version it replaced reached "whole server"
// exactly that way.
// ---------- A picker you can type into ----------
// The server, database, schema, table and column pickers were plain selects:
// right that they only accept what the catalog holds, slow on a server with
// hundreds of databases or a table with two hundred columns. This keeps the
// first property and fixes the second — typing FILTERS the list (starts-with
// first, then contains), ↑/↓ and Enter pick, Escape puts the last answer back.
// It still never accepts free text: leaving the field without picking keeps
// what was there, so a typo cannot become an exemption that matches nothing.
// The list is portalled and placed from the input, so no scrolling parent
// clips it, and it opens upward when there is no room below.
function MxCombo({ value, options, onPick, placeholder, disabled, clearOnPick, wide }) {
  const [q, setQ] = useMx(null);          // null = not typing; shows the picked label
  const [hi, setHi] = useMx(0);
  const [pos, setPos] = useMx(null);
  const inRef = React.useRef(null), listRef = React.useRef(null);
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
    setPos({ left: r.left, width: Math.max(r.width, wide ? 380 : 300), top: up ? r.top - 4 : r.bottom + 4, up, max: Math.max(160, Math.min(300, (up ? r.top : below) - 16)) });
  };
  const close = () => { setPos(null); setQ(null); setHi(0); };
  const pick = (o) => { if (!o) return; onPick(o.value); close(); if (clearOnPick && inRef.current) inRef.current.focus(); };
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
  return (
    <span className={'qh-mxcombo' + (wide ? ' is-wide' : '')}>
      <input ref={inRef} className="qh-input qh-mxcombo-in" disabled={disabled} placeholder={placeholder} spellCheck={false} autoComplete="off"
        role="combobox" aria-expanded={open} aria-autocomplete="list"
        value={q != null ? q : (clearOnPick ? '' : (cur ? cur.label : ''))}
        onFocus={() => { if (!open) place(); }} onClick={() => { if (!open) place(); }}
        onChange={e => { setQ(e.target.value); setHi(0); if (!open) place(); }} onKeyDown={key} />
      <svg className="qh-mxcombo-caret" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M6 9l6 6 6-6" /></svg>
      {open && ReactDOM.createPortal(
        <div ref={listRef} className={'qh-mxcombo-list' + (pos.up ? ' is-up' : '')} role="listbox" style={{ left: pos.left, top: pos.top, width: pos.width, maxHeight: pos.max }}>
          {shown.length === 0 && <div className="qh-mxcombo-none">{options.length ? 'Nothing matches “' + q.trim() + '”.' : 'Nothing to pick here.'}</div>}
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

function MxForm({ st, seed, onDone }) {
  const conns = (st.connections || []).filter(c => c.enabled !== false);
  const [f, setF] = useMx(() => ({
    connectionId: (seed && seed.connectionId) || '', databaseId: (seed && seed.databaseId) || '',
    scope: (seed && seed.scope) || 'column', schema: (seed && seed.schema) || '', table: (seed && seed.table) || '', column: '',
    columns: (seed && seed.column) ? [seed.column] : [],
    strength: (seed && seed.strength) || 'soft', survivesJoin: !!(seed && seed.survivesJoin),
    audience: 'everyone', reason: '', confirmed: false, typed: '',
  }));
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
  const col = columns.find(c => c.name === (f.columns && f.columns.length === 1 ? f.columns[0] : f.column)) || null;
  // Several columns of ONE table, in one pass. Four of the live rows are sibling
  // columns of the same table with the same reason, each of which cost a walk
  // back through five pickers; `crypto_transactions.from_address` and `.to_address`
  // are the same decision typed twice. This is NOT bulk edit across rows — it is
  // one target, one reason, one set of consequences, written once per column,
  // and the reach is still picked explicitly at every level above it.
  const picked = f.scope === 'column' ? (f.columns || []) : [];
  const addCol = (name) => { if (name && picked.indexOf(name) < 0) set({ columns: picked.concat([name]), column: '' }); };
  const dropCol = (name) => set({ columns: picked.filter(c => c !== name) });

  const wide = mxWide(f.scope);
  const needsConfirm = f.scope === 'database' || f.scope === 'server';
  // Fleet-wide is the only rung that cannot be reached by clicking a rung and
  // the only confirmation that cannot be clicked: a checkbox is enough for
  // "this server" because there is a server name on screen to read, and there
  // is nothing to read for the fleet — so the confirmation IS the sentence.
  const FLEET_PHRASE = 'every server';
  const needsType = f.scope === 'fleet';
  const typedOk = !needsType || f.typed.trim().toLowerCase() === FLEET_PHRASE;
  // A prefilled target (Add it there, from a sibling server's suggestion) can
  // name a column the catalog snapshot does not have. "Picked from the catalog,
  // never typed" has to hold on the prefill path too, or the rule is only true
  // of the paths nobody worries about.
  // A prefilled column (Add it there, from a sibling server) can name something
  // the snapshot does not have. "Picked, never typed" has to hold on the prefill
  // path too, or the rule is only true of the paths nobody worries about.
  const gonePicked = f.scope === 'column' && !!cat && !!table ? picked.filter(c => !columns.some(x => x.name === c)) : [];
  const colGone = gonePicked.length > 0;
  const ready = !!f.connectionId
    && (f.scope === 'server' || !!f.databaseId)
    && (f.scope !== 'schema' || !!f.schema)
    && (f.scope !== 'table' || (!!f.schema && !!f.table))
    && (f.scope !== 'column' || (!!f.schema && !!f.table && picked.length > 0));
  const readyAll = f.scope === 'fleet' ? true : ready;
  const bad = !readyAll || colGone || !f.reason.trim() || (needsConfirm && !f.confirmed) || !typedOk;

  const preview = { connectionId: f.connectionId, databaseId: f.databaseId, scope: f.scope, schema: f.schema, table: f.table, column: picked[0] || f.column };
  const runPreview = () => {
    if (!readyAll || prevBusy) return;
    setPrevBusy(true);
    st.maskPreview(preview).then(r => { setPrev(r); setPrevBusy(false); }).catch(() => { setPrev({ error: true }); setPrevBusy(false); });
  };

  const save = () => {
    if (bad || busy) return;
    setBusy(true); setErr(null);
    const settings = { strength: f.strength, survivesJoin: f.survivesJoin, audience: f.audience, reason: f.reason.trim() };
    const bodies = f.scope === 'column'
      ? picked.map(c => ({ ...preview, column: c, ...settings }))
      : [{ ...preview, ...settings }];
    // One at a time, and it stops at the first refusal rather than firing the
    // rest: these are writes that reduce protection, so a partial result has to
    // say exactly which ones landed instead of leaving the operator to guess.
    bodies.reduce((chain, b, i) => chain.then(() => st.addMaskExemption(b).catch(e => {
      const done = i > 0 ? ' The first ' + i + ' were written.' : '';
      throw { message: ((e && e.message) || 'Could not add the exemption.') + (f.scope === 'column' && picked.length > 1 ? ' Stopped at ' + b.column + '.' + done : ''), code: e && e.code };
    })), Promise.resolve())
      .then(() => { setBusy(false); onDone(); })
      .catch(e => { setBusy(false); setErr({ msg: (e && e.message) || 'Could not add the exemption.', code: e && e.code }); });
  };

  // The button says the consequence, in the words of the rung. "Save" on a
  // control that turns masking off for a production server is the one label
  // that would make the wide rungs feel like the narrow ones.
  const saveLabel = !readyAll ? 'Add exemption'
    : f.scope === 'fleet' ? 'Turn masking off across the whole fleet'
      : f.scope === 'server' ? 'Turn masking off for all of ' + (conn ? conn.name : '')
        : f.scope === 'database' ? 'Turn masking off for ' + (db ? db.name : '')
          : f.scope === 'schema' ? 'Unmask every column in ' + f.schema
            : f.scope === 'table' ? 'Unmask every column in ' + f.table
              : picked.length > 1 ? 'Unmask ' + picked.length + ' columns in ' + f.table
                : 'Unmask ' + f.table + '.' + (picked[0] || f.column);

  const asRow = { ...preview, connectionName: conn ? conn.name : '', databaseName: db ? db.name : '',
    strength: f.strength, survivesJoin: f.survivesJoin };
  const pickSchema = f.scope === 'column' || f.scope === 'table' || f.scope === 'schema' || f.scope === 'fleet';

  return (
    <div className="qh-mxform">
      <div className="qh-mxstep">
        <span className="qh-mxstep-n">1</span>
        <span className="qh-mxstep-t">What stops being masked</span>
      </div>
      {/* The ladder as one row, widest last. Five cards showing five widths at
          once made the reader compare all of them before picking one; the
          sentence underneath states the width of the rung they actually picked,
          which is the only one they are deciding about. */}
      <div className="qh-mxscope">
        <div className="qh-seg qh-seg-sm">
          {QH_MX_ORDER.map(k => (
            <button key={k} type="button" className={'qh-seg-opt' + (f.scope === k ? ' is-active' : '') + (mxWide(k) ? ' is-wide' : '')}
                    onClick={() => set({ scope: k, confirmed: false, typed: '', audience: f.audience === 'super' && f.scope === 'fleet' ? 'everyone' : f.audience,
                      table: (k === 'column' || k === 'table') ? f.table : '',
                      columns: k === 'column' ? (f.columns || []) : [], column: '',
                      schema: (k === 'column' || k === 'table' || k === 'schema') ? f.schema : '' })}>{QH_MX_RUNGS[k].label}</button>
          ))}
        </div>
        {f.scope !== 'fleet'
          ? <button type="button" className="qh-linkbtn qh-mxfleetlink" onClick={() => set({ scope: 'fleet', confirmed: false, typed: '', connectionId: '', databaseId: '', table: '', column: '', audience: 'super' })}>…or every server</button>
          : <button type="button" className="qh-linkbtn qh-mxfleetlink" onClick={() => set({ scope: 'column', typed: '', audience: 'everyone' })}>back to one server</button>}
      </div>
      {/* Off the ladder rather than a sixth segment: it is wider than the top
          rung of a ladder built to make width feel wide, and it should not sit
          in the same row as "one column". */}
      {f.scope === 'fleet' && (
        <div className="qh-mxfleetbox">
          <MxIcon.warn />
          <span>Wider than any rung above. One of the thirty live exemptions is this shape — it starts restricted to super-admins.</span>
        </div>
      )}
      <div className="qh-mxwidth">{QH_MX_RUNGS[f.scope].w}</div>

      {/* Table and column are PICKED from the catalog, never typed — 156,000
          known columns, and a typo writes an exemption that silently matches
          nothing. Each column shows what currently masks it, which is the
          question that brought most people to this screen.
          A picker appears when the one above it has an answer: five selects
          reading "pick a server first" are five controls that do nothing, and
          this form is meant to be four decisions, not a wall. */}
      <div className="qh-mxfields">
        <label className="qh-rolefield"><span className="qh-rolefield-l">Server</span>
          <MxCombo disabled={f.scope === 'fleet'} value={f.connectionId} placeholder={f.scope === 'fleet' ? 'Every server' : 'Type or pick a server…'}
            options={conns.map(c => ({ value: c.id, label: c.name, hint: c.env || null }))}
            onPick={v => set({ connectionId: v, databaseId: '', schema: '', table: '', column: '' })} /></label>
        {f.scope !== 'server' && f.scope !== 'fleet' && !!conn && <label className="qh-rolefield"><span className="qh-rolefield-l">Database</span>
          <MxCombo value={f.databaseId} placeholder="Type or pick a database…"
            options={dbs.map(d => ({ value: d.id, label: d.name }))}
            onPick={v => set({ databaseId: v, schema: '', table: '', column: '' })} /></label>}
        {pickSchema && (f.scope === 'fleet' || !!f.databaseId) && <label className="qh-rolefield"><span className="qh-rolefield-l">Schema{f.scope === 'fleet' && <span className="qh-accedit-hint"> — optional</span>}</span>
          {f.scope === 'fleet'
            // No catalog to read across the fleet, and the live fleet-wide row
            // names a schema (`dba`) that exists on every server — so this one
            // rung takes a typed schema name. The exception the rule earns.
            ? <input className="qh-input" placeholder="e.g. dba — empty for every schema" value={f.schema} onChange={e => set({ schema: e.target.value })} />
            : <MxCombo disabled={!cat} value={f.schema} placeholder={cat ? 'Type or pick a schema…' : 'Loading catalog…'}
              options={schemas.map(x => ({ value: x.name, label: x.name }))}
              onPick={v => set({ schema: v, table: '', column: '' })} />}</label>}
        {(f.scope === 'column' || f.scope === 'table') && !!schema && <label className="qh-rolefield"><span className="qh-rolefield-l">Table</span>
          <MxCombo value={f.table} placeholder="Type or pick a table…"
            options={tables.map(t => ({ value: t.name, label: t.name }))}
            onPick={v => set({ table: v, column: '' })} /></label>}
        {f.scope === 'column' && !!table && <div className="qh-rolefield qh-mxcolfield"><span className="qh-rolefield-l">Columns{picked.length > 1 && <span className="qh-accedit-hint"> — one exemption each, same reason</span>}</span>
          {/* Picked from the catalog and never typed — 156,000 known columns, and
              a typo writes an exemption that silently matches nothing. Each
              option says what masks it today, which is the question that brought
              most people here. Picking ADDS: standing at a table, the next
              sibling column is one more click, not another walk down the form. */}
          <div className="qh-mxcols">
            {picked.map(c => (
              <span key={c} className="qh-mxcolchip">{c}
                <button type="button" title={'Remove ' + c} onClick={() => dropCol(c)}><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18" /></svg></button>
              </span>
            ))}
            <MxCombo clearOnPick wide value="" placeholder={picked.length ? 'Add another column…' : 'Type or pick a column…'}
              options={columns.filter(c => picked.indexOf(c.name) < 0).map(c => ({ value: c.name, label: c.name, hint: c.rule ? 'masked as ' + c.rule.label : 'not masked', hintWarn: !c.rule }))}
              onPick={addCol} />
          </div></div>}
      </div>
      {catErr && <div className="qh-roleform-err">{catErr} Pick another database, or ask for a catalog snapshot to be taken.</div>}
      {colGone && <div className="qh-roleform-err"><code>{gonePicked.join(', ')}</code> {gonePicked.length > 1 ? 'are' : 'is'} not in {conn ? conn.name : 'this server'}’s catalog snapshot for {f.schema}.{f.table} — pick a column from the list, or have the snapshot refreshed if you know it is there.</div>}
      {/* Exempting a column nothing masks is not an error, but it is almost
          always a mis-pick, so it is said before Save rather than discovered
          when the [REDACTED] does not go away. */}
      {f.scope === 'column' && col && !col.rule && <div className="qh-mxnote">Nothing masks <code>{col.name}</code> today — no name rule matches it. An exemption here only changes what the six value detectors do.</div>}
      {f.scope === 'column' && col && col.rule && <div className="qh-mxnote">Masked today by the name rule <code>{col.rule.key}</code> — {col.rule.label}, {col.rule.mask === 'full' ? 'fully' : 'partially'} masked.</div>}

      {/* The reach sentence, and the confirmation for the rungs that need one.
          Once, here, rather than as a banner on every row of the list. */}
      {readyAll && !colGone && (
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

      <div className="qh-mxstep">
        <span className="qh-mxstep-n">2</span>
        <span className="qh-mxstep-t">Why<span className="qh-accedit-hint">Required — this is what an auditor reads when asking why this data stopped being protected. Say what you checked, not that you checked it.</span></span>
      </div>
      <textarea className="qh-input qh-mxreason-in" rows={2} value={f.reason} onChange={e => set({ reason: e.target.value })}
                placeholder="e.g. Holds the counterparty wallet address, not a postal one. 42 of 51 queries in March joined customers, so it survives joins." />

      {/* Two questions with a right answer 27 times out of 30 are not questions
          on the way past — they are a stated default with one control. Opening
          it shows the same two questions in the same words as before. */}
      <div className="qh-mxstep">
        <span className="qh-mxstep-n">3</span>
        <span className="qh-mxstep-t">Settings<span className="qh-accedit-hint">The defaults are what twenty-seven of the thirty live exemptions use.</span></span>
      </div>
      <button type="button" className="qh-mxsum" onClick={() => setMore(m => !m)}>
        <span className="qh-mxsum-t">{mxSummary(f)}</span><span className="qh-linkbtn">{more ? 'Done' : 'Change'}</span>
      </button>
      {more && <MxOptions f={f} set={set} table={f.table} joinStats={prev && prev.joinStats ? { ...prev.joinStats, days: prev.days } : null} />}
      {f.scope === 'fleet' && f.audience === 'everyone' && <div className="qh-mxnote">Widening a fleet-wide exemption to everyone is a second decision, not part of this one — the live fleet-wide row is restricted to super-admins.</div>}

      {prev && (
        <div className="qh-mxprev">
          {prev.error && <div className="qh-mxprev-idle">Could not build a preview. The exemption is still what the sentence above says.</div>}
          {/* No sample is a finding, not an empty box. */}
          {!prev.error && !prev.seen && !prev.wide && <div className="qh-mxprev-idle">Nothing has queried <code>{f.table}</code> in the last {prev.days} days, so there is no real row to show. The change is what the sentence above says.</div>}
          {prev.wide && <div className="qh-mxprev-idle">This reaches <b>{prev.tables}</b> tables and <b>{prev.maskedColumns}</b> columns that are masked today. Too wide for a single sample row — that count is the preview.</div>}
          {prev.seen && (
            <div className="qh-mxdiffwrap">
              {picked.length > 1 && <div className="qh-mxnote">Shown for <code>{preview.column}</code>; the other {picked.length - 1} change the same way.</div>}
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
      <div className="qh-accedit-acts">
        <button className="qh-btn qh-btn-sm" onClick={onDone}>Cancel</button>
        <button className="qh-btn qh-btn-sm qh-mxprevbtn" disabled={!readyAll || prevBusy} onClick={runPreview}>{prevBusy ? 'Checking…' : prev ? 'Check again' : 'Show what this changes'}</button>
        <button className={'qh-btn qh-btn-sm ' + (wide ? 'qh-btn-danger' : 'qh-btn-primary')} disabled={bad || busy} onClick={save}>{busy ? 'Saving…' : saveLabel}</button>
      </div>
      {readyAll && !f.reason.trim() && <div className="qh-mxnote">A reason is required — an exemption without one is unreadable to whoever finds it next.</div>}
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
        {QH_MX_ORDER.map(k => <div key={k}><dt>{QH_MX_RUNGS[k].label}</dt><dd>{QH_MX_RUNGS[k].w}</dd></div>)}
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
  const [openId, setOpenId] = useMx(null);
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
  const wideCount = all.filter(e => mxWide(e.scope)).length;

  // Sorted by server and database, then grouped under ONE label each. The first
  // cut printed the same `<server> / <database>` pair on four consecutive rows,
  // so the widest text on the row was the part that was identical to its
  // neighbours — which is what made the list tiring to read. A header is not
  // furniture when it REPLACES text: eighteen labels here delete sixty repeats
  // of the same two identifiers, and each one carries the `+` that starts the
  // next exemption in that database.
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

  const start = (s) => { setSeed(s || null); setAdding(true); setOpenId(null); };
  const remove = (e) => {
    if (!window.confirm('Remove this exemption? ' + (e.column ? e.table + '.' + e.column : e.connectionName) + ' goes back to being masked, and the reason on the row goes with it. Turning it off keeps the record.')) return;
    st.removeMaskExemption(e.id);
  };
  // Replace = the same target, pre-filled, so the reach can be changed the one
  // honest way: a new row with its own reason, and the old one turned off by
  // whoever is sure the new one is right.
  const replace = (e) => start({ connectionId: e.connectionId, databaseId: e.databaseId, scope: e.scope, schema: e.schema, table: e.table, column: e.column, strength: e.strength, survivesJoin: e.survivesJoin });
  // The two shortcuts the operator asked for, and they are the same gesture at
  // two depths: standing at a table, add another of its columns; standing at a
  // database, add anything in it. Both land in the form with everything above
  // the missing field already answered.
  const addHere = (e) => start({ connectionId: e.connectionId, databaseId: e.databaseId, scope: 'column', schema: e.schema, table: e.table, strength: e.strength, survivesJoin: e.survivesJoin });
  const addInGroup = (g) => start(g.seed.scope === 'fleet' ? null
    : { connectionId: g.seed.connectionId, databaseId: g.seed.databaseId, scope: 'column' });
  const norms = mxNorms(all);

  return (
    <div className="qh-apad">
      <div className="qh-aview-head">
        <div><div className="qh-aview-title">Masking exemptions</div>
          <div className="qh-aview-sub">Where masking is deliberately switched off, and why. {all.length} row{all.length === 1 ? '' : 's'} · {meta.nameRules || 0} name rules and {(meta.valueDetectors || []).length} value detectors are what they correct.</div>
          {/* The norm, read from the rows and stated once, so a row can stay
              quiet about the three settings it shares with everything else. A
              labelled line rather than a sentence: unlabelled it read as a
              wrapped continuation of the subtitle above it. */}
          {all.length >= 4 && <div className="qh-mxnorm"><span className="qh-mxnorm-k">Unless a row says otherwise</span>{mxNormLine(norms)}</div>}</div>
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
              {[['all', 'All ' + all.length], ['wide', 'Wider than a column ' + wideCount], ['off', 'Turned off'], ['gone', 'Matching nothing']].map(([v, l]) => (
                <button key={v} className={'qh-seg-opt' + (filter === v ? ' is-active' : '')} onClick={() => setFilter(v)}>{l}</button>
              ))}
            </div>
          </div>

          {/* The one fact the list cannot show by itself: how much of it reaches
              past a single column. A sentence with the filter behind it, not a
              second section with its own explanatory paragraph. */}
          {filter === 'all' && wideCount > 0 && (
            <div className="qh-mxbar">
              <MxIcon.warn />
              <span><b>{wideCount}</b> of {all.length} reach wider than one column — those are the rows to read first.</span>
              <button className="qh-linkbtn" onClick={() => setFilter('wide')}>Show just those</button>
            </div>
          )}

          {groups.map(g => (
            <div key={g.key} className="qh-mxgroup">
              <div className="qh-mxgroup-h">
                <span className="qh-mxgroup-n">{g.key}</span>
                {/* A count beside a single row says nothing — the row is right
                    there. It only informs once there is more than one. */}
                {g.rows.length > 1 && <span className="qh-mxgroup-c">{g.rows.length}</span>}
                {canWrite && g.seed.scope !== 'fleet' && (
                  <button className="qh-mxadd" title={'Add an exemption in ' + g.key} onClick={() => addInGroup(g)}><MxIcon.plus />add</button>
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
          {rows.length === 0 && <div className="qh-conn-empty">No exemptions match your filter.</div>}
        </>
      )}
    </div>
  );
}

Object.assign(window, { MaskingView, MxReach, QH_MX_RUNGS });
