// Download of a result in another shape (2026-10-25).
//
// CSV and Excel come from the server as plain files, and a link is enough. JSON,
// Markdown and SQL INSERT are built by the server while it reads the stored,
// masked CSV, and the server refuses a result with too many rows (413). A plain
// link would save that error as the file, so these go through fetch: a refusal
// shows its message, and only a good answer becomes a file.

const QH_RESULT_FORMATS = {
  'csv-bom': { ext: 'csv', label: 'CSV for Excel', viaLink: true },
  json: { ext: 'json', label: 'JSON', viaLink: false },
  md: { ext: 'md', label: 'Markdown table', viaLink: false },
  sql: { ext: 'sql', label: 'SQL INSERT', viaLink: false },
};

// Resolves to { ok: true } or { ok: false, message }. It never rejects.
// `env` lets a test pass its own fetch, document, URL and setTimeout.
async function qhDownloadResultFile(url, filename, env) {
  const e = env || {};
  const doFetch = e.fetch || window.fetch.bind(window);
  const doc = e.document || document;
  const urlApi = e.URL || URL;
  try {
    const res = await doFetch(url, { credentials: 'same-origin' });
    if (!res.ok) {
      let message = 'The download failed (' + res.status + ').';
      try {
        const body = await res.json();
        if (body && body.error && body.error.message) message = body.error.message;
      } catch (err) { /* the answer was not JSON */ }
      return { ok: false, message };
    }
    const blob = await res.blob();
    const a = doc.createElement('a');
    a.href = urlApi.createObjectURL(blob);
    a.download = filename;
    a.rel = 'noopener';
    a.click();
    (e.setTimeout || setTimeout)(() => urlApi.revokeObjectURL(a.href), 10000);
    return { ok: true };
  } catch (err) {
    return { ok: false, message: 'The download failed. Check your connection.' };
  }
}

// Generate script as (2026-11-10).
//
// The result grid copies SQL that it builds from the selected cells. This code
// only builds text. Nothing runs, and a write that the person runs later still
// goes through approval. A masked value never goes into the text: the column
// is dropped, and a comment names it.

const QH_RESULT_SCRIPT_LABELS = {
  insert: 'INSERT',
  'where-in': 'WHERE IN (…)',
  'where-andor': 'WHERE … AND … OR',
  update: 'UPDATE',
};

// The script kinds the grid offers for one engine. UPDATE is offered only on
// the engines where QueryHub runs writes.
function qhResultScriptKinds(engineId) {
  const kinds = ['insert', 'where-in', 'where-andor'];
  if (engineId === 'postgres' || engineId === 'mssql') kinds.push('update');
  return kinds;
}

// Driver and catalog type names that hold a number.
const QH_SCRIPT_NUMERIC_TYPE = /^(?:u?int\d*|integer|smallint|bigint|tinyint|mediumint|smallserial|bigserial|serial\d*|numeric|decimal|dec|real|float\d*|double(?: precision)?|number)\b/i;
const QH_SCRIPT_NUMBER_TEXT = /^-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/;

