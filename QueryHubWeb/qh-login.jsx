// QueryHub — Slack SSO login gate (mock; mirrors the real OIDC flow).
// Real flow: button -> Slack OIDC -> redirect back with code -> backend
// exchanges code, verifies team_id, issues session JWT. Here we simulate.

function SlackMark({ size = 22 }) {
  // Slack's 4-color glyph
  const s = size, u = size / 24;
  return (
    <svg width={s} height={s} viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <path d="M5.04 15.18a2.52 2.52 0 11-2.52-2.52h2.52v2.52z" fill="#E01E5A"/>
      <path d="M6.3 15.18a2.52 2.52 0 015.04 0v6.3a2.52 2.52 0 11-5.04 0v-6.3z" fill="#E01E5A"/>
      <path d="M8.82 5.04a2.52 2.52 0 112.52-2.52v2.52H8.82z" fill="#36C5F0"/>
      <path d="M8.82 6.3a2.52 2.52 0 010 5.04h-6.3a2.52 2.52 0 110-5.04h6.3z" fill="#36C5F0"/>
      <path d="M18.96 8.82a2.52 2.52 0 112.52 2.52h-2.52V8.82z" fill="#2EB67D"/>
      <path d="M17.7 8.82a2.52 2.52 0 01-5.04 0v-6.3a2.52 2.52 0 115.04 0v6.3z" fill="#2EB67D"/>
      <path d="M15.18 18.96a2.52 2.52 0 11-2.52 2.52v-2.52h2.52z" fill="#ECB22E"/>
      <path d="M15.18 17.7a2.52 2.52 0 010-5.04h6.3a2.52 2.52 0 110 5.04h-6.3z" fill="#ECB22E"/>
    </svg>
  );
}

// Built-in username/password sign-in (vanilla profile). Posts to
// /api/auth/local/login; on success the server sets the session cookie and we
// reload so the app's standard /api/me boot picks up the real user.
function LocalLoginForm() {
  const [username, setUsername] = React.useState('');
  const [password, setPassword] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState('');

  const submit = async (e) => {
    e.preventDefault();
    if (busy || !username.trim() || !password) return;
    setBusy(true); setError('');
    try {
      await window.qhApi.localLogin(username.trim(), password);
      window.location.reload();
    } catch (err) {
      setError((err && err.message) || 'Sign in failed.');
      setBusy(false);
    }
  };

  return (
    <form className="qh-login-form" onSubmit={submit}>
      <div className="qh-field">
        <label className="qh-field-lbl" htmlFor="qh-login-username">Username</label>
        <input id="qh-login-username" className="qh-input" type="text" autoComplete="username"
               autoCapitalize="none" spellCheck={false} autoFocus value={username}
               onChange={e => setUsername(e.target.value)} disabled={busy} />
      </div>
      <div className="qh-field">
        <label className="qh-field-lbl" htmlFor="qh-login-password">Password</label>
        <input id="qh-login-password" className="qh-input" type="password" autoComplete="current-password"
               value={password} onChange={e => setPassword(e.target.value)} disabled={busy} />
      </div>
      {error && <div className="qh-login-error" role="alert">{error}</div>}
      <button type="submit" className={'qh-slack-btn qh-login-submit' + (busy ? ' is-busy' : '')}
              disabled={busy || !username.trim() || !password}>
        {busy ? <><span className="qh-spin dark" /><span>Signing in…</span></> : <span>Sign in</span>}
      </button>
    </form>
  );
}

