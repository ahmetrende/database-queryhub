"""Pre-flight EXPLAIN check before a /sql submission becomes a real request.

We connect to the target with the credentials that match the query's mode
(ro/rw/ddl) and ask Postgres to plan the query without executing it. This
catches the common "submitted, admin approved, executor exploded" loop:

    - typos that sqlparse/safety_analyzer accepts (e.g. column names)
    - references to tables/views the role doesn't see
    - relation missing on this target

Fail-OPEN on transport / auth errors: if we can't reach the target right
now, we don't block the user — they get the same error at execution time
anyway, and a flaky network shouldn't add a sharp UX wall in the modal.

Toggle via `bot_config.pre_flight_explain` (default 'on'). Plan capture
toggle is `bot_config.query_plan_logging` (default 'off')."""
from __future__ import annotations

import logging

import psycopg
import sqlglot

from . import config as cfg
from . import engines
from . import errors
from . import targets

log = logging.getLogger(__name__)


def is_enabled() -> bool:
    return _truthy(cfg.get_setting("pre_flight_explain", "on"))


def is_plan_logging_enabled() -> bool:
    return _truthy(cfg.get_setting("query_plan_logging", "off"))


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"on", "true", "yes", "1"}


# Leading keywords Postgres cannot EXPLAIN. ALTER / CREATE (non-AS) /
# DROP / TRUNCATE / GRANT etc. yield a misleading "syntax error at or
# near ALTER" if we try. We skip pre-flight for these and the executor
# surfaces real errors at run time after approval.
_NON_EXPLAINABLE_LEADING = {
    "ALTER", "DROP", "TRUNCATE", "GRANT", "REVOKE", "COMMENT",
    "VACUUM", "ANALYZE", "REINDEX", "CLUSTER", "REFRESH", "REASSIGN",
    "SET", "RESET", "SHOW", "BEGIN", "COMMIT", "ROLLBACK", "CALL", "DO",
    # EXPLAIN itself can't be pre-EXPLAIN'd: wrapping "EXPLAIN ANALYZE
    # SELECT ..." in another EXPLAIN yields "EXPLAIN (...) EXPLAIN ..." →
    # a syntax error that would falsely reject a valid EXPLAIN submission.
    # Skip pre-flight for it; the executor surfaces real errors at run time.
    "EXPLAIN",
}


def is_explainable(query: str) -> bool:
    """True if Postgres can EXPLAIN this statement. SELECT / WITH / INSERT
    / UPDATE / DELETE / MERGE / VALUES / TABLE plan fine; most DDL does
    not. CREATE TABLE AS / CREATE MATERIALIZED VIEW AS are explainable
    but we keep CREATE out of the allow-set to stay conservative."""
    head = (query or "").strip().split(None, 1)
    if not head:
        return False
    leading = head[0].upper().strip("(")
    return leading not in _NON_EXPLAINABLE_LEADING and leading != "CREATE"


