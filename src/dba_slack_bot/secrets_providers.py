"""Pluggable credential providers for target databases.

A target's DB credentials can come from more than one place. The default
provider, ``local``, keeps the historical behavior: per-tier credentials live
on ``target_servers`` as Fernet-encrypted columns and are decrypted with the
master key — no external dependency, so a stock install needs nothing but the
metadata database. Optional providers fetch credentials from an external secret
store at execution time, so the ciphertext never has to live in the metadata
DB at all.

The provider is chosen **per target** (``target_servers.secrets_provider``);
when unset, ``local`` is used. Resolution happens inside
``targets.get_credentials()``, so the executor, the SQL Server path and
pre-flight all go through the same choke point unchanged.

Adding a provider is: implement the small ``SecretsProvider`` protocol and
register it in ``_REGISTRY``. Cloud SDKs are imported lazily inside their
provider, so they are never a hard dependency of the base install.
"""
from __future__ import annotations

import json
import time
from typing import Protocol

from .crypto import decrypt

_TIERS = ("ro", "rw", "ddl")


class SecretsProviderError(RuntimeError):
    """A provider could not resolve credentials for a reason that is not
    'the target simply hasn't configured this tier' (that stays a
    ``LookupError``, matching the historical local-vault contract)."""


class SecretsProvider(Protocol):
    name: str

    def get_credentials(self, row: dict, mode: str) -> tuple[str, str]:
        """Return ``(username, plaintext_password)`` for ``mode`` on the target
        described by ``row`` (a ``target_servers`` row as a dict). Raise
        ``LookupError`` if this target/tier has no credentials configured."""
        ...


class LocalVaultProvider:
    """Default provider: per-tier Fernet-encrypted columns on target_servers."""

    name = "local"
    _COLS = {
        "ro": ("username", "password_encrypted"),
        "rw": ("username_rw", "password_rw_encrypted"),
        "ddl": ("username_ddl", "password_ddl_encrypted"),
    }

    def get_credentials(self, row: dict, mode: str) -> tuple[str, str]:
        ucol, pcol = self._COLS[mode]
        username, ciphertext = row.get(ucol), row.get(pcol)
        if not username or not ciphertext:
            raise LookupError(
                f"target {row.get('id', '?')} has no local credentials for mode={mode}"
            )
        return username, decrypt(ciphertext)


class AwsSecretsManagerProvider:
    """Fetch a target's credentials from AWS Secrets Manager at execution time.

    ``secrets_ref`` shape:
        ``{"secret_id": "<arn-or-name>", "region": "<optional>"}``
    The secret's ``SecretString`` is JSON with per-tier credentials:
        ``{"ro": {"username": .., "password": ..}, "rw": {..}, "ddl": {..}}``

    ``boto3`` is imported lazily, so it is only required on a host that
    actually points a target at this provider (``pip install '.[aws]'``).
    Fetched secrets are cached briefly in-process to avoid a Secrets Manager
    call on every query; the TTL is bot_config ``awssm_cache_ttl_seconds``
    (default 60), so a deployment with aggressive rotation can shorten it —
    or set it to 0 to always fetch fresh.
    """

    name = "awssm"

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, dict]] = {}

    def _cache_ttl(self) -> float:
        from . import config as cfg
        return float(cfg.get_int("awssm_cache_ttl_seconds", 60))

    def _fetch(self, secret_id: str, region: str | None) -> dict:
        now = time.monotonic()
        ttl = self._cache_ttl()
        cached = self._cache.get(secret_id)
        if ttl > 0 and cached and cached[0] > now:
            return cached[1]
        try:
            import boto3  # lazy: never a hard dependency of the base install
        except ImportError as e:  # pragma: no cover - depends on host extras
            raise SecretsProviderError(
                "the 'awssm' secrets provider needs boto3 — install the [aws] extra"
            ) from e
        client = boto3.client(
            "secretsmanager", **({"region_name": region} if region else {})
        )
        resp = client.get_secret_value(SecretId=secret_id)
        raw = resp.get("SecretString")
        if raw is None:
            raise SecretsProviderError(f"secret {secret_id} has no SecretString")
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as e:
            raise SecretsProviderError(
                f"secret {secret_id} is not the expected JSON {{tier: {{username, password}}}}"
            ) from e
        if ttl > 0:
            self._cache[secret_id] = (now + ttl, data)
        return data

    def get_credentials(self, row: dict, mode: str) -> tuple[str, str]:
        ref = row.get("secrets_ref") or {}
        if isinstance(ref, str):
            try:
                ref = json.loads(ref)
            except json.JSONDecodeError as e:
                raise SecretsProviderError("secrets_ref is not valid JSON") from e
        secret_id = ref.get("secret_id")
        if not secret_id:
            raise LookupError(
                f"target {row.get('id', '?')} has no secrets_ref.secret_id for awssm"
            )
        data = self._fetch(secret_id, ref.get("region"))
        tier = data.get(mode) or {}
        username, password = tier.get("username"), tier.get("password")
        if not username or not password:
            raise LookupError(
                f"awssm secret {secret_id} has no credentials for mode={mode}"
            )
        return username, password


# Registered providers, keyed by the value stored in
# target_servers.secrets_provider. Add a provider here to make it selectable.
_REGISTRY: dict[str, SecretsProvider] = {
    p.name: p for p in (LocalVaultProvider(), AwsSecretsManagerProvider())
}


def resolve_credentials(row: dict, mode: str) -> tuple[str, str]:
    """Dispatch to the target's configured secrets provider. ``row`` is a
    ``target_servers`` row (dict) that must include ``secrets_provider`` and
    ``secrets_ref`` plus whatever columns the provider needs."""
    if mode not in _TIERS:
        raise ValueError(f"unknown mode: {mode}")
    name = (row.get("secrets_provider") or "local").strip().lower()
    provider = _REGISTRY.get(name)
    if provider is None:
        raise SecretsProviderError(f"unknown secrets provider: {name!r}")
    return provider.get_credentials(row, mode)
