// The prototype components (kept verbatim, not adapted for the bundler) share
// symbols via `window` and reference `React.*` / `ReactDOM.createRoot`
// as globals — the CDN + Babel-in-browser pattern. We keep that code
// UNCHANGED and just satisfy those globals here, so the Vite bundle is a
// faithful build of the exact same components (no UI edits). This module
// is imported FIRST in main.jsx, so window.React is set before any
// component module runs.
import React from 'react';
import { createRoot } from 'react-dom/client';

window.React = React;
window.ReactDOM = { createRoot };
