"""sqlparse_patch: the tree sqlparse builds, without the cubic cost.

sqlparse built each new group's text by flattening the whole subtree beneath
it. An operator chain nests one group per operator, so its cost was cubic in
the chain's length: 1,000 chained `+` took 7 s to refuse, from a statement of
4 KB. The patch joins the children's texts instead.

These tests pin both halves of the claim:
- the tree is the same, compared constructor against constructor;
- a long chain is refused fast.
"""
import ast
import random
import time
import warnings
from pathlib import Path

import pytest
import sqlparse
from sqlparse import sql

from queryhub import query_safety as qs
from queryhub import sqlparse_patch

TESTS = Path(__file__).parent

# The leading words of the string constants in this suite that are worth
# parsing: SQL, including the adversarial statements the safety tests hold.
_SQL_START = ("SELECT", "WITH", "UPDATE", "INSERT", "DELETE", "CREATE", "ALTER",
              "DROP", "EXPLAIN", "SET", "BEGIN", "DO", "VACUUM", "ANALYZE",
              "TRUNCATE", "GRANT", "REVOKE", "EXEC", "DECLARE", "MERGE", "COPY",
              "CALL", "SHOW", "REFRESH", "REINDEX", "CLUSTER", "COMMENT", "--", "/*")


def _suite_statements():
    out = []
    for path in sorted(TESTS.glob("*.py")):
        with warnings.catch_warnings():
            # A test file's own non-raw "\;" is its business, not this test's.
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and len(node.value) < 20_000
                    and node.value.lstrip().upper().startswith(_SQL_START)):
                out.append(node.value)
    for path in sorted((TESTS / "corpus").glob("*.txt")):
        out += [line for line in path.read_text().splitlines()
                if line.strip() and not line.startswith("#")]
    return out


_OPERANDS = ["a", "t.a", '"s"."t"', "[c]", "42", "1.5", "0x1F", "-1", "'x'", "$1",
             "%s", "?", "@v", "x::int", "CAST(x AS int)", "f(1)", "count(*)",
             "INTERVAL '1' DAY", "DATE '2020-01-01'", "CURRENT_DATE", "NULL", "TRUE",
             "a[1]", "ARRAY[1, 2]", "t.*", "sum(a) OVER (ORDER BY a)", "int", "date",
             "CASE WHEN a THEN 1 END", "(SELECT 1)", "~a", "-a", "a b"]
_OPERATORS = ["+", "-", "*", "/", "||", "->", "->>", "#>", "@>", "DIV", "=", "<>",
              "<", "LIKE", "~", "AND", ",", "/* c */"]


def _fuzzed(count, seed=1):
    rnd = random.Random(seed)
    out = []
    for _ in range(count):
        parts = [rnd.choice(_OPERANDS)]
        for _ in range(rnd.choice([1, 5, 30, 99, 100, 101, 120])):
            parts += [rnd.choice(_OPERATORS), rnd.choice(_OPERANDS)]
        out.append(rnd.choice(["SELECT ", "SELECT * FROM t WHERE "]) + " ".join(parts))
    return out


def _shape(statements):
    """Everything a caller can read off the tree, in order."""
    out = []

    def walk(tok, depth):
        out.append((depth, type(tok).__name__, tok.ttype, tok.value, tok.normalized,
                    tok.is_keyword, tok.is_group, tok.is_whitespace, tok.is_newline))
        if tok.is_group:
            for child in tok.tokens:
                assert child.parent is tok
                walk(child, depth + 1)

    for statement in statements:
        out.append(("text", str(statement)))
        walk(statement, 0)
    return out


def _parse_with(init, text):
    saved = sql.TokenList.__init__
    setattr(sql.TokenList, "__init__", init)
    try:
        return "parsed", _shape(sqlparse.parse(text))
    except sqlparse.exceptions.SQLParseError as exc:
        return "refused", str(exc)
    finally:
        setattr(sql.TokenList, "__init__", saved)


def _differences(statements):
    return [text[:120] for text in statements
            if _parse_with(sqlparse_patch.ORIGINAL, text)
            != _parse_with(sqlparse_patch.init_from_children, text)]


# --- in place ----------------------------------------------------------------------

def test_a_group_takes_its_text_from_its_children():
    """This module's constructor on sqlparse 0.5, sqlparse's own on 0.6.

    If this fails after a sqlparse upgrade, the constructor changed again.
    Re-measure before trusting the new one, then teach apply() about it."""
    assert sqlparse_patch.apply() is True
    assert sqlparse_patch.joins_children(sql.TokenList.__init__)


def test_apply_is_idempotent():
    assert sqlparse_patch.apply() is True
    installed = sql.TokenList.__init__
    assert sqlparse_patch.apply() is True
    assert sql.TokenList.__init__ is installed


class _Upstream:
    # sqlparse 0.6.0's constructor, character for character.
    def __init__(self, tokens=None):
        self.tokens = tokens or []
        [setattr(token, 'parent', self) for token in self.tokens]
        super().__init__(None, ''.join(token.value for token in self.tokens))
        self.is_group = True


def test_the_upstream_fix_is_left_in_place(monkeypatch):
    monkeypatch.setattr(sql.TokenList, "__init__", _Upstream.__init__)
    assert sqlparse_patch.apply() is True
    assert sql.TokenList.__init__ is _Upstream.__init__


def test_a_constructor_it_does_not_know_is_left_alone(monkeypatch):
    def other(self, tokens=None):
        sqlparse_patch.ORIGINAL(self, tokens)

    monkeypatch.setattr(sql.TokenList, "__init__", other)
    assert sqlparse_patch.apply() is False
    assert sql.TokenList.__init__ is other


# --- the same tree -----------------------------------------------------------------

def test_the_suite_has_statements_to_compare():
    assert len(_suite_statements()) > 1000


def test_every_statement_in_the_suite_builds_the_same_tree():
    assert _differences(_suite_statements()) == []


def test_fuzzed_chains_build_the_same_tree_or_are_refused_alike():
    """Chains up to 120 operators, past sqlparse's limit of 100, mixing the
    operands and separators that end a chain for sqlparse."""
    assert _differences(_fuzzed(400)) == []


# --- fast --------------------------------------------------------------------------

@pytest.mark.parametrize("sql_text", [
    "SELECT 1" + " + 1" * 1000,
    "SELECT 'a'" + " || 'a'" * 1000,
    "SELECT 1" + " = 1" * 1000,
    "SELECT f(1)" + " + f(1)" * 1000,
    "SELECT 1 + 1" + " = 1 + 1" * 1000,
    "SELECT d" + " -> 'k'" * 1000,
], ids=["plus", "concat", "comparison", "function-operands", "mixed", "json"])
def test_a_long_chain_is_refused_in_well_under_a_second(sql_text):
    """Before the patch these took 7 to 14 s each; with it, about 0.1 s."""
    started = time.perf_counter()
    report = qs.analyze(sql_text)
    elapsed = time.perf_counter() - started
    assert report.blocked
    assert "nested too deeply" in report.blockers[0]
    assert elapsed < 1.0, f"{elapsed:.2f}s"
