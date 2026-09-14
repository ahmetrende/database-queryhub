"""Operational metrics in Prometheus text-exposition format.

Distinct from `web/metrics.py`, which computes PRODUCT metrics for the admin
dashboard (how many queries per team, adoption over time). This module answers
the operator's questions: is the queue backing up, is anything stuck, is the
kill switch on, how long are approvals taking.

Two deliberate design choices.

**Derived from SQL at scrape time, not from in-process counters.** Counters held
in memory would be wrong here in three ways: they reset on restart, they differ
between the two processes (slackbot and queryhub-web both serve requests, so
neither has the whole picture), and they would have to be threaded through every
code path that changes state. The database already records every request with
its timestamps and status — it is the only place that knows the truth, and
reading it costs one round trip per scrape.

**No new dependency.** The text format is a documented, stable, line-oriented
protocol; `prometheus_client` exists mostly to manage an in-process registry,
which the choice above makes unnecessary. Rendering it directly keeps the
install footprint where it is, which matters for a tool people self-host.

Counters here are cumulative over all history rather than since process start,
which is exactly what Prometheus counters are supposed to be — `rate()` over a
monotonic series works the same either way, and it survives restarts.
"""
from __future__ import annotations

import logging

from .. import config as cfg
from .. import db
from . import build_info

log = logging.getLogger(__name__)


