"""The work behind each MCP tool, as plain functions.

No SDK here on purpose. `catalog.call` runs these for the `POST /mcp` door
(`web/routes_mcp.py`), which speaks the protocol itself. The tests can call
them directly, so they reach the authorization decisions without standing up
a protocol.

Every function resolves its own caller through `caller.acting_principal()` and
none of them takes a "who" argument. See this package's docstring for why that
is the shape rather than a preference.
"""
from __future__ import annotations

import csv
import functools
import logging
import time
from pathlib import Path

from .. import (admins, auto_approve, core_submit, db, db_scope, engines, errors, origins,
                query_safety, targets, teams)
from . import caller, catalog, policy

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
#: id to poll: the default and the maximum. Under the time limits of the
#: portal, so the wait ends in an answer rather than in the portal giving up.
#: See `catalog.WAIT_SECONDS`.
DEFAULT_WAIT_SECONDS = catalog.WAIT_SECONDS
MAX_WAIT_SECONDS = catalog.WAIT_SECONDS

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
                        "Refused: the QueryHub MCP server is off. To enable it, "
                        "an operator sets `mcp_door_enabled` to `on`.")


def _me() -> str:
    _require_enabled()
    try:
        return caller.acting_principal()
    except caller.CallerError as e:
        raise ToolError(e.code, e.message) from e


def _granted(uid: str, connection: str):
    """Resolve an alias the caller named, with the caller's grant on it.

    Refuses it if they hold no grant. Unknown and ungranted are the same
    answer, which is the rule the web door already applies: distinguishing
    them lets a caller enumerate real server names by watching which refusal
    comes back.
    """
    t = targets.by_alias(connection)
    grant = teams.effective_grant_for_user(uid, t.id) if t is not None else None
    if t is None or grant is None:
        raise ToolError("unknown_connection",
                        f"There is no connection named '{connection}' that you "
                        f"can use. Call {catalog.named('list_connections')} to see "
                        f"your connections.")
    return t, grant


