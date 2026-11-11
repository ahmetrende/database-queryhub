// Keyword phrases and table aliases (2026-11-11, Devart A5 and A19).
//
//   * Each engine has its own keywords (qhKeywordsFor): SQL Server gets TOP and
//     loses LIMIT, ClickHouse gets PREWHERE, Athena gets UNNEST.
//   * A phrase is one item in the typing list: `ord` gives ORDER BY. It shows
//     only where it can stand (qhKwPhraseFits).
//   * A table picked after FROM or JOIN gets an alias (qhAutoAlias): `oi` for
//     order_items. Rules `mask = alias` name the alias. The switch and the
//     rules live in the Format options popup (qh.alias.v1).
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { bareWindow, loadInto } from './_load.mjs';
import { web } from './_mount.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');

const SCHEMA = {
  tables: ['orders', 'users', 'order_items', 'audit_log'],
  columns: ['id', 'user_id', 'total'],
  dbs: [],
  tableCols: { orders: ['id', 'user_id', 'total'], users: ['id'] },
};
const split = (sql) => [sql.replace('|', ''), sql.indexOf('|')];
const suggest = (sql, eng = 'postgres') => { const [t, c] = split(sql); return W.qhBuildSuggest(t, c, SCHEMA, eng); };
const kws = (sql, eng) => { const s = suggest(sql, eng); return s ? s.items.filter(i => i.type === 'keyword').map(i => i.text) : []; };
// The alias for the word before `|`, as a pick of `table` would add it.
const alias = (sql, table, eng = 'postgres', rules, qualified) => {
  const [t, c] = split(sql);
  const m = t.slice(0, c).match(/[A-Za-z_][A-Za-z0-9_]*$/);
  const start = m ? c - m[0].length : c;
  return W.qhAutoAlias(t, start, c, table, eng, rules, qualified);
};

// ---------- keywords per engine ----------

test('each engine gets its own keywords and loses the ones it does not know', () => {
  const pg = W.qhKeywordsFor('postgres'), ms = W.qhKeywordsFor('mssql');
  const ch = W.qhKeywordsFor('clickhouse'), at = W.qhKeywordsFor('athena');
  assert.ok(pg.includes('LIMIT') && pg.includes('ILIKE') && pg.includes('LATERAL'));
  assert.ok(!pg.includes('TOP'));
  assert.ok(ms.includes('TOP') && ms.includes('APPLY') && ms.includes('OUTPUT'));
  assert.ok(!ms.includes('LIMIT') && !ms.includes('ILIKE') && !ms.includes('RETURNING'), 'SQL Server has no LIMIT, ILIKE or RETURNING');
  assert.ok(ch.includes('PREWHERE') && ch.includes('FINAL') && ch.includes('QUALIFY'));
  assert.ok(at.includes('UNNEST') && !at.includes('ILIKE'));
  for (const list of [pg, ms, ch, at]) assert.equal(new Set(list).size, list.length, 'no word twice');
});

test('the typing list uses the engine keywords', () => {
  assert.ok(kws('SELECT t|', 'mssql').includes('TOP'));
  assert.ok(!kws('SELECT t|', 'postgres').includes('TOP'));
  assert.ok(kws('SELECT * FROM t pre|', 'clickhouse').includes('PREWHERE'));
  assert.ok(!kws('SELECT * FROM t li|', 'mssql').includes('LIMIT'));
  assert.ok(kws('SELECT * FROM t li|', 'postgres').includes('LIMIT'));
});

// ---------- phrases ----------

test('a phrase is one item, ranked above its first word', () => {
  const k = kws('SELECT * FROM t ord|');
  assert.ok(k.includes('ORDER BY'));
  const exact = kws('SELECT * FROM t order|');
  assert.ok(exact.indexOf('ORDER BY') >= 0 && exact.indexOf('ORDER BY') < exact.indexOf('ORDER'), 'ORDER BY before ORDER');
  assert.ok(kws('SELECT * FROM t gr|').includes('GROUP BY'));
  assert.ok(kws('SELECT * FROM t le|').includes('LEFT JOIN'));
});

