"""Labels for the result switcher: every statement, on every response.

The menu that names the results is open while ONE of them is on screen, so the
array has to describe all N. Describing only the one being returned would cost
a request per row, and a nine-statement script is exactly the case the menu
exists for.

`n` is the statement's position in the script, carried on the row rather than
implied by the array. The (c) round found the same trap in `bundlePosition`:
"item 3 of 5" means nothing if 3 is a list index.
"""
from queryhub import executor as ex
from queryhub.web import routes_queries as rq


def test_labels_come_from_what_was_executed():
    row = {"run_notes": {"statements": [
        {"i": 1, "leading": "ALTER", "snippet": "alter table orders add column x int"},
        {"i": 2, "leading": "UPDATE", "snippet": "update orders set x = 0"},
    ]}}
    assert rq._statement_labels(row, 2) == [
        {"n": 1, "kind": "ALTER", "snippet": "alter table orders add column x int"},
        {"n": 2, "kind": "UPDATE", "snippet": "update orders set x = 0"},
    ]


def test_notes_stored_as_text_are_parsed():
    import json
    row = {"run_notes": json.dumps(
        {"statements": [{"i": 1, "leading": "SELECT", "snippet": "select 1"}]})}
    assert rq._statement_labels(row, 1)[0]["kind"] == "SELECT"


def test_a_run_from_before_the_notes_column_is_re_split():
    # No run_notes: the SQL is the only record left, and a label from it is
    # better than a row that says nothing.
    row = {"query": "SELECT 1;\nUPDATE t SET a = 1;"}
    out = rq._statement_labels(row, 2)
    assert [x["n"] for x in out] == [1, 2]
    assert [x["kind"] for x in out] == ["SELECT", "UPDATE"]
    assert out[1]["snippet"].startswith("UPDATE t SET")


def test_unparseable_sql_still_answers_with_the_right_number_of_rows():
    row = {"query": None}
    assert rq._statement_labels(row, 3) == [
        {"n": 1, "kind": None, "snippet": ""},
        {"n": 2, "kind": None, "snippet": ""},
        {"n": 3, "kind": None, "snippet": ""},
    ]


# --- the snippet itself -----------------------------------------------------

def test_a_snippet_is_one_line():
    assert ex._sql_snippet("select id,\n  name\nfrom users") == "select id, name from users"


def test_a_banner_comment_does_not_become_the_label():
    # Generated scripts open every statement with the same banner. Labelling
    # by it would give nine rows the same label, which is what the numbers
    # already do.
    sql = ("-- ===========================\n"
           "-- Step 1: Create roles\n"
           "-- ===========================\n"
           "GRANT CONNECT ON DATABASE app TO reader;")
    assert ex._sql_snippet(sql).startswith("GRANT CONNECT ON DATABASE app")


def test_a_string_literal_survives():
    # code_text() blanks literals, which is right for a keyword scan and wrong
    # for a label: it turned this into "SELECT 2 AS two, AS label".
    assert ex._sql_snippet("SELECT 2 AS two, 'x' AS label") == "SELECT 2 AS two, 'x' AS label"


def test_the_snippet_carries_no_whitespace_the_label_cannot_render():
    """A contract the switcher's menu depends on (design brief 2026-08-24 (b)).

    The row is nowrap and clipped, so an embedded newline would open a wide
    gap in the middle of the label. Newlines, CRLF, tabs, blank lines and runs
    of spaces all collapse to a single space here, which is why the client
    does not have to do it again.
    """
    out = ex._sql_snippet("SELECT id,\r\n\tcreated_at\n\n  FROM orders")
    assert out == "SELECT id, created_at FROM orders"
    assert "\n" not in out and "\r" not in out and "\t" not in out
    assert "  " not in out


def test_a_long_statement_is_clamped():
    assert len(ex._sql_snippet("select " + "a, " * 200)) == ex._SNIPPET_CHARS


def test_a_comment_only_statement_falls_back_to_its_raw_text():
    # code_text() blanks comments, so a statement that is nothing else would
    # otherwise be labelled with an empty string.
    assert ex._sql_snippet("-- just a note").startswith("-- just a note")


def test_the_snippet_reaches_the_run_record():
    r = ex._StmtResult(index=1, leading="SELECT", snippet="select 1")
    assert ex._run_notes([r])["statements"][0]["snippet"] == "select 1"
