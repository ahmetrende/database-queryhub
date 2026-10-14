"""Which assertion refusals reach the person, and which stay generic (PLA-1409).

The panel forwards `error.code` from a 401 to the browser. Three refusals are
about the person: no account for the address, an address outside the domain,
and an address that two people share. Each one names its code and says what to
do. Every other refusal is a seam failure: the body stays generic, and only the
log names the code.

verify() raises the person codes only after the signature, the request binding
and the jti pass. So a forged token must get the generic answer. Otherwise the
401 would tell anyone which addresses have an account.
"""
import ast
import inspect
import json
import logging
import time
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import Depends, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from queryhub import requesters
from queryhub.web import app as web_app
from queryhub.web import deps, idp_assertion

# The contract that the panel builds against: code -> the exact message.
PERSON = {
    "not_onboarded":
        "Refused: QueryHub has no account for this address. Ask an admin to add you.",
    "bad_domain": "Refused: this address is outside the allowed domain.",
    "ambiguous_address":
        "Refused: two QueryHub accounts share this address. Ask an admin to fix it.",
}
GENERIC = {"code": "unauthenticated",
           "message": "Refused: the identity assertion is not valid."}
SEAM = {"expired", "clock_ahead", "too_long_lived", "bad_signature", "bad_audience",
        "bad_issuer", "unknown_kid", "body_mismatch", "replayed", "malformed",
        "disabled", "no_domain"}


# ---------------------------------------------------------------------------
# The classification itself
# ---------------------------------------------------------------------------

def _codes_verify_can_raise() -> set[str]:
    """Every literal code that idp_assertion.py passes to AssertionError_."""
    tree = ast.parse(inspect.getsource(idp_assertion))
    return {node.args[0].value for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", "") == "AssertionError_"
            and node.args and isinstance(node.args[0], ast.Constant)}


def test_every_refusal_code_is_a_person_code_or_a_seam_code():
    """A new code must be sorted on purpose. An unsorted code stays generic,
    which is safe, but it hides a refusal that the person could act on."""
    codes = _codes_verify_can_raise()
    assert len(codes) > 10, f"the scan found too few raise sites: {codes}"
    assert codes == SEAM | set(PERSON)
    assert set(idp_assertion.PERSON_MESSAGES) == set(PERSON)


def _conn():
    return Request({"type": "http", "method": "GET", "path": "/api/queue",
                    "query_string": b"", "headers": [(b"x-idp-assertion", b"tok")]})


def _refused_with(monkeypatch, code) -> dict:
    def refuse(*a, **k):
        raise idp_assertion.AssertionError_(code, "detail that only the log carries")
    monkeypatch.setattr(deps.idp_assertion, "verify", refuse)
    with pytest.raises(HTTPException) as e:
        deps.current_user(_conn())
    assert e.value.status_code == 401
    return e.value.detail


@pytest.mark.parametrize("code", sorted(PERSON))
def test_a_person_refusal_names_its_code_and_its_message(monkeypatch, code):
    assert _refused_with(monkeypatch, code) == {"code": code, "message": PERSON[code]}


@pytest.mark.parametrize("code", sorted(SEAM) + ["a_code_added_later"])
def test_a_seam_refusal_keeps_the_generic_answer(monkeypatch, caplog, code):
    with caplog.at_level(logging.WARNING, logger=deps.log.name):
        assert _refused_with(monkeypatch, code) == GENERIC
    # The operator still learns the cause, from the log line only.
    assert f"idp assertion refused: {code}" in caplog.text


# ---------------------------------------------------------------------------
# End to end: real tokens, the real verify(), the real app
# ---------------------------------------------------------------------------

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


def _mint(priv, *, sub, bh):
    now = int(time.time())
    claims = {"iss": "idp", "aud": "queryhub", "sub": sub, "jti": f"j-{next(_JTI)}",
              "iat": now, "exp": now + 60, "bh": bh}
    return pyjwt.encode(claims, priv, algorithm="EdDSA", headers={"kid": "k1"})


