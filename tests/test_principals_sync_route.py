"""The Layer-A reconcile endpoint.

The IDP panel owns who may use QueryHub; this service keeps a derived copy so
it can still decide while the panel is down. The endpoint takes the FULL
desired state as verified addresses and applies the difference.

Limits that are deliberate and tested here: it never creates a row
(onboarding needs a Slack id the panel does not own), it refuses a desired
state that would disable everyone, it never disables an admin, a role holder
or the sync account itself, and a dry run writes nothing. The gate is the sync
principal arriving through an assertion; an admin role is neither needed nor
enough.

A run that would disable more than `idp_sync_max_disable` requesters is held
whole (idp_sync_guard.py): the guard's own states are pinned in
test_idp_sync_guard.py, and here only what the route does with each verdict.
"""
import pytest
from fastapi.testclient import TestClient

from queryhub.idp_sync_guard import Verdict
from queryhub.web import app as web_app
from queryhub.web import deps, routes_admin

SYNC = "U_SYNC_ACCOUNT"


@pytest.fixture
def app_client(monkeypatch):
    """Patch the names ROUTES_ADMIN resolves, not another module's re-export:
    the handler looks up routes_admin.requesters, so patching elsewhere would
    be a no-op that reads like a pass."""
    monkeypatch.setattr(routes_admin.cfg, "get_setting",
                        lambda k, d=None: SYNC if k == "idp_sync_principal" else d)
    # Role holders come from the access model's tables; no database here.
    monkeypatch.setattr(routes_admin.access_model, "role_holder_ids", lambda: set())
    app = web_app.create_app()
    return app, TestClient(app)


def _as(app, monkeypatch, principal, *, is_admin=True, provider="idp"):
    """Replace the identity through dependency_overrides.

    Monkeypatching deps.current_user does nothing: FastAPI captured the
    function object when the route was declared, so the override registry is
    the only seam that reaches the resolved dependency.
    """
    monkeypatch.setattr(routes_admin.admin.admins, "is_admin", lambda uid: is_admin)
    monkeypatch.setattr(routes_admin.admin.admins, "is_super_admin", lambda uid: False)
    app.dependency_overrides[deps.current_user] = \
        lambda: {"sub": principal, "provider": provider, "sid": None}


def _people(monkeypatch, *, live, by_email, admins_active=(), role_holders=()):
    """Stub the tables the reconcile reads, and record what it writes."""
    monkeypatch.setattr(routes_admin.requesters, "list_enabled_ids", lambda: set(live))
    monkeypatch.setattr(routes_admin.requesters, "principal_by_email",
                        lambda e: ({"slack_user_id": by_email[e]} if e in by_email else None))
    monkeypatch.setattr(routes_admin.admins, "list_active",
                        lambda: [{"slack_user_id": a, "source": "permanent"}
                                 for a in admins_active])
    monkeypatch.setattr(routes_admin.access_model, "role_holder_ids",
                        lambda: set(role_holders))
    writes = {"enable": [], "disable": [], "audit": []}
    monkeypatch.setattr(routes_admin.requesters, "enable",
                        lambda pid: writes["enable"].append(pid))
    monkeypatch.setattr(routes_admin.requesters, "disable",
                        lambda pid: writes["disable"].append(pid))
    monkeypatch.setattr(routes_admin.audit, "log",
                        lambda *a, **k: writes["audit"].append((a, k)))
    return writes


def test_the_sync_principal_needs_no_admin_role(app_client, monkeypatch):
    """Under the access model an admin role is fleet-wide, so requiring one
    handed the panel's cron key approval authority it has no use for."""
    app, client = app_client
    _as(app, monkeypatch, SYNC, is_admin=False)
    _people(monkeypatch, live={"U_KEEP"}, by_email={"keep@example.com": "U_KEEP"})
    r = client.post("/api/admin/principals/sync",
                    json={"emails": ["keep@example.com"], "admin_emails": []})
    assert r.status_code == 200, r.text


