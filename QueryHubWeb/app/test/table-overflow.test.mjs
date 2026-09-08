// Every admin table scrolls inside its own box.
//
// Reported from production 2026-09-08: the Connections page scrolled the whole
// PAGE sideways and cut the last action button off at the viewport edge.
//
// The containment pattern already existed — `.qh-tablewrap { overflow-x: auto }`
// — and all three Insights tables used it. All five Access tables did not, and
// had not since they were written. It surfaced on Connections first only
// because that one is the widest (7 columns, 5 action buttons, and a cell
// holding a 36-char name over an 88-char host line).
//
// So what is pinned here is the RULE, not the fix. Design owns these files and
// carried the repair at source, so a port will not revert it; the failure this
// catches is the next one — a table added later that quietly misses the wrap,
// exactly as five of them did.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'src');
const read = (f) => readFileSync(resolve(SRC, f), 'utf8');

/** Every `<table className="qh-atable…">` and the markup just before it. */
function tables() {
  const out = [];
  for (const f of readdirSync(SRC).filter(f => f.endsWith('.jsx'))) {
    const src = read(f);
    for (const m of src.matchAll(/<table className="qh-atable[^"]*"/g)) {
      out.push({ file: f, before: src.slice(Math.max(0, m.index - 220), m.index) });
    }
  }
  return out;
}

test('there are admin tables to check', () => {
  // A rename that emptied the scan would otherwise make this file pass by
  // finding nothing.
  assert.ok(tables().length >= 8, `only ${tables().length} tables found`);
});

test('every admin table is inside a scroll wrapper', () => {
  const naked = tables().filter(t => !/className="qh-tablewrap"/.test(t.before));
  assert.deepEqual(naked.map(t => t.file), [],
    'a table with no .qh-tablewrap scrolls the PAGE instead of itself');
});

test('the wrapper still has the rule that makes it work', () => {
  const css = read('index.css');
  assert.match(css, /\.qh-tablewrap\s*\{[^}]*overflow-x:\s*auto/);
});

test('the action column stays reachable without scrolling', () => {
  // The wrap alone turns "the page is broken" into "I have to scroll to every
  // button" — Actions being the column somebody opened the page for. It is
  // pinned to the right edge of the table's own scroller instead.
  const css = read('index.css');
  assert.match(css, /\.qh-acttable[^{]*\{[^}]*position:\s*sticky/);
  assert.match(css, /\.qh-acttable[^{]*\{[^}]*right:\s*0/);
});

test('group headers and inline edit rows are excluded from the sticky cell', () => {
  // `:not([colspan])` — a group header spans the table and has no action cell,
  // so pinning it would pin the header text to the right edge.
  assert.match(read('index.css'), /td\.qh-tright:not\(\[colspan\]\)/);
});

test('the widest cell is capped rather than left to set the table width', () => {
  // A 36-char name over an 88-char host:port/database line was ~500px of table
  // width on its own. The name wraps whole; the host ellipsises and keeps the
  // full value in a title.
  assert.match(read('index.css'), /\.qh-conntable td:first-child[^{]*\{[^}]*max-width/);
  assert.match(read('qh-admin-access.jsx'), /className="qh-muted qh-mono qh-conn-host" title=/);
});
