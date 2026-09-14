#!/usr/bin/env python3
"""Build metrics_dashboard.html from request_facts + lookups.

Architecture: pull every reportable request as a denormalized fact row,
ship the rows + lookup dictionaries as inline JSON, render an HTML
shell, and let a JS renderer aggregate + filter on the client. Every
chart re-computes from the filtered row set on each filter change.

Why client-side: the only way to support arbitrary intersections of
the filter dimensions (date / team / user / target / db / tier /
status) without exploding into N pre-aggregated SQL views. Pilot
volume is small (≤ tens of thousands of rows at 100x growth), so
shipping the raw projection is cheap and the user gets instant
filter feedback with no round-trip.
"""

import sys
import json
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal

# Make `from dba_slack_bot import ...` work when invoked from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dba_slack_bot import db, metrics_defs  # noqa: E402


REPO_DIR = Path(__file__).resolve().parent.parent
OUT_HTML = REPO_DIR / "metrics_dashboard.html"


# ----------------------------- helpers -------------------------------------


def _json_default(o):
    """JSON serializer for Postgres-native types Python can't dump."""
    if isinstance(o, datetime):
        # Always emit UTC ISO strings; the JS side converts to local TZ
        # on display when needed.
        if o.tzinfo is None:
            o = o.replace(tzinfo=timezone.utc)
        return o.astimezone(timezone.utc).isoformat()
    if isinstance(o, Decimal):
        return float(o)
    return str(o)


def _fetch_all(cur, sql, params=()):
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


# ----------------------------- payload -------------------------------------


def fetch_payload() -> dict:
    """Pull every reportable request + the lookups the filter UI needs."""
    with db.connection() as conn:
        with conn.cursor() as cur:
            rows = _fetch_all(cur, "SELECT * FROM p_metrics_request_facts "
                                   "ORDER BY created_at")

            # Annotations: render as vertical lines on time-axis charts.
            annotations_raw = _fetch_all(
                cur,
                "SELECT occurred_at, label FROM metric_annotations "
                " ORDER BY occurred_at",
            )

            teams = [r["team"] for r in _fetch_all(
                cur,
                "SELECT DISTINCT team FROM p_metrics_request_facts "
                " WHERE team IS NOT NULL ORDER BY team",
            )]

            users = [
                {"id": r["requester_slack_id"],
                 "name": r["requester_name"] or r["requester_slack_id"]}
                for r in _fetch_all(
                    cur,
                    "SELECT DISTINCT requester_slack_id, requester_name "
                    "  FROM p_metrics_request_facts "
                    " ORDER BY requester_name NULLS LAST",
                )
            ]

            targets = [r["target_alias"] for r in _fetch_all(
                cur,
                "SELECT DISTINCT target_alias FROM p_metrics_request_facts "
                " WHERE target_alias IS NOT NULL ORDER BY target_alias",
            )]

            databases = [r["database_name"] for r in _fetch_all(
                cur,
                "SELECT DISTINCT database_name FROM p_metrics_request_facts "
                " WHERE database_name IS NOT NULL ORDER BY database_name",
            )]

            # Cost-saving inputs + report window — used by the KPI cards. The
            # key list comes from metrics_defs so this query and the web API's
            # cannot select different keys (they did not, but nothing said so).
            cfg_rows = _fetch_all(
                cur,
                "SELECT key, value FROM bot_config WHERE key IN ("
                + metrics_defs.config_key_sql_list() + ")",
            )
            config = {r["key"]: r["value"] for r in cfg_rows}

            # Static reference data — admins + their grants. Not affected
            # by request-side filters because it's an org structure view.
            who_can_what = _fetch_all(
                cur, "SELECT * FROM p_metrics_who_can_what ORDER BY name")

            # Low-rating feedback table — joined onto rows so the dashboard
            # can render the feedback text alongside the rating.
            rating_low = _fetch_all(
                cur,
                "SELECT * FROM p_metrics_rating_low_with_feedback "
                " ORDER BY rated_at DESC",
            )

            # CSV bulk imports — one row per /sql import, denormalized with
            # the target alias and minus self-test traffic. Aggregated
            # client-side for the CSV-import section.
            csv_imports = _fetch_all(
                cur,
                "SELECT id, created_at, requester_slack_id, requester_name, "
                "       target_alias, database_name, table_name, is_new_table, "
                "       status, row_count, inserted_rows, byte_size, load_seconds "
                "  FROM p_metrics_csv_imports ORDER BY created_at",
            )

    return {
        "rows": rows,
        "annotations": [
            {
                "x": a["occurred_at"].date().isoformat(),
                "label": a["label"],
            } for a in annotations_raw
        ],
        "teams": teams,
        "users": users,
        "targets": targets,
        "databases": databases,
        "config": config,
        "who_can_what": who_can_what,
        "rating_low": rating_low,
        "csv_imports": csv_imports,
        "generated_at": _now_tr_label(),
    }


# ----------------------------- timestamp helper ----------------------------


def _now_authoritative() -> datetime:
    """Prefer an external authoritative time source so a drifted host
    clock can't quietly emit a wrong 'last updated' label. Falls back
    to local clock on any error."""
    import urllib.request
    from email.utils import parsedate_to_datetime
    try:
        req = urllib.request.Request("https://www.google.com", method="HEAD")
        with urllib.request.urlopen(req, timeout=3) as resp:
            date_hdr = resp.headers.get("Date")
        if date_hdr:
            dt = parsedate_to_datetime(date_hdr)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    except Exception:
        pass
    return datetime.now(timezone.utc)


