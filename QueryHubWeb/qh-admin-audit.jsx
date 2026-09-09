// QueryHub Admin — Audit trail (design brief 2026-09-09 (b)).
//
// Why this screen was rebuilt: it filtered `audit_log` through a hand-written
// list of 37 action names while the table held 132. 110 action types were
// invisible — a requester withdrawing a request, a super-admin viewing unmasked
// personal data, masking being switched off for a column, every auto-approve
// grant — and NOTHING ON THE SCREEN SAID ANYTHING WAS MISSING. The inverted
// filter (shipped) took visible types from 37 to 123 and exposed the design
// problem: seven chips, one of which had become a bucket of six unrelated
// questions.
//
// Three commitments follow from that, and they are the whole design:
//
//   1. CATEGORY IS DERIVED, NEVER LISTED. The category comes from the action
//      name's own shape (`<area>.<object>.<verb>`), computed server-side. A new
//      action type inherits a category for free. Adding one chip per new action
//      is what already failed, so this file contains no list of action names —
//      only six categories and five effects, both of which a name lands in.
//   2. A SLICE IS ALWAYS VISIBLE AS A SLICE. Every count on the screen carries
//      its denominator, unclassified actions are counted rather than absorbed,
//      and the one deliberate exclusion (per-request lifecycle) is stated on
//      screen with a control to include it. The failure was silence.
//   3. THE PAYLOAD HAS A PLACE THAT DOES NOT MOVE THE LIST. A detail rail, not
//      an expanding row: some payloads are one field and some are a paragraph,
//      and a list that reflows under the cursor is unusable for the thing this
//      screen is for — finding one specific event under time pressure.
const { useState: useAud, useEffect: useAudEf, useRef: useAudRef } = React;

// SEVEN kinds. Object-based, not verb-based: an auditor always arrives asking
// about a KIND OF THING — "who turned masking off on that column", "who enabled
// that server". The old set mixed verb-outcomes (Approvals) with objects
// (Grants), and that mix is why an unclassifiable action had nowhere to go.
//
// Two changed in round (c), both on measured data:
//   `usage` is new and is the trail's second-largest thing — 1,178 rows of
//   somebody opening the /sql modal, which was 73% of the old Access bucket and
//   is not an access event at all. It is EXCLUDED by default (below) because no
//   auditor arrives asking about it, but it is a real kind rather than a
//   permanent hide: "what was this person doing around the incident" is a real
//   question and a kind you cannot switch on cannot answer it.
//   `protection` is ordered second, ahead of `access`: once usage is removed it
//   is the LARGER of the two (444 vs 373) as well as the more sensitive.
const QH_AUD_CATS = {
  requests:    { label: 'Requests', hint: 'Approvals, rejections, change requests, withdrawals, runaway stops' },
  protection:  { label: 'Data protection', hint: 'Masking exemptions, unmasked views, exports' },
  access:      { label: 'Access', hint: 'Grants, auto-approve, roles, admins, teams, accounts' },
  connections: { label: 'Connections', hint: 'Targets, credentials, schema snapshots, migrations' },
  config:      { label: 'Configuration', hint: 'Fleet-wide settings and the kill switch' },
  usage:       { label: 'Usage', hint: 'Somebody opened a screen — no authority changed' },
  unclassified:{ label: 'Unclassified', hint: 'Action names no pattern claims yet' },
};
const QH_AUD_CAT_ORDER = ['requests', 'protection', 'access', 'connections', 'config', 'usage', 'unclassified'];
// Six effects — the second dimension, and the reason seven kinds is enough.
// *Data protection × Read* is every time somebody looked at unmasked personal
// data: two rows on the whole trail, and it is that small only because nobody
// could ask before. *Data protection × Removed* is every time masking was
// reduced — the question the five-names-for-one-event problem was hiding.
//
// `ran` is new in (c): `execution_runaway_stopped` and `execution_orphaned` are
// neither configuration nor structure, and nothing in the old set was about
// something happening during a run. It also gives the hidden lifecycle slice a
// name in the vocabulary, so including it fills an effect that already exists
// instead of distorting the other five.
const QH_AUD_EFFECTS = {
  added: 'Added', changed: 'Changed', removed: 'Removed', read: 'Read', decided: 'Decided', ran: 'Ran', other: 'Other',
};
const QH_AUD_EFFECT_ORDER = ['added', 'changed', 'removed', 'read', 'decided', 'ran', 'other'];
// THE THIRD DIMENSION, and the one that makes the biggest kind usable. 61% of
// the trail is machine activity and it is concentrated in one place: Requests is
// 6,044 rows and 78% of it is the auto-approver. A Requests filter that mixes
// 4,643 machine approvals with ~1,400 human ones answers a different question
// from the one asked — *who approved this* and *what was approved without
// anybody looking* are the two questions people actually arrive with.
//
// It is an ACTOR filter and not a split of Requests, because the human/machine
// distinction applies to every kind: the 379 job rows are mostly masking
// changes applied by a migration and security-config writes. Splitting one kind
// would not generalise; a dimension does.
const QH_AUD_ACTORS = {
  person: { label: 'A person', hint: 'Somebody signed in did this' },
  auto:   { label: 'The auto-approver', hint: 'Approved without anybody looking' },
  job:    { label: 'A job', hint: 'A background job or a migration' },
};
const QH_AUD_ACTOR_ORDER = ['person', 'auto', 'job'];

