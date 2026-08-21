// QueryHub Admin — one person's access, resolved (CODE brief 2026-08-20 §6).
//
// PERSON-centric on purpose (CODE's caveat A): teams are being replaced by pods,
// so a screen built around a team picker would be rebuilt in a few weeks.
// Everything here is keyed to a person; team membership appears only as the
// SOURCE of a grant — a label, not a control. That is the loosely coupled part,
// and the only thing a pod migration has to touch.
//
// The operator's complaint was that picking a person shows nowhere they already
// stand, so adding one grant means working around what you cannot see. Three
// blocks, in that order: where they stand today (resolved by the same resolver a
// submission uses, so it cannot disagree with Run), their own editable grants,
// and "give them what someone else has".
const { useState: usePer } = React;

const PerIcon = {
  back: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M19 12H5M12 19l-7-7 7-7" /></svg>,
  search: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="7" /><path d="M21 21l-4.3-4.3" /></svg>,
  copy: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="12" height="12" rx="2" /><path d="M5 15V5a2 2 0 012-2h8" /></svg>,
  edit: () => <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z" /></svg>,
};

// What a resolved target reads as. `allDatabases` is the server's word for it;
// spelled out, because `conn/*` reads like a path that exists.
function perScope(t) {
  if (t.allDatabases) return 'all databases';
  const dbs = t.databases || [];
  return dbs.length ? dbs.join(', ') : 'all databases';
}

// WHY they have it. `source` has three values: their own row, a team's row, or
// nothing at all — an admin reaches everything without a grant anywhere
// (`admin_or_bypass`), and calling that "direct" would be true but not the truth.
// `sourceTeam` names ONE team: several can grant the same target and the resolver
// has already merged them into one decision, so the first by name is reported.
function perSource(t) {
  if (t.source === 'team') return { word: 'via ' + (t.sourceTeam || 'a team'), cls: ' is-team' };
  if (t.source === 'admin_or_bypass') return { word: 'as admin', cls: ' is-admin' };
  return { word: 'direct', cls: '' };
}

function PersonCard({ p, grants, onOpen }) {
  const own = (grants || []).filter(g => g.subjectType === 'user' && g.subject === p.handle).length;
  return (
    <button className="qh-percard" onClick={() => onOpen(p)}>
      <span className="qh-peravatar">{p.initials}</span>
      <span className="qh-percard-main">
        <span className="qh-percard-name">{p.name}</span>
        <span className="qh-percard-h">{p.handle}</span>
      </span>
      {/* Direct grants only — what a team adds needs the resolver, and one
          request per person to draw a list would be a hundred requests. */}
      <span className="qh-percard-n">{own ? own + ' direct' : 'none direct'}</span>
    </button>
  );
}

