// QueryHub — "What's new" page, version 2 (2026-11-23). Opened from the profile
// menu (avatar -> What's new), the home page link or the bell.
//
// Data: GET /changelog?lang=xx (qhApi.changelog). The server sends the entries
// newest first, the languages the changelog file has, the page strings for a
// language that is not English, and isAdmin. Admin entries come to admins only:
// the server decides that, not this page. Screenshots load from
// qhApi.changelogMediaUrl(name).
//
// Page strings: QH_WN_EN holds the English text for each key. In another
// language, feed.ui[key] replaces it. Those values live in the changelog file
// (i18n.<lang>.ui), not in this repository. Dates use Intl in the active
// language, so no month or day name is written here.
//
// The entries are written by hand, one for each change that a reader can see.
// The page shows no commits and no commit links.

const QH_WN_EN = {
  title: "What's new",
  subtitle: 'What changed in QueryHub, newest first.',
  buildTime: 'Build time',
  audienceLabel: 'Audience',
  audAll: 'All',
  audUser: 'Users',
  audAdmin: 'Admin',
  search: 'Search updates',
  periodLabel: 'Period',
  periodWeek: 'Last 7 days',
  periodMonth: 'Last month',
  periodQuarter: 'Last 3 months',
  periodAll: 'All time',
  categoryLabel: 'Category',
  categoryAll: 'All categories',
  languageLabel: 'Language',
  recentOne: 'Last 7 days · 1 update',
  recentMany: 'Last 7 days · {n} updates',
  recentShowAll: 'Show all {n}',
  adminBadge: 'Admin',
  actionLabel: 'What you need to do',
  before: 'Before',
  after: 'After',
  openImage: 'Open the full image',
  kindNew: 'New',
  kindImproved: 'Improved',
  kindFixed: 'Fixed',
  kindChanged: 'Changed',
  showDetails: 'Show details',
  hideDetails: 'Hide details',
  loading: 'Loading…',
  loadFailed: 'The updates did not load.',
  retry: 'Try again',
  noUpdates: 'There are no updates yet.',
  empty: 'No updates match your filters.',
  clearFilters: 'Clear filters',
};

const QH_WN_KIND_KEY = { new: 'kindNew', improved: 'kindImproved', fixed: 'kindFixed', changed: 'kindChanged' };
const QH_WN_PERIODS = [['all', 'periodAll'], ['week', 'periodWeek'], ['month', 'periodMonth'], ['quarter', 'periodQuarter']];
const QH_WN_LANG_KEY = 'qh.whatsnew.lang.v1';
// The box at the top shows this many recent headlines. A button opens the rest.
const QH_WN_RECENT_MAX = 5;

// Text from the file never shows a literal "**", even from an old entry.
function qhWnPlain(s) { return String(s == null ? '' : s).replace(/\*\*/g, ''); }

// The language choice survives a reload. Storage can be blocked: the page
// still works, it only forgets the choice.
function qhWnStoredLang() {
  try { return (window.localStorage && window.localStorage.getItem(QH_WN_LANG_KEY)) || ''; } catch (e) { return ''; }
}
function qhWnStoreLang(v) {
  try { if (window.localStorage) window.localStorage.setItem(QH_WN_LANG_KEY, v); } catch (e) { /* no storage */ }
}

// "YYYY-MM-DD" as a local date at midnight, or null.
function qhWnDay(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s || ''));
  if (!m) return null;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return isNaN(d.getTime()) ? null : d;
}
function qhWnToday() { const n = new Date(); return new Date(n.getFullYear(), n.getMonth(), n.getDate()); }
function qhWnDaysAgo(d, today) { return Math.round((today.getTime() - d.getTime()) / 86400000); }

// The first day that a period includes, or null for all time.
function qhWnCutoff(period, today) {
  const y = today.getFullYear(), m = today.getMonth(), d = today.getDate();
  if (period === 'week') return new Date(y, m, d - 6);
  // The same day n months back. A day the target month does not have becomes
  // its last day: 31 March minus one month is the end of February, not 3 March.
  const back = (n) => new Date(y, m - n, Math.min(d, new Date(y, m - n + 1, 0).getDate()));
  if (period === 'month') return back(1);
  if (period === 'quarter') return back(3);
  return null;
}

