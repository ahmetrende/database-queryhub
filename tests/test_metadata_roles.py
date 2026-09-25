"""The metadata role split's policy and guards, without a database.

The real proof is tests/test_integration_metadata_roles.py, which CI runs as a
split runtime. These pin the decisions that the integration pass would only
show as a symptom: which privileges each kind of object ends up with, and the
guards that keep the split from being run the wrong way round.
"""
import importlib.util
from pathlib import Path

import pytest

from queryhub import metadata_roles as mr

ROOT = Path(__file__).resolve().parents[1]


def _load(script):
    spec = importlib.util.spec_from_file_location(script, ROOT / "scripts" / f"{script}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Cur:
    """Answers the three catalog reads runtime_grants makes."""

    def __init__(self, relations, db_ok=True):
        self.relations, self.db_ok, self._next = relations, db_ok, None

    def execute(self, sql, params=None):
        text = str(sql)
        if "current_database() AS d" in text:
            self._next = [{"d": "queryhub"}]
        elif "has_database_privilege" in text:
            ok = self.db_ok
            self._next = [{"c": ok, "t": ok, "u": ok}]
        else:
            self._next = self.relations

    def fetchone(self):
        return self._next[0]

    def fetchall(self):
        return self._next


def _row(name, rk="r", **held):
    keys = ("s", "i", "u", "d", "t", "f", "g")
    row = {"name": name, "rk": rk, **{k: held.get(k, False) for k in keys},
           "su": held.get("su") if rk == "S" else None}
    return row


def _text(stmts):
    return [s.as_string(None) for s in stmts]


ALL = dict(s=True, i=True, u=True, d=True, t=True, f=True, g=True)


def test_an_owner_turned_runtime_loses_everything_it_should_not_keep():
    """What a pre-split owner holds on audit_log, a view and the ledger."""
    cur = _Cur([_row("audit_log", **ALL), _row("audit_log_reportable", "v", **ALL),
                _row("schema_migrations", **ALL), _row("requests", **ALL)])
    out = _text(mr.runtime_grants(cur, "qh_runtime", "qh_owner"))
    assert 'REVOKE DELETE, REFERENCES, TRIGGER, TRUNCATE, UPDATE ON public."audit_log" FROM "qh_runtime"' in out
    assert 'REVOKE DELETE, INSERT, REFERENCES, TRIGGER, TRUNCATE, UPDATE ON public."audit_log_reportable" FROM "qh_runtime"' in out
    assert 'REVOKE DELETE, INSERT, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON public."schema_migrations" FROM "qh_runtime"' in out
    assert 'REVOKE REFERENCES, TRIGGER, TRUNCATE ON public."requests" FROM "qh_runtime"' in out
    assert not any("GRANT" in s and "audit_log" in s and "UPDATE" in s for s in out)


def test_a_runtime_holding_nothing_gets_exactly_the_policy():
    cur = _Cur([_row("audit_log"), _row("audit_log_reportable", "v"),
                _row("schema_migrations"), _row("requests"),
                _row("requests_id_seq", "S")], db_ok=False)
    out = _text(mr.runtime_grants(cur, "qh_runtime", "qh_owner"))
    assert out == [
        'GRANT CONNECT, TEMPORARY ON DATABASE "queryhub" TO "qh_runtime"',
        'GRANT USAGE ON SCHEMA public TO "qh_runtime"',
        'GRANT INSERT, SELECT ON public."audit_log" TO "qh_runtime"',
        'GRANT SELECT ON public."audit_log_reportable" TO "qh_runtime"',
        'GRANT DELETE, INSERT, SELECT, UPDATE ON public."requests" TO "qh_runtime"',
        'GRANT USAGE, SELECT ON SEQUENCE public."requests_id_seq" TO "qh_runtime"',
    ]


def test_nothing_drifted_means_nothing_to_run():
    cur = _Cur([_row("audit_log", s=True, i=True),
                _row("audit_log_reportable", "v", s=True),
                _row("requests", s=True, i=True, u=True, d=True),
                _row("requests_id_seq", "S", s=True, su=True)])
    assert mr.runtime_grants(cur, "qh_runtime", "qh_owner") == []


# --- guards ------------------------------------------------------------------


def test_the_split_refuses_to_run_as_the_runtime():
    split = _load("split_metadata_roles")
    with pytest.raises(SystemExit) as e:
        split.main(["--owner", "qh_owner", "--runtime", "qh_runtime",
                    "--admin-user", "qh_runtime"])
    assert e.value.code == 2


def test_the_split_needs_an_admin_login(monkeypatch):
    monkeypatch.delenv("PGUSER", raising=False)
    split = _load("split_metadata_roles")
    with pytest.raises(SystemExit):
        split.main(["--owner", "qh_owner", "--runtime", "qh_runtime", "--admin-user", ""])


def test_the_runner_needs_the_owner_role_with_a_migrator(monkeypatch):
    runner = _load("apply_migrations")
    monkeypatch.delenv("BOT_DB_MIGRATOR_USER", raising=False)
    assert runner._migrator() is None
    monkeypatch.setenv("BOT_DB_MIGRATOR_USER", "qh_migrator")
    monkeypatch.delenv("BOT_DB_OWNER_ROLE", raising=False)
    with pytest.raises(SystemExit):
        runner._migrator()
    monkeypatch.setenv("BOT_DB_OWNER_ROLE", "qh_owner")
    monkeypatch.setenv("BOT_DB_MIGRATOR_PASSWORD", "x")
    assert runner._migrator() == ("qh_migrator", "x", "qh_owner")


def test_the_container_drops_the_migrator_login_before_the_service_starts():
    text = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    migrate = text.index("python scripts/apply_migrations.py")
    unset = text.index("unset BOT_DB_MIGRATOR_USER BOT_DB_MIGRATOR_PASSWORD BOT_DB_OWNER_ROLE")
    assert migrate < unset < text.index('exec "$@"')


def test_ci_runs_the_integration_suite_as_a_split_runtime():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "scripts/split_metadata_roles.py --owner qh_owner" in ci
    assert "BOT_DB_USER: qh_runtime" in ci and "BOT_DB_MIGRATOR_USER: qh_migrator" in ci
