// QueryHub — "What's new" page. Opened from the profile menu (avatar → What's new)
// or the build stamp. Renders the changelog newest-first: each entry is one
// curated, user-facing change (headline + one-line lede + short bullet points).
// Entries are written by hand, not derived from commits — the page used to say
// otherwise in two places, which was worth fixing because it told readers the
// list was exhaustive when it is deliberately a selection.
// Real-API: the list comes from GET /changelog (qhApi.changelog); QH_BUILD +
// qhCommitUrl come from qh-data.jsx (build-injected). Design used a mock global.

const QH_WN_GH = <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>;
const QH_WN_TAG = { new: 'New', improved: 'Improved', fixed: 'Fixed', changed: 'Changed' };

function WhatsNewRelease({ rel, latest }) {
  const [open, setOpen] = React.useState(false);
  const shaUrl = qhCommitUrl(rel.sha);
  return (
    <li className="qh-wn-rel">
      <div className="qh-wn-rail"><span className={'qh-wn-node' + (latest ? ' is-latest' : '')} /></div>
      <div className="qh-wn-card">
        <div className="qh-wn-relhead">
          {rel.version && <span className="qh-wn-relver">{rel.version}</span>}
          {latest && <span className="qh-wn-latest">Latest</span>}
          {rel.area && <span className="qh-wn-relarea">{rel.area}</span>}
          <span className="qh-wn-spacer" />
          <span className="qh-wn-reldate">{rel.date}</span>
          {rel.sha && (shaUrl
            ? <a className="qh-wn-sha" href={shaUrl} target="_blank" rel="noopener noreferrer" title={'View ' + rel.sha + ' on GitHub'}>{QH_WN_GH}<span className="qh-mono">{rel.sha}</span></a>
            : <span className="qh-wn-sha is-plain">{QH_WN_GH}<span className="qh-mono">{rel.sha}</span></span>)}
        </div>
        <h2 className="qh-wn-relhl">{rel.headline}</h2>
        <p className="qh-wn-relsum">{rel.summary}</p>
        {(rel.changes || []).length > 0 && (
          <ul className="qh-wn-changes">
            {rel.changes.map((c, i) => (
              <li key={i} className="qh-wn-change">
                <span className={'qh-wn-ctag t-' + c.type}>{QH_WN_TAG[c.type] || c.type}</span>
                <span className="qh-wn-ctext">{c.text}</span>
              </li>
            ))}
          </ul>
        )}
        {rel.commits && rel.commits.length > 0 && (
          <>
            <button className="qh-wn-cmtoggle" onClick={() => setOpen(o => !o)} aria-expanded={open}>
              <svg className={'qh-wn-caret' + (open ? ' is-open' : '')} width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><path d="M9 6l6 6-6 6"/></svg>
              {(open ? 'Hide ' : 'Show ') + rel.commits.length + ' commit' + (rel.commits.length > 1 ? 's' : '')}
            </button>
            {open && (
              <ul className="qh-wn-commits">
                {rel.commits.map(c => {
                  const u = qhCommitUrl(c.sha);
                  return (
                    <li key={c.sha} className="qh-wn-commit">
                      {u
                        ? <a className="qh-wn-csha" href={u} target="_blank" rel="noopener noreferrer" title="View on GitHub">{QH_WN_GH}<span className="qh-mono">{c.sha}</span></a>
                        : <span className="qh-wn-csha is-plain">{QH_WN_GH}<span className="qh-mono">{c.sha}</span></span>}
                      <span className="qh-wn-cmsg">{c.msg}</span>
                    </li>
                  );
                })}
              </ul>
            )}
          </>
        )}
      </div>
    </li>
  );
}

function WhatsNew() {
  // Real data: GET /changelog — the curated changelog file, re-read whenever it
  // changes. Approver-only entries come back only for admins.
  const [releases, setReleases] = React.useState(null);
  React.useEffect(() => {
    let alive = true;
    qhApi.changelog()
      .then(r => { if (alive) setReleases((r && r.releases) || []); })
      .catch(() => { if (alive) setReleases([]); });
    return () => { alive = false; };
  }, []);
  const build = (typeof window !== 'undefined' && window.QH_BUILD) || {};
  const buildShaUrl = qhCommitUrl(build.sha);
  // Approver-only entries — how the decision queue behaves, what an admin
  // screen gained — are hidden by DEFAULT even for admins. An admin is still a
  // reader of this page, and the queue-ordering entries were crowding out the
  // ones that concern everybody. The server only sends them to admins at all,
  // so for a requester this toggle never appears.
  const [showApprover, setShowApprover] = React.useState(false);
  const all = releases || [];
  const approverCount = all.filter(r => r.audience === 'approver').length;
  const list = showApprover ? all : all.filter(r => r.audience !== 'approver');
  return (
    <div className="qh-wn">
      <div className="qh-wn-inner">
        <header className="qh-wn-hero">
          <span className="qh-wn-hero-icn" aria-hidden="true">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12 3l1.9 3.9 4.3.6-3.1 3 .7 4.3-3.8-2-3.8 2 .7-4.3-3.1-3 4.3-.6z"/></svg>
          </span>
          <div className="qh-wn-hero-text">
            <h1 className="qh-wn-title">What's new</h1>
            <p className="qh-wn-sub">What changed in QueryHub, newest first.</p>
            {approverCount > 0 && (
              <label className="qh-wn-audtoggle">
                <input type="checkbox" checked={showApprover}
                       onChange={(e) => setShowApprover(e.target.checked)} />
                <span>Also show {approverCount} approver-only update{approverCount === 1 ? '' : 's'}</span>
              </label>
            )}
          </div>
          <div className="qh-wn-build">
            <div className="qh-wn-build-ver">{build.version || ''}</div>
            <div className="qh-wn-build-meta">
              {build.date && <span title="Build time">{build.date}</span>}
              {build.date && build.sha && <span className="qh-wn-dot">·</span>}
              {build.sha && (buildShaUrl
                ? <a className="qh-wn-sha" href={buildShaUrl} target="_blank" rel="noopener noreferrer" title={'Build ' + build.sha + ' on GitHub'}>{QH_WN_GH}<span className="qh-mono">{build.sha}</span></a>
                : <span className="qh-wn-sha is-plain">{QH_WN_GH}<span className="qh-mono">{build.sha}</span></span>)}
            </div>
          </div>
        </header>
        {releases === null ? (
          <div className="qh-wn-foot">Loading…</div>
        ) : list.length === 0 ? (
          <div className="qh-wn-foot">No releases yet.</div>
        ) : (
          <ol className="qh-wn-feed">
            {list.map((r, i) => <WhatsNewRelease key={r.sha + '-' + i} rel={r} latest={i === 0} />)}
          </ol>
        )}
        <p className="qh-wn-foot">One entry per user-facing change, written by hand.{build.repo ? <> Commit hashes link to <span className="qh-mono">github.com/{build.repo}</span>.</> : null}</p>
      </div>
    </div>
  );
}

Object.assign(window, { WhatsNew });
