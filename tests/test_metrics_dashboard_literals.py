"""The S3 metrics page never prints a value typed into a query.

The low-rating table showed the first 100 characters of each rated query.
Masking covers what a query returns, not the query's own text. So one row
showed a phone number that someone had typed into a WHERE clause.
"""
import importlib.util
from pathlib import Path

import pytest

BUILDER = Path(__file__).resolve().parents[1] / "scripts" / "build_metrics_dashboard.py"


def _builder():
    spec = importlib.util.spec_from_file_location("build_metrics_dashboard", BUILDER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


B = _builder()


@pytest.mark.parametrize("sql, shown", [
    ("select * from users u where u.mobile = '0000000000'",
     "select * from users u where u.mobile = ?"),
    ("SELECT * FROM t WHERE id = 42 AND amount > 1.5e3 AND delta = -7",
     "SELECT * FROM t WHERE id = ? AND amount > ? AND delta = ?"),
    ("SELECT * FROM t WHERE id IN ('a', 'b', 3)",
     "SELECT * FROM t WHERE id IN (?, ?, ?)"),
    ("SELECT * FROM t WHERE a = $$x y$$ AND b = $q$z$q$",
     "SELECT * FROM t WHERE a = ? AND b = ?"),
    ("SELECT * FROM t WHERE ts > TIMESTAMP '2020-01-01' AND n = N'Ali'",
     "SELECT * FROM t WHERE ts > TIMESTAMP ? AND n = N?"),
], ids=["quoted", "numbers", "in-list", "dollar-quoted", "typed-and-prefixed"])
def test_every_literal_becomes_a_question_mark(sql, shown):
    assert B._hide_literals(sql) == shown


def test_names_keywords_and_placeholders_stay():
    sql = 'SELECT "Phone", t.email FROM t WHERE id = $1 AND k = %s'
    assert B._hide_literals(sql) == sql


def test_comments_are_dropped():
    out = B._hide_literals("-- call 0000000000\nSELECT 1 /* a@example.test */")
    assert "0000000000" not in out and "example" not in out
    assert out.split() == ["SELECT", "?"]


def test_everything_after_an_unmatched_quote_is_hidden():
    """The lexer reads the rest as plain words, which would all show."""
    assert (B._hide_literals("SELECT * FROM t WHERE email = 'ali@example.test")
            == "SELECT * FROM t WHERE email = ?")


def test_the_preview_is_cut_only_after_the_literals_are_hidden():
    """The view cuts the query at 200 characters. A literal cut in two lexes as
    a stray quote followed by plain words, so the row is rebuilt from the whole
    query, and the whole query does not travel on to the page."""
    sql = "SELECT * FROM t WHERE note = '" + "x" * 180 + " ali@example.test'"
    row = {"request_id": 7, "rating": 1, "query_preview": sql[:200], "query_full": sql}
    out = B._with_literals_hidden(row)
    assert out["query_preview"] == "SELECT * FROM t WHERE note = ?"
    assert "query_full" not in out
    assert out["request_id"] == 7 and out["rating"] == 1


def test_an_empty_query_is_an_empty_preview():
    assert B._with_literals_hidden({"query_full": None})["query_preview"] == ""
