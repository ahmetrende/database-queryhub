// The prototype components (kept verbatim, not adapted for the bundler) share
// symbols via `window` and reference `React.*` / `ReactDOM.createRoot`
// as globals — the CDN + Babel-in-browser pattern. We keep that code
// UNCHANGED and just satisfy those globals here, so the Vite bundle is a
// faithful build of the exact same components (no UI edits). This module
// is imported FIRST in main.jsx, so window.React is set before any
// component module runs.
import React from 'react';
import { createRoot } from 'react-dom/client';
// `createPortal` lives in `react-dom`, not `react-dom/client`. The prototype
// loads the full react-dom UMD from a CDN, so every ReactDOM.* the design uses
// exists there and only the bundle can be missing one — the same asymmetry the
// note above describes for a stale index.css. The editor's autocomplete portals
// itself to document.body (2026-08-27 round); without this line the built app
// throws "ReactDOM.createPortal is not a function" the moment a suggestion
// list opens, while the prototype is fine.
import { createPortal } from 'react-dom';

window.React = React;
window.ReactDOM = { createRoot, createPortal };
