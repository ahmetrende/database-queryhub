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

// Returns the formatted text, or null when there is no formatter or it fails
// on this input. It never throws. Empty input comes back unchanged.
function qhFormatSql(sql, engineId) {
  try {
    const f = typeof window !== 'undefined' ? window.__qhSqlFormatter : null;
    if (!f || typeof f.format !== 'function') return null;
    if (typeof sql !== 'string') return null;
    if (!sql.trim()) return sql;
    const language = Object.prototype.hasOwnProperty.call(QH_FMT_LANG, engineId) ? QH_FMT_LANG[engineId] : 'sql';
    const out = f.format(sql, { language, tabWidth: 2, keywordCase: 'preserve' });
    if (typeof out !== 'string') return null;
    // The library may end the text with a newline. Drop one, so the editor
    // does not grow by an empty line on every run.
    return out.endsWith('\n') ? out.slice(0, -1) : out;
  } catch (err) {
    return null;
  }
}

Object.assign(window, { qhFormatSql });
