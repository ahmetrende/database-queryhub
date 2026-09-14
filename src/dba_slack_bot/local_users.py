"""Local (built-in) user accounts for the vanilla profile.

The canonical identity everywhere in QueryHub is a single TEXT column
(historically named slack_user_id, semantically the *principal id*). A local
account maps to the principal id `local:<username>`, which lives in the SAME
namespace as Slack ids and so flows through the whole authorization model —
requesters, admins, grants, teams, audit — unchanged. Migration 075 widens
the id-format CHECK on those tables to accept this form.

Credentials live HERE; authorization does NOT. A local user is allowed into
QueryHub only if they ALSO have an enabled `requesters` row (or an `admins`
row), exactly like a Slack user. Passwords are stored hashed (passwords.py) —
never cleartext. A local principal is never linked to a Slack id: it is a
distinct principal.
"""
from __future__ import annotations

import functools

from . import db, passwords

LOCAL_PREFIX = "local:"


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def to_identity(username: str) -> str:
    """username -> principal id used everywhere in the authz model."""
    return f"{LOCAL_PREFIX}{normalize_username(username)}"


def is_local_identity(identity: str) -> bool:
    return (identity or "").startswith(LOCAL_PREFIX)


def username_of(identity: str) -> str | None:
    """Inverse of to_identity: local:<username> -> username (else None)."""
    if not is_local_identity(identity):
        return None
    return identity[len(LOCAL_PREFIX):]


@functools.lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """A valid hash of a throwaway secret. verify_login runs the KDF against
    this for unknown usernames so response time does not reveal whether an
    account exists (user-enumeration guard). Computed once, lazily."""
    return passwords.hash_password("qh-local-enumeration-guard")


def get(username: str) -> dict | None:
    return db.fetch_one(
        "SELECT username, password_hash, display_name, email, enabled, "
        "       must_change_pw, created_at, last_login_at "
        "FROM local_users WHERE username = %s",
        (normalize_username(username),),
    )


def verify_login(username: str, password: str) -> dict | None:
    """Return the user row on a correct password for an ENABLED account, else
    None. Always runs the KDF (even for an unknown user) so timing does not
    leak whether the username exists."""
    row = get(username)
    stored = row["password_hash"] if row else _dummy_hash()
    ok = passwords.verify_password(password or "", stored)
    if not row or not row["enabled"] or not ok:
        return None
    return row


def create(username: str, password_hash: str, *,
           display_name: str | None = None, email: str | None = None,
           created_by: str | None = None, must_change_pw: bool = False) -> None:
    """Insert a new local account. `password_hash` MUST already be a
    passwords.hash_password() result — this function never sees cleartext."""
    db.execute(
        "INSERT INTO local_users "
        "  (username, password_hash, display_name, email, created_by, must_change_pw) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (normalize_username(username), password_hash, display_name, email,
         created_by, must_change_pw),
    )


def set_password(username: str, password_hash: str, *,
                 must_change_pw: bool = False) -> None:
    db.execute(
        "UPDATE local_users SET password_hash = %s, must_change_pw = %s "
        "WHERE username = %s",
        (password_hash, must_change_pw, normalize_username(username)),
    )


MIN_PASSWORD_LEN = 8


def change_password(username: str, current_password: str,
                    new_password: str) -> str:
    """Self-service password change: verify the current password against the
    stored hash, then store the new one (clearing must_change_pw). Returns an
    error code string on failure, or "" on success. Never reveals more than
    needed. The KDF work happens here; callers should throttle."""
    if len(new_password or "") < MIN_PASSWORD_LEN:
        return "weak_password"
    if new_password == current_password:
        return "same_password"
    row = get(username)
    if row is None or not row["enabled"]:
        return "bad_credentials"
    if not passwords.verify_password(current_password or "", row["password_hash"]):
        return "bad_credentials"
    set_password(username, passwords.hash_password(new_password),
                 must_change_pw=False)
    return ""


def touch_login(username: str) -> None:
    db.execute(
        "UPDATE local_users SET last_login_at = NOW() WHERE username = %s",
        (normalize_username(username),),
    )


def set_enabled(username: str, enabled: bool) -> None:
    db.execute(
        "UPDATE local_users SET enabled = %s WHERE username = %s",
        (enabled, normalize_username(username)),
    )


def exists(username: str) -> bool:
    return get(username) is not None


def list_users() -> list[dict]:
    return db.fetch_all(
        "SELECT username, display_name, email, enabled, created_at, last_login_at "
        "FROM local_users ORDER BY username")