def test_a_browser_session_of_the_sync_principal_is_refused(app_client, monkeypatch):
    """The same account signed in through the web UI is not the panel."""
    app, client = app_client
    _as(app, monkeypatch, SYNC, provider="slack")
    r = client.post("/api/admin/principals/sync",
                    json={"emails": ["a@example.com"], "admin_emails": []})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_an_unset_sync_principal_refuses_everyone(app_client, monkeypatch):
    app, client = app_client
    monkeypatch.setattr(routes_admin.cfg, "get_setting", lambda k, d=None: d)
    _as(app, monkeypatch, SYNC)
    r = client.post("/api/admin/principals/sync",
                    json={"emails": ["a@example.com"], "admin_emails": []})
    assert r.status_code == 403


def test_admins_role_holders_and_the_sync_account_are_never_disabled(
        app_client, monkeypatch):
    """Disabling a requesters row disables the principal its roles hang on,
    so leaving an approver out of the panel's list must not strip approval
    power. They come back in `kept` instead."""
    app, client = app_client
    _as(app, monkeypatch, SYNC)
    writes = _people(
        monkeypatch,
        live={"U_GONE", "U_ADMIN", "U_APPROVER", SYNC},
        by_email={"new@example.com": "U_NEW"},
        admins_active=["U_ADMIN"], role_holders=["U_APPROVER"])
    r = client.post("/api/admin/principals/sync",
                    json={"emails": ["new@example.com"], "admin_emails": []})
    assert r.status_code == 200, r.text
    assert writes["disable"] == ["U_GONE"]
    assert writes["enable"] == ["U_NEW"]
    assert r.json()["kept"] == sorted(["U_ADMIN", "U_APPROVER", SYNC])
    assert writes["audit"][0][0][4]["kept"] == sorted(["U_ADMIN", "U_APPROVER", SYNC])