function PersonPage({ st, actor, person, onBack }) {
  const [eff, setEff] = usePer(null);
  const [failed, setFailed] = usePer(false);
  const [editing, setEditing] = usePer(false);
  const [src, setSrc] = usePer('');
  const [withTeams, setWithTeams] = usePer(false);
  const [tierOver, setTierOver] = usePer('');
  const [copying, setCopying] = usePer(false);

  const load = React.useCallback(() => {
    setFailed(false); setEff(null);
    st.effectiveAccess(person.id).then(setEff).catch(() => setFailed(true));
    // st is rebuilt on every admin reload; keying on the person is what matters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [person.id]);
  React.useEffect(() => { load(); }, [load]);

  const people = st.people || [];
  const srcPerson = people.find(p => p.id === src || p.handle === src) || null;
  const copy = () => {
    if (!srcPerson || copying) return;
    setCopying(true);
    Promise.resolve(st.copyAccess(person.id, { source: srcPerson.id, includeTeams: withTeams, tier: tierOver || null }))
      .then(() => { setSrc(''); setWithTeams(false); setTierOver(''); load(); })
      .finally(() => setCopying(false));
  };

  const ap = eff && eff.admin;
  const cap = eff && eff.rowLimitOverride && eff.rowLimitOverride.maxRows;
  const access = (eff && eff.access) || [];
  const autos = (eff && eff.autoApprove) || [];
  return (
    <div className="qh-perwrap">
      <div className="qh-perhead">
        <button className="qh-rowbtn" onClick={onBack}><PerIcon.back />Everyone</button>
        <span className="qh-peravatar lg">{person.initials}</span>
        <div className="qh-perhead-main">
          <div className="qh-perhead-name">{person.name}</div>
          <div className="qh-perhead-h">{person.handle}</div>
        </div>
        {eff && eff.teams.length > 0 && (
          <div className="qh-perteams">
            {eff.teams.map(t => <span key={t.id || t.name} className="qh-team-badge">{t.name}</span>)}
          </div>
        )}
      </div>

      {/* ---- 1. Where they stand today ---- */}
      <div className="qh-persec">
        <div className="qh-persec-h">
          <div className="qh-aview-title">Where they stand today</div>
          <div className="qh-aview-sub">Resolved the way a submission resolves it — their own grants, every grant of every team they are in, and anything they reach as an admin. Their own row wins over a team's, because it is usually the narrower, deliberate one.</div>
        </div>
        {failed && <div className="qh-conn-empty">Could not load resolved access.</div>}
        {!eff && !failed && <div className="qh-perload">Resolving…</div>}
        {eff && (
          <>
            {(ap || cap != null) && (
              <div className="qh-perappr">
                {ap && <span><span className="qh-perappr-k">Approver</span>{ap.superAdmin ? 'Super-admin — every tier, every target' : 'Can approve up to ' + (ap.maxTier || '—') + (ap.scopeTargets && ap.scopeTargets.length ? ' on ' + ap.scopeTargets.join(', ') : ' on every target')}</span>}
                {cap != null && <span className="qh-perappr-nw"><span className="qh-perappr-k">Row cap</span>{cap.toLocaleString()} rows</span>}
              </div>
            )}
            <div className="qh-perlist">
              {access.map(t => {
                const src = perSource(t);
                return (
                <div key={t.connectionId} className="qh-perrow">
                  <span className="qh-perrow-t">{t.connectionId}</span>
                  <span className="qh-perrow-dbs">{perScope(t)}</span>
                  <TierBadge tier={t.tier} sm />
                  {/* A grant they hold directly can be edited below; one that
                      comes from a team cannot, and saying which is which is the
                      difference between this panel and a list. */}
                  <span className={'qh-persrc' + src.cls}>{src.word}</span>
                  {t.expiresAt && <span className="qh-expiry is-soon">ends {String(t.expiresAt).slice(0, 10)}</span>}
                </div>
                );
              })}
              {access.length === 0 && <div className="qh-conn-empty">No access anywhere yet.</div>}
              {autos.map((a, i) => (
                <div key={'a' + i} className="qh-perrow is-auto">
                  <span className="qh-perrow-t">{a.allTargets ? 'every target' : a.connectionId}</span>
                  <span className="qh-perrow-dbs">{a.allTargets ? '' : (a.databaseId || 'all databases')}</span>
                  <TierBadge tier={a.tier} sm />
                  <span className="qh-persrc is-auto">auto-approve{a.via ? ' · via ' + a.via : ''}</span>
                  {a.expiresAt && <span className="qh-expiry">until {String(a.expiresAt).slice(0, 10)}</span>}
                </div>
              ))}
            </div>
          </>
        )}
      </div>

      {/* ---- 2. Their own grants ---- */}
      <div className="qh-persec">
        <div className="qh-persec-h">
          <div className="qh-aview-title">Their own grants</div>
          <div className="qh-aview-sub">Only the rows written against this person. Anything reached through a team is changed on the team, not here.</div>
        </div>
        {editing
          ? <div className="qh-subjcard is-editing">
              <SubjectAccessEditor st={st} actor={actor} subjectType0="user" subject0={person.handle} name0={person.name} lockSubject
                onDone={() => { setEditing(false); load(); }} />
            </div>
          : (() => {
            const own = (st.grants || []).filter(g => g.subjectType === 'user' && g.subject === person.handle);
            return (
              <div className="qh-perown">
                <div className="qh-perlist">
                  {own.map(g => (
                    <div key={g.id} className="qh-perrow">
                      <span className="qh-perrow-t">{g.connectionId}</span>
                      <span className="qh-perrow-dbs">{qhGrantDbNames(g) || 'all databases'}</span>
                      <TierBadge tier={g.tier} sm />
                      {g.expiresAt && <span className="qh-expiry">ends {String(g.expiresAt).slice(0, 10)}</span>}
                    </div>
                  ))}
                  {own.length === 0 && <div className="qh-conn-empty">No grants written directly against this person.</div>}
                </div>
                <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={() => setEditing(true)}><PerIcon.edit />{own.length ? 'Edit their grants' : 'Grant access'}</button>
              </div>
            );
          })()}
      </div>

      {/* ---- 3. Copy from someone ---- */}
      <div className="qh-persec">
        <div className="qh-persec-h">
          <div className="qh-aview-title">Give them what someone else has</div>
          <div className="qh-aview-sub">Writes explicit per-user grants covering everything that person can reach — including what a team gives them, which is the part hand-copying loses.</div>
        </div>
        <div className="qh-percopy">
          <select className="qh-select" value={src} onChange={e => setSrc(e.target.value)}>
            <option value="">Copy from…</option>
            {people.filter(p => p.handle !== person.handle).map(p => <option key={p.id} value={p.id}>{p.name} · {p.handle}</option>)}
          </select>
          <select className="qh-select" value={tierOver} onChange={e => setTierOver(e.target.value)}>
            <option value="">Keep each tier</option>
            {['RO', 'RW', 'DDL'].map(t => <option key={t} value={t}>Force {t}</option>)}
          </select>
          {/* Off by default, and the label carries the part that outlives the
              click: joining a team also inherits whatever it is granted LATER. */}
          <label className="qh-percopy-tm">
            <input type="checkbox" checked={withTeams} onChange={e => setWithTeams(e.target.checked)} />
            Also add them to the same teams
          </label>
          <button className="qh-btn qh-btn-primary qh-btn-sm" disabled={!srcPerson || copying} onClick={copy}><PerIcon.copy />{copying ? 'Copying…' : 'Copy access'}</button>
        </div>
        {srcPerson && (
          <div className="qh-percopy-say">
            {withTeams
              ? <>{person.name} will join {srcPerson.name}'s teams and get {srcPerson.name}'s own grants, written against {person.handle}{tierOver ? ', all at ' + tierOver : ', at the tier they have there'}. Team access arrives through membership — <b>including anything those teams are granted later</b>.</>
              : <>{person.name} will get every connection {srcPerson.name} can reach, including through a team, as grants written against {person.handle}{tierOver ? ', all at ' + tierOver : ', at the tier they have there'}, without joining any team. Where {srcPerson.name} holds both a team grant and their own, their own wins — it is usually the narrower, deliberate one.</>}
            {' '}Existing grants on the same connection are replaced.
          </div>
        )}
      </div>
    </div>
  );
}

function PersonAccessView({ st, actor }) {
  const [pick, setPick] = usePer(null);
  const [q, setQ] = usePer('');
  const people = st.people || [];
  const person = pick ? (people.find(p => p.handle === pick) || null) : null;

  if (person) return <PersonPage st={st} actor={actor} person={person} onBack={() => setPick(null)} />;

  const t = q.trim().toLowerCase();
  const list = t ? people.filter(p => (p.name + ' ' + p.handle).toLowerCase().includes(t)) : people;
  return (
    <div>
      <div className="qh-conn-controls">
        <div className="qh-search sm">
          <span className="qh-search-ic"><PerIcon.search /></span>
          <input className="qh-search-in" placeholder="Find a person…" value={q} onChange={e => setQ(e.target.value)} />
        </div>
      </div>
      <div className="qh-percards">
        {list.map(p => <PersonCard key={p.id} p={p} grants={st.grants} onOpen={(x) => setPick(x.handle)} />)}
        {list.length === 0 && <div className="qh-conn-empty">Nobody matches that.</div>}
      </div>
    </div>
  );
}

Object.assign(window, { PersonAccessView });