function qhScriptIdent(name, engineId) {
  if (typeof qhQuoteIdentFor === 'function') return qhQuoteIdentFor(name, engineId);
  if (typeof qhQuoteIdent === 'function') return qhQuoteIdent(name);
  return '"' + String(name).replace(/"/g, '""') + '"';
}

// One value as an SQL literal. A value in a numeric column stays unquoted only
// when its text is a plain number, so a stray string never becomes SQL code.
function qhScriptLiteral(v, type, engineId) {
  if (v == null) return 'NULL';
  // SQL Server has no true / false literal. Its bit column takes 1 and 0.
  if (typeof v === 'boolean') return engineId === 'mssql' ? (v ? '1' : '0') : (v ? 'true' : 'false');
  if (typeof v === 'number' && Number.isFinite(v)) return String(v);
  const s = typeof v === 'object' ? JSON.stringify(v) : String(v);
  if (type && QH_SCRIPT_NUMERIC_TYPE.test(String(type)) && QH_SCRIPT_NUMBER_TEXT.test(s.trim())) return s.trim();
  return (engineId === 'mssql' ? "N'" : "'") + s.replace(/'/g, "''") + "'";
}

// A column name inside a comment. A line break would end the comment.
function qhScriptCommentName(name) { return String(name).replace(/[\r\n]+/g, ' '); }

// The SQL for one script kind.
//   columns     the selected column names, in display order
//   rows        one array per selected row, aligned with `columns`
//   allColumns  every column of the result, and fullRows the whole rows
//               aligned with it (the UPDATE key comes from there)
//   table       the relation as SQL text, already quoted. Empty gives table_name.
//   masked      the names of the masked columns (a Set or an array)
//   pkCols      the primary key columns, when the caller knows them
//   types       column name -> type name, to tell numbers from strings
// Returns '' when there is nothing to write.
function qhResultScript(kind, opts) {
  const o = opts || {};
  const engineId = o.engineId || 'postgres';
  if (!qhResultScriptKinds(engineId).includes(kind)) return '';
  const columns = o.columns || [];
  const rows = o.rows || [];
  if (!columns.length || !rows.length) return '';
  const masked = o.masked instanceof Set ? o.masked : new Set(o.masked || []);
  const types = o.types || {};
  const allColumns = o.allColumns || columns;
  const fullRows = o.fullRows || [];
  const q = (n) => qhScriptIdent(n, engineId);
  const lit = (v, c) => qhScriptLiteral(v, types[c], engineId);
  const eq = (c, v) => (v == null ? q(c) + ' IS NULL' : q(c) + ' = ' + lit(v, c));

  const keep = [], skipped = [];
  columns.forEach((c, i) => (masked.has(c) ? skipped.push(qhScriptCommentName(c)) : keep.push(i)));
  const skipLine = skipped.length
    ? '-- Skipped masked column' + (skipped.length > 1 ? 's' : '') + ': ' + skipped.join(', ') : null;
  if (!keep.length) return skipLine;

  const head = [];
  if (!o.table) head.push('-- Replace table_name with the real table.');
  if (skipLine) head.push(skipLine);
  const table = o.table || 'table_name';
  const cols = keep.map(i => columns[i]);
  const vals = rows.map(r => keep.map(i => (r || [])[i]));

  // WHERE (a = 1 AND b = 2) OR (…), one line for each different row.
  const andOr = () => {
    const seen = new Set(), conds = [];
    vals.forEach((r) => {
      const s = '(' + cols.map((c, j) => eq(c, r[j])).join(' AND ') + ')';
      if (!seen.has(s)) { seen.add(s); conds.push(s); }
    });
    return conds.map((s, i) => (i ? '   OR ' : 'WHERE ') + s).join('\n');
  };

  let body = '';
  if (kind === 'insert') {
    const list = cols.map(q).join(', ');
    body = vals.map(r => 'INSERT INTO ' + table + ' (' + list + ') VALUES ('
      + r.map((v, j) => lit(v, cols[j])).join(', ') + ');').join('\n');
  } else if (kind === 'where-in') {
    if (cols.length > 1) {
      head.push('-- Several columns: written as AND / OR.');
      body = andOr();
    } else {
      const c = cols[0], seen = new Set(), list = [];
      let hasNull = false;
      vals.forEach((r) => {
        if (r[0] == null) { hasNull = true; return; }
        const s = lit(r[0], c);
        if (!seen.has(s)) { seen.add(s); list.push(s); }
      });
      body = list.length
        ? 'WHERE ' + q(c) + ' IN (' + list.join(', ') + ')' + (hasNull ? ' OR ' + q(c) + ' IS NULL' : '')
        : 'WHERE ' + q(c) + ' IS NULL';
    }
  } else if (kind === 'where-andor') {
    body = andOr();
  } else if (kind === 'update') {
    // The key: the known primary key columns that the result holds. A masked
    // key column cannot be used, because its value would go into the text.
    const keyCols = (o.pkCols || []).filter(k => allColumns.includes(k) && !masked.has(k));
    const keyIdx = keyCols.map(k => allColumns.indexOf(k));
    if (!keyCols.length) head.push('-- No primary key in the result. Add a WHERE condition before you run this.');
    const setIdx = cols.map((_, j) => j).filter(j => !keyCols.includes(cols[j]));
    if (!setIdx.length) {
      head.push('-- Every selected column is part of the key. There is nothing to set.');
      return head.join('\n');
    }
    body = vals.map((r, ri) => {
      const set = setIdx.map(j => q(cols[j]) + ' = ' + lit(r[j], cols[j])).join(', ');
      const full = fullRows[ri];
      const where = keyCols.length && full ? keyCols.map((k, n) => eq(k, full[keyIdx[n]])).join(' AND ') : '';
      return 'UPDATE ' + table + ' SET ' + set + ' WHERE ' + (where || '/* add a key */') + ';';
    }).join('\n');
  }
  return head.concat(body ? [body] : []).join('\n');
}

// The one relation in the FROM of a single SELECT, as quoted SQL text, or null.
// It reads the statement with the editor's FROM reader (qhReadFromList), so a
// join, a subquery in FROM, a set operation, a CTE or SELECT INTO gives null.
function qhScriptUnquote(t) {
  return t.k !== 'qid' ? t.v
    : t.v[0] === '"' ? t.v.slice(1, -1).replace(/""/g, '"') : t.v.slice(1, -1).replace(/\]\]/g, ']');
}

