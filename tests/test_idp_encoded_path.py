"""An assertion signs the DECODED path and the RAW query (PLA-1409, item 5).

The panel signs Go's `u.Path`, the decoded path, plus `"?" + u.RawQuery`, the
raw query. It sends the request with the escaped path. The two forms differ
only for a percent-encoded segment. current_user builds the signed target from
`scope["path"]` (the server decodes it) and `scope["query_string"]` (raw). So
both sides hash the same string. Measured 2026-10-14 with a Go 1.24 client
against uvicorn 0.51, with httptools and with h11. For `%20`, `%2F`, `%C3%BC`,
`%2541` and `+`, the target that Go signs equals `scope["path"]` plus the raw
query.

These tests run the real verify() behind the real app, through TestClient.
TestClient decodes the path twice (httpx decodes it, then TestClient calls
`unquote` again). uvicorn decodes it once. The two agree for every escape used
here. A `%25` escape would show a difference that production does not have, so
these tests do not send one.
"""
import json
import logging
import time
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import Depends
from fastapi.testclient import TestClient

from queryhub import requesters
from queryhub.web import app as web_app
from queryhub.web import deps, idp_assertion

GENERIC = {"code": "unauthenticated",
           "message": "Refused: the identity assertion is not valid."}


def _keypair():
    priv = ed25519.Ed25519PrivateKey.generate()
    return (
        priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()).decode(),
        priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
    )


_JTI = iter(range(10**6))


@pytest.fixture
def seam(monkeypatch):
    priv, pub = _keypair()
    settings = {"idp_assertion_enabled": "on", "idp_issuer": "idp",
                "idp_audience": "queryhub", "web_allowed_email_domain": "example.com",
                "idp_public_keys": json.dumps({"k1": pub})}
    monkeypatch.setattr(idp_assertion.cfg, "get_setting",
                        lambda k, d=None: settings.get(k, d))
    monkeypatch.setattr(idp_assertion, "_claim_jti", lambda jti, until: True)
    person = {"slack_user_id": "U0DEV00001", "email": "dev@example.com",
              "name": "Dev", "enabled": True}
    monkeypatch.setattr(
        requesters.db, "fetch_all",
        lambda sql, params=None: [person] if "FROM requesters" in sql
        and params == ("dev@example.com",) else [])

    app = web_app.create_app()

    @app.get("/api/_probe_path/{rest:path}")
    def _probe(rest: str, claims: dict = Depends(deps.current_user)):
        return {"sub": claims["sub"], "rest": rest}

    # Ahead of a mounted frontend bundle, as in test_deps_assertion_path.py.
    app.router.routes.insert(0, app.router.routes.pop())
    return SimpleNamespace(client=TestClient(app), priv=priv)


def _get(seam, wire, *, signed):
    """Send `wire` (escaped, as on the network) with an assertion over `signed`."""
    now = int(time.time())
    tok = pyjwt.encode(
        {"iss": "idp", "aud": "queryhub", "sub": "dev@example.com",
         "jti": f"j-{next(_JTI)}", "iat": now, "exp": now + 60,
         "bh": idp_assertion.body_hash("GET", signed, b"")},
        seam.priv, algorithm="EdDSA", headers={"kid": "k1"})
    return seam.client.get(wire, headers={deps.ASSERTION_HEADER: tok})


# A named escape keeps this file ASCII. The repository scanner refuses the
# literal character.
_U_UMLAUT = "\N{LATIN SMALL LETTER U WITH DIAERESIS}"

# (what goes on the wire, what Go's u.Path gives, the decoded segment)
CASES = [
    ("/api/_probe_path/a%20b", "/api/_probe_path/a b", "a b"),
    ("/api/_probe_path/a%2Fb", "/api/_probe_path/a/b", "a/b"),
    ("/api/_probe_path/%C3%BC", "/api/_probe_path/" + _U_UMLAUT, _U_UMLAUT),
]


@pytest.mark.parametrize("wire,decoded,segment", CASES)
def test_an_assertion_over_the_decoded_path_verifies(seam, wire, decoded, segment):
    r = _get(seam, wire, signed=decoded)
    assert r.status_code == 200, r.text
    assert r.json() == {"sub": "U0DEV00001", "rest": segment}


@pytest.mark.parametrize("wire,decoded,segment", CASES)
def test_an_assertion_over_the_escaped_path_is_refused(seam, caplog, wire, decoded,
                                                        segment):
    with caplog.at_level(logging.WARNING, logger=deps.log.name):
        r = _get(seam, wire, signed=wire)
    assert r.status_code == 401
    assert r.json() == {"error": GENERIC}
    assert "idp assertion refused: body_mismatch" in caplog.text


def test_the_query_string_is_signed_raw(seam):
    """The other half of Go's rule: `u.RawQuery` keeps its escapes."""
    wire = "/api/_probe_path/a%20b?q=x%20y&n=1"
    assert _get(seam, wire, signed="/api/_probe_path/a b?q=x%20y&n=1").status_code == 200
    assert _get(seam, wire, signed="/api/_probe_path/a b?q=x y&n=1").status_code == 401
