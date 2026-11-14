// The AI assistant (M13, PR C): the side panel of the editor and the admin form.
//
// The panel sends a prompt, and for some modes the SQL of the editor, to
// POST /api/ai/complete. The server adds the table names of the tab's database
// and lets the model read table detail through its tools. The assistant never
// runs SQL. A person puts its SQL into the editor with Insert, and runs it as
// usual. Insert marks the tab, and the next run sends `aiAssisted`, which the
// approver sees. See docs/AI.md.
//
// The admin form (super-admin) edits the settings and the providers through
// /api/admin/ai. An API key goes in and never comes back.

// mode -> [button label, placeholder, sends the editor SQL, needs a prompt]
const QH_AI_MODES = {
  generate: ['Write SQL', 'What must the SQL do? For example: orders of the last 7 days by status.', false, true],
  explain: ['Explain', 'Optional: what to explain. The assistant reads the selected SQL, or all of it.', true, false],
  optimize: ['Optimize', 'Optional: what is slow. The assistant reads the selected SQL, or all of it.', true, false],
  fix: ['Fix', 'Optional: paste the error message here. The assistant reads the selected SQL, or all of it.', true, false],
  chat: ['Chat', 'Ask about the schema or the SQL in the editor.', true, true],
};
const QH_AI_MODE_ORDER = ['generate', 'explain', 'optimize', 'fix', 'chat'];

// Text -> [{ kind: 'text' | 'code', text, lang }]. Only fenced code blocks
// become code. Everything else stays plain text, so no markup from the model
// reaches the page.
function qhAiSegments(text) {
  const out = [];
  const src = String(text || '');
  const re = /```([A-Za-z0-9_+-]*)[ \t]*\n([\s\S]*?)```/g;
  let at = 0, m;
  while ((m = re.exec(src)) !== null) {
    if (m.index > at) out.push({ kind: 'text', text: src.slice(at, m.index) });
    out.push({ kind: 'code', lang: (m[1] || '').toLowerCase(), text: m[2].replace(/\s+$/, '') });
    at = re.lastIndex;
  }
  if (at < src.length) out.push({ kind: 'text', text: src.slice(at) });
  return out.filter(s => s.kind === 'code' || s.text.trim());
}

// The "Sent:" line under an answer: what left QueryHub for that call.
function qhAiSentLine(sent) {
  if (!sent) return '';
  const bits = [];
  if (sent.promptChars) bits.push('your prompt');
  if (sent.sqlChars) bits.push(sent.sqlChars + ' characters of SQL');
  bits.push((sent.tableNames || 0) + ' table name' + (sent.tableNames === 1 ? '' : 's'));
  const read = sent.tablesRead || [];
  let line = 'Sent: ' + bits.join(', ') + '.';
  if (read.length) line += ' Read the detail of ' + read.join(', ') + '.';
  return line;
}

function QhAiCode({ seg, onInsert, onToast }) {
  const copy = () => {
    const done = (ok) => onToast && onToast(ok ? 'SQL copied.' : 'Could not copy. Select the SQL and copy it.');
    try {
      navigator.clipboard.writeText(seg.text).then(() => done(true), () => done(false));
    } catch (e) { done(false); }
  };
  return (
    <div className="qh-ai-code">
      <pre className="qh-ai-pre"><code>{seg.text}</code></pre>
      <div className="qh-ai-codebar">
        <button type="button" className="qh-btn qh-btn-primary qh-btn-sm" onClick={() => onInsert(seg.text)}
          title="Put this SQL in the editor, at the caret or over the selection">Insert</button>
        <button type="button" className="qh-btn qh-btn-ghost qh-btn-sm" onClick={copy}>Copy</button>
      </div>
    </div>
  );
}

function QhAiTurn({ turn, onInsert, onToast }) {
  if (turn.role === 'user') {
    return (
      <div className="qh-ai-turn is-user">
        <div className="qh-ai-turnhead">{QH_AI_MODES[turn.mode] ? QH_AI_MODES[turn.mode][0] : 'You'}</div>
        {turn.prompt && <div className="qh-ai-text">{turn.prompt}</div>}
        {turn.sqlLines > 0 && <div className="qh-ai-meta">With {turn.sqlLines} line{turn.sqlLines === 1 ? '' : 's'} of SQL from the editor.</div>}
      </div>
    );
  }
  if (turn.role === 'error') {
    return <div className="qh-ai-turn is-error" role="alert">{turn.text}</div>;
  }
  return (
    <div className="qh-ai-turn is-ai">
      {qhAiSegments(turn.text).map((seg, i) => seg.kind === 'code'
        ? <QhAiCode key={i} seg={seg} onInsert={onInsert} onToast={onToast} />
        : <div key={i} className="qh-ai-text">{seg.text.trim()}</div>)}
      {turn.note && <div className="qh-ai-note">{turn.note}</div>}
      <div className="qh-ai-meta">{qhAiSentLine(turn.sent)}{turn.modelName ? ' Model: ' + turn.modelName + '.' : ''}</div>
    </div>
  );
}

