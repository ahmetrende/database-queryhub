// B26: rows that open a record are keyboard-operable.
// B29: the login copy does not promise an instant read-only run.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const read = (f) => readFileSync(join(root, f), 'utf8');

/** Every JSX opening tag that holds `needle` in an onClick, up to the closing `>` line. */
function rowTags(src, needle) {
  const out = [];
  let i = 0;
  while ((i = src.indexOf(needle, i)) !== -1) {
    const start = src.lastIndexOf('<div', i);
    const end = src.indexOf('>\n', i);
    out.push(src.slice(start, end));
    i += needle.length;
  }
  return out;
}

const cases = [
  ['qh-home.jsx', 'onClick={() => onRestoreSession(s)}'],
  ['qh-home.jsx', 'onClick={() => onLoadSaved(s)}'],
  ['qh-home.jsx', 'onClick={() => onOpenScheduled(s)}'],
  ['qh-panels.jsx', 'onClick={() => onLoadSaved(s)}'],
  ['qh-panels.jsx', 'onClick={() => onRestoreSession(s)}'],
  ['qh-panels.jsx', 'onClick={() => onOpenScheduled(s)}'],
];

for (const [file, needle] of cases) {
  test(`${file} row ${needle} has role, tabIndex, name and a key handler`, () => {
    const tags = rowTags(read(file), needle);
    assert.equal(tags.length, 1);
    const t = tags[0];
    assert.match(t, /role="button"/);
    assert.match(t, /tabIndex=\{0\}/);
    assert.match(t, /aria-label=/);
    assert.match(t, /onKeyDown=/);
    assert.match(t, /e\.target !== e\.currentTarget/);
    assert.match(t, /e\.key === 'Enter' \|\| e\.key === ' '/);
    assert.match(t, /e\.preventDefault\(\)/);
  });
}

test('rows have a focus-visible style', () => {
  assert.match(read('QueryHub.html'), /\.qh-home-row:focus-visible, \.qh-saved:focus-visible/);
  assert.match(read('app/src/index.css'), /\.qh-home-row:focus-visible, \.qh-saved:focus-visible/);
});

test('login copy no longer promises an instant read-only run', () => {
  const src = read('qh-login.jsx');
  assert.doesNotMatch(src, /read-only query with a matching grant runs at once/);
  assert.match(src, /only when an auto-approve rule covers it/);
  assert.match(src, /waits for an approver/);
});