function LoginScreen({ onSignedIn, brand }) {
  const [phase, setPhase] = React.useState('idle'); // idle | redirecting | verifying
  // Enabled login methods from the backend. null = still loading. On any
  // error (e.g. the offline design mock) we fall back to Slack-only so the
  // screen always renders something sensible.
  const [providers, setProviders] = React.useState(null);
  // Organisation name for the "restricted to <org>" line. It comes from the
  // server (bot_config.web_org_label, returned by /auth/providers) — the design
  // constant in qh-data.jsx is a mock, and using it meant every deployment's
  // sign-in page named a company that wasn't theirs. null = don't claim one.
  const [orgLabel, setOrgLabel] = React.useState(null);

  React.useEffect(() => {
    let alive = true;
    const api = window.qhApi;
    if (!api || !api.providers) { setProviders([{ id: 'slack', kind: 'oauth' }]); return; }
    api.providers()
      .then(r => {
        if (!alive) return;
        setProviders((r && r.providers) || []);
        if (r && r.orgLabel) setOrgLabel(r.orgLabel);
      })
      .catch(() => { if (alive) setProviders([{ id: 'slack', kind: 'oauth' }]); });
    return () => { alive = false; };
  }, []);

  const signIn = () => {
    // Real Slack OIDC: a full-page navigation to the backend, which builds
    // the Slack authorize URL (state + openid/email/profile scopes), verifies
    // team_id on callback, sets the session cookie, and redirects back here —
    // the app's boot /api/me then loads the real user.
    setPhase('redirecting');
    if (window.qhSignInWithSlack) window.qhSignInWithSlack();
    else window.location.href = '/api/auth/slack/start';
  };

  const busy = phase !== 'idle';
  const list = providers || [];
  const slackOn = list.some(p => p.kind === 'oauth' || p.id === 'slack');
  const localOn = list.some(p => p.kind === 'password');

  return (
    <div className="qh-login">
      <div className="qh-login-card">
        <div className="qh-login-brand">
          <QHMark size={40} variant="green" radius={0.28} />
          <span className="qh-brand-name">QueryHub</span>
        </div>

        <h1 className="qh-login-title">Sign in to QueryHub</h1>
        <p className="qh-login-sub">A developer SQL workspace — write and submit queries, track approvals, and pull results. Read-only with a matching grant runs instantly; everything else goes to DBA review.</p>

        {providers === null && (
          <div className="qh-login-loading"><span className="qh-spin" /></div>
        )}

        {slackOn && (
          <button className={'qh-slack-btn' + (busy ? ' is-busy' : '')} onClick={signIn} disabled={busy}>
            {phase === 'idle' && <><SlackMark size={20} /><span>Sign in with Slack</span></>}
            {phase === 'redirecting' && <><span className="qh-spin dark" /><span>Opening Slack…</span></>}
            {phase === 'verifying' && <><span className="qh-spin dark" /><span>Verifying workspace…</span></>}
          </button>
        )}

        {providers !== null && !slackOn && !localOn && (
          <div className="qh-login-none" role="alert">
            No sign-in method is enabled on this deployment. An administrator has
            to turn on Slack login (<code>web_auth_slack_enabled</code>) or local
            accounts (<code>web_auth_local_enabled</code>).
          </div>
        )}

        {slackOn && localOn && <div className="qh-login-or"><span>or</span></div>}

        {localOn && <LocalLoginForm />}

        {slackOn && orgLabel && (
          <div className="qh-login-ws">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2 4 5v6c0 5 3.4 8.5 8 10 4.6-1.5 8-5 8-10V5l-8-3z"/><path d="M9 12l2 2 4-4"/></svg>
            Restricted to the <b>{orgLabel}</b> Slack workspace
          </div>
        )}

        <div className="qh-login-foot">
          {slackOn
            ? 'Approvals run in Slack or the web admin panel — never bypassing policy.'
            : 'Approvals run in the web admin panel — never bypassing policy.'}
        </div>
      </div>

      <div className="qh-login-legal">QueryHub · internal tool · access is logged to the audit trail · v {window.QH_VERSION}</div>
    </div>
  );
}

