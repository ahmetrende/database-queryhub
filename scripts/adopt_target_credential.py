#!/usr/bin/env python3
"""Give a target the credential a sibling target already has.

Onboarding a discovered endpoint is two steps: enable it, and give it a
password. The inventory import can only do the first, so it writes a sentinel
into the password column and leaves the second to a human — who then has to
find the secret, paste it somewhere, and hope no shell history keeps it.

On a fleet where one database role serves every instance, that is a lot of
handling for a value the database already holds. This copies the CIPHERTEXT from
a sibling row instead: same Fernet key, so it decrypts identically, and the
plaintext is never read, printed, logged or passed as an argument.

Copying the ciphertext rather than comparing it matters, because the same secret
encrypts to a different blob every time (Fernet randomises the IV). On this
fleet 28 rows hold 5 distinct ciphertexts of 1 password. So "is this the fleet
credential?" can only be answered by decrypting, which is what --check does.

    python3 scripts/adopt_target_credential.py --target NEW --from SIBLING
    python3 scripts/adopt_target_credential.py --check

Tiers other than RO are opt-in (--tier rw|ddl): most targets deliberately have
no write credential, and copying one in would hand out write access to a target
nobody decided to make writable.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from queryhub import audit, db, targets  # noqa: E402

log = logging.getLogger("adopt-credential")

_COLUMNS = {
    "ro": ("username", "password_encrypted"),
    "rw": ("username_rw", "password_rw_encrypted"),
    "ddl": ("username_ddl", "password_ddl_encrypted"),
}


def _row(cur, alias: str) -> dict:
    cur.execute(
        "SELECT id, alias, enabled, username, username_rw, username_ddl, "
        "       password_encrypted, password_rw_encrypted, password_ddl_encrypted "
        "FROM target_servers WHERE alias = %s",
        (alias,),
    )
    rows = cur.fetchall()
    if not rows:
        raise SystemExit(f"no target with alias {alias!r}")
    return rows[0]


def check() -> int:
    """Report enabled targets that cannot actually be used."""
    victims = targets.unprovisioned_enabled()
    if not victims:
        print("clean — every enabled target has a real read-only password")
        return 0
    print(f"{len(victims)} enabled target(s) still hold the import placeholder:")
    for v in victims:
        print(f"  {v['id']:>4}  {v['alias']}")
    print("\nThese are visible to users and will fail on the first query.")
    return 1


def adopt(target_alias: str, source_alias: str, tier: str, apply: bool) -> int:
    user_col, pw_col = _COLUMNS[tier]
    with db.transaction() as cur:
        dst = _row(cur, target_alias)
        src = _row(cur, source_alias)

        if not src[pw_col]:
            raise SystemExit(f"{source_alias} has no {tier.upper()} credential to copy")
        if targets._is_placeholder(src[pw_col]):
            raise SystemExit(
                f"{source_alias} is itself a placeholder — pick a target that works")
        if src[user_col] != dst[user_col] and dst[user_col]:
            # Different role names mean different secrets; copying one under the
            # other's username produces a login that fails in a way that looks
            # like a password problem.
            raise SystemExit(
                f"username mismatch: {target_alias} uses {dst[user_col]!r} but "
                f"{source_alias} uses {src[user_col]!r} — not the same role, "
                f"so its password will not work")

        was = "placeholder" if targets._is_placeholder(dst[pw_col]) else (
            "a real password" if dst[pw_col] else "empty")
        print(f"{target_alias}: {tier.upper()} credential is currently {was}")
        print(f"  would copy the ciphertext from {source_alias} "
              f"(role {src[user_col]!r})")
        if not apply:
            print("\ndry run — pass --apply to write it")
            return 0

        cur.execute(
            f"UPDATE target_servers SET {user_col} = %s, {pw_col} = %s, "
            f"       updated_at = NOW() WHERE id = %s",
            (src[user_col], src[pw_col], dst["id"]),
        )
        audit.log_in(
            cur,
            request_id=None,
            actor_slack_id=None,
            actor_name="adopt_target_credential.py",
            action="target_credential_set",
            details={
                "target_server_id": dst["id"],
                "alias": dst["alias"],
                "tier": tier,
                "from": was,
                "source_alias": src["alias"],
                "method": "ciphertext copied from a sibling row; the plaintext "
                          "was never read or printed",
            },
        )
        print(f"  applied — {target_alias} now shares {source_alias}'s "
              f"{tier.upper()} credential")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true",
                   help="list enabled targets still holding the placeholder")
    p.add_argument("--target", help="alias receiving the credential")
    p.add_argument("--from", dest="source", help="alias to copy it from")
    p.add_argument("--tier", default="ro", choices=sorted(_COLUMNS),
                   help="credential tier (default: ro)")
    p.add_argument("--apply", action="store_true",
                   help="write it; without this the run is a dry run")
    a = p.parse_args()

    if a.check:
        return check()
    if not a.target or not a.source:
        p.error("--target and --from are required (or use --check)")
    return adopt(a.target, a.source, a.tier, a.apply)


if __name__ == "__main__":
    raise SystemExit(main())
