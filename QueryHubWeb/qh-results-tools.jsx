// Results grid tools (2026-10-25). Sort, filter, sum and pretty-print helpers,
// plus one hook that wraps a result in a sorted and filtered view.
//
// The helpers are pure. The hook needs `React` from the global, and only when
// it is called. Nothing here touches the DOM.

const QH_RES_MAX_ROWS = 5000;

const QH_RES_NUM_RE = /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;
const QH_RES_DATE_RE = /^\d{4}-\d{2}-\d{2}/;
const QH_RES_CHUNK = 2000;
const QH_RES_DISABLED_REASON =
  'Sorting and filtering work on results up to 5,000 rows. Download the result to sort a larger one.';

function qhResIsEmpty(v) {
  return v === null || v === undefined || String(v).trim() === '';
}

function qhResIsNumeric(v) {
  return QH_RES_NUM_RE.test(String(v).trim());
}

// Negative, 0 or positive. Callers must not pass empty cells.
function qhResCompareCells(a, b) {
  const an = qhResIsNumeric(a);
  const bn = qhResIsNumeric(b);
  if (an && bn) return Number(String(a).trim()) - Number(String(b).trim());
  if (an) return -1;
  if (bn) return 1;
  const sa = String(a);
  const sb = String(b);
  if (QH_RES_DATE_RE.test(sa) && QH_RES_DATE_RE.test(sb)) {
    return sa < sb ? -1 : sa > sb ? 1 : 0;
  }
  return sa.localeCompare(sb, undefined, { numeric: true, sensitivity: 'base' });
}

// Returns a new sorted array. Empty cells go last in both directions.
function qhResSortRows(rows, sort) {
  if (!sort || !sort.col) return rows;
  const col = sort.col;
  const known = rows.some((r) => r && Object.prototype.hasOwnProperty.call(r, col));
  if (!known) return rows;
  const sign = sort.dir === 'desc' ? -1 : 1;
  const items = rows.map((row, i) => ({ row, i, v: row[col] }));
  items.sort((x, y) => {
    const xe = qhResIsEmpty(x.v);
    const ye = qhResIsEmpty(y.v);
    if (xe && ye) return x.i - y.i;
    if (xe) return 1;
    if (ye) return -1;
    const c = qhResCompareCells(x.v, y.v) * sign;
    return c !== 0 ? c : x.i - y.i;
  });
  return items.map((it) => it.row);
}

function qhResCellText(v) {
  return v === null || v === undefined ? '' : String(v);
}

// Keeps rows that match the query. The form `name:text` searches one column.
function qhResFilterRows(rows, cols, query) {
  const q = String(query === null || query === undefined ? '' : query).trim();
  if (!q) return rows;
  const at = q.indexOf(':');
  if (at >= 0) {
    const name = q.slice(0, at).trim().toLowerCase();
    const column = (cols || []).find((c) => String(c).toLowerCase() === name);
    if (column !== undefined) {
      const text = q.slice(at + 1).trim().toLowerCase();
      if (!text) return rows;
      return rows.filter((r) => qhResCellText(r[column]).toLowerCase().includes(text));
    }
  }
  const needle = q.toLowerCase();
  return rows.filter((r) => (cols || []).some((c) => qhResCellText(r[c]).toLowerCase().includes(needle)));
}

function qhResAggregate(values) {
  let count = 0;
  let numeric = 0;
  let sum = 0;
  let min = null;
  let max = null;
  for (const v of values || []) {
    if (qhResIsEmpty(v)) continue;
    count += 1;
    if (!qhResIsNumeric(v)) continue;
    const n = Number(String(v).trim());
    numeric += 1;
    sum += n;
    if (min === null || n < min) min = n;
    if (max === null || n > max) max = n;
  }
  if (numeric === 0) return { count, numeric, sum: null, avg: null, min: null, max: null };
  return { count, numeric, sum, avg: sum / numeric, min, max };
}

// Returns indented JSON text for an object or array, or null.
function qhPrettyJson(text) {
  if (typeof text !== 'string') return null;
  const t = text.trim();
  if (!t.startsWith('{') && !t.startsWith('[')) return null;
  try {
    return JSON.stringify(JSON.parse(t), null, 2);
  } catch (err) {
    return null;
  }
}