// Reads a single SELECT with one relation in its FROM. Returns its tokens, their
// paren depth and the one relation, or null.
function qhResultScriptRead(sql) {
  if (typeof qhSqlTokens !== 'function' || typeof qhReadFromList !== 'function') return null;
  const all = qhSqlTokens(String(sql || ''));
  let S = all;
  const semi = all.findIndex(t => t.v === ';');
  if (semi >= 0) {
    if (all.slice(semi + 1).some(t => t.v !== ';')) return null;   // more than one statement
    S = all.slice(0, semi);
  }
  if (!S.length || S[0].u !== 'SELECT') return null;
  const depth = [];
  let d = 0;
  for (const t of S) { if (t.v === ')') d--; depth.push(d); if (t.v === '(') d++; }
  const stop = new Set(['UNION', 'INTERSECT', 'EXCEPT', 'INTO']);
  if (S.some((t, i) => depth[i] === 0 && t.k === 'word' && stop.has(t.u))) return null;
  const refs = qhReadFromList(S, depth, 1, 0, false);
  if (!refs || refs.length !== 1) return null;
  return { S, depth, ref: refs[0] };
}

function qhResultScriptTable(sql, engineId) {
  const r = qhResultScriptRead(sql);
  return r ? r.ref.parts.map(p => qhScriptIdent(qhScriptUnquote(p), engineId)).join('.') : null;
}

// The relation's name parts, unquoted, for a catalog lookup. Null as above.
function qhResultScriptRelation(sql) {
  const r = qhResultScriptRead(sql);
  return r ? r.ref.parts.map(qhScriptUnquote) : null;
}

// The primary key columns that UPDATE may use for this statement's result.
// `schema` is the editor's catalog view (tableMeta, tableMetaQ, qualify) of the
// database that ran the statement. A key column counts only when the select
// list returns it as it is: `*`, `t.*`, `id` or `t.id`. Any other item that
// gives a column the key's name (`other AS id`, `id + 1 id`) drops the key,
// because that value may not be the key. A bare table name that two schemas
// hold gives no key.
function qhResultScriptPk(sql, schema) {
  const r = qhResultScriptRead(sql);
  const sch = schema || {};
  if (!r) return [];
  const parts = r.ref.parts.map(qhScriptUnquote);
  const pick = (map, key) => {
    if (!map || !key) return null;
    if (map[key]) return map[key];
    const lc = key.toLowerCase();
    const k = Object.keys(map).find(x => x.toLowerCase() === lc);
    return k ? map[k] : null;
  };
  let meta = null;
  if (parts.length >= 2) meta = pick(sch.tableMetaQ, parts.slice(-2).join('.'));
  else if (!(sch.qualify && sch.qualify[parts[0]] === null)) meta = pick(sch.tableMeta, parts[0]);
  const pk = (meta || []).filter(c => c && c.pk).map(c => String(c.name));
  if (!pk.length) return [];
  // The select list: the depth-0 tokens between SELECT and FROM, cut at commas.
  const { S, depth } = r;
  const from = S.findIndex((t, i) => i > 0 && depth[i] === 0 && t.u === 'FROM');
  const items = [[]];
  for (let i = 1; i < (from < 0 ? S.length : from); i++) {
    if (depth[i] === 0 && S[i].v === ',') { items.push([]); continue; }
    items[items.length - 1].push(S[i]);
  }
  const lead = new Set(['DISTINCT', 'ALL']);
  const plain = new Set(), renamed = new Set();
  let star = false;
  items.forEach((it) => {
    let toks = it;
    while (toks.length && lead.has(toks[0].u)) toks = toks.slice(1);
    const last = toks[toks.length - 1];
    if (!last) return;
    if (last.v === '*' && (toks.length === 1 || (toks.length === 3 && toks[1].v === '.'))) { star = true; return; }
    const isName = (t) => t && (t.k === 'word' || t.k === 'qid');
    if (toks.length === 1 && isName(last)) { plain.add(qhScriptUnquote(last).toLowerCase()); return; }
    if (toks.length === 3 && isName(toks[0]) && toks[1].v === '.' && isName(last)) { plain.add(qhScriptUnquote(last).toLowerCase()); return; }
    if (isName(last)) renamed.add(qhScriptUnquote(last).toLowerCase());
  });
  return pk.filter(n => !renamed.has(n.toLowerCase()) && (star || plain.has(n.toLowerCase())));
}

Object.assign(window, { QH_RESULT_FORMATS, qhDownloadResultFile,
  QH_RESULT_SCRIPT_LABELS, qhResultScriptKinds, qhResultScript, qhResultScriptTable,
  qhResultScriptRelation, qhResultScriptPk });
