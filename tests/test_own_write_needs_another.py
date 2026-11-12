"""Only a super-admin approves their own write.

An admin with a tier ceiling (or with an end date) used to approve their own
RW request: the role row said "admin", and the admin exemption in
`access.can_approve` asked nothing more. So their own writes on every
production server had one reader, the person who wrote them (found
2026-11-12, operator decision the same day). Their own read-only requests stay
self-approvable, and a super-admin keeps the exemption.

Nothing here may reach a database. A read a test forgot to fake fails it.
"""
from __future__ import annotations

import pytest

from queryhub import access, admins, db

ME = "U0EXAMPLE001"
OTHER = "U0EXAMPLE002"

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
    """`st[pid]` is that person's role rows. Unknown people hold none."""
    st: dict[str, list] = {}
    monkeypatch.setattr(admins, "_v2", lambda: True)
    monkeypatch.setattr(access, "roles", lambda pid: st.get(pid, []))
    return st


def _req(tier, requester=ME, origin="web"):
    return {"id": 41, "requester_slack_id": requester, "target_server_id": 5,
            "required_tier": tier, "engine": "postgres",
            "query": "UPDATE ledgers SET n = 1 WHERE id = 2", "origin": origin}


def test_a_ceiling_admin_cannot_approve_their_own_rw(roles):
    roles[ME] = [CEILING_RW]
    assert admins.can_approve(ME, _req("rw")) is False


def test_a_ceiling_admin_cannot_approve_their_own_ddl(roles):
    """DDL is above an RW ceiling anyway. The own-write rule refuses it first,
    so the reason a screen gives is the right one."""
    roles[ME] = [CEILING_RW]
    assert admins.own_write_needs_another(ME, _req("ddl")) is True
    assert admins.can_approve(ME, _req("ddl")) is False


def test_a_ceiling_admin_still_approves_their_own_read(roles):
    roles[ME] = [CEILING_RW]
    assert admins.can_approve(ME, _req("ro")) is True


def test_a_ceiling_admin_still_approves_another_persons_rw(roles):
    roles[ME] = [CEILING_RW]
    assert admins.can_approve(ME, _req("rw", requester=OTHER)) is True


def test_a_temporary_unscoped_admin_cannot_approve_their_own_rw(roles):
    """No ceiling, but an end date: not a super-admin, so not exempt."""
    roles[ME] = [dict(SUPER, valid_until="2099-01-01T00:00:00+00:00")]
    assert admins.can_approve(ME, _req("rw")) is False


@pytest.mark.parametrize("tier", ["ro", "rw", "ddl"])
def test_a_super_admin_keeps_the_exemption(roles, tier):
    roles[ME] = [SUPER]
    assert admins.own_write_needs_another(ME, _req(tier)) is False
    assert admins.can_approve(ME, _req(tier)) is True


def test_a_window_request_follows_the_same_rule(roles):
    """`decide_window` and `can_decide` pass the window's tier as
    `required_tier`, so an own RW window is refused the same way."""
    roles[ME] = [CEILING_RW]
    window = {"required_tier": "rw", "target_server_id": 5, "requester_slack_id": ME}
    assert admins.can_approve(ME, window) is False


# --- what the refusal says ----------------------------------------------------

def test_the_web_gate_names_the_own_write(roles, monkeypatch):
    from queryhub.web import admin as web_admin, deps
    roles[ME] = [CEILING_RW]
    monkeypatch.setattr(web_admin.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(web_admin.admins, "is_super_admin", lambda uid: False)
    monkeypatch.setattr(deps, "require_whitelisted", lambda claims: None, raising=False)
    with pytest.raises(Exception) as got:
        web_admin.require_admin({"sub": ME}, "review", request=_req("rw"))
    assert "your own write" in str(getattr(got.value, "detail", got.value))


def test_the_admin_card_says_why_it_has_no_buttons(roles):
    from queryhub import targets
    from queryhub.slack_app import notifications as n
    target = targets.TargetServer(
        id=5, alias="example-pg", host="pg.example.test", port=5432,
        default_database="ledger", username="rw", enabled=True, notes=None,
        engine="postgres", engine_config={})
    request = dict(_req("rw"), database_name="ledger", risk_summary=None,
                   justification="fix the typo", result_format="csv", wants_result=True)
    own = n._request_blocks(request, target, with_actions=False, own_write=True)
    scope = n._request_blocks(request, target, with_actions=False)
    assert "your own write" in str(own) and "your own write" not in str(scope)
    assert not any(b.get("type") == "actions" for b in own)
