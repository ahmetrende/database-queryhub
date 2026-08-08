"""Static safety analysis for SQL submissions.

Responsibilities:

1. Allow multi-statement submissions (statements separated by `;`) but:
     - Each statement is classified independently (ro / rw / ddl / set).
     - All non-SET ("main") statements must classify to the SAME tier.
       Mixing read-only and write statements in one request is rejected
       (e.g. `SELECT 1; UPDATE t SET ...`) — the bot connects with one
       credential tier per request, and the user should split mixed
       intent into separate requests for clear admin review.
     - The first N statements may optionally be `SET LOCAL` directives
       on a per-cluster parameter allowlist (`bot_config.
       set_allowed_params`). Plain `SET` (without LOCAL) is rewritten
       to `SET LOCAL` automatically — transaction-scoped, undone on
       commit. SETs on parameters outside the allowlist are rejected
       with a specific error naming the offender.
     - SET, if present, must come BEFORE any main statement (no
       `SELECT ...; SET LOCAL work_mem = ...; SELECT ...`).

2. Restrict the leading word of each non-SET statement to a strict
   allow-list. Anything else is rejected with a friendly error. This is
   the layer that blocks `DO` / `CALL` / `COPY` / `LOCK` / `PREPARE` /
   cursors etc. — none have legitimate use in /sql and several are
   sandbox-escape vectors.

3. Reject CTE-DML — a top-level `WITH` whose body contains
   INSERT / UPDATE / DELETE / MERGE. Submit the DML as its own
   statement so admin reviews it at the correct permission tier.

4. Detect destructive statements (UPDATE / DELETE / DROP / TRUNCATE /
   ALTER / GRANT / REVOKE / INSERT / CREATE / REPLACE) so admin DMs
   can show a warning badge.

5. Hard-block UPDATE / DELETE without a WHERE clause and UPDATE /
   DELETE whose WHERE clause is trivially always-true.

6. Block `EXPLAIN ANALYZE` (which executes the wrapped statement).

`analyze()` returns a SafetyReport carrying per-statement classification
and a `rewritten_sql` payload — the SQL the bot should execute, with
`SET` -> `SET LOCAL` substitutions already applied. Callers (executor)
should iterate `report.statements` to run each one with its own
post-processing (CSV per SELECT, etc.).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlparse
from sqlparse.sql import Statement

from . import engines

# For a read-only engine (e.g. ClickHouse) only these leading words are
# accepted; everything else is rejected up front, before tier
# classification. Kept deliberately narrow (default-deny).
_READ_ONLY_LEADING = {"SELECT", "WITH"}

DESTRUCTIVE_KEYWORDS = {
    "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER",
    "GRANT", "REVOKE", "INSERT", "CREATE", "REPLACE",
}

RW_KEYWORDS = {"INSERT", "UPDATE", "DELETE", "MERGE"}
DDL_KEYWORDS = {
    "CREATE", "ALTER", "DROP", "TRUNCATE", "COMMENT", "RENAME",
    "GRANT", "REVOKE",
    "VACUUM", "ANALYZE", "REINDEX", "CLUSTER", "REFRESH", "REASSIGN",
}

WHERE_REQUIRED_KEYWORDS = {"UPDATE", "DELETE"}

# Strict allow-list for the leading word of a non-SET statement.
ALLOWED_LEADING = {
    # Read
    "SELECT", "WITH", "VALUES", "TABLE", "EXPLAIN", "SHOW",
    # DML
    "INSERT", "UPDATE", "DELETE", "MERGE",
    # DDL — schema
    "CREATE", "ALTER", "DROP", "TRUNCATE", "RENAME", "COMMENT",
    # DDL — permissions
    "GRANT", "REVOKE",
    # DDL — maintenance
    "VACUUM", "REINDEX", "ANALYZE", "CLUSTER", "REFRESH", "REASSIGN",
}

BANNED_LEADING_REASONS: dict[str, str] = {
    "DO":          "Procedural blocks (DO) are not allowed.",
    "CALL":        "Procedural blocks (CALL) are not allowed.",
    "COPY":        "COPY is not allowed. Use SELECT to read or INSERT to write.",
    "RESET":       "Session config (RESET) is not allowed.",
    "DISCARD":     "DISCARD is not allowed.",
    "BEGIN":       "Don't include BEGIN — the bot wraps your query in a transaction.",
    "START":       "Don't include START TRANSACTION — the bot wraps your query in a transaction.",
    "COMMIT":      "Don't include COMMIT — the bot wraps your query in a transaction.",
    "END":         "Don't include END — the bot wraps your query in a transaction.",
    "ROLLBACK":    "Don't include ROLLBACK — the bot wraps your query in a transaction.",
    "SAVEPOINT":   "Savepoints are not allowed.",
    "RELEASE":     "RELEASE is not allowed.",
    "NOTIFY":      "Async messaging (NOTIFY) is not allowed.",
    "LISTEN":      "Async messaging (LISTEN) is not allowed.",
    "UNLISTEN":    "Async messaging (UNLISTEN) is not allowed.",
    "LOCK":        "Explicit LOCK is not allowed.",
    "DECLARE":     "Cursors (DECLARE) are not allowed.",
    "FETCH":       "Cursors (FETCH) are not allowed.",
    "MOVE":        "Cursors (MOVE) are not allowed.",
    "CLOSE":       "Cursors (CLOSE) are not allowed.",
    "PREPARE":     "Prepared statements (PREPARE) are not allowed.",
    "EXECUTE":     "Prepared statements (EXECUTE) are not allowed.",
    "DEALLOCATE":  "Prepared statements (DEALLOCATE) are not allowed.",
    "CHECKPOINT":  "CHECKPOINT is not allowed.",
    "SECURITY":    "SECURITY LABEL is not allowed.",
}

# Default SET allowlist if bot_config.set_allowed_params is missing.
# Production list lives in DB; this is just a safety floor.
SET_ALLOWED_DEFAULT: frozenset[str] = frozenset({
    "work_mem", "statement_timeout", "lock_timeout",
    "idle_in_transaction_session_timeout",
    "enable_seqscan", "enable_indexscan", "enable_bitmapscan",
    "enable_hashjoin", "enable_mergejoin", "enable_nestloop",
    "enable_indexonlyscan",
    "random_page_cost", "seq_page_cost", "cpu_tuple_cost",
    "cpu_index_tuple_cost", "cpu_operator_cost", "effective_cache_size",
    "default_statistics_target",
    "geqo", "geqo_threshold",
    "from_collapse_limit", "join_collapse_limit", "jit",
})


def _load_set_allowed() -> set[str]:
    """Load the SET parameter allowlist from bot_config. Falls back to
    SET_ALLOWED_DEFAULT if the row is missing or any error occurs (e.g.
    bot DB unreachable at safety-check time — fail-closed-ish)."""
    try:
        from . import config as cfg
        v = cfg.get_setting("set_allowed_params", "")
    except Exception:
        return set(SET_ALLOWED_DEFAULT)
    if not v or not v.strip():
        return set(SET_ALLOWED_DEFAULT)
    return {p.strip().lower() for p in v.split(",") if p.strip()}


def _explain_analyze_allowed() -> bool:
    """bot_config.allow_explain_analyze gate (default off). Fail-closed on
    any error so a DB blip never opens the EXPLAIN ANALYZE path."""
    try:
        from . import config as cfg
        v = cfg.get_setting("allow_explain_analyze", "off")
    except Exception:
        return False
    return (v or "").strip().lower() in {"on", "true", "yes", "1"}


def _explain_inner(stripped: str) -> str | None:
    """The SQL wrapped by EXPLAIN [(...)] [ANALYZE ...], or None if it can't
    be isolated. Handles both the option-list form
    `EXPLAIN (ANALYZE, BUFFERS) ...` and the legacy bare
    `EXPLAIN ANALYZE [VERBOSE] ...`."""
    m = re.match(r"^\s*EXPLAIN\s+", stripped, flags=re.IGNORECASE)
    if not m:
        return None
    rest = stripped[m.end():].lstrip()
    if rest.startswith("("):                       # EXPLAIN (ANALYZE, ...) stmt
        rest = re.sub(r"^\([^)]*\)\s*", "", rest)
    else:                                          # EXPLAIN ANALYZE [VERBOSE] stmt
        rest = re.sub(r"^(?:(?:ANALY[SZ]E|VERBOSE)\s+)+", "", rest,
                      flags=re.IGNORECASE)
    rest = rest.lstrip()
    return rest or None


def _explain_wraps_read(stripped: str) -> bool:
    """True iff the statement wrapped by EXPLAIN [ANALYZE] is a pure read
    (SELECT / WITH-read / VALUES / TABLE). A WITH must not embed
    INSERT/UPDATE/DELETE/MERGE (same rule as the top-level CTE-DML ban).
    Gate for EXPLAIN ANALYZE, whose ANALYZE actually executes the wrapped
    statement — a write must never slip through."""
    inner = _explain_inner(stripped)
    if not inner:
        return False
    head = inner.split(None, 1)
    kw = head[0].upper().strip("(")
    if kw in {"SELECT", "VALUES", "TABLE"}:
        return True
    if kw == "WITH":
        return not any(_contains_keyword(inner, k)
                       for k in ("INSERT", "UPDATE", "DELETE", "MERGE"))
    return False


@dataclass
class StatementInfo:
    """Classified per-statement info. `rewritten` is what the bot will
    actually execute (e.g. `SET` rewritten to `SET LOCAL`)."""
    raw: str
    rewritten: str
    kind: str  # "set" | "ro" | "rw" | "ddl"
    leading: str  # uppercase leading word (e.g. SELECT, SET, UPDATE)


@dataclass
class SafetyReport:
    is_destructive: bool = False
    keywords_found: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    statements: list[StatementInfo] = field(default_factory=list)
    main_tier: str = "ro"   # tier shared by all non-SET statements
    rewritten_sql: str = ""  # SET-rewritten + ;-joined; for legacy callers

    @property
    def blocked(self) -> bool:
        return bool(self.blockers)


def analyze(sql: str, engine: str = "postgres") -> SafetyReport:
    report = SafetyReport()
    # Per-engine classification. Each spec field that is None falls back to
    # the Postgres module constant below, so the postgres path is
    # byte-identical to the pre-engine code. A non-Postgres three-tier
    # engine (MSSQL) supplies its own T-SQL keyword sets.
    spec = engines.spec(engine)
    read_only = spec.read_only
    allowed_leading = spec.allowed_leading or ALLOWED_LEADING
    banned_leading = spec.banned_leading or BANNED_LEADING_REASONS
    rw_keywords = spec.rw_keywords or RW_KEYWORDS
    ddl_keywords = spec.ddl_keywords or DDL_KEYWORDS
    destructive = spec.destructive_keywords or DESTRUCTIVE_KEYWORDS
    statements = [s for s in sqlparse.parse(sql) if _has_real_tokens(s)]
    if not statements:
        report.blockers.append("Query is empty.")
        return report

    # Parser-differential gate. Everything below classifies what SQLPARSE
    # sees, but the SERVER is what runs the text — and the two tokenizers
    # disagree about backslashes inside string literals. sqlparse accepts
    # `\'` as an escaped quote (MySQL convention); PostgreSQL with
    # standard_conforming_strings=on (default since 9.1) and T-SQL both read
    # `'\'` as a COMPLETE one-character string. So
    #     UPDATE t SET a = '\'; DELETE FROM t; --' WHERE id = 5
    # is ONE statement to sqlparse (with a WHERE that reassures every guard
    # below) and THREE to the server: an unfiltered UPDATE plus a hidden
    # DELETE. That breaks the product's central invariant — the approved text
    # must be the executed text — and silently falsifies the audited
    # statement count. Cross-check the decomposition against the engine's own
    # parser and fail CLOSED on any disagreement. Deliberately independent of
    # bot_config.ast_safety_enabled: this protects the core guarantee, so it
    # is not an operator-flippable second opinion.
    # A backslash before a quote inside a normal literal makes the two parsers
    # read different SQL even when they agree on the statement COUNT, so this
    # runs before the count gate rather than inside it.
    bad_literal = _backslash_escape_in_literal(sql)
    if bad_literal is not None:
        # E'...' is a PostgreSQL extension; suggesting it on SQL Server would
        # send the user to a syntax error.
        escape_hint = ("Escape a quote as '' , or use an E'...' literal, so "
                       "what is reviewed is exactly what runs."
                       if spec.name == "postgres" else
                       "Escape a quote as '' so what is reviewed is exactly "
                       "what runs.")
        report.blockers.append(
            "Statement structure is ambiguous — the SQL parses differently "
            "than this engine would read it (a backslash before a quote inside "
            f"the literal {bad_literal[:40]!r}). Under this server a backslash "
            "in a literal is an ordinary character, so the quote after it CLOSES "
            "the string and everything following can end up commented out — "
            "including a WHERE clause. " + escape_hint)
        return report

    if _statement_count_disagrees(sql, spec):
        report.blockers.append(
            "Statement structure is ambiguous — the SQL parses differently "
            "than this engine would read it (usually a backslash inside a "
            "string literal). Rewrite the literal without backslash escapes, "
            "or split the statements explicitly, so what is reviewed is "
            "exactly what runs.")
        return report

    # set_config() is SET by another name. The SET branch below only runs when
    # the LEADING word is SET, so `SELECT set_config('statement_timeout','0',
    # false)` used to sail past the whole allow-list + value policy: it could
    # re-enable an unlimited statement_timeout, undo the CVE-2018-1058
    # search_path pin, or — via `set_config('role', <other team's role>)` —
    # step out of the per-team SET LOCAL ROLE fence. Route the function form
    # through exactly the same policy as the statement form. Independent of
    # ast_safety_enabled for the same reason as the gate above.
    set_allowed = _load_set_allowed()
    set_config_blockers = _check_set_config_calls(sql, spec, set_allowed=set_allowed)
    if set_config_blockers:
        report.blockers.extend(set_config_blockers)
        return report

    seen_main = False
    main_tier_set: set[str] = set()

    for stmt in statements:
        leading = _first_word_from_statement(stmt)
        if leading is None:
            continue  # only-whitespace/comment chunk
        upper = leading.upper()

        # --- SET branch ---
        if upper == "SET":
            if not spec.set_local_supported:
                report.blockers.append(
                    "SET statements are not supported on this engine."
                )
                return report
            if seen_main:
                report.blockers.append(
                    "SET statements must come BEFORE the main query. "
                    "Move all SET LOCAL ... lines to the top of your "
                    "submission."
                )
                return report
            ok, rewritten, err = _validate_set(stmt.value, set_allowed)
            if not ok:
                report.blockers.append(err)
                return report
            report.statements.append(StatementInfo(
                raw=stmt.value.strip().rstrip(";").strip(),
                rewritten=rewritten,
                kind="set",
                leading="SET",
            ))
            continue

        # --- non-SET (main) branch ---
        seen_main = True

        # Read-only engine (e.g. ClickHouse): permit only read statements,
        # rejected BEFORE tier classification — the load-bearing guard that
        # keeps a read-only target read-only even if a write keyword slips
        # past the per-engine allow-list.
        if read_only and upper not in _READ_ONLY_LEADING:
            report.blockers.append(
                f"This target's engine is read-only — only SELECT / WITH "
                f"queries are allowed here (got `{leading}`)."
            )
            return report

        if upper in banned_leading:
            report.blockers.append(banned_leading[upper])
            return report

        if upper not in allowed_leading:
            report.blockers.append(
                f"Unrecognized SQL: leading word '{leading}' is not allowed. "
                f"Did you mistype a keyword like UPDATE / DELETE / SELECT?"
            )
            return report

        # CTE-DML ban.
        if upper == "WITH":
            stripped = _stripped_body(stmt)
            for kw in ("INSERT", "UPDATE", "DELETE", "MERGE"):
                if _contains_keyword(stripped, kw):
                    report.blockers.append(
                        f"CTE-embedded {kw} is not allowed inside WITH. "
                        f"Submit the {kw} as its own statement so the admin "
                        f"reviews it at the correct permission tier."
                    )
                    return report

        # EXPLAIN ANALYZE EXECUTES the wrapped statement. It is allowed only
        # when bot_config.allow_explain_analyze is on AND the wrapped statement
        # is a pure read (SELECT / WITH-read / VALUES / TABLE). EXPLAIN ANALYZE
        # of a write or DDL would actually run it, so that stays blocked
        # regardless of the toggle. Plain EXPLAIN (no ANALYZE) never executes
        # and is always fine.
        if upper == "EXPLAIN":
            stripped = _stripped_body(stmt)
            # ANALY[SZ]E, not ANALYZE: PostgreSQL accepts the British spelling
            # as an exact synonym, and matching only the American one let
            # `EXPLAIN ANALYSE` past this gate entirely — including
            # `EXPLAIN ANALYSE UPDATE ...`, which really performs the write that
            # the comment above promises stays blocked regardless of the toggle.
            # Measured with the toggle off, 2026-07-30: ANALYZE blocked, ANALYSE
            # not, for SELECT, UPDATE and pg_read_file alike.
            if re.search(r"^\s*EXPLAIN\b[^;]*\bANALY[SZ]E\b", stripped,
                         flags=re.IGNORECASE):
                if not _explain_analyze_allowed():
                    report.blockers.append(
                        "EXPLAIN ANALYZE is not allowed here (it executes the "
                        "wrapped statement). Use plain EXPLAIN."
                    )
                    return report
                if not _explain_wraps_read(stripped):
                    report.blockers.append(
                        "EXPLAIN ANALYZE is allowed only for read queries "
                        "(SELECT / WITH). It would EXECUTE a write or DDL "
                        "statement here — submit that as a normal request."
                    )
                    return report
                # ANALYZE executes the inner query. sqlglot parses the whole
                # EXPLAIN as an opaque Command, so the module-level AST check
                # below can't see inside it — scan the inner statement here so
                # file / cross-DB / exec functions (pg_read_file, dblink, ...)
                # can't ride in via EXPLAIN ANALYZE. Use the raw (literals
                # intact) body: the literal-stripped `stripped` doesn't parse
                # once casts / IN-lists lose their operands.
                from . import ast_safety as _ast
                _inner = _explain_inner(_raw_body(stmt))
                _inner_blockers = _ast.check(_inner, engine=engine) if _inner else [
                    "EXPLAIN ANALYZE target could not be parsed."]
                if _inner_blockers:
                    report.blockers.extend(_inner_blockers)
                    return report
                # Allowed read-only EXPLAIN ANALYZE — falls through and is
                # classified 'ro' below (runs with RO credentials).

        # Destructive flag.
        if upper in destructive:
            report.is_destructive = True
            if upper not in report.keywords_found:
                report.keywords_found.append(upper)

        # MERGE is a write whose row selector is the ON condition, not a WHERE
        # clause, so it slipped past both write guards below: `MERGE INTO t
        # USING s ON true WHEN MATCHED THEN UPDATE SET ...` touches every
        # matched row, exactly what "UPDATE with always-true WHERE" blocks.
        # Check the ON condition with the same tautology rule.
        if upper == "MERGE":
            on_text = _extract_merge_on_text(sql, spec)
            if on_text is None:
                report.blockers.append(
                    "MERGE without an ON condition is blocked — the ON "
                    "condition is what limits the rows it changes.")
                return report
            if _where_is_always_true(on_text):
                report.blockers.append(
                    "MERGE with an always-true ON condition is blocked "
                    "(e.g. `ON TRUE`, `ON 1=1`). The ON condition must "
                    "constrain the rows actually affected.")
                return report

        # WHERE-required check (UPDATE, DELETE).
        if upper in WHERE_REQUIRED_KEYWORDS:
            where_text = _extract_where_text(stmt)
            if where_text is None:
                report.blockers.append(
                    f"{upper} without WHERE clause is blocked. Add a "
                    f"WHERE filter that targets specific rows."
                )
                return report
            if _where_is_always_true(where_text):
                report.blockers.append(
                    f"{upper} with always-true WHERE clause is blocked "
                    f"(e.g. `WHERE 1=1`, `WHERE TRUE`, `OR 1=1`). WHERE "
                    f"clause must constrain the rows actually affected."
                )
                return report

        # Classify tier.
        if upper in ddl_keywords:
            tier = "ddl"
        elif upper in rw_keywords:
            tier = "rw"
        elif upper == "WITH":
            stripped = _stripped_body(stmt)
            if any(_contains_keyword(stripped, k) for k in ddl_keywords):
                tier = "ddl"
            elif any(_contains_keyword(stripped, k) for k in rw_keywords):
                tier = "rw"
            else:
                tier = "ro"
        else:
            tier = "ro"

        main_tier_set.add(tier)
        report.statements.append(StatementInfo(
            raw=stmt.value.strip().rstrip(";").strip(),
            rewritten=stmt.value.strip().rstrip(";").strip(),
            kind=tier,
            leading=upper,
        ))

    if not any(s.kind != "set" for s in report.statements):
        report.blockers.append(
            "No main query found — only SET statements provided. "
            "Add at least one SELECT/INSERT/UPDATE/etc. after the SET prelude."
        )
        return report

    if len(main_tier_set) > 1:
        tiers = sorted(main_tier_set)
        report.blockers.append(
            f"Mixed-tier submission rejected: this request contains "
            f"statements at different permission tiers ({', '.join(tiers)}). "
            f"Split into separate requests so each is reviewed at its own "
            f"tier (e.g. one request for SELECTs, another for UPDATEs)."
        )
        return report

    report.main_tier = next(iter(main_tier_set)) if main_tier_set else "ro"
    report.rewritten_sql = ";\n".join(s.rewritten for s in report.statements) + ";"

    # Final pass — AST-level dangerous-function + COPY PROGRAM + pg_sleep
    # checks (sqlglot, PostgreSQL dialect). Gated by
    # bot_config.ast_safety_enabled so an operator can flip it off when
    # chasing a false positive. The earlier regex / sqlparse layer
    # remains the primary defense; this is a second layer for things
    # the regex can't see (obfuscation, function-name calls inside
    # nested expressions, COPY PROGRAM, long pg_sleep).
    from . import ast_safety  # local to keep the import boundary clean
    ast_blockers = ast_safety.check(sql, engine=engine)
    if ast_blockers:
        report.blockers.extend(ast_blockers)
    return report


def required_mode(sql: str, engine: str = "postgres") -> str:
    """Backward-compatible thin wrapper. Returns the main_tier from
    analyze() — 'ro' for empty / unrecognized / blocked queries.

    NOT an authorization input on its own. A BLOCKED query — a mixed-tier
    submission, `UPDATE` with no `WHERE`, an unparseable fragment — also reports
    'ro', so anything comparing this against a grant must call analyze() and
    return on `.blocked` FIRST. Every such caller does, and the ordering is
    pinned by tests/test_required_mode_ordering.py. Read on its own, treat this
    as a display label."""
    rep = analyze(sql, engine=engine)
    return rep.main_tier if not rep.blocked else "ro"


# ---------- SET validation ----------

# Permissive parser for `[SET] [LOCAL] <param> = <value>`.
# We don't need the value's structure, just the param name.
_SET_RE = re.compile(
    r"^\s*SET\s+(?:LOCAL\s+|SESSION\s+)?"
    r"(?P<param>[a-zA-Z_][a-zA-Z0-9_.]*)\s*"
    r"(?:=|\bTO\b)",
    re.IGNORECASE,
)

# ---- SET value policy -------------------------------------------------------
# Allow-listing the parameter NAME isn't enough: `SET LOCAL statement_timeout=0`
# would DISABLE the query timeout, and `work_mem='100GB'` would invite an OOM.
# So each safe param also gets a value policy (type + bound). Params in the
# allowlist but absent here (e.g. a custom bot_config addition) are left to the
# operator — the value passes unchecked.
_DURATION_MS_MAX = {                       # user may tighten, never disable/raise past this
    "statement_timeout": 600_000,          # 10 min — above query_timeout_sec (300s)
    "lock_timeout": 600_000,
    "idle_in_transaction_session_timeout": 600_000,
}
_SIZE_KB_MAX = {
    "work_mem": 262_144,                   # 256 MB (per sort/hash node — keep modest)
    "effective_cache_size": 67_108_864,    # 64 GB (planner hint, not an allocation)
}
_BOOL_PARAMS = {
    "enable_seqscan", "enable_indexscan", "enable_bitmapscan", "enable_hashjoin",
    "enable_mergejoin", "enable_nestloop", "enable_indexonlyscan", "geqo", "jit",
}
_FLOAT_MAX = {
    "random_page_cost": 1e10, "seq_page_cost": 1e10, "cpu_tuple_cost": 1e10,
    "cpu_index_tuple_cost": 1e10, "cpu_operator_cost": 1e10,
}
_INT_RANGE = {
    "default_statistics_target": (1, 10_000),
    "geqo_threshold": (2, 2_147_483_647),
    "from_collapse_limit": (1, 2_147_483_647),
    "join_collapse_limit": (1, 2_147_483_647),
}
_DUR_UNIT_MS = {"us": 0.001, "ms": 1, "s": 1000, "min": 60_000, "h": 3_600_000, "d": 86_400_000}
_SIZE_UNIT_KB = {"b": 1 / 1024, "kb": 1, "mb": 1024, "gb": 1024 * 1024, "tb": 1024 * 1024 * 1024}
_DUR_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(us|ms|s|min|h|d)?$", re.IGNORECASE)
_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(b|kb|mb|gb|tb)?$", re.IGNORECASE)


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] in "'\"" and v[-1] == v[0]:
        return v[1:-1].strip()
    return v


def _parse_pg_duration_ms(v: str) -> float | None:
    m = _DUR_RE.match(_unquote(v))
    if not m:
        return None
    return float(m.group(1)) * _DUR_UNIT_MS[(m.group(2) or "ms").lower()]  # bare = ms


def _parse_pg_size_kb(v: str) -> float | None:
    m = _SIZE_RE.match(_unquote(v))
    if not m:
        return None
    return float(m.group(1)) * _SIZE_UNIT_KB[(m.group(2) or "kb").lower()]  # bare = kB


def _validate_set_value(param: str, raw: str) -> tuple[bool, str]:
    """Validate the VALUE of an allow-listed SET param. Returns (ok, err)."""
    val = _unquote(raw)
    if param in _DURATION_MS_MAX:
        ms = _parse_pg_duration_ms(raw)
        if ms is None:
            return False, f"`{param}` must be a duration (e.g. 5000, '30s', '2min')."
        if ms <= 0:
            return False, (f"`{param} = {val}` would DISABLE the limit (0/unlimited "
                           f"is not allowed — it removes a safety timeout).")
        if ms > _DURATION_MS_MAX[param]:
            return False, (f"`{param} = {val}` exceeds the max "
                           f"{_DURATION_MS_MAX[param]} ms allowed here.")
        return True, ""
    if param in _SIZE_KB_MAX:
        kb = _parse_pg_size_kb(raw)
        if kb is None:
            return False, f"`{param}` must be a size (e.g. 65536, '64MB', '1GB')."
        if kb <= 0:
            return False, f"`{param} = {val}` must be a positive size."
        if kb > _SIZE_KB_MAX[param]:
            return False, (f"`{param} = {val}` exceeds the max "
                           f"{_SIZE_KB_MAX[param]} kB ({_SIZE_KB_MAX[param] // 1024} MB) here.")
        return True, ""
    if param in _BOOL_PARAMS:
        if val.lower() not in {"on", "off", "true", "false", "yes", "no", "1", "0"}:
            return False, f"`{param}` must be on/off (boolean)."
        return True, ""
    if param in _FLOAT_MAX:
        try:
            f = float(val)
        except ValueError:
            return False, f"`{param}` must be a number."
        if f < 0 or f != f or f > _FLOAT_MAX[param]:
            return False, f"`{param} = {val}` is out of the allowed range."
        return True, ""
    if param in _INT_RANGE:
        try:
            i = int(val)
        except ValueError:
            return False, f"`{param}` must be an integer."
        lo, hi = _INT_RANGE[param]
        if not (lo <= i <= hi):
            return False, f"`{param} = {val}` must be between {lo} and {hi}."
        return True, ""
    return True, ""   # allow-listed but no value policy → operator's call


def _validate_set(stmt_text: str, allowed: set[str]) -> tuple[bool, str, str]:
    """Validate a SET statement. Returns (ok, rewritten_text, err).
    On success, `rewritten_text` is the user's text with `SET ` rewritten
    to `SET LOCAL ` (idempotent — preserves explicit `SET LOCAL`).
    `SESSION` form is rejected (we don't want session-scope changes)."""
    text = _strip_sql_comments(stmt_text).strip().rstrip(";").strip()
    if re.match(r"^\s*SET\s+SESSION\b", text, flags=re.IGNORECASE):
        return False, "", (
            "SET SESSION is not allowed. Use `SET LOCAL <param> = <value>` "
            "(transaction-scoped) — bot rewrites plain `SET` to `SET LOCAL` "
            "automatically."
        )

    m = _SET_RE.match(text)
    if not m:
        return False, "", (
            "Could not parse SET statement. Expected form: "
            "`SET LOCAL <param> = <value>` (or plain `SET <param> = <value>` — "
            "we add LOCAL for you)."
        )

    param = m.group("param").lower()
    if param not in allowed:
        return False, "", (
            f"SET parameter `{param}` is not allowed. Only a small set of "
            f"safe tuning parameters (e.g. work_mem, statement_timeout) "
            f"can be used here. Contact the DBA team if you need this one."
        )

    # Validate the VALUE (type + bound) so an allow-listed param can't be
    # abused: statement_timeout=0 (disable the timeout), work_mem='100GB', etc.
    value = text[m.end():].strip()
    ok_v, err_v = _validate_set_value(param, value)
    if not ok_v:
        return False, "", err_v

    # Auto-rewrite: ensure LOCAL after SET. Idempotent.
    rewritten = re.sub(
        r"^\s*SET\s+(?!LOCAL\b)", "SET LOCAL ", text,
        count=1, flags=re.IGNORECASE,
    )
    return True, rewritten, ""


# ---------- statement utilities ----------

def _first_word_from_statement(stmt: Statement) -> str | None:
    text = _strip_sql_comments(stmt.value).lstrip(" \t\r\n;(")
    m = re.match(r"([A-Za-z][A-Za-z0-9_]*)", text)
    return m.group(1) if m else None


def _extract_where_text(stmt: Statement) -> str | None:
    from sqlparse.sql import Where
    for tok in stmt.tokens:
        if isinstance(tok, Where):
            text = tok.value.strip()
            return re.sub(r"^WHERE\s+", "", text, count=1, flags=re.IGNORECASE)
    return None


def _has_real_tokens(stmt: Statement) -> bool:
    for tok in stmt.flatten():
        if tok.is_whitespace:
            continue
        if tok.ttype in sqlparse.tokens.Comment:
            continue
        if tok.ttype is sqlparse.tokens.Punctuation and tok.value == ";":
            continue
        return True
    return False


def _extract_merge_on_text(sql: str, spec) -> str | None:
    """The ON condition of a MERGE, as SQL text, or None if absent.

    Uses sqlglot rather than sqlparse: sqlparse has no MERGE grammar, so the
    ON condition is just loose tokens there. Falls back to a regex slice when
    the dialect can't parse it, so an unparseable MERGE is treated as having no
    usable ON condition (and refused) rather than silently skipping the check."""
    import sqlglot
    from sqlglot import exp

    try:
        stmt = sqlglot.parse_one(sql, read=spec.sqlglot_dialect)
    except Exception:
        stmt = None
    if stmt is not None:
        merge = stmt if isinstance(stmt, exp.Merge) else stmt.find(exp.Merge)
        if merge is not None:
            on = merge.args.get("on")
            if on is not None:
                return on.sql(dialect=spec.sqlglot_dialect)
            return None
    m = re.search(r"\bON\b(.+?)(?:\bWHEN\b|$)", sql, re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else None


def _check_set_config_calls(sql: str, spec, set_allowed=None) -> list[str]:
    """Apply the SET allow-list + value policy to `set_config()` calls.

    `set_config(param, value, is_local)` changes a GUC exactly like `SET`, so
    it must obey the same rules. Both arguments must be literals — a computed
    parameter name can't be policy-checked, so it is refused. sqlglot resolves
    `pg_catalog.set_config` to the same Anonymous name, so the qualified form
    is covered too. Returns a list of blockers (empty = allowed)."""
    import sqlglot
    from sqlglot import exp

    try:
        parsed = sqlglot.parse(sql, read=spec.sqlglot_dialect)
    except Exception:
        return []          # unparseable SQL is ast_safety's call, not ours
    if set_allowed is None:
        set_allowed = _load_set_allowed()

    out: list[str] = []
    for stmt in parsed:
        if stmt is None:
            continue
        for call in stmt.find_all(exp.Anonymous):
            from . import ast_safety as _ast
            if _ast.anon_name(call) != "set_config":
                continue
            args = call.expressions
            if len(args) < 2 or not all(
                    isinstance(a, exp.Literal) for a in args[:2]):
                out.append(
                    "`set_config()` with a computed parameter or value is "
                    "blocked — the safety policy can only validate literal "
                    "settings. Use `SET LOCAL <param> = <value>` instead.")
                continue
            param = str(args[0].this).strip().lower()
            value = str(args[1].this)
            if param not in set_allowed:
                out.append(
                    f"`set_config('{param}', ...)` is blocked — `{param}` is "
                    f"not in the allowed settings list (same policy as `SET "
                    f"LOCAL {param}`). Allowed: "
                    f"{', '.join(sorted(set_allowed))}.")
                continue
            ok, err = _validate_set_value(param, value)
            if not ok:
                out.append(f"`set_config('{param}', '{value}')`: {err}")
    return out


def _backslash_escape_in_literal(sql: str) -> str | None:
    """The literal that makes sqlparse and the engine read different SQL, or None.

    Reproduced on a live PostgreSQL 2026-07-30, temp table, rolled back:

        submitted : UPDATE accounts SET balance = 0, note = '\\' --' WHERE id = 42
        analyze() : blocked=False, tier=rw, one statement, WHERE id = 42 SEEN
        engine    : updated 3 of 3 rows -- the whole table

    Under `standard_conforming_strings = on` (the default since PG 9.1, and the
    only mode this product supports) a backslash inside a normal literal is just
    a character, so `'\\'` is a COMPLETE one-character string and the `--` that
    follows opens a comment which swallows the rest of the line, WHERE included.
    sqlparse instead treats `\\'` as an escaped quote, keeps the literal open,
    and hands the classifier a statement whose WHERE looks present. The approving
    DBA reads a WHERE the engine never sees.

    The existing statement-count gate cannot fire here: both parsers agree there
    is exactly ONE statement. Only the meaning differs.

    So the rule is about the tokenizer, not the count: inside a normal
    single-quoted literal, a backslash immediately before a quote is refused. It
    is never NEEDED — the SQL escape for a quote is `''` in both PostgreSQL and
    T-SQL — and where a trailing backslash is genuinely wanted, `E'\\\\'`
    states it unambiguously to both readers.

    Dollar-quoted bodies and E-strings are skipped: inside them the backslash has
    its own agreed meaning and neither parser is misled.
    """
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        # $tag$ ... $tag$ — no escape processing inside, both parsers agree.
        if ch == "$":
            m = re.match(r"\$[A-Za-z_]*\$", sql[i:])
            if m:
                tag = m.group(0)
                end = sql.find(tag, i + len(tag))
                i = n if end == -1 else end + len(tag)
                continue
        if ch == '"':                      # quoted identifier: no \ escapes
            j = sql.find('"', i + 1)
            i = n if j == -1 else j + 1
            continue
        if ch == "'":
            # E'...' asks for backslash escapes explicitly; both readers honour it.
            explicit = i > 0 and sql[i - 1] in "eE" and (
                i == 1 or not (sql[i - 2].isalnum() or sql[i - 2] == "_"))
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":   # '' is an escaped quote
                        j += 2
                        continue
                    break
                if sql[j] == "\\" and not explicit and j + 1 < n and sql[j + 1] == "'":
                    return sql[i:min(j + 2, n)]
                j += 1
            i = j + 1
            continue
        i += 1
    return None


def _statement_count_disagrees(sql: str, spec) -> bool:
    """True when sqlparse and the engine's own parser split `sql` into a
    different number of statements — the signature of a parser differential
    (see the gate in analyze()).

    Counts only statements with real content on both sides, so trailing
    semicolons and comment-only chunks don't create phantom mismatches. A
    sqlglot parse failure returns False (no *disagreement* was established):
    unparseable SQL is ast_safety's job — it rejects it outright — and
    treating a parser gap as a differential here would block legitimate
    engine-specific syntax that sqlglot doesn't model yet."""
    import sqlglot
    from sqlglot import expressions as sqlglot_exp

    try:
        parsed = sqlglot.parse(sql, read=spec.sqlglot_dialect)
    except Exception:
        return False
    # Both sides must count only statements that carry real SQL. sqlglot parses a
    # trailing comment into its own node — `SELECT 1; -- note` becomes
    # [Select, Semicolon(comments=[' note'])] — while sqlparse keeps the comment
    # attached to the statement it follows. Comparing raw counts therefore
    # blocked `UPDATE ... WHERE id = 1; -- ticket QH-42`: ordinary SQL, and a
    # false positive bad enough to make the whole gate look broken.
    #
    # The test is STRUCTURAL, not textual. Filtering on the rendered SQL looks
    # equivalent and is not: `s.sql()` without a dialect cannot render a T-SQL
    # `EXECUTE` node, returns an empty string, and the smuggled
    # `EXEC xp_cmdshell` node would be dropped as if it were a comment — turning
    # a false positive into a false negative on exactly the payload this gate
    # exists for. A comment-only node is an argument-less Semicolon; nothing
    # else is.
    engine_stmts = [
        s for s in parsed
        if s is not None and not (isinstance(s, sqlglot_exp.Semicolon)
                                  and not s.args)
    ]
    if not engine_stmts:
        return False
    sqlparse_stmts = [s for s in sqlparse.parse(sql) if _has_real_tokens(s)]
    return len(engine_stmts) != len(sqlparse_stmts)


def _raw_body(stmt: Statement) -> str:
    """Statement text with comments stripped but string literals INTACT —
    safe to hand to a real SQL parser. Unlike `_stripped_body` (which also
    removes literals for keyword scanning), this still parses: stripping
    literals turns `x = 'a'::uuid AND y IN ('p','q')` into `x = ::uuid AND
    y IN (,)`, which no parser accepts."""
    return _strip_sql_comments(stmt.value).strip()


def _stripped_body(stmt: Statement) -> str:
    raw = _strip_sql_comments(stmt.value)
    out: list[str] = []
    in_string = False
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "'":
            if in_string and i + 1 < n and raw[i + 1] == "'":
                i += 2
                continue
            in_string = not in_string
            i += 1
            continue
        if in_string:
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _contains_keyword(text: str, keyword: str) -> bool:
    return re.search(rf"\b{re.escape(keyword)}\b", text,
                     flags=re.IGNORECASE) is not None


# ---------- tautology detection ----------
#
# Two layers, because they fail in different directions.
#
# The text layer below (_expr_always_true and friends) works on the raw WHERE
# string. It is robust — it runs even when the SQL doesn't parse and when the
# AST pass is turned off — but it can only recognise shapes someone wrote a
# pattern for, so it misses anything phrased differently: `id IS NOT NULL OR id
# IS NULL`, `EXISTS (SELECT 1)`, `1 BETWEEN 0 AND 2`, `random() < 2`,
# `COALESCE(NULL,1)=1`, `id IN (SELECT id FROM t)`. Each of those is `WHERE
# TRUE` over the whole table, and each one passed.
#
# The AST layer (_where_ast_has_no_row_filter) closes that class structurally:
# instead of matching text it asks whether the predicate can distinguish one row
# from another at all. That single question covers most of the list, because a
# WHERE that never mentions a column evaluates identically for every row.
#
# The guard is an assist, not the last line of defence — a DBA still reads the
# statement. But a tool that advertises "always-true WHERE is blocked" and then
# waves through `WHERE id IS NOT NULL OR id IS NULL` is worse than one that
# never claimed it, because it buys the reviewer's trust.

def _where_is_always_true(where_text: str) -> bool:
    cleaned = _strip_sql_comments(where_text).strip()
    cleaned = cleaned.rstrip(";").strip()
    if not cleaned:
        return False
    if _expr_always_true(cleaned):
        return True
    return _where_ast_has_no_row_filter(cleaned)


def _where_ast_has_no_row_filter(where_text: str) -> bool:
    """True when the predicate provably cannot select a subset of rows.

    Parsed with sqlglot, so this is about structure rather than spelling. Three
    rules, each conservative — every one of them is "this cannot filter", not
    "this looks suspicious":

      1. No column reference anywhere in the predicate. Then it evaluates the
         same for every row, so the statement touches all rows or none:
         `EXISTS (SELECT 1)`, `1 BETWEEN 0 AND 2`, `random() < 2`,
         `COALESCE(NULL,1)=1`. Provably-false predicates are excluded — those
         touch nothing, and calling them "always true" would be a lie.
      2. A column IS NULL OR'd with the same column IS NOT NULL — every row
         satisfies one of the two.
      3. `col IN (SELECT col FROM ...)` where the subquery selects the same
         bare column with no WHERE of its own: satisfied by every row whose
         column is non-null, i.e. not a filter.

    Anything this can't parse falls through to False and the text layer's
    verdict stands: an unparseable WHERE must not become a free pass.
    """
    try:
        import sqlglot
        from sqlglot import exp
    except Exception:
        return False

    try:
        # Wrap so a bare condition parses; SELECT 1 contributes no columns.
        tree = sqlglot.parse_one(f"SELECT 1 WHERE {where_text}", read="postgres")
    except Exception:
        return False
    if tree is None:
        return False
    where = tree.find(exp.Where)
    if where is None or where.this is None:
        return False
    cond = where.this

    # Rule 1 — nothing in here varies per row.
    if not list(cond.find_all(exp.Column)):
        # `WHERE 1=0` affects no rows; blocking it as always-true would be
        # both wrong and confusing. The text layer knows the false shapes.
        if _expr_always_false(where_text):
            return False
        return True

    def _or_operands(node):
        if isinstance(node, exp.Paren):
            return _or_operands(node.this)
        if isinstance(node, exp.Or):
            return _or_operands(node.left) + _or_operands(node.right)
        return [node]

    def _null_check(node):
        """(column_sql, negated) for `x IS NULL` / `x IS NOT NULL`.

        sqlglot has spelled `IS NOT NULL` two different ways: as an outer
        `Not` wrapping an `Is` (<= 30.10), and as the `Is` node carrying
        `negate=True` (>= 30.11). Read BOTH. This is not defensive padding —
        when only the old shape was handled, a newer sqlglot made
        `WHERE id IS NOT NULL OR id IS NULL` look like the same plain
        `IS NULL` twice, the tautology went unrecognised, and a full-table
        UPDATE stopped being blocked. A parser upgrade must not be able to
        quietly widen what this gate lets through.
        """
        if isinstance(node, exp.Paren):
            return _null_check(node.this)
        negated = False
        if isinstance(node, exp.Not):
            negated, node = True, node.this
            if isinstance(node, exp.Paren):
                node = node.this
        if isinstance(node, exp.Is) and isinstance(node.expression, exp.Null):
            if node.args.get("negate"):
                negated = not negated       # NOT (x IS NOT NULL) is x IS NULL
            col = node.this
            if isinstance(col, exp.Column):
                return col.sql(dialect="postgres").lower(), negated
        return None

    # Rule 2 — `x IS NULL OR x IS NOT NULL` covers every row.
    operands = _or_operands(cond)
    if len(operands) > 1:
        seen: dict[str, set[bool]] = {}
        for op in operands:
            found = _null_check(op)
            if found is None:
                continue
            seen.setdefault(found[0], set()).add(found[1])
        if any(len(v) == 2 for v in seen.values()):
            return True

    # Rule 3 — membership in a subquery that selects the same column unfiltered.
    for node in [cond, *cond.find_all(exp.In)]:
        if not isinstance(node, exp.In):
            continue
        left = node.this
        query = node.args.get("query")
        if not isinstance(left, exp.Column) or query is None:
            continue
        select = query.this if isinstance(query, exp.Subquery) else query
        if not isinstance(select, exp.Select):
            continue
        if select.args.get("where") is not None:
            continue
        projections = select.expressions or []
        if len(projections) != 1 or not isinstance(projections[0], exp.Column):
            continue
        if projections[0].name.lower() == left.name.lower():
            return True

    # Rule 4 — a reflexive comparison of an expression with ITSELF.
    #
    # The text layer already caught the bare `id = id` via a backreference, and
    # that made the guard look finished. It is not: the backreference only
    # matches a plain identifier, so every wrapped form walked straight through —
    # `id::text = id::text`, `(id) = (id)`, `upper(name) = upper(name)`,
    # `abs(id) = abs(id)`, `COALESCE(id,0) = COALESCE(id,0)`,
    # `CAST(id AS text) = CAST(id AS text)`, `id >= id`,
    # `id IS NOT DISTINCT FROM id`. Eleven of thirteen probe payloads.
    #
    # `DELETE FROM t WHERE id::text = id::text` deletes every row whose id is
    # not null, which on a primary key is the whole table — the exact outcome
    # this guard claims to prevent, and the README lists it as part of the
    # security model.
    #
    # Comparing the two sides' rendered SQL rather than their text catches every
    # wrapping, because sqlglot normalises spelling: `(id)` and `id` render the
    # same, `CAST(id AS text)` and `id::text` both render as a Cast. Only
    # operators that are TRUE when both sides are equal belong here — `<>`, `<`
    # and `>` are false in that case, and `IS DISTINCT FROM` likewise, so
    # including them would reject legitimate always-false predicates.
    def _unparen(node):
        # `((id)) = (id)` is still a self-comparison; the two sides only render
        # differently because of how many redundant parens each side carries.
        while isinstance(node, exp.Paren) and node.this is not None:
            node = node.this
        return node

    reflexive = (exp.EQ, exp.GTE, exp.LTE, exp.NullSafeEQ)

    def _self_comparison(node) -> bool:
        if not isinstance(node, reflexive):
            return False
        left, right = _unparen(node.this), _unparen(node.expression)
        if left is None or right is None:
            return False
        try:
            return (left.sql(dialect="postgres").strip().lower()
                    == right.sql(dialect="postgres").strip().lower())
        except Exception:
            return False  # unrenderable in this dialect: no claim either way

    # Rule 5 — a pattern match that matches everything.
    #
    # `col LIKE '%'` and `col SIMILAR TO '%'` are true for every non-null value,
    # so they filter exactly as much as `col IS NOT NULL` and read like a real
    # predicate. Only a literal all-wildcard pattern counts; `'%abc%'` is a real
    # filter.
    def _matches_everything(node) -> bool:
        if not isinstance(node, (exp.Like, exp.ILike, exp.SimilarTo)):
            return False
        pattern = node.expression
        return (isinstance(pattern, exp.Literal) and pattern.is_string
                and bool(pattern.this) and set(pattern.this) <= {"%"})

    # Rules 4 and 5 have to respect boolean structure, not just scan for a
    # matching node anywhere in the tree. `find_all` did the latter, and it was
    # wrong in the safe direction for OR and the UNSAFE-to-usability direction
    # for AND: `UPDATE t SET a=1 WHERE 1=1 AND id=5` was rejected even though
    # `id=5` constrains the rows perfectly well. Padding a WHERE with `1=1` is
    # ordinary generated-SQL practice and is explicitly allowed.
    #
    # So walk the operators:
    #   AND — always true only if BOTH sides are; one real filter is enough to
    #         constrain the statement.
    #   OR  — always true if EITHER side is; a tautology anywhere in a
    #         disjunction makes the whole predicate match every row.
    def _tautology(node) -> bool:
        node = _unparen(node)
        if isinstance(node, exp.And):
            return _tautology(node.left) and _tautology(node.right)
        if isinstance(node, exp.Or):
            return _tautology(node.left) or _tautology(node.right)
        return _self_comparison(node) or _matches_everything(node)

    return _tautology(cond)


def _expr_always_true(expr: str) -> bool:
    expr = expr.strip()
    expr = _strip_redundant_outer_parens(expr)

    or_parts = _split_top_level(expr, "OR")
    if len(or_parts) > 1:
        return any(_expr_always_true(p) for p in or_parts)

    and_parts = _split_top_level(expr, "AND")
    if len(and_parts) > 1:
        return all(_expr_always_true(p) for p in and_parts)

    not_match = re.match(r"^NOT\s+(.+)$", expr, flags=re.IGNORECASE)
    if not_match:
        return _expr_always_false(not_match.group(1).strip())

    return _atom_is_tautology(expr)


def _expr_always_false(expr: str) -> bool:
    expr = _strip_redundant_outer_parens(expr.strip())
    norm = _normalize_atom(expr)
    if norm in {"false", "0"}:
        return True
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)=(-?\d+(?:\.\d+)?)", norm)
    if m and m.group(1) != m.group(2):
        return True
    m = re.fullmatch(r"'([^']*)'='([^']*)'", norm)
    if m and m.group(1) != m.group(2):
        return True
    return False


def _atom_is_tautology(atom: str) -> bool:
    norm = _normalize_atom(atom)
    if norm in {"true", "1"}:
        return True

    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)=(-?\d+(?:\.\d+)?)", norm)
    if m and m.group(1) == m.group(2):
        return True

    m = re.fullmatch(r"'([^']*)'='([^']*)'", norm)
    if m and m.group(1) == m.group(2):
        return True

    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)(?:<>|!=)(-?\d+(?:\.\d+)?)", norm)
    if m and m.group(1) != m.group(2):
        return True

    m = re.fullmatch(r"'([^']*)'(?:<>|!=)'([^']*)'", norm)
    if m and m.group(1) != m.group(2):
        return True

    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)(>=|<=|>|<)(-?\d+(?:\.\d+)?)", norm)
    if m:
        a, op, b = float(m.group(1)), m.group(2), float(m.group(3))
        if op == ">" and a > b:
            return True
        if op == ">=" and a >= b:
            return True
        if op == "<" and a < b:
            return True
        if op == "<=" and a <= b:
            return True

    m = re.fullmatch(r"([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)?)=\1", norm)
    if m:
        return True

    return False


