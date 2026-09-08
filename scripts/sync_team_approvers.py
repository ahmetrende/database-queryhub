#!/usr/bin/env python3
"""Make each team's lead an approver for the targets that team owns.

Reads ownership from `target_team` and reconciles `role_assignment`. It used
to take that ownership as a CSV; migration 115 made it a relation, so the
answer now lives in the model where a screen can show it and a person can
correct it — and this script needs no input at all beyond which source to own.
Fill `target_team` first with `scripts/sync_target_owners.py`, or by hand.

WHY ONE ROW PER TARGET. The rule this exists to express is "the lead approves
requests FROM their team TO their team's databases" — both conditions, which
`can_approve` checks as a scoped team plus a scoped target. `scope_target_id`
holds one target, so a team owning seven databases is seven rows. That is not
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

WHAT CHANGES WITHOUT IT. A new member of a team needs nothing — the role is
scoped to the TEAM, so they are covered the moment they appear in
`team_member`. A new TARGET does need a row, and so does a change of lead;
those two are what this closes.

    python3 scripts/sync_team_approvers.py --source pod-sync
    python3 scripts/sync_team_approvers.py --source pod-sync --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from queryhub import audit, db  # noqa: E402

_TIERS = ("ro", "rw", "ddl")


def resolve(cur):
    """(wanted, label, notes) from the model.

    A wanted row is (lead principal, team, target) — every team that owns a
    target, paired with that team's lead. Both halves come from tables a
    person can see and edit: `target_team` for ownership, `team_member.is_lead`
    for who speaks for the team.
    """
    notes: list[str] = []
    wanted: set[tuple[int, int, int]] = set()
    label: dict[tuple[int, int, int], str] = {}

    cur.execute(
        "SELECT tt.target_id, tt.team_id, ts.alias, t.display_name AS team_name "
        "  FROM target_team tt "
        "  JOIN team t ON t.id = tt.team_id AND NOT t.is_deleted "
        "  JOIN target_servers ts ON ts.id = tt.target_id AND ts.enabled "
        " ORDER BY t.display_name, ts.alias")
    owned = cur.fetchall()
    if not owned:
        notes.append("no team owns any enabled target — fill target_team first")

    leads: dict[int, list[dict]] = {}
    for row in owned:
        if row["team_id"] not in leads:
            cur.execute(
                "SELECT m.principal_id, p.display_name, p.enabled "
                "  FROM team_member m JOIN principal p ON p.id = m.principal_id "
                " WHERE m.team_id = %s AND m.is_lead "
                "   AND NOT m.is_deleted AND NOT p.is_deleted", (row["team_id"],))
            leads[row["team_id"]] = cur.fetchall()
            if not leads[row["team_id"]]:
                notes.append(f"'{row['team_name']}' owns targets but has no "
                             f"lead in QueryHub — skipped")
        for lead in leads[row["team_id"]]:
            if not lead["enabled"]:
                note = (f"'{row['team_name']}' lead {lead['display_name']} is "
                        f"disabled — skipped")
                if note not in notes:
                    notes.append(note)
                continue
            key = (lead["principal_id"], row["team_id"], row["target_id"])
            wanted.add(key)
            label[key] = (f"{lead['display_name']} → {row['team_name']}"
                          f" @ {row['alias']}")
    return wanted, label, notes


def plan(cur, source: str, max_tier: str):
    wanted, label, notes = resolve(cur)
    cur.execute(
        "SELECT id, principal_id, scope_team_id, scope_target_id, max_tier "
        "  FROM role_assignment "
        " WHERE source = %s AND role = 'approver' "
        "   AND revoked_at IS NULL AND NOT is_deleted", (source,))
    live = {(r["principal_id"], r["scope_team_id"], r["scope_target_id"]): r
            for r in cur.fetchall()}

    add = sorted(wanted - set(live), key=lambda k: label.get(k, ""))
    drop = sorted(set(live) - wanted)
    # A ceiling that no longer matches is a revoke-and-recreate, because a role
    # is immutable and `role_assignment_live_uq` would refuse the pair anyway.
    retier = [k for k in wanted & set(live) if live[k]["max_tier"] != max_tier]
    return {"add": add, "drop": drop, "retier": retier,
            "live": live, "label": label, "notes": notes}


def apply(cur, source: str, p, max_tier: str, actor: str) -> None:
    for key in p["drop"] + p["retier"]:
        cur.execute("UPDATE role_assignment SET revoked_at = NOW() "
                    " WHERE id = %s", (p["live"][key]["id"],))
    for key in p["add"] + p["retier"]:
        pid, team_id, target_id = key
        cur.execute(
            "INSERT INTO role_assignment "
            "  (principal_id, role, scope_team_id, all_teams, scope_target_id, "
            "   all_targets, max_tier, any_tier, reason, source, created_by) "
            "VALUES (%s,'approver',%s,FALSE,%s,FALSE,%s,FALSE,%s,%s,"
            "        (SELECT p.id FROM principal p "
            "           JOIN principal_identity i ON i.principal_id = p.id "
            "          WHERE i.provider='slack' AND i.external_id = %s "
            "            AND NOT i.is_deleted LIMIT 1))",
            (pid, team_id, target_id, max_tier,
             f"team lead, synced from {source}", source, actor))


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
        for key in p["drop"]:
            r = p["live"][key]
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
