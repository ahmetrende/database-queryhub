"""An auto-approve rule can be switched off, and on again (PLA-1592 U2).

`PATCH /admin/auto-grants/{id}` with `{enabled}`. A switched-off rule approves
nothing, so every reader of the rule tables has to skip it: the submit
decision, the v2 waiver read, the access resolver, the screens that list what
a person or a team holds, the copy of access, the expiry warnings, and the
migration-109 mirror. These tests pin each reader in the hermetic suite. The
behaviour against real SQL is in tests/test_integration_expiry_rules.py.

The list keeps showing a switched-off rule, with `enabled: false`, because the
list is where it is switched on again. `runCount` counts the `auto_approved`
audit rows that cite the rule, and only those.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from queryhub import access, auth_events, auto_approve, grant_expiry, teams
from queryhub.web import routes_admin as ra

CLAIMS = {"sub": "U0EXAMPLE001", "name": "Jordan Ray"}
NOW = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)


class Cur:
    """A cursor that answers each SELECT from a queue and records every SQL."""

    def __init__(self, answers=None):
        self.sql: list[tuple[str, object]] = []
        self.answers = list(answers or [])
        self._row = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        self._row = self.answers.pop(0) if sql.lstrip().startswith("SELECT") and self.answers \
            else None

    def fetchone(self):
        return self._row


class Txn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self.cur

    def __exit__(self, *exc):
        return False


@pytest.fixture
def env(monkeypatch):
    st = {"cur": Cur(), "audit": [], "listed": []}
    monkeypatch.setattr(ra.admin, "require_admin", lambda claims, need: claims["sub"])
    monkeypatch.setattr(ra.db, "transaction", lambda: Txn(st["cur"]))
    monkeypatch.setattr(ra.audit, "log_in",
                        lambda cur, rid, uid, name, action, details=None:
                        st["audit"].append((action, details)))
    monkeypatch.setattr(ra, "admin_auto_grants",
                        lambda claims: {"autoGrants": st["listed"]})
    return st


def _patch(gid, enabled, claims=CLAIMS):
    return ra.admin_set_auto_grant(gid, ra.AutoGrantPatchIn(enabled=enabled), claims=claims)


def _status(fn, *a):
    with pytest.raises(HTTPException) as e:
        fn(*a)
    return e.value.status_code


# --- the endpoint ------------------------------------------------------------

def test_switching_a_legacy_rule_off_writes_the_flag_and_one_audit_row(env):
    env["cur"].answers = [{"id": 41, "slack_user_id": "U0EXAMPLE002",
                           "target_server_id": 53, "database_name": "ledger",
                           "max_tier": "ro", "enabled": True, "ends": None,
                           "ended": False}]
    env["listed"] = [{"id": "41", "enabled": False, "runCount": 3}]
    out = _patch("41", False)
    assert out == {"id": "41", "enabled": False, "runCount": 3, "changed": True}
    [_select, (sql, params)] = env["cur"].sql
    assert sql.startswith("UPDATE auto_approve_grants SET enabled = %s")
    assert params == (False, 41)
    assert env["audit"] == [("auto_approve_disabled",
                             {"grant_id": 41, "user": "U0EXAMPLE002",
                              "target_id": 53, "database": "ledger", "tier": "ro"})]


def test_switching_a_team_waiver_on_writes_access_grant(env):
    env["cur"].answers = [{"id": 501, "team_id": 7, "principal_id": None,
                           "target_id": 53, "database_name": None, "tier": "ro",
                           "enabled": False, "ends": None, "ended": False}]
    env["listed"] = [{"id": "ag:501", "enabled": True, "runCount": 0}]
    out = _patch("ag:501", True)
    assert out["changed"] is True and out["enabled"] is True
    [(select, sel_params), (sql, params)] = env["cur"].sql
    assert "mirrored_from IS NULL" in select and sel_params == (501,)
    assert sql.startswith("UPDATE access_grant SET enabled = %s")
    assert params == (True, 501)
    assert [a for a, _ in env["audit"]] == ["auto_approve_enabled"]
    assert env["audit"][0][1]["grant_id"] == "ag:501"


def test_an_edit_that_changes_nothing_writes_nothing(env):
    env["cur"].answers = [{"id": 41, "slack_user_id": "U0EXAMPLE002",
                           "target_server_id": 53, "database_name": None,
                           "max_tier": "ro", "enabled": True, "ends": None,
                           "ended": False}]
    env["listed"] = [{"id": "41", "enabled": True}]
    assert _patch("41", True)["changed"] is False
    assert len(env["cur"].sql) == 1 and env["audit"] == []


def test_a_rule_that_has_ended_is_refused_and_untouched(env):
    """Switching an ended rule changes nothing, and the list no longer shows
    it. A 409 says so rather than a 200 that reads like it did something."""
    env["cur"].answers = [{"id": 41, "slack_user_id": "U0EXAMPLE002",
                           "target_server_id": 53, "database_name": None,
                           "max_tier": "ro", "enabled": False,
                           "ends": NOW - timedelta(days=1), "ended": True}]
    assert _status(_patch, "41", True) == 409
    assert len(env["cur"].sql) == 1 and env["audit"] == []


def test_a_team_waiver_that_has_ended_is_refused_too(env):
    env["cur"].answers = [{"id": 501, "team_id": 7, "principal_id": None,
                           "target_id": 53, "database_name": None, "tier": "ro",
                           "enabled": True, "ends": NOW - timedelta(hours=1),
                           "ended": True}]
    assert _status(_patch, "ag:501", False) == 409
    assert len(env["cur"].sql) == 1 and env["audit"] == []


def test_an_unknown_rule_is_404_and_a_bad_id_is_400(env):
    assert _status(_patch, "41", False) == 404
    assert _status(_patch, "ag:501", False) == 404
    for bad in ("x", "ag:x"):
        assert _status(_patch, bad, False) == 400


def test_the_body_takes_enabled_and_nothing_else():
    with pytest.raises(Exception):
        ra.AutoGrantPatchIn(enabled=False, tier="rw")
    with pytest.raises(Exception):
        ra.AutoGrantPatchIn()


def test_same_authority_as_the_other_rule_writes(monkeypatch):
    seen = []
    monkeypatch.setattr(ra.admin, "require_admin",
                        lambda claims, need: seen.append(need) or claims["sub"])
    monkeypatch.setattr(ra.db, "transaction", lambda: Txn(Cur()))
    with pytest.raises(HTTPException):
        _patch("41", False)
    assert seen == ["access"]
    for fn in (ra.admin_create_auto_grant, ra.admin_delete_auto_grant):
        assert 'require_admin(claims, "access")' in inspect.getsource(fn)


# --- the list ----------------------------------------------------------------

def test_the_list_carries_enabled_and_run_count(monkeypatch):
    rows = [{"id": 41, "slack_user_id": "U0EXAMPLE002", "max_tier": "ro",
             "target_server_id": None, "database_name": None, "reason": None,
             "expires_at": None, "granted_by": None, "granted_at": None,
             "enabled": False, "user_name": "Casey Lin", "granted_by_name": None},
            {"id": 42, "slack_user_id": "U0EXAMPLE003", "max_tier": "ro",
             "target_server_id": None, "database_name": None, "reason": None,
             "expires_at": None, "granted_by": None, "granted_at": None,
             "enabled": True, "user_name": "Sam Ortiz", "granted_by_name": None}]
    asked = []

    def fetch_all(sql, params=None):
        if "FROM audit_log" in sql:
            asked.append((sql, params))
            return [{"gid": "41", "n": 5}]
        return rows
    monkeypatch.setattr(ra.admin, "require_admin", lambda claims, need: claims["sub"])
    monkeypatch.setattr(ra.db, "fetch_all", fetch_all)
    monkeypatch.setattr(ra.teams_mod, "use_v2", lambda: False)
    out = ra.admin_auto_grants(CLAIMS)["autoGrants"]
    assert [(g["id"], g["enabled"], g["runCount"]) for g in out] == [
        ("41", False, 5), ("42", True, 0)]
    [(sql, params)] = asked
    assert params == (["41", "42"],)


def test_the_run_count_reads_only_the_approval_rows():
    """The `submitted` row of an auto-approved query carries the same grant_id.
    Counting it would double every number."""
    src = inspect.getsource(ra._auto_grant_run_counts)
    assert "action = 'auto_approved'" in src
    assert "details->>'grant_id'" in src
    assert ra._auto_grant_run_counts([]) == {}


def test_the_v2_list_shows_a_switched_off_waiver_with_its_flag():
    src = inspect.getsource(ra._v2_only_auto_grants)
    assert "g.enabled" in src and '"enabled": bool(r.get("enabled", True))' in src


# --- every reader skips a switched-off rule ----------------------------------

def test_the_decision_reads_only_switched_on_legacy_rules():
    src = inspect.getsource(auto_approve.active_grants)
    assert "AND enabled" in src


def test_the_decision_reads_only_switched_on_v2_waivers():
    assert "AND g.enabled" in inspect.getsource(auto_approve._v2_only_waivers)


@pytest.mark.parametrize("fn", [access._covering, access.resolve_many,
                                access.resolve_databases])
def test_the_resolver_skips_a_switched_off_waiver(fn):
    """`auto_tier` comes from waiver rows. A switched-off one is not counted."""
    assert "g.revoked_at IS NULL AND g.enabled" in inspect.getsource(fn)


def test_a_switched_off_team_waiver_covers_nothing_when_a_grant_is_written():
    assert "AND auto_approve AND enabled" in inspect.getsource(ra._team_waiver_plan)


def test_the_team_view_lists_only_switched_on_waivers():
    assert "AND g.enabled" in inspect.getsource(ra._team_rows_v2)


def test_the_legacy_person_view_lists_only_switched_on_rules():
    assert "AND enabled AND starts_at <= NOW()" in inspect.getsource(
        ra._effective_access_legacy)


def test_copying_access_never_copies_a_switched_off_rule():
    src = inspect.getsource(ra.admin_copy_access)
    assert src.count("WHERE slack_user_id = %s AND enabled") == 2


def test_an_ended_waiver_that_is_switched_off_is_not_warned_about():
    assert "AND g.enabled" in inspect.getsource(grant_expiry)


def _copy_script_func(name: str) -> str:
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "scripts"
           / "copy_access_model.py").read_text(encoding="utf-8")
    m = re.search(rf"\ndef {name}\(.*?(?=\ndef |\nclass |\Z)", src, re.S)
    assert m, name
    return m.group(0)


def test_the_access_model_copy_skips_a_switched_off_rule():
    """scripts/copy_access_model.py writes the new model from the old tables.
    A re-run must not copy a paused rule as a live waiver, and its drift gate
    must count the same live rules on both sides."""
    assert "WHERE enabled AND (expires_at IS NULL OR expires_at > now())" in \
        _copy_script_func("copy_grants")
    gate = _copy_script_func("verify")
    assert "WHERE auto_approve AND enabled AND revoked_at IS NULL" in gate
    assert "WHERE enabled AND (expires_at IS NULL OR expires_at > now())" in gate


def test_the_bulk_refusal_says_to_switch_a_held_waiver_on(monkeypatch):
    monkeypatch.setattr(ra.admin, "require_admin", lambda claims, need: claims["sub"])
    monkeypatch.setattr(ra.teams_mod, "use_v2", lambda: True)
    monkeypatch.setattr(ra, "_resolve_team", lambda n: {"id": 7, "name": "Team Alpha"})
    monkeypatch.setattr(ra, "_target_id_of", lambda c: 53)
    monkeypatch.setattr(auto_approve, "validate_scope", lambda tid, dbn: None)
    monkeypatch.setattr(ra, "_team_reach", lambda tid, scopes: {s: "rw" for s in scopes})
    monkeypatch.setattr(ra.access_model, "target_is_super_admin_only", lambda tid: False)
    monkeypatch.setattr(ra.db, "fetch_all", lambda sql, params=None: [
        {"target_id": 53, "database_name": None, "enabled": False}])
    with pytest.raises(HTTPException) as e:
        ra.admin_bulk_create_auto_grants(ra.BulkAutoGrantIn(
            subjectType="team", subject="Team Alpha",
            targets=[{"connectionId": "prod-main"}]), claims=CLAIMS)
    assert e.value.status_code == 409
    assert "switched off" in e.value.detail["refused"][0]["reason"]


def test_a_paused_rule_is_not_a_live_waiver_to_the_decision(monkeypatch):
    """The decision path end to end, with the read answering as the database
    does once the rule is off: no row, so a human approves."""
    monkeypatch.setattr(auto_approve.db, "fetch_all", lambda q, p=None: [])
    monkeypatch.setattr(teams, "use_v2", lambda: True)
    assert auto_approve.effective_grant("U0EXAMPLE002", "ro", 53, "ledger") is None


# --- the person is told ------------------------------------------------------

def _ev(table, old, new, user="U0EXAMPLE002", team=None):
    return {"table_name": table, "op": "UPDATE", "old_row": old, "new_row": new,
            "slack_user_id": user, "team_id": team}


def test_switching_a_legacy_rule_off_and_on_dms_the_person():
    base = {"max_tier": "ro", "target_server_id": 53, "database_name": "ledger",
            "expires_at": None}
    off = auth_events.build_notifications(
        _ev("auto_approve_grants", {**base, "enabled": True}, {**base, "enabled": False}),
        alias_of=lambda t: "prod-main")
    on = auth_events.build_notifications(
        _ev("auto_approve_grants", {**base, "enabled": False}, {**base, "enabled": True}),
        alias_of=lambda t: "prod-main")
    assert len(off) == 1 and "is off" in off[0][1] and "`prod-main`" in off[0][1]
    assert len(on) == 1 and "is on again" in on[0][1] and "*permanent*" in on[0][1]


def test_switching_a_team_waiver_tells_every_member():
    row = {"tier": "ro", "target_id": 53, "all_databases": True, "auto_approve": True,
           "team_id": 7, "valid_until": None, "revoked_at": None}
    notes = auth_events.build_notifications(
        _ev("access_grant", {**row, "enabled": True}, {**row, "enabled": False},
            user=None, team=7),
        alias_of=lambda t: "prod-main",
        team_info=lambda tid, table=None: ("Team Alpha", ["U0EXAMPLE004", "U0EXAMPLE005"]))
    assert [u for u, _ in notes] == ["U0EXAMPLE004", "U0EXAMPLE005"]
    assert all("not skipped any more" in t for _, t in notes)


def test_a_switch_with_nobody_to_tell_is_silent():
    base = {"max_tier": "ro", "target_server_id": 53, "expires_at": None}
    assert auth_events.build_notifications(
        _ev("auto_approve_grants", {**base, "enabled": True}, {**base, "enabled": False},
            user=None),
        alias_of=lambda t: "prod-main") == []


def test_an_outbox_row_from_before_the_column_is_silent():
    """No `enabled` key on either side reads as on, so nothing changed."""
    base = {"max_tier": "ro", "target_server_id": 53, "expires_at": None}
    assert auth_events.build_notifications(
        _ev("auto_approve_grants", dict(base), dict(base)),
        alias_of=lambda t: "prod-main") == []
