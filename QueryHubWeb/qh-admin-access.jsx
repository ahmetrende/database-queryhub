// QueryHub Admin — access-control views (super-admin): Grants, Auto-approve, Admin scopes, Teams, Connections.
const { useState: useAcc } = React;

const QH_TIERS = ['RO', 'RW', 'DDL'];

// Small reusable icons for this file (distinct from qh-admin.jsx AdminIcons).
const AIcon = {
  search: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="7" /><path d="M21 21l-4.3-4.3" /></svg>,
  plus: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round"><path d="M12 5v14M5 12h14" /></svg>,
  x: () => <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18" /></svg>,
  check: () => <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5" /></svg>,
  edit: () => <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z" /></svg>,
};

function TierSelect({ value, onChange }) {
  return (
    <div className="qh-seg qh-seg-sm">
      {QH_TIERS.map(v => <button key={v} className={'qh-seg-opt' + (value === v ? ' is-active' : '')} onClick={() => onChange(v)}><TierBadge tier={v} sm /></button>)}
    </div>
  );
}

function expiryLabel(iso) {
  if (!iso) return { text: 'No expiry', cls: '' };
  const days = Math.ceil((new Date(iso) - Date.now()) / (1000 * 86400));
  if (days <= 0) return { text: 'Expired', cls: 'is-exp' };
  if (days <= 3) return { text: days + 'd left', cls: 'is-soon' };
  return { text: days + 'd left', cls: '' };
}
// Standing grants can expire as of migration 096 (CODE brief 2026-08-15 (c)) —
// until then only auto-approve was time-bounded and this control was removed
// from here on that word. NULL stays the common case (every live grant has it),
// so "No expiry" is the default and the date field only appears when an end is
// actually wanted. A picked day is read as THROUGH that day (23:59 local):
// "until Friday" includes Friday, and expiring at 00:00 would cut a day short.
const expDay = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
const expToday = () => expDay(new Date());
function expForm(iso) { return iso ? { ttl: 'date', expDate: expDay(new Date(iso)) } : { ttl: 'none', expDate: '' }; }
function expIso(f) {
  if (!f.ttl || f.ttl === 'none') return null;
  if (f.ttl === 'date') return f.expDate ? new Date(f.expDate + 'T23:59:59').toISOString() : null;
  return qhIso(new Date(Date.now() + 1000 * 86400 * parseInt(f.ttl)));
}
// The server refuses a past date with 400 rather than accepting it inert — a
// grant that is dead on arrival still reads to the admin as "access given". The
// input carries `min` and Save refuses, so that 400 is a backstop, not the UI.
function expBad(f) { return f.ttl === 'date' && (!f.expDate || f.expDate < expToday()); }
// Expiry only ever REMOVES: an expired user grant does not fall back to the
// team's, because a user row is usually written to NARROW what a team allows
// and falling through would widen access on the day it was meant to end.
function ExpiryNote({ f, subjectType }) {
  if (!f.ttl || f.ttl === 'none') return null;
  return <div className="qh-exp-note">Access stops on this date{subjectType === 'user' ? ' — it does not fall back to a team grant' : ''}. Re-granting later replaces the date, or clears it.</div>;
}
function ExpiryPick({ f, onChange }) {
  return (
    <div className="qh-exppick">
      <select className="qh-select" value={f.ttl || 'none'} onChange={e => onChange({ ttl: e.target.value, expDate: e.target.value === 'date' ? (f.expDate || expToday()) : f.expDate })}>
        <option value="none">No expiry</option>
        <option value="7">7 days</option><option value="30">30 days</option><option value="90">90 days</option>
        <option value="date">Until a date…</option>
      </select>
      {f.ttl === 'date' && <input type="date" className={'qh-input qh-input-sm qh-input-date' + (expBad(f) ? ' is-err' : '')} min={expToday()} value={f.expDate} onChange={e => onChange({ ttl: 'date', expDate: e.target.value })} />}
    </div>
  );
}
function ExpiryChip({ iso }) { if (!iso) return null; const ex = expiryLabel(iso); return <span className={'qh-expiry ' + ex.cls}>{ex.text}</span>; }
function blankTarget(conns) {
  const c = (conns || [])[0];
  return { connectionId: c ? c.id : '', databases: ['*'], tier: 'RO', ttl: 'none', expDate: '' };
}
// Database multi-pick for a per-connection grant: "All databases" (['*']) or a
// specific set. Picking a specific db drops '*'; clearing all falls back to '*'.
function DbMultiPick({ conns, connectionId, databases, onChange }) {
  const conn = (conns || []).find(c => c.id === connectionId) || { databases: [] };
  const all = !databases || databases.length === 0 || databases.includes('*');
  const toggleDb = (id) => {
    const base = all ? [] : databases.filter(x => x !== '*');
    const next = base.includes(id) ? base.filter(x => x !== id) : [...base, id];
    onChange(next.length === 0 ? ['*'] : next);
  };
  return (
    <div className="qh-dbpick">
      <span className="qh-dbpick-lbl">Databases</span>
      <button type="button" className={'qh-dbpick-all' + (all ? ' is-on' : '')} onClick={() => onChange(['*'])}>All databases</button>
      {conn.databases.map(d => {
        const on = !all && databases.includes(d.id);
        return <button key={d.id} type="button" className={'qh-dbpick-db' + (on ? ' is-on' : '')} onClick={() => toggleDb(d.id)}>{d.name}</button>;
      })}
    </div>
  );
}
// Connection pickers list disabled targets too — a grant can legitimately be
// written before a target is enabled — so the label has to say which is which,
// or a grant that silently does nothing looks like a grant that works.
function connLabel(c) { return c.enabled === false ? c.name + ' (disabled)' : c.name; }
// A PERSON is printed as a name (`qhPersonName`, qh-data.jsx 2026-09-17): the
// directory's `name` is a handle wherever the profile carried none, and the
// fallback here is the handle itself. A TEAM goes through untouched — a team id
// is not a person's name, and rewriting it would invent words.
function subjLabel(people, subjectType, subject) {
  if (subjectType === 'user') { const p = (people || []).find(x => x.handle === subject); return qhPersonName(p ? p.name : subject); }
  return subject;
}
// GET /admin/grants resolves the display name server-side as `subjectName`,
// against BOTH people tables (CODE brief 2026-08-21 §1). The client-side lookup
// above can only see the requester roster, so an **admin-only principal** — an
// admins row with no requesters row — rendered as a raw handle beside rows that
// rendered as names. `subjLabel` stays as the fallback for a row this client
// built locally and has not reloaded yet.
// For a team `subjectName` is the TEAM NAME rather than null, so the handle line
// below is suppressed by `subjectType`, never by a missing name.
function grantName(g, people) {
  if (g.subjectName) return g.subjectType === 'user' ? qhPersonName(g.subjectName) : g.subjectName;
  return subjLabel(people, g.subjectType, g.subject);
}

// Shared controls: search box + group-by segmented.
function AccSearch({ q, setQ, placeholder }) {
  return (
    <div className="qh-search sm">
      <span className="qh-search-ic"><AIcon.search /></span>
      <input className="qh-search-in" placeholder={placeholder} value={q} onChange={e => setQ(e.target.value)} />
      {q && <button className="qh-search-x" onMouseDown={e => { e.preventDefault(); setQ(''); }} aria-label="Clear"><AIcon.x /></button>}
    </div>
  );
}
function AccGroupBy({ group, setGroup, options, label }) {
  return (
    <div className="qh-groupby">
      <span className="qh-groupby-label">{label || 'Group by'}</span>
      <div className="qh-seg qh-seg-sm">{options.map(([v, l]) => <button key={v} className={'qh-seg-opt' + (group === v ? ' is-active' : '')} onClick={() => setGroup(v)}>{l}</button>)}</div>
    </div>
  );
}
function accGroup(rows, keyFn) {
  const m = new Map();
  rows.forEach(r => { const k = keyFn(r); if (!m.has(k)) m.set(k, []); m.get(k).push(r); });
  return [...m.entries()].sort((a, b) => a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0);
}

// ---------- Subject combo: pick a person, or type a principal id ----------
// A <select> over the existing people list was the ONLY thing blocking "add a
// person" (CODE brief 2026-08-22 §3). `POST /admin/grants` has always accepted
// any principal id, including a Slack id QueryHub has never seen: `grants.grant`
// whitelists them, fills name / email / tz from Slack and writes the grant in one
// transaction. **Granting access IS the create** — there is no POST
// /admin/people and there should not be, since a person with no grants is a row
// that does nothing. What was missing was a control that accepts an id.
// `GET /admin/people/resolve` is what makes a mistyped id catchable BEFORE the
// grant exists: `known: false` with a name filled in means "not in QueryHub yet,
// but the directory says this is who it is"; a null name means nobody has that id.
function PersonPick({ people, value, onChange, resolve, autoFocus }) {
  const [q, setQ] = useAcc(null);      // null = not editing; the box shows `value`
  const [hi, setHi] = useAcc(0);
  const [res, setRes] = useAcc(null);
  const open = q !== null;
  const wrapRef = qhUseDismiss(open, () => setQ(null));
  const known = (people || []).find(p => p.handle === value || p.id === value) || null;
  const term = (q || '').trim().toLowerCase();
  const list = (people || []).filter(p => !term || (p.name + ' ' + p.handle).toLowerCase().includes(term)).slice(0, 8);
  const raw = (q || '').trim();
  // The typed text is offered as an id only when it is not already someone in
  // the list — "use Elena Silva" as a principal id is not a thing anyone wants.
  const asId = raw && !(people || []).some(p => p.handle === raw) && /^[A-Za-z0-9._@-]{3,}$/.test(raw) ? raw : null;
  const n = list.length + (asId ? 1 : 0);
  const pickPerson = (h) => { onChange(h); setRes(null); setQ(null); };
  const useId = (id) => {
    onChange(id); setQ(null); setRes({ principal: id, pending: true });
    Promise.resolve(resolve ? resolve(id) : null)
      .then(r => setRes(r ? { ...r, principal: r.principal || id } : null))
      // The server resolves the principal again when it writes the grant, so a
      // failed lookup is a note, not a block.
      .catch(() => setRes({ principal: id, failed: true }));
  };
  const commit = () => { if (hi < list.length) { if (list[hi]) pickPerson(list[hi].handle); } else if (asId) useId(asId); };
  const note = () => {
    if (!value || known || !res || res.principal !== value) return null;
    if (res.pending) return <span className="qh-pcombo-note">Checking <b>{value}</b>…</span>;
    if (res.failed) return <span className="qh-pcombo-note">Couldn’t check <b>{value}</b> just now — it is resolved again when the grant is written.</span>;
    if (res.known) return <span className="qh-pcombo-note">{res.name || value} is already in QueryHub{res.admin ? ', as an admin' : ''}.</span>;
    if (res.name) return <span className="qh-pcombo-note is-new">Not in QueryHub yet — the directory says this is <b>{res.name}</b>{res.email ? ' · ' + res.email : ''}. Saving access adds them.</span>;
    return <span className="qh-pcombo-note is-bad">Nobody in the directory has the id <b>{value}</b>. Check it before saving — the grant would be written against a principal that cannot sign in.</span>;
  };
  return (
    <div className={'qh-pcombo' + (open ? ' is-open' : '')} ref={wrapRef}>
      <input className="qh-input qh-pcombo-in" autoFocus={autoFocus} placeholder="Search people, or paste a principal id…"
        value={open ? q : (known ? qhPersonName(known.name) + ' · ' + known.handle : (value || ''))}
        onFocus={() => { setQ(''); setHi(0); }}
        onChange={e => { setQ(e.target.value); setHi(0); }}
        onKeyDown={e => {
          if (e.key === 'ArrowDown') { e.preventDefault(); setHi(h => (n ? (h + 1) % n : 0)); }
          else if (e.key === 'ArrowUp') { e.preventDefault(); setHi(h => (n ? (h + n - 1) % n : 0)); }
          else if (e.key === 'Enter') { e.preventDefault(); commit(); }
          else if (e.key === 'Escape') { setQ(null); }
        }} />
      {open && (
        <div className="qh-pcombo-pop">
          {list.map((p, i) => (
            <button key={p.handle} className={'qh-pcombo-opt' + (i === hi ? ' is-hi' : '')} onMouseEnter={() => setHi(i)}
              onMouseDown={e => { e.preventDefault(); pickPerson(p.handle); }}>
              <span className="qh-peravatar sm">{p.initials}</span>
              <span className="qh-pcombo-name">{qhPersonName(p.name)}</span>
              <span className="qh-pcombo-h">{p.handle}</span>
            </button>
          ))}
          {asId && (
            <button className={'qh-pcombo-opt is-id' + (hi === list.length ? ' is-hi' : '')} onMouseEnter={() => setHi(list.length)}
              onMouseDown={e => { e.preventDefault(); useId(asId); }}>
              <span className="qh-pcombo-idic"><AIcon.plus /></span>
              <span className="qh-pcombo-name">Use “{asId}”</span>
              <span className="qh-pcombo-h">principal id — granting adds them</span>
            </button>
          )}
          {!n && <div className="qh-pcombo-none">Nobody matches. A Slack id (U…) or a handle can be typed here.</div>}
        </div>
      )}
      {note()}
    </div>
  );
}

