"""An error message must not hand back data that the result file would have masked.

`SELECT full_name::int FROM customers WHERE id = 1` fails with the value of that
row in the message. The content detectors behind `errors.scrub()` catch an e-mail
or an IBAN. They cannot catch a name, an address, a phone number or a salary.
So `scrub()` also finds the phrasings in which an engine echoes a value, and
replaces the value with `[hidden]`.

What this file pins:

- every echo phrasing of PostgreSQL, SQL Server, ClickHouse and Athena that the
  code lists loses its value and keeps the words around it;
- a value the requester typed in the SQL stays visible, because it is not a data
  value and it is how they find the bad literal in a long IN list;
- the names of tables, columns and constraints stay readable;
- with PII masking off, or for a CSV import (the values are the requester's own),
  nothing is hidden;
- a failure inside the hiding step cannot break error reporting or skip the
  content detectors;
- every `errors.scrub()` call in the executor and the pre-flight says where the
  values came from, so a new call cannot forget it.
"""
from __future__ import annotations

import ast
import pathlib

import psycopg
import pytest

from queryhub import config as cfg
from queryhub import errors, executor

SRC = pathlib.Path(executor.__file__).parent


def _setting(monkeypatch, **values):
    monkeypatch.setattr(cfg, "get_setting", lambda k, d=None: values.get(k, d))


@pytest.fixture
def masking_on(monkeypatch):
    _setting(monkeypatch, pii_masking_enabled="on", pii_region="generic")


@pytest.fixture
def masking_off(monkeypatch):
    _setting(monkeypatch, pii_masking_enabled="off")


# --- every echo phrasing --------------------------------------------------------

# (engine, raw message as the driver gives it, the value that must not survive)
ECHOES = [
    ("pg cast, a name", 'ERROR:  invalid input syntax for type integer: "Jordan Ray"',
     "Jordan Ray"),
    ("pg cast, an address",
     'ERROR:  invalid input syntax for type uuid: "12 Example Street"', "Example Street"),
    ("pg cast, a phone", 'ERROR:  invalid input syntax for type bigint: "5550100123"',
     "5550100123"),
    ("pg cast, old wording", 'ERROR:  invalid input syntax for integer: "Jordan Ray"',
     "Jordan Ray"),
    ("pg enum", 'ERROR:  invalid input value for enum order_status: "refunded-late"',
     "refunded-late"),
    ("pg integer range", 'ERROR:  value "85000.50" is out of range for type smallint',
     "85000.50"),
    ("pg float range", 'ERROR:  "1e999" is out of range for type double precision',
     "1e999"),
    ("pg timestamp", 'ERROR:  date/time field value out of range: "2020-13-45"',
     "2020-13-45"),
    ("pg array", 'ERROR:  malformed array literal: "{Jordan Ray"', "Jordan Ray"),
    ("pg to_date", 'ERROR:  invalid value "Jo" for "YYYY"', '"Jo"'),
    ("pg time zone", 'ERROR:  time zone "Jordan Ray" not recognized', "Jordan Ray"),
    ("pg time zone name", 'ERROR:  unrecognized time zone name: "Jordan Ray"',
     "Jordan Ray"),
    ("pg hex digit", 'ERROR:  invalid hexadecimal digit: "q"', '"q"'),
    ("pg bytes", 'ERROR:  invalid byte sequence for encoding "UTF8": 0xe9 0x20', "0xe9"),
    ("psycopg timestamp",
     "DataError: timestamp too large (after year 10K): '12345-01-01 00:00:00'",
     "12345-01-01"),
    ("mssql convert",
     "[Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Conversion failed when "
     "converting the nvarchar value 'Jordan Ray' to data type int. (245) (SQLExecDirectW)",
     "Jordan Ray"),
    ("mssql duplicate key",
     "[Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Violation of UNIQUE KEY "
     "constraint 'UQ_c'. Cannot insert duplicate key in object 'dbo.c'. "
     "The duplicate key value is (Jordan Ray). (2627) (SQLExecDirectW)", "Jordan Ray"),
    ("mssql duplicate key, two columns",
     "Violation of PRIMARY KEY constraint 'PK_c'. Cannot insert duplicate key in object "
     "'dbo.c'. The duplicate key value is (Jordan Ray, 12 Example Street).",
     "Example Street"),
    ("mssql truncated",
     "String or binary data would be truncated in table 'db.dbo.t', column 'c'. "
     "Truncated value: 'Jordan R'.", "Jordan R"),
    ("mssql overflow",
     "The conversion of the varchar value '99999999999' overflowed an int column.",
     "99999999999"),
    ("clickhouse string",
     "Code: 6. DB::Exception: Cannot parse string 'Jordan Ray' as UInt32: syntax error "
     "at begin of string. Note: there are toUInt32OrZero and toUInt32OrNull functions",
     "Jordan Ray"),
    ("clickhouse input", "Cannot parse input: expected ',' before: 'Jordan Ray'",
     "Jordan Ray"),
    ("clickhouse convert", "Cannot convert string 'Jordan Ray' to type Int32",
     "Jordan Ray"),
    ("clickhouse enum", "Unknown element 'refunded-late' for enum", "refunded-late"),
    ("athena cast", "INVALID_CAST_ARGUMENT: Cannot cast 'Jordan Ray' to INTEGER",
     "Jordan Ray"),
    ("athena typed cast", "Cannot cast VARCHAR 'Jordan Ray' to BIGINT", "Jordan Ray"),
    ("athena date", "INVALID_CAST_ARGUMENT: Value cannot be cast to date: 2020-13-45",
     "2020-13-45"),
]


