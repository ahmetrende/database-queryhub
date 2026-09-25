"""Environment + DB-backed runtime configuration."""
from __future__ import annotations

import fnmatch
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _maybe_load_encrypted_secrets() -> None:
    """If `/etc/queryhub/secrets.enc` exists, decrypt it with the master
    key and push its contents into os.environ — but only for keys that
    are not already set (so an explicit env var still wins, useful for
    testing). Silent no-op if the file doesn't exist (backward compat
    with plaintext-env deployments).

    A file that exists and cannot be read stops the process. It used to log
    and carry on with the plaintext environment, which starts the service on
    whatever stale credentials that environment still holds while the
    operator believes the encrypted store is in use. Set
    QH_SECRETS_PLAINTEXT_FALLBACK=1 to allow that on purpose, for the move
    from a plaintext env file to the encrypted one. `manage_env_secrets.py`
    does not import this module, so the file can still be repaired."""
    from . import secrets_store
    if not secrets_store.exists():
        return
    try:
        secrets = secrets_store.load()
    except Exception as e:
        path = secrets_store.default_path()
        if os.environ.get("QH_SECRETS_PLAINTEXT_FALLBACK", "").strip().lower() in {
                "1", "true", "yes", "on"}:
            logging.getLogger(__name__).error(
                "Failed to decrypt %s: %s. Falling back to plaintext env "
                "(QH_SECRETS_PLAINTEXT_FALLBACK is set).", path, e)
            return
        raise RuntimeError(
            f"{path} exists but cannot be read ({type(e).__name__}: {e}). Fix "
            f"the file or MASTER_KEY_PATH, or set QH_SECRETS_PLAINTEXT_FALLBACK=1 "
            f"to start from the plaintext environment.") from e
    for k, v in secrets.items():
        if not os.environ.get(k):
            os.environ[k] = v


_maybe_load_encrypted_secrets()


@dataclass(frozen=True)
class EnvConfig:
    slack_bot_token: str
    slack_app_token: str
    bot_db_host: str
    bot_db_port: int
    bot_db_name: str
    bot_db_user: str
    bot_db_password: str
    master_key_path: Path
    log_level: str
    # TLS for the metadata DB (and the inventory DB on the same server). Empty
    # leaves libpq's default. Named settings rather than PGSSLROOTCERT: libpq
    # would apply that variable to target connections too, and a root file
    # turns their `require` into verify-ca against the wrong CA.
    bot_db_sslmode: str = ""
    bot_db_sslrootcert: str = ""

    def bot_db_ssl_kwargs(self) -> dict:
        """psycopg kwargs for a connection to the metadata DB's server."""
        kw = {}
        if self.bot_db_sslmode:
            kw["sslmode"] = self.bot_db_sslmode
        if self.bot_db_sslrootcert:
            kw["sslrootcert"] = self.bot_db_sslrootcert
        return kw

    @property
    def slack_enabled(self) -> bool:
        """True when Slack transport is configured. Slack is an OPTIONAL
        channel: with no bot token the app runs in the vanilla profile
        (web-only approval + in-app notifications), and every Slack call
        no-ops. The Slack bot process (main.py) requires it and asserts so
        at startup; the web process and executor tolerate its absence."""
        return bool(self.slack_bot_token)

    @classmethod
    def from_env(cls) -> "EnvConfig":
        def need(key: str) -> str:
            v = os.environ.get(key)
            if not v:
                raise RuntimeError(f"Missing required env var: {key}")
            return v

        return cls(
            # Slack tokens are OPTIONAL — absent means the vanilla (no-Slack)
            # profile. The Slack bot entrypoint validates their presence.
            slack_bot_token=os.environ.get("SLACK_BOT_TOKEN", ""),
            slack_app_token=os.environ.get("SLACK_APP_TOKEN", ""),
            bot_db_host=need("BOT_DB_HOST"),
            bot_db_port=int(os.environ.get("BOT_DB_PORT", "5432")),
            bot_db_name=need("BOT_DB_NAME"),
            bot_db_user=need("BOT_DB_USER"),
            bot_db_password=need("BOT_DB_PASSWORD"),
            master_key_path=Path(os.environ.get("MASTER_KEY_PATH", "/etc/queryhub/master.key")),
            # LOG_LEVEL is read from env at startup (logging is configured
            # before the DB pool is open). All OTHER tunables live in
            # bot_config — see migration 007 and `queryhub.config.get_setting`.
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            bot_db_sslmode=os.environ.get("BOT_DB_SSLMODE", "").strip(),
            bot_db_sslrootcert=os.environ.get("BOT_DB_SSLROOTCERT", "").strip(),
        )