def explain(
    target_id: int,
    database: str,
    mode: str,
    query: str,
    *,
    summary: bool = False,
    allow_write: bool = False,
) -> tuple[bool, str | None, list | None]:
    """Run EXPLAIN against the target. Returns (ok, error, plan).
      ok=True,  error=None, plan=list-or-None  → query parses+plans cleanly.
      ok=False, error=str,  plan=None          → surfaceable error to user.

    Plan is the JSON output of `EXPLAIN (FORMAT JSON ...)` (a list with a
    single dict, per Postgres convention). We always run with FORMAT JSON
    for parseability — it's cheap.

    RO ONLY. The caller must pass mode='ro'; any other mode is skipped
    (returns ok=True, plan=None) because:
      - `EXPLAIN INSERT/UPDATE/DELETE` is rejected in a READ ONLY
        transaction and would EXECUTE if run read-write — and only the
        first statement of a multi-statement payload gets the EXPLAIN
        prefix, so the rest would run before approval.
      - DDL can't be EXPLAIN'd by Postgres at all.

    Multi-statement queries are also skipped: EXPLAIN prefixes only the
    first statement, so the rest would execute. We only EXPLAIN a single
    RO statement.

    A 2s statement_timeout keeps the whole pre-flight inside Slack's 3s
    ack() deadline and blocks planner-bomb DoS via long-planning queries.
    """
    # The Slack submit path only pre-EXPLAINs a single RO statement
    # (allow_write=False). The web /explain opts in with allow_write=True to
    # also plan a single RW statement: EXPLAIN runs with ANALYZE OFF, so the
    # write is planned but NEVER executed — it just needs a read-WRITE session
    # (a read-only txn rejects a data-modifying statement even under EXPLAIN).
    # DDL is never explainable and is filtered by the caller + is_explainable.
    if mode == "ro":
        session_read_only = True
    elif mode == "rw" and allow_write:
        session_read_only = False
    else:
        return True, None, None
    try:
        stmts = [s for s in sqlglot.parse(query, read="postgres") if s is not None]
        if len(stmts) != 1:
            return True, None, None  # multi-statement / empty → skip
    except Exception:
        # Parse trouble — let the real EXPLAIN below surface any error.
        pass

    target = targets.get(target_id)
    if target is None:
        return False, f"target_id={target_id} not found", None

    # Pre-flight EXPLAIN (FORMAT JSON ...) is Postgres syntax. For an engine
    # that doesn't support it (SQL Server, ClickHouse), no-op (fail-open) —
    # the engine-aware safety layer is the real guard, and running a PG
    # EXPLAIN against a T-SQL target would just error.
    if not engines.spec(target.engine).supports_explain:
        return True, None, None

    try:
        db_user, password = targets.get_credentials(target_id, mode)
    except LookupError as e:
        return False, str(e), None

    # SUMMARY ON adds "Planning Time" without executing (the web plan view
    # shows it); it is off by default so the Slack submit path is byte-for-
    # byte unchanged. ANALYZE stays OFF either way — nothing runs.
    _summary = ", SUMMARY ON" if summary else ""
    explain_sql = (
        f"EXPLAIN (FORMAT JSON, ANALYZE OFF, COSTS ON, BUFFERS OFF{_summary}) "
        + query
    )

    try:
        with psycopg.connect(
            host=target.host,
            port=target.port,
            dbname=database,
            user=db_user,
            password=password,
            connect_timeout=2,
            **cfg.target_ssl_kwargs(),
            application_name=f"dba-slack-bot:explain target={target.alias}"[:63],
            # Tight 2s caps so the whole pre-flight fits inside Slack's
            # 3s ack() deadline on view_submission. Planner-slow queries
            # (e.g. heavily-partitioned tables) trip statement_timeout
            # and fail-open below — request still gets through, just
            # without the typo / missing-relation early-warning.
            options="-c statement_timeout=2000 -c idle_in_transaction_session_timeout=3000",
        ) as conn, conn.cursor() as cur:
            # Belt-and-suspenders: for an RO statement pin the transaction
            # read-only too (RO creds already can't write). An RW plan preview
            # (allow_write) must stay read-WRITE so EXPLAIN accepts the
            # statement type — ANALYZE OFF still means nothing executes.
            if session_read_only:
                cur.execute("SET TRANSACTION READ ONLY")
            # Pin search_path so the planner doesn't resolve unqualified
            # identifiers via attacker-controlled schemas during EXPLAIN.
            # MUST match the executor's order (pg_catalog FIRST) — with
            # `public` first, a same-named object in a writable schema can
            # shadow a built-in (CVE-2018-1058 class), and, worse, submit-time
            # EXPLAIN would resolve a name differently from the run that the
            # admin then approves.
            cur.execute("SET LOCAL search_path = pg_catalog, public")
            cur.execute(explain_sql)
            row = cur.fetchone()
            plan = row[0] if row else None
            return True, None, plan
    except psycopg.OperationalError as e:
        # Network / auth / timeout — fail-open. User isn't punished for our
        # transport problems; if it's still broken at execution time the
        # executor will surface the real error.
        log.warning(
            "pre-flight EXPLAIN fail-open on operational error "
            "(target=%s db=%s mode=%s): %s",
            target.alias, database, mode, e,
        )
        return True, None, None
    except psycopg.Error as e:
        # Real SQL-level error: syntax / permission / missing-relation /
        # invalid type. Surface it.
        return False, errors.scrub(e), None


# Leading keywords whose affected-row count an admin wants before
# approving — these all produce a `ModifyTable` plan node.
_WRITE_LEADING = {"UPDATE", "DELETE", "INSERT", "MERGE"}


