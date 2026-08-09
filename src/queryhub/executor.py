"""Executes approved queries against target servers in a background worker pool.

Runs the query with a per-statement timeout, streams up to max_rows into a CSV,
uploads the CSV (or a status message) to the requester, and updates request +
admin DMs with the final outcome.
"""
from __future__ import annotations

import csv
import itertools
import json
import io
import logging
import os
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
from psycopg import sql as pgsql

try:
    from slack_sdk.errors import SlackApiError
except ModuleNotFoundError:  # vanilla profile: the [slack] extra isn't installed
    class SlackApiError(Exception):  # type: ignore[no-redef]  # sentinel; Slack delivery paths don't run here
        pass

if TYPE_CHECKING:  # only a type hint — no runtime dependency on slack_sdk
    from slack_sdk.web import WebClient

from . import admins, audit, cancellation, cell_format, db, engines, errors, pii, pii_lineage, profile_sync, query_safety, ratings, requesters, row_limits, stmt_guard, targets, teams
from . import config as cfg
from .slack_app import notifications

log = logging.getLogger(__name__)

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sql-exec")
# There is deliberately no global write lock. The old one wrapped the ENTIRE CSV
# write ("guard CSV file writes if we ever share names"), so every
# result-producing query serialized behind it: the pool advertised 4 workers but
# delivered 1 for exactly the queries that take longest. Result paths are unique
# by construction — `req_<request_id>_q<n>_<utc-timestamp>` — and a request id is
# claimed by a single executor, so two writers cannot target the same file.

# Leading keywords whose statements only READ, so they can be streamed instead
# of materialised. DML is excluded on purpose: the server reports no rowcount
# for a streamed statement and the DML paths need it.
_STREAMABLE_LEADING = frozenset({"SELECT", "WITH", "VALUES", "TABLE", "EXPLAIN", "SHOW"})
_NO_ROW = object()

def _results_dir() -> Path:
    """Where result files are written.

    Env-configurable, because it was a hardcoded absolute path and that made
    the app unrunnable anywhere the path did not exist — a container being the
    obvious case, where the first approved query wrote nothing and the request
    sat in 'executing' until the lease reconciler noticed.

    The default is unchanged so an existing install keeps finding its files;
    QH_RESULTS_DIR overrides it (docker sets it to a volume).
    """
    raw = (os.environ.get("QH_RESULTS_DIR") or "").strip()
    return Path(raw) if raw else Path("/var/lib/queryhub/results")


CSV_DIR = _results_dir()


def _fmt_count(n: int) -> str:
    """Thousand-separated integer, e.g. 6_990_295 -> '6,990,295'."""
    return f"{n:,}"


def _fmt_duration(seconds: float) -> str:
    """Human-readable elapsed time. Sub-second uses ms; sub-minute keeps
    one decimal; minutes and hours drop the smaller unit when zero."""
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        m, s = divmod(int(round(seconds)), 60)
        return f"{m}m {s}s" if s else f"{m}m"
    h, rem = divmod(int(round(seconds)), 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m}m" if m else f"{h}h"


def _fmt_approve_ts(request: dict) -> str:
    """Compact ' @ HH:MM UTC' suffix for the admin's 'Approved by ...'
    line. Reads requests.decided_at when present; returns an empty
    string for auto-approved or older rows missing the column. Always
    UTC — admin DMs are broadcast (one shared block stream per
    request), so a single timezone keeps the message consistent
    across every admin's copy."""
    dt = request.get("decided_at")
    if dt is None:
        return ""
    return f" @ `{dt:%Y-%m-%d %H:%M UTC}`"


def _build_application_name(request: dict) -> str:
    """Identify the connection in pg_stat_activity / log_line_prefix.
    Pattern: `queryhub req=<id> by=<email-or-slack-id>`. Email is preferred
    (human-readable in logs); falls back to slack ID when the bot hasn't
    backfilled email yet. Truncated to Postgres' 63-byte application_name
    limit on the safe side (bytes ≠ chars for utf-8 emails). Don't bother
    raising on overflow — Postgres just truncates."""
    requester_id = request["requester_slack_id"]
    by = profile_sync.lookup_email(requester_id) or requester_id
    name = f"queryhub req={request['id']} by={by}"
    return name[:63]


def _team_role_for(principal_id: str, target_id: int) -> str | None:
    """If the user is a non-admin team member with a `target_role` set on
    their grant for this target, return that role name. The executor will
    `SET LOCAL ROLE <role>` before running the query so Postgres enforces
    the team's permissions on the target. Returns None for admins (they run
    as the bot's login user) or when no role is configured."""
    if admins.is_admin(principal_id):
        return None
    row = db.fetch_one(
        "SELECT g.target_role "
        "FROM team_target_grants g "
        "JOIN team_members tm ON tm.team_id = g.team_id "
        "WHERE tm.slack_user_id = %s "
        "  AND g.target_server_id = %s "
        "  AND g.target_role IS NOT NULL "
        "ORDER BY g.team_id LIMIT 1",
        (principal_id, target_id),
    )
    return row["target_role"] if row else None


def submit(request: dict, client: WebClient) -> None:
    """Schedule background execution of an approved request.

    Refuses to start NEW work while the process is draining. Drain was only
    checked on the submit path, so an approval (or a scheduler dispatch)
    landing during a restart still started a query the shutdown was about to
    interrupt — which then had to be reconciled as orphaned. Leaving the row in
    'approved' is the better outcome: boot recovery picks it up in the next
    process, so the work is delayed rather than half-run. In-flight executions
    are unaffected; shutdown() still waits for them."""
    from . import lifecycle
    if lifecycle.is_draining():
        log.info("draining — not starting request %s now; it stays 'approved' "
                 "and the next process start will run it", request.get("id"))
        return
    # A ThreadPoolExecutor keeps an escaped exception inside the Future, and
    # nobody was reading the Future — so anything raised outside _run's own
    # handler vanished completely: no log line, and the row left in 'executing'
    # until the lease reconciler timed it out fifteen minutes later. That is the
    # difference between a two-minute diagnosis and an afternoon. Found the hard
    # way: an unwritable results directory produced exactly this silence.
    future = _pool.submit(_run, request, client)
    future.add_done_callback(
        lambda f, rid=request.get("id"): _log_escaped_exception(rid, f))


def _log_escaped_exception(request_id, future) -> None:
    """Surface an exception that escaped the worker instead of losing it."""
    try:
        exc = future.exception()
    except Exception:            # cancelled — nothing to report
        return
    if exc is not None:
        log.error("request %s: execution worker raised an unhandled exception; "
                  "the row stays 'executing' until the lease reconciler clears "
                  "it", request_id, exc_info=exc)


_SENTINEL_PASSWORD = "PASSWORD_NOT_SET"


@dataclass
class _StmtResult:
    """Per-statement outcome captured during _run. `csv_path` is set only
    for statements that returned a result set AND wants_result was True.
    `rowcount` is rows affected (DML) or rows captured (SELECT)."""
    index: int               # 1-based main-statement index
    leading: str             # e.g. SELECT, UPDATE
    rowcount: int = 0
    csv_path: Path | None = None
    plan_text: str | None = None    # set for a lone EXPLAIN → inline code block
    truncated_rows: bool = False
    truncated_size: bool = False
    has_result_set: bool = False
    pii_masked: set | None = None   # detector names that fired (email/phone/...)
    pii_exempt: bool = False        # masking skipped via pii_masking_exemptions
    col_types: dict | None = None   # column name -> SQL type, from the driver


def _resolve_unknown_oids(cur, oids: set) -> dict:
    """{oid: SQL type name} for types psycopg could not name itself.

    psycopg's `type_display` falls back to the bare OID for any type it has no
    adapter for — user-defined ones: enums, domains, composites. Measured, not
    guessed: the control DB's own `requests.status` is an enum and came back as
    the string "42887", which in a tooltip is worse than showing nothing.

    `format_type` is the server's own canonical spelling and handles arrays and
    modifiers, so it is a better answer than `typname`. Only the unknown OIDs are
    looked up, so the common all-builtin result costs no extra query at all.

    Row-factory agnostic on purpose. The target connection yields tuples while
    the control-plane pool is configured with `dict_row`, so indexing by position
    works in production and raises KeyError under a dict cursor — and it failed
    SILENTLY: the lookup returned {} and the column just lost its type, which
    looks exactly like the bug this whole change is fixing. Read by name when the
    row supports it, by position otherwise.
    """
    if not oids:
        return {}
    try:
        cur.execute("SELECT oid, format_type(oid, NULL) AS fmt FROM pg_type "
                    "WHERE oid = ANY(%s)", (list(oids),))
        out = {}
        for r in cur.fetchall():
            try:
                oid, fmt = r["oid"], r["fmt"]
            except (TypeError, KeyError, IndexError):
                oid, fmt = r[0], r[1]
            if fmt:
                out[str(oid)] = fmt
        return out
    except Exception:
        # A failed lookup just means those columns keep their numeric label,
        # which the caller then drops. Never fatal.
        log.debug("pg_type lookup for unknown column types failed", exc_info=True)
        return {}


def _apply_resolved_types(types: dict | None, unknown: set, cur) -> dict | None:
    """Replace numeric OID labels in `types` with real type names.

    Split out from `_column_types` — and called at a different TIME — because of
    a production wedge. The lookup is a second query on the SAME connection that
    is streaming the result, and it used to run while that stream was still open:

        res.col_types = _column_types(cur.description, cur)   # portal OPEN
        ...
        _stream_to_csv(..., rows, max_rows, ...)              # may break early

    A stream broken at the row cap leaves the original portal open, so the OID
    lookup queued behind the rest of that result and never returned. No error, no
    timeout: the single `sql-exec` worker blocked forever, every later request sat
    in `approved`, and the UI showed them as running. The row itself was force
    -failed by the runaway watchdog, so the database looked clean while the
    process stayed wedged — the outage lasted until a restart.

    It only bit when BOTH held, which is why it looked intermittent: the result
    had to be truncated (portal left open) AND contain a user-defined type (enum,
    domain, composite) for the lookup to happen at all.

    So the caller must close the portal first. This function is the second half,
    and it is best-effort by construction: an unresolvable label is dropped so the
    grid falls back to the schema catalog rather than rendering an OID.
    """
    if not types:
        return types or None
    if unknown:
        resolved = _resolve_unknown_oids(cur, unknown) if cur is not None else {}
        for name, label in list(types.items()):
            if label.isdigit():
                real = resolved.get(label)
                if real:
                    types[name] = real
                else:
                    del types[name]
    return types or None


def _column_types(description) -> tuple[dict, set]:
    """{column name: SQL type} from the driver's own cursor description.

    The authoritative answer, and the reason this exists at all. The web grid's
    header tooltip used to guess: it built a column-NAME -> type map from the
    hourly schema snapshot and dropped any name that appeared in two tables with
    different types. `id`, `user_id` and `created_at` live in dozens of tables,
    so they were always dropped — the tooltip fell back to the bare name for
    exactly the columns people hover most, which made the feature useless for
    the case it was built for.

    Asking the driver fixes that and more: it is correct for aliases,
    expressions, aggregates and joins, which no catalog lookup can resolve
    (`count(*)` is `int8`, `upper(x)` is `text`), and it needs no schema
    snapshot, so it works on a target that has never been catalogued.

    Nullability is deliberately NOT taken from here. psycopg reports
    `null_ok=None` for every column (measured, not assumed) and pyodbc's flag
    describes the expression rather than the underlying column, so `not null`
    keeps coming from the catalog, which does know.

    Returns (types, unknown_oids) and issues NO query of its own — types whose
    name psycopg could not give are left as their numeric OID for
    `_apply_resolved_types` to fix later, once the caller has closed the result
    portal. See that function for why the two halves must not run together.

    Never raises: a tooltip must never be the reason a delivered result fails.
    """
    try:
        out: dict[str, str] = {}
        unknown: set = set()          # OIDs psycopg named only by number
        for d in description:
            name = d[0]
            if not name or name in out:
                # A duplicate name is ambiguous by construction here (the CSV
                # header dedups them separately), so keep the first and move on.
                continue
            label = None
            # psycopg3 exposes exactly what is wanted, including modifiers and
            # array-ness: varchar(20), numeric(10,2), int4[].
            disp = getattr(d, "type_display", None)
            if isinstance(disp, str) and disp:
                label = disp
                if disp.isdigit():
                    # psycopg has no adapter for this type, so type_display is
                    # the raw OID — a user-defined enum, domain or composite.
                    # Resolve it below; a number in a tooltip is worse than
                    # nothing.
                    unknown.add(int(disp))
            else:
                # pyodbc: description[i][1] is a Python type object. Coarser
                # than a SQL type name, but it is what the driver knows, and a
                # size is available for the char/binary types where it matters.
                t = d[1] if len(d) > 1 else None
                base = getattr(t, "__name__", None) or (str(t) if t else None)
                if base:
                    size = d[3] if len(d) > 3 else None
                    label = (f"{base}({size})"
                             if base == "str" and isinstance(size, int) and size > 0
                             else base)
            if label:
                out[name] = label
        return out, unknown
    except Exception:
        log.debug("column type extraction failed", exc_info=True)
        return {}, set()


