// Code analysis in the editor (2026-11-10, Devart H1).
//
//   * qhLint(sql, engine) reads the SQL in the browser and returns findings:
//     a range, a level (warn or info) and a message. A finding is advice. It
//     never stops a run.
//   * Each rule names the engines that get it. A rule about a write skips a
//     read-only engine (ClickHouse, Athena).
//   * Strings, quoted names and comments never match.
//   * The editor draws a finding under the word: amber wavy for warn, grey
//     dotted for info. A pre-flight blocker draws on top and wins the tooltip.
//   * A button in the tab bar turns the analysis off and counts the findings.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';
import { page, jsdomOpts, web } from './_mount.mjs';

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');

// The findings as [marked text, rule id] pairs.
const lint = (sql, eng = 'postgres') => W.qhLint(sql, eng).map(f => [sql.slice(f.start, f.end), f.message.match(/\((QA\d+)\)/)[1]]);
const ids = (sql, eng) => lint(sql, eng).map(x => x[1]);

// One sentence per rule that must hit, with the marked text it must give.
const HITS = [
  ['QA101', 'postgres', "SELECT 1 FROM t WHERE a = NULL", '= NULL'],
  ['QA101', 'postgres', "SELECT 1 FROM t WHERE (a <> NULL OR b = 1)", '<> NULL'],
  ['QA101', 'mssql', "IF @x != NULL PRINT 1", '!= NULL'],
  ['QA102', 'postgres', "SELECT CASE x WHEN NULL THEN 1 ELSE 2 END FROM t", 'WHEN NULL'],
  ['QA103', 'postgres', "SELECT 1 FROM t WHERE id NOT IN (SELECT id FROM u)", 'NOT IN'],
  ['QA104', 'postgres', "SELECT 1 FROM a, b WHERE a.id = b.id", ', b'],
  ['QA104', 'postgres', "SELECT 1 FROM shop.a x, shop.b y", ', shop.b'],
  ['QA105', 'postgres', "SELECT 1 FROM t WHERE name LIKE '%son'", "'%son'"],
  ['QA105', 'mssql', "SELECT 1 FROM t WHERE name LIKE N'%son'", "'%son'"],
  ['QA106', 'postgres', "SELECT 1 FROM t WHERE name ILIKE 'abc'", "'abc'"],
  ['QA107', 'postgres', "SELECT 1 FROM t WHERE lower(email) = 'a'", 'lower(email)'],
  ['QA107', 'postgres', "SELECT 1 FROM t WHERE x = 1 AND date_trunc('day', t.created_at) >= now()", "date_trunc('day', t.created_at)"],
  ['QA107', 'mssql', "SELECT 1 FROM t WHERE CAST(code AS int) IN (1, 2)", 'CAST(code AS int)'],
  ['QA108', 'postgres', "SELECT a FROM t UNION SELECT a FROM u", 'UNION'],
  ['QA109', 'postgres', "SELECT a, b FROM t ORDER BY 2 DESC, a", '2'],
  ['QA110', 'postgres', "INSERT INTO shop.orders VALUES (1, 2)", 'INSERT INTO shop.orders'],
  ['QA110', 'mssql', "INSERT orders SELECT * FROM staging", 'INSERT orders'],
  ['QA111', 'postgres', "DELETE FROM orders", 'DELETE'],
  ['QA111', 'mssql', "UPDATE orders SET total = 0", 'UPDATE'],
  ['QA111', 'postgres', "WITH x AS (SELECT 1) UPDATE orders SET total = 0", 'UPDATE'],
  ['QA201', 'postgres', "SELECT pg_sleep(5)", 'pg_sleep'],
  ['QA202', 'postgres', "CREATE TABLE t (id int, amount money)", 'money'],
  ['QA203', 'postgres', "CREATE TABLE t (code char(3))", 'char(3)'],
  ['QA204', 'postgres', "ALTER TABLE t ADD COLUMN seen_at timestamp(3)", 'timestamp(3)'],
  ['QA204', 'postgres', "CREATE TABLE t (at TIMESTAMP WITHOUT TIME ZONE)", 'TIMESTAMP WITHOUT TIME ZONE'],
  ['QA301', 'mssql', "SELECT * FROM orders WITH (NOLOCK)", 'NOLOCK'],
  ['QA301', 'mssql', "SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED", 'READ UNCOMMITTED'],
  ['QA302', 'mssql', "SELECT @@IDENTITY", '@@IDENTITY'],
  ['QA303', 'mssql', "CREATE TABLE t (note ntext)", 'ntext'],
  ['QA304', 'mssql', "WAITFOR DELAY '00:00:05'", 'WAITFOR DELAY'],
  ['QA401', 'clickhouse', "SELECT count(DISTINCT user_id) FROM events", 'count(DISTINCT'],
  ['QA402', 'clickhouse', "SELECT * FROM events FINAL", 'FINAL'],
  ['QA501', 'athena', "SELECT * FROM logs LIMIT 10", '*'],
  ['QA501', 'athena', "SELECT a, * FROM logs LIMIT 10", '*'],
  ['QA502', 'athena', "SELECT a FROM logs ORDER BY a", 'ORDER BY'],
];

