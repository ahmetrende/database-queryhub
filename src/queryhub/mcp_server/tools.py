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
from pathlib import Path

from .. import core_submit, db, origins, query_safety, targets, teams
from . import caller, policy

log = logging.getLogger(__name__)

#: Rows one call may return. A protocol response is read into an assistant's
#: context, so an unbounded page is a way to spend somebody's whole context on
#: one query by accident.
MAX_ROWS = 200


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


def describe_database(connection: str, database: str) -> dict:
    """Tables and columns, from QueryHub's own catalog.

    Read from the stored snapshot rather than the live server: describing a
    schema should not open a production connection, and the snapshot is what
    every other surface autocompletes from, so an assistant and a person see
    the same shape.
    """
    uid = _me()
    t = _target_or_refuse(uid, connection)
    rows = db.fetch_all(
        "SELECT st.schema_name, st.table_name, st.relkind, "
        "       sc.column_name, sc.data_type, sc.is_pk "
        "  FROM schema_tables st "
        "  JOIN schema_columns sc ON sc.table_id = st.id "
        " WHERE st.target_server_id = %s AND st.database_name = %s "
        " ORDER BY st.schema_name, st.table_name, sc.column_name",
        (t.id, database))
    if not rows:
        raise ToolError(
            "no_catalog",
            f"No catalogued schema for '{database}' on '{connection}'. It may "
            f"not be a database you hold, or its first snapshot has not run.")
    tables: dict[tuple, dict] = {}
    for r in rows:
        key = (r["schema_name"], r["table_name"])
        e = tables.setdefault(key, {"schema": r["schema_name"],
                                    "table": r["table_name"],
                                    "kind": r["relkind"], "columns": []})
        e["columns"].append({"name": r["column_name"], "type": r["data_type"],
                             "pk": bool(r["is_pk"])})
    return {"connection": t.alias, "database": database,
            "tables": list(tables.values())}


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
                 justification: str | None = None) -> dict:
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
    return {
        "requestId": rid,
        "requiredTier": required.upper(),
        "decision": "auto_approved" if outcome.auto_approved else "needs_approval",
        "status": outcome.row.get("status"),
        "note": (None if outcome.auto_approved else
                 "A DBA has been notified. Poll query_status for the outcome."),
    }


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
