"""The IDP panel's identity assertion: what it proves, and what it must not.

Every negative here is a way an attacker could try to speak as someone else.
The positives pin the contract the panel signs against — the two frozen
digests are shared with the panel's own Go tests, so the two implementations
are checked against the same fixed values rather than against each other.
"""
import json
import time
from datetime import datetime, timezone

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
           exp_delta=60, iat_delta=0, jti="j-1", bh=None, alg="EdDSA"):
    now = int(time.time())
    claims = {"iss": iss, "aud": aud, "sub": sub, "jti": jti,
              "iat": now + iat_delta, "exp": now + exp_delta}
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
        idp_assertion.requesters, "principal_by_email",
        lambda e: {"slack_user_id": "U123"} if e == "dev@example.com" else None)
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
    # The resolver reads the admins table as well (principal_by_email).
    monkeypatch.setattr(idp_assertion.requesters, "principal_by_email",
                        lambda e: {"slack_user_id": "UADMIN"})
    tok = _token(configured, bh=idp_assertion.body_hash("GET", "/api/queue", b""))
    assert idp_assertion.verify(tok, "GET", "/api/queue", b"").id == "UADMIN"


def test_unknown_address_is_refused_not_onboarded(configured, monkeypatch):
    monkeypatch.setattr(idp_assertion.requesters, "principal_by_email", lambda e: None)
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


# ---------------------------------------------------------------------------
# Clock skew, lifetime, the jti ledger's horizon and the empty-domain case
# ---------------------------------------------------------------------------

def _with_setting(monkeypatch, key, value):
    real = idp_assertion.cfg.get_setting
    monkeypatch.setattr(idp_assertion.cfg, "get_setting",
                        lambda k, d=None: value if k == key else real(k, d))


_BH = idp_assertion.body_hash("GET", "/api/admin/queue", b"")


def test_a_panel_clock_a_few_seconds_ahead_is_tolerated(configured):
    """Two hosts never agree to the second. With no leeway a panel one second
    ahead minted an `iat` in this host's future and every call failed."""
    tok = _token(configured, iat_delta=5, exp_delta=65, bh=_BH)
    assert idp_assertion.verify(tok, "GET", "/api/admin/queue", b"").id == "U123"


def test_a_panel_clock_far_ahead_is_named_as_such(configured):
    tok = _token(configured, iat_delta=40, exp_delta=100, bh=_BH)
    with pytest.raises(idp_assertion.AssertionError_) as e:
        idp_assertion.verify(tok, "GET", "/api/admin/queue", b"")
    assert e.value.code == "clock_ahead"


def test_the_tolerance_is_a_setting(configured, monkeypatch):
    _with_setting(monkeypatch, "idp_clock_skew_seconds", "0")
    tok = _token(configured, iat_delta=3, exp_delta=63, bh=_BH)
    with pytest.raises(idp_assertion.AssertionError_) as e:
        idp_assertion.verify(tok, "GET", "/api/admin/queue", b"")
    assert e.value.code == "clock_ahead"


@pytest.mark.parametrize("raw,expected", [
    ("10", 10), ("0", 0), ("999", 60), ("-5", 0), ("junk", 10), ("", 10)])
def test_the_tolerance_setting_is_clamped(monkeypatch, raw, expected):
    monkeypatch.setattr(idp_assertion.cfg, "get_setting",
                        lambda k, d=None: raw if k == "idp_clock_skew_seconds" else d)
    assert idp_assertion._clock_skew() == expected


def test_an_assertion_living_longer_than_two_minutes_is_refused(configured):
    """The panel mints 60-second assertions; a longer one was not minted as
    specified, and the jti ledger only has to cover what can be accepted."""
    tok = _token(configured, exp_delta=600, bh=_BH)
    with pytest.raises(idp_assertion.AssertionError_) as e:
        idp_assertion.verify(tok, "GET", "/api/admin/queue", b"")
    assert e.value.code == "too_long_lived"


def test_the_jti_is_remembered_until_expiry_plus_the_tolerance(configured, monkeypatch):
    """A fixed window from now let a token that outlived it be replayed once
    its ledger row was pruned."""
    seen = {}
    monkeypatch.setattr(idp_assertion, "_claim_jti",
                        lambda jti, until: seen.setdefault("until", until) is not None)
    tok = _token(configured, bh=_BH)
    idp_assertion.verify(tok, "GET", "/api/admin/queue", b"")
    exp = pyjwt.decode(tok, options={"verify_signature": False})["exp"]
    assert seen["until"] == datetime.fromtimestamp(exp + 10, timezone.utc)


def test_an_empty_domain_setting_refuses_every_assertion(configured, monkeypatch):
    """Fail closed. The OIDC logins skip the check when no domain is set; this
    path is a machine speaking for people, and would accept any address."""
    _with_setting(monkeypatch, "web_allowed_email_domain", "")
    tok = _token(configured, bh=_BH)
    with pytest.raises(idp_assertion.AssertionError_) as e:
        idp_assertion.verify(tok, "GET", "/api/admin/queue", b"")
    assert e.value.code == "no_domain"
