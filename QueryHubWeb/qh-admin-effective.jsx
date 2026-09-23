// QueryHub Admin — Effective access (design round 2026-09-22 §1).
//
// The question this screen answers: "what can this person — or this team —
// actually reach, and WHY?" Before it, an admin answered that by reading the
// grants, auto-approve and scopes tables and applying the precedence rules by
// hand. So the precedence is the screen, not a footnote:
//   personal grant  >  team grant  >  nothing, decided PER DATABASE — and an
//   ENDED personal grant still decides: it is no access, not a fall-through to
//   the team (CODE 2026-09-23 §3, the resolver's rule 2). An admin bypasses the
//   question entirely.
// Every row says which of those it came from, and where a lower rung was
// overruled the row says that too.
//
// The resolution is the SERVER's (GET /admin/people/{id}/effective-access and
// the team twin), the same resolver the executor uses. The one thing read from
// the grants table here is the "overruled" note — which grant did NOT win — and
// it is worded as a note, never as access.
//
// Three states that must not look alike: resolving, "could not resolve" (says
// nothing about their access), and "resolved: no access" (a fact).
const { useState: useEff } = React;

const EffIcon = {
  search: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="7" /><path d="M21 21l-4.3-4.3" /></svg>,
  warn: () => <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L14.7 3.9a2 2 0 00-3.4 0z" /><path d="M12 9v4M12 17h.01" /></svg>,
  none: () => <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="12" cy="12" r="9" /><path d="M5.6 5.6l12.8 12.8" /></svg>,
  bolt: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M13 2L3 14h7l-1 8 10-12h-7z" /></svg>,
  shield: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /></svg>,
  arrow: () => <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14M13 6l6 6-6 6" /></svg>,
};

function effDate(iso) {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  const o = { day: 'numeric', month: 'short' };
  if (d.getFullYear() !== new Date().getFullYear()) o.year = 'numeric';
  return d.toLocaleDateString('en-GB', o);
}
// Fourteen days is where "ends" becomes something to act on: long enough to
// renew before it lapses, short enough that it is not every row.
function EffEnds({ iso }) {
  if (!iso) return <span className="qh-eff-ends is-open">No end date</span>;
  const days = Math.ceil((new Date(iso) - Date.now()) / 86400000);
  if (days < 0) return <span className="qh-eff-ends is-exp">Ended {effDate(iso)}</span>;
  const cls = days <= 14 ? ' is-soon' : '';
  return <span className={'qh-eff-ends' + cls} title={iso}>Ends {effDate(iso)}<span className="qh-eff-ends-n">{days === 0 ? 'today' : days === 1 ? 'tomorrow' : 'in ' + days + ' days'}</span></span>;
}

// Where a row came from, in the words the precedence rule uses.
function EffSource({ t }) {
  if (t.source === 'admin_or_bypass') return <span className="qh-eff-src is-admin">Admin bypass</span>;
  if (t.source === 'team') return <span className="qh-eff-src is-team">Team · {t.sourceTeam || 'unnamed'}</span>;
  return <span className="qh-eff-src">Own grant</span>;
}

// Names, not counts. "All databases" is its own chip rather than a missing list,
// because on a shared server "which databases" is the whole question.
function EffDbs({ all, dbs }) {
  if (all || !dbs || !dbs.length) return <span className="qh-eff-db is-all">All databases</span>;
  return <>{dbs.map(d => <span key={d} className="qh-eff-db">{d}</span>)}</>;
}

