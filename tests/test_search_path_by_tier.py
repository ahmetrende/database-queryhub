"""The search_path order differs for DDL, and both orders are deliberate.

`pg_catalog` first stops a same-named object in a writable schema from shadowing
a built-in (CVE-2018-1058 class) — the right default when the bot runs someone
else's SELECT or UPDATE.

For DDL it is a wall rather than a hardening: an unqualified `CREATE` lands in the
FIRST schema on the path, so with pg_catalog leading, `CREATE TABLE t (...)` fails
with "permission denied for schema pg_catalog". Measured against a real database,
not reasoned about — the comment in the code used to claim the opposite ("still
lands in public"), and nobody had noticed because no DDL had ever run through this
pipeline (`required_tier='ddl'`: zero requests, ever).

Both directions are pinned here. Flipping the order for ro/rw would silently undo
SEC-PATH, and that would look like a passing test suite.
"""
from __future__ import annotations

import pathlib
import re

SRC = (pathlib.Path(__file__).resolve().parents[1] / "src" / "dba_slack_bot"
       / "executor.py").read_text(encoding="utf-8")


def _the_block() -> str:
    """The search_path decision, as source text.

    Anchored on `path = (` rather than a few lines earlier: the line above is
    `scope = "" if autocommit else "LOCAL "`, and a regex looking for `else "..."`
    matched THAT — which is how the first version of this file reported a failure
    against correct code.
    """
    i = SRC.index("path = (")
    return SRC[i:i + 200]


def test_ddl_puts_the_writable_schema_first():
    block = _the_block()
    m = re.search(r'"(?P<ddl>[^"]*)"\s*if\s*mode\s*==\s*"ddl"', block)
    assert m, "the DDL branch is gone from the search_path decision"
    order = [s.strip() for s in m.group("ddl").split(",")]
    assert order[0] == "public", (
        f"DDL search_path starts with {order[0]!r}; an unqualified CREATE would "
        f"be refused for schema {order[0]!r}")
    assert "pg_catalog" in order, "pg_catalog must still be reachable"


def test_read_and_write_keep_pg_catalog_first():
    block = _the_block()
    m = re.search(r'if\s*mode\s*==\s*"ddl"\s*\n?\s*else\s*"(?P<other>[^"]*)"',
                  block)
    assert m, "the non-DDL branch is gone"
    order = [s.strip() for s in m.group("other").split(",")]
    assert order[0] == "pg_catalog", (
        f"ro/rw search_path starts with {order[0]!r} — a writable schema first "
        f"lets a planted object shadow a built-in in someone else's query, "
        f"which is what SEC-PATH exists to prevent")


def test_the_two_orders_are_not_the_same():
    """A refactor that collapsed them would break one of the two properties."""
    block = _the_block()
    ddl = re.search(r'"([^"]*)"\s*if\s*mode\s*==\s*"ddl"', block).group(1)
    other = re.search(r'if\s*mode\s*==\s*"ddl"\s*\n?\s*else\s*"([^"]*)"',
                       block).group(1)
    assert ddl != other


def test_the_decision_is_keyed_on_the_tier_not_the_principal():
    """It must not depend on who submitted: an admin's SELECT should get the
    strict order too, and a non-admin's granted DDL should still work."""
    block = _the_block()
    assert "mode ==" in block
    for principal_ish in ("is_super_admin", "is_admin", "requester"):
        assert principal_ish not in block, (
            f"search_path keyed on {principal_ish} — it belongs to the statement, "
            f"not the person")