// ---------- the rules ----------

test('every rule has an id, a level, a title and an engine list', () => {
  const seen = new Set();
  for (const r of W.QH_LINT_RULES) {
    assert.match(r.id, /^QA\d{3}$/, r.id);
    assert.ok(!seen.has(r.id), r.id + ' is unique');
    seen.add(r.id);
    assert.ok(r.level === 'warn' || r.level === 'info', r.id + ' level');
    assert.ok(r.title && r.title.length <= 40, r.id + ' title');
    assert.ok(r.engines === null || (Array.isArray(r.engines) && r.engines.length), r.id + ' engines');
    assert.equal(typeof r.check, 'function');
  }
});

test('each rule marks the words it is about', () => {
  for (const [id, eng, sql, mark] of HITS) {
    const got = lint(sql, eng);
    assert.ok(got.some(([m, i]) => i === id && m === mark), `${id} on ${eng}: ${sql}\n  got ${JSON.stringify(got)}`);
  }
});

test('every rule has at least one case above', () => {
  const covered = new Set(HITS.map(h => h[0]));
  for (const r of W.QH_LINT_RULES) assert.ok(covered.has(r.id), r.id + ' has a case');
});

test('each message is plain: a title line, then short sentences', () => {
  for (const [, eng, sql] of HITS) {
    for (const f of W.qhLint(sql, eng)) {
      for (const part of f.message.split('\n\n')) {
        const [title, body] = part.split('\n');
        assert.match(title, /^.+ \(QA\d{3}\)$/);
        for (const s of body.split(/(?<=\.)\s+/)) {
          assert.ok(s.split(/\s+/).length <= 20, 'at most 20 words: ' + s);
        }
      }
    }
  }
});

// ---------- what must not match ----------

test('an assignment, an alias and IS NULL are not a comparison with NULL', () => {
  for (const [sql, eng] of [
    ["UPDATE t SET a = NULL, b = NULL WHERE id = 1", 'postgres'],
    ["SELECT 1 FROM t WHERE a IS NULL AND b IS NOT NULL", 'postgres'],
    ["DECLARE @x int = NULL", 'mssql'],
    ["SELECT @x = NULL", 'mssql'],
    ["SET @x = NULL", 'mssql'],
    ["SELECT CASE WHEN a IS NULL THEN 1 END FROM t", 'postgres'],
  ]) assert.deepEqual(ids(sql, eng).filter(i => i === 'QA101' || i === 'QA102'), [], sql);
});

test('a string, a quoted name and a comment never match', () => {
  const sql = "SELECT 'x = NULL', \"a = NULL\" FROM t -- WHERE b = NULL\n/* DELETE FROM t */ WHERE c LIKE 'a%' LIMIT 1";
  assert.deepEqual(lint(sql), []);
});