// The rungs that did NOT win, for one row. Read from the grants table and said
// as a note: the row above it is the answer, this is why it is the answer.
// Per database, as the resolver decides it (CODE 2026-09-23 §4): a team grant
// on ANOTHER database of the same server did not lose to anything.
const effLiveG = (g) => !g.expiresAt || new Date(g.expiresAt).getTime() > Date.now();
const effDbsOf = (g) => (!g.databases || !g.databases.length || g.databases.indexOf('*') >= 0) ? null : g.databases;   // null = every database
const effMeets = (a, b) => !a || !b || a.some(d => b.indexOf(d) >= 0);
function effOverruled(st, eff, t) {
  const teams = (eff.teams || []).map(x => x.name);
  const rowDbs = t.allDatabases ? null : (t.databases || null);
  const onConn = (st.grants || []).filter(g => g.connectionId === t.connectionId && g.subjectType === 'team'
    && teams.indexOf(g.subject) >= 0 && effLiveG(g) && effMeets(effDbsOf(g), rowDbs));
  const out = [];
  if (t.source === 'user') onConn.forEach(g =>
    out.push(<>Team <b>{g.subject}</b> also grants {g.tier} here — not used, their own grant takes precedence.</>));
  if (t.source === 'team') onConn.filter(g => g.subject !== t.sourceTeam).forEach(g =>
    out.push(<>Team <b>{g.subject}</b> grants {g.tier} here too; the resolver merged the two.</>));
  return out;
}
// Where an ENDED own grant is the answer. The resolver returns nothing for that
// database, so without this row the screen would show the team granting it and
// the person not reaching it, with no reason in between — the exact question an
// admin opens this screen to answer. Same client read as the notes: the grants
// table, said as a reason, never as access.
function effBlocked(st, eff, ids) {
  const teams = (eff.teams || []).map(x => x.name);
  const gs = st.grants || [];
  const mine = (g) => g.subjectType === 'user' && ids.indexOf(g.subject) >= 0;
  const out = [];
  gs.filter(g => mine(g) && !effLiveG(g)).forEach(g => {
    const dbs = effDbsOf(g);
    if (gs.some(x => x !== g && mine(x) && x.connectionId === g.connectionId && effLiveG(x) && effMeets(effDbsOf(x), dbs))) return;
    const team = gs.find(x => x.subjectType === 'team' && teams.indexOf(x.subject) >= 0 && x.connectionId === g.connectionId && effLiveG(x) && effMeets(effDbsOf(x), dbs));
    if (team) out.push({ g, team });
  });
  return out;
}
function EffBlockedRow({ g, team }) {
  const dbs = effDbsOf(g);
  return (
    <div className="qh-eff-row is-blocked">
      <div className="qh-eff-row-main">
        <span className="qh-eff-conn">{g.connectionId}</span>
        <span className="qh-eff-dbs"><EffDbs all={!dbs} dbs={dbs} /></span>
        <span className="qh-eff-noacc">No access</span>
        <span className="qh-eff-src">Own grant ended</span>
        <EffEnds iso={g.expiresAt} />
      </div>
      <div className="qh-eff-notes"><div className="qh-eff-note">Their own grant ended on {effDate(g.expiresAt)}; team <b>{team.subject}</b>'s {team.tier} does not apply to them. Deleting the ended grant lets the team's apply; renewing it restores their own.</div></div>
    </div>
  );
}

// `mixedTiers` (team rows): `tier` is the highest, so one badge would overstate
// the rest — the databases are listed one per line, each with its own tier and
// end, instead. `enabled: false` is the CONNECTION being off, which no grant
// changes, so it is said on the row rather than left for the admin to find.
function EffAccessRow({ t, notes }) {
  const split = !!t.mixedTiers && (t.perDatabase || []).length > 1;
  return (
    <div className={'qh-eff-row' + (t.enabled === false ? ' is-off' : '')}>
      <div className="qh-eff-row-main">
        <span className="qh-eff-conn">{t.connectionId}</span>
        <span className="qh-eff-dbs">{split ? <span className="qh-eff-mixed">Tier differs by database</span> : <EffDbs all={t.allDatabases} dbs={t.databases} />}</span>
        {!split && <TierBadge tier={t.tier} sm />}
        {t.enabled === false && <span className="qh-eff-off" title="Nobody reaches a disabled connection, whatever they are granted, until it is enabled again.">connection disabled</span>}
        <EffSource t={t} />
        {t.source === 'admin_or_bypass' ? <span className="qh-eff-ends is-open">While admin</span> : !split && <EffEnds iso={t.expiresAt} />}
      </div>
      {split && <div className="qh-eff-per">{t.perDatabase.map(d => (
        <div key={d.database || '*'} className="qh-eff-per-row">
          <span className="qh-eff-dbs"><EffDbs all={!d.database} dbs={d.database ? [d.database] : null} /></span>
          <TierBadge tier={d.tier} sm />
          <EffEnds iso={d.expiresAt} />
        </div>))}</div>}
      {notes && notes.length > 0 && <div className="qh-eff-notes">{notes.map((n, i) => <div key={i} className="qh-eff-note">{n}</div>)}</div>}
    </div>
  );
}

