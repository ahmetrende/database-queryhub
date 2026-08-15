"""csv_import — header normalization, CSV parsing, user-supplied schema parse."""
from queryhub import csv_import as ci


# --- normalize_column -------------------------------------------------------

def test_normalize_basic():
    seen = set()
    assert ci.normalize_column("User Name", 0, seen) == "user_name"


def test_normalize_strips_non_ascii():
    seen = set()
    # Non-identifier chars collapse to a single underscore, trimmed at edges.
    assert ci.normalize_column("Σ total", 0, seen) == "total"


def test_normalize_empty_uses_index():
    seen = set()
    assert ci.normalize_column("", 3, seen) == "col_4"


def test_normalize_leading_digit_prefixed():
    seen = set()
    assert ci.normalize_column("2024_total", 0, seen) == "c_2024_total"


def test_normalize_collision_suffixed():
    seen = set()
    a = ci.normalize_column("name", 0, seen)
    b = ci.normalize_column("name", 1, seen)
    assert a == "name" and b == "name_1"


# --- parse_csv --------------------------------------------------------------

def parse(text, delim=","):
    return ci.parse_csv(text.encode("utf-8"), delim)


def test_parse_happy_path():
    p = parse("id,name\n1,alice\n2,bob\n")
    assert p.error is None
    assert p.columns == ["id", "name"]
    assert p.row_count == 2
    assert p.sample_rows[0] == ["1", "alice"]


def test_parse_normalizes_header():
    p = parse("User ID,Full Name\n1,x\n")
    assert p.columns == ["user_id", "full_name"]
    assert p.raw_header == ["User ID", "Full Name"]


def test_parse_semicolon_delimiter():
    p = ci.parse_csv("a;b\n1;2\n".encode("utf-8"), ";")
    assert p.error is None
    assert p.columns == ["a", "b"]


def test_parse_bom_tolerated():
    p = ci.parse_csv("﻿id,name\n1,x\n".encode("utf-8"), ",")
    assert p.error is None
    assert p.columns == ["id", "name"]


def test_parse_rejects_non_utf8():
    p = ci.parse_csv("id,name\n1,\xff\xfe".encode("latin-1"), ",")
    assert p.error and "UTF-8" in p.error


def test_parse_empty_is_error():
    p = parse("")
    assert p.error == "CSV is empty."


def test_parse_header_only_is_error():
    p = parse("id,name\n")
    assert p.error and "no data rows" in p.error


def test_parse_blank_lines_skipped():
    p = parse("id\n1\n\n2\n")
    assert p.error is None
    assert p.row_count == 2


# --- parse_column_defs ------------------------------------------------------

def test_coldefs_happy():
    defs, err = ci.parse_column_defs("id int, name text", 2)
    assert err is None
    assert defs == [{"name": "id", "type": "int"}, {"name": "name", "type": "text"}]


def test_coldefs_numeric_precision_inner_comma():
    # The inner comma of numeric(10,2) must NOT split the column apart.
    defs, err = ci.parse_column_defs("amount numeric(10,2), note text", 2)
    assert err is None
    assert defs[0] == {"name": "amount", "type": "numeric(10,2)"}


def test_coldefs_rejects_unknown_type():
    defs, err = ci.parse_column_defs("id frobtype", 1)
    assert defs is None
    assert err and "not allowed" in err


def test_coldefs_rejects_count_mismatch():
    defs, err = ci.parse_column_defs("id int", 2)
    assert defs is None
    assert err and "must match" in err


def test_coldefs_rejects_bad_pair():
    defs, err = ci.parse_column_defs("idonly", 1)
    assert defs is None
    assert err and "name type" in err


def test_coldefs_normalizes_names():
    # The name is a single token (first \S+); normalization lowercases it
    # and replaces non-identifier chars with underscores.
    defs, err = ci.parse_column_defs("User-Name text", 1)
    assert err is None
    assert defs[0]["name"] == "user_name"


def test_coldefs_multiword_type():
    defs, err = ci.parse_column_defs("ts timestamp with time zone", 1)
    assert err is None
    assert defs[0]["type"] == "timestamp with time zone"


# --- create_table_preview ---------------------------------------------------

def test_preview_all_text():
    ddl = ci.create_table_preview("foo", True, ["id", "name"], None)
    assert ddl.startswith('CREATE UNLOGGED TABLE dba."foo"')
    assert '"id" text' in ddl and '"name" text' in ddl


def test_preview_typed_logged():
    ddl = ci.create_table_preview(
        "foo", False, ["id"], [{"name": "id", "type": "int"}])
    assert ddl.startswith('CREATE TABLE dba."foo"')
    assert '"id" int' in ddl


def test_preview_pins_dba_schema():
    ddl = ci.create_table_preview("x", False, ["a"], None)
    assert 'dba."x"' in ddl
