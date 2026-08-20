"""An auto-approve grant scoped to a database must be able to match.

`grant_covers` compares a non-NULL `database_name` for EQUALITY — there is no
wildcard. The web admin form defaults its database field to `*` and posted it
verbatim, so the grant was stored, looked right in the table, and matched
nothing: the request fell through to manual approval exactly as if no grant
existed. Nothing failed, so nothing was logged. Grant 48 sat like that for
twelve minutes before a human noticed the approvals still arriving by hand.
"""
import pytest

from queryhub import auto_approve as aa


@pytest.mark.parametrize("typed", ["*", "", "   ", "all", "ANY", "Any"])
def test_every_database_spelling_folds_to_null(typed):
    assert aa.normalise_scope(typed) is None


def test_none_stays_none():
    assert aa.normalise_scope(None) is None


@pytest.mark.parametrize("typed,stored", [
    ("notify_service", "notify_service"),
    ("  nova  ", "nova"),
])
def test_a_real_name_is_kept_and_trimmed(typed, stored):
    assert aa.normalise_scope(typed) == stored


def test_the_star_grant_would_not_have_matched():
    """The bug itself, stated as the matcher sees it."""
    star = {"max_tier": "ro", "target_server_id": 21, "database_name": "*"}
    assert not aa.grant_covers(star, "ro", 21, "notify_service")

    fixed = dict(star, database_name=aa.normalise_scope(star["database_name"]))
    assert aa.grant_covers(fixed, "ro", 21, "notify_service")


def test_a_named_scope_still_narrows():
    """Folding `*` must not turn every scoped grant into a fleet-wide one."""
    scoped = {"max_tier": "ro", "target_server_id": 21,
              "database_name": "notify_service"}
    assert aa.grant_covers(scoped, "ro", 21, "notify_service")
    assert not aa.grant_covers(scoped, "ro", 21, "some_other_db")


def test_validate_scope_ignores_a_target_with_no_catalog(monkeypatch):
    """A freshly onboarded server has no snapshot yet; rejecting every database
    on it would block onboarding in order to catch a typo."""
    monkeypatch.setattr(aa.db, "fetch_one", lambda *a, **k: {"hit": 0, "total": 0})
    aa.validate_scope(99, "anything")          # must not raise


def test_validate_scope_rejects_a_database_that_is_not_there(monkeypatch):
    monkeypatch.setattr(aa.db, "fetch_one", lambda *a, **k: {"hit": 0, "total": 7})
    with pytest.raises(aa.ScopeError) as e:
        aa.validate_scope(21, "notify_servcie")     # transposed letters
    assert "notify_servcie" in str(e.value)


def test_validate_scope_accepts_a_database_that_is_there(monkeypatch):
    monkeypatch.setattr(aa.db, "fetch_one", lambda *a, **k: {"hit": 1, "total": 7})
    aa.validate_scope(21, "notify_service")     # must not raise


def test_validate_scope_skips_the_any_database_grant(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not query the catalog for a NULL scope")
    monkeypatch.setattr(aa.db, "fetch_one", boom)
    aa.validate_scope(21, None)