test('a phrase shows only where it can stand', () => {
  assert.ok(!kws('ord|').includes('ORDER BY'), 'no SELECT yet');
  assert.ok(!kws('SELECT le|').includes('LEFT JOIN'), 'no FROM yet');
  assert.ok(kws('SELECT * FROM t WHERE a is|').includes('IS NULL'));
  assert.ok(kws('SELECT * FROM t WHERE a is|').includes('IS NOT NULL'));
  assert.ok(!kws('SELECT a is|').includes('IS NULL'), 'no condition yet');
  assert.ok(kws('SELECT * FROM t WHERE a no|').includes('NOT IN'));
  assert.ok(kws('ins|').includes('INSERT INTO'), 'the first word of a statement');
  assert.ok(!kws('SELECT 1; SELECT ins|').includes('INSERT INTO'));
  assert.ok(kws('SELECT row_number() OVER (pa|').includes('PARTITION BY'));
  assert.ok(kws('SELECT dis|').includes('DISTINCT ON'));
  assert.ok(!kws('SELECT a, dis|').includes('DISTINCT ON'), 'only right after SELECT');
  assert.ok(kws("INSERT INTO t VALUES (1) on|").includes('ON CONFLICT'));
  assert.ok(kws('SELECT * FROM t ORDER BY a nu|').includes('NULLS FIRST'));
});

test('engine phrases go to their engine only', () => {
  assert.ok(kws('SELECT * FROM t cr|', 'mssql').includes('CROSS APPLY'));
  assert.ok(kws('SELECT * FROM t ou|', 'mssql').includes('OUTER APPLY'));
  assert.ok(kws('SELECT * FROM t wi|', 'mssql').includes('WITH (NOLOCK)'));
  assert.ok(!kws('SELECT * FROM t cr|', 'postgres').includes('CROSS APPLY'));
  assert.ok(kws('SELECT * FROM t ar|', 'clickhouse').includes('ARRAY JOIN'));
  assert.ok(kws('SELECT * FROM t WHERE a gl|', 'clickhouse').includes('GLOBAL IN'));
  assert.ok(kws('SELECT * FROM t cr|', 'athena').includes('CROSS JOIN UNNEST'));
  assert.ok(!kws('SELECT 1 on|', 'mssql').includes('ON CONFLICT'));
});

test('a read-only engine gets no write phrase', () => {
  assert.ok(!kws('ins|', 'clickhouse').includes('INSERT INTO'));
  assert.ok(!kws('del|', 'athena').includes('DELETE FROM'));
  assert.ok(kws('del|', 'mssql').includes('DELETE FROM'));
});

test('no snippet key is the start of an engine keyword or of a phrase', () => {
  const words = new Set();
  for (const eng of ['postgres', 'mssql', 'clickhouse', 'athena']) {
    W.qhKeywordsFor(eng).forEach(w => words.add(w.toLowerCase()));
    W.qhKwPhrasesFor(eng).forEach(p => words.add(p.t.split(' ')[0].toLowerCase()));
  }
  for (const sn of W.QH_SNIPPETS) {
    const hit = [...words].find(w => w.startsWith(sn.key));
    assert.equal(hit, undefined, `${sn.key} is the start of ${hit}`);
  }
});

// ---------- alias rules ----------

test('rules read as mask = alias, one per line', () => {
  const r = W.qhAliasRules('audit_* = au\n\n# a comment\nsales.orders=so\nnot a rule\nx = 1bad');
  assert.deepEqual(r.map(x => [x.mask, x.alias]), [['audit_*', 'au'], ['sales.orders', 'so']]);
  assert.equal(r.bad, 2);
  assert.equal(W.qhAliasRules('').length, 0);
});

test('the alias base: a rule first, then the initials, then the first letter', () => {
  assert.equal(W.qhAliasBase('order_items'), 'oi');
  assert.equal(W.qhAliasBase('JobCandidate'), 'jc');
  assert.equal(W.qhAliasBase('users'), 'u');
  assert.equal(W.qhAliasBase('2024_sales'), 't2s');
  const rules = W.qhAliasRules('audit_* = au\nSALES.ORDERS = so');
  assert.equal(W.qhAliasBase('audit_log', rules), 'au');
  assert.equal(W.qhAliasBase('Audit_Trail', rules), 'au', 'case does not count');
  assert.equal(W.qhAliasBase('orders', rules), 'o', 'a mask with a dot needs the qualified name');
  assert.equal(W.qhAliasBase('orders', rules, 'sales.orders'), 'so');
  assert.equal(W.qhAliasBase('orders', rules, 'app.orders'), 'o');
});

test('a used alias or a keyword gets a number', () => {
  assert.equal(W.qhJoinAlias('users', new Set(['u']), 'postgres'), 'u2');
  assert.equal(W.qhJoinAlias('in_stock', new Set(), 'postgres'), 'is2', 'IS is a keyword');
  assert.equal(W.qhJoinAlias('audit_log', new Set(['au']), 'postgres', W.qhAliasRules('audit_* = AU')), 'AU2');
});

// ---------- where an alias goes ----------