def explain_write_estimate(
    target_id: int,
    database: str,
    mode: str,
    query: str,
) -> str | None:
    """Estimated affected-row hint for a single data-modifying statement
    (UPDATE / DELETE / INSERT / MERGE), or None when unavailable.

    The estimate matters most for exactly the queries `explain()` skips:
    a reviewer approving a DELETE wants "~X rows affected" up front. We
    get it without running the write:

      - `EXPLAIN` WITHOUT `ANALYZE` only PLANS; it never executes the
        statement. The top `ModifyTable` node carries `Plan Rows` =
        estimated affected rows.
      - We use the rw credential (it has the privilege to *plan* the
        write; the ro role would hit "permission denied") but pin the
        transaction `READ ONLY`, so even an accidental write-path — or
        the tail of a multi-statement payload, which only the first
        statement's EXPLAIN prefix would spare — is rejected by Postgres
        rather than executed.
      - Single statement only (belt to the READ ONLY suspenders).
      - Fail-OPEN on any trouble: a missing hint must never block a
        submission.
    """
    if mode != "rw":
        return None
    try:
        stmts = [s for s in sqlglot.parse(query, read="postgres") if s is not None]
        if len(stmts) != 1:
            return None
    except Exception:
        return None
    head = (query or "").strip().split(None, 1)
    if not head or head[0].upper().strip("(") not in _WRITE_LEADING:
        return None

    target = targets.get(target_id)
    if target is None:
        return None
    try:
        db_user, password = targets.get_credentials(target_id, "rw")
    except LookupError:
        return None

    explain_sql = "EXPLAIN (FORMAT JSON, ANALYZE OFF, COSTS ON, BUFFERS OFF) " + query
    try:
        with psycopg.connect(
            host=target.host,
            port=target.port,
            dbname=database,
            user=db_user,
            password=password,
            connect_timeout=2,
            **cfg.target_ssl_kwargs(),
            application_name=f"dba-slack-bot:explain-write target={target.alias}"[:63],
            options="-c statement_timeout=2000 -c idle_in_transaction_session_timeout=3000",
        ) as conn, conn.cursor() as cur:
            # The write never executes: EXPLAIN-only plus a READ ONLY txn.
            cur.execute("SET TRANSACTION READ ONLY")
            # Same order as the executor (pg_catalog first) — see the note on
            # the read path above.
            cur.execute("SET LOCAL search_path = pg_catalog, public")
            cur.execute(explain_sql)
            row = cur.fetchone()
            plan = row[0] if row else None
    except psycopg.Error as e:
        # Includes the READ ONLY rejection if a multi-statement tail ever
        # reached here — fail-open, the executor surfaces real errors later.
        log.warning("write-estimate fail-open (target=%s db=%s): %s",
                    target.alias, database, e)
        return None

    rows = _affected_rows(plan)
    if rows is None:
        return None
    verb = head[0].upper().strip("(")
    return f":pencil2: Est. ~{_fmt_int(rows)} rows affected ({verb.title()})"


def _affected_rows(plan: list | None) -> int | None:
    """Estimated affected-row count from an EXPLAIN plan of a write, or
    None if the plan is unparseable.

    A ModifyTable node reports 0 output rows without RETURNING, so the
    estimate is the rows its child scan feeds it. With RETURNING the root
    carries the count, so we fall back to the root estimate."""
    if not plan or not isinstance(plan, list) or not isinstance(plan[0], dict):
        return None
    root = plan[0].get("Plan") or {}
    rows = int(root.get("Plan Rows") or 0)
    if root.get("Node Type") == "ModifyTable":
        child_rows = sum(int(ch.get("Plan Rows") or 0)
                         for ch in (root.get("Plans") or []))
        if child_rows:
            rows = child_rows
    return rows


# ---------------------------------------------------------------------------
# Risk analysis — turn an EXPLAIN plan into a short admin-facing hint.
# ---------------------------------------------------------------------------


def _walk_plan_nodes(node: dict):
    """Yield every plan node depth-first, starting at `node`."""
    if not isinstance(node, dict):
        return
    yield node
    for child in node.get("Plans", []) or []:
        yield from _walk_plan_nodes(child)


