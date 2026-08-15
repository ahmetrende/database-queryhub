"""web/sessions.py — token rotation, theft response, and revocation.

These are the functions that decide whether a browser stays signed in, and
their untested parts were exactly the branches with security consequences:
single-use rotation, the grace window that stops a second tab from looking like
theft, the theft response itself, and each revoke path (sign-out, admin kill,
per-user kill).

The SQL is what enforces every one of these properties — `revoked_at IS NULL`,
`expires_at > NOW()`, `prev_refresh_hash = %s`, the grace interval — so the
tests assert on the statements issued and the rows returned, driven by a fake
cursor. No DB, no clock dependency.
"""
import pytest

from dba_slack_bot.web import sessions


class _Cur:
    """Returns a queued row per execute(), and records the SQL."""

    def __init__(self, rows):
        self.queue = list(rows)
        self.executed = []
        self.rowcount = 0
        self._last = None

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.executed.append((flat, params))
        self._last = self.queue.pop(0) if self.queue else None
        self.rowcount = 1 if self._last else 0

    def fetchone(self):
        return self._last


class _Txn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self.cur

    def __exit__(self, *a):
        return False


SESSION_ROW = {"id": 3, "slack_user_id": "U0DEV", "auth_provider": "slack",
               "avatar_url": None}


def _wire(monkeypatch, rows, grace=10):
    cur = _Cur(rows)
    monkeypatch.setattr(sessions.db, "transaction", lambda: _Txn(cur))
    monkeypatch.setattr(sessions, "_refresh_grace_seconds", lambda: grace)
    return cur


# ------------------------------------------------------------ happy rotation


def test_rotation_issues_a_new_token_and_keeps_the_old_as_prev(monkeypatch):
    """Single-use: the presented token stops being current, and is remembered
    as `prev` so a replay can be recognised."""
    cur = _wire(monkeypatch, [SESSION_ROW])
    out = sessions.rotate_refresh("tok-1")

    assert out is not None and out["id"] == 3
    assert out["refresh_token"] and out["refresh_token"] != "tok-1"

    sql, params = cur.executed[0]
    assert "SET refresh_hash = %s, prev_refresh_hash = %s" in sql
    # Only a live, unexpired session may rotate.
    assert "revoked_at IS NULL" in sql
    assert "expires_at > NOW()" in sql
    # The new token is stored hashed, never in the clear.
    assert params[0] == sessions._hash(out["refresh_token"])
    assert params[1] == sessions._hash("tok-1")
    assert out["refresh_token"] not in str(params)


def test_the_new_token_is_not_the_old_one_and_is_long(monkeypatch):
    _wire(monkeypatch, [SESSION_ROW])
    out = sessions.rotate_refresh("tok-1")
    assert len(out["refresh_token"]) >= 40


# ------------------------------------------------------------- grace window


def test_a_superseded_token_inside_the_grace_window_rotates_again(monkeypatch):
    """Two tabs refresh at once, or a response is lost and the client retries.
    Both present a token that was just superseded. Revoking there would sign
    the user out everywhere for opening a second tab."""
    cur = _wire(monkeypatch, [None, SESSION_ROW], grace=10)
    out = sessions.rotate_refresh("tok-1")

    assert out is not None and "reuse" not in out
    assert out["id"] == 3
    sql, params = cur.executed[1]
    assert "WHERE prev_refresh_hash = %s" in sql
    assert "last_refresh_at > NOW() - make_interval(secs => %s)" in sql
    assert params[-1] == 10
    # The presented token stays as prev, so the sibling tab's in-flight retry
    # also lands inside the window rather than tripping theft detection.
    assert params[1] == sessions._hash("tok-1")


def test_grace_window_is_skipped_when_disabled(monkeypatch):
    """grace=0 means strict single-use: straight to the theft check."""
    cur = _wire(monkeypatch, [None, None], grace=0)
    assert sessions.rotate_refresh("tok-1") is None
    joined = " | ".join(s for s, _ in cur.executed)
    assert "make_interval(secs =>" not in joined
    assert "refresh token reuse detected" in joined


# ---------------------------------------------------------- theft response


def test_a_replay_outside_the_grace_window_revokes_the_session(monkeypatch):
    """A long-superseded token on a live session is a replay: the whole session
    dies, and the caller is told so it can clear cookies and force a re-login."""
    cur = _wire(monkeypatch, [None, None, {"id": 3, "slack_user_id": "U0DEV"}])
    out = sessions.rotate_refresh("tok-old")

    assert out == {"reuse": True}
    sql, params = cur.executed[2]
    assert "SET revoked_at = NOW()" in sql
    assert "refresh token reuse detected" in sql
    assert "WHERE prev_refresh_hash = %s" in sql
    assert params[0] == sessions._hash("tok-old")


