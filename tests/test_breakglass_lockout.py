"""Break glass: the order of the three moves is the whole design.

Each step alone leaves a way back in. NOLOGIN without a password change is
undone by anyone who can ALTER ROLE. A password change without NOLOGIN is
undone by whoever holds the next leak. Terminating first is worst of all: the
client reconnects with credentials that still work, and the operator watching
the session count sees it drop and come back.

So: shut the door, change the lock, then clear the building.
"""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_spec = importlib.util.spec_from_file_location(
    "breakglass_lockout",
    Path(__file__).resolve().parent.parent / "scripts" / "breakglass_lockout.py")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["breakglass_lockout"] = _mod
_spec.loader.exec_module(_mod)

ROW = {"id": 3, "alias": "svc-prod-orders", "engine": "postgres", "enabled": True,
       "super_ddl_role": "queryhub_superadmin", "username": "queryhub_ro",
       "username_rw": "queryhub_rw", "username_ddl": "queryhub_ddl"}


class _Cur:
    """A cursor that records every statement and answers the two questions the
    script asks: does the role exist, and how many sessions does it have."""
    def __init__(self, existing=("queryhub_ro", "queryhub_rw",
                                 "queryhub_ddl"), sessions=4):
        self.sql = []
        self.existing = set(existing)
        self.sessions = sessions
        self._answer = None

    def execute(self, q, params=None):
        text = q.as_string(None) if hasattr(q, "as_string") else str(q)
        self.sql.append(text)
        if "FROM pg_roles" in text:
            self._answer = (1,) if params[0] in self.existing else None
        elif "pg_stat_activity" in text:
            self._answer = (self.sessions,)
        else:
            self._answer = None

    def fetchone(self):
        return self._answer

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _args(**kw):
    base = dict(apply=True, terminate=True, admin_user=None,
                admin_password_env="PGPASSWORD", timeout=10)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def cur(monkeypatch):
    c = _Cur()
    monkeypatch.setattr(_mod, "_connect", lambda row, args: _Conn(c))
    return c


def _positions(cur, needle):
    return [i for i, s in enumerate(cur.sql) if needle in s]


# --- the ordering -----------------------------------------------------------

def test_the_door_is_shut_before_the_lock_is_changed(cur):
    _mod.lock_postgres(ROW, _args())
    for name in ("queryhub_ro", "queryhub_rw", "queryhub_ddl"):
        nologin = _positions(cur, f'"{name}" NOLOGIN')[0]
        passwd = _positions(cur, f'"{name}" PASSWORD')[0]
        assert nologin < passwd


def test_sessions_are_terminated_last(cur):
    _mod.lock_postgres(ROW, _args())
    terminate = _positions(cur, "pg_terminate_backend")[0]
    assert terminate > max(_positions(cur, "NOLOGIN"))
    assert terminate > max(_positions(cur, "PASSWORD"))


def test_the_script_does_not_kill_itself(cur):
    _mod.lock_postgres(ROW, _args())
    kill = [s for s in cur.sql if "pg_terminate_backend" in s][0]
    assert "pid <> pg_backend_pid()" in kill


# --- what it touches --------------------------------------------------------

def test_all_three_tiers_are_locked(cur):
    out = _mod.lock_postgres(ROW, _args())
    assert out["locked"] == ["queryhub_ro", "queryhub_rw",
                             "queryhub_ddl"]
    assert out["terminated"] == 4
    assert out["error"] is None


def test_a_login_absent_from_the_cluster_is_skipped(monkeypatch):
    # Not every target has all three. Locking a role that is not there is an
    # error that stops the rest of the target.
    c = _Cur(existing=("queryhub_ro",))
    monkeypatch.setattr(_mod, "_connect", lambda row, args: _Conn(c))
    out = _mod.lock_postgres(ROW, _args())
    assert out["locked"] == ["queryhub_ro"]
    assert not any("queryhub_rw" in s for s in c.sql if "ALTER ROLE" in s)


def test_only_the_logins_the_row_names_are_touched(monkeypatch):
    row = dict(ROW, username="app_reader", username_rw=None, username_ddl=None)
    c = _Cur(existing=("app_reader", "queryhub_rw"))
    monkeypatch.setattr(_mod, "_connect", lambda r, a: _Conn(c))
    out = _mod.lock_postgres(row, _args())
    assert out["locked"] == ["app_reader"]
    assert not any("queryhub_rw" in s for s in c.sql)


def test_the_elevated_role_is_assumed_when_the_target_has_one(cur):
    _mod.lock_postgres(ROW, _args())
    assert cur.sql[0] == 'SET ROLE "queryhub_superadmin"'


def test_no_role_is_assumed_when_connecting_as_a_named_superuser(cur):
    # --admin-user is the mode for "I do not trust the bot's credentials".
    # Using them to SET ROLE would defeat the point.
    _mod.lock_postgres(ROW, _args(admin_user="postgres"))
    assert not any("SET ROLE" in s for s in cur.sql)


def test_a_target_naming_no_logins_reports_rather_than_passing_silently():
    row = dict(ROW, username=None, username_rw=None, username_ddl="")
    out = _mod.lock_postgres(row, _args())
    assert out["locked"] == [] and out["error"]


def test_a_failure_is_reported_per_target_not_raised(monkeypatch):
    def boom(row, args):
        raise RuntimeError("connection failed: timeout\nsecond line")
    monkeypatch.setattr(_mod, "_connect", boom)
    out = _mod.lock_postgres(ROW, _args())
    # One line, so 43 targets still print as a table the operator can read.
    assert out["error"] == "connection failed: timeout"
    assert out["locked"] == []


# --- the dry run ------------------------------------------------------------

def test_a_dry_run_changes_nothing_but_still_counts_the_sessions(cur):
    out = _mod.lock_postgres(ROW, _args(apply=False))
    assert not any("ALTER ROLE" in s for s in cur.sql)
    assert not any("pg_terminate_backend" in s for s in cur.sql)
    assert out["locked"] == ["queryhub_ro", "queryhub_rw",
                             "queryhub_ddl"]
    assert out["terminated"] == 4          # what --apply would have killed


def test_no_terminate_leaves_the_sessions_alone(cur):
    out = _mod.lock_postgres(ROW, _args(terminate=False))
    assert any("NOLOGIN" in s for s in cur.sql)
    assert not any("pg_stat_activity" in s for s in cur.sql)
    assert out["terminated"] == 0


# --- the passwords ----------------------------------------------------------

def test_each_login_gets_its_own_password(cur):
    _mod.lock_postgres(ROW, _args())
    pw = [s for s in cur.sql if "PASSWORD" in s]
    assert len(pw) == 3 and len(set(pw)) == 3


def test_a_password_is_never_returned_or_logged(cur):
    out = _mod.lock_postgres(ROW, _args())
    # The result is what gets printed. It must not carry a credential.
    assert "password" not in repr(out).lower()


def test_a_password_is_long_and_not_repeated():
    a, b = _mod.new_password(), _mod.new_password()
    assert len(a) == 40 and a != b


# --- the fleet --------------------------------------------------------------

def test_logins_are_de_duplicated_in_tier_order():
    row = dict(ROW, username="one", username_rw="one", username_ddl="two")
    assert _mod.logins_of(row) == ["one", "two"]
