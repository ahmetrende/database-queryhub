"""Target TLS is decided per host, so a fleet can verify one cloud at a time.

`target_ssl_mode` was one switch for every PostgreSQL target. A fleet spread
over two clouds has two certificate authorities, so turning on verify-full with
one CA file would have cut off every server the other CA signed. Measured on
2026-09-24: every AWS server in the fleet verifies against the AWS RDS bundle,
and no Huawei server does, because they are signed by Huawei's own CA.

Two host-glob lists now set the mode per server, and the same lists decide
SQL Server's certificate check. The CA file travels only with a verifying mode:
libpq reads `require` plus a root file as verify-ca, so the AWS bundle handed to
a Huawei server on `require` would have failed every connection to it.
"""
import ast
import contextlib
import re
from pathlib import Path

import pytest

from queryhub import config as cfg
from queryhub import mssql_exec, replicas, targets
from queryhub.web import config_admin

ROOT = Path(__file__).resolve().parents[1]
AWS = "prod-main.abc123.eu-central-1.rds.amazonaws.com"
OTHER = "beta-cache.pg.example.myhuaweicloud.com"


@pytest.fixture
def settings(monkeypatch):
    vals: dict = {}
    monkeypatch.setattr(cfg, "get_setting", lambda k, d=None: vals.get(k, d))
    return vals


def test_nothing_set_keeps_require_everywhere(settings):
    assert cfg.target_ssl_kwargs(AWS) == {"sslmode": "require"}
    assert cfg.target_ssl_kwargs() == {"sslmode": "require"}


def test_a_ca_file_alone_does_not_turn_require_into_verify_ca(settings):
    settings["target_ssl_rootcert"] = "/ca/bundle.pem"
    assert cfg.target_ssl_kwargs(OTHER) == {"sslmode": "require"}


def test_a_listed_host_verifies_against_the_ca_file(settings):
    settings.update(target_ssl_verify_hosts="*.rds.amazonaws.com",
                    target_ssl_rootcert="/ca/bundle.pem")
    assert cfg.target_ssl_kwargs(AWS) == {"sslmode": "verify-full",
                                          "sslrootcert": "/ca/bundle.pem"}
    assert cfg.target_ssl_kwargs(OTHER) == {"sslmode": "require"}


def test_host_matching_ignores_case_and_takes_commas_or_spaces(settings):
    settings["target_ssl_verify_hosts"] = "other.example.com,  *.RDS.amazonaws.com"
    assert cfg.target_ssl_kwargs(AWS.upper())["sslmode"] == "verify-full"


def test_exempt_beats_the_verify_list(settings):
    settings.update(target_ssl_verify_hosts="*.rds.amazonaws.com",
                    target_ssl_verify_exempt_hosts=AWS,
                    target_ssl_rootcert="/ca/bundle.pem")
    assert cfg.target_ssl_kwargs(AWS) == {"sslmode": "require"}


def test_exempt_beats_a_fleet_wide_verifying_mode(settings):
    settings.update(target_ssl_mode="verify-full",
                    target_ssl_verify_exempt_hosts="*.myhuaweicloud.com",
                    target_ssl_rootcert="/ca/bundle.pem")
    assert cfg.target_ssl_kwargs(OTHER) == {"sslmode": "require"}
    assert cfg.target_ssl_kwargs(AWS) == {"sslmode": "verify-full",
                                          "sslrootcert": "/ca/bundle.pem"}


def test_exempt_never_raises_a_weaker_fleet_wide_mode(settings):
    """Exempt means "do not verify". It is not a way to turn prefer into require."""
    settings.update(target_ssl_mode="prefer", target_ssl_verify_exempt_hosts=OTHER)
    assert cfg.target_ssl_kwargs(OTHER) == {"sslmode": "prefer"}