def _now_tr_label() -> str:
    """`22 May 2026 · 11:30 TR` style label for the dashboard header."""
    from zoneinfo import ZoneInfo
    try:
        utc = _now_authoritative()
        tr = utc.astimezone(ZoneInfo("Europe/Istanbul"))
        return tr.strftime("%d %b %Y · %H:%M") + " TR"
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ----------------------------- HTML ----------------------------------------


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>QueryHub — product metrics dashboard</title>
<link rel="icon" type="image/svg+xml" href="queryhub-mark.svg">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3.0.1/dist/chartjs-plugin-annotation.min.js"></script>
<style>
  :root {
    color-scheme: light dark;

    --fg-primary:   #1F2229;
    --fg-secondary: rgba(31, 34, 41, 0.80);
    --fg-tertiary:  rgba(31, 34, 41, 0.60);
    --fg-accent:    #7B9530;
    --fg-danger:    #E53D3D;
    --fg-warning:   #D97706;

    --bg-page:    #FFFFFF;
    --bg-light:   #F9FAFB;
    --bg-regular: #F5F6F8;
    --bg-strong:  #ECEDF0;

    --stroke-light:  rgba(31, 34, 41, 0.04);
    --stroke-medium: rgba(31, 34, 41, 0.08);
    --stroke-strong: rgba(31, 34, 41, 0.15);

    --brand-solid-light:  #9BBA3C;
    --brand-solid-medium: #7B9530;
    --brand-adaptive-md:  rgba(155, 186, 60, 0.20);

    --shadow-card: 0 1px 2px rgba(31, 34, 41, 0.04),
                   0 1px 1px rgba(31, 34, 41, 0.02);

    --space-xs:  4px;
    --space-ms:  8px;
    --space-md:  12px;
    --space-ml:  16px;
    --space-lg:  20px;
    --space-xl:  24px;
    --space-2xl: 32px;
    --space-3xl: 40px;

    --radius-sm:   8px;
    --radius-md:   12px;
    --radius-lg:   16px;
    --radius-xl:   20px;
    --radius-full: 999px;

    --surface-bg:     #FFFFFF;
    --surface-border: var(--stroke-medium);
    --table-stripe:   var(--bg-regular);
    --table-hover:    var(--bg-light);
  }

  @media (prefers-color-scheme: dark) {
    :root {
      --fg-primary:   #FFFFFF;
      --fg-secondary: rgba(255, 255, 255, 0.80);
      --fg-tertiary:  rgba(255, 255, 255, 0.50);
      --fg-accent:    #9BBA3C;
      --fg-danger:    #E53D3D;
      --fg-warning:   #F59E0B;

      --bg-page:    #181A20;
      --bg-light:   #1F2229;
      --bg-regular: #232A31;
      --bg-strong:  #434954;

      --stroke-light:  rgba(255, 255, 255, 0.04);
      --stroke-medium: rgba(255, 255, 255, 0.08);
      --stroke-strong: rgba(255, 255, 255, 0.15);

      --brand-solid-light:  #9BBA3C;
      --brand-solid-medium: #9BBA3C;
      --brand-adaptive-md:  rgba(155, 186, 60, 0.30);

      --shadow-card: 0 1px 2px rgba(0, 0, 0, 0.48),
                     0 1px 1px rgba(0, 0, 0, 0.32);

      --surface-bg:     var(--bg-light);
      --surface-border: var(--stroke-medium);
      --table-stripe:   var(--bg-regular);
      --table-hover:    var(--bg-regular);
    }
  }

  body {
    font-family: 'Manrope', -apple-system, "Segoe UI", Roboto,
                 system-ui, sans-serif;
    margin: 0;
    padding: var(--space-xl);
    max-width: 1280px;
    margin-inline: auto;
    background: var(--bg-light);
    color: var(--fg-primary);
    font-size: 14px;
    line-height: 22px;
    -webkit-font-smoothing: antialiased;
  }

  h1 {
    font-size: 32px; line-height: 40px; font-weight: 600;
    margin: 0 0 var(--space-xs); letter-spacing: -0.01em;
  }
  h2 {
    font-size: 20px; line-height: 24px; font-weight: 600;
    margin: var(--space-xl) 0 var(--space-ms);
    letter-spacing: -0.005em;
  }
  .meta {
    color: var(--fg-tertiary);
    font-size: 12px; line-height: 16px;
    margin: 0;
  }

  .page-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-xl);
    margin-bottom: var(--space-xl);
    flex-wrap: wrap;
  }
  .page-header h1 { margin-bottom: var(--space-xs); }

  .brand {
    display: flex;
    align-items: center;
    gap: var(--space-md);
    margin-bottom: var(--space-ms);
  }
  .brand__logo {
    width: 44px; height: 44px;
    flex-shrink: 0;
    border-radius: var(--radius-sm);
    object-fit: contain;
  }
  .brand h1 { margin: 0; }

  .updated-pill {
    display: inline-flex;
    align-items: center;
    gap: var(--space-ms);
    background: var(--surface-bg);
    border: 1px solid var(--surface-border);
    border-radius: var(--radius-full);
    padding: var(--space-ms) var(--space-ml);
    box-shadow: var(--shadow-card);
    font-size: 12px; line-height: 16px;
    color: var(--fg-secondary);
    white-space: nowrap;
  }
  .updated-pill__dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--brand-solid-light);
    box-shadow: 0 0 0 0 var(--brand-adaptive-md);
    animation: pulse 2.4s ease-out infinite;
  }
  .updated-pill__label {
    color: var(--fg-tertiary);
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em;
    font-weight: 500;
  }
  @keyframes pulse {
    0%   { box-shadow: 0 0 0 0 var(--brand-adaptive-md); }
    70%  { box-shadow: 0 0 0 10px transparent; }
    100% { box-shadow: 0 0 0 0 transparent; }
  }

  /* Filter panel — sits between the page header and the TOC. Two
   * rows: presets / date range on top, multi-select dropdowns
   * underneath. Everything client-side. */
  .filters {
    background: var(--surface-bg);
    border: 1px solid var(--surface-border);
    border-radius: var(--radius-lg);
    padding: var(--space-ml) var(--space-lg);
    margin-bottom: var(--space-ml);
    box-shadow: var(--shadow-card);
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
  }
  .filters__row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-ms);
    align-items: center;
  }
  .filters__label {
    color: var(--fg-tertiary);
    font-size: 11px; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.06em;
    min-width: 70px;
  }
  .filter-btn {
    background: transparent;
    color: var(--fg-secondary);
    border: 1px solid transparent;
    border-radius: var(--radius-full);
    padding: 4px 14px;
    font-size: 13px;
    line-height: 18px;
    cursor: pointer;
    transition: background 0.15s ease, color 0.15s ease;
    font-family: inherit;
  }
  .filter-btn:hover { background: var(--bg-regular); color: var(--fg-primary); }
  .filter-btn.active {
    background: var(--brand-solid-medium);
    color: #FFFFFF;
    font-weight: 500;
  }
  .filter-btn--reset {
    border: 1px solid var(--stroke-strong);
    color: var(--fg-secondary);
    margin-left: auto;
  }
  .filter-btn--reset:hover {
    background: var(--bg-regular);
  }
  .filter-date {
    background: var(--bg-page);
    border: 1px solid var(--stroke-strong);
    border-radius: var(--radius-sm);
    padding: 4px 10px;
    font-size: 13px;
    font-family: inherit;
    color: var(--fg-primary);
    color-scheme: light dark;
  }
  .filter-select {
    background: var(--bg-page);
    border: 1px solid var(--stroke-strong);
    border-radius: var(--radius-sm);
    padding: 4px 10px;
    font-size: 13px;
    font-family: inherit;
    color: var(--fg-primary);
    min-width: 140px;
    cursor: pointer;
  }
  .filter-summary {
    font-size: 12px;
    color: var(--fg-tertiary);
    margin-left: auto;
  }
  .filter-summary strong {
    color: var(--fg-primary);
    font-weight: 600;
  }

  /* Layout primitives ----------------------------------------------------- */

  .toc {
    background: var(--surface-bg);
    border: 1px solid var(--surface-border);
    border-radius: var(--radius-lg);
    padding: var(--space-ml) var(--space-lg);
    margin-bottom: var(--space-xl);
    box-shadow: var(--shadow-card);
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: var(--space-xs) var(--space-ml);
  }
  .toc a {
    color: var(--fg-accent);
    text-decoration: none;
    font-size: 13px;
    line-height: 18px;
    padding: 3px 0;
  }
  .toc a:hover { color: var(--brand-solid-medium); text-decoration: underline; }

  .card {
    background: var(--surface-bg);
    border: 1px solid var(--surface-border);
    border-radius: var(--radius-lg);
    padding: var(--space-lg);
    margin-bottom: var(--space-ml);
    box-shadow: var(--shadow-card);
  }
  .card h2:first-child { margin-top: 0; }
  .chart-wrap {
    position: relative;
    height: 340px;
    overflow: auto;
  }
  /* Tables / KPI grids don't need a fixed canvas height; the
   * factories opt into auto-sizing via this modifier. */
  .chart-wrap--auto {
    height: auto;
    min-height: 80px;
  }
  .anno-pill {
    display: inline-block;
    background: var(--fg-danger);
    color: #FFFFFF;
    font-size: 11px;
    font-weight: 600;
    line-height: 14px;
    padding: 3px 10px;
    border-radius: var(--radius-full);
    margin-right: var(--space-xs);
  }

  /* KPI / table / heatmap --------------------------------------------------- */
  .kpi-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: var(--space-ml);
  }
  .kpi {
    background: var(--bg-regular);
    border-radius: var(--radius-md);
    padding: var(--space-ml);
  }
  .kpi__label {
    color: var(--fg-tertiary);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-weight: 500;
    margin-bottom: var(--space-xs);
  }
  .kpi__value {
    color: var(--fg-primary);
    font-size: 24px;
    font-weight: 600;
    line-height: 28px;
    letter-spacing: -0.005em;
  }
  .kpi__hint {
    color: var(--fg-tertiary);
    font-size: 11px;
    margin-top: var(--space-xs);
  }

  table.data {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  table.data th,
  table.data td {
    text-align: left;
    padding: 6px 10px;
    border-bottom: 1px solid var(--stroke-light);
  }
  table.data th {
    color: var(--fg-tertiary);
    font-weight: 500;
    text-transform: uppercase;
    font-size: 11px;
    letter-spacing: 0.04em;
    background: var(--bg-regular);
  }
  table.data tr:hover td { background: var(--table-hover); }

  .heatmap {
    display: grid;
    gap: 2px;
  }
  .heatmap__cell {
    text-align: center;
    padding: 6px 2px;
    font-size: 11px;
    border-radius: 4px;
    color: var(--fg-primary);
  }
  .heatmap__row-label {
    font-size: 11px;
    color: var(--fg-tertiary);
    padding-right: 6px;
    text-align: right;
  }

  .empty {
    color: var(--fg-tertiary);
    font-style: italic;
    padding: var(--space-ml);
    text-align: center;
  }
</style>
</head>
<body>
<div class="page-header">
  <div>
    <div class="brand">
      <img class="brand__logo" src="queryhub-mark.svg" alt="QueryHub" width="44" height="44">
      <h1>QueryHub — product metrics</h1>
    </div>
    <div class="meta">Internal dashboard. Auto-refreshed by the
      <code>1_hour</code> publisher; reload the page to pull the
      latest copy from S3.</div>
  </div>
  <div class="updated-pill"
       title="Wall-clock time the HTML was built, sourced from a HEAD request to google.com (NTP-synced)">
    <span class="updated-pill__dot"></span>
    <span class="updated-pill__label">Last updated</span>
    <span class="updated-pill__value">%GENERATED_AT%</span>
  </div>
</div>

<div class="filters" role="region" aria-label="Dashboard filters">
  <div class="filters__row">
    <span class="filters__label">Date range</span>
    <button class="filter-btn active" data-preset="all">All</button>
    <button class="filter-btn" data-preset="90">Last 90d</button>
    <button class="filter-btn" data-preset="30">Last 30d</button>
    <button class="filter-btn" data-preset="7">Last 7d</button>
    <button class="filter-btn" data-preset="today">Today</button>
    <span class="filters__label" style="margin-left: var(--space-md)">Custom</span>
    <input type="date" id="filter-from" class="filter-date" aria-label="From date">
    <span style="color: var(--fg-tertiary)">→</span>
    <input type="date" id="filter-to"   class="filter-date" aria-label="To date">
    <button class="filter-btn filter-btn--reset" id="filter-reset"
            type="button" title="Clear every filter">Reset all</button>
  </div>
  <div class="filters__row">
    <span class="filters__label">Slice by</span>
    <select id="filter-team"    class="filter-select" aria-label="Team"></select>
    <select id="filter-user"    class="filter-select" aria-label="Requester"></select>
    <select id="filter-target"  class="filter-select" aria-label="Target server (RDS)"></select>
    <select id="filter-db"      class="filter-select" aria-label="Database"></select>
    <select id="filter-tier"    class="filter-select" aria-label="Tier"></select>
    <select id="filter-status"  class="filter-select" aria-label="Status"></select>
    <span class="filter-summary"
          id="filter-summary">—</span>
  </div>
</div>

<div class="toc">
%TOC%
</div>

%SECTIONS%

<script>
window.__DATA = %DATA%;
%RENDERER_JS%
</script>
</body>
</html>"""


# ----------------------------- chart specs ---------------------------------
#
# Each spec is (id, title, factory_name). The renderer JS picks the
# factory function by name and runs it against the current filtered
# row set. Title is human display.


CHART_SPECS = [
    # KPIs come first — at-a-glance numbers
    ("kpi-headline",   "Headline KPIs",                                       "kpi"),
    ("cost-savings",   "Cost savings snapshot",                               "kpiCostSavings"),
    # Volume / status mix
    ("volume-daily",   "Daily volume — status mix + active users",            "volumeDaily"),
    ("volume-weekly",  "Weekly volume (status mix + WAU overlay)",            "volumeWeekly"),
    ("volume-monthly", "Monthly volume (status mix + MAU overlay)",           "volumeMonthly"),
    # Outcomes
    ("failure-breakdown", "Terminal outcomes per week (success-rate overlay)","failureBreakdown"),
    # Tier
    ("tier-distribution", "Tier mix per week (ro / rw / ddl)",                "tierDistribution"),
    # Adoption
    ("scheduled-usage",  "Scheduled-request adoption (weekly %)",             "scheduledUsage"),
    # Latency
    ("approval-sla", "Approval latency percentiles",                          "approvalSla"),
    # Hours
    ("business-offhours","Business hours vs off-hours (weekly)",              "businessOffhours"),
    ("peak-hours",       "Peak hours — request count by day-of-week × hour",  "peakHours"),
    # Per-team / per-user / per-target
    ("team-usage", "Per-team usage",                                          "teamUsage"),
    ("top-users",  "Top 10 users by total requests",                          "topUsers"),
    ("admin-workload", "Admin workload (decisions taken)",                    "adminWorkload"),
    ("target-heatmap", "Target heatmap — usage by alias",                     "targetHeatmap"),
    # Ratings
    ("rating-weekly",  "Weekly avg rating (1-5) + counts",                    "ratingWeekly"),
    ("rating-response","Rating response rate (weekly %)",                     "ratingResponse"),
    ("rating-low",     "Low ratings (≤2) with feedback",                      "ratingLow"),
    # CSV bulk imports
    ("csv-imports",    "CSV bulk imports",                                    "csvImports"),
    # Static refs
    ("who-can-what",   "Who can do what",                                     "whoCanWhat"),
]


# ----------------------------- renderer JS ---------------------------------
#
# Embedded verbatim into the HTML. Pure ES2017, no build step. Reads
# window.__DATA; everything else is computed from rows + lookups.

RENDERER_JS = r"""
'use strict';

// ===== utilities ===========================================================

const DATA = window.__DATA;
const ROWS = DATA.rows.map(r => {
  // Re-hydrate decimals (Decimal-from-Postgres came across as numbers).
  // Dates stay as ISO strings; we parse them lazily in helpers.
  return r;
});

// Filter state — mutated by the UI handlers; rebuilt each render() call.
const STATE = {
  preset: 'all',     // 'all' | '90' | '30' | '7' | 'today' | 'custom'
  from: null,        // ISO YYYY-MM-DD (custom range only)
  to:   null,
  team:   '',
  user:   '',
  target: '',
  db:     '',
  tier:   '',
  status: '',
};

// Registered chart instances, keyed by chart spec id. Re-created from
// scratch on each render to avoid stale axis scales / annotations.
const CHARTS = {};

// Color palette — tied to status / tier so semantics stay consistent.
const C = {
  completed: '#9BBA3C',
  failed:    '#E53D3D',
  rejected:  '#F59E0B',
  cancelled: '#5A6170',
  pending:   '#9CA3AF',
  approved:  '#66CCFF',
  scheduled: '#BFB2FF',
  executing: '#144A66',
  awaiting_dba_manual: '#FF9933',
  changes_requested:   '#FF66B2',
  ro:           '#9BBA3C',
  rw:           '#FF9933',
  ddl_or_other: '#E53D3D',
  overlay:      '#66CCFF',
  accent:       '#BFB2FF',
  neutral:      '#5A6170',
};

// Theme tokens — read on load so chart text matches CSS.
function readToken(name, fallback) {
  const v = getComputedStyle(document.documentElement)
    .getPropertyValue(name).trim();
  return v || fallback;
}
const COLOR_FG_PRIMARY   = readToken('--fg-primary',   '#1F2229');
const COLOR_FG_SECONDARY = readToken('--fg-secondary', 'rgba(31,34,41,0.8)');
const COLOR_FG_TERTIARY  = readToken('--fg-tertiary',  'rgba(31,34,41,0.6)');
const COLOR_STROKE_MED   = readToken('--stroke-medium','rgba(31,34,41,0.08)');

Chart.defaults.color = COLOR_FG_SECONDARY;
Chart.defaults.borderColor = COLOR_STROKE_MED;
Chart.defaults.font.family = "'Manrope', -apple-system, 'Segoe UI', "
                           + "Roboto, system-ui, sans-serif";
Chart.defaults.font.size = 12;
if (window['chartjs-plugin-annotation']) {
  Chart.register(window['chartjs-plugin-annotation']);
}

const CHART_DEFAULTS = {
  responsive: true,
  maintainAspectRatio: false,
  interaction: { mode: 'index', intersect: false },
  plugins: {
    legend: {
      position: 'bottom',
      labels: {
        color: COLOR_FG_SECONDARY,
        usePointStyle: true,
        padding: 16,
        boxHeight: 8,
        font: { size: 12 },
      },
    },
    tooltip: {
      backgroundColor: COLOR_FG_PRIMARY,
      titleColor: '#FFFFFF',
      bodyColor: '#FFFFFF',
      padding: 10,
      cornerRadius: 8,
      titleFont: { size: 12, weight: '600' },
      bodyFont:  { size: 12 },
      displayColors: true,
      boxPadding: 6,
    },
  },
};

// ===== date helpers ========================================================

function parseISO(s) {
  return s ? new Date(s) : null;
}

function isoDate(d) {
  // YYYY-MM-DD in local time (matches the date-input value format).
  if (!d) return null;
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return y + '-' + m + '-' + dd;
}

function truncDay(iso) {
  return iso.slice(0, 10);
}

function truncWeek(iso) {
  // Monday-anchored ISO week start.
  const d = new Date(iso);
  const dow = d.getUTCDay();           // 0 = Sun
  const offset = (dow === 0 ? -6 : 1 - dow);
  d.setUTCDate(d.getUTCDate() + offset);
  d.setUTCHours(0, 0, 0, 0);
  return d.toISOString().slice(0, 10);
}

function truncMonth(iso) {
  return iso.slice(0, 7) + '-01';
}

function daysBetween(fromISO, toISO) {
  // Inclusive — returns an array of YYYY-MM-DD strings.
  const out = [];
  if (!fromISO || !toISO) return out;
  const from = new Date(fromISO + 'T00:00:00Z');
  const to   = new Date(toISO   + 'T00:00:00Z');
  const cur  = new Date(from);
  while (cur <= to) {
    out.push(cur.toISOString().slice(0, 10));
    cur.setUTCDate(cur.getUTCDate() + 1);
  }
  return out;
}

function weeksBetween(fromISO, toISO) {
  const out = [];
  if (!fromISO || !toISO) return out;
  let cur = truncWeek(fromISO);
  const last = truncWeek(toISO);
  while (cur <= last) {
    out.push(cur);
    const d = new Date(cur + 'T00:00:00Z');
    d.setUTCDate(d.getUTCDate() + 7);
    cur = d.toISOString().slice(0, 10);
  }
  return out;
}

function monthsBetween(fromISO, toISO) {
  const out = [];
  if (!fromISO || !toISO) return out;
  let y = +fromISO.slice(0, 4);
  let m = +fromISO.slice(5, 7);
  const lastY = +toISO.slice(0, 4);
  const lastM = +toISO.slice(5, 7);
  while (y < lastY || (y === lastY && m <= lastM)) {
    out.push(y + '-' + String(m).padStart(2, '0') + '-01');
    m += 1;
    if (m === 13) { y += 1; m = 1; }
  }
  return out;
}

// ===== aggregator helpers ==================================================

function pct(arr, p) {
  if (!arr || !arr.length) return null;
  const sorted = arr.slice().sort((a, b) => a - b);
  const idx = (sorted.length - 1) * p;
  const lo = Math.floor(idx);
  const hi = Math.ceil(idx);
  if (lo === hi) return sorted[lo];
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (idx - lo);
}

function chooseTimeUnit(rawValues) {
  // Pick seconds / minutes / hours based on the largest value across
  // the input series. Thresholds are deliberately lopsided so the
  // smaller percentiles stay readable when max sits just over a
  // coarser unit's natural boundary.
  let max = 0;
  rawValues.forEach(arr => arr.forEach(v => {
    if (v != null && isFinite(v)) max = Math.max(max, v);
  }));
  if (max < 120)    return { unit: 'seconds', div: 1,    decimals: 1 };
  if (max < 36000)  return { unit: 'minutes', div: 60,   decimals: 1 };
  return                   { unit: 'hours',   div: 3600, decimals: 2 };
}

function scaleTime(values, div, decimals) {
  return values.map(v => v == null ? 0 : +(v / div).toFixed(decimals));
}

// ===== filter logic ========================================================

function presetRange(preset) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const to = isoDate(today);
  if (preset === 'today') return { from: to, to: to };
  if (preset === '7')     {
    const d = new Date(today); d.setDate(d.getDate() - 6);
    return { from: isoDate(d), to: to };
  }
  if (preset === '30') {
    const d = new Date(today); d.setDate(d.getDate() - 29);
    return { from: isoDate(d), to: to };
  }
  if (preset === '90') {
    const d = new Date(today); d.setDate(d.getDate() - 89);
    return { from: isoDate(d), to: to };
  }
  return { from: null, to: null };  // 'all'
}