# Commands Postgres refuses to run inside a transaction block. When one
# of these is the ONLY statement we run it in autocommit mode (no txn).
# When it appears alongside others (multi-statement, which we run in a
# transaction for atomicity) we can't honor both, so we escalate to
# manual DBA execution. Matched against the rewritten statement text.
_TXN_INCOMPATIBLE_RE = re.compile(
    r"\bCONCURRENTLY\b"
    r"|\bVACUUM\b"
    r"|\bCREATE\s+DATABASE\b"
    r"|\bDROP\s+DATABASE\b"
    r"|\bCREATE\s+TABLESPACE\b"
    r"|\bDROP\s+TABLESPACE\b"
    r"|\bALTER\s+SYSTEM\b",
    re.IGNORECASE,
)


def _is_txn_incompatible(sql: str) -> bool:
    return bool(_TXN_INCOMPATIBLE_RE.search(sql or ""))


# Transient Slack errors on the file-upload flow. These come back as
# HTTP 200 with ok:false (an app-level error), so slack_sdk's built-in
# retry handlers — which only cover transport/rate-limit — never retry
# them. `file_update_failed` on files.completeUploadExternal is the one we
# see intermittently (a few times a month); without a retry it throws away
# the result of an already-run query. Keep the set tight to genuinely
# transient errors so we don't mask real problems (e.g. not_in_channel).
_TRANSIENT_UPLOAD_ERRORS = frozenset({
    "file_update_failed", "service_unavailable", "internal_error",
    "fatal_error", "timeout_error",
})
_UPLOAD_BACKOFF_SEC = (0.5, 1.5)   # waits before attempts 2 and 3 (3 total)


def _upload_with_retry(client: WebClient, **kwargs):
    """`client.files_upload_v2` with a short retry on transient Slack upload
    errors. Re-raises immediately on a non-transient error and after the
    final attempt. Safe to sleep here — the executor runs off the Bolt ack
    path, in a worker thread."""
    if not cfg.ENV.slack_enabled:
        return {}  # vanilla profile: no Slack upload
    attempts = len(_UPLOAD_BACKOFF_SEC) + 1
    for attempt in range(1, attempts + 1):
        try:
            return client.files_upload_v2(**kwargs)
        except SlackApiError as e:
            err = e.response.get("error") if e.response is not None else None
            if err not in _TRANSIENT_UPLOAD_ERRORS or attempt == attempts:
                raise
            wait = _UPLOAD_BACKOFF_SEC[attempt - 1]
            log.warning(
                "Slack upload attempt %d/%d hit transient error %r; "
                "retrying in %.1fs", attempt, attempts, err, wait)
            time.sleep(wait)


def _deliver_result_to_requester(request: dict) -> bool:
    """Whether to deliver the finished result to the requester in Slack.

    A request is answered on the channel it came from: web-origin requests
    read their result in the web UI, so by default the CSV/summary is NOT
    also DM'd to the requester in Slack. `bot_config web_result_to_slack`
    flips that back on (deliver to both). Slack-origin requests always
    deliver — Slack is their channel. Admin-facing messages (approval-card
    updates) are unaffected by this gate."""
    if not cfg.ENV.slack_enabled:
        return False  # vanilla profile: results are read in the web UI
    if (request.get("origin") or "slack") == "web":
        return cfg.get_bool("web_result_to_slack", False)
    return True