// "23 November 2026 · today" in the active language.
function qhWnDateLabel(s, lang, today) {
  const d = qhWnDay(s);
  if (!d) return qhWnPlain(s);
  let out = String(s);
  try { out = new Intl.DateTimeFormat(lang, { day: 'numeric', month: 'long', year: 'numeric' }).format(d); } catch (e) { /* raw date */ }
  const ago = qhWnDaysAgo(d, today);
  if (ago === 0 || ago === 1) {
    try { out += ' · ' + new Intl.RelativeTimeFormat(lang, { numeric: 'auto' }).format(-ago, 'day'); } catch (e) { /* no relative label */ }
  }
  return out;
}

function qhWnLower(s, lang) {
  const v = qhWnPlain(s);
  try { return v.toLocaleLowerCase(lang); } catch (e) { return v.toLowerCase(); }
}

// Search reads the headline, the summary, and the text and detail of each change.
function qhWnMatches(r, q, lang) {
  if (!q) return true;
  const parts = [r.headline, r.summary];
  for (const c of (r.changes || [])) { if (c) parts.push(c.text, c.detail); }
  return parts.some(p => qhWnLower(p, lang).includes(q));
}

function WhatsNewShot({ name, alt, label, title, onBroken }) {
  const src = qhApi.changelogMediaUrl(name);
  return (
    <figure className="qh-wn-shot">
      <figcaption className="qh-wn-shot-lbl">{label}</figcaption>
      <a className="qh-wn-shot-link" href={src} target="_blank" rel="noopener noreferrer" title={title}>
        <img src={src} alt={alt || label} loading="lazy" onError={onBroken} />
      </a>
    </figure>
  );
}

function WhatsNewCard({ rel, idx, t }) {
  const [open, setOpen] = React.useState(false);
  const [broken, setBroken] = React.useState({});
  const changes = (rel.changes || []).filter(c => c && qhWnPlain(c.text).trim());
  const hasDetail = changes.some(c => qhWnPlain(c.detail).trim());
  const shots = rel.shots || {};
  // A new screen has only "after". An image that fails to load is removed.
  const sides = ['before', 'after'].filter(k => shots[k] && !broken[k]);
  const action = rel.breaking ? qhWnPlain(rel.action).trim() : '';
  const listId = 'qh-wn-ch-' + String(rel.id || idx).replace(/[^A-Za-z0-9_-]/g, '-');
  return (
    <li className="qh-wn-card">
      <div className="qh-wn-meta">
        {rel.area && <span className="qh-wn-area">{qhWnPlain(rel.area)}</span>}
        {rel.audience === 'admin' && <span className="qh-wn-admin">{t('adminBadge')}</span>}
      </div>
      <h3 className="qh-wn-hl">{qhWnPlain(rel.headline)}</h3>
      {rel.summary && <p className="qh-wn-sum">{qhWnPlain(rel.summary)}</p>}
      {action && (
        <div className="qh-wn-act" role="note">
          <span className="qh-wn-act-lbl">{t('actionLabel')}</span>
          {action}
        </div>
      )}
      {sides.length > 0 && (
        <div className={'qh-wn-shots' + (sides.length === 1 ? ' is-one' : '')}>
          {sides.map(k => (
            <WhatsNewShot key={k} name={shots[k]} alt={qhWnPlain(shots[k + 'Alt']).trim()}
                          label={t(k)} title={t('openImage')}
                          onBroken={() => setBroken(b => ({ ...b, [k]: true }))} />
          ))}
        </div>
      )}
      {changes.length > 0 && (
        <ul className="qh-wn-changes" id={listId}>
          {changes.map((c, i) => {
            const kind = QH_WN_KIND_KEY[c.type] ? c.type : 'changed';
            const det = qhWnPlain(c.detail).trim();
            return (
              <li key={i} className="qh-wn-change">
                <div className="qh-wn-crow">
                  <span className="qh-wn-kind"><span className={'qh-wn-kdot k-' + kind} aria-hidden="true" />{t(QH_WN_KIND_KEY[kind])}</span>
                  <span className="qh-wn-ctext">{qhWnPlain(c.text)}</span>
                </div>
                {open && det && <p className="qh-wn-cdet">{det}</p>}
              </li>
            );
          })}
        </ul>
      )}
      {hasDetail && (
        <button type="button" className="qh-btn qh-btn-ghost qh-btn-sm qh-wn-toggle"
                aria-expanded={open} aria-controls={listId} onClick={() => setOpen(o => !o)}>
          {t(open ? 'hideDetails' : 'showDetails')}
        </button>
      )}
    </li>
  );
}

