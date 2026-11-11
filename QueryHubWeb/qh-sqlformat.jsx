// Format SQL (2026-10-25). One shared function, used by the editor shortcut.
//
// The formatter itself is the `format` function of the npm package
// `sql-formatter`. The built app attaches it as `window.__qhSqlFormatter`
// (app/src/sqlformat-bridge.js). The raw prototype has none, so the function
// returns null there and the editor does nothing.

const QH_FMT_LANG = {
  postgres: 'postgresql',
  mssql: 'transactsql',
  clickhouse: 'clickhouse',
  athena: 'trino',
};

// Format options (2026-11-11, Devart E3). The person picks them in the popup
// next to the Format button. They stay in this browser only (QH_FMT_KEY).
// Each option has a fixed list of values. The first value is the default, and
// the defaults give the same text as before the options existed.
const QH_FMT_KEY = 'qh.fmt.v1';
const QH_FMT_CHOICES = {
  keywordCase: ['preserve', 'upper', 'lower'],
  functionCase: ['preserve', 'upper', 'lower'],
  dataTypeCase: ['preserve', 'upper', 'lower'],
  identifierCase: ['preserve', 'upper', 'lower'],
  indent: ['2', '4', 'tab', '0'],
  indentStyle: ['standard', 'tabularLeft', 'tabularRight'],
  logicalOperatorNewline: ['before', 'after'],
  expressionWidth: [50, 80, 120],
  linesBetweenQueries: [1, 0, 2],
  denseOperators: [false, true],
  newlineBeforeSemicolon: [false, true],
};
const QH_FMT_DEFAULTS = Object.keys(QH_FMT_CHOICES).reduce((o, k) => { o[k] = QH_FMT_CHOICES[k][0]; return o; }, {});

// Only known keys with a value from their list. Anything else gets the default,
// so an old or edited stored value cannot reach the formatter.
function qhFmtOptionsClean(raw) {
  const src = raw && typeof raw === 'object' ? raw : {};
  const out = {};
  Object.keys(QH_FMT_CHOICES).forEach(k => {
    const v = src[k];
    out[k] = QH_FMT_CHOICES[k].some(x => x === v) ? v : QH_FMT_DEFAULTS[k];
  });
  return out;
}
function qhFmtOptionsLoad() {
  try { return qhFmtOptionsClean(JSON.parse(localStorage.getItem(QH_FMT_KEY) || 'null')); } catch (e) { return qhFmtOptionsClean(null); }
}
function qhFmtOptionsSave(opts) {
  try { localStorage.setItem(QH_FMT_KEY, JSON.stringify(qhFmtOptionsClean(opts))); } catch (e) {}
}

// Format profiles (2026-11-11, Devart E2). A profile is a named set of the
// options above. It changes only the options that it names, and every other
// option gets its default. The popup shows the profile that matches the
// stored options, or "Custom" when none matches. The popup stores no profile.
//
// The Devart texts for each profile come from the SQL Complete 6.3 release
// article. MSDN SQL follows the Microsoft "Transact-SQL syntax conventions"
// page: keywords and data types in upper case. Indented is our reading of
// "indenting makes SQL code easier to follow": a deeper indent.
// The formatter cannot make three Devart profiles:
//   * Collapsed (a block on one line): each clause gets its own line.
//   * Commas before: sql-formatter 15 removed `commaPosition`.
//   * Stack compact: no Devart page says what it does.
const QH_FMT_PROFILES = [
  ['default', 'Default', {}],
  ['compact', 'Compact', { indent: '0', linesBetweenQueries: 0 }],
  ['extended', 'Extended', { linesBetweenQueries: 2 }],
  ['indented', 'Indented', { indent: '4' }],
  ['msdn', 'MSDN SQL', { keywordCase: 'upper', dataTypeCase: 'upper' }],
  ['right', 'Right aligned', { indentStyle: 'tabularRight' }],
];

// The full options of a profile, or null for an unknown id.
function qhFmtProfileOptions(id) {
  const hit = QH_FMT_PROFILES.find(p => p[0] === id);
  return hit ? qhFmtOptionsClean(hit[2]) : null;
}

// The id of the profile that has exactly these options, or '' (Custom).
function qhFmtProfileOf(opts) {
  const o = qhFmtOptionsClean(opts);
  const hit = QH_FMT_PROFILES.find(([id]) => {
    const p = qhFmtProfileOptions(id);
    return Object.keys(QH_FMT_CHOICES).every(k => p[k] === o[k]);
  });
  return hit ? hit[0] : '';
}

// The options for the library. ClickHouse names, functions and types are case
// sensitive (`toDate`, `UInt64`). The library also reads some names as keywords
// (a table `events` became `EVENTS`). So on ClickHouse every case stays as
// typed. The other engines take all four cases.
function qhFmtLibOptions(opts, engineId) {
  const o = qhFmtOptionsClean(opts);
  const ch = engineId === 'clickhouse';
  return {
    tabWidth: o.indent === '4' ? 4 : o.indent === '0' ? 0 : 2,
    useTabs: o.indent === 'tab',
    keywordCase: ch ? 'preserve' : o.keywordCase,
    functionCase: ch ? 'preserve' : o.functionCase,
    dataTypeCase: ch ? 'preserve' : o.dataTypeCase,
    identifierCase: ch ? 'preserve' : o.identifierCase,
    indentStyle: o.indentStyle,
    logicalOperatorNewline: o.logicalOperatorNewline,
    expressionWidth: o.expressionWidth,
    linesBetweenQueries: o.linesBetweenQueries,
    denseOperators: o.denseOperators,
    newlineBeforeSemicolon: o.newlineBeforeSemicolon,
  };
}

// Returns the formatted text, or null when there is no formatter or it fails
// on this input. It never throws. Empty input comes back unchanged. With no
// `opts`, it uses the options stored in this browser.
function qhFormatSql(sql, engineId, opts) {
  try {
    const f = typeof window !== 'undefined' ? window.__qhSqlFormatter : null;
    if (!f || typeof f.format !== 'function') return null;
    if (typeof sql !== 'string') return null;
    if (!sql.trim()) return sql;
    const language = Object.prototype.hasOwnProperty.call(QH_FMT_LANG, engineId) ? QH_FMT_LANG[engineId] : 'sql';
    const out = f.format(sql, { language, ...qhFmtLibOptions(opts === undefined ? qhFmtOptionsLoad() : opts, engineId) });
    if (typeof out !== 'string') return null;
    // The library may end the text with a newline. Drop one, so the editor
    // does not grow by an empty line on every run.
    return out.endsWith('\n') ? out.slice(0, -1) : out;
  } catch (err) {
    return null;
  }
}

Object.assign(window, { qhFormatSql, QH_FMT_KEY, QH_FMT_CHOICES, QH_FMT_DEFAULTS, qhFmtOptionsClean, qhFmtOptionsLoad, qhFmtOptionsSave, qhFmtLibOptions, QH_FMT_PROFILES, qhFmtProfileOptions, qhFmtProfileOf });
