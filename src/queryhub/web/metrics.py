"""Product-metrics aggregation for the web admin Metrics view.

Computes the same panels the static S3 dashboard (scripts/build_metrics_
dashboard.py) shows, but server-side in Python over p_metrics_request_facts
(one reportable row per request; self-test traffic already excluded by the
view). ~1.4k rows today, so a full scan + in-memory aggregation is cheap and
keeps every panel's definition in one readable place. Returns a JSON-able dict
consumed by MetricsView in qh-admin-insights.jsx.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import db, metrics_defs, people
from ..auto_approve import AUTO_DECIDED_BY

# Definitions live in metrics_defs so the static S3 dashboard and this module
# cannot drift apart; see that module's docstring.
_TERMINAL = metrics_defs.TERMINAL_STATUSES
_COST_KEYS = metrics_defs.CONFIG_KEYS

#: The `?window=` values of GET /admin/metrics, in days (PLA-1592 U8). The
#: window is rolling: it starts this many days before the request, to the
#: second. No window means every reportable request, as before.
WINDOWS = {"7d": 7, "30d": 30}

#: The statuses a request reaches through a person's decision. `cancelled` is
#: not one of them: a withdrawal, an expiry and a superseded edit write
#: `decided_at` too, and nobody decided those.
_DECIDED_BY_A_PERSON = frozenset({
    "approved", "scheduled", "executing", "completed", "failed",
    "awaiting_dba_manual", "rejected", "changes_requested"})

#: Decision-time bands in seconds, the ones the Metrics screen draws:
#: under 5 min, 5 to 15 min, 15 to 60 min, an hour or more (None = no end).
_DECISION_BANDS = ((0, 300), (300, 900), (900, 3600), (3600, None))


def _f(v):
    return None if v is None else float(v)


def _pct(vals: list[float], q: float):
    """Linear-interpolated percentile of a numeric list (q in 0..1)."""
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _tz(config: dict):
    try:
        return ZoneInfo(config.get("report_timezone") or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def window_start(window: str | None, now: datetime | None = None) -> datetime | None:
    """The first moment a `?window=` value covers, or None for no window.
    Raises ValueError for a value that is not in WINDOWS."""
    if window is None:
        return None
    if window not in WINDOWS:
        raise ValueError(f"window must be one of {sorted(WINDOWS)}, got {window!r}")
    return (now or datetime.now(timezone.utc)) - timedelta(days=WINDOWS[window])


def _decision_seconds(r: dict) -> float | None:
    """Request to decision, in seconds, when a PERSON decided this request.

    None for an auto-approval (nobody waited for a person), for a self-decision
    (the requester decided: this includes a super-admin's own query in a row
    written before 2026-10-22), and for a request that was withdrawn, expired
    or is still open."""
    sec = r.get("approval_sec")
    by = r.get("decided_by_slack_id")
    if sec is None or not by or by == AUTO_DECIDED_BY:
        return None
    if by == r.get("requester_slack_id"):
        return None
    if r.get("status") not in _DECIDED_BY_A_PERSON:
        return None
    return _f(sec)


def _sec_stats(vals: list[float]) -> dict:
    def r1(v):
        return None if v is None else round(v, 1)
    return {"requests": len(vals), "p50Sec": r1(_pct(vals, .5)), "p90Sec": r1(_pct(vals, .9))}


def decision_time(rows: list[dict]) -> dict:
    """Request to decision for the requests a person decided (PLA-1592 U8).

    {requests, p50Sec, p90Sec, bands: [{fromSec, toSec, count}],
     byServer: [{name, requests, p50Sec, p90Sec}], byTeam: [same]}

    `byServer` groups by connection name, `byTeam` by the requester's pod.
    Each breakdown leaves out a request that has no name for it. Both lists
    put the busiest first, then sort by name."""
    secs: list[float] = []
    by_server: dict[str, list[float]] = defaultdict(list)
    by_team: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        sec = _decision_seconds(r)
        if sec is None:
            continue
        secs.append(sec)
        if r.get("target_alias"):
            by_server[r["target_alias"]].append(sec)
        if r.get("team"):
            by_team[r["team"]].append(sec)

    def breakdown(groups: dict[str, list[float]]) -> list[dict]:
        out = [{"name": name, **_sec_stats(vals)} for name, vals in groups.items()]
        return sorted(out, key=lambda e: (-e["requests"], e["name"]))

    return {
        **_sec_stats(secs),
        "bands": [{"fromSec": lo, "toSec": hi,
                   "count": sum(1 for s in secs if s >= lo and (hi is None or s < hi))}
                  for lo, hi in _DECISION_BANDS],
        "byServer": breakdown(by_server),
        "byTeam": breakdown(by_team),
    }


def build_metrics(window: str | None = None) -> dict:
    """Every Metrics panel. `window` ("7d" | "30d") keeps the requests,
    imports and ratings of that many days. None keeps all of them."""
    since = window_start(window)
    if since is None:
        rows = db.fetch_all("SELECT * FROM p_metrics_request_facts")
    else:
        rows = db.fetch_all(
            "SELECT * FROM p_metrics_request_facts WHERE created_at >= %s", (since,))
    config = {r["key"]: r["value"]
              for r in db.fetch_all(
                  "SELECT key, value FROM bot_config WHERE key = ANY(%s)",
                  (list(_COST_KEYS),))}
    tz = _tz(config)

    # local-time buckets (match the view's hour_local / dow_local)
    def day_key(r):
        c = r["created_at"]
        return c.astimezone(tz).strftime("%Y-%m-%d") if c else None

    def week_key(r):
        c = r["created_at"]
        if not c:
            return None
        iso = c.astimezone(tz).isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"

    def month_key(r):
        c = r["created_at"]
        return c.astimezone(tz).strftime("%Y-%m") if c else None

    def _volume(keyfn):
        by = defaultdict(lambda: {"completed": 0, "failed": 0, "rejected": 0,
                                  "cancelled": 0, "total": 0, "_users": set()})
        for r in rows:
            k = keyfn(r)
            if not k:
                continue
            b = by[k]
            b["total"] += 1
            if r["status"] in b:
                b[r["status"]] += 1
            if r["requester_slack_id"]:
                b["_users"].add(r["requester_slack_id"])
        out = []
        for k in sorted(by):
            b = by[k]
            out.append({"period": k, "completed": b["completed"],
                        "failed": b["failed"], "rejected": b["rejected"],
                        "cancelled": b["cancelled"], "total": b["total"],
                        "activeUsers": len(b["_users"])})
        return out

    # ---- headline KPIs ----
    total = len(rows)
    sc = Counter(r["status"] for r in rows)
    # Seconds a person took to decide: an auto-approval is decided by a grant
    # in about no time, and counting them put every percentile near zero.
    appr = [_f(r["approval_sec"]) for r in rows
            if r["approval_sec"] is not None and not r.get("auto_approved")]
    ratings = [r["rating"] for r in rows if r["rating"] is not None]
    headline = {
        "total": total,
        "completed": sc.get("completed", 0),
        "failed": sc.get("failed", 0),
        "rejected": sc.get("rejected", 0),
        "cancelled": sc.get("cancelled", 0),
        "uniqueUsers": len({r["requester_slack_id"] for r in rows if r["requester_slack_id"]}),
        "targetsTouched": len({r["target_alias"] for r in rows if r["target_alias"]}),
        "p50ApprovalSec": _pct(appr, 0.5),
        "p95ApprovalSec": _pct(appr, 0.95),
        "avgRating": round(sum(ratings) / len(ratings), 2) if ratings else None,
        "ratingCount": len(ratings),
        "autoApproveRate": round(sum(1 for r in rows if r["decided_by_slack_id"] == "AUTO") / total, 3) if total else 0,
        "successRate": round(sc.get("completed", 0) / total, 3) if total else 0,
        "rejectRate": round((sc.get("rejected", 0) + sc.get("cancelled", 0)) / total, 3) if total else 0,
    }

    # ---- cost savings ----
    # The formula is shared with the static dashboard's JS implementation, and
    # tests/test_metrics_parity.py runs both over the same inputs.
    cost = metrics_defs.cost_savings(sc.get("completed", 0), config)

    # ---- approval SLA percentiles (overall + by tier) ----
    sla = {"overall": {p: _pct(appr, q) for p, q in
                       (("p50", .5), ("p75", .75), ("p90", .9), ("p95", .95), ("p99", .99))}}
    by_tier_sec = defaultdict(list)
    for r in rows:
        if r["approval_sec"] is not None and r["tier"] and not r.get("auto_approved"):
            by_tier_sec[r["tier"].upper()].append(_f(r["approval_sec"]))
    sla["byTier"] = {t: {"p50": _pct(v, .5), "p95": _pct(v, .95), "n": len(v)}
                     for t, v in sorted(by_tier_sec.items())}

    # ---- failure breakdown per week + success rate ----
    fb: dict = defaultdict(lambda: {"completed": 0, "failed": 0, "rejected": 0, "cancelled": 0})
    for r in rows:
        k = week_key(r)
        if k and r["status"] in _TERMINAL:
            fb[k][r["status"]] += 1
    failure = []
    for k in sorted(fb):
        b = fb[k]
        tot = sum(b.values())
        failure.append({"period": k, **b,
                        "successRate": round(b["completed"] / tot, 3) if tot else 0})

    # ---- tier mix per week ----
    tw: dict = defaultdict(lambda: {"RO": 0, "RW": 0, "DDL": 0})
    for r in rows:
        k = week_key(r)
        if k and r["tier"] and r["tier"].upper() in tw[k]:
            tw[k][r["tier"].upper()] += 1
    tier_weekly = [{"period": k, **tw[k]} for k in sorted(tw)]
    tier_totals = Counter(r["tier"].upper() for r in rows if r["tier"])

    # ---- scheduled adoption per week (% of requests scheduled) ----
    sched: dict = defaultdict(lambda: {"scheduled": 0, "total": 0})
    for r in rows:
        k = week_key(r)
        if k:
            sched[k]["total"] += 1
            if r["scheduled_for"] is not None:
                sched[k]["scheduled"] += 1
    scheduled = [{"period": k, "scheduled": sched[k]["scheduled"], "total": sched[k]["total"],
                  "pct": round(100 * sched[k]["scheduled"] / sched[k]["total"], 1) if sched[k]["total"] else 0}
                 for k in sorted(sched)]

    # ---- business hours vs off-hours (weekly); business = Mon-Fri 09-18 local ----
    bh: dict = defaultdict(lambda: {"business": 0, "offHours": 0})
    for r in rows:
        k = week_key(r)
        h, d = r["hour_local"], r["dow_local"]
        if not k or h is None or d is None:
            continue
        biz = (1 <= d <= 5) and (9 <= h < 18)
        bh[k]["business" if biz else "offHours"] += 1
    business = [{"period": k, **bh[k]} for k in sorted(bh)]

    # ---- peak hours grid: row 0=Sun..6=Sat x hour 0-23 counts ----
    # `d % 7` handles both dow (0=Sun..6=Sat) and isodow (1=Mon..7=Sun→0) so
    # neither convention drops a day; the row order is Sun-first either way.
    peak = [[0] * 24 for _ in range(7)]
    for r in rows:
        h, d = r["hour_local"], r["dow_local"]
        if h is not None and d is not None and 0 <= h <= 23:
            peak[d % 7][h] += 1

    # ---- per-team / top-users / admin-workload / target usage ----
    def _rank(counter, limit=None):
        items = sorted(counter.items(), key=lambda kv: -kv[1])
        if limit:
            items = items[:limit]
        return [{"name": k or "—", "count": v} for k, v in items]

    # Every person this payload prints, resolved in one lookup. The fact rows
    # carry a handle wherever the identity source had no display name, and the
    # ranked lists are keyed on that spelling — so resolve BEFORE counting, or
    # one person ranks twice, once per spelling. The two detail tables are
    # fetched here for the same reason: one batch, not three.
    # The window keeps the ratings and the imports of the same days.
    in_window = "" if since is None else " WHERE {col} >= %s"
    window_args = None if since is None else (since,)
    low_rows = db.fetch_all(
        "SELECT * FROM p_metrics_rating_low_with_feedback"
        + in_window.format(col="rated_at") + " ORDER BY rated_at DESC", window_args)
    imports = db.fetch_all(
        "SELECT id, created_at, requester_slack_id, requester_name, target_alias, "
        "       database_name, table_name, is_new_table, status, row_count, "
        "       inserted_rows, byte_size, load_seconds "
        "  FROM p_metrics_csv_imports"
        + in_window.format(col="created_at") + " ORDER BY created_at DESC", window_args)

    def _who(r, name_col="requester_name", id_col="requester_slack_id"):
        return r.get(name_col) or r.get(id_col)

    name_of = people.namer(
        [_who(r) for r in rows]
        + [_who(r, "decided_by_name", "decided_by_slack_id") for r in rows]
        + [_who(r) for r in low_rows]
        + [_who(i) for i in imports])

    team_usage = _rank(Counter(r["team"] for r in rows if r["team"]))
    top_users = _rank(Counter(name_of(_who(r)) for r in rows), 10)
    admin_workload = _rank(Counter(
        name_of(_who(r, "decided_by_name", "decided_by_slack_id"))
        for r in rows if r["decided_by_slack_id"] and r["decided_by_slack_id"] != "AUTO"))
    target_usage = _rank(Counter(r["target_alias"] for r in rows if r["target_alias"]))

    # ---- ratings: weekly avg + count, response rate, low-rating feedback ----
    rw = defaultdict(list)
    resp: dict = defaultdict(lambda: {"rated": 0, "completed": 0})
    for r in rows:
        k = week_key(r)
        if not k:
            continue
        if r["rating"] is not None:
            rw[k].append(r["rating"])
        if r["status"] == "completed":
            resp[k]["completed"] += 1
            if r["rating"] is not None:
                resp[k]["rated"] += 1
    rating_weekly = [{"period": k, "avg": round(sum(rw[k]) / len(rw[k]), 2), "count": len(rw[k])}
                     for k in sorted(rw)]
    rating_response = [{"period": k, "rated": resp[k]["rated"], "completed": resp[k]["completed"],
                        "pct": round(100 * resp[k]["rated"] / resp[k]["completed"], 1) if resp[k]["completed"] else 0}
                       for k in sorted(resp)]
    rating_low = [{"user": name_of(_who(r)),
                   "rating": r.get("rating"), "feedback": r.get("feedback_text"),
                   "when": r["rated_at"].isoformat() if r.get("rated_at") else None}
                  for r in low_rows]

    # ---- CSV bulk imports ----
    imp_done = [i for i in imports if i["status"] == "completed"]
    imp_fail = [i for i in imports if i["status"] in ("failed", "rejected")]
    csv_summary = {
        "imports": len(imports),
        "completed": len(imp_done),
        "failed": len(imp_fail),
        "rowsLoaded": sum(i["inserted_rows"] or 0 for i in imp_done),
        "successRate": round(100 * len(imp_done) / (len(imp_done) + len(imp_fail)))
        if (imp_done or imp_fail) else None,
    }
    csv_rows = [{"id": i["id"], "when": i["created_at"].isoformat() if i["created_at"] else None,
                 "user": name_of(_who(i)),
                 "target": i["target_alias"], "db": i["database_name"], "table": i["table_name"],
                 "isNew": i["is_new_table"], "status": i["status"],
                 "rows": i["inserted_rows"] if i["inserted_rows"] is not None else i["row_count"],
                 "bytes": i["byte_size"], "loadSeconds": _f(i["load_seconds"])}
                for i in imports]

    # ---- who can what (org access reference) ----
    who = [dict(r) for r in db.fetch_all("SELECT * FROM p_metrics_who_can_what ORDER BY name")]

    vol_daily = _volume(day_key)
    vol_weekly = _volume(week_key)
    vol_monthly = _volume(month_key)
    avg_appr_min = round((sum(appr) / len(appr)) / 60, 1) if appr else 0

    return {
        # back-compat keys for the current MetricsView (kept until the view is
        # rewritten to the rich shape below — avoids a blank Metrics page mid-swap).
        "totalQueries": total,
        "autoApproveRate": headline["autoApproveRate"],
        "avgLatencyMin": avg_appr_min,
        "rejectRate": headline["rejectRate"],
        "perDay": [d["total"] for d in vol_daily][-14:],
        "tierBreakdown": {"RO": tier_totals.get("RO", 0), "RW": tier_totals.get("RW", 0), "DDL": tier_totals.get("DDL", 0)},
        "topSubmitters": [{"user": u["name"], "count": u["count"]} for u in top_users],
        "latencyByTier": {t: round((sla["byTier"].get(t, {}).get("p50") or 0) / 60, 1) for t in ("RO", "RW", "DDL")},
        "generatedAt": max((r["created_at"] for r in rows if r["created_at"]), default=None) and
                       max(r["created_at"] for r in rows if r["created_at"]).isoformat(),
        "reportStart": config.get("report_start_date"),
        "timezone": config.get("report_timezone") or "UTC",
        "headline": headline,
        "costSavings": cost,
        "volumeDaily": vol_daily,
        "volumeWeekly": vol_weekly,
        "volumeMonthly": vol_monthly,
        "approvalSla": sla,
        "failureWeekly": failure,
        "tierWeekly": tier_weekly,
        "tierTotals": {"RO": tier_totals.get("RO", 0), "RW": tier_totals.get("RW", 0), "DDL": tier_totals.get("DDL", 0)},
        "scheduledUsage": scheduled,
        "businessHours": business,
        "peakHours": peak,
        "teamUsage": team_usage,
        "topUsers": top_users,
        "adminWorkload": admin_workload,
        "targetUsage": target_usage,
        "ratingWeekly": rating_weekly,
        "ratingResponse": rating_response,
        "ratingLow": rating_low,
        "csvSummary": csv_summary,
        "csvImports": csv_rows,
        "whoCanWhat": who,
        # PLA-1592 U8. `window` echoes the parameter (null: all time), and
        # `windowStart` is the first moment it covers. `whoCanWhat` is the
        # access of today, so the window does not apply to it.
        "window": window,
        "windowStart": since.isoformat() if since else None,
        "decisionTime": decision_time(rows),
    }