test('a table after FROM or JOIN gets an alias', () => {
  assert.equal(alias('SELECT * FROM ord|', 'orders'), 'o');
  assert.equal(alias('SELECT * FROM users u JOIN ord|', 'order_items'), 'oi');
  assert.equal(alias('SELECT * FROM ord| WHERE id = 1', 'orders'), 'o');
  assert.equal(alias('SELECT * FROM ord|;', 'orders'), 'o');
  assert.equal(alias('SELECT * FROM ord|, users u', 'orders'), 'o');
  assert.equal(alias('SELECT * FROM sales.ord|', 'orders'), 'o', 'after a schema qualifier too');
});

test('no alias where it would be wrong or is there already', () => {
  assert.equal(alias('SELECT * FROM ord| x', 'orders'), '', 'an alias is there');
  assert.equal(alias('SELECT * FROM ord| AS x', 'orders'), '');
  assert.equal(alias('SELECT * FROM a JOIN ord| ON ord.id = a.id', 'orders'), '', 'ON already names the table');
  assert.equal(alias('DELETE FROM ord|', 'orders'), '', 'SQL Server does not read DELETE FROM t a');
  assert.equal(alias('UPDATE ord|', 'orders'), '');
  assert.equal(alias('INSERT INTO ord|', 'orders'), '');
  assert.equal(alias('SELECT * FROM t WHERE ord|', 'orders'), '');
  assert.equal(alias('ord|', 'orders'), '');
});

test('the alias is unique in its statement only', () => {
  assert.equal(alias('SELECT o.id FROM ord|', 'orders'), 'o2', 'o is used');
  assert.equal(alias('SELECT 1 AS o; SELECT * FROM ord|', 'orders'), 'o', 'another statement does not count');
  assert.equal(alias('SELECT * FROM ord| WHERE o = 1', 'orders'), 'o2', 'a word after the caret counts');
});

test('a rule names the alias of a picked table and of a JOIN item', () => {
  const rules = W.qhAliasRules('audit_* = ad');
  assert.equal(W.qhAliasBase('audit_log'), 'al', 'the default differs from the rule');
  assert.equal(alias('SELECT * FROM aud|', 'audit_log', 'postgres', rules), 'ad');
  const meta = {
    tables: ['users', 'audit_log'],
    tableMeta: {
      users: [{ name: 'id', type: 'int' }],
      audit_log: [{ name: 'id', type: 'int' }, { name: 'user_id', type: 'int', fk: 'users.id' }],
    },
  };
  const sql = 'SELECT * FROM users u JOIN ';
  const js = W.qhJoinSuggestions(sql, sql.length, meta, 'postgres', rules);
  assert.equal(js.items[0].text, 'audit_log ad ON ad.user_id = u.id');
});

// ---------- the stored switch ----------

test('the alias options default to on with no rules', () => {
  assert.deepEqual(W.qhAliasOptsClean(null), { auto: true, rules: '' });
  assert.deepEqual(W.qhAliasOptsClean({ auto: false, rules: 'a = b' }), { auto: false, rules: 'a = b' });
  assert.equal(W.qhAliasOptsClean({ rules: 'x'.repeat(5000) }).rules.length, 2000);
  assert.equal(W.QH_ALIAS_KEY, 'qh.alias.v1');
});

// ---------- the real editor ----------

function mountEditor(initial, store) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { pretendToBeVisual: true, url: 'https://localhost/' });
  for (const k of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node', 'Event',
                   'KeyboardEvent', 'InputEvent', 'getComputedStyle', 'requestAnimationFrame',
                   'cancelAnimationFrame', 'localStorage']) {
    Object.defineProperty(globalThis, k, { value: dom.window[k], configurable: true, writable: true });
  }
  if (store) dom.window.localStorage.setItem('qh.alias.v1', JSON.stringify(store));
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const React = require_('react');
  const ReactDOM = require_('react-dom/client');
  globalThis.React = React;
  const win = dom.window;
  win.React = React;
  win.ReactDOM = require_('react-dom');
  loadInto(win, 'qh-data.jsx', 'qh-editor.jsx');
  function Host() {
    const [v, setV] = React.useState(initial);
    return React.createElement(win.SqlEditor, { value: v, docId: 't1', fontSize: 13, schema: SCHEMA, engineId: 'postgres', onChange: setV });
  }
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(Host)));
  const ta = win.document.querySelector('textarea');
  const setValue = Object.getOwnPropertyDescriptor(win.HTMLTextAreaElement.prototype, 'value').set;
  return {
    win, ta,
    caret(i) { ta.focus(); ta.setSelectionRange(i, i); },
    type(s) {
      for (const ch of s) {
        const a = ta.selectionStart, b = ta.selectionEnd, v = ta.value;
        React.act(() => {
          ta.dispatchEvent(new win.InputEvent('beforeinput', { inputType: 'insertText', data: ch, bubbles: true, cancelable: true }));
          setValue.call(ta, v.slice(0, a) + ch + v.slice(b));
          ta.setSelectionRange(a + 1, a + 1);
          ta.dispatchEvent(new win.InputEvent('input', { inputType: 'insertText', data: ch, bubbles: true }));
        });
      }
    },
    press(key) { React.act(() => ta.dispatchEvent(new win.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }))); },
    first() { const o = win.document.querySelector('.qh-ac-ed-opt .qh-ac-ed-name'); return o ? o.textContent : null; },
  };
}

