"""Only a super-admin approves a request on a super-admin-only target.

Migration 141 stopped an admin's own access to a flagged target (the control
plane). It did not stop an admin from approving somebody else's request there,
so a query on the control plane could run with no super-admin reading it
(found 2026-11-12, operator decision the same day). A waiver that names the
target still skips the wait: no approver is asked then.

Nothing here may reach a database. A read a test forgot to fake fails it.
"""
from __future__ import annotations

import pytest

from queryhub import access, admins, db

ME = "U0EXAMPLE001"
OTHER = "U0EXAMPLE002"
FLAGGED, PLAIN = 1, 5

SUPER = {"role": "admin", "scope_team_id": None, "all_teams": True,
         "scope_target_id": None, "all_targets": True, "max_tier": None,
         "any_tier": True, "valid_until": None}
CEILING_RW = dict(SUPER, max_tier="rw", any_tier=False)


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test in this module reached the database")
    for name in ("connection", "transaction", "execute", "fetch_one", "fetch_all"):
        monkeypatch.setattr(db, name, refuse)


@pytest.fixture
def roles(monkeypatch):
    st: dict[str, list] = {}
    monkeypatch.setattr(admins, "_v2", lambda: True)
    monkeypatch.setattr(access, "roles", lambda pid: st.get(pid, []))
    monkeypatch.setattr(access, "target_is_super_admin_only", lambda tid: tid == FLAGGED)
    return st


def _req(tid, tier="ro"):
    return {"id": 41, "requester_slack_id": OTHER, "target_server_id": tid,
            "required_tier": tier, "engine": "postgres", "origin": "web"}


@pytest.mark.parametrize("tier", ["ro", "rw"])
def test_a_capped_admin_cannot_approve_on_a_flagged_target(roles, tier):
    roles[ME] = [CEILING_RW]
    assert admins.can_approve(ME, _req(FLAGGED, tier)) is False


def test_a_capped_admin_still_approves_on_other_targets(roles):
    roles[ME] = [CEILING_RW]
    assert admins.can_approve(ME, _req(PLAIN, "rw")) is True


def test_a_scoped_approver_named_on_the_flagged_target_cannot_approve(roles):
    roles[ME] = [{"role": "approver", "scope_team_id": None, "all_teams": True,
                  "scope_target_id": FLAGGED, "all_targets": False, "max_tier": "ro",
                  "any_tier": False, "valid_until": None}]
    assert admins.can_approve(ME, _req(FLAGGED)) is False


@pytest.mark.parametrize("tier", ["ro", "rw", "ddl"])
def test_a_super_admin_approves_on_a_flagged_target(roles, tier):
    roles[ME] = [SUPER]
    assert admins.can_approve(ME, _req(FLAGGED, tier)) is True


def test_a_window_request_on_a_flagged_target_follows_the_rule(roles):
    roles[ME] = [CEILING_RW]
    window = {"required_tier": "ro", "target_server_id": FLAGGED, "requester_slack_id": OTHER}
    assert admins.can_approve(ME, window) is False


def test_a_target_without_the_flag_reads_no_role_for_this_rule(roles, monkeypatch):
    """The flag is read first: a plain target costs no super-admin read here."""
    asked = []
    monkeypatch.setattr(admins, "is_super_admin", lambda pid: asked.append(pid) or False)
    admins.flagged_target_needs_super_admin(ME, _req(PLAIN))
    assert asked == []