def _run(request: dict, client: WebClient) -> None:
    request_id = request["id"]
    csv_paths_to_cleanup: list[Path] = []
    # Assigned BEFORE the try, not inside it. The except handler below reads it
    # to decide whether a mutation already committed, so anything raising before
    # its old assignment point crashed the ERROR HANDLER with an
    # UnboundLocalError — the one place that must not fail. Surfaced by adding a
    # new early return above it; the hazard was already there.
    committed = {"mutation": False}
    try:
        target = targets.get(request["target_server_id"])
        if target is None:
            _fail(client, request, "Target server not found at execution time.")
            return

        # A DISABLED target must not be reached, at execution time and not just
        # at submit time. `enabled` gated the pickers and the connection list, so
        # nothing new could be submitted — but a request already approved, queued
        # or scheduled ran anyway, because targets.get() returns the row
        # regardless. Disabling is what an operator does when a host is being
        # migrated, decommissioned or is in an incident, and it has to mean "no
        # more queries", not "no more submissions".
        #
        # Checked here rather than in targets.get(): the browse and admin paths
        # legitimately need to SEE a disabled target, and hiding it from them
        # would be a different bug.
        if not target.enabled:
            _fail(client, request,
                  f"Target `{target.alias}` was disabled before this request "
                  f"ran, so it was not executed. Ask an admin why the target is "
                  f"disabled, then resubmit.")
            return

        # Fail closed for a known-but-unwired engine. A target tagged with an
        # engine the bot can't execute yet (e.g. mssql before its driver +
        # execution path are validated against a real host) must NOT run
        # through the Postgres path — that would point psycopg at a non-PG
        # server and skip the engine's own safety dialect.
        if not engines.is_executable(target.engine):
            _fail(client, request,
                  f"Target `{target.alias}` runs on the `{target.engine}` "
                  f"engine, which the bot cannot execute yet.")
            return

        # Re-analyze the query NOW (instead of trusting the modal classification)
        # so we always pick the right credential tier even if the query was
        # edited via the Request-changes flow.
        report = query_safety.analyze(request["query"], engine=target.engine)
        if report.blocked:
            _fail(client, request, " ".join(report.blockers))
            return
        mode = report.main_tier
        # Set true once a committing mutation has run in autocommit mode: a
        # later delivery/finalize error must then NOT report the request as
        # failed, because the change is already applied. Defined here
        # so it is always in scope for the exception handlers below.

        # Re-authorize at execution time. The grant that justified this request
        # at submit can be revoked or downgraded before it actually runs —
        # scheduled, bundled, and queued requests can execute long after
        # approval. Resolve the requester's CURRENT tier for this database and
        # fail closed if it no longer covers the query. Admins / bypass keep
        # their ddl-everywhere standing, matching submit-time authorization.
        _AUTH_RANK = {"ro": 0, "rw": 1, "ddl": 2}
        requester = request["requester_slack_id"]
        # Offboarding must actually stop work. Disabling the `requesters` row is
        # the documented leaver step, but it only gated /sql ENTRY: a request
        # already approved — especially one scheduled for next week — still ran
        # after the person left, because their grant row was untouched and the
        # tier check below passes. Re-check the whitelist here too, the same
        # gate the submit path applies (admins pass implicitly, as they do
        # there).
        if not (admins.is_admin(requester) or requesters.is_allowed(requester)):
            _fail(
                client, request,
                "The requester's QueryHub access has been withdrawn, so this "
                "query was not run.")
            return
        current_mode = teams.effective_mode_for_database(
            requester, target.id, request["database_name"])
        if current_mode is None or _AUTH_RANK[mode] > _AUTH_RANK[current_mode]:
            _fail(
                client, request,
                "Your access to this database changed after approval, so this "
                "query was not run. Please re-submit if you still need it.")
            return

        try:
            db_user, password = targets.get_credentials(target.id, mode)
        except LookupError:
            _fail(
                client, request,
                f"Target `{target.alias}` is not ready for {mode.upper()} "
                f"queries (credentials not configured on the bot side). "
                f"Please contact the DBA team.",
            )
            return
        if password == _SENTINEL_PASSWORD:
            _fail(
                client, request,
                f"Target `{target.alias}` is not ready yet. "
                f"Please contact the DBA team.",
            )
            return

        timeout_sec = cfg.get_int("query_timeout_sec", 300)
        # Per-user caps: a time-bounded row-limit override raises these
        # above the global default for specific users (row_limits); an
        # absent/expired override falls back to the global caps.
        max_rows, max_csv_bytes = row_limits.effective_caps(
            request["requester_slack_id"])

        # Atomically claim the request before executing. This
        # 'approved' -> 'executing' transition is the SINGLE exclusive gate on
        # execution: exactly one caller can win it, so a request handed to the
        # pool twice runs once.
        #
        # It previously also accepted status='executing', which broke
        # exclusivity: a request sitting 'approved' in one process's worker
        # queue could be picked up by another process's boot recovery, and the
        # second _run would happily re-claim the row the first had already
        # flipped to 'executing' — running an RW/DDL statement twice. The
        # scheduler no longer pre-flips to 'executing' either (it moves due
        # rows to 'approved'), so this is the only writer of that state.
        #
        # rowcount==0 means the claim was lost — the row was cancelled,
        # rejected, superseded, or already claimed. Abort silently: whoever
        # owns the new state owns the user-facing message.
        with db.transaction() as cur:
            cur.execute(
                "UPDATE requests SET status = 'executing', executed_at = NOW(), "
                " executed_tier = %s "
                "WHERE id = %s AND status = 'approved'",
                (mode, request_id),
            )
            if cur.rowcount == 0:
                log.info("request %s is no longer runnable at claim time "
                         "(cancelled/superseded) — skipping execution", request_id)
                return
            audit.log_in(cur, request_id, None, None, "execution_started",
                         {"mode": mode, "user": db_user,
                          "n_statements": len(report.statements)})

        # SQL Server (MSSQL) uses a separate pyodbc flow — no search_path /
        # SET ROLE / SET LOCAL prelude / EXPLAIN plan / CONCURRENTLY rules.
        # It builds the same _StmtResult list and shares _finalize + the
        # completion path. Reached only for a WIRED mssql target (fail-closed
        # in engines.is_executable above). Postgres continues below unchanged.
        if target.engine == "mssql":
            _run_mssql(client, request, target, mode, report,
                       db_user, password, timeout_sec, max_rows, max_csv_bytes)
            return

        app_name = _build_application_name(request)
        team_role = _team_role_for(request["requester_slack_id"], target.id)

        # Tighter cap on idle-in-transaction so a stalled bot can't hold
        # locks past the query's own timeout window. statement_timeout
        # bounds active query time; idle_in_tx bounds the gap between
        # statements (e.g. CSV write). Slightly looser than statement
        # itself to give CSV streaming room.
        options = (
            f"-c statement_timeout={timeout_sec * 1000} "
            f"-c idle_in_transaction_session_timeout={(timeout_sec + 30) * 1000}"
        )
        # Defense in depth: an RO-tier query runs in a read-only
        # session, so even a VOLATILE / SECURITY DEFINER function it calls
        # can't write, and a mis-classification can't mutate data — the RO
        # db role is no longer the only barrier between a "read" and a write.
        # RW/DDL tiers must be able to write, so this is scoped to RO only.
        if mode == "ro":
            options += " -c default_transaction_read_only=on"

        prelude_stmts = [s for s in report.statements if s.kind == "set"]
        main_stmts = [s for s in report.statements if s.kind != "set"]

        # A single statement with no SET prelude runs in AUTOCOMMIT mode —
        # no surrounding transaction. This lets CREATE INDEX CONCURRENTLY,
        # VACUUM, etc. run (they're rejected inside a transaction block),
        # and a failure simply means the one statement didn't apply, so
        # there's nothing to roll back. Everything else (multi-statement
        # or with a SET prelude) runs inside a transaction for atomicity.
        autocommit = (len(main_stmts) == 1 and not prelude_stmts)

        # Transaction path can't host a txn-incompatible command. If a
        # multi-statement request contains one, we can't both keep
        # atomicity AND run that command — escalate to manual DBA exec.
        if not autocommit:
            bad = next((s for s in main_stmts
                        if _is_txn_incompatible(s.rewritten)), None)
            if bad is not None:
                _escalate_to_dba(
                    client, request,
                    "This request runs multiple statements (so the bot uses "
                    "a transaction), but one of them can't run inside a "
                    "transaction block (e.g. CREATE INDEX CONCURRENTLY, "
                    "VACUUM). Submit it as a single-statement request, or "
                    "the DBA team can run it out-of-band.",
                )
                return

        with psycopg.connect(
            host=target.host,
            port=target.port,
            dbname=request["database_name"],
            user=db_user,
            password=password,
            connect_timeout=15,
            application_name=app_name,
            **cfg.target_ssl_kwargs(),
            autocommit=autocommit,
            options=options,
        ) as conn:
            with conn.cursor() as cur:
                # Pin search_path with pg_catalog FIRST so a same-named
                # object in a writable schema (e.g. a malicious public.now())
                # can't shadow a built-in function/operator (CVE-2018-1058
                # class). `public` still follows, so unqualified user tables
                # resolve and an unqualified CREATE still lands in public
                # (pg_catalog isn't writable). In a transaction we use SET
                # LOCAL (undone at commit); in autocommit there's no
                # transaction, so we use a plain session SET — safe because
                # the connection is single-use and closed right after.
                scope = "" if autocommit else "LOCAL "
                cur.execute(f"SET {scope}search_path = pg_catalog, public")

                # Record which target backend is about to run this, so a user
                # (or the runaway watchdog) can actually stop it. Without a pid
                # there is nothing to aim at, and an operator had to hunt through
                # pg_stat_activity by hand. Best-effort: losing the pid must
                # never cost us the query.
                try:
                    cur.execute("SELECT pg_backend_pid()")
                    _pid = cur.fetchone()[0]
                    with db.transaction() as _mcur:
                        _mcur.execute(
                            "UPDATE requests SET backend_pid = %s WHERE id = %s",
                            (_pid, request["id"]))
                except Exception:
                    log.debug("could not record backend pid for request %s",
                              request["id"], exc_info=True)
                if team_role:
                    set_role = pgsql.SQL(
                        "SET " + scope + "ROLE {}"
                    ).format(pgsql.Identifier(team_role))
                    cur.execute(set_role)

                t_start = time.monotonic()

                # Prelude SET LOCAL statements (auto-rewritten to LOCAL by
                # query_safety). Empty in the autocommit path by construction.
                for s in prelude_stmts:
                    cur.execute(s.rewritten)

                # An EXPLAIN plan is delivered inline as a code block rather
                # than a file — but only for a lone EXPLAIN statement, and
                # only while the (default-on) toggle allows it.
                capture_plan = (
                    len(main_stmts) == 1
                    and cfg.get_setting("explain_inline_plan", "on") == "on"
                )

                # Run main statements, capture per-statement result.
                stmt_results: list[_StmtResult] = []
                for i, s in enumerate(main_stmts, start=1):
                    res = _execute_main_statement(
                        cur, s, i, request_id,
                        request["wants_result"], max_rows, max_csv_bytes,
                        result_format=request.get("result_format") or "csv",
                        target_id=target.id,
                        database=request["database_name"],
                        engine=target.engine,
                        requester_id=request["requester_slack_id"],
                        capture_plan=capture_plan,
                        # In autocommit mode a mutating (RW/DDL) statement is
                        # durable the instant it returns — mark it so a later
                        # failure isn't reported as if nothing happened.
                        on_committed=(
                            (lambda: committed.__setitem__("mutation", True))
                            if (autocommit and mode in ("rw", "ddl")) else None
                        ),
                        # Wire-level multi-command refusal (Postgres path only;
                        # the pyodbc path has no equivalent knob).
                        force_extended=True,
                    )
                    if res.csv_path is not None:
                        csv_paths_to_cleanup.append(res.csv_path)
                    stmt_results.append(res)

                if not autocommit:
                    conn.commit()
                elapsed = time.monotonic() - t_start

        _finalize(client, request, stmt_results, csv_paths_to_cleanup,
                  max_csv_bytes=max_csv_bytes, target=target, elapsed=elapsed)

    except psycopg.errors.QueryCanceled as e:
        # `statement_timeout` fired (or pg_cancel_backend / lock_timeout).
        # Surface the configured timeout in the DM so the user
        # immediately knows the bar that was hit, instead of a generic
        # "canceling statement" message.
        log.info("Request %s hit statement timeout (%ss)", request_id,
                 cfg.get_int("query_timeout_sec", 300))
        for p in csv_paths_to_cleanup:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        timeout_sec = cfg.get_int("query_timeout_sec", 300)
        scrubbed = errors.scrub(e)
        msg = (
            f"Query exceeded the {_fmt_duration(timeout_sec)} statement "
            f"timeout ({timeout_sec}s, `bot_config.query_timeout_sec`) "
            f"and was cancelled. Try narrowing the WHERE clause, adding "
            f"a LIMIT, or asking the DBA team to raise the cap.\n"
            f"_Postgres said:_ {scrubbed}"
        )
        _fail(client, request, msg)
        return

    except psycopg.errors.InsufficientPrivilege as e:
        # DDL-tier requests can hit "permission denied" / "must be owner"
        # (SQLSTATE 42501) when our queryhub_ddl role lacks
        # ownership / superuser for the operation. Escalate to DBA
        # manual execution instead of marking the request failed —
        # admin runs the query out-of-band with elevated creds and
        # closes it via [Mark completed] / [Mark failed] buttons.
        log.info("Request %s needs DBA manual execution: %s", request_id, e)
        for p in csv_paths_to_cleanup:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        if mode == "ddl":
            _escalate_to_dba(client, request, errors.scrub(e))
        else:
            # Non-DDL perm errors are genuine failures (RO/RW shouldn't
            # hit owner-required ops). Surface as fail.
            _fail(client, request, errors.scrub(e))
    except Exception as e:  # noqa: BLE001 — surface any exec error back to the user
        log.exception("Request %s failed during execution", request_id)
        # Ordering guard: a completion transaction commits 'completed' BEFORE
        # the Slack delivery / file bookkeeping that follows it. If that
        # post-commit step raises, the query already ran — and for RW/DDL its
        # changes are applied. Cleaning the result CSV and flipping the row to
        # 'failed' here would lose the result and make the requester re-run an
        # already-applied statement. If the row already reached a terminal /
        # DBA-owned state, this failure is post-completion: record it and
        # leave the row (and its CSV) intact.
        try:
            _cur = db.fetch_one(
                "SELECT status FROM requests WHERE id = %s", (request_id,))
        except Exception:
            _cur = None
        if _cur and _cur["status"] in (
                "completed", "rejected", "cancelled", "awaiting_dba_manual"):
            log.warning("Request %s already '%s' — post-completion error "
                        "swallowed; not failing or cleaning the result.",
                        request_id, _cur["status"])
            try:
                with db.transaction() as _gc:
                    audit.log_in(_gc, request_id, "SYSTEM", "delivery guard",
                                 "delivery_failed",
                                 {"after_status": _cur["status"],
                                  "error": errors.scrub(f"{type(e).__name__}: {e}")})
            except Exception:
                log.exception("Request %s: delivery_failed audit failed",
                              request_id)
            return
        # A mutation ran and committed in autocommit mode, but we
        # failed before recording completion (e.g. the result CSV write or
        # _finalize raised). Marking the row 'failed' here would tell the user
        # nothing happened while the change is in fact applied — and could
        # prompt a duplicate re-run. Record it as completed with a delivery
        # warning instead.
        if committed["mutation"] and _cur and _cur["status"] == "executing":
            log.warning("Request %s: mutation committed but delivery/finalize "
                        "failed — marking completed with a warning, not failed.",
                        request_id)
            _complete_with_delivery_warning(
                client, request, errors.scrub(f"{type(e).__name__}: {e}"))
            return
        for p in csv_paths_to_cleanup:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        if isinstance(e, psycopg.Error):
            user_msg = errors.scrub(e)
        else:
            user_msg = errors.scrub(f"{type(e).__name__}: {e}")
        # Connection-timeout-flavoured OperationalError: surface the
        # connect_timeout value so the user knows the bar (separate from
        # statement_timeout — connect_timeout is a hardcoded 15s here).
        low = user_msg.lower()
        if isinstance(e, psycopg.OperationalError) and (
                "timeout expired" in low or "timed out" in low):
            user_msg = (
                f"Could not connect to the target within the 15s connect "
                f"timeout. The DB may be down, unreachable from the bot "
                f"host, or saturated. _Postgres said:_ {user_msg}"
            )
        _fail(client, request, user_msg)


def _read_plan_text(cur, columns) -> tuple[str, int, bool]:
    """Read an EXPLAIN result into plain text for a code block. The default
    (text) format is a single 'QUERY PLAN' column, one plan line per row;
    FORMAT JSON/YAML/XML is a single cell. Caps total length at
    `explain_max_chars` so a huge plan can't blow past Slack's message
    limits — the tail is dropped and the truncation flag returned."""
    budget = cfg.get_int("explain_max_chars", 11000)
    single_col = len(columns) == 1
    lines: list[str] = []
    total = 0
    truncated = False
    for row in cur:
        cell = row[0] if single_col else " | ".join(
            "" if v is None else str(v) for v in row)
        cell = "" if cell is None else str(cell)
        if total + len(cell) + 1 > budget:
            truncated = True
            break
        lines.append(cell)
        total += len(cell) + 1
    return "\n".join(lines), len(lines), truncated


def _finalize(client: WebClient, request: dict, stmt_results: list,
              csv_paths_to_cleanup: list, *,
              max_csv_bytes: int, target, elapsed: float) -> None:
    """Shared post-execution: size-cap check, PII-fired collection + audit,
    and completion dispatch (plan / no-result / csv / multi). Engine-agnostic
    — both the Postgres and SQL Server paths build a list of _StmtResult and
    hand it here, so the completion logic lives in exactly one place."""
    request_id = request["id"]
    # Size truncation -> fail the whole request, clean every CSV.
    if any(r.truncated_size for r in stmt_results):
        for p in csv_paths_to_cleanup:
            p.unlink(missing_ok=True)
        max_mb = max_csv_bytes // 1024 // 1024
        _fail(
            client, request,
            f"Result would exceed the {max_mb} MB result-size limit. "
            f"Add a LIMIT / narrow the columns, or ask the DBA team for "
            f"a larger export.",
        )
        _alert_admins_size_cap(client, request, max_mb)
        return

    # Collect every PII detector that fired across all statements, for the
    # audit log + the result-DM hint.
    masked: set = set()
    for r in stmt_results:
        if r.pii_masked:
            masked |= r.pii_masked
    masked_sorted = sorted(masked) if masked else None
    pii_exempted = any(r.pii_exempt for r in stmt_results)
    if pii_exempted:
        # Forensic trail: masking was (partly) lifted by an exemption.
        with db.transaction() as audit_cur:
            audit.log_in(audit_cur, request_id, None, None,
                         "pii_masking_exempted",
                         {"target_id": target.id,
                          "database": request["database_name"]})

    # Dispatch to completion based on shape.
    if len(stmt_results) == 1:
        r = stmt_results[0]
        if r.plan_text is not None:
            # EXPLAIN plan → inline code block, no file.
            _complete_with_plan(
                client, request, r.plan_text, r.truncated_rows,
                elapsed=elapsed)
        elif r.csv_path is None:
            # No CSV path: either DML w/o RETURNING (had_result_set=False)
            # or SELECT/RETURNING that produced 0 rows (had_result_set=True).
            _complete_no_result(
                client, request, r.rowcount,
                truncated=r.truncated_rows, elapsed=elapsed,
                had_result_set=r.has_result_set,
            )
        else:
            _complete_with_csv(
                client, request, r.csv_path, r.rowcount,
                r.truncated_rows, elapsed=elapsed,
                pii_masked=masked_sorted,
                pii_exempt=pii_exempted,
                col_types=r.col_types,
            )
    else:
        _complete_multi(client, request, stmt_results, elapsed=elapsed,
                        pii_masked=masked_sorted)