// ---------- Subject-centric access editor: one subject → many targets ----------
function SubjectAccessEditor({ st, actor, subjectType0, subject0, name0, lockSubject, onDone }) {
  const people = st.people, teams = st.teams;
  const conns = st.connections || [];
  const [subjectType, setSubjectType] = useAcc(subjectType0 || 'user');
  const [subject, setSubject] = useAcc(subject0 || '');
  // A connection whose rows differ in tier by database (pod imports, rows
  // written by hand) opens READ-ONLY: POST /admin/grants writes one tier per
  // connection, so saving it here would raise the lower databases to the higher
  // tier (CODE 2026-09-23 (b) §3). It is shown, kept, and left out of the save.
  const rowsForSubject = (stype, subj) => {
    const ex = st.grants.filter(g => g.subjectType === stype && g.subject === subj);
    if (!ex.length) return [blankTarget(conns)];
    const out = [], seen = {};
    ex.forEach(g => {
      const on = ex.filter(x => x.connectionId === g.connectionId);
      if (new Set(on.map(x => x.tier)).size > 1) {
        if (seen[g.connectionId]) return;
        seen[g.connectionId] = 1;
        out.push({ locked: true, connectionId: g.connectionId, parts: on.map(x => ({ databases: (x.databases && x.databases.length) ? x.databases : ['*'], tier: x.tier, expiresAt: x.expiresAt || null })) });
        return;
      }
      out.push({ connectionId: g.connectionId, databases: (g.databases && g.databases.length) ? g.databases : ['*'], tier: g.tier, ...expForm(g.expiresAt) });
    });
    return out;
  };
  const [rows, setRows] = useAcc(() => subject0 ? rowsForSubject(subjectType0, subject0) : [blankTarget(conns)]);

  const pickType = (v) => { setSubjectType(v); setSubject(''); setRows([blankTarget(conns)]); };
  const pickSubject = (val) => { setSubject(val); setRows(val ? rowsForSubject(subjectType, val) : [blankTarget(conns)]); };
  const setRow = (i, patch) => setRows(rs => rs.map((r, j) => j === i ? { ...r, ...patch } : r));
  const removeRow = (i) => setRows(rs => rs.filter((_, j) => j !== i));
  const addRow = () => setRows(rs => [...rs, blankTarget(conns)]);
  const lockedConns = rows.filter(r => r.locked).map(r => r.connectionId);
  // An editable row may not name a read-only connection, nor one another row
  // already has: either would be a second tier on one connection.
  const rowFlag = (r, i) => r.locked ? null
    : lockedConns.indexOf(r.connectionId) >= 0 ? 'The tier on ' + r.connectionId + ' differs by database — it cannot be changed here. Remove this row.'
    : rows.findIndex(x => !x.locked && x.connectionId === r.connectionId) !== i ? 'Listed twice — one row per connection.' : null;
  const save = () => {
    if (!subject.trim() || rows.some(r => !r.locked && expBad(r)) || rows.some(rowFlag)) return;
    const targets = rows.filter(r => !r.locked).map(r => ({ connectionId: r.connectionId, databases: r.databases, tier: r.tier, expiresAt: expIso(r) }));
    st.setSubjectGrants(subjectType, subject.trim(), targets, { untouched: lockedConns });
    onDone();
  };
  // ONE save control, rendered in two places (CODE brief 2026-08-20 §1): a
  // subject with a dozen connections pushes the only Save below the fold, so a
  // change made at the top is committed by scrolling back down to find it. Same
  // function, same disabled rule — two buttons that could disagree about
  // whether the form is savable would be worse than one badly placed.
  // The rule now matches `save`'s own guard: a bad date used to leave the
  // button enabled and the click did nothing.
  const blocked = !subject.trim() || rows.some(r => !r.locked && expBad(r)) || rows.some(rowFlag);
  const acts = (where) => (
    <div className={'qh-teamform-acts qh-accedit-acts is-' + where}>
      <button className="qh-btn qh-btn-ghost qh-btn-sm" onClick={onDone}>Cancel</button>
      <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={save} disabled={blocked}>Save access</button>
    </div>
  );

  return (
    <div className="qh-accedit">
      <div className="qh-accedit-subj">
        <div className="qh-seg qh-seg-sm">
          {['user', 'team'].map(v => <button key={v} disabled={lockSubject} className={'qh-seg-opt' + (subjectType === v ? ' is-active' : '')} onClick={() => pickType(v)}>{v}</button>)}
        </div>
        {lockSubject
          ? <span className="qh-accedit-subjname"><span className={'qh-subj-type ' + subjectType}>{subjectType}</span> <b>{(subjectType === 'user' ? qhPersonName(name0) : name0) || subjLabel(people, subjectType, subject)}</b></span>
          : subjectType === 'user'
            ? <PersonPick people={people} value={subject} onChange={pickSubject} resolve={st.resolvePerson} autoFocus />
            : <select className="qh-select" value={subject} onChange={e => pickSubject(e.target.value)}><option value="">Select team…</option>{teams.map(t => <option key={t.id} value={t.name}>{t.name}</option>)}</select>}
        {acts('top')}
      </div>

      <div className="qh-accedit-label">Targets · {rows.length}<span className="qh-accedit-hint">one row per connection — databases (or all), a single tier, and an end date only if the access should stop</span></div>
      <div className="qh-acctargets">
        {rows.map((r, i) => r.locked ? (
          <div key={i} className="qh-accrow is-locked">
            <div className="qh-accrow-head">
              <span className="qh-accrow-conn">{connLabel(conns.find(c => c.id === r.connectionId) || { name: r.connectionId })}</span>
              <span className="qh-accrow-lock">Tier differs by database · read-only here</span>
            </div>
            <div className="qh-accrow-parts">{r.parts.map((p, j) => (
              <div key={j} className="qh-accrow-part">
                <span className="qh-accrow-dbs">{p.databases.includes('*') ? 'All databases' : p.databases.join(', ')}</span>
                <TierBadge tier={p.tier} sm />
                <ExpiryChip iso={p.expiresAt} />
              </div>))}</div>
            <div className="qh-exp-note">Saving here writes one tier for the whole connection, which would raise the lower databases. This connection is left exactly as it is — change it from Grants, one row at a time.</div>
          </div>
        ) : (
          <div key={i} className={'qh-accrow' + (rowFlag(r, i) ? ' is-bad' : '')}>
            <div className="qh-accrow-head">
              <select className="qh-select" value={r.connectionId} onChange={e => setRow(i, { connectionId: e.target.value, databases: ['*'] })}>{conns.map(c => <option key={c.id} value={c.id}>{connLabel(c)}</option>)}</select>
              <TierSelect value={r.tier} onChange={v => setRow(i, { tier: v })} />
              <ExpiryPick f={r} onChange={p => setRow(i, p)} />
              <button className="qh-accrow-x" onClick={() => removeRow(i)} aria-label="Remove target"><AIcon.x /></button>
            </div>
            <DbMultiPick conns={conns} connectionId={r.connectionId} databases={r.databases} onChange={dbs => setRow(i, { databases: dbs })} />
            <ExpiryNote f={r} subjectType={subjectType} />
            {rowFlag(r, i) && <div className="qh-autobulk-flag">{rowFlag(r, i)}</div>}
          </div>
        ))}
        {rows.length === 0 && <div className="qh-acc-none">No targets — saving will remove all access for this subject.</div>}
        {rows.length > 0 && rows.every(r => r.locked) && <div className="qh-acc-none">Nothing here can be edited — every connection's tier differs by database.</div>}
      </div>
      <button className="qh-acc-addtarget" onClick={addRow}><AIcon.plus />Add connection</button>

      {acts('bottom')}
    </div>
  );
}

// ---------- Grants (flat by-grant form, used in By-grant inline edit) ----------
function GrantForm({ init, actor, st, people, teams, onDone }) {
  const [f, setF] = useAcc(() => ({ ...init, ...expForm(init.expiresAt) }));
  const conns = st.connections || [];
  const editing = !!f.id;
  const save = () => {
    if (!f.subject.trim() || expBad(f)) return;
    const payload = { subjectType: f.subjectType, subject: f.subject.trim(), connectionId: f.connectionId, databases: f.databases, tier: f.tier, expiresAt: expIso(f) };
    if (editing) st.updateGrant({ ...payload, id: f.id }, actor); else Promise.resolve(st.addGrant(payload, actor)).catch(() => {});
    onDone();
  };
  return (
    <div className="qh-addrow wrap">
      <div className="qh-seg qh-seg-sm">
        {['user', 'team'].map(v => <button key={v} className={'qh-seg-opt' + (f.subjectType === v ? ' is-active' : '')} onClick={() => setF({ ...f, subjectType: v, subject: '' })}>{v}</button>)}
      </div>
      {f.subjectType === 'user'
        ? <PersonPick people={people} value={f.subject} onChange={v => setF({ ...f, subject: v })} resolve={st.resolvePerson} />
        : <select className="qh-select" value={f.subject} onChange={e => setF({ ...f, subject: e.target.value })}><option value="">Select team…</option>{teams.map(t => <option key={t.id} value={t.name}>{t.name}</option>)}</select>}
      <select className="qh-select" value={f.connectionId} onChange={e => setF({ ...f, connectionId: e.target.value, databases: ['*'] })}>
        {conns.map(c => <option key={c.id} value={c.id}>{connLabel(c)}</option>)}
      </select>
      <TierSelect value={f.tier} onChange={v => setF({ ...f, tier: v })} />
      <DbMultiPick conns={conns} connectionId={f.connectionId} databases={f.databases} onChange={dbs => setF({ ...f, databases: dbs })} />
      <ExpiryPick f={f} onChange={p => setF({ ...f, ...p })} />
      <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={save}>{editing ? 'Save' : 'Add'}</button>
      {editing && <button className="qh-btn qh-btn-ghost qh-btn-sm" onClick={onDone}>Cancel</button>}
      <ExpiryNote f={f} subjectType={f.subjectType} />
    </div>
  );
}

// `grantedByName` resolves the granting principal against both people tables,
// the same way `createdByName` does on the auto-approve table (CODE brief
// 2026-08-21 (b)). Three honest gaps to render, not work around:
//   · null on EVERY team grant — `team_target_grants` has no `granted_by` column
//   · null on the 15 of 68 rows that recorded nothing
//   · four rows hold a free-text NOTE instead of a principal id, because someone
//     used the column as a comment field; for those it is the only record of why
//     the grant exists, so it is shown rather than blanked — clamped to one line
//     with the full text on hover, or a sentence would stretch the column.
function grantByLabel(g) { return qhPersonName(g.grantedByName || g.grantedBy) || '—'; }

function GrantsView({ st, user }) {
  const actor = 'dba.' + user.name.split(' ')[0].toLowerCase();
  // Person first, and it is the DEFAULT (CODE brief 2026-08-20 §6): the report
  // was that granting is hard because picking a person shows nowhere they
  // already stand. By-subject and by-grant stay for the bulk work they are good
  // at — but the screen you land on is now the one that answers "who is this".
  const [mode, setMode] = useAcc('person');
  const [q, setQ] = useAcc('');
  const [addingSubject, setAddingSubject] = useAcc(false);
  const [editKey, setEditKey] = useAcc(null);
  const [group, setGroup] = useAcc('none');
  const [adding, setAdding] = useAcc(false);
  const [editId, setEditId] = useAcc(null);
  const c0 = (st.connections || [])[0] || { id: '' };
  const blank = { subjectType: 'user', subject: '', connectionId: c0.id, databases: ['*'], tier: 'RO' };

  // By-subject: fold the flat grants into one card per subject.
  const subjects = (() => {
    const m = new Map();
    st.grants.forEach(g => { const k = g.subjectType + '\u0000' + g.subject; if (!m.has(k)) m.set(k, { subjectType: g.subjectType, subject: g.subject, subjectName: g.subjectName, targets: [] }); m.get(k).targets.push(g); });
    let arr = [...m.values()];
    const t = q.trim().toLowerCase();
    if (t) arr = arr.filter(s => ((s.subjectName || subjLabel(st.people, s.subjectType, s.subject)) + ' ' + s.subject + ' ' + s.targets.map(g => g.connectionId + ' ' + qhGrantDbNames(g) + ' ' + g.tier).join(' ')).toLowerCase().includes(t));
    return arr.sort((a, b) => a.subjectType !== b.subjectType ? (a.subjectType === 'team' ? -1 : 1) : (a.subject < b.subject ? -1 : 1));
  })();

  const newBtn = () => { if (mode === 'grant') { setAdding(a => !a); setEditId(null); } else { setAddingSubject(true); setEditKey(null); } };

  return (
    <div className="qh-apad">
      <div className="qh-aview-head">
        <div><div className="qh-aview-title">Grants</div><div className="qh-aview-sub">Give a person or team standing access to one or many connections — each connection has a database scope and a single tier.</div></div>
        {/* Person mode gets the button too, reading "Add person": the operator
            asked why there was no such button, and the answer is that granting IS
            the create — it opens the same editor with the subject combo unlocked. */}
        <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={newBtn}><AIcon.plus />{mode === 'grant' ? 'New grant' : mode === 'person' ? 'Add person' : 'Grant access'}</button>
      </div>

      <div className="qh-conn-controls">
        {/* Person mode carries its own search — it filters people, not grants. */}
        {mode !== 'person' && <AccSearch q={q} setQ={setQ} placeholder={mode === 'subject' ? 'Filter by subject or target…' : 'Filter by subject, server, database…'} />}
        <AccGroupBy label="View" group={mode} setGroup={setMode} options={[['person', 'Person'], ['subject', 'By subject'], ['grant', 'By grant']]} />
        {mode === 'grant' && <AccGroupBy group={group} setGroup={setGroup} options={[['none', 'None'], ['subject', 'Subject'], ['server', 'Server'], ['database', 'Database']]} />}
      </div>

      {mode === 'person' ? (
        <>
          {addingSubject && <div className="qh-subjcard is-editing"><SubjectAccessEditor st={st} actor={actor} onDone={() => setAddingSubject(false)} /></div>}
          <PersonAccessView st={st} actor={actor} />
        </>
      ) : mode === 'subject' ? (
        <>
          {addingSubject && <div className="qh-subjcard is-editing"><SubjectAccessEditor st={st} actor={actor} onDone={() => setAddingSubject(false)} /></div>}
          <div className="qh-subjlist">
            {subjects.map(s => {
              const key = s.subjectType + '\u0000' + s.subject;
              if (editKey === key) return <div key={key} className="qh-subjcard is-editing"><SubjectAccessEditor st={st} actor={actor} subjectType0={s.subjectType} subject0={s.subject} name0={s.subjectName} lockSubject onDone={() => setEditKey(null)} /></div>;
              return (
                <div key={key} className="qh-subjcard">
                  <div className="qh-subjcard-main">
                    <div className="qh-subjcard-head"><span className={'qh-subj-type ' + s.subjectType}>{s.subjectType}</span><span className="qh-subjcard-name">{s.subjectName || subjLabel(st.people, s.subjectType, s.subject)}</span><span className="qh-subjcard-count">{s.targets.length} connection{s.targets.length === 1 ? '' : 's'}</span></div>
                    <div className="qh-subjcard-targets">
                      {s.targets.map(g => <span key={g.id} className="qh-acctarget"><span className="qh-acctarget-t">{qhGrantTarget(g)}</span><TierBadge tier={g.tier} sm /><ExpiryChip iso={g.expiresAt} /></span>)}
                    </div>
                  </div>
                  <div className="qh-subjcard-acts"><button className="qh-rowbtn" onClick={() => { setEditKey(key); setAddingSubject(false); }}><AIcon.edit />Edit access</button></div>
                </div>
              );
            })}
            {subjects.length === 0 && <div className="qh-conn-empty">No grants match your filter.</div>}
          </div>
        </>
      ) : (
        <>
          {adding && <GrantForm init={blank} actor={actor} st={st} people={st.people} teams={st.teams} onDone={() => setAdding(false)} />}
          {(() => {
            const rows = st.grants.filter(g => { const t = q.trim().toLowerCase(); if (!t) return true; return (grantName(g, st.people) + ' ' + g.subject + ' ' + g.connectionId + ' ' + qhGrantDbNames(g) + ' ' + g.tier + ' ' + (g.grantedBy || '')).toLowerCase().includes(t); });
            const keyFn = group === 'subject' ? (g => g.subjectType + ' · ' + g.subject) : group === 'server' ? (g => g.connectionId) : group === 'database' ? (g => qhGrantTarget(g)) : null;
            const grouped = keyFn ? accGroup(rows, keyFn) : [['', rows]];
            function renderRow(g) {
              if (editId === g.id) return <tr key={g.id} className="qh-editrow"><td colSpan={6}><GrantForm init={{ ...g }} actor={actor} st={st} people={st.people} teams={st.teams} onDone={() => setEditId(null)} /></td></tr>;
              const ex = expiryLabel(g.expiresAt);
              return (
                <tr key={g.id}>
                  <td><span className={'qh-subj-type ' + g.subjectType}>{g.subjectType}</span> <b>{grantName(g, st.people)}</b>{g.subjectType === 'user' && grantName(g, st.people) !== g.subject && <div className="qh-muted qh-mono" style={{ fontSize: 11.5 }}>{g.subject}</div>}</td>
                  <td className="qh-mono">{qhGrantTarget(g)}</td>
                  <td><TierBadge tier={g.tier} sm /></td>
                  <td><span className={'qh-expiry ' + ex.cls}>{ex.text}</span></td>
                  <td className="qh-muted"><span className="qh-grantby" title={grantByLabel(g)}>{grantByLabel(g)}</span>{g.grantedByName && g.grantedByName !== g.grantedBy && <div className="qh-muted qh-mono" style={{ fontSize: 11.5 }}>{g.grantedBy}</div>}</td>
                  <td className="qh-tright"><div className="qh-rowacts"><button className="qh-rowbtn" onClick={() => { setEditId(g.id); setAdding(false); }}><AIcon.edit />Edit</button><button className="qh-revoke" onClick={() => st.revokeGrant(g.id, actor)}>Revoke</button></div></td>
                </tr>
              );
            }
            return (
              <div className="qh-tablewrap">
              <table className="qh-atable qh-acttable">
                <thead><tr><th>Subject</th><th>Target</th><th>Tier</th><th>Expires</th><th>Granted by</th><th></th></tr></thead>
                <tbody>
                  {grouped.map(([k, list]) => <React.Fragment key={k || 'all'}>{k && <tr className="qh-grouphead"><td colSpan={6}>{k}<span className="qh-grouphead-n">{list.length}</span></td></tr>}{list.map(renderRow)}</React.Fragment>)}
                  {rows.length === 0 && <tr><td colSpan={6} className="qh-conn-empty">No grants match your filter.</td></tr>}
                </tbody>
              </table>
              </div>
            );
          })()}
        </>
      )}
    </div>
  );
}

// ---------- Auto-approve ----------
// `databaseId` NULL means every database on the connection; the server
// normalises '*' / '' / 'all' / 'any' to NULL (CODE brief 2026-08-20 §3). One
// rule, read by both the form and the table, so a row cannot describe a scope
// the form would never produce.
const autoAllDbs = (id) => !id || ['*', 'all', 'any'].indexOf(String(id).toLowerCase()) >= 0;
function AutoForm({ init, actor, st, onDone }) {
  const [f, setF] = useAcc(init);
  const conns = st.connections || [];
  const editing = !!f.id;
  const conn = conns.find(c => c.id === f.connectionId);
  // '*' in a text field was never a wildcard to the matcher — it compares a
  // non-NULL scope for equality, so the grant matched nothing and the request
  // fell through to manual review with an active grant sitting in the table.
  // "All databases" is an option here now, and NULL is what it sends.
  const allDbs = autoAllDbs(f.databaseId);
  const save = () => {
    if (!f.user.trim()) return;
    // "No expiry" is a choice beside the durations, not two fields left blank
    // (§17): an open-ended auto-grant has always been accepted by the API, and
    // the only way to ask for one was to leave the form empty and hope.
    const expiresAt = f.ttl === 'keep' ? f.expiresAt
      : f.ttl === 'none' ? null
      : qhIso(new Date(Date.now() + 1000 * 86400 * parseInt(f.ttl)));
    // No per-grant row cap: caps live in the row-limit overrides, keyed to a
    // PERSON, because how much someone can pull is a property of them and their
    // machine rather than of one authorization row. `maxRows` came out of this
    // form and table on 2026-08-16 (CODE brief 2026-08-15 (c)) — the API had
    // returned a hardcoded null since it was written, so the field was a
    // control that looked available and was not.
    const payload = { user: f.user.trim(), tier: f.tier, connectionId: f.connectionId, databaseId: allDbs ? null : f.databaseId, expiresAt };
    if (editing) st.updateAutoGrant({ ...payload, id: f.id }, actor); else st.addAutoGrant(payload, actor);
    onDone();
  };
  return (
    <div className="qh-addrow">
      {/* The same picker the Grants form uses, and for the same reason: the
          field takes a PRINCIPAL, and typing one from memory is how a grant
          ends up written against an id that cannot sign in. Search by name or
          by Slack id; a pasted id still works and is checked against the
          directory before it is saved.
          It also stops the placeholder lying — auto-approve has no team
          column (`auto_approve_grants.slack_user_id`), so "user or team" named
          a subject the server would refuse. */}
      <PersonPick people={st.people} value={f.user}
                  onChange={v => setF({ ...f, user: v })}
                  resolve={st.resolvePerson} autoFocus={!editing} />
      <TierSelect value={f.tier} onChange={v => setF({ ...f, tier: v })} />
      <select className="qh-select" value={f.connectionId} onChange={e => setF({ ...f, connectionId: e.target.value, databaseId: null })}>{conns.map(c => <option key={c.id} value={c.id}>{connLabel(c)}</option>)}</select>
      <select className="qh-select" value={allDbs ? '' : f.databaseId} onChange={e => setF({ ...f, databaseId: e.target.value || null })}>
        <option value="">All databases</option>
        {(conn ? conn.databases : []).map(d => <option key={d.id} value={d.id}>{d.name}</option>)}
      </select>
      <select className="qh-select" value={f.ttl} onChange={e => setF({ ...f, ttl: e.target.value })}>
        {editing && <option value="keep">Keep ({expiryLabel(f.expiresAt).text})</option>}
        <option value="none">No expiry</option>
        <option value="7">7 days</option><option value="30">30 days</option><option value="90">90 days</option>
      </select>
      <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={save}>{editing ? 'Save' : 'Add'}</button>
      {editing && <button className="qh-btn qh-btn-ghost qh-btn-sm" onClick={onDone}>Cancel</button>}
      {(f.ttl === 'none' || (f.ttl === 'keep' && !f.expiresAt)) && <div className="qh-exp-note">No end date — this subject stays auto-approved on that target until someone revokes the grant.</div>}
    </div>
  );
}

function AutoView({ st, user }) {
  const actor = 'dba.' + user.name.split(' ')[0].toLowerCase();
  const [adding, setAdding] = useAcc(null);   // null | '' (pick a subject) | a subject key (locked)
  const [editId, setEditId] = useAcc(null);
  const [q, setQ] = useAcc('');
  // By subject is the DEFAULT (design 2026-09-22 §4a): the subject is the unit
  // an operator thinks in, and a flat table made one person's exemptions read
  // as unrelated rows. The flat table stays one click away for scanning.
  const [view, setView] = useAcc('subject');
  const reqs = st.autoRequests || [];

  const rows = st.autoGrants.filter(a => {
    const t = q.trim().toLowerCase(); if (!t) return true;
    // The resolved name is searchable too — it is what the column shows, and a
    // filter reading only the handle finds nothing for a typed first name.
    return (a.user + ' ' + (a.userName || '') + ' ' + a.connectionId + ' ' + (a.databaseId || '') + ' ' + a.tier).toLowerCase().includes(t);
  });
  const subjects = autoSubjects(rows, st.people);

  const actions = (a) => <div className="qh-rowacts"><button className="qh-rowbtn" onClick={() => { setEditId(a.id); setAdding(null); }}><AIcon.edit />Edit</button><button className="qh-revoke" onClick={() => st.revokeAutoGrant(a.id, actor)}>Revoke</button></div>;

  return (
    <div className="qh-apad">
      <div className="qh-aview-head">
        <div><div className="qh-aview-title">Auto-approve</div><div className="qh-aview-sub">Standing exemptions from review — a person's or a team's queries up to a tier, on a database, run without a DBA until the window ends.</div></div>
        <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={() => { setAdding(a => a === '' ? null : ''); setEditId(null); }}><AIcon.plus />New exemption</button>
      </div>

      {/* Asked for from the web (§3). Above the list, because a request is the
          one thing on this screen waiting on the reader. */}
      {reqs.length > 0 && (
        <div className="qh-autoreqs">
          <div className="qh-section-label">Asked for · {reqs.length}</div>
          {reqs.map(r => {
            // `windowLabel` ("8h", "7 days"), never `days`: an ask made in Slack
            // has `days: null`, and this card printed "for null days" (CODE
            // 2026-09-23 §4). The days fallback is for a payload without a label.
            const win = r.windowLabel || (r.days ? r.days + (r.days === 1 ? ' day' : ' days') : null);
            return (
            <div key={r.id} className="qh-autoreq">
              <div className="qh-autoreq-main">
                <div className="qh-autoreq-say"><b>{qhPersonName(r.requesterName || r.requester)}</b> requests auto-approve on <span className="qh-mono">{r.connectionId} · {r.databaseId || 'all databases'}</span> <TierBadge tier={r.tier} sm />{win && <> for <b>{win}</b></>}</div>
                <div className="qh-autoreq-why">“{r.reason}”</div>
                <div className="qh-autoreq-when">{r.requester} · asked {qhAgo(r.requestedAt)} · the window starts when you grant it</div>
              </div>
              <div className="qh-autoreq-acts">
                <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={() => st.decideAutoRequest(r.id, true)}>{win ? 'Grant for ' + win : 'Grant'}</button>
                <button className="qh-btn qh-btn-ghost qh-btn-sm" onClick={() => st.decideAutoRequest(r.id, false)}>Decline</button>
              </div>
            </div>
            );
          })}
        </div>
      )}

      {adding === '' && <AutoBulkForm st={st} actor={actor} onDone={() => setAdding(null)} />}
      <div className="qh-conn-controls">
        <AccSearch q={q} setQ={setQ} placeholder="Filter by person, team, server…" />
        <AccGroupBy label="Show" group={view} setGroup={setView} options={[['subject', 'By person or team'], ['flat', 'All rows']]} />
      </div>

      {view === 'subject' ? (
        <div className="qh-autosubs">
          {subjects.map(s => (
            <div key={s.key} className="qh-autosub">
              <div className="qh-autosub-head">
                <span className={'qh-peravatar' + (s.team ? ' is-team' : '')}>{s.initials}</span>
                <div className="qh-autosub-who"><div className="qh-autosub-name">{s.name}{s.team && <span className="qh-subj-type team">team</span>}</div>
                  {!s.team && s.key !== s.name && <div className="qh-autosub-h">{s.key}</div>}</div>
                <div className="qh-autosub-sum">Auto-approved on {s.rows.length} target{s.rows.length === 1 ? '' : 's'}{s.open ? <> · <span className="qh-expiry is-soon">{s.open} with no end date</span></> : null}</div>
              </div>
              <div className="qh-autosub-rows">
                {s.rows.map(a => editId === a.id
                  ? <div key={a.id} className="qh-autosub-edit"><AutoForm init={{ ...a, ttl: 'keep' }} actor={actor} st={st} onDone={() => setEditId(null)} /></div>
                  : (() => { const ex = expiryLabel(a.expiresAt); return (
                    <div key={a.id} className="qh-autosub-row">
                      <span className="qh-autosub-t">{a.connectionId}<span className="qh-autosub-db">{autoAllDbs(a.databaseId) ? 'all databases' : a.databaseId}</span></span>
                      <TierBadge tier={a.tier} sm />
                      <span className={'qh-expiry ' + ex.cls}>{ex.text}</span>
                      <span className="qh-autosub-by">by {qhPersonName(a.createdByName || a.createdBy) || '—'}</span>
                      {actions(a)}
                    </div>); })())}
              </div>
              {/* A team can hold auto-approve too (CODE 2026-09-23 §4), so its card gets the same Add. */}
              {adding === s.key
                ? <AutoBulkForm st={st} actor={actor} lockType={s.team ? 'team' : 'user'} lockUser={s.team ? s.name : s.key} lockName={s.name} existing={s.rows} onDone={() => setAdding(null)} />
                : <button className="qh-linkbtn qh-autosub-add" onClick={() => { setAdding(s.key); setEditId(null); }}><AIcon.plus />Add targets for {s.name}</button>}
            </div>
          ))}
          {subjects.length === 0 && <div className="qh-conn-empty">{q.trim() ? 'No exemption matches your filter.' : 'Nobody is auto-approved. Every query is looked at by a DBA.'}</div>}
        </div>
      ) : (
      <div className="qh-tablewrap">
      <table className="qh-atable qh-acttable">
        <thead><tr><th>Person or team</th><th>Scope</th><th>Tier</th><th>Expiry</th><th>Granted by</th><th></th></tr></thead>
        <tbody>
          {rows.map(a => {
            if (editId === a.id) return <tr key={a.id} className="qh-editrow"><td colSpan={6}><AutoForm init={{ ...a, ttl: 'keep' }} actor={actor} st={st} onDone={() => setEditId(null)} /></td></tr>;
            const ex = expiryLabel(a.expiresAt);
            return (
              <tr key={a.id}>
                <td><b>{qhPersonName(a.userName || a.user)}</b>{a.userName && a.userName !== a.user && <div className="qh-muted qh-mono" style={{ fontSize: 11.5 }}>{a.user}</div>}</td>
                <td className="qh-mono">{a.connectionId}{autoAllDbs(a.databaseId) ? <span className="qh-muted"> · all databases</span> : '/' + a.databaseId}</td>
                <td><TierBadge tier={a.tier} sm /></td>
                <td><span className={'qh-expiry ' + ex.cls}>{ex.text}</span></td>
                <td className="qh-muted">{qhPersonName(a.createdByName || a.createdBy) || '—'}</td>
                <td className="qh-tright">{actions(a)}</td>
              </tr>
            );
          })}
          {rows.length === 0 && <tr><td colSpan={6} className="qh-conn-empty">No auto-approve grants match your filter.</td></tr>}
        </tbody>
      </table>
      </div>
      )}
    </div>
  );
}

// One card per subject. A team row arrives as `<name> (team)` with no userName
// (a team is in neither people table), so the suffix is the only thing that
// says which kind of subject it is — read here, once, for the card.
function autoSubjects(rows, people) {
  const m = new Map();
  rows.forEach(a => {
    const team = / \(team\)$/.test(a.user || '');
    if (!m.has(a.user)) {
      const p = (people || []).find(x => x.handle === a.user || x.id === a.user);
      const name = team ? a.user.replace(/ \(team\)$/, '') : qhPersonName(a.userName || (p && p.name) || a.user);
      m.set(a.user, { key: a.user, team, name, initials: team ? name.slice(0, 2).toUpperCase() : ((p && p.initials) || name.slice(0, 1).toUpperCase()), rows: [] });
    }
    m.get(a.user).rows.push(a);
  });
  return [...m.values()].map(s => ({ ...s, open: s.rows.filter(a => !a.expiresAt).length }))
    .sort((a, b) => (a.team === b.team ? (a.name < b.name ? -1 : 1) : a.team ? 1 : -1));
}

// ---------- One subject, many targets, one Save (§4b) ----------
// The subject is picked ONCE — a person or a team, since the write path takes
// both (CODE 2026-09-23 §4) — then as many targets as they need, with ONE tier
// and ONE window for all of them: POST /admin/auto-grants/bulk takes one of each
// and is all or nothing, so the form asks exactly the question the endpoint
// answers. DDL is not offered: schema changes are always reviewed, and a tier
// the server refuses is a control that lies. Duplicates, and targets the
// subject already holds, are marked before Save; whatever the server still
// refuses comes back on its own row, and nothing is written.
const AUTO_WINDOWS = [['7', '7 days'], ['30', '30 days'], ['90', '90 days'], ['none', 'No end date']];
function AutoBulkForm({ st, actor, lockType, lockUser, lockName, existing, onDone }) {
  const conns = (st.connections || []).filter(c => c.enabled !== false);
  const teams = st.teams || [];
  const blankRow = () => ({ k: Math.random().toString(36).slice(2), connectionId: (conns[0] || {}).id || '', databaseId: null });
  const [type, setType] = useAcc(lockType || 'user');
  const [who, setWho] = useAcc(lockUser || '');
  const [rows, setRows] = useAcc(() => [blankRow()]);
  const [tier, setTier] = useAcc('RO');
  const [ttl, setTtl] = useAcc('30');
  const [reason, setReason] = useAcc('');
  const [busy, setBusy] = useAcc(false);
  const [err, setErr] = useAcc(null);
  const [refused, setRefused] = useAcc({});   // row key -> the server's reason
  const set = (k, patch) => {
    setRows(rs => rs.map(r => r.k === k ? { ...r, ...patch } : r)); setErr(null);
    setRefused(x => { if (!x[k]) return x; const n = { ...x }; delete n[k]; return n; });
  };
  // A team's rows are stored as `<name> (team)` — the key the list groups by.
  const held = existing || (st.autoGrants || []).filter(a => a.user === (type === 'team' ? who + ' (team)' : who));
  const keyOf = (r) => r.connectionId + '/' + (r.databaseId || '*');
  const dupeIn = (r, i) => rows.findIndex(x => keyOf(x) === keyOf(r)) !== i;
  const heldBy = (r) => held.find(a => a.connectionId === r.connectionId && (a.databaseId || '*') === (r.databaseId || '*'));
  const bad = !who.trim() || !rows.length || rows.some((r, i) => dupeIn(r, i) || heldBy(r)) || busy;
  const win = (AUTO_WINDOWS.find(w => w[0] === ttl) || [])[1];
  const whoName = lockName || (type === 'team' ? who : qhPersonName(((st.people || []).find(p => p.handle === who || p.id === who) || {}).name || who));
  const pickType = (v) => { setType(v); setWho(''); setErr(null); setRefused({}); };
  const save = () => {
    if (bad) return;
    setBusy(true); setErr(null); setRefused({});
    st.addAutoGrants({ subjectType: type, subject: who.trim(), targets: rows, tier, reason: reason.trim() || null,
      expiresAt: ttl === 'none' ? null : qhIso(new Date(Date.now() + 86400000 * parseInt(ttl, 10))) })
      .then(() => { setBusy(false); onDone(); })
      .catch(e => {
        const list = (e && e.refused) || [], m = {};
        list.forEach(x => { const t = x.target || {};
          const r = rows.find(y => y.connectionId === t.connectionId && (y.databaseId || null) === (t.databaseId || null));
          if (r && !m[r.k]) m[r.k] = x.reason; });
        setRefused(m); setBusy(false);
        setErr(list.length
          ? 'Nothing was created: ' + list.length + ' of ' + rows.length + ' target' + (rows.length === 1 ? ' was' : 's were') + ' refused. Fix or remove ' + (list.length === 1 ? 'it' : 'them') + ', then save again.'
          : ((e && e.message) || 'Nothing was created.'));
      });
  };
  const does = tier === 'RO' ? 'reads' : 'reads and writes';
  return (
    <div className="qh-autobulk">
      {!lockUser && (
        <div className="qh-autobulk-who">
          <span className="qh-rolefield-l">Who</span>
          <div className="qh-seg qh-seg-sm">
            <button className={'qh-seg-opt' + (type === 'user' ? ' is-active' : '')} onClick={() => pickType('user')}>Person</button>
            <button className={'qh-seg-opt' + (type === 'team' ? ' is-active' : '')} onClick={() => pickType('team')}>Team</button>
          </div>
          {type === 'user'
            ? <PersonPick people={st.people} value={who} onChange={v => { setWho(v); setErr(null); }} resolve={st.resolvePerson} autoFocus />
            : <select className="qh-select" value={who} onChange={e => { setWho(e.target.value); setErr(null); }}>
                <option value="">Pick a team…</option>
                {teams.map(t => <option key={t.id} value={t.name}>{t.name} · {t.members.length} member{t.members.length === 1 ? '' : 's'}</option>)}
              </select>}
        </div>
      )}
      {held.length > 0 && who && <div className="qh-autobulk-held">Already auto-approved on {held.map(a => a.connectionId + ' · ' + (a.databaseId || 'all databases')).join(', ')}.</div>}
      <div className="qh-autobulk-rows">
        <div className="qh-autobulk-hd"><span>Connection</span><span>Database</span><span></span></div>
        {rows.map((r, i) => {
          const conn = conns.find(c => c.id === r.connectionId);
          const flag = dupeIn(r, i) ? 'Listed twice' : heldBy(r) ? 'Already exempt here — edit that row instead' : (refused[r.k] || null);
          return (
            <div key={r.k} className={'qh-autobulk-row' + (flag ? ' is-bad' : '')}>
              <select className="qh-select" value={r.connectionId} onChange={e => set(r.k, { connectionId: e.target.value, databaseId: null })}>{conns.map(c => <option key={c.id} value={c.id}>{connLabel(c)}</option>)}</select>
              <select className="qh-select" value={r.databaseId || ''} onChange={e => set(r.k, { databaseId: e.target.value || null })}>
                <option value="">All databases</option>
                {(conn ? conn.databases : []).map(d => <option key={d.id} value={d.id}>{d.name}</option>)}
              </select>
              <button className="qh-bulk-chipx" style={rows.length === 1 ? { visibility: 'hidden' } : undefined} disabled={rows.length === 1} onClick={() => setRows(rs => rs.filter(x => x.k !== r.k))} aria-label="Remove this target"><AIcon.x /></button>
              {flag && <div className="qh-autobulk-flag">{flag}</div>}
            </div>
          );
        })}
        <button className="qh-linkbtn" onClick={() => setRows(rs => rs.concat([blankRow()]))}><AIcon.plus />Another target</button>
      </div>
      <div className="qh-autobulk-opts">
        <div className="qh-autobulk-opt"><span className="qh-rolefield-l">Up to</span>
          <div className="qh-seg qh-seg-sm">{['RO', 'RW'].map(t => <button key={t} className={'qh-seg-opt' + (tier === t ? ' is-active' : '')} onClick={() => { setTier(t); setErr(null); }}>{t}</button>)}</div></div>
        <div className="qh-autobulk-opt"><span className="qh-rolefield-l">For</span>
          <select className="qh-select" value={ttl} onChange={e => { setTtl(e.target.value); setErr(null); }}>{AUTO_WINDOWS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></div>
        <span className="qh-autobulk-note">One tier and one window for every target here.</span>
      </div>
      <input className="qh-input qh-input-sm qh-autobulk-why" placeholder="Why (optional, kept on every row)" value={reason} onChange={e => setReason(e.target.value)} />
      {err && <div className="qh-roleform-err">{err}</div>}
      <div className="qh-autobulk-foot">
        <span className="qh-autobulk-say">{!who
          ? 'Pick the person or team first — then as many targets as they need.'
          : <>{type === 'team' ? <>Every member of <b>{whoName}</b>: their</> : <><b>{whoName}</b>'s</>} {does} on {rows.length} target{rows.length === 1 ? '' : 's'} will run without a DBA{ttl === 'none' ? <>, <b className="qh-eff-sum-warn">with no end date</b></> : <> for {win}</>}.</>}</span>
        <button className="qh-btn qh-btn-ghost qh-btn-sm" onClick={onDone} disabled={busy}>Cancel</button>
        <button className="qh-btn qh-btn-primary qh-btn-sm" disabled={bad} onClick={save}>{busy ? 'Creating…' : 'Create ' + rows.length + ' exemption' + (rows.length === 1 ? '' : 's')}</button>
      </div>
    </div>
  );
}

// ---------- Admin scopes ----------
function ScopeForm({ init, actor, st, onDone }) {
  const [f, setF] = useAcc(init);
  const editing = !!f.id;
  const toggleTier = (t) => setF(s => ({ ...s, canApprove: s.canApprove.includes(t) ? s.canApprove.filter(x => x !== t) : [...s.canApprove, t] }));
  const save = () => {
    if (!f.admin.trim()) return;
    const connections = (f.connections.trim() === '*' || !f.connections.trim()) ? ['*'] : f.connections.split(',').map(s => s.trim()).filter(Boolean);
    st.saveScope({ ...(editing ? { id: f.id } : {}), admin: f.admin.trim(), role: f.role, canApprove: f.canApprove, connections }, actor);
    onDone();
  };
  return (
    <div className="qh-addrow wrap">
      <input className="qh-input qh-input-sm" placeholder="admin handle" value={f.admin} onChange={e => setF({ ...f, admin: e.target.value })} />
      <div className="qh-seg qh-seg-sm">{['dba', 'super'].map(v => <button key={v} className={'qh-seg-opt' + (f.role === v ? ' is-active' : '')} onClick={() => setF({ ...f, role: v })}>{v === 'super' ? 'super-admin' : 'DBA'}</button>)}</div>
      <div className="qh-tierchecks">{QH_TIERS.map(t => <button key={t} className={'qh-tierchk' + (f.canApprove.includes(t) ? ' is-on' : '')} onClick={() => toggleTier(t)}><TierBadge tier={t} sm />{f.canApprove.includes(t) ? '✓' : ''}</button>)}</div>
      <input className="qh-input qh-input-sm" placeholder="connections (* or comma list)" value={f.connections} onChange={e => setF({ ...f, connections: e.target.value })} />
      <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={save}>{editing ? 'Save' : 'Add'}</button>
      {editing && <button className="qh-btn qh-btn-ghost qh-btn-sm" onClick={onDone}>Cancel</button>}
    </div>
  );
}

function ScopesView({ st, user }) {
  const actor = 'dba.' + user.name.split(' ')[0].toLowerCase();
  const [adding, setAdding] = useAcc(false);
  const [editId, setEditId] = useAcc(null);
  const [q, setQ] = useAcc('');
  const [group, setGroup] = useAcc('none');
  const blank = { admin: '', role: 'dba', canApprove: ['RO'], connections: '*' };

  const rows = st.scopes.filter(s => {
    const t = q.trim().toLowerCase(); if (!t) return true;
    return (s.admin + ' ' + s.role + ' ' + s.canApprove.join(' ') + ' ' + s.connections.join(' ')).toLowerCase().includes(t);
  });
  const keyFn = group === 'role' ? (s => s.role === 'super' ? 'super-admin' : 'DBA')
    : group === 'connection' ? (s => s.connections.join(', ')) : null;
  const grouped = keyFn ? accGroup(rows, keyFn) : [['', rows]];

  const renderRow = (s) => {
    if (editId === s.id) return (
      <tr key={s.id} className="qh-editrow"><td colSpan={5}><ScopeForm init={{ id: s.id, admin: s.admin, role: s.role, canApprove: s.canApprove, connections: s.connections.join(', ') }} actor={actor} st={st} onDone={() => setEditId(null)} /></td></tr>
    );
    return (
      <tr key={s.id}>
        <td><b>{s.admin}</b></td>
        <td><span className={'qh-rolechip ' + s.role}>{s.role === 'super' ? 'super-admin' : 'DBA'}</span></td>
        <td><div className="qh-tierrow">{s.canApprove.map(t => <TierBadge key={t} tier={t} sm />)}</div></td>
        <td className="qh-mono">{s.connections.join(', ')}</td>
        <td className="qh-tright"><div className="qh-rowacts"><button className="qh-rowbtn" onClick={() => { setEditId(s.id); setAdding(false); }}><AIcon.edit />Edit</button>{st.scopes.length > 1 && <button className="qh-revoke" onClick={() => st.removeScope(s.id, actor)}>Remove</button>}</div></td>
      </tr>
    );
  };

  return (
    <div className="qh-apad">
      <div className="qh-aview-head">
        <div><div className="qh-aview-title">Admin scopes</div><div className="qh-aview-sub">Which admins may approve which tiers, on which connections. Edit any admin's reach.</div></div>
        <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={() => { setAdding(a => !a); setEditId(null); }}><AIcon.plus />Add admin</button>
      </div>
      {adding && <ScopeForm init={blank} actor={actor} st={st} onDone={() => setAdding(false)} />}
      <div className="qh-conn-controls">
        <AccSearch q={q} setQ={setQ} placeholder="Filter by admin, role, connection…" />
        <AccGroupBy group={group} setGroup={setGroup} options={[['none', 'None'], ['role', 'Role'], ['connection', 'Connection']]} />
      </div>
      <div className="qh-tablewrap">
      <table className="qh-atable qh-acttable">
        <thead><tr><th>Admin</th><th>Role</th><th>Can approve</th><th>Connections</th><th></th></tr></thead>
        <tbody>
          {grouped.map(([k, list]) => (
            <React.Fragment key={k || 'all'}>
              {k && <tr className="qh-grouphead"><td colSpan={5}>{k}<span className="qh-grouphead-n">{list.length}</span></td></tr>}
              {list.map(renderRow)}
            </React.Fragment>
          ))}
          {rows.length === 0 && <tr><td colSpan={5} className="qh-conn-empty">No admins match your filter.</td></tr>}
        </tbody>
      </table>
      </div>
    </div>
  );
}

// ---------- Teams ----------
// The member editor (design 2026-09-22 §5): one box that scrolls on its own,
// the team's members first and stacked, everyone else below, and a type-ahead
// that matches name, handle and Slack id — the operator knows people by both.
// Enter adds the first "everyone else" match, so a known id is two keystrokes.
function MemberEditor({ people, members, setMembers }) {
  const [q, setQ] = useAcc('');
  const t = q.trim().toLowerCase();
  const match = (p) => !t || [p.name, p.handle, p.slackId, p.id].filter(Boolean).join(' ').toLowerCase().includes(t);
  const byH = (h) => people.find(p => p.handle === h) || { handle: h, name: h, initials: '?' };
  const inRows = members.map(byH).filter(match);
  const outRows = people.filter(p => members.indexOf(p.handle) < 0).filter(match);
  const row = (p, on) => (
    <div key={p.handle} className={'qh-mem-row' + (p.enabled === false ? ' is-off' : '')}>
      <span className="qh-mini-avatar">{p.initials || '?'}</span>
      <span className="qh-mem-n">{qhPersonName(p.name)}{p.enabled === false && <span className="qh-perkind is-off">disabled</span>}</span>
      <span className="qh-mem-h">{p.handle}{p.slackId ? ' · ' + p.slackId : ''}</span>
      {on
        ? <button type="button" className="qh-rowbtn" onClick={() => setMembers(members.filter(x => x !== p.handle))}>Remove</button>
        : <button type="button" className="qh-rowbtn is-add" onClick={() => { setMembers(members.concat([p.handle])); }}><AIcon.plus />Add</button>}
    </div>
  );
  return (
    <div className="qh-mem">
      <div className="qh-mem-top">
        <span className="qh-teamform-label">Members · {members.length}</span>
        <div className="qh-search sm qh-mem-search">
          <span className="qh-search-ic"><AIcon.search /></span>
          <input className="qh-search-in" placeholder="Name, handle or Slack id" value={q} onChange={e => setQ(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && outRows.length) { e.preventDefault(); setMembers(members.concat([outRows[0].handle])); setQ(''); } }} />
        </div>
      </div>
      <div className="qh-mem-box">
        <div className="qh-mem-glabel">In this team · {inRows.length}{t && inRows.length !== members.length ? ' of ' + members.length : ''}</div>
        {inRows.length ? inRows.map(p => row(p, true)) : <div className="qh-mem-none">{t ? 'No member matches.' : 'Nobody yet — add people from the list below.'}</div>}
        <div className="qh-mem-glabel is-rest">Everyone else · {outRows.length}</div>
        {outRows.length ? outRows.map(p => row(p, false)) : <div className="qh-mem-none">{t ? 'Nobody else matches.' : 'Everyone is already in this team.'}</div>}
      </div>
    </div>
  );
}

function TeamForm({ people, init, onSave, onCancel }) {
  const [name, setName] = useAcc(init.name || '');
  const [desc, setDesc] = useAcc(init.desc || '');
  const [members, setMembers] = useAcc(init.members || []);
  const submit = () => { if (!name.trim()) return; onSave({ id: init.id, name: name.trim(), desc: desc.trim(), members }); };
  return (
    <div className="qh-teamform">
      <div className="qh-teamform-top">
        <input className="qh-input qh-input-sm" style={{ width: 200 }} placeholder="Team name (e.g. data-eng)" value={name} onChange={e => setName(e.target.value)} />
        <input className="qh-input qh-input-sm qh-flex1" placeholder="Description (optional)" value={desc} onChange={e => setDesc(e.target.value)} />
      </div>
      <MemberEditor people={people} members={members} setMembers={setMembers} />
      <div className="qh-teamform-acts">
        <button className="qh-btn qh-btn-ghost qh-btn-sm" onClick={onCancel}>Cancel</button>
        <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={submit}>{init.id ? 'Save team' : 'Create team'}</button>
      </div>
    </div>
  );
}

function TeamsView({ st, user }) {
  const actor = 'dba.' + user.name.split(' ')[0].toLowerCase();
  const [tab, setTab] = useAcc('teams');
  const [q, setQ] = useAcc('');
  const [adding, setAdding] = useAcc(false);
  const [editId, setEditId] = useAcc(null);
  const [accessId, setAccessId] = useAcc(null);
  const [editPerson, setEditPerson] = useAcc(null);
  const [pdraft, setPdraft] = useAcc([]);

  const teams = st.teams, people = st.people;
  const personBy = (h) => people.find(p => p.handle === h) || {};
  const teamById = (id) => teams.find(t => t.id === id);
  const teamsOf = (h) => teams.filter(t => t.members.includes(h));
  const memberOf = (id) => teams.filter(t => (t.subteams || []).includes(id));
  const grantsOf = (name) => st.grants.filter(g => g.subjectType === 'team' && g.subject === name);
  const unassigned = people.filter(p => teamsOf(p.handle).length === 0);

  const tRows = teams.filter(t => { const s = q.trim().toLowerCase(); if (!s) return true; return (t.name + ' ' + (t.desc || '') + ' ' + t.members.join(' ')).toLowerCase().includes(s); });
  const pRows = people.filter(p => { const s = q.trim().toLowerCase(); if (!s) return true; return (p.name + ' ' + p.handle + ' ' + teamsOf(p.handle).map(t => t.name).join(' ')).toLowerCase().includes(s); });

  const startEditPerson = (p) => { setEditPerson(p.handle); setPdraft(teamsOf(p.handle).map(t => t.id)); };
  const togglePdraft = (id) => setPdraft(d => d.includes(id) ? d.filter(x => x !== id) : [...d, id]);
  const savePerson = () => { st.setPersonTeams(editPerson, pdraft, actor); setEditPerson(null); };
  const clearEdits = () => { setEditId(null); setAccessId(null); setAdding(false); };

  return (
    <div className="qh-apad">
      <div className="qh-aview-head">
        <div><div className="qh-aview-title">Teams</div><div className="qh-aview-sub">Group developers into teams. A person can be in several teams; a team holds access to many targets at different tiers, shared by every member.</div></div>
        {tab === 'teams' && <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={() => { setAdding(a => !a); setEditId(null); setAccessId(null); }}><AIcon.plus />New team</button>}
      </div>

      <div className="qh-conn-controls">
        <div className="qh-seg qh-seg-sm">
          <button className={'qh-seg-opt' + (tab === 'teams' ? ' is-active' : '')} onClick={() => { setTab('teams'); setEditPerson(null); }}>Teams · {teams.length}</button>
          <button className={'qh-seg-opt' + (tab === 'people' ? ' is-active' : '')} onClick={() => { setTab('people'); clearEdits(); }}>People · {people.length}</button>
        </div>
        <AccSearch q={q} setQ={setQ} placeholder={tab === 'teams' ? 'Filter teams…' : 'Filter people…'} />
        {tab === 'people' && unassigned.length > 0 && <span className="qh-team-note">{unassigned.length} without a team</span>}
      </div>

      {tab === 'teams' ? (
        <>
          {adding && <TeamForm people={people} init={{ members: [] }} onSave={(t) => { st.addTeam(t, actor); setAdding(false); }} onCancel={() => setAdding(false)} />}
          <div className="qh-teamlist">
            {tRows.map(t => {
              if (editId === t.id) return <div key={t.id} className="qh-teamcard is-editing"><TeamForm people={people} init={t} onSave={(x) => { st.updateTeam(x, actor); setEditId(null); }} onCancel={() => setEditId(null)} /></div>;
              if (accessId === t.id) return <div key={t.id} className="qh-teamcard is-editing"><div className="qh-teamform"><div className="qh-teamform-label">Access for team “{t.name}”</div><SubjectAccessEditor st={st} actor={actor} subjectType0="team" subject0={t.name} lockSubject onDone={() => setAccessId(null)} /></div></div>;
              const parents = memberOf(t.id), subs = (t.subteams || []), tgts = grantsOf(t.name);
              return (
                <div key={t.id} className="qh-teamcard">
                  <div className="qh-teamcard-main">
                    <div className="qh-teamcard-head"><span className="qh-team-badge">team</span><span className="qh-teamcard-name">{t.name}</span><span className="qh-teamcard-count">{t.members.length} member{t.members.length === 1 ? '' : 's'}</span>{parents.length > 0 && <span className="qh-teamcard-parent">member of {parents.map(p => p.name).join(', ')}</span>}</div>
                    {t.desc && <div className="qh-teamcard-desc">{t.desc}</div>}
                    <div className="qh-teamcard-members">
                      {t.members.length === 0 && subs.length === 0 ? <span className="qh-team-empty">No members yet</span> : (<>
                        {t.members.map(h => <span key={h} className="qh-memberpill"><span className="qh-mini-avatar sm">{personBy(h).initials || '?'}</span>{personBy(h).name || h}</span>)}
                        {subs.map(id => { const s = teamById(id); return s ? <span key={id} className="qh-subteampill"><span className="qh-team-badge">team</span>{s.name}</span> : null; })}
                      </>)}
                    </div>
                    <div className="qh-teamcard-access">
                      <span className="qh-teamcard-access-lbl">Access</span>
                      {/* Three different sentences, because they are three
                          different facts (design 2026-09-22 §2). "No targets"
                          printed for all of them, and a failed read looked
                          exactly like a team with nothing. `grantsOf` still
                          matches on the team's NAME — that is the contract. */}
                      {st.grantsState === 'error'
                        ? <span className="qh-team-err">Couldn't load this team's grants — its access is unknown, not empty.<button className="qh-linkbtn" onClick={() => st.reloadGrants && st.reloadGrants()}>Try again</button></span>
                        : st.grantsState === 'loading'
                          ? <span className="qh-team-none">Loading…</span>
                          : tgts.length === 0
                            ? <span className="qh-team-none">No access granted to this team</span>
                            : tgts.map(g => <span key={g.id} className="qh-acctarget"><span className="qh-acctarget-t">{qhGrantTarget(g)}</span><TierBadge tier={g.tier} sm /></span>)}
                    </div>
                  </div>
                  <div className="qh-teamcard-acts">
                    <button className="qh-rowbtn" onClick={() => { setAccessId(t.id); setEditId(null); setAdding(false); }}><AIcon.edit />Edit access</button>
                    <button className="qh-rowbtn" onClick={() => { setEditId(t.id); setAccessId(null); setAdding(false); }}><AIcon.edit />Edit team</button>
                    <button className="qh-revoke" onClick={() => { if (window.confirm('Delete team “' + t.name + '”? Members are kept; only the team is removed.')) st.removeTeam(t.id, actor); }}>Delete</button>
                  </div>
                </div>
              );
            })}
            {tRows.length === 0 && <div className="qh-conn-empty">No teams match your filter.</div>}
          </div>
        </>
      ) : (
        <div className="qh-tablewrap">
        <table className="qh-atable qh-acttable">
          <thead><tr><th>Person</th><th>Handle</th><th>Teams</th><th></th></tr></thead>
          <tbody>
            {pRows.map(p => editPerson === p.handle ? (
              <tr key={p.handle} className="qh-editrow"><td colSpan={4}>
                <div className="qh-personedit">
                  <div className="qh-personedit-top"><span className="qh-mini-avatar">{p.initials}</span><b>{qhPersonName(p.name)}</b><span className="qh-muted">— assign to teams (none, one, or several)</span></div>
                  <div className="qh-memberpick">
                    {teams.map(t => { const on = pdraft.includes(t.id); return <button key={t.id} type="button" className={'qh-memberchip' + (on ? ' is-on' : '')} onClick={() => togglePdraft(t.id)}><span className="qh-team-badge">team</span><span className="qh-memberchip-name">{t.name}</span>{on && <span className="qh-memberchip-ck"><AIcon.check /></span>}</button>; })}
                  </div>
                  <div className="qh-teamform-acts"><button className="qh-btn qh-btn-ghost qh-btn-sm" onClick={() => setEditPerson(null)}>Cancel</button><button className="qh-btn qh-btn-primary qh-btn-sm" onClick={savePerson}>Save membership</button></div>
                </div>
              </td></tr>
            ) : (
              <tr key={p.handle}>
                <td><div className="qh-person-cell"><span className="qh-mini-avatar">{p.initials}</span><b>{qhPersonName(p.name)}</b></div></td>
                <td className="qh-mono">{p.handle}</td>
                <td><div className="qh-person-teams">{teamsOf(p.handle).length === 0 ? <span className="qh-team-none">No team</span> : teamsOf(p.handle).map(t => <span key={t.id} className="qh-teamtag">{t.name}</span>)}</div></td>
                <td className="qh-tright"><button className="qh-rowbtn" onClick={() => startEditPerson(p)}><AIcon.edit />Edit teams</button></td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
    </div>
  );
}

// ---------- Connections + endpoint requests ----------
// Engines the registry accepts. Kept in step with engines.WIRED_ENGINES on the
// server, which refuses anything else — an engine can carry a safety profile
// before it can execute, and offering one of those here would let an admin
// register a connection that fails closed at submit time for reasons this
// screen gives no hint of.
// ClickHouse executes since 2026-09-23 (d), read-only over the native protocol:
// 9440 is native over TLS. The 8443 from the cloud console is HTTPS, and wrong
// here — a read-only login cannot cancel a query over HTTP.
const QH_CONN_ENGINES = [
  ['postgres', 'PostgreSQL', 5432],
  ['mssql', 'SQL Server', 1433],
  ['clickhouse', 'ClickHouse', 9440],
];
// Engines where only SELECT / WITH ever run (anything else is refused at
// submit). The form asks for the read-only credential alone: an RW or DDL
// password there would be stored and never used. `athena` is listed before it
// is wired so the rule is already right the day it lands.
const QH_RO_ENGINES = ['clickhouse', 'athena'];
const QH_CRED_TIERS = [
  ['ro', 'Read-only', 'Used for every SELECT, the schema snapshot and the connection test.'],
  ['rw', 'Read/Write', 'Optional. Without it, write queries on this target are refused.'],
  ['ddl', 'Schema (DDL)', 'Optional. Without it, schema changes on this target are refused.'],
];

// One tier's username + password. The password box is always empty on open,
// never prefilled with a placeholder: the server does not send passwords back,
// so a masked value in here would be a lie about what is stored — and leaving
// it blank is what tells the server "keep the current one".
// A connection test answers in colour (operator, CODE 2026-09-23 §6a): green
// for connected, red for refused — on the row's button and in the form alike.
const CONN_TEST_ICON = {
  ok: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5" /></svg>,
  bad: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18" /></svg>,
};
// A row's less-used actions, behind one button. The menu is PORTALLED to
// <body> and placed with position: fixed from the button's rect: inside the
// row it would sit in the pinned actions cell — a sticky box, so its own
// stacking context, painted over by the next rows' pinned cells — and inside
// .qh-tablewrap, whose overflow-x: auto clips it below the last row (CODE
// 2026-09-24). Same shape as the editor's autocomplete. It opens upward when
// there is no room below, and closes on pick, outside click, Escape, and any
// scroll or resize, because a fixed box would otherwise drift off its row.
function ConnRowMenu({ items, busy }) {
  const [pos, setPos] = React.useState(null);   // null = closed | { top, left, up }
  const btnRef = React.useRef(null), popRef = React.useRef(null);
  const place = () => {
    const r = btnRef.current.getBoundingClientRect();
    const h = 4 * 34 + 10, below = window.innerHeight - r.bottom;
    const up = below < h + 8 && r.top > below;
    setPos({ top: up ? r.top - 4 : r.bottom + 4, right: Math.max(8, window.innerWidth - r.right), up });
  };
  React.useEffect(() => {
    if (!pos) return undefined;
    const off = (e) => { if (!(btnRef.current && btnRef.current.contains(e.target)) && !(popRef.current && popRef.current.contains(e.target))) setPos(null); };
    const esc = (e) => { if (e.key === 'Escape') { setPos(null); btnRef.current && btnRef.current.focus(); } };
    const shut = (e) => { if (!(popRef.current && popRef.current.contains(e.target))) setPos(null); };
    const shutAll = () => setPos(null);
    document.addEventListener('mousedown', off); document.addEventListener('keydown', esc);
    window.addEventListener('scroll', shut, true); window.addEventListener('resize', shutAll);
    return () => { document.removeEventListener('mousedown', off); document.removeEventListener('keydown', esc);
      window.removeEventListener('scroll', shut, true); window.removeEventListener('resize', shutAll); };
  }, [pos]);
  const open = !!pos;
  return (
    <>
      <button ref={btnRef} className={'qh-rowbtn qh-rowmenu-btn' + (open ? ' is-open' : '')} onClick={() => (open ? setPos(null) : place())} aria-haspopup="menu" aria-expanded={open} aria-label="More actions" title="More actions">
        {busy ? <span className="qh-spin" /> : <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="1.8" /><circle cx="12" cy="12" r="1.8" /><circle cx="19" cy="12" r="1.8" /></svg>}
      </button>
      {open && ReactDOM.createPortal(
        <div ref={popRef} className={'qh-rowmenu-pop' + (pos.up ? ' is-up' : '')} role="menu" style={{ top: pos.top, right: pos.right }}>
          {items.map(it => (
            <button key={it.label} role="menuitem" className={'qh-rowmenu-item' + (it.danger ? ' is-danger' : '')} onClick={() => { setPos(null); it.on(); }}>
              <span>{it.label}</span>{it.hint && <span className="qh-rowmenu-hint">{it.hint}</span>}
            </button>
          ))}
        </div>, document.body)}
    </>
  );
}
// The queue's checkbox, for picking connections to change together (§6b).
function ConnCheck({ on, some, onChange, label }) {
  return (
    <label className="qh-qcheck qh-conncheck">
      <input type="checkbox" checked={!!on} onChange={onChange} aria-label={label} />
      <span className={'qh-qcheck-box' + (some ? ' is-some' : '')}>{CONN_TEST_ICON.ok}</span>
    </label>
  );
}
function ConnCredRow({ label, hint, stored, value, onChange }) {
  const state = stored
    ? (stored.placeholder ? 'not provisioned yet'
      : stored.configured ? ('stored · ' + (stored.username || '—')) : 'not set')
    : null;
  return (
    <div className="qh-field">
      <span className="qh-field-lbl">{label}{state && <span className="qh-muted"> — {state}</span>}</span>
      <div className="qh-addrow" style={{ marginBottom: 0 }}>
        <input className="qh-input qh-input-sm qh-flex1" placeholder="username"
               autoComplete="off"
               value={value.username} onChange={e => onChange({ ...value, username: e.target.value })} />
        <input className="qh-input qh-input-sm qh-flex1" type="password"
               placeholder={stored && stored.configured ? 'leave blank to keep' : 'password'}
               autoComplete="new-password"
               value={value.password} onChange={e => onChange({ ...value, password: e.target.value })} />
      </div>
      <span className="qh-muted" style={{ fontSize: 11.5 }}>{hint}</span>
    </div>
  );
}

const qhBlankCreds = () => ({ ro: { username: '', password: '' }, rw: { username: '', password: '' }, ddl: { username: '', password: '' } });

// Why a connection cannot be enabled yet, if that is the case. The server
// refuses to enable a target whose read-only password is missing or still the
// import placeholder, so say it here rather than let the admin discover it by
// pressing Enable and reading a 409.
function credNote(c) {
  const ro = (c.credentials || {}).ro;
  if (!ro) return '';
  if (!ro.configured) return 'no read-only credentials';
  if (ro.placeholder) return 'placeholder credentials';
  return '';
}

// ---------- Registry tags: where the machine runs ----------
// `provider` / `service` / `account` are reserved and get real controls; the rest
// is a free key:value list a DBA-admin invents here. The vocabulary is DERIVED
// from the fleet (GET /admin/tag-keys), so the form offers what other
// connections already say instead of admitting a fourth spelling of one account
// id. Tags describe the SERVER — its databases inherit them.
function HostingFields({ tags, onChange, vocab }) {
  const t = tags || {};
  const set = (k, v) => {
    const n = { ...t };
    if (v === '' || v == null) delete n[k]; else n[k] = v;
    // Services are per provider: keeping 'ECS' after a switch to AWS would name
    // a service that provider does not sell.
    if (k === 'provider') delete n.service;
    onChange(n);
  };
  const prov = QH_PROVIDERS[t.provider];
  const custom = Object.keys(t).filter(k => QH_TAG_RESERVED.indexOf(k) < 0).sort();
  const [nk, setNk] = useAcc('');
  const [nv, setNv] = useAcc('');
  const known = (vocab || []).filter(v => !v.reserved).map(v => v.key);
  const valuesFor = (k) => { const e = (vocab || []).find(v => v.key === k); return e ? e.values.map(x => x.value) : []; };
  const addCustom = () => {
    // The server's own rule: ^[a-z][a-z0-9_-]{0,31}$ (a key becomes a search
    // token, so a space could not be typed back). Sanitise here rather than let
    // the 400 be the first time anyone hears about it.
    const k = nk.trim().toLowerCase().replace(/[^a-z0-9_-]/g, '-').replace(/^[^a-z]+/, '').slice(0, 32);
    if (!k || !nv.trim() || QH_TAG_RESERVED.indexOf(k) >= 0) return;
    set(k, nv.trim().slice(0, 120)); setNk(''); setNv('');
  };
  return (
    <div className="qh-field">
      <span className="qh-field-lbl">Where it runs</span>
      <div className="qh-provseg">
        <button className={'qh-provopt' + (!t.provider ? ' is-active' : '')} onClick={() => set('provider', '')}>Untagged</button>
        {Object.keys(QH_PROVIDERS).map(id => (
          <button key={id} className={'qh-provopt' + (t.provider === id ? ' is-active' : '')} onClick={() => set('provider', id)}>
            <img className="qh-prov-logo" src={qhProviderLogo(id)} alt="" draggable={false} />{QH_PROVIDERS[id].label}
          </button>
        ))}
      </div>
      {prov && (
        <div className="qh-tagrow">
          <span className="qh-tagrow-k">service</span>
          <div className="qh-seg qh-seg-sm">
            {prov.services.map(s => <button key={s} className={'qh-seg-opt' + (t.service === s ? ' is-active' : '')} onClick={() => set('service', t.service === s ? '' : s)}>{s}</button>)}
          </div>
        </div>
      )}
      <div className="qh-tagrow">
        <span className="qh-tagrow-k">account</span>
        <input className="qh-input qh-input-sm qh-flex1" list="qh-tagvals-account" maxLength={120} placeholder="account / project id" value={t.account || ''} onChange={e => set('account', e.target.value)} />
        <datalist id="qh-tagvals-account">{valuesFor('account').map(v => <option key={v} value={v} />)}</datalist>
      </div>
      {custom.map(k => (
        <div className="qh-tagrow" key={k}>
          <span className="qh-tagrow-k">{k}</span>
          <input className="qh-input qh-input-sm qh-flex1" maxLength={120} value={t[k]} onChange={e => set(k, e.target.value)} />
          <button className="qh-icon-btn" onClick={() => set(k, '')} aria-label={'Remove ' + k} title={'Remove ' + k}><AIcon.x /></button>
        </div>
      ))}
      <div className="qh-tagrow">
        <input className="qh-input qh-input-sm" style={{ width: 132 }} maxLength={32} list="qh-tagkeys" placeholder="new tag key" value={nk} onChange={e => setNk(e.target.value)} />
        <datalist id="qh-tagkeys">{known.map(k => <option key={k} value={k} />)}</datalist>
        <input className="qh-input qh-input-sm qh-flex1" maxLength={120} placeholder="value" value={nv} onChange={e => setNv(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addCustom(); } }} />
        <button className="qh-btn qh-btn-ghost qh-btn-sm" onClick={addCustom} disabled={!nk.trim() || !nv.trim()}>Add</button>
      </div>
      {/* A new key is not a typo guard — it is a fleet-wide decision: it becomes a
          filter every connection can carry, and two keys meaning the same thing
          is how the tag stops being worth filtering by. */}
      {nk.trim() && known.indexOf(nk.trim().toLowerCase()) < 0 && (
        <div className="qh-tagnew">New key · it becomes a filter for the whole fleet.{known.length ? ' Existing keys: ' + known.join(', ') + '.' : ''}</div>
      )}
    </div>
  );
}

// Add / edit / rotate, one component. `mode` is 'create' | 'edit' | 'rotate';
// rotate shows only the credential block, because rotating a password is the
// routine job and making someone scroll past the host and port to do it is how
// the host and port get changed by accident.
function ConnectionForm({ st, init, mode, onDone }) {
  const editing = mode !== 'create';
  const [f, setF] = useAcc(() => ({
    alias: init ? init.name : '',
    host: init ? (init.host || '') : '',
    port: init && init.port ? String(init.port) : '',
    engine: init ? (init.engineId || 'postgres') : 'postgres',
    defaultDatabase: init ? (init.defaultDatabase || '') : '',
    notes: init ? (init.notes || '') : '',
    tags: init && init.tags ? { ...init.tags } : {},
  }));
  const [creds, setCreds] = useAcc(() => {
    const c = qhBlankCreds();
    // Prefill the usernames so a password rotation does not require retyping
    // a role name the admin would have to go and look up.
    if (init && init.credentials) QH_CRED_TIERS.forEach(([t]) => { c[t].username = (init.credentials[t] || {}).username || ''; });
    return c;
  });
  const [busy, setBusy] = useAcc(false);
  const [probe, setProbe] = useAcc(null);
  // The tag keys and values the fleet already uses, for the suggestions in
  // HostingFields. Failure is silent: it costs the suggestions, not the form.
  const [vocab, setVocab] = useAcc(null);
  React.useEffect(() => {
    let live = true;
    qhApi.adminTagKeys().then(r => { if (live) setVocab(r.keys || []); }).catch(() => {});
    return () => { live = false; };
  }, []);
  const enginePort = (QH_CONN_ENGINES.find(e => e[0] === f.engine) || [])[2];
  const valid = f.alias.trim() && f.host.trim() && f.defaultDatabase.trim();

  // Only tiers the admin actually touched are sent: an untouched tier must not
  // overwrite what is stored, and on edit the username boxes start prefilled.
  const changedCreds = () => {
    const out = {};
    QH_CRED_TIERS.forEach(([t]) => {
      if (t !== 'ro' && QH_RO_ENGINES.indexOf(f.engine) >= 0) return;
      const cur = creds[t];
      const was = ((init && init.credentials && init.credentials[t]) || {}).username || '';
      const u = cur.username.trim();
      if (cur.password || (u && u !== was)) out[t] = { username: u || null, password: cur.password || null };
    });
    return out;
  };

  const test = () => {
    setBusy(true); setProbe(null);
    // With a typed RO password, probe exactly what is on screen — that is the
    // point of testing before saving. Without one, only the stored credential
    // can answer, and on a connection that does not exist yet there is no
    // stored credential to fall back to.
    const typed = creds.ro.password;
    if (!typed && !init) {
      setBusy(false);
      setProbe({ ok: false, error: 'Enter the read-only username and password to test before saving.' });
      return;
    }
    const p = typed
      ? qhApi.adminTestNewConnection({ host: f.host.trim(), port: f.port ? parseInt(f.port, 10) : null, engine: f.engine, defaultDatabase: f.defaultDatabase.trim(), username: creds.ro.username.trim(), password: typed })
      : qhApi.adminTestConnection(init.id);
    p.then(r => setProbe(r || { ok: false, error: 'No answer.' }))
      .catch(e => setProbe({ ok: false, error: (e && e.message) || 'Test failed.' }))
      .finally(() => setBusy(false));
  };

  const save = () => {
    if (!valid && mode !== 'rotate') return;
    setBusy(true);
    const payload = mode === 'rotate'
      ? { credentials: changedCreds() }
      : {
        alias: f.alias.trim(), host: f.host.trim(),
        port: f.port ? parseInt(f.port, 10) : null, engine: f.engine,
        defaultDatabase: f.defaultDatabase.trim(),
        notes: f.notes.trim(), credentials: changedCreds(),
        // The whole bag, every save: a merge patch cannot say “this key is gone”.
        tags: f.tags,
      };
    const p = editing ? st.updateConnection(init.id, payload) : st.addConnection(payload);
    p.then(ok => { setBusy(false); if (ok) onDone(); });
  };

  const title = mode === 'create' ? 'Add connection'
    : mode === 'rotate' ? ('Rotate credentials · ' + init.name)
      : ('Edit connection · ' + init.name);
  const sub = mode === 'create'
    ? 'Registers a target server. It starts disabled — set credentials, test it, then enable it deliberately.'
    : mode === 'rotate'
      ? 'Passwords are stored encrypted and never sent back to this screen. Leave a box blank to keep the current value.'
      : 'Changing the alias also changes how grants and admin scopes name this connection.';

  return (
    <QhModal onClose={busy ? (() => {}) : onDone}>
      <div className="qh-modal-head">
        <div>
          <div className="qh-modal-title">{title}</div>
          <div className="qh-modal-sub">{sub}</div>
        </div>
        <button className="qh-icon-btn" onClick={onDone} aria-label="Close"><AIcon.x /></button>
      </div>
      <div className="qh-modal-body">
        {mode !== 'rotate' && (<>
          <label className="qh-field">
            <span className="qh-field-lbl">Alias</span>
            <input className="qh-input" placeholder="e.g. prod-beta" value={f.alias} onChange={e => setF({ ...f, alias: e.target.value })} />
          </label>
          <div className="qh-field">
            <span className="qh-field-lbl">Engine</span>
            <div className="qh-seg">
              {QH_CONN_ENGINES.map(([v, l]) => <button key={v} className={'qh-seg-opt' + (f.engine === v ? ' is-active' : '')} onClick={() => setF({ ...f, engine: v })}>{l}</button>)}
            </div>
          </div>
          <div className="qh-addrow" style={{ marginBottom: 0 }}>
            <input className="qh-input qh-input-sm qh-flex1" placeholder="host — e.g. db.example.internal" value={f.host} onChange={e => setF({ ...f, host: e.target.value })} />
            <input className="qh-input qh-input-sm" style={{ width: 110 }} placeholder={'port ' + enginePort} value={f.port} onChange={e => setF({ ...f, port: e.target.value.replace(/\D/g, '') })} />
          </div>
          <label className="qh-field">
            <span className="qh-field-lbl">Default database</span>
            <input className="qh-input" placeholder="the database the bot connects to first" value={f.defaultDatabase} onChange={e => setF({ ...f, defaultDatabase: e.target.value })} />
          </label>
          <label className="qh-field">
            <span className="qh-field-lbl">Notes</span>
            <input className="qh-input" placeholder="optional — who owns it, why it exists" value={f.notes} onChange={e => setF({ ...f, notes: e.target.value })} />
          </label>
          <HostingFields tags={f.tags} vocab={vocab} onChange={tags => setF({ ...f, tags })} />
        </>)}
        {QH_RO_ENGINES.indexOf(f.engine) >= 0 && <div className="qh-req-note">Read-only engine — only SELECT and WITH run here.</div>}
        {QH_CRED_TIERS.filter(([t]) => t === 'ro' || QH_RO_ENGINES.indexOf(f.engine) < 0).map(([t, label, hint]) => (
          <ConnCredRow key={t} label={label} hint={hint}
                       stored={init && init.credentials ? init.credentials[t] : null}
                       value={creds[t]} onChange={v => setCreds({ ...creds, [t]: v })} />
        ))}
        {probe && (
          <div className={'qh-probe ' + (probe.ok ? 'is-ok' : 'is-bad')} role="status">
            {probe.ok ? CONN_TEST_ICON.ok : CONN_TEST_ICON.bad}
            <span>{probe.ok
              ? ('Connected' + (probe.serverVersion ? ' · server ' + probe.serverVersion : '') + (probe.latencyMs != null ? ' · ' + probe.latencyMs + ' ms' : ''))
              : ('Could not connect — ' + (probe.error || 'unknown error'))}</span>
          </div>
        )}
      </div>
      <div className="qh-modal-foot">
        <button className="qh-btn qh-btn-ghost" onClick={onDone} disabled={busy}>Cancel</button>
        {mode !== 'rotate' && <button className="qh-btn qh-btn-ghost" onClick={test} disabled={busy || !f.host.trim() || !f.defaultDatabase.trim()}>{busy ? <span className="qh-spin" /> : 'Test connection'}</button>}
        {/* Rotate with nothing typed would post an empty patch and toast
            "updated" for a save that changed nothing. */}
        <button className="qh-btn qh-btn-primary" onClick={save} disabled={busy || (mode === 'rotate' ? Object.keys(changedCreds()).length === 0 : !valid)}>{mode === 'create' ? 'Add connection' : 'Save'}</button>
      </div>
    </QhModal>
  );
}

function ConnectionsView({ st, user }) {
  const act = 'dba.' + user.name.split(' ')[0].toLowerCase();
  const pending = st.endpointReqs.filter(e => e.status === 'submitted');
  const [refreshing, setRefreshing] = React.useState(null);
  const [q, setQ] = React.useState('');
  const [envF, setEnvF] = React.useState('all');
  const [provF, setProvF] = React.useState('all');
  // No default client sort. The server orders by `enabled DESC, alias`, so
  // disabled targets arrive LAST and an alphabetical re-sort here would
  // silently undo that (CODE brief 2026-08-20 §5). A clicked header still
  // sorts — that is the admin asking, not the client second-guessing.
  const [sort, setSort] = React.useState({ key: null, dir: 'asc' });
  const [form, setForm] = React.useState(null);     // {mode, conn} while a modal is open
  const [testing, setTesting] = React.useState(null);
  const [tested, setTested] = React.useState({});   // alias -> last probe result
  // Bulk (CODE 2026-09-23 §6b): picked connection ids, the one-credential
  // panel, and what the server refused. The write is all or nothing, so a
  // refusal is a LIST, each line naming its connection.
  const [sel, setSel] = React.useState([]);
  const [bulk, setBulk] = React.useState(null);        // null | { enable, tier, username, password }
  const [bulkBusy, setBulkBusy] = React.useState(false);
  const [refused, setRefused] = React.useState(null);  // null | { body, list, msg }
  const allConns = st.connectionRows || st.connections || [];
  const selRows = allConns.filter(c => sel.indexOf(c.id) >= 0);
  const refusedNames = refused ? refused.list.map(x => x.connection) : [];
  const toggleSel = (id) => { setRefused(null); setSel(xs => xs.indexOf(id) >= 0 ? xs.filter(x => x !== id) : xs.concat([id])); };
  // Dry run first: the write changes all of them or none, so asking before
  // writing turns "nothing changed" into "these two would have stopped it" —
  // with a way on from there, instead of a toast.
  const runBulk = (body) => {
    if (bulkBusy || !selRows.length) return;
    setBulkBusy(true); setRefused(null);
    const req = { connections: selRows.map(c => c.name), ...body };
    st.bulkConnections({ ...req, dryRun: true })
      .then(() => st.bulkConnections({ ...req, dryRun: false }))
      .then(() => { setBulkBusy(false); setSel([]); setBulk(null); })
      .catch(e => { setBulkBusy(false); setRefused({ body, list: (e && e.refused) || [], msg: (e && e.message) || 'Nothing was changed.' }); });
  };
  const bulkDisable = () => {
    if (!window.confirm('Disable ' + selRows.length + ' connection' + (selRows.length === 1 ? '' : 's') + '? Developers lose access to them until they are enabled again. Running queries are unaffected.')) return;
    runBulk({ enabled: false });
  };
  const bulkCredSave = () => {
    const b = bulk;
    if (!b || !b.username.trim() || !b.password) return;
    runBulk({ ...(b.enable ? { enabled: true } : {}), credentials: { [b.tier]: { username: b.username.trim(), password: b.password } } });
  };
  const toggleSort = (key) => setSort(s => (s.key === key ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: 'asc' }));
  const refreshSchema = (c) => {
    if (refreshing) return;
    setRefreshing(c.id);
    qhApi.adminSchemaRefresh(c.id)
      .then(r => { const n = r.tables || 0; st.pushToast && st.pushToast('Refreshed ' + n + ' table' + (n === 1 ? '' : 's') + ' on ' + c.name + '.'); })
      .catch(e => st.pushToast && st.pushToast((e && e.message) || ("Couldn't refresh " + c.name + "'s schema.")))
      .finally(() => setRefreshing(null));
  };
  // Probe the STORED credential. Answers ok:false rather than rejecting when
  // the target refuses, so the failure path is the toast, not the catch.
  const testConnection = (c) => {
    if (testing) return;
    setTesting(c.id);
    qhApi.adminTestConnection(c.id)
      .then(r => {
        setTested(t => ({ ...t, [c.id]: r }));
        st.pushToast && st.pushToast(r && r.ok
          ? ('Connected to ' + c.name + (r.serverVersion ? ' · server ' + r.serverVersion : '') + (r.latencyMs != null ? ' · ' + r.latencyMs + ' ms' : ''))
          : (c.name + ' — ' + ((r && r.error) || 'could not connect.')));
      })
      .catch(e => st.pushToast && st.pushToast((e && e.message) || ("Couldn't test " + c.name + '.')))
      .finally(() => setTesting(null));
  };
  const toggleEnabled = (c) => {
    // Disabling pulls a target out of every picker mid-flight, so it asks
    // first; enabling is the reversible direction and does not.
    if (c.enabled && !window.confirm(c.replicaOf
      ? 'Take “' + c.name + '” out of rotation? Read-only queries on ' + c.replicaOf + ' run on the primary until it is enabled again. Nobody loses access.'
      : 'Disable “' + c.name + '”? Developers lose access to it until it is enabled again. Running queries are unaffected.')) return;
    st.setConnectionEnabled(c.id, !c.enabled);
  };
  const removeConnection = (c) => {
    if (!window.confirm('Delete “' + c.name + '”? If any query history or live grants still point at it, it will be disabled instead of deleted.')) return;
    st.removeConnection(c.id);
  };
  return (
    <div className="qh-apad">
      <div className="qh-aview-head">
        <div><div className="qh-aview-title">Connections & endpoint requests</div><div className="qh-aview-sub">Registered databases and pending access requests from developers.</div></div>
        <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={() => setForm({ mode: 'create', conn: null })}><AIcon.plus />Add connection</button>
      </div>
      {form && <ConnectionForm st={st} init={form.conn} mode={form.mode} onDone={() => setForm(null)} />}

      {pending.length > 0 && <div className="qh-section-label">Pending requests · {pending.length}</div>}
      <div className="qh-erlist">
        {pending.map(er => (
          <div key={er.id} className="qh-ercard">
            <div className="qh-ercard-main">
              <div className="qh-ercard-top"><span className="qh-mono qh-ertarget">{er.server}/{er.database}</span><TierBadge tier={er.tier} sm /><span className="qh-qcard-when">{qhAgo(er.requestedAt)}</span></div>
              <div className="qh-ercard-reason">{er.reason}</div>
              {/* The requester arrives as a principal id here (there is no name
                  on the endpoint-request row), so it is read as a name and the
                  handle stays on `title`. */}
              <div className="qh-ercard-by">requested by <b title={qhIsHandleName(er.requester) ? er.requester : null}>{qhPersonName(er.requester)}</b></div>
            </div>
            <div className="qh-ercard-actions">
              <button className="qh-btn qh-btn-danger qh-btn-sm" onClick={() => st.decideEndpoint(er.id, false, act)}>Reject</button>
              <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={() => st.decideEndpoint(er.id, true, act)}>Provision</button>
            </div>
          </div>
        ))}
      </div>

      <div className="qh-section-label">Registered connections · {allConns.filter(c => !c.replicaOf).length}{allConns.some(c => c.replicaOf) ? <span className="qh-muted"> · {allConns.filter(c => c.replicaOf).length} read replica{allConns.filter(c => c.replicaOf).length === 1 ? '' : 's'}</span> : null}</div>
      <div className="qh-conn-controls">
        <div className="qh-search sm">
          <svg className="qh-search-ic" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>
          <input className="qh-search-in" placeholder="Filter by name, engine or database…" value={q} onChange={e => setQ(e.target.value)} />
          {q && <button className="qh-search-x" onMouseDown={e => { e.preventDefault(); setQ(''); }} aria-label="Clear"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>}
        </div>
        <div className="qh-seg qh-seg-sm">
          {[['all', 'All'], ['production', 'Production'], ['staging', 'Staging']].map(([v, l]) => <button key={v} className={'qh-seg-opt' + (envF === v ? ' is-active' : '')} onClick={() => setEnvF(v)}>{l}</button>)}
        </div>
        {/* Which cloud runs it. 'Untagged' is a filter of its own, because the
            registry gap is the thing an admin needs to find and close. */}
        <div className="qh-seg qh-seg-sm">
          {[['all', 'Any host'], ...Object.keys(QH_PROVIDERS).map(id => [id, QH_PROVIDERS[id].label]), ['none', 'Untagged']].map(([v, l]) => (
            <button key={v} className={'qh-seg-opt' + (provF === v ? ' is-active' : '')} onClick={() => setProvF(v)}>
              {QH_PROVIDERS[v] && <img className="qh-prov-logo" src={qhProviderLogo(v)} alt="" draggable={false} />}{l}
            </button>
          ))}
        </div>
      </div>
      {(() => {
        const dir = sort.dir === 'asc' ? 1 : -1;
        const rows0 = allConns
          .filter(c => envF === 'all' || c.env === envF)
          .filter(c => provF === 'all' || (provF === 'none' ? !qhProvider(c) : qhTags(c).provider === provF))
          .filter(c => { const t = q.trim().toLowerCase(); if (!t) return true; return (c.name + ' ' + c.engine + ' ' + (c.host || '') + ' ' + qhHostingFull(c) + ' ' + (c.databases || []).map(d => d.name).join(' ')).toLowerCase().includes(t); })
          .slice();
        if (sort.key) rows0.sort((a, b) => {
          let av, bv;
          if (sort.key === 'dbs') { av = (a.databases || []).length; bv = (b.databases || []).length; }
          else if (sort.key === 'hosting') { av = (qhHosting(a) || 'zzz').toLowerCase(); bv = (qhHosting(b) || 'zzz').toLowerCase(); }
          else { av = String(a[sort.key]).toLowerCase(); bv = String(b[sort.key]).toLowerCase(); }
          return av < bv ? -dir : av > bv ? dir : 0;
        });
        // A replica sits UNDER its primary (CODE 2026-09-23 (e)), whatever the
        // sort: it has no identity of its own anyone queries by. A replica whose
        // primary the filter hid stays in place, standalone, so it is not lost.
        const shown = new Set(rows0.map(c => c.name));
        const rows = [];
        rows0.forEach(c => {
          if (c.replicaOf && shown.has(c.replicaOf)) return;
          rows.push(c);
          rows0.filter(r => r.replicaOf === c.name).forEach(r => rows.push(r));
        });
        const arrow = (k) => (sort.key === k ? (sort.dir === 'asc' ? ' ↑' : ' ↓') : '');
        const th = (k, label, cls) => <th className={'qh-sort-th' + (sort.key === k ? ' is-sorted' : '') + (cls || '')} onClick={() => toggleSort(k)}>{label}<span className="qh-sort-arw">{arrow(k)}</span></th>;
        const vis = rows.map(c => c.id);
        const allOn = vis.length > 0 && vis.every(id => sel.indexOf(id) >= 0);
        const someOn = !allOn && vis.some(id => sel.indexOf(id) >= 0);
        const hidden = sel.filter(id => vis.indexOf(id) < 0).length;
        const credWord = bulk ? { ro: 'read-only', rw: 'read/write', ddl: 'DDL' }[bulk.tier] : '';
        const had = bulk ? selRows.filter(c => { const k = (c.credentials || {})[bulk.tier]; return k && k.configured && !k.placeholder; }).length : 0;
        const nSel = selRows.length;
        return (
          <>
          {sel.length > 0 && (
            <div className="qh-connbulk">
              <div className="qh-connbulk-bar">
                <span className="qh-connbulk-n">{sel.length} selected{hidden ? ' · ' + hidden + ' hidden by the filter' : ''}</span>
                <div className="qh-flex1" />
                <button className="qh-btn qh-btn-ghost qh-btn-sm" disabled={bulkBusy} onClick={() => { setBulk(null); runBulk({ enabled: true }); }}>Enable</button>
                <button className="qh-btn qh-btn-ghost qh-btn-sm" disabled={bulkBusy} onClick={() => { setBulk(null); bulkDisable(); }}>Disable</button>
                <button className="qh-btn qh-btn-ghost qh-btn-sm" disabled={bulkBusy} onClick={() => { setRefused(null); setBulk({ enable: false, tier: 'ro', username: '', password: '' }); }}>Set a credential…</button>
                <button className="qh-btn qh-btn-ghost qh-btn-sm" disabled={bulkBusy} onClick={() => { setSel([]); setBulk(null); setRefused(null); }}>Clear</button>
                {bulkBusy && <span className="qh-spin" />}
              </div>
              {refused && (
                <div className="qh-connbulk-refused" role="alert">
                  <div className="qh-connbulk-refused-h">{refused.list.length ? 'Nothing was changed — ' + refused.list.length + ' of ' + nSel + ' would have been refused:' : refused.msg}</div>
                  {refused.list.map(x => <div key={x.connection} className="qh-connbulk-refused-row"><b className="qh-mono">{x.connection}</b> — {x.reason}</div>)}
                  {refused.list.length > 0 && (
                    <div className="qh-connbulk-acts">
                      {refused.list.length < nSel && <button className="qh-btn qh-btn-ghost qh-btn-sm" onClick={() => { setSel(xs => xs.filter(id => { const c = allConns.find(x => x.id === id); return !c || refusedNames.indexOf(c.name) < 0; })); setRefused(null); }}>Leave {refused.list.length === 1 ? 'it' : 'those ' + refused.list.length} out</button>}
                      {refused.body.enabled === true && !refused.body.credentials && <button className="qh-btn qh-btn-ghost qh-btn-sm" onClick={() => { setRefused(null); setBulk({ enable: true, tier: 'ro', username: '', password: '' }); }}>Set one read-only credential, then enable</button>}
                    </div>
                  )}
                </div>
              )}
              {bulk && (
                <div className="qh-connbulk-cred">
                  <div className="qh-connbulk-cred-row">
                    {bulk.enable
                      ? <span className="qh-rolefield-l">Read-only credential</span>
                      : <div className="qh-seg qh-seg-sm">{[['ro', 'Read-only'], ['rw', 'Read/Write'], ['ddl', 'DDL']].map(([t, l]) => <button key={t} className={'qh-seg-opt' + (bulk.tier === t ? ' is-active' : '')} onClick={() => setBulk({ ...bulk, tier: t })}>{l}</button>)}</div>}
                    <input className="qh-input qh-input-sm" placeholder="username" autoComplete="off" autoFocus value={bulk.username} onChange={e => setBulk({ ...bulk, username: e.target.value })} />
                    <input className="qh-input qh-input-sm" type="password" placeholder="password" autoComplete="new-password" value={bulk.password} onChange={e => setBulk({ ...bulk, password: e.target.value })} />
                  </div>
                  {/* The consequence, said before Save: ONE credential lands on every
                      selected connection, including the ones that already had one. */}
                  <div className="qh-connbulk-say">Sets the {credWord} credential on <b>{nSel} connection{nSel === 1 ? '' : 's'}</b>{had ? <>, replacing the one {had === nSel ? (nSel === 1 ? 'it has' : 'each has') : <><b>{had}</b> of them have</>} now</> : ''}{bulk.enable ? ', then enables them' : ''}. Stored encrypted and never shown again.</div>
                  <div className="qh-connbulk-acts">
                    <button className="qh-btn qh-btn-ghost qh-btn-sm" onClick={() => setBulk(null)} disabled={bulkBusy}>Cancel</button>
                    <button className="qh-btn qh-btn-primary qh-btn-sm" disabled={bulkBusy || !bulk.username.trim() || !bulk.password} onClick={bulkCredSave}>{(bulk.enable ? 'Set and enable ' : 'Set on ') + nSel}</button>
                  </div>
                </div>
              )}
            </div>
          )}
          <div className="qh-tablewrap">
          <table className="qh-atable qh-conntable qh-acttable">
            <thead><tr><th className="qh-conn-selcol"><ConnCheck on={allOn} some={someOn} label={allOn ? 'Deselect every connection shown' : 'Select all ' + vis.length + ' connections shown'} onChange={() => { setRefused(null); setSel(xs => allOn ? xs.filter(id => vis.indexOf(id) < 0) : [...new Set(xs.concat(vis))]); }} /></th>{th('name', 'Connection')}{th('engine', 'Engine · hosting')}{th('enabled', 'Status')}{th('dbs', 'Databases')}<th className="qh-tright">Actions</th></tr></thead>
            <tbody>
              {rows.map(c => {
                const probe = tested[c.id];
                return (
                <tr key={c.id} className={(sel.indexOf(c.id) >= 0 ? 'is-sel' : '') + (refusedNames.indexOf(c.name) >= 0 ? ' is-refused' : '') + (c.replicaOf ? ' is-replica' : '')}>
                  <td className="qh-conn-selcol"><ConnCheck on={sel.indexOf(c.id) >= 0} onChange={() => toggleSel(c.id)} label={'Select ' + c.name} /></td>
                  <td className="qh-conn-name-td"><div className="qh-conn-namecell"><img className="qh-engine-logo" src={qhEngineLogo(c)} alt="" draggable={false} /><b title={c.name}>{c.name}</b><span className={'qh-envtag env-' + c.env}>{c.env}</span></div>{c.host && <div className="qh-muted qh-mono qh-conn-host" title={c.host + ':' + c.port + '/' + c.defaultDatabase} style={{ fontSize: 11.5 }}>{c.host}:{c.port}/{c.defaultDatabase}</div>}</td>
                  {/* Engine over hosting in ONE column, and the environment beside
                      the name: seven columns plus pinned actions were ~400px wider
                      than the panel, so Databases sat under the actions. The
                      account and custom tags stay on the hover. */}
                  <td className="qh-conn-engtd">
                    <div className="qh-conn-eng">{c.engine}</div>
                    {qhProvider(c)
                      ? <div className="qh-conn-namecell qh-hostcell" title={qhHostingFull(c)}><img className="qh-prov-logo" src={qhProviderLogo(qhTags(c).provider)} alt="" draggable={false} />{qhHosting(c)}</div>
                      : <span className="qh-expiry is-soon" title="No provider tag — nothing here says where this server runs.">untagged</span>}
                  </td>
                  {/* `qh-expiry` is the table's existing "state worth
                      noticing" text: neutral normally, red for is-exp, amber
                      for is-soon. A disabled connection is the row you want
                      to spot, and unset or placeholder credentials are the
                      reason it usually cannot be enabled yet. */}
                  <td>
                    {/* A replica runs on its primary's login, so it has no
                        credential to be missing; "enabled" means "in rotation". */}
                    <span className={'qh-expiry' + (c.enabled ? '' : ' is-exp')}>{c.replicaOf ? (c.enabled ? 'in rotation' : 'out of rotation') : (c.enabled ? 'enabled' : 'disabled')}</span>
                    {c.replicaOf
                      ? <div className="qh-replica-chip" title={'Read-only queries on ' + c.replicaOf + ' can run here. It uses ' + c.replicaOf + '’s login, and nobody picks it by name.'}>Replica of {c.replicaOf}</div>
                      : credNote(c) && <div className="qh-expiry is-soon">{credNote(c)}</div>}
                  </td>
                  <td><div className="qh-conn-dbcell">{(c.databases || []).map(d => <span key={d.id} className="qh-dbchip">{d.name}{d.tier && <TierBadge tier={d.tier} sm />}</span>)}</div></td>
                  <td className="qh-tright"><div className="qh-rowacts">
                    {/* The last answer, in its colour (operator, CODE 2026-09-23 §6a):
                        green with the latency, red with the error on hover. Still a
                        button — pressing it tests again. */}
                    <button className={'qh-rowbtn qh-testbtn' + (probe && testing !== c.id ? (probe.ok ? ' is-ok' : ' is-bad') : '')} disabled={testing === c.id} onClick={() => testConnection(c)}
                      title={probe ? (probe.ok ? 'Connected' + (probe.serverVersion ? ' · server ' + probe.serverVersion : '') + ' — press to test again' : 'Could not connect — ' + (probe.error || 'unknown error') + ' — press to test again') : 'Open one connection with the stored read-only credential'}>
                      {testing === c.id ? <span className="qh-spin" /> : probe ? (probe.ok ? <>{CONN_TEST_ICON.ok}{probe.latencyMs != null ? 'OK · ' + probe.latencyMs + ' ms' : 'OK'}</> : <>{CONN_TEST_ICON.bad}Failed</>) : 'Test'}
                    </button>
                    <button className="qh-rowbtn" onClick={() => setForm({ mode: 'edit', conn: c })}><AIcon.edit />Edit</button>
                    {/* Six buttons per row made the pinned actions column wider than
                        the Databases column it sat on top of. The two used most stay
                        out; the rest are one click further, in the row's menu. */}
                    <ConnRowMenu busy={refreshing === c.id} items={[
                      !c.replicaOf && { label: 'Rotate credentials', on: () => setForm({ mode: 'rotate', conn: c }) },
                      { label: c.replicaOf ? (c.enabled ? 'Take out of rotation' : 'Put back in rotation') : (c.enabled ? 'Disable' : 'Enable'), on: () => toggleEnabled(c) },
                      { label: 'Refresh schema', hint: 'otherwise hourly', on: () => refreshSchema(c) },
                      { label: 'Delete', danger: true, on: () => removeConnection(c) },
                    ].filter(Boolean)} />
                  </div></td>
                </tr>
                );
              })}
              {rows.length === 0 && <tr><td colSpan={6} className="qh-conn-empty">No connections match your filter.</td></tr>}
            </tbody>
          </table>
          </div>
          </>
        );
      })()}
    </div>
  );
}

// `PersonPick` / `ExpiryPick` / the expiry helpers are shared with the
// person-first screen (`qh-admin-person.jsx`), which is a separate Babel scope:
// one picker for every field that names a person, one expiry control everywhere
// a grant can end — a second copy of either is a second set of rules.
// `AccGroupBy` goes with them: the Roles screen groups by team, and its own
// segmented control would be a second grouping idiom on one page.
Object.assign(window, { GrantsView, AutoView, ScopesView, TeamsView, ConnectionsView, SubjectAccessEditor, subjLabel, grantName, PersonPick, ExpiryPick, ExpiryNote, expIso, expBad, expForm, DbMultiPick, TierSelect, connLabel, AccGroupBy });
