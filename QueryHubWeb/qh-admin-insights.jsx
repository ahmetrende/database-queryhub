// QueryHub Admin — insights views: Audit log, Metrics, Feedback.
const { useState: useIns } = React;

const QH_AUDIT_KINDS = {
  approve: { label: 'Approvals', color: 'var(--fg-accent)', bg: 'var(--brand-adaptive-light)' },
  reject:  { label: 'Rejections', color: 'var(--fg-danger)', bg: 'var(--danger-adaptive-light)' },
  changes: { label: 'Change requests', color: 'var(--fg-warning)', bg: 'var(--warning-adaptive-light)' },
  grant:   { label: 'Grants', color: '#15688C', bg: 'var(--sup-blue-light)' },
  auto:    { label: 'Auto-approve', color: 'var(--fg-accent)', bg: 'var(--brand-adaptive-light)' },
  scope:   { label: 'Scopes', color: 'var(--fg-secondary)', bg: 'var(--adaptive-medium)' },
  access:  { label: 'Access', color: '#6D4ACF', bg: 'var(--adaptive-medium)' },
};

// Human-readable execution duration from milliseconds.
function qhDurMs(ms) {
  if (ms == null) return '—';
  if (ms < 1000) return ms + 'ms';
  const s = ms / 1000;
  if (s < 60) return (s < 10 ? s.toFixed(1) : Math.round(s)) + 's';
  const m = Math.floor(s / 60), rs = Math.round(s % 60);
  return m + 'm' + (rs ? ' ' + rs + 's' : '');
}

