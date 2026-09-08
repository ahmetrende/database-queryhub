// QueryHub Admin — Roles (new authorization model, CODE brief 2026-09-07).
//
// What this screen exists for: the model can now express a SCOPED approver —
// "Ceyda approves requests from payments, up to RW, and nothing else". The
// column has been in the database for a while and no screen could set it, so
// nobody used it, and the only way to let somebody approve is still to make them
// a fleet-wide admin. This is the screen that closes that gap.
//
// Two things it has to do that a grants table does not:
//   1. Say what a row MEANS. "Approver · payments · up to RW" is three chips a
//      reader assembles into a sentence; the row writes the sentence instead.
//   2. Show the rows it cannot change. Every current row is MIRRORED from the
//      admins table and the API refuses to revoke it (409). Hiding them would
//      leave a list that disagrees with the access people actually get, so they
//      are shown, marked, and carry their reason without a click.
//
// People / Teams are untouched by this round — the tab sits beside them.
const { useState: useRol } = React;

const RolIcon = {
  plus: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round"><path d="M12 5v14M5 12h14" /></svg>,
  lock: () => <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="4" y="11" width="16" height="10" rx="2" /><path d="M8 11V8a4 4 0 018 0v3" /></svg>,
  check: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5" /></svg>,
};

// The four roles, in the words the screen uses. `what` is the one line the form
// shows under the choice — the difference between granter and approver is not
// guessable from the label alone.
const QH_ROLE_DEFS = {
  approver: { label: 'Approver', what: 'Approve other people’s requests.' },
  granter: { label: 'Granter', what: 'Give other people access.' },
  importer: { label: 'Importer', what: 'Upload CSV data.' },
  admin: { label: 'Admin', what: 'Everything above, everywhere, plus reach every database.' },
};
const QH_ROLE_ORDER = ['approver', 'granter', 'importer', 'admin'];
// Tier is stored as the wire value and read as a word: RO/RW/DDL is the query
// vocabulary, but a ceiling is a sentence about permission, not a badge. Read
// case-insensitively — the field was lowercase on the wire until 2026-09-07, and
// a blank lookup rendered a ceiling that WAS in force as no ceiling at all.
const QH_TIER_WORD = { RO: 'Read', RW: 'Write', DDL: 'Schema' };
const rolTierWord = (t) => (t ? (QH_TIER_WORD[String(t).toUpperCase()] || t) : null);
// A ceiling is only read on admin and approver rows; on a granter or an importer
// the API refuses it outright, so the control is not offered there.
const rolTierApplies = (role) => role === 'approver' || role === 'admin';

function rolDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  const opts = { day: 'numeric', month: 'long' };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
  return d.toLocaleDateString('en-GB', opts);
}
function rolDaysLeft(iso) { return Math.ceil((new Date(iso) - Date.now()) / (1000 * 86400)); }

// The row as a sentence. Scope is read from the explicit booleans — `allTeams`,
// `allTargets`, `anyTier` — and not from a null id: a null alone cannot be told
// apart from a field the server did not fill, and on an authorization screen
// that difference IS the scope (CODE brief 2026-09-07 §3). Each "all" gets a
// WORD rather than a blank, because an omitted clause reads as a narrower row
// than it is. Bold carries what differs between two rows of the same role.
function RoleSentence({ r }) {
  const allTeams = r.allTeams != null ? r.allTeams : !r.scopeTeamId;
  const allTargets = r.allTargets != null ? r.allTargets : !r.scopeTargetId;
  const anyTier = r.anyTier != null ? r.anyTier : !r.maxTier;
  const tier = anyTier ? <b>at any tier</b> : <b>up to {rolTierWord(r.maxTier)}</b>;
  const on = allTargets ? null : (r.scopeTargetName || r.scopeTargetId);
  const team = allTeams ? null : (r.scopeTeamName || r.scopeTeamId);
  let body;
  if (r.role === 'admin') {
    body = <>Everything, everywhere — approves, grants, imports, and reaches <b>every database</b>.</>;
  } else if (r.role === 'approver') {
    body = <>Approves requests from {team ? <b>{team}</b> : <b>everyone</b>}
      {on ? <> on <b>{on}</b></> : null}, {tier}.</>;
  } else if (r.role === 'granter') {
    body = <>Gives access {team ? <>to people in <b>{team}</b> </> : null}
      on {on ? <b>{on}</b> : <b>every connection</b>}.</>;
  } else {
    body = <>Uploads CSV data to {on ? <b>{on}</b> : <b>every connection</b>}
      {team ? <> for <b>{team}</b></> : null}.</>;
  }
  // A temporary role reads as temporary IN the sentence, not as a footnote
  // beside it: the date is the second thing about the row, before the reason.
  const d = r.validUntil ? rolDaysLeft(r.validUntil) : null;
  return (
    <div className="qh-role-sent">{body}
      {r.validUntil && <span className={'qh-role-until' + (d != null && d <= 3 ? ' is-soon' : '')}>
        {d != null && d < 0 ? ' Ended ' + rolDate(r.validUntil) + '.' : ' Ends ' + rolDate(r.validUntil) + '.'}</span>}
    </div>
  );
}