// The side panel. One conversation per editor tab, kept while the page is open.
function QhAiPanel({ status, tabId, conn, db, getSql, onInsert, onClose, onToast }) {
  const api = window.qhApi;
  const [mode, setMode] = React.useState('generate');
  const [prompt, setPrompt] = React.useState('');
  const [model, setModel] = React.useState(status.default || '');
  const [convos, setConvos] = React.useState({});
  const [busy, setBusy] = React.useState(false);
  const logRef = React.useRef(null);
  const turns = convos[tabId] || [];
  const meta = QH_AI_MODES[mode];
  const ready = !!(conn && db);

  React.useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns.length, busy]);

  const add = (id, items) => setConvos(c => ({ ...c, [id]: [...(c[id] || []), ...items] }));

  const send = () => {
    if (busy || !ready) return;
    const sql = meta[2] ? String(getSql() || '') : '';
    const text = prompt.trim();
    if (meta[3] && !text) { onToast && onToast('Write what you need in the prompt.'); return; }
    if (!meta[3] && !sql.trim()) { onToast && onToast('There is no SQL. Write or select SQL first.'); return; }
    // Earlier turns go back as text, so a chat can refer to them.
    const history = turns.filter(t => t.role === 'user' || t.role === 'ai')
      .map(t => ({ role: t.role === 'ai' ? 'assistant' : 'user', content: t.role === 'ai' ? t.text : (t.prompt || QH_AI_MODES[t.mode][0]) }))
      .filter(t => t.content);
    const id = tabId;
    add(id, [{ role: 'user', mode, prompt: text, sqlLines: sql.trim() ? sql.trim().split('\n').length : 0 }]);
    setPrompt('');
    setBusy(true);
    api.aiComplete({
      mode, prompt: text, sql, messages: history, connectionId: conn, databaseId: db,
      ...(status.userModelChoice && model ? { model } : null),
    }).then(r => {
      add(id, [{ role: 'ai', text: r.text || '', note: r.note || null, sent: r.sent, modelName: r.modelName }]);
    }).catch(e => {
      add(id, [{ role: 'error', text: (e && e.message) || 'The AI request failed.' }]);
    }).then(() => setBusy(false));
  };

  const onKey = (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); }
  };

  const sends = ['your prompt'];
  if (meta[2]) sends.push('the selected SQL or all of it');
  sends.push('the table names of ' + (db || 'the database'));

  return (
    <aside className="qh-ai" aria-label="AI assistant">
      <div className="qh-ai-head">
        <span className="qh-ai-title">AI assistant</span>
        {status.userModelChoice && (status.models || []).length > 1 && (
          <select className="qh-select qh-ai-model" value={model} onChange={e => setModel(e.target.value)} aria-label="Model">
            {status.models.map(m => <option key={m.id} value={m.id}>{m.provider + ' · ' + m.model}</option>)}
          </select>
        )}
        <div className="qh-flex1" />
        {turns.length > 0 && (
          <button type="button" className="qh-btn qh-btn-ghost qh-btn-sm" disabled={busy}
            onClick={() => setConvos(c => ({ ...c, [tabId]: [] }))}>Clear</button>
        )}
        <button type="button" className="qh-ai-x" onClick={onClose} aria-label="Close the AI assistant" title="Close">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18" /></svg>
        </button>
      </div>
      <div className="qh-ai-modes" role="group" aria-label="What the assistant does">
        {QH_AI_MODE_ORDER.map(k => (
          <button key={k} type="button" className={'qh-ai-mode' + (mode === k ? ' is-active' : '')}
            aria-pressed={mode === k} onClick={() => setMode(k)}>{QH_AI_MODES[k][0]}</button>
        ))}
      </div>
      <div className="qh-ai-log" ref={logRef}>
        {turns.length === 0 && (
          <div className="qh-ai-empty">
            The assistant writes and explains SQL for <b>{db || 'the database of this tab'}</b>. It never runs SQL. You read its SQL and run it from the editor.
          </div>
        )}
        {turns.map((t, i) => <QhAiTurn key={i} turn={t} onInsert={onInsert} onToast={onToast} />)}
        {busy && <div className="qh-ai-busy" role="status"><span className="qh-ai-spin" />Thinking…</div>}
      </div>
      <div className="qh-ai-compose">
        {!ready && <div className="qh-ai-need">Choose a connection and a database first.</div>}
        <textarea className="qh-input qh-ai-prompt" rows={3} value={prompt} placeholder={meta[1]}
          onChange={e => setPrompt(e.target.value)} onKeyDown={onKey} disabled={!ready} aria-label="Prompt" />
        <div className="qh-ai-sendrow">
          <span className="qh-ai-sends">Sends {sends.join(', ')}. Never rows or values.</span>
          <button type="button" className="qh-btn qh-btn-primary qh-btn-sm" onClick={send} disabled={busy || !ready}
            title="Send (Ctrl+Enter)">{busy ? 'Sending…' : 'Send'}</button>
        </div>
      </div>
    </aside>
  );
}