// Auto-approve reads as a hole in the review, not as another grant: the sentence
// is about what happens WITHOUT a human, and the styling is the warning ramp.
function EffAutoRow({ a, own }) {
  return (
    <div className="qh-eff-row is-auto">
      <div className="qh-eff-row-main">
        <span className="qh-eff-auto-ic"><EffIcon.bolt /></span>
        <span className="qh-eff-auto-say">{a.tier === 'RO' ? 'Reads' : 'Queries up to ' + a.tier} on <b className="qh-mono">{a.allTargets ? 'every connection' : a.connectionId}</b>
          {!a.allTargets && <> · <span className="qh-mono">{a.databaseId || 'all databases'}</span></>} run without a DBA</span>
        {a.via && <span className="qh-eff-src is-team">Team · {String(a.via).replace(/ \(team\)$/, '')}</span>}
        {!a.via && own && <span className="qh-eff-src">{own}</span>}
        <EffEnds iso={a.expiresAt} />
      </div>
    </div>
  );
}

function EffSection({ title, sub, children, tone }) {
  return (
    <section className={'qh-eff-sec' + (tone ? ' is-' + tone : '')}>
      <div className="qh-eff-sec-h"><div className="qh-eff-sec-t">{title}</div>{sub && <div className="qh-eff-sec-sub">{sub}</div>}</div>
      {children}
    </section>
  );
}

function EffSkeleton() {
  return <div className="qh-eff-skel" aria-busy="true" aria-label="Resolving access">{[0, 1, 2].map(i => <div key={i} className="qh-eff-skel-row" />)}</div>;
}

// "Could not resolve" — worded so it cannot be mistaken for an answer.
function EffFailed({ who, onRetry }) {
  return (
    <div className="qh-eff-state is-failed" role="alert">
      <EffIcon.warn />
      <div><div className="qh-eff-state-t">Couldn't resolve {who}'s access</div>
        <div className="qh-eff-state-p">This says nothing about what they can reach. Try again before acting on it — neither granting nor dismissing is safe from here.</div></div>
      <button className="qh-btn qh-btn-sm" onClick={onRetry}>Try again</button>
    </div>
  );
}

// "Resolved: nothing" — a fact, stated with its three reasons.
function EffNone({ team }) {
  return (
    <div className="qh-eff-state is-none">
      <EffIcon.none />
      <div><div className="qh-eff-state-t">{team ? 'This team grants no access' : 'No access anywhere'}</div>
        <div className="qh-eff-state-p">{team
          ? 'No live team grant on any connection. Members reach only what they hold personally.'
          : 'No own grant, no team grant, no admin role. Every query they submit would be refused.'}</div></div>
    </div>
  );
}

