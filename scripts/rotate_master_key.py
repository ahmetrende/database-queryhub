#!/usr/bin/env python3
"""Re-encrypt every stored secret with the current PRIMARY master key.

Step 3 of the rotation described in src/queryhub/crypto.py and
docs/KEY_ROTATION.md. Prerequisite: the new key is already line 1 of the master
key file and the OLD key is still present on a later line. This script does not
generate or edit keys — it only moves ciphertext from the old key to the new one.

What it covers, i.e. everything encrypted with Fernet:

    target_servers.password_encrypted        (the RO credential)
    target_servers.password_rw_encrypted
    target_servers.password_ddl_encrypted
    the env-secrets file                     (SECRETS_ENC_PATH, if present)

local_users.password_hash is deliberately NOT here: it is a PBKDF2 hash, not
ciphertext, and cannot be re-encrypted — passwords survive a key rotation
untouched.

Safety, because this rewrites the only copy of credentials that open production
databases:

  * dry run by default. Nothing is written without --apply.
  * every value is decrypted, re-encrypted, and decrypted AGAIN and compared to
    the original plaintext before the transaction commits. A value that does not
    round-trip aborts the whole run.
  * the DB work is one transaction. Either every target moves to the new key or
    none does.
  * resumable: values already on the primary key are skipped, so a run that is
    interrupted can simply be repeated.
  * the secrets file is written to a temp file and renamed, after the same
    round-trip check, so an interrupted write cannot truncate it.
  * it refuses to run when the file holds only one key — with no old key there
    is nothing to rotate FROM, and the likely mistake is having deleted the old
    line before this step rather than after it.

Usage:
    set -a; source /etc/queryhub/env; set +a
    .venv/bin/python scripts/rotate_master_key.py            # dry run
    .venv/bin/python scripts/rotate_master_key.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from queryhub import crypto, db  # noqa: E402

PASSWORD_COLUMNS = (
    "password_encrypted",
    "password_rw_encrypted",
    "password_ddl_encrypted",
)


def _plan_targets() -> tuple[list[dict], int, int]:
    """(rows, values_needing_rotation, values_already_current)."""
    cols = ", ".join(PASSWORD_COLUMNS)
    rows = db.fetch_all(f"SELECT id, alias, {cols} FROM target_servers ORDER BY id")
    todo = current = 0
    for row in rows:
        for col in PASSWORD_COLUMNS:
            val = row.get(col)
            if not val:
                continue
            if crypto.encrypted_with_primary(val):
                current += 1
            else:
                todo += 1
    return rows, todo, current


def _rotate_value(ciphertext: str) -> str:
    """Decrypt with any key, re-encrypt with the primary, prove it round-trips.

    The second decrypt is the point. Without it a subtly wrong new key would be
    discovered the next time a query ran against that target — which is to say,
    in production, after the old key had been deleted.
    """
    plain = crypto.decrypt(ciphertext)
    fresh = crypto.encrypt(plain)
    if crypto.decrypt(fresh) != plain:
        raise RuntimeError("re-encrypted value did not round-trip")
    if not crypto.encrypted_with_primary(fresh):
        raise RuntimeError("re-encrypted value is not readable by the primary key")
    return fresh


def _rotate_secrets_file(path: Path, apply: bool) -> str:
    """Re-encrypt the env-secrets file. Returns a one-line status."""
    if not path.exists():
        return f"  secrets file {path}: not present, skipped"
    from queryhub import secrets_store

    try:
        values = secrets_store.load(path)
    except Exception as e:
        raise RuntimeError(f"could not read {path}: {e}") from e
    if not values:
        return f"  secrets file {path}: empty, skipped"

    if not apply:
        return (f"  secrets file {path}: {len(values)} value(s) would be "
                f"re-encrypted")

    # Write via temp + rename so an interrupted run cannot leave a half file
    # where the bot's credentials used to be.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".rotate-")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        secrets_store.save(values, tmp_path)
        os.chmod(tmp_path, 0o600)
        # Read it back through the same code path the bot uses.
        if secrets_store.load(tmp_path) != values:
            raise RuntimeError("re-written secrets file did not round-trip")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return f"  secrets file {path}: {len(values)} value(s) re-encrypted"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without it, this is a dry run.")
    ap.add_argument("--skip-secrets-file", action="store_true",
                    help="rotate the database only, not SECRETS_ENC_PATH")
    args = ap.parse_args()

    crypto.reset_cache()
    keys = crypto.read_keys()
    print(f"master key file: {crypto._master_key_path()}")
    print(f"keys on the ring: {len(keys)} (line 1 is the primary)\n")

    if len(keys) < 2:
        print("Only one key on the ring, so there is nothing to rotate FROM.\n"
              "Prepend the NEW key to the file, keep the old line, restart the\n"
              "services, then run this again. If you already deleted the old\n"
              "line, restore it from your backup first — otherwise any value\n"
              "still encrypted with it is unreadable.")
        return 1

    db.init_pool()
    rows, todo, current = _plan_targets()
    print(f"targets: {len(rows)}")
    print(f"  values already on the primary key: {current}")
    print(f"  values to re-encrypt:              {todo}")

    if not args.apply:
        if not args.skip_secrets_file:
            print()
            print(_rotate_secrets_file(_secrets_path(), apply=False))
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    if todo == 0 and args.skip_secrets_file:
        print("\nnothing to do.")
        return 0

    rotated = 0
    with db.transaction() as cur:
        for row in rows:
            updates = {}
            for col in PASSWORD_COLUMNS:
                val = row.get(col)
                if not val or crypto.encrypted_with_primary(val):
                    continue
                try:
                    updates[col] = _rotate_value(val)
                except Exception as e:
                    raise RuntimeError(
                        f"target {row['id']} ({row['alias']}) column {col}: {e}. "
                        f"Nothing has been committed."
                    ) from e
            if not updates:
                continue
            sets = ", ".join(f"{c} = %s" for c in updates)
            cur.execute(f"UPDATE target_servers SET {sets} WHERE id = %s",
                        (*updates.values(), row["id"]))
            rotated += len(updates)
            print(f"  target {row['id']:>3} {row['alias']}: "
                  f"{', '.join(updates)} re-encrypted")

        cur.execute(
            "INSERT INTO audit_log (actor_slack_id, actor_name, action, details) "
            "VALUES (%s, %s, 'master_key_rotated', %s::jsonb) RETURNING id",
            ("SYSTEM", "rotate_master_key.py", json.dumps({
                "values_rotated": rotated,
                "values_already_current": current,
                "keys_on_ring": len(keys),
                "targets_examined": len(rows),
            })))
        audit_id = cur.fetchone()["id"]

    print(f"\n{rotated} value(s) re-encrypted; audit_log id {audit_id}")

    if not args.skip_secrets_file:
        print(_rotate_secrets_file(_secrets_path(), apply=True))

    print("\nNow verify, THEN remove the old key line:")
    print("  1. restart both services and run a real query against a target")
    print("  2. delete every line after line 1 in the master key file")
    print("  3. restart again")
    print("Until step 2 the old key still works, so there is no rush.")
    return 0


def _secrets_path() -> Path:
    from queryhub import secrets_store
    return secrets_store.default_path()


if __name__ == "__main__":
    sys.exit(main())
