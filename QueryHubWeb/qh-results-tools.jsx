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

// Find in results (2026-11-11, Devart J1). The filter box has three switches:
// match case, whole word and regular expression. qhResMatcher turns the text
// and the switches into one matcher. `ranges(s)` gives the [start, end] pairs
// of the matches in a cell (at most 200), for the marks. `test(s)` says if a
// cell matches. A regular expression that does not compile gives { error }.
// A whole word has no letter, digit or _ on either side, in any script.
const QH_RES_MAX_HITS = 200;
function qhResMatcher(text, opts) {
  const o = opts || {};
  const q = String(text === null || text === undefined ? '' : text);
  if (!q) return null;
  let re;
  try {
    const src = o.regex ? q : q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const body = o.word ? '(?<![\\p{L}\\p{N}_])(?:' + src + ')(?![\\p{L}\\p{N}_])' : src;
    re = new RegExp(body, 'g' + (o.word ? 'u' : '') + (o.caseSensitive ? '' : 'i'));
  } catch (err) {
    return { error: 'Not a valid regular expression.' };
  }
  const ranges = (s) => {
    const str = String(s);
    const out = [];
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(str)) !== null) {
      if (m[0] === '') { re.lastIndex += 1; continue; }   // an empty match marks nothing
      out.push([m.index, m.index + m[0].length]);
      if (out.length >= QH_RES_MAX_HITS) break;
    }
    return out;
  };
  return { test: (s) => ranges(s).length > 0, ranges };
}

// The filter text split into an optional column and the text to find. The
// form `name:text` searches one column when `name` is a column of the result.
// Returns { column, text }. `column` is undefined for every column.
function qhResFilterSplit(query, cols) {
  const q = String(query === null || query === undefined ? '' : query).trim();
  const at = q.indexOf(':');
  if (at >= 0) {
    const name = q.slice(0, at).trim().toLowerCase();
    const column = (cols || []).find((c) => String(c).toLowerCase() === name);
    if (column !== undefined) return { column, text: q.slice(at + 1).trim() };
  }
  return { column: undefined, text: q };
}

// True when a switch of the filter box is on.
function qhResOptsOn(opts) {
  return !!(opts && (opts.caseSensitive || opts.word || opts.regex));
}

