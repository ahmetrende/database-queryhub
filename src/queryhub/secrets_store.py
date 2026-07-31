"""Encrypted at-rest store for env-level secrets (Slack tokens, bot DB
password). Uses the same Fernet master key as target server credentials.

File format on disk (text, single line per field, mode 0600):

    SLBOT_SECRETS_v1
    <fernet_token>

The fernet_token is a base64 blob produced by `cryptography.fernet.Fernet.
encrypt(json_bytes)`. The decrypted plaintext is a JSON object mapping
env-var name -> string value, e.g.:

    {"SLACK_BOT_TOKEN": "xoxb-...",
     "SLACK_APP_TOKEN": "xapp-...",
     "BOT_DB_PASSWORD": "..."}

The plaintext header (`SLBOT_SECRETS_v1`) is informational — it lets a
human or grep identify what the file is, and lets us evolve the format
later (v2, v3, ...) without breaking existing readers. It is NOT a
security boundary; the security comes from Fernet + master.key.

Threat model (same as `crypto.py`): protects against accidental leaks
(backup tools, log scrapers, screenshare cat) and any reader without
master.key. Does NOT protect against a local compromise that can read
both the encrypted file and master.key — that's the same boundary
PSCred / DPAPI sit at.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from .crypto import _master_key_path, read_keys

MAGIC_LINE = "SLBOT_SECRETS_v1"

# Env vars that are eligible for encrypted storage. Anything outside this
# set is rejected by the CLI to prevent typos and to keep the secrets
# surface explicit.
KNOWN_KEYS = (
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "BOT_DB_PASSWORD",
)


def default_path() -> Path:
    return Path(os.environ.get("SECRETS_ENC_PATH", "/etc/queryhub/secrets.enc"))


def _fernet() -> MultiFernet:
    """The whole key ring, not just the primary — this file has to stay readable
    mid-rotation.

    Re-read every time at CLI scope (small perf cost, simpler invariants). The
    bot's runtime path goes through crypto.fernet() which caches.

    It matters that this is the RING: step 2 of a key rotation is "prepend the
    new key and restart", and at that moment this file is still encrypted with
    the OLD key. Reading it with the primary alone would make the bot fail to
    start — the credentials it needs to boot would be the first casualty of the
    rotation that was supposed to be non-disruptive. MultiFernet encrypts with
    the primary and decrypts with any key, which is exactly the requirement.
    """
    return MultiFernet([Fernet(k) for k in read_keys(_master_key_path())])


def exists(path: Path | None = None) -> bool:
    return (path or default_path()).exists()


def load(path: Path | None = None) -> dict[str, str]:
    """Read and decrypt the secrets file. Returns {key: value}.
    Raises FileNotFoundError, ValueError (bad format), or
    cryptography.fernet.InvalidToken (wrong master key)."""
    p = path or default_path()
    if not p.exists():
        raise FileNotFoundError(f"Secrets file not found: {p}")

    if os.name == "posix":
        mode = stat.S_IMODE(p.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(
                f"Secrets file {p} is too permissive ({oct(mode)}); "
                f"run: chmod 600 {p}"
            )

    text = p.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != MAGIC_LINE:
        raise ValueError(
            f"Secrets file at {p} is missing magic header '{MAGIC_LINE}' "
            f"on first line — not a valid secrets file."
        )
    body = "\n".join(lines[1:]).strip()
    if not body:
        raise ValueError(f"Secrets file at {p} has no ciphertext body.")

    try:
        plaintext = _fernet().decrypt(body.encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise InvalidToken(
            "Failed to decrypt secrets file — wrong master key?"
        ) from e

    obj = json.loads(plaintext)
    if not isinstance(obj, dict):
        raise ValueError(f"Decrypted secrets payload is not a JSON object.")
    return {str(k): str(v) for k, v in obj.items()}


def save(secrets: dict[str, str], path: Path | None = None) -> Path:
    """Encrypt and write `secrets` to disk atomically. Backs up any
    existing file to `<path>.replaced[_N]` before overwriting. Sets mode
    0600. Returns the resolved path written.

    Validates that all keys are in KNOWN_KEYS — anything else raises
    ValueError to prevent typos like 'SLACK_BOT_TOKN'."""
    for k in secrets:
        if k not in KNOWN_KEYS:
            raise ValueError(
                f"Unknown secret key {k!r}. Known keys: {KNOWN_KEYS}. "
                f"Edit secrets_store.KNOWN_KEYS to add a new one."
            )

    p = path or default_path()
    p.parent.mkdir(parents=True, exist_ok=True)

    plaintext = json.dumps(secrets, separators=(",", ":")).encode("utf-8")
    ciphertext = _fernet().encrypt(plaintext).decode("ascii")
    file_body = MAGIC_LINE + "\n" + ciphertext + "\n"

    # Backup existing
    if p.exists():
        backup = _next_backup_name(p, ".replaced")
        shutil.move(str(p), str(backup))

    # Atomic write: tmp file in same dir, fsync, rename
    fd, tmp_name = tempfile.mkstemp(prefix=".secrets.", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(file_body)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, str(p))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    return p


def remove(path: Path | None = None) -> Path:
    """Soft-delete the secrets file (rename to <path>.deleted[_N]).
    Returns the new path. Caller must confirm with the user before
    calling."""
    p = path or default_path()
    if not p.exists():
        raise FileNotFoundError(f"Secrets file not found: {p}")
    target = _next_backup_name(p, ".deleted")
    shutil.move(str(p), str(target))
    return target


def _next_backup_name(p: Path, suffix: str) -> Path:
    candidate = Path(f"{p}{suffix}")
    n = 1
    while candidate.exists():
        candidate = Path(f"{p}{suffix}_{n}")
        n += 1
    return candidate


def metadata(path: Path | None = None) -> dict:
    """Return file-level metadata (path, mode, mtime, size, key list)
    without decrypting values. For `list` CLI output."""
    p = path or default_path()
    if not p.exists():
        return {"path": str(p), "exists": False}

    st = p.stat()
    info: dict = {
        "path": str(p),
        "exists": True,
        "mode": oct(stat.S_IMODE(st.st_mode)),
        "size_bytes": st.st_size,
        "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
    }
    try:
        secrets = load(p)
        info["keys"] = sorted(secrets.keys())
        info["status"] = "ok"
    except Exception as e:
        info["status"] = f"error: {type(e).__name__}: {e}"
    return info
