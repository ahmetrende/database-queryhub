"""The work behind each MCP tool, as plain functions.

No SDK here on purpose. `server` is a thin adapter that registers these, so
the package imports and its tests run without `queryhub[mcp]` installed, and
the optional dependency is optional in fact rather than in the README. It also
means these can be exercised directly, which is how the tests below reach the
authorization decisions without standing up a protocol.

Every function resolves its own caller through `caller.acting_principal()` and
none of them takes a "who" argument. See this package's docstring for why that
is the shape rather than a preference.
"""
from __future__ import annotations

import csv
import logging
import time
from pathlib import Path

from .. import core_submit, db, origins, query_safety, targets, teams
from . import caller, policy

log = logging.getLogger(__name__)

#: Rows one call may return. A protocol response is read into an assistant's
#: context, so an unbounded page is a way to spend somebody's whole context on
#: one query by accident.
MAX_ROWS = 200

#: Table names one listing may return. The largest catalogue here holds
#: 2,727 tables; all of them, with columns, is 2.6 MB of JSON.
MAX_TABLES = 300

#: Tables one filtered call may describe in full. A loose filter still
#: matches hundreds, and each carries every column.
MAX_DETAIL_TABLES = 25

#: How long `submit_query` will wait for a result before handing back an
#: id to poll. Under the timeout an MCP client typically applies, so the
#: wait ends in an answer rather than in the client giving up.
DEFAULT_WAIT_SECONDS = 25
MAX_WAIT_SECONDS = 60

#: Rows returned inline when the wait pays off. Small: the point is to
#: answer in one round trip, not to move the whole result into a context.
INLINE_ROWS = 20