// ---- admin form ------------------------------------------------------------

function qhAiWhen(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? String(iso) : d.toLocaleString();
}

function QhAiSwitch({ on, onChange, label }) {
  return <button type="button" role="switch" aria-checked={!!on} aria-label={label}
    className={'qh-switch' + (on ? ' is-on' : '')} onClick={onChange} />;
}

function QhAiRow({ label, desc, children }) {
  return (
    <div className="qh-cfg-row">
      <div className="qh-cfg-rowmain">
        <div className="qh-cfg-rowlabel">{label}</div>
        {desc && <div className="qh-cfg-rowdesc">{desc}</div>}
      </div>
      <div className="qh-cfg-ctrl">{children}</div>
    </div>
  );
}

const QH_AI_KIND_WORD = { anthropic: 'Anthropic API', openai: 'OpenAI-compatible API' };

function QhAiProvider({ p, isDefault, sdk, busy, run, onTest, test }) {
  const [draft, setDraft] = React.useState({ name: p.name, baseUrl: p.baseUrl || '', models: (p.models || []).join(', ') });
  const [key, setKey] = React.useState('');
  const [ask, setAsk] = React.useState(null); // 'delete' | 'clear'
  const changed = draft.name !== p.name || draft.baseUrl !== (p.baseUrl || '') || draft.models !== (p.models || []).join(', ');
  const save = () => run(api => api.adminAiProviderSave(p.id, { name: draft.name, baseUrl: draft.baseUrl, models: draft.models }), 'Provider saved.');
  return (
    <div className="qh-ai-prov">
      <div className="qh-ai-provhead">
        <b>{p.name}</b>
        <span className="qh-ai-kind">{QH_AI_KIND_WORD[p.kind] || p.kind}</span>
        {isDefault && <span className="qh-ai-chip">Default</span>}
        <div className="qh-flex1" />
        <span className="qh-ai-provsw">Enabled <QhAiSwitch on={p.enabled} label={'Enable ' + p.name}
          onChange={() => run(api => api.adminAiProviderSave(p.id, { enabled: !p.enabled }), p.enabled ? 'Provider turned off.' : 'Provider turned on.')} /></span>
        <span className="qh-ai-provsw">Schema tools <QhAiSwitch on={p.supportsTools} label={'Schema tools for ' + p.name}
          onChange={() => run(api => api.adminAiProviderSave(p.id, { supportsTools: !p.supportsTools }), 'Provider saved.')} /></span>
      </div>
      {p.kind === 'anthropic' && !sdk && <div className="qh-ai-warn">The server does not have the anthropic package. Install the [ai] extra before you use this provider.</div>}
      <div className="qh-ai-provgrid">
        <label>Name<input className="qh-input" value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })} /></label>
        <label>Base URL<input className="qh-input" value={draft.baseUrl} placeholder={p.kind === 'anthropic' ? 'Empty: the Anthropic API' : 'https://…'} onChange={e => setDraft({ ...draft, baseUrl: e.target.value })} /></label>
        <label className="qh-ai-wide">Models, separated by commas<input className="qh-input" value={draft.models} onChange={e => setDraft({ ...draft, models: e.target.value })} /></label>
      </div>
      <div className="qh-ai-keyrow">
        <span className="qh-ai-keystate">{p.keySet ? 'API key set' + (p.keySetBy ? ' by ' + p.keySetBy : '') + (p.keySetAt ? ' on ' + qhAiWhen(p.keySetAt) : '') + '.' : 'No API key.'}</span>
        <input className="qh-input qh-ai-keyin" type="password" autoComplete="off" value={key} placeholder={p.keySet ? 'New API key' : 'API key'}
          onChange={e => setKey(e.target.value)} aria-label={'New API key for ' + p.name} />
        <button type="button" className="qh-btn qh-btn-ghost qh-btn-sm" disabled={busy || !key.trim()}
          onClick={() => run(api => api.adminAiProviderSave(p.id, { apiKey: key.trim() }), 'API key saved.').then(ok => { if (ok) setKey(''); })}>Save key</button>
        {p.keySet && <button type="button" className="qh-btn qh-btn-ghost qh-btn-sm" disabled={busy} onClick={() => setAsk('clear')}>Clear key</button>}
      </div>
      <div className="qh-ai-provactions">
        <button type="button" className="qh-btn qh-btn-ghost qh-btn-sm" disabled={busy || !p.keySet} onClick={onTest}>Test</button>
        {test && <span className={'qh-ai-test' + (test.ok ? ' is-ok' : ' is-bad')}>{test.ok ? 'Works: ' + (test.model || '') + ', ' + test.ms + ' ms.' : (test.message || 'The test failed.')}</span>}
        <div className="qh-flex1" />
        {changed && <button type="button" className="qh-btn qh-btn-primary qh-btn-sm" disabled={busy} onClick={save}>Save provider</button>}
        <button type="button" className="qh-btn qh-btn-ghost qh-btn-sm" disabled={busy || isDefault}
          title={isDefault ? 'Choose another default provider first' : 'Delete this provider'} onClick={() => setAsk('delete')}>Delete</button>
      </div>
      {ask === 'delete' && <InlineConfirm say={'Delete ' + p.name + ' and its API key?'} confirmLabel="Delete" danger busy={busy}
        onCancel={() => setAsk(null)} onConfirm={() => run(api => api.adminAiProviderDelete(p.id), 'Provider deleted.').then(() => setAsk(null))} />}
      {ask === 'clear' && <InlineConfirm say={'Clear the API key of ' + p.name + '? The assistant stops using it.'} confirmLabel="Clear key" danger busy={busy}
        onCancel={() => setAsk(null)} onConfirm={() => run(api => api.adminAiProviderSave(p.id, { clearKey: true }), 'API key cleared.').then(() => setAsk(null))} />}
    </div>
  );
}

