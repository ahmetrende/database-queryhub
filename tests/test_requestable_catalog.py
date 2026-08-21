"""What a person can ask for access to — and what the list must never offer.

Before this, the "request access" modal was two free-text fields. A requester's
connection list only contains targets they already hold a grant on, so they were
typing the name of a server they had never been shown: the request arrived as
prose, an admin resolved `svc-prod-notifcation` by hand, and only then granted.

`GET /requestable` answers "what is missing" instead. The exclusions are the
interesting part — a list that offers something the backend would refuse is
worse than one that leaves it out.
"""
import pytest

from queryhub.web import routes_requests as rr


class _T:
    def __init__(self, tid, alias, engine="postgres", env="production",
                 enabled=True):
        self.id = tid
        self.alias = alias
        self.engine = engine
        self.env = env
        self.enabled = enabled


@pytest.fixture
def catalog(monkeypatch):
    """Patch the three facts the endpoint reads: the fleet, the user's grants,
    and each target's catalogued databases."""
    def _wire(targets_list, grants_by_target, dbs_by_target,
              control_plane=frozenset()):
        monkeypatch.setattr(rr.targets, "list_enabled", lambda: targets_list)
        monkeypatch.setattr(rr.teams, "effective_grant_for_user",
                            lambda uid, tid: grants_by_target.get(tid))
        monkeypatch.setattr(rr, "_catalog_databases",
                            lambda tid: list(dbs_by_target.get(tid, [])))
        monkeypatch.setattr(rr.grants, "control_plane_target_ids",
                            lambda: set(control_plane))
        monkeypatch.setattr(rr.deps, "require_whitelisted", lambda c: None)
        return rr.requestable(claims={"sub": "U1"})["connections"]
    return _wire


def test_a_target_with_no_grant_is_offered_whole(catalog):
    out = catalog([_T(7, "prod-main")], {}, {7: ["payments", "ledger"]})
    assert out == [{"connectionId": "prod-main", "name": "prod-main",
                    "engine": "postgres", "env": "production",
                    "databases": ["payments", "ledger"], "partial": False}]


def test_an_unrestricted_grant_leaves_nothing_to_ask_for(catalog):
    # allowed_databases None = every database on the target.
    out = catalog([_T(7, "prod-main")],
                  {7: {"allowed_databases": None, "mode": "ro"}},
                  {7: ["payments", "ledger"]})
    assert out == []


def test_a_scoped_grant_offers_only_the_databases_not_held(catalog):
    out = catalog([_T(7, "prod-main")],
                  {7: {"allowed_databases": ["payments"], "mode": "ro"}},
                  {7: ["payments", "ledger", "audit"]})
    assert len(out) == 1
    assert out[0]["databases"] == ["ledger", "audit"]
    # The UI needs to know they already hold part of this one, or it will say
    # "no access" about a server they query every day.
    assert out[0]["partial"] is True


def test_a_target_whose_every_database_is_held_drops_out(catalog):
    out = catalog([_T(7, "prod-main")],
                  {7: {"allowed_databases": ["payments"], "mode": "ro"}},
                  {7: ["payments"]})
    assert out == []


def test_the_control_plane_is_never_offered(catalog):
    # A grant there would let someone edit the audit trail recording their own
    # queries. grants.grant refuses it, so offering it would only produce a
    # request that cannot be approved.
    out = catalog([_T(1, "bot-control"), _T(7, "prod-main")], {},
                  {1: ["queryhub"], 7: ["payments"]}, control_plane={1})
    assert [c["connectionId"] for c in out] == ["prod-main"]


def test_a_target_with_no_catalogued_databases_still_appears(catalog):
    # A freshly onboarded server has no snapshot yet. Hiding it would make the
    # newest target the one nobody can ask for.
    out = catalog([_T(9, "new-server")], {}, {})
    assert [c["connectionId"] for c in out] == ["new-server"]
    assert out[0]["databases"] == []


def test_disabled_targets_are_out_by_construction(catalog, monkeypatch):
    # list_enabled() is the source, so a retired server cannot be requested.
    # Asserted here so a future switch to list_all() fails a test rather than
    # quietly offering parked servers.
    seen = {}
    monkeypatch.setattr(rr.targets, "list_enabled",
                        lambda: seen.setdefault("called", True) and [])
    monkeypatch.setattr(rr.targets, "list_all",
                        lambda: pytest.fail("must not enumerate the whole fleet"))
    monkeypatch.setattr(rr.grants, "control_plane_target_ids", lambda: set())
    monkeypatch.setattr(rr.deps, "require_whitelisted", lambda c: None)
    assert rr.requestable(claims={"sub": "U1"})["connections"] == []
    assert seen["called"] is True