const AudIcon = {
  search: () => <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="7" /><path d="M21 21l-4.3-4.3" /></svg>,
  x: () => <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18" /></svg>,
  copy: () => <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" /><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" /></svg>,
  info: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="12" cy="12" r="9" /><path d="M12 11v5M12 8h.01" /></svg>,
};

function audNum(n) { return n == null ? '—' : Number(n).toLocaleString(); }
function audDayKey(iso) { return new Date(iso).toDateString(); }
function audDayLabel(iso) {
  const d = new Date(iso), t = new Date(), y = new Date(Date.now() - 86400000);
  if (d.toDateString() === t.toDateString()) return 'Today';
  if (d.toDateString() === y.toDateString()) return 'Yesterday';
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'long', year: d.getFullYear() === t.getFullYear() ? undefined : 'numeric' });
}
function audTime(iso) { return new Date(iso).toLocaleTimeString('en-GB', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' }); }

// The actor, in three shapes. Rendering a person for a machine action states
// something untrue, so the machine kinds are named as what they are rather than
// dressed as a user. The person/system distinction is information here, not
// decoration.
function AudActor({ actor, sm }) {
  if (!actor) return <span className="qh-audunknown">unknown</span>;
  if (actor.kind === 'auto') {
    return <span className="qh-audactor is-auto" title={actor.via ? 'Authorised by: ' + actor.via : 'Approved without anybody looking'}>Auto-approver</span>;
  }
  if (actor.kind === 'job') {
    return <span className="qh-audactor is-job" title="A background job or migration, not a person">{actor.handle}</span>;
  }
  if (actor.name) return <span className="qh-audactor">{actor.name}</span>;
  // Bare ids are resolved to names where a name exists, so what is left is a
  // principal nobody ever named. Never blank, never a fabricated name.
  return <span className={'qh-audactor is-id' + (sm ? ' sm' : '')} title="No named account — only a principal id was recorded">{actor.handle}</span>;
}

// ---------- The detail rail ----------
// Fixed width, sticky, its own scroll. The list never moves when this changes,
// which is the requirement a row that expands in place cannot meet once a
// payload is a paragraph.
function AudDetail({ row, onClose, pushToast }) {
  const [expanded, setExpanded] = useAud({});
  useAudEf(() => { setExpanded({}); }, [row && row.id]);
  if (!row) {
    return (
      <aside className="qh-auddetail is-idle">
        <div className="qh-auddetail-idle">
          <AudIcon.info />
          <div>Pick a row to see its payload — what changed, from what, to what.</div>
        </div>
      </aside>
    );
  }
  const d = row.detail || {};
  const keys = Object.keys(d).filter(k => d[k] !== undefined);
  const copy = (txt, what) => qhCopyText(txt).then(ok => pushToast && pushToast(ok ? what + ' copied.' : 'Could not copy — the browser blocked clipboard access.'));
  return (
    <aside className="qh-auddetail">
      <div className="qh-auddetail-head">
        <div className="qh-auddetail-t">{row.actionLabel || row.action}</div>
        <button className="qh-iconbtn" onClick={onClose} aria-label="Close detail"><AudIcon.x /></button>
      </div>
      <div className="qh-auddetail-body">
        <div className="qh-audkv">
          <div><dt>When</dt><dd>{new Date(row.time).toLocaleString('en-GB', { hour12: false })}</dd></div>
          <div><dt>Actor</dt><dd><AudActor actor={row.actor} /></dd></div>
          {/* The raw action name is shown deliberately: it is what the category
              is derived FROM, so an auditor questioning a classification can see
              the evidence rather than being told the answer. */}
          <div><dt>Action</dt><dd><code className="qh-audcode">{row.action}</code></dd></div>
          <div><dt>Classified as</dt><dd>{QH_AUD_CATS[row.category].label} · {QH_AUD_EFFECTS[row.effect]}
            {/* Round (b) split the action name on a dot to name the unmapped
                prefix. The names are NOT namespaced — there is no dot — so that
                rendered the whole name as though it were a prefix. It says
                "this action" now, because that is all the trail knows. */}
            {!row.classified && <div className="qh-audhint">Derived from the action name. No pattern claims <code>{row.action}</code> yet, so this row is counted as unclassified rather than filed under something it is not.</div>}</dd></div>
          {/* A machine acted under some authority, and naming it is the honest
              answer to "who approved this" when the answer is "nobody looked". */}
          {row.actor && row.actor.via && <div><dt>Authorised by</dt><dd>{row.actor.via}</dd></div>}
          {row.target && <div><dt>Target</dt><dd>{row.target}</dd></div>}
        </div>

        {/* 77% of rows attach to a request — measured, and the reverse of what
            the first brief said. The request block is the COMMON case, so it
            comes before the payload and gets the room; the 23% without one now
            get a sentence, because at this ratio the absence is the surprise and
            an auditor should know it is expected rather than missing data. */}
        {row.request ? (
          <>
            <div className="qh-auddetail-sec">Request #{row.request.id}</div>
            <div className="qh-audkv">
              <div><dt>Requester</dt><dd>{row.request.requester}</dd></div>
              <div><dt>Target</dt><dd>{row.request.connection}/{row.request.database}</dd></div>
              {row.request.tier && <div><dt>Tier</dt><dd><TierBadge tier={row.request.tier} sm /></dd></div>}
              {row.request.rows != null && <div><dt>Rows</dt><dd>{audNum(row.request.rows)}</dd></div>}
              {row.request.durationMs != null && <div><dt>Duration</dt><dd>{qhDurMs(row.request.durationMs)}</dd></div>}
            </div>
            {row.request.sql && (
              <div className="qh-audsql">
                <div className="qh-audsql-h">SQL<button className="qh-linkbtn" onClick={() => copy(row.request.sql, 'Query')}>Copy</button></div>
                <pre>{row.request.sql}</pre>
              </div>
            )}
          </>
        ) : <div className="qh-audnoreq">No request behind this one — an administrative action.</div>}

        <div className="qh-auddetail-sec">Payload
          <button className="qh-linkbtn" onClick={() => copy(JSON.stringify(d, null, 2), 'Payload')}>Copy JSON</button></div>
        {keys.length === 0
          ? <div className="qh-audhint">No payload was recorded for this row.</div>
          : <div className="qh-audkv is-payload">
            {keys.map(k => {
              const v = d[k];
              const str = typeof v === 'string';
              const long = str && v.length > 90;
              // Measured: the average payload is 60 characters, 25 rows exceed
              // 400, and the longest in the table is 9,882 — in the slice the
              // include control can expose. So a long value is clamped with its
              // own size stated: ten kilobytes of JSON must not become ten
              // kilobytes of scrolling before the next field.
              const huge = str && v.length > 600;
              const open = expanded[k];
              return <div key={k} className={long ? 'is-long' : ''}>
                <dt>{k}</dt>
                <dd>{v === null ? <span className="qh-audnull">null</span>
                  : typeof v === 'boolean' ? String(v)
                    : Array.isArray(v) ? (v.length ? v.join(', ') : <span className="qh-audnull">none</span>)
                      : typeof v === 'object' ? <code className="qh-audcode">{JSON.stringify(v)}</code>
                        : huge && !open
                          ? <>{v.slice(0, 600)}…<button className="qh-linkbtn qh-audexp" onClick={() => setExpanded(x => ({ ...x, [k]: true }))}>Show all {audNum(v.length)} characters</button></>
                          : String(v)}</dd>
              </div>;
            })}
          </div>}
        {/* Nothing in the product removes an audit row, so the screen says so
            rather than offering an action that does not exist. */}
        <div className="qh-audimmutable">Audit rows are never edited or deleted.</div>
      </div>
    </aside>
  );
}

// ---------- The view ----------
function AuditView({ st }) {
  const [q, setQ] = useAud('');
  const [term, setTerm] = useAud('');
  const [cats, setCats] = useAud([]);
  const [effs, setEffs] = useAud([]);
  const [acts, setActs] = useAud([]);
  const [include, setInclude] = useAud([]);
  const [days, setDays] = useAud(0);              // 0 = everything on the trail
  const [res, setRes] = useAud(null);
  const [rows, setRows] = useAud([]);
  const [busy, setBusy] = useAud(true);
  const [sel, setSel] = useAud(null);
  const [err, setErr] = useAud(false);
  const reqRef = useAudRef(0);

  // The search is server-side over the whole table (actor, requester, target,
  // database, action, SQL and payload), so it is debounced and it OWNS the
  // result set — the chips filter what the server returned, they do not run a
  // second, different search on a page.
  useAudEf(() => { const h = setTimeout(() => setTerm(q.trim()), 260); return () => clearTimeout(h); }, [q]);

  const params = () => ({
    q: term || undefined,
    categories: cats.length ? cats : undefined,
    effects: effs.length ? effs : undefined,
    actorKinds: acts.length ? acts : undefined,
    from: days ? new Date(Date.now() - days * 86400000).toISOString() : undefined,
    include: include.length ? include : undefined,
    limit: 60,
  });

  useAudEf(() => {
    const id = ++reqRef.current;
    setBusy(true); setErr(false);
    window.qhApi.adminAuditSearch(params())
      .then(r => { if (id !== reqRef.current) return; setRes(r); setRows(r.rows || []); setBusy(false); })
      .catch(() => { if (id !== reqRef.current) return; setErr(true); setBusy(false); });
  }, [term, cats.join(','), effs.join(','), acts.join(','), days, include.join(',')]);

  const more = () => {
    if (!res || !res.cursor || busy) return;
    const id = reqRef.current;
    setBusy(true);
    window.qhApi.adminAuditSearch({ ...params(), cursor: res.cursor })
      .then(r => { if (id !== reqRef.current) return; setRes(r); setRows(x => x.concat(r.rows || [])); setBusy(false); })
      .catch(() => setBusy(false));
  };

  const toggle = (arr, set, v) => set(arr.includes(v) ? arr.filter(x => x !== v) : arr.concat([v]));
  const filtered = cats.length > 0 || effs.length > 0 || acts.length > 0;
  const facetsC = (res && res.facets.categories) || {};
  const facetsE = (res && res.facets.effects) || {};
  const facetsA = (res && res.facets.actorKinds) || {};
  const excl = (res && res.exclusions) || [];
  const hiddenNow = excl.filter(x => !x.included);
  const clearAll = () => { setCats([]); setEffs([]); setActs([]); setQ(''); setTerm(''); setDays(0); };

  // Day headers give the list a spine. Most sessions are somebody looking for
  // one event, and "which day am I in" is the orientation a flat 8,000-row list
  // never provides.
  const grouped = [];
  // Deliberately NOT counted per day: the number would be "how many of this day
  // I have loaded so far", which is not a fact about the day. The scope line
  // owns the counting, and a half-true number is what this round removes.
  rows.forEach(r => {
    const k = audDayKey(r.time);
    if (!grouped.length || grouped[grouped.length - 1].k !== k) grouped.push({ k, label: audDayLabel(r.time), rows: [] });
    grouped[grouped.length - 1].rows.push(r);
  });

  return (
    <div className="qh-apad qh-audwrap">
      <div className="qh-aview-head">
        <div><div className="qh-aview-title">Audit trail</div>
          <div className="qh-aview-sub">Every action the system recorded, immutable and attributed.</div></div>
      </div>

      {/* Search first and full width. It reaches the whole table server-side and
          is the main way people find things, so it is sized like the primary
          control rather than tucked beside the chips. */}
      <div className="qh-audsearch">
        <AudIcon.search />
        <input className="qh-audsearch-in" value={q} onChange={e => setQ(e.target.value)}
               placeholder="Search the whole trail — actor, requester, target, database, action, SQL, payload" />
        {q && <button className="qh-iconbtn" onMouseDown={e => { e.preventDefault(); setQ(''); }} aria-label="Clear search"><AudIcon.x /></button>}
      </div>

      <div className="qh-audfilters">
        <div className="qh-audfrow">
          <span className="qh-audfl">Kind</span>
          <div className="qh-chips">
            {QH_AUD_CAT_ORDER.map(k => {
              // A kind that IS an excluded slice reads "excluded" and clicking
              // it includes the slice. Showing "Usage 0" beside an exclusion
              // saying "+1,224" is two true numbers that read as a
              // contradiction, and it leaves the chip doing nothing.
              const slice = excl.find(x => x.category === k && !x.included);
              if (slice) return (
                <button key={k} title={slice.sub + ' — excluded by default'} className="qh-chip is-excluded"
                        onClick={() => { setInclude(v => v.concat([slice.id])); setSel(null); }}>
                  {QH_AUD_CATS[k].label}<span className="qh-chipn">excluded</span>
                </button>
              );
              return (
                <button key={k} title={QH_AUD_CATS[k].hint}
                        className={'qh-chip' + (cats.includes(k) ? ' is-active' : '') + (k === 'unclassified' ? ' is-unclassified' : '')}
                        onClick={() => toggle(cats, setCats, k)}>
                  {QH_AUD_CATS[k].label}
                  {/* Every chip carries the count it would show. A filter whose
                      size you can only learn by clicking it is how the old Scopes
                      bucket stayed unexamined for months. */}
                  <span className="qh-chipn">{audNum(facetsC[k] || 0)}</span>
                </button>
              );
            })}
          </div>
        </div>
        <div className="qh-audfrow">
          {/* The label is dropped with the chips: an "Effect" heading over nothing
              reads as a control that failed to load. */}
          {QH_AUD_EFFECT_ORDER.some(k => (facetsE[k] || 0) > 0 || effs.includes(k)) && <span className="qh-audfl">Effect</span>}
          <div className="qh-chips">            {QH_AUD_EFFECT_ORDER.filter(k => (facetsE[k] || 0) > 0 || effs.includes(k)).map(k => (
              <button key={k} className={'qh-chip' + (effs.includes(k) ? ' is-active' : '')} onClick={() => toggle(effs, setEffs, k)}>
                {QH_AUD_EFFECTS[k]}<span className="qh-chipn">{audNum(facetsE[k] || 0)}</span>
              </button>
            ))}
          </div>
        </div>
        {/* Actor is the third dimension, and on this trail it is the one that
            turns the biggest kind into the two questions people actually ask. */}
        <div className="qh-audfrow">
          <span className="qh-audfl">Who</span>
          <div className="qh-chips">
            {QH_AUD_ACTOR_ORDER.filter(k => (facetsA[k] || 0) > 0 || acts.includes(k)).map(k => (
              <button key={k} title={QH_AUD_ACTORS[k].hint}
                      className={'qh-chip' + (acts.includes(k) ? ' is-active' : '') + (k !== 'person' ? ' is-machine' : '')}
                      onClick={() => toggle(acts, setActs, k)}>
                {QH_AUD_ACTORS[k].label}<span className="qh-chipn">{audNum(facetsA[k] || 0)}</span>
              </button>
            ))}
          </div>
          <div className="qh-seg qh-seg-sm qh-audrange">
            {[[0, 'All time'], [7, '7 days'], [30, '30 days'], [90, '90 days']].map(([v, l]) => (
              <button key={v} className={'qh-seg-opt' + (days === v ? ' is-active' : '')} onClick={() => setDays(v)}>{l}</button>
            ))}
          </div>
        </div>
      </div>

      {/* THE SCOPE LINE. Every number carries its denominator, so "this is
          everything" and "this is a slice" can be told apart at a glance —
          which is exactly what the screen could not do before. */}
      {res && (
        <div className="qh-audscope">
          <div className="qh-audscope-l">
            <span className="qh-audscope-n">{audNum(res.matched)}</span>
            <span>
              {filtered || term
                ? <>of <b>{audNum(res.total)}</b> rows{term && <> matching “{term}”</>}{filtered && <> in the selected slice</>}</>
                : hiddenNow.length
                  ? <>of <b>{audNum(res.grandTotal)}</b> rows</>
                  : <>rows — everything the trail holds</>}
              {' · '}{res.actionTypes.total} action types
              {res.actionTypes.unclassified > 0 && <>, <b>{res.actionTypes.unclassified}</b> of them with no category yet</>}
            </span>
            {(filtered || term || days) ? <button className="qh-linkbtn" onClick={clearAll}>Clear</button> : null}
          </div>
          {/* The declared exclusions, as a LIST rather than a sentence each: a
              third one will arrive, and the line has to stay honest without
              growing a paragraph. Each states what INCLUDING it would add —
              lifecycle is 2.1× the visible trail, so "show everything" triples
              the list and must not read as a few more rows. */}
          {excl.length > 0 && (
            <div className="qh-audexcl">
              <span className="qh-audexcl-l">{hiddenNow.length ? 'Excluded' : 'Nothing excluded'}</span>
              {excl.map(x => (
                <button key={x.id} title={x.sub}
                        className={'qh-audexcl-b' + (x.included ? ' is-on' : '')}
                        onClick={() => { setInclude(v => v.includes(x.id) ? v.filter(y => y !== x.id) : v.concat([x.id])); setSel(null); }}>
                  {x.label}<span className="qh-audexcl-n">{x.included ? 'shown' : '+' + audNum(x.rows)}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="qh-audsplit">
        <div className="qh-audlist">
          {err && <div className="qh-aempty"><div>Could not load the trail.</div><button className="qh-btn qh-btn-sm" onClick={() => setDays(d => d)}>Retry</button></div>}
          {/* A search that matches nothing and a filter that matches nothing are
              different situations with different fixes, so they are different
              messages with different buttons. The old screen said "No matching
              entries" to both. */}
          {!err && !busy && rows.length === 0 && term && (
            <div className="qh-aempty">
              <div>Nothing in the trail matches “{term}”.</div>
              <div className="qh-audhint">Searched actor, requester, target, database, action name, SQL and payload across all {audNum(res ? res.total : 0)} rows{days ? ' in the selected range' : ''}.</div>
              <button className="qh-btn qh-btn-sm" onClick={() => { setQ(''); setTerm(''); }}>Clear the search</button>
            </div>
          )}
          {!err && !busy && rows.length === 0 && !term && (
            <div className="qh-aempty">
              <div>No rows of this kind{days ? ' in the last ' + days + ' days' : ''}.</div>
              <div className="qh-audhint">The trail holds {audNum(res ? res.total : 0)} rows in total — the filter, not the data, is what is empty here.</div>
              <button className="qh-btn qh-btn-sm" onClick={clearAll}>Clear the filter</button>
            </div>
          )}
          {grouped.map(g => (
            <div key={g.k} className="qh-audday">
              <div className="qh-audday-h">{g.label}</div>
              {g.rows.map(r => (
                <button key={r.id} className={'qh-audrow' + (sel && sel.id === r.id ? ' is-sel' : '')} onClick={() => setSel(r)}>
                  <span className="qh-audtime">{audTime(r.time)}</span>
                  <span className={'qh-audcat is-' + r.category} title={QH_AUD_CATS[r.category].hint}>{QH_AUD_CATS[r.category].label}</span>
                  <span className="qh-audwhat">
                    <span className="qh-audlabel">{r.actionLabel || r.action}</span>
                    {r.target && <span className="qh-audtarget">{r.target}</span>}
                  </span>
                  <span className="qh-audby"><AudActor actor={r.actor} sm /></span>
                  {/* Request-bound fields appear only on the third of rows that
                      have them. No dashes: an administrative row has no tier, and
                      printing "—" five times says nothing five times. */}
                  <span className="qh-audreq">{r.request ? '#' + r.request.id : ''}</span>
                </button>
              ))}
            </div>
          ))}
          {busy && <div className="qh-audbusy">Loading…</div>}
          {!busy && res && res.cursor && (
            <button className="qh-btn qh-btn-sm qh-audmore" onClick={more}>Load more · {audNum(res.matched - rows.length)} left</button>
          )}
          {!busy && res && !res.cursor && rows.length > 0 && (
            <div className="qh-audend">End of the trail for this filter — all {audNum(rows.length)} rows shown.</div>
          )}
        </div>
        <AudDetail row={sel} onClose={() => setSel(null)} pushToast={st.pushToast} />
      </div>
    </div>
  );
}

Object.assign(window, { AuditView, QH_AUD_CATS, QH_AUD_EFFECTS, QH_AUD_ACTORS });
