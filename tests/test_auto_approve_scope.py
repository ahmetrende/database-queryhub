"""Unit tests for auto_approve.grant_covers — the pure scope/tier predicate
behind target-scoped auto-approve grants (migration 051)."""
from queryhub.auto_approve import grant_covers


def _g(max_tier, target=None, db=None):
    return {"max_tier": max_tier, "target_server_id": target, "database_name": db}


def test_broad_grant_covers_any_target():
    g = _g("ro")
    assert grant_covers(g, "ro", target_server_id=49, database_name="nova")
    assert grant_covers(g, "ro", target_server_id=99, database_name="other")
    assert grant_covers(g, "ro")  # no target context still matches a broad grant


def test_tier_gating():
    assert grant_covers(_g("ro"), "ro")
    assert not grant_covers(_g("ro"), "rw")
    assert not grant_covers(_g("ro"), "ddl")
    assert grant_covers(_g("rw"), "rw")
    assert grant_covers(_g("ddl"), "rw")
    assert not grant_covers(_g("rw"), "ddl")


def test_target_scoped_matches_only_its_target():
    g = _g("ro", target=49)
    assert grant_covers(g, "ro", target_server_id=49)
    assert not grant_covers(g, "ro", target_server_id=50)
    # a scoped grant must NOT match when the caller gives no target context
    assert not grant_covers(g, "ro")


def test_target_and_db_scoped():
    g = _g("ro", target=49, db="nova")
    assert grant_covers(g, "ro", target_server_id=49, database_name="nova")
    assert not grant_covers(g, "ro", target_server_id=49, database_name="other")
    assert not grant_covers(g, "ro", target_server_id=50, database_name="nova")


def test_db_ignored_when_target_unscoped():
    g = _g("ro", target=None, db=None)
    assert grant_covers(g, "ro", target_server_id=1, database_name="anything")


def test_unknown_mode_never_covers():
    assert not grant_covers(_g("ddl"), "bogus")