function applyFilters() {
  const range = STATE.preset === 'custom'
    ? { from: STATE.from, to: STATE.to }
    : presetRange(STATE.preset);

  return ROWS.filter(r => {
    if (range.from && r.created_at.slice(0, 10) < range.from) return false;
    if (range.to   && r.created_at.slice(0, 10) > range.to)   return false;
    if (STATE.team   && r.team           !== STATE.team)   return false;
    if (STATE.user   && r.requester_slack_id !== STATE.user)   return false;
    if (STATE.target && r.target_alias   !== STATE.target) return false;
    if (STATE.db     && r.database_name  !== STATE.db)     return false;
    if (STATE.tier   && r.tier           !== STATE.tier)   return false;
    if (STATE.status && r.status         !== STATE.status) return false;
    return true;
  });
}

// ===== annotations + weekends ==============================================

function annotationsFor(labels) {
  // labels are date strings (day / week-start / month-start). Match
  // each annotation to its bucket — day matches exact, week matches
  // any annotation in the [week, week+7) range, month similarly.
  if (!labels.length) return [];
  const out = [];
  DATA.annotations.forEach((a, i) => {
    const ax = a.x;
    let bucketX = null;
    if (labels[0].length === 10 && labels[0].endsWith('-01') &&
        labels.length > 1 && labels[1].endsWith('-01')) {
      bucketX = ax.slice(0, 7) + '-01';
    } else if (labels.includes(ax)) {
      bucketX = ax;
    } else {
      // Maybe week bucket
      const w = truncWeek(ax);
      if (labels.includes(w)) bucketX = w;
    }
    if (bucketX && labels.includes(bucketX)) {
      out.push({ x: bucketX, label: a.label });
    }
  });
  // Group multiple annotations on the same bucket.
  const merged = {};
  out.forEach(a => {
    merged[a.x] = merged[a.x] ? merged[a.x] + ' · ' + a.label : a.label;
  });
  return Object.entries(merged).map(([x, label]) => ({ x, label }));
}

