// QueryHub Admin — System configuration (super-admin): view & edit every
// fleet-wide setting. Real bot_config, fetched from GET /admin/config (typed +
// grouped) and written back via PUT — no mock. Draft/dirty/save so edits are
// deliberate and audited. Keeps the design's card + switch + save-bar shell.
const { useState: useCfg } = React;

const CfgLock = ({ sm }) => <svg width={sm ? 10 : 12} height={sm ? 10 : 12} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><rect x="5" y="11" width="14" height="9" rx="2" /><path d="M8 11V8a4 4 0 018 0v3" /></svg>;

const CfgIcons = {
  approval: <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2l8 3.5v5c0 5-3.4 8.6-8 10-4.6-1.4-8-5-8-10v-5L12 2z" /><path d="M9 12l2 2 4-4" /></svg>,
  execution: <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M4 13a8 8 0 1116 0" /><path d="M12 13l3.5-3.5" /><path d="M4 13h1.5M18.5 13H20" /></svg>,
  pii: <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7a11.6 11.6 0 01-4.2-.8" /><circle cx="12" cy="12" r="3" /><path d="M4 4l16 16" /></svg>,
  slack: <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M10 4H8.5a2.5 2.5 0 100 5H10zM14 20h1.5a2.5 2.5 0 100-5H14zM20 10V8.5a2.5 2.5 0 10-5 0V10zM4 14v1.5a2.5 2.5 0 105 0V14z" /></svg>,
  security: <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="8" cy="15" r="4" /><path d="M10.8 12.2L20 3M17 6l2 2M14 9l2 2" /></svg>,
  retention: <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M3 5h18v4H3zM5 9v10a1 1 0 001 1h12a1 1 0 001-1V9" /><path d="M9 13h6" /></svg>,
  database: <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"><ellipse cx="12" cy="6" rx="8" ry="3" /><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6" /></svg>,
};

// bot_config group id -> a header icon (falls back to execution).
const CFG_GROUP_ICON = {
  web: CfgIcons.security, approval: CfgIcons.approval, grants: CfgIcons.security,
  execution: CfgIcons.execution, safety: CfgIcons.approval, pii: CfgIcons.pii,
  targets: CfgIcons.database, csv: CfgIcons.database, slack: CfgIcons.slack,
  cost: CfgIcons.retention, reports: CfgIcons.retention, retention: CfgIcons.retention,
  system: CfgIcons.execution,
};

const cfgTruthy = (v) => ['1', 'true', 'yes', 'on'].includes(String(v == null ? '' : v).trim().toLowerCase());

function CfgSwitch({ on, disabled, onChange }) {
  return <button type="button" role="switch" aria-checked={on} disabled={disabled} className={'qh-switch' + (on ? ' is-on' : '')} onClick={disabled ? undefined : onChange} />;
}

