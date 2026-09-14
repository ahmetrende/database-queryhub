"""Faz 3.3 — local (built-in) accounts: password hashing (never cleartext),
identity namespacing, verify_login timing guard, and the LocalPassword
provider. No DB, no network — DB-touching functions are monkeypatched."""
import pytest

from dba_slack_bot import config as cfg
from dba_slack_bot import local_users, passwords
from dba_slack_bot.web import auth_providers


# ---- password hashing -------------------------------------------------------

def test_hash_verify_roundtrip():
    h = passwords.hash_password("correct horse battery staple")
    assert passwords.verify_password("correct horse battery staple", h)
    assert not passwords.verify_password("wrong password", h)


def test_hash_is_never_cleartext():
    """The stored string must not contain the password in any form — this is
    the whole point: the DB never holds a cleartext password."""
    secret = "S3cr3t-Passw0rd!"
    h = passwords.hash_password(secret)
    assert secret not in h
    assert h.startswith("pbkdf2_sha256$")
    # Format: algo$iterations$salt$hash — four fields.
    assert len(h.split("$")) == 4


def test_salt_makes_hashes_unique():
    a = passwords.hash_password("same-password")
    b = passwords.hash_password("same-password")
    assert a != b                      # different random salt
    assert passwords.verify_password("same-password", a)
    assert passwords.verify_password("same-password", b)


def test_empty_password_rejected():
    with pytest.raises(ValueError):
        passwords.hash_password("")


def test_verify_malformed_fails_closed():
    assert not passwords.verify_password("x", "not-a-valid-hash")
    assert not passwords.verify_password("x", "")
    assert not passwords.verify_password("x", "bcrypt$12$abc$def")  # wrong algo
    assert not passwords.verify_password("x", None)  # type: ignore[arg-type]


def test_needs_rehash():
    weak = passwords.hash_password("pw", iterations=1000)
    strong = passwords.hash_password("pw")
    assert passwords.needs_rehash(weak)         # fewer iterations than current
    assert not passwords.needs_rehash(strong)
    assert passwords.needs_rehash("garbage")    # unparseable -> rehash


# ---- identity namespacing ---------------------------------------------------

def test_identity_helpers():
    assert local_users.to_identity("Alice") == "local:alice"       # lowercased
    assert local_users.to_identity("  bob ") == "local:bob"        # trimmed
    assert local_users.is_local_identity("local:alice")
    assert not local_users.is_local_identity("U0EXAMPLE01")        # a Slack id
    assert local_users.username_of("local:alice") == "alice"
    assert local_users.username_of("U0EXAMPLE01") is None


# ---- verify_login (DB monkeypatched) ---------------------------------------

def _row(pw, *, enabled=True):
    return {"username": "alice", "password_hash": passwords.hash_password(pw),
            "display_name": "Alice", "email": "a@x.io", "enabled": enabled,
            "must_change_pw": False}


def test_verify_login_ok(monkeypatch):
    monkeypatch.setattr(local_users, "get", lambda u: _row("hunter2"))
    row = local_users.verify_login("alice", "hunter2")
    assert row and row["username"] == "alice"


def test_verify_login_wrong_password(monkeypatch):
    monkeypatch.setattr(local_users, "get", lambda u: _row("hunter2"))
    assert local_users.verify_login("alice", "nope") is None


def test_verify_login_disabled_account(monkeypatch):
    monkeypatch.setattr(local_users, "get", lambda u: _row("hunter2", enabled=False))
    assert local_users.verify_login("alice", "hunter2") is None


def test_verify_login_unknown_user_runs_kdf(monkeypatch):
    """Unknown username -> None, but the KDF still runs against a dummy hash
    (no early return) so timing does not leak account existence."""
    called = {"kdf": 0}
    real_verify = passwords.verify_password

    def counting_verify(pw, stored):
        called["kdf"] += 1
        return real_verify(pw, stored)

    monkeypatch.setattr(local_users, "get", lambda u: None)
    monkeypatch.setattr(local_users.passwords, "verify_password", counting_verify)
    assert local_users.verify_login("ghost", "whatever") is None
    assert called["kdf"] == 1


