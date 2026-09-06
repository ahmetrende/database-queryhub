"""The IDP panel's identity assertion: what it proves, and what it must not.

Every negative here is a way an attacker could try to speak as someone else.
The positives pin the contract the panel signs against — the two frozen
digests are shared with the panel's own Go tests, so the two implementations
are checked against the same fixed values rather than against each other.
"""
import json
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
import jwt as pyjwt

from queryhub.web import idp_assertion


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


def _token(priv, *, kid="k1", sub="dev@example.com", iss="idp", aud="queryhub",
           exp_delta=60, jti="j-1", bh=None, alg="EdDSA"):
    claims = {"iss": iss, "aud": aud, "sub": sub, "jti": jti,
              "iat": int(time.time()), "exp": int(time.time()) + exp_delta}
    if bh is not None:
        claims["bh"] = bh
    return pyjwt.encode(claims, priv, algorithm=alg, headers={"kid": kid})


@pytest.fixture
def configured(monkeypatch):
    priv, pub = _keypair()
    settings = {"idp_assertion_enabled": "on", "idp_issuer": "idp",
                "idp_audience": "queryhub",
                "web_allowed_email_domain": "example.com",
                "idp_public_keys": json.dumps({"k1": pub})}
    monkeypatch.setattr(idp_assertion.cfg, "get_setting",
                        lambda k, d=None: settings.get(k, d))
    seen = set()
    monkeypatch.setattr(
        idp_assertion, "_claim_jti",
        lambda jti, exp: False if jti in seen else (seen.add(jti) or True))
    monkeypatch.setattr(
        idp_assertion.requesters, "by_email",
        lambda e: {"slack_user_id": "U123"} if e == "dev@example.com" else None)
    monkeypatch.setattr(idp_assertion.admins, "by_email", lambda e: None)
    return priv


def test_canonical_string_matches_the_frozen_vectors():
    assert idp_assertion.body_hash("POST", "/api/queries", b'{"sql":"SELECT 1"}') == \
        "5a7a8f41df2f2a0588b7454388f015c5ee552eb33a3cf5bf43f300ed3666cbaf"
    assert idp_assertion.body_hash("GET", "/api/queue?status=pending", b"") == \
        "ab0762cc2eedb13bfa213b7eb9f728640d3ab8f978ec1fb3cfe51aa78795d061"


def test_valid_assertion_resolves_to_the_row_principal(configured):
    body = b'{"sql":"SELECT 1"}'
    tok = _token(configured, bh=idp_assertion.body_hash("POST", "/api/queries", body))
    assert idp_assertion.verify(tok, "POST", "/api/queries", body).id == "U123"


def test_admin_only_address_resolves_too(configured, monkeypatch):
    """A DBA who only ever approves has no requesters row. The OIDC path
    consults admins for exactly this reason, and so must this one."""
    monkeypatch.setattr(idp_assertion.requesters, "by_email", lambda e: None)
    monkeypatch.setattr(idp_assertion.admins, "by_email",
                        lambda e: {"slack_user_id": "UADMIN"})
    tok = _token(configured, bh=idp_assertion.body_hash("GET", "/api/queue", b""))
    assert idp_assertion.verify(tok, "GET", "/api/queue", b"").id == "UADMIN"


def test_unknown_address_is_refused_not_onboarded(configured, monkeypatch):
    monkeypatch.setattr(idp_assertion.requesters, "by_email", lambda e: None)
    monkeypatch.setattr(idp_assertion.admins, "by_email", lambda e: None)
    tok = _token(configured, sub="stranger@example.com",
                 bh=idp_assertion.body_hash("GET", "/api/queue", b""))
    with pytest.raises(idp_assertion.AssertionError_) as e:
        idp_assertion.verify(tok, "GET", "/api/queue", b"")
    assert e.value.code == "not_onboarded"


def test_address_outside_the_allowed_domain_is_refused(configured):
    tok = _token(configured, sub="dev@evil.example",
                 bh=idp_assertion.body_hash("GET", "/api/queue", b""))
    with pytest.raises(idp_assertion.AssertionError_) as e:
        idp_assertion.verify(tok, "GET", "/api/queue", b"")
    assert e.value.code == "bad_domain"


@pytest.mark.parametrize("mutate,code", [
    (dict(iss="evil"), "bad_issuer"),
    (dict(aud="other"), "bad_audience"),
    (dict(exp_delta=-10), "expired"),
    (dict(kid="unknown"), "unknown_kid"),
])
def test_rejects_bad_claims(configured, mutate, code):
    kwargs = dict(bh=idp_assertion.body_hash("GET", "/api/queue", b""))
    kwargs.update(mutate)
    tok = _token(configured, **kwargs)
    with pytest.raises(idp_assertion.AssertionError_) as e:
        idp_assertion.verify(tok, "GET", "/api/queue", b"")
    assert e.value.code == code


def test_rejects_wrong_signing_key(configured):
    other, _ = _keypair()
    tok = _token(other, bh=idp_assertion.body_hash("GET", "/api/queue", b""))
    with pytest.raises(idp_assertion.AssertionError_):
        idp_assertion.verify(tok, "GET", "/api/queue", b"")


