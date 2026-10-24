// A jsdom page with the real UI files loaded, for the batch 1 tests (2026-10-24).
// Not a test file: `npm test` runs only `*.test.mjs`.
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { transformSync } from 'esbuild';

import { loadInto } from './_load.mjs';

const require_ = createRequire(import.meta.url);
const HERE = dirname(fileURLToPath(import.meta.url));
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }
export const jsdomOpts = JSDOM ? {} : { skip: 'jsdom not installed' };

export const web = (file) => readFileSync(resolve(HERE, '..', '..', file), 'utf8');

/** The design-token file the page links, and whether it is the vendor file.
 *  The open-source tree links its own token file, so tests that read tokens go
 *  through here, and pin exact contrast numbers only for the vendor values. */
export const tokenFile = () => {
  const m = web('QueryHub.html').match(/<link rel="stylesheet" href="([^"]*(?:colors_and_type|tokens)\.css)"/);
  if (!m) throw new Error('QueryHub.html links no token file');
  return m[1];
};
export const tokensCss = () => web(tokenFile());
export const isVendorTokens = () => tokenFile().startsWith('theme-vendor/');

/** A fresh page. `files` are loaded from src/ in order. */
export function page(files, { url = 'https://localhost/', prep } = {}) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { pretendToBeVisual: true, url });
  for (const k of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node', 'Event', 'KeyboardEvent', 'MouseEvent',
                   'getComputedStyle', 'requestAnimationFrame', 'cancelAnimationFrame', 'localStorage', 'location', 'history']) {
    Object.defineProperty(globalThis, k, { value: dom.window[k], configurable: true, writable: true });
  }
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const React = require_('react');
  const ReactDOM = require_('react-dom/client');
  globalThis.React = React;
  const win = dom.window;
  win.React = React;
  win.ReactDOM = require_('react-dom');
  if (prep) prep(win);
  loadInto(win, ...files);
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  const render = (el) => React.act(() => root.render(el));
  const click = (el) => React.act(() => el.dispatchEvent(new win.MouseEvent('click', { bubbles: true })));
  const flush = () => React.act(() => new Promise(r => setTimeout(r, 0)));
  const all = (sel) => [...win.document.querySelectorAll(sel)];
  const key = (el, k) => React.act(() => el.dispatchEvent(new win.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true })));
  return { win, doc: win.document, React, render, click, flush, all, key };
}

/** Evaluate one top-level function of a UI file that cannot be loaded whole
 *  (qh-app.jsx renders the app when it loads). `names` are window globals the
 *  function needs. Returns the function. */
export function sliceFunction(file, name, win) {
  const src = web(file);
  const start = src.indexOf('function ' + name + '(');
  if (start < 0) throw new Error(name + ' is not in ' + file);
  const end = src.indexOf('\n}\n', start);
  const code = transformSync(src.slice(start, end + 3) + '\nwindow.' + name + ' = ' + name + ';', {
    loader: 'jsx', jsx: 'transform', jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment',
  }).code;
  new Function('window', 'with (window) {\n' + code + '\n}')(win);
  return win[name];
}
