"""A parser that blows up must refuse the query, never reach the user as a 500.

`ast_safety.check` runs on the submit path and already meant to fail closed: its
message says "Check for stray quotes" in as many words. But it caught
`sqlglot.errors.ParseError`, and `TokenError` is a SIBLING of ParseError under
`SqlglotError`, not a subclass. The tokenizer raises before a parse is ever
attempted, so every unbalanced quote escaped the handler and came out of the web
API as "internal error" — on the exact input the handler was written for.

That is the shape worth pinning, so this file asserts the hierarchy assumption
itself rather than only the behaviour. If a future sqlglot re-parents its error
classes, the assertion about the family fails loudly instead of quietly widening
the hole again.
"""
import pytest
import sqlglot.errors

from dba_slack_bot import ast_safety


UNTERMINATED = (
    "SELECT CASE WHEN a IS NULL THEN 'truncated mid-line\n"
    "            WHEN b IS NULL THEN 'other' ELSE 'x' END AS d FROM t;"
)


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(ast_safety, "is_enabled", lambda engine="postgres": True)


# ---------------------------------------------------------------------------
# the assumption that broke
# ---------------------------------------------------------------------------

def test_token_error_is_not_a_parse_error():
    """The whole bug in one assertion. Catching ParseError does not catch this."""
    assert not issubclass(sqlglot.errors.TokenError, sqlglot.errors.ParseError)
    assert issubclass(sqlglot.errors.TokenError, sqlglot.errors.SqlglotError)
    assert issubclass(sqlglot.errors.ParseError, sqlglot.errors.SqlglotError)


def test_an_unterminated_literal_really_does_raise_tokenerror():
    """Pins the reproduction, so the tests below cannot pass against an input
    that stopped exercising the tokenizer."""
    with pytest.raises(sqlglot.errors.TokenError):
        sqlglot.parse(UNTERMINATED, read="postgres")


# ---------------------------------------------------------------------------
# fail closed, with something the user can act on
# ---------------------------------------------------------------------------

def test_unterminated_literal_is_blocked_not_raised():
    blockers = ast_safety.check(UNTERMINATED, engine="postgres")
    assert blockers, "an unparseable query must be refused"
    assert "stray quotes" in blockers[0]


@pytest.mark.parametrize("sql", [
    "SELECT * FROM t WHERE (a = 1;",        # unbalanced parenthesis
    "SELECT 'abc FROM t;",                  # single quote left open
    'SELECT "col FROM t;',                  # identifier quote left open
    "SELECT CASE END FROM t;",              # structurally invalid
])
def test_malformed_shapes_all_block_cleanly(sql):
    assert ast_safety.check(sql, engine="postgres")


def test_an_unknown_parser_exception_still_blocks(monkeypatch):
    """The backstop. A parser is a moving dependency on the submit path, so an
    exception class this code has never seen must still refuse the query."""
    class Surprise(Exception):
        pass

    def boom(*a, **kw):
        raise Surprise("sqlglot did something new")

    monkeypatch.setattr(ast_safety.sqlglot, "parse", boom)
    blockers = ast_safety.check("SELECT 1;", engine="postgres")
    assert blockers and "could not be parsed" in blockers[0]


def test_valid_sql_is_untouched():
    """The guard must not become a wall. A correct version of the query that
    triggered this returns no blockers."""
    fixed = (
        "SELECT CASE WHEN a IS NULL THEN 'only in db'\n"
        "            WHEN b IS NULL THEN 'only in doc'\n"
        "            ELSE 'matched' END AS status, c FROM t ORDER BY status;"
    )
    assert ast_safety.check(fixed, engine="postgres") == []
