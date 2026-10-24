// Dark mode and focus (2026-10-24).
//
// (a) The provider logo has a dark variant (the AWS mark is dark ink). qhProviderLogo
//     reads <html data-theme> while a component renders, and App set the attribute in
//     an effect, AFTER its children rendered. So the logo stayed on the old variant
//     until something else re-rendered. App now writes the attribute in its render
//     body (qhApplyTheme), and bumps a state on an OS theme change.
// (b) A dialog no longer opens with a heavy ring on Cancel or the close X. Focus goes
//     to a field that took it, else [data-autofocus], else the first text field, else
//     the panel. The ring is :focus-visible with the brand box-shadow.
import test from 'node:test';
import assert from 'node:assert/strict';

import { createRequire } from 'node:module';

import { loadInto } from './_load.mjs';
import { page, web, jsdomOpts as opts } from './_mount.mjs';

/** A window with a fake <html> that records data-theme. */
function fakeWin(osDark) {
  const attrs = {}, writes = [];
  const win = {
    document: { documentElement: {
      getAttribute: (k) => (k in attrs ? attrs[k] : null),
      setAttribute: (k, v) => { attrs[k] = v; writes.push([k, v]); } } },
    matchMedia: () => ({ matches: osDark }),
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    navigator: { platform: 'test', userAgent: 'node', clipboard: null },
    React: null,
  };
  win.window = win;
  win.React = createRequire(import.meta.url)('react');
  return { win, attrs, writes };
}

test('qhApplyTheme resolves system from the OS and writes only when the value changes', () => {
  const dark = fakeWin(true);
  const { qhApplyTheme } = loadInto(dark.win, 'qh-data.jsx');
  assert.equal(qhApplyTheme('system'), 'dark');
  assert.equal(dark.attrs['data-theme'], 'dark');
  qhApplyTheme('system'); qhApplyTheme('system');
  assert.equal(dark.writes.length, 1, 'no write while it is unchanged');
  assert.equal(qhApplyTheme('light'), 'light');
  assert.equal(dark.attrs['data-theme'], 'light');
  assert.equal(qhApplyTheme('dark'), 'dark');
  const light = fakeWin(false);
  assert.equal(loadInto(light.win, 'qh-data.jsx').qhApplyTheme('system'), 'light');
});

test('the provider logo flips to its dark variant in the same call that sets the theme', () => {
  const f = fakeWin(false);
  const w = loadInto(f.win, 'qh-data.jsx');
  w.qhApplyTheme('light');
  assert.match(w.qhProviderLogo('aws'), /providers\/aws\.svg$/);
  w.qhApplyTheme('dark');
  assert.match(w.qhProviderLogo('aws'), /providers\/aws-dark\.svg$/);
  w.qhApplyTheme('system');   // OS is light
  assert.match(w.qhProviderLogo('aws'), /providers\/aws\.svg$/);
  assert.match(w.qhProviderLogo('huawei'), /huawei\.svg$/, 'a provider with no dark variant is unchanged');
});