function effApproverSentence(ap) {
  if (ap.superAdmin) return <>Super-admin — approves <b>anything, anywhere</b>, and their own queries skip review.</>;
  const teams = ap.scopeTeamsAll ? <b>everyone</b> : (ap.scopeTeams && ap.scopeTeams.length ? <b>{ap.scopeTeams.join(', ')}</b> : null);
  const targets = ap.scopeTargetsAll ? <b>every connection</b> : (ap.scopeTargets && ap.scopeTargets.length ? <b>{ap.scopeTargets.join(', ')}</b> : null);
  if (!teams || !targets) return <>Holds an approver scope that <b>approves nothing</b> — {!teams ? 'no team' : 'no connection'} is in it.</>;
  return <>Approves requests from {teams} on {targets}, up to <b>{ap.maxTier || 'any tier'}</b>.</>;
}

// ---------- A person ----------
function EffPerson({ st, subject, onOpenTeam }) {
  const [eff, setEff] = useEff(null);
  const [failed, setFailed] = useEff(false);
  const load = React.useCallback(() => {
    setFailed(false); setEff(null);
    st.effectiveAccess(subject.id).then(setEff).catch(() => setFailed(true));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subject.id]);
  React.useEffect(() => { load(); }, [load]);

  const name = qhPersonName(subject.name);
  const access = (eff && eff.access) || [];
  const autos = (eff && eff.autoApprove) || [];
  const ap = eff && eff.admin;
  const cap = eff && eff.rowLimitOverride && eff.rowLimitOverride.maxRows;
  const bypassAll = ap && ap.superAdmin && access.length > 0 && access.every(t => t.source === 'admin_or_bypass');
  const order = { user: 0, team: 1, admin_or_bypass: 2 };
  const rows = access.slice().sort((a, b) => (a.connectionId < b.connectionId ? -1 : a.connectionId > b.connectionId ? 1 : 0) || (order[a.source] - order[b.source]));
  // A connection held at two tiers arrives as two rows (CODE 2026-09-23 §4),
  // so the count is of CONNECTIONS, not rows.
  const nConn = new Set(access.map(t => t.connectionId)).size;
  const ids = [subject.handle, subject.slackId, subject.id].filter(Boolean);
  const blocked = eff && !bypassAll ? effBlocked(st, eff, ids) : [];
  const soon = rows.concat(autos).filter(t => t.expiresAt && (new Date(t.expiresAt) - Date.now()) / 86400000 <= 14 && new Date(t.expiresAt) > Date.now()).length;

  return (
    <div className="qh-eff-body">
      <div className="qh-eff-head">
        <span className="qh-peravatar lg">{subject.initials || '?'}</span>
        <div className="qh-eff-head-main">
          <div className="qh-eff-head-name">{name}
            {subject.enabled === false && <span className="qh-perkind is-off">disabled — nothing below was revoked</span>}
            {eff && eff.known === false && <span className="qh-perkind">no QueryHub account</span>}</div>
          <div className="qh-eff-head-id">{subject.handle}{subject.slackId ? ' · ' + subject.slackId : ''}</div>
        </div>
        {eff && (eff.teams || []).length > 0 && (
          <div className="qh-eff-head-teams"><span className="qh-eff-head-k">In</span>
            {eff.teams.map(t => <button key={t.id || t.name} className="qh-eff-teamlink" onClick={() => onOpenTeam(t)}>{t.name}</button>)}</div>
        )}
      </div>

      {!eff && !failed && <EffSkeleton />}
      {failed && <EffFailed who={name} onRetry={load} />}
      {eff && (
        <>
          {/* One line a reader can stop at. Counts only — the names are below. */}
          <div className="qh-eff-sum">
            {bypassAll ? 'Reaches every connection as an admin' : nConn ? 'Can query ' + nConn + ' connection' + (nConn === 1 ? '' : 's') : 'Can query nothing'}
            {autos.length > 0 && <> · <b className="qh-eff-sum-warn">{autos.length} skip{autos.length === 1 ? 's' : ''} review</b></>}
            {soon > 0 && <> · <b className="qh-eff-sum-soon">{soon} end{soon === 1 ? 's' : ''} within 14 days</b></>}
            {ap && <> · approver</>}
          </div>

          <EffSection title="Can query" sub="Per database, their own grant beats a team grant — and still does once it has ended, as no access. An admin needs neither.">
            {access.length === 0 && autos.length === 0 && !ap && !blocked.length && <EffNone />}
            {access.length === 0 && (autos.length > 0 || ap || blocked.length > 0) && <div className="qh-eff-empty">No grant on any connection.</div>}
            {bypassAll
              ? <div className="qh-eff-row"><div className="qh-eff-row-main"><span className="qh-eff-conn">Every connection</span><span className="qh-eff-dbs"><EffDbs all /></span><TierBadge tier="DDL" sm /><span className="qh-eff-src is-admin">Admin bypass</span><span className="qh-eff-ends is-open">While admin</span></div>
                  <div className="qh-eff-notes"><div className="qh-eff-note">No grant is consulted for a super-admin, so none is listed. Their own grants, if any, change nothing.</div></div></div>
              : <div className="qh-eff-list">
                  {rows.map(t => <EffAccessRow key={t.key || t.connectionId + ':' + t.source + ':' + t.tier} t={t} notes={effOverruled(st, eff, t)} />)}
                  {blocked.map(b => <EffBlockedRow key={b.g.id} g={b.g} team={b.team} />)}
                </div>}
          </EffSection>

          <EffSection title="Skips review" tone="warn" sub="Queries matching these run without anyone looking. Every run is still in the audit log.">
            {autos.length
              ? <div className="qh-eff-list">{autos.map((a, i) => <EffAutoRow key={i} a={a} own="Own exemption" />)}</div>
              : <div className="qh-eff-empty">Nothing — every query they submit is reviewed.</div>}
          </EffSection>

          <EffSection title="As an approver" sub="Approval is team × connection × tier. It has no database dimension.">
            {ap ? (
              <div className="qh-eff-appr">
                <div className="qh-eff-appr-say"><EffIcon.shield />{effApproverSentence(ap)}</div>
                <div className="qh-eff-appr-facts">
                  <span className={'qh-eff-fact' + (ap.canGrant ? ' is-on' : '')}>{ap.canGrant ? 'Can grant access to others' : 'Cannot grant access'}</span>
                  {cap != null && <span className="qh-eff-fact">Result cap {cap.toLocaleString('en-GB')} rows</span>}
                </div>
              </div>
            ) : <div className="qh-eff-empty">Not an approver.{cap != null ? ' Result cap ' + cap.toLocaleString('en-GB') + ' rows (personal override).' : ''}</div>}
          </EffSection>
        </>
      )}
    </div>
  );
}

