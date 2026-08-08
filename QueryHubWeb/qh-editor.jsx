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

// Tokenize + return highlighted HTML
function qhHighlight(code) {
  // master regex: comments | strings | numbers | words | other
  const re = /(--[^\n]*|\/\*[\s\S]*?\*\/)|('(?:[^']|'')*')|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)|([\s\S])/g;
  let out = '';
  let m;
  while ((m = re.exec(code)) !== null) {
    if (m[1] != null) out += `<span class="tk-com">${qhEscape(m[1])}</span>`;
    else if (m[2] != null) out += `<span class="tk-str">${qhEscape(m[2])}</span>`;
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
  if (dot && tableMode) {
    // `schema.tbl|` inside FROM/JOIN/UPDATE...: a column cannot appear here at
    // all, so offering them is what made the popup look broken.
    add(schema.tables, 'table', 0);
    add(schema.systemTables, 'system', 1);
  }
  else if (dot) {
    // `alias.col|` in a select list / WHERE: columns first, tables still
    // offered because an alias cannot be resolved from the text alone.
    add(schema.columns, 'column', 0);
    add(schema.tables, 'table', 1);
  }
  else if (tableMode) { add(schema.tables, 'table', 0); add(schema.systemTables, 'system', 1); add(schema.dbs, 'database', 2); }
  else { add(schema.tables, 'table', 0); add(schema.columns, 'column', 1); add(schema.systemTables, 'system', 2); add(QH_KEYWORDS, 'keyword', 3); }
  // Sort BEFORE truncating: the old code capped in insertion order, so a
  // matching table could be pushed out of the list by ten columns.
  items.sort((a, b) => (a.rank - b.rank) || (a.typeRank - b.typeRank));
  const seen = new Set(); const out = [];
  for (const it of items) { const k = it.type + ':' + it.text; if (!seen.has(k)) { seen.add(k); out.push(it); } if (out.length >= 12) break; }
  return out.length ? { items: out, start, end: start + token.length } : null;
}
const QH_AC_TYPE_LABEL = { keyword: 'kw', table: 'table', column: 'col', database: 'db', system: 'system', expand: 'all cols' };

