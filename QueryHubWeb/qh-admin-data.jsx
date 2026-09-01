// QueryHub Admin — real data via qhApi (no mock).
// useAdminState fetches from /api/admin/* when the admin panel is open and
// maps each response to the shape the admin views render. Actions call the
// real endpoints and refetch. Actions with no web backend yet (scope
// add/edit/remove, endpoint-request decision) surface an honest "managed in
// Slack" toast instead of faking a local mutation.
//
// DESIGN NOTE (this project only): the file is the product's real hook, byte for
// byte — in this prototype the qhApi it calls is the MOCK client in
// qh-api-mock.jsx, so the same code drives mock data. Do not fork it to add mock
// behaviour; change the mock API instead.

const { useState: useAdminStateHook, useEffect: useAdminEffect, useCallback: useAdminCb } = React;

// How often the approval queue refreshes for an admin. Matches the
// notification bell's cadence (60s) so the badge and the bell can't
// disagree about whether something is waiting.
const QH_QUEUE_POLL_MS = 60000;

function qhIso(d) { return d.toISOString(); }
function qhAgo(iso) {
  if (!iso) return '';
  const s = Math.max(1, Math.floor((Date.now() - new Date(iso)) / 1000));
  if (s < 60) return s + 's ago';
  const m = Math.floor(s / 60); if (m < 60) return m + 'm ago';
  const h = Math.floor(m / 60); if (h < 24) return h + 'h ago';
  return Math.floor(h / 24) + 'd ago';
}
// Format a UTC ISO timestamp in the fleet's display timezone (window.QH_TZ,
// set from GET /me; default Europe/Istanbul). DB stores UTC everywhere; every
// shown time is converted here. Falls back to UTC if the zone is unusable.
function qhFmt(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  const tz = (typeof window !== 'undefined' && window.QH_TZ) || 'Europe/Istanbul';
  try {
    const p = new Intl.DateTimeFormat('en-CA', { timeZone: tz, hourCycle: 'h23',
      year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).formatToParts(d);
    const g = t => (p.find(x => x.type === t) || {}).value || '';
    return g('year') + '-' + g('month') + '-' + g('day') + ' ' + g('hour') + ':' + g('minute');
  } catch (e) {
    return d.toISOString().slice(0, 16).replace('T', ' ');
  }
}

// Tier precedence — most permissive wins when a connection collapses (RO < RW < DDL).
const QH_TIER_RANK = { RO: 0, RW: 1, DDL: 2, ro: 0, rw: 1, ddl: 2 };
function qhMaxTier(a, b) { return (QH_TIER_RANK[b] || 0) > (QH_TIER_RANK[a] || 0) ? b : a; }
// Standing grants are PER-CONNECTION with a database LIST (['*'] = all databases).
// Real db ids equal their names, so no lookup is needed to render them.
function qhGrantDbNames(g) {
  const dbs = (g.databases && g.databases.length) ? g.databases : ['*'];
  return dbs.includes('*') ? 'all databases' : dbs.join(', ');
}
function qhGrantTarget(g) { return g.connectionId + ' / ' + qhGrantDbNames(g); }

// Empty metrics shell (same keys the Insights view reads) until /admin/metrics loads.
const QH_METRICS = {
  totalQueries: 0, autoApproveRate: 0, avgLatencyMin: 0, rejectRate: 0,
  perDay: [], tierBreakdown: { RO: 0, RW: 0, DDL: 0 },
  topSubmitters: [], latencyByTier: { RO: 0, RW: 0, DDL: 0 },
};

// System configuration is the REAL bot_config, fetched from GET /admin/config
// (typed + grouped) and written back via PUT — no mock. Shape:
// { groups:[{id,title,items:[{key,label,value,type,description,updatedAt}]}], values:{key:value} }.

// ---------- The state hook ----------
// `active` = the admin panel is the current view (drives the full reload of all
// thirteen sections). A developer-only session never fires admin fetches.
// `isAdminViewer` = this user can review at all, panel open or not — that is
// what the queue poll keys off, so the sidebar badge is right before the
// first visit to the panel.
function useAdminState(pushToast, active, isAdminViewer) {
  const [queue, setQueue] = useAdminStateHook([]);
  const [grants, setGrants] = useAdminStateHook([]);
  const [autoGrants, setAutoGrants] = useAdminStateHook([]);
  const [scopes, setScopes] = useAdminStateHook([]);
  const [people, setPeople] = useAdminStateHook([]);
  const [teams, setTeams] = useAdminStateHook([]);
  const [endpointReqs, setEndpointReqs] = useAdminStateHook([]);
  const [feedback, setFeedback] = useAdminStateHook([]);
  const [audit, setAudit] = useAdminStateHook([]);
  const [metrics, setMetrics] = useAdminStateHook({});
  const [connections, setConnections] = useAdminStateHook([]);
  const [killSwitch, setKillSwitch] = useAdminStateHook({ enabled: false, message: '', by: null, at: null });
  const [config, setConfig] = useAdminStateHook({ groups: [], values: {} });
  const [loadError, setLoadError] = useAdminStateHook(false);
  const [loading, setLoading] = useAdminStateHook(false);

  // API → view shapes.
  const mapQueue = (items) => (items || []).map(it => ({
    ...it,
    piiCols: (it.piiCols || []).map(c => (typeof c === 'string' ? c : c.col)),
  }));
  // Keep g.subject as the IDENTITY the access editor matches on: a user's
  // slack_user_id (so subjLabel resolves it against the people directory) and a
  // team's NAME (team grant writes resolve by name). databases stays a LIST —
  // ['*'] = every database on the connection (grants are per-connection).
  const mapGrants = (gs) => (gs || []).map(g => ({
    ...g,
    subject: g.subjectType === 'team' ? (g.subjectName || g.subject) : g.subject,
    databases: (Array.isArray(g.databases) && g.databases.length) ? g.databases : ['*'],
  }));

  const loadQueue = useAdminCb(() => qhApi.adminQueue().then(r => setQueue(mapQueue(r.queue))).catch(() => {}), []);
  const loadGrants = useAdminCb(() => qhApi.adminGrants().then(r => setGrants(mapGrants(r.grants))).catch(() => {}), []);
  const loadAuto = useAdminCb(() => qhApi.adminAutoGrants().then(r => setAutoGrants(r.autoGrants || [])).catch(() => {}), []);
  const loadAudit = useAdminCb(() => qhApi.adminAudit().then(r => setAudit(r.audit || [])).catch(() => {}), []);
  const loadKill = useAdminCb(() => qhApi.adminKillGet().then(r => setKillSwitch({ enabled: !!r.enabled, message: r.message || '', by: r.by || null, at: r.at || null })).catch(() => {}), []);
  const loadScopes = useAdminCb(() => qhApi.adminScopes().then(r => setScopes(r.scopes || [])).catch(() => {}), []);
  const loadEndpointReqs = useAdminCb(() => qhApi.adminEndpointReqs().then(r => setEndpointReqs((r.requests || []).map(x => ({ ...x, tier: x.tier || null })))).catch(() => {}), []);
  const loadPeople = useAdminCb(() => qhApi.adminPeople().then(r => setPeople(r.people || [])).catch(() => {}), []);
  const loadTeams = useAdminCb(() => qhApi.adminTeams().then(r => setTeams(r.teams || [])).catch(() => {}), []);
  const loadConnections = useAdminCb(() => qhApi.adminConnections().then(r => setConnections(r.connections || [])).catch(() => {}), []);

  // Full initial load. Unlike the per-section loaders above (which swallow
  // errors on post-mutation refreshes), this tracks failure so the panel can
  // show an error + Retry instead of a silently-empty view. Promise.allSettled
  // so one failing section doesn't abort the rest.
  const reloadAll = useAdminCb(() => {
    setLoadError(false); setLoading(true);
    const jobs = [
      qhApi.adminQueue().then(r => setQueue(mapQueue(r.queue))),
      qhApi.adminGrants().then(r => setGrants(mapGrants(r.grants))),
      qhApi.adminAutoGrants().then(r => setAutoGrants(r.autoGrants || [])),
      qhApi.adminAudit().then(r => setAudit(r.audit || [])),
      qhApi.adminKillGet().then(r => setKillSwitch({ enabled: !!r.enabled, message: r.message || '', by: r.by || null, at: r.at || null })),
      qhApi.adminScopes().then(r => setScopes(r.scopes || [])),
      qhApi.adminEndpointReqs().then(r => setEndpointReqs((r.requests || []).map(x => ({ ...x, tier: x.tier || null })))),
      qhApi.adminFeedback().then(r => setFeedback(r.feedback || [])),
      qhApi.adminMetrics().then(r => setMetrics(r || {})),
      qhApi.adminConnections().then(r => setConnections(r.connections || [])),
      qhApi.adminConfig().then(r => setConfig(r.config || { groups: [], values: {} })),
      qhApi.adminPeople().then(r => setPeople(r.people || [])),
      qhApi.adminTeams().then(r => setTeams(r.teams || [])),
    ];
    return Promise.allSettled(jobs).then(results => {
      setLoading(false);
      // A 403 just means this admin's scope can't reach that section (a scoped
      // "dba" admin can't see the super-only Access/System endpoints — the nav
      // hides them too). That is expected, not a load failure. Only real errors
      // (network / 5xx / auth loss) should surface the error + Retry banner.
      if (results.some(x => x.status === 'rejected' && (!x.reason || x.reason.status !== 403))) setLoadError(true);
    });
  }, []);

  useAdminEffect(() => {
    if (!active) return;
    reloadAll();
  }, [active, reloadAll]);

  // Keep the approval queue live.
  //
  // Two problems this fixes. (1) The full reloadAll only ran when the panel
  // opened, so the pending-count badge in the sidebar was always 0 until you
  // had already navigated into Admin — where the badge is redundant. (2) Once
  // inside, nothing re-fetched: a DBA could sit on the queue for an hour and
  // never see a request that arrived a minute after they opened it, or one a
  // colleague had already decided.
  //
  // Only the queue is polled (one cheap endpoint), not all thirteen sections.
  // Skipped while the tab is hidden so a parked window isn't polling forever;
  // a visibilitychange refetch covers coming back.
  useAdminEffect(() => {
    if (!isAdminViewer) return undefined;
    const tick = () => {
      if (typeof document !== 'undefined' && document.hidden) return;
      loadQueue();
    };
    tick();
    const iv = setInterval(tick, QH_QUEUE_POLL_MS);
    const onVis = () => { if (!document.hidden) loadQueue(); };
    document.addEventListener('visibilitychange', onVis);
    return () => { clearInterval(iv); document.removeEventListener('visibilitychange', onVis); };
  }, [isAdminViewer, loadQueue]);

  const fail = (e, fallback) => pushToast && pushToast((e && e.message) || fallback);

  const decide = (id, decision, actor, note) => {
    qhApi.adminDecision(id, { decision, note: note || null })
      .then(() => { loadQueue(); loadAudit();
        pushToast && pushToast(decision === 'approve' ? 'Approved — running now. Submitter notified in Slack.'
          : decision === 'reject' ? 'Rejected. Submitter notified in Slack.'
          : 'Change request sent to the submitter in Slack.'); })
      .catch(e => fail(e, 'Decision failed.'));
  };
  const batchApprove = (ids) => {
    qhApi.adminBatchApprove(ids).then(r => { loadQueue(); loadAudit();
      pushToast && pushToast('Approved ' + (r && r.approved != null ? r.approved : ids.length) + ' queries.'); })
      .catch(e => fail(e, 'Batch approve failed.'));
  };
  const approveBundle = (bundleId) => {
    const ids = queue.filter(x => x.bundleId === bundleId).map(x => x.id);
    if (ids.length) batchApprove(ids);
  };
  const toggleKill = (on, actor, message) => {
    qhApi.adminKillSet({ enabled: !!on, message: message || undefined }).then(() => { loadKill(); loadAudit();
      pushToast && pushToast(on ? 'Kill switch ON — new query traffic paused fleet-wide.' : 'Kill switch released — traffic resumed.'); })
      .catch(e => fail(e, 'Kill switch change failed.'));
  };
  // `subjects` (a list) and `subject` (one) are both accepted and exactly one is
  // used, server-side: with a list the ids are all validated before a row is
  // written and the writes share one transaction, so a refusal leaves nothing
  // behind (CODE brief 2026-09-01 §1). The promise is RETURNED here — the
  // multi-person form keeps the picker open on a refusal and needs to know.
  const addGrant = (g) => {
    const many = (g.subjects || []).filter(Boolean);
    const who = many.length ? many.length + ' people' : g.subject;
    return qhApi.adminAddGrant({ subjectType: g.subjectType, subject: g.subject, subjects: many.length ? many : null, connectionId: g.connectionId, databaseId: g.databaseId, databases: g.databases || null, tier: g.tier, reason: g.reason || null, expiresAt: g.expiresAt || null })
      .then(r => { loadGrants(); loadAudit(); pushToast && pushToast('Grant added: ' + who + ' → ' + g.connectionId + ' (' + g.tier + ').'); return r; })
      .catch(e => { fail(e, 'Add grant failed.'); throw e; });
  };
  const revokeGrant = (id) => {
    qhApi.adminDelGrant(id).then(() => { loadGrants(); loadAudit(); pushToast && pushToast('Grant revoked.'); })
      .catch(e => fail(e, 'Revoke failed.'));
  };
  const addAutoGrant = (g) => {
    qhApi.adminAddAutoGrant({ user: g.user, connectionId: g.connectionId, databaseId: g.databaseId, tier: g.tier, reason: g.reason || null, expiresAt: g.expiresAt || null })
      .then(() => { loadAuto(); loadAudit(); pushToast && pushToast('Auto-approve grant created.'); })
      .catch(e => fail(e, 'Create failed.'));
  };
  // One person's resolved reach, and "give them what that person has"
  // (CODE brief 2026-08-20 §6). effectiveAccess returns the promise rather than
  // holding state: it is per-person and only its caller wants it, so a shared
  // slot would go stale behind whoever looked last.
  const effectiveAccess = (id) => qhApi.adminEffectiveAccess(id);
  // Is this id someone? (CODE brief 2026-08-22 §3.) Also a promise, and the
  // errors are the CALLER's to render: the subject combo turns a failed lookup
  // into a note, because the grant is still writable — the server resolves the
  // principal again when it writes it.
  const resolvePerson = (principal) => qhApi.adminResolvePerson(principal);
  // `mode` ('merge' | 'replace'), `includeAutoApprove` and `dryRun` (CODE brief
  // 2026-09-01 §2). A dry run writes nothing, so it must not reload or toast — it
  // is the preview the confirm step is drawn from, and a toast saying "copied"
  // for a preview would be a lie the admin acts on.
  const copyAccess = (id, body) =>
    qhApi.adminCopyAccess(id, { source: body.source, includeTeams: !!body.includeTeams,
      includeAutoApprove: !!body.includeAutoApprove, tier: body.tier || null,
      mode: body.mode === 'replace' ? 'replace' : 'merge', dryRun: !!body.dryRun })
      .then(r => {
        if (body.dryRun) return r;
        loadGrants(); loadAuto(); loadAudit();
        const n = r && r.written, rev = (r && r.revoked && r.revoked.length) || 0;
        pushToast && pushToast('Access copied' + (n ? ' · ' + n + ' connection' + (n === 1 ? '' : 's') : '')
          + (rev ? ' · ' + rev + ' revoked' : '')
          + (r && r.autoApproveCopied ? ' · ' + r.autoApproveCopied + ' auto-approve' : '')
          + (r && r.teams && r.teams.length ? ' · joined ' + r.teams.join(', ') : '') + '.');
        return r;
      })
      .catch(e => { fail(e, 'Copy failed.'); throw e; });

  const revokeAutoGrant = (id) => {
    qhApi.adminDelAutoGrant(id).then(() => { loadAuto(); loadAudit(); pushToast && pushToast('Auto-approve grant revoked.'); })
      .catch(e => fail(e, 'Revoke failed.'));
  };

  // Admin scopes — real writes (POST /admin/scopes upserts a scoped/super
  // admin; DELETE disables one). The backend guards the last super-admin and
  // the auth-event trigger DMs the affected user.
  const saveScope = (scope) => {
    // On edit the row's `admin` is a display name; `id` carries the slack id
    // the backend upserts on. On create, `admin` is the typed slack id.
    const adminId = scope.id || scope.admin;
    qhApi.adminSaveScope({ admin: adminId, role: scope.role,
      canApprove: scope.canApprove, connections: scope.connections })
      .then(() => { loadScopes(); loadAudit();
        pushToast && pushToast('Admin scope saved: ' + (scope.admin || adminId) + '.'); })
      .catch(e => fail(e, 'Save scope failed.'));
  };
  const removeScope = (id) => {
    qhApi.adminDelScope(id).then(() => { loadScopes(); loadAudit();
      pushToast && pushToast('Admin removed.'); })
      .catch(e => fail(e, 'Remove failed.'));
  };
  // Endpoint requests — approve provisions a real RO grant (+ DMs the
  // requester), reject just records the decision. Both audited server-side.
  const decideEndpoint = (id, ok) => {
    qhApi.adminDecideEndpoint(id, !!ok)
      .then(() => { loadEndpointReqs(); loadGrants(); loadAudit();
        pushToast && pushToast(ok
          ? 'Provisioned — RO grant created and the requester was notified in Slack.'
          : 'Request rejected. The requester was notified in Slack.'); })
      .catch(e => fail(e, 'Decision failed.'));
  };

  // Subject-centric access: replace ALL grants for one subject with a new
  // target set. Each editor row is already per-connection ({connectionId,
  // databases:[..]|['*'], tier}); duplicate connections collapse — union the
  // databases ('*' absorbs), most permissive tier wins. POST upserts per
  // connection; connections no longer present are DELETEd (no PATCH by design).
  const setSubjectGrants = (subjectType, subject, targets) => {
    const byConn = new Map();
    (targets || []).forEach(t => {
      const dbs = (t.databases && t.databases.length) ? t.databases : ['*'];
      const ex = byConn.get(t.connectionId);
      if (!ex) byConn.set(t.connectionId, { connectionId: t.connectionId, databases: dbs.includes('*') ? ['*'] : [...dbs], tier: t.tier, expiresAt: t.expiresAt || null });
      else {
        ex.tier = qhMaxTier(ex.tier, t.tier);
        ex.databases = (ex.databases.includes('*') || dbs.includes('*')) ? ['*'] : [...new Set([...ex.databases, ...dbs])];
        // An expiry is a limit like the other two, so it collapses the same way:
        // the more permissive of the pair wins, and no date outlives any date.
        ex.expiresAt = (!ex.expiresAt || !t.expiresAt) ? null : (new Date(t.expiresAt) > new Date(ex.expiresAt) ? t.expiresAt : ex.expiresAt);
      }
    });
    const desired = [...byConn.values()];
    const keep = new Set(desired.map(d => d.connectionId));
    const mine = grants.filter(g => g.subjectType === subjectType && g.subject === subject);
    const jobs = desired.map(d => qhApi.adminAddGrant({ subjectType, subject, connectionId: d.connectionId, databases: d.databases.includes('*') ? null : d.databases, tier: d.tier, expiresAt: d.expiresAt || null }))
      .concat(mine.filter(g => !keep.has(g.connectionId)).map(g => qhApi.adminDelGrant(g.id)));
    Promise.allSettled(jobs).then(rs => {
      loadGrants(); loadAudit();
      const bad = rs.filter(x => x.status === 'rejected').length;
      pushToast && pushToast(bad ? ('Saved with ' + bad + ' error' + (bad === 1 ? '' : 's') + '.')
        : (desired.length ? ('Access saved · ' + desired.length + ' connection' + (desired.length === 1 ? '' : 's') + '.') : 'All access removed.'));
    });
  };
  // Edit a single grant = upsert the new one, then drop the old row if the
  // connection changed (add first, so a failure never leaves the subject with none).
  const updateGrant = (g) => {
    const dbs = (g.databases && g.databases.length && !g.databases.includes('*')) ? g.databases : null;
    qhApi.adminAddGrant({ subjectType: g.subjectType, subject: g.subject, connectionId: g.connectionId, databases: dbs, tier: g.tier, expiresAt: g.expiresAt || null })
      .then(res => (res && res.id && res.id !== g.id) ? qhApi.adminDelGrant(g.id) : null)
      .then(() => { loadGrants(); loadAudit(); pushToast && pushToast('Grant updated.'); })
      .catch(e => fail(e, 'Update failed.'));
  };
  const updateAutoGrant = (g) => {
    qhApi.adminAddAutoGrant({ user: g.user, connectionId: g.connectionId, databaseId: g.databaseId, tier: g.tier, expiresAt: g.expiresAt || null })
      .then(() => qhApi.adminDelAutoGrant(g.id))
      .then(() => { loadAuto(); loadAudit(); pushToast && pushToast('Auto-approve grant updated.'); })
      .catch(e => fail(e, 'Update failed.'));
  };

  // Teams — create / edit / delete + membership. All audited server-side; the
  // team_members trigger DMs every added/removed member automatically.
  const addTeam = (t) => {
    qhApi.adminSaveTeam({ name: t.name, desc: t.desc, members: t.members || [] })
      .then(() => { loadTeams(); loadAudit(); pushToast && pushToast('Team created: ' + t.name + '.'); })
      .catch(e => fail(e, 'Create team failed.'));
  };
  const updateTeam = (t) => {
    qhApi.adminSaveTeam({ id: t.id, name: t.name, desc: t.desc, members: t.members || [] })
      .then(() => { loadTeams(); loadGrants(); loadAudit(); pushToast && pushToast('Team updated: ' + t.name + '.'); })
      .catch(e => fail(e, 'Update team failed.'));
  };
  const removeTeam = (id) => {
    qhApi.adminDelTeam(id).then(() => { loadTeams(); loadGrants(); loadAudit(); pushToast && pushToast('Team deleted.'); })
      .catch(e => fail(e, 'Delete team failed.'));
  };
  const setPersonTeams = (handle, teamIds) => {
    qhApi.adminSetPersonTeams(handle, teamIds || []).then(() => { loadTeams(); loadAudit(); pushToast && pushToast('Team membership updated.'); })
      .catch(e => fail(e, 'Update failed.'));
  };

  // Connections — the target-server registry. These three resolve to true or
  // false instead of rejecting, because the caller is a modal that has to
  // decide whether to close: chaining off a promise that was already handled
  // by `fail` would close the form on an error too.
  const addConnection = (payload) =>
    qhApi.adminCreateConnection(payload)
      .then(c => { loadConnections(); loadAudit();
        pushToast && pushToast('Connection "' + (c && c.name) + '" registered — disabled until you set credentials and enable it.');
        return true; })
      .catch(e => { fail(e, 'Create connection failed.'); return false; });
  const updateConnection = (conn, patch) =>
    qhApi.adminUpdateConnection(conn, patch)
      .then(() => { loadConnections(); loadAudit(); pushToast && pushToast('Connection "' + conn + '" updated.'); return true; })
      .catch(e => { fail(e, 'Update connection failed.'); return false; });
  // Delete has two success outcomes: gone, or kept-but-disabled because
  // history or live grants still point at it. The server decides which and
  // explains itself in `reason`; show that rather than a generic "deleted".
  const removeConnection = (conn) =>
    qhApi.adminDeleteConnection(conn)
      .then(r => { loadConnections(); loadGrants(); loadAudit();
        pushToast && pushToast(r && r.deleted ? ('Connection "' + conn + '" deleted.') : ((r && r.reason) || ('Connection "' + conn + '" disabled.')));
        return true; })
      .catch(e => { fail(e, 'Delete connection failed.'); return false; });
  const setConnectionEnabled = (conn, on) =>
    qhApi.adminUpdateConnection(conn, { enabled: !!on })
      .then(() => { loadConnections(); loadAudit();
        pushToast && pushToast('Connection "' + conn + '" ' + (on ? 'enabled — developers with a grant can query it now.' : 'disabled.'));
        return true; })
      .catch(e => { fail(e, (on ? 'Enable' : 'Disable') + ' failed.'); return false; });

  // System configuration: write the changed bot_config keys for real (PUT
  // /admin/config), then refresh from the server + re-pull the audit log.
  // `changes` is a flat { key: value } of only what the user edited.
  const saveConfig = (changes) => {
    qhApi.adminConfigSave(changes)
      .then(r => { setConfig(r.config || { groups: [], values: {} }); loadAudit();
        const n = r && r.applied != null ? r.applied : Object.keys(changes || {}).length;
        pushToast && pushToast(n ? ('Configuration saved · ' + n + ' change' + (n === 1 ? '' : 's') + ' applied fleet-wide.')
          : 'No changes to save.'); })
      .catch(e => fail(e, 'Save failed.'));
  };

  return {
    queue, grants, autoGrants, scopes, people, teams, endpointReqs, feedback, audit, metrics, connections, killSwitch, config, pushToast,
    loadError, loading, reload: reloadAll,
    decide, batchApprove, approveBundle, toggleKill,
    addGrant, updateGrant, revokeGrant, setSubjectGrants,
    effectiveAccess, copyAccess, resolvePerson,
    addAutoGrant, updateAutoGrant, revokeAutoGrant,
    saveScope, removeScope, decideEndpoint, saveConfig,
    addTeam, updateTeam, removeTeam, setPersonTeams,
    addConnection, updateConnection, removeConnection, setConnectionEnabled,
    reloadConnections: loadConnections,
  };
}

Object.assign(window, { useAdminState, qhAgo, qhFmt, qhIso, qhGrantTarget, qhGrantDbNames, qhMaxTier, QH_METRICS });