function WhatsNew() {
  const [lang, setLang] = React.useState(qhWnStoredLang);
  const [feed, setFeed] = React.useState(null);
  const [failed, setFailed] = React.useState(false);
  const [reload, setReload] = React.useState(0);
  const [aud, setAud] = React.useState('all');
  const [query, setQuery] = React.useState('');
  const [period, setPeriod] = React.useState('all');
  const [cat, setCat] = React.useState('');
  const listRef = React.useRef(null);
  const [jump, setJump] = React.useState(0);

  React.useEffect(() => {
    let alive = true;
    setFailed(false);
    // A list in the other language does not stay on screen while the new one loads.
    setFeed(prev => (prev && lang && prev.lang !== lang ? null : prev));
    qhApi.changelog(lang)
      .then(r => { if (alive) setFeed(r && typeof r === 'object' ? r : { releases: [] }); })
      .catch(() => { if (alive) setFailed(true); });
    return () => { alive = false; };
  }, [lang, reload]);

  // "Show all" moves the page to the list after the filters change.
  React.useEffect(() => {
    const el = listRef.current;
    if (!jump || !el || !el.scrollIntoView) return;
    const still = typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    el.scrollIntoView({ block: 'start', behavior: still ? 'auto' : 'smooth' });
  }, [jump]);

  const ui = (feed && feed.ui) || {};
  const t = (k, vars) => {
    let s = (typeof ui[k] === 'string' && ui[k]) || QH_WN_EN[k] || k;
    if (vars) for (const v of Object.keys(vars)) s = s.split('{' + v + '}').join(String(vars[v]));
    return s;
  };
  const activeLang = (feed && feed.lang) || lang || 'en';
  const languages = (feed && Array.isArray(feed.languages) && feed.languages.length) ? feed.languages : ['en'];
  const isAdmin = !!(feed && feed.isAdmin);
  const build = (typeof window !== 'undefined' && window.QH_BUILD) || {};
  const today = qhWnToday();

  // Newest first. The server sorts already, this keeps it true for any input.
  const all = ((feed && feed.releases) || [])
    .map((r, i) => ({ r, i }))
    .sort((a, b) => (a.r.date < b.r.date ? 1 : a.r.date > b.r.date ? -1 : a.i - b.i))
    .map(x => x.r);
  const adminCount = all.filter(r => r.audience === 'admin').length;
  const audNow = isAdmin ? aud : 'all';
  const byAud = audNow === 'all' ? all : all.filter(r => (r.audience === 'admin') === (audNow === 'admin'));

  const cats = [];
  const seen = new Set();
  for (const r of byAud) {
    if (r.category && !seen.has(r.category)) { seen.add(r.category); cats.push([r.category, qhWnPlain(r.area || r.category)]); }
  }

  const recent = byAud.filter(r => { const d = qhWnDay(r.date); return d && qhWnDaysAgo(d, today) <= 6; });
  const cutoff = qhWnCutoff(period, today);
  const q = qhWnLower(query.trim(), activeLang);
  const shown = byAud.filter(r => {
    if (cat && r.category !== cat) return false;
    if (cutoff) { const d = qhWnDay(r.date); if (!d || d < cutoff) return false; }
    return qhWnMatches(r, q, activeLang);
  });
  const groups = [];
  for (const r of shown) {
    const last = groups[groups.length - 1];
    if (last && last.date === r.date) last.items.push(r); else groups.push({ date: r.date, items: [r] });
  }
  const clearFilters = () => { setAud('all'); setQuery(''); setPeriod('all'); setCat(''); };
  const pickLang = (l) => { setLang(l); qhWnStoreLang(l); };
  // The list then holds exactly the recent updates that the box counts.
  const showAllRecent = () => { setQuery(''); setCat(''); setPeriod('week'); setJump(n => n + 1); };

  return (
    <div className="qh-wn" lang={activeLang}>
      <div className="qh-wn-inner">
        <header className="qh-wn-hero">
          <span className="qh-wn-hero-icn" aria-hidden="true">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12 3l1.9 3.9 4.3.6-3.1 3 .7 4.3-3.8-2-3.8 2 .7-4.3-3.1-3 4.3-.6z"/></svg>
          </span>
          <div className="qh-wn-hero-text">
            <h1 className="qh-wn-title">{t('title')}</h1>
            <p className="qh-wn-sub">{t('subtitle')}</p>
          </div>
          <div className="qh-wn-build" lang="en">
            <div className="qh-wn-build-ver">{build.version || ''}</div>
            <div className="qh-wn-build-meta">
              {build.date && <span title={t('buildTime')}>{build.date}</span>}
              {build.date && build.sha && <span className="qh-wn-dot" aria-hidden="true">·</span>}
              {build.sha && <span className="qh-wn-sha qh-mono">{build.sha}</span>}
            </div>
          </div>
        </header>

        {feed && (
          <div className="qh-wn-controls">
            {isAdmin && (
              <div className="qh-seg qh-seg-sm qh-wn-seg" role="group" aria-label={t('audienceLabel')}>
                {[['all', 'audAll'], ['user', 'audUser'], ['admin', 'audAdmin']].map(([v, k]) => (
                  <button key={v} type="button" className={'qh-seg-opt' + (aud === v ? ' is-active' : '')}
                          aria-pressed={aud === v} onClick={() => setAud(v)}>
                    {t(k)}{v === 'admin' && <span className="qh-wn-segn">{adminCount}</span>}
                  </button>
                ))}
              </div>
            )}
            <input type="search" className="qh-input qh-wn-search" placeholder={t('search')} aria-label={t('search')}
                   value={query} onChange={(e) => setQuery(e.target.value)} />
            <select className="qh-select qh-wn-select" aria-label={t('periodLabel')} value={period}
                    onChange={(e) => setPeriod(e.target.value)}>
              {QH_WN_PERIODS.map(([v, k]) => <option key={v} value={v}>{t(k)}</option>)}
            </select>
            {cats.length > 0 && (
              <select className="qh-select qh-wn-select" aria-label={t('categoryLabel')} value={cat}
                      onChange={(e) => setCat(e.target.value)}>
                <option value="">{t('categoryAll')}</option>
                {cats.map(([k, label]) => <option key={k} value={k}>{label}</option>)}
              </select>
            )}
            {languages.length > 1 && (
              <div className="qh-seg qh-seg-sm qh-wn-seg" role="group" aria-label={t('languageLabel')}>
                {languages.map(l => (
                  <button key={l} type="button" lang={l} className={'qh-seg-opt' + (activeLang === l ? ' is-active' : '')}
                          aria-pressed={activeLang === l} onClick={() => pickLang(l)}>
                    {String(l).toUpperCase()}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        {feed && recent.length > 0 && (
          <section className="qh-wn-recent" aria-label={t('periodWeek')}>
            <div className="qh-wn-recent-lbl">{recent.length === 1 ? t('recentOne') : t('recentMany', { n: recent.length })}</div>
            <ul className="qh-wn-recent-list">
              {recent.slice(0, QH_WN_RECENT_MAX).map(r => <li key={r.id || (r.date + '|' + r.headline)}>{qhWnPlain(r.headline)}</li>)}
            </ul>
            {recent.length > QH_WN_RECENT_MAX && (
              <button type="button" className="qh-btn qh-btn-ghost qh-btn-sm qh-wn-recent-all" onClick={showAllRecent}>
                {t('recentShowAll', { n: recent.length })}
              </button>
            )}
          </section>
        )}

        <div ref={listRef} className="qh-wn-feed">
          {!feed ? (
            failed ? (
              <div className="qh-wn-state" role="alert">
                <span>{t('loadFailed')}</span>
                <button type="button" className="qh-btn qh-btn-ghost qh-btn-sm" onClick={() => setReload(n => n + 1)}>{t('retry')}</button>
              </div>
            ) : (
              <div className="qh-wn-state" role="status">{t('loading')}</div>
            )
          ) : all.length === 0 ? (
            <div className="qh-wn-state">{t('noUpdates')}</div>
          ) : shown.length === 0 ? (
            <div className="qh-wn-state">
              <span>{t('empty')}</span>
              <button type="button" className="qh-btn qh-btn-ghost qh-btn-sm" onClick={clearFilters}>{t('clearFilters')}</button>
            </div>
          ) : (
            groups.map((g, gi) => (
              <section key={g.date + '-' + gi} className="qh-wn-group">
                <h2 className="qh-wn-date">{qhWnDateLabel(g.date, activeLang, today)}</h2>
                <ul className="qh-wn-cards">
                  {g.items.map((r, i) => <WhatsNewCard key={r.id || (r.date + '|' + r.headline)} rel={r} idx={gi + '-' + i} t={t} />)}
                </ul>
              </section>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { WhatsNew });
