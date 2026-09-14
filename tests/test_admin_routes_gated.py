"""Every /api/admin route must check admin rights. Structurally, not by review.

The gate is per-handler (`admins.require_admin(claims, ...)`), not
router-level — the router only carries `block_pw_gate`. All 22 routes call it
today, verified by reading every one. But "all of them do" is a fact about this
moment: adding a route and forgetting the line is a two-character mistake that
no existing test notices, and the result is an unauthenticated admin endpoint.

So this walks the router and asserts the property for each route, which means a
new route is covered the moment it is added rather than when someone remembers
to write its test.

Two complementary checks:
  1. Source-level: every handler function's body mentions require_admin.
  2. Behavioural: every route, called over HTTP with a valid but NON-admin
     session, answers 403 — no route body runs.

The second is the real one: it exercises the actual dependency chain, so it also
catches a gate that is present in the source but unreachable (sitting after the
work, or behind a branch). The first needs no HTTP and pins the reason in the
failure message, which is what a contributor sees.
"""
import ast
import inspect
import logging
import re
import textwrap
import typing

import pytest
from starlette.testclient import TestClient

from dba_slack_bot import admins, db, requesters
from dba_slack_bot.web import app as web_app
from dba_slack_bot.web import routes_admin, sessions

# Routes that deliberately do NOT require admin rights would go here, with the
# reason. Empty on purpose: there are none, and an addition should have to argue
# for itself in this list rather than pass silently.
PUBLIC_ADMIN_ROUTES: dict[str, str] = {}


def _admin_routes():
    """(path, methods, endpoint) for every route on the admin router."""
    for route in routes_admin.router.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/admin"):
            continue
        yield path, sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}), \
            route.endpoint


def test_the_router_actually_has_routes():
    """Guard the guard: an empty iteration would make everything below pass."""
    routes = list(_admin_routes())
    assert len(routes) >= 20, f"only found {len(routes)} admin routes"


@pytest.mark.parametrize(
    "path,methods,endpoint",
    list(_admin_routes()),
    ids=[f"{m[0] if m else '?'} {p}" for p, m, _ in _admin_routes()])
def test_every_admin_route_checks_admin_rights(path, methods, endpoint):
    if path in PUBLIC_ADMIN_ROUTES:
        pytest.skip(PUBLIC_ADMIN_ROUTES[path])
    src = inspect.getsource(endpoint)
    # require_admin / require_super_admin / is_super_admin — any of the admin
    # gates. A route that resolves its own permission some other way should be
    # listed in PUBLIC_ADMIN_ROUTES with a reason, not pass by accident.
    assert re.search(r"require_admin|require_super|is_super_admin", src), (
        f"{methods} {path} -> {endpoint.__name__}() has no admin check. "
        f"Add one, or list the route in PUBLIC_ADMIN_ROUTES with a reason.")


def _first_gate_index(fn_src: str):
    """(index of the first statement that calls a gate, [statements before it]).

    Parsed, not grepped: "the gate is mentioned somewhere" is a weaker property
    than "the gate runs before anything else".
    """
    tree = ast.parse(textwrap.dedent(fn_src))
    fn = tree.body[0]
    for i, stmt in enumerate(fn.body):
        if any(getattr(n.func, "attr", "") in
               ("require_admin", "require_super", "is_super_admin")
               for n in ast.walk(stmt) if isinstance(n, ast.Call)):
            before = [s for s in fn.body[:i]
                      if not (isinstance(s, ast.Expr)
                              and isinstance(s.value, ast.Constant))]
            return i, before
    return None, []


@pytest.mark.parametrize(
    "path,methods,endpoint",
    list(_admin_routes()),
    ids=[f"{m[0] if m else '?'} {p}" for p, m, _ in _admin_routes()])
def test_nothing_runs_before_the_admin_gate(path, methods, endpoint):
    """Authorization comes first — before input validation, before any lookup.

    /queue/{id}/decision used to validate `body.decision` and SELECT the
    `requests` row before calling require_admin. The handler still refused to
    act, so nothing was approved by a non-admin, but the two error paths were
    reachable without admin rights: the 400 leaked the accepted decision values,
    and 404-vs-anything-else on an arbitrary id was a request-id enumeration
    oracle. Both were visible only by reading the statement order, which is
    exactly what no review reliably does.
    """
    if path in PUBLIC_ADMIN_ROUTES:
        pytest.skip(PUBLIC_ADMIN_ROUTES[path])
    _, before = _first_gate_index(inspect.getsource(endpoint))
    assert not before, (
        f"{methods} {path} -> {endpoint.__name__}() runs "
        f"{len(before)} statement(s) before its admin check "
        f"(first at line {before[0].lineno} of the function). Move the gate up: "
        f"a non-admin must not reach validation, a lookup, or an error path. "
        f"A scope check that needs the row can stay where it is, as long as a "
        f"plain require_admin runs first.")


def test_the_ordering_check_catches_a_late_gate():
    """Mutation check: a handler whose gate sits after a lookup must fail."""
    late = '''
def handler(request_id: int, claims: dict):
    """Docstring is not work."""
    row = db.fetch_one("SELECT 1 WHERE id = %s", (request_id,))
    admin.require_admin(claims, "review")
    return row
'''
    _, before = _first_gate_index(late)
    assert len(before) == 1

    early = '''
def handler(request_id: int, claims: dict):
    """Docstring is not work."""
    admin.require_admin(claims, "review")
    return db.fetch_one("SELECT 1 WHERE id = %s", (request_id,))
'''
    _, before = _first_gate_index(early)
    assert before == []


