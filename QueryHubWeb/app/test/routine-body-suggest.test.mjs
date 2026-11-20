// Completion inside a PostgreSQL routine body (2026-11-20, P2.3).
//
//   * The body of CREATE FUNCTION ... AS $$ ... $$ and of DO $$ ... $$ is code:
//     the typing list works there as it works on the body text alone.
//   * The offsets of the answer are offsets in the full text.
//   * Any other dollar quote is data: SELECT $$x$$, format($q$ ... $q$), and a
//     body in a language that is not sql or plpgsql.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';

const W = loadInto(bareWindow(), 'qh-data.jsx', 'qh-editor.jsx');

const SCHEMA = {
  tables: ['users', 'orders'],
  columns: ['id', 'email', 'user_id'],
  dbs: [],
  tableCols: { users: ['id', 'email'], orders: ['id', 'user_id'] },
  tableColsQ: {},
  qualify: {},
  systemTables: [],
  functions: [],
  functionKind: {},
  schemas: ['public'],
  tableInfo: {},
  tableInfoQ: {},
};

const split = (sql) => [sql.replace('|', ''), sql.indexOf('|')];
const sug = (sql, eng = 'postgres', opts) => { const [t, c] = split(sql); return W.qhBuildSuggest(t, c, SCHEMA, eng, opts); };
const texts = (r) => (r ? r.items : []).map(i => i.text);
const view = (sql, eng = 'postgres') => { const [t, c] = split(sql); return W.qhCodeView(t, c, eng); };

// The list at `sql` lists the table `users`, and its range is the `us` that
// is typed in the full text.
const listsUsers = (sql) => {
  const [t] = split(sql);
  const r = sug(sql);
  assert.ok(r, 'a list in the body: ' + sql);
  assert.ok(texts(r).includes('users'), 'users is listed: ' + sql);
  const at = t.lastIndexOf('us');
  assert.equal(r.start, at, 'start is an offset in the full text');
  assert.equal(r.end, at + 2);
  return r;
};

test('an open CREATE FUNCTION body lists tables after FROM', () => {
  listsUsers('CREATE FUNCTION f() RETURNS int AS $$ BEGIN SELECT * FROM us|');
});

test('a $body$ tag and a closed body work too', () => {
  listsUsers('CREATE FUNCTION f() RETURNS int AS $body$ BEGIN SELECT * FROM us| ');
  listsUsers('CREATE OR REPLACE FUNCTION f() RETURNS int AS $$\nBEGIN\n  SELECT * FROM us|;\n  RETURN 1;\nEND\n$$ LANGUAGE plpgsql;');
});

test('a DO block and DO LANGUAGE plpgsql are bodies', () => {
  listsUsers('DO $$ BEGIN PERFORM 1 FROM us| ; END $$;');
  listsUsers('DO LANGUAGE plpgsql $$ BEGIN PERFORM 1 FROM us|');
});

test('the item start of a qualified table is an offset in the full text', () => {
  const sch = { ...SCHEMA, qualify: { users: 'app.users' } };
  const sql = 'DO $$ BEGIN PERFORM 1 FROM app.us| ; END $$;';
  const [t, c] = split(sql);
  const r = W.qhBuildSuggest(t, c, sch, 'postgres');
  assert.ok(r);
  const it = r.items.find(i => i.label === 'users');
  assert.ok(it, 'users is listed');
  assert.equal(it.start, t.indexOf('app.us'), 'the qualifier start moves with the view');
});

test('any other dollar quote stays data', () => {
  assert.equal(sug('SELECT $$ FROM us|'), null);
  assert.equal(sug("SELECT format($q$ FROM us|"), null);
  assert.equal(sug('DO $$ BEGIN EXECUTE $q$ SELECT * FROM us|'), null, 'a dollar quote inside a body is data too');
  assert.equal(sug('CREATE FUNCTION f() RETURNS int LANGUAGE plpython3u AS $$ FROM us|'), null);
  assert.equal(sug('CREATE FUNCTION f() RETURNS int AS $$ FROM us| $$ LANGUAGE plpython3u;'), null, 'LANGUAGE after the body counts');
  assert.equal(sug('CREATE FUNCTION f() RETURNS int AS $$ FROM us| $$ LANGUAGE "PLPYTHON3U";'), null, 'quoted and any case');
});

test('SQL Server has no dollar quotes, so nothing changes there', () => {
  const v = view('SELECT 1 AS $$ x|', 'mssql');
  assert.equal(v.off, 0);
});

test('qhCodeView: off 0 outside a body, the body offset inside it', () => {
  const out = view('SELECT * FROM us|');
  assert.equal(out.text, 'SELECT * FROM us');
  assert.equal(out.caret, 16);
  assert.equal(out.off, 0);
  const sql = 'CREATE FUNCTION f() RETURNS int AS $$ SELECT 1| $$;';
  const [t] = split(sql);
  const v = view(sql);
  assert.equal(v.off, t.indexOf('$$') + 2);
  assert.equal(v.text, ' SELECT 1 ');
  assert.equal(v.caret, ' SELECT 1'.length);
  // Before the opening tag the caret is not in the body.
  assert.equal(W.qhCodeView(t, t.indexOf('$$'), 'postgres').off, 0);
});

test('qhCodeView: a body inside a body narrows twice', () => {
  const sql = 'DO $$ BEGIN EXECUTE 1; CREATE FUNCTION g() RETURNS int AS $i$ SELECT * FROM us| $i$; END $$;';
  const [t] = split(sql);
  const v = view(sql);
  assert.equal(v.off, t.indexOf('$i$') + 3);
  assert.equal(v.text, ' SELECT * FROM us ');
  listsUsers(sql);
});

test('outside any body the answer is the same as before', () => {
  const r = sug('SELECT * FROM us|');
  assert.ok(r);
  assert.equal(r.start, 14);
  assert.equal(r.end, 16);
  assert.equal(r.items[0].text, 'users');
});

test('caret marks and parameter info read the body alone', () => {
  const sql = 'DO $$ BEGIN PERFORM id FROM users WHERE id| = 1; END $$;';
  const [t, c] = split(sql);
  const marks = W.qhCaretMarks(t, c, 'postgres').filter(m => m.kind === 'occ');
  assert.deepEqual(marks.map(m => t.slice(m.start, m.end)), ['id', 'id'], 'both ids are marked, in full-text offsets');
  const call = 'DO $$ BEGIN PERFORM coalesce(1, |';
  const [t2, c2] = split(call);
  const r = W.qhCallAt(t2, c2, 'postgres');
  assert.ok(r, 'a call inside the body');
  assert.equal(r.name, 'coalesce');
  assert.equal(r.arg, 1);
  assert.equal(r.open, t2.indexOf('('));
  assert.equal(r.nameStart, t2.indexOf('coalesce'));
});