def test_rejects_alg_none(configured):
    """The classic: strip the signature, keep the payload."""
    claims = {"iss": "idp", "aud": "queryhub", "sub": "dev@example.com",
              "jti": "j-none", "iat": int(time.time()),
              "exp": int(time.time()) + 60,
              "bh": idp_assertion.body_hash("GET", "/api/queue", b"")}
    tok = pyjwt.encode(claims, key="", algorithm="none", headers={"kid": "k1"})
    with pytest.raises(idp_assertion.AssertionError_):
        idp_assertion.verify(tok, "GET", "/api/queue", b"")


def test_rejects_hs256_signed_with_the_public_key(configured):
    """Algorithm confusion: with an HMAC alg the published verification key
    doubles as the signing secret.

    The token is assembled by hand rather than with pyjwt.encode, because
    pyjwt refuses to SIGN with a PEM key — a protection on the signing side
    that an attacker simply would not use. Building the token the way an
    attacker would is the only way this exercises our verification at all.
    """
    import base64
    import hashlib
    import hmac

    def b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    pub = json.loads(idp_assertion.cfg.get_setting("idp_public_keys"))["k1"]
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "k1"}).encode())
    payload = b64(json.dumps({
        "iss": "idp", "aud": "queryhub", "sub": "dev@example.com",
        "jti": "j-hs", "iat": int(time.time()), "exp": int(time.time()) + 60,
        "bh": idp_assertion.body_hash("GET", "/api/queue", b""),
    }).encode())
    signing_input = header + b"." + payload
    sig = b64(hmac.new(pub.encode(), signing_input, hashlib.sha256).digest())
    tok = (signing_input + b"." + sig).decode()

    with pytest.raises(idp_assertion.AssertionError_):
        idp_assertion.verify(tok, "GET", "/api/queue", b"")


def test_rejects_body_mismatch(configured):
    tok = _token(configured,
                 bh=idp_assertion.body_hash("POST", "/api/queries",
                                            b'{"sql":"SELECT 1"}'))
    with pytest.raises(idp_assertion.AssertionError_) as e:
        idp_assertion.verify(tok, "POST", "/api/queries",
                             b'{"sql":"DROP TABLE users"}')
    assert e.value.code == "body_mismatch"


def test_rejects_path_mismatch(configured):
    tok = _token(configured, bh=idp_assertion.body_hash("POST", "/api/queries", b""))
    with pytest.raises(idp_assertion.AssertionError_):
        idp_assertion.verify(tok, "POST", "/api/admin/queue/1/decision", b"")


def test_rejects_replayed_jti(configured):
    bh = idp_assertion.body_hash("GET", "/api/queue", b"")
    tok = _token(configured, jti="replay-me", bh=bh)
    assert idp_assertion.verify(tok, "GET", "/api/queue", b"").id == "U123"
    with pytest.raises(idp_assertion.AssertionError_) as e:
        idp_assertion.verify(tok, "GET", "/api/queue", b"")
    assert e.value.code == "replayed"


def test_disabled_by_config_rejects_everything(configured, monkeypatch):
    """The whole seam is off until an operator turns it on."""
    monkeypatch.setattr(idp_assertion.cfg, "get_setting",
                        lambda k, d=None: "off" if k == "idp_assertion_enabled" else d)
    with pytest.raises(idp_assertion.AssertionError_) as e:
        idp_assertion.verify("anything", "GET", "/api/queue", b"")
    assert e.value.code == "disabled"


def test_verify_accepts_a_get_carrying_a_query_string(configured):
    """Task A2 / carried forward from B1's review: every call across this
    seam until now was a POST. The panel's poller (B2) signs a bodyless GET
    against `path + "?" + rawquery` — see
    internal/proxy/proxy.go's PrincipalPoster.Get in the panel repo, which
    builds exactly `/api/admin/notifications/outbox?limit=50` as `target`
    and signs it with a nil body.

    verify() is path-and-body bound (bh = body_hash(method, path, body)), and
    `path` here is whatever the caller passes — nothing in verify() special-
    cases a query string one way or the other. This proves the whole round
    trip: a signature built the way the panel builds one, checked the way
    QueryHub checks one, over a GET target that carries `?limit=N`.
    """
    path = "/api/admin/notifications/outbox?limit=50"
    tok = _token(configured, bh=idp_assertion.body_hash("GET", path, b""))
    assert idp_assertion.verify(tok, "GET", path, b"").id == "U123"


def test_verify_still_binds_the_query_string_not_just_the_bare_path(configured):
    """A signature minted for one query must not verify against another —
    otherwise the bare path would be the real binding and `?limit=N` would be
    decorative. Mirrors test_rejects_path_mismatch for the query-string case
    specifically, since that existing test never varies the query alone."""
    tok = _token(configured, bh=idp_assertion.body_hash(
        "GET", "/api/admin/notifications/outbox?limit=50", b""))
    with pytest.raises(idp_assertion.AssertionError_) as e:
        idp_assertion.verify(
            tok, "GET", "/api/admin/notifications/outbox?limit=999", b"")
    assert e.value.code == "body_mismatch"