function RoleRow({ r, people, canWrite, enforced, onRevoke }) {
  const mirrored = r.source === 'mirrored';
  const off = r.enabled === false;
  const p = (people || []).find(x => x.handle === r.subject || x.id === r.subject);
  return (
    <div className={'qh-rolerow' + (mirrored ? ' is-mirrored' : '') + (off ? ' is-off' : '')}>
      <span className="qh-peravatar sm">{p ? p.initials : (r.name || r.subject || '?').slice(0, 1).toUpperCase()}</span>
      <div className="qh-rolerow-main">
        <div className="qh-rolerow-top">
          <span className="qh-rolerow-name">{r.name || r.subject}</span>
          <span className={'qh-rolechip is-' + r.role}>{(QH_ROLE_DEFS[r.role] || {}).label || r.role}</span>
          {mirrored && <span className="qh-rolechip is-src">mirrored</span>}
          {/* While the fleet is on the old code path a DIRECT row decides
              nothing, so the row says so rather than leaving the banner to
              carry it — the sentence is per row, and so is the doubt. */}
          {!mirrored && enforced === false && <span className="qh-rolechip is-staged">staged</span>}
          {off && <span className="qh-rolechip is-danger">account disabled</span>}
          <span className="qh-rolerow-h">{r.subject}</span>
        </div>
        <RoleSentence r={r} />
        {/* Disabling somebody REVOKES NOTHING: it stops them submitting and
            leaves every grant and role standing. A disabled super-admin still
            has a row saying they can approve anything, and surfacing that is
            most of why this screen exists (CODE brief 2026-09-07 §5). */}
        {off && <div className="qh-role-why is-danger">Their QueryHub account is disabled — that revokes nothing. This role still stands.</div>}
        {/* Why the row cannot be edited here is part of the row, not a tooltip:
            an admin who has to click to find out will instead try Revoke and
            collect a 409. */}
        {mirrored
          ? <div className="qh-role-why">From the admins table — change it in Admin scopes. QueryHub mirrors it and cannot revoke it here.</div>
          : r.reason ? <div className="qh-role-why">“{r.reason}”</div> : null}
      </div>
      <div className="qh-rolerow-acts">
        {mirrored
          ? <span className="qh-rolelock"><RolIcon.lock />read-only</span>
          : canWrite
            ? <button className="qh-revoke" onClick={() => onRevoke(r)}>Revoke</button>
            : null}
      </div>
    </div>
  );
}

