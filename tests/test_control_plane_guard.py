"""The bot's own metadata database is never grantable — on any path.

The control-plane target used to be the constant `1`, which was true of the
author's install and nothing else: on a fresh install target 1 is whatever the
operator registered first, so the documented promise held nowhere. A DDL grant
there means rewriting `audit_log` and `admins` — editing the record of what you
did.

The check also moved into grants.grant(), because the Slack modal enforced it
and the web admin endpoint did not.
"""
import pytest

from queryhub import config as cfg
from queryhub import grants


def _env(host="db.internal", port=5432, name="queryhub"):
    return type("E", (), {"bot_db_host": host, "bot_db_port": port,
                          "bot_db_name": name})()


def test_detects_by_host_and_database_not_by_id(monkeypatch):
    monkeypatch.setattr(cfg, "get_setting", lambda k, d=None: d)
    monkeypatch.setattr(cfg, "ENV", _env())
    # The metadata DB is target 7 here — the old constant would have missed it
    # and happily granted access.
    monkeypatch.setattr(grants.db, "fetch_all", lambda *a, **k: [
        {"id": 7, "default_database": "queryhub"},
    ])
    assert grants.control_plane_target_ids() == {7}


def test_same_host_other_database_still_counts(monkeypatch):
    # A grant on a same-host target with unrestricted database scope reaches the
    # metadata DB anyway, so this errs toward refusing.
    monkeypatch.setattr(cfg, "get_setting", lambda k, d=None: d)
    monkeypatch.setattr(cfg, "ENV", _env())
    monkeypatch.setattr(grants.db, "fetch_all", lambda *a, **k: [
        {"id": 4, "default_database": "something_else"},
    ])
    assert grants.control_plane_target_ids() == {4}


def test_config_override_wins(monkeypatch):
    monkeypatch.setattr(
        cfg, "get_setting",
        lambda k, d=None: "3, 9" if k == "control_plane_target_ids" else d)
    monkeypatch.setattr(cfg, "ENV", _env())
    assert grants.control_plane_target_ids() == {3, 9}


def test_detection_failure_falls_back_closed(monkeypatch):
    monkeypatch.setattr(cfg, "get_setting", lambda k, d=None: d)
    monkeypatch.setattr(cfg, "ENV", _env())

    def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(grants.db, "fetch_all", _boom)
    # Falls back to the legacy id rather than returning "nothing is protected".
    assert grants.control_plane_target_ids() == {grants.CONTROL_PLANE_TARGET_ID}


def test_grant_refuses_the_control_plane_target(monkeypatch):
    monkeypatch.setattr(grants, "control_plane_target_ids", lambda: {7})
    with pytest.raises(PermissionError, match="control-plane"):
        grants.grant(granter_id="U0A", granter_name="a", grantee_id="U0B",
                     grantee_profile={}, target_id=7, mode="ddl",
                     databases=None, reason="nope")


def test_grant_allows_an_ordinary_target(monkeypatch):
    # Guard must not block normal grants: stop right after the check.
    monkeypatch.setattr(grants, "control_plane_target_ids", lambda: {7})
    reached = {}

    class _Txn:
        def __enter__(self):
            reached["txn"] = True
            raise RuntimeError("stop after the guard")

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(grants.db, "transaction", lambda: _Txn())
    with pytest.raises(RuntimeError, match="stop after the guard"):
        grants.grant(granter_id="U0A", granter_name="a", grantee_id="U0B",
                     grantee_profile={}, target_id=42, mode="ro",
                     databases=None, reason="fine")
    assert reached.get("txn") is True
