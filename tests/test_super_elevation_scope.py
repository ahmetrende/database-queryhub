"""The super-admin elevation is for DDL, and only DDL.

`queryhub_superadmin` is a member of `rds_superuser`. It was granted to all
three logins after two RO requests failed with "Permission denied to set role":
the SELECTs never ran, they died on a SET ROLE that had been asked for
regardless of tier. Granting the membership fixed the symptom and turned the
read-only password — one password, shared by every target in the fleet — into a
superuser credential on every cluster where the elevation exists.

A read does not need it: the RO login inherits `pg_read_all_data`. A write does
not either: the RW login holds explicit grants, and no RW request has ever run
elevated. DDL is what the role is for, because the bot's login owns nothing.
"""
import pytest

from queryhub import executor as ex

SUPER = "U_SUPER"
PLAIN = "U_PLAIN"


@pytest.fixture
def fleet(monkeypatch):
    monkeypatch.setattr(ex.admins, "is_super_admin", lambda uid: uid == SUPER)
    monkeypatch.setattr(ex.db, "fetch_one",
                        lambda *a, **k: {"super_ddl_role": "queryhub_superadmin"})


def test_ddl_enters_the_role(fleet):
    assert ex._super_role_for(SUPER, 3, "ddl") == "queryhub_superadmin"


def test_a_read_does_not(fleet):
    # The 42 elevated RO executions on record are the ones this removes.
    assert ex._super_role_for(SUPER, 3, "ro") is None


def test_a_write_does_not(fleet):
    assert ex._super_role_for(SUPER, 3, "rw") is None


def test_a_non_super_admin_never_does(fleet):
    assert ex._super_role_for(PLAIN, 3, "ddl") is None


def test_a_target_with_no_role_configured_gives_none(monkeypatch):
    monkeypatch.setattr(ex.admins, "is_super_admin", lambda uid: True)
    monkeypatch.setattr(ex.db, "fetch_one", lambda *a, **k: {"super_ddl_role": None})
    assert ex._super_role_for(SUPER, 3, "ddl") is None


def test_the_tier_is_checked_before_anything_is_read(monkeypatch):
    # Cheapest possible refusal, and it means an RO query cannot fail on a
    # lookup for a role it will not use.
    monkeypatch.setattr(ex.admins, "is_super_admin",
                        lambda uid: pytest.fail("must not check standing for a read"))
    monkeypatch.setattr(ex.db, "fetch_one",
                        lambda *a, **k: pytest.fail("must not read the target row"))
    assert ex._super_role_for(SUPER, 3, "ro") is None


def test_an_omitted_mode_keeps_the_old_behaviour(fleet):
    # The caller that asks without a tier is the audit flag, which mirrors
    # whatever the executor decided; leaving it unchanged keeps that honest.
    assert ex._super_role_for(SUPER, 3) == "queryhub_superadmin"
