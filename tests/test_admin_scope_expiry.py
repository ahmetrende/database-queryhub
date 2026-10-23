"""An admin scope can end on a date (PLA-1592 U5).

`POST` and `PUT /admin/scopes` accept `expiresAt`. After that moment the scope
decides nothing. Under access_model_v2 the end reaches every reader through
`role_assignment.valid_until`, which the migration-109 mirror projects from
`admins.expires_at` (migration 148). Under the legacy model the `admins`
readers filter on the column themselves. Both are pinned here, and the real
SQL runs in tests/test_integration_expiry_rules.py.

Two rules are easy to get backwards, so they have tests of their own:
  * a body without `expiresAt` keeps an end that is stored and still ahead.
    A client that has never heard of the field must not turn a scope that ends
    on Friday into one that never ends by editing its tiers;
  * a super-admin never ends. The new model already refuses one with an end.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from queryhub import admins, auth_events, grants
from queryhub.web import admin as web_admin
from queryhub.web import ops_metrics
from queryhub.web import routes_admin as ra

CLAIMS = {"sub": "U0EXAMPLE001", "name": "Jordan Ray"}
SUBJECT = "U0EXAMPLE002"
AHEAD = datetime.now(timezone.utc) + timedelta(days=9)


class Cur:
    def __init__(self, live=None):
        self.live = live
        self.sql: list[tuple[str, tuple]] = []
        self._row = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        self._row = self.live if sql.lstrip().startswith("SELECT") else None

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
    st = {"cur": Cur(), "audit": []}
    monkeypatch.setattr(ra.admin, "require_admin", lambda claims, need: claims["sub"])
    monkeypatch.setattr(ra.admins, "is_super_admin", lambda uid: False)
    monkeypatch.setattr(ra, "_slack_profile", lambda uid: {"name": "Casey Lin"})
    monkeypatch.setattr(ra, "_target_id_of", lambda alias: {"prod-main": 5}.get(alias))
    monkeypatch.setattr(ra.db, "transaction", lambda: Txn(st["cur"]))
    monkeypatch.setattr(ra.audit, "log_in",
                        lambda cur, rid, uid, name, action, details=None:
                        st["audit"].append((action, details)))
    monkeypatch.setattr(ra.admin, "admin_block", lambda uid: {})
    return st


def _scope(**kw):
    kw.setdefault("admin", SUBJECT)
    kw.setdefault("role", "dba")
    kw.setdefault("canApprove", ["RO"])
    return ra.ScopeIn(**kw)


def _written_end(env):
    [(sql, params)] = [x for x in env["cur"].sql if x[0].startswith("INSERT INTO admins")]
    assert "expires_at = EXCLUDED.expires_at" in sql
    return params[-1]


def _status(fn, *a):
    with pytest.raises(HTTPException) as e:
        fn(*a)
    return e.value.status_code


# --- the write ---------------------------------------------------------------

def test_an_end_is_written_and_answered(env):
    out = ra.admin_create_scope(_scope(expiresAt=AHEAD.isoformat()), claims=CLAIMS)
    assert _written_end(env) == AHEAD
    assert out["expiresAt"] == AHEAD.isoformat()
    assert env["audit"][0][1]["expires_at"] == AHEAD.isoformat()


def test_a_body_without_the_field_keeps_a_live_end(env):
    """The tier edit from a client that predates the field."""
    env["cur"].live = {"expires_at": AHEAD}
    ra.admin_update_scope(SUBJECT, _scope(canApprove=["RO", "RW"]), claims=CLAIMS)
    assert _written_end(env) == AHEAD
    [(select, _params)] = [x for x in env["cur"].sql if x[0].lstrip().startswith("SELECT")]
    assert "enabled AND expires_at > NOW()" in select and "FOR UPDATE" in select


def test_null_clears_the_end(env):
    env["cur"].live = {"expires_at": AHEAD}
    ra.admin_update_scope(SUBJECT, _scope(expiresAt=None), claims=CLAIMS)
    assert _written_end(env) is None


def test_no_live_end_and_no_field_is_no_end(env):
    """A disabled or ended row's old end must not make a fresh scope dead on
    arrival."""
    ra.admin_create_scope(_scope(), claims=CLAIMS)
    assert _written_end(env) is None


def test_a_past_end_is_refused_before_anything_is_written(env):
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert _status(ra.admin_create_scope, _scope(expiresAt=past), CLAIMS) == 400
    assert _status(ra.admin_create_scope, _scope(expiresAt="Friday"), CLAIMS) == 400
    assert env["cur"].sql == [] and env["audit"] == []


@pytest.mark.parametrize("sent", [{"expiresAt": AHEAD.isoformat()}, {}])
def test_a_super_admin_never_ends(env, sent):
    """Sent with an end, or promoted from a DBA scope whose end is stored."""
    env["cur"].live = {"expires_at": AHEAD}
    assert _status(ra.admin_create_scope, _scope(role="super", **sent), CLAIMS) == 400
    assert not any(s.startswith("INSERT") for s, _ in env["cur"].sql)
    assert env["audit"] == []


def test_a_super_admin_with_null_is_permanent(env):
    env["cur"].live = {"expires_at": AHEAD}
    ra.admin_create_scope(_scope(role="super", expiresAt=None), claims=CLAIMS)
    assert _written_end(env) is None


def test_a_naive_time_is_read_as_utc(env):
    naive = (datetime.now(timezone.utc) + timedelta(days=3)).replace(tzinfo=None)
    ra.admin_create_scope(_scope(expiresAt=naive.isoformat()), claims=CLAIMS)
    assert _written_end(env) == naive.replace(tzinfo=timezone.utc)


# --- the legacy readers ------------------------------------------------------
# Under access_model_v2 every one of these delegates to access.py, which reads
# role_assignment.valid_until. With the switch off they read `admins` directly,
# so each one carries the filter itself.

LIVE = "(expires_at IS NULL OR expires_at > NOW())"


@pytest.mark.parametrize("fn", [admins.list_active, admins.is_admin,
                                admins._candidate_scope_rows, grants._authz_legacy,
                                ra._team_rows_legacy, ra._super_approvers])
def test_every_legacy_reader_skips_an_ended_scope(fn):
    assert LIVE in inspect.getsource(fn), fn.__name__


def test_a_scope_with_an_end_is_not_the_dba_level(monkeypatch):
    """The DBA level is a PERMANENT unscoped admin. An `admins` row with an end
    is temporary, as a role with a valid_until is in the new model."""
    asked = []
    monkeypatch.setattr(admins, "_v2", lambda: False)
    monkeypatch.setattr(admins.db, "fetch_one",
                        lambda sql, params=None: asked.append(sql) or None)
    assert admins.is_dba(SUBJECT) is False
    [sql] = asked
    assert "expires_at       IS NULL" in sql


def test_the_legacy_team_view_lists_no_ended_approver(monkeypatch):
    """The team's own grants carry an expiry filter too, so the check is on the
    query that reads `admins`, not on the function as a whole."""
    asked = []
    monkeypatch.setattr(ra.db, "fetch_all",
                        lambda sql, params=None: asked.append(sql) or [])
    ra._team_rows_legacy(7)
    [approvers] = [s for s in asked if "FROM admins" in s]
    assert LIVE in approvers


def test_list_active_says_temp_for_a_scope_with_an_end():
    src = inspect.getsource(admins.list_active)
    assert "CASE WHEN expires_at IS NULL THEN 'permanent'" in src
    # A temp grant still shows when the permanent row has ended.
    assert "AND (p.expires_at IS NULL OR p.expires_at > NOW())" in src


def test_the_legacy_person_view_reads_an_ended_scope_as_off():
    assert "(enabled AND (expires_at IS NULL OR expires_at > NOW())) AS enabled" in \
        inspect.getsource(ra._effective_access_legacy)


def test_the_admin_count_metric_skips_an_ended_scope():
    assert "AND (expires_at IS NULL OR expires_at > NOW())" in inspect.getsource(ops_metrics)


def test_a_legacy_super_admin_is_one_that_never_ends():
    """The CHECK keeps an end off a row with every scope NULL, so the legacy
    reader needs no extra filter, and the migration says so."""
    from pathlib import Path
    mig = (Path(__file__).resolve().parents[1] / "migrations"
           / "147_admin_scope_expiry.sql").read_text(encoding="utf-8")
    assert "admins_super_admin_never_ends" in mig


def test_the_scope_screen_folds_no_ended_row(monkeypatch):
    """Under either model, /me and GET /admin/scopes fold the `admins` row into
    the DBA's scope. An ended row grants nothing, so it must add nothing: here
    it would have widened RO on one connection to everything everywhere."""
    monkeypatch.setattr(web_admin.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(web_admin.admins, "is_super_admin", lambda uid: False)
    monkeypatch.setattr(web_admin.admins, "is_dba", lambda uid: False)
    monkeypatch.setattr(web_admin, "_alias", lambda tid: {5: "prod-main"}.get(tid))
    scope = {"permanent": {"max_tier": None, "scope_target_ids": None,
                           "enabled": True, "ended": True},
             "temp_grants": [{"max_tier": "ro", "scope_target_ids": [5]}]}
    monkeypatch.setattr(web_admin.admins, "get_scope", lambda uid: scope)
    blk = web_admin.admin_block(SUBJECT)
    assert blk["canApprove"] == ["RO"] and blk["connections"] == ["prod-main"]
    scope["permanent"]["ended"] = False
    blk = web_admin.admin_block(SUBJECT)
    assert blk["canApprove"] == ["RO", "RW", "DDL"] and blk["connections"] == ["*"]


def test_the_access_model_copy_keeps_a_scopes_end():
    """scripts/copy_access_model.py makes roles from `admins`. Before this it
    wrote every row, disabled ones included, as a role with no end. A re-run
    after a scope ended would have handed the authority back for good."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "scripts"
           / "copy_access_model.py").read_text(encoding="utf-8")
    body = re.search(r"\ndef copy_roles\(.*?(?=\ndef |\Z)", src, re.S).group(0)
    assert "WHERE enabled AND (expires_at IS NULL OR expires_at > now())" in body
    assert "max_tier, any_tier, valid_until, reason" in body
    assert 'a["expires_at"]' in body


def test_get_scope_says_when_the_row_has_ended():
    assert "AS ended" in inspect.getsource(admins.get_scope)


# --- the person is told ------------------------------------------------------

def _ev(op, old=None, new=None):
    return {"table_name": "admins", "op": op, "old_row": old, "new_row": new,
            "slack_user_id": SUBJECT, "team_id": None}


def test_a_scope_with_an_end_says_until_when():
    [(_u, text)] = auth_events.build_notifications(_ev("INSERT", new={
        "enabled": True, "added_by": "U0EXAMPLE001",
        "expires_at": "2026-10-31T17:00:00+00:00"}))
    assert "until `2026-10-31 17:00 UTC`" in text


def test_moving_the_end_is_a_change_the_person_hears_about():
    [(_u, text)] = auth_events.build_notifications(_ev(
        "UPDATE", old={"enabled": True, "expires_at": None},
        new={"enabled": True, "expires_at": "2026-10-31T17:00:00+00:00"}))
    assert "they now last until `2026-10-31 17:00 UTC`" in text
    assert auth_events.build_notifications(_ev(
        "UPDATE", old={"enabled": True}, new={"enabled": True})) == []
