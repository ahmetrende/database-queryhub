// QueryHub — welcome / home landing. Shown on sign-in and when the logo is
// clicked or all tabs are closed. Lists the current workspace, saved sessions,
// saved queries and recent history, with quick actions to start work.

const QH_HOME_ICN = {
  plus: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 5v14M5 12h14"/></svg>,
  save: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M19 21H5a2 2 0 01-2-2V5a2 2 0 012-2h11l5 5v11a2 2 0 01-2 2z"/><path d="M17 21v-8H7v8M7 3v5h8"/></svg>,
  browse: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="18" height="5" rx="1.5"/><rect x="3" y="15" width="18" height="5" rx="1.5"/><path d="M7 9v4M7 13h6v2"/></svg>,
  trash: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18M8 6V4a1 1 0 011-1h6a1 1 0 011 1v2M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6M10 11v6M14 11v6"/></svg>,
  arrow: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>,
};

function HomeCard({ title, count, action, children }) {
  return (
    <section className="qh-home-card">
      <div className="qh-home-card-head">
        <h2 className="qh-home-card-title">{title}{count != null && <span className="qh-home-card-count">{count}</span>}</h2>
        {action}
      </div>
      <div className="qh-home-card-body">{children}</div>
    </section>
  );
}

function HomeEmpty({ text }) {
  return <div className="qh-home-empty">{text}</div>;
}

