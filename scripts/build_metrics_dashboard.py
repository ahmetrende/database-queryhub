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

import base64
import re
import sys
import json
from html import escape
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal

# Make `from queryhub import ...` work when invoked from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from queryhub import db, metrics_defs  # noqa: E402


REPO_DIR = Path(__file__).resolve().parent.parent
OUT_HTML = REPO_DIR / "metrics_dashboard.html"

# The admin panel's typeface. Its @font-face rules are read from the web app's
# own stylesheet and inlined as base64, so the published page needs no font
# files next to it and sets type exactly as the panel does.
FONT_CSS = REPO_DIR / "QueryHubWeb" / "fonts.css"
FONT_FAMILY = "Manrope"


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


_FONT_FACE = re.compile(r"@font-face\s*\{[^}]*\}")
_FONT_URL = re.compile(r"""url\(\s*['"]?([^'")]+?)['"]?\s*\)""")
_FONT_MIME = {".woff2": "font/woff2", ".woff": "font/woff",
              ".ttf": "font/ttf", ".otf": "font/otf"}


def embedded_font_faces(css_path: Path = FONT_CSS,
                        family: str = FONT_FAMILY) -> str:
    """`family`'s @font-face rules from `css_path`, each url() inlined.

    A face whose file cannot be read is left out rather than published with a
    relative url that would 404 on S3. With no faces at all the page still
    renders, in the system fonts that follow the family name in every stack.
    """
    try:
        css = css_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    wanted = re.compile(r"font-family:\s*['\"]?" + re.escape(family) + r"['\";]")
    faces = []
    for block in _FONT_FACE.findall(css):
        if not wanted.search(block):
            continue
        unreadable = []

        def inline(m: re.Match) -> str:
            path = css_path.parent / m.group(1)
            try:
                data = base64.b64encode(path.read_bytes()).decode("ascii")
            except OSError:
                unreadable.append(path)
                return m.group(0)
            mime = _FONT_MIME.get(path.suffix.lower(), "application/octet-stream")
            return f"url(data:{mime};base64,{data})"

        block = _FONT_URL.sub(inline, block)
        if not unreadable:
            faces.append(block)
    return "\n".join(faces)


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
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QueryHub — product metrics dashboard</title>
<link rel="icon" type="image/svg+xml" href="queryhub-mark.svg">
<script>
  // Inside the site's tabbed shell the shell's bar is the page chrome, so
  // this page's own bar steps aside (.is-framed below).
  if (window.self !== window.top) document.documentElement.classList.add('is-framed');
