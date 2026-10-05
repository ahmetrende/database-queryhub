// Amazon Athena is an engine of its own in the UI, not a Postgres look-alike.
//
// The server reports an Athena connection's engine as `athena`, and qhEngineId
// used to fall through to `postgres` for it. The archive was then quoted,
// qualified and drawn as Postgres: `public` as the schema a generated SELECT
// named, and the Postgres logo -- the one thing in the connection tree that
// says what engine a connection is.
import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';

import { bareWindow, loadInto } from './_load.mjs';

const win = loadInto(bareWindow(), 'qh-data.jsx');
const conn = { engine: 'athena' };

test('athena is recognised, however it is spelled', () => {
  assert.equal(win.qhEngineId('athena'), 'athena');
  assert.equal(win.qhEngineId('Amazon Athena'), 'athena');
  assert.equal(win.qhEngine(conn).label, 'Amazon Athena');
  assert.equal(win.qhEngineBadge(conn), 'ATHENA');
});

test('the engines it already knew are unchanged', () => {
  for (const [s, id] of [['PostgreSQL', 'postgres'], ['postgres', 'postgres'], ['', 'postgres'],
                         ['SQL Server 2022', 'mssql'], ['mssql', 'mssql'],
                         ['ClickHouse', 'clickhouse']]) {
    assert.equal(win.qhEngineId(s), id, s);
  }
});

test('identifiers are quoted the Trino way', () => {
  assert.equal(win.qhQuoteIdentFor('ledgers', 'athena'), 'ledgers');
  assert.equal(win.qhQuoteIdentFor('Ledgers', 'athena'), '"Ledgers"');
  assert.equal(win.qhQuoteIdentFor('odd"name', 'athena'), '"odd""name"');
});

test('the Glue database stands in for the schema', () => {
  const db = { name: 'archive_db' };
  assert.equal(win.qhSchemaFor(conn, db), 'archive_db');
  assert.equal(win.qhSelectSql(conn, db, 'ledgers'), 'SELECT *\nFROM archive_db.ledgers\nLIMIT 100;');
});

test('no system catalog node: information_schema is refused on this engine', () => {
  assert.equal(win.qhEngine(conn).catalogLabel, null);
  assert.deepEqual(win.qhEngine(conn).system, {});
});

test('athena has a logo of its own', () => {
  assert.equal(win.qhEngineLogo(conn), '/brand/engines/athena.svg');
});

test('an engine with no logo file gets a neutral glyph, not another engine\'s logo', () => {
  const src = win.qhEngineLogo({ engine: 'MySQL 8' });
  assert.ok(src.startsWith('data:image/svg+xml,'), src);
  assert.equal(win.qhEngineLogo({ engine: 'PostgreSQL' }), '/brand/engines/postgres.svg');
  assert.equal(win.qhEngineLogo({ engine: 'ClickHouse' }), '/brand/engines/clickhouse.svg');
});

test('every logo path names a file that exists', () => {
  for (const engine of ['PostgreSQL', 'SQL Server', 'ClickHouse', 'athena']) {
    const p = win.qhEngineLogo({ engine });
    assert.ok(existsSync(new URL('../..' + p, import.meta.url)), engine + ' -> ' + p);
  }
});