@pytest.mark.parametrize("raw,secret", [(r, s) for _, r, s in ECHOES],
                         ids=[e for e, _, _ in ECHOES])
def test_an_echoed_value_is_hidden(masking_on, raw, secret):
    out = errors.scrub(raw)
    assert secret not in out
    assert "[hidden]" in out


def test_the_words_around_the_value_stay(masking_on):
    out = errors.scrub('ERROR:  invalid input syntax for type integer: "Jordan Ray"')
    assert out == "Invalid input syntax for type integer: [hidden]"
    out = errors.scrub('ERROR:  value "85000.50" is out of range for type smallint')
    assert out == "Value [hidden] is out of range for type smallint"
    out = errors.scrub("The duplicate key value is (Jordan Ray).")
    assert out == "The duplicate key value is [hidden]."


def test_the_rest_of_a_sql_server_message_survives(masking_on):
    out = errors.scrub(
        "[Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Violation of UNIQUE KEY "
        "constraint 'UQ_c'. Cannot insert duplicate key in object 'dbo.c'. "
        "The duplicate key value is (Jordan Ray). (2627) (SQLExecDirectW)")
    assert "UQ_c" in out and "dbo.c" in out and "Jordan Ray" not in out


# --- a message that was cut short -----------------------------------------------

LONG_TEXT = "Jordan Ray " * 400       # 4,400 characters, more than SQL Server keeps


@pytest.mark.parametrize("raw", [
    # SQL Server cuts a message at 2047 characters, so a long text cell loses
    # its closing quote and the words that follow it.
    "Conversion failed when converting the nvarchar value '" + LONG_TEXT[:1900],
    "The conversion of the varchar value '" + LONG_TEXT,
    "The duplicate key value is (" + LONG_TEXT,
    "Truncated value: '" + LONG_TEXT,
    'ERROR:  time zone "' + LONG_TEXT,
    'ERROR:  value "' + LONG_TEXT,
    'ERROR:  invalid value "' + LONG_TEXT,
    "Cannot parse string '" + LONG_TEXT,
    "Cannot cast '" + LONG_TEXT,
], ids=["mssql convert", "mssql overflow", "mssql duplicate key", "mssql truncated",
        "pg time zone", "pg out of range", "pg to_date", "clickhouse", "athena"])
def test_a_message_cut_before_its_closing_quote_still_hides_the_value(masking_on, raw):
    out = errors.scrub(raw)
    assert "Jordan" not in out
    assert "[hidden]" in out


def test_a_message_the_limit_cut_still_hides_the_value(masking_on):
    # The text is longer than the work limit, so the closing quote is cut off.
    out = errors.scrub('ERROR:  time zone "' + "Jordan Ray " * 5000 + '" not recognized')
    assert out == "Time zone [hidden]"


# --- bounded cost ----------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    # 200 KB of one character took 38 seconds in the content detectors.
    "Conversion failed when converting the nvarchar value '" + "x" * 100_000,
    # A prefix repeated 8,000 times: each match used to scan to the end.
    "Cannot cast '" * 8_000,
    "Conversion failed when converting the nvarchar value '" * 2_000,
    'time zone "' * 9_000,
    'value ' + '" ' * 50_000,
], ids=["one long run", "repeated cannot cast", "repeated convert", "repeated time zone",
        "quote storm"])
def test_a_huge_message_is_scrubbed_in_bounded_time(masking_on, raw):
    import time
    started = time.perf_counter()
    out = errors.scrub(raw)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"scrub took {elapsed:.1f}s on {len(raw)} characters"
    assert len(out) <= 500


# --- the requester's own text ---------------------------------------------------

def test_a_value_the_requester_typed_stays_visible(masking_on):
    out = errors.scrub('ERROR:  invalid input syntax for type integer: "abc"',
                       query="SELECT * FROM t WHERE id = 'abc'")
    assert out.endswith('"abc"')


def test_the_bad_literal_in_a_long_in_list_is_found(masking_on):
    query = "SELECT * FROM t WHERE id IN ('1', '2', 'x3', '4')"
    out = errors.scrub('ERROR:  invalid input syntax for type integer: "x3"', query=query)
    assert '"x3"' in out


def test_a_value_that_is_not_in_the_query_is_hidden_even_with_other_literals(masking_on):
    out = errors.scrub('ERROR:  invalid input syntax for type integer: "Jordan Ray"',
                       query="SELECT full_name::int FROM customers WHERE id = 123")
    assert "Jordan Ray" not in out and "[hidden]" in out


