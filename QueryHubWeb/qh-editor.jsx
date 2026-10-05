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

// Build schema-aware + keyword suggestions for the token left of the caret.
const QH_KW_AFTER_TABLE = new Set(['from', 'join', 'into', 'update', 'table', 'truncate', 'describe']);
function qhBuildSuggest(value, caret, schema, engineId) {
  const before = value.slice(0, caret);
  const m = before.match(/[A-Za-z_][A-Za-z0-9_]*$/);
  if (!m) return null;
  const token = m[0];
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
  const tableMode = QH_KW_AFTER_TABLE.has(dot ? gov : prev);
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
  const add = (arr, type, typeRank) => (arr || []).forEach(n => {
    const nl = n.toLowerCase();
    let rank;
    if (nl === lc) rank = 0;
    else if (nl.startsWith(lc)) rank = 1;
    else if (type !== 'keyword' && nl.indexOf(lc) > 0) rank = 2;
    else return;
    const text = insertFor(n, type);
    const it = { text, label: n, type, rank, typeRank };
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

  if (dot && tableMode) {
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
  // Sort BEFORE truncating: the old code capped in insertion order, so a
  // matching table could be pushed out of the list by ten columns.
  items.sort((a, b) => (a.rank - b.rank) || (a.typeRank - b.typeRank));
  const seen = new Set(); const out = [];
  for (const it of items) { const k = it.type + ':' + it.text; if (!seen.has(k)) { seen.add(k); out.push(it); } if (out.length >= 12) break; }
  return out.length ? { items: out, start, end: start + token.length } : null;
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
function qhSqlTokens(sql) {
  const out = [];
  const re = /(--[^\n]*|\/\*[\s\S]*?(?:\*\/|$))|('(?:[^']|'')*'?)|("(?:[^"]|"")*"?|\[[^\]]*\]?)|(\$[A-Za-z_]*\$)|([A-Za-z_][A-Za-z0-9_$]*)|(\d+(?:\.\d+)?)|(\s+)|([\s\S])/g;
  let m;
  while ((m = re.exec(sql)) !== null) {
    const s = m.index;
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
  let f = -1;
  for (let i = at + 1; i < S.length; i++) {
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
  const name = (j) => top(j) && isName(F[j].t);
  // The length of a join keyword run at j: 0 when there is none, -1 for a join
  // whose columns cannot be listed exactly (NATURAL, APPLY).
  const joinLen = (j) => {
    if (word(j, 'NATURAL') || (word(j, 'CROSS', 'OUTER') && word(j + 1, 'APPLY'))) return -1;
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
    const r = readRef();
    if (!r) return null;
    refs.push(r);
    if (joined) {
      if (word(i, 'USING')) return null;
      if (word(i, 'ON')) {
        i++;
        while (i < F.length && !(top(i) && (F[i].t.v === ',' || joinLen(i) !== 0))) i++;
      }
    }
    if (i >= F.length) break;
  }

  const unq = (t) => (t.k !== 'qid' ? t.v
    : t.v[0] === '"' ? t.v.slice(1, -1).replace(/""/g, '"') : t.v.slice(1, -1).replace(/\]\]/g, ']'));
  const sch = schema || {};
  const colsOf = (r) => {
    const parts = r.parts.map(unq), exact = r.parts.some(p => p.k === 'qid');
    let cols;
    if (parts.length >= 2) cols = qhLookupCi(sch.tableColsQ, parts[parts.length - 2] + '.' + parts[parts.length - 1], exact);
    // A bare name two schemas share is left to the server, which resolves it
    // with the search_path the query itself will run under.
    else if (qhLookupCi(sch.qualify, parts[0], exact) !== null) cols = qhLookupCi(sch.tableCols, parts[0], exact);
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
const QH_AC_TYPE_LABEL ={ keyword: 'kw', table: 'table', column: 'col', database: 'db', system: 'system', function: 'fn', expand: 'all cols' };

function SqlEditor({ value, onChange, fontSize, wrap, onRun, onRunSelection, selectionGetter, schema, engineId, focusSignal, revealRange, docId }) {
  const taRef = React.useRef(null);
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
  // Which character a pointer is over on a wrapped line: the visual row decides
  // first (vertical distance dominates the score), the column within it second.
  const wrapColAt = (row, relX, relY) => {
    const el = wrapRef.current && wrapRef.current.children[row];
    const node = el && el.firstChild;
    const len = (value.split('\n')[row] || '').length;
    if (!node || !len) return 0;
    let best = 0, bestScore = Infinity;
    for (let c = 0; c <= len; c++) {
      const p = wrapRect(row, c); if (!p) break;
      const score = Math.abs(p.bottom - lh / 2 - relY) * 10000 + Math.abs(p.right - relX);
      if (score < bestScore) { bestScore = score; best = c; }
    }
    return best;
  };
  const indexFromPoint = (ta, x, y) => {
    const r = ta.getBoundingClientRect();
    const relY = y - r.top - 14 + ta.scrollTop, relX = x - r.left - 16 + ta.scrollLeft;
    const ls = value.split('\n');
    let rr, col;
    if (wrap && wrapRef.current && wrapRef.current.children.length === ls.length) {
      rr = ls.length - 1;
      for (let i = 0; i < ls.length; i++) {
        const c = wrapRef.current.children[i];
        if (relY < c.offsetTop + c.offsetHeight) { rr = i; break; }
      }
      col = wrapColAt(rr, relX, relY);
    } else {
      rr = Math.min(Math.max(0, Math.floor(relY / lh)), ls.length - 1);
      col = Math.min(Math.max(0, Math.round(relX / charW)), ls[rr].length);
    }
    let idx = 0; for (let i = 0; i < rr; i++) idx += ls[i].length + 1;
    return idx + col;
  };
  const onDrop = (e) => {
    const text = e.dataTransfer.getData('text/plain');
    if (!text) return;
    e.preventDefault(); setDragOver(false);
    const ta = taRef.current;
    const idx = indexFromPoint(ta, e.clientX, e.clientY);
    const nv = value.slice(0, idx) + text + value.slice(idx);
    commit(nv, 'drop', [ta.selectionStart, ta.selectionEnd], [idx + text.length, idx + text.length]);
    requestAnimationFrame(() => { if (ta) { ta.focus(); ta.selectionStart = ta.selectionEnd = idx + text.length; } });
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

  // Focus the textarea when the parent bumps focusSignal (e.g. a new query
  // tab or an opened table) so you can start typing without a click.
  React.useEffect(() => {
    if (!focusSignal) return;
    requestAnimationFrame(() => { const ta = taRef.current; if (ta) { ta.focus(); ta.selectionStart = ta.selectionEnd = ta.value.length; } });
  }, [focusSignal]);

  // Select a span the parent points at — the hidden password, when Run stopped
  // on it — so the next keystroke replaces the placeholder.
  React.useEffect(() => {
    if (!revealRange) return;
    requestAnimationFrame(() => { const ta = taRef.current; if (ta) { ta.focus(); ta.setSelectionRange(revealRange.start, revealRange.end); } });
  }, [revealRange]);

  const sync = () => {
    const ta = taRef.current;
    if (preRef.current) { preRef.current.scrollTop = ta.scrollTop; preRef.current.scrollLeft = ta.scrollLeft; }
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
  const refreshAC = (ta, afterLookup) => {
    const caret = ta.selectionStart;
    if (caret !== ta.selectionEnd) { setAc(null); return; }
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
            && document.activeElement === t) refreshAC(t, true);
      });
    }
    const s = qhBuildSuggest(ta.value, caret, sch, engineId);
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
    const caret = from + item.text.length;
    setAc(null);
    commit(nv, 'complete', ta ? [ta.selectionStart, ta.selectionEnd] : null, [caret, caret]);
    requestAnimationFrame(() => { if (ta) { ta.focus(); ta.selectionStart = ta.selectionEnd = caret; } });
  };

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

  const onKey = (e) => {
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
    if (e.key === 'F5') { e.preventDefault(); setAc(null); onRun && onRun(); return; }
    if (e.key === 'F8') {
      e.preventDefault(); setAc(null);
      const ta = e.target; let s = ta.value.slice(ta.selectionStart, ta.selectionEnd);
      if (!s.trim()) { const v = ta.value; const st = v.lastIndexOf('\n', ta.selectionStart - 1) + 1; let en = v.indexOf('\n', ta.selectionStart); if (en < 0) en = v.length; s = v.slice(st, en); }
      onRunSelection && onRunSelection(s); return;
    }
    if (ac && ac.items.length) {
      if (e.key === 'ArrowDown') { e.preventDefault(); setAc(a => ({ ...a, idx: (a.idx + 1) % a.items.length })); return; }
      if (e.key === 'ArrowUp') { e.preventDefault(); setAc(a => ({ ...a, idx: (a.idx - 1 + a.items.length) % a.items.length })); return; }
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
        <textarea
          ref={taRef}
          className="qh-ta"
          value={value}
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
          onScroll={sync}
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
          <div className="qh-ac-ed" ref={acRef} style={{ top: ac.top, left: ac.left }}>
            {ac.items.map((it, i) => (
              <div key={it.type + it.text} className={'qh-ac-ed-opt' + (i === ac.idx ? ' is-hi' : '')}
                onMouseEnter={() => setAc(a => ({ ...a, idx: i }))}
                onMouseDown={(e) => { e.preventDefault(); accept(it); }}>
                <span className="qh-ac-ed-name">{it.label}</span>
                <span className={'qh-ac-ed-type ac-t-' + it.type}>{QH_AC_TYPE_LABEL[it.type]}</span>
              </div>
            ))}
          </div>, document.body)}
      </div>
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

function EditorTabs({ tabs, activeId, onSelect, onClose, onNew, wrap, onToggleWrap, onCloseOthers, onCloseRight, onCloseAll, onDuplicate, onRename, onCopySql, onDownloadSql, onReorder }) {
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
  const SHORTCUTS = [['F5', 'Run selection, else whole query'], ['F8', 'Run selection / current line'], ['⌘ / Ctrl + ↵', 'Run'], ['F2', 'Rename tab'], ['⌘/Ctrl+⇧+T', 'Reopen closed tab'], ['Tab', 'Indent'], ['⌥ / Alt + Z', 'Toggle word wrap'], ['⌥ / Alt + ← →', 'Previous / next result'], ['Drag tab', 'Reorder tabs'], ['Middle-click', 'Close tab'], ['Drag', 'Drop tree object into editor'], ['*', 'SELECT → expand columns'], ['Right-click', 'Tab options']];
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
        <div key={t.id} className={'qh-tab' + (t.id === activeId ? ' is-active' : '') + (overId === t.id ? ' is-drop' : '') + (dragId === t.id ? ' is-dragging' : '')} onClick={() => onSelect(t.id)}
          // DESIGN: the request id only sits in the strip on the ACTIVE tab — the
          // one whose number you would quote. On the others it moves to hover,
          // because in-flow-on-hover would resize the tab and shift the strip.
          title={t.id !== activeId && t.reqId ? t.name + ' · request #' + t.reqId : undefined}
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
            <span className={'qh-tab-dot tier-' + t.tier.toLowerCase()} />
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
          {editId !== t.id && !t.kind && t.reqId && t.id === activeId && (
            // The request id, known from the moment the tab opened — the number
            // the requester quotes, watches and cancels by. A chip rather than
            // part of the name, so renaming a tab cannot lose or fake it.
            <span className="qh-tab-req" title={'Request #' + t.reqId}>#{t.reqId}</span>
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
Object.assign(window, { SqlEditor, EditorTabs, qhHighlight, qhBuildSuggest, QH_REDACTED_LIT, qhRedactedAt,
  qhTextDiff, qhEditKind, qhUndoDoc, qhUndoRecord, qhUndoSeal, qhUndoStep, qhUndoForget,
  qhSqlTokens, qhStarExpansion });
