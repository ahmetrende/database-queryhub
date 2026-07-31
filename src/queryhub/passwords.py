"""Password hashing for local (built-in) accounts.

Local accounts are the vanilla profile's login (no Slack, no external IdP).
Passwords are NEVER stored in cleartext: only a salted, iterated
PBKDF2-HMAC-SHA256 digest is persisted, in a self-describing string that
carries the algorithm, iteration count and salt so the work factor can be
raised later without invalidating existing hashes:

    pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>

PBKDF2 is chosen deliberately: it ships in the Python standard library, so
the base install needs no extra crypto dependency (argon2/bcrypt would add
one, against the vanilla-profile goal of zero external vendors). The
iteration count follows current OWASP guidance for PBKDF2-HMAC-SHA256.
Verification is constant-time.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_ALGO = "pbkdf2_sha256"
# OWASP (2023) floor for PBKDF2-HMAC-SHA256. Stored per hash, so this can be
# raised over time; old hashes keep verifying against their own recorded count.
_ITERATIONS = 600_000
_SALT_BYTES = 16


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(txt: str) -> bytes:
    return base64.b64decode(txt.encode("ascii"))


def hash_password(password: str, *, iterations: int = _ITERATIONS) -> str:
    """Return a self-describing PBKDF2 hash string. Raises on empty input so
    a blank password can never be silently accepted."""
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGO}${iterations}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify. Returns False (never raises) on any malformed or
    unknown-algorithm hash, so a corrupt row fails closed."""
    try:
        algo, iters, salt_b64, hash_b64 = stored.split("$", 3)
        if algo != _ALGO:
            return False
        iterations = int(iters)
        salt = _unb64(salt_b64)
        expected = _unb64(hash_b64)
    except (ValueError, TypeError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"),
                             salt, iterations)
    return hmac.compare_digest(dk, expected)


def needs_rehash(stored: str, *, iterations: int = _ITERATIONS) -> bool:
    """True if the stored hash uses an older algorithm or a weaker work factor
    than the current default — the caller may re-hash on next successful login."""
    try:
        algo, iters, _, _ = stored.split("$", 3)
        return algo != _ALGO or int(iters) < iterations
    except (ValueError, TypeError, AttributeError):
        return True
