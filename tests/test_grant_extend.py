"""Moving a grant's end date in place (PLA-1592 U1).

`PATCH /admin/grants/{gid}` with `{expiresAt, reason}`. The ids, the authority
and the write paths are the revoke's: a person's grant is a
`user_target_grants` row (the migration-109 mirror carries the new end into
`access_grant`), and a team's grant is its `access_grant` rows under
access_model_v2, or its `team_target_grants` row otherwise.

The decision this file pins: a grant that has already ended is NOT extended.
Every reader treats an ended grant as no access, and an ended personal grant
even blocks the team's access on that server (access.py, rule 2). Moving its
end would give access again through a path that records neither who granted it
nor when. That is POST /admin/grants, so the PATCH answers 409 and points
there. Against real SQL: tests/test_integration_expiry_rules.py.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from queryhub import auth_events
from queryhub.web import routes_admin as ra

CLAIMS = {"sub": "U0EXAMPLE001", "name": "Jordan Ray"}
NOW = datetime.now(timezone.utc)
AHEAD = (NOW + timedelta(days=8)).replace(microsecond=0)
SOON = (NOW + timedelta(days=2)).replace(microsecond=0)
GONE = (NOW - timedelta(days=1)).replace(microsecond=0)


class Cur:
    def __init__(self):
        self.one = None
        self.many: list[dict] = []
        self.sql: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class Txn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self.cur

    def __exit__(self, *exc):
        return False


@pytest.fixture
def env(monkeypatch):
    st = {"cur": Cur(), "audit": [], "v2": True, "listed": [], "control": set()}
    monkeypatch.setattr(ra.admin, "require_admin", lambda claims, need: claims["sub"])
    monkeypatch.setattr(ra.db, "transaction", lambda: Txn(st["cur"]))
    monkeypatch.setattr(ra.teams_mod, "use_v2", lambda: st["v2"])
    monkeypatch.setattr(ra.grants, "control_plane_target_ids", lambda: st["control"])
    monkeypatch.setattr(ra.audit, "log_in",
                        lambda cur, rid, uid, name, action, details=None:
                        st["audit"].append((action, details)))
    monkeypatch.setattr(ra, "admin_grants", lambda claims: {"grants": st["listed"]})
    return st


def _patch(gid, **body):
    return ra.admin_extend_grant(gid, ra.GrantPatchIn(**body), claims=CLAIMS)


def _status(gid, **body):
    with pytest.raises(HTTPException) as e:
        _patch(gid, **body)
    return e.value.status_code


def _updates(env):
    return [(s, p) for s, p in env["cur"].sql if s.lstrip().startswith("UPDATE")]


# --- a person's grant --------------------------------------------------------

def test_a_persons_grant_moves_in_the_legacy_table(env):
    env["cur"].one = {"ends": SOON, "ended": False}
    env["listed"] = [{"id": "u:U0EXAMPLE002:53", "expiresAt": AHEAD.isoformat()}]
    out = _patch("u:U0EXAMPLE002:53", expiresAt=AHEAD.isoformat(), reason="month-end close")
    assert out == {"id": "u:U0EXAMPLE002:53", "expiresAt": AHEAD.isoformat()}
    [(sql, params)] = _updates(env)
    assert sql.startswith("UPDATE user_target_grants SET expires_at = %s")
    assert params == (AHEAD, "U0EXAMPLE002", 53)
    [(action, d)] = env["audit"]
    assert action == "grant_expiry_changed"
    assert d["grantee"] == "U0EXAMPLE002" and d["target_id"] == 53
    assert d["from"] == SOON.isoformat() and d["expires_at"] == AHEAD.isoformat()
    assert d["reason"] == "month-end close"
    assert d["note"] == (f"Extended u:U0EXAMPLE002:53 from {SOON:%d %b %Y %H:%M} UTC "
                         f"to {AHEAD:%d %b %Y %H:%M} UTC")


def test_null_removes_the_end(env):
    env["cur"].one = {"ends": SOON, "ended": False}
    _patch("u:U0EXAMPLE002:53", expiresAt=None)
    [(_sql, params)] = _updates(env)
    assert params[0] is None
    assert env["audit"][0][1]["note"].endswith("to no end")


def test_an_earlier_end_is_accepted_and_called_what_it_is(env):
    env["cur"].one = {"ends": AHEAD, "ended": False}
    _patch("u:U0EXAMPLE002:53", expiresAt=SOON.isoformat())
    assert env["audit"][0][1]["note"].startswith("Shortened ")


def test_an_ended_grant_is_refused_and_untouched(env):
    env["cur"].one = {"ends": GONE, "ended": True}
    assert _status("u:U0EXAMPLE002:53", expiresAt=AHEAD.isoformat()) == 409
    assert _updates(env) == [] and env["audit"] == []


def test_no_live_grant_is_404(env):
    assert _status("u:U0EXAMPLE002:53", expiresAt=AHEAD.isoformat()) == 404


def test_the_select_reads_only_a_live_row_and_locks_it(env):
    env["cur"].one = {"ends": None, "ended": False}
    _patch("u:U0EXAMPLE002:53", expiresAt=AHEAD.isoformat())
    select = env["cur"].sql[0][0]
    assert "revoked_at IS NULL FOR UPDATE" in select
    assert "(expires_at IS NOT NULL AND expires_at <= NOW()) AS ended" in select


# --- the body ----------------------------------------------------------------

def test_the_end_must_be_sent(env):
    assert _status("u:U0EXAMPLE002:53", reason="no date") == 400


def test_a_past_or_unreadable_end_is_refused_before_anything_is_read(env):
    assert _status("u:U0EXAMPLE002:53", expiresAt=GONE.isoformat()) == 400
    assert _status("u:U0EXAMPLE002:53", expiresAt="next week") == 400
    assert env["cur"].sql == []


def test_a_bad_id_is_400(env):
    for gid in ("x", "u:U0EXAMPLE002", "t:team-a:53", "g:1:2"):
        assert _status(gid, expiresAt=AHEAD.isoformat()) == 400


def test_a_field_this_endpoint_does_not_know_is_refused():
    """A client that sends a tier believes it changed one."""
    with pytest.raises(Exception):
        ra.GrantPatchIn(expiresAt=None, tier="rw")


def test_the_control_plane_is_refused_as_on_create(env):
    env["control"] = {53}
    assert _status("u:U0EXAMPLE002:53", expiresAt=AHEAD.isoformat()) == 403
    assert env["cur"].sql == []


def test_same_authority_as_the_revoke(monkeypatch, env):
    seen = []
    monkeypatch.setattr(ra.admin, "require_admin",
                        lambda claims, need: seen.append(need) or claims["sub"])
    env["cur"].one = {"ends": None, "ended": False}
    _patch("u:U0EXAMPLE002:53", expiresAt=AHEAD.isoformat())
    assert seen == ["access"]
    assert 'require_admin(claims, "access")' in inspect.getsource(ra.admin_delete_grant)


# --- a team's grant ----------------------------------------------------------

def test_a_teams_rows_move_together_under_the_new_model(env):
    env["cur"].many = [{"id": 101, "ends": SOON, "mirrored_from": None, "ended": False},
                       {"id": 102, "ends": SOON, "mirrored_from": None, "ended": False}]
    _patch("t:7:53", expiresAt=AHEAD.isoformat())
    [(sql, params)] = _updates(env)
    assert sql.startswith("UPDATE access_grant SET valid_until = %s WHERE id = ANY(%s)")
    assert params == (AHEAD, [101, 102])
    select = env["cur"].sql[0][0]
    assert "NOT auto_approve" in select and "revoked_at IS NULL AND NOT is_deleted" in select
    [(action, d)] = env["audit"]
    assert action == "team_grant_expiry_changed" and d["team_id"] == 7


def test_one_ended_database_is_an_ended_grant(env):
    env["cur"].many = [{"id": 101, "ends": AHEAD, "mirrored_from": None, "ended": False},
                       {"id": 102, "ends": GONE, "mirrored_from": None, "ended": True}]
    assert _status("t:7:53", expiresAt=AHEAD.isoformat()) == 409
    assert _updates(env) == []


def test_a_mirrored_team_row_is_changed_at_its_source(env):
    env["cur"].many = [{"id": 101, "ends": None, "mirrored_from": "team_target_grants",
                        "ended": False}]
    assert _status("t:7:53", expiresAt=AHEAD.isoformat()) == 409
    assert _updates(env) == []


def test_no_team_rows_is_404(env):
    assert _status("t:7:53", expiresAt=AHEAD.isoformat()) == 404


def test_the_legacy_team_table_without_the_new_model(env):
    env["v2"] = False
    env["cur"].one = {"ends": None, "ended": False}
    _patch("t:7:53", expiresAt=AHEAD.isoformat())
    [(sql, params)] = _updates(env)
    assert sql.startswith("UPDATE team_target_grants SET expires_at = %s")
    assert params == (AHEAD, 7, 53)


def test_a_legacy_team_grant_that_ended_is_refused(env):
    env["v2"] = False
    env["cur"].one = {"ends": GONE, "ended": True}
    assert _status("t:7:53", expiresAt=AHEAD.isoformat()) == 409


# --- the grantee is told -----------------------------------------------------

def _ev(table, old, new, user="U0EXAMPLE002", team=None):
    return {"table_name": table, "op": "UPDATE", "old_row": old, "new_row": new,
            "slack_user_id": user, "team_id": team}


def test_a_person_hears_the_new_end():
    base = {"target_server_id": 53, "mode": "ro", "revoked_at": None}
    [(uid, text)] = auth_events.build_notifications(
        _ev("user_target_grants", {**base, "expires_at": "2026-10-25T14:00:00+00:00"},
            {**base, "expires_at": "2026-10-30T14:00:00+00:00"}),
        alias_of=lambda t: "prod-main")
    assert uid == "U0EXAMPLE002"
    assert "it now lasts until `2026-10-30 14:00 UTC`" in text


def test_every_member_hears_a_legacy_team_grants_new_end():
    base = {"target_server_id": 53, "mode": "ro", "revoked_at": None}
    notes = auth_events.build_notifications(
        _ev("team_target_grants", {**base, "expires_at": None},
            {**base, "expires_at": "2026-10-30T14:00:00+00:00"}, user=None, team=3),
        alias_of=lambda t: "prod-main",
        team_info=lambda tid, table=None: ("Team Alpha", ["U0EXAMPLE004", "U0EXAMPLE005"]))
    assert [u for u, _ in notes] == ["U0EXAMPLE004", "U0EXAMPLE005"]
    assert all("it now lasts until" in t for _, t in notes)
