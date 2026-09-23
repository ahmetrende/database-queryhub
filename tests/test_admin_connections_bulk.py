"""Several connections at once: enable, disable, or set one credential on all.

Onboarding a batch of servers that share a credential meant typing it once per
server. The bulk route plans every connection with the single-connection
route's own rules and writes nothing unless every one of them can take the
change: a half-applied bulk change leaves a fleet in a state nobody chose.
"""
import pytest
from fastapi import HTTPException

from queryhub.web import admin as web_admin
from queryhub.web import routes_admin as ra

SUPER = {"sub": "U0EXAMPLE001", "name": "Example Super"}


def _row(alias, enabled=False, ro_real=False):
    return {"id": {"alpha": 1, "beta": 2, "gamma": 3}[alias], "alias": alias,
            "host": f"{alias}.db.example.internal", "port": 5432,
            "default_database": "ledger", "engine": "postgres", "notes": None,
            "enabled": enabled, "tags": {},
            "credentials": {"ro": {"configured": ro_real, "placeholder": not ro_real},
                            "rw": {"configured": False, "placeholder": False},
                            "ddl": {"configured": False, "placeholder": False}}}


class Txn:
    def __init__(self, st): self.st = st
    def __enter__(self): self.st["txns"] += 1; return self.st
    def __exit__(self, *e): return False


@pytest.fixture
def env(monkeypatch):
    st = {"rows": {"alpha": _row("alpha"), "beta": _row("beta", ro_real=True),
                   "gamma": _row("gamma", ro_real=True)},
          "applied": [], "audit": [], "txns": 0, "super": True}
    monkeypatch.setattr(web_admin.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(web_admin.admins, "is_super_admin", lambda uid: st["super"])

    def require_row(name):
        if name not in st["rows"]:
            raise ra.deps._error(404, "not_found", f"No connection '{name}'.")
        return st["rows"][name]
    monkeypatch.setattr(ra, "_require_target_row", require_row)
    monkeypatch.setattr(ra, "_apply_connection_update",
                        lambda cur, row, ch, cr, uid, nm: st["applied"].append((row["alias"], ch, sorted(cr))))
    monkeypatch.setattr(ra.db, "transaction", lambda: Txn(st))
    monkeypatch.setattr(ra.audit, "log_in", lambda cur, rid, uid, name, action, details=None:
                        st["audit"].append((action, details)))
    monkeypatch.setattr(ra.targets, "admin_row", lambda tid: {"id": tid})
    monkeypatch.setattr(ra, "_connection_payload", lambda row, *a: {"id": row["id"]})
    monkeypatch.setattr(ra, "_clean_credentials",
                        lambda c: {m: (v.username, v.password) for m, v in (c or {}).items()})
    return st


def _body(**kw):
    return ra.BulkConnectionsIn(**kw)


def test_disabling_several_writes_each_once_in_one_transaction(env):
    for r in env["rows"].values():
        r["enabled"] = True
    out = ra.admin_bulk_update_connections(_body(connections=["alpha", "beta", "gamma"],
                                                 enabled=False), claims=SUPER)
    assert out["applied"] is True
    assert [a for a, _c, _k in env["applied"]] == ["alpha", "beta", "gamma"]
    assert env["txns"] == 1
    assert env["audit"][-1][0] == "connections_bulk_updated"


def test_one_refusal_writes_nothing_and_names_every_refused_connection(env):
    """alpha has no real read-only credential, so it cannot be enabled."""
    with pytest.raises(HTTPException) as e:
        ra.admin_bulk_update_connections(_body(connections=["alpha", "beta", "nope"],
                                               enabled=True), claims=SUPER)
    assert e.value.status_code == 409
    refused = {r["connection"] for r in e.value.detail["refused"]}
    assert refused == {"alpha", "nope"}
    assert env["applied"] == [] and env["txns"] == 0


def test_credentials_in_the_same_request_let_a_placeholder_be_enabled(env):
    creds = {"ro": ra.CredentialIn(username="reader", password="example-only")}
    out = ra.admin_bulk_update_connections(_body(connections=["alpha", "beta"], enabled=True,
                                                 credentials=creds), claims=SUPER)
    assert out["applied"] is True
    assert all(k == ["ro"] for _a, _c, k in env["applied"])


def test_a_dry_run_plans_and_writes_nothing(env):
    out = ra.admin_bulk_update_connections(_body(connections=["beta"], enabled=True,
                                                 dryRun=True), claims=SUPER)
    assert out == {"applied": False, "results": [
        {"connection": "beta", "changes": ["enabled"], "unchanged": False}]}
    assert env["txns"] == 0


def test_a_request_that_changes_nothing_is_refused(env):
    with pytest.raises(HTTPException) as e:
        ra.admin_bulk_update_connections(_body(connections=["beta"]), claims=SUPER)
    assert e.value.status_code == 400


def test_the_size_is_capped(env):
    with pytest.raises(HTTPException) as e:
        ra.admin_bulk_update_connections(
            _body(connections=[f"c{i}" for i in range(ra._BULK_CONNECTIONS_MAX + 1)],
                  enabled=False), claims=SUPER)
    assert e.value.status_code == 400


def test_duplicates_are_planned_once(env):
    for r in env["rows"].values():
        r["enabled"] = True
    ra.admin_bulk_update_connections(_body(connections=["beta", "beta", " beta "],
                                           enabled=False), claims=SUPER)
    assert [a for a, _c, _k in env["applied"]] == ["beta"]


def test_a_scoped_admin_is_refused(env):
    env["super"] = False
    with pytest.raises(HTTPException) as e:
        ra.admin_bulk_update_connections(_body(connections=["beta"], enabled=False), claims=SUPER)
    assert e.value.status_code == 403


def test_the_bulk_route_takes_only_fields_that_are_the_same_across_servers():
    assert set(ra.BulkConnectionsIn.model_fields) == {"connections", "enabled", "credentials", "dryRun"}


def test_one_rulebook_for_one_and_for_many():
    import inspect
    assert "_plan_connection_update(row, body)" in inspect.getsource(ra.admin_update_connection)
    assert "_plan_connection_update(row, patch)" in inspect.getsource(ra.admin_bulk_update_connections)