function SqlEditor({ value, onChange, fontSize, onRun, onRunSelection, selectionGetter, schema, engineId, focusSignal }) {
  const taRef = React.useRef(null);
  const preRef = React.useRef(null);
  const gutRef = React.useRef(null);
  const measRef = React.useRef(null);
  const [charW, setCharW] = React.useState(fontSize * 0.6);
  const [ac, setAc] = React.useState(null); // {items, idx, top, left, start, end}
  const [dragOver, setDragOver] = React.useState(false);
  const lines = value.split('\n');
  const lh = Math.round(fontSize * 1.55);
  const sch = schema || { tables: [], columns: [], dbs: [] };

  const indexFromPoint = (ta, x, y) => {
    const r = ta.getBoundingClientRect();
    const row = Math.max(0, Math.floor((y - r.top - 14 + ta.scrollTop) / lh));
    const col = Math.max(0, Math.round((x - r.left - 16 + ta.scrollLeft) / charW));
    const ls = value.split('\n');
    const rr = Math.min(row, ls.length - 1);
    let idx = 0; for (let i = 0; i < rr; i++) idx += ls[i].length + 1;
    return idx + Math.min(col, ls[rr].length);
  };
  const onDrop = (e) => {
    const text = e.dataTransfer.getData('text/plain');
    if (!text) return;
    e.preventDefault(); setDragOver(false);
    const ta = taRef.current;
    const idx = indexFromPoint(ta, e.clientX, e.clientY);
    const nv = value.slice(0, idx) + text + value.slice(idx);
    onChange(nv);
    requestAnimationFrame(() => { if (ta) { ta.focus(); ta.selectionStart = ta.selectionEnd = idx + text.length; } });
  };
  const onDragOver = (e) => {
    if (Array.from(e.dataTransfer.types || []).includes('text/plain')) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; if (!dragOver) setDragOver(true); }
  };

  React.useLayoutEffect(() => {
    if (measRef.current) setCharW(measRef.current.getBoundingClientRect().width / 10);
  }, [fontSize]);

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

  const sync = () => {
    const ta = taRef.current;
    if (preRef.current) { preRef.current.scrollTop = ta.scrollTop; preRef.current.scrollLeft = ta.scrollLeft; }
    if (gutRef.current) gutRef.current.scrollTop = ta.scrollTop;
    if (ac) setAc(null);
  };

  const posAt = (ta, idx) => {
    const rows = ta.value.slice(0, idx).split('\n');
    const row = rows.length - 1, col = rows[row].length;
    return { top: 14 + (row + 1) * lh - ta.scrollTop + 2, left: 16 + col * charW - ta.scrollLeft };
  };
  const refreshAC = (ta) => {
    const caret = ta.selectionStart;
    if (caret !== ta.selectionEnd) { setAc(null); return; }
    const before = ta.value.slice(0, caret);
    // SELECT * expansion: caret right after a select-list star
    if (before.endsWith('*') && /\bselect\b/i.test(before) && !/\bfrom\b[^*]*$/i.test(before)) {
      const fm = ta.value.match(/\bfrom\s+([a-z0-9_.]+)/i);
      const tbl = fm ? fm[1].split('.').pop() : null;
      const cols = (tbl && sch.tableCols && sch.tableCols[tbl]) ? sch.tableCols[tbl] : sch.columns;
      if (cols && cols.length) {
        const p = posAt(ta, caret - 1);
        const quote = (n) => (typeof qhQuoteIdentFor === 'function' ? qhQuoteIdentFor(n, engineId) : qhQuoteIdent(n));
        setAc({ items: [{ text: cols.map(quote).join(', '), label: 'Expand * → ' + cols.length + ' columns', type: 'expand' }], idx: 0, top: p.top, left: p.left, start: caret - 1, end: caret });
        return;
      }
    }
    const s = qhBuildSuggest(ta.value, caret, sch, engineId);
    if (!s) { setAc(null); return; }
    const p = posAt(ta, s.start);
    setAc({ items: s.items, idx: 0, top: p.top, left: p.left, start: s.start, end: s.end });
  };

  const accept = (item) => {
    const ta = taRef.current;
    // The item may carry its own start: a qualified insert replaces the
    // qualifier the user already typed rather than appending behind it.
    const from = (item && typeof item.start === 'number') ? item.start : ac.start;
    const nv = value.slice(0, from) + item.text + value.slice(ac.end);
    const caret = from + item.text.length;
    setAc(null);
    onChange(nv);
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
    onChange(rest);
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
    setAc(null);
    onChange(v.slice(0, st) + txt + v.slice(st));
    const next = caret + txt.length;
    requestAnimationFrame(() => { ta.selectionStart = ta.selectionEnd = next; });
  };

  const onKey = (e) => {
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
      onChange(value.slice(0, s) + '  ' + value.slice(en));
      requestAnimationFrame(() => { ta.selectionStart = ta.selectionEnd = s + 2; });
    } else if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault(); setAc(null); onRun && onRun();
    }
  };

  return (
    <div className="qh-editor" style={{ '--ed-fs': fontSize + 'px', '--ed-lh': lh + 'px' }}>
      <span ref={measRef} className="qh-meas" aria-hidden="true">0000000000</span>
      <div className="qh-gutter" ref={gutRef}>
        {lines.map((_, i) => <div key={i} className="qh-lno">{i + 1}</div>)}
      </div>
      <div className={'qh-code-wrap' + (dragOver ? ' is-drop' : '')}>
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
          onChange={(e) => { onChange(e.target.value); refreshAC(e.target); }}
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
        {ac && ac.items.length > 0 && (
          <div className="qh-ac-ed" style={{ top: ac.top, left: ac.left }}>
            {ac.items.map((it, i) => (
              <div key={it.type + it.text} className={'qh-ac-ed-opt' + (i === ac.idx ? ' is-hi' : '')}
                onMouseEnter={() => setAc(a => ({ ...a, idx: i }))}
                onMouseDown={(e) => { e.preventDefault(); accept(it); }}>
                <span className="qh-ac-ed-name">{it.label}</span>
                <span className={'qh-ac-ed-type ac-t-' + it.type}>{QH_AC_TYPE_LABEL[it.type]}</span>
              </div>
            ))}
          </div>
        )}
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

function EditorTabs({ tabs, activeId, onSelect, onClose, onNew, onCloseOthers, onCloseRight, onCloseAll, onDuplicate, onRename, onCopySql, onDownloadSql, onReorder }) {
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
  const SHORTCUTS = [['F5', 'Run selection, else whole query'], ['F8', 'Run selection / current line'], ['⌘ / Ctrl + ↵', 'Run'], ['F2', 'Rename tab'], ['⌘/Ctrl+⇧+T', 'Reopen closed tab'], ['Tab', 'Indent'], ['Drag tab', 'Reorder tabs'], ['Middle-click', 'Close tab'], ['Drag', 'Drop tree object into editor'], ['*', 'SELECT → expand columns'], ['Right-click', 'Tab options']];
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
      </div>
      <div className="qh-tabs-fixed">
      <button className="qh-tab-new" onClick={onNew} data-kbd={(window.QH_KBD || {}).newq} aria-label="New query">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M12 5v14M5 12h14"/></svg>
      </button>
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

Object.assign(window, { SqlEditor, EditorTabs, qhHighlight });