test('the forms that are fine give no finding', () => {
  for (const [sql, eng] of [
    ["SELECT extract(year FROM d) FROM t", 'postgres'],
    ["SELECT 1 FROM t WHERE a IS DISTINCT FROM b", 'postgres'],
    ["SELECT 1 FROM t, LATERAL jsonb_array_elements(t.x) e", 'postgres'],
    ["SELECT 1 FROM t, unnest(t.tags) tag", 'postgres'],
    ["SELECT a FROM t UNION ALL SELECT a FROM u", 'postgres'],
    ["SELECT 1 FROM t WHERE email = lower('A')", 'postgres'],
    ["SELECT 1 FROM t WHERE name LIKE 'abc%'", 'postgres'],
    ["SELECT 1 FROM t WHERE name LIKE '[ab]c'", 'mssql'],
    ["SELECT a FROM t ORDER BY a + 1, b", 'postgres'],
    ["SELECT a FROM t LIMIT 10 OFFSET 20", 'postgres'],
    ["INSERT INTO t (a, b) VALUES (1, 2)", 'postgres'],
    ["INSERT INTO t DEFAULT VALUES", 'postgres'],
    ["UPDATE t SET a = 1 WHERE id = 2", 'postgres'],
    ["DELETE FROM t WHERE id = 2", 'mssql'],
    ["UPDATE STATISTICS orders", 'mssql'],
    ["CREATE TABLE t (timestamp int, text int, ok timestamptz, at TIMESTAMP WITH TIME ZONE, n CHARACTER VARYING(5))", 'postgres'],
    ["CREATE TABLE t (text int, note nvarchar(max))", 'mssql'],
    ["SELECT money, char_length(a) FROM t", 'postgres'],
    ["CREATE VIEW v AS SELECT price money FROM t", 'postgres'],
    ["SELECT count(*) FROM logs ORDER BY 1 LIMIT 5", 'athena'],
    ["SELECT t.a FROM logs t LIMIT 5", 'athena'],
    ["SELECT uniq(user_id) FROM events", 'clickhouse'],
  ]) {
    const got = lint(sql, eng).filter(([, i]) => i !== 'QA109' || !/count/.test(sql));
    assert.deepEqual(got, [], `${eng}: ${sql}`);
  }
});

// ---------- engines ----------

test('a rule runs only on its own engines', () => {
  assert.deepEqual(ids("SELECT pg_sleep(1)", 'mssql'), []);
  assert.deepEqual(ids("SELECT * FROM t WITH (NOLOCK)", 'postgres'), []);
  assert.deepEqual(ids("SELECT count(DISTINCT a) FROM t", 'postgres'), []);
  assert.deepEqual(ids("SELECT * FROM logs LIMIT 1", 'postgres'), []);
  assert.deepEqual(ids("SELECT * FROM logs LIMIT 1", 'clickhouse'), []);
});

test('a write rule skips a read-only engine', () => {
  for (const eng of ['clickhouse', 'athena']) {
    assert.deepEqual(ids("DELETE FROM t", eng), [], eng);
    assert.deepEqual(ids("INSERT INTO t VALUES (1)", eng), [], eng);
  }
  assert.deepEqual(ids("DELETE FROM t", 'mysql'), ['QA111'], 'an engine with no rules of its own gets the common ones');
});

test('each statement is read on its own', () => {
  const sql = "DELETE FROM a WHERE id = 1;\nDELETE FROM b";
  const got = W.qhLint(sql, 'postgres');
  assert.equal(got.length, 1);
  assert.equal(sql.slice(got[0].start, got[0].end), 'DELETE');
  assert.equal(got[0].start, sql.lastIndexOf('DELETE'), 'the finding is on the second statement');
});

test('a finding that two rules or two passes give shows once', () => {
  const sql = "SELECT 1 FROM t WHERE id IN (SELECT id FROM u WHERE lower(n) = 'a')";
  const got = W.qhLint(sql, 'postgres');
  assert.equal(got.length, 1, 'the inner WHERE is inside the outer one, and it counts once');
  assert.equal(sql.slice(got[0].start, got[0].end), 'lower(n)');
  assert.equal(got[0].message.split('\n\n').length, 1);
});

test('findings come sorted and never overlap', () => {
  const sql = "SELECT a FROM t, u WHERE a = NULL AND lower(b) LIKE '%x' UNION SELECT 1 ORDER BY 1";
  const got = W.qhLint(sql, 'postgres');
  assert.ok(got.length >= 5);
  for (let i = 1; i < got.length; i++) assert.ok(got[i].start >= got[i - 1].end, 'no overlap at ' + i);
});

test('empty text gives nothing', () => {
  assert.deepEqual(W.qhLint('', 'postgres'), []);
  assert.deepEqual(W.qhLint('   \n', 'mssql'), []);
  assert.deepEqual(W.qhLint(null, 'athena'), []);
});

// ---------- the editor ----------

function mountEditor(value, props) {
  const ui = page(['qh-data.jsx', 'qh-editor.jsx']);
  ui.render(ui.React.createElement(ui.win.SqlEditor, {
    value, docId: 't1', fontSize: 13, engineId: 'postgres', onChange: () => {}, ...props }));
  return ui;
}

