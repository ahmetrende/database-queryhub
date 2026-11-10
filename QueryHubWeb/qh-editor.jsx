// QueryHub — SQL editor with line numbers, syntax highlighting, tabs.

const QH_KEYWORDS = ('SELECT INSERT UPDATE DELETE FROM WHERE JOIN INNER LEFT RIGHT FULL OUTER ON GROUP BY ORDER HAVING LIMIT OFFSET ' +
  'AS AND OR NOT NULL IS IN LIKE ILIKE BETWEEN UNION ALL DISTINCT INTO VALUES SET CREATE ALTER DROP TABLE INDEX VIEW ' +
  'TRUNCATE RENAME ADD COLUMN PRIMARY KEY FOREIGN REFERENCES DEFAULT CASE WHEN THEN ELSE END WITH RETURNING ' +
  'ASC DESC INTERVAL CURRENT_DATE NOW EXISTS GRANT REVOKE MERGE USING CASCADE CONSTRAINT').split(/\s+/);
const QH_KW_SET = new Set(QH_KEYWORDS);

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
  { key: 'dfw', desc: 'DELETE ... WHERE', write: true, body: { any: 'DELETE FROM $CURSOR$\nWHERE ' } },
  { key: 'cte', desc: 'WITH ... SELECT', body: { any: 'WITH cte AS (\n  $CURSOR$\n)\nSELECT * FROM cte' } },
  { key: 'cw', desc: 'CASE WHEN', expr: true, body: { any: 'CASE WHEN $CURSOR$ THEN  ELSE  END' } },
  { key: 'exs', desc: 'EXISTS (SELECT 1 ...)', expr: true, body: { any: 'EXISTS (SELECT 1 FROM $CURSOR$ WHERE )' } },
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

// The snippets that an engine gets, each as { key, desc, body, write, expr }.
// `body` is the text for that engine, with the marker still in it.
function qhSnippetsFor(engineId) {
  const eng = String(engineId || 'postgres');
  const info = (typeof QH_ENGINES !== 'undefined' && QH_ENGINES[eng]) || null;
  const readOnly = info ? !!info.readOnly : (eng === 'clickhouse' || eng === 'athena');
  const out = [];
  QH_SNIPPETS.forEach(sn => {
    if (sn.write && readOnly) return;
    const body = Object.prototype.hasOwnProperty.call(sn.body, eng) ? sn.body[eng] : sn.body.any;
    if (body == null) return;
    out.push({ key: sn.key, desc: sn.desc, body, write: !!sn.write, expr: !!sn.expr });
  });
  return out;
}