def _target_or_refuse(uid: str, connection: str):
    """`_granted`, for a caller that needs only the target."""
    return _granted(uid, connection)[0]


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
    """Every database this caller may query, and at what tier.

    The list is the one the web UI shows (`db_scope.visible_databases`): the
    same databases for the same person. The names are always listed, also for
    an unrestricted grant, so an assistant never has to guess them.
    """
    uid = _me()
    # Except a super-admin, nobody is shown more than RO on an Athena archive
    # (engines.shown_tier); asked only if an archive row would change.
    is_super = functools.cache(lambda: admins.is_super_admin(uid))
    # A read replica is not a connection of its own (replicas.py).
    fleet = [t for t in targets.list_enabled()
             if getattr(t, "replica_of", None) is None]
    grants = teams.effective_grants_for_user(uid, [t.id for t in fleet])
    granted = [(t, g) for t in fleet if (g := grants.get(t.id)) is not None]
    # Not `catalog`: that name is the module that names the tools.
    catalogued = db_scope.catalog_databases_map([t.id for t, _g in granted])
    out = []
    for t, grant in granted:
        names = db_scope.visible_databases(t, grant, catalogued.get(t.id, []))
        if not names:
            continue
        server_tier = grant["mode"] or "ro"
        entries = []
        for name in names:
            # A restricted grant is a short list, and its databases can differ
            # in tier. An unrestricted grant can list hundreds, so it shows the
            # server tier. `submit_query` checks the tier again per database.
            tier = (teams.effective_mode_for_database(uid, t.id, name)
                    if grant["allowed_databases"] is not None else server_tier)
            if tier is None:
                continue
            entries.append({
                "name": name,
                "tier": engines.shown_tier(t.engine, tier, is_super).upper()})
        if not entries:
            continue
        out.append({
            "connection": t.alias,
            "engine": t.engine,
            "tier": engines.shown_tier(t.engine, server_tier, is_super).upper(),
            "databases": entries,
        })
    return {"connections": out, "maxTier": policy.max_tier().upper(),
            "resultMode": policy.result_mode()}


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
    t, grant = _granted(uid, connection)
    # A database outside the person's list gets one answer, whether it exists
    # or not. The first read below cannot tell the two apart for an
    # unrestricted grant, so the list decides.
    usable =db_scope.usable_databases(t, grant)
    if database not in usable:
        raise ToolError("no_access",
                        db_scope.no_access_message(t.alias, database, usable))
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
                f"The schema of '{database}' on '{connection}' is not catalogued "
                f"yet. The first snapshot has not run.")
        more = len(rows) > MAX_TABLES
        rows = rows[:MAX_TABLES]
        return {
            "connection": t.alias, "database": database,
            "tables": [{"schema": r["schema_name"], "table": r["table_name"],
                        "kind": r["relkind"], "columns": r["columns"]}
                       for r in rows],
            "truncated": more,
            "note": (f"This database has more than {MAX_TABLES} tables. The list "
                     f"shows the first {MAX_TABLES}. To see columns, pass `table` "
                     f"with part of a table name."
                     if more else
                     "To see columns, pass `table` with part of a table name."),
        }

    # The tables are chosen in SQL, capped, and only their columns are read. A
    # loose filter (`table="a"`) matched 2,073 tables on the largest database,
    # and every column of every one of them used to come back here to be
    # dropped. A table with no catalogued column is not a match, as before.
    like = f"%{want}%"
    rows = db.fetch_all(
        "WITH hit AS ("
        "  SELECT st.id, st.schema_name, st.table_name, st.relkind "
        "    FROM schema_tables st "
        "   WHERE st.target_server_id = %s AND st.database_name = %s "
        "     AND lower(st.table_name) LIKE %s "
        "     AND EXISTS (SELECT 1 FROM schema_columns x WHERE x.table_id = st.id) "
        "   ORDER BY st.schema_name, st.table_name "
        "   LIMIT %s) "
        "SELECT h.schema_name, h.table_name, h.relkind, "
        "       sc.column_name, sc.data_type, sc.is_pk "
        "  FROM hit h JOIN schema_columns sc ON sc.table_id = h.id "
        " ORDER BY h.schema_name, h.table_name, sc.column_name",
        (t.id, database, like, MAX_DETAIL_TABLES))
    if not rows:
        raise ToolError(
            "no_match",
            f"No catalogued table in '{database}' on '{connection}' matches "
            f"'{table}'. To see the table names, call "
            f"{catalog.named('describe_database')} without `table`.")
    tables: dict[tuple, dict] = {}
    for r in rows:
        e = tables.setdefault((r["schema_name"], r["table_name"]),
                              {"schema": r["schema_name"],
                               "table": r["table_name"],
                               "kind": r["relkind"], "columns": []})
        e["columns"].append({"name": r["column_name"], "type": r["data_type"],
                             "pk": bool(r["is_pk"])})
    matched = len(tables)
    if matched >= MAX_DETAIL_TABLES:
        # Only a full page can hide more. Counting reads no columns.
        counted = db.fetch_one(
            "SELECT count(*) AS n FROM schema_tables st "
            " WHERE st.target_server_id = %s AND st.database_name = %s "
            "   AND lower(st.table_name) LIKE %s "
            "   AND EXISTS (SELECT 1 FROM schema_columns x WHERE x.table_id = st.id)",
            (t.id, database, like))
        matched = counted["n"] if counted else matched
    return {
        "connection": t.alias, "database": database, "match": table,
        "tables": list(tables.values()),
        "truncated": matched > len(tables),
        "note": (f"{matched} tables matched. This result shows {len(tables)} of "
                 f"them. Use a more specific `table` filter."
                 if matched > len(tables) else None),
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
                "blocked": True, "reason": " ".join(blockers)}
    ok = policy.tier_allowed(required)
    return {"requiredTier": required.upper(), "accepted": ok,
            "maxTier": policy.max_tier().upper(), "blocked": False,
            "reason": None if ok else policy.refusal(required)}


# --- submitting --------------------------------------------------------------


