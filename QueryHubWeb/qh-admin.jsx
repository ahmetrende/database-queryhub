// QueryHub Admin — panel shell (nav + role) + Approvals + DDL escalation views.
const { useState: useAdm } = React;

const AdminIcons = {
  approvals: () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/></svg>,
  ddl: () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2l9 4.5v5c0 5-3.8 8.5-9 10-5.2-1.5-9-5-9-10v-5L12 2z"/><path d="M12 8v4M12 16h.01"/></svg>,
  grants: () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M16 21v-2a4 4 0 00-4-4H6a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 11l-3 3-2-2"/></svg>,
  auto: () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M13 2L3 14h7l-1 8 10-12h-7z"/></svg>,
  scopes: () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>,
  teams: () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75"/></svg>,
  conns: () =><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"><ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6"/></svg>,
  audit: () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><path d="M14 2v6h6M8 13h8M8 17h5"/></svg>,
  metrics: () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M3 3v18h18"/><path d="M7 14l3-3 3 3 5-6"/></svg>,
  feedback: () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg>,
  kill: () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M18.4 6.6a9 9 0 11-12.8 0M12 2v10"/></svg>,
  config: () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M4 6h10M18 6h2M4 12h2M10 12h10M4 18h7M15 18h5"/><circle cx="16" cy="6" r="2"/><circle cx="8" cy="12" r="2"/><circle cx="13" cy="18" r="2"/></svg>,
};

