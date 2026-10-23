// QueryHub Admin — panel shell (nav) + Approvals + Kill switch.
// The separate DDL escalations screen was removed 2026-09-22 (§8): DDL requests
// sit in the approval queue, flagged, carrying everything that screen carried.
const { useState: useAdm } = React;

const AdminIcons = {
  approvals: () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/></svg>,
  ddl: () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2l9 4.5v5c0 5-3.8 8.5-9 10-5.2-1.5-9-5-9-10v-5L12 2z"/><path d="M12 8v4M12 16h.01"/></svg>,
  grants: () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M16 21v-2a4 4 0 00-4-4H6a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 11l-3 3-2-2"/></svg>,
  auto: () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M13 2L3 14h7l-1 8 10-12h-7z"/></svg>,
  scopes: () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>,
  teams: () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75"/></svg>,
  conns: () =><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"><ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6"/></svg>,
  roles: () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="9" cy="8" r="3.4"/><path d="M3 20v-1.5A4.5 4.5 0 017.5 14h3"/><path d="M17.5 12.5l1.4 2.9 3.1.4-2.3 2.2.6 3.1-2.8-1.5-2.8 1.5.6-3.1-2.3-2.2 3.1-.4z"/></svg>,
  mask: () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M1.5 12S5 5.5 12 5.5c2 0 3.7.5 5.1 1.3M22.5 12S19 18.5 12 18.5c-2 0-3.7-.5-5.1-1.3"/><path d="M9.9 9.9a3 3 0 104.2 4.2"/><path d="M3 3l18 18"/></svg>,
  audit: () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><path d="M14 2v6h6M8 13h8M8 17h5"/></svg>,
  metrics: () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M3 3v18h18"/><path d="M7 14l3-3 3 3 5-6"/></svg>,
  feedback: () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg>,
  kill: () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M18.4 6.6a9 9 0 11-12.8 0M12 2v10"/></svg>,
  effective: () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/><path d="M8.5 11l1.8 1.8L14 9"/></svg>,
  config: () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M4 6h10M18 6h2M4 12h2M10 12h10M4 18h7M15 18h5"/><circle cx="16" cy="6" r="2"/><circle cx="8" cy="12" r="2"/><circle cx="13" cy="18" r="2"/></svg>,
};

const QH_ADMIN_SECTIONS = ['approvals', 'kill', 'effective', 'grants', 'auto', 'scopes', 'teams', 'roles', 'mask', 'conns', 'audit', 'metrics', 'feedback', 'config'];
// Deep-link support: the admin section lives in the URL as #admin/<section>.
// Which door a request came through. Two values existed when this was written
// and the chip coerced everything to them — `web`, or else Slack — so a third
// surface would have been labelled Slack on the one field whose whole job is to
// say where the request came from. The value is rendered as itself now; an
// unstyled chip for an unfamiliar origin is the honest answer, and the base
// `.qh-origin-chip` rule already covers it.
const QH_ORIGIN_WHERE = { web: 'the web app', slack: 'Slack', idp: 'the IdP portal' };
function qhOriginKey(o) { return String(o || 'slack').toLowerCase(); }
function qhOriginWhere(o) {
  const k = qhOriginKey(o);
  return QH_ORIGIN_WHERE[k] || k;
}

// The admin level a section needs (2026-10-23, PLA-1592 U4a and U13). 'dba' is
// the DBA level of GET /me `admin.isDba`: a super-admin, or an admin role with
// no end date on every connection. 'super' is a super-admin only. A section not
// listed opens for every admin. The server enforces the same levels. The nav
// only stops a click that the server would refuse.
const QH_ADMIN_NEED = { effective: 'dba', conns: 'dba', grants: 'super', auto: 'super', scopes: 'super', teams: 'super', mask: 'super', config: 'super' };
// 'super' | 'dba' | 'admin'. `isDba` is false when /me does not send it, so an
// older server gives a DBA the same nav as before, never a wider one.
function qhAdminLevel(user) {
  if (user && user.role === 'super') return 'super';
  return user && user.isDba === true ? 'dba' : 'admin';
}
function qhAdminCanOpen(id, user) {
  const need = QH_ADMIN_NEED[id], lv = qhAdminLevel(user);
  if (!need) return true;
  return need === 'super' ? lv === 'super' : lv === 'super' || lv === 'dba';
}
function qhAdminLockReason(id) {
  return QH_ADMIN_NEED[id] === 'dba' ? 'DBA or super-admin only' : 'Super-admin only';
}
const QH_LOCK_ICON = <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 018 0v3"/></svg>;

