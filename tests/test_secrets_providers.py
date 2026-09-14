"""secrets_providers — pluggable target-credential resolution.

The default 'local' provider must behave exactly like the historical
Fernet-column path; the optional 'awssm' provider fetches from AWS Secrets
Manager with boto3 imported lazily (never a hard dependency). `decrypt` is
patched so these tests need no master key, and boto3 is faked so they need no
AWS.
"""
import json
import sys
import types

import pytest

from dba_slack_bot import secrets_providers as sp


# ---- local vault provider (default) ----------------------------------------

def test_default_and_local_use_the_fernet_columns(monkeypatch):
    monkeypatch.setattr(sp, "decrypt", lambda ct: "plain:" + ct)
    row = {
        "id": 7,
        "username": "app_ro", "password_encrypted": "ROCT",
        "username_rw": "app_rw", "password_rw_encrypted": "RWCT",
        "username_ddl": "app_ddl", "password_ddl_encrypted": "DDLCT",
        "secrets_provider": None, "secrets_ref": None,
    }
    assert sp.resolve_credentials(row, "ro") == ("app_ro", "plain:ROCT")
    assert sp.resolve_credentials(row, "rw") == ("app_rw", "plain:RWCT")
    assert sp.resolve_credentials(row, "ddl") == ("app_ddl", "plain:DDLCT")
    # explicit 'local' is identical to NULL
    row["secrets_provider"] = "local"
    assert sp.resolve_credentials(row, "ro") == ("app_ro", "plain:ROCT")


def test_local_missing_tier_raises_lookup(monkeypatch):
    monkeypatch.setattr(sp, "decrypt", lambda ct: ct)
    row = {"id": 1, "username": "u", "password_encrypted": "c",
           "username_rw": None, "password_rw_encrypted": None,
           "secrets_provider": None, "secrets_ref": None}
    with pytest.raises(LookupError):
        sp.resolve_credentials(row, "rw")


def test_unknown_mode_and_unknown_provider():
    with pytest.raises(ValueError):
        sp.resolve_credentials({"secrets_provider": "local"}, "admin")
    with pytest.raises(sp.SecretsProviderError):
        sp.resolve_credentials({"secrets_provider": "vault-does-not-exist"}, "ro")


# ---- AWS Secrets Manager provider (optional) -------------------------------

class _FakeSMClient:
    def __init__(self, payload):
        self._payload = payload
        self.calls = 0

    def get_secret_value(self, SecretId):
        self.calls += 1
        return {"SecretString": self._payload}


def _install_fake_boto3(monkeypatch, client):
    fake = types.ModuleType("boto3")
    captured = {}

    def _client(service, **kwargs):
        captured["service"] = service
        captured["kwargs"] = kwargs
        return client

    fake.client = _client
    monkeypatch.setitem(sys.modules, "boto3", fake)
    return captured


def test_awssm_fetches_tiered_secret(monkeypatch):
    secret = json.dumps({
        "ro": {"username": "sm_ro", "password": "pw_ro"},
        "rw": {"username": "sm_rw", "password": "pw_rw"},
    })
    client = _FakeSMClient(secret)
    captured = _install_fake_boto3(monkeypatch, client)
    prov = sp.AwsSecretsManagerProvider()  # fresh instance → clean cache
    monkeypatch.setitem(sp._REGISTRY, "awssm", prov)

    row = {"id": 9, "secrets_provider": "awssm",
           "secrets_ref": {"secret_id": "dba/queryhub/demo", "region": "eu-central-1"}}
    assert sp.resolve_credentials(row, "ro") == ("sm_ro", "pw_ro")
    assert sp.resolve_credentials(row, "rw") == ("sm_rw", "pw_rw")
    assert captured["service"] == "secretsmanager"
    assert captured["kwargs"] == {"region_name": "eu-central-1"}
    # second lookup is served from the in-process cache (one SM call total)
    assert client.calls == 1


def test_awssm_accepts_json_string_ref(monkeypatch):
    client = _FakeSMClient(json.dumps({"ddl": {"username": "d", "password": "p"}}))
    _install_fake_boto3(monkeypatch, client)
    prov = sp.AwsSecretsManagerProvider()
    monkeypatch.setitem(sp._REGISTRY, "awssm", prov)
    row = {"id": 3, "secrets_provider": "awssm",
           "secrets_ref": '{"secret_id": "dba/queryhub/demo"}'}
    assert sp.resolve_credentials(row, "ddl") == ("d", "p")


def test_awssm_missing_tier_raises_lookup(monkeypatch):
    client = _FakeSMClient(json.dumps({"ro": {"username": "u", "password": "p"}}))
    _install_fake_boto3(monkeypatch, client)
    prov = sp.AwsSecretsManagerProvider()
    monkeypatch.setitem(sp._REGISTRY, "awssm", prov)
    row = {"id": 3, "secrets_provider": "awssm",
           "secrets_ref": {"secret_id": "dba/queryhub/demo"}}
    with pytest.raises(LookupError):
        sp.resolve_credentials(row, "rw")


def test_awssm_requires_secret_id():
    row = {"id": 3, "secrets_provider": "awssm", "secrets_ref": {}}
    with pytest.raises(LookupError):
        sp.resolve_credentials(row, "ro")


def test_awssm_ttl_zero_always_refetches(monkeypatch):
    # awssm_cache_ttl_seconds=0 disables the in-process cache, so a
    # rotated secret is picked up on the very next query.
    from dba_slack_bot import config as cfg
    monkeypatch.setattr(cfg, "get_int",
                        lambda k, d=None: 0 if k == "awssm_cache_ttl_seconds" else d)
    client = _FakeSMClient(json.dumps({"ro": {"username": "u", "password": "p"}}))
    _install_fake_boto3(monkeypatch, client)
    prov = sp.AwsSecretsManagerProvider()
    monkeypatch.setitem(sp._REGISTRY, "awssm", prov)
    row = {"id": 1, "secrets_provider": "awssm",
           "secrets_ref": {"secret_id": "dba/queryhub/demo"}}
    sp.resolve_credentials(row, "ro")
    sp.resolve_credentials(row, "ro")
    assert client.calls == 2  # no caching → each lookup hits Secrets Manager
