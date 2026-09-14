"""The schema endpoint asks about PII once, not once per column.

`pii.column_pii_map` reloads the pattern catalog from the bot DB on every
call -- its docstring says "computed once per result set", which is true of the
executor, where one call covers a whole result. The /schema route called it
inside the per-column loop with a single name, so the largest catalogued
database (just over 40,000 columns) turned one request into that many identical
SELECTs.

Batching is safe precisely because the old call passed the column NAME and
nothing else: no SQL, no lineage, no table. Name-level granularity is what the
endpoint already had, so the same set of names comes back -- computed once.
"""
import inspect

from queryhub import pii
from queryhub.web import routes_data


def _code(mod) -> str:
    """Source with comment lines dropped. The comment explaining the fix
    quotes the call it removed, so a plain `in` check on the file finds the
    prose and calls it the bug."""
    return "\n".join(ln for ln in inspect.getsource(mod).splitlines()
                      if not ln.lstrip().startswith("#"))


def test_the_route_does_not_ask_per_column():
    code = _code(routes_data)
    assert "column_pii_map([name])" not in code
    assert "pii.column_pii_map(all_names)" in code


# (pattern, pii_type, match_type, exclude_tokens) -- the shape
# `_load_column_patterns` returns. A fixed catalog, so the property is tested
# against the rules rather than against whatever production happens to hold.
_PATTERNS = [
    ("email", "email", "token", None),
    ("name", "name", "token", ["database", "table", "schema", "column"]),
    ("tckn", "tckn", "token", None),
    ("iban", "iban", "token", None),
]


def test_the_batched_answer_equals_the_per_name_answer(monkeypatch):
    """The property that makes the change safe, put to the function rather
    than argued in a comment: one call over N names flags the same names as N
    calls of one. Includes a name the exclusion list must keep OUT
    (`table_name`) and a duplicate, because collapsing duplicates is where the
    saving comes from."""
    monkeypatch.setattr(pii, "_load_column_patterns", lambda: _PATTERNS)
    names = ["email", "user_name", "product_id", "tckn", "created_at",
             "iban", "table_name", "email"]
    uniq = sorted(set(names))
    batched = {uniq[i] for i in pii.column_pii_map(uniq)}
    one_at_a_time = {n for n in uniq if pii.column_pii_map([n])}
    assert batched == one_at_a_time
    assert "email" in batched and "tckn" in batched
    assert "table_name" not in batched, "the exclusion list must survive batching"
    assert "product_id" not in batched


def test_an_empty_catalog_does_not_call_out_at_all():
    """A database with no catalogued columns must not send an empty IN list
    or a needless round trip."""
    src = inspect.getsource(routes_data)
    i = src.index("all_names = sorted(")
    assert "if all_names else set()" in src[i:i + 400]


def test_duplicate_names_across_tables_collapse():
    """`created_at` exists in nearly every table; the lookup should see it
    once. This is where the saving actually comes from."""
    src = inspect.getsource(routes_data)
    i = src.index("all_names = sorted(")
    assert "{c[\"column_name\"] for c in crows}" in src[i:i + 200], "must be a set"