// ---------- Audit log ----------
function AuditView2({ st }) {
  const [filter, setFilter] = useIns('all');
  const [q, setQ] = useIns('');
  // Search runs SERVER-SIDE over the whole audit_log (not just the recent
  // window loaded into st.audit), debounced. Empty search shows st.audit.
  const [remote, setRemote] = useIns(null);
  React.useEffect(() => {
    const term = q.trim();
    if (!term) { setRemote(null); return; }
    let alive = true;
    const h = setTimeout(() => {
      window.qhApi.adminAudit('?q=' + encodeURIComponent(term) + '&limit=300')
        .then(r => { if (alive) setRemote(r.audit || []); })
        .catch(() => { if (alive) setRemote([]); });
    }, 250);
    return () => { alive = false; clearTimeout(h); };
  }, [q]);
  const base = remote !== null ? remote : st.audit;
  const rows = base.filter(a => filter === 'all' || a.kind === filter);
  const chips = [['all', 'All'], ...Object.entries(QH_AUDIT_KINDS).map(([k, v]) => [k, v.label])];
  return (
    <div className="qh-apad">
      <div className="qh-aview-head"><div><div className="qh-aview-title">Audit log</div><div className="qh-aview-sub">Every admin action, immutable and attributed.</div></div></div>
      <div className="qh-audit-controls">
        <div className="qh-chips">
          {chips.map(([k, l]) => <button key={k} className={'qh-chip' + (filter === k ? ' is-active' : '')} onClick={() => setFilter(k)}>{l}</button>)}
        </div>
        <div className="qh-search sm">
          <svg className="qh-search-ic" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>
          <input className="qh-search-in" placeholder="Filter by actor, target…" value={q} onChange={e => setQ(e.target.value)} />
        </div>
      </div>
      <div className="qh-auditlog">
        <div className="qh-auditline is-head">
          <span />
          <span>Time</span>
          <span>Request</span>
          <span>By</span>
          <span>Action</span>
          <span>Requester · target</span>
          <span>Tier</span>
          <span className="qh-audit-r">Rows</span>
          <span className="qh-audit-r">Duration</span>
          <span>Query</span>
        </div>
        {rows.length === 0 && <div className="qh-aempty"><div>No matching entries.</div></div>}
        {rows.map(a => {
          const k = QH_AUDIT_KINDS[a.kind] || QH_AUDIT_KINDS.scope;
          const copy = () => { qhCopyText(a.query).then(ok => st.pushToast && st.pushToast(ok ? 'Query copied to clipboard.' : 'Could not copy — the browser blocked clipboard access.')); };
          return (
            <div key={a.id} className="qh-auditline">
              <span className="qh-auditdot" style={{ background: k.color }} />
              <span className="qh-auditwhen">{qhFmt(a.time)}</span>
              {/* The request id the requester saw in their tab, so the audit log
                  and the query screen share a visible key. Dash for entries with
                  no request behind them (grants, scopes, kill switch). */}
              <span className="qh-auditreq">{a.requestId ? '#' + a.requestId : '—'}</span>
              <span className="qh-auditactor">{a.actor}</span>
              <span className="qh-auditevent">{a.event}</span>
              <span className="qh-audittarget" title={a.target}>{a.target || '—'}</span>
              <span className="qh-audit-tier">{a.tier ? <TierBadge tier={a.tier} sm /> : ''}</span>
              <span className="qh-audit-r qh-audit-num">{a.rows != null ? Number(a.rows).toLocaleString() : '—'}</span>
              <span className="qh-audit-r qh-audit-num">{a.durationMs != null ? qhDurMs(a.durationMs) : '—'}</span>
              <span className="qh-auditquery">
                {a.query ? (
                  <>
                    <code className="qh-auditquery-sql" title={a.query}>{a.query}</code>
                    <button className="qh-auditquery-copy" onClick={copy} title="Copy full query" aria-label="Copy full query">
                      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
                    </button>
                  </>
                ) : <span className="qh-audit-dash">—</span>}
              </span>
              {a.info && <span className="qh-auditinfo" title={a.info}>{a.info}</span>}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ---------- Metrics ----------
function Stat({ k, v, sub }) {
  return <div className="qh-metric"><div className="qh-metric-v">{v}</div><div className="qh-metric-k">{k}</div>{sub && <div className="qh-metric-sub">{sub}</div>}</div>;
}

const _DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
function _fmtSec(s) {
  if (s == null) return '—';
  if (s < 1) return '0s';
  if (s < 120) return Math.round(s) + 's';
  if (s < 36000) return (s / 60).toFixed(1) + 'm';
  return (s / 3600).toFixed(1) + 'h';
}
function _fmtBytes(b) {
  if (b == null) return '—';
  const u = ['B', 'KB', 'MB', 'GB']; let n = b, i = 0;
  while (n >= 1024 && i < 3) { n /= 1024; i++; }
  return (i && n < 10 ? n.toFixed(1) : Math.round(n)) + u[i];
}
function _fmtNum(n) { return n == null ? '—' : Number(n).toLocaleString(); }
function _pctStr(v) { return v == null ? '—' : Math.round(v * 100) + '%'; }

// Horizontal labelled bar list — reuses the existing toplist styles.
function BarList({ items, fmt }) {
  const mx = Math.max(1, ...items.map(it => it.value || 0));
  if (!items.length) return <div className="qh-metric-sub">No data yet.</div>;
  return (
    <div className="qh-toplist">
      {items.map((it, i) => (
        <div key={it.label + i} className="qh-toprow">
          <span className="qh-topuser" title={it.label}>{it.label}</span>
          <div className="qh-toptrack"><div className="qh-topfill" style={{ width: ((it.value || 0) / mx * 100) + '%' }} /></div>
          <span className="qh-topn">{fmt ? fmt(it.value) : _fmtNum(it.value)}</span>
        </div>
      ))}
    </div>
  );
}

// Compact weekly % trend bars — reuses qh-bars.
function TrendBars({ data, valKey, fmtTitle }) {
  if (!data.length) return <div className="qh-metric-sub">No data yet.</div>;
  const mx = Math.max(1, ...data.map(d => d[valKey] || 0));
  return (
    <div className="qh-bars">
      {data.map(d => <div key={d.period} className="qh-bar-wrap"><div className="qh-bar" style={{ height: ((d[valKey] || 0) / mx * 100) + '%' }} title={fmtTitle(d)} /></div>)}
    </div>
  );
}

function MetricsView({ st }) {
  const m = st.metrics || {};
  if (!m.headline) return (
    <div className="qh-apad"><div className="qh-aview-head"><div>
      <div className="qh-aview-title">Metrics</div><div className="qh-aview-sub">Loading…</div>
    </div></div></div>
  );
  const h = m.headline, cost = m.costSavings || {};
  const vol = m.volumeWeekly || [];
  const volMax = Math.max(1, ...vol.map(v => v.total));
  const sla = (m.approvalSla && m.approvalSla.overall) || {};
  const tierT = m.tierTotals || { RO: 0, RW: 0, DDL: 0 };
  const tierSum = (tierT.RO || 0) + (tierT.RW || 0) + (tierT.DDL || 0);
  const peak = m.peakHours || [];
  const peakMax = Math.max(1, ...peak.flat());
  const low = m.ratingLow || [], imports = m.csvImports || [], csv = m.csvSummary || {}, who = m.whoCanWhat || [];

  return (
    <div className="qh-apad">
      <div className="qh-aview-head"><div>
        <div className="qh-aview-title">Metrics</div>
        <div className="qh-aview-sub">From p_metrics_* (self-test excluded){m.reportStart ? ' · since ' + m.reportStart : ''} · {m.timezone || 'UTC'}</div>
      </div></div>

      <div className="qh-kpi-grid">
        <Stat k="Total requests" v={_fmtNum(h.total)} />
        <Stat k="Completed" v={_fmtNum(h.completed)} sub={_pctStr(h.successRate) + ' success'} />
        <Stat k="Failed" v={_fmtNum(h.failed)} />
        <Stat k="Rejected" v={_fmtNum(h.rejected)} />
        <Stat k="Cancelled" v={_fmtNum(h.cancelled)} />
        <Stat k="Auto-approved" v={_pctStr(h.autoApproveRate)} sub="less DBA load" />
        <Stat k="Unique users" v={_fmtNum(h.uniqueUsers)} />
        <Stat k="Targets touched" v={_fmtNum(h.targetsTouched)} />
        <Stat k="p50 approval" v={_fmtSec(h.p50ApprovalSec)} />
        <Stat k="p95 approval" v={_fmtSec(h.p95ApprovalSec)} />
        <Stat k="Avg rating" v={h.avgRating != null ? h.avgRating : '—'} sub={(h.ratingCount || 0) + ' ratings'} />
      </div>

      <div className="qh-mcard">
        <div className="qh-mcard-title">Cost savings snapshot</div>
        <div className="qh-kpi-grid qh-kpi-flush">
          <Stat k="Completed" v={_fmtNum(cost.completed)} />
          <Stat k="DBA hours saved" v={cost.dbaHoursSaved != null ? cost.dbaHoursSaved : '—'} sub={(cost.dbaMinutesPerRequest || 0) + 'm × $' + (cost.dbaHourlyUsd || 0) + '/hr'} />
          <Stat k="DBA $ saved" v={'$' + _fmtNum(cost.dbaSavingUsd)} />
          <Stat k="Avoided replicas" v={_fmtNum(cost.avoidedReplicas)} />
          <Stat k="Infra $ / mo" v={'$' + _fmtNum(cost.infraUsdPerMonth)} sub="replicas + other" />
        </div>
      </div>

      <div className="qh-msection">Volume, tiers & usage</div>
      <div className="qh-mcards">
        <div className="qh-mcard">
          <div className="qh-mcard-title">Weekly volume — status mix</div>
          <div className="qh-stackbars">
            {vol.map(w => (
              <div key={w.period} className="qh-stackcol" title={w.period + ' · ' + w.total + ' total · ' + w.activeUsers + ' users'}>
                <div className="qh-stack" style={{ height: (w.total / volMax * 100) + '%' }}>
                  <div className="qh-stackseg seg-completed" style={{ flexGrow: w.completed }} />
                  <div className="qh-stackseg seg-failed" style={{ flexGrow: w.failed }} />
                  <div className="qh-stackseg seg-rejected" style={{ flexGrow: w.rejected }} />
                  <div className="qh-stackseg seg-cancelled" style={{ flexGrow: w.cancelled }} />
                </div>
              </div>
            ))}
          </div>
          <div className="qh-legend"><span className="lg seg-completed">completed</span><span className="lg seg-failed">failed</span><span className="lg seg-rejected">rejected</span><span className="lg seg-cancelled">cancelled</span></div>
        </div>
        <div className="qh-mcard">
          <div className="qh-mcard-title">Tier mix</div>
          <div className="qh-tierbars">
            {['RO', 'RW', 'DDL'].map(t => (
              <div key={t} className="qh-tierbar-row">
                <TierBadge tier={t} sm />
                <div className="qh-tierbar-track"><div className={'qh-tierbar-fill tier-fill-' + t.toLowerCase()} style={{ width: (tierSum ? (tierT[t] || 0) / tierSum * 100 : 0) + '%' }} /></div>
                <span className="qh-tierbar-n">{_fmtNum(tierT[t])}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="qh-mcards">
        <div className="qh-mcard"><div className="qh-mcard-title">Approval latency percentiles</div>
          <BarList items={['p50', 'p75', 'p90', 'p95', 'p99'].map(p => ({ label: p, value: sla[p] || 0 }))} fmt={_fmtSec} /></div>
        <div className="qh-mcard"><div className="qh-mcard-title">Top users</div>
          <BarList items={(m.topUsers || []).map(u => ({ label: u.name, value: u.count }))} /></div>
      </div>

      <div className="qh-mcards">
        <div className="qh-mcard"><div className="qh-mcard-title">Per-team usage</div>
          <BarList items={(m.teamUsage || []).slice(0, 10).map(u => ({ label: u.name, value: u.count }))} /></div>
        <div className="qh-mcard"><div className="qh-mcard-title">Admin workload (decisions)</div>
          <BarList items={(m.adminWorkload || []).map(u => ({ label: u.name, value: u.count }))} /></div>
      </div>

      <div className="qh-mcards">
        <div className="qh-mcard"><div className="qh-mcard-title">Target usage</div>
          <BarList items={(m.targetUsage || []).slice(0, 12).map(u => ({ label: u.name, value: u.count }))} /></div>
        <div className="qh-mcard"><div className="qh-mcard-title">Scheduled adoption (weekly %)</div>
          <TrendBars data={m.scheduledUsage || []} valKey="pct" fmtTitle={d => d.period + ' · ' + d.pct + '% (' + d.scheduled + '/' + d.total + ')'} /></div>
      </div>

      <div className="qh-mcard">
        <div className="qh-mcard-title">Peak hours — day × hour (local)</div>
        <div className="qh-heat">
          <div className="qh-heatrow"><span className="qh-heat-lbl" />{Array.from({ length: 24 }, (_, hh) => <span key={hh} className="qh-heat-h">{hh % 6 === 0 ? hh : ''}</span>)}</div>
          {peak.map((row, d) => (
            <div key={d} className="qh-heatrow">
              <span className="qh-heat-lbl">{_DOW[d]}</span>
              {row.map((c, hh) => <span key={hh} className="qh-heatcell" style={{ opacity: c ? (0.14 + 0.86 * c / peakMax) : 0.05 }} title={_DOW[d] + ' ' + hh + ':00 · ' + c + ' req'} />)}
            </div>
          ))}
        </div>
      </div>

      <div className="qh-msection">Quality & data</div>
      <div className="qh-mcards">
        <div className="qh-mcard"><div className="qh-mcard-title">Weekly avg rating (of 5)</div>
          <TrendBars data={m.ratingWeekly || []} valKey="avg" fmtTitle={d => d.period + ' · ' + d.avg + ' (' + d.count + ')'} /></div>
        <div className="qh-mcard"><div className="qh-mcard-title">Rating response rate (weekly %)</div>
          <TrendBars data={m.ratingResponse || []} valKey="pct" fmtTitle={d => d.period + ' · ' + d.pct + '% (' + d.rated + '/' + d.completed + ')'} /></div>
      </div>

      {low.length > 0 && (
        <div className="qh-mcard"><div className="qh-mcard-title">Low ratings (≤2) with feedback</div>
          <div className="qh-tablewrap"><table className="qh-atable"><thead><tr><th>User</th><th>Rating</th><th>Feedback</th><th>When</th></tr></thead>
            <tbody>{low.map((r, i) => <tr key={i}><td><b>{r.user}</b></td><td>{r.rating}★</td><td>{r.feedback || '—'}</td><td className="qh-muted">{r.when ? qhFmt(r.when) : '—'}</td></tr>)}</tbody></table></div>
        </div>
      )}

      <div className="qh-mcard">
        <div className="qh-mcard-title">CSV bulk imports</div>
        <div className="qh-kpi-grid">
          <Stat k="Imports" v={_fmtNum(csv.imports)} />
          <Stat k="Completed" v={_fmtNum(csv.completed)} />
          <Stat k="Failed" v={_fmtNum(csv.failed)} />
          <Stat k="Rows loaded" v={_fmtNum(csv.rowsLoaded)} />
          <Stat k="Success rate" v={csv.successRate != null ? csv.successRate + '%' : '—'} />
        </div>
        {imports.length > 0 && (
          <div className="qh-tablewrap"><table className="qh-atable"><thead><tr><th>When</th><th>User</th><th>Target / DB</th><th>Table</th><th>Status</th><th>Rows</th><th>Size</th></tr></thead>
            <tbody>{imports.slice(0, 20).map(r => <tr key={r.id}><td className="qh-muted">{r.when ? qhFmt(r.when) : '—'}</td><td>{r.user}</td><td className="qh-mono">{(r.target || '?') + ' / ' + (r.db || '')}</td><td className="qh-mono">{r.table}{r.isNew ? ' (new)' : ''}</td><td className={'qh-csv-st is-' + (r.status || '')}>{r.status}</td><td className="qh-mono">{_fmtNum(r.rows)}</td><td className="qh-mono">{_fmtBytes(r.bytes)}</td></tr>)}</tbody></table></div>
        )}
      </div>

      {who.length > 0 && (
        <div className="qh-mcard"><div className="qh-mcard-title">Who can do what</div>
          <div className="qh-tablewrap"><table className="qh-atable"><thead><tr>{Object.keys(who[0]).map(k => <th key={k}>{k}</th>)}</tr></thead>
            <tbody>{who.map((r, i) => <tr key={i}>{Object.keys(who[0]).map(k => <td key={k} className="qh-mono">{r[k] == null ? '' : String(r[k])}</td>)}</tr>)}</tbody></table></div>
        </div>
      )}
    </div>
  );
}

// ---------- Feedback ----------
function Stars({ n }) {
  return <span className="qh-stars">{[1, 2, 3, 4, 5].map(i => <svg key={i} width="13" height="13" viewBox="0 0 24 24" fill={i <= n ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round"><path d="M12 3l2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9 6.8 19.2l1-5.8L3.5 9.2l5.9-.9z"/></svg>)}</span>;
}

function FeedbackView({ st }) {
  const fb = st.feedback;
  const avg = (fb.reduce((s, f) => s + f.score, 0) / fb.length).toFixed(1);
  return (
    <div className="qh-apad">
      <div className="qh-aview-head"><div><div className="qh-aview-title">Feedback</div><div className="qh-aview-sub">Developer ratings on their query experience · avg {avg} ★</div></div></div>
      <div className="qh-fblist">
        {fb.map(f => (
          <div key={f.id} className={'qh-fbcard' + (f.score <= 2 ? ' is-low' : '')}>
            <div className="qh-fbtop">
              <span className="qh-fbuser">{f.user}</span>
              <Stars n={f.score} />
              <span className="qh-qcard-when">{qhAgo(f.when)}</span>
            </div>
            <div className="qh-fbcomment">“{f.comment}”</div>
            <div className="qh-fbmeta">on query {f.queryId}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

Object.assign(window, { AuditView2, MetricsView, FeedbackView });