function weekendBands(labels) {
  // Only meaningful for daily charts. Coalesce adjacent Sat-Sun pairs
  // into one (startIdx, endIdx) band so a 2-day weekend shows as one
  // 2-column stripe rather than two 1-column ones.
  if (!labels.length || labels[0].length !== 10) return [];
  const out = [];
  let cur = null;
  labels.forEach((iso, i) => {
    const d = new Date(iso + 'T00:00:00Z');
    const dow = d.getUTCDay();
    const isWeekend = dow === 0 || dow === 6;
    if (isWeekend) {
      if (cur && cur.endIdx === i - 1) cur.endIdx = i;
      else { cur = { startIdx: i, endIdx: i }; out.push(cur); }
    } else {
      cur = null;
    }
  });
  return out;
}

function buildAnnotations(items, weekends) {
  const out = {};
  (weekends || []).forEach((w, i) => {
    out['weekend-' + i] = {
      type: 'box',
      xMin: w.startIdx - 0.5,
      xMax: w.endIdx + 0.5,
      backgroundColor: 'rgba(31, 34, 41, 0.06)',
      borderWidth: 0,
      drawTime: 'beforeDatasetsDraw',
    };
  });
  (items || []).forEach((a, i) => {
    out['anno-' + i] = {
      type: 'line',
      xMin: a.x, xMax: a.x,
      borderColor: 'rgba(229, 61, 61, 0.70)',
      borderWidth: 1.5,
      borderDash: [4, 4],
      label: {
        display: true,
        content: a.label,
        position: 'start',
        backgroundColor: '#E53D3D',
        color: '#FFFFFF',
        borderRadius: 999,
        font: { size: 10, weight: '600' },
        padding: { top: 3, bottom: 3, left: 8, right: 8 },
      },
    };
  });
  return out;
}

// ===== chart factories =====================================================

const FACTORIES = {};

function lineStackedSpec(labels, datasets, opts) {
  opts = opts || {};
  // Line/area charts read as random dots when there's only one
  // bucket on the x-axis. Fall back to a stacked bar in that case
  // so the data still renders meaningfully (e.g. pilot's first
  // month, or the user filtering down to a single day).
  const sparse = labels.length <= 1;
  const ds = datasets.map(d => ({
    label: d.label,
    data: d.data,
    borderColor: d.color,
    backgroundColor: sparse ? d.color : d.color + '80',
    fill: !sparse,
    tension: 0.25,
    borderWidth: sparse ? 0 : 2,
    pointRadius: sparse ? 0 : 2,
    stack: 'stack0',
  }));
  if (opts.overlay) {
    ds.push({
      label: opts.overlay.label,
      data: opts.overlay.data,
      type: 'line',
      borderColor: opts.overlay.color || C.overlay,
      backgroundColor: 'transparent',
      borderWidth: 2,
      borderDash: [6, 4],
      pointRadius: sparse ? 6 : 2,
      fill: false,
      tension: 0.25,
      yAxisID: 'y1',
    });
  }
  const scales = {
    y: {
      stacked: true, beginAtZero: true,
      title: { display: true, text: opts.yTitle || 'requests' },
    },
  };
  if (opts.overlay) {
    scales.y1 = {
      position: 'right',
      grid: { drawOnChartArea: false },
      beginAtZero: true,
      title: { display: true, text: opts.y1Title || '' },
    };
  }
  return {
    type: sparse ? 'bar' : 'line',
    data: { labels, datasets: ds },
    options: {
      ...CHART_DEFAULTS,
      plugins: {
        ...CHART_DEFAULTS.plugins,
        annotation: { annotations: buildAnnotations(opts.annotations, opts.weekends) },
      },
      scales,
    },
  };
}

function lineSpec(labels, datasets, opts) {
  opts = opts || {};
  // Same single-bucket guard as the stacked variant — a non-stacked
  // line with a single point is just a floating dot; switch to bars.
  const sparse = labels.length <= 1;
  const ds = datasets.map(d => ({
    label: d.label,
    data: d.data,
    borderColor: d.color,
    backgroundColor: sparse ? d.color : d.color + '33',
    fill: false,
    tension: 0.25,
    borderWidth: sparse ? 0 : 2,
    pointRadius: sparse ? 6 : 2,
    yAxisID: d.secondAxis ? 'y1' : 'y',
    borderDash: d.dashed ? [6, 4] : undefined,
    type: sparse ? 'bar' : 'line',
  }));
  const scales = {
    y: { beginAtZero: true,
         title: { display: true, text: opts.yTitle || 'value' } },
  };
  const hasSecond = ds.some(d => d.yAxisID === 'y1');
  if (hasSecond) {
    scales.y1 = {
      position: 'right',
      grid: { drawOnChartArea: false },
      beginAtZero: true,
      title: { display: true, text: opts.y1Title || '' },
    };
  }
  return {
    type: sparse ? 'bar' : 'line',
    data: { labels, datasets: ds },
    options: {
      ...CHART_DEFAULTS,
      plugins: {
        ...CHART_DEFAULTS.plugins,
        annotation: { annotations: buildAnnotations(opts.annotations, opts.weekends) },
      },
      scales,
    },
  };
}

