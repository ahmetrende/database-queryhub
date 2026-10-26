"""Two routes must not tell a whitelisted person which connection names exist.

The request-access forms hide the connection list on purpose. These two older
routes still answered differently for a name that exists and one that does not:

* `POST /auto-approve-requests` gave 404 for a missing alias, and for an alias
  that exists it went on: 403 "you don't have access", or 400 for a bad tier
  that only an existing name ever reached.
* `POST /saved` echoed the alias of a connection the caller cannot use, with
  its state (`no_access`, `retired`), and saved an unbound query for a name
  that does not exist.

The rule now: a name the caller cannot use answers exactly like a name that
does not exist, and the reachability check comes before every other rule.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from queryhub import auto_approve_requests, targets
from queryhub.web import routes_data as rd
from queryhub.web import routes_requests as rr

SECRET = SimpleNamespace(id=7, alias="prod-secret", enabled=True)
OPEN = SimpleNamespace(id=8, alias="prod-open", enabled=True)
RETIRED = SimpleNamespace(id=9, alias="prod-retired", enabled=False)
FLEET = {t.alias: t for t in (SECRET, OPEN, RETIRED)}
USABLE = {OPEN.id}            # what the caller may use


# ---------------------------------------------------------------------------
# POST /auto-approve-requests
# ---------------------------------------------------------------------------

@pytest.fixture
def window(monkeypatch):
    submitted = []
    monkeypatch.setattr(targets, "by_alias", lambda a: FLEET.get(a))
    monkeypatch.setattr(rr.teams, "can_use_target", lambda uid, tid: tid in USABLE)

    def submit_window(**kw):
        submitted.append(kw)
        return ({"id": 1, "status": "pending", "database_name": None,
                 "max_tier": "ro", "window_minutes": 60}, FLEET["prod-open"])
    monkeypatch.setattr(auto_approve_requests, "submit_window", submit_window)
    monkeypatch.setattr(auto_approve_requests, "notify_admins", lambda *a, **k: 1)
    import queryhub.web.routes_queries as rq
    monkeypatch.setattr(rq, "_bot_client", lambda: None)
    return submitted


def _ask(alias, **kw):
    body = rr.WindowRequestIn(connectionId=alias, reason="reconciliation work",
                              **kw)
    try:
        return rr.create_window_request(body, claims={"sub": "U1", "name": "dev"})
    except HTTPException as e:
        return (e.status_code, e.detail)


@pytest.mark.parametrize("kw", [{}, {"tier": "ddl"}, {"tier": "nonsense"},
                                {"windowMinutes": 7}])
def test_a_name_the_caller_cannot_use_answers_like_a_name_that_does_not_exist(
        window, kw):
    """Same status, same body, for every kind of request. The bad tier and the
    bad window are the ones that used to reach only an existing name."""
    missing = _ask("no-such-server", **kw)
    secret = _ask("prod-secret", **kw)
    retired = _ask("prod-retired", **kw)
    assert missing[0] == 404
    assert missing == secret == retired
    assert window == []            # none of them reached the rules


def test_a_usable_name_still_goes_through_the_rules(window):
    out = _ask("prod-open")
    assert out["status"] == "pending"
    assert [c["target_id"] for c in window] == [OPEN.id]


def test_a_usable_name_with_a_bad_tier_still_gets_the_tier_error(window, monkeypatch):
    def refuse(**kw):
        raise auto_approve_requests.WindowRequestRefused("tier", "Pick RO or RW.")
    monkeypatch.setattr(auto_approve_requests, "submit_window", refuse)
    status, _detail = _ask("prod-open", tier="nonsense")
    assert status == 400


# ---------------------------------------------------------------------------
# POST /saved
# ---------------------------------------------------------------------------

@pytest.fixture
def saved(monkeypatch):
    monkeypatch.setattr(rd.deps, "require_whitelisted", lambda c: None)
    monkeypatch.setattr(rd, "_target_by_alias", lambda a: FLEET.get(a))
    monkeypatch.setattr(rd, "_is_admin", lambda uid: uid == "UADMIN")
    monkeypatch.setattr(rd.teams, "effective_grant_for_user",
                        lambda uid, tid: {"mode": "ro"} if tid in USABLE else None)

    def add(**kw):
        return {"id": 5, "query": kw["query"], "label": kw["label"],
                "target_server_id": kw["target_server_id"],
                "database_name": kw["database_name"]}
    monkeypatch.setattr(rd.favorites, "add", add)
    by_id = {t.id: t for t in FLEET.values()}
    monkeypatch.setattr(rd, "_alias_of",
                        lambda tid: by_id[tid].alias if tid in by_id else None)

    def resolver(uid):
        def state_of(tid):
            if tid is None:
                return "none"
            t = by_id[tid]
            if not t.enabled:
                return "retired"
            return "ok" if tid in USABLE else "no_access"
        return state_of
    monkeypatch.setattr(rd, "_conn_state_resolver", resolver)


def _save(alias, uid="U1"):
    body = rd.SavedIn(sql="select 1", connectionId=alias)
    return rd.saved_create(body, claims={"sub": uid})


def test_a_saved_query_on_a_name_the_caller_cannot_use_looks_like_one_on_no_name(saved):
    missing = _save("no-such-server")
    secret = _save("prod-secret")
    retired = _save("prod-retired")
    assert missing["connectionId"] is None
    assert missing["connectionState"] == "none"
    assert missing == secret == retired


def test_a_saved_query_on_a_usable_connection_still_binds(saved):
    out = _save("prod-open")
    assert out["connectionId"] == "prod-open"
    assert out["connectionState"] == "ok"


def test_an_admin_still_binds_to_any_connection(saved):
    assert _save("prod-secret", uid="UADMIN")["connectionId"] == "prod-secret"
    assert _save("prod-retired", uid="UADMIN")["connectionId"] == "prod-retired"