def test_the_check_would_fail_for_an_ungated_handler():
    """Mutation check on the assertion itself: a handler with no gate must be
    reported, so the test above cannot be vacuous."""
    def ungated_handler(claims: dict):
        return {"secrets": "everything"}

    src = inspect.getsource(ungated_handler)
    assert not re.search(r"require_admin|require_super|is_super_admin", src)


# ---------------------------------------------------------------------------
# The behavioural half. A valid session for a user who is NOT an admin, driven
# through the real app: middleware, dependencies, handler. Nothing in the route
# body may run, so every method gets 403 and never 200/500.
# ---------------------------------------------------------------------------

NON_ADMIN = "U_NOT_AN_ADMIN"


def _minimal_body(endpoint):
    """A request body that PASSES validation for this route, or None.

    Necessary, and the reason is worth writing down: FastAPI validates the body
    before the handler runs, and the admin gate lives *inside* the handler. So a
    non-admin POSTing `{}` gets 422 — refused, but refused by the schema, which
    proves nothing about authorization. To test the gate we must get past
    validation first.

    Synthesized from the model rather than hand-listed, so a new route with a new
    body model is covered the day it lands instead of quietly degrading to 422.
    """
    try:
        hints = typing.get_type_hints(endpoint)
    except Exception:
        return None
    for ann in hints.values():
        fields = getattr(ann, "model_fields", None)
        if not fields:
            continue
        filler = {bool: True, int: 1, float: 1.0, str: "x",
                  list: [], dict: {}}
        body = {}
        for fname, finfo in fields.items():
            if not finfo.is_required():
                continue
            base = typing.get_origin(finfo.annotation) or finfo.annotation
            body[fname] = filler.get(base, "x")
        return body
    return None


@pytest.fixture
def non_admin_client(monkeypatch):
    logging.disable(logging.CRITICAL)
    monkeypatch.setattr(db, "init_pool", lambda: None)
    from dba_slack_bot.slack_app import notifications
    monkeypatch.setattr(notifications, "dm_all_admins", lambda *a, **k: None)

    # A session that is genuinely valid — signature, expiry, liveness — and
    # belongs to a plain whitelisted user. This is the interesting case: not
    # "no session" (401), but a real logged-in non-admin.
    monkeypatch.setattr(sessions, "verify_access",
                        lambda t: {"sub": NON_ADMIN, "sid": "sid-1",
                                   "provider": "slack"} if t == "good" else None)
    monkeypatch.setattr(sessions, "session_alive", lambda sid, principal=None: True)
    monkeypatch.setattr(admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(admins, "is_super_admin", lambda uid: False)
    monkeypatch.setattr(requesters, "is_allowed", lambda uid: True)
    # Same objects, imported into the gate module's namespace.
    from dba_slack_bot.web import admin as web_admin
    monkeypatch.setattr(web_admin.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(web_admin.admins, "is_super_admin", lambda uid: False)

    with TestClient(web_app.create_app()) as c:
        c.cookies.set("qh_session", "good")
        yield c


@pytest.mark.parametrize(
    "path,methods,endpoint",
    list(_admin_routes()),
    ids=[f"{m[0] if m else '?'} {p}" for p, m, _ in _admin_routes()])
def test_non_admin_session_gets_403_from_every_admin_route(
        non_admin_client, path, methods, endpoint):
    if path in PUBLIC_ADMIN_ROUTES:
        pytest.skip(PUBLIC_ADMIN_ROUTES[path])
    # Path params: any value will do — the gate must refuse before the handler
    # looks anything up. That is the property under test.
    url = re.sub(r"\{[^}]+\}", "1", path)
    body = _minimal_body(endpoint)
    for method in methods:
        r = non_admin_client.request(
            method, url,
            headers={"origin": "http://testserver"},  # pass the CSRF gate
            json=body if method in {"POST", "PUT", "PATCH"} else None)
        assert r.status_code == 403, (
            f"{method} {url} answered {r.status_code}, not 403 — a non-admin "
            f"reached past the gate. Body: {r.text[:200]}")
        # API_CONTRACT envelope: {"error": {code, message}} — and the code must
        # say "forbidden", not "server_error". A 403 that is really a crash
        # dressed up by an exception handler would not prove the gate ran.
        assert (r.json().get("error", {}) or {}).get("code") == "forbidden", \
            f"{method} {url}: {r.text[:200]}"


def test_the_behavioural_check_is_not_passing_for_the_wrong_reason(
        non_admin_client):
    """403-on-everything would also happen if the session were simply broken —
    then this whole file would prove nothing. Confirm the same client reaches a
    NON-admin route successfully, i.e. the session really is valid."""
    r = non_admin_client.get("/api/me")
    assert r.status_code == 200, r.text
    user = r.json()["user"]
    assert user["slackId"] == NON_ADMIN
    # ...and that the same session is genuinely non-privileged, so the 403s
    # above are the gate refusing an authenticated user rather than the app
    # refusing a broken one.
    assert user["role"] == "developer"


def test_router_carries_the_password_change_gate():
    """Separate property, also easy to lose: a local account flagged
    must_change_pw is blocked from every admin route by a router-level
    dependency rather than by each handler remembering."""
    from dba_slack_bot.web import deps
    dependencies = [d.dependency for d in routes_admin.router.dependencies]
    assert deps.block_pw_gate in dependencies