function qhUseResultView(result) {
  const isTable = !!result && result.kind === 'table';
  const rowsIn = isTable && Array.isArray(result.rows) ? result.rows : [];
  const baseTotal = isTable && Number.isFinite(result.total) ? result.total : rowsIn.length;
  const enabled = isTable && !result.truncated && baseTotal <= QH_RES_MAX_ROWS;

  // Sort and filter belong to one result object. A new object starts fresh.
  const [st, setSt] = React.useState({ res: result, sort: null, filter: '' });
  const cur = st.res === result ? st : { res: result, sort: null, filter: '' };
  const [failed, setFailed] = React.useState(null);
  const [, setTick] = React.useState(0);
  const cache = React.useRef({ res: null, rows: null });

  const sort = cur.sort;
  const filter = cur.filter;
  const need = enabled && (sort !== null || filter.trim() !== '');
  const complete = enabled && rowsIn.length >= baseTotal;
  const cached = cache.current.res === result ? cache.current.rows : null;
  const allRows = need ? (complete ? rowsIn : cached) : null;
  const error = failed && failed.res === result ? failed.msg : null;

  React.useEffect(() => {
    if (!need || complete) return undefined;
    if (cache.current.res === result && cache.current.rows) return undefined;
    let cancelled = false;
    (async () => {
      try {
        let loaded = rowsIn.slice();
        while (loaded.length < baseTotal) {
          if (typeof result.fetchPage !== 'function') break;
          const chunk = await result.fetchPage(loaded.length, QH_RES_CHUNK);
          if (cancelled) return;
          if (!Array.isArray(chunk) || chunk.length === 0) break;
          loaded = loaded.concat(chunk);
        }
        if (cancelled) return;
        cache.current = { res: result, rows: loaded };
        setTick((n) => n + 1);
      } catch (err) {
        if (cancelled) return;
        setFailed({ res: result, msg: 'Could not load every row.' });
      }
    })();
    return () => { cancelled = true; };
  }, [result, need, complete]);

  const derived = React.useMemo(() => {
    if (!allRows) return null;
    return qhResSortRows(qhResFilterRows(allRows, result.cols, filter), sort);
  }, [allRows, result, filter, sort]);

  const view = React.useMemo(() => {
    if (!derived) return result;
    return {
      ...result,
      rows: derived,
      total: derived.length,
      truncated: false,
      slice: (o, c) => derived.slice(o, o + c),
      fetchPage: undefined,
    };
  }, [derived, result]);

  const setSort = React.useCallback((next) => {
    if (!isTable) return;
    setSt((prev) => ({ ...(prev.res === result ? prev : { res: result, sort: null, filter: '' }), sort: next }));
  }, [result, isTable]);
  const setFilter = React.useCallback((text) => {
    if (!isTable) return;
    setSt((prev) => ({ ...(prev.res === result ? prev : { res: result, sort: null, filter: '' }), filter: String(text) }));
  }, [result, isTable]);
  const toggleSort = React.useCallback((col) => {
    if (!isTable) return;
    setSt((prev) => {
      const base = prev.res === result ? prev : { res: result, sort: null, filter: '' };
      const s = base.sort;
      let next;
      if (!s || s.col !== col) next = { col, dir: 'asc' };
      else if (s.dir === 'asc') next = { col, dir: 'desc' };
      else next = null;
      return { ...base, sort: next };
    });
  }, [result, isTable]);

  if (!isTable) {
    return {
      view: result, enabled: false, reason: null, sort: null, setSort, toggleSort,
      filter: '', setFilter, loading: false, error: null, shown: 0, total: 0,
    };
  }
  return {
    view,
    enabled,
    reason: enabled ? null : QH_RES_DISABLED_REASON,
    sort,
    setSort,
    toggleSort,
    filter,
    setFilter,
    loading: need && !derived && !error,
    error,
    shown: derived ? derived.length : baseTotal,
    total: baseTotal,
  };
}

Object.assign(window, {
  QH_RES_MAX_ROWS, qhResCompareCells, qhResSortRows, qhResFilterRows,
  qhResAggregate, qhPrettyJson, qhUseResultView,
});