</script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3.0.1/dist/chartjs-plugin-annotation.min.js"></script>
<style>
%FONT_FACES%

  /* The admin panel's Metrics view, copied rather than linked because this
   * page is one self-contained file. Class names are the panel's own
   * (QueryHubWeb/QueryHub.html), so a later design change carries across by
   * name. Token values are the design system's, with the web app's contrast
   * fix for --fg-tertiary. Brand tints are 8-digit hex, not rgba(), so the
   * open-source export's colour swap reaches them too. */
  :root {
    color-scheme: light dark;

    --brand-green:           #C4603F;
    --brand-green-strong:    #B0512F;
    --brand-adaptive-light:  #C4603F1A;
    --brand-adaptive-medium: #C4603F33;

    --fg-primary:   #1F2229;
    --fg-secondary: rgba(31, 34, 41, 0.80);
    --fg-tertiary:  rgba(31, 34, 41, 0.66);
    --fg-accent:    #A24628;
    --fg-danger:    #E53D3D;

    --bg-white:   #FFFFFF;
    --bg-light:   #F9FAFB;

    --stroke-light:  rgba(31, 34, 41, 0.04);
    --stroke-medium: rgba(31, 34, 41, 0.08);
    --stroke-strong: rgba(31, 34, 41, 0.15);

    --adaptive-light:  rgba(31, 34, 41, 0.02);
    --adaptive-medium: rgba(31, 34, 41, 0.04);
    --adaptive-strong: rgba(31, 34, 41, 0.08);

    --danger-adaptive-light: rgba(229, 61, 61, 0.10);
    --tier-rw-fg: #15688C;
    --tier-rw-bg: #E1F2FA;

    --r-2xs: 4px;
    --r-sm:  8px;
    --r-md:  12px;
    --r-lg:  16px;

    --qh-mono: ui-monospace, 'SF Mono', 'JetBrains Mono', Menlo, Consolas, monospace;
  }

  @media (prefers-color-scheme: dark) {
    :root {
      --fg-primary:   #FFFFFF;
      --fg-secondary: rgba(255, 255, 255, 0.80);
      --fg-tertiary:  rgba(255, 255, 255, 0.50);
      --fg-accent:    #C4603F;

      --bg-white:   #181A20;
      --bg-light:   #1F2229;

      --stroke-light:  rgba(255, 255, 255, 0.04);
      --stroke-medium: rgba(255, 255, 255, 0.08);
      --stroke-strong: rgba(255, 255, 255, 0.15);

      --adaptive-light:  rgba(255, 255, 255, 0.02);
      --adaptive-medium: rgba(255, 255, 255, 0.04);
      --adaptive-strong: rgba(255, 255, 255, 0.08);

      --tier-rw-fg: #7FD2F5;
      --tier-rw-bg: rgba(102, 204, 255, 0.12);
    }
  }

  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: 'Manrope', -apple-system, "Segoe UI", Roboto,
                 system-ui, sans-serif;
    font-size: 14px;
    line-height: 1.45;
    background: var(--bg-white);
    color: var(--fg-primary);
    -webkit-font-smoothing: antialiased;
  }
  a { color: var(--fg-accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  code { font-family: var(--qh-mono); font-size: 12px; }

  /* Top chrome — only when the page is opened on its own. */
  .qh-top {
    height: 52px; display: flex; align-items: center; padding: 0 18px;
    background: var(--bg-white); border-bottom: 1px solid var(--stroke-medium);
  }
  .is-framed .qh-top { display: none; }
  .qh-brand { display: flex; align-items: center; gap: 10px; }
  .qh-applogo { width: 26px; height: 26px; border-radius: var(--r-sm); display: block; }
  .qh-brand-name { font-size: 15px; font-weight: 600; }
  .qh-brand-tag {
    font-size: 10px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase;
    color: var(--fg-accent); background: var(--brand-adaptive-light);
    padding: 2px 6px; border-radius: var(--r-2xs);
  }

  /* View */
  .qh-apad { padding: 24px max(28px, calc((100% - 1600px) / 2)) 48px; }
  .qh-aview-head {
    display: flex; align-items: flex-start; justify-content: space-between;
    gap: 16px; margin-bottom: 18px; flex-wrap: wrap;
  }
  .qh-aview-title { font-size: 19px; font-weight: 600; letter-spacing: -0.2px; }
  .qh-aview-sub { font-size: 13px; color: var(--fg-tertiary); margin-top: 3px; }
  .qh-updated {
    display: inline-flex; align-items: center; gap: 6px; padding-top: 4px;
    font-size: 12px; font-weight: 500; color: var(--fg-tertiary); white-space: nowrap;
  }
  .qh-dot {
    width: 7px; height: 7px; border-radius: 999px; background: var(--brand-green);
    box-shadow: 0 0 0 3px var(--brand-adaptive-light);
  }

  /* Filters: presets + date range on top, dimensions underneath. */
  .qh-filterbar {
    display: flex; flex-direction: column; gap: 10px; padding: 12px;
    background: var(--bg-light); border: 1px solid var(--stroke-medium);
    border-radius: var(--r-md); margin-bottom: 12px;
  }
  .qh-frow { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
  .qh-seg { display: flex; flex-wrap: wrap; gap: 6px; }
  .qh-seg-opt {
    display: inline-flex; align-items: center; height: 34px; padding: 0 12px;
    background: var(--bg-white); border: 1px solid var(--stroke-medium);
    border-radius: var(--r-sm); cursor: pointer; font-family: inherit;
    font-size: 12.5px; font-weight: 500; color: var(--fg-secondary);
  }
  .qh-seg-opt:hover { background: var(--adaptive-medium); }
  .qh-seg-opt.is-active {
    border-color: var(--brand-green); background: var(--brand-adaptive-light);
    color: var(--fg-accent); font-weight: 600;
  }
  .qh-select, .qh-input {
    height: 34px; padding: 0 10px; background: var(--bg-white);
    border: 1px solid var(--stroke-medium); border-radius: var(--r-sm);
    font-family: inherit; font-size: 13px; color: var(--fg-primary);
  }
  .qh-select { cursor: pointer; max-width: 240px; }
  .qh-input-date { width: 150px; color-scheme: light dark; }
  .qh-select:focus, .qh-input:focus, .qh-seg-opt:focus-visible, .qh-btn:focus-visible {
    outline: none; border-color: var(--brand-green);
    box-shadow: 0 0 0 2px var(--brand-adaptive-medium);
  }
  .qh-btn {
    display: inline-flex; align-items: center; height: 34px; padding: 0 14px;
    border-radius: 999px; background: transparent; border: 1px solid var(--stroke-strong);
    font-family: inherit; font-size: 12.5px; font-weight: 600;
    color: var(--fg-secondary); cursor: pointer;
  }
  .qh-btn:hover { background: var(--adaptive-medium); }
  .qh-fsum { margin-left: auto; font-size: 12px; color: var(--fg-tertiary); }
  .qh-fsum strong { color: var(--fg-primary); font-weight: 600; }
  .qh-muted { color: var(--fg-tertiary); }

  /* Jump links to the section labels. */
  .qh-jump { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 18px; }
  .qh-chip {
    padding: 5px 11px; background: var(--bg-light); border: 1px solid var(--stroke-medium);
    border-radius: 999px; font-size: 12px; font-weight: 500; color: var(--fg-secondary);
  }
  .qh-chip:hover { background: var(--adaptive-medium); color: var(--fg-primary); text-decoration: none; }

  /* KPI cards */
  .qh-kpi-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(158px, 1fr));
    gap: 10px; margin: 0 0 16px;
  }
  .qh-kpi-flush { margin: 0; }
  .qh-metric {
    background: var(--bg-light); border: 1px solid var(--stroke-light);
    border-radius: var(--r-lg); padding: 18px; min-width: 0;
  }
  .qh-metric-v { font-size: 30px; font-weight: 700; letter-spacing: -1px; line-height: 1.2; }
  .qh-metric-k { font-size: 12.5px; color: var(--fg-secondary); margin-top: 4px; font-weight: 500; }
  .qh-metric-sub { font-size: 11px; color: var(--fg-accent); margin-top: 3px; }

  /* Cards, section labels, the two-column grid */
  .qh-mcard {
    background: var(--bg-white); border: 1px solid var(--stroke-medium);
    border-radius: var(--r-lg); padding: 18px; min-width: 0;
  }
  .qh-apad > .qh-mcard { margin-bottom: 14px; }
  .qh-mcard-title { font-size: 13px; font-weight: 600; margin-bottom: 16px; }
  .qh-msection {
    margin: 24px 0 12px; padding-top: 16px; border-top: 1px solid var(--stroke-light);
    font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em;
    color: var(--fg-tertiary); scroll-margin-top: 12px;
  }
  .qh-mgrid {
    display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 14px; margin-bottom: 14px;
  }
  .qh-mgrid > .is-wide { grid-column: 1 / -1; }
  .chart-wrap { position: relative; height: 240px; }
  .qh-mcard.is-wide .chart-wrap { height: 280px; }
  .chart-wrap.chart-wrap--auto,
  .qh-mcard .chart-wrap.chart-wrap--auto { height: auto; }

  /* Labelled bars */
  .qh-toplist { display: flex; flex-direction: column; gap: 12px; }
  .qh-toprow { display: flex; align-items: center; gap: 12px; }
  .qh-topuser {
    flex: 0 0 150px; font-size: 12.5px; color: var(--fg-secondary); font-family: var(--qh-mono);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .qh-toptrack {
    flex: 1; min-width: 60px; height: 8px; background: var(--adaptive-medium);
    border-radius: 999px; overflow: hidden;
  }
  .qh-topfill {
    height: 100%; display: flex; border-radius: 999px; overflow: hidden;
    background: var(--brand-green);
  }
  .qh-topseg { flex: 1 1 0; min-width: 0; height: 100%; }
  .qh-topn {
    flex: 0 0 56px; font-family: var(--qh-mono); font-size: 12px;
    color: var(--fg-tertiary); text-align: right;
  }
  .qh-legend {
    display: flex; flex-wrap: wrap; gap: 12px; margin-top: 12px;
    font-size: 11px; color: var(--fg-tertiary);
  }
  .qh-legend .lg { display: inline-flex; align-items: center; gap: 5px; }
  .qh-legend .lg::before {
    content: ''; width: 9px; height: 9px; border-radius: 2px; background: var(--seg);
  }

  /* Day x hour heatmap */
  .qh-heat { display: flex; flex-direction: column; gap: 3px; overflow-x: auto; }
  .qh-heatrow { display: flex; gap: 3px; align-items: center; }
  .qh-heat-lbl {
    flex: 0 0 34px; font-size: 10.5px; color: var(--fg-tertiary);
    text-align: right; padding-right: 4px;
  }
  .qh-heat-h { flex: 1; min-width: 14px; font-size: 9.5px; color: var(--fg-tertiary); text-align: center; }
  .qh-heatcell { flex: 1; min-width: 14px; height: 22px; border-radius: 3px; background: var(--brand-green); }

  /* Tables */
  .qh-tablewrap { overflow-x: auto; }
  .qh-kpi-grid + .qh-tablewrap { margin-top: 6px; }
  .qh-atable { width: 100%; border-collapse: collapse; font-size: 13px; }
  .qh-atable th {
    text-align: left; padding: 9px 12px; font-size: 11px; font-weight: 600;
    letter-spacing: 0.04em; text-transform: uppercase; color: var(--fg-tertiary);
    border-bottom: 1px solid var(--stroke-medium); white-space: nowrap;
  }
  .qh-atable td {
    padding: 10px 12px; border-bottom: 1px solid var(--stroke-light);
    vertical-align: middle;
  }
  .qh-atable tr:hover td { background: var(--adaptive-light); }
  .qh-atable .num { text-align: right; font-family: var(--qh-mono); font-size: 12px; white-space: nowrap; }
  .qh-atable .qh-heatbar { width: 28%; min-width: 120px; }
  .qh-atable .nowrap { white-space: nowrap; }
  .qh-mono { font-family: var(--qh-mono); font-size: 12px; }
  .qh-tier {
    display: inline-flex; font-size: 10px; font-weight: 700; font-family: var(--qh-mono);
    padding: 2px 7px; border-radius: var(--r-2xs); letter-spacing: 0.02em;
  }
  .tier-ro  { color: var(--fg-accent);  background: var(--brand-adaptive-light); }
  .tier-rw  { color: var(--tier-rw-fg); background: var(--tier-rw-bg); }
  .tier-ddl { color: var(--fg-danger);  background: var(--danger-adaptive-light); }
  .qh-st-completed { color: var(--fg-accent); font-weight: 600; }
  .qh-st-failed, .qh-st-rejected { color: var(--fg-danger); font-weight: 600; }
  .empty { color: var(--fg-tertiary); font-size: 12.5px; padding: 4px 0; }

  @media (max-width: 960px) {
    .qh-mgrid { grid-template-columns: minmax(0, 1fr); }
  }
  @media (max-width: 640px) {
    .qh-top { padding: 0 16px; }
    .qh-apad { padding: 16px 16px 32px; }
    .qh-fsum { margin-left: 0; width: 100%; }
    .qh-topuser { flex-basis: 96px; }
  }
</style>
</head>
<body>
<header class="qh-top">
  <div class="qh-brand">
    <img class="qh-applogo" src="queryhub-mark.svg" alt="QueryHub" width="26" height="26">
    <span class="qh-brand-name">QueryHub</span>
    <span class="qh-brand-tag">Metrics</span>
  </div>
</header>

<main class="qh-apad">
  <div class="qh-aview-head">
    <div>
      <div class="qh-aview-title">Metrics</div>
      <div class="qh-aview-sub">%VIEW_SUB%</div>
    </div>
    <span class="qh-updated"
          title="Wall-clock time the HTML was built, sourced from a HEAD request to google.com (NTP-synced). The 1_hour publisher rebuilds it; reload for the latest copy.">
      <span class="qh-dot"></span>Updated %GENERATED_AT%
    </span>
  </div>

  <div class="qh-filterbar" role="region" aria-label="Dashboard filters">
    <div class="qh-frow">
      <div class="qh-seg" role="group" aria-label="Date range">
        <button type="button" class="qh-seg-opt is-active" data-preset="all">All time</button>
        <button type="button" class="qh-seg-opt" data-preset="90">Last 90d</button>
        <button type="button" class="qh-seg-opt" data-preset="30">Last 30d</button>
        <button type="button" class="qh-seg-opt" data-preset="7">Last 7d</button>
        <button type="button" class="qh-seg-opt" data-preset="today">Today</button>
      </div>
      <input type="date" id="filter-from" class="qh-input qh-input-date" aria-label="From date">
      <span class="qh-muted">→</span>
      <input type="date" id="filter-to"   class="qh-input qh-input-date" aria-label="To date">
      <button type="button" class="qh-btn" id="filter-reset" title="Clear every filter">Reset</button>
      <span class="qh-fsum" id="filter-summary">—</span>
    </div>
    <div class="qh-frow">
      <select id="filter-team"    class="qh-select" aria-label="Team"></select>
      <select id="filter-user"    class="qh-select" aria-label="Requester"></select>
      <select id="filter-target"  class="qh-select" aria-label="Target server (RDS)"></select>
      <select id="filter-db"      class="qh-select" aria-label="Database"></select>
      <select id="filter-tier"    class="qh-select" aria-label="Tier"></select>
      <select id="filter-status"  class="qh-select" aria-label="Status"></select>
    </div>
  </div>

  <nav class="qh-jump" aria-label="Sections">
%TOC%
  </nav>

%SECTIONS%
</main>

<script>
window.__DATA = %DATA%;
%RENDERER_JS%
</script>
</body>
</html>"""


# ----------------------------- chart specs ---------------------------------
#
# Each spec is (id, title, factory_name, group, layout). The renderer JS picks
# the factory function by name and runs it against the current filtered row
# set. A group opens a labelled two-column grid the first time it appears,
# the way the admin panel's Metrics view is laid out (None = above the first
# label). Layout is "half", "wide" (both columns) or "bare" (no card).


CHART_SPECS = [
    # KPIs come first — at-a-glance numbers
    ("kpi-headline",   "Headline KPIs",                           "kpi",            None, "bare"),
    ("cost-savings",   "Cost savings snapshot",                   "kpiCostSavings", None, "wide"),
    # Volume / status mix
    ("volume-daily",   "Daily volume — status mix + active users", "volumeDaily",   "Volume", "wide"),
    ("volume-weekly",  "Weekly volume — status mix + WAU",        "volumeWeekly",   "Volume", "half"),
    ("volume-monthly", "Monthly volume — status mix + MAU",       "volumeMonthly",  "Volume", "half"),
    # Outcomes, tiers, approvals
    ("failure-breakdown", "Terminal outcomes per week + success rate", "failureBreakdown",
     "Outcomes & approvals", "half"),
    ("tier-distribution", "Tier mix per week",                    "tierDistribution",
     "Outcomes & approvals", "half"),
    ("approval-sla",   "Approval latency percentiles",            "approvalSla",
     "Outcomes & approvals", "half"),
    ("admin-workload", "Admin workload (decisions taken)",        "adminWorkload",
     "Outcomes & approvals", "half"),
    # When requests arrive
    ("peak-hours",     "Peak hours — day × hour (local)",         "peakHours",
     "When requests arrive", "wide"),
    ("business-offhours", "Business hours vs off-hours (weekly)", "businessOffhours",
     "When requests arrive", "half"),
    ("scheduled-usage", "Scheduled-request adoption (weekly %)",  "scheduledUsage",
     "When requests arrive", "half"),
    # Per-team / per-user / per-target
    ("team-usage",     "Per-team usage",                          "teamUsage",
     "Teams, people & targets", "half"),
    ("top-users",      "Top 10 users by total requests",          "topUsers",
     "Teams, people & targets", "half"),
    ("target-heatmap", "Target heatmap — usage by alias",         "targetHeatmap",
     "Teams, people & targets", "wide"),
    # Ratings
    ("rating-weekly",  "Weekly avg rating (1-5) + counts",        "ratingWeekly",   "Ratings", "half"),
    ("rating-response", "Rating response rate (weekly %)",        "ratingResponse", "Ratings", "half"),
    ("rating-low",     "Low ratings (≤2) with feedback",          "ratingLow",      "Ratings", "wide"),
    # CSV bulk imports + static refs
    ("csv-imports",    "CSV bulk imports",                        "csvImports",
     "Imports & access", "wide"),
    ("who-can-what",   "Who can do what",                         "whoCanWhat",
     "Imports & access", "wide"),
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

// Theme tokens — read on load so chart text and fills match the CSS.
function readToken(name, fallback) {
  const v = getComputedStyle(document.documentElement)
    .getPropertyValue(name).trim();
  return v || fallback;
}
const COLOR_FG_SECONDARY = readToken('--fg-secondary', 'rgba(31,34,41,0.8)');
const COLOR_FG_TERTIARY  = readToken('--fg-tertiary',  'rgba(31,34,41,0.66)');
const COLOR_STROKE_MED   = readToken('--stroke-medium','rgba(31,34,41,0.08)');
const COLOR_WEEKEND      = readToken('--adaptive-strong', 'rgba(31,34,41,0.08)');

// Color palette — the admin panel's, tied to status / tier so the two pages
// mean the same thing by a colour: completed and RO green, RW blue, DDL and
// failed red, rejected amber, cancelled grey.
const C = {
  completed:    '#C4603F',
  failed:       '#E53D3D',
  rejected:     '#E0A800',
  cancelled:    COLOR_FG_TERTIARY,
  ro:           '#C4603F',
  rw:           '#66CCFF',
  ddl_or_other: '#E53D3D',
  blue:         '#66CCFF',
  purple:       '#BFB2FF',
  recent:       '#B0512F',
  overlay:      COLOR_FG_SECONDARY,
  neutral:      COLOR_FG_TERTIARY,
};

Chart.defaults.color = COLOR_FG_TERTIARY;
Chart.defaults.borderColor = COLOR_STROKE_MED;
Chart.defaults.font.family = "'Manrope', -apple-system, 'Segoe UI', "
                           + "Roboto, system-ui, sans-serif";
Chart.defaults.font.size = 11;
if (window['chartjs-plugin-annotation']) {
  Chart.register(window['chartjs-plugin-annotation']);
}

// Legend and tooltip keep dataset order even where `order` lifts a line
// above the bars it overlays.
const byDataset = (a, b) => a.datasetIndex - b.datasetIndex;

const CHART_DEFAULTS = {
  responsive: true,
  maintainAspectRatio: false,
  interaction: { mode: 'index', intersect: false },
  plugins: {
    legend: {
      position: 'bottom',
      align: 'start',
      labels: {
        color: COLOR_FG_TERTIARY,
        usePointStyle: true,
        pointStyle: 'rectRounded',
        boxWidth: 9,
        boxHeight: 9,
        padding: 12,
        font: { size: 11 },
        sort: byDataset,
      },
    },
    tooltip: {
      // A fixed dark surface: --fg-primary turns white in dark mode, and the
      // text on it is white.
      backgroundColor: '#1F2229',
      borderColor: 'rgba(255, 255, 255, 0.15)',
      borderWidth: 1,
      titleColor: '#FFFFFF',
      bodyColor: '#FFFFFF',
      padding: 10,
      cornerRadius: 8,
      titleFont: { size: 12, weight: '600' },
      bodyFont:  { size: 12 },
      displayColors: true,
      usePointStyle: true,
      boxPadding: 6,
      itemSort: byDataset,
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
      backgroundColor: COLOR_WEEKEND,
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

// Axes the admin panel's way: no frame, no vertical grid lines, faint
// horizontal ones, small tertiary text.
function axisTitle(text) {
  return { display: !!text, text: text || '', color: COLOR_FG_TERTIARY,
           font: { size: 11 } };
}

function xAxis(bucket, stacked) {
  return {
    stacked: !!stacked,
    grid: { display: false },
    border: { display: false },
    ticks: {
      maxRotation: 0,
      autoSkipPadding: 14,
      callback: function (value) {
        return shortDate(this.getLabelForValue(value), bucket);
      },
    },
  };
}

function yAxis(title, stacked) {
  return {
    stacked: !!stacked,
    beginAtZero: true,
    grid: { color: COLOR_STROKE_MED, drawTicks: false },
    border: { display: false },
    ticks: { padding: 8 },
    title: axisTitle(title),
  };
}

function y1Axis(title) {
  return {
    position: 'right',
    beginAtZero: true,
    grid: { drawOnChartArea: false, drawTicks: false },
    border: { display: false },
    ticks: { padding: 8 },
    title: axisTitle(title),
  };
}

// "2026-09-22" -> "22 Sep" (a day, or a week's Monday); "Sep 2026" for a
// month. The labels themselves stay ISO: annotations match on them.
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function shortDate(iso, bucket) {
  if (typeof iso !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(iso)) return iso;
  const month = MONTHS[+iso.slice(5, 7) - 1];
  return bucket === 'month' ? month + ' ' + iso.slice(0, 4)
                            : +iso.slice(8, 10) + ' ' + month;
}

function withAnnotations(opts) {
  return {
    ...CHART_DEFAULTS.plugins,
    annotation: { annotations: buildAnnotations(opts.annotations, opts.weekends) },
  };
}

// Stacked bars, the admin panel's status-mix chart, with an optional dashed
// line on a second axis (active users, success rate). One bar per bucket
// reads the same with one bucket or with two hundred.
function stackSpec(labels, datasets, opts) {
  opts = opts || {};
  const ds = datasets.map(d => ({
    label: d.label,
    data: d.data,
    backgroundColor: d.color,
    borderWidth: 0,
    stack: 'stack0',
    order: 1,
    categoryPercentage: 0.86,
    barPercentage: 0.94,
  }));
  if (opts.overlay) {
    ds.push({
      label: opts.overlay.label,
      data: opts.overlay.data,
      type: 'line',
      borderColor: opts.overlay.color || C.overlay,
      backgroundColor: opts.overlay.color || C.overlay,
      borderWidth: 1.5,
      borderDash: [5, 4],
      pointRadius: labels.length <= 1 ? 4 : 0,
      pointHoverRadius: 3,
      fill: false,
      tension: 0.3,
      cubicInterpolationMode: 'monotone',
      yAxisID: 'y1',
      order: 0,
    });
  }
  const scales = {
    x: xAxis(opts.bucket, true),
    y: yAxis(opts.yTitle || 'requests', true),
  };
  if (opts.overlay) scales.y1 = y1Axis(opts.y1Title || '');
  return {
    type: 'bar',
    data: { labels, datasets: ds },
    options: { ...CHART_DEFAULTS, plugins: withAnnotations(opts), scales },
  };
}

function lineSpec(labels, datasets, opts) {
  opts = opts || {};
  // A line with a single point is just a floating dot; switch to bars.
  const sparse = labels.length <= 1;
  const ds = datasets.map(d => ({
    label: d.label,
    data: d.data,
    borderColor: d.color,
    backgroundColor: d.color,
    fill: false,
    tension: 0.3,
    cubicInterpolationMode: 'monotone',
    borderWidth: sparse ? 0 : (d.dashed ? 1.5 : 2),
    pointRadius: 0,
    pointHoverRadius: 3,
    yAxisID: d.secondAxis ? 'y1' : 'y',
    borderDash: d.dashed ? [5, 4] : undefined,
    type: sparse ? 'bar' : 'line',
  }));
  const scales = {
    x: xAxis(opts.bucket, false),
    y: yAxis(opts.yTitle || 'value', false),
  };
  if (ds.some(d => d.yAxisID === 'y1')) scales.y1 = y1Axis(opts.y1Title || '');
  return {
    type: sparse ? 'bar' : 'line',
    data: { labels, datasets: ds },
    options: { ...CHART_DEFAULTS, plugins: withAnnotations(opts), scales },
  };
}

// Labelled horizontal bars, the admin panel's BarList. Each item is
// { label, parts: [{ name, value, color }] }: the parts stack inside one
// fill, and a fill's length is its total against the largest total.
function renderBarList(containerId, items, legend) {
  const container = document.getElementById(containerId);
  if (!items.length) {
    container.innerHTML = '<div class="empty">No requests match the current filters.</div>';
    return;
  }
  const totals = items.map(it => it.parts.reduce((s, p) => s + p.value, 0));
  const max = Math.max(1, ...totals);
  let out = '<div class="qh-toplist">';
  items.forEach((it, i) => {
    const segs = it.parts.filter(p => p.value > 0).map(p =>
      '<span class="qh-topseg" style="flex-grow:' + p.value + ';background:' + p.color + '"'
      + ' title="' + escapeHtml(p.name + ': ' + fmtNum(p.value)) + '"></span>').join('');
    out += '<div class="qh-toprow">'
      + '<span class="qh-topuser" title="' + escapeHtml(it.label) + '">'
      + escapeHtml(it.label) + '</span>'
      + '<div class="qh-toptrack"><div class="qh-topfill" style="width:'
      + (100 * totals[i] / max) + '%">' + segs + '</div></div>'
      + '<span class="qh-topn">' + fmtNum(totals[i]) + '</span>'
      + '</div>';
  });
  out += '</div>';
  if (legend) out += legendHtml(legend);
  container.innerHTML = out;
}

function legendHtml(items) {
  return '<div class="qh-legend">' + items.map(it =>
    '<span class="lg" style="--seg:' + it.color + '">' + escapeHtml(it.name) + '</span>'
  ).join('') + '</div>';
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
    return stackSpec(labels, [
      { label: 'completed', data: sc('completed'), color: C.completed },
      { label: 'failed',    data: sc('failed'),    color: C.failed    },
      { label: 'rejected',  data: sc('rejected'),  color: C.rejected  },
      { label: 'cancelled', data: sc('cancelled'), color: C.cancelled },
    ], {
      overlay: { label: opts.overlayLabel, data: active, color: C.overlay },
      yTitle: 'requests',
      y1Title: opts.y1Title,
      bucket: opts.bucket,
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
  return stackSpec(labels, [
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
  return stackSpec(labels, [
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
    { label: 'scheduled %',    data: scheduledPct, color: C.completed },
    { label: 'total requests', data: totals,       color: C.neutral,
      secondAxis: true, dashed: true },
  ], {
    yTitle: 'percent',
    y1Title: 'requests',
    annotations: annotationsFor(labels),
  });
};

// Seconds a PERSON took to decide. An auto-approval is decided by a grant in
// about no time; they are most decisions, and counting them put every
// percentile near zero.
function humanApprovalSecs(rows) {
  return rows.filter(r => !r.auto_approved)
             .map(r => r.approval_sec).filter(v => v != null);
}

FACTORIES.approvalSla = function (rows) {
  const { labels, map } = gapFilledBuckets(rows, truncWeek, weeksBetween);
  const p50raw = labels.map(k => {
    const vals = humanApprovalSecs(map.get(k) || []);
    return vals.length ? pct(vals, 0.5) : null;
  });
  const p90raw = labels.map(k => {
    const vals = humanApprovalSecs(map.get(k) || []);
    return vals.length ? pct(vals, 0.9) : null;
  });
  const p95raw = labels.map(k => {
    const vals = humanApprovalSecs(map.get(k) || []);
    return vals.length ? pct(vals, 0.95) : null;
  });
  const u = chooseTimeUnit([p50raw, p90raw, p95raw]);
  // Mirror the unit into the section title.
  const titleEl = document.querySelector('#sec-approval-sla .qh-mcard-title');
  if (titleEl) titleEl.textContent = 'Approval latency percentiles, decided by people (' + u.unit + ')';
  return lineSpec(labels, [
    { label: 'p50', data: scaleTime(p50raw, u.div, u.decimals), color: C.completed },
    { label: 'p90', data: scaleTime(p90raw, u.div, u.decimals), color: C.rejected  },
    { label: 'p95', data: scaleTime(p95raw, u.div, u.decimals), color: C.failed    },
  ], { yTitle: u.unit, annotations: annotationsFor(labels) });
};

FACTORIES.businessOffhours = function (rows) {
  const { labels, map } = gapFilledBuckets(rows, truncWeek, weeksBetween);
  const cls = (filter) => labels.map(k => (map.get(k) || []).filter(filter).length);
  return stackSpec(labels, [
    { label: 'business hours',  data: cls(r => r.dow_local >= 1 && r.dow_local <= 5
                                          && r.hour_local >= 9 && r.hour_local <= 17),
      color: C.completed },
    { label: 'weekday evening', data: cls(r => r.dow_local >= 1 && r.dow_local <= 5
                                          && r.hour_local >= 18),
      color: C.rejected  },
    { label: 'weekday early',   data: cls(r => r.dow_local >= 1 && r.dow_local <= 5
                                          && r.hour_local <  9),
      color: C.blue      },
    { label: 'weekend',         data: cls(r => r.dow_local === 0 || r.dow_local === 6),
      color: C.purple    },
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
  const size = t => t.completed + t.failed + t.rejected;
  const labels = Object.keys(byTeam).sort((a, b) => size(byTeam[b]) - size(byTeam[a]));
  renderBarList('canvas-team-usage', labels.map(k => ({
    label: k,
    parts: [
      { name: 'completed', value: byTeam[k].completed, color: C.completed },
      { name: 'failed',    value: byTeam[k].failed,    color: C.failed    },
      { name: 'rejected',  value: byTeam[k].rejected,  color: C.rejected  },
    ],
  })), [
    { name: 'completed', color: C.completed },
    { name: 'failed',    color: C.failed    },
    { name: 'rejected',  color: C.rejected  },
  ]);
  return null;
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
  // Oldest on the left, the last seven days at the end of the bar.
  renderBarList('canvas-top-users', sorted.map(u => ({
    label: personName(u.name),
    parts: [
      { name: 'earlier',     value: u.total - u.recent, color: C.completed },
      { name: 'last 7 days', value: u.recent,           color: C.recent    },
    ],
  })), [
    { name: 'earlier',     color: C.completed },
    { name: 'last 7 days', color: C.recent    },
  ]);
  return null;
};

FACTORIES.adminWorkload = function (rows) {
  // Decisions taken by people; an auto-approval is not anyone's workload.
  const byAdmin = {};
  rows.filter(r => r.decided_by_slack_id && !r.auto_approved).forEach(r => {
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
  renderBarList('canvas-admin-workload', sorted.map(a => ({
    label: personName(a.name),
    parts: [
      { name: 'approved',          value: a.approved, color: C.completed },
      { name: 'rejected',          value: a.rejected, color: C.failed    },
      { name: 'changes requested', value: a.changes,  color: C.rejected  },
    ],
  })), [
    { name: 'approved',          color: C.completed },
    { name: 'rejected',          color: C.failed    },
    { name: 'changes requested', color: C.rejected  },
  ]);
  return null;
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
  if (!ranked.length) {
    container.innerHTML = '<div class="empty">No requests match the current filters.</div>';
    return null;
  }
  let out = '<div class="qh-tablewrap"><table class="qh-atable"><thead><tr>'
    + '<th>Target</th><th class="num">Total</th><th class="num">Completed</th>'
    + '<th class="num">Failed</th><th>Heat</th><th>Last used</th></tr></thead><tbody>';
  ranked.forEach(t => {
    out += '<tr><td class="qh-mono">' + escapeHtml(t.alias) + '</td>'
      + '<td class="num">' + fmtNum(t.total) + '</td>'
      + '<td class="num">' + fmtNum(t.completed) + '</td>'
      + '<td class="num">' + fmtNum(t.failed) + '</td>'
      + '<td class="qh-heatbar"><div class="qh-toptrack"><div class="qh-topfill" style="width:'
      + (100 * t.total / max) + '%"></div></div></td>'
      + '<td class="qh-muted">' + (t.last_used ? t.last_used.slice(0, 10) : '—') + '</td></tr>';
  });
  container.innerHTML = out + '</tbody></table></div>';
  return null;  // signal "no Chart.js instance"
};

FACTORIES.peakHours = function (rows) {
  // Day-of-week × hour heatmap. dow_local: 0 = Sun, 1 = Mon … 6 = Sat.
  // Reorder to Mon-first for the display.
  const dayLabels = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const cells = {};
  rows.forEach(r => {
    const key = r.dow_local + '|' + r.hour_local;
    cells[key] = (cells[key] || 0) + 1;
  });
  const max = Math.max(0, ...Object.values(cells));
  const container = document.getElementById('canvas-peak-hours');
  if (!Object.keys(cells).length) {
    container.innerHTML = '<div class="empty">No requests match the current filters.</div>';
    return null;
  }
  // The admin panel's heatmap: one green, the count in its opacity.
  let out = '<div class="qh-heat"><div class="qh-heatrow"><span class="qh-heat-lbl"></span>';
  for (let h = 0; h < 24; h++) {
    out += '<span class="qh-heat-h">' + (h % 3 === 0 ? h : '') + '</span>';
  }
  out += '</div>';
  for (let dIdx = 0; dIdx < 7; dIdx++) {
    const dow = dIdx === 6 ? 0 : dIdx + 1;   // Sun (0) goes last
    out += '<div class="qh-heatrow"><span class="qh-heat-lbl">' + dayLabels[dIdx] + '</span>';
    for (let h = 0; h < 24; h++) {
      const v = cells[dow + '|' + h] || 0;
      const opacity = v ? (0.14 + 0.86 * v / max).toFixed(3) : '0.05';
      out += '<span class="qh-heatcell" style="opacity:' + opacity + '" title="'
        + dayLabels[dIdx] + ' ' + h + ':00 · ' + fmtNum(v) + ' request' + (v === 1 ? '' : 's')
        + '"></span>';
    }
    out += '</div>';
  }
  container.innerHTML = out + '</div>';
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
    { label: 'avg rating', data: avg.map(v => v == null ? 0 : v),    color: C.completed },
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
  if (!filtered.length) {
    container.innerHTML = '<div class="empty">No low ratings in the current filter window.</div>';
    return null;
  }
  let out = '<div class="qh-tablewrap"><table class="qh-atable"><thead><tr>'
    + '<th>Rated at</th><th>Rating</th><th>Feedback</th><th>Request</th>'
    + '<th>Requester</th><th>Status</th><th>Query</th></tr></thead><tbody>';
  filtered.forEach(r => {
    const ratedAt = r.rated_at ? r.rated_at.slice(0, 16).replace('T', ' ') : '';
    out += '<tr><td class="qh-muted">' + ratedAt + '</td>'
      + '<td>' + (r.rating ? r.rating + '★' : '') + '</td>'
      + '<td>' + escapeHtml(r.feedback_text || '—') + '</td>'
      + '<td class="qh-mono">#' + r.request_id + '</td>'
      + '<td class="nowrap">' + escapeHtml(personName(r.requester_name || r.requester_slack_id)) + '</td>'
      + '<td>' + statusHtml(r.status) + '</td>'
      + '<td><code>' + escapeHtml((r.query_preview || '').slice(0, 100)) + '</code></td></tr>';
  });
  container.innerHTML = out + '</tbody></table></div>';
  return null;
};

FACTORIES.whoCanWhat = function (rows) {
  // Static org structure — not filtered by request-side filters.
  const container = document.getElementById('canvas-who-can-what');
  if (!DATA.who_can_what.length) {
    container.innerHTML = '<div class="empty">No users registered.</div>';
    return null;
  }
  let out = '<div class="qh-tablewrap"><table class="qh-atable"><thead><tr>'
    + '<th>Name</th><th>Slack ID</th><th>Admin</th><th>Max tier</th><th>Bypass</th>'
    + '<th>Teams</th><th>User grants</th></tr></thead><tbody>';
  DATA.who_can_what.forEach(r => {
    out += '<tr><td class="nowrap">' + escapeHtml(r.name || '(?)') + '</td>'
      + '<td class="qh-mono">' + escapeHtml(r.slack_user_id || '') + '</td>'
      + '<td>' + (r.is_admin ? 'yes' : '') + '</td>'
      + '<td>' + tierHtml(r.admin_max_tier) + '</td>'
      + '<td>' + (r.is_bypass ? 'yes' : '') + '</td>'
      + '<td class="nowrap">' + escapeHtml((r.teams || []).join(', ')) + '</td>'
      + '<td class="qh-mono">' + escapeHtml((r.user_grants || []).join(', ')) + '</td></tr>';
  });
  container.innerHTML = out + '</tbody></table></div>';
  return null;
};

FACTORIES.csvImports = function (rows) {
  // CSV bulk imports (/sql import). Its own dataset (DATA.csv_imports),
  // independent of the request-side filters — imports aren't requests.
  // A summary line + a recent-imports table, rendered outside Chart.js.
  const imports = DATA.csv_imports || [];
  const container = document.getElementById('canvas-csv-imports');
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

  const completed = imports.filter(r => r.status === 'completed');
  const failed    = imports.filter(r => r.status === 'failed' || r.status === 'rejected');
  const rowsLoaded = completed.reduce((s, r) => s + (r.inserted_rows || 0), 0);
  const decided    = completed.length + failed.length;
  const successPct = decided ? Math.round(100 * completed.length / decided) : null;

  // Same cards as the headline KPIs.
  let out = kpiGridHtml([
    { label: 'Imports',           value: imports.length },
    { label: 'Completed',         value: completed.length },
    { label: 'Failed / rejected', value: failed.length },
    { label: 'Rows loaded',       value: rowsLoaded },
    { label: 'Success rate',      value: successPct == null ? '—' : successPct + '%' },
  ]);
  out += '<div class="qh-tablewrap"><table class="qh-atable"><thead><tr>'
    + '<th>When</th><th>Import</th><th>Requester</th><th>Target / DB</th><th>Table</th>'
    + '<th>New?</th><th>Status</th><th class="num">Rows</th><th class="num">Size</th>'
    + '<th class="num">Load</th></tr></thead><tbody>';
  // Most recent first.
  imports.slice().reverse().forEach(r => {
    const when = r.created_at ? r.created_at.slice(0, 16).replace('T', ' ') : '';
    const load = r.load_seconds == null ? '—'
               : (Number(r.load_seconds) < 1
                    ? (Number(r.load_seconds) * 1000).toFixed(0) + 'ms'
                    : Number(r.load_seconds).toFixed(1) + 's');
    const rowsCell = fmtNum(r.inserted_rows != null ? r.inserted_rows : r.row_count);
    out += '<tr><td class="qh-muted">' + when + '</td>'
      + '<td class="qh-mono">#' + r.id + '</td>'
      + '<td class="nowrap">' + escapeHtml(personName(r.requester_name || r.requester_slack_id)) + '</td>'
      + '<td class="qh-mono">' + escapeHtml((r.target_alias || '?') + ' / ' + (r.database_name || '')) + '</td>'
      + '<td><code>dba.' + escapeHtml(r.table_name || '') + '</code></td>'
      + '<td>' + (r.is_new_table ? 'new' : 'append') + '</td>'
      + '<td>' + statusHtml(r.status) + '</td>'
      + '<td class="num">' + rowsCell + '</td>'
      + '<td class="num">' + fmtBytes(r.byte_size) + '</td>'
      + '<td class="num">' + load + '</td></tr>';
  });
  container.innerHTML = out + '</tbody></table></div>';
  return null;
};

FACTORIES.kpi = function (rows) {
  const total = rows.length;
  const sc = statusCounts(rows);
  const decided = humanApprovalSecs(rows);
  const p50 = decided.length ? pct(decided, 0.5) : null;
  const p95 = decided.length ? pct(decided, 0.95) : null;
  const autoN = rows.filter(r => r.auto_approved).length;
  const approvedN = rows.filter(r => r.decided_by_slack_id).length;
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
    { label: 'Auto-approved',   value: autoN, hint: approvedN
        ? (Math.round(1000 * autoN / approvedN) / 10) + '% of decisions' : '' },
    { label: 'p50 approval',    value: fmtTime(p50), hint: 'decided by people' },
    { label: 'p95 approval',    value: fmtTime(p95), hint: 'decided by people' },
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
    { label: 'Completed',             value: completed },
    { label: 'DBA hours saved',       value: fmtNum(Number(dbaHoursSaved.toFixed(1)), 1),
      hint: dbaMin + 'm × $' + dbaHr + '/hr' },
    { label: 'DBA $ saved',           value: '$' + fmtNum(Number(dbaSavingUSD.toFixed(0))) },
    { label: 'Avoided replicas',      value: replicas },
    { label: 'Infra $ / mo',          value: '$' + fmtNum(Number(monthlyInfra.toFixed(0))),
      hint: 'replicas + other' },
  ], true);
  return null;
};

// The admin panel's Stat card: the number first, its label under it, a hint
// in the accent colour.
function kpiGridHtml(cards, flush) {
  return '<div class="qh-kpi-grid' + (flush ? ' qh-kpi-flush' : '') + '">'
    + cards.map(c => '<div class="qh-metric">'
      + '<div class="qh-metric-v">' + escapeHtml(fmtValue(c.value)) + '</div>'
      + '<div class="qh-metric-k">' + escapeHtml(c.label) + '</div>'
      + (c.hint ? '<div class="qh-metric-sub">' + escapeHtml(c.hint) + '</div>' : '')
      + '</div>').join('')
    + '</div>';
}

function renderKPIs(canvasId, cards, flush) {
  document.getElementById(canvasId).innerHTML = kpiGridHtml(cards, flush);
}

// 6933 -> "6,933", as the admin panel prints counts.
function fmtNum(n, digits) {
  if (n == null || !isFinite(n)) return '—';
  return Number(n).toLocaleString('en-US', digits == null ? undefined
    : { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

// "mehmet.genc" -> "Mehmet Genc". A Slack handle stands in for a name where
// no profile name existed; the admin panel prints it as a name the same way
// (qhPersonName in QueryHubWeb/qh-data.jsx) and leaves role accounts alone.
const ROLE_PREFIXES = ['dba', 'oncall', 'svc', 'service', 'bot', 'job', 'auto',
                       'admin', 'sys', 'ops', 'root'];
function personName(n) {
  if (typeof n !== 'string') return n;
  const s = n.trim();
  if (!s || /\s/.test(s) || !/[._]/.test(s) || !/^[a-z0-9._-]+$/i.test(s)) return n;
  const parts = s.split(/[._]+/).filter(Boolean);
  if (!parts.length || ROLE_PREFIXES.includes(parts[0].toLowerCase())) return n;
  return parts.map(p => p.charAt(0).toLocaleUpperCase('tr') + p.slice(1)).join(' ');
}

function fmtValue(v) {
  return typeof v === 'number' ? fmtNum(v) : String(v);
}

function tierHtml(tier) {
  if (!tier) return '';
  const t = String(tier).toLowerCase();
  if (!['ro', 'rw', 'ddl'].includes(t)) return escapeHtml(tier);
  return '<span class="qh-tier tier-' + t + '">' + t.toUpperCase() + '</span>';
}

function statusHtml(status) {
  const s = String(status || '');
  return '<span class="qh-st-' + s.replace(/[^a-z_]/g, '') + '">' + escapeHtml(s) + '</span>';
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

function render() {
  const filtered = applyFilters();

  // Filter summary line
  const sum = document.getElementById('filter-summary');
  const range = STATE.preset === 'custom'
    ? (STATE.from || '…') + ' → ' + (STATE.to || '…')
    : STATE.preset === 'all' ? 'All time' : presetLabel(STATE.preset);
  sum.innerHTML = '<strong>' + fmtNum(filtered.length) + '</strong> request'
    + (filtered.length === 1 ? '' : 's')
    + ' · ' + escapeHtml(range);

  destroyAllCharts();
  CHART_SPECS_DOM.forEach(spec => {
    const factory = FACTORIES[spec.factory];
    if (!factory) return;
    const wrap = document.getElementById('canvas-' + spec.id);
    // HTML-emit factories (KPI, bar list, table, heatmap) clear the wrap
    // and inject their own DOM; they want auto height. Chart.js factories
    // need a fresh canvas at the fixed height.
    const wantsChartJs = !['kpi', 'kpiCostSavings', 'targetHeatmap', 'peakHours',
                            'teamUsage', 'topUsers', 'adminWorkload',
                            'ratingLow', 'whoCanWhat', 'csvImports']
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
  document.querySelectorAll('[data-preset]').forEach(btn => {
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
    DATA.users.map(u => ({ value: u.id, label: personName(u.name) })),
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
  document.querySelectorAll('[data-preset]').forEach(b => {
    b.classList.toggle('is-active', b.dataset.preset === preset);
  });
}

// ===== boot ================================================================

setupFilters();
render();
"""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def render(payload: dict) -> str:
    # Section shells only (canvas placeholders); the JS fills them from the
    # rows at runtime. Each group gets a section label, a jump link, and a
    # two-column grid the way the admin panel's Metrics view is laid out.
    toc = []
    sections = []
    chart_specs_js = []

    group = None
    for spec_id, title, factory, spec_group, layout in CHART_SPECS:
        if spec_group != group:
            if group is not None:
                sections.append("</div>")
            group = spec_group
            if group is not None:
                slug = _slug(group)
                toc.append(f"<a class='qh-chip' href='#grp-{slug}'>{escape(group)}</a>")
                sections.append(
                    f"<div class='qh-msection' id='grp-{slug}'>{escape(group)}</div>\n"
                    f"<div class='qh-mgrid'>")
        if layout == "bare":
            sections.append(
                f"<section id='sec-{spec_id}' aria-label='{escape(title)}'>\n"
                f"  <div class='chart-wrap chart-wrap--auto' id='canvas-{spec_id}'></div>\n"
                f"</section>")
        else:
            wide = " is-wide" if layout == "wide" else ""
            sections.append(
                f"<section class='qh-mcard{wide}' id='sec-{spec_id}'>\n"
                f"  <div class='qh-mcard-title'>{escape(title)}</div>\n"
                f"  <div class='chart-wrap' id='canvas-{spec_id}'>\n"
                f"    <canvas></canvas>\n"
                f"  </div>\n"
                f"</section>")
        chart_specs_js.append({"id": spec_id, "factory": factory})
    if group is not None:
        sections.append("</div>")

    chart_specs_js_str = (
        "const CHART_SPECS_DOM = "
        + json.dumps(chart_specs_js, separators=(",", ":"))
        + ";\n"
    )

    # The admin panel's subtitle, word for word.
    cfg = payload.get("config") or {}
    sub = "From p_metrics_* (self-test excluded)"
    if cfg.get("report_start_date"):
        sub += f" · since {cfg['report_start_date']}"
    sub += f" · {cfg.get('report_timezone') or 'UTC'}"

    html = HTML
    html = html.replace("%FONT_FACES%", embedded_font_faces())
    html = html.replace("%GENERATED_AT%", escape(payload["generated_at"]))
    html = html.replace("%VIEW_SUB%", escape(sub))
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
