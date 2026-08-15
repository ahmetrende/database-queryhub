"""Per-user row-limit override resolution: row cap + scaled size cap."""
import pytest

from queryhub import row_limits as rl

_MB = 1024 * 1024


@pytest.fixture
def caps(monkeypatch):
    vals = {"max_rows": 5000, "csv_size_mb": 10, "csv_size_mb_ceiling": 100}
    monkeypatch.setattr(rl.cfg, "get_int", lambda k, d=0: vals.get(k, d))
    state = {"override": None}
    monkeypatch.setattr(rl, "_override_rows", lambda uid: state["override"])
    # The cap now consults identity: a super-admin has a raised FLOOR
    # (config `super_admin_max_rows`), read live on every resolution.
    # These cases are an ordinary user unless a test says otherwise.
    state["super"] = False
    monkeypatch.setattr(rl.admins, "is_super_admin",
                        lambda uid: state["super"])
    return state


def test_no_override_uses_global(caps):
    assert rl.effective_caps("U") == (5000, 10 * _MB)


def test_override_raises_rows_and_scales_size(caps):
    caps["override"] = 15000                       # 3x global
    assert rl.effective_caps("U") == (15000, 30 * _MB)


def test_override_size_capped_at_ceiling(caps):
    caps["override"] = 1_000_000                   # would scale huge
    rows, b = rl.effective_caps("U")
    assert rows == 1_000_000
    assert b == 100 * _MB                          # clamped to ceiling


def test_override_below_global_is_ignored(caps):
    caps["override"] = 100
    assert rl.effective_caps("U") == (5000, 10 * _MB)