def _normalize_atom(atom: str) -> str:
    out = []
    in_string = False
    for ch in atom:
        if ch == "'":
            in_string = not in_string
            out.append(ch)
        elif in_string:
            out.append(ch)
        elif ch.isspace():
            continue
        else:
            out.append(ch.lower())
    return "".join(out)


def _strip_sql_comments(text: str) -> str:
    result = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            result.append(ch)
            i += 1
            while i < n:
                if text[i] == "'":
                    if i + 1 < n and text[i + 1] == "'":
                        result.append("''")
                        i += 2
                        continue
                    result.append("'")
                    i += 1
                    break
                result.append(text[i])
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if ch == "-" and i + 1 < n and text[i + 1] == "-":
            while i < n and text[i] != "\n":
                i += 1
            continue
        result.append(ch)
        i += 1
    return "".join(result)


def _strip_redundant_outer_parens(expr: str) -> str:
    while expr.startswith("(") and expr.endswith(")"):
        depth = 0
        outer_match = True
        for i, ch in enumerate(expr):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(expr) - 1:
                    outer_match = False
                    break
        if not outer_match:
            break
        expr = expr[1:-1].strip()
    return expr


def _split_top_level(expr: str, keyword: str) -> list[str]:
    pattern = re.compile(rf"\b{re.escape(keyword)}\b", flags=re.IGNORECASE)
    parts: list[str] = []
    depth = 0
    in_string = False
    i = 0
    last = 0
    n = len(expr)
    while i < n:
        ch = expr[i]
        if ch == "'":
            if in_string and i + 1 < n and expr[i + 1] == "'":
                i += 2
                continue
            in_string = not in_string
            i += 1
            continue
        if in_string:
            i += 1
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth -= 1
            i += 1
            continue
        if depth == 0:
            m = pattern.match(expr, i)
            if m:
                start, end = m.span()
                before_ok = (start == 0 or expr[start - 1].isspace()
                             or expr[start - 1] == ")")
                after_ok = (end == n or expr[end].isspace()
                            or expr[end] == "(")
                if before_ok and after_ok:
                    parts.append(expr[last:start].strip())
                    last = end
                    i = end
                    continue
        i += 1
    parts.append(expr[last:].strip())
    return [p for p in parts if p]