def submit_query(connection: str, sql: str, database: str,
                 justification: str | None = None,
                 wait_seconds: int = DEFAULT_WAIT_SECONDS) -> dict:
    """Submit a statement. Governed exactly as a Slack or web submit is.

    The parameters follow the catalog: `connection`, `sql` and `database` are
    required, the rest are optional. `catalog.call` passes only the arguments
    that the client sent, so every optional argument has a default here.

    `database` is required, unlike in Slack and the web UI. A person sees the
    database that the form picks. A program that leaves it out sends the
    statement to the default database of the connection, which it never
    chose.

    The tier ceiling is checked here, before `validate_submission`, so a
    refusal costs nothing and reads as a policy answer rather than a
    validation failure. It is checked with the target's engine for the same
    reason `classify_sql` is.

    `unmasked` is not a parameter and never will be. It is refused for anyone
    who is not a super-admin and re-checked at execution, but a door for
    programs should not be able to ask the question at all -- the argument
    that reaches this function is the one an assistant can be talked into
    setting.

    `wait_seconds` counts from the start of this call, not from the start of
    the wait. A client has one time limit for the whole call, and the checks,
    the pre-flight plan and the notices before the wait use seconds of it.
    """
    started = time.monotonic()
    uid = _me()
    t = _target_or_refuse(uid, connection)

    required, blockers = _classify(sql, t.engine)
    if blockers:
        # Refused here as well as by `validate_submission`, which would also
        # catch it. The point is that the CEILING must not be the thing that
        # let it past: a blocked statement reports `ro`, so a ro ceiling would
        # have said yes to it.
        raise ToolError("blocked", " ".join(blockers))
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
        return _duplicate_or_refuse(prep, uid, t, database, sql)

    outcome = core_submit.create_request(prep)
    if isinstance(outcome, core_submit.Rejection):
        # The guard runs again inside the transaction, so a call that raced
        # its own retry lands here.
        return _duplicate_or_refuse(outcome, uid, t, database, sql)

    # Same fan-out the web submit does, with the same Slack client: admins are
    # told, and the executor is dispatched for an auto-approved request. The
    # door runs in the web process, so it uses the web's client, which is None
    # while Slack is off. `dm_requester=False` because the caller is a program
    # that gets its answer from `query_status`, not a DM.
    from ..web.routes_queries import _bot_client
    core_submit.dispatch_and_notify(_bot_client(), prep, outcome, dm_requester=False)

    rid = outcome.row["id"]
    out = {
        "requestId": rid,
        "requiredTier": required.upper(),
        "decision": "auto_approved" if outcome.auto_approved else "needs_approval",
        "status": outcome.row.get("status"),
    }
    _add_request_link(out, rid)
    if not outcome.auto_approved:
        # Waiting here would burn the whole budget and still return nothing: a
        # human approval is minutes, not seconds.
        out["note"] = (f"QueryHub notified a DBA. Approval is a human step and "
                       f"takes minutes. Poll {catalog.named('query_status')} with "
                       f"this `requestId` for the outcome.")
        return out

    wait = max(0, min(int(wait_seconds), MAX_WAIT_SECONDS))
    if wait:
        final = _await_result(rid, wait, started)
        out.update(final)
    else:
        out["note"] = (f"Poll {catalog.named('query_status')} with this `requestId` "
                       f"for the outcome.")
    return out


#: The statuses of a request that still waits or runs. The duplicate guard of
#: `core_submit` also counts `changes_requested`, but such a request does not
#: move again: a DBA asked for another statement. So a second submit of it
#: gets the guard's refusal, not "poll this request".
_OPEN_STATUSES = ("pending", "approved", "scheduled", "executing")


def _duplicate_or_refuse(rejection, uid: str, target, database: str | None,
                         sql: str) -> dict:
    """The answer for the request that already holds this statement, or the
    refusal.

    A client that gave up on a slow call sends the same call again. The
    duplicate guard refuses that second submit, and the model then has no
    request id to poll. So a duplicate answers like a submit, for the request
    that holds the statement, with `duplicate: true`. Every other refusal,
    and a duplicate whose request this lookup cannot find, raises as before.

    The limit of open requests runs before the guard in
    `validate_submission`. A person at the limit meets it first, also when
    the statement already waits, so that refusal asks too.
    """
    if rejection.reason == "duplicate" or rejection.field == "rate_limit":
        row = _open_request(uid, target, database, sql)
        if row is not None:
            out = {
                "requestId": row["id"],
                "requiredTier": (row["required_tier"] or "").upper() or None,
                "decision": _decision(row),
                "status": row["status"],
                "duplicate": True,
            }
            _add_request_link(out, row["id"])
            out["note"] = (f"You submitted this statement before. It is request "
                           f"{row['id']}. Do not submit it again. Poll "
                           f"{catalog.named('query_status')} with this `requestId`.")
            return out
    raise ToolError(rejection.reason or rejection.field, rejection.message)