ENV = EnvConfig.from_env()


# Short-lived cache for bot_config reads.
#
# There are ~90 call sites and several fire per request — the live fleet shows
# over a million sequential scans of a 61-row table, and the same pattern on the
# 2-row `admins` table. That is not an indexing problem (Postgres is right to
# seq-scan a 61-row table); it is one round trip per read. A few seconds of
# caching removes almost all of them.
#
# The TTL is deliberately tiny so the documented contract — "bot_config keys are
# runtime-effective, no restart needed" — still holds: a change takes effect
# within seconds, not on restart. Longer caching would quietly turn the kill
# switch into a stale value, which is exactly the wrong thing to be lazy about.
_CACHE_TTL_SECONDS = 5.0
_cache: dict[str, tuple[float, str | None]] = {}
_cache_lock = threading.Lock()


def invalidate_cache() -> None:
    """Drop cached bot_config values. Called after a write so an operator sees
    their own change immediately rather than up to a TTL later."""
    with _cache_lock:
        _cache.clear()


def get_setting(key: str, default: str | None = None) -> str:
    """Read a value from the bot_config table (cached for a few seconds)."""
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        value = hit[1]
    else:
        from .db import fetch_one
        row = fetch_one("SELECT value FROM bot_config WHERE key = %s", (key,))
        value = row["value"] if row is not None else None
        with _cache_lock:
            _cache[key] = (now + _CACHE_TTL_SECONDS, value)
    if value is None:
        if default is None:
            raise KeyError(f"bot_config key not set: {key}")
        return default
    return value


def get_int(key: str, default: int) -> int:
    return int(get_setting(key, str(default)))


def get_bool(key: str, default: bool) -> bool:
    return get_setting(key, "true" if default else "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


# The sslmodes under which libpq checks the server certificate.
_VERIFYING_MODES = frozenset({"verify-ca", "verify-full"})


def _host_globs(key: str) -> list[str]:
    raw = get_setting(key, "") or ""
    return [p.lower() for p in re.split(r"[,\s]+", raw.strip()) if p]


def target_tls_rule(host: str | None) -> bool | None:
    """What the TLS host lists say about `host`: True when it must verify the
    server certificate, False when it is exempt, None when neither list names
    it (the engine's own default applies). Exempt beats verify."""
    if not host:
        return None
    name = host.strip().lower()
    if any(fnmatch.fnmatchcase(name, g)
           for g in _host_globs("target_ssl_verify_exempt_hosts")):
        return False
    if any(fnmatch.fnmatchcase(name, g)
           for g in _host_globs("target_ssl_verify_hosts")):
        return True
    return None


def target_ssl_kwargs(host: str | None = None) -> dict:
    """psycopg SSL connect kwargs for a connection to a *target* database.

    `target_ssl_mode` (default require) is the fleet-wide mode. require
    encrypts the link but does not check who answered. Two host-glob lists
    (comma or space separated, like the target policy keys) change it per
    server, so a fleet can move to verification one cloud, or one host, at a
    time:

      target_ssl_verify_hosts          these hosts use verify-full, checked
                                       against the CA file in
                                       `target_ssl_rootcert`
      target_ssl_verify_exempt_hosts   these never verify: a server whose
                                       certificate cannot be checked. Exempt
                                       beats verify, and beats a fleet-wide
                                       verifying mode

    `host` is the name the connection goes to. For a query sent to a read
    replica that is the replica's host, not the primary's. A caller that
    passes no host gets the fleet-wide mode.

    The CA file is passed only with a verifying mode. libpq treats `require`
    plus a root file as verify-ca, so handing one cloud's bundle to a server
    that runs `require` would fail every connection to it.
    Returns only the keys that are set, so it spreads cleanly into connect()."""
    mode = (get_setting("target_ssl_mode", "require") or "require").strip()
    rule = target_tls_rule(host)
    if rule is False and mode in _VERIFYING_MODES:
        mode = "require"
    elif rule is True:
        mode = "verify-full"
    kwargs: dict = {"sslmode": mode}
    rootcert = (get_setting("target_ssl_rootcert", "") or "").strip()
    if rootcert and mode in _VERIFYING_MODES:
        kwargs["sslrootcert"] = rootcert
    return kwargs
