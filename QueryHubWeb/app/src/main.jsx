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
import './qh-admin-insights.jsx';
import './qh-admin-config.jsx';
import './qh-admin.jsx';
import './qh-whatsnew.jsx';
import './qh-app.jsx';