def test_an_unknown_token_is_simply_rejected(monkeypatch):
    """No match anywhere: not theft, just a stale or forged cookie. Returning
    None (rather than {'reuse': True}) matters — it must not revoke a session
    that a random token happened to be presented against."""
    cur = _wire(monkeypatch, [None, None, None])
    assert sessions.rotate_refresh("nonsense") is None
    assert len(cur.executed) == 3


def test_reuse_on_an_already_revoked_session_does_not_re_revoke(monkeypatch):
    """The theft UPDATE is guarded by `revoked_at IS NULL`, so a replay against
    a dead session returns None instead of reporting a fresh theft."""
    cur = _wire(monkeypatch, [None, None, None])
    assert sessions.rotate_refresh("tok-old") is None
    assert "revoked_at IS NULL" in cur.executed[2][0]


# -------------------------------------------------------------- revocation


def test_sign_out_revokes_by_refresh_token(monkeypatch):
    """Sign-out must kill the server-side session. The access JWT may already
    be expired, so the refresh cookie is the only usable handle."""
    cur = _wire(monkeypatch, [{"id": 3}])
    assert sessions.revoke_by_refresh("tok-1", "sign out") is True
    sql, params = cur.executed[0]
    assert "SET revoked_at = NOW()" in sql
    assert params == ("sign out", sessions._hash("tok-1"))


def test_sign_out_with_an_unknown_token_reports_false(monkeypatch):
    _wire(monkeypatch, [None])
    assert sessions.revoke_by_refresh("nope", "sign out") is False


def test_revoke_user_kills_every_session_and_reports_the_count(monkeypatch):
    """The offboarding kill switch. The count is what the admin UI shows, so a
    silent 0 would read as success."""
    class _CountCur(_Cur):
        """rowcount is what revoke_user returns, so it has to be set by the
        UPDATE rather than by the row queue."""
        def execute(self, sql, params=None):
            super().execute(sql, params)
            self.rowcount = 4

    cur = _CountCur([{"id": 1}])
    monkeypatch.setattr(sessions.db, "transaction", lambda: _Txn(cur))

    assert sessions.revoke_user("U0GONE", "offboarded") == 4
    sql, params = cur.executed[0]
    assert "WHERE slack_user_id = %s AND revoked_at IS NULL" in sql
    assert params == ("offboarded", "U0GONE")


def test_revoke_session_targets_one_id(monkeypatch):
    calls = []
    monkeypatch.setattr(sessions.db, "execute",
                        lambda sql, params=None: calls.append((" ".join(sql.split()), params)))
    sessions.revoke_session(9, "admin action")
    sql, params = calls[0]
    assert "WHERE id = %s AND revoked_at IS NULL" in sql
    assert params == ("admin action", 9)


# ------------------------------------------------------------- liveness


@pytest.mark.parametrize("row,alive", [({"ok": 1}, True), (None, False)])
def test_session_alive_is_the_per_request_revocation_check(monkeypatch, row, alive):
    seen = {}

    def _fetch(sql, params=None):
        seen["sql"] = " ".join(sql.split())
        seen["params"] = params
        return row
    monkeypatch.setattr(sessions.db, "fetch_one", _fetch)

    assert sessions.session_alive(3) is alive
    assert "revoked_at IS NULL" in seen["sql"]
    assert "expires_at > NOW()" in seen["sql"]


def test_create_session_stores_only_a_hash(monkeypatch):
    """The refresh token exists in the clear exactly once: in the response to
    the browser. The row keeps a hash, so a metadata-DB read cannot mint
    sessions."""
    seen = {}

    def _insert(sql, params=None):
        seen["sql"] = " ".join(sql.split())
        seen["params"] = params
        return {"id": 11}
    monkeypatch.setattr(sessions.db, "insert_returning", _insert)
    monkeypatch.setattr(sessions, "refresh_ttl_hours", lambda: 72)

    sid, token = sessions.create_session("U0DEV", provider="slack",
                                         user_agent="ua")
    assert sid == 11
    assert token not in str(seen["params"])
    assert sessions._hash(token) in seen["params"]


def test_long_user_agent_is_truncated_not_rejected(monkeypatch):
    """A 4KB UA header must not blow up the INSERT."""
    monkeypatch.setattr(sessions.db, "insert_returning",
                        lambda sql, params=None: {"id": 1})
    monkeypatch.setattr(sessions, "refresh_ttl_hours", lambda: 72)
    sid, _ = sessions.create_session("U0DEV", provider="local",
                                     user_agent="x" * 5000)
    assert sid == 1
