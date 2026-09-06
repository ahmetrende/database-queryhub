"""A request proxied from the IdP panel is recorded as coming from the IdP.

Every surface reaches the same `/api/*` endpoints, so an IdP-proxied request
inherits `origin='web'` unless something says otherwise — which `origins`' own
module docstring warns about, having declared `IDP` before anything wrote it.
For a while nothing did: the panel stamped `origin` into the request body, and
neither `QueryIn` nor `BatchIn` has such a field, so every panel request was
filed as `web` on the one column whose job is to say which door was used.

The value is derived from the authenticated provider and never read from the
body. That is not a detail of convenience: the panel cannot assert its own
origin for the same reason it cannot assert its own authority.
"""
from __future__ import annotations

import ast
from pathlib import Path

from queryhub import origins
from queryhub.web.routes_queries import _origin_for

_ROUTES = Path(__file__).resolve().parents[1] / "src" / "queryhub" / "web" / "routes_queries.py"


def test_an_idp_proxied_request_is_recorded_as_idp():
    assert _origin_for({"sub": "U1", "provider": "idp"}) == origins.IDP


def test_a_cookie_session_is_still_recorded_as_web():
    assert _origin_for({"sub": "U1", "provider": "slack"}) == origins.WEB
    assert _origin_for({"sub": "U1", "provider": "local"}) == origins.WEB


def test_claims_without_a_provider_fall_back_to_web():
    """The pre-assertion shape, and anything unexpected: `web` is the safe
    answer here because it is what this router has always meant."""
    assert _origin_for({"sub": "U1"}) == origins.WEB
    assert _origin_for({}) == origins.WEB


def test_no_literal_origin_argument_survives_in_the_queries_router():
    """The regression guard, and the reason this file exists.

    The defect was not a wrong branch — it was four literal `origin="web"`
    arguments that no test could see. Re-introducing one anywhere in this
    router would silently file panel traffic as web again, and every
    assertion above would still pass, because they only exercise the helper.

    Parsed rather than grepped: an earlier version of this guard matched its
    own prose, which is the same class of mistake it exists to catch. The AST
    sees keyword arguments and nothing else.
    """
    tree = ast.parse(_ROUTES.read_text())
    literals = [
        f"line {kw.value.lineno}: origin={kw.value.value!r}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "origin"
        and isinstance(kw.value, ast.Constant)
        and isinstance(kw.value.value, str)
    ]
    assert literals == [], (
        "literal origin argument(s) back in routes_queries.py — "
        f"{literals}; use _origin_for(claims) so a proxied request is not "
        "filed under the wrong surface"
    )
