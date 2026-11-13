// QueryHub — SQL editor with line numbers, syntax highlighting, tabs.

const QH_KEYWORDS = ('SELECT INSERT UPDATE DELETE FROM WHERE JOIN INNER LEFT RIGHT FULL OUTER ON GROUP BY ORDER HAVING LIMIT OFFSET ' +
  'AS AND OR NOT NULL IS IN LIKE ILIKE BETWEEN UNION ALL DISTINCT INTO VALUES SET CREATE ALTER DROP TABLE INDEX VIEW ' +
  'TRUNCATE RENAME ADD COLUMN PRIMARY KEY FOREIGN REFERENCES DEFAULT CASE WHEN THEN ELSE END WITH RETURNING ' +
  'ASC DESC INTERVAL CURRENT_DATE NOW EXISTS GRANT REVOKE MERGE USING CASCADE CONSTRAINT').split(/\s+/);
const QH_KW_SET = new Set(QH_KEYWORDS);

// Keywords per engine and keyword phrases (2026-11-11, Devart A5).
// Each engine adds its own words to QH_KEYWORDS and drops the common words that
// it does not know: SQL Server has no LIMIT. The typing list offers a phrase as
// one item: `ord` gives ORDER BY. A phrase shows only where it can stand.
// `need` names what the statement holds before the typed word (qhKwPhraseFits).
// `engines` limits a phrase to some engines. `write` phrases change data, so a
// read-only engine does not get them.
const QH_KW_ENGINE = {
  postgres: ['LATERAL', 'FILTER', 'CONFLICT', 'FIRST', 'LAST', 'OVER', 'PARTITION', 'WINDOW'],
  mssql: ['TOP', 'APPLY', 'OUTPUT', 'PIVOT', 'UNPIVOT', 'NOLOCK', 'DECLARE', 'BEGIN', 'COMMIT', 'ROLLBACK', 'TRAN',
    'OVER', 'PARTITION', 'FETCH', 'NEXT', 'ROWS', 'ONLY'],
  clickhouse: ['PREWHERE', 'FINAL', 'SAMPLE', 'SETTINGS', 'FORMAT', 'GLOBAL', 'QUALIFY', 'ARRAY', 'OVER', 'PARTITION'],
  athena: ['UNNEST', 'ORDINALITY', 'TABLESAMPLE', 'BERNOULLI', 'FETCH', 'FIRST', 'ROWS', 'ONLY', 'OVER', 'PARTITION'],
};
const QH_KW_DROP = {
  mssql: ['LIMIT', 'ILIKE', 'RETURNING', 'INTERVAL', 'NOW'],
  clickhouse: ['RETURNING'],
  athena: ['ILIKE', 'RETURNING'],
};
const QH_KW_PHRASES = [
  { t: 'ORDER BY', need: 'select' },
  { t: 'GROUP BY', need: 'from' },
  { t: 'PARTITION BY', need: 'over' },
  { t: 'LEFT JOIN', need: 'from' },
  { t: 'INNER JOIN', need: 'from' },
  { t: 'RIGHT JOIN', need: 'from' },
  { t: 'FULL OUTER JOIN', need: 'from' },
  { t: 'CROSS JOIN', need: 'from' },
  { t: 'UNION ALL', need: 'select' },
  { t: 'IS NULL', need: 'cond' },
  { t: 'IS NOT NULL', need: 'cond' },
  { t: 'NOT IN', need: 'cond' },
  { t: 'NOT EXISTS', need: 'cond' },
  { t: 'NOT LIKE', need: 'cond' },
  { t: 'GROUPING SETS', need: 'group' },
  { t: 'INSERT INTO', need: 'first', write: true },
  { t: 'DELETE FROM', need: 'first', write: true },
  { t: 'ON CONFLICT', need: 'insert', engines: ['postgres'] },
  { t: 'DO NOTHING', need: 'conflict', engines: ['postgres'] },
  { t: 'DISTINCT ON', need: 'afterSelect', engines: ['postgres'] },
  { t: 'NULLS FIRST', need: 'order', engines: ['postgres', 'clickhouse', 'athena'] },
  { t: 'NULLS LAST', need: 'order', engines: ['postgres', 'clickhouse', 'athena'] },
  { t: 'FETCH FIRST', need: 'select', engines: ['postgres', 'athena'] },
  { t: 'TABLESAMPLE BERNOULLI', need: 'from', engines: ['postgres', 'athena'] },
  { t: 'CROSS APPLY', need: 'from', engines: ['mssql'] },
  { t: 'OUTER APPLY', need: 'from', engines: ['mssql'] },
  { t: 'WITH (NOLOCK)', need: 'from', engines: ['mssql'] },
  { t: 'FETCH NEXT', need: 'order', engines: ['mssql'] },
  { t: 'ARRAY JOIN', need: 'from', engines: ['clickhouse'] },
  { t: 'LEFT ARRAY JOIN', need: 'from', engines: ['clickhouse'] },
  { t: 'ASOF JOIN', need: 'from', engines: ['clickhouse'] },
  { t: 'GLOBAL IN', need: 'cond', engines: ['clickhouse'] },
  { t: 'CROSS JOIN UNNEST', need: 'from', engines: ['athena'] },
  { t: 'WITH ORDINALITY', need: 'unnest', engines: ['athena'] },
];
const QH_KW_COND = ['WHERE', 'ON', 'HAVING', 'WHEN', 'AND', 'OR', 'PREWHERE'];

// True when an engine runs reads only in QueryHub (ClickHouse, Athena).
function qhEngineIdReadOnly(engineId) {
  const eng = String(engineId || 'postgres');
  const info = (typeof QH_ENGINES !== 'undefined' && QH_ENGINES[eng]) || null;
  return info ? !!info.readOnly : (eng === 'clickhouse' || eng === 'athena');
}

// The keywords of an engine, in QH_KEYWORDS order, then the engine's own words.
const qhKwCache = {};
function qhKeywordsFor(engineId) {
  const eng = String(engineId || 'postgres');
  if (qhKwCache[eng]) return qhKwCache[eng];
  const drop = new Set(QH_KW_DROP[eng] || []);
  const out = QH_KEYWORDS.filter(w => !drop.has(w));
  (QH_KW_ENGINE[eng] || []).forEach(w => { if (!out.includes(w)) out.push(w); });
  qhKwCache[eng] = out;
  return out;
}

// The phrases of an engine, as { t, need }.
function qhKwPhrasesFor(engineId) {
  const eng = String(engineId || 'postgres');
  const ro = qhEngineIdReadOnly(eng);
  return QH_KW_PHRASES.filter(p => (!p.engines || p.engines.includes(eng)) && !(p.write && ro));
}

// True when a phrase can stand after the words W of its statement (upper case,
// from qhSuggestTokens, without the word at the caret).
function qhKwPhraseFits(need, W) {
  const has = (w) => W.includes(w);
  switch (need) {
    case 'first': return W.length === 0;
    case 'select': return has('SELECT');
    case 'from': return has('FROM');
    case 'cond': return QH_KW_COND.some(has);
    case 'over': return has('OVER');
    case 'group': return has('GROUP');
    case 'order': return has('ORDER');
    case 'insert': return W[0] === 'INSERT';
    case 'conflict': return has('CONFLICT');
    case 'afterSelect': return W[W.length - 1] === 'SELECT';
    case 'unnest': return has('UNNEST');
    default: return false;
  }
}

// The last text this editor copied as a WHOLE LINE (Ctrl+C / Ctrl+X with
// nothing selected). VS Code remembers that a copy was line-shaped and pastes
// it back as its own line rather than into the middle of the current one; the
// system clipboard carries no such flag, so we keep it here and confirm on
// paste that the clipboard still holds exactly this string. If the user copied
// anything else in between — another app, another field — the comparison fails
// and the paste falls through to normal behaviour, which is the safe default.
let qhLineClip = null;

function qhEscape(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// A password QueryHub did not keep (CODE 2026-09-23 §5): a stored role script
// holds this literal where the password was — in history, workspaces, templates
// and favourites alike — and the server refuses to run it (422). The editor
// marks it, and Run stops on it with the placeholder selected, so the refusal
// is never the first anyone hears of it.
const QH_REDACTED_LIT = "'***REDACTED***'";
function qhRedactedAt(sql) {
  const s = String(sql || ''), out = [];
  for (let i = s.indexOf(QH_REDACTED_LIT); i >= 0; i = s.indexOf(QH_REDACTED_LIT, i + QH_REDACTED_LIT.length)) out.push(i);
  return out;
}

// ---------------------------------------------------------------------------
// Undo / redo
//
// Every change to the text lands in one history, whoever made it: typing, a
// paste, a completion, the * expansion, Tab, a dropped tree object, a line cut
// or paste, or the app replacing the text from outside the editor. The
// browser's own undo cannot do this. It only knows the edits it made itself,
// and the first time React writes the value — every edit the editor makes on
// its own — that stack is gone. That is why ⌘Z after expanding * did nothing.
// So the editor keeps its own history and owns ⌘Z / Ctrl+Z (back), ⌘⇧Z /
// Ctrl+Shift+Z (forward), and Ctrl+Y (forward) off the Mac.
//
// One history per tab, held outside React, so it survives a tab switch and the
// editor unmounting behind the Welcome tab. A step stores what changed (where,
// what was removed, what was inserted), never a copy of the whole script.
// Typing and deleting merge into one step the way VS Code groups them: a new
// step at the first blank after a word, or whenever the caret moved in
// between. Every other edit is a step of its own.
// ---------------------------------------------------------------------------
const QH_UNDO_MAX = 500;      // steps kept per tab
const QH_UNDO_TABS = 64;      // tabs whose history is kept at once
const QH_UNDO_MERGE = new Set(['type', 'back', 'fwd', 'compose']);
const QH_EDITOR_MAC = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
const qhUndoDocs = new Map();

// The one contiguous change that turns `a` into `b`.
function qhTextDiff(a, b) {
  const n = Math.min(a.length, b.length);
  let s = 0;
  while (s < n && a.charCodeAt(s) === b.charCodeAt(s)) s++;
  let ea = a.length, eb = b.length;
  while (ea > s && eb > s && a.charCodeAt(ea - 1) === b.charCodeAt(eb - 1)) { ea--; eb--; }
  return { at: s, del: a.slice(s, ea), ins: b.slice(s, eb) };
}

// How an input event groups, from its InputEvent.inputType. Only plain typing,
// plain deleting and an IME composition can merge; a paste, a cut, Enter or a
// word delete is always a step of its own.
function qhEditKind(inputType) {
  if (inputType === 'insertText') return 'type';
  if (inputType === 'deleteContentBackward') return 'back';
  if (inputType === 'deleteContentForward') return 'fwd';
  if (/Composition/.test(inputType || '')) return 'compose';
  return inputType || 'edit';
}

// The history of one document, started at its current text the first time it
// is asked for. Past the cap the least recently used history is dropped.
function qhUndoDoc(key, text) {
  let h = qhUndoDocs.get(key);
  if (h) { qhUndoDocs.delete(key); qhUndoDocs.set(key, h); return h; }
  h = { text, undo: [], redo: [] };
  qhUndoDocs.set(key, h);
  if (qhUndoDocs.size > QH_UNDO_TABS) qhUndoDocs.delete(qhUndoDocs.keys().next().value);
  return h;
}

// Sign-out drops every history: what is in it is the previous user's SQL.
function qhUndoForget() { qhUndoDocs.clear(); }

// Whether change `c` continues the open step `top` rather than starting one.
function qhUndoJoins(top, c, kind, before) {
  if (top.kind !== kind) return false;
  // A composition is one step from compositionstart (which closes the step
  // before it) to the next edit of another kind.
  if (kind === 'compose') return true;
  const caret = top.after[0];
  if (top.after[1] !== caret || before[0] !== caret || before[1] !== caret) return false;
  if (kind === 'type') {
    const last = top.changes[top.changes.length - 1].ins;
    if (/^\s/.test(c.ins) && /\S$/.test(last)) return false;
  }
  return true;
}

// Record the change from the history's text to `next`. `meta.before` and
// `meta.after` are the selections around it — undo puts back the first, redo
// the second. Without them the changed range stands in.
function qhUndoRecord(h, next, meta) {
  if (next === h.text) return false;
  const m = meta || {};
  const c = qhTextDiff(h.text, next);
  const kind = m.kind || 'edit';
  const before = m.before || [c.at, c.at + c.del.length];
  const after = m.after || [c.at + c.ins.length, c.at + c.ins.length];
  h.text = next;
  h.redo.length = 0;
  const top = h.undo[h.undo.length - 1];
  if (top && top.open && qhUndoJoins(top, c, kind, before)) {
    top.changes.push(c);
    top.after = after;
    return true;
  }
  if (top) top.open = false;
  h.undo.push({ kind, changes: [c], before, after, open: QH_UNDO_MERGE.has(kind) });
  if (h.undo.length > QH_UNDO_MAX) h.undo.shift();
  return true;
}

// Close the open step, so the next edit starts a new one.
function qhUndoSeal(h) {
  const top = h.undo[h.undo.length - 1];
  if (top) top.open = false;
}

// One step back (dir < 0) or forward (dir > 0). Returns the new text and the
// selection to restore, or null when there is nothing to take.
function qhUndoStep(h, dir) {
  const e = (dir < 0 ? h.undo : h.redo).pop();
  if (!e) return null;
  e.open = false;
  let t = h.text;
  if (dir < 0) {
    for (let i = e.changes.length - 1; i >= 0; i--) {
      const c = e.changes[i];
      t = t.slice(0, c.at) + c.del + t.slice(c.at + c.ins.length);
    }
    h.redo.push(e);
  } else {
    for (const c of e.changes) t = t.slice(0, c.at) + c.ins + t.slice(c.at + c.del.length);
    h.undo.push(e);
  }
  h.text = t;
  return { text: t, sel: dir < 0 ? e.before : e.after };
}

// Tokenize + return highlighted HTML
function qhHighlight(code) {
  // master regex: comments | strings | numbers | words | other
  const re = /(--[^\n]*|\/\*[\s\S]*?\*\/)|('(?:[^']|'')*')|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)|([\s\S])/g;
  let out = '';
  let m;
  while ((m = re.exec(code)) !== null) {
    if (m[1] != null) out += `<span class="tk-com">${qhEscape(m[1])}</span>`;
    else if (m[2] != null) out += m[2] === QH_REDACTED_LIT ? `<span class="tk-str tk-redact">${qhEscape(m[2])}</span>` : `<span class="tk-str">${qhEscape(m[2])}</span>`;
    else if (m[3] != null) out += `<span class="tk-num">${qhEscape(m[3])}</span>`;
    else if (m[4] != null) {
      const w = m[4];
      if (QH_KW_SET.has(w.toUpperCase())) out += `<span class="tk-kw">${qhEscape(w)}</span>`;
      else {
        // function call if followed by (
        const after = code.slice(re.lastIndex).match(/^\s*\(/);
        if (after) out += `<span class="tk-fn">${qhEscape(w)}</span>`;
        else out += qhEscape(w);
      }
    } else out += qhEscape(m[5]);
  }
  return out;
}

// ---------------------------------------------------------------------------
// The kind of name the statement wants at the caret (2026-11-10, Devart A2)
//
// qhBuildSuggest knows the table slots of a query: FROM, JOIN, INTO and so on.
// qhSuggestContext adds the slots of EXEC and CALL, of DROP, ALTER and CREATE,
// and of GRANT and REVOKE. It reads only the statement that the caret is in.
// A word in a string, a quoted name or a comment does not count.
//
// It returns null when the old rules apply, or { mode, kind, words, paren }:
//   routine    a procedure or function. kind: 'procedure', 'function' or 'any'.
//              paren: true after CALL, where the name is inserted with '('.
//   objtype    an object type after DROP, ALTER or CREATE (words).
//   table      a table.
//   schema     a schema.
//   index      an index.
//   privilege  a privilege after GRANT or REVOKE (words).
//   grantOn    the object after GRANT ... ON: tables, then words.
//   database   a database.
//   grantee    a role after GRANT ... TO or REVOKE ... FROM. The catalog has
//              no role list, so the only word is PUBLIC.
// ---------------------------------------------------------------------------
const QH_CTX_OBJTYPES = {
  postgres: ['TABLE', 'VIEW', 'MATERIALIZED VIEW', 'INDEX', 'FUNCTION', 'PROCEDURE', 'SCHEMA', 'SEQUENCE', 'TRIGGER', 'TYPE', 'EXTENSION'],
  mssql: ['TABLE', 'VIEW', 'INDEX', 'FUNCTION', 'PROCEDURE', 'SCHEMA', 'SEQUENCE', 'TRIGGER', 'TYPE', 'SYNONYM'],
};
const QH_CTX_PRIVILEGES = {
  postgres: ['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER', 'EXECUTE', 'USAGE', 'CONNECT', 'CREATE', 'TEMPORARY', 'ALL PRIVILEGES'],
  mssql: ['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'EXECUTE', 'REFERENCES', 'ALTER', 'CONTROL', 'VIEW DEFINITION'],
};
const QH_CTX_GRANT_ON = {
  postgres: ['TABLE', 'SCHEMA', 'SEQUENCE', 'FUNCTION', 'PROCEDURE', 'DATABASE', 'ALL TABLES IN SCHEMA'],
  mssql: ['SCHEMA::', 'OBJECT::'],
};
// The routine kind that each type word names.
const QH_CTX_ROUTINE_KIND = { FUNCTION: 'function', PROCEDURE: 'procedure', PROC: 'procedure', ROUTINE: 'any' };

// The tokens of the statement that ends at the end of `text`: an upper-case
// word, '@' for a variable, '"' for a quoted name, 'S' for a string, or the
// punctuation itself ('::' is one token). A top-level `;` starts a new list.
// The word that is being typed at the end is dropped, and so is a qualifier
// in front of it (`dbo.|`): `dot` says that there was one. The answer is null
// when the end of `text` is inside a string, a quoted name or a comment.
function qhSuggestTokens(text) {
  const n = text.length;
  let out = [];
  let i = 0;
  const wordRe = /[A-Za-z_@#][A-Za-z0-9_$@#]*/y;
  // The end of a quoted run that opens at `i`, or -1 when it does not close.
  const closeAt = (i0, close) => {
    let j = i0 + 1;
    while (j < n) {
      if (text[j] === close) {
        if (close !== ']' && text[j + 1] === close) { j += 2; continue; }
        return j + 1;
      }
      j++;
    }
    return -1;
  };
  while (i < n) {
    const ch = text[i];
    if (ch === ';') { out = []; i++; continue; }
    if (ch === ' ' || ch === '\t' || ch === '\n' || ch === '\r' || ch === '\f' || ch === '\v') { i++; continue; }
    if (ch === '-' && text[i + 1] === '-') {
      const e = text.indexOf('\n', i);
      if (e < 0) return null;
      i = e; continue;
    }
    if (ch === '/' && text[i + 1] === '*') {
      const e = text.indexOf('*/', i + 2);
      if (e < 0) return null;
      i = e + 2; continue;
    }
    if (ch === "'" || ch === '"' || ch === '`' || ch === '[') {
      const e = closeAt(i, ch === '[' ? ']' : ch);
      if (e < 0) return null;
      out.push(ch === "'" ? 'S' : '"'); i = e; continue;
    }
    if (ch === '$' && !(i > 0 && /[A-Za-z0-9_]/.test(text[i - 1]))) {
      const m = /^\$[A-Za-z_]*\$/.exec(text.slice(i, i + 66));
      if (m) {
        const e = text.indexOf(m[0], i + m[0].length);
        if (e < 0) return null;
        out.push('S'); i = e + m[0].length; continue;
      }
    }
    if (ch >= '0' && ch <= '9') {
      while (i < n && /[0-9.]/.test(text[i])) i++;
      out.push('0'); continue;
    }
    wordRe.lastIndex = i;
    const w = wordRe.exec(text);
    if (w) {
      out.push(w[0][0] === '@' ? '@' : w[0].toUpperCase());
      i = wordRe.lastIndex; continue;
    }
    if (ch === ':' && text[i + 1] === ':') { out.push('::'); i += 2; continue; }
    out.push(ch); i++;
  }
  // Drop the word that is being typed, then a qualifier in front of it.
  if (/[A-Za-z0-9_$@#]$/.test(text)) out.pop();
  let dot = false;
  if (out[out.length - 1] === '.') { out.pop(); out.pop(); dot = true; }
  return { words: out, dot };
}

function qhSuggestContext(value, caret, engineId) {
  const tk = qhSuggestTokens(String(value == null ? '' : value).slice(0, Math.max(0, caret)));
  if (!tk || !tk.words.length) return null;
  const W = tk.words;
  const eng = engineId === 'mssql' ? 'mssql' : 'postgres';
  const first = W[0];
  const last = W[W.length - 1];
  const ctx = (mode, extra) => Object.assign({ mode, dot: tk.dot }, extra || {});
  // Skip IF EXISTS, IF NOT EXISTS, CONCURRENTLY and ONLY at the start of `a`.
  const skipLead = (a) => {
    let k = 0;
    for (;;) {
      if (a[k] === 'IF' && a[k + 1] === 'EXISTS') k += 2;
      else if (a[k] === 'IF' && a[k + 1] === 'NOT' && a[k + 2] === 'EXISTS') k += 3;
      else if (a[k] === 'CONCURRENTLY' || a[k] === 'ONLY') k += 1;
      else return a.slice(k);
    }
  };
  // A list slot: nothing typed yet, or a comma after the previous name.
  const open = (a) => a.length === 0 || a[a.length - 1] === ',';

  if (first === 'GRANT' || first === 'REVOKE') {
    // A column list of a privilege, `GRANT SELECT (a, |`, wants columns.
    let depth = 0;
    W.forEach(w => { if (w === '(') depth++; else if (w === ')') depth = Math.max(0, depth - 1); });
    if (depth > 0) return null;
    const toAt = Math.max(W.lastIndexOf('TO'), first === 'REVOKE' ? W.lastIndexOf('FROM') : -1);
    if (toAt > 0) return open(W.slice(toAt + 1)) ? ctx('grantee', { words: ['PUBLIC'] }) : null;
    const onAt = W.lastIndexOf('ON');
    if (onAt > 0) {
      const a = W.slice(onAt + 1);
      if (!a.length) return ctx('grantOn', { words: QH_CTX_GRANT_ON[eng] });
      const cls = a[0];
      const rest = a.slice(1);
      if (cls === 'ALL') {
        // ALL TABLES IN SCHEMA |, ALL FUNCTIONS IN SCHEMA | and the like.
        const inAt = a.indexOf('IN');
        return (inAt > 0 && a[inAt + 1] === 'SCHEMA' && open(a.slice(inAt + 2))) ? ctx('schema') : null;
      }
      if (cls === 'SCHEMA' && rest[0] === '::') return rest.length === 1 ? ctx('schema') : null;
      if (cls === 'OBJECT' && rest[0] === '::') return rest.length === 1 ? ctx('table') : null;
      if (cls === 'TABLE') return open(rest) ? ctx('table') : null;
      if (cls === 'SCHEMA') return open(rest) ? ctx('schema') : null;
      if (cls === 'DATABASE') return open(rest) ? ctx('database') : null;
      if (QH_CTX_ROUTINE_KIND[cls]) return open(rest) ? ctx('routine', { kind: QH_CTX_ROUTINE_KIND[cls] }) : null;
      // `GRANT SELECT ON t1, |`: a bare list of tables.
      return last === ',' ? ctx('table') : null;
    }
    // The privilege list. REVOKE may start with GRANT OPTION FOR.
    if (W.length === 1 || last === ',' || (first === 'REVOKE' && last === 'FOR' && W[W.length - 2] === 'OPTION')) {
      return ctx('privilege', { words: QH_CTX_PRIVILEGES[eng] });
    }
    return null;
  }

  if ((first === 'EXEC' || first === 'EXECUTE') && engineId === 'mssql') {
    // EXEC |, and EXEC @rc = | for a return code.
    if (W.length === 1 || (W.length === 3 && W[1] === '@' && W[2] === '=')) return ctx('routine', { kind: 'procedure' });
    return null;
  }
  if (first === 'CALL' && engineId === 'postgres') {
    return W.length === 1 ? ctx('routine', { kind: 'procedure', paren: true }) : null;
  }

  if (first === 'DROP' || first === 'ALTER' || first === 'CREATE') {
    let a = W.slice(1);
    if (first === 'CREATE') {
      // Words that may stand between CREATE and the object type.
      if (a[0] === 'OR' && (a[1] === 'REPLACE' || a[1] === 'ALTER')) a = a.slice(2);
      while (['UNIQUE', 'CLUSTERED', 'NONCLUSTERED', 'TEMP', 'TEMPORARY', 'UNLOGGED'].indexOf(a[0]) >= 0) a = a.slice(1);
    }
    if (!a.length) {
      const words = QH_CTX_OBJTYPES[eng].slice();
      if (first === 'CREATE' && W.length === 1) {
        if (eng === 'postgres') words.push('OR REPLACE');
        else words.push('OR ALTER');
        words.push('UNIQUE INDEX');
      }
      return ctx('objtype', { words });
    }
    let type = a[0];
    a = a.slice(1);
    if (type === 'MATERIALIZED' && a[0] === 'VIEW') { type = 'VIEW'; a = a.slice(1); }
    a = skipLead(a);
    if (first === 'CREATE') {
      // CREATE INDEX ix ON |, CREATE INDEX ON | and CREATE TRIGGER ... ON |.
      if ((type === 'INDEX' || type === 'TRIGGER') && (last === 'ON' || (last === 'ONLY' && W[W.length - 2] === 'ON'))) return ctx('table');
      return null;
    }
    if (type === 'TABLE' || type === 'VIEW') return open(a) ? ctx('table') : null;
    if (type === 'SCHEMA') return open(a) ? ctx('schema') : null;
    if (type === 'DATABASE') return open(a) ? ctx('database') : null;
    if (QH_CTX_ROUTINE_KIND[type]) return open(a) ? ctx('routine', { kind: QH_CTX_ROUTINE_KIND[type] }) : null;
    if (type === 'INDEX') {
      if (open(a)) return ctx('index');
      // DROP INDEX ix ON | and ALTER INDEX ix ON | (SQL Server).
      return (last === 'ON' && a.length === 2) ? ctx('table') : null;
    }
    if (type === 'TRIGGER') return (last === 'ON' && a.length === 2) ? ctx('table') : null;
    return null;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Snippets (2026-11-10, Devart D1)
//
// A short key puts a piece of SQL in its place: `ssf` + Tab gives
// `SELECT * FROM `. The keys show in the typing list. `$CURSOR$` marks where
// the caret goes. Without the marker, the caret goes to the end.
//   body   the text per engine: `postgres`, `mssql`, or `any` for the rest.
//          An engine with no body gets no snippet.
//   write  the snippet changes data. A read-only engine does not get it.
//   expr   the snippet goes inside a statement. Every other snippet starts a
//          statement, so the list gives it only for the first word.
// No key is the start of a keyword, so a typed keyword never turns into a
// snippet (app/test/snippets.test.mjs checks it).
// ---------------------------------------------------------------------------
const QH_SNIPPET_CURSOR = '$CURSOR$';
// The grey text of an empty editor. It teaches the snippets: a person who
// does not know a key exists never types it. The browser hides it on the
// first character. `.qh-ta::placeholder` gives it a colour, because the
// textarea's own text is transparent.
const QH_EDITOR_HINT = 'Type SQL. Tip: ssf + Tab gives SELECT * FROM.\nAll snippets: \u2328 in the tab bar.';
// The drag type of a saved query row (qh-panels.jsx). The editor reads it on
// drop and puts the text on lines of its own.
const QH_DRAG_BLOCK = 'application/x-qh-sql-block';
const QH_SNIPPETS = [
  { key: 'ssf', desc: 'SELECT * FROM', body: { any: 'SELECT * FROM $CURSOR$' } },
  { key: 's100', desc: 'First 100 rows', body: { any: 'SELECT * FROM $CURSOR$ LIMIT 100', mssql: 'SELECT TOP 100 * FROM $CURSOR$' } },
  { key: 'scf', desc: 'Count rows', body: { any: 'SELECT COUNT(*) FROM $CURSOR$' } },
  { key: 'sgc', desc: 'Count by group', body: { any: 'SELECT $CURSOR$, COUNT(*) AS cnt\nFROM \nGROUP BY \nORDER BY cnt DESC' } },
  { key: 'ii', desc: 'INSERT INTO ... VALUES', write: true, body: { any: 'INSERT INTO $CURSOR$ ()\nVALUES ()' } },
  { key: 'upw', desc: 'UPDATE ... WHERE', write: true, body: { any: 'UPDATE $CURSOR$\nSET \nWHERE ' } },
  { key: 'dfw', desc: 'DELETE ... WHERE', write: true, body: { any: 'DELETE FROM $table$\nWHERE $condition$' } },
  { key: 'cte', desc: 'WITH ... SELECT', body: { any: 'WITH cte AS (\n  $CURSOR$\n)\nSELECT * FROM cte' } },
  { key: 'cw', desc: 'CASE WHEN', expr: true, body: { any: 'CASE WHEN $condition$ THEN $result$ ELSE $other$ END' } },
  { key: 'exs', desc: 'EXISTS (SELECT 1 ...)', expr: true, body: { any: 'EXISTS (SELECT 1 FROM $table$ WHERE $condition$)' } },
  { key: 'hdr', desc: 'Header comment', body: { any: '-- $DATE$ $USER$\n-- $SERVER$ / $DB$\n$CURSOR$' } },
  { key: 'act', desc: 'Active sessions', body: {
    postgres: 'SELECT pid, usename, datname, state, wait_event_type, wait_event,\n' +
      '  now() - query_start AS running_for, left(query, 200) AS query\n' +
      'FROM pg_stat_activity\n' +
      "WHERE state <> 'idle' AND pid <> pg_backend_pid()\n" +
      'ORDER BY query_start',
    mssql: 'SELECT r.session_id, s.login_name, DB_NAME(r.database_id) AS database_name, r.status,\n' +
      '  r.command, r.wait_type, r.total_elapsed_time / 1000 AS elapsed_s, LEFT(t.text, 200) AS query\n' +
      'FROM sys.dm_exec_requests r\n' +
      'JOIN sys.dm_exec_sessions s ON s.session_id = r.session_id\n' +
      'CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) t\n' +
      'WHERE s.is_user_process = 1 AND r.session_id <> @@SPID\n' +
      'ORDER BY r.total_elapsed_time DESC' } },
  { key: 'blk', desc: 'Blocked sessions', body: {
    postgres: 'SELECT pid, pg_blocking_pids(pid) AS blocked_by, usename, wait_event_type, wait_event,\n' +
      '  now() - query_start AS waiting_for, left(query, 200) AS query\n' +
      'FROM pg_stat_activity\n' +
      'WHERE cardinality(pg_blocking_pids(pid)) > 0\n' +
      'ORDER BY query_start',
    mssql: 'SELECT r.session_id, r.blocking_session_id, r.wait_type, r.wait_time / 1000 AS wait_s,\n' +
      '  DB_NAME(r.database_id) AS database_name, LEFT(t.text, 200) AS query\n' +
      'FROM sys.dm_exec_requests r\n' +
      'CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) t\n' +
      'WHERE r.blocking_session_id <> 0\n' +
      'ORDER BY r.wait_time DESC' } },
  { key: 'tsz', desc: 'Biggest tables', body: {
    postgres: 'SELECT schemaname, relname, n_live_tup,\n' +
      '  pg_size_pretty(pg_total_relation_size(relid)) AS total_size\n' +
      'FROM pg_stat_user_tables\n' +
      'ORDER BY pg_total_relation_size(relid) DESC\n' +
      'LIMIT 20',
    mssql: 'SELECT TOP 20 s.name AS schema_name, t.name AS table_name,\n' +
      '  (SELECT SUM(p.rows) FROM sys.partitions p\n' +
      '    WHERE p.object_id = t.object_id AND p.index_id IN (0, 1)) AS row_count,\n' +
      '  (SELECT SUM(a.total_pages) * 8 / 1024 FROM sys.partitions p\n' +
      '    JOIN sys.allocation_units a ON a.container_id = p.partition_id\n' +
      '    WHERE p.object_id = t.object_id) AS total_mb\n' +
      'FROM sys.tables t\n' +
      'JOIN sys.schemas s ON s.schema_id = t.schema_id\n' +
      'ORDER BY total_mb DESC' } },
];

// The snippets that an engine gets, each as { key, desc, body, write, expr,
// from }. `body` is the text for that engine, with the markers still in it.
// `from` is 'mine', 'builtin' or 'shared'.
//
// `user` is the /snippets list of the person (2026-11-11, Devart D3 and D4):
// { key, description, body, engine, mine }. A snippet with an engine goes
// only to that engine. One key gives one snippet, in this order: the
// person's own, the built-in one, then a shared one of another person. So a
// shared snippet never changes the `ssf` of another person. Two shared
// snippets with one key: the first in the list (the oldest) wins. A person's
// snippet has `expr` null: the typing list offers it in every position.
function qhSnippetsFor(engineId, user) {
  const eng = String(engineId || 'postgres');
  const readOnly = qhEngineIdReadOnly(eng);
  const out = [];
  const taken = new Set();
  const users = (Array.isArray(user) ? user : []).filter(u => u && typeof u.key === 'string' && u.key &&
    typeof u.body === 'string' && (!u.engine || u.engine === eng));
  const addUser = (u, from) => {
    if (taken.has(u.key)) return;
    taken.add(u.key);
    out.push({ key: u.key, desc: u.description || qhSnippetPreview(u.body), body: u.body, write: false, expr: null, from });
  };
  users.filter(u => u.mine).forEach(u => addUser(u, 'mine'));
  QH_SNIPPETS.forEach(sn => {
    if (sn.write && readOnly) return;
    if (taken.has(sn.key)) return;
    const body = Object.prototype.hasOwnProperty.call(sn.body, eng) ? sn.body[eng] : sn.body.any;
    if (body == null) return;
    taken.add(sn.key);
    out.push({ key: sn.key, desc: sn.desc, body, write: !!sn.write, expr: !!sn.expr, from: 'builtin' });
  });
  users.filter(u => !u.mine).forEach(u => addUser(u, 'shared'));
  return out;
}

// The first line of a snippet body, for a snippet with no description.
function qhSnippetPreview(body) {
  const line = String(body || '').split('\n').map(l => l.trim()).find(Boolean) || '';
  return line.length > 48 ? line.slice(0, 47) + '\u2026' : line;
}

// The message for the snippet form (qh-panels.jsx SnippetsPanel), or null.
// The limits are those of the table (migration 205). One more rule is the
// rule of the built-in keys: a key is not the start of a keyword of any
// engine, so a typed keyword never turns into a snippet. `mine` is the
// person's own snippets, for a key that is already in use.
const QH_SNIP_KEY_RE = /^[a-z][a-z0-9_]{1,31}$/;
function qhSnippetFormError(form, mine) {
  const f = form || {};
  const key = String(f.key || '').trim().toLowerCase();
  if (!QH_SNIP_KEY_RE.test(key)) return 'Use 2 to 32 characters for the key: a letter first, then letters, digits or _.';
  const words = new Set(QH_KEYWORDS.map(w => w.toLowerCase()));
  ['postgres', 'mssql', 'clickhouse', 'athena'].forEach(e => qhKeywordsFor(e).forEach(w => words.add(String(w).toLowerCase())));
  const hit = [...words].sort().find(w => w.startsWith(key));
  if (hit) return 'The key is the start of the keyword ' + hit.toUpperCase() + '. Use a different key.';
  if ((mine || []).some(s => s && s.key === key && String(s.id) !== String(f.id == null ? '' : f.id))) {
    return 'You have a snippet with the key ' + key + '. Use a different key.';
  }
  if (String(f.description || '').trim().length > 200) return 'Use at most 200 characters for the description.';
  if (String(f.folder || '').trim().length > 64) return 'Use at most 64 characters for the folder.';
  if (!String(f.body || '').trim()) return 'Write the SQL of the snippet.';
  if (String(f.body).length > 20000) return 'Use at most 20000 characters for the SQL.';
  return null;
}

// The text that a snippet inserts and the caret offset in it. Each line after
// the first gets `indent`, the indent of the line where the person typed the key.
// Markers (Devart D2):
//   $CURSOR$ or $CARET$   where the caret goes at the end.
//   $DATE$ $TIME$         today's date (YYYY-MM-DD) and time (HH:MM), local.
//   $DB$ $SERVER$ $USER$  the database, the server and the person (`ctx`).
//   $name$                a field `<name>`: Tab goes from field to field.
// A literal that `ctx` does not know becomes a field too. Any other `$` stays.
// `stops` holds the fields as [start, end] offsets, only when there are some.
const QH_SNIPPET_MARK = /\$([A-Za-z][A-Za-z0-9_]*)\$/g;
function qhSnippetText(body, indent, ctx, now) {
  const b = String(body).split('\n').join('\n' + (indent || ''));
  const c = ctx || {};
  const d = now || new Date();
  const two = (n) => String(n).padStart(2, '0');
  const lit = {
    DATE: d.getFullYear() + '-' + two(d.getMonth() + 1) + '-' + two(d.getDate()),
    TIME: two(d.getHours()) + ':' + two(d.getMinutes()),
    DB: c.db || '', SERVER: c.server || '', USER: c.user || '',
  };
  let text = '', pos = 0, caretAt = -1;
  const stops = [];
  b.replace(QH_SNIPPET_MARK, (m, name, at) => {
    text += b.slice(pos, at);
    pos = at + m.length;
    const up = name.toUpperCase();
    if (name === 'CURSOR' || name === 'CARET') { if (caretAt < 0) caretAt = text.length; return m; }
    if (name === up && Object.prototype.hasOwnProperty.call(lit, up)) {
      if (lit[up]) { text += lit[up]; return m; }
      name = up.toLowerCase();
    } else if (name !== name.toLowerCase()) { text += m; return m; }
    const f = '<' + name + '>';
    stops.push([text.length, text.length + f.length]);
    text += f;
    return m;
  });
  text += b.slice(pos);
  const out = { text, caretAt: caretAt < 0 ? text.length : caretAt };
  if (stops.length) out.stops = stops;
  return out;
}

// The fields of an inserted snippet or template after an edit (Devart D2).
// `ses` is { list: [[s, e]], idx, final }: the fields in text order, the field
// the caret is in, and where the caret goes at the end. An edit inside a field
// grows or shrinks that field and moves the ones after it. An edit outside
// every field ends the fields: the result is null. `before` is the selection
// before the edit, so a run of equal characters cannot hide where it was.
function qhStopsAfterEdit(ses, oldV, newV, before) {
  if (!ses || !ses.list || !ses.list.length) return null;
  const a = String(oldV), b = String(newV);
  let p = 0;
  const max = Math.min(a.length, b.length);
  while (p < max && a.charCodeAt(p) === b.charCodeAt(p)) p++;
  if (before) p = Math.min(p, before[0], before[1]);
  let q = 0;
  while (q < a.length - p && q < b.length - p && a.charCodeAt(a.length - 1 - q) === b.charCodeAt(b.length - 1 - q)) q++;
  const oe = a.length - q, delta = b.length - a.length;
  const inside = (i) => ses.list[i][0] <= p && oe <= ses.list[i][1];
  let hit = inside(ses.idx) ? ses.idx : -1;
  if (hit < 0) hit = ses.list.findIndex((r, i) => inside(i));
  if (hit < 0) return null;
  const he = ses.list[hit][1];
  const list = ses.list.map(([s, e], i) => (i === hit ? [s, e + delta] : s >= he ? [s + delta, e + delta] : [s, e]));
  return { list, idx: hit, final: ses.final >= he ? ses.final + delta : ses.final };
}

// Insert a block of SQL (a saved query) at the caret or over the selection.
// The block goes on lines of its own: a newline goes in front when the line
// has text before the caret, and one goes after it when the line has text
// after the selection. Returns { value, caret }, with the caret at the end of
// the block.
function qhInsertBlock(value, start, end, text) {
  const v = String(value == null ? '' : value);
  const a = Math.max(0, Math.min(start, v.length)), b = Math.max(a, Math.min(end, v.length));
  const lineStart = v.lastIndexOf('\n', a - 1) + 1;
  let lineEnd = v.indexOf('\n', b);
  if (lineEnd < 0) lineEnd = v.length;
  const body = String(text == null ? '' : text).replace(/\s+$/, '');
  const head = v.slice(lineStart, a).trim() ? '\n' : '';
  const tail = v.slice(b, lineEnd).trim() ? '\n' : '';
  const ins = head + body + tail;
  return { value: v.slice(0, a) + ins + v.slice(b), caret: a + head.length + body.length };
}

// Build schema-aware + keyword suggestions for the token left of the caret.
// The typing list holds at most this many rows (Devart A4). About ten show at
// once. The rest scroll. PageUp and PageDown move QH_AC_PAGE rows.
const QH_AC_MAX = 50;
const QH_AC_PAGE = 8;

// The initials of the words of a name, lower case: JobCandidate and
// job_candidate give "jc", userId2 gives "ui2". A word starts at a capital
// after a small letter, at a digit run, and after _ or $.
function qhNameInitials(name) {
  const words = String(name || '').replace(/([a-z])([A-Z])/g, '$1 $2').replace(/([A-Za-z])(\d)/g, '$1 $2')
    .split(/[\s_$]+/).filter(Boolean);
  return words.length > 1 ? words.map(w => w[0]).join('').toLowerCase() : '';
}

const QH_KW_AFTER_TABLE = new Set(['from', 'join', 'into', 'update', 'table', 'truncate', 'describe']);
// `opts.force` (Ctrl+Space) also answers with nothing typed: every name matches.
function qhBuildSuggest(value, caret, schema, engineId, opts) {
  const before = value.slice(0, caret);
  const m = before.match(/[A-Za-z_][A-Za-z0-9_]*$/);
  if (!m && !(opts && opts.force)) return null;
  const token = m ? m[0] : '';
  const start = caret - token.length;
  const dot = before[start - 1] === '.';
  const prevM = before.slice(0, dot ? start - 1 : start).match(/([A-Za-z_][A-Za-z0-9_]*)\s*$/);
  const prev = prevM ? prevM[1].toLowerCase() : '';
  // Where the already-typed qualifier begins. `prevM` matched a slice starting
  // at 0, so its index is an index into `before`.
  const qualStart = (dot && prevM) ? prevM.index : start;
  // The word that governs what belongs here. Without a qualifier that is the
  // previous word; with one it is the word BEFORE the qualifier, since `prev` is
  // then the qualifier itself. Reading only `prev` is why `FROM dba.ind|` offered
  // columns: `prev` was `dba`, never `from`, so table mode stayed off and columns
  // outranked tables in a clause where a column cannot appear at all.
  const govM = dot
    ? before.slice(0, qualStart).match(/([A-Za-z_][A-Za-z0-9_]*)\s*$/)
    : prevM;
  const gov = govM ? govM[1].toLowerCase() : '';
  // The slot that the statement kind decides: EXEC, DROP, GRANT and so on.
  const ctx = qhSuggestContext(value, caret, engineId);
  // A table slot goes on to the table branches below. So does a qualified
  // name after GRANT ... ON (`ON app.|`).
  const ctxTable = !!(ctx && (ctx.mode === 'table' || (ctx.mode === 'grantOn' && dot)));
  const ctxOwn = !!(ctx && !ctxTable && (!dot || ctx.mode === 'routine' || ctx.mode === 'index'));
  // A slot of its own beats the old keyword rule: `REVOKE ... FROM |` wants a
  // role, so FROM must not switch on table mode there.
  const tableMode = ctxTable || (!ctxOwn && QH_KW_AFTER_TABLE.has(dot ? gov : prev));
  const lc = token.toLowerCase();
  const items = [];
  const quote = (n) => (typeof qhQuoteIdentFor === 'function' ? qhQuoteIdentFor(n, engineId) : qhQuoteIdent(n));
  // Ranked matching. An EXACT match used to be dropped ("it would complete to
  // itself"), which made autocomplete look broken the moment you finished
  // typing a real table name — you got an empty list and no confirmation the
  // object exists. It is kept now and ranked first; accepting it is a no-op,
  // so onKey lets Enter/Tab through instead of eating the keystroke.
  // rank 0 = exact, 1 = prefix, 2 = substring (identifiers only — substring on
  // keywords is noise, e.g. "ele" matching SELECT).
  // What gets INSERTED for a table: its schema-qualified form when the
  // catalog knows the schema unambiguously (`schema.qualify`), otherwise the
  // bare name. Unqualified names only resolve against search_path, which is
  // pinned narrow — so on this fleet, where most tables are NOT in
  // public/dbo, inserting a bare name produced SQL that could not run.
  const insertFor = (n, type) => {
    if (type === 'keyword') return n.toUpperCase();
    // A routine is inserted CALLED — `count(` — with the caret left inside the
    // parens. A bare name costs two more keystrokes every time, and the thing you
    // certainly want next is the argument.
    if (type === 'function') return quote(n) + '(';
    if (type === 'system') return n;          // already qualified (pg_catalog.x / sys.x)
    const q = (type === 'table' && schema.qualify) ? schema.qualify[n] : null;
    if (!q) return quote(n);
    const dot = q.lastIndexOf('.');
    return quote(q.slice(0, dot)) + '.' + quote(q.slice(dot + 1));
  };
  // `textOf` gives the inserted text when the type alone does not decide it.
  // rank 1.5 = the initials of the words of a name (Devart A3): `jc` finds
  // JobCandidate and job_candidate. Identifiers only, from two letters on.
  const add = (arr, type, typeRank, textOf) => (arr || []).forEach(n => {
    const nl = n.toLowerCase();
    let rank;
    if (nl === lc) rank = 0;
    else if (nl.startsWith(lc)) rank = 1;
    else if (type !== 'keyword' && lc.length >= 2 && qhNameInitials(n).startsWith(lc)) rank = 1.5;
    else if (type !== 'keyword' && nl.indexOf(lc) > 0) rank = 2;
    else return;
    const text = textOf ? textOf(n) : insertFor(n, type);
    const it = { text, label: n, type, rank, typeRank };
    // A bare column name that is PII in at least one table of the database.
    // The mark is a hint only: ranking does not change.
    if (type === 'column' && schema.piiColumns && schema.piiColumns.has && schema.piiColumns.has(n)) it.pii = true;
    // A pick whose own text is qualified, dropped in after a qualifier the user
    // already typed, produced `dba.dba.whoisactive` -- the insert re-qualified
    // while the replacement range covered only the bare token. So an item that
    // carries a qualifier replaces from the qualifier, and the range travels
    // with the item: a COLUMN pick in `alias.col|` must NOT eat the alias
    // (`u.email` becoming `public.email` would be a different, wrong column),
    // while a TABLE pick must, or the schema is written twice.
    if (dot && text.indexOf('.') >= 0) it.start = qualStart;
    items.push(it);
  });
  // The system pool holds QUALIFIED names (`sys.dm_exec_sessions`,
  // `pg_catalog.pg_class`). After a dot the token is only the tail, so matching
  // the stored string against it fails at the prefix and could only ever hit as a
  // substring — which is why `sys.dm_` returned nothing useful while the bare
  // `dm_` worked. Match the tail against the tail, and only for entries whose own
  // qualifier is the one the user typed: inside `sys.`, `pg_catalog.*` is noise.
  const addSystem = (arr, qual, typeRank) => {
    const q = String(qual || '').toLowerCase();
    if (!q) return;
    (arr || []).forEach(full => {
      const i = full.indexOf('.');
      if (i < 0) return;
      if (full.slice(0, i).toLowerCase() !== q) return;
      const tail = full.slice(i + 1), tl = tail.toLowerCase();
      let rank;
      if (tl === lc) rank = 0;
      else if (tl.startsWith(lc)) rank = 1;
      else if (tl.indexOf(lc) > 0) rank = 2;
      else return;
      // Replaces the TOKEN only: the qualifier the user typed is already there
      // and correct, so it must not be re-inserted (that is the doubled-schema
      // bug, one dot to the left).
      items.push({ text: tail, label: full, type: 'system', rank, typeRank });
    });
  };

  if (ctxOwn) {
    if (ctx.mode === 'routine') {
      // A procedure is inserted bare: `EXEC dbo.load_day @d = ...`. After CALL
      // the name gets '(' like a function call. A name with no known kind is
      // kept, so a catalog without kinds still gives names.
      const kindOf = (n) => (schema.functionKind || {})[n];
      const fns = schema.functions || [];
      const textOf = ctx.paren ? (n) => quote(n) + '(' : (n) => quote(n);
      if (ctx.kind !== 'function') add(fns.filter(n => kindOf(n) === 'procedure'), 'procedure', 0, textOf);
      add(fns.filter(n => kindOf(n) !== 'procedure' && (ctx.kind !== 'procedure' || kindOf(n) == null)), 'function', 1, textOf);
    } else if (ctx.mode === 'grantOn') {
      add(schema.tables, 'table', 0);
      add(ctx.words, 'keyword', 1);
    } else if (ctx.mode === 'schema') {
      add(schema.schemas, 'schema', 0, (n) => quote(n));
    } else if (ctx.mode === 'database') {
      add(schema.dbs, 'database', 0);
    } else if (ctx.mode === 'index') {
      // Index names from every table the catalog knows. PostgreSQL drops an
      // index by its schema-qualified name. SQL Server names the table after
      // ON instead, so the name goes in bare there. After a qualifier the
      // name goes in bare too.
      const full = {};
      [schema.tableInfoQ, schema.tableInfo].forEach(map => Object.keys(map || {}).forEach(t => {
        const info = map[t];
        (info && Array.isArray(info.indexes) ? info.indexes : []).forEach(ix => {
          const nm = ix && ix.name ? String(ix.name) : '';
          if (!nm || full[nm] !== undefined) return;
          full[nm] = (engineId === 'postgres' && !dot && info.schema) ? quote(info.schema) + '.' + quote(nm) : quote(nm);
        });
      }));
      add(Object.keys(full), 'index', 0, (n) => full[n]);
    } else {
      // objtype, privilege, grantee: words only.
      add(ctx.words, 'keyword', 0);
    }
  }
  else if (dot && tableMode) {
    // `schema.tbl|` inside FROM/JOIN/UPDATE...: a column cannot appear here at
    // all, so offering them is what made the popup look broken.
    add(schema.tables, 'table', 0);
    // `prev`, not `gov`: the QUALIFIER is the word before the dot (`sys`), while
    // `gov` is the clause keyword that put us in table mode (`from`).
    addSystem(schema.systemTables, prev, 1);
    // A table-valued function is a legal FROM entry, so routines belong here too.
    add(schema.functions, 'function', 2);
  }
  else if (dot) {
    // `alias.col|` in a select list / WHERE: columns first, tables still
    // offered because an alias cannot be resolved from the text alone.
    add(schema.columns, 'column', 0);
    add(schema.tables, 'table', 1);
    addSystem(schema.systemTables, prev, 2);
  }
  else if (tableMode) {
    // The CTEs of the statement go first after FROM and JOIN (Devart A20).
    // A CTE name goes in as written.
    if (prev === 'from' || prev === 'join') add(qhCteNamesAt(value, start), 'cte', -1, (n) => n);
    add(schema.tables, 'table', 0); add(schema.systemTables, 'system', 1); add(schema.dbs, 'database', 2); add(schema.functions, 'function', 3);
  }
  else {
    add(schema.tables, 'table', 0); add(schema.columns, 'column', 1); add(schema.functions, 'function', 2); add(schema.systemTables, 'system', 3);
    add(qhKeywordsFor(engineId), 'keyword', 4);
    // Phrases (QH_KW_PHRASES) match on their first word. They go just before
    // the single keywords, so `order` lists ORDER BY above ORDER.
    const tw = qhSuggestTokens(before);
    if (tw) qhKwPhrasesFor(engineId).forEach(ph => {
      if (!qhKwPhraseFits(ph.need, tw.words)) return;
      const head = ph.t.split(' ')[0].toLowerCase();
      const rank = head === lc ? 0 : head.startsWith(lc) ? 1 : -1;
      if (rank >= 0) items.push({ text: ph.t, label: ph.t, type: 'keyword', rank, typeRank: 3.9 });
    });
  }
  // Snippets (QH_SNIPPETS): from two letters on, with no qualifier, and not
  // in a slot that wants a name. A statement snippet needs the first word of
  // a statement: nothing before it since the last `;`, or a line of its own
  // after a blank line. An expression snippet needs a word inside a statement.
  // An exact key goes first. A key that the token only starts goes after the
  // keywords. A string or a comment gets none (qhSuggestTokens gives null).
  if (token.length >= 2 && !dot && !ctxOwn && !tableMode) {
    const tk = qhSuggestTokens(before);
    if (tk) {
      const lineStart = before.lastIndexOf('\n', start - 1) + 1;
      const lead = before.slice(lineStart, start);
      const prevLine = before.slice(before.lastIndexOf('\n', lineStart - 2) + 1, Math.max(0, lineStart - 1));
      const first = tk.words.length === 0 || (!lead.trim() && lineStart > 0 && !prevLine.trim());
      const indent = lead.match(/^[ \t]*/)[0];
      qhSnippetsFor(engineId, schema.snippets).forEach(sn => {
        if (sn.expr === first) return;
        const kl = sn.key.toLowerCase();
        let rank, typeRank;
        if (kl === lc) { rank = 0; typeRank = -1; }
        else if (kl.startsWith(lc)) { rank = 1; typeRank = 5; }
        else return;
        const st = qhSnippetText(sn.body, indent, schema.context);
        const it = { text: st.text, caretAt: st.caretAt, label: sn.key, desc: sn.desc, type: 'snippet', rank, typeRank };
        if (st.stops) it.stops = st.stops;
        items.push(it);
      });
    }
  }
  // Sort BEFORE truncating: the old code capped in insertion order, so a
  // matching table could be pushed out of the list by ten columns.
  items.sort((a, b) => (a.rank - b.rank) || (a.typeRank - b.typeRank));
  const seen = new Set(); const out = [];
  for (const it of items) { const k = it.type + ':' + it.text; if (!seen.has(k)) { seen.add(k); out.push(it); } if (out.length >= QH_AC_MAX) break; }
  return out.length ? { items: out, start, end: start + token.length } : null;
}

// ---------------------------------------------------------------------------
// The statement at the caret (2026-11-10)
//
// F8 with no selection and Shift+Alt+S read it. It returns { start, end } of the
// statement around `caret`, or null. The range has no surrounding whitespace and
// no closing `;`. Only a top-level `;` splits. A `;` inside '...', "...", [...],
// a $tag$ body, a -- comment or a /* */ comment is data. These are the rules of
// qhSplitStatements (qh-data.jsx), plus the comment and bracket cases.
//
// The caret rules:
//   * a caret inside a statement, or in the blanks before its own `;`, gives it.
//   * a caret in blanks or a comment after a `;`, on the line of that `;`,
//     gives the statement that ends there.
//   * a caret in the indent of a statement's first line gives that statement.
//   * any other caret in a part with no SQL (blank or comment only) gives null.
// A comment on its own line above a statement belongs to that statement, so a
// hint such as /*+ ... */ runs with it. A comment that starts on the line of the
// previous `;` belongs to the previous statement.
// ---------------------------------------------------------------------------
function qhStatementAt(sql, caret) {
  const text = String(sql == null ? '' : sql);
  const n = text.length;
  if (!n) return null;
  const c = Math.max(0, Math.min(n, Number(caret) || 0));
  const spans = [];                         // { s, e, k: 'code' | 'com' }, blanks left out
  const seps = [];                          // offsets of the top-level `;`
  const push = (s, e, k) => {
    const last = spans[spans.length - 1];
    if (k === 'code' && last && last.k === 'code' && last.e === s) last.e = e;
    else spans.push({ s, e, k });
  };
  // The end of a quoted run that starts at `i`. A doubled closer is an escape.
  const quoted = (i, close) => {
    let j = i + 1;
    while (j < n) {
      if (text[j] === close) {
        if (text[j + 1] === close) { j += 2; continue; }
        return j + 1;
      }
      j++;
    }
    return n;
  };
  let i = 0;
  while (i < n) {
    const ch = text[i];
    if (ch === ';') { seps.push(i); i++; continue; }
    if (ch === ' ' || ch === '\t' || ch === '\n' || ch === '\r' || ch === '\f' || ch === '\v') { i++; continue; }
    if (ch === '-' && text[i + 1] === '-') {
      let e = text.indexOf('\n', i);
      if (e < 0) e = n;
      push(i, e, 'com'); i = e; continue;
    }
    if (ch === '/' && text[i + 1] === '*') {
      const e0 = text.indexOf('*/', i + 2);
      const e = e0 < 0 ? n : e0 + 2;
      push(i, e, 'com'); i = e; continue;
    }
    if (ch === "'" || ch === '"') { const e = quoted(i, ch); push(i, e, 'code'); i = e; continue; }
    if (ch === '[') { const e = quoted(i, ']'); push(i, e, 'code'); i = e; continue; }
    // A dollar quote. A `$` inside a name (`a$b$`) does not open one.
    if (ch === '$' && !(i > 0 && /[A-Za-z0-9_]/.test(text[i - 1]))) {
      const m = /^\$[A-Za-z_]*\$/.exec(text.slice(i, i + 66));
      if (m) {
        const e0 = text.indexOf(m[0], i + m[0].length);
        const e = e0 < 0 ? n : e0 + m[0].length;
        push(i, e, 'code'); i = e; continue;
      }
    }
    push(i, i + 1, 'code'); i++;
  }
  // One part per `;`, plus the tail. `a` is the first offset, `b` the `;` or n.
  const parts = [];
  for (let k = 0; k <= seps.length; k++) {
    const a = k === 0 ? 0 : seps[k - 1] + 1;
    const b = k < seps.length ? seps[k] : n;
    parts.push({ a, b, closed: k < seps.length, stmt: null });
  }
  let si = 0;
  parts.forEach((p, k) => {
    const own = [];
    while (si < spans.length && spans[si].s < p.b) { if (spans[si].s >= p.a) own.push(spans[si]); si++; }
    if (!own.some(sp => sp.k === 'code')) return;           // blank or comment only
    let lineEnd = -1;                                        // the end of the line of the previous `;`
    if (k > 0) { lineEnd = text.indexOf('\n', p.a - 1); if (lineEnd < 0) lineEnd = n; }
    const first = own.find(sp => sp.k === 'code' || k === 0 || sp.s > lineEnd);
    p.stmt = { start: first.s, end: own[own.length - 1].e };
  });
  for (let k = 0; k < parts.length; k++) {
    const p = parts[k], st = p.stmt;
    if (c < p.a || c > p.b) continue;
    if (st && c >= st.start && c <= st.end) return { start: st.start, end: st.end };
    if (st && c > st.end && (p.closed || text.slice(st.end, c).indexOf('\n') < 0)) return { start: st.start, end: st.end };
    if (k > 0 && text.slice(p.a - 1, c).indexOf('\n') < 0) {
      const prev = parts[k - 1].stmt;
      return prev ? { start: prev.start, end: prev.end } : null;
    }
    if (st && c < st.start && /^[ \t]*$/.test(text.slice(c, st.start))) return { start: st.start, end: st.end };
    return null;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Statement bounds with or without `;` (2026-11-10, Devart G2 and F6)
//
// qhStatementAt splits only at `;`. Many scripts have no `;` at all:
//   SELECT 1
//   SELECT 2
// qhStatementRanges also starts a statement at a line whose first word is a
// statement word (QH_STMT_LEAD) when the line before ends a clause. It is
// careful on purpose. A missed bound costs one `;`. A wrong bound breaks the
// SQL. So these words do NOT start a statement:
//   * a word inside brackets, or a word after a word that waits for more
//     (QH_STMT_WAIT: AS, THEN, UNION, FOR, ...) or after punctuation.
//   * the source of an INSERT INTO (SELECT, VALUES, EXEC, WITH).
//   * the main statement after a WITH list or after EXPLAIN.
//   * SET after UPDATE, ALTER or CREATE in the same statement.
//   * DROP, ALTER, SET, DELETE, UPDATE, GRANT and REVOKE inside an ALTER
//     (ALTER TABLE t / DROP COLUMN c). DROP TABLE and ALTER TABLE still split.
//   * EXEC and EXECUTE inside a CREATE (a trigger runs a function).
//   * a WITH that opens no CTE (WITH (NOLOCK), WITH GRANT OPTION).
//   * the body of an IF or a WHILE (SQL Server).
// On SQL Server, GO alone on its line ends a batch.
// Returns [{ start, end, semi, complete }]: the first and the last code
// offsets of each statement, whether a `;` ends it, and whether its last word
// ends a clause (a `;` may follow it).
// ---------------------------------------------------------------------------
const QH_STMT_LEAD = new Set(('SELECT INSERT UPDATE DELETE MERGE WITH CREATE ALTER DROP TRUNCATE GRANT REVOKE ' +
  'EXEC EXECUTE CALL DECLARE SET USE COMMIT ROLLBACK VACUUM ANALYZE SHOW DESCRIBE OPTIMIZE COPY REFRESH ' +
  'REINDEX CLUSTER LOCK COMMENT').split(' '));
const QH_STMT_LEAD_MSSQL = new Set(['IF', 'WHILE', 'PRINT', 'RAISERROR', 'THROW', 'WAITFOR']);
// After these words the next line continues the statement. Every statement
// word is one too (GRANT / SELECT ON t), except COMMIT and ROLLBACK.
const QH_STMT_WAIT = new Set(('AS THEN ELSE DO FOR ALSO INSTEAD UNION ALL EXCEPT INTERSECT MINUS EXPLAIN VERBOSE ' +
  'ROW STATEMENT ATOMIC CATCH TRY RETURN RETURNS IN EXISTS ANY SOME LATERAL APPLY NOT AND OR BEFORE AFTER OF ON ' +
  'BY FULL FREEZE WHERE FROM JOIN INTO TO IS LIKE ILIKE BETWEEN CASE WHEN HAVING LIMIT OFFSET TOP DISTINCT USING ' +
  'ORDER GROUP PARTITION OVER VALUES TABLE VIEW INDEX ESCAPE').split(' '));
const QH_STMT_OBJ = new Set(('TABLE VIEW INDEX SEQUENCE FUNCTION PROCEDURE PROC DATABASE SCHEMA ROLE USER TYPE ' +
  'DOMAIN MATERIALIZED TRIGGER EXTENSION SERVER LOGIN POLICY PUBLICATION SUBSCRIPTION AGGREGATE OPERATOR RULE ' +
  'TABLESPACE SYNONYM DICTIONARY STATISTICS EVENT FOREIGN').split(' '));
const QH_BEGIN_TXN = new Set(['TRAN', 'TRANSACTION', 'WORK', 'DISTRIBUTED', 'ISOLATION', 'READ', 'DEFERRABLE', 'NOT']);

// Does the WITH at T[i] open a CTE list? WITH RECURSIVE, WITH x AS, or
// WITH x (a, b) AS. WITH ORDINALITY AS is a FROM item, not a CTE.
function qhWithOpensCte(T, i) {
  const a = T[i + 1], b = T[i + 2];
  if (!a) return false;
  if (a.k === 'word' && a.u === 'RECURSIVE') return true;
  if (!(a.k === 'word' || a.k === 'qid') || a.u === 'ORDINALITY') return false;
  if (b && b.k === 'word' && b.u === 'AS') return true;
  if (!(b && b.k === 'p' && b.v === '(')) return false;
  let d = 0, j = i + 2;
  for (; j < T.length; j++) {
    if (T[j].k !== 'p') continue;
    if (T[j].v === '(') d++;
    else if (T[j].v === ')' && --d === 0) break;
  }
  const c = T[j + 1];
  return !!(c && c.k === 'word' && c.u === 'AS');
}

// Does T[i] end a clause, so that a `;` or a new statement may follow it?
function qhStmtTokEnds(T, i, engineId, head) {
  const p = T[i];
  if (!p) return false;
  if (p.k === 'qid' || p.k === 'num' || p.k === 'str') return true;
  if (p.k === 'p') return p.v === ')' || p.v === '*';
  if (p.k !== 'word') return false;
  const u = p.u;
  if ((u === 'TRY' || u === 'CATCH') && T[i - 1] && T[i - 1].u === 'END') return true;
  if (u === 'ON' && head === 'SET') return true;               // SET NOCOUNT ON
  if (u === 'BEGIN') return engineId === 'postgres';           // PostgreSQL: a transaction. SQL Server: a block.
  if (u === 'COMMIT' || u === 'ROLLBACK') return true;
  if (u === 'UPDATE' && T[i - 1] && T[i - 1].u === 'FOR') return true;   // SELECT ... FOR UPDATE
  return !(QH_STMT_WAIT.has(u) || QH_STMT_LEAD.has(u) || QH_STMT_LEAD_MSSQL.has(u));
}

// GO alone on its line, with an optional count: `GO` or `GO 5`.
function qhGoLine(text, t) {
  const a = text.lastIndexOf('\n', t.s - 1) + 1;
  let b = text.indexOf('\n', t.e);
  if (b < 0) b = text.length;
  return /^[ \t]*GO(?:[ \t]+\d+)?[ \t]*(?:--.*)?\r?$/i.test(text.slice(a, b));
}

function qhStatementRanges(sql, engineId) {
  const text = String(sql == null ? '' : sql);
  const ms = engineId === 'mssql';
  const T = qhSqlTokens(text);
  const out = [];
  let st = -1, depth = 0, head = '';
  let needSrc = false, needMain = false, needBody = false, setHome = false;
  const close = (last, semi) => {
    if (st >= 0 && last >= st) {
      out.push({ start: T[st].s, end: T[last].e, semi, complete: semi || qhStmtTokEnds(T, last, engineId, head) });
    }
    st = -1; depth = 0; head = '';
    needSrc = needMain = needBody = setHome = false;
  };
  const isLead = (u) => QH_STMT_LEAD.has(u) || (ms && QH_STMT_LEAD_MSSQL.has(u));
  // Does the statement word T[i] start a new statement?
  const splits = (i) => {
    const t = T[i], u = t.u;
    if (!isLead(u)) return false;
    const nl = text.lastIndexOf('\n', t.s - 1);
    if (nl < T[i - 1].e || !/^[ \t\r]*$/.test(text.slice(nl + 1, t.s))) return false;   // not first on its line
    if (!qhStmtTokEnds(T, i - 1, engineId, head)) return false;
    if (needSrc || needMain || needBody) return false;
    if (u === 'WITH' && !qhWithOpensCte(T, i)) return false;
    if (u === 'SET' && setHome) return false;
    if (head === 'ALTER') {
      if (u === 'SET' || u === 'DELETE' || u === 'UPDATE' || u === 'GRANT' || u === 'REVOKE') return false;
      const n = T[i + 1];
      const obj = n && n.k === 'word' && QH_STMT_OBJ.has(n.u) && !(n.u === 'INDEX' && engineId === 'clickhouse');
      if ((u === 'DROP' || u === 'ALTER') && !obj) return false;
    }
    if (head === 'CREATE' && (u === 'EXEC' || u === 'EXECUTE')) return false;
    return true;
  };
  for (let i = 0; i < T.length; i++) {
    const t = T[i];
    if (t.k === 'p' && t.v === ';') { close(i - 1, true); continue; }
    if (ms && t.k === 'word' && t.u === 'GO' && qhGoLine(text, t)) {
      close(i - 1, false);
      if (T[i + 1] && T[i + 1].k === 'num' && text.slice(t.e, T[i + 1].s).indexOf('\n') < 0) i++;
      continue;
    }
    if (st >= 0 && depth === 0 && t.k === 'word' && splits(i)) close(i - 1, false);
    if (st < 0) { st = i; head = t.k === 'word' ? t.u : ''; }
    if (t.k === 'p' && t.v === '(') { depth++; continue; }
    if (t.k === 'p' && t.v === ')') { depth = Math.max(0, depth - 1); continue; }
    if (t.k !== 'word' || depth !== 0) continue;
    const u = t.u, prev = T[i - 1];
    // A statement word that continued the statement ends the wait it was for.
    if (needBody && i !== st && isLead(u)) needBody = (u === 'IF' || u === 'WHILE');
    if (ms && u === 'BEGIN') needBody = false;
    if (ms && (u === 'IF' || u === 'WHILE') && (i === st || (prev && prev.u === 'ELSE'))) needBody = true;
    if (needSrc && (u === 'SELECT' || u === 'VALUES' || u === 'EXEC' || u === 'EXECUTE' || u === 'DEFAULT')) needSrc = false;
    if (needMain && (u === 'SELECT' || u === 'INSERT' || u === 'UPDATE' || u === 'DELETE' || u === 'MERGE')) needMain = false;
    if (u === 'INSERT' && T[i + 1] && T[i + 1].u === 'INTO') needSrc = true;
    if (u === 'EXPLAIN' || (u === 'WITH' && qhWithOpensCte(T, i))) needMain = true;
    if (u === 'UPDATE' || u === 'ALTER' || u === 'CREATE') setHome = true;
  }
  if (st >= 0) close(T.length - 1, false);
  return out;
}

// Insert missing semicolons (Devart F6). A `;` goes after the last word of
// each statement that has none and ends a clause (qhStatementRanges). Nothing
// else in the text differs: no space, no comment, no `;` after GO. Returns
// { text, at } (`at` = the offsets where a `;` went in, in the old text), or
// null when every statement already has one.
function qhAddSemicolons(sql, engineId) {
  const text = String(sql == null ? '' : sql);
  const at = qhStatementRanges(text, engineId).filter(r => !r.semi && r.complete).map(r => r.end);
  if (!at.length) return null;
  let out = '', pos = 0;
  at.forEach(p => { out += text.slice(pos, p) + ';'; pos = p; });
  return { text: out + text.slice(pos), at };
}

// The caret target of Alt+PageDown (dir 1) and Alt+PageUp (dir -1) (Devart
// G2). Down: the start of the next statement. Up: the start of the statement
// the caret is in, or of the one before when the caret is already at its start.
function qhStatementJump(sql, caret, dir, engineId) {
  const rs = qhStatementRanges(sql, engineId);
  const c = Number(caret) || 0;
  if (dir > 0) {
    const r = rs.find(x => x.start > c);
    return r ? r.start : null;
  }
  for (let k = rs.length - 1; k >= 0; k--) if (rs[k].start < c) return rs[k].start;
  return null;
}

// Execute to cursor (Devart I3, Shift+F8). The range from the first character
// of the text to the end of the statement at the caret, or null. Statements
// come from qhStatementRanges, so a script with no `;` also splits. A
// statement counts when it starts at or before the caret, or when only spaces
// or tabs lie between the caret and its first word (the indent of its line).
// A caret in the blanks after a statement stops at that statement. A caret
// before the first statement gives null. A comment at the top goes with the
// run, so a hint is not lost. The last `;` stays out, as with F8.
function qhToCaretRange(sql, caret, engineId) {
  const text = String(sql == null ? '' : sql);
  const c = Math.max(0, Math.min(text.length, Number(caret) || 0));
  let last = null;
  for (const r of qhStatementRanges(text, engineId)) {
    if (r.start <= c || /^[ \t]*$/.test(text.slice(c, r.start))) last = r;
    else break;
  }
  if (!last) return null;
  const first = /\S/.exec(text);
  return { start: first ? first.index : 0, end: last.end };
}

// ---------------------------------------------------------------------------
// Paired constructs (2026-11-10, Devart G3 and G6)
//
// qhPairs lists the brackets and the keyword pairs of the text:
//   ( )   CASE END   BEGIN END   BEGIN TRY END TRY   BEGIN CATCH END CATCH
// A BEGIN that opens a transaction (`BEGIN;`, `BEGIN TRAN`, `BEGIN WORK` ...) is
// no block. An END never closes across an open bracket. Strings, quoted names
// and comments are data (qhSqlTokens). Returns [{ a, b }], each end { s, e }.
// ---------------------------------------------------------------------------
function qhPairs(sql) {
  const T = qhSqlTokens(String(sql == null ? '' : sql));
  const stack = [], pairs = [];
  for (let i = 0; i < T.length; i++) {
    const t = T[i];
    if (t.k === 'p' && t.v === '(') { stack.push({ k: '(', s: t.s, e: t.e }); continue; }
    if (t.k === 'p' && t.v === ')') {
      for (let j = stack.length - 1; j >= 0; j--) {
        if (stack[j].k !== '(') continue;
        const o = stack[j];
        stack.length = j;
        pairs.push({ a: { s: o.s, e: o.e }, b: { s: t.s, e: t.e } });
        break;
      }
      continue;
    }
    if (t.k !== 'word') continue;
    const n = T[i + 1];
    const nu = n && n.k === 'word' ? n.u : '';
    if (t.u === 'CASE') { stack.push({ k: 'CASE', s: t.s, e: t.e }); continue; }
    if (t.u === 'BEGIN') {
      if (nu === 'TRY' || nu === 'CATCH') { stack.push({ k: nu, s: t.s, e: n.e }); i++; continue; }
      if (!n || (n.k === 'p' && n.v === ';') || QH_BEGIN_TXN.has(nu)) continue;
      stack.push({ k: 'BEGIN', s: t.s, e: t.e });
      continue;
    }
    if (t.u === 'END') {
      const want = (nu === 'TRY' || nu === 'CATCH') ? nu : null;
      const e = want ? n.e : t.e;
      for (let j = stack.length - 1; j >= 0; j--) {
        const o = stack[j];
        if (o.k === '(') break;
        if (want ? o.k === want : (o.k === 'CASE' || o.k === 'BEGIN')) {
          stack.length = j;
          pairs.push({ a: { s: o.s, e: o.e }, b: { s: t.s, e } });
          break;
        }
      }
      if (want) i++;
    }
  }
  return pairs;
}

// The pair end under the caret: { self, mate }, or null. The caret is on an
// end when it is inside it or touches it. The end right after the caret wins
// over the end right before it.
function qhPairAt(sql, caret, pairs) {
  const ps = pairs || qhPairs(sql);
  const c = Number(caret) || 0;
  let after = null, before = null;
  for (const p of ps) {
    for (const [self, mate] of [[p.a, p.b], [p.b, p.a]]) {
      if (self.s <= c && c < self.e) { if (!after) after = { self, mate }; }
      else if (self.e === c && !before) before = { self, mate };
    }
  }
  return after || before;
}

// The caret target of Shift+F12 (Devart G3): the mate of the end under the
// caret. With no end under the caret, the closing end of the innermost pair
// around it. Else null.
function qhPairJump(sql, caret) {
  const ps = qhPairs(sql);
  const hit = qhPairAt(sql, caret, ps);
  if (hit) return hit.mate.s;
  const c = Number(caret) || 0;
  let best = null;
  for (const p of ps) if (p.a.e <= c && c <= p.b.s && (!best || p.a.s > best.a.s)) best = p;
  return best ? best.b.s : null;
}

// ---------------------------------------------------------------------------
// Occurrences of the name under the caret (2026-11-10, Devart G5)
//
// A name is a word that is no keyword, or a quoted name. "x", [x] and x are
// the same name, in any case. @x and #x keep their sign, so the variable @x
// and the column x stay apart. A name that occurs once marks nothing.
// Returns [{ start, end }], at most QH_OCC_MAX.
// ---------------------------------------------------------------------------
const QH_OCC_MAX = 500;
const QH_OCC_SKIP = new Set(QH_KEYWORDS.concat(('BEGIN TOP EXEC EXECUTE DECLARE IF WHILE RETURN OVER PARTITION ' +
  'WINDOW FETCH FIRST NEXT ROWS ONLY CROSS APPLY LATERAL ANY SOME TRUE FALSE CAST GO USE PRINT TRY CATCH CURSOR ' +
  'FOR EACH ROW TRIGGER FUNCTION PROCEDURE SCHEMA DATABASE NOLOCK EXPLAIN ANALYZE QUALIFY PREWHERE FINAL ' +
  'SETTINGS FORMAT FILTER RECURSIVE NULLS LAST').split(' ')));
function qhOccKey(T, i) {
  const t = T[i];
  if (t.k === 'qid') return { key: t.v.slice(1, t.v.length - (/["\]]$/.test(t.v) ? 1 : 0)).toLowerCase(), s: t.s };
  if (t.k !== 'word' || QH_OCC_SKIP.has(t.u)) return null;
  const p = T[i - 1];
  if (p && p.k === 'p' && (p.v === '@' || p.v === '#') && p.e === t.s) return { key: p.v + t.v.toLowerCase(), s: p.s };
  return { key: t.v.toLowerCase(), s: t.s };
}
function qhOccurrences(sql, caret) {
  const text = String(sql == null ? '' : sql);
  const T = qhSqlTokens(text);
  const c = Number(caret) || 0;
  let at = T.findIndex(t => t.s <= c && c < t.e && (t.k === 'word' || t.k === 'qid'));
  if (at < 0) at = T.findIndex(t => t.e === c && (t.k === 'word' || t.k === 'qid'));
  if (at < 0) return [];
  const me = qhOccKey(T, at);
  if (!me || !me.key) return [];
  const out = [];
  for (let i = 0; i < T.length && out.length < QH_OCC_MAX; i++) {
    if (T[i].k !== 'word' && T[i].k !== 'qid') continue;
    const k = qhOccKey(T, i);
    if (k && k.key === me.key) out.push({ start: k.s, end: T[i].e });
  }
  return out.length > 1 ? out : [];
}

// The marks of the caret layer: the two ends of a pair under the caret (kind
// 'pair') and the occurrences of the name under it (kind 'occ'), sorted by
// start. The two never share a token: a pair end is a bracket or a keyword.
function qhCaretMarks(sql, caret) {
  const out = qhOccurrences(sql, caret).map(r => ({ start: r.start, end: r.end, kind: 'occ' }));
  const hit = qhPairAt(sql, caret);
  if (hit) [hit.self, hit.mate].forEach(r => out.push({ start: r.s, end: r.e, kind: 'pair' }));
  return out.sort((x, y) => x.start - y.start);
}

// Move the caret's line, or every line the selection touches, one line up
// (dir -1) or down (dir 1) (Devart G10). A selection that ends at the start of
// a line leaves that line where it is. Returns { value, sel } with the
// selection moved with the text, or null at the top or the bottom.
function qhMoveLines(value, a, b, dir) {
  const v = String(value == null ? '' : value);
  const lo = Math.min(a, b), hi0 = Math.max(a, b);
  const hi = (hi0 > lo && v[hi0 - 1] === '\n') ? hi0 - 1 : hi0;
  const s = v.lastIndexOf('\n', lo - 1) + 1;
  let e = v.indexOf('\n', hi);
  if (e < 0) e = v.length;
  const block = v.slice(s, e);
  if (dir < 0) {
    if (s === 0) return null;
    const ps = v.lastIndexOf('\n', s - 2) + 1;
    const d = s - ps;
    return { value: v.slice(0, ps) + block + '\n' + v.slice(ps, s - 1) + v.slice(e), sel: [a - d, b - d] };
  }
  if (e >= v.length) return null;
  let ne = v.indexOf('\n', e + 1);
  if (ne < 0) ne = v.length;
  const below = v.slice(e + 1, ne);
  return { value: v.slice(0, s) + below + '\n' + block + v.slice(ne), sel: [a + below.length + 1, b + below.length + 1] };
}

// Type ( ' " [ or ` over a selection and the selection is wrapped, not
// replaced (Devart F7). The selection stays on the same text, inside the new
// pair. Returns { value, sel }, or null for another key or no selection.
const QH_WRAP_PAIRS = { '(': ')', "'": "'", '"': '"', '[': ']', '`': '`' };
function qhWrapSelection(value, a, b, ch) {
  const close = Object.prototype.hasOwnProperty.call(QH_WRAP_PAIRS, ch) ? QH_WRAP_PAIRS[ch] : null;
  const lo = Math.min(a, b), hi = Math.max(a, b);
  if (!close || lo === hi) return null;
  const v = String(value == null ? '' : value);
  return { value: v.slice(0, lo) + ch + v.slice(lo, hi) + close + v.slice(hi), sel: [lo + 1, hi + 1] };
}

// ---------------------------------------------------------------------------
// SELECT * expansion
//
// Which columns a select-list `*` stands for, read from the statement the caret
// is in. Two things were wrong before this existed:
//   * a FROM table the catalog did not list — a partition (the catalog folds
//     them into their parent), or a table newer than the hourly snapshot — fell
//     back to EVERY column in the database, so Tab filled the select list with
//     hundreds of unrelated names;
//   * the table came from the FIRST `from` anywhere in the editor, so in a
//     script the star expanded another statement's table.
// Now the answer is exact or there is none. Each relation in FROM is looked up
// in the catalog — qualified when written qualified, a bare name only when one
// schema has it — and then through `live(name)`, the columns the target itself
// reports for that name (GET .../columns): an array, null (no such relation, or
// it cannot be asked), or undefined (not asked yet). A join expands to every
// table's columns, each qualified by its alias or name; `t.*` expands to t's.
// What cannot be read exactly — a subquery or a function in FROM, NATURAL and
// USING joins (they merge columns), column aliases — offers nothing.
//
// Returns { kind: 'ready', start, end, text, count }, { kind: 'lookup', names }
// when a relation is not known yet but can be asked about, or null.
// ---------------------------------------------------------------------------

// Tokens for the reader below: words, quoted identifiers, strings, numbers and
// single punctuation characters, with comments and blanks dropped. `s`/`e` are
// offsets into the text; `u` is a word in upper case.
function qhSqlTokens(sql, keepComments) {
  const out = [];
  const re = /(--[^\n]*|\/\*[\s\S]*?(?:\*\/|$))|('(?:[^']|'')*'?)|("(?:[^"]|"")*"?|\[[^\]]*\]?)|(\$[A-Za-z_]*\$)|([A-Za-z_][A-Za-z0-9_$]*)|(\d+(?:\.\d+)?)|(\s+)|([\s\S])/g;
  let m;
  while ((m = re.exec(sql)) !== null) {
    const s = m.index;
    // `keepComments` (the Ctrl+Space list) keeps them as 'cmt' tokens.
    if (m[1] != null && keepComments) { out.push({ k: 'cmt', v: m[0], s, e: s + m[0].length, u: null }); continue; }
    if (m[1] != null || m[7] != null) continue;
    if (m[4] != null) {                       // a $tag$ … $tag$ string
      const close = sql.indexOf(m[4], re.lastIndex);
      const e = close < 0 ? sql.length : close + m[4].length;
      re.lastIndex = e;
      out.push({ k: 'str', v: sql.slice(s, e), s, e, u: null });
      continue;
    }
    const k = m[2] != null ? 'str' : m[3] != null ? 'qid' : m[5] != null ? 'word' : m[6] != null ? 'num' : 'p';
    out.push({ k, v: m[0], s, e: s + m[0].length, u: k === 'word' ? m[0].toUpperCase() : null });
  }
  return out;
}

// Where the server pre-flight blockers point in the SQL. The first rule that
// matches places each message: a quoted fragment, a named keyword, the word
// "parse" or "syntax" (last word), else the first word. Returns
// [{ start, end, message }] sorted by start. One range joins its messages.
const QH_BLOCKER_PHRASES = ['EXPLAIN ANALYZE', 'SET LOCAL'];
const QH_BLOCKER_WORDS = ('SELECT WITH INSERT UPDATE DELETE MERGE DROP TRUNCATE ALTER CREATE GRANT REVOKE ' +
  'EXPLAIN SET COPY CALL DO VACUUM REINDEX CLUSTER LOCK COMMENT REFRESH').split(' ');
const QH_BLOCKER_WORD_SET = new Set(QH_BLOCKER_WORDS);
function qhBlockerRanges(sql, blockers) {
  const text = String(sql || '');
  if (!text || !Array.isArray(blockers) || !blockers.length) return [];
  const words = qhSqlTokens(text).filter(t => t.k === 'word');
  const lower = text.toLowerCase();
  const byKey = new Map();
  const add = (start, end, message) => {
    const key = start + ':' + end;
    const hit = byKey.get(key);
    if (hit) hit.message += '\n' + message;
    else byKey.set(key, { start, end, message });
  };
  blockers.forEach((raw) => {
    const msg = String(raw == null ? '' : raw);
    if (!msg) return;
    // a. a quoted fragment
    const qre = /`([^`]{1,60})`|'([^']{1,60})'|"([^"]{1,60})"/g;
    let q;
    while ((q = qre.exec(msg)) !== null) {
      const frag = q[1] != null ? q[1] : q[2] != null ? q[2] : q[3];
      const at = lower.indexOf(frag.toLowerCase());
      if (at >= 0) { add(at, at + frag.length, msg); return; }
    }
    // b. a keyword that the message names in upper case
    const named = (msg.match(/\b[A-Z]+\b/g) || []).filter(x => QH_BLOCKER_WORD_SET.has(x));
    for (const ph of QH_BLOCKER_PHRASES) {
      if (!new RegExp('\\b' + ph + '\\b').test(msg)) continue;
      const [a, b] = ph.split(' ');
      for (let i = 0; i + 1 < words.length; i++) {
        if (words[i].u === a && words[i + 1].u === b) { add(words[i].s, words[i + 1].e, msg); return; }
      }
    }
    // The words in the order the message names them: "CTE-embedded INSERT is
    // not allowed inside WITH" points at the INSERT, not at the WITH.
    for (const kw of named) {
      const w = words.find(t => t.u === kw);
      if (w) { add(w.s, w.e, msg); return; }
    }
    // c. the SQL could not be parsed
    if (!words.length) return;
    const w = /parse|syntax/i.test(msg) ? words[words.length - 1] : words[0];
    add(w.s, w.e, msg);
  });
  return Array.from(byKey.values()).sort((x, y) => x.start - y.start);
}

// Words that end a FROM list, and words that cannot be a table or an alias.
const QH_FROM_END = new Set(['WHERE', 'GROUP', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET', 'FETCH', 'FOR',
  'WINDOW', 'UNION', 'INTERSECT', 'EXCEPT', 'RETURNING', 'OPTION', 'QUALIFY', 'SETTINGS', 'FORMAT',
  'PREWHERE', 'SAMPLE', 'FINAL']);
const QH_NOT_NAME = new Set([...QH_FROM_END, 'ON', 'USING', 'JOIN', 'INNER', 'LEFT', 'RIGHT', 'FULL',
  'CROSS', 'NATURAL', 'OUTER', 'LATERAL', 'TABLESAMPLE', 'WITH', 'AS', 'SELECT', 'FROM', 'APPLY', 'ONLY']);
// Clause words a select-list star cannot be behind, walking back to its SELECT.
const QH_STAR_STOP = new Set(['FROM', 'WHERE', 'GROUP', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET', 'UNION',
  'INTERSECT', 'EXCEPT', 'INTO', 'VALUES', 'SET', 'ON', 'USING', 'JOIN', 'RETURNING', 'WINDOW']);

// ---------------------------------------------------------------------------
// Code analysis (2026-11-10, Devart H1)
//
// Rules that read the SQL in the browser and mark a word with a wavy line,
// as the pre-flight blockers do. A finding is advice: it never stops a run.
//   level    warn: the statement is probably wrong or holds a session open.
//            info: the statement works, but a better form exists.
//   engines  the engine ids that get the rule. null = every engine.
//   write    the rule is about a write, so a read-only engine skips it.
// The rules read qhSqlTokens, so a string, a quoted name or a comment never
// matches. qhLint(sql, engineId) returns [{ start, end, level, message }]
// sorted by start. app/test/code-analysis.test.mjs covers each rule.
// ---------------------------------------------------------------------------
// Words that open a condition. A `= NULL` after one of them is a test.
const QA_COND = new Set(['WHERE', 'ON', 'HAVING', 'WHEN', 'AND', 'OR', 'NOT', 'IF', 'WHILE']);
// Words after which a `= NULL` is an assignment or an alias, not a test.
const QA_ASSIGN = new Set(['SET', 'DECLARE', 'SELECT', 'VALUES', 'UPDATE', 'INSERT', 'RETURNING',
  'THEN', 'ELSE', 'BY', 'DEFAULT', 'USING']);
// Words before `(` that are not a function call.
const QA_NOT_FUNC = new Set(['IN', 'EXISTS', 'ANY', 'ALL', 'SOME', 'VALUES', 'AND', 'OR', 'NOT',
  'SELECT', 'ARRAY', 'ROW', 'WHERE', 'CASE', 'WHEN', 'THEN', 'ELSE', 'ON', 'USING', 'AS', 'OVER']);
// Words inside a call that are not a column: parts of the call's own syntax.
const QA_ARG_WORDS = new Set(('AS FROM FOR AND OR NOT NULL TRUE FALSE CASE WHEN THEN ELSE END IS IN ' +
  'DISTINCT YEAR MONTH DAY HOUR MINUTE SECOND WEEK QUARTER EPOCH DOW DOY ISODOW INTERVAL BOTH LEADING ' +
  'TRAILING USING VARCHAR CHAR CHARACTER VARYING TEXT INT INTEGER BIGINT SMALLINT NUMERIC DECIMAL DATE ' +
  'TIME TIMESTAMP TIMESTAMPTZ BOOLEAN REAL FLOAT DOUBLE PRECISION NVARCHAR MAX JSON JSONB UUID ZONE AT ' +
  'WITH WITHOUT CURRENT_DATE CURRENT_TIMESTAMP CURRENT_TIME LOCALTIMESTAMP').split(' '));
const QA_CMP_WORDS = new Set(['LIKE', 'ILIKE', 'IN', 'BETWEEN', 'SIMILAR']);

// The operator made of adjacent punctuation tokens that ends just before
// token i: { s: index of its first token, v: its text }.
function qaOpBefore(S, i) {
  let k = i - 1, v = '';
  while (k >= 0 && S[k].k === 'p' && '<>!='.includes(S[k].v) && (k === i - 1 || S[k].e === S[k + 1].s)) {
    v = S[k].v + v;
    k--;
  }
  return { s: k + 1, v };
}
// The operator that starts at token i, as text ('' when there is none).
function qaOpAt(S, i) {
  let v = '';
  for (let k = i; k < S.length && S[k].k === 'p' && '<>!='.includes(S[k].v) && (k === i || S[k - 1].e === S[k].s); k++) v += S[k].v;
  return v;
}
// The index of the `)` that closes the `(` at index i, or -1.
function qaClose(S, depth, i) {
  for (let k = i + 1; k < S.length; k++) if (S[k].v === ')' && depth[k] === depth[i]) return k;
  return -1;
}
// A token that names a column or a table: a quoted name, or a word that is
// not part of the call syntax and not a function name. A word after `@` or
// `:` is a parameter.
function qaIsName(S, k) {
  const t = S[k];
  if (!t) return false;
  if (t.k === 'qid') return true;
  if (t.k !== 'word' || QA_ARG_WORDS.has(t.u)) return false;
  if (S[k + 1] && S[k + 1].v === '(') return false;
  const p = S[k - 1];
  return !(p && p.k === 'p' && (p.v === '@' || p.v === ':') && p.e === t.s);
}
// The last index of a dotted name that starts at k (`a.b.c`), or -1.
function qaNameEnd(S, k) {
  const ok = (t) => t && (t.k === 'qid' || (t.k === 'word' && !QH_NOT_NAME.has(t.u)));
  if (!ok(S[k])) return -1;
  let e = k;
  while (S[e + 1] && S[e + 1].v === '.' && ok(S[e + 2])) e += 2;
  return e;
}
// The word that tells what token i is part of: walk back over nested
// parentheses, leave a parenthesis outward, stop at a word of `stops`.
function qaClauseOf(S, depth, i, stops) {
  let d = depth[i];
  for (let k = i - 1; k >= 0; k--) {
    if (depth[k] > d) continue;
    if (depth[k] < d) d = depth[k];
    if (S[k].k === 'word' && stops.has(S[k].u)) return S[k].u;
  }
  return null;
}
// A type word in a column definition: the token before it is the column's
// name (or TYPE, AS, RETURNS), and that name follows `(`, `,`, COLUMN or ADD.
function qaIsColumnType(S, i) {
  const p = S[i - 1], pp = S[i - 2];
  if (!p) return false;
  if (p.k === 'word' && (p.u === 'TYPE' || p.u === 'AS' || p.u === 'RETURNS')) return true;
  if (p.k !== 'qid' && p.k !== 'word') return false;
  return !!pp && (pp.v === '(' || pp.v === ',' || pp.u === 'COLUMN' || pp.u === 'ADD');
}

const QH_LINT_RULES = [
  { id: 'QA101', level: 'warn', title: 'Comparison with NULL', engines: null, check(c) {
    c.S.forEach((t, i) => {
      if (t.u !== 'NULL') return;
      const op = qaOpBefore(c.S, i);
      if (op.v !== '=' && op.v !== '<>' && op.v !== '!=') return;
      if (!QA_COND.has(qaClauseOf(c.S, c.depth, op.s, new Set([...QA_COND, ...QA_ASSIGN])) || '')) return;
      c.hit(op.s, i, op.v === '=' ? 'A comparison with NULL is never true. Use IS NULL.'
        : 'A comparison with NULL is never true. Use IS NOT NULL.');
    });
  } },
  { id: 'QA102', level: 'warn', title: 'WHEN NULL in a simple CASE', engines: null, check(c) {
    const { S, depth } = c;
    S.forEach((t, i) => {
      if (t.u !== 'CASE' || !S[i + 1] || S[i + 1].u === 'WHEN') return;
      let nest = 0;
      for (let j = i; j < S.length && depth[j] >= depth[i]; j++) {
        if (depth[j] !== depth[i]) continue;
        if (S[j].u === 'CASE') nest++;
        else if (S[j].u === 'END' && --nest === 0) break;
        else if (nest === 1 && S[j].u === 'WHEN' && S[j + 1] && S[j + 1].u === 'NULL') {
          c.hit(j, j + 1, 'WHEN NULL never matches in a simple CASE. Write CASE WHEN x IS NULL.');
        }
      }
    });
  } },
  { id: 'QA103', level: 'warn', title: 'NOT IN with a subquery', engines: null, check(c) {
    const S = c.S;
    S.forEach((t, i) => {
      if (t.u === 'NOT' && S[i + 1] && S[i + 1].u === 'IN' && S[i + 2] && S[i + 2].v === '('
          && S[i + 3] && (S[i + 3].u === 'SELECT' || S[i + 3].u === 'WITH')) {
        c.hit(i, i + 1, 'NOT IN gives no rows when the subquery returns a NULL. Use NOT EXISTS.');
      }
    });
  } },
  { id: 'QA104', level: 'info', title: 'Comma join', engines: null, check(c) {
    const { S, depth } = c;
    S.forEach((t, i) => {
      if (t.u !== 'FROM' || (S[i - 1] && S[i - 1].u === 'DISTINCT')) return;
      // The FROM must belong to a query at its own depth, not to a call
      // such as EXTRACT(YEAR FROM d).
      let owner = false;
      for (let k = i - 1; k >= 0 && depth[k] >= depth[i]; k--) {
        if (depth[k] === depth[i] && S[k].k === 'word' && (S[k].u === 'SELECT' || S[k].u === 'DELETE' || S[k].u === 'UPDATE')) { owner = true; break; }
      }
      if (!owner) return;
      for (let j = i + 1; j < S.length && depth[j] >= depth[i]; j++) {
        if (depth[j] > depth[i]) continue;
        if (S[j].v === ';' || (S[j].k === 'word' && QH_FROM_END.has(S[j].u))) break;
        if (S[j].v !== ',') continue;
        const e = qaNameEnd(S, j + 1);
        if (e < 0 || (S[e + 1] && S[e + 1].v === '(')) continue;
        c.hit(j, e, 'A comma join is a cross join until WHERE links the tables. Write JOIN ... ON.');
      }
    });
  } },
  { id: 'QA105', level: 'info', title: 'Leading wildcard', engines: null, check(c) {
    c.likes((k, body) => {
      if (body.startsWith('%')) c.hit(k, k, 'A pattern that starts with % cannot use an index. The server reads every row.');
    });
  } },
  { id: 'QA106', level: 'info', title: 'LIKE without a wildcard', engines: null, check(c) {
    c.likes((k, body) => {
      const wild = c.engine === 'mssql' ? /[%_[]/ : /[%_]/;
      if (!wild.test(body)) c.hit(k, k, 'LIKE without % or _ matches the whole value. Use =.');
    });
  } },
  { id: 'QA107', level: 'info', title: 'Function on a column in WHERE', engines: null, check(c) {
    const { S, depth } = c;
    S.forEach((w, i) => {
      if (w.u !== 'WHERE') return;
      for (let j = i + 1; j < S.length && depth[j] >= depth[i]; j++) {
        if (depth[j] === depth[i] && (S[j].v === ';' || (S[j].k === 'word' && QH_FROM_END.has(S[j].u) && S[j].u !== 'WHERE'))) break;
        const f = S[j];
        if (f.k !== 'word' || QA_NOT_FUNC.has(f.u) || !S[j + 1] || S[j + 1].v !== '(') continue;
        const p = S[j - 1];
        if (!p || !(p.v === '(' || (p.k === 'word' && (p.u === 'WHERE' || p.u === 'AND' || p.u === 'OR' || p.u === 'NOT')))) continue;
        const close = qaClose(S, depth, j + 1);
        if (close < 0) continue;
        let col = false;
        for (let k = j + 2; k < close && !col; k++) col = qaIsName(S, k);
        if (!col) continue;
        const n = S[close + 1];
        const cmp = n && ((n.k === 'p' && qaOpAt(S, close + 1)) || (n.k === 'word' && (QA_CMP_WORDS.has(n.u)
          || (n.u === 'NOT' && S[close + 2] && QA_CMP_WORDS.has(S[close + 2].u)))));
        if (cmp) c.hit(j, close, 'A function on a column in WHERE keeps the server from using an index on that column. Compare the bare column.');
      }
    });
  } },
  { id: 'QA108', level: 'info', title: 'UNION without ALL', engines: null, check(c) {
    c.S.forEach((t, i) => {
      const n = c.S[i + 1];
      if (t.u === 'UNION' && !(n && (n.u === 'ALL' || n.u === 'DISTINCT'))) {
        c.hit(i, i, 'UNION keeps one copy of each row, and that needs a sort. Use UNION ALL when the parts cannot repeat.');
      }
    });
  } },
  { id: 'QA109', level: 'info', title: 'ORDER BY a column number', engines: null, check(c) {
    const { S, depth } = c;
    const ends = new Set(['LIMIT', 'OFFSET', 'FETCH', 'FOR', 'UNION', 'INTERSECT', 'EXCEPT', 'OPTION', 'ROWS', 'RANGE', 'SETTINGS']);
    S.forEach((t, i) => {
      if (t.u !== 'ORDER' || !S[i + 1] || S[i + 1].u !== 'BY') return;
      let start = true;
      for (let j = i + 2; j < S.length && depth[j] >= depth[i]; j++) {
        if (depth[j] > depth[i]) { start = false; continue; }
        if (S[j].v === ';' || S[j].v === ')' || (S[j].k === 'word' && ends.has(S[j].u))) break;
        if (S[j].v === ',') { start = true; continue; }
        const n = S[j + 1];
        if (start && S[j].k === 'num' && (!n || n.v === ',' || n.v === ';' || n.v === ')' || ['ASC', 'DESC', 'NULLS'].includes(n.u) || ends.has(n.u))) {
          c.hit(j, j, 'ORDER BY a column number breaks when the select list changes. Name the column.');
        }
        start = false;
      }
    });
  } },
  { id: 'QA110', level: 'warn', title: 'INSERT without a column list', engines: null, write: true, check(c) {
    const S = c.S;
    S.forEach((t, i) => {
      if (t.u !== 'INSERT') return;
      const k = S[i + 1] && S[i + 1].u === 'INTO' ? i + 2 : i + 1;
      const e = qaNameEnd(S, k);
      if (e < 0) return;
      const n = S[e + 1], n2 = S[e + 2];
      const bare = n && (n.u === 'VALUES' || n.u === 'SELECT' || n.u === 'WITH'
        || (n.v === '(' && n2 && (n2.u === 'SELECT' || n2.u === 'WITH')));
      if (bare) c.hit(i, e, 'INSERT without a column list breaks when the table gets a new column. Name the columns.');
    });
  } },
  { id: 'QA111', level: 'warn', title: 'No WHERE', engines: null, write: true, check(c) {
    const v = c.verb;
    if (v == null || (c.S[v].u !== 'UPDATE' && c.S[v].u !== 'DELETE')) return;
    if (c.S[v].u === 'UPDATE' && c.S[v + 1] && c.S[v + 1].u === 'STATISTICS') return;
    if (c.S.some((t, i) => t.u === 'WHERE' && c.depth[i] === c.depth[v])) return;
    c.hit(v, v, c.S[v].u === 'UPDATE' ? 'UPDATE without WHERE changes every row of the table.'
      : 'DELETE without WHERE deletes every row of the table.');
  } },
  { id: 'QA201', level: 'warn', title: 'pg_sleep', engines: ['postgres'], check(c) {
    c.S.forEach((t, i) => {
      if (/^PG_SLEEP(_FOR|_UNTIL)?$/.test(t.u || '') && c.S[i + 1] && c.S[i + 1].v === '(') {
        c.hit(i, i, 'pg_sleep keeps a connection and its transaction open while it waits.');
      }
    });
  } },
  { id: 'QA202', level: 'info', title: 'money type', engines: ['postgres'], check(c) {
    if (!c.ddl) return;
    c.S.forEach((t, i) => {
      if (t.u === 'MONEY' && qaIsColumnType(c.S, i)) c.hit(i, i, 'money rounds by lc_monetary, a server setting. Use numeric.');
    });
  } },
  { id: 'QA203', level: 'info', title: 'char(n) type', engines: ['postgres'], check(c) {
    if (!c.ddl) return;
    c.S.forEach((t, i) => {
      if ((t.u !== 'CHAR' && t.u !== 'CHARACTER') || !qaIsColumnType(c.S, i)) return;
      const n = c.S[i + 1];
      if (n && n.u === 'VARYING') return;
      const e = n && n.v === '(' ? qaClose(c.S, c.depth, i + 1) : i;
      c.hit(i, e < 0 ? i : e, 'char(n) pads each value with spaces. Use text or varchar.');
    });
  } },
  { id: 'QA204', level: 'info', title: 'timestamp without time zone', engines: ['postgres'], check(c) {
    if (!c.ddl) return;
    const S = c.S;
    S.forEach((t, i) => {
      if (t.u !== 'TIMESTAMP' || !qaIsColumnType(S, i)) return;
      let k = i + 1;
      if (S[k] && S[k].v === '(') { const e = qaClose(S, c.depth, k); k = e < 0 ? k : e + 1; }
      if (S[k] && S[k].u === 'WITH') return;
      const e = S[k] && S[k].u === 'WITHOUT' && S[k + 2] && S[k + 2].u === 'ZONE' ? k + 2 : k - 1;
      c.hit(i, e, 'timestamp without time zone does not mark one moment. Use timestamptz.');
    });
  } },
  { id: 'QA301', level: 'warn', title: 'Dirty read', engines: ['mssql'], check(c) {
    c.S.forEach((t, i) => {
      if (t.u === 'NOLOCK' || t.u === 'READUNCOMMITTED') c.hit(i, i, 'NOLOCK reads rows that are not committed. A row can show twice or not at all.');
      else if (t.u === 'READ' && c.S[i + 1] && c.S[i + 1].u === 'UNCOMMITTED') {
        c.hit(i, i + 1, 'READ UNCOMMITTED reads rows that are not committed. A row can show twice or not at all.');
      }
    });
  } },
  { id: 'QA302', level: 'warn', title: '@@IDENTITY', engines: ['mssql'], check(c) {
    const S = c.S;
    S.forEach((t, i) => {
      if (t.v === '@' && S[i + 1] && S[i + 1].v === '@' && S[i + 2] && S[i + 2].u === 'IDENTITY'
          && t.e === S[i + 1].s && S[i + 1].e === S[i + 2].s) {
        c.hit(i, i + 2, '@@IDENTITY can return the id that a trigger made. Use SCOPE_IDENTITY().');
      }
    });
  } },
  { id: 'QA303', level: 'warn', title: 'Old large-object type', engines: ['mssql'], check(c) {
    if (!c.ddl) return;
    c.S.forEach((t, i) => {
      if ((t.u === 'TEXT' || t.u === 'NTEXT' || t.u === 'IMAGE') && qaIsColumnType(c.S, i)) {
        c.hit(i, i, 'SQL Server marks ntext, text and image as old types. Use nvarchar(max), varchar(max) or varbinary(max).');
      }
    });
  } },
  { id: 'QA304', level: 'warn', title: 'WAITFOR', engines: ['mssql'], check(c) {
    c.S.forEach((t, i) => {
      if (t.u !== 'WAITFOR') return;
      const n = c.S[i + 1];
      c.hit(i, n && (n.u === 'DELAY' || n.u === 'TIME') ? i + 1 : i, 'WAITFOR keeps the session open while it waits.');
    });
  } },
  { id: 'QA401', level: 'info', title: 'count(DISTINCT)', engines: ['clickhouse'], check(c) {
    const S = c.S;
    S.forEach((t, i) => {
      if (t.u === 'COUNT' && S[i + 1] && S[i + 1].v === '(' && S[i + 2] && S[i + 2].u === 'DISTINCT') {
        c.hit(i, i + 2, 'count(DISTINCT) is exact and slow on a large table. uniq() is faster when an estimate is enough.');
      }
    });
  } },
  { id: 'QA402', level: 'info', title: 'FINAL', engines: ['clickhouse'], check(c) {
    c.S.forEach((t, i) => {
      const p = c.S[i - 1];
      if (t.u === 'FINAL' && p && (p.k === 'qid' || (p.k === 'word' && !QH_NOT_NAME.has(p.u)))) {
        c.hit(i, i, 'FINAL merges the data parts while the query reads them. The query is slower.');
      }
    });
  } },
  { id: 'QA501', level: 'warn', title: 'SELECT * on Athena', engines: ['athena'], check(c) {
    const { S, depth } = c;
    S.forEach((t, i) => {
      if (t.v !== '*') return;
      const p = S[i - 1];
      let list = p && (p.u === 'SELECT' || p.u === 'DISTINCT' || p.u === 'ALL');
      if (!list && p && p.v === ',') list = qaClauseOf(S, depth, i, new Set(['SELECT', ...QH_STAR_STOP])) === 'SELECT';
      if (list) c.hit(i, i, 'Athena bills by the data that a query scans. SELECT * reads every column.');
    });
  } },
  { id: 'QA502', level: 'info', title: 'ORDER BY without LIMIT', engines: ['athena'], check(c) {
    const { S, depth } = c;
    const at = S.findIndex((t, i) => t.u === 'ORDER' && depth[i] === 0 && S[i + 1] && S[i + 1].u === 'BY');
    if (at < 0 || S.some((t, i) => depth[i] === 0 && (t.u === 'LIMIT' || t.u === 'FETCH'))) return;
    c.hit(at, at + 1, 'ORDER BY without LIMIT sorts every row on one node. Add LIMIT.');
  } },
];

// Run the rules for one engine over the text. qhLintFindings returns one
// finding per rule hit: [{ start, end, level, id, title, text, message }],
// sorted by start, overlaps kept. The findings list (Problems tab) shows them.
// qhLint returns [{ start, end, level, message }] with no overlap: a finding
// inside another one joins its message to it. The editor marks those.
function qhLintFindings(sql, engineId) {
  const text = String(sql || '');
  if (!text.trim()) return [];
  const eng = String(engineId || 'postgres');
  const info = (typeof QH_ENGINES !== 'undefined' && QH_ENGINES[eng]) || null;
  const readOnly = info ? !!info.readOnly : (eng === 'clickhouse' || eng === 'athena');
  const rules = QH_LINT_RULES.filter(r => (!r.engines || r.engines.includes(eng)) && !(r.write && readOnly));
  const all = qhSqlTokens(text);
  // Split into statements at a top-level `;`, with the depth of each token.
  const stmts = [];
  let cur = { S: [], depth: [] }, d = 0;
  all.forEach((t) => {
    if (t.v === '(') { cur.S.push(t); cur.depth.push(d); d++; return; }
    if (t.v === ')') { d = Math.max(0, d - 1); cur.S.push(t); cur.depth.push(d); return; }
    if (t.v === ';' && d === 0) { stmts.push(cur); cur = { S: [], depth: [] }; return; }
    cur.S.push(t); cur.depth.push(d);
  });
  stmts.push(cur);
  const found = [];
  stmts.forEach(({ S, depth }) => {
    if (!S.length) return;
    // The statement's verb: its first word, or after WITH the first top-level
    // SELECT, INSERT, UPDATE, DELETE or MERGE.
    let verb = S.findIndex(t => t.k === 'word');
    if (verb >= 0 && S[verb].u === 'WITH') {
      verb = S.findIndex((t, i) => depth[i] === 0 && ['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'MERGE'].includes(t.u));
    }
    const first = verb >= 0 ? S[verb].u : '';
    const likes = (fn) => S.forEach((t, i) => {
      if (t.u !== 'LIKE' && t.u !== 'ILIKE') return;
      let k = i + 1;
      if (S[k] && S[k].k === 'word' && S[k].u === 'N' && S[k + 1] && S[k + 1].k === 'str' && S[k].e === S[k + 1].s) k++;
      if (S[k] && S[k].k === 'str' && S[k].v[0] === "'") fn(k, S[k].v.slice(1, -1));
    });
    rules.forEach((r) => {
      const hit = (a, b, msg) => found.push({ start: S[a].s, end: S[b].e, level: r.level,
        id: r.id, title: r.title, text: msg, message: r.title + ' (' + r.id + ')\n' + msg });
      r.check({ S, depth, engine: eng, verb: verb >= 0 ? verb : null, ddl: first === 'CREATE' || first === 'ALTER', hit, likes });
    });
  });
  found.sort((x, y) => x.start - y.start || y.end - x.end);
  return found;
}
function qhLint(sql, engineId) {
  const out = [];
  qhLintFindings(sql, engineId).forEach((f) => {
    const last = out[out.length - 1];
    if (last && f.start < last.end) {
      if (!last.message.split('\n\n').includes(f.message)) last.message += '\n\n' + f.message;
      last.end = Math.max(last.end, f.end);
      if (f.level === 'warn') last.level = 'warn';
      return;
    }
    out.push({ start: f.start, end: f.end, level: f.level, message: f.message });
  });
  return out;
}

// True when every row of a suggestion list inserts exactly the word that is
// already typed (case ignored), for example `NULL` after `= NULL`. A snippet
// inserts other text, so it always counts.
function qhListAddsNothing(items, typed) {
  const want = String(typed || '').toUpperCase();
  const rows = (items || []).filter(it => !it.header);
  return !!want && rows.length > 0 && rows.every(it => it.type !== 'snippet' && String(it.text || '').trim().toUpperCase() === want);
}

// The text of an overlay layer: the editor text with each range in a
// <mark>. `cls` gives a range its class. A range that starts inside the one
// before it is cut to the part after it.
function qhMarkHtml(value, list, cls) {
  let out = '', pos = 0;
  list.forEach((r, i) => {
    const s = Math.max(r.start, pos), e = Math.min(r.end, value.length);
    if (e <= s) return;
    out += qhEscape(value.slice(pos, s)) + `<mark class="${cls(r)}" data-i="${i}">` + qhEscape(value.slice(s, e)) + '</mark>';
    pos = e;
  });
  return out + qhEscape(value.slice(pos)) + '\n';
}

// A case-insensitive lookup that only answers when exactly one key matches.
function qhLookupCi(map, key, exact) {
  if (!map) return undefined;
  if (Object.prototype.hasOwnProperty.call(map, key)) return map[key];
  if (exact) return undefined;
  const want = key.toLowerCase();
  let hit, n = 0;
  for (const k of Object.keys(map)) if (k.toLowerCase() === want) { hit = map[k]; n++; }
  return n === 1 ? hit : undefined;
}

// A token that can be a table name or an alias.
function qhFromIsName(t) {
  return !!t && (t.k === 'qid' || (t.k === 'word' && !QH_NOT_NAME.has(t.u)));
}
// A name token as the server reads it: no quotes, doubled quotes undone.
function qhFromUnquote(t) {
  return t.k !== 'qid' ? t.v
    : t.v[0] === '"' ? t.v.slice(1, -1).replace(/""/g, '"') : t.v.slice(1, -1).replace(/\]\]/g, ']');
}

// The FROM list of the query block at depth D, searched from token `from`.
// Returns [{ parts, alias }] in FROM order, or null when the list cannot be
// read exactly: a subquery or a function in FROM, NATURAL and USING joins,
// column aliases. `loose` (the Ctrl+Space column list) also reads NATURAL and
// USING joins, and a list that ends half typed (`FROM a JOIN `). `subs` (the
// column list only) also reads a subquery `(SELECT ...) x` as
// { parts: [], alias, sub: { a, b }, cols }: its body is S[a] .. S[b - 1] and
// `cols` holds the names of `x (c1, c2)`, else null. Every other caller needs
// null for a subquery, so it stays off for them.
function qhReadFromList(S, depth, from, D, loose, subs) {
  let f = -1;
  for (let i = from; i < S.length; i++) {
    if (depth[i] < D) break;
    if (depth[i] > D || S[i].k !== 'word') continue;
    if (S[i].u === 'FROM') { f = i; break; }
    if (QH_FROM_END.has(S[i].u)) break;
  }
  if (f < 0) return null;
  const F = [];
  for (let i = f + 1; i < S.length; i++) {
    if (depth[i] < D) break;
    if (depth[i] === D && S[i].k === 'word' && QH_FROM_END.has(S[i].u)) break;
    F.push({ t: S[i], d: depth[i] - D, x: i });
  }

  const top = (j) => F[j] && F[j].d === 0;
  const word = (j, ...us) => top(j) && F[j].t.k === 'word' && us.includes(F[j].t.u);
  const name = (j) => top(j) && qhFromIsName(F[j].t);
  // The length of a join keyword run at j: 0 when there is none, -1 for a join
  // whose columns cannot be listed exactly (NATURAL, APPLY).
  const joinLen = (j) => {
    if (word(j, 'NATURAL')) {
      if (!loose) return -1;
      const n = joinLen(j + 1);
      return n > 0 ? n + 1 : -1;
    }
    if (word(j, 'CROSS', 'OUTER') && word(j + 1, 'APPLY')) return -1;
    if (word(j, 'JOIN')) return 1;
    if (word(j, 'INNER', 'CROSS') && word(j + 1, 'JOIN')) return 2;
    if (word(j, 'LEFT', 'RIGHT', 'FULL')) {
      if (word(j + 1, 'JOIN')) return 2;
      if (word(j + 1, 'OUTER') && word(j + 2, 'JOIN')) return 3;
    }
    return 0;
  };
  let i = 0;
  // A subquery in FROM, LATERAL or not. Null when it is not closed yet.
  const readSub = () => {
    let j = i;
    if (word(j, 'LATERAL')) j++;
    const head = F[j + 1] && F[j + 1].t;
    if (!(top(j) && F[j].t.v === '(' && head && head.k === 'word' && (head.u === 'SELECT' || head.u === 'WITH'))) return undefined;
    const a = F[j + 1].x;
    j++;
    while (j < F.length && !(top(j) && F[j].t.v === ')')) j++;
    if (j >= F.length) return null;
    const b = F[j].x;
    i = j + 1;
    let alias = null;
    if (word(i, 'AS')) { if (!name(i + 1)) return null; alias = F[i + 1].t; i += 2; }
    else if (name(i)) { alias = F[i].t; i++; }
    let cols = null;
    if (alias && top(i) && F[i].t.v === '(') {
      cols = [];
      i++;
      while (i < F.length && !(top(i) && F[i].t.v === ')')) { if (F[i].d === 1 && qhFromIsName(F[i].t)) cols.push(F[i].t); i++; }
      i++;
    }
    return { parts: [], alias, sub: { a, b }, cols };
  };
  const readRef = () => {
    if (subs) { const sq = readSub(); if (sq !== undefined) return sq; }
    if (word(i, 'ONLY')) i++;
    if (!name(i)) return null;                              // a subquery, LATERAL, nothing
    const parts = [F[i].t];
    i++;
    while (top(i) && F[i].t.v === '.' && name(i + 1)) { parts.push(F[i + 1].t); i += 2; }
    if (top(i) && F[i].t.v === '(') return null;            // a table function
    if (top(i) && F[i].t.v === '*') i++;                    // `t *`: t and its children
    let alias = null;
    if (word(i, 'AS')) { if (!name(i + 1)) return null; alias = F[i + 1].t; i += 2; }
    else if (name(i)) { alias = F[i].t; i++; }
    if (top(i) && F[i].t.v === '(') return null;            // column aliases rename the columns
    if (word(i, 'WITH') && top(i + 1) && F[i + 1].t.v === '(') {  // T-SQL table hints
      i += 2;
      while (F[i] && !(top(i) && F[i].t.v === ')')) i++;
      i++;
    }
    if (word(i, 'TABLESAMPLE')) return null;
    return { parts, alias };
  };
  const refs = [];
  for (;;) {
    let joined = false;
    if (refs.length) {
      if (top(i) && F[i].t.v === ',') i++;
      else {
        const n = joinLen(i);
        if (n <= 0) return null;
        i += n;
        joined = true;
      }
    }
    if (loose && refs.length && i >= F.length) break;      // the next table is not typed yet
    const r = readRef();
    if (!r) return null;
    refs.push(r);
    if (joined) {
      if (word(i, 'USING')) {
        if (!loose) return null;
        i++;
        if (top(i) && F[i].t.v === '(') {
          i++;
          while (i < F.length && !(top(i) && F[i].t.v === ')')) i++;
          i++;
        }
      }
      if (word(i, 'ON')) {
        i++;
        while (i < F.length && !(top(i) && (F[i].t.v === ',' || joinLen(i) !== 0))) i++;
      }
    }
    if (i >= F.length) break;
  }
  return refs;
}

// What the catalog holds for one FROM entry, from a qualified map and a bare
// map. Qualified when written qualified. A bare name two schemas share is left
// to the server, which resolves it with the search_path the query runs under.
function qhRefLookup(r, sch, qMap, bareMap) {
  const parts = r.parts.map(qhFromUnquote), exact = r.parts.some(p => p.k === 'qid');
  if (parts.length >= 2) return qhLookupCi(qMap, parts[parts.length - 2] + '.' + parts[parts.length - 1], exact);
  if (qhLookupCi(sch.qualify, parts[0], exact) !== null) return qhLookupCi(bareMap, parts[0], exact);
  return undefined;
}

// ---------------------------------------------------------------------------
// CTE and subquery columns (2026-11-11, Devart A20)
//
// The column list also reads a CTE of the statement and a subquery in FROM.
// Their columns are the output names of the first SELECT: an alias, a bare
// column, the tail of `t.col`. A list `name (a, b) AS (` or `(...) x (a, b)`
// gives the names instead. A `*` counts only when the catalog or the live
// lookup knows every relation that it covers. An expression without an alias
// has no name, so the list leaves it out. A column that comes straight from a
// table keeps that table's type, nullability and PII mark.
// ---------------------------------------------------------------------------

// The CTEs of a statement: lower-case name -> { name, cols, a, b }. `name` is
// the name token, `cols` the tokens of an explicit column list or null. The
// body is S[a] .. S[b - 1].
function qhCteDefs(S, depth) {
  const out = new Map();
  for (let w = 0; w < S.length; w++) {
    if (S[w].k !== 'word' || S[w].u !== 'WITH') continue;
    let i = w + 1;
    if (S[i] && S[i].u === 'RECURSIVE') i++;
    for (;;) {
      if (!qhFromIsName(S[i])) break;
      const nameTok = S[i];
      i++;
      let cols = null;
      if (S[i] && S[i].v === '(') {
        const d0 = depth[i];
        cols = [];
        i++;
        while (i < S.length && !(S[i].v === ')' && depth[i] === d0)) { if (qhFromIsName(S[i])) cols.push(S[i]); i++; }
        i++;
      }
      if (!S[i] || S[i].u !== 'AS') break;
      i++;
      if (S[i] && S[i].u === 'NOT') i++;
      if (S[i] && S[i].u === 'MATERIALIZED') i++;
      if (!S[i] || S[i].v !== '(') break;
      const d0 = depth[i], a = i + 1;
      i++;
      while (i < S.length && !(S[i].v === ')' && depth[i] === d0)) i++;
      const key = qhFromUnquote(nameTok).toLowerCase();
      if (!out.has(key)) out.set(key, { name: nameTok, cols, a, b: i });
      i++;
      if (!S[i] || S[i].v !== ',') break;
      i++;
    }
  }
  return out;
}

// The CTE names of the statement around the caret, as written. The typing
// list offers them after FROM and JOIN.
function qhCteNamesAt(sql, caret) {
  const toks = qhSqlTokens(String(sql || ''));
  let a = 0, b = toks.length;
  for (let i = 0; i < toks.length; i++) {
    if (toks[i].v !== ';') continue;
    if (toks[i].e <= caret) a = i + 1; else { b = i; break; }
  }
  const S = toks.slice(a, b);
  const depth = [];
  let d = 0;
  for (const t of S) { if (t.v === ')') d--; depth.push(d); if (t.v === '(') d++; }
  const out = [];
  qhCteDefs(S, depth).forEach((c) => { if (c.name.s < caret) out.push(c.name.v); });
  return out;
}

// The name of an output column as the engine stores it: PostgreSQL and
// Athena fold a name without quotes to lower case.
function qhOutName(t, engineId) {
  const n = qhFromUnquote(t);
  return t.k !== 'qid' && (engineId === 'postgres' || engineId === 'athena') ? n.toLowerCase() : n;
}

// The output columns of the query S[a] .. S[b - 1], from its first SELECT.
// `of(ref, ask)` gives the columns of a FROM entry (see qhRelCols). `named`
// (the tokens of `name (a, b)`) gives the names by position. Returns an array,
// null when the list cannot be read, or undefined when a `*` waits for the
// live lookup.
function qhSelectOutCols(S, depth, a, b, of, engineId, named) {
  const names = named ? named.map(t => ({ name: qhOutName(t, engineId) })) : null;
  const D = depth[a];
  let sel = -1;
  for (let i = a; i < b; i++) {
    if (depth[i] === D && S[i].k === 'word' && S[i].u === 'SELECT') { sel = i; break; }
  }
  if (sel < 0) return names;
  let end = b;
  for (let i = sel + 1; i < b; i++) {
    if (depth[i] !== D || S[i].k !== 'word') continue;
    if (S[i].u === 'FROM' || S[i].u === 'INTO' || QH_FROM_END.has(S[i].u)) { end = i; break; }
  }
  // Split the select list at the commas of this level. DISTINCT, ALL,
  // DISTINCT ON (...), TOP n and TOP (n) are no part of the first item.
  const parts = [];
  let cur = [];
  for (let i = sel + 1; i < end; i++) {
    if (depth[i] === D && S[i].v === ',') { parts.push(cur); cur = []; continue; }
    cur.push(i);
  }
  parts.push(cur);
  let p0 = parts[0];
  const skipParens = () => { let q = 1; while (q < p0.length && !(S[p0[q]].v === ')' && depth[p0[q]] === D)) q++; p0 = p0.slice(q + 1); };
  while (p0.length && (S[p0[0]].u === 'DISTINCT' || S[p0[0]].u === 'ALL')) {
    const on = S[p0[0]].u === 'DISTINCT' && p0[1] !== undefined && S[p0[1]].u === 'ON';
    p0 = p0.slice(on ? 2 : 1);
    if (on && p0.length && S[p0[0]].v === '(') skipParens();
  }
  if (p0.length && S[p0[0]].u === 'TOP') {
    p0 = p0.slice(1);
    if (p0.length && S[p0[0]].v === '(') skipParens();
    else if (p0.length && S[p0[0]].k === 'num') p0 = p0.slice(1);
    while (p0.length && (S[p0[0]].u === 'PERCENT' || (S[p0[0]].u === 'WITH' && p0[1] !== undefined && S[p0[1]].u === 'TIES'))) p0 = p0.slice(S[p0[0]].u === 'WITH' ? 2 : 1);
  }
  parts[0] = p0;

  const refs = qhReadFromList(S, depth, sel + 1, D, true, true);
  const label = (r) => (r.alias || r.parts[r.parts.length - 1] || null);
  const memo = new Map();
  const colsOf = (r, ask) => {
    const k = (ask ? 'a' : 'q') + refs.indexOf(r);
    if (!memo.has(k)) memo.set(k, of(r, ask));
    return memo.get(k);
  };
  const find = (cols, n) => (Array.isArray(cols) ? cols.find(c => c && String(c.name).toLowerCase() === n.toLowerCase()) : undefined);
  const isRef = (T) => T.length % 2 === 1 && !(T.length === 1 && ['NULL', 'TRUE', 'FALSE'].includes(T[0].u))
    && T.every((t, j) => (j % 2 === 0 ? qhFromIsName(t) : t.v === '.'));
  // The source column of `col` or `q.col`, when exactly one relation has it.
  const source = (T) => {
    if (!refs) return null;
    const n = qhFromUnquote(T[T.length - 1]);
    if (T.length >= 3) {
      const q = qhFromUnquote(T[T.length - 3]).toLowerCase();
      const r = refs.filter(x => label(x) && qhFromUnquote(label(x)).toLowerCase() === q);
      return r.length === 1 ? (find(colsOf(r[0], false), n) || null) : null;
    }
    const hits = refs.map(r => find(colsOf(r, false), n)).filter(Boolean);
    return hits.length === 1 ? hits[0] : null;
  };
  const out = [];
  let skipped = false, waiting = false;
  for (const p of parts) {
    if (!p.length) { skipped = true; continue; }
    const T = p.map(i => S[i]);
    // `*` and `q.*`.
    const star = T.length === 1 && T[0].v === '*';
    const qstar = T.length >= 3 && T[T.length - 1].v === '*' && T[T.length - 2].v === '.' && isRef(T.slice(0, -2));
    if (star || qstar) {
      if (!refs || !refs.length) return null;
      let want = refs;
      if (qstar) {
        const q = qhFromUnquote(T[T.length - 3]).toLowerCase();
        want = refs.filter(r => label(r) && qhFromUnquote(label(r)).toLowerCase() === q);
        if (want.length !== 1) return null;
      }
      for (const r of want) {
        const c = colsOf(r, true);
        if (c === undefined) { waiting = true; continue; }
        if (!c) return null;
        c.forEach(x => out.push(x));
      }
      continue;
    }
    // The name of the item: `expr AS n`, `n = expr` (SQL Server), `expr n`,
    // else the tail of a column reference.
    let nameTok = null, expr = T;
    const L = T.length;
    if (L >= 3 && T[L - 2].k === 'word' && T[L - 2].u === 'AS' && depth[p[L - 2]] === D && qhFromIsName(T[L - 1])) {
      nameTok = T[L - 1]; expr = T.slice(0, -2);
    } else if (engineId === 'mssql' && L >= 3 && qhFromIsName(T[0]) && T[1].v === '=') {
      nameTok = T[0]; expr = T.slice(2);
    } else if (L >= 2) {
      const last = T[L - 1], prev = T[L - 2];
      const lastName = last.k === 'qid' || (last.k === 'word' && !QH_KW_SET.has(last.u));
      const prevEnds = prev.k === 'qid' || prev.k === 'num' || prev.k === 'str' || prev.v === ')'
        || (prev.k === 'word' && (!QH_KW_SET.has(prev.u) || prev.u === 'END'));
      if (lastName && prevEnds) { nameTok = last; expr = T.slice(0, -1); }
    }
    const ref = isRef(expr);
    if (!nameTok && !ref) { skipped = true; continue; }
    const src = ref ? source(expr) : null;
    if (nameTok) out.push(src ? { ...src, name: qhOutName(nameTok, engineId) } : { name: qhOutName(nameTok, engineId) });
    else out.push(src ? { ...src } : { name: qhOutName(expr[expr.length - 1], engineId) });
  }
  if (waiting) return undefined;
  if (names) return (!skipped && out.length === names.length) ? names.map((n, j) => ({ ...out[j], name: n.name })) : names;
  return out;
}

// The column reader for the column list. `of(ref, ask)` gives the columns of
// one FROM entry: a subquery, a CTE of the statement, the catalog, or the live
// lookup. It returns an array of { name, type?, nullable?, pk?, pii? }, null
// when nobody knows the relation, or undefined when the lookup must ask the
// server first. Only `ask` collects such names in `need`. Without `ask` an
// unknown relation gives null.
function qhRelCols(S, depth, schema, live, engineId) {
  const sch = schema || {};
  const ctes = qhCteDefs(S, depth);
  const busy = new Set();
  const need = [];
  const of = (r, ask) => {
    if (r.sub) return qhSelectOutCols(S, depth, r.sub.a, r.sub.b, of, engineId, r.cols);
    if (r.parts.length === 1) {
      const key = qhFromUnquote(r.parts[0]).toLowerCase();
      const c = ctes.get(key);
      if (c) {
        if (busy.has(key)) return null;                     // a CTE that reads itself
        busy.add(key);
        try { return qhSelectOutCols(S, depth, c.a, c.b, of, engineId, c.cols); } finally { busy.delete(key); }
      }
    }
    const meta = qhRefLookup(r, sch, sch.tableMetaQ, sch.tableMeta);
    if (meta && meta.length) return meta;
    const names = qhRefLookup(r, sch, sch.tableColsQ, sch.tableCols);
    const n = r.parts.map(x => x.v).join('.');
    const got = (names && names.length) ? names : (live ? live(n) : null);
    if (got === undefined) {
      if (!ask) return null;
      if (!need.includes(n)) need.push(n);
      return undefined;
    }
    return Array.isArray(got) ? got.map(x => ({ name: x })) : null;
  };
  return { of, need };
}

// The column list of an INSERT: the caret is in `INSERT INTO t (a, |` (or
// `INSERT t (` on SQL Server) after `(` or a comma. Returns { parts, listed }:
// the name tokens of t and the lower-case names already in the list. Else null.
function qhInsertColsSlot(S, depth, p) {
  if (!S[p] || (S[p].v !== '(' && S[p].v !== ',')) return null;
  let o = p;
  while (o >= 0 && S[o].v !== '(') {
    if (S[o].v !== ',' && !qhFromIsName(S[o])) return null;
    o--;
  }
  // A table name is no keyword: `INSERT INTO (` names no table.
  const isTable = (t) => qhFromIsName(t) && !(t.k === 'word' && QH_KW_SET.has(t.u));
  if (o < 1 || !isTable(S[o - 1])) return null;
  let k = o - 1;
  const parts = [S[k]];
  k--;
  while (k >= 1 && S[k].v === '.' && isTable(S[k - 1])) { parts.unshift(S[k - 1]); k -= 2; }
  if (!S[k] || S[k].k !== 'word') return null;
  if (S[k].u === 'INTO') { if (!S[k - 1] || S[k - 1].u !== 'INSERT') return null; }
  else if (S[k].u !== 'INSERT') return null;
  const listed = new Set();
  for (let j = o + 1; j < S.length && !(S[j].v === ')' && depth[j] === depth[o]); j++) {
    if (depth[j] === depth[o] + 1 && qhFromIsName(S[j])) listed.add(qhFromUnquote(S[j]).toLowerCase());
  }
  return { parts, listed };
}

// ---------------------------------------------------------------------------
// Ctrl+Space column list
//
// In a column position of a SELECT (the select list, WHERE, ON, HAVING, GROUP
// BY, ORDER BY), the columns of every table in that SELECT's FROM list, grouped
// by table in FROM order. One table: bare names. Several: each name qualified
// by the alias, or by the table name when there is no alias. A typed `t.`
// lists t only, bare. Type, nullability and the primary key come from the
// catalog (`tableMeta`). A table the catalog does not list asks `live(name)`
// (names only). A table nobody knows lists nothing, never invented columns.
// A CTE of the statement and a subquery in FROM list their output columns
// (qhRelCols). In `INSERT INTO t (|` the list holds the columns of t that the
// list does not name yet.
//
// Returns { kind: 'ready', start, end, slot, qual, items, count },
// { kind: 'lookup', names }, or null when the caret is not in a column position.
// `slot` is where the reference starts, qualifier included. `qual` is the
// typed qualifier, or ''.
// ---------------------------------------------------------------------------
const QH_CL_CLAUSE = new Set(['SELECT', 'WHERE', 'ON', 'HAVING', 'QUALIFY', 'PREWHERE']);
const QH_CL_STOP = new Set(['FROM', 'JOIN', 'INTO', 'UPDATE', 'SET', 'VALUES', 'USING', 'LIMIT', 'OFFSET',
  'FETCH', 'RETURNING', 'WINDOW', 'UNION', 'INTERSECT', 'EXCEPT', 'INSERT', 'DELETE', 'MERGE', 'TABLE', 'WITH']);
// Words after which a column can start. After any other word (a name, AS,
// NULL, END, ASC) the next word is an alias or a keyword.
const QH_CL_LEAD = new Set(['SELECT', 'DISTINCT', 'ALL', 'WHERE', 'AND', 'OR', 'NOT', 'ON', 'BY', 'HAVING',
  'WHEN', 'THEN', 'ELSE', 'CASE', 'LIKE', 'ILIKE', 'BETWEEN', 'IN', 'QUALIFY', 'PREWHERE', 'ANY', 'SOME']);

// A string, quoted name or comment that the text has not closed yet.
function qhClTokOpen(t) {
  const v = t.v;
  if (t.k === 'cmt') return v.startsWith('--') || !(v.length >= 4 && v.endsWith('*/'));
  if (v[0] === '$') { const tag = v.match(/^\$[A-Za-z_]*\$/)[0]; return !(v.length >= 2 * tag.length && v.endsWith(tag)); }
  if (v[0] === '[') return !(v.length >= 2 && v.endsWith(']'));
  return v.length < 2 || (v.split(v[0]).length - 1) % 2 === 1;
}

function qhColumnList(sql, caret, schema, engineId, live) {
  const text = String(sql || '');
  const pm = text.slice(0, caret).match(/[A-Za-z_][A-Za-z0-9_]*$/);
  const start = pm ? caret - pm[0].length : caret;
  let qual = null, slot = start;
  if (text[start - 1] === '.') {
    const qm = text.slice(0, start - 1).match(/(?:[A-Za-z_][A-Za-z0-9_$]*|"(?:[^"]|"")*"|\[[^\]]*\])$/);
    if (!qm) return null;
    slot = start - 1 - qm[0].length;
    if (text[slot - 1] === '.') return null;                // schema.table.col is not read here
    qual = qm[0];
  }
  const all = qhSqlTokens(text, true);
  for (const t of all) {                                    // inside a comment, a string or a quoted name
    if (t.s >= caret) break;
    if (t.k !== 'cmt' && t.k !== 'str' && t.k !== 'qid') continue;
    if (caret < t.e || (caret === t.e && qhClTokOpen(t))) return null;
  }
  // The statement around the slot, without the slot's own tokens.
  const toks = all.filter(t => t.k !== 'cmt' && !(t.e > slot && t.s < caret));
  let a = 0, b = toks.length;
  for (let i = 0; i < toks.length; i++) {
    if (toks[i].v !== ';') continue;
    if (toks[i].e <= slot) a = i + 1; else { b = i; break; }
  }
  const S = toks.slice(a, b);
  const depth = [];
  let d = 0;
  for (const t of S) { if (t.v === ')') d--; depth.push(d); if (t.v === '(') d++; }
  let p = -1;
  for (let i = 0; i < S.length && S[i].s < slot; i++) p = i;
  if (p < 0) return null;
  const prev = S[p];
  const quote = (n) => (typeof qhQuoteIdentFor === 'function' ? qhQuoteIdentFor(n, engineId) : qhQuoteIdent(n));
  const lc = pm ? pm[0].toLowerCase() : '';
  const ins = qual ? null : qhInsertColsSlot(S, depth, p);
  if (ins) {
    const res = qhRelCols(S, depth, schema, live, engineId);
    const cols = res.of({ parts: ins.parts, alias: null }, true);
    if (cols === undefined) return { kind: 'lookup', names: res.need };
    const hit = (cols || []).filter(col => col && !ins.listed.has(String(col.name).toLowerCase())
      && String(col.name).toLowerCase().startsWith(lc));
    const items = [];
    if (hit.length) items.push({ type: 'group', header: true, key: 'g0', text: '', label: ins.parts.map(x => x.v).join('.'), alias: '' });
    hit.forEach(col => items.push(qhColItem(col, 'c0:' + col.name, quote(String(col.name)))));
    return { kind: 'ready', start, end: caret, slot, qual: '', items, count: hit.length };
  }
  if (!qual) {
    if (prev.k === 'num' || prev.k === 'str' || prev.k === 'qid' || prev.v === ')' || prev.v === ':') return null;
    // A select-list star belongs to the * expansion. `a * b` is a product.
    if (prev.v === '*' && (!S[p - 1] || S[p - 1].v === ',' || S[p - 1].v === '.'
        || ['SELECT', 'DISTINCT', 'ALL'].includes(S[p - 1].u))) return null;
    if (prev.k === 'word' && !QH_CL_LEAD.has(prev.u)) return null;
  }
  // The clause the slot is in. A function's or an expression's parens are
  // left outwards, so `count(|)` is in the select list.
  let D = depth[p] + (prev.v === '(' ? 1 : 0), c = -1;
  for (let i = p; i >= 0; i--) {
    if (depth[i] < D) D = depth[i];
    if (depth[i] > D || S[i].k !== 'word') continue;
    const u = S[i].u;
    if (QH_CL_CLAUSE.has(u)) { c = i; break; }
    if (u === 'BY' && S[i - 1] && ['GROUP', 'ORDER', 'PARTITION'].includes(S[i - 1].u)) { c = i; break; }
    if (QH_CL_STOP.has(u)) return null;
  }
  if (c < 0) return null;
  // The SELECT that owns the clause, then its FROM list, before or after the caret.
  let sel = -1, Ds = depth[c];
  for (let i = c; i >= 0; i--) {
    if (depth[i] < Ds) Ds = depth[i];
    if (depth[i] > Ds || S[i].k !== 'word') continue;
    if (S[i].u === 'SELECT') { sel = i; break; }
  }
  if (sel < 0) return null;
  const refs = qhReadFromList(S, depth, sel + 1, Ds, true, true);
  if (!refs || !refs.length) return null;

  // A subquery without an alias has no label: its columns go in bare.
  const label = (r) => (r.alias || r.parts[r.parts.length - 1] || null);
  let want = refs;
  if (qual) {
    const q = qhFromUnquote(qhSqlTokens(qual)[0]).toLowerCase();
    want = refs.filter(r => label(r) && qhFromUnquote(label(r)).toLowerCase() === q);
    if (want.length !== 1) return null;
  }
  const res = qhRelCols(S, depth, schema, live, engineId);
  const cols = want.map(r => res.of(r, true));
  if (cols.some(c => c === undefined) && res.need.length) return { kind: 'lookup', names: res.need };

  const multi = !qual && refs.length > 1;
  const items = [];
  let count = 0;
  want.forEach((r, j) => {
    const hit = (cols[j] || []).filter(col => col && String(col.name).toLowerCase().startsWith(lc));
    if (!hit.length) return;
    items.push({ type: 'group', header: true, key: 'g' + j, text: '',
                 label: r.sub ? '(subquery)' : r.parts.map(x => x.v).join('.'), alias: r.alias ? r.alias.v : '' });
    hit.forEach(col => {
      const n = String(col.name);
      items.push(qhColItem(col, 'c' + j + ':' + n, multi && label(r) ? label(r).v + '.' + quote(n) : quote(n)));
      count++;
    });
  });
  return { kind: 'ready', start, end: caret, slot, qual: qual || '', items, count };
}

// One column row of the column list.
function qhColItem(col, key, text) {
  return { type: 'column', key, label: String(col.name), text,
           colType: col.type ? String(col.type) : '',
           nullable: typeof col.nullable === 'boolean' ? col.nullable : null,
           pk: !!col.pk, pii: !!col.pii };
}

// The list that typing opens after `alias.` (2026-11-10). It is the Ctrl+Space
// column list for that qualifier: the columns of the one FROM table that the
// alias or the name points at. It returns that list when it has a column, a
// { kind: 'lookup' } when a relation must be asked about first, or null. On
// null the caller keeps the normal list: a schema-qualified table, `sys.` or an
// alias that no FROM entry has.
function qhAliasColumns(sql, caret, schema, engineId, live) {
  const text = String(sql || '');
  const pm = text.slice(0, caret).match(/[A-Za-z_][A-Za-z0-9_]*$/);
  const start = pm ? caret - pm[0].length : caret;
  if (text[start - 1] !== '.') return null;
  const cl = qhColumnList(text, caret, schema, engineId, live);
  if (!cl) return null;
  if (cl.kind === 'lookup') return cl;
  return (cl.kind === 'ready' && cl.count > 0) ? cl : null;
}

// ---------------------------------------------------------------------------
// Statement templates (2026-11-11, Devart C3)
//
// Tab right after `INSERT INTO t`, `UPDATE t`, `EXEC p` (SQL Server) or
// `CALL p` (PostgreSQL) writes the rest of the statement: every column or
// parameter, each value as a field `<name>`, with the type, NOT NULL, PK and a
// default in a comment. Tab then goes from field to field. A field is no valid
// SQL, so a template that runs unfilled fails and changes nothing. The
// template also replaces the empty parts of the ii and upw snippets
// (`() VALUES ()`, `SET WHERE`). A read-only engine (ClickHouse, Athena) gets no template. A table or a
// routine that the catalog does not know gets none.
// Returns { kind, start, end, text, stops, caretAt, count } or null.
// ---------------------------------------------------------------------------
const QH_TPL_SKELETON = {
  insert: [[], ['(', ')'], ['(', ')', 'VALUES', '(', ')']],
  update: [[], ['SET'], ['SET', 'WHERE']],
  exec: [[], ['(', ')']],
};
// Words that start a PostgreSQL type, so an argument `double precision` is a
// type with no name.
const QH_TPL_TYPE_WORDS = new Set(['character', 'double', 'timestamp', 'time', 'bit', 'interval', 'national', 'char', 'varchar']);

// The parameters of a catalog signature: [{ name, type, def, out }].
function qhTplParams(args, engineId) {
  const parts = [];
  let d = 0, cur = '';
  for (const ch of String(args || '')) {
    if (ch === '(') d++;
    if (ch === ')') d--;
    if (ch === ',' && d === 0) { parts.push(cur); cur = ''; continue; }
    cur += ch;
  }
  parts.push(cur);
  const out = [];
  parts.map(x => x.trim()).filter(Boolean).forEach((x, i) => {
    let rest = x, mode = '', def = '';
    const m = rest.match(/^(IN|OUT|INOUT|VARIADIC)\s+/i);
    if (m) { mode = m[1].toUpperCase(); rest = rest.slice(m[0].length); }
    const dm = rest.match(/\s+(?:DEFAULT|=)\s+([\s\S]*)$/i);
    if (dm) { def = dm[1].trim(); rest = rest.slice(0, dm.index); }
    const om = rest.match(/\s+(OUTPUT|OUT)$/i);
    if (om) { mode = 'OUT'; rest = rest.slice(0, om.index); }
    let name = '', type = rest.trim();
    const w = type.split(/\s+/);
    if (engineId === 'mssql' || type[0] === '@') {
      if (w[0][0] === '@') { name = w[0]; type = w.slice(1).join(' '); }
    } else if (w.length >= 2 && !QH_TPL_TYPE_WORDS.has(w[0].toLowerCase())) {
      name = w[0]; type = w.slice(1).join(' ');
    }
    out.push({ name: name || 'arg' + (i + 1), named: !!name, type, def, out: mode === 'OUT' || mode === 'INOUT' });
  });
  return out;
}

function qhStatementTemplate(sql, caret, schema, engineId, live) {
  const eng = String(engineId || 'postgres');
  if (qhEngineIdReadOnly(eng)) return null;
  const text = String(sql || '');
  const c = Math.max(0, Math.min(text.length, Number(caret) || 0));
  const all = qhSqlTokens(text, true);
  for (const t of all) {                                    // not inside a string, a quoted name or a comment
    if (t.s >= c) break;
    if (t.k !== 'cmt' && t.k !== 'str' && t.k !== 'qid') continue;
    if (c < t.e || (c === t.e && qhClTokOpen(t))) return null;
  }
  const toks = all.filter(t => t.k !== 'cmt' || t.s >= c);
  // The statement head: every token before the caret since the last `;`.
  let a = 0;
  for (let i = 0; i < toks.length && toks[i].s < c; i++) if (toks[i].v === ';') a = i + 1;
  const H = [];
  let i = a;
  for (; i < toks.length && toks[i].e <= c; i++) H.push(toks[i]);
  if (i < toks.length && toks[i].s < c) return null;        // the caret is inside a token
  if (!H.length || H.some(t => t.k === 'cmt')) return null;
  let kind = null, n = 0;
  const u0 = H[0].u, u1 = H[1] && H[1].u;
  if (u0 === 'INSERT' && u1 === 'INTO') { kind = 'insert'; n = 2; }
  else if (u0 === 'INSERT' && eng === 'mssql') { kind = 'insert'; n = 1; }
  else if (u0 === 'UPDATE') { kind = 'update'; n = 1; }
  else if ((u0 === 'EXEC' || u0 === 'EXECUTE') && eng === 'mssql') { kind = 'exec'; n = 1; }
  else if (u0 === 'CALL' && eng === 'postgres') { kind = 'call'; n = 1; }
  if (!kind) return null;
  const parts = [];
  for (let j = n; j < H.length; j += 2) {
    if (!qhFromIsName(H[j]) || (H[j].k === 'word' && QH_KW_SET.has(H[j].u))) return null;
    parts.push(H[j]);
    if (j + 1 < H.length && H[j + 1].v !== '.') return null;
    if (j + 1 === H.length - 1) return null;                // ends on a dot
  }
  if (!parts.length) return null;
  const nameEnd = H[H.length - 1].e;
  if (text.slice(nameEnd, c).trim()) return null;
  // What follows: nothing, or the empty part of a snippet, up to a `;` or a
  // blank line.
  const R = [];
  let end = c;
  for (let k = i; k < toks.length; k++) {
    const t = toks[k];
    if (t.v === ';' || /\n[ \t]*\n/.test(text.slice(end, t.s))) break;
    R.push(t.k === 'word' ? t.u : t.v);
    end = t.e;
  }
  const sk = QH_TPL_SKELETON[kind === 'call' ? 'exec' : kind];
  if (!sk.some(x => x.length === R.length && x.every((v, j) => v === R[j]))) return null;

  const sch = schema || {};
  const quote = (x) => (typeof qhQuoteIdentFor === 'function' ? qhQuoteIdentFor(x, eng) : qhQuoteIdent(x));
  const lineStart = text.lastIndexOf('\n', H[0].s - 1) + 1;
  const ind = text.slice(lineStart, H[0].s).match(/^[ \t]*/)[0];
  const stops = [];
  let out = '';
  const field = (nm) => { const f = '<' + nm + '>'; stops.push([out.length, out.length + f.length]); out += f; };
  // Lines of `head` + field + `tail`, with the comments in one column. `w0`
  // is a column that an earlier block of the same statement uses.
  const width = (list, lead, sep) => Math.max(...list.map((r, j) => (j ? lead.next : lead.first).length + r.head.length + r.name.length + 2 + r.tail.length + sep.length));
  const rows = (list, lead, sep, w0) => {
    const w = Math.max(w0 || 0, width(list, lead, sep));
    list.forEach((r, j) => {
      const last = j === list.length - 1;
      out += '\n' + ind + (j === 0 ? lead.first : lead.next) + r.head;
      field(r.name);
      const tail = r.tail + (last ? '' : sep);
      out += tail;
      const lw = (j === 0 ? lead.first : lead.next).length;
      if (r.note) out += ' '.repeat(w - (lw + r.head.length + r.name.length + 2 + tail.length) + 2) + '-- ' + r.note;
    });
  };
  const note = (col) => [((col.type ? String(col.type) : '') + (col.nullable === false ? ' NOT NULL' : '')).trim(), col.pk ? 'PK' : ''].filter(Boolean).join(', ');

  if (kind === 'insert' || kind === 'update') {
    const res = qhRelCols(toks.slice(a, i), [], sch, live, eng);
    const cols = res.of({ parts, alias: null }, false);
    if (!Array.isArray(cols) || !cols.length) return null;
    if (kind === 'insert') {
      out += ' (' + cols.map(x => quote(String(x.name))).join(', ') + ')\n' + ind + 'VALUES (';
      rows(cols.map(x => ({ head: '', name: String(x.name), tail: '', note: note(x) })), { first: '  ', next: '  ' }, ',');
      out += '\n' + ind + ')';
    } else {
      const keys = cols.filter(x => x.pk);
      const set = keys.length && keys.length < cols.length ? cols.filter(x => !x.pk) : cols;
      const where = set === cols ? [] : keys;
      const row = (x) => ({ head: quote(String(x.name)) + ' = ', name: String(x.name), tail: '', note: note(x) });
      const sl = { first: 'SET ', next: '    ' }, wl = { first: 'WHERE ', next: '  AND ' };
      const w = where.length ? Math.max(width(set.map(row), sl, ','), width(where.map(row), wl, '')) : 0;
      rows(set.map(row), sl, ',', w);
      if (where.length) rows(where.map(row), wl, '', w);
      else { out += '\n' + ind + 'WHERE '; field('condition'); }
    }
    return { kind, start: nameEnd, end: Math.max(c, end), text: out, stops, caretAt: out.length, count: cols.length };
  }
  // EXEC / CALL: the routine's catalog signature. One routine, or none.
  const tail = qhFromUnquote(parts[parts.length - 1]).toLowerCase();
  const qual = parts.length > 1 ? qhFromUnquote(parts[parts.length - 2]).toLowerCase() : null;
  const sigs = ((sch.functionSigs || {})[tail] || []).filter(x => x.k === 'procedure' && (!qual || String(x.s || '').toLowerCase() === qual));
  if (sigs.length !== 1) return null;
  const ps = qhTplParams(sigs[0].args, eng);
  if (!ps.length) return null;
  const pnote = (x) => [(x.type + (x.def ? ' DEFAULT ' + x.def : '')).trim(), x.out ? 'OUT' : ''].filter(Boolean).join(', ');
  if (kind === 'exec') {
    rows(ps.map(x => ({ head: x.named ? x.name + ' = ' : '', name: x.name.replace(/^@/, ''), tail: x.out ? ' OUTPUT' : '', note: pnote(x) })),
      { first: '  ', next: '  ' }, ',');
  } else {
    out += '(';
    rows(ps.map(x => ({ head: '', name: x.name, tail: '', note: pnote(x) })), { first: '  ', next: '  ' }, ',');
    out += '\n' + ind + ')';
  }
  return { kind, start: nameEnd, end: Math.max(c, end), text: out, stops, caretAt: out.length, count: ps.length };
}

// The row of the typing list for a template (Ctrl+Space).
function qhTemplateItem(tp) {
  const what = tp.kind === 'insert' || tp.kind === 'update' ? (tp.count === 1 ? ' column' : ' columns') : (tp.count === 1 ? ' parameter' : ' parameters');
  return { type: 'template', key: 'template', label: 'Template: ' + tp.kind.toUpperCase() + ', ' + tp.count + what,
           text: tp.text, start: tp.start, replaceEnd: tp.end, stops: tp.stops, caretAt: tp.caretAt };
}

// ---------------------------------------------------------------------------
// JOIN suggestions (2026-11-10)
//
// After JOIN, the list starts with whole JOIN clauses: `orders o ON o.user_id =
// u.id`. After ON, it starts with the conditions between the new table and the
// tables before it: `u.id = o.user_id`. A condition comes from a foreign key in
// either direction. Only when a pair of tables has no foreign key, it comes
// from columns with the same name and the same type. A column named `id` alone
// never matches, because every table has one.
//
// The answer is exact or there is none. A relation that the catalog does not
// list, a subquery, a CTE name, NATURAL and USING joins, CROSS JOIN, a string
// and a comment give null. The caller then keeps the normal list.
// ---------------------------------------------------------------------------
const QH_JOIN_MAX = 8;
const QH_JOIN_ON_MAX = 6;

// The table and the column that a catalog `fk` value points at. The value is
// 'table.col' or 'schema.table.col'. The schema is not compared.
function qhJoinFkTarget(fk) {
  const p = String(fk || '').split('.');
  if (p.length < 2 || !p[p.length - 1] || !p[p.length - 2]) return null;
  return { table: p[p.length - 2].toLowerCase(), col: p[p.length - 1] };
}

// The join conditions between table a and table b, as { left, right, fk }.
// `left` is a column of a and `right` a column of b. Foreign keys in either
// direction come first. Same name and same type only when there is no foreign
// key. A table with itself joins only on a foreign key.
function qhJoinPairs(aName, aCols, bName, bCols) {
  const an = String(aName).toLowerCase(), bn = String(bName).toLowerCase();
  const find = (cols, n) => cols.find(c => c && String(c.name).toLowerCase() === String(n).toLowerCase());
  const out = [];
  aCols.forEach(c => {
    const t = c ? qhJoinFkTarget(c.fk) : null;
    const o = (t && t.table === bn) ? find(bCols, t.col) : null;
    if (o) out.push({ left: c.name, right: o.name, fk: true });
  });
  bCols.forEach(c => {
    const t = c ? qhJoinFkTarget(c.fk) : null;
    const o = (t && t.table === an) ? find(aCols, t.col) : null;
    if (o) out.push({ left: o.name, right: c.name, fk: true });
  });
  if (out.length || an === bn) return out;
  aCols.forEach(c => {
    if (!c || !c.type || String(c.name).toLowerCase() === 'id') return;
    const o = find(bCols, c.name);
    if (o && o.type && String(o.type).toLowerCase() === String(c.type).toLowerCase()) out.push({ left: c.name, right: o.name, fk: false });
  });
  return out;
}

// The statement around the caret, for the two JOIN readers. `p` is the last
// token before the word that is being typed. Null inside a string, a quoted
// name or a comment, and after a dot.
function qhJoinScan(sql, caret) {
  const text = String(sql || '');
  const c = Math.max(0, Math.min(text.length, Number(caret) || 0));
  const pm = text.slice(0, c).match(/[A-Za-z_][A-Za-z0-9_]*$/);
  const start = pm ? c - pm[0].length : c;
  if (text[start - 1] === '.') return null;
  const all = qhSqlTokens(text, true);
  for (const t of all) {
    if (t.s >= c) break;
    if (t.k !== 'cmt' && t.k !== 'str' && t.k !== 'qid') continue;
    if (c < t.e || (c === t.e && qhClTokOpen(t))) return null;
  }
  const toks = all.filter(t => t.k !== 'cmt' && !(t.e > start && t.s < c));
  let a = 0, b = toks.length;
  for (let i = 0; i < toks.length; i++) {
    if (toks[i].v !== ';') continue;
    if (toks[i].e <= start) a = i + 1; else { b = i; break; }
  }
  const S = toks.slice(a, b);
  const depth = [];
  let d = 0;
  for (const t of S) { if (t.v === ')') d--; depth.push(d); if (t.v === '(') d++; }
  let p = -1;
  for (let i = 0; i < S.length && S[i].s < start; i++) p = i;
  if (p < 0) return null;
  return { caret: c, start, typed: pm ? pm[0] : '', S, depth, p };
}

// The FROM list that ends before token `endAt`, with the catalog columns of
// each relation: [{ name, label, cols }]. `label` is the alias, else the name
// as written. Null when any relation is not a catalog table, or the list has
// NATURAL or USING.
function qhJoinFrom(sc, endAt, schema) {
  const { S, depth, p } = sc;
  const D = depth[p];
  let f = -1;
  for (let i = p; i >= 0; i--) {
    if (depth[i] < D) break;
    if (depth[i] > D || S[i].k !== 'word') continue;
    if (S[i].u === 'FROM') { f = i; break; }
    if (QH_FROM_END.has(S[i].u) || S[i].u === 'SELECT') break;
  }
  if (f < 0) return null;
  for (let i = f + 1; i < endAt; i++) {
    if (depth[i] === D && S[i].k === 'word' && (S[i].u === 'NATURAL' || S[i].u === 'USING')) return null;
  }
  const refs = qhReadFromList(S.slice(0, endAt), depth.slice(0, endAt), f, D, true);
  if (!refs || !refs.length) return null;
  // CTE names: `name AS (` or `name (cols) AS (`. A CTE can share a table's name.
  const ctes = new Set();
  for (let i = 0; i < S.length; i++) {
    if (!qhFromIsName(S[i])) continue;
    let j = i + 1;
    if (S[j] && S[j].v === '(') { while (j < S.length && !(S[j].v === ')' && depth[j] === depth[i])) j++; j++; }
    if (S[j] && S[j].u === 'AS' && S[j + 1] && S[j + 1].v === '(') ctes.add(qhFromUnquote(S[i]).toLowerCase());
  }
  const sch = schema || {};
  const out = [];
  for (const r of refs) {
    if (r.parts.length === 1 && ctes.has(qhFromUnquote(r.parts[0]).toLowerCase())) return null;
    const cols = qhRefLookup(r, sch, sch.tableMetaQ, sch.tableMeta);
    if (!Array.isArray(cols) || !cols.length) return null;
    const last = r.parts[r.parts.length - 1];
    out.push({ name: qhFromUnquote(last), label: (r.alias || last).v, cols });
  }
  return out;
}

// A token after the caret that already fills the slot: a name, a subquery, or
// (after JOIN) the ON / USING / AS of a table that is already there.
function qhJoinSlotTaken(t, afterJoin) {
  if (!t) return false;
  if (t.v === '(') return true;
  if (t.k === 'word' && afterJoin && ['ON', 'USING', 'AS'].includes(t.u)) return true;
  if (t.k === 'word' && QH_NOT_NAME.has(t.u)) return false;
  return t.v !== ')';
}

// ---------------------------------------------------------------------------
// Table aliases (2026-11-11, Devart A19)
//
// A table picked from the typing list after FROM or JOIN gets an alias:
// `order_items` gives `oi`, `JobCandidate` gives `jc`, `users` gives `u`. A
// rule `mask = alias` names the alias of the tables that match the mask: `*`
// is any run of characters, `?` is one, and case does not count. A mask with a
// dot matches the schema-qualified name. The first rule that matches wins. A
// number goes on when the statement already uses the alias (`u2`). The switch
// and the rules stay in this browser (QH_ALIAS_KEY). The JOIN suggestions use
// the same aliases.
// ---------------------------------------------------------------------------
const QH_ALIAS_KEY = 'qh.alias.v1';
const QH_ALIAS_RULES_MAX = 2000;

// { auto, rules } with the defaults: auto on, no rules.
function qhAliasOptsClean(raw) {
  const r = raw && typeof raw === 'object' ? raw : {};
  return { auto: r.auto !== false, rules: typeof r.rules === 'string' ? r.rules.slice(0, QH_ALIAS_RULES_MAX) : '' };
}
function qhAliasOptsLoad() {
  try { return qhAliasOptsClean(JSON.parse(localStorage.getItem(QH_ALIAS_KEY) || 'null')); } catch (e) { return qhAliasOptsClean(null); }
}
function qhAliasOptsSave(opts) {
  try { localStorage.setItem(QH_ALIAS_KEY, JSON.stringify(qhAliasOptsClean(opts))); } catch (e) {}
}

// The rules in `text`, one `mask = alias` per line, as [{ mask, alias, re }].
// The reader skips a line that is not a rule, and `bad` counts those lines.
function qhAliasRules(text) {
  const rules = [];
  let bad = 0;
  String(text || '').split('\n').forEach(line => {
    const l = line.trim();
    if (!l || l.startsWith('#') || l.startsWith('--')) return;
    const m = /^([A-Za-z0-9_.*?$]+)\s*=\s*([A-Za-z_][A-Za-z0-9_]*)$/.exec(l);
    if (!m) { bad++; return; }
    const src = m[1].replace(/[.$]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.');
    rules.push({ mask: m[1], alias: m[2], re: new RegExp('^' + src + '$', 'i') });
  });
  rules.bad = bad;
  return rules;
}

// The alias before any number: the first rule that matches, else the
// initials of the words of the name, else its first letter.
function qhAliasBase(table, rules, qualified) {
  const name = String(table || '');
  for (const r of (rules || [])) {
    const subject = r.mask.indexOf('.') >= 0 ? String(qualified || name) : name;
    if (r.re.test(subject)) return r.alias;
  }
  let base = (qhNameInitials(name) || name.replace(/[^A-Za-z0-9_]/g, '').slice(0, 1)).toLowerCase().replace(/[^a-z0-9_]/g, '');
  if (!/^[a-z_]/.test(base)) base = 't' + base;
  return base;
}

// The alias for a new table: qhAliasBase, with a number when the statement
// already uses that word, or the word is a keyword (`u2`).
function qhJoinAlias(table, used, engineId, rules, qualified) {
  const base = qhAliasBase(table, rules, qualified);
  const bad = (x) => used.has(x.toLowerCase()) || QH_KW_SET.has(x.toUpperCase()) || QH_NOT_NAME.has(x.toUpperCase())
    || (typeof qhIsReservedWord === 'function' && qhIsReservedWord(x, engineId || 'postgres'));
  if (!bad(base)) return base;
  for (let n = 2; ; n++) if (!bad(base + n)) return base + n;
}

function qhJoinQuote(n, engineId) {
  return typeof qhQuoteIdentFor === 'function' ? qhQuoteIdentFor(n, engineId) : qhQuoteIdent(n);
}

// The alias to put after a table picked from the typing list, or ''. The pick
// replaces value[start, end) and `table` is the bare name. The slot must follow
// FROM or JOIN. A DELETE gets none: SQL Server does not read `DELETE FROM t a`.
// A slot that already has an alias, AS or a subquery after it gets none.
function qhAutoAlias(value, start, end, table, engineId, rules, qualified) {
  const v = String(value == null ? '' : value);
  const tk = qhSuggestTokens(v.slice(0, start));
  if (!tk || !tk.words.length) return '';
  const W = tk.words;
  const last = W[W.length - 1];
  if (last !== 'FROM' && last !== 'JOIN') return '';
  if (W[0] === 'DELETE') return '';
  // The next token: a name, AS, a string or a number is an alias that is there
  // already. ON or USING after JOIN already names the table. A keyword such as
  // WHERE, a `,`, a `;` or the end leaves room for the alias.
  const n = qhSqlTokens(v.slice(end)).find(Boolean);
  if (n && (n.v === '.' || n.v === '(' || n.k === 'qid' || n.k === 'str' || n.k === 'num'
      || (n.k === 'word' && (['AS', 'ON', 'USING'].includes(n.u) || !(QH_NOT_NAME.has(n.u) || QH_KW_SET.has(n.u)))))) return '';
  const st = typeof qhStatementAt === 'function' ? qhStatementAt(v, start) : null;
  const a = st ? Math.min(st.start, start) : 0, b = st ? Math.max(st.end, end) : v.length;
  const used = new Set();
  qhSqlTokens(v.slice(a, start) + ' ' + v.slice(end, b)).forEach(t => {
    if (t.k === 'word' || t.k === 'qid') used.add(qhFromUnquote(t).toLowerCase());
  });
  return qhJoinAlias(table, used, engineId, rules, qualified);
}

// After JOIN: whole JOIN clauses for the catalog tables, at most QH_JOIN_MAX.
// The table name is inserted as a table pick inserts it (`schema.qualify`).
// `rules` are the alias rules (qhAliasRules). Returns { items, start, end } or null.
function qhJoinSuggestions(sql, caret, schema, engineId, rules) {
  const sc = qhJoinScan(sql, caret);
  if (!sc) return null;
  const { S, p } = sc;
  if (S[p].k !== 'word' || S[p].u !== 'JOIN') return null;
  if (S[p - 1] && S[p - 1].u === 'CROSS') return null;
  if (qhJoinSlotTaken(S[p + 1], true)) return null;
  const from = qhJoinFrom(sc, p + 1, schema);
  if (!from) return null;
  const sch = schema || {};
  const quote = (n) => qhJoinQuote(n, engineId);
  const used = new Set();
  S.forEach(t => { if (t.k === 'word' || t.k === 'qid') used.add(qhFromUnquote(t).toLowerCase()); });
  const inFrom = new Set(from.map(r => r.name.toLowerCase()));
  const lc = sc.typed.toLowerCase();
  const fkItems = [], nameItems = [];
  (sch.tables || []).forEach(n => {
    const nl = String(n).toLowerCase();
    if (lc && !nl.startsWith(lc)) return;
    if (sch.qualify && sch.qualify[n] === null) return;     // two schemas have it
    const cols = qhLookupCi(sch.tableMeta, n, true);
    if (!Array.isArray(cols) || !cols.length) return;
    const q = sch.qualify ? sch.qualify[n] : null;
    const alias = qhJoinAlias(n, used, engineId, rules, q);
    const name = q ? quote(q.slice(0, q.lastIndexOf('.'))) + '.' + quote(q.slice(q.lastIndexOf('.') + 1)) : quote(n);
    from.forEach(r => {
      // A table already in FROM comes back only for its own foreign key.
      if (inFrom.has(nl) && r.name.toLowerCase() !== nl) return;
      qhJoinPairs(n, cols, r.name, r.cols).forEach(pr => {
        const text = name + ' ' + alias + ' ON ' + alias + '.' + quote(pr.left) + ' = ' + r.label + '.' + quote(pr.right);
        (pr.fk ? fkItems : nameItems).push({ text, label: text, type: 'join', fk: pr.fk, rank: pr.fk ? 0 : 1,
                                              typeRank: -1, start: sc.start });
      });
    });
  });
  const seen = new Set();
  const items = fkItems.concat(nameItems).filter(it => !seen.has(it.text) && seen.add(it.text)).slice(0, QH_JOIN_MAX);
  return items.length ? { items, start: sc.start, end: sc.caret } : null;
}

// After ON: the conditions between the new table and each table before it in
// FROM, at most QH_JOIN_ON_MAX. A typed word filters them. Returns
// { items, start, end } or null.
function qhJoinOnSuggestions(sql, caret, schema, engineId) {
  const sc = qhJoinScan(sql, caret);
  if (!sc) return null;
  const { S, p } = sc;
  if (S[p].k !== 'word' || S[p].u !== 'ON') return null;
  if (qhJoinSlotTaken(S[p + 1], false)) return null;
  const from = qhJoinFrom(sc, p, schema);
  if (!from || from.length < 2) return null;
  const quote = (n) => qhJoinQuote(n, engineId);
  const nw = from[from.length - 1];
  const lc = sc.typed.toLowerCase();
  const fkItems = [], nameItems = [];
  from.slice(0, -1).forEach(r => {
    qhJoinPairs(nw.name, nw.cols, r.name, r.cols).forEach(pr => {
      const text = nw.label + '.' + quote(pr.left) + ' = ' + r.label + '.' + quote(pr.right);
      if (lc && !text.toLowerCase().startsWith(lc)) return;
      (pr.fk ? fkItems : nameItems).push({ text, label: text, type: 'join', fk: pr.fk, rank: pr.fk ? 0 : 1,
                                            typeRank: -1, start: sc.start });
    });
  });
  const seen = new Set();
  const items = fkItems.concat(nameItems).filter(it => !seen.has(it.text) && seen.add(it.text)).slice(0, QH_JOIN_ON_MAX);
  return items.length ? { items, start: sc.start, end: sc.caret } : null;
}

// ---------------------------------------------------------------------------
// Quick info on hover (2026-11-10)
//
// What the catalog knows about the table or column name at `offset`. The answer
// is exact or there is none. A table resolves like a FROM entry: qualified when
// written qualified, a bare name only when one schema has it. A column resolves
// through the FROM list of its own query block. `alias.col` and `table.col` go
// through the qualifier, and an outer block can own the qualifier. A bare
// column needs exactly one relation of its block that has it. Strings,
// comments, numbers, keywords, function names and column aliases give null.
//
// Returns { kind: 'table', start, end, name, approxRows, columns, pk, indexes,
// fks }, { kind: 'column', start, end, table, name, type, nullable, pk, pii,
// fk, indexes } or null. `start`/`end` is the range of the word itself.
// ---------------------------------------------------------------------------
const QH_HOVER_OWNER = new Set(['SELECT', 'UPDATE', 'DELETE']);
const QH_HOVER_TARGET = new Set(['UPDATE', 'INTO', 'TABLE']);

// A token that can be a table, schema, alias or column name. Keywords cannot.
function qhHoverIsName(t) {
  return !!t && (t.k === 'qid' || (t.k === 'word' && !QH_KW_SET.has(t.u) && !QH_NOT_NAME.has(t.u)));
}

// The catalog entry for a table written as `parts` (name tokens), or null.
function qhHoverTable(parts, sch) {
  const r = { parts, alias: null };
  const meta = qhRefLookup(r, sch, sch.tableMetaQ, sch.tableMeta);
  const info = qhRefLookup(r, sch, sch.tableInfoQ, sch.tableInfo);
  if (!Array.isArray(meta) || (!meta.length && !info)) return null;
  const names = parts.map(qhFromUnquote);
  const n = names[names.length - 1];
  let name;
  if (names.length >= 2) name = names[names.length - 2] + '.' + n;
  else {
    const q = qhLookupCi(sch.qualify, n, parts[0].k === 'qid');
    name = typeof q === 'string' ? q : (info && info.schema ? info.schema + '.' + n : n);
  }
  return { name, meta, info: info || null };
}

// The one column of `meta` that the name token names, or null.
function qhHoverColumn(meta, tok) {
  const want = qhFromUnquote(tok), exact = tok.k === 'qid';
  const hits = (meta || []).filter(c => c && (exact ? String(c.name) === want
    : String(c.name).toLowerCase() === want.toLowerCase()));
  return hits.length === 1 ? hits[0] : null;
}

function qhHoverTableCard(tok, hit) {
  const info = hit.info || {};
  const rows = info.approxRows == null ? NaN : Number(info.approxRows);
  const idx = Array.isArray(info.indexes) ? info.indexes : [];
  return {
    kind: 'table', start: tok.s, end: tok.e, name: hit.name,
    approxRows: Number.isFinite(rows) && rows >= 0 ? rows : null,
    columns: hit.meta.length,
    pk: hit.meta.filter(c => c && c.pk).map(c => String(c.name)),
    indexes: idx.filter(Boolean).map(ix => ({ name: String(ix.name || ''),
      cols: Array.isArray(ix.cols) ? ix.cols.map(String) : [], unique: !!ix.unique })),
    fks: hit.meta.filter(c => c && c.fk).map(c => ({ col: String(c.name), ref: String(c.fk) })),
  };
}

function qhHoverColumnCard(tok, hit, col) {
  const lc = String(col.name).toLowerCase();
  const idx = hit.info && Array.isArray(hit.info.indexes) ? hit.info.indexes : [];
  return {
    kind: 'column', start: tok.s, end: tok.e, table: hit.name, name: String(col.name),
    type: col.type ? String(col.type) : '',
    nullable: typeof col.nullable === 'boolean' ? col.nullable : null,
    pk: !!col.pk, pii: !!col.pii, fk: col.fk ? String(col.fk) : null,
    indexes: idx.filter(ix => ix && Array.isArray(ix.cols) && ix.cols.some(c => String(c).toLowerCase() === lc))
      .map(ix => String(ix.name)),
  };
}

function qhHoverInfo(sql, offset, schema, engineId) {
  void engineId;                                            // the same rules for every engine today
  const text = String(sql == null ? '' : sql);
  const at = Number(offset);
  if (!text || !Number.isFinite(at)) return null;
  const toks = qhSqlTokens(text);
  const k = toks.findIndex(t => t.s <= at && at < t.e);
  if (k < 0 || !qhHoverIsName(toks[k])) return null;       // a blank, a comment, a string, a keyword
  const tok = toks[k];
  const sch = schema || {};
  // The statement around the word, and the paren depth of each token.
  let a = k, b = k;
  while (a > 0 && toks[a - 1].v !== ';') a--;
  while (b < toks.length && toks[b].v !== ';') b++;
  const S = toks.slice(a, b), h = k - a;
  const depth = [];
  let d = 0;
  for (const t of S) { if (t.v === ')') d--; depth.push(d); if (t.v === '(') d++; }

  // The target of UPDATE, INTO and TABLE: a name, then maybe an alias.
  const readTarget = (j) => {
    if (S[j] && S[j].u === 'ONLY') j++;
    if (!qhHoverIsName(S[j])) return null;
    const parts = [S[j]];
    while (S[j + 1] && S[j + 1].v === '.' && qhHoverIsName(S[j + 2])) { parts.push(S[j + 2]); j += 2; }
    let alias = null;
    if (S[j + 1] && S[j + 1].u === 'AS' && qhHoverIsName(S[j + 2])) alias = S[j + 2];
    else if (qhHoverIsName(S[j + 1])) alias = S[j + 1];
    return { parts, alias };
  };
  // The relations of the query block that the keyword at i owns.
  const blockRefs = (i) => {
    const refs = (qhReadFromList(S, depth, i + 1, depth[i], true) || []).slice();
    if (S[i].u === 'UPDATE') { const t = readTarget(i + 1); if (t) refs.unshift(t); }
    return refs;
  };

  // 1. A table position: a FROM or JOIN entry, or the target of UPDATE/INTO/TABLE.
  const placed = [];
  S.forEach((t, i) => {
    if (t.k !== 'word') return;
    if (QH_HOVER_OWNER.has(t.u)) placed.push(...blockRefs(i));
    if (QH_HOVER_TARGET.has(t.u)) { const r = readTarget(i + 1); if (r) placed.push(r); }
  });
  const inRef = placed.find(r => r.parts.includes(tok) || r.alias === tok);
  if (inRef) {
    if (inRef.alias !== tok && inRef.parts[inRef.parts.length - 1] !== tok) return null;  // a schema part
    const hit = qhHoverTable(inRef.parts, sch);
    return hit ? qhHoverTableCard(tok, hit) : null;
  }

  // The dotted name the word is part of: P[0..m-1], the word at index w.
  let lo = h, hi = h;
  while (lo >= 2 && S[lo - 1].v === '.' && qhHoverIsName(S[lo - 2])) lo -= 2;
  while (hi + 2 < S.length && S[hi + 1].v === '.' && qhHoverIsName(S[hi + 2])) hi += 2;
  const P = [];
  for (let i = lo; i <= hi; i += 2) P.push(S[i]);
  const w = (h - lo) / 2, m = P.length;
  if (m > 3) return null;
  if (S[hi + 1] && S[hi + 1].v === '(') return null;       // a function call
  if (m === 1 && S[h - 1] && S[h - 1].u === 'AS') return null;  // a column alias

  // The query blocks around the word, the innermost first.
  const blocks = [];
  let D = depth[h];
  for (let i = h; i >= 0; i--) {
    if (depth[i] < D) D = depth[i];
    if (depth[i] > D || S[i].k !== 'word' || !QH_HOVER_OWNER.has(S[i].u)) continue;
    blocks.push(blockRefs(i));
    D = depth[i] - 1;
  }
  // The FROM entry that a qualifier names, searched from the inner block out.
  const byQualifier = (Q) => {
    const q = Q.map(qhFromUnquote).map(s => s.toLowerCase());
    for (const refs of blocks) {
      const hits = refs.filter((r) => {
        if (q.length === 1) return qhFromUnquote(r.alias || r.parts[r.parts.length - 1]).toLowerCase() === q[0];
        if (r.alias || r.parts.length < 2) return false;
        const p = r.parts.slice(-2).map(qhFromUnquote).map(s => s.toLowerCase());
        return p[0] === q[0] && p[1] === q[1];
      });
      if (hits.length === 1) return hits[0];
      if (hits.length > 1) return null;
    }
    return null;
  };

  // 2. The last part of a dotted name: a column, or a schema-qualified table.
  if (m >= 2 && w === m - 1) {
    const r = byQualifier(P.slice(0, m - 1));
    if (r) {
      const hit = qhHoverTable(r.parts, sch);
      const col = hit && qhHoverColumn(hit.meta, tok);
      return col ? qhHoverColumnCard(tok, hit, col) : null;
    }
    if (m === 2) { const hit = qhHoverTable(P, sch); if (hit) return qhHoverTableCard(tok, hit); }
    return null;
  }
  // 3. A qualifier: the table of `x.col` or of `schema.table.col`.
  if (m >= 2) {
    if (w !== m - 2) return null;                           // the schema of schema.table.col
    if (m === 3) { const hit = qhHoverTable(P.slice(0, 2), sch); return hit ? qhHoverTableCard(tok, hit) : null; }
    const r = byQualifier([tok]);
    const hit = r && qhHoverTable(r.parts, sch);
    return hit ? qhHoverTableCard(tok, hit) : null;
  }
  // 4. A bare word: a column of exactly one relation of its block.
  if (blocks.length && blocks[0].length) {
    const hits = blocks[0].map(r => qhHoverTable(r.parts, sch));
    const owners = hits.filter(x => x && qhHoverColumn(x.meta, tok));
    const unknown = hits.some(x => !x);
    if (owners.length === 1 && !unknown) return qhHoverColumnCard(tok, owners[0], qhHoverColumn(owners[0].meta, tok));
    if (owners.length) return null;                         // two relations have it, or one is unknown
  }
  // 5. A bare word the catalog knows as a table.
  const hit = qhHoverTable([tok], sch);
  return hit ? qhHoverTableCard(tok, hit) : null;
}

// The plain-text lines of the hover card, at most about eight.
function qhHoverLines(info) {
  if (!info) return [];
  const num = (n) => String(Math.round(Number(n))).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  if (info.kind === 'table') {
    const out = [info.name];
    if (info.approxRows != null) out.push('~' + num(info.approxRows) + ' rows');
    out.push(info.columns + (info.columns === 1 ? ' column' : ' columns'));
    if (info.pk.length) out.push('PK: ' + info.pk.join(', '));
    info.indexes.slice(0, 4).forEach(ix => out.push(ix.name + ' (' + ix.cols.join(', ') + ')' + (ix.unique ? ' unique' : '')));
    info.fks.slice(0, 4).forEach(f => out.push('fk ' + f.col + ' → ' + f.ref));
    return out;
  }
  const out = [info.table + '.' + info.name];
  if (info.type) out.push(info.type);
  if (info.nullable === true) out.push('NULL');
  else if (info.nullable === false) out.push('NOT NULL');
  if (info.pk) out.push('PK');
  if (info.fk) out.push('FK → ' + info.fk);
  if (info.pii) out.push('PII · masked in results');
  if (info.indexes.length) out.push('Indexed: ' + info.indexes.join(', '));
  return out;
}

// ---------------------------------------------------------------------------
// Parameter info (2026-11-11, Devart B2). Inside the parentheses of a call, a
// box above the caret line shows the parameters of the function. The
// parameter at the caret is bold. On SQL Server, `EXEC proc a, b` gets the
// same box. The signatures come from two places:
//   * the catalog: the database's own routines (`functionSigs` of the editor
//     schema, one signature for each name, as the catalog keeps it).
//   * QH_FUNC_SIGS: common built-in functions of each engine. A name can have
//     more than one signature (overloads). ClickHouse and Athena have no
//     routine catalog, so for them this list is the only source.
//
// qhCallAt(sql, caret, engineId) -> { name, qual, open, nameStart, arg, exec } or null
//   `open` is the offset of the call's `(` (of the name for EXEC), `arg` the
//   index of the parameter at the caret (the commas before it).
// qhSigParams(args)              -> the parameters of an argument list text
// qhSignaturesFor(call, schema, engineId) -> [{ name, params, ret, kind, src }]
// qhSigPick(list, arg)           -> the first signature that has parameter `arg`
// qhSigActive(sig, arg)          -> the index of the bold parameter, or -1
// ---------------------------------------------------------------------------

// One line per signature: `name(parameters) → result`. `...` as the last
// parameter means "more of the same". Names compare without case.
const QH_FUNC_SIGS = {
  postgres: [
    'coalesce(value1, value2, ...) → first non-null value',
    'nullif(value1, value2) → value1 type',
    'greatest(value1, value2, ...) → largest value',
    'least(value1, value2, ...) → smallest value',
    'cast(value AS type) → type',
    'length(text) → integer',
    'lower(text) → text',
    'upper(text) → text',
    'trim([leading | trailing | both] [characters FROM] text) → text',
    'substring(text FROM start FOR count) → text',
    'substring(text, start) → text',
    'substring(text, start, count) → text',
    'position(substring IN text) → integer',
    'strpos(text, substring) → integer',
    'replace(text, from, to) → text',
    'split_part(text, delimiter, n) → text',
    'concat(value1, value2, ...) → text',
    'concat_ws(separator, value1, value2, ...) → text',
    'left(text, n) → text',
    'right(text, n) → text',
    'lpad(text, length) → text',
    'lpad(text, length, fill) → text',
    'rpad(text, length) → text',
    'rpad(text, length, fill) → text',
    'format(formatstr, value1, ...) → text',
    'regexp_replace(text, pattern, replacement) → text',
    'regexp_replace(text, pattern, replacement, flags) → text',
    'regexp_match(text, pattern) → text[]',
    'string_agg(value, delimiter) → text',
    'array_agg(value) → array',
    'to_char(value, format) → text',
    'to_date(text, format) → date',
    'to_timestamp(text, format) → timestamp with time zone',
    'to_timestamp(epoch_seconds) → timestamp with time zone',
    'to_number(text, format) → numeric',
    'date_trunc(field, source) → timestamp',
    'date_trunc(field, source, time_zone) → timestamp with time zone',
    'date_part(field, source) → double precision',
    'extract(field FROM source) → numeric',
    'age(timestamp, timestamp) → interval',
    'age(timestamp) → interval',
    'make_date(year, month, day) → date',
    'make_interval(years, months, weeks, days, hours, mins, secs) → interval',
    'now() → timestamp with time zone',
    'generate_series(start, stop) → setof',
    'generate_series(start, stop, step) → setof',
    'count(expression) → bigint',
    'sum(expression) → numeric',
    'avg(expression) → numeric',
    'min(expression) → input type',
    'max(expression) → input type',
    'round(number) → numeric',
    'round(number, decimals) → numeric',
    'trunc(number, decimals) → numeric',
    'abs(number) → number type',
    'ceil(number) → number type',
    'floor(number) → number type',
    'mod(y, x) → y mod x',
    'random() → double precision',
    'row_number() → bigint',
    'rank() → bigint',
    'dense_rank() → bigint',
    'lag(value, offset, default) → value type',
    'lead(value, offset, default) → value type',
    'first_value(value) → value type',
    'last_value(value) → value type',
    'json_build_object(key1, value1, ...) → json',
    'jsonb_build_object(key1, value1, ...) → jsonb',
    'json_agg(value) → json',
    'jsonb_agg(value) → jsonb',
    'jsonb_extract_path_text(from_json, path1, ...) → text',
    'jsonb_set(target, path, new_value, create_missing) → jsonb',
    'jsonb_array_elements(jsonb) → setof jsonb',
    'to_jsonb(value) → jsonb',
    'array_length(array, dimension) → integer',
    'unnest(array) → setof element',
    'md5(text) → text',
    'encode(bytes, format) → text',
    'decode(text, format) → bytea',
    'gen_random_uuid() → uuid',
    'current_setting(setting_name) → text',
    'nextval(sequence) → bigint',
    'currval(sequence) → bigint',
    'setval(sequence, value) → bigint',
    'pg_size_pretty(bytes) → text',
    'pg_total_relation_size(relation) → bigint',
    'pg_relation_size(relation) → bigint',
    'pg_blocking_pids(pid) → integer[]',
    'pg_cancel_backend(pid) → boolean',
    'pg_terminate_backend(pid) → boolean',
    'pg_terminate_backend(pid, timeout) → boolean',
  ],
  mssql: [
    'ISNULL(check_expression, replacement_value) → check_expression type',
    'COALESCE(value1, value2, ...) → first non-null value',
    'NULLIF(expression1, expression2) → expression1 type',
    'IIF(boolean_expression, true_value, false_value) → value type',
    'CAST(expression AS data_type) → data_type',
    'TRY_CAST(expression AS data_type) → data_type or NULL',
    'CONVERT(data_type, expression) → data_type',
    'CONVERT(data_type, expression, style) → data_type',
    'TRY_CONVERT(data_type, expression) → data_type or NULL',
    'TRY_CONVERT(data_type, expression, style) → data_type or NULL',
    'DATEADD(datepart, number, date) → date type',
    'DATEDIFF(datepart, startdate, enddate) → int',
    'DATEDIFF_BIG(datepart, startdate, enddate) → bigint',
    'DATEPART(datepart, date) → int',
    'DATENAME(datepart, date) → nvarchar',
    'DATEFROMPARTS(year, month, day) → date',
    'EOMONTH(start_date) → date',
    'EOMONTH(start_date, month_to_add) → date',
    'GETDATE() → datetime',
    'GETUTCDATE() → datetime',
    'SYSDATETIME() → datetime2',
    'SYSUTCDATETIME() → datetime2',
    'FORMAT(value, format) → nvarchar',
    'FORMAT(value, format, culture) → nvarchar',
    'LEFT(character_expression, integer_expression) → varchar or nvarchar',
    'RIGHT(character_expression, integer_expression) → varchar or nvarchar',
    'SUBSTRING(expression, start, length) → expression type',
    'CHARINDEX(expression_to_find, expression_to_search) → int',
    'CHARINDEX(expression_to_find, expression_to_search, start_location) → int',
    'PATINDEX(pattern, expression) → int',
    'LEN(string_expression) → int',
    'DATALENGTH(expression) → int',
    'REPLACE(string_expression, string_pattern, string_replacement) → varchar or nvarchar',
    'STUFF(character_expression, start, length, replace_with_expression) → character type',
    'REPLICATE(string_expression, integer_expression) → string type',
    'LTRIM(character_expression) → varchar or nvarchar',
    'RTRIM(character_expression) → varchar or nvarchar',
    'TRIM([characters FROM] string) → varchar or nvarchar',
    'UPPER(character_expression) → varchar or nvarchar',
    'LOWER(character_expression) → varchar or nvarchar',
    'CONCAT(value1, value2, ...) → string',
    'CONCAT_WS(separator, value1, value2, ...) → string',
    'STRING_AGG(expression, separator) → nvarchar or varchar',
    'STRING_SPLIT(string, separator) → table',
    'STRING_SPLIT(string, separator, enable_ordinal) → table',
    'ROUND(numeric_expression, length) → numeric type',
    'ROUND(numeric_expression, length, function) → numeric type',
    'ABS(numeric_expression) → numeric type',
    'CEILING(numeric_expression) → numeric type',
    'FLOOR(numeric_expression) → numeric type',
    'COUNT(expression) → int',
    'COUNT_BIG(expression) → bigint',
    'SUM(expression) → numeric type',
    'AVG(expression) → numeric type',
    'MIN(expression) → expression type',
    'MAX(expression) → expression type',
    'ROW_NUMBER() → bigint',
    'RANK() → bigint',
    'DENSE_RANK() → bigint',
    'NTILE(integer_expression) → bigint',
    'LAG(scalar_expression, offset, default) → expression type',
    'LEAD(scalar_expression, offset, default) → expression type',
    'JSON_VALUE(expression, path) → nvarchar(4000)',
    'JSON_QUERY(expression, path) → nvarchar(max)',
    'JSON_MODIFY(expression, path, new_value) → nvarchar(max)',
    'ISJSON(expression) → int',
    'OPENJSON(json_expression) → table',
    'OPENJSON(json_expression, path) → table',
    'OBJECT_ID(object_name) → int',
    'OBJECT_ID(object_name, object_type) → int',
    'OBJECT_NAME(object_id) → sysname',
    'OBJECT_NAME(object_id, database_id) → sysname',
    'DB_NAME() → nvarchar',
    'DB_NAME(database_id) → nvarchar',
    'DB_ID(database_name) → int',
    'SCHEMA_NAME(schema_id) → sysname',
    'NEWID() → uniqueidentifier',
    'SCOPE_IDENTITY() → numeric',
  ],
  clickhouse: [
    'count() → UInt64',
    'count(expr) → UInt64',
    'countIf(cond) → UInt64',
    'sum(x) → number',
    'sumIf(x, cond) → number',
    'avg(x) → Float64',
    'avgIf(x, cond) → Float64',
    'min(x) → x type',
    'max(x) → x type',
    'uniq(x, ...) → UInt64',
    'uniqExact(x, ...) → UInt64',
    'any(x) → x type',
    'anyLast(x) → x type',
    'argMax(arg, val) → arg type',
    'argMin(arg, val) → arg type',
    'groupArray(x) → Array',
    'groupUniqArray(x) → Array',
    'if(cond, then, else) → value type',
    'multiIf(cond1, then1, cond2, then2, ..., else) → value type',
    'ifNull(x, alt) → x type',
    'coalesce(x, ...) → first non-null value',
    'nullIf(x, y) → x type or NULL',
    'isNull(x) → UInt8',
    'toString(x) → String',
    'toInt32(x) → Int32',
    'toInt64(x) → Int64',
    'toUInt64(x) → UInt64',
    'toFloat64(x) → Float64',
    'toDecimal64(value, scale) → Decimal64',
    'toDate(expr) → Date',
    'toDateTime(expr) → DateTime',
    'toDateTime(expr, timezone) → DateTime',
    'toDateTime64(expr, scale) → DateTime64',
    'toStartOfDay(value) → DateTime',
    'toStartOfHour(value) → DateTime',
    'toStartOfWeek(value) → Date',
    'toStartOfMonth(value) → Date',
    'toMonday(value) → Date',
    'toYYYYMM(date) → UInt32',
    'toYYYYMMDD(date) → UInt32',
    'toYear(date) → UInt16',
    'toHour(value) → UInt8',
    'formatDateTime(time, format) → String',
    'formatDateTime(time, format, timezone) → String',
    'dateDiff(unit, startdate, enddate) → Int64',
    'dateDiff(unit, startdate, enddate, timezone) → Int64',
    'date_trunc(unit, value) → DateTime',
    'addDays(date, n) → date type',
    'addHours(date, n) → date type',
    'subtractDays(date, n) → date type',
    'now() → DateTime',
    'today() → Date',
    'yesterday() → Date',
    'length(x) → UInt64',
    'lower(s) → String',
    'upper(s) → String',
    'substring(s, offset) → String',
    'substring(s, offset, length) → String',
    'position(haystack, needle) → UInt64',
    'replaceAll(haystack, pattern, replacement) → String',
    'replaceRegexpAll(haystack, pattern, replacement) → String',
    'splitByChar(separator, s) → Array(String)',
    'splitByString(separator, s) → Array(String)',
    'concat(s1, s2, ...) → String',
    'match(haystack, pattern) → UInt8',
    'extract(haystack, pattern) → String',
    'arrayJoin(arr) → element type',
    'has(arr, elem) → UInt8',
    'arrayMap(func, arr) → Array',
    'arrayFilter(func, arr) → Array',
    'JSONExtractString(json, key) → String',
    'JSONExtractInt(json, key) → Int64',
    'JSONExtractRaw(json, key) → String',
    'JSONHas(json, key) → UInt8',
    'round(x) → x type',
    'round(x, n) → x type',
    'intDiv(a, b) → integer',
    'abs(x) → x type',
    'floor(x) → x type',
    'ceil(x) → x type',
    'toTypeName(x) → String',
  ],
  athena: [
    'count(*) → bigint',
    'count(x) → bigint',
    'count_if(condition) → bigint',
    'sum(x) → x type',
    'avg(x) → double',
    'min(x) → x type',
    'max(x) → x type',
    'approx_distinct(x) → bigint',
    'approx_distinct(x, e) → bigint',
    'approx_percentile(x, percentage) → x type',
    'arbitrary(x) → x type',
    'array_agg(x) → array',
    'max_by(x, y) → x type',
    'min_by(x, y) → x type',
    'coalesce(value1, value2, ...) → first non-null value',
    'nullif(value1, value2) → value1 type',
    'if(condition, true_value) → value type',
    'if(condition, true_value, false_value) → value type',
    'try(expression) → expression type or NULL',
    'cast(value AS type) → type',
    'try_cast(value AS type) → type or NULL',
    'typeof(expr) → varchar',
    'date_trunc(unit, x) → x type',
    'date_add(unit, value, timestamp) → timestamp type',
    'date_diff(unit, timestamp1, timestamp2) → bigint',
    'date_format(timestamp, format) → varchar',
    'date_parse(string, format) → timestamp',
    'format_datetime(timestamp, format) → varchar',
    'parse_datetime(string, format) → timestamp with time zone',
    'from_unixtime(unixtime) → timestamp',
    'from_unixtime(unixtime, zone) → timestamp with time zone',
    'to_unixtime(timestamp) → double',
    'from_iso8601_timestamp(string) → timestamp with time zone',
    'from_iso8601_date(string) → date',
    'date(x) → date',
    'year(x) → bigint',
    'month(x) → bigint',
    'day(x) → bigint',
    'hour(x) → bigint',
    'day_of_week(x) → bigint',
    'now() → timestamp with time zone',
    'length(string) → bigint',
    'lower(string) → varchar',
    'upper(string) → varchar',
    'trim(string) → varchar',
    'ltrim(string) → varchar',
    'rtrim(string) → varchar',
    'substr(string, start) → varchar',
    'substr(string, start, length) → varchar',
    'strpos(string, substring) → bigint',
    'replace(string, search) → varchar',
    'replace(string, search, replace) → varchar',
    'split(string, delimiter) → array(varchar)',
    'split(string, delimiter, limit) → array(varchar)',
    'split_part(string, delimiter, index) → varchar',
    'concat(string1, string2, ...) → varchar',
    'lpad(string, size, padstring) → varchar',
    'rpad(string, size, padstring) → varchar',
    'regexp_like(string, pattern) → boolean',
    'regexp_extract(string, pattern) → varchar',
    'regexp_extract(string, pattern, group) → varchar',
    'regexp_replace(string, pattern) → varchar',
    'regexp_replace(string, pattern, replacement) → varchar',
    'json_extract(json, json_path) → json',
    'json_extract_scalar(json, json_path) → varchar',
    'json_parse(string) → json',
    'json_format(json) → varchar',
    'json_array_length(json) → bigint',
    'element_at(array, index) → element type',
    'element_at(map, key) → value type',
    'cardinality(x) → bigint',
    'contains(array, element) → boolean',
    'array_join(x, delimiter) → varchar',
    'sequence(start, stop) → array',
    'sequence(start, stop, step) → array',
    'transform(array, function) → array',
    'filter(array, function) → array',
    'round(x) → x type',
    'round(x, d) → x type',
    'abs(x) → x type',
    'ceil(x) → x type',
    'floor(x) → x type',
    'mod(n, m) → n type',
    'greatest(value1, value2, ...) → largest value',
    'least(value1, value2, ...) → smallest value',
    'row_number() → bigint',
    'rank() → bigint',
    'dense_rank() → bigint',
    'ntile(n) → bigint',
    'lag(x, offset, default_value) → x type',
    'lead(x, offset, default_value) → x type',
    'first_value(x) → x type',
    'last_value(x) → x type',
  ],
};

// The parameters of an argument list: split at the commas that are not inside
// parentheses, brackets or quotes. An empty text has no parameters.
function qhSigParams(args) {
  const text = String(args == null ? '' : args).trim();
  if (!text) return [];
  const out = [];
  let depth = 0, quote = '', cur = '';
  for (const ch of text) {
    if (quote) { cur += ch; if (ch === quote) quote = ''; continue; }
    if (ch === "'" || ch === '"') { quote = ch; cur += ch; continue; }
    if (ch === '(' || ch === '[') depth++;
    else if (ch === ')' || ch === ']') depth = Math.max(0, depth - 1);
    if (ch === ',' && depth === 0) { out.push(cur.trim()); cur = ''; continue; }
    cur += ch;
  }
  out.push(cur.trim());
  return out;
}

// QH_FUNC_SIGS of one engine, read once: [{ lname, name, params, ret }].
const qhBuiltinSigCache = {};
function qhBuiltinSigs(engineId) {
  const eng = String(engineId || 'postgres');
  if (qhBuiltinSigCache[eng]) return qhBuiltinSigCache[eng];
  const out = (QH_FUNC_SIGS[eng] || []).map((line) => {
    const open = line.indexOf('(');
    const arrow = line.lastIndexOf(' → ');
    const close = line.lastIndexOf(')', arrow < 0 ? line.length : arrow);
    const name = line.slice(0, open);
    return { lname: name.toLowerCase(), name, params: qhSigParams(line.slice(open + 1, close)),
             ret: arrow < 0 ? '' : line.slice(arrow + 3) };
  });
  qhBuiltinSigCache[eng] = out;
  return out;
}

// Words before `(` that do not call a function: the paren is a list, a
// subquery or a type. A call is never found through one of them.
const QH_SIG_NOT_CALL = new Set(['IN', 'EXISTS', 'ANY', 'ALL', 'SOME', 'VALUES', 'AS', 'OVER', 'FROM', 'JOIN',
  'INTO', 'TABLE', 'USING', 'ON', 'WHERE', 'AND', 'OR', 'NOT', 'SELECT', 'WITH', 'RETURNING', 'BY',
  'PARTITION', 'WINDOW', 'FILTER', 'WITHIN', 'KEY', 'REFERENCES', 'UNIQUE', 'CHECK', 'INDEX', 'VIEW']);

// Is the caret inside a comment or a string? A line comment counts up to the
// end of its line. A string or block comment with no end counts to the end.
function qhSigInText(text, caret) {
  for (const t of qhSqlTokens(text, true)) {
    if (t.s >= caret) return false;
    if (t.k !== 'cmt' && t.k !== 'str' && t.k !== 'qid') continue;
    if (caret < t.e) return true;
    if (caret === t.e) {
      if (t.k === 'cmt') return t.v.startsWith('--') || !t.v.endsWith('*/') || t.v.length < 4;
      const q = t.v[0];
      if (t.k === 'str' && q === "'") return !/^'(?:[^']|'')*'$/.test(t.v);
      if (t.k === 'qid' && q === '"') return !/^"(?:[^"]|"")*"$/.test(t.v);
      if (t.k === 'qid' && q === '[') return !t.v.endsWith(']');
      return false;
    }
  }
  return false;
}

function qhCallAt(sql, caret, engineId) {
  const text = String(sql == null ? '' : sql);
  const c = Number(caret);
  if (!text || !Number.isFinite(c) || c < 0 || c > text.length) return null;
  if (qhSigInText(text, c)) return null;
  const T = qhSqlTokens(text).filter(t => t.e <= c);
  // The open parentheses before the caret, innermost last, with their commas.
  const stack = [];
  T.forEach((t, i) => {
    if (t.k !== 'p') return;
    if (t.v === '(') stack.push({ i, commas: 0 });
    else if (t.v === ')') stack.pop();
    else if (t.v === ',' && stack.length) stack[stack.length - 1].commas++;
  });
  // The innermost paren that a name opens. A bare paren (grouping) passes the
  // search outward. A list, a subquery or a type paren ends it.
  for (let k = stack.length - 1; k >= 0; k--) {
    const { i, commas } = stack[k];
    const n = T[i - 1];
    if (!n || (n.k !== 'word' && n.k !== 'qid')) {
      if (T[i + 1] && T[i + 1].u === 'SELECT') return null;      // a subquery
      continue;
    }
    if (n.k === 'word' && QH_SIG_NOT_CALL.has(n.u)) return null;
    // A SELECT at the paren's own level makes it a subquery, not a call.
    let d = 0;
    for (let j = i + 1; j < T.length; j++) {
      if (T[j].v === '(') d++;
      else if (T[j].v === ')') d--;
      else if (d === 0 && T[j].u === 'SELECT') return null;
    }
    let qual = null;
    if (T[i - 2] && T[i - 2].v === '.' && T[i - 3] && (T[i - 3].k === 'word' || T[i - 3].k === 'qid')) qual = qhFromUnquote(T[i - 3]);
    return { name: qhFromUnquote(n), qual, open: T[i].s, nameStart: qual != null ? T[i - 3].s : n.s, arg: commas, exec: false };
  }
  if (stack.length) return null;
  // SQL Server: EXEC proc a, b. The statement at the caret opens with EXEC or
  // EXECUTE, then the name. `EXEC @rc = proc` skips the return variable.
  if (engineId !== 'mssql') return null;
  const ranges = qhStatementRanges(text, 'mssql');
  const r = ranges.filter(x => x.start < c).pop();
  if (!r || (r.semi && c > r.end)) return null;
  const S = T.filter(t => t.s >= r.start);
  if (!S[0] || (S[0].u !== 'EXEC' && S[0].u !== 'EXECUTE')) return null;
  let j = 1;
  if (S[j] && S[j].v === '@' && S[j + 1] && S[j + 2] && S[j + 2].v === '=') j += 3;
  if (!S[j] || (S[j].k !== 'word' && S[j].k !== 'qid')) return null;
  const parts = [S[j]];
  while (S[j + 1] && S[j + 1].v === '.' && S[j + 2] && (S[j + 2].k === 'word' || S[j + 2].k === 'qid')) { parts.push(S[j + 2]); j += 2; }
  const last = parts[parts.length - 1];
  if (c <= last.e) return null;                                 // the caret is on the name
  if (S[j + 1] && S[j + 1].v === '(' && S[j + 1].s === last.e) return null;
  const commas = S.slice(j + 1).filter(t => t.v === ',').length;
  return { name: qhFromUnquote(last), qual: parts.length > 1 ? qhFromUnquote(parts[parts.length - 2]) : null,
           open: last.s, nameStart: parts[0].s, arg: commas, exec: true };
}

function qhSignaturesFor(call, schema, engineId) {
  if (!call || !call.name) return [];
  const lname = String(call.name).toLowerCase();
  const qual = call.qual ? String(call.qual).toLowerCase() : null;
  const out = [];
  const cat = (schema && schema.functionSigs && schema.functionSigs[lname]) || [];
  cat.forEach((f) => {
    if (qual && f.s && String(f.s).toLowerCase() !== qual) return;
    if (call.exec && f.k !== 'procedure') return;
    out.push({ name: (f.s ? f.s + '.' : '') + f.n, params: qhSigParams(f.args), ret: f.ret ? String(f.ret) : '',
               kind: f.k || 'function', src: 'catalog' });
  });
  if (!call.exec && (!qual || qual === 'pg_catalog')) {
    qhBuiltinSigs(engineId).forEach((b) => {
      if (b.lname === lname) out.push({ name: b.name, params: b.params, ret: b.ret, kind: 'built-in', src: 'builtin' });
    });
  }
  return out;
}

// Does a signature take more parameters after the last one it names?
function qhSigVariadic(sig) {
  const p = sig && sig.params;
  return !!(p && p.length && /^\.\.\.$|^VARIADIC\b/i.test(p[p.length - 1]));
}
function qhSigPick(list, arg) {
  const k = (list || []).findIndex(s => s.params.length > arg || qhSigVariadic(s));
  return k < 0 ? 0 : k;
}
function qhSigActive(sig, arg) {
  if (!sig || !sig.params.length) return -1;
  if (arg < sig.params.length) return arg;
  return qhSigVariadic(sig) ? sig.params.length - 1 : -1;
}

// The findings list (2026-11-11, Devart H3). One row per finding of the open
// tab: the server pre-flight blockers first (they stop a run), then the code
// analysis findings (qhLintFindings) in text order. A row is { level: 'error' |
// 'warn' | 'info', code, title, text, start, end, line, col }. `line` and
// `col` count from 1. A blocker that names no place in the text has start,
// line and col null. `blockers` are [{ message, start, end }].
function qhProblems(sql, findings, blockers) {
  const text = String(sql == null ? '' : sql);
  const lc = (at) => {
    const before = text.slice(0, at);
    return { line: before.split('\n').length, col: at - before.lastIndexOf('\n') };
  };
  const rows = [];
  (blockers || []).forEach((b) => {
    if (!b || !b.message) return;
    const at = Number.isFinite(b.start) ? b.start : null;
    rows.push({ level: 'error', code: 'Blocked', title: 'The pre-flight check blocks this statement',
                text: String(b.message), start: at, end: at == null ? null : b.end,
                ...(at == null ? { line: null, col: null } : lc(at)) });
  });
  (findings || []).forEach((f) => {
    rows.push({ level: f.level, code: f.id, title: f.title, text: f.text, start: f.start, end: f.end, ...lc(f.start) });
  });
  return rows;
}

// The name under a viewport point, read from the highlight layer. The textarea
// sits on top of that layer and has no rectangle per word. So this test uses
// ranges over the layer's text nodes, as onIssueMove uses the marks.
// Returns { offset, rect } or null.
function qhHoverOffsetAt(root, text, x, y) {
  const doc = root && root.ownerDocument;
  if (!doc || !doc.createRange || !doc.createTreeWalker) return null;
  const nodes = [];
  const walk = doc.createTreeWalker(root, 4);              // 4 = NodeFilter.SHOW_TEXT
  let pos = 0, nd;
  while ((nd = walk.nextNode())) { nodes.push({ node: nd, a: pos, z: pos + nd.nodeValue.length }); pos += nd.nodeValue.length; }
  if (pos < text.length) return null;                       // the layer is behind the text
  const range = doc.createRange();
  let ni = 0;
  for (const t of qhSqlTokens(text)) {
    if (t.k !== 'word' && t.k !== 'qid') continue;
    while (ni < nodes.length && nodes[ni].z <= t.s) ni++;
    let nj = ni;
    while (nj < nodes.length && nodes[nj].z < t.e) nj++;
    if (nj >= nodes.length) break;
    try {
      range.setStart(nodes[ni].node, t.s - nodes[ni].a);
      range.setEnd(nodes[nj].node, t.e - nodes[nj].a);
    } catch (_) { continue; }
    const rs = typeof range.getClientRects === 'function' ? range.getClientRects() : [];
    for (let j = 0; j < rs.length; j++) {
      const r = rs[j];
      if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) {
        return { offset: t.s, rect: { top: r.top, bottom: r.bottom, left: r.left, right: r.right } };
      }
    }
  }
  return null;
}

// The next row a key moves to in the completion list. Group headers are skipped.
function qhAcStep(items, idx, dir) {
  const n = items.length;
  for (let k = 1; k <= n; k++) {
    const j = (((idx + dir * k) % n) + n) % n;
    if (!items[j].header) return j;
  }
  return idx;
}

function qhStarExpansion(sql, caret, schema, engineId, live) {
  const text = String(sql || '');
  if (text[caret - 1] !== '*') return null;
  const toks = qhSqlTokens(text);
  const k = toks.findIndex(t => t.v === '*' && t.e === caret);
  if (k < 0) return null;                                   // inside a string or a comment
  let a = k, b = k;
  while (a > 0 && toks[a - 1].v !== ';') a--;
  while (b < toks.length && toks[b].v !== ';') b++;
  const S = toks.slice(a, b), at = k - a;
  const depth = [];
  let d = 0;
  for (const t of S) { if (t.v === ')') d--; depth.push(d); if (t.v === '(') d++; }
  const D = depth[at];

  // `x.*` expands x only.
  const isName = (t) => t && (t.k === 'qid' || (t.k === 'word' && !QH_NOT_NAME.has(t.u)));
  let qual = null, first = at;
  if (at >= 2 && S[at - 1].v === '.' && isName(S[at - 2])) { qual = S[at - 2]; first = at - 2; }

  // Only a select-list star: right after SELECT / DISTINCT / ALL / a comma,
  // TOP n, or the ) of TOP (n) / DISTINCT ON (…). `count(*)` and `a * b` are not.
  const prev = S[first - 1];
  if (!prev) return null;
  let ok = prev.v === ',' || (prev.k === 'word' && ['SELECT', 'DISTINCT', 'ALL', 'PERCENT', 'TIES'].includes(prev.u))
    || (prev.k === 'num' && S[first - 2] && S[first - 2].u === 'TOP');
  if (!ok && prev.v === ')') {
    let j = first - 2;
    while (j >= 0 && !(S[j].v === '(' && depth[j] === depth[first - 1])) j--;
    ok = j > 0 && (S[j - 1].u === 'TOP' || S[j - 1].u === 'ON');
  }
  if (!ok) return null;
  let sel = false;
  for (let i = first - 1; i >= 0; i--) {
    if (depth[i] < D) break;                                // left the parenthesised group
    if (depth[i] > D || S[i].k !== 'word') continue;
    if (S[i].u === 'SELECT') { sel = true; break; }
    if (S[i].u === 'ON' && S[i - 1] && S[i - 1].u === 'DISTINCT') continue;   // DISTINCT ON (…)
    if (QH_STAR_STOP.has(S[i].u)) break;
  }
  if (!sel) return null;

  // This SELECT's FROM list.
  const refs = qhReadFromList(S, depth, at + 1, D, false);
  if (!refs) return null;

  const unq = qhFromUnquote;
  const sch = schema || {};
  const colsOf = (r) => {
    const cols = qhRefLookup(r, sch, sch.tableColsQ, sch.tableCols);
    if (cols && cols.length) return cols;
    return live ? live(r.parts.map(p => p.v).join('.')) : null;
  };
  const label = (r) => (r.alias || r.parts[r.parts.length - 1]);
  let want = refs;
  if (qual) {
    const q = unq(qual).toLowerCase();
    want = refs.filter(r => unq(label(r)).toLowerCase() === q);
    if (want.length !== 1) return null;
  }
  const cols = want.map(colsOf);
  if (cols.some(c => c === null)) return null;
  const names = want.filter((r, j) => cols[j] === undefined).map(r => r.parts.map(p => p.v).join('.'));
  if (names.length) return { kind: 'lookup', names };

  const quote = (n) => (typeof qhQuoteIdentFor === 'function' ? qhQuoteIdentFor(n, engineId) : qhQuoteIdent(n));
  const list = [];
  want.forEach((r, j) => cols[j].forEach(c => list.push(
    qual ? qual.v + '.' + quote(c) : want.length > 1 ? label(r).v + '.' + quote(c) : quote(c))));
  return { kind: 'ready', start: S[first].s, end: caret, text: list.join(', '), count: list.length };
}

// Expand every select-list `*` of the text, or of the range [a, b] (Devart
// C2, Shift+Alt+E). Each star goes through qhStarExpansion, so the same rules
// hold: a star whose columns cannot be read exactly stays. Returns
// { edits: [{ start, end, text, count }], names }: `names` are relations to
// ask the server about first (see qhStarExpansion's 'lookup').
function qhExpandAllStars(sql, schema, engineId, live, range) {
  const text = String(sql || '');
  const edits = [], names = [];
  qhSqlTokens(text).forEach(t => {
    if (t.k !== 'p' || t.v !== '*') return;
    if (range && (t.s < range[0] || t.e > range[1])) return;
    const r = qhStarExpansion(text, t.e, schema, engineId, live);
    if (!r) return;
    if (r.kind === 'lookup') r.names.forEach(n => { if (names.indexOf(n) < 0) names.push(n); });
    else edits.push({ start: r.start, end: r.end, text: r.text, count: r.count });
  });
  return { edits, names };
}

// Apply edits that do not overlap to a text. Returns { text, caret } with the
// caret moved by the edits before it.
function qhApplyEdits(text, edits, caret) {
  let out = String(text), c = caret;
  edits.slice().sort((x, y) => y.start - x.start).forEach(e => {
    out = out.slice(0, e.start) + e.text + out.slice(e.end);
    if (e.end <= caret) c += e.text.length - (e.end - e.start);
  });
  return { text: out, caret: c };
}

// ---------------------------------------------------------------------------
// Alias rename and name qualification (2026-11-12, Devart F1 and F4)
//
// Shift+F6 renames the table alias at the caret: its definition after FROM or
// JOIN and every `alias.` of the statement (qhRenameAlias). F2 stays the tab
// rename. Shift+Alt+Q puts the alias or the table name before each bare
// column, and the schema before each bare table (qhQualifyNames). Both work
// on the catalog the editor has, and both leave a name alone when they are
// not sure about it.
// ---------------------------------------------------------------------------

// The tokens of the statement around token k, and their bracket depths.
function qhStmtSlice(toks, k) {
  let a = k, b = k;
  while (a > 0 && toks[a - 1].v !== ';') a--;
  while (b < toks.length && toks[b].v !== ';') b++;
  const S = toks.slice(a, b);
  const depth = [];
  let d = 0;
  for (const t of S) { if (t.v === ')') d--; depth.push(d); if (t.v === '(') d++; }
  return { S, depth };
}

// The alias at the caret, as { name, places, taken }, or { error } when the
// statement defines that name twice, or null when the caret is on no alias.
// `places` are the definition and every `name.` of the statement. `taken` are
// the other aliases and the table names of the statement, in lower case.
function qhAliasAt(sql, caret) {
  const text = String(sql || '');
  const toks = qhSqlTokens(text);
  const isName = (t) => !!t && (t.k === 'qid' || t.k === 'word');
  let k = toks.findIndex(t => isName(t) && t.s <= caret && caret < t.e);
  if (k < 0) k = toks.findIndex(t => isName(t) && t.e === caret);
  if (k < 0) return null;
  const { S, depth } = qhStmtSlice(toks, k);
  const want = qhFromUnquote(toks[k]).toLowerCase();
  const defs = [], taken = new Set();
  S.forEach((t, i) => {
    if (t.k !== 'word' || t.u !== 'FROM') return;
    (qhReadFromList(S, depth, i, depth[i], true, true) || []).forEach(r => {
      if (r.parts && r.parts.length) taken.add(qhFromUnquote(r.parts[r.parts.length - 1]).toLowerCase());
      if (!r.alias) return;
      const n = qhFromUnquote(r.alias).toLowerCase();
      if (n === want) { if (!defs.includes(r.alias)) defs.push(r.alias); } else taken.add(n);
    });
  });
  const uses = S.filter((t, i) => isName(t) && qhFromUnquote(t).toLowerCase() === want &&
    S[i + 1] && S[i + 1].v === '.' && !(S[i - 1] && S[i - 1].v === '.'));
  const here = toks[k];
  if (!defs.includes(here) && !uses.includes(here)) return null;
  if (!defs.length) return null;
  if (defs.length > 1) return { error: 'The alias ' + defs[0].v + ' is defined twice in this statement. Rename it by hand.' };
  taken.delete(want);
  const places = [defs[0], ...uses].sort((x, y) => x.s - y.s).map(t => ({ s: t.s, e: t.e }));
  return { name: defs[0].v, places, taken, def: { s: defs[0].s, e: defs[0].e } };
}

// The edits that rename the alias at the caret to `newName`, as
// { edits, count }, or { error }. The new name is a plain name that is no
// keyword and no other name of the statement.
function qhRenameAlias(sql, caret, newName, engineId) {
  const at = qhAliasAt(sql, caret);
  if (!at) return { error: 'Put the caret on a table alias.' };
  if (at.error) return at;
  const nn = String(newName == null ? '' : newName).trim();
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(nn)) return { error: 'Use letters, digits and _ for the alias, with a letter or _ first.' };
  const kw = new Set(qhKeywordsFor(engineId).map(w => String(w).toUpperCase()));
  if (kw.has(nn.toUpperCase()) || QH_NOT_NAME.has(nn.toUpperCase())) return { error: 'The name ' + nn + ' is an SQL keyword. Use a different name.' };
  if (at.taken.has(nn.toLowerCase())) return { error: 'The name ' + nn + ' is in this statement already. Use a different name.' };
  const edits = at.places.map(p => ({ start: p.s, end: p.e, text: nn }));
  return { edits, count: edits.length, from: at.name };
}

// Go to definition (2026-11-12, Devart G1). The alias at the caret goes to
// its definition after FROM or JOIN. A CTE name goes to its name after WITH.
// Any other name is an object for the tree: { kind: 'object', name }. Returns
// { kind: 'text', start, end }, { kind: 'none', message } or null.
function qhDefinitionAt(sql, caret, engineId) {
  const al = qhAliasAt(sql, caret);
  if (al && al.error) return { kind: 'none', message: al.error };
  if (al) return { kind: 'text', start: al.def.s, end: al.def.e };
  const text = String(sql || '');
  const toks = qhSqlTokens(text);
  const k = toks.findIndex(t => (t.k === 'word' || t.k === 'qid') && t.s <= caret && caret <= t.e);
  if (k >= 0 && !(toks[k - 1] && toks[k - 1].v === '.')) {
    const { S, depth } = qhStmtSlice(toks, k);
    const cte = qhCteDefs(S, depth).get(qhFromUnquote(toks[k]).toLowerCase());
    if (cte) return { kind: 'text', start: cte.name.s, end: cte.name.e };
  }
  const name = qhObjectNameAt(text, caret, engineId);
  return name ? { kind: 'object', name } : null;
}

// The dotted name at the caret as typed (`sales.orders`, `"My T"`), for the
// CREATE script (Devart B5). Null on a keyword, a string or no name.
function qhObjectNameAt(sql, caret, engineId) {
  const text = String(sql || '');
  const toks = qhSqlTokens(text);
  const kw = new Set(qhKeywordsFor(engineId).map(w => String(w).toUpperCase()));
  const isName = (t) => !!t && (t.k === 'qid' || (t.k === 'word' && !kw.has(t.u) && !QH_NOT_NAME.has(t.u)));
  let k = toks.findIndex(t => t.s <= caret && caret < t.e);
  if (k < 0 || !isName(toks[k])) k = toks.findIndex(t => t.e === caret);
  if (k < 0 || !isName(toks[k])) return null;
  let a = k, b = k;
  while (a >= 2 && toks[a - 1].v === '.' && isName(toks[a - 2])) a -= 2;
  while (b + 2 < toks.length && toks[b + 1].v === '.' && isName(toks[b + 2])) b += 2;
  return text.slice(toks[a].s, toks[b].e);
}

// Qualify the names of every SELECT of the text, or of the statements that
// touch the range [a, b] (Devart F4). For each query block whose FROM list
// reads exactly:
//   * a bare table that the catalog knows in one schema gets that schema.
//   * a bare column gets the alias, or the table name, of the one relation
//     that has it. A column of two relations stays and goes to `twice`.
// A block with a relation of unknown columns (a subquery, a CTE, a table the
// catalog does not have) qualifies no column: a bare name could be from it.
// It skips these names: a function, a name after AS or `::`, a variable,
// a typed literal, an output alias, a name after another name (an implicit
// alias), and a name already qualified. Returns { edits, tables, columns,
// twice }.
function qhQualifyNames(sql, schema, engineId, range) {
  const text = String(sql || '');
  const sch = schema || {};
  const toks = qhSqlTokens(text);
  const edits = [], twice = [];
  let tables = 0, columns = 0;
  const kw = new Set(qhKeywordsFor(engineId).map(w => String(w).toUpperCase()));
  const quote = (n) => (typeof qhQuoteIdentFor === 'function' ? qhQuoteIdentFor(n, engineId) : qhQuoteIdent(n));
  const isName = (t) => !!t && (t.k === 'qid' || (t.k === 'word' && !kw.has(t.u) && !QH_NOT_NAME.has(t.u)));
  const inRange = (t) => !range || (t.s >= range[0] && t.e <= range[1]);
  let k = 0;
  while (k < toks.length) {
    const { S, depth } = qhStmtSlice(toks, k);
    k += S.length + 1;
    if (!S.length) continue;
    const ctes = qhCteDefs(S, depth);
    // Each SELECT starts a block that runs to where its bracket closes. A later
    // SELECT (a subquery, the next part of a UNION) takes its own tokens: the
    // owner of each token is the last SELECT whose span holds it.
    const starts = [];
    S.forEach((t, i) => { if (t.k === 'word' && t.u === 'SELECT') starts.push(i); });
    const ends = starts.map(i => {
      let j = i + 1;
      while (j < S.length && depth[j] >= depth[i]) j++;
      return j;
    });
    const owner = S.map(() => -1);
    starts.forEach((i, n) => { for (let j = i; j < ends[n]; j++) owner[j] = n; });
    starts.forEach((i, n) => {
      const D = depth[i];
      const refs = qhReadFromList(S, depth, i + 1, D, false);
      if (!refs || !refs.length) return;
      const skip = new Set();
      refs.forEach(r => { r.parts.forEach(t => skip.add(t)); if (r.alias) skip.add(r.alias); });
      // (a) the schema before a bare table.
      refs.forEach(r => {
        if (r.parts.length !== 1) return;
        const t = r.parts[0], nm = qhFromUnquote(t);
        if (ctes.has(nm.toLowerCase()) || !inRange(t)) return;
        const q = qhLookupCi(sch.qualify, nm, t.k === 'qid');
        if (typeof q !== 'string' || q.indexOf('.') < 0) return;
        edits.push({ start: t.s, end: t.s, text: quote(q.slice(0, q.indexOf('.'))) + '.' });
        tables++;
      });
      // (b) the relation before a bare column.
      const cols = refs.map(r => (r.parts.length && !ctes.has(qhFromUnquote(r.parts[r.parts.length - 1]).toLowerCase())
        ? qhRefLookup(r, sch, sch.tableColsQ, sch.tableCols) : undefined));
      if (cols.some(c => !Array.isArray(c))) return;
      const lower = cols.map(c => new Set(c.map(x => String(x).toLowerCase())));
      const label = (r) => (r.alias || r.parts[r.parts.length - 1]).v;
      const outNames = new Set();
      for (let j = i + 1; j < ends[n]; j++) {
        if (owner[j] !== n) continue;
        const t = S[j];
        if (!isName(t) || skip.has(t)) continue;
        const prev = S[j - 1], next = S[j + 1];
        const nm = qhFromUnquote(t).toLowerCase();
        if (prev && (prev.u === 'AS' || isName(prev) || prev.v === ')' || prev.k === 'num' || prev.k === 'str')) {
          if (depth[j] === D) outNames.add(nm);
          continue;
        }
        // `x.`, `.x`, `x(`, `::x`, `@x`, `:x`, `$x`, and a typed literal `DATE '...'`.
        if ((prev && ['.', ':', '@', '$'].includes(prev.v)) || (next && (next.v === '.' || next.v === '(' || next.k === 'str'))) continue;
        if (nm === 'true' || nm === 'false' || outNames.has(nm) || !inRange(t)) continue;
        const hit = [];
        lower.forEach((set, x) => { if (set.has(nm)) hit.push(x); });
        if (hit.length === 1) {
          edits.push({ start: t.s, end: t.s, text: label(refs[hit[0]]) + '.' });
          columns++;
        } else if (hit.length > 1 && !twice.includes(t.v)) twice.push(t.v);
      }
    });
  }
  return { edits, tables, columns, twice };
}

// ---------------------------------------------------------------------------
// GROUP BY from the select list (2026-11-11, Devart A14)
//
// After `GROUP BY ` the typing list offers the select list's expressions that
// hold no aggregate, in one row. It drops a `*`, a constant, a window
// expression (OVER) and an alias (`expr AS a`, `expr a`). DISTINCT, ALL and
// TOP n at the start are no part of the first expression. The text of each
// expression is copied as written. Every engine.
// Returns { start, end, text, count } or null.
// ---------------------------------------------------------------------------
const QH_AGG_FUNCS = new Set(('COUNT COUNT_BIG SUM AVG MIN MAX STRING_AGG ARRAY_AGG GROUP_CONCAT LISTAGG STDDEV ' +
  'STDDEV_POP STDDEV_SAMP STDEV STDEVP VARIANCE VAR VARP VAR_POP VAR_SAMP BOOL_AND BOOL_OR EVERY BIT_AND BIT_OR ' +
  'BIT_XOR JSON_AGG JSONB_AGG JSON_OBJECT_AGG JSONB_OBJECT_AGG XMLAGG APPROX_COUNT_DISTINCT APPROX_DISTINCT ' +
  'CHECKSUM_AGG MODE PERCENTILE_CONT PERCENTILE_DISC UNIQ UNIQEXACT ANY ANYLAST ARGMAX ARGMIN GROUPARRAY ' +
  'GROUPUNIQARRAY QUANTILE QUANTILES MEDIAN TOPK SUMIF COUNTIF AVGIF MAP_AGG ARBITRARY COUNT_IF MAX_BY MIN_BY').split(' '));
function qhGroupByFill(sql, caret) {
  const text = String(sql || '');
  const before = text.slice(0, caret);
  const m = before.match(/[A-Za-z_][A-Za-z0-9_]*$/);
  const start = caret - (m ? m[0].length : 0);
  const toks = qhSqlTokens(text);
  // The statement around the caret, split at `;`.
  let a = toks.findIndex(t => t.s >= start);
  if (a < 0) a = toks.length;
  let b = a;
  while (a > 0 && toks[a - 1].v !== ';') a--;
  while (b < toks.length && toks[b].v !== ';') b++;
  const S = toks.slice(a, b);
  const k = S.findIndex(t => t.s >= start);
  const at = k < 0 ? S.length : k;
  if (at < 2 || S[at - 1].u !== 'BY' || S[at - 2].u !== 'GROUP') return null;
  const depth = [];
  let d = 0;
  for (const t of S) { if (t.v === ')') d--; depth.push(d); if (t.v === '(') d++; }
  const D = depth[at - 2];
  // The SELECT and the FROM of this level.
  let sel = -1;
  for (let i = at - 3; i >= 0; i--) {
    if (depth[i] < D) break;
    if (depth[i] === D && S[i].u === 'SELECT') { sel = i; break; }
  }
  if (sel < 0) return null;
  let from = -1;
  for (let i = sel + 1; i < at - 2; i++) if (depth[i] === D && S[i].u === 'FROM') { from = i; break; }
  if (from < 0) return null;
  // Split the select list at the commas of this level.
  const parts = [];
  let cur = [];
  for (let i = sel + 1; i < from; i++) {
    if (depth[i] === D && S[i].v === ',') { parts.push(cur); cur = []; continue; }
    cur.push(i);
  }
  parts.push(cur);
  if (parts[0].length) {                                   // DISTINCT, ALL, TOP n, TOP (n)
    let p = parts[0];
    while (p.length && (S[p[0]].u === 'DISTINCT' || S[p[0]].u === 'ALL')) p = p.slice(1);
    if (p.length && S[p[0]].u === 'TOP') {
      p = p.slice(1);
      if (p.length && S[p[0]].v === '(') { let q = 0; while (q < p.length && S[p[q]].v !== ')') q++; p = p.slice(q + 1); }
      else if (p.length && S[p[0]].k === 'num') p = p.slice(1);
      while (p.length && (S[p[0]].u === 'PERCENT' || (S[p[0]].u === 'WITH' && S[p[1]] && S[p[1]].u === 'TIES'))) p = p.slice(S[p[0]].u === 'WITH' ? 2 : 1);
    }
    parts[0] = p;
  }
  const out = [];
  parts.forEach(p => {
    if (!p.length) return;
    // An alias: `expr AS a`, or a name after a finished expression.
    let q = p.slice();
    const asAt = q.findIndex(i => depth[i] === D && S[i].u === 'AS');
    if (asAt > 0) q = q.slice(0, asAt);
    else if (q.length > 1) {
      const last = S[q[q.length - 1]], prev = S[q[q.length - 2]];
      const lastName = last.k === 'qid' || (last.k === 'word' && !QH_KW_SET.has(last.u));
      const prevEnds = prev.k === 'word' || prev.k === 'qid' || prev.k === 'num' || prev.k === 'str' || prev.v === ')';
      if (lastName && prevEnds && prev.v !== '.') q = q.slice(0, -1);
    }
    if (!q.length) return;
    const T = q.map(i => S[i]);
    if (T.length === 1 && (T[0].v === '*' || T[0].k === 'num' || T[0].k === 'str' || ['NULL', 'TRUE', 'FALSE'].includes(T[0].u))) return;
    if (T.length >= 3 && T[T.length - 1].v === '*' && T[T.length - 2].v === '.') return;   // t.*
    if (T.some(t => t.u === 'OVER')) return;
    for (let j = 0; j + 1 < T.length; j++) if (T[j].k === 'word' && QH_AGG_FUNCS.has(T[j].u) && T[j + 1].v === '(') return;
    out.push(text.slice(T[0].s, T[T.length - 1].e));
  });
  if (!out.length) return null;
  return { start, end: caret, text: out.join(', '), count: out.length };
}

// The row of the typing list for qhGroupByFill.
function qhGroupByItem(g) {
  const head = g.text.length > 48 ? g.text.slice(0, 48) + '…' : g.text;
  return { text: g.text, label: 'Group by ' + head, type: 'groupby', key: 'groupby' };
}

// The size of the typing list, dragged at its corner grip (Devart A4). It is
// kept in this browser only. A bad or missing value gives the CSS size.
const QH_AC_SIZE_KEY = 'qh.ac.size.v1';
function qhAcSizeLoad() {
  try {
    const v = JSON.parse(localStorage.getItem(QH_AC_SIZE_KEY) || 'null');
    return v && v.w > 0 && v.h > 0 ? { w: Math.min(Math.round(v.w), 900), h: Math.round(v.h) } : null;
  } catch (err) { return null; }
}
function qhAcSizeSave(v) {
  try { localStorage.setItem(QH_AC_SIZE_KEY, JSON.stringify(v)); } catch (err) { /* private window */ }
}

// PageUp / PageDown in the typing list: `n` rows that are not group headers,
// no wrap at either end.
function qhAcPage(items, idx, dir, n) {
  let j = idx;
  for (let moved = 0; moved < n;) {
    let k = j + dir;
    while (k >= 0 && k < items.length && items[k].header) k += dir;
    if (k < 0 || k >= items.length) break;
    j = k; moved++;
  }
  return j;
}
// The caret layer (Devart G5 and G6): the other places of the name under the
// caret, and the mate of the bracket or keyword under it (qhCaretMarks). It is
// a child with its own state, so a caret move redraws this layer only, not the
// editor. It reads the caret 120 ms after the last move, and only while the
// textarea has the focus and no selection. The caret is kept with the text it
// was read in, so new text (undo, a tab switch) never shows old marks.
const QH_MARKS_MAX_TEXT = 200000;
function QhCaretMarks({ taRef, value, show }) {
  const [at, setAt] = React.useState(null);   // { c, v }
  const ref = React.useRef(null);
  React.useEffect(() => {
    const ta = taRef.current;
    if (!ta) return undefined;
    let timer = null;
    const read = () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        const on = document.activeElement === ta && ta.selectionStart === ta.selectionEnd;
        setAt(on ? { c: ta.selectionStart, v: ta.value } : null);
      }, 120);
    };
    const onScroll = () => { const el = ref.current; if (el) { el.scrollTop = ta.scrollTop; el.scrollLeft = ta.scrollLeft; } };
    const onDoc = () => { if (document.activeElement === ta) read(); };
    const evs = ['keyup', 'mouseup', 'input', 'focus', 'blur', 'select'];
    evs.forEach(ev => ta.addEventListener(ev, read));
    ta.addEventListener('scroll', onScroll);
    document.addEventListener('selectionchange', onDoc);
    return () => {
      clearTimeout(timer);
      evs.forEach(ev => ta.removeEventListener(ev, read));
      ta.removeEventListener('scroll', onScroll);
      document.removeEventListener('selectionchange', onDoc);
    };
  }, [taRef]);
  const html = React.useMemo(() => {
    if (!at || at.v !== value || value.length > QH_MARKS_MAX_TEXT) return '';
    const list = qhCaretMarks(value, at.c);
    return list.length ? qhMarkHtml(value, list, r => 'qh-' + r.kind) : '';
  }, [at, value]);
  const on = !!(show && html);
  React.useLayoutEffect(() => {
    const ta = taRef.current, el = ref.current;
    if (ta && el) { el.scrollTop = ta.scrollTop; el.scrollLeft = ta.scrollLeft; }
  }, [on, html]);
  if (!on) return null;
  return (
    <pre className="qh-pre qh-marks" ref={ref} aria-hidden="true">
      <code dangerouslySetInnerHTML={{ __html: html }} />
    </pre>
  );
}

const QH_AC_TYPE_LABEL ={ keyword: 'kw', table: 'table', cte: 'cte', column: 'col', database: 'db', system: 'system', function: 'fn', expand: 'all cols', join: 'join',
  procedure: 'proc', schema: 'schema', index: 'index', snippet: 'snip', groupby: 'group', template: 'tpl' };

function SqlEditor({ value, onChange, fontSize, wrap, onRun, onRunSelection, selectionGetter, onSelectionChange, schema, engineId, focusSignal, formatSignal, insertSignal, revealRange, docId, issues, revealSignal, lints, onNotice, onScriptCreate, onRevealObject }) {
  const taRef = React.useRef(null);
  const issueRef = React.useRef(null);
  const lintRef = React.useRef(null);         // the code-analysis layer (qhLint)
  const tipRef = React.useRef(null);
  // Pre-flight blockers (see qhBlockerRanges). The underline hides while you
  // type and returns 1 s after the last typing edit.
  const [issuesShown, setIssuesShown] = React.useState(true);
  const [tip, setTip] = React.useState(null); // { i, msg, rect } — viewport coords
  // Quick info on hover (qhHoverInfo): { info, lines, rect }, viewport coords.
  // The card shows when the pointer rests on a name for 400 ms.
  const [hover, setHover] = React.useState(null);
  const hoverRef = React.useRef(null);
  const hoverTimer = React.useRef(null);
  const hoverLive = React.useRef({});          // this render's tip, ac, value and schema, for the timer
  const hideHover = () => {
    clearTimeout(hoverTimer.current);
    hoverTimer.current = null;
    setHover(null);
  };
  React.useEffect(() => () => clearTimeout(hoverTimer.current), []);
  const issueTimer = React.useRef(null);
  const ownVal = React.useRef(value);         // the last text this editor committed
  const issueEdit = (kind) => {
    clearTimeout(issueTimer.current);
    setTip(null);
    hideHover();
    if (kind === 'type' || kind === 'back' || kind === 'fwd' || kind === 'compose') {
      setIssuesShown(false);
      issueTimer.current = setTimeout(() => setIssuesShown(true), 1000);
    } else setIssuesShown(true);
  };
  React.useEffect(() => () => clearTimeout(issueTimer.current), []);
  // Text that did not come from a commit (a tab switch, a loaded query, undo or
  // redo) shows the marks at once.
  React.useEffect(() => {
    if (value === ownVal.current) return;
    ownVal.current = value;
    if (sesRef.current) setSes(null);
    clearTimeout(issueTimer.current);
    setTip(null);
    hideHover();
    setSig(null);
    sigArm.current = null;
    setIssuesShown(true);
  }, [value]);
  React.useEffect(() => {
    if (revealSignal == null) return;
    clearTimeout(issueTimer.current);
    setIssuesShown(true);
  }, [revealSignal]);
  const preRef = React.useRef(null);
  const gutRef = React.useRef(null);
  const measRef = React.useRef(null);
  const hostRef = React.useRef(null);
  // Wrapping breaks the assumption the gutter and the caret maths rest on: one
  // logical line is no longer one visual row. Heights are MEASURED rather than
  // derived from character counts — `overflow-wrap: break-word` breaks at spaces,
  // so arithmetic would be wrong on exactly the lines that wrap.
  const wrapRef = React.useRef(null);
  const [rowH, setRowH] = React.useState(null);
  const [, bumpWidth] = React.useReducer(x => x + 1, 0);
  const [charW, setCharW] = React.useState(fontSize * 0.6);
  const [ac, setAc] = React.useState(null); // {items, idx, top, lineTop, left, start, end} — viewport coords
  const acRef = React.useRef(null);
  // This render's list, for handlers that run before the next render.
  const acNow = React.useRef(null);
  acNow.current = ac;
  const openRef = React.useRef(null);     // the newest openAC, for a late lookup answer
  // Parameter info (qhCallAt, Devart B2): { open, exec, list, idx, chosen, arg,
  // top, below, left }, viewport coords. A `(` or `,` typed in a call, or
  // Ctrl+Shift+Space, opens the box. It follows the caret while the caret
  // stays in that call (`sigArm` keeps the call's `open`). Escape closes it.
  const [sig, setSig] = React.useState(null);
  const sigRef = React.useRef(null);
  // The alias rename box (Shift+F6): { name, count, caret, value, error,
  // top, left }, or { error, top, left } with no name when the caret is on no
  // alias. See qhRenameAlias.
  const [ren, setRen] = React.useState(null);
  // A box with only a message closes by itself after 3 s.
  React.useEffect(() => {
    if (!ren || ren.name) return undefined;
    const t = setTimeout(() => setRen(null), 3000);
    return () => clearTimeout(t);
  }, [ren]);
  const sigArm = React.useRef(null);
  // The size that the corner grip of the list sets (qhAcSizeLoad). A drag
  // writes the box directly. The size is stored when the drag ends.
  const [acSize, setAcSize] = React.useState(() => qhAcSizeLoad());
  const startAcResize = (e) => {
    e.preventDefault(); e.stopPropagation();
    const el = acRef.current;
    if (!el) return;
    const s0 = { x: e.clientX, y: e.clientY, w: el.offsetWidth, h: el.offsetHeight };
    let last = null;
    const move = (ev) => {
      const w = Math.round(Math.max(190, Math.min(900, window.innerWidth - 16, s0.w + ev.clientX - s0.x)));
      const h = Math.round(Math.max(120, Math.min(window.innerHeight - 40, s0.h + ev.clientY - s0.y)));
      el.style.width = w + 'px'; el.style.maxWidth = 'none'; el.style.maxHeight = h + 'px'; el.style.height = h + 'px';
      last = { w, h };
    };
    const up = () => {
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', up);
      el.style.height = '';
      if (last) { setAcSize(last); qhAcSizeSave(last); }
    };
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
  };
  const [dragOver, setDragOver] = React.useState(false);
  // The fields of the last snippet or template (Devart C3 and D2):
  // { list: [[s, e]], idx, final } (see qhStopsAfterEdit). Tab and Shift+Tab
  // go from field to field. `sesRef` is the newest value for the handlers.
  const [ses, setSesState] = React.useState(null);
  const sesRef = React.useRef(null);
  const stopRef = React.useRef(null);
  const setSes = (v) => { sesRef.current = v; setSesState(v); };
  // True when the selection is exactly a field. A field is a place to type,
  // not a selection: Run, F8 and the verdicts take the whole text, and a quote
  // or a bracket typed over it replaces it.
  const fieldSel = (ta) => {
    const f = sesRef.current;
    if (!f || !ta) return false;
    const a = Math.min(ta.selectionStart, ta.selectionEnd), b = Math.max(ta.selectionStart, ta.selectionEnd);
    return a !== b && f.list.some(([s, e]) => s === a && e === b);
  };
  const lines = value.split('\n');
  const lh = Math.round(fontSize * 1.55);
  const sch = schema || { tables: [], columns: [], dbs: [] };

  // Undo history (see qhUndoRecord). Keyed by the tab when the app passes
  // `docId`, so it follows the tab; otherwise this mount keeps its own.
  const ownKey = React.useRef({});
  const histKey = docId != null ? docId : ownKey.current;
  const pendSel = React.useRef(null);     // selection when the edit in flight began
  const histInput = React.useRef(false);  // a browser undo already answered in beforeinput
  const restoreSel = React.useRef(null);  // { text, sel } to put back after a step
  const live = React.useRef(null);        // this render's step + key, for native listeners

  // Every edit the editor makes itself goes through here, so it is in the
  // history before the parent hears of it.
  const commit = (next, kind, before, after) => {
    if (sesRef.current) setSes(qhStopsAfterEdit(sesRef.current, value, next, before));
    qhUndoRecord(qhUndoDoc(histKey, value), next, { kind, before, after });
    ownVal.current = next;
    issueEdit(kind);
    onChange(next);
  };
  const step = (dir) => {
    const r = qhUndoStep(qhUndoDoc(histKey, value), dir);
    if (!r) return;
    setAc(null);
    restoreSel.current = r;
    onChange(r.text);
  };
  live.current = { step, histKey };

  // Text the history has not seen came from outside the editor — the app
  // replacing it. It is recorded as a step of its own, so it undoes too.
  React.useLayoutEffect(() => {
    const h = qhUndoDoc(histKey, value);
    if (h.text !== value) qhUndoRecord(h, value, { kind: 'external' });
  }, [value, histKey]);

  // After a step, put the selection back once React has written the text —
  // before paint, so the caret never flashes at the end of the script.
  React.useLayoutEffect(() => {
    const r = restoreSel.current, ta = taRef.current;
    if (!r || !ta || ta.value !== r.text) return;
    restoreSel.current = null;
    const n = r.text.length;
    ta.setSelectionRange(Math.min(r.sel[0], n), Math.min(r.sel[1], n));
  });

  // `beforeinput` is the last moment the selection BEFORE an edit can be read;
  // React's onChange runs after the text has changed. It is also how the
  // browser's own Undo arrives from its menus, which is answered with ours.
  // A composition (IME, dead keys) starts a step of its own.
  React.useEffect(() => {
    const ta = taRef.current;
    if (!ta) return undefined;
    const onBefore = (e) => {
      if (e.inputType === 'historyUndo' || e.inputType === 'historyRedo') {
        e.preventDefault();
        histInput.current = true;
        live.current.step(e.inputType === 'historyUndo' ? -1 : 1);
        return;
      }
      histInput.current = false;
      pendSel.current = [ta.selectionStart, ta.selectionEnd];
    };
    const onCompose = () => qhUndoSeal(qhUndoDoc(live.current.histKey, ta.value));
    ta.addEventListener('beforeinput', onBefore);
    ta.addEventListener('compositionstart', onCompose);
    return () => {
      ta.removeEventListener('beforeinput', onBefore);
      ta.removeEventListener('compositionstart', onCompose);
    };
  }, []);

  // Caret geometry inside the hidden mirror. Its box sits exactly on the text
  // origin, so rects come back relative to the first character and the caller
  // only has to add the padding and the scroll offset.
  // The mirror is built imperatively and has NO JSX children: React must not
  // own nodes that wrapRect updates mid-keystroke, or a shrinking line count
  // has React removing a node this code already detached (it throws, and the
  // editor unmounts to the error boundary with the user's query in it).
  const syncMirror = (text) => {
    const m = wrapRef.current; if (!m) return null;
    const live = text.split('\n');
    while (m.children.length > live.length) m.removeChild(m.lastChild);
    while (m.children.length < live.length) m.appendChild(document.createElement('div'));
    live.forEach((l, i) => { const t = (l === '' ? '\u200b' : l); if (m.children[i].textContent !== t) m.children[i].textContent = t; });
    return m;
  };
  const wrapRect = (row, col) => {
    const ta = taRef.current;
    if (!ta) return null;
    // refreshAC runs synchronously inside onChange, so React's value is one
    // render behind the textarea's. Measure the live text or the popup lands a
    // whole visual row out whenever the keystroke crossed a wrap point.
    const m = syncMirror(ta.value);
    const el = m && m.children[row];
    if (!el) return null;
    const node = el.firstChild, mr = m.getBoundingClientRect();
    if (!node || !col) return { bottom: el.offsetTop + lh, right: 0 };
    const r = document.createRange();
    r.setStart(node, 0); r.setEnd(node, Math.min(col, node.length));
    const rects = r.getClientRects(), last = rects[rects.length - 1];
    if (!last) return { bottom: el.offsetTop + lh, right: 0 };
    return { bottom: last.bottom - mr.top, right: last.right - mr.left };
  };
  // A dropped name goes where the caret is, like a paste, not where the pointer
  // let go. The old code turned the pointer into a character index and inserted
  // there. A person who drags a database in from the sidebar lets go near the left
  // edge of the editor, and the name landed at the start of the line, away from the
  // statement being typed ("example_dbDROP database "). The caret is the
  // place the person is writing, so it is the one place that is never a surprise.
  // A selection is replaced, as a paste replaces it.
  // A saved query is a whole statement. Its row sets the drag type
  // QH_DRAG_BLOCK, and the text goes on lines of its own, as its Insert button puts it.
  const onDrop = (e) => {
    const text = e.dataTransfer.getData('text/plain');
    if (!text) return;
    e.preventDefault(); setDragOver(false);
    const ta = taRef.current;
    const a = ta.selectionStart, b = ta.selectionEnd;
    const block = Array.from(e.dataTransfer.types || []).includes(QH_DRAG_BLOCK);
    const r = block ? qhInsertBlock(value, a, b, text) : { value: value.slice(0, a) + text + value.slice(b), caret: a + text.length };
    const at = r.caret;
    commit(r.value, 'drop', [a, b], [at, at]);
    requestAnimationFrame(() => { if (ta) { ta.focus(); ta.selectionStart = ta.selectionEnd = at; } });
  };
  const onDragOver = (e) => {
    if (Array.from(e.dataTransfer.types || []).includes('text/plain')) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; if (!dragOver) setDragOver(true); }
  };

  React.useLayoutEffect(() => {
    if (measRef.current) setCharW(measRef.current.getBoundingClientRect().width / 10);
  }, [fontSize]);

  // Re-measure after every render while wrapping: the text, the font size and
  // the pane width all move the break points. `clientWidth` excludes the
  // textarea's scrollbar, so the mirror wraps where the textarea does and not
  // one character later. The equality guard is what stops the setState loop.
  React.useLayoutEffect(() => {
    if (!wrap) { if (rowH) setRowH(null); return; }
    const m = wrapRef.current, ta = taRef.current;
    if (!m || !ta) return;
    m.style.width = Math.max(0, ta.clientWidth - 32) + 'px';
    syncMirror(value);
    const hs = Array.prototype.map.call(m.children, (c) => c.offsetHeight);
    setRowH(prev => (prev && prev.length === hs.length && prev.every((h, i) => h === hs[i])) ? prev : hs);
  });
  // A pane resize changes the break points without changing any prop.
  React.useEffect(() => {
    if (!wrap || !hostRef.current || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => bumpWidth());
    ro.observe(hostRef.current);
    return () => ro.disconnect();
  }, [wrap]);

  // Run must honour a selection wherever it is pressed from — F5, ⌘↵, or the
  // toolbar button, which lives outside this component and cannot reach the
  // textarea. Publish a READER rather than pushing changes up: reading at the
  // moment Run is pressed cannot go stale, needs no event plumbing, and cannot
  // hand over a selection made in a tab the user has since left.
  //
  // Pushing was tried first, via React's onSelect. It does not fire from a
  // dispatched `select` event (React derives onSelect from its own
  // selectionchange plugin), so the parent silently never learned there was a
  // selection and Run kept running the whole tab — the exact bug this fixes.
  // A DOM selection also survives blur, so the toolbar button still sees it.
  React.useEffect(() => {
    if (!selectionGetter) return;
    selectionGetter.current = () => {
      const ta = taRef.current;
      if (!ta || ta.selectionStart === ta.selectionEnd || fieldSel(ta)) return '';
      return ta.value.slice(ta.selectionStart, ta.selectionEnd);
    };
    return () => { selectionGetter.current = null; };
  }, [selectionGetter]);

  // The Run button names a selection, and the verdicts judge it (2026-11-10),
  // so the parent must learn when the selection changes. React's onSelect
  // misses a dispatched select event (see above), so this listens on the
  // textarea and the document. It reports { lines, start, text }, or null
  // when Run would send the whole tab (qhSelectionLines gives 0).
  React.useEffect(() => {
    const ta = taRef.current;
    if (!ta || !onSelectionChange) return;
    let last = null, timer = null;
    const report = () => {
      const a = Math.min(ta.selectionStart, ta.selectionEnd), b = Math.max(ta.selectionStart, ta.selectionEnd);
      const n = fieldSel(ta) ? 0 : qhSelectionLines(ta.value, a, b);
      const key = n ? a + ':' + b + ':' + ta.value.length : '';
      if (key === last) return;
      last = key;
      onSelectionChange(n ? { lines: n, start: a, text: ta.value.slice(a, b) } : null);
    };
    const onDoc = () => { if (document.activeElement === ta) report(); };
    // An `input` reports in the next task. This listener runs before React's
    // own one, and in a real browser the parent's update ran in the gap: the
    // parent drew the textarea with the old text, so a key typed over a
    // selection was lost and the caret jumped to the end.
    const onInput = () => { clearTimeout(timer); timer = setTimeout(report, 0); };
    const evs = ['select', 'keyup', 'mouseup', 'focus'];
    evs.forEach(ev => ta.addEventListener(ev, report));
    ta.addEventListener('input', onInput);
    document.addEventListener('selectionchange', onDoc);
    report();
    return () => {
      clearTimeout(timer);
      evs.forEach(ev => ta.removeEventListener(ev, report));
      ta.removeEventListener('input', onInput);
      document.removeEventListener('selectionchange', onDoc);
    };
  }, [onSelectionChange, docId]);

  // Focus the textarea when the parent bumps focusSignal (e.g. a new query
  // tab or an opened table) so you can start typing without a click.
  React.useEffect(() => {
    if (!focusSignal) return;
    requestAnimationFrame(() => { const ta = taRef.current; if (ta) { ta.focus(); ta.selectionStart = ta.selectionEnd = ta.value.length; } });
  }, [focusSignal]);

  // Format when the parent bumps formatSignal (the Format button in the tab bar).
  // The first value is not a request, so a remount does not format by itself.
  const fmtSeen = React.useRef(formatSignal || 0);
  React.useEffect(() => {
    if (!formatSignal || formatSignal === fmtSeen.current) return;
    fmtSeen.current = formatSignal;
    setAc(null);
    formatNow();
    requestAnimationFrame(() => { const ta = taRef.current; if (ta) ta.focus(); });
  }, [formatSignal]);

  // Insert a block of SQL when the parent bumps insertSignal ({ text, n }): the
  // "Insert at caret" button of a saved query. The block goes at the caret, or
  // over the selection, on lines of its own (qhInsertBlock). One undo step.
  // The textarea keeps its caret when it loses focus, so the click on the
  // button does not move it. The first value is not a request.
  const insSeen = React.useRef(insertSignal ? insertSignal.n : 0);
  React.useEffect(() => {
    if (!insertSignal || !insertSignal.n || insertSignal.n === insSeen.current) return;
    insSeen.current = insertSignal.n;
    const ta = taRef.current;
    if (!ta || !insertSignal.text) return;
    const a = ta.selectionStart, b = ta.selectionEnd;
    const r = qhInsertBlock(value, a, b, insertSignal.text);
    setAc(null);
    commit(r.value, 'insert', [a, b], [r.caret, r.caret]);
    requestAnimationFrame(() => { const t = taRef.current; if (t) { t.focus(); t.selectionStart = t.selectionEnd = r.caret; } });
  }, [insertSignal]);

  // Select a span the parent points at — the hidden password, when Run stopped
  // on it — so the next keystroke replaces the placeholder.
  React.useEffect(() => {
    if (!revealRange) return;
    requestAnimationFrame(() => { const ta = taRef.current; if (ta) { ta.focus(); ta.setSelectionRange(revealRange.start, revealRange.end); } });
  }, [revealRange]);

  const sync = () => {
    const ta = taRef.current;
    if (preRef.current) { preRef.current.scrollTop = ta.scrollTop; preRef.current.scrollLeft = ta.scrollLeft; }
    if (issueRef.current) { issueRef.current.scrollTop = ta.scrollTop; issueRef.current.scrollLeft = ta.scrollLeft; }
    if (lintRef.current) { lintRef.current.scrollTop = ta.scrollTop; lintRef.current.scrollLeft = ta.scrollLeft; }
    if (stopRef.current) { stopRef.current.scrollTop = ta.scrollTop; stopRef.current.scrollLeft = ta.scrollLeft; }
    if (gutRef.current) gutRef.current.scrollTop = ta.scrollTop;
    if (ac) setAc(null);
    if (sig) setSig(null);
  };

  // VIEWPORT coordinates, because the menu is portalled to document.body and
  // positioned `fixed`: `.qh-editor` has `overflow: hidden`, and clipping by an
  // ancestor ignores z-index, so the list was cut off exactly where the results
  // panel starts. `lineTop` is the caret line's top edge — what the flip-up
  // branch measures against.
  const posAt = (ta, idx) => {
    const rows = ta.value.slice(0, idx).split('\n');
    const row = rows.length - 1, col = rows[row].length;
    const r = ta.getBoundingClientRect();
    if (wrap) {
      const p = wrapRect(row, col);
      if (p) {
        const top = r.top + 14 + p.bottom - ta.scrollTop + 2;
        return { top, lineTop: top - lh - 2, left: r.left + 16 + p.right - ta.scrollLeft };
      }
    }
    const top = r.top + 14 + (row + 1) * lh - ta.scrollTop + 2;
    return { top, lineTop: top - lh - 2, left: r.left + 16 + col * charW - ta.scrollLeft };
  };
  // Show, move or close the parameter info box for the caret. `arm` opens it
  // for the call at the caret. Without `arm` it shows only for the call that
  // an earlier `arm` opened.
  const updateSig = (ta, arm) => {
    if (!ta) return;
    const c = ta.selectionStart;
    const call = c === ta.selectionEnd ? qhCallAt(ta.value, c, engineId) : null;
    if (!call) { setSig(null); sigArm.current = null; return; }
    if (!arm && sigArm.current !== call.open) { setSig(null); return; }
    const list = qhSignaturesFor(call, sch, engineId);
    if (!list.length) { setSig(null); sigArm.current = null; return; }
    sigArm.current = call.open;
    const p = posAt(ta, c), q = posAt(ta, call.nameStart);
    const left = Math.abs(p.lineTop - q.lineTop) < 1 ? q.left : p.left;
    setSig((prev) => {
      const same = prev && prev.open === call.open && prev.list.length === list.length;
      const idx = same && prev.chosen ? Math.min(prev.idx, list.length - 1) : qhSigPick(list, call.arg);
      return { open: call.open, exec: call.exec, list, idx, chosen: !!(same && prev.chosen), arg: call.arg,
               top: p.lineTop, below: p.top, left };
    });
  };
  const stepSig = (dir) => setSig(s => (s && s.list.length > 1
    ? { ...s, idx: (s.idx + dir + s.list.length) % s.list.length, chosen: true } : s));
  const refreshAC = (ta, afterLookup, force) => {
    const caret = ta.selectionStart;
    if (caret !== ta.selectionEnd) { setAc(null); return; }
    // A list that Ctrl+Space opened in a column position stays one while the
    // caret stays in the same slot. Typing filters it.
    const cur = acNow.current;
    if (!force && cur && cur.cols && showColumns(ta, cur.slot, false)) return;
    // SELECT * expansion (see qhStarExpansion). A relation the catalog does not
    // list is asked about once; if the answer arrives while the caret is still
    // on the same star, the expansion is offered then.
    const look = sch.columnLookup || null;
    const se = qhStarExpansion(ta.value, caret, sch, engineId, look ? look.peek : null);
    if (se && se.kind === 'ready') {
      const p = posAt(ta, se.start);
      setAc({ items: [{ text: se.text, label: 'Expand * → ' + se.count + ' columns', type: 'expand' }], idx: 0, top: p.top, lineTop: p.lineTop, left: p.left, start: se.start, end: se.end });
      return;
    }
    if (se && se.kind === 'lookup' && look && !afterLookup) {
      const v = ta.value;
      Promise.all(se.names.map(n => look.fetch(n))).then(() => {
        const t = taRef.current;
        if (t && t.value === v && t.selectionStart === caret && t.selectionEnd === caret
            && document.activeElement === t) refreshAC(t, true, force);
      });
    }
    // After `alias.`, the columns of the table that the alias names (see
    // qhAliasColumns). A relation the catalog does not list is asked about
    // once. Anything else falls back to the normal list below.
    const peek = look ? look.peek : null;
    const known = (afterLookup && peek) ? (n) => { const c = peek(n); return c === undefined ? null : c; } : peek;
    const al = qhAliasColumns(ta.value, caret, sch, engineId, known);
    if (al && al.kind === 'ready') {
      const p = posAt(ta, al.start);
      setAc({ items: al.items, idx: Math.max(0, al.items.findIndex(it => !it.header)), top: p.top, lineTop: p.lineTop,
              left: p.left, start: al.start, end: al.end, cols: true, slot: al.slot, qual: al.qual, checked: [] });
      return;
    }
    if (al && al.kind === 'lookup' && look && !afterLookup) {
      const v = ta.value;
      Promise.all(al.names.map(n => look.fetch(n))).then(() => {
        const t = taRef.current;
        if (t && t.value === v && t.selectionStart === caret && t.selectionEnd === caret
            && document.activeElement === t) refreshAC(t, true, force);
      }, () => {});
    }
    const s = qhBuildSuggest(ta.value, caret, sch, engineId, force ? { force: true } : undefined);
    // JOIN suggestions (qhJoinSuggestions, qhJoinOnSuggestions) come first,
    // then the normal list. They open on a typed letter or Ctrl+Space only, so
    // Enter right after `JOIN ` or `ON ` still starts a new line.
    const typedWord = /[A-Za-z_][A-Za-z0-9_]*$/.test(ta.value.slice(0, caret));
    const js = (typedWord || force)
      ? (qhJoinSuggestions(ta.value, caret, sch, engineId, qhAliasRules(qhAliasOptsLoad().rules)) || qhJoinOnSuggestions(ta.value, caret, sch, engineId))
      : null;
    if (js) {
      const p = posAt(ta, js.start);
      setAc({ items: js.items.concat(s ? s.items : []), idx: 0, top: p.top, lineTop: p.lineTop, left: p.left,
              start: js.start, end: js.end });
      return;
    }
    // A list that only repeats the typed word adds nothing, and while it is
    // open it hides the wavy lines. Ctrl+Space still opens it.
    if (!s || (!force && qhListAddsNothing(s.items, ta.value.slice(s.start, s.end)))) { setAc(null); return; }
    const p = posAt(ta, s.start);
    setAc({ items: s.items, idx: 0, top: p.top, lineTop: p.lineTop, left: p.left, start: s.start, end: s.end });
  };

  const accept = (item) => {
    const ta = taRef.current;
    // Checked columns (Devart A13) go in together, in check order. After a
    // typed `t.`, each one gets the qualifier.
    if (ac && ac.cols && ac.checked && ac.checked.length) {
      const q = ac.qual || '';
      const from = q ? ac.slot : ac.start;
      const text = ac.checked.map(c => (q ? q + '.' : '') + c.text).join(', ');
      const nv = value.slice(0, from) + text + value.slice(ac.end);
      const caret = from + text.length;
      setAc(null);
      commit(nv, 'complete', ta ? [ta.selectionStart, ta.selectionEnd] : null, [caret, caret]);
      requestAnimationFrame(() => { if (ta) { ta.focus(); ta.selectionStart = ta.selectionEnd = caret; } });
      return;
    }
    // The item may carry its own start: a qualified insert replaces the
    // qualifier the user already typed rather than appending behind it.
    const from = (item && typeof item.start === 'number') ? item.start : ac.start;
    // A table after FROM or JOIN gets an alias (qhAutoAlias), unless the
    // switch in Format options is off.
    let text = item.text;
    if (item.type === 'table') {
      const ao = qhAliasOptsLoad();
      const q = sch.qualify ? sch.qualify[item.label] : null;
      const al = ao.auto ? qhAutoAlias(value, from, ac.end, item.label, engineId, qhAliasRules(ao.rules), q) : '';
      if (al) text += ' ' + al;
    }
    // A template replaces past the caret too (`replaceEnd`).
    const to = typeof item.replaceEnd === 'number' ? Math.max(item.replaceEnd, ac.end) : ac.end;
    const caret = from + (typeof item.caretAt === 'number' ? item.caretAt : text.length);
    setAc(null);
    // A snippet says where its caret goes (caretAt). Everything else ends
    // with the caret after the text. Fields go first (insertWithStops).
    if (item.stops && item.stops.length) { insertWithStops(from, to, text, item.stops, caret - from); return; }
    const nv = value.slice(0, from) + text + value.slice(to);
    commit(nv, 'complete', ta ? [ta.selectionStart, ta.selectionEnd] : null, [caret, caret]);
    requestAnimationFrame(() => { if (ta) { ta.focus(); ta.selectionStart = ta.selectionEnd = caret; } });
  };
  // Put `text` over [from, to] and select its first field. `stops` and
  // `caretAt` are offsets in `text`.
  const insertWithStops = (from, to, text, stops, caretAt) => {
    const ta = taRef.current;
    const nv = value.slice(0, from) + text + value.slice(to);
    const list = stops.map(([s0, e0]) => [from + s0, from + e0]);
    commit(nv, 'complete', ta ? [ta.selectionStart, ta.selectionEnd] : null, list[0]);
    setSes({ list, idx: 0, final: from + caretAt });
    requestAnimationFrame(() => { if (ta) { ta.focus(); ta.setSelectionRange(list[0][0], list[0][1]); } });
  };
  // Tab and Shift+Tab: the next or the previous field. Tab on the last field
  // puts the caret at the end and ends the fields.
  const stepStop = (ta, dir) => {
    const cur = sesRef.current;
    const j = cur.idx + dir;
    if (j >= cur.list.length) { setSes(null); ta.setSelectionRange(cur.final, cur.final); return; }
    const k = Math.max(0, j);
    setSes({ ...cur, idx: k });
    ta.setSelectionRange(cur.list[k][0], cur.list[k][1]);
  };

  // The column list at the caret (see qhColumnList). With `slot`, only while the
  // caret is still in that slot. With `ask`, it asks the server about the
  // relations the catalog does not list, then opens the list again. It returns
  // true when it handled the key.
  const showColumns = (ta, slot, ask) => {
    const caret = ta.selectionStart;
    const look = sch.columnLookup || null;
    const peek = look ? look.peek : null;
    let cl = qhColumnList(ta.value, caret, sch, engineId, peek);
    if (cl && cl.kind === 'lookup') {
      if (ask) {
        const v = ta.value;
        Promise.all(cl.names.map(n => look.fetch(n))).then(() => {
          const t = taRef.current;
          if (t && t.value === v && t.selectionStart === caret && t.selectionEnd === caret
              && document.activeElement === t && openRef.current) openRef.current(t, true);
        }, () => {});
        setAc(null);
        return true;
      }
      // Not asked again: a relation nobody knows lists nothing.
      cl = qhColumnList(ta.value, caret, sch, engineId, (n) => { const c = peek(n); return c === undefined ? null : c; });
    }
    if (!cl || cl.kind !== 'ready' || !cl.count) return false;
    if (slot != null && cl.slot !== slot) return false;
    const p = posAt(ta, cl.start);
    // The checked columns (Devart A13) stay while the caret stays in the slot,
    // also when typing filters them out of view.
    setAc(prev => ({ items: cl.items, idx: Math.max(0, cl.items.findIndex(it => !it.header)), top: p.top, lineTop: p.lineTop,
            left: p.left, start: cl.start, end: cl.end, cols: true, slot: cl.slot, qual: cl.qual,
            checked: (prev && prev.cols && prev.slot === cl.slot && prev.checked) ? prev.checked : [] }));
    return true;
  };
  // Check or uncheck a row of the column list. The order of the checks is the
  // order of the insert.
  const toggleCheck = (it) => {
    if (!it || it.header) return;
    setAc(a => {
      if (!a || !a.cols) return a;
      const on = (a.checked || []).some(c => c.key === it.key);
      return { ...a, checked: on ? a.checked.filter(c => c.key !== it.key) : (a.checked || []).concat({ key: it.key, text: it.text }) };
    });
  };
  // Ctrl+Space: the column list in a column position, else the normal list for
  // the caret, also with nothing typed.
  const openAC = (ta, afterLookup) => {
    if (ta.selectionStart !== ta.selectionEnd) { setAc(null); return; }
    // After `INSERT INTO t`, `UPDATE t`, `EXEC p` or `CALL p`: the template
    // (qhStatementTemplate), then the normal list.
    const tp = qhStatementTemplate(ta.value, ta.selectionStart, sch, engineId, sch.columnLookup ? sch.columnLookup.peek : null);
    if (tp) {
      const s = qhBuildSuggest(ta.value, ta.selectionStart, sch, engineId, { force: true });
      const p = posAt(ta, ta.selectionStart);
      setAc({ items: [qhTemplateItem(tp)].concat(s ? s.items : []), idx: 0, top: p.top, lineTop: p.lineTop,
              left: p.left, start: s ? s.start : ta.selectionStart, end: ta.selectionStart });
      return;
    }
    // After GROUP BY: the select list's columns with no aggregate, in one row
    // (qhGroupByFill), then the normal list. Ctrl+Space only, so Enter after
    // `GROUP BY ` still starts a new line.
    const gb = qhGroupByFill(ta.value, ta.selectionStart);
    if (gb) {
      const s = qhBuildSuggest(ta.value, ta.selectionStart, sch, engineId, { force: true });
      const p = posAt(ta, gb.start);
      setAc({ items: [qhGroupByItem(gb)].concat(s ? s.items : []), idx: 0, top: p.top, lineTop: p.lineTop,
              left: p.left, start: gb.start, end: gb.end });
      return;
    }
    if (showColumns(ta, null, !afterLookup)) return;
    refreshAC(ta, false, true);
  };
  openRef.current = openAC;

  // Ctrl+C / Ctrl+X with nothing selected act on the caret's whole line,
  // newline included — VS Code's behaviour, and the thing people miss most
  // when a textarea stands in for an editor (the browser's own answer is to
  // copy nothing at all).
  const lineClip = (e, cut) => {
    const ta = e.target, v = ta.value, caret = ta.selectionStart;
    const st = v.lastIndexOf('\n', caret - 1) + 1;
    let en = v.indexOf('\n', caret);
    const trailing = en >= 0;               // false only on the very last line
    if (!trailing) en = v.length;
    // Always hand over a newline-terminated string: that is what makes the
    // paste land as its own line, and it is what marks this as a line copy.
    const text = v.slice(st, en) + '\n';
    e.preventDefault();
    qhCopyText(text);
    qhLineClip = text;
    if (!cut) return;
    // Removing a line means removing its newline too, otherwise a blank line
    // is left behind. On the last line there is no trailing newline to take,
    // so take the PRECEDING one instead — same as VS Code.
    const rest = trailing ? v.slice(0, st) + v.slice(en + 1)
                          : v.slice(0, Math.max(0, st - 1));
    const col = caret - st;
    // Keep the column, clamped to whatever line now sits under the caret.
    const base = trailing ? Math.min(st, rest.length)
                          : rest.lastIndexOf('\n') + 1;
    let lineEnd = rest.indexOf('\n', base);
    if (lineEnd < 0) lineEnd = rest.length;
    const next = Math.min(base + col, lineEnd);
    setAc(null);
    commit(rest, 'cut-line', [caret, caret], [next, next]);
    requestAnimationFrame(() => { ta.selectionStart = ta.selectionEnd = next; });
  };

  // A line-shaped clipboard pastes ABOVE the caret's line and leaves the caret
  // on its own text, which has simply moved down a row.
  const onPaste = (e) => {
    const txt = e.clipboardData && e.clipboardData.getData('text/plain');
    const ta = e.target;
    if (!txt || txt !== qhLineClip) return;                  // not our line copy
    if (ta.selectionStart !== ta.selectionEnd) return;       // replacing a selection
    e.preventDefault();
    const v = ta.value, caret = ta.selectionStart;
    const st = v.lastIndexOf('\n', caret - 1) + 1;
    const next = caret + txt.length;
    setAc(null);
    commit(v.slice(0, st) + txt + v.slice(st), 'paste-line', [caret, caret], [next, next]);
    requestAnimationFrame(() => { ta.selectionStart = ta.selectionEnd = next; });
  };

  // Format SQL. The key (Shift+Alt+F) and the tab-bar button both come here. The
  // selection if there is one, else the whole text. One undo step. A missing
  // formatter, a failure or an unchanged text changes nothing. The textarea keeps
  // its selection when it loses focus, so a click on the button still formats
  // the part that you selected.
  const formatNow = () => {
    const ta = taRef.current;
    if (!ta) return;
    const s = ta.selectionStart, en = ta.selectionEnd, part = s !== en;
    const src = part ? value.slice(s, en) : value;
    const out = typeof qhFormatSql === 'function' ? qhFormatSql(src, engineId) : null;
    if (out == null || out === src) return;
    const next = part ? value.slice(0, s) + out + value.slice(en) : out;
    const end = part ? s + out.length : out.length;
    restoreSel.current = { text: next, sel: [end, end] };
    commit(next, 'format', [s, en], [end, end]);
  };

  // Add the missing semicolons (qhAddSemicolons, `Shift+Alt+;`): the selection
  // if there is one, else the whole text. One undo step. The caret keeps its
  // place in the text. A selection stays on the changed text.
  const addSemicolonsNow = () => {
    const ta = taRef.current;
    if (!ta) return;
    const s = ta.selectionStart, en = ta.selectionEnd, part = s !== en;
    const r = qhAddSemicolons(part ? value.slice(s, en) : value, engineId);
    if (!r) return;
    const next = part ? value.slice(0, s) + r.text + value.slice(en) : r.text;
    const c = s + r.at.filter(x => x <= s).length;
    const sel = part ? [s, s + r.text.length] : [c, c];
    restoreSel.current = { text: next, sel };
    commit(next, 'semicolons', [s, en], sel);
  };

  // Expand every select-list * (qhExpandAllStars, Shift+Alt+E): the selection
  // if there is one, else the whole text. A relation the catalog does not list
  // is asked about first. Nothing changes when the text changed while the
  // answer was on its way. One undo step.
  // Cmd/Ctrl+B or Cmd/Ctrl+click: go to the definition of the name at the
  // caret (qhDefinitionAt). F12 belongs to the browser.
  const goToDefinition = (ta) => {
    const d = qhDefinitionAt(ta.value, ta.selectionStart, engineId);
    if (!d) { if (onNotice) onNotice('Put the caret on an alias, a CTE or an object name.'); return; }
    if (d.kind === 'none') { if (onNotice) onNotice(d.message); return; }
    if (d.kind === 'text') { setAc(null); ta.focus(); ta.setSelectionRange(d.start, d.end); reveal(ta, d.start); return; }
    if (onRevealObject) onRevealObject(d.name);
  };
  // Shift+F6: open the rename box for the alias at the caret.
  const renameOpen = () => {
    const ta = taRef.current;
    if (!ta) return;
    const c = ta.selectionStart;
    const p = posAt(ta, c);
    const at = qhAliasAt(ta.value, c);
    if (!at || at.error) { setRen({ error: at ? at.error : 'Put the caret on a table alias.', top: p.top, left: p.left }); return; }
    setRen({ name: at.name, count: at.places.length, caret: c, value: at.name, error: null, top: p.top, left: p.left });
  };
  const renameClose = () => {
    setRen(null);
    const ta = taRef.current;
    if (ta) ta.focus();
  };
  // Enter in the rename box: one edit for every place, one undo step.
  const renameApply = () => {
    const ta = taRef.current;
    if (!ta || !ren || !ren.name) return;
    const r = qhRenameAlias(ta.value, ren.caret, ren.value, engineId);
    if (r.error) { setRen(x => ({ ...x, error: r.error })); return; }
    const a = ta.selectionStart, b = ta.selectionEnd;
    const out = qhApplyEdits(ta.value, r.edits, ren.caret);
    const sel = [out.caret, out.caret];
    restoreSel.current = { text: out.text, sel };
    setRen(null);
    commit(out.text, 'rename', [a, b], sel);
    ta.focus();
  };
  // Shift+Alt+Q: qualify the names of the selection, or of the whole text.
  const qualifyNow = () => {
    const ta = taRef.current;
    if (!ta) return;
    const v0 = ta.value, a = ta.selectionStart, b = ta.selectionEnd;
    const range = a !== b ? [a, b] : null;
    const r = qhQualifyNames(v0, sch, engineId, range);
    const left = r.twice.length ? ' ' + (r.twice.length === 1 ? r.twice[0] + ' is' : r.twice.join(', ') + ' are') + ' in two tables: left as typed.' : '';
    if (!r.edits.length) { if (onNotice) onNotice('No name to qualify.' + left); return; }
    const out = qhApplyEdits(v0, r.edits, b);
    const sel = range ? [a, out.caret] : [out.caret, out.caret];
    restoreSel.current = { text: out.text, sel };
    commit(out.text, 'qualify', [a, b], sel);
    const what = [r.tables ? r.tables + (r.tables === 1 ? ' table' : ' tables') : '', r.columns ? r.columns + (r.columns === 1 ? ' column' : ' columns') : ''].filter(Boolean).join(' and ');
    if (onNotice) onNotice('Qualified ' + what + '.' + left);
  };

  const expandAllNow = async () => {
    const ta = taRef.current;
    if (!ta) return;
    const look = sch.columnLookup || null;
    const v0 = ta.value, a = ta.selectionStart, b = ta.selectionEnd;
    const range = a !== b ? [a, b] : null;
    let r = qhExpandAllStars(v0, sch, engineId, look ? look.peek : null, range);
    if (r.names.length && look) {
      await Promise.all(r.names.map(n => Promise.resolve(look.fetch(n)).catch(() => null)));
      const t = taRef.current;
      if (!t || t.value !== v0) return;
      r = qhExpandAllStars(v0, sch, engineId, (n) => { const c = look.peek(n); return c === undefined ? null : c; }, range);
    }
    if (!r.edits.length) return;
    const out = qhApplyEdits(v0, r.edits, b);
    const sel = range ? [a, out.caret] : [out.caret, out.caret];
    restoreSel.current = { text: out.text, sel };
    commit(out.text, 'expand-all', [a, b], sel);
  };

  // Scroll the textarea so that offset `pos` is in view. A textarea follows a
  // caret that a key moved, not one that code moved. 14 and 16 are the
  // textarea's padding.
  const reveal = (ta, pos) => {
    const rows = ta.value.slice(0, pos).split('\n');
    const row = rows.length - 1, col = rows[row].length;
    let bottom = (row + 1) * lh;
    if (wrap) { const p = wrapRect(row, col); if (p) bottom = p.bottom; }
    const top = bottom - lh;
    if (top < ta.scrollTop) ta.scrollTop = Math.max(0, top);
    else if (bottom + 28 > ta.scrollTop + ta.clientHeight) ta.scrollTop = bottom + 28 - ta.clientHeight;
    if (!wrap) {
      const x = col * charW;
      if (x < ta.scrollLeft) ta.scrollLeft = Math.max(0, x - 16);
      else if (x + 32 > ta.scrollLeft + ta.clientWidth) ta.scrollLeft = x + 64 - ta.clientWidth;
    }
    sync();
  };
  const jumpTo = (ta, pos) => {
    setAc(null);
    ta.setSelectionRange(pos, pos);
    reveal(ta, pos);
  };

  const onKey = (e) => {
    // Any key hides the hover card, Escape too. The key still does its own work.
    if (hover || hoverTimer.current) hideHover();
    // Undo / redo. The letter is read from e.key, the one printed on the
    // user's layout; e.code only when e.key is not a Latin letter, so a
    // Cyrillic or Greek layout still undoes. Ctrl+Y is left alone on a Mac,
    // where ⌘Y is the browser's History and Ctrl+Y a text-field yank.
    if ((e.metaKey || e.ctrlKey) && !e.altKey) {
      const k = /^[a-z]$/i.test(e.key) ? e.key.toLowerCase()
        : e.code === 'KeyZ' ? 'z' : e.code === 'KeyY' ? 'y' : '';
      if (k === 'z') { e.preventDefault(); step(e.shiftKey ? 1 : -1); return; }
      if (k === 'y' && e.ctrlKey && !e.metaKey && !e.shiftKey && !QH_EDITOR_MAC) { e.preventDefault(); step(1); return; }
    }
    if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey
        && (e.key === 'c' || e.key === 'C' || e.key === 'x' || e.key === 'X')
        && e.target.selectionStart === e.target.selectionEnd) {
      lineClip(e, e.key === 'x' || e.key === 'X');
      return;
    }
    // Format SQL: Shift+Alt+F. e.code, not e.key: Alt changes e.key on a Mac.
    // The selection if there is one, else the whole text. One undo step. A
    // missing formatter, a failure or an unchanged text changes nothing.
    if (e.shiftKey && e.altKey && !e.ctrlKey && !e.metaKey && e.code === 'KeyF') {
      e.preventDefault(); setAc(null);
      formatNow();
      return;
    }
    // Rename the alias at the caret: Shift+F6. F2 renames the tab.
    if (e.shiftKey && !e.altKey && !e.ctrlKey && !e.metaKey && e.key === 'F6') {
      e.preventDefault(); setAc(null);
      renameOpen();
      return;
    }
    // Go to definition: Cmd/Ctrl+B.
    if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && e.code === 'KeyB') {
      e.preventDefault();
      goToDefinition(e.target);
      return;
    }
    // The CREATE script of the object at the caret, in a new tab: Shift+Alt+D.
    if (e.shiftKey && e.altKey && !e.ctrlKey && !e.metaKey && e.code === 'KeyD') {
      e.preventDefault(); setAc(null);
      const nm = qhObjectNameAt(e.target.value, e.target.selectionStart, engineId);
      if (!nm) { if (onNotice) onNotice('Put the caret on a table, view, function or procedure name.'); }
      else if (onScriptCreate) onScriptCreate(nm);
      return;
    }
    // Qualify the table and column names: Shift+Alt+Q (e.code, for the Mac).
    if (e.shiftKey && e.altKey && !e.ctrlKey && !e.metaKey && e.code === 'KeyQ') {
      e.preventDefault(); setAc(null);
      qualifyNow();
      return;
    }
    // Expand every select-list * of the text, or of the selection: Shift+Alt+E.
    if (e.shiftKey && e.altKey && !e.ctrlKey && !e.metaKey && e.code === 'KeyE') {
      e.preventDefault(); setAc(null);
      expandAllNow();
      return;
    }
    // Add the missing semicolons: `Shift+Alt+;` (e.code, for the Mac reason above).
    if (e.shiftKey && e.altKey && !e.ctrlKey && !e.metaKey && e.code === 'Semicolon') {
      e.preventDefault(); setAc(null);
      addSemicolonsNow();
      return;
    }
    // ( ' " [ or ` over a selection wraps it (qhWrapSelection). AltGr, which
    // types [ on many layouts, reports Ctrl and Alt together, so only Ctrl
    // without Alt and Cmd are left out.
    if (!e.metaKey && !(e.ctrlKey && !e.altKey) && !e.isComposing) {
      const ta = e.target, a = ta.selectionStart, b = ta.selectionEnd;
      const r = a !== b && !fieldSel(ta) ? qhWrapSelection(value, a, b, e.key) : null;
      if (r) {
        e.preventDefault(); setAc(null);
        restoreSel.current = { text: r.value, sel: r.sel };
        commit(r.value, 'wrap', [a, b], r.sel);
        return;
      }
    }
    // Alt+Up / Alt+Down move the caret's line, or the lines of the selection
    // (qhMoveLines). The selection moves with the text. One undo step each.
    if (e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
      e.preventDefault(); setAc(null);
      const ta = e.target, a = ta.selectionStart, b = ta.selectionEnd;
      const r = qhMoveLines(value, a, b, e.key === 'ArrowUp' ? -1 : 1);
      if (!r) return;
      restoreSel.current = { text: r.value, sel: r.sel };
      commit(r.value, 'move-line', [a, b], r.sel);
      requestAnimationFrame(() => { const t = taRef.current; if (t) reveal(t, r.sel[1]); });
      return;
    }
    // Alt+PageUp / Alt+PageDown: the start of the previous or the next
    // statement (qhStatementJump). Statements without `;` count too.
    if (e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey && (e.key === 'PageUp' || e.key === 'PageDown')) {
      e.preventDefault();
      const ta = e.target, up = e.key === 'PageUp';
      const pos = qhStatementJump(ta.value, up ? ta.selectionStart : ta.selectionEnd, up ? -1 : 1, engineId);
      if (pos != null) jumpTo(ta, pos);
      return;
    }
    // Shift+F12, or Ctrl/Cmd+Shift+\: the mate of the bracket, CASE, BEGIN or
    // END at the caret, else the end of the pair around it (qhPairJump).
    // e.code for the backslash: its character differs between layouts.
    if ((e.key === 'F12' && e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey)
        || ((e.ctrlKey || e.metaKey) && e.shiftKey && !e.altKey && e.code === 'Backslash')) {
      e.preventDefault();
      const ta = e.target, pos = qhPairJump(ta.value, ta.selectionStart);
      if (pos != null) jumpTo(ta, pos);
      return;
    }
    if (e.key === 'F5') { e.preventDefault(); setAc(null); onRun && onRun(); return; }
    // Select the statement at the caret: Shift+Alt+S. It runs nothing. e.code,
    // not e.key, for the same Mac reason as Format SQL.
    if (e.shiftKey && e.altKey && !e.ctrlKey && !e.metaKey && e.code === 'KeyS') {
      e.preventDefault(); setAc(null);
      const ta = e.target, r = qhStatementAt(ta.value, ta.selectionStart);
      if (r) ta.setSelectionRange(r.start, r.end);
      return;
    }
    // Shift+F8 runs from the top of the text to the end of the statement at
    // the caret (qhToCaretRange). It selects that part first, as F8 does, so
    // the person sees what runs. With a selection, the end of the selection
    // is the caret.
    if (e.key === 'F8' && e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault(); setAc(null);
      const ta = e.target, r = qhToCaretRange(ta.value, ta.selectionEnd, engineId);
      if (!r) return;
      ta.setSelectionRange(r.start, r.end);
      onRunSelection && onRunSelection(ta.value.slice(r.start, r.end));
      return;
    }
    // F8 runs the selection. With no selection it runs the statement at the
    // caret (see qhStatementAt) and selects it first, so the person sees what
    // runs. A caret in a part with no SQL does nothing.
    if (e.key === 'F8') {
      e.preventDefault(); setAc(null);
      const ta = e.target, s = fieldSel(ta) ? '' : ta.value.slice(ta.selectionStart, ta.selectionEnd);
      if (s.trim()) { onRunSelection && onRunSelection(s); return; }
      const r = qhStatementAt(ta.value, ta.selectionStart);
      if (!r) return;
      ta.setSelectionRange(r.start, r.end);
      onRunSelection && onRunSelection(ta.value.slice(r.start, r.end));
      return;
    }
    // Ctrl+Shift+Space shows the parameter info of the call at the caret.
    if (e.ctrlKey && e.shiftKey && !e.metaKey && !e.altKey && (e.key === ' ' || e.code === 'Space')) {
      e.preventDefault();
      updateSig(e.target, true);
      return;
    }
    // Ctrl+Space opens the list at the caret. Ctrl on every OS, the Mac too.
    // In an open column list it checks or unchecks the highlighted row. A
    // plain Space still types a space.
    if (e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey && (e.key === ' ' || e.code === 'Space')) {
      e.preventDefault();
      if (ac && ac.cols && ac.items[ac.idx] && !ac.items[ac.idx].header) { toggleCheck(ac.items[ac.idx]); return; }
      openAC(e.target);
      return;
    }
    if (ac && ac.items.length) {
      // Group headers of the column list are skipped.
      if (e.key === 'ArrowDown') { e.preventDefault(); setAc(a => ({ ...a, idx: qhAcStep(a.items, a.idx, 1) })); return; }
      if (e.key === 'ArrowUp') { e.preventDefault(); setAc(a => ({ ...a, idx: qhAcStep(a.items, a.idx, -1) })); return; }
      if ((e.key === 'PageDown' || e.key === 'PageUp') && !e.altKey) {
        e.preventDefault();
        setAc(a => ({ ...a, idx: qhAcPage(a.items, a.idx, e.key === 'PageDown' ? 1 : -1, QH_AC_PAGE) }));
        return;
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        const it = ac.items[ac.idx];
        // Exact matches are listed so you can see the object exists, but
        // "completing" one replaces the token with itself. Don't swallow the
        // keystroke for a no-op: close the popup and let Enter make a newline
        // / Tab indent, as it would with no popup open.
        const itFrom = (it && typeof it.start === 'number') ? it.start : ac.start;
        if ((ac.checked && ac.checked.length) || !(it && value.slice(itFrom, ac.end) === it.text)) {
          e.preventDefault(); accept(it); return;
        }
        setAc(null);
      }
      if (e.key === 'Escape') { e.preventDefault(); setAc(null); return; }
    }
    // The parameter info box: Escape closes it. With more than one signature,
    // Up and Down pick the signature, as in VS Code. With one, they move the caret.
    if (sig) {
      if (e.key === 'Escape') { e.preventDefault(); setSig(null); sigArm.current = null; return; }
      if (sig.list.length > 1 && (e.key === 'ArrowUp' || e.key === 'ArrowDown')
          && !e.altKey && !e.shiftKey && !e.metaKey && !e.ctrlKey) {
        e.preventDefault(); stepSig(e.key === 'ArrowDown' ? 1 : -1); return;
      }
    }
    if (e.key === 'Tab' && !e.shiftKey && !e.altKey && !e.metaKey && !e.ctrlKey
        && e.target.selectionStart === e.target.selectionEnd) {
      const tp = qhStatementTemplate(e.target.value, e.target.selectionStart, sch, engineId, sch.columnLookup ? sch.columnLookup.peek : null);
      if (tp) { e.preventDefault(); setAc(null); insertWithStops(tp.start, tp.end, tp.text, tp.stops, tp.caretAt); return; }
    }
    if (e.key === 'Tab' && sesRef.current && !e.altKey && !e.metaKey && !e.ctrlKey) {
      e.preventDefault();
      setAc(null);
      stepStop(e.target, e.shiftKey ? -1 : 1);
      return;
    }
    if (e.key === 'Escape' && sesRef.current) { setSes(null); return; }
    if (e.key === 'Tab') {
      e.preventDefault();
      const ta = e.target, s = ta.selectionStart, en = ta.selectionEnd;
      commit(value.slice(0, s) + '  ' + value.slice(en), 'indent', [s, en], [s + 2, s + 2]);
      requestAnimationFrame(() => { ta.selectionStart = ta.selectionEnd = s + 2; });
    } else if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault(); setAc(null); onRun && onRun();
    }
  };

  // Fit the portalled menu to the viewport, before paint. No dependency array:
  // every render re-applies the JSX anchor (`top`/`left` from `ac`), so the
  // correction has to run just as often — an idx change would otherwise repaint
  // the list at the uncorrected anchor.
  React.useLayoutEffect(() => {
    const el = acRef.current;
    if (!el || !ac || !ac.items.length) return;
    const h = el.offsetHeight, w = el.offsetWidth;
    let top = ac.top, left = ac.left;
    // Open upwards when the list would run off the bottom — a short editor pane
    // over a tall results panel never has room below the last line.
    if (top + h > window.innerHeight - 8) {
      const up = ac.lineTop - h - 4;
      top = up >= 8 ? up : Math.max(8, window.innerHeight - 8 - h);
    }
    left = Math.max(8, Math.min(left, window.innerWidth - w - 8));
    el.style.top = top + 'px';
    el.style.left = left + 'px';
    // Keep the picked row in view, with its group header when it is the first row.
    const hi = el.querySelector('.qh-ac-ed-opt.is-hi');
    if (hi) {
      const lead = hi.previousElementSibling;
      const hiTop = (lead && lead.classList.contains('qh-ac-ed-group') ? lead : hi).offsetTop;
      const hiBot = hi.offsetTop + hi.offsetHeight;
      if (hiTop < el.scrollTop) el.scrollTop = hiTop;
      else if (hiBot > el.scrollTop + el.clientHeight) el.scrollTop = hiBot - el.clientHeight;
    }
  });

  // `fixed` detaches from the caret as soon as anything scrolls, so close.
  // Capture phase to catch scrolling ancestors too; the list's own wheel
  // scrolling and the textarea's (already handled by sync) are exempt.
  React.useEffect(() => {
    if (!ac) return;
    const close = (e) => {
      // A resize event's target is `window`, which is not a Node — the guard has
      // to be on the ARGUMENT, or `contains` throws and the handler never gets
      // to the close it exists for.
      const t = e && e.target;
      if (t && t.nodeType === 1 && acRef.current && acRef.current.contains(t)) return;
      if (t === taRef.current) return;
      setAc(null);
    };
    window.addEventListener('scroll', close, true);
    window.addEventListener('resize', close);
    return () => { window.removeEventListener('scroll', close, true); window.removeEventListener('resize', close); };
  }, [!!ac]);

  // The parameter info box sits above the caret line, so it does not cover the
  // typing list below that line. With no room above, it goes below the line.
  React.useLayoutEffect(() => {
    const el = sigRef.current;
    if (!el || !sig) return;
    const h = el.offsetHeight, w = el.offsetWidth;
    let top = sig.top - h - 4;
    if (top < 8) top = sig.below + 2;
    el.style.top = top + 'px';
    el.style.left = Math.max(8, Math.min(sig.left, window.innerWidth - w - 8)) + 'px';
  });
  // A page scroll or a resize moves the caret away from the box: close it.
  React.useEffect(() => {
    if (!sig) return;
    const close = (e) => {
      const t = e && e.target;
      if (t === taRef.current) return;
      setSig(null);
    };
    window.addEventListener('scroll', close, true);
    window.addEventListener('resize', close);
    return () => { window.removeEventListener('scroll', close, true); window.removeEventListener('resize', close); };
  }, [!!sig]);

  const issueList = Array.isArray(issues) ? issues : [];
  const issuesOn = issuesShown && !ac && issueList.length > 0;
  const issueHtml = React.useMemo(
    () => (issueList.length ? qhMarkHtml(value, issueList, () => 'qh-issue') : ''), [value, issues]);
  // Code-analysis findings (qhLint): the same layer, with a softer line. It
  // hides while the person types, as the blocker line does.
  const lintList = Array.isArray(lints) ? lints : [];
  const lintsOn = issuesShown && !ac && lintList.length > 0;
  const lintHtml = React.useMemo(
    () => (lintList.length ? qhMarkHtml(value, lintList, r => 'qh-lint lv-' + r.level) : ''), [value, lints]);
  const srText = issuesOn ? issueList.map(r => r.message).join(' ') : '';
  // The overlay mounts only while it shows. It must start at the textarea's
  // scroll position, or the line sits under the wrong word.
  React.useLayoutEffect(() => {
    const ta = taRef.current, el = issueRef.current;
    if (ta && el) { el.scrollTop = ta.scrollTop; el.scrollLeft = ta.scrollLeft; }
  }, [issuesOn, issueHtml]);
  React.useLayoutEffect(() => {
    const ta = taRef.current, el = lintRef.current;
    if (ta && el) { el.scrollTop = ta.scrollTop; el.scrollLeft = ta.scrollLeft; }
  }, [lintsOn, lintHtml]);

  // The balloon is a hover tooltip, not a menu: it goes when the pointer leaves
  // the text (see tests/test_dropdown_dismiss.py for the menu rule).
  const hideTip = () => setTip(null);
  // Find the marked word under the pointer. The textarea sits on top of the
  // overlay, so this test uses the rectangles of the marks.
  // A blocker wins over a code-analysis finding on the same word.
  const onIssueMove = (e) => {
    const layers = [[issueRef.current, 'mark.qh-issue', issueList, 'issue'], [lintRef.current, 'mark.qh-lint', lintList, 'lint']];
    for (const [host, sel, list, kind] of layers) {
      if (!host) continue;
      const marks = host.querySelectorAll(sel);
      for (let n = 0; n < marks.length; n++) {
        const rs = marks[n].getClientRects();
        for (let j = 0; j < rs.length; j++) {
          const r = rs[j];
          if (e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom) {
            const i = Number(marks[n].getAttribute('data-i'));
            if (!tip || tip.i !== i || tip.kind !== kind) setTip({ i, kind, lv: list[i].level, msg: list[i].message, rect: { top: rs[0].top, bottom: rs[0].bottom, left: rs[0].left } });
            return;
          }
        }
      }
    }
    if (tip) setTip(null);
  };

  // The balloon is `fixed`, so it closes on scroll and resize like the list.
  React.useEffect(() => {
    if (!tip) return undefined;
    const close = () => setTip(null);
    window.addEventListener('scroll', close, true);
    window.addEventListener('resize', close);
    return () => { window.removeEventListener('scroll', close, true); window.removeEventListener('resize', close); };
  }, [!!tip]);

  // Place the balloon above the word with a 6px gap, or below it when there is
  // no room above. Clamp it to the viewport with an 8px margin.
  React.useLayoutEffect(() => {
    const el = tipRef.current;
    if (!tip || !el) return;
    const w = el.offsetWidth, h = el.offsetHeight;
    let top = tip.rect.top - h - 6;
    if (top < 8) top = tip.rect.bottom + 6;
    top = Math.max(8, Math.min(top, window.innerHeight - 8 - h));
    const left = Math.max(8, Math.min(tip.rect.left, window.innerWidth - w - 8));
    el.style.top = top + 'px';
    el.style.left = left + 'px';
  });

  // Quick info on hover. The pointer must rest on a name for 400 ms. A drag
  // shows nothing. A blocker balloon and the completion list win over the card.
  hoverLive.current = { tip, ac, value, schema, engineId };
  const onHoverMove = (e) => {
    const x = e.clientX, y = e.clientY;
    if (hover) {
      const r = hover.rect;                               // the card stays while the pointer is on its word
      if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) return;
      setHover(null);
    }
    clearTimeout(hoverTimer.current);
    hoverTimer.current = null;
    if (e.buttons) return;
    hoverTimer.current = setTimeout(() => {
      hoverTimer.current = null;
      const live = hoverLive.current;
      if (live.tip || (live.ac && live.ac.items && live.ac.items.length)) return;
      const code = preRef.current && preRef.current.querySelector('code');
      const hit = qhHoverOffsetAt(code, String(live.value || ''), x, y);
      if (!hit) return;
      const info = qhHoverInfo(live.value, hit.offset, live.schema, live.engineId);
      if (info) setHover({ info, lines: qhHoverLines(info), rect: hit.rect });
    }, 400);
  };

  // The card is `fixed`, so it closes on scroll and resize like the balloon.
  React.useEffect(() => {
    if (!hover) return undefined;
    const close = () => setHover(null);
    window.addEventListener('scroll', close, true);
    window.addEventListener('resize', close);
    return () => { window.removeEventListener('scroll', close, true); window.removeEventListener('resize', close); };
  }, [!!hover]);

  // The same placement as the balloon: above the word, else below it.
  React.useLayoutEffect(() => {
    const el = hoverRef.current;
    if (!hover || !el) return;
    const w = el.offsetWidth, h = el.offsetHeight;
    let top = hover.rect.top - h - 6;
    if (top < 8) top = hover.rect.bottom + 6;
    top = Math.max(8, Math.min(top, window.innerHeight - 8 - h));
    const left = Math.max(8, Math.min(hover.rect.left, window.innerWidth - w - 8));
    el.style.top = top + 'px';
    el.style.left = left + 'px';
  });

  return (
    <div className={'qh-editor' + (wrap ? ' is-wrap' : '')} style={{ '--ed-fs': fontSize + 'px', '--ed-lh': lh + 'px' }}>
      <span ref={measRef} className="qh-meas" aria-hidden="true">0000000000</span>
      <div className="qh-gutter" ref={gutRef}>
        {lines.map((_, i) => (
          <div key={i} className="qh-lno"
            style={(rowH && rowH.length === lines.length) ? { height: rowH[i] + 'px' } : undefined}>{i + 1}</div>
        ))}
      </div>
      <div className={'qh-code-wrap' + (dragOver ? ' is-drop' : '')} ref={hostRef}>
        {wrap && <div className="qh-wrapmeas" ref={wrapRef} aria-hidden="true"></div>}
        {/* Before the highlight layer, so the text draws on top of the tint.
            It stays while the typing list is open: a click on a name opens
            that list, and the marks are what the click was for. */}
        <QhCaretMarks taRef={taRef} value={value} show={issuesShown} />
        {ses && ses.list.length > 0 && (
          // The fields of a snippet or a template. The field with the caret
          // has a fill, the others a frame.
          <pre className="qh-pre qh-marks qh-stops" ref={(el) => { stopRef.current = el; const ta = taRef.current; if (el && ta) { el.scrollTop = ta.scrollTop; el.scrollLeft = ta.scrollLeft; } }} aria-hidden="true">
            <code dangerouslySetInnerHTML={{ __html: qhMarkHtml(value, ses.list.map(([a, b], i) => ({ start: a, end: b, cur: i === ses.idx })),
              r => 'qh-stop' + (r.cur ? ' is-cur' : '')) }} />
          </pre>
        )}
        <pre className="qh-pre" ref={preRef} aria-hidden="true">
          <code dangerouslySetInnerHTML={{ __html: qhHighlight(value) + '\n' }} />
        </pre>
        {lintsOn && lintHtml && (
          <pre className="qh-pre qh-lints" ref={lintRef} aria-hidden="true">
            <code dangerouslySetInnerHTML={{ __html: lintHtml }} />
          </pre>
        )}
        {issuesOn && issueHtml && (
          <pre className="qh-pre qh-issues" ref={issueRef} aria-hidden="true">
            <code dangerouslySetInnerHTML={{ __html: issueHtml }} />
          </pre>
        )}
        <textarea
          ref={taRef}
          className="qh-ta"
          onMouseMove={(e) => { onIssueMove(e); onHoverMove(e); }}
          onMouseLeave={() => { hideTip(); hideHover(); }}
          onMouseDown={hideHover}
          value={value}
          placeholder={QH_EDITOR_HINT}
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          onChange={(e) => {
            const ta = e.target, type = e.nativeEvent && e.nativeEvent.inputType;
            // Undo and redo are ours. A browser that ran its own anyway (its
            // beforeinput could not be cancelled) is overruled: React puts the
            // controlled value back, and the step comes from our history.
            if (type === 'historyUndo' || type === 'historyRedo') {
              if (!histInput.current) step(type === 'historyUndo' ? -1 : 1);
              histInput.current = false;
              return;
            }
            const before = pendSel.current;
            pendSel.current = null;
            commit(ta.value, qhEditKind(type), before, [ta.selectionStart, ta.selectionEnd]);
            refreshAC(ta);
            const typed = e.nativeEvent && e.nativeEvent.data;
            updateSig(ta, type === 'insertText' && (typed === '(' || typed === ','));
          }}
          onScroll={() => { sync(); if (hover || hoverTimer.current) hideHover(); }}
          onKeyDown={onKey}
          onPaste={onPaste}
          onKeyUp={(e) => {
            if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) refreshAC(e.target);
            if (sig && ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'].includes(e.key)
                && !(sig.list.length > 1 && (e.key === 'ArrowUp' || e.key === 'ArrowDown'))) updateSig(e.target, false);
          }}
          onClick={(e) => {
            // Cmd/Ctrl+click: go to definition of the name under the pointer.
            if ((e.metaKey || e.ctrlKey) && e.target.selectionStart === e.target.selectionEnd) { goToDefinition(e.target); return; }
            const cs = sesRef.current, c = e.target.selectionStart;
            if (cs && !cs.list.some(([a, b]) => c >= a && c <= b)) setSes(null);
            refreshAC(e.target); updateSig(e.target, false);
          }}
          onBlur={() => setTimeout(() => { setAc(null); setSig(null); }, 150)}
          onDragOver={onDragOver}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
        />
        {ac && ac.items.length > 0 && ReactDOM.createPortal(
          <div className={'qh-ac-ed' + (ac.cols ? ' is-cols' : '')} ref={acRef}
            style={{ top: ac.top, left: ac.left, ...(acSize ? { width: acSize.w, maxWidth: 'none', maxHeight: acSize.h } : {}) }}>
            {ac.items.map((it, i) => (it.header ? (
              // A group header of the column list. It cannot be picked.
              <div key={it.key} className="qh-ac-ed-group" aria-disabled="true"
                onMouseDown={(e) => e.preventDefault()}>
                <span className="qh-ac-ed-gname">{it.label}</span>
                {it.alias ? <span className="qh-ac-ed-galias">{it.alias}</span> : null}
              </div>
            ) : (
              <div key={it.key || (it.type + it.text)} className={'qh-ac-ed-opt' + (i === ac.idx ? ' is-hi' : '')}
                title={it.type === 'snippet' ? it.text : undefined}
                onMouseEnter={() => setAc(a => ({ ...a, idx: i }))}
                onMouseDown={(e) => { e.preventDefault(); accept(it); }}>
                {ac.cols ? (() => {
                  // The check box (Devart A13). It shows the place of the
                  // column in the insert. A click on it keeps the list open.
                  const n = (ac.checked || []).findIndex(c => c.key === it.key) + 1;
                  return (
                    <span className={'qh-ac-ed-chk' + (n ? ' is-on' : '')} role="checkbox" aria-checked={n ? 'true' : 'false'}
                      title={n ? 'Column ' + n + ' of the insert. Click to remove it' : 'Click to add this column to a multiple insert'}
                      onMouseDown={(e) => { e.preventDefault(); e.stopPropagation(); toggleCheck(it); }}>{n || ''}</span>
                  );
                })() : null}
                <span className="qh-ac-ed-name">{it.label}{it.desc ? <span className="qh-ac-ed-desc">{it.desc}</span> : null}{it.pii ? (
                  // The lock of the tree's PII column (TICN.col in qh-panels.jsx).
                  <span className="qh-ac-ed-pii" role="img" aria-label="PII"
                    title={ac.cols ? 'PII: masked in results' : 'PII in at least one table'}>
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><rect x="4" y="10" width="16" height="10" rx="2"/><path d="M8 10V7a4 4 0 018 0v3"/></svg>
                  </span>) : null}</span>
                {it.fk ? (
                  // A JOIN suggestion that a foreign key gives (qhJoinSuggestions).
                  // It sits outside the name, which is cut short on a long row.
                  <span className="qh-ac-ed-fk" title="Foreign key">FK</span>) : null}
                {ac.cols ? (
                  <React.Fragment>
                    <span className="qh-ac-ed-pk">{it.pk ? 'PK' : ''}</span>
                    <span className="qh-ac-ed-dtype">{it.colType}</span>
                    <span className="qh-ac-ed-null">{it.nullable === true ? 'NULL' : it.nullable === false ? 'NOT NULL' : ''}</span>
                  </React.Fragment>
                ) : <span className={'qh-ac-ed-type ac-t-' + it.type}>{QH_AC_TYPE_LABEL[it.type]}</span>}
              </div>
            )))}
            {ac.cols ? (
              <div className="qh-ac-ed-foot" aria-live="polite" onMouseDown={(e) => e.preventDefault()}>
                {ac.checked && ac.checked.length
                  ? 'Enter: insert ' + ac.checked.length + (ac.checked.length === 1 ? ' column' : ' columns')
                  : 'Ctrl + Space or a click on the box: check a column'}
              </div>
            ) : null}
            {/* The corner grip: drag it to size the list. It keeps the focus
                in the editor, as a row does. */}
            <div className="qh-ac-grip" aria-hidden="true" title="Drag to resize the list" onMouseDown={startAcResize} />
          </div>, document.body)}
        {ren && ReactDOM.createPortal(
          // The alias rename box. Enter renames, Escape or a click outside closes.
          <div className="qh-rename" role="dialog" aria-label="Rename alias" style={{ top: ren.top + 4, left: ren.left }}>
            {ren.name ? (
              <>
                <label className="qh-rename-label">
                  <span>{'Rename alias ' + ren.name + ' · ' + ren.count + (ren.count === 1 ? ' place' : ' places')}</span>
                  <input autoFocus value={ren.value} spellCheck={false} aria-label="New alias"
                    onFocus={(e) => e.target.select()}
                    onChange={(e) => { const v = e.target.value; setRen(x => ({ ...x, value: v, error: null })); }}
                    onBlur={() => setRen(null)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') { e.preventDefault(); renameApply(); }
                      else if (e.key === 'Escape') { e.preventDefault(); renameClose(); }
                    }} />
                </label>
                {ren.error && <div className="qh-rename-err" role="alert">{ren.error}</div>}
              </>
            ) : <div className="qh-rename-err" role="alert">{ren.error}</div>}
          </div>, document.body)}
        {sig && sig.list[sig.idx] && ReactDOM.createPortal(
          // Parameter info. A mouse press keeps the focus in the editor.
          <div className="qh-sig" role="tooltip" ref={sigRef} style={{ top: sig.top, left: sig.left }}
            onMouseDown={(e) => e.preventDefault()}>
            {sig.list.length > 1 && (
              <span className="qh-sig-nav">
                <button type="button" tabIndex={-1} aria-label="Previous signature" onClick={() => stepSig(-1)}>‹</button>
                <span className="qh-sig-pos">{(sig.idx + 1) + ' of ' + sig.list.length}</span>
                <button type="button" tabIndex={-1} aria-label="Next signature" onClick={() => stepSig(1)}>›</button>
              </span>
            )}
            {(() => {
              const sg = sig.list[sig.idx];
              const on = qhSigActive(sg, sig.arg);
              return (
                <span className="qh-sig-text">
                  <span className="qh-sig-name">{sg.name}</span>{sig.exec ? ' ' : '('}
                  {sg.params.map((p, i) => (
                    <React.Fragment key={i}>{i ? ', ' : ''}<span className={i === on ? 'qh-sig-cur' : undefined}>{p}</span></React.Fragment>
                  ))}
                  {sig.exec ? '' : ')'}
                  {sg.ret ? <span className="qh-sig-ret">{' → ' + sg.ret}</span> : null}
                  {sg.src === 'catalog' ? <span className="qh-sig-kind">{sg.kind}</span> : null}
                </span>
              );
            })()}
          </div>, document.body)}
        {tip && ReactDOM.createPortal(
          <div className={'qh-issue-tip' + (tip.kind === 'lint' ? ' is-lint lv-' + tip.lv : '')} role="tooltip" ref={tipRef} style={{ top: tip.rect.top, left: tip.rect.left }}>{tip.msg}</div>,
          document.body)}
        {hover && !tip && ReactDOM.createPortal(
          // No tabIndex and no pointer events: the card never takes focus.
          <div className="qh-hover-card" role="tooltip" ref={hoverRef} style={{ top: hover.rect.top, left: hover.rect.left }}>
            {hover.lines.map((l, i) => <div key={i} className={i === 0 ? 'qh-hover-head' : undefined}>{l}</div>)}
          </div>,
          document.body)}
      </div>
      <div className="qh-sr-only" role="status" aria-live="polite">{srText}</div>
    </div>
  );
}

const QH_TAB_ICN = {
  rename: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 013 3L7 19l-4 1 1-4 12.5-12.5z"/></svg>,
  dup: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>,
  copy: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="8" y="2" width="8" height="4" rx="1"/><path d="M16 4h2a2 2 0 012 2v14a2 2 0 01-2 2H6a2 2 0 01-2-2V6a2 2 0 012-2h2"/></svg>,
  download: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>,
  x: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>,
  right: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h13M13 6l6 6-6 6"/></svg>,
  all: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 9l6 6M15 9l-6 6"/></svg>,
};

// A PROD tab has a thin orange top line (2026-11-10). The words are those of the
// PROD env chip (qhEnvChipWord). qh-app.jsx puts the connection's `env` on each
// tab of the strip. STG, TEST, an unknown env, a special page and a tab with no
// target get no colour.
function qhTabIsProd(t) {
  if (!t || t.kind || qhTabNoTarget(t)) return false;
  return qhEnvChipWord(t.env) === 'PROD';
}

// The Format options popup (2026-11-11, Devart E3). The popup stores each
// change at once (qhFmtOptionsSave), and the next Format uses it. The preview formats a
// fixed sample with the engine of the open tab. "Format now" formats the tab,
// as the Format button does.
// The Profile row (Devart E2) sets every option at once (QH_FMT_PROFILES). It
// shows "Custom" when the options match no profile.
// An option label has at most 20 characters. A select shows about 133 px of
// text in macOS Chrome, and "Up to 120 characters" (20) is 130 px there.
const QH_FMT_CASE = [['preserve', 'As typed'], ['upper', 'UPPER'], ['lower', 'lower']];
const QH_FMT_ROWS = [
  ['keywordCase', 'Keywords', QH_FMT_CASE],
  ['functionCase', 'Functions', QH_FMT_CASE],
  ['dataTypeCase', 'Data types', QH_FMT_CASE],
  ['identifierCase', 'Names', QH_FMT_CASE],
  ['indent', 'Indent', [['2', '2 spaces'], ['4', '4 spaces'], ['tab', 'Tab'], ['0', 'No indent']]],
  ['indentStyle', 'Layout', [['standard', 'Standard'], ['tabularLeft', 'Left aligned'], ['tabularRight', 'Right aligned']]],
  ['logicalOperatorNewline', 'AND / OR', [['before', 'Start of the line'], ['after', 'End of the line']]],
  ['expressionWidth', '( ) on one line', [[50, 'Up to 50 characters'], [80, 'Up to 80 characters'], [120, 'Up to 120 characters']]],
  ['linesBetweenQueries', 'Between statements', [[1, '1 empty line'], [0, 'No empty line'], [2, '2 empty lines']]],
];
const QH_FMT_CASE_KEYS = ['keywordCase', 'functionCase', 'dataTypeCase', 'identifierCase'];
const QH_FMT_SAMPLE = "select u.id, u.name, count(o.id) as orders, cast(sum(o.total) as decimal(12, 2)) as total " +
  "from users u join orders o on o.user_id = u.id where u.active = 1 and o.created_at >= '2026-01-01' " +
  "or o.total > 100 group by u.id, u.name; select 1;";

function FormatOptions({ engineId, onFormat, style }) {
  const [opts, setOpts] = React.useState(() => qhFmtOptionsLoad());
  const set = (k, v) => { const next = qhFmtOptionsClean({ ...opts, [k]: v }); setOpts(next); qhFmtOptionsSave(next); };
  // Table aliases (qhAutoAlias): the switch and the rules, stored at once.
  const [al, setAl] = React.useState(() => qhAliasOptsLoad());
  const setAlias = (k, v) => { const next = qhAliasOptsClean({ ...al, [k]: v }); setAl(next); qhAliasOptsSave(next); };
  const badRules = qhAliasRules(al.rules).bad;
  const ch = engineId === 'clickhouse';
  const preview = qhFormatSql(QH_FMT_SAMPLE, engineId, opts);
  const prof = qhFmtProfileOf(opts);
  const pick = (id) => { const p = qhFmtProfileOptions(id); if (p) { setOpts(p); qhFmtOptionsSave(p); } };
  return (
    <div className="qh-fmt-pop" style={style} role="dialog" aria-label="Format options">
      <div className="qh-kb-title">Format options</div>
      <label className="qh-fmt-row qh-fmt-prof">
        <span>Profile</span>
        <select value={prof} onChange={(e) => pick(e.target.value)}>
          {QH_FMT_PROFILES.map(([id, l]) => <option key={id} value={id}>{l}</option>)}
          {!prof && <option value="" disabled>Custom</option>}
        </select>
      </label>
      {QH_FMT_ROWS.map(([k, label, list]) => {
        const locked = ch && QH_FMT_CASE_KEYS.includes(k);
        return (
          <label key={k} className="qh-fmt-row" title={locked ? 'ClickHouse names are case sensitive. The case stays as typed.' : undefined}>
            <span>{label}</span>
            <select value={String(locked ? 'preserve' : opts[k])} disabled={locked}
              onChange={(e) => { const hit = list.find(([v]) => String(v) === e.target.value); if (hit) set(k, hit[0]); }}>
              {list.map(([v, l]) => <option key={String(v)} value={String(v)}>{l}</option>)}
            </select>
          </label>
        );
      })}
      <label className="qh-fmt-check">
        <input type="checkbox" checked={opts.denseOperators} onChange={(e) => set('denseOperators', e.target.checked)} />
        <span>No spaces around operators</span>
      </label>
      <label className="qh-fmt-check">
        <input type="checkbox" checked={opts.newlineBeforeSemicolon} onChange={(e) => set('newlineBeforeSemicolon', e.target.checked)} />
        <span>Put ; on its own line</span>
      </label>
      {ch && <div className="qh-fmt-note">ClickHouse names are case sensitive. The case stays as typed.</div>}
      {preview != null && <pre className="qh-fmt-prev" aria-label="Preview">{preview}</pre>}
      <div className="qh-kb-title qh-fmt-sub">Table aliases</div>
      <label className="qh-fmt-check">
        <input type="checkbox" checked={al.auto} onChange={(e) => setAlias('auto', e.target.checked)} />
        <span>Add an alias to a table that you pick after FROM or JOIN</span>
      </label>
      <label className="qh-fmt-rules">
        <span>Alias rules, one per line. <code>*</code> is any text.</span>
        <textarea rows={3} spellCheck={false} value={al.rules} placeholder={'audit_* = au\nsales.orders = so'}
          aria-label="Alias rules" onChange={(e) => setAlias('rules', e.target.value)} />
      </label>
      {badRules > 0 && <div className="qh-fmt-note">{badRules === 1 ? '1 line is not a rule. Write it as name = alias.' : badRules + ' lines are not rules. Write each as name = alias.'}</div>}
      <div className="qh-fmt-foot">
        <button type="button" className="qh-btn qh-btn-ghost qh-btn-sm" onClick={() => { const d = qhFmtOptionsClean(null); setOpts(d); qhFmtOptionsSave(d); }}>Reset</button>
        <button type="button" className="qh-btn qh-btn-primary qh-btn-sm" onClick={onFormat}>Format now</button>
      </div>
    </div>
  );
}

function EditorTabs({ tabs, activeId, onSelect, onClose, onNew, wrap, onToggleWrap, lint, onToggleLint, lintCount, onFormat, fmtEngine, onCloseOthers, onCloseRight, onCloseAll, onDuplicate, onRename, onCopySql, onDownloadSql, onReorder }) {
  const [kb, setKb] = React.useState(false);
  const [dragId, setDragId] = React.useState(null);
  const [overId, setOverId] = React.useState(null);
  const [kbPos, setKbPos] = React.useState(null);
  // Same fix as the export menu: the popup is rendered 6px below the button, and
  // closing on mouse-out meant crossing that gap dismissed it. See qhUseDismiss.
  const closeKb = React.useCallback(() => setKb(false), []);
  const kbWrapRef = qhUseDismiss(kb, closeKb);
  const kbBtnRef = React.useRef(null);
  // The Format options popup: fixed under its button, closed by a click outside.
  const [fmtPos, setFmtPos] = React.useState(null);
  const closeFmt = React.useCallback(() => setFmtPos(null), []);
  const fmtWrapRef = qhUseDismiss(!!fmtPos, closeFmt);
  const fmtBtnRef = React.useRef(null);
  const toggleFmt = () => {
    if (fmtPos) { setFmtPos(null); return; }
    const r = fmtBtnRef.current.getBoundingClientRect();
    setKb(false);
    setFmtPos({ top: r.bottom, right: Math.max(6, window.innerWidth - r.right) });
  };
  const barRef = React.useRef(null);
  // The + belongs at the END OF THE TABS, not at the end of the row: that is
  // where the next tab will appear, and a gap of empty strip between the last
  // tab and the button reads as "unrelated control". It only moves to the
  // pinned cluster when the tabs no longer fit — there, in flow, it would
  // scroll out of reach exactly when a new tab is most likely wanted.
  // The test is on the TABS' width alone (36px of room for the button, 4px of
  // strip padding), never on scrollWidth with the button in it, or showing the
  // button would create the overflow that hides it again.
  const [inlineNew, setInlineNew] = React.useState(true);
  React.useLayoutEffect(() => {
    const bar = barRef.current; if (!bar) return;
    let t = 4;
    bar.querySelectorAll('.qh-tab').forEach((el) => { t += el.offsetWidth; });
    const fits = t + 36 <= bar.clientWidth;
    if (fits !== inlineNew) setInlineNew(fits);
  });
  React.useEffect(() => {
    const bar = barRef.current;
    if (!bar || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => {
      let t = 4;
      bar.querySelectorAll('.qh-tab').forEach((el) => { t += el.offsetWidth; });
      setInlineNew(t + 36 <= bar.clientWidth);
    });
    ro.observe(bar);
    return () => ro.disconnect();
  }, []);
  const newBtn = (extra) => (
    <button className={'qh-tab-new' + (extra || '')} onClick={onNew} data-kbd={(window.QH_KBD || {}).newq} aria-label="New query">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M12 5v14M5 12h14"/></svg>
    </button>
  );
  React.useEffect(() => {
    const bar = barRef.current; if (!bar) return;
    const el = bar.querySelector('.qh-tab.is-active'); if (!el) return;
    const er = el.getBoundingClientRect(), br = bar.getBoundingClientRect();
    if (er.left < br.left) bar.scrollLeft -= (br.left - er.left) + 8;
    else if (er.right > br.right) bar.scrollLeft += (er.right - br.right) + 8;
  }, [activeId, tabs.length]);
  React.useEffect(() => {
    const bar = barRef.current; if (!bar) return;
    const onWheel = (e) => {
      if (e.deltaY && Math.abs(e.deltaY) >= Math.abs(e.deltaX) && bar.scrollWidth > bar.clientWidth) {
        bar.scrollLeft += e.deltaY; e.preventDefault();
      }
    };
    bar.addEventListener('wheel', onWheel, { passive: false });
    return () => bar.removeEventListener('wheel', onWheel);
  }, []);
  const [menu, setMenu] = React.useState(null);
  const [editId, setEditId] = React.useState(null);
  const [draft, setDraft] = React.useState('');
  const cancelledRef = React.useRef(false);
  const SHORTCUTS = [['F5', 'Run selection, else whole query'], ['F8', 'Run selection / current statement'], ['⇧ + F8', 'Run from the top to the current statement'], ['⇧ + ⌥ / Alt + S', 'Select current statement'], ['⌥ / Alt + PgUp / PgDn', 'Previous / next statement'], ['⇧ + F12 · ⌘/Ctrl + ⇧ + \\', 'Go to the matching bracket, CASE/END or BEGIN/END'], ['⌥ / Alt + ↑ / ↓', 'Move line up / down'], ['⇧ + ⌥ / Alt + ;', 'Add missing semicolons'], ['⇧ + ⌥ / Alt + E', 'Expand every * of the text or selection'], ['⇧ + F6', 'Rename the alias at the caret'], ['⌘ / Ctrl + B · ⌘ / Ctrl + click', 'Go to the definition: alias, CTE, or the object in the tree'], ['⇧ + ⌥ / Alt + D', 'CREATE script of the object at the caret'], ['⇧ + ⌥ / Alt + Q', 'Qualify table and column names'], ['Ctrl + Space after GROUP BY', 'The non-aggregated SELECT columns'], ['PgUp / PgDn in the list', 'Move 8 rows; drag the corner to resize'], ['Select, then ( \' " [', 'Wrap the selection'], ['⌘ / Ctrl + ↵', 'Run'], ['F2', 'Rename tab'], ['⌘/Ctrl+⇧+T', 'Reopen closed tab'], ['Ctrl + Space', 'Suggestions; in a SELECT, all columns'], ['Ctrl + Space in the column list', 'Check a column; Enter inserts the checked ones'], ['Tab after INSERT INTO t, UPDATE t, EXEC p, CALL p', 'Statement template'], ['Tab / ⇧ + Tab in a template or snippet', 'Next / previous field'], ['Ctrl + ⇧ + Space', 'Parameter info of the function at the caret'], ['ssf + Tab', 'Snippet: type a key, pick it (list below)'], ['Tab', 'Indent'], ['⌥ / Alt + Z', 'Toggle word wrap'], ['⇧ + ⌥ / Alt + F', 'Format SQL'], ['⌥ / Alt + ← →', 'Previous / next result'], ['Drag tab', 'Reorder tabs'], ['Middle-click', 'Close tab'], ['Drag', 'Drop tree object into editor'], ['*', 'SELECT → expand columns'], ['Right-click', 'Tab options']];
  const beginRename = (t) => { if (t.kind) return; setMenu(null); setEditId(t.id); setDraft(t.name); };
  const openMenu = (e, id) => { e.preventDefault(); e.stopPropagation(); setKb(false); setMenu({ x: Math.min(e.clientX, window.innerWidth - 200), y: e.clientY, id }); };
  React.useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'F2' && !editId) { const t = tabs.find(x => x.id === activeId); if (t) { e.preventDefault(); beginRename(t); } }
      else if (e.key === 'Escape' && menu) setMenu(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [tabs, activeId, editId, menu]);
  return (
    <div className="qh-tabbar">
      <div className="qh-tabs" ref={barRef}>
      {tabs.map(t => (
        <div key={t.id} className={'qh-tab' + (t.id === activeId ? ' is-active' : '') + (qhTabIsProd(t) ? ' is-prod' : '') + (overId === t.id ? ' is-drop' : '') + (dragId === t.id ? ' is-dragging' : '')} onClick={() => onSelect(t.id)}
          // 2026-11-09: the request id is no longer in the strip. Every tab, the
          // active one too, has the full name and the request id on hover,
          // because a narrow tab cuts its name. A PROD tab adds "PROD".
          title={t.name + (!t.kind && t.reqId ? ' · request #' + t.reqId : '') + (qhTabIsProd(t) ? ' · PROD' : '')}
          draggable={editId !== t.id}
          onDragStart={(e) => { setDragId(t.id); e.dataTransfer.effectAllowed = 'move'; try { e.dataTransfer.setData('text/x-qh-tab', t.id); } catch (err) {} }}
          onDragOver={(e) => { if (dragId && dragId !== t.id) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; if (overId !== t.id) setOverId(t.id); } }}
          onDragLeave={() => setOverId(o => (o === t.id ? null : o))}
          onDrop={(e) => { e.preventDefault(); if (dragId && dragId !== t.id) onReorder(dragId, t.id); setDragId(null); setOverId(null); }}
          onDragEnd={() => { setDragId(null); setOverId(null); }}
          onMouseDown={(e) => { if (e.button === 1) e.preventDefault(); }}
          onAuxClick={(e) => { if (e.button === 1) { e.preventDefault(); onClose(t.id); } }}
          onContextMenu={(e) => openMenu(e, t.id)} onDoubleClick={(e) => { e.stopPropagation(); beginRename(t); }}>
          {t.kind === 'welcome' ? (
            <span className="qh-tab-home" aria-hidden="true"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 11l9-8 9 8"/><path d="M5 10v10h5v-6h4v6h5V10"/></svg></span>
          ) : t.kind === 'whatsnew' ? (
            <span className="qh-tab-home" aria-hidden="true"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 3l1.9 3.9 4.3.6-3.1 3 .7 4.3-3.8-2-3.8 2 .7-4.3-3.1-3 4.3-.6z"/></svg></span>
          ) : t.kind === 'bundle' ? (
            // A batch or fleet run: a stack of databases (M12c).
            <span className="qh-tab-home" aria-hidden="true"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></svg></span>
          ) : (
            // A tab with no target yet has a grey dot: it has no tier to show.
            <span className={'qh-tab-dot ' + (qhTabNoTarget(t) ? 'tier-none' : 'tier-' + String(t.tier || '').toLowerCase())} />
          )}
          {editId === t.id ? (
            <input className="qh-tab-rename" value={draft} autoFocus spellCheck={false}
              onChange={(e) => setDraft(e.target.value)} onClick={(e) => e.stopPropagation()}
              onKeyDown={(e) => {
                if (e.key === 'Enter') { e.preventDefault(); e.currentTarget.blur(); }
                else if (e.key === 'Escape') { e.preventDefault(); cancelledRef.current = true; e.currentTarget.blur(); }
              }}
              onBlur={() => { if (cancelledRef.current) { cancelledRef.current = false; setEditId(null); return; } onRename(t.id, draft); setEditId(null); }} />
          ) : (
            <span className="qh-tab-name">{t.name}{t.dirty ? ' •' : ''}</span>
          )}
          {editId !== t.id && (
            <button className="qh-tab-x" onClick={(e) => { e.stopPropagation(); onClose(t.id); }} aria-label="Close tab">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
            </button>
          )}
        </div>
      ))}
      {inlineNew && newBtn(' qh-tab-new-inline')}
      </div>
      <div className="qh-tabs-fixed">
      {!inlineNew && newBtn('')}
      {menu && (() => {
        const idx = tabs.findIndex(x => x.id === menu.id);
        const mt = tabs[idx];
        if (!mt) return null;
        const many = tabs.length > 1;
        const isLast = idx === tabs.length - 1;
        const hasSql = !!(mt.sql && mt.sql.trim());
        const close = () => setMenu(null);
        return (
          <>
            <div className="qh-ctx-backdrop" onClick={close} onContextMenu={(e) => { e.preventDefault(); close(); }} />
            <div className="qh-ctxmenu" style={{ left: menu.x, top: menu.y }}>
              <div className="qh-ctx-title">{mt.name}</div>
              {!mt.kind && <>
              <button onClick={() => beginRename(mt)}>{QH_TAB_ICN.rename}Rename…<span className="qh-ctx-kbd">F2</span></button>
              <button onClick={() => { onDuplicate(mt.id); close(); }}>{QH_TAB_ICN.dup}Duplicate</button>
              <button disabled={!hasSql} onClick={() => { onCopySql(mt.id); close(); }}>{QH_TAB_ICN.copy}Copy SQL</button>
              <button disabled={!hasSql} onClick={() => { onDownloadSql(mt.id); close(); }}>{QH_TAB_ICN.download}Download .sql</button>
              <div className="qh-ctx-sep" />
              </>}
              <button onClick={() => { onClose(mt.id); close(); }}>{QH_TAB_ICN.x}Close</button>
              <button disabled={!many} onClick={() => { onCloseOthers(mt.id); close(); }}>{QH_TAB_ICN.x}Close others</button>
              <button disabled={isLast} onClick={() => { onCloseRight(mt.id); close(); }}>{QH_TAB_ICN.right}Close to the right</button>
              <button disabled={!many} onClick={() => { onCloseAll(); close(); }}>{QH_TAB_ICN.all}Close all</button>
            </div>
          </>
        );
      })()}
      {/* Format SQL sits next to word wrap in the same pinned cluster. It does one
          thing and keeps no state, so it is a plain icon button. It is absent when
          the build has no formatter (the raw prototype), because a button that does
          nothing is worse than none. */}
      {onFormat && typeof window.qhFormatSql === 'function' && window.__qhSqlFormatter && (
        <div className="qh-fmt-wrap" ref={fmtWrapRef}>
          <button className="qh-kb-btn" onClick={onFormat}
            data-kbd={'Format SQL  ·  ' + ((window.QH_KBD || {}).fmt || 'Shift Alt F')}
            aria-label="Format SQL">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M4 6h16"/><path d="M4 10h10"/><path d="M4 14h16"/><path d="M4 18h10"/></svg>
          </button>
          {/* The options of Format (case, indent, layout). A narrow button
              beside it, so the Format button keeps its one job. */}
          <button ref={fmtBtnRef} className={'qh-kb-btn qh-fmt-more' + (fmtPos ? ' is-open' : '')} onClick={toggleFmt}
            data-kbd="Format options" aria-label="Format options" aria-expanded={fmtPos ? 'true' : 'false'}>
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round"><path d="M6 9l6 6 6-6"/></svg>
          </button>
          {fmtPos && <FormatOptions engineId={fmtEngine} onFormat={onFormat} style={{ position: 'fixed', top: fmtPos.top, right: fmtPos.right }} />}
        </div>
      )}
      {/* Word wrap lives in the tab bar's pinned cluster rather than in a
          toolbar of its own: the row is already there and never scrolls, so a
          per-editor view switch costs 34px and no new surface. State is on the
          button (tinted when on), not in a label that would need the space. */}
      <button className={'qh-kb-btn qh-wrapt' + (wrap ? ' is-on' : '')} onClick={onToggleWrap}
        data-kbd={'Word wrap  ·  ' + ((window.QH_KBD || {}).wrap || 'Alt Z')}
        aria-pressed={wrap ? 'true' : 'false'} aria-label={wrap ? 'Word wrap on' : 'Word wrap off'}>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M4 6h16"/><path d="M4 11.5h12a3 3 0 010 6h-4"/><path d="M15 14.5l-3 3 3 3"/></svg>
      </button>
      {/* Code analysis (qhLint) on or off. The button counts the findings of
          the open tab, so a person sees them without a scan of the text. */}
      {onToggleLint && (
        <button className={'qh-kb-btn qh-wrapt qh-lintt' + (lint ? ' is-on' : '')} onClick={onToggleLint}
          data-kbd={lint ? 'Code analysis  ·  ' + (lintCount ? lintCount + (lintCount === 1 ? ' finding' : ' findings') : 'no findings') : 'Code analysis  ·  off'}
          aria-pressed={lint ? 'true' : 'false'} aria-label={lint ? 'Code analysis on' : 'Code analysis off'}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M4 7h10M4 12h8M4 17h6"/><path d="M14 16l2.5 2.5L21 14"/></svg>
          {lint && lintCount > 0 ? <span className="qh-lint-n">{lintCount}</span> : null}
        </button>
      )}
      <div className="qh-kb-wrap" ref={kbWrapRef}>
        <button ref={kbBtnRef} className="qh-kb-btn" onClick={() => {
          if (kb) { setKb(false); return; }
          setFmtPos(null);
          const r = kbBtnRef.current.getBoundingClientRect();
          setKbPos({ top: r.bottom, right: Math.max(6, window.innerWidth - r.right) });
          setKb(true);
        }} title="Keyboard shortcuts" aria-label="Keyboard shortcuts">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M6 14h12"/></svg>
        </button>
        {kb && kbPos && (
          <div className="qh-kb-pop" style={{ position: 'fixed', top: kbPos.top, right: kbPos.right }}>
            <div className="qh-kb-title">Keyboard shortcuts</div>
            {SHORTCUTS.map(([k, d]) => (
              <div key={k} className="qh-kb-row"><kbd className="qh-kbd">{k}</kbd><span>{d}</span></div>
            ))}
            {/* The snippet keys (QH_SNIPPETS). The list in the editor shows
                only the ones that the tab's engine gets. */}
            <div className="qh-kb-title qh-kb-sub">Snippets</div>
            {QH_SNIPPETS.map(sn => (
              <div key={'snip:' + sn.key} className="qh-kb-row"><kbd className="qh-kbd">{sn.key}</kbd><span>{sn.desc}</span></div>
            ))}
          </div>
        )}
      </div>
    </div>
    </div>
  );
}

// qhBuildSuggest is exported so the suggestion rules can be tested directly.
// It is the one piece of editor behaviour with no visible surface of its own —
// a wrong pool looks like "autocomplete is being unhelpful", never like a bug.
// The undo history's pieces and the * expansion reader are exported for the
// same reason; qhUndoForget is also what sign-out calls.
Object.assign(window, { SqlEditor, EditorTabs, FormatOptions, QH_FMT_ROWS, QH_FMT_SAMPLE, QH_EDITOR_HINT, qhLint, qhListAddsNothing, QH_LINT_RULES, qhHighlight, qhBuildSuggest, qhSuggestContext, QH_REDACTED_LIT, qhRedactedAt,
  QH_SNIPPETS, QH_SNIPPET_CURSOR, QH_DRAG_BLOCK, qhSnippetsFor, qhSnippetText, qhInsertBlock, QH_KEYWORDS, qhSnippetPreview, qhSnippetFormError, QH_SNIP_KEY_RE, qhAliasAt, qhRenameAlias, qhQualifyNames, qhObjectNameAt, qhDefinitionAt,
  qhTextDiff, qhEditKind, qhUndoDoc, qhUndoRecord, qhUndoSeal, qhUndoStep, qhUndoForget,
  qhSqlTokens, qhStarExpansion, qhBlockerRanges, qhFromIsName, qhFromUnquote, qhReadFromList, qhRefLookup,
  qhClTokOpen, qhColumnList, qhStopsAfterEdit, qhStatementTemplate, qhTemplateItem, qhTplParams, qhCteDefs, qhCteNamesAt, qhSelectOutCols, qhRelCols, qhInsertColsSlot, qhColItem, qhOutName, qhAcStep, qhStatementAt, qhAliasColumns, qhTabIsProd,
  qhJoinSuggestions, qhJoinOnSuggestions, qhJoinPairs, qhJoinAlias,
  QH_KW_PHRASES, QH_KW_ENGINE, qhKeywordsFor, qhKwPhrasesFor, qhKwPhraseFits, qhEngineIdReadOnly,
  QH_ALIAS_KEY, qhAliasOptsClean, qhAliasOptsLoad, qhAliasOptsSave, qhAliasRules, qhAliasBase, qhAutoAlias,
  qhNameInitials, qhExpandAllStars, qhApplyEdits, qhGroupByFill, qhGroupByItem, qhAcPage, QH_AC_MAX, QH_AC_PAGE, qhAcSizeLoad, QH_AC_SIZE_KEY,
  qhStatementRanges, qhAddSemicolons, qhStatementJump, qhToCaretRange, qhPairs, qhPairAt, qhPairJump, qhOccurrences, qhCaretMarks,
  qhMoveLines, qhWrapSelection, QH_WRAP_PAIRS, QhCaretMarks,
  qhHoverInfo, qhHoverLines, qhHoverOffsetAt,
  QH_FUNC_SIGS, qhSigParams, qhBuiltinSigs, qhCallAt, qhSignaturesFor, qhSigPick, qhSigActive, qhLintFindings, qhProblems });
