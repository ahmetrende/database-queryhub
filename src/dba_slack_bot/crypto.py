"""Fernet encryption using master keys stored on disk.

The master key file (default: /etc/slackbot/master.key) holds one url-safe
base64-encoded 32-byte key PER LINE. To migrate the bot to a new host, copy
this file plus set BOT_DB_* env vars. All encrypted secrets live in the DB and
become immediately decryptable on the new host.

Multiple lines exist so the key can be ROTATED without downtime. Before, this
module built a single Fernet from a single key, and there was no transition
window: an operator who needed to rotate — key exposed, staff change, a policy
that says every 90 days — had to stop the bot, re-encrypt every secret by hand,
and start it again, with no way back if a value failed to decrypt halfway
through. In practice that means the key never gets rotated.

Now the file is read as a KEY RING under MultiFernet:

    <new key>      <- line 1: the PRIMARY. Everything new is encrypted with it.
    <old key>      <- still accepted for decryption.

Decryption tries every key in order, so old ciphertext keeps working while
new writes use the primary. The rotation is therefore:

    1. prepend a new key, leaving the old line in place
    2. restart (both services) — new writes now use the new key
    3. python scripts/rotate_master_key.py --apply   (re-encrypt everything)
    4. delete the old line, restart again

Step 3 is resumable and verifies each value round-trips before committing, and
until step 4 the old key still works — so an interrupted rotation leaves a
working system rather than an unreadable one. See docs/KEY_ROTATION.md.

A single-line file is a key ring of one and behaves exactly as before, so
existing installs need no action.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


def _master_key_path() -> Path:
    """Resolve the master key path lazily so importing this module does not
    require the full bot env (`SLACK_BOT_TOKEN`, BOT_DB_*) to be set. The
    encryption helper used at admin time only needs this single path.
    """
    return Path(os.environ.get("MASTER_KEY_PATH", "/etc/slackbot/master.key"))


def _read_key(path: Path) -> bytes:
    """The PRIMARY key — the one new ciphertext is encrypted with.

    Kept for callers that genuinely need one key (secrets_store writes the
    env-secrets file with the primary). Use `read_keys()` for anything that
    must also DECRYPT, or rotation breaks it.
    """
    return read_keys(path)[0]


def read_keys(path: Path | None = None) -> list[bytes]:
    """Every key in the file, in order. Index 0 is the primary.

    Blank lines and `#` comments are skipped, so an operator can label which
    key is which during a rotation — that label is the difference between a
    confident step 4 and a nervous one.
    """
    path = path or _master_key_path()
    if not path.exists():
        raise FileNotFoundError(
            f"Master key not found at {path}. Generate one with "
            f"`python -c 'from cryptography.fernet import Fernet; "
            f"print(Fernet.generate_key().decode())'` and write it to that path."
        )
    if os.name == "posix":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(
                f"Master key {path} is too permissive ({oct(mode)}); "
                f"run: chmod 600 {path}"
            )
    keys: list[bytes] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        keys.append(line.encode("ascii"))
    if not keys:
        raise ValueError(
            f"Master key file {path} contains no keys. Expected one url-safe "
            f"base64 32-byte key per line, primary first."
        )
    return keys


_FERNET: MultiFernet | None = None


def fernet() -> MultiFernet:
    """The key ring. Encrypts with the primary, decrypts with any key in it."""
    global _FERNET
    if _FERNET is None:
        keys = read_keys(_master_key_path())
        try:
            _FERNET = MultiFernet([Fernet(k) for k in keys])
        except Exception as e:
            # cryptography's own message for a malformed key is opaque
            # ("Fernet key must be 32 url-safe base64-encoded bytes"), and with
            # several lines in the file it does not say WHICH line — during a
            # rotation that is the only thing you want to know.
            for i, k in enumerate(keys, start=1):
                try:
                    Fernet(k)
                except Exception:
                    raise ValueError(
                        f"Line {i} of {_master_key_path()} is not a valid Fernet "
                        f"key (expected 32 url-safe base64-encoded bytes)."
                    ) from e
            raise
    return _FERNET


def reset_cache() -> None:
    """Drop the cached key ring so the next call re-reads the file.

    The rotation script edits the file mid-run and must not keep using the ring
    it loaded at import time.
    """
    global _FERNET
    _FERNET = None


def encrypt(plaintext: str) -> str:
    return fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    try:
        return fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        # With a key ring, "wrong key" means NO key in the file worked — say so,
        # because the fix during a rotation is usually "you deleted the old line
        # too early", not "you have the wrong key".
        n = len(read_keys())
        raise RuntimeError(
            f"Failed to decrypt — none of the {n} key(s) in "
            f"{_master_key_path()} can read this ciphertext. If you are mid-"
            f"rotation, the key that wrote it may have been removed from the "
            f"file; restore that line and run scripts/rotate_master_key.py."
        ) from e


def encrypted_with_primary(ciphertext: str) -> bool:
    """True if this ciphertext was written with the CURRENT primary key.

    How the rotation script knows what still needs re-encrypting, and how it
    stays resumable: a run that is interrupted can be repeated and will skip
    what it already did. There is no metadata to consult, so this decides by
    trying the primary alone.
    """
    try:
        Fernet(read_keys()[0]).decrypt(ciphertext.encode("ascii"))
        return True
    except Exception:
        return False
