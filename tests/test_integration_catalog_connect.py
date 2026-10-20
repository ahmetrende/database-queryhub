"""The listing leaves out a database that refuses the login, on a real server.

`has_database_privilege` is the server's own answer to the question "may this
login open that database". The unit tests in tests/test_catalog_closed_databases.py
check what the code does with that answer. Only a real server shows that the
answer is the right one: the listing leaves out a database with CONNECT revoked
from PUBLIC, and an explicit grant to the login puts it back.

The tests make one throwaway login and three throwaway databases on the server
in QH_TARGET_ADMIN_DSN, a superuser conninfo. Without it, the control login
makes them if it is a superuser. The tests drop all of them at the end.
"""
import os
import secrets

import psycopg
import pytest
from psycopg import sql

from queryhub import schema_catalog, targets
from queryhub import config as cfg
from queryhub.config import ENV

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("QH_RUN_INTEGRATION"),
        reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
]

_LOGIN = "qh_it_catalog_ro"
_OPEN = "qh_it_catalog_open"
_CLOSED = "qh_it_catalog_closed"
_GRANTED = "qh_it_catalog_granted"
_DATABASES = (_OPEN, _CLOSED, _GRANTED)


def _admin():
    dsn = os.environ.get("QH_TARGET_ADMIN_DSN")
    if dsn:
        return psycopg.connect(dsn, autocommit=True, connect_timeout=10)
    conn = psycopg.connect(
        host=ENV.bot_db_host, port=ENV.bot_db_port, dbname=ENV.bot_db_name,
        user=ENV.bot_db_user, password=ENV.bot_db_password, autocommit=True,
        connect_timeout=10, **ENV.bot_db_ssl_kwargs())
    row = conn.execute(
        "SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()
    if not (row and row[0]):
        conn.close()
        pytest.skip("the control login cannot make roles: set "
                    "QH_TARGET_ADMIN_DSN to a superuser conninfo")
    return conn


def _drop(admin) -> None:
    for name in _DATABASES:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
            sql.Identifier(name)))
    admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(_LOGIN)))


@pytest.fixture(scope="module")
def server():
    admin = _admin()
    password = secrets.token_urlsafe(18)
    _drop(admin)
    admin.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER PASSWORD {}").format(
        sql.Identifier(_LOGIN), sql.Literal(password)))
    for name in _DATABASES:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    for name in (_CLOSED, _GRANTED):
        admin.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
            sql.Identifier(name)))
    admin.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
        sql.Identifier(_GRANTED), sql.Identifier(_LOGIN)))
    try:
        yield {"host": admin.info.host, "port": admin.info.port,
               "dbname": admin.info.dbname, "password": password}
    finally:
        _drop(admin)
        admin.close()


@pytest.fixture(autouse=True)
def plain_tcp(monkeypatch):
    """The test server is a local container without TLS. The fleet default,
    `require`, is not what these tests are about."""
    monkeypatch.setattr(cfg, "target_ssl_kwargs",
                        lambda host=None: {"sslmode": "prefer"})


def _target(server) -> targets.TargetServer:
    return targets.TargetServer(
        id=7, alias="integration-catalog", host=server["host"],
        port=server["port"], default_database=server["dbname"], username=_LOGIN,
        enabled=False, notes=None, engine="postgres", engine_config=None)


def test_the_closed_database_really_refuses_the_login(server):
    """The premise. Without it the next test would pass for the wrong reason."""
    with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
        psycopg.connect(host=server["host"], port=server["port"], dbname=_CLOSED,
                        user=_LOGIN, password=server["password"],
                        connect_timeout=10, sslmode="prefer")


def test_the_listing_names_the_databases_the_login_may_open(server):
    listed = schema_catalog.list_target_databases(_target(server), server["password"])
    ours = [name for name in listed if name.startswith("qh_it_catalog_")]
    assert ours == sorted([_OPEN, _GRANTED]), (
        "an open database and a granted one are listed, the closed one is not")
    # Every name that is listed can be opened by this login.
    for name in ours:
        psycopg.connect(host=server["host"], port=server["port"], dbname=name,
                        user=_LOGIN, password=server["password"],
                        connect_timeout=10, sslmode="prefer").close()