function barSpec(labels, datasets, opts) {
  opts = opts || {};
  const ds = datasets.map(d => ({
    label: d.label,
    data: d.data,
    backgroundColor: d.color,
    borderColor: d.color,
    borderWidth: 1,
  }));
  return {
    type: 'bar',
    data: { labels, datasets: ds },
    options: {
      ...CHART_DEFAULTS,
      indexAxis: opts.horizontal ? 'y' : 'x',
      scales: {
        x: { stacked: !!opts.stacked },
        y: { stacked: !!opts.stacked, beginAtZero: true },
      },
    },
  };
}

// ===== status counters =====================================================

const STATUS_COMPLETED = ['completed'];
const STATUS_FAILED    = ['failed'];
const STATUS_REJECTED  = ['rejected'];
const STATUS_CANCELLED = ['cancelled'];

function statusCounts(rows) {
  const out = { completed: 0, failed: 0, rejected: 0, cancelled: 0 };
  rows.forEach(r => {
    if (out[r.status] !== undefined) out[r.status]++;
  });
  return out;
}

// ===== chart factory implementations ======================================

function gapFilledBuckets(rows, bucketFn, rangeFn) {
  // Group rows by bucket key, returning labels (sorted, gap-filled
  // across the range) + a Map from key → rows.
  const map = new Map();
  rows.forEach(r => {
    const k = bucketFn(r.created_at);
    if (!map.has(k)) map.set(k, []);
    map.get(k).push(r);
  });
  // Determine range: from = min(created_at, STATE), to = max(created_at, today).
  const dates = [...map.keys()].sort();
  let labels;
  if (!dates.length) {
    labels = [];
  } else {
    labels = rangeFn(dates[0], dates[dates.length - 1]);
  }
  return { labels, map };
}

function volumeStatusFactory(bucketFn, rangeFn, opts) {
  return function (rows) {
    const { labels, map } = gapFilledBuckets(rows, bucketFn, rangeFn);
    const sc = (label) => labels.map(k => {
      const arr = map.get(k) || [];
      return arr.filter(r => r.status === label).length;
    });
    const active = labels.map(k => {
      const arr = map.get(k) || [];
      return new Set(arr.map(r => r.requester_slack_id)).size;
    });
    return lineStackedSpec(labels, [
      { label: 'completed', data: sc('completed'), color: C.completed },
      { label: 'failed',    data: sc('failed'),    color: C.failed    },
      { label: 'rejected',  data: sc('rejected'),  color: C.rejected  },
      { label: 'cancelled', data: sc('cancelled'), color: C.cancelled },
    ], {
      overlay: { label: opts.overlayLabel, data: active, color: C.overlay },
      yTitle: 'requests',
      y1Title: opts.y1Title,
      annotations: annotationsFor(labels),
      weekends: opts.bucket === 'day' ? weekendBands(labels) : [],
    });
  };
}

FACTORIES.volumeDaily   = volumeStatusFactory(
  truncDay,   daysBetween,   { bucket: 'day',   overlayLabel: 'active users', y1Title: 'users' }
);
FACTORIES.volumeWeekly  = volumeStatusFactory(
  truncWeek,  weeksBetween,  { bucket: 'week',  overlayLabel: 'WAU',          y1Title: 'WAU'   }
);
FACTORIES.volumeMonthly = volumeStatusFactory(
  truncMonth, monthsBetween, { bucket: 'month', overlayLabel: 'MAU',          y1Title: 'MAU'   }
);

FACTORIES.failureBreakdown = function (rows) {
  const { labels, map } = gapFilledBuckets(rows, truncWeek, weeksBetween);
  const counts = (status) => labels.map(k =>
    (map.get(k) || []).filter(r => r.status === status).length);
  const completed = counts('completed');
  const rejected  = counts('rejected');
  const failed    = counts('failed');
  const cancelled = counts('cancelled');
  const successPct = labels.map((_, i) => {
    const total = completed[i] + rejected[i] + failed[i] + cancelled[i];
    if (!total) return 0;
    return Math.round(100 * completed[i] / total);
  });
  return lineStackedSpec(labels, [
    { label: 'completed',       data: completed, color: C.completed },
    { label: 'admin rejected',  data: rejected,  color: C.rejected  },
    { label: 'execute failed',  data: failed,    color: C.failed    },
    { label: 'user cancelled',  data: cancelled, color: C.cancelled },
  ], {
    overlay: { label: 'success %', data: successPct, color: C.overlay },
    yTitle: 'requests',
    y1Title: 'percent',
    annotations: annotationsFor(labels),
  });
};

FACTORIES.tierDistribution = function (rows) {
  const { labels, map } = gapFilledBuckets(rows, truncWeek, weeksBetween);
  const counts = (tier) => labels.map(k =>
    (map.get(k) || []).filter(r => r.tier === tier).length);
  return lineStackedSpec(labels, [
    { label: 'ro',  data: counts('ro'),  color: C.ro  },
    { label: 'rw',  data: counts('rw'),  color: C.rw  },
    { label: 'ddl', data: counts('ddl_or_other'), color: C.ddl_or_other },
  ], {
    yTitle: 'requests',
    annotations: annotationsFor(labels),
  });
};

FACTORIES.scheduledUsage = function (rows) {
  const { labels, map } = gapFilledBuckets(rows, truncWeek, weeksBetween);
  const scheduledPct = labels.map(k => {
    const arr = map.get(k) || [];
    if (!arr.length) return 0;
    const sched = arr.filter(r => r.scheduled_for).length;
    return Math.round(1000 * sched / arr.length) / 10;
  });
  const totals = labels.map(k => (map.get(k) || []).length);
  return lineSpec(labels, [
    { label: 'scheduled %',    data: scheduledPct, color: C.accent  },
    { label: 'total requests', data: totals,       color: C.neutral,
      secondAxis: true, dashed: true },
  ], {
    yTitle: 'percent',
    y1Title: 'requests',
    annotations: annotationsFor(labels),
  });
};

FACTORIES.approvalSla = function (rows) {
  const { labels, map } = gapFilledBuckets(rows, truncWeek, weeksBetween);
  const p50raw = labels.map(k => {
    const vals = (map.get(k) || []).map(r => r.approval_sec).filter(v => v != null);
    return vals.length ? pct(vals, 0.5) : null;
  });
  const p90raw = labels.map(k => {
    const vals = (map.get(k) || []).map(r => r.approval_sec).filter(v => v != null);
    return vals.length ? pct(vals, 0.9) : null;
  });
  const p95raw = labels.map(k => {
    const vals = (map.get(k) || []).map(r => r.approval_sec).filter(v => v != null);
    return vals.length ? pct(vals, 0.95) : null;
  });
  const u = chooseTimeUnit([p50raw, p90raw, p95raw]);
  // Mirror the unit into the section title.
  const titleEl = document.querySelector('#sec-approval-sla h2');
  if (titleEl) titleEl.textContent = 'Approval latency percentiles (' + u.unit + ')';
  return lineSpec(labels, [
    { label: 'p50', data: scaleTime(p50raw, u.div, u.decimals), color: C.completed },
    { label: 'p90', data: scaleTime(p90raw, u.div, u.decimals), color: C.rejected  },
    { label: 'p95', data: scaleTime(p95raw, u.div, u.decimals), color: C.failed    },
  ], { yTitle: u.unit, annotations: annotationsFor(labels) });
};

FACTORIES.businessOffhours = function (rows) {
  const { labels, map } = gapFilledBuckets(rows, truncWeek, weeksBetween);
  const cls = (filter) => labels.map(k => (map.get(k) || []).filter(filter).length);
  return lineStackedSpec(labels, [
    { label: 'business hours',  data: cls(r => r.dow_local >= 1 && r.dow_local <= 5
                                          && r.hour_local >= 9 && r.hour_local <= 17),
      color: C.completed },
    { label: 'weekday evening', data: cls(r => r.dow_local >= 1 && r.dow_local <= 5
                                          && r.hour_local >= 18),
      color: C.rejected  },
    { label: 'weekday early',   data: cls(r => r.dow_local >= 1 && r.dow_local <= 5
                                          && r.hour_local <  9),
      color: C.accent    },
    { label: 'weekend',         data: cls(r => r.dow_local === 0 || r.dow_local === 6),
      color: C.failed    },
  ], { yTitle: 'requests', annotations: annotationsFor(labels) });
};

