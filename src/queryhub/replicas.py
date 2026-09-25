"""Read replicas: where a read-only request actually runs.

A target can be the read replica of another (`target_servers.replica_of`,
migration 131; import_targets_from_inventory.py links it from the inventory's
`replica_source`). Nobody picks a replica: every list shows the primary's one
name, and the request, its grant and its history all stay on the primary. At
run time a read-only request on the primary goes to an enabled replica instead
-- when the replica is healthy -- and to the primary otherwise.

What decides it, in this order:

1. `replica_routing` is 'on' (bot_config, runtime; the kill switch).
2. Postgres, and the request runs at the RO tier.
3. The SQL reads nothing that describes the server it runs on (pg_stat_*,
   pg_locks, WAL positions, ...): on a replica those answer about the replica,
   and the person asking about locks or sessions meant the primary.
4. The requester has not run an RW/DDL request on this target in the last
   `replica_read_your_writes_minutes`, so a SELECT that checks an UPDATE the
   same person just ran reads it back, instead of a replica that may not have
   it yet.
5. The replica is healthy: still in recovery, and no more than
   `replica_max_lag_seconds` behind -- measured against the primary's current
   WAL position, because the age of the last replayed commit alone reads a
   quiet primary as a lagging replica. One check is trusted for
   `replica_health_ttl_seconds`, per process.

The login is the primary's: a physical replica carries the primary's roles and
passwords (measured on all five pairs, 2026-09-23), so a replica row needs no
credential of its own.

If the replica fails the query for a reason that is about the replica -- it
cannot be reached, or it cancelled the statement to keep replaying (a conflict
with recovery) -- the executor runs it again on the primary. A read has no side
effects, so running it twice is safe; a timeout or a user's cancel is not
retried.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass

import psycopg

from . import config as cfg
from . import db, query_safety

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT_SEC = 3
_PROBE_TIMEOUT_MS = 3000

# Functions and views whose answer is about the server they run on. Matched
# against the statement with its comments and string literals blanked out.
_NODE_LOCAL = re.compile(
    r"\b(pg_stat\w*|pg_locks|pg_blocking_pids|pg_backend_pid|pg_replication\w*|"
    r"pg_prepared_xacts|pg_current_\w+|pg_last_\w+|pg_wal\w*|pg_is_in_recovery|"
    r"pg_is_wal_replay_paused|pg_get_wal\w*|txid_\w+|pg_snapshot\w*|"
    r"pg_control_\w+|pg_postmaster_start_time|pg_conf_load_time|pg_ls_\w+|"
    r"pg_read_file|pg_buffercache\w*|inet_server_\w+)\b",
    re.IGNORECASE)

# A replica that stops answering, or is shutting down, gives no sqlstate or one
# of these. Anything else would fail on the primary the same way.
_UNREACHABLE_SQLSTATES = {"57P01", "57P02", "57P03",
                          "08000", "08001", "08003", "08004", "08006"}


@dataclass(frozen=True)
class Route:
    """The replica a request runs on."""
    target_id: int
    alias: str
    host: str
    port: int
    lag_s: float


@dataclass(frozen=True)
class Decision:
    """`route` when a replica serves the request. `skipped` says why not, when
    the target HAS a replica and routing is on -- the only case where "why did
    this run on the primary" is a question worth answering in the audit row."""
    route: Route | None = None
    skipped: str | None = None


@dataclass(frozen=True)
class Health:
    ok: bool
    lag_s: float | None
    reason: str | None = None


_NONE = Decision()
_lock = threading.Lock()
_health: dict[int, tuple[float, Health]] = {}


def enabled() -> bool:
    return (cfg.get_setting("replica_routing", "off") or "off").strip().lower() == "on"


def node_local(sql: str) -> str | None:
    """The first node-local name `sql` reads, or None."""
    m = _NODE_LOCAL.search(query_safety.code_text(sql or ""))
    return m.group(1) if m else None


def replicas_of(primary_id: int) -> list[dict]:
    return db.fetch_all(
        "SELECT id, alias, host, port FROM target_servers "
        " WHERE replica_of = %s AND enabled ORDER BY id", (primary_id,))


def _wrote_recently(requester: str, target_id: int, minutes: int) -> bool:
    return db.fetch_one(
        "SELECT 1 AS hit FROM requests "
        " WHERE requester_slack_id = %s AND target_server_id = %s "
        "   AND executed_tier IN ('rw', 'ddl') "
        "   AND executed_at > NOW() - make_interval(mins => %s) LIMIT 1",
        (requester, target_id, minutes)) is not None