def _run_mssql(client: WebClient, request: dict, target, mode: str, report,
               db_user: str, password: str, timeout_sec: int,
               max_rows: int, max_csv_bytes: int) -> None:
    """SQL Server execution path (pyodbc). Reached only for a WIRED mssql
    target (fail-closed until then), called inside _run's try so the outer
    handler surfaces any error via _fail (the ordering guard applies). Reuses the
    cursor-agnostic _execute_main_statement + _finalize + completion fns; the
    only engine-specific parts are the pyodbc connection and read-only routing.
    Read routing is done in the BOT (not via /etc/hosts): for RO we resolve the
    current readable secondary through the bot-DB IP map (mssql_exec.
    resolve_ro_endpoint) and connect straight to that node, so the AG's FQDN
    routing redirect is never used; RW/DDL go to the listener (primary). T-SQL
    has no SET-LOCAL prelude (query_safety rejects a leading SET on this engine)
    and no PG-style EXPLAIN plan (capture_plan=False)."""
    from . import mssql_exec
    request_id = request["id"]
    main_stmts = [s for s in report.statements if s.kind != "set"]
    result_format = request.get("result_format") or "csv"
    db_name = request["database_name"]
    if mode == "ro":
        ep = mssql_exec.resolve_ro_endpoint(
            target.host, target.port, db_name, db_user, password,
            timeout_sec=timeout_sec)
        if ep:
            conn = mssql_exec.connect(ep[0], ep[1], db_name, db_user, password,
                                      timeout_sec=timeout_sec, read_only=True)
        else:
            log.warning("Request %s: MSSQL read-routing unavailable — running "
                        "RO on the primary (no read offload).", request_id)
            conn = mssql_exec.connect(target.host, target.port, db_name, db_user,
                                      password, timeout_sec=timeout_sec, read_only=False)
    else:
        conn = mssql_exec.connect(target.host, target.port, db_name, db_user,
                                  password, timeout_sec=timeout_sec, read_only=False)
    try:
        t_start = time.monotonic()
        cur = conn.cursor()
        stmt_results: list[_StmtResult] = []
        for i, s in enumerate(main_stmts, start=1):
            # geography / geometry / hierarchyid come back as SQL Server's own
            # serialisation, which pyodbc hands over as opaque bytes and we render
            # as hex. Only the server can turn that into `POINT (28.9784
            # 41.0082)` or `/1/2/3/`, so ask it to: describe the result shape
            # without running anything, and if a UDT column is in there, run a
            # re-projection that calls .ToString() on exactly those columns.
            #
            # RO only, and the server is the judge of whether the wrap is legal.
            # A top-level ORDER BY makes the query unwrappable ("The ORDER BY
            # clause is invalid in views, inline functions…" — measured), and
            # deciding that ourselves would mean parsing T-SQL. So we try the
            # wrapped form and fall back to the original on any error. That retry
            # is only free because RO statements have no effects; never do this
            # for RW/DDL, where a first attempt may already have changed data.
            s_run, wrapped = s, None
            if mode == "ro":
                try:
                    cols = mssql_exec.describe_columns(cur, s.rewritten)
                    wrapped = (mssql_exec.wrap_udt_projection(s.rewritten, cols)
                               if cols else None)
                except Exception:
                    log.info("Request %s: UDT describe failed — running the "
                             "statement unchanged", request_id, exc_info=True)
                    wrapped = None
            if wrapped:
                s_run = dataclasses.replace(s, rewritten=wrapped)
            try:
                res = _execute_main_statement(
                    cur, s_run, i, request_id,
                    request["wants_result"], max_rows, max_csv_bytes,
                    result_format=result_format,
                    target_id=target.id,
                    database=request["database_name"],
                    engine=target.engine,
                    requester_id=request["requester_slack_id"],
                    capture_plan=False,   # no PG-style EXPLAIN plan on SQL Server
                )
            except Exception:
                if not wrapped:
                    raise
                log.info("Request %s: the server refused the UDT re-projection "
                         "— running the original; those columns stay hex.",
                         request_id, exc_info=True)
                res = _execute_main_statement(
                    cur, s, i, request_id,
                    request["wants_result"], max_rows, max_csv_bytes,
                    result_format=result_format,
                    target_id=target.id,
                    database=request["database_name"],
                    engine=target.engine,
                    requester_id=request["requester_slack_id"],
                    capture_plan=False,
                )
            stmt_results.append(res)
        conn.commit()
        elapsed = time.monotonic() - t_start
    finally:
        conn.close()

    csv_paths = [r.csv_path for r in stmt_results if r.csv_path is not None]
    _finalize(client, request, stmt_results, csv_paths,
              max_csv_bytes=max_csv_bytes, target=target, elapsed=elapsed)


def _execute_main_statement(
    cur,
    stmt: query_safety.StatementInfo,
    index: int,
    request_id: int,
    wants_result: bool,
    max_rows: int,
    max_csv_bytes: int,
    *,
    # Who is reading. A masking exemption can be scoped to super-admins (the
    # operator's own dba.* toolkit), so the decision needs the reader; absent,
    # _load_exemptions gives the unprivileged answer.
    requester_id: str | None = None,
    result_format: str = "csv",
    target_id: int | None = None,
    database: str | None = None,
    engine: str = "postgres",
    capture_plan: bool = False,
    on_committed=None,
    force_extended: bool = False,
) -> _StmtResult:
    """Execute one main (non-SET) statement and capture its outcome.

    - EXPLAIN (when `capture_plan`): capture the plan as text for inline
      code-block delivery instead of a file.
    - DML without RETURNING (no result set): record rowcount.
    - SELECT / DML+RETURNING with wants_result: stream to its own
      file (CSV or XLSX, dispatched by `result_format`).
    - SELECT / DML+RETURNING without wants_result: drain + count.

    `on_committed`, if given, is called the instant the statement returns.
    In autocommit mode that is also the instant it commits, so the caller
    can record that a mutation is now durable before the result
    capture below — which can still fail — runs.

    `force_extended` (Postgres only) runs the statement over the extended
    protocol, which makes the SERVER refuse a string holding more than one
    command ("cannot insert multiple commands into a prepared statement").
    That is a wire-level backstop for the parser-differential class of bug:
    even if some future tokenizer gap hides a second statement from the
    classifier, Postgres will not execute it. Verified against this fleet
    that DDL, VACUUM and CREATE INDEX CONCURRENTLY all still work this way.
    Note psycopg treats an empty params tuple as "no params" and falls back
    to the simple protocol, so passing `()` would NOT have this effect.

    Read-only statements on Postgres are STREAMED rather than executed into a
    client-side result. A client cursor materialises the whole result set in the
    process before a single row is inspected, so `max_rows` capped what we
    WROTE but not what we HELD: one unbounded SELECT could exhaust the gateway's
    memory for every tenant on it. Measured on this fleet: reading 100 rows out
    of an 800k-row result costs ~1 MB streamed versus tens of MB materialised.
    `stream()` uses the extended protocol too, so the wire-level multi-command
    refusal above still applies. Only read-only leading keywords stream: the
    server reports no rowcount for a streamed statement, which DML needs.
    """
    # Ask the server how many statements it reads here, and refuse if that is
    # more than the one that was approved. On Postgres the extended protocol
    # below already makes the server refuse a second command, so this is a
    # no-op; on SQL Server there is no wire-level equivalent (measured: a
    # two-command batch runs unchallenged, parameterized or not), so the guard
    # compiles the batch with SHOWPLAN and counts. Before the portal, same
    # reason as the lineage lookup below.
    stmt_guard.check(cur, stmt.rewritten, engine=engine, request_id=request_id)

    # Ask the database where the result columns come from, BEFORE anything opens
    # a portal on this cursor. Two reasons it has to be here and not later:
    #
    #  - Issuing a second statement while `cur.stream()` holds a portal open
    #    blocks forever with no client-side timeout. That is the 2026-07-30
    #    outage, and it was a catalog lookup exactly like this one.
    #  - Same cursor, same transaction, so the view definitions the planner
    #    reads are the ones this statement is about to use. A view redefined
    #    between approval and a scheduled run cannot slip past.
    #
    # Only for masked results — no masking, no reason to plan twice. Read the
    # switch directly here: the `pii_found` sentinel is not built until after
    # execution, and waiting for it would put this after the portal opens.
    pii_lineage_map = None
    if wants_result and pii.is_enabled():
        pii_lineage_map = pii_lineage.source_columns(
            cur, stmt.rewritten, engine=engine)

    rows = cur          # what the row-consuming paths below iterate
    streamed = False
    if force_extended and stmt.leading.upper() in _STREAMABLE_LEADING:
        stream = cur.stream(stmt.rewritten)
        streamed = True
        # stream() is lazy and only fills cur.description once a row has been
        # produced, so pull one and put it back in front of the iterator.
        first = next(stream, _NO_ROW)
        rows = stream if first is _NO_ROW else itertools.chain([first], stream)
    elif force_extended:
        cur.execute(stmt.rewritten, prepare=True)
    else:
        cur.execute(stmt.rewritten)
    if on_committed is not None:
        on_committed()
    res = _StmtResult(index=index, leading=stmt.leading)

    if cur.description is None:
        if streamed:
            # A streamed read that matched NOTHING never produced a row, so
            # `description` was never populated — but it is still a result
            # set, just an empty one. Falling through to the DML branch made
            # it report the driver's rowcount, which is -1 for a streamed
            # statement: users saw "-1 rows affected" for a clean SELECT with
            # no matches.
            res.has_result_set = True
            res.rowcount = 0
            return res
        # DML without RETURNING. Clamp: a driver that cannot count reports
        # -1, which must never surface as a row count.
        rc = cur.rowcount
        res.rowcount = rc if isinstance(rc, int) and rc > 0 else 0
        return res

    res.has_result_set = True
    # DBAPI-portable column name (description[i][0]) — works for both psycopg
    # (Column supports indexing) and pyodbc (7-tuple), so this function is
    # driver-agnostic and the SQL Server path reuses it as-is.
    columns = [d[0] for d in cur.description]
    # ...and while the cursor is still open, the column TYPES. This is the only
    # moment they exist: the result is delivered as a CSV file and the web grid
    # reads that file back in another process, long after the cursor is gone.
    #
    # Reading `description` is free — it is already in the client. Naming the
    # user-defined types needs a catalog QUERY, and that must not happen here:
    # the result portal is open, and issuing a second query on this connection
    # while it is would queue behind the rest of the result. See
    # `_apply_resolved_types`, called once the stream is closed.
    res.col_types, _unknown_oids = _column_types(cur.description)

    # EXPLAIN: the result is a plan (a tree of text lines / a JSON blob),
    # not tabular data — deliver it inline as a fenced code block, which
    # is far more readable than a one-column CSV/XLSX and needs no
    # download. Handled before `wants_result` because the plan IS the
    # point of an EXPLAIN. Only for a lone EXPLAIN statement; a
    # multi-statement request keeps per-statement files. Plans carry no
    # queried row data (only the user's own query literals + cost/timing),
    # so PII masking does not apply.
    # Both early returns below pass `cur=None` deliberately: they may leave the
    # portal open (a plan read short, a row count that stopped at the cap), and
    # no path is allowed to run the catalog lookup in that state. With no cursor
    # the unresolved labels are dropped instead, so the grid falls back to the
    # schema catalog rather than rendering a bare OID.
    if capture_plan and stmt.leading == "EXPLAIN":
        res.plan_text, res.rowcount, res.truncated_rows = _read_plan_text(
            rows, columns)
        res.col_types = _apply_resolved_types(res.col_types, _unknown_oids, None)
        return res

    if not wants_result:
        rows_seen = 0
        for _r in rows:
            if rows_seen >= max_rows:
                res.truncated_rows = True
                break
            rows_seen += 1
        res.rowcount = rows_seen
        res.col_types = _apply_resolved_types(res.col_types, _unknown_oids, None)
        return res

    # PII masking: values are masked as they stream into the result
    # file, after the query ran normally. Two layers — content scan
    # (email/phone/tckn/vkn/iban/card by value) + column-name catalog
    # (name/address/... by column). `pii_found` accumulates which kinds
    # fired for the audit log + user hint. None when masking is disabled.
    pii_found: set | None = set() if pii.is_enabled() else None
    pii_skip: set[int] = set()
    pii_namescan: set[int] = set()
    if pii_found is not None and target_id is not None:
        # Public-data exemptions (pii_masking_exemptions): a target/db-wide
        # row (or an only-exempt-tables query) lifts masking entirely for
        # this statement; column-level rows exempt individual result
        # columns. "Soft" (keep_value_scan) column rows lift only the
        # column-name rule and keep the value scan. Fail-closed inside.
        try:
            skip_all, pii_skip = pii.exemption_decision(
                target_id, database or "", stmt.rewritten, columns,
                engine=engine, principal_id=requester_id)
            pii_namescan = pii.exemption_namescan(
                target_id, database or "", stmt.rewritten, columns,
                engine=engine, principal_id=requester_id)
        except Exception:
            log.exception("pii exemption check failed; keeping masking on")
            skip_all, pii_skip, pii_namescan = False, set(), set()
        if skip_all:
            pii_found = None
            res.pii_exempt = True
        elif pii_skip or pii_namescan:
            res.pii_exempt = True
    pii_cols = (pii.column_pii_map(columns, stmt.rewritten, engine=engine,
                                   lineage=pii_lineage_map)
                if pii_found is not None else {})
    # Column-level exemptions beat the column-name catalog. Full skips AND
    # soft (keep_value_scan) rows both drop the name rule; soft columns are
    # NOT added to skip_cols, so they fall through to the per-value scan.
    for i in pii_skip:
        pii_cols.pop(i, None)
    for i in pii_namescan:
        pii_cols.pop(i, None)
    if result_format == "xlsx":
        out_path, rows_written, t_rows, t_size = _stream_to_xlsx(
            request_id, columns, rows, max_rows, max_csv_bytes,
            suffix=f"_q{index}", pii_found=pii_found, pii_cols=pii_cols,
            pii_skip=pii_skip,
        )
    else:
        out_path, rows_written, t_rows, t_size = _stream_to_csv(
            request_id, columns, rows, max_rows, max_csv_bytes,
            suffix=f"_q{index}", pii_found=pii_found, pii_cols=pii_cols,
            pii_skip=pii_skip,
        )
    res.rowcount = rows_written
    res.truncated_rows = t_rows
    res.truncated_size = t_size
    res.pii_masked = pii_found or None

    # The result is written; the portal is not necessarily finished. Streaming
    # stops at the row or size cap, and breaking out of a `for` over a generator
    # leaves it SUSPENDED, not closed — the server is still holding the rest of
    # the result, waiting for us to ask for it. Close it explicitly before doing
    # anything else on this connection.
    #
    # This is the fix for a production wedge: the type lookup below used to run
    # before this point, so it queued behind the remainder of a truncated result
    # and blocked forever. One request took the only executor slot with it and
    # every later one sat in `approved`, which the UI renders as running.
    if streamed:
        try:
            stream.close()
        except Exception:
            log.debug("closing the result stream failed", exc_info=True)
    # Now safe: name the user-defined types (enum, domain, composite) that
    # psycopg could only give us as OIDs. Cosmetic, so failure just drops the
    # label and the grid falls back to the schema catalog.
    res.col_types = _apply_resolved_types(res.col_types, _unknown_oids, cur)

    # Empty result: don't ship a header-only file to the user — drop
    # it and let the completion handler use the no-result text path.
    if rows_written == 0 and not t_size:
        out_path.unlink(missing_ok=True)
        res.csv_path = None
    else:
        res.csv_path = out_path
    return res