function HomeScreen({ user, openTabs, slackEnabled, onFocusTab, onNewQuery, onSaveSession, sessions, onRestoreSession, onDeleteSession, scheduled, onOpenScheduled, onCancelScheduled, history, onLoadHistory, saved, onLoadSaved, onDeleteSaved, onBrowse, onWhatsNew, unseenNews }) {
  const first = (user && user.name ? user.name.split(' ')[0] : 'there');
  const nonEmpty = (openTabs || []).filter(t => t.sql && t.sql.trim());

  return (
    <div className="qh-home">
      <div className="qh-home-inner">
        <div className="qh-home-hero">
          <QHMark size={44} variant="green" radius={0.3} />
          <div className="qh-home-hero-text">
            <h1 className="qh-home-title">Welcome back, {first}</h1>
            <p className="qh-home-sub">Pick up where you left off, or start something new. {slackEnabled ? 'Approvals still run in Slack.' : 'Approvals run in the admin panel.'}</p>
          </div>
          <div className="qh-home-actions">
            <button className="qh-btn qh-btn-primary qh-btn-lg" onClick={onNewQuery}>{QH_HOME_ICN.plus}New query</button>
            <button className="qh-btn qh-btn-ghost qh-btn-lg" onClick={onSaveSession} disabled={!nonEmpty.length} title={nonEmpty.length ? 'Save all open tabs as a session' : 'No open tabs to save yet'}>{QH_HOME_ICN.save}Save workspace</button>
            <button className="qh-btn qh-btn-ghost qh-btn-lg" onClick={onBrowse}>{QH_HOME_ICN.browse}Browse connections</button>
          </div>
        </div>

        <div className="qh-home-grid">
          <HomeCard title="Current session" count={openTabs.length}
            action={<button className="qh-home-link" onClick={() => onFocusTab((openTabs[0] || {}).id)}>Open editor {QH_HOME_ICN.arrow}</button>}>
            {openTabs.length === 0 ? <HomeEmpty text="No open tabs. Start a new query above." /> : (
              <div className="qh-home-list">
                {openTabs.map(t => (
                  <button key={t.id} className="qh-home-row" onClick={() => onFocusTab(t.id)}>
                    <span className={'qh-tab-dot tier-' + t.tier.toLowerCase()} />
                    <span className="qh-home-row-name">{t.name}{t.dirty ? ' •' : ''}</span>
                    <span className="qh-home-row-meta">{t.conn} · {t.db}</span>
                  </button>
                ))}
              </div>
            )}
          </HomeCard>

          <HomeCard title="Saved sessions" count={sessions.length}
            action={<button className="qh-home-link" onClick={onSaveSession} disabled={!nonEmpty.length}>Save current {QH_HOME_ICN.arrow}</button>}>
            {sessions.length === 0 ? <HomeEmpty text="No saved sessions. Save your open tabs as one named workspace to restore later — here or on another device." /> : (
              <div className="qh-home-list">
                {sessions.map(s => (
                  <div key={s.id} className="qh-home-row is-linky" onClick={() => onRestoreSession(s)} title="Open all tabs from this workspace">
                    <span className="qh-home-row-name">{s.name}</span>
                    <span className="qh-home-row-meta"><OriginBadge dest={s.dest} /><span>{s.tabs.length} tab{s.tabs.length > 1 ? 's' : ''} · {qhAgo(s.savedAt)}</span></span>
                    <button className="qh-row-del" onClick={(e) => { e.stopPropagation(); onDeleteSession(s.id); }} title="Delete" aria-label="Delete session">{QH_HOME_ICN.trash}</button>
                  </div>
                ))}
              </div>
            )}
          </HomeCard>

          <HomeCard title="Recent history" count={history.length}>
            {history.length === 0 ? <HomeEmpty text="Your recent queries will show up here." /> : (
              <div className="qh-home-list">
                {history.map(h => (
                  <button key={h.id} className="qh-home-row is-hist" onClick={() => onLoadHistory(h)}>
                    <span className="qh-home-histtop"><TierBadge tier={h.tier} sm /><StatusPill status={h.status} /><span className="qh-home-when">{h.when}</span></span>
                    <span className="qh-home-histsql">{h.sql}</span>
                    <span className="qh-home-row-meta">{h.conn} · {h.db}</span>
                  </button>
                ))}
              </div>
            )}
          </HomeCard>

          <HomeCard title="Saved queries" count={saved.length}>
            {saved.length === 0 ? <HomeEmpty text="Save queries from a tab's right-click menu to build a personal library." /> : (
              <div className="qh-home-list">
                {saved.map(s => (
                  <div key={s.id} className="qh-home-row is-linky" onClick={() => onLoadSaved(s)} title="Open in a new tab">
                    <span className="qh-home-row-name">{s.name}</span>
                    <span className="qh-home-row-meta"><OriginBadge dest={s.dest} /><span>{s.conn} · {s.db}</span></span>
                    <button className="qh-row-del" onClick={(e) => { e.stopPropagation(); onDeleteSaved(s.id); }} title="Delete" aria-label="Delete saved query">{QH_HOME_ICN.trash}</button>
                  </div>
                ))}
              </div>
            )}
          </HomeCard>

          <HomeCard title="Scheduled" count={(scheduled || []).length}>
            {(scheduled || []).length === 0 ? <HomeEmpty text="Queries you schedule (a preset or a custom date & time) show up here until they run." /> : (
              <div className="qh-home-list">
                {scheduled.map(s => (
                  <div key={s.id} className="qh-home-row is-linky" onClick={() => onOpenScheduled(s)} title="Open this query in a new tab">
                    <span className="qh-home-row-name">{s.name}</span>
                    <span className="qh-home-row-meta"><span className="qh-sched-chip"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>{s.when}</span><span>{s.conn} · {s.db}</span></span>
                    <button className="qh-row-del" onClick={(e) => { e.stopPropagation(); onCancelScheduled(s.id); }} title="Cancel schedule" aria-label="Cancel schedule">{QH_HOME_ICN.trash}</button>
                  </div>
                ))}
              </div>
            )}
          </HomeCard>
        </div>

        <footer className="qh-home-foot">
          <span className="qh-home-build">
            <span className="qh-mono">{(window.QH_BUILD && window.QH_BUILD.version) || window.QH_VERSION}</span>
            {window.QH_BUILD && window.QH_BUILD.date && <> · {window.QH_BUILD.date}</>}
            {window.QH_BUILD && window.QH_BUILD.sha && (qhCommitUrl(window.QH_BUILD.sha)
              ? <> · <a className="qh-home-sha qh-mono" href={qhCommitUrl(window.QH_BUILD.sha)} target="_blank" rel="noopener noreferrer" title="View this build on GitHub">{window.QH_BUILD.sha}</a></>
              : <> · <span className="qh-home-sha qh-mono">{window.QH_BUILD.sha}</span></>)}
          </span>
          <button className="qh-home-link" onClick={onWhatsNew}>What's new{unseenNews && <span className="qh-user-newpill">New</span>} {QH_HOME_ICN.arrow}</button>
        </footer>
      </div>
    </div>
  );
}

Object.assign(window, { HomeScreen });
