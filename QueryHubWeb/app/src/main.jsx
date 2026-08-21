// Entry point. Order mirrors the prototype's <script> tags in
// QueryHub.html: globals (window.React/ReactDOM) first, then CSS, then
// each component module in load order. qh-app.jsx renders <App/> last.
import './globals.js';
import './index.css';
import './tweaks-panel.jsx';
import './qh-api.jsx';
import './qh-data.jsx';
import './qh-version.js'; // build-stamp override for QH_VERSION (generated)
import './qh-modal.jsx';  // QhModal shell used by qh-panels + qh-app
import './qh-login.jsx';
import './qh-editor.jsx';
import './qh-panels.jsx';
import './qh-home.jsx';
import './qh-admin-data.jsx';
import './qh-admin-access.jsx';
// New in the 2026-08-21 (c) round. The prototype loads it from a <script>
// tag in QueryHub.html; the bundle needs this import as well, or the file
// is simply absent from the build — `PersonAccessView` is referenced by
// qh-admin-access.jsx and would be undefined the moment Grants opens on
// its new default tab. Same failure shape as a stale index.css: the raw
// prototype is fine and only the built app is broken.
import './qh-admin-person.jsx';
import './qh-admin-insights.jsx';
import './qh-admin-config.jsx';
import './qh-admin.jsx';
import './qh-whatsnew.jsx';
import './qh-app.jsx';
