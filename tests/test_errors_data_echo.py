"""An error message must not hand back data that the result file would have masked.

`SELECT full_name::int FROM customers WHERE id = 1` fails with the value of that
row in the message. The content detectors behind `errors.scrub()` catch an e-mail
or an IBAN. They cannot catch a name, an address, a phone number or a salary.
So `scrub()` also finds the phrasings in which an engine echoes a value, and
replaces the value with `[hidden]`.

What this file pins:

- Every echo phrasing of PostgreSQL, SQL Server, ClickHouse and Athena that the
  code lists loses its value and keeps the words around it.
- A value the requester typed in the SQL stays visible, because it is not a data
  value and it is how they find the bad literal in a long IN list.
- The names of tables, columns and constraints stay readable, unless the SQL
  does not contain the name. Then a cast to regclass put a value there.
- A pyodbc error arrives as the repr of its tuple, and the code scrubs the
  message inside it.
- With PII masking off, or for a CSV import (the values are the requester's own),
  nothing is hidden.
- A failure inside the hiding step cannot break error reporting or skip the
  content detectors.
- Every `errors.scrub()` call in the executor and the pre-flight says where the
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
    # Measured on PostgreSQL 17 with a sentinel value in every column type. The
    # SQL that produced each one is in the comment where it is not obvious.
    ("pg range literal", 'ERROR:  malformed range literal: "Jordan Ray"', "Jordan Ray"),
    ("pg multirange literal", 'ERROR:  malformed multirange literal: "Jordan Ray"',
     "Jordan Ray"),
    ("pg record literal", 'ERROR:  malformed record literal: "Jordan Ray"', "Jordan Ray"),
    ("pg interval range",
     'ERROR:  interval field value out of range: "99999999999999999 years"',
     "99999999999999999"),
    ("pg time zone displacement",
     'ERROR:  time zone displacement out of range: "2047-07-07 07:07:07+99"', "2047-07-07"),
    ("pg timestamp range", 'ERROR:  timestamp out of range: "9.87654e+19"', "9.87654e+19"),
    ("pg make_date", 'ERROR:  date field value out of range: 2020-1234567-01', "1234567"),
    ("pg make_time", 'ERROR:  time field value out of range: 1234567:00:00', "1234567"),
    ("pg make_timestamp", 'ERROR:  timestamp out of range: 1234567-01-01 0:00:00',
     "1234567"),
    ("pg cidr", 'ERROR:  invalid cidr value: "203.0.113.77/24"', "203.0.113.77"),
    ("pg chr", 'ERROR:  requested character too large for encoding: 1234567', "1234567"),
    ("pg chr surrogate", 'ERROR:  requested character not valid for encoding: 55297',
     "55297"),
    ("pg get_byte", 'ERROR:  index 1234567 out of valid range, 0..22', "1234567"),
    ("pg lo_get", 'ERROR:  large object 1234567 does not exist', "1234567"),
    ("pg oid", 'ERROR:  collation with OID 1234567 does not exist', "1234567"),
    ("pg xid", 'ERROR:  transaction ID 1234567 is in the future', "1234567"),
    ("pg date_trunc",
     'ERROR:  unit "jordan ray" not recognized for type timestamp with time zone',
     "jordan ray"),
    ("pg set_config", 'ERROR:  invalid value for parameter "work_mem": "Jordan Ray"',
     "Jordan Ray"),
    ("pg encode", 'ERROR:  unrecognized encoding: "Jordan Ray"', "Jordan Ray"),
    ("pg convert_from", 'ERROR:  invalid source encoding name "Jordan Ray"', "Jordan Ray"),
    ("pg convert_to", 'ERROR:  invalid destination encoding name "Jordan Ray"',
     "Jordan Ray"),
    ("pg to_ascii", 'ERROR:  Jordan Ray is not a valid encoding name', "Jordan Ray"),
    ("pg ts_headline", 'ERROR:  invalid parameter list format: "Jordan Ray"', "Jordan Ray"),
    ("pg headline parameter", 'ERROR:  unrecognized headline parameter: "Jordan Ray"',
     "Jordan Ray"),
    ("pg tsquery", 'ERROR:  syntax error in tsquery: "Jordan Ray"', "Jordan Ray"),
    ("pg jsonpath method",
     'ERROR:  argument "Jordan Ray" of jsonpath item method .integer() is invalid for '
     'type integer', "Jordan Ray"),
    ("pg jsonpath date", 'ERROR:  date format is not recognized: "Jordan Ray"',
     "Jordan Ray"),
    ("pg jsonpath datetime", 'ERROR:  datetime format is not recognized: "Jordan Ray"',
     "Jordan Ray"),
    ("pg privilege type", 'ERROR:  unrecognized privilege type: "Jordan Ray"',
     "Jordan Ray"),
    ("pg object type", 'ERROR:  unrecognized object type "Jordan Ray"', "Jordan Ray"),
    ("pg pg_size_bytes", 'ERROR:  invalid size: "10 Jordan Ray"', "Jordan Ray"),
    ("pg log format", 'ERROR:  log format "Jordan Ray" is not supported', "Jordan Ray"),
    ("pg parse_ident", 'ERROR:  string is not a valid identifier: "Jordan Ray"',
     "Jordan Ray"),
    ("pg parameter name", 'ERROR:  invalid configuration parameter name "jordan.ray"',
     "jordan.ray"),
    ("pg pgcrypto", 'ERROR:  Cannot use "jordan ray": No such hash algorithm',
     "jordan ray"),
    ("pg aclitem", 'ERROR:  unrecognized key word: "Jordan"', "Jordan"),
    ("pg binary digit", 'ERROR:  "J" is not a valid binary digit', '"J"'),
    ("pg weight", 'ERROR:  unrecognized weight: "J"', '"J"'),
    ("pg regex option", 'ERROR:  invalid regular expression option: "J"', '"J"'),
    ("pg format specifier", 'ERROR:  unrecognized format() type specifier "J"', '"J"'),
    ("pg base64", 'ERROR:  invalid symbol "-" found while decoding base64 sequence',
     '"-"'),
    ("pg hstore", 'ERROR:  syntax error in hstore, near "J" at position 4', '"J"'),
    ("pg tablespace path",
     'ERROR:  could not stat file "pg_tblspc/1234567": No such file or directory',
     "1234567"),
    ("pg no equivalent",
     'ERROR:  character with byte sequence 0xe2 0x82 0xac in encoding "UTF8" has no '
     'equivalent in encoding "LATIN1"', "0xe2"),
    ("psycopg time", "DataError: can't parse time '24:00:00': hour must be in 0..23",
     "24:00"),
    ("psycopg timetz",
     "DataError: can't parse timetz '24:00:00+00': hour must be in 0..23", "24:00"),
    ("psycopg interval",
     "DataError: can't parse interval '170000000 years': days=1920457856; must have "
     "magnitude <= 999999999", "170000000"),
    ("psycopg date BC", "DataError: date too small (before year 1): '0144-03-10 BC'",
     "0144-03-10"),
    # Measured on SQL Server 2022. The executor passes a pyodbc error as
    # `<class>: <repr of the (sqlstate, message) tuple>`, which is what these are.
    ("mssql tuple",
     "DataError: ('22018', \"[22018] [Microsoft][ODBC Driver 18 for SQL Server]"
     "[SQL Server]Conversion failed when converting the nvarchar value 'Jordan Ray' "
     "to data type int. (245) (SQLExecDirectW)\")", "Jordan Ray"),
    ("mssql quote in the value",
     "DataError: ('22018', '[22018] [Microsoft][ODBC Driver 18 for SQL Server]"
     "[SQL Server]Conversion failed when converting the nvarchar value "
     "\\'Jordan \"Ray\"\\' to data type int. (245) (SQLExecDirectW)')", "Jordan"),
    ("mssql int overflow",
     "DataError: ('22003', '[22003] [Microsoft][ODBC Driver 18 for SQL Server]"
     "[SQL Server]Arithmetic overflow error for data type tinyint, value = 1234567. "
     "(220) (SQLExecDirectW)')", "1234567"),
    ("mssql style",
     "1234567 is not a valid style number when converting from datetime to a "
     "character string.", "1234567"),
    ("mssql style varbinary",
     "The style 1234567 is not supported for conversions from varbinary to varchar.",
     "1234567"),
    ("mssql parse",
     "Error converting string value 'Jordan Ray' into data type int using culture ''.",
     "Jordan Ray"),
    ("mssql culture",
     "The culture parameter 'Jordan Ray' provided in the function call is not supported.",
     "Jordan Ray"),
    ("mssql time zone",
     "The time zone parameter 'Jordan Ray' provided to AT TIME ZONE clause is invalid.",
     "Jordan Ray"),
    ("mssql hierarchyid",
     "24001: SqlHierarchyId.Parse failed because the input string 'Jordan Ray' is not "
     "a valid string representation of a SqlHierarchyId node.", "Jordan Ray"),
    ("mssql geography",
     "24114: The label Jordan Ray in the input well-known text (WKT) is not valid. "
     "Valid labels are POINT, LINESTRING.", "Jordan Ray"),
    ("mssql like escape",
     'The invalid escape character "Jordan Ray" was specified in a LIKE predicate.',
     "Jordan Ray"),
    ("mssql json",
     "JSON text is not properly formatted. Unexpected character 'J' is found at "
     "position 0.", "'J'"),
    ("mssql unique index",
     "Cannot insert duplicate key row in object 'dbo.c' with unique index 'IX_c'. "
     "The duplicate key value is (Jordan Ray, 12 Example Street).", "Example Street"),
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
    out = errors.scrub('ERROR:  unit "jordan ray" not recognized for type interval')
    assert out == "Unit [hidden] not recognized for type interval"
    out = errors.scrub('ERROR:  invalid value for parameter "work_mem": "Jordan Ray"')
    assert out == 'Invalid value for parameter "work_mem": [hidden]'
    out = errors.scrub("Arithmetic overflow error for data type tinyint, value = 1234567.")
    assert out == "Arithmetic overflow error for data type tinyint, value = [hidden]."
    out = errors.scrub("can't parse time '24:00:00': hour must be in 0..23")
    assert out == "Can't parse time [hidden]"


def test_the_rest_of_a_sql_server_message_survives(masking_on):
    out = errors.scrub(
        "[Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Violation of UNIQUE KEY "
        "constraint 'UQ_c'. Cannot insert duplicate key in object 'dbo.c'. "
        "The duplicate key value is (Jordan Ray). (2627) (SQLExecDirectW)")
    assert "UQ_c" in out and "dbo.c" in out and "Jordan Ray" not in out


def test_a_value_with_a_quote_inside_is_hidden_whole(masking_on):
    # The closing quote is the one the words follow, not the first one.
    out = errors.scrub('ERROR:  value "Jordan "Ray"" is out of range for type smallint')
    assert out == "Value [hidden] is out of range for type smallint"
    out = errors.scrub('ERROR:  unit "jordan "ray"" not recognized for type interval')
    assert out == "Unit [hidden] not recognized for type interval"
    out = errors.scrub('ERROR:  "1e999"" is out of range for type real')
    assert out == "[hidden] is out of range for type real"


# --- the pyodbc envelope ---------------------------------------------------------

_TUPLE = ("DataError: ('22018', '[22018] [Microsoft][ODBC Driver 18 for SQL Server]"
          "[SQL Server]Conversion failed when converting the nvarchar value "
          "\\'Jordan \"Ray\"\\' to data type int. (245) (SQLExecDirectW)')")


def test_the_message_is_taken_out_of_the_pyodbc_tuple(masking_on):
    """pyodbc gives `('42000', "message")`, and the executor passes its repr. The
    repr escapes a quote inside the message as \\', which hid the quotes the
    pattern looks for. Measured: a value with a double quote in it leaked."""
    assert errors.scrub(_TUPLE) == (
        "Conversion failed when converting the nvarchar value [hidden] to data type int.")
    out = errors.scrub(
        "ProgrammingError: ('42S02', \"[42S02] [Microsoft][ODBC Driver 18 for SQL Server]"
        "[SQL Server]Invalid object name 'dbo.Orderz'. (208) (SQLExecDirectW)\")",
        query="SELECT * FROM dbo.Orderz")
    assert out == "Invalid object name 'dbo.Orderz'."


def test_a_dot_net_error_keeps_only_its_first_line(masking_on):
    # hierarchyid and geography parse in .NET and put the value on the second
    # line. The first line alone reaches the requester.
    out = errors.scrub(
        "ProgrammingError: ('42000', '[42000] [Microsoft][ODBC Driver 18 for SQL Server]"
        "[SQL Server]A .NET Framework error occurred during execution of user-defined "
        "routine or aggregate \"hierarchyid\": \\r\\nMicrosoft.SqlServer.Types."
        "HierarchyIdException: 24001: SqlHierarchyId.Parse failed because the input "
        "string \\'Jordan Ray\\' is not a valid string representation of a SqlHierarchyId "
        "node.\\r\\n. (6522) (SQLExecDirectW)')")
    assert "Jordan" not in out
    assert out.startswith("A .NET Framework error occurred")


@pytest.mark.parametrize("raw", [
    "ProgrammingError: (" + "'" * 100_000 + ")",
    "ProgrammingError: ('42000', " + "(" * 50_000 + ")" * 50_000 + ")",
    "ProgrammingError: ('42000', 1)",
    "('42000',)",
], ids=["quotes", "nested", "not strings", "one element"])
def test_a_text_that_only_looks_like_a_tuple_is_left_alone(masking_on, raw):
    out = errors.scrub(raw)
    assert out.startswith(raw[:20]) and len(out) <= 500


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
    'ERROR:  unit "' + LONG_TEXT,
    'ERROR:  argument "' + LONG_TEXT,
    'ERROR:  Cannot use "' + LONG_TEXT,
    "Error converting string value '" + LONG_TEXT,
    "The culture parameter '" + LONG_TEXT,
    "the input string '" + LONG_TEXT,
    "The label " + LONG_TEXT,
    'The invalid escape character "' + LONG_TEXT,
], ids=["mssql convert", "mssql overflow", "mssql duplicate key", "mssql truncated",
        "pg time zone", "pg out of range", "pg to_date", "clickhouse", "athena",
        "pg unit", "pg jsonpath", "pg pgcrypto", "mssql parse", "mssql culture",
        "mssql hierarchyid", "mssql geography", "mssql escape"])
def test_a_message_cut_before_its_closing_quote_still_hides_the_value(masking_on, raw):
    out = errors.scrub(raw)
    assert "Jordan" not in out
    assert "[hidden]" in out


def test_a_name_message_cut_before_its_closing_quote_still_hides_the_name(masking_on):
    out = errors.scrub('ERROR:  relation "' + LONG_TEXT, query="SELECT 1")
    assert out == "Relation [hidden]"


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
    'unit "' * 9_000,
    "The label " * 8_000,
    'relation "' * 9_000,
    'syntax error at or near "' * 4_000,
    "index " + " x" * 50_000 + " is not a valid style number",
    "ProgrammingError: ('42000', \"[42000] Conversion failed when converting the "
    "nvarchar value '" + "x" * 100_000 + "' to data type int. (245) (SQLExecDirectW)\")",
], ids=["one long run", "repeated cannot cast", "repeated convert", "repeated time zone",
        "quote storm", "repeated unit", "repeated label", "repeated relation",
        "repeated syntax error", "space storm", "tuple"])
def test_a_huge_message_is_scrubbed_in_bounded_time(masking_on, raw):
    import time
    started = time.perf_counter()
    out = errors.scrub(raw, query="SELECT 1")
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


# --- a name that is not in the SQL ----------------------------------------------

# (message, the SQL that produced it, the value that must not survive)
NAMES_FROM_DATA = [
    ("regclass", 'ERROR:  relation "jordan ray" does not exist',
     "SELECT full_name::regclass FROM customers", "jordan ray"),
    ("regtype", 'ERROR:  type "jordan ray" does not exist',
     "SELECT full_name::regtype FROM customers", "jordan ray"),
    ("regproc", 'ERROR:  function "Jordan Ray" does not exist',
     "SELECT full_name::regproc FROM customers", "Jordan Ray"),
    ("regrole", 'ERROR:  role "jordan ray" does not exist',
     "SELECT full_name::regrole FROM customers", "jordan ray"),
    ("regoper", 'ERROR:  operator does not exist: jordan-ray',
     "SELECT full_name::regoper FROM customers", "jordan-ray"),
    ("regcollation", 'ERROR:  collation "jordan ray" for encoding "UTF8" does not exist',
     "SELECT full_name::regcollation FROM customers", "jordan ray"),
    ("current_setting", 'ERROR:  unrecognized configuration parameter "Jordan Ray"',
     "SELECT current_setting(full_name) FROM customers", "Jordan Ray"),
    ("query_to_xml", 'ERROR:  syntax error at or near "Jordan"',
     "SELECT query_to_xml(full_name, false, false, '') FROM customers", "Jordan"),
    ("has_column_privilege",
     'ERROR:  column "Jordan Ray" of relation "pg_class" does not exist',
     "SELECT has_column_privilege('pg_class', full_name, 'select') FROM customers",
     "Jordan Ray"),
    ("a quote in the name", 'ERROR:  relation "jordan"ray" does not exist',
     "SELECT full_name::regclass FROM customers", "ray"),
    ("mssql object", "Invalid object name 'Jordan Ray'.", "SELECT 1", "Jordan Ray"),
    ("mssql column", "Invalid column name 'Jordan'.", "SELECT 1", "Jordan"),
    ("mssql syntax", "Incorrect syntax near 'Jordan'.", "SELECT 1", "Jordan"),
    ("mssql database", "Database 'Jordan' does not exist. Make sure that the name is "
     "entered correctly.", "SELECT 1", "Jordan"),
]


@pytest.mark.parametrize("raw,query,secret", [(r, q, s) for _, r, q, s in NAMES_FROM_DATA],
                         ids=[e for e, _, _, _ in NAMES_FROM_DATA])
def test_a_name_that_is_not_in_the_sql_is_hidden(masking_on, raw, query, secret):
    out = errors.scrub(raw, query=query)
    assert secret not in out
    assert "[hidden]" in out


def test_the_words_around_a_hidden_name_stay(masking_on):
    out = errors.scrub('ERROR:  column "Jordan Ray" of relation "pg_class" does not exist',
                       query="SELECT has_column_privilege('pg_class', full_name, 'select') "
                             "FROM customers")
    assert out == 'Column [hidden] of relation "pg_class" does not exist'
    out = errors.scrub('ERROR:  operator does not exist: jordan-ray',
                       query="SELECT full_name::regoper FROM customers")
    assert out == "Operator does not exist: [hidden]"


# (message, the SQL with the typo in it)
TYPOS = [
    ("relation", 'ERROR:  relation "orderz" does not exist', "SELECT * FROM Orderz"),
    ("relation, quoted", 'ERROR:  relation "public.Orderz" does not exist',
     'SELECT * FROM "public"."Orderz"'),
    ("column", 'ERROR:  column "namez" does not exist', "SELECT NAMEZ FROM customers"),
    ("column of relation", 'ERROR:  column "namez" of relation "customers" does not exist',
     "INSERT INTO customers (namez) VALUES (1)"),
    ("syntax", 'ERROR:  syntax error at or near "FORM"', "select * form customers"),
    ("syntax at character", 'ERROR:  syntax error at or near "wher" at character 20',
     "SELECT * FROM customers wher id = 1"),
    ("type", 'ERROR:  type "intt" does not exist', "SELECT 1::intt"),
    ("setting", 'ERROR:  unrecognized configuration parameter "work_memz"',
     "SET work_memz = '1MB'"),
    ("role", 'ERROR:  role "nosuch_role" does not exist', "SET ROLE nosuch_role"),
    ("database", 'ERROR:  database "nosuchdb" does not exist',
     "SELECT has_database_privilege('nosuchdb', 'connect')"),
    ("collation", 'ERROR:  collation "nosuch" for encoding "UTF8" does not exist',
     "SELECT 'x' COLLATE \"nosuch\""),
    ("sequence", 'ERROR:  relation "nosuch_seq" does not exist',
     "SELECT nextval('nosuch_seq')"),
    ("mssql object", "Invalid object name 'dbo.Orderz'.", "SELECT * FROM [dbo].[Orderz]"),
    ("mssql column", "Invalid column name 'namez'.", "SELECT Namez FROM dbo.customers"),
    ("mssql syntax", "Incorrect syntax near 'FORM'.", "select * form dbo.customers"),
    ("mssql keyword", "Incorrect syntax near the keyword 'from'.", "SELECT 1"),
    ("operator types", "ERROR:  operator does not exist: text = integer",
     "SELECT 1 FROM customers WHERE full_name = 1"),
    ("function with argument types", "ERROR:  function nowz() does not exist",
     "SELECT 1"),
]


@pytest.mark.parametrize("raw,query", [(r, q) for _, r, q in TYPOS],
                         ids=[e for e, _, _ in TYPOS])
def test_a_name_the_requester_typed_stays_visible(masking_on, raw, query):
    out = errors.scrub(raw, query=query)
    assert "[hidden]" not in out
    assert errors.scrub(raw, query=query) == errors.scrub(raw)


def test_a_connection_failure_keeps_the_database_name(masking_on):
    # The name is the database of the request, not a value from a row.
    out = errors.scrub(
        'connection failed: connection to server at "db.example.internal" (192.0.2.10), '
        'port 5432 failed: FATAL: database "ledger" does not exist', query="SELECT 1")
    assert '"ledger"' in out


def test_a_name_is_left_alone_when_no_sql_is_given(masking_on):
    # Connection tests and catalog reads pass no SQL. There is nothing to compare
    # with, and no requester wrote the statement that failed.
    assert errors.scrub('ERROR:  relation "jordan ray" does not exist') == (
        'Relation "jordan ray" does not exist')


def test_a_name_from_a_function_body_is_hidden_too(masking_on):
    # The function the query calls reads a table that is gone. Its name is not
    # in the SQL, so the rule hides it. Measured on 265 stored errors: none of
    # the 58 messages of these shapes named something outside the SQL.
    out = errors.scrub('ERROR:  relation "ledger.gone" does not exist',
                       query="SELECT ledger.reads_gone()")
    assert out == "Relation [hidden] does not exist"


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