test('the editor draws a finding in its own layer, under the blocker layer', jsdomOpts, () => {
  const sql = "SELECT 1 FROM t WHERE a = NULL";
  const lints = W.qhLint(sql, 'postgres');
  const issues = [{ start: 0, end: 6, message: 'blocked' }];
  const ui = mountEditor(sql, { lints, issues });
  const marks = ui.all('.qh-lints mark.qh-lint');
  assert.equal(marks.length, 1);
  assert.equal(marks[0].textContent, '= NULL');
  assert.ok(marks[0].classList.contains('lv-warn'));
  const layers = ui.all('.qh-code-wrap > pre').map(p => p.className);
  assert.deepEqual(layers, ['qh-pre', 'qh-pre qh-lints', 'qh-pre qh-issues'], 'the blocker layer comes last, so it draws on top');
});

test('an info finding gets the info class, and no finding means no layer', jsdomOpts, () => {
  const sql = "SELECT a FROM t UNION SELECT a FROM u";
  const ui = mountEditor(sql, { lints: W.qhLint(sql, 'postgres') });
  assert.ok(ui.all('.qh-lints mark.qh-lint.lv-info').length === 1);
  const ui2 = mountEditor(sql, { lints: [] });
  assert.equal(ui2.all('.qh-lints').length, 0);
});

// ---------- the switch ----------

function mountTabs(props) {
  const ui = page(['qh-data.jsx', 'qh-modal.jsx', 'qh-editor.jsx']);
  const noop = () => {};
  ui.render(ui.React.createElement(ui.win.EditorTabs, {
    tabs: [{ id: 'a', name: 'shop (srv-alpha)', conn: 'srv-alpha', db: 'shop', tier: 'RO' }], activeId: 'a',
    onSelect: noop, onClose: noop, onNew: noop, wrap: false, onToggleWrap: noop, onFormat: noop,
    onCloseOthers: noop, onCloseRight: noop, onCloseAll: noop, onDuplicate: noop, onRename: noop,
    onCopySql: noop, onDownloadSql: noop, onReorder: noop, ...props }));
  return ui;
}

test('the tab-bar button turns the analysis off and counts the findings', jsdomOpts, () => {
  let n = 0;
  const ui = mountTabs({ lint: true, lintCount: 3, onToggleLint: () => { n++; } });
  const btn = ui.doc.querySelector('.qh-lintt');
  assert.ok(btn, 'the button is there');
  assert.equal(btn.getAttribute('aria-pressed'), 'true');
  assert.equal(btn.getAttribute('aria-label'), 'Code analysis on');
  assert.equal(btn.querySelector('.qh-lint-n').textContent, '3');
  assert.equal(btn.getAttribute('data-kbd'), 'Code analysis  ·  3 findings');
  ui.click(btn);
  assert.equal(n, 1);
  const off = mountTabs({ lint: false, lintCount: 0, onToggleLint: () => {} }).doc.querySelector('.qh-lintt');
  assert.equal(off.getAttribute('aria-pressed'), 'false');
  assert.equal(off.querySelector('.qh-lint-n'), null, 'no count while off');
  assert.equal(off.getAttribute('data-kbd'), 'Code analysis  ·  off');
});

test('the app keeps the switch in this browser, on by default, and passes the findings', () => {
  const app = web('qh-app.jsx');
  assert.match(app, /localStorage\.getItem\('qh\.lint\.v1'\) !== '0'/, 'on until the person turns it off');
  assert.match(app, /lint \? qhLint\(lintText, editorEngine\) : \[\]/);
  assert.match(app, /lints=\{lintRanges\}/);
  assert.match(app, /lintCount=\{lintRanges\.length\}/);
  // The state must exist before the memo reads it, or the app throws.
  assert.ok(app.indexOf('const [lint, setLint]') < app.indexOf('const lintRanges'), 'state before its first use');
});

test('the styles exist', () => {
  const html = web('QueryHub.html');
  assert.match(html, /\.qh-lints \{ color: transparent; pointer-events: none; \}/);
  assert.match(html, /\.qh-lint \{[^}]*underline wavy var\(--stroke-warning\)/);
  assert.match(html, /\.qh-lint\.lv-info \{[^}]*dotted/);
  assert.match(html, /\.qh-issue-tip\.is-lint \{/);
  assert.match(html, /\.qh-lint-n \{/);
});