function navFromAdminHash() {
  const m = (typeof location !== 'undefined' ? (location.hash || '') : '').match(/^#admin\/([a-z]+)/i);
  // A bookmark to the retired DDL screen lands on the queue, where DDL now is.
  if (m && m[1].toLowerCase() === 'ddl') return 'approvals';
  return m && QH_ADMIN_SECTIONS.indexOf(m[1].toLowerCase()) !== -1 ? m[1].toLowerCase() : null;
}

// Opens in the caller's REAL role (CODE 2026-09-23 (j) §1): the "Viewing as"
// DBA / Super-admin switch was never used, and the server enforces the scope
// whatever it said. `user.role` from GET /me is 'super' or 'dba' here.
function AdminPanel({ st, user, modeEntry }) {
  const [nav, setNav] = useAdm(() => navFromAdminHash() || 'approvals');
  // A hand-off waiting on a DBA is the most stuck work in the system — approved,
  // and nothing will move until someone runs it — so it counts on the badge.
  const pendCount = st.queue.length + (st.manualRuns || []).length;
  const erCount = st.endpointReqs.filter(e => e.status === 'submitted').length;

  const groups = [
    { label: 'Review', items: [
      ['approvals', 'Approval queue', AdminIcons.approvals, pendCount],
      ['kill', 'Kill switch', AdminIcons.kill, null],
    ]},
    { label: 'Access', items: [
      // First in the group: "what can this person reach" is the question the
      // other Access screens are each a third of the answer to.
      ['effective', 'Effective access', AdminIcons.effective, null],
      ['grants', 'Grants', AdminIcons.grants, null],
      ['auto', 'Auto-approve', AdminIcons.auto, (st.autoRequests || []).length || null],
      ['scopes', 'Admin scopes', AdminIcons.scopes, null],
      ['teams', 'Teams', AdminIcons.teams, null],
      // Any admin may READ Roles: GET /admin/roles needs any admin, the writes
      // need a super-admin. An admin who cannot find out who approves their
      // team's requests asks in Slack instead.
      ['roles', 'Roles', AdminIcons.roles, null],
      // Super-admin only: this is the one screen whose whole purpose is to
      // reduce protection.
      ['mask', 'Masking exemptions', AdminIcons.mask, null],
      ['conns', 'Connections', AdminIcons.conns, erCount],
    ]},
    { label: 'Insights', items: [
      ['audit', 'Audit log', AdminIcons.audit, null],
      ['metrics', 'Metrics', AdminIcons.metrics, null],
      ['feedback', 'Feedback', AdminIcons.feedback, null],
    ]},
    { label: 'System', items: [
      ['config', 'System configuration', AdminIcons.config, null],
    ]},
  ];

  const isSuper = qhAdminLevel(user) === 'super';
  // A DBA opens the kill switch, Effective access and Connections (read-only).
  // A section this admin cannot open is a LOCKED row, not a missing one
  // (2026-10-23, design takeaway 3), so they know it exists and who opens it.
  const visibleNav = (id) => qhAdminCanOpen(id, user);
  const curNav = visibleNav(nav) ? nav : 'approvals';

  // Keep the URL in sync so an admin section is linkable/bookmarkable
  // (e.g. #admin/audit). Client-only — no backend involved.
  //
  // PUSH on a move, REPLACE only on the first pass. This used replaceState
  // unconditionally, which meant walking the admin panel created no history
  // at all: Back skipped every section and left QueryHub entirely, landing
  // on the sign-in page. The first pass is different — it is a NORMALISATION
  // (arriving with no hash, or asking for a section this admin may not open),
  // and an entry there would make Back a no-op that looks broken.
  //
  // Back itself is safe from re-pushing: it changes the hash, `onHash` below
  // sets the state, and this effect then finds the hash already correct.
  const navFirst = React.useRef(true);
  React.useEffect(() => {
    const want = '#admin/' + curNav;
    if (location.hash !== want) {
      // A child's effects run BEFORE its parent's, so on the crossing from the
      // editor this runs ahead of App's '#admin' push and the hash is still the
      // editor's. Replacing it then overwrote the editor's entry, and Back
      // stayed in the panel. First pass replaces only an '#admin…' hash (a deep
      // link, or App's push already made); from anywhere else it pushes.
      if (navFirst.current && (location.hash || '').indexOf('#admin') === 0) history.replaceState(null, '', want);
      else history.pushState(null, '', want);
    }
    navFirst.current = false;
  }, [curNav]);
  React.useEffect(() => {
    const onHash = () => { const n = navFromAdminHash(); if (n) setNav(n); };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  return (
    <div className="qh-admin">
      <nav className="qh-anav">
        {modeEntry}
        {groups.map(g => {
          const navBtn = ([id, label, Icon, count]) => {
            if (!visibleNav(id)) {
              // aria-disabled, not disabled: the row stays in the tab order and
              // keeps its title, so a keyboard or screen-reader user learns the
              // reason too. The click does nothing.
              const why = qhAdminLockReason(id);
              return (
                <button key={id} type="button" className="qh-anav-item is-locked" aria-disabled="true" title={why}
                        aria-label={label + '. ' + why + '.'} onClick={(e) => e.preventDefault()}>
                  <Icon /><span className="qh-anav-text">{label}</span>
                  <span className="qh-lock" aria-hidden="true">{QH_LOCK_ICON}</span>
                </button>
              );
            }
            return (
              <button key={id} className={'qh-anav-item' + (curNav === id ? ' is-active' : '')} onClick={() => setNav(id)}>
                <Icon /><span className="qh-anav-text">{label}</span>
                {count != null && count > 0 && <span className="qh-anav-badge">{count}</span>}
              </button>
            );
          };
          return (
            <div key={g.label} className="qh-anav-group">
              <div className="qh-anav-glabel">{g.label}</div>
              {g.items.map(navBtn)}
            </div>
          );
        })}
      </nav>

      <div className="qh-aview">
        {st.loadError && (
          <div className="qh-load-error" role="alert">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L14.7 3.9a2 2 0 00-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg>
            <span>Couldn't load admin data. Check your connection and try again.</span>
            <button className="qh-btn qh-btn-primary qh-btn-sm" disabled={st.loading} onClick={() => st.reload && st.reload()}>{st.loading ? 'Retrying…' : 'Retry'}</button>
          </div>
        )}
        {curNav === 'approvals' && <ApprovalsView st={st} user={user} role={isSuper ? 'super' : 'dba'} />}
        {curNav === 'kill' && <KillView st={st} user={user} canWrite={qhAdminLevel(user) !== 'admin'} />}
        {curNav === 'effective' && <EffectiveAccessView st={st} user={user} />}
        {curNav === 'grants' && <GrantsView st={st} user={user} />}
        {curNav === 'auto' && <AutoView st={st} user={user} />}
        {curNav === 'scopes' && <ScopesView st={st} user={user} />}
        {curNav === 'teams' && <TeamsView st={st} user={user} />}
        {curNav === 'roles' && <RolesView st={st} user={user} canWrite={isSuper} />}
        {curNav === 'mask' && <MaskingView st={st} user={user} />}
        {curNav === 'conns' && <ConnectionsView st={st} user={user} readOnly={!isSuper} />}
        {curNav === 'audit' && <AuditView st={st} />}
        {curNav === 'metrics' && <MetricsView st={st} />}
        {curNav === 'feedback' && <FeedbackView st={st} />}
        {curNav === 'config' && <SystemConfigView st={st} user={user} />}
      </div>
    </div>
  );
}

// ---------- Approvals ----------
// The checkbox is a SIBLING of the select control, not a child of it: a
// <label>/<input> nested inside a <button> is invalid HTML with undefined
// assistive-tech behaviour, and it made the card's accessible name the entire
// concatenated submitter + tier + SQL body. Now the control announces who
// submitted what, at which tier, against which target, and the SQL is read as
// content instead of as part of the name.
const QCHK = <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5"/></svg>;
function QueueCard({ it, selected, onSelect, checked, onCheck }) {
  const who = qhPersonName(it.submitter.name) + ', ' + it.tier + ' on '
    + it.connectionId + '/' + it.databaseId + ', ' + qhAgo(it.submittedAt);
  // DDL is the most dangerous tier and must not be mistakable for RO at a
  // glance: its own card treatment, a flag that leads the row rather than a
  // badge among badges, and the elevation's end date on the card itself.
  const ddl = !!it.escalate || it.tier === 'DDL';
  const el = it.elevation;
  return (
    <div className={'qh-qcard' + (selected ? ' is-active' : '') + (ddl ? ' is-ddl' : '')}>
      <label className="qh-qcheck">
        <input type="checkbox" checked={checked} onChange={() => onCheck(it.id)}
               aria-label={'Select request from ' + who} />
        <span className="qh-qcheck-box">{QCHK}</span>
      </label>
      <button type="button" className="qh-qcard-main" onClick={() => onSelect(it.id)}
              aria-label={'Review request from ' + who} aria-pressed={!!selected}>
        {ddl && <div className="qh-qddl-flag"><AdminIcons.ddl />Schema change · DDL{el && el.source === 'admin' ? <span className="qh-qddl-until">as a fleet admin</span> : el && el.expiresAt ? <span className="qh-qddl-until">elevation ends {qhFmt(el.expiresAt)}</span> : el ? <span className="qh-qddl-until">elevation has no end date</span> : null}</div>}
        <div className="qh-qcard-top">
          <span className="qh-qavatar">{it.submitter.initials}</span>
          <span className="qh-qname" title={qhIsHandleName(it.submitter.name) ? it.submitter.name : null}>{qhPersonName(it.submitter.name)}</span>
          <TierBadge tier={it.tier} sm />
          <span className={'qh-origin-chip o-' + qhOriginKey(it.origin)} title={'Submitted via ' + qhOriginWhere(it.origin)}>{qhOriginKey(it.origin)}</span>
          {/* Inside a batch the chip says WHICH one — `bundlePosition` /
              `bundleSize` (GET /admin/queue, 2026-08-20). "bundle" alone told
              an approver a batch existed and nothing about where this row sat
              in it, which for a migration is the only thing that matters. */}
          {it.bundleId && <span className="qh-bundle-chip" title={'Bundle ' + it.bundleId}>{it.bundlePosition ? it.bundlePosition + ' of ' + (it.bundleSize || '?') : 'bundle'}</span>}
          {it.piiCols.length > 0 && <span className="qh-qpii" title={it.piiCols.join(', ')}>PII</span>}
        </div>
        <div className="qh-qcard-target">{it.connectionId}/{it.databaseId}</div>
        <div className="qh-qcard-sql">{it.sql.replace(/\n/g, ' ')}</div>
        <div className="qh-qcard-when">{qhAgo(it.submittedAt)}</div>
      </button>
    </div>
  );
}

// ---------- Waiting for a DBA to run by hand (CODE 2026-09-23 §5) ----------
// DDL the bot handed over because it may not run it itself — a role without
// the attribute, "must be owner". It is past deciding (it was approved); what
// is missing is a person with the right role at a prompt. So it sits ABOVE the
// queue, oldest first, and each card is the next step: copy it, run it, say how
// it went. Marking it failed needs a reason — the server refuses one without.
function ManualRunsBlock({ st }) {
  const items = st.manualRuns || [];
  const [failing, setFailing] = useAdm(null);   // id whose failure reason is being typed
  const [why, setWhy] = useAdm('');
  const [busy, setBusy] = useAdm(null);
  const [err, setErr] = useAdm(null);           // { id, msg }
  if (!items.length) return null;
  const close = (it, completed) => {
    if (busy) return;
    setBusy(it.id); setErr(null);
    st.closeManualRun(it.id, completed, completed ? null : why.trim())
      .then(() => { setBusy(null); setFailing(null); setWhy(''); })
      .catch(e => { setBusy(null); setErr({ id: it.id, msg: (e && e.message) || 'Not saved.' }); });
  };
  const copy = (it) => {
    const said = (ok) => st.pushToast && st.pushToast(ok ? 'SQL copied.' : 'Could not copy — select the SQL and copy it by hand.');
    try { navigator.clipboard.writeText(it.sql || '').then(() => said(true), () => said(false)); } catch (e) { said(false); }
  };
  return (
    <div className="qh-mruns">
      <div className="qh-mruns-h">Waiting for a DBA to run by hand · {items.length}</div>
      {items.map(it => {
        const who = qhPersonName((it.requester || {}).name) || (it.requester || {}).slackId || 'The requester';
        // The stored text has no password in it (§5) — say so before the DBA
        // pastes a placeholder into a prompt as if it were one.
        const hidden = qhRedactedAt(it.sql || '').length > 0;
        const typing = failing === it.id;
        return (
          <div key={it.id} className="qh-mrun">
            <div className="qh-mrun-top">
              <span className="qh-mrun-who">{who}</span>
              <TierBadge tier={it.tier} sm />
              <span className="qh-mrun-target">{it.connectionId}/{it.databaseId}</span>
              <span className="qh-mrun-when" title={it.escalatedAt || ''}>{qhAgo(it.escalatedAt || it.createdAt)}</span>
            </div>
            <pre className="qh-mrun-sql" dangerouslySetInnerHTML={{ __html: qhHighlight(it.sql || '') }} />
            {it.refusal && <div className="qh-mrun-line">The database refused the bot: <span className="qh-mono">{it.refusal}</span></div>}
            {hidden && <div className="qh-mrun-line is-warn">QueryHub hid the password when it stored this. Agree it with {who.split(' ')[0]} before you run it.</div>}
            {it.reason && <div className="qh-mrun-reason">“{it.reason}”</div>}
            {typing && <input className="qh-input qh-input-sm qh-mrun-why" autoFocus placeholder="Why it could not be run (required)" value={why}
              onChange={e => { setWhy(e.target.value); setErr(null); }}
              onKeyDown={e => { if (e.key === 'Enter' && why.trim()) close(it, false); if (e.key === 'Escape') setFailing(null); }} />}
            {err && err.id === it.id && <div className="qh-roleform-err">{err.msg}</div>}
            <div className="qh-mrun-acts">
              <button className="qh-btn qh-btn-ghost qh-btn-sm" onClick={() => copy(it)}>Copy SQL</button>
              <div className="qh-flex1" />
              {typing
                ? <><button className="qh-btn qh-btn-ghost qh-btn-sm" onClick={() => setFailing(null)}>Cancel</button>
                    <button className="qh-btn qh-btn-danger qh-btn-sm" disabled={!why.trim() || busy === it.id} onClick={() => close(it, false)}>Mark failed</button></>
                : <><button className="qh-btn qh-btn-ghost qh-btn-sm" disabled={busy === it.id} onClick={() => { setFailing(it.id); setWhy(''); setErr(null); }}>Mark failed</button>
                    <button className="qh-btn qh-btn-primary qh-btn-sm" disabled={busy === it.id} onClick={() => close(it, true)}>Mark done</button></>}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function ApprovalsView({ st, user, role }) {
  // Decisions are optimistic: st.decide() reloads the queue, but that round
  // trip lands AFTER this render. Without hiding the decided row the detail
  // pane kept showing it (see `cur` below) with live buttons, so a second
  // click hit the server again and came back "already decided".
  const [decided, setDecided] = useAdm([]);
  const items = (st.queue || []).filter(x => decided.indexOf(x.id) === -1);
  React.useEffect(() => {   // queue reloaded — the optimistic list can go
    if (decided.length) setDecided([]);
  }, [st.queue]);
  // The DDL screen's one job — "show me only schema changes" — is a filter
  // here now. Counted, so the number the old nav badge carried is not lost.
  const [onlyDdl, setOnlyDdl] = useAdm(false);
  const isDdl = (x) => !!x.escalate || x.tier === 'DDL';
  const ddlCount = items.filter(isDdl).length;
  const shown = onlyDdl ? items.filter(isDdl) : items;
  const [sel, setSel] = useAdm(items[0] ? items[0].id : null);
  const [checked, setChecked] = useAdm([]);
  const [note, setNote] = useAdm('');
  const [noteMode, setNoteMode] = useAdm(null); // 'reject' | 'changes'

  // `sel === null` means "nothing left to review" — it must NOT fall back to
  // the first row, or deciding the last item re-selects the one just decided.
  const cur = sel == null ? null : (items.find(x => x.id === sel) || items[0]);
  const toggleCheck = (id) => setChecked(c => c.includes(id) ? c.filter(x => x !== id) : [...c, id]);
  const actor = user ? ('dba.' + user.name.split(' ')[0].toLowerCase()) : 'dba';

  // Select all / none. Tri-state, because "some selected" and "all selected"
  // must not look identical when the next click empties the selection.
  const allIds = items.map(x => x.id);
  const allChecked = allIds.length > 0 && checked.length >= allIds.length;
  const someChecked = checked.length > 0 && !allChecked;
  const setMany = (ids, on) => setChecked(c => on ? [...new Set(c.concat(ids))] : c.filter(x => ids.indexOf(x) < 0));

  // A batch arrives as N rows interleaved with everyone else's. `bundleId` +
  // `bundlePosition` + `bundleSize` are enough to put it back together and
  // order it AS WRITTEN — by position, not by arrival, because "item 3 of 5" of
  // a migration means nothing out of sequence. The group takes the place of the
  // first of its rows, so nothing jumps to the top of the queue.
  const groups = React.useMemo(() => {
    const out = [], seen = {};
    shown.forEach(it => {
      if (!it.bundleId) { out.push({ solo: it }); return; }
      if (seen[it.bundleId]) return;
      seen[it.bundleId] = 1;
      const rows = shown.filter(x => x.bundleId === it.bundleId)
        .slice().sort((a, b) => (a.bundlePosition || 0) - (b.bundlePosition || 0));
      out.push({ bundleId: it.bundleId, rows, size: it.bundleSize || rows.length });
    });
    return out;
  }, [shown]);

  // Reject and Request changes need a note of QH_NOTE_MIN characters. The first
  // click opens the note. The second click sends it. Until the note is long
  // enough, that button is disabled and its title says why.
  const noteChk = qhNoteCheck(note);
  // A note belongs to one request. A different card closes it, so a note
  // written for one submitter never goes to the next.
  const curId = cur ? cur.id : null;
  React.useEffect(() => { setNoteMode(null); setNote(''); }, [curId]);
  
  const act = (decision) => {
    if ((decision === 'reject' || decision === 'changes') && noteMode !== decision) { setNoteMode(decision); return; }
    st.decide(cur.id, decision, actor, note.trim() || undefined);
    setNote(''); setNoteMode(null);
    const rest = items.filter(x => x.id !== cur.id);
    setDecided(d => d.concat([cur.id]));   // hide it now, not when the reload lands
    setSel(rest[0] ? rest[0].id : null);
  };

  return (
    <div className="qh-aqueue">
      <div className="qh-aqueue-list">
        <div className="qh-aview-head">
          <div>
            <div className="qh-aview-title">Approval queue</div>
            <div className="qh-aview-sub">{items.length} pending · you approve as {role === 'super' ? 'super-admin' : 'DBA'}</div>
          </div>
        </div>
        {ddlCount > 0 && (
          <div className="qh-qfilter">
            <div className="qh-seg qh-seg-sm">
              <button className={'qh-seg-opt' + (!onlyDdl ? ' is-active' : '')} onClick={() => setOnlyDdl(false)}>All · {items.length}</button>
              <button className={'qh-seg-opt qh-seg-ddl' + (onlyDdl ? ' is-active' : '')} onClick={() => setOnlyDdl(true)}><AdminIcons.ddl />DDL · {ddlCount}</button>
            </div>
          </div>
        )}
        {items.length > 0 && (
          <div className={'qh-batchbar' + (checked.length ? ' is-armed' : '')}>
            <label className="qh-qcheck qh-batchbar-all">
              <input type="checkbox" checked={allChecked} onChange={() => setChecked(allChecked ? [] : allIds)}
                     aria-label={allChecked ? 'Deselect all requests' : 'Select all ' + items.length + ' requests'} />
              <span className={'qh-qcheck-box' + (someChecked ? ' is-some' : '')}>{QCHK}</span>
            </label>
            <span>{checked.length ? checked.length + ' selected' : 'Select all ' + items.length}</span>
            <div className="qh-flex1" />
            {checked.length > 0 && (
              <div className="qh-batchbar-actions">
                <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={() => { st.batchApprove(checked, actor); setChecked([]); }}>Approve {checked.length}</button>
                <button className="qh-btn qh-btn-ghost qh-btn-sm" onClick={() => setChecked([])}>Clear</button>
              </div>
            )}
          </div>
        )}
        <div className="qh-qcards">
          <ManualRunsBlock st={st} />
          {/* An empty queue is the most likely place to be looking for a
              request that has already been decided, so the sentence naming
              where it went is also the way there. `#admin/audit` is picked up
              by the hashchange listener in AdminPanel — no prop drilling. */}
          {items.length === 0 && <div className="qh-aempty"><AdminIcons.approvals /><div>Queue is clear.</div><div className="qh-aempty-hint">Nothing waiting. Approved and rejected queries move to the <a className="qh-aempty-link" href="#admin/audit">audit log</a>.</div></div>}
          {groups.map(g => {
            if (g.solo) return <QueueCard key={g.solo.id} it={g.solo} selected={cur && cur.id === g.solo.id} onSelect={setSel} checked={checked.includes(g.solo.id)} onCheck={toggleCheck} />;
            const ids = g.rows.map(x => x.id);
            const on = ids.every(id => checked.includes(id));
            return (
              <div key={g.bundleId} className="qh-qbundle">
                <div className="qh-qbundle-head">
                  <label className="qh-qcheck">
                    <input type="checkbox" checked={on} onChange={() => setMany(ids, !on)} aria-label={(on ? 'Deselect' : 'Select') + ' the whole batch'} />
                    <span className={'qh-qcheck-box' + (!on && ids.some(id => checked.includes(id)) ? ' is-some' : '')}>{QCHK}</span>
                  </label>
                  <span className="qh-qbundle-t">Batch · {g.size} {g.size === 1 ? 'query' : 'queries'}</span>
                  {/* Only what is still waiting is here — say so rather than
                      letting "5 queries" sit above three rows. */}
                  {g.rows.length !== g.size && <span className="qh-qbundle-part">{g.rows.length} still waiting</span>}
                  <div className="qh-flex1" />
                  <button className="qh-btn qh-btn-ghost qh-btn-sm" onClick={() => st.approveBundle(g.bundleId, actor)}>Approve all</button>
                </div>
                {g.rows.map(it => <QueueCard key={it.id} it={it} selected={cur && cur.id === it.id} onSelect={setSel} checked={checked.includes(it.id)} onCheck={toggleCheck} />)}
              </div>
            );
          })}
        </div>
      </div>

      <div className="qh-adetail">
        {!cur ? <div className="qh-aempty big"><AdminIcons.approvals /><div>Nothing to review.</div></div> : (
          <>
            <div className="qh-adetail-head">
              <div className="qh-adetail-who">
                <span className="qh-qavatar lg">{cur.submitter.initials}</span>
                <div>
                  <div className="qh-adetail-name">{qhPersonName(cur.submitter.name)}</div>
                  {/* The handle is appended only when the name above was derived from it — it is what an approver types into Slack, so it must stay readable somewhere. */}
                  <div className="qh-adetail-slack"><SlackMark size={12} />{qhIsHandleName(cur.submitter.name) ? cur.submitter.name + ' · ' : ''}{cur.submitter.slackId}{cur.submitter.trust != null ? ' · trust ' + cur.submitter.trust : ''}</div>
                </div>
              </div>
              <div className="qh-adetail-target">
                <span className={'qh-env-dot env-' + cur.env} />
                <span className="qh-adetail-conn">{cur.connectionId}</span><span className="qh-target-slash">/</span><span>{cur.databaseId}</span>
                <TierBadge tier={cur.tier} />
                {/* Where it would run. An approver saying yes to a DDL on the
                    Huawei box is not saying yes to the same thing as on AWS. */}
                {qhHosting(cur) && (
                  <span className="qh-adetail-host" title={qhHostingFull(cur)}>
                    <img className="qh-prov-logo" src={qhProviderLogo(qhTags(cur).provider)} alt="" draggable={false} />
                    {qhHosting(cur)}
                  </span>
                )}
              </div>
            </div>

            <div className="qh-adetail-analysis">
              <div className="qh-astat"><div className="qh-astat-k">Classification</div><div className="qh-astat-v"><span className={'qh-sec-badge tier-' + cur.tier.toLowerCase()}><span className="qh-sec-dot" />{cur.tier}</span></div></div>
              <div className="qh-astat"><div className="qh-astat-k">Statements</div><div className="qh-astat-v">{cur.statements}</div></div>
              <div className="qh-astat"><div className="qh-astat-k">Est. impact</div><div className="qh-astat-v">{cur.tier === 'DDL' ? 'schema' : (cur.estRows != null ? '~' + cur.estRows + ' rows' : (cur.riskSummary || '—'))}</div></div>
              <div className="qh-astat"><div className="qh-astat-k">Tables</div><div className="qh-astat-v">{(cur.estTables || []).join(', ') || '—'}</div></div>
              <div className="qh-astat"><div className="qh-astat-k">Submitted</div><div className="qh-astat-v" title={cur.submittedAt || ''}>{cur.submittedAt ? qhFmt(cur.submittedAt) + ' · ' + qhAgo(cur.submittedAt) : '—'}</div></div>
            </div>

            {/* Everything the DDL escalations screen carried, on the request
                itself: why they hold schema rights, how long for, who gave
                them. A missing field is SAID missing — a blank here would read
                as "nothing to worry about" on the tier where that is least true. */}
            {(cur.escalate || cur.tier === 'DDL') && (() => {
              const el = cur.elevation || null;
              return (
                <div className="qh-ddlbox">
                  <div className="qh-ddlbox-h"><AdminIcons.ddl />Schema change — always reviewed by a person, never auto-approved</div>
                  <div className="qh-ddlbox-grid">
                    <div className="qh-ddlbox-k">DDL rights from</div>
                    <div className="qh-ddlbox-v">{!el ? <span className="qh-ddlbox-miss">not reported</span> : el.source === 'team' ? <>team <b>{el.team}</b></> : el.source === 'admin' ? <>their <b>admin role</b> — a fleet admin's own standing</> : 'their own grant'}</div>
                    <div className="qh-ddlbox-k">Why they hold it</div>
                    <div className="qh-ddlbox-v">{el && el.reason ? el.reason : <span className="qh-ddlbox-miss">no reason recorded</span>}</div>
                    <div className="qh-ddlbox-k">Lasts</div>
                    <div className="qh-ddlbox-v">{!el ? <span className="qh-ddlbox-miss">not reported</span> : el.expiresAt ? <>until <b>{qhFmt(el.expiresAt)}</b></> : el.source === 'admin' ? 'as long as they are an admin' : <span className="qh-ddlbox-warn">no end date — standing schema rights</span>}</div>
                    <div className="qh-ddlbox-k">Given by</div>
                    <div className="qh-ddlbox-v">{el && (el.grantedByName || el.grantedBy) ? <>{qhPersonName(el.grantedByName || el.grantedBy)}{el.grantedAt ? ' · ' + qhAgo(el.grantedAt) : ''}</> : <span className="qh-ddlbox-miss">not recorded</span>}</div>
                  </div>
                </div>
              );
            })()}

            {cur.piiCols.length > 0 && (
              <div className="qh-adetail-pii">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="4" y="10" width="16" height="10" rx="2"/><path d="M8 10V7a4 4 0 018 0v3"/></svg>
                Touches PII — masked on return: <b>{cur.piiCols.join(', ')}</b>
              </div>
            )}

            <div className="qh-adetail-sqlwrap">
              <div className="qh-adetail-sqllabel">SQL</div>
              <pre className="qh-adetail-sql"><code dangerouslySetInnerHTML={{ __html: qhHighlight(cur.sql) }} /></pre>
            </div>

            {/* `justification` is the canonical field; `reason` is the legacy
                alias the queue still emits for one release. */}
            <div className="qh-adetail-reason"><span className="qh-reason-k">Reason</span>{cur.justification || cur.reason}</div>

            {cur.bundleId && (
              <div className="qh-bundle-note">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>
                Part of bundle <b>{cur.bundleId}</b> ({cur.bundleSize || items.filter(x => x.bundleId === cur.bundleId).length} queries) — one approval round.
                <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={() => st.approveBundle(cur.bundleId, actor)}>Approve whole bundle</button>
              </div>
            )}

            {noteMode && <NoteBox mode={noteMode} value={note} onChange={setNote} onCancel={() => setNoteMode(null)} />}

            {/* Approve leads, in its own colour — it is the action taken on
                the overwhelming majority of requests, so it sits where the
                eye lands first. Reject / Request changes follow beside it;
                the escalation tag stays right-aligned. */}
            <div className="qh-adetail-actions">
              <button className="qh-btn qh-btn-primary" onClick={() => act('approve')}>
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5"/></svg>
                {cur.escalate || cur.tier === 'DDL' ? 'Approve schema change' : 'Approve & run'}
              </button>
              <button className="qh-btn qh-btn-danger" disabled={noteMode === 'reject' && !noteChk.ok}
                      title={noteMode === 'reject' ? noteChk.why : null} onClick={() => act('reject')}>{noteMode === 'reject' ? 'Confirm reject' : 'Reject'}</button>
              <button className="qh-btn qh-btn-ghost" disabled={noteMode === 'changes' && !noteChk.ok}
                      title={noteMode === 'changes' ? noteChk.why : null} onClick={() => act('changes')}>{noteMode === 'changes' ? 'Send request' : 'Request changes'}</button>
              <div className="qh-flex1" />
              {cur.escalate && <span className="qh-escalate-tag"><AdminIcons.ddl />Escalated · DDL</span>}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ---------- Kill switch ----------
// The DBA level, on screen as on the server (PLA-1592 U4a): POST /admin/kill
// needs a DBA or a super-admin, and so does /sql kill in Slack. Every admin
// still READS it — the state, who, why, and the past pauses — because "is
// execution paused, and by whom" is what a DBA is asked first in an incident.
// What an admin below the DBA level does not get is a button the server would
// refuse.

// How long a pause lasted, from two ISO times. '' when a time is missing or the
// order is wrong: an absent fact shows as absent, never as "0s".
function qhKillLasted(fromIso, toIso) {
  const a = new Date(fromIso || '').getTime(), b = new Date(toIso || '').getTime();
  if (isNaN(a) || isNaN(b) || b < a) return '';
  const s = Math.floor((b - a) / 1000);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm';
  const h = Math.floor(m / 60);
  if (h < 24) return h + 'h' + (m % 60 ? ' ' + (m % 60) + 'm' : '');
  const d = Math.floor(h / 24);
  return d + 'd' + (h % 24 ? ' ' + (h % 24) + 'h' : '');
}
// The sentence after a switch to ON. `cancelledCount` from POST /admin/kill is
// a number, or null when the server could not count. Null is said as unknown,
// never as zero. Writes keep running on purpose, so every case says why.
const QH_KILL_WRITES = 'Writes and schema changes continue, because a cancel can leave a change half done.';
function qhKillCancelSay(n) {
  if (typeof n !== 'number') return 'QueryHub could not count the cancelled reads. ' + QH_KILL_WRITES;
  if (n === 0) return 'No read was running, so QueryHub cancelled nothing. ' + QH_KILL_WRITES;
  return 'QueryHub cancelled ' + n + ' running read' + (n === 1 ? '' : 's') + '. ' + QH_KILL_WRITES;
}
function qhKillCountCell(n) {
  if (typeof n !== 'number') return { text: 'Not counted', title: 'This pause is older than the count, or QueryHub could not count.', muted: true };
  if (n === 0) return { text: 'None', title: 'No read was running.', muted: true };
  return { text: n + ' read' + (n === 1 ? '' : 's'), title: null, muted: false };
}

function KillHistory({ rows, onRetry }) {
  if (rows === null) return <div className="qh-kill-hist-state" aria-busy="true">Loading the pauses…</div>;
  if (rows === 'error') return (
    <div className="qh-kill-hist-state" role="alert">Could not load the pauses.
      <button className="qh-btn qh-btn-ghost qh-btn-sm" onClick={onRetry}>Try again</button></div>);
  if (!rows.length) return <div className="qh-kill-hist-state">No pauses on record.</div>;
  return (
    <div className="qh-tablewrap">
      <table className="qh-atable qh-kill-hist">
        <thead><tr><th>When</th><th>By</th><th>Why</th><th>Lasted</th><th>Cancelled</th></tr></thead>
        <tbody>{rows.map((p, i) => {
          const c = qhKillCountCell(p.cancelledCount);
          const lasted = p.endedAt ? qhKillLasted(p.startedAt, p.endedAt) : null;
          return (
            <tr key={(p.startedAt || '') + ':' + i}>
              <td title={p.startedAt || ''}>{p.startedAt ? qhFmt(p.startedAt) : '—'}<div className="qh-muted qh-kill-hist-sub">{qhAgo(p.startedAt)}</div></td>
              <td title={qhIsHandleName(p.by) ? p.by : null}>{p.by ? qhPersonName(p.by) : <span className="qh-muted">Not recorded</span>}</td>
              <td className="qh-kill-hist-why">{p.message ? p.message : <span className="qh-muted">No reason given</span>}</td>
              <td>{p.endedAt
                ? <>{lasted || '—'}<div className="qh-muted qh-kill-hist-sub" title={p.endedAt}>released{p.endedBy ? ' by ' + qhPersonName(p.endedBy) : ''}</div></>
                : <span className="qh-kill-hist-open">Still on</span>}</td>
              <td title={c.title}>{c.muted ? <span className="qh-muted">{c.text}</span> : c.text}</td>
            </tr>
          );
        })}</tbody>
      </table>
    </div>
  );
}

function KillView({ st, user, canWrite }) {
  const k = st.killSwitch;
  const actor = 'dba.' + user.name.split(' ')[0].toLowerCase();
  const [msg, setMsg] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const [said, setSaid] = React.useState(null);     // the sentence after a switch to ON
  // null = loading, 'error' = the read failed, [] = no pause on record.
  const [hist, setHist] = React.useState(null);
  const loadHist = React.useCallback(() => {
    setHist(null);
    st.killHistory().then(r => setHist(Array.isArray(r) ? r : [])).catch(() => setHist('error'));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  React.useEffect(() => { loadHist(); }, [loadHist]);
  const flip = () => {
    if (busy) return;
    const on = !k.enabled;
    setBusy(true); setSaid(null);
    st.toggleKill(on, actor, msg).then(r => {
      setBusy(false);
      if (r) { setMsg(''); if (on) setSaid(qhKillCancelSay(r.cancelledCount)); }
      loadHist();
    });
  };
  const by = k.by ? qhPersonName(k.by) : null;
  return (
    <div className="qh-apad">
      <div className="qh-aview-head"><div><div className="qh-aview-title">Kill switch</div><div className="qh-aview-sub">Emergency stop for all query execution on every server. Same as <code>/sql kill</code> in Slack. A DBA or a super-admin can engage it.</div></div></div>
      <div className={'qh-kill-card' + (k.enabled ? ' is-on' : '')}>
        <div className="qh-kill-ic">
          <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M18.4 6.6a9 9 0 11-12.8 0M12 2v10"/></svg>
        </div>
        <div className="qh-kill-body">
          <div className="qh-kill-status">{k.enabled ? 'Execution PAUSED' : 'Execution normal'}</div>
          <div className="qh-kill-note">{k.enabled
            ? ('Paused' + (by ? ' by ' + by : '') + (k.at ? ' · ' + qhAgo(k.at) : '') + (k.message ? ' · “' + k.message + '”' : '') + '. QueryHub blocks new submissions. Running reads stop. Running writes and schema changes continue.')
            : 'All targets accept queries. Approvals and runs continue as usual.'}</div>
          {!k.enabled && canWrite && <input className="qh-input qh-kill-msg" value={msg} onChange={(e) => setMsg(e.target.value)} placeholder="Optional reason for developers, for example an incident number" />}
          {said && <div className="qh-kill-said" role="status">{said}</div>}
        </div>
        {canWrite
          ? <button className={'qh-btn qh-btn-lg ' + (k.enabled ? 'qh-btn-primary' : 'qh-btn-danger')} disabled={busy} onClick={flip}>
              {k.enabled ? 'Release kill switch' : 'Engage kill switch'}
            </button>
          : <span className="qh-kill-ro">Only a DBA or a super-admin can {k.enabled ? 'release' : 'engage'} it</span>}
      </div>
      <div className="qh-kill-hint">While engaged: developers see a banner and a disabled Submit, auto-approve is suspended, and scheduled runs hold. Engaging it cancels the running reads. Use it for incidents: a runaway query, a credential leak, a target under load. QueryHub audits every change.</div>
      <div className="qh-kill-hist-h">Past pauses</div>
      <KillHistory rows={hist} onRetry={loadHist} />
    </div>
  );
}

// ---------- The note on Reject and Request changes (2026-10-23) ----------
// Design takeaway 2: a note shorter than this does not tell the submitter what
// to change. Web only. The server and Slack keep their own rule (not empty), so
// nothing that Slack accepts is refused there.
const QH_NOTE_MIN = 15;
// Sentence-case chips. Each one adds a full sentence that alone meets the
// minimum, so one click is enough, and a second chip adds to the first.
const QH_NOTE_CHIPS = [
  { label: 'Try on test first', text: 'Run this on a test database first.' },
  { label: 'Add a WHERE clause', text: 'Add a WHERE clause to limit the rows.' },
  { label: 'Link an incident', text: 'Add the incident number to the reason.' },
  { label: 'Inside change freeze', text: 'A change freeze is active. Submit this again after the freeze.' },
];
// Counts trimmed characters (code points, so "\u015f" is one). `why` is the title
// of the disabled send button, and null once the note is long enough.
function qhNoteCheck(text) {
  const n = Array.from(String(text == null ? '' : text).trim()).length;
  const ok = n >= QH_NOTE_MIN;
  return { n, ok, count: ok ? n + ' characters' : n + ' of ' + QH_NOTE_MIN,
    why: ok ? null : 'Write at least ' + QH_NOTE_MIN + ' characters. The submitter reads this note.' };
}
// A chip adds its sentence. It never adds the same sentence twice.
function qhNoteAdd(text, add) {
  const t = String(text == null ? '' : text).trim();
  if (!t) return add;
  if (t.indexOf(add) >= 0) return t;
  return t + (/[.!?]$/.test(t) ? ' ' : '. ') + add;
}
function NoteBox({ mode, value, onChange, onCancel }) {
  const chk = qhNoteCheck(value);
  const id = 'qh-note-' + mode;
  return (
    <div className="qh-notebox">
      <label className="qh-notebox-l" htmlFor={id}>{mode === 'reject' ? 'Why do you reject it? The submitter reads this note.' : 'What must they change? The submitter reads this note.'}</label>
      <textarea id={id} className="qh-input qh-notebox-in" autoFocus rows={3} value={value}
                aria-describedby={id + '-n'} onChange={(e) => onChange(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Escape') onCancel(); }} />
      <div className="qh-notebox-foot">
        <div className="qh-notebox-chips">{QH_NOTE_CHIPS.map(c => (
          <button key={c.label} type="button" className="qh-notechip" title={c.text}
                  onClick={() => onChange(qhNoteAdd(value, c.text))}>{c.label}</button>))}</div>
        <span id={id + '-n'} className={'qh-notebox-n' + (chk.ok ? ' is-ok' : '')}>{chk.count}</span>
        <button type="button" className="qh-btn qh-btn-ghost qh-btn-sm" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

Object.assign(window, { AdminPanel, AdminIcons, qhAdminLevel, qhAdminCanOpen, qhAdminLockReason, qhKillLasted, qhKillCancelSay, qhNoteCheck, qhNoteAdd, QH_NOTE_MIN });
