"""Environment + DB-backed runtime configuration."""
from __future__ import annotations

import logging
import os
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
    with plaintext-env deployments)."""
    from . import secrets_store
    if not secrets_store.exists():
        return
    try:
        secrets = secrets_store.load()
    except Exception as e:
        # Don't crash startup on a malformed file — log and let env-var
        # fallback handle missing values (which raises a clear error).
        logging.getLogger(__name__).error(
            "Failed to decrypt %s: %s. Falling back to plaintext env.",
            secrets_store.default_path(), e,
        )
        return
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


def target_ssl_kwargs() -> dict:
    """psycopg SSL connect kwargs for connections to *target* databases.

    Defaults to sslmode=require: the link is encrypted but the server
    certificate is NOT authenticated (no MITM protection) — the historical
    behavior. A security-conscious deployment sets bot_config
    `target_ssl_mode=verify-full` and points `target_ssl_rootcert` at a CA
    bundle (e.g. the RDS global bundle) to also authenticate the server.
    Returns only the keys that are set, so it spreads cleanly into connect()."""
    mode = (get_setting("target_ssl_mode", "require") or "require").strip()
    kwargs: dict = {"sslmode": mode}
    rootcert = (get_setting("target_ssl_rootcert", "") or "").strip()
    if rootcert:
        kwargs["sslrootcert"] = rootcert
    return kwargs
