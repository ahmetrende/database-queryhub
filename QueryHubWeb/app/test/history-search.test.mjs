// History search, duration and CSV export (2026-11-10).
//
// The pure helpers in qh-data.jsx: the duration label on each history row, the
// CSV text that Export CSV downloads, and its file name. The sidebar wiring is
// checked by reading the source, because mounting Sidebar needs most of the app.
import test from 'node:test';
import assert from 'node:assert/strict';
import { bareWindow, loadInto } from './_load.mjs';
import { web as read } from './_mount.mjs';
const win = loadInto(bareWindow(), 'qh-data.jsx');
const { qhDurationLabel, qhHistoryCsv, qhHistoryCsvName, qhHistoryRow, qhCsvCell } = win;

const HEADER = 'id,created_at,status,outcome,tier,connection,database,rows,duration_ms,approver,sql';

test('the duration label', () => {
  assert.equal(qhDurationLabel(null), '');
  assert.equal(qhDurationLabel(undefined), '');
  assert.equal(qhDurationLabel(0), '0 ms');
  assert.equal(qhDurationLabel(850), '850 ms');
  assert.equal(qhDurationLabel(999), '999 ms');
  assert.equal(qhDurationLabel(1000), '1.0 s');
  assert.equal(qhDurationLabel(1250), '1.2 s');
  assert.equal(qhDurationLabel(59999), '59.9 s');
  assert.equal(qhDurationLabel(60000), '1 min 0 s');
  assert.equal(qhDurationLabel(125000), '2 min 5 s');
});

test('a history row carries durationMs', () => {
  assert.equal(qhHistoryRow({ id: '1', durationMs: 1500 }).durationMs, 1500);
  assert.equal(qhHistoryRow({ id: '1', durationMs: 0 }).durationMs, 0);
  assert.equal(qhHistoryRow({ id: '1' }).durationMs, null);
  assert.equal(qhHistoryRow({ id: '1', durationMs: null }).durationMs, null);
});

test('the CSV has the header and ends each line with CRLF', () => {
  assert.equal(qhHistoryCsv([]), HEADER + '\r\n');
  assert.equal(qhHistoryCsv(null), HEADER + '\r\n');
});

test('the CSV quotes commas, quotes and line breaks, and leaves null cells empty', () => {
  const row = qhHistoryRow({
    id: '7', createdAt: '2026-09-28T12:00:00Z', status: 'done', outcome: null, tier: 'ro',
    connectionId: 'srv-alpha', databaseId: 'shop', rowCount: 3, durationMs: 1500,
    approver: 'ann, bob', sql: 'SELECT "id"\nFROM orders',
  });
  const lines = qhHistoryCsv([row]).split('\r\n');
  assert.equal(lines[0], HEADER);
  assert.equal(lines[1], '7,2026-09-28T12:00:00Z,done,,ro,srv-alpha,shop,3,1500,"ann, bob","SELECT ""id""\nFROM orders"');
  assert.equal(lines[2], '');
});

test('the CSV guards cells that a spreadsheet reads as a formula', () => {
  assert.equal(qhCsvCell('=SUM(1)'), "'=SUM(1)");
  assert.equal(qhCsvCell('+1'), "'+1");
  assert.equal(qhCsvCell('-1'), "'-1");
  assert.equal(qhCsvCell('@cmd'), "'@cmd");
  assert.equal(qhCsvCell('\tx'), "'\tx");
  assert.equal(qhCsvCell('\rx'), '"\'\rx"');
  assert.equal(qhCsvCell('=SUM(1,2)'), '"\'=SUM(1,2)"');
  assert.equal(qhCsvCell('SELECT 1'), 'SELECT 1');
  assert.equal(qhCsvCell(null), '');
  assert.equal(qhCsvCell(undefined), '');
  assert.equal(qhCsvCell(0), '0');
  const csv = qhHistoryCsv([{ id: '1', sql: '=SUM(1)' }]);
  assert.equal(csv.split('\r\n')[1], "1,,,,,,,,,,'=SUM(1)");
});

test('the CSV file name carries the local date', () => {
  assert.equal(qhHistoryCsvName(new Date(2026, 0, 5, 10, 0)), 'queryhub-history-2026-01-05.csv');
  assert.equal(qhHistoryCsvName(new Date(2026, 10, 30)), 'queryhub-history-2026-11-30.csv');
});

test('the sidebar searches through the API with the three fields', () => {
  const src = read('qh-panels.jsx');
  assert.match(src, /qhApi\.history\(200, \{ q: hq\.trim\(\), since: hsince, until: huntil \}\)/);
  assert.match(src, /placeholder="Search SQL, database or #id"/);
  assert.match(src, /aria-label="From date"/);
  assert.match(src, /aria-label="To date"/);
  assert.match(src, /qhHistoryCsv\(hvisible\)/);
  assert.match(src, /qhDurationLabel\(h\.durationMs\)/);
});

test('the API client and the mock take the search', () => {
  assert.match(read('qh-api.jsx'), /history:\s+\(n=50, f\) =>/);
  assert.match(read('qh-api-mock.jsx'), /history: \(n, f\) => mockDelay/);
});

test('the date range follows the browser clock, and a bell item ends a search', () => {
  // The server reads `since` and `until` as days on the caller's clock.
  assert.match(read('qh-api.jsx'), /p\.set\('tz', String\(-new Date\(\)\.getTimezoneOffset\(\)\)\)/);
  // A bell item points at one row, so a search that could hide it ends.
  assert.match(read('qh-panels.jsx'), /setHfilter\('all'\);\s+\/\/[^\n]*\n\s+setHq\(''\); setHsince\(''\); setHuntil\(''\);/);
});
