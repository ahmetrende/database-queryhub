"""Where a result column actually comes from — asked of the database, not parsed.

The PII catalog matches column NAMES. That works until something renames the
column on the way out, and the parser in `pii._source_columns_by_position` closes
most of those doors: it follows aliases, casts, derived tables and CTEs back to
the physical column. It cannot follow a **view**, because a view's body is not in
the submitted SQL at all — it lives in the database's catalog.

Measured 2026-07-30, and this is the whole reason this module exists:

    CREATE VIEW v_cust AS
      SELECT full_name AS disp, address AS loc, birth_date AS dob FROM customers;
    SELECT disp, loc, dob FROM v_cust;

    static resolver -> [{'disp'}, {'loc'}, {'dob'}]     catalog match: {} (nothing)
    EXPLAIN VERBOSE -> customers.full_name, customers.address, customers.birth_date

So `name`, `address` and `birth_date` shipped in clear. The value scan still
caught email / phone / TCKN / IBAN / card by content, which is exactly why this
went unnoticed: the types that look like something were fine, and the types that
only a name can identify were not.

The planner already knows the answer. `EXPLAIN (VERBOSE, FORMAT JSON, COSTS OFF)`
puts an `Output` array on the root node that is **positionally identical** to the
result columns and names the true source relation and column — through views,
aliases, derived tables, CTEs and expressions alike. Asking it is not a
reimplementation of the planner; it IS the planner.

**This never replaces the other two rules, it adds to them.** The map is a union
of output-name matches, static lineage and this, so a failure here can only lose
the extra coverage — never unmask something already caught. That is a deliberate
choice over failing closed: a masking improvement must not become a reason a
query cannot run.

Engine-modular: `_RESOLVERS` maps an engine id to its implementation. Postgres is
implemented; SQL Server has an equivalent (`SET SHOWPLAN_XML ON` emits
`<ColumnReference Table Column>`) and is left unimplemented rather than guessed,
so it degrades to the static path with a note instead of pretending.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# Planning is cheap, but a pathological query can make it slow, and this runs on
# the critical path of every masked result. SET LOCAL inside the savepoint, so it
# disappears with the rollback and can never leak onto the real statement.
_PLAN_TIMEOUT_MS = 5000

# Only statements whose result set we would mask. DML is deliberately excluded:
# EXPLAIN without ANALYZE does not execute it, but there is nothing to gain — a
# RETURNING clause names its columns in the statement text, which the static
# resolver already reads — and every statement we EXPLAIN is one more thing that
# can go wrong on a write path.
_LINEAGE_WORTHY = re.compile(r"^\s*(?:SELECT|WITH|TABLE|VALUES)\b", re.I)


def _pg_output_array(plan: list | dict) -> list[str] | None:
    """The root `Output` of an EXPLAIN JSON plan.

    Depth-first from the top, because the outermost node that HAS an Output is
    the one whose columns are the result's. A Limit or Sort above the projection
    carries the same list; a node without one (some Append shapes) delegates to
    its children, so descend rather than give up.
    """
    node = plan
    if isinstance(node, list):
        node = node[0] if node else None
    if isinstance(node, dict) and "Plan" in node:
        node = node["Plan"]
    if not isinstance(node, dict):
        return None

    def walk(n):
        if not isinstance(n, dict):
            return None
        out = n.get("Output")
        if isinstance(out, list) and out:
            return out
        for child in (n.get("Plans") or []):
            got = walk(child)
            if got:
                return got
        return None

    return walk(node)


# An Output entry is an expression, not always a bare reference:
#   customers.full_name          -> full_name
#   (customers.first || ' ')     -> first
#   upper((c.email)::text)       -> email
# So pull every dotted-or-bare identifier out of it rather than trying to parse
# the expression: this feeds a name-matching catalog, and extra candidate names
# only ever cause MORE checking.
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
# Words that appear in plan expressions but are never column names. Keeping this
# list short on purpose — a false candidate costs one catalog lookup, while a
# missing one costs a masked column.
_NOISE = frozenset({"null", "true", "false", "case", "when", "then", "else",
                    "end", "and", "or", "not", "text", "int", "int4", "int8",
                    "numeric", "varchar", "bool", "date", "timestamp",
                    "timestamptz", "json", "jsonb", "count", "sum", "avg",
                    "min", "max", "coalesce", "upper", "lower", "cast"})


def _names_in(expr: str) -> set[str]:
    out = set()
    for m in _IDENT.finditer(expr or ""):
        w = m.group(0).lower()
        if w not in _NOISE:
            out.add(w)
    return out


def _postgres(cur, sql: str, n_columns: int | None) -> list[set[str]] | None:
    """Postgres lineage via EXPLAIN VERBOSE.

    Runs inside a SAVEPOINT and **always rolls back to it**, which is doing two
    jobs. A failed statement aborts a Postgres transaction, so without the
    savepoint an EXPLAIN this module could not plan would take the user's real
    query down with it ("current transaction is aborted") — a masking
    improvement turning into a query-killer. And rolling back even on SUCCESS
    discards the `SET LOCAL statement_timeout`, so the 5s planning budget cannot
    leak onto a legitimately long-running main statement.

    The plan is already in Python by then; there is nothing in the transaction
    worth keeping.
    """
    try:
        cur.execute("SAVEPOINT qh_pii_lineage")
    except Exception:
        log.debug("lineage: could not open a savepoint; skipping", exc_info=True)
        return None
    try:
        cur.execute(f"SET LOCAL statement_timeout = {_PLAN_TIMEOUT_MS}")
        cur.execute("EXPLAIN (VERBOSE, FORMAT JSON, COSTS OFF) " + sql)
        row = cur.fetchone()
        plan = row[0] if isinstance(row, (list, tuple)) else row
        # dict_row cursors hand back {'QUERY PLAN': [...]}.
        if isinstance(plan, dict):
            plan = next(iter(plan.values()), None)
        out = _pg_output_array(plan)
    except Exception as e:
        log.info("lineage: EXPLAIN did not resolve (%s) — falling back to the "
                 "static resolver for this statement", type(e).__name__)
        out = None
    finally:
        try:
            cur.execute("ROLLBACK TO SAVEPOINT qh_pii_lineage")
        except Exception:
            log.warning("lineage: savepoint rollback failed", exc_info=True)
    if not out:
        return None
    if n_columns is not None and len(out) != n_columns:
        # Positional identity is the entire premise. If the arity disagrees the
        # mapping cannot be trusted, and a wrong mapping is worse than none: it
        # would mask an innocent column and leave the real one alone.
        log.info("lineage: plan output arity %d != %d result columns — ignoring",
                 len(out), n_columns)
        return None
    return [_names_in(e) for e in out]


_RESOLVERS = {
    "postgres": _postgres,
    # "mssql": SET SHOWPLAN_XML ON emits <ColumnReference Table= Column=>, which
    # would work the same way. Unimplemented rather than approximated.
}


def supported(engine: str | None) -> bool:
    return (engine or "postgres") in _RESOLVERS


def source_columns(cur, sql: str, *, engine: str = "postgres",
                   n_columns: int | None = None) -> list[set[str]] | None:
    """Source column names per result position, or None when unavailable.

    MUST be called before the caller opens a result portal on `cur`. Issuing a
    second statement while `cur.stream()` has one open blocks forever with no
    client-side timeout — that is how the 2026-07-30 outage happened, and this
    function runs on the same cursor by design (so the view definitions it reads
    are the ones the real statement will use, in the same transaction).

    Never raises.
    """
    if not sql or not _LINEAGE_WORTHY.match(sql):
        return None
    fn = _RESOLVERS.get(engine or "postgres")
    if fn is None:
        return None
    try:
        return fn(cur, sql, n_columns)
    except Exception:
        log.debug("lineage resolver raised", exc_info=True)
        return None