def _stream_to_csv(
    request_id: int,
    columns: list[str],
    cur,
    max_rows: int,
    max_csv_bytes: int,
    suffix: str = "",
    pii_found: set | None = None,
    pii_cols: dict | None = None,
    pii_skip: set | None = None,
) -> tuple[Path, int, bool, bool]:
    """Stream rows from `cur` into a CSV file, capping by row count and
    cumulative byte size. Returns (path, rows_written, truncated_rows,
    truncated_size). When truncated_size is True the caller should delete
    the file and report the failure — the file contains a partial result
    and shouldn't be sent to the user.

    `suffix` is appended to the filename stem (e.g. "_q2") for
    multi-statement requests so each statement gets its own CSV.

    Each row is serialized via `csv.writer` to count bytes accurately
    (handles quoting / escaping). The file handle is held open for the
    duration of cursor iteration; we don't accumulate rows in memory."""
    CSV_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = CSV_DIR / f"req_{request_id}{suffix}_{ts}.csv"

    rows_written = 0
    truncated_rows = False
    truncated_size = False
    bytes_written = 0

    # `csv.writer` stringifies with `str()`, which prints six sub-second digits
    # for any datetime that has a non-zero microsecond — padding a SQL Server
    # DATETIME's three real digits out to `…36.520000`. This is the one seam that
    # fixes every reader (Slack CSV, Copy CSV, the web grid, which all read this
    # file). None when no column in the result needs it, which is the usual case.
    fmt_row = cell_format.row_formatter(getattr(cur, "description", None))

    with path.open("w", newline="", encoding="utf-8") as fh:
        _own_only(path)

        header_buf = io.StringIO()
        csv.writer(header_buf).writerow(columns)
        header_text = header_buf.getvalue()
        if len(header_text.encode("utf-8")) > max_csv_bytes:
            # Pathological wide schema (e.g. 10000 columns). Header alone
            # blows the cap.
            truncated_size = True
            return path, 0, False, True
        fh.write(header_text)
        bytes_written += len(header_text.encode("utf-8"))

        for r in cur:
            if rows_written >= max_rows:
                truncated_rows = True
                break
            if pii_found is not None:
                r = pii.mask_row(r, pii_found, pii_cols, skip_cols=pii_skip)
            if fmt_row is not None:
                r = fmt_row(r)
            r = [_neutralize_formula(v) for v in r]
            row_buf = io.StringIO()
            csv.writer(row_buf).writerow(r)
            row_text = row_buf.getvalue()
            row_bytes = len(row_text.encode("utf-8"))
            if bytes_written + row_bytes > max_csv_bytes:
                truncated_size = True
                break
            fh.write(row_text)
            bytes_written += row_bytes
            rows_written += 1

    return path, rows_written, truncated_rows, truncated_size


def _stream_to_xlsx(
    request_id: int,
    columns: list[str],
    cur,
    max_rows: int,
    max_csv_bytes: int,
    suffix: str = "",
    pii_found: set | None = None,
    pii_cols: dict | None = None,
    pii_skip: set | None = None,
) -> tuple[Path, int, bool, bool]:
    """XLSX counterpart of `_stream_to_csv`. Uses openpyxl's
    write-only Workbook so rows stream to disk without accumulating
    in memory — same memory profile as the CSV path. The size cap
    is checked against the file on disk after each row; xlsx is a
    ZIP archive so the on-disk size grows in spurts, which makes
    early-truncation slightly approximate but never overshoots
    by more than one row's compressed payload."""
    from openpyxl import Workbook  # local import: heavy dep, only for xlsx path

    CSV_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = CSV_DIR / f"req_{request_id}{suffix}_{ts}.xlsx"

    wb = Workbook(write_only=True)
    ws = wb.create_sheet(title="result")
    ws.append(columns)

    rows_written = 0
    truncated_rows = False
    truncated_size = False
    # A write-only workbook FINALIZES on the first `wb.save()` — appending
    # afterwards raises (StopIteration), so we must save exactly once, at the
    # end. Size is bounded by an uncompressed cell-text estimate as we stream
    # (XLSX is zip-compressed, so the file on disk is smaller — this errs on
    # the safe side and never overshoots the cap).
    est_bytes = sum(len(str(c)) for c in columns)

    # XLSX is a SEPARATE writer from the CSV one, so it needs the same
    # sub-second trim. It matters less here and for a different reason: a
    # `datetime` is handed to openpyxl NATIVELY (see _xlsx_cell), which is
    # better than any string we could produce — Excel holds the real value and
    # formats it. But a `time` has no native mapping, falls through to str(),
    # and so carried the same padded six digits the CSV path used to.
    fmt_row = cell_format.row_formatter(getattr(cur, "description", None),
                                        skip_types=(datetime,))

    for r in cur:
        if rows_written >= max_rows:
            truncated_rows = True
            break
        if pii_found is not None:
            r = pii.mask_row(r, pii_found, pii_cols, skip_cols=pii_skip)
        if fmt_row is not None:
            r = fmt_row(r)
        # Convert non-primitive values to str + neutralize formula injection
        # so openpyxl never raises and never emits a live formula cell.
        cells = [_xlsx_cell(v) for v in r]
        est_bytes += sum(len(str(c)) for c in cells if c is not None)
        if est_bytes > max_csv_bytes:
            truncated_size = True
            break
        ws.append(cells)
        rows_written += 1

    wb.save(path)   # write_only: exactly one finalize
    _own_only(path)
    return path, rows_written, truncated_rows, truncated_size


def _own_only(path: Path) -> None:
    """Restrict a result artifact to the service account (0600).

    Query results are the actual rows a DBA approved for one requester, and they
    sit on disk for `results_ttl_hours`. The default umask left them 0644, so any
    local account on the host could read every delivered result. Best-effort: a
    filesystem that does not support chmod must not fail the query whose result
    was already produced."""
    try:
        path.chmod(0o600)
    except OSError:
        log.warning("could not restrict permissions on %s", path, exc_info=True)