// ---------- The form ----------
// One form for the whole screen: there is no edit endpoint, so a role is created
// or revoked and nothing in between. Scope RETIRES rather than being rejected
// when admin is picked (the API answers 400 for an admin role with any scope) —
// leaving three enabled controls that will be refused is a form that lies.
function RoleForm({ st, onDone }) {
  const conns = (st.connections || []).filter(c => c.enabled !== false);
  const teams = st.teams || [];
  const [f, setF] = useRol({ subject: '', role: 'approver', scopeTeamId: '', scopeTargetId: '', maxTier: '', ttl: 'none', expDate: '', reason: '' });
  const [busy, setBusy] = useRol(false);
  const [err, setErr] = useRol(null);
  const set = (patch) => { setF(x => ({ ...x, ...patch })); setErr(null); };
  const fleetWide = f.role === 'admin';
  const tierOn = rolTierApplies(f.role) && !fleetWide;
  const bad = !f.subject.trim() || expBad(f);

  const preview = {
    subject: f.subject, name: (st.people || []).reduce((acc, p) => (p.handle === f.subject || p.id === f.subject ? p.name : acc), f.subject),
    role: f.role,
    scopeTeamName: fleetWide ? null : (teams.find(t => t.id === f.scopeTeamId) || {}).name || null,
    scopeTargetName: fleetWide ? null : (conns.find(c => c.id === f.scopeTargetId) || {}).name || null,
    maxTier: tierOn ? (f.maxTier || null) : null,
    allTeams: fleetWide || !f.scopeTeamId, allTargets: fleetWide || !f.scopeTargetId,
    anyTier: !tierOn || !f.maxTier,
    validUntil: expIso(f), reason: f.reason, source: 'direct',
  };

  // `write` is separate from `save` so the 409's "revoke and recreate" can run it
  // after the revoke resolves — re-entering `save` would be refused by its own
  // busy guard, which is still true at that point.
  const write = () => st.addRole({
    subject: f.subject.trim(), role: f.role,
    scopeTeamId: fleetWide ? null : (f.scopeTeamId || null),
    scopeTargetId: fleetWide ? null : (f.scopeTargetId || null),
    maxTier: tierOn ? (f.maxTier || null) : null,
    validUntil: expIso(f), reason: f.reason.trim() || null,
  }).then(() => { setBusy(false); onDone(); })
    .catch(e => { setBusy(false); setErr({ msg: (e && e.message) || 'Could not create the role.', code: e && e.code, roleId: e && e.roleId }); });

  const save = () => { if (bad || busy) return; setBusy(true); setErr(null); write(); };
  // A role is immutable, so narrowing one is revoke-then-create. The 409 hands
  // back the row's id, which is the only reason this can be one button: without
  // it the admin would have to find a row the open form is covering.
  const replace = () => {
    if (!err || !err.roleId || busy) return;
    setBusy(true);
    st.removeRole(err.roleId).then(() => { setErr(null); write(); }).catch(() => setBusy(false));
  };

  return (
    <div className="qh-roleform">
      <div className="qh-accedit-label">Who<span className="qh-accedit-hint">A role is one person — teams hold access, not permission to approve.</span></div>
      <PersonPick people={st.people} value={f.subject} onChange={v => set({ subject: v })} resolve={st.resolvePerson} autoFocus />
      {/* 404 is a fact about the person, so it belongs under the picker and
          not in the footer: the id stays typed and the fix is "have them sign
          in", not "try a different value". */}
      {err && err.code === 'no_account' && <div className="qh-roleform-err">{err.msg}</div>}

      <div className="qh-accedit-label" style={{ marginTop: 16 }}>What they may do
        {/* Roles are a UNION: `can_approve` returns true on the first row that
            covers the request, so a second role never narrows the first (CODE
            brief 2026-09-07 §4). Said here because "which role wins" is the
            question every reader of this form brings to it. */}
        <span className="qh-accedit-hint">Roles add up — a person may do the union of what their rows allow; a narrower role never limits a wider one.</span></div>
      <div className="qh-rolepick">
        {QH_ROLE_ORDER.map(k => (
          <button key={k} type="button" className={'qh-roleopt' + (f.role === k ? ' is-on' : '')} onClick={() => set({ role: k, maxTier: rolTierApplies(k) ? f.maxTier : '' })}>
            <span className="qh-roleopt-l">{QH_ROLE_DEFS[k].label}{f.role === k && <RolIcon.check />}</span>
            <span className="qh-roleopt-w">{QH_ROLE_DEFS[k].what}</span>
          </button>
        ))}
      </div>

      <div className="qh-accedit-label" style={{ marginTop: 16 }}>Where it stops
        <span className="qh-accedit-hint">{fleetWide ? 'Admin is fleet-wide — scope does not apply.' : 'Every field left as “all” widens the role.'}</span></div>
      <div className={'qh-rolescope' + (fleetWide ? ' is-off' : '')}>
        <label className="qh-rolefield"><span className="qh-rolefield-l">Team</span>
          <select className="qh-select" disabled={fleetWide} value={f.scopeTeamId} onChange={e => set({ scopeTeamId: e.target.value })}>
            <option value="">Everyone’s requests</option>
            {teams.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select></label>
        <label className="qh-rolefield"><span className="qh-rolefield-l">Connection</span>
          <select className="qh-select" disabled={fleetWide} value={f.scopeTargetId} onChange={e => set({ scopeTargetId: e.target.value })}>
            <option value="">All connections</option>
            {conns.map(c => <option key={c.id} value={c.id}>{connLabel(c)}</option>)}
          </select></label>
        {/* A ceiling is read on admin and approver rows only — the API refuses
            it on the other two rather than storing a value nothing reads, so
            the control is absent there instead of being refused on Save. */}
        {tierOn && <label className="qh-rolefield"><span className="qh-rolefield-l">Tier ceiling</span>
          <select className="qh-select" value={f.maxTier} onChange={e => set({ maxTier: e.target.value })}>
            <option value="">No ceiling</option>
            <option value="RO">Read (RO)</option><option value="RW">Write (RW)</option><option value="DDL">Schema (DDL)</option>
          </select></label>}
        <label className="qh-rolefield"><span className="qh-rolefield-l">Ends</span>
          {/* The same expiry control every grant uses (2026-08-16): a role that
              ends is the same kind of thing as access that ends, and a second
              date widget would be a second set of rules for one field. */}
          <ExpiryPick f={f} onChange={p => set(p)} /></label>
      </div>
      {err && err.code === 'admin_scope' && <div className="qh-roleform-err">{err.msg}</div>}
      {err && err.code === 'tier_scope' && <div className="qh-roleform-err">{err.msg}</div>}
      {err && err.code === 'no_team' && <div className="qh-roleform-err">{err.msg}</div>}

      <div className="qh-accedit-label" style={{ marginTop: 16 }}>Why<span className="qh-accedit-hint">Optional. Shown on the row — it is the only record of why this role exists.</span></div>
      <input className="qh-input" style={{ maxWidth: 460 }} placeholder="e.g. team lead, covering for Aylin until October" value={f.reason} onChange={e => set({ reason: e.target.value })} />

      {/* What Save writes, in the words the row will use — so the sentence is
          checked before it exists, not after. */}
      <div className="qh-roleprev">
        <div className="qh-roleprev-h">This writes</div>
        <div className="qh-rolerow is-preview">
          <span className="qh-peravatar sm">{((st.people || []).find(p => p.handle === f.subject) || {}).initials || '?'}</span>
          <div className="qh-rolerow-main">
            <div className="qh-rolerow-top"><span className="qh-rolerow-name">{preview.name || 'Nobody picked yet'}</span>
              <span className={'qh-rolechip is-' + f.role}>{QH_ROLE_DEFS[f.role].label}</span></div>
            <RoleSentence r={preview} />
          </div>
        </div>
      </div>

      {err && ['no_account', 'admin_scope', 'tier_scope', 'no_team'].indexOf(err.code) < 0 && (
        <div className="qh-roleform-err">{err.msg}
          {err.roleId && <div className="qh-roleform-act">
            <button className="qh-btn qh-btn-danger qh-btn-sm" disabled={busy} onClick={replace}>Revoke role {err.roleId} and create this one</button>
          </div>}
        </div>
      )}
      <div className="qh-accedit-acts">
        <button className="qh-btn qh-btn-sm" onClick={onDone}>Cancel</button>
        <button className="qh-btn qh-btn-primary qh-btn-sm" disabled={bad || busy} onClick={save}>{busy ? 'Saving…' : 'Create role'}</button>
      </div>
      {expBad(f) && <div className="qh-roleform-err">Pick an end date in the future — a role that is already over reads as a role that works.</div>}
    </div>
  );
}

// ---------- Empty state ----------
// Nobody has seen a scoped approver, so the empty state teaches one instead of
// saying "no roles yet": the example is rendered as a REAL row in the real row
// styling, because the thing being explained is what the list will look like.
function RolesEmpty({ canWrite, onStart, teams, enforced }) {
  // The example carries the SAME explicit flags the wire does — a scoped row is
  // the entire point of this block, and reading scope from the booleans (§3)
  // means a locally-built row without them renders as fleet-wide.
  const eg = { subject: 'clara.alvarez', name: 'Clara Alvarez', role: 'approver',
    scopeTeamId: 't_payments', scopeTeamName: (teams && teams[2] ? teams[2].name : 'payments'),
    scopeTargetId: 'prod-main', scopeTargetName: 'prod-main',
    allTeams: false, allTargets: false, anyTier: false,
    maxTier: 'RW', validUntil: null, reason: 'team lead', source: 'direct' };
  return (
    <div className="qh-roleempty">
      <div className="qh-roleempty-h">No roles have been created here yet</div>
      <p className="qh-roleempty-p">Letting somebody approve has meant making them an admin — fleet-wide, every server, every tier. A role is narrower than that: <b>one person</b>, <b>one thing they may do</b>, and <b>where it stops</b>.
        {/* While the fleet is on the old code path, the staging fact belongs in
            THIS block rather than in a banner above it: with no direct rows yet
            there is nothing for a second message to be about, and two notices
            over an empty list read as a broken screen (answer to CODE's (a)). */}
        {enforced === false && <> Rows created here are <b>recorded but not yet in force</b> — the fleet still reads the admins table, so the mirrored rows below are the live ones.</>}</p>
      <div className="qh-roleempty-eg">
        <div className="qh-roleprev-h">For example</div>
        <RoleRow r={eg} people={[{ handle: 'clara.alvarez', initials: 'CA' }]} canWrite={false} onRevoke={() => {}} />
      </div>
      <dl className="qh-roledefs">
        <div><dt>Team</dt><dd>Whose requests they may approve. Left empty, everyone’s.</dd></div>
        <div><dt>Connection</dt><dd>Which server. Left empty, all of them.</dd></div>
        <div><dt>Tier ceiling</dt><dd>The highest of Read / Write / Schema they may approve. Left empty, no ceiling. Only approvers and admins have one — nothing reads a ceiling on a granter.</dd></div>
        {/* The one place roles are not a plain union, and the reason is worth
            stating: an admin could grant themselves the access anyway, a scoped
            approver waving through their own query removes the review. */}
        <div><dt>Self-approval</dt><dd>An admin may approve their own request; a scoped approver may not. Holding both takes the admin exemption.</dd></div>
      </dl>
      {canWrite
        ? <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={onStart}><RolIcon.plus />Create the first role</button>
        : <div className="qh-role-why">Creating a role needs a super-admin.</div>}
    </div>
  );
}

// ---------- The view ----------
function RolesView({ st, user, canWrite }) {
  const [q, setQ] = useRol('');
  const [roleF, setRoleF] = useRol('all');
  const [group, setGroup] = useRol('none');
  const [adding, setAdding] = useRol(false);
  const roles = st.roles || [];
  const enforced = st.rolesEnforced !== false;
  const direct = roles.filter(r => r.source !== 'mirrored');
  const mirrored = roles.filter(r => r.source === 'mirrored');

  const match = (r) => {
    const t = q.trim().toLowerCase();
    if (!t) return true;
    return [r.name, r.subject, r.role, r.scopeTeamName, r.scopeTargetName, r.maxTier, r.reason]
      .filter(Boolean).join(' ').toLowerCase().includes(t);
  };
  const rows = direct.filter(r => (roleF === 'all' || r.role === roleF)).filter(match);
  const mRows = mirrored.filter(r => (roleF === 'all' || r.role === roleF)).filter(match);

  const revoke = (r) => {
    if (!window.confirm('Revoke ' + (r.name || r.subject) + '’s ' + (QH_ROLE_DEFS[r.role] || {}).label + ' role? They keep their own database access — only what they may do to other people’s changes.')) return;
    st.removeRole(r.id);
  };

  // "Who can approve my team's requests?" is the first question the list has to
  // answer, and it is a grouping question: by team, an approver whose scope is
  // NULL belongs under every team, so the all-teams rows get their own group
  // stated as such rather than being repeated under each one.
  const groups = (() => {
    if (group !== 'team') return [['', rows]];
    const m = new Map();
    rows.forEach(r => {
      const k = r.scopeTeamName || (r.role === 'admin' ? 'Every team (admin)' : 'Every team');
      if (!m.has(k)) m.set(k, []);
      m.get(k).push(r);
    });
    return [...m.entries()].sort((a, b) => (a[0] > b[0] ? 1 : -1));
  })();

  return (
    <div className="qh-apad">
      <div className="qh-aview-head">
        <div><div className="qh-aview-title">Roles</div>
          <div className="qh-aview-sub">Who may approve, grant or import — and how far. A role is a person, what they may do, and where it stops.</div></div>
        {canWrite && !adding && direct.length > 0 && <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={() => setAdding(true)}><RolIcon.plus />Add a role</button>}
      </div>

      {/* The staging banner sits above the LIST, not above the empty state:
          while there are no direct rows the empty block says it instead (CODE's
          question (a) — one message, not two). It names the exception, because
          "not in force" is false of the mirrored rows and they are on screen. */}
      {!enforced && direct.length > 0 && <div className="qh-rolenote is-warn">
        <RolIcon.lock />Roles are recorded but not yet in force — the fleet still reads the admins table. Rows marked <b>mirrored</b> are live; rows marked <b>staged</b> decide nothing yet.</div>}

      {/* A non-super admin reads this screen (GET needs any admin) but cannot
          write it. Controls that would 403 are absent, and the strip says why —
          a disabled button with no explanation reads as a broken screen. */}
      {!canWrite && <div className="qh-rolenote"><RolIcon.lock />You can see who holds what. Creating or revoking a role needs a super-admin.</div>}

      {adding && <RoleForm st={st} onDone={() => setAdding(false)} />}

      {direct.length === 0 && !adding && <RolesEmpty canWrite={canWrite} onStart={() => setAdding(true)} teams={st.teams} enforced={enforced} />}

      {direct.length > 0 && (
        <>
          <div className="qh-conn-controls">
            <div className="qh-search sm">
              <svg className="qh-search-ic" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="7" /><path d="M21 21l-4.3-4.3" /></svg>
              <input className="qh-search-in" placeholder="Filter by person, team, connection…" value={q} onChange={e => setQ(e.target.value)} />
              {q && <button className="qh-search-x" onMouseDown={e => { e.preventDefault(); setQ(''); }} aria-label="Clear"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18" /></svg></button>}
            </div>
            <div className="qh-seg qh-seg-sm">
              {[['all', 'All'], ...QH_ROLE_ORDER.map(k => [k, QH_ROLE_DEFS[k].label + 's'])].map(([v, l]) => (
                <button key={v} className={'qh-seg-opt' + (roleF === v ? ' is-active' : '')} onClick={() => setRoleF(v)}>{l}</button>
              ))}
            </div>
            <AccGroupBy label="Group" group={group} setGroup={setGroup} options={[['none', 'Flat'], ['team', 'By team']]} />
          </div>
          {groups.map(([k, list]) => (
            <div key={k || 'flat'}>
              {k && <div className="qh-section-label">{k} · {list.length}</div>}
              <div className="qh-rolelist">{list.map(r => <RoleRow key={r.id} r={r} people={st.people} canWrite={canWrite} enforced={enforced} onRevoke={revoke} />)}</div>
            </div>
          ))}
          {rows.length === 0 && <div className="qh-conn-empty">No roles match your filter.</div>}
        </>
      )}

      {mRows.length > 0 && (
        <>
          <div className="qh-section-label">Mirrored from the admins table · {mRows.length}</div>
          <div className="qh-aview-sub" style={{ marginBottom: 10 }}>{enforced ? 'Shown because this is what the system enforces.' : 'These are the rows in force today.'} They are changed in {canWrite ? <a href="#admin/scopes">Admin scopes</a> : 'Admin scopes'}, not here.</div>
          <div className="qh-rolelist">{mRows.map(r => <RoleRow key={r.id} r={r} people={st.people} canWrite={canWrite} enforced={enforced} onRevoke={revoke} />)}</div>
        </>
      )}
    </div>
  );
}

Object.assign(window, { RolesView, RoleSentence, QH_ROLE_DEFS });
