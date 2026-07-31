// Build-version override. The backend stamps the deployed build (the git commit
// the web service runs out of) into index.html as
//
//     <meta name="qh-build" content='{"version":"...","sha":"...",...}'>
//
// and this file, which loads right after qh-data.jsx, promotes it over the
// design's hardcoded QH_VERSION constant. Falls back to that constant when the
// tag is absent (e.g. the raw prototype served without the FastAPI backend).
//
// It's a meta tag rather than an injected `<script>` on purpose: a stamped
// inline script would force `script-src 'unsafe-inline'` in the CSP for the sake
// of two assignments, which is exactly the hole an XSS needs. Data in the DOM,
// read by a bundled script, keeps the policy at 'self'.
(function () {
  var build = null;
  var tag = document.querySelector('meta[name="qh-build"]');
  if (tag && tag.content) {
    try {
      build = JSON.parse(tag.content);
    } catch (e) {
      // Malformed stamp: keep the design constants rather than blanking the
      // version display. Nothing else depends on this.
      build = null;
    }
  }
  // window.__QH_BUILD__ is the older injection shape — still honoured so a
  // stale cached index.html (or a custom deployment that stamps that global)
  // keeps working.
  build = build || window.__QH_BUILD__ || null;

  if (build && build.version) window.QH_VERSION = build.version;
  else if (window.__QH_VERSION__) window.QH_VERSION = window.__QH_VERSION__;

  // The richer object (version/date/sha/branch/repo) the What's-new page and the
  // sidebar build stamp read.
  if (build) window.QH_BUILD = build;
})();