def _escape(value: str) -> str:
    """Escape a label VALUE per the exposition format."""
    return (str(value).replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n"))


class _Out:
    """Accumulates lines, emitting each metric's HELP/TYPE exactly once.

    Repeating a `# TYPE` line for the same metric name makes the payload
    invalid, which is easy to do when samples for one metric come from
    different queries.
    """

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._declared: set[str] = set()

    def metric(self, name: str, help_text: str, kind: str) -> None:
        if name in self._declared:
            return
        self._declared.add(name)
        self.lines.append(f"# HELP {name} {help_text}")
        self.lines.append(f"# TYPE {name} {kind}")

    def sample(self, name: str, value, labels: dict | None = None) -> None:
        if value is None:
            return
        if labels:
            rendered = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
            self.lines.append(f"{name}{{{rendered}}} {value}")
        else:
            self.lines.append(f"{name} {value}")

    def text(self) -> str:
        # The format requires a trailing newline.
        return "\n".join(self.lines) + "\n"


def _request_totals(out: _Out) -> None:
    """Cumulative request counts, split the three ways an operator asks about:
    outcome, where it came from, and which credential tier it needed."""
    out.metric("queryhub_requests_total",
               "Requests ever submitted, by status, origin and required tier.",
               "counter")
    rows = db.fetch_all(
        "SELECT status, COALESCE(origin, 'unknown') AS origin, "
        "       COALESCE(required_tier, 'unknown') AS tier, COUNT(*) AS n "
        # Drafts are reserved ids from open query tabs, not submitted requests;
        # counting them would make this gauge track browser tabs.
        "  FROM requests WHERE status <> 'draft' GROUP BY 1, 2, 3")
    for r in rows:
        out.sample("queryhub_requests_total", r["n"],
                   {"status": r["status"], "origin": r["origin"],
                    "tier": r["tier"]})


def _queue_state(out: _Out) -> None:
    """Current depth and, more usefully, AGE.

    Depth alone does not distinguish "three requests arrived this second" from
    "one request has been waiting since yesterday". The age of the oldest
    unapproved request is the number worth alerting on, because it is what a
    developer experiences as the tool being broken.
    """
    out.metric("queryhub_requests_in_state",
               "Requests currently in a non-terminal state.", "gauge")
    out.metric("queryhub_oldest_request_age_seconds",
               "Age of the oldest request in each non-terminal state.", "gauge")
    rows = db.fetch_all(
        "SELECT status, COUNT(*) AS n, "
        "       MAX(EXTRACT(EPOCH FROM (NOW() - created_at)))::bigint AS oldest "
        "  FROM requests "
        " WHERE status NOT IN ('completed','failed','rejected','cancelled',"
        "                       'draft') "
        " GROUP BY 1")
    seen = set()
    for r in rows:
        seen.add(r["status"])
        out.sample("queryhub_requests_in_state", r["n"], {"status": r["status"]})
        out.sample("queryhub_oldest_request_age_seconds", r["oldest"],
                   {"status": r["status"]})
    # Emit an explicit zero for the states we care about but that have no rows.
    # Without this the series disappears when the queue drains, and a
    # disappearing series is indistinguishable from a broken exporter — alerts
    # on `absent()` and on `> 0` both misfire.
    for state in ("pending", "approved", "executing", "scheduled"):
        if state not in seen:
            out.sample("queryhub_requests_in_state", 0, {"status": state})
            out.sample("queryhub_oldest_request_age_seconds", 0,
                       {"status": state})


def _durations(out: _Out) -> None:
    """Sum/count pairs rather than quantiles.

    `rate(sum) / rate(count)` gives the average over any window the operator
    picks, which is the idiomatic way to expose a duration without keeping
    per-bucket state in the process. Real quantiles would need histogram buckets
    maintained in memory — see the module docstring for why there is no memory
    here to keep them in.
    """
    out.metric("queryhub_approval_wait_seconds_sum",
               "Total time requests spent waiting for a decision.", "counter")
    out.metric("queryhub_approval_wait_seconds_count",
               "Number of requests that reached a decision.", "counter")
    out.metric("queryhub_execution_seconds_sum",
               "Total time spent executing approved queries.", "counter")
    out.metric("queryhub_execution_seconds_count",
               "Number of executions that finished.", "counter")

    row = db.fetch_one(
        "SELECT "
        "  COALESCE(SUM(EXTRACT(EPOCH FROM (decided_at - created_at))), 0)::bigint "
        "    AS wait_sum, "
        "  COUNT(decided_at) AS wait_count, "
        "  COALESCE(SUM(EXTRACT(EPOCH FROM (completed_at - executed_at))), 0)::bigint "
        "    AS exec_sum, "
        "  COUNT(completed_at) AS exec_count "
        "  FROM requests WHERE status <> 'draft'")
    if row:
        out.sample("queryhub_approval_wait_seconds_sum", row["wait_sum"])
        out.sample("queryhub_approval_wait_seconds_count", row["wait_count"])
        out.sample("queryhub_execution_seconds_sum", row["exec_sum"])
        out.sample("queryhub_execution_seconds_count", row["exec_count"])


def _rows_returned(out: _Out) -> None:
    out.metric("queryhub_result_rows_total",
               "Rows returned to users across all completed queries.", "counter")
    row = db.fetch_one(
        "SELECT COALESCE(SUM(row_count), 0)::bigint AS n FROM requests "
        " WHERE status <> 'draft'")
    if row:
        out.sample("queryhub_result_rows_total", row["n"])

    out.metric("queryhub_results_truncated_total",
               "Completed queries whose result hit the row limit.", "counter")
    row = db.fetch_one(
        "SELECT COUNT(*) AS n FROM requests "
        " WHERE truncated AND status <> 'draft'")
    if row:
        out.sample("queryhub_results_truncated_total", row["n"])


def _fleet(out: _Out) -> None:
    out.metric("queryhub_targets", "Configured target databases.", "gauge")
    rows = db.fetch_all(
        "SELECT enabled, COUNT(*) AS n FROM target_servers GROUP BY 1")
    for r in rows:
        out.sample("queryhub_targets", r["n"],
                   {"enabled": "true" if r["enabled"] else "false"})

    out.metric("queryhub_people", "People who can use QueryHub.", "gauge")
    for label, sql in (
        ("requesters", "SELECT COUNT(*) AS n FROM requesters WHERE enabled"),
        ("admins", "SELECT COUNT(*) AS n FROM admins WHERE enabled"),
    ):
        row = db.fetch_one(sql)
        if row:
            out.sample("queryhub_people", row["n"], {"role": label})


def _control_plane(out: _Out) -> None:
    """Things that silently stop the system, and the backlog that proves a
    background worker is alive.

    The outbox depth earns its place: the auth-event poller only ran in the
    Slack process, so a vanilla install appended a row on every grant change
    and never drained it. Nothing surfaced that — the table just grew. A gauge
    here turns that class of failure into an alert instead of an archaeology
    exercise.
    """
    out.metric("queryhub_kill_switch_active",
               "1 when the kill switch is halting all new query traffic.",
               "gauge")
    val = (cfg.get_setting("kill_switch", "off") or "").strip().lower()
    out.sample("queryhub_kill_switch_active",
               1 if val in {"on", "1", "true", "yes"} else 0)

    out.metric("queryhub_auth_event_outbox_depth",
               "Unprocessed rows in the authorization-change outbox. A number "
               "that only grows means the poller is not running.", "gauge")
    row = db.fetch_one(
        "SELECT COUNT(*) AS n FROM auth_event_outbox WHERE processed_at IS NULL")
    if row:
        out.sample("queryhub_auth_event_outbox_depth", row["n"])

    out.metric("queryhub_active_grants", "Active grants, by kind.", "gauge")
    for label, sql in (
        ("user", "SELECT COUNT(*) AS n FROM user_target_grants "
                 "WHERE revoked_at IS NULL"),
        ("team", "SELECT COUNT(*) AS n FROM team_target_grants"),
    ):
        row = db.fetch_one(sql)
        if row:
            out.sample("queryhub_active_grants", row["n"], {"kind": label})


def _build(out: _Out) -> None:
    """The standard info-metric idiom: a constant 1 carrying labels, so a
    dashboard can annotate "this changed right after that deploy"."""
    out.metric("queryhub_build_info",
               "Build identity of the process serving this endpoint.", "gauge")
    try:
        b = build_info.build()
    except Exception:
        b = {}
    out.sample("queryhub_build_info", 1, {
        "version": b.get("version", "unknown"),
        "sha": b.get("sha", "unknown"),
    })


# Each collector is independent, so one failing query does not cost the whole
# scrape. A partial payload is far more useful than a 500 — especially when the
# reason for scraping is that something is already wrong.
_COLLECTORS = (
    _build, _queue_state, _request_totals, _durations, _rows_returned,
    _fleet, _control_plane,
)


def render() -> str:
    out = _Out()
    out.metric("queryhub_scrape_errors",
               "Collectors that failed during this scrape.", "gauge")
    errors = 0
    for collect in _COLLECTORS:
        try:
            collect(out)
        except Exception:
            errors += 1
            log.warning("metrics collector %s failed", collect.__name__,
                        exc_info=True)
    # Reported rather than hidden: a dashboard showing zeros because the queries
    # are broken looks exactly like a healthy idle system.
    out.sample("queryhub_scrape_errors", errors)
    return out.text()