def analyze_plan(plan: list | None) -> dict | None:
    """Reduce an EXPLAIN (FORMAT JSON) plan to a risk summary dict:

        {
          "total_cost":  float,    # top node Total Cost
          "est_rows":    int,      # top node Plan Rows (estimated output)
          "seq_scans":   [(relation, est_rows), ...],  # all Seq Scan nodes
          "flags":       ["seq_scan_large", "high_cost"],
          "has_risk":    bool,
        }

    Returns None when the plan is missing or unparseable (caller treats
    that as 'no hint available'). Thresholds come from bot_config so an
    operator can tune sensitivity without a redeploy."""
    if not plan or not isinstance(plan, list):
        return None
    root = plan[0].get("Plan") if isinstance(plan[0], dict) else None
    if not isinstance(root, dict):
        return None

    seq_rows_threshold = cfg.get_int("risk_seq_scan_rows", 100000)
    high_cost_threshold = cfg.get_int("risk_high_cost", 50000)

    total_cost = float(root.get("Total Cost") or 0.0)
    est_rows = int(root.get("Plan Rows") or 0)
    est_width = int(root.get("Plan Width") or 0)   # bytes per output row
    est_bytes = est_rows * est_width               # estimated result size

    seq_scans: list[tuple[str, int]] = []
    for n in _walk_plan_nodes(root):
        if n.get("Node Type") == "Seq Scan":
            rel = n.get("Relation Name") or "?"
            rows = int(n.get("Plan Rows") or 0)
            seq_scans.append((rel, rows))

    flags: list[str] = []
    if any(rows >= seq_rows_threshold for _, rows in seq_scans):
        flags.append("seq_scan_large")
    if total_cost >= high_cost_threshold:
        flags.append("high_cost")

    return {
        "total_cost": total_cost,
        "cost_band": _cost_band(total_cost),
        "est_rows": est_rows,
        "est_bytes": est_bytes,
        "seq_scans": seq_scans,
        "flags": flags,
        "has_risk": bool(flags),
    }


def _fmt_int(n: int) -> str:
    """Compact human count: 1234 → 1.2K, 5200000 → 5.2M."""
    if n is None:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _fmt_bytes(n: int) -> str:
    """Compact human size: 2048 → 2 KB, 357000000 → 340 MB."""
    if not n:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    i = 0
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    if i == 0:
        return f"{int(size)} {units[i]}"
    return f"{size:.0f} {units[i]}" if size >= 10 else f"{size:.1f} {units[i]}"


def _cost_band(cost: float) -> str:
    """Translate the abstract planner cost into a t-shirt size.
    PostgreSQL cost has no real-world unit (it's relative to a single
    sequential page read = 1.0), so a size reads far better than the
    raw number. Boundaries: trivial point-lookups land in XS/S, big
    table scans in L/XL."""
    if cost < 100:
        return "XS"
    if cost < 1_000:
        return "S"
    if cost < 50_000:
        return "M"
    if cost < 1_000_000:
        return "L"
    return "XL"


def risk_summary_text(plan: list | None) -> str | None:
    """Build a one-line, Slack-mrkdwn risk hint from an EXPLAIN plan.
    Returns None when there's no plan (DDL / fail-open) so the caller
    can omit the line entirely rather than print a noisy placeholder.

    The abstract planner cost is never shown raw — it's translated into
    an estimated result size (rows × width) plus a weight band, both of
    which mean something to a human reviewer.

    Examples:
      :bar_chart: Index Scan · ~12 rows (~2 KB) · light
      :warning: Seq scan on `transactions` (~5.2M rows, ~340 MB) · very heavy
    """
    a = analyze_plan(plan)
    if a is None:
        return None

    rows_str = _fmt_int(a["est_rows"])
    size_str = _fmt_bytes(a["est_bytes"])
    band = a["cost_band"]
    # Raw planner cost kept as a trailing technical reference for DBAs
    # who read EXPLAIN output directly; the size + band carry the
    # human-readable meaning ahead of it.
    cost_ref = f"cost {_fmt_int(int(a['total_cost']))}"

    if a["has_risk"]:
        bits = []
        if "seq_scan_large" in a["flags"] and a["seq_scans"]:
            # Name the biggest seq-scanned relation.
            rel, rows = max(a["seq_scans"], key=lambda x: x[1])
            bits.append(f"Seq scan on `{rel}` (~{_fmt_int(rows)} rows, ~{size_str})")
        else:
            bits.append(f"~{rows_str} rows (~{size_str})")
        detail = " · ".join(bits)
        return f":warning: {detail} · size `{band}` ({cost_ref})"

    # Clean plan — still show the headline so the admin always has a
    # "what will this do" line (per the always-show decision).
    node_type = "Plan"
    if plan and isinstance(plan[0], dict):
        root = plan[0].get("Plan") or {}
        node_type = root.get("Node Type") or "Plan"
    return f":bar_chart: {node_type} · ~{rows_str} rows (~{size_str}) · size `{band}` ({cost_ref})"