def test_a_dry_run_returns_the_plan_and_writes_nothing(app_client, monkeypatch):
    """Run before the first real sync: that one disables everyone the panel
    leaves out, and nobody should learn the list by living through it."""
    app, client = app_client
    _as(app, monkeypatch, SYNC)
    writes = _people(
        monkeypatch, live={"U_GONE", "U_ADMIN"},
        by_email={"new@example.com": "U_NEW"}, admins_active=["U_ADMIN"])
    r = client.post("/api/admin/principals/sync",
                    json={"emails": ["new@example.com", "stranger@example.com"],
                          "admin_emails": [], "dry_run": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["requesters"] == {"would_enable": ["U_NEW"],
                                  "would_disable": ["U_GONE"]}
    assert body["kept"] == ["U_ADMIN"]
    assert body["unresolved"] == ["stranger@example.com"]
    assert writes == {"enable": [], "disable": [], "audit": []}


def test_an_admin_that_is_not_the_sync_principal_is_refused(app_client, monkeypatch):
    app, client = app_client
    _as(app, monkeypatch, "U_SOME_ADMIN")
    r = client.post("/api/admin/principals/sync",
                    json={"emails": ["a@example.com"], "admin_emails": []})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_reconcile_enables_and_disables_known_addresses(app_client, monkeypatch):
    app, client = app_client
    _as(app, monkeypatch, SYNC)
    monkeypatch.setattr(routes_admin.requesters, "list_enabled_ids",
                        lambda: {"U_GONE"})
    monkeypatch.setattr(
        routes_admin.requesters, "principal_by_email",
        lambda e: {"slack_user_id": "U_NEW"} if e == "new@example.com" else None)
    enabled, disabled = [], []
    monkeypatch.setattr(routes_admin.requesters, "enable",
                        lambda pid: enabled.append(pid))
    monkeypatch.setattr(routes_admin.requesters, "disable",
                        lambda pid: disabled.append(pid))
    monkeypatch.setattr(routes_admin.admins, "list_active", lambda: [])
    monkeypatch.setattr(routes_admin.audit, "log", lambda *a, **k: None)

    r = client.post("/api/admin/principals/sync",
                    json={"emails": ["new@example.com"], "admin_emails": []})
    assert r.status_code == 200, r.text
    assert enabled == ["U_NEW"]
    assert disabled == ["U_GONE"]
    assert r.json()["requesters"] == {"enabled": 1, "disabled": 1}


def test_an_unknown_address_is_reported_never_created(app_client, monkeypatch):
    app, client = app_client
    _as(app, monkeypatch, SYNC)
    monkeypatch.setattr(routes_admin.requesters, "list_enabled_ids",
                        lambda: {"U_KEEP"})
    monkeypatch.setattr(
        routes_admin.requesters, "principal_by_email",
        lambda e: {"slack_user_id": "U_KEEP"} if e == "keep@example.com" else None)
    monkeypatch.setattr(routes_admin.requesters, "enable", lambda pid: None)
    monkeypatch.setattr(
        routes_admin.requesters, "disable",
        lambda pid: pytest.fail("nothing should be disabled here"))
    monkeypatch.setattr(routes_admin.admins, "list_active", lambda: [])
    monkeypatch.setattr(routes_admin.audit, "log", lambda *a, **k: None)

    r = client.post("/api/admin/principals/sync",
                    json={"emails": ["keep@example.com", "stranger@example.com"],
                          "admin_emails": []})
    assert r.status_code == 200, r.text
    assert r.json()["unresolved"] == ["stranger@example.com"]


def test_an_empty_desired_state_is_refused(app_client, monkeypatch):
    """A sync that would disable every requester is far likelier to be a
    broken caller than a real intent."""
    app, client = app_client
    _as(app, monkeypatch, SYNC)
    monkeypatch.setattr(routes_admin.requesters, "list_enabled_ids",
                        lambda: {"U_A", "U_B"})
    r = client.post("/api/admin/principals/sync",
                    json={"emails": [], "admin_emails": []})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "empty_sync_refused"


def test_the_sync_never_writes_to_the_admins_table(app_client, monkeypatch):
    """The panel must not be able to change who is a DBA admin — in either
    direction.

    Promotion was never implemented, but disabling was, and the guard against
    an empty desired state only covered requesters: a caller sending a full
    `emails` list with an empty `admin_emails` disabled EVERY admin, which
    stops approvals dead. Admin membership now changes only by a human acting
    in QueryHub, so a compromised panel cannot reach it at all.
    """
    app, client = app_client
    _as(app, monkeypatch, SYNC)
    monkeypatch.setattr(routes_admin.requesters, "list_enabled_ids", lambda: {"U_KEEP"})
    monkeypatch.setattr(
        routes_admin.requesters, "principal_by_email",
        lambda e: {"slack_user_id": "U_KEEP"} if e == "keep@example.com" else None)
    monkeypatch.setattr(routes_admin.requesters, "enable", lambda pid: None)
    monkeypatch.setattr(routes_admin.requesters, "disable", lambda pid: None)
    monkeypatch.setattr(
        routes_admin.admins, "list_active",
        lambda: [{"slack_user_id": "U_OLD_ADMIN", "source": "permanent"}])
    monkeypatch.setattr(routes_admin.audit, "log", lambda *a, **k: None)
    for forbidden in ("add", "disable"):
        monkeypatch.setattr(
            routes_admin.admins, forbidden,
            lambda *a, _f=forbidden, **k: pytest.fail(
                f"the reconcile called admins.{_f}"))

    r = client.post("/api/admin/principals/sync",
                    json={"emails": ["keep@example.com"], "admin_emails": []})
    assert r.status_code == 200, r.text


def test_admin_drift_is_reported_for_a_human_to_act_on(app_client, monkeypatch):
    """Reporting is the whole contribution the panel makes to admin
    membership: it says what it believes, and a person decides."""
    app, client = app_client
    _as(app, monkeypatch, SYNC)
    monkeypatch.setattr(routes_admin.requesters, "list_enabled_ids", lambda: {"U_KEEP"})
    # One resolver reads both people tables (requesters.principal_by_email).
    monkeypatch.setattr(
        routes_admin.requesters, "principal_by_email",
        lambda e: ({"slack_user_id": "U_KEEP"} if e == "keep@example.com"
                   else {"slack_user_id": "U_WANTS"} if e == "wants@example.com"
                   else None))
    monkeypatch.setattr(routes_admin.requesters, "enable", lambda pid: None)
    monkeypatch.setattr(routes_admin.requesters, "disable", lambda pid: None)
    monkeypatch.setattr(
        routes_admin.admins, "list_active",
        lambda: [{"slack_user_id": "U_STALE", "source": "permanent"}])
    monkeypatch.setattr(routes_admin.audit, "log", lambda *a, **k: None)

    r = client.post("/api/admin/principals/sync",
                    json={"emails": ["keep@example.com"],
                          "admin_emails": ["wants@example.com"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["admin_drift"]["not_admin_here"] == ["U_WANTS"]
    assert body["admin_drift"]["not_listed_by_panel"] == ["U_STALE"]


def test_every_reconcile_writes_an_audit_row(app_client, monkeypatch):
    """A permission change nobody can reconstruct afterwards is a permission
    change nobody can review."""
    app, client = app_client
    _as(app, monkeypatch, SYNC)
    monkeypatch.setattr(routes_admin.requesters, "list_enabled_ids", lambda: {"U_GONE"})
    monkeypatch.setattr(
        routes_admin.requesters, "principal_by_email",
        lambda e: {"slack_user_id": "U_NEW"} if e == "new@example.com" else None)
    monkeypatch.setattr(routes_admin.requesters, "enable", lambda pid: None)
    monkeypatch.setattr(routes_admin.requesters, "disable", lambda pid: None)
    monkeypatch.setattr(routes_admin.admins, "list_active", lambda: [])
    rows = []
    monkeypatch.setattr(routes_admin.audit, "log",
                        lambda *a, **k: rows.append((a, k)))

    r = client.post("/api/admin/principals/sync",
                    json={"emails": ["new@example.com"], "admin_emails": []})
    assert r.status_code == 200, r.text
    assert len(rows) == 1
    args, _ = rows[0]
    assert args[3] == "idp_principal_sync"
    details = args[4]
    assert details["enabled"] == ["U_NEW"]
    assert details["disabled"] == ["U_GONE"]


# ---- the guard on the number of people a run disables -----------------------

EIGHT = [f"U_GONE_{n}" for n in range(8)]


def _held_run(monkeypatch, verdict, *, live=EIGHT):
    """A run that would disable `live`, with the guard's answer fixed, so
    these read as what the route does with a verdict."""
    calls = []
    writes = _people(monkeypatch, live=set(live),
                     by_email={"new@example.com": "U_NEW"})

    def gate(ids, *, actor=None):
        calls.append({"ids": list(ids), "actor": actor})
        return verdict
    monkeypatch.setattr(routes_admin.idp_sync_guard, "gate", gate)
    return calls, writes


def test_a_run_over_the_limit_changes_nothing_and_says_it_is_waiting(
        app_client, monkeypatch):
    """Enables are held with the disables: a list that looks wrong is not
    applied in part."""
    app, client = app_client
    _as(app, monkeypatch, SYNC)
    calls, writes = _held_run(monkeypatch, Verdict("pending", 5, 8, 3, notified=2))
    r = client.post("/api/admin/principals/sync",
                    json={"emails": ["new@example.com"], "admin_emails": []})
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "approval_required"
    assert "8 requesters" in err["message"] and "limit of 5" in err["message"]
    assert err["state"] == "pending" and err["would_disable"] == 8
    assert writes == {"enable": [], "disable": [], "audit": []}
    assert calls == [{"ids": sorted(EIGHT), "actor": SYNC}]


def test_a_rejected_list_is_refused_with_the_same_status(app_client, monkeypatch):
    app, client = app_client
    _as(app, monkeypatch, SYNC)
    _, writes = _held_run(monkeypatch, Verdict("rejected", 5, 8, 3))
    r = client.post("/api/admin/principals/sync",
                    json={"emails": ["new@example.com"], "admin_emails": []})
    assert r.status_code == 409
    assert "rejected" in r.json()["error"]["message"]
    assert writes == {"enable": [], "disable": [], "audit": []}


def test_an_approved_run_applies_and_the_audit_row_names_the_approval(
        app_client, monkeypatch):
    app, client = app_client
    _as(app, monkeypatch, SYNC)
    _, writes = _held_run(monkeypatch, Verdict("approved", 5, 8, 9))
    r = client.post("/api/admin/principals/sync",
                    json={"emails": ["new@example.com"], "admin_emails": []})
    assert r.status_code == 200, r.text
    assert writes["disable"] == sorted(EIGHT) and writes["enable"] == ["U_NEW"]
    assert writes["audit"][0][0][4]["approved_hold"] == 9
    assert r.json()["guard"] == {"state": "approved", "limit": 5,
                                 "would_disable": 8, "hold_id": 9}


def test_exactly_five_is_within_the_limit_and_needs_no_approval(
        app_client, monkeypatch):
    """The real guard, no stub: the limit is inclusive."""
    app, client = app_client
    _as(app, monkeypatch, SYNC)
    writes = _people(monkeypatch, live=set(EIGHT[:5]),
                     by_email={"new@example.com": "U_NEW"})
    r = client.post("/api/admin/principals/sync",
                    json={"emails": ["new@example.com"], "admin_emails": []})
    assert r.status_code == 200, r.text
    assert writes["disable"] == sorted(EIGHT[:5])
    assert r.json()["guard"]["state"] == "within_limit"
    assert "approved_hold" not in writes["audit"][0][0][4]


def test_six_is_over_the_limit_with_the_real_guard_asking_once(
        app_client, monkeypatch):
    from queryhub import idp_sync_guard as guard
    app, client = app_client
    _as(app, monkeypatch, SYNC)
    writes = _people(monkeypatch, live=set(EIGHT[:6]),
                     by_email={"new@example.com": "U_NEW"})
    opened = []
    monkeypatch.setattr(guard, "_claim_approval", lambda ids: None)
    monkeypatch.setattr(guard, "_same_list", lambda ids: None)
    monkeypatch.setattr(guard, "_open",
                        lambda ids, lim, actor: opened.append((ids, lim, actor)) or {"id": 1})
    monkeypatch.setattr(guard, "_notify", lambda hold_id: 2)
    r = client.post("/api/admin/principals/sync",
                    json={"emails": ["new@example.com"], "admin_emails": []})
    assert r.status_code == 409
    assert opened == [(sorted(EIGHT[:6]), 5, SYNC)]
    assert writes == {"enable": [], "disable": [], "audit": []}


def test_a_dry_run_reports_the_guard_and_never_asks(app_client, monkeypatch):
    """Looking must not open a hold or send a card: nobody should learn the
    list by being asked about it."""
    app, client = app_client
    _as(app, monkeypatch, SYNC)
    writes = _people(monkeypatch, live=set(EIGHT),
                     by_email={"new@example.com": "U_NEW"})
    monkeypatch.setattr(routes_admin.idp_sync_guard, "gate",
                        lambda *a, **k: pytest.fail("a dry run called gate"))
    monkeypatch.setattr(routes_admin.idp_sync_guard, "preview",
                        lambda ids: Verdict("would_hold", 5, len(ids)))
    r = client.post("/api/admin/principals/sync",
                    json={"emails": ["new@example.com"], "admin_emails": [],
                          "dry_run": True})
    assert r.status_code == 200, r.text
    assert r.json()["guard"] == {"state": "would_hold", "limit": 5, "would_disable": 8}
    assert r.json()["requesters"]["would_disable"] == sorted(EIGHT)
    assert writes == {"enable": [], "disable": [], "audit": []}
