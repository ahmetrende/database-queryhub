// The action bar steps down when its contents do not fit (2026-11-20). With
// the AI panel open at 1440px, the Masked and Runs on chips painted over the
// panel's mode buttons, and Explain and Schedule sat under the panel. The
// steps are measured, not a width breakpoint: what fits depends on the chips
// the query brings.
import test from 'node:test';
import assert from 'node:assert/strict';
import { page, sliceFunction, web, jsdomOpts } from './_mount.mjs';

const app = web('qh-app.jsx');
const css = web('QueryHub.html');

// A stand-in element: `need[level]` is the width the contents take at a step.
function fakeBar(width, need) {
  const el = { dataset: {} };
  const level = () => (el.dataset.fit ? el.dataset.fit.split(' ').length : 0);
  return { el, overflows: () => need[level()] > width };
}

test('the bar takes the first step whose contents fit, and lists the steps in force', jsdomOpts, () => {
  const ui = page([]);
  const fit = sliceFunction('qh-app.jsx', 'qhFitActionBar', ui.win);
  let b = fakeBar(1200, [1300, 1250, 1100, 900]);
  assert.equal(fit(b.el, b.overflows), 2);
  assert.equal(b.el.dataset.fit, '1 2');
  b = fakeBar(2000, [1300, 1250, 1100, 900]);
  assert.equal(fit(b.el, b.overflows), 0);
  assert.equal(b.el.dataset.fit, undefined, 'a bar that fits has no data-fit');
});

test('the bar stops at the wrap step even when the contents still do not fit', jsdomOpts, () => {
  const ui = page([]);
  const fit = sliceFunction('qh-app.jsx', 'qhFitActionBar', ui.win);
  const b = fakeBar(300, [1300, 1250, 1100, 900]);
  assert.equal(fit(b.el, b.overflows), 3);
  assert.equal(b.el.dataset.fit, '1 2 3');
});

test('a measurement starts from step 0, so a wider bar gets its labels back', jsdomOpts, () => {
  const ui = page([]);
  const fit = sliceFunction('qh-app.jsx', 'qhFitActionBar', ui.win);
  const need = [1300, 1250, 1100, 900];
  const b = fakeBar(1000, need);
  assert.equal(fit(b.el, b.overflows), 3);
  const wide = { el: b.el, overflows: () => need[b.el.dataset.fit ? b.el.dataset.fit.split(' ').length : 0] > 1260 };
  assert.equal(fit(wide.el, wide.overflows), 1);
});

test('the overflow check reads the in-flow children only, not a fixed popover', jsdomOpts, () => {
  const ui = page([]);
  const over = sliceFunction('qh-app.jsx', 'qhActionBarOverflows', ui.win);
  const box = (right) => ({ getBoundingClientRect: () => ({ right }) });
  const bar = ui.doc.createElement('div');
  bar.getBoundingClientRect = () => ({ right: 1000 });
  const kid = (right, fixed) => { const c = ui.doc.createElement('span'); Object.assign(c, box(right)); Object.defineProperty(c, 'offsetParent', { value: fixed ? null : bar }); return c; };
  bar.append(kid(500), kid(1400, true));
  assert.equal(over(bar), false, 'the fixed schedule popover does not count');
  bar.append(kid(1001));
  assert.equal(over(bar), true);
});

test('the CSS hides words step by step and wraps last; every short word starts hidden', () => {
  assert.match(css, /\.qh-actionbar\[data-fit~="1"\] \.qh-ab-blabel \{ display: none; \}/);
  assert.match(css, /\.qh-actionbar\[data-fit~="1"\] \.qh-runon-k, \.qh-actionbar\[data-fit~="1"\] \.qh-runon-warn \{ display: none; \}/);
  assert.match(css, /\n  \.qh-ab-short \{ display: none; \}/);
  assert.match(css, /\.qh-actionbar\[data-fit~="2"\] \.qh-ab-long, \.qh-actionbar\[data-fit~="2"\] \.qh-sec-label, \.qh-actionbar\[data-fit~="2"\] \.qh-sel-count \{ display: none; \}/);
  assert.match(css, /\.qh-actionbar\[data-fit~="3"\] \{ flex-wrap: wrap; height: auto;/);
  assert.doesNotMatch(css, /\.qh-actionbar\.is-tight/, 'the old width breakpoint is gone');
});

test('the bar has no width breakpoint left, and Runs on is not told to be tight', () => {
  const bar = app.slice(app.indexOf('function ActionBar('), app.indexOf('<div className="qh-target">'));
  assert.doesNotMatch(bar, /1120|is-tight|setTight/);
  assert.match(bar, /React\.useLayoutEffect\(\(\) => refit\(false\)\)/);
  assert.match(app, /function RunOnToggle\(\{ conn, runOn, tier, onRunOn \}\)/);
});

test('each chip that gets short keeps a long and a short label side by side', () => {
  const mask = app.slice(app.indexOf('function MaskToggle('), app.indexOf('function RunOnOpt('));
  assert.match(mask, /<span className="qh-ab-long">\{label\}<\/span><span className="qh-ab-short">\{short\}<\/span>/);
  assert.match(app, /<span className="qh-ab-long">Also send the result to Slack<\/span><span className="qh-ab-short">Slack too<\/span>/);
  assert.match(app, /className="qh-sel-lines qh-sel-count"/);
  assert.match(app, /className=\{'qh-sec-badge tier-' \+ classify\.tier\.toLowerCase\(\)\} title=\{tierLabel\}/);
});
