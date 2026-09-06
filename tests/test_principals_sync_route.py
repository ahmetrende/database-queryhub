"""The Layer-A reconcile endpoint.

The IDP panel owns who may use QueryHub; this service keeps a derived copy so
it can still decide while the panel is down. The endpoint takes the FULL
desired state as verified addresses and applies the difference.

Two limits are deliberate and tested here: it never creates a row (onboarding
needs a Slack id the panel does not own), and it refuses a desired state that
would disable everyone.
"""
import pytest
from fastapi.testclient import TestClient

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
    app = web_app.create_app()
    return app, TestClient(app)


def _as(app, monkeypatch, principal, *, is_admin=True):
    """Replace the identity through dependency_overrides.

    Monkeypatching deps.current_user does nothing: FastAPI captured the
    function object when the route was declared, so the override registry is
    the only seam that reaches the resolved dependency.
    """
    monkeypatch.setattr(routes_admin.admin.admins, "is_admin", lambda uid: is_admin)
    monkeypatch.setattr(routes_admin.admin.admins, "is_super_admin", lambda uid: False)
    app.dependency_overrides[deps.current_user] = \
        lambda: {"sub": principal, "provider": "idp", "sid": None}


def test_a_non_admin_is_refused(app_client, monkeypatch):
    """The admin gate runs first, before the sync-principal check."""
    app, client = app_client
    _as(app, monkeypatch, SYNC, is_admin=False)
    r = client.post("/api/admin/principals/sync",
                    json={"emails": ["a@example.com"], "admin_emails": []})
    assert r.status_code == 403


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
        routes_admin.requesters, "by_email",
        lambda e: {"slack_user_id": "U_NEW"} if e == "new@example.com" else None)
    monkeypatch.setattr(routes_admin.admins, "by_email", lambda e: None)
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
        routes_admin.requesters, "by_email",
        lambda e: {"slack_user_id": "U_KEEP"} if e == "keep@example.com" else None)
    monkeypatch.setattr(routes_admin.admins, "by_email", lambda e: None)
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
        routes_admin.requesters, "by_email",
        lambda e: {"slack_user_id": "U_KEEP"} if e == "keep@example.com" else None)
    monkeypatch.setattr(routes_admin.admins, "by_email", lambda e: None)
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
    monkeypatch.setattr(
        routes_admin.requesters, "by_email",
        lambda e: {"slack_user_id": "U_KEEP"} if e == "keep@example.com" else None)
    monkeypatch.setattr(
        routes_admin.admins, "by_email",
        lambda e: {"slack_user_id": "U_WANTS"} if e == "wants@example.com" else None)
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
        routes_admin.requesters, "by_email",
        lambda e: {"slack_user_id": "U_NEW"} if e == "new@example.com" else None)
    monkeypatch.setattr(routes_admin.admins, "by_email", lambda e: None)
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
