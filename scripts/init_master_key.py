"""Generate the Fernet master key with mandatory backup confirmation.

This is a one-time setup step. The master key decrypts every target server
password stored in the bot DB — losing it means re-entering every credential
manually. Refusing to write the key until the operator types its fingerprint
back is a small ritual that has saved real DBAs from real disasters.

Usage:
    .venv/bin/python scripts/init_master_key.py /etc/slackbot/master.key

After it writes the key:
    chmod 600 /etc/slackbot/master.key
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet


def _fingerprint(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", help="Where to write the key (e.g. /etc/slackbot/master.key)")
    ap.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing key. DANGEROUS — destroys access to existing encrypted creds.",
    )
    ap.add_argument(
        "--yes-i-have-backed-it-up",
        action="store_true",
        help="Skip the interactive backup confirmation. Only for automation.",
    )
    args = ap.parse_args()

    path = Path(args.path)
    if path.exists() and not args.force:
        print(
            f"ERROR: {path} already exists.\n"
            f"       Pass --force to overwrite (this destroys the ability to "
            f"decrypt every credential currently in target_servers).",
            file=sys.stderr,
        )
        return 1

    key = Fernet.generate_key()
    fp = _fingerprint(key)

    print()
    print(f"Generated new master key.")
    print(f"  Fingerprint: {fp}")
    print(f"  Path:        {path}")
    print()
    print("This file is the ONLY way to decrypt all stored target credentials.")
    print("If you lose it, every target server password must be re-entered manually.")
    print()
    print("Back it up NOW to AT LEAST TWO of:")
    print("  - Your password manager (1Password / Bitwarden / KeePass)")
    print("  - An encrypted USB stick stored offline")
    print("  - A sealed envelope in a safe")
    print()
    print("Do NOT store it in: git, Slack, plain email, shared drives, S3 without KMS.")
    print()

    if not args.yes_i_have_backed_it_up:
        try:
            entered = input(
                f"Type the fingerprint above ({fp}) to confirm you have backed it up: "
            ).strip()
        except EOFError:
            print("ERROR: stdin closed; cannot confirm. Re-run interactively.", file=sys.stderr)
            return 1
        if entered != fp:
            print(
                "Fingerprint mismatch. Key NOT written. Try again when you have a backup.",
                file=sys.stderr,
            )
            return 1

    path.parent.mkdir(parents=True, exist_ok=True)
    # Write atomically: write to a temp sibling, fsync, rename.
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as fh:
        fh.write(key)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)

    fp_path = path.with_suffix(path.suffix + ".fingerprint")
    fp_path.write_text(fp + "\n")
    os.chmod(fp_path, 0o644)

    print()
    print(f"  Key written to:         {path}  (mode 600)")
    print(f"  Fingerprint sidecar at: {fp_path}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
