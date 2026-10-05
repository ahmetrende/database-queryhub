#!/usr/bin/env python3
"""Make each team's lead an approver for the targets that team owns.

THE RULE: the lead approves requests TO their team's databases, whoever sends
them, up to the ceiling (`--max-tier`, default RO). The team owns the data, so
its lead is the one who knows what a query against it means, and they see who
is reading it.

It used to be BOTH conditions: FROM my team AND TO my team's databases. A
person given access to another pod's database then matched no lead at all, and
their requests went to the admins alone. That was not rare. On 2026-09-29, 58
of the 120 live single-server personal grants were on a server the grantee's
own pod does not own, and 28 of the 268 requests decided by a person in the
previous 30 days were exactly that case. So a row now says "any requester" (all
teams) for one owned target, and the requester's team no longer matters.

Reads ownership from `target_team` and reconciles `role_assignment`. The
logic is in `queryhub.owner_approvers`, which the Connections screen
shares. It used to take that ownership as a CSV; migration 115 made it a
relation, so the answer now lives in the model where a screen can show it
and a person can correct it — and this script needs no input at all beyond
which source to own.
Fill `target_team` first with `scripts/sync_target_owners.py`, or by hand.

WHY ONE ROW PER TARGET. `scope_target_id` holds one target, so a team owning
seven databases is seven rows, each with `all_teams`: `can_approve` then checks
the target and the ceiling, and skips the requester's team. That is not
a shape worth maintaining by hand: a team gains a service and the set is
silently wrong until somebody notices a request going to the wrong queue.

WHAT IT DOES NOT DO, each one deliberate:

* **It never grants access.** An approver role decides other people's
  requests; it reaches no database. If a lead needs to query what they
  approve, that is a separate grant a person makes.
* **It never touches a row it does not own.** Only rows carrying its own
  `--source` are updated or revoked. A role somebody wrote by hand
  (`source IS NULL`) and one the migration-109 mirror projects
  (`mirrored_from`) are both invisible to it.
* **It never invents a lead.** A team with no `is_lead` member, or a lead with
  no QueryHub account, is reported and skipped. Somebody being named a lead in
  an org chart is not a decision to give them approval authority.
* **It never widens the tier.** `--max-tier` is a ceiling, default `ro`.

WHAT CHANGES WITHOUT IT. A new member of a team needs nothing — the role does
not look at the requester's team at all. A new TARGET does need a row, and so
does a change of lead; those two are what this closes.

    python3 scripts/sync_team_approvers.py --source pod-sync
    python3 scripts/sync_team_approvers.py --source pod-sync --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from queryhub import audit, db, owner_approvers  # noqa: E402

# The logic lives in the package, because the Connections screen runs the same
# reconcile for one target right after an owner change. This file is the CLI.
_TIERS = owner_approvers.TIERS
plan = owner_approvers.plan
apply = owner_approvers.apply


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True,
                    help="owns the rows it writes; must not be empty")
    ap.add_argument("--max-tier", default="ro", choices=_TIERS,
                    help="the ceiling these approvers get (default ro)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--notify", action="store_true",
                    help="let the role DMs go out (off by default on a bulk run)")
    ap.add_argument("--actor", default="", help="your Slack id, for the audit row")
    a = ap.parse_args()

    if not a.source.strip():
        raise SystemExit("--source names the rows this owns; it cannot be empty.")

    with db.transaction() as cur:
        if not a.notify:
            cur.execute("SET LOCAL app.auth_dm_suppress = 'on'")
        p = plan(cur, a.source, a.max_tier)

        print(f"\nsource '{a.source}' — {len(p['live'])} role(s) live here")
        for key in p["add"]:
            print(f"  +  {p['label'].get(key, key)}  (up to {a.max_tier.upper()})")
        for key in p["retier"]:
            print(f"  ~  {p['label'].get(key, key)}  ceiling → {a.max_tier.upper()}")
        reshaped = {(k[0], k[2]) for k in p["add"]}
        for key in p["drop"]:
            r = p["live"][key]
            if (key[0], key[2]) in reshaped:
                print(f"  -  role {r['id']} scoped to one team, replaced by the "
                      f"any-requester row above")
            else:
                print(f"  -  role {r['id']} no longer owned by that team")
        for n in p["notes"]:
            print(f"  !  {n}")
        if not (p["add"] or p["drop"] or p["retier"]):
            print("  nothing to change")

        if not a.apply:
            print("\ndry run — nothing written. Re-run with --apply.")
            cur.connection.rollback()
            return 0

        apply(cur, a.source, p, a.max_tier, a.actor)
        audit.log_in(cur, None, a.actor or a.source, a.actor or a.source,
                     "team_approvers_synced",
                     {"source": a.source,
                      "max_tier": a.max_tier, "added": len(p["add"]),
                      "revoked": len(p["drop"]), "retiered": len(p["retier"]),
                      "notes": p["notes"], "notified": bool(a.notify)})
        print("\napplied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