const dom = JSDOM ? {} : { skip: 'jsdom not installed' };

test('Tab on a table after FROM writes the table and its alias', dom, () => {
  const ed = mountEditor('SELECT * FROM ');
  ed.caret(14);
  ed.type('ord');
  assert.equal(ed.first(), 'orders');
  ed.press('Tab');
  assert.equal(ed.ta.value, 'SELECT * FROM orders o');
});

test('with the switch off, a picked table gets no alias', dom, () => {
  const ed = mountEditor('SELECT * FROM ', { auto: false });
  ed.caret(14);
  ed.type('ord');
  ed.press('Tab');
  assert.equal(ed.ta.value, 'SELECT * FROM orders');
});

test('a stored rule names the alias', dom, () => {
  const ed = mountEditor('SELECT * FROM ', { auto: true, rules: 'audit_* = ad' });
  ed.caret(14);
  ed.type('aud');
  ed.press('Tab');
  assert.equal(ed.ta.value, 'SELECT * FROM audit_log ad');
});

test('Enter on a fully typed table name adds no alias', dom, () => {
  const ed = mountEditor('SELECT * FROM ');
  ed.caret(14);
  ed.type('users');
  ed.press('Enter');
  assert.equal(ed.ta.value, 'SELECT * FROM users', 'Enter is not a pick when the name is complete');
});

function mountOptions(store) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { pretendToBeVisual: true, url: 'https://localhost/' });
  for (const k of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node', 'Event', 'localStorage']) {
    Object.defineProperty(globalThis, k, { value: dom.window[k], configurable: true, writable: true });
  }
  if (store) dom.window.localStorage.setItem('qh.alias.v1', JSON.stringify(store));
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const React = require_('react');
  const ReactDOM = require_('react-dom/client');
  globalThis.React = React;
  const win = dom.window;
  win.React = React;
  win.ReactDOM = require_('react-dom');
  loadInto(win, 'qh-data.jsx', 'qh-sqlformat.jsx', 'qh-editor.jsx');
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(win.FormatOptions, { engineId: 'postgres', onFormat: () => {} })));
  const setValue = Object.getOwnPropertyDescriptor(win.HTMLTextAreaElement.prototype, 'value').set;
  return {
    win, doc: win.document,
    stored() { return JSON.parse(win.localStorage.getItem('qh.alias.v1') || 'null'); },
    click(el) { React.act(() => el.click()); },
    write(el, v) { React.act(() => { setValue.call(el, v); el.dispatchEvent(new win.Event('input', { bubbles: true })); }); },
  };
}

test('the Format options popup holds the alias switch and the rules', dom, () => {
  const ui = mountOptions();
  const sw = [...ui.doc.querySelectorAll('.qh-fmt-check')].find(l => /alias/.test(l.textContent)).querySelector('input');
  assert.equal(sw.checked, true, 'on by default');
  ui.click(sw);
  assert.equal(ui.stored().auto, false, 'stored at once');
  const ta = ui.doc.querySelector('textarea[aria-label="Alias rules"]');
  ui.write(ta, 'audit_* = al\nnot a rule');
  assert.equal(ui.stored().rules, 'audit_* = al\nnot a rule');
  assert.match(ui.doc.querySelector('.qh-fmt-pop').textContent, /1 line is not a rule\./);
});

test('the source wires the alias into a pick and the CSS styles the rules box', () => {
  const src = web('qh-editor.jsx');
  assert.match(src, /if \(item\.type === 'table'\) \{\n\s+const ao = qhAliasOptsLoad\(\);/);
  assert.match(src, /qhAutoAlias\(value, from, ac\.end, item\.label, engineId, qhAliasRules\(ao\.rules\), q\)/);
  assert.match(web('QueryHub.html'), /\.qh-fmt-rules textarea \{/);
});
