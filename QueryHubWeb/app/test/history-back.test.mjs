// Back must walk the app, not leave it.
//
// Both places that keep the URL in step with the view used `replaceState`
// unconditionally, so QueryHub created ZERO history entries: moving between
// admin sections rewrote one entry over and over, and crossing between the
// editor and the panel did the same. Pressing Back therefore skipped the whole
// application and landed on the sign-in page — reported from production
// 2026-09-08.
//
// The rule, in both files: a MOVE pushes, the FIRST pass replaces. The first
// pass is a normalisation — arriving with no hash, a deep link to a section
// this admin may not open — and an entry there makes Back a no-op that reads
// as broken.
//
// This reads the sources rather than rendering them, and that is the right
// tool for this particular regression: `qh-admin.jsx` and `qh-app.jsx` are
// DESIGN-OWNED, so the way this comes back is a whole-file port copying the
// old version over the fix, silently and correctly (SYNC.md §1 — it has
// already happened once, to the `maxRows` null guard). A test that reads the
// shipped source fails the moment that happens.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'src');
const read = (f) => readFileSync(resolve(SRC, f), 'utf8');

/** The body of the effect that writes the URL, in one file. */
function urlEffect(src, marker) {
  const i = src.indexOf(marker);
  assert.notEqual(i, -1, `the URL-sync effect moved: ${marker}`);
  const j = src.indexOf('}, [', i);
  assert.notEqual(j, -1, 'no dependency array after the marker');
  return src.slice(i, j);
}

const ADMIN = urlEffect(read('qh-admin.jsx'), "const want = '#admin/' + curNav");
const APP = urlEffect(read('qh-app.jsx'), "const inAdmin = (location.hash");

test('moving between admin sections pushes a history entry', () => {
  assert.match(ADMIN, /history\.pushState/);
});

test('crossing into and out of the admin panel pushes', () => {
  // Both directions: entering has to be a Back target as much as leaving.
  assert.match(APP, /view === 'admin'[\s\S]*?pushState/);
  assert.match(APP, /view === 'dev'[\s\S]*?pushState/);
});

test('neither file replaces the current entry unconditionally', () => {
  // The bug in one sentence. A bare replaceState with no first-pass guard is
  // what made Back leave the app.
  for (const [name, body] of [['qh-admin.jsx', ADMIN], ['qh-app.jsx', APP]]) {
    for (const m of body.matchAll(/replaceState/g)) {
      const before = body.slice(0, m.index);
      assert.match(before, /First\b|first\b/,
        `${name}: a replaceState that is not the first-pass normalisation`);
    }
  }
});

test('the first pass still replaces, so Back is never a no-op', () => {
  for (const [name, body] of [['qh-admin.jsx', ADMIN], ['qh-app.jsx', APP]]) {
    assert.match(body, /First\.current/, `${name}: no first-pass guard`);
    assert.match(body, /replaceState/, `${name}: the first pass must replace`);
  }
});

test('the guard is a ref, so it survives a re-render without resetting', () => {
  // useState would re-run the effect; a plain module variable would leak
  // across the panel being closed and reopened, and the second visit would
  // then push where it should replace.
  assert.match(read('qh-admin.jsx'), /const navFirst = React\.useRef\(true\)/);
  assert.match(read('qh-app.jsx'), /const viewFirst = useRef\(true\)/);
});

// ---------------------------------------------------------------------------
// The other half, from the 2026-09-08 design round: with real history entries,
// Back inside the admin panel started changing the SECTION BEHIND an open
// modal — a form left floating over a screen it no longer describes. So the
// topmost modal answers Back instead.
//
// The shape that makes it safe is the SENTINEL: an entry at the SAME URL, not
// a hash of its own. Same URL means no `hashchange`, and both view-sync
// listeners above are hashchange listeners, so the entry is invisible to them
// and cannot move the screen. Pinned here because a modal with its own hash
// would look equivalent and would not be: it would put a URL on a form and
// bring a dismissed dialog back with Forward.

const MODAL = read('qh-modal.jsx');

test('the modal sentinel sits at the same URL, not a hash of its own', () => {
  assert.match(MODAL, /pushState\(\{ qhModal: [\s\S]{0,40}location\.href\)/);
  assert.doesNotMatch(MODAL, /pushState\([^)]*['"`]#/,
    'a modal must not get a hash — that is a location, and a modal is not one');
});

test('only the topmost modal answers a Back', () => {
  // A stacked pair closes one at a time, the way Escape does.
  assert.match(MODAL, /QH_MODAL_STACK\[QH_MODAL_STACK\.length - 1\] !== entry/);
});

test('closing any other way spends the sentinel', () => {
  // Otherwise every dismissed dialog leaves a dead Back press behind and the
  // depth drifts up for the rest of the session.
  assert.match(MODAL, /if \(!spent && mine\(\)\) history\.back\(\)/);
});

test('a modal that refuses to close puts its sentinel back', () => {
  // ConnectionForm passes a no-op onClose while a save is in flight. Without
  // the re-push the entry is gone and the NEXT Back moves the screen behind a
  // modal that is still open.
  assert.match(MODAL, /spent = false; push\(\);/);
});

test('Back calls the current onClose, not the one captured at mount', () => {
  // The history effect is mount-only and `onClose` changes between renders.
  assert.match(MODAL, /closeRef\.current = onClose/);
  assert.match(MODAL, /closeRef\.current\(\)/);
});

test('the app never listens for popstate, which is what keeps the sentinel invisible', () => {
  // If either view-sync listener were on popstate instead of hashchange, the
  // sentinel would move the screen behind every open modal.
  for (const f of ['qh-app.jsx', 'qh-admin.jsx']) {
    assert.doesNotMatch(read(f), /addEventListener\('popstate'/, f);
  }
});