function SystemConfigView({ st }) {
  const cfg = st.config || { groups: [], values: {} };
  const base = cfg.values || {};
  const [draft, setDraft] = useCfg(base);
  const [savedAt, setSavedAt] = useCfg(null);
  const [q, setQ] = useCfg('');
  // Reset the draft whenever the loaded config changes (initial load, save,
  // or a background refetch). savedAt is left alone so the "saved" note shows.
  React.useEffect(() => { setDraft(cfg.values || {}); }, [st.config]);

  const typeOf = {};
  cfg.groups.forEach(g => g.items.forEach(it => { typeOf[it.key] = it.type; }));

  const set = (k, v) => setDraft(d => ({ ...d, [k]: v }));
  const isChanged = (k) => {
    const a = draft[k], b = base[k];
    if (typeOf[k] === 'bool') return cfgTruthy(a) !== cfgTruthy(b);
    return String(a == null ? '' : a) !== String(b == null ? '' : b);
  };
  const changed = Object.keys(typeOf).filter(isChanged);
  const dirty = changed.length > 0;
  const save = () => { const patch = {}; changed.forEach(k => { patch[k] = draft[k]; }); st.saveConfig(patch); setSavedAt(Date.now()); };
  const discard = () => setDraft(base);

  // Client-side filter: a group matches by title/id (then shows all its
  // settings), or a single setting matches by key, label, or description.
  const ql = q.trim().toLowerCase();
  const visibleGroups = cfg.groups.map(sec => {
    const groupHit = ql && (sec.title + ' ' + sec.id).toLowerCase().includes(ql);
    const items = (!ql || groupHit) ? sec.items
      : sec.items.filter(it => (it.key + ' ' + it.label + ' ' + (it.description || '')).toLowerCase().includes(ql));
    return { sec, items };
  }).filter(x => x.items.length > 0);
  const shownCount = visibleGroups.reduce((n, g) => n + g.items.length, 0);

  const ctrl = (it) => {
    const v = draft[it.key];
    if (it.type === 'bool') { const on = cfgTruthy(v); return <CfgSwitch on={on} onChange={() => set(it.key, on ? 'off' : 'on')} />; }
    if (it.type === 'int') return (
      <input className="qh-input qh-cfg-numin" type="number" value={v == null ? '' : v}
        onChange={e => set(it.key, e.target.value.replace(/[^\d-]/g, ''))} />
    );
    if (it.type === 'tz') {
      // Full IANA zone list from the browser — nothing to memorize or hardcode.
      // Native <select> supports type-to-search; backend re-validates on save.
      const zones = (typeof Intl.supportedValuesOf === 'function') ? Intl.supportedValuesOf('timeZone') : [];
      const opts = zones.length ? zones : [v || 'Europe/Istanbul'];
      return (
        <select className="qh-select qh-cfg-strin" value={v == null ? '' : v} onChange={e => set(it.key, e.target.value)}>
          {v && !opts.includes(v) && <option value={v}>{v}</option>}
          {opts.map(z => <option key={z} value={z}>{z}</option>)}
        </select>
      );
    }
    // str — a textarea for long values (patterns, messages), an input otherwise.
    if (String(v == null ? '' : v).length > 48) {
      return <textarea className="qh-input qh-cfg-textarea" rows={3} value={v == null ? '' : v}
        onChange={e => set(it.key, e.target.value)} />;
    }
    return <input className="qh-input qh-cfg-strin" value={v == null ? '' : v}
      onChange={e => set(it.key, e.target.value)} />;
  };

  return (
    <div className="qh-apad">
      <div className="qh-aview-head">
        <div>
          <div className="qh-aview-title">System configuration</div>
          <div className="qh-aview-sub">Every fleet-wide setting from <span className="qh-mono">bot_config</span> — bot and web. Changes are audited and take effect at runtime (log level needs a restart). Super-admin only.</div>
        </div>
        <span className="qh-cfg-scope"><CfgLock />Super-admin</span>
      </div>

      {cfg.groups.length > 0 && (
        <div className="qh-cfg-searchrow">
          <div className="qh-search qh-cfg-search">
            <svg className="qh-search-ic" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="7" /><path d="M21 21l-4.3-4.3" /></svg>
            <input className="qh-search-in" placeholder="Search settings by name, key or description…" value={q} onChange={e => setQ(e.target.value)} />
            {q && <button className="qh-search-x" onMouseDown={e => { e.preventDefault(); setQ(''); }} aria-label="Clear"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18" /></svg></button>}
          </div>
          {ql && <span className="qh-cfg-searchcount">{shownCount} setting{shownCount === 1 ? '' : 's'} match</span>}
        </div>
      )}

      {cfg.groups.length === 0 ? (
        <div className="qh-metric-sub">Loading configuration…</div>
      ) : (
        <div className="qh-cfg-sections">
          {visibleGroups.map(({ sec, items }) => (
            <div key={sec.id} className="qh-cfg-card">
              <div className="qh-cfg-cardhead">
                <span className="qh-cfg-cardicon">{CFG_GROUP_ICON[sec.id] || CfgIcons.execution}</span>
                <div>
                  <div className="qh-cfg-cardtitle">{sec.title}</div>
                  <div className="qh-cfg-carddesc">{sec.items.length} setting{sec.items.length === 1 ? '' : 's'}</div>
                </div>
              </div>
              {items.map(it => (
                <div key={it.key} className={'qh-cfg-row' + (isChanged(it.key) ? ' is-changed' : '')}>
                  <div className="qh-cfg-rowmain">
                    <div className="qh-cfg-rowlabel">
                      {isChanged(it.key) && <span className="qh-cfg-changed" title="Unsaved change" />}
                      {it.label}
                      <span className="qh-cfg-key">{it.key}</span>
                    </div>
                    {it.description && <div className="qh-cfg-rowdesc">{it.description}</div>}
                  </div>
                  <div className="qh-cfg-ctrl">{ctrl(it)}</div>
                </div>
              ))}
            </div>
          ))}
          {visibleGroups.length === 0 && <div className="qh-cfg-empty">No settings match “{q}”.</div>}
        </div>
      )}

      {dirty ? (
        <div className="qh-cfg-savebar">
          <span className="qh-cfg-savedot" />
          <span className="qh-cfg-savetext"><b>{changed.length}</b> unsaved change{changed.length === 1 ? '' : 's'}</span>
          <div className="qh-flex1" />
          <button className="qh-btn qh-btn-ghost qh-btn-sm" onClick={discard}>Discard</button>
          <button className="qh-btn qh-btn-primary qh-btn-sm" onClick={save}>Save changes</button>
        </div>
      ) : savedAt ? (
        <div className="qh-cfg-savednote">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5" /></svg>
          Configuration saved · applied fleet-wide
        </div>
      ) : null}
    </div>
  );
}

Object.assign(window, { SystemConfigView });
