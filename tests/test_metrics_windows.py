"""GET /admin/metrics: a 7-day or 30-day window, and the time to a decision.

PLA-1592 U8. The Metrics screen switches between the last 7 and the last 30
days, and it shows how long a request waits for a person's decision, by server
and by team. Without `?window=` every existing key keeps the values it had.
`decisionTime` counts only the requests a PERSON decided. A waiver decides in
no time, and a withdrawal or an expiry also writes `decided_at`, so each of
those would pull the figure toward a wait that nobody had.
"""
from datetime import datetime, timedelta, timezone

import pytest

from queryhub.web import metrics
from queryhub.web import routes_admin as ra

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)
REQ = "U0EXAMPLE001"


def _row(sec, *, by="U0EXAMPLE002", status="completed", server="prod-main",
         team="Ledger Pod", requester=REQ):
    return {"id": 1, "created_at": T0, "status": status, "requester_slack_id": requester,
            "requester_name": "Jordan Ray", "team": team, "target_alias": server,
            "database_name": "app", "tier": "rw", "decided_by_slack_id": by,
            "decided_by_name": "Sam Archer", "approval_sec": sec, "exec_sec": 1.0,
            "hour_local": 10, "dow_local": 2, "rating": None, "scheduled_for": None,
            "auto_approved": by == "AUTO" if by else None, "decided_at": T0,
            "executed_at": T0, "completed_at": T0}


# ---- who counts --------------------------------------------------------------

def test_only_a_persons_decisions_count():
    rows = [
        _row(120.0),                                   # counts
        _row(0.0, by="AUTO"),                          # a waiver
        _row(5.0, by=REQ),                             # the requester decided
        _row(272.8, by=None, status="cancelled"),      # withdrawn or expired
        _row(900.0, status="cancelled"),               # approved, then cancelled
        _row(None),                                    # no decision yet
    ]
    dt = metrics.decision_time(rows)
    assert dt["requests"] == 1 and dt["p50Sec"] == 120.0


@pytest.mark.parametrize("status", sorted(metrics._DECIDED_BY_A_PERSON))
def test_every_decided_status_counts(status):
    assert metrics.decision_time([_row(60.0, status=status)])["requests"] == 1


@pytest.mark.parametrize("status", ["pending", "cancelled", "draft", "expired", ""])
def test_a_status_that_no_person_decided_does_not_count(status):
    assert metrics.decision_time([_row(60.0, status=status)])["requests"] == 0


# ---- the figures ----------------------------------------------------------------

def test_the_bands_are_the_ones_the_screen_draws():
    secs = [10.0, 299.9, 300.0, 899.9, 900.0, 3599.9, 3600.0, 86400.0]
    dt = metrics.decision_time([_row(s) for s in secs])
    assert dt["bands"] == [
        {"fromSec": 0, "toSec": 300, "count": 2},
        {"fromSec": 300, "toSec": 900, "count": 2},
        {"fromSec": 900, "toSec": 3600, "count": 2},
        {"fromSec": 3600, "toSec": None, "count": 2},
    ]
    assert sum(b["count"] for b in dt["bands"]) == dt["requests"] == 8


def test_percentiles_by_server_and_by_team():
    rows = [_row(60.0, server="prod-main", team="Ledger Pod"),
            _row(180.0, server="prod-main", team="Ledger Pod"),
            _row(600.0, server="prod-main", team="Risk Pod"),
            _row(1200.0, server="prod-edge", team="Risk Pod"),
            _row(30.0, server=None, team=None)]
    dt = metrics.decision_time(rows)
    assert (dt["requests"], dt["p50Sec"], dt["p90Sec"]) == (5, 180.0, 960.0)
    assert dt["byServer"] == [
        {"name": "prod-main", "requests": 3, "p50Sec": 180.0, "p90Sec": 516.0},
        {"name": "prod-edge", "requests": 1, "p50Sec": 1200.0, "p90Sec": 1200.0},
    ]
    # Same count: the names decide the order.
    assert dt["byTeam"] == [
        {"name": "Ledger Pod", "requests": 2, "p50Sec": 120.0, "p90Sec": 168.0},
        {"name": "Risk Pod", "requests": 2, "p50Sec": 900.0, "p90Sec": 1140.0},
    ]


def test_no_decisions_gives_zero_and_nulls():
    assert metrics.decision_time([]) == {
        "requests": 0, "p50Sec": None, "p90Sec": None,
        "bands": [{"fromSec": 0, "toSec": 300, "count": 0},
                  {"fromSec": 300, "toSec": 900, "count": 0},
                  {"fromSec": 900, "toSec": 3600, "count": 0},
                  {"fromSec": 3600, "toSec": None, "count": 0}],
        "byServer": [], "byTeam": []}