// ---------- A team ----------
function EffTeam({ st, subject, onOpenPerson }) {
  const [eff, setEff] = useEff(null);
  const [failed, setFailed] = useEff(false);
  const load = React.useCallback(() => {
    setFailed(false); setEff(null);
    st.teamEffectiveAccess(subject.id).then(setEff).catch(() => setFailed(true));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subject.id]);
  React.useEffect(() => { load(); }, [load]);
  const access = (eff && eff.access) || [];
  const autos = (eff && eff.autoApprove) || [];
  const members = (eff && eff.members) || [];
  const approvers = (eff && eff.approvers) || [];
  const synced = eff && eff.team && eff.team.syncedFrom;
  // One row per connection here, but count distinct anyway: the person payload
  // already repeats `connectionId`, and a count that depends on which does is
  // a count that will be wrong the day this one does too.
  const nConn = new Set(access.map(t => t.connectionId)).size;

  return (
    <div className="qh-eff-body">
      <div className="qh-eff-head">
        <span className="qh-peravatar lg is-team">{subject.name.slice(0, 2).toUpperCase()}</span>
        <div className="qh-eff-head-main">
          <div className="qh-eff-head-name">{subject.name}{synced && <span className="qh-perkind" title={'The pod sync owns this team’s membership: ' + synced}>synced</span>}</div>
          <div className="qh-eff-head-id">{subject.desc || 'Team'} · {subject.members.length} member{subject.members.length === 1 ? '' : 's'}</div>
        </div>
      </div>

      {!eff && !failed && <EffSkeleton />}
      {failed && <EffFailed who={subject.name} onRetry={load} />}
      {eff && (
        <>
          <div className="qh-eff-sum">
            {nConn ? 'Members can query ' + nConn + ' connection' + (nConn === 1 ? '' : 's') + ' through this team' : 'Grants nothing'}
            {autos.length > 0 && <> · <b className="qh-eff-sum-warn">{autos.length} skip{autos.length === 1 ? 's' : ''} review</b></>}
          </div>

          <EffSection title="Members can query" sub="What this team gives every member — except where a member holds their own grant on one of these databases. Theirs decides for them, and still does once it has ended.">
            {access.length === 0
              ? <EffNone team />
              : <div className="qh-eff-list">{access.map(t => (
                <EffAccessRow key={t.key || t.connectionId} t={t} notes={(t.overriddenFor || []).map(o => {
                  const on = o.databases && o.databases.indexOf('*') < 0 ? ' on ' + o.databases.join(', ') : '';
                  const who = <button className="qh-eff-inlink" onClick={() => onOpenPerson(o.handle)}>{qhPersonName(o.name)}</button>;
                  // CODE's rule 2, in the words CODE asked for: an ended own grant
                  // is NOT dropped from this list — it is the reason they have nothing.
                  return o.expired
                    ? <span className="qh-eff-note-warn">For {who}, their own grant{on} ended on {effDate(o.expiresAt)}; the team's does not apply to them.</span>
                    : <>For {who} their own {o.tier} grant{on} applies instead.</>;
                })} />))}</div>}
          </EffSection>

          <EffSection title="Skips review" tone="warn" sub="Every member's matching queries run without anyone looking.">
            {autos.length
              ? <div className="qh-eff-list">{autos.map((a, i) => <EffAutoRow key={i} a={a} own="Team exemption" />)}</div>
              : <div className="qh-eff-empty">Nothing — every member's query is reviewed.</div>}
          </EffSection>

          <EffSection title="Who approves their requests" sub="Approvers scoped to this team, plus every super-admin.">
            <div className="qh-eff-list">
              {approvers.map(a => (
                <div key={a.handle} className="qh-eff-row"><div className="qh-eff-row-main">
                  <button className="qh-eff-inlink is-strong" onClick={() => onOpenPerson(a.handle)}>{qhPersonName(a.name)}</button>
                  <span className="qh-eff-dbs">on {a.scopeTargetsAll ? 'every connection' : a.scopeTargets.join(', ')}</span>
                  <span className="qh-eff-appr-tier">up to {a.maxTier || 'any tier'}</span>
                </div></div>))}
              <div className="qh-eff-empty">{approvers.length ? 'And' : 'No approver is scoped to this team — only'} {eff.superApprovers} super-admin{eff.superApprovers === 1 ? '' : 's'}.</div>
            </div>
          </EffSection>

          <EffSection title={'Members · ' + members.length} sub={synced ? 'Membership comes from ' + synced + '.' : null}>
            {members.length === 0 ? <div className="qh-eff-empty">Nobody is in this team, so what it grants reaches nobody.</div> : (
              <div className="qh-eff-members">{members.map(m => (
                <button key={m.handle} className={'qh-eff-member' + (m.enabled ? '' : ' is-off')} onClick={() => onOpenPerson(m.handle)}>
                  <span className="qh-eff-member-n">{qhPersonName(m.name)}</span><span className="qh-eff-member-h">{m.handle}</span><EffIcon.arrow />
                </button>))}</div>
            )}
          </EffSection>
        </>
      )}
    </div>
  );
}

// ---------- The screen ----------
function EffectiveAccessView({ st }) {
  const [kind, setKind] = useEff('person');
  const [q, setQ] = useEff('');
  const [pick, setPick] = useEff(null);   // { kind, key }
  const people = st.people || [];
  const teams = st.teams || [];
  const t = q.trim().toLowerCase();
  const pList = people.filter(p => !t || [p.name, p.handle, p.slackId, p.id].filter(Boolean).join(' ').toLowerCase().includes(t));
  const tList = teams.filter(x => !t || (x.name + ' ' + (x.desc || '')).toLowerCase().includes(t));
  const openPerson = (h) => { setKind('person'); setPick({ kind: 'person', key: h }); };
  const openTeam = (tm) => { setKind('team'); setPick({ kind: 'team', key: tm.id || tm.name }); };
  const person = pick && pick.kind === 'person' ? people.find(p => p.handle === pick.key || p.id === pick.key) : null;
  const team = pick && pick.kind === 'team' ? teams.find(x => x.id === pick.key || x.name === pick.key) : null;

  return (
    <div className="qh-eff">
      <aside className="qh-eff-pick">
        <div className="qh-eff-pick-top">
          <div className="qh-aview-title">Effective access</div>
          <div className="qh-aview-sub">What a person or a team can actually reach, and why.</div>
          <div className="qh-seg qh-seg-sm qh-eff-kind">
            <button className={'qh-seg-opt' + (kind === 'person' ? ' is-active' : '')} onClick={() => setKind('person')}>People · {people.length}</button>
            <button className={'qh-seg-opt' + (kind === 'team' ? ' is-active' : '')} onClick={() => setKind('team')}>Teams · {teams.length}</button>
          </div>
          <div className="qh-search sm">
            <span className="qh-search-ic"><EffIcon.search /></span>
            <input className="qh-search-in" placeholder={kind === 'person' ? 'Name, handle or Slack id' : 'Team name'} value={q} onChange={e => setQ(e.target.value)} />
          </div>
        </div>
        <div className="qh-eff-pick-list">
          {kind === 'person' ? pList.map(p => (
            <button key={p.handle} className={'qh-eff-pick-row' + (person && person.handle === p.handle ? ' is-on' : '') + (p.enabled === false ? ' is-off' : '')} onClick={() => openPerson(p.handle)}>
              <span className="qh-peravatar sm">{p.initials}</span>
              <span className="qh-eff-pick-main"><span className="qh-eff-pick-n">{qhPersonName(p.name)}</span><span className="qh-eff-pick-h">{p.handle}</span></span>
              {p.enabled === false && <span className="qh-perkind is-off">disabled</span>}
            </button>
          )) : tList.map(x => (
            <button key={x.id} className={'qh-eff-pick-row' + (team && team.id === x.id ? ' is-on' : '')} onClick={() => openTeam(x)}>
              <span className="qh-peravatar sm is-team">{x.name.slice(0, 2).toUpperCase()}</span>
              <span className="qh-eff-pick-main"><span className="qh-eff-pick-n">{x.name}</span><span className="qh-eff-pick-h">{x.members.length} member{x.members.length === 1 ? '' : 's'}</span></span>
            </button>
          ))}
          {(kind === 'person' ? pList : tList).length === 0 && <div className="qh-eff-empty">Nobody matches “{q.trim()}”.</div>}
        </div>
      </aside>
      <div className="qh-eff-main">
        {person ? <EffPerson key={'p' + person.handle} st={st} subject={person} onOpenTeam={openTeam} />
          : team ? <EffTeam key={'t' + team.id} st={st} subject={team} onOpenPerson={openPerson} />
          : <div className="qh-eff-intro"><div className="qh-eff-intro-t">Pick someone on the left</div>
              <div className="qh-eff-intro-p">Every connection and database they can query, where each one comes from, when it ends, what skips review, and whether they approve anything — resolved the way a submission resolves it.</div></div>}
      </div>
    </div>
  );
}

Object.assign(window, { EffectiveAccessView });
