"""A connection picked from the catalogue is authoritative.

Free text stays — it is the only way to ask for a database QueryHub does not
have yet — but when the client says "this row, from the list you gave me", a
near-miss must be an error rather than a request nobody can resolve. That
silent fallback is what produced requests for servers that do not exist.
"""
import pytest

from fastapi import HTTPException

from queryhub.web import routes_requests as rr


class _T:
    def __init__(self, tid=7, alias="prod-main", enabled=True):
        self.id = tid
        self.alias = alias
        self.enabled = enabled


def _body(**kw):
    base = {"server": "prod-main", "database": "payments", "tier": "RO",
            "reason": "reconciliation report for finance"}
    base.update(kw)
    return rr.EndpointRequestIn(**base)


@pytest.fixture
def wired(monkeypatch):
    """Everything after resolution is stubbed: this file is about resolution."""
    state = {"created": None}

    def _wire(target=None, dbs=("payments",), control_plane=frozenset()):
        monkeypatch.setattr(rr.deps, "require_whitelisted", lambda c: None)
        monkeypatch.setattr(rr, "_target_by_alias",
                            lambda a: target if target and a == target.alias else None)
        monkeypatch.setattr(rr, "_target_by_id",
                            lambda v: target if target and v == str(target.id) else None)
        monkeypatch.setattr(rr, "_catalog_databases", lambda tid: list(dbs))
        monkeypatch.setattr(rr.grants, "control_plane_target_ids",
                            lambda: set(control_plane))

        def _create(**kw):
            state["created"] = kw
            return {"id": 42, **kw}
        monkeypatch.setattr(rr.access_requests, "create", _create)
        monkeypatch.setattr(rr.access_requests, "open_count_for", lambda u: 0)
        monkeypatch.setattr(rr, "_bot_client", lambda: None)
        import queryhub.admins as admins_mod
        monkeypatch.setattr(admins_mod, "is_admin", lambda u: False)
        monkeypatch.setattr(admins_mod, "list_active", lambda: [{"id": "UA"}])
        from queryhub.slack_app import access
        monkeypatch.setattr(access, "fan_out_admin_dms",
                            lambda *a, **k: "1787.000")
        return state
    return _wire


def test_a_picked_connection_resolves_to_the_target(wired):
    state = wired(target=_T())
    out = rr.endpoint_request(_body(connectionId="prod-main"),
                              claims={"sub": "U1", "name": "Dev"})
    assert out["status"] == "submitted"
    # Resolved: the request carries the target id, so approving it can write the
    # grant instead of an admin re-typing an alias.
    assert state["created"]["target_server_id"] == 7
    assert state["created"]["database_name"] == "payments"
    # A resolved request needs no free-text discriminator in attempted_query.
    assert state["created"]["attempted_query"] is None


def test_a_picked_connection_can_be_named_by_id(wired):
    state = wired(target=_T())
    rr.endpoint_request(_body(connectionId="7"), claims={"sub": "U1"})
    assert state["created"]["target_server_id"] == 7


def test_an_unknown_pick_is_refused_not_downgraded_to_free_text(wired):
    wired(target=None)
    with pytest.raises(HTTPException) as e:
        rr.endpoint_request(_body(connectionId="prod-gone"), claims={"sub": "U1"})
    assert e.value.status_code == 404


def test_a_disabled_pick_is_refused(wired):
    wired(target=_T(enabled=False))
    with pytest.raises(HTTPException) as e:
        rr.endpoint_request(_body(connectionId="prod-main"), claims={"sub": "U1"})
    assert e.value.status_code == 404


def test_a_database_not_on_the_connection_is_refused(wired):
    wired(target=_T(), dbs=("payments",))
    with pytest.raises(HTTPException) as e:
        rr.endpoint_request(_body(connectionId="prod-main", database="ledgr"),
                            claims={"sub": "U1"})
    assert e.value.status_code == 400


def test_the_control_plane_cannot_be_requested(wired):
    wired(target=_T(tid=1, alias="bot-control"), control_plane={1})
    with pytest.raises(HTTPException) as e:
        rr.endpoint_request(_body(server="bot-control", database=None,
                                  connectionId="bot-control"),
                            claims={"sub": "U1"})
    assert e.value.status_code == 400


def test_free_text_still_works_for_something_qh_does_not_have(wired):
    state = wired(target=None)
    rr.endpoint_request(_body(server="brand-new-host", database="ledger"),
                        claims={"sub": "U1"})
    assert state["created"]["target_server_id"] is None
    # The discriminator keeps two different unknown servers from deduping into
    # one request.
    assert "brand-new-host" in state["created"]["attempted_query"]