function QhAiAddProvider({ presets, busy, run }) {
  const [form, setForm] = React.useState(null);
  const pick = (name) => {
    const ps = (presets || []).find(x => x.name === name);
    if (!ps) { setForm(null); return; }
    setForm({ name: ps.name === 'Other' ? '' : ps.name, kind: ps.kind, baseUrl: ps.baseUrl || '', models: (ps.models || []).join(', '), apiKey: '' });
  };
  const add = () => run(api => api.adminAiProviderAdd({ ...form, apiKey: form.apiKey.trim() || null }), 'Provider added.')
    .then(ok => { if (ok) setForm(null); });
  return (
    <div className="qh-ai-add">
      <div className="qh-ai-addhead">
        <b>Add a provider</b>
        <select className="qh-select" value="" onChange={e => pick(e.target.value)} aria-label="Choose a provider">
          <option value="">Choose a provider…</option>
          {(presets || []).map(ps => <option key={ps.name} value={ps.name}>{ps.name}</option>)}
        </select>
        <span className="qh-ai-addhint">Fills in the protocol, base URL and model names. You can change them.</span>
      </div>
      {form && (
        <div className="qh-ai-provgrid">
          <label>Name<input className="qh-input" value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></label>
          <label>Protocol<select className="qh-select" value={form.kind} onChange={e => setForm({ ...form, kind: e.target.value })}>
            <option value="anthropic">Anthropic API</option><option value="openai">OpenAI-compatible API</option></select></label>
          <label>Base URL<input className="qh-input" value={form.baseUrl} placeholder={form.kind === 'anthropic' ? 'Empty: the Anthropic API' : 'https://…'} onChange={e => setForm({ ...form, baseUrl: e.target.value })} /></label>
          <label>Models, separated by commas<input className="qh-input" value={form.models} onChange={e => setForm({ ...form, models: e.target.value })} /></label>
          <label className="qh-ai-wide">API key<input className="qh-input" type="password" autoComplete="off" value={form.apiKey} onChange={e => setForm({ ...form, apiKey: e.target.value })} /></label>
          <div className="qh-ai-wide qh-ai-provactions">
            <div className="qh-flex1" />
            <button type="button" className="qh-btn qh-btn-ghost qh-btn-sm" onClick={() => setForm(null)}>Cancel</button>
            <button type="button" className="qh-btn qh-btn-primary qh-btn-sm" disabled={busy || !form.name.trim()} onClick={add}>Add provider</button>
          </div>
        </div>
      )}
    </div>
  );
}