test('App sets the theme in its render body, before any child, and bumps on an OS change', () => {
  const app = web('qh-app.jsx');
  const body = app.slice(app.indexOf('function App()'));
  const apply = body.indexOf('qhApplyTheme(t.theme);\n  const resolvedDark');
  assert.ok(apply > 0, 'qhApplyTheme in the render body');
  assert.ok(apply < body.indexOf('const localClassify'), 'before anything derived or rendered');
  assert.ok(apply < body.indexOf('if (!user) return <LoginScreen'), 'before the early returns');
  assert.match(body, /const \[, setOsTick\] = useState\(0\);/);
  assert.match(body, /const apply = \(\) => \{ qhApplyTheme\(t\.theme\); setOsTick\(n => n \+ 1\); \};/);
  assert.match(body, /mq\.addEventListener\('change', apply\)/);
  // The old effect wrote the attribute by hand; there is one writer now.
  assert.doesNotMatch(body, /document\.documentElement\.setAttribute\('data-theme'/);
});

// ---------- focus ----------

function modal(children, extra) {
  const ui = page(['qh-data.jsx', 'qh-modal.jsx']);
  const h = ui.React.createElement;
  const closed = { n: 0 };
  ui.render(h(ui.win.QhModal, { onClose: () => { closed.n++; }, ...extra },
    h('div', { className: 'qh-modal-head' }, h('div', null, h('div', { className: 'qh-modal-title' }, 'Title')),
      h('button', { className: 'qh-icon-btn', 'aria-label': 'Close' }, 'x')),
    h('div', { className: 'qh-modal-body' }, ...children(h)),
    h('div', { className: 'qh-modal-foot' }, h('button', { className: 'qh-btn' }, 'Cancel'), h('button', { className: 'qh-btn' }, 'Go'))));
  return { ...ui, closed, panel: ui.doc.querySelector('.qh-modal') };
}

test('a dialog with no field opens on the panel, not on the close X or Cancel', opts, () => {
  const ui = modal(() => []);
  assert.equal(ui.doc.activeElement, ui.panel);
  assert.equal(ui.panel.getAttribute('tabindex'), '-1');
});

test('the first text field takes focus, not the close X', opts, () => {
  const ui = modal((h) => [h('input', { type: 'checkbox', key: 'c' }), h('input', { className: 'qh-input', key: 'i' }), h('textarea', { key: 't' })]);
  assert.equal(ui.doc.activeElement, ui.doc.querySelector('input.qh-input'));
});

test('an element marked data-autofocus wins over a text field', opts, () => {
  const ui = modal((h) => [h('input', { className: 'qh-input', key: 'i' }), h('button', { 'data-autofocus': true, key: 'b' }, 'Keep')]);
  assert.equal(ui.doc.activeElement.textContent, 'Keep');
});

test('a field that took focus itself keeps it', opts, () => {
  const ui = modal((h) => [h('input', { className: 'qh-input', key: 'a' }), h('input', { className: 'qh-input second', key: 'b', autoFocus: true })]);
  assert.ok(ui.doc.activeElement.classList.contains('second'));
});

test('Shift+Tab from the panel stays in the dialog, and Escape closes it', opts, () => {
  const ui = modal(() => []);
  const ev = new ui.win.KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true });
  ui.React.act(() => { ui.panel.dispatchEvent(ev); });
  assert.equal(ev.defaultPrevented, true, 'the browser does not move focus out of the dialog');
  ui.key(ui.panel, 'Escape');
  assert.equal(ui.closed.n, 1);
});

test('focus returns to the control that opened the dialog', opts, () => {
  const ui = page(['qh-data.jsx', 'qh-modal.jsx']);
  const h = ui.React.createElement;
  const App = () => { const [on, set] = ui.React.useState(false);
    return h('div', null, h('button', { id: 'opener', onClick: () => set(true) }, 'Open'),
      on && h(ui.win.QhModal, { onClose: () => set(false) }, h('div', { className: 'qh-modal-title' }, 'T'), h('input', { className: 'qh-input', autoFocus: true }))); };
  ui.render(h(App));
  const opener = ui.doc.getElementById('opener');
  opener.focus();
  ui.click(opener);
  assert.equal(ui.doc.activeElement.tagName, 'INPUT');
  ui.key(ui.doc.activeElement, 'Escape');
  assert.equal(ui.doc.activeElement, opener, 'back on the opener, although the input had taken focus first');
});

test('the CSS: a ring on :focus-visible with the brand shadow, and no outline on the panel', () => {
  const css = web('QueryHub.html');
  assert.match(css, /\.qh-modal:focus \{ outline: none; \}/);
  assert.match(css, /\.qh-modal :focus-visible \{ outline: none; box-shadow: 0 0 0 2px var\(--brand-adaptive-medium\); \}/);
});
