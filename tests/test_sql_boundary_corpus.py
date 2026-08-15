"""The SQL safety boundary, tested the two ways a hand-written case list can't.

1. A regression corpus (tests/corpus/*.txt). The tautology guard had four tests
   and six live bypasses; a corpus turns each discovered phrasing into a
   permanent gate, and its must-allow half stops the guard from being tightened
   into something that blocks ordinary work.

2. A metamorphic property. The interesting bug class here is not "some payload
   is missed" but "a payload we DO catch stops being caught once it is dressed
   up" — comments, parens, case, padding whitespace. That is one property over
   an infinite input space, which is what hypothesis is for.

The property is deliberately one-directional: noise must never turn a blocked
statement into an allowed one. The reverse is not a bug — noise that makes SQL
unparseable should fail closed, i.e. block more.
"""
import re
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dba_slack_bot import query_safety

CORPUS = Path(__file__).parent / "corpus"


def _load(name):
    lines = []
    for raw in (CORPUS / name).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


TAUTOLOGIES = _load("tautologies.txt")
REAL_PREDICATES = _load("real_predicates.txt")


def test_corpus_files_are_not_empty():
    """A silently empty corpus would make every test below vacuously pass."""
    assert len(TAUTOLOGIES) >= 30
    assert len(REAL_PREDICATES) >= 15


@pytest.mark.parametrize("sql", TAUTOLOGIES, ids=range(len(TAUTOLOGIES)))
def test_predicate_that_cannot_filter_rows_is_blocked(sql):
    report = query_safety.analyze(sql)
    assert report.blocked, f"not blocked: {sql}"


@pytest.mark.parametrize("sql", REAL_PREDICATES, ids=range(len(REAL_PREDICATES)))
def test_real_predicate_is_allowed(sql):
    report = query_safety.analyze(sql)
    assert not report.blocked, f"false positive: {sql} -> {report.blockers}"


# --------------------------------------------------------------------------
# Metamorphic property: dressing a blocked statement up must not unblock it.
# --------------------------------------------------------------------------

# Only insert noise OUTSIDE string literals — a comment injected inside a
# literal changes what the statement means, so a verdict change there would be
# correct rather than a bypass.
_TOKEN_SPLIT = re.compile(r"('(?:[^']|'')*')")


def _outside_literals(sql):
    """Yield (chunk, is_literal) so noise can skip literal chunks."""
    for i, part in enumerate(_TOKEN_SPLIT.split(sql)):
        yield part, bool(i % 2)


NOISE = st.sampled_from([
    "block-comment",   # /**/ between tokens
    "line-comment",    # trailing -- comment
    "parens",          # wrap the whole WHERE predicate in ( )
    "case-flip",       # KeYwOrD casing
    "extra-space",     # runs of spaces, tabs, newlines
    "nbsp",            # U+00A0 between tokens: looks like a space, isn't one
])


def _apply_noise(sql, kind):
    if kind == "block-comment":
        out = []
        for chunk, lit in _outside_literals(sql):
            out.append(chunk if lit else chunk.replace(" ", " /**/ ", 1))
        return "".join(out)
    if kind == "line-comment":
        return sql + " -- trailing note"
    if kind == "parens":
        m = re.search(r"\bWHERE\b", sql, re.IGNORECASE)
        if not m:
            return sql
        head, tail = sql[:m.end()], sql[m.end():].strip()
        return f"{head} ({tail})"
    if kind == "case-flip":
        out = []
        for chunk, lit in _outside_literals(sql):
            if lit:
                out.append(chunk)
            else:
                out.append("".join(c.upper() if i % 2 else c.lower()
                                   for i, c in enumerate(chunk)))
        return "".join(out)
    if kind == "nbsp":
        # A non-breaking space is not SQL whitespace to Postgres, so this is
        # the classic "looks identical in the review UI" trick. Verified to be
        # handled in both directions before being added here.
        out = []
        for chunk, lit in _outside_literals(sql):
            out.append(chunk if lit else chunk.replace(" ", "\u00a0"))
        return "".join(out)
    if kind == "extra-space":
        out = []
        for chunk, lit in _outside_literals(sql):
            out.append(chunk if lit else chunk.replace(" ", "  \t \n "))
        return "".join(out)
    raise AssertionError(f"unknown noise {kind}")


@settings(max_examples=250, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(sql=st.sampled_from(TAUTOLOGIES),
       noises=st.lists(NOISE, min_size=1, max_size=3))
def test_noise_never_unblocks_a_blocked_statement(sql, noises):
    """The bypass shape: `1=1` is caught, so an attacker writes `1 /**/ = 1`.

    Any composition of comment/paren/case/whitespace noise, applied outside
    string literals, must leave the verdict blocked.
    """
    noisy = sql
    for kind in noises:
        noisy = _apply_noise(noisy, kind)
    assert query_safety.analyze(noisy).blocked, \
        f"noise {noises} unblocked: {noisy!r}"


@settings(max_examples=120, deadline=None)
@given(sql=st.sampled_from(REAL_PREDICATES),
       noise=NOISE)
def test_noise_does_not_break_legitimate_queries(sql, noise):
    """The other half: cosmetic noise must not start rejecting real work
    either, or every formatter becomes a denial of service."""
    noisy = _apply_noise(sql, noise)
    report = query_safety.analyze(noisy)
    assert not report.blocked, f"{noise} blocked a real predicate: {noisy!r}"


# --------------------------------------------------------------------------
# Tier classification must not be affected by the same noise: an operation that
# needs DDL credentials must not be downgraded to RO by a comment.
# --------------------------------------------------------------------------

TIERED = [
    ("SELECT * FROM t WHERE id = 1", "ro"),
    ("UPDATE t SET a = 1 WHERE id = 1", "rw"),
    ("DELETE FROM t WHERE id = 1", "rw"),
    ("ALTER TABLE t ADD COLUMN c text", "ddl"),
    ("VACUUM FULL t", "ddl"),
    ("REFRESH MATERIALIZED VIEW mv", "ddl"),
    ("TRUNCATE TABLE t", "ddl"),
]


@settings(max_examples=150, deadline=None)
@given(case=st.sampled_from(TIERED), noise=NOISE)
def test_noise_never_lowers_the_required_tier(case, noise):
    sql, expected = case
    noisy = _apply_noise(sql, noise)
    report = query_safety.analyze(noisy)
    rank = {"ro": 1, "rw": 2, "ddl": 3}
    if report.blocked:
        return          # failing closed is always acceptable
    assert rank[report.main_tier] >= rank[expected], \
        f"{noise} lowered {sql!r} from {expected} to {report.main_tier}"