// Who can use the assistant: rules on everyone, people, teams or roles. A rule
// only opens the assistant. With no rule, nobody can use it.
const QH_AI_AUD_KIND = { everyone: 'Everyone', person: 'People', team: 'Teams', role: 'Roles' };
const QH_AI_AUD_ONE = { everyone: 'Everyone', person: 'Person', team: 'Team', role: 'Role' };

function qhAiAudLabel(rule, st, roles) {
  if (rule.kind === 'everyone') return 'Everyone';
  if (rule.kind === 'person') {
    const p = ((st && st.people) || []).find(x => x.handle === rule.value || x.id === rule.value);
    return p && p.name ? p.name + ' (' + rule.value + ')' : rule.value;
  }
  if (rule.kind === 'team') {
    const t = ((st && st.teams) || []).find(x => String(x.id) === String(rule.value));
    return t ? (t.name || t.displayName || ('Team ' + rule.value)) : 'Team ' + rule.value;
  }
  return (roles && roles[rule.value]) || rule.value;
}

function QhAiAudience({ aud, st, busy, run }) {
  const [kind, setKind] = React.useState('team');
  const [chosen, setChosen] = React.useState([]);     // ids: Slack ids, team ids or role keys
  const roles = (aud && aud.roles) || {};
  const rules = (aud && aud.rules) || [];
  const people = (st && st.people) || [];
  const has = (k, v) => rules.some(r => r.kind === k && String(r.value) === String(v));
  const toggle = (v) => setChosen(c => (c.indexOf(v) >= 0 ? c.filter(x => x !== v) : [...c, v]));
  const pickKind = (k) => { setKind(k); setChosen([]); };
  const n = kind === 'everyone' ? 1 : chosen.length;
  const add = () => run(api => api.adminAiAudienceAdd({ kind, values: kind === 'everyone' ? [] : chosen.map(String) }),
    n === 1 ? 'Rule added.' : n + ' rules added.').then(ok => { if (ok) setChosen([]); });
  const personName = (h) => { const p = people.find(x => x.handle === h || x.id === h); return p && p.name ? p.name : h; };
  return (
    <div className="qh-cfg-card">
      <div className="qh-cfg-cardhead"><div>
        <div className="qh-cfg-cardtitle">Who can use it</div>
        <div className="qh-cfg-carddesc">A person can use the assistant when a rule names them. Add as many people, teams and roles as you need. With no rule, nobody can use it.</div>
      </div></div>
      {aud && aud.count != null && <div className="qh-ai-audcount">{aud.count} {aud.count === 1 ? 'person can' : 'people can'} use it now.</div>}
      {rules.length === 0 && <div className="qh-ai-empty">No rule. Nobody can use the assistant.</div>}
      {rules.map(r => (
        <div key={r.id} className="qh-ai-audrow">
          <span className="qh-ai-kind">{QH_AI_AUD_ONE[r.kind] || r.kind}</span>
          <span className="qh-ai-audname">{qhAiAudLabel(r, st, roles)}</span>
          <div className="qh-flex1" />
          <button type="button" className="qh-btn qh-btn-ghost qh-btn-sm" disabled={busy}
            onClick={() => run(api => api.adminAiAudienceRemove(r.id), 'Rule removed.')}>Remove</button>
        </div>
      ))}
      <div className="qh-ai-audadd">
        <select className="qh-select" value={kind} onChange={e => pickKind(e.target.value)} aria-label="Whom to add">
          {Object.keys(QH_AI_AUD_KIND).map(k => <option key={k} value={k}>{QH_AI_AUD_KIND[k]}</option>)}
        </select>
        {kind === 'person' && (
          <div className="qh-ai-audpick">
            <PersonPick people={people} value="" onChange={v => { if (v && chosen.indexOf(v) < 0) setChosen(c => [...c, v]); }} resolve={st && st.resolvePerson} />
            {chosen.length > 0 && (
              <div className="qh-ai-audchips">
                {chosen.map(h => (
                  <span key={h} className="qh-ai-audchip">{personName(h)}
                    <button type="button" aria-label={'Remove ' + personName(h)} onClick={() => toggle(h)}>{'×'}</button>
                  </span>
                ))}
              </div>
            )}
          </div>
        )}
        {kind === 'team' && (
          <div className="qh-ai-audpick">
            <TeamMultiPick teams={(st && st.teams) || []} value={chosen} onChange={setChosen} />
          </div>
        )}
        {kind === 'role' && (
          <div className="qh-ai-audroles" role="group" aria-label="Roles">
            {Object.keys(roles).map(k => (
              <label key={k} className="qh-ai-audrole">
                <input type="checkbox" checked={chosen.indexOf(k) >= 0} disabled={has('role', k)} onChange={() => toggle(k)} />
                {roles[k]}{has('role', k) ? ' (added)' : ''}
              </label>
            ))}
          </div>
        )}
        <button type="button" className="qh-btn qh-btn-primary qh-btn-sm" disabled={busy || n === 0 || (kind === 'everyone' && has('everyone', ''))} onClick={add}>
          {kind === 'everyone' ? 'Add everyone' : n > 1 ? 'Add ' + n + ' rules' : 'Add rule'}
        </button>
      </div>
    </div>
  );
}

