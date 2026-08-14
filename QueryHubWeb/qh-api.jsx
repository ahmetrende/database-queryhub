// QueryHub — API client (integration plumbing only; no UI here).
// Same-origin cookies carry the session; on 401 we try one silent
// refresh, then broadcast qh:signed-out so the app shows the login.
// CODE owns this file (it is not part of the design mock); main.jsx
// loads it before qh-login/qh-app so window.qhApi + API_BASE exist.

const API_BASE = '/api';

// Refresh is single-flight: the access JWT is short (~20 min) and the
// refresh token is single-use (rotated server-side), so if several calls
// 401 at once — the per-tab pollers and the post-login fan-out routinely
// do — each must NOT fire its own /auth/refresh. The losers would present
// the already-rotated (now stale) refresh cookie, get 401, and bounce the
// user to login mid-session. Instead all concurrent 401s await one shared
// refresh; the winner rotates the cookie and everyone retries with it.
let _refreshInFlight = null;

async function qhFetch(path, opts = {}, _retried = false) {
  const res = await fetch(API_BASE + path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  });
  // A 401 from the login endpoints is an expected outcome (wrong password /
  // not enabled), NOT an expired session — don't try to refresh or bounce
  // the user to signed-out; let the caller show the error.
  if (res.status === 401 && !_retried
      && path !== '/auth/refresh' && path !== '/auth/local/login') {
    if (!_refreshInFlight) {
      _refreshInFlight = fetch(API_BASE + '/auth/refresh',
                               { method: 'POST', credentials: 'same-origin' })
        .finally(() => { _refreshInFlight = null; });
    }
    const r = await _refreshInFlight;
    if (r.ok) return qhFetch(path, opts, true);
    window.dispatchEvent(new Event('qh:signed-out'));
    const e = new Error('Signed out.'); e.code = 'unauthenticated'; e.status = 401; throw e;
  }
  if (res.status === 204) return null;
  let body = null;
  try { body = await res.json(); } catch (err) { /* non-JSON */ }
  if (!res.ok) {
    const err = (body && body.error) || { code: 'server_error', message: 'Request failed (' + res.status + ')' };
    const e = new Error(err.message); e.code = err.code; e.status = res.status; throw e;
  }
  return body;
}

// The Slack SSO entry point — a full-page navigation (server builds the
// authorize URL with state + openid/email/profile scopes, sets the cookie
// on callback, then redirects back here).
function qhSignInWithSlack() {
  window.location.href = API_BASE + '/auth/slack/start';
}