def test_no_host_gets_the_fleet_wide_mode(settings):
    settings.update(target_ssl_verify_hosts="*", target_ssl_rootcert="/ca/bundle.pem")
    assert cfg.target_ssl_kwargs() == {"sslmode": "require"}
    assert cfg.target_ssl_kwargs("") == {"sslmode": "require"}


# --- SQL Server reads the same lists -------------------------------------------


def test_sql_server_trust_follows_the_lists_then_its_own_default(settings):
    settings["mssql_trust_server_cert"] = "on"
    assert mssql_exec.trusts_server_cert("192.0.2.5") is True
    settings["target_ssl_verify_hosts"] = "192.0.2.5"
    assert mssql_exec.trusts_server_cert("192.0.2.5") is False
    settings["target_ssl_verify_exempt_hosts"] = "192.0.2.*"
    assert mssql_exec.trusts_server_cert("192.0.2.5") is True


# --- every connection passes the host it goes to --------------------------------


def _py_files():
    for base in ("src", "scripts"):
        yield from (ROOT / base).rglob("*.py")


def test_every_call_names_the_host_it_connects_to():
    """A call without a host silently gets the fleet-wide mode, which is the
    downgrade this change exists to remove: two connections once hardcoded
    `require` and ignored an operator's verify-full the same way."""
    bare = []
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "target_ssl_kwargs"
                    and not node.args and not node.keywords):
                bare.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    # The break-glass script's fleet-wide default is only a fallback for a plan
    # file written before plans carried a mode per target.
    assert bare == ["scripts/breakglass_lockout.py:" + str(_fallback_line())]


def _fallback_line():
    text = (ROOT / "scripts" / "breakglass_lockout.py").read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), 1):
        if "cfg.target_ssl_kwargs())" in line:
            return i
    raise AssertionError("fallback call not found")


def test_no_target_connection_hardcodes_an_sslmode():
    hits = []
    for path in _py_files():
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"""\bsslmode\s*=\s*["']""", line):
                hits.append(f"{path.relative_to(ROOT)}:{i}")
    assert hits == []


def test_a_replica_probe_asks_for_each_hosts_own_mode(monkeypatch):
    """The primary and its replica can sit under different names, or behind
    different CAs. One shared mode for both was what the probe had."""
    seen = {}
    monkeypatch.setattr(replicas.cfg, "target_ssl_kwargs",
                        lambda host=None: {"sslmode": f"mode-for-{host}"})
    monkeypatch.setattr(replicas.cfg, "get_int", lambda k, d=None: d)
    monkeypatch.setattr(replicas.log, "info", lambda *a, **k: None)

    @contextlib.contextmanager
    def connect(host=None, port=None, **kw):
        seen[host] = kw["sslmode"]
        if host == "replica.example.test":
            raise replicas.psycopg.OperationalError("stop here")

        class Cur:
            def execute(self, sql, params=None):
                pass

            def fetchone(self):
                return ("3B4F/FA6EE000",)

        class Conn:
            @contextlib.contextmanager
            def cursor(self):
                yield Cur()
        yield Conn()
    monkeypatch.setattr(replicas.psycopg, "connect", connect)

    class Primary:
        host, port, default_database = "primary.example.test", 5432, "app"
    replicas._probe(Primary(), {"alias": "prod-main-replica", "host": "replica.example.test",
                                "port": 5432}, "u", "p")
    assert seen == {"primary.example.test": "mode-for-primary.example.test",
                    "replica.example.test": "mode-for-replica.example.test"}


# --- what the settings screen will accept ---------------------------------------


def test_the_mode_must_be_a_word_libpq_knows():
    assert config_admin._coerce("verify-full", "str", "require", key="target_ssl_mode") == "verify-full"
    assert config_admin._coerce("verify_full", "str", "require", key="target_ssl_mode") is None


