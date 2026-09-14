"""Break glass: the order of the three moves is the whole design.

Each step alone leaves a way back in. NOLOGIN without a password change is
undone by anyone who can ALTER ROLE. A password change without NOLOGIN is
undone by whoever holds the next leak. Terminating first is worst of all: the
client reconnects with credentials that still work, and the operator watching
the session count sees it drop and come back.

So: shut the door, change the lock, then clear the building.
"""
import importlib.util
import json
import os
import re
import subprocess
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
       "host": "db.example.internal", "port": 5432, "database": "postgres",
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
    base = dict(apply=True, terminate=True, admin_user=None, plan=None,
                alias=None, include_disabled=False, dump_plan=None,
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


# --- the copy that runs somewhere else --------------------------------------
#
# The incident may be that THIS host is the problem, so a laptop copy is worth
# having. But the fleet list lives in the metadata DB, and a second machine
# that needs BOT_DB_* and the master key just to learn which servers exist
# means spreading the bot's secrets around to prepare for the day they leak.
# The plan file breaks that: hosts and login names travel, secrets do not.

def test_the_exported_plan_carries_no_credentials(tmp_path):
    out = tmp_path / "fleet.json"
    _mod.dump_plan([ROW], str(out), {"sslmode": "verify-full"})
    text = out.read_text()
    assert "password" not in text.lower() and "secret" not in text.lower()
    plan = json.loads(text)
    assert plan["targets"][0]["host"] == "db.example.internal"
    assert plan["targets"][0]["username"] == "queryhub_ro"
    assert plan["ssl"] == {"sslmode": "verify-full"}


def test_the_plan_file_is_not_world_readable(tmp_path):
    # It names every production host in the fleet.
    out = tmp_path / "fleet.json"
    _mod.dump_plan([ROW], str(out), {})
    assert oct(out.stat().st_mode)[-3:] == "600"


def test_a_plan_run_reads_the_file_instead_of_the_database(tmp_path, monkeypatch):
    out = tmp_path / "fleet.json"
    _mod.dump_plan([ROW, dict(ROW, id=4, alias="svc-prod-billing")], str(out), {})
    monkeypatch.setattr(_mod, "db", None)          # no metadata DB at all
    _mod._load_plan.cache_clear()
    rows = _mod.fleet(_args(plan=str(out)))
    assert [r["alias"] for r in rows] == ["svc-prod-orders", "svc-prod-billing"]


def test_a_plan_run_still_honours_the_alias_filter(tmp_path):
    out = tmp_path / "fleet.json"
    _mod.dump_plan([ROW, dict(ROW, id=4, alias="svc-prod-billing")], str(out), {})
    _mod._load_plan.cache_clear()
    rows = _mod.fleet(_args(plan=str(out), alias="*billing"))
    assert [r["alias"] for r in rows] == ["svc-prod-billing"]


def _cli(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["breakglass_lockout.py", *argv])
    with pytest.raises(SystemExit) as e:
        _mod.main()
    return e.value.code


def test_a_plan_without_a_superuser_is_refused(monkeypatch, tmp_path, capsys):
    # The file has no credentials in it by design, so there is nothing to
    # connect with unless the operator brings their own.
    code = _cli(monkeypatch, "--plan", str(tmp_path / "f.json"), "--apply")
    assert code == 2
    assert "--admin-user is required" in capsys.readouterr().err


def test_a_plan_run_cannot_flip_the_kill_switch(monkeypatch, tmp_path, capsys):
    # It writes to the metadata DB, which is exactly what this mode does not open.
    code = _cli(monkeypatch, "--plan", str(tmp_path / "f.json"),
                "--admin-user", "root", "--kill-switch")
    assert code == 2
    assert "/sql kill on" in capsys.readouterr().err


def test_without_a_plan_and_without_the_bot_modules_it_says_so(monkeypatch, capsys):
    monkeypatch.setattr(_mod, "db", None)
    code = _cli(monkeypatch, "--apply")
    assert code == 2
    err = capsys.readouterr().err
    assert "--plan" in err and "bot host" in err


def test_it_starts_on_a_machine_with_no_bot_environment(tmp_path):
    """The laptop case, end to end.

    `config.py` resolves BOT_DB_* at import time and raises RuntimeError when
    they are missing - so importing the bot's modules is itself the thing that
    fails on a second machine, before any of the --plan handling runs. A
    subprocess with the environment stripped is the only honest check.
    """
    plan = tmp_path / "fleet.json"
    _mod.dump_plan([ROW], str(plan), {})
    env = {k: v for k, v in os.environ.items() if not k.startswith("BOT_DB_")}
    env["MASTER_KEY_PATH"] = str(tmp_path / "no-such-key")
    out = subprocess.run(
        [sys.executable, str(Path(_mod.__file__)), "--plan", str(plan),
         "--admin-user", "someone", "--alias", "matches-nothing-*"],
        capture_output=True, text=True, env=env, timeout=60)
    assert "Traceback" not in out.stderr, out.stderr
    assert out.returncode == 2 and "no targets matched" in out.stderr


# --- the PowerShell twin ----------------------------------------------------
#
# The operator's laptop is Windows, and asking for Python plus psycopg there to
# prepare for an incident is one dependency too many. breakglass_lockout.ps1
# does the same three moves through psql. It cannot be imported, so what is
# checked here is the handful of properties that would fail silently if they
# ever changed.

PS1 = (Path(__file__).resolve().parent.parent / "scripts" / "breakglass_lockout.ps1")


def test_the_powershell_twin_ships_next_to_the_python_one():
    assert PS1.exists()


def test_it_shuts_the_door_before_changing_the_lock_before_clearing_the_room():
    src = PS1.read_text(encoding="utf-8")
    body = src[src.index("function Build-Sql"):]
    nologin = body.index("NOLOGIN")
    passwd = body.index("PASSWORD %L")
    kill = body.index("pg_terminate_backend")
    assert nologin < passwd < kill


def test_the_password_alphabet_is_exactly_sixty_four_characters():
    """The mapping is `byte % 64`, so 64 characters means no modulo bias.

    Any other length quietly makes the first N characters more likely, which
    is the kind of thing that is never noticed because the passwords are never
    looked at.
    """
    src = PS1.read_text(encoding="utf-8")
    alphabet = re.search(r"Alphabet = '([^']+)'", src).group(1)
    assert len(alphabet) == 64
    assert len(set(alphabet)) == 64
    assert not (set(alphabet) & set("'\"\;"))     # nothing that needs escaping


def test_the_generated_password_never_reaches_the_command_line():
    """psql gets the SQL on stdin. With -c it would be in the process list and
    in the shell's history, which is where a random password stops being one."""
    src = PS1.read_text(encoding="utf-8")
    args = src[src.index("function Invoke-Psql"):src.index("function Build-Sql")]
    assert "'-c'" not in args and '"-c"' not in args
    assert "$sql | & $psql" in args


def test_it_never_asks_for_queryhubs_own_credentials():
    # The whole point of the laptop copy: the operator's superuser, and no
    # master key or BOT_DB_* anywhere near it.
    src = PS1.read_text(encoding="utf-8")
    assert "BOT_DB" not in src and "master.key" not in src
