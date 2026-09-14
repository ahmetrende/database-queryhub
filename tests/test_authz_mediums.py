"""Authorization and session fixes that were fail-open or profile-breaking.

1. Admin scope arrays: NULL means "every target" (a deliberate wildcard) but an
   EMPTY array means "no target". Truthiness collapsed the two, so a scope
   written `{}` — what a typo in the documented ARRAY[...] recipe produces —
   granted fleet-wide approval rights instead of none.
2. Local principals: migration 075 widened the identity CHECK to accept
   `local:<username>` so a Slack-less deployment could be administered, but the
   admin endpoints still validated the Slack id shape, making the admin panel
   unusable in exactly the profile where it is the only surface.
3. Refresh rotation: single-use tokens with reuse detection are right, but with
   no grace window the ordinary two-tab race is indistinguishable from a replay
   and the theft response revoked the session.
"""
import pytest

from queryhub import admins
from queryhub import config as cfg
from queryhub.web import routes_admin as ra
from queryhub.web import sessions

_REQ = {"required_tier": "ro", "target_server_id": 7, "requester_slack_id": "U0R"}


def _scope(**kw):
    base = {"max_tier": None, "scope_target_ids": None, "scope_team_ids": None}
    base.update(kw)
    return base


def test_null_target_scope_is_a_wildcard():
    assert admins._scope_admits(_scope(), _REQ) is True


def test_empty_target_scope_admits_nothing():
    # The fail-open case: `{}` used to behave like "all targets".
    assert admins._scope_admits(_scope(scope_target_ids=[]), _REQ) is False


def test_empty_team_scope_admits_nothing():
    assert admins._scope_admits(_scope(scope_team_ids=[]), _REQ) is False


def test_in_and_out_of_target_scope():
    assert admins._scope_admits(_scope(scope_target_ids=[7]), _REQ) is True
    assert admins._scope_admits(_scope(scope_target_ids=[9]), _REQ) is False


@pytest.mark.parametrize("pid,ok", [
    ("U012345678", True),
    ("W012345678", True),
    ("local:alice", True),
    ("local:a.b-c_1", True),
    ("local:", False),
    ("local:bad user", False),
    ("nope", False),
    ("", False),
    (None, False),
])
def test_admin_endpoints_accept_both_identity_namespaces(pid, ok):
    assert ra._valid_principal(pid) is ok


def test_refresh_grace_window_default_is_short_and_configurable(monkeypatch):
    monkeypatch.setattr(cfg, "get_int",
                        lambda k, d: 30 if k == "web_refresh_grace_seconds" else d)
    assert sessions._refresh_grace_seconds() == 30
    # 0 restores strict single-use for anyone who wants it.
    monkeypatch.setattr(cfg, "get_int",
                        lambda k, d: 0 if k == "web_refresh_grace_seconds" else d)
    assert sessions._refresh_grace_seconds() == 0


def test_rotation_inside_grace_window_returns_a_token_instead_of_revoking(monkeypatch):
    """The second tab must get a working token, not a dead session."""
    monkeypatch.setattr(sessions, "_refresh_grace_seconds", lambda: 30)
    calls = []

    class _Cur:
        def execute(self, sql, params=None):
            calls.append(sql)

        def fetchone(self):
            # 1st statement: the current-hash path misses (already rotated).
            # 2nd statement: the grace path hits.
            if len(calls) == 1:
                return None
            return {"id": 5, "slack_user_id": "U0A",
                    "auth_provider": "slack", "avatar_url": None}

    class _Txn:
        def __enter__(self): return _Cur()
        def __exit__(self, *a): return False

    monkeypatch.setattr(sessions.db, "transaction", lambda: _Txn())
    out = sessions.rotate_refresh("some-token")
    assert out is not None and "refresh_token" in out
    assert out.get("reuse") is None
    # The grace statement rotates; it must not be the revoke statement.
    assert "revoked_at = NOW()" not in calls[1]


def test_replay_outside_grace_window_still_revokes(monkeypatch):
    monkeypatch.setattr(sessions, "_refresh_grace_seconds", lambda: 30)
    calls = []

    class _Cur:
        def execute(self, sql, params=None):
            calls.append(sql)

        def fetchone(self):
            # current-hash path misses, grace path misses, revoke path hits
            if len(calls) < 3:
                return None
            return {"id": 5, "slack_user_id": "U0A"}

    class _Txn:
        def __enter__(self): return _Cur()
        def __exit__(self, *a): return False

    monkeypatch.setattr(sessions.db, "transaction", lambda: _Txn())
    assert sessions.rotate_refresh("stolen-token") == {"reuse": True}
    assert "revoked_at = NOW()" in calls[2]