const QH_AI_SETTING_KEYS = ['enabled', 'defaultProvider', 'defaultModel', 'userModelChoice', 'effort', 'instructions', 'maxCallsPerHour', 'timeoutSec', 'schemaMaxTables'];

function AiAdminView({ st }) {
  const [data, setData] = React.useState(null);
  const [draft, setDraft] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [tests, setTests] = React.useState({});
  const toast = (m) => st && st.pushToast && st.pushToast(m);
  const take = (r) => { if (r && r.settings) { setData(r); setDraft({ ...r.settings }); } };

  React.useEffect(() => {
    window.qhApi.adminAi().then(take).catch(e => toast((e && e.message) || 'Could not load the AI settings.'));
  }, []);

  // One write: busy while it runs, the new state on success, a toast either way.
  const run = (call, said) => {
    setBusy(true);
    return call(window.qhApi).then(r => { take(r); toast(said); return true; })
      .catch(e => { toast((e && e.message) || 'The save failed.'); return false; })
      .then(ok => { setBusy(false); return ok; });
  };

  if (!data || !draft) return <div className="qh-apad"><div className="qh-metric-sub">Loading the AI settings…</div></div>;

  const s = data.settings;
  const changed = QH_AI_SETTING_KEYS.filter(k => draft[k] !== s[k]);
  const set = (k, v) => setDraft(d => ({ ...d, [k]: v }));
  const save = () => {
    const changes = {};
    changed.forEach(k => { changes[k] = draft[k]; });
    run(api => api.adminAiSave(changes), 'AI settings saved.');
  };
  const provs = data.providers || [];
  const defProv = provs.find(p => p.id === draft.defaultProvider);
  const num = (k) => <input className="qh-input qh-cfg-numin" type="number" value={draft[k]}
    onChange={e => set(k, e.target.value === '' ? '' : parseInt(e.target.value, 10))} />;
  const test = (p) => {
    setTests(t => ({ ...t, [p.id]: { ok: false, message: 'Testing…' } }));
    window.qhApi.adminAiTest(p.id).then(r => setTests(t => ({ ...t, [p.id]: r })))
      .catch(e => setTests(t => ({ ...t, [p.id]: { ok: false, message: (e && e.message) || 'The test failed.' } })));
  };

  return (
    <div className="qh-apad">
      <div className="qh-aview-head">
        <div>
          <div className="qh-aview-title">AI assistant</div>
          <div className="qh-aview-sub">The assistant of the web editor writes and explains SQL. It never runs SQL. It sends the prompt, the editor SQL and schema names to the provider, never rows or values. Super-admin only.</div>
        </div>
        <span className={'qh-ai-ready' + (data.ready ? ' is-on' : '')}>{data.ready ? 'On for users' : 'Off: ' + (data.notReady || 'not ready')}</span>
      </div>

      <div className="qh-cfg-sections">
        <div className="qh-cfg-card">
          <div className="qh-cfg-cardhead"><div><div className="qh-cfg-cardtitle">Settings</div></div></div>
          <QhAiRow label="Turn on the assistant" desc="Users see it when the default provider has an API key and a model.">
            <QhAiSwitch on={draft.enabled} label="Turn on the assistant" onChange={() => set('enabled', !draft.enabled)} />
          </QhAiRow>
          <QhAiRow label="Default provider">
            <select className="qh-select qh-cfg-strin" value={draft.defaultProvider == null ? '' : draft.defaultProvider}
              onChange={e => { set('defaultProvider', e.target.value === '' ? null : parseInt(e.target.value, 10)); set('defaultModel', ''); }}>
              <option value="">None</option>
              {provs.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </QhAiRow>
          <QhAiRow label="Default model" desc="Empty uses the first model of the default provider.">
            <select className="qh-select qh-cfg-strin" value={draft.defaultModel || ''} onChange={e => set('defaultModel', e.target.value)}>
              <option value="">First model</option>
              {(defProv ? defProv.models : []).map(m => <option key={m} value={m}>{m}</option>)}
            </select>
          </QhAiRow>
          <QhAiRow label="Users choose the model" desc="When on, users can pick any enabled provider and model.">
            <QhAiSwitch on={draft.userModelChoice} label="Users choose the model" onChange={() => set('userModelChoice', !draft.userModelChoice)} />
          </QhAiRow>
          <QhAiRow label="Effort" desc="For Anthropic models. Higher effort thinks longer and costs more.">
            <select className="qh-select qh-cfg-strin" value={draft.effort || ''} onChange={e => set('effort', e.target.value)}>
              {(data.efforts || ['']).map(v => <option key={v} value={v}>{v || 'Model default'}</option>)}
            </select>
          </QhAiRow>
          <QhAiRow label="Instructions" desc="Extra instructions for every request, for example the house SQL style.">
            <textarea className="qh-input qh-cfg-textarea" rows={4} value={draft.instructions || ''} onChange={e => set('instructions', e.target.value)} />
          </QhAiRow>
          <QhAiRow label="Requests per user per hour">{num('maxCallsPerHour')}</QhAiRow>
          <QhAiRow label="Timeout in seconds" desc="For one request with all its schema lookups.">{num('timeoutSec')}</QhAiRow>
          <QhAiRow label="Table names per request" desc="The model can look up more tables with its tools.">{num('schemaMaxTables')}</QhAiRow>
        </div>

        <div className="qh-cfg-card">
          <div className="qh-cfg-cardhead"><div>
            <div className="qh-cfg-cardtitle">Providers</div>
            <div className="qh-cfg-carddesc">One API key for each provider. QueryHub stores the key encrypted and never shows it again.</div>
          </div></div>
          {provs.length === 0 && <div className="qh-ai-empty">No provider yet. Add one below.</div>}
          {provs.map(p => <QhAiProvider key={p.id + ':' + (p.keySetAt || '') + ':' + p.name} p={p} isDefault={p.id === s.defaultProvider}
            sdk={data.anthropicSdk} busy={busy} run={run} onTest={() => test(p)} test={tests[p.id]} />)}
          <QhAiAddProvider presets={data.presets} busy={busy} run={run} />
        </div>

        <QhAiAudience aud={data.audience} st={st} busy={busy} run={run} />
      </div>

      {changed.length > 0 && (
        <div className="qh-cfg-savebar">
          <span className="qh-cfg-savedot" />
          <span className="qh-cfg-savetext"><b>{changed.length}</b> unsaved change{changed.length === 1 ? '' : 's'}</span>
          <div className="qh-flex1" />
          <button className="qh-btn qh-btn-ghost qh-btn-sm" onClick={() => setDraft({ ...s })}>Discard</button>
          <button className="qh-btn qh-btn-primary qh-btn-sm" disabled={busy} onClick={save}>Save changes</button>
        </div>
      )}
    </div>
  );
}

Object.assign(window, { QH_AI_MODES, qhAiSegments, qhAiSentLine, qhAiAudLabel, QhAiPanel, AiAdminView });