FACTORIES.teamUsage = function (rows) {
  const byTeam = {};
  rows.forEach(r => {
    const k = r.team || '(unteamed)';
    if (!byTeam[k]) byTeam[k] = { completed: 0, failed: 0, rejected: 0 };
    if (r.status === 'completed') byTeam[k].completed++;
    else if (r.status === 'failed') byTeam[k].failed++;
    else if (r.status === 'rejected') byTeam[k].rejected++;
  });
  const labels = Object.keys(byTeam).sort((a, b) =>
    (byTeam[b].completed + byTeam[b].failed + byTeam[b].rejected)
    - (byTeam[a].completed + byTeam[a].failed + byTeam[a].rejected));
  return barSpec(labels, [
    { label: 'completed', data: labels.map(k => byTeam[k].completed), color: C.completed },
    { label: 'failed',    data: labels.map(k => byTeam[k].failed),    color: C.failed    },
    { label: 'rejected',  data: labels.map(k => byTeam[k].rejected),  color: C.rejected  },
  ], { stacked: true });
};

FACTORIES.topUsers = function (rows) {
  const byUser = {};
  const lastWeekCutoff = Date.now() - 7 * 86400000;
  rows.forEach(r => {
    const k = r.requester_slack_id;
    if (!byUser[k]) byUser[k] = {
      name: r.requester_name || k, total: 0, recent: 0,
    };
    byUser[k].total++;
    if (new Date(r.created_at).getTime() >= lastWeekCutoff) byUser[k].recent++;
  });
  const sorted = Object.values(byUser)
    .sort((a, b) => b.total - a.total)
    .slice(0, 10);
  return barSpec(
    sorted.map(u => u.name),
    [
      { label: 'total',   data: sorted.map(u => u.total),  color: C.overlay },
      { label: 'last 7d', data: sorted.map(u => u.recent), color: C.accent  },
    ],
    { horizontal: true }
  );
};

FACTORIES.adminWorkload = function (rows) {
  // Decided rows only; group by decided_by_slack_id.
  const byAdmin = {};
  rows.filter(r => r.decided_by_slack_id).forEach(r => {
    const k = r.decided_by_slack_id;
    if (!byAdmin[k]) byAdmin[k] = {
      name: r.decided_by_name || k, approved: 0, rejected: 0, changes: 0,
    };
    if (r.status === 'rejected') byAdmin[k].rejected++;
    else if (r.status === 'changes_requested') byAdmin[k].changes++;
    else byAdmin[k].approved++;
  });
  const sorted = Object.values(byAdmin)
    .sort((a, b) => (b.approved + b.rejected + b.changes)
                  - (a.approved + a.rejected + a.changes));
  return barSpec(
    sorted.map(a => a.name),
    [
      { label: 'approved',          data: sorted.map(a => a.approved), color: C.completed },
      { label: 'rejected',          data: sorted.map(a => a.rejected), color: C.failed    },
      { label: 'changes requested', data: sorted.map(a => a.changes),  color: C.rejected  },
    ],
    { horizontal: true, stacked: true }
  );
};

FACTORIES.targetHeatmap = function (rows) {
  // Render outside Chart.js. Build a sorted table grouped by target.
  const byTarget = {};
  rows.forEach(r => {
    const k = r.target_alias || '?';
    if (!byTarget[k]) byTarget[k] = {
      alias: k, total: 0, completed: 0, failed: 0, last_used: null,
    };
    byTarget[k].total++;
    if (r.status === 'completed') byTarget[k].completed++;
    if (r.status === 'failed')    byTarget[k].failed++;
    if (!byTarget[k].last_used || r.created_at > byTarget[k].last_used) {
      byTarget[k].last_used = r.created_at;
    }
  });
  const ranked = Object.values(byTarget).sort((a, b) => b.total - a.total);
  const max = ranked.reduce((m, r) => Math.max(m, r.total), 0) || 1;
  const container = document.getElementById('canvas-target-heatmap');
  container.innerHTML = '';
  if (!ranked.length) {
    container.innerHTML = '<div class="empty">No requests match the current filters.</div>';
    return null;
  }
  const tbl = document.createElement('table');
  tbl.className = 'data';
  tbl.innerHTML = '<thead><tr><th>Target</th><th>Total</th>'
                + '<th>Completed</th><th>Failed</th><th>Heat</th>'
                + '<th>Last used</th></tr></thead>';
  const body = document.createElement('tbody');
  ranked.forEach(t => {
    const pct = Math.round(100 * t.total / max);
    const tr = document.createElement('tr');
    tr.innerHTML = '<td>' + t.alias + '</td>'
      + '<td>' + t.total + '</td>'
      + '<td>' + t.completed + '</td>'
      + '<td>' + t.failed + '</td>'
      + '<td style="min-width:120px"><div style="background: '
      + 'linear-gradient(to right, var(--brand-solid-light) ' + pct + '%, transparent ' + pct + '%);'
      + 'height: 14px; border-radius: 4px"></div></td>'
      + '<td>' + (t.last_used ? t.last_used.slice(0, 10) : '—') + '</td>';
    body.appendChild(tr);
  });
  tbl.appendChild(body);
  container.appendChild(tbl);
  return null;  // signal "no Chart.js instance"
};

FACTORIES.peakHours = function (rows) {
  // Day-of-week × hour heatmap. dow_local: 0 = Sun, 1 = Mon … 6 = Sat.
  // Reorder to Mon-first for the display.
  const dayLabels = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const cells = {};
  rows.forEach(r => {
    const dow = r.dow_local;
    const h   = r.hour_local;
    const key = dow + '|' + h;
    cells[key] = (cells[key] || 0) + 1;
  });
  // dow 0 (Sun) should be last; 1..6 → 0..5 in display order; 0 → 6.
  function displayIdx(dow) { return dow === 0 ? 6 : dow - 1; }
  const max = Math.max(0, ...Object.values(cells));
  const container = document.getElementById('canvas-peak-hours');
  container.innerHTML = '';
  if (!Object.keys(cells).length) {
    container.innerHTML = '<div class="empty">No requests match the current filters.</div>';
    return null;
  }
  const grid = document.createElement('div');
  grid.className = 'heatmap';
  grid.style.gridTemplateColumns = '40px repeat(24, 1fr)';
  grid.appendChild(document.createElement('div'));  // top-left blank
  for (let h = 0; h < 24; h++) {
    const cell = document.createElement('div');
    cell.className = 'heatmap__row-label';
    cell.textContent = h;
    grid.appendChild(cell);
  }
  for (let dIdx = 0; dIdx < 7; dIdx++) {
    const labelCell = document.createElement('div');
    labelCell.className = 'heatmap__row-label';
    labelCell.textContent = dayLabels[dIdx];
    grid.appendChild(labelCell);
    // Reverse map dIdx → dow
    const dow = dIdx === 6 ? 0 : dIdx + 1;
    for (let h = 0; h < 24; h++) {
      const v = cells[dow + '|' + h] || 0;
      const intensity = max ? v / max : 0;
      const cell = document.createElement('div');
      cell.className = 'heatmap__cell';
      cell.style.background = v
        ? 'rgba(37, 99, 235, ' + (0.08 + intensity * 0.72) + ')'
        : 'var(--bg-regular)';
      cell.textContent = v || '';
      cell.title = dayLabels[dIdx] + ' ' + h + ':00 · ' + v + ' request' + (v === 1 ? '' : 's');
      grid.appendChild(cell);
    }
  }
  container.appendChild(grid);
  return null;
};

FACTORIES.ratingWeekly = function (rows) {
  const { labels, map } = gapFilledBuckets(rows, truncWeek, weeksBetween);
  const avg = labels.map(k => {
    const vals = (map.get(k) || []).map(r => r.rating).filter(v => v != null);
    if (!vals.length) return null;
    return +(vals.reduce((s, v) => s + v, 0) / vals.length).toFixed(2);
  });
  const counts = labels.map(k =>
    (map.get(k) || []).filter(r => r.rating != null).length);
  return lineSpec(labels, [
    { label: 'avg rating', data: avg.map(v => v == null ? 0 : v),    color: C.overlay },
    { label: 'n ratings',  data: counts, color: C.neutral, secondAxis: true, dashed: true },
  ], { yTitle: 'avg (1-5)', y1Title: 'count', annotations: annotationsFor(labels) });
};

FACTORIES.ratingResponse = function (rows) {
  const { labels, map } = gapFilledBuckets(rows, truncWeek, weeksBetween);
  const pct = labels.map(k => {
    const arr = (map.get(k) || []).filter(r => r.status === 'completed');
    if (!arr.length) return 0;
    const responded = arr.filter(r => r.rating != null).length;
    return Math.round(1000 * responded / arr.length) / 10;
  });
  return lineSpec(labels, [
    { label: 'response %', data: pct, color: C.completed },
  ], { yTitle: 'percent', annotations: annotationsFor(labels) });
};