def _open_request(uid: str, target, database: str | None, sql: str) -> dict | None:
    """The newest request of this person that holds this statement on this
    target and database, while it waits or runs.

    The match of the duplicate guard: the same requester, target, database
    and statement. It resolves the database as `validate_submission` does.

    None for a statement with a password literal. QueryHub stores it masked,
    so `WHERE password = 'a'` and `WHERE password = 'b'` have one stored
    form, and the guard inside `create_request` refuses the second as a
    duplicate of the first. The stored form cannot say which one waits, and
    the answer for the other one would hand the model a result for a
    statement it did not send. So that refusal stays.
    """
    query = (sql or "").strip()
    if query_safety.mask_password_literals(query) != query:
        return None
    return db.fetch_one(
        "SELECT id, status, required_tier, decided_by_slack_id "
        "  FROM requests "
        " WHERE requester_slack_id = %s AND target_server_id = %s "
        "   AND database_name = %s AND query = %s AND status = ANY(%s) "
        " ORDER BY id DESC LIMIT 1",
        (uid, target.id, database or target.default_database, query,
         list(_OPEN_STATUSES)))


def _decision(row: dict) -> str:
    """`needs_approval` while a person must decide, `auto_approved` when
    QueryHub approved the request, and `approved` when a person did."""
    if row["status"] == "pending":
        return "needs_approval"
    if row.get("decided_by_slack_id") == auto_approve.AUTO_DECIDED_BY:
        return "auto_approved"
    return "approved"


def _add_request_link(out: dict, request_id: int) -> None:
    """Add the link to this request in the person's QueryHub workspace.

    The link opens the request as a tab of the workspace in the portal: the
    SQL, the state, and the result when it ran. The approvers get the request
    in Slack and in their queue, so the person needs no forwarding step.
    Absent when the portal address is unknown, so Claude never shows a broken
    link.
    """
    link = policy.request_url(request_id)
    if link:
        out["requestUrl"] = link


def _await_result(request_id: int, seconds: int, started: float | None = None) -> dict:
    """Block until the request settles, or until the budget runs out.

    The budget counts from `started` (a `time.monotonic()` value) when it is
    given, and from now when it is not. A budget that the work before the
    wait used up still reads the status once.

    Measured on this fleet: a trivial auto-approved statement takes about 1.4
    seconds end to end, most of it two fresh connections to the target -- one
    for the pre-flight plan, one to run it. That is under a second of real
    work and over a second of connecting, and the caller should not have to
    spend a round trip discovering it finished.

    Polling belongs here rather than in the caller: an assistant asked to poll
    picks its own interval, and whatever it picks is added to a wait that was
    already over. One call in, one answer out.

    This blocks the thread that runs the call for the duration. The door
    (`web/routes_mcp.py`) runs each call in its own worker thread, so the wait
    is per call and not per process. It also caps the calls that run at once.
    The deadline is bounded for that reason.
    """
    deadline = (time.monotonic() if started is None else started) + seconds
    delay = 0.15
    while True:
        row = db.fetch_one(
            "SELECT status, row_count, truncated, required_tier, query, "
            "       error_message, csv_file_path "
            "  FROM requests WHERE id = %s", (request_id,))
        status = (row or {}).get("status")
        if row is not None and status in ("completed", "failed", "rejected", "cancelled"):
            out = {"status": status, "rowCount": row["row_count"],
                   "error": _error_text(row)}
            if status == "completed" and not policy.rows_allowed():
                # Counts and column names, never a value. A write has no file,
                # and its count is the rows it changed.
                out.update(_metadata(row))
                _result_link(out)
            elif status == "completed" and row["csv_file_path"]:
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
                    "note": (f"The request is still {status} after {seconds} "
                             f"seconds. The wait ended, but the work continues. "
                             f"Poll {catalog.named('query_status')} for the outcome.")}
        time.sleep(delay)
        delay = min(delay * 1.5, 1.0)   # tight at first, then back off


# --- reading back ------------------------------------------------------------


def _own(request_id: int, uid: str) -> dict:
    row = db.fetch_one(
        "SELECT id, status, requester_slack_id, target_server_id, "
        "       database_name, query, row_count, truncated, required_tier, "
        "       error_message, csv_file_path, created_at, completed_at "
        "  FROM requests WHERE id = %s", (request_id,))
    # Not found and not yours are one answer, so a caller cannot count other
    # people's requests by watching which refusal comes back.
    if row is None or row["requester_slack_id"] != uid:
        raise ToolError("not_found", f"You have no request with id {request_id}.")
    return row


_KINDS = {"ro": "read", "rw": "write", "ddl": "ddl"}


def _kind(row: dict) -> str | None:
    """`read`, `write` or `ddl`, from the tier the request needed."""
    return _KINDS.get((row.get("required_tier") or "").lower())


