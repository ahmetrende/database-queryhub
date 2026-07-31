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


def check(sql: str, engine: str = "postgres") -> list[str]:
    """Return a list of human-facing blocker messages for the query.
    Empty list = clean. The caller appends these onto its own
    `report.blockers`. `engine` selects the sqlglot dialect and unions the
    engine's dangerous-function set onto the Postgres one."""
    if not is_enabled(engine):
        return []
    sql = (sql or "").strip()
    if not sql:
        return []

    spec = engines.spec(engine)
    dialect = spec.sqlglot_dialect
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except sqlglot.errors.ParseError as e:
        # Fail closed: anything that doesn't parse cleanly under this
        # engine's syntax is suspicious. Most obfuscation bypasses fall here.
        log.info("ast_safety: parse error (engine=%s), blocking. %s", engine, e)
        return [
            f"Query could not be parsed as standard {dialect} SQL. Check for "
            "stray quotes, unbalanced parentheses, or non-standard syntax. "
            "If the query is intentionally exotic, ask the DBA team to "
            "disable bot_config.ast_safety_enabled for the run."
        ]

    blocked_funcs = _DANGEROUS_FUNCS | spec.blocked_functions
    blockers: list[str] = []
    for stmt in statements:
        if stmt is None:
            continue
        blockers.extend(_check_stmt(stmt, blocked_funcs, dialect,
                                    block_catalog_refs=spec.block_catalog_refs,
                                    blocked_schemas=spec.blocked_schemas))
    return blockers


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
                "return rows with nothing in them — name the columns you want, "
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
                    f"Schema `{parts[-2]}` is not queryable on this engine — "
                    f"it holds server internals, not your data."
                )
                break

    # 1. Dangerous function names (anywhere in the tree). The set is the
    # Postgres baseline unioned with the target engine's own blocklist
    # (e.g. ClickHouse url/s3/remote; SQL Server OPENROWSET/OPENQUERY).
    for func_name in _function_names(stmt):
        if func_name in blocked_funcs:
            out.append(
                f"Function `{func_name}` is blocked by the bot's safety "
                f"policy (filesystem / remote / cluster-control / cross-DB "
                f"or code-execution side channel). If you genuinely need it, "
                f"ask the DBA team to run the query out-of-band."
            )

    # 2. `COPY ... TO PROGRAM` / `COPY ... FROM PROGRAM`.
    # sqlglot models COPY as exp.Copy; the `program` flag is set when
    # the parser sees TO/FROM PROGRAM. Older sqlglot versions don't
    # set the attribute consistently, so fall back to the SQL string.
    if isinstance(stmt, exp.Copy):
        sql_text = stmt.sql(dialect=dialect).upper()
        if "PROGRAM" in sql_text:
            out.append(
                "COPY ... PROGRAM is blocked — it runs shell on the database "
                "server. Use COPY ... FROM/TO 'filename' or pipe through the "
                "client instead."
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
                    f"Cross-server reference `{ref}` is blocked — a 4-part "
                    f"name (server.database.schema.object) reaches a linked "
                    f"server outside the approved target. Reference only "
                    f"objects in the target database as schema.object."
                )
            else:
                out.append(
                    f"Cross-database reference `{ref}` is blocked — a 3-part "
                    f"name (database.schema.object) reaches another database "
                    f"on the server. Query only the approved target database; "
                    f"drop the database prefix and use schema.object."
                )

    # 4. `pg_sleep(N)` with large N.
    for func_call in stmt.find_all(exp.Anonymous):
        name = anon_name(func_call)
        if name in {"pg_sleep", "pg_sleep_for", "pg_sleep_until"}:
            arg = (func_call.expressions or [None])[0]
            seconds = _literal_number(arg)
            if seconds is None or seconds > _PG_SLEEP_MAX_SECONDS:
                out.append(
                    f"`{name}` is blocked at this argument — capped at "
                    f"{_PG_SLEEP_MAX_SECONDS}s. Long sleeps hold a "
                    f"connection slot for the whole duration and are a "
                    f"DoS lever even on RO."
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
