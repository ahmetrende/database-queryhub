"""Standing grants can end on their own (migration 096).

Until this shipped, `auto_approve_grants` was the only grant with an expiry:
`user_target_grants` had a manual revoke and `team_target_grants` had neither,
so access given for one afternoon's migration outlived the migration, the
quarter, and — measured on 2026-08-15 — an employee who had already left.

What these pin is not "the column exists". It is that the ONE authority every
surface asks, `teams.effective_grant_for_user`, applies expiry on both levels,
that expiry is evaluated live rather than swept, and the one behaviour that is
easy to get backwards: an expired user override must not fall through to a
wider team grant.
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "queryhub" / "teams.py"


def _src():
    return SRC.read_text(encoding="utf-8")


def _query_blocks(src):
    """Every SQL string handed to a db.fetch_* call in this module."""
    return re.findall(r'db\.fetch_\w+\(\s*(?:f?"[^"]*"\s*\n?\s*)+', src)


def test_every_grant_query_checks_expiry():
    """The rule has to hold in every reader, not just the famous one.

    Six places in this module read these tables: two target-list queries, a
    search, two EXISTS checks and the resolver itself. A picker that lists a
    target the submit path then refuses is the failure this prevents — and it
    is the failure you get by updating the resolver alone.
    """
    src = _src()
    missing = [b for b in _query_blocks(src)
               if ("user_target_grants" in b or "team_target_grants" in b)
               and "expires_at" not in b]
    assert not missing, (
        f"{len(missing)} query on a grant table with no expiry check:\n"
        + "\n".join(b[:140] for b in missing))


def test_both_grant_levels_are_covered():
    """A team grant had NO liveness filter at all before this — not even a
    revoke — so it is the level most likely to be forgotten."""
    src = _src()
    assert "g.expires_at IS NULL OR g.expires_at > NOW()" in src, \
        "team_target_grants expiry is not enforced"
    assert "expires_at IS NULL OR expires_at > NOW()" in src, \
        "user_target_grants expiry is not enforced"
    assert "g.revoked_at IS NULL" in src, \
        "team_target_grants revoke is not enforced"


def test_expiry_is_evaluated_in_sql_not_in_python():
    """Live, at resolution time, in the same statement that reads the row.

    A sweep job that marks grants expired is a window in which the answer is
    stale — between the grant lapsing and the job running, the gateway would
    still say yes. Every other authorization answer here is computed live and
    this one must be too, which is why the predicate is SQL rather than a
    timestamp compared after the fetch.
    """
    src = _src()
    assert "NOW()" in src, "expiry must be evaluated by the database"
    # No scheduled cleanup pretending to be the enforcement point.
    assert not re.search(r"def .*(sweep|expire_grants|purge_grants)", src), \
        "expiry must not be a background job in this module"


def test_an_expired_user_override_does_not_widen_access():
    """The one that is easy to get backwards.

    A user row exists to OVERRIDE a team grant — often to NARROW it. If it
    fell through to the team aggregate when it lapsed, expiring an override
    would silently restore the wider access it was written to replace, and
    "this person, this target, until Friday" would end on Friday by granting
    more. The resolver returns on the user row or on nothing.
    """
    src = _src()
    fn = src[src.index("def effective_grant_for_user"):]
    user_part = fn[:fn.index("# 2. aggregate team grants")]
    # The user branch must RETURN, not fall through, when a live row is found;
    # and the query that found it is the one carrying the expiry predicate.
    assert "expires_at" in user_part, "the user-level lookup lost its expiry check"
    assert 'return {' in user_part, "the user branch no longer returns on a hit"
    # And the docstring has to say so, because this is a decision, not a detail.
    doc = fn[:fn.index('"""', fn.index('"""') + 3)]
    assert "does NOT fall through" in doc or "must never widen" in doc, \
        "the no-widening rule is not written down where the next reader will look"


def test_null_expiry_means_never():
    """Every grant that existed when 096 shipped got NULL, and NULL has to keep
    meaning 'no expiry' — the migration must not have changed anyone's access."""
    src = _src()
    assert "expires_at IS NULL OR" in src, \
        "NULL no longer short-circuits to 'never expires'"