@pytest.fixture
def seam(monkeypatch):
    """The real verify() behind the real app. The two people tables and the
    jti ledger live in memory."""
    priv, pub = _keypair()
    settings = {"idp_assertion_enabled": "on", "idp_issuer": "idp",
                "idp_audience": "queryhub", "web_allowed_email_domain": "example.com",
                "idp_public_keys": json.dumps({"k1": pub})}
    monkeypatch.setattr(idp_assertion.cfg, "get_setting",
                        lambda k, d=None: settings.get(k, d))
    used = set()

    def claim(jti, until):
        if jti in used:
            return False
        used.add(jti)
        return True
    monkeypatch.setattr(idp_assertion, "_claim_jti", claim)

    tables = {"requesters": [], "admins": []}

    def fetch_all(sql, params=None):
        rows = tables["requesters"] if "FROM requesters" in sql else tables["admins"]
        return [r for r in rows if r["email"] == params[0]]
    monkeypatch.setattr(requesters.db, "fetch_all", fetch_all)

    app = web_app.create_app()

    @app.get("/api/_probe_idp")
    def _probe(claims: dict = Depends(deps.current_user)):
        return {"sub": claims["sub"]}

    # Ahead of a mounted frontend bundle, as in test_deps_assertion_path.py.
    app.router.routes.insert(0, app.router.routes.pop())
    return SimpleNamespace(client=TestClient(app), priv=priv, tables=tables,
                           settings=settings)


def _get(seam, sub, *, priv=None):
    path = "/api/_probe_idp"
    tok = _mint(priv or seam.priv, sub=sub, bh=idp_assertion.body_hash("GET", path, b""))
    return seam.client.get(path, headers={deps.ASSERTION_HEADER: tok})


def _person(uid, email="dev@example.com"):
    return {"slack_user_id": uid, "email": email, "name": f"Person {uid}", "enabled": True}


def test_a_known_address_passes(seam):
    """The control: the same set-up lets a real person through."""
    seam.tables["requesters"].append(_person("U0DEV00001"))
    r = _get(seam, "dev@example.com")
    assert r.status_code == 200, r.text
    assert r.json() == {"sub": "U0DEV00001"}


def test_an_unknown_address_reaches_the_person_as_not_onboarded(seam):
    r = _get(seam, "dev@example.com")
    assert r.status_code == 401
    assert r.json() == {"error": {"code": "not_onboarded",
                                  "message": PERSON["not_onboarded"]}}


def test_an_address_outside_the_domain_reaches_the_person_as_bad_domain(seam):
    seam.tables["requesters"].append(_person("U0DEV00001", "dev@elsewhere.test"))
    r = _get(seam, "dev@elsewhere.test")
    assert r.status_code == 401
    assert r.json() == {"error": {"code": "bad_domain", "message": PERSON["bad_domain"]}}


def test_a_shared_address_reaches_the_person_as_ambiguous_address(seam):
    seam.tables["requesters"].append(_person("U0DEV00001"))
    seam.tables["admins"].append(_person("U0DEV00002"))
    r = _get(seam, "dev@example.com")
    assert r.status_code == 401
    assert r.json() == {"error": {"code": "ambiguous_address",
                                  "message": PERSON["ambiguous_address"]}}


def test_a_forged_token_learns_nothing_about_the_address(seam):
    """Signed with a key that the panel does not hold. A known address, an
    unknown one and one outside the domain must get the same answer."""
    other, _ = _keypair()
    seam.tables["requesters"].append(_person("U0DEV00001"))
    seam.tables["admins"].append(_person("U0DEV00002", "shared@example.com"))
    seam.tables["requesters"].append(_person("U0DEV00003", "shared@example.com"))
    for sub in ("dev@example.com", "stranger@example.com", "shared@example.com",
                "dev@elsewhere.test"):
        r = _get(seam, sub, priv=other)
        assert r.status_code == 401
        assert r.json() == {"error": GENERIC}, sub


def test_an_empty_domain_setting_is_a_seam_failure(seam):
    """`no_domain` is the operator's configuration, not the person's address,
    so the person sees the generic answer."""
    seam.settings["web_allowed_email_domain"] = ""
    seam.tables["requesters"].append(_person("U0DEV00001"))
    r = _get(seam, "dev@example.com")
    assert r.status_code == 401
    assert r.json() == {"error": GENERIC}