# ---- LocalPassword provider -------------------------------------------------

def _toggle(monkeypatch, value):
    def fake(key, default=None):
        if key == "web_auth_local_enabled":
            return value
        return default if default is not None else ""
    monkeypatch.setattr(cfg, "get_setting", fake)


def test_local_provider_registry_toggle(monkeypatch):
    _toggle(monkeypatch, "on")
    assert "local" in auth_providers.enabled_providers()
    p = auth_providers.get_provider("local")
    assert p is not None and p.kind == "password"

    _toggle(monkeypatch, "off")
    assert "local" not in auth_providers.enabled_providers()
    assert auth_providers.get_provider("local") is None


def test_local_provider_verify_maps_identity(monkeypatch):
    _toggle(monkeypatch, "on")
    monkeypatch.setattr(local_users, "verify_login",
                        lambda u, pw: {"username": "alice", "display_name": "Alice",
                                       "email": "a@x.io"})
    p = auth_providers.get_provider("local")
    ident = p.verify("alice", "hunter2")
    assert ident.principal_id == "local:alice"    # namespaced principal id
    assert ident.provider == "local"
    assert ident.name == "Alice"
    assert ident.avatar is None


def test_local_provider_bad_credentials(monkeypatch):
    _toggle(monkeypatch, "on")
    monkeypatch.setattr(local_users, "verify_login", lambda u, pw: None)
    p = auth_providers.get_provider("local")
    with pytest.raises(auth_providers.AuthError) as e:
        p.verify("alice", "wrong")
    assert e.value.code == "bad_credentials"


# ---- self-service password change (7.2) ------------------------------------

def _pw_row(pw, *, enabled=True):
    return {"username": "alice", "password_hash": passwords.hash_password(pw),
            "display_name": "Alice", "email": None, "enabled": enabled,
            "must_change_pw": False}


def test_change_password_success(monkeypatch):
    saved = {}
    monkeypatch.setattr(local_users, "get", lambda u: _pw_row("oldpass12"))
    monkeypatch.setattr(local_users, "set_password",
                        lambda u, h, must_change_pw=False: saved.update(hash=h, mcp=must_change_pw))
    assert local_users.change_password("alice", "oldpass12", "newpass34") == ""
    # stored a NEW hash that verifies the new password, and cleared the flag
    assert passwords.verify_password("newpass34", saved["hash"])
    assert saved["mcp"] is False


def test_change_password_wrong_current(monkeypatch):
    monkeypatch.setattr(local_users, "get", lambda u: _pw_row("oldpass12"))
    monkeypatch.setattr(local_users, "set_password",
                        lambda *a, **k: pytest.fail("must not set on bad current"))
    assert local_users.change_password("alice", "WRONG", "newpass34") == "bad_credentials"


def test_change_password_too_short(monkeypatch):
    monkeypatch.setattr(local_users, "get", lambda u: _pw_row("oldpass12"))
    assert local_users.change_password("alice", "oldpass12", "short") == "weak_password"


def test_change_password_same(monkeypatch):
    monkeypatch.setattr(local_users, "get", lambda u: _pw_row("oldpass12"))
    assert local_users.change_password("alice", "oldpass12", "oldpass12") == "same_password"


def test_change_password_unknown_or_disabled(monkeypatch):
    monkeypatch.setattr(local_users, "get", lambda u: None)
    assert local_users.change_password("ghost", "x"*9, "y"*9) == "bad_credentials"
    monkeypatch.setattr(local_users, "get", lambda u: _pw_row("oldpass12", enabled=False))
    assert local_users.change_password("alice", "oldpass12", "newpass34") == "bad_credentials"
