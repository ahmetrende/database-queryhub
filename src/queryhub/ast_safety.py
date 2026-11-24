"""Second-pass safety analysis on the SQL AST.

The regex / sqlparse layer in `query_safety.analyze()` already catches
leading-keyword bans, multi-statement traps, missing WHEREs, and
tautology patterns. This module adds an AST-aware layer using
sqlglot (PostgreSQL dialect) for things the regex layer can't see:

- Obfuscation: a query that doesn't parse cleanly is rejected. Defense
  in depth — most bypass attempts (unbalanced quotes, comment tricks,
  malformed UNION) won't parse, so a parse failure is signal.
- Dangerous Postgres-specific functions: `pg_read_file`,
  `pg_read_server_files`, `pg_ls_dir`, `lo_import`, `lo_export`,
  `dblink_*`, `pg_terminate_backend`, `pg_cancel_backend`,
  `pg_reload_conf`, `pg_promote`. These are either filesystem /
  cluster-control side channels or RCE-adjacent.
- `COPY ... TO PROGRAM` / `COPY ... FROM PROGRAM` — runs shell on the
  server. Never appropriate for an end-user query.
- Long `pg_sleep` calls — a DoS lever even on RO connections.

The bot runs this AFTER the regex pass, only when
`bot_config.ast_safety_enabled` is `on` (default). If the flag is
off the module is a no-op — easy escape hatch for an operator
chasing a false-positive.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Iterable

import sqlglot
from sqlglot import exp

from . import config as cfg
from . import engines

log = logging.getLogger(__name__)


def fingerprint(sql: str, engine: str = "postgres") -> str | None:
    """Parameter-agnostic fingerprint of a query for the approval cache.

    Parse with sqlglot, replace every literal (numbers, strings,
    booleans) with a placeholder, re-serialize canonically, and hash.
    Two queries that differ only in literal VALUES — `WHERE id = 1` vs
    `WHERE id = 2` — collapse to the same fingerprint; any STRUCTURAL
    difference (an added `OR`, a different column/table, an extra IN
    element) changes it. Returns None when the SQL can't be parsed —
    no fingerprint means it can never match the cache, which is the
    safe default.

    NOTE: callers must only cache-match RO queries. For RW/DDL a literal
    change alters the real-world effect (`SET amount = 1` vs
    `SET amount = 999999`) while the fingerprint stays identical, so a
    fingerprint hit must never auto-approve a write."""
    sql = (sql or "").strip()
    if not sql:
        return None
    dialect = engines.spec(engine).sqlglot_dialect
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        return None
    if tree is None:
        return None

    def _norm(node):
        if isinstance(node, (exp.Literal, exp.Boolean)):
            return exp.Placeholder()
        return node

    try:
        normalized = tree.transform(_norm)
        canonical = normalized.sql(dialect=dialect)
    except Exception:
        return None
    # Key the hash by dialect so a structurally identical query under two
    # dialects can never collide into the same approval-cache fingerprint.
    return hashlib.sha256(f"{dialect}:{canonical}".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Approval cache, layer 1: the canonical form
# ---------------------------------------------------------------------------
#
# `fingerprint` treats two queries as the same only when they differ in
# literal values. Layer 1 also ignores differences of form that do not change
# which data the query reads:
#
#   * the number of values in an IN list
#   * comments and the case of unquoted names
#   * table alias names, when each table appears once
#   * the order and the aliases of the outer SELECT list
#   * the order of AND and OR operands
#   * the order of inner joins (an inner join is a cross join plus a filter)
#   * the outer ORDER BY
#
# The outer LIMIT stays, but its number is a placeholder, as in `fingerprint`.
# An ORDER BY or a LIMIT inside a subquery stays: there it selects rows.


def _parse_single(sql: str, dialect: str):
    """The one statement in `sql`, or None (empty, unparseable, or more than
    one statement)."""
    sql = (sql or "").strip()
    if not sql:
        return None
    try:
        trees = [t for t in sqlglot.parse(sql, read=dialect) if t is not None]
    except Exception:
        return None
    return trees[0] if len(trees) == 1 else None


def _placeholder_literals(tree):
    return tree.transform(
        lambda n: exp.Placeholder() if isinstance(n, (exp.Literal, exp.Boolean)) else n)


def _has_positional_refs(select) -> bool:
    """True when GROUP BY or ORDER BY names a SELECT-list position (`GROUP BY 1`).
    The SELECT list must then keep its order."""
    for key in ("group", "order"):
        node = select.args.get(key)
        if node is not None and any(isinstance(e, exp.Literal) or
                                    (isinstance(e, exp.Ordered) and isinstance(e.this, exp.Literal))
                                    for e in node.expressions):
            return True
    return False


def _table_key(table) -> str:
    return ".".join(p for p in (table.catalog, table.db, table.name) if p)


def _drop_table_aliases(tree) -> None:
    """Replace each table alias with the table name. Only when every table
    appears once in the whole statement, and no alias is used twice: then the
    rename cannot make two different columns look the same."""
    tables = list(tree.find_all(exp.Table))
    names = [_table_key(t) for t in tables]
    if len(set(names)) != len(names):
        return
    aliases = [a.name for a in tree.find_all(exp.TableAlias) if a.name]
    if len(set(aliases)) != len(aliases) or set(aliases) & {t.name for t in tables}:
        return
    if tree.find(exp.CTE) is not None:
        return
    rename = {}
    for t in tables:
        if t.alias:
            rename[t.alias] = t.name
            t.set("alias", None)
    if not rename:
        return
    for col in tree.find_all(exp.Column):
        if col.table in rename:
            col.set("table", exp.to_identifier(rename[col.table]))


def _flatten_inner_joins(select) -> None:
    """Rewrite `FROM a JOIN b ON x JOIN c ON y` as a sorted cross join with
    `x AND y` in WHERE. Only when every join is a plain inner join on plain
    tables: an outer join, USING, LATERAL or a derived table keeps its form."""
    joins = select.args.get("joins") or []
    from_ = select.args.get("from_")
    if not joins or from_ is None or not isinstance(from_.this, exp.Table):
        return
    conds = []
    tables = [from_.this]
    for j in joins:
        if (j.side or (j.kind or "").upper() not in ("", "INNER") or j.args.get("using")
                or j.args.get("method") or not isinstance(j.this, exp.Table)
                or j.args.get("on") is None):
            return
        tables.append(j.this)
        conds.append(j.args["on"])
    tables.sort(key=lambda t: t.sql())
    where = select.args.get("where")
    parts = ([where.this] if where is not None else []) + conds
    select.set("from_", exp.From(this=tables[0].copy()))
    select.set("joins", [exp.Join(this=t.copy(), kind="CROSS") for t in tables[1:]])
    select.set("where", exp.Where(this=exp.and_(*[p.copy() for p in parts])))


def _flat(node, cls):
    if isinstance(node, cls):
        return _flat(node.left, cls) + _flat(node.right, cls)
    if isinstance(node, exp.Paren) and isinstance(node.this, cls):
        return _flat(node.this, cls)
    return [node]


def _sort_connectors(tree):
    """Sort the operands of every AND and OR chain. Deepest chains first, so
    an outer chain sorts on operands that are already canonical."""
    chains = [n for n in tree.find_all(exp.And, exp.Or)
              if not isinstance(n.parent, type(n))
              and not (isinstance(n.parent, exp.Paren)
                       and isinstance(n.parent.parent, type(n)))]
    chains.sort(key=lambda n: -n.depth)
    for node in chains:
        cls = type(node)
        parts = sorted((p.copy() for p in _flat(node, cls)), key=lambda e: e.sql())
        new = parts[0]
        for p in parts[1:]:
            new = cls(this=new, expression=p)
        if node is tree:
            tree = new
        else:
            node.replace(new)
    return tree


def canonical_fingerprint(sql: str, engine: str = "postgres") -> str | None:
    """Layer 1 of the approval cache: a fingerprint that ignores differences of
    form (see the list above). None when the SQL does not parse to exactly one
    statement. Like `fingerprint`, callers must match RO queries only."""
    dialect = engines.spec(engine).sqlglot_dialect
    tree = _parse_single(sql, dialect)
    if tree is None:
        return None
    try:
        from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
        tree = normalize_identifiers(tree, dialect=dialect)
        # Positions in GROUP BY / ORDER BY are literals: read them before the
        # literals become placeholders.
        keep_order = isinstance(tree, exp.Select) and _has_positional_refs(tree)
        tree = _placeholder_literals(tree)
        for node in list(tree.find_all(exp.In)):
            values = node.args.get("expressions")
            if values and all(isinstance(v, exp.Placeholder) for v in values):
                node.set("expressions", [exp.Placeholder()])
        _drop_table_aliases(tree)
        for select in list(tree.find_all(exp.Select)):
            _flatten_inner_joins(select)
        if isinstance(tree, (exp.Select, exp.SetOperation)):
            tree.set("order", None)
        if isinstance(tree, exp.Select) and not keep_order:
            projections = [e.this if isinstance(e, exp.Alias) else e
                           for e in tree.expressions]
            projections.sort(key=lambda e: e.sql())
            tree.set("expressions", projections)
        tree = _sort_connectors(tree)
        canonical = tree.sql(dialect=dialect, comments=False)
    except Exception:
        log.debug("canonical_fingerprint failed", exc_info=True)
        return None
    return hashlib.sha256(f"k1:{dialect}:{canonical}".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Approval cache, layer 2: the column footprint
# ---------------------------------------------------------------------------
#
# The footprint lists each (table, column, use) that a query reads. The use is
#   raw     the column value is in the result
#   agg     the column is in the result only inside an aggregate
#   filter  the column is only in WHERE, JOIN, GROUP BY, HAVING or ORDER BY
# A query is covered when earlier approved queries together read each of its
# columns at the same use or a higher one (raw > agg > filter).
#
# Only a plain SELECT gets a complete footprint: no subquery, no CTE, no set
# operation, no table function. Any other query, and any column whose table is
# not certain, makes the footprint incomplete. An incomplete footprint can
# still add its known columns to the pool, but it can never be covered.

FOOTPRINT_RANK = {"raw": 3, "agg": 2, "filter": 1}


def _projection_use(col, root) -> str | None:
    """raw / agg when `col` is inside a SELECT-list item of `root`, else None.
    An aggregate inside a window function returns a value per row: raw."""
    in_agg = False
    node, parent = col, col.parent
    while parent is not None:
        if isinstance(node, exp.AggFunc) and not isinstance(parent, exp.Window):
            in_agg = True
        if isinstance(node, exp.Window):
            in_agg = False
        if parent is root:
            if node in root.expressions:
                return "agg" if in_agg else "raw"
            return None
        node, parent = parent, parent.parent
    return None


def footprint(sql: str, engine: str = "postgres") -> dict | None:
    """The column footprint of one statement:

        {"v": 1, "complete": bool, "tables": [...], "cols": [[table, column, use], ...]}

    `column` is `*` for `SELECT *` or `t.*`. None when the SQL does not parse
    to one statement or when no table can be named."""
    dialect = engines.spec(engine).sqlglot_dialect
    tree = _parse_single(sql, dialect)
    if tree is None:
        return None
    try:
        from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
        tree = normalize_identifiers(tree, dialect=dialect)
        complete = (isinstance(tree, exp.Select)
                    and tree.find(exp.Subquery, exp.CTE, exp.SetOperation,
                                  exp.Unnest, exp.Lateral) is None
                    and sum(1 for _ in tree.find_all(exp.Select)) == 1)
        tables: dict[str, str] = {}      # alias or name -> table key
        for t in tree.find_all(exp.Table):
            if not isinstance(t.this, exp.Identifier):
                complete = False         # a table function
                continue
            key = _table_key(t)
            ref = t.alias or t.name
            if ref in tables and tables[ref] != key:
                complete = False
            tables[ref] = key
        if not tables:
            return None
        single = next(iter(set(tables.values()))) if len(set(tables.values())) == 1 else None
        # ORDER BY may name a SELECT-list alias. That is not a table column.
        out_names = ({e.alias for e in tree.expressions if isinstance(e, exp.Alias)}
                     if isinstance(tree, exp.Select) else set())
        cols: set[tuple[str, str, str]] = set()
        for col in tree.find_all(exp.Column):
            if isinstance(col.this, exp.Star):
                continue                 # `t.*`, handled with the stars below
            if (not col.table and col.name in out_names
                    and col.find_ancestor(exp.Order) is not None):
                continue
            table = tables.get(col.table) if col.table else single
            if table is None:
                complete = False
                continue
            use = _projection_use(col, tree) if isinstance(tree, exp.Select) else None
            cols.add((table, col.name, use or "filter"))
        if isinstance(tree, exp.Select):
            for item in tree.expressions:
                star = item if isinstance(item, exp.Star) else (
                    item if isinstance(item, exp.Column) and isinstance(item.this, exp.Star)
                    else None)
                if star is None:
                    continue
                if isinstance(star, exp.Column) and star.table:
                    table = tables.get(star.table)
                    if table is None:
                        complete = False
                        continue
                    cols.add((table, "*", "raw"))
                else:
                    for table in set(tables.values()):
                        cols.add((table, "*", "raw"))
    except Exception:
        log.debug("footprint failed", exc_info=True)
        return None
    return {"v": 1, "complete": bool(complete),
            "tables": sorted(set(tables.values())),
            "cols": sorted([list(c) for c in cols])}


def footprint_covered(new: dict, pool: list[tuple[int, dict]]) -> list[int] | None:
    """The ids of the pool requests that together cover `new`, or None when
    the pool does not cover it. `pool` is (request id, footprint) pairs, newest
    first. Covered means: `new` is complete, every table of `new` is in the
    pool, and every column of `new` is read in the pool at the same use or a
    higher one. A `*` in the pool covers every raw column of that table."""
    if not new or not new.get("complete") or not new.get("cols"):
        return None
    best: dict[tuple[str, str], tuple[int, int]] = {}   # (table, col) -> (rank, id)
    stars: dict[str, int] = {}
    pool_tables: set[str] = set()
    for rid, fp in pool:
        if not isinstance(fp, dict):
            continue
        pool_tables.update(fp.get("tables") or [])
        for table, col, use in fp.get("cols") or []:
            rank = FOOTPRINT_RANK.get(use, 0)
            if col == "*":
                stars.setdefault(table, rid)
            if rank > best.get((table, col), (0, 0))[0]:
                best[(table, col)] = (rank, rid)
    if not set(new.get("tables") or []) <= pool_tables:
        return None
    used: list[int] = []
    for table, col, use in new["cols"]:
        need = FOOTPRINT_RANK.get(use)
        if need is None:
            return None
        rank, rid = best.get((table, col), (0, 0))
        if rank < need and col != "*" and table in stars:
            rank, rid = FOOTPRINT_RANK["raw"], stars[table]
        if rank < need:
            return None
        if rid not in used:
            used.append(rid)
    return used


# Function names the bot should never let through, regardless of who
# submitted or which tier they're at. Case-insensitive match against
# sqlglot's resolved function name (`anonymous` Functions carry the
# raw name in their `this` arg; built-in functions get their own
# Expression subclass and we match by the `key` / `sql_name`).
_DANGEROUS_FUNCS = frozenset(name.lower() for name in {
    # Filesystem / large-object I/O
    "pg_read_file",
    "pg_read_binary_file",
    "pg_read_server_files",
    "pg_ls_dir",
    "pg_stat_file",
    "lo_import",
    "lo_export",
    "lo_from_bytea",
    "lo_put",
    # Cluster control
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_reload_conf",
    "pg_rotate_logfile",
    "pg_promote",
    # Remote SQL execution (often used to bypass our local checks)
    "dblink",
    "dblink_connect",
    "dblink_connect_u",
    "dblink_exec",
    "dblink_open",
    # Replication-slot fiddling
    "pg_create_logical_replication_slot",
    "pg_create_physical_replication_slot",
    "pg_drop_replication_slot",
})

# Max permissible argument (in seconds) for pg_sleep / pg_sleep_for /
# pg_sleep_until. Anything above this is treated as a DoS lever; the
# bot blocks it even on RO since the connection holds for that long.
_PG_SLEEP_MAX_SECONDS = 10


def is_enabled(engine: str = "postgres") -> bool:
    """Honor the bot_config kill-switch so an operator can disable this module
    without a redeploy when chasing a false positive — EXCEPT on engines where
    it is the only defense.

    On PostgreSQL this module is genuinely a second opinion: the leading-keyword
    allow-list, the WHERE-required and tautology guards, the statement-count
    cross-check and `default_transaction_read_only` all still apply with it off.
    On SQL Server it is the ONLY thing that blocks semicolon-free T-SQL batch
    smuggling, OPENROWSET and linked-server references, so honoring an "off"
    there turns a false-positive escape hatch into a full bypass — reachable
    with one click in the web config panel. The switch is therefore ignored for
    non-Postgres engines."""
    if (engine or "postgres").lower() != "postgres":
        return True
    val = (cfg.get_setting("ast_safety_enabled", "on") or "on").strip().lower()
    return val in {"on", "true", "yes", "1"}


# Ending or cancelling a server session: DataGrip's "Terminate", SSMS's "Kill
# process". Refused like the rest of _DANGEROUS_FUNCS, except for a
# super-admin (operator request, 2026-09-26), for whom query_safety asks first
# and runs the statement as the elevated role. A tier login can signal only
# the sessions of its own login, unless it holds pg_signal_backend. The
# elevated role can signal other sessions.
SESSION_CONTROL_FUNCS = frozenset({"pg_terminate_backend", "pg_cancel_backend"})


def session_control_calls(sql: str, engine: str = "postgres") -> set[str]:
    """The session-control functions a statement calls. Empty when it calls
    none, or when it does not parse (check() refuses that on its own)."""
    try:
        statements = sqlglot.parse(sql, read=engines.spec(engine).sqlglot_dialect)
    except Exception:
        return set()
    found: set[str] = set()
    for stmt in statements:
        if isinstance(stmt, exp.Expression):
            found |= _function_names(stmt) & SESSION_CONTROL_FUNCS
    return found


#: Functions that change a sequence. They read like a SELECT and they write.
#: `nextval` consumes a value and `setval` moves the counter, so a statement
#: that calls either is a write and needs the write tier. `currval` and `lastval`
#: only read session state and are not listed.
SEQUENCE_WRITE_FUNCS = frozenset({"setval", "nextval"})


def sequence_write_calls(sql: str, engine: str = "postgres") -> set[str]:
    """The sequence-writing functions a statement calls. Empty when it calls
    none, or when it does not parse (check() refuses that on its own)."""
    try:
        statements = sqlglot.parse(sql, read=engines.spec(engine).sqlglot_dialect)
    except Exception:
        return set()
    found: set[str] = set()
    for stmt in statements:
        if isinstance(stmt, exp.Expression):
            found |= _function_names(stmt) & SEQUENCE_WRITE_FUNCS
    return found


def check(sql: str, engine: str = "postgres", unrestricted: bool = False) -> list[str]:
    """Return a list of human-facing blocker messages for the query.
    Empty list = clean. The caller appends these onto its own
    `report.blockers`. `engine` selects the sqlglot dialect and unions the
    engine's dangerous-function set onto the Postgres one.

    `unrestricted` (the super-admin path) lets SESSION_CONTROL_FUNCS through;
    query_safety adds the confirmation and the tier. Everything else stays
    refused, and so does a session function an operator blocks in bot_config."""
    if not is_enabled(engine):
        return []
    sql = (sql or "").strip()
    if not sql:
        return []

    spec = engines.spec(engine)
    dialect = spec.sqlglot_dialect
    # Read-only engines get a pass that does not depend on the parser first: a
    # SETTINGS clause and the worst function names are refused even where the
    # dialect mis-parses. Comments and string literals are blanked before it
    # looks, so it cannot fire on a note or a value.
    if spec.read_only:
        lexical = _read_only_lexical_block(sql)
        if lexical:
            return lexical
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except sqlglot.errors.SqlglotError as e:
        # Fail closed: anything that doesn't parse cleanly under this engine's
        # syntax is suspicious. Most obfuscation bypasses fall here.
        #
        # SqlglotError, not ParseError. `TokenError` is a SIBLING of ParseError,
        # not a subclass, so catching ParseError alone let every unbalanced quote
        # escape as a 500 — the tokenizer raises before a parse is ever
        # attempted. A user pasted a CASE arm whose string literal had been
        # truncated mid-line and got "internal error" from a check whose message
        # already said "stray quotes". The intent was right and the except clause
        # was one level too narrow.
        log.info("ast_safety: %s (engine=%s), blocking. %s",
                 type(e).__name__, engine, e)
        return [
            f"QueryHub could not parse this query as standard {dialect} SQL. "
            "Check for stray quotes, unbalanced parentheses or non-standard "
            "syntax. If the syntax is intentional, ask the DBA team to disable "
            "bot_config.ast_safety_enabled for this run."
        ]
    except Exception:
        # Backstop for the same failure in a shape this sqlglot does not have
        # yet. A parser is a moving dependency and this one is on the submit
        # path: an exception class we have never seen must still refuse the
        # query rather than reach the user as an unhandled error.
        log.exception("ast_safety: unexpected parser failure (engine=%s), "
                      "blocking", engine)
        return [
            f"QueryHub could not parse this query as standard {dialect} SQL. "
            "Check for stray quotes, unbalanced parentheses or non-standard "
            "syntax."
        ]

    # PostgreSQL only: query_safety routes the call to the elevated role there,
    # and on another engine the name is not a session control at all.
    builtin = (_DANGEROUS_FUNCS - SESSION_CONTROL_FUNCS
               if unrestricted and spec.name == "postgres" else _DANGEROUS_FUNCS)
    blocked_funcs = builtin | spec.blocked_functions | _config_blocked(engine)
    blockers: list[str] = []
    for stmt in statements:
        if stmt is None:
            continue
        blockers.extend(_check_stmt(stmt, blocked_funcs, dialect,
                                    block_catalog_refs=spec.block_catalog_refs,
                                    blocked_schemas=spec.blocked_schemas))
        if spec.read_only:
            blockers.extend(_check_read_only_engine(stmt, spec))
    return blockers


def _config_blocked(engine: str) -> frozenset:
    """Extra function names an operator blocks at runtime, from bot_config
    `<engine>_blocked_functions` (comma separated). Tightening only: it adds
    to what the code blocks and cannot re-enable anything."""
    try:
        value = cfg.get_setting(f"{engine}_blocked_functions", "")
    except Exception:
        return frozenset()
    return frozenset(p.strip().lower() for p in (value or "").split(",") if p.strip())


# Run on SQL with comments and string literals blanked out.
_SETTINGS_RE = re.compile(r"\bSETTINGS\s+[\w`\"]+\s*=", re.IGNORECASE)
_HIGH_SEVERITY_RE = re.compile(
    r"\b(url|urlcluster|s3|s3cluster|gcs|hdfs|hdfscluster|remote|remotesecure"
    r"|cluster|clusterallreplicas|file|filecluster|executable|mysql|postgresql"
    r"|jdbc|odbc|mongodb|redis|sqlite|joinget|dictget\w*|addresstoline\w*"
    r"|addresstosymbol|demangle)\s*\(", re.IGNORECASE)


def _read_only_lexical_block(sql: str) -> list[str]:
    from .query_safety import code_text
    text = code_text(sql)
    if _SETTINGS_RE.search(text):
        return ["An inline SETTINGS clause is not allowed on a read-only "
                "target. A query-level setting could relax the target's limits."]
    m = _HIGH_SEVERITY_RE.search(text)
    if m:
        return [f"`{m.group(1)}` is blocked on a read-only target. It reads "
                f"data from outside the database (remote, file, cluster or "
                f"dictionary access), or it runs code."]
    return []


def _check_read_only_engine(stmt: exp.Expression, spec) -> list[str]:
    """What a read-only engine adds to the shared checks.

    * Table functions are DEFAULT-DENY: only the benign generators on
      `spec.table_function_allowlist` pass, so a table function the server
      gains later is refused until someone decides it is harmless.
    * The dictionary family is blocked by prefix, which covers the typed
      variants (`dictGetString`, `dictGetUInt64`, ...) a fixed list misses.
      readonly=1 does not stop these reads; only GRANTs do, and this is the
      in-app backstop.
    * A SETTINGS clause, again, from the parse tree.
    """
    out: list[str] = []
    for tbl in stmt.find_all(exp.Table):
        fn = tbl.this
        if not isinstance(fn, exp.Func):
            continue
        name = anon_name(fn) if isinstance(fn, exp.Anonymous) else (fn.sql_name() or "").lower()
        if name and name not in spec.table_function_allowlist:
            out.append(f"Table function `{name}` is not allowed on a read-only "
                       f"target. Read a real table, or a generator such as "
                       f"`numbers`.")
    for name in sorted(_function_names(stmt)):
        if name.startswith("dictget"):
            out.append(f"`{name}` reads a dictionary, which is blocked on a "
                       f"read-only target.")
    if any(node.args.get("settings") for node in stmt.walk()
           if isinstance(node, exp.Expression)):
        out.append("An inline SETTINGS clause is not allowed on a read-only "
                   "target. A query-level setting could relax the target's "
                   "limits.")
    return list(dict.fromkeys(out))


def _check_stmt(stmt: exp.Expression, blocked_funcs: frozenset = _DANGEROUS_FUNCS,
                dialect: str = "postgres",
                block_catalog_refs: bool = False,
                blocked_schemas: frozenset = frozenset()) -> Iterable[str]:
    out: list[str] = []

    # 0a. A SELECT with no columns at all. PostgreSQL accepts `SELECT FROM users`
    # — an empty target list is legal there, verified against a live server — and
    # it is essentially always a typo where a column list or a `*` was meant.
    #
    # Left alone it is the worst kind of harmless: a real full scan of a
    # production table, capped at the row limit, producing a CSV whose header
    # line is empty and a grid that renders row numbers with no columns. Observed
    # live — `select  from users;` completed with 5000 rows and zero columns and
    # no error, and the empty grid read as the tool being broken.
    #
    # Refused at submit rather than merely warned about, because there is no
    # result it could produce that anyone wants: running it spends a scan and an
    # approval round to arrive at nothing.
    for sel in stmt.find_all(exp.Select):
        if not sel.expressions:
            out.append(
                "This SELECT lists no columns. PostgreSQL allows that and would "
                "return rows with nothing in them. Name the columns you want, "
                "or use `*`."
            )
            break

    # 0. Schemas an engine declares off-limits (EngineSpec.blocked_schemas).
    # This field existed but nothing ever read it, so a read-only engine's
    # "never touch these" list was decoration. Matched on the schema part of
    # a qualified name, case-insensitively.
    if blocked_schemas:
        lowered = {s.lower() for s in blocked_schemas}
        for tbl in stmt.find_all(exp.Table):
            parts = [p.name for p in tbl.parts]
            if len(parts) >= 2 and parts[-2].lower() in lowered:
                out.append(
                    f"Schema `{parts[-2]}` is not queryable on this engine. "
                    f"It holds server internals, not your data."
                )
                break

    # 1. Dangerous function names (anywhere in the tree). The set is the
    # Postgres baseline unioned with the target engine's own blocklist
    # (e.g. ClickHouse url/s3/remote; SQL Server OPENROWSET/OPENQUERY).
    for func_name in _function_names(stmt):
        if func_name in blocked_funcs:
            out.append(
                f"Function `{func_name}` is blocked by the QueryHub safety "
                f"policy. It gives access to the file system, a remote server, "
                f"cluster control, another database or code execution. If you "
                f"need it, ask the DBA team to run the query outside QueryHub."
            )

    # 2. `COPY ... TO PROGRAM` / `COPY ... FROM PROGRAM`.
    # sqlglot models COPY as exp.Copy; the `program` flag is set when
    # the parser sees TO/FROM PROGRAM. Older sqlglot versions don't
    # set the attribute consistently, so fall back to the SQL string.
    if isinstance(stmt, exp.Copy):
        sql_text = stmt.sql(dialect=dialect).upper()
        if "PROGRAM" in sql_text:
            out.append(
                "COPY ... PROGRAM is blocked. It runs a shell command on the "
                "database server. Use COPY ... FROM or TO 'filename', or send "
                "the data through the client."
            )

    # 3. Cross-database / cross-server table references (engine opt-in).
    # A SQL Server login is scoped to one database on one instance, but a
    # 3-part name (database.schema.object) reaches another database and a
    # 4-part name (server.database.schema.object) reaches a linked server —
    # both slip past the approved target. sqlglot exposes the identifier
    # chain as `Table.parts`; 1-2 parts (object / schema.object) stay in
    # the connected database, so only >= 3 is a boundary crossing.
    if block_catalog_refs:
        seen: set[str] = set()
        for tbl in stmt.find_all(exp.Table):
            nparts = len(tbl.parts)
            if nparts < 3:
                continue
            ref = tbl.sql(dialect=dialect)
            if ref in seen:
                continue
            seen.add(ref)
            if nparts >= 4:
                out.append(
                    f"Cross-server reference `{ref}` is blocked. A 4-part "
                    f"name (server.database.schema.object) reaches a linked "
                    f"server outside the approved target. Use only objects in "
                    f"the target database, written as schema.object."
                )
            elif dialect == "athena":
                # Same rule, different thing named. Athena's first part is a
                # CATALOG, and the Athena console writes queries in exactly
                # this form (`awsdatacatalog.db.table`), so a reader who
                # copies one in deserves to be told what to drop rather than
                # to read about a "server" that does not exist here.
                out.append(
                    f"Catalog reference `{ref}` is blocked. A 3-part name "
                    f"(catalog.database.table) can reach a federated catalog "
                    f"outside the approved target. Remove the catalog prefix "
                    f"and use database.table."
                )
            else:
                out.append(
                    f"Cross-database reference `{ref}` is blocked. A 3-part "
                    f"name (database.schema.object) reaches another database "
                    f"on the server. Query only the approved target database. "
                    f"Remove the database prefix and use schema.object."
                )

    # 4. `pg_sleep(N)` with large N.
    for func_call in stmt.find_all(exp.Anonymous):
        name = anon_name(func_call)
        if name in {"pg_sleep", "pg_sleep_for", "pg_sleep_until"}:
            arg = (func_call.expressions or [None])[0]
            seconds = _literal_number(arg)
            if seconds is None or seconds > _PG_SLEEP_MAX_SECONDS:
                out.append(
                    f"`{name}` is blocked with this argument. The limit is "
                    f"{_PG_SLEEP_MAX_SECONDS}s. A long sleep holds a connection "
                    f"slot for its full duration, so it can cause a denial of "
                    f"service, even on an RO connection."
                )

    return out


def anon_name(func: exp.Expression) -> str:
    """Lowercased name of an Anonymous function call, quoted or not.

    sqlglot puts a bare name in `.this` as a plain string, but a QUOTED call —
    `"pg_read_file"(...)`, which PostgreSQL resolves to the same function —
    gives `.this` an `exp.Identifier`. Code that assumed a string either
    skipped the call entirely (so quoting a name walked straight past the
    dangerous-function blocklist) or crashed on `.lower()`. Returns "" when no
    name can be resolved."""
    raw = getattr(func, "this", None)
    if isinstance(raw, str):
        return raw.lower()
    name = getattr(raw, "name", None) or getattr(raw, "this", None)
    return name.lower() if isinstance(name, str) else ""


def _function_names(node: exp.Expression) -> set[str]:
    """Every function-call name in the tree, lowercased. Covers both
    built-in functions (sqlglot has subclasses for them like
    exp.Substring, exp.JSONExtract — we resolve their sql_name) and
    user-callable / anonymous ones (exp.Anonymous, whose `.this`
    carries the raw name, quoted or bare)."""
    names: set[str] = set()
    for func in node.find_all(exp.Func):
        # Anonymous: pg_read_file, lo_export, dblink_connect — these
        # are the ones we usually care about.
        if isinstance(func, exp.Anonymous):
            name = anon_name(func)
            if name:
                names.add(name)
            continue
        # Built-in mapped subclasses (exp.Substring etc.) won't ever
        # match _DANGEROUS_FUNCS today, but if sqlglot grows a
        # dedicated subclass for one of these functions later, we'd
        # still catch it via sql_name.
        try:
            sql_name = func.sql_name()
            if sql_name:
                names.add(sql_name.lower())
        except Exception:
            pass
    return names


def _literal_number(node) -> float | None:
    """Return a numeric value if `node` is a literal number, else
    None (means "we can't tell, treat conservatively")."""
    if node is None:
        return None
    try:
        if isinstance(node, exp.Literal):
            if node.is_number:
                return float(node.this)
        if isinstance(node, exp.Neg):
            inner = _literal_number(node.this)
            return -inner if inner is not None else None
        if isinstance(node, exp.Cast):
            return _literal_number(node.this)
    except Exception:
        pass
    return None