# ---- the window -------------------------------------------------------------------

def test_a_window_starts_that_many_days_back():
    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    assert metrics.window_start("7d", now) == now - timedelta(days=7)
    assert metrics.window_start("30d", now) == now - timedelta(days=30)
    assert metrics.window_start(None, now) is None
    with pytest.raises(ValueError):
        metrics.window_start("90d", now)


@pytest.fixture
def fake_db(monkeypatch):
    calls = []

    def fetch_all(sql, params=None):
        calls.append((" ".join(sql.split()), params))
        if "p_metrics_request_facts" in sql:
            return [_row(120.0), _row(0.0, by="AUTO")]
        return []
    monkeypatch.setattr(metrics.db, "fetch_all", fetch_all)
    monkeypatch.setattr(metrics.people, "display_names", lambda ids: {}, raising=False)
    return calls


def _sql_for(calls, view):
    return [(sql, p) for sql, p in calls if view in sql]


def test_without_a_window_the_reads_are_the_ones_before(fake_db):
    out = metrics.build_metrics()
    assert _sql_for(fake_db, "p_metrics_request_facts") == [
        ("SELECT * FROM p_metrics_request_facts", None)]
    assert _sql_for(fake_db, "p_metrics_rating_low_with_feedback") == [
        ("SELECT * FROM p_metrics_rating_low_with_feedback ORDER BY rated_at DESC", None)]
    imports = _sql_for(fake_db, "p_metrics_csv_imports")
    assert len(imports) == 1 and imports[0][1] is None and "WHERE" not in imports[0][0]
    assert out["window"] is None and out["windowStart"] is None
    assert out["totalQueries"] == 2 and out["decisionTime"]["requests"] == 1


@pytest.mark.parametrize("window,days", [("7d", 7), ("30d", 30)])
def test_a_window_reaches_every_dated_read(fake_db, window, days):
    before = datetime.now(timezone.utc)
    out = metrics.build_metrics(window)
    since = datetime.fromisoformat(out["windowStart"])
    assert before - timedelta(days=days, seconds=5) <= since <= before - timedelta(days=days) + timedelta(seconds=5)
    assert out["window"] == window
    for view, col in (("p_metrics_request_facts", "created_at"),
                      ("p_metrics_rating_low_with_feedback", "rated_at"),
                      ("p_metrics_csv_imports", "created_at")):
        (sql, params), = _sql_for(fake_db, view)
        assert f"WHERE {col} >= %s" in sql, view
        assert params == (since,), view
    # The access of today has no date: no window applies to it.
    (sql, params), = _sql_for(fake_db, "p_metrics_who_can_what")
    assert "WHERE" not in sql and params is None


def test_every_key_from_before_is_still_there(fake_db):
    keys = set(metrics.build_metrics("7d"))
    old = {"totalQueries", "autoApproveRate", "avgLatencyMin", "rejectRate", "perDay",
           "tierBreakdown", "topSubmitters", "latencyByTier", "generatedAt",
           "reportStart", "timezone", "headline", "costSavings", "volumeDaily",
           "volumeWeekly", "volumeMonthly", "approvalSla", "failureWeekly",
           "tierWeekly", "tierTotals", "scheduledUsage", "businessHours", "peakHours",
           "teamUsage", "topUsers", "adminWorkload", "targetUsage", "ratingWeekly",
           "ratingResponse", "ratingLow", "csvSummary", "csvImports", "whoCanWhat"}
    assert old <= keys
    assert keys - old == {"window", "windowStart", "decisionTime", "channelMix", "channelWeekly"}


# ---- the route ----------------------------------------------------------------------

@pytest.fixture
def route(monkeypatch):
    seen = []
    monkeypatch.setattr(ra.admin, "require_admin", lambda claims, need="review", **k: "U0DBA")
    monkeypatch.setattr(ra.metrics, "build_metrics", lambda window=None: seen.append(window) or {})
    return seen


@pytest.mark.parametrize("window", [None, "7d", "30d"])
def test_the_route_passes_the_window_on(route, window):
    ra.admin_metrics(window=window, claims={"sub": "U0DBA"})
    assert route == [window]


@pytest.mark.parametrize("window", ["", "7", "1y", "7D", "all"])
def test_the_route_refuses_any_other_window(route, window):
    with pytest.raises(Exception) as e:
        ra.admin_metrics(window=window, claims={"sub": "U0DBA"})
    assert e.value.status_code == 400 and route == []