def _probe(primary, replica: dict, user: str, password: str) -> Health:
    """Measure one replica. The primary is read FIRST: a replica that has
    replayed past that position by the time it is asked is caught up."""
    # TLS is decided per host: a replica can live under a different name, or a
    # different cloud's certificate, than its primary.
    kw = dict(dbname=primary.default_database, user=user, password=password,
              connect_timeout=_CONNECT_TIMEOUT_SEC, autocommit=True,
              application_name="queryhub:replica-health",
              options=f"-c statement_timeout={_PROBE_TIMEOUT_MS}")
    try:
        with psycopg.connect(host=primary.host, port=primary.port, **kw,
                             **cfg.target_ssl_kwargs(primary.host)) as pc, \
                pc.cursor() as cur:
            cur.execute("SELECT pg_current_wal_lsn()::text")
            primary_lsn = cur.fetchone()[0]
        with psycopg.connect(host=replica["host"], port=replica["port"], **kw,
                             **cfg.target_ssl_kwargs(replica["host"])) as rc, \
                rc.cursor() as cur:
            cur.execute(
                "SELECT pg_is_in_recovery(), "
                "       pg_wal_lsn_diff(%s::pg_lsn, pg_last_wal_replay_lsn()), "
                "       EXTRACT(EPOCH FROM now() - pg_last_xact_replay_timestamp())",
                (primary_lsn,))
            in_recovery, behind, age = cur.fetchone()
    except Exception as e:  # noqa: BLE001 -- any failure means "do not use it"
        log.info("replica %s: health check failed: %s", replica["alias"],
                 type(e).__name__)
        return Health(False, None, f"unreachable ({type(e).__name__})")
    if not in_recovery:
        return Health(False, None, "not in recovery")
    if behind is not None and behind <= 0:
        lag = 0.0
    elif age is None:
        return Health(False, None, "lag unknown")
    else:
        lag = max(0.0, float(age))
    limit = cfg.get_int("replica_max_lag_seconds", 10)
    if lag > limit:
        return Health(False, lag, f"{lag:.0f}s behind (limit {limit}s)")
    return Health(True, lag)


def health(primary, replica: dict, user: str, password: str) -> Health:
    ttl = cfg.get_int("replica_health_ttl_seconds", 15)
    now = time.monotonic()
    with _lock:
        hit = _health.get(replica["id"])
    if hit and now - hit[0] < ttl:
        return hit[1]
    h = _probe(primary, replica, user, password)
    with _lock:
        _health[replica["id"]] = (time.monotonic(), h)
    return h


def mark_unhealthy(replica_id: int, reason: str) -> None:
    """A replica that just failed a query is not asked again until its check
    expires."""
    with _lock:
        _health[replica_id] = (time.monotonic(), Health(False, None, reason))


def choose(target, request: dict, mode: str, user: str, password: str) -> Decision:
    """Where `request` runs: a Route to a replica, or the primary.

    Never raises. Routing is an optimisation, so a failure to decide it -- the
    bot DB blinking, a malformed row -- sends the request to the primary, where
    it would have run anyway.
    """
    if (getattr(target, "engine", "postgres") or "postgres") != "postgres" or mode != "ro":
        return _NONE
    try:
        if not enabled():
            return _NONE
        return _choose(target, request, user, password)
    except Exception:  # noqa: BLE001
        log.exception("request %s: replica routing failed; running on the primary",
                      request.get("id"))
        return _NONE


def _choose(target, request: dict, user: str, password: str) -> Decision:
    candidates = replicas_of(target.id)
    if not candidates:
        return _NONE
    local = node_local(request.get("query") or "")
    if local:
        return Decision(skipped=f"reads {local}, which describes the server it runs on")
    minutes = cfg.get_int("replica_read_your_writes_minutes", 5)
    if minutes > 0 and _wrote_recently(request["requester_slack_id"], target.id, minutes):
        return Decision(skipped=f"the requester wrote to it in the last {minutes} min")
    reasons = []
    for r in candidates:
        h = health(target, r, user, password)
        if h.ok:
            return Decision(route=Route(r["id"], r["alias"], r["host"], r["port"],
                                        round(h.lag_s or 0.0, 1)))
        reasons.append(f"{r['alias']}: {h.reason}")
    return Decision(skipped="; ".join(reasons))


def served_note(lag_s: float | None) -> str:
    """What the requester is told when a read replica ran their query: the
    Messages tab on the web, one line in the Slack result. No replica name --
    people pick one connection, and that is the name they know it by."""
    if lag_s is None or lag_s < 0.05:
        return "Ran on a read replica, caught up with the primary."
    return f"Ran on a read replica, about {lag_s:.1f} s behind the primary."


def is_fallback_error(e: BaseException) -> bool:
    """Whether a failure on a replica is about the replica, so the primary
    should run the query instead."""
    if isinstance(e, psycopg.errors.QueryCanceled):
        return False                    # a timeout or a cancel: not retried
    state = getattr(e, "sqlstate", None)
    if state == "40001":
        return "conflict with recovery" in str(e).lower()
    if state is None:
        return isinstance(e, psycopg.OperationalError)
    return state in _UNREACHABLE_SQLSTATES