def _error_text(row: dict) -> str | None:
    """The stored error, safe to send out.

    The executor stores a cleaned message. While the door sends no rows, the
    text is cleaned again, since a database can echo a value in an error
    (`invalid input syntax for type integer: "Jordan Ray"`). That clean hides
    the values with PII masking off too. Cleaning a clean text changes nothing.
    """
    text = row.get("error_message")
    if not text or policy.rows_allowed():
        return text
    return errors.scrub(text, query=row.get("query"), hide_values=True)


def _columns_of(row: dict) -> list[str] | None:
    """The column names of a stored result: the header line and nothing else.

    None when the request has no readable file: it has no result set, the file
    is gone, or it holds one result for each statement.
    """
    path = row.get("csv_file_path")
    if not path:
        return None
    p = Path(path)
    if not p.is_file() or p.suffix.lower() != ".csv":
        return None
    with p.open(newline="", encoding="utf-8") as fh:
        try:
            return next(csv.reader(fh))
        except StopIteration:
            return []


def _metadata(row: dict) -> dict:
    """What a program may read of a request that ran, in any result mode.

    A count and the names of the columns. No value of the result is here. For a
    read, `rowCount` is the rows in the result file, which stops at the row cap
    (`max_rows`), so `truncated` says the real number may be higher. For a
    write, `rowCount` is the rows the statement changed. It has no cap.
    """
    out = {
        "kind": _kind(row),
        "rowCount": row["row_count"],
        "truncated": bool(row.get("truncated")),
        "columns": _columns_of(row),
    }
    if out["truncated"]:
        out["note"] = (f"The result holds {row['row_count']} rows, which is the row "
                       f"limit. The query returned more rows than that.")
    return out


def _result_link(out: dict) -> None:
    """Say that the rows stay in QueryHub, while the door sends none."""
    if policy.rows_allowed():
        return
    out["rowsHidden"] = True
    out["resultNote"] = ("QueryHub sends no result rows to Claude. The person opens "
                         "the request in QueryHub to read the result. The link is "
                         "`requestUrl`.")


def query_status(request_id: int) -> dict:
    """Where one submission has got to."""
    uid = _me()
    row = _own(int(request_id), uid)
    out = {
        "requestId": row["id"],
        "status": row["status"],
        "kind": _kind(row),
        "rowCount": row["row_count"],
        "truncated": bool(row["truncated"]),
        "error": _error_text(row),
        "hasResult": bool(row["csv_file_path"]),
        "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
        "completedAt": (row["completed_at"].isoformat()
                        if row["completed_at"] else None),
    }
    _add_request_link(out, row["id"])
    if row["status"] == "completed":
        out["columns"] = _columns_of(row)
        _result_link(out)
    return out


def fetch_result(request_id: int, offset: int = 0, limit: int = 50) -> dict:
    """A page of a completed result, read from the stored masked CSV.

    Only while `mcp_result_mode` is `masked_rows`. In the default mode this
    returns the counts and the column names and no row.

    The stored file is the masked one -- the executor writes it that way -- so
    reading it here cannot expose what a person reading the same result would
    not see. That is the property that makes this tool safe to hand to a
    program, and it holds because this reads the artefact rather than
    re-running the query. A result that a super-admin ran without masking is
    refused, because its stored file is not masked.
    """
    uid = _me()
    row = _own(int(request_id), uid)
    if row["status"] != "completed":
        raise ToolError("not_ready",
                        f"Request {row['id']} is not completed. Its status is "
                        f"'{row['status']}'.")
    if not policy.rows_allowed():
        # The default. A count and the column names, and the way to the result
        # in the web UI. This never opens the rows of the file.
        meta = {"requestId": row["id"], **_metadata(row)}
        _add_request_link(meta, row["id"])
        _result_link(meta)
        return meta
    if not row["csv_file_path"]:
        return {"requestId": row["id"], "kind": "affected",
                "affected": row["row_count"], "rows": [], "columns": []}
    # A super-admin can run their own request without masking. That file holds
    # the real values, and it never goes to a model.
    from ..web.routes_queries import _ran_unmasked
    if _ran_unmasked(row["id"]):
        raise ToolError(
            "unmasked_result",
            f"Request {row['id']} ran without masking. QueryHub does not send "
            f"its rows through MCP. Open the result in the web UI.")
    p = Path(row["csv_file_path"])
    if not p.is_file():
        raise ToolError("expired",
                        "QueryHub deleted that result. It keeps results for a "
                        "limited time only. Submit the statement again.")
    if p.suffix.lower() == ".zip":
        raise ToolError(
            "multi_statement",
            "That request stored one result for each statement. This MCP "
            "server cannot read those results yet. Use the web UI.")

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
