// The target strip of the action bar shows the provider logo, the env tag, the
// engine logo and then the name (2026-10-23, asked after the portal's design:
// the cloud a database runs on sits where the person checks the target).
// The ActionBar is not a global, so this reads its source in order.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const web = (f) => readFileSync(resolve(HERE, '..', '..', f), 'utf8');
const app = web('qh-app.jsx');
const strip = app.slice(app.indexOf('<div className="qh-target">'), app.indexOf('<div className="qh-ab-spacer" />'));

test('the target strip lists provider logo, env tag, engine logo, then the connection name', () => {
  const at = (needle) => { const i = strip.indexOf(needle); assert.ok(i >= 0, needle); return i; };
  const prov = at('qh-target-prov'), env = at('qh-envtag-sm'), eng = at('qh-target-eng'), name = at('qh-target-conn');
  assert.ok(prov < env && env < eng && eng < name, [prov, env, eng, name].join(' < '));
});

test('a connection with no provider tag shows no provider logo, and the logo reads the provider tag', () => {
  assert.match(strip, /\{conn && qhProvider\(conn\) && <img className="qh-prov-logo qh-target-prov" src=\{qhProviderLogo\(qhTags\(conn\)\.provider\)\}/);
});

test('the engine logo has a name for a screen reader and a hover title', () => {
  assert.match(strip, /className="qh-engine-logo qh-target-eng"[^>]*alt=\{qhEngine\(conn\)\.label\}[^>]*title=\{qhEngine\(conn\)\.label\}/);
});

test('the logos have their own sizes in the page CSS, in a dated block', () => {
  const css = web('QueryHub.html');
  assert.match(css, /2026-10-23 · Provider and engine logos on the target strip/);
  assert.match(css, /\.qh-target-prov \{ width: 24px; height: 14px;/);
  assert.match(css, /\.qh-target-eng \{ width: 16px; height: 16px; flex-shrink: 0; \}/);
});