// Local-account password change. `forced` = the account is flagged
// must_change_pw and cannot proceed until it changes (no cancel). Any
// successful change revokes every session server-side, so we reload back to
// the login screen either way.
function ChangePasswordScreen({ forced, onCancel }) {
  const [cur, setCur] = React.useState('');
  const [nw, setNw] = React.useState('');
  const [cf, setCf] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState('');

  const submit = async (e) => {
    e.preventDefault();
    if (busy) return;
    if (nw.length < 8) { setError('New password must be at least 8 characters.'); return; }
    if (nw !== cf) { setError('New passwords do not match.'); return; }
    setBusy(true); setError('');
    try {
      await window.qhApi.localChangePassword(cur, nw);
      window.location.reload();   // sessions revoked → back to login
    } catch (err) {
      setError((err && err.message) || 'Could not change password.');
      setBusy(false);
    }
  };

  return (
    <div className="qh-login">
      <div className="qh-login-card">
        <div className="qh-login-brand">
          <QHMark size={40} variant="green" radius={0.28} />
          <span className="qh-brand-name">QueryHub</span>
        </div>
        <h1 className="qh-login-title">{forced ? 'Set a new password' : 'Change password'}</h1>
        <p className="qh-login-sub">{forced
          ? 'This account must set a new password before you can continue.'
          : 'For your security, changing your password signs you out on every device.'}</p>
        <form className="qh-login-form" onSubmit={submit}>
          <div className="qh-field">
            <label className="qh-field-lbl" htmlFor="qh-pw-cur">Current password</label>
            <input id="qh-pw-cur" className="qh-input" type="password" autoComplete="current-password"
                   autoFocus value={cur} onChange={e => setCur(e.target.value)} disabled={busy} />
          </div>
          <div className="qh-field">
            <label className="qh-field-lbl" htmlFor="qh-pw-new">New password</label>
            <input id="qh-pw-new" className="qh-input" type="password" autoComplete="new-password"
                   value={nw} onChange={e => setNw(e.target.value)} disabled={busy} />
          </div>
          <div className="qh-field">
            <label className="qh-field-lbl" htmlFor="qh-pw-cf">Confirm new password</label>
            <input id="qh-pw-cf" className="qh-input" type="password" autoComplete="new-password"
                   value={cf} onChange={e => setCf(e.target.value)} disabled={busy} />
          </div>
          {error && <div className="qh-login-error" role="alert">{error}</div>}
          <button type="submit" className={'qh-slack-btn qh-login-submit' + (busy ? ' is-busy' : '')}
                  disabled={busy || !cur || !nw || !cf}>
            {busy ? <><span className="qh-spin dark" /><span>Saving…</span></> : <span>Update password</span>}
          </button>
          {!forced && (
            <button type="button" className="qh-login-cancel" onClick={onCancel} disabled={busy}>Cancel</button>
          )}
        </form>
      </div>
      <div className="qh-login-legal">QueryHub · internal tool · access is logged to the audit trail</div>
    </div>
  );
}

Object.assign(window, { LoginScreen, ChangePasswordScreen, SlackMark, QHMark, qhMockAvatar });

// Stand-in for a Slack profile photo (image_192) so the avatar path renders
// offline. In production the backend returns the real Slack CDN URL; if it
// 404s or is missing, <Avatar> falls back to the user's initials.
function qhMockAvatar(initials) {
  const b = (window.qhBrand ? window.qhBrand() : { avatarFrom: '#6E8C22', avatarTo: '#C3D86A' });
  const svg = "<svg xmlns='http://www.w3.org/2000/svg' width='96' height='96'><defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'><stop offset='0' stop-color='" + b.avatarFrom + "'/><stop offset='1' stop-color='" + b.avatarTo + "'/></linearGradient></defs><rect width='96' height='96' fill='url(#g)'/><text x='48' y='62' font-family='sans-serif' font-size='40' font-weight='700' fill='#ffffff' text-anchor='middle'>" + initials + "</text></svg>";
  return 'data:image/svg+xml;utf8,' + encodeURIComponent(svg);
}

// QueryHub mark — database + centered >_ prompt. On a green squircle by default.
function QHMark({ size = 32, variant = 'green', radius = 0.26 }) {
  let fill, gcol, stroke = 'none';
  if (variant === 'green') { fill = 'var(--brand-green)'; gcol = '#fff'; }
  else if (variant === 'dark') { fill = 'var(--bg-fix-dark)'; gcol = 'var(--brand-green)'; }
  else if (variant === 'plain') { fill = 'none'; gcol = 'currentColor'; }
  else { fill = '#fff'; gcol = 'var(--brand-green)'; stroke = 'rgba(31,34,41,0.10)'; }
  const r = (radius * 100).toFixed(0);
  return (
    <svg width={size} height={size} viewBox="0 0 100 100" style={{ display: 'block', flexShrink: 0 }}>
      {fill !== 'none' && <rect x="0.5" y="0.5" width="99" height="99" rx={r} style={{ fill, stroke: stroke === 'none' ? undefined : stroke }} />}
      <g fill="none" style={{ stroke: gcol }} strokeLinecap="round" strokeLinejoin="round">
        <ellipse cx="50" cy="33" rx="24" ry="8" strokeWidth="6" />
        <path d="M26 33 V67 C26 71.4 36.7 75 50 75 C63.3 75 74 71.4 74 67 V33" strokeWidth="6" />
        <path d="M40 48 L48.5 55 L40 62" strokeWidth="5.5" />
        <path d="M52 62 L60 62" strokeWidth="5.5" />
      </g>
    </svg>
  );
}
