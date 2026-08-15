"""Ask the SERVER how many statements it sees, and refuse to run if it
disagrees with what was reviewed.

Every parser this gateway owns is a guess about what the database will do with
a string. `query_safety` splits with sqlparse and cross-checks against sqlglot,
which catches the differentials we know about — but a tokenizer gap nobody has
found yet looks exactly like a single statement to both of them. The only
authority that cannot be wrong about how a server reads a batch is that server.

PostgreSQL gives this away for free: `executor._execute_main_statement` runs
the statement over the extended protocol, and the server itself refuses a
string carrying more than one command ("cannot insert multiple commands into a
prepared statement"). Nothing to implement.

SQL Server has no equivalent. Measured on this fleet 2026-07-30: a two-command
batch runs unchallenged both through a plain `execute()` and through a
parameterized one (pyodbc routes it via `sp_executesql`, which executes a batch
by definition). So the wire-level backstop is genuinely absent there, and the
closest real substitute is to make the server COMPILE the batch without running
it and count the statements it found:

    SET SHOWPLAN_XML ON  ->  one <Stmt*> element per statement
      SELECT 1                          -> 1
      SELECT 1; SELECT 2                -> 2
      SELECT 1; SELECT 2; SELECT 3      -> 3

That is the server's own parser answering, not a third reimplementation of it.

Two deliberate asymmetries in how failure is treated:

- **A disagreement is fail-CLOSED.** If the server found more statements than
  were approved, the reviewer did not see what would run. That is the whole
  bug class; it stops here.
- **An unavailable check is fail-OPEN**, with a warning. SHOWPLAN needs a
  permission the login may not hold on every database, and losing the check
  puts us exactly where we were before this module existed — still behind the
  `\\'` gate and the sqlparse/sqlglot count agreement. A hardening layer that
  can take a working gateway offline when a permission is missing would get
  switched off, and then it protects nothing.

Cost is one extra compile per statement on SQL Server. Adding an engine means
adding one resolver to `_RESOLVERS`; an engine with a wire-level guarantee (or
no way to ask) simply has no entry, and `check()` is then a no-op.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

log = logging.getLogger(__name__)

#: SHOWPLAN emits one element per statement whose tag starts with "Stmt"
#: (StmtSimple for a plain query, StmtCond for IF/WHILE, StmtCursor...).
#: Namespaced, so compare on the local name only.
_STMT_TAG_PREFIX = "Stmt"


class TooManyStatements(RuntimeError):
    """The server found more statements in the batch than were approved."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _mssql_statement_count(cur, sql: str) -> int | None:
    """Statements SQL Server finds in `sql`, or None if it would not say.

    SHOWPLAN_XML compiles without executing, so this cannot have a side
    effect of its own. It must be turned off again even on failure: leaving it
    on would make the caller's real statement return a plan document instead of
    rows.
    """
    docs: list[str] = []
    try:
        cur.execute("SET SHOWPLAN_XML ON")
    except Exception as exc:                       # permission, or not supported
        log.warning("stmt_guard: SHOWPLAN_XML unavailable (%s) — statement "
                    "count not verified against the server", exc)
        return None
    try:
        cur.execute(sql)
        while True:
            docs += [row[0] for row in cur.fetchall()]
            if not cur.nextset():
                break
    except Exception as exc:
        # A statement the optimizer cannot compile (a temp table this session
        # has not created yet, for instance) is not evidence of smuggling.
        log.warning("stmt_guard: could not compile the statement for counting "
                    "(%s) — not verified", exc)
        return None
    finally:
        try:
            cur.execute("SET SHOWPLAN_XML OFF")
        except Exception:                          # pragma: no cover - defensive
            log.exception("stmt_guard: failed to turn SHOWPLAN_XML back off")
            raise                                  # a plan-mode cursor is unusable

    n = 0
    for doc in docs:
        try:
            root = ET.fromstring(doc)
        except ET.ParseError as exc:
            log.warning("stmt_guard: unparseable plan document (%s)", exc)
            return None
        n += sum(1 for el in root.iter()
                 if _local(el.tag).startswith(_STMT_TAG_PREFIX))
    return n or None


#: engine -> callable(cur, sql) -> int | None. No entry means no check:
#: postgres is covered at the wire by the extended protocol.
_RESOLVERS = {
    "mssql": _mssql_statement_count,
}


def supported(engine: str) -> bool:
    return engine in _RESOLVERS


def check(cur, sql: str, *, engine: str, request_id: int | None = None) -> None:
    """Raise TooManyStatements if the server reads `sql` as more than one
    statement. Silent when the engine has no resolver or the server would not
    answer.

    Call this BEFORE anything opens a portal or a result set on `cur`: it
    issues its own statements on the same cursor, and interleaving that with a
    live result is what caused the 2026-07-30 hang on the Postgres side.
    """
    resolver = _RESOLVERS.get(engine)
    if resolver is None:
        return
    n = resolver(cur, sql)
    if n is None or n <= 1:
        return
    log.error("Request %s: server reads the approved statement as %d "
              "statements — refusing to execute.", request_id, n)
    raise TooManyStatements(
        f"The database server reads this as {n} separate statements, but it "
        f"was reviewed and approved as one. Refusing to run it — the approver "
        f"did not see everything it would do. Submit each statement "
        f"separately, or use the batch option."
    )
