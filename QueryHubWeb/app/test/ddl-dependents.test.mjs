// What uses an object before a DROP or ALTER (2026-11-20, W5 P2.1).
//
// The DDL box of the approval queue has a "Used by" row. It lists what uses
// each object that the request drops or alters. A null `ddlDependents` shows
// "not checked", never an empty value. The mock queue carries one item with
// the field, so the row renders in the mock.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const read = (name) => readFileSync(resolve(HERE, '..', '..', name), 'utf8');
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/^\s*\/\/.*$/gm, ' ');

const admin = strip(read('qh-admin.jsx'));
const mock = read('qh-api-mock.jsx');
const html = read('QueryHub.html');

test('the DDL box has a "Used by" row that reads ddlDependents', () => {
  assert.match(admin, /<div className="qh-ddlbox-k">Used by<\/div>/);
  assert.match(admin, /<QhDdlUsedBy deps=\{cur\.ddlDependents\} \/>/);
});

test('a null value says "not checked"', () => {
  const fn = admin.slice(admin.indexOf('function QhDdlUsedBy'), admin.indexOf('function ApprovalsView'));
  assert.ok(fn.length > 0, 'QhDdlUsedBy is defined before ApprovalsView');
  assert.match(fn, /if \(!deps \|\| !Array\.isArray\(deps\.objects\)\)/);
  assert.match(fn, /<span className="qh-ddlbox-miss">not checked<\/span>/);
});

test('the row says each state in words', () => {
  const fn = admin.slice(admin.indexOf('function QhDdlUsedBy'), admin.indexOf('function ApprovalsView'));
  assert.match(fn, /object not found/);
  assert.match(fn, /nothing found that uses it/);
  assert.match(fn, /CASCADE drops these too/);
  assert.match(fn, /Code inside function bodies is not checked\./);
  assert.match(fn, /qh-ddlbox-detail/);
});

test('a CREATE gets no "Used by" row', () => {
  assert.match(admin, /cur\.ddlDependents != null \|\| \/\\b\(DROP\|ALTER\)\\b\/i\.test\(cur\.sql/);
});

test('the mock queue has an item with ddlDependents', () => {
  assert.match(mock, /ddlDependents: \{ engine: 'postgres', bodiesChecked: false, objects: \[/);
  assert.match(mock, /kind: 'foreign key', name: 'analytics\.event_tags', detail: 'fk_event_tags_event'/);
});

test('the new classes have CSS next to the DDL box rules', () => {
  for (const cls of ['qh-ddlbox-deps', 'qh-ddlbox-detail', 'qh-ddlbox-note']) {
    assert.ok(html.includes('.' + cls + ' '), cls + ' has a rule');
  }
});