FACTORIES.ratingLow = function (rows) {
  // Table of low ratings; render outside Chart.js. The data lives in
  // DATA.rating_low (joined feedback text); we re-filter it by the
  // active request-id set.
  const allowedIds = new Set(rows.map(r => r.id));
  const filtered = DATA.rating_low.filter(r => allowedIds.has(r.request_id));
  const container = document.getElementById('canvas-rating-low');
  container.innerHTML = '';
  if (!filtered.length) {
    container.innerHTML = '<div class="empty">No low ratings in the current filter window.</div>';
    return null;
  }
  const tbl = document.createElement('table');
  tbl.className = 'data';
  tbl.innerHTML = '<thead><tr><th>Rated at</th><th>Rating</th>'
                + '<th>Feedback</th><th>Request</th><th>Requester</th>'
                + '<th>Status</th><th>Query</th></tr></thead>';
  const body = document.createElement('tbody');
  filtered.forEach(r => {
    const tr = document.createElement('tr');
    const ratedAt = r.rated_at ? r.rated_at.slice(0, 19).replace('T', ' ') : '';
    tr.innerHTML = '<td>' + ratedAt + '</td>'
      + '<td>' + (r.rating || '') + '</td>'
      + '<td>' + escapeHtml(r.feedback_text || '') + '</td>'
      + '<td>#' + r.request_id + '</td>'
      + '<td>' + escapeHtml(r.requester_name || r.requester_slack_id) + '</td>'
      + '<td>' + r.status + '</td>'
      + '<td><code>' + escapeHtml((r.query_preview || '').slice(0, 100)) + '</code></td>';
    body.appendChild(tr);
  });
  tbl.appendChild(body);
  container.appendChild(tbl);
  return null;
};

FACTORIES.whoCanWhat = function (rows) {
  // Static org structure — not filtered by request-side filters.
  const container = document.getElementById('canvas-who-can-what');
  container.innerHTML = '';
  if (!DATA.who_can_what.length) {
    container.innerHTML = '<div class="empty">No users registered.</div>';
    return null;
  }
  const tbl = document.createElement('table');
  tbl.className = 'data';
  tbl.innerHTML = '<thead><tr><th>Name</th><th>Slack ID</th><th>Admin</th>'
                + '<th>Max tier</th><th>Bypass</th><th>Teams</th>'
                + '<th>User grants</th></tr></thead>';
  const body = document.createElement('tbody');
  DATA.who_can_what.forEach(r => {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td>' + escapeHtml(r.name || '(?)') + '</td>'
      + '<td>' + r.slack_user_id + '</td>'
      + '<td>' + (r.is_admin ? 'yes' : '') + '</td>'
      + '<td>' + (r.admin_max_tier || '') + '</td>'
      + '<td>' + (r.is_bypass ? 'yes' : '') + '</td>'
      + '<td>' + ((r.teams || []).join(', ')) + '</td>'
      + '<td>' + ((r.user_grants || []).join(', ')) + '</td>';
    body.appendChild(tr);
  });
  tbl.appendChild(body);
  container.appendChild(tbl);
  return null;
};

FACTORIES.csvImports = function (rows) {
  // CSV bulk imports (/sql import). Its own dataset (DATA.csv_imports),
  // independent of the request-side filters — imports aren't requests.
  // A summary line + a recent-imports table, rendered outside Chart.js.
  const imports = DATA.csv_imports || [];
  const container = document.getElementById('canvas-csv-imports');
  container.innerHTML = '';
  if (!imports.length) {
    container.innerHTML = '<div class="empty">No CSV imports yet.</div>';
    return null;
  }

  function fmtBytes(n) {
    if (n == null) return '—';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let s = Number(n), i = 0;
    while (s >= 1024 && i < u.length - 1) { s /= 1024; i++; }
    return (i === 0 ? s : s.toFixed(s >= 10 ? 0 : 1)) + ' ' + u[i];
  }
  // Same markup as renderKPIs() so the summary cards match the headline KPIs.
  function kpiCard(label, value) {
    return '<div class="kpi"><div class="kpi__label">' + escapeHtml(label)
         + '</div><div class="kpi__value">' + escapeHtml(String(value)) + '</div></div>';
  }

  const completed = imports.filter(r => r.status === 'completed');
  const failed    = imports.filter(r => r.status === 'failed' || r.status === 'rejected');
  const rowsLoaded = completed.reduce((s, r) => s + (r.inserted_rows || 0), 0);
  const decided    = completed.length + failed.length;
  const successPct = decided ? Math.round(100 * completed.length / decided) : null;

  const sum = document.createElement('div');
  sum.className = 'kpi-grid';
  sum.innerHTML =
      kpiCard('Imports', imports.length)
    + kpiCard('Completed', completed.length)
    + kpiCard('Failed / rejected', failed.length)
    + kpiCard('Rows loaded', rowsLoaded.toLocaleString())
    + kpiCard('Success rate', successPct == null ? '—' : successPct + '%');
  container.appendChild(sum);

  const tbl = document.createElement('table');
  tbl.className = 'data';
  tbl.innerHTML = '<thead><tr><th>When</th><th>Import</th><th>Requester</th>'
                + '<th>Target / DB</th><th>Table</th><th>New?</th>'
                + '<th>Status</th><th>Rows</th><th>Size</th><th>Load</th></tr></thead>';
  const body = document.createElement('tbody');
  // Most recent first.
  imports.slice().reverse().forEach(r => {
    const tr = document.createElement('tr');
    const when = r.created_at ? r.created_at.slice(0, 19).replace('T', ' ') : '';
    const load = r.load_seconds == null ? '—'
               : (Number(r.load_seconds) < 1
                    ? (Number(r.load_seconds) * 1000).toFixed(0) + 'ms'
                    : Number(r.load_seconds).toFixed(1) + 's');
    const rowsCell = r.inserted_rows != null ? Number(r.inserted_rows).toLocaleString()
                   : (r.row_count != null ? Number(r.row_count).toLocaleString() : '—');
    tr.innerHTML = '<td>' + when + '</td>'
      + '<td>#' + r.id + '</td>'
      + '<td>' + escapeHtml(r.requester_name || r.requester_slack_id) + '</td>'
      + '<td>' + escapeHtml((r.target_alias || '?') + ' / ' + (r.database_name || '')) + '</td>'
      + '<td><code>dba.' + escapeHtml(r.table_name || '') + '</code></td>'
      + '<td>' + (r.is_new_table ? 'new' : 'append') + '</td>'
      + '<td>' + escapeHtml(r.status) + '</td>'
      + '<td>' + rowsCell + '</td>'
      + '<td>' + fmtBytes(r.byte_size) + '</td>'
      + '<td>' + load + '</td>';
    body.appendChild(tr);
  });
  tbl.appendChild(body);
  container.appendChild(tbl);
  return null;
};

FACTORIES.kpi = function (rows) {
  const total = rows.length;
  const sc = statusCounts(rows);
  const decided = rows.filter(r => r.approval_sec != null).map(r => r.approval_sec);
  const p50 = decided.length ? pct(decided, 0.5) : null;
  const p95 = decided.length ? pct(decided, 0.95) : null;
  const ratings = rows.map(r => r.rating).filter(v => v != null);
  const avgRating = ratings.length
    ? (ratings.reduce((s, v) => s + v, 0) / ratings.length).toFixed(2)
    : '—';
  const uniqUsers   = new Set(rows.map(r => r.requester_slack_id)).size;
  const uniqTargets = new Set(rows.map(r => r.target_alias).filter(Boolean)).size;

  function fmtTime(sec) {
    if (sec == null) return '—';
    if (sec < 120) return sec.toFixed(0) + 's';
    if (sec < 36000) return (sec / 60).toFixed(1) + 'm';
    return (sec / 3600).toFixed(2) + 'h';
  }

  renderKPIs('canvas-kpi-headline', [
    { label: 'Total requests',  value: total },
    { label: 'Completed',       value: sc.completed, hint: total
        ? (Math.round(1000 * sc.completed / total) / 10) + '% of total' : '' },
    { label: 'Failed',          value: sc.failed },
    { label: 'Rejected',        value: sc.rejected },
    { label: 'Cancelled',       value: sc.cancelled },
    { label: 'Unique users',    value: uniqUsers },
    { label: 'Targets touched', value: uniqTargets },
    { label: 'p50 approval',    value: fmtTime(p50) },
    { label: 'p95 approval',    value: fmtTime(p95) },
    { label: 'Avg rating',      value: avgRating,
      hint: ratings.length + ' rating' + (ratings.length === 1 ? '' : 's') },
  ]);
  return null;
};

