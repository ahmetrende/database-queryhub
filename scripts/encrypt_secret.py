"""Encrypt a single secret with the bot's master.key and print the Fernet
ciphertext to stdout. Used to populate `target_servers.password_encrypted`
when adding a target via raw SQL (db_admin_templates.sql).

Usage:
    .venv/bin/python scripts/encrypt_secret.py
    Password: ********
    gAAAAABp9R...

Then paste the printed ciphertext as the :password_encrypted parameter in
the INSERT statement.

The plaintext is read from a TTY prompt (no echo) and never written to
disk or shell history. The output is written ONCE to stdout — capture it
immediately.
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dba_slack_bot.crypto import encrypt  # noqa: E402


def main() -> int:
    pw = getpass.getpass("Password: ")
    if not pw:
        print("Empty password — nothing encrypted.", file=sys.stderr)
        return 1
    confirm = getpass.getpass("Re-enter to confirm: ")
    if pw != confirm:
        print("Mismatch — try again.", file=sys.stderr)
        return 1
    print(encrypt(pw))
    return 0


if __name__ == "__main__":
    sys.exit(main())
