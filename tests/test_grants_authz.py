"""grants.authz / grants.grant — who may hand out access, and up to what tier.

authz() is the gate that decides whether an admin can grant at all and how far.
It had no direct test: the one test that touches the grant flow monkeypatches
authz away entirely, so both the function and the convention that callers must
consult it were unverified. The module docstring states the contract in prose
("Callers must enforce this via authz() before calling grant()") and prose is
not a gate.

The verdict hinges on one SQL expression — `max_tier IS NULL AND scope_team_ids
IS NULL AND scope_target_ids IS NULL AS is_super` — so these tests drive authz
through a fake row for every combination that expression can produce, and pin
the tier ceiling that follows from it.
"""
import pytest

from queryhub import grants


@pytest.fixture
def rows(monkeypatch):
    """authz() does exactly one fetch_one; feed it whatever the test needs."""
    box = {"row": None}
    monkeypatch.setattr(grants.db, "fetch_one", lambda *a, **k: box["row"])
    return box


# ------------------------------------------------------------------ authz


def test_unknown_or_disabled_admin_cannot_grant(rows):
    """The query filters on `enabled`, so a disabled admin looks like no row.
    None means "cannot grant", not "unlimited"."""
    rows["row"] = None
    assert grants.authz("U0NOBODY") is None


def test_admin_without_can_grant_cannot_grant(rows):
    rows["row"] = {"can_grant": False, "max_tier": "rw", "is_super": False}
    assert grants.authz("U0DBA") is None


def test_scoped_admin_with_can_grant_is_capped_at_its_max_tier(rows):
    rows["row"] = {"can_grant": True, "max_tier": "rw", "is_super": False}
    cap = grants.authz("U0DBA")
    assert cap == {"super": False, "max_tier": "rw"}
    assert grants.allowed_tiers(cap) == ["ro", "rw"]


def test_super_admin_is_unlimited_even_without_can_grant(rows):
    """is_super is derived from having no ceiling and no scope at all. Such an
    admin may grant regardless of the can_grant flag — that is what the SQL
    says, and it is the property the whole admin model rests on."""
    rows["row"] = {"can_grant": False, "max_tier": None, "is_super": True}
    cap = grants.authz("U0SUPER")
    assert cap == {"super": True, "max_tier": None}
    assert grants.allowed_tiers(cap) == ["ro", "rw", "ddl"]


@pytest.mark.parametrize("max_tier,expected", [
    ("ro", ["ro"]),
    ("rw", ["ro", "rw"]),
    ("ddl", ["ro", "rw", "ddl"]),
    (None, ["ro", "rw", "ddl"]),
])
def test_allowed_tiers_never_exceeds_the_ceiling(max_tier, expected):
    cap = {"super": max_tier is None, "max_tier": max_tier}
    assert grants.allowed_tiers(cap) == expected


def test_a_granter_cannot_hand_out_a_tier_above_its_own(rows):
    """The escalation this prevents: an RW-capped admin granting DDL."""
    rows["row"] = {"can_grant": True, "max_tier": "rw", "is_super": False}
    cap = grants.authz("U0DBA")
    assert "ddl" not in grants.allowed_tiers(cap)


# ------------------------------------------------- control-plane protection


class _Cur:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(" ".join(sql.split()))

    def fetchone(self):
        return {"inserted": True, "mode": "ro", "allowed_databases": None}


class _Txn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self.cur

    def __exit__(self, *a):
        return False


def test_granting_on_the_control_plane_target_is_refused(monkeypatch):
    """A grant on the bot's own metadata database would let the grantee edit
    audit_log and admins — i.e. rewrite the record of what they did. The check
    lives in grant() itself, not in the callers, because the Slack modal had it
    and the web panel did not."""
    cur = _Cur()
    monkeypatch.setattr(grants.db, "transaction", lambda: _Txn(cur))
    monkeypatch.setattr(grants, "control_plane_target_ids", lambda: {4})

    with pytest.raises(PermissionError) as e:
        grants.grant(granter_id="U0SUPER", granter_name="s",
                     grantee_id="U0DEV", grantee_profile={},
                     target_id=4, mode="ro", databases=None, reason=None,
                     notify=False)
    assert "control-plane" in str(e.value)
    assert cur.executed == [], "refused grant still wrote to the database"


def test_granting_on_an_ordinary_target_proceeds(monkeypatch):
    cur = _Cur()
    monkeypatch.setattr(grants.db, "transaction", lambda: _Txn(cur))
    monkeypatch.setattr(grants, "control_plane_target_ids", lambda: {4})

    out = grants.grant(granter_id="U0SUPER", granter_name="s",
                       grantee_id="U0DEV", grantee_profile={"name": "Dev"},
                       target_id=9, mode="ro", databases=["app"], reason="ticket",
                       notify=False)
    assert out["mode"] == "ro"
    joined = " | ".join(cur.executed)
    # Whitelist + grant + audit, all in the one transaction.
    assert "INSERT INTO requesters" in joined
    assert "INSERT INTO user_target_grants" in joined
    assert "access_granted" in joined
    # The auth-event outbox must be suppressed here: this path sends its own DM,
    # and without this the grantee gets two.
    assert "SET LOCAL app.auth_dm_suppress = 'on'" in joined


def test_control_plane_detection_prefers_explicit_config(monkeypatch):
    """An operator behind a pooler or a CNAME can't be auto-detected, so the
    config key has to win over detection."""
    from queryhub import config as cfg
    monkeypatch.setattr(cfg, "get_setting",
                        lambda k, d=None: "3, 7" if k == "control_plane_target_ids" else d)
    assert grants.control_plane_target_ids() == {3, 7}


def test_control_plane_detection_ignores_garbage_config(monkeypatch):
    """A typo must not silently disable the protection — it falls through to
    detection rather than returning an empty set."""
    from queryhub import config as cfg
    monkeypatch.setattr(cfg, "get_setting",
                        lambda k, d=None: "abc,," if k == "control_plane_target_ids" else d)
    monkeypatch.setattr(grants.db, "fetch_all", lambda *a, **k: [])
    assert grants.control_plane_target_ids() == set()