def _neutralize_formula(v):
    """CSV/spreadsheet formula-injection guard (OWASP): a *text* cell a
    spreadsheet could execute as a formula — one starting with '=', '+',
    '-', '@', a tab or a carriage return — is prefixed with a single quote
    so it is shown literally instead of evaluated. Non-strings (numbers,
    dates) pass through untouched. Applied AFTER PII masking, on both the
    CSV and XLSX export paths."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def _xlsx_cell(v):
    """Coerce a DB row value to something openpyxl can write directly, with
    formula injection neutralized. Numbers / datetime pass through; strings
    (and stringified non-primitives) are guarded so a leading '=' is stored
    as literal text, never an openpyxl formula."""
    if v is None:
        return None
    if isinstance(v, (int, float, bool, datetime)):
        return v
    return _neutralize_formula(v if isinstance(v, str) else str(v))


def _complete_no_result(
    client: WebClient,
    request: dict,
    rowcount: int,
    truncated: bool = False,
    elapsed: float | None = None,
    had_result_set: bool = False,
) -> None:
    """Final state for a request that produced no CSV. Two shapes:
      - had_result_set=False: DML without RETURNING. Uses "affected".
      - had_result_set=True:  SELECT or DML+RETURNING with 0 rows
        (caller dropped the empty CSV). Uses "returned" and explains
        why no file is attached when the user asked for a CSV."""
    with db.transaction() as cur:
        cur.execute(
            "UPDATE requests SET status = 'completed', completed_at = NOW(), "
            " row_count = %s, truncated = %s WHERE id = %s",
            (rowcount, truncated, request["id"]),
        )
        audit.log_in(cur, request["id"], None, None, "completed",
                     {"row_count": rowcount, "truncated": truncated,
                      "elapsed_sec": round(elapsed, 3) if elapsed is not None else None})

    rc_str = _fmt_count(rowcount)
    rs = "row" if rowcount == 1 else "rows"
    verb = "returned" if had_result_set else "affected"
    dur = f", {_fmt_duration(elapsed)}" if elapsed is not None else ""
    trunc_admin = ", truncated to max_rows" if truncated else ""
    trunc_user = ""
    if truncated:
        cap = row_limits.effective_caps(request["requester_slack_id"])[0]
        trunc_user = f" (showing first {_fmt_count(cap)} — result was larger)"

    # A read that matched nothing. Say so plainly instead of "0 rows
    # returned", and make clear no file is coming: an empty result never
    # ships a header-only CSV/XLSX even when one was requested (the file is
    # dropped in _execute_main_statement).
    no_result = had_result_set and rowcount == 0
    if no_result:
        fmt = (request.get("result_format") or "csv").upper()
        rc_str, rs, verb = "no", "rows", "matched"
        empty_csv_note = (f" (nothing to download — no {fmt} attached)"
                          if request.get("wants_result") else "")
    else:
        empty_csv_note = ""

    if request.get("bundle_id"):
        # Bundle item — bundle summary DM will land once every item is
        # terminal. Just refresh the admin bundle DM in place.
        notifications.update_bundle_admin_dms(client, request["bundle_id"])
        return

    notifications.update_all_admin_messages(
        client, request,
        f":white_check_mark: Approved by <@{request['decided_by_slack_id']}>{_fmt_approve_ts(request)} — "
        f"executed ({rc_str} {rs} {verb}{trunc_admin}{dur}).",
    )
    if _deliver_result_to_requester(request):
        notifications.dm_requester(
            client, request["requester_slack_id"],
            f":white_check_mark: *SQL query `#{request['id']}` completed* — "
            f"{rc_str} {rs} {verb}{trunc_user}{empty_csv_note}{dur}.\n"
            + notifications.request_context_md(request),
        )
        notifications.favorite_followup(client, request["requester_slack_id"], request["id"])
        ratings.maybe_prompt(client, request)


def _pii_hint(masked: list | None, exempted: bool = False) -> str:
    """One-line note for the result DM telling the requester their output
    was PII-masked (and/or that a public-data exemption lifted masking).
    Empty string when neither applies."""
    parts = []
    if masked:
        label = ", ".join(masked)
        parts.append(f"\n:lock: _Output PII-masked ({label}) — matching values "
                     f"are partially hidden in the result file._")
    if exempted:
        parts.append("\n:unlock: _PII masking skipped for (part of) this "
                     "result — public-data exemption._")
    return "".join(parts)


def _complete_with_csv(
    client: WebClient,
    request: dict,
    csv_path: Path,
    row_count: int,
    truncated: bool,
    elapsed: float | None = None,
    pii_masked: list | None = None,
    pii_exempt: bool = False,
    col_types: dict | None = None,
) -> None:
    with db.transaction() as cur:
        cur.execute(
            "UPDATE requests SET status = 'completed', completed_at = NOW(), "
            " row_count = %s, truncated = %s, csv_file_path = %s, "
            " result_column_types = %s WHERE id = %s",
            (row_count, truncated, str(csv_path),
             json.dumps(col_types) if col_types else None, request["id"]),
        )
        audit.log_in(cur, request["id"], None, None, "completed", {
            "row_count": row_count, "truncated": truncated, "csv": str(csv_path),
            "elapsed_sec": round(elapsed, 3) if elapsed is not None else None,
            "pii_masked": pii_masked,
        })

    rc_str = _fmt_count(row_count)
    rs = "row" if row_count == 1 else "rows"
    dur = f", {_fmt_duration(elapsed)}" if elapsed is not None else ""
    trunc_note = ""
    if truncated:
        cap = row_limits.effective_caps(request["requester_slack_id"])[0]
        trunc_note = f" (showing first {_fmt_count(cap)} — result was larger)"

    if request.get("bundle_id"):
        # Bundle item — defer the CSV upload until the bundle finishes
        # so the requester gets a single summary DM with every result
        # attached (instead of N drip messages). The local csv_path is
        # already stored in `requests.csv_file_path`; the aggregator
        # uploads from there.
        notifications.update_bundle_admin_dms(client, request["bundle_id"])
        return

    deliver = _deliver_result_to_requester(request)
    requester = request["requester_slack_id"]
    if deliver:
        opened = client.conversations_open(users=requester)
        channel = opened["channel"]["id"]
        note = (
            f":white_check_mark: *SQL query `#{request['id']}` completed* — "
            f"{rc_str} {rs}{trunc_note}{dur}.\n"
            + notifications.request_context_md(request)
            + _pii_hint(pii_masked, pii_exempt)
        )
        upload_resp = _upload_with_retry(
            client,
            channel=channel,
            file=str(csv_path),
            filename=csv_path.name,
            title=f"Request #{request['id']} result",
            initial_comment=note,
        )
        # Store the Slack file ID so the cleanup timer can files.delete it later.
        # files_upload_v2 returns either {"files":[{"id":...}]} or {"file":{"id":...}}
        # depending on slack-sdk version; handle both.
        file_id: str | None = None
        if upload_resp.get("files"):
            file_id = upload_resp["files"][0].get("id")
        elif upload_resp.get("file"):
            file_id = upload_resp["file"].get("id")
        if file_id:
            db.execute(
                "UPDATE requests SET slack_file_id = %s WHERE id = %s",
                (file_id, request["id"]),
            )

    notifications.update_all_admin_messages(
        client, request,
        f":white_check_mark: Approved by <@{request['decided_by_slack_id']}>{_fmt_approve_ts(request)} — "
        f"executed ({rc_str} {rs}"
        f"{', truncated to max_rows' if truncated else ''}"
        f"{dur}).",
    )
    if deliver:
        notifications.favorite_followup(client, request["requester_slack_id"], request["id"])
        ratings.maybe_prompt(client, request)


# Slack caps a section's text at 3000 chars; leave room for the ``` fences.
_PLAN_CHUNK_CHARS = 2800


def _plan_code_blocks(plan_text: str) -> list[dict]:
    """Wrap plan text in one or more fenced-code section blocks, each under
    Slack's per-section text limit, splitting on line boundaries (and
    hard-slicing any single line that is itself too long)."""
    if not plan_text:
        plan_text = "(empty plan)"
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0

    def flush():
        nonlocal buf, buf_len
        if buf:
            chunks.append("\n".join(buf))
            buf = []
            buf_len = 0

    for line in plan_text.split("\n"):
        while len(line) > _PLAN_CHUNK_CHARS:
            flush()
            chunks.append(line[:_PLAN_CHUNK_CHARS])
            line = line[_PLAN_CHUNK_CHARS:]
        if buf_len + len(line) + 1 > _PLAN_CHUNK_CHARS:
            flush()
        buf.append(line)
        buf_len += len(line) + 1
    flush()

    return [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"```\n{c}\n```"}}
        for c in chunks
    ]


def _complete_with_plan(
    client: WebClient,
    request: dict,
    plan_text: str,
    truncated: bool,
    elapsed: float | None = None,
) -> None:
    """Deliver an EXPLAIN plan inline as fenced code block(s) instead of a
    CSV/XLSX file. The plan is text; a code block preserves the tree
    indentation and needs no download."""
    line_count = (plan_text.count("\n") + 1) if plan_text else 0
    with db.transaction() as cur:
        cur.execute(
            "UPDATE requests SET status = 'completed', completed_at = NOW(), "
            " row_count = %s, truncated = %s WHERE id = %s",
            (line_count, truncated, request["id"]),
        )
        audit.log_in(cur, request["id"], None, None, "completed", {
            "explain_plan_lines": line_count, "truncated": truncated,
            "elapsed_sec": round(elapsed, 3) if elapsed is not None else None,
            "delivery": "code_block",
        })

    dur = f", {_fmt_duration(elapsed)}" if elapsed is not None else ""
    note = (
        f":white_check_mark: *SQL query `#{request['id']}` completed* — "
        f"query plan{dur}.\n"
        + notifications.request_context_md(request)
    )

    if request.get("bundle_id"):
        notifications.update_bundle_admin_dms(client, request["bundle_id"])
        return

    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": note}}]
    blocks += _plan_code_blocks(plan_text)
    if truncated:
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": (":scissors: _Plan truncated to fit Slack — raise "
                         "`bot_config.explain_max_chars` or narrow the query._"),
            }],
        })

    deliver = _deliver_result_to_requester(request)
    if deliver:
        notifications.dm_requester(
            client, request["requester_slack_id"],
            f":white_check_mark: SQL query #{request['id']} completed — query plan.",
            blocks=blocks,
        )
    notifications.update_all_admin_messages(
        client, request,
        f":white_check_mark: Approved by <@{request['decided_by_slack_id']}>{_fmt_approve_ts(request)} — "
        f"executed (query plan{dur}).",
    )
    if deliver:
        notifications.favorite_followup(client, request["requester_slack_id"], request["id"])
        ratings.maybe_prompt(client, request)


def _complete_multi(
    client: WebClient,
    request: dict,
    stmt_results: list,
    elapsed: float | None = None,
    pii_masked: list | None = None,
) -> None:
    """Finalize a multi-statement request. Builds a per-statement summary,
    zips any CSVs into one archive (if 2+) or uploads the single CSV as-is,
    and posts the user DM + admin update. Updates `requests` row with the
    sum of rowcounts and a single csv path (the zip / sole csv) for the
    cleanup job."""
    request_id = request["id"]
    csvs = [r for r in stmt_results if r.csv_path is not None]
    total_rows = sum(r.rowcount for r in stmt_results)
    any_truncated = any(r.truncated_rows for r in stmt_results)

    # Build the upload artifact: zip if 2+ csvs, plain csv if 1, none if 0.
    upload_path: Path | None = None
    if len(csvs) >= 2:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        zip_path = CSV_DIR / f"req_{request_id}_results_{ts}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            _own_only(zip_path)
            for r in csvs:
                zf.write(r.csv_path, arcname=r.csv_path.name)
        # Drop the source CSVs now that they are inside the zip.
        for r in csvs:
            r.csv_path.unlink(missing_ok=True)
        upload_path = zip_path
    elif len(csvs) == 1:
        upload_path = csvs[0].csv_path

    # Column types only when a SINGLE csv is the artifact. A zip of several
    # per-statement files is not pageable in the grid (/rows refuses anything
    # but .csv), so there is no header to annotate and no single statement whose
    # types would even be the right answer.
    multi_col_types = csvs[0].col_types if len(csvs) == 1 else None

    with db.transaction() as cur:
        cur.execute(
            "UPDATE requests SET status = 'completed', completed_at = NOW(), "
            " row_count = %s, truncated = %s, "
            " csv_file_path = %s, result_column_types = %s WHERE id = %s",
            (total_rows, any_truncated,
             str(upload_path) if upload_path else None,
             json.dumps(multi_col_types) if multi_col_types else None,
             request_id),
        )
        audit.log_in(cur, request_id, None, None, "completed", {
            "n_statements": len(stmt_results),
            "total_rows": total_rows,
            "truncated": any_truncated,
            "csv": str(upload_path) if upload_path else None,
            "elapsed_sec": round(elapsed, 3) if elapsed is not None else None,
            "pii_masked": pii_masked,
            "per_statement": [
                {"i": r.index, "leading": r.leading,
                 "rows": r.rowcount, "truncated": r.truncated_rows,
                 "csv": str(r.csv_path) if r.csv_path else None}
                for r in stmt_results
            ],
        })

    dur = f", {_fmt_duration(elapsed)}" if elapsed is not None else ""
    cap = row_limits.effective_caps(request["requester_slack_id"])[0]
    summary_lines = []
    for r in stmt_results:
        rc = _fmt_count(r.rowcount)
        rs = "row" if r.rowcount == 1 else "rows"
        suffix_bits: list[str] = []
        if r.truncated_rows:
            suffix_bits.append(f"showing first {_fmt_count(cap)} — result was larger")
        if r.csv_path is not None:
            suffix_bits.append("→ csv")
        suffix = f" ({'; '.join(suffix_bits)})" if suffix_bits else ""
        summary_lines.append(f"  • Statement {r.index} ({r.leading}): {rc} {rs}{suffix}")
    summary = "\n".join(summary_lines)

    if request.get("bundle_id"):
        # Bundle item — defer upload and per-item DM; the bundle
        # aggregator will collect everything and send a single summary.
        notifications.update_bundle_admin_dms(client, request["bundle_id"])
        return

    requester = request["requester_slack_id"]
    note_header = (
        f":white_check_mark: *SQL query `#{request_id}` completed* — "
        f"{len(stmt_results)} statements{dur}.\n"
        f"{summary}\n"
        + notifications.request_context_md(request)
        + _pii_hint(pii_masked, any(r.pii_exempt for r in stmt_results))
    )

    deliver = _deliver_result_to_requester(request)
    if deliver and upload_path is not None:
        opened = client.conversations_open(users=requester)
        channel = opened["channel"]["id"]
        upload_resp = _upload_with_retry(
            client,
            channel=channel,
            file=str(upload_path),
            filename=upload_path.name,
            title=f"Request #{request_id} results",
            initial_comment=note_header,
        )
        file_id: str | None = None
        if upload_resp.get("files"):
            file_id = upload_resp["files"][0].get("id")
        elif upload_resp.get("file"):
            file_id = upload_resp["file"].get("id")
        if file_id:
            db.execute(
                "UPDATE requests SET slack_file_id = %s WHERE id = %s",
                (file_id, request_id),
            )
    elif deliver:
        # No CSVs (all DML/DDL without RETURNING). Plain DM.
        notifications.dm_requester(client, requester, note_header)

    notifications.update_all_admin_messages(
        client, request,
        f":white_check_mark: Approved by <@{request['decided_by_slack_id']}>{_fmt_approve_ts(request)} — "
        f"executed {len(stmt_results)} statements ({_fmt_count(total_rows)} "
        f"rows total{dur}).",
    )
    if deliver:
        ratings.maybe_prompt(client, request)


def _escalate_to_dba(client: WebClient, request: dict, pg_error: str) -> None:
    """Move a DDL request to 'awaiting_dba_manual'. Notifies the admin
    DMs (with [Mark completed] / [Mark failed] buttons) and the
    requester. From here, only an admin click finalizes the request."""
    with db.transaction() as cur:
        cur.execute(
            "UPDATE requests SET status = 'awaiting_dba_manual', "
            "       error_message = %s WHERE id = %s",
            (f"requires DBA manual execution — {pg_error}", request["id"]),
        )
        audit.log_in(cur, request["id"], None, None, "escalated_to_dba",
                     {"pg_error": pg_error})

    if request.get("bundle_id"):
        # Bundle item — refresh the bundle DM (item card flips to
        # "awaiting manual DBA execution"). No per-item DM to requester
        # — bundle summary will cover it once the DBA closes the item.
        notifications.update_bundle_admin_dms(client, request["bundle_id"])
        return

    decided_by = request.get("decided_by_slack_id") or "(admin)"
    notifications.update_all_admin_messages(
        client, request,
        f":construction: Approved by <@{decided_by}>{_fmt_approve_ts(request)} — *DDL needs DBA "
        f"manual execution*. Run the query out-of-band with elevated "
        f"creds, then close out below.\n"
        f"_Reason: {pg_error}_",
        dba_manual=True,
    )
    if _deliver_result_to_requester(request):
        notifications.dm_requester(
            client, request["requester_slack_id"],
            f":construction: *SQL query `#{request['id']}` needs DBA-level "
            f"execution* — your DDL touches an object the bot's DDL role "
            f"can't modify directly. The DBA team has been notified and "
            f"will run it out-of-band. You'll be notified when it completes.\n"
            + notifications.request_context_md(request),
        )


def _alert_admins_size_cap(client: WebClient, request: dict, max_mb: int) -> None:
    """DM the admins when a result was too big for a Slack file (hit the
    size cap). The requester already got the failure DM via _fail; this is
    the operator heads-up to consider a bigger ceiling / row-size override /
    an S3 export. Best-effort — never raises into the exec path."""
    try:
        t = targets.get(request["target_server_id"])
        alias = t.alias if t else str(request["target_server_id"])
        text = (
            f":warning: SQL request `#{request['id']}` from "
            f"<@{request['requester_slack_id']}> hit the *{max_mb} MB* "
            f"result-size limit on `{alias}/{request['database_name']}` — the "
            f"result was too large for a Slack file, so it failed. Options: "
            f"raise `bot_config.csv_size_mb_ceiling`, grant a row/size "
            f"override, or an S3 export."
        )
        for admin in admins.list_active():
            try:
                notifications.dm_requester(client, admin["slack_user_id"], text)
            except Exception:
                log.exception("size-cap alert DM failed for admin %s",
                              admin["slack_user_id"])
    except Exception:
        log.exception("size-cap admin alert failed for request %s", request["id"])


def _fail(client: WebClient, request: dict, message: str) -> None:
    with db.transaction() as cur:
        cur.execute(
            "UPDATE requests SET status = 'failed', completed_at = NOW(), "
            " error_message = %s "
            " WHERE id = %s "
            "   AND status NOT IN ('completed', 'rejected', 'cancelled')",
            (message, request["id"]),
        )
        if cur.rowcount == 0:
            # Compare-and-set: the row already reached a terminal
            # success / closed state (it completed and only Slack delivery
            # failed, or an admin already closed it). Don't clobber it to
            # 'failed' or DM a misleading failure notice — just record the
            # swallowed error for forensics.
            audit.log_in(cur, request["id"], "SYSTEM", "fail guard",
                         "fail_suppressed", {"error": message})
            log.warning("Request %s: _fail suppressed — already terminal.",
                        request["id"])
            return
        audit.log_in(cur, request["id"], None, None, "failed",
                     {"error": message})

    if request.get("bundle_id"):
        notifications.update_bundle_admin_dms(client, request["bundle_id"])
        return

    notifications.update_all_admin_messages(
        client, request,
        f":warning: Approved by <@{request['decided_by_slack_id']}>{_fmt_approve_ts(request)} — "
        # `message` is already Slack mrkdwn (it carries its own code spans /
        # italics, e.g. the `_Postgres said:_` footer). Don't wrap it in a code
        # span — that both nests backticks (garbled render) and the old 200-char
        # cap sliced mid-word ("said" -> "sai`"). Emit it as-is, bounded.
        f"execution failed: {message[:2900]}",
    )
    if _deliver_result_to_requester(request):
        fail_text = (
            f":warning: *SQL query `#{request['id']}` failed*\n"
            + notifications.request_context_with_query_md(request)
            + f"\n*Error:*\n```{message}```"
        )
        notifications.dm_requester(
            client, request["requester_slack_id"], fail_text,
            blocks=notifications.resubmit_blocks(fail_text, request["id"]),
        )
        ratings.maybe_prompt(client, request)


def _complete_with_delivery_warning(
    client: WebClient, request: dict, message: str
) -> None:
    """Close a request whose mutation committed but whose result delivery or
    finalization then failed. The change IS applied, so record the
    row 'completed' — never 'failed', which would imply nothing happened and
    could prompt a duplicate re-run — and warn the requester that the change
    went through but the result could not be delivered."""
    with db.transaction() as cur:
        cur.execute(
            "UPDATE requests SET status = 'completed', completed_at = NOW(), "
            " error_message = %s "
            " WHERE id = %s AND status = 'executing'",
            (f"change applied; result delivery failed: {message}", request["id"]),
        )
        if cur.rowcount == 0:
            audit.log_in(cur, request["id"], "SYSTEM", "delivery guard",
                         "delivery_failed",
                         {"error": message, "note": "row not in 'executing'"})
            return
        audit.log_in(cur, request["id"], "SYSTEM", "delivery guard",
                     "completed_delivery_failed", {"error": message})

    # Notifications are best-effort: the delivery failure that brought us here
    # may well be Slack itself. The row is already 'completed', so a DM error
    # here doesn't change the outcome.
    try:
        if request.get("bundle_id"):
            notifications.update_bundle_admin_dms(client, request["bundle_id"])
        else:
            notifications.update_all_admin_messages(
                client, request,
                f":warning: Approved by <@{request['decided_by_slack_id']}>"
                f"{_fmt_approve_ts(request)} — change applied, but result "
                f"delivery failed: {message[:2900]}")
        if _deliver_result_to_requester(request):
            notifications.dm_requester(
                client, request["requester_slack_id"],
                f":warning: *SQL query `#{request['id']}` was applied* — but an "
                f"error occurred delivering the result:\n```{message}```\n"
                f"Your change is in effect; do *not* re-run it unless you mean "
                f"to apply it again.")
    except Exception:
        log.exception("Request %s: delivery-warning notification failed "
                      "(row already completed)", request["id"])


def reconcile_orphaned_executing() -> int:
    """Sweep lease-expired 'executing' rows to 'failed'. A request left in
    'executing' past its lease is orphaned: the process that ran it died
    (restart / crash / host migration) before it could write completed/failed,
    and a query can't outlive its connection — so nothing is actually running.
    Called both at boot and periodically from the scheduler loop, so orphans
    are cleaned within ~one tick of the lease expiring without needing a
    restart. Safe to call anytime (no-op when nothing is stuck). Returns the
    number reconciled. The status change fires the bundle-status trigger, so
    bundle rollups stay correct."""
    lease = cfg.get_int("execution_lease_sec", 900)
    # Only lease-expired rows are orphaned. A query that
    # started less than `execution_lease_sec` ago may still be running in a
    # LIVE sibling process — QueryHub Web and the bot run separate executors
    # against the same control DB — so failing a recent 'executing' row on
    # this process's boot / sweep would kill a healthy cross-process query.
    # The lease sits well above the max query lifetime (statement_timeout +
    # result streaming), so anything older is genuinely dead: its connection,
    # and therefore its query, cannot have survived.
    stuck = db.fetch_all(
        "SELECT id FROM requests WHERE status = 'executing' "
        "   AND (executed_at IS NULL "
        "        OR executed_at < NOW() - make_interval(secs => %s))",
        (lease,))
    if not stuck:
        return 0
    note = ("Orphaned: the process running this query stopped (restart / "
            "crash / migration) before it finished. Re-submit if still needed.")
    n = 0
    with db.transaction() as cur:
        for r in stuck:
            cur.execute(
                "UPDATE requests SET status = 'failed', completed_at = NOW(), "
                "       error_message = COALESCE(error_message, %s) "
                " WHERE id = %s AND status = 'executing' "
                "   AND (executed_at IS NULL "
                "        OR executed_at < NOW() - make_interval(secs => %s))",
                (note, r["id"], lease),
            )
            if cur.rowcount:
                n += 1
                audit.log_in(cur, r["id"], "SYSTEM", "orphan reconciler",
                             "execution_orphaned",
                             {"reason": "executing lease expired",
                              "lease_sec": lease})
    return n


def resubmit_approved_on_boot(client: WebClient) -> int:
    """Re-submit 'approved' requests that were never picked up.

    The worker pool is in-memory: a request handed to it but not yet started
    is lost if the process is hard-killed (crash / OOM / power loss). A
    graceful restart drains the pool, but a crash does not — leaving the row
    stuck in 'approved' forever, since the orphan reconciler only sweeps
    'executing'. At process start the pool is empty, so any request still
    'approved' is orphaned and safe to re-submit: the atomic claim and
    execution-time re-authorization in _run make re-submission idempotent.
    Call once at boot, in the process that executes, after the client exists.
    Returns the count re-submitted."""
    rows = db.fetch_all(
        "SELECT * FROM requests WHERE status = 'approved' ORDER BY id")
    for row in rows:
        log.info("re-submitting orphaned 'approved' request %s after restart",
                 row["id"])
        submit(row, client)
    return len(rows)


def shutdown() -> None:
    _pool.shutdown(wait=True, cancel_futures=False)


# =============================================================================
# Scheduler — dispatches due `scheduled` requests to the executor
# =============================================================================

_SCHEDULED_SELECT_COLS = (
    "id, requester_slack_id, requester_name, target_server_id, "
    "database_name, query, wants_result, result_format, justification, status, "
    "decided_by_slack_id, decided_by_name, decided_at, scheduled_for, "
    "requester_dm_channel_id, requester_dm_message_ts, "
    "bundle_id, position, origin"
)


def dispatch_due(client: WebClient, batch_limit: int = 50) -> int:
    """Atomically pick up scheduled requests that are due, flip them to
    'approved', and submit them to the worker pool. Returns the count
    dispatched. Safe to call repeatedly; uses FOR UPDATE SKIP LOCKED so
    multiple bot instances would not double-fire (we run a single instance
    today, but the lock keeps the door closed).

    Note the target state is 'approved', not 'executing'. Only _run's atomic
    claim may write 'executing', which makes that claim the single exclusive
    gate on execution; pre-flipping here used to let a second submission of the
    same request re-claim an already-'executing' row and run it twice. A
    consequence worth having: if this process dies between the flip and the
    claim, the row sits in 'approved' where boot recovery finds it, instead of
    waiting out the orphan lease in 'executing'.

    Honors bot_config.kill_switch — if set to 'on', dispatches NOTHING and
    leaves due rows in 'scheduled' state. They flow through naturally once
    kill switch returns to 'off' (just delayed)."""
    if (cfg.get_setting("kill_switch", "off") or "off").strip().lower() in (
        "on", "true", "yes", "1"
    ):
        return 0

    # State change + per-row audit in one transaction. If any audit fails,
    # the whole batch rolls back — the rows go back to 'scheduled' and
    # we'll retry on the next tick. Notifications and worker submission
    # happen AFTER commit (best-effort: a failed Slack DM doesn't undo the
    # state change, and a failed submit() leaves the row in 'approved', where
    # the next process start re-submits it).
    with db.transaction() as cur:
        cur.execute(
            f"UPDATE requests SET status = 'approved' "
            f"WHERE id IN ("
            f"  SELECT id FROM requests "
            f"  WHERE status = 'scheduled' AND scheduled_for <= NOW() "
            f"  ORDER BY scheduled_for "
            f"  FOR UPDATE SKIP LOCKED "
            f"  LIMIT %s "
            f") "
            f"RETURNING {_SCHEDULED_SELECT_COLS}",
            (batch_limit,),
        )
        rows = cur.fetchall()
        for row in rows:
            audit.log_in(cur, row["id"], None, None, "scheduled_dispatched",
                         {"trigger": "scheduler"})

    for row in rows:
        notifications.update_user_scheduled_dm(
            client, row,
            f":zap: *SQL query `#{row['id']}` is running now* (scheduled "
            f"start reached).",
        )
        submit(row, client)
    return len(rows)


# ===========================================================================
# CSV bulk import (COPY) — separate pipeline from the SQL query path
# ===========================================================================

IMPORT_DIR = Path("/var/lib/queryhub/imports")


def submit_import(import_row: dict, client: WebClient) -> None:
    """Schedule background execution of an approved CSV import."""
    _pool.submit(_import_run, import_row, client)


def _import_run(imp: dict, client: WebClient) -> None:
    """Bulk-load an approved CSV via COPY into the dba schema. New table
    -> CREATE [UNLOGGED] TABLE (all TEXT) then COPY; existing table ->
    COPY straight in. Runs with the target's DDL credentials in a single
    transaction (CREATE + COPY atomic). synchronous_commit is disabled
    LOCALly for speed; statement_timeout uses import_timeout_sec."""
    from . import csv_import as ci
    import_id = imp["id"]
    try:
        target = targets.get(imp["target_server_id"])
        if target is None:
            _import_fail(client, imp, "Target server not found at execution time.")
            return
        # CSV import is a Postgres-only COPY path; refuse any other engine.
        if target.engine != "postgres" or not engines.is_executable(target.engine):
            _import_fail(client, imp,
                         f"CSV import is not supported on the `{target.engine}` engine.")
            return

        # Re-authorize the REQUESTER now, not just at submit. An import runs on
        # DDL credentials and creates a table, so it is the most privileged
        # thing a non-admin can ask for — and approval-to-execution is exactly
        # the window in which a grant gets revoked or a leaver is offboarded.
        # The main query path got this in B5; this path did not, which is the
        # asymmetry the audit found. Same helpers, so there is one rule.
        requester = imp.get("requester_slack_id")
        if requester:
            from . import csv_import as _ci_auth
            if not _ci_auth.can_import(requester):
                _import_fail(client, imp,
                             "Your permission to import was removed after this "
                             "request was approved, so it was not run.")
                return
            if not teams.can_use_database(requester, target.id,
                                          imp["database_name"]):
                _import_fail(client, imp,
                             f"Your access to database `{imp['database_name']}` "
                             f"on `{target.alias}` was removed after this "
                             f"request was approved, so it was not run.")
                return
        try:
            db_user, password = targets.get_credentials(target.id, "ddl")
        except LookupError:
            _import_fail(client, imp,
                         f"Target `{target.alias}` has no DDL credentials "
                         f"configured (required for import). Contact the DBA team.")
            return
        if password == _SENTINEL_PASSWORD:
            _import_fail(client, imp,
                         f"Target `{target.alias}` is not ready yet. "
                         f"Please contact the DBA team.")
            return

        csv_path = Path(imp["csv_file_path"])
        if not csv_path.exists():
            _import_fail(client, imp,
                         "Uploaded CSV is no longer available (it may have "
                         "expired). Re-submit the import.")
            return

        # column_defs (user-supplied typed schema) wins over the all-TEXT
        # default. Both define the table's columns AND the COPY column list.
        col_defs = imp.get("column_defs")          # list[{name,type}] or None
        if col_defs:
            columns = [d["name"] for d in col_defs]
        else:
            columns = imp["columns"]               # list[str], normalized -> TEXT
        table = imp["table_name"]
        delimiter = imp["delimiter"]
        is_new = imp["is_new_table"]
        unlogged = imp["unlogged"]

        # Claim the import atomically, the way the main query path does. The
        # UPDATE used to be unconditional, so this state flip was a record of
        # what happened rather than a gate on it: an import handed to the pool
        # twice — or picked up by another process's boot recovery while it sat
        # 'approved' in this one's queue — would COPY the same file twice, and a
        # COPY into an existing table appends. rowcount==0 means someone else
        # owns it now (cancelled, rejected, or already claimed); abort quietly,
        # because whoever owns the new state owns the user-facing message.
        with db.transaction() as cur:
            cur.execute(
                "UPDATE csv_imports SET status='executing', executed_at=NOW() "
                "WHERE id=%s AND status='approved'", (import_id,),
            )
            if cur.rowcount == 0:
                log.info("import %s is no longer runnable at claim time "
                         "(cancelled/superseded/already claimed) — skipping",
                         import_id)
                return
            audit.log_in(cur, None, None, None, "import_execution_started", {
                "import_id": import_id, "table": f"{ci.IMPORT_SCHEMA}.{table}",
                "is_new_table": is_new, "row_count": imp.get("row_count"),
            })

        timeout_sec = cfg.get_int("import_timeout_sec", 600)
        app_name = f"queryhub-import id={import_id}"[:63]
        options = (
            f"-c statement_timeout={timeout_sec * 1000} "
            f"-c idle_in_transaction_session_timeout={(timeout_sec + 60) * 1000}"
        )

        tbl_sql = pgsql.Identifier(ci.IMPORT_SCHEMA, table)
        cols_sql = pgsql.SQL(", ").join(pgsql.Identifier(c) for c in columns)

        t_start = time.monotonic()
        with psycopg.connect(
            host=target.host, port=target.port, dbname=imp["database_name"],
            user=db_user, password=password, connect_timeout=15,
            application_name=app_name, **cfg.target_ssl_kwargs(), options=options,
        ) as conn:
            with conn.cursor() as cur:
                # Pin to the dba schema; speed knob for the bulk load.
                cur.execute("SET LOCAL search_path = dba, pg_catalog")
                cur.execute("SET LOCAL synchronous_commit = off")

                if is_new:
                    if col_defs:
                        # User-supplied types. Names are quoted identifiers;
                        # types come from the validated allow-list (no raw SQL).
                        coldefs = pgsql.SQL(", ").join(
                            pgsql.SQL("{} ").format(pgsql.Identifier(d["name"]))
                            + pgsql.SQL(d["type"])
                            for d in col_defs
                        )
                    else:
                        coldefs = pgsql.SQL(", ").join(
                            pgsql.SQL("{} text").format(pgsql.Identifier(c))
                            for c in columns
                        )
                    unlogged_kw = pgsql.SQL("UNLOGGED ") if unlogged else pgsql.SQL("")
                    cur.execute(pgsql.SQL("CREATE {}TABLE {} ({})").format(
                        unlogged_kw, tbl_sql, coldefs))

                copy_sql = pgsql.SQL(
                    "COPY {} ({}) FROM STDIN WITH "
                    "(FORMAT csv, HEADER true, DELIMITER {}, NULL '')"
                ).format(tbl_sql, cols_sql, pgsql.Literal(delimiter))

                with cur.copy(copy_sql) as cp:
                    with open(csv_path, "rb") as fh:
                        while chunk := fh.read(65536):
                            cp.write(chunk)
                inserted = cur.rowcount
                conn.commit()
        elapsed = time.monotonic() - t_start

        with db.transaction() as cur:
            cur.execute(
                "UPDATE csv_imports SET status='completed', completed_at=NOW(), "
                "inserted_rows=%s WHERE id=%s", (inserted, import_id),
            )
            audit.log_in(cur, None, None, None, "import_completed", {
                "import_id": import_id, "table": f"{ci.IMPORT_SCHEMA}.{table}",
                "inserted_rows": inserted,
                "elapsed_sec": round(elapsed, 2),
            })

        notifications.update_import_admin_messages(
            client, dict(imp, status="completed"),
            f":white_check_mark: Approved by <@{imp.get('decided_by_slack_id')}>"
            f" — imported {_fmt_count(inserted)} row(s) into "
            f"`dba.{table}` ({_fmt_duration(elapsed)}).",
        )
        verb = "created and loaded" if is_new else "loaded into"
        notifications.dm_requester(
            client, imp["requester_slack_id"],
            f":white_check_mark: *CSV import `#{import_id}` completed* — "
            f"{_fmt_count(inserted)} row(s) {verb} `dba.{table}` "
            f"on `{target.alias}/{imp['database_name']}` ({_fmt_duration(elapsed)}).",
        )

    except psycopg.errors.InsufficientPrivilege as e:
        _import_fail(client, imp,
                     f"The DDL role lacks privilege for this import: "
                     f"{errors.scrub(e)}")
    except Exception as e:  # noqa: BLE001
        log.exception("CSV import %s failed", import_id)
        msg = errors.scrub(e) if isinstance(e, psycopg.Error)\
            else errors.scrub(f"{type(e).__name__}: {e}")
        _import_fail(client, imp, msg)


def _import_fail(client: WebClient, imp: dict, reason: str) -> None:
    import_id = imp["id"]
    with db.transaction() as cur:
        cur.execute(
            "UPDATE csv_imports SET status='failed', completed_at=NOW(), "
            "error_message=%s WHERE id=%s", (reason[:2000], import_id),
        )
        audit.log_in(cur, None, None, None, "import_failed",
                     {"import_id": import_id, "error": reason[:500]})
    try:
        notifications.update_import_admin_messages(
            client, dict(imp, status="failed"),
            f":x: Import `#{import_id}` failed: {reason[:300]}",
        )
        notifications.dm_requester(
            client, imp["requester_slack_id"],
            f":x: *CSV import `#{import_id}` failed.*\n{reason[:1000]}",
        )
    except Exception:
        log.exception("import fail notification failed for %s", import_id)


def scheduler_loop(client: WebClient, stop_event, interval_sec: int = 60) -> None:
    """Run dispatch_due forever. Designed to run in a daemon thread; exits
    when `stop_event` is set."""
    while not stop_event.is_set():
        try:
            n = dispatch_due(client)
            if n:
                log.info("scheduler dispatched %d due request(s)", n)
        except Exception:
            log.exception("scheduler iteration failed")
        try:
            orphaned = reconcile_orphaned_executing()
            if orphaned:
                log.info("scheduler reconciled %d lease-expired 'executing' "
                         "request(s)", orphaned)
        except Exception:
            log.exception("scheduler orphan reconcile failed")
        # A different question from the orphan sweep above, and it has to be
        # asked separately: that one waits for the LEASE to expire because a
        # recent 'executing' row may belong to a healthy sibling process. This
        # one is wall-clock and unconditional, because a query blocked writing
        # results honours neither statement_timeout nor pg_cancel_backend, so a
        # runaway can outlive the lease while holding a production connection.
        try:
            runaway = cancellation.sweep_runaways()
            if runaway:
                log.warning("scheduler stopped %d runaway execution(s)", runaway)
        except Exception:
            log.exception("scheduler runaway sweep failed")
        # Reserved ids from query tabs nobody submitted. Cheap (partial index),
        # and it belongs here rather than in cron so the vanilla profile — which
        # runs no Slack cron jobs — cleans up too.
        try:
            from . import core_submit
            reaped = core_submit.reap_stale_drafts()
            if reaped:
                log.info("scheduler reaped %d stale draft request(s)", reaped)
        except Exception:
            log.exception("scheduler draft reap failed")
        stop_event.wait(interval_sec)