def test_whitespace_does_not_hide_a_typed_value(masking_on):
    # The message is already collapsed to single spaces. The query is not.
    out = errors.scrub('ERROR:  invalid input syntax for type uuid: "not a uuid"',
                       query="SELECT *\n  FROM t WHERE id = 'not   a\tuuid'")
    assert '"not a uuid"' in out


def test_an_empty_value_is_left_alone(masking_on):
    assert errors.scrub('ERROR:  invalid input syntax for type integer: ""').endswith('""')


# --- what must not change -------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ('ERROR:  relation "orders" does not exist', 'Relation "orders" does not exist'),
    ('ERROR:  column "full_name" of relation "customers" does not exist',
     'Column "full_name" of relation "customers" does not exist'),
    ('ERROR:  syntax error at or near "FORM"', 'Syntax error at or near "FORM"'),
    ('ERROR:  duplicate key value violates unique constraint "customers_email_key"',
     'Duplicate key value violates unique constraint "customers_email_key"'),
    ('ERROR:  canceling statement due to statement timeout',
     'Canceling statement due to statement timeout'),
    ('ERROR:  null value in column "email" of relation "customers" violates not-null '
     'constraint',
     'Null value in column "email" of relation "customers" violates not-null constraint'),
    ("Invalid column name 'full_name'.", "Invalid column name 'full_name'."),
], ids=["relation", "column", "syntax", "constraint", "timeout", "not-null", "mssql name"])
def test_names_of_objects_stay_readable(masking_on, raw, expected):
    assert errors.scrub(raw) == expected


def test_nothing_is_hidden_when_masking_is_off(masking_off):
    out = errors.scrub('ERROR:  invalid input syntax for type integer: "Jordan Ray"')
    assert out.endswith('"Jordan Ray"')


def test_nothing_is_hidden_for_the_requesters_own_upload(masking_on):
    out = errors.scrub('ERROR:  invalid input syntax for type integer: "Jordan Ray"',
                       requester_data=True)
    assert out.endswith('"Jordan Ray"')


def test_content_detectors_still_run_after_the_hiding_step(masking_on):
    # An e-mail in a message that no echo phrasing matches is still masked.
    out = errors.scrub('ERROR:  value too long for type character varying(8): '
                       '"jane.doe@example.com"')
    assert "jane.doe@example.com" not in out


def test_a_failing_hiding_step_cannot_break_reporting_or_skip_the_detectors(
        masking_on, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("regex engine fell over")
    monkeypatch.setattr(errors, "_hide_data_values", boom)
    out = errors.scrub('ERROR:  value too long for type character varying(8): '
                       '"jane.doe@example.com"')
    assert "jane.doe@example.com" not in out          # the detectors still ran
    assert errors.scrub('ERROR:  relation "x" does not exist').endswith("does not exist")


def test_the_message_is_still_capped_and_capitalised(masking_on):
    out = errors.scrub('ERROR:  invalid input syntax for type integer: "' + "x" * 900 + '"')
    assert out[0].isupper() and len(out) <= 500


# --- the executor and the pre-flight use it --------------------------------------

class _Route:
    alias = "replica-1"
    target_id = 7


def _chosen_replica_message(monkeypatch, request):
    seen = []
    monkeypatch.setattr(executor.replicas, "mark_unhealthy", lambda *_a, **_k: None)
    monkeypatch.setattr(executor, "_fail", lambda _client, _req, msg: seen.append(msg))
    err = psycopg.errors.InvalidTextRepresentation(
        'invalid input syntax for type integer: "Jordan Ray"')
    executor._fail_on_chosen_replica(None, request, _Route(), err, [])
    return seen[0]


def test_the_executor_hides_a_value_the_requester_did_not_write(masking_on, monkeypatch):
    msg = _chosen_replica_message(
        monkeypatch, {"id": 1, "query": "SELECT full_name::int FROM customers"})
    assert "Jordan Ray" not in msg and "[hidden]" in msg


def test_the_executor_keeps_a_value_the_requester_wrote(masking_on, monkeypatch):
    msg = _chosen_replica_message(
        monkeypatch, {"id": 1, "query": "SELECT 1 WHERE x = 'Jordan Ray'::int"})
    assert "Jordan Ray" in msg


def _scrub_calls(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "scrub"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "errors"):
            yield node


@pytest.mark.parametrize("name", ["executor.py", "pre_flight.py"])
def test_every_scrub_call_says_where_the_values_came_from(name):
    """A call without `query=` hides every echoed value, even one the requester
    typed. A call that forgets `requester_data=True` hides the values of a CSV
    import. Name the source at each call, so a new call must decide."""
    calls = list(_scrub_calls(SRC / name))
    assert calls, f"no errors.scrub call found in {name}: did the code move?"
    missing = [c.lineno for c in calls
               if not {k.arg for k in c.keywords} & {"query", "requester_data"}]
    assert not missing, (
        f"{name}: errors.scrub at line(s) {missing} needs query=<the SQL the requester "
        f"wrote> or requester_data=True")