// Keeps rows that match the query. The form `name:text` searches one column.
// With a switch on (qhResOptsOn), the rows that qhResMatcher finds stay. A
// regular expression that does not compile keeps every row: the box shows the
// error.
function qhResFilterRows(rows, cols, query, opts) {
  const q = String(query === null || query === undefined ? '' : query).trim();
  if (!q) return rows;
  if (qhResOptsOn(opts)) {
    const sp = qhResFilterSplit(q, cols);
    if (!sp.text) return rows;
    const m = qhResMatcher(sp.text, opts);
    if (!m || m.error) return rows;
    const scan = sp.column !== undefined ? [sp.column] : (cols || []);
    return rows.filter((r) => scan.some((c) => m.test(qhResCellText(r[c]))));
  }
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

// The sum bar of a selection. Empty cells do not count. `distinct` counts the
// different values (Devart J2). With no number among the values, `tmin` and
// `tmax` give the lowest and the highest text: dates (YYYY-MM-DD...) compare
// as text, other text compares as qhResCompareCells does. They are null when
// a number is there or nothing is.
function qhResAggregate(values) {
  let count = 0;
  let numeric = 0;
  let sum = 0;
  let min = null;
  let max = null;
  const seen = new Set();
  const texts = [];
  for (const v of values || []) {
    if (qhResIsEmpty(v)) continue;
    count += 1;
    seen.add(String(v).trim());
    if (!qhResIsNumeric(v)) { texts.push(String(v)); continue; }
    const n = Number(String(v).trim());
    numeric += 1;
    sum += n;
    if (min === null || n < min) min = n;
    if (max === null || n > max) max = n;
  }
  const distinct = seen.size;
  if (numeric === 0) {
    let tmin = null;
    let tmax = null;
    for (const t of texts) {
      if (tmin === null || qhResCompareCells(t, tmin) < 0) tmin = t;
      if (tmax === null || qhResCompareCells(t, tmax) > 0) tmax = t;
    }
    return { count, numeric, sum: null, avg: null, min: null, max: null, distinct, tmin, tmax };
  }
  return { count, numeric, sum, avg: sum / numeric, min, max, distinct, tmin: null, tmax: null };
}

// ---------------------------------------------------------------------------
// Copy a selection as CSV, JSON, XML or HTML (2026-11-11, Devart J3), and copy
// column names (Devart J4). `columns` are names. `rows` are arrays of raw cell
// values in the order of `columns`. Masked columns hold the masked text the
// server sent, as in every other copy.
// ---------------------------------------------------------------------------
const QH_RES_COPY_KINDS = [['csv', 'CSV'], ['json', 'JSON'], ['xml', 'XML'], ['html', 'HTML']];
function qhResEscXml(s) {
  // Characters that XML 1.0 does not allow are dropped.
  return String(s).replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\uFFFE\uFFFF]/g, '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
// JSON values from typed columns (2026-11-11). Web results come from the
// stored CSV file, so every cell is text and a NULL is ''. With the column's
// SQL type (result.colTypes, else the catalog type) a number column gives a
// JSON number (the text as it is, so a bigint keeps every digit), a boolean
// column gives true or false, and an empty cell of a column that is not text
// gives null (XML: null="true"). A text column keeps '' as "": there it can be
// an empty string or NULL. A value that does not read as a number or a boolean
// (a masked one) stays a string. The JSON download uses the same rule
// (result_export.json_kind in the server).
const QH_RES_JSON_NUM = /^(u?int(2|4|8|16|32|64|128|256)?|integer|smallint|bigint|tinyint|mediumint|serial|bigserial|smallserial|numeric|decimal|real|float(4|8|32|64)?|double( precision)?|oid)$/;
const QH_RES_JSON_TEXT = /^(text|varchar|char|character( varying)?|nvarchar|nchar|ntext|bpchar|name|citext|string|fixedstring|str|sysname|xml|enum(8|16)?)$/;
const QH_RES_JSON_NUMBER = /^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?$/;
function qhResJsonKind(type) {
  let t = String(type == null ? '' : type).trim().toLowerCase();
  // LowCardinality(Nullable(String)): unwrap until nothing changes.
  for (let again = true; again;) {
    again = false;
    for (const wrap of ['nullable(', 'lowcardinality(']) {
      if (t.startsWith(wrap) && t.endsWith(')')) { t = t.slice(wrap.length, -1).trim(); again = true; }
    }
  }
  if (!t) return 'text';
  if (t.endsWith('[]') || t.startsWith('array(')) return 'other';
  const base = t.split('(')[0].trim();
  if (QH_RES_JSON_NUM.test(base)) return 'num';
  if (base === 'bool' || base === 'boolean' || base === 'bit') return 'bool';
  if (QH_RES_JSON_TEXT.test(base)) return 'text';
  return 'other';
}
// The JSON text of one cell. A real number or boolean (the API sends some as
// such) stays one, and null or undefined is null in every kind.
function qhResJsonCell(v, kind) {
  if (v === null || v === undefined) return 'null';
  if (typeof v === 'number') return Number.isFinite(v) ? String(v) : JSON.stringify(String(v));
  if (typeof v === 'boolean') return String(v);
  const s = String(v);
  if (kind === 'text') return JSON.stringify(s);
  if (s === '') return 'null';
  if (kind === 'num' && QH_RES_JSON_NUMBER.test(s)) return s;
  if (kind === 'bool') {
    const low = s.trim().toLowerCase();
    if (low === 't' || low === 'true' || low === '1') return 'true';
    if (low === 'f' || low === 'false' || low === '0') return 'false';
  }
  return JSON.stringify(s);
}
// `types` (optional) maps a column name to its SQL type (see qhResJsonKind).
function qhResCopyAs(kind, columns, rows, types) {
  const cols = (columns || []).map(String);
  const data = rows || [];
  const isNull = (v) => v === null || v === undefined;
  const kinds = cols.map((c) => qhResJsonKind(types ? types[c] : null));
  if (kind === 'csv') {
    const field = (v) => {
      if (isNull(v)) return '';
      const s = String(v);
      return /[",\r\n]|^\s|\s$/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    };
    return [cols.map(field).join(',')].concat(data.map((r) => cols.map((_, i) => field(r[i])).join(','))).join('\n');
  }
  if (kind === 'json') {
    // A name that repeats (two `id` columns of a join) gets _2, _3 ...
    const used = new Map();
    const keys = cols.map((c) => {
      const n = (used.get(c) || 0) + 1;
      used.set(c, n);
      return n === 1 ? c : c + '_' + n;
    });
    // Written by hand, in JSON.stringify's two-space layout, so that a number
    // keeps its own text.
    if (!data.length) return '[]';
    return '[\n' + data.map((r) => (keys.length
      ? '  {\n' + keys.map((k, i) => '    ' + JSON.stringify(k) + ': ' + qhResJsonCell(r[i], kinds[i])).join(',\n') + '\n  }'
      : '  {}')).join(',\n') + '\n]';
  }
  if (kind === 'xml') {
    const out = ['<?xml version="1.0" encoding="UTF-8"?>', '<rows>'];
    data.forEach((r) => {
      out.push('  <row>');
      cols.forEach((c, i) => {
        const v = r[i];
        out.push(isNull(v) || (v === '' && kinds[i] !== 'text')
          ? '    <field name="' + qhResEscXml(c) + '" null="true"/>'
          : '    <field name="' + qhResEscXml(c) + '">' + qhResEscXml(v) + '</field>');
      });
      out.push('  </row>');
    });
    out.push('</rows>');
    return out.join('\n');
  }
  if (kind === 'html') {
    const esc = (v) => String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    const out = ['<table>', '  <thead>', '    <tr>' + cols.map((c) => '<th>' + esc(c) + '</th>').join('') + '</tr>', '  </thead>', '  <tbody>'];
    data.forEach((r) => {
      out.push('    <tr>' + cols.map((_, i) => '<td>' + (isNull(r[i]) ? '' : esc(r[i])) + '</td>').join('') + '</tr>');
    });
    out.push('  </tbody>', '</table>');
    return out.join('\n');
  }
  return '';
}

// Column names for the clipboard. Names only: one line, comma-separated, for a
// SELECT list. With types: one `name type` per line, comma-separated, for a
// CREATE TABLE. A column with no known type gives its name only.
function qhResColumnNames(names, types, withTypes) {
  const ns = (names || []).map(String);
  if (!withTypes) return ns.join(', ');
  const t = types || {};
  return ns.map((n) => (t[n] ? n + ' ' + t[n] : n)).join(',\n');
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

// ---------- Data viewer (M11, J6) ----------
// The full-value window shows one cell as Text, JSON, XML, HTML, Hex or Image.
// The value is the text the grid holds: a masked column arrives masked from
// the server, and every view shows that masked text. Nothing here asks the
// server for more.

const QH_VIEW_HEX_MAX = 65536;          // bytes in a hex dump; the rest is cut

// The bytes behind a cell. A PostgreSQL bytea arrives as `\x89504e47...`, a
// SQL Server varbinary as `0x89504E47...`. Anything else is its UTF-8 text.
function qhCellBytes(value) {
  const t = String(value == null ? '' : value).trim();
  const m = /^(?:\\x|0x)([0-9a-fA-F]*)$/.exec(t);
  if (m && m[1].length % 2 === 0) {
    const h = m[1], out = new Uint8Array(h.length / 2);
    for (let i = 0; i < out.length; i++) out[i] = parseInt(h.substr(i * 2, 2), 16);
    return out;
  }
  return new TextEncoder().encode(String(value == null ? '' : value));
}

// `00000000  48 65 6c 6c 6f ...  |Hello...|`, 16 bytes a line.
function qhHexDump(bytes, max = QH_VIEW_HEX_MAX) {
  const n = Math.min(bytes.length, max), lines = [];
  for (let off = 0; off < n; off += 16) {
    const row = bytes.subarray(off, Math.min(off + 16, n));
    const hex = Array.from(row, b => b.toString(16).padStart(2, '0')).join(' ');
    const asc = Array.from(row, b => (b >= 32 && b < 127 ? String.fromCharCode(b) : '.')).join('');
    lines.push(off.toString(16).padStart(8, '0') + '  ' + hex.padEnd(47, ' ') + '  |' + asc + '|');
  }
  if (bytes.length > n) lines.push('... ' + (bytes.length - n).toLocaleString('en-GB') + ' more bytes not shown');
  return lines.join('\n');
}

function qhBase64(bytes) {
  let s = '';
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

// The image type from the first bytes. SVG is left out on purpose: it is
// markup that can carry script, and the other types cannot.
function qhImageType(b) {
  if (b.length >= 8 && b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47) return 'png';
  if (b.length >= 3 && b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return 'jpeg';
  if (b.length >= 6 && b[0] === 0x47 && b[1] === 0x49 && b[2] === 0x46 && b[3] === 0x38) return 'gif';
  if (b.length >= 12 && b[0] === 0x52 && b[1] === 0x49 && b[2] === 0x46 && b[3] === 0x46
      && b[8] === 0x57 && b[9] === 0x45 && b[10] === 0x42 && b[11] === 0x50) return 'webp';
  if (b.length >= 2 && b[0] === 0x42 && b[1] === 0x4d) return 'bmp';
  return null;
}

// A `data:` URL for an image cell, or null. Accepts a data URL already, hex
// (bytea or varbinary) and plain base64. Never a remote URL: the viewer must
// not make a request that the query did not.
function qhImageDataUrl(value) {
  const t = String(value == null ? '' : value).trim();
  if (!t) return null;
  const d = /^data:image\/(png|jpeg|gif|webp|bmp);base64,([A-Za-z0-9+/=\s]+)$/.exec(t);
  if (d) return 'data:image/' + d[1] + ';base64,' + d[2].replace(/\s+/g, '');
  let bytes = null;
  if (/^(?:\\x|0x)[0-9a-fA-F]+$/.test(t)) bytes = qhCellBytes(t);
  else if (t.length >= 16 && /^[A-Za-z0-9+/\s]+={0,2}$/.test(t)) {
    try {
      const bin = atob(t.replace(/\s+/g, ''));
      bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    } catch (e) { bytes = null; }
  }
  const type = bytes && qhImageType(bytes);
  return type ? 'data:image/' + type + ';base64,' + qhBase64(bytes) : null;
}

// Indented XML, or null when the text does not parse as XML.
function qhPrettyXml(text) {
  const t = String(text == null ? '' : text).trim();
  if (!t.startsWith('<') || typeof DOMParser === 'undefined') return null;
  let doc;
  try { doc = new DOMParser().parseFromString(t, 'application/xml'); } catch (e) { return null; }
  if (!doc || !doc.documentElement || doc.getElementsByTagName('parsererror').length) return null;
  const esc = (v) => String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const out = [];
  const walk = (node, depth) => {
    const pad = '  '.repeat(depth);
    if (node.nodeType === 3) { const v = node.nodeValue.trim(); if (v) out.push(pad + esc(v)); return; }
    if (node.nodeType === 4) { out.push(pad + '<![CDATA[' + node.nodeValue + ']]>'); return; }
    if (node.nodeType === 8) { out.push(pad + '<!--' + node.nodeValue + '-->'); return; }
    if (node.nodeType === 7) { out.push(pad + '<?' + node.target + ' ' + node.data + '?>'); return; }
    if (node.nodeType !== 1) return;
    const attrs = Array.from(node.attributes, a => ' ' + a.name + '="' + esc(a.value).replace(/"/g, '&quot;') + '"').join('');
    const kids = Array.from(node.childNodes).filter(k => !(k.nodeType === 3 && !k.nodeValue.trim()));
    if (!kids.length) { out.push(pad + '<' + node.nodeName + attrs + '/>'); return; }
    if (kids.length === 1 && kids[0].nodeType === 3) {
      out.push(pad + '<' + node.nodeName + attrs + '>' + esc(kids[0].nodeValue.trim()) + '</' + node.nodeName + '>');
      return;
    }
    out.push(pad + '<' + node.nodeName + attrs + '>');
    kids.forEach(k => walk(k, depth + 1));
    out.push(pad + '</' + node.nodeName + '>');
  };
  Array.from(doc.childNodes).forEach(n => walk(n, 0));
  return out.join('\n');
}

// The document an HTML view renders. The frame has the `sandbox` attribute
// with no permission (no script, no form, no same-origin), and this policy
// blocks every network load, so a cell cannot call out or run code.
function qhHtmlSrcdoc(text) {
  return '<!doctype html><meta charset="utf-8">'
    + '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src data:">'
    + String(text == null ? '' : text);
}

// The views one value offers, in button order, and the one it opens in.
function qhViewModes(value) {
  const t = String(value == null ? '' : value).trim();
  const modes = ['text'];
  if (qhPrettyJson(t) != null) modes.push('json');
  if (t.startsWith('<') && qhPrettyXml(t) != null) modes.push('xml');
  if (t.startsWith('<') && t.indexOf('>') > 0) modes.push('html');
  modes.push('hex');
  const image = qhImageDataUrl(t) != null;
  if (image) modes.push('image');
  const initial = image ? 'image' : modes.indexOf('json') >= 0 ? 'json' : 'text';
  return { modes, initial };
}

// Where the full-value window goes. It opens at the asked point and moves up
// or left until the whole box is on the screen, `pad` pixels from each edge. A
// box larger than the screen keeps its top-left corner on the screen.
function qhPeekPlace(top, left, width, height, vw, vh, pad = 8) {
  const fit = (at, size, room) => Math.max(pad, Math.min(at, room - size - pad));
  return { top: fit(top, height, vh), left: fit(left, width, vw) };
}

const QH_VIEW_LABELS = { text: 'Text', json: 'JSON', xml: 'XML', html: 'HTML', hex: 'Hex', image: 'Image' };

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
  // The three switches of the filter box (qhResMatcher). They stay when a new
  // result arrives: they are how this person likes to search.
  const [opts, setOptsState] = React.useState({ caseSensitive: false, word: false, regex: false });
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
    return qhResSortRows(qhResFilterRows(allRows, result.cols, filter, opts), sort);
  }, [allRows, result, filter, sort, opts]);

  // The marks and the match list of the grid read this matcher. With every
  // switch off it finds the same text as the filter, in any case.
  const find = React.useMemo(() => {
    if (!isTable || !filter.trim()) return null;
    const sp = qhResFilterSplit(filter, result.cols);
    if (!sp.text) return null;
    const m = qhResMatcher(sp.text, opts);
    if (!m) return null;
    if (m.error) return { error: m.error, column: sp.column, matcher: null };
    return { error: null, column: sp.column, matcher: m };
  }, [isTable, filter, result, opts]);
  const toggleOpt = React.useCallback((key) => {
    setOptsState((o) => ({ ...o, [key]: !o[key] }));
  }, []);

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
      opts, toggleOpt, find: null,
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
    opts,
    toggleOpt,
    find: enabled ? find : null,
  };
}

Object.assign(window, { qhResJsonKind, qhResJsonCell,
  QH_RES_MAX_ROWS, qhResCompareCells, qhResSortRows, qhResFilterRows,
  qhResAggregate, qhPrettyJson, qhUseResultView,
  QH_VIEW_HEX_MAX, qhCellBytes, qhHexDump, qhImageDataUrl, qhPrettyXml, qhHtmlSrcdoc, qhViewModes, QH_VIEW_LABELS, qhPeekPlace,
  qhResMatcher, qhResFilterSplit, qhResOptsOn, QH_RES_COPY_KINDS, qhResCopyAs, qhResColumnNames,
});