class ToolError(Exception):
    """A refusal the caller can act on. `code` is short and log-safe."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _require_enabled() -> None:
    if not policy.enabled():
        raise ToolError("disabled",
                        "The QueryHub MCP surface is switched off. An operator "
                        "enables it with the `mcp_enabled` setting.")


def _me() -> str:
    _require_enabled()
    try:
        return caller.acting_principal()
    except caller.CallerError as e:
        raise ToolError(e.code, e.message) from e


def _target_or_refuse(uid: str, connection: str):
    """Resolve an alias the caller named, and refuse it if they hold no grant.

    Unknown and ungranted are the same answer, which is the rule the web door
    already applies: distinguishing them lets a caller enumerate real server
    names by watching which refusal comes back.
    """
    t = targets.by_alias(connection)
    if t is not None and teams.effective_grant_for_user(uid, t.id) is None:
        t = None
    if t is None:
        raise ToolError("unknown_connection",
                        f"No connection named '{connection}' that you can use.")
    return t


def _classify(sql: str, engine: str) -> tuple[str, list[str]]:
    """The tier this statement needs, and any blockers.

    `query_safety.required_mode` is NOT usable as an authorization input and
    says so in its own docstring: a BLOCKED statement -- an `UPDATE` with no
    `WHERE`, a mixed-tier script, an unparseable fragment -- also reports `ro`,
    so a ceiling compared against it waves the dangerous ones through as
    read-only. It is a display label.

    Caught here before this shipped: the first version of the ceiling did
    exactly that, and `update t set a = 1` came back as `ro` and passed. So
    this asks `analyze()` and reports the blockers, which is the ordering
    `tests/test_required_mode_ordering.py` pins for every other caller.
    """
    rep = query_safety.analyze(sql or "", engine=engine)
    return rep.main_tier, list(rep.blockers or [])


# --- discovery ---------------------------------------------------------------


def list_connections() -> dict:
    """Every database this caller may query, and at what tier."""
    uid = _me()
    out = []
    for t in targets.list_enabled():
        grant = teams.effective_grant_for_user(uid, t.id)
        if grant is None:
            continue
        allowed = grant["allowed_databases"]
        out.append({
            "connection": t.alias,
            "engine": t.engine,
            "tier": (grant["mode"] or "ro").upper(),
            "databases": sorted(allowed) if allowed is not None else None,
            "allDatabases": allowed is None,
        })
    return {"connections": out, "maxTier": policy.max_tier().upper()}


def describe_database(connection: str, database: str,
                      table: str | None = None) -> dict:
    """What is in a database. Two questions, two answers.

    Without `table` this lists table NAMES. With one it returns the columns of
    the tables whose name contains it. That split is not tidiness: asked for
    everything, the largest catalogued database here answers with 2,727 tables
    and 2.6 MB of JSON, which lands in the caller's context in one go and
    leaves no room for the work it was fetched for. Names alone are ~30x
    smaller, and they are what an assistant actually needs first -- it asks
    what exists, then asks about one thing.

    Both halves are capped and say so when they truncate. A cap that stays
    quiet reads as "that is all there is", which is worse than a short answer.
    """
    uid = _me()
    t = _target_or_refuse(uid, connection)
    want = (table or "").strip().lower()

    if not want:
        rows = db.fetch_all(
            "SELECT st.schema_name, st.table_name, st.relkind, "
            "       COUNT(sc.id) AS columns "
            "  FROM schema_tables st "
            "  LEFT JOIN schema_columns sc ON sc.table_id = st.id "
            " WHERE st.target_server_id = %s AND st.database_name = %s "
            " GROUP BY st.schema_name, st.table_name, st.relkind "
            " ORDER BY st.schema_name, st.table_name "
            " LIMIT %s", (t.id, database, MAX_TABLES + 1))
        if not rows:
            raise ToolError(
                "no_catalog",
                f"No catalogued schema for '{database}' on '{connection}'. It "
                f"may not be a database you hold, or its first snapshot has "
                f"not run.")
        more = len(rows) > MAX_TABLES
        rows = rows[:MAX_TABLES]
        return {
            "connection": t.alias, "database": database,
            "tables": [{"schema": r["schema_name"], "table": r["table_name"],
                        "kind": r["relkind"], "columns": r["columns"]}
                       for r in rows],
            "truncated": more,
            "note": (f"Showing the first {MAX_TABLES} tables. Pass `table` with "
                     f"part of a name to see columns."
                     if more else
                     "Pass `table` with part of a name to see its columns."),
        }

    rows = db.fetch_all(
        "SELECT st.schema_name, st.table_name, st.relkind, "
        "       sc.column_name, sc.data_type, sc.is_pk "
        "  FROM schema_tables st "
        "  JOIN schema_columns sc ON sc.table_id = st.id "
        " WHERE st.target_server_id = %s AND st.database_name = %s "
        "   AND lower(st.table_name) LIKE %s "
        " ORDER BY st.schema_name, st.table_name, sc.column_name",
        (t.id, database, f"%{want}%"))
    if not rows:
        raise ToolError(
            "no_match",
            f"No catalogued table matching '{table}' in '{database}' on "
            f"'{connection}'. Call this without `table` to see what is there.")
    tables: dict[tuple, dict] = {}
    for r in rows:
        key = (r["schema_name"], r["table_name"])
        if key not in tables and len(tables) >= MAX_DETAIL_TABLES:
            continue          # a loose filter can still match hundreds
        e = tables.setdefault(key, {"schema": r["schema_name"],
                                    "table": r["table_name"],
                                    "kind": r["relkind"], "columns": []})
        e["columns"].append({"name": r["column_name"], "type": r["data_type"],
                             "pk": bool(r["is_pk"])})
    matched = len({(r["schema_name"], r["table_name"]) for r in rows})
    return {
        "connection": t.alias, "database": database, "match": table,
        "tables": list(tables.values()),
        "truncated": matched > len(tables),
        "note": (f"{matched} tables matched; showing {len(tables)}. Narrow the "
                 f"`table` filter." if matched > len(tables) else None),
    }


def classify_sql(connection: str, sql: str) -> dict:
    """What tier this statement needs, and whether this door will take it.

    Offered so an assistant can find out BEFORE submitting, rather than
    learning it from a refusal. Classified with the target's own engine, since
    a statement the Postgres parser reads as read-only is not necessarily one
    on SQL Server.
    """
    uid = _me()
    t = _target_or_refuse(uid, connection)
    required, blockers = _classify(sql, t.engine)
    if blockers:
        return {"requiredTier": None, "accepted": False,
                "maxTier": policy.max_tier().upper(),
                "blocked": True, "reason": "; ".join(blockers)}
    ok = policy.tier_allowed(required)
    return {"requiredTier": required.upper(), "accepted": ok,
            "maxTier": policy.max_tier().upper(), "blocked": False,
            "reason": None if ok else policy.refusal(required)}


# --- submitting --------------------------------------------------------------


def submit_query(connection: str, database: str | None, sql: str,
                 justification: str | None = None,
                 wait_seconds: int = DEFAULT_WAIT_SECONDS) -> dict:
    """Submit a statement. Governed exactly as a Slack or web submit is.

    The tier ceiling is checked here, before `validate_submission`, so a
    refusal costs nothing and reads as a policy answer rather than a
    validation failure. It is checked with the target's engine for the same
    reason `classify_sql` is.

    `unmasked` is not a parameter and never will be. It is refused for anyone
    who is not a super-admin and re-checked at execution, but a door for
    programs should not be able to ask the question at all -- the argument
    that reaches this function is the one an assistant can be talked into
    setting.
    """
    uid = _me()
    t = _target_or_refuse(uid, connection)

    required, blockers = _classify(sql, t.engine)
    if blockers:
        # Refused here as well as by `validate_submission`, which would also
        # catch it. The point is that the CEILING must not be the thing that
        # let it past: a blocked statement reports `ro`, so a ro ceiling would
        # have said yes to it.
        raise ToolError("blocked", "; ".join(blockers))
    if not policy.tier_allowed(required):
        raise ToolError("tier_refused", policy.refusal(required))

    prep = core_submit.validate_submission(
        uid,
        caller.display_name(uid),
        target_server_id=t.id,
        database_name=database,
        query=sql,
        justification=justification,
        wants_result=True,
        result_format="csv",
        origin=origins.MCP,
        confirmed=False,
        unmasked=False,
    )
    if isinstance(prep, core_submit.Rejection):
        raise ToolError(prep.reason or prep.field, prep.message)

    outcome = core_submit.create_request(prep)
    if isinstance(outcome, core_submit.Rejection):
        raise ToolError(outcome.reason or outcome.field, outcome.message)

    # Same fan-out the web submit does: admins are told, and the executor is
    # dispatched for an auto-approved request. `dm_requester=False` because the
    # caller is a program that gets its answer from `query_status`, not a DM.
    core_submit.dispatch_and_notify(None, prep, outcome, dm_requester=False)

    rid = outcome.row["id"]
    out = {
        "requestId": rid,
        "requiredTier": required.upper(),
        "decision": "auto_approved" if outcome.auto_approved else "needs_approval",
        "status": outcome.row.get("status"),
    }
    if not outcome.auto_approved:
        # Waiting here would burn the whole budget and still return nothing: a
        # human approval is minutes, not seconds.
        out["note"] = ("A DBA has been notified. Poll query_status for the "
                       "outcome; approval is a human step and takes minutes.")
        return out

    wait = max(0, min(int(wait_seconds), MAX_WAIT_SECONDS))
    if wait:
        final = _await_result(rid, wait)
        out.update(final)
    else:
        out["note"] = "Poll query_status for the outcome."
    return out


def _await_result(request_id: int, seconds: int) -> dict:
    """Block until the request settles, or until the budget runs out.

    Measured on this fleet: a trivial auto-approved statement takes about 1.4
    seconds end to end, most of it two fresh connections to the target -- one
    for the pre-flight plan, one to run it. That is under a second of real
    work and over a second of connecting, and the caller should not have to
    spend a round trip discovering it finished.

    Polling belongs here rather than in the caller: an assistant asked to poll
    picks its own interval, and whatever it picks is added to a wait that was
    already over. One call in, one answer out.

    This blocks the server for the duration, which is fine while the transport
    is stdio and serves one caller. An HTTP transport serving several will need
    this to run per-connection rather than per-process; the deadline is bounded
    for that reason.
    """
    deadline = time.monotonic() + seconds
    delay = 0.15
    while True:
        row = db.fetch_one(
            "SELECT status, row_count, error_message, csv_file_path "
            "  FROM requests WHERE id = %s", (request_id,))
        status = (row or {}).get("status")
        if status in ("completed", "failed", "rejected", "cancelled"):
            out = {"status": status, "rowCount": row["row_count"],
                   "error": row["error_message"]}
            if status == "completed" and row["csv_file_path"]:
                try:
                    page = fetch_result(request_id, 0, INLINE_ROWS)
                    out["columns"] = page.get("columns")
                    out["rows"] = page.get("rows")
                    out["moreRows"] = page.get("more")
                except ToolError as e:
                    # The result exists but could not be read -- say which,
                    # rather than reporting the request as unfinished.
                    out["resultError"] = e.message
            return out
        if time.monotonic() >= deadline:
            return {"status": status,
                    "note": (f"Still {status} after {seconds}s. Poll "
                             f"query_status; the work continues either way.")}
        time.sleep(delay)
        delay = min(delay * 1.5, 1.0)   # tight at first, then back off


# --- reading back ------------------------------------------------------------


def _own(request_id: int, uid: str) -> dict:
    row = db.fetch_one(
        "SELECT id, status, requester_slack_id, target_server_id, "
        "       database_name, query, row_count, error_message, "
        "       csv_file_path, created_at, completed_at "
        "  FROM requests WHERE id = %s", (request_id,))
    # Not found and not yours are one answer, so a caller cannot count other
    # people's requests by watching which refusal comes back.
    if row is None or row["requester_slack_id"] != uid:
        raise ToolError("not_found", f"No query {request_id} of yours.")
    return row


def query_status(request_id: int) -> dict:
    """Where one submission has got to."""
    uid = _me()
    row = _own(int(request_id), uid)
    return {
        "requestId": row["id"],
        "status": row["status"],
        "rowCount": row["row_count"],
        "error": row["error_message"],
        "hasResult": bool(row["csv_file_path"]),
        "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
        "completedAt": (row["completed_at"].isoformat()
                        if row["completed_at"] else None),
    }


def fetch_result(request_id: int, offset: int = 0, limit: int = 50) -> dict:
    """A page of a completed result, read from the stored masked CSV.

    The stored file is the masked one -- the executor writes it that way -- so
    reading it here cannot expose what a person reading the same result would
    not see. That is the property that makes this tool safe to hand to a
    program, and it holds because this reads the artefact rather than
    re-running the query.
    """
    uid = _me()
    row = _own(int(request_id), uid)
    if row["status"] != "completed":
        raise ToolError("not_ready",
                        f"Query {row['id']} is {row['status']}, not completed.")
    if not row["csv_file_path"]:
        return {"requestId": row["id"], "kind": "affected",
                "affected": row["row_count"], "rows": [], "columns": []}
    p = Path(row["csv_file_path"])
    if not p.is_file():
        raise ToolError("expired",
                        "That result has been purged; results are kept for a "
                        "limited time. Submit the query again.")
    if p.suffix.lower() == ".zip":
        raise ToolError(
            "multi_statement",
            "That request stored one result per statement. Reading those "
            "through MCP is not supported yet; use the web UI.")

    offset = max(0, int(offset))
    limit = max(1, min(int(limit), MAX_ROWS))
    out: list[dict] = []
    with p.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            return {"requestId": row["id"], "kind": "table",
                    "columns": [], "rows": [], "offset": offset, "more": False}
        for i, rec in enumerate(reader):
            if i < offset:
                continue
            if len(out) >= limit:
                return {"requestId": row["id"], "kind": "table",
                        "columns": header, "rows": out,
                        "offset": offset, "more": True}
            out.append(dict(zip(header, rec)))
    return {"requestId": row["id"], "kind": "table", "columns": header,
            "rows": out, "offset": offset, "more": False}
