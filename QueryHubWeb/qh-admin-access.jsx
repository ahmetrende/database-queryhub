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
function subjLabel(people, subjectType, subject) {
  if (subjectType === 'user') { const p = (people || []).find(x => x.handle === subject); return p ? p.name : subject; }
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
function grantName(g, people) { return g.subjectName || subjLabel(people, g.subjectType, g.subject); }

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

// ---------- Subject-centric access editor: one subject → many targets ----------
function SubjectAccessEditor({ st, actor, subjectType0, subject0, name0, lockSubject, onDone }) {
  const people = st.people, teams = st.teams;
  const conns = st.connections || [];
  const [subjectType, setSubjectType] = useAcc(subjectType0 || 'user');
  const [subject, setSubject] = useAcc(subject0 || '');
  const rowsForSubject = (stype, subj) => {
    const ex = st.grants.filter(g => g.subjectType === stype && g.subject === subj);
    return ex.length ? ex.map(g => ({ connectionId: g.connectionId, databases: (g.databases && g.databases.length) ? g.databases : ['*'], tier: g.tier, ...expForm(g.expiresAt) })) : [blankTarget(conns)];
  };
  const [rows, setRows] = useAcc(() => subject0 ? rowsForSubject(subjectType0, subject0) : [blankTarget(conns)]);

  const pickType = (v) => { setSubjectType(v); setSubject(''); setRows([blankTarget(conns)]); };
  const pickSubject = (val) => { setSubject(val); setRows(val ? rowsForSubject(subjectType, val) : [blankTarget(conns)]); };
  const setRow = (i, patch) => setRows(rs => rs.map((r, j) => j === i ? { ...r, ...patch } : r));
  const removeRow = (i) => setRows(rs => rs.filter((_, j) => j !== i));
  const addRow = () => setRows(rs => [...rs, blankTarget(conns)]);
  const save = () => {
    if (!subject.trim() || rows.some(expBad)) return;
    const targets = rows.map(r => ({ connectionId: r.connectionId, databases: r.databases, tier: r.tier, expiresAt: expIso(r) }));
    st.setSubjectGrants(subjectType, subject.trim(), targets, actor);
    onDone();
  };
  // ONE save control, rendered in two places (CODE brief 2026-08-20 §1): a
  // subject with a dozen connections pushes the only Save below the fold, so a
  // change made at the top is committed by scrolling back down to find it. Same
  // function, same disabled rule — two buttons that could disagree about
  // whether the form is savable would be worse than one badly placed.
  // The rule now matches `save`'s own guard: a bad date used to leave the
  // button enabled and the click did nothing.
  const blocked = !subject.trim() || rows.some(expBad);
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
          ? <span className="qh-accedit-subjname"><span className={'qh-subj-type ' + subjectType}>{subjectType}</span> <b>{name0 || subjLabel(people, subjectType, subject)}</b></span>
          : subjectType === 'user'
            ? <select className="qh-select" value={subject} onChange={e => pickSubject(e.target.value)}><option value="">Select user…</option>{people.map(p => <option key={p.handle} value={p.handle}>{p.name} · {p.handle}</option>)}</select>
            : <select className="qh-select" value={subject} onChange={e => pickSubject(e.target.value)}><option value="">Select team…</option>{teams.map(t => <option key={t.id} value={t.name}>{t.name}</option>)}</select>}
        {acts('top')}
      </div>

      <div className="qh-accedit-label">Targets · {rows.length}<span className="qh-accedit-hint">one row per connection — databases (or all), a single tier, and an end date only if the access should stop</span></div>
      <div className="qh-acctargets">
        {rows.map((r, i) => (
          <div key={i} className="qh-accrow">
            <div className="qh-accrow-head">
              <select className="qh-select" value={r.connectionId} onChange={e => setRow(i, { connectionId: e.target.value, databases: ['*'] })}>{conns.map(c => <option key={c.id} value={c.id}>{connLabel(c)}</option>)}</select>
              <TierSelect value={r.tier} onChange={v => setRow(i, { tier: v })} />
              <ExpiryPick f={r} onChange={p => setRow(i, p)} />
              <button className="qh-accrow-x" onClick={() => removeRow(i)} aria-label="Remove target"><AIcon.x /></button>
            </div>
            <DbMultiPick conns={conns} connectionId={r.connectionId} databases={r.databases} onChange={dbs => setRow(i, { databases: dbs })} />
            <ExpiryNote f={r} subjectType={subjectType} />
          </div>
        ))}
        {rows.length === 0 && <div className="qh-acc-none">No targets — saving will remove all access for this subject.</div>}
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
    if (editing) st.updateGrant({ ...payload, id: f.id }, actor); else st.addGrant(payload, actor);
    onDone();
  };
  return (
    <div className="qh-addrow wrap">
      <div className="qh-seg qh-seg-sm">
        {['user', 'team'].map(v => <button key={v} className={'qh-seg-opt' + (f.subjectType === v ? ' is-active' : '')} onClick={() => setF({ ...f, subjectType: v, subject: '' })}>{v}</button>)}
      </div>
      {f.subjectType === 'user'
        ? <select className="qh-select" value={f.subject} onChange={e => setF({ ...f, subject: e.target.value })}><option value="">Select user…</option>{people.map(p => <option key={p.handle} value={p.handle}>{p.name} · {p.handle}</option>)}</select>
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

function GrantsView({ st, user }) {
  const actor = 'dba.' + user.name.split(' ')[0].toLowerCase();
  const [mode, setMode] = useAcc('subject');
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
        <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={newBtn}><AIcon.plus />{mode === 'grant' ? 'New grant' : 'Grant access'}</button>
      </div>

      <div className="qh-conn-controls">
        <AccSearch q={q} setQ={setQ} placeholder={mode === 'subject' ? 'Filter by subject or target…' : 'Filter by subject, server, database…'} />
        <AccGroupBy label="View" group={mode} setGroup={setMode} options={[['subject', 'By subject'], ['grant', 'By grant']]} />
        {mode === 'grant' && <AccGroupBy group={group} setGroup={setGroup} options={[['none', 'None'], ['subject', 'Subject'], ['server', 'Server'], ['database', 'Database']]} />}
      </div>

      {mode === 'subject' ? (
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
                  <td className="qh-muted">{g.grantedBy}</td>
                  <td className="qh-tright"><div className="qh-rowacts"><button className="qh-rowbtn" onClick={() => { setEditId(g.id); setAdding(false); }}><AIcon.edit />Edit</button><button className="qh-revoke" onClick={() => st.revokeGrant(g.id, actor)}>Revoke</button></div></td>
                </tr>
              );
            }
            return (
              <table className="qh-atable">
                <thead><tr><th>Subject</th><th>Target</th><th>Tier</th><th>Expires</th><th>Granted by</th><th></th></tr></thead>
                <tbody>
                  {grouped.map(([k, list]) => <React.Fragment key={k || 'all'}>{k && <tr className="qh-grouphead"><td colSpan={6}>{k}<span className="qh-grouphead-n">{list.length}</span></td></tr>}{list.map(renderRow)}</React.Fragment>)}
                  {rows.length === 0 && <tr><td colSpan={6} className="qh-conn-empty">No grants match your filter.</td></tr>}
                </tbody>
              </table>
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
      <input className="qh-input qh-input-sm" placeholder="user or team" value={f.user} onChange={e => setF({ ...f, user: e.target.value })} />
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
      {(f.ttl === 'none' || (f.ttl === 'keep' && !f.expiresAt)) && <div className="qh-exp-note">No end date — this subject keeps skipping review on that target until someone revokes the grant.</div>}
    </div>
  );
}

function AutoView({ st, user }) {
  const actor = 'dba.' + user.name.split(' ')[0].toLowerCase();
  const [adding, setAdding] = useAcc(false);
  const [editId, setEditId] = useAcc(null);
  const [q, setQ] = useAcc('');
  const [group, setGroup] = useAcc('none');
  // A bounded window stays the DEFAULT: auto-approve is the grant that skips a
  // human, so "forever" is a thing to ask for, not a thing to land on.
  const blank = { user: '', tier: 'RO', connectionId: ((st.connections || [])[0] || {}).id || '', databaseId: null, ttl: '30' };

  const rows = st.autoGrants.filter(a => {
    const t = q.trim().toLowerCase(); if (!t) return true;
    // The resolved name is searchable too — it is what the column shows, and a
    // filter reading only the handle finds nothing for a typed first name.
    return (a.user + ' ' + (a.userName || '') + ' ' + a.connectionId + ' ' + (a.databaseId || '') + ' ' + a.tier).toLowerCase().includes(t);
  });
  const keyFn = group === 'subject' ? (a => a.user) : group === 'server' ? (a => a.connectionId) : null;
  const grouped = keyFn ? accGroup(rows, keyFn) : [['', rows]];

  const renderRow = (a) => {
    if (editId === a.id) return (
      <tr key={a.id} className="qh-editrow"><td colSpan={6}><AutoForm init={{ ...a, ttl: 'keep' }} actor={actor} st={st} onDone={() => setEditId(null)} /></td></tr>
    );
    const ex = expiryLabel(a.expiresAt);
    return (
      <tr key={a.id}>
        {/* The name the API resolved, with the id under it only when the two
            differ — the handle is still how this row is correlated with Slack,
            and a team is in neither people table, so `userName` is null there
            and the id IS the name. */}
        <td><b>{a.userName || a.user}</b>{a.userName && a.userName !== a.user && <div className="qh-muted qh-mono" style={{ fontSize: 11.5 }}>{a.user}</div>}</td>
        <td className="qh-mono">{a.connectionId}{autoAllDbs(a.databaseId) ? <span className="qh-muted"> · all databases</span> : '/' + a.databaseId}</td>
        <td><TierBadge tier={a.tier} sm /></td>
        <td><span className={'qh-expiry ' + ex.cls}>{ex.text}</span></td>
        <td className="qh-muted">{a.createdByName || a.createdBy || '—'}</td>
        <td className="qh-tright"><div className="qh-rowacts"><button className="qh-rowbtn" onClick={() => { setEditId(a.id); setAdding(false); }}><AIcon.edit />Edit</button><button className="qh-revoke" onClick={() => st.revokeAutoGrant(a.id, actor)}>Revoke</button></div></td>
      </tr>
    );
  };

  return (
    <div className="qh-apad">
      <div className="qh-aview-head">
        <div><div className="qh-aview-title">Auto-approve grants</div><div className="qh-aview-sub">Skip DBA review for trusted, bounded queries — one target, one tier, and an end date if it should stop.</div></div>
        <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={() => { setAdding(a => !a); setEditId(null); }}><AIcon.plus />New</button>
      </div>
      {adding && <AutoForm init={blank} actor={actor} st={st} onDone={() => setAdding(false)} />}
      <div className="qh-conn-controls">
        <AccSearch q={q} setQ={setQ} placeholder="Filter by person, team, server…" />
        <AccGroupBy group={group} setGroup={setGroup} options={[['none', 'None'], ['subject', 'Person or team'], ['server', 'Server']]} />
      </div>
      <table className="qh-atable">
        {/* "Subject" was the data model's word for what is a person on screen
            (CODE brief 2026-08-20 §2). */}
        <thead><tr><th>Person or team</th><th>Scope</th><th>Tier</th><th>Expiry</th><th>Granted by</th><th></th></tr></thead>
        <tbody>
          {grouped.map(([k, list]) => (
            <React.Fragment key={k || 'all'}>
              {k && <tr className="qh-grouphead"><td colSpan={6}>{k}<span className="qh-grouphead-n">{list.length}</span></td></tr>}
              {list.map(renderRow)}
            </React.Fragment>
          ))}
          {rows.length === 0 && <tr><td colSpan={6} className="qh-conn-empty">No auto-approve grants match your filter.</td></tr>}
        </tbody>
      </table>
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
      <table className="qh-atable">
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
  );
}

// ---------- Teams ----------
function TeamForm({ people, init, onSave, onCancel }) {
  const [name, setName] = useAcc(init.name || '');
  const [desc, setDesc] = useAcc(init.desc || '');
  const [members, setMembers] = useAcc(init.members || []);
  const toggle = (h) => setMembers(m => m.includes(h) ? m.filter(x => x !== h) : [...m, h]);
  const submit = () => { if (!name.trim()) return; onSave({ id: init.id, name: name.trim(), desc: desc.trim(), members }); };
  return (
    <div className="qh-teamform">
      <div className="qh-teamform-top">
        <input className="qh-input qh-input-sm" style={{ width: 200 }} placeholder="Team name (e.g. data-eng)" value={name} onChange={e => setName(e.target.value)} />
        <input className="qh-input qh-input-sm qh-flex1" placeholder="Description (optional)" value={desc} onChange={e => setDesc(e.target.value)} />
      </div>
      <div className="qh-teamform-label">Members · {members.length}</div>
      <div className="qh-memberpick">
        {people.map(p => {
          const on = members.includes(p.handle);
          return <button key={p.handle} type="button" className={'qh-memberchip' + (on ? ' is-on' : '')} onClick={() => toggle(p.handle)}><span className="qh-mini-avatar">{p.initials}</span><span className="qh-memberchip-name">{p.name}</span>{on && <span className="qh-memberchip-ck"><AIcon.check /></span>}</button>;
        })}
      </div>
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
                      {tgts.length === 0 ? <span className="qh-team-none">No targets</span> : tgts.map(g => <span key={g.id} className="qh-acctarget"><span className="qh-acctarget-t">{qhGrantTarget(g)}</span><TierBadge tier={g.tier} sm /></span>)}
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
        <table className="qh-atable">
          <thead><tr><th>Person</th><th>Handle</th><th>Teams</th><th></th></tr></thead>
          <tbody>
            {pRows.map(p => editPerson === p.handle ? (
              <tr key={p.handle} className="qh-editrow"><td colSpan={4}>
                <div className="qh-personedit">
                  <div className="qh-personedit-top"><span className="qh-mini-avatar">{p.initials}</span><b>{p.name}</b><span className="qh-muted">— assign to teams (none, one, or several)</span></div>
                  <div className="qh-memberpick">
                    {teams.map(t => { const on = pdraft.includes(t.id); return <button key={t.id} type="button" className={'qh-memberchip' + (on ? ' is-on' : '')} onClick={() => togglePdraft(t.id)}><span className="qh-team-badge">team</span><span className="qh-memberchip-name">{t.name}</span>{on && <span className="qh-memberchip-ck"><AIcon.check /></span>}</button>; })}
                  </div>
                  <div className="qh-teamform-acts"><button className="qh-btn qh-btn-ghost qh-btn-sm" onClick={() => setEditPerson(null)}>Cancel</button><button className="qh-btn qh-btn-primary qh-btn-sm" onClick={savePerson}>Save membership</button></div>
                </div>
              </td></tr>
            ) : (
              <tr key={p.handle}>
                <td><div className="qh-person-cell"><span className="qh-mini-avatar">{p.initials}</span><b>{p.name}</b></div></td>
                <td className="qh-mono">{p.handle}</td>
                <td><div className="qh-person-teams">{teamsOf(p.handle).length === 0 ? <span className="qh-team-none">No team</span> : teamsOf(p.handle).map(t => <span key={t.id} className="qh-teamtag">{t.name}</span>)}</div></td>
                <td className="qh-tright"><button className="qh-rowbtn" onClick={() => startEditPerson(p)}><AIcon.edit />Edit teams</button></td>
              </tr>
            ))}
          </tbody>
        </table>
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
const QH_CONN_ENGINES = [
  ['postgres', 'PostgreSQL', 5432],
  ['mssql', 'SQL Server', 1433],
];
const QH_CRED_TIERS = [
  ['ro', 'Read-only', 'Used for every SELECT, the schema snapshot and the connection test.'],
  ['rw', 'Read/Write', 'Optional. Without it, write queries on this target are refused.'],
  ['ddl', 'Schema (DDL)', 'Optional. Without it, schema changes on this target are refused.'],
];

// One tier's username + password. The password box is always empty on open,
// never prefilled with a placeholder: the server does not send passwords back,
// so a masked value in here would be a lie about what is stored — and leaving
// it blank is what tells the server "keep the current one".
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
        {QH_CRED_TIERS.map(([t, label, hint]) => (
          <ConnCredRow key={t} label={label} hint={hint}
                       stored={init && init.credentials ? init.credentials[t] : null}
                       value={creds[t]} onChange={v => setCreds({ ...creds, [t]: v })} />
        ))}
        {probe && (
          <div className="qh-fb-meta">
            {probe.ok
              ? ('Connected' + (probe.serverVersion ? ' · server ' + probe.serverVersion : '') + (probe.latencyMs != null ? ' · ' + probe.latencyMs + ' ms' : ''))
              : ('Could not connect — ' + (probe.error || 'unknown error'))}
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
    if (c.enabled && !window.confirm('Disable “' + c.name + '”? Developers lose access to it until it is enabled again. Running queries are unaffected.')) return;
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
              <div className="qh-ercard-by">requested by <b>{er.requester}</b></div>
            </div>
            <div className="qh-ercard-actions">
              <button className="qh-btn qh-btn-danger qh-btn-sm" onClick={() => st.decideEndpoint(er.id, false, act)}>Reject</button>
              <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={() => st.decideEndpoint(er.id, true, act)}>Provision</button>
            </div>
          </div>
        ))}
      </div>

      <div className="qh-section-label">Registered connections · {(st.connections || []).length}</div>
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
        const rows = (st.connections || [])
          .filter(c => envF === 'all' || c.env === envF)
          .filter(c => provF === 'all' || (provF === 'none' ? !qhProvider(c) : qhTags(c).provider === provF))
          .filter(c => { const t = q.trim().toLowerCase(); if (!t) return true; return (c.name + ' ' + c.engine + ' ' + (c.host || '') + ' ' + qhHostingFull(c) + ' ' + (c.databases || []).map(d => d.name).join(' ')).toLowerCase().includes(t); })
          .slice();
        if (sort.key) rows.sort((a, b) => {
          let av, bv;
          if (sort.key === 'dbs') { av = (a.databases || []).length; bv = (b.databases || []).length; }
          else if (sort.key === 'hosting') { av = (qhHosting(a) || 'zzz').toLowerCase(); bv = (qhHosting(b) || 'zzz').toLowerCase(); }
          else { av = String(a[sort.key]).toLowerCase(); bv = String(b[sort.key]).toLowerCase(); }
          return av < bv ? -dir : av > bv ? dir : 0;
        });
        const arrow = (k) => (sort.key === k ? (sort.dir === 'asc' ? ' ↑' : ' ↓') : '');
        const th = (k, label, cls) => <th className={'qh-sort-th' + (sort.key === k ? ' is-sorted' : '') + (cls || '')} onClick={() => toggleSort(k)}>{label}<span className="qh-sort-arw">{arrow(k)}</span></th>;
        return (
          <table className="qh-atable qh-conntable">
            <thead><tr>{th('name', 'Connection')}{th('engine', 'Engine')}{th('hosting', 'Hosting')}{th('enabled', 'Status')}{th('env', 'Environment')}{th('dbs', 'Databases')}<th className="qh-tright">Actions</th></tr></thead>
            <tbody>
              {rows.map(c => {
                const probe = tested[c.id];
                return (
                <tr key={c.id}>
                  <td><div className="qh-conn-namecell"><img className="qh-engine-logo" src={qhEngineLogo(c)} alt="" draggable={false} /><b>{c.name}</b></div>{c.host && <div className="qh-muted qh-mono" style={{ fontSize: 11.5 }}>{c.host}:{c.port}/{c.defaultDatabase}</div>}</td>
                  <td className="qh-muted">{c.engine}</td>
                  {/* Provider + service on one line; the account and any custom
                      tags are on the hover, because this column sits between two
                      identity columns and must not widen the table. */}
                  <td>
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
                    <span className={'qh-expiry' + (c.enabled ? '' : ' is-exp')}>{c.enabled ? 'enabled' : 'disabled'}</span>
                    {credNote(c) && <div className="qh-expiry is-soon">{credNote(c)}</div>}
                  </td>
                  <td><span className={'qh-envtag env-' + c.env}>{c.env}</span></td>
                  <td><div className="qh-conn-dbcell">{(c.databases || []).map(d => <span key={d.id} className="qh-dbchip">{d.name}{d.tier && <TierBadge tier={d.tier} sm />}</span>)}</div></td>
                  <td className="qh-tright"><div className="qh-rowacts">
                    <button className="qh-rowbtn" disabled={testing === c.id} onClick={() => testConnection(c)} title="Open one connection with the stored read-only credential">{testing === c.id ? <span className="qh-spin" /> : (probe ? (probe.ok ? 'Test · ok' : 'Test · failed') : 'Test')}</button>
                    <button className="qh-rowbtn" onClick={() => setForm({ mode: 'edit', conn: c })}><AIcon.edit />Edit</button>
                    <button className="qh-rowbtn" onClick={() => setForm({ mode: 'rotate', conn: c })}>Rotate</button>
                    <button className="qh-rowbtn" onClick={() => toggleEnabled(c)}>{c.enabled ? 'Disable' : 'Enable'}</button>
                    <button className="qh-icon-btn" disabled={refreshing === c.id} onClick={() => refreshSchema(c)} title="Refresh schema — pull this connection's tables & columns now (otherwise an hourly snapshot)" aria-label="Refresh schema">{refreshing === c.id ? <span className="qh-spin" /> : <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12a9 9 0 11-2.6-6.3M21 4v5h-5"/></svg>}</button>
                    <button className="qh-revoke" onClick={() => removeConnection(c)}>Delete</button>
                  </div></td>
                </tr>
                );
              })}
              {rows.length === 0 && <tr><td colSpan={6} className="qh-conn-empty">No connections match your filter.</td></tr>}
            </tbody>
          </table>
        );
      })()}
    </div>
  );
}

Object.assign(window, { GrantsView, AutoView, ScopesView, TeamsView, ConnectionsView });