// The text that a snippet inserts and the caret offset in it. Each line after
// the first gets `indent`, the indent of the line where the person typed the key.
function qhSnippetText(body, indent) {
  const b = String(body).split('\n').join('\n' + (indent || ''));
  const at = b.indexOf(QH_SNIPPET_CURSOR);
  if (at < 0) return { text: b, caretAt: b.length };
  return { text: b.slice(0, at) + b.slice(at + QH_SNIPPET_CURSOR.length), caretAt: at };
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
  const add = (arr, type, typeRank, textOf) => (arr || []).forEach(n => {
    const nl = n.toLowerCase();
    let rank;
    if (nl === lc) rank = 0;
    else if (nl.startsWith(lc)) rank = 1;
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
  else if (tableMode) { add(schema.tables, 'table', 0); add(schema.systemTables, 'system', 1); add(schema.dbs, 'database', 2); add(schema.functions, 'function', 3); }
  else { add(schema.tables, 'table', 0); add(schema.columns, 'column', 1); add(schema.functions, 'function', 2); add(schema.systemTables, 'system', 3); add(QH_KEYWORDS, 'keyword', 4); }
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
      qhSnippetsFor(engineId).forEach(sn => {
        if (sn.expr === first) return;
        const kl = sn.key.toLowerCase();
        let rank, typeRank;
        if (kl === lc) { rank = 0; typeRank = -1; }
        else if (kl.startsWith(lc)) { rank = 1; typeRank = 5; }
        else return;
        const st = qhSnippetText(sn.body, indent);
        items.push({ text: st.text, caretAt: st.caretAt, label: sn.key, desc: sn.desc, type: 'snippet', rank, typeRank });
      });
    }
  }
  // Sort BEFORE truncating: the old code capped in insertion order, so a
  // matching table could be pushed out of the list by ten columns.
  items.sort((a, b) => (a.rank - b.rank) || (a.typeRank - b.typeRank));
  const seen = new Set(); const out = [];
  for (const it of items) { const k = it.type + ':' + it.text; if (!seen.has(k)) { seen.add(k); out.push(it); } if (out.length >= 12) break; }
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
// USING joins, and a list that ends half typed (`FROM a JOIN `).
function qhReadFromList(S, depth, from, D, loose) {
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
    F.push({ t: S[i], d: depth[i] - D });
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
  const readRef = () => {
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
// Ctrl+Space column list
//
// In a column position of a SELECT (the select list, WHERE, ON, HAVING, GROUP
// BY, ORDER BY), the columns of every table in that SELECT's FROM list, grouped
// by table in FROM order. One table: bare names. Several: each name qualified
// by the alias, or by the table name when there is no alias. A typed `t.`
// lists t only, bare. Type, nullability and the primary key come from the
// catalog (`tableMeta`). A table the catalog does not list asks `live(name)`
// (names only). A table nobody knows lists nothing, never invented columns.
//
// Returns { kind: 'ready', start, end, slot, items, count },
// { kind: 'lookup', names }, or null when the caret is not in a column position.
// `slot` is where the reference starts, qualifier included.
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
  const refs = qhReadFromList(S, depth, sel + 1, Ds, true);
  if (!refs || !refs.length) return null;

  const label = (r) => (r.alias || r.parts[r.parts.length - 1]);
  let want = refs;
  if (qual) {
    const q = qhFromUnquote(qhSqlTokens(qual)[0]).toLowerCase();
    want = refs.filter(r => qhFromUnquote(label(r)).toLowerCase() === q);
    if (want.length !== 1) return null;
  }
  const sch = schema || {};
  const colsOf = (r) => {
    const meta = qhRefLookup(r, sch, sch.tableMetaQ, sch.tableMeta);
    if (meta && meta.length) return meta;
    const names = qhRefLookup(r, sch, sch.tableColsQ, sch.tableCols);
    const got = (names && names.length) ? names : (live ? live(r.parts.map(x => x.v).join('.')) : null);
    return Array.isArray(got) ? got.map(n => ({ name: n })) : got;
  };
  const cols = want.map(colsOf);
  const names = want.filter((r, j) => cols[j] === undefined).map(r => r.parts.map(x => x.v).join('.'));
  if (names.length) return { kind: 'lookup', names };

  const quote = (n) => (typeof qhQuoteIdentFor === 'function' ? qhQuoteIdentFor(n, engineId) : qhQuoteIdent(n));
  const lc = pm ? pm[0].toLowerCase() : '';
  const multi = !qual && refs.length > 1;
  const items = [];
  let count = 0;
  want.forEach((r, j) => {
    const hit = (cols[j] || []).filter(col => col && String(col.name).toLowerCase().startsWith(lc));
    if (!hit.length) return;
    items.push({ type: 'group', header: true, key: 'g' + j, text: '',
                 label: r.parts.map(x => x.v).join('.'), alias: r.alias ? r.alias.v : '' });
    hit.forEach(col => {
      const n = String(col.name);
      items.push({ type: 'column', key: 'c' + j + ':' + n, label: n,
                   text: multi ? label(r).v + '.' + quote(n) : quote(n),
                   colType: col.type ? String(col.type) : '',
                   nullable: typeof col.nullable === 'boolean' ? col.nullable : null,
                   pk: !!col.pk, pii: !!col.pii });
      count++;
    });
  });
  return { kind: 'ready', start, end: caret, slot, items, count };
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

// The alias for a new table: the first letter of each word of the name, split
// on `_` (`order_items` -> `oi`). A number goes on when the statement already
// uses that word, or the word is a keyword (`u2`).
function qhJoinAlias(table, used, engineId) {
  let base = String(table).split('_').filter(Boolean).map(w => w[0]).join('').toLowerCase().replace(/[^a-z0-9_]/g, '');
  if (!/^[a-z_]/.test(base)) base = 't' + base;
  const bad = (x) => used.has(x) || QH_KW_SET.has(x.toUpperCase()) || QH_NOT_NAME.has(x.toUpperCase())
    || (typeof qhIsReservedWord === 'function' && qhIsReservedWord(x, engineId || 'postgres'));
  if (!bad(base)) return base;
  for (let n = 2; ; n++) if (!bad(base + n)) return base + n;
}

function qhJoinQuote(n, engineId) {
  return typeof qhQuoteIdentFor === 'function' ? qhQuoteIdentFor(n, engineId) : qhQuoteIdent(n);
}

// After JOIN: whole JOIN clauses for the catalog tables, at most QH_JOIN_MAX.
// The table name is inserted as a table pick inserts it (`schema.qualify`).
// Returns { items, start, end } or null.
function qhJoinSuggestions(sql, caret, schema, engineId) {
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
    const alias = qhJoinAlias(n, used, engineId);
    const q = sch.qualify ? sch.qualify[n] : null;
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
const QH_AC_TYPE_LABEL ={ keyword: 'kw', table: 'table', column: 'col', database: 'db', system: 'system', function: 'fn', expand: 'all cols', join: 'join',
  procedure: 'proc', schema: 'schema', index: 'index', snippet: 'snip' };

function SqlEditor({ value, onChange, fontSize, wrap, onRun, onRunSelection, selectionGetter, onSelectionChange, schema, engineId, focusSignal, formatSignal, insertSignal, revealRange, docId, issues, revealSignal }) {
  const taRef = React.useRef(null);
  const issueRef = React.useRef(null);
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
    clearTimeout(issueTimer.current);
    setTip(null);
    hideHover();
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
  const [dragOver, setDragOver] = React.useState(false);
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
      if (!ta || ta.selectionStart === ta.selectionEnd) return '';
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
    let last = null;
    const report = () => {
      const a = Math.min(ta.selectionStart, ta.selectionEnd), b = Math.max(ta.selectionStart, ta.selectionEnd);
      const n = qhSelectionLines(ta.value, a, b);
      const key = n ? a + ':' + b + ':' + ta.value.length : '';
      if (key === last) return;
      last = key;
      onSelectionChange(n ? { lines: n, start: a, text: ta.value.slice(a, b) } : null);
    };
    const onDoc = () => { if (document.activeElement === ta) report(); };
    const evs = ['select', 'keyup', 'mouseup', 'input', 'focus'];
    evs.forEach(ev => ta.addEventListener(ev, report));
    document.addEventListener('selectionchange', onDoc);
    report();
    return () => {
      evs.forEach(ev => ta.removeEventListener(ev, report));
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
    if (gutRef.current) gutRef.current.scrollTop = ta.scrollTop;
    if (ac) setAc(null);
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
              left: p.left, start: al.start, end: al.end, cols: true, slot: al.slot });
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
      ? (qhJoinSuggestions(ta.value, caret, sch, engineId) || qhJoinOnSuggestions(ta.value, caret, sch, engineId))
      : null;
    if (js) {
      const p = posAt(ta, js.start);
      setAc({ items: js.items.concat(s ? s.items : []), idx: 0, top: p.top, lineTop: p.lineTop, left: p.left,
              start: js.start, end: js.end });
      return;
    }
    if (!s) { setAc(null); return; }
    const p = posAt(ta, s.start);
    setAc({ items: s.items, idx: 0, top: p.top, lineTop: p.lineTop, left: p.left, start: s.start, end: s.end });
  };

  const accept = (item) => {
    const ta = taRef.current;
    // The item may carry its own start: a qualified insert replaces the
    // qualifier the user already typed rather than appending behind it.
    const from = (item && typeof item.start === 'number') ? item.start : ac.start;
    const nv = value.slice(0, from) + item.text + value.slice(ac.end);
    // A snippet says where its caret goes (caretAt). Everything else ends
    // with the caret after the text.
    const caret = from + (typeof item.caretAt === 'number' ? item.caretAt : item.text.length);
    setAc(null);
    commit(nv, 'complete', ta ? [ta.selectionStart, ta.selectionEnd] : null, [caret, caret]);
    requestAnimationFrame(() => { if (ta) { ta.focus(); ta.selectionStart = ta.selectionEnd = caret; } });
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
    setAc({ items: cl.items, idx: Math.max(0, cl.items.findIndex(it => !it.header)), top: p.top, lineTop: p.lineTop,
            left: p.left, start: cl.start, end: cl.end, cols: true, slot: cl.slot });
    return true;
  };
  // Ctrl+Space: the column list in a column position, else the normal list for
  // the caret, also with nothing typed.
  const openAC = (ta, afterLookup) => {
    if (ta.selectionStart !== ta.selectionEnd) { setAc(null); return; }
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
    if (e.key === 'F5') { e.preventDefault(); setAc(null); onRun && onRun(); return; }
    // Select the statement at the caret: Shift+Alt+S. It runs nothing. e.code,
    // not e.key, for the same Mac reason as Format SQL.
    if (e.shiftKey && e.altKey && !e.ctrlKey && !e.metaKey && e.code === 'KeyS') {
      e.preventDefault(); setAc(null);
      const ta = e.target, r = qhStatementAt(ta.value, ta.selectionStart);
      if (r) ta.setSelectionRange(r.start, r.end);
      return;
    }
    // F8 runs the selection. With no selection it runs the statement at the
    // caret (see qhStatementAt) and selects it first, so the person sees what
    // runs. A caret in a part with no SQL does nothing.
    if (e.key === 'F8') {
      e.preventDefault(); setAc(null);
      const ta = e.target, s = ta.value.slice(ta.selectionStart, ta.selectionEnd);
      if (s.trim()) { onRunSelection && onRunSelection(s); return; }
      const r = qhStatementAt(ta.value, ta.selectionStart);
      if (!r) return;
      ta.setSelectionRange(r.start, r.end);
      onRunSelection && onRunSelection(ta.value.slice(r.start, r.end));
      return;
    }
    // Ctrl+Space opens the list at the caret. Ctrl on every OS, the Mac too.
    if (e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey && (e.key === ' ' || e.code === 'Space')) {
      e.preventDefault();
      openAC(e.target);
      return;
    }
    if (ac && ac.items.length) {
      // Group headers of the column list are skipped.
      if (e.key === 'ArrowDown') { e.preventDefault(); setAc(a => ({ ...a, idx: qhAcStep(a.items, a.idx, 1) })); return; }
      if (e.key === 'ArrowUp') { e.preventDefault(); setAc(a => ({ ...a, idx: qhAcStep(a.items, a.idx, -1) })); return; }
      if (e.key === 'Enter' || e.key === 'Tab') {
        const it = ac.items[ac.idx];
        // Exact matches are listed so you can see the object exists, but
        // "completing" one replaces the token with itself. Don't swallow the
        // keystroke for a no-op: close the popup and let Enter make a newline
        // / Tab indent, as it would with no popup open.
        const itFrom = (it && typeof it.start === 'number') ? it.start : ac.start;
        if (!(it && value.slice(itFrom, ac.end) === it.text)) {
          e.preventDefault(); accept(it); return;
        }
        setAc(null);
      }
      if (e.key === 'Escape') { e.preventDefault(); setAc(null); return; }
    }
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

  const issueList = Array.isArray(issues) ? issues : [];
  const issuesOn = issuesShown && !ac && issueList.length > 0;
  const issueHtml = React.useMemo(() => {
    if (!issueList.length) return '';
    let out = '', pos = 0;
    issueList.forEach((r, i) => {
      const s = Math.max(r.start, pos), e = Math.min(r.end, value.length);
      if (e <= s) return;
      out += qhEscape(value.slice(pos, s)) + `<mark class="qh-issue" data-i="${i}">` + qhEscape(value.slice(s, e)) + '</mark>';
      pos = e;
    });
    return out + qhEscape(value.slice(pos)) + '\n';
  }, [value, issues]);
  const srText = issuesOn ? issueList.map(r => r.message).join(' ') : '';
  // The overlay mounts only while it shows. It must start at the textarea's
  // scroll position, or the line sits under the wrong word.
  React.useLayoutEffect(() => {
    const ta = taRef.current, el = issueRef.current;
    if (ta && el) { el.scrollTop = ta.scrollTop; el.scrollLeft = ta.scrollLeft; }
  }, [issuesOn, issueHtml]);

  // The balloon is a hover tooltip, not a menu: it goes when the pointer leaves
  // the text (see tests/test_dropdown_dismiss.py for the menu rule).
  const hideTip = () => setTip(null);
  // Find the marked word under the pointer. The textarea sits on top of the
  // overlay, so this test uses the rectangles of the marks.
  const onIssueMove = (e) => {
    const host = issueRef.current;
    if (!host) { if (tip) setTip(null); return; }
    const marks = host.querySelectorAll('mark.qh-issue');
    for (let n = 0; n < marks.length; n++) {
      const rs = marks[n].getClientRects();
      for (let j = 0; j < rs.length; j++) {
        const r = rs[j];
        if (e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom) {
          const i = Number(marks[n].getAttribute('data-i'));
          if (!tip || tip.i !== i) setTip({ i, msg: issueList[i].message, rect: { top: rs[0].top, bottom: rs[0].bottom, left: rs[0].left } });
          return;
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
        <pre className="qh-pre" ref={preRef} aria-hidden="true">
          <code dangerouslySetInnerHTML={{ __html: qhHighlight(value) + '\n' }} />
        </pre>
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
          }}
          onScroll={() => { sync(); if (hover || hoverTimer.current) hideHover(); }}
          onKeyDown={onKey}
          onPaste={onPaste}
          onKeyUp={(e) => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) refreshAC(e.target); }}
          onClick={(e) => refreshAC(e.target)}
          onBlur={() => setTimeout(() => setAc(null), 150)}
          onDragOver={onDragOver}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
        />
        {ac && ac.items.length > 0 && ReactDOM.createPortal(
          <div className={'qh-ac-ed' + (ac.cols ? ' is-cols' : '')} ref={acRef} style={{ top: ac.top, left: ac.left }}>
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
          </div>, document.body)}
        {tip && ReactDOM.createPortal(
          <div className="qh-issue-tip" role="tooltip" ref={tipRef} style={{ top: tip.rect.top, left: tip.rect.left }}>{tip.msg}</div>,
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

function EditorTabs({ tabs, activeId, onSelect, onClose, onNew, wrap, onToggleWrap, onFormat, onCloseOthers, onCloseRight, onCloseAll, onDuplicate, onRename, onCopySql, onDownloadSql, onReorder }) {
  const [kb, setKb] = React.useState(false);
  const [dragId, setDragId] = React.useState(null);
  const [overId, setOverId] = React.useState(null);
  const [kbPos, setKbPos] = React.useState(null);
  // Same fix as the export menu: the popup is rendered 6px below the button, and
  // closing on mouse-out meant crossing that gap dismissed it. See qhUseDismiss.
  const closeKb = React.useCallback(() => setKb(false), []);
  const kbWrapRef = qhUseDismiss(kb, closeKb);
  const kbBtnRef = React.useRef(null);
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
  const SHORTCUTS = [['F5', 'Run selection, else whole query'], ['F8', 'Run selection / current statement'], ['⇧ + ⌥ / Alt + S', 'Select current statement'], ['⌘ / Ctrl + ↵', 'Run'], ['F2', 'Rename tab'], ['⌘/Ctrl+⇧+T', 'Reopen closed tab'], ['Ctrl + Space', 'Suggestions; in a SELECT, all columns'], ['ssf + Tab', 'Snippet: type a key, pick it (list below)'], ['Tab', 'Indent'], ['⌥ / Alt + Z', 'Toggle word wrap'], ['⇧ + ⌥ / Alt + F', 'Format SQL'], ['⌥ / Alt + ← →', 'Previous / next result'], ['Drag tab', 'Reorder tabs'], ['Middle-click', 'Close tab'], ['Drag', 'Drop tree object into editor'], ['*', 'SELECT → expand columns'], ['Right-click', 'Tab options']];
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
        <button className="qh-kb-btn" onClick={onFormat}
          data-kbd={'Format SQL  ·  ' + ((window.QH_KBD || {}).fmt || 'Shift Alt F')}
          aria-label="Format SQL">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M4 6h16"/><path d="M4 10h10"/><path d="M4 14h16"/><path d="M4 18h10"/></svg>
        </button>
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
      <div className="qh-kb-wrap" ref={kbWrapRef}>
        <button ref={kbBtnRef} className="qh-kb-btn" onClick={() => {
          if (kb) { setKb(false); return; }
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
Object.assign(window, { SqlEditor, EditorTabs, QH_EDITOR_HINT, qhHighlight, qhBuildSuggest, qhSuggestContext, QH_REDACTED_LIT, qhRedactedAt,
  QH_SNIPPETS, QH_SNIPPET_CURSOR, QH_DRAG_BLOCK, qhSnippetsFor, qhSnippetText, qhInsertBlock, QH_KEYWORDS,
  qhTextDiff, qhEditKind, qhUndoDoc, qhUndoRecord, qhUndoSeal, qhUndoStep, qhUndoForget,
  qhSqlTokens, qhStarExpansion, qhBlockerRanges, qhFromIsName, qhFromUnquote, qhReadFromList, qhRefLookup,
  qhClTokOpen, qhColumnList, qhAcStep, qhStatementAt, qhAliasColumns, qhTabIsProd,
  qhJoinSuggestions, qhJoinOnSuggestions, qhJoinPairs, qhJoinAlias,
  qhHoverInfo, qhHoverLines, qhHoverOffsetAt });