const qhApi = {
  me:          ()      => qhFetch('/me'),
  // Enabled login methods for the sign-in screen: [{id,label,kind}] where
  // kind is "oauth" (redirect button) or "password" (username/password form).
  providers:   ()      => qhFetch('/auth/providers'),
  // Vanilla-profile login: verify a built-in local account; on success the
  // server sets the session cookie and the caller reloads to boot /api/me.
  localLogin:  (username, password) =>
               qhFetch('/auth/local/login',
                       { method: 'POST', body: JSON.stringify({ username, password }) }),
  // Change one's own local password. On success the server revokes all
  // sessions (reauth), so the caller sends the user back to the login screen.
  localChangePassword: (currentPassword, newPassword) =>
               qhFetch('/auth/local/change-password',
                       { method: 'POST',
                         body: JSON.stringify({ currentPassword, newPassword }) }),
  // Developer-facing changelog (release list derived live from git history).
  changelog:   ()      => qhFetch('/changelog'),
  signout:     ()      => qhFetch('/auth/signout', { method: 'POST' }),
  connections: ()      => qhFetch('/connections'),
  schema:      (conn, dbn) => qhFetch('/connections/' + encodeURIComponent(conn) +
                                      '/databases/' + encodeURIComponent(dbn) + '/schema'),
  // Server DB roles (super-only, enforced server-side). {roles:[{name,kind,login,sup,note}]}.
  roles:       (conn)  => qhFetch('/connections/' + encodeURIComponent(conn) + '/roles'),
  saved:       ()      => qhFetch('/saved'),
  history:     (n=50)  => qhFetch('/history?limit=' + n),
  // Reserve the id a new tab will submit under, so the number on screen is the
  // real request id from the first keystroke rather than appearing after submit.
  reserveDraft: ()      => qhFetch('/queries/draft', { method: 'POST' }),
  submit:      (body)  => qhFetch('/queries', { method: 'POST', body: JSON.stringify(body) }),
  // Batch: submit several queries as one bundle (one Slack approval round).
  submitBatch: (body)  => qhFetch('/queries/batch', { method: 'POST', body: JSON.stringify(body) }),
  status:      (id)    => qhFetch('/queries/' + id),
  // The v3 results grid is paged: it reads result.total + result.cols +
  // result.piiCols and pulls each page via result.slice(offset, count).
  // Server rows are already PII-masked, so the grid never re-masks. Until
  // server-side paging lands we slice the (capped) in-memory rows; the
  // `truncated` flag still signals when the server capped the set.
  result:      (id)    => qhFetch('/queries/' + id + '/result').then(r => {
    if (!r || r.kind === 'affected') return r || { kind: 'affected', affected: 0, message: '' };
    const rows = r.rows || [];
    return {
      kind: 'table', cols: r.cols || [], piiCols: r.piiCols || [], rows,
      // Driver-reported column types for the header tooltip (migration 083).
      // This object is assembled key by key, so any field not named here is
      // dropped — which is exactly what happened to colTypes the first time:
      // the API sent it, the grid never received it, and the tooltip kept
      // falling back to the schema-name guess that cannot resolve `id` or
      // `user_id`. Backend correctness proved nothing about the wire.
      colTypes: r.colTypes || null,
      total: (r.total != null ? r.total : rows.length), truncated: !!r.truncated,
      slice: (offset, count) => rows.slice(offset, offset + count),
      // Server-paged: fetch a window BEYOND the inline first page from /rows.
      fetchPage: (offset, count) => qhApi.rows(id, offset, count).then(rr => rr.rows || []),
    };
  }),
  // Stop a running query. The server signals the target backend and escalates
  // a cancel to closing the connection when the cancel does not land.
  cancelRun:   (id)    => qhFetch('/queries/' + id + '/cancel', { method: 'POST' }),
  rows:        (id, o, l) => qhFetch('/queries/' + id + '/rows?offset=' + o + '&limit=' + l),
  resultCsvUrl:(id)    => API_BASE + '/queries/' + id + '/result.csv',
  resultXlsxUrl:(id)   => API_BASE + '/queries/' + id + '/result.xlsx',
  saveSnippet: (body)  => qhFetch('/saved', { method: 'POST', body: JSON.stringify(body) }),
  deleteSnippet:(id)   => qhFetch('/saved/' + id, { method: 'DELETE' }),
  requestEndpoint:(b)  => qhFetch('/endpoint-requests', { method: 'POST', body: JSON.stringify(b) }),
  feedback:    (b)     => qhFetch('/feedback', { method: 'POST', body: JSON.stringify(b) }),
  // Developer notifications (approval decisions, scheduled runs, endpoint
  // grants, kill switch). Read state mirrors server-side.
  notifications:     ()  => qhFetch('/notifications'),
  notificationsRead: (b) => qhFetch('/notifications/read', { method: 'POST', body: JSON.stringify(b) }),
  // Read-only plan preview (EXPLAIN, no execution). Returns
  // {plan:{planningMs,rows,scan,nodes}, hints:[{level,text}]}.
  explain:     (b)    => qhFetch('/explain', { method: 'POST', body: JSON.stringify(b) }),
  // Authoritative tier verdict — the server runs the same query_safety
  // analysis, grant resolution and auto-approve lookup the submit path
  // uses. Returns {tier, statements, blocked, blockers, warnings,
  // grantedTier, tierExceedsGrant, willAutoApprove, requiresJustification}.
  classify:    (b)    => qhFetch('/classify', { method: 'POST', body: JSON.stringify(b) }),
  // Server-synced named workspaces (dest='server' only) + real scheduled queries.
  sessions:        ()   => qhFetch('/sessions'),
  saveSessionSrv:  (b)  => qhFetch('/sessions', { method: 'PUT', body: JSON.stringify(b) }),
  deleteSessionSrv:(id) => qhFetch('/sessions/' + encodeURIComponent(id), { method: 'DELETE' }),
  scheduled:       ()   => qhFetch('/scheduled'),
  cancelScheduledSrv:(id)=> qhFetch('/scheduled/' + encodeURIComponent(id), { method: 'DELETE' }),

  // ---- admin panel ----
  adminQueue:      ()     => qhFetch('/admin/queue'),
  adminDecision:   (id, b)=> qhFetch('/admin/queue/' + id + '/decision', { method: 'POST', body: JSON.stringify(b) }),
  adminBatchApprove:(ids) => qhFetch('/admin/queue/batch-approve', { method: 'POST', body: JSON.stringify({ ids }) }),
  adminKillGet:    ()     => qhFetch('/admin/kill'),
  adminKillSet:    (b)    => qhFetch('/admin/kill', { method: 'POST', body: JSON.stringify(b) }),
  adminGrants:     ()     => qhFetch('/admin/grants'),
  adminAddGrant:   (b)    => qhFetch('/admin/grants', { method: 'POST', body: JSON.stringify(b) }),
  adminDelGrant:   (id)   => qhFetch('/admin/grants/' + encodeURIComponent(id), { method: 'DELETE' }),
  adminAutoGrants: ()     => qhFetch('/admin/auto-grants'),
  adminAddAutoGrant:(b)   => qhFetch('/admin/auto-grants', { method: 'POST', body: JSON.stringify(b) }),
  adminDelAutoGrant:(id)  => qhFetch('/admin/auto-grants/' + encodeURIComponent(id), { method: 'DELETE' }),
  adminScopes:     ()     => qhFetch('/admin/scopes'),
  adminSaveScope:  (b)    => qhFetch('/admin/scopes', { method: 'POST', body: JSON.stringify(b) }),
  adminDelScope:   (id)   => qhFetch('/admin/scopes/' + encodeURIComponent(id), { method: 'DELETE' }),
  adminConnections:()     => qhFetch('/admin/connections'),
  // Target-server registry CRUD (super-admin). Passwords travel in the
  // credentials block on create/update and are never returned: a response
  // carries {username, configured, placeholder} per tier and nothing else.
  // The tag vocabulary, derived from the fleet — keys, their counts, and the
  // values already in use. The connection form calls this for suggestions and
  // to warn when a new key is about to become a fleet-wide filter dimension.
  //
  // It was missing when the 2026-08-15 design round landed: the ported form
  // already called it and swallowed the failure in a `.catch`, so the feature
  // degraded to an empty picker instead of an error. `qh-api.jsx` is
  // code-owned, so a design round can add a call site here and nothing tells
  // us — worth checking the client whenever a ported file gains a `qhApi.`
  // method name we do not recognise.
  adminTagKeys:()         => qhFetch('/admin/tag-keys'),
  adminCreateConnection:(b)     => qhFetch('/admin/connections', { method: 'POST', body: JSON.stringify(b) }),
  adminUpdateConnection:(conn, b)=> qhFetch('/admin/connections/' + encodeURIComponent(conn), { method: 'PATCH', body: JSON.stringify(b) }),
  // Answers {deleted, disabled, reason} — a connection with history or live
  // grants is disabled instead of removed, and that counts as success.
  adminDeleteConnection:(conn)  => qhFetch('/admin/connections/' + encodeURIComponent(conn), { method: 'DELETE' }),
  // Reachability probes. Both answer {ok, latencyMs, serverVersion, error}
  // with ok:false for a refused connection — an unreachable target is an
  // answer, not a failed request, so neither rejects.
  adminTestNewConnection:(b)    => qhFetch('/admin/connections/test', { method: 'POST', body: JSON.stringify(b) }),
  adminTestConnection:(conn)    => qhFetch('/admin/connections/' + encodeURIComponent(conn) + '/test', { method: 'POST' }),
  adminSchemaRefresh:(conn)=> qhFetch('/admin/connections/' + encodeURIComponent(conn) + '/schema-refresh', { method: 'POST' }),
  adminEndpointReqs:()    => qhFetch('/admin/endpoint-requests'),
  adminDecideEndpoint:(id, approve, note) => qhFetch('/admin/endpoint-requests/' + encodeURIComponent(String(id).replace(/^er_/, '')) + '/decision', { method: 'POST', body: JSON.stringify({ approve: !!approve, note: note || null }) }),
  // Teams + people directory (super-admin).
  adminPeople:     ()     => qhFetch('/admin/people'),
  adminTeams:      ()     => qhFetch('/admin/teams'),
  adminSaveTeam:   (t)    => t.id
    ? qhFetch('/admin/teams/' + encodeURIComponent(t.id), { method: 'PUT', body: JSON.stringify(t) })
    : qhFetch('/admin/teams', { method: 'POST', body: JSON.stringify(t) }),
  adminDelTeam:    (id)   => qhFetch('/admin/teams/' + encodeURIComponent(id), { method: 'DELETE' }),
  adminSetPersonTeams:(slackId, teamIds) => qhFetch('/admin/people/' + encodeURIComponent(slackId) + '/teams', { method: 'PUT', body: JSON.stringify({ teams: teamIds }) }),
  adminAudit:      (qs)   => qhFetch('/admin/audit' + (qs || '')),
  adminMetrics:    ()     => qhFetch('/admin/metrics'),
  adminFeedback:   ()     => qhFetch('/admin/feedback'),
  adminConfig:     ()      => qhFetch('/admin/config'),
  adminConfigSave: (changes) => qhFetch('/admin/config', { method: 'PUT', body: JSON.stringify({ changes }) }),
};

// "2 min ago" style labels for history rows (server sends ISO timestamps).
function qhTimeAgo(iso) {
  if (!iso) return '';
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + ' min ago';
  if (s < 86400) return Math.floor(s / 3600) + ' h ago';
  if (s < 172800) return 'Yesterday';
  return Math.floor(s / 86400) + ' d ago';
}

// Schedule presets → ISO run-at (UTC).
function qhScheduleToISO(when) {
  const d = new Date();
  if (when === 'In 1 hour') d.setHours(d.getHours() + 1);
  else if (when === 'Tonight 02:00') { d.setDate(d.getDate() + 1); d.setHours(2, 0, 0, 0); }
  else if (when === 'Tomorrow 09:00') { d.setDate(d.getDate() + 1); d.setHours(9, 0, 0, 0); }
  else return null;
  return d.toISOString();
}

Object.assign(window, { qhApi, qhSignInWithSlack, qhTimeAgo, qhScheduleToISO, API_BASE });
