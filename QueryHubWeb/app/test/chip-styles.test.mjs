// 2026-10-24 (operator request): the env chip (.qh-envtag-sm) is SMALLER on purpose:
// 10px, weight 600, padding 1px 6px, set in a separate block at the end of the
// style. The 11px floor below therefore no longer covers it. The other chips keep it.
// Chips (2026-10-24): the env, tier, status, origin, conn-state, conn-off and
// reqrow-partial chips have text of at least 11px, are in sentence case except
// PROD, STG, TEST, RO, RW and DDL, and use tokens for colour. The PROD chip is a
// solid orange pill with dark ink. This reads the source CSS, so a later edit that
// walks one back fails here. Contrast is computed by the test from the token hex
// values with the WCAG formula, so the numbers in the CSS comment cannot rot.
import test from 'node:test';
import assert from 'node:assert/strict';

import { web, tokensCss, isVendorTokens } from './_mount.mjs';

const css = web('QueryHub.html');
const tokens = tokensCss();
// Exact contrast numbers belong to the vendor token values. Another token file
// only has to keep 4.5:1 (the >= asserts stay on for every file).
const pin = (got, want) => { if (isVendorTokens()) assert.equal(got, want); };

/** The body of the first rule whose selector is exactly `sel`. */
function rule(sel) {
  const live = css.replace(/\/\*[\s\S]*?\*\//g, ' ');
  for (const m of live.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (m[1].trim() === sel) return m[2];
  }
  assert.fail('no rule for ' + sel);
}
const px = (body) => Number((body.match(/font-size:\s*([\d.]+)px/) || [])[1]);

for (const sel of ['.qh-origin', '.qh-conn-state', '.qh-conn-off', '.qh-reqrow-partial', '.qh-tier', '.qh-tier.is-sm', '.qh-status']) {
  test(sel + ' text is at least 11px and never forced to upper case', () => {
    const body = rule(sel);
    assert.ok(px(body) >= 11, sel + ' is ' + px(body) + 'px');
    assert.doesNotMatch(body, /text-transform:\s*uppercase/);
  });
}

/** The body of the LAST rule whose selector is exactly `sel` (the override wins). */
function lastRule(sel) {
  const live = css.replace(/\/\*[\s\S]*?\*\//g, ' ');
  let found = null;
  for (const m of live.matchAll(/([^{}]+)\{([^{}]*)\}/g)) if (m[1].trim() === sel) found = m[2];
  assert.ok(found, 'no rule for ' + sel);
  return found;
}

test('the env chip is small: 10px, weight 600, padding 1px 6px, one line, never upper-cased by CSS', () => {
  const body = lastRule('.qh-envtag-sm');
  assert.equal(px(body), 10);
  assert.match(body, /font-weight:\s*600/);
  assert.match(body, /padding:\s*1px 6px/);
  assert.match(body, /white-space:\s*nowrap/);
  assert.match(body, /flex-shrink:\s*0/);
  assert.doesNotMatch(body, /text-transform:\s*uppercase/);
  // The size block is its own, dated block, after the batch 1 block.
  assert.ok(css.indexOf('2026-10-24 · Smaller env chip') > css.indexOf('2026-10-24 · Identity, dialogs and focus (batch 1)'));
});

test('the provider logo in a tree row is 20x12, and the colours of the chip did not move', () => {
  assert.match(lastRule('.qh-tr > .qh-tr-prov'), /width:\s*20px;\s*height:\s*12px/);
  // Colours stay in the first rules (tokens), not in the size block.
  assert.doesNotMatch(lastRule('.qh-envtag-sm'), /background|color:/);
  pin(ratio(hexOf('--fg-fix-dark', 'light'), hexOf('--warning-solid-medium', 'light')).toFixed(2), '5.00');
  pin(ratio(hexOf('--fg-fix-dark', 'light'), hexOf('--warning-solid-light', 'dark')).toFixed(2), '7.41');
});

test('the chips that carry words set only the first letter in upper case', () => {
  for (const sel of ['.qh-envtag-sm', '.qh-conn-state', '.qh-conn-off', '.qh-reqrow-partial']) {
    assert.match(css, new RegExp(sel.replace('.', '\\.') + '::first-letter \\{ text-transform: uppercase; \\}'), sel);
  }
});

// ---------- contrast ----------
function hexOf(name, theme) {
  const dark = tokens.slice(tokens.indexOf("[data-theme='dark']"));
  const light = tokens.slice(0, tokens.indexOf("[data-theme='dark']"));
  const re = new RegExp(name + ':\\s*(#[0-9A-Fa-f]{6})');
  const m = (theme === 'dark' ? dark.match(re) || light.match(re) : light.match(re));
  assert.ok(m, name + ' in ' + theme);
  return m[1];
}
const lin = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
const lum = (h) => { const n = parseInt(h.slice(1), 16); return 0.2126 * lin(n >> 16) + 0.7152 * lin((n >> 8) & 255) + 0.0722 * lin(n & 255); };
const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };

test('PROD is a solid orange pill with dark ink, in both themes (4.5:1)', () => {
  assert.match(rule('.qh-envtag-sm.env-production'), /background: var\(--warning-solid-medium\); color: var\(--fg-fix-dark\)/);
  assert.match(rule("[data-theme='dark'] .qh-envtag-sm.env-production"), /background: var\(--warning-solid-light\); color: var\(--fg-fix-dark\)/);
  const ink = hexOf('--fg-fix-dark', 'light');
  const light = ratio(ink, hexOf('--warning-solid-medium', 'light'));
  const dark = ratio(ink, hexOf('--warning-solid-light', 'dark'));
  assert.ok(light >= 4.5 && dark >= 4.5, light + ' / ' + dark);
  pin(light.toFixed(2), '5.00');
  pin(dark.toFixed(2), '7.41');
  // White ink on the orange would fail, which is why the ink is dark.
  assert.ok(ratio('#FFFFFF', hexOf('--warning-solid-medium', 'light')) < 4.5);
});

test('STG and TEST are a calm solid pill (4.5:1 in both themes)', () => {
  assert.match(rule('.qh-envtag-sm'), /background: var\(--system-solid-light\); color: var\(--fg-fix-white\)/);
  assert.match(rule("[data-theme='dark'] .qh-envtag-sm"), /color: var\(--bg-fix-dark\)/);
  pin(ratio('#FFFFFF', hexOf('--system-solid-light', 'light')).toFixed(2), '6.22');
  pin(ratio(hexOf('--bg-fix-dark', 'light'), hexOf('--system-solid-light', 'dark')).toFixed(2), '9.95');
});

test('the confirm button and its icon tile pass 4.5:1 in both themes', () => {
  assert.match(rule('.qh-conf-go'), /background: var\(--danger-solid-medium\); color: var\(--fg-fix-white\)/);
  assert.match(rule("[data-theme='dark'] .qh-conf-go"), /color: var\(--bg-fix-dark\)/);
  pin(ratio('#FFFFFF', hexOf('--danger-solid-medium', 'light')).toFixed(2), '5.70');
  pin(ratio(hexOf('--bg-fix-dark', 'light'), hexOf('--danger-solid-medium', 'dark')).toFixed(2), '5.42');
  assert.ok(ratio('#FFFFFF', hexOf('--danger-solid-medium', 'dark')) < 4.5, 'white on the dark red fails, so dark takes ink');
});

test('the status and tier chips use the strong text tokens, not the 3:1 ones', () => {
  assert.match(rule('.st-pending'), /var\(--warning-solid-strong\)/);
  assert.match(rule('.st-done'), /var\(--brand-solid-strong\)/);
  assert.match(rule('.st-rejected'), /var\(--danger-solid-medium\)/);
  assert.match(rule('.tier-ro'), /var\(--brand-solid-strong\)/);
  assert.match(rule('.tier-ddl'), /var\(--danger-solid-medium\)/);
});

test('every chip colour is a token, and each rule has a dated comment', () => {
  for (const sel of ['.qh-envtag-sm', '.qh-envtag-sm.env-production']) assert.doesNotMatch(rule(sel), /#[0-9a-fA-F]{3,8}\b/);
  assert.match(css, /2026-10-24 · Env chip/);
  assert.match(css, /2026-10-24 · Status chips/);
  assert.match(css, /2026-10-24 · Identity, dialogs and focus \(batch 1\)/);
});