def test_verifying_needs_a_readable_ca_file(tmp_path):
    hold = config_admin._tls_settings_hold
    assert hold({"target_ssl_mode": "require"}) is None
    assert "must name a CA file" in hold({"target_ssl_verify_hosts": "*.rds.amazonaws.com"})
    assert "not a readable file" in hold({"target_ssl_mode": "verify-full",
                                          "target_ssl_rootcert": str(tmp_path / "missing.pem")})
    ca = tmp_path / "bundle.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    assert hold({"target_ssl_verify_hosts": "*", "target_ssl_rootcert": str(ca)}) is None


def test_the_ca_check_runs_only_when_a_tls_key_changes():
    """An install already in a bad state must still be able to save an
    unrelated setting."""
    class Cur:
        def __init__(self):
            self.writes = []

        def execute(self, sql, params=None):
            if sql.startswith("UPDATE"):
                self.writes.append(params)

        def fetchall(self):
            return [{"key": "target_ssl_verify_hosts", "value": "*"},
                    {"key": "target_ssl_rootcert", "value": ""},
                    {"key": "max_rows", "value": "1000"}]
    cur = Cur()
    assert config_admin.apply_config({"max_rows": "2000"}, cur)[0]["key"] == "max_rows"
    with pytest.raises(ValueError):
        config_admin.apply_config({"target_ssl_verify_hosts": "*.example.com"}, Cur())


def test_the_four_keys_are_seeded_so_the_screen_shows_them():
    text = "".join(p.read_text(encoding="utf-8")
                   for p in sorted((ROOT / "migrations").glob("*.sql")))
    for key in ("target_ssl_mode", "target_ssl_rootcert", "target_ssl_verify_hosts",
                "target_ssl_verify_exempt_hosts"):
        assert f"('{key}'," in text, key
        assert key in config_admin._TYPES


# --- the startup line --------------------------------------------------------------


def test_the_posture_counts_what_each_engine_actually_does(monkeypatch, settings):
    settings.update(target_ssl_verify_hosts="*.rds.amazonaws.com",
                    target_ssl_rootcert="/ca/bundle.pem", mssql_trust_server_cert="on")
    monkeypatch.setattr(targets.db, "fetch_all", lambda sql, *a: [
        {"host": AWS, "engine": "postgres"}, {"host": OTHER, "engine": "postgres"},
        {"host": "192.0.2.5", "engine": "mssql"}, {"host": "ch.example.com", "engine": "clickhouse"},
        {"host": "athena", "engine": "athena"}])
    assert targets.tls_posture() == {"verified": 2, "unverified": 2}


def test_the_startup_line_never_stops_a_process(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("metadata DB unreachable")
    monkeypatch.setattr(targets.db, "pool_ready", lambda: True)
    monkeypatch.setattr(targets.db, "fetch_all", broken)
    targets.log_tls_posture()


def test_the_startup_line_skips_a_pool_that_failed_at_boot(monkeypatch):
    monkeypatch.setattr(targets.db, "pool_ready", lambda: False)
    monkeypatch.setattr(targets, "tls_posture",
                        lambda: pytest.fail("read the DB with no pool"))
    targets.log_tls_posture()


# --- the metadata DB has settings of its own -------------------------------------


def test_metadata_db_tls_comes_from_its_own_settings():
    """Not PGSSLROOTCERT: libpq applies that to every connection naming no root
    file, so each target's `require` would become verify-ca against it."""
    import dataclasses

    from queryhub import db
    env = dataclasses.replace(cfg.ENV, bot_db_sslmode="verify-full",
                              bot_db_sslrootcert="/ca/bundle.pem")
    assert env.bot_db_ssl_kwargs() == {"sslmode": "verify-full",
                                       "sslrootcert": "/ca/bundle.pem"}
    bare = dataclasses.replace(cfg.ENV, bot_db_sslmode="", bot_db_sslrootcert="")
    assert bare.bot_db_ssl_kwargs() == {}
    # The file, not inspect: conftest swaps init_pool for a refuser.
    assert "**ENV.bot_db_ssl_kwargs()" in Path(db.__file__).read_text(encoding="utf-8")