FACTORIES.kpiCostSavings = function (rows) {
  const cfg = DATA.config || {};
  // `parseFloat(v || 0)` was not enough: `|| 0` only catches falsy values, so a
  // mis-typed coefficient like "12 min" is truthy, parseFloat returns NaN, and
  // every card below rendered "NaN" to the operator. Python's
  // metrics_defs.coerce_float returns 0 for the same input, so the two
  // dashboards disagreed — caught by tests/test_metrics_parity.py.
  const num = (v) => { const n = parseFloat(v); return isFinite(n) ? n : 0; };
  const dbaMin   = num(cfg.cost_dba_minutes_per_request);
  const dbaHr    = num(cfg.cost_dba_hourly_usd);
  const replicas = num(cfg.cost_avoided_replicas);
  const perRep   = num(cfg.cost_per_replica_monthly_usd);
  const other    = num(cfg.cost_other_monthly_usd);

  const completed = rows.filter(r => r.status === 'completed').length;
  const dbaHoursSaved = completed * dbaMin / 60;
  const dbaSavingUSD  = dbaHoursSaved * dbaHr;
  const monthlyInfra  = replicas * perRep + other;

  renderKPIs('canvas-cost-savings', [
    { label: 'Completed (in filter)', value: completed },
    { label: 'DBA hours saved',       value: dbaHoursSaved.toFixed(1),
      hint: dbaMin + ' min × $' + dbaHr + '/hr' },
    { label: 'DBA $ saved',           value: '$' + dbaSavingUSD.toFixed(0) },
    { label: 'Avoided replicas',      value: replicas },
    { label: 'Infra $ avoided / mo',  value: '$' + monthlyInfra.toFixed(0),
      hint: 'replicas + other' },
  ]);
  return null;
};

function renderKPIs(canvasId, cards) {
  const container = document.getElementById(canvasId);
  container.innerHTML = '';
  const grid = document.createElement('div');
  grid.className = 'kpi-grid';
  cards.forEach(c => {
    const card = document.createElement('div');
    card.className = 'kpi';
    card.innerHTML = '<div class="kpi__label">' + escapeHtml(c.label) + '</div>'
      + '<div class="kpi__value">' + escapeHtml(String(c.value)) + '</div>'
      + (c.hint ? '<div class="kpi__hint">' + escapeHtml(c.hint) + '</div>' : '');
    grid.appendChild(card);
  });
  container.appendChild(grid);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

// ===== render orchestration ================================================

function destroyAllCharts() {
  Object.values(CHARTS).forEach(c => { if (c && typeof c.destroy === 'function') c.destroy(); });
  Object.keys(CHARTS).forEach(k => delete CHARTS[k]);
}

function annotationsForChartCard(chartId) {
  // Surface annotations as pills underneath the chart card. Always
  // shows the full set (not filtered) so the operator can see where
  // milestones fall on the time axis even after a date filter.
  return DATA.annotations.map(a =>
    '<span class="anno-pill">' + a.x + ': ' + escapeHtml(a.label) + '</span>'
  ).join('');
}

function render() {
  const filtered = applyFilters();

  // Filter summary line
  const sum = document.getElementById('filter-summary');
  const range = STATE.preset === 'custom'
    ? (STATE.from || '…') + ' → ' + (STATE.to || '…')
    : STATE.preset === 'all' ? 'All time' : presetLabel(STATE.preset);
  sum.innerHTML = '<strong>' + filtered.length + '</strong> request'
    + (filtered.length === 1 ? '' : 's')
    + ' · ' + escapeHtml(range);

  destroyAllCharts();
  CHART_SPECS_DOM.forEach(spec => {
    const factory = FACTORIES[spec.factory];
    if (!factory) return;
    const wrap = document.getElementById('canvas-' + spec.id);
    // HTML-emit factories (KPI, table, heatmap) clear the wrap and
    // inject their own DOM; they want auto height. Chart.js factories
    // need a fresh canvas at the fixed height.
    const wantsChartJs = !['kpi', 'kpiCostSavings', 'targetHeatmap',
                            'peakHours', 'ratingLow', 'whoCanWhat', 'csvImports']
                         .includes(spec.factory);
    if (wantsChartJs) {
      wrap.classList.remove('chart-wrap--auto');
      wrap.innerHTML = '<canvas></canvas>';
    } else {
      wrap.classList.add('chart-wrap--auto');
      wrap.innerHTML = '';   // factory will populate
    }
    const chartCfg = factory(filtered);
    if (chartCfg) {
      const canvas = wrap.querySelector('canvas');
      CHARTS[spec.id] = new Chart(canvas, chartCfg);
    }
  });
}

function presetLabel(p) {
  if (p === 'today') return 'Today';
  return 'Last ' + p + 'd';
}

// ===== filter UI wiring ====================================================

function populateSelect(id, values, placeholder) {
  const el = document.getElementById(id);
  el.innerHTML = '';
  const opt0 = document.createElement('option');
  opt0.value = '';
  opt0.textContent = placeholder;
  el.appendChild(opt0);
  values.forEach(v => {
    const o = document.createElement('option');
    if (typeof v === 'string') { o.value = v; o.textContent = v; }
    else { o.value = v.value; o.textContent = v.label; }
    el.appendChild(o);
  });
}

function setupFilters() {
  // Preset buttons
  document.querySelectorAll('.filter-btn[data-preset]').forEach(btn => {
    btn.addEventListener('click', () => {
      STATE.preset = btn.dataset.preset;
      // Clear the custom date inputs to avoid stale display.
      document.getElementById('filter-from').value = '';
      document.getElementById('filter-to').value = '';
      STATE.from = STATE.to = null;
      activatePreset(btn.dataset.preset);
      render();
    });
  });

  // Custom date inputs
  ['filter-from', 'filter-to'].forEach(id => {
    document.getElementById(id).addEventListener('change', () => {
      STATE.from = document.getElementById('filter-from').value || null;
      STATE.to   = document.getElementById('filter-to').value   || null;
      if (STATE.from || STATE.to) STATE.preset = 'custom';
      activatePreset('custom');
      render();
    });
  });

  // Reset
  document.getElementById('filter-reset').addEventListener('click', () => {
    STATE.preset = 'all'; STATE.from = STATE.to = null;
    STATE.team = STATE.user = STATE.target = STATE.db = '';
    STATE.tier = STATE.status = '';
    document.getElementById('filter-from').value = '';
    document.getElementById('filter-to').value = '';
    ['filter-team','filter-user','filter-target','filter-db',
     'filter-tier','filter-status'].forEach(id => {
      document.getElementById(id).value = '';
    });
    activatePreset('all');
    render();
  });

  // Dropdowns
  const dims = ['team', 'user', 'target', 'db', 'tier', 'status'];
  dims.forEach(d => {
    document.getElementById('filter-' + d).addEventListener('change', e => {
      STATE[d] = e.target.value;
      render();
    });
  });

  // Populate dropdown values from lookups
  populateSelect('filter-team',   DATA.teams,   'All teams');
  populateSelect('filter-user',
    DATA.users.map(u => ({ value: u.id, label: u.name })),
    'All users');
  populateSelect('filter-target', DATA.targets, 'All targets');
  populateSelect('filter-db',     DATA.databases, 'All databases');
  populateSelect('filter-tier',   ['ro', 'rw', 'ddl_or_other'], 'All tiers');
  populateSelect('filter-status', [
    'pending', 'approved', 'scheduled', 'executing',
    'completed', 'failed', 'rejected', 'cancelled',
    'awaiting_dba_manual', 'changes_requested'
  ], 'All statuses');
}

function activatePreset(preset) {
  document.querySelectorAll('.filter-btn[data-preset]').forEach(b => {
    b.classList.toggle('active', b.dataset.preset === preset);
  });
}

// ===== boot ================================================================

setupFilters();
render();
"""


def render(payload: dict) -> str:
    # Build TOC + section shells (canvas placeholders only; data is
    # injected via JS at runtime).
    toc = []
    sections = []
    chart_specs_js = []

    for spec_id, title, factory in CHART_SPECS:
        toc.append(f"<a href='#sec-{spec_id}'>{title}</a>")
        sections.append(
            f"<div class='card' id='sec-{spec_id}'>\n"
            f"  <h2>{title}</h2>\n"
            f"  <div class='chart-wrap' id='canvas-{spec_id}'>\n"
            f"    <canvas></canvas>\n"
            f"  </div>\n"
            f"</div>"
        )
        chart_specs_js.append({"id": spec_id, "factory": factory})

    chart_specs_js_str = (
        "const CHART_SPECS_DOM = "
        + json.dumps(chart_specs_js, separators=(",", ":"))
        + ";\n"
    )

    html = HTML
    html = html.replace("%GENERATED_AT%", payload["generated_at"])
    html = html.replace("%TOC%", "\n".join(toc))
    html = html.replace("%SECTIONS%", "\n".join(sections))
    html = html.replace(
        "%DATA%",
        json.dumps(payload, default=_json_default, separators=(",", ":")),
    )
    html = html.replace("%RENDERER_JS%", chart_specs_js_str + RENDERER_JS)
    return html


# ----------------------------- entrypoint ----------------------------------


def main() -> int:
    payload = fetch_payload()
    html = render(payload)
    OUT_HTML.write_text(html, encoding="utf-8")
    print(f"wrote {OUT_HTML} "
          f"({len(html.encode('utf-8'))} bytes, "
          f"{len(payload['rows'])} requests, "
          f"{len(CHART_SPECS)} charts)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
