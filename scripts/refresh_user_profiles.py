#!/usr/bin/env python3
"""Refresh every roster row's name / email / timezone from Slack.

`profile_sync` already does this for one person, lazily: the first time they
submit a query or press an admin button. That leaves anybody who has not
interacted since their Slack profile changed showing a stale name for as long
as they stay quiet -- and a name is what the audit trail, the queue and every
DM are read by.

This is the same lookup for the whole roster at once. Run it after a change to
what `profile_sync` considers a name, or when a screen shows somebody a name
they do not use.

    python scripts/refresh_user_profiles.py            # show the diff only
    python scripts/refresh_user_profiles.py --apply    # write it

Dry-run by default, prints every change either way, and writes one `audit_log`
row for the batch so the trail says where the names came from.
"""
from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, "src")

from queryhub import audit, config as cfg, db  # noqa: E402

cfg._maybe_load_encrypted_secrets()

import os  # noqa: E402

try:
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web import WebClient
except ModuleNotFoundError:
    sys.exit("slack_sdk is not installed — this script needs the [slack] extra.")

FIELDS = ("name", "email", "tz")


def _slack_profile(client: WebClient, uid: str) -> dict | None:
    """name / email / tz for one id, in the same precedence profile_sync uses.
    None means Slack could not answer for this id."""
    try:
        user = client.users_info(user=uid).get("user") or {}
    except SlackApiError as e:
        print(f"  {uid}: users.info failed — "
              f"{(e.response or {}).get('error', e)}")
        return None
    p = user.get("profile") or {}
    return {
        # `real_name` before `real_name_normalized`: the normalized variant is
        # Slack's ASCII fold, and the fold is what put the wrong spelling on
        # every screen in the first place.
        "name": (p.get("real_name") or p.get("real_name_normalized")
                 or user.get("real_name") or user.get("name")),
        "email": p.get("email"),
        "tz": user.get("tz"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (default: show them only)")
    args = ap.parse_args()

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return print("SLACK_BOT_TOKEN is not set.") or 1
    client = WebClient(token=token)

    rows = db.fetch_all(
        "SELECT slack_user_id, name, email, tz, 'requesters' AS tbl "
        "  FROM requesters "
        "UNION ALL "
        "SELECT slack_user_id, name, email, tz, 'admins' FROM admins "
        " ORDER BY slack_user_id")
    by_id: dict[str, list[dict]] = {}
    for r in rows:
        by_id.setdefault(r["slack_user_id"], []).append(r)
    print(f"{len(by_id)} people across {len(rows)} roster rows.\n")

    # Slack, once per person, then compared against every row that person has.
    changes: list[tuple[str, str, str, str, str]] = []   # tbl, uid, field, old, new
    unreachable = 0
    for uid, mine in by_id.items():
        fresh = _slack_profile(client, uid)
        if fresh is None:
            unreachable += 1
            continue
        for row in mine:
            for f in FIELDS:
                new = fresh.get(f)
                if new and new != row.get(f):
                    changes.append((row["tbl"], uid, f, row.get(f), new))
        time.sleep(0.05)                      # users.info is tier-3 rate limited

    if not changes:
        print(f"Nothing to change. {unreachable} id(s) Slack could not answer for.")
        return 0

    print(f"{len(changes)} field(s) differ:\n")
    for tbl, uid, f, old, new in changes:
        print(f"  {tbl:11} {uid}  {f:5} {old!r} -> {new!r}")
    if unreachable:
        print(f"\n{unreachable} id(s) Slack could not answer for — left alone.")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write these.")
        return 0

    # One transaction: the batch is a single fact about where the names came
    # from, and a half-applied roster is worse than a stale one.
    with db.transaction() as cur:
        for tbl, uid, f, _old, new in changes:
            # Both halves are our own literals -- the table name comes from the
            # UNION above, the column from FIELDS -- and neither can be an
            # identifier this loop did not write. Checked anyway: a value that
            # reaches an f-string SQL statement should never be taken on trust.
            assert tbl in ("requesters", "admins") and f in FIELDS
            cur.execute(f"UPDATE {tbl} SET {f} = %s WHERE slack_user_id = %s",
                        (new, uid))
        audit.log_in(
            cur, None, None, "refresh_user_profiles.py",
            "user_profiles_refreshed",
            {"people": len(by_id), "fields_changed": len(changes),
             "unreachable": unreachable, "source": "slack users.info",
             "changes": [{"table": t, "user": u, "field": f,
                          "from": o, "to": n} for t, u, f, o, n in changes]})
    print(f"\nApplied {len(changes)} change(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
