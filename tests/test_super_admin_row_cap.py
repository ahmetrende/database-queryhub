"""A super-admin keeps a row cap, and it can only ever go up.

The cap is a resource guard, not an authorization one: it stops a mistyped
SELECT from filling the disk. So the super-admin path raises it by configuration
instead of removing it, and the resolution is a FLOOR — a badly chosen value must
not be able to shrink anyone's limit, including the super-admin's own.

`is_super_admin` is read live here for the same reason as everywhere else: the
answer can differ between two submissions and nothing caches it.
"""
from __future__ import annotations

import pytest

from dba_slack_bot import row_limits as rl

_MB = 1024 * 1024


@pytest.fixture
def caps(monkeypatch):
    vals = {"max_rows": 5000, "csv_size_mb": 10, "csv_size_mb_ceiling": 100,
            "super_admin_max_rows": 5000}
    state = {"override": None, "super": False}
    monkeypatch.setattr(rl.cfg, "get_int", lambda k, d=0: vals.get(k, d))
    monkeypatch.setattr(rl, "_override_rows", lambda uid: state["override"])
    monkeypatch.setattr(rl.admins, "is_super_admin", lambda uid: state["super"])
    state["cfg"] = vals
    return state


def test_a_super_admin_gets_the_global_cap_by_default(caps):
    """The seeded value equals max_rows, so turning someone into a super-admin
    changes nothing about their limits until the key is raised."""
    caps["super"] = True
    assert rl.effective_caps("U0EXAMPLE01") == (5000, 10 * _MB)


def test_raising_the_key_raises_the_cap_and_scales_the_size(caps):
    caps["super"] = True
    caps["cfg"]["super_admin_max_rows"] = 50_000
    rows, size = rl.effective_caps("U0EXAMPLE01")
    assert rows == 50_000
    assert size == 100 * _MB, "the size cap should scale with the rows"


def test_it_does_nothing_for_an_ordinary_user(caps):
    caps["cfg"]["super_admin_max_rows"] = 50_000
    caps["super"] = False
    assert rl.effective_caps("U0EXAMPLE01") == (5000, 10 * _MB), (
        "a non-super-admin picked up the super-admin floor")


def test_a_low_value_cannot_shrink_the_cap(caps):
    """The failure this shape prevents: an operator types 100 and the DBA's
    results silently get smaller than everyone else's."""
    caps["super"] = True
    caps["cfg"]["super_admin_max_rows"] = 100
    assert rl.effective_caps("U0EXAMPLE01") == (5000, 10 * _MB)


def test_a_per_user_override_still_wins_when_higher(caps):
    caps["super"] = True
    caps["cfg"]["super_admin_max_rows"] = 20_000
    caps["override"] = 200_000
    rows, size = rl.effective_caps("U0EXAMPLE01")
    assert rows == 200_000
    assert size == 100 * _MB


def test_the_floor_wins_when_the_override_is_lower(caps):
    caps["super"] = True
    caps["cfg"]["super_admin_max_rows"] = 20_000
    caps["override"] = 6000
    assert rl.effective_caps("U0EXAMPLE01")[0] == 20_000


def test_the_size_ceiling_is_still_honoured(caps):
    caps["super"] = True
    caps["cfg"]["super_admin_max_rows"] = 10_000_000
    assert rl.effective_caps("U0EXAMPLE01")[1] == 100 * _MB


def test_the_identity_is_read_on_every_resolution(caps, monkeypatch):
    """No cache: two calls, two answers, because the row changed in between.

    This is the property the operator asked for in so many words — identity is
    resolved per execution, never remembered from the last one.
    """
    calls = {"n": 0}

    def flip(uid):
        calls["n"] += 1
        return calls["n"] > 1        # promoted between the two calls

    caps["cfg"]["super_admin_max_rows"] = 40_000
    monkeypatch.setattr(rl.admins, "is_super_admin", flip)
    assert rl.effective_caps("U0EXAMPLE01")[0] == 5000
    assert rl.effective_caps("U0EXAMPLE01")[0] == 40_000
    assert calls["n"] == 2, "the identity lookup was skipped or cached"