const QH_ADMIN_SECTIONS = ['approvals', 'ddl', 'kill', 'grants', 'auto', 'scopes', 'teams', 'conns', 'audit', 'metrics', 'feedback', 'config'];
// Deep-link support: the admin section lives in the URL as #admin/<section>.
function navFromAdminHash() {
  const m = (typeof location !== 'undefined' ? (location.hash || '') : '').match(/^#admin\/([a-z]+)/i);
  return m && QH_ADMIN_SECTIONS.indexOf(m[1].toLowerCase()) !== -1 ? m[1].toLowerCase() : null;
}

function AdminPanel({ st, adminRole, setAdminRole, user }) {
  const [nav, setNav] = useAdm(() => navFromAdminHash() || 'approvals');
  const escCount = st.queue.filter(q => q.escalate).length;
  const pendCount = st.queue.length;
  const erCount = st.endpointReqs.filter(e => e.status === 'submitted').length;

  const groups = [
    { label: 'Review', items: [
      ['approvals', 'Approval queue', AdminIcons.approvals, pendCount],
      ['ddl', 'DDL escalations', AdminIcons.ddl, escCount],
      ['kill', 'Kill switch', AdminIcons.kill, null],
    ]},
    { label: 'Access', super: true, items: [
      ['grants', 'Grants', AdminIcons.grants, null],
      ['auto', 'Auto-approve', AdminIcons.auto, null],
      ['scopes', 'Admin scopes', AdminIcons.scopes, null],
      ['teams', 'Teams', AdminIcons.teams, null],
      ['conns', 'Connections', AdminIcons.conns, erCount],
    ]},
    { label: 'Insights', items: [
      ['audit', 'Audit log', AdminIcons.audit, null],
      ['metrics', 'Metrics', AdminIcons.metrics, null],
      ['feedback', 'Feedback', AdminIcons.feedback, null],
    ]},
    { label: 'System', super: true, items: [
      ['config', 'System configuration', AdminIcons.config, null],
    ]},
  ];

  const canSuper = !!(user && user.role === 'super');
  const isSuper = canSuper && adminRole === 'super';
  // DBA can't open super-only sections
  const visibleNav = (id) => {
    if (['grants', 'auto', 'scopes', 'teams', 'conns', 'config'].includes(id)) return isSuper;
    return true;
  };
  const curNav = visibleNav(nav) ? nav : 'approvals';

  // Keep the URL in sync so an admin section is linkable/bookmarkable
  // (e.g. #admin/audit). Client-only — no backend involved.
  React.useEffect(() => {
    const want = '#admin/' + curNav;
    if (location.hash !== want) history.replaceState(null, '', want);
  }, [curNav]);
  React.useEffect(() => {
    const onHash = () => { const n = navFromAdminHash(); if (n) setNav(n); };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  return (
    <div className="qh-admin">
      <nav className="qh-anav">
        <div className="qh-anav-role">
          <div className="qh-anav-role-label">Viewing as</div>
          <div className="qh-role-seg">
            <button className={'qh-role-opt' + (adminRole === 'dba' ? ' is-active' : '')} onClick={() => setAdminRole('dba')}>DBA</button>
            <button className={'qh-role-opt' + (adminRole === 'super' ? ' is-active' : '')} disabled={!canSuper} title={canSuper ? undefined : 'Super-admin only'} onClick={() => canSuper && setAdminRole('super')}>Super-admin</button>
          </div>
          <div className="qh-anav-role-hint">{isSuper ? 'Full access: review + access control' : 'Review & insights only'}</div>
        </div>
        {groups.map(g => {
          if (g.super && !isSuper) return (
            <div key={g.label} className="qh-anav-group is-locked">
              <div className="qh-anav-glabel">{g.label}<span className="qh-lock"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 018 0v3"/></svg></span></div>
              <div className="qh-anav-locked-note">Super-admin only</div>
            </div>
          );
          return (
            <div key={g.label} className="qh-anav-group">
              <div className="qh-anav-glabel">{g.label}</div>
              {g.items.map(([id, label, Icon, count]) => (
                <button key={id} className={'qh-anav-item' + (curNav === id ? ' is-active' : '')} onClick={() => setNav(id)}>
                  <Icon /><span className="qh-anav-text">{label}</span>
                  {count != null && count > 0 && <span className="qh-anav-badge">{count}</span>}
                </button>
              ))}
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
        {curNav === 'approvals' && <ApprovalsView st={st} user={user} role={adminRole} />}
        {curNav === 'ddl' && <DdlView st={st} user={user} />}
        {curNav === 'kill' && <KillView st={st} user={user} />}
        {curNav === 'grants' && <GrantsView st={st} user={user} />}
        {curNav === 'auto' && <AutoView st={st} user={user} />}
        {curNav === 'scopes' && <ScopesView st={st} user={user} />}
        {curNav === 'teams' && <TeamsView st={st} user={user} />}
        {curNav === 'conns' && <ConnectionsView st={st} user={user} />}
        {curNav === 'audit' && <AuditView2 st={st} />}
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
function QueueCard({ it, selected, onSelect, checked, onCheck }) {
  const who = it.submitter.name + ', ' + it.tier + ' on '
    + it.connectionId + '/' + it.databaseId + ', ' + qhAgo(it.submittedAt);
  return (
    <div className={'qh-qcard' + (selected ? ' is-active' : '')}>
      <label className="qh-qcheck">
        <input type="checkbox" checked={checked} onChange={() => onCheck(it.id)}
               aria-label={'Select request from ' + who} />
        <span className="qh-qcheck-box"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5"/></svg></span>
      </label>
      <button type="button" className="qh-qcard-main" onClick={() => onSelect(it.id)}
              aria-label={'Review request from ' + who} aria-pressed={!!selected}>
        <div className="qh-qcard-top">
          <span className="qh-qavatar">{it.submitter.initials}</span>
          <span className="qh-qname">{it.submitter.name}</span>
          <TierBadge tier={it.tier} sm />
          <span className={'qh-origin-chip o-' + (it.origin === 'web' ? 'web' : 'slack')} title={'Submitted via ' + (it.origin === 'web' ? 'the web app' : 'Slack')}>{it.origin === 'web' ? 'web' : 'slack'}</span>
          {it.bundleId && <span className="qh-bundle-chip" title={'Bundle ' + it.bundleId}>bundle</span>}
          {it.piiCols.length > 0 && <span className="qh-qpii" title={it.piiCols.join(', ')}>PII</span>}
        </div>
        <div className="qh-qcard-target">{it.connectionId}/{it.databaseId}</div>
        <div className="qh-qcard-sql">{it.sql.replace(/\n/g, ' ')}</div>
        <div className="qh-qcard-when">{qhAgo(it.submittedAt)}</div>
      </button>
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
  const [sel, setSel] = useAdm(items[0] ? items[0].id : null);
  const [checked, setChecked] = useAdm([]);
  const [note, setNote] = useAdm('');
  const [noteMode, setNoteMode] = useAdm(null); // 'reject' | 'changes'

  // `sel === null` means "nothing left to review" — it must NOT fall back to
  // the first row, or deciding the last item re-selects the one just decided.
  const cur = sel == null ? null : (items.find(x => x.id === sel) || items[0]);
  const toggleCheck = (id) => setChecked(c => c.includes(id) ? c.filter(x => x !== id) : [...c, id]);

  const act = (decision) => {
    if ((decision === 'reject' || decision === 'changes') && noteMode !== decision) { setNoteMode(decision); return; }
    st.decide(cur.id, decision, user ? ('dba.' + (user.name.split(' ')[0].toLowerCase())) : 'dba', note.trim() || undefined);
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
        {checked.length > 0 && (
          <div className="qh-batchbar">
            <span>{checked.length} selected</span>
            <div className="qh-batchbar-actions">
              <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={() => { st.batchApprove(checked, 'dba.admin'); setChecked([]); }}>Approve {checked.length}</button>
              <button className="qh-btn qh-btn-ghost qh-btn-sm" onClick={() => setChecked([])}>Clear</button>
            </div>
          </div>
        )}
        <div className="qh-qcards">
          {items.length === 0 && <div className="qh-aempty"><AdminIcons.approvals /><div>Queue is clear.</div><div className="qh-aempty-hint">Approved & rejected queries move to the audit log.</div></div>}
          {items.map(it => <QueueCard key={it.id} it={it} selected={cur && cur.id === it.id} onSelect={setSel} checked={checked.includes(it.id)} onCheck={toggleCheck} />)}
        </div>
      </div>

      <div className="qh-adetail">
        {!cur ? <div className="qh-aempty big"><AdminIcons.approvals /><div>Nothing to review.</div></div> : (
          <>
            <div className="qh-adetail-head">
              <div className="qh-adetail-who">
                <span className="qh-qavatar lg">{cur.submitter.initials}</span>
                <div>
                  <div className="qh-adetail-name">{cur.submitter.name}</div>
                  <div className="qh-adetail-slack"><SlackMark size={12} />{cur.submitter.slackId}{cur.submitter.trust != null ? ' · trust ' + cur.submitter.trust : ''}</div>
                </div>
              </div>
              <div className="qh-adetail-target">
                <span className={'qh-env-dot env-' + cur.env} />
                <span className="qh-adetail-conn">{cur.connectionId}</span><span className="qh-target-slash">/</span><span>{cur.databaseId}</span>
                <TierBadge tier={cur.tier} />
              </div>
            </div>

            <div className="qh-adetail-analysis">
              <div className="qh-astat"><div className="qh-astat-k">Classification</div><div className="qh-astat-v"><span className={'qh-sec-badge tier-' + cur.tier.toLowerCase()}><span className="qh-sec-dot" />{cur.tier}</span></div></div>
              <div className="qh-astat"><div className="qh-astat-k">Statements</div><div className="qh-astat-v">{cur.statements}</div></div>
              <div className="qh-astat"><div className="qh-astat-k">Est. impact</div><div className="qh-astat-v">{cur.tier === 'DDL' ? 'schema' : (cur.estRows != null ? '~' + cur.estRows + ' rows' : (cur.riskSummary || '—'))}</div></div>
              <div className="qh-astat"><div className="qh-astat-k">Tables</div><div className="qh-astat-v">{(cur.estTables || []).join(', ') || '—'}</div></div>
              <div className="qh-astat"><div className="qh-astat-k">Submitted</div><div className="qh-astat-v" title={cur.submittedAt || ''}>{cur.submittedAt ? qhFmt(cur.submittedAt) + ' · ' + qhAgo(cur.submittedAt) : '—'}</div></div>
            </div>

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
                Part of bundle <b>{cur.bundleId}</b> ({items.filter(x => x.bundleId === cur.bundleId).length} queries) — one approval round.
                <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={() => st.approveBundle(cur.bundleId, 'dba.admin')}>Approve whole bundle</button>
              </div>
            )}

            {noteMode && (
              <div className="qh-notebox">
                <input className="qh-input" autoFocus placeholder={noteMode === 'reject' ? 'Why is this rejected? (sent to submitter)' : 'What should they change?'} value={note} onChange={(e) => setNote(e.target.value)} />
              </div>
            )}

            {/* Approve leads, in its own colour — it is the action taken on
                the overwhelming majority of requests, so it sits where the
                eye lands first. Reject / Request changes follow beside it;
                the escalation tag stays right-aligned. */}
            <div className="qh-adetail-actions">
              <button className="qh-btn qh-btn-primary" onClick={() => act('approve')}>
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5"/></svg>
                Approve & run
              </button>
              <button className="qh-btn qh-btn-danger" onClick={() => act('reject')}>{noteMode === 'reject' ? 'Confirm reject' : 'Reject'}</button>
              <button className="qh-btn qh-btn-ghost" onClick={() => act('changes')}>{noteMode === 'changes' ? 'Send request' : 'Request changes'}</button>
              <div className="qh-flex1" />
              {cur.escalate && <span className="qh-escalate-tag"><AdminIcons.ddl />Escalated · DDL</span>}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ---------- DDL escalations ----------
function DdlView({ st, user }) {
  const items = st.queue.filter(q => q.escalate);
  return (
    <div className="qh-apad">
      <div className="qh-aview-head">
        <div>
          <div className="qh-aview-title">DDL escalations</div>
          <div className="qh-aview-sub">Schema changes always need a human DBA — never auto-approved.</div>
        </div>
      </div>
      {items.length === 0 ? <div className="qh-aempty big"><AdminIcons.ddl /><div>No schema changes waiting.</div></div> : (
        <div className="qh-ddl-list">
          {items.map(it => (
            <div key={it.id} className="qh-ddl-card">
              <div className="qh-ddl-top">
                <span className="qh-qavatar">{it.submitter.initials}</span>
                <span className="qh-qname">{it.submitter.name}</span>
                <TierBadge tier="DDL" />
                <span className={'qh-origin-chip o-' + (it.origin === 'web' ? 'web' : 'slack')} title={'Submitted via ' + (it.origin === 'web' ? 'the web app' : 'Slack')}>{it.origin === 'web' ? 'web' : 'slack'}</span>
                <span className="qh-ddl-target">{it.connectionId}/{it.databaseId}</span>
                <span className="qh-qcard-when">{qhAgo(it.submittedAt)}</span>
              </div>
              <pre className="qh-adetail-sql sm"><code dangerouslySetInnerHTML={{ __html: qhHighlight(it.sql) }} /></pre>
              <div className="qh-adetail-reason"><span className="qh-reason-k">Reason</span>{it.justification || it.reason}</div>
              <div className="qh-ddl-actions">
                <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={() => st.decide(it.id, 'approve', 'dba.ops')}>Approve schema change</button>
                <button className="qh-btn qh-btn-danger qh-btn-sm" onClick={() => st.decide(it.id, 'reject', 'dba.ops')}>Reject</button>
                <button className="qh-btn qh-btn-ghost qh-btn-sm" onClick={() => st.decide(it.id, 'changes', 'dba.ops')}>Request changes</button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------- Kill switch ----------
function KillView({ st, user }) {
  const k = st.killSwitch;
  const actor = 'dba.' + user.name.split(' ')[0].toLowerCase();
  const [msg, setMsg] = React.useState('');
  return (
    <div className="qh-apad">
      <div className="qh-aview-head"><div><div className="qh-aview-title">Kill switch</div><div className="qh-aview-sub">Emergency stop — pause all query execution fleet-wide. Mirrors Slack <code>/sql kill</code>. Super-admin only.</div></div></div>
      <div className={'qh-kill-card' + (k.enabled ? ' is-on' : '')}>
        <div className="qh-kill-ic">
          <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M18.4 6.6a9 9 0 11-12.8 0M12 2v10"/></svg>
        </div>
        <div className="qh-kill-body">
          <div className="qh-kill-status">{k.enabled ? 'Execution PAUSED' : 'Execution normal'}</div>
          <div className="qh-kill-note">{k.enabled ? ('Paused by ' + k.by + ' · ' + qhAgo(k.at) + (k.message ? ' · “' + k.message + '”' : '') + '. In-flight runs finish; new submissions are blocked.') : 'All targets accepting queries. Approvals and runs proceed normally.'}</div>
          {!k.enabled && <input className="qh-input qh-kill-msg" value={msg} onChange={(e) => setMsg(e.target.value)} placeholder="Optional reason shown to developers (e.g. incident #4821)" />}
        </div>
        <button className={'qh-btn qh-btn-lg ' + (k.enabled ? 'qh-btn-primary' : 'qh-btn-danger')} onClick={() => { st.toggleKill(!k.enabled, actor, msg); setMsg(''); }}>
          {k.enabled ? 'Release kill switch' : 'Engage kill switch'}
        </button>
      </div>
      <div className="qh-kill-hint">While engaged: developers see a banner and a disabled Submit, auto-approve is suspended, and scheduled runs hold. Use for incidents (runaway query, credential leak, target under load). Every toggle is audited.</div>
    </div>
  );
}

Object.assign(window, { AdminPanel, AdminIcons });
