// Every selector and token the source CSS declares must survive minification.
//
// Written during the vite 6 -> 8 migration, which swapped the CSS minifier
// (esbuild -> lightningcss) and changed the output size. A minifier that drops a
// rule it does not understand produces a smaller file and a silently broken
// screen: nothing errors, the styling is just gone. The bundle is 100 kB of
// design-owned CSS covering surfaces that are hard to reach by hand — the admin
// heat map, the reason strip, two-line tree rows — so "it looked fine when I
// loaded the editor" is not coverage.
//
// This compares the built stylesheet against src/index.css, which is itself
// GENERATED from the design prototype's inline <style> by gen-css.mjs. So the
// chain checked here is design source -> generated CSS -> shipped bundle.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, '..', 'src', 'index.css');
const ASSETS = resolve(HERE, '..', 'dist', 'assets');

function builtCss() {
  if (!existsSync(ASSETS)) return null;
  const css = readdirSync(ASSETS).filter(f => f.endsWith('.css'));
  if (!css.length) return null;
  return css.map(f => readFileSync(resolve(ASSETS, f), 'utf8')).join('\n');
}

const built = builtCss();
const opts = built ? {} : { skip: 'no built CSS — run `npm run build` first' };

/** Class names the source declares, e.g. `.qh-heatcell`. */
function sourceClasses(css) {
  // Strip comments first: commented-out rules are not declarations.
  const live = css.replace(/\/\*[\s\S]*?\*\//g, ' ');
  return new Set([...live.matchAll(/\.(-?[A-Za-z_][A-Za-z0-9_-]*)/g)]
    .map(m => m[1])
    // Numeric-looking fragments come from things like `0.5s`; a real class name
    // cannot start with a digit, and the regex already excludes that.
    .filter(n => n.length > 1));
}

/** Custom properties the source DEFINES, e.g. `--qh-mono:`. */
function sourceVars(css) {
  const live = css.replace(/\/\*[\s\S]*?\*\//g, ' ');
  return new Set([...live.matchAll(/(--[A-Za-z0-9_-]+)\s*:/g)].map(m => m[1]));
}

test('the built CSS keeps every class the source declares', opts, () => {
  const src = readFileSync(SRC, 'utf8');
  const missing = [...sourceClasses(src)].filter(c => !built.includes('.' + c));
  assert.deepEqual(missing, [],
    `the minifier dropped ${missing.length} class selector(s) — a screen using ` +
    `any of them is unstyled and nothing throws`);
});

test('the built CSS keeps every custom property the source defines', opts, () => {
  const src = readFileSync(SRC, 'utf8');
  const missing = [...sourceVars(src)].filter(v => !built.includes(v));
  assert.deepEqual(missing, [],
    'a design token vanished; every rule reading it falls back silently');
});

test('the surfaces that are hardest to eyeball are present', opts, () => {
  // A spot list, on purpose: the checks above are mechanical and would pass a
  // file that kept the selectors and lost their bodies. These are rules whose
  // absence would not be noticed on the screens anyone opens by habit.
  for (const needle of [
    '.qh-heatcell',        // admin metrics heat map
    '.qh-why',             // the reason strip (RW/DDL only)
    '.qh-tr.is-two',       // two-line tree row for an ambiguous database name
    '.qh-name-pre',        // folded fleet prefix
    '.qh-ac-ed',           // editor autocomplete popup
    '.qh-stackseg',        // weekly volume status mix
  ]) {
    assert.ok(built.includes(needle), `${needle} is not in the built CSS`);
  }

  // Dark mode is an ATTRIBUTE, not a media query: the theme is chosen in the
  // app, so asserting prefers-color-scheme would test nothing (the source has
  // none). Count against the source rather than a number written here, and count
  // BOTH sides with comments stripped and quotes normalised — lightningcss emits
  // `[data-theme=dark]` where the source wrote `[data-theme='dark']`, and the
  // source discusses the selector in prose, which is not a rule.
  const themeRules = (css, arm) =>
    (css.replace(/\/\*[\s\S]*?\*\//g, ' ')
        .match(new RegExp(`\\[data-theme=['"]?${arm}`, 'g')) || []).length;
  for (const arm of ['dark', 'light']) {
    const want = themeRules(readFileSync(SRC, 'utf8'), arm);
    const got = themeRules(built, arm);
    assert.ok(want > 0, `the source declares no ${arm} rules — update this test`);
    assert.equal(got, want,
      `${want} ${arm}-theme rule(s) in the source, ${got} in the bundle`);
  }
});

test('the built CSS is not suspiciously small', opts, () => {
  // A wholesale failure — an empty or nearly-empty stylesheet — would pass the
  // selector checks only if the source were empty too, but this is the cheap
  // canary for "the CSS pipeline broke" and it costs nothing.
  assert.ok(built.length > 50_000,
    `built CSS is only ${built.length} bytes; the design stylesheet alone is ~100 kB`);
});
